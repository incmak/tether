"""The shared phase history for Factory tasks. Screens report phases inferred by factoryModel.ts."""
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import paths

PATH = '/mobile/api/factory/history'
PHASES = ('inbox', 'spec', 'build', 'review', 'verify', 'deploy', 'done')
TASK_ID = re.compile(r'[B%][0-9]{1,19}')  # backlog ref or tmux pane ID, at most 20 characters
MAX_MOVES = 256
MAX_BODY = 32 * 1024
MAX_TASKS = 1000
MAX_ENTRIES = 40
WEEK_MS = 7 * 24 * 3600 * 1000
_lock = threading.Lock()


def file():
    return paths.TETHER / 'factory-history.json'


class Unreadable(OSError):
    """The history file is not valid JSON of the stored shape."""


def _valid(history):
    return isinstance(history, dict) and all(
        isinstance(task, str) and isinstance(entries, list) and entries and all(
            isinstance(entry, dict) and entry.get('phase') in PHASES and type(entry.get('at')) is int
            for entry in entries)
        for task, entries in history.items())


def _read(target):
    try:
        history = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise Unreadable('Could not read phase history.') from error
    if not _valid(history):
        raise Unreadable('Phase history has an unknown shape.')
    return history


def _write(target, history):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.factory-history-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(history, stream, separators=(',', ':'))
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def _recent(history, now):
    return {task: entries for task, entries in history.items() if entries[-1]['at'] >= now - WEEK_MS}


def history():
    """Return the retained history without changing the file on a read."""
    with _lock:
        return _recent(_read(file()), time.time_ns() // 1_000_000)


def record(body):
    """Validate the entire batch before changing any history. Ignore any client 'at' value."""
    if not isinstance(body, dict) or body.keys() != {'moves'}:
        raise ValueError('Send an object with moves.')
    moves = body['moves']
    if not isinstance(moves, list) or len(moves) > MAX_MOVES:
        raise ValueError(f'Send a list of up to {MAX_MOVES} moves.')
    for move in moves:
        if not isinstance(move, dict) or not {'task', 'phase'} <= move.keys() <= {'task', 'phase', 'at'}:
            raise ValueError('Each move needs a task and phase.')
        if not isinstance(move['task'], str) or not TASK_ID.fullmatch(move['task']) or move['phase'] not in PHASES:
            raise ValueError('Invalid task or phase.')

    with _lock:
        target = file()
        now = time.time_ns() // 1_000_000
        try:
            previous = _read(target)
        except Unreadable:
            # Keep the broken file for a look, then start a new history: it only records what screens saw.
            os.replace(target, target.with_name(target.name + '.bad'))
            previous = {}
        kept = _recent(previous, now)
        for move in moves:
            task, phase = move['task'], move['phase']
            entries = kept.get(task, [])
            if entries and entries[-1]['phase'] == phase:
                continue
            kept.pop(task, None)  # a later change wins when timestamps tie
            kept[task] = [*entries, {'phase': phase, 'at': now}][-MAX_ENTRIES:]
        if len(kept) > MAX_TASKS:
            newest = sorted(kept, key=lambda task: kept[task][-1]['at'])[-MAX_TASKS:]
            kept = {task: kept[task] for task in newest}
        if kept != previous:
            _write(target, kept)


def post(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= MAX_BODY:
            handler.close_connection = True
            raise ValueError('Invalid request size.')
        record(json.loads(handler.rfile.read(size)))
    except (ValueError, RecursionError):
        handler.respond(400, {'error': 'Invalid phase history request.'})
        return
    except OSError:
        handler.respond(503, {'error': 'Could not save phase history.'})
        return
    handler.respond(200, {'ok': True})
