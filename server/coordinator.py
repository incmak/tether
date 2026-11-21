"""Coordinator tab: one agent tab per tmux session that routes the user's tasks to the other tabs and reports on them.

A tab becomes the coordinator through Tether or when its agent runs `agent_coordinator_cli.py claim`. The pane option
@tether_role (coordinator, coordinator-needs or coordinator-off: which updates it gets) marks it. Session discovery
reads that option (entry['coordinator']) and a resumed pane keeps it.
The coordinator reads only the state and the last reply of each tab (tabs, reply), never a transcript. It may send a
message to any agent tab of its own session (send). To start a tab it uses agent_tab_cli.py as every tab does.

Feed.observe runs in the server monitor. When a tab finishes a turn, fails, or waits on the user, the feed sends
the coordinator one short status message, so the coordinator can tell the user without polling. It sends only while the
coordinator is idle, joins updates that come close together, and keeps them when the send fails.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata

import agents
import attention
import auth
import codex_messages
import helper_tabs
import history
import needs
import paths
import push

PATH = '/mobile/api/coordinator'
ROLE_PATH = '/mobile/api/coordinator-role'
OPTION = '@tether_role'
MODES = ('all', 'needs', 'off')
AGENTS = ('claude', 'codex')
PANE = re.compile(r'%[0-9]+')
MARK = '[Tether status]'
MAX_REPLY = 20000
SUMMARY = 300
SETTLE = 4          # a state must hold this long before it counts
MINIMUM_WORK = 5    # a shorter turn is not worth an update
JOIN = 15           # wait this long for more updates before a send
GAP = 30            # least time between two sends to one coordinator
RETRY = 60          # after a failed send
HOLD = 600          # a missing or restarting coordinator can return to its chat
MAX_EVENTS = 20
FINISHED = ('done', 'asks', 'failed')
KNOWN = ('Idle', 'Working', 'Approval', 'Input needed')   # other states (Unknown, Unavailable) may change back
CLAIM_FIRST = 'This tab is not the coordinator. Run claim first, when the user asked this tab to coordinate.'


def cli():
    return f'python3 {Path(__file__).resolve().parent / "agent_coordinator_cli.py"}'


def tab_cli():
    return f'python3 {Path(__file__).resolve().parent / "agent_tab_cli.py"}'


def name_of(entry):
    return entry.get('pane_name') or entry.get('agent_name') or entry.get('label') or entry['id']


def rules():
    """What a tab gets when it becomes the coordinator."""
    own, tab = cli(), tab_cli()
    return (
        'You are the coordinator of this Tether session. The user gives you tasks and questions here. '
        'The other tabs do the work.\n'
        '1. A quick question: answer it here. Do not start a tab for it.\n'
        f'2. See the tabs: `{own} tabs` (name, state, what each waits on, the start of its last reply). '
        f'`{own} reply --pane %N` prints the full last reply of one tab. You see only what tabs report, not their steps.\n'
        f'3. A task that fits a tab that already works in that area: `{own} send --pane %N "text"` (`-` = stdin). '
        'A tab that waits on a prompt takes no message: tell the user to answer it in that tab.\n'
        f'4. A new task: `{tab} start --name NAME [--worktree BRANCH] "full spec"`. Give the goal, the files, the limits '
        'and the checks. One tab per task. Several unrelated tasks in one message become several tabs.\n'
        '5. Do not do the task yourself and do not `wait` on a tab. Tether sends you a line that starts with '
        f'"{MARK}" when a tab finishes or needs the user. Then tell the user in one or two lines per tab what changed '
        'and what needs them. Say nothing when nothing changed for the user.\n'
        f'6. The user sets how much you report, in words. Then run `{own} events all|needs|off` '
        '(all: finished and waiting tabs; needs: tabs that wait on the user or failed; off: none) and follow it.\n'
        '7. Tell the user which tab took each task. Write tab names in backticks.\n'
        f'8. `{own} release` ends the coordinator role.')


def _run(run, args):
    return run([paths.TMUX, *args], capture_output=True, text=True, timeout=5)


def marks(entries):
    """{pane: (session id, mode)} for every live agent tab that holds the coordinator mark."""
    return {entry['id']: (entry.get('tmux_session'), entry['coordinator']) for entry in entries
            if entry.get('coordinator') in MODES and entry.get('agent') in AGENTS}


def _set(run, pane, mode):
    args = (['set-option', '-p', '-t', pane, OPTION, 'coordinator' if mode == 'all' else 'coordinator-' + mode]
            if mode else ['set-option', '-p', '-u', '-t', pane, OPTION])
    try:
        failed = _run(run, args).returncode
    except (OSError, subprocess.SubprocessError):
        failed = True
    if failed:
        raise RuntimeError('Could not change the coordinator mark.')


def _mode(body, default):
    mode = body.get('events', default)
    if mode not in MODES:
        raise ValueError('Choose all, needs or off for events.')
    return mode


def _peers(caller, entries):
    return [entry for entry in entries
            if entry['id'] != caller['id'] and entry.get('tmux_session') == caller.get('tmux_session')
            and entry.get('agent') in AGENTS]


def row(entry, status, last_reply, finished=None):
    info = status(entry)
    waits = needs.summary(info)
    reply = last_reply(entry)
    asks, _ = attention.reply_ask(reply) if reply.strip() else (None, '')
    out = {'pane': entry['id'], 'name': name_of(entry), 'agent': entry['agent'], 'state': info.get('state'),
           'last_reply': push.reply_summary(reply, SUMMARY)}
    if waits:
        out['waits_on'] = f'{waits["kind"]}: {waits["text"]}'
    elif asks == 'yes' and info.get('state') == 'Idle':
        out['waits_on'] = 'its last reply asks the user'
    if entry.get('worktree'):
        out['worktree'] = entry['worktree']
    if entry.get('role') or entry.get('helper'):
        out['helper'] = entry.get('role') or entry.get('helper')
    if isinstance(finished, (int, float)):
        # server.track records finished in wall-clock time.
        out['finished_seconds_ago'] = max(0, int(time.time() - finished))
    return out


def _target(body, caller, entries):
    pane = body.get('pane')
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise ValueError('Select a tab with --pane %N.')
    if pane == caller['id']:
        raise ValueError('That is this tab.')
    target = next((entry for entry in _peers(caller, entries) if entry['id'] == pane), None)
    if target is None:
        raise ValueError('That tab is not an agent tab of this session. Run tabs for the list.')
    return target


def _same_chat(entry, session, transcript):
    return entry is not None and (entry.get('tmux_session'), entry.get('transcript')) == (session, transcript)


def _chat_known(entry):
    return entry is not None and entry.get('transcript') is not None and not entry.get('pending_conversation')


IN_TASK = 'A tab inside a task cannot be the coordinator. Move it out of its task first.'


def _change_mark(body, caller, entries, run, *, on_claim=None):
    """Change the mark using fresh entries while the caller holds the lock."""
    action = body['action']
    found = marks(entries)
    pane = caller['id']
    mine = found.get(pane)
    if action != 'claim':
        if mine is None:
            raise ValueError(CLAIM_FIRST)
        if action == 'release':
            _set(run, pane, None)
            return {'released': pane}
        mode = _mode(body, None)
        _set(run, pane, mode)
        return {'events': mode}

    # Hold the record lock from the placement check to the mark: a move, sort or Undo cannot place the tab in a task
    # in between (write_record checks the mark under the same lock).
    with helper_tabs.record_lock():
        return _claim(body, caller, entries, run, mine, on_claim)


def _claim(body, caller, entries, run, mine, on_claim):
    pane = caller['id']
    mode = _mode(body, mine[1] if mine else 'all')
    if mine is None and (caller.get('role') or caller.get('helper')):
        raise ValueError('A reviewer or advisor tab cannot be the coordinator.')
    if mine is None and (caller.get('task') or helper_tabs.placed(helper_tabs.load(pane, run)[1])):
        raise ValueError(IN_TASK)
    other = next((entry for entry in entries if entry['id'] != pane and entry.get('coordinator') in MODES
                  and entry.get('tmux_session') == caller.get('tmux_session')), None)
    replaced = None
    if other is not None:
        if body.get('take') is not True:
            raise ValueError(f'The tab `{name_of(other)}` ({other["id"]}) is the coordinator of this session. '
                             'Run release there, or claim --take when the user asked for the change.')
        _set(run, other['id'], None)
        replaced = other['id']
    marked = False
    try:
        _set(run, pane, mode)
        marked = True
        if on_claim is not None:
            on_claim()
    except Exception as exc:
        prior_marks = [(pane, mine[1] if mine else None)] if marked else []
        if replaced is not None:
            prior_marks.append((replaced, other['coordinator']))
        for prior_pane, prior_mode in prior_marks:
            try:
                _set(run, prior_pane, prior_mode)
            except RuntimeError as rollback:
                exc.add_note(f'{rollback} Pane {prior_pane} did not restore its previous mark.')
        raise
    reply = {'coordinator': pane, 'events': mode, 'rules': rules()}
    if replaced is not None and on_claim is not None:
        reply['replaced'] = replaced
    return reply


def browser_role(body, *, fresh, lock, run, status, send):
    """Set a browser-selected chat's role and tell the agent when it first claims the role."""
    pane, thread, mode = body.get('pane'), body.get('thread'), body.get('mode')
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise ValueError('Select a pane.')
    if 'mode' not in body or mode is not None and mode not in MODES:
        raise ValueError('Choose all, needs or off for the coordinator, or release it.')
    with lock:
        entries = fresh()
        target = next((entry for entry in entries if entry['id'] == pane), None)
        if target is None or not isinstance(thread, str) or not thread or thread != target.get('thread'):
            raise ValueError('That tab started a new chat. Try again.')
        if target.get('agent') not in AGENTS:
            raise ValueError('Only a Claude or Codex tab can be the coordinator.')
        if target.get('role') or target.get('helper'):
            raise ValueError('A reviewer or advisor tab cannot be the coordinator.')
        new_claim = mode is not None and target.get('coordinator') not in MODES
        if new_claim:
            state = _send_state(target, status)   # refuse before any mark changes; a Working tab gets a held send

            def notify():
                send(target, '[Tether] The user made this tab the coordinator of this session in Tether.\n' + rules(), state)

            changed = _change_mark({'action': 'claim', 'events': mode, 'take': body.get('take')},
                                   target, entries, run, on_claim=notify)
        elif target.get('coordinator') in MODES:
            changed = _change_mark({'action': 'release' if mode is None else 'events', 'events': mode},
                                   target, entries, run)
        else:
            changed = {}
    return {'coordinator': pane if mode is not None else None, 'events': mode,
            'sent': new_claim, 'replaced': changed.get('replaced')}


def _send_state(target, status):
    if not target.get('writable'):
        raise RuntimeError('Messaging is unavailable for that tab.')
    state = status(target).get('state')
    if state not in KNOWN:
        raise RuntimeError('Messaging is unavailable for that tab.')
    if state in ('Approval', 'Input needed'):
        raise RuntimeError('That tab waits on a prompt. Ask the user to answer it in that tab.')
    return state


def act(body, caller, entries, *, fresh, lock, run, status, fresh_status, send, last_reply, timeline):
    """One coordinator command from the tab `caller`. Returns (HTTP status, reply)."""
    action = body.get('action')
    if caller.get('agent') not in AGENTS:
        raise ValueError('Only a Claude or Codex tab can be the coordinator.')
    pane, session, chat = caller['id'], caller.get('tmux_session'), caller.get('transcript')
    if action in ('claim', 'release', 'events'):
        with lock:
            entries = fresh()
            current = next((entry for entry in entries if entry['id'] == pane), None)
            if not _same_chat(current, session, chat) or current.get('agent') not in AGENTS:
                raise RuntimeError('This tab moved or started a new chat.')
            reply = _change_mark(body, current, entries, run)
        if action == 'claim':
            reply['tabs'] = [row(entry, status, last_reply, timeline(entry['id']).get('finished'))
                             for entry in _peers(current, entries)]
        return 200, reply
    mine = marks(entries).get(pane)
    if mine is None:
        raise ValueError(CLAIM_FIRST)
    if action == 'tabs':
        return 200, {'events': mine[1],
                     'tabs': [row(entry, status, last_reply, timeline(entry['id']).get('finished'))
                              for entry in _peers(caller, entries)]}
    if action == 'reply':
        target = _target(body, caller, entries)
        reply = last_reply(target)
        return 200, {'pane': target['id'], 'name': name_of(target), 'state': status(target).get('state'),
                     'reply': reply[:MAX_REPLY], 'truncated': len(reply) > MAX_REPLY}
    if action == 'send':
        target = _target(body, caller, entries)
        message = body.get('message')
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 20000:
            raise ValueError('Enter a message with 1 to 20000 characters.')
        if any(unicodedata.category(char) == 'Cc' and char not in '\n\t' for char in message):
            raise ValueError('The message contains terminal control characters.')
        _send_state(target, status)
        target_pane, target_chat = target['id'], target.get('transcript')
        with lock:
            entries = fresh()
            live = {entry['id']: entry for entry in entries}
            if not _same_chat(live.get(pane), session, chat) or pane not in marks(entries):
                raise RuntimeError('This tab moved, started a new chat or is no longer the coordinator.')
            target = live.get(target_pane)
            if not _same_chat(target, session, target_chat) or target.get('agent') not in AGENTS:
                raise ValueError('That tab moved or started a new chat. Run tabs again.')
            state = _send_state(target, fresh_status)
            delivery = send(target, message, state)
        return 200, {'sent': True, 'pane': target['id'], 'name': name_of(target), 'delivery': delivery}
    raise ValueError('Unknown action.')


def serve(handler, body, *, sessions, fresh, lock, run, status, fresh_status, send, last_reply, timeline):
    credential = handler.headers.get('X-Tether-Hook', '')
    if (not auth.is_loopback(handler.client_address[0]) or not credential
            or not hmac.compare_digest(credential.encode(), auth.session_value().encode())):
        handler.respond(403, {'error': 'Local hook authentication required.'})
        return
    entries = sessions()
    caller = next((entry for entry in entries if entry['id'] == body.get('session')), None)
    if caller is None:
        raise ValueError('That session has ended. Select another tab.')
    handler.respond(*act(body, caller, entries, fresh=fresh, lock=lock, run=run, status=status,
                         fresh_status=fresh_status, send=send, last_reply=last_reply, timeline=timeline))


def message(events, mode):
    """The status text for the coordinator: one line per tab."""
    lines = [f'{MARK} {len(events)} tab update{"" if len(events) == 1 else "s"}. Tell the user what changed and what '
             f'needs them, in one or two lines per tab. Reports are set to "{mode}". If you do not have the '
             f'coordinator rules, run `{cli()} claim`.']
    words = {'done': 'finished', 'asks': 'finished and asks the user', 'needs': 'waits on the user', 'failed': 'failed'}
    for event in events:
        text = f': {event["text"]}' if event['text'] else ''
        lines.append(f'- `{event["name"]}` ({event["pane"]}) {words[event["kind"]]}{text}')
    return '\n'.join(lines)


def _error_text(record, fallback):
    notice = agents.error_message(record)
    if notice:
        return notice
    for key in ('message', 'text', 'content', 'error'):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict) and isinstance(value.get('message'), str) and value['message'].strip():
            return value['message']
    return fallback


def _fingerprint(outcome):
    return outcome['kind'], outcome['identity'], outcome['text']


def _claude_turn(state, index, record):
    kind = record.get('type')
    if any(record.get(flag) for flag in ('isSidechain', 'isCompactSummary', 'isAutoCompactSummary')):
        return
    if kind not in ('user', 'assistant', 'system', 'attachment'):
        return
    subtype = record.get('subtype')
    if kind == 'system' and subtype == 'turn_duration':
        if state.get('draft'):
            identity, text = state['draft']
            state.update(kind='done', text=text, identity=identity)
        state.update(active=None, pending=None, draft=None, interrupted=False)
        return
    system_error = kind == 'system' and subtype in ('error', 'api_error')
    if kind == 'system' and not system_error:
        return

    message = record.get('message')
    message = message if isinstance(message, dict) else {}
    content = message.get('content')
    if kind == 'attachment':
        attachment = record.get('attachment')
        if not isinstance(attachment, dict) or attachment.get('type') != 'queued_command':
            return
        content = attachment.get('prompt')
    parts = [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []
    text = content if isinstance(content, str) else '\n'.join(
        part['text'] for part in parts if part.get('type') == 'text' and isinstance(part.get('text'), str))
    record_id = str(record.get('uuid') or index)
    pending = state.get('pending')
    active = state.get('active')
    if kind in ('user', 'attachment'):
        if kind == 'user' and (record.get('interruptedMessageId') or text.startswith('[Request interrupted by user')):
            if active and not pending:
                state['identity'] = active[0]
            state.update(kind=None, text='', active=None, pending=None, draft=None, interrupted=True)
        elif isinstance(content, (str, list)) and not any(part.get('type') == 'tool_result' for part in parts):
            # Shell output and skill bodies can be additional records for the same prompt.
            prompt = record.get('promptId')
            current = pending or active
            companion = (record.get('turnCompanion')
                         or pending and record.get('isMeta') and not prompt and not record.get('turnOrigin'))
            if not (current and (companion or prompt and prompt == current[1])):
                state['pending'] = (record_id, prompt)
        return

    api_error = kind == 'assistant' and record.get('isApiErrorMessage') is True
    aborted = kind == 'assistant' and record.get('isAbortedMidStream')
    if not (api_error or system_error or aborted or text.strip()) or (state.get('interrupted') and not pending):
        return
    # Some versions omit turn_duration, including after completed replies and usage-limit errors.
    # Keep input pending until a response arrives. A turn with no input record uses its first response.
    active = pending or active or (record_id, None)
    identity = active[0]
    state.update(active=active, pending=None, interrupted=False)
    if aborted:
        state.update(kind=None, text='', identity=identity, active=None, draft=None, interrupted=True)
    elif api_error or system_error:
        error = dict(record, message=text) if api_error else record
        state.update(kind='failed', text=_error_text(error, 'API error.'), identity=identity, draft=None)
    elif message.get('stop_reason') == 'tool_use':
        state['draft'] = (identity, text)
    else:
        state.update(kind='done', text=text, identity=identity, draft=None)


def _codex_turn(state, index, record):
    payload = record.get('payload') or {}
    kind, turn = payload.get('type'), payload.get('turn_id')
    if record.get('type') == 'response_item':
        if (state.get('active') and kind == 'message' and payload.get('role') == 'assistant'
                and payload.get('phase') in (None, 'final_answer')):
            text = '\n'.join(part['text'] for part in codex_messages.parts(payload) if not part.get('kind'))
            if text.strip():
                state.update(kind=None, text='', draft=text)
        return
    if record.get('type') != 'event_msg' or payload.get('root_turn_id', turn) != turn:
        return
    if kind == 'task_started' or (kind == 'user_message' and not state.get('active')):
        state.update(kind=None, text='', identity=str(turn or index), active=True, draft='')
    elif kind == 'turn_aborted':
        reason = payload.get('reason')
        identity = str(turn or (state['identity'] if state.get('active') else index))
        cancelled = reason in ('interrupted', 'replaced', 'review_ended')
        state.update(kind=None if cancelled else 'failed', active=False, identity=identity,
                     text='' if cancelled else 'Turn ' + str(reason or 'aborted') + '.')
    elif kind == 'error':
        identity = str(turn or (state['identity'] if state.get('active') else index))
        state.update(kind='failed', text=_error_text(payload, 'Agent error.'), identity=identity, active=True)
    elif kind == 'task_complete':
        identity = str(turn or (state['identity'] if state.get('active') else index))
        error = payload.get('error')
        if isinstance(error, dict):
            text = error.get('message')
            state.update(kind='failed', text=text if isinstance(text, str) else 'Agent error.',
                         identity=identity, active=False)
        else:
            text = payload.get('last_agent_message') or state.get('draft') or ''
            state.update(kind='done', text=text, identity=identity, active=False)
    elif state.get('active'):
        if state['kind'] == 'failed':
            state.update(kind=None, text='')
        if kind == 'agent_message' and payload.get('phase') != 'commentary' and isinstance(payload.get('message'), str):
            state['draft'] = payload['message']


def deliver(pane, queue, events, *, lock, fresh, fresh_status, latest_outcome, submit_locked):
    """Return sent and held events, [] for a stale queue, or None to retry."""
    if not lock.acquire(timeout=2):
        return None
    try:
        entries = fresh()
        by_pane = {entry['id']: entry for entry in entries}
        found = marks(entries)
        coordinator = by_pane.get(pane)
        if not _chat_known(coordinator) or coordinator.get('agent') not in AGENTS:
            return None
        if pane not in found or not _same_chat(coordinator, queue['session'], queue['chat']):
            return []
        if not coordinator.get('writable'):
            return None
        mode = found[pane][1]
        live, held = [], []
        for event in events:
            tab = by_pane.get(event['pane'])
            if mode == 'off' or tab is None or tab['id'] in found or tab.get('role') or tab.get('helper'):
                continue
            if not _chat_known(tab):
                held.append(event)
                continue
            if not _same_chat(tab, event['session'], event['chat']):
                continue
            info = fresh_status(tab)
            state = info.get('state')
            if state not in KNOWN:
                held.append(event)
            elif event['kind'] == 'needs':
                if prompt_identity(info) == event['token']:
                    live.append(event)
            elif state == 'Idle':
                outcome = latest_outcome(tab)
                if outcome['kind'] not in FINISHED:
                    continue
                if outcome['identity'] != event['identity']:
                    event = dict(event, **outcome)
                if wanted(event, mode):
                    live.append(event)
        if fresh_status(coordinator).get('state') != 'Idle':
            return None
        if live:
            # terminal_submit refuses a draft in the input.
            submit_locked(message(live, mode), coordinator)
        return live, held
    finally:
        lock.release()


class Feed:
    """Sends each coordinator the state changes of the tabs in its session."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.seen = {}      # peer pane -> state and report identities
        self.queues = {}    # coordinator pane -> chat, report mode, and delivery state
        self.outcomes = {}  # peer pane -> transcript signature and last outcome
        self.parsers = {}   # peer pane -> parser key and incremental history record

    def latest_outcome(self, entry):
        """The latest turn's outcome, with no transcript read when its file is unchanged."""
        empty = {'kind': None, 'text': '', 'identity': ''}
        pane, agent, path = entry['id'], entry.get('agent'), entry.get('transcript')
        if agent not in AGENTS or not path or entry.get('ambiguous'):
            return empty
        path = str(path)
        try:
            with history._lock_for(path):
                info = os.stat(path)
                key = (agent, path, info.st_dev, info.st_ino)
                signature = (info.st_size, info.st_mtime_ns)
                revision = 0
                cached = self.outcomes.get(pane)
                if cached and cached[0] == key:
                    _, previous, revision, outcome = cached
                    if previous == signature:
                        return outcome
                    if previous[0] >= signature[0]:
                        revision = info.st_mtime_ns  # a same-size rewrite or truncation needs a new parser state
                parser_key = (*key, revision)
                previous_key, parser = self.parsers.get(pane, (None, None))
                if previous_key != parser_key:
                    parser = None
                consume = _claude_turn if agent == 'claude' else _codex_turn
                parser = history.advance(path, parser, empty.copy, consume, trim=False, info=info)
                state = parser['state']
                kind, text = state['kind'], state['text']
                if kind == 'done' and attention.reply_ask(text)[0] == 'yes':
                    kind = 'asks'
                # open: a reply of a turn arrived and the turn has not ended, so `identity` may be the running turn.
                outcome = {'kind': kind, 'text': push.reply_summary(text, SUMMARY), 'identity': state['identity'],
                           'open': bool(state.get('active'))}
                self.parsers[pane] = (parser_key, parser)
                self.outcomes[pane] = (key, signature, revision, outcome)
                return outcome
        except OSError:
            return empty

    def _event(self, entry, info, latest_outcome, now):
        """The update that this tab's state gives now, once per change, else None."""
        if not _chat_known(entry):
            return None
        state = info.get('state')
        record = self.seen.get(entry['id'])
        prompt = prompt_identity(info)
        if record is None or not _same_chat(entry, record['tmux_session'], record['transcript']):
            # First sight of this tab: a prompt that is already on screen is in the tabs list, so it is no update.
            record = self.seen[entry['id']] = {'state': state, 'prompt': prompt, 'since': now, 'worked': None,
                                               'sent_prompt': prompt, 'finished': None, 'seeded': False,
                                               'running_at_first_sight': False, 'first': None,
                                               'tmux_session': entry.get('tmux_session'), 'transcript': entry.get('transcript')}
        if not record['seeded'] and state in KNOWN:
            seen = latest_outcome(entry)
            if state == 'Idle':
                record['finished'] = seen['identity'] or None
            # The outcome at first sight is old: a sign-in prompt that closes with no new turn reports nothing.
            # The transcript cannot always show that a turn ended (no turn_duration, a Codex turn that ends on an
            # error), so the whole outcome must change. Exception: a Working tab whose finished reply is already
            # written (Claude writes the final text before turn_duration, while Stop hooks run).
            running_reply = state == 'Working' and seen.get('open') and seen['kind'] in FINISHED
            record['first'] = None if running_reply else _fingerprint(seen)
            # A turn that runs now started before the first poll, so its full duration is not known.
            record['running_at_first_sight'] = state != 'Idle'
            record['seeded'] = True
        if state != record['state'] or prompt != record['prompt']:
            record['state'], record['prompt'], record['since'] = state, prompt, now
        if state == 'Working':
            record['worked'] = record['worked'] if record['worked'] is not None else now
            record['sent_prompt'] = None
            return None
        if now - record['since'] < SETTLE:
            return None
        if prompt:
            if record['sent_prompt'] == prompt:
                return None
            record['sent_prompt'] = prompt
            waits = needs.summary(info)
            return {'kind': 'needs', 'text': f'{waits["kind"]}: {waits["text"]}' if waits else '', 'token': prompt}
        if state != 'Idle':
            return None
        worked = record['worked']
        first_turn = record['running_at_first_sight']
        record.update(worked=None, running_at_first_sight=False)
        outcome = latest_outcome(entry)
        if record['first'] is not None:
            if _fingerprint(outcome) == record['first']:
                return None
            record['first'] = None
        if (outcome['kind'] not in FINISHED or outcome['identity'] == record['finished']
                or outcome['kind'] != 'failed' and not first_turn
                and (worked is None or record['since'] - worked < MINIMUM_WORK)):
            return None
        record['finished'] = outcome['identity']
        return outcome

    def observe(self, entries, status, deliver, latest_outcome, *, lock, fresh):
        now = self.clock()
        found = marks(entries)
        by_pane = {entry['id']: entry for entry in entries}
        if any(entry['id'] in self.queues and entry['id'] not in found
               and _chat_known(entry) and entry.get('agent') in AGENTS for entry in entries):
            # A takeover can restore an old mark on failure. Discovery can also cache the temporary marks.
            # Confirm the marks under the same lock that covers the takeover and its rollback.
            if not lock.acquire(blocking=False):
                return
            try:
                entries = fresh()
            finally:
                lock.release()
            found = marks(entries)
            by_pane = {entry['id']: entry for entry in entries}
        for pane in [pane for pane in self.seen if pane not in by_pane]:
            del self.seen[pane]
        for pane in [pane for pane in self.outcomes if pane not in by_pane]:
            del self.outcomes[pane]
        for pane in [pane for pane in self.parsers if pane not in by_pane]:
            del self.parsers[pane]
        for pane, queue in list(self.queues.items()):
            entry = by_pane.get(pane)
            away = queue['away_since']
            if away is not None and now - away >= HOLD:
                del self.queues[pane]
            elif _chat_known(entry) and entry.get('agent') in AGENTS:
                if pane not in found or not _same_chat(entry, queue['session'], queue['chat']):
                    del self.queues[pane]
                else:
                    queue['away_since'] = None
            else:
                if away is None:
                    queue['away_since'] = now
                if entry is not None and entry.get('coordinator') in MODES:
                    queue['mode'] = entry['coordinator']
        coordinators = {}   # session id -> coordinator pane, preferring a live agent
        for pane, (session, mode) in found.items():
            if not _chat_known(by_pane[pane]):
                continue
            queue = self.queues.setdefault(pane, {'events': [], 'sent_at': None, 'retry_at': None,
                                                  'session': session, 'chat': by_pane[pane].get('transcript'),
                                                  'away_since': None, 'mode': mode})
            queue['mode'] = mode
            coordinators.setdefault(session, pane)
        for pane, queue in self.queues.items():
            coordinators.setdefault(queue['session'], pane)
        if not coordinators:
            self.seen.clear()
            return
        queued = {event['pane'] for queue in self.queues.values() for event in queue['events']}
        for entry in entries:
            target = coordinators.get(entry.get('tmux_session'))
            pane = entry['id']
            eligible = (target is not None and pane not in found and entry.get('agent') in AGENTS
                        and entry.get('owner_pid') and entry.get('writable')
                        and not (entry.get('role') or entry.get('helper')))
            if not eligible and pane not in queued:
                continue    # reviewers and advisors report to the tab they help
            try:
                info = status(entry)
                if pane in queued and _chat_known(entry) and info.get('state') == 'Working':
                    for queue in self.queues.values():
                        queue['events'] = [old for old in queue['events'] if old['pane'] != pane]
                if not eligible:
                    continue
                event = self._event(entry, info, latest_outcome, now)
            except Exception:  # one odd pane must not stop the feed
                continue
            if event is None:
                continue
            queue = self.queues[target]
            if not wanted(event, queue['mode']):
                continue
            queue['events'] = [old for old in queue['events'] if old['pane'] != entry['id']]
            queue['events'].append(dict(event, pane=entry['id'], name=name_of(entry), at=now,
                                        session=entry.get('tmux_session'), chat=entry.get('transcript')))
            del queue['events'][:-MAX_EVENTS]
        for pane in found:
            if _chat_known(by_pane[pane]):
                self._flush(pane, by_pane[pane], self.queues[pane], status, deliver, now)

    def _flush(self, pane, coordinator, queue, status, deliver, now):
        """Wait for the send window before checking the live panes."""
        events = queue['events']
        if not events or now - events[-1]['at'] < JOIN and now - events[0]['at'] < 4 * JOIN:
            return
        if queue['sent_at'] is not None and now - queue['sent_at'] < GAP:
            return
        if queue['retry_at'] is not None and now < queue['retry_at']:
            return
        try:
            if status(coordinator).get('state') != 'Idle':
                return
            result = deliver(pane, queue, events)
        except Exception:  # a draft in the input, a prompt on screen, a closed pane: try again later
            queue['retry_at'] = now + RETRY
            return
        if result is None:
            return
        if result == []:
            del self.queues[pane]
            return
        sent, held = result
        queue['events'] = held
        if sent:
            queue.update(sent_at=now, retry_at=None)
            for event in sent:
                record = self.seen.get(event['pane'])
                if event['kind'] in FINISHED and _same_chat(record, event['session'], event['chat']):
                    record['finished'] = event['identity']


def prompt_identity(info):
    """The identity of the prompt a tab waits on, else None. One function for the once-per-prompt check and for the check
    at delivery. A missing id proves nothing (Codex and fallback approvals have none), so the prompt's own content
    stands in for it."""
    waiting = {'Approval': 'approval', 'Input needed': 'attention'}.get((info or {}).get('state'))
    if waiting is None:
        return None
    prompt = info.get(waiting) if isinstance(info.get(waiting), dict) else {}
    if prompt.get('id') is not None:
        return (waiting, 'id', str(prompt['id']))
    content = json.dumps(_plain({key: value for key, value in prompt.items() if key != 'id'}),
                         sort_keys=True, ensure_ascii=False, default=str)
    return (waiting, 'content', hashlib.sha256(content.encode()).hexdigest())


def _plain(value):
    """value with every string's whitespace (spaces, tabs, line breaks) cut to single spaces: a prompt that only wraps
    in another way is the same prompt. Runs before JSON encoding, which would escape the line breaks and tabs."""
    if isinstance(value, str):
        return ' '.join(value.split())
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def wanted(event, mode):
    """True when the coordinator's report setting asks for this update."""
    return mode == 'all' or mode == 'needs' and event['kind'] in ('asks', 'needs', 'failed')


FEED = Feed()
