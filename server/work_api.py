"""HTTP routes for the work board (/mobile/api/work/...). server.py checks sign-in, Origin and the X-Codex-Mobile
header before it calls these. The store is work_items.py.

An answer goes to the tab that asked, as a message, only while that pane still runs the same conversation; else it
stays in the item's history and the reply says it was not sent."""
import json
import threading

import paths
from projects import _read, _write

import jira_client
import graph_client
import work_feeds
import work_fix
import work_items
import work_jira
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
            return {**work_items.board(), 'jira': jira_client.status(), 'feeds': work_feeds.statuses(),
                    'graph': graph_client.status()}
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


def post(handler, sessions=None, status=None, send=None, run=None, lock=None):
    """run and lock: tmux runner and the server's send lock. Without sessions and run, no fix tab starts."""
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
            result = {'item': item, 'sent': sent, 'why': why}
            if item['stage'] == 'ready' and (body.get('choice') or '').casefold() == 'build':
                if sessions and run:
                    result['item'], result['fix'] = work_fix.try_start(item, sessions(), run, lock, ask.get('q', ''))
                else:
                    result['fix'] = {'started': False, 'why': 'Fix tabs are off.'}
            return result
        if name == 'fix':
            if not (sessions and run):
                raise ProjectError(503, 'Fix tabs are off.')
            _, item = work_items.find(body.get('item'))
            item, fix = work_fix.try_start({**item, 'version': version}, sessions(), run, lock, strict=True)
            return {'item': item, 'fix': fix}
        if name == 'stage':
            return {'item': work_items.stage(body.get('item'), body.get('stage'), 'mak', version=version,
                                             text=body.get('text') or '')}
        if name == 'qa':
            return {'item': work_items.set_qa(body.get('item'), body.get('qa'), 'mak', version=version)}
        if name == 'jira':  # Move again: the Jira move of a Done item, on the worker thread
            _, item = work_items.find(body.get('item'))
            if item['version'] != version:
                raise ProjectError(409, f'{item["id"]} changed since you loaded it. Reload and try again.')
            if item['stage'] != 'done':
                raise ProjectError(409, f'{item["id"]} is not Done. Tether moves Jira tickets when an item is Done.')
            if not work_jira.tickets(item, work_items.project(item['project'])):
                raise ProjectError(400, f'{item["id"]} has no Jira ticket of its project.')
            queued = work_jira.MOVER.queue(item['id'])
            return {'item': item, 'queued': bool(queued), 'why': '' if queued else 'A Jira move for it runs now.'}
        if name == 'triage':  # Start triage tab from the drawer: a Triage item with no live triage tab
            if not (sessions and run):
                raise ProjectError(503, 'Triage tabs are off.')
            with work_fix.START_LOCK:  # the feed's own launch takes it too: check the item inside it
                _, item = work_items.find(body.get('item'))
                if item['version'] != version:
                    raise ProjectError(409, f'{item["id"]} changed since you loaded it. Reload and try again.')
                if item['stage'] != 'triage':
                    raise ProjectError(409, f'{item["id"]} is not in Triage.')
                entries = sessions()
                if work_fix.triage_running(item, entries, run):
                    raise ProjectError(409, f'{item["id"]} already has a triage tab.')
                item, tab = work_fix.start_triage(item, entries, run, lock)  # checks the item again under the lock
            return {'item': item, 'tab': tab}
        if name == 'note':
            return {'item': work_items.note(body.get('item'), body.get('text'), 'mak', version=version)}
        if name == 'item':
            refs = body.get('refs') or []
            tab = body.get('tab', False)
            if not isinstance(tab, bool):
                raise ValueError('Invalid request.')
            item = work_items.new(body.get('project'), body.get('title'), 'mak', refs=refs, source='Added by hand')
            if not tab:
                return {'item': item}
            if not (sessions and run):
                return {'item': item, 'tab': {'started': False, 'why': 'Triage tabs are off.'}}
            item, started = work_fix.start_triage(item, sessions(), run, lock)
            return {'item': item, 'tab': started}
        if name == 'project':
            create = body.get('create')
            if not isinstance(create, bool):
                raise ValueError('Invalid request.')
            return work_items.save_project(body.get('project'), body.get('revision'), create)
        raise ProjectError(404, 'Not found')
    _answer(handler, work)


NOTICE_LIMIT = 180


class Watcher:
    """A push notification when an item enters Needs you, also with every page closed. It remembers the last event
    number it saw per project (paths.work()/notified.json): a restart repeats no old question, and its first run
    only takes note of what is there. It follows the Urgent setting in notification settings: Off sends nothing,
    Silent sends a silent notice."""

    def __init__(self, notify, every=4.0, clock=None, delivery=None):
        import time
        self.notify, self.every, self.clock = notify, every, clock or time.monotonic
        self.delivery = delivery or (lambda: {})
        self.next = 0.0

    def _path(self):
        return paths.work() / 'notified.json'

    def _load(self):
        try:
            data = json.loads(_read(self._path()) or '{}')
        except ValueError:
            data = {}
        return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}

    def observe(self):
        if self.clock() < self.next:
            return 0
        self.next = self.clock() + self.every
        seen = self._load()
        changed, sent = False, 0
        for record in work_items.registry()['projects']:
            prefix = record['prefix']
            try:
                events = work_items._events(prefix)
            except ProjectError:
                continue
            top = events[-1]['n'] if events else 0
            last = seen.get(prefix)
            if last is None or top < last:  # first look, or the file started over
                seen[prefix], changed = top, True
                continue
            asks = [e for e in events if e['n'] > last and e['type'] == 'ask']
            mode = self.delivery().get('urgent', 'sound') if asks else 'sound'
            if asks and mode != 'off':
                items = work_items.fold(record, events)
                for event in asks:
                    item = items.get(event['item'])
                    if item and item['stage'] == 'needs' and (item.get('ask') or {}).get('id') == event.get('ask'):
                        self.notify(self.payload(item, silent=mode == 'silent'), True)
                        sent += 1
            if top != last:
                seen[prefix], changed = top, True
        if changed:
            _write(self._path(), json.dumps(seen) + '\n')
        return sent

    @staticmethod
    def payload(item, silent=False):
        question = item['ask']['q']
        body = question if len(question) <= NOTICE_LIMIT else question[:NOTICE_LIMIT - 1].rstrip() + '…'
        return {'title': f'{item["id"]} needs you: {item["title"]}'[:120], 'body': body, 'tag': 'work-' + item['id'],
                'go': 'work', 'item': item['id'], 'priority': 'urgent', 'silent': silent, 'url': '/chat/?go=work'}
