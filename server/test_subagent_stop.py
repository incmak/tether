"""All terminal writes use a fake tmux runner and isolated transcript files."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import ask_bridge
import server
import subagent_stop as stop
import subagents
import terminal_input as terminal


LIST_HINT = ('↑/↓ to select · Enter to view · f to foreground · x to stop · '
             'ctrl+x ctrl+k to stop all agents · Esc to close')
DETAIL_HINT = '← to go back · Esc/Enter/Space to close · x to stop · f to foreground'


class RowsTests(unittest.TestCase):
    def test_exact_screen_with_shells(self):
        screen = '''   Background
   3 active shells · 1 active agent

     Shells (3)
   ❯ ● Wait for /tmp/claude-1000/never-created-xyz to ex…   running
     ● python3 -c "import time; time.sleep(1500); print(…   running
     ● sleep 1500                                           running

     Local agents (1)
     ● Short build                                          running · Haiku 5.5

       Earlier in this session (0)

   ↑/↓ to select · Enter to view · x to stop · Esc to close
'''
        self.assertEqual(stop.rows(screen), [
            {'label': 'Wait for /tmp/claude-1000/never-created-xyz to ex…',
             'selected': True, 'section': 'Shells'},
            {'label': 'python3 -c "import time; time.sleep(1500); print(…',
             'selected': False, 'section': 'Shells'},
            {'label': 'sleep 1500', 'selected': False, 'section': 'Shells'},
            {'label': 'Short build', 'selected': False, 'section': 'Local agents'},
        ])

    def test_one_kind_has_summary_and_no_header(self):
        screen = ('''   Background
   4 active shells

   ❯ ● sleep 1502                                           running
     ● sleep 1501                                           running

       Earlier in this session (2)

   ↑/↓ to select · Enter to view · x to stop · Esc to close
''')
        self.assertEqual([(row['label'], row['section']) for row in stop.rows(screen)],
                         [('sleep 1502', 'Shells'), ('sleep 1501', 'Shells')])
        self.assertEqual([row['section'] for row in stop.rows('Background\n1 active agent\n❯ ● Quiet wait  running · Haiku 5.5\n')],
                         ['Local agents'])

    def test_shell_detail_facts(self):
        screen = ('Shell details\n\n Status:   running\n Runtime:  1m 19s\n Command:  sleep 1502\n Output:\n'
                  ' No output available\n' + DETAIL_HINT)
        target = {'task': True, 'labels': ['Same sleep', 'sleep 1502'], 'kind': 'x', 'started': 0, 'body': 'sleep 1502'}
        self.assertEqual(stop.detail_facts(screen, target), (True, '1m 19s', 'sleep 1502'))
        self.assertTrue(stop.fits(screen, target, 80))
        self.assertFalse(stop.fits(screen, target, 120))
        self.assertFalse(stop.fits(screen.replace('sleep 1502', 'sleep 1501'), target, 79))

    def test_five_rows_across_sections_ignore_other_lines_and_stop_at_hint(self):
        screen = ('Background\n● Before section\nShells (2)\n❯ ● Shell one running\n'
                  '● Shell two completed\n\nOther text\nLocal agents (2)\n'
                  '● Agent one failed\n● Agent two stopped\nEarlier in this session (1)\n'
                  '● Old agent killed\n' + LIST_HINT + '\n● After hint running\n')
        parsed = stop.rows(screen)
        self.assertEqual(len(parsed), 5)
        self.assertEqual([row['section'] for row in parsed],
                         ['Shells', 'Shells', 'Local agents', 'Local agents', 'Earlier in this session'])
        self.assertEqual([row['label'] for row in parsed],
                         ['Shell one', 'Shell two', 'Agent one', 'Agent two', 'Old agent'])


class FakeTasks:
    def __init__(self, meta):
        self.meta = meta
        self.names = ['Beta wait', 'Alpha wait']
        self.shells = []
        self.at = 0
        self.detail = False
        self.open = False
        self.opens = True
        self.closes = True
        self.confirms = True
        self.stopped = False
        self.draft = ''
        self.before = None
        self.after_move = None
        self.keys = []
        self.commands = []
        self.needs_read = False
        self.fail_key = None
        self.on_stop = None
        self.detail_header = None
        self.viewing = None  # Row index opened with Enter; None means /tasks opened the detail itself.
        self.agent_details = {}  # name index -> (elapsed text, prompt)
        self.shell_details = {}  # shell index -> (runtime text or None, command)
        self.summary_only = False

    def screen(self):
        if not self.open:
            return self.before or f'──────────\n❯ {self.draft}\n──────────\n'
        if self.detail:
            hint = DETAIL_HINT.replace(' · x to stop', '') if self.stopped else DETAIL_HINT
            if self.viewing is not None and self.viewing < len(self.shells):
                runtime, command = self.shell_details.get(self.viewing, (None, self.shells[self.viewing]))
                return ('Shell details\n\nStatus:   running\n' + (f'Runtime:  {runtime}\n' if runtime else '')
                        + f'Command:  {command}\n\nOutput:\nNo output available\n\n{hint}')
            index = len(self.names) - 1 if self.viewing is None else self.viewing - len(self.shells)
            elapsed, prompt = self.agent_details.get(index, ('28s', 'Wait'))
            header = self.detail_header or f'general-purpose › {self.names[index]}'
            return (f'{header}\n{elapsed} · 42.3k tokens · 1 tool · Haiku 5.5\n\n   Progress\n   › Bash(sleep 600)\n\n'
                    f'   Prompt\n   {prompt}\n\n{hint}')
        if self.after_move and any(key in (('Down',), ('Up',)) for key in self.keys):
            return self.after_move
        shells = '\n'.join(f"{'❯' if self.at == index and not self.stopped else ' '} ● {name}    running"
                           for index, name in enumerate(self.shells))
        rows = '\n'.join(f"{'❯' if self.at == index + len(self.shells) and not self.stopped else ' '} ● {name}    running · Haiku 5.5"
                         for index, name in enumerate(self.names))
        if self.summary_only:
            # Claude Code 2.1.325 with one kind of task: a summary line and no section header.
            return f'Background\n{len(self.shells)} active shells\n\n{shells}\n\n{LIST_HINT}'
        return ('Background\n2 active agents\n'
                + (f'  Shells ({len(self.shells)})\n{shells}\n\n' if self.shells else '')
                + '  Local agents (2)\n' + rows + '\n'
                + ('❯' if self.stopped else ' ') + '   Earlier in this session (2)\n' + LIST_HINT)

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[1] == 'capture-pane':
            self.needs_read = False
            return subprocess.CompletedProcess(argv, 0, self.screen(), '')
        if argv[1] == 'display-message' and 'pane_width' in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, '80\t24\t80\t24\tfake\tfake\tmanual', '')
        if argv[1] != 'send-keys':
            raise AssertionError(f'Unexpected tmux call: {argv}')
        if self.needs_read:
            raise AssertionError('A key was sent without reading the screen after the previous key')
        keys = tuple(argv[4:])
        self.keys.append(keys)
        self.needs_read = True
        if keys == self.fail_key:
            raise subprocess.CalledProcessError(1, argv, stderr='private terminal data')
        if keys == ('-l', '/tasks'):
            self.draft = '/tasks'
        elif keys == ('Enter',):
            if self.open:
                # Enter views the selected row; in a detail view it would close the dialog.
                if self.detail or self.stopped:
                    raise AssertionError('Enter in a detail view closes the dialog')
                self.detail, self.viewing = True, self.at
            else:
                self.open = self.opens
                self.draft = ''
        elif keys in (('Down',), ('Up',)):
            if self.detail:
                raise AssertionError('Arrow keys sent in a detail view')
            self.at += 1 if keys == ('Down',) else -1
        elif keys == ('Left',):
            if not self.detail:
                raise AssertionError('Left sent outside a detail view')
            self.detail, self.viewing = False, None
        elif keys == ('x',):
            if not self.open:
                raise AssertionError('x sent outside the tasks dialog')
            self.stopped = True
            if self.confirms and self.on_stop is not None:
                self.on_stop()
            if self.confirms:
                self.meta.write_text('{"description":"Alpha wait","stoppedByUser":true}')
        elif keys == ('Escape',):
            if not self.open:
                raise AssertionError('Escape would interrupt the main agent')
            if self.closes:
                self.open = False
        else:
            raise AssertionError(f'Forbidden keys: {keys}')
        return subprocess.CompletedProcess(argv, 0, '', '')


class StopTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        main = root / 'chat.jsonl'
        main.write_text('')
        agents = root / 'chat' / 'subagents'
        agents.mkdir(parents=True)
        (agents / 'agent-alpha.jsonl').write_text('{"type":"user","message":{"content":"Wait"}}\n')
        self.meta = agents / 'agent-alpha.meta.json'
        self.meta.write_text('{"description":"Alpha wait"}')
        self.entry = {'id': '%fake-stop', 'agent': 'claude', 'transcript': str(main),
                      'thread': 'chat', 'writable': True, 'owner_pid': 123, 'process_start': '456'}
        self.enterContext(patch.object(subagents, 'process_start', return_value='456'))
        self.enterContext(patch.object(subagents.procinfo, 'start_ms', return_value=1800000000000))
        self.pane = FakeTasks(self.meta)
        self.verify = self.enterContext(patch.object(terminal, 'verify'))
        self.enterContext(patch.object(ask_bridge, 'overlay', side_effect=lambda entry, status: status))
        self.enterContext(patch.object(terminal, '_attention_seen', {}))
        self.addCleanup(subagents._cache.clear)
        self.enterContext(patch.object(subagents, '_fd_links', return_value=[]))
        self.enterContext(patch.object(subagents.task_process, 'LEDGER', root / 'task-stops.json'))
        # No task process is found unless a test supplies one, so the /tasks path runs.
        self.children = self.enterContext(patch.object(stop.task_process, 'children', return_value=[]))
        subagents._tasks_cache.clear()
        self.addCleanup(subagents._tasks_cache.clear)
        subagents._cache.clear()

    def stop(self, identifier='alpha'):
        return stop.stop(self.entry, identifier, run=self.pane, sleep=lambda _: None)

    def test_list_stops_second_row_with_exact_keys_and_reads_after_each_key(self):
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Down',), ('Enter',), ('x',), ('Escape',)])
        self.verify.assert_called_once_with(self.entry, self.pane)
        self.assertFalse(self.pane.needs_read)

    def test_list_moves_across_shells_to_second_agent(self):
        self.pane.shells = ['First shell', 'Second shell']
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',),
                                         ('Down',), ('Down',), ('Down',), ('Enter',), ('x',), ('Escape',)])

    def test_shell_with_matching_label_is_not_a_target(self):
        self.pane.shells = ['Alpha wait']
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',),
                                         ('Down',), ('Down',), ('Enter',), ('x',), ('Escape',)])

    def test_detail_stops_and_closes_after_stop_hint_disappears(self):
        self.pane.detail = True
        self.assertTrue(self.stop()['ok'])
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('x',), ('Escape',)])

    def test_two_hour_old_log_under_live_owner_can_be_stopped(self):
        path = self.meta.with_name('agent-alpha.jsonl')
        old = subagents.time.time() - 2 * 60 * 60
        os.utime(path, (old, old))
        self.pane.detail = True
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('x',), ('Escape',)])

    def test_list_can_move_up(self):
        self.pane.names.reverse()
        self.pane.at = 1
        self.assertTrue(self.stop()['ok'])
        self.assertIn(('Up',), self.pane.keys)

    def test_cut_description_matches_in_list_and_detail(self):
        for detail in (False, True):
            with self.subTest(detail=detail):
                self.meta.write_text('{"description":"Alpha wait"}')
                self.pane = FakeTasks(self.meta)
                self.pane.detail = detail
                self.pane.names[-1] = 'Alpha…'
                self.assertTrue(self.stop()['ok'])

    def test_duplicate_or_ambiguous_cut_names_refuse_and_close(self):
        for names in (['Alpha wait', 'Alpha wait'], ['Alpha…', 'Alpha wait']):
            with self.subTest(names=names):
                self.pane = FakeTasks(self.meta)
                self.pane.names = names
                with self.assertRaisesRegex(stop.StopError, '2 agents share this name and could not be told apart'):
                    self.stop()
                # Each one is viewed and left with Left; nothing is stopped.
                self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Enter',), ('Left',),
                                                  ('Down',), ('Enter',), ('Left',), ('Escape',)])

    def test_same_name_agents_told_apart_by_prompt(self):
        self.pane.names = ['Alpha wait', 'Alpha wait']
        self.pane.agent_details = {0: ('28s', 'Another prompt'), 1: ('28s', 'Wait')}
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Enter',), ('Left',), ('Down',), ('Enter',),
                                         ('Left',), ('Enter',), ('x',), ('Escape',)])

    def test_same_name_agents_told_apart_by_run_time(self):
        path = self.meta.with_name('agent-alpha.jsonl')
        path.write_text('{"type":"user","timestamp":"2027-01-15T08:00:00Z","message":{"content":"Wait"}}\n')
        started = subagents._stamp('2027-01-15T08:00:00Z')
        self.enterContext(patch.object(subagents.procinfo, 'start_ms', return_value=(started - 60) * 1000))
        self.pane.names = ['Alpha wait', 'Alpha wait']
        self.pane.agent_details = {0: ('1m 43s', 'Wait'), 1: ('3m 2s', 'Wait')}
        result = stop.stop(self.entry, 'alpha', run=self.pane, sleep=lambda _: None, clock=lambda: started + 183)
        self.assertEqual(result, {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys[-3:], [('Enter',), ('x',), ('Escape',)])
        self.assertEqual(self.pane.at, 1)

    def test_run_time_rounded_to_minutes_uses_wider_slack(self):
        target = {'task': False, 'labels': ['Alpha wait'], 'kind': 'general-purpose', 'started': 0, 'body': ''}
        screen = 'general-purpose › Alpha wait\n2h 5m · 1k tokens\n'
        self.assertTrue(stop.fits(screen, target, 7500 + 60))
        self.assertFalse(stop.fits(screen, target, 7500 + 200))
        self.assertFalse(stop.fits('general-purpose › Alpha wait\n1m 40s · 1k tokens\n', target, 110))

    def test_mismatched_list_or_detail_refuses_and_closes(self):
        for detail in (False, True):
            with self.subTest(detail=detail):
                self.pane = FakeTasks(self.meta)
                self.pane.detail = detail
                self.pane.names = ['Beta wait', 'Alpha']  # no ellipsis means no prefix match
                with self.assertRaisesRegex(stop.StopError, 'Could not identify'):
                    self.stop()
                self.assertEqual(self.pane.keys[-1], ('Escape',))
                self.assertNotIn(('x',), self.pane.keys)

    def test_draft_refused_without_any_keys(self):
        self.pane.draft = 'Keep my draft'
        with self.assertRaisesRegex(stop.StopError, 'unsent draft'):
            self.stop()
        self.assertEqual(self.pane.keys, [])
        self.assertEqual(self.pane.draft, 'Keep my draft')

    def test_approval_refused_without_any_keys(self):
        self.pane.before = ('Bash command\n  rm a.py\nDo you want to proceed?\n'
                            '❯ 1. Yes\n  2. No\nEsc to cancel')
        with self.assertRaisesRegex(stop.StopError, 'waiting for you'):
            self.stop()
        self.assertEqual(self.pane.keys, [])

    def test_question_refused_without_any_keys_even_before_attention_delay(self):
        self.pane.before = 'Which option?\n❯ 1. First\n  2. Second\nEnter to select · Esc to cancel'
        with self.assertRaises(stop.StopError):
            self.stop()
        self.assertEqual(self.pane.keys, [])

    def test_non_idle_non_working_states_refused(self):
        for state in ('Input needed', 'Unavailable', 'Unknown'):
            with self.subTest(state=state), patch.object(terminal, 'live_status', return_value={'state': state}):
                with self.assertRaises(stop.StopError):
                    self.stop()
                self.assertEqual(self.pane.keys, [])

    def test_working_main_agent_can_stop_a_subagent(self):
        with patch.object(terminal, 'live_status', return_value={'state': 'Working'}):
            self.assertTrue(self.stop()['ok'])

    def test_never_opened_dialog_sends_no_escape(self):
        self.pane.opens = False
        with self.assertRaisesRegex(stop.StopError, 'tasks dialog did not open'):
            self.stop()
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',)])

    def test_agent_not_running_refused_without_terminal_writes(self):
        self.meta.write_text('{"finished":true}')
        with self.assertRaisesRegex(stop.StopError, 'not running'):
            self.stop()
        self.assertEqual(self.pane.commands, [])

    def test_invalid_ids_and_non_claude_refused_without_terminal_access(self):
        for identifier in ('../alpha', '', 'a/b', None, 'a' * 65):
            with self.subTest(identifier=identifier), self.assertRaises(stop.StopError):
                self.stop(identifier)
        self.entry['agent'] = 'codex'
        with self.assertRaisesRegex(stop.StopError, 'Only Claude'):
            self.stop()
        self.assertEqual(self.pane.commands, [])

    def test_verify_failure_never_drives_unverified_pane(self):
        self.verify.side_effect = RuntimeError('The Claude conversation changed. Reload its tab.')
        with self.assertRaisesRegex(stop.StopError, 'conversation changed'):
            self.stop()
        self.assertEqual(self.pane.commands, [])

    def test_failure_to_confirm_raises_after_closing(self):
        self.pane.confirms = False
        with self.assertRaisesRegex(stop.StopError, 'The agent did not stop. Check the terminal view.'):
            self.stop()
        self.assertFalse(self.pane.open)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_leftover_dialog_is_closed_before_opening_another(self):
        self.pane.open = True
        self.assertTrue(self.stop()['ok'])
        self.assertEqual(self.pane.keys[0], ('Escape',))

    def test_dialog_that_will_not_close_gets_only_three_escapes(self):
        self.pane.closes = False
        with self.assertRaisesRegex(stop.StopError, 'tasks dialog did not close'):
            self.stop()
        self.assertEqual(self.pane.keys.count(('Escape',)), 3)

    def test_leftover_dialog_that_will_not_close_gets_only_three_escapes(self):
        self.pane.open = True
        self.pane.closes = False
        with self.assertRaisesRegex(stop.StopError, 'tasks dialog did not close'):
            self.stop()
        self.assertEqual(self.pane.keys, [('Escape',)] * 3)

    def test_readonly_panel_is_not_closed_with_escape(self):
        self.pane.before = 'Settings: Status   Config   Usage\nSome usage\nEsc to close'
        with self.assertRaises(stop.StopError):
            self.stop()
        self.assertEqual(self.pane.keys, [])

    def test_changed_list_after_move_refuses_stop_and_closes(self):
        self.pane.after_move = ('Background\nLocal agents (1)\n❯ ● Different task    running · Haiku 5.5\n' + LIST_HINT)
        with self.assertRaisesRegex(stop.StopError, 'Could not identify'):
            self.stop()
        self.assertNotIn(('x',), self.pane.keys)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_navigation_is_bounded_at_40_keys(self):
        self.pane.after_move = (
            'Background\nLocal agents (2)\n❯ ● Beta wait    running · Haiku 5.5\n'
            '  ● Alpha wait    running · Haiku 5.5\n' + LIST_HINT)
        with self.assertRaisesRegex(stop.StopError, 'Could not reach'):
            self.stop()
        self.assertEqual(self.pane.keys.count(('Down',)), 40)
        self.assertNotIn(('x',), self.pane.keys)

    def test_key_failure_still_closes_and_does_not_expose_subprocess_output(self):
        self.pane.fail_key = ('Down',)
        with self.assertRaisesRegex(stop.StopError, '^Could not stop the agent. Check the terminal view.$'):
            self.stop()
        self.assertFalse(self.pane.open)
        self.assertEqual(self.pane.keys[-1], ('Escape',))


PROCESS = stop.task_process.Process(4242, '1')


class ProcessStopTests(unittest.TestCase):
    def setUp(self):
        StopTests.setUp(self)
        self.enterContext(patch.object(stop.task_process, 'same', return_value=True))

    stop = StopTests.stop
    def make_tasks(self):
        self.tasks = self.meta.parent.parent.parent / 'tmp' / 'cwd' / '11111111-1111-1111-1111-111111111111' / 'tasks'
        self.tasks.mkdir(parents=True)
        self.enterContext(patch.object(subagents, 'TASKS_TMP', self.tasks.parent.parent.parent))
        self.enterContext(patch.object(subagents, '_fd_links', return_value=[self.tasks]))
        self.output = self.tasks / 'shell02.output'
        self.output.write_text('output')
        records = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'bash-start', 'name': 'Bash',
                 'input': {'description': 'Second wait', 'command': 'sleep 600\necho done'}}]}},
            {'type': 'user', 'timestamp': '2027-01-15T08:00:00Z', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'bash-start'}]},
             'toolUseResult': {'backgroundTaskId': 'shell02'}}]
        Path(self.entry['transcript']).write_text(''.join(json.dumps(record) + '\n' for record in records))
        self.pane.names = ['sleep 600']
        self.pane.shells = ['sleep 300', 'sleep 600']
        self.pane.at = 2
        self.pane.on_stop = lambda: self.output.write_text('output\n[killed]\n')

    def test_stop_task_moves_up_from_agent_to_second_shell_only(self):
        self.make_tasks()
        self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Up',), ('Enter',), ('x',), ('Escape',)])

    def test_running_earlier_chat_task_can_confirm_after_it_leaves_listing(self):
        self.make_tasks()
        first = Path(self.entry['transcript']).with_name(self.tasks.parent.name + '.jsonl')
        first.write_text(Path(self.entry['transcript']).read_text())
        Path(self.entry['transcript']).write_text('')
        self.assertTrue(self.stop('shell02')['ok'])
        self.assertEqual(subagents.listing(self.entry)['tasks'], [])

    def test_task_can_target_description_when_command_differs(self):
        self.make_tasks()
        self.pane.shells[-1] = 'Second wait'
        self.pane.shell_details[1] = (None, 'sleep 600')  # The detail view shows the real command.
        self.assertTrue(self.stop('shell02')['ok'])

    def test_task_detail_header_is_checked_before_x(self):
        for header, success in (('Shell › sleep 600', True), ('Second wait', True), ('Unrelated task', False)):
            with self.subTest(header=header):
                if not hasattr(self, 'tasks'):
                    self.make_tasks()
                self.output.write_text('output')
                self.pane = FakeTasks(self.meta)
                self.pane.detail = True
                self.pane.detail_header = header
                self.pane.on_stop = lambda: self.output.write_text('[killed]')
                if success:
                    self.assertTrue(self.stop('shell02')['ok'])
                else:
                    with self.assertRaisesRegex(stop.StopError, 'Could not identify that task. Stop it in the terminal.'):
                        self.stop('shell02')
                    self.assertNotIn(('x',), self.pane.keys)
                self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_task_ambiguous_shells_refuse_with_task_error(self):
        self.make_tasks()
        self.pane.shells = ['sleep 600', 'Second wait']
        self.pane.shell_details = {0: (None, 'sleep 600'), 1: (None, 'sleep 600')}
        with self.assertRaisesRegex(stop.StopError, '2 tasks share this name'):
            self.stop('shell02')
        self.assertNotIn(('x',), self.pane.keys)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_same_command_shells_told_apart_by_runtime(self):
        self.make_tasks()
        started = subagents._stamp('2027-01-15T08:00:00Z')
        self.pane.names = []
        self.pane.shells = ['sleep 600', 'sleep 600']
        self.pane.at = 0
        self.pane.shell_details = {0: ('19s', 'sleep 600'), 1: ('5m 0s', 'sleep 600')}
        result = stop.stop(self.entry, 'shell02', run=self.pane, sleep=lambda _: None, clock=lambda: started + 301)
        self.assertEqual(result, {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.at, 1)
        self.assertEqual(self.pane.keys[-3:], [('Enter',), ('x',), ('Escape',)])

    def test_shells_only_list_without_section_header(self):
        self.make_tasks()
        self.pane.summary_only = True
        self.pane.names = []
        self.pane.at = 0
        self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Down',), ('Enter',), ('x',), ('Escape',)])

    def test_detail_opened_at_once_for_another_task_goes_back_to_list(self):
        self.make_tasks()
        self.pane.names = []
        self.pane.at = 0
        self.pane.detail = True
        self.pane.viewing = 0  # /tasks opened the first shell's detail; the target is the second.
        self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Left',), ('Down',), ('Enter',), ('x',), ('Escape',)])

    def test_task_with_exact_process_stops_by_signal_without_terminal_keys(self):
        self.make_tasks()
        signals = []

        def kill(pid, sig):
            signals.append((pid, sig))
            self.output.write_text('output\n[exited with code 143]\n')

        # /tasks refuses (an unsent draft), so the signal fallback stops the task.
        self.pane.draft = 'Keep draft'
        with patch.object(stop.task_process, 'find', return_value=PROCESS) as find, \
                patch.object(stop.task_process, 'stop', side_effect=lambda process: kill(process.pid, 15) or True):
            self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(find.call_args.args[2], os.path.realpath(self.output))
        self.assertEqual(signals, [(4242, 15)])
        self.assertEqual(self.pane.keys, [])
        task = subagents.listing(self.entry)['tasks'][0]
        self.assertEqual((task['state'], task['summary']), ('stopped', 'Stopped from Tether.'))

    def test_task_process_that_will_not_die_reports_error(self):
        self.make_tasks()
        self.pane.draft = 'Keep draft'
        with patch.object(stop.task_process, 'find', return_value=PROCESS), \
                patch.object(stop.task_process, 'stop', return_value=False):
            with self.assertRaisesRegex(stop.StopError, 'The task did not stop'):
                self.stop('shell02')
        self.assertEqual(self.pane.keys, [])

    def test_tasks_screen_stop_never_sends_a_signal(self):
        self.make_tasks()
        with patch.object(stop.task_process, 'find', return_value=PROCESS), \
                patch.object(stop.task_process, 'stop') as signal_stop:
            self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        signal_stop.assert_not_called()
        self.assertIn(('x',), self.pane.keys)

    def test_unreadable_tasks_screen_falls_back_to_signal(self):
        self.make_tasks()
        self.pane.shells = ['sleep 600', 'Second wait']
        self.pane.shell_details = {0: (None, 'sleep 600'), 1: (None, 'sleep 600')}
        with patch.object(stop.task_process, 'find', return_value=PROCESS), \
                patch.object(stop.task_process, 'stop',
                             side_effect=lambda process: self.output.write_text('[exited with code 143]') or True):
            self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertNotIn(('x',), self.pane.keys)

    def test_failure_after_x_never_falls_back_to_signal(self):
        # After x, Claude may already have stopped the task; a signal could then reach a same-command twin.
        for setup in (lambda: setattr(self.pane, 'confirms', False), lambda: setattr(self.pane, 'fail_key', ('Escape',))):
            with self.subTest(setup=setup):
                self.setUp()  # A fresh pane and session for each case.
                self.make_tasks()
                setup()
                with patch.object(stop.task_process, 'find', return_value=PROCESS), \
                        patch.object(stop.task_process, 'stop') as signal_stop:
                    with self.assertRaises(stop.StopError):
                        self.stop('shell02')
                signal_stop.assert_not_called()
                self.assertIn(('x',), self.pane.keys)

    def test_sole_row_whose_detail_does_not_fit_gets_no_x(self):
        self.make_tasks()
        self.pane.names = []
        self.pane.at = 0
        self.pane.shells = ['Second wait']
        self.pane.shell_details = {0: (None, 'sleep 900')}  # Same label, other command.
        with patch.object(stop.task_process, 'find', return_value=None):
            with self.assertRaisesRegex(stop.StopError, 'Could not identify that task'):
                self.stop('shell02')
        self.assertNotIn(('x',), self.pane.keys)
        self.assertIn(('Enter',), self.pane.keys[2:])  # The detail view was opened and checked.

    def test_process_is_bound_once_before_any_terminal_key(self):
        self.make_tasks()
        self.pane.draft = 'Keep draft'
        keys_at_find = []
        with patch.object(stop.task_process, 'find',
                          side_effect=lambda *args, **kwargs: keys_at_find.append(len(self.pane.keys)) or PROCESS), \
                patch.object(stop.task_process, 'stop', side_effect=lambda process: self.output.write_text('[exited with code 143]') or True):
            self.assertTrue(self.stop('shell02')['ok'])
        self.assertEqual(keys_at_find, [0])

    def test_task_that_ends_during_the_terminal_try_is_not_replaced_by_a_twin(self):
        # The bound process ended on its own; the fallback must not look again and find a twin.
        self.make_tasks()
        self.pane.draft = 'Keep draft'
        def ended(process):
            self.output.write_text('[exited with code 0]')
            return False
        with patch.object(stop.task_process, 'find', return_value=PROCESS) as find, \
                patch.object(stop.task_process, 'same', side_effect=ended), \
                patch.object(stop.task_process, 'record') as record, \
                patch.object(stop.task_process, 'stop') as signal_stop:
            self.assertTrue(self.stop('shell02')['ok'])
        self.assertEqual(find.call_count, 1)
        signal_stop.assert_not_called()
        record.assert_not_called()

    def test_monitor_is_never_bound_for_a_signal(self):
        # A same-command monitor B can start after the listing snapshot and be the sole process match.
        folder = Path(self.meta).parent / 'monitor-tasks'
        folder.mkdir()
        (folder / 'm1.output').write_text('')
        monitor = {'id': 'm1', 'kind': 'monitor', 'command': 'tail -f log', 'started': '2026-11-14T10:00:00Z'}
        shell = {'id': 's1', 'kind': 'shell', 'command': 'sleep 600'}
        (folder / 's1.output').write_text('')
        with patch.object(stop.subagents, '_owner_since', return_value=1), \
                patch.object(stop.subagents, '_tasks_dir', return_value=folder), \
                patch.object(stop.task_process, 'find', return_value=PROCESS) as find:
            self.assertIsNone(stop.bind(self.entry, monitor))
            find.assert_not_called()
            self.assertEqual(stop.bind(self.entry, shell), (PROCESS, f'{folder}/s1'))
            self.assertEqual(find.call_args.args[2], os.path.realpath(folder / 's1.output'))

    def test_unreadable_screen_error_names_a_newer_claude_version(self):
        sessions = Path(self.meta).parent / 'sessions'
        sessions.mkdir(exist_ok=True)
        (sessions / f'{self.entry["owner_pid"]}.json').write_text(json.dumps({'version': '9.9.9'}))
        with patch.object(stop, 'SESSIONS', sessions):
            message = stop.with_version(self.entry, 'Could not identify that agent. Stop it in the terminal.')
            self.assertIn('Claude Code 9.9.9', message)
            self.assertEqual(stop.with_version(self.entry, 'That agent is not running.'), 'That agent is not running.')
            (sessions / f'{self.entry["owner_pid"]}.json').write_text(json.dumps({'version': stop.CHECKED_TASKS_UI}))
            self.assertEqual(stop.with_version(self.entry, 'Could not identify x.'), 'Could not identify x.')

    def test_task_confirmation_accepts_finished_and_failed(self):
        self.make_tasks()
        for code, state in ((0, 'finished'), (2, 'failed')):
            with self.subTest(code=code):
                self.output.write_text('output')
                self.pane = FakeTasks(self.meta)
                self.pane.shells = ['sleep 600']
                self.pane.names = []
                self.pane.on_stop = lambda: self.output.write_text(f'[exited with code {code}]')
                self.assertEqual(self.stop('shell02'), {'ok': True, 'state': state})

    def test_task_draft_and_failed_confirmation_are_safe(self):
        self.make_tasks()
        self.pane.draft = 'Keep draft'
        with self.assertRaisesRegex(stop.StopError, 'unsent draft'):
            self.stop('shell02')
        self.assertEqual(self.pane.keys, [])
        self.pane.draft = ''
        self.pane.confirms = False
        with self.assertRaisesRegex(stop.StopError, 'The task did not stop'):
            self.stop('shell02')
        self.assertFalse(self.pane.open)

    def test_foreground_output_without_start_is_not_a_task(self):
        self.make_tasks()
        self.output.with_name('unknown1.output').write_text('')
        with self.assertRaisesRegex(stop.StopError, 'no longer available'):
            self.stop('unknown1')
        self.assertEqual(self.pane.commands, [])

    def test_old_silent_agent_can_be_stopped_after_clear(self):
        self.make_tasks()
        old = self.tasks.parent.name
        new = '22222222-2222-2222-2222-222222222222'
        project = self.meta.parent.parent.parent / 'projects' / 'project'
        folder = project / old / 'subagents'
        folder.mkdir(parents=True)
        path = folder / 'agent-alpha.jsonl'
        path.write_text('{"type":"user","message":{"content":"Wait"}}\n')
        self.pane.meta = path.with_suffix('.meta.json')
        self.pane.meta.write_text('{"description":"Alpha wait"}')
        (self.tasks / 'alpha.output').symlink_to(path)
        main = project / f'{new}.jsonl'
        main.write_text('')
        self.entry.update(transcript=str(main), thread=new)
        self.enterContext(patch.object(subagents, 'PROJECTS', project.parent))
        self.pane.names = ['Alpha wait']
        self.pane.shells = []
        self.pane.at = 0
        self.pane.on_stop = None
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})


class StopRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = {'id': '%fake', 'thread': 'chat', 'agent': 'claude', 'writable': True}

    def handler(self, **extra):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/subagent-stop'
        payload = json.dumps({'session': '%fake', 'id': 'alpha', **extra}).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        return handler

    def test_route_selects_session_and_stops_under_same_lock_as_model_picker(self):
        handler = self.handler()

        def apply(config, identifier):
            self.assertIs(config, self.config)
            self.assertEqual(identifier, 'alpha')
            self.assertTrue(server.SEND_LOCK.locked())
            return {'ok': True, 'state': 'stopped'}

        with patch.object(server, 'choose', return_value=self.config) as choose, \
                patch.object(stop, 'stop', side_effect=apply):
            handler.do_POST()
        choose.assert_called_once_with('%fake')
        handler.respond.assert_called_once_with(200, {'ok': True, 'state': 'stopped'})

    def test_auth_origin_and_write_header_required_before_stop(self):
        for blocked in ('auth', 'origin', 'header'):
            handler = self.handler()
            if blocked == 'auth':
                handler.deny.return_value = True
            elif blocked == 'origin':
                handler.headers['Origin'] = 'http://elsewhere.test'
            else:
                del handler.headers['X-Codex-Mobile']
            with self.subTest(blocked=blocked), patch.object(server, 'choose') as choose, patch.object(stop, 'stop') as apply:
                handler.do_POST()
                choose.assert_not_called()
                apply.assert_not_called()
                self.assertEqual(handler.rfile.tell(), 0)

    def test_stale_thread_or_readonly_pane_refused(self):
        handler = self.handler(thread='old')
        with patch.object(server, 'choose', return_value=self.config), patch.object(stop, 'stop') as apply:
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 409)
            self.config['writable'] = False
            handler = self.handler()
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 503)
            apply.assert_not_called()

    def test_error_message_uses_existing_write_error_shape(self):
        handler = self.handler()
        with patch.object(server, 'choose', return_value=self.config), \
                patch.object(stop, 'stop', side_effect=stop.StopError('The agent did not stop. Check the terminal view.')):
            handler.do_POST()
        handler.respond.assert_called_once_with(503, {
            'error': 'The agent did not stop. Check the terminal view.', 'draft': False})


class RealScreenTests(unittest.TestCase):
    """Real /tasks screens captured from Claude Code. Add a folder for each new version whose screens change."""
    FOLDER = Path(__file__).parent / 'fixtures' / 'claude_2.1.325_tasks'
    FOLDER_327 = FOLDER.parent / 'claude_2.1.327_tasks'
    FOLDER_330 = FOLDER.parent / 'claude_2.1.330_tasks'

    def screen(self, name):
        return (self.FOLDER / f'{name}.txt').read_text()

    def test_list_reads_shells_monitor_and_agent_sections(self):
        self.assertEqual([(row['label'], row['selected'], row['section']) for row in stop.rows(self.screen('list'))], [
            ('Fixture monitor', True, 'Shells'), ('sleep 401', False, 'Shells'), ('Fixture wait 2', False, 'Local agents')])
        self.assertEqual([row['selected'] for row in stop.rows(self.screen('list_agent_selected'))], [False, False, True])

    def test_2_1_327_list_identifies_each_task(self):
        screen = (self.FOLDER_327 / 'list.txt').read_text()
        self.assertTrue(stop.dialog_open(screen.replace(' · x to stop', '')))
        rows = stop.rows(screen)
        self.assertEqual([(row['label'], row['selected'], row['section']) for row in rows], [
            ('Stop canary monitor', True, 'Shells'), ('sleep 600', False, 'Shells'),
            ('Stop canary agent', False, 'Background agents')])
        for label, is_task in (('Stop canary monitor', True), ('sleep 600', True), ('Stop canary agent', False)):
            with self.subTest(label=label):
                target = {'task': is_task}
                self.assertEqual([row['label'] for row in rows if stop.in_section(target, row['section'])
                                  and stop.matches(label, row['label'])], [label])

    def test_2_1_330_lists_identify_main_tasks_and_refuse_child_tasks(self):
        cases = (
            ('list', 'Stop canary agent', ['sleep 601']),
            ('list_no_sections', 'Stop canary monitor', ['sleep 601']),
            ('list_subagent_shell', 'Wait for background sleep 601 to finish',
             ['Wait for background sleep 601 to finish', 'sleep 601']),
        )
        for name, selected, children in cases:
            with self.subTest(name=name):
                screen = (self.FOLDER_330 / f'{name}.txt').read_text()
                self.assertTrue(stop.dialog_open(screen))
                rows = stop.rows(screen)
                self.assertEqual([row['label'] for row in rows if row['selected']], [selected])
                self.assertEqual([row['label'] for row in rows if row.get('unverified_task')], children)
                for label in ('sleep 600', 'Stop canary monitor'):
                    target = {'task': True, 'labels': [label]}
                    self.assertEqual([row['label'] for row in rows if stop.is_target(target, row)], [label])
                for label in children:
                    target = {'task': True, 'labels': [label]}
                    self.assertFalse(any(stop.is_target(target, row) for row in rows))

    def test_2_1_330_single_kind_summary_parses_selected_task(self):
        for summary, section in (('1 shell', 'Shells'), ('3 shells', 'Shells'), ('1 monitor', 'Monitors')):
            with self.subTest(summary=summary):
                screen = (self.FOLDER_330 / 'list_no_sections.txt').read_text().replace('2 shells, 1 monitor', summary)
                self.assertTrue(any(row['selected'] and row['section'] == section for row in stop.rows(screen)))

    def test_2_1_330_agent_detail_matches_prompt_and_runtime(self):
        screen = (self.FOLDER_330 / 'detail_agent.txt').read_text()
        body = ('Run Bash `sleep 601` with run_in_background true and timeout 600000. '
                'Wait for that shell to finish before you reply. Then reply done.')
        target = {'task': False, 'labels': ['Stop canary agent'], 'kind': 'general-purpose',
                  'started': 1000, 'body': body}
        self.assertTrue(stop.detail_open(screen))
        self.assertEqual(stop.detail_facts(screen, target), (True, '2s', body))
        self.assertTrue(stop.fits(screen, target, 1002))

    def test_detail_footer_distinguishes_both_versions_from_the_list(self):
        detail = (self.FOLDER_327 / 'detail_agent.txt').read_text()
        for screen in (self.screen('agent_detail'), detail):
            with self.subTest(footer=screen.splitlines()[-1]):
                self.assertTrue(stop.detail_open(screen))
                self.assertTrue(stop.dialog_open(screen.replace(' · x to stop', '')))
                self.assertFalse(stop.list_navigation(screen))
        for screen in (self.screen('list'), (self.FOLDER_327 / 'list.txt').read_text()):
            with self.subTest(footer=screen.splitlines()[-1]):
                self.assertTrue(stop.list_navigation(screen))
                self.assertTrue(stop.dialog_open(screen))
                self.assertFalse(stop.detail_open(screen))
        for footer in (' · Enter/Space to close', ' · Esc to close'):
            with self.subTest(missing=footer):
                self.assertFalse(stop.detail_open(detail.replace(footer, '')))
        self.assertFalse(stop.detail_open(detail.replace(' · Esc to close', ' and Esc to close')))

    def test_2_1_327_list_ignores_chat_above_last_background_title(self):
        screen = (self.FOLDER_327 / 'list.txt').read_text()
        chat = 'Background\nShells\n● sleep 600  running\n'
        self.assertEqual(stop.rows(chat + screen), stop.rows(screen))

    def test_list_refuses_without_background_title(self):
        screen = (self.FOLDER_327 / 'list.txt').read_text().replace('  Background\n', '')
        self.assertEqual(stop.rows(screen), [])
        target = {'task': True, 'labels': ['sleep 600'], 'body': 'sleep 600'}
        with patch.object(stop, 'visible', side_effect=['❯ /tasks', screen]), patch.object(stop, 'press') as press:
            with self.assertRaisesRegex(stop.StopError, 'Could not identify that task'):
                stop.stop_selected({}, target, None, lambda _: None)
        self.assertEqual([call.args[1:] for call in press.call_args_list], [('-l', '/tasks'), ('Enter',)])

    def test_2_1_327_matching_shell_suffix_refuses_before_row_keys(self):
        screen = (self.FOLDER_327 / 'list_subagent_shell.txt').read_text()
        target = {'task': True, 'labels': ['sleep 600'], 'body': 'sleep 600'}
        for suffix in (' · general-purpose subagent', ' · general-purpose subag…', ' · unknown stopped', ' owned elsewhere', '·'):
            for only_match in (False, True):
                with self.subTest(suffix=suffix, only_match=only_match):
                    listed = screen.replace(' · general-purpose subagent', suffix)
                    if only_match:
                        listed = listed.replace('  ❯ ● sleep 600             running\n', '').replace(
                            '    ● sleep 600', '  ❯ ● sleep 600')
                    with patch.object(stop, 'visible', side_effect=['❯ /tasks', listed]), \
                            patch.object(stop, 'press') as press, \
                            self.assertRaisesRegex(stop.StopError, 'Could not identify that task'):
                        stop.stop_selected({}, target, None, lambda _: None)
                    self.assertEqual([call.args[1:] for call in press.call_args_list], [('-l', '/tasks'), ('Enter',)])

    def test_matching_monitor_suffix_appears_during_selection(self):
        screen = (self.FOLDER_327 / 'list.txt').read_text().replace('    Shells', '    Monitors').replace(
            '  ❯ ● Stop canary monitor', '    ● Stop canary monitor').replace('    ● sleep 600', '  ❯ ● sleep 600')
        changed = screen.replace('Stop canary monitor   running', 'Stop canary monitor   running · unknown')
        target = {'task': True, 'labels': ['Stop canary monitor'], 'body': 'sleep 599'}
        with patch.object(stop, 'visible', side_effect=['❯ /tasks', screen, changed]), \
                patch.object(stop, 'press') as press, self.assertRaisesRegex(stop.StopError, 'Could not identify that task'):
            stop.stop_selected({}, target, None, lambda _: None)
        self.assertEqual([call.args[1:] for call in press.call_args_list], [('-l', '/tasks'), ('Enter',), ('Up',)])

    def test_2_1_327_list_without_sections_distinguishes_session_tasks_from_child_shell(self):
        screen = (self.FOLDER_327 / 'list_no_sections.txt').read_text()
        rows = stop.rows(screen)
        self.assertEqual([(row['label'], row['section'], row['selected']) for row in rows], [
            ('sleep 601', 'Shells', True), ('Stop canary monitor', 'Shells', False), ('sleep 600', 'Shells', False),
        ])
        for label, matches in (('Stop canary monitor', [1]), ('sleep 600', [2]), ('sleep 601', [])):
            with self.subTest(label=label):
                target = {'task': True, 'labels': [label]}
                self.assertEqual([i for i, row in enumerate(rows) if stop.is_target(target, row)], matches)

    def test_shell_detail(self):
        target = {'task': True, 'labels': ['Fixture shell', 'sleep 401'], 'kind': 'shell', 'started': 1000.0,
                  'body': 'sleep 401'}
        self.assertEqual(stop.detail_facts(self.screen('shell_detail'), target), (True, '2m 45s', 'sleep 401'))
        self.assertTrue(stop.fits(self.screen('shell_detail'), target, 1165))
        self.assertFalse(stop.fits(self.screen('shell_detail'), target, 1400))
        self.assertFalse(stop.fits(self.screen('shell_detail'), dict(target, labels=['sleep 402'], body='sleep 402'), 1165))

    def test_monitor_detail_uses_script_line(self):
        target = {'task': True, 'labels': ['Fixture monitor', 'sleep 402'], 'kind': 'monitor', 'started': 1000.0,
                  'body': 'sleep 402'}
        self.assertEqual(stop.detail_facts(self.screen('monitor_detail'), target), (True, '2m 41s', 'sleep 402'))
        self.assertTrue(stop.fits(self.screen('monitor_detail'), target, 1161))

    def test_detail_views_on_2_1_330(self):
        for name, label, body, runtime, started in (
                ('shell_detail', 'Background sleep 600 for Stop test', 'sleep 600', '4s', 1000.0),
                ('monitor_detail', 'Stop canary monitor', 'sleep 599', '6s', 1000.0)):
            with self.subTest(name=name):
                screen = (self.FOLDER_330 / f'{name}.txt').read_text()
                target = {'task': True, 'labels': [label, body], 'kind': name.split('_')[0], 'started': started,
                          'body': body}
                self.assertEqual(stop.detail_facts(screen, target), (True, runtime, body))
                other = dict(target, labels=['sleep 1'], body='sleep 1')
                self.assertFalse(stop.detail_facts(screen, other)[0])

    def test_agent_detail(self):
        target = {'task': False, 'labels': ['Fixture wait 2'], 'kind': 'general-purpose', 'started': 1000.0,
                  'body': 'Run this Bash command in the foreground with timeout 400000: '
                          'python3 -c "import time; time.sleep(300)" . Then reply done.'}
        named, shown, body = stop.detail_facts(self.screen('agent_detail'), target)
        self.assertEqual((named, shown), (True, '2m 18s'))
        self.assertTrue(stop.fits(self.screen('agent_detail'), target, 1138))
        self.assertFalse(stop.fits(self.screen('agent_detail'), dict(target, body='Another prompt'), 1138))
        self.assertFalse(stop.fits(self.screen('agent_detail'), dict(target, kind='Explore'), 1138))

    def test_2_1_327_agent_detail_matches_the_name_kind_prompt_and_runtime(self):
        screen = (self.FOLDER_327 / 'detail_agent.txt').read_text()
        body = 'Run Bash `sleep 600` in the foreground with timeout 600000, then reply done.'
        target = {'task': False, 'labels': ['Stop canary agent'], 'kind': 'general-purpose',
                  'started': 1000.0, 'body': body}
        self.assertEqual(stop.detail_facts(screen, target), (True, '2s', body))
        self.assertTrue(stop.fits(screen, target, 1002))
        self.assertFalse(stop.fits(screen, target, 1010))
        for field, wrong in (('kind', 'Explore'), ('labels', ['Another agent']), ('body', 'Another prompt')):
            with self.subTest(field=field):
                self.assertFalse(stop.fits(screen, dict(target, **{field: wrong}), 1002))

    def test_2_1_327_shell_and_monitor_details_match_command_and_runtime(self):
        for name, label, command, runtime in (('detail_shell', 'sleep 600', 'sleep 600', 8),
                                              ('detail_monitor', 'Stop canary monitor', 'sleep 599', 10)):
            with self.subTest(name=name):
                screen = (self.FOLDER_327 / f'{name}.txt').read_text()
                self.assertTrue(stop.detail_open(screen))
                self.assertFalse(stop.list_navigation(screen))
                self.assertTrue(stop.has_action(screen, 'x to stop'))
                target = {'task': True, 'labels': [label, command], 'kind': '', 'started': 1000.0, 'body': command}
                self.assertEqual(stop.detail_facts(screen, target), (True, f'{runtime}s', command))
                self.assertTrue(stop.fits(screen, target, 1000 + runtime))
                self.assertFalse(stop.fits(screen, target, 1000 + runtime + 30))
                self.assertFalse(stop.fits(screen, dict(target, labels=['sleep 601'], body='sleep 601'), 1000 + runtime))


class AgentBarTests(unittest.TestCase):
    FOLDER = Path(__file__).parent / 'fixtures' / 'claude_2.1.330_agent_bar'
    NAME = 'Stop canary agent'
    ROW = '  ◯ general-purpose  waiting on 1 shell, 1 monitor · Stop canary agent · since 8:54 AM'

    def screen(self, name):
        return (self.FOLDER / f'{name}.txt').read_text()

    def setUp(self):
        self.agent = {'id': 'alpha', 'description': self.NAME, 'type': 'general-purpose',
                      'state': 'running', 'waiting': {'shell': 1, 'monitor': 1}}
        self.siblings = []
        self.keys = []
        self.reads = []
        self.verify = self.enterContext(patch.object(terminal, 'verify'))
        self.status = self.enterContext(patch.object(terminal, 'live_status', return_value={'state': 'Working'}))
        self.enterContext(patch.object(stop, 'claude_version', return_value='2.1.330'))
        # Refusals name the running version when it differs from the last version checked live.
        self.enterContext(patch.object(stop, 'CHECKED_TASKS_UI', '2.1.325'))

    def test_all_captured_focus_states(self):
        cases = {
            'prompt_waiting_shell_monitor': ('prompt', None, None),
            'prompt_waiting_working': ('prompt', None, None),
            'pill_focus': ('pill', None, None),
            'main_selected': ('row', 0, None),
            'agent_selected': ('row', 1, 'stop'),
            'agent_stopped': ('row', 1, 'clear'),
            'after_escape': ('prompt', None, None),
            'narrow_prompt_waiting': ('prompt', None, None),
            'narrow_pill_focus': ('pill', None, None),
            'narrow_main_selected': ('row', 0, None),
            'narrow_agent_selected': ('row', 1, 'stop'),
            'narrow_agent_stopped': ('row', 1, 'clear'),
            'narrow_after_escape': ('prompt', None, None),
            'after_stop_resume_footer': ('prompt', None, None),
            'pill_focus_after_stop': ('pill', None, None),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                screen = self.screen(name)
                bar = stop.agent_bar(screen)
                self.assertIsNotNone(bar)
                self.assertEqual((bar['focus'], bar['selected'], bar['action']), expected)
                self.assertFalse(bar['draft'])
                self.assertEqual(bar['rows'][0]['kind'], 'main')
                self.assertEqual(stop.agent_bar('❯ ● main\n' + self.ROW + '\n' + screen), bar)
        for name in ('agent_view_open', 'background_session_dialog'):
            with self.subTest(name=name):
                self.assertIsNone(stop.agent_bar(self.screen(name)))

    def test_waiting_identity_requires_supported_counts_and_complete_name(self):
        screen = self.screen('agent_selected')
        self.assertEqual(stop.agent_bar(screen)['rows'][1], {
            'kind': 'general-purpose', 'name': self.NAME, 'waiting': self.agent['waiting'],
        })
        for text in (screen.replace('1 monitor', '1 worker'), screen.replace('1 monitor', '2 shells'),
                     screen.replace(' · since 8:54 AM', '')):
            self.assertIsNone(stop.agent_bar(text)['rows'][1]['waiting'])
        narrow = self.screen('narrow_agent_selected')
        self.assertEqual(stop.agent_bar(narrow)['rows'][1], {
            'kind': 'general-purpose', 'name': 'Prob…', 'waiting': {'shell': 1, 'monitor': 0},
        })

    def drive(self, frames, fresh=None):
        """Each step is (visible frame, allowed key). An empty key allows only reads."""
        position = 0
        self.keys.clear()
        self.reads.clear()

        def run(args, **kwargs):
            nonlocal position
            screen, key = frames[position]
            if args[1] == 'capture-pane':
                self.reads.append(position)
                return subprocess.CompletedProcess(args, 0, screen, '')
            self.assertEqual(args[1], 'send-keys')
            self.assertEqual(args[4:], [key])
            self.assertTrue(self.reads and self.reads[-1] == position)
            self.keys.append(key)
            position += 1
            return subprocess.CompletedProcess(args, 0, '', '')

        def listing(entry):
            state = 'stopped' if 'x' in self.keys else self.agent['state']
            if fresh is not None and self.keys and state == 'running':
                fresh()
            return {'agents': [dict(self.agent, state=state), *self.siblings], 'tasks': []}

        entry = {'id': '%fake-bar', 'agent': 'claude'}
        with patch.object(subagents, 'listing', side_effect=listing):
            return stop.stop(entry, 'alpha', run=run, sleep=lambda _: None)

    def wide_frames(self):
        return [(self.screen(name), key) for name, key in (
            ('prompt_waiting_shell_monitor', 'Down'), ('pill_focus', 'Down'), ('main_selected', 'Down'),
            ('agent_selected', 'x'), ('agent_stopped', 'Escape'), ('after_escape', None),
        )]

    def narrow_frames(self):
        self.agent.update(description='Probe agent', waiting={'shell': 1, 'monitor': 0})
        return [(self.screen('narrow_' + name), key) for name, key in (
            ('prompt_waiting', 'Down'), ('pill_focus', 'Down'), ('main_selected', 'Down'),
            ('agent_selected', 'x'), ('agent_stopped', 'Escape'), ('after_escape', None),
        )]

    def test_driver_writes_only_verified_keys(self):
        for frames in (self.wide_frames, self.narrow_frames):
            with self.subTest(frames=frames.__name__):
                self.assertEqual(self.drive(frames()), {'ok': True, 'state': 'stopped'})
                self.assertEqual(self.keys, ['Down', 'Down', 'Down', 'x', 'Escape'])
                self.assertEqual(self.reads.count(3), 2)  # Fresh screen before x.

    def test_cleanup_accepts_the_resume_footer_after_a_stop(self):
        # Live 2.1.330: after x and Escape the prompt footer says "↓ to resume subagent".
        frames = self.wide_frames()
        frames[-1] = (self.screen('after_stop_resume_footer'), None)
        self.assertEqual(self.drive(frames), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.keys, ['Down', 'Down', 'Down', 'x', 'Escape'])

    def test_closes_a_recognized_tasks_panel_before_navigating_the_bar(self):
        tasks = RealScreenTests.FOLDER_330 / 'list_subagent_shell.txt'
        frames = [(tasks.read_text(), 'Escape'), *self.wide_frames()]
        self.assertEqual(self.drive(frames), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.keys, ['Escape', 'Down', 'Down', 'Down', 'x', 'Escape'])

    def test_refusals_at_prompt_press_no_keys(self):
        prompt = self.screen('prompt_waiting_shell_monitor')
        cases = (
            (prompt + self.ROW + '\n', 'share this name'),
            (prompt.replace('waiting on 1 shell, 1 monitor', 'waiting on 2 shells'), 'Could not identify'),
            (self.screen('agent_view_open'), 'Could not read'),
            (self.screen('background_session_dialog'), 'Could not read'),
            (prompt.replace('\n❯\n', '\n❯ unsent text\nmore text\n'), 'unsent draft'),
            (prompt.replace('↓ to manage', '↓ to manage the terminal'), 'Could not read'),
            (prompt.replace('↓ to manage', '↓ t…'), 'Could not read'),
        )
        for screen, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(stop.StopError, message):
                    self.drive([(screen, None)])
                self.assertEqual(self.keys, [])

    def test_truncated_name_refuses_a_second_row(self):
        frames = self.narrow_frames()
        prompt, _ = frames[0]
        with self.assertRaisesRegex(stop.StopError, 'share this name'):
            self.drive([(prompt + '  ◯ general-purpose  waiting on 1 shell · Pro…\n', None)])
        self.assertEqual(self.keys, [])

    def test_truncated_name_refuses_a_second_running_listed_agent(self):
        frames = self.narrow_frames()
        self.siblings = [dict(self.agent, id='beta', description='Probe two')]
        frames[3:] = [(frames[3][0], 'Escape'), (frames[0][0], None)]
        with self.assertRaisesRegex(stop.StopError, 'share this name.*Claude Code 2.1.330'):
            self.drive(frames)
        self.assertEqual(self.keys, ['Down', 'Down', 'Down', 'Escape'])

    def test_refusals_during_navigation_close_recognized_focus(self):
        frames = self.wide_frames()
        selected, _ = frames[3]
        other = '❯ ◯ general-purpose  waiting on 1 shell · A different agent · since 8:54 AM'
        stopped, _ = frames[4]
        cases = (
            (selected.replace('❯ ◯ general-purpose', '  ◯ general-purpose') + other + '\n', 'Could not reach'),
            (selected.replace('❯ ◯ general-purpose  waiting on 1 shell, 1 monitor · ' + self.NAME
                              + ' · since 8:54 AM', other), 'Could not identify'),
            (stopped.replace('  ● main\n', '  ● main\n' + self.ROW + '\n'), 'Could not reach'),
            (selected.replace('\n❯\n', '\n❯ New draft\n'), 'unsent draft'),
        )
        for changed, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(stop.StopError, message):
                self.drive([*frames[:3], (changed, 'Escape'), frames[-1]])
            self.assertEqual(self.keys, ['Down', 'Down', 'Down', 'Escape'])

    def test_unknown_selection_does_not_send_escape_or_x(self):
        frames = self.wide_frames()
        for action in ('x to stop all', 'x to stop · Esc to cancel'):
            unknown = frames[3][0].replace('x to stop', action)
            with self.subTest(action=action), self.assertRaisesRegex(
                    stop.StopError, 'agent bar did not close.*Claude Code 2.1.330'):
                self.drive([*frames[:3], (unknown, None)])
            self.assertEqual(self.keys, ['Down', 'Down', 'Down'])

    def test_changed_snapshot_immediately_before_x_refuses(self):
        frames = self.wide_frames()
        frames[3:] = [(frames[3][0], 'Escape'), frames[-1]]

        def redraw():
            frames[3] = (frames[3][0].replace('\n❯\n', '\n❯ New draft\n'), 'Escape')

        with self.assertRaisesRegex(stop.StopError, 'unsent draft'):
            self.drive(frames, fresh=redraw)
        self.assertEqual(self.keys, ['Down', 'Down', 'Down', 'Escape'])

    def test_navigation_stops_after_eight_down_keys(self):
        prompt = self.screen('prompt_waiting_shell_monitor')
        with self.assertRaisesRegex(stop.StopError, 'Could not reach that agent'):
            self.drive([(prompt, 'Down')] * 8 + [(prompt, None)])
        self.assertEqual(self.keys, ['Down'] * 8)

    def test_cleanup_stops_after_three_escape_keys(self):
        frames = self.wide_frames()
        stopped = frames[4][0]
        with self.assertRaisesRegex(stop.StopError, 'agent bar did not close'):
            self.drive([*frames[:4], (stopped, 'Escape'), (stopped, 'Escape'),
                        (stopped, 'Escape'), (stopped, None)])
        self.assertEqual(self.keys, ['Down', 'Down', 'Down', 'x', 'Escape', 'Escape', 'Escape'])


if __name__ == '__main__':
    unittest.main()
