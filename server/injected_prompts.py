"""Bind successful Tether sends to native user records, once per transcript."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

import codex_messages
import continuations
import paths
import shell_log

ROOT = paths.CACHE / 'injected'
PENDING_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
DELIVERY_LINE = re.compile(r'\r?\n\r?\n\[Tether id ([0-9a-f]{16})\][ \t\r\n]*\Z')
_lock = threading.Lock()
_sending = [threading.Lock() for _ in range(32)]


def _transcript(entry):
    path = entry.get('transcript')
    if entry.get('agent') not in ('claude', 'codex') or not path or entry.get('pending_conversation') or entry.get('ambiguous'):
        return None
    return Path(os.path.abspath(path))


def _file(path):
    return ROOT / (hashlib.sha256(os.fsencode(path)).hexdigest() + '.json')


def _send_lock(path):
    return _sending[hash(path) % len(_sending)]


def _read(file):
    try:
        data = json.loads(file.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(file, data, *, once=False):
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = tempfile.NamedTemporaryFile(mode='w', dir=file.parent, delete=False)
    try:
        with stream:
            json.dump(data, stream)
        if once:
            try:
                os.link(stream.name, file)
            except FileExistsError:
                pass
        else:
            os.replace(stream.name, file)
    finally:
        Path(stream.name).unlink(missing_ok=True)


def initialize():
    """Start the display cutover once and prune receipts that cannot bind."""
    try:
        file = paths.CACHE / 'injected-since.json'
        if not file.exists():
            _write(file, {'since_ms': time.time_ns() // 1_000_000}, once=True)
        with _lock:
            now = time.time_ns() // 1_000_000
            for file in ROOT.glob('*.json'):
                data = _read(file)
                path = data.get('transcript')
                if not isinstance(path, str):
                    continue
                receipts = data.get('receipts', [])
                kept = _live_receipts(receipts, now)
                try:
                    Path(path).stat()
                except FileNotFoundError:
                    # A send can precede creation of its transcript. Keep its zero anchor.
                    kept = [r for r in kept if 'id' not in r and r['file'] is None]
                except OSError:
                    continue
                if not kept:
                    file.unlink(missing_ok=True)
                elif len(kept) != len(receipts):
                    _write(file, dict(data, receipts=kept))
    except OSError as exc:
        logging.warning('Could not initialize Tether prompt receipts: %s', exc.strerror)


def timestamp_ms(value):
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if when.tzinfo is not None:
            delta = when - datetime(1970, 1, 1, tzinfo=timezone.utc)
            return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    except (ValueError, OverflowError, OSError):
        pass
    return None


def anchor(entry):
    """Take the transcript's end position and the clock time before submitting."""
    path = _transcript(entry)
    if path is None:
        return None
    at = time.time_ns() // 1_000_000
    try:
        info = path.stat()
        return {'at': at, 'offset': info.st_size, 'file': [info.st_dev, info.st_ino]}
    except FileNotFoundError:
        return {'at': at, 'offset': 0, 'file': None}
    except OSError:
        return None


def _text(text):
    return shell_log.strip_folder_note(continuations.unwrap(text))


def _hash(text):
    return hashlib.sha256(' '.join(text.split()).encode()).hexdigest()


def _split_delivery(text):
    match = DELIVERY_LINE.search(text)
    return (text[:match.start()], match[1]) if match else (text, None)


def strip_delivery_id(text):
    """Return the prompt text for recognisers that read native records."""
    return _split_delivery(text)[0]


def _live_receipts(receipts, now):
    return [r for r in receipts if 'id' in r or r.get('delivery_id') and now - r['at'] <= PENDING_MAX_AGE_MS]


def _record(path, text, kind, *, before, delivery_id, tabs):
    file = _file(path)
    metadata = {'prompt': kind, **({'tabs': tabs} if kind == 'status' else {})}
    with _lock:
        try:
            receipts = _live_receipts(_read(file).get('receipts', []), before['at'])
            receipts.append(dict(before, delivery_id=delivery_id, hash=_hash(_text(text)), metadata=metadata))
            _write(file, {'transcript': str(path), 'receipts': receipts})
        except OSError as exc:
            logging.warning('Could not save Tether prompt receipt: %s', exc.strerror)


@contextmanager
def track_send(entry, text, kind, *, tabs=None):
    """Yield the wire text and keep readers out until the successful send is recorded."""
    path = _transcript(entry)
    if path is None:
        yield text
        return
    with _send_lock(path):
        before = anchor(entry)
        if before is None:
            yield text
            return
        delivery_id = secrets.token_hex(8)
        yield f'{text}\n\n[Tether id {delivery_id}]'
        _record(path, text, kind, before=before, delivery_id=delivery_id, tabs=tabs)


def revision(entry):
    """Notify the chat stream if a receipt arrived after it read the native message."""
    path = _transcript(entry)
    try:
        return _file(path).stat().st_mtime_ns if path is not None else None
    except OSError:
        return None


def codex_id(offset, stamp):
    # The byte position identifies the same rollout line as the chat's line-number id.
    return f'{offset}:{stamp or ""}'


def codex_parts(record):
    """The visible user parts of a Codex rollout line."""
    payload = record.get('payload')
    if (record.get('type') == 'response_item' and isinstance(payload, dict)
            and payload.get('type') == 'message' and payload.get('role') == 'user'):
        for part in codex_messages.parts(payload):
            if part['role'] == 'user' and not part.get('kind') and (part['_user'] or not part['text'].startswith('# AGENTS.md instructions')):
                yield part['text']


def _candidate(agent, offset, record):
    if agent == 'codex':
        return codex_id(offset, record.get('timestamp')), codex_parts(record)
    identifier = record.get('uuid')
    if not isinstance(identifier, str) or not identifier or record.get('type') not in ('attachment', 'user'):
        return None, ()
    from agents import claude_consume  # history is used by the Claude parser
    messages = []
    claude_consume(messages, 0, record, continuations.unwrap)
    return identifier, (m['text'] for m in messages if m['role'] == 'user' and not m.get('kind'))


def _bind(path, agent, pending, bound):
    """Read at the earliest unbound send, including records outside the chat window."""
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        identity = [info.st_dev, info.st_ino]
        pending = {r['delivery_id']: r for r in pending if r['offset'] <= info.st_size and r['file'] in (None, identity)}
        if not pending:
            return
        offset = min(r['offset'] for r in pending.values())
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b'\n':
                stream.readline()  # the anchor can fall inside a record already being written
        while pending and stream.tell() < info.st_size:
            start = stream.tell()
            line = stream.readline(info.st_size - start)
            if not line.endswith(b'\n'):
                break  # Do not bind an unfinished native record.
            try:
                record = json.loads(line)
                identifier, texts = _candidate(agent, start, record)
                if not identifier or identifier in bound:
                    continue
                for text in texts:
                    prompt, delivery_id = _split_delivery(_text(text))
                    match = pending.get(delivery_id)
                    if match and match['offset'] <= start and match['hash'] == _hash(prompt):
                        match['id'] = identifier
                        bound[identifier] = match
                        del pending[delivery_id]
                        break
            except (ValueError, AttributeError, TypeError):
                continue


def bindings(entry):
    """Return receipts by native record id, binding a successful send at most once."""
    path = _transcript(entry)
    if path is None:
        return {}
    file = _file(path)
    with _send_lock(path), _lock:
        data = _read(file)
        receipts = data.get('receipts', [])
        kept = _live_receipts(receipts, time.time_ns() // 1_000_000)
        bound = {r['id']: r for r in kept if 'id' in r}
        pending = [r for r in kept if 'id' not in r]
        if pending:
            try:
                _bind(path, entry['agent'], pending, bound)
            except OSError:
                pass
        if len(kept) != len(receipts) or any('id' in r for r in pending):
            try:
                _write(file, dict(data, receipts=kept))
            except OSError as exc:
                logging.warning('Could not persist Tether prompt bindings: %s', exc.strerror)
        return bound


def mark(entry, messages):
    """Decorate user records without changing the cached transcript."""
    bound = bindings(entry)
    cutover = _read(paths.CACHE / 'injected-since.json').get('since_ms')
    if not bound and type(cutover) is not int and not any('_record_id' in message for message in messages):
        return messages
    result = []
    for message in messages:
        identifier = message.get('_record_id')
        if '_record_id' in message:
            message = {key: value for key, value in message.items() if key != '_record_id'}
        if message.get('role') == 'user' and not message.get('kind'):
            match = bound.get(identifier)
            stamp = timestamp_ms(message.get('time')) if type(cutover) is int else None
            if stamp is not None and stamp < cutover:
                message = dict(message, legacy=True)
            if match:
                text = _text(message['text'])
                delivery_id = match.get('delivery_id')
                if delivery_id is None:  # receipts already bound before the delivery ID cutover
                    message = dict(message, text=text, source='tether', **match['metadata'])
                else:
                    prompt, found = _split_delivery(text)
                    if found == delivery_id:
                        message = dict(message, text=prompt, source='tether', **match['metadata'])
        result.append(message)
    return result
