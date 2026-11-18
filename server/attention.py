"""Attention mode: one "ask" per pane that waits on you, kept until you answer it, mark it handled or snooze it.

An ask starts when a turn ends with "Needs you: yes", or when the pane shows an approval, a plan or a question
(needs.summary). It clears when you send text to the pane, press Handled, or the next turn ends without
"Needs you: yes". A prompt ask also clears when it leaves the screen. A reply ask follows its conversation
when the conversation returns to a live pane. Opening the tab only marks it opened.
Urgent asks get a three-minute grace period before an unopened ask can send a Teams ping.
The state lives in one JSON file (paths.ATTENTION), written atomically, so asks survive a restart.
"""
from datetime import datetime
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import needs
import paths
import urgent_ping
from notification_priority import status_line

PREFIX = '/mobile/api/attention'
ACTIONS = ('handled', 'snooze', 'opened')
SNOOZE = 24 * 3600
RETENTION = 30 * 86400
TURN_REFRESH_INTERVAL = 86400
TURN_HISTORY_LIMIT = 50
URGENT_GRACE = 180
LIMIT = 160
QUIET = 8  # seconds after you answer a prompt in which the same prompt, still on screen, does not start a new ask
AGENTS = ('claude', 'codex', 'antigravity')
ENDED = ('Idle', 'Stopped')  # the turn is over, so the transcript's last reply is final
PANE = re.compile(r'^%\d{1,6}$')
NEXT = re.compile(r'^(?:[-•>]\s*)?Next:\s*(.+)$', re.I)
URGENT = re.compile(r'^(?:[-•>]\s*)?\**Urgent:\**\s*(.+)$', re.I)


def _clip(text):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= LIMIT else text[:LIMIT - 1].rstrip() + '…'


def reply_ask(reply):
    """('yes' | 'no' | None, the "Next:" text): the reply's last line that starts with "Next:", else its last line."""
    status, lines = status_line(reply)
    plain = [re.sub(r'[*`]', '', line).strip() for line in lines]
    plain = [line for line in plain if line]
    found = next((match[1] for match in map(NEXT.match, reversed(plain)) if match), None)
    return status, _clip(found if found is not None else (plain[-1] if plain else ''))


def urgent_text(reply):
    """The last Urgent: question, without Markdown emphasis, or None."""
    for line in reversed(str(reply or '').splitlines()):
        match = URGENT.match(line.strip().replace('`', ''))
        if match:
            return _clip(re.sub(r'[*`]', '', match[1])) or None
    return None


def _digest(text):
    return hashlib.sha256(str(text or '').encode('utf-8', 'replace')).hexdigest()[:24]


def age_text(seconds):
    if seconds < 3600:
        return f'{max(1, int(seconds // 60))} minutes'
    if seconds < 48 * 3600:
        hours = int(seconds // 3600)
        return '1 hour' if hours == 1 else f'{hours} hours'
    return f'{int(seconds // 86400)} days'


class Attention:
    """Live asks and dismissed prompts belong to panes. Turns read and parked reply asks belong to conversations.

    checked remembers the conversation and live turn-end marker for each pane with a successful reply read.
    """

    def __init__(self, path=None, clock=time.time, on_urgent=urgent_ping.send):
        self.path, self.clock = Path(path or paths.ATTENTION), clock
        self.on_urgent = on_urgent
        self.lock = threading.Lock()
        self.checked, self.quiet = {}, {}
        self._legacy_needs_save = False
        data = self._load()
        self.asks, self.seen, self.digest = data['asks'], data['seen'], data['digest']
        self.turns, self.parked = data['turns'], data['parked']
        self.legacy = data['legacy']

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        asks = {pane: ask for pane, ask in (data.get('asks') or {}).items()
                if isinstance(ask, dict) and PANE.match(str(pane)) and ask.get('kind') in ('reply', 'approval', 'plan', 'question')
                and isinstance(ask.get('since'), (int, float))}
        seen = {pane: value for pane, value in (data.get('seen') or {}).items() if isinstance(value, dict)}
        turns = {thread: value for thread, value in (data.get('turns') or {}).items()
                 if isinstance(value, dict) and isinstance(value.get('turn'), (str, int))
                 and isinstance(value.get('at'), (int, float))}
        for value in turns.values():
            latest = value['turn']
            history = value.get('history')
            if not isinstance(history, list):
                history = []
            older = [turn for turn in history if isinstance(turn, (str, int)) and turn != latest]
            value['history'] = older[-(TURN_HISTORY_LIMIT - 1):] + [latest]
        legacy = {thread: value for thread, value in (data.get('legacy') or {}).items()
                  if thread not in turns and isinstance(value, dict) and isinstance(value.get('reply'), str)
                  and isinstance(value.get('at'), (int, float))}
        now = self.clock()
        for value in (*seen.values(), *asks.values()):
            thread, reply = value.get('thread'), value.pop('reply', None)
            if isinstance(thread, str) and thread and isinstance(reply, str) and reply:
                self._legacy_needs_save = True
                if thread not in turns:
                    legacy.setdefault(thread, {'reply': reply, 'at': now})
        parked = {thread: ask for thread, ask in (data.get('parked') or {}).items()
                  if isinstance(ask, dict) and ask.get('kind') == 'reply' and ask.get('thread') == thread
                  and isinstance(ask.get('since'), (int, float)) and isinstance(ask.get('parked_at'), (int, float))}
        digest = data.get('digest') if isinstance(data.get('digest'), dict) else {}
        hour = digest.get('hour')
        return {'asks': asks, 'seen': seen, 'turns': turns, 'parked': parked, 'legacy': legacy,
                'digest': {'enabled': digest.get('enabled') is True,
                           'hour': hour if isinstance(hour, int) and 0 <= hour <= 23 else 9,
                           'sent': digest.get('sent') if isinstance(digest.get('sent'), str) else None}}

    def _prune(self, now):
        changed = False
        for store, field in ((self.turns, 'at'), (self.parked, 'parked_at'), (self.legacy, 'at')):
            expired = [thread for thread, value in store.items() if value[field] < now - RETENTION]
            for thread in expired:
                del store[thread]
            changed = bool(expired) or changed
        return changed

    def _save(self, now):
        """Write to a temporary file in the same folder, then rename it over the old one."""
        self._prune(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.attention-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'asks': self.asks, 'seen': self.seen, 'digest': self.digest,
                           'turns': self.turns, 'parked': self.parked, 'legacy': self.legacy}, stream)
            os.replace(temp, self.path)
            self._legacy_needs_save = False
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass

    @staticmethod
    def public(ask):
        return dict({key: ask.get(key) for key in ('pane', 'thread', 'since', 'next', 'kind', 'opened', 'snoozed_until')},
                    urgent=ask.get('urgent', False))

    def ask(self, pane, thread=None):
        """The pane's ask for the session list, or None. Snoozed asks are included; snoozed_until says until when."""
        with self.lock:
            ask = self.asks.get(pane)
            return self.public(ask) if ask and (ask.get('thread') or None) == (thread or None) else None

    def waiting(self):
        """Asks that show now (not snoozed), oldest first."""
        now = self.clock()
        with self.lock:
            asks = [self.public(ask) for ask in self.asks.values() if not ask.get('snoozed_until') or ask['snoozed_until'] <= now]
        return sorted(asks, key=lambda ask: ask['since'])

    def snoozed(self):
        now = self.clock()
        with self.lock:
            return sum(1 for ask in self.asks.values() if ask.get('snoozed_until') and ask['snoozed_until'] > now)

    def _start(self, pane, thread, kind, text, now):
        self.asks[pane] = {'pane': pane, 'thread': thread, 'since': now, 'next': _clip(text), 'kind': kind,
                           'opened': False, 'snoozed_until': None}

    def observe(self, entries, status_of, reply_of, finished_of=lambda entry: None):
        """Start and clear asks from the live session list. Read (reply, turn id) from reply_of(entry) once the
        conversation is idle or stopped. Retry empty reads, and after a successful read wait for a new
        conversation or finished_of(entry) marker. The marker must change when the transcript changes, so a
        record that was still being written when it was read is read again once it is complete.
        """
        now = self.clock()
        live = {}
        for entry in entries:
            pane = entry['id']
            if entry.get('agent') in AGENTS and PANE.fullmatch(pane) and not entry.get('ambiguous'):
                live[pane] = None if entry.get('pending_conversation') else (entry.get('thread') or None)
        with self.lock:
            changed = self._legacy_needs_save
            for thread in live.values():
                turn = self.turns.get(thread)
                if turn and turn['at'] < now - TURN_REFRESH_INTERVAL:
                    turn['at'] = now
                    changed = True
            changed = self._prune(now) or changed
            for store in (self.asks, self.seen):
                for pane in list(store):
                    thread = live.get(pane, False)
                    value = store[pane]
                    previous_thread = value.get('thread') or None
                    # The pane went away, or it now runs another conversation.
                    if thread is not False and (thread is None or thread == previous_thread):
                        continue
                    if store is self.asks and value['kind'] == 'reply' and previous_thread:
                        self.parked[previous_thread] = dict(value, parked_at=now)
                    del store[pane]
                    changed = True
            for pane in list(self.checked):
                if pane not in live:
                    del self.checked[pane]
            for ask in self.asks.values():
                if ask.get('snoozed_until') and ask['snoozed_until'] <= now:
                    ask['snoozed_until'] = None  # back, with its first `since`
                    changed = True
        for entry in entries:
            pane = entry['id']
            if pane not in live or live[pane] is None and entry.get('pending_conversation'):
                continue
            thread = entry.get('thread') or None
            status = status_of(entry) or {}
            summary = needs.summary(status)
            with self.lock:
                ask, seen = self.asks.get(pane), self.seen.setdefault(pane, {'thread': thread})
                if not summary and ask and ask['kind'] != 'reply':
                    del self.asks[pane]  # the prompt left the screen: it was answered, here or in the terminal
                    ask = None
                    changed = True
                if not ask and thread in self.parked:
                    ask = self.parked.pop(thread)
                    ask.pop('parked_at')
                    ask['pane'] = pane
                    self.asks[pane] = ask
                    changed = True
                if summary:
                    if ask and ask['kind'] != 'reply' and (ask['next'] != summary['text'] or ask['kind'] != summary['kind']):
                        ask.update(kind=summary['kind'], next=summary['text'])
                        changed = True
                    elif not ask and seen.get('dismissed') != summary['text'] and self.quiet.get(pane, 0) <= now:
                        self._start(pane, thread, summary['kind'], summary['text'], now)
                        changed = True
                    continue
                if seen.pop('dismissed', None) is not None:
                    changed = True
            if status.get('state') not in ENDED:
                continue
            marker = finished_of(entry)
            checked = self.checked.get(pane)
            if checked == (thread, marker):
                continue
            reply, turn_id = reply_of(entry)
            if not reply:
                continue
            first_read = checked is None or checked[0] != thread
            self.checked[pane] = (thread, marker)
            if turn_id is None and (first_read or marker is None):
                continue
            changed = self.note_reply(pane, thread, reply, turn_id, now) or changed
        if changed:
            with self.lock:
                self._save(now)
        self.due_pings(now)
        return changed

    def note_reply(self, pane, thread, reply, turn_id, now=None):
        """Apply a finished turn once per conversation, even if its pane changes."""
        if not reply:
            return False
        now = self.clock() if now is None else now
        with self.lock:
            ask = self.asks.get(pane)
            if thread and turn_id is not None:
                previous = self.turns.get(thread)
                history = previous['history'] if previous else []
                if turn_id in history:
                    return False
                self.turns[thread] = {'turn': turn_id, 'history': (history + [turn_id])[-TURN_HISTORY_LIMIT:], 'at': now}
                legacy = self.legacy.pop(thread, None)
                if previous is None and legacy and legacy['reply'] == _digest(reply):
                    if ask and ask['kind'] == 'reply' and ask.get('thread') == thread:
                        ask['turn'] = turn_id
                    return True
                if ask and ask.get('turn') == turn_id:
                    return True  # An open ask can outlast the stored turn's retention period.
            status, text = reply_ask(reply)
            if status == 'yes':
                urgent = urgent_text(reply)
                is_new_urgent = urgent and (turn_id is None or not ask or not ask.get('urgent') or ask.get('turn') != turn_id)
                if not ask:
                    self._start(pane, thread, 'reply', urgent or text, now)
                    ask = self.asks[pane]
                ask.update(kind='reply', next=urgent or text, urgent=bool(urgent), opened=False, turn=turn_id)
                if is_new_urgent:
                    ask.update(ping_due=now + URGENT_GRACE, pinged=False)
                elif not urgent:
                    ask.pop('ping_due', None)
            elif ask and ask['kind'] == 'reply':
                del self.asks[pane]  # a new turn without "Needs you: yes": you answered it, here or in the terminal
        return True

    def due_pings(self, now=None):
        """Send due urgent pings once, after recording the attempts on disk."""
        now = self.clock() if now is None else now
        notifications = []
        changed = False
        with self.lock:
            for ask in self.asks.values():
                due = ask.get('ping_due')
                if due is None or due > now or ask.get('pinged'):
                    continue
                # Opened (seen in Tether) or snoozed during the grace: you already know, so no ping.
                snoozed = (ask.get('snoozed_until') or 0) > now
                sendable = ask.get('urgent') is True and ask.get('opened') is False and not snoozed
                ask['pinged'] = True
                changed = True
                if sendable:
                    notifications.append(self.public(ask))
            if changed:
                self._save(now)
        for ask in notifications:
            try:
                self.on_urgent(ask)
            except Exception:  # A notification must never stop observation.
                pass

    def clear(self, pane, thread=None, save=True):
        """You answered the pane (any write that submits text to it). Returns True when an ask cleared."""
        now = self.clock()
        with self.lock:
            ask = self.asks.get(pane)
            self.quiet[pane] = now + QUIET
            if not ask or (thread is not None and (ask.get('thread') or None) != (thread or None)):
                return False
            self._forget(pane, ask)
            if save:
                self._save(now)
        return True

    def _forget(self, pane, ask):
        if ask['kind'] != 'reply':
            # The prompt may stay on screen a moment longer (or for good, after Handled): do not raise it again.
            self.seen.setdefault(pane, {'thread': ask.get('thread')})['dismissed'] = ask['next']
        del self.asks[pane]

    def act(self, pane, thread, action):
        """handled, snooze or opened, for the pane's ask in this conversation. Returns the ask after the change, None
        when the pane has no ask, or False when the ask belongs to another conversation (a stale page)."""
        now = self.clock()
        with self.lock:
            ask = self.asks.get(pane)
            if not ask:
                return None
            if (ask.get('thread') or None) != (thread or None):
                return False
            if action == 'handled':
                self._forget(pane, ask)
            elif action == 'snooze':
                ask['snoozed_until'] = now + SNOOZE
            elif action == 'opened':
                if ask.get('opened'):
                    return self.public(ask)
                ask['opened'] = True
            else:
                raise ValueError('Unknown action.')
            self._save(now)
            return self.public(ask)

    def set_digest(self, enabled, hour):
        if not isinstance(enabled, bool) or not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
            raise ValueError('Choose on or off and an hour from 0 to 23.')
        now = self.clock()
        with self.lock:
            self.digest.update(enabled=enabled, hour=hour)
            self._save(now)
            return self.digest_settings()

    def digest_settings(self):
        return {'enabled': self.digest['enabled'], 'hour': self.digest['hour']}

    def digest_due(self, now=None):
        """The daily summary notification, once a day at the chosen hour (local time), when something waits."""
        now = now or datetime.now()
        today = now.strftime('%Y-%m-%d')
        if not self.digest['enabled'] or now.hour < self.digest['hour'] or self.digest.get('sent') == today:
            return None
        epoch = self.clock()
        with self.lock:
            self.digest['sent'] = today
            self._save(epoch)
        asks = self.waiting()
        review_count = 0
        try:
            import lessons
            review = lessons.review(now=now.timestamp())
            if review['due']:
                review_count = review['total'] if review['reason'] == 'time' else review['new'] + review['disputed']
        except Exception:  # A lesson review must not stop the existing digest.
            pass
        if not asks and not review_count:
            return None
        if asks:
            oldest = age_text(max(0, self.clock() - asks[0]['since']))
            count = len(asks)
            body = f'{count} tab{"s" if count != 1 else ""} wait{"" if count != 1 else "s"} on you, oldest {oldest}.'
            if review_count:
                body += f' {review_count} lesson{"s" if review_count != 1 else ""} to review.'
        else:
            body = f'Lessons to review: {review_count}.'
        return {'title': 'Waiting on you', 'body': body, 'tag': 'tether-attention', 'priority': 'important',
                'silent': False, 'url': '/chat/?go=attention'}


STORE = Attention()


def handles(path):
    path = path.split('?', 1)[0]
    return path == PREFIX or path in [f'{PREFIX}/{action}' for action in (*ACTIONS, 'digest')]


def get(handler):
    handler.respond(200, {'asks': STORE.waiting(), 'snoozed': STORE.snoozed(), 'digest': STORE.digest_settings()})


def serve(handler, body, choose, store=None):
    """POST /mobile/api/attention/{handled,snooze,opened}: {session: pane id, thread}. A pane that ended or now runs
    another conversation gets 409. POST /mobile/api/attention/digest: {enabled, hour}."""
    store = store or STORE
    action = handler.path.split('?', 1)[0][len(PREFIX) + 1:]
    if action == 'digest':
        try:
            handler.respond(200, {'digest': store.set_digest(body.get('enabled'), body.get('hour'))})
        except ValueError as exc:
            handler.respond(400, {'error': str(exc)})
        return
    if action not in ACTIONS:
        handler.respond(404, {'error': 'Not found'})
        return
    pane, thread = body.get('session'), body.get('thread')
    if not isinstance(pane, str) or not PANE.match(pane) or not (thread is None or isinstance(thread, str)):
        handler.respond(400, {'error': 'Select a tab first.'})
        return
    try:
        entry = choose(pane)
    except ValueError:
        handler.respond(409, {'error': 'That tab has ended. Reload the list.'})
        return
    if (thread or None) != (entry.get('thread') or None):
        handler.respond(409, {'error': 'This tab changed conversations. Reload it and try again.'})
        return
    result = store.act(pane, thread, action)
    if result is False:
        handler.respond(409, {'error': 'This tab changed conversations. Reload it and try again.'})
        return
    handler.respond(200, {'ask': result if action != 'handled' and result else None, 'waiting': len(store.waiting())})
