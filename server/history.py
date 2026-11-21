"""Incremental reading of append-only JSONL history files.

A chat history only ever grows at the end, so each file is parsed once; later calls read just the bytes
appended since, instead of re-parsing hundreds of megabytes on every update.
"""
import json
import os
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import re
import sqlite3

import injected_prompts
import paths

MAX_FILES = 12
# The chat view never asks for more than the latest 2,000 messages (see window_limit in server.py), so only a little more than
# that is kept per conversation; trimming happens in batches, and `dropped` keeps the true total.
KEEP_MESSAGES = 2500
TRIM_SLACK = 500
_states = {}
_locks = {}
_registry_lock = threading.Lock()


def _lock_for(path):
    with _registry_lock:
        return _locks.setdefault(path, threading.Lock())


def keep_tail(state):
    """Trim the oldest messages of a `{'messages': [...], 'dropped': n}` state once it is well over the cap."""
    if not isinstance(state, dict) or 'messages' not in state:
        return  # only message-list states are capped
    excess = len(state['messages']) - KEEP_MESSAGES
    if excess > TRIM_SLACK:
        del state['messages'][:excess]
        state['dropped'] += excess


def advance(path, cached, fresh, consume, trim, *, info=None, with_offset=False):
    """Advance a caller-owned parser record under the path lock. Return None if stat fails."""
    if info is None:
        try:
            info = os.stat(path)
        except (OSError, TypeError):
            return None
    if cached and (cached['inode'] != info.st_ino or info.st_size < cached['offset']):
        cached = None  # replaced or truncated: start over
    if cached is None:
        cached = {'inode': info.st_ino, 'offset': 0, 'index': 0, 'state': fresh()}
    if info.st_size > cached['offset']:
        with open(path, 'rb') as stream:
            stream.seek(cached['offset'])
            if cached.pop('unterminated', False) and stream.read(1) == b'\n':
                cached['offset'] += 1  # the newline that finished a record already consumed
            stream.seek(cached['offset'])
            since_trim = 0
            while True:
                line = stream.readline()
                if not line:
                    break
                complete = line.endswith(b'\n')
                try:
                    record = json.loads(line)
                except ValueError:
                    if not complete:
                        break  # a record still being written is picked up on the next call
                    record = None
                offset = cached['offset']
                cached['offset'] += len(line)
                index, cached['index'] = cached['index'], cached['index'] + 1
                cached['unterminated'] = not complete
                if record is not None:
                    try:
                        if with_offset:
                            consume(cached['state'], index, record, offset=offset)
                        else:
                            consume(cached['state'], index, record)
                    except (AttributeError, TypeError):  # one odd record (an unexpected null) must not break the whole chat
                        pass
                    since_trim += 1
                    if trim and since_trim >= 2000:  # keep the first load of a huge file bounded too
                        trim(cached['state'])
                        since_trim = 0
        if trim:
            trim(cached['state'])
    return cached


def read(path, fresh, consume, trim=None, *, namespace=None, with_offset=False):
    """Bring the cached state for `path` up to date and return it.

    `fresh()` builds an empty state; `consume(state, index, record)` folds one decoded record in, where `index`
    is the record's zero-based line number; `trim(state)` bounds its size after each read. The returned state is
    shared: callers must copy before changing it. Use a namespace to keep a second parser's state for the same file.
    With `with_offset=True`, consume also gets the byte offset of the line.
    """
    path = str(path)
    key = path if namespace is None else (path, namespace)
    trim = keep_tail if trim is None else trim  # looked up now, so it can be replaced; pass False to disable
    with _lock_for(path):
        cached = advance(path, _states.get(key), fresh, consume, trim, with_offset=with_offset)
        if cached is None:
            return fresh()
        with _registry_lock:
            _states.pop(key, None)
            _states[key] = cached  # most recently used goes last
            while len(_states) > MAX_FILES:
                oldest = next(iter(_states))
                _states.pop(oldest)
        return cached['state']


def forget():
    with _registry_lock:
        _states.clear()


UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
PREVIEW_BYTES = 65536
_previews = {}
_preview_lock = threading.Lock()


def transcript_files(identifier=None):
    """Only direct project children are chats; subagents have their own subfolders."""
    try:
        with os.scandir(paths.CLAUDE / 'projects') as projects:
            for project in projects:
                if not project.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(project.path) as files:
                        for entry in files:
                            if not entry.name.endswith('.jsonl') or not UUID.fullmatch(entry.name[:-6]):
                                continue
                            if identifier is not None and entry.name != identifier + '.jsonl':
                                continue
                            if entry.is_file(follow_symlinks=False):
                                yield Path(entry.path), entry.stat()
                except OSError:
                    continue  # a project can disappear during a poll
    except OSError:
        return


def user_title(entry, limit=80, *, bound=()):
    identifier = entry.get('uuid')
    if entry.get('isMeta') or entry.get('isSidechain') or (isinstance(identifier, str) and identifier in bound):
        return ''
    if entry.get('type') == 'user':
        message = entry.get('message')
        content = message.get('content') if isinstance(message, dict) else None
    elif entry.get('type') == 'attachment':
        attachment = entry.get('attachment')
        content = attachment.get('prompt') if isinstance(attachment, dict) and attachment.get('type') == 'queued_command' else None
    else:
        return ''
    if isinstance(content, list):
        content = '\n'.join(part.get('text', '') for part in content
                            if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str))
    if not isinstance(content, str):
        return ''
    text = ' '.join(line for line in content.splitlines() if not line.lstrip().startswith('<'))
    return ' '.join(text.split())[:limit]


def _head_records(path):
    with path.open('rb') as stream:
        while line := stream.readline():
            offset = stream.tell() - len(line)
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield offset, entry


def preview(path, info):
    bound = injected_prompts.bindings({'transcript': path, 'agent': 'claude'})
    signature = (info.st_mtime_ns, info.st_size, frozenset(bound))
    with _preview_lock:
        cached = _previews.get(path)
        if cached and cached[0] == signature:
            return dict(cached[1])
        with open(path, 'rb') as stream:
            head = stream.read(PREVIEW_BYTES)
            tail_start = max(len(head), info.st_size - PREVIEW_BYTES)
            stream.seek(tail_start)
            tail = stream.read(PREVIEW_BYTES)
        # Never join two partial records across the unread middle of a large file.
        if tail_start > len(head):
            tail = tail.partition(b'\n')[2]
        title, custom_title, first_user, cwd = '', '', '', None
        lines = (head + tail).splitlines() if tail_start == len(head) else head.splitlines() + tail.splitlines()
        for line in lines:
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get('cwd'), str) and entry['cwd']:
                cwd = entry['cwd']
            kind = entry.get('type')
            field = {'custom-title': 'customTitle', 'ai-title': 'aiTitle', 'summary': 'summary'}.get(kind) if isinstance(kind, str) else None
            value = entry.get(field) if field else None
            if isinstance(value, str) and value.strip():
                title = ' '.join(value.split())
                if kind == 'custom-title':
                    custom_title = title
            if not first_user:
                first_user = user_title(entry, limit=300, bound=bound)
        if bound:
            # The bounded preview may skip the first real user message if a relay started the chat.
            starts_with_relay = False
            for _, entry in _head_records(path):
                identifier = entry.get('uuid')
                if isinstance(identifier, str) and identifier in bound:
                    starts_with_relay = True
                text = user_title(entry, limit=300, bound=bound)
                if text:
                    first_user = text
                    break
            if starts_with_relay:
                title = custom_title
        result = {'id': path.stem, 'title': title or first_user[:80] or path.stem, 'text': first_user, 'cwd': cwd,
                  'time': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()}
        _previews[path] = (signature, result)
        return dict(result)


def recent_chats(limit=30):
    files = sorted(transcript_files(), key=lambda item: item[1].st_mtime_ns, reverse=True)[:limit]
    chats = []
    for path, info in files:
        try:
            chats.append(preview(path, info))
        except OSError:
            continue
    return chats


@lru_cache(maxsize=128)
def _codex_relay_title(path, _signature, bound):
    """Return the next user's text if the first user record is a bound relay."""
    started = False
    for offset, entry in _head_records(path):
        text = next(injected_prompts.codex_parts(entry), None)
        if text is None:
            continue
        if injected_prompts.codex_id(offset, entry.get('timestamp')) in bound:
            started = True
        else:
            return ' '.join(text.split()) if started else None
    return '' if started else None


def _codex_chats():
    """Read the newest Codex index without importing agents (which imports history)."""
    try:
        databases = sorted(paths.CODEX.glob('state_*.sqlite'),
                           key=lambda path: [int(n) for n in re.findall(r'\d+', path.name)])
        if not databases:
            return []
        connection = sqlite3.connect(databases[-1].resolve().as_uri() + '?mode=ro', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            if 'id' not in columns:
                return []
            rules = []
            if 'archived' in columns:
                rules.append('archived=0')
            if 'source' in columns:
                rules.append("source IN ('cli','vscode')")
            query = 'SELECT * FROM threads' + (' WHERE ' + ' AND '.join(rules) if rules else '')
            chats = []
            for row in connection.execute(query):
                row = dict(row)
                identifier = row['id']
                if not isinstance(identifier, str) or not UUID.fullmatch(identifier):
                    continue
                text = row.get('first_user_message')
                rollout = row.get('rollout_path')
                changed = None
                if isinstance(rollout, str) and rollout:
                    bound = injected_prompts.bindings({'transcript': rollout, 'agent': 'codex'})
                    if bound:
                        try:
                            path = Path(rollout)
                            info = path.stat()
                            changed = _codex_relay_title(path, (info.st_ino, info.st_size, info.st_mtime_ns), frozenset(bound))
                        except OSError:
                            pass
                if changed is not None:
                    text = changed
                titles = (row.get('name'), row.get('title'), text) if changed is None else (row.get('name'), text, identifier)
                title = next((' '.join(value.split()) for value in titles if isinstance(value, str) and value.strip()), None)
                if title is None:
                    # An empty thread. has_user_event cannot tell: Codex stores 0 on every row.
                    if columns & {'name', 'title', 'first_user_message'}:
                        continue
                    title = identifier
                try:
                    updated = datetime.fromtimestamp(row['updated_at_ms'] / 1000, timezone.utc).isoformat()
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    updated = None
                chats.append({'id': identifier, 'agent': 'codex', 'title': title[:80],
                              'text': text[:300] if isinstance(text, str) else '',
                              'cwd': row.get('cwd'), 'time': updated})
            return chats
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []


def all_chats():
    chats = []
    for path, info in transcript_files():
        try:
            chats.append(dict(preview(path, info), agent='claude'))
        except OSError:
            continue
    return chats + _codex_chats()


def search_chats(chats, *, agent='all', q='', dir=None, state='all', live=frozenset(),
                 display_dir=lambda cwd: cwd):
    """Filter an index snapshot; displayed directories belong to the caller."""
    terms = q.casefold().split()
    matches = []
    for chat in chats:
        if agent != 'all' and chat['agent'] != agent:
            continue
        if state != 'all' and (chat['id'] in live) != (state == 'open'):
            continue
        if dir is not None and display_dir(chat['cwd']) != dir:
            continue
        searchable = ' '.join(chat.get(key) or '' for key in ('title', 'text', 'cwd')).casefold()
        if all(term in searchable for term in terms):
            matches.append(chat)
    return sorted(matches, key=lambda chat: chat['time'] or '', reverse=True)


def find_chat(identifier, agent='claude'):
    if not isinstance(identifier, str) or not UUID.fullmatch(identifier):
        raise ValueError('Choose a valid chat id.')
    if agent not in ('claude', 'codex'):
        raise ValueError('Choose Claude or Codex.')
    if agent == 'codex':
        return next((chat for chat in _codex_chats() if chat['id'] == identifier), None)
    for path, info in transcript_files(identifier):
        try:
            return dict(preview(path, info), agent='claude')
        except OSError:
            continue
    return None
