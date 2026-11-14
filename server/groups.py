"""Groups: named sets of tabs (tmux panes) inside one tmux session, each with its own agent context.

The session option @groups holds the list, in order, empty groups included:
  [{"id": "g1a2b3c4d", "name": "Home dashboard", "project": "<12-hex projects.py id or empty>"}]
The pane option @group holds a group id. A pane with no @group, or with an id that is not in @groups, is in Unstacked.
The pane option @group_by is "user" after the user moved the tab by hand (groups_api.py). Auto stacks never move such a tab.
A group's context is a projects.py bundle, made on first use. Removing a group archives the bundle; nothing is deleted.
This module is the store and its rules. groups_api.py is the HTTP part."""
import contextlib
import json
import re
import secrets
import subprocess
import threading
import unicodedata

try:
    import fcntl
except ImportError:  # Windows: the server is the only writer there
    fcntl = None

import paths
import projects

GROUP = re.compile(r'g[0-9a-f]{8}')
SESSION = re.compile(r'\$[0-9]+')
PANE = re.compile(r'%[0-9]+')
NAME_LIMIT = 40
RESERVED = {'unstacked', 'main'}  # names for the tabs in no stack; Main is the old name
_THREADS = threading.RLock()
_depth = 0


@contextlib.contextmanager
def _locked():
    """Hold for each read-modify-write of @groups. The server, the agent launcher (project_launch.py), auto-resume and
    tmux merge are separate processes, so a thread lock is not enough: the outermost holder also takes a file lock."""
    global _depth
    with _THREADS:
        handle = None
        if _depth == 0 and fcntl is not None:
            try:
                paths.CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
                handle = open(paths.CACHE / 'groups.lock', 'a')
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                if handle:
                    handle.close()
                handle = None
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
            if handle:
                handle.close()  # closing releases the lock


LOCK = _locked
GONE_TAB = 'That tab has ended.'
GONE_GROUP = 'That stack no longer exists.'
GONE_SESSION = 'That session has ended.'


class GroupError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _tmux(run, *args):
    return run([paths.TMUX, *args], capture_output=True, text=True, timeout=5)


def _check(result, message='Could not save the stack.'):
    if result.returncode:
        raise GroupError(500, result.stderr.strip() or message)


def clean_name(value):
    """A trimmed group name, or GroupError(400). Unstacked is checked by the callers that make or rename a group."""
    if not isinstance(value, str):
        raise GroupError(400, 'Enter a stack name.')
    name = value.strip()
    if not 1 <= len(name) <= NAME_LIMIT:
        raise GroupError(400, f'Use 1 to {NAME_LIMIT} characters for the stack name.')
    if any(unicodedata.category(char)[0] == 'C' for char in name):
        raise GroupError(400, 'The stack name cannot have control characters.')
    return name


def _valid(item):
    if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not GROUP.fullmatch(item['id']):
        return None
    try:
        name = clean_name(item.get('name'))
    except GroupError:
        return None
    project = item.get('project')
    return {'id': item['id'], 'name': name,
            'project': project if isinstance(project, str) and projects.ID.fullmatch(project) else ''}


def parse(text):
    """The @groups value as a list. Bad JSON reads as []; bad entries and repeated ids are dropped."""
    try:
        data = json.loads(text) if text else []
    except ValueError:
        return []
    found, seen = [], set()
    for item in data if isinstance(data, list) else []:
        group = _valid(item)
        if group and group['id'] not in seen:
            seen.add(group['id'])
            found.append(group)
    return found


def _session(session_id, run):
    """(session name, groups) of a live session, or GroupError(404)."""
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise GroupError(400, 'Choose a session.')
    shown = _tmux(run, 'display-message', '-p', '-t', session_id + ':', '#{session_id}\t#{session_name}\t#{@groups}')
    found, _, rest = shown.stdout.rstrip('\n').partition('\t')
    if shown.returncode or found != session_id or '\t' not in rest:
        raise GroupError(404, GONE_SESSION)
    name, _, raw = rest.rpartition('\t')
    return name, parse(raw)


def read(session_id, run=subprocess.run):
    return _session(session_id, run)[1]


def session_name(session_id, run=subprocess.run):
    return _session(session_id, run)[0]


def write(session_id, groups, run=subprocess.run):
    """Save the list. An empty list unsets @groups, so the session looks as it did before groups."""
    if groups:
        value = json.dumps([{'id': g['id'], 'name': g['name'], 'project': g.get('project') or ''} for g in groups])
        _check(_tmux(run, 'set-option', '-t', session_id, '@groups', value))
    else:
        _check(_tmux(run, 'set-option', '-u', '-t', session_id, '@groups'))


def all_groups(run=subprocess.run):
    """{session id: [groups]} for every session that has groups, from one tmux call."""
    shown = _tmux(run, 'list-sessions', '-F', '#{session_id}\t#{@groups}')
    if shown.returncode:
        return {}
    out = {}
    for line in shown.stdout.splitlines():
        session_id, _, raw = line.partition('\t')
        found = parse(raw)
        if SESSION.fullmatch(session_id) and found:
            out[session_id] = found
    return out


def _find(groups, group_id):
    if not isinstance(group_id, str) or not GROUP.fullmatch(group_id):
        raise GroupError(400, 'Choose a stack.')
    group = next((g for g in groups if g['id'] == group_id), None)
    if group is None:
        raise GroupError(404, GONE_GROUP)
    return group


def _free_name(groups, name, keep=None):
    if name.lower() in RESERVED:
        raise GroupError(400, 'Unstacked and Main are kept for the tabs in no stack. Choose another name.')
    if any(g['name'].lower() == name.lower() and g['id'] != keep for g in groups):
        raise GroupError(409, 'A stack with that name exists.')


def _new_id(taken):
    while True:
        group_id = 'g' + secrets.token_hex(4)
        if group_id not in taken:
            return group_id


def _pane(pane, run):
    """(session id, @group) of a live pane, or GroupError(404)."""
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise GroupError(400, 'Choose a tab.')
    shown = _tmux(run, 'display-message', '-p', '-t', pane, '#{pane_id}\t#{session_id}\t#{@group}')
    fields = shown.stdout.rstrip('\n').split('\t')
    if shown.returncode or len(fields) != 3 or fields[0] != pane:
        raise GroupError(404, GONE_TAB)
    return fields[1], fields[2]


def _label(pane, group_id, run, by_user=False):
    if group_id:
        _check(_tmux(run, 'set-option', '-p', '-t', pane, '@group', group_id), 'Could not move the tab. It may have closed.')
    else:
        _check(_tmux(run, 'set-option', '-p', '-u', '-t', pane, '@group'), 'Could not move the tab. It may have closed.')
    if by_user:
        _check(_tmux(run, 'set-option', '-p', '-t', pane, BY, 'user'), 'Could not move the tab. It may have closed.')


BY = '@group_by'


def placed_by_user(pane, run=subprocess.run):
    """True when the user moved this tab by hand (to a stack or to Unstacked). Auto stacks leave such a tab alone."""
    shown = _tmux(run, 'display-message', '-p', '-t', pane, '#{' + BY + '}')
    return shown.returncode == 0 and shown.stdout.strip() == 'user'


def _pane_in(session_id, pane, run):
    if _pane(pane, run)[0] != session_id:
        raise GroupError(404, 'That tab is in another session. Refresh the list and try again.')


def create(session_id, name, run=subprocess.run, pane=None, by_user=False):
    """Add a group at the end of the list. With pane, move that tab into it. by_user: the user asked (groups_api.py)."""
    name = clean_name(name)
    with LOCK():
        groups = read(session_id, run)
        _free_name(groups, name)
        if pane is not None:
            _pane_in(session_id, pane, run)
        # The bundle id is made here, so an agent launcher only reads @groups (context()).
        group = {'id': _new_id({g['id'] for g in groups}), 'name': name, 'project': secrets.token_hex(6)}
        write(session_id, groups + [group], run)
        if pane is not None:
            _label(pane, group['id'], run, by_user)
    return group


def rename(session_id, group_id, name, run=subprocess.run):
    name = clean_name(name)
    with LOCK():
        groups = read(session_id, run)
        group = _find(groups, group_id)
        _free_name(groups, name, keep=group_id)
        group['name'] = name
        write(session_id, groups, run)
    if group['project']:
        projects.set_name(group['project'], name)  # the name an archive shows
    return group


def move(session_id, pane, group_id, run=subprocess.run, by_user=False):
    """Put a tab in a group of its session. None or '' puts it in Unstacked."""
    with LOCK():
        groups = read(session_id, run)
        if group_id:
            _find(groups, group_id)
        _pane_in(session_id, pane, run)
        _label(pane, group_id or '', run, by_user)


def _labelled(session_id, group_id, run):
    shown = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{pane_id}\t#{@group}')
    _check(shown, GONE_SESSION)
    return [pane for pane, _, label in (line.partition('\t') for line in shown.stdout.splitlines()) if label == group_id]


def _archive(group):
    if group['project']:
        projects.archive(group['project'])


def ungroup(session_id, group_id, run=subprocess.run, by_user=False):
    """Move the group's tabs to Unstacked and drop the group. No agent stops. Its context bundle goes to the archive."""
    with LOCK():
        groups = read(session_id, run)
        group = _find(groups, group_id)
        for pane in _labelled(session_id, group_id, run):
            _label(pane, '', run, by_user)
        write(session_id, [g for g in groups if g['id'] != group_id], run)
    _archive(group)


def remove(session_id, group_id, run=subprocess.run):
    """Drop an empty group. Its context bundle goes to the archive."""
    with LOCK():
        groups = read(session_id, run)
        group = _find(groups, group_id)
        if _labelled(session_id, group_id, run):
            raise GroupError(409, 'The stack still has tabs. Move or close them first.')
        write(session_id, [g for g in groups if g['id'] != group_id], run)
    _archive(group)


def find_or_create(session_id, name, run=subprocess.run):
    """For agent_tab_cli.py --group NAME: the group with that name (any case), made when missing. Unstacked gives None."""
    name = clean_name(name)
    if name.lower() in RESERVED:
        return None
    with LOCK():
        found = next((g for g in read(session_id, run) if g['name'].lower() == name.lower()), None)
        return found or create(session_id, name, run)


def free_name(groups, name):
    """name, or "name 2", "name 3"... : the first one that no group has (any case) and that is not reserved."""
    name, base, number = clean_name(name), clean_name(name), 2
    taken = {g['name'].lower() for g in groups} | RESERVED
    while name.lower() in taken:
        suffix = f' {number}'
        name = base[:NAME_LIMIT - len(suffix)].rstrip() + suffix
        number += 1
    return name


def create_unique(session_id, name, run=subprocess.run):
    """Auto stacks: a new group under a free form of name ("Name 2" when "Name" is taken)."""
    with LOCK():
        return create(session_id, free_name(read(session_id, run), name), run)


def auto_move(session_id, pane, group_id, run=subprocess.run):
    """Auto stacks: put a tab in a group, unless the user placed it by hand. True when it moved."""
    with LOCK():
        if placed_by_user(pane, run):
            return False
        move(session_id, pane, group_id, run)
    return True


def group_of_pane(pane, run=subprocess.run):
    """(session id, group dict or None) of a live pane."""
    session_id, label = _pane(pane, run)
    if not label:
        return session_id, None
    return session_id, next((g for g in read(session_id, run) if g['id'] == label), None)


def find_in_session_of(pane, group_id, run=subprocess.run):
    """The group with that id in the live pane's session, or GroupError (400 bad id, 404 gone)."""
    session_id = _pane(pane, run)[0]
    return _find(read(session_id, run), group_id)


def context(session_id, group_id, run=subprocess.run):
    """The group's context bundle id, with its folder made. create() sets the id; a group saved without one (an old
    @groups value) gets one here, under the cross-process lock."""
    with LOCK():
        groups = read(session_id, run)
        group = _find(groups, group_id)
        if not group['project']:
            group['project'] = secrets.token_hex(6)
            write(session_id, groups, run)
    pid = group['project']
    (projects.bundle(pid) / 'notes').mkdir(mode=0o700, parents=True, exist_ok=True)
    projects.set_name(pid, group['name'])
    return pid


def merged(target, source):
    """Target groups, then source groups. A source name that a target group has (any case) gets a number:
    "Name 2", "Name 3", the first free one. A source id that the target has gets a new id.
    Returns (groups, {old source id: new id})."""
    out = [dict(g) for g in target]
    names = {g['name'].lower() for g in out}
    ids = {g['id'] for g in out}
    remap = {}
    for group in source:
        name, number = group['name'], 2
        while name.lower() in names:
            suffix = f' {number}'
            name = group['name'][:NAME_LIMIT - len(suffix)].rstrip() + suffix
            number += 1
        group_id = group['id'] if group['id'] not in ids else _new_id(ids | {g['id'] for g in source})
        remap[group['id']] = group_id
        names.add(name.lower())
        ids.add(group_id)
        out.append({'id': group_id, 'name': name, 'project': group['project']})
    return out, remap


def merge_into(source, target, run=subprocess.run):
    """tmux_merge.merge_session, before the windows move: the target gets the source groups (merged), and source panes
    get the ids their groups have in the target. A source label with no source group is cleared, so no pane points
    to a group it was never in."""
    with LOCK():
        theirs, ours = read(target, run), read(source, run)
        labels = _tmux(run, 'list-panes', '-s', '-t', source + ':', '-F', '#{pane_id}\t#{@group}')
        _check(labels, GONE_SESSION)
        combined, remap = merged(theirs, ours)
        if ours:
            write(target, combined, run)
        for pane, _, label in (line.partition('\t') for line in labels.stdout.splitlines()):
            if label and remap.get(label) != label:
                _label(pane, remap.get(label, ''), run)


def restore(session_id, text, run=subprocess.run):
    """Auto-resume: add saved groups that the session does not have (by id). A saved name that is taken is skipped."""
    saved = parse(text)
    if not saved:
        return
    with LOCK():
        groups = read(session_id, run)
        ids, names = {g['id'] for g in groups}, {g['name'].lower() for g in groups}
        added = [g for g in saved if g['id'] not in ids and g['name'].lower() not in names]
        if added:
            write(session_id, groups + added, run)
