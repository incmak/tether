"""Regression coverage for Codex rollout status and approval dismissal."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import claude_hooks as hooks
from own_time import own_time
import paths
import terminal_input as terminal


FIXTURES = Path(__file__).parent / 'fixtures/codex_0.170.0_working'
REAL_HOOK = object()


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
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rollout = self.root / 'rollout.jsonl'
        self.thread = 'codex-working'
        self.enterContext(patch.object(paths, 'CACHE', self.root))

    def entry(self):
        return {'id': '%codex-working', 'agent': 'codex', 'thread': self.thread, 'transcript': self.rollout}

    def status(self, screen, live=REAL_HOOK):
        run = Mock(side_effect=AssertionError('status must not touch tmux'))
        hook = nullcontext() if live is REAL_HOOK else patch.object(hooks, 'codex_live', return_value=live)
        with patch.object(terminal, 'capture_quota_notices'), patch.object(terminal, 'attention', return_value=None), \
                patch.object(terminal.prompt_actions, 'panel', return_value=None), hook:
            return terminal._live_status(self.entry(), run, screen)

    def test_live_status_identifies_the_fresh_codex_banner_for_context(self):
        entry = dict(self.entry(), owner_pid=1, writable=True)
        restart = FIXTURES.with_name('codex_0.170.0_restart')
        for screen, expected in ((restart / 'idle.ansi', True), (restart / 'idle_narrow.ansi', True),
                                 (FIXTURES / 'idle_reply_is_status_line.ansi', False)):
            with self.subTest(screen=screen.name), patch.object(terminal, 'capture', return_value=screen.read_text()), \
                    patch.object(terminal, 'login_capture', return_value=''), \
                    patch.object(terminal, '_live_status', return_value={'state': 'Idle'}):
                self.assertIs(terminal.live_status(entry).get('_codex_fresh', False), expected)

    def waiting_hook(self):
        reported = 100
        self.enterContext(own_time(hooks, time=Mock(return_value=110)))
        folder = self.root / 'hooks'
        folder.mkdir()
        (folder / (self.thread + '.json')).write_text(json.dumps({
            'session_id': self.thread, 'agent': 'codex', 'waiting': True, 'busy': True, 'reported_at': reported,
        }))
        return reported

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

    def test_waiting_hook_clears_after_recorded_turn_end(self):
        reported = self.waiting_hook()
        stamp = datetime.fromtimestamp(reported + 0.5, timezone.utc).isoformat()
        for kind in ('turn_aborted', 'task_complete'):
            with self.subTest(kind=kind):
                self.rollout = self.root / (kind + '.jsonl')
                write_rollout(self.rollout, lifecycle('task_started'), dict(lifecycle(kind), timestamp=stamp))
                self.assertIs(hooks.codex_live(self.entry())['waiting'], True)
                self.assertEqual(self.status(capture('idle_interrupted')), {'state': 'Idle', 'approval': None})

    def test_waiting_hook_keeps_visible_approval(self):
        reported = self.waiting_hook()
        stamp = datetime.fromtimestamp(reported - 1, timezone.utc).isoformat()
        write_rollout(self.rollout, dict(lifecycle('task_started'), timestamp=stamp))
        self.assertIs(hooks.codex_live(self.entry())['waiting'], True)
        result = self.status(capture('approval_reply_quotes_status'))
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

    def append(self, data):
        with self.rollout.open('ab') as stream:
            stream.write(data)

    def poll(self, expected):
        read = terminal._rollout_read
        bytes_read = 0

        def count(stream, size):
            nonlocal bytes_read
            data = read(stream, size)
            bytes_read += len(data)
            return data

        with patch.object(terminal, '_rollout_read', side_effect=count):
            self.assertIs(terminal.transcript_busy(self.rollout), expected)
        return bytes_read

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
                self.rollout = self.rollout.with_name(kind + '.jsonl')
                write_rollout(self.rollout, lifecycle('task_started'), lifecycle(kind), *([output] * 301))
                self.assertGreater(self.rollout.stat().st_size, 300 * 1024)
                self.assertIs(terminal.transcript_busy(self.rollout), expected)

    def test_ignores_partial_utf8_at_window_edge(self):
        write_rollout(self.rollout, {'type': 'event_msg', 'payload': {'type': 'task_started', 'text': '€'}})
        data = self.rollout.read_bytes()
        start = data.index('€'.encode('utf-8')) + 1
        self.assertTrue(terminal.transcript_busy(self.rollout, window=len(data) - start))

    def test_second_poll_reads_only_appended_bytes(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        self.assertTrue(terminal.transcript_busy(self.rollout))
        data = b'\n'.join(json.dumps(lifecycle(kind)).encode() for kind in ('task_complete', 'task_started')) + b'\n'
        self.append(data)
        self.assertEqual(self.poll(True), len(data))
        self.assertEqual(self.poll(True), 0)

    def test_large_rollout_reads_each_byte_at_most_once(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        line = json.dumps({'type': 'response_item', 'payload': {'output': 'x' * 1024}}).encode() + b'\n'
        self.append(line * 8192)
        size = self.rollout.stat().st_size
        self.assertGreater(size, 8 * 1024 * 1024)
        self.assertLessEqual(self.poll(True), size)
        self.append(line * 10)
        self.assertEqual(self.poll(True), len(line) * 10)
        complete = json.dumps(lifecycle('task_complete')).encode() + b'\n'
        self.append(complete)
        self.assertEqual(self.poll(False), len(complete))

    def test_large_append_is_read_in_bounded_blocks(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        self.assertTrue(terminal.transcript_busy(self.rollout, window=4096))
        line = json.dumps({'type': 'response_item', 'payload': {'output': 'x' * 1000}}).encode() + b'\n'
        complete = json.dumps(lifecycle('task_complete')).encode() + b'\n'
        self.append(line * 1024 + complete + line * 3)
        sizes = []
        read = terminal._rollout_read

        def bounded(stream, size):
            sizes.append(size)
            return read(stream, size)

        with patch.object(terminal, '_rollout_read', side_effect=bounded):
            self.assertIs(terminal.transcript_busy(self.rollout, window=4096), False)
        self.assertEqual(sum(sizes), len(line) * 1027 + len(complete))
        self.assertLessEqual(max(sizes), 4096)

    def test_incomplete_lifecycle_is_counted_only_after_newline(self):
        write_rollout(self.rollout, lifecycle('task_started'))
        complete = json.dumps(lifecycle('task_complete')).encode()
        start = json.dumps(lifecycle('task_started')).encode()
        self.append(complete[:len(complete) // 2])
        self.poll(True)
        self.assertEqual(self.poll(True), 0)
        rest = complete[len(complete) // 2:]
        self.append(rest)
        self.assertEqual(self.poll(True), len(rest))
        self.append(b'\n')
        self.assertEqual(self.poll(False), 1)
        self.append(start[:len(start) // 2])
        self.assertEqual(self.poll(False), len(start) // 2)
        rest = start[len(start) // 2:] + b'\n'
        self.append(rest)
        self.assertEqual(self.poll(True), len(rest))

    def test_inode_change_resets_rollout(self):
        write_rollout(self.rollout, lifecycle('task_complete'))
        self.poll(False)
        replacement = self.rollout.with_name('replacement.jsonl')
        write_rollout(replacement, lifecycle('task_started'), lifecycle('token_count'))
        self.assertGreater(replacement.stat().st_size, self.rollout.stat().st_size)
        os.replace(replacement, self.rollout)
        self.assertEqual(self.poll(True), self.rollout.stat().st_size)

    def test_truncation_resets_rollout(self):
        write_rollout(self.rollout, lifecycle('task_started'), lifecycle('token_count'))
        self.poll(True)
        initial = self.rollout.stat()
        write_rollout(self.rollout, lifecycle('task_complete'))
        truncated = self.rollout.stat()
        self.assertEqual(truncated.st_ino, initial.st_ino)
        self.assertLess(truncated.st_size, initial.st_size)
        self.assertEqual(self.poll(False), truncated.st_size)
