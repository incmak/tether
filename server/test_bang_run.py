import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import subprocess
import unittest
from unittest.mock import patch

import bang_run
import server


COMMAND = 'echo "open the page and enter the code ABC123"; sleep 25; echo done'
HINT = '     (ctrl+b ctrl+b (twice) to run in background)'
BOX = '────────────────────\n❯\n────────────────────'
RUNNING = f'! {COMMAND}\n  ⎿  open the page and enter the code ABC123\n     (7s)\n{HINT}\n\nother lines\n{BOX}'


class ExtractTests(unittest.TestCase):
    def test_running(self):
        self.assertEqual(bang_run.extract(RUNNING), {
            'command': COMMAND, 'output': 'open the page and enter the code ABC123', 'seconds': 7})

    def test_finished(self):
        screen = f'! {COMMAND}\n  ⎿  open the page and enter the code ABC123\n     done\n{BOX}'
        self.assertIsNone(bang_run.extract(screen))

    def test_real_screen_no_break_space(self):
        screen = f'! {COMMAND}\n  ⎿ \xa0open the page\n     (14s)\n{HINT}\n────────\n❯\xa0'
        self.assertEqual(bang_run.extract(screen), {'command': COMMAND, 'output': 'open the page', 'seconds': 14})

    def test_starting_shows_before_hint(self):
        screen = f'! {COMMAND}\n  ⎿\xa0 Running…\n────────\n❯\xa0'
        self.assertEqual(bang_run.extract(screen), {'command': COMMAND, 'output': '', 'seconds': None})

    def test_no_command(self):
        self.assertIsNone(bang_run.extract(f'● Some reply\n{HINT}\n{BOX}'))

    def test_multiline_output(self):
        screen = f'! example\n  ⎿  first\n     second\n     \n       indented\n     (12s)\n{HINT}\n\nother lines\n{BOX}'
        self.assertEqual(bang_run.extract(screen), {
            'command': 'example', 'output': 'first\nsecond\n\n  indented', 'seconds': 12})

    def test_wrapped_long_command_and_output(self):
        command = 'echo "' + 'long command ' * 20
        screen = f'! {command}\n  command continues here"\n  ⎿  output wraps\n     onto the next row\n{HINT}\n{BOX}'
        self.assertEqual(bang_run.extract(screen), {
            'command': command, 'output': 'output wraps\nonto the next row', 'seconds': None})

    def test_newer_finished_command_overrides_old_running_command(self):
        screen = f'! old\n  ⎿  old output\n     (3s)\n{HINT}\n! new\n  ⎿  done\n{BOX}'
        self.assertIsNone(bang_run.extract(screen))

    def test_last_running_command(self):
        screen = f'! old\n  ⎿  old output\n{HINT}\n{RUNNING}'
        self.assertEqual(bang_run.extract(screen)['command'], COMMAND)

    def test_last_40_output_lines(self):
        output = '\n'.join('     line ' + str(i) for i in range(1, 55))
        screen = f'! example\n  ⎿  line 0\n{output}\n     (9s)\n{HINT}\n{BOX}'
        self.assertEqual(bang_run.extract(screen)['output'], '\n'.join('line ' + str(i) for i in range(15, 55)))

    def test_hint_in_input_box_does_not_mark_command_running(self):
        screen = f'! example\n  ⎿  done\n────────────────────\n❯\n{HINT}\n────────────────────'
        self.assertIsNone(bang_run.extract(screen))

    def test_odd_input(self):
        for screen in (None, 42, [], {}, '', '! example', f'! example\n{HINT}', '❯'):
            with self.subTest(screen=screen):
                self.assertIsNone(bang_run.extract(screen))
        screen = f'! example\n  ⎿  output\n     ({"9" * 5000}s)\n{HINT}\n{BOX}'
        self.assertEqual(bang_run.extract(screen)['output'], 'output')


class PayloadTests(unittest.TestCase):
    def payload(self, preview=False, agent='claude', state='Working', error=None):
        config = {'id': '%test', 'agent': agent, 'transcript': None}
        with patch.object(server, 'live_status', return_value={'state': state, 'approval': None}), \
             patch.object(server, 'snapshot', return_value={'messages': []}), \
             patch.object(server.shell_log, 'merge', side_effect=lambda pane, messages: messages), \
             patch.object(server.reply_preview, 'sample', return_value=(RUNNING, 80), side_effect=error) as sample, \
             patch.object(server.reply_preview, 'extract', return_value='reply text') as extract, \
             patch.object(server, 'change_signature', return_value=()):
            result = server.chat_payload(config, preview=preview)
            return result, sample.call_count, extract.call_count

    def test_bang_without_preview(self):
        result, samples, previews = self.payload()
        self.assertEqual(result['bang'], bang_run.extract(RUNNING))
        self.assertNotIn('preview', result)
        self.assertEqual((samples, previews), (1, 0))

    def test_preview_and_bang_share_sample(self):
        result, samples, previews = self.payload(preview=True)
        self.assertEqual(result['bang'], bang_run.extract(RUNNING))
        self.assertEqual(result['preview'], {'text': 'reply text'})
        self.assertEqual((samples, previews), (1, 1))

    def test_only_working_claude_samples(self):
        for options in ({'state': 'Idle'}, {'state': 'Approval'}, {'agent': 'codex'}, {'agent': 'antigravity'}):
            with self.subTest(options=options):
                result, samples, _ = self.payload(preview=True, **options)
                self.assertNotIn('bang', result)
                self.assertEqual(samples, 0)

    def test_capture_exceptions_are_caught(self):
        for error in (OSError(), ValueError(), KeyError(), subprocess.SubprocessError()):
            with self.subTest(error=error):
                result, samples, _ = self.payload(preview=True, error=error)
                self.assertNotIn('bang', result)
                self.assertNotIn('preview', result)
                self.assertEqual(samples, 1)


if __name__ == '__main__':
    unittest.main()
