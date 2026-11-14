"""Build on the work board starts a fix tab: a new Claude tab in the project's work session, stack Fixes.

The answer moves the item to Ready (work_items.answer_stage). Then Tether opens the tab, links it to the item as its
fix tab and moves the item to Building. When the work session is not open, or the tab does not start, the item
stays Ready and its history says why; the drawer's "Start fix tab" button tries again."""
import os
import subprocess
import threading

import delegate
import groups
import paths
import work_items
from projects import ProjectError

STACK = 'Fixes'
START_LOCK = threading.Lock()


class NotStarted(Exception):
    """The fix tab did not start. The message says why, for Mak."""


def anchor(work_session, entries):
    """A live tab in the work session (tmux name or label, any case): the new tab opens after it. Agent tabs first."""
    name = (work_session or '').casefold()
    if not name:
        return None
    found = [entry for entry in entries
             if name in ((entry.get('tmux_session') or '').casefold(), (entry.get('label') or '').casefold())]
    found.sort(key=lambda entry: entry.get('agent') not in ('claude', 'codex'))
    return found[0] if found else None


def brief(item, question=''):
    """The fix tab's first message."""
    lines = [f'Fix work item {item["id"]}: {item["title"]}']
    if question:
        lines.append(f'Mak chose Build on: {question}')
    if item.get('refs'):
        lines.append('Links: ' + ', '.join(item['refs']))
    lines += ['',
              f'1. Run `tether item show {item["id"]}` and read its history before you change anything.',
              '2. Make the fix and check it.',
              f'3. When it is ready for review, run `tether item stage {item["id"]} review --text "<what changed>"`.']
    if item.get('qa'):
        lines.append('This item needs QA testing when Done.')
    return '\n'.join(lines)


def live_fix(item, entries):
    """A fix tab of this item whose pane still runs an agent, or None. A pane left at a shell does not count."""
    live = {entry['id'] for entry in entries if entry.get('agent') not in (None, 'shell')}
    return next((tab for tab in item.get('tabs', []) if tab.get('role') == 'fix' and tab.get('pane') in live), None)


def _session_name(pane, run):
    """The tmux session name of a live pane, read now (a pane can move to another session)."""
    shown = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_id}\t#{session_name}'],
                capture_output=True, text=True, timeout=5)
    fields = (shown.stdout or '').rstrip('\n').split('\t')
    if shown.returncode or len(fields) != 2 or fields[0] != pane:
        raise RuntimeError(f'Tab {pane} closed. Try again.')
    return fields[1]


def _release(item_id, claimed):
    """After a failed launch, move the claimed item back to Ready. A note or other write meanwhile does not stop it;
    a stage move or a new fix tab by someone else does."""
    fixes = {tab['pane'] for tab in claimed.get('tabs', []) if tab.get('role') == 'fix'}
    for _ in range(3):
        _, item = work_items.find(item_id)
        if item['stage'] != 'building' or {tab['pane'] for tab in item.get('tabs', []) if tab.get('role') == 'fix'} - fixes:
            return
        try:
            work_items.stage(item_id, 'ready', 'tether', version=item['version'])
            return
        except ProjectError:
            continue


def start(item_id, version, entries, run=subprocess.run, lock=None, question=''):
    """Move a Ready item to Building, open its fix tab and link it. Returns the item.
    Raises NotStarted (the item is back in Ready, with a history line) or ProjectError (409: the item changed)."""
    with START_LOCK:
        _, item = work_items.find(item_id)
        if item['version'] != version:
            raise ProjectError(409, f'{item_id} changed. Reload and try again.')
        if item['stage'] != 'ready':
            raise ProjectError(409, f'{item_id} is not Ready. Only a Ready item starts a fix tab.')
        if live_fix(item, entries):
            raise ProjectError(409, f'{item_id} already has a fix tab.')
        project = work_items.project(item['project'])
        source = anchor(project.get('work'), entries)
        if source is None:
            raise NotStarted(f'The work session {project.get("work") or "(none)"} is not open. '
                             'Open it, then use Start fix tab.')
        folder = os.path.expanduser(project.get('folder') or '')
        if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
            raise NotStarted(f'The project folder {project.get("folder") or "(none)"} does not exist. '
                             'Fix it in the project settings, then use Start fix tab.')
        name = f'{item["id"]} fix'
        body = {'agent': 'claude', 'message': brief(item, question), 'name': name,
                'session': source['id'], 'thread': source.get('thread')}
        try:
            delegate.validate(body, entries)
        except ValueError as exc:
            raise NotStarted(str(exc)) from exc
        with lock or threading.Lock():
            try:
                # The anchor tab can move to another session after the list was read: check it under the lock.
                if _session_name(source['id'], run).casefold() != project['work'].casefold():
                    raise RuntimeError(f'Tab {source["id"]} is no longer in the work session {project["work"]}. Try again.')
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                raise NotStarted(str(exc) or type(exc).__name__) from exc
            # Claim the item before the launch: the store refuses (409) when it changed since the request was read.
            claimed = work_items.stage(item_id, 'building', 'tether', text=f'Starting fix tab {name}', version=version)
            try:
                session_id = groups.group_of_pane(source['id'], run)[0]
                group = groups.find_or_create(session_id, STACK, run)
                pane, name = delegate.start(source, body, folder, run=run, group=group['id'] if group else '')
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, groups.GroupError) as exc:
                _release(item_id, claimed)
                raise NotStarted(str(exc) or type(exc).__name__) from exc
        # The tab runs now. Link it whatever changed in the item meanwhile, so a retry cannot start a second one.
        return work_items.link(item_id, pane, 'fix', by='tether', name=name, session=project['work'], stack=STACK)


def try_start(item, entries, run=subprocess.run, lock=None, question='', strict=False):
    """Start the fix tab; a failure becomes a history line. Returns (item, {'started': bool, 'why': str}).
    strict: raise ProjectError (409: changed, not Ready, or a live fix tab) instead of returning it."""
    try:
        return start(item['id'], item['version'], entries, run, lock, question), {'started': True, 'why': ''}
    except NotStarted as exc:
        return work_items.record(item['id'], 'fix', f'Fix tab not started. {exc}'), {'started': False, 'why': str(exc)}
    except ProjectError as exc:
        if strict:
            raise
        return item, {'started': False, 'why': str(exc)}
