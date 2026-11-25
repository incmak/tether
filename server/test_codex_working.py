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
        approval = terminal.prompt_actions.plain(capture('approval_reply_quotes_status'))
        for frame in (screen.split(composer)[0] + composer + '\n\n', approval):
            with self.subTest(frame=frame[-80:]):
                self.assertIsNone(terminal.codex_screen_working(frame))

    def test_draft_and_footer_variants(self):
        # Codex 0.170.0 footer.rs: the queue hint and the Plan mode label; a draft may start with "1. ".
        screen = terminal.prompt_actions.plain(capture('working_queued_draft'))
        for old, new in (('› draft text left in box', '› 1. draft text left in box'),
                         ('  tab to queue message', '  tab to queue message · Plan mode'),
                         ('  tab to queue message', '  tab to queue message · Plan mode (shift+tab to cycle)')):
            with self.subTest(new=new):
                self.assertIs(terminal.codex_screen_working(screen.replace(old, new)), True)
        idle = terminal.prompt_actions.plain(capture('idle_reply_is_status_line'))
        hint = next(row for row in idle.splitlines() if row.startswith('  ← for agents'))
        self.assertIs(terminal.codex_screen_working(idle.replace(hint, '  Plan mode')), False)

    def test_status_row_must_be_codex_status_row(self):
        # Reviewer case: a reply with an elapsed counter and an attached detail row is not the status row.
        screen = terminal.prompt_actions.plain(capture('idle_reply_is_status_line'))
        frame = screen.replace('• Working (12s • esc to interrupt)\n\n  Worked for 2s • 16:06',
                               '• Benchmark finished (12s) with no errors\n  └ unrelated transcript detail')
        self.assertNotEqual(frame, screen)
        self.assertIs(terminal.codex_screen_working(frame), False)
        # A detail row cut off from the status row by a blank row ends the walk.
        working = terminal.prompt_actions.plain(capture('working_tool_tip'))
        frame = working.replace('/stop to close\n  └ Tip:', '/stop to close\n\n  └ Tip:')
        self.assertNotEqual(frame, working)
        self.assertIs(terminal.codex_screen_working(frame), False)
        for row in ('• Benchmark finished (12s)', '• Working (12s) • esc to interrupt', '• Working (12s • esc to interrupt) done'):
            with self.subTest(row=row):
                self.assertFalse(terminal.codex_status_row(row))

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
        for row in ('◦ Reading the project (2h 03m 04s • esc to int…', '• Checking the results (3m 04s • esc to interrupt)',
                    '• Working (3s • esc to interrupt) · 1 background terminal r…'):
            with self.subTest(row=row):
                status = next(line for line in screen.splitlines() if line.startswith('• Working ('))
                frame = screen.replace(status, row)
                self.assertNotEqual(frame, screen)
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

    def test_waiting_hook_clears_for_draft_variants(self):
        live = {'waiting': True, 'busy': False}
        screen = terminal.prompt_actions.plain(capture('working_queued_draft'))
        for old, new in (('draft text left in box', '1. draft text left in box'),
                         ('tab to queue message', 'tab to queue message · Plan mode')):
            with self.subTest(new=new):
                self.assertIn(old, screen)
                entry = {'id': '%codex-working', 'agent': 'codex'}
                run = Mock(side_effect=AssertionError('status must not touch tmux'))
                with patch.object(terminal, 'capture_quota_notices'), patch.object(terminal, 'attention', return_value=None), \
                        patch.object(terminal.prompt_actions, 'panel', return_value=None), \
                        patch.object(terminal.claude_hooks, 'codex_live', return_value=live):
                    status = terminal._live_status(entry, run, screen.replace(old, new))
                self.assertEqual(status['state'], 'Working')
