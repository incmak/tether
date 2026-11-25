"""Checkpoints for caller-owned JSONL parsers. Transcripts remain the source of truth."""
import hashlib
import json
import os
import signal
import stat
import tempfile
import threading
import time

import history
import paths

SAVE_INTERVAL = 30
ANCHOR_BYTES = 4096


def _signature(info):
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(key, offset, expected_signature=None):
    _, path, dev, inode, *_ = key
    length = min(offset, ANCHOR_BYTES)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb', buffering=0) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (dev, inode)
                    or info.st_size < offset
                    or expected_signature is not None and _signature(info) != expected_signature):
                return None
            stream.seek(offset - length)
            data = stream.read(length)
            if len(data) == length and _signature(os.fstat(stream.fileno())) == _signature(info):
                return hashlib.sha256(data).hexdigest()
    except OSError:
        pass
    return None


def _valid_state(state, fresh):
    for key, value in fresh.items():
        # The Feed's initial null kind becomes a text outcome when a turn ends.
        types = (type(None), str) if value is None else (type(value),)
        if key not in state or type(state[key]) not in types:
            return False
    return True


def _valid_review_state(state):
    replies = state.get('replies')
    text_or_null = (str, type(None))
    return (isinstance(replies, dict)
            and all(isinstance(reply, text_or_null) for reply in replies.values())
            and isinstance(state.get('current'), text_or_null))


def install_sigterm_flush(stores):
    def flush(signum, _frame):
        try:
            for store in stores:
                store.save(force=True, timeout=2)
        finally:
            # Preserve signal termination without running the browser cleanup at exit.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, flush)


class ParserStore:
    def __init__(self, name, version, *, clock=time.monotonic, pending=None):
        self.name = name
        self.version = version
        self.clock = clock
        self.pending = pending
        self.lock = threading.Lock()
        self._records = None
        self._prune_to = None
        self._saved = {}
        self._anchors = {}  # pane -> (signature at last advance, digest); loaded signatures start as None
        self._changed = set()
        self._dirty = False
        self._last_save = float('-inf')

    @property
    def path(self):
        return paths.PARSERS / f'{self.name}.json'

    def _state(self, state, source, target):
        if self.pending is None:
            return state.copy()
        # A verdict is always text. Reserve JSON null in the reply map for the pending sentinel.
        return dict(state, replies={key: target if reply is source else reply
                                    for key, reply in state['replies'].items()})

    @property
    def records(self):
        """The pane map. Access it under the store lock."""
        if self._records is None:
            self._records = self._load()
            if self._prune_to is not None:
                self._prune_records(self._prune_to)
                self._prune_to = None
        return self._records

    def _load(self):
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved['version'] != self.version:
                return {}
            entries = saved['records'].items()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return {}
        records = {}
        for pane, entry in entries:
            try:
                key = tuple(entry['key'])
                record = entry['record']
                offset = record['offset']
                digest = entry['sha256']
                if (len(key) not in (4, 5) or not isinstance(key[1], str)
                        or len(key) == 5 and type(key[4]) is not int
                        or type(offset) is not int or offset < 0 or type(record['index']) is not int
                        or record['index'] < 0 or record['inode'] != key[3]
                        or not isinstance(record['state'], dict) or not isinstance(digest, str)
                        or not isinstance(record.get('unterminated', False), bool)):
                    continue
            except (KeyError, IndexError, TypeError):
                continue
            records[pane] = key, record
            self._anchors[pane] = None, digest
            self._saved[pane] = dict(entry, key=key)
        return records

    def _discard(self, pane):
        del self.records[pane]
        del self._anchors[pane]
        self._changed.discard(pane)
        self._dirty = True

    def advance(self, pane, key, info, fresh, consume):
        """Advance under the store lock and history's path lock."""
        if not stat.S_ISREG(info.st_mode):
            raise OSError('The transcript is not a regular file.')
        previous_key, previous = self.records.get(pane, (None, None))
        cached = previous if previous_key == key else None
        signature = _signature(info)
        old_signature, digest = self._anchors.get(pane, (None, None))
        if cached is not None:
            offset = cached['offset']
            if old_signature is None or old_signature != signature and offset > 0:
                if digest is None or _digest(key, offset) != digest:
                    cached = None
            if cached is not None and old_signature is None:
                state = cached['state']
                if (not _valid_state(state, fresh())
                        or self.pending is not None and not _valid_review_state(state)):
                    cached = None
                else:
                    cached = dict(cached, state=self._state(state, None, self.pending))
        if previous is not None and cached is None:
            self._discard(pane)
        progress = (cached['offset'], cached.get('unterminated')) if cached is not None else None
        try:
            record = history.advance(key[1], cached, fresh, consume, trim=False, info=info,
                                     strict_consume=cached is not None)
        except history.ConsumeError:
            record = None
        except OSError:
            if cached is not None:
                self._discard(pane)
            raise
        if record is not None and progress and progress[0] > 0:
            # advance mutates the cached offset. Check the bytes that belonged to the old state.
            checked = _digest(key, progress[0])
            if checked is None or checked != digest:
                record = None
        if record is None:
            self._discard(pane)
            info = os.stat(key[1])
            if not stat.S_ISREG(info.st_mode):
                raise OSError('The transcript is not a regular file.')
            key = (key[0], key[1], info.st_dev, info.st_ino, *key[4:])
            signature = _signature(info)
            record = history.advance(key[1], None, fresh, consume, trim=False, info=info)
        if record is not cached or progress != (record['offset'], record.get('unterminated')):
            digest = _digest(key, record['offset'], signature)
            self._changed.add(pane)
            self._dirty = True
        self.records[pane] = key, record
        self._anchors[pane] = signature, digest
        return record['state']

    def prune(self, panes):
        with self.lock:
            if self._records is not None:
                self._prune_records(panes)
            elif self._prune_to is None:
                self._prune_to = set(panes)
            else:
                self._prune_to.intersection_update(panes)

    def _prune_records(self, panes):
        for pane in self._records.keys() - panes:
            self._discard(pane)

    def save(self, *, force=False, timeout=-1):
        if not self.lock.acquire(timeout=timeout):
            return
        try:
            if not self._dirty and self._prune_to is None:
                return
            current = self.records  # apply any prune deferred until the checkpoint opened
            now = self.clock()
            if not self._dirty or not force and now - self._last_save < SAVE_INTERVAL:
                return
            self._last_save = now
            records = {pane: entry for pane, entry in self._saved.items()
                       if pane in current and current[pane][0] == entry['key']}
            missing = set()
            for pane in self._changed:
                key, record = current[pane]
                signature, digest = self._anchors[pane]
                if digest is None:
                    # Retry only against the file version this state describes.
                    digest = _digest(key, record['offset'], signature)
                if digest is None:
                    missing.add(pane)
                    continue
                self._anchors[pane] = signature, digest
                state = self._state(record['state'], self.pending, None)
                records[pane] = dict(key=key, record=dict(record, state=state), sha256=digest)
            if records != self._saved:
                self._write(dict(version=self.version, records=records))
            self._saved = records
            self._changed = missing
            self._dirty = bool(missing)
        except Exception:
            # Checkpoint failures must never interrupt the monitor or stop an exit.
            pass
        finally:
            self.lock.release()

    def _write(self, saved):
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
                temp = stream.name
                os.fchmod(stream.fileno(), 0o600)
                json.dump(saved, stream)
            os.replace(temp, path)
        finally:
            if temp is not None and os.path.exists(temp):
                os.unlink(temp)
