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
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import audit
import needs
import paths
import tether_zone
import urgent_ping
from notification_priority import status_line

PREFIX = '/mobile/api/attention'
ACTIONS = ('handled', 'snooze', 'opened', 'later', 'back')
SNOOZE = 24 * 3600
STALE = 24 * 3600
STALE_RETRY = 600
AUTOMATION_HISTORY = 7 * 86400
RETENTION = 30 * 86400
TURN_REFRESH_INTERVAL = 86400
TURN_HISTORY_LIMIT = 50
URGENT_GRACE = 180
LIMIT = 160
QUIET = 8  # seconds after you answer a prompt in which the same prompt, still on screen, does not start a new ask
AGENTS = ('claude', 'codex', 'antigravity')
ENDED = ('Idle', 'Stopped')  # the turn is over, so the transcript's last reply is final
PANE = re.compile(r'^%\d{1,6}$')
ASK_ID = re.compile(r'[0-9a-f]{12}')
NEXT = re.compile(r'^(?:[-•>]\s*)?Next:\s*(.+)$', re.I)
URGENT = re.compile(r'^(?:[-•>]\s*)?\**Urgent:\**\s*(.+)$', re.I)


class AttentionError(Exception):
    pass


def _local_midnight(now, tz=None):
    """The first instant of today in the Tether zone."""
    if tz is None:
        tz = tether_zone.zone()
    return tether_zone.day_start(datetime.fromtimestamp(now, tz).date(), tz=tz)


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
    latest ties the transcript's latest reply occurrence to that marker.
    """

    def __init__(self, path=None, clock=time.time, on_urgent=urgent_ping.send, log=audit):
        self.path, self.clock = Path(path or paths.ATTENTION), clock
        self.on_urgent, self.log = on_urgent, log
        self.lock = threading.Lock()
        self._failed_moves, self._failed_undos = {}, {}
        self.checked, self.latest, self.quiet = {}, {}, {}
        self._legacy_needs_save = False
        data = self._load()
        self.asks, self.seen, self.digest = data['asks'], data['seen'], data['digest']
        self.turns, self.parked = data['turns'], data['parked']
        self.legacy, self.rules = data['legacy'], data['rules']
        self.undos = data['undos']

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
        for ask in (*asks.values(), *parked.values()):
            ask_id = ask.get('id')
            if not isinstance(ask_id, str) or not ASK_ID.fullmatch(ask_id):
                ask.update(id=secrets.token_hex(6), asked_at=ask['since'])
                self._legacy_needs_save = True
            elif type(ask.get('asked_at')) not in (int, float):
                ask['asked_at'] = ask['since']
                self._legacy_needs_save = True
            later = ask.get('later')
            if not isinstance(ask.get('moved'), list):
                # moved lists the saved rule moves of this occurrence. A rule-Later ask records at least its own.
                ask['moved'] = [later['entry']] if isinstance(later, dict) and later.get('by') == 'rule' and later.get('entry') else []
                self._legacy_needs_save = True
        rules = data.get('rules')
        stale = rules.get('stale') if isinstance(rules, dict) else None
        digest = data.get('digest') if isinstance(data.get('digest'), dict) else {}
        hour = digest.get('hour')
        saved_undos = data.get('undos')
        if not isinstance(saved_undos, dict):
            saved_undos = {}
        undos = {entry_id: item for entry_id, item in saved_undos.items()
                 if isinstance(entry_id, str) and isinstance(item, dict)
                 and type(item.get('at')) in (int, float) and -float('inf') < item['at'] < float('inf')
                 and all(isinstance(item.get(key), str) for key in ('pane', 'thread', 'ask'))
                 and isinstance(item.get('actor'), dict)}
        return {'asks': asks, 'seen': seen, 'turns': turns, 'parked': parked, 'legacy': legacy, 'undos': undos,
                'rules': {'stale': stale if isinstance(stale, bool) else True},
                'digest': {'enabled': digest.get('enabled') is True,
                           'hour': hour if isinstance(hour, int) and 0 <= hour <= 23 else 9,
                           'sent': digest.get('sent') if isinstance(digest.get('sent'), str) else None}}

    def _prune(self, now):
        changed = False
        for store, field in ((self.turns, 'at'), (self.parked, 'parked_at'), (self.legacy, 'at')):
            expired = [thread for thread, value in store.items()
                       if value[field] < now - RETENTION and not value.get('later')]
            for thread in expired:
                del store[thread]
            changed = bool(expired) or changed
        expired_undos = [entry_id for entry_id, item in self.undos.items() if item['at'] < now - AUTOMATION_HISTORY]
        for entry_id in expired_undos:
            del self.undos[entry_id]
            self._failed_undos.pop(entry_id, None)
        return changed or bool(expired_undos)

    def _save(self, now):
        """Write atomically and report whether the state reached disk."""
        self._prune(now)
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.attention-', dir=self.path.parent)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'asks': self.asks, 'seen': self.seen, 'digest': self.digest,
                           'turns': self.turns, 'parked': self.parked, 'legacy': self.legacy,
                           'rules': self.rules, 'undos': self.undos}, stream)
            os.replace(temp, self.path)
            self._legacy_needs_save = False
        except OSError:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
            return False
        return True

    def _save_user(self, now):
        """Save a user write under the state lock and roll back pruning if it fails."""
        before = self.turns.copy(), self.parked.copy(), self.legacy.copy()
        before_undos = self.undos.copy(), self._failed_undos.copy()
        if not self._save(now):
            self.turns, self.parked, self.legacy = before
            self.undos, self._failed_undos = before_undos
            raise AttentionError('Could not save. Try again.')

    @staticmethod
    def public(ask):
        later = ask.get('later')
        return dict({key: ask.get(key) for key in ('id', 'asked_at', 'pane', 'thread', 'since', 'next', 'kind',
                                                  'opened', 'snoozed_until')},
                    urgent=ask.get('urgent', False),
                    later={key: later.get(key) for key in ('at', 'by', 'entry')} if later else None)

    def ask(self, pane, thread=None):
        """The pane's ask for the session list, or None. Snoozed asks are included; snoozed_until says until when."""
        with self.lock:
            ask = self.asks.get(pane)
            return self.public(ask) if ask and (ask.get('thread') or None) == (thread or None) else None

    def reply_identity(self, pane, thread):
        """The open reply ask and its transcript turn, read together."""
        with self.lock:
            ask = self.asks.get(pane)
            if ask and ask['kind'] == 'reply' and ask.get('thread') == thread and ask.get('turn') is not None:
                return self.public(ask), ask['turn']
        return None, None

    def waiting(self):
        """Asks in the queue, oldest first."""
        now = self.clock()
        with self.lock:
            asks = [self.public(ask) for ask in self.asks.values()
                    if not ask.get('later') and (not ask.get('snoozed_until') or ask['snoozed_until'] <= now)]
        return sorted(asks, key=lambda ask: ask['since'])

    def later_asks(self):
        with self.lock:
            asks = [self.public(ask) for ask in self.asks.values() if ask.get('later')]
        return sorted(asks, key=lambda ask: ask['later']['at'], reverse=True)

    def snoozed(self):
        now = self.clock()
        with self.lock:
            return sum(1 for ask in self.asks.values() if ask.get('snoozed_until') and ask['snoozed_until'] > now)

    def _start(self, pane, thread, kind, text, now):
        self.asks[pane] = {'id': secrets.token_hex(6), 'asked_at': now, 'pane': pane, 'thread': thread,
                           'since': now, 'next': _clip(text), 'kind': kind, 'opened': False, 'snoozed_until': None,
                           'later': None, 'stay': False, 'back_at': None}

    @staticmethod
    def _end_snooze(ask, now):
        if ask.get('snoozed_until') and ask['snoozed_until'] <= now:
            ask.update(snoozed_until=None, back_at=now)
            return True
        return False

    def observe(self, entries, status_of, reply_of, finished_of=lambda entry: None, version_of=lambda entry: None):
        """Start and clear asks from the live session list. Read (reply, turn id) from reply_of(entry) once the
        conversation is idle or stopped. Retry empty reads. After a successful read, read again only when the
        conversation, the live turn end finished_of(entry) or the transcript version version_of(entry) changes, so a
        record that was still being written is read again once it is complete. A reply without a turn id can start
        an ask only after the live turn end changed while this process runs; a file change alone is not enough.
        """
        now = self.clock()
        live = {}
        moved = False
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
            if self.undos:
                self._flush_undos(now)
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
            for pane in self.latest.keys() - live.keys():
                del self.latest[pane]
                self.checked.pop(pane, None)
            for ask in self.asks.values():
                changed = self._end_snooze(ask, now) or changed
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
                    self._end_snooze(ask, now)
                    self.asks[pane] = ask
                    changed = True
                if summary:
                    replaces_reply = ask and ask['kind'] == 'reply' and ask.get('later')
                    if ask and ask['kind'] != 'reply' and (ask['next'] != summary['text'] or ask['kind'] != summary['kind']):
                        ask.update(kind=summary['kind'], next=summary['text'])
                        changed = True
                    elif replaces_reply or (not ask and seen.get('dismissed') != summary['text'] and self.quiet.get(pane, 0) <= now):
                        self._start(pane, thread, summary['kind'], summary['text'], now)
                        changed = True
                    continue
                if seen.pop('dismissed', None) is not None:
                    changed = True
            if status.get('state') not in ENDED:
                continue
            finished, version = finished_of(entry), version_of(entry)
            marker = (thread, finished, version)
            checked = self.checked.get(pane)
            if checked != marker:
                reply, turn_id = reply_of(entry)
                occurrence = (turn_id if turn_id is not None else finished) if reply else None
                if reply:
                    live_turn_ended = checked and checked[0] == thread and finished is not None and finished != checked[1]
                    if turn_id is not None or live_turn_ended:
                        changed = self.note_reply(pane, thread, reply, turn_id, now, finished=finished) or changed
                with self.lock:
                    self.latest[pane] = (marker, occurrence)
                    if reply:
                        self.checked[pane] = marker
            moved = self._stale(pane, entry, marker, now) or moved
        if changed:
            with self.lock:
                self._save(now)
        self.due_pings(now)
        return changed or moved

    def _stale_candidate(self, pane, marker, now):
        """Return the eligible ask with the state lock held."""
        thread = marker[0]
        ask = self.asks.get(pane)
        if (not self.rules['stale'] or not ask or ask['kind'] != 'reply' or (ask.get('thread') or None) != thread
                or ask.get('later') or ask.get('stay') or ask.get('urgent') or (ask.get('snoozed_until') or 0) > now
                or self.checked.get(pane) != marker):
            return None
        turn = ask.get('turn')
        occurrence = ask.get('occ', turn)
        if occurrence is None or self.latest.get(pane) != (marker, occurrence):
            return None
        if turn is not None and self.turns.get(thread, {}).get('turn') != turn:
            return None
        if now - max(ask['asked_at'], ask.get('back_at') or 0) >= STALE:
            return ask
        return None

    def _stale(self, pane, entry, marker, now):
        with self.lock:
            ask = self._stale_candidate(pane, marker, now)
            if not ask:
                return False
            ask_id, asked_at = ask['id'], ask['asked_at']
            retry_at = self._failed_moves.get(ask_id)
            if retry_at is not None and now < retry_at:
                return False
            # Audit never takes the Attention lock. Keep the check, audit and state save together.
            since = now - AUTOMATION_HISTORY
            if ask.get('back_at') is not None:
                # A move from before this snooze ended belongs to a previous time in Later.
                since = max(since, ask['back_at'])
            try:
                page = self._query_log(text=ask_id, kinds=['auto.stale_ask'], since=since, limit=200)
                move = next((row for row in page['entries'] if row.get('detail', {}).get('ask') == ask_id), None)
                if move is None:
                    day = tether_zone.now(asked_at)
                    name = entry.get('pane_name') or entry.get('label') or entry.get('tmux_session') or pane
                    move = self.log.record('auto.stale_ask', f'Moved an ask from {day.day} {day:%b} to Later',
                                           actor={'source': 'server'}, pane=pane, thread=marker[0] or '', ask=ask_id,
                                           tab=name, rule='Stale ask')
            except OSError:
                move = None
            if move is not None:
                before = ask.copy()
                ask.update(later={'at': now, 'by': 'rule', 'entry': move['id']}, moved=[*ask.get('moved', []), move['id']][-20:])
                if self._save(now):
                    return True
                self.asks[pane] = before
            if retry_at is None:
                operation = 'log' if move is None else 'save'
                print(f'Attention: could not {operation} the move to Later for ask {ask_id}.', file=sys.stderr)
            self._failed_moves[ask_id] = now + STALE_RETRY
            return False

    def note_reply(self, pane, thread, reply, turn_id, now=None, *, finished=None):
        """Apply a finished turn once per conversation, even if its pane changes."""
        if not reply:
            return False
        now = self.clock() if now is None else now
        occurrence = turn_id if turn_id is not None else finished
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
                        ask.update(turn=turn_id, occ=occurrence)
                    return True
                if ask and ask.get('turn') == turn_id:
                    return True  # An open ask can outlast the stored turn's retention period.
            status, text = reply_ask(reply)
            if status == 'yes':
                urgent = urgent_text(reply)
                new_occurrence = not ask or ask.get('occ', ask.get('turn')) != occurrence
                is_new_urgent = urgent and (new_occurrence or not ask.get('urgent'))
                if not ask:
                    self._start(pane, thread, 'reply', urgent or text, now)
                    ask = self.asks[pane]
                elif new_occurrence:
                    ask.update(id=secrets.token_hex(6), asked_at=now, later=None, stay=False, back_at=None)
                    ask.pop('moved', None)
                ask.update(kind='reply', next=urgent or text, urgent=bool(urgent), opened=False, turn=turn_id, occ=occurrence)
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
                # Opened, snoozed or moved to Later during the grace: you already know, so no ping.
                snoozed = (ask.get('snoozed_until') or 0) > now
                sendable = ask.get('urgent') is True and ask.get('opened') is False and not snoozed and not ask.get('later')
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

    def act(self, pane, thread, action, ask_id=None, entry_id=None):
        """Return the updated ask, False for a stale target, or None if there is no ask and no id was given."""
        now = self.clock()
        with self.lock:
            ask = self.asks.get(pane)
            if not ask:
                return False if ask_id is not None else None
            if (ask.get('thread') or None) != (thread or None) or ask_id is not None and ask_id != ask['id']:
                return False
            before = ask.copy()
            seen = self.seen.get(pane)
            before_seen = seen.copy() if seen is not None else None
            later = ask.get('later')
            if action == 'handled':
                self._forget(pane, ask)
            elif action == 'snooze':
                ask.update(snoozed_until=now + SNOOZE, later=None)
            elif action == 'opened':
                if ask.get('opened'):
                    return self.public(ask)
                ask['opened'] = True
            elif action == 'later':
                ask.update(later={'at': now, 'by': 'you', 'entry': None}, snoozed_until=None)
            elif action == 'back':
                if not later:
                    return False if entry_id is not None else self.public(ask)
                rule = later['by'] == 'rule'
                if rule and (entry_id is None or entry_id != later.get('entry')):
                    return False
                if not rule and entry_id is not None:
                    return False  # A log row cannot put a manual move back in the queue.
                ask.update(stay=True, later=None)
                if rule:
                    previous_undo = self.undos.get(entry_id)
                    capture_actor = getattr(self.log, 'capture_actor', audit.capture_actor)
                    self.undos[entry_id] = {'at': now, 'pane': pane, 'thread': thread or '', 'ask': ask['id'],
                                            'actor': capture_actor()}
                try:
                    self._save_user(now)
                except AttentionError:
                    self.asks[pane] = before
                    if rule:
                        if previous_undo is None:
                            del self.undos[entry_id]
                        else:
                            self.undos[entry_id] = previous_undo
                    raise
                if rule:
                    self._flush_undos(now, only=entry_id)
                return self.public(ask)
            else:
                raise ValueError('Unknown action.')
            try:
                self._save_user(now)
            except AttentionError:
                self.asks[pane] = before
                if before_seen is None:
                    self.seen.pop(pane, None)
                else:
                    self.seen[pane] = before_seen
                raise
            return self.public(ask)

    def set_rules(self, stale):
        if not isinstance(stale, bool):
            raise ValueError('Choose on or off for the stale ask rule.')
        with self.lock:
            old = self.rules['stale']
            if old != stale:
                self.rules['stale'] = stale
                try:
                    self._save_user(self.clock())
                except AttentionError:
                    self.rules['stale'] = old
                    raise
                self.log.record('setting.automation', 'Changed the stale ask rule', old=old, new=stale)
        return {'stale': stale}

    def _query_log(self, **filters):
        page = self.log.query(**filters)
        if 'error' in page:
            raise OSError(page['error'])
        return page

    def _flush_undos(self, now, only=None):
        """Copy saved Undos to the audit log with the state lock held."""
        due = {entry_id: item for entry_id, item in self.undos.items()
               if entry_id == only or (only is None and self._failed_undos.get(entry_id, now) <= now)}
        if not due:
            return
        # Match by entry id only: a clock correction can date the line before its outbox item or its move.
        filters = {'kinds': ['auto.undo'], 'since': min(now, *(item['at'] for item in due.values())) - AUTOMATION_HISTORY,
                   'limit': 100}
        logged = set()
        can_append = False
        try:
            while True:
                page = self._query_log(**filters)
                for row in page['entries']:
                    entry_id = row.get('detail', {}).get('entry')
                    if entry_id in due:
                        logged.add(entry_id)
                if not page['next'] or len(logged) == len(due):
                    break
                filters['before'] = page['next']
            can_append = True
        except OSError:
            # Wait until the log can confirm which Undo lines already exist.
            pass

        dropped = False
        for entry_id, item in due.items():
            written = entry_id in logged
            if not written and can_append:
                try:
                    written = self.log.record('auto.undo', 'Put an ask back in the queue', actor=item['actor'],
                                              pane=item['pane'], thread=item['thread'], ask=item['ask'],
                                              entry=entry_id, rule='Stale ask') is not None
                except OSError:
                    pass
            if written:
                del self.undos[entry_id]
                self._failed_undos.pop(entry_id, None)
                dropped = True
            else:
                if entry_id not in self._failed_undos:
                    print(f'Attention: could not log Undo for move {entry_id}.', file=sys.stderr)
                self._failed_undos[entry_id] = now + STALE_RETRY
        if dropped:
            self._save(now)

    @staticmethod
    def _move_failed(row, asks):
        ask = asks.get(row.get('detail', {}).get('ask'))
        return ask is not None and row['id'] not in ask.get('moved', [])

    def _automation_today(self, midnight, page=None):
        """Count moves and collect their state under the same lock as the audit reads."""
        # Undo lines come from the whole log window, not from today only: a clock correction can date an Undo
        # before midnight while its move counts today. Home and the log then read the same lines.
        since = self.clock() - AUTOMATION_HISTORY
        if page is None:
            page = self._query_log(kinds=['automation'], since=since, limit=100)
        asks = {ask['id']: ask for ask in (*self.parked.values(), *self.asks.values())}
        moves, undone = set(), set(self.undos)
        while True:
            entries = page['entries']
            for row in entries:
                if row['kind'] == 'auto.undo':
                    undone.add(row.get('detail', {}).get('entry'))
                elif row['kind'] == 'auto.stale_ask' and row['at'] >= midnight and not self._move_failed(row, asks):
                    moves.add(row['id'])
            before = page['next']
            if not before:
                break
            page = self._query_log(kinds=['automation'], since=since, limit=100, before=before)
        # Active rule moves in state take precedence over Undo records.
        undone.difference_update(ask['later']['entry'] for ask in asks.values()
                                 if (ask.get('later') or {}).get('by') == 'rule')
        return len(moves - undone), undone, asks

    def handled_today(self):
        with self.lock:
            return self._automation_today(_local_midnight(self.clock()))[0]

    def automation_log(self):
        # Read the audit log and the Undo outbox under the same lock.
        with self.lock:
            now = self.clock()
            zone = tether_zone.name()
            today_start = _local_midnight(now, ZoneInfo(zone))
            page = self._query_log(kinds=['automation'], since=now - AUTOMATION_HISTORY, limit=100)
            handled_today, undone, asks = self._automation_today(today_start, page)
            entries = [self._move_row(row, undone, asks) if row['kind'] == 'auto.stale_ask' else row
                       for row in page['entries']]
        return {'entries': entries, 'handled_today': handled_today, 'today_start': today_start, 'zone': zone}

    def _move_row(self, row, undone, asks):
        if self._move_failed(row, asks):
            return dict(row, undone=False, undo=None, failed=True)
        ask = asks.get(row.get('detail', {}).get('ask'), {})
        pane, later = ask.get('pane'), ask.get('later') or {}
        target = None
        # The action route needs a live pane with a matching conversation.
        if later.get('by') == 'rule' and later.get('entry') == row['id'] and self.asks.get(pane) is ask:
            target = {'session': pane, 'thread': ask.get('thread'), 'ask': ask['id'], 'entry': row['id']}
        return dict(row, undone=row['id'] in undone, undo=target)

    def set_digest(self, enabled, hour):
        if not isinstance(enabled, bool) or not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
            raise ValueError('Choose on or off and an hour from 0 to 23.')
        now = self.clock()
        with self.lock:
            before = self.digest.copy()
            self.digest.update(enabled=enabled, hour=hour)
            try:
                self._save_user(now)
            except AttentionError:
                self.digest = before
                raise
            return self.digest_settings()

    def digest_settings(self):
        return {'enabled': self.digest['enabled'], 'hour': self.digest['hour']}

    def digest_due(self, now=None):
        """The daily summary notification, once a day at the chosen hour (Tether time), when something waits."""
        now = now or tether_zone.now()
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
    return path == PREFIX or path in [f'{PREFIX}/{action}' for action in (*ACTIONS, 'digest', 'rules', 'log')]


def get(handler):
    path = handler.path.split('?', 1)[0]
    try:
        if path == PREFIX:
            payload = {'asks': STORE.waiting(), 'snoozed': STORE.snoozed(), 'digest': STORE.digest_settings(),
                       'later': STORE.later_asks(), 'rules': dict(STORE.rules)}
            try:
                payload['handled_today'] = STORE.handled_today()
            except OSError:
                payload.update(handled_today=None, log_error=True)
        elif path == f'{PREFIX}/log':
            payload = STORE.automation_log()
        else:
            handler.respond(404, {'error': 'Not found'})
            return
    except OSError:
        handler.respond(500, {'error': 'Tether could not read the audit log.'})
    else:
        handler.respond(200, payload)


def serve(handler, body, choose, store=None):
    """Dispatch attention actions and settings. Ask actions bind to the pane, thread and optional occurrence id."""
    store = store or STORE
    action = handler.path.split('?', 1)[0][len(PREFIX) + 1:]
    if action in ('digest', 'rules'):
        try:
            if action == 'digest':
                result = store.set_digest(body.get('enabled'), body.get('hour'))
            else:
                result = store.set_rules(body.get('stale'))
        except ValueError as exc:
            handler.respond(400, {'error': str(exc)})
        except AttentionError as exc:
            handler.respond(503, {'error': str(exc)})
        else:
            handler.respond(200, {action: result})
        return
    if action not in ACTIONS:
        handler.respond(404, {'error': 'Not found'})
        return
    pane, thread, ask_id, entry_id = body.get('session'), body.get('thread'), body.get('ask'), body.get('entry')
    if not isinstance(pane, str) or not PANE.match(pane) or not (thread is None or isinstance(thread, str)):
        handler.respond(400, {'error': 'Select a tab first.'})
        return
    if ((ask_id is not None and not isinstance(ask_id, str))
            or (entry_id is not None and not isinstance(entry_id, str))):
        handler.respond(400, {'error': 'Use an ask id.'})
        return
    try:
        entry = choose(pane)
    except ValueError:
        handler.respond(409, {'error': 'That tab has ended. Reload the list.'})
        return
    if (thread or None) != (entry.get('thread') or None):
        handler.respond(409, {'error': 'This tab changed conversations. Reload it and try again.'})
        return
    try:
        result = store.act(pane, thread, action, ask_id=ask_id, entry_id=entry_id)
    except AttentionError as exc:
        handler.respond(503, {'error': str(exc)})
        return
    if result is False:
        message = ('This ask changed. Reload and try again.' if ask_id is not None or action == 'back'
                   else 'This tab changed conversations. Reload it and try again.')
        handler.respond(409, {'error': message})
        return
    handler.respond(200, {'ask': result if action != 'handled' and result else None, 'waiting': len(store.waiting())})
