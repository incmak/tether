"""HTTP routes for groups (/mobile/api/groups/...). server.py checks sign-in, Origin and the X-Codex-Mobile header
before it calls post(). The store and its rules are groups.py."""
import json
import subprocess
from urllib.parse import parse_qs, urlsplit

import groups
import projects

PREFIX = '/mobile/api/groups/'
BODY_LIMIT = 16384
WRITES = {PREFIX + name for name in ('create', 'rename', 'move', 'ungroup', 'remove')}  # server.py TMUX_WRITES


def handles(path):
    return path.startswith(PREFIX)


def _answer(handler, work):
    try:
        handler.respond(200, work())
    except (groups.GroupError, projects.ProjectError) as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})
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


def post(handler, run=subprocess.run):
    name = handler.path.split('?', 1)[0][len(PREFIX):]

    def work():
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= BODY_LIMIT:
            raise groups.GroupError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        if name not in ('create', 'rename', 'move', 'ungroup', 'remove'):
            raise groups.GroupError(404, 'Not found')
        session_id = _session(body.get('session'))
        if name == 'create':
            pane = body.get('pane')
            return {'group': groups.create(session_id, body.get('name'), run, pane=None if pane is None else _pane(pane),
                                           by_user=True)}
        if name == 'move':
            group_id = body.get('group')
            groups.move(session_id, _pane(body.get('pane')), None if group_id in (None, '') else _group(group_id), run,
                        by_user=True)
            return {'ok': True}
        group_id = _group(body.get('group'))
        if name == 'rename':
            return {'group': groups.rename(session_id, group_id, body.get('name'), run)}
        if name == 'ungroup':
            groups.ungroup(session_id, group_id, run, by_user=True)
        else:
            groups.remove(session_id, group_id, run)
        return {'ok': True}
    _answer(handler, work)
