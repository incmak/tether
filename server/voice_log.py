"""Log bounded browser voice diagnostics without audio or transcript fields."""
from collections import deque
import json
import sys
import threading
import time

PATH = '/mobile/api/voice-log'
LIMIT = 4096
REPORTS = deque()
LOCK = threading.Lock()
FIELDS = {'kind': 20, 'event': 40, 'trail': 400, 'detail': 300, 'ua': 200}


def handles(path):
    return path.split('?', 1)[0] == PATH


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if size > LIMIT:
            handler.respond(413, {'error': 'Invalid request size.'})
            return
        if size <= 0:
            raise ValueError('Invalid request size.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        report = {key: value.strip()[:limit] for key, limit in FIELDS.items()
                  if isinstance(value := body.get(key), str)}
        if report.get('kind') not in ('dictation', 'friday'):
            raise ValueError('Invalid voice kind.')
        if type(body.get('ms')) is int:
            report['ms'] = max(0, min(600000, body['ms']))
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc)})
        return
    with LOCK:
        now = time.monotonic()
        while REPORTS and REPORTS[0] <= now - 60:
            REPORTS.popleft()
        if len(REPORTS) >= 60:
            handler.respond(429, {'error': 'Too many voice reports.'})
            return
        REPORTS.append(now)
    def quoted(key):
        return json.dumps(report.get(key, '').replace('\r', ' ').replace('\n', ' '), ensure_ascii=True)
    print(f'voice-log kind={quoted("kind")} event={quoted("event")} ms={report.get("ms", "-")} '
          f'trail={quoted("trail")} detail={quoted("detail")} ua={quoted("ua")}', file=sys.stderr, flush=True)
    handler.respond(204, b'')
