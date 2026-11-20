"""The coordinator tab: commands and the status feed, with fake tmux options, statuses and sends."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
from functools import partial
import http.client
from http.server import HTTPServer
import io
import json
import multiprocessing
import os
from pathlib import Path
from queue import Queue
import tempfile
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
                 'pane_name': 'tab ' + pane[1:], 'transcript': 'tab-' + pane[1:] + '.jsonl'}, **extra)


class World:
    def __init__(self):
        self.entries = [tab('%1'), tab('%2'), tab('%3', agent='codex'), tab('%9', session='$1')]
        self.tmux = Tmux(self)
        self.states = {}
        self.replies = {'%2': 'Fixed the login test.\n\nNeeds you: no'}
        self.turns = {}
        self.outcomes = {}
        self.sent = []
        self.fail = None
        self.busy = False   # the fresh read under the send lock finds the coordinator at work
        self.now = 1000.0
        self.lock = threading.Lock()
        self.feed = coordinator.Feed(clock=lambda: self.now)
        self.deliver = partial(coordinator.deliver, lock=self.lock, fresh=lambda: self.fresh(),
                               fresh_status=self.fresh_status, latest_outcome=lambda entry: self.delivery_outcome(entry),
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

    def outcome(self, entry):
        pane = entry['id']
        if pane in self.outcomes:
            return self.outcomes[pane]
        text = self.last_reply(entry)
        return {'kind': 'asks' if 'Needs you: yes' in text else 'done',
                'text': ' '.join(text.split()), 'identity': str(self.turns.get(pane, 0))}

    def delivery_outcome(self, entry):
        return self.outcome(entry)

    def change_role(self, pane='%2', thread='thread-2', mode='all', **extra):
        def fresh():
            assert self.lock.locked()
            return self.fresh()
        return coordinator.browser_role(dict(pane=pane, thread=thread, mode=mode, **extra),
                                        fresh=fresh, lock=self.lock, run=self.tmux,
                                        status=self.fresh_status, send=self.send)

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
        self.feed.observe(self.fresh() if entries is None else entries, self.status, self.deliver, self.outcome)

    def run_turn(self, pane, work=10):
        """One turn of `pane`: it works, then it is idle long enough to count."""
        self.states[pane] = 'Working'
        self.tick()
        self.turns[pane] = self.turns.get(pane, 0) + 1
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


class BrowserRole(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.entries[1]['thread'] = 'thread-2'

    def test_new_claim_sends_rules_and_take_reports_the_replaced_pane(self):
        for take in (False, True):
            with self.subTest(take=take):
                if take:
                    self.world.act('%2', action='release')
                    self.world.act('%1', action='claim', events='off')
                self.assertEqual(self.world.change_role(mode='needs', take=take),
                                 {'coordinator': '%2', 'events': 'needs', 'sent': True,
                                  'replaced': '%1' if take else None})
                self.assertEqual(self.world.marked(), {'%2': 'needs'})
                self.assertEqual(self.world.sent[-1], ('%2',
                    '[Tether] The user made this tab the coordinator of this session in Tether.\n' + coordinator.rules()))
        self.assertEqual(len(self.world.sent), 2)

    def test_a_send_failure_restores_each_previous_mark(self):
        for mode in ('off', None):
            with self.subTest(previous_mode=mode):
                if mode:
                    self.world.act('%1', action='claim', events=mode)
                else:
                    self.world.act('%1', action='release')
                self.world.fail = RuntimeError('Terminal draft')
                with self.assertRaisesRegex(RuntimeError, 'Terminal draft'):
                    self.world.change_role(take=True)
                self.assertEqual(self.world.marked(), {'%1': mode} if mode else {})
                self.assertFalse(self.world.lock.locked())

    def test_take_has_to_be_explicit_and_the_error_names_the_tab(self):
        self.world.act('%1', action='claim')
        for agent in ('claude', 'shell'):
            self.world.entries[0]['agent'] = agent
            for take in (False, 1):
                with self.subTest(agent=agent, take=take), self.assertRaisesRegex(
                        ValueError, r'`tab 1` \(%1\) is the coordinator'):
                    self.world.change_role(take=take)
        self.assertEqual(self.world.marked(), {'%1': 'all'})
        self.assertEqual(self.world.sent, [])

    def test_stale_and_missing_threads_are_refused(self):
        for thread in ('old-thread', None):
            with self.subTest(thread=thread), self.assertRaisesRegex(ValueError, 'That tab started a new chat. Try again.'):
                self.world.change_role(thread=thread)
        self.assertEqual(self.world.marked(), {})
        self.assertEqual(self.world.sent, [])

    def test_reviewer_and_advisor_tabs_are_refused(self):
        for role in ({'helper': 'reviewer'}, {'role': 'advisor'}):
            with self.subTest(role=role):
                with patch.dict(self.world.entries[1], role), self.assertRaisesRegex(ValueError, 'reviewer or advisor'):
                    self.world.change_role(take=True)
        self.assertEqual(self.world.marked(), {})

    def test_mode_change_and_release_send_nothing(self):
        self.world.act('%2', action='claim')
        for mode in ('off', 'needs', None):
            with self.subTest(mode=mode):
                self.assertEqual(self.world.change_role(mode=mode),
                                 {'coordinator': '%2' if mode else None, 'events': mode,
                                  'sent': False, 'replaced': None})
                self.assertEqual(self.world.marked(), {'%2': mode} if mode else {})
        self.assertEqual(self.world.sent, [])

    def test_claim_refuses_prompts_unavailable_and_unknown_states(self):
        for state in ('Approval', 'Input needed', 'Unavailable', 'Unknown', None):
            with self.subTest(state=state):
                self.world.states['%2'] = state
                with self.assertRaises(RuntimeError):
                    self.world.change_role()
                self.assertEqual(self.world.marked(), {})
        self.assertEqual(self.world.sent, [])

    def test_refused_take_preserves_both_marks_and_the_old_queue(self):
        for state, writable in (('Approval', True), ('Input needed', True), ('Unknown', True), ('Idle', False)):
            with self.subTest(state=state, writable=writable):
                world = World()
                world.entries[1].update(thread='thread-2', writable=writable)
                world.act('%1', action='claim')
                world.run_turn('%3')
                queue = world.feed.queues['%1']
                queued = queue['events'].copy()
                self.assertEqual(len(queued), 1)
                world.states['%2'] = state
                tmux = world.tmux

                def observe_after_mark(*args, **kwargs):
                    result = tmux(*args, **kwargs)
                    world.tick(0)
                    return result

                with patch.object(world, 'tmux', side_effect=observe_after_mark), self.assertRaises(RuntimeError):
                    world.change_role(take=True)
                self.assertEqual(world.marked(), {'%1': 'all'})
                self.assertEqual(world.feed.queues['%1']['events'], queued)
                self.assertEqual(world.sent, [])

    def test_a_working_tab_can_be_claimed_and_gets_the_rules_held(self):
        # A held send waits for the turn to end, like the coordinator's own messages to a working tab.
        for state in ('Working', 'Idle'):
            with self.subTest(state=state):
                world = World()
                world.entries[1]['thread'] = 'thread-2'
                world.states['%2'] = state
                self.assertTrue(world.change_role(mode='all')['sent'])
                self.assertEqual(world.marked(), {'%2': 'all'})
                self.assertEqual(len(world.sent), 1)

    def test_claim_rejects_invalid_modes_and_unsupported_panes(self):
        with self.assertRaisesRegex(ValueError, 'all, needs or off'):
            self.world.change_role(mode='loud')
        for agent in ('shell', 'antigravity'):
            with self.subTest(agent=agent), patch.dict(self.world.entries[1], agent=agent):
                with self.assertRaisesRegex(ValueError, 'Claude or Codex'):
                    self.world.change_role()
        self.assertEqual(self.world.marked(), {})


class TurnOutcome(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'transcript.jsonl'
        self.outcome = coordinator.Feed().latest_outcome

    def fixture(self, agent, count=None, *, name=None):
        source = Path(__file__).parent / 'fixtures' / 'coordinator_failed' / f'{name or agent}.jsonl'
        self.path.write_bytes(b''.join(source.read_bytes().splitlines(keepends=True)[:count]))
        return tab('%2', agent=agent, transcript=str(self.path))

    def append(self, *records):
        with self.path.open('a') as stream:
            for row in records:
                stream.write(json.dumps(row) + '\n')

    def test_claude_api_error_has_a_new_identity_even_after_a_good_reply(self):
        entry = self.fixture('claude', 3)
        self.assertEqual(self.outcome(entry), {'kind': 'done', 'identity': 'prompt-1', 'text': 'Done.'})
        failed = self.fixture('claude')
        self.assertEqual(self.outcome(failed), {'kind': 'failed', 'identity': 'prompt-2',
                                                'text': 'You have reached your usage limit.'})

    def test_claude_task_notification_starts_a_new_turn(self):
        for kind, text in (('failed', 'API Error: Authentication failed.'), ('done', 'Second result.')):
            with self.subTest(kind=kind):
                entry = self.fixture('claude', 3, name='claude_notifications')
                first = self.outcome(entry)
                self.assertEqual(first['kind'], 'done')
                entry = self.fixture('claude', None if kind == 'failed' else 6, name='claude_notifications')
                if kind == 'done':
                    self.append({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}})
                second = self.outcome(entry)
                self.assertEqual((second['kind'], second['text']), (kind, text))
                self.assertNotEqual(first['identity'], second['identity'])
                self.assertEqual((first['identity'], second['identity']), ('notice-1', 'notice-2'))

    def test_claude_slash_command_records_keep_the_turn_identity(self):
        # A local command (/model, /cost) writes user text records but runs no turn: the last reply stays the outcome.
        for records in (
                [{'type': 'user', 'uuid': 'cmd', 'message': {'content': '<command-name>/model</command-name>\n<command-args></command-args>'}}],
                [{'type': 'user', 'uuid': 'out', 'message': {'content': '<local-command-stdout>Set model</local-command-stdout>'}}],
                [{'type': 'user', 'uuid': 'cmd', 'message': {'content': '<command-name>/cost</command-name>'}},
                 {'type': 'user', 'uuid': 'out', 'message': {'content': '<local-command-stdout>$0.10</local-command-stdout>'}},
                 {'type': 'assistant', 'uuid': 'late', 'message': {'content': [{'type': 'text', 'text': 'Done.'}]}}],
                [{'type': 'user', 'uuid': 'sh', 'message': {'content': '<bash-input>ls</bash-input>'}},
                 {'type': 'user', 'uuid': 'sh-out', 'message': {'content': '<bash-stdout>a.txt</bash-stdout><bash-stderr></bash-stderr>'}}]):
            with self.subTest(records=records[0]['uuid']):
                entry = self.fixture('claude', 3)
                before = self.outcome(entry)
                self.append(*records)
                self.assertEqual(self.outcome(entry), before)

    def test_claude_skill_command_starts_a_turn_when_claude_answers(self):
        # Real shape (Claude Code 2.1): the command record, then the skill text as isMeta, then the reply.
        for reply, kind in (({'type': 'assistant', 'uuid': 'r', 'message': {'content': [{'type': 'text', 'text': 'Shots pass.'}]}}, 'done'),
                            ({'type': 'assistant', 'uuid': 'r', 'isApiErrorMessage': True,
                              'message': {'content': [{'type': 'text', 'text': 'API Error: Overloaded.'}]}}, 'failed')):
            with self.subTest(kind=kind):
                entry = self.fixture('claude', 3)
                before = self.outcome(entry)
                self.append({'type': 'user', 'uuid': 'skill', 'message': {
                                'content': '<command-message>verify</command-message>\n<command-name>/verify</command-name>'}},
                            {'type': 'user', 'uuid': 'skill-text', 'isMeta': True, 'message': {'content': [{'type': 'text', 'text': 'Skill body.'}]}},
                            reply)
                after = self.outcome(entry)
                self.assertEqual((after['kind'], after['identity']), (kind, 'skill'))
                self.assertNotEqual(after['identity'], before['identity'])

    def test_claude_non_prompt_records_keep_the_turn_identity(self):
        entry = self.fixture('claude', 3)
        prompt = {'type': 'user', 'uuid': 'non-prompt', 'message': {'content': 'Text outside a turn.'}}
        self.append(*(dict(prompt, **{flag: True}) for flag in (
            'isMeta', 'isSidechain', 'isCompactSummary', 'isAutoCompactSummary')),
            dict(prompt, message={'content': [{'type': 'tool_result', 'content': 'Tool output.'},
                                             {'type': 'text', 'text': 'Tool context.'}]}))
        self.assertEqual(self.outcome(entry), {'kind': 'done', 'identity': 'prompt-1', 'text': 'Done.'})

    def test_claude_scheduled_fire_and_queued_prompt_start_turns(self):
        for prompt in (
                {'type': 'user', 'isMeta': True, 'turnOrigin': 'scheduled', 'scheduledTaskId': 'job',
                 'message': {'role': 'user', 'content': 'Check the job.'}},
                {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': 'Check the job.'}}):
            with self.subTest(kind=prompt['type']):
                entry = self.fixture('claude', 3)
                self.append({'type': 'user', 'message': {'content': '[Request interrupted by user]'}},
                            dict(prompt, uuid='next'),
                            {'type': 'assistant', 'message': {'content': 'Checked.'}})
                self.assertEqual(self.outcome(entry), {'kind': 'done', 'identity': 'next', 'text': 'Checked.'})

    def test_claude_system_error_recovery_and_user_interrupt(self):
        entry = self.fixture('claude')
        self.append({'type': 'assistant', 'uuid': 'retry-reply',
                     'message': {'content': [{'type': 'text', 'text': 'Which branch?\nNeeds you: yes'}]}})
        self.assertEqual(self.outcome(entry), {'kind': 'asks', 'identity': 'prompt-2',
                                               'text': 'Which branch? Needs you: yes'})
        self.append({'type': 'system', 'subtype': 'api_error', 'content': 'Authentication failed.'})
        self.assertEqual(self.outcome(entry), {'kind': 'failed', 'identity': 'prompt-2',
                                               'text': 'Authentication failed.'})
        self.append({'type': 'user', 'uuid': 'interrupt', 'message': {'content': [
            {'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}})
        self.assertIsNone(self.outcome(entry)['kind'])

    def test_claude_system_error_reads_the_notice_message(self):
        entry = self.fixture('claude', 3)
        self.append({'type': 'system', 'subtype': 'error', 'message': 'Invalid credentials.'},
                    {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}})
        self.assertEqual(self.outcome(entry), {'kind': 'failed', 'identity': 'prompt-1',
                                               'text': 'Invalid credentials.'})

    def codex_event(self, kind, **fields):
        return {'type': 'event_msg', 'payload': dict(type=kind, **fields)}

    def test_codex_terminal_error_then_retry_and_interruption(self):
        entry = self.fixture('codex', 3)
        self.append(self.codex_event('error', message='API Error: Authentication failed.'))
        self.assertEqual(self.outcome(entry), {'kind': 'failed', 'identity': 'turn-2',
                                               'text': 'API Error: Authentication failed.'})
        self.append(self.codex_event('task_complete', turn_id='turn-2', root_turn_id='turn-2',
                                     last_agent_message='Authenticated.'))
        self.assertEqual(self.outcome(entry), {'kind': 'done', 'identity': 'turn-2', 'text': 'Authenticated.'})
        self.append(self.codex_event('task_started', turn_id='turn-3', root_turn_id='turn-3'),
                    self.codex_event('turn_aborted', turn_id='turn-3', root_turn_id='turn-3', reason='interrupted'))
        self.assertIsNone(self.outcome(entry)['kind'])

    def test_codex_real_abort_shape_and_non_interrupt_reason(self):
        entry = self.fixture('codex')
        self.assertIsNone(self.outcome(entry)['kind'])
        self.append(self.codex_event('task_started', turn_id='turn-3', root_turn_id='turn-3'),
                    self.codex_event('turn_aborted', turn_id='turn-3', root_turn_id='turn-3', reason='error'))
        self.assertEqual(self.outcome(entry), {'kind': 'failed', 'identity': 'turn-3', 'text': 'Turn error.'})

    def test_codex_task_complete_with_error_reports_a_failed_turn(self):
        entry = self.fixture('codex', 2, name='codex_task_complete')
        self.assertEqual(self.outcome(entry), {'kind': 'done', 'identity': 'turn-1', 'text': 'Done.'})
        self.fixture('codex', name='codex_task_complete')
        self.assertEqual(self.outcome(entry), {'kind': 'failed', 'identity': 'turn-2',
                                               'text': 'The requested model is unavailable.'})
        error_text = 'API Error: You have hit your usage limit.'
        self.append(self.codex_event('task_started', turn_id='turn-3'),
                    self.codex_event('task_complete', turn_id='turn-3', last_agent_message=None,
                                     error={'message': error_text, 'codex_error_info': 'usage_limit_exceeded'}))
        self.assertEqual(self.outcome(entry), {'kind': 'failed', 'identity': 'turn-3', 'text': error_text})

    def test_codex_task_complete_with_null_reply_and_no_error_is_done(self):
        entry = self.fixture('codex', 3, name='codex_task_complete')
        self.append(self.codex_event('task_complete', turn_id='turn-2', root_turn_id='turn-2',
                                     last_agent_message=None))
        self.assertEqual(self.outcome(entry), {'kind': 'done', 'identity': 'turn-2', 'text': ''})

    def test_codex_abort_reports_only_failure_reasons(self):
        reasons = {'interrupted': None, 'replaced': None, 'review_ended': None, 'budget_limited': 'failed'}
        for reason, kind in reasons.items():
            with self.subTest(reason=reason):
                entry = self.fixture('codex', 3)
                self.assertIsNone(self.outcome(entry)['kind'])
                self.append(self.codex_event('turn_aborted', turn_id='turn-2', root_turn_id='turn-2', reason=reason))
                self.assertEqual(self.outcome(entry), {'kind': kind, 'identity': 'turn-2',
                                                       'text': 'Turn budget_limited.' if kind else ''})

    def test_transcript_is_read_only_when_its_signature_changes(self):
        entry = self.fixture('codex', 2)
        initial = self.outcome(entry)
        with patch('builtins.open', side_effect=AssertionError('opened unchanged transcript')):
            self.assertEqual(self.outcome(entry), initial)
        with self.path.open('rb+') as stream:
            content = stream.read()
            stream.seek(0)
            stream.write(content.replace(b'Done.', b'Next.'))
        metadata = self.path.stat()
        os.utime(self.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1000000))
        self.assertEqual(self.outcome(entry)['text'], 'Next.')


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

    def test_an_absent_or_restarting_coordinator_drops_its_updates_after_hold(self):
        for shell in (False, True):
            with self.subTest(shell=shell):
                world = World()
                world.act('%1', action='claim')
                world.run_turn('%2')
                if shell:
                    world.entries[0].update(agent='shell', writable=False)
                else:
                    world.entries.pop(0)
                world.tick()
                world.tick(599)
                self.assertTrue(world.feed.queues['%1']['events'])
                world.tick(1)
                self.assertEqual((world.sent, world.feed.queues), ([], {}))

    def test_an_absent_coordinator_can_return_to_its_queue(self):
        self.world.run_turn('%2')
        entry = self.world.entries.pop(0)
        self.world.tick(coordinator.JOIN)
        self.assertTrue(self.world.feed.queues['%1']['events'])
        self.world.entries.insert(0, entry)
        self.world.tick()
        self.assertIn('tab 2', self.world.sent[0][1])

    def test_a_coordinator_that_cannot_write_keeps_its_queue(self):
        self.world.run_turn('%2')
        self.world.entries[0]['writable'] = False
        self.world.tick(coordinator.JOIN)
        self.assertEqual(self.world.sent, [])
        self.assertTrue(self.world.feed.queues['%1']['events'])
        self.world.entries[0]['writable'] = True
        self.world.tick()
        self.assertIn('tab 2', self.world.sent[0][1])

    def test_shell_gap_keeps_the_queue_and_observes_work_in_the_same_session(self):
        for queued in (True, False):
            with self.subTest(queued=queued):
                world = World()
                world.entries[0]['transcript'] = 'same.jsonl'
                world.act('%1', action='claim')
                world.tick()
                if queued:
                    world.run_turn('%2')
                world.states['%3'] = 'Working'
                world.tick()
                world.entries[0].update(agent='shell', transcript=None, writable=False)
                world.tick(10)
                world.turns['%3'] = 1   # the turn that finishes in the gap
                world.states['%3'] = 'Idle'
                world.tick()
                world.tick(coordinator.SETTLE)
                self.assertEqual(world.sent, [])
                world.entries[0].update(agent='claude', transcript='same.jsonl', writable=True)
                world.tick(coordinator.JOIN)
                self.assertEqual(len(world.sent), 1)
                self.assertIn('tab 3', world.sent[0][1])
                self.assertEqual('tab 2' in world.sent[0][1], queued)

    def test_unbound_coordinator_keeps_its_queue_until_its_chat_returns(self):
        for middle in (dict(agent='claude', transcript=None, pending_conversation=True),
                       dict(agent='claude', transcript='pending.jsonl', pending_conversation=True),
                       dict(agent='codex', transcript=None)):
            with self.subTest(middle=middle):
                world = World()
                entry = world.entries[0]
                entry['agent'] = middle['agent']
                world.act('%1', action='claim')
                world.tick()
                world.run_turn('%2')
                bound = entry.copy()
                entry.update(agent='shell', transcript=None, writable=False)
                world.tick()
                entry.update(middle, writable=True)
                world.tick(coordinator.JOIN)
                self.assertEqual(world.sent, [])
                self.assertTrue(world.feed.queues['%1']['events'])
                entry.pop('pending_conversation', None)
                entry.update(bound)
                world.tick()
                self.assertEqual(len(world.sent), 1)
                self.assertIn('tab 2', world.sent[0][1])

    def test_unbound_coordinator_is_away_until_hold_expires(self):
        for middle in (dict(transcript='pending.jsonl', pending_conversation=True), dict(transcript=None)):
            with self.subTest(middle=middle):
                world = World()
                world.act('%1', action='claim')
                world.run_turn('%2')
                world.entries[0].update(middle)
                world.tick()
                world.tick(coordinator.HOLD - 1)
                self.assertTrue(world.feed.queues['%1']['events'])
                world.tick(1)
                self.assertEqual((world.sent, world.feed.queues), ([], {}))

    def test_delivery_retries_an_unbound_coordinator_seen_under_the_lock(self):
        for middle in (dict(transcript='pending.jsonl', pending_conversation=True), dict(transcript=None)):
            with self.subTest(middle=middle):
                world = World()
                world.act('%1', action='claim')
                world.run_turn('%2')
                snapshot = world.fresh()
                world.entries[0].update(middle)
                queue = world.feed.queues['%1']
                self.assertIsNone(world.deliver('%1', queue, queue['events']))
                world.tick(coordinator.JOIN, entries=snapshot)
                self.assertEqual(world.sent, [])
                self.assertTrue(world.feed.queues['%1']['events'])
                world.entries[0] = snapshot[0]
                world.tick()
                self.assertEqual(len(world.sent), 1)

    def test_unbound_source_keeps_its_previous_turn_state(self):
        for middle in (dict(transcript='pending.jsonl', pending_conversation=True), dict(transcript=None)):
            with self.subTest(middle=middle):
                world = World()
                world.act('%1', action='claim')
                world.tick()
                world.states['%2'] = 'Working'
                world.tick()
                bound = world.entries[1].copy()
                world.entries[1].update(middle)
                world.states['%2'] = 'Unknown'
                world.tick(coordinator.MINIMUM_WORK)
                world.entries[1] = bound
                world.states['%2'] = 'Idle'
                world.turns['%2'] = 1
                world.tick()
                world.tick(coordinator.SETTLE)
                world.tick(coordinator.JOIN)
                self.assertEqual(len(world.sent), 1)
                self.assertIn('tab 2', world.sent[0][1])

    def test_unbound_source_keeps_its_queued_update(self):
        for middle in (dict(transcript='pending.jsonl', pending_conversation=True), dict(transcript=None)):
            with self.subTest(middle=middle):
                world = World()
                world.act('%1', action='claim')
                world.run_turn('%2')
                bound = world.entries[1].copy()
                world.entries[1].update(middle)
                world.tick(coordinator.JOIN)
                self.assertEqual(world.sent, [])
                world.entries[1] = bound
                world.tick()
                self.assertEqual(len(world.sent), 1)
                self.assertIn('tab 2', world.sent[0][1])

    def test_release_drops_an_existing_queue_immediately(self):
        self.world.run_turn('%2')
        self.world.act('%1', action='release')
        self.world.tick(0)
        self.assertEqual((self.world.sent, self.world.feed.queues), ([], {}))

    def test_a_new_chat_after_a_shell_gap_drops_the_previous_chats_queue(self):
        self.world.entries[0]['transcript'] = 'first.jsonl'
        self.world.run_turn('%2')
        self.world.entries[0].update(agent='shell', transcript=None, writable=False)
        self.world.tick()
        self.assertTrue(self.world.feed.queues['%1']['events'])
        self.world.entries[0].update(agent='claude', transcript='another.jsonl', writable=True)
        self.world.tick(0)
        self.assertEqual(self.world.sent, [])
        self.assertFalse(self.world.feed.queues.get('%1', {}).get('events'))

    def test_failure_is_reported_once_per_turn_without_a_working_tick(self):
        for identity in ('first-failure', 'second-failure'):
            self.world.outcomes['%2'] = {'kind': 'failed', 'text': 'Check your credentials.', 'identity': identity}
            self.world.tick(coordinator.SETTLE)
            self.wait_for_send()
            count = 1 if identity == 'first-failure' else 2
            self.assertEqual(len(self.world.sent), count)
            self.assertIn('failed: Check your credentials.', self.world.sent[-1][1])
            self.wait_for_send()
            self.assertEqual(len(self.world.sent), count)

    def test_a_failure_from_before_the_first_sight_is_not_reported(self):
        # A server restart and a new claim both see the tabs for the first time; an old failure is no update.
        for restart in (False, True):
            with self.subTest(restart=restart):
                world = World()
                world.outcomes['%2'] = {'kind': 'failed', 'text': 'Old failure.', 'identity': 'old'}
                if restart:
                    world.act('%1', action='claim')
                    world.feed = coordinator.Feed(clock=lambda: world.now)
                else:
                    world.tick()
                    world.act('%1', action='claim')
                world.tick()
                world.tick(coordinator.SETTLE)
                world.tick(coordinator.JOIN)
                world.tick(coordinator.GAP)
                self.assertEqual(world.sent, [])
                world.outcomes['%2'] = {'kind': 'failed', 'text': 'New failure.', 'identity': 'new'}
                world.tick(coordinator.SETTLE)
                world.tick(coordinator.JOIN)
                world.tick(coordinator.GAP)
                self.assertEqual(len(world.sent), 1)
                self.assertIn('failed: New failure.', world.sent[0][1])

    def test_first_idle_after_an_unknown_or_unavailable_read_seeds_the_old_turn(self):
        for first in ('Unknown', 'Unavailable'):
            with self.subTest(first=first):
                world = World()
                world.outcomes['%2'] = {'kind': 'failed', 'text': 'Old failure.', 'identity': 'old'}
                world.states['%2'] = first
                world.act('%1', action='claim')
                world.tick()
                world.states['%2'] = 'Idle'
                world.tick()
                world.tick(coordinator.SETTLE)
                world.tick(coordinator.JOIN)
                self.assertEqual(world.sent, [])

    def test_first_working_after_an_unknown_read_reports_the_turn_when_it_ends(self):
        world = World()
        world.states['%2'] = 'Unknown'
        world.act('%1', action='claim')
        world.tick()
        world.states['%2'] = 'Working'
        world.outcomes['%2'] = {'kind': 'done', 'text': 'Partial text.', 'identity': 'new'}
        world.tick()
        world.outcomes['%2'] = {'kind': 'done', 'text': 'Finished text.', 'identity': 'new'}
        world.states['%2'] = 'Idle'
        world.tick(coordinator.MINIMUM_WORK)
        world.tick(coordinator.SETTLE)
        world.tick(coordinator.JOIN)
        self.assertEqual(len(world.sent), 1)
        self.assertIn('finished: Finished text.', world.sent[0][1])

    def test_a_turn_running_at_first_sight_is_reported_when_it_ends(self):
        # The real parsers fix a turn's identity when it starts, so first sight during the turn sees its identity.
        for restart in (False, True):
            with self.subTest(restart=restart):
                world = World()
                world.states['%2'] = 'Working'
                world.outcomes['%2'] = {'kind': 'done', 'text': 'Partial text.', 'identity': 'running'}
                if restart:
                    world.act('%1', action='claim')
                    world.feed = coordinator.Feed(clock=lambda: world.now)
                else:
                    world.tick()
                    world.act('%1', action='claim')
                world.tick()
                world.tick(coordinator.MINIMUM_WORK)
                world.outcomes['%2'] = {'kind': 'done', 'text': 'Finished text.', 'identity': 'running'}
                world.states['%2'] = 'Idle'
                world.tick()
                world.tick(coordinator.SETTLE)
                world.tick(coordinator.JOIN)
                world.tick(coordinator.GAP)
                self.assertEqual(len(world.sent), 1)
                self.assertIn('finished: Finished text.', world.sent[0][1])

    def test_failed_turn_waits_for_idle_to_settle(self):
        self.world.states['%2'] = 'Working'
        self.world.outcomes['%2'] = {'kind': 'failed', 'text': 'Rate limit reached.', 'identity': 'failed'}
        self.world.tick()
        self.world.states['%2'] = 'Idle'
        self.world.tick()
        self.world.tick(coordinator.SETTLE - 1)
        self.assertFalse(self.world.feed.queues.get('%1', {}).get('events'))
        self.world.tick(1)
        self.wait_for_send()
        self.assertIn('failed: Rate limit reached.', self.world.sent[0][1])

    def test_report_words_and_mode_matrix(self):
        words = {'done': 'finished', 'asks': 'finished and asks the user',
                 'needs': 'waits on the user', 'failed': 'failed'}
        for mode, allowed in (('all', set(words)), ('needs', {'asks', 'needs', 'failed'}), ('off', set())):
            for kind, word in words.items():
                with self.subTest(mode=mode, kind=kind):
                    event = {'kind': kind, 'name': 'worker', 'pane': '%2', 'text': 'Example'}
                    self.assertIn(f'{word}: Example', coordinator.message([event], mode))
                    self.assertEqual(coordinator.wanted(event, mode), kind in allowed)
        self.assertIn('needs: tabs that wait on the user or failed', coordinator.rules())


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

    def test_delivery_rebuilds_a_queued_turn_and_applies_the_current_mode(self):
        cases = (
            ('all', 'done', 'done', 'finished'),
            ('all', 'done', 'asks', 'finished and asks the user'),
            ('needs', 'asks', 'done', None),
            ('all', 'done', 'failed', 'failed'),
            ('needs', 'done', 'failed', 'failed'),
            ('all', 'done', None, None),
        )
        for mode, previous, current, word in cases:
            with self.subTest(mode=mode, previous=previous, current=current):
                world = World()
                world.act('%1', action='claim')
                world.tick()   # first sight: the turn below is new
                world.outcomes['%2'] = {'kind': previous, 'text': 'First turn.', 'identity': 'first'}
                world.run_turn('%2')
                world.act('%1', action='events', events=mode)
                world.replies['%2'] = 'Second turn.\nNeeds you: ' + ('yes' if current == 'asks' else 'no')
                # The next short turn finishes before the read under the delivery lock.
                def next_turn(entry):
                    outcome = {'kind': current, 'text': 'Second turn.', 'identity': 'second'}
                    world.outcomes[entry['id']] = outcome
                    return outcome

                with patch.object(world, 'delivery_outcome', side_effect=next_turn):
                    world.tick(coordinator.JOIN)
                if word:
                    self.assertEqual(len(world.sent), 1)
                    self.assertIn(f'{word}: Second turn.', world.sent[0][1])
                    self.assertNotIn('First turn.', world.sent[0][1])
                else:
                    self.assertEqual(world.sent, [])
                self.assertEqual(world.feed.queues['%1']['events'], [])
                world.tick(coordinator.JOIN + coordinator.GAP)
                self.assertEqual(len(world.sent), int(bool(word)))

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
        event = {'pane': '%2', 'session': '$0', 'chat': self.source['transcript'], 'kind': 'needs', 'text': 'update',
                 'name': 'tab 2', 'token': ('approval', 'id', 'a1')}
        queue = {'session': '$0', 'chat': self.entry['transcript']}
        return coordinator.deliver('%1', queue, [event], lock=self.server.SEND_LOCK,
                                   fresh=lambda: [self.entry, self.source], fresh_status=self.server.fresh_status,
                                   latest_outcome=coordinator.FEED.latest_outcome, submit_locked=self.server.submit_locked)

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


class BrowserRoleRoute(unittest.TestCase):
    def setUp(self):
        import server

        self.server = server
        self.world = World()
        self.world.entries[1]['thread'] = 'thread-2'
        self.world.lock = server.SEND_LOCK
        self.enterContext(patch.object(server.auth, 'token', return_value='test-token'))
        self.enterContext(patch.object(server, 'fresh_sessions', self.world.fresh))
        self.enterContext(patch.object(server, 'fresh_status', self.world.fresh_status))
        self.enterContext(patch.object(server.subprocess, 'run', self.world.tmux))
        self.send = self.enterContext(patch.object(server, 'send_text', side_effect=self.send_text))

    def send_text(self, entry, text, state, *, held):
        self.assertTrue(held)
        return self.world.send(entry, text, state)

    def post(self, *, cookie=True, origin='http://tether.test', header='1', thread='thread-2'):
        handler = object.__new__(self.server.Handler)
        body = json.dumps({'pane': '%2', 'thread': thread, 'mode': 'all'}).encode()
        handler.command = 'POST'
        handler.path = '/mobile/api/coordinator-role'
        handler.headers = {'Host': 'tether.test', 'Origin': origin, 'Content-Length': str(len(body)),
                           'X-Codex-Mobile': header, 'X-Tether-Hook': self.server.auth.session_value()}
        if cookie:
            handler.headers['Cookie'] = f'{self.server.auth.COOKIE}={self.server.auth.session_value()}'
        handler.client_address = ('127.0.0.1', 0)
        handler.rfile = io.BytesIO(body)
        with patch.object(handler, 'respond') as respond:
            handler.do_POST()
        respond.assert_called_once()
        return respond.call_args.args

    def test_browser_role_requires_cookie_origin_and_write_header(self):
        for headers in ({'cookie': False}, {'origin': 'http://not-tether.test'}, {'header': ''}):
            with self.subTest(headers=headers):
                status, *_ = self.post(**headers)
                self.assertIn(status, (401, 403))
        self.send.assert_not_called()
        self.assertEqual(self.world.marked(), {})
        self.assertEqual(self.post()[0], 200)
        self.send.assert_called_once()
        self.assertIn('/mobile/api/coordinator-role', self.server.TMUX_WRITES)

    def test_browser_role_shows_the_stale_thread_error(self):
        status, body = self.post(thread='old-thread')
        self.assertIn(status, (400, 409))
        self.assertEqual(body['error'], 'That tab started a new chat. Try again.')
        self.send.assert_not_called()

    def test_session_row_exposes_the_active_mode(self):
        entry = self.world.entries[1]
        with patch.object(self.server, 'shared_status', self.world.status), \
                patch.object(self.server, 'tab_folder', return_value='/'):
            self.assertIsNone(self.server.session_row(entry).get('coordinator'))
            for mode in ('all', 'needs', 'off'):
                with self.subTest(mode=mode):
                    entry['coordinator'] = mode
                    self.assertEqual(self.server.session_row(entry)['coordinator'], mode)


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
