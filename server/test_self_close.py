import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import agent_tab_cli
import audit
import self_close


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class WatchTests(unittest.TestCase):
    entry = {'id': '%12', 'agent': 'claude'}

    def watch(self, states, live=None, fresh=None):
        clock, states = Clock(), iter(states)
        status = Mock(side_effect=lambda entry: {'state': next(states, 'Idle')})
        sessions = lambda: [live if live is not None else dict(self.entry, thread='T1')]
        self.entry = dict(self.entry, thread='T1')
        with patch.object(self_close.helper_close, 'close_pane') as close:
            result = self_close.watch(self.entry, 'run', status, sessions=sessions, lock=threading.Lock(),
                                      fresh=fresh or (lambda entry: {'state': 'Idle'}), sleep=clock.sleep, now=clock.now)
        return result, close, clock

    def test_closes_after_the_reply_ends_and_stays_quiet(self):
        result, close, clock = self.watch(['Working'] * 5)
        self.assertEqual(result, 'closed')
        close.assert_called_once_with('%12', 'run')
        self.assertGreaterEqual(clock.t, 5 + self_close.STEADY)

    def test_a_short_quiet_gap_between_tool_calls_does_not_close(self):
        result, close, clock = self.watch(['Working', 'Idle', 'Working', 'Idle', 'Idle', 'Working'])
        self.assertEqual(result, 'closed')
        self.assertGreaterEqual(clock.t, 6 + self_close.STEADY)

    def test_waits_on_a_prompt(self):
        result, close, _ = self.watch(['Input needed'] * 20)
        self.assertEqual(result, 'closed')
        close.assert_called_once()

    def test_a_tab_that_keeps_working_stays_open(self):
        result, close, _ = self.watch(['Working'] * (self_close.WAIT + 10))
        self.assertEqual(result, 'timeout')
        close.assert_not_called()

    def test_a_pane_that_ended_first_is_left_alone(self):
        result, close, _ = self.watch(['Working', 'Unavailable'])
        self.assertEqual(result, 'gone')
        close.assert_not_called()

    def test_a_message_sent_just_before_the_close_keeps_the_tab(self):
        fresh = iter([{'state': 'Working'}, {'state': 'Idle'}])
        result, close, clock = self.watch(['Working'], fresh=lambda entry: next(fresh))
        self.assertEqual(result, 'closed')
        self.assertGreaterEqual(clock.t, 1 + 2 * self_close.STEADY)  # the quiet time started again

    def test_a_pane_with_another_conversation_is_left_alone(self):
        for live in ({'id': '%12', 'agent': 'claude', 'thread': 'T2'}, {'id': '%12', 'agent': 'codex', 'thread': 'T1'}):
            with self.subTest(live=live):
                result, close, _ = self.watch(['Working'], live=live)
                self.assertEqual(result, 'gone')
                close.assert_not_called()

    def test_never_closes_after_the_deadline(self):
        result, close, clock = self.watch(['Working'] * (self_close.WAIT - 2))
        self.assertEqual(result, 'timeout')
        close.assert_not_called()

    def test_one_watch_per_tab(self):
        jobs = []
        kwargs = dict(sessions=list, lock=threading.Lock(), fresh=Mock(), start=jobs.append)
        first = self_close.schedule(self.entry, 'run', Mock(), **kwargs)
        second = self_close.schedule(self.entry, 'run', Mock(), **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(jobs), 1)
        self_close._pending.discard('%12')

    def test_scheduled_close_records_the_request_actor_after_the_pane_closes(self):
        clock, jobs = Clock(), []
        current = dict(self.entry, pane_name='Audit Log')
        with tempfile.TemporaryDirectory() as folder, patch.object(audit.paths, 'CONFIG', Path(folder)), \
                patch.object(audit, '_tab_name', side_effect=['Audit Log', None]) as tab_name, \
                patch.object(self_close.helper_close, 'close_pane') as close:
            with audit.acting('agent-cli', pane='%12'):
                for _ in range(2):
                    self_close.schedule(current, 'run', lambda entry: {'state': 'Idle'}, sessions=lambda: [current],
                                        lock=threading.Lock(), fresh=lambda entry: {'state': 'Idle'}, start=jobs.append,
                                        sleep=clock.sleep, now=clock.now)
            self.addCleanup(self_close._pending.discard, '%12')
            job, = jobs
            self.assertEqual(audit.query()['entries'], [])
            self.assertEqual(job(), 'closed')
            close.assert_called_once_with('%12', 'run')
            tab_name.assert_called_once_with('%12')
            entry, = audit.query()['entries']
            self.assertEqual((entry['kind'], entry['summary']), ('tab.close', 'Closed tab Audit Log'))
            self.assertEqual(entry['actor'], {'source': 'agent-cli', 'pane': '%12', 'tab': 'Audit Log'})


class CliTests(unittest.TestCase):
    def test_close_self(self):
        self.assertEqual(agent_tab_cli.arguments(['close', '--self']), {'action': 'close', 'self': True})

    def test_close_pane(self):
        self.assertEqual(agent_tab_cli.arguments(['close', '--pane', '%3']), {'action': 'close', 'pane': '%3'})

    def test_close_needs_one_target(self):
        for argv in (['close'], ['close', '--self', '--pane', '%3']):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                agent_tab_cli.arguments(argv)


if __name__ == '__main__':
    unittest.main()
