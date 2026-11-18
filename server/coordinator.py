"""Coordinator tab: one agent tab per tmux session that routes the user's tasks to the other tabs and reports on them.

A tab becomes the coordinator of its session when its agent runs `agent_coordinator_cli.py claim`. The pane option
@tether_role (coordinator, coordinator-needs or coordinator-off: which updates it gets) marks it. Session discovery
reads that option already (entry['coordinator']) and a resumed pane keeps it, so no other state is kept.
The coordinator reads only the state and the last reply of each tab (tabs, reply), never a transcript. It may send a
message to any agent tab of its own session (send). To start a tab it uses agent_tab_cli.py as every tab does.

Feed.observe runs in the server monitor. When a tab of the session finishes a turn or waits on the user, the feed sends
the coordinator one short status message, so the coordinator can tell the user without polling. It sends only while the
coordinator is idle, joins updates that come close together, and keeps them when the send fails.
"""
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess
import time
import unicodedata

import attention
import auth
import needs
import paths
import push

PATH = '/mobile/api/coordinator'
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
MAX_EVENTS = 20
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
        '(all: finished and waiting tabs; needs: only tabs that wait on the user; off: none) and follow it.\n'
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


def _change_mark(body, caller, entries, run):
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

    mode = _mode(body, mine[1] if mine else 'all')
    if mine is None and (caller.get('role') or caller.get('helper')):
        raise ValueError('A reviewer or advisor tab cannot be the coordinator.')
    other = next((entry for entry in entries if entry['id'] != pane and entry['id'] in found
                  and entry.get('tmux_session') == caller.get('tmux_session')), None)
    if other is not None:
        if body.get('take') is not True:
            raise ValueError(f'The tab `{name_of(other)}` ({other["id"]}) is the coordinator of this session. '
                             'Run release there, or claim --take when the user asked for the change.')
        _set(run, other['id'], None)
    _set(run, pane, mode)
    return {'coordinator': pane, 'events': mode, 'rules': rules()}


def _send_state(target, status):
    if not target.get('writable'):
        raise RuntimeError('Messaging is unavailable for that tab.')
    state = status(target).get('state')
    if state in ('Unavailable', 'Unknown'):
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
    words = {'done': 'finished', 'asks': 'finished and asks the user', 'needs': 'waits on the user'}
    for event in events:
        text = f': {event["text"]}' if event['text'] else ''
        lines.append(f'- `{event["name"]}` ({event["pane"]}) {words[event["kind"]]}{text}')
    return '\n'.join(lines)


def _reply_fingerprint(reply):
    return hashlib.sha256(reply.encode('utf-8', errors='surrogatepass')).hexdigest()


def deliver(pane, queue, events, *, lock, fresh, fresh_status, last_reply, submit_locked):
    """Return sent and held events, [] for a stale queue, or None to retry."""
    if not lock.acquire(timeout=2):
        return None
    try:
        entries = fresh()
        by_pane = {entry['id']: entry for entry in entries}
        found = marks(entries)
        coordinator = by_pane.get(pane)
        if (pane not in found or not _same_chat(coordinator, queue['session'], queue['chat'])
                or not coordinator.get('writable')):
            return []
        mode = found[pane][1]
        live, held = [], []
        for event in events:
            tab = by_pane.get(event['pane'])
            if (not wanted(event, mode) or not _same_chat(tab, event['session'], event['chat'])
                    or tab['id'] in found or tab.get('role') or tab.get('helper')):
                continue
            info = fresh_status(tab)
            state = info.get('state')
            if state not in KNOWN:
                held.append(event)
            elif event['kind'] == 'needs':
                if prompt_identity(info) == event['token']:
                    live.append(event)
            elif state == 'Idle' and _reply_fingerprint(last_reply(tab)) == event['fingerprint']:
                live.append(event)
        if live:
            if fresh_status(coordinator).get('state') != 'Idle':
                return None
            # terminal_submit refuses a draft in the input.
            submit_locked(message(live, mode), coordinator)
        return live, held
    finally:
        lock.release()


class Feed:
    """Sends each coordinator the state changes of the tabs in its session."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.seen = {}      # pane -> {'state', 'since', 'worked', 'sent'}
        self.queues = {}    # coordinator pane -> {'events': [..], 'sent_at', 'retry_at'}

    def _event(self, entry, info, last_reply, now):
        """The update that this tab's state gives now, once per change, else None."""
        state = info.get('state')
        record = self.seen.get(entry['id'])
        if record is None:
            # First sight of this tab: a prompt that is already on screen is in the tabs list, so it is no update.
            prompt = prompt_identity(info)
            record = self.seen[entry['id']] = {'state': state, 'prompt': prompt, 'since': now, 'worked': None,
                                               'sent': ('needs', prompt) if prompt else None}
        prompt = prompt_identity(info)
        if state != record['state'] or prompt != record['prompt']:
            record['state'], record['prompt'], record['since'] = state, prompt, now
        if state == 'Working':
            record['worked'] = record['worked'] if record['worked'] is not None else now
            record['sent'] = None
            return None
        kind, token = None, None
        if prompt:
            kind, token = 'needs', prompt
        elif state == 'Idle' and record['worked'] is not None and record['since'] - record['worked'] >= MINIMUM_WORK:
            kind = 'done'
        if state == 'Idle' and kind is None and record['worked'] is not None and now - record['since'] >= SETTLE:
            record['worked'] = None
        if kind is None or now - record['since'] < SETTLE or record['sent'] == (kind, token):
            return None
        record['sent'] = (kind, token)
        if kind == 'done':
            record['worked'] = None
            reply = last_reply(entry)
            asks, _ = attention.reply_ask(reply) if reply.strip() else (None, '')
            return {'kind': 'asks' if asks == 'yes' else 'done', 'text': push.reply_summary(reply, SUMMARY),
                    'fingerprint': _reply_fingerprint(reply)}
        waits = needs.summary(info)
        return {'kind': 'needs', 'text': f'{waits["kind"]}: {waits["text"]}' if waits else '', 'token': token}

    def observe(self, entries, status, deliver, last_reply):
        now = self.clock()
        found = marks(entries)
        by_pane = {entry['id']: entry for entry in entries}
        for pane in [pane for pane in self.seen if pane not in by_pane]:
            del self.seen[pane]
        for pane in [pane for pane in self.queues if pane not in found or pane not in by_pane]:
            del self.queues[pane]
        coordinators = {}   # session id -> (pane, mode)
        for pane, (_, mode) in found.items():
            coordinators.setdefault(by_pane[pane].get('tmux_session'), (pane, mode))
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
                if pane in queued and info.get('state') == 'Working':
                    for queue in self.queues.values():
                        queue['events'] = [old for old in queue['events'] if old['pane'] != pane]
                if not eligible:
                    continue
                event = self._event(entry, info, last_reply, now)
            except Exception:  # one odd pane must not stop the feed
                continue
            if event is None or not wanted(event, target[1]):
                continue
            coordinator = by_pane[target[0]]
            queue = self.queues.get(target[0])
            if queue is None or queue['session'] != coordinator.get('tmux_session') or queue['chat'] != coordinator.get('transcript'):
                queue = self.queues[target[0]] = {'events': [], 'sent_at': None, 'retry_at': None,
                                                  'session': coordinator.get('tmux_session'), 'chat': coordinator.get('transcript')}
            queue['events'] = [old for old in queue['events'] if old['pane'] != entry['id']]
            queue['events'].append(dict(event, pane=entry['id'], name=name_of(entry), at=now,
                                        session=entry.get('tmux_session'), chat=entry.get('transcript')))
            del queue['events'][:-MAX_EVENTS]
        for pane, queue in list(self.queues.items()):
            self._flush(pane, by_pane[pane], queue, status, deliver, now)

    def _flush(self, pane, coordinator, queue, status, deliver, now):
        """Wait for the send window before checking the live panes."""
        if not _same_chat(coordinator, queue['session'], queue['chat']):
            del self.queues[pane]   # the coordinator moved to another session or started a new chat
            return
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
    return mode == 'all' or mode == 'needs' and event['kind'] != 'done'


FEED = Feed()
