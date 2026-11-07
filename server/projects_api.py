"""HTTP routes for session projects (/mobile/api/projects/...). server.py checks sign-in, Origin and the
X-Codex-Mobile header before it calls these. The store is projects.py."""
import json
import subprocess
from urllib.parse import parse_qs, urlsplit

import backlog
import projects

PREFIX = '/mobile/api/projects/'
BODY_LIMIT = 262144


def handles(path):
    return path.startswith(PREFIX)


def _answer(handler, work):
    try:
        handler.respond(200, work())
    except projects.ProjectError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read or write the project folder.'})


def get(handler, run=subprocess.run, home=None):
    parts = urlsplit(handler.path)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    name = parts.path[len(PREFIX):]

    def work():
        if name == 'project':  # the Project sheet: the session's project, made on first look
            pid, session_name = projects.session_project(query.get('session'), run)
            projects.set_name(pid, session_name)
            return dict(projects.state(pid, home), session_name=session_name)
        if name == 'marks':  # the sidebar mark and the "restart to apply" badge
            return projects.marks(run, home)
        if name == 'note':
            return projects.read_note(query.get('project'), query.get('name'))
        if name == 'backlog':
            return backlog.read()
        raise projects.ProjectError(404, 'Not found')
    _answer(handler, work)


def post(handler, home=None):
    name = handler.path.split('?', 1)[0][len(PREFIX):]

    def work():
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= BODY_LIMIT:
            raise projects.ProjectError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        if name == 'backlog':  # one list for the app, with no project id or revision
            return backlog.save(body.get('text'), body.get('version'))
        pid = body.get('project')
        projects.bundle(pid)  # checks the id
        revision = body.get('revision')
        if name == 'instructions':
            projects.set_instructions(pid, body.get('text'), revision)
        elif name == 'budget':
            projects.set_budget(pid, body.get('chars'), revision)
        elif name == 'pin':
            projects.pin(pid, body.get('path'), body.get('mode'), revision, home)
        elif name == 'unpin':
            projects.unpin(pid, body.get('name'), revision)
        elif name == 'note':
            projects.write_note(pid, body.get('name'), body.get('text'))
        elif name == 'note-delete':
            projects.delete_note(pid, body.get('name'))
        elif name == 'seen':
            projects.mark_seen(pid)
        else:
            raise projects.ProjectError(404, 'Not found')
        if isinstance(body.get('session_name'), str) and len(body['session_name']) <= 64:  # for archives
            projects.set_name(pid, body['session_name'])
        return projects.state(pid, home)
    _answer(handler, work)
