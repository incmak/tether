"""Merge tmux sessions and move panes between them, for drag and drop in the sidebar.

Sessions are named by tmux session id ($N) and panes by pane id (%N), the ids the session list already sends to the
page. Every move keeps the pane ids, so open chats stay attached. The terminal panel's own session
(side_shells.SESSION) is never a source or a target."""
import re
import secrets
import subprocess
import sys
import time

import coordinator
import groups
import helper_tabs
import paths
import projects
import side_shells

PANE = re.compile(r'%[0-9]+')
SESSION = re.compile(r'\$[0-9]+')
GONE = 'That session or pane is gone. Refresh the list and try again.'
BREAKOUT_MARKER = '@tether_breakout'
BREAKOUT_IDS = re.compile(r'(\$[0-9]+)\t(%[0-9]+)')
ROLE_FIELDS = '#{pane_id}\t#{@tether_role}'
CLEANUP_ERRORS = (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError)


def _tmux(run, *args, command=None):
    return run([*(command or [paths.TMUX]), *args], capture_output=True, text=True, timeout=5)


def _try_tmux(run, *args, command=None):
    """A timeout leaves the command's effect unknown."""
    try:
        return _tmux(run, *args, command=command)
    except subprocess.TimeoutExpired:
        return None


def _target_session(run, target, *, gone=None):
    """Read an exact pane or window's session. A confirmed absence can be distinguished from a failed read."""
    field = '#{pane_id}' if target.startswith('%') else '#{window_id}'
    result = _try_tmux(run, 'display-message', '-p', '-t', target, field + '\t#{session_id}')
    if result is None or result.returncode:
        return None
    fields = result.stdout.rstrip('\r\n').split('\t')
    if fields == ['', '']:
        return gone
    if len(fields) == 2 and fields[0] == target and SESSION.fullmatch(fields[1]):
        return fields[1]
    return None


def _info(run, target, fmt):
    result = _tmux(run, 'display-message', '-p', '-t', target, fmt)
    fields = result.stdout.strip().split('\t')
    if result.returncode or len(fields) != fmt.count('\t') + 1 or not all(fields):
        raise ValueError(GONE)
    return fields


def _session(run, session_id):
    """The id and name of a checked session outside the terminal panel."""
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise ValueError('Choose a session.')
    found, name = _info(run, session_id + ':', '#{session_id}\t#{session_name}')
    if found != session_id:
        raise ValueError(GONE)
    if name == side_shells.SESSION:
        raise ValueError('Terminal panel shells cannot be moved.')
    return found, name


def _pane(run, pane_id):
    """The session id that holds the pane, checked like _session."""
    if not isinstance(pane_id, str) or not PANE.fullmatch(pane_id):
        raise ValueError('Choose a pane.')
    found, session_id = _info(run, pane_id, '#{pane_id}\t#{session_id}')
    if found != pane_id:
        raise ValueError(GONE)
    return _session(run, session_id)[0]


def _check(result, message):
    if result is None:
        raise RuntimeError(message)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or message)


def _moved(run, session_id, pane):
    """What the page needs to offer a rename: the target session, its name, and a pane in it."""
    name, first = _info(run, session_id + ':', '#{session_name}\t#{pane_id}')
    return {'session': session_id, 'name': name, 'pane': pane or first}


def _names(run, session_id):
    """Pane id -> Tether pane name (tmux option @chat_name, '' when unnamed) for every pane of the session."""
    result = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{pane_id}\t#{@chat_name}')
    _check(result, GONE)
    return dict(line.split('\t', 1) for line in result.stdout.splitlines() if '\t' in line)


def pane_role(run, pane):
    result = _tmux(run, 'display-message', '-p', '-t', pane, ROLE_FIELDS)
    found, _, role = result.stdout.rstrip('\n').partition('\t')
    if result.returncode or found != pane:
        raise ValueError(GONE)
    return role


def coordinator_panes(run, session_id):
    result = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', ROLE_FIELDS)
    _check(result, GONE)
    return {pane for pane, _, role in (line.partition('\t') for line in result.stdout.splitlines())
            if role in helper_tabs.COORDINATOR_MARKS}


def coordinator_to_clear(run, pane, target):
    """The pane's mark when a different pane in the destination already serves as coordinator."""
    role = pane_role(run, pane)
    return role if role in helper_tabs.COORDINATOR_MARKS and coordinator_panes(run, target) - {pane} else ''


def _clear_coordinator(run, pane):
    if pane_role(run, pane) in helper_tabs.COORDINATOR_MARKS:
        coordinator._set(run, pane, None)


def _name_plan(run, source, target, panes):
    """Names for the moving panes that have no Tether name: the session they leave ('alpha', then 'alpha 2', ...),
    never a name already used in either session. tmux's own numbered session names ('3') carry no name, so they give none."""
    base = ' '.join(_info(run, source + ':', '#{session_name}')[0].split())[:34]
    if base.isdigit():
        return {}
    named = _names(run, source)
    taken = set(named.values()) | set(_names(run, target).values())
    plan, name, number = {}, base, 1
    for pane in panes:
        if named.get(pane):
            continue
        while name in taken:
            number += 1
            name = f'{base} {number}'
        plan[pane] = name
        taken.add(name)
    return plan


def _set_name(run, pane, name):
    """No @chat_name_at is set, so the name counts as the oldest: a name given later in Claude or Codex still wins."""
    _check(_try_tmux(run, 'set-option', '-p', '-t', pane, '@chat_name', name), 'Could not name the pane.')


def _cleanup(run, *steps):
    """Try every cleanup step after a move and report the first failure."""
    first = None
    for action, *args in steps:
        try:
            action(run, *args)
        except CLEANUP_ERRORS as exc:
            if first is None:
                first = exc
    if isinstance(first, OSError):
        raise RuntimeError(str(first)) from first
    if first is not None:
        raise first


KEEP = ('source', 'target')
CHOOSE = 'Both sessions have a project. Choose which one to keep.'


def _project(run, session_id):
    """The session's @project id (projects.py), or '' when it has none."""
    found = _tmux(run, 'display-message', '-p', '-t', session_id + ':', '#{@project}').stdout.strip()
    return found if projects.ID.fullmatch(found) else ''


def _project_plan(run, source, target, keep):
    """(project the merged session keeps, project to archive). Asked before any window moves, so a missing choice
    changes nothing. When only one session's project gives agents text, that one stays and nothing is lost."""
    if keep is not None and keep not in KEEP:
        raise ValueError('Choose which project to keep.')
    ours, theirs = _project(run, source), _project(run, target)
    if not ours or ours == theirs:
        return None, None
    if not theirs:
        return ours, None
    if keep is None:
        ours_text, theirs_text = projects.has_context(ours), projects.has_context(theirs)
        if ours_text and theirs_text:
            raise ValueError(CHOOSE)
        keep = 'source' if ours_text else 'target'
    return (ours, theirs) if keep == 'source' else (theirs, ours)


def _apply_project(run, target, kept, dropped):
    """Set the project's tmux option and archive the other project if possible."""
    if not kept:
        return
    _check(_try_tmux(run, 'set-option', '-t', target, '@project', kept), 'Could not set the project.')
    if dropped:
        try:
            projects.archive(dropped)
        except (OSError, projects.ProjectError) as exc:
            print(f'merge: project {dropped} not archived: {exc}', file=sys.stderr, flush=True)


def _window_panes(run, session_id):
    """{window id: {pane ids}} of a session, read before a merge moves its windows."""
    result = _tmux(run, 'list-panes', '-s', '-t', session_id + ':', '-F', '#{window_id}\t#{pane_id}')
    _check(result, GONE)
    out = {}
    for line in result.stdout.splitlines():
        window, _, pane = line.partition('\t')
        out.setdefault(window, set()).add(pane)
    return out


def _windows(run, session_id):
    """The window ids of a session, or None when tmux did not answer."""
    result = _try_tmux(run, 'list-windows', '-t', session_id + ':', '-F', '#{window_id}')
    return None if result is None or result.returncode else set(result.stdout.split())


def _window_left(run, window, source, target):
    """True when the window is gone from source and is in target, False when not, None when tmux did not answer.
    A window linked into both sessions has not left the source."""
    left, there = _windows(run, source), _windows(run, target)
    if there is None or (left is None and _session_open(run, source) is not False):
        return None  # a source that closed has no windows left
    return window not in (left or set()) and window in there


def _merge_cleanup(run, source, moved, coordinators, plan):
    """moved: the panes of the windows whose move the merge confirmed. A later tmux read cannot change it."""
    if moved:
        _cleanup(
            run,
            *((_clear_coordinator, pane) for pane in sorted(coordinators & moved)),
            (_drop_root, source, moved),
            *((_set_name, pane, name) for pane, name in plan.items() if pane in moved),
        )


def merge_session(source, target, run=subprocess.run, keep=None):
    """Move every window of source into target, with its layout. The emptied source session closes;
    its name stays on the moved panes. keep ('source' or 'target') says whose project the merged session keeps;
    the other project's folder goes to the archive."""
    source, _ = _session(run, source)
    target, name = _session(run, target)
    if source == target:
        raise ValueError('Drop the session on a different session.')
    kept, dropped = _project_plan(run, source, target, keep)
    source_coordinators = coordinator_panes(run, source)
    target_coordinators = coordinator_panes(run, target) if source_coordinators else set()
    windows = _tmux(run, 'list-windows', '-t', source + ':', '-F', '#{window_id}')
    _check(windows, GONE)
    source_panes = _names(run, source)
    window_panes = _window_panes(run, source)
    plan = _name_plan(run, source, target, source_panes)
    coordinators = source_coordinators - target_coordinators if target_coordinators else set()
    groups.merge_into(source, target, run)  # before the move, so no moved pane points to a missing group
    confirmed = set()  # panes of the windows whose move is confirmed
    pending = None     # the window whose move is in flight
    try:
        for moved_count, window in enumerate(windows.stdout.split()):
            pending = window
            # source:window names the source's link: a bare window id may pick the link in another session.
            moved = _try_tmux(run, 'move-window', '-d', '-s', f'{source}:{window}', '-t', target + ':')
            if moved is None or moved.returncode:
                left = _window_left(run, window, source, target)
                if left is None:
                    raise RuntimeError('Could not confirm the move. Refresh the list.')
                if not left:
                    if moved_count:
                        raise RuntimeError('Only part of the session moved. Check both sessions.')
                    _check(moved, 'Could not move the window.')
            confirmed |= window_panes.get(window, set())
            pending = None
    except CLEANUP_ERRORS as exc:
        try:
            if pending is not None and _window_left(run, pending, source, target):
                confirmed |= window_panes.get(pending, set())  # it moved, though the first check could not say so
        except CLEANUP_ERRORS:
            pass  # unknown: the confirmed moves still get their cleanup below
        try:
            _merge_cleanup(run, source, confirmed, coordinators, plan)
        except CLEANUP_ERRORS as cleanup_error:
            raise exc from cleanup_error
        raise
    try:
        _merge_cleanup(run, source, confirmed, coordinators, plan)
        _apply_project(run, target, kept, dropped)
        return _moved(run, target, None)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        raise RuntimeError(f'The windows moved to “{name}”, but Tether could not finish. Refresh the list.') from exc


def move_pane(pane, target, run=subprocess.run):
    """Move one pane into a new window of target. A session left with no panes closes."""
    source = _pane(run, pane)
    target, name = _session(run, target)
    if source == target:
        raise ValueError('The pane is already in that session.')
    clear_coordinator = coordinator_to_clear(run, pane, target)
    plan = _name_plan(run, source, target, [pane])
    moved = _try_tmux(run, 'break-pane', '-d', '-s', pane, '-t', target + ':')
    if moved is None or moved.returncode:
        current = _target_session(run, pane, gone='')
        if current is None:
            raise RuntimeError('Could not confirm the move. Refresh the list.')
        if current != target:
            _check(moved, 'Could not move the pane.')
    try:
        coordinator_steps = [(_clear_coordinator, pane)] if clear_coordinator else []
        _cleanup(
            run, *coordinator_steps,
            (_drop_root, source, {pane}),
            (_ungroup, pane),
            *((_set_name, tab, tab_name) for tab, tab_name in plan.items()),
        )
        result = _moved(run, target, pane)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        raise RuntimeError(f'The pane moved to “{name}”, but Tether could not finish. Refresh the list.') from exc
    restart = restart_info(run, pane, target)
    if restart:
        result['restart'] = restart
    return result


def restart_info(run, pane, session_id):
    """The target project id and name, plus the session name, when the pane's agent started in another project.
    The agent keeps its start project's folder access until it restarts (project_launch.py, B326). Read only: a
    session with no @project yet gets one at the agent's next start, so any started project differs. A pane with no
    @project_rev did not start through Tether. Any tmux failure gives None: the move itself is done."""
    shown = _try_tmux(run, 'display-message', '-p', '-t', pane, '#{session_id}\t#{@project_rev}\t#{@project}\t#{session_name}')
    fields = shown.stdout.rstrip('\n').split('\t') if shown is not None and not shown.returncode else []
    if len(fields) != 4 or fields[0] != session_id:
        return None
    started = fields[1].partition('+')[0].rpartition(':')[0]
    if not projects.ID.fullmatch(started) or started == fields[2]:
        return None
    pid = fields[2] if projects.ID.fullmatch(fields[2]) else ''
    try:
        name = projects.meta(pid)['name'] if pid else ''
    except OSError:
        name = ''
    return {'project': pid, 'name': name or fields[3], 'session': fields[3]}


def _ungroup(run, pane):
    """A pane that leaves its session goes to Unstacked: groups belong to one session (groups.py)."""
    _check(_try_tmux(run, 'set-option', '-p', '-u', '-t', pane, '@group'), 'Could not clear the pane group.')


def _drop_root(run, session_id, panes):
    """A stack can name a departing pane as its root even if the pane is in Unstacked."""
    with groups.LOCK():
        try:
            stacks = groups.read(session_id, run)
        except groups.GroupError as exc:
            if exc.status == 404:  # The session disappears when its last pane moves.
                return
            raise
        rooted = [stack for stack in stacks if stack.get('root') in panes]
        if rooted:
            for stack in rooted:
                del stack['root']
            groups.write(session_id, stacks, run)


def _free_name(run, wanted=''):
    """The pane's Tether name when it has one (tmux turns '.' and ':' in a session name into '_'), else split-HHMMSS."""
    names = set(_tmux(run, 'list-sessions', '-F', '#{session_name}').stdout.split('\n'))
    base = re.sub(r'[.:]', '_', ' '.join(wanted.split())) or time.strftime('split-%H%M%S')
    name, n = base, 2
    while name in names:
        name, n = f'{base} {n}' if wanted.strip() else f'{base}-{n}', n + 1
    return name


def _has_breakout_marker(run, pane, token):
    result = _try_tmux(run, 'display-message', '-p', '-t', pane, '#{@tether_breakout}')
    return result is not None and result.returncode == 0 and result.stdout == token + '\n'


def _remove_placeholder(run, pane, token):
    """Retry an uncertain kill once. Return None if the marker cannot be confirmed before a kill."""
    if not _has_breakout_marker(run, pane, token):
        return None
    killed = _try_tmux(run, 'kill-pane', '-t', pane)
    if killed is not None and not killed.returncode:
        return True
    current = _target_session(run, pane, gone='')
    if current:
        if not _has_breakout_marker(run, pane, token):
            return None
        _try_tmux(run, 'kill-pane', '-t', pane)
        current = _target_session(run, pane, gone='')
    return current == ''


def _session_open(run, session):
    """True or False by the exact session id; None when tmux did not answer."""
    result = _try_tmux(run, 'list-sessions', '-F', '#{session_id}')
    if result is None or result.returncode:
        return None
    return session in result.stdout.split()


def _session_warning(run, name, session=None):
    left = _session_open(run, session) if session is not None else None
    if left is None:
        return f' A session “{name}” may be left open.'
    if left:
        return f' The new session “{name}” stays open. Close it from the sidebar.'
    return ''


def break_out(pane, run=subprocess.run):
    """Move one pane into a new session of its own, started in the pane's folder."""
    source = _pane(run, pane)
    count = _tmux(run, 'list-panes', '-s', '-t', source + ':', '-F', '#{pane_id}')
    _check(count, GONE)
    if len(count.stdout.split()) < 2:
        raise ValueError('That pane is already a session of its own.')
    folder = _info(run, pane, '#{pane_current_path}')[0]
    wanted = _names(run, source).get(pane, '')
    name = _free_name(run, wanted)
    token = secrets.token_hex(8)
    try:
        created = _tmux(run, 'new-session', '-d', '-s', name, '-c', folder,
                        '-P', '-F', '#{session_id}\t#{pane_id}', ';', 'set-option', '-p', BREAKOUT_MARKER, token,
                        command=paths.tmux_new_session())
    except subprocess.TimeoutExpired as exc:
        created, output = None, exc.stdout
    else:
        output = created.stdout
    if isinstance(output, bytes):
        output = output.decode(errors='replace')  # TimeoutExpired can hold bytes even with text=True.
    # The last field after splitting on newline may be a clipped record.
    records = [record for line in (output or '').split('\n')[:-1] if (record := BREAKOUT_IDS.fullmatch(line))]
    ids = records[0] if len(records) == 1 else None
    if created is None or created.returncode or ids is None:
        checked_session, confirmed = None, False
        if ids and _has_breakout_marker(run, ids[2], token):
            killed = _try_tmux(run, 'kill-pane', '-t', ids[2])
            checked_session, confirmed = ids[1], killed is not None and not killed.returncode
        # Any printed output, even a clipped record, means tmux may have made the session.
        warning = (_session_warning(run, name, checked_session)
                   if created is None or created.returncode == 0 or (output or '').strip() else '')
        if checked_session and not confirmed and not warning:
            # The session is gone, but an unconfirmed kill may have missed a placeholder moved elsewhere.
            warning = _session_warning(run, name)
        raise RuntimeError('Could not start a session.' + warning)
    session_id, placeholder = ids.groups()
    moved = _try_tmux(run, 'break-pane', '-d', '-s', pane, '-t', session_id + ':')
    recheck_move = False
    if moved is None or moved.returncode:
        current = _target_session(run, pane, gone='')
        if current not in (session_id, source, ''):
            raise RuntimeError(f'Could not confirm the move. The new session “{name}” may be left open.')
        recheck_move = current != session_id
    # Killing only the placeholder lets a move that lands during cleanup keep its pane.
    removed = _remove_placeholder(run, placeholder, token)
    if recheck_move and _target_session(run, pane, gone='') != session_id:
        warning = _session_warning(run, name, session_id if removed is not None else None)
        if removed is False and not warning:
            # The session is gone, but an unconfirmed kill may have missed a placeholder moved elsewhere.
            warning = _session_warning(run, name)
        if warning:
            raise RuntimeError('Could not move the pane.' + warning)
        _check(moved, 'Could not move the pane.')
    warning = ''
    if removed is None:
        warning = _session_warning(run, name).lstrip()
    elif not removed:
        warning = 'The pane moved, but its new session kept an extra shell tab. Close it from the sidebar.'
    try:
        _cleanup(run, (_drop_root, source, {pane}), (_ungroup, pane))
        result = _moved(run, session_id, pane)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        message = f'The pane moved to “{name}”, but Tether could not finish. Refresh the list.'
        raise RuntimeError(f'{message} {warning}' if warning else message) from exc
    if warning:
        result['warning'] = warning
    restart = restart_info(run, pane, session_id)
    if restart:
        result['restart'] = restart
    return result


def merge(kind, source, target, run=subprocess.run, keep=None):
    if kind == 'session':
        return merge_session(source, target, run, keep)
    if kind == 'pane':
        return move_pane(source, target, run)
    if kind == 'break':
        return break_out(source, run)
    raise ValueError('Unknown move.')
