"""Task placement and Done rules shared by the listing, writes, and sort pass."""
import re

import helper_tabs
import injected_prompts
import paths

BACKLOG_ID = re.compile(r'\bB\d{1,5}\b')
ROLES = {'reviewer': 'Review', 'advisor': 'Advisor', 'second-advisor': 'Second advisor'}
DONE = '@tether_done'
REOPENED = '@tether_reopened'
DONE_AT = '@tether_done_at'
CLOSE_AFTER = 24 * 3600


def classify(record, role, name, message, opener):
    """Classify a launch or a record in the sort pass. The opener is a discovery entry."""
    home = record.get('home_pane') or record.get('owner_pane')
    if record.get('moved_by') == 'user' or record.get('group_by') == 'user':
        return ('child', home) if record.get('kind') == 'helper' and record.get('moved_by') == 'user' else ('separate', None)
    if role in ROLES:
        return 'child', home
    ids = BACKLOG_ID.findall(name or '') + BACKLOG_ID.findall((message or '').partition('\n')[0])
    owner_ids = set(BACKLOG_ID.findall(helper_tabs.visible_name(opener or {}) or ''))
    if record.get('kind') == 'own' or (ids and (len(owner_ids) != 1 or set(ids) != owner_ids)):
        return 'separate', None
    if not ids and record.get('kind') is None:
        return 'separate', None  # a legacy worker with no evidence of a shared task
    return 'child', home


def is_child(record, role, group_by):
    """Read an existing placement. Legacy workers wait for the explicit sort pass."""
    return bool(record and record.get('kind') != 'own'
                and (group_by != 'user' or record.get('moved_by') == 'user')
                and (record.get('kind') == 'helper' or role in ROLES))


def placement(pane, rows, entries):
    """Return the immediate owner of a child with a live, matching placement."""
    options = rows.get(pane)
    if options is None or not is_child(options.record, options.role, options.group_by):
        return None
    home, bound = helper_tabs.placement_owner(options.record)
    entry, owner = entries.get(pane), entries.get(home)
    if (not entry or not owner or home == pane or owner.get('agent') == 'shell'
            or entry.get('tmux_session') != owner.get('tmux_session')
            or (bound and helper_tabs._thread(owner) != bound)):
        return None
    return home


def placements(rows, entries):
    """Resolve task roots in memory, flattening deeper helpers and refusing cycles."""
    parents = {pane: home for pane in entries if (home := placement(pane, rows, entries))}
    fields = {}
    for pane, home in parents.items():
        root, seen = home, {pane}
        while root in parents and root not in seen:
            seen.add(root)
            root = parents[root]
        if root in seen:
            continue
        role = ROLES.get(rows[pane].role, 'Worker' if not rows[pane].role else 'Helper')
        if home != root and role == 'Review':
            role = ROLES.get(rows[home].role, 'Worker' if not rows[home].role else 'Helper') + "'s review"
        fields[pane] = {'task': root, 'task_depth': 1 if home == root else 2, 'task_role': role}
    return fields


def pending_roots(options, entries, fields):
    """A helper can show in tmux before discovery can bind its process."""
    pending = set()
    for pane, row in options.items():
        if pane in entries or not is_child(row.record, row.role, row.group_by):
            continue
        home, thread = helper_tabs.placement_owner(row.record)
        owner = entries.get(home)
        if owner and row.session == owner.get('tmux_session') and (not thread or helper_tabs._thread(owner) in (None, thread)):
            pending.add(fields.get(home, {}).get('task', home))
    return pending


def state(row):
    if row.get('state') in ('Approval', 'Input needed') or row.get('ask') or row.get('escalated'):
        return 'needs'
    return 'idle' if row.get('state') == 'Idle' else 'working'


def manual_mark(entry, options, user_prompt_at=None):
    thread = helper_tabs.verified_thread(entry)
    if not thread or options is None or options.done != thread:
        return False
    if options.done_at and user_prompt_at is not None:
        try:
            done_at = float(options.done_at)
        except (ValueError, TypeError):
            return True
        try:
            return user_prompt_at(entry) <= done_at
        except injected_prompts.ProvenanceUnknown as exc:
            if exc.user_at > done_at:
                return False  # a proven user prompt after Done
            if exc.at is not None and exc.at <= done_at:
                return True  # the unknown prompt came before Done, so the mark still holds
            raise  # an unknown prompt after Done: the caller cannot decide yet
        except (ValueError, TypeError):
            pass
    return True


def is_done(entry, row, children, options, backlog_done, user_prompt_at=None):
    thread = helper_tabs.verified_thread(entry)
    if not thread or any(state(member) != 'idle' for member in (row, *children)):
        return False
    try:
        if manual_mark(entry, options, user_prompt_at):
            return True
    except injected_prompts.ReceiptsUnreadable:
        return False
    if options is not None and options.reopened == thread:
        return False
    ids = BACKLOG_ID.findall(helper_tabs.visible_name(entry) or '')
    return len(ids) == 1 and ids[0] in backlog_done


def done_items():
    """Read the shared backlog only once per listing or sweep."""
    import backlog
    from projects import ProjectError

    try:
        return backlog.done_ids()
    except (OSError, ProjectError, ValueError):
        return set()


def annotate_done(rows, entries, options, user_prompt_at=None):
    done = done_items()
    fields = {row['id']: row for row in rows if 'task' in row}
    pending = pending_roots(options, entries, fields)
    children = {}
    for row in rows:
        if 'task' in row:
            children.setdefault(row['task'], []).append(row)
    for row in rows:
        pane = row['id']
        if 'task' in row or pane not in entries:
            continue
        marks = options.get(pane)
        entry = entries[pane]
        try:
            holds = manual_mark(entry, marks, user_prompt_at)
        except injected_prompts.ReceiptsUnreadable:
            continue  # unknown: neither Done nor a Done mark on this read
        # Replay that decision in is_done without a second transcript read.
        read_prompt = (lambda _: 0) if holds else (lambda _: float('inf'))
        if holds:
            row['done_mark'] = True
        if pane not in pending and is_done(entry, row, children.get(pane, []), marks, done, read_prompt):
            row['done'] = True


def set_marks(pane, run, **values):
    """Publish related mark changes in one tmux command."""
    names = {'done': DONE, 'reopened': REOPENED, 'done_at': DONE_AT}
    command = [paths.TMUX]
    for name, value in values.items():
        if len(command) > 1:
            command.append(';')
        command += ['set-option', '-p', '-t', pane]
        command += ['-u', names[name]] if value is None else [names[name], str(value)]
    run(command, check=True, capture_output=True, timeout=5)
