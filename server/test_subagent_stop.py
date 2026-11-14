"""All terminal writes use a fake tmux runner and isolated transcript files."""
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
        self.assertEqual([row['section'] for row in stop.rows('1 active agent\n❯ ● Quiet wait  running · Haiku 5.5\n')],
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
        screen = ('● Before section\nShells (2)\n❯ ● Shell one running\n'
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
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Down',), ('x',), ('Escape',)])
        self.verify.assert_called_once_with(self.entry, self.pane)
        self.assertFalse(self.pane.needs_read)

    def test_list_moves_across_shells_to_second_agent(self):
        self.pane.shells = ['First shell', 'Second shell']
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',),
                                         ('Down',), ('Down',), ('Down',), ('x',), ('Escape',)])

    def test_shell_with_matching_label_is_not_a_target(self):
        self.pane.shells = ['Alpha wait']
        self.assertEqual(self.stop(), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',),
                                         ('Down',), ('Down',), ('x',), ('Escape',)])

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
        self.pane.after_move = ('Local agents (1)\n❯ ● Different task    running · Haiku 5.5\n' + LIST_HINT)
        with self.assertRaisesRegex(stop.StopError, 'Could not identify'):
            self.stop()
        self.assertNotIn(('x',), self.pane.keys)
        self.assertEqual(self.pane.keys[-1], ('Escape',))

    def test_navigation_is_bounded_at_40_keys(self):
        self.pane.after_move = (
            'Local agents (2)\n❯ ● Beta wait    running · Haiku 5.5\n'
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


class ProcessStopTests(unittest.TestCase):
    setUp = StopTests.setUp
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
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Up',), ('x',), ('Escape',)])

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
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Down',), ('x',), ('Escape',)])

    def test_detail_opened_at_once_for_another_task_goes_back_to_list(self):
        self.make_tasks()
        self.pane.names = []
        self.pane.at = 0
        self.pane.detail = True
        self.pane.viewing = 0  # /tasks opened the first shell's detail; the target is the second.
        self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(self.pane.keys, [('-l', '/tasks'), ('Enter',), ('Left',), ('Down',), ('x',), ('Escape',)])

    def test_task_with_exact_process_stops_by_signal_without_terminal_keys(self):
        self.make_tasks()
        signals = []

        def kill(pid, sig):
            signals.append((pid, sig))
            self.output.write_text('output\n[exited with code 143]\n')

        # /tasks refuses (an unsent draft), so the signal fallback stops the task.
        self.pane.draft = 'Keep draft'
        with patch.object(stop.task_process, 'find', return_value=4242) as find, \
                patch.object(stop.task_process, 'stop', side_effect=lambda pid: kill(pid, 15) or True):
            self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertEqual(find.call_args.args[2], os.path.realpath(self.output))
        self.assertEqual(signals, [(4242, 15)])
        self.assertEqual(self.pane.keys, [])
        task = subagents.listing(self.entry)['tasks'][0]
        self.assertEqual((task['state'], task['summary']), ('stopped', 'Stopped from Tether.'))

    def test_task_process_that_will_not_die_reports_error(self):
        self.make_tasks()
        self.pane.draft = 'Keep draft'
        with patch.object(stop.task_process, 'find', return_value=4242), \
                patch.object(stop.task_process, 'stop', return_value=False):
            with self.assertRaisesRegex(stop.StopError, 'The task did not stop'):
                self.stop('shell02')
        self.assertEqual(self.pane.keys, [])

    def test_tasks_screen_stop_never_sends_a_signal(self):
        self.make_tasks()
        with patch.object(stop.task_process, 'find', return_value=4242), \
                patch.object(stop.task_process, 'stop') as signal_stop:
            self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        signal_stop.assert_not_called()
        self.assertIn(('x',), self.pane.keys)

    def test_unreadable_tasks_screen_falls_back_to_signal(self):
        self.make_tasks()
        self.pane.shells = ['sleep 600', 'Second wait']
        self.pane.shell_details = {0: (None, 'sleep 600'), 1: (None, 'sleep 600')}
        with patch.object(stop.task_process, 'find', return_value=4242), \
                patch.object(stop.task_process, 'stop',
                             side_effect=lambda pid: self.output.write_text('[exited with code 143]') or True):
            self.assertEqual(self.stop('shell02'), {'ok': True, 'state': 'stopped'})
        self.assertNotIn(('x',), self.pane.keys)

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

    def screen(self, name):
        return (self.FOLDER / f'{name}.txt').read_text()

    def test_list_reads_shells_monitor_and_agent_sections(self):
        self.assertEqual([(row['label'], row['selected'], row['section']) for row in stop.rows(self.screen('list'))], [
            ('Fixture monitor', True, 'Shells'), ('sleep 401', False, 'Shells'), ('Fixture wait 2', False, 'Local agents')])
        self.assertEqual([row['selected'] for row in stop.rows(self.screen('list_agent_selected'))], [False, False, True])

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

    def test_agent_detail(self):
        target = {'task': False, 'labels': ['Fixture wait 2'], 'kind': 'general-purpose', 'started': 1000.0,
                  'body': 'Run this Bash command in the foreground with timeout 400000: '
                          'python3 -c "import time; time.sleep(300)" . Then reply done.'}
        named, shown, body = stop.detail_facts(self.screen('agent_detail'), target)
        self.assertEqual((named, shown), (True, '2m 18s'))
        self.assertTrue(stop.fits(self.screen('agent_detail'), target, 1138))
        self.assertFalse(stop.fits(self.screen('agent_detail'), dict(target, body='Another prompt'), 1138))
        self.assertFalse(stop.fits(self.screen('agent_detail'), dict(target, kind='Explore'), 1138))


if __name__ == '__main__':
    unittest.main()
