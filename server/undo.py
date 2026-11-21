"""Undo and redo for Tether steps: stacks, tab and session names, tab and session close and start, tab moves between
sessions, and the tab and session order.

undo_store.py keeps the history. This module records the steps (the wrappers below) and runs their inverses.
Rules:
- Never guess. Each inverse first checks that the world is still as the step left it (same tmux server, same tab, same
  stack, same name). When it is not, it refuses with the reason (Refused) and changes nothing.
- An inverse that starts or stops an agent, brings back a session, brings back a stack of several tabs, or moves a tab
  between sessions is critical: plan() returns its effects, and the page asks before it runs.
- An undo or redo records nothing itself (undo_store.quiet).
- Only the server records (undo_store.enable). The one-time cleanup CLI (auto_stacks_cleanup.py) and other tools that
  call groups.py directly record nothing: an operator runs them with --dry-run first, and their many steps would push
  the person's own steps out of the 50-entry history.
inventory: .release-work/2026-11-15/undo/inventory.md (outside the repository).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import replace

import auto_resume
import coordinator
import groups
import helper_close
import helper_tabs
import paths
import prefs_sync
import projects
import review_links
import task_tree
import tmux_merge
import undo_store
from terminal_input import UUID, create_session, new_window_after, resume_command, valid_name

_RUN_LOCK = threading.Lock()
PANE = re.compile(r'%[0-9]+')
SESSION = re.compile(r'\$[0-9]+')
AGENT_NAMES = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity', 'shell': 'Shell'}


class Refused(ValueError):
    """The undo or redo cannot run now. The message says why."""


def _tmux(run, *args):
    return run([paths.TMUX, *args], capture_output=True, text=True, timeout=5)


def _show(run, target, fmt):
    """The fields of a tmux format for a target, or None when it is gone."""
    shown = _tmux(run, 'display-message', '-p', '-t', target, fmt)
    if shown.returncode:
        return None
    return shown.stdout.rstrip('\n').split('\t')


def _q(name):
    return f'`{name}`' if name else 'a tab'


# ---------------------------------------------------------------- recording wrappers (who acts: undo_store.acting)

def _group_place(session_id, group_id, run):
    found = groups.read(session_id, run)
    index = next((i for i, g in enumerate(found) if g['id'] == group_id), len(found))
    group = next((dict(g) for g in found if g['id'] == group_id), None)
    return group, index


def _pane_name(pane, run):
    # As Tether shows the tab (agents.py): its name, else its session. Not window_name: tmux renames the window after
    # each command, so a new shell is `locale`, then `sh`, and a confirm text would change under the person.
    fields = _show(run, pane, '#{@chat_name}\t#{session_name}')
    return (fields[0] or fields[1]) if fields and len(fields) == 2 else ''


def _group_name(session_id, group_id, run):
    if not group_id:
        return 'Unstacked'
    group = next((g for g in groups.read(session_id, run) if g['id'] == group_id), None)
    return group['name'] if group else 'a stack'


def group_create(session_id, name, run=subprocess.run, pane=None, by_user=False):
    if not undo_store.recording():
        return groups.create(session_id, name, run, pane=pane, by_user=by_user)
    with groups.LOCK():  # the read before, the change and the record are one step for other writers
        before = (groups._pane(pane, run)[1], groups.placed_by_user(pane, run)) if pane else ('', False)
        from_group, from_index = _group_place(session_id, before[0], run) if before[0] else (None, 0)
        group = groups.create(session_id, name, run, pane=pane, by_user=by_user)
        try:
            _, index = _group_place(session_id, group['id'], run)
            label = f'Made stack {_q(group["name"])}' + (f' with {_q(_pane_name(pane, run))}' if pane else '')
        except Exception as error:  # noqa: BLE001 - the stack exists
            _unrecorded(f'stack {group["id"]}', error)
            return group
        undo_store.record('group.create', label, session=session_id, group=group, index=index, pane=pane,
                          pane_from=before[0] if from_group else '', by_before=before[1], from_group=from_group,
                          from_index=from_index)
    return group


def _created_at(session_id, group, run):
    """The place of a stack just made, for its record; None when the read failed (the stack stays, unrecorded)."""
    try:
        return _group_place(session_id, group['id'], run)[1]
    except Exception as error:  # noqa: BLE001 - the stack exists
        _unrecorded(f'stack {group["id"]}', error)
        return None


def group_find_or_create(session_id, name, run=subprocess.run):
    """groups.find_or_create (a Tether stack), recorded when it made the stack."""
    if not undo_store.recording():
        return groups.find_or_create(session_id, name, run)
    with groups.LOCK():
        known = {g['id'] for g in groups.read(session_id, run)}
        group = groups.find_or_create(session_id, name, run)
        if group and group['id'] not in known:
            index = _created_at(session_id, group, run)
            if index is None:
                return group
            undo_store.record('group.create', f'Made stack {_q(group["name"])}', session=session_id, group=group,
                              index=index, pane=None, pane_from='', by_before=False)
    return group


def group_rename(session_id, group_id, name, run=subprocess.run):
    if not undo_store.recording():
        return groups.rename(session_id, group_id, name, run)
    with groups.LOCK():
        old, _ = _group_place(session_id, group_id, run)
        group = groups.rename(session_id, group_id, name, run)
        if old and old['name'] != group['name'] or old and groups._maker(old) != groups._maker(group):
            undo_store.record('group.rename', f'Renamed stack {_q(old["name"])} to {_q(group["name"])}',
                              session=session_id, group=group_id, old=old, new=dict(group))
    return group


def _unrecorded(what, error):
    """A step happened, but its record failed (a tmux read timed out). The step stands: recording undo is optional and
    must never fail the action. The open step is spoiled, so no part of it is saved alone."""
    print(f'undo: could not record {what}: {error!r}', file=sys.stderr)
    undo_store.spoil()


def _move_record(session_id, pane, group_id, run, by_user, mover):
    if not undo_store.recording():
        return mover()
    with groups.LOCK():
        return _move_locked(session_id, pane, group_id, run, by_user, mover)


def _move_locked(session_id, pane, group_id, run, by_user, mover):
    found = groups.read(session_id, run)
    _, old = groups._pane(pane, run)
    old = old if any(g['id'] == old for g in found) else ''
    by_before = groups.placed_by_user(pane, run)
    old_group = next((dict(g) for g in found if g['id'] == old), None)
    old_index = next((i for i, g in enumerate(found) if g['id'] == old), 0)
    others = [p for p in groups.panes_in(session_id, old, run) if p != pane] if old else []
    moved = mover()
    if moved is False or old == (group_id or ''):
        return moved
    try:
        after = groups.read(session_id, run)
        dropped = old_group if old_group and not any(g['id'] == old for g in after) else None
        to_name = _group_name(session_id, group_id or '', run)
        label = f'Moved {_q(_pane_name(pane, run))} to {to_name}'
        undo_store.record('group.move', label, session=session_id, pane=pane, frm=old, to=group_id or '',
                          frm_name=old_group['name'] if old_group else '', to_name=to_name if group_id else '',
                          by_before=by_before, by_after=by_user or by_before, dropped=dropped, dropped_index=old_index,
                          released=_released(others, run) if dropped else [])
    except Exception as error:  # noqa: BLE001 - the move happened
        _unrecorded(f'the move of tab {pane}', error)
    return moved


def _released(panes, run):
    """The tabs a dissolved stack put in Unstacked (its main session: groups.drop_if_alone)."""
    out = []
    for pane in panes:
        place = _pane_in(pane, run)
        if place and not place[1]:
            out.append(pane)
    return out


def group_move(session_id, pane, group_id, run=subprocess.run, by_user=False):
    return _move_record(session_id, pane, group_id, run, by_user,
                        lambda: groups.move(session_id, pane, group_id, run, by_user=by_user))


def group_ungroup(session_id, group_id, run=subprocess.run, by_user=False):
    if not undo_store.recording():
        return groups.ungroup(session_id, group_id, run, by_user=by_user)
    with groups.LOCK():
        group, index = _group_place(session_id, group_id, run)
        panes = groups.panes_in(session_id, group_id, run) if group else []
        by_before = {pane: groups.placed_by_user(pane, run) for pane in panes}
        groups.ungroup(session_id, group_id, run, by_user=by_user)
        if group:
            record_ungroup(session_id, group, index, by_before, by_user,
                           label=f'Unstacked {_q(group["name"])} ({len(panes)} tab{"" if len(panes) == 1 else "s"})')


def record_ungroup(session_id, group, index, by_before, by_user=False, label=None):
    """The shared hook for a stack that went away and left its tabs in Unstacked (Unstack, and the stack dissolve):
    group is the stack as it was, index its place, by_before {pane: placed by the user before}."""
    undo_store.record('group.ungroup', label or f'Dissolved stack {_q(group["name"])}', session=session_id,
                      group=dict(group), index=index, panes=list(by_before), by_before=by_before, by_after=by_user)


def group_remove(session_id, group_id, run=subprocess.run, **check):
    with groups.LOCK():
        removed = groups.remove(session_id, group_id, run, **check)
        if removed:
            group, index = removed
            undo_store.record('group.remove', f'Removed stack {_q(group["name"])}', session=session_id, group=group,
                              index=index)


# Names

NAME_OPTIONS = ('@chat_name', '@chat_name_at', '@chat_name_by', '@chat_name_auto')


def name_state(pane, run=subprocess.run):
    """The pane's name options before a route renames it, or None when nothing records."""
    if not undo_store.recording():
        return None
    try:
        return _name_state(pane, run)
    except Exception as error:  # noqa: BLE001 - a read for the record: the rename goes on (after it, unrecorded)
        _unrecorded(f'the name of tab {pane}', error)
        return None


def _name_state(pane, run):
    fields = _show(run, pane, '\t'.join('#{' + option + '}' for option in NAME_OPTIONS))
    return dict(zip(NAME_OPTIONS, fields)) if fields and len(fields) == len(NAME_OPTIONS) else None


def record_pane_name(pane, before, name, run=subprocess.run):
    if before is None or before.get('@chat_name', '') == name:
        return
    old = before.get('@chat_name', '')
    label = (f'Renamed tab {_q(old)} to {_q(name)}' if old and name else f'Named tab {_q(name)}' if name
             else f'Cleared the name of tab {_q(old)}')
    try:
        after = _name_state(pane, run)
    except Exception as error:  # noqa: BLE001 - the rename happened
        return _unrecorded(f'the name of tab {pane}', error)
    undo_store.record('pane.name', label, pane=pane, old=before, new=after or {'@chat_name': name})


def record_session_name(session_id, old, new):
    if old and old != new:
        undo_store.record('session.name', f'Renamed session {_q(old)} to {_q(new)}', session=session_id, old=old,
                          new=new)


# Tabs and sessions

def capture(pane, run=subprocess.run, entry=None):
    """What a reopen needs, read before the pane closes: the auto-resume record when Tether has one (Claude and Codex
    with a conversation), else the pane's folder, names and stack. agent: 'claude', 'codex', 'shell', or another
    agent that cannot resume."""
    fields = _show(run, pane, '\t'.join(['#{session_name}', '#{session_id}', '#{pane_current_path}', '#{window_index}',
                                         '#{pane_index}', *('#{' + o + '}' for o in auto_resume.OPTIONS),
                                         *('#{' + o + '}' for o in auto_resume.SESSION_OPTIONS)]))
    if not fields or len(fields) != 5 + len(auto_resume.OPTIONS) + len(auto_resume.SESSION_OPTIONS):
        return None
    session, session_id, folder, window, index = fields[:5]
    values = fields[5:]
    options = {o: v for o, v in zip(auto_resume.OPTIONS, values) if v}
    session_options = {o: v for o, v in zip(auto_resume.SESSION_OPTIONS, values[len(auto_resume.OPTIONS):]) if v}
    tab = {'pane': pane, 'session': session, 'session_id': session_id, 'folder': folder, 'options': options,
           'session_options': session_options, 'name': options.get('@chat_name', ''), 'order': [int(window), int(index)],
           'agent': '', 'thread': '', 'profile': None, 'transcript': '', 'server': _server(run)}
    saved = auto_resume.saved_tab(pane)
    if saved and (entry is None or (saved['thread'] == helper_tabs._thread(entry)
                                     and saved['agent'] == entry.get('agent'))):
        tab.update(agent=saved['agent'], thread=saved['thread'], profile=saved.get('profile'),
                   transcript=saved.get('transcript') or '', folder=saved.get('folder') or folder)
    elif entry is not None:
        thread = entry.get('thread')
        tab['agent'] = entry.get('agent') or ''
        if isinstance(thread, str) and UUID.fullmatch(thread) and not entry.get('pending_conversation'):
            tab.update(thread=thread, profile=entry.get('codex_profile'), transcript=entry.get('transcript') or '',
                       folder=entry.get('thread_cwd') or folder)
    if not tab['agent']:
        command = _show(run, pane, '#{pane_current_command}')
        tab['agent'] = 'shell' if command and command[0] in ('bash', 'zsh', 'sh', 'fish', 'dash', 'ksh') else ''
    if reviews := review_links.capture_close(pane):
        tab['reviews'] = reviews
    label = options.get('@group')
    try:
        tab['peers'] = [p for p in groups.panes_in(session_id, label, run) if p != pane] if label else []
    except groups.GroupError:
        tab['peers'] = []  # then a stack the close dissolves comes back with this tab only
    return tab


def note_released(tab, run=subprocess.run):
    """After the close: when the close dissolved the tab's stack (groups.closing: a Tether-made stack with only its main
    session left), the tabs it put in Unstacked, so the reopen puts them back in the stack."""
    stack = _saved_stack(tab)
    tab['released'] = []
    try:
        if stack and not any(g['id'] == stack['id'] for g in _groups(tab['session_id'], run)):
            tab['released'] = _released(tab.get('peers', []), run)  # _released_open keeps those in its session
    except (Refused, OSError, subprocess.SubprocessError):
        pass  # the session closed with the tab: no tab of it is left to put back
    return tab


def session_of(pane, run=subprocess.run):
    """(session id, session name) of a live pane, for a record; None when gone, not recording or the read failed."""
    if not undo_store.recording():
        return None
    try:
        fields = _show(run, pane, '#{session_id}\t#{session_name}')
    except Exception as error:  # noqa: BLE001 - a read for the record: the rename goes on
        _unrecorded(f'the session of tab {pane}', error)
        return None
    return tuple(fields) if fields and len(fields) == 2 else None


def _listed(entries):
    return entries() if callable(entries) else entries


def capture_safe(pane, run=subprocess.run, entries=()):
    """capture(), for a close route: an error here must never stop the close. entries: the session list, or a
    function that returns it (called here, so its errors are caught too)."""
    if not undo_store.recording():
        return None
    try:
        return capture(pane, run, next((entry for entry in _listed(entries) if entry.get('id') == pane), None))
    except Exception as error:  # noqa: BLE001 - recording undo is optional; the close goes on
        _unrecorded(f'tab {pane} before close', error)
        return None


def capture_session_safe(session_id, run=subprocess.run, entries=()):
    if not undo_store.recording():
        return None
    try:
        return capture_session(session_id, run, _listed(entries))
    except Exception as error:  # noqa: BLE001 - as capture_safe
        _unrecorded(f'session {session_id} before close', error)
        return None


CLOSE_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError, ValueError)


class CloseInterrupted(RuntimeError):
    def __init__(self, error, members, identifier, label, *, stale=False, without_undo=(), unknown=()):
        super().__init__(error)
        self.status = 409 if stale else 503
        self.members = members
        self.undo = identifier
        self.label = label
        self.without_undo = without_undo
        self.unknown = unknown


def kill_outcome(target, server, run, *, session=False):
    """Return True if gone, False if present, or None if tmux cannot prove either."""
    kind, current = auto_resume.server_id(run=run)
    if kind == 'none':
        return True
    if kind != 'up' or current != server:
        return None
    try:
        if session:
            result = _tmux(run, 'list-sessions', '-F', '#{session_id}')
            pattern = SESSION
        else:
            result = _tmux(run, 'list-panes', '-a', '-F', '#{pane_id}')
            pattern = PANE
        listed = result.stdout.splitlines() if result.returncode == 0 else None
        if listed is not None and not all(pattern.fullmatch(item) for item in listed):
            listed = None
    except (OSError, subprocess.SubprocessError):
        listed = None
    kind, current = auto_resume.server_id(run=run)
    if kind == 'none':
        return True
    if listed is not None and kind == 'up' and current == server:
        return target not in listed
    return None


class CloseJournal:
    """Checkpoint an in-flight close before attempting it. Keep the record when its outcome is unknown."""
    def __init__(self, label, server):
        self.label, self.server = label, server
        self.identifier = self.pending = None
        self.closed = []

    def save(self, actions, *, staging=False):
        status = ('staged' if staging else 'unknown') if self.pending and not self.closed else None
        self.identifier = undo_store.checkpoint(self.identifier, self.label, actions, server=self.server, status=status)

    def kill(self, target, run, close, actions, recover, *, session=False, checkpoint=None):
        self.pending = target
        try:
            if checkpoint is None:
                self.save(actions(), staging=True)
            else:
                checkpoint()
        except CLOSE_ERRORS:
            self.pending = None
            raise
        try:
            close()
        except helper_tabs.Stale:
            self.pending = None  # the guard refused before the kill
            raise
        except CLOSE_ERRORS:
            outcome = kill_outcome(target, self.server, run, session=session)
            if outcome is not None:
                self.pending = None
            if outcome:
                self.closed.append(target)
                recover()
            raise
        self.closed.append(target)
        self.pending = None


def _journal_close(target, run, title, label, server, action, close, recover, after, *, session=False):
    journal = CloseJournal(label, server)

    def actions():
        if action is None or not (journal.closed or journal.pending):
            return []
        return [dict(action, pending=True) if journal.pending else action]

    def finish():
        if journal.closed:
            after()
        journal.save(actions())

    try:
        journal.kill(target, run, close, actions, recover, session=session)
    except CLOSE_ERRORS as exc:
        try:
            finish()
        except CLOSE_ERRORS:
            pass  # The pending step is already on disk.
        stale = isinstance(exc, helper_tabs.Stale)
        if stale:
            error = str(exc)
        elif journal.closed:
            error = 'The close took effect, but tmux did not confirm it. Refresh the tabs.'
        elif journal.pending:
            error = 'Tether could not confirm whether the close took effect. Refresh the tabs.'
        else:
            error = 'Tether could not close the tab or session. Refresh the tabs.'
        raise CloseInterrupted(error, [(target, title)] if journal.closed else [], journal.identifier,
                                label, stale=stale, unknown=[title] if journal.pending else []) from exc
    try:
        finish()
    except CLOSE_ERRORS as exc:
        raise CloseInterrupted('The tab or session closed. Tether could not finish saving Undo.',
                                [(target, title)], journal.identifier, label) from exc
    undo_store.offer_checkpoint(journal.identifier, label)
    return journal.identifier


def tab_close_label(tab, title):
    tab = tab or {}
    return f'Closed tab {_q(tab.get("name") or tab.get("session") or title)}'


def close_tab(pane, run, tab, title, *, close=None, server=None):
    """Close one tab and journal any captured Undo while the caller holds the send lock."""
    label = tab_close_label(tab, title)

    def recover():
        auto_resume.forget_pane(pane, run=run)
        helper_close.release_reviews(pane)

    return _journal_close(pane, run, title, label, server or (tab or {}).get('server'),
                          dict(kind='tab.close', tab=tab) if tab else None,
                          close or (lambda: helper_close.close_pane(pane, run)), recover,
                          lambda: note_released(tab, run) if tab else None)


def record_tab_close(tab, run=subprocess.run):
    if tab:
        try:
            note_released(tab, run)
        except Exception as error:  # noqa: BLE001 - as capture_safe: the close happened, so record it all the same
            print(f'undo: could not read the stack of closed tab {tab["pane"]}: {error!r}', file=sys.stderr)
        return undo_store.record('tab.close', f'Closed tab {_q(tab["name"] or tab["session"])}', tab=tab)


def _task_tree_change(entry, before, after):
    return {'pane': entry['id'], 'thread': helper_tabs.verified_thread(entry),
            'session': entry['tmux_session'], 'before': before, 'after': after}


def record_task_tree_move(entry, before, after):
    if before != after:
        return undo_store.record('task-tree.move', f'Moved tab {_q(helper_tabs.visible_name(entry))}',
                                 **_task_tree_change(entry, before, after))


def record_task_tree_close(closed, kept, run):
    """Save the tabs that actually closed and the placement changes to tabs that stayed."""
    if not undo_store.recording():
        return
    if not closed:
        for entry, before, after in kept:
            record_task_tree_move(entry, before, after)
        return
    for tab, _ in closed:
        try:
            note_released(tab, run)
        except Exception as error:  # noqa: BLE001 - as with a single tab close
            print(f'undo: could not read the stack of closed tab {tab["pane"]}: {error!r}', file=sys.stderr)
    root = closed[-1][0]
    return undo_store.record('task-tree.close', f'Closed task {_q(root["name"] or root["session"])}',
                             closed=[{'tab': tab, 'record': record} for tab, record in closed],
                             kept=[_task_tree_change(entry, before, after) for entry, before, after in kept])


def capture_session(session_id, run=subprocess.run, entries=()):
    by_pane = {entry['id']: entry for entry in entries}
    listed = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{pane_id}')
    if listed.returncode:
        return None
    tabs = [tab for tab in (capture(pane, run, by_pane.get(pane)) for pane in listed.stdout.split()) if tab]
    if not tabs:
        return None
    return {'session_id': session_id, 'name': tabs[0]['session'], 'tabs': sorted(tabs, key=lambda t: t['order'])}


def record_session_close(saved):
    if saved:
        count = len(saved['tabs'])
        undo_store.record('session.close', f'Closed session {_q(saved["name"])} ({count} tab{"" if count == 1 else "s"})',
                          saved=saved)


def close_session(session_id, run, saved, panes, close):
    title = saved['name'] if saved else session_id
    count = len(saved['tabs']) if saved else 0
    label = f'Closed session {_q(title)} ({count} tab{"" if count == 1 else "s"})'
    server = saved['tabs'][0].get('server') if saved else None

    def release():
        for pane in panes:
            helper_close.release_reviews(pane)

    def kill():
        close()
        release()

    return _journal_close(session_id, run, title, label, server,
                          dict(kind='session.close', saved=saved, removed_panes=panes) if saved else None,
                          kill, release, lambda: None, session=True)


def record_tab_start(pane, run=subprocess.run):
    if not undo_store.recording():
        return
    try:
        fields = _show(run, pane, '#{session_id}\t#{session_name}\t#{session_windows}\t#{window_panes}')
        if not fields or len(fields) != 4:
            raise Refused('the tab is gone')
        alone = fields[2] == '1' and fields[3] == '1'
        label = f'Made session {_q(fields[1])}' if alone else f'Started tab {_q(_pane_name(pane, run) or fields[1])}'
    except Exception as error:  # noqa: BLE001 - the tab started
        return _unrecorded(f'the start of tab {pane}', error)
    undo_store.title(label)
    undo_store.record('tab.start', label, pane=pane, session=fields[0])


def record_merge(kind, source, target, run=subprocess.run):
    """Before tmux_merge.merge: returns a function to call with its result, which records the step."""
    if kind not in ('pane', 'break', 'session') or not undo_store.recording():
        return lambda moved: None
    try:
        if kind == 'session':
            name = (_show(run, source + ':', '#{session_name}') or [''])[0]
            into = (_show(run, target + ':', '#{session_name}') or [''])[0]
            return lambda moved: undo_store.record('merge.session', f'Merged session {_q(name)} into {_q(into)}',
                                                   source=source, target=target)
        fields = _show(run, source, '#{session_id}\t#{session_name}\t#{@group}\t#{@group_by}')
        if not fields or len(fields) != 4:
            raise Refused('its place could not be read')
        session_id, session_name, label, by = fields
        name = _pane_name(source, run)
        # The stack it leaves, as it is now: the move or a later sweep can dissolve it (groups.drop_if_alone), and
        # undo brings it back.
        stack, index = _group_place(session_id, label, run) if label else (None, 0)
    except Exception as error:  # noqa: BLE001 - recording undo is optional; the move goes on
        _unrecorded(f'the move of tab {source}', error)
        return lambda moved: None

    def done(moved):
        to = moved.get('session') if isinstance(moved, dict) else None
        if not to:
            return
        if kind == 'pane':
            into = moved.get('name', '')
            text = f'Moved {_q(name)} to session {_q(into)}'
        else:
            text = f'Moved {_q(name)} to a session of its own'
        undo_store.record('merge.' + kind, text, pane=source, frm=session_id, frm_name=session_name, frm_group=label,
                          frm_by=by, to=to, frm_stack=stack, frm_stack_index=index,
                          to_name=moved.get('name', '') if kind == 'pane' else '')
    return done


def record_order(key, old, new):
    """prefs_sync: a reorder of the sessions ('order') or of one session's tabs ('paneOrder'). Only a change of order
    of the same items counts; an added or dropped item is not a reorder."""
    if key == 'order':
        if isinstance(old, list) and isinstance(new, list) and old != new and sorted(old) == sorted(new):
            undo_store.record('prefs.order', 'Reordered sessions', key='order', sub=None, old=old, new=new)
        return
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return
    for session in set(old) & set(new):
        before, after = old[session], new[session]
        if isinstance(before, list) and isinstance(after, list) and before != after and sorted(before) == sorted(after):
            undo_store.record('prefs.order', 'Reordered tabs', key='paneOrder', sub=session, old=before, new=after)
            return


# ---------------------------------------------------------------- the world, for checks and effects

class Env:
    """What the inverses read from the server. server.py passes its own; tests pass fakes."""

    def __init__(self, run=subprocess.run, entries=lambda: [], status=lambda entry: {}, draft=lambda entry: [],
                 web_draft=lambda pane: '', name_window=lambda pane, name: None, fresh=None):
        self.run, self.entries, self.status, self.draft, self.web_draft = run, entries, status, draft, web_draft
        self.name_window = name_window  # server.py name_in_tmux: the window shows the tab name
        self.fresh = fresh or entries
        self._cache = None

    def live(self):
        if self._cache is None:
            self._cache = list(self.entries())
        return self._cache

    def entry(self, pane):
        return next((entry for entry in self.live() if entry.get('id') == pane), None)

    def forget(self):
        self._cache = None

    def refresh(self):
        self._cache = list(self.fresh())


def _server(run):
    kind, identifier = auto_resume.server_id(run=run)
    return identifier if kind == 'up' else None


def _same_server(entry, run):
    # No server id (tmux was down, or the id could not be read when the step was saved): no proof, so refuse.
    if not entry.get('server') or entry['server'] != _server(run):
        raise Refused('tmux restarted after this step, so its tabs and sessions are not the same ones.')


def _same_tmux(server, run, what='that tab'):
    """A pane or session id that an undo or redo made is only that pane or session on the tmux server that made it."""
    if not server or server != _server(run):
        raise Refused(f'tmux restarted since, so {what} is not the same one any more.')


def _session_alive(session_id, run):
    fields = _show(run, session_id + ':', '#{session_id}\t#{session_name}')
    return fields[1] if fields and len(fields) == 2 and fields[0] == session_id else None


def _session_by_name(name, run):
    fields = _show(run, '=' + name + ':', '#{session_id}')
    return fields[0] if fields and SESSION.fullmatch(fields[0]) else None


def _pane_in(pane, run):
    """(session id, @group, @group_by) of a live pane, or None."""
    fields = _show(run, pane, '#{pane_id}\t#{session_id}\t#{@group}\t#{@group_by}')
    if not fields or len(fields) != 4 or fields[0] != pane:
        return None
    return fields[1], fields[2], fields[3]


def _groups(session_id, run):
    try:
        return groups.read(session_id, run)
    except groups.GroupError:
        raise Refused(groups.GONE_SESSION) from None


def _need_pane(pane, run, what='That tab', ctx=None):
    """(session id, @group, @group_by) of a live pane. In a check, ctx['places'] holds where an earlier action of the
    same step puts it (a move to another session, then into a stack there): the check sees the step's world."""
    found = _pane_in(pane, run)
    if found is not None and ctx and pane in ctx.get('places', {}):
        return ctx['places'][pane]
    if found is None:
        raise Refused(f'{what} has closed.')
    return found


# ---------------------------------------------------------------- inverses
# Each kind: check(action, direction, env, ctx) raises Refused; apply(action, direction, env) does it;
# effects(action, direction, env) lists what a critical inverse does ([] = not critical).
# direction 'undo' runs the inverse; 'redo' runs the step again. ctx: {'panes', 'groups'} the entry itself touches.

def _readd(session_id, group, index, run):
    with groups.LOCK():
        found = _groups(session_id, run)
        if any(g['id'] == group['id'] for g in found):
            raise Refused(f'Stack {_q(group["name"])} exists again.')
        if any(g['name'].lower() == group['name'].lower() for g in found):
            raise Refused(f'Another stack is named {_q(group["name"])} now.')
        found.insert(min(index, len(found)), dict(group))
        groups.write(session_id, found, run)
    _bundle_back(group)


def _bundle_back(group):
    if group.get('project'):
        try:
            projects.unarchive(group['project'])
        except (OSError, projects.ProjectError):
            pass  # the stack is back; its context bundle stays in the archive


def _drop(session_id, group_id, run):
    with groups.LOCK():
        found = _groups(session_id, run)
        group = next((g for g in found if g['id'] == group_id), None)
        if group is None:
            return
        groups.write(session_id, [g for g in found if g['id'] != group_id], run)
    groups._archive(group)


def _set_label(pane, group_id, by_user, run):
    groups._label(pane, group_id, run)
    if by_user:
        _tmux(run, 'set-option', '-p', '-t', pane, groups.BY, 'user')
    else:
        _tmux(run, 'set-option', '-p', '-u', '-t', pane, groups.BY)


def _check_free(session_id, group, run, ctx):
    found = _groups(session_id, run)
    if any(g['id'] == group['id'] for g in found):
        raise Refused(f'Stack {_q(group["name"])} exists again.')
    if any(g['name'].lower() == group['name'].lower() for g in found):
        raise Refused(f'Another stack is named {_q(group["name"])} now.')


def _check_group(session_id, group, run, ctx, name=True):
    if group['id'] in ctx.get('groups_pending', ()):
        return None
    current = next((g for g in _groups(session_id, run) if g['id'] == group['id']), None)
    if current is None:
        raise Refused(f'Stack {_q(group["name"])} no longer exists.')
    if name and current['name'] != group['name']:
        raise Refused(f'Stack {_q(group["name"])} was renamed to {_q(current["name"])} since.')
    return current


class GroupCreate:
    @staticmethod
    def check(a, d, env, ctx):
        run = env.run
        if d == 'undo':
            _check_group(a['session'], a['group'], run, ctx)
            extra = set(groups.panes_in(a['session'], a['group']['id'], run)) - ctx['panes'] - {a.get('pane')}
            if extra:
                raise Refused(f'Stack {_q(a["group"]["name"])} has other tabs now. Move them out first.')
            if a.get('pane'):
                place = _pane_in(a['pane'], run)
                if place and place[0] != a['session']:
                    raise Refused('That tab is in another session now.')
                if place and place[1] != a['group']['id']:
                    raise Refused('That tab moved to another stack since.')
                GroupCreate._check_from(a, run)
        else:
            _check_free(a['session'], a['group'], run, ctx)
            if a.get('pane'):
                place = _need_pane(a['pane'], run)
                if place[0] != a['session']:
                    raise Refused('That tab is in another session now.')
                found = {g['id']: g for g in _groups(a['session'], run)}
                if (place[1] if place[1] in found else '') != a.get('pane_from', ''):
                    raise Refused('That tab moved to another stack since.')
                saved, current = a.get('from_group'), found.get(a.get('pane_from', ''))
                if saved and current and current['name'] != saved['name']:
                    raise Refused(f'Stack {_q(saved["name"])} was renamed to {_q(current["name"])} since.')

    @staticmethod
    def _check_from(a, run):
        """The undo puts the tab back in the stack it came from. That stack may have gone since (Tether removes an
        emptied stack it made): it comes back when its name is free, else the undo refuses."""
        if not a.get('pane_from'):
            return False
        current = next((g for g in _groups(a['session'], run) if g['id'] == a['pane_from']), None)
        if current is not None:
            saved = a.get('from_group')
            if saved and current['name'] != saved['name']:
                raise Refused(f'Stack {_q(saved["name"])} was renamed to {_q(current["name"])} since.')
            return False
        if not a.get('from_group'):
            raise Refused('The stack it came from no longer exists.')
        try:
            _check_free(a['session'], a['from_group'], run, {})
        except Refused:
            raise Refused(f'The stack it came from, {_q(a["from_group"]["name"])}, no longer exists, and another '
                          'stack has its name now.') from None
        return True

    @staticmethod
    def apply(a, d, env):
        run = env.run
        if d == 'undo':
            if a.get('pane') and _pane_in(a['pane'], run):
                if GroupCreate._check_from(a, run):
                    _readd(a['session'], a['from_group'], a.get('from_index', 0), run)
                _set_label(a['pane'], a.get('pane_from', ''), a.get('by_before'), run)
            _drop(a['session'], a['group']['id'], run)
        else:
            _readd(a['session'], a['group'], a['index'], run)
            if a.get('pane'):
                _set_label(a['pane'], a['group']['id'], a['group'].get('by') == 'user', run)

    @staticmethod
    def effects(a, d, env):
        return []


class GroupRename:
    @staticmethod
    def check(a, d, env, ctx):
        now, then = (a['new'], a['old']) if d == 'undo' else (a['old'], a['new'])
        _check_group(a['session'], now, env.run, ctx)
        if any(g['name'].lower() == then['name'].lower() and g['id'] != a['group'] for g in _groups(a['session'], env.run)):
            raise Refused(f'Another stack is named {_q(then["name"])} now.')

    @staticmethod
    def apply(a, d, env):
        then = a['old'] if d == 'undo' else a['new']
        with groups.LOCK():
            found = _groups(a['session'], env.run)
            group = next((g for g in found if g['id'] == a['group']), None)
            if group is None:
                raise Refused(groups.GONE_GROUP)
            for key in ('auto', 'root', 'by'):
                group.pop(key, None)
            group.update({k: v for k, v in then.items() if k in ('name', 'auto', 'root', 'by')})
            groups.write(a['session'], found, env.run)
        if group.get('project'):
            try:
                projects.set_name(group['project'], group['name'])
            except (OSError, projects.ProjectError):
                pass

    @staticmethod
    def effects(a, d, env):
        return []


class GroupMove:
    @staticmethod
    def _named(a, group_id, run, ctx):
        """Refuse when a stack of the step was renamed since (a step recorded before names were kept skips this)."""
        name = a.get('frm_name') if group_id == a['frm'] else a.get('to_name')
        if group_id and name:
            _check_group(a['session'], {'id': group_id, 'name': name}, run, ctx)

    @staticmethod
    def check(a, d, env, ctx):
        run = env.run
        place = _need_pane(a['pane'], run, ctx=ctx)
        if place[0] != a['session']:
            raise Refused('That tab is in another session now.')
        now, then = (a['to'], a['frm']) if d == 'undo' else (a['frm'], a['to'])
        if place[1] != now:
            raise Refused('That tab moved to another stack since.')
        GroupMove._named(a, now, run, ctx)
        if not then:
            return
        gone = a.get('dropped') if d == 'undo' else a.get('to_dropped')
        if gone and gone['id'] == then and not any(g['id'] == then for g in _groups(a['session'], run)):
            _check_free(a['session'], gone, run, ctx)  # the step dropped it: it comes back
            if d == 'undo':
                _check_back(_open_in(a.get('released', []), a['session'], run), gone['name'], 'this tab moved out', run)
            return
        if then in ctx.get('groups_pending', ()):
            return
        if not any(g['id'] == then for g in _groups(a['session'], run)):
            raise Refused('The stack it came from no longer exists.' if d == 'undo' else groups.GONE_GROUP)
        GroupMove._named(a, then, run, ctx)

    @staticmethod
    def _drop_saved(a, group_id, key, run):
        """Keep the dropped stack and its place in a[key], so the other way brings it back. Redo leaves the stack the
        tab came from as the move did (groups.move: drop_if_alone, which also puts a lone main session in Unstacked);
        undo leaves the stack it went to only when that is empty (drop_if_empty), as it was before the move."""
        found = _groups(a['session'], run)
        index = next((i for i, g in enumerate(found) if g['id'] == group_id), 0)
        group = next((dict(g) for g in found if g['id'] == group_id), None)
        if group is None:
            return
        if key == 'dropped':
            others = [p for p in groups.panes_in(a['session'], group_id, run) if p != a['pane']]
            if groups.drop_if_alone(a['session'], group_id, run):
                a[key], a[key + '_index'], a['released'] = group, index, _released(others, run)
        elif groups.drop_if_empty(a['session'], group_id, run):
            a[key], a[key + '_index'] = group, index

    @staticmethod
    def apply(a, d, env):
        run = env.run
        if d == 'undo':
            if a.get('dropped') and not any(g['id'] == a['frm'] for g in _groups(a['session'], run)):
                _readd(a['session'], a['dropped'], a.get('dropped_index', 0), run)
                for pane in a.get('released', []):
                    place = _pane_in(pane, run)
                    if place and place[0] == a['session'] and not place[1]:
                        groups._label(pane, a['frm'], run)
            _set_label(a['pane'], a['frm'], a.get('by_before'), run)
            if a['to']:
                GroupMove._drop_saved(a, a['to'], 'to_dropped', run)
        else:
            if a.get('to_dropped') and a['to'] and not any(g['id'] == a['to'] for g in _groups(a['session'], run)):
                _readd(a['session'], a['to_dropped'], a.get('to_dropped_index', 0), run)
            _set_label(a['pane'], a['to'], a.get('by_after'), run)
            if a['frm']:
                GroupMove._drop_saved(a, a['frm'], 'dropped', run)

    @staticmethod
    def effects(a, d, env):
        return []


class GroupUngroup:
    @staticmethod
    def _open(a, run):
        """The tabs that are still open and still in Unstacked (undo), or still in the stack (redo)."""
        out = []
        for pane in a['panes']:
            place = _pane_in(pane, run)
            if place and place[0] == a['session']:
                out.append((pane, place[1]))
        return out

    @staticmethod
    def check(a, d, env, ctx):
        run = env.run
        if d == 'undo':
            _check_free(a['session'], a['group'], run, ctx)
            moved = [pane for pane, label in GroupUngroup._open(a, run) if label]
            if moved:
                raise Refused('A tab of that stack is in another stack now.')
            if not GroupUngroup._open(a, run) and a['panes']:
                raise Refused(f'The tabs of {_q(a["group"]["name"])} have closed.')
        else:
            _check_group(a['session'], a['group'], run, ctx)
            recorded = {pane for pane, label in GroupUngroup._open(a, run) if label == a['group']['id']}
            if set(groups.panes_in(a['session'], a['group']['id'], run)) != recorded:
                raise Refused(f'Stack {_q(a["group"]["name"])} has other tabs now than when it was unstacked.')

    @staticmethod
    def apply(a, d, env):
        run = env.run
        if d == 'undo':
            _readd(a['session'], a['group'], a['index'], run)
            for pane, _ in GroupUngroup._open(a, run):
                _set_label(pane, a['group']['id'], a['by_before'].get(pane, False), run)
        else:
            groups.ungroup(a['session'], a['group']['id'], run, by_user=a.get('by_after', False))

    @staticmethod
    def effects(a, d, env):
        count = len(GroupUngroup._open(a, env.run))
        if count < 2:
            return []
        if d == 'undo':
            return [f'Brings back stack {_q(a["group"]["name"])} and puts {count} tabs back in it.']
        return [f'Moves the {count} tabs of {_q(a["group"]["name"])} to Unstacked and removes the stack. No agent stops.']


class GroupRemove:
    @staticmethod
    def check(a, d, env, ctx):
        if d == 'undo':
            _check_free(a['session'], a['group'], env.run, ctx)
        else:
            _check_group(a['session'], a['group'], env.run, ctx)
            if groups.stack_members(a['session'], a['group']['id'], env.live(), env.run):
                raise Refused(f'Stack {_q(a["group"]["name"])} has tabs now.')

    @staticmethod
    def apply(a, d, env):
        if d == 'undo':
            _readd(a['session'], a['group'], a['index'], env.run)
        else:
            groups.remove(a['session'], a['group']['id'], env.run, entries=env.live())

    @staticmethod
    def effects(a, d, env):
        return []


class PaneName:
    @staticmethod
    def check(a, d, env, ctx):
        _need_pane(a['pane'], env.run)
        now = (a['new'] if d == 'undo' else a['old']).get('@chat_name', '')
        current = (_name_state(a['pane'], env.run) or {}).get('@chat_name', '')
        if current != now:
            raise Refused(f'That tab was renamed to {_q(current)} since.' if current else 'That tab name changed since.')

    @staticmethod
    def apply(a, d, env):
        state = a['old'] if d == 'undo' else a['new']
        command = []
        for option in NAME_OPTIONS:
            value = state.get(option, '')
            if option == '@chat_name_at' and state.get('@chat_name'):
                value = str(int(time.time() * 1000))  # newest: it wins against an agent's older /rename
            command += (['set-option', '-p', '-t', a['pane'], option, value] if value
                        else ['set-option', '-p', '-u', '-t', a['pane'], option]) + [';']
        result = _tmux(env.run, *command[:-1])
        if result.returncode:
            raise Refused('Could not rename the tab. It may have closed.')
        env.name_window(a['pane'], state.get('@chat_name', ''))

    @staticmethod
    def effects(a, d, env):
        return []


class SessionName:
    @staticmethod
    def check(a, d, env, ctx):
        now, then = (a['new'], a['old']) if d == 'undo' else (a['old'], a['new'])
        current = _session_alive(a['session'], env.run)
        if current is None:
            raise Refused(groups.GONE_SESSION)
        if current != now:
            raise Refused(f'That session was renamed to {_q(current)} since.')
        if _session_by_name(then, env.run):
            raise Refused(f'Another session is named {_q(then)} now.')

    @staticmethod
    def apply(a, d, env):
        then = a['old'] if d == 'undo' else a['new']
        if _tmux(env.run, 'rename-session', '-t', a['session'] + ':', then).returncode:
            raise Refused('Could not rename the session.')

    @staticmethod
    def effects(a, d, env):
        return []


# Close and reopen: the shared parts of tab and session steps.

def _close_effects(pane, env, name=None, agent=None):
    entry = env.entry(pane)
    title = name or (entry or {}).get('pane_name') or (entry or {}).get('agent_name') or _pane_name(pane, env.run)
    agent = AGENT_NAMES.get((entry or {}).get('agent') or agent)
    if agent == 'Shell':
        lines = [f'Closes shell tab {_q(title)}.']
    else:
        lines = [f'Closes tab {_q(title)}. {agent or "The program"} in it stops.']
    if entry is not None:
        try:
            state = env.status(entry).get('state')
        except Exception:
            state = None
        if state == 'Working':
            lines.append('It is working now. The running turn stops and its unfinished work is lost.')
        elif state in ('Approval', 'Input needed'):
            lines.append('It waits on a prompt now. The prompt is lost.')
        try:
            draft = [line for line in env.draft(entry) if line.strip()]
        except Exception:
            draft = []
        if draft:
            lines.append(f'Its terminal has unsent text: “{" ".join(draft)[:80]}”.')
    web = env.web_draft(pane)
    if web.strip():
        lines.append(f'Its message box in Tether has unsent text: “{web.strip()[:80]}”.')
    if entry is not None and entry.get('agent') in ('claude', 'codex') and entry.get('thread') and not entry.get('pending_conversation'):
        lines.append('Tether keeps the conversation. Redo opens it again.')
    return lines


def _close(pane, env):
    helper_close.close_pane(pane, env.run)
    env.forget()


def _restore_reviews(tabs, reopened, env, sources=None):
    snapshots = [tab['reviews'] for tab in tabs if tab.get('reviews')]
    if not snapshots:
        return None
    sources = sources or {}
    bindings = {sources.get(tab['pane'], tab['pane']): (reopened[tab['pane']], tab['thread']) for tab in tabs}
    try:
        threads = {entry['id']: helper_tabs.verified_thread(entry) for entry in env.fresh()}
        if review_links.restore_close(snapshots, bindings, threads):
            return None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f'undo: could not restore review state: {exc}', file=sys.stderr)
    return 'Tether could not restore all review state. Check the review links.'


def _reopen_check(tab, env, session_ok=True):
    if tab['agent'] in ('claude', 'codex'):
        if not tab.get('thread'):
            raise Refused(f'Tab {_q(tab["name"] or tab["session"])} had no conversation yet, so there is nothing to resume.')
        holder = next((entry for entry in env.live() if entry.get('id') != 'saved'
                       and entry.get('thread') == tab['thread']), None)
        if holder is not None:
            raise Refused(f'That conversation is open in tab {_q(holder.get("pane_name") or holder.get("label"))}.')
        import history  # slow import: only when a reopen is checked
        try:
            chat = history.find_chat(tab['thread'], agent=tab['agent'])
        except ValueError:
            chat = None
        if chat is None and not (tab.get('transcript') and os.path.isfile(tab['transcript'])):
            raise Refused('Tether cannot find that conversation any more.')
    elif tab['agent'] != 'shell':
        raise Refused(f'{AGENT_NAMES.get(tab["agent"], "That agent")} tabs cannot be resumed.')
    if not os.path.isdir(tab['folder']):
        raise Refused(f'The folder {tab["folder"]} no longer exists.')


def _target_session(tab, run):
    """The open session a reopened tab goes back to: its own session, when it is still open under the same name. None:
    no session has the name, so the tab gets a new session with it. Refuses rather than guess at another session.
    A session id is that session only on the tmux server that saved it: tmux gives out the same ids after a restart."""
    session_id = tab.get('session_id') or ''
    same = bool(tab.get('server')) and tab['server'] == _server(run)
    if not same:
        if _session_by_name(tab['session'], run):
            raise Refused(f'tmux restarted since, so session {_q(tab["session"])} is not the same one any more.')
        return None
    current = _session_alive(session_id, run) if SESSION.fullmatch(session_id) else None
    if current == tab['session']:
        return session_id
    if current is not None:
        raise Refused(f'Session {_q(tab["session"])} was renamed to {_q(current)} since.')
    if _session_by_name(tab['session'], run):
        raise Refused(f'A different session is named {_q(tab["session"])} now.')
    return None


def _place(session_id, order, run):
    """(pane to open after, whether the new window then moves in front of it), for the tab's old window index."""
    listed = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{window_index}\t#{pane_id}')
    rows = []
    for line in listed.stdout.splitlines() if listed.returncode == 0 else []:
        window, _, pane = line.partition('\t')
        if window.isdigit() and PANE.fullmatch(pane):
            rows.append((int(window), pane))
    if not rows:
        raise Refused(groups.GONE_SESSION)
    want = order[0] if isinstance(order, list) and order and isinstance(order[0], int) else None
    earlier = [pane for window, pane in rows if want is not None and window < want]
    if earlier:
        return earlier[-1], False
    return (rows[0][1], True) if want is not None else (rows[-1][1], False)


def _reopen(tab, env, after=None):
    """Start the tab again: after the given pane (a session coming back), else in its own session at its old place
    (_target_session), else in a new session with its old name. Returns the new pane."""
    run = env.run
    session_id = None if after else _target_session(tab, run)
    front = False
    if session_id:
        _check_stack(tab, session_id, run)
        after, front = _place(session_id, tab.get('order'), run)
    options = _options_for(tab, existing=bool(after))
    if after:
        pane = new_window_after(after, run, cwd=tab['folder'], group=options['options'].get('@group', ''))
    else:
        if not valid_name(tab['session']):
            raise Refused('The old session name is not valid any more.')
        pane = create_session(tab['session'], tab['folder'], run=run)
    try:
        if front:
            _tmux(run, 'swap-window', '-d', '-s', pane, '-t', after)
        session_id = _show(run, pane, '#{session_id}')[0]
        auto_resume.apply_options(options, pane, session_id, run=run)
        _unarchive_stack(tab)
        if tab['agent'] in ('claude', 'codex'):
            command = resume_command(tab['thread'], agent=tab['agent'], profile=tab.get('profile'))
            _tmux(run, 'send-keys', '-t', pane, '-l', command)
            _tmux(run, 'send-keys', '-t', pane, 'Enter')
    except Exception:
        _tmux(run, 'kill-pane', '-t', pane)
        raise
    env.forget()
    return pane


def _saved_stack(tab):
    """The stack the tab was in when it closed, as saved then, or None (Unstacked)."""
    label = tab['options'].get('@group')
    return next((g for g in groups.parse(tab['session_options'].get('@groups', '')) if label and g['id'] == label), None)


def _check_stack(tab, session_id, run):
    """A tab reopened in a session that is open goes back to its stack only when that stack is the same one: not
    renamed since, and, when it is gone, its name still free (groups.restore brings it back)."""
    saved = _saved_stack(tab)
    if saved is None:
        return
    found = _groups(session_id, run)
    current = next((g for g in found if g['id'] == saved['id']), None)
    if current is not None and current['name'] != saved['name']:
        raise Refused(f'Stack {_q(saved["name"])} was renamed to {_q(current["name"])} since.')
    if current is None and any(g['name'].lower() == saved['name'].lower() for g in found):
        raise Refused(f'Stack {_q(saved["name"])} no longer exists, and another stack is named {_q(saved["name"])} now.')


def _options_for(tab, existing):
    """In a session that is open, a reopened tab brings back only its own stack (when it is gone and its name is
    free: groups.restore), not the session's other old stacks or its old project. A label with no saved stack goes:
    the tab shows in Unstacked, with no id that a later stack could match."""
    if not existing:
        return tab
    own = _saved_stack(tab)
    options = dict(tab['options'])
    if own is None and options.pop('@group', None):
        options.pop('@group_by', None)
    session_options = {'@groups': json.dumps([own])} if own else {}
    return dict(tab, options=options, session_options=session_options)


def _check_reopen_target(tab, run):
    session_id = _target_session(tab, run)
    if session_id:
        _check_stack(tab, session_id, run)


def _released_open(tab, run):
    """[(pane, @group, @group_by)] of the tabs the close released (note_released) that are open in its session."""
    if not tab.get('released') or not tab.get('server') or tab['server'] != _server(run):
        return []  # pane ids repeat after a tmux restart: those tabs are gone
    return _open_in(tab['released'], tab['session_id'], run)


def _open_in(panes, session_id, run):
    """[(pane, @group, @group_by)] of the panes that are open in the session."""
    out = []
    for pane in panes:
        place = _pane_in(pane, run)
        if place and place[0] == session_id:
            out.append((pane, place[1], place[2]))
    return out


def _check_released(tab, run):
    """The close dissolved the tab's stack and put its main session in Unstacked: the reopen puts it back, so it must
    still be there and not placed by hand since. A tab that closed since is left out."""
    _check_back(_released_open(tab, run), _saved_stack(tab)['name'] if tab.get('released') else '', 'this tab closed',
                run)


def _check_back(released, stack_name, when, run):
    """The tabs a dissolve put in Unstacked go back in the stack only when they are still there and the user did not
    place them by hand since: else refuse before any change."""
    for pane, label, by in released:
        if label or by == 'user':
            raise Refused(f'Tab {_q(_pane_name(pane, run))} left stack {_q(stack_name)} when {when}, and it was '
                          'moved since.')


def _restack_released(tab, pane, run):
    """After the reopen brought the stack back: its main session goes back in it, as before the close."""
    stack = _saved_stack(tab)
    place = _pane_in(pane, run)
    if not stack or not place or place[1] != stack['id'] or place[0] != tab['session_id']:
        return  # the tab came back in a new session, or without its stack
    for other, label, by in _released_open(tab, run):
        if not label and by != 'user':
            groups._label(other, stack['id'], run)


def _unarchive_stack(tab):
    """The stack of a reopened tab came back (groups.restore): bring its context bundle back too, when it went to the
    archive with the stack."""
    label = tab['options'].get('@group')
    for group in groups.parse(tab['session_options'].get('@groups', '')) if label else []:
        if group['id'] == label:
            _bundle_back(group)


def _reopen_effects(tab):
    name = _q(tab['name'] or tab['session'])
    if tab['agent'] == 'shell':
        return [f'Opens a new shell tab {name} in {tab["folder"]}. Its old output does not come back.']
    return [f'Starts {AGENT_NAMES[tab["agent"]]} again in tab {name} and resumes its conversation.',
            'A turn that was running when it closed does not continue.']


def _check_pending_close(action, target, server, run, *, session=False):
    if not action.get('pending'):
        return action
    outcome = kill_outcome(target, server, run, session=session)
    if outcome is True:
        closed = dict(action, release_reviews=True)
        del closed['pending']
        return closed
    return action if outcome is None else None


class _RedoCloseInterrupted(Refused):
    """Keep the durable Undo of a Redo that stopped partway through."""


def _redo_kill(target, server, env, close, recover, checkpoint, previous, pending, *, session=False):
    """Use the close journal to stage Undo on the existing History step before the kill."""
    journal = CloseJournal(None, server)
    staged = False

    def stage():
        nonlocal staged
        checkpoint(previous, pending={'target': target, 'server': server, 'session': session, 'action': pending})
        staged = True

    try:
        journal.kill(target, env.run, close, None, recover, session=session, checkpoint=stage)
    except CLOSE_ERRORS as exc:
        if not staged:
            raise
        if journal.pending:
            raise _RedoCloseInterrupted(str(exc)) from exc
        if not journal.closed:
            checkpoint(previous)
            raise
        return True  # The kill took effect even though tmux reported an error.
    return False


def _redo_pane_cleanup(pane, env):
    auto_resume.forget_pane(pane, run=env.run)
    helper_close.release_reviews(pane)
    env.forget()


class TabClose:
    """undo: reopen (the pane gets a new id, kept in a['reopened']). redo: close that pane again."""
    @staticmethod
    def check(a, d, env, ctx):
        if d == 'undo':
            if a.get('pending'):
                raise Refused('Tether could not confirm the close. Try Undo again.')
            _reopen_check(a['tab'], env)
            _check_reopen_target(a['tab'], env.run)
            _check_released(a['tab'], env.run)
        else:
            _same_tmux(a.get('reopened_server'), env.run)
            _need_pane(a.get('reopened') or '-', env.run, f'Tab {_q(a["tab"]["name"])}')

    @staticmethod
    def apply(a, d, env, *, checkpoint=None):
        if d == 'undo':
            tab = a['tab']
            if a.get('release_reviews'):
                auto_resume.forget_pane(tab['pane'], run=env.run)
                helper_close.release_reviews(tab['pane'])
            a['reopened'] = _reopen(tab, env)
            a['reopened_server'] = _server(env.run)
            _restack_released(tab, a['reopened'], env.run)
            a.pop('release_reviews', None)
            return _restore_reviews([tab], {tab['pane']: a['reopened']}, env)
        else:
            pane = a['reopened']
            tab = capture(pane, env.run, env.entry(pane))
            if tab is None:
                raise Refused('Tether could not save the reopened tab for Undo.')
            interrupted = _redo_kill(pane, a['reopened_server'], env, lambda: _close(pane, env),
                                     lambda: _redo_pane_cleanup(pane, env),
                                     checkpoint, None, dict(a, tab=tab, pending=True))
            a['tab'] = tab
            note_released(a['tab'], env.run)
            checkpoint(a)
            if interrupted:
                return 'The tab closed, but tmux did not confirm it.'

    @staticmethod
    def effects(a, d, env):
        if d == 'undo':
            return _reopen_effects(a['tab'])
        return _close_effects(a['reopened'], env, a['tab']['name'], a['tab'].get('agent'))


def _tree_snapshot(env, ctx):
    if 'tree' not in ctx:
        try:
            rows = helper_tabs.read_panes(env.run)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused('Tether could not read the task tree. Try again.') from exc
        ctx['tree'] = rows, {entry['id']: entry for entry in env.live()}
    return ctx['tree']


def _tree_check(pane, session, thread, record, rows, entries, *, target=None):
    entry, options = entries.get(pane), rows.get(pane)
    if entry is None or options is None or options.session != session:
        raise Refused(f'Tab {pane} closed or moved to another tmux session.')
    if thread:
        same = helper_tabs.verified_thread(entry) == thread
    else:
        same = entry.get('agent') == 'shell'
    if not same:
        raise Refused(f'Tab {pane} changed conversations. Reload its tab.')
    if not helper_tabs.record_matches(options.record, record, target) or (options.record is None and options.raw):
        raise Refused(f'Tab {pane} changed task placement. Reload its tab.')


def _tree_parent(pane, rows, entries):
    if pane in entries:
        return task_tree.placement(pane, rows, entries)
    # A tab starting in tmux may not yet be in discovery. Treat its recorded parent as possible.
    row = rows[pane]
    if task_tree.is_child(row.record, row.role, row.group_by):
        return helper_tabs.placement_owner(row.record)[0]
    return None


class TaskTreeMove:
    @staticmethod
    def check(a, d, env, ctx):
        rows, entries = _tree_snapshot(env, ctx)
        pane = a['pane']
        current, target = (a['after'], a['before']) if d == 'undo' else (a['before'], a['after'])
        _tree_check(pane, a['session'], a['thread'], current, rows, entries, target=target)
        row = rows[pane]
        if not task_tree.is_child(target, row.role, row.group_by):
            return
        if (entries.get(pane) or {}).get('coordinator') in coordinator.MODES:
            raise Refused(f'Tab {pane} is now the coordinator. Release the coordinator role before you put it back in a task.')
        if d == 'redo' and any(_tree_parent(child, rows, entries) == pane for child in rows):
            raise Refused('That tab now has helpers of its own. Move them out first.')
        prospective = {**rows, pane: replace(row, record=target)}
        home = task_tree.placement(pane, prospective, entries)
        if not home or not helper_tabs.verified_thread(entries[home]):
            raise Refused(f'The task for tab {pane} closed or changed conversations.')
        seen = {pane}
        while home:
            if home in seen:
                raise Refused('That placement would make a task loop.')
            seen.add(home)
            home = task_tree.placement(home, prospective, entries)
            if home and d == 'redo':
                raise Refused('The destination is now a helper. Choose its task instead.')

    @staticmethod
    def apply(a, d, env):
        before, after = (a['after'], a['before']) if d == 'undo' else (a['before'], a['after'])
        helper_tabs.write_record(a['pane'], before, after, env.run)

    @staticmethod
    def effects(a, d, env):
        return []


_PANE_REFS = ('owner_pane', 'home_pane', 'root_pane')


def _remap_helper(record, panes):
    if record is None:
        return None
    return {key: panes.get(value, value) if key in _PANE_REFS and isinstance(value, str)
            else value for key, value in record.items()}


def _check_tree_server(a, direction, run):
    if direction == 'redo':
        _same_tmux(a['reopened_server'], run, 'the reopened task')
        return
    kind, current = auto_resume.server_id(run=run)
    saved = a['closed'][-1]['tab']['server']
    if saved and current == saved:
        return
    if kind == 'none' and not a['kept']:
        # The task may have been the last session. A new tmux can reuse any old pane ID.
        # Internal references get mapped to the new IDs; an external reference must be bound
        # to a conversation that will not be in the new session.
        panes = {member['tab']['pane'] for member in a['closed']}
        threads = {member['tab']['thread'] for member in a['closed']}
        for member in a['closed']:
            record = member['record'] or {}
            for key in _PANE_REFS:
                if record.get(key) and record[key] not in panes:
                    bound = record.get(key.replace('_pane', '_thread'))
                    if not bound or bound in threads:
                        raise Refused('tmux stopped. Tether cannot confirm an old relationship to another task.')
        return
    raise Refused('tmux restarted or could not be reached. Tether cannot confirm the task’s tabs.')


class TaskTreeClose:
    @staticmethod
    def check(a, d, env, ctx):
        if d == 'undo' and any(member.get('pending') for member in a['closed']):
            raise Refused('Tether could not confirm which task tabs closed. Try Undo again.')
        if not a['closed']:
            if not a['kept']:
                raise Refused('No task tabs closed.')
            for change in a['kept']:
                TaskTreeMove.check(change, d, env, ctx)
            return
        _check_tree_server(a, d, env.run)
        rows, entries = _tree_snapshot(env, ctx) if d == 'redo' or a['kept'] else ({}, {})
        panes = a.get('closed_panes', {}) if d == 'undo' else a['reopened']
        detached = set(a.get('detached', ()))
        for change in a['kept']:
            field = 'after' if d == 'undo' or change['pane'] in detached else 'before'
            _tree_check(change['pane'], change['session'], change['thread'], _remap_helper(change[field], panes),
                        rows, entries, target=_remap_helper(change['before' if field == 'after' else 'after'], panes))
        if d == 'undo':
            for member in a['closed']:
                tab = member['tab']
                _reopen_check(tab, env)
                _check_reopen_target(tab, env.run)
                _check_released(tab, env.run)
            return

        closing = set()
        for member in a['closed']:
            tab = member['tab']
            if tab['pane'] not in a.get('redone', ()):
                pane = panes[tab['pane']]
                _tree_check(pane, a['reopened_session'], tab['thread'], _remap_helper(member['record'], panes),
                            rows, entries)
                closing.add(pane)
        TaskTreeClose._check_new_children(a, closing, rows, entries)

    @staticmethod
    def _check_new_children(a, closing, rows, entries):
        expected = closing | {change['pane'] for change in a['kept']}
        placed = task_tree.placements(rows, entries)
        for pane in rows:
            if pane in expected:
                continue
            if _tree_parent(pane, rows, entries) in closing or placed.get(pane, {}).get('task') in closing:
                raise Refused(f'Tab {pane} joined the task since. Review the new task before closing it.')

    @staticmethod
    def _undo(a, env):
        run = env.run
        tabs = [member['tab'] for member in a['closed']]
        new_session = _target_session(tabs[0], run) is None
        panes, attached, after = {}, [], None
        try:
            for tab in sorted(tabs, key=lambda tab: tab['order']):
                pane = _reopen(tab, env, after=after)
                panes[tab['pane']] = pane
                if new_session:
                    after = pane
            session = _need_pane(next(iter(panes.values())), run)[0]
            server = _server(run)
            if server is None:
                raise Refused('Tether could not confirm the tmux server of the reopened task.')
            for member in a['closed']:
                if member['record'] is not None:
                    helper_tabs.write_record(panes[member['tab']['pane']], None,
                                             _remap_helper(member['record'], panes), run)
            for change in a['kept']:
                expected = _remap_helper(change['after'], a.get('closed_panes', {}))
                helper_tabs.write_record(change['pane'], expected, _remap_helper(change['before'], panes), run)
                attached.append(change)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
            failed = []
            for change in reversed(attached):
                try:
                    expected = _remap_helper(change['after'], a.get('closed_panes', {}))
                    helper_tabs.write_record(change['pane'], _remap_helper(change['before'], panes), expected, run)
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                    failed.append(change['pane'])
            for pane in reversed(list(panes.values())):
                try:
                    _close(pane, env)
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                    failed.append(pane)
            if failed:
                raise Refused(f'Tether could not restore the task. Check tabs {", ".join(failed)} before retrying.') from None
            raise
        a['reopened'] = panes
        a['reopened_session'] = session
        a['reopened_server'] = server
        for member in a['closed']:
            if member.pop('release_reviews', False):
                helper_close.release_reviews(member['tab']['pane'])
        warning = _restore_reviews(tabs, panes, env, sources=a.get('closed_panes'))
        for tab in tabs:
            try:
                _restack_released(tab, panes[tab['pane']], run)
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                stack_warning = 'The task reopened. Tether could not put all of its tabs back in their stack.'
                return f'{stack_warning} {warning}' if warning else stack_warning
        return warning

    @staticmethod
    def _redo(a, env, checkpoint):
        run, panes = env.run, a['reopened']
        redone, detached = a.get('redone', []), a.get('detached', [])
        captures = []
        for member in a['closed']:
            old = member['tab']['pane']
            if old in redone:
                continue
            tab = capture(panes[old], run, env.entry(panes[old]))
            if tab is None or tab['thread'] != member['tab']['thread'] or tab['agent'] != member['tab']['agent']:
                raise Refused(f'Tether could not save tab {panes[old]} for undo. Try again.')
            captures.append((member, old, dict(tab, pane=old)))

        def projection(*, pending=None):
            # A partial Undo has to restore relationships to the panes that stayed open during Redo.
            closed = [{'tab': dict(member['tab'], pane=panes[old]),
                       'record': _remap_helper(member['record'], panes)}
                      for member in a['closed'] if (old := member['tab']['pane']) in redone]
            if pending:
                member, old, tab = pending
                closed.append({'tab': dict(tab, pane=panes[old]), 'pending': True,
                               'record': _remap_helper(member['record'], panes)})
            kept = [dict(change, before=_remap_helper(change['before'], panes),
                         after=_remap_helper(change['after'], panes))
                    for change in a['kept'] if change['pane'] in detached]
            return dict(kind='task-tree.close', closed=closed, kept=kept) if closed or kept else None

        warning = None
        try:
            for change in a['kept']:
                pane = change['pane']
                if pane not in detached:
                    helper_tabs.write_record(pane, _remap_helper(change['before'], panes),
                                             _remap_helper(change['after'], panes), run)
                    detached.append(pane)
                    a['detached'] = detached
                    checkpoint(projection())
            for member, old, tab in captures:
                pane = panes[old]
                interrupted = _redo_kill(pane, a['reopened_server'], env, lambda: _close(pane, env),
                                         lambda: _redo_pane_cleanup(pane, env),
                                         checkpoint, projection(), projection(pending=(member, old, tab)))
                member['tab'] = tab
                redone.append(old)
                a['redone'] = redone
                note_released(tab, run)
                checkpoint(projection())
                if interrupted:
                    if len(redone) != len(a['closed']):
                        raise _RedoCloseInterrupted('tmux did not confirm the close.')
                    warning = 'The tabs closed. Tether could not confirm the last close.'
        except CLOSE_ERRORS as exc:
            if redone or detached:
                raise _RedoCloseInterrupted(str(exc)) from exc
            raise
        for member in a['closed']:
            try:
                note_released(member['tab'], run)
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                print(f'undo: could not read the stack of closed tab {member["tab"]["pane"]}: {exc!r}', file=sys.stderr)
        a['closed_panes'] = a.pop('reopened')
        for key in ('redone', 'detached', 'reopened_session', 'reopened_server'):
            a.pop(key, None)
        return warning

    @staticmethod
    def apply(a, d, env, *, checkpoint=None):
        if not a['closed']:
            for change in a['kept']:
                TaskTreeMove.apply(change, d, env)
            return None
        return TaskTreeClose._undo(a, env) if d == 'undo' else TaskTreeClose._redo(a, env, checkpoint)

    @staticmethod
    def effects(a, d, env):
        if d == 'undo':
            reopened = [effect for member in reversed(a['closed']) for effect in _reopen_effects(member['tab'])]
            return reopened + ['Restores the task placement of the helpers that stayed open.'] if a['kept'] else reopened
        return [effect for member in a['closed'] if member['tab']['pane'] not in a.get('redone', ())
                for effect in _close_effects(a['reopened'][member['tab']['pane']], env, member['tab']['name'],
                                             member['tab']['agent'])]


class TabStart:
    """undo: close the tab (its state is captured first, for redo). redo: reopen it."""
    @staticmethod
    def check(a, d, env, ctx):
        if d == 'undo':
            _same_tmux(a.get('pane_server') or ctx.get('server'), env.run)
            _need_pane(a['pane'], env.run)
        else:
            if not a.get('tab'):
                raise Refused('Tether has no record of that tab to open again.')
            _reopen_check(a['tab'], env)
            _check_reopen_target(a['tab'], env.run)
            _check_released(a['tab'], env.run)

    @staticmethod
    def apply(a, d, env):
        if d == 'undo':
            a['tab'] = capture(a['pane'], env.run, env.entry(a['pane']))
            _close(a['pane'], env)
            if a['tab']:
                note_released(a['tab'], env.run)
        else:
            a['pane'] = _reopen(a['tab'], env)
            a['pane_server'] = _server(env.run)
            _restack_released(a['tab'], a['pane'], env.run)
            return _restore_reviews([a['tab']], {a['tab']['pane']: a['pane']}, env)

    @staticmethod
    def effects(a, d, env):
        if d == 'undo':
            return _close_effects(a['pane'], env)
        return _reopen_effects(a['tab'])


def _reopen_session(saved, env):
    if _session_by_name(saved['name'], env.run):
        raise Refused(f'A session named {_q(saved["name"])} is open now.')
    opened, after = [], None
    for tab in saved['tabs']:
        try:
            _reopen_check(tab, env)
        except Refused:
            continue
        after = _reopen(dict(tab, session=saved['name'], session_id=''), env, after=after)
        opened.append(after)
    return opened


def _session_reopen_check(saved, env):
    if _session_by_name(saved['name'], env.run):
        raise Refused(f'A session named {_q(saved["name"])} is open now.')
    reasons = []
    for tab in saved['tabs']:
        try:
            _reopen_check(tab, env)
            return
        except Refused as exc:
            reasons.append(str(exc))
    raise Refused(reasons[0] if reasons else 'That session had no tabs to open again.')


def _session_reopen_effects(saved, env):
    lines, skipped = [f'Makes session {_q(saved["name"])} again.'], []
    for tab in saved['tabs']:
        try:
            _reopen_check(tab, env)
            lines += _reopen_effects(tab)[:1]
        except Refused as exc:
            skipped.append(f'Skips tab {_q(tab["name"] or tab["session"])}: {exc}')
    return lines + skipped


def _session_close_effects(session_id, env):
    listed = _tmux(env.run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{pane_id}')
    name = _session_alive(session_id, env.run)
    lines = [f'Closes session {_q(name)} and every tab in it.']
    for pane in listed.stdout.split():
        lines += _close_effects(pane, env)
    return [line for line in lines if not line.startswith('Tether keeps')] + [
        'Tether keeps the conversations. Redo opens them again.']


def _close_session(session_id, env):
    auto_resume.forget_session(session_id, run=env.run)
    if _tmux(env.run, 'kill-session', '-t', session_id + ':').returncode:
        raise Refused('Could not close the session.')
    env.forget()


class SessionClose:
    @staticmethod
    def check(a, d, env, ctx):
        if d == 'undo':
            if a.get('pending'):
                raise Refused('Tether could not confirm the close. Try Undo again.')
            _session_reopen_check(a['saved'], env)
        else:
            _same_tmux(a.get('reopened_server'), env.run, 'that session')
            if not a.get('reopened') or _session_alive(a['reopened'], env.run) != a['saved']['name']:
                raise Refused(f'Session {_q(a["saved"]["name"])} has closed or was renamed.')

    @staticmethod
    def apply(a, d, env, *, checkpoint=None):
        if d == 'undo':
            if a.get('release_reviews'):
                for pane in a.get('removed_panes', ()):
                    helper_close.release_reviews(pane)
            panes = _reopen_session(a['saved'], env)
            a['reopened'] = _show(env.run, panes[0], '#{session_id}')[0] if panes else None
            a['reopened_server'] = _server(env.run)
            a.pop('release_reviews', None)
        else:
            session = a['reopened']
            saved = capture_session(session, env.run, env.live())
            if saved is None:
                raise Refused('Tether could not save the reopened session for Undo.')
            panes = [tab['pane'] for tab in saved['tabs']]

            def release():
                for pane in panes:
                    helper_close.release_reviews(pane)
                env.forget()

            interrupted = _redo_kill(session, a['reopened_server'], env, lambda: _close_session(session, env),
                                     release, checkpoint, None, dict(a, saved=saved, removed_panes=panes, pending=True),
                                     session=True)
            a.update(saved=saved, removed_panes=panes)
            if not interrupted:
                release()
            checkpoint(a)
            if interrupted:
                return 'The session closed, but tmux did not confirm it.'

    @staticmethod
    def effects(a, d, env):
        if d == 'undo':
            return _session_reopen_effects(a['saved'], env)
        return _session_close_effects(a['reopened'], env)


class MergePane:
    """A tab moved to another session (kind 'pane') or to a session of its own (kind 'break'). Both ways move one tab
    between sessions, so both ask first. A session that closed since is not made again: the step refuses."""
    @staticmethod
    def check(a, d, env, ctx):
        place = _need_pane(a['pane'], env.run, ctx=ctx)
        now = a['to'] if d == 'undo' else a['frm']
        if place[0] != now:
            raise Refused('That tab moved to another session since.')
        if d == 'undo' and _session_alive(a['frm'], env.run) is None:
            raise Refused(f'Session {_q(a["frm_name"])} closed after this move, so the tab cannot go back.')
        if d == 'redo' and a['kind'] == 'merge.pane' and _session_alive(a['to'], env.run) is None:
            into = a.get('to_name')
            raise Refused(f'Session {_q(into)} closed after this move, so the tab cannot go there again.' if into
                          else 'The session it moved to closed after this move, so the tab cannot go there again.')
        if d == 'redo':
            # Undo put it back in its stack (or in Unstacked when that stack was gone): a tab placed by hand since stays.
            back = a.get('frm_group') if any(g['id'] == a.get('frm_group') for g in _groups(a['frm'], env.run)) else ''
            if (place[1] or '') != (back or ''):
                raise Refused('That tab moved to another stack since.')
        stack = a.get('frm_stack')
        if (stack and a.get('frm_group') == stack['id']
                and any(g['id'] == stack['id'] for g in _groups(a['frm'], env.run))):
            _check_group(a['frm'], stack, env.run, ctx)  # the tab goes back into it, or leaves it: the same stack
        if d == 'undo' and MergePane._stack_gone(a, env.run):
            _check_free(a['frm'], a['frm_stack'], env.run, ctx)
            _check_back(MergePane._root(a, env.run), a['frm_stack']['name'], 'this tab moved out', env.run)
        # Where this action puts the tab, for the checks of the next actions of the step (a search-menu move: to
        # another session, then into a stack there).
        if d == 'redo' and a['kind'] == 'merge.pane':
            ctx.setdefault('places', {})[a['pane']] = (a['to'], '', place[2])
        elif d == 'undo':
            back = a.get('frm_group') if (MergePane._stack_gone(a, env.run) or any(
                g['id'] == a.get('frm_group') for g in _groups(a['frm'], env.run))) else ''
            ctx.setdefault('places', {})[a['pane']] = (a['frm'], back or '', 'user' if a.get('frm_by') == 'user' else '')

    @staticmethod
    def _stack_gone(a, run):
        """The stack the tab left went away since (the sweep dissolved it), and its session is still open."""
        stack = a.get('frm_stack')
        return bool(stack and a.get('frm_group') == stack['id'] and _session_alive(a['frm'], run) is not None
                    and not any(g['id'] == stack['id'] for g in _groups(a['frm'], run)))

    @staticmethod
    def _root(a, run):
        """[(pane, @group, @group_by)] of the stack's main session, open in its session: the dissolve put it in Unstacked."""
        root = a['frm_stack'].get('root')
        return _open_in([root], a['frm'], run) if root and root != a['pane'] else []

    @staticmethod
    def apply(a, d, env):
        run = env.run
        if d == 'redo':
            moved = (tmux_merge.move_pane(a['pane'], a['to'], run) if a['kind'] == 'merge.pane'
                     else tmux_merge.break_out(a['pane'], run))
            a['to'] = moved['session']
            return moved.get('warning')
        if _session_alive(a['frm'], run) is None:  # closed between the check and now
            raise Refused(f'Session {_q(a["frm_name"])} closed after this move, so the tab cannot go back.')
        restack = MergePane._stack_gone(a, run)
        tmux_merge.move_pane(a['pane'], a['frm'], run)
        if restack:
            _readd(a['frm'], a['frm_stack'], a.get('frm_stack_index', 0), run)
            for root, label, by in MergePane._root(a, run):
                if not label and by != 'user':
                    groups._label(root, a['frm_stack']['id'], run)
        if a.get('frm_group') and any(g['id'] == a['frm_group'] for g in _groups(a['frm'], run)):
            _set_label(a['pane'], a['frm_group'], a.get('frm_by') == 'user', run)

    @staticmethod
    def effects(a, d, env):
        name = _q(_pane_name(a['pane'], env.run))
        if d == 'undo':
            return [f'Moves tab {name} back to session {_q(_session_alive(a["frm"], env.run))}.',
                    'Its agent keeps running.']
        return [f'Moves tab {name} out of this session again. Its agent keeps running.']


class MergeSession:
    @staticmethod
    def check(a, d, env, ctx):
        raise Refused('Tether cannot split merged sessions apart. Drag the tabs out to a session of their own.')

    apply = effects = staticmethod(lambda *args: [])


class PrefsOrder:
    @staticmethod
    def _current(a):
        prefs = prefs_sync.load()['prefs']
        value = prefs.get(a['key'])
        return value.get(a['sub']) if a['sub'] and isinstance(value, dict) else value

    @staticmethod
    def check(a, d, env, ctx):
        if PrefsOrder._current(a) != (a['new'] if d == 'undo' else a['old']):
            raise Refused('The order changed again since.')

    @staticmethod
    def apply(a, d, env):
        then = a['old'] if d == 'undo' else a['new']
        if a['sub']:
            whole = dict(prefs_sync.load()['prefs'].get('paneOrder') or {})
            whole[a['sub']] = then
            prefs_sync.save({'paneOrder': whole})
        else:
            prefs_sync.save({'order': then})

    @staticmethod
    def effects(a, d, env):
        return []


KINDS = {'group.create': GroupCreate, 'group.rename': GroupRename, 'group.move': GroupMove,
         'group.ungroup': GroupUngroup, 'group.remove': GroupRemove, 'pane.name': PaneName,
         'session.name': SessionName, 'tab.close': TabClose, 'tab.start': TabStart, 'session.close': SessionClose, 'merge.pane': MergePane, 'merge.break': MergePane,
         'merge.session': MergeSession, 'prefs.order': PrefsOrder,
         'task-tree.move': TaskTreeMove, 'task-tree.close': TaskTreeClose}
# Kinds whose recorded pane and session ids belong to the tmux server of the entry. tab.start, tab.close and
# session.close check the server of the pane or session they hold themselves (_same_tmux): an undo or redo makes a new one.
TASK_TREE_STEPS = {'task-tree.move', 'task-tree.close'}
REDO_CLOSE_STEPS = {'tab.close', 'session.close', 'task-tree.close'}
LIVE_IDS = {'group.create', 'group.rename', 'group.move', 'group.ungroup', 'group.remove', 'pane.name', 'session.name',
            'merge.pane', 'merge.break', 'merge.session', 'task-tree.move'}


# ---------------------------------------------------------------- planning and running

def _ordered(entry, direction):
    return list(reversed(entry['actions'])) if direction == 'undo' else list(entry['actions'])


def _ctx(entry):
    panes = {a.get('pane') for a in entry['actions'] if a['kind'] in ('group.move', 'tab.start', 'group.create')}
    made = {a['group']['id'] for a in entry['actions'] if a['kind'] == 'group.create'}
    return {'panes': {pane for pane in panes if pane}, 'groups': made}


def _check(entry, direction, env):
    """Raise Refused when any part cannot run now."""
    kinds = [action['kind'] for action in entry['actions']]
    if any(kind not in KINDS for kind in kinds):
        raise Refused('Tether does not know how to undo that step.')
    if any(kind in LIVE_IDS for kind in kinds):
        _same_server(entry, env.run)
    ctx = _ctx(entry)
    ctx['server'] = entry.get('server')
    # Redo makes the entry's stacks before it moves tabs into them: their checks wait for that.
    ctx['groups_pending'] = ctx['groups'] if direction == 'redo' else set()
    for action in _ordered(entry, direction):
        KINDS[action['kind']].check(action, direction, env, ctx)


def _effects(entry, direction, env):
    lines = []
    for action in _ordered(entry, direction):
        lines += KINDS[action['kind']].effects(action, direction, env)
    return lines


def _resolve_close_actions(actions, server, run):
    """Keep unresolved closes and drop only the panes or sessions that the probe proves still open."""
    resolved, refusal = [], None
    for action in actions:
        kind = action['kind']
        if kind == 'tab.close' and action.get('pending'):
            tab = action['tab']
            action = _check_pending_close(action, tab['pane'], tab.get('server') or server, run)
        elif kind == 'session.close' and action.get('pending'):
            saved = action['saved']
            action = _check_pending_close(action, saved['session_id'], saved['tabs'][0].get('server') or server,
                                          run, session=True)
        elif kind == 'task-tree.close':
            members = []
            for member in action['closed']:
                tab = member['tab']
                closed = _check_pending_close(member, tab['pane'], tab['server'], run)
                if closed is None:
                    refusal = 'The tab did not close. Nothing to undo.'
                else:
                    members.append(closed)
            if members != action['closed']:
                action = dict(action, closed=members) if members or action['kept'] else None
        if action is None:
            subject = 'session' if kind == 'session.close' else 'tab'
            refusal = f'The {subject} did not close. Nothing to undo.'
        else:
            resolved.append(action)
    return resolved, refusal


def _resolve_pending_closes(found, env, *, only=None):
    """Check the journal after the writer has finished. Persist outcomes before choosing or applying an inverse."""
    updates = []
    for entry in found:
        if only is not None and entry['id'] != only:
            continue
        actions, redo = entry['actions'], entry.get('redo_close')
        pending = redo and redo['pending']
        if pending:
            projection, refusal = _resolve_close_actions(pending['actions'], pending['server'], env.run)
            if refusal:
                redo = dict(redo, pending=None) if redo['actions'] else None
            elif projection != pending['actions']:
                redo = dict(redo, pending=dict(pending, actions=projection))
        elif entry['state'] == 'done':
            actions, refusal = _resolve_close_actions(actions, entry.get('server'), env.run)
        else:
            continue
        status = entry.get('close_checkpoint')
        if status:
            # A staged record can outlive the writer. On the first read, settle it or mark its outcome unknown.
            status = ('unknown' if any(action.get('pending') or
                                       any(member.get('pending') for member in action.get('closed', ()))
                                       for action in actions) else None)
        if (actions, redo, status) != (entry['actions'], entry.get('redo_close'), entry.get('close_checkpoint')):
            updates.append((entry, actions, redo, status, refusal))
    if not updates:
        return found, None

    def save(stored):
        refused = None
        for entry, actions, redo, status, refusal in updates:
            current = undo_store.find(stored, entry['id'])
            fields = ('state', 'actions', 'redo_close', 'close_checkpoint')
            if current is None or any(current.get(key) != entry.get(key) for key in fields):
                continue
            if not actions:
                stored.remove(current)
            else:
                current['actions'] = actions
                if redo is None:
                    current.pop('redo_close', None)
                else:
                    current['redo_close'] = redo
                if status is None:
                    undo_store.confirm_checkpoint(stored, current)
                else:
                    current['close_checkpoint'] = status
            refused = refusal or refused
        return refused

    refused = undo_store.edit(save)
    return undo_store.entries(), refused


def _undo_view(entry):
    """Treat a partial Redo as an Undo of its actual changes, with the original Redo still resumable."""
    saved = entry.get('redo_close')
    if not saved:
        return entry
    actions = saved['actions']
    if pending := saved['pending']:
        actions = pending['actions']
    return dict(entry, actions=actions, state='done') if actions else entry


def view(entry, env=None, check=True, *, found=None):
    """An entry for the page, after settling pending closes and with the reason a step may refuse."""
    if found is None and env is not None:
        with _RUN_LOCK:
            found, _ = _resolve_pending_closes(undo_store.entries(), env, only=entry['id'])
        entry = undo_store.find(found, entry['id'])
        if entry is None:
            return None
    entry = _undo_view(entry)
    shown = {key: entry[key] for key in ('id', 'at', 'who', 'label', 'state')}
    shown['redo'] = undo_store.redo_available(entry, found if found is not None else [entry])
    shown['undone_at'] = entry.get('undone_at')
    direction = 'undo' if entry['state'] == 'done' else 'redo' if shown['redo'] else None
    if check and env is not None and direction:
        try:
            _check(entry, direction, env)
            shown['why'] = None
        except Refused as exc:
            shown['why'] = str(exc)
        except (groups.GroupError, OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            shown['why'] = str(exc) or 'Tether could not check that step.'
    return shown


def history(env, check=True, *, close_lock=None):
    # The send lock excludes a first close, and the run lock excludes a Redo while we read its journal.
    with close_lock or nullcontext(), _RUN_LOCK:
        env.forget()
        found, _ = _resolve_pending_closes(undo_store.entries(), env)
    next_undo, next_redo = next_target('undo', found), next_target('redo', found)
    label = lambda entry_id: entry_id and undo_store.find(found, entry_id)['label']  # noqa: E731
    return {'entries': [view(entry, env, check, found=found) for entry in reversed(found)],
            'next_undo': next_undo, 'next_redo': next_redo,
            'next_undo_label': label(next_undo), 'next_redo_label': label(next_redo)}


def next_target(direction, found=None):
    """The entry Cmd+Z (undo) or Cmd+Shift+Z (redo) acts on, or None."""
    found = undo_store.entries() if found is None else found
    if direction == 'undo':
        return next((entry['id'] for entry in reversed(found) if _undo_view(entry)['state'] == 'done'), None)
    undone = [entry for entry in found if undo_store.redo_available(entry, found)
              and not entry.get('redo_close', {}).get('pending')]
    return max(undone, key=lambda entry: entry.get('undone_at') or 0)['id'] if undone else None


def token(entry, direction, effects):
    """Binds a yes to what the dialog showed: the entry as it was (its state and the ids and stacks it targets) and the
    effects. When any of them changes before the yes arrives (a redo after another undo got a new pane id, say), the
    page asks again."""
    bound = [entry['id'], direction, entry.get('state'), entry.get('undone_at'), entry.get('server'), entry['actions'],
             effects]
    return hashlib.sha256(json.dumps(bound, sort_keys=True, default=str).encode()).hexdigest()[:24]


def run(direction, entry_id, env, confirmed=None):
    """Undo or redo one entry (None: the next one). Returns {'status': 'done'|'confirm', ...}. Raises Refused.
    confirmed: the token of the confirm answer the person said yes to.
    The check and the change run under one lock with the stack writes (groups.LOCK, which other processes take too)
    and the preference writes (prefs_sync.LOCK), so a step in between cannot slip past the check."""
    if direction not in ('undo', 'redo'):
        raise ValueError('Choose undo or redo.')
    with _RUN_LOCK, groups.LOCK(), helper_tabs.record_lock(), prefs_sync.LOCK:
        return _run(direction, entry_id, env, confirmed)


def _run(direction, entry_id, env, confirmed):
    found = undo_store.entries()
    if direction == 'redo':
        found, _ = _resolve_pending_closes(found, env)
    known = {entry['id'] for entry in found}
    entry_id = entry_id or next_target(direction, found)
    entry = undo_store.find(found, entry_id) if entry_id else None
    if entry is None:
        raise Refused('Nothing to undo.' if direction == 'undo' else 'Nothing to redo.')
    if direction == 'undo':
        found, refused = _resolve_pending_closes(found, env, only=entry_id)
        if refused:
            raise Refused(refused)
        entry = undo_store.find(found, entry_id)
        if entry is None:
            raise Refused('Nothing to undo.')
        entry = _undo_view(entry)
    elif entry.get('redo_close', {}).get('pending'):
        raise Refused('A tab or session may have closed during Redo. Check Undo in History.')
    if (direction == 'undo') != (entry['state'] == 'done') or direction == 'redo' and not undo_store.redo_available(entry, found):
        raise Refused('That step is already undone.' if direction == 'undo' else 'That step cannot be redone now.')
    if any(action['kind'] in TASK_TREE_STEPS for action in entry['actions']):
        env.refresh()
    else:
        env.forget()
    _check(entry, direction, env)
    effects = _effects(entry, direction, env)
    if effects and confirmed != token(entry, direction, effects):
        return {'status': 'confirm', 'id': entry['id'], 'label': entry['label'], 'direction': direction,
                'effects': effects, 'token': token(entry, direction, effects)}
    done, at = 0, time.time()  # the time of this change, under the locks: undo_store orders and ends redo by it
    warnings, applied = [], []

    def checkpoint(action, *, pending=None):
        actions = applied + ([action] if action is not None else [])
        if pending:
            pending = dict(pending)
            pending['actions'] = [*applied, pending.pop('action')]

        def update(stored):
            target = undo_store.find(stored, entry['id'])
            if target is None:
                raise undo_store.LockError('Tether could not save Undo for this Redo.')
            target['actions'] = entry['actions']
            if actions or pending:
                target['redo_close'] = {'actions': actions, 'pending': pending}
            else:
                target.pop('redo_close', None)

        undo_store.edit(update)

    with undo_store.quiet():
        try:
            for action in _ordered(entry, direction):
                apply = KINDS[action['kind']].apply
                if direction == 'redo' and action['kind'] in REDO_CLOSE_STEPS:
                    warning = apply(action, direction, env, checkpoint=checkpoint)
                else:
                    warning = apply(action, direction, env)
                if warning:
                    warnings.append(warning)
                applied.append(action)
                done += 1
        except _RedoCloseInterrupted:
            failed = True
        except Refused:
            if not done:
                raise
            failed = True
        except (groups.GroupError, OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            if not done:
                raise Refused(str(exc) or 'Tether could not do that.') from exc
            failed = True
        else:
            failed = False
    actions = entry['actions']

    def save(stored):
        target = undo_store.find(stored, entry['id'])
        if target is None:
            return
        if not failed or not target.get('redo_close'):
            target['actions'] = actions
        if not failed:
            target.pop('redo_close', None)
            # A step of the person saved while this ran ended redo (undo_store._save): keep it ended.
            # A step saved late but changed before this undo does not end its redo (undo_store._save).
            newer = any(e['id'] not in known and e['who'].get('by') == 'user' and e['at'] > at
                        and not e.get('close_checkpoint') for e in stored)
            target['state'] = 'undone' if direction == 'undo' else 'done'
            target['redo'] = direction == 'undo' and not newer
            target['undone_at'] = at if direction == 'undo' else None
    undo_store.edit(save)
    if failed:
        raise Refused(f'Only part of “{entry["label"]}” could be {"undone" if direction == "undo" else "redone"}. '
                      'Check the tabs, then try again from History.')
    result = {'status': 'done', 'id': entry['id'], 'label': entry['label'], 'direction': direction,
              'state': 'undone' if direction == 'undo' else 'done'}
    if warnings:
        result['warnings'] = warnings
    return result
