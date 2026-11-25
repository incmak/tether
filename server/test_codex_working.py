"""Regression coverage for Codex rollout status and approval dismissal."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import terminal_input as terminal


FIXTURES = Path(__file__).parent / 'fixtures/codex_0.170.0_working'


def capture(name):
    return (FIXTURES / (name + '.ansi')).read_text()


def lifecycle(kind):
    return {'type': 'event_msg', 'payload': {'type': kind}}


def write_rollout(path, *records):
    path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records), encoding='utf-8')


def without_footer(screen):
    lines = terminal.prompt_actions.plain(screen).splitlines()
    composer = max(i for i, line in enumerate(lines) if line.startswith('›'))
    return '\n'.join(lines[:composer + 1]) + '\n\n'


class CodexComposerTests(unittest.TestCase):
    def test_captured_frames(self):
        frames = sorted(FIXTURES.glob('*.ansi'))
        self.assertEqual(len(frames), 12)
        for path in frames:
            with self.subTest(frame=path.name):
                expected = path.stem != 'approval_reply_quotes_status'
                self.assertIs(terminal.codex_composer_ready(terminal.prompt_actions.plain(path.read_text())), expected)

    def test_missing_composer_or_footer(self):
        self.assertIs(terminal.codex_composer_ready('Reply text\n'), False)
        self.assertIs(terminal.codex_composer_ready(without_footer(capture('working_tool'))), False)

    def test_draft_and_footer_variants(self):
        screen = terminal.prompt_actions.plain(capture('working_queued_draft'))
        self.assertIs(terminal.codex_composer_ready(screen), True)
        # A draft may start like a menu row; the Codex footer below it still verifies the composer.
        for prefix in ('1. ', '1) '):
            with self.subTest(prefix=prefix):
                self.assertIs(terminal.codex_composer_ready(screen.replace('› draft', '› ' + prefix + 'draft')), True)
        hint = '  tab to queue message'
        for row in (hint + ' · Plan mode', hint + ' · Plan mode (shift+tab to cycle)', '  Plan mode'):
            with self.subTest(hint=row):
                self.assertIs(terminal.codex_composer_ready(screen.replace(hint, row)), True)


class CodexWorkingStatusTests(unittest.TestCase):
    def setUp(self):
        self.rollout = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'rollout.jsonl'

    def status(self, screen, live):
        entry = {'id': '%codex-working', 'agent': 'codex', 'transcript': self.rollout}
        run = Mock(side_effect=AssertionError('status must not touch tmux'))
        with patch.object(terminal, 'capture_quota_notices'), patch.object(terminal, 'attention', return_value=None), \
                patch.object(terminal.prompt_actions, 'panel', return_value=None), \
                patch.object(terminal.claude_hooks, 'codex_live', return_value=live):
            return terminal._live_status(entry, run, screen)

    def test_without_hooks_uses_rollout_even_when_reply_looks_live(self):
        write_rollout(self.rollout, lifecycle('task_complete'))
        screen = capture('idle_reply_is_status_line')
        self.assertEqual(self.status(screen, None)['state'], 'Idle')
        lines = terminal.prompt_actions.plain(screen).splitlines()
        replay = '\n'.join(line for line in lines if not line.lstrip().startswith('Worked for'))
        self.assertIn('Worked for', screen)
        self.assertNotIn('Worked for', replay)
        self.assertEqual(self.status(replay, None)['state'], 'Idle')

    def test_without_hooks_a_started_turn_is_working(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        self.assertEqual(self.status(capture('working_tool'), None)['state'], 'Working')

    def test_waiting_hook_clears_when_composer_returns(self):
        live = {'waiting': True, 'busy': False}
        write_rollout(self.rollout, lifecycle('task_started'))
        for frame in ('idle_reply_quotes_status', 'working_after_approval', 'working_queued_draft'):
            with self.subTest(frame=frame):
                self.assertEqual(self.status(capture(frame), live)['state'], 'Working')

    def test_waiting_hook_keeps_visible_approval(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        result = self.status(capture('approval_reply_quotes_status'), {'waiting': True, 'busy': False})
        self.assertEqual(result['state'], 'Approval')
        self.assertTrue(result['approval']['options'])

    def test_waiting_hook_keeps_read_only_approval_without_verified_composer(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        result = self.status(without_footer(capture('working_tool')), {'waiting': True, 'busy': False})
        self.assertEqual(result['state'], 'Approval')
        self.assertFalse(result['approval']['actionable'])


class CodexRolloutBusyTests(unittest.TestCase):
    def setUp(self):
        self.rollout = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'rollout.jsonl'

    def test_ignores_lifecycle_text_in_messages_and_tool_outputs(self):
        quoted = '{"type":"task_started"}'
        write_rollout(self.rollout, lifecycle('task_complete'),
                      {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                       'content': [{'type': 'output_text', 'text': quoted}]}},
                      {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': quoted}})
        self.assertFalse(terminal.transcript_busy(self.rollout))

    def test_requires_lifecycle_type_in_event_payload(self):
        write_rollout(self.rollout, lifecycle('task_complete'),
                      {'type': 'response_item', 'payload': {'type': 'task_started'}},
                      {'type': 'event_msg', 'payload': {'type': 'token_count', 'other': {'type': 'task_started'}}},
                      {'type': 'event_msg', 'payload': None}, [])
        self.assertFalse(terminal.transcript_busy(self.rollout))

    def test_finds_latest_lifecycle_before_initial_window(self):
        output = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 1024}}
        for kind, expected in (('task_started', True), ('task_complete', False)):
            with self.subTest(kind=kind):
                write_rollout(self.rollout, lifecycle('task_started'), lifecycle(kind), *([output] * 301))
                self.assertGreater(self.rollout.stat().st_size, 300 * 1024)
                self.assertIs(terminal.transcript_busy(self.rollout), expected)

    def test_ignores_partial_utf8_at_window_edge(self):
        write_rollout(self.rollout, {'type': 'response_item', 'payload': {'text': '€'}}, lifecycle('task_started'))
        data = self.rollout.read_bytes()
        start = data.index('€'.encode('utf-8')) + 1
        self.assertTrue(terminal.transcript_busy(self.rollout, window=len(data) - start))
