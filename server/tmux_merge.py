"""Merge tmux sessions and move panes between them, for drag and drop in the sidebar.

Sessions are named by tmux session id ($N) and panes by pane id (%N), the ids the session list already sends to the
page. Every move keeps the pane ids, so open chats stay attached. The terminal panel's own session
(side_shells.SESSION) is never a source or a target."""
import re
import secrets
import subprocess
import sys
import time

import groups
import paths
import projects
import side_shells

PANE = re.compile(r'%[0-9]+')
SESSION = re.compile(r'\$[0-9]+')
GONE = 'That session or pane is gone. Refresh the list and try again.'
BREAKOUT_MARKER = '@tether_breakout'
BREAKOUT_IDS = re.compile(r'(\$[0-9]+)\t(%[0-9]+)')


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


def _apply_names(run, plan):
    """No @chat_name_at is set, so the name counts as the oldest: a name given later in Claude or Codex still wins."""
    for pane, name in plan.items():
        _check(_try_tmux(run, 'set-option', '-p', '-t', pane, '@chat_name', name), 'Could not name the pane.')


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


def merge_session(source, target, run=subprocess.run, keep=None):
    """Move every window of source into target, with its layout. The emptied source session closes;
    its name stays on the moved panes. keep ('source' or 'target') says whose project the merged session keeps;
    the other project's folder goes to the archive."""
    source, _ = _session(run, source)
    target, name = _session(run, target)
    if source == target:
        raise ValueError('Drop the session on a different session.')
    kept, dropped = _project_plan(run, source, target, keep)
    windows = _tmux(run, 'list-windows', '-t', source + ':', '-F', '#{window_id}')
    _check(windows, GONE)
    plan = _name_plan(run, source, target, list(_names(run, source)))
    groups.merge_into(source, target, run)  # before the move, so no moved pane points to a missing group
    for moved_count, window in enumerate(windows.stdout.split()):
        moved = _try_tmux(run, 'move-window', '-d', '-s', window, '-t', target + ':')
        if moved is None or moved.returncode:
            current = _target_session(run, window, gone='')
            if current is None:
                raise RuntimeError('Could not confirm the move. Refresh the list.')
            if current == target:
                continue
            if moved_count:
                raise RuntimeError('Only part of the session moved. Check both sessions.')
            _check(moved, 'Could not move the window.')
    try:
        _apply_names(run, plan)
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
    plan = _name_plan(run, source, target, [pane])
    moved = _try_tmux(run, 'break-pane', '-d', '-s', pane, '-t', target + ':')
    if moved is None or moved.returncode:
        current = _target_session(run, pane, gone='')
        if current is None:
            raise RuntimeError('Could not confirm the move. Refresh the list.')
        if current != target:
            _check(moved, 'Could not move the pane.')
    try:
        _ungroup(run, pane)
        _apply_names(run, plan)
        return _moved(run, target, pane)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        raise RuntimeError(f'The pane moved to “{name}”, but Tether could not finish. Refresh the list.') from exc


def _ungroup(run, pane):
    """A pane that leaves its session goes to Unstacked: groups belong to one session (groups.py)."""
    _check(_try_tmux(run, 'set-option', '-p', '-u', '-t', pane, '@group'), 'Could not clear the pane group.')


def _free_name(run, wanted=''):
    """The pane's Tether name when it has one (tmux turns '.' and ':' in a session name into '_'), else split-HHMMSS."""
    names = set(_tmux(run, 'list-sessions', '-F', '#{session_name}').stdout.split('\n'))
    base = re.sub(r'[.:]', '_', ' '.join(wanted.split())) or time.strftime('split-%H%M%S')
    name, n = base, 2
    while name in names:
        name, n = f'{base} {n}' if wanted.strip() else f'{base}-{n}', n + 1
    return name


def _remove_placeholder(run, pane):
    """Retry once when an uncertain kill left the shell pane open."""
    killed = _try_tmux(run, 'kill-pane', '-t', pane)
    if killed is not None and not killed.returncode:
        return True
    current = _target_session(run, pane, gone='')
    if current:
        _try_tmux(run, 'kill-pane', '-t', pane)
        current = _target_session(run, pane, gone='')
    return current == ''


def _session_open(run, session):
    """True or False by the exact session id; None when tmux did not answer."""
    result = _try_tmux(run, 'list-sessions', '-F', '#{session_id}')
    if result is None or result.returncode:
        return None
    return session in result.stdout.split()


def _breakout_panes(run):
    """Read pane ids and markers across all sessions. None means the scan could not be confirmed."""
    listed = _try_tmux(run, 'list-panes', '-a', '-F', '#{pane_id}\t#{@tether_breakout}')
    if listed is None or listed.returncode:
        return None
    panes = [line.split('\t', 1) for line in listed.stdout.splitlines()]
    if any(len(fields) != 2 or not PANE.fullmatch(fields[0]) for fields in panes):
        return None
    return panes


def _drop_new(run, name, token, *, placeholder=None, uncertain=False):
    """Remove only the pane identified by the creation output or its unique marker."""
    warning = f' A session “{name}” may be left open.'
    if placeholder is None:
        panes = _breakout_panes(run)
        if panes is None:
            return warning
        matches = [pane for pane, marker in panes if marker == token]
        if not matches:
            # After a timeout the server may still create a pane.
            return warning if uncertain else ''
        if len(matches) != 1:
            return warning
        placeholder = matches[0]
    _try_tmux(run, 'kill-pane', '-t', placeholder)
    panes = _breakout_panes(run)
    return warning if panes is None or any(pane == placeholder for pane, _ in panes) else ''


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
    records = [record for line in (output or '').splitlines() if (record := BREAKOUT_IDS.fullmatch(line))]
    ids = records[0] if len(records) == 1 else None
    if created is None or created.returncode or ids is None:
        warning = _drop_new(run, name, token, placeholder=ids[2] if ids else None,
                            uncertain=created is None or created.returncode == 0 or bool(records))
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
    removed = _remove_placeholder(run, placeholder)
    if recheck_move and _target_session(run, pane, gone='') != session_id:
        left = _session_open(run, session_id) if removed else None
        if left is None:
            raise RuntimeError(f'Could not move the pane. The new session “{name}” may be left open.')
        if left:
            raise RuntimeError(f'Could not move the pane. The new session “{name}” was left open.')
        _check(moved, 'Could not move the pane.')
    warning = '' if removed else 'The pane moved, but its new session kept an extra shell tab. Close it from the sidebar.'
    try:
        _ungroup(run, pane)
        result = _moved(run, session_id, pane)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as exc:
        message = f'The pane moved to “{name}”, but Tether could not finish. Refresh the list.'
        raise RuntimeError(f'{message} {warning}' if warning else message) from exc
    if warning:
        result['warning'] = warning
    return result


def merge(kind, source, target, run=subprocess.run, keep=None):
    if kind == 'session':
        return merge_session(source, target, run, keep)
    if kind == 'pane':
        return move_pane(source, target, run)
    if kind == 'break':
        return break_out(source, run)
    raise ValueError('Unknown move.')
