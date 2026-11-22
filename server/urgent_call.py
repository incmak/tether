"""Plan urgent calls and record their outcomes without a calling service."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

import paths
import tether_zone

WINDOW = 30 * 60
WINDOW_MAX, DAY_MAX, ASK_MAX = 2, 8, 3
SLOTS = (0, 10 * 60, 40 * 60)
LATE = 10 * 60  # a slot found later than this (downtime, a parked tab) is skipped, not rung
HOLD = 2 * 3600
DND_MAX = 24 * 3600
CALL_MAX = 15 * 60 + 60
ASK_KEEP = 1000  # ask records kept, newest first; a returning ask keeps its decided slots
DEFAULT_SETTINGS = {'mode': 'on', 'day_start': '08:00', 'day_end': '22:00'}
OUTCOMES = ('answered', 'declined', 'unanswered')
# dnd_from..dnd_until and hold_from..hold_until are the latest blocks; released_at is the last "calls back on".
TIMES = ('hold_until', 'dnd_until', 'hold_from', 'dnd_from', 'released_at')


class CallRefused(Exception):
    pass


def _number(value):
    return type(value) in (int, float) and -float('inf') < value < float('inf')


def _valid(data):
    if (type(data['version']) is not int or data['version'] != 1
            or not isinstance(data['calls'], list) or not isinstance(data['asks'], dict)
            or not all(_number(data[key]) for key in TIMES)):
        return False
    ids, live = set(), None
    for call in data['calls']:
        call_id, agenda, panes, outcome = (call[k] for k in ('id', 'asks', 'panes', 'outcome'))
        if (not isinstance(call_id, str) or not call_id or call_id in ids or not _number(call['at'])
                or outcome not in (*OUTCOMES, None, 'lost', 'refused')
                or call['counted'] is not (outcome != 'refused')
                or not isinstance(agenda, list) or not agenda or not all(isinstance(a, str) for a in agenda)
                or not isinstance(panes, dict) or len(agenda) != len(panes) or set(agenda) != panes.keys()
                or not all(isinstance(pane, str) for pane in panes.values())):
            return False
        ids.add(call_id)
        if outcome is None:
            if live is not None or not panes.keys() <= data['asks'].keys():
                return False
            live = call_id
    return live == data['live'] and all(
        isinstance(ask['decided'], list)
        and all(type(i) is int and 0 <= i < len(SLOTS) for i in ask['decided'])
        and len(ask['decided']) == len(set(ask['decided']))
        and type(ask['calls']) is int and 0 <= ask['calls'] <= ASK_MAX
        and type(ask['declines']) is int and 0 <= ask['declines'] <= 2
        and ask['hold_slot'] in (None, 'pending', 'used')
        and isinstance(ask['done'], bool) and _number(ask['seen_at'])
        for ask in data['asks'].values())


class Planner:
    def __init__(self, path=None, clock=time.time, zone=tether_zone.zone, settings=lambda: DEFAULT_SETTINGS,
                 place=None, mark_opened=None, log=None):
        self.path = Path(paths.URGENT_CALLS if path is None else path)
        self.clock, self.zone, self.settings = clock, zone, settings
        self.place, self.mark_opened = place, mark_opened
        self.log = log or (lambda event, **fields: None)
        self.lock = threading.Lock()
        self.broken = self._reported_broken = self._dirty = False
        self.state = dict(version=1, calls=[], asks={}, live=None, **dict.fromkeys(TIMES, 0))
        try:
            data = json.loads(self.path.read_text())
            if not _valid(data):
                raise ValueError('Invalid urgent call state')
            self.state = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            self.broken = True

    def _save(self):
        if self.broken:
            raise OSError('Urgent call state is unreadable')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.urgent-calls-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.state, stream)
            os.replace(temp, self.path)
        finally:
            Path(temp).unlink(missing_ok=True)
        self._dirty = False

    def _commit(self, before):
        try:
            self._save()
        except OSError:
            self.state = before
            raise

    def _call(self, call_id):
        return next((call for call in self.state['calls'] if call['id'] == call_id), None)

    def _today(self, local):
        start = tether_zone.day_start(local.date(), tz=local.tzinfo)
        return sum(call['counted'] and call['at'] >= start for call in self.state['calls'])

    def _blocked(self, now):
        settings = self.settings()
        settings = settings if isinstance(settings, dict) else {}
        mode = settings.get('mode', DEFAULT_SETTINGS['mode'])
        if mode == 'off':
            return 'off'
        local = datetime.fromtimestamp(now, self.zone())
        if mode == 'daytime':
            hours = []
            for key in ('day_start', 'day_end'):
                value = settings.get(key, DEFAULT_SETTINGS[key])
                hours.append(value if isinstance(value, str) and re.fullmatch(
                    r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value) else DEFAULT_SETTINGS[key])
            start, end = hours
            at = local.strftime('%H:%M')
            daytime = start <= at < end if start < end else at >= start or at < end
            if not daytime:
                return 'night'
        for rule in ('dnd', 'hold'):
            if now < self.state[rule + '_until']:
                return rule
        if sum(call['counted'] and call['at'] > now - WINDOW for call in self.state['calls']) >= WINDOW_MAX:
            return 'window'
        if self._today(local) >= DAY_MAX:
            return 'day'
        return None

    def blocked(self, now):
        with self.lock:
            return self._blocked(now)

    def _eligible(self, candidates, now, decide):
        for candidate in candidates:
            ask = self.state['asks'].setdefault(candidate['id'], dict(
                decided=[], calls=0, declines=0, hold_slot=None, done=False))
            ask['seen_at'] = now
            due = [i for i, offset in enumerate(SLOTS)
                   if i not in ask['decided'] and candidate['grace_end'] + offset <= now]
            late = due[-1:] if due and now > candidate['grace_end'] + SLOTS[due[-1]] + LATE else []
            decide(candidate, ask, due[:-1] + late, 'skipped')
            slots = [] if late else due[-1:]
            if slots:
                # A slot whose time fell inside a do-not-disturb or hold that has ended since stays blocked, even
                # if no tick ran before the block ended (C2). A block still active is checked in tick, in order.
                at = candidate['grace_end'] + SLOTS[slots[0]]
                rule = next((rule for rule in ('dnd', 'hold')
                             if self.state[rule + '_from'] <= at < self.state[rule + '_until'] <= now), None)
                if rule:
                    decide(candidate, ask, slots, rule, rule)
                    slots = []
            hold_end = max(self.state['hold_until'], self.state['released_at'])
            if ask['hold_slot'] == 'pending' and now >= self.state['hold_until']:
                if now > hold_end + LATE:
                    decide(candidate, ask, ['hold'], 'skipped')
                else:
                    slots.append('hold')
            if not slots:
                continue
            if candidate['opened'] or candidate['snoozed'] or candidate['later']:
                decide(candidate, ask, slots, 'skip_ask')
                if candidate['opened'] or candidate['later']:
                    ask['done'] = True
            elif not ask['done'] and ask['calls'] < ASK_MAX and ask['declines'] < 2:
                yield candidate, ask, slots

    def tick(self, candidates, now=None):
        now = self.clock() if now is None else now
        result = dict(placed=None, blocked=None, joined=[])
        events = []

        def decide(candidate, ask, slots, decision, rule=None):
            for slot in slots:
                if slot == 'hold':
                    ask['hold_slot'] = 'used'
                else:
                    ask['decided'].append(slot)
                events.append(('decide', dict(ask=candidate['id'], pane=candidate['pane'],
                                              slot=slot, result=decision, rule=rule)))

        pending = None
        with self.lock:
            if self.broken:
                if not self._reported_broken:
                    self._reported_broken = True
                    self.log('broken')
                return result
            before = deepcopy(self.state)
            live = self._call(self.state['live'])
            if live is not None and live['at'] < now - CALL_MAX:
                live['outcome'] = 'lost'
                self.state['live'] = None
                events.append(('outcome', dict(call_id=live['id'], outcome='lost')))
                live = None
            self.state['calls'] = [call for call in self.state['calls'] if call['at'] >= now - 48 * 3600]
            eligible = sorted(self._eligible(candidates, now, decide), key=lambda item: item[0]['grace_end'], reverse=True)
            if live is not None:
                # A slot that comes due for an ask already on the live call is used by that call.
                for candidate, ask, slots in eligible:
                    if candidate['id'] in live['panes']:
                        decide(candidate, ask, slots, 'on_call')
                targets = [item for item in eligible if item[0]['id'] not in live['panes']]
                for candidate, _, _ in targets:
                    live['asks'].append(candidate['id'])
                    live['panes'][candidate['id']] = candidate['pane']
                    result['joined'].append(candidate['id'])
                decision, rule = 'joined', None
            else:
                targets = eligible
                rule = self._blocked(now) if targets else None
                result['blocked'] = rule
                decision = rule or 'called'
                if targets and rule is None:
                    pending = uuid4().hex
                    agenda = [candidate for candidate, _, _ in targets]
                    self.state['calls'].append(dict(id=pending, at=now, asks=[item['id'] for item in agenda],
                                                    panes={item['id']: item['pane'] for item in agenda},
                                                    outcome=None, counted=True))
                    self.state['live'] = pending
            for candidate, ask, slots in targets:
                if pending is not None:
                    ask['calls'] += 1
                decide(candidate, ask, slots, decision, rule)
            if len(self.state['asks']) > ASK_KEEP:
                keep = set(sorted(self.state['asks'], key=lambda key: self.state['asks'][key]['seen_at'])[-ASK_KEEP:])
                keep |= {candidate['id'] for candidate in candidates}
                current = self._call(self.state['live'])
                keep |= set(current['panes']) if current is not None else set()
                self.state['asks'] = {key: ask for key, ask in self.state['asks'].items() if key in keep}
            try:
                if self._dirty or self.state != before:
                    self._save()
            except OSError:
                # Keep the slot decisions so a blocked slot never rings later (C2). Cancel only the new call.
                if pending is not None:
                    # The call was never placed: give its slots back, so it can ring once a save works.
                    self.state['calls'] = [call for call in self.state['calls'] if call['id'] != pending]
                    self.state['live'] = None
                    for _, ask, slots in targets:
                        ask['calls'] -= 1
                        for slot in slots:
                            if slot == 'hold':
                                ask['hold_slot'] = 'pending'
                            else:
                                ask['decided'].remove(slot)
                self._dirty = True
                self.log('save_failed')
                return dict(placed=None, blocked=None, joined=[])
        for event, fields in events:
            self.log(event, **fields)
        if pending is not None:
            result['placed'] = self._place(pending, agenda)
        return result

    def _place(self, pending, agenda):
        reason = None
        try:
            if self.place is None:
                raise CallRefused('no placement driver')
            call_id = self.place(agenda)
        except CallRefused as exc:
            reason = str(exc)
            for item in agenda:
                reason = reason.replace(item['call'], '[redacted]')
        except Exception as exc:
            reason = type(exc).__name__
        with self.lock:
            call = self._call(pending)
            if call is not None:
                if reason is None:
                    call['id'] = call_id
                    if self.state['live'] == pending:
                        self.state['live'] = call_id
                else:
                    call.update(counted=False, outcome='refused')
                    if self.state['live'] == pending:
                        self.state['live'] = None
                    for item in agenda:
                        ask = self.state['asks'].get(item['id'])
                        if ask is not None:
                            ask['calls'] -= 1
                self._dirty = True
                try:
                    self._save()
                except OSError:
                    pass  # The on-disk reservation still counts. Retry the save on the next update.
        if reason is not None:
            self.log('refused', call_id=pending, reason=reason)
            return None
        self.log('placed', call_id=call_id)
        return call_id

    def finish(self, call_id, outcome, told=()):
        with self.lock:
            if self.broken or call_id is None or call_id != self.state['live']:
                return False
            before = deepcopy(self.state)
            call = self._call(call_id)
            outcome = outcome if outcome in OUTCOMES else 'declined'
            self.state['live'], call['outcome'] = None, outcome
            opened = []
            if outcome == 'answered':
                told = set(told)
                for ask_id in call['asks']:
                    if ask_id in told:
                        self.state['asks'][ask_id]['done'] = True
                        opened.append((call['panes'][ask_id], ask_id))
            elif outcome == 'declined':
                now = self.clock()
                if self.state['hold_until'] <= now:
                    self.state['hold_from'] = now
                self.state['hold_until'] = max(self.state['hold_until'], now + HOLD)
                for ask_id in call['asks']:
                    ask = self.state['asks'][ask_id]
                    ask['declines'] += 1
                    if ask['declines'] == 1:
                        ask['hold_slot'] = 'pending'
                    else:
                        ask['done'] = True
            self._commit(before)
        self.log('outcome', call_id=call_id, outcome=outcome)
        if self.mark_opened is not None:
            for pane, ask_id in opened:
                try:
                    if not self.mark_opened(pane, ask_id):
                        self.log('open_failed', ask=ask_id, error='gone')
                except Exception as exc:  # The ask stays done here: Mak heard it. Tether just shows it unopened.
                    self.log('open_failed', ask=ask_id, error=type(exc).__name__)
        return True

    def set_dnd(self, until):
        with self.lock:
            now = self.clock()
            if not _number(until) or not now < until <= now + DND_MAX:
                raise ValueError('Do-not-disturb must end within the next 24 hours')
            before = deepcopy(self.state)
            if self.state['dnd_until'] <= now:
                self.state['dnd_from'] = now
            self.state['dnd_until'] = until
            self._commit(before)

    def calls_back_on(self):
        with self.lock:
            before = deepcopy(self.state)
            now = self.clock()
            # End both blocks now. A pending hold slot is due on the next tick (C6).
            self.state.update(dnd_until=min(self.state['dnd_until'], now),
                              hold_until=min(self.state['hold_until'], now), released_at=now)
            self._commit(before)

    def status(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            local = datetime.fromtimestamp(now, self.zone())
            active = {key: self.state[key] if self.state[key] > now else 0 for key in ('dnd_until', 'hold_until')}
            return dict(dnd_until=active['dnd_until'], hold_until=active['hold_until'],
                        calls_today=self._today(local), live=self.state['live'])

    def live_agenda(self):
        with self.lock:
            call = self._call(self.state['live'])
            return list(call['asks']) if call is not None else []
