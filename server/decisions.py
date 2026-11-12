"""Opt-in, bounded saved answers for one existing Tether project."""
from datetime import datetime
import hashlib
import os
import re
import tempfile
import threading
import unicodedata

from codex_level import PROJECT_ID
import paths

HEADER = '# Answers you saved for this project (Tether Settings › Project lists them)\n\n'
LIMIT = 100
_LOCK = threading.Lock()
_ENTRY = re.compile(r'^- (\d{4}-\d{2}-\d{2}) \[([0-9a-f]{8})\]: Q: (.{1,200}?) -> A: (.{1,500})$')


def path(pid):
    if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
        raise ValueError('Choose a project.')
    folder = paths.TETHER / 'projects' / pid
    if not folder.is_dir():
        raise ValueError('Choose an existing project.')
    return folder / 'decisions.md'


def _one_line(text, limit):
    text = ''.join(char for char in str(text)
                   if char.isspace() or unicodedata.category(char) not in ('Cc', 'Cf', 'Cs'))
    return ' '.join(text.split())[:limit]


def _id(date, question, answer):
    return hashlib.sha1(f'{date}|{question}|{answer}'.encode('utf-8')).hexdigest()[:8]


def _entries(file):
    try:
        lines = file.read_text(encoding='utf-8', errors='replace').splitlines()
    except FileNotFoundError:
        return []
    result = []
    for line in lines:
        match = _ENTRY.fullmatch(line)
        if match:
            date, entry_id, question, answer = match.groups()
            # The question or answer can contain the delimiter. Its digest identifies the saved split.
            combined = question + ' -> A: ' + answer
            offset = combined.find(' -> A: ')
            while offset >= 0:
                left, right = combined[:offset], combined[offset + len(' -> A: '):]
                if 0 < len(left) <= 200 and 0 < len(right) <= 500 and _id(date, left, right) == entry_id:
                    question, answer = left, right
                    break
                offset = combined.find(' -> A: ', offset + 1)
            try:
                datetime.strptime(date, '%Y-%m-%d')
            except ValueError:
                continue
            result.append(dict(id=entry_id, date=date, question=question, answer=answer))
    return result


def entries(pid):
    file = path(pid)
    with _LOCK:
        return list(reversed(_entries(file)))


def _write(file, rows):
    fd, temporary = tempfile.mkstemp(prefix='.decisions-', dir=file.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(HEADER)
            for row in rows:
                stream.write(f'- {row["date"]} [{row["id"]}]: Q: {row["question"]} -> A: {row["answer"]}\n')
        os.replace(temporary, file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record(pid, question, answer, now=None):
    file = path(pid)
    if not isinstance(question, str) or not isinstance(answer, str):
        raise ValueError('Enter a question and an answer.')
    question, answer = _one_line(question, 200), _one_line(answer, 500)
    if not question or not answer:
        raise ValueError('Enter a question and an answer.')
    date = (datetime.now() if now is None else now).strftime('%Y-%m-%d')
    entry_id = _id(date, question, answer)
    entry = dict(id=entry_id, date=date, question=question, answer=answer)
    with _LOCK:
        rows = _entries(file)
        if not any(row['id'] == entry_id for row in rows):
            _write(file, (rows + [entry])[-LIMIT:])
    return entry


def delete(pid, entry_id):
    file = path(pid)
    with _LOCK:
        rows = _entries(file)
        kept = [row for row in rows if row['id'] != entry_id]
        if len(rows) == len(kept):
            return False
        if kept:
            _write(file, kept)
        else:
            file.unlink()
    return True
