"""Bounded feedback hints with atomic writes and cross-process project locks."""
from contextlib import contextmanager, ExitStack
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

import decisions
import paths
import projects

DAYS_GAP = 3
NEW_COUNT = 5
DAYS_PERIOD = 14
DAYS_STALE = 30
DAY = 86400
PANE = re.compile(r'%[0-9]+')

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
        row.setdefault('status', 'confirmed' if row['hits'] >= 2 else 'trial')
        row.setdefault('quote', '')
        row.setdefault('tab', '')
        row.setdefault('first_pane', '')
        row.setdefault('disputes', [])
        row.setdefault('reviewed_at', None)
        if (row['status'] not in ('trial', 'confirmed')
                or any(not isinstance(row[key], str) or len(row[key]) > limit
                       for key, limit in (('quote', 160), ('tab', 60)))
                or not _pane(row['first_pane'], empty=True)
                or not isinstance(row['disputes'], list) or len(row['disputes']) > 20
                or row['reviewed_at'] is not None and not _timestamp(row['reviewed_at'])):
            raise ValueError('The lesson file is invalid.')
        panes = set()
        for item in row['disputes']:
            if (not isinstance(item, dict) or set(item) != {'pane', 'reason', 'at'}
                    or not _pane(item.get('pane')) or item['pane'] in panes
                    or not isinstance(item.get('reason'), str) or len(item['reason']) > 200
                    or not _timestamp(item.get('at'))):
                raise ValueError('The lesson file is invalid.')
            panes.add(item['pane'])
    return rows


def _timestamp(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _pane(value, empty=False):
    return isinstance(value, str) and (empty and value == '' or bool(PANE.fullmatch(value)))


def _text(value, limit):
    if not isinstance(value, str):
        raise ValueError('Enter lesson details as text.')
    return decisions._one_line(value, limit)


def _save(file, rows):
    _write(file, {'version': 1, 'lessons': rows})


def _rank(row):
    return (('user', 'reviewer', 'agent').index(row['source']),
            ('avoid', 'keep').index(row['kind']), -row['hits'], -row['last_hit'])


def _ranked(rows):
    return [dict(row, stuck=row['hits'] >= 3, disputed=disputed(row)) for row in sorted(rows, key=_rank)]


def disputed(row):
    return bool(row['disputes'])


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


def add(pid, text, kind, source, agent, force_new=False, quote='', tab='', pane=''):
    file, text = path(pid), clean(text)
    quote, tab = _text(quote, 160), _text(tab, 60)
    if not _pane(pane, empty=True):
        raise ValueError('Choose a writing pane.')
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
                   created=now, last_hit=now, proposed_global=False, status='trial', quote=quote,
                   tab=tab, first_pane=pane, disputes=[], reviewed_at=None)
        _save(file, rows + [row])
        return {'status': 'added', 'lesson': row}


def _change(file, identifier, change, fallback=None):
    files = (file, fallback) if fallback is not None else (file,)
    with _locked(*files):
        for target in files:
            rows = _read(target)
            row = next((row for row in rows if row['id'] == identifier), None)
            if row is not None:
                change(row)
                _save(target, rows)
                return dict(row)
    return None


def reinforce(pid, identifier, pane=''):
    if not _pane(pane, empty=True):
        raise ValueError('Choose a writing pane.')
    def change(row):
        now = time.time()
        row.update(hits=row['hits'] + 1, last_hit=now)
        if (pane and row['first_pane'] and pane != row['first_pane']) or now - row['created'] >= DAY:
            row['status'] = 'confirmed'
    return _change(path(pid), identifier, change)


def _keep(row):
    row.update(status='confirmed', disputes=[], reviewed_at=time.time())


def keep(pid, identifier):
    return _change(path(pid), identifier, _keep)


def keep_global(identifier):
    return _change(global_path(), identifier, _keep)


def edit(pid, identifier, text):
    text = clean(text)
    def change(row):
        row['text'] = text
        _keep(row)
    return _change(path(pid), identifier, change)


def dispute(pid, identifier, pane, reason):
    if not _pane(pane):
        raise ValueError('Choose a writing pane.')
    reason = _text(reason, 200)
    def change(row):
        if any(item['pane'] == pane for item in row['disputes']):
            raise ValueError('This tab already disputed this lesson.')
        if len(row['disputes']) >= 20:
            raise ValueError('This lesson has 20 disputes. Review it in Tether first.')
        row['disputes'].append({'pane': pane, 'reason': reason, 'at': time.time()})
    return _change(path(pid), identifier, change, fallback=global_path())


def propose_global(pid, identifier):
    return _change(path(pid), identifier, lambda row: row.update(proposed_global=True))


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
        if row['status'] != 'confirmed' or disputed(row):
            raise ValueError('Only a confirmed lesson can be used in all projects.')
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
    rows = global_entries() + entries(pid)
    confirmed = sorted((row for row in rows if row['status'] == 'confirmed' and not disputed(row)), key=_rank)
    trial = sorted((row for row in rows if row['status'] == 'trial' and not disputed(row)),
                   key=lambda row: -row['created'])
    trial += sorted((row for row in rows if row['status'] == 'confirmed' and disputed(row)), key=_rank)
    reserved = [row for row in trial if row['status'] == 'trial'][:2]
    result, size = list(reserved), sum(len(row['text']) for row in reserved)
    for row in confirmed + trial:
        if any(row is entry for entry in result) or len(result) == 8:
            continue
        if size + len(row['text']) <= 1200:
            result.append(row)
            size += len(row['text'])
    return {'confirmed': [row for row in confirmed if any(row is entry for entry in result)],
            'trial': [row for row in trial if any(row is entry for entry in result)]}


def all_entries():
    rows = [dict(row, project=None, project_name=None) for row in global_entries()]
    skipped = []
    root = paths.TETHER / 'projects'
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        pid = folder.name
        if not decisions.PROJECT_ID.fullmatch(pid) or not folder.is_dir():
            continue
        try:
            entries_here = entries(pid)
            name = projects.meta(pid)['name'] or pid
        except (OSError, ValueError):
            skipped.append(pid)
            continue
        rows.extend(dict(row, project=pid, project_name=name) for row in entries_here)
    return {'lessons': rows, 'skipped': skipped}


def review_path():
    return paths.CONFIG / 'lessons-review.json'


def _review_state(file, now):
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {'version': 1, 'last_review': now, 'snoozed_until': 0}
        _write(file, data)
    if (not isinstance(data, dict) or data.get('version') != 1
            or not all(_timestamp(data.get(key)) for key in ('last_review', 'snoozed_until'))):
        raise ValueError('The lesson review file is invalid.')
    return data


def review(now=None):
    now = time.time() if now is None else now
    file = review_path()
    with _locked(file):
        state = _review_state(file, now)
    data = all_entries()
    rows, last = data['lessons'], state['last_review']
    new = sum(row['created'] > last for row in rows)
    disputes = sum(disputed(row) for row in rows)
    reason = None
    if rows and now >= state['snoozed_until']:
        if disputes:
            reason = 'disputed'
        elif now - last >= DAYS_GAP * DAY and new >= NEW_COUNT:
            reason = 'new'
        elif now - last >= DAYS_PERIOD * DAY:
            reason = 'time'
    groups = {key: [] for key in ('disputed', 'stuck', 'stale', 'new', 'other')}
    words = [_words(row['text']) for row in rows]
    for index, row in enumerate(rows):
        flags = []
        for other_index, other in enumerate(rows):
            if index == other_index or (row['project'] is not None and other['project'] is not None
                                        and row['project'] != other['project']):
                continue
            if len(words[index] & words[other_index]) / max(1, len(words[index] | words[other_index])) >= 0.5:
                flags.append({'type': 'repeat' if row['kind'] == other['kind'] else 'conflict', 'with': other['id']})
        key = ('disputed' if disputed(row) else 'stuck' if row['hits'] >= 3 else
               'stale' if row['status'] == 'trial' and now - row['created'] > DAYS_STALE * DAY else
               'new' if row['created'] > last else 'other')
        groups[key].append(dict(row, flags=flags))
    return {'due': reason is not None, 'reason': reason, 'total': len(rows), 'new': new, 'disputed': disputes,
            'last_review': last, 'snoozed_until': state['snoozed_until'],
            'groups': [{'key': key, 'lessons': group} for key, group in groups.items() if group],
            'skipped': data['skipped']}


def _review_change(now, snooze):
    now = time.time() if now is None else now
    file = review_path()
    with _locked(file):
        state = _review_state(file, now)
        if snooze:
            state['snoozed_until'] = now + DAYS_GAP * DAY
        else:
            state.update(last_review=now, snoozed_until=0)
        _write(file, state)


def review_done(now=None):
    _review_change(now, False)


def review_snooze(now=None):
    _review_change(now, True)
