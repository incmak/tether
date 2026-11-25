"""Claude's Working signal comes from the input box status area and registry."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import side_question
import terminal_input as terminal


FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name, group='claude_2.1.331_working'):
    return (FIXTURES / group / name).read_text(encoding='utf-8')


class ClaudeWorkingTests(unittest.TestCase):
    def setUp(self):
        self.registry = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'registry.json'
        self.entry = {'agent': 'claude', 'id': '%42', 'registry': self.registry}
        self.run = Mock(side_effect=AssertionError('status must not touch tmux'))
        self.enterContext(patch.object(terminal, 'capture_quota_notices'))
        self.enterContext(patch.object(terminal, 'attention', return_value=None))
        self.enterContext(patch.object(terminal.prompt_actions, 'panel', return_value=None))
        self.enterContext(patch.object(terminal, 'suggestion', return_value=''))

    def test_captured_screens_and_registry_independently_signal_working(self):
        for extension in ('.txt', '.ansi'):
            for name, registry, screen_working, state in (
                ('idle_reply_quotes_status', 'idle', False, 'Idle'),
                ('working', 'idle', True, 'Working'),
                ('idle_reply_quotes_status', 'busy', False, 'Working'),
            ):
                with self.subTest(extension=extension, name=name, registry=registry):
                    screen = fixture(name + extension)
                    self.registry.write_text(json.dumps({'status': registry}))
                    self.assertEqual(terminal._live_status(self.entry, self.run, screen)['state'], state)
                    self.assertIs(terminal.claude_screen_working(terminal.prompt_actions.plain(screen)),
                                  screen_working)

    def test_reply_and_draft_text_do_not_signal_working(self):
        idle = fixture('idle_reply_quotes_status.txt').splitlines()
        top = next(index for index, row in enumerate(idle) if row.startswith('─'))
        for text in ('  Press esc to interrupt', '  ● Reviewing…', '  ✻ Working… (1s)'):
            with self.subTest(text=text):
                frame = idle[:top] + [text] + idle[top:]
                self.assertIs(terminal.claude_screen_working('\n'.join(frame)), False)
        idle[top + 1] = '❯ esc to interrupt ✻ Working…'
        idle.insert(top + 2, '✻ Working… (1s) esc to interrupt')
        self.assertIs(terminal.claude_screen_working('\n'.join(idle)), False)

    def test_text_shaped_like_the_footer_does_not_signal_working(self):
        # Review r1: an indented rule typed in the draft must not end the box, and a status line that quotes the
        # hint is not the hint.
        idle = fixture('idle_reply_quotes_status.txt').splitlines()
        anchor = next(index for index, row in enumerate(idle) if row.startswith('❯'))
        width = len(idle[anchor - 1].rstrip())
        for draft in (['  ' + '─' * (width - 2), '  esc to interrupt is the hint'],
                      ['  ' + '─' * width, '  esc to interrupt'],
                      ['─' * 10, 'esc to interrupt']):
            with self.subTest(draft=draft):
                frame = idle[:anchor + 1] + draft + idle[anchor + 1:]
                self.assertIs(terminal.claude_screen_working('\n'.join(frame)), False)
                self.assertEqual(terminal._live_status(self.entry, self.run, '\n'.join(frame))['state'], 'Idle')
        bottom = max(index for index, row in enumerate(idle) if row.startswith('─'))
        for status in ('  Opus 5.5 | esc to interrupt is the hint', '  esc to interrupt is the hint · main'):
            with self.subTest(status=status):
                frame = idle[:bottom + 1] + [status] + idle[bottom + 1:]
                self.assertIs(terminal.claude_screen_working('\n'.join(frame)), False)
        frame = idle[:bottom + 1] + ['  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt'] + idle[bottom + 2:]
        self.assertIs(terminal.claude_screen_working('\n'.join(frame)), True)

    def test_side_question_without_registry_uses_its_answering_state(self):
        for answering, state in ((True, 'Working'), (False, 'Idle')):
            with self.subTest(answering=answering), patch.object(
                    side_question, 'observe', return_value={'reason': 'side', 'pending': True, 'answering': answering}):
                self.assertEqual(terminal._live_status(self.entry, self.run, '')['state'], state)
        self.registry.write_text('{"status": "idle"}')
        with patch.object(side_question, 'observe', return_value={'reason': 'side', 'pending': True, 'answering': True}):
            self.assertEqual(terminal._live_status(self.entry, self.run, '')['state'], 'Idle')

    def test_footer_hint_is_case_insensitive(self):
        screen = '──────────\n❯\n──────────\n  Esc To Interrupt'
        self.assertIs(terminal.claude_screen_working(screen), True)

    def test_spinners_with_missing_mode_row(self):
        for name, spinner in (
            ('prompt_waiting_working.txt', '✢ Scurrying…'),
            ('narrow_prompt_waiting.txt', '· Germinating…'),
        ):
            with self.subTest(name=name):
                screen = fixture(name, 'claude_2.1.330_agent_bar')
                self.assertIn(spinner, screen)
                self.assertIs(terminal.claude_screen_working(screen), True)
                rows = screen.splitlines()
                bottom = max(index for index, row in enumerate(rows) if row.startswith('─'))
                self.assertIs(terminal.claude_screen_working('\n'.join(rows[:bottom + 1])), True)
        screen = fixture('working.txt')
        self.assertIs(terminal.claude_screen_working(screen.split('  ⏵⏵')[0]), True)

    def test_spinner_scan_stops_at_message_boundary(self):
        for start in ('● Reply', '❯ Prompt'):
            with self.subTest(start=start):
                screen = f"✻ Thinking…\n{start}\n──────────\n❯\n──────────"
                self.assertIs(terminal.claude_screen_working(screen), False)

    def test_spinner_scan_is_limited_to_40_rows(self):
        box = '──────────\n❯\n──────────'
        for blank, working in ((39, True), (40, False)):
            with self.subTest(blank=blank):
                self.assertIs(terminal.claude_screen_working('✻ Thinking…\n' + '\n' * blank + box), working)

    def test_dialog_without_input_box_is_unknown(self):
        screen = fixture('background_session_dialog.txt', 'claude_2.1.330_agent_bar')
        self.assertIsNone(terminal.claude_screen_working(screen))

    def test_side_question_uses_registry_instead_of_answer_text(self):
        side = {'reason': 'side'}
        with patch.object(side_question, 'observe', return_value=side):
            for contents, state in ((None, 'Idle'), ('{', 'Idle'), ('null', 'Idle'),
                                    ('{"status": "idle"}', 'Idle'), ('{"status": "busy"}', 'Working')):
                with self.subTest(contents=contents):
                    if contents is not None:
                        self.registry.write_text(contents)
                    status = terminal._live_status(self.entry, self.run, 'esc to interrupt')
                    self.assertEqual(status, {'state': state, 'approval': None, 'attention': side})
