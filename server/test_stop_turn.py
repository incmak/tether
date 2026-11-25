"""Stop restores Claude's interrupted draft without touching pre-existing text."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import subprocess
import unittest
from unittest.mock import Mock, patch

import terminal_input as terminal


BOX = '──────────\n❯\n──────────'


class StopTurnTests(unittest.TestCase):
    def setUp(self):
        self.entry = {'id': '%stop', 'agent': 'claude'}
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=''))
        self.sleep = Mock()
        self.verify = self.enterContext(patch.object(terminal, 'verify'))
        self.drop = self.enterContext(patch.object(terminal, 'drop_double_escape', return_value=False))
        self.clear = self.enterContext(patch.object(terminal, 'clear_draft'))
        self.capture = self.enterContext(patch.object(terminal, 'capture', return_value=''))

    def stop(self):
        return terminal.stop_turn(self.entry, run=self.run, sleep=self.sleep)

    def assert_escape(self):
        self.verify.assert_called_once_with(self.entry, self.run)
        self.run.assert_called_once_with(
            [terminal.paths.TMUX, 'send-keys', '-t', '%stop', 'Escape'],
            check=True, capture_output=True, timeout=5)

    def test_restores_after_redraw_then_clears(self):
        with patch.object(terminal, 'prompt_draft', side_effect=[[''], ['partial'], ['whole message', 'second line']]):
            self.assertEqual(self.stop(), {'restored': 'whole message\nsecond line'})
        self.assert_escape()
        self.clear.assert_called_once_with(self.entry, self.run)
        self.assertEqual(self.sleep.call_count, 2)

    def test_empty_idle_box_stops_after_one_second(self):
        self.capture.return_value = '● The footer says esc to interrupt.\n' + BOX
        with patch.object(terminal, 'prompt_draft', return_value=['']):
            self.assertEqual(self.stop(), {'restored': ''})
        self.assert_escape()
        self.clear.assert_not_called()
        self.assertEqual(self.sleep.call_count, 5)

    def test_working_box_polls_for_two_seconds(self):
        self.capture.return_value = "✻ Beboppin'…\n" + BOX
        with patch.object(terminal, 'prompt_draft', return_value=[]):
            self.assertEqual(self.stop(), {'restored': ''})
        self.assertEqual(self.sleep.call_count, 10)
        self.clear.assert_not_called()

    def test_unknown_screen_keeps_waiting_for_restored_draft(self):
        with patch.object(terminal, 'prompt_draft', side_effect=[[]] * 6 + [['restored']] * 2):
            self.assertEqual(self.stop(), {'restored': 'restored'})
        self.capture.assert_called_once_with(self.entry, self.run)
        self.clear.assert_called_once_with(self.entry, self.run)

    def test_existing_terminal_draft_is_preserved(self):
        with patch.object(terminal, 'prompt_draft', return_value=['typed text']) as draft:
            self.assertEqual(self.stop(), {'restored': ''})
        self.assert_escape()
        draft.assert_called_once_with(self.entry, self.run)
        self.sleep.assert_not_called()
        self.clear.assert_not_called()

    def test_missing_or_small_input_still_gets_escape(self):
        for error in (RuntimeError('No input box'), terminal.PaneTooSmall('Too small')):
            with self.subTest(error=error), patch.object(terminal, 'prompt_draft', side_effect=error):
                self.run.reset_mock()
                self.verify.reset_mock()
                self.assertEqual(self.stop(), {'restored': ''})
                self.assert_escape()
        self.clear.assert_not_called()
        self.sleep.assert_not_called()

    def test_double_escape_is_dropped(self):
        self.drop.return_value = True
        with patch.object(terminal, 'prompt_draft', return_value=[]):
            self.assertEqual(self.stop(), {'restored': ''})
        self.run.assert_not_called()
        self.clear.assert_not_called()

    def test_other_agents_are_rejected(self):
        for agent in ('codex', 'antigravity'):
            self.entry['agent'] = agent
            with self.assertRaises(ValueError):
                self.stop()
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_dim_placeholders_are_empty_but_restored_text_is_not(self):
        # Use the real styled parser; Claude restores ordinary, non-dim input.
        for placeholder in ('Try "edit a file"', 'A suggested next prompt'):
            self.capture.side_effect = [
                '──────────\n❯ \x1b[2m' + placeholder + '\x1b[22m\n──────────',
                '──────────\n❯ write about rivers\n──────────',
                '──────────\n❯ write about rivers\n──────────',
            ]
            self.assertEqual(self.stop(), {'restored': 'write about rivers'})


if __name__ == '__main__':
    unittest.main()
