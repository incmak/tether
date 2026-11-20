"""The coordinator tab: commands and the status feed, with fake tmux options, statuses and sends."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
from functools import partial
import http.client
from http.server import HTTPServer
import io
import json
import multiprocessing
from queue import Queue
import threading
import time
import unittest
from unittest.mock import patch

import agent_coordinator_cli as cli
import coordinator
import paths


class Tmux:
    """`tmux set-option` on @tether_role, written back to the entries as session discovery reads it."""

    def __init__(self, world):
        self.world, self.fail = world, False

    def __call__(self, args, **kwargs):
        assert args[0] == paths.TMUX and kwargs == dict(capture_output=True, text=True, timeout=5)
        unset = args[1:4] == ['set-option', '-p', '-u']
        assert unset and args[4:] == ['-t', args[5], '@tether_role'] or args[1:4] == ['set-option', '-p', '-t'] and args[5] == '@tether_role', args
        entry = next(entry for entry in self.world.entries if entry['id'] == (args[5] if unset else args[4]))
        if not self.fail:
            entry.pop('coordinator', None)
            if not unset:
                entry['coordinator'] = args[6].partition('-')[2] or 'all'
        return type('Result', (), {'returncode': int(self.fail), 'stdout': ''})()


def tab(pane, session='$0', agent='claude', **extra):
    return dict({'id': pane, 'tmux_session': session, 'agent': agent, 'writable': True, 'owner_pid': 1,
                 'pane_name': 'tab ' + pane[1:]}, **extra)


class World:
    def __init__(self):
        self.entries = [tab('%1'), tab('%2'), tab('%3', agent='codex'), tab('%9', session='$1')]
        self.tmux = Tmux(self)
        self.states = {}
        self.replies = {'%2': 'Fixed the login test.\n\nNeeds you: no'}
        self.sent = []
        self.fail = None
        self.busy = False   # the fresh read under the send lock finds the coordinator at work
        self.now = 1000.0
        self.lock = threading.Lock()
        self.feed = coordinator.Feed(clock=lambda: self.now)
        self.deliver = partial(coordinator.deliver, lock=self.lock, fresh=lambda: self.fresh(),
                               fresh_status=self.fresh_status, last_reply=self.last_reply,
                               submit_locked=self.submit_locked)

    def fresh(self):
        return [entry.copy() for entry in self.entries]

    def marked(self):
        return {entry['id']: entry['coordinator'] for entry in self.entries if 'coordinator' in entry}

    def status(self, entry):
        state = self.states.get(entry['id'], 'Idle')
        return state if isinstance(state, dict) else {'state': state}

    def fresh_status(self, entry):
        assert self.lock.locked()
        if self.busy and entry['id'] == '%1':
            return {'state': 'Working'}
        return self.status(entry)

    def last_reply(self, entry):
        return self.replies.get(entry['id'], '')

    def send(self, entry, message, state=None):
        assert self.lock.locked()
        if self.fail:
            raise self.fail
        self.sent.append((entry['id'], message))
        return 'submitted'

    def submit_locked(self, message, entry):
        self.send(entry, message)

    def act(self, caller_pane, *, entries=None, **body):
        entries = self.fresh() if entries is None else entries
        caller = next(entry for entry in entries if entry['id'] == caller_pane)
        return coordinator.act(body, caller, entries, fresh=self.fresh, lock=self.lock, run=self.tmux,
                               status=self.status, fresh_status=self.fresh_status, send=self.send, last_reply=self.last_reply,
                               timeline=lambda pane: {'finished': None})[1]

    def tick(self, seconds=2, *, entries=None):
        self.now += seconds
        self.feed.observe(self.fresh() if entries is None else entries, self.status, self.deliver, self.last_reply)

    def run_turn(self, pane, work=10):
        """One turn of `pane`: it works, then it is idle long enough to count."""
        self.states[pane] = 'Working'
        self.tick()
        self.states[pane] = 'Idle'
        self.tick(work)
        self.tick(coordinator.SETTLE)


class Commands(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_claim_marks_the_tab_and_lists_only_its_session(self):
        result = self.world.act('%1', action='claim')
        self.assertEqual(self.world.marked(), {'%1': 'all'})
        self.assertEqual([row['pane'] for row in result['tabs']], ['%2', '%3'])
        self.assertIn('You are the coordinator', result['rules'])
        self.assertEqual(result['tabs'][0]['last_reply'], 'Fixed the login test. Needs you: no')

    def test_a_second_tab_needs_take(self):
        self.world.act('%1', action='claim')
        with self.assertRaisesRegex(ValueError, 'is the coordinator of this session'):
            self.world.act('%2', action='claim')
        self.world.act('%2', action='claim', take=True)
        self.assertEqual(self.world.marked(), {'%2': 'all'})

    def test_concurrent_claims_with_unmarked_snapshots_have_one_winner(self):
        start = threading.Barrier(2, timeout=2)
        results = Queue()

        def claim(pane):
            try:
                snapshot = self.world.fresh()
                start.wait()
                self.world.act(pane, entries=snapshot, action='claim')
                results.put((pane, None))
            except Exception as exc:
                results.put((pane, exc))

        write = self.world.tmux

        def slow_write(*args, **kwargs):
            time.sleep(0.02)
            return write(*args, **kwargs)

        with patch.object(self.world, 'tmux', slow_write):
            threads = [threading.Thread(target=claim, args=(pane,), daemon=True) for pane in ('%1', '%2')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive(), 'claim did not finish')
        outcomes = [results.get_nowait() for _ in threads]
        winners = [pane for pane, error in outcomes if error is None]
        errors = [error for _, error in outcomes if error is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIn('is the coordinator of this session', str(errors[0]))
        self.assertEqual(self.world.marked(), {winners[0]: 'all'})

    def test_another_session_has_its_own_coordinator(self):
        self.world.act('%1', action='claim')
        self.world.act('%9', action='claim')
        self.assertEqual(self.world.marked(), {'%1': 'all', '%9': 'all'})

    def test_a_closed_coordinator_does_not_block_a_claim(self):
        self.world.act('%1', action='claim')
        self.world.entries = [entry for entry in self.world.entries if entry['id'] != '%1']
        self.world.act('%2', action='claim')

    def test_commands_need_the_role(self):
        for action in ('tabs', 'release', 'events', 'send', 'reply'):
            with self.assertRaisesRegex(ValueError, 'not the coordinator'):
                self.world.act('%2', action=action, pane='%3', message='x', events='off')

    def test_send_reaches_a_tab_of_the_session_only(self):
        self.world.act('%1', action='claim')
        self.assertEqual(self.world.act('%1', action='send', pane='%3', message='Also fix the logout test.')['sent'], True)
        self.assertEqual(self.world.sent, [('%3', 'Also fix the logout test.')])
        for pane, error in (('%9', 'not an agent tab of this session'), ('%1', 'this tab'), ('nope', 'Select a tab')):
            with self.assertRaisesRegex(ValueError, error):
                self.world.act('%1', action='send', pane=pane, message='x')
        with self.assertRaisesRegex(ValueError, 'control characters'):
            self.world.act('%1', action='send', pane='%2', message='a\x1bb')

    def test_send_refuses_a_tab_that_waits_on_a_prompt(self):
        self.world.act('%1', action='claim')
        for state in ('Approval', 'Input needed'):
            self.world.states['%2'] = state
            with self.assertRaisesRegex(RuntimeError, 'waits on a prompt'):
                self.world.act('%1', action='send', pane='%2', message='go')
        self.assertEqual(self.world.sent, [])

    def test_send_refuses_a_target_that_moves_after_validation(self):
        for change in ({'tmux_session': '$1'}, {'transcript': 'new-chat.jsonl'}):
            with self.subTest(change=change):
                world = World()
                world.act('%1', action='claim')
                status = world.status

                def move_after_status(entry):
                    info = status(entry)
                    world.entries[1].update(change)
                    return info

                with patch.object(world, 'status', move_after_status), self.assertRaisesRegex(ValueError, 'Run tabs again'):
                    world.act('%1', action='send', pane='%2', message='go')
                self.assertEqual(world.sent, [])

    def test_send_rechecks_the_caller_mark_chat_and_target_state(self):
        for check in ('caller mark', 'caller chat', 'Approval', 'Input needed', 'Unavailable', 'Unknown'):
            with self.subTest(check=check):
                world = World()
                world.act('%1', action='claim')
                snapshot = world.fresh()
                if check == 'caller mark':
                    world.entries[0].pop('coordinator')
                elif check == 'caller chat':
                    world.entries[0]['transcript'] = 'new-chat.jsonl'
                else:
                    world.fresh_status = lambda entry: {'state': check}
                with self.assertRaises(RuntimeError):
                    world.act('%1', entries=snapshot, action='send', pane='%2', message='go')
                self.assertEqual(world.sent, [])

    def test_mark_writes_require_a_current_caller_and_role(self):
        world = self.world
        snapshot = world.fresh()
        world.entries[0]['transcript'] = 'new-chat.jsonl'
        with self.assertRaisesRegex(RuntimeError, 'started a new chat'):
            world.act('%1', entries=snapshot, action='claim')
        world.act('%1', action='claim')
        snapshot = world.fresh()
        world.act('%1', action='release')
        for action in ('release', 'events'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'not the coordinator'):
                world.act('%1', entries=snapshot, action=action, events='off')
        self.assertEqual(world.marked(), {})

    def test_row_uses_wall_clock_to_measure_time_since_finish(self):
        for seconds in (30, 120):
            with self.subTest(seconds=seconds):
                row = coordinator.row(self.world.entries[1], self.world.status, self.world.last_reply,
                                      finished=time.time() - seconds)
                self.assertAlmostEqual(row['finished_seconds_ago'], seconds, delta=1)

    def test_tabs_shows_what_a_tab_waits_on(self):
        self.world.act('%1', action='claim')
        self.world.states['%2'] = {'state': 'Approval', 'approval': {'id': 'a1', 'text': 'Bash command\nrm -r build'}}
        self.world.replies['%3'] = 'Which branch?\n\nNeeds you: yes'
        rows = {row['pane']: row for row in self.world.act('%1', action='tabs')['tabs']}
        self.assertEqual(rows['%2']['waits_on'], 'approval: Bash command · rm -r build')
        self.assertEqual(rows['%3']['waits_on'], 'its last reply asks the user')

    def test_reply_is_bounded_and_events_and_release_change_the_mark(self):
        self.world.act('%1', action='claim', events='needs')
        self.world.replies['%2'] = 'x' * (coordinator.MAX_REPLY + 5)
        result = self.world.act('%1', action='reply', pane='%2')
        self.assertEqual((len(result['reply']), result['truncated']), (coordinator.MAX_REPLY, True))
        self.assertEqual(self.world.act('%1', action='tabs')['events'], 'needs')
        self.world.act('%1', action='events', events='off')
        self.assertEqual(self.world.marked(), {'%1': 'off'})
        with self.assertRaisesRegex(ValueError, 'all, needs or off'):
            self.world.act('%1', action='events', events='loud')
        self.world.act('%1', action='release')
        self.assertEqual(self.world.marked(), {})

    def test_a_broken_mark_or_a_shell_is_no_coordinator(self):
        self.world.entries[0]['coordinator'] = 'loud'
        self.world.entries[1].update(coordinator='all', agent='shell')
        self.assertEqual(coordinator.marks(self.world.entries), {})

    def test_a_helper_cannot_claim_and_a_tmux_failure_is_reported(self):
        self.world.entries[1]['helper'] = 'reviewer'
        with self.assertRaisesRegex(ValueError, 'reviewer or advisor'):
            self.world.act('%2', action='claim')
        self.world.tmux.fail = True
        with self.assertRaisesRegex(RuntimeError, 'Could not change the coordinator mark'):
            self.world.act('%1', action='claim')
        self.assertEqual(self.world.marked(), {})


class Feed(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.act('%1', action='claim')
        self.world.tick()

    def wait_for_send(self):
        self.world.tick(coordinator.JOIN)
        self.world.tick(coordinator.GAP)

    def test_a_finished_turn_reaches_the_coordinator_once(self):
        self.world.run_turn('%2')
        self.assertEqual(self.world.sent, [])   # more updates may follow
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)
        pane, text = self.world.sent[0]
        self.assertEqual(pane, '%1')
        self.assertTrue(text.startswith(coordinator.MARK + ' 1 tab update.'))
        self.assertIn('- `tab 2` (%2) finished: Fixed the login test. Needs you: no', text)
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)

    def test_a_codex_tab_and_a_claude_tab_join_in_one_message(self):
        self.world.replies['%3'] = 'Which branch?\n\nNeeds you: yes'
        self.world.run_turn('%2')
        self.world.run_turn('%3')
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)
        self.assertIn('2 tab updates', self.world.sent[0][1])
        self.assertIn('`tab 3` (%3) finished and asks the user: Which branch?', self.world.sent[0][1])

    def test_a_short_turn_and_another_session_give_no_update(self):
        self.world.states['%2'] = 'Working'
        self.world.tick()
        self.world.states['%2'] = 'Idle'
        self.world.tick(2)
        self.world.run_turn('%9')
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])

    def test_helpers_report_to_their_owner_not_to_the_coordinator(self):
        self.world.entries[1]['helper'] = 'reviewer'
        self.world.entries[2]['role'] = 'advisor'
        self.world.run_turn('%2')
        self.world.run_turn('%3')
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])

    def test_a_new_prompt_is_an_update_but_one_that_was_there_is_not(self):
        self.world.states['%3'] = {'state': 'Input needed', 'attention': {'id': 'q0', 'question': 'Old question?'}}
        self.world.entries.append(tab('%4'))
        self.world.states['%4'] = {'state': 'Approval', 'approval': {'id': 'a0', 'text': 'Bash command\nls'}}
        self.world.tick()   # first sight of %4: its prompt is already in the tabs list
        self.world.states['%2'] = {'state': 'Approval', 'approval': {'id': 'a1', 'text': 'Bash command\nrm -r build'}}
        self.world.tick()
        self.world.tick(coordinator.SETTLE)
        self.wait_for_send()
        text = self.world.sent[0][1]
        self.assertIn('`tab 2` (%2) waits on the user: approval: Bash command · rm -r build', text)
        self.assertIn('`tab 3` (%3) waits on the user: question: Old question?', text)   # new since the claim
        self.assertNotIn('%4', text)

    def test_needs_mode_drops_finished_turns_and_off_drops_all(self):
        self.world.act('%1', action='events', events='needs')
        self.world.run_turn('%2')
        self.world.replies['%3'] = 'Which branch?\n\nNeeds you: yes'
        self.world.run_turn('%3')
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)
        self.assertNotIn('%2', self.world.sent[0][1])
        self.assertIn('%3', self.world.sent[0][1])
        self.world.act('%1', action='events', events='off')
        self.world.run_turn('%3')
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)

    def test_the_update_waits_while_the_coordinator_works(self):
        self.world.run_turn('%2')
        self.world.states['%1'] = 'Working'
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])
        self.world.states['%1'] = 'Idle'
        self.world.tick()
        self.assertEqual(len(self.world.sent), 1)

    def test_a_failed_send_keeps_the_update_and_tries_again_later(self):
        self.world.run_turn('%2')
        self.world.fail = RuntimeError('The input box holds a draft.')
        self.wait_for_send()
        self.world.fail = None
        self.world.tick()
        self.assertEqual(self.world.sent, [])   # not before the retry time
        self.world.tick(coordinator.RETRY)
        self.assertEqual(len(self.world.sent), 1)

    def test_a_tab_that_works_again_or_answered_prompt_is_not_reported(self):
        self.world.run_turn('%2')
        self.world.states['%3'] = {'state': 'Approval', 'approval': {'id': 'a1', 'text': 'Bash command\nls'}}
        self.world.tick()
        self.world.tick(coordinator.SETTLE)
        self.world.states['%2'] = 'Working'
        self.world.states['%3'] = 'Working'
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])

    def test_release_stops_the_feed(self):
        self.world.act('%1', action='release')
        self.world.run_turn('%2')
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])

    def test_a_closed_coordinator_drops_its_updates(self):
        self.world.run_turn('%2')
        self.world.entries = [entry for entry in self.world.entries if entry['id'] != '%1']
        self.wait_for_send()
        self.assertEqual((self.world.sent, self.world.feed.queues), ([], {}))


    # Delivery checks everything again: a check made when the update was queued may be out of date.
    def test_a_changed_report_setting_applies_to_queued_updates(self):
        for mode in ('off', 'needs'):
            with self.subTest(mode=mode):
                self.world.act('%1', action='events', events='all')
                self.world.run_turn('%2')
                self.world.act('%1', action='events', events=mode)
                self.wait_for_send()
                self.assertEqual(self.world.sent, [])

    def test_a_coordinator_that_moves_after_the_snapshot_gets_no_report(self):
        for change in ({'tmux_session': '$1'}, {'transcript': 'new-chat.jsonl'}):
            with self.subTest(change=change):
                world = World()
                world.act('%1', action='claim')
                world.run_turn('%2')
                snapshot = world.fresh()
                world.entries[0].update(change)
                world.tick(coordinator.JOIN, entries=snapshot)
                self.assertEqual((world.sent, world.feed.queues), ([], {}))

    def test_a_coordinator_that_releases_or_turns_reports_off_after_the_snapshot_gets_no_report(self):
        for change in ({'action': 'release'}, {'action': 'events', 'events': 'off'}):
            with self.subTest(change=change):
                world = World()
                world.act('%1', action='claim')
                world.run_turn('%2')
                snapshot = world.fresh()
                world.act('%1', **change)
                world.tick(coordinator.JOIN, entries=snapshot)
                self.assertEqual(world.sent, [])
                self.assertFalse(any(queue['events'] for queue in world.feed.queues.values()))

    def test_needs_mode_set_after_the_snapshot_sends_only_the_request(self):
        self.world.run_turn('%2')
        self.world.replies['%3'] = 'Which branch?\n\nNeeds you: yes'
        self.world.run_turn('%3')
        snapshot = self.world.fresh()
        self.world.act('%1', action='events', events='needs')
        self.world.tick(coordinator.JOIN, entries=snapshot)
        self.assertEqual(len(self.world.sent), 1)
        self.assertIn('Reports are set to "needs"', self.world.sent[0][1])
        self.assertIn('%3', self.world.sent[0][1])
        self.assertNotIn('%2', self.world.sent[0][1])

    def test_working_clears_a_finished_event_even_before_the_send_window(self):
        self.world.run_turn('%2')
        queue = self.world.feed.queues['%1']
        self.assertEqual(len(queue['events']), 1)
        self.world.states['%2'] = 'Working'
        self.world.tick(1)
        self.assertEqual(queue['events'], [])
        self.world.states['%2'] = 'Idle'
        self.world.tick(1)  # this turn is too short for its own report
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])

    def test_changed_reply_drops_a_completion_even_outside_the_summary(self):
        for needs_you in ('no', 'yes'):
            with self.subTest(needs_you=needs_you):
                world = World()
                world.act('%1', action='claim')
                prefix = 'Same first part of the reply. ' * coordinator.SUMMARY
                world.replies['%2'] = f'{prefix}First turn.\n\nNeeds you: {needs_you}'
                world.run_turn('%2')
                event = world.feed.queues['%1']['events'][0]
                self.assertEqual(event['kind'], 'asks' if needs_you == 'yes' else 'done')
                self.assertNotIn('First turn', event['text'])
                world.replies['%2'] = f'{prefix}Second turn.\n\nNeeds you: {needs_you}'
                world.tick(coordinator.JOIN)  # both snapshots see Idle
                self.assertEqual(world.sent, [])
                self.assertEqual(world.feed.queues['%1']['events'], [])

    def test_a_prompt_that_changed_is_not_reported_as_the_old_one(self):
        self.world.states['%2'] = {'state': 'Approval', 'approval': {'id': 'a1', 'text': 'Bash command\nrm -r build'}}
        self.world.tick()
        self.world.tick(coordinator.SETTLE)     # approval a1 is queued
        self.world.tick(coordinator.JOIN - 3)
        self.world.states['%2'] = {'state': 'Input needed', 'attention': {'id': 'q1', 'question': 'Which branch?'}}
        self.world.tick(3)                      # the flush time, before question q1 settles
        self.assertEqual(self.world.sent, [])
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)
        self.assertNotIn('rm -r build', self.world.sent[0][1])
        self.assertIn('Which branch?', self.world.sent[0][1])

    def approval(self, pane, command):
        """A Codex or fallback approval: it has no id, so only its content tells two prompts apart."""
        self.world.states[pane] = {'state': 'Approval', 'approval': {'id': None, 'text': 'Bash command\n' + command}}

    def test_a_prompt_without_an_id_that_changed_is_not_reported_as_the_old_one(self):
        self.approval('%2', 'OLD command')
        self.world.tick()
        self.world.tick(coordinator.SETTLE)     # OLD is queued
        self.world.tick(coordinator.JOIN - 3)
        self.approval('%2', 'NEW command')
        self.world.tick(3)                      # the flush time, before NEW settles
        self.assertEqual(self.world.sent, [])
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)
        self.assertNotIn('OLD command', self.world.sent[0][1])
        self.assertIn('NEW command', self.world.sent[0][1])

    def test_a_second_prompt_without_an_id_is_reported_and_the_same_one_is_not(self):
        self.approval('%2', 'OLD command')
        self.world.tick()
        self.world.tick(coordinator.SETTLE)
        self.wait_for_send()
        self.assertIn('OLD command', self.world.sent[-1][1])
        for command, count in (('OLD command', 1), ('NEW command', 2)):
            self.world.states['%2'] = 'Unknown'
            self.world.tick()
            self.approval('%2', command)
            self.world.tick()
            self.world.tick(coordinator.SETTLE)
            self.wait_for_send()
            self.assertEqual(len(self.world.sent), count)
        self.assertIn('NEW command', self.world.sent[-1][1])

    def test_a_prompt_that_only_wraps_again_gives_one_report(self):
        self.approval('%2', 'ls -a')
        self.world.tick()
        self.world.tick(coordinator.SETTLE)
        self.wait_for_send()
        self.world.states['%2']['approval']['text'] = 'Bash command ls\n-a'   # the pane is wider or narrower now
        self.world.tick()
        self.world.tick(coordinator.SETTLE)
        self.wait_for_send()
        self.assertEqual(len(self.world.sent), 1)

    def test_prompt_identity_uses_the_id_else_the_kind_and_content(self):
        identity = coordinator.prompt_identity
        self.assertIsNone(identity({'state': 'Idle'}))
        self.assertEqual(identity({'state': 'Approval', 'approval': {'id': 'a1', 'text': 'x'}}),
                         identity({'state': 'Approval', 'approval': {'id': 'a1', 'text': 'y'}}))
        same = {identity({'state': 'Approval', 'approval': {'id': None, 'text': text, 'options': [text]}})
                for text in ('Bash command ls  -a', 'Bash command\nls -a', 'Bash command\tls -a', ' Bash command\r\n ls -a\n')}
        self.assertEqual(len(same), 1)
        self.assertNotEqual(identity({'state': 'Approval', 'approval': {'id': None, 'text': 'ls'}}),
                            identity({'state': 'Approval', 'approval': {'id': None, 'text': 'rm'}}))
        self.assertNotEqual(identity({'state': 'Approval', 'approval': {'text': 'ls'}}),
                            identity({'state': 'Input needed', 'attention': {'text': 'ls'}}))

    def test_a_tab_of_unknown_state_keeps_its_update(self):
        self.world.run_turn('%2')
        for state in ('Unknown', 'Unavailable'):
            self.world.states['%2'] = state
            self.wait_for_send()
            self.assertEqual(self.world.sent, [])
        self.world.states['%2'] = 'Idle'
        self.world.tick()
        self.assertEqual(len(self.world.sent), 1)
        self.assertIn('%2', self.world.sent[0][1])

    def test_a_coordinator_that_moves_or_starts_a_new_chat_drops_its_updates(self):
        for change in ({'tmux_session': '$1'}, {'transcript': 'new-chat.jsonl'}):
            with self.subTest(change=change):
                world = self.world = World()
                world.act('%1', action='claim')
                world.tick()
                world.run_turn('%2')
                world.entries[0].update(change)
                world.entries.append(tab('%4', coordinator='all'))  # a new coordinator in session $0
                self.wait_for_send()
                self.assertEqual([pane for pane, _ in world.sent], [])

    def test_a_tab_that_moves_or_starts_a_new_chat_is_not_reported(self):
        for change in ({'tmux_session': '$1'}, {'transcript': 'new-chat.jsonl'}, {'helper': 'reviewer'}):
            with self.subTest(change=change):
                world = self.world = World()
                world.act('%1', action='claim')
                world.tick()
                world.run_turn('%2')
                world.entries[1].update(change)
                self.wait_for_send()
                self.assertEqual(world.sent, [])

    def test_a_coordinator_found_busy_under_the_send_lock_gets_the_update_later(self):
        self.world.run_turn('%2')
        self.world.busy = True
        self.wait_for_send()
        self.assertEqual(self.world.sent, [])
        self.world.busy = False
        self.world.tick()
        self.assertEqual(len(self.world.sent), 1)


class ServerSend(unittest.TestCase):
    """Coordinator sends use the server lock and keep terminal drafts in place."""
    entry = tab('%1', coordinator='all')
    source = tab('%2')

    def setUp(self):
        import server
        import terminal_input
        self.server, self.terminal = server, terminal_input
        self.stash = self.enterContext(patch.object(server, 'stash_quiet_draft'))
        self.enterContext(patch.object(server.continuations, 'prepare', side_effect=lambda entry, text: (text, None)))

    def fresh(self, state):
        def read(entry):
            self.assertTrue(self.server.SEND_LOCK.locked())
            return {'state': state} if entry == self.entry else {'state': 'Approval', 'approval': {'id': 'a1'}}
        return patch.object(self.server, 'fresh_status', side_effect=read)

    def deliver(self):
        event = {'pane': '%2', 'session': '$0', 'chat': None, 'kind': 'needs', 'text': 'update',
                 'name': 'tab 2', 'token': ('approval', 'id', 'a1')}
        return coordinator.deliver('%1', {'session': '$0', 'chat': None}, [event], lock=self.server.SEND_LOCK,
                                   fresh=lambda: [self.entry, self.source], fresh_status=self.server.fresh_status,
                                   last_reply=self.server.last_reply, submit_locked=self.server.submit_locked)

    def test_a_draft_refuses_the_send_and_is_not_moved(self):
        with self.fresh('Idle'), patch.object(self.server, 'terminal_submit',
                                               side_effect=self.terminal.DraftError('draft')) as submit:
            with self.assertRaises(self.terminal.DraftError):
                self.deliver()
        submit.assert_called_once()
        self.stash.assert_not_called()
        self.assertFalse(self.server.SEND_LOCK.locked())

    def test_only_an_idle_coordinator_gets_the_update(self):
        for state, sent in (('Working', False), ('Approval', False), ('Unknown', False), ('Idle', True)):
            with self.subTest(state=state), self.fresh(state), \
                    patch.object(self.server, 'terminal_submit') as submit:
                self.assertEqual(self.deliver() is not None, sent)
                self.assertEqual(submit.called, sent)
        self.assertFalse(self.server.SEND_LOCK.locked())

    def test_send_text_uses_the_lock_the_caller_already_holds(self):
        results = Queue()
        lock = threading.Lock()

        def send():
            try:
                with self.server.SEND_LOCK:
                    results.put(self.server.send_text(self.entry, '!status', 'Idle', held=True))
            except Exception as exc:
                results.put(exc)

        with patch.object(self.server, 'SEND_LOCK', lock), patch.object(self.server, 'terminal_submit') as terminal:
            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'send_text tried to acquire SEND_LOCK again')
            self.assertEqual(results.get_nowait(), 'submitted')
        terminal.assert_called_once_with(self.entry, '!status')


def post_coordinator_send(result, moved):
    """Use a child process so a deadlock cannot leave the suite's SEND_LOCK held."""
    import server

    caller = tab('%1', coordinator='all', transcript='caller.jsonl')
    target = tab('%2', transcript='target.jsonl')
    fresh = [caller, dict(target, tmux_session='$1') if moved else target]
    with result, HTTPServer(('127.0.0.1', 0), server.Handler) as httpd, \
            patch.object(server.auth, 'token', return_value='test-token'), \
            patch.object(server, 'sessions', return_value=[caller, target]), \
            patch.object(server, 'fresh_sessions', return_value=fresh), \
            patch.object(server, 'shared_status', return_value={'state': 'Idle'}), \
            patch.object(server, 'fresh_status', return_value={'state': 'Idle'}), \
            patch.object(server, 'terminal_submit') as terminal:
        httpd.timeout = 3
        worker = threading.Thread(target=httpd.handle_request, daemon=True)
        worker.start()
        host, port = httpd.server_address
        credential = server.auth.session_value()
        client = http.client.HTTPConnection(host, port, timeout=2)
        try:
            client.request('POST', coordinator.PATH,
                           json.dumps({'action': 'send', 'session': '%1', 'pane': '%2', 'message': '!status'}),
                           {'Origin': f'http://{host}:{port}', 'X-Codex-Mobile': '1',
                            'Cookie': f'{server.auth.COOKIE}={credential}', 'X-Tether-Hook': credential})
            response = client.getresponse()
            result.send((response.status, json.loads(response.read()), [call.args for call in terminal.call_args_list],
                         server.SEND_LOCK.locked()))
        except TimeoutError:
            result.send(None)
        finally:
            client.close()


class SendRouteTests(unittest.TestCase):
    def post_send(self, *, moved=False):
        context = multiprocessing.get_context('spawn')
        reader, writer = context.Pipe(duplex=False)
        with reader, writer:
            child = context.Process(target=post_coordinator_send, args=(writer, moved))
            child.start()
            writer.close()
            try:
                self.assertTrue(reader.poll(6), 'Coordinator HTTP test process did not respond')
                result = reader.recv()
                self.assertIsNotNone(result, 'Coordinator HTTP request timed out; SEND_LOCK may be held twice')
                return result
            finally:
                child.join(timeout=1)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=1)

    def test_send_submits_to_the_target_and_releases_the_server_lock(self):
        status, body, calls, locked = self.post_send()
        self.assertEqual(status, 200, body)
        self.assertEqual((body['pane'], body['delivery']), ('%2', 'submitted'))
        self.assertEqual(calls, [(tab('%2', transcript='target.jsonl'), '!status')])
        self.assertFalse(locked)

    def test_send_refuses_a_target_that_moved_to_another_session(self):
        status, body, calls, locked = self.post_send(moved=True)
        self.assertIn(status, (400, 409), body)
        self.assertEqual(body['error'], 'That tab moved or started a new chat. Run tabs again.')
        self.assertEqual(calls, [])
        self.assertFalse(locked)


class Cli(unittest.TestCase):
    def test_arguments(self):
        self.assertEqual(cli.arguments(['claim']), {'action': 'claim'})
        self.assertEqual(cli.arguments(['claim', '--events', 'needs', '--take']),
                         {'action': 'claim', 'events': 'needs', 'take': True})
        self.assertEqual(cli.arguments(['events', 'off']), {'action': 'events', 'events': 'off'})
        self.assertEqual(cli.arguments(['send', '--pane', '%2', 'go']), {'action': 'send', 'pane': '%2', 'message': 'go'})
        with patch('sys.stdin', io.StringIO('from stdin')):
            self.assertEqual(cli.arguments(['send', '--pane', '%2', '-'])['message'], 'from stdin')
        for bad in (['send', '--pane', '%2', ' '], ['events', 'loud'], []):
            with self.assertRaises(ValueError):
                cli.arguments(bad)

    def test_outside_a_tab_and_show(self):
        err = io.StringIO()
        with patch.dict('os.environ', {}, clear=True), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['tabs']), 1)
        self.assertIn('inside a Tether tab', err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.show({'rules': 'RULES', 'tabs': []})
        self.assertTrue(out.getvalue().startswith('RULES\n'))
        self.assertNotIn('"rules"', out.getvalue())


if __name__ == '__main__':
    unittest.main()
