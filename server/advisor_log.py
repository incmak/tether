"""The advisor log of a project, written by agent_advisor_cli.py and read by the Project sheet.

Two files in the project folder, one entry per ask, the last KEEP entries each:
- advisor-log.md: one short line per ask, which agents read. The question and the answer are cut.
- advisor-log.jsonl: the full ask (question, answers, evidence, error), so the Project sheet can show it.

Asks from before the JSONL file exist only as Markdown lines. The sheet shows them as cut entries.
Each append holds an flock on .advisor-log.lock, so asks from two tabs at once both stay in both files."""
import contextlib
import fcntl
import json
import os
import re
import secrets
import time

import advisor
import tether_zone

KEEP = 200
LINE_QUESTION, LINE_ANSWER = 200, 300  # the cuts in the Markdown line
# Storage policy: the most characters Tether keeps of one text field. It is above one tab reply (helper_tabs.MAX_REPLY,
# 100,000) and evidence from two replies, so those stay whole. A longer field (for example a question from 'ask -',
# which reads stdin of any size) is cut here, named in the entry's cut list, and the sheet says so.
FULL = 250_000
PREVIEW_QUESTION, PREVIEW_ANSWER = 300, 400  # the list shows one and two lines; the rest loads on open
STATUSES = ('decided', 'ask_user', 'timeout', 'error', 'off')
CUT_FIELDS = ('question', 'answer', 'evidence', 'error', 'advisors')
TEXT_FIELDS = ('confidence', 'evidence', 'error')  # optional: a wrong type reads as ''
ADVISOR_FIELDS = ('role', 'agent', 'model', 'answer', 'confidence')
ID = re.compile(r'[0-9a-f]{12}')
LAST_TIME = 253402300799  # 9999-12-31 23:59:59 UTC
LINE = re.compile(rf'- ({tether_zone.STAMP_PATTERN}) \[([a-z_]+)\] Q: (.*?) -> A: (.*) \((\S+) (\S+)\)')


def _flat(text, limit):
    return ' '.join(str(text or '').split())[:limit]


def _cut(text, field, cut):
    text = str(text or '')
    if len(text) <= FULL:
        return text
    if field not in cut:
        cut.append(field)
    return text[:FULL - 1] + '…'


def line(entry, *, stamp=None):
    """The Markdown line of an entry, the same for the writer and for the reader that matches lines to entries."""
    stamp = stamp or entry.get('stamp') or tether_zone.stamp(entry['time'])
    return (f'- {stamp} [{entry["status"]}] '
            f'Q: {_flat(entry["question"], LINE_QUESTION)} -> A: {_flat(entry["answer"], LINE_ANSWER)} '
            f'({entry["agent"]} {entry["model"]})')


def make(question, result, agent, model, now=None):
    """One entry from an ask and its result (agent_advisor_cli.result_for). The raw replies stay out. cut names each
    field that was over FULL, so the sheet can say so."""
    cut = []
    entry = {'id': secrets.token_hex(6), 'time': int(time.time() if now is None else now), 'status': result['status'],
             'question': _cut(question, 'question', cut), 'answer': _cut(result.get('answer', ''), 'answer', cut),
             'confidence': result.get('confidence', ''), 'evidence': _cut(result.get('evidence', ''), 'evidence', cut),
             'error': _cut(result.get('error', ''), 'error', cut), 'agent': agent, 'model': model,
             'advisors': [{'role': item.get('role', ''), 'agent': item.get('agent', ''), 'model': item.get('model') or '',
                           'answer': _cut(item.get('answer', ''), 'advisors', cut), 'confidence': item.get('confidence', '')}
                          for item in result.get('advisors', [])]}
    entry['cut'] = cut
    # Keep the Markdown stamp with its full record so the reader can still match them after a zone change.
    entry['stamp'] = tether_zone.stamp(entry['time'])
    return entry


def _lines(path):
    if path.is_symlink():
        return []  # Tether writes plain files here; a link could point anywhere
    try:
        return path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []


def _valid(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('id'), str) and ID.fullmatch(entry['id'])
            and type(entry.get('time')) is int and 0 <= entry['time'] <= LAST_TIME and entry.get('status') in STATUSES
            and all(isinstance(entry.get(key), str) for key in ('question', 'answer', 'agent', 'model')))


def _text(value):
    return value if isinstance(value, str) else ''


def _normal(entry):
    """The entry with every detail field in the shape the sheet reads, or None when a required field is bad.
    Optional fields of a wrong type become empty, so a damaged line never reaches the sheet as null or a number."""
    if not _valid(entry):
        return None
    stamp = entry.get('stamp')
    if 'stamp' in entry and (not isinstance(stamp, str) or not re.fullmatch(tether_zone.STAMP_PATTERN, stamp)):
        entry = dict(entry)
        entry.pop('stamp')
    advisors = entry.get('advisors')
    cut = entry.get('cut')
    return dict(entry, **{key: _text(entry.get(key)) for key in TEXT_FIELDS},
                advisors=[{key: _text(item.get(key)) for key in ADVISOR_FIELDS}
                          for item in (advisors if isinstance(advisors, list) else []) if isinstance(item, dict)],
                cut=[name for name in (cut if isinstance(cut, list) else []) if name in CUT_FIELDS])


def _entries(folder):
    found = []
    for text in _lines(folder / 'advisor-log.jsonl'):
        try:
            entry = _normal(json.loads(text))
        except (ValueError, RecursionError, OverflowError):
            continue  # a damaged line (bad JSON, or nested too deep to read): skip it, keep the rest
        if entry:
            found.append(entry)
    return found


@contextlib.contextmanager
def _locked(folder):
    """One writer at a time, across processes: each ask runs in its own agent_advisor_cli.py."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(folder / '.advisor-log.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)  # also releases the lock


def append(folder, entry):
    """Add the entry to both files under one lock. Each file keeps its last KEEP entries."""
    with _locked(folder):
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
        when = int(tether_zone.parse(found[1]))
    except (ValueError, OverflowError):
        return None
    return {'id': '', 'time': when, 'status': found[2], 'question': found[3], 'answer': found[4],
            'agent': found[5], 'model': found[6], 'full': False}


def listing(folder):
    """Every kept ask, newest first. JSONL entries have an id and open in full; older Markdown lines have none."""
    entries = _entries(folder)
    # Earlier JSONL records had no stamp and were paired with Markdown in host time.
    known = {line(entry, stamp=entry.get('stamp') or time.strftime(tether_zone.FORMAT, time.localtime(entry['time'])))
             for entry in entries}
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
