"""Unnumbered Claude pickers use panel text and never receive an automatic Esc."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import terminal_input as terminal
from own_time import own_time


FIXTURES = Path(__file__).parent / 'test_fixtures' / 'pickers'


class CursorPickerTests(unittest.TestCase):
    entry = {'id': '%picker', 'agent': 'claude'}

    def setUp(self):
        terminal._attention_seen.clear()
        self.screen = (FIXTURES / 'claude_rewind_150x39.txt').read_text()

    def attention(self, screen):
        terminal.attention(self.entry, screen, False, now=0)
        return terminal.attention(self.entry, screen, False, now=4)

    def test_rewind_panel_has_title_items_cursor_counts_and_hint(self):
        self.assertEqual(len(self.screen.splitlines()), 39)
        self.assertEqual(max(map(len, self.screen.splitlines())), 150)
        found = self.attention(self.screen)
        self.assertEqual(found['reason'], 'picker')
        self.assertEqual(found['title'], 'Rewind')
        self.assertEqual(found['subtitle'], 'Restore the code and/or conversation to the point before…')
        self.assertEqual(found['items'], [
            {'text': 'hello?', 'selected': False},
            {'text': 'No code changes', 'selected': False},
            {'text': '(current)', 'selected': True},
        ])
        self.assertEqual((found['more_above'], found['more_below']), (26, 0))
        self.assertEqual(found['hint'], 'Enter to continue · Esc to cancel')
        self.assertTrue(found['text'].startswith('Rewind\n'))
        self.assertNotIn('Worked for 9s', found['text'])
        self.assertNotIn('Earlier chat', found['text'])

    def test_a_panel_with_paragraphs_takes_its_title_from_the_top_and_keeps_every_paragraph(self):
        # Claude Code 2.1.325, real screens. The paragraph above the list is the security note, not the question.
        subtitle = ("This project's CLAUDE.md or .claude/rules imports files outside the current working directory. "
                    'Never allow this for third-party repositories.\n'
                    'External imports: /workspace/apps/tools/AGENTS.md\n'
                    'Important: Only use Claude Code with files you trust. Accessing untrusted files may pose security '
                    'risks https://code.claude.com/docs/en/security')
        for width in (140, 60):
            with self.subTest(width=width):
                terminal._attention_seen.clear()
                screen = (FIXTURES / f'claude_2.1.325_external_imports_{width}x40.txt').read_text()
                found = self.attention(screen)
                self.assertEqual(found['reason'], 'picker')
                self.assertEqual(found['title'], 'Allow external CLAUDE.md file imports?')
                self.assertEqual(found['subtitle'], subtitle)
                self.assertEqual(found['items'], [
                    {'text': 'No, disable external imports', 'selected': True},
                    {'text': 'Yes, allow external imports', 'selected': False},
                ])
                self.assertTrue(found['text'].startswith('Allow external CLAUDE.md file imports?\n'))

    def test_without_a_rule_only_the_paragraph_above_the_list_is_the_header(self):
        screen = ('● Earlier reply\n\nEarlier paragraph\n\nPick one\nWhich file\n\n'
                  '❯ First\n  Second\n\nEnter to select · Esc to cancel\n')
        found = self.attention(screen)
        self.assertEqual((found['title'], found['subtitle']), ('Pick one', 'Which file'))
        self.assertNotIn('Earlier', found['text'])

    def test_picker_without_a_rule_keeps_chat_out_and_reads_rows_below_cursor(self):
        screen = ('Earlier chat\nWorked for 9s\n\nSelect session\nChoose a conversation\n\n'
                  '  First session\n› Second session\n  Third session\n↓ 7 more\n\n'
                  'Enter to select · Esc to cancel\n')
        found = self.attention(screen)
        self.assertEqual(found['reason'], 'picker')
        self.assertEqual((found['title'], found['subtitle']), ('Select session', 'Choose a conversation'))
        self.assertEqual(found['items'], [
            {'text': 'First session', 'selected': False},
            {'text': 'Second session', 'selected': True},
            {'text': 'Third session', 'selected': False},
        ])
        self.assertEqual((found['more_above'], found['more_below']), (0, 7))
        self.assertNotIn('Earlier chat', found['text'])
        self.assertNotIn('Worked for 9s', found['text'])

    def test_unverified_numbered_menu_keeps_the_question(self):
        screen = (Path(__file__).parent / 'fixtures' / 'claude_question_narrow.txt').read_text()
        found = self.attention(screen)
        self.assertEqual(found['reason'], 'menu')
        self.assertEqual(found['question'], 'Which data for the live screenshots?')
        self.assertFalse(found['menu_verified'])
        self.assertNotIn('options', found)
        self.assertNotIn('items', found)
        self.assertNotIn('title', found)

    def test_cursor_movement_does_not_restart_the_delay(self):
        terminal.attention(self.entry, self.screen, False, now=0)
        moved = self.screen.replace('❯ (current)', '  (current)').replace('  hello?', '❯ hello?')
        found = terminal.attention(self.entry, moved, False, now=4)
        self.assertEqual(found['reason'], 'picker')
        self.assertEqual([item['text'] for item in found['items'] if item['selected']], ['hello?'])

    def test_wrapped_hint_is_joined_and_stays_out_of_items(self):
        screen = self.screen.replace('Enter to continue · Esc to cancel', 'Enter to continue · Esc to\ncancel')
        found = self.attention(screen)
        self.assertEqual(found['reason'], 'picker')
        self.assertEqual(found['hint'], 'Enter to continue · Esc to cancel')
        self.assertEqual(len(found['items']), 3)

    def test_chat_cursor_and_old_hint_are_not_an_open_picker(self):
        for screen in (
                self.screen + '\n● Back in chat\n──────────\n❯ \n──────────\n',
                '❯ Earlier chat\n● Reply\nEnter to continue · Esc to cancel\n'):
            with self.subTest(screen=screen):
                terminal._attention_seen.clear()
                found = self.attention(screen)
                self.assertTrue(found is None or found['reason'] != 'picker')

    def test_close_info_panel_does_not_escape_a_picker(self):
        with own_time(terminal, sleep=Mock()), patch.object(terminal, 'capture', return_value=self.screen), \
                patch.object(terminal.subprocess, 'run') as send:
            self.assertFalse(terminal.close_info_panel(self.entry, run=send))
        send.assert_not_called()

    def test_send_refuses_a_picker_before_pasting_or_pressing_keys(self):
        indented = '\n'.join('  ' + line for line in self.screen.splitlines())
        for screen in (self.screen, indented):
            with self.subTest(screen=screen), patch.object(terminal, 'verify'), \
                    patch.object(terminal, 'leave_scrollback'), patch.object(terminal, 'capture', return_value=screen), \
                    patch.object(terminal.subprocess, 'run') as send:
                with self.assertRaisesRegex(RuntimeError, r'^Claude has the Rewind picker open\. Choose an item or press Esc in the card\.$'):
                    terminal.submit(self.entry, 'hello', run=send)
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
