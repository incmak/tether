"""UI usage: which controls people click, which views they open, and how many clicks each view takes.

The page (ui/src/uiUsage.ts) sends small batches of events. Each event names a control by its place ("region"), its
label and its kind, never by the text a person typed, message text or the names of tabs and files in lists. The events
stay on this server, in one JSON-lines file under the cache folder. The 'uiUsage' preference turns the log off.

Event fields (all short strings or small numbers):
  k  'click' (a control was used), 'seen' (a control was on screen in this visit), 'open' (a menu or dialog opened)
  r  region: the open menu or dialog, or the labelled part of the page ("Sessions", "Tether")
  l  label: the control's accessible name, or the view's name for 'open'
  c  kind: button, link, tab, menuitem, switch, ... ('' for 'open')
  d  depth: 0 on the page, 1 in a menu or dialog, 2 in a menu or dialog opened from one, ...
  n  clicks to reach: the clicks in the current chain. A click on the page starts a chain; 0 means by keyboard.
  j  visit id: one page load, or a return after a long pause
  w  'phone' or 'wide'

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
_lock = threading.Lock()


def file():
    """Read at call time: tests move the cache folder."""
    return paths.CACHE / 'ui-usage.jsonl'


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


def clean(raw, now):
    """One stored event from one sent event, or None when the event is not valid."""
    if not isinstance(raw, dict) or raw.get('k') not in KINDS or raw.get('w') not in DEVICES:
        return None
    event = {'at': round(now, 3), 'k': raw['k'], 'r': _text(raw.get('r')), 'l': _text(raw.get('l')), 'c': _text(raw.get('c'))[:20],
             'd': _number(raw.get('d'), 9), 'n': _number(raw.get('n'), 99), 'j': _text(raw.get('j'))[:16], 'w': raw['w']}
    if not event['l'] or not event['j']:
        return None
    return event


def record(events, now=None):
    """Append the valid events. Returns how many were kept."""
    now = time.time() if now is None else now
    kept = [event for event in (clean(raw, now) for raw in events[:MAX_EVENTS]) if event]
    if not kept:
        return 0
    target = file()
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, 'a', encoding='utf-8') as out:
            os.chmod(target, 0o600)
            out.write(''.join(json.dumps(event, separators=(',', ':')) + '\n' for event in kept))
        if target.stat().st_size > MAX_FILE:
            _trim(target, now)
    return len(kept)


def _trim(target, now):
    """Keep the last KEEP_DAYS days, and at most half of MAX_FILE, so the file never grows without end."""
    lines = [line for line in target.read_text(encoding='utf-8').splitlines(True) if _at(line) >= now - KEEP_DAYS * 86400]
    while lines and sum(map(len, lines)) > MAX_FILE // 2:
        lines = lines[len(lines) // 4:]
    temp = target.with_suffix('.tmp')
    temp.write_text(''.join(lines), encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, target)


def _at(line):
    try:
        return float(json.loads(line).get('at', 0))
    except (ValueError, AttributeError):
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


def clear():
    with _lock:
        try:
            file().unlink()
        except FileNotFoundError:
            pass


def _merge_templates(events):
    """Labels such as "Close tab one", "Close tab two" name the same control with a different item. When one region
    and kind has three or more labels that share their first word, count them as one control: "Close …"."""
    groups = defaultdict(set)
    for event in events:
        if event['k'] != 'open' and ' ' in event['l']:
            groups[(event['w'], event['r'], event['c'], event['l'].split(' ', 1)[0])].add(event['l'])
    merged = {key for key, labels in groups.items() if len(labels) >= 3}
    for event in events:
        if event['k'] != 'open' and ' ' in event['l'] and (event['w'], event['r'], event['c'], event['l'].split(' ', 1)[0]) in merged:
            event['l'] = event['l'].split(' ', 1)[0] + ' …'
    return events


def report(days=30, device='', now=None):
    now = time.time() if now is None else now
    events = [event for event in load(now - days * 86400) if not device or event.get('w') == device]
    events = _merge_templates([event for event in events if isinstance(event.get('l'), str)])
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
        handler.respond(200, {'kept': 0, 'enabled': False})
        return
    handler.respond(200, {'kept': record(events), 'enabled': True})


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    json.dump(report(days, sys.argv[2] if len(sys.argv) > 2 else ''), sys.stdout, indent=2, default=str)
    print()
