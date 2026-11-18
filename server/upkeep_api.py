"""HTTP routes for the clean-up list (prune.py, B211) and the daily ops report (ops_agent.py, B212), and the one
call that runs both and the worktree sweep (sweep.py, B210) from the server's monitor loop.

  GET  /mobile/api/upkeep/prune       the flagged lessons and notes, and what can be undone
  POST /mobile/api/upkeep/prune       {action: remove, kind, project, id, mark} | {action: keep, kind: lesson|note,
                                      project, id} | {action: restore, token}
                                      | {action: check}
  GET  /mobile/api/upkeep/ops         the switch, the last runs and the lesson candidates
  GET  /mobile/api/upkeep/ops/note    ?project=&day=  one postmortem note
  POST /mobile/api/upkeep/ops         {enabled: bool} | {action: run} | {action: accept|dismiss, project, id}

server.py checks sign-in, Origin and the X-Codex-Mobile header before it calls post().
"""
import json
import re
import subprocess
from urllib.parse import parse_qs, urlsplit

import lesson_candidates
import ops_agent
import projects
import prune
import sweep

PREFIX = '/mobile/api/upkeep/'
BODY_LIMIT = 4096
ID = re.compile(r'[0-9a-f]{8}')


def handles(path):
    return path.split('?', 1)[0] in (PREFIX + 'prune', PREFIX + 'ops', PREFIX + 'ops/note')


def _answer(handler, work):
    try:
        handler.respond(200, work())
    except (prune.PruneError, projects.ProjectError) as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read or write its files.'})


def get(handler):
    parts = urlsplit(handler.path)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    name = parts.path[len(PREFIX):]

    def work():
        if name == 'prune':
            return prune.state()
        if name == 'ops':
            return ops_agent.state()
        return ops_agent.read_note(query.get('project'), query.get('day'))
    _answer(handler, work)


def _body(handler):
    size = int(handler.headers.get('Content-Length', '0') or 0)
    if not 0 < size <= BODY_LIMIT:
        raise ValueError('Invalid request.')
    body = json.loads(handler.rfile.read(size))
    if not isinstance(body, dict):
        raise ValueError('Invalid request.')
    return body


def _prune(body):
    action = body.get('action')
    if action == 'check' and set(body) == {'action'}:
        prune.check()
    elif action == 'restore' and set(body) == {'action', 'token'} and isinstance(body['token'], str):
        prune.restore(body['token'])
    elif action == 'remove' and set(body) == {'action', 'kind', 'project', 'id', 'mark'}:
        prune.remove(body['kind'], body['project'], body['id'], body['mark'])
    elif action == 'keep' and set(body) == {'action', 'kind', 'project', 'id'}:
        prune.keep(body['kind'], body['project'], body['id'])
    else:
        raise ValueError('Choose check, remove, keep or restore.')
    return prune.state()


def _ops(body):
    if set(body) == {'enabled'}:
        ops_agent.set_enabled(body['enabled'])
    elif body.get('action') == 'run' and set(body) == {'action'}:
        ops_agent.request()
    elif body.get('action') in ('accept', 'dismiss') and set(body) == {'action', 'project', 'id'}:
        pid, identifier = body['project'], body['id']
        projects.bundle(pid)  # checks the id
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError('Choose a lesson candidate.')
        done = (lesson_candidates.accept if body['action'] == 'accept' else lesson_candidates.dismiss)(pid, identifier)
        if not done:
            raise projects.ProjectError(404, 'That lesson candidate is gone.')
    else:
        raise ValueError('Choose on or off, run, accept or dismiss.')
    return ops_agent.state()


def post(handler):
    name = handler.path.split('?', 1)[0][len(PREFIX):]

    def work():
        body = _body(handler)
        if name == 'prune':
            return _prune(body)
        if name == 'ops':
            return _ops(body)
        raise projects.ProjectError(405, 'Use GET for a postmortem note.')
    _answer(handler, work)


class Upkeep:
    """The three periodic jobs, called on every tick of the server's monitor loop. Each decides itself when it is due,
    and one that fails never stops the others or the monitor."""

    def __init__(self, notify, delivery):
        self.sweeper = sweep.Sweeper(notify, delivery)
        self.pruner = prune.Pruner(notify, delivery)
        self.ops = ops_agent.OPS

    def observe(self, entries, folder_of, status, run=subprocess.run, lock=None, sessions=None):
        for name, work in (('worktree sweep', lambda: self.sweeper.observe(entries, folder_of, status, run)),
                           ('clean-up check', self.pruner.observe),
                           ('ops report', lambda: self.ops.observe(entries, folder_of, run, lock, sessions))):
            try:
                work()
            except Exception as exc:  # noqa: BLE001
                print(f'{name} failed: {exc!r}', flush=True)
