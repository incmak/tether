"""Local agent commands share the browser's guarded tab launcher."""
import contextlib
import hmac
import os
import re
import subprocess

import agents
import audit
import auth
import auto_resume
import auto_stacks
import delegate
import groups
import helper_close
import helper_tabs
import paths
import procinfo
import review_links
import self_close
import undo
import undo_store
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


def link_pair(body, entries, last_reply, *, reset=False):
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
                     worker_thread=helper_tabs._thread(source), reset=reset,
                     seen_working=bool(last_reply(source).strip()) and live_status(source)['state'] != 'Working')
    return {'linked': True}


SHELLS = ('zsh', 'bash', 'sh', 'dash', 'fish', 'ksh', 'tcsh')


def _agent_exited(target, run):
    """Two sources agree that no agent runs in the pane: discovery found no agent process, and tmux shows a shell
    as the foreground command. Then no conversation is left to confirm."""
    if target is None or target.get('agent') != 'shell':
        return False
    try:
        result = run([paths.TMUX, 'display-message', '-p', '-t', target['id'], '#{pane_pid}\t#{pane_current_command}'],
                     capture_output=True, text=True, timeout=5)
        pane_pid, command = result.stdout.strip().split('\t')
        if result.returncode or not pane_pid.isdigit() or command not in SHELLS:
            return False
        # Discovery may be up to a second old: read the process tree again before a close.
        processes = procinfo.table(fresh=True)
        tree = agents.descendants(int(pane_pid), processes)
        registered = agents._pane_registries(agents.claude_registries(processes), target['id'], tree)
        return int(pane_pid) in processes and agents._pane_agent(int(pane_pid), processes, tree, registered)[0] is None
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def close_tab(body, caller, entries, run, status, sessions=None, lock=None):
    """Close a tab this tab started or reviews, after its work is done. A working tab stays open."""
    if body.get('self') is True:
        # The user asked this tab to close. fresh skips the status cache for the last check before the close.
        return self_close.schedule(caller, run, status, sessions=sessions, lock=lock,
                                   fresh=lambda entry: auto_resume.overlay(entry, live_status(entry)))
    pane = helper_tabs._pane(body)
    if pane == caller['id']:
        raise ValueError('To close this tab when the user asks, run `close --self`.')
    exists, record = helper_tabs.load(pane, run)
    if not exists:
        raise ValueError('That session has ended. Select another tab.')
    target = next((entry for entry in entries if entry['id'] == pane), None)
    owner = record is not None and record['owner_pane'] == caller['id']
    linked = set() if owner else review_links.linked_worker_threads(  # empty: no link
        pane, caller['id'], helper_tabs.verified_thread(caller))
    if not owner and not linked:
        raise ValueError('That tab is not a helper of this tab, and this tab does not review it.')
    exited = _agent_exited(target, run)
    if not exited and helper_tabs.verified_thread(target) is None:  # closing is final: only a confirmed conversation
        raise RuntimeError("Tether cannot confirm that helper's conversation right now. Try again in a moment.")
    if owner:
        helper_tabs.check(caller['id'], caller, target, record, run, pane=pane)
    elif not exited and linked - {None, helper_tabs.verified_thread(target)}:
        raise helper_tabs.Stale('That tab changed conversations since your review started.')
    if target is not None and status(target)['state'] in ('Working', 'Approval', 'Input needed'):
        raise RuntimeError('That tab is working or waits on a prompt. Close it after its work is done.')
    closed = undo.capture_safe(pane, run, entries)
    name = (closed or {}).get('name') or delegate.source_title(target or {}, fallback=pane)
    helper_close.close_pane(pane, run)
    audit.record('tab.close', f'Closed tab {name}', pane=pane)
    undo.record_tab_close(closed, run)
    return {'closed': pane}


def _start_home(body, caller, entries):
    """Resolve the placement owner. None means the new tab is a main tab."""
    if 'owner' not in body:
        return caller
    value = body['owner']
    if value == 'self':
        if delegate.normalize_review(body.get('review', False)) == 'new' or body.get('role'):
            raise ValueError('A reviewer or advisor needs an owner tab.')
        return None
    if not isinstance(value, str) or not helper_tabs.PANE.fullmatch(value):
        raise ValueError('Choose self or a pane ID like %12 for the owner.')
    if value == caller['id']:
        return caller
    home = next((entry for entry in entries if entry['id'] == value), None)
    session = caller.get('tmux_session')
    if (home is None or home.get('agent') not in ('claude', 'codex')
            or not session or home.get('tmux_session') != session):
        raise ValueError('Choose an owner tab in this tmux session.')
    return home


@contextlib.contextmanager
def _start_group(body, caller, run, entries=(), *, branch=None):
    """Yield the group ID, tab name and helper record for a new tab."""
    home = _start_home(body, caller, entries)
    token = session_id = None
    with groups.LOCK():
        session_id, group_id, stack_goal = _pick_group(body, caller, home, run)
        owner = _owner(caller, home, entries, run)
        if group_id:  # until the new tab has its @group, the stack must not look empty or root-only (groups.py)
            token = groups.start_begin(session_id, group_id, run)
    try:
        name = auto_stacks.helper_tab_name(role=body.get('role'), review=delegate.normalize_review(body.get('review', False)) == 'new',
                                           name=body.get('name'), branch=branch, stack=body.get('group'),
                                           message=body.get('message') or '', caller=caller, stack_goal=stack_goal)
        yield group_id, name, owner
    finally:
        _end_start(session_id, token, run)


def _end_start(session_id, token, run):
    try:
        if token is not None:
            groups.start_end(session_id, token, run)
    except (groups.GroupError, OSError, subprocess.SubprocessError):
        pass  # the marker expires (groups.START_LIMIT)


def _owner(caller, home, entries, run):
    """Record the opener's authority and the new tab's placement."""
    home_pane = home['id'] if home is not None else None
    root = root_thread = None
    if home_pane is not None:
        by_id = {entry['id']: entry for entry in entries}
        by_id.setdefault(caller['id'], caller)
        root = auto_stacks.root_of(home_pane, lambda pane: by_id.get(pane, {}).get('thread'), run)
        root_thread = helper_tabs._thread(by_id.get(root, {}))
    name = helper_tabs.visible_name(caller)
    return dict(helper_tabs.new_record(caller), root_pane=root, root_thread=root_thread,
                kind='helper' if home is not None else 'own',
                home_pane=home_pane, home_thread=helper_tabs.verified_thread(home),
                opener_name=name[:40] if name else None)


def _pick_group(body, caller, home, run):
    """(session ID, group ID, stack goal). Place a helper with its owner, or start a main tab Unstacked."""
    goal = body.get('group') if isinstance(body.get('group'), str) else None
    unstacked = goal is not None and groups.clean_name(goal).lower() in groups.RESERVED
    named = auto_stacks.goal(goal) if goal and not unstacked else None
    if home is None:
        return None, '', named
    session_id, current = groups.group_of_pane(home['id'], run)
    if home['id'] != caller['id'] and groups.group_of_pane(caller['id'], run)[0] != session_id:
        raise ValueError('Choose an owner tab in this tmux session.')
    if unstacked or current is None:
        return session_id, '', named
    return session_id, current['id'], named or current['name']


def serve(handler, body, **kwargs):
    """The agent's own steps (start, close) go in the undo history under its tab name; a failed start records
    nothing (undo_store.step)."""
    caller = next((entry for entry in kwargs['sessions']() if entry['id'] == body.get('session')), None) \
        if body.get('action') in ('start', 'close') else None
    if caller is None:
        return _serve(handler, body, **kwargs)
    name = caller.get('pane_name') or caller.get('agent_name') or caller.get('label') or ''
    with undo_store.acting('agent', name), undo_store.step():
        return _serve(handler, body, **kwargs)


def _serve(handler, body, *, sessions, folder, snapshot, last_reply, run, lock, status, send, timeline, fresh=None):
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
                # A prompt from another agent continues the worker's current review task (B189).
                pane = result[1]['pane']
                worker = next((entry for entry in entries if entry['id'] == pane), {})
                review_links.agent_prompt(pane, sender=caller['id'], sender_thread=helper_tabs.verified_thread(caller),
                                          worker_thread=helper_tabs._thread(worker) or '')
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
        _start_home(body, caller, entries)  # before the worktree is made
        if 'group' in body:
            groups.clean_name(body['group'])  # before the worktree is made
        if 'cwd' in body and (not isinstance(body['cwd'], str) or not os.path.isabs(body['cwd'])
                              or not os.path.isdir(body['cwd'])):
            raise ValueError('Choose an absolute path to an existing directory.')
        directory = body['cwd'] if 'cwd' in body else folder(caller)
        directory = directory if directory and os.path.isdir(directory) else str(paths.HOME)
        made = worktrees.from_body(body, body.get('cwd', directory))
        with lock, worktrees.keeping(made):
            fresh = sessions()
            caller = delegate.validate(request, fresh)
            with _start_group(body, caller, run, fresh, branch=made['branch']) as (group, title, owner):
                pane, name = delegate.start(caller, request, directory, run=run,
                                            recent=lambda: snapshot(caller).get('messages') or [],
                                            cwd=made['path'], worktree=made['branch'],
                                            owner=owner, group=group,
                                            name=title)
        audit.record('tab.start', f"Started tab {name} ({request['agent']})", pane=pane)
        undo.record_tab_start(pane, run)
        handler.respond(201, {'pane': pane, 'name': name, 'worktree': made})
        return
    with lock:
        # Closing is final: the caller's and the target's conversations come from uncached discovery.
        entries = (fresh or sessions)() if body.get('action') == 'close' else sessions()
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
            with _start_group(body, caller, run, entries) as (group, title, owner):
                pane, name = delegate.start(caller, request,
                                            directory if directory and os.path.isdir(directory) else str(paths.HOME),
                                            run=run, recent=lambda: snapshot(caller).get('messages') or [],
                                            cwd=body.get('cwd'), owner=owner,
                                            group=group, name=title)
            audit.record('tab.start', f"Started tab {name} ({request['agent']})", pane=pane)
            undo.record_tab_start(pane, run)
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
        elif action == 'escalate':
            worker = next((entry for entry in entries if entry['id'] == body.get('pane')), {})
            review_links.escalate(body.get('pane'), caller['id'], caller.get('thread'),
                                  worker_thread=helper_tabs._thread(worker) or '')
            result = 200, {'escalated': True}
        elif action == 'close':
            result = 200, close_tab(body, caller, entries, run, status, sessions, lock)
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
