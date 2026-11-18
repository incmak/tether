"""Lesson candidates (B212): lessons an agent proposes and the person has not decided yet.

A candidate is not a lesson. No agent gets it with a prompt. The person adds it (it becomes a trial lesson from
the agent) or dismisses it in Settings › Agents › Daily ops report. Each project keeps at most LIMIT candidates in
<project folder>/lesson-candidates.json; a proposal past that is refused, never swapped for an older one.
"""
import json
import re
import secrets
import time

import decisions
import lessons

LIMIT = 20


def path(pid):
    return decisions.path(pid).with_name('lesson-candidates.json')


def _read(file):
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    rows = data.get('candidates') if isinstance(data, dict) and data.get('version') == 1 else None
    if not isinstance(rows, list):
        raise ValueError('The lesson candidates file is invalid.')
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not re.fullmatch(r'[0-9a-f]{8}', row['id'])
                or row.get('kind') not in ('avoid', 'keep') or row.get('agent') not in ('claude', 'codex', '')
                or type(row.get('at')) not in (int, float)
                or any(not isinstance(row.get(key), str) or len(row[key]) > limit
                       for key, limit in (('quote', 160), ('tab', 60), ('pane', 12)))):
            raise ValueError('The lesson candidates file is invalid.')
        lessons.clean(row.get('text'))
    return rows


def _save(file, rows):
    lessons._write(file, {'version': 1, 'candidates': rows})


def entries(pid):
    return sorted(_read(path(pid)), key=lambda row: -row['at'])


def _alike(a, b):
    first, second = lessons._words(a), lessons._words(b)
    return len(first & second) / max(1, len(first | second)) >= 0.5


def propose(pid, text, kind, agent, quote='', tab='', pane=''):
    """{'status': 'proposed', 'candidate'} or {'status': 'similar', 'text'} when a lesson or a candidate of this
    kind says the same already."""
    file, text = path(pid), lessons.clean(text)
    quote, tab = lessons._text(quote, 160), lessons._text(tab, 60)
    if kind not in ('avoid', 'keep') or agent not in ('claude', 'codex', '') or not lessons._pane(pane, empty=True):
        raise ValueError('Choose a lesson kind and agent.')
    with lessons._locked(file):
        rows = _read(file)
        known = [row['text'] for row in rows if row['kind'] == kind]
        known += [row['text'] for row in lessons.entries(pid) + lessons.global_entries() if row['kind'] == kind]
        same = next((other for other in known if _alike(text, other)), None)
        if same is not None:
            return {'status': 'similar', 'text': same}
        if len(rows) >= LIMIT:
            raise ValueError(f'This project has {LIMIT} lesson candidates that wait for the person. Propose no more.')
        identifier = secrets.token_hex(4)
        while any(row['id'] == identifier for row in rows):
            identifier = secrets.token_hex(4)
        row = dict(id=identifier, text=text, kind=kind, agent=agent, quote=quote, tab=tab, pane=pane, at=time.time())
        _save(file, rows + [row])
        return {'status': 'proposed', 'candidate': row}


def _take(pid, identifier):
    file = path(pid)
    with lessons._locked(file):
        rows = _read(file)
        row = next((row for row in rows if row['id'] == identifier), None)
        if row is not None:
            _save(file, [other for other in rows if other['id'] != identifier])
        return row


def dismiss(pid, identifier):
    return _take(pid, identifier) is not None


def accept(pid, identifier):
    """The person adds the candidate: it becomes a trial lesson from the agent. Returns the lesson, or None when the
    candidate is gone."""
    file = path(pid)
    with lessons._locked(file):  # held to the end: two Add taps on one candidate make one lesson
        rows = _read(file)
        row = next((row for row in rows if row['id'] == identifier), None)
        if row is None:
            return None
        # The lesson is written first, so a failure never loses the proposal. A retry after a failed candidate save
        # finds the lesson that the first try wrote and adds no second one.
        # (also when the person moved it to the list for all projects in between) and adds no second one. A full
        # list refuses: Add never pushes another lesson out.
        lesson = next((other for other in lessons.entries(pid) + lessons.global_entries() if other['source'] == 'agent'
                       and other['kind'] == row['kind'] and other['text'] == row['text']), None)
        if lesson is None:
            lesson = lessons.add(pid, row['text'], row['kind'], 'agent', row['agent'], True, quote=row['quote'],
                                 tab=row['tab'], pane=row['pane'], evict=False)['lesson']
        _save(file, [other for other in rows if other['id'] != identifier])
        return lesson
