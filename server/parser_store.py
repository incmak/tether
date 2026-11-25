"""Checkpoints for caller-owned JSONL parsers. Transcripts remain the source of truth."""
import hashlib
import json
import os
import tempfile
import threading
import time

import history
import paths

SAVE_INTERVAL = 30
ANCHOR_BYTES = 4096


def _signature(info):
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(key, offset, observed=None):
    _, path, dev, inode, *_ = key
    length = min(offset, ANCHOR_BYTES)
    try:
        with open(path, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if ((info.st_dev, info.st_ino) != (dev, inode) or info.st_size < offset
                    or observed is not None and _signature(info) != observed):
                return None
            stream.seek(offset - length)
            data = stream.read(length)
            if len(data) == length and _signature(os.fstat(stream.fileno())) == _signature(info):
                return hashlib.sha256(data).hexdigest()
    except OSError:
        pass
    return None


class ParserStore:
    def __init__(self, name, version, *, clock=time.monotonic, pending=None):
        self.name = name
        self.version = version
        self.clock = clock
        self.pending = pending
        self.lock = threading.Lock()
        self._records = None
        self._saved = {}
        self._observed = {}  # changed pane -> file signature at its last advance
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
                        or not isinstance(record.get('unterminated', False), bool)
                        or _digest(key, offset) != digest):
                    continue
                state = self._state(record['state'], None, self.pending)
            except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
                continue
            records[pane] = key, dict(record, state=state)
            self._saved[pane] = dict(entry, key=key)
        return records

    def advance(self, pane, key, info, fresh, consume):
        """Advance under the store lock and history's path lock."""
        previous_key, previous = self.records.get(pane, (None, None))
        cached = previous if previous_key == key else None
        progress = (cached['offset'], cached.get('unterminated')) if cached is not None else None
        record = history.advance(key[1], cached, fresh, consume, trim=False, info=info)
        if record is not previous or progress != (record['offset'], record.get('unterminated')):
            self.records[pane] = key, record
            self._observed[pane] = _signature(info)
            self._dirty = True
        return record['state']

    def prune(self, panes):
        with self.lock:
            for pane in self.records.keys() - panes:
                del self.records[pane]
                self._observed.pop(pane, None)
                self._dirty = True

    def save(self, *, force=False):
        with self.lock:
            try:
                now = self.clock()
                if not self._dirty or not force and now - self._last_save < SAVE_INTERVAL:
                    return
                self._last_save = now
                records = {pane: entry for pane, entry in self._saved.items()
                           if pane in self.records and self.records[pane][0] == entry['key']}
                for pane, observed in self._observed.items():
                    key, record = self.records[pane]
                    digest = _digest(key, record['offset'], observed)
                    # Keep the previous checkpoint if the transcript changed since the parser read it.
                    if digest is None:
                        continue
                    state = self._state(record['state'], self.pending, None)
                    records[pane] = dict(key=key, record=dict(record, state=state), sha256=digest)
                if records != self._saved:
                    self._write(dict(version=self.version, records=records))
                self._saved = records
                self._observed.clear()
                self._dirty = False
            except Exception:
                # Checkpoint failures must never interrupt the monitor or stop an exit.
                pass

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
