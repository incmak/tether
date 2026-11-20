from contextlib import nullcontext
import json
import subprocess
from threading import Lock
import unittest
from unittest.mock import Mock, call, patch

import helper_close
import undo
from test_review_links import ApprovalTurnTimingFixture

OWNER_THREAD = '11111111-1111-4111-8111-111111111111'
OTHER_THREAD = '22222222-2222-4222-8222-222222222222'


def record(owner='%1', thread=OWNER_THREAD):
    return json.dumps({'owner_pane': owner, 'owner_thread': thread, 'helper_thread': None, 'sent_at': 1.0, 'before': None})


class Tmux:
    """tmux list-panes rows: pane -> (role, helper record JSON). Records every kill-pane."""
    def __init__(self, panes):
        self.panes = panes
        self.killed = []

    def __call__(self, args, **kwargs):
        if 'list-panes' in args:
            out = ''.join(f'{pane}\t{role}\t{raw}\n' for pane, (role, raw) in self.panes.items())
            return subprocess.CompletedProcess(args, 0, out, '')
        if 'display-message' in args:
            pane = args[args.index('-t') + 1]
            if pane not in self.panes:
                return subprocess.CompletedProcess(args, 0, '\n', '')
            return subprocess.CompletedProcess(args, 0, f'{pane}\t{self.panes[pane][1]}\n', '')
        if 'kill-pane' in args:
            self.killed.append(args[-1])
            self.panes.pop(args[-1], None)
        return subprocess.CompletedProcess(args, 0, '', '')


class CloserTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.links = {}
        self.rows = []
        self.states = {}
        self.viewed = set()
        patches = [patch.object(helper_close.review_links, 'reviewed_by', side_effect=lambda: self.links),
                   patch.object(helper_close.review_links, '_read', side_effect=lambda: self.rows),
                   patch.object(helper_close.review_links, 'close'),
                   patch.object(helper_close.auto_resume, 'forget_pane')]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.scope = Mock()
        self.closer = helper_close.Closer(now=lambda: self.clock[0], scope=self.scope)

    def entries(self, tmux, threads=None):
        threads = threads or {}
        return [{'id': pane, 'thread': threads.get(pane, OWNER_THREAD if pane == '%1' else None)} for pane in tmux.panes]

    def sweep(self, tmux, after=0, threads=None):
        self.clock[0] += after
        return self.closer.observe(self.entries(tmux, threads), lambda entry: {'state': self.states.get(entry['id'], 'Idle')},
                                   tmux, watching=lambda pane: pane in self.viewed, log=lambda text: None)

    def test_idle_advisor_closes_after_30_minutes(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record()), '%3': ('second-advisor', record())})
        self.assertEqual(self.sweep(tmux), [])
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE - 31), [])
        closed = self.sweep(tmux, after=31)
        self.assertEqual(sorted(pane for pane, _ in closed), ['%2', '%3'])
        self.assertEqual(sorted(tmux.killed), ['%2', '%3'])
        self.assertIn('idle for 30 minutes', closed[0][1])

    def test_busy_waiting_or_viewed_tab_stays_and_its_timer_restarts(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record()), '%3': ('advisor', record()), '%4': ('advisor', record())})
        self.sweep(tmux)
        self.states['%2'] = 'Working'
        self.states['%3'] = 'Input needed'
        self.viewed.add('%4')
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE + 1), [])
        self.states.clear()
        self.viewed.clear()
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE - 31), [])
        self.assertEqual(len(self.sweep(tmux, after=31)), 3)

    def test_helper_closes_one_minute_after_its_owner_is_gone(self):
        tmux = Tmux({'%2': ('', record(owner='%9')), '%3': ('advisor', record(owner='%9'))})
        self.assertEqual(self.sweep(tmux), [])
        closed = self.sweep(tmux, after=helper_close.GRACE)
        self.assertEqual(sorted(pane for pane, _ in closed), ['%2', '%3'])
        self.assertEqual(closed[0][1], 'its owner tab is gone')

    def test_owner_changed_conversation_closes_stale_helper(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('', record())})
        self.sweep(tmux, threads={'%1': OTHER_THREAD})
        self.assertEqual(self.sweep(tmux, after=helper_close.GRACE, threads={'%1': OTHER_THREAD}),
                         [('%2', 'its owner tab changed conversations')])

    def test_own_tab_stays_if_its_opener_changes_conversation_or_closes(self):
        own = json.dumps(dict(json.loads(record()), kind='own', home_pane=None))
        tmux = Tmux({'%1': ('', ''), '%2': ('', own), '%3': ('', record())})
        moved = {'%1': OTHER_THREAD}
        self.assertEqual(self.sweep(tmux, threads=moved), [])
        self.assertEqual(self.sweep(tmux, after=helper_close.GRACE, threads=moved),
                         [('%3', 'its owner tab changed conversations')])
        tmux.panes.pop('%1')
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [])
        self.assertEqual(self.sweep(tmux, after=helper_close.GRACE), [])
        self.assertEqual(tmux.killed, ['%3'])

    def test_owner_without_a_known_thread_keeps_helper(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('', record())})
        self.sweep(tmux, threads={'%1': None})
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE * 2, threads={'%1': None}), [])

    def test_task_helper_stays_while_its_owner_lives(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('', record())})
        self.sweep(tmux)
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE * 4), [])

    def test_reviewer_closes_when_its_review_ends(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('reviewer', record())})
        self.links = {'%1': ['%2']}
        self.sweep(tmux)
        self.assertEqual(self.sweep(tmux, after=helper_close.GRACE), [])  # still linked
        self.links = {}
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [])  # the grace starts when the link ends
        self.assertEqual(self.sweep(tmux, after=helper_close.GRACE), [('%2', 'its review ended')])

    def test_linked_reviewer_waits_for_a_working_worker(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('', record())})
        self.links = {'%1': ['%2']}
        self.sweep(tmux)
        self.states['%1'] = 'Working'
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE + 1), [])
        self.states.clear()
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE - 1), [])
        closed = self.sweep(tmux, after=helper_close.SWEEP)
        self.assertEqual(closed, [('%2', 'the review has been idle for 30 minutes')])
        helper_close.review_links.close.assert_called_once_with('%2')

    def test_idle_reviewer_uses_the_verdict_folder(self):
        self.links = {'%1': ['%2']}
        head, folder = 'a' * 40, '/work/reviewed'
        baseline = dict(commit=head, dirty=True, mark='reviewed tree', folder=folder)
        self.rows = [dict(worker='%1', reviewer='%2', worker_thread=OWNER_THREAD, approved_state=baseline,
                          review_commit='b' * 40, review_folder='/work/new-request')]
        tmux = Tmux({'%1': ('', ''), '%2': ('reviewer', record())})
        live = dict(baseline)
        self.scope.side_effect = lambda entry: live if entry.get('review_folder') == folder else dict(
            commit='c' * 40, dirty=False)
        self.sweep(tmux)
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE),
                         [('%2', 'the review has been idle for 30 minutes')])
        self.assertEqual(tmux.killed, ['%2'])

    def test_idle_reviewer_checks_pending_work_and_live_scope(self):
        head, next_head = 'a' * 40, 'b' * 40
        baseline = {'commit': head, 'dirty': False, 'mark': 'before'}
        clean = dict(baseline)
        cases = (
            ('new HEAD', {}, {}, dict(clean, commit=next_head), True, 1),
            ('new HEAD already requested', {'review_commit': next_head, 'awaiting': True}, {}, None, True, 0),
            ('new dirty tree', {}, {}, dict(clean, dirty=True), True, 1),
            ('edit in a dirty tree', {'approved_state': dict(baseline, dirty=True, mark='before', folder=None)}, {},
             dict(clean, dirty=True, mark='after'), True, 1),
            ('same HEAD and clean', {}, {}, clean, False, 2),
            ('paused cap with a review pending', {'awaiting': True}, {'paused': True}, None, False, 0),
            ('waits on Mak with a review pending', {'awaiting': True}, {'escalate': 'notice'}, None, False, 0),
            ('unreadable scope', {}, {}, OSError('git failed'), False, 2),
            ('unreadable mark', {}, {}, dict(clean, commit=next_head, mark=None), False, 2),
            ('review pending', {'seen_working': True}, {}, None, True, 0),
            ('review requested without verdict', {'request_id': '1234abcd'}, {}, None, True, 0),
            ('review stalled', {'stalled': '1234abcd'}, {}, None, True, 0),
            ('follow-up still open', {'approved_turn': True}, {}, None, True, 0),
            ('stale correction', {'stale_correction': {'commit': head}}, {}, None, True, 0),
            ('stale commit', {'stale_commit': ''}, {}, None, True, 0),
            ('legacy baseline', {'approved_state': None}, {}, dict(clean, commit=next_head), True, 1),
            ('no baseline', {'approved_state': None, 'review_commit': None}, {}, None, False, 0),
        )
        for name, changes, cap, live, keep, reads in cases:
            with self.subTest(name=name):
                self.closer = helper_close.Closer(now=lambda: self.clock[0], scope=self.scope)
                self.scope.reset_mock(return_value=True, side_effect=True)
                if isinstance(live, Exception):
                    self.scope.side_effect = live
                else:
                    self.scope.return_value = live
                self.links = {'%1': ['%2']}
                self.rows = [
                    dict(worker='%1', reviewer='%2', worker_thread=OWNER_THREAD, seen_working=False, awaiting=False,
                         approved_state=baseline, review_commit=head, review_clean=True) | changes,
                    dict(cap=True, worker='%1', worker_thread=OWNER_THREAD) | cap,
                ]
                tmux = Tmux({'%1': ('', ''), '%2': ('reviewer', record())})
                self.assertEqual(self.sweep(tmux), [])
                self.scope.assert_not_called()
                self.assertEqual(self.sweep(tmux, after=helper_close.IDLE + 1),
                                 [] if keep else [('%2', 'the review has been idle for 30 minutes')])
                self.assertEqual(tmux.killed, [] if keep else ['%2'])
                self.assertEqual(self.scope.call_args_list,
                                 # No saved folder: the worker entry resolves its folder like the review request.
                                 [call({'id': '%1', 'thread': OWNER_THREAD})] * reads)

    def test_non_helpers_and_discovery_misses_stay(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', 'not json'), '%3': ('advisor', record())})
        self.sweep(tmux)
        self.clock[0] += helper_close.IDLE * 2
        entries = [{'id': '%1', 'thread': OWNER_THREAD}, {'id': '%2'}]  # %3 missing from discovery
        self.assertEqual(self.closer.observe(entries, lambda entry: {'state': 'Idle'}, tmux, log=lambda text: None), [])
        self.assertEqual(tmux.killed, [])

    def test_sweeps_at_most_every_30_seconds_and_survives_tmux_failure(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record())})
        self.sweep(tmux)
        calls = []
        self.assertEqual(self.closer.observe([], lambda entry: {}, lambda *a, **k: calls.append(a), log=lambda text: None), [])
        self.assertEqual(calls, [])
        failing = lambda args, **kwargs: subprocess.CompletedProcess(args, 1, '', 'no server')
        self.clock[0] += helper_close.SWEEP
        self.assertEqual(self.closer.observe([], lambda entry: {}, failing, log=lambda text: None), [])

    def test_failed_kill_keeps_the_monitor_going(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record()), '%3': ('advisor', record())})
        self.sweep(tmux)
        real = tmux.__call__

        def run(args, **kwargs):
            if 'kill-pane' in args and args[-1] == '%2':
                raise subprocess.CalledProcessError(1, args)
            return real(args, **kwargs)
        self.clock[0] += helper_close.IDLE
        entries = self.entries(tmux)
        closed = self.closer.observe(entries, lambda entry: {'state': 'Idle'}, run, log=lambda text: None)
        self.assertEqual(closed, [('%3', 'the advisor has been idle for 30 minutes')])

    def test_unreadable_status_never_closes(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record(owner='%9'))})
        self.states['%2'] = 'Unavailable'
        self.sweep(tmux)
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE * 2), [])
        self.assertEqual(tmux.killed, [])

    def test_grace_starts_when_the_owner_goes_not_when_the_helper_went_quiet(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('', record())})
        self.sweep(tmux)
        self.sweep(tmux, after=600)
        del tmux.panes['%1']
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [])
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [])  # 30 s after the owner went
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [('%2', 'its owner tab is gone')])

    def test_message_sent_during_the_sweep_keeps_the_advisor(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record())})
        self.sweep(tmux)
        self.clock[0] += helper_close.IDLE

        def run(args, **kwargs):
            if 'display-message' in args:  # a reuse send landed after the sweep read the panes
                tmux.panes['%2'] = ('advisor', json.dumps({**json.loads(record()), 'sent_at': self.clock[0]}))
            return tmux(args, **kwargs)
        self.assertEqual(self.closer.observe(self.entries(tmux), lambda entry: {'state': 'Idle'}, run,
                                             log=lambda text: None), [])
        self.assertEqual(tmux.killed, [])

    def test_state_change_under_the_lock_keeps_the_advisor(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record())})
        self.sweep(tmux)
        self.clock[0] += helper_close.IDLE
        calls = []

        def status(entry):
            calls.append(entry['id'])
            return {'state': 'Working' if calls.count('%2') > 1 else 'Idle'}
        lock = __import__('threading').Lock()
        self.assertEqual(self.closer.observe(self.entries(tmux), status, tmux, log=lambda text: None, lock=lock), [])
        self.assertFalse(lock.locked())
        self.assertEqual(tmux.killed, [])

    def test_busy_send_lock_skips_the_close(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record())})
        self.sweep(tmux)
        self.clock[0] += helper_close.IDLE
        class Held:
            def acquire(self, timeout):
                return False
        self.assertEqual(self.closer.observe(self.entries(tmux), lambda entry: {'state': 'Idle'}, tmux,
                                             log=lambda text: None, lock=Held()), [])
        self.assertEqual(tmux.killed, [])

    def test_closing_a_reviewer_drops_its_links_as_worker_and_as_reviewer(self):
        self.links = {'%1': ['%2'], '%5': ['%6']}
        tmux = Tmux({'%2': ('reviewer', record())})
        helper_close.close_pane('%2', tmux)
        self.assertEqual(tmux.killed, ['%2'])
        helper_close.review_links.close.assert_called_once_with('%2')

    def test_fresh_status_under_the_lock_sees_a_composer_prompt(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('advisor', record())})
        self.sweep(tmux)
        self.clock[0] += helper_close.IDLE
        cached = lambda entry: {'state': 'Idle'}
        fresh = lambda entry: {'state': 'Working'}  # the user typed a prompt after the cached read
        self.assertEqual(self.closer.observe(self.entries(tmux), cached, tmux, log=lambda text: None, fresh=fresh), [])
        self.assertEqual(tmux.killed, [])
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [])  # the short request ended: the timer restarted
        self.assertEqual(self.sweep(tmux, after=helper_close.IDLE - helper_close.SWEEP),
                         [('%2', 'the advisor has been idle for 30 minutes')])

    def test_fresh_worker_status_under_the_lock_keeps_linked_reviewer(self):
        tmux = Tmux({'%1': ('', ''), '%2': ('reviewer', record())})
        self.links = {'%1': ['%2']}
        self.sweep(tmux)
        self.clock[0] += helper_close.IDLE
        lock = __import__('threading').Lock()

        def fresh(entry):
            self.assertTrue(lock.locked())
            return {'state': 'Working' if entry['id'] == '%1' else 'Idle'}

        cached = lambda entry: {'state': 'Idle'}
        self.assertEqual(self.closer.observe(self.entries(tmux), cached, tmux, log=lambda text: None,
                                             lock=lock, fresh=fresh), [])
        self.assertFalse(lock.locked())
        self.assertEqual(tmux.killed, [])

    def test_busy_orphan_gets_a_fresh_grace_when_it_goes_quiet(self):
        tmux = Tmux({'%2': ('', record(owner='%9'))})
        self.states['%2'] = 'Working'
        self.sweep(tmux)
        self.assertEqual(self.sweep(tmux, after=helper_close.GRACE), [])
        self.states.clear()
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [])  # 30 s since it was last seen busy
        self.assertEqual(self.sweep(tmux, after=helper_close.SWEEP), [('%2', 'its owner tab is gone')])


class MockPanes:
    def __init__(self, worker, reviewer):
        self.worker_id = worker["id"]
        self.reviewer_id = reviewer["id"]
        self.closed = []
        self.record = json.dumps({
            "owner_pane": self.worker_id, "owner_thread": worker["thread"],
            "helper_thread": reviewer["thread"], "sent_at": 1.0, "before": None,
        })

    def __call__(self, args, **kwargs):
        command = args[1]
        output = ""
        if command == "list-panes":
            output = f"{self.worker_id}\t\t\n{self.reviewer_id}\treviewer\t{self.record}\n"
        elif command == "display-message":
            pane = args[args.index("-t") + 1]
            record = self.record if pane == self.reviewer_id else ""
            output = f"{pane}\t{record}\n"
        elif command == "kill-pane":
            self.closed.append(args[args.index("-t") + 1])
        else:
            raise AssertionError(f"Unexpected tmux test command {command}")
        return subprocess.CompletedProcess(args, 0, output, "")


class ApprovalTurnCloseTests(ApprovalTurnTimingFixture):
    def test_reviewer_stays_open_if_worker_finishes_edit_during_idle_check(self):
        self.approve_current_work()
        self.worker_turn()
        self.assertEqual(self.send.call_count, 2)
        self.enterContext(patch.object(undo, "capture_safe"))
        self.enterContext(patch.object(undo, "record_tab_close"))
        self.enterContext(patch.object(helper_close.auto_resume, "forget_pane"))
        self.enterContext(patch.object(helper_close.groups, "closing", return_value=nullcontext()))

        def scope(entry):
            snapshot = dict(self.live)
            self.live.update(dirty=True, mark="new edit")
            return snapshot

        panes = MockPanes(self.worker, self.reviewer)
        closer = helper_close.Closer(now=lambda: self.now, scope=lambda entry: self.scope(entry))
        entries = [self.worker, self.reviewer]
        send_lock = Lock()

        def idle(entry):
            return {"state": "Idle"}

        closer.observe(entries, idle, panes, lock=send_lock, fresh=idle)
        self.now += helper_close.IDLE + 1
        self.scope = scope
        closer.observe(entries, idle, panes, lock=send_lock, fresh=idle)
        self.assertEqual(panes.closed, [])


if __name__ == '__main__':
    unittest.main()
