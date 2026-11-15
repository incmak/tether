"""UI usage: which controls people click, which views they open, and how many clicks each view takes.

The page (ui/src/uiUsage.ts) sends small batches of events. Each event names a control by its place ("region"), its
label and its kind. The page never sends typed text or the visible text of rows in lists. Labels can still hold a name
(aria-label="Restore <tab name>"), so before the server stores a label it keeps only the words that the Tether UI code
has (ui-words.txt, written by the UI build) and makes every other word "…": "Restore …". The events stay on this
server, in one JSON-lines file under the cache folder, for 90 days. The 'uiUsage' preference turns the log off.

Event fields (all short strings or small numbers):
  k  'click' (a control was used), 'seen' (a control was on screen in this visit), 'open' (a menu or dialog opened)
  r  region: the open menu or dialog, or the labelled part of the page ("Sessions", "Tether")
  l  label: the control's accessible name, or the view's name for 'open'
  c  kind: button, link, tab, menuitem, switch, ... ('' for 'open')
  d  depth: 0 on the page, 1 in a menu or dialog, 2 in a menu or dialog opened from one, ...
  n  clicks to reach: the clicks in the current chain. A click on the page starts a chain; 0 means by keyboard.
  j  visit id: one page load, or a return after a long pause
  w  'phone' or 'wide'
  (batch) gen: the log generation from the last answer. After a clear the server drops batches with an older one.
  t  when it happened, in the page's milliseconds. The batch has the page's time when it was sent ('now'), so the
     server finds the real time without trusting the page's clock.

report() turns the log into the lists in Settings > UI usage: the most used controls, controls on the page that are
rarely used (move them into a menu), controls in menus that are often used (move them up), controls that were seen
but never used, and the clicks it takes to reach each view. Run `python3 ui_usage.py` for the same report as JSON.
"""
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import paths
import prefs_sync

PATH = '/mobile/api/ui-usage'
CLEAR = PATH + '/clear'
KINDS = ('click', 'seen', 'open')
DEVICES = ('phone', 'wide')
MAX_BODY = 60000  # a keepalive fetch can carry 64 KB
MAX_EVENTS = 200
TEXT = 60
KEEP_DAYS = 90
MAX_FILE = 8 * 1024 * 1024
MIN_VISITS = 5  # a control must be on screen in this many visits before the report suggests a move
RARE = 0.02  # clicked in at most 2% of the visits where it was on screen
POPULAR = 0.2  # clicked in at least 20% of all visits
DEEP = 3  # clicks to reach that count as deep
LIST = 15
MAX_AGE = 86400  # an event sent later than this after it happened gets the time it arrived
WORDS = 'ui-words.txt'
_TOKEN = re.compile(r'[^\W\d_]+|\d+')
_lock = threading.Lock()
_words = {'key': None, 'set': frozenset()}
CHECK_EVERY = 3600  # a full check for old events at most this often: a late batch can append older events
_state = {'checked': float('-inf')}


def file():
    """Read at call time: tests move the cache folder."""
    return paths.CACHE / 'ui-usage.jsonl'


def cleared_file():
    return paths.CACHE / 'ui-usage.cleared'


def words_file():
    """The words of the Tether UI code, from the deployed build (server.py CHAT_UI_ROOT)."""
    root = os.environ.get('TMUX_CHAT_DIST') or Path(__file__).resolve().parent.parent / 'ui' / 'dist'
    return Path(root) / WORDS


def words():
    """The UI words, or None when the build has no word list. Read again when the file changes."""
    target = words_file()
    try:
        stat = target.stat()
    except OSError:
        return None
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    if _words['key'] != key:
        _words.update(key=key, set=frozenset(target.read_text(encoding='utf-8').lower().split()))
    return _words['set']


def redact(text, known):
    """Keep the words in known; other words become '…', numbers become '#'. "Restore My tab 2" -> "Restore … #"."""
    def word(match):
        token = match.group()
        return '#' if token.isdigit() else token if token.lower() in known else '…'
    text = _TOKEN.sub(word, text)
    return re.sub(r'…(?:\W*…)+', '…', text)


def enabled():
    return prefs_sync.load()['prefs'].get('uiUsage') is not False


def handles(path):
    return path.split('?', 1)[0] in (PATH, CLEAR)


def _text(value):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', value).strip()[:TEXT]


def _number(value, top):
    return max(0, min(top, value)) if type(value) is int else 0


def _when(raw, now, sent):
    """The server time of the event: now minus how long before the send it happened, by the page's own clock."""
    at = raw.get('t')
    if type(sent) is not int or type(at) is not int or not 0 <= sent - at <= MAX_AGE * 1000:
        return now
    return now - (sent - at) / 1000


def clean(raw, now, known, sent=None):
    """One stored event from one sent event, or None when the event is not valid."""
    if not isinstance(raw, dict) or raw.get('k') not in KINDS or raw.get('w') not in DEVICES:
        return None
    event = {'at': round(_when(raw, now, sent), 3), 'k': raw['k'], 'r': redact(_text(raw.get('r')), known),
             'l': redact(_text(raw.get('l')), known), 'c': _text(raw.get('c'))[:20],
             'd': _number(raw.get('d'), 9), 'n': _number(raw.get('n'), 99), 'j': _text(raw.get('j'))[:16], 'w': raw['w']}
    if not event['l'] or not event['j']:
        return None
    return event


def generation():
    """Changes when the log is cleared. A batch that carries an older generation left the page before the clear."""
    return str(_cleared())


def record(events, now=None, sent=None, gen=None):
    """Append the valid events. Returns how many were kept. Without the UI word list nothing is kept: a label could
    then hold a name."""
    now = time.time() if now is None else now
    known = words()
    if known is None:
        return 0
    with _lock:
        since = _cleared()
        if gen is not None and gen != str(since):
            return 0
        kept = [event for event in (clean(raw, now, known, sent) for raw in events[:MAX_EVENTS]) if event and event['at'] >= since]
        target = file()
        if kept:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'a', encoding='utf-8') as out:
                os.chmod(target, 0o600)
                out.write(''.join(json.dumps(event, separators=(',', ':')) + '\n' for event in kept))
        _expire(now)
    return len(kept)


def _cleared():
    try:
        return float(cleared_file().read_text())
    except (OSError, ValueError):
        return 0


def expire(now=None):
    with _lock:
        _expire(time.time() if now is None else now)


def _expire(now):
    """Remove events older than KEEP_DAYS, and old events when the file is too big. Call with _lock held. Reads the
    whole file at most once per CHECK_EVERY, since a late batch can put an old event after newer ones."""
    target = file()
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return
    if size > MAX_FILE:
        _trim(target, now)
        return
    if now - _state['checked'] < CHECK_EVERY:
        return
    _state['checked'] = now
    with open(target, encoding='utf-8') as source:
        if any(_at(line) < now - KEEP_DAYS * 86400 for line in source):
            _trim(target, now)


def _trim(target, now):
    """Keep the last KEEP_DAYS days, and at most half of MAX_FILE, so the file never grows without end."""
    lines = sorted((line for line in target.read_text(encoding='utf-8').splitlines(True) if _at(line) >= now - KEEP_DAYS * 86400), key=_at)
    while lines and sum(map(len, lines)) > MAX_FILE // 2:
        lines = lines[len(lines) // 4:]
    temp = target.with_suffix('.tmp')
    temp.write_text(''.join(lines), encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, target)


def _at(line):
    try:
        return float(json.loads(line).get('at', 0))
    except (ValueError, AttributeError, TypeError):
        return 0


def load(since):
    try:
        with open(file(), encoding='utf-8') as source:
            lines = source.readlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('at', 0) >= since:
            events.append(event)
    return events


def clear(now=None):
    """Remove the log. Events that happened before now and arrive later (a page's queue) are dropped too."""
    with _lock:
        cleared_file().parent.mkdir(parents=True, exist_ok=True)
        cleared_file().write_text(str(time.time() if now is None else now))
        try:
            file().unlink()
        except FileNotFoundError:
            pass


def report(days=30, device='', now=None):
    now = time.time() if now is None else now
    events = [event for event in load(now - days * 86400) if not device or event.get('w') == device]
    events = [event for event in events if isinstance(event.get('l'), str)]
    visits = {event.get('j') for event in events}
    controls = {}
    views = {}
    for event in events:
        if event['k'] == 'open':
            view = views.setdefault(event['l'], {'view': event['l'], 'from': defaultdict(int), 'visits': 0, 'steps': []})
            view['visits'] += 1
            view['steps'].append(event.get('n', 0))
            if event.get('r'):
                view['from'][event['r']] += 1
            continue
        key = (event['r'], event['l'], event['c'])
        control = controls.setdefault(key, {'region': event['r'], 'label': event['l'], 'kind': event['c'], 'clicks': 0,
                                            'depths': defaultdict(int), 'seenIn': set(), 'clickedIn': set(), 'steps': []})
        control['depths'][event.get('d', 0)] += 1
        if event['k'] == 'seen':
            control['seenIn'].add(event.get('j'))
        else:
            control['clicks'] += 1
            control['clickedIn'].add(event.get('j'))
            control['seenIn'].add(event.get('j'))
            control['steps'].append(event.get('n', 0))
    rows = [_row(control, len(visits)) for control in controls.values()]
    on_page = [row for row in rows if row['depth'] == 0]
    hidden = [row for row in rows if row['depth'] > 0]
    total = len(visits)
    result = {
        'enabled': enabled(), 'days': days, 'device': device, 'visits': total,
        'clicks': sum(row['clicks'] for row in rows), 'controls': len(rows), 'ready': total >= MIN_VISITS,
        'top': sorted((row for row in rows if row['clicks']), key=lambda row: (-row['clicks'], row['label']))[:LIST],
        # On the page in many visits, but almost never used: a menu or a "More" popover can hold it.
        'demote': sorted((row for row in on_page if row['seen'] >= MIN_VISITS and row['rate'] <= RARE),
                         key=lambda row: (row['rate'], -row['seen'], row['label']))[:LIST],
        # In a menu or dialog, and used in many visits or only after many clicks: a place on the page may suit it.
        'promote': sorted((row for row in hidden if len(row['_clickedIn']) >= 3 and total and
                           (len(row['_clickedIn']) / total >= POPULAR or (row['steps'] or 0) >= DEEP)),
                          key=lambda row: (-row['clicks'], row['label']))[:LIST],
        # In a menu or dialog that people open, but never used there.
        'unused': sorted((row for row in hidden if not row['clicks'] and row['seen'] >= 3),
                         key=lambda row: (-row['seen'], row['region'], row['label']))[:LIST],
        'views': sorted((_view(view) for view in views.values()), key=lambda view: (-view['visits'], view['view']))[:LIST * 2],
    }
    for name in ('top', 'demote', 'promote', 'unused'):
        result[name] = [{key: value for key, value in row.items() if not key.startswith('_')} for row in result[name]]
    return result


def _row(control, visits):
    depth = max(control['depths'].items(), key=lambda item: (item[1], -item[0]))[0]
    seen = len(control['seenIn'])
    return {'region': control['region'], 'label': control['label'], 'kind': control['kind'], 'depth': depth,
            'clicks': control['clicks'], 'seen': seen, 'rate': round(len(control['clickedIn']) / seen, 3) if seen else 0,
            'steps': statistics.median(control['steps']) if control['steps'] else None, '_clickedIn': control['clickedIn']}


def _view(view):
    steps = view['steps']
    keyboard = sum(1 for step in steps if step == 0)
    clicked = [step for step in steps if step > 0]
    top_from = max(view['from'].items(), key=lambda item: item[1])[0] if view['from'] else ''
    return {'view': view['view'], 'visits': view['visits'], 'steps': statistics.median(clicked) if clicked else None,
            'most': max(clicked) if clicked else None, 'keyboard': keyboard, 'from': top_from}


def get(handler):
    """GET ?days=7|30|90&device=phone|wide: the report."""
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(handler.path).query)
    days = (query.get('days') or ['30'])[0]
    device = (query.get('device') or [''])[0]
    if days not in ('7', '30', '90') or device not in ('', *DEVICES):
        handler.respond(400, {'error': 'Invalid request.'})
        return
    expire()
    handler.respond(200, report(int(days), device))


def post(handler):
    """POST {events: [...]} adds events; POST /clear removes the whole log."""
    path = handler.path.split('?', 1)[0]
    if path == CLEAR:
        clear()
        handler.respond(200, {'cleared': True})
        return
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= MAX_BODY:
            raise ValueError
        body = json.loads(handler.rfile.read(size))
        events = body.get('events') if isinstance(body, dict) else None
        if not isinstance(events, list):
            raise ValueError
    except (ValueError, UnicodeError):
        handler.respond(400, {'error': 'Invalid request.'})
        return
    if not enabled():
        expire()
        handler.respond(200, {'kept': 0, 'enabled': False, 'gen': generation()})
        return
    gen = body.get('gen')
    kept = record(events, sent=body.get('now'), gen=gen if isinstance(gen, str) else None)
    handler.respond(200, {'kept': kept, 'enabled': True, 'gen': generation()})


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    json.dump(report(days, sys.argv[2] if len(sys.argv) > 2 else ''), sys.stdout, indent=2, default=str)
    print()
