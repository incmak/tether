"""Local agent commands share the browser's guarded tab launcher."""
import contextlib
import hmac
import os
import re
import subprocess

import auth
import auto_resume
import auto_stacks
import delegate
import groups
import helper_close
import helper_tabs
import paths
import review_links
import worktrees
from terminal_input import UUID, live_status

PATH = '/mobile/api/agent-tab'


def review_pair(body):
    worker, reviewer = body.get('worker'), body.get('reviewer')
    if (not isinstance(worker, str) or not re.fullmatch(r'%[0-9]+', worker)
            or ('reviewer' in body and (not isinstance(reviewer, str)
                                      or not re.fullmatch(r'%[0-9]+', reviewer)))):
        raise ValueError('Select a tab to stop reviewing.')
    return worker, reviewer


def link_pair(body, entries, last_reply):
    worker, reviewer = body.get('worker'), body.get('reviewer')
    for pane in (worker, reviewer):
        if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
            raise ValueError('Select worker and reviewer tabs.')
    by_pane = {entry['id']: entry for entry in entries}
    if worker not in by_pane or reviewer not in by_pane:
        raise ValueError('That session has ended. Select another tab.')
    if worker == reviewer:
        raise ValueError('Choose different worker and reviewer tabs.')
    source, target = by_pane[worker], by_pane[reviewer]
    if any(entry.get('agent') not in ('claude', 'codex') for entry in (source, target)):
        raise ValueError('Review needs Claude or Codex tabs.')
    links = review_links.reviewed_by()
    if reviewer in links.get(worker, []):
        raise ValueError('That review link already exists.')
    pending, visited = [reviewer], set()
    while pending:
        pane = pending.pop()
        if pane == worker:
            raise ValueError('Those tabs cannot review each other.')
        if pane not in visited:
            visited.add(pane)
            pending.extend(links.get(pane, []))
    thread = target.get('thread')
    review_links.add(worker=worker, reviewer=reviewer,
                     reviewer_thread=thread if isinstance(thread, str) and UUID.fullmatch(thread) else None,
                     name=delegate.source_title(source), agent=source['agent'],
                     seen_working=bool(last_reply(source).strip()) and live_status(source)['state'] != 'Working')
    return {'linked': True}


def close_tab(body, caller, entries, run, status):
    """Close a tab this tab started or reviews, after its work is done. A working tab stays open."""
    pane = helper_tabs._pane(body)
    if pane == caller['id']:
        raise ValueError('A tab cannot close itself. Ask the tab that started it.')
    exists, record = helper_tabs.load(pane, run)
    if not exists:
        raise ValueError('That session has ended. Select another tab.')
    target = next((entry for entry in entries if entry['id'] == pane), None)
    if record is not None and record['owner_pane'] == caller['id']:
        helper_tabs.check(caller['id'], caller, target, record, run, pane=pane)
    elif caller['id'] not in review_links.reviewed_by().get(pane, []):
        raise ValueError('That tab is not a helper of this tab, and this tab does not review it.')
    if target is not None and status(target)['state'] in ('Working', 'Approval', 'Input needed'):
        raise RuntimeError('That tab is working or waits on a prompt. Close it after its work is done.')
    helper_close.close_pane(pane, run)
    return {'closed': pane}


@contextlib.contextmanager
def _start_group(body, caller, run, *, folder='', branch=None):
    """Yield the group id for a new tab ("" = Unstacked), under the auto stack rules (auto_stacks.py). It may make a
    group and move the caller into it; when the start fails, the caller goes back and an empty new group goes away.
    branch: the --worktree branch of a separate task."""
    state = {'made': None, 'moved': None}  # moved: the group id the caller moved into
    with groups.LOCK():
        try:
            session_id, current = groups.group_of_pane(caller['id'], run)
            group_id = _pick_group(body, caller, run, session_id, current, folder, branch, state)
        except BaseException:
            _undo_group(caller, run, state)
            raise
    try:
        yield group_id
    except BaseException:
        with groups.LOCK():
            _undo_group(caller, run, state)
        raise


def _pick_group(body, caller, run, session_id, current, folder, branch, state):
    if 'group' in body:
        found = next((g for g in groups.read(session_id, run)
                      if g['name'].lower() == groups.clean_name(body['group']).lower()), None)
        group = groups.find_or_create(session_id, body['group'], run)
        if group is None:
            return ''
        state['made'] = (session_id, group['id']) if found is None else None
        if current is None and branch is None and groups.auto_move(session_id, caller['id'], group['id'], run):
            state['moved'] = (session_id, group['id'])
        return group['id']
    if branch is not None:
        name = auto_stacks.task_name(body, branch, delegate.task_name(body.get('message') or ''))
        made = groups.create_unique(session_id, name, run)
        state['made'] = (session_id, made['id'])
        return made['id']
    if current is not None:
        return current['id']
    if groups.placed_by_user(caller['id'], run):
        return ''
    made = groups.create_unique(session_id, auto_stacks.goal_name(caller, folder), run)
    state['made'] = (session_id, made['id'])
    if groups.auto_move(session_id, caller['id'], made['id'], run):
        state['moved'] = (session_id, made['id'])
    return made['id']


def _undo_group(caller, run, state):
    """After a failed start: move the caller out of the group Tether moved it into, unless the user has moved it since,
    then drop the group Tether made if it is empty. Errors are ignored: the start error is the one to report."""
    try:
        if state['moved'] is not None and not groups.placed_by_user(caller['id'], run):
            session_id, group = groups.group_of_pane(caller['id'], run)
            if group is not None and (session_id, group['id']) == state['moved']:
                groups.move(session_id, caller['id'], '', run)
    except (groups.GroupError, OSError, subprocess.SubprocessError):
        pass
    try:
        if state['made'] is not None:
            groups.remove(*state['made'], run)
    except (groups.GroupError, OSError, subprocess.SubprocessError):
        pass

def serve(handler, body, *, sessions, folder, snapshot, last_reply, run, lock, status, send, timeline):
    credential = handler.headers.get('X-Tether-Hook', '')
    if (not auth.is_loopback(handler.client_address[0]) or not credential
            or not hmac.compare_digest(credential.encode(), auth.session_value().encode())):
        handler.respond(403, {'error': 'Local hook authentication required.'})
        return
    if body.get('action') in ('send', 'wait'):
        entries = sessions()
        caller = next((entry for entry in entries if entry['id'] == body.get('session')), None)
        try:
            if body['action'] == 'send':
                if caller is None:
                    raise ValueError('That session has ended. Select another tab.')
                result = helper_tabs.send_message(body, caller, entries, run, status, send, last_reply,
                                                  helper_tabs.SEND_LOCK)
            else:
                handler.keep_sessions = True
                result = 200, helper_tabs.poll(body, caller, entries, run, status, last_reply, timeline)
        except helper_tabs.Stale as exc:
            result = 409, {'error': str(exc)}
        handler.respond(*result)
        return
    if body.get('action') == 'start' and 'worktree' in body:
        entries = sessions()
        caller = next((entry for entry in entries if entry['id'] == body.get('session')), None)
        if caller is None:
            raise ValueError('That session has ended. Select another tab.')
        request = dict(body, thread=caller.get('thread'))
        request.setdefault('agent', caller['agent'] if caller.get('agent') in ('claude', 'codex') else 'claude')
        delegate.validate(request, entries)
        if 'group' in body:
            groups.clean_name(body['group'])  # before the worktree is made
        if 'cwd' in body and (not isinstance(body['cwd'], str) or not os.path.isabs(body['cwd'])
                              or not os.path.isdir(body['cwd'])):
            raise ValueError('Choose an absolute path to an existing directory.')
        directory = body['cwd'] if 'cwd' in body else folder(caller)
        directory = directory if directory and os.path.isdir(directory) else str(paths.HOME)
        made = worktrees.from_body(body, body.get('cwd', directory))
        with lock, worktrees.keeping(made):
            caller = delegate.validate(request, sessions())
            with _start_group(body, caller, run, folder=directory, branch=made['branch']) as group:
                pane, name = delegate.start(caller, request, directory, run=run,
                                            recent=lambda: snapshot(caller).get('messages') or [],
                                            cwd=made['path'], worktree=made['branch'],
                                            owner=helper_tabs.new_record(caller), group=group)
        handler.respond(201, {'pane': pane, 'name': name, 'worktree': made})
        return
    with lock:
        entries = sessions()
        caller = next((entry for entry in entries if entry['id'] == body.get('session')), None)
        if caller is None:
            raise ValueError('That session has ended. Select another tab.')
        action = body.get('action')
        if action == 'start':
            request = dict(body, thread=caller.get('thread'))
            request.setdefault('agent', caller['agent'] if caller.get('agent') in ('claude', 'codex') else 'claude')
            delegate.validate(request, entries)
            if 'cwd' in body and (not isinstance(body['cwd'], str) or not os.path.isabs(body['cwd'])
                                  or not os.path.isdir(body['cwd'])):
                raise ValueError('Choose an absolute path to an existing directory.')  # before a stack is made
            directory = folder(caller)
            with _start_group(body, caller, run, folder=directory) as group:
                pane, name = delegate.start(caller, request,
                                            directory if directory and os.path.isdir(directory) else str(paths.HOME),
                                            run=run, recent=lambda: snapshot(caller).get('messages') or [],
                                            cwd=body.get('cwd'), owner=helper_tabs.new_record(caller), group=group)
            result = 201, {'pane': pane, 'name': name}
        elif action == 'link':
            if caller['id'] not in (body.get('worker'), body.get('reviewer')):
                raise ValueError('That review link is not on this tab.')
            result = 200, link_pair(body, entries, last_reply)
        elif action == 'stop':
            worker, reviewer = review_pair(body)
            if caller['id'] != worker:
                if reviewer is None:
                    if caller['id'] not in review_links.reviewed_by().get(worker, []):
                        raise ValueError('That review link is not on this tab.')
                    reviewer = caller['id']
                elif reviewer != caller['id']:
                    raise ValueError('That review link is not on this tab.')
            result = 200, {'stopped': review_links.remove(worker, reviewer)}
        elif action == 'close':
            result = 200, close_tab(body, caller, entries, run, status)
        elif action == 'list':
            links = review_links.reviewed_by()
            by_pane = {entry['id']: entry for entry in entries}

            def row(pane):
                entry = by_pane.get(pane, {})
                return {'pane': pane, 'name': entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or ''}

            result = 200, {
                'reviewing': [row(worker) for worker, reviewers in links.items() if caller['id'] in reviewers],
                'reviewers': [row(reviewer) for reviewer in links.get(caller['id'], [])]}
        else:
            raise ValueError('Unknown action.')
    handler.respond(*result)
