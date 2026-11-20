"""HTTP routes for groups (/mobile/api/groups/...). server.py checks sign-in, Origin and the X-Codex-Mobile header
before it calls post(). The store and its rules are groups.py."""
from contextlib import nullcontext
import json
import subprocess
from urllib.parse import parse_qs, urlsplit

import audit
import auto_resume
import groups
import helper_close
import helper_tabs
import projects
from task_tree_api import interrupted_response
from terminal_input import UUID
import undo

PREFIX = '/mobile/api/groups/'
BODY_LIMIT = 16384
WRITES = {PREFIX + name for name in ('create', 'rename', 'move', 'ungroup', 'remove', 'arrange', 'reorder', 'close')}  # server.py TMUX_WRITES


def handles(path):
    return path.startswith(PREFIX)


def _answer(handler, work):
    try:
        handler.respond(200, work())
    except undo.CloseInterrupted as exc:
        interrupted_response(handler, exc)
    except (groups.GroupError, projects.ProjectError) as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})
    except subprocess.SubprocessError:
        handler.respond(503, {'error': 'Tether could not read or update the live stacks. Try again.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read or write the stack folder.'})


def _id(value, pattern, message):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise groups.GroupError(400, message)
    return value


def _session(value):
    return _id(value, groups.SESSION, 'Choose a session.')


def _group(value):
    return _id(value, groups.GROUP, 'Choose a stack.')


def _pane(value):
    return _id(value, groups.PANE, 'Choose a tab.')


def get(handler, run=subprocess.run, home=None):
    parts = urlsplit(handler.path)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    name = parts.path[len(PREFIX):]

    def work():
        if name == 'context':  # the group's Context sheet; the first look makes the bundle
            session_id, group_id = _session(query.get('session')), _group(query.get('group'))
            pid = groups.context(session_id, group_id, run)
            session_name, found = groups._session(session_id, run)
            group = next((g for g in found if g['id'] == group_id), None)
            if group is None:
                raise groups.GroupError(404, groups.GONE_GROUP)
            return dict(projects.state(pid, home), group_name=group['name'], session_name=session_name)
        raise groups.GroupError(404, 'Not found')
    _answer(handler, work)


def _threads(sessions):
    """thread_of(pane) for arrange: the conversation each tab holds now (sessions(): server.py's tab list)."""
    found = {}
    for entry in sessions():
        thread = entry.get('thread')
        if isinstance(thread, str) and UUID.fullmatch(thread):
            found[entry.get('id')] = thread
    return found.get


def _close(session_id, group_id, body, fresh, run, status):
    """The caller holds the send, helper record and stack locks through preflight and close."""
    requested = body.get('panes')
    if not isinstance(requested, list) or not requested:
        raise ValueError('Confirm the tabs to close.')
    planned, buckets = {}, {}
    for item in requested:
        if not isinstance(item, dict) or 'thread' not in item:
            raise ValueError('Confirm each tab and its conversation.')
        pane = _pane(item.get('pane'))
        thread = item['thread']
        bucket = item.get('bucket')
        if (pane in planned or bucket not in ('idle', 'working')
                or thread is not None and (not isinstance(thread, str) or not UUID.fullmatch(thread))):
            raise ValueError('Confirm each tab and its conversation.')
        planned[pane] = thread
        buckets[pane] = bucket

    groups.check_group(session_id, group_id, run)
    kind, server = auto_resume.server_id(run=run)
    if kind != 'up':
        raise groups.GroupError(409, helper_close.CLOSE_CHANGED)
    entries = fresh()
    members = groups.stack_members(session_id, group_id, entries, run)
    live = {entry['id']: entry for entry in entries if entry.get('tmux_session') == session_id}
    if planned.keys() != members.keys() or any(
        pane not in live or thread != helper_tabs.verified_thread(live[pane])
        or thread is None and live[pane].get('agent') != 'shell' for pane, thread in planned.items()
    ):
        raise groups.GroupError(409, 'The stack’s tabs changed. Open the stack menu again to review them.')

    def close_status(entry):
        row = status(entry)
        return dict(row, state='Shell') if entry.get('agent') == 'shell' else row

    if any(helper_close.close_bucket(close_status(live[pane])) != buckets[pane] for pane in planned):
        raise groups.GroupError(409, helper_close.CLOSE_CHANGED)
    identities = {pane: helper_close.close_identity(live[pane]) for pane in planned}

    # Close grandchildren first. Keep the existing separate Undo steps for each tab.
    closed, saved_step, saved_label = [], None, ''
    for pane in sorted(members, key=members.get, reverse=True):
        saved = undo.capture_safe(pane, run, [live[pane]])
        title = helper_tabs.visible_name(live[pane]) or pane

        def close():
            helper_close.close_matching_pane(pane, run, expected=identities[pane], bucket=buckets[pane],
                                            server=server, fresh=fresh, status=close_status)

        try:
            step = undo.close_tab(pane, run, saved, title, close=close, server=server)
        except undo.CloseInterrupted as exc:
            if exc.members:
                audit.record('tab.close', f'Closed tab {title}', pane=pane)
            raise undo.CloseInterrupted(str(exc), closed + exc.members, exc.undo or saved_step,
                                        exc.label if exc.undo else saved_label, stale=exc.status == 409,
                                        unknown=exc.unknown) from exc
        closed.append((pane, title))
        audit.record('tab.close', f'Closed tab {title}', pane=pane)
        if step:
            saved_step, saved_label = step, undo.tab_close_label(saved, title)
    try:
        undo.group_remove(session_id, group_id, run, after_close=True)
    except undo.CLOSE_ERRORS as exc:
        raise undo.CloseInterrupted('The tabs closed. Tether could not finish removing the stack.',
                                    closed, saved_step, saved_label) from exc
    return {'ok': True}


def post(handler, run=subprocess.run, sessions=lambda: [], *, fresh=None, status=None, lock=None, changed=lambda: None):
    name = handler.path.split('?', 1)[0][len(PREFIX):]

    def work():
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= BODY_LIMIT:
            raise groups.GroupError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        if PREFIX + name not in WRITES:
            raise groups.GroupError(404, 'Not found')
        session_id = _session(body.get('session'))
        if name == 'create':
            pane = body.get('pane')
            return {'group': undo.group_create(session_id, body.get('name'), run, pane=None if pane is None else _pane(pane),
                                           by_user=True)}
        if name == 'arrange':
            return {'groups': groups.arrange(session_id, body.get('stacks', []), run, body.get('unstack', []),
                                             _threads(sessions))}
        if name == 'reorder':
            return {'groups': groups.reorder(session_id, body.get('groups'), run)}
        if name == 'move':
            group_id = body.get('group')
            undo.group_move(session_id, _pane(body.get('pane')), None if group_id in (None, '') else _group(group_id), run,
                            by_user=True)
            return {'ok': True}
        group_id = _group(body.get('group'))
        if name == 'rename':
            return {'group': undo.group_rename(session_id, group_id, body.get('name'), run)}
        if name == 'ungroup':
            undo.group_ungroup(session_id, group_id, run, by_user=True)
        else:
            with lock or nullcontext(), groups.LOCK(), helper_tabs.record_lock():
                if name == 'close':
                    try:
                        return _close(session_id, group_id, body, fresh or sessions, run, status)
                    except undo.CloseInterrupted:
                        raise
                    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                        raise groups.GroupError(503, 'Tether could not finish closing the stack. Refresh its tabs.') from exc
                    finally:
                        changed()
                undo.group_remove(session_id, group_id, run, entries=(fresh or sessions)())
        return {'ok': True}
    _answer(handler, work)
