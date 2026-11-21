"""Private JSONL command history and its browser and Friday routes."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from heapq import nlargest
import hmac
import json
import logging
import os
import re
import secrets
from tempfile import NamedTemporaryFile
import threading
import time
from urllib.parse import parse_qs, urlsplit

try:
    from . import auth, paths
except ImportError:
    import auth
    import paths


PATH = '/mobile/api/command-history'
FRIDAY_PATH = PATH + '/friday'
DELETE_PATH = PATH + '/delete'
FRIDAY_HEADER = 'X-Tether-Friday'
MAX_BYTES = 16 * 1024
ID = re.compile(r'[A-Za-z0-9_-]{8,64}')
MONTH_FILE = re.compile(r'history-[0-9]{4}-(?:0[1-9]|1[0-2])\.jsonl')
FRIDAY_KEYS = {'id', 'text', 'answer', 'call', 'turn', 'route', 'result', 'project'}
ENTRY_KEYS = FRIDAY_KEYS | {'time', 'channel', 'source', 'device'}
CHANNELS = ('typed', 'dictation', 'conversation')
SOURCES = ('browser speech', 'backup recording', 'friday')
FAILED = ('refused', 'cancelled')
STATUSES = ('sent', 'queued', 'answered', *FAILED)
ROUTE_KEYS = {'kind', 'task', 'target', 'pane', 'conversation', 'ask'}
LOCK = threading.Lock()
LOG = logging.getLogger(__name__)


@contextmanager
def _locked():
    with LOCK:
        directory = paths.CONFIG / 'command'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        fd = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(fd)


def _valid_id(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _check_strings(fields, limits):
    for key, limit in limits.items():
        if key in fields and (not isinstance(fields[key], str) or len(fields[key]) > limit):
            raise ValueError(f'Invalid {key}.')


def _validate(entry):
    if not isinstance(entry, dict) or entry.keys() - ENTRY_KEYS:
        raise ValueError('Unknown history field.')
    if not _valid_id(entry.get('id')):
        raise ValueError('Invalid history id.')
    if entry.get('channel') not in CHANNELS or entry.get('source') not in SOURCES:
        raise ValueError('Invalid channel or source.')
    if 'turn' in entry and type(entry['turn']) is not int:
        raise ValueError('Invalid turn.')
    _check_strings(entry, {'text': 4000, 'answer': 4000, 'project': 200, 'call': 64, 'device': 200})
    if not (entry.get('text', '').strip() or entry.get('answer', '').strip()):
        raise ValueError('Enter text or an answer.')
    if 'route' in entry:
        route = entry['route']
        if not isinstance(route, dict) or route.keys() - ROUTE_KEYS:
            raise ValueError('Invalid route.')
        _check_strings(route, dict.fromkeys(route, 200))
    if 'result' in entry:
        result = entry['result']
        if (not isinstance(result, dict) or result.keys() - {'status', 'reason'}
                or result.get('status') not in STATUSES):
            raise ValueError('Invalid result.')
        _check_strings(result, {'reason': 500})


def _timestamp():
    return time.time_ns() // 1_000_000


def _month_file(directory, month):
    return directory / f'history-{month:%Y-%m}.jsonl'


def _month_files(directory):
    return sorted((path for path in directory.iterdir() if MONTH_FILE.fullmatch(path.name)),
                  reverse=True)


def _lines(path):
    unreadable = 0
    try:
        with path.open('rb') as file:
            for raw in file:
                try:
                    if len(raw) > MAX_BYTES:
                        raise ValueError('History line is too large.')
                    entry = json.loads(raw)
                    _validate(entry)
                    if type(entry.get('time')) is not int:
                        raise ValueError('Invalid time.')
                except (ValueError, RecursionError):
                    entry = None
                    unreadable += 1
                yield raw, entry
    finally:
        if unreadable:
            LOG.warning('Skipped %d unreadable command history lines in %s', unreadable, path.name)


def append(entry):
    _validate(entry)
    with _locked() as directory:
        entry = dict(entry, time=_timestamp())
        line = (json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(line) > MAX_BYTES:
            raise ValueError('History entry is too large.')
        month = datetime.fromtimestamp(entry['time'] // 1000, timezone.utc).replace(day=1)
        current = _month_file(directory, month)
        previous = _month_file(directory, month.replace(year=month.year - 1, month=12) if month.month == 1
                               else month.replace(month=month.month - 1))
        for path in (current, previous):
            if path.exists():
                for _, saved in _lines(path):
                    if saved is not None and saved['id'] == entry['id']:
                        return saved
        fd = os.open(current, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a+b') as file:
            os.fchmod(file.fileno(), 0o600)
            # A prior process may have stopped before writing the final newline.
            if file.tell():
                file.seek(-1, os.SEEK_END)
                if file.read(1) != b'\n':
                    file.write(b'\n')
            file.write(line)
        return entry


def _matches(entry, word):
    route = entry.get('route', {})
    return any(word in text.casefold() for text in (
        entry.get('text', ''), entry.get('answer', ''), route.get('target', ''), route.get('task', '')))


def _entry_key(entry):
    return entry['time'], entry['id']


def _cursor(before):
    # The representation stays internal to this API. Clients pass it back unchanged.
    stamp, sep, entry_id = before.partition(':')
    if not sep or not _valid_id(entry_id):
        raise ValueError('Invalid history cursor.')
    return int(stamp), entry_id


def search(query='', channel=None, failed=False, project=None, before=None, limit=50):
    older_than = _cursor(before) if before is not None else None
    limit = min(limit, 50)
    if limit <= 0:
        return {'entries': [], 'next': None}
    word = query.casefold()

    def matches(entry):
        return ((older_than is None or _entry_key(entry) < older_than)
                and (channel is None or entry['channel'] == channel)
                and (not failed or entry.get('result', {}).get('status') in FAILED)
                and (project is None or entry.get('project') == project)
                and (not word or _matches(entry, word)))

    entries = []
    with _locked() as directory:
        for path in _month_files(directory):
            candidates = (entry for _, entry in _lines(path) if entry is not None and matches(entry))
            entries.extend(nlargest(limit + 1 - len(entries), candidates, key=_entry_key))
            if len(entries) > limit:
                moment, entry_id = _entry_key(entries[limit - 1])
                return {'entries': entries[:limit], 'next': f'{moment}:{entry_id}'}
    return {'entries': entries, 'next': None}


def _delete(matches, *, all_entries=False):
    removed = 0
    with _locked() as directory:
        for path in _month_files(directory):
            if all_entries:
                removed += sum(entry is not None for _, entry in _lines(path))
                path.unlink()
                continue
            count = 0
            with NamedTemporaryFile(mode='wb', dir=directory, delete=False) as output:
                try:
                    for raw, entry in _lines(path):
                        if entry is not None and matches(entry):
                            count += 1
                        else:
                            output.write(raw)
                    kept = output.tell()
                    output.close()
                    if count:
                        if kept:
                            os.replace(output.name, path)
                        else:
                            path.unlink()
                finally:
                    if os.path.exists(output.name):
                        os.unlink(output.name)
            removed += count
    return removed


def delete_ids(ids):
    ids = set(ids)
    return _delete(lambda entry: entry['id'] in ids)


def delete_range(start_ms, end_ms):
    if type(start_ms) is not int or type(end_ms) is not int or start_ms > end_ms:
        raise ValueError('Invalid date range.')
    return _delete(lambda entry: start_ms <= entry['time'] <= end_ms)


def delete_project(project):
    if not isinstance(project, str):
        raise ValueError('Invalid project.')
    return _delete(lambda entry: entry.get('project') == project)


def delete_keyword(word):
    if not isinstance(word, str) or not word.strip():
        raise ValueError('Enter a keyword.')
    return _delete(lambda entry: _matches(entry, word.casefold()))


def delete_all():
    return _delete(None, all_entries=True)


def friday_token():
    # Readers take the same lock so they cannot see an empty file during creation.
    with _locked() as directory:
        path = directory / 'friday-token'
        try:
            return path.read_text(encoding='ascii').strip()
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text(encoding='ascii').strip()
        secret = secrets.token_urlsafe(32)
        with os.fdopen(fd, 'w', encoding='ascii') as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(secret)
        return secret


def _body(handler):
    size = int(handler.headers.get('Content-Length', '0'))
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid request size.')
    raw = handler.rfile.read(size)
    if len(raw) != size:
        raise ValueError('Incomplete request.')
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError('Send a JSON object.')
    return body


def friday_authorized(handler):
    token = handler.headers.get(FRIDAY_HEADER, '')
    return (auth.is_loopback(handler.client_address[0])
            and not any(name in handler.headers for name in ('Origin', 'Cookie', 'X-Forwarded-For', 'Forwarded'))
            and bool(token) and hmac.compare_digest(token.encode(), friday_token().encode()))


def friday_post(handler):
    try:
        if not friday_authorized(handler):
            handler.respond(403, {'error': 'Friday access refused.'})
            return
        body = _body(handler)
        if body.keys() - FRIDAY_KEYS:
            raise ValueError('Unknown Friday history field.')
        entry = append(dict(body, channel='conversation', source='friday'))
    except (ValueError, RecursionError):
        handler.respond(400, {'error': 'Invalid history entry.'})
    except OSError:
        handler.respond(503, {'error': 'Command history is unavailable.'})
    else:
        handler.respond(200, {'ok': True, 'id': entry['id']})


def get(handler):
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    param = lambda name, default=None: query.get(name, [default])[0]
    try:
        result = search(query=param('q', ''), channel=param('channel') or None, failed=param('failed') == '1',
                        project=param('project') or None, before=param('before') or None)
    except ValueError:
        handler.respond(400, {'error': 'Invalid history search.'})
    except OSError:
        handler.respond(503, {'error': 'Command history is unavailable.'})
    else:
        handler.respond(200, result)


def handles(path):
    return path == DELETE_PATH


def post(handler):
    try:
        body = _body(handler)
        fields = body.keys()
        if fields == {'ids'}:
            ids = body['ids']
            if not isinstance(ids, list) or len(ids) > 500 or not all(map(_valid_id, ids)):
                raise ValueError('Invalid history ids.')
            removed = delete_ids(ids)
        elif fields == {'from', 'to'}:
            removed = delete_range(body['from'], body['to'])
        elif fields == {'project'}:
            removed = delete_project(body['project'])
        elif fields == {'keyword'}:
            removed = delete_keyword(body['keyword'])
        elif fields == {'all'} and body['all'] is True:
            removed = delete_all()
        else:
            raise ValueError('Choose one way to delete history.')
    except (ValueError, RecursionError):
        handler.respond(400, {'error': 'Invalid history delete.'})
    except OSError:
        handler.respond(503, {'error': 'Command history is unavailable.'})
    else:
        handler.respond(200, {'removed': removed})
