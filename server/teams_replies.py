"""Teams answers to an urgent ping go into the tab that asked (B126).

An urgent ping through BriskBot to the approver's 1:1 chat carries a random reply key
(post-update.py --reply-key). BriskBot puts a reply box on the card and keeps each answer
for one day. While a key is open, a watcher reads the answers with chat-replies.py (the
post token: only this machine reads its own answers), sends each one to the tab and thread
the key belongs to, through the same guarded path as a broadcast, and tells Teams the result.

State: paths.CACHE/teams-replies.json (mode 0600): the read cursor and the open keys.
"""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

import paths

# A BriskBot card takes answers for one day after it is sent, and keeps each answer one more day
# (replies.mjs). The key lives through both days plus a margin, so every answer BriskBot keeps finds its tab.
CARD_TTL = 86400
ANSWER_TTL = 86400
KEY_TTL = CARD_TTL + ANSWER_TTL + 600
POLL = 10                # seconds between reads in the first hour after the newest ping
SLOW = 60                # seconds between reads after that, while a key is still open
FAST = 3600
IDLE = 300               # seconds between checks while no key is open (a new key wakes the watcher at once)
MAX_KEYS = 200
_LOCK = threading.Lock()
_WAKE = threading.Event()


def _file():
    return paths.CACHE / 'teams-replies.json'


def _load():
    try:
        data = json.loads(_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'after': 0, 'keys': {}}
    if not isinstance(data, dict):
        return {'after': 0, 'keys': {}}
    after = data.get('after')
    keys = data.get('keys')
    return {'after': after if isinstance(after, int) and after >= 0 else 0,
            'keys': {k: v for k, v in keys.items() if isinstance(v, dict)} if isinstance(keys, dict) else {}}


def _save(data):
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle)
    os.replace(temp, target)


def _live(keys, now):
    return {k: v for k, v in keys.items() if isinstance(v.get('sent'), (int, float)) and v['sent'] > now - KEY_TTL}


def reader(config):
    """chat-replies.py next to the configured post-update.py, when the ping goes to the BriskBot dm; else None."""
    fields = config.get('fields') if isinstance(config.get('fields'), dict) else {}
    command = config.get('command')
    if config.get('preset') != 'briskbot' or fields.get('space') != 'dm' or not isinstance(command, list) or len(command) < 2:
        return None
    post = command[1]
    if not isinstance(post, str) or Path(post).name != 'post-update.py' or '--space' not in command:
        return None
    path = Path(post).with_name('chat-replies.py')
    return [command[0], str(path)] if path.is_file() else None


def open_key(pane, thread, *, clock=time.time):
    """A new reply key for one ping to `pane` in conversation `thread`. The watcher starts reading at once.
    A ping without a known conversation gets no key: the answer could reach a later conversation in that pane."""
    if not thread:
        return None
    key = secrets.token_urlsafe(24)
    with _LOCK:
        data = _load()
        now = clock()
        keys = _live(data['keys'], now)
        keys[key] = {'pane': pane, 'thread': thread, 'sent': now}
        if len(keys) > MAX_KEYS:
            keys = dict(sorted(keys.items(), key=lambda item: item[1]['sent'])[-MAX_KEYS:])
        data['keys'] = keys
        _save(data)
    _WAKE.set()
    return key


def clean(text):
    """The answer as the tab gets it: no terminal control characters, CRLF as newlines."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127 and not 0x80 <= ord(c) < 0xa0)).strip()


def _read(argv, after, runner):
    try:
        out = runner(argv + ['--after', str(after)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'Teams replies: read failed: {exc}', file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f'Teams replies: read failed: {(out.stderr or "").strip()[-200:]}', file=sys.stderr)
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get('replies'), list) or not isinstance(data.get('last'), int):
        return None
    return data


def _outcome(result):
    """What Teams hears about one delivery row from broadcast.deliver."""
    name = result.get('name') or result.get('pane')
    if result.get('result') == 'sent':
        return f'The tab {name} has your answer.'
    if result.get('result') == 'sent_busy':
        return f'The tab {name} has your answer. It was working, so it reads it next.'
    return f'Not sent to {name}: {result.get("reason") or "the tab did not take it."} Answer in Tether.'


def _confirm(config, pane, text, runner):
    """A plain dm message with the result. Uses the configured interpreter and post-update.py."""
    command = config.get('command') or []
    argv = [command[0], command[1], 'Tether', text, '--space', 'dm', '--plain', '--force',
            '--topic', f'tether-reply:{pane}', '--no-collab']
    try:
        runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'Teams replies: confirm failed: {exc}', file=sys.stderr)


def _wait(keys, now):
    """Seconds to the next read: POLL soon after a ping, SLOW later, None with no open key."""
    if not keys:
        return None
    return POLL if max(v['sent'] for v in keys.values()) > now - FAST else SLOW


def _advance(sequence, clock):
    """Move the saved cursor to `sequence` and return the open keys."""
    with _LOCK:
        data = _load()
        data['after'] = max(data['after'], sequence)
        data['keys'] = _live(data['keys'], clock())
        _save(data)
        return data['keys']


def poll_once(config, deliver, *, clock=time.time, runner=subprocess.run):
    """Read new answers once and deliver them. Returns the seconds to the next read, or None with no open key."""
    argv = reader(config)
    with _LOCK:
        data = _load()
        keys = _live(data['keys'], clock())
    if not keys or argv is None:
        return None
    after = data['after']
    while True:
        page = _read(argv, after, runner)
        if page is None:
            return _wait(keys, clock())
        for reply in page['replies']:
            if not isinstance(reply, dict) or not isinstance(reply.get('sequence'), int) or reply['sequence'] <= after:
                continue
            # The cursor moves before each attempt: a crash or restart never types one answer twice,
            # and an error on one answer does not drop the answers after it.
            keys = _advance(reply['sequence'], clock)
            target = keys.get(reply.get('key')) if isinstance(reply.get('key'), str) else None
            if target is None or not target.get('thread'):
                continue  # an expired key, or one from before a reset
            text = clean(str(reply.get('text') or ''))
            if not text:
                continue
            pane = target['pane']
            try:
                result = deliver('[Answer from Teams] ' + text, {'pane': pane, 'thread': target['thread']})
            except Exception as exc:  # report it in Teams and go on with the next answer
                print(f'Teams replies: delivery failed: {exc}', file=sys.stderr)
                result = {'pane': pane, 'result': 'failed', 'reason': 'Tether could not send it.'}
            _confirm(config, pane, _outcome(result), runner)
        last = max(page['last'], after)
        keys = _advance(last, clock)
        if last == after:
            return _wait(keys, clock())
        after = last


def watch(config_of, deliver, *, clock=time.time, runner=subprocess.run):
    """Run forever: read while a key is open, else sleep until open_key() wakes it."""
    while True:
        try:
            wait = poll_once(config_of(), deliver, clock=clock, runner=runner)
        except Exception as exc:  # the watcher must outlive one bad read
            print(f'Teams replies: {exc}', file=sys.stderr)
            wait = SLOW
        _WAKE.wait(IDLE if wait is None else wait)
        _WAKE.clear()
