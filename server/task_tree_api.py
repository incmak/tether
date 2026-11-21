"""Pane-bound task tree routes and whole-task closing."""
from contextlib import nullcontext
import json
import os
import subprocess
from urllib.parse import quote
import time

import audit
import auto_resume
import coordinator
import groups
import helper_close
import helper_tabs
import task_tree
import undo
import undo_store
from terminal_input import UUID
from undo import CloseInterrupted

PREFIX = '/mobile/api/task-tree/'
PLAN = PREFIX + 'close-plan'
CLOSE_CHANGED = helper_close.CLOSE_CHANGED
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
        self.escalated_of = escalated
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

    def fresh_status_row(self, entry):
        pane = entry['id']
        return dict(self.status(entry), ask=self.ask(pane, entry.get('thread')),
                    escalated=pane in self.escalated_of([entry]))

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
        if entry.get('coordinator') in coordinator.MODES:
            # A helper cannot be the coordinator (coordinator.browser_role), so the move would silently end the role.
            raise ValueError('This tab is the coordinator. Release the coordinator role before you move it into a task.')
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
    helper_tabs.write_record(pane, old, new, tree.run)
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
    return prepare_close(pane, tree)[0]


def _capture_close(member, entry, run, server):
    if entry.get('agent') not in ('claude', 'codex'):
        return None
    try:
        saved = undo.capture(member, run, entry)
        if (saved and saved['agent'] == entry['agent'] and saved['thread'] == helper_tabs.verified_thread(entry)
                and saved.get('server') == server and os.path.isdir(saved['folder'])
                and saved.get('transcript') and os.path.isfile(saved['transcript'])):
            return saved
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        pass  # The confirmation must say when a tab cannot be reopened.
    return None


def prepare_close(pane, tree):
    """Read the confirmation and keep its Undo captures for the write that uses them."""
    root = tree.root(pane)
    kind, server = auto_resume.server_id(run=tree.run)
    if kind != 'up':
        raise helper_tabs.Stale(CLOSE_CHANGED)
    plan = {'task': {'pane': pane, 'name': name(root), 'state': task_tree.state(tree.status_row(pane))},
            'idle': [], 'working': []}
    for child in tree.children(pane):
        entry = tree.entries[child]
        state = task_tree.state(tree.status_row(child))
        plan['idle' if state == 'idle' else 'working'].append({
            'pane': child, 'thread': helper_tabs.verified_thread(entry), 'name': name(entry),
            'role': tree.fields[child]['task_role'], 'state': state})
    targets = [plan['task'], *plan['idle'], *plan['working']]
    captured = {}
    for item in targets:
        member = item['pane']
        saved = _capture_close(member, tree.entries[member], tree.run, server)
        item['undo'] = saved is not None
        if saved is not None:
            captured[member] = saved
    signature = [server, pane, sorted(
        (helper_close.close_identity(tree.entries[item['pane']]), item['state'] == 'idle', item['undo'])
        for item in targets)]
    plan['plan'] = helper_tabs.digest(json.dumps(signature))
    return plan, captured, server


def close(body, tree):
    pane = body.get('pane')
    try:
        tree.bound(pane, body.get('thread'))
        plan, captured, server = prepare_close(pane, tree)
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
    if (body.get('plan') != plan['plan'] or idle != {child['pane'] for child in plan['idle'] if child['undo']}
            or not also <= {child['pane'] for child in [*plan['idle'], *plan['working']] if child['pane'] not in idle}):
        raise helper_tabs.Stale(CLOSE_CHANGED)

    try:
        return close_task(pane, tree, idle | also, plan, captured, server)
    except helper_tabs.Stale as exc:
        raise helper_tabs.Stale(CLOSE_CHANGED) from exc


def interrupted_response(handler, interruption):
    result = {'error': str(interruption), 'closed': [title for _, title in interruption.members]}
    if interruption.unknown:
        result['unknown'] = list(interruption.unknown)
    if interruption.without_undo:
        result['closed_without_undo'] = list(interruption.without_undo)
    if interruption.undo:
        result['undo'] = interruption.undo
        handler.respond(interruption.status, result, headers={'X-Tether-Undo': interruption.undo,
                        'X-Tether-Undo-Label': quote(interruption.label, safe='')})
    else:
        handler.respond(interruption.status, result)


def close_task(pane, tree, close_children, plan, snapshots, server, *, recheck=None):
    """Capture Undo first. Detach retained children while their task is still alive."""
    children = tree.children(pane)
    if pane in task_tree.pending_roots(tree.options, tree.entries, tree.fields):
        raise helper_tabs.Stale('A helper is still starting. Open the close dialog again when it appears.')
    keep = [child for child in children if child not in close_children]

    def ancestors(child):
        while child and child != pane:
            child = task_tree.placement(child, tree.options, tree.entries)
            if child:
                yield child

    order = sorted((child for child in children if child in close_children),
                   key=lambda child: sum(1 for _ in ancestors(child)), reverse=True) + [pane]
    before = {member: tree.record(member) for member in [*order, *keep]}
    entries = {member: tree.bound(member, helper_tabs.verified_thread(tree.entries[member])) for member in before}
    identities = {member: helper_close.close_identity(entry) for member, entry in entries.items()}
    buckets = {item['pane']: 'idle' if item['state'] == 'idle' else 'working'
               for item in [plan['task'], *plan['idle'], *plan['working']]}
    tree.check_records(before, root=pane)
    if any(helper_close.close_bucket(tree.fresh_status_row(entry)) != buckets[member]
           for member, entry in entries.items()):
        raise helper_tabs.Stale(CLOSE_CHANGED)
    if recheck is not None:
        recheck(())

    detached = []
    label = f'Closed task {name(entries[pane])}'
    journal = undo.CloseJournal(label, server)
    killed = journal.closed

    def actions():
        pending = journal.pending
        # An Undo must leave a tab separate when its former task (or parent helper) cannot reopen.
        lost = {member for member in killed if member not in snapshots}
        if pending and pending not in snapshots:
            lost.add(pending)
        closed = []
        for member in killed + ([pending] if pending else []):
            if member in snapshots:
                record = before[member]
                if lost.intersection(ancestors(member)):
                    record = moved_record(entries[member], record)
                closed.append(dict(tab=snapshots[member], record=record,
                                   **({'pending': True} if member == pending else {})))
        kept = [change for change in detached if not lost.intersection(ancestors(change['pane']))]
        return ([dict(kind='task-tree.close', closed=closed, kept=kept)] if closed
                else [dict(kind='task-tree.move', **change) for change in kept])

    def interruption(error, *, stale=False):
        return CloseInterrupted(error, [(member, name(entries[member])) for member in killed],
                                journal.identifier, label, stale=stale,
                                unknown=[name(entries[journal.pending])] if journal.pending else [],
                                without_undo=[name(entries[member]) for member in killed if member not in snapshots])

    def finish():
        for member in killed:
            if member in snapshots:
                undo.note_released(snapshots[member], tree.run)
        journal.save(actions())

    try:
        for child in keep:
            entry = entries[child]
            own = moved_record(entry, before[child])
            helper_tabs.write_record(child, before[child], own, tree.run)
            detached.append(undo._task_tree_change(entry, before[child], own))
        for member in order:
            def close_member():
                helper_close.close_matching_pane(member, tree.run, expected=identities[member],
                                                bucket=buckets[member], server=server,
                                                fresh=tree.fresh_entries or tree.entries.values,
                                                status=tree.fresh_status_row,
                                                recheck=(lambda: recheck(killed)) if recheck else None)

            def recover():
                auto_resume.forget_pane(member, run=tree.run)
                helper_close.release_reviews(member)

            journal.kill(member, tree.run, close_member, actions, recover)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        if not killed and journal.pending is None:
            for change in detached[:]:
                try:
                    helper_tabs.write_record(change['pane'], change['after'], change['before'], tree.run)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    continue
                detached.remove(change)
        try:
            finish()
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            pass  # the staged kill already has a durable record when it can be undone
        stale = isinstance(exc, helper_tabs.Stale)
        error = ((str(exc) if recheck else CLOSE_CHANGED) if stale else
                 'The task close only partly completed. Reload the task tree.'
                 if killed or detached or journal.pending else 'Tether could not close the task. Reload the task tree.')
        raise interruption(error, stale=stale) from exc
    try:
        finish()
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        raise interruption('The tabs closed. Tether could not finish saving Undo.') from exc
    audit.record('tab.close', label, pane=pane, closed=order, kept=keep)
    result = {'ok': True, 'closed': order, 'kept': keep}
    if journal.identifier:
        result['undo'] = journal.identifier
    if without_undo := [name(entries[member]) for member in killed if member not in snapshots]:
        result['closed_without_undo'] = without_undo
    return result


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
        with lock, stack_lock, record_lock:
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
        interrupted_response(handler, exc)
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
