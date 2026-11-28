"""Codex queued prompt captures and chat payload coverage."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import codex_queue
import paths
import prompt_actions
import server
import terminal_input


FIXTURES = Path(__file__).parent / 'fixtures/codex_0.170.0_queue'
LONG_ROWS = [
    'This is a long queued follow-up message that should wrap across more than one row in the preview',
    'because the pane is only one hundred columns wide, and it keeps going so that it needs a third',
    'row and then a fourth row to be sure the preview cuts it off with an ellipsis line at the end of',
]
LONG_60 = ('This is a long queued follow-up message that should wrap across more than one row in the preview '
           'because the pane is only one hundred columns wide, and it keeps going so…')
URL = 'https://example.com/some/very/long/path/that/keeps/going/and/going/for/a/while/until/it/is/longe'
HINT = '    shift+← edit last queued message'


def frame(name):
    styled = (FIXTURES / (name + '.ansi')).read_text()
    return prompt_actions.plain(terminal_input.OSC.sub('', styled))


def entries(*items, start=0):
    return [dict(id=index, text=text, time=None, partial=partial)
            for index, (text, partial) in enumerate(items, start=start)]


def four(long_text=' '.join(LONG_ROWS) + '…'):
    return entries(('say hi', False), (long_text, True), ('!ls -la', False), ('line one\nline two', False))


class QueueScreenTests(unittest.TestCase):
    def test_captures(self):
        six = four() + entries(('a\n\nb', False), ('see ' + URL + '…', True), start=4)
        many = six + entries(*[(f'extra {i}', False) for i in range(1, 13)], start=6)
        cases = (
            ('queued_four', 100, four()),
            ('queued_four_60col', 60, four(LONG_60)),
            ('queued_blank_url', 100, six),
            ('queued_with_draft', 100, six),
            ('queued_many_40rows', 100, many),
            ('queued_many_20rows', 100, None),
            ('idle_reply_quotes_queue', 100, []),
            ('after_esc', 100, []),
        )
        for name, width, expected in cases:
            with self.subTest(frame=name):
                self.assertEqual(codex_queue.parse(frame(name), width), expected)

    def test_without_width_keeps_rows(self):
        self.assertEqual(codex_queue.parse(frame('queued_four')), four('\n'.join(LONG_ROWS) + '…'))

    def test_unknown_rows_and_missing_hint_refuse_the_entire_queue(self):
        screen = frame('queued_four')
        for changed in (
            screen.replace('    line two', '  unexpected content'),
            screen.replace(HINT + '\n', ''),
            frame('queued_many_20rows').replace('  ↳ see\n', '    another line after the blank\n'),
        ):
            with self.subTest(changed=changed.splitlines()[24:34]):
                self.assertIsNone(codex_queue.parse(changed, 100))

    def test_unverified_composer_refuses(self):
        for name in ('queued_four', 'queued_many_20rows'):
            with self.subTest(frame=name):
                screen = frame(name).split('  GPT-6.2-Sol')[0]
                self.assertIsNone(codex_queue.parse(screen, 100))
        screen = frame('after_esc').replace('■ Conversation interrupted', '■ A different event')
        self.assertIsNone(codex_queue.parse(screen, 100))

    def test_edit_binding_can_change_and_one_spacer_also_works(self):
        screen = frame('queued_four').replace(HINT + '\n\n\n', '    alt+← edit last queued message\n\n')
        self.assertEqual(codex_queue.parse(screen, 100), four())

    def test_steer_sections_stay_out_of_queue_without_status(self):
        tail = frame('queued_four_60col').split('• Queued follow-up inputs', 1)[1]
        screen = ('• Messages to be submitted after next tool call (press esc\n'
                  '  to interrupt and send immediately)\n'
                  '  ↳ pending steer\n'
                  '    second line\n\n'
                  '• Messages to be submitted at end of turn\n'
                  '  ↳ rejected steer\n\n'
                  '• Queued follow-up inputs' + tail)
        self.assertEqual(codex_queue.parse(screen, 60), four(LONG_60))

    def test_header_without_entries_or_hint(self):
        screen = frame('queued_four')
        block = screen[screen.index('  ↳ say hi'):screen.index(HINT) + len(HINT)]
        self.assertEqual(codex_queue.parse(screen.replace(block + '\n', ''), 100), [])

    def test_misplaced_blank_rows_refuse(self):
        screen = frame('queued_four')
        for row in ('  ↳ say hi', '  ↳ !ls -la', HINT):
            with self.subTest(row=row):
                self.assertIsNone(codex_queue.parse(screen.replace(row, '\n' + row), 100))

    def test_blank_preview_line_before_cut_marker_is_preserved(self):
        screen = frame('queued_four').replace('    ' + LONG_ROWS[2] + '\n', '\n')
        expected = four(' '.join(LONG_ROWS[:2]) + '\n…')
        self.assertEqual(codex_queue.parse(screen, 100), expected)

    def test_wrap_and_clipping_use_display_columns(self):
        screen = frame('queued_four')
        block = screen[screen.index('  ↳ say hi'):screen.index(HINT)]
        body = '  ↳ 0123456789\n    界界界界界\n  ↳ ' + ('界' * 10) + '\n'
        self.assertEqual(codex_queue.parse(screen.replace(block, body), 24),
                         entries(('0123456789 界界界界界', False), ('界' * 10 + '…', True)))

    def test_full_width_wrapped_row_is_not_cut(self):
        # LONG_ROWS[0] fills the 96 columns Codex has at width 100. Codex wraps at spaces, so the text is complete.
        screen = frame('queued_four').replace(
            '    ' + LONG_ROWS[2] + '\n    …\n', '').replace('    ' + LONG_ROWS[1], '    short tail')
        self.assertEqual(len(LONG_ROWS[0]), 96)
        self.assertEqual(codex_queue.parse(screen, 100), four(LONG_ROWS[0] + ' short tail')[:1]
                         + entries((LONG_ROWS[0] + ' short tail', False), start=1)
                         + four()[2:])

    def test_steer_rows_above_the_composer_are_not_a_clipped_queue(self):
        screen = frame('queued_four')
        block = screen[screen.index('• Queued follow-up inputs'):screen.index(HINT) + len(HINT)]
        steer = '• Messages to be submitted at end of turn\n  ↳ rejected steer'
        self.assertEqual(codex_queue._parse(screen.replace(block, steer), 100), [])

    def test_keeps_the_last_50_entries_with_their_screen_indexes(self):
        screen = frame('queued_four')
        block = screen[screen.index('  ↳ say hi'):screen.index(HINT)]
        body = ''.join(f'  ↳ prompt {i}\n' for i in range(52))
        self.assertEqual(codex_queue.parse(screen.replace(block, body), 100),
                         entries(*[(f'prompt {i}', False) for i in range(2, 52)], start=2))


class QueuePayloadTests(unittest.TestCase):
    def setUp(self):
        self.entry = {'id': '%42', 'agent': 'codex', 'agent_version': '0.170.0', 'thread': 'queue-fixture'}
        self.screen = frame('queued_four')
        self.state = 'Idle'
        self.status = self.enterContext(patch.object(server, 'live_status', side_effect=self.live_status))
        self.enterContext(patch.object(server, 'snapshot', side_effect=lambda _: {'messages': []}))
        self.enterContext(patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages))
        self.enterContext(patch.object(server.continuations, '_read', return_value=None))
        self.enterContext(patch.object(codex_queue, '_refusals', set()))
        self.tmux = self.enterContext(patch.object(codex_queue.subprocess, 'run'))
        self.tmux.return_value.stdout = '100\n'

    def live_status(self, entry, *, on_screen=None):
        if on_screen is not None:
            on_screen(self.screen)
        return {'state': self.state, 'approval': None}

    def test_payload_uses_live_status_screen_in_any_state_and_keeps_queue_with_continuation(self):
        link = dict(id='fixture', history='fixture', target='codex-plan', native=False, at=0)
        for state in ('Idle', 'Working'):
            with self.subTest(state=state), \
                    patch.object(server.continuations, '_read', return_value=link), \
                    patch.object(server.continuations, '_history', return_value=[]):
                self.state = state
                payload = server.chat_payload(self.entry)
                self.assertEqual(payload['queued'], four())
                self.assertEqual(payload['state'], state)
                self.assertEqual(payload['messages'][0]['id'], 'continuation:fixture')
                self.status.assert_called_once()
                self.tmux.assert_called_once_with(
                    [paths.TMUX, 'display', '-p', '-t', '%42', '#{pane_width}'],
                    check=True, capture_output=True, text=True, timeout=5,
                )
                self.status.reset_mock()
                self.tmux.reset_mock()

    def test_refusal_hides_queue_and_logs_once_per_version_and_reason(self):
        self.screen = frame('queued_many_20rows')
        output = StringIO()
        with redirect_stderr(output):
            for _ in range(2):
                self.assertEqual(server.chat_payload(self.entry)['queued'], [])
            self.assertEqual(server.chat_payload(dict(self.entry, agent_version='0.171.0'))['queued'], [])
            self.screen = frame('queued_four').replace('    line two', '  unexpected content')
            self.assertEqual(server.chat_payload(self.entry)['queued'], [])
        self.assertEqual(output.getvalue().splitlines(), [
            'Codex 0.170.0 queue block did not match (clipped); queued prompts hidden',
            'Codex 0.171.0 queue block did not match (clipped); queued prompts hidden',
            'Codex 0.170.0 queue block did not match (unexpected row); queued prompts hidden',
        ])

    def test_no_title_skips_tmux_and_shell_mode_skips_queue(self):
        self.screen = frame('after_esc')
        self.assertEqual(server.chat_payload(self.entry)['queued'], [])
        self.screen = frame('queued_four')
        self.assertNotIn('queued', server.chat_payload(self.entry, shell_mode=True))
        self.status.assert_called_with(self.entry)
        self.tmux.assert_not_called()

    def test_width_error_preserves_the_visible_rows(self):
        self.tmux.side_effect = subprocess.TimeoutExpired('tmux display', 5)
        self.assertEqual(server.chat_payload(self.entry)['queued'], four('\n'.join(LONG_ROWS) + '…'))
        self.tmux.assert_called_once()
