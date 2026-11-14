"""Bounded feedback hints with atomic writes and cross-process project locks."""
from contextlib import contextmanager, ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

import decisions
import paths

_LOCK = threading.RLock()
DENY = re.compile(
    r"(?<!\w)(?:approv\w*|passkey|autonomy|level|permission|force-push|force push|--no-verify|"
    r"delete|rm -rf|drop table|money|pay|purchase|billing|secret|token|password|credential|release|publish|"
    r"without asking|don't ask|do not ask|no need to ask|skip review|skip the review|ignore the rules|"
    r"ignore the instructions|override)(?!\w)", re.I)


def path(pid):
    return decisions.path(pid).with_name('lessons.json')


def global_path():
    return paths.CONFIG / 'lessons-global.json'


def clean(text):
    if not isinstance(text, str):
        raise ValueError('Enter a lesson as text.')
    text = decisions._one_line(text, len(text))
    if not 12 <= len(text) <= 200:
        raise ValueError('Use 12 to 200 characters for a lesson.')
    if not text.lower().startswith('when '):
        raise ValueError('Start the lesson with "When ".')
    match = DENY.search(text)
    if match:
        raise ValueError(f'Lessons cannot include "{match[0]}".')
    return text


def _write(file, data):
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.lessons-', dir=file.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, file)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _locked(*files):
    with _LOCK, ExitStack() as stack:
        for file in sorted(set(files), key=str):
            file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(file) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
            stream = stack.enter_context(os.fdopen(fd, 'a'))
            os.fchmod(stream.fileno(), 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def enabled():
    try:
        value = json.loads((paths.CONFIG / 'learning.json').read_text())
        return value['enabled'] if isinstance(value, dict) and type(value.get('enabled')) is bool else True
    except (OSError, ValueError):
        return True


def set_enabled(value):
    if type(value) is not bool:
        raise ValueError('Choose whether learning is on.')
    file = paths.CONFIG / 'learning.json'
    with _locked(file):
        _write(file, {'enabled': value})


def _read(file):
    try:
        value = json.loads(file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('lessons'), list):
        raise ValueError('The lesson file is invalid.')
    rows = value['lessons']
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not re.fullmatch(r'[0-9a-f]{8}', row['id'])
                or row.get('kind') not in ('avoid', 'keep') or row.get('source') not in ('user', 'reviewer', 'agent')
                or row.get('agent') not in ('claude', 'codex', '')
                or type(row.get('hits')) is not int or row['hits'] < 1
                or any(type(row.get(key)) not in (int, float) for key in ('created', 'last_hit'))
                or type(row.get('proposed_global')) is not bool):
            raise ValueError('The lesson file is invalid.')
        clean(row.get('text'))
    return rows


def _save(file, rows):
    _write(file, {'version': 1, 'lessons': rows})


def _rank(row):
    return (('user', 'reviewer', 'agent').index(row['source']),
            ('avoid', 'keep').index(row['kind']), -row['hits'], -row['last_hit'])


def _ranked(rows):
    return [dict(row, stuck=row['hits'] >= 3) for row in sorted(rows, key=_rank)]


def entries(pid):
    return _ranked(_read(path(pid)))


def global_entries():
    return _ranked(_read(global_path()))


def _room(rows, limit):
    if len(rows) < limit:
        return
    candidates = [row for row in rows if row['source'] != 'user']
    if not candidates:
        where = 'This project has 60 lessons' if limit == 60 else 'The global list has 30 lessons'
        raise ValueError(where + '. Remove one in Tether first.')
    rows.remove(max(candidates, key=_rank))


def _words(text):
    return set(re.findall(r'\b[^\W\d_]{3,}\b', text.lower()))


def add(pid, text, kind, source, agent, force_new=False):
    file, text = path(pid), clean(text)
    if kind not in ('avoid', 'keep') or source not in ('user', 'reviewer', 'agent') or agent not in ('claude', 'codex', ''):
        raise ValueError('Choose a lesson kind, source and agent.')
    with _locked(file):
        rows = _read(file)
        words = _words(text)
        similar = [row for row in sorted(rows, key=_rank) if row['kind'] == kind
                   and len(words & _words(row['text'])) / max(1, len(words | _words(row['text']))) >= 0.5]
        if similar and not force_new:
            return {'status': 'similar', 'candidates': _ranked(similar[:3])}
        _room(rows, 60)
        identifier = secrets.token_hex(4)
        while any(row['id'] == identifier for row in rows):
            identifier = secrets.token_hex(4)
        now = time.time()
        row = dict(id=identifier, text=text, kind=kind, source=source, agent=agent, hits=1,
                   created=now, last_hit=now, proposed_global=False)
        _save(file, rows + [row])
        return {'status': 'added', 'lesson': row}


def _change(pid, identifier, change):
    file = path(pid)
    with _locked(file):
        rows = _read(file)
        row = next((row for row in rows if row['id'] == identifier), None)
        if row is None:
            return None
        change(row)
        _save(file, rows)
        return dict(row)


def reinforce(pid, identifier):
    return _change(pid, identifier, lambda row: row.update(hits=row['hits'] + 1, last_hit=time.time()))


def edit(pid, identifier, text):
    text = clean(text)
    return _change(pid, identifier, lambda row: row.update(text=text))


def propose_global(pid, identifier):
    return _change(pid, identifier, lambda row: row.update(proposed_global=True))


def _remove(file, identifier):
    with _locked(file):
        rows = _read(file)
        kept = [row for row in rows if row['id'] != identifier]
        if len(kept) == len(rows):
            return False
        _save(file, kept)
        return True


def remove(pid, identifier):
    return _remove(path(pid), identifier)


def remove_global(identifier):
    return _remove(global_path(), identifier)


def promote(pid, identifier):
    file, target = path(pid), global_path()
    with _locked(file, target):
        rows, global_rows = _read(file), _read(target)
        row = next((row for row in rows if row['id'] == identifier), None)
        if row is None:
            return None
        # A retry after a failed project write must not duplicate or evict a global lesson.
        existing = next((entry for entry in global_rows if entry['id'] == identifier), None)
        if existing is not None and existing != row:
            raise ValueError('A global lesson has the same ID.')
        if existing is None:
            _room(global_rows, 30)
            _save(target, global_rows + [row])
        _save(file, [entry for entry in rows if entry['id'] != identifier])
        return dict(row)


def for_prompt(pid):
    result, size = [], 0
    for row in global_entries() + entries(pid):
        if len(result) == 8:
            break
        if size + len(row['text']) <= 1200:
            result.append(row)
            size += len(row['text'])
    return result
