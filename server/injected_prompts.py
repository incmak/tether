"""Bind Tether deliveries to native user records, once per transcript."""
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
UNKNOWN_RECEIPT = 'unknown-receipt'
_lock = threading.Lock()
_sending = [threading.Lock() for _ in range(32)]


class ReceiptsUnreadable(Exception):
    """The available data cannot establish who sent a prompt."""


class ReceiptNotSaved(RuntimeError):
    """The prompt was refused before it reached the terminal."""


def _transcript(entry):
    path = entry.get('transcript')
    if entry.get('agent') not in ('claude', 'codex') or not path or entry.get('pending_conversation') or entry.get('ambiguous'):
        return None
    return Path(os.path.abspath(path))


def keeps_receipts(entry):
    """Whether track_send records a receipt for this tab. Elsewhere only the text can say who sent a prompt."""
    return _transcript(entry) is not None


def _file(path):
    return ROOT / (hashlib.sha256(os.fsencode(path)).hexdigest() + '.json')


def _send_lock(path):
    return _sending[hash(path) % len(_sending)]


def _read(file):
    try:
        data = json.loads(file.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:
        raise ReceiptsUnreadable(str(exc)) from exc
    if not isinstance(data, dict):
        raise ReceiptsUnreadable('Receipt data must be a JSON object')
    return data


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
        for file in ROOT.glob('*.json'):
            try:
                with _lock:
                    path = _read(file).get('transcript')
                if not isinstance(path, str):
                    continue
                path = Path(path)
                with _send_lock(path):
                    _resolve(path, None, cleanup=True)
            except ReceiptsUnreadable as exc:
                logging.warning('Could not initialize Tether prompt receipts: %s', exc)
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


def cutover_ms():
    since = _read(paths.CACHE / 'injected-since.json').get('since_ms')
    return since if type(since) is int else None


def is_legacy(timestamp, cutover):
    stamp = timestamp_ms(timestamp) if cutover is not None else None
    return stamp is not None and stamp < cutover


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


def has_delivery_id(text):
    """Whether the native prompt ends with a Tether delivery line."""
    return DELIVERY_LINE.search(text) is not None


def _record(path, text, kind, *, before, delivery_id, tabs):
    file = _file(path)
    metadata = {'prompt': kind, **({'tabs': tabs} if kind == 'status' else {})}
    with _lock:
        data = _read(file)
        scanned = data.get('scanned')
        if scanned and (scanned['file'] != before['file'] or scanned['offset'] > before['offset']):
            data.pop('scanned')  # This anchor was not covered by the saved scan.
        receipt = dict(before, delivery_id=delivery_id, hash=_hash(_text(text)), metadata=metadata)
        _write(file, dict(data, transcript=str(path), receipts=[*data.get('receipts', ()), receipt]))


@contextmanager
def track_send(entry, text, kind, *, tabs=None):
    """Save a pending receipt before yielding the wire text."""
    path = _transcript(entry)
    if path is None:
        yield text
        return
    with _send_lock(path):
        try:
            # Resolve earlier sends before taking the new anchor. Its record may arrive during the send.
            _resolve(path, entry['agent'])
        except ReceiptsUnreadable:
            pass  # Retry the old receipts later. An unread transcript can still receive a new prompt.
        delivery_id = secrets.token_hex(8)
        try:
            before = anchor(entry)
            _record(path, text, kind, before=before, delivery_id=delivery_id, tabs=tabs)
        except (OSError, ReceiptsUnreadable) as exc:
            raise ReceiptNotSaved('Could not save the Tether prompt receipt. Try again.') from exc
        # Keep the receipt pending on send failure. The text may have partly reached the pane.
        yield f'{text}\n\n[Tether id {delivery_id}]'


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
    if agent is None:  # Existing receipt files store a path but no agent.
        agent = 'codex' if record.get('type') == 'response_item' else 'claude'
    if agent == 'codex':
        return codex_id(offset, record.get('timestamp')), codex_parts(record)
    identifier = record.get('uuid')
    if not isinstance(identifier, str) or not identifier or record.get('type') not in ('attachment', 'user'):
        return None, ()
    from agents import claude_consume  # history is used by the Claude parser
    messages = []
    claude_consume(messages, 0, record, continuations.unwrap)
    return identifier, (m['text'] for m in messages if m['role'] == 'user' and not m.get('kind'))


def _receipt_offset(receipt, identity):
    # An anchor belongs to its original file. In a replacement, match the ID and hash from the start.
    return receipt['offset'] if receipt['file'] in (None, identity) else 0


def _bind(path, agent, pending, bound, scanned):
    """Scan the unread suffix of the transcript for pending deliveries."""
    from history import records

    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        identity = [info.st_dev, info.st_ino]
        end = info.st_size
        starts = {key: _receipt_offset(r, identity) for key, r in pending.items()}
        starts = {key: start for key, start in starts.items() if start <= end}
        if not starts:
            return identity, end, {}, None, False
        used = {key for key, r in bound.items() if r.get('file') in (None, identity)}
        matches = {}
        offset = min(starts.values())
        if scanned and scanned['file'] == identity and offset <= scanned['offset'] <= end:
            offset = scanned['offset']
        elif offset:
            stream.seek(offset - 1)
            if stream.read(1) != b'\n':
                prefix = stream.readline(end - offset)
                if not prefix.endswith(b'\n'):
                    return identity, end, {}, None, False
                offset += len(prefix)  # The anchor fell inside a record that started before the send.
        for start, record, _, length in records(stream, offset, end):
            offset = start + length
            if not isinstance(record, dict):
                continue
            try:
                identifier, texts = _candidate(agent, start, record)
                if not identifier or identifier in used:
                    continue
                for text in texts:
                    prompt, delivery_id = _split_delivery(_text(text))
                    match = pending.get(delivery_id)
                    if match and delivery_id in starts and starts[delivery_id] <= start and match['hash'] == _hash(prompt):
                        matches[delivery_id] = dict(match, id=identifier, file=identity)
                        used.add(identifier)
                        del starts[delivery_id]
                        break
            except (ValueError, AttributeError, TypeError):
                continue
            if not starts:
                break
    current = path.stat()
    if [current.st_dev, current.st_ino] != identity or current.st_size < offset:
        raise ReceiptsUnreadable('Transcript changed during the receipt scan')
    return identity, current.st_size, matches, {'file': identity, 'offset': offset}, offset == end == current.st_size


def _pending(receipts):
    return {r['delivery_id']: r for r in receipts if 'id' not in r and r.get('delivery_id')}


def _resolve(path, agent, *, cleanup=False):
    file = _file(path)
    with _lock:
        data = _read(file)
    receipts = data.get('receipts', ())
    bound = {r['id']: r for r in receipts if 'id' in r}
    pending = _pending(receipts)
    if not pending and not cleanup and 'missing_since' not in data:
        return bound
    previous = data.get('scanned')
    identity, size = None, 0
    matches, scanned, covered = {}, None, False
    try:
        if cleanup or 'missing_since' in data:
            # Only startup can retire old receipts for an absent transcript. Any read can see it return.
            try:
                info = path.stat()
            except FileNotFoundError:
                pass
            else:
                identity, size = [info.st_dev, info.st_ino], info.st_size
        if pending and (not cleanup or identity is not None):
            identity, size, matches, scanned, covered = _bind(path, agent, pending, bound, previous)
    except OSError as exc:
        raise ReceiptsUnreadable(str(exc)) from exc

    now = time.time_ns() // 1_000_000
    with _lock:
        current = _read(file)
        missing_since = None if identity is not None else current.get('missing_since', now if cleanup else None)
        # A transcript is gone only after the age cap of continuous absence, not after one failed startup check.
        gone = cleanup and identity is None and now - missing_since > PENDING_MAX_AGE_MS
        kept = []
        for receipt in current.get('receipts', ()):
            delivery_id = receipt.get('delivery_id')
            if 'id' in receipt:
                if bound.get(receipt['id']) != receipt or not (gone and now - receipt['at'] > PENDING_MAX_AGE_MS):
                    kept.append(receipt)  # A bound record id stays valid while a transcript exists at this path.
            elif delivery_id:
                if pending.get(delivery_id) != receipt:
                    kept.append(receipt)
                elif delivery_id in matches:
                    kept.append(matches[delivery_id])
                elif not ((gone or identity is not None and covered and _receipt_offset(receipt, identity) <= size)
                          and now - receipt['at'] > PENDING_MAX_AGE_MS):
                    kept.append(receipt)
        updated = dict(current, receipts=kept)
        if missing_since is None:
            updated.pop('missing_since', None)
        else:
            updated['missing_since'] = missing_since
        cursor = current.get('scanned')
        if identity is not None and cursor and cursor == previous and (
                cursor['file'] != identity or cursor['offset'] > size):
            updated.pop('scanned')
            cursor = None
        if scanned and (cursor is None or cursor['file'] == identity and cursor['offset'] < scanned['offset']):
            cursor = updated['scanned'] = scanned
        if cursor and any(r != pending.get(key) and _receipt_offset(r, cursor['file']) < cursor['offset']
                          for key, r in _pending(kept).items()):
            updated.pop('scanned')  # A receipt added after the snapshot needs its own anchor covered.
        try:
            if cleanup and not kept:
                file.unlink(missing_ok=True)
            elif updated != current:
                _write(file, updated)
        except OSError as exc:
            logging.warning('Could not persist Tether prompt bindings: %s', exc)
    return {r['id']: r for r in kept if 'id' in r}


def bindings(entry):
    """Return receipts by native record id, binding a successful send at most once."""
    path = _transcript(entry)
    if path is None:
        return {}
    with _send_lock(path):
        try:
            return _resolve(path, entry['agent'])
        except OSError as exc:
            raise ReceiptsUnreadable(str(exc)) from exc


def bound_receipt(entry, record):
    """Read the receipt for this exact native prompt record, if one binds."""
    identifier = record.get('uuid') if entry.get('agent') == 'claude' else None
    if entry.get('agent') == 'codex' and type(record.get('_offset')) is int:
        identifier = codex_id(record['_offset'], record.get('timestamp'))
    bound = bindings(entry)
    return bound.get(identifier) if isinstance(identifier, str) else None


def mark(entry, messages):
    """Decorate user records without changing the cached transcript."""
    unreadable = False
    try:
        bound = bindings(entry)
        cutover = cutover_ms()
    except ReceiptsUnreadable:
        bound, cutover, unreadable = {}, None, True
    if not unreadable and not bound and cutover is None and not any('_record_id' in message for message in messages):
        return messages
    result = []
    for message in messages:
        identifier = message.get('_record_id')
        if '_record_id' in message:
            message = {key: value for key, value in message.items() if key != '_record_id'}
        if message.get('role') == 'user' and not message.get('kind'):
            match = bound.get(identifier)
            if is_legacy(message.get('time'), cutover):
                message = dict(message, legacy=True)
            if unreadable:
                message = dict(message, role='assistant', kind='notice', title='Prompt source unknown', source=UNKNOWN_RECEIPT)
            elif match:
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
