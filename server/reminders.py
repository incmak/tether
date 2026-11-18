"""Reminders: Mak or an agent sets a text for a time; Tether delivers it at that time, also after a restart.

One JSON file (paths.reminders()/reminders.json) holds every reminder. The server and agent_reminder_cli.py both
change it under a file lock and replace it atomically, so a Codex sandbox (write access to that folder only) can add
reminders without the server's port. The server reloads the file on each tick.

States: scheduled -> due (the time passed) -> done. Snooze moves a reminder back to scheduled with a new time.
Each schedule has an occurrence number (`occ`). A notification action targets its own occurrence. Relative
snoozes use the server clock. One ticker per store holds the scheduler lock. Push and ping retry independently
using `sent`, `tries` and `retry_at`. Each attempt holds a 120-second claim in `sending` until its result is saved.
An expired claim can retry. This can duplicate a send if the service accepted it before the server saved the
result, for example if the server crashed. Web Push saves each accepted endpoint before sending to the next.
The Teams command receives `TETHER_PING_KEY=<id>:<occ>` for deduplication. A Teams timeout has an unknown outcome
and is recorded as sent to avoid a duplicate. Web Push and Waiting on you still deliver the reminder.
A lost Web Push answer (read timeout, the service closed the connection) also counts as accepted for that
browser. Only errors that prove the request never left retry (push.NOT_SENT: connect timeout, refused, DNS, TLS).
After eight failures the channel stops retrying and is listed in `failed`. Deleted reminders keep their full
delivery state for restore by ID for 30 days.
Times are UTC epoch seconds.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import paths

PREFIX = '/mobile/api/reminders'
ACTIONS = ('add', 'edit', 'done', 'reopen', 'snooze', 'delete', 'restore')
ZONE = ZoneInfo(os.environ.get('TETHER_REMINDER_ZONE') or 'Asia/Kolkata')
TEXT_LIMIT = 500
LATE = 120  # fired this many seconds after its time (server down or busy): the notice says late
DONE_KEPT = 50  # finished reminders kept; older ones are dropped
DELETED_AGE = 30 * 86400
MAX_AHEAD = 5 * 366 * 86400
CHANNELS = ('push', 'ping')
MAX_TRIES = 8
BACKOFF = (30, 120, 600, 1800)
LEASE = 120  # seconds before another tick can retry an unfinished channel attempt
PANE = re.compile(r'%\d{1,6}')
THREAD = re.compile(r'[A-Za-z0-9_.:-]{1,120}')
ITEM = re.compile(r'B\d{1,6}')
PROJECT = re.compile(r'[A-Za-z0-9_.-]{1,80}')
ID = re.compile(r'R\d{1,9}')
STATES = ('scheduled', 'due', 'done')


class ReminderError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def folder():
    return paths.reminders()


def _path():
    return folder() / 'reminders.json'


@contextlib.contextmanager
def _locked():
    folder().mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(folder() / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _clean_link(link):
    if link is None:
        return None
    if not isinstance(link, dict):
        raise ReminderError('The link is not valid.')
    out = {}
    for key, pattern in (('pane', PANE), ('thread', THREAD), ('item', ITEM), ('project', PROJECT)):
        value = link.get(key)
        if value in (None, ''):
            continue
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ReminderError(f'The link {key} is not valid.')
        out[key] = value
    if 'thread' in out and 'pane' not in out:
        del out['thread']
    return out or None


def _valid(record):
    return (isinstance(record, dict) and isinstance(record.get('id'), str) and ID.fullmatch(record['id'])
            and isinstance(record.get('text'), str) and record['text'].strip()
            and isinstance(record.get('due'), (int, float)) and not isinstance(record.get('due'), bool)
            and record.get('state') in STATES)


def _recently_deleted(records, now):
    return sorted((r for r in records if r['deleted_at'] > now - DELETED_AGE),
                  key=lambda r: r['deleted_at'], reverse=True)


def _load(now, *, prune_deleted=True):
    try:
        data = json.loads(_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise ReminderError(f'Cannot read {_path()}: {exc}', 500) from exc
    data = data if isinstance(data, dict) else {}
    items = [record for record in data.get('reminders') or [] if _valid(record)]
    deleted = [record for record in data.get('deleted') or []
               if _valid(record) and isinstance(record.get('deleted_at'), (int, float))]
    top = max((int(record['id'][1:]) for record in items + deleted), default=0)
    if prune_deleted:
        deleted = _recently_deleted(deleted, now)
    for record in items + deleted:
        notified = record.pop('notified', False)
        record.setdefault('occ', 1)
        record.setdefault('sent', list(CHANNELS) if notified else [])
        record.setdefault('tries', {})
        record.setdefault('retry_at', {})
        record.setdefault('sending', {})
        record.setdefault('failed', [])
    nxt = data.get('next') if isinstance(data.get('next'), int) and data.get('next') > top else top + 1
    return {'version': 1, 'next': nxt, 'reminders': items, 'deleted': deleted}


def _save(data, restored_id=None):
    # A restored done reminder keeps its original completion time and must survive this save.
    done = sorted((r for r in data['reminders'] if r['state'] == 'done'),
                  key=lambda r: (r['id'] == restored_id, r.get('done_at') or 0))
    drop = {r['id'] for r in done[:-DONE_KEPT]} if len(done) > DONE_KEPT else set()
    data['reminders'] = [r for r in data['reminders'] if r['id'] not in drop]
    fd, temp = tempfile.mkstemp(prefix='.reminders-', dir=folder())
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
            stream.write('\n')
        os.replace(temp, _path())
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _text(text):
    if not isinstance(text, str) or not ' '.join(text.split()):
        raise ReminderError('Write what to remind you about.')
    text = text.strip()
    if len(text) > TEXT_LIMIT:
        raise ReminderError(f'Keep the text to {TEXT_LIMIT} characters.')
    return text


def _due(value, now):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        raise ReminderError('Pick a time.')
    if value <= now:
        raise ReminderError('Pick a time in the future.')
    if value > now + MAX_AHEAD:
        raise ReminderError('Pick a time within five years.')
    return float(value)


def _id(rid):
    if not isinstance(rid, str) or not ID.fullmatch(rid.upper()):
        raise ReminderError('That reminder ID is not valid.')
    return rid.upper()


def _find(records, rid):
    return next((r for r in records if r['id'] == rid), None)


def _schedule(record, due):
    record.update(due=due, state='scheduled', fired_at=None, late=False, occ=record['occ'] + 1, sent=[],
                  tries={}, retry_at={}, sending={}, failed=[])
    record.pop('push', None)


def listing(now=None):
    """Live and recently deleted reminders, and the server time."""
    now = time.time() if now is None else now
    with _locked():
        data = _load(now)
    return {'reminders': [public(r) for r in data['reminders']],
            'deleted': [dict(public(r), deleted_at=r['deleted_at']) for r in data['deleted']], 'now': now}


def public(record):
    keys = ('id', 'text', 'due', 'created', 'by', 'state', 'fired_at', 'late', 'snoozes', 'done_at', 'link',
            'occ', 'failed')
    return {key: record.get(key) for key in keys}


def add(text, due, by='you', link=None, now=None):
    now = time.time() if now is None else now
    record = {'text': _text(text), 'due': _due(due, now), 'created': now, 'by': str(by or 'you')[:200],
              'state': 'scheduled', 'fired_at': None, 'late': False, 'snoozes': 0, 'done_at': None,
              'link': _clean_link(link), 'occ': 1, 'sent': [], 'tries': {}, 'retry_at': {}, 'sending': {}, 'failed': []}
    with _locked():
        data = _load(now)
        record['id'] = f'R{data["next"]}'
        data['next'] += 1
        data['reminders'].append(record)
        _save(data)
    return public(record)


def change(rid, action, body=None, now=None, *, by_id=False):
    """Edit, mark done, reopen, snooze by time or duration, or delete (returns None after delete)."""
    now = time.time() if now is None else now
    body = body or {}
    occ = None
    if action in ('done', 'snooze') and (not by_id or 'occ' in body):
        occ = body.get('occ')
        if type(occ) is not int:
            raise ReminderError('Include the reminder occurrence (occ) as an integer.')
    with _locked():
        rid = _id(rid)
        data = _load(now)
        record = _find(data['reminders'], rid)
        if record is None:
            raise ReminderError(f'No reminder {rid}. It may have been deleted.', 404)
        if occ is not None and occ != record['occ']:
            raise ReminderError('This reminder changed after that notice. Open Reminders to see it.', 409)
        if action == 'edit':
            if 'text' in body:
                record['text'] = _text(body['text'])
            if 'link' in body:
                record['link'] = _clean_link(body['link'])
            if 'due' in body and body['due'] != record['due']:
                _schedule(record, _due(body['due'], now))
        elif action == 'done':
            record.update(state='done', done_at=now)
        elif action == 'reopen':
            if record['state'] != 'done':
                return public(record)
            record['done_at'] = None
            if record['due'] > now:
                _schedule(record, record['due'])
            else:
                record['state'] = 'due'
                if record.get('fired_at') is None:
                    record.update(fired_at=now, late=now - record['due'] > LATE)
        elif action == 'snooze':
            if record['state'] == 'done':
                raise ReminderError('That reminder is done. Reopen it first.', 409)
            until = body.get('until')
            if 'for' in body:
                seconds = body['for']
                if type(seconds) is not int or not 60 <= seconds <= MAX_AHEAD:
                    raise ReminderError('Pick a snooze duration from one minute to five years.')
                until = now + seconds
            _schedule(record, _due(until, now))
            record['snoozes'] = int(record.get('snoozes') or 0) + 1
        elif action == 'delete':
            data['reminders'].remove(record)
            record['deleted_at'] = now
            data['deleted'] = _recently_deleted([record, *data['deleted']], now)
            _save(data)
            return None
        else:
            raise ReminderError('Unknown action.', 404)
        _save(data)
        return public(record)


def restore(rid, now=None):
    """Move a recently deleted reminder back, preserving its schedule and delivery state."""
    now = time.time() if now is None else now
    rid = _id(rid)
    with _locked():
        data = _load(now)
        existing = _find(data['reminders'], rid)
        if existing is not None:
            return public(existing)
        record = _find(data['deleted'], rid)
        if record is None:
            raise ReminderError('That reminder is no longer in Recently deleted.', 404)
        data['deleted'].remove(record)
        del record['deleted_at']
        data['reminders'].append(record)
        _save(data, restored_id=rid)
    return public(record)


def _push_id(endpoint):
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def deliver_push(reminder, subscriptions, send):
    """Save the first audience and each acceptance. Return None if the reminder changes and stops the batch."""
    rid, occ = reminder['id'], reminder['occ']
    endpoints = {endpoint: _push_id(endpoint) for endpoint in subscriptions()}
    with _locked():
        data = _load(time.time())
        record = _find(data['reminders'], rid)
        if record is None or record['state'] != 'due' or record['occ'] != occ or 'push' in record['sent']:
            return None
        if 'push' not in record:
            record['push'] = {'audience': sorted(set(endpoints.values())), 'to': []}
            _save(data)
        remaining = set(record['push']['audience']).difference(record['push']['to'])

    stopped = False

    def before_each(_endpoint):
        nonlocal stopped
        with _locked():
            record = _find(_load(time.time())['reminders'], rid)
            stopped = record is None or record['state'] != 'due' or record['occ'] != occ
        return not stopped

    def accepted(endpoint):
        target = endpoints[endpoint]
        with _locked():
            data = _load(time.time())
            record = _find(data['reminders'], rid) or _find(data['deleted'], rid)
            if record is not None and record['occ'] == occ:
                record['push']['to'].append(target)
                _save(data)
        remaining.discard(target)

    pending = {endpoint for endpoint, target in endpoints.items() if target in remaining}
    if pending:
        send(pending, accepted, before_each)
        if stopped:
            return None
    return not remaining or all(_push_id(endpoint) not in remaining for endpoint in subscriptions())


def tick(channels, now=None):
    """Fire due reminders and try ready channels. Return the (ID, channel) pairs that succeeded."""
    clock = time.time if now is None else lambda: now
    started = clock()
    with _locked():
        data = _load(started, prune_deleted=False)
        deleted = _recently_deleted(data['deleted'], started)
        changed = len(deleted) != len(data['deleted'])
        data['deleted'] = deleted
        for record in data['reminders']:
            if record['state'] == 'scheduled' and record['due'] <= started:
                record.update(state='due', fired_at=started, late=started - record['due'] > LATE)
                changed = True
        if changed:
            _save(data)
        pending = [(r['id'], r['occ'], channel) for r in data['reminders'] if r['state'] == 'due'
                   for channel in channels if channel not in r['sent'] and r['retry_at'].get(channel, 0) <= started]
    sent = []
    for rid, occ, channel in pending:
        with _locked():
            claimed_at = clock()
            data = _load(claimed_at)
            record = _find(data['reminders'], rid)
            if record is None or record['state'] != 'due' or record['occ'] != occ or channel in record['sent']:
                continue
            if (record['retry_at'].get(channel, 0) > claimed_at
                    or record['sending'].get(channel, {}).get('until', 0) > claimed_at):
                continue
            claim = {'until': claimed_at + LEASE}
            record['sending'][channel] = claim
            _save(data)
        try:
            ok = channels[channel](public(record))
        except Exception:  # one failing channel must never stop the tick
            ok = False
        with _locked():
            finished = clock()
            data = _load(finished)
            # Deletion can happen during a send. Undo must see its result as well.
            record = _find(data['reminders'], rid) or _find(data['deleted'], rid)
            if ok:
                sent.append((rid, channel))
            # A late result must not replace state saved by a retry that took over this claim.
            if record is None or record['occ'] != occ or record['sending'].get(channel) != claim:
                continue
            del record['sending'][channel]
            if ok is None:  # the reminder changed before or during the send
                _save(data)
                continue
            gave_up = False
            if ok:
                record['sent'].append(channel)
            else:
                tries = record['tries'].get(channel, 0) + 1
                record['tries'][channel] = tries
                gave_up = tries >= MAX_TRIES
                if gave_up:
                    record['sent'].append(channel)
                    record['failed'].append(channel)
                    record['retry_at'].pop(channel, None)
                else:
                    record['retry_at'][channel] = finished + BACKOFF[min(tries, len(BACKOFF)) - 1]
            _save(data)
            if gave_up:
                print(f'Reminder {rid} occurrence {occ}: {channel} failed after {tries} tries', file=sys.stderr)
    return sent


def when_text(at, now=None):
    """'10:00', 'tomorrow 10:00' or 'Wed 19 Nov 10:00', in ZONE."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(at, ZONE)
    today = datetime.fromtimestamp(now, ZONE).date()
    clock = day.strftime('%H:%M')
    if day.date() == today:
        return clock
    if day.date() == today + timedelta(days=1):
        return f'tomorrow {clock}'
    return day.strftime('%a %d %b ') + clock


def payload(reminder, silent=False, now=None):
    """The Web Push notification. Old service workers show only title and body, so both carry the text."""
    late = f' (late: was due {when_text(reminder["due"], now)})' if reminder.get('late') else ''
    return {'title': 'Reminder' + (' (late)' if late else ''), 'body': reminder['text'] + late,
            'tag': 'reminder-' + reminder['id'], 'priority': 'urgent', 'silent': silent, 'go': 'reminders',
            'reminder': reminder['id'], 'occ': reminder['occ']}


# Times the CLI understands: "2026-11-19 10:00", "2026-11-19T10:00+05:30", "10:00" (next), "tomorrow 10:00",
# "tomorrow", "in 90m", "in 2h", "in 1d". A time without an offset is in ZONE.
RELATIVE = re.compile(r'in\s+(\d{1,5})\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)', re.I)
CLOCK = re.compile(r'(\d{1,2}):(\d{2})')


def parse_when(text, now=None):
    now = time.time() if now is None else now
    text = ' '.join(str(text or '').split())
    base = datetime.fromtimestamp(now, ZONE)
    match = RELATIVE.fullmatch(text)
    if match:
        amount, unit = int(match[1]), match[2][0].lower()
        return now + amount * {'m': 60, 'h': 3600, 'd': 86400}[unit]
    try:
        lower = text.lower()
        if lower == 'tomorrow' or lower.startswith('tomorrow '):
            clock = CLOCK.fullmatch(text[8:].strip() or '09:00')
            if clock is None:
                raise ValueError
            day = base + timedelta(days=1)
            return day.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0).timestamp()
        clock = CLOCK.fullmatch(text)
        if clock:
            at = base.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0)
            return (at if at.timestamp() > now else at + timedelta(days=1)).timestamp()
        at = datetime.fromisoformat(text.replace(' ', 'T', 1) if 'T' not in text else text)
        return (at if at.tzinfo is not None else at.replace(tzinfo=ZONE)).timestamp()
    except (ValueError, OverflowError, OSError):
        raise ReminderError(f'Cannot read the time "{text}". Use "2026-11-19 10:00", "tomorrow 10:00" or "in 2h".') from None


# HTTP

def handles(path):
    path = path.split('?', 1)[0]
    return path == PREFIX or path in [f'{PREFIX}/{action}' for action in ACTIONS]


def get(handler):
    try:
        handler.respond(200, listing())
    except ReminderError as exc:
        handler.respond(exc.status, {'error': str(exc)})


def serve(handler, body):
    """POST /mobile/api/reminders/add {text, due, link?}, /{edit,done,reopen,snooze,delete,restore} {id, ...}."""
    action = handler.path.split('?', 1)[0][len(PREFIX) + 1:]
    try:
        if action == 'add':
            result = add(body.get('text'), body.get('due'), 'you', body.get('link'))
        elif action == 'restore':
            result = restore(body.get('id'))
        elif action in ACTIONS:
            result = change(body.get('id'), action, body)
        else:
            raise ReminderError('Not found', 404)
    except ReminderError as exc:
        handler.respond(exc.status, {'error': str(exc)})
        return
    except OSError as exc:
        handler.respond(500, {'error': f'Cannot save reminders: {exc.strerror or exc}'})
        return
    handler.respond(200, {'reminder': result})


class Ticker:
    """The server's scheduler: its own daemon thread, so a failing session scan never delays a reminder."""

    def __init__(self, channels, every=2.0):
        self.channels, self.every = channels, every
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='reminders', daemon=True)
            self.thread.start()

    def _run(self):
        handle = None
        acquired = False
        try:
            while True:
                try:
                    if handle is None:
                        directory = folder()
                        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                        handle = os.open(directory / '.scheduler', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    if not acquired:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            time.sleep(self.every)
                            continue
                        acquired = True
                    tick(self.channels)
                except Exception as exc:  # a bad file must not end the thread; the next tick tries again
                    print(f'reminders tick failed: {exc!r}', flush=True)
                time.sleep(self.every)
        finally:
            if handle is not None:
                os.close(handle)
