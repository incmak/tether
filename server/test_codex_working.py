"""Regression coverage for Codex status rows above the composer."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import terminal_input as terminal


FIXTURES = Path(__file__).parent / 'fixtures/codex_0.170.0_working'


def capture(name):
    return (FIXTURES / (name + '.ansi')).read_text()


class CodexScreenWorkingTests(unittest.TestCase):
    def test_captured_frames(self):
        frames = sorted(FIXTURES.glob('*.ansi'))
        self.assertEqual(len(frames), 12)
        for path in frames:
            with self.subTest(frame=path.name):
                expected = {'working': True, 'idle': False, 'approval': None}[path.name.split('_', 1)[0]]
                self.assertIs(terminal.codex_screen_working(terminal.prompt_actions.plain(path.read_text())), expected)

    def test_unverified_composer(self):
        screen = terminal.prompt_actions.plain(capture('working_tool'))
        composer = '› Ask Codex to do anything'
        for frame in (screen.split(composer)[0] + composer + '\n\n', screen.replace(composer, '› 1. Yes')):
            with self.subTest(frame=frame):
                self.assertIsNone(terminal.codex_screen_working(frame))

    def test_orphaned_queue_rows_do_not_hide_reply_boundary(self):
        screen = terminal.prompt_actions.plain(capture('idle_reply_is_status_line'))
        for row in ('  ↳ unverified queued input', '    unverified queue hint'):
            with self.subTest(row=row):
                frame = screen.replace('  Worked for 2s • 16:06', row)
                self.assertIs(terminal.codex_screen_working(frame), False)

    def test_unknown_row_blocks_status(self):
        screen = terminal.prompt_actions.plain(capture('working_tool'))
        frame = screen.replace('› Ask Codex to do anything', '  Unknown status\n\n› Ask Codex to do anything')
        self.assertIs(terminal.codex_screen_working(frame), False)

    def test_reasoning_title_and_elapsed_counter(self):
        screen = terminal.prompt_actions.plain(capture('working_tool'))
        for row in ('◦ Reading the project (2h 3m 4s…', '• Checking the results (3m 4s)'):
            with self.subTest(row=row):
                frame = screen.replace('• Working (9s • esc to interrupt)', row)
                self.assertIs(terminal.codex_screen_working(frame), True)


class CodexWorkingStatusTests(unittest.TestCase):
    def status(self, frame, live):
        entry = {'id': '%codex-working', 'agent': 'codex'}
        run = Mock(side_effect=AssertionError('status must not touch tmux'))
        with patch.object(terminal, 'capture_quota_notices'), patch.object(terminal, 'attention', return_value=None), \
                patch.object(terminal.prompt_actions, 'panel', return_value=None), \
                patch.object(terminal.claude_hooks, 'codex_live', return_value=live):
            return terminal._live_status(entry, run, capture(frame))

    def test_without_hooks(self):
        self.assertEqual(self.status('idle_reply_is_status_line', None)['state'], 'Idle')
        self.assertEqual(self.status('working_tool', None)['state'], 'Working')

    def test_waiting_hook(self):
        live = {'waiting': True, 'busy': False}
        waiting = self.status('idle_reply_quotes_status', live)
        self.assertEqual(waiting['state'], 'Approval')
        self.assertFalse(waiting['approval']['actionable'])
        self.assertEqual(self.status('working_after_approval', live)['state'], 'Working')
