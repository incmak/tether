"""Read Codex's queued follow-up preview from a visible tmux frame."""
from itertools import pairwise
import re
import subprocess
import sys
import threading

import paths
from terminal_input import _agent_version, _terminal_columns, codex_composer_row

TITLE = 'Queued follow-up inputs'
HEADER = '• ' + TITLE
ENTRY = '  ↳ '
INDENT = '    '
CUT = INDENT + '…'
HINT = re.compile(r'^    \S.* edit last queued message$')
INTERRUPTED = '■ Conversation interrupted - use /feedback if something went wrong'

_refusals = set()
_refusal_lock = threading.Lock()


def _restored_draft(lines):
    """After Esc, the restored queue can fill the composer and hide its footer."""
    top = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith('› ')), None)
    if top is None or not lines[top].endswith(' ↑'):
        return False
    above = next((row for row in reversed(lines[:top]) if row and not row.startswith(' ')), None)
    return above == INTERRUPTED and all(not row or row.startswith('  ') for row in lines[top + 1:])


def _entry(index, rows, cut, width):
    available = width - len(INDENT) if width is not None else None
    # Codex wraps at spaces, so a full row is normal. It clips only a token too long for one row (a URL).
    partial = cut or (available is not None and any(
        ' ' not in row and _terminal_columns(row) >= available for row in rows if row))
    parts = [rows[0]]
    for prev, row in pairwise(rows):
        words = row.split(maxsplit=1)
        wrapped = (available is not None and prev and words
                   and _terminal_columns(prev) + 1 + _terminal_columns(words[0]) > available)
        parts.extend((' ' if wrapped else '\n', row))
    if partial:
        parts.append('…')
    return {'id': index, 'text': ''.join(parts), 'time': None, 'partial': partial}


def _parse(plain_screen, width):
    """Return entries or a short refusal reason."""
    lines = plain_screen.splitlines()
    composer = codex_composer_row(lines)
    if composer is None:
        # This layout can establish an empty queue only. All entries require the footer check.
        return [] if _restored_draft(lines) else 'unverified composer'

    bottom = composer - 1
    # Codex can leave a composer padding row in addition to the preview separator.
    for _ in range(2):
        if bottom < 0 or lines[bottom]:
            break
        bottom -= 1
    if bottom < 0:
        return []
    if not HINT.fullmatch(lines[bottom]):
        # A clipped preview ends on an entry or continuation row, with the queued header above those rows.
        # Rows under a steer header only are not a queue.
        if not lines[bottom].startswith((ENTRY, INDENT)):
            return []
        while bottom >= 0 and (not lines[bottom] or lines[bottom].startswith((ENTRY, INDENT))):
            bottom -= 1
        return 'clipped' if bottom >= 0 and lines[bottom] == HEADER else []

    top = bottom - 1
    while top >= 0 and (not lines[top] or lines[top].startswith((ENTRY, INDENT))):
        top -= 1
    if top < 0 or lines[top] != HEADER:
        return 'unexpected row'

    # Only the rows below this header belong to the queue. Steer sections, including
    # any wrapped headers and separators, stay above it.
    entries, rows, cut = [], [], False
    for row in lines[top + 1:bottom]:
        if row.startswith(ENTRY):
            if rows:
                if not rows[-1] and not cut:
                    return 'unexpected blank row'
                entries.append(_entry(len(entries), rows, cut, width))
            rows, cut = [row[len(ENTRY):]], False
        elif not rows or cut:
            return 'unexpected row'
        elif row == CUT:
            cut = True
        else:
            rows.append(row[len(INDENT):])
    if not rows or (not rows[-1] and not cut):
        return 'unexpected blank row'
    entries.append(_entry(len(entries), rows, cut, width))
    return entries[-50:]


def parse(plain_screen, width=None) -> list | None:
    """Return queued entries, or None when the screen fails the format check."""
    result = _parse(plain_screen, width)
    return result if isinstance(result, list) else None


def for_chat(entry, plain_screen):
    """Use the existing live-status frame and log each version/reason once."""
    width = None
    if TITLE in plain_screen:
        try:
            result = subprocess.run([paths.TMUX, 'display', '-p', '-t', entry['id'], '#{pane_width}'],
                                    check=True, capture_output=True, text=True, timeout=5)
            columns = int(result.stdout)
            if columns > 0:
                width = columns
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    result = _parse(plain_screen, width)
    if isinstance(result, list):
        return result
    version = _agent_version(entry) or 'unknown version'
    key = (version, result)
    with _refusal_lock:
        if key not in _refusals:
            _refusals.add(key)
            print(f'Codex {version} queue block did not match ({result}); queued prompts hidden',
                  file=sys.stderr, flush=True)
    return []
