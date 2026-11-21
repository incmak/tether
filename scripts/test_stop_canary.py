"""Run the canary against real 2.1.325 screens and throwaway Claude records."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stop_canary as canary  # noqa: E402

FIXTURE = canary.FIXTURES / 'claude_2.1.325_tasks'
NO_SECTIONS = canary.FIXTURES / 'claude_2.1.327_tasks' / 'list_no_sections.txt'
BAR_FIXTURE = canary.FIXTURES / 'claude_2.1.330_agent_bar'
NO_SECTIONS_330 = canary.FIXTURES / 'claude_2.1.330_tasks' / 'list_no_sections.txt'
VERSION = '2.1.327'
NOW = 1_800_000_000
PID = 4242
AGENT = 'Fixture wait 2'
MONITOR = 'Fixture monitor'
COMMANDS = {'shell': 'sleep 401', 'monitor': 'sleep 402'}
IDS = {'agent': 'cagent', 'shell': 'bshell', 'monitor': 'bwatch'}
PROMPT = ('Run this Bash command in the foreground with timeout 400000: '
          'python3 -c "import time; time.sleep(300)" . Then reply done.')
ROW_ORDER = ['monitor', 'shell', 'agent']
SCREEN_FILES = {'list.txt', 'list_agent_selected.txt', *(f'{kind}_detail.txt' for kind in canary.KINDS)}


def timestamp(age):
    return datetime.fromtimestamp(NOW - age, timezone.utc).isoformat()


class Replay:
    """Emulate the pane and the records its three tasks write."""
    list_without_sections = NO_SECTIONS

    def __init__(self, root, change=None, unconfirmed=None, row_order=ROW_ORDER):
        self.root = root
        self.change = change
        self.unconfirmed = unconfirmed
        self.row_order = row_order
        self.keys = []
        self.frames = []
        self.opened = []
        self.stopped = []
        self.closed = False

    @contextmanager
    def session(self):
        thread = str(uuid.uuid4())
        projects = self.root / 'projects'
        agents = projects / 'work' / thread / 'subagents'
        agents.mkdir(parents=True)
        self.transcript = agents.parent.with_suffix('.jsonl')
        self.meta = agents / f'agent-{IDS["agent"]}.meta.json'
        self.agent_path = self.meta.with_name(f'agent-{IDS["agent"]}.jsonl')
        self.agent_path.write_text(json.dumps({
            'type': 'user', 'timestamp': timestamp(138), 'message': {'content': PROMPT},
        }) + '\n')
        self.meta.write_text(json.dumps({'description': AGENT, 'agentType': 'general-purpose',
                                         'requestShape': 'background'}))
        tasks_root = self.root / 'tasks'
        tasks = tasks_root / 'work' / thread / 'tasks'
        tasks.mkdir(parents=True)
        self.output = tasks / f'{IDS["shell"]}.output'
        self.output.touch()
        registry = self.root / 'registry.json'
        registry.write_text(json.dumps({'pid': PID, 'sessionId': thread, 'version': VERSION}))
        for kind, tool, age in (('shell', 'Bash', 165), ('monitor', 'Monitor', 161)):
            self.append({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': kind, 'name': tool,
                 'input': {'command': COMMANDS[kind], 'description': MONITOR if kind == 'monitor' else 'Fixture shell'}},
            ]}})
            self.append({'type': 'user', 'timestamp': timestamp(age),
                         'toolUseResult': {'backgroundTaskId': IDS[kind]},
                         'message': {'content': [{'type': 'tool_result', 'tool_use_id': kind}]}})
        self.mode, self.selection = 'prompt', 0
        self.entry = {'id': '%1', 'owner_pid': PID, 'process_start': str(self.root), 'agent': 'claude',
                      'thread': thread, 'writable': True, 'registry': registry, 'transcript': str(self.transcript)}
        try:
            with patch.multiple(canary.subagents, TASKS_TMP=tasks_root, PROJECTS=projects), \
                    patch.object(canary.subagents, '_owner_since', return_value=NOW - 500), \
                    patch.object(canary.subagents, '_fd_links', return_value=[]), \
                    patch.object(canary.procinfo, 'start_ticks', return_value=str(self.root)), \
                    patch.object(canary.procinfo, 'comm', return_value='claude'), \
                    patch.object(canary.task_process, 'children', return_value=[PID + 1]), \
                    patch.object(canary.task_process, 'running', return_value=True), \
                    patch.object(canary.stop, 'bind', side_effect=AssertionError('unexpected process fallback')):
                yield self.entry
        finally:
            self.closed = True

    def append(self, record):
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    listing = staticmethod(canary.subagents.listing)

    def screen(self):
        if self.mode == 'prompt':
            return '─────────────────────\n❯ \n─────────────────────\n'
        if self.mode == 'typed':
            return '─────────────────────\n❯ /tasks\n─────────────────────\n'
        kind = self.row_order[self.selection]
        if self.mode == 'detail':
            name = f'{kind}_detail'
            screen = (FIXTURE / f'{name}.txt').read_text()
        elif 'agent' not in self.row_order:
            name = 'list'
            screen = self.list_without_sections.read_text().replace('❯ ●', '  ●')
            selected = {'subagent_shell': 'sleep 601', 'monitor': 'Stop canary monitor', 'shell': 'sleep 600'}
            screen = screen.replace(f'  ● {selected[kind]}', f'❯ ● {selected[kind]}')
            screen = screen.replace('sleep 600', COMMANDS['shell']).replace('Stop canary monitor', MONITOR)
        else:
            name = 'list_agent_selected' if kind == 'agent' else 'list'
            screen = (FIXTURE / f'{name}.txt').read_text()
            if kind == 'shell':
                screen = screen.replace('❯ ● Fixture monitor', '  ● Fixture monitor').replace(
                    '  ● sleep 401', '❯ ● sleep 401')
            if 'subagent_shell' in self.row_order:
                if kind == 'subagent_shell':
                    screen = screen.replace('❯ ● Fixture monitor', '  ● Fixture monitor')
                mark = '❯' if kind == 'subagent_shell' else ' '
                screen = screen.replace('Shells (2)\n', f'Shells (3)\n   {mark} ● sleep 601  running · general-purpose subagent\n')
                screen = screen.replace('2 active shells', '3 active shells')
        return self.change(name, screen) if self.change else screen

    def run(self, args, **kwargs):
        if args[1] == 'display-message':
            return subprocess.CompletedProcess(args, 0, f'{PID}\tclaude\t0\n')
        if args[1] == 'capture-pane':
            screen = self.screen()
            self.frames.append(screen)
            return subprocess.CompletedProcess(args, 0, screen)
        if args[1] != 'send-keys' or args[2:4] != ['-t', self.entry['id']]:
            raise AssertionError(args)
        keys = tuple(args[4:])
        self.keys.append(keys)
        kind = self.row_order[self.selection]
        if keys == ('-l', '/tasks') and self.mode == 'prompt':
            self.mode = 'typed'
        elif keys == ('Enter',) and self.mode == 'typed':
            self.mode, self.selection = 'list', 0
        elif keys == ('Enter',) and self.mode == 'list':
            self.opened.append((kind, tuple(self.stopped)))
            self.mode = 'detail'
        elif keys in (('Up',), ('Down',)) and self.mode == 'list':
            self.selection += 1 if keys == ('Down',) else -1
        elif keys == ('Escape',) and self.mode in ('detail', 'list'):
            self.mode = 'prompt'
        elif keys == ('x',) and self.mode == 'detail':
            self.stopped.append(kind)
            if kind == self.unconfirmed:
                pass
            elif kind == 'agent':
                self.meta.write_text(json.dumps({'description': AGENT, 'stoppedByUser': True}))
            elif kind == 'shell':
                self.output.write_text('[killed]\n')
            else:
                event = f'<task-notification><task-id>{IDS[kind]}</task-id><status>killed</status></task-notification>'
                self.append({'type': 'user', 'message': {'content': event}})
        else:
            raise AssertionError((self.mode, keys))
        return subprocess.CompletedProcess(args, 0)


class WaitingReplay(Replay):
    """Replay the 2.1.330 transition to waiting and the agent-bar Stop."""
    list_without_sections = NO_SECTIONS_330

    def __init__(self, root, unconfirmed=None):
        super().__init__(root, unconfirmed=unconfirmed, row_order=['subagent_shell', 'monitor', 'shell'])
        self.bar_keys = []
        self.preflight = []

    @contextmanager
    def session(self):
        with super().session() as entry:
            registry = json.loads(entry['registry'].read_text())
            entry['registry'].write_text(json.dumps(dict(registry, version='2.1.330')))
            child = 'bchild'
            self.child_output = self.output.with_name(f'{child}.output')
            self.child_output.touch()
            with self.agent_path.open('a') as stream:
                for record in (
                    {'type': 'assistant', 'message': {'content': [
                        {'type': 'tool_use', 'name': 'Bash', 'id': 'child', 'input': {'command': 'sleep 601'}},
                    ]}},
                    {'type': 'user', 'timestamp': timestamp(137), 'toolUseResult': {'backgroundTaskId': child},
                     'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'child'}]}},
                ):
                    stream.write(json.dumps(record) + '\n')
            yield entry

    def listing(self, entry):
        listed = super().listing(entry)
        self.preflight.append('waiting' in listed['agents'][0])
        if len(self.preflight) == 1:
            self.meta.write_text(json.dumps(dict(json.loads(self.meta.read_text()), finished=True)))
        return listed

    def screen(self):
        name = {'prompt': 'prompt_waiting_working', 'bar_pill': 'pill_focus', 'bar_main': 'main_selected',
                'bar_agent': 'agent_selected', 'bar_stopped': 'agent_stopped'}.get(self.mode)
        if name is not None:
            screen = (BAR_FIXTURE / f'{name}.txt').read_text()
            if self.mode == 'prompt' and 'agent' in self.stopped:
                screen = (BAR_FIXTURE / 'after_escape.txt').read_text()
                stopped = (BAR_FIXTURE / 'agent_stopped.txt').read_text().splitlines()[-1]
                screen = '\n'.join([*screen.splitlines()[:-1], stopped.replace('❯', ' ', 1)]) + '\n'
            return screen.replace('Stop canary agent', AGENT).replace('Probe agent', AGENT).replace(
                'waiting on 1 shell, 1 monitor', 'waiting on 1 shell')
        screen = super().screen()
        if self.mode == 'list' and 'subagent_shell' not in self.row_order:
            screen = '\n'.join(line for line in screen.splitlines() if '● sleep 601' not in line) + '\n'
            screen = screen.replace('2 shells, 1 monitor', '1 shell, 1 monitor')
        return screen

    def run(self, args, **kwargs):
        if args[1] == 'send-keys':
            keys = tuple(args[4:])
            next_mode = {
                ('prompt', ('Down',)): 'bar_pill',
                ('bar_pill', ('Down',)): 'bar_main',
                ('bar_main', ('Down',)): 'bar_agent',
                ('bar_agent', ('x',)): 'bar_stopped',
                ('bar_stopped', ('Escape',)): 'prompt',
            }.get((self.mode, keys))
            if next_mode:
                self.keys.append(keys)
                self.bar_keys.append(keys[0])
                self.mode = next_mode
                if keys == ('x',):
                    self.stopped.append('agent')
                    if self.unconfirmed != 'agent':
                        self.meta.write_text(json.dumps(dict(json.loads(self.meta.read_text()), stoppedByUser=True)))
                        self.child_output.write_text('[killed]\n')
                        self.row_order = ['monitor', 'shell']
                        self.selection = 0
                return subprocess.CompletedProcess(args, 0)
        return super().run(args, **kwargs)


class StopCanaryTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixtures = root / 'fixtures'
        self.fixtures.mkdir()
        shutil.copytree(FIXTURE, self.fixtures / FIXTURE.name)
        self.report = root / 'report.json'
        self.state = root / 'last-passed'
        self.replay = None
        self.enterContext(patch.multiple(canary, AGENT_NAME=AGENT, MONITOR_NAME=MONITOR,
                                         SHELL_COMMAND=COMMANDS['shell'], MONITOR_COMMAND=COMMANDS['monitor']))

    def run_canary(self, change=None, *flags, sleep=lambda _: None, unconfirmed=None, version=VERSION,
                   row_order=ROW_ORDER):
        def factory(root):
            if version == '2.1.330':
                self.replay = WaitingReplay(root, unconfirmed)
            else:
                self.replay = Replay(root, change, unconfirmed, row_order)
            return self.replay

        output = io.StringIO()
        with redirect_stdout(output):
            code = canary.main(['--json', str(self.report), '--if-changed', str(self.state), *flags],
                               version_probe=lambda: version, adapter_factory=factory, fixtures=self.fixtures,
                               sleep=sleep, clock=lambda: NOW)
        return code, output.getvalue()

    def test_replays_all_stop_actions_and_writes_versioned_fixtures(self):
        self.assertEqual(self.run_canary(None, '--save-fixtures')[0], 0)
        report = json.loads(self.report.read_text())
        self.assertTrue(report['passed'])
        self.assertTrue(report['screens_parsed'])
        self.assertNotIn('screens_dir', report)
        self.assertFalse(self.report.with_name('report-screens').exists())
        self.assertEqual(report['baseline'], FIXTURE.name)
        self.assertEqual(report['diffs'], {})
        self.assertEqual(report['tasks'], {kind: {'passed': True, 'state': 'stopped'} for kind in canary.KINDS})
        self.assertEqual(self.replay.opened[:3], [(kind, ()) for kind in canary.KINDS])
        self.assertEqual(self.replay.stopped, list(canary.KINDS))
        self.assertEqual(self.replay.keys.count(('x',)), 3)
        saved = self.fixtures / f'claude_{VERSION}_tasks'
        self.assertEqual({path.name for path in saved.iterdir()}, SCREEN_FILES)
        for name in SCREEN_FILES:
            self.assertEqual((saved / name).read_text(), (FIXTURE / name).read_text())
        self.assertEqual(self.state.read_text(), VERSION + '\n')
        self.assertTrue(self.replay.closed)
        self.assertFalse(self.replay.root.exists())

    def test_waiting_agent_uses_the_bar_and_saves_its_parsed_screens(self):
        shutil.copytree(BAR_FIXTURE, self.fixtures / BAR_FIXTURE.name)
        code, output = self.run_canary(None, '--save-fixtures', version='2.1.330')
        self.assertEqual(code, 0, output)
        report = json.loads(self.report.read_text())
        self.assertTrue(report['screens_parsed'])
        self.assertEqual(report['agent_bar_baseline'], BAR_FIXTURE.name)
        self.assertEqual(report['tasks'], {kind: {'passed': True, 'state': 'stopped'} for kind in canary.KINDS})
        self.assertEqual(self.replay.preflight, [False, True])
        self.assertEqual(self.replay.bar_keys, ['Down', 'Down', 'Down', 'x', 'Escape'])
        self.assertEqual(self.replay.opened, [
            ('shell', ()), ('monitor', ()), ('shell', ('agent',)), ('monitor', ('agent', 'shell')),
        ])
        self.assertEqual(self.replay.stopped, list(canary.KINDS))
        self.assertEqual(self.replay.keys.count(('x',)), 3)
        for name in ('prompt_waiting_shell', 'pill_focus', 'main_selected',
                     'agent_selected', 'agent_stopped', 'after_escape'):
            with self.subTest(name=name):
                saved = self.fixtures / BAR_FIXTURE.name / f'{name}.txt'
                self.assertIn(saved.read_text(), self.replay.frames)
                self.assertIn(f'agent_bar/{name}.txt', report['diffs'])
        saved_tasks = self.fixtures / 'claude_2.1.330_tasks'
        self.assertEqual({path.name for path in saved_tasks.iterdir()}, {'list.txt', 'shell_detail.txt', 'monitor_detail.txt'})

    def test_same_version_skips_quietly_without_starting_tmux(self):
        self.state.write_text(VERSION + '\n')
        self.assertEqual(self.run_canary(), (0, ''))
        self.assertIsNone(self.replay)
        self.assertFalse(self.report.exists())

    def test_replays_stops_with_the_new_footers_and_subagent_shell(self):
        def change(name, screen):
            if name.startswith('list'):
                footer = ('↑/↓ to navigate · x to stop · f to foreground · Enter to view · Esc to close'
                          if name == 'list_agent_selected' else '↑/↓ to navigate · x to stop · Enter to view · Esc to close')
                screen = screen.split('↑/↓ to select', 1)[0] + footer + '\n'
                return screen.replace('Shells (3)', 'Shells').replace('Local agents (1)', 'Background agents')
            if name == 'agent_detail':
                screen = screen.replace(f'general-purpose › {AGENT}\n   ', f'{AGENT}\n   general-purpose · ')
            return screen.replace('Esc/Enter/Space to close', 'Enter/Space to close · Esc to close')

        self.assertEqual(self.run_canary(change, row_order=['subagent_shell', *ROW_ORDER])[0], 0)
        self.assertEqual(self.replay.opened[:3], [(kind, ()) for kind in canary.KINDS])
        self.assertEqual(self.replay.stopped, list(canary.KINDS))

    def test_agent_missing_from_list_leaves_shell_and_monitor_eligible_to_stop(self):
        without_agent = ['subagent_shell', 'monitor', 'shell']

        def hide_agent_after_inspection(name, screen):
            if name == 'list' and self.replay.opened and 'agent' in self.replay.row_order:
                self.replay.row_order = without_agent
                return self.replay.screen()
            return screen

        for hides_after_inspection in (False, True):
            with self.subTest(hides_after_inspection=hides_after_inspection):
                code, output = self.run_canary(hide_agent_after_inspection,
                                               row_order=ROW_ORDER if hides_after_inspection else without_agent)
                self.assertEqual(code, 1)
                report = json.loads(self.report.read_text())
                reason = (f'Claude Code {VERSION} does not list this subagent in /tasks '
                          '(it waits on a background shell).')
                self.assertIn(reason, output)
                self.assertEqual(report['tasks'], {
                    'agent': {'passed': False, 'error': reason},
                    'shell': {'passed': True, 'state': 'stopped'},
                    'monitor': {'passed': True, 'state': 'stopped'},
                })
                self.assertEqual(report['screens_parsed'], hides_after_inspection)
                self.assertFalse(self.state.exists())
                self.assertEqual(self.replay.stopped, ['shell', 'monitor'])
                inspected = ['agent'] if hides_after_inspection else []
                self.assertEqual([kind for kind, _ in self.replay.opened],
                                 [*inspected, 'shell', 'monitor', 'shell', 'monitor'])

    def test_failed_detail_keeps_every_frame_in_order_including_text_above_dialog(self):
        email = '@'.join(('person', 'example' + '.com'))
        before = f'Heading /private/example/log and {email}\n'

        def change(name, screen):
            if name == 'agent_detail':
                return before + screen.replace('Esc/Enter/Space to close', 'Ctrl+Esc/Enter/Space to close')
            return screen

        self.assertEqual(self.run_canary(change)[0], 1)
        report = json.loads(self.report.read_text())
        self.assertIn('dialog changed', report['tasks']['agent']['error'])
        self.assertEqual(self.replay.stopped, ['shell', 'monitor'])
        directory = Path(report['screens_dir'])
        self.assertEqual(directory, self.report.with_name('report-screens'))
        names = report['screens']
        width = max(2, len(str(len(self.replay.frames))))
        self.assertEqual([name.split('-', 1)[0] for name in names],
                         [f'{index:0{width}}' for index in range(1, len(self.replay.frames) + 1)])
        self.assertTrue(names[0].endswith('-preflight.txt'))
        self.assertTrue(any(name.endswith('-inspect-agent.txt') for name in names))
        frames = [(directory / name).read_text() for name in names]
        self.assertEqual(frames, [frame.replace(before, 'Heading ~/x and [email]\n') for frame in self.replay.frames])
        self.assertTrue(any('Ctrl+Esc/Enter/Space to close' in frame for frame in frames))

    def test_stop_confirmation_failure_keeps_frames_from_the_real_stop_path(self):
        self.assertEqual(self.run_canary(unconfirmed='shell')[0], 1)
        report = json.loads(self.report.read_text())
        self.assertTrue(report['screens_parsed'])
        self.assertIn('did not stop', report['tasks']['shell']['error'])
        names = report['screens']
        self.assertTrue(any(name.endswith('-stop-shell.txt') for name in names))
        self.assertTrue(names[-1].endswith('-stop-monitor.txt'))
        self.assertEqual([(Path(report['screens_dir']) / name).read_text() for name in names], self.replay.frames)

    def test_unexpected_stop_error_keeps_frames_already_captured(self):
        with patch.object(canary.stop, 'stop', side_effect=KeyError('unexpected task field')):
            self.assertEqual(self.run_canary()[0], 1)
        report = json.loads(self.report.read_text())
        self.assertIn('KeyError', report['error'])
        self.assertTrue(report['screens_parsed'])
        self.assertEqual(len(report['screens']), len(self.replay.frames))
        self.assertTrue(report['screens'][-1].endswith('-inspect-monitor.txt'))

    def test_changed_list_refuses_before_any_row_key(self):
        self.state.write_text('2.1.325\n')
        self.assertEqual(self.run_canary(lambda name, screen: screen.replace('●', '◆')
                                        if name.startswith('list') else screen, '--save-fixtures')[0], 1)
        report = json.loads(self.report.read_text())
        self.assertFalse(report['passed'])
        self.assertFalse(report['screens_parsed'])
        for result in report['tasks'].values():
            self.assertEqual(result['error'], 'Could not verify one selected row in the /tasks list.')
        self.assertIn('list.txt', report['diffs'])
        self.assertEqual(self.replay.keys, [('-l', '/tasks'), ('Enter',), ('Escape',)] * 3)
        self.assertEqual(self.state.read_text(), '2.1.325\n')
        self.assertFalse((self.fixtures / f'claude_{VERSION}_tasks').exists())

    def test_two_selected_rows_refuse_before_any_row_key(self):
        def change(name, screen):
            return screen.replace(f'  ● {AGENT}', f'❯ ● {AGENT}') if name == 'list' else screen

        self.assertEqual(self.run_canary(change)[0], 1)
        self.assertEqual(self.replay.keys, [('-l', '/tasks'), ('Enter',), ('Escape',)] * 3)

    def test_duplicate_shell_row_refuses_shell_before_view_or_stop(self):
        for appears_at_stop in (False, True):
            with self.subTest(appears_at_stop=appears_at_stop):
                def change(name, screen):
                    if not name.startswith('list') or appears_at_stop and not self.replay.stopped:
                        return screen
                    return screen.replace('       Earlier', '     Shells\n     ● sleep 401   running\n\n       Earlier')

                self.assertEqual(self.run_canary(change)[0], 1)
                report = json.loads(self.report.read_text())
                self.assertIn('Found 2 shell rows in /tasks. Expected one.', report['tasks']['shell']['error'])
                self.assertEqual(self.replay.stopped, ['agent', 'monitor'])
                self.assertEqual([kind for kind, _ in self.replay.opened].count('shell'), int(appears_at_stop))

    def test_changed_detail_refuses_only_that_task(self):
        def change(name, screen):
            return screen.replace('sleep 402', 'sleep 403') if name == 'monitor_detail' else screen

        self.assertEqual(self.run_canary(change)[0], 1)
        self.assertEqual(self.replay.stopped, ['agent', 'shell'])
        report = json.loads(self.report.read_text())
        self.assertIn('Could not identify that task', report['tasks']['monitor']['error'])
        self.assertIn('sleep 403', report['diffs']['unparsed_monitor.txt'])
        for kind in ('agent', 'shell'):
            self.assertTrue(report['tasks'][kind]['passed'])
        self.assertFalse(self.state.exists())

    def test_action_hint_must_match_before_a_row_key_or_stop(self):
        for name, hint, replacement in (('list', 'Enter to view', 'Enter to stop'),
                                        ('list', 'Enter to view', 'Shift+Enter to view'),
                                        ('list', '↑/↓ to select', 'Ctrl+↑/↓ to select'),
                                        ('list', '↑/↓ to select', '↑/↓ to navigate list'),
                                        ('list', 'x to stop', 'Ctrl+x to stop'),
                                        ('agent_detail', 'x to stop', 'k to stop'),
                                        ('agent_detail', 'x to stop', 'Ctrl+x to stop')):
            with self.subTest(screen=name, action=replacement):
                def change(screen_name, screen):
                    return screen.replace(hint, replacement) if screen_name == name else screen

                self.assertEqual(self.run_canary(change)[0], 1)
                if name == 'list':
                    self.assertEqual(self.replay.keys, [('-l', '/tasks'), ('Enter',), ('Escape',)] * 3)
                    self.assertEqual(self.replay.stopped, [])
                else:
                    self.assertEqual(self.replay.stopped, ['shell', 'monitor'])
                self.assertFalse(self.state.exists())

    def test_unconfirmed_stop_fails_without_skipping_other_stops(self):
        self.assertEqual(self.run_canary(unconfirmed='shell')[0], 1)
        report = json.loads(self.report.read_text())
        self.assertTrue(report['screens_parsed'])
        self.assertTrue(report['tasks']['agent']['passed'])
        self.assertFalse(report['tasks']['shell']['passed'])
        self.assertIn('did not stop', report['tasks']['shell']['error'])
        self.assertTrue(report['tasks']['monitor']['passed'])
        self.assertFalse(self.state.exists())

    def test_removal_warning_preserves_successful_and_failed_task_results(self):
        real_rmtree = shutil.rmtree
        with tempfile.TemporaryDirectory() as outer:
            def fail_remove(path, *args, **kwargs):
                if Path(path) == root:
                    self.assertTrue(self.replay.closed)
                    raise OSError(39, 'Directory not empty', str(root / 'claude'))
                return real_rmtree(path, *args, **kwargs)

            for unconfirmed, code in ((None, 0), ('shell', 1)):
                with self.subTest(unconfirmed=unconfirmed):
                    self.state.unlink(missing_ok=True)
                    root = Path(outer).resolve() / f'canary-{code}'
                    root.mkdir()
                    with patch.object(canary.tempfile, 'mkdtemp', return_value=str(root)), \
                            patch.object(canary.shutil, 'rmtree', side_effect=fail_remove):
                        self.assertEqual(self.run_canary(unconfirmed=unconfirmed)[0], code)
                    report = json.loads(self.report.read_text())
                    self.assertIn('Directory not empty', report['cleanup_warning'])
                    self.assertNotIn(str(root), self.report.read_text())
                    self.assertNotIn('error', report)
                    self.assertTrue(report['tasks']['agent']['passed'])
                    self.assertTrue(report['tasks']['monitor']['passed'])
                    self.assertEqual(report['tasks']['shell']['passed'], unconfirmed is None)
                    if unconfirmed:
                        self.assertIn('did not stop', report['tasks']['shell']['error'])
                    real_rmtree(root)

    def test_version_change_during_launch_refuses_before_opening_tasks(self):
        self.assertEqual(self.run_canary(version='2.1.328')[0], 1)
        self.assertEqual(self.replay.keys, [])
        self.assertIn('version changed', json.loads(self.report.read_text())['error'])
        self.assertFalse(self.state.exists())

    def test_timeout_and_interrupt_remove_the_temporary_directory(self):
        for sig, message in ((signal.SIGINT, 'Interrupted.'), (signal.SIGALRM, canary.TIMEOUT_MESSAGE)):
            with self.subTest(signal=sig):
                def interrupt(_):
                    signal.raise_signal(sig)

                self.assertEqual(self.run_canary(sleep=interrupt)[0], 1)
                report = json.loads(self.report.read_text())
                self.assertEqual(report['error'], message)
                self.assertTrue(self.replay.closed)
                self.assertFalse(self.replay.root.exists())
                self.assertFalse(self.state.exists())

    def test_baseline_uses_numeric_versions_and_redacts_paths_in_diff(self):
        (self.fixtures / 'claude_2.1.99_tasks').mkdir()
        self.assertEqual(self.run_canary(lambda name, screen: screen.replace(
            'No output available', 'Output at /private/example/log') if name == 'shell_detail' else screen)[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['baseline'], FIXTURE.name)
        self.assertIn('Output at ~/x', report['diffs']['shell_detail.txt'])
        self.assertNotIn('/private/example/log', self.report.read_text())

    def test_redact_preserves_device_paths(self):
        root = self.fixtures.parent
        text = f'/dev/null /dev/pts/7 /private/project-/dev/log {root}/work'
        self.assertEqual(canary.redact(text, root), '/dev/null /dev/pts/7 ~/x ~/x')

    def test_credentials_are_reconciled_and_the_copy_is_removed(self):
        original = b'{"token":"original"}\r\n'
        refreshed = b'{"token":"refreshed"}\r\n'
        other = b'{"token":"other"}\n'
        cases = (
            ('unchanged', original, False, False, original, None),
            ('refreshed', refreshed, False, False, refreshed, None),
            ('conflict', refreshed, True, False, other, 'credentials changed'),
            ('shutdown failed', refreshed, False, True, refreshed, None),
        )
        for name, copied, concurrent_change, shutdown_fails, expected, warning in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'canary'
                root.mkdir()
                source = Path(directory) / '.credentials.json'
                source.write_bytes(original)
                source.chmod(0o600)
                report_path = Path(directory) / 'report.json'

                def run(args, **kwargs):
                    if args[-1] == 'kill-server':
                        return subprocess.CompletedProcess(args, int(shutdown_fails), '', 'shutdown failed')
                    self.assertIn('new-session', args)
                    return subprocess.CompletedProcess(args, 0, f'%1\t{PID}\n', '')

                adapter = canary.TmuxClaude(root, run=run)
                copy = adapter.config / source.name

                def exercise(_adapter, _entry, report, *_args):
                    self.assertEqual(copy.read_bytes(), original)
                    self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
                    copy.write_bytes(copied)
                    if concurrent_change:
                        source.write_bytes(other)
                    report['screens_parsed'] = True
                    for task in report['tasks'].values():
                        task['passed'] = True

                stdout = io.StringIO()
                with source.open('rb') as before, redirect_stdout(stdout), \
                        patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': directory}), \
                        patch.object(canary.tempfile, 'mkdtemp', return_value=str(root)), \
                        patch.object(canary.procinfo, 'start_ticks', return_value='start'), \
                        patch.object(adapter, 'pane_processes', return_value=[]), \
                        patch.object(canary, 'exercise', side_effect=exercise):
                    code = canary.main(['--json', str(report_path)], version_probe=lambda: VERSION,
                                       adapter_factory=lambda _: adapter, fixtures=self.fixtures)
                    self.assertEqual(source.stat().st_ino == os.fstat(before.fileno()).st_ino, expected != refreshed)
                self.assertEqual(code, int(shutdown_fails))
                self.assertEqual(source.read_bytes(), expected)
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
                self.assertEqual(root.exists(), shutdown_fails)
                self.assertFalse(copy.exists())
                report = json.loads(report_path.read_text())
                if warning:
                    self.assertIn(warning, report['credentials_warning'])
                    self.assertIn(report['credentials_warning'], stdout.getvalue())
                else:
                    self.assertNotIn('credentials_warning', report)

    def test_live_adapter_uses_one_private_socket_even_if_launch_fails(self):
        calls = []

        def fail_start(args, **kwargs):
            calls.append((args, kwargs['env']))
            if 'new-session' in args:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, '', '')

        root = self.fixtures.parent
        adapter = canary.TmuxClaude(root, run=fail_start)
        with patch.object(canary, 'prepare_config', return_value=None), self.assertRaises(subprocess.CalledProcessError):
            with adapter.session():
                self.fail('unexpected live session')
        self.assertEqual(len(calls), 2)
        for args, env in calls:
            self.assertEqual(args[1:3], ['-L', adapter.socket])
            self.assertTrue(adapter.socket.startswith('tether-canary-'))
            self.assertEqual(env['TMUX_TMPDIR'], str(root))
            self.assertNotIn('TMUX', env)
        self.assertEqual(calls[1][0][-1], 'kill-server')

    def test_prompt_separates_the_quoted_subagent_prompt_from_parent_tools(self):
        introduction, literal = canary.PROMPT.split('Its prompt must be: ', 1)
        child_prompt, end = json.JSONDecoder().raw_decode(literal)
        self.assertIn('run_in_background true', introduction)
        self.assertEqual(child_prompt, 'Run Bash `sleep 601` with run_in_background true and timeout 600000. Wait for that shell to finish before you reply. Then reply done.')
        self.assertIn('The subagent must not start any other tools.', literal[end:])
        self.assertIn('Use Bash with command "sleep 600", run_in_background true', literal[end:])
        self.assertIn('Use Monitor with description "Stop canary monitor", command "sleep 599"', literal[end:])

    def test_cleanup_stops_claude_then_tmux_and_waits_for_descendants(self):
        events = []
        live = {PID, PID + 1, PID + 2}
        parents = {PID + 1: PID, PID + 2: PID + 1}

        def stat_fields(pid):
            if pid not in live:
                raise ProcessLookupError
            return ['S', str(parents.get(pid, 0)), *(['0'] * 17), str(pid)]

        def run(args, **kwargs):
            events.append(args[-1])
            return subprocess.CompletedProcess(args, 0, '', '')

        def sleep(_):
            events.append('wait')
            live.clear()

        adapter = canary.TmuxClaude(self.fixtures.parent, run=run)
        adapter.entry = {'owner_pid': PID, 'process_start': str(PID)}
        with patch.multiple(canary.procinfo, stat_fields=stat_fields, pids=lambda: sorted(live),
                            start_ticks=lambda pid: stat_fields(pid)[19], parent=lambda pid: parents.get(pid, 0)), \
                patch.object(canary.os, 'kill', side_effect=lambda pid, sig: events.append((pid, sig))):
            adapter.cleanup(sleep=sleep)
        self.assertEqual(events, [(PID, signal.SIGKILL), (PID + 1, signal.SIGKILL),
                                  (PID + 2, signal.SIGKILL), 'kill-server', 'wait'])

    def test_cleanup_wait_has_a_deadline(self):
        adapter = canary.TmuxClaude(self.fixtures.parent, run=lambda args, **_: subprocess.CompletedProcess(args, 0))
        process = canary.task_process.Process(PID, 'start')
        with patch.object(adapter, 'pane_processes', return_value=[process]), \
                patch.object(canary, 'process_alive', return_value=True), patch.object(canary.os, 'kill'), \
                self.assertRaisesRegex(canary.CanaryError, 'still has running processes'):
            adapter.cleanup(sleep=lambda _: self.fail('The deadline already passed.'), clock=iter([0, 3]).__next__)

    def test_unverified_shutdown_keeps_directory_and_records_stop_results(self):
        root = self.fixtures.parent / 'test-shutdown'
        root.mkdir()
        with patch.object(canary.tempfile, 'mkdtemp', return_value=str(root)), \
                patch.object(Replay, 'cleanup_error', 'The canary pane still has running processes.', create=True):
            self.assertEqual(self.run_canary(unconfirmed='shell')[0], 1)
        report = json.loads(self.report.read_text())
        self.assertIn('still has running processes', report['cleanup_error'])
        self.assertIn('Kept the temporary canary directory', report['cleanup_warning'])
        self.assertTrue(root.is_dir())
        self.assertIn('did not stop', report['tasks']['shell']['error'])
        self.assertTrue(report['tasks']['agent']['passed'])
        self.assertTrue(report['tasks']['monitor']['passed'])


if __name__ == '__main__':
    unittest.main()
