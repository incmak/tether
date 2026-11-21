"""Plan and replay the one-time task-tree sort without changing stacks or opener authority."""
from collections import defaultdict, deque
from dataclasses import replace
import html
import json
import subprocess

import agents
import auto_resume
import coordinator
import helper_tabs
import paths
import projects
import task_tree


class Refused(ValueError):
    """The saved plan cannot be used with the live panes."""


def _matching(pane, thread, entries):
    entry = entries.get(pane)
    current = helper_tabs.verified_thread(entry)
    return entry if current and (not thread or current == thread) else None


def _proposal(pane, options, panes, entries):
    """Return the rule's candidate record and any deferred legacy placement."""
    old = options.record
    if old is None:
        return old, None, 'No opener record'
    if old.get('moved_by') == 'user' or options.group_by == 'user':
        return old, None, 'Placed by Mak'
    if (entries.get(pane) or {}).get('coordinator') in coordinator.MODES:
        return old, None, 'Coordinator stays its own task'
    if not helper_tabs.verified_thread(entries.get(pane)):
        return old, None, 'Conversation cannot be confirmed'

    name = helper_tabs.visible_name(entries[pane])
    opener = _matching(old['owner_pane'], old.get('owner_thread'), entries)
    if opener is None:
        opener = {'pane_name': old.get('opener_name')}
    kind, home = task_tree.classify(old, options.role, name, '', opener)
    deferred = (not options.role and old.get('kind') is None
                and not task_tree.BACKLOG_ID.search(name or ''))
    if deferred:
        # The shared classifier treats an unidentified legacy worker as separate. During migration it may follow
        # an owner that is itself a confirmed child of a task.
        _, home = task_tree.classify(dict(old, kind='helper'), options.role, name, '', opener)
    separate = old if old.get('kind') == 'own' else dict(old, kind='own', root_pane=pane,
                                                        root_thread=helper_tabs.verified_thread(entries[pane]))
    if separate is not old:
        for key in ('home_pane', 'home_thread'):
            separate.pop(key, None)
    if kind == 'separate' and not deferred:
        reason = 'Own task' if old.get('kind') == 'own' else 'Different backlog item'
        if old.get('kind') is None and not task_tree.BACKLOG_ID.search(name or ''):
            reason = 'Legacy worker without a backlog ID'
        return separate, None, reason

    bound = old.get('home_thread') if old.get('home_pane') else old.get('owner_thread')
    owner = _matching(home, bound, entries)
    if (owner is None or home == pane or home not in panes or panes[home].session != options.session):
        if deferred:
            return separate, None, 'Legacy worker without a backlog ID'
        return old, None, 'Placement owner cannot be confirmed'
    child = dict(old, kind='helper', home_pane=home, home_thread=helper_tabs.verified_thread(owner))
    if deferred:
        return separate, (home, child), 'Legacy worker without a backlog ID'
    return child, None, 'Review or advisor' if options.role in task_tree.ROLES else 'Works on the same task'


def plan(panes, entries):
    """Build a plan from a single read of all panes. Includes unchanged panes as staleness witnesses."""
    entries = {entry['id']: entry for entry in entries if entry['id'] in panes}
    projected, reasons, deferred = {}, {}, defaultdict(list)
    before = task_tree.placements(panes, entries)
    for pane, options in panes.items():
        record, follow, reasons[pane] = _proposal(pane, options, panes, entries)
        projected[pane] = replace(options, record=record)
        if follow:
            home, child = follow
            deferred[home].append((pane, child))
    # A no-ID worker can follow a child under the same task, including a chain of such workers.
    queue = deque(task_tree.placements(projected, entries))
    while queue:
        for pane, record in deferred.pop(queue.popleft(), ()):
            projected[pane] = replace(projected[pane], record=record)
            reasons[pane] = 'Follows a helper in the same task'
            queue.append(pane)
    after = task_tree.placements(projected, entries)
    for pane, options in projected.items():
        old, new = panes[pane].record, options.record
        if new != old and new.get('kind') == 'helper' and pane not in after:
            projected[pane] = panes[pane]
            reasons[pane] = 'Placement would form a loop or has no task'
    after = task_tree.placements(projected, entries)
    rows = []
    for pane, options in panes.items():
        old, new = options.record, projected[pane].record
        task = after.get(pane, {}).get('task')
        old_task = before.get(pane, {}).get('task')
        if task and new != old:
            new = dict(new, root_pane=task, root_thread=helper_tabs.verified_thread(entries.get(task)))
        row = _row(pane, options, entries.get(pane))
        row.update(old=old, new=new, before=old_task, after=task, reason=reasons[pane])
        rows.append(row)
    return rows


def _row(pane, options, entry):
    entry = entry or {}
    return {'pane': pane, 'thread': helper_tabs.verified_thread(entry), 'observed_thread': entry.get('thread'),
            'process': [entry.get('owner_pid'), entry.get('process_start'), entry.get('pane_pid'), entry.get('pane_start')],
            'agent': entry.get('agent'), 'name': helper_tabs.visible_name(entry) or pane,
            'session': options.session, 'role': options.role, 'group_by': options.group_by, 'raw': options.raw}


def _server(run):
    kind, identifier = auto_resume.server_id(run=run)
    if kind != 'up':
        raise Refused('Could not confirm the tmux server. Open tmux before running the sort.')
    return identifier


def _snapshot(run):
    server = _server(run)
    # Normal discovery can restore an abandoned private view. A sort snapshot must read only.
    entries = agents.discover({'thread': None}, run, repair_zoom=False)
    panes = helper_tabs.read_panes(run)
    if server != _server(run):
        raise Refused('The tmux server restarted while reading the panes. Make a new plan.')
    return server, panes, entries


def _report(rows):
    names = {row['pane']: row['name'] for row in rows}

    def title(pane):
        safe = html.escape(names[pane]).replace('\\', '\\\\').replace('|', r'\|')
        return ' '.join(safe.split()) + f' ({pane})'

    def place(pane):
        return 'Child of ' + title(pane) if pane else 'Task'

    lines = ['# Task tree sort', '', '| Tab | Now | After | Reason |', '| --- | --- | --- | --- |']
    for row in rows:
        if row['old'] is not None:
            lines.append(f"| {title(row['pane'])} | {place(row['before'])} | {place(row['after'])} | {row['reason']} |")
    changed = sum(row['old'] != row['new'] for row in rows)
    moved = sum(row['before'] != row['after'] for row in rows)
    lines += ['', f'{len(rows)} panes checked. {changed} records to update. {moved} tabs change tasks.', '']
    return '\n'.join(lines)


def _json(path, document):
    projects._write(path, json.dumps(document, ensure_ascii=True, indent=2, allow_nan=False) + '\n')


def save_plan(folder, *, run=subprocess.run):
    server, panes, entries = _snapshot(run)
    rows = plan(panes, entries)
    files = [folder / name for name in ('move-list.md', 'plan.json', 'undo.json')]
    if any(file.exists() or file.is_symlink() for file in files):
        raise Refused('This folder already contains a sort plan. Choose a new output folder.')
    document = {'version': 1, 'server': server, 'panes': rows}
    _json(folder / 'undo.json', document)
    _json(folder / 'plan.json', document)
    projects._write(folder / 'move-list.md', _report(rows))
    return sum(row['old'] != row['new'] for row in rows)


IDENTITY = ('thread', 'observed_thread', 'process', 'agent', 'session')
PLACEMENT = ('name', 'role', 'group_by')
FIELDS = ('kind', 'home_pane', 'home_thread', 'root_pane', 'root_thread', 'moved_by')


def _placement(raw):
    if not helper_tabs._record(raw):
        return (raw,) if raw else ()
    record = json.loads(raw)
    return {key: record[key] for key in FIELDS if key in record}


def _merge(raw, target):
    result = json.loads(raw)
    for key in FIELDS:
        result.pop(key, None)
    result.update(_placement(target))
    return target if result == json.loads(target) else json.dumps(result, separators=(',', ':'), allow_nan=False)



def _document(path):
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
        rows = document['panes']
        if (document['version'] != 1 or not isinstance(document['server'], str) or not isinstance(rows, list)
                or any(not helper_tabs.PANE.fullmatch(row['pane'])
                       or not isinstance(row['raw'], str)
                       or ('old' not in row or 'new' not in row)
                       or not all(key in row for key in (*IDENTITY, *PLACEMENT))
                       or (row['old'] != row['new'] and (not row['thread'] or not isinstance(row['new'], dict)
                                                       or not isinstance(row['old'], dict)))
                       for row in rows)
                or len({row['pane'] for row in rows}) != len(rows)):
            raise ValueError('Bad sort document')
        return document
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise Refused(f'Could not read {path.name}. Use a folder made by the sort plan command.') from exc


def _check(server, rows, expected, snapshot):
    current_server, panes, entries = snapshot
    if server != current_server:
        raise Refused('The tmux server restarted since the plan. Make a new plan.')
    entries = {entry['id']: entry for entry in entries}
    stale = []
    for row in rows:
        pane = row['pane']
        if pane not in panes:
            stale.append(f'{pane} ended')
            continue
        current = _row(pane, panes[pane], entries.get(pane))
        if any(row[key] != current[key] for key in IDENTITY):
            stale.append(f'{pane} changed conversations or sessions')
        elif _placement(current['raw']) != _placement(expected[pane]):
            stale.append(f'{pane} changed its helper record')
        elif any(row[key] != current[key] for key in PLACEMENT):
            stale.append(f'{pane} changed its name or placement')
    stale.extend(f'{pane} opened' for pane in panes.keys() - expected.keys())
    if stale:
        raise Refused('The sort plan is stale: ' + ', '.join(stale) + '.')


def _confirm_record(server, row, expected, run):
    pane = row['pane']
    fmt = '#{pid}:#{start_time}\t#{pane_id}\t#{session_id}\t#{@tether_helper}'
    result = run([paths.TMUX, 'display-message', '-p', '-t', pane, fmt],
                 capture_output=True, text=True, timeout=5)
    fields = result.stdout.rstrip('\n').split('\t', 3)
    if (result.returncode or len(fields) != 4 or fields[:3] != [server, pane, row['session']]
            or _placement(fields[3]) != _placement(expected)):
        raise Refused(f'The sort plan is stale: {pane} changed its session or helper record.')
    return True, fields[3]


def _write(server, row, before, after, run):
    helper_tabs.write_record(row['pane'], before, after, run,
                             read=lambda _, runner: _confirm_record(server, row, before, runner))


def _restore(server, applied, run):
    """Restore only our own writes and only while the recorded conversation still matches."""
    try:
        current_server, panes, entries = _snapshot(run)
    except (OSError, subprocess.SubprocessError, Refused):
        return [row['pane'] for row, _, _ in applied]
    entries = {entry['id']: entry for entry in entries}
    failed = []
    for row, old, new in reversed(applied):
        pane = row['pane']
        current = _row(pane, panes[pane], entries.get(pane)) if pane in panes else {}
        if server != current_server or any(row[key] != current.get(key) for key in IDENTITY):
            failed.append(pane)
            continue
        if _placement(current.get('raw', '')) == _placement(old):
            continue  # a failed tmux call can leave the option unchanged
        try:
            _write(server, row, new, old, run)
        except (OSError, subprocess.SubprocessError, Refused, helper_tabs.Stale):
            failed.append(pane)
    return failed


def replay(folder, *, undo=False, run=subprocess.run):
    """Preflight the entire plan, then apply it or restore its saved records."""
    with helper_tabs.record_lock():
        return _replay(folder, undo, run)


def _replay(folder, undo, run):
    document = _document(folder / ('undo.json' if undo else 'plan.json'))
    rows, server = document['panes'], document['server']
    expected, target, changed = {}, {}, []
    for row in rows:
        pane, old = row['pane'], row['raw']
        new = (_merge(old, json.dumps(row['new'], separators=(',', ':'), allow_nan=False))
               if row['old'] != row['new'] else old)
        expected[pane], target[pane] = (new, old) if undo else (old, new)
        if new != old:
            changed.append(row)
    _check(server, rows, expected, _snapshot(run))
    if not undo:
        _json(folder / 'undo.json', document)
    applied = []
    try:
        for row in changed:
            pane = row['pane']
            # Conversation IDs and helper records can change while the CLI runs. Recheck before each write.
            _check(server, rows, expected, _snapshot(run))
            applied.append((row, expected[pane], target[pane]))
            _write(server, row, expected[pane], target[pane], run)
            expected[pane] = target[pane]
    except (OSError, subprocess.SubprocessError, Refused, helper_tabs.Stale, KeyboardInterrupt) as exc:
        failed = _restore(server, applied, run) if applied else []
        reason = str(exc) if isinstance(exc, (Refused, helper_tabs.Stale)) else f'Tmux could not write pane {pane}.'
        if isinstance(exc, KeyboardInterrupt):
            reason = 'The sort was interrupted.'
        if failed:
            reason += ' Could not restore the record for ' + ', '.join(failed) + '. The old records are in undo.json.'
        elif applied:
            reason += ' Restored the records written by this command.'
        raise Refused(reason) from exc
    return len(applied)
