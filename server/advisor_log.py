"""The advisor log of a project, written by agent_advisor_cli.py and read by the Project sheet.

Two files in the project folder, one entry per ask, the last KEEP entries each:
- advisor-log.md: one short line per ask, which agents read. The question and the answer are cut.
- advisor-log.jsonl: the full ask (question, answers, evidence, error), so the Project sheet can show it.

Asks from before the JSONL file exist only as Markdown lines. The sheet shows them as cut entries."""
import json
import re
import secrets
import time

import advisor

KEEP = 200
LINE_QUESTION, LINE_ANSWER = 200, 300  # the cuts in the Markdown line
FULL = 8000  # the most characters Tether keeps of one text field
PREVIEW_QUESTION, PREVIEW_ANSWER = 300, 400  # the list shows one and two lines; the rest loads on open
STATUSES = ('decided', 'ask_user', 'timeout', 'error', 'off')
ID = re.compile(r'[0-9a-f]{12}')
LINE = re.compile(r'- (\d{4}-\d\d-\d\d \d\d:\d\d) \[([a-z_]+)\] Q: (.*?) -> A: (.*) \((\S+) (\S+)\)')


def _flat(text, limit):
    return ' '.join(str(text or '').split())[:limit]


def _cut(text, limit=FULL):
    text = str(text or '')
    return text if len(text) <= limit else text[:limit - 1] + '…'


def line(entry):
    """The Markdown line of an entry, the same for the writer and for the reader that matches lines to entries."""
    return (f'- {time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["time"]))} [{entry["status"]}] '
            f'Q: {_flat(entry["question"], LINE_QUESTION)} -> A: {_flat(entry["answer"], LINE_ANSWER)} '
            f'({entry["agent"]} {entry["model"]})')


def make(question, result, agent, model, now=None):
    """One entry from an ask and its result (agent_advisor_cli.result_for). The raw replies stay out."""
    return {'id': secrets.token_hex(6), 'time': int(time.time() if now is None else now), 'status': result['status'],
            'question': _cut(question), 'answer': _cut(result.get('answer', '')),
            'confidence': result.get('confidence', ''), 'evidence': _cut(result.get('evidence', '')),
            'error': _cut(result.get('error', ''), 2000), 'agent': agent, 'model': model,
            'advisors': [{'role': item.get('role', ''), 'agent': item.get('agent', ''), 'model': item.get('model') or '',
                          'answer': _cut(item.get('answer', '')), 'confidence': item.get('confidence', '')}
                         for item in result.get('advisors', [])]}


def _lines(path):
    if path.is_symlink():
        return []  # Tether writes plain files here; a link could point anywhere
    try:
        return path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []


def _valid(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('id'), str) and ID.fullmatch(entry['id'])
            and isinstance(entry.get('time'), int) and entry.get('status') in STATUSES
            and all(isinstance(entry.get(key), str) for key in ('question', 'answer', 'agent', 'model')))


def _entries(folder):
    found = []
    for text in _lines(folder / 'advisor-log.jsonl'):
        try:
            entry = json.loads(text)
        except ValueError:
            continue  # a damaged line: skip it, keep the rest
        if _valid(entry):
            found.append(entry)
    return found


def append(folder, entry):
    """Add the entry to both files. Each file keeps its last KEEP entries."""
    lines = _lines(folder / 'advisor-log.md') + [line(entry)]
    advisor._write(folder / 'advisor-log.md', '\n'.join(lines[-KEEP:]) + '\n')
    kept = [json.dumps(item) for item in _entries(folder)[-(KEEP - 1):]] + [json.dumps(entry)]
    advisor._write(folder / 'advisor-log.jsonl', '\n'.join(kept) + '\n')


def _legacy(text):
    """A Markdown line that has no JSONL entry, or None when it does not match the format."""
    found = LINE.fullmatch(text)
    if not found or found[2] not in STATUSES:
        return None
    try:
        when = int(time.mktime(time.strptime(found[1], '%Y-%m-%d %H:%M')))
    except (ValueError, OverflowError):
        return None
    return {'id': '', 'time': when, 'status': found[2], 'question': found[3], 'answer': found[4],
            'agent': found[5], 'model': found[6], 'full': False}


def listing(folder):
    """Every kept ask, newest first. JSONL entries have an id and open in full; older Markdown lines have none."""
    entries = _entries(folder)
    known = {line(entry) for entry in entries}
    older = [item for item in map(_legacy, (text for text in _lines(folder / 'advisor-log.md') if text not in known)) if item]
    shown = [{'id': entry['id'], 'time': entry['time'], 'status': entry['status'],
              'question': _flat(entry['question'], PREVIEW_QUESTION), 'answer': _flat(entry['answer'], PREVIEW_ANSWER),
              'agent': entry['agent'], 'model': entry['model'], 'full': True} for entry in entries]
    # The sort is stable: inside one second or one minute, the later line in the file comes first.
    return sorted(reversed(older + shown), key=lambda item: item['time'], reverse=True)


def find(folder, entry_id):
    if not isinstance(entry_id, str) or not ID.fullmatch(entry_id):
        return None
    return next((entry for entry in _entries(folder) if entry['id'] == entry_id), None)
