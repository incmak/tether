"""Append-only records of changes made through Tether and its tools."""
import contextlib
import contextvars
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from itertools import chain, groupby
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

try:
    import fcntl
except ImportError:
    fcntl = None

import paths

PATH = '/mobile/api/audit'
RETENTION_DAYS = 90
KINDS = {
    'setting.codex_level': 'settings', 'setting.codex_run': 'settings',
    'setting.autonomy_level': 'settings', 'setting.reviewers': 'settings',
    'setting.advisor': 'settings', 'setting.claude_advisor': 'settings',
    'setting.learning': 'settings', 'setting.automation': 'settings',
    'passkey.add': 'settings', 'passkey.remove': 'settings',
    'tab.start': 'tabs', 'tab.close': 'tabs', 'tab.restart': 'tabs',
    'review.link': 'reviews', 'review.stop': 'reviews', 'review.verdict': 'reviews',
    'backlog.add': 'backlog', 'backlog.start': 'backlog', 'backlog.done': 'backlog',
    'backlog.move': 'backlog', 'backlog.remove': 'backlog',
    'lesson.add': 'lessons', 'lesson.remove': 'lessons', 'lesson.dispute': 'lessons',
    'advisor.ask': 'advisor', 'approval.approve': 'approvals', 'approval.deny': 'approvals',
    'auto.stale_ask': 'automation', 'auto.undo': 'automation',
}
MISSING = object()
_SECRET_KEYS = ('token', 'secret', 'password', 'credential', 'assertion', 'approval',
                'signature', 'public_key', 'cookie', 'key_material')
_SOURCES = ('ui', 'agent-cli', 'cli', 'hook', 'server', 'unknown')
_AGENT_COMMANDS = {'claude': 'claude', 'codex': 'codex', 'agy': 'antigravity'}
_AGENT_LABELS = {'claude': 'Claude tab', 'codex': 'Codex tab', 'antigravity': 'Antigravity tab'}
_PANE = re.compile(r'%[0-9]+')
_ID = re.compile(r'[0-9]{16}-[0-9a-f]{6}')
_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl')
_ACTOR = contextvars.ContextVar('audit_actor', default=None)
_LOCK = threading.Lock()
_TAB_LOCK = threading.Lock()
_TABS = {}
_PRUNED = set()
_FACETS = None


@contextlib.contextmanager
def acting(source, *, device=None, pane=None, tab=None):
    token = _ACTOR.set({'source': source, 'device': device, 'pane': pane, 'tab': tab})
    try:
        yield
    finally:
        _ACTOR.reset(token)


def _browser(ua):
    for marker, name in (('edg/', 'Edge'), ('chrome/', 'Chrome'), ('firefox/', 'Firefox'), ('safari/', 'Safari')):
        if marker in ua:
            return name
    return None


def device_name(ua):
    ua = (ua or '').lower()
    if 'python-urllib' in ua or 'curl/' in ua or ua == 'curl':
        return None
    browser = _browser(ua) or 'Browser'
    for marker, name in (('iphone', 'iPhone'), ('ipad', 'iPad'), ('android', 'Android'),
                         ('mac', 'Mac'), ('windows', 'Windows'), ('linux', 'Linux')):
        if marker in ua:
            return f'{browser} on {name}'
    return f'{browser} on Computer'


def actor_from_request(headers):
    client = headers.get('X-Tether-Client')
    pane = headers.get('X-Tether-Pane') or ''
    ua = headers.get('User-Agent') or ''
    if client == 'agent-cli' and _PANE.fullmatch(pane):
        return {'source': 'agent-cli', 'pane': pane}
    if client == 'web':
        return {'source': 'ui', 'device': device_name(ua)}
    device = device_name(ua) if _browser(ua.lower()) else None
    return {'source': 'unknown', 'device': device or ua[:60] or 'No user agent'}


def _tab_name(pane):
    with _TAB_LOCK:
        now = time.monotonic()
        expires, metadata = _TABS.get(pane, (0, (None, None)))
        if now < expires:
            return metadata
        metadata = (None, None)
        try:
            result = subprocess.run([paths.TMUX, 'display-message', '-p', '-t', pane,
                                     '#{@chat_name}\t#{pane_current_command}'],
                                    capture_output=True, text=True, timeout=1)
            if result.returncode == 0:
                name, separator, command = result.stdout.rstrip('\r\n').rpartition('\t')
                if separator:
                    metadata = (name.strip() or None, _AGENT_COMMANDS.get(command.strip()))
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
        _TABS[pane] = (now + 60, metadata)
        return metadata


def _resolve_actor(actor):
    if actor is None:
        actor = _ACTOR.get()
    if actor is None:
        pane = os.environ.get('TMUX_PANE', '')
        actor = {'source': 'agent-cli', 'pane': pane} if _PANE.fullmatch(pane) else {'source': 'cli'}
    source = actor.get('source')
    result = {'source': source if source in _SOURCES else 'unknown'}
    for key in ('device', 'tab'):
        value = actor.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    agent = actor.get('agent')
    if isinstance(agent, str) and agent in _AGENT_LABELS:
        result['agent'] = agent
    pane = actor.get('pane')
    if isinstance(pane, str) and _PANE.fullmatch(pane):
        result['pane'] = pane
        if 'tab' not in result or 'agent' not in result:
            name, agent = _tab_name(pane)
            if name:
                result.setdefault('tab', name)
            if agent:
                result.setdefault('agent', agent)
    return result


def capture_actor():
    """Resolve the actor now for deferred work that can outlive the request or pane."""
    try:
        return _resolve_actor(None)
    except Exception:
        return {'source': 'unknown'}


def actor_name(actor):
    name = actor.get('tab') or actor.get('device')
    if name:
        return name
    if actor['source'] in ('agent-cli', 'hook') or actor.get('pane'):
        return _AGENT_LABELS.get(actor.get('agent'), 'An agent tab')
    return actor['source']


def folder():
    return paths.CONFIG / 'audit'


_folder = folder


@contextlib.contextmanager
def _locked(folder):
    with _LOCK:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(folder / '.lock', os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)


def _day_files(folder):
    try:
        candidates = list(folder.iterdir())
    except FileNotFoundError:
        return
    for path in sorted(candidates, reverse=True):
        if not _DAY.fullmatch(path.name):
            continue
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        yield day, path


def _prune(folder, today):
    key = (folder, today)
    if key in _PRUNED:
        return
    _PRUNED.add(key)
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for day, path in _day_files(folder):
        if day < cutoff and not path.is_dir():
            path.unlink(missing_ok=True)


def _warn(message):
    try:
        print(f'Audit log: {message}', file=sys.stderr)
    except Exception:
        pass  # An unavailable stderr must not fail the action either.


def prune():
    """Prune on startup as well as on the first write of each UTC day."""
    try:
        folder = _folder()
        if folder.exists():
            with _locked(folder):
                _prune(folder, datetime.fromtimestamp(time.time(), timezone.utc).date())
    except Exception as exc:
        _warn(f'could not prune ({type(exc).__name__}).')


def _scalar(value):
    return type(value) in (str, int, bool, type(None)) or (type(value) is float and math.isfinite(value))


def clip(text, limit):
    """Shorten audit text to its cap, preserving a word break if it fits."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    if limit <= 0:
        return ''
    prefix = text[:limit - 1]
    boundary = re.search(r'\s\S*\Z', prefix)
    if boundary and boundary.start() * 2 >= limit:
        prefix = re.sub(r'[\s,;:–—-]+\Z', '', prefix[:boundary.start()])
    return prefix + '…'


def _unordered(path):
    try:
        return path.stem.encode() in path.with_name('.lock').read_bytes().splitlines()
    except FileNotFoundError:
        return False


def _mark_clock_reversal(lock, path, entry):
    # Use the lock file to flag days whose writes are out of timestamp order.
    with contextlib.closing(_entries(path, newest_first=True)) as entries:
        previous = next(entries, None)
    if previous and previous['id'][:16] > entry['id'][:16] and not _unordered(path):
        marker = (path.stem + '\n').encode()
        written = os.write(lock, marker)
        if written != len(marker):
            os.ftruncate(lock, os.fstat(lock).st_size - written)
            raise OSError('Short audit ordering write.')


def record(kind, summary, *, old=MISSING, new=MISSING, project=None, actor=None, undo=False, **detail):
    """Write one action. An audit failure must never fail the action itself."""
    try:
        if kind not in KINDS:
            _warn('unknown kind.')
            return None
        setting = KINDS[kind] == 'settings'
        if setting and old is not MISSING and new is not MISSING and old == new:
            return None
        entry = {'kind': kind, 'summary': clip(' '.join(summary.split()), 200), 'actor': _resolve_actor(actor)}
        if setting:
            for key, value in (('old', old), ('new', new)):
                if _scalar(value):
                    entry[key] = clip(value, 120)
        if isinstance(project, str) and project:
            entry['project'] = project
        if undo:
            entry['undo'] = True
        safe = {}
        for key, value in detail.items():
            if not _scalar(value) or any(word in key.casefold() for word in _SECRET_KEYS):
                continue
            safe[key] = clip(value, 200)
            if len(safe) == 8:
                break
        if safe:
            entry['detail'] = safe
        folder = _folder()
        with _locked(folder) as lock:
            at = float(time.time())
            day = datetime.fromtimestamp(at, timezone.utc).date()
            _prune(folder, day)
            entry.update(id=f'{int(at * 1000):016d}-{secrets.token_hex(3)}', at=at)
            line = (json.dumps(entry, allow_nan=False) + '\n').encode('utf-8')
            path = folder / f'{day}.jsonl'
            _mark_clock_reversal(lock, path, entry)
            fd = os.open(path,
                         os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                written = os.write(fd, line)
                if written != len(line):
                    # Remove the failed fragment so the next append starts on a new line.
                    os.ftruncate(fd, os.fstat(fd).st_size - written)
                    raise OSError('Short audit write.')
            finally:
                os.close(fd)
        return entry
    except Exception as exc:
        _warn(f'could not record ({type(exc).__name__}).')
        return None


def _valid(entry):
    if not isinstance(entry, dict):
        return False
    if not all(isinstance(entry.get(key), str) for key in ('id', 'kind', 'summary')):
        return False
    actor = entry.get('actor')
    detail = entry.get('detail', {})
    at = entry.get('at')
    return (_ID.fullmatch(entry['id']) and entry['kind'] in KINDS
            and type(at) in (int, float) and 0 <= at <= 253402300799  # last second of year 9999
            and isinstance(actor, dict) and actor.get('source') in _SOURCES
            and all(isinstance(value, str) for value in actor.values())
            and isinstance(entry.get('project', ''), str)
            and _scalar(entry.get('old')) and _scalar(entry.get('new'))
            and isinstance(detail, dict) and all(_scalar(value) for value in detail.values()))


def _reverse_lines(stream):
    """Read an append-only file from its end, one block at a time."""
    position = stream.seek(0, os.SEEK_END)
    fragment = b''
    while position:
        size = min(position, 8192)
        position -= size
        stream.seek(position)
        fragment, *lines = (stream.read(size) + fragment).split(b'\n')
        yield from reversed(lines)
    if fragment:
        yield fragment


def _entries(path, *, newest_first=False):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return
        stream = path.open('rb')
    except FileNotFoundError:  # Retention may run while a reader lists the files.
        return
    with stream:
        for line in _reverse_lines(stream) if newest_first else stream:
            if not line:
                continue
            try:
                entry = json.loads(line)
                if _valid(entry):
                    yield entry
            except (ValueError, UnicodeError, RecursionError):
                continue


def _newest_entries(path):
    with contextlib.closing(_entries(path, newest_first=True)) as stream:
        first = next(stream, None)  # Snapshot the file end before reading the ordering marker.
        if first is None:
            return
        entries = chain((first,), stream)
        if _unordered(path):
            yield from sorted(entries, key=lambda entry: entry['id'], reverse=True)
        else:
            for _, same_millisecond in groupby(entries, key=lambda entry: entry['id'][:16]):
                yield from sorted(same_millisecond, key=lambda entry: entry['id'], reverse=True)


def _text_values(entry):
    return (entry['summary'], entry['kind'], entry.get('project', ''), *entry['actor'].values(),
            *(str(entry[key]) for key in ('old', 'new') if key in entry),
            *(str(value) for value in entry.get('detail', {}).values()))


def query(text=None, kinds=None, project=None, actor=None, since=None, until=None, limit=50, before=None):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Use a limit from 1 to 200.')
    words = (text or '').casefold().split()
    kinds = set(kinds or ())
    project = project.casefold() if project else None
    actor = actor.casefold() if actor else None
    matches = []
    for _, path in _day_files(_folder()):
        for entry in _newest_entries(path):
            kind = entry['kind']
            if before and entry['id'] >= before:
                continue
            if (since is not None and entry['at'] < since) or (until is not None and entry['at'] >= until):
                continue
            if kinds and kind not in kinds and KINDS[kind] not in kinds:
                continue
            if project and entry.get('project', '').casefold() != project:
                continue
            if actor:
                entry_actor = entry['actor']
                if not any(actor in value.casefold() for value in (*entry_actor.values(), actor_name(entry_actor))):
                    continue
            if words:
                values = [value.casefold() for value in _text_values(entry)]
                if not all(any(word in value for value in values) for word in words):
                    continue
            if len(matches) == limit:
                return {'entries': matches, 'next': matches[-1]['id']}
            matches.append(entry)
    return {'entries': matches, 'next': None}


def facets():
    global _FACETS
    folder = _folder()
    files = []
    for _, path in _day_files(folder):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            files.append((path.name, info.st_size, info.st_mtime_ns))
    key = (folder, files)
    cached = _FACETS
    if cached is not None and cached[0] == key:
        return cached[1]
    kinds = Counter()
    projects, actors = set(), set()
    for name, _, _ in files:
        for entry in _entries(folder / name):
            kinds[entry['kind']] += 1
            if entry.get('project'):
                projects.add(entry['project'])
            actors.add(actor_name(entry['actor']))
    result = {'kinds': [{'id': kind, 'group': KINDS[kind], 'count': kinds[kind]} for kind in KINDS if kinds[kind]],
              'projects': sorted(projects, key=str.casefold), 'actors': sorted(actors, key=str.casefold)}
    _FACETS = key, result
    return result


def handles(path):
    return path.split('?', 1)[0] == PATH


def _epoch(value, name):
    if value is None:
        return None
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except ValueError:
        pass
    raise ValueError(f'Use epoch seconds for {name}.')


def get(handler):
    try:
        params = {key: values[0] for key, values in parse_qs(urlsplit(handler.path).query,
                                                           keep_blank_values=True).items()}
        try:
            limit = int(params.get('limit', 50))
        except ValueError:
            raise ValueError('Use a limit from 1 to 200.') from None
        result = query(text=params.get('q'), kinds=[kind.strip() for kind in params.get('kind', '').split(',') if kind.strip()],
                       project=params.get('project'), actor=params.get('actor'), since=_epoch(params.get('since'), 'since'),
                       until=_epoch(params.get('until'), 'until'), limit=limit, before=params.get('before'))
        if params.get('facets') == '1':
            result['facets'] = facets()
        result['retention_days'] = RETENTION_DAYS
    except ValueError as exc:
        handler.respond(400, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read the audit log.'})
    else:
        handler.respond(200, result)
