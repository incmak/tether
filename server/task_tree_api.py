"""Pane-bound task tree routes and whole-task closing."""
from contextlib import nullcontext
import json
import os
import subprocess
from urllib.parse import quote
import time

import audit
import auto_resume
import groups
import helper_close
import helper_tabs
import task_tree
import undo
import undo_store
from terminal_input import UUID

PREFIX = '/mobile/api/task-tree/'
PLAN = PREFIX + 'close-plan'
CLOSE_CHANGED = 'The helpers changed. Review the list again.'
WRITES = {PREFIX + name for name in ('move', 'done', 'close', 'answer')}


def pane_id(pane):
    if not isinstance(pane, str) or not helper_tabs.PANE.fullmatch(pane):
        raise ValueError('Select a live chat tab.')
    return pane


class Tree:
    def __init__(self, entries, run, status, ask, escalated, *, fresh_entries=None):
        self.entries = {entry['id']: entry for entry in entries}
        self.options = helper_tabs.read_panes(run)
        self.fields = task_tree.placements(self.options, self.entries)
        self.run = run
        self.fresh_entries = fresh_entries
        self.status = status
        self.ask = ask
        self.escalated = escalated(entries)

    def entry(self, pane):
        pane_id(pane)
        entry, options = self.entries.get(pane), self.options.get(pane)
        if not entry or not options or options.session != entry.get('tmux_session'):
            raise helper_tabs.Stale('That tab moved or closed. Reload the task tree.')
        return entry

    def bound(self, pane, thread):
        entry = self.entry(pane)
        if entry.get('agent') == 'shell':
            raise ValueError('Choose a tab with a running chat.')
        if not isinstance(thread, str) or not UUID.fullmatch(thread) or helper_tabs.verified_thread(entry) != thread:
            raise helper_tabs.Stale('That tab changed conversations or is still starting. Reload the task tree.')
        return entry

    def record(self, pane):
        row = self.options[pane]
        if row.raw and row.record is None:
            raise helper_tabs.Stale('The placement record cannot be read. Reload the task tree.')
        return row.record

    def status_row(self, pane):
        entry = self.entries[pane]
        return dict(self.status(entry), ask=self.ask(pane, entry.get('thread')), escalated=pane in self.escalated)

    def children(self, pane):
        return [child for child, fields in self.fields.items() if fields['task'] == pane]

    def root(self, pane):
        entry = self.entry(pane)
        if pane in self.fields or task_tree.placement(pane, self.options, self.entries):
            raise ValueError('Choose the task tab above this helper.')
        if entry.get('agent') == 'shell':
            raise ValueError('Choose a tab with a running chat.')
        return entry

    def check_records(self, panes, root=None):
        entries = self.entries if self.fresh_entries is None else {entry['id']: entry for entry in self.fresh_entries()}
        current = helper_tabs.read_panes(self.run)
        for pane in panes:
            before, after = self.options[pane], current.get(pane)
            if (after is None or (after.raw, after.role, after.group_by, after.session)
                    != (before.raw, before.role, before.group_by, before.session)
                    or helper_tabs.verified_thread(entries.get(pane)) != helper_tabs.verified_thread(self.entries[pane])):
                raise helper_tabs.Stale('A tab or its conversation changed. Reload the task tree.')
        if root is not None:
            fields = task_tree.placements(current, entries)
            children = {child for child, field in fields.items() if field['task'] == root}
            if children != set(self.children(root)) or root in task_tree.pending_roots(current, entries, fields):
                raise helper_tabs.Stale('The task’s helpers changed. Open the close dialog again.')


def moved_record(entry, record, home=None):
    result = dict(record or helper_tabs.new_record(entry), moved_by='user')
    result.pop('root_thread', None)
    if home is None:
        for key in ('home_pane', 'home_thread', 'root_pane'):
            result.pop(key, None)
        result['kind'] = 'own'
    else:
        thread = helper_tabs.verified_thread(home)
        result.update(kind='helper', home_pane=home['id'], home_thread=thread,
                      root_pane=home['id'], root_thread=thread)
    return result


def move(body, tree):
    pane = body.get('pane')
    entry = tree.bound(pane, body.get('thread'))
    if 'into' not in body:
        raise ValueError('Choose a destination task or move the tab out.')
    into = body['into']
    home = None
    if into is not None:
        if pane_id(into) == pane:
            raise ValueError('A tab cannot move into itself.')
        home = tree.bound(into, body.get('into_thread'))
        if tree.options[pane].session != tree.options[into].session:
            raise ValueError('Choose a task in the same tmux session.')
        tree.root(into)
        if (any(task_tree.placement(child, tree.options, tree.entries) == pane for child in tree.entries)
                or pane in task_tree.pending_roots(tree.options, tree.entries, tree.fields)):
            raise ValueError('Move this tab’s helpers out before moving it into another task.')
    elif body.get('into_thread') is not None:
        raise ValueError('Move out without a destination conversation.')
    old = tree.record(pane)
    new = moved_record(entry, old, home)
    tree.check_records([pane] if home is None else [pane, into])
    helper_tabs.write_record(pane, new, tree.run)
    undo.record_task_tree_move(entry, old, new)
    audit.record('tab.move', f'Moved tab {name(entry)}' + (f' into {name(home)}' if home else ' out of its task'), pane=pane)
    return {'ok': True}


def done(body, tree):
    pane = body.get('pane')
    entry = tree.bound(pane, body.get('thread'))
    tree.root(pane)
    mark = body.get('done')
    if type(mark) is not bool:
        raise ValueError('Choose Mark done or Reopen.')
    thread = helper_tabs.verified_thread(entry)
    if mark:
        task_tree.set_marks(pane, tree.run, done=thread, done_at=time.time())
    else:
        task_tree.set_marks(pane, tree.run, done=None, done_at=None, reopened=thread)
    return {'ok': True}


def name(entry):
    return helper_tabs.visible_name(entry) or entry['id']


def close_plan(pane, tree):
    root = tree.root(pane)
    plan = {'task': {'pane': pane, 'name': name(root)}, 'idle': [], 'working': []}
    for child in tree.children(pane):
        entry = tree.entries[child]
        state = task_tree.state(tree.status_row(child))
        plan['idle' if state == 'idle' else 'working'].append({
            'pane': child, 'thread': helper_tabs.verified_thread(entry), 'name': name(entry),
            'role': tree.fields[child]['task_role'], 'state': state})
    signature = [pane, helper_tabs.verified_thread(root), sorted(
        (child['pane'], child['thread'], bucket)
        for bucket in ('idle', 'working') for child in plan[bucket])]
    plan['plan'] = helper_tabs.digest(json.dumps(signature))
    return plan


def close(body, tree):
    pane = body.get('pane')
    try:
        tree.bound(pane, body.get('thread'))
        plan = close_plan(pane, tree)
    except helper_tabs.Stale as exc:
        raise helper_tabs.Stale(CLOSE_CHANGED) from exc

    def ids(key):
        values = body.get(key)
        if not isinstance(values, list):
            raise helper_tabs.Stale(CLOSE_CHANGED)
        found = set(map(pane_id, values))
        if len(found) != len(values):
            raise ValueError('Select each helper once.')
        return found

    idle, also = ids('close_idle'), ids('close_also')
    if (body.get('plan') != plan['plan'] or idle != {child['pane'] for child in plan['idle']}
            or not also <= {child['pane'] for child in plan['working']}):
        raise helper_tabs.Stale(CLOSE_CHANGED)

    def recheck():
        if close_plan(pane, tree)['plan'] != plan['plan']:
            raise helper_tabs.Stale(CLOSE_CHANGED)

    try:
        return close_task(pane, tree, idle | also, recheck=recheck)
    except helper_tabs.Stale as exc:
        raise helper_tabs.Stale(CLOSE_CHANGED) from exc


class CloseInterrupted(RuntimeError):
    def __init__(self, error, members, identifier, label, *, stale=False):
        super().__init__(error)
        self.status = 409 if stale else 503
        self.members = members
        self.undo = identifier
        self.label = label


def kill_outcome(pane, server, run):
    """After a transport failure, check whether tmux applied the kill."""
    try:
        live = helper_tabs.read_panes(run)
        kind, current = auto_resume.server_id(run=run)
        if kind == 'none':
            return True
        if kind == 'up' and current == server:
            return pane not in live
    except (OSError, subprocess.SubprocessError):
        if auto_resume.server_id(run=run)[0] == 'none':
            return True
    return None


def close_task(pane, tree, close_children, *, recheck=None, before_kill=None):
    """Capture Undo first. Detach retained children while their task is still alive."""
    children = tree.children(pane)
    if pane in task_tree.pending_roots(tree.options, tree.entries, tree.fields):
        raise helper_tabs.Stale('A helper is still starting. Open the close dialog again when it appears.')
    keep = [child for child in children if child not in close_children]
    def depth(child):
        count = 0
        while child and child != pane:
            count += 1
            child = task_tree.placement(child, tree.options, tree.entries)
        return count

    order = sorted((child for child in children if child in close_children), key=depth, reverse=True) + [pane]
    before = {member: tree.record(member) for member in [*order, *keep]}
    entries = {member: tree.bound(member, helper_tabs.verified_thread(tree.entries[member]))
               for member in before}
    snapshots = {}
    for member in order:
        entry = entries[member]
        saved = undo.capture(member, tree.run, entry)
        if (not saved or saved['agent'] not in ('claude', 'codex')
                or saved['thread'] != helper_tabs.verified_thread(entry)
                or not saved.get('server') or not os.path.isdir(saved['folder'])
                or not saved.get('transcript') or not os.path.isfile(saved['transcript'])):
            raise ValueError(f'Tether cannot capture an undo for {name(entry)}. Open that tab before closing it.')
        snapshots[member] = saved
    tree.check_records(before, root=pane)
    if recheck is not None:
        recheck()
    killed, detached = [], []
    label = f'Closed task {name(entries[pane])}'
    identifier = None
    server = snapshots[pane].get('server')
    pending = None

    def journal():
        nonlocal identifier
        actions = []
        if killed or pending:
            actions.append(dict(kind='task-tree.close', closed=killed + ([pending] if pending else []),
                                kept=detached))
        else:
            actions.extend(dict(kind='task-tree.move', **change) for change in detached)
        identifier = undo_store.checkpoint(identifier, label, actions, server=server)

    try:
        for child in keep:
            entry = entries[child]
            own = moved_record(entry, before[child])
            detached.append(undo._task_tree_change(entry, before[child], own))
            helper_tabs.write_record(child, own, tree.run)
        if keep and recheck:
            recheck()
        for member in order:
            if before_kill:
                before_kill(member)
            pending = dict(tab=snapshots[member], record=before[member], pending=True)
            try:
                journal()
            except (OSError, ValueError):
                pending = None
                raise
            try:
                helper_close.close_pane(member, tree.run)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
                outcome = kill_outcome(member, server, tree.run)
                if outcome is not None:
                    pending = None
                if outcome:
                    killed.append(dict(tab=snapshots[member], record=before[member]))
                    helper_close.release_reviews(member)
                raise
            killed.append(dict(tab=snapshots[member], record=before[member]))
            pending = None
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        if not killed and pending is None:
            for change in detached[:]:
                try:
                    helper_tabs.write_record(change['pane'], change['before'], tree.run)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    continue
                detached.remove(change)
        if not (killed or detached or pending or identifier) and isinstance(exc, helper_tabs.Stale):
            raise
        try:
            for member in killed:
                undo.note_released(member['tab'], tree.run)
            journal()
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            pass  # the staged kill already has a durable record
        closed = [item['tab']['pane'] for item in killed]
        error = ('The task close only partly completed. Reload the task tree.' if killed or detached or pending
                 else 'Tether could not close the task. Reload the task tree.')
        raise CloseInterrupted(error, [(item, name(entries[item])) for item in closed],
                               identifier, label, stale=isinstance(exc, helper_tabs.Stale)) from exc
    try:
        for member in killed:
            undo.note_released(member['tab'], tree.run)
        journal()
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        raise CloseInterrupted('The tabs closed. Tether could not finish saving Undo.',
                               [(item, name(entries[item])) for item in order], identifier, label) from exc
    audit.record('tab.close', label, pane=pane, closed=order, kept=keep)
    return {'ok': True, 'closed': order, 'kept': keep, **({'undo': identifier} if identifier else {})}


def get(handler, pane, tree_factory):
    try:
        pane_id(pane)
        handler.respond(200, close_plan(pane, tree_factory()))
    except helper_tabs.Stale as exc:
        handler.respond(409, {'error': str(exc)})
    except ValueError as exc:
        handler.respond(400, {'error': str(exc)})
    except (OSError, RuntimeError, subprocess.SubprocessError):
        handler.respond(503, {'error': 'Tether could not read the task tree. Try again.'})


def post(handler, body, tree_factory, lock, answer, changed):
    try:
        action = handler.path.rsplit('/', 1)[1]
        stack_lock = groups.LOCK() if action == 'close' else nullcontext()
        record_lock = helper_tabs.record_lock() if action in ('close', 'move') else nullcontext()
        with helper_tabs.SEND_LOCK, lock, stack_lock, record_lock:
            tree = tree_factory()
            result = {'move': move, 'done': done, 'close': close, 'answer': answer}[action](body, tree)
        step = undo_store.flush()
        if step:
            result['undo'] = step['id']
        if action == 'close' and result.get('undo'):
            handler.respond(200, result, headers={'X-Tether-Undo': result['undo'],
                                                 'X-Tether-Undo-Label': quote(f'Closed task {name(tree.entries[body["pane"]])}', safe='')})
        else:
            handler.respond(200, result)
    except CloseInterrupted as exc:
        changed()
        result = {'error': str(exc), 'closed': [title for _, title in exc.members]}
        if exc.undo:
            result['undo'] = exc.undo
            handler.respond(exc.status, result, headers={'X-Tether-Undo': exc.undo,
                                                        'X-Tether-Undo-Label': quote(exc.label, safe='')})
        else:
            handler.respond(exc.status, result)
    except helper_tabs.Stale as exc:
        handler.respond(409, {'error': str(exc)})
    except groups.GroupError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except ValueError as exc:
        handler.respond(400, {'error': str(exc)})
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if action == 'close':
            changed()  # even if only part of the close reached tmux
        step = undo_store.flush()
        error = str(exc) if isinstance(exc, RuntimeError) else 'Tether could not complete the task change. Reload its tabs.'
        handler.respond(503, {'error': error, **({'undo': step['id']} if step else {})})
