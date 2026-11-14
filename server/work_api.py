"""HTTP routes for the work board (/mobile/api/work/...). server.py checks sign-in, Origin and the X-Codex-Mobile
header before it calls these. The store is work_items.py.

An answer goes to the tab that asked, as a message, only while that pane still runs the same conversation; else it
stays in the item's history and the reply says it was not sent."""
import json
import threading

import work_items
from projects import ProjectError

PREFIX = '/mobile/api/work/'
BODY_LIMIT = 65536
DELIVERY_LOCK = threading.Lock()


def handles(path):
    return path.split('?', 1)[0].startswith(PREFIX)


def _answer(handler, work):
    try:
        handler.respond(200, work())
    except ProjectError as exc:
        handler.respond(exc.status, {'error': str(exc)})
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc) or 'Invalid request.'})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read or write the work items.'})


def get(handler):
    """The board: projects and items. The page joins linked tabs with its own session list (live state and names)."""
    name = handler.path.split('?', 1)[0][len(PREFIX):]

    def work():
        if name == 'board':
            return work_items.board()
        raise ProjectError(404, 'Not found')
    _answer(handler, work)


def message_for(item, event_text):
    return (f'Mak answered {item["id"]} on the work board: {event_text}\n'
            f'Item: {item["title"]}. Its stage is now {work_items.STAGE_LABELS[item["stage"]]}. '
            f'Run `tether item show {item["id"]}` for its history.')


def deliver(item, ask, text, sessions, status, send):
    """Send Mak's answer to the asking pane. Returns (sent, why). The pane must still run the conversation that asked
    (a reused pane id or a new chat in the pane gets nothing) and must not wait on a prompt of its own."""
    pane, chat = ask.get('pane'), ask.get('chat')
    if not pane:
        return False, 'No tab asked this question.'
    if not chat:
        return False, f'Tether does not know which conversation in tab {pane} asked.'
    entry = next((e for e in sessions() if e.get('id') == pane), None)
    if entry is None:
        return False, f'The asking tab {pane} is closed.'
    if entry.get('thread') != chat:
        return False, f'Tab {pane} runs another conversation now.'
    if not entry.get('writable', True):
        return False, f'Tab {pane} does not take messages.'
    state = status(entry).get('state')
    if state in ('Approval', 'Input needed', 'Unavailable'):
        return False, f'Tab {pane} waits on a prompt. Answer it there first.'
    with DELIVERY_LOCK:
        send(entry, message_for(item, text), state)
    return True, ''


def post(handler, sessions=None, status=None, send=None):
    name = handler.path.split('?', 1)[0][len(PREFIX):]

    def work():
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= BODY_LIMIT:
            raise ProjectError(413, 'Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        version = body.get('version')
        if name != 'project' and name != 'item' and not isinstance(version, int):
            raise ValueError('Reload the board and try again.')
        if name == 'answer':
            before = work_items.find(body.get('item'))[1]
            ask = dict(before.get('ask') or {})
            item = work_items.answer(body.get('item'), body.get('ask'), version,
                                     choice=body.get('choice'), text=body.get('text'))
            said = body['choice'] if body.get('choice') is not None else body.get('text', '')
            sent, why = False, 'Delivery is off.'
            if sessions and status and send:
                try:
                    sent, why = deliver(item, ask, said, sessions, status, send)
                except (OSError, RuntimeError, ValueError) as exc:
                    sent, why = False, str(exc) or 'The message did not go through.'
            tab_name = ask.get('pane', '')
            item = work_items.record(item['id'], 'sent', f'Sent to tab {tab_name}' if sent else f'Not sent: {why}')
            return {'item': item, 'sent': sent, 'why': why}
        if name == 'stage':
            return {'item': work_items.stage(body.get('item'), body.get('stage'), 'mak', version=version,
                                             text=body.get('text') or '')}
        if name == 'qa':
            return {'item': work_items.set_qa(body.get('item'), body.get('qa'), 'mak', version=version)}
        if name == 'note':
            return {'item': work_items.note(body.get('item'), body.get('text'), 'mak', version=version)}
        if name == 'item':
            refs = body.get('refs') or []
            return {'item': work_items.new(body.get('project'), body.get('title'), 'mak', refs=refs,
                                           source='Added by hand')}
        if name == 'project':
            create = body.get('create')
            if not isinstance(create, bool):
                raise ValueError('Invalid request.')
            return work_items.save_project(body.get('project'), body.get('revision'), create)
        raise ProjectError(404, 'Not found')
    _answer(handler, work)
