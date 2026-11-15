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
import hmac
from pathlib import Path
import re
import subprocess
import threading
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
SEND_LOCK = threading.Lock()


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
        out['finished_seconds_ago'] = max(0, int(time.monotonic() - finished))
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


def act(body, caller, entries, *, run, status, send, last_reply, timeline):
    """One coordinator command from the tab `caller`. Returns (HTTP status, reply)."""
    action = body.get('action')
    if caller.get('agent') not in AGENTS:
        raise ValueError('Only a Claude or Codex tab can be the coordinator.')
    found = marks(entries)
    mine = found.get(caller['id'])
    if action == 'claim':
        mode = _mode(body, mine[1] if mine else 'all')
        if mine is None and (caller.get('role') or caller.get('helper')):
            raise ValueError('A reviewer or advisor tab cannot be the coordinator.')
        live = {entry['id']: entry for entry in entries}
        other = next((pane for pane, (session, _) in found.items()
                      if pane != caller['id'] and session == caller.get('tmux_session')), None)
        if other is not None and body.get('take') is not True:
            raise ValueError(f'The tab `{name_of(live[other])}` ({other}) is the coordinator of this session. '
                             'Run release there, or claim --take when the user asked for the change.')
        if other is not None:
            _set(run, other, None)
        _set(run, caller['id'], mode)
        return 200, {'coordinator': caller['id'], 'events': mode, 'rules': rules(),
                     'tabs': [row(entry, status, last_reply, timeline(entry['id']).get('finished'))
                              for entry in _peers(caller, entries)]}
    if mine is None:
        raise ValueError('This tab is not the coordinator. Run claim first, when the user asked this tab to coordinate.')
    if action == 'release':
        _set(run, caller['id'], None)
        return 200, {'released': caller['id']}
    if action == 'events':
        mode = _mode(body, None)
        _set(run, caller['id'], mode)
        return 200, {'events': mode}
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
        if not target.get('writable'):
            raise RuntimeError('Messaging is unavailable for that tab.')
        state = status(target).get('state')
        if state in ('Unavailable', 'Unknown'):
            raise RuntimeError('Messaging is unavailable for that tab.')
        if state in ('Approval', 'Input needed'):
            raise RuntimeError('That tab waits on a prompt. Ask the user to answer it in that tab.')
        with SEND_LOCK:
            delivery = send(target, message, state)
        return 200, {'sent': True, 'pane': target['id'], 'name': name_of(target), 'delivery': delivery}
    raise ValueError('Unknown action.')


def serve(handler, body, *, sessions, run, status, send, last_reply, timeline):
    credential = handler.headers.get('X-Tether-Hook', '')
    if (not auth.is_loopback(handler.client_address[0]) or not credential
            or not hmac.compare_digest(credential.encode(), auth.session_value().encode())):
        handler.respond(403, {'error': 'Local hook authentication required.'})
        return
    entries = sessions()
    caller = next((entry for entry in entries if entry['id'] == body.get('session')), None)
    if caller is None:
        raise ValueError('That session has ended. Select another tab.')
    handler.respond(*act(body, caller, entries, run=run, status=status, send=send, last_reply=last_reply,
                         timeline=timeline))


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
            waiting = {'Approval': 'approval', 'Input needed': 'attention'}.get(state)
            record = self.seen[entry['id']] = {
                'state': state, 'since': now, 'worked': None,
                'sent': ('needs', (info.get(waiting) or {}).get('id')) if waiting else None}
        if state != record['state']:
            record['state'], record['since'] = state, now
        if state == 'Working':
            record['worked'] = record['worked'] if record['worked'] is not None else now
            record['sent'] = None
            return None
        kind, token = None, None
        if state == 'Approval':
            kind, token = 'needs', (info.get('approval') or {}).get('id')
        elif state == 'Input needed':
            kind, token = 'needs', (info.get('attention') or {}).get('id')
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
            return {'kind': 'asks' if asks == 'yes' else 'done', 'text': push.reply_summary(reply, SUMMARY)}
        waits = needs.summary(info)
        return {'kind': 'needs', 'text': f'{waits["kind"]}: {waits["text"]}' if waits else ''}

    def observe(self, entries, status, send, last_reply):
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
        statuses = {}
        for entry in entries:
            target = coordinators.get(entry.get('tmux_session'))
            if (target is None or entry['id'] in found or entry.get('agent') not in AGENTS
                    or not entry.get('owner_pid') or not entry.get('writable')
                    or entry.get('role') or entry.get('helper')):
                continue    # reviewers and advisors report to the tab they help
            try:
                info = statuses[entry['id']] = status(entry)
                event = self._event(entry, info, last_reply, now)
            except Exception:  # one odd pane must not stop the feed
                continue
            if event is None or target[1] == 'off' or (target[1] == 'needs' and event['kind'] == 'done'):
                continue
            queue = self.queues.setdefault(target[0], {'events': [], 'sent_at': None, 'retry_at': None})
            queue['events'] = [old for old in queue['events'] if old['pane'] != entry['id']]
            queue['events'].append(dict(event, pane=entry['id'], name=name_of(entry), at=now))
            del queue['events'][:-MAX_EVENTS]
        for pane, queue in self.queues.items():
            self._flush(by_pane[pane], found[pane][1], queue, by_pane, statuses, status, send, now)

    def _flush(self, coordinator, mode, queue, by_pane, statuses, status, send, now):
        events = queue['events']
        if not events or now - events[-1]['at'] < JOIN and now - events[0]['at'] < 4 * JOIN:
            return
        if queue['sent_at'] is not None and now - queue['sent_at'] < GAP:
            return
        if queue['retry_at'] is not None and now < queue['retry_at']:
            return
        # A tab that no longer waits on the user, or that works again, has nothing to report.
        live = []
        for event in events:
            state = (statuses.get(event['pane']) or {}).get('state')
            if event['pane'] in by_pane and (state in ('Approval', 'Input needed') if event['kind'] == 'needs'
                                             else state == 'Idle'):
                live.append(event)
        queue['events'] = events = live
        if not events:
            return
        try:
            state = status(coordinator).get('state')
            if state != 'Idle':
                return
            send(coordinator, message(events, mode), state)
        except Exception:  # a draft in the input, a prompt on screen, a closed pane: try again later
            queue['retry_at'] = now + RETRY
            return
        queue.update(events=[], sent_at=now, retry_at=None)


FEED = Feed()
