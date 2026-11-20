"""Start times come from bounded transcript heads, not the loaded message window."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
import thread_started
from test_tts import handler

FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'


class ThreadStartedTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.folder) / 'thread.jsonl'
        thread_started._cache.clear()
        self.addCleanup(thread_started._cache.clear)

    def entry(self, agent='claude', **extra):
        return dict({'id': '%fixture', 'thread': 'fixture-thread', 'agent': agent, 'transcript': str(self.path)}, **extra)

    def write(self, records):
        self.path.write_text(''.join(json.dumps(record) + '\n' for record in records))

    def test_claude_first_timestamp_and_codex_session_start_fixtures(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent, transcript=str(FIXTURES / ('started-' + agent + '.jsonl')))
                self.assertEqual(thread_started.started(entry), '2026-11-08T08:29:11Z')

    def test_codex_session_meta_payload_timestamp_and_first_record_fallback(self):
        for rows, expected in (([{'type': 'session_meta', 'payload': {'timestamp': '2026-11-08T08:29:11.123Z'}}],
                                '2026-11-08T08:29:11.123000Z'),
                               ([{'type': 'response_item', 'timestamp': '2026-11-08T08:29:11Z'}],
                                '2026-11-08T08:29:11Z'),
                               ([{'type': 'session_meta', 'payload': {}},
                                 {'type': 'response_item', 'timestamp': '2026-11-08T08:29:11Z'}],
                                '2026-11-08T08:29:11Z')):
            self.write(rows)
            thread_started._cache.clear()
            self.assertEqual(thread_started.started(self.entry('codex')), expected)

    def test_antigravity_created_at_or_unknown(self):
        for record, expected in (({'type': 'USER_INPUT', 'created_at': '2026-11-08T08:29:11Z'}, '2026-11-08T08:29:11Z'),
                                 ({'type': 'USER_INPUT', 'content': 'No time'}, None)):
            self.write([record])
            thread_started._cache.clear()
            self.assertEqual(thread_started.started(self.entry('antigravity')), expected)

    def test_unknown_thread_shell_missing_file_and_invalid_times(self):
        self.assertIsNone(thread_started.started(self.entry()))
        self.write([{'timestamp': '2026-11-08T08:29:11Z'}])
        for entry in (self.entry(thread=None), self.entry('shell'), self.entry(transcript=None), self.entry(ambiguous=True)):
            self.assertIsNone(thread_started.started(entry))
        self.write([{}, {'timestamp': None}, {'timestamp': 123}, {'timestamp': 'bad'}, {'timestamp': '2026-11-08T08:29:11'}])
        self.assertIsNone(thread_started.started(self.entry()))

    def test_malformed_records_are_skipped(self):
        self.path.write_bytes(b'not json\n[]\n\xff\n{"timestamp":"2026-11-08T08:29:11Z"}\n')
        self.assertEqual(thread_started.started(self.entry()), '2026-11-08T08:29:11Z')

    def test_known_start_is_cached_even_after_append(self):
        self.write([{'timestamp': '2026-11-08T08:29:11Z'}])
        with patch.object(thread_started, '_read', wraps=thread_started._read) as read:
            self.assertEqual(thread_started.started(self.entry()), '2026-11-08T08:29:11Z')
            with self.path.open('a') as stream:
                stream.write('{"timestamp":"2026-11-09T00:00:00Z"}\n')
            self.assertEqual(thread_started.started(self.entry()), '2026-11-08T08:29:11Z')
            read.assert_called_once()

    def test_replacement_and_truncation_invalidate_cache(self):
        self.write([{'timestamp': '2026-11-08T08:29:11Z'}, {'padding': 'x' * 200}])
        self.assertIsNotNone(thread_started.started(self.entry()))
        replacement = self.path.with_suffix('.new')
        replacement.write_text('{"timestamp":"2026-11-09T00:00:00Z"}\n')
        replacement.replace(self.path)
        self.assertEqual(thread_started.started(self.entry()), '2026-11-09T00:00:00Z')
        self.path.write_text('{}\n')
        self.assertIsNone(thread_started.started(self.entry()))

    def test_unknown_start_is_cached_until_file_changes(self):
        self.path.touch()
        with patch.object(thread_started, '_read', wraps=thread_started._read) as read:
            self.assertIsNone(thread_started.started(self.entry()))
            self.assertIsNone(thread_started.started(self.entry()))
            read.assert_called_once()
            self.write([{'timestamp': '2026-11-08T08:29:11Z'}])
            self.assertEqual(thread_started.started(self.entry()), '2026-11-08T08:29:11Z')

    def test_same_inode_rewrite_and_truncation_after_growth_invalidate_cache(self):
        self.write([{'timestamp': '2026-11-08T08:29:11Z'}])
        self.assertEqual(thread_started.started(self.entry()), '2026-11-08T08:29:11Z')
        before = self.path.stat()
        self.write([{'timestamp': '2026-11-09T08:29:11Z'}])
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        self.assertEqual(thread_started.started(self.entry()), '2026-11-09T08:29:11Z')
        with self.path.open('a') as stream:
            stream.write(json.dumps({'padding': 'x' * 300}) + '\n')
        self.assertEqual(thread_started.started(self.entry()), '2026-11-09T08:29:11Z')
        self.write([{'timestamp': '2026-11-10T08:29:11Z'}, {'padding': 'small'}])
        self.assertEqual(thread_started.started(self.entry()), '2026-11-10T08:29:11Z')

    def test_reads_only_bounded_head_and_cache_is_bounded(self):
        self.path.touch()
        stream = io.BytesIO(b'{"timestamp":"2026-11-08T08:29:11Z"}\n' + b'x' * thread_started.HEAD_BYTES)
        with patch('builtins.open', return_value=stream), patch.object(stream, 'read', wraps=stream.read) as read:
            self.assertIsNotNone(thread_started.started(self.entry()))
            read.assert_called_once_with(thread_started.HEAD_BYTES)
        with patch.object(thread_started, 'CACHE_LIMIT', 2):
            for index in range(3):
                path = Path(self.folder) / str(index)
                path.touch()
                thread_started.started(self.entry(transcript=str(path)))
        self.assertEqual(len(thread_started._cache), 2)

    def test_chat_payload_start_does_not_depend_on_message_limit(self):
        self.path.write_bytes((FIXTURES / 'started-claude.jsonl').read_bytes())
        messages = [{'id': str(index), 'text': 'Later', 'time': '2026-11-09T00:00:00Z'} for index in range(300)]
        with patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}), \
                patch.object(server, 'snapshot', return_value={'messages': messages}), \
                patch.object(server.shell_log, 'merge', side_effect=lambda pane, rows: rows):
            for limit in (20, 150, 2000):
                data = server.chat_payload(self.entry(), limit=limit)
                self.assertEqual(data['started'], '2026-11-08T08:29:11Z')
                self.assertEqual(len(data['messages']), min(limit, 300))

    def test_get_chat_route_includes_started(self):
        self.path.write_bytes((FIXTURES / 'started-codex.jsonl').read_bytes())
        value = handler('/mobile/api/chat?session=%25fixture&limit=20', method='GET')
        with patch.object(server, 'choose', return_value=self.entry('codex')), \
                patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}), \
                patch.object(server, 'snapshot', return_value={'messages': []}), \
                patch.object(server.shell_log, 'merge', side_effect=lambda pane, rows: rows):
            server.Handler.do_GET(value)
        value.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(value.wfile.getvalue())['started'], '2026-11-08T08:29:11Z')

    def test_shell_payload_includes_null_start(self):
        with patch.object(server, 'shell_snapshot', return_value={'messages': []}):
            data = server.chat_payload(self.entry('shell'), shell_mode=True)
        self.assertIsNone(data['started'])


if __name__ == '__main__':
    unittest.main()
