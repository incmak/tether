"""Reply ratings: the thumbs up and thumbs down under an agent's reply in the chat (ui/src/ReplyFeedback.tsx).

One JSON file under the config folder holds the ratings by conversation (thread) and message id:
  {"<thread>": {"<message id>": {"rating": "up"|"down", "note": "...", "excerpt": "...", "time": 1763370000}}}

GET  /mobile/api/feedback?thread=T   -> {"ratings": {"<message id>": {"rating": ..., "note": ...}}}
POST /mobile/api/feedback {thread, message, rating: "up"|"down"|"", note?, excerpt?}
     An empty rating removes the row. The note says what was wrong; the page can also send it to the agent as a message.
The file keeps the newest MAX_ROWS rows; older rows drop first.
"""
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import paths

PATH = '/mobile/api/feedback'
RATINGS = ('up', 'down')
MAX_BODY = 8192
MAX_ROWS = 5000
NOTE = 2000
EXCERPT = 200
_ID = re.compile(r'^[\w.:%@-]{1,200}$')
_lock = threading.Lock()


def file():
    """Read at call time: tests move the config folder."""
    return paths.CONFIG / 'feedback.json'


def handles(path):
    return path.split('?', 1)[0] == PATH


def _read():
    try:
        data = json.loads(file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data):
    target = file()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=target.parent, prefix='.feedback-')
    try:
        with os.fdopen(handle, 'w') as out:
            json.dump(data, out, separators=(',', ':'))
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _trim(data):
    """Drop the oldest rows when the file holds more than MAX_ROWS."""
    rows = [(row.get('time', 0), thread, message) for thread, items in data.items() for message, row in items.items()]
    if len(rows) <= MAX_ROWS:
        return
    for _, thread, message in sorted(rows)[:len(rows) - MAX_ROWS]:
        data[thread].pop(message, None)
        if not data[thread]:
            data.pop(thread)


def ratings(thread):
    """The ratings of one conversation, by message id."""
    if not isinstance(thread, str) or not _ID.match(thread):
        raise ValueError('Invalid thread.')
    with _lock:
        items = _read().get(thread)
    if not isinstance(items, dict):
        return {}
    return {message: {'rating': row['rating'], 'note': row.get('note', '')} for message, row in items.items()
            if isinstance(row, dict) and row.get('rating') in RATINGS}


def rate(thread, message, rating, note='', excerpt='', now=None):
    """Save, change or (rating '') remove one rating. Returns the saved row, or None when it was removed."""
    if not isinstance(thread, str) or not _ID.match(thread) or not isinstance(message, str) or not _ID.match(message):
        raise ValueError('Invalid message.')
    if rating not in ('', *RATINGS):
        raise ValueError('Invalid rating.')
    with _lock:
        data = _read()
        items = data.get(thread) if isinstance(data.get(thread), dict) else {}
        if not rating:
            items.pop(message, None)
            row = None
        else:
            row = {'rating': rating, 'note': _text(note, NOTE), 'excerpt': _text(excerpt, EXCERPT), 'time': int(now if now is not None else time.time())}
            items[message] = row
        if items:
            data[thread] = items
        else:
            data.pop(thread, None)
        _trim(data)
        _write(data)
    return row and {'rating': row['rating'], 'note': row['note']}


def get(handler):
    thread = (parse_qs(urlsplit(handler.path).query).get('thread') or [''])[0]
    try:
        handler.respond(200, {'ratings': ratings(thread)})
    except ValueError as error:
        handler.respond(400, {'error': str(error)})


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= MAX_BODY:
            raise ValueError('Invalid request.')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        row = rate(body.get('thread'), body.get('message'), body.get('rating'), body.get('note'), body.get('excerpt'))
    except (ValueError, UnicodeError) as error:
        handler.respond(400, {'error': str(error) if isinstance(error, ValueError) and str(error).startswith('Invalid') else 'Invalid request.'})
        return
    handler.respond(200, {'saved': row})
