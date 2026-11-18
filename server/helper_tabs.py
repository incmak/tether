"""Owned helper tabs, guarded messages, and short turn polls."""
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata

import paths
from agents import BOUND
from terminal_input import UUID

OPTION = '@tether_helper'
AGENTS = ('claude', 'codex', 'antigravity')
MAX_REPLY = 100000
MAX_PROMPT = 8000
SETTLE = 3
NO_TURN_GRACE = 60
START_WAIT = 300
MISSING_GRACE = 10
_missing = {}
SEND_LOCK = threading.Lock()


class Stale(RuntimeError):
    pass


def _thread(entry):
    thread = entry.get('thread')
    return thread if isinstance(thread, str) and UUID.fullmatch(thread) and not entry.get('pending_conversation') else None


def verified_thread(entry):
    """Return the conversation identity this entry can prove."""
    if entry is None:
        return None
    agent = entry.get('agent')
    if agent == 'claude' and not entry.get('registry'):
        return None
    if agent == 'codex' and (entry.get('codex_binding') not in BOUND or entry.get('codex_unresolved')):
        return None
    return _thread(entry)


def new_record(caller):
    return {'owner_pane': caller['id'], 'owner_thread': verified_thread(caller),
            'helper_thread': None, 'sent_at': time.time(), 'before': None}


def save(pane, record, run):
    run([paths.TMUX, 'set-option', '-p', '-t', pane, OPTION, json.dumps(record, separators=(',', ':'))],
        check=True, capture_output=True, timeout=5)


def load(pane, run):
    try:
        result = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_id}\t#{@tether_helper}'],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Could not read the helper tab.') from exc
    fields = result.stdout.rstrip('\n').split('\t', 1)
    if result.returncode or fields[0] != pane:
        return False, None
    try:
        record = json.loads(fields[1])
    except (ValueError, IndexError):
        return True, None
    if (not isinstance(record, dict) or not isinstance(record.get('owner_pane'), str)
            or not re.fullmatch(r'%[0-9]+', record['owner_pane'])
            or not isinstance(record.get('sent_at'), (int, float))
            or isinstance(record.get('sent_at'), bool)):
        record = None
    return True, record


def check(caller_pane, caller, helper, record, run, *, pane=None):
    # A discovery miss has no helper entry; retain the requested pane for owner binding.
    if record is None or record['owner_pane'] != caller_pane:
        raise ValueError('That tab is not a helper of this tab.')
    pane = helper['id'] if helper is not None else pane
    bindings = {}
    for side, entry, subject, stale in (
            ('owner', caller, 'this tab', 'This tab changed conversations. Its helper tabs no longer accept its commands.'),
            ('helper', helper, 'that helper', 'That helper changed conversations. Start a new helper.')):
        if side == 'helper' and (entry is None or entry.get('agent') not in AGENTS):
            continue  # no agent runs there: no conversation to confirm; poll reports Missing, then gone
        key = side + '_thread'
        bound, seen = record.get(key), verified_thread(entry)
        if bound:
            if seen is None:
                raise RuntimeError(f"Tether cannot confirm {subject}'s conversation right now. Try again in a moment.")
            if seen != bound:
                print(f"helper check refused {pane} ({side}): bound {bound} seen {seen} agent {entry.get('agent')} "
                      f"binding {entry.get('codex_binding')} pending {bool(entry.get('pending_conversation'))}",
                      file=sys.stderr)
                raise Stale(stale)
        elif bound is None and seen is not None:
            bindings[key] = seen
    if bindings and verified_thread(helper) is not None:  # a refused or unconfirmed call writes nothing
        record.update(bindings)
        save(pane, record, run)


def _pane(body):
    pane = body.get('pane')
    if not isinstance(pane, str) or not re.fullmatch(r'%[0-9]+', pane):
        raise ValueError('Select a helper tab.')
    return pane


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _helper(entries, pane):
    return next((entry for entry in entries
                 if entry['id'] == pane and entry.get('agent') in ('claude', 'codex')), None)


def send_message(body, caller, entries, run, status, send, last_reply, lock):
    pane = _pane(body)
    message = body.get('message')
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 20000:
        raise ValueError('Enter a message with 1 to 20000 characters.')
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in message):
        raise ValueError('The message contains terminal control characters.')
    exists, record = load(pane, run)
    if not exists:
        raise ValueError('That session has ended. Select another tab.')
    helper = _helper(entries, pane)
    check(caller['id'], caller, helper, record, run, pane=pane)
    if helper is None:
        if record.get('helper_thread') is None and time.time() - record['sent_at'] < START_WAIT:
            raise RuntimeError('That helper is still starting. Try again in a moment.')
        raise ValueError('That session has ended. Select another tab.')
    if verified_thread(helper) is None:  # no write and no delivery until its conversation is confirmed
        raise RuntimeError('That helper is still starting. Try again in a moment.')
    if not helper.get('writable'):
        raise RuntimeError('Messaging is unavailable for that helper.')
    state = status(helper)['state']
    if state == 'Unavailable':
        raise RuntimeError('Messaging is unavailable for that helper.')
    if state in ('Approval', 'Input needed'):
        raise RuntimeError('That helper waits on a prompt. Use wait to read it, then ask the user to answer it.')
    with lock:
        previous = dict(record)
        record.update(sent_at=time.time(), before=digest(last_reply(helper)))
        save(pane, record, run)
        try:
            delivery = send(helper, message, state)
        except BaseException:
            try:
                save(pane, previous, run)
            except Exception:
                pass
            raise
    return 200, {'sent': True, 'pane': pane, 'delivery': delivery}


def _approval_prompt(text, options):
    choices = []
    if isinstance(options, list):
        for position, option in enumerate(options, 1):
            if not isinstance(option, dict):
                continue
            label = option.get('text')
            if not isinstance(label, str) or not label.strip():
                continue
            number = option.get('id')
            if not isinstance(number, int):
                number = position
            choices.append(f'{number}. {label}')
    if not choices:
        return text[:MAX_PROMPT]
    choices = '\n'.join(choices)
    request_limit = MAX_PROMPT - len(choices) - 2
    if text and request_limit > 0:
        return text[:request_limit] + '\n\n' + choices
    return choices[:MAX_PROMPT]


def poll(body, caller, entries, run, status, last_reply, timeline, now=time.time):
    pane = _pane(body)
    exists, record = load(pane, run)
    if not exists:
        _missing.pop(pane, None)
        return {'status': 'gone', 'pane': pane}
    caller_pane = caller['id'] if caller is not None else body.get('session')
    if record is None or record['owner_pane'] != caller_pane:
        raise ValueError('That tab is not a helper of this tab.')
    if caller is None:
        raise RuntimeError('Could not read this tab. Try again.')
    helper = _helper(entries, pane)
    check(caller['id'], caller, helper, record, run, pane=pane)
    current = now()
    running = {'status': 'running', 'pane': pane}
    if helper is None:
        if record.get('helper_thread') is None and current - record['sent_at'] < START_WAIT:
            return dict(running, state='Starting')
        missing = _missing.setdefault(pane, current)
        if current - missing >= MISSING_GRACE:
            _missing.pop(pane, None)
            return {'status': 'gone', 'pane': pane}
        return dict(running, state='Missing')
    _missing.pop(pane, None)
    info = status(helper)
    state = info['state']
    if state in ('Approval', 'Input needed'):
        attention = (info.get('approval' if state == 'Approval' else 'attention') or {})
        text = attention.get('text') or ''
        if state == 'Approval':
            text = _approval_prompt(text, attention.get('options'))
        bridge = attention.get('askBridge')
        if state == 'Input needed' and isinstance(bridge, dict) and bridge.get('questions'):
            text += '\n' + json.dumps(bridge['questions'], ensure_ascii=False)
        return {'status': 'needs_input', 'pane': pane,
                'kind': 'approval' if state == 'Approval' else 'input', 'prompt': text[:MAX_PROMPT]}
    if state != 'Idle':
        return dict(running, state=state)
    seen = timeline(pane)
    if isinstance(seen.get('since'), (int, float)) and current - seen['since'] < SETTLE:
        return dict(running, state=state)
    reply = last_reply(helper)
    turn_seen = (isinstance(seen.get('finished'), (int, float)) and seen['finished'] >= record['sent_at']
                 or bool(reply.strip()) and digest(reply) != record.get('before'))
    if turn_seen:
        return {'status': 'finished', 'pane': pane, 'reply': reply[:MAX_REPLY],
                'truncated': len(reply) > MAX_REPLY}
    if current - record['sent_at'] >= NO_TURN_GRACE:
        return {'status': 'no_turn', 'pane': pane, 'state': state}
    return dict(running, state=state)
