import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
import reply_preview


class PreviewTests(unittest.TestCase):
    tides_prompt = 'No tools. Write 300 words of plain prose about tides.'
    lighthouse_prompt = 'Without using any tools, write about 400 words of plain prose paragraphs (no headings, no lists) about lighthouses.'

    def fixture(self, name):
        return (Path(__file__).parent / 'fixtures' / name).read_text()

    def no_spinner(self, body, anchor=True, border=None):
        prefix = '❯ ' + self.tides_prompt + '\n\n' if anchor else ''
        return prefix + '● ' + body + '\n\n' + (border or '─' * 120) + '\n❯ \n'

    def test_stream_without_spinner(self):
        text = reply_preview.extract(self.fixture('claude_stream_no_spinner.txt'), 120,
                                     [{'role': 'user', 'text': self.tides_prompt}])
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith('Tides are the regular rise and fall of the sea'))
        self.assertTrue(text.endswith('less than half as strong'))
        self.assertEqual(len(text.split('\n\n')), 2)
        self.assertIn('\n\nAs the Earth turns', text)
        self.assertNotIn('\n', text.replace('\n\n', ''))

    def test_stream_with_spinner(self):
        text = reply_preview.extract(self.fixture('claude_stream_spinner.txt'), 120,
                                     [{'role': 'user', 'text': self.lighthouse_prompt}])
        self.assertTrue(text.startswith('Lighthouses have guided ships'))

    def test_done_reply_in_transcript(self):
        screen = self.fixture('claude_stream_done.txt')
        block = screen.split('● ', 1)[1].split('\n✻ ', 1)[0]
        full_reply = '\n'.join(line[2:] if line.startswith('  ') else line
                               for line in block.splitlines()).strip()
        messages = [{'role': 'user', 'text': self.tides_prompt},
                    {'role': 'assistant', 'text': full_reply}]
        self.assertIsNone(reply_preview.extract(screen, 120, messages))

    def test_hint_fixture(self):
        self.assertIsNone(reply_preview.extract(self.fixture('claude_hint_above_box.txt'), 120,
                                               [{'role': 'user', 'text': self.lighthouse_prompt}]))

    def test_no_spinner_safety(self):
        messages = [{'role': 'user', 'text': self.tides_prompt}]
        for body in ('Bash(ls)\n  ⎿  file.txt', 'Prose\n  │ chrome', 'Prose\nunindented'):
            with self.subTest(body=body):
                self.assertIsNone(reply_preview.extract(self.no_spinner(body), 120, messages))
        self.assertIsNone(reply_preview.extract(self.no_spinner('New prose', anchor=False), 120, messages))

    def test_no_spinner_trailing_status(self):
        messages = [{'role': 'user', 'text': self.tides_prompt}]
        for status in ('✻ Sautéed for 18s · done', '                  tmux focus-events off · hint',
                       '                  ◐ medium · /effort'):
            with self.subTest(status=status):
                self.assertEqual(reply_preview.extract(self.no_spinner('New prose\n\n' + status),
                                                      120, messages), 'New prose')
                self.assertIsNone(reply_preview.extract(self.no_spinner('New prose\n\n' + status + '\n  More prose'),
                                                       120, messages))

    def test_no_spinner_border(self):
        messages = [{'role': 'user', 'text': self.tides_prompt}]
        for border in ('  ' + '─' * 120 + '  ', '─' * 119 + 'x'):
            with self.subTest(border=border):
                self.assertEqual(reply_preview.extract(self.no_spinner('New prose', border=border),
                                                      120, messages), 'New prose')
        self.assertIsNone(reply_preview.extract(self.no_spinner('New prose', border='missing'), 120, messages))

    def extract(self, body, width=80, known='Earlier answer.'):
        screen = '● Earlier answer.\n\n● ' + body + '\n\n✻ Composing… (2s · esc to interrupt)\n' + '─' * width + '\n❯ \n' + '─' * width + '\n footer'
        return reply_preview.extract(screen, width, [{'role': 'assistant', 'text': known}])

    def test_plain_paragraph(self):
        self.assertEqual(self.extract('A new answer.'), 'A new answer.')

    def test_wrapped_lines(self):
        self.assertEqual(self.extract('These words fill the row\n  and continue here.', 26), 'These words fill the row and continue here.')

    def test_bullets(self):
        self.assertEqual(self.extract('Here are the items:\n\n  - First item\n  - Second item'), 'Here are the items:\n\n- First item\n- Second item')

    def test_table(self):
        self.assertIsNone(self.extract('Table\n  ┌───┬───┐\n  │ A │ B │\n  └───┴───┘'))

    def test_already_in_transcript(self):
        self.assertIsNone(self.extract('Earlier answer.'))

    def test_markdown_table(self):
        self.assertIsNone(self.extract('Table\n  | A | B |\n  |---|---|'))

    def test_tool_box(self):
        self.assertIsNone(self.extract('Bash(ls)\n  ⎿  file.txt'))

    def test_no_anchor_or_block_start(self):
        self.assertIsNone(self.extract('New text', known='Not visible'))
        self.assertIsNone(reply_preview.extract('unmarked text\n✻ Working… (2s)\n❯ ', 80, []))

    def test_known_markdown(self):
        self.assertIsNone(self.extract('Earlier answer.', known='**Earlier answer.**'))

    def test_known_markdown_link(self):
        self.assertIsNone(self.extract('Earlier answer.', known='[Earlier answer.](https://example.invalid)'))

    def test_capture_throttled_across_clients(self):
        entry = {'id': '%test'}
        with patch.object(reply_preview, '_samples', {}), patch.object(reply_preview.subprocess, 'run') as run:
            run.return_value.stdout = '80\n'  # both calls are read-only
            reply_preview.sample(entry, now=1)
            reply_preview.sample(entry, now=1.299)
            self.assertEqual(run.call_count, 2)
            reply_preview.sample(entry, now=1.6)
            self.assertEqual(run.call_count, 4)


class ChangeTests(unittest.TestCase):
    def test_signature_uses_stat_without_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history'
            entry = {'transcript': str(path)}
            with patch.object(server, 'capture', side_effect=AssertionError('expensive capture')):
                missing = server.change_signature(entry)
                path.write_text('one')
                first = server.change_signature(entry)
                self.assertNotEqual(missing, first)
                self.assertEqual(first, server.change_signature(entry))
                path.write_text('two')
                os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
                self.assertNotEqual(first, server.change_signature(entry))
                before = server.change_signature(entry)
                path.write_text('longer')
                self.assertNotEqual(before, server.change_signature(entry))
                replacement = path.with_name('replacement')
                replacement.write_text('longer')
                old = path.stat()
                os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
                before = server.change_signature(entry)
                replacement.replace(path)
                self.assertNotEqual(before, server.change_signature(entry))
                path.unlink()
                self.assertEqual(missing, server.change_signature(entry))


class PayloadPreviewTests(unittest.TestCase):
    entry = {'id': '%test', 'agent': 'claude', 'transcript': None}
    screen = '● Earlier answer.\n\n● New text.\n\n✻ Composing… (2s)\n────\n❯ \n────'

    def payload(self, messages=None, state='Working', enabled=True, signatures=None):
        with patch.object(server, 'live_status', return_value={'state': state, 'approval': None}), \
             patch.object(server, 'snapshot', return_value={'messages': messages or [{'role': 'assistant', 'text': 'Earlier answer.'}]}), \
             patch.object(server.shell_log, 'merge', side_effect=lambda pane, messages: messages), \
             patch.object(server.reply_preview, 'sample', return_value=(self.screen, 80)) as capture, \
             patch.object(server, 'change_signature', side_effect=signatures or [(), ()]):
            return server.chat_payload(self.entry, preview=enabled), capture.call_count

    def test_preview_is_ephemeral_and_handoff_is_atomic(self):
        data, count = self.payload()
        self.assertEqual(data['preview'], {'text': 'New text.'})
        self.assertEqual(len(data['messages']), 1)
        committed, _ = self.payload([{'role': 'assistant', 'text': 'Earlier answer.'}, {'role': 'assistant', 'text': 'New text.'}])
        self.assertNotIn('preview', committed)
        self.assertEqual(committed['messages'][-1]['text'], 'New text.')

    def test_idle_never_captures(self):
        data, count = self.payload(state='Idle')
        self.assertNotIn('preview', data)
        self.assertEqual(count, 0)

    def test_disabled_preview_still_samples_once_for_bang_commands(self):
        data, count = self.payload(enabled=False)
        self.assertNotIn('preview', data)
        self.assertEqual(count, 1)

    def test_change_during_snapshot_suppresses_preview(self):
        data, _ = self.payload(signatures=[(1,), (2,)])
        self.assertNotIn('preview', data)


if __name__ == '__main__':
    unittest.main()
