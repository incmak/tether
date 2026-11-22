import unittest_isolated  # noqa: F401
from datetime import datetime
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import urgent_call


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'state.json'
        self.tz = ZoneInfo('Asia/Kolkata')
        self.clock = Clock(self.local(11, 22, 10))
        self.settings = {}
        self.agendas = []
        self.log = mock.Mock()
        self.mark_opened = mock.Mock()
        self.planner = self.load()

    def local(self, month, day, hour=0, minute=0):
        return datetime(2026, month, day, hour, minute, tzinfo=self.tz).timestamp()

    def place(self, agenda):
        self.agendas.append(agenda)
        return f'call-{len(self.agendas)}'

    def load(self, place=None):
        return urgent_call.Planner(self.path, clock=self.clock, zone=lambda: self.tz, settings=lambda: self.settings,
                                   place=self.place if place is None else place, mark_opened=self.mark_opened,
                                   log=self.log)

    def candidate(self, ask_id='a', **fields):
        candidate = dict(id=ask_id, pane='%1', thread='t1', call=f'Private question {ask_id}?',
                         grace_end=self.clock.now, opened=False, snoozed=False, later=False)
        return dict(candidate, **fields)

    def state(self):
        return json.loads(self.path.read_text())

    def place_and_finish(self, *candidates):
        result = self.planner.tick(candidates)
        self.assertIsNotNone(result['placed'], result)
        self.assertTrue(self.planner.finish(result['placed'], 'unanswered'))
        return result['placed']

    def test_opening_during_grace_ends_the_ask(self):
        ask = self.candidate(grace_end=self.clock.now + 180)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.state()['asks']['a']['decided'], [])
        ask['opened'] = True
        self.clock.advance(180)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertTrue(self.state()['asks']['a']['done'])
        self.log.assert_called_with('decide', ask='a', pane='%1', slot=0, result='skip_ask', rule=None)
        self.clock.advance(600)
        self.assertIsNone(self.planner.tick([dict(ask, opened=False)])['placed'])
        self.assertEqual(self.agendas, [])

    def test_snoozed_skips_a_slot_and_later_ends_the_ask(self):
        snoozed = self.candidate(snoozed=True)
        later = self.candidate('b', later=True)
        self.assertIsNone(self.planner.tick([snoozed, later])['placed'])
        self.assertFalse(self.state()['asks']['a']['done'])
        self.assertTrue(self.state()['asks']['b']['done'])
        self.clock.advance(600)
        self.assertEqual(self.planner.tick([dict(snoozed, snoozed=False), dict(later, later=False)])['placed'], 'call-1')
        self.assertEqual(self.planner.live_agenda(), ['a'])

    def test_window_allows_second_call_and_blocks_third(self):
        self.assertEqual(self.place_and_finish(self.candidate()), 'call-1')
        self.clock.advance(5 * 60)
        self.assertEqual(self.place_and_finish(self.candidate('b')), 'call-2')
        self.clock.advance(15 * 60 + 1)
        third = self.candidate('c')
        self.assertEqual(self.planner.tick([third])['blocked'], 'window')
        self.assertIn(mock.call('decide', ask='c', pane='%1', slot=0, result='window', rule='window'),
                      self.log.call_args_list)
        self.clock.advance(10 * 60)
        self.assertEqual(self.place_and_finish(third), 'call-3')

    def test_window_excludes_call_at_exact_cutoff(self):
        self.place_and_finish(self.candidate())
        self.clock.advance(1)
        self.place_and_finish(self.candidate('b'))
        self.clock.advance(urgent_call.WINDOW - 1)
        self.assertEqual(self.place_and_finish(self.candidate('c')), 'call-3')

    def test_day_allows_eight_calls_then_resets_at_local_midnight(self):
        for i in range(8):
            self.assertEqual(self.place_and_finish(self.candidate(str(i))), f'call-{i + 1}')
            self.clock.advance(urgent_call.WINDOW)
        self.assertEqual(self.planner.status()['calls_today'], 8)
        self.clock.now = self.local(11, 22, 23, 45)
        ninth = self.candidate('ninth')
        self.assertEqual(self.planner.tick([ninth])['blocked'], 'day')
        self.clock.advance(10 * 60)
        self.assertEqual(self.planner.tick([ninth])['blocked'], 'day')
        self.clock.now = self.local(11, 23)
        self.assertEqual(self.planner.status()['calls_today'], 0)
        self.assertIsNone(self.planner.tick([ninth])['placed'])
        self.assertEqual(self.place_and_finish(self.candidate('new-day')), 'call-9')
        self.clock.now = self.local(11, 23, 0, 25)
        self.assertEqual(self.place_and_finish(ninth), 'call-10')

    def test_count_uses_midnight_before_dst_offset_change(self):
        self.tz = ZoneInfo('America/New_York')
        self.clock.now = self.local(3, 7, 23, 59)
        self.place_and_finish(self.candidate('yesterday'))
        self.clock.now = self.local(3, 8)
        self.place_and_finish(self.candidate('midnight'))
        self.clock.now = self.local(3, 8, 12)
        self.assertEqual(self.planner.status()['calls_today'], 1)
        self.clock.now = self.local(3, 9)
        self.assertEqual(self.planner.status()['calls_today'], 0)

    def test_one_ask_rings_at_zero_ten_and_forty_minutes_only(self):
        ask = self.candidate()
        self.assertEqual(self.place_and_finish(ask), 'call-1')
        self.clock.advance(599)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.clock.advance(1)
        self.assertEqual(self.place_and_finish(ask), 'call-2')
        self.clock.advance(1799)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.clock.advance(1)
        self.assertEqual(self.place_and_finish(ask), 'call-3')
        self.clock.advance(urgent_call.HOLD)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.state()['asks']['a']['calls'], 3)

    def test_dnd_blocked_slot_is_never_made_up(self):
        ask = self.candidate()
        self.planner.set_dnd(self.clock.now + 60)
        self.assertEqual(self.planner.tick([ask])['blocked'], 'dnd')
        self.clock.advance(60)
        self.assertEqual(self.planner.tick([ask]), dict(placed=None, blocked=None, joined=[]))
        self.clock.advance(540)
        self.assertEqual(self.place_and_finish(ask), 'call-1')

    def test_downtime_skips_all_but_latest_due_slot(self):
        ask = self.candidate()
        self.clock.advance(50 * 60)
        self.assertEqual(self.place_and_finish(ask), 'call-1')
        self.assertEqual(self.state()['asks']['a']['decided'], [0, 1, 2])
        self.assertEqual(self.log.call_args_list[:3], [
            mock.call('decide', ask='a', pane='%1', slot=i, result=result, rule=None)
            for i, result in enumerate(('skipped', 'skipped', 'called'))])
        self.assertIsNone(self.planner.tick([ask])['placed'])

    def test_first_decline_gives_one_hold_slot_and_second_decline_ends_ask(self):
        ask = self.candidate()
        call_id = self.planner.tick([ask])['placed']
        self.assertTrue(self.planner.finish(call_id, 'declined'))
        self.assertEqual(self.planner.status()['hold_until'], self.clock.now + urgent_call.HOLD)
        self.clock.advance(600)
        self.assertEqual(self.planner.tick([ask])['blocked'], 'hold')
        self.clock.advance(1800)
        self.assertEqual(self.planner.tick([ask])['blocked'], 'hold')
        self.clock.advance(urgent_call.HOLD - 2400)
        second = self.planner.tick([ask])['placed']
        self.assertEqual(second, 'call-2')
        self.assertTrue(self.planner.finish(second, 'declined'))
        self.assertTrue(self.state()['asks']['a']['done'])
        self.clock.advance(urgent_call.HOLD)
        self.assertIsNone(self.planner.tick([ask])['placed'])

    def test_answered_opens_only_told_asks_and_finish_is_idempotent(self):
        told = self.candidate()
        untold = self.candidate('b', pane='%2')
        self.assertEqual(self.planner.tick([told, untold])['placed'], 'call-1')
        self.assertFalse(self.planner.finish('unknown', 'answered', ('a',)))
        def mark_opened(*args):
            self.assertFalse(self.planner.lock.locked())
            self.assertIsNone(self.planner.status()['live'])

        self.mark_opened.side_effect = mark_opened
        self.assertTrue(self.planner.finish('call-1', 'answered', ('a', 'unknown', 'a')))
        self.assertFalse(self.planner.finish('call-1', 'answered', ('b',)))
        self.mark_opened.assert_called_once_with('%1', 'a')
        self.clock.advance(600)
        self.assertEqual(self.planner.tick([told, untold])['placed'], 'call-2')
        self.assertEqual(self.agendas[-1], [untold])

    def test_failed_open_logs_and_still_opens_the_other_told_asks(self):
        first, second = self.candidate(), self.candidate('b', pane='%2')
        self.assertEqual(self.planner.tick([first, second])['placed'], 'call-1')
        self.mark_opened.side_effect = [RuntimeError('disk'), False]
        self.assertTrue(self.planner.finish('call-1', 'answered', ('a', 'b')))
        self.assertEqual(self.mark_opened.call_count, 2)
        self.log.assert_any_call('open_failed', ask='b', error='gone')
        failed = [c for c in self.log.call_args_list if c.args == ('open_failed',)]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(self.state()['asks'][key]['done'] for key in ('a', 'b')))

    def test_calls_back_on_releases_a_long_expired_pending_hold_slot(self):
        ask = self.candidate()
        self.assertTrue(self.planner.finish(self.planner.tick([ask])['placed'], 'declined'))
        self.clock.advance(urgent_call.HOLD + urgent_call.LATE + 1)
        self.planner.calls_back_on()
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-2')

    def test_unknown_outcome_counts_as_declined(self):
        for i, outcome in enumerate((None, 'established')):
            with self.subTest(outcome=outcome):
                self.clock.advance(urgent_call.HOLD)
                call_id = self.planner.tick([self.candidate(str(i))])['placed']
                self.assertTrue(self.planner.finish(call_id, outcome))
                self.assertEqual(self.state()['calls'][-1]['outcome'], 'declined')
                self.assertEqual(self.planner.status()['hold_until'], self.clock.now + urgent_call.HOLD)

    def test_calls_back_on_clears_dnd_and_hold_and_releases_pending_hold_slot(self):
        ask = self.candidate()
        call_id = self.planner.tick([ask])['placed']
        self.assertTrue(self.planner.finish(call_id, 'declined'))
        for until in (self.clock.now - 1, self.clock.now, self.clock.now + urgent_call.DND_MAX + 1):
            with self.subTest(until=until), self.assertRaises(ValueError):
                self.planner.set_dnd(until)
        until = self.clock.now + urgent_call.DND_MAX
        self.planner.set_dnd(until)
        self.assertEqual(self.planner.status()['dnd_until'], until)
        self.assertEqual(self.planner.tick([self.candidate('b')])['blocked'], 'dnd')
        self.planner.calls_back_on()
        self.assertEqual(self.planner.status(), dict(dnd_until=0, hold_until=0, calls_today=1, live=None))
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-2')
        self.assertEqual(self.state()['asks']['a']['hold_slot'], 'used')

    def test_daytime_boundaries_and_wrapped_range(self):
        for settings, hour, minute, rule in (
            ({'mode': 'daytime'}, 22, 0, 'night'),
            ({'mode': 'daytime'}, 7, 59, 'night'),
            ({'mode': 'daytime'}, 8, 0, None),
            ({'mode': 'daytime', 'day_start': '8:00', 'day_end': 0}, 21, 59, None),
            ({'mode': 'daytime', 'day_start': '22:00', 'day_end': '06:00'}, 22, 0, None),
            ({'mode': 'daytime', 'day_start': '22:00', 'day_end': '06:00'}, 5, 59, None),
            ({'mode': 'daytime', 'day_start': '22:00', 'day_end': '06:00'}, 6, 0, 'night'),
            ({'mode': 'off'}, 12, 0, 'off'),
            ({'mode': 'invalid'}, 1, 0, None),
        ):
            with self.subTest(settings=settings, hour=hour, minute=minute):
                self.path.unlink(missing_ok=True)
                self.settings = settings
                self.planner = self.load()
                self.clock.now = self.local(11, 22, hour, minute)
                result = self.planner.tick([self.candidate()])
                self.assertEqual(result['blocked'], rule)
                self.assertEqual(result['placed'] is not None, rule is None)

    def test_first_block_wins(self):
        call_id = self.planner.tick([self.candidate()])['placed']
        self.planner.finish(call_id, 'declined')
        self.planner.set_dnd(self.clock.now + 60)
        self.assertEqual(self.planner.tick([self.candidate('b')])['blocked'], 'dnd')
        self.settings['mode'] = 'off'
        self.assertEqual(self.planner.tick([self.candidate('c')])['blocked'], 'off')

    def test_live_call_joins_due_asks_and_keeps_their_panes_after_reload(self):
        ask = self.candidate()
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')
        self.clock.advance(1)
        second = self.candidate('b', pane='%2')
        self.settings['mode'] = 'off'
        self.assertEqual(self.planner.tick([ask, second]), dict(placed=None, blocked=None, joined=['b']))
        self.assertEqual(self.planner.tick([ask, second])['joined'], [])
        self.assertEqual(self.agendas, [[ask]])
        self.planner = self.load()
        self.assertEqual(self.planner.live_agenda(), ['a', 'b'])
        self.assertTrue(self.planner.finish('call-1', 'answered', ('b',)))
        self.mark_opened.assert_called_once_with('%2', 'b')
        self.assertEqual(self.planner.live_agenda(), [])

    def test_slot_due_during_a_live_call_is_used_by_that_call(self):
        ask = self.candidate()
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')
        self.clock.advance(11 * 60)
        self.assertEqual(self.planner.tick([ask]), dict(placed=None, blocked=None, joined=[]))
        self.assertTrue(self.planner.finish('call-1', 'unanswered'))
        self.clock.advance(1)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.clock.now = ask['grace_end'] + 40 * 60
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-2')

    def test_refusal_and_other_exceptions_use_slot_without_counting_call(self):
        refused = self.candidate()
        error = self.candidate('error')
        place = mock.Mock(side_effect=[urgent_call.CallRefused('provider refused'),
                                       RuntimeError(error['call']), 'first', 'second'])
        self.planner = self.load(place=place)
        for candidate in (refused, error):
            self.assertIsNone(self.planner.tick([candidate])['placed'])
            self.assertIsNone(self.planner.tick([candidate])['placed'])
            self.assertEqual(self.state()['asks'][candidate['id']]['calls'], 0)
        self.assertEqual([call['outcome'] for call in self.state()['calls']], ['refused', 'refused'])
        self.assertEqual(self.planner.status()['calls_today'], 0)
        self.assertEqual(self.place_and_finish(self.candidate('b')), 'first')
        self.assertEqual(self.place_and_finish(self.candidate('c')), 'second')
        self.assertEqual(self.planner.tick([self.candidate('d')])['blocked'], 'window')
        reasons = [fields['reason'] for (event,), fields in self.log.call_args_list if event == 'refused']
        self.assertEqual(reasons, ['provider refused', 'RuntimeError'])
        self.assertNotIn(error['call'], str(self.log.call_args_list))

    def test_lost_call_expires_strictly_after_limit_and_remains_counted(self):
        ask = self.candidate()
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')
        self.planner = self.load()
        self.clock.advance(urgent_call.CALL_MAX)
        self.planner.tick([ask])
        self.assertEqual(self.planner.status()['live'], 'call-1')
        self.clock.advance(1)
        self.assertEqual(self.place_and_finish(self.candidate('b')), 'call-2')
        self.assertEqual(self.state()['calls'][0]['outcome'], 'lost')
        self.assertFalse(self.planner.finish('call-1', 'answered', ('a',)))
        self.assertEqual(self.planner.status()['calls_today'], 2)
        self.assertEqual(self.planner.tick([self.candidate('c')])['blocked'], 'window')

    def test_agenda_order_and_save_before_place_without_holding_lock(self):
        ask = self.candidate(grace_end=self.clock.now - 100)
        newer = self.candidate('b')

        def place(agenda):
            self.assertEqual(agenda, [newer, ask])
            self.assertFalse(self.planner.lock.locked())
            call, = self.state()['calls']
            self.assertEqual(call['asks'], ['b', 'a'])
            self.assertEqual(self.planner.status()['live'], call['id'])
            return 'external-id'

        self.planner = self.load(place=place)
        self.assertEqual(self.planner.tick([ask, newer])['placed'], 'external-id')
        self.assertEqual(self.state()['live'], 'external-id')
        self.assertEqual(self.state()['calls'][0]['id'], 'external-id')
        self.assertNotIn(ask['call'], self.path.read_text())
        self.assertNotIn(newer['call'], self.path.read_text())

    def test_corrupt_invalid_and_unreadable_state_fail_closed(self):
        ask = self.candidate()
        valid = dict(version=1, asks={}, calls=[], hold_until=0, dnd_until=0, live=None)
        invalid = [
            '{', '[]', '{}', json.dumps(dict(valid, version=True)),
            json.dumps(dict(valid, live='missing')), json.dumps(dict(valid, calls=[{}])),
            json.dumps(dict(valid, asks={'a': None})),
        ]
        for original in invalid:
            with self.subTest(original=original):
                self.log.reset_mock()
                self.path.write_text(original)
                self.planner = self.load()
                self.assertTrue(self.planner.broken)
                for _ in range(2):
                    self.assertIsNone(self.planner.tick([ask])['placed'])
                self.log.assert_called_once_with('broken')
                self.assertEqual(self.path.read_text(), original)
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertTrue(self.load().broken)
        self.assertEqual(self.agendas, [])

    def test_missing_file_starts_fresh_and_saves_mode_0600(self):
        self.assertFalse(self.path.exists())
        self.assertFalse(self.planner.broken)
        self.assertEqual(self.place_and_finish(self.candidate()), 'call-1')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_save_places_nothing_and_gives_the_slot_back(self):
        ask = self.candidate()
        with mock.patch.object(urgent_call.os, 'replace', side_effect=OSError('denied')):
            self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.planner.status(), dict(dnd_until=0, hold_until=0, calls_today=0, live=None))
        self.assertEqual(list(self.path.parent.iterdir()), [])
        self.assertEqual(self.agendas, [])
        self.clock.advance(1)
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')
        self.assertEqual(self.state()['asks']['a']['decided'], [0])

    def test_pruning_keeps_asks_seen_in_this_tick(self):
        with mock.patch.object(urgent_call, 'ASK_KEEP', 2):
            later = [self.candidate(f'x{i}', grace_end=self.clock.now + 3600) for i in range(3)]
            call_id = self.planner.tick([self.candidate(), *later])['placed']
            self.assertEqual(call_id, 'call-1')
            self.assertIsNotNone(self.load())
            self.assertFalse(self.load().broken)
            self.assertTrue(self.planner.finish(call_id, 'unanswered'))
            self.assertIn('a', self.state()['asks'])

    def test_slot_found_more_than_ten_minutes_late_is_skipped(self):
        ask = self.candidate()
        self.clock.advance(10 * 60 - 1)
        self.planner.tick([self.candidate('b', grace_end=self.clock.now + 3600)])
        self.clock.now = ask["grace_end"] + 20 * 60 + 1
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(sorted(self.state()['asks']['a']['decided']), [0, 1])
        self.clock.now = ask['grace_end'] + 40 * 60 + 10 * 60
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')

    def test_slot_inside_an_ended_dnd_does_not_ring_without_a_tick_or_after_restart(self):
        ask = self.candidate(grace_end=self.clock.now + 60)
        self.planner.set_dnd(self.clock.now + 120)
        self.clock.advance(121)
        self.planner = self.load()
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.state()['asks']['a']['decided'], [0])
        self.clock.now = ask['grace_end'] + 10 * 60
        self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')

    def test_slot_inside_an_ended_hold_does_not_ring_and_hold_slot_still_does(self):
        first = self.candidate()
        self.assertTrue(self.planner.finish(self.planner.tick([first])['placed'], 'declined'))
        other = self.candidate('b', grace_end=self.clock.now + urgent_call.HOLD - 300)
        self.clock.now = first['grace_end'] + urgent_call.HOLD + 1
        self.assertIsNone(self.planner.tick([other])['placed'])
        self.assertEqual(self.state()['asks']['b']['decided'], [0])
        self.assertEqual(self.planner.tick([first])['placed'], 'call-2')

    def test_slot_inside_an_earlier_of_two_ended_dnd_blocks_does_not_ring(self):
        ask = self.candidate(grace_end=self.clock.now + 30)
        self.planner.set_dnd(self.clock.now + 60)
        self.clock.advance(90)
        self.planner.set_dnd(self.clock.now + 60)
        self.clock.advance(61)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.state()['asks']['a']['decided'], [0])

    def test_hold_retry_due_inside_an_ended_dnd_does_not_ring(self):
        ask = self.candidate()
        self.assertTrue(self.planner.finish(self.planner.tick([ask])['placed'], 'declined'))
        self.clock.now = ask['grace_end'] + urgent_call.HOLD - 30
        self.planner.set_dnd(self.clock.now + 60)
        self.clock.advance(61)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.state()['asks']['a']['hold_slot'], 'used')

    def test_calls_back_on_after_the_clock_moves_back_keeps_a_valid_state(self):
        ask = self.candidate()
        self.assertTrue(self.planner.finish(self.planner.tick([ask])['placed'], 'declined'))
        self.planner.set_dnd(self.clock.now + 600)
        self.clock.advance(-1)
        self.planner.calls_back_on()
        self.assertTrue(all(start < until for _, start, until in self.state()['blocks']))
        self.planner = self.load()
        self.assertEqual(self.planner.status()['dnd_until'], 0)
        self.clock.advance(1)
        self.assertEqual(self.planner.tick([self.candidate('b')])['placed'], 'call-2')

    def test_ended_overlapping_blocks_report_dnd_before_hold(self):
        first = self.candidate()
        self.assertTrue(self.planner.finish(self.planner.tick([first])['placed'], 'declined'))
        other = self.candidate('b', grace_end=self.clock.now + urgent_call.HOLD - 120)
        self.planner.set_dnd(self.clock.now + urgent_call.HOLD - 60)
        self.clock.now = first['grace_end'] + urgent_call.HOLD + 1
        self.log.reset_mock()
        self.assertIsNone(self.planner.tick([other])['placed'])
        self.log.assert_any_call('decide', ask='b', pane='%1', slot=0, result='dnd', rule='dnd')

    def test_slot_blocked_while_save_fails_does_not_ring_when_the_block_ends(self):
        ask = self.candidate()
        self.planner.set_dnd(self.clock.now + 60)
        with mock.patch.object(urgent_call.os, 'replace', side_effect=OSError('denied')):
            self.assertIsNone(self.planner.tick([ask])['placed'])
        self.clock.advance(61)
        self.assertIsNone(self.planner.tick([ask])['placed'])
        self.assertEqual(self.agendas, [])
        self.assertEqual(self.state()['asks']['a']['decided'], [0])

    def test_failed_external_id_save_keeps_the_reservation_until_the_next_save(self):
        ask = self.candidate()
        replace = urgent_call.os.replace

        def save_reservation_only(temp, path):
            if self.agendas:
                raise OSError('denied')
            replace(temp, path)

        with mock.patch.object(urgent_call.os, 'replace', side_effect=save_reservation_only):
            self.assertEqual(self.planner.tick([ask])['placed'], 'call-1')
        self.assertNotEqual(self.state()['live'], 'call-1')
        self.assertEqual(self.load().live_agenda(), ['a'])
        self.assertEqual(self.planner.tick([ask]), dict(placed=None, blocked=None, joined=[]))
        self.assertEqual(self.state()['live'], 'call-1')
        self.assertEqual(self.agendas, [[ask]])

    def test_prunes_old_calls_and_keeps_newest_ask_records(self):
        old = self.candidate()
        self.place_and_finish(old)
        self.clock.advance(48 * 3600)
        self.planner.tick([])
        self.assertEqual(len(self.state()['calls']), 1)
        self.clock.advance(1)
        self.planner.tick([])
        self.assertEqual(self.state()['calls'], [])
        # An ask parked for weeks keeps its decided slots, so it does not ring again on return.
        self.clock.advance(30 * 86400)
        self.planner.tick([])
        self.assertIsNone(self.planner.tick([old])['placed'])
        with mock.patch.object(urgent_call, 'ASK_KEEP', 2):
            for ask_id in ('b', 'c'):
                self.clock.advance(1)
                self.planner.tick([self.candidate(ask_id, grace_end=self.clock.now + 3600)])
        self.assertEqual(sorted(self.state()['asks']), ['b', 'c'])
