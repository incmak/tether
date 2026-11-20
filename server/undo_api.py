"""HTTP routes for undo (/mobile/api/undo). server.py checks sign-in, Origin and the X-Codex-Mobile header first.

GET: the history, newest first, each entry with 'why' when it cannot run now.
POST {action: 'undo'|'redo', id?: entry id (none: the next one), confirmed?: the token of the confirm answer}:
  200 {status: 'done', ...} after it ran, 200 {status: 'confirm', effects: [...], token} when a critical step needs a
  yes first (and again when its effects changed since the token),
  409 {error} when the world changed and it cannot run (nothing changed).
Also: the hooks server.py calls after each tab and session route (note_start), and the response headers that name the
step a request recorded (headers), so the page can offer Undo in its toast.
"""
import json
import subprocess
from urllib.parse import quote

import groups
import helper_tabs
import undo
import undo_store

PATH = '/mobile/api/undo'
BODY_LIMIT = 4096
START_PATHS = {'/mobile/api/split', '/mobile/api/fork', '/mobile/api/delegate', '/mobile/api/continue',
               '/mobile/api/resume', '/mobile/api/session', '/mobile/api/tmux-sessions'}


def handles(path):
    return path.split('?', 1)[0] == PATH


def get(handler, env):
    """?brief=1: no checks (the Undo buttons only need what comes next; History asks for the reasons)."""
    brief = 'brief=1' in handler.path.split('?', 1)[1:2][0].split('&') if '?' in handler.path else False
    try:
        handler.respond(200, undo.history(env, check=not brief))
    except undo_store.LockError as exc:
        handler.respond(503, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read the undo history.'})


def post(handler, env, lock):
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= BODY_LIMIT:
            handler.respond(413, {'error': 'Invalid request.'})
            return
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict) or body.get('action') not in ('undo', 'redo'):
            raise ValueError('Choose undo or redo.')
        entry_id = body.get('id')
        if entry_id is not None and (not isinstance(entry_id, str) or len(entry_id) > 40):
            raise ValueError('Choose a step from the history.')
        confirmed = body.get('confirmed')
        if confirmed is not None and (not isinstance(confirmed, str) or len(confirmed) > 64):
            raise ValueError('Invalid request.')
        with helper_tabs.SEND_LOCK, lock:
            result = undo.run(body['action'], entry_id, env, confirmed=confirmed)
        handler.respond(200, result)
    except undo.Refused as exc:
        handler.respond(409, {'error': str(exc)})
    except undo_store.LockError as exc:
        handler.respond(503, {'error': str(exc)})
    except groups.GroupError as exc:  # a ValueError with its own status: 503 when the stacks could not be locked
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})
    except (OSError, subprocess.SubprocessError):
        handler.respond(503, {'error': 'Tether could not reach tmux. Try again.'})


def note_start(path, status, body, run=subprocess.run):
    """After a route that opened a tab or a session: record it, so Undo can close it."""
    if path not in START_PATHS or status not in (200, 201) or not isinstance(body, dict):
        return
    pane = body.get('pane')
    if path == '/mobile/api/session':
        pane = body.get('session')
    elif path == '/mobile/api/tmux-sessions':
        listed = run([undo.paths.TMUX, 'list-panes', '-s', '-t', str(body.get('session', '')) + ':', '-F', '#{pane_id}'],
                     capture_output=True, text=True, timeout=5)
        pane = (listed.stdout.split() or [None])[0] if listed.returncode == 0 else None
    elif path == '/mobile/api/continue' and status != 201:
        return  # a repeat of a continue that already ran
    if isinstance(pane, str) and undo.PANE.fullmatch(pane):
        undo.record_tab_start(pane, run)


def headers(status):
    """Save the step this request recorded and name it in the answer: X-Tether-Undo (id), X-Tether-Undo-Label."""
    if status >= 300:
        return {}
    entry = undo_store.flush()
    if not entry:
        return {}
    return {'X-Tether-Undo': entry['id'], 'X-Tether-Undo-Label': quote(entry['label'], safe='')}
