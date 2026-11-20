"""Owned helper tabs, guarded messages, and short turn polls."""
from dataclasses import dataclass
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
PANE = re.compile(r'%[0-9]+')
AGENTS = ('claude', 'codex', 'antigravity')
MAX_REPLY = 100000
MAX_PROMPT = 8000
SETTLE = 3
NO_TURN_GRACE = 60
START_WAIT = 300
MISSING_GRACE = 10
_missing = {}
# Record writers take this before the server send lock.
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


def visible_name(entry):
    return entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or None


def save(pane, record, run):
    run([paths.TMUX, 'set-option', '-p', '-t', pane, OPTION, json.dumps(record, separators=(',', ':'))],
        check=True, capture_output=True, timeout=5)


def write_record(pane, record, run):
    if record is None:
        run([paths.TMUX, 'set-option', '-p', '-t', pane, '-u', OPTION],
            check=True, capture_output=True, timeout=5)
    else:
        save(pane, record, run)


def _record(raw):
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(record, dict) or not isinstance(record.get('owner_pane'), str)
            or not PANE.fullmatch(record['owner_pane'])
            or not isinstance(record.get('sent_at'), (int, float))
            or isinstance(record.get('sent_at'), bool)):
        return None
    if record.get('kind') not in ('helper', 'own'):
        record.pop('kind', None)
    home_pane = record.get('home_pane')
    if home_pane is not None and not (isinstance(home_pane, str) and PANE.fullmatch(home_pane)):
        record.pop('home_pane')
    home_thread = record.get('home_thread')
    if home_thread is not None and not (isinstance(home_thread, str) and UUID.fullmatch(home_thread)):
        record.pop('home_thread')
    name = record.get('opener_name')
    if not isinstance(name, str) or not 1 <= len(name) <= 40:
        record.pop('opener_name', None)
    return record


def placement_owner(record):
    """The pane and conversation used for placement, including legacy records."""
    if record.get('kind') == 'own':
        return None, None
    if record.get('home_pane'):
        return record['home_pane'], record.get('home_thread')
    return record['owner_pane'], record.get('owner_thread')


def load(pane, run):
    try:
        result = run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_id}\t#{@tether_helper}'],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Could not read the helper tab.') from exc
    found, _, raw = result.stdout.rstrip('\n').partition('\t')
    if result.returncode or found != pane:
        return False, None
    return True, _record(raw)



@dataclass(frozen=True)
class PaneOptions:
    record: dict | None
    raw: str = ''
    role: str = ''
    group_by: str = ''
    session: str = ''
    done: str = ''
    reopened: str = ''
    done_at: str = ''


def read_panes(run):
    """Read placement and marks together for all panes on this tmux server."""
    names = ('pane_id', '@group_by', '@tether_role', OPTION, 'session_id',
             '@tether_done', '@tether_reopened', '@tether_done_at')
    fmt = '\t'.join('#{' + name + '}' for name in names)
    command = [paths.TMUX, 'list-panes', '-a', '-F', fmt]
    result = run(command, capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    panes = {}
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) != len(names) or not PANE.fullmatch(parts[0]):
            continue
        pane, group_by, role, raw, session, done, reopened, done_at = parts
        panes[pane] = PaneOptions(_record(raw), raw, role, group_by, session, done, reopened, done_at)
    return panes


def listing_fields(run, entries, *, options=None):
    """Read the helper fields for each listing row in one tmux call."""
    import task_tree

    options = read_panes(run) if options is None else options
    by_id = {entry['id']: entry for entry in entries}
    tasks = task_tree.placements(options, by_id)
    fields = {}
    for pane, option in options.items():
        if pane not in by_id or option.record is None:
            continue
        record, group_by, role = option.record, option.group_by, option.role
        opener_pane = record['owner_pane']
        opener = by_id.get(opener_pane)
        live = opener is not None and opener_pane != pane and opener.get('agent') != 'shell'
        if live:
            bound, current = record.get('owner_thread'), _thread(opener)
            live = not (bound and current and bound != current)
        row = fields[pane] = {'opened_by': {'pane': opener_pane, 'live': live,
                                            'name': visible_name(opener) if live else record.get('opener_name')}}
        kind = record.get('kind')
        if live:
            row['parent'] = opener_pane
            if kind is None:
                row['owner'] = opener_pane
        if kind == 'helper':
            home_pane, home_thread = placement_owner(record)
            home = by_id.get(home_pane)
            if (home is not None and home_pane != pane and home.get('agent') != 'shell'
                    and home_thread is not None and _thread(home) == home_thread):
                row['owner'] = home_pane
        if group_by != 'user' and (kind == 'helper' or
                                  kind is None and role in ('reviewer', 'advisor', 'second-advisor')):
            row['helper'] = True
        if pane in tasks:
            row.update(tasks[pane])
        elif opener_pane != pane:
            row['from'] = dict(row['opened_by'], name=row['opened_by']['name'] or opener_pane)
        if record.get('moved_by') == 'user':
            row['moved_by'] = 'user'
    return fields


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
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise ValueError('Select a helper tab.')
    return pane


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _helper(entries, pane):
    return next((entry for entry in entries
                 if entry['id'] == pane and entry.get('agent') in ('claude', 'codex')), None)


def send_message(body, caller, entries, run, status, send, last_reply, lock, before_send=None):
    pane = _pane(body)
    message = body.get('message')
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 20000:
        raise ValueError('Enter a message with 1 to 20000 characters.')
    if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in message):
        raise ValueError('The message contains terminal control characters.')
    with lock:
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
        previous = dict(record)
        record.update(sent_at=time.time(), before=digest(last_reply(helper)))
        save(pane, record, run)
        try:
            # Inside the lock and before delivery, so the helper's turn cannot finish before this runs.
            if before_send is not None:
                before_send(helper)
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
