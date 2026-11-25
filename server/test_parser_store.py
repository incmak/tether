import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import coordinator
import history
import parser_store
import paths
import review_links
import server


FIXTURES = Path(__file__).parent / 'fixtures' / 'coordinator_failed'
REQUEST_ID = 'a1b2c3d4'
REQUEST = (f'[Tether review] The Codex tab “Worker” (%13) finished a turn. {review_links.INTRO} '
           f'Review id: {REQUEST_ID}. Its last reply:\n\nDone.\n\nTranscript: /t.jsonl')


def lines(*records):
    return b''.join(json.dumps(record, ensure_ascii=False).encode() + b'\n' for record in records)


def claude(role, text, **fields):
    return {'type': role, 'message': dict(role=role, content=text, **fields)}


def codex(role, text, **fields):
    return {'type': 'response_item', 'payload': dict(type='message', role=role, content=[
        {'type': 'input_text' if role == 'user' else 'output_text', 'text': text}], **fields)}


def event(kind, **fields):
    return {'type': 'event_msg', 'payload': dict(type=kind, **fields)}


CLAUDE_END = {'type': 'system', 'subtype': 'turn_duration'}


class ParserCheckpoints(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, 'PARSERS', self.folder / 'private' / 'parsers'))
        self.path = self.folder / 'transcript.jsonl'

    def advance(self, store, *, pane='%1', agent='claude', consume=coordinator._claude_turn):
        info = self.path.stat()
        key = (agent, str(self.path), info.st_dev, info.st_ino)
        with store.lock, history._lock_for(str(self.path)):
            store.advance(pane, key, info, lambda: {'kind': None, 'text': '', 'identity': ''}, consume)
            return store.records[pane][1]

    def assert_same_record(self, actual, expected):
        # advance treats an absent unterminated flag as False and may pop it after consuming a trailing newline.
        normalize = lambda record: json.loads(json.dumps({'unterminated': False, **record}))
        self.assertEqual(normalize(actual), normalize(expected))

    def test_turn_parsers_match_uninterrupted_reads_at_record_boundaries_and_mid_record(self):
        endings = {
            'claude': lines(claude('user', 'Third task 🌱'), claude('assistant', 'Final result.', stop_reason='tool_use'),
                            CLAUDE_END),
            'codex': lines(event('task_started'), event('agent_message', message='Final result.'), event('task_complete')),
        }
        for agent, ending in endings.items():
            consume = getattr(coordinator, f'_{agent}_turn')
            fixture = (FIXTURES / f'{agent}.jsonl').read_bytes()
            data = fixture + ending
            self.path.write_bytes(data)
            baseline = self.advance(parser_store.ParserStore('baseline', 1), agent=agent, consume=consume)
            self.assertEqual(baseline['state']['identity'], str(fixture.count(b'\n')))
            self.assertEqual(baseline['state']['text'], 'Final result.')
            first = ending.index(b'\n')
            second = ending.index(b'\n', first + 1)
            for number, cut in enumerate((0, first // 2, first, first + 1, second + 1, len(ending) - 1)):
                with self.subTest(agent=agent, cut=cut):
                    self.path.write_bytes(fixture + ending[:cut])
                    store = parser_store.ParserStore(f'{agent}-{number}', 1)
                    self.advance(store, agent=agent, consume=consume)
                    store.save()
                    with self.path.open('ab') as stream:
                        stream.write(ending[cut:])
                    restart = parser_store.ParserStore(store.name, 1)
                    self.assertIn('%1', restart.records)
                    resumed = self.advance(restart, agent=agent, consume=consume)
                    self.assert_same_record(resumed, baseline)

    def test_invalid_checkpoints_restart_only_the_affected_parser_from_zero(self):
        original = lines(claude('user', 'Task 🌱'), claude('assistant', 'Old verdict'), CLAUDE_END)
        new = original.replace(b'Old verdict', b'New verdict')
        for change in ('version', 'path', 'device', 'inode', 'truncated', 'rewrite', 'rewrite and append', 'read error'):
            with self.subTest(change=change):
                self.path.write_bytes(original)
                store = parser_store.ParserStore(change, 1)
                before = self.advance(store)
                self.assertEqual(before['state']['text'], 'Old verdict')
                other = self.path.with_name('other.jsonl')
                other.write_bytes(original)
                with patch.object(self, 'path', other):
                    self.advance(store, pane='%2')
                store.save()
                if change in ('path', 'device'):
                    saved = json.loads(store.path.read_text())
                    key = saved['records']['%1']['key']
                    if change == 'path':
                        key[1] = str(other)
                    else:
                        key[2] += 1
                    store.path.write_text(json.dumps(saved))
                if change == 'inode':
                    replacement = self.path.with_suffix('.new')
                    replacement.write_bytes(new)
                    replacement.replace(self.path)
                elif change == 'truncated':
                    self.path.write_bytes(new[:-len(lines(CLAUDE_END))])
                elif change.startswith('rewrite'):
                    self.path.write_bytes(new + (lines(CLAUDE_END) if change.endswith('append') else b''))
                restart = parser_store.ParserStore(store.name, 2 if change == 'version' else 1)
                native_open = open

                def read(path, *args, **kwargs):
                    if change == 'read error' and path == str(self.path):
                        raise PermissionError('closed transcript')
                    return native_open(path, *args, **kwargs)

                with patch('builtins.open', side_effect=read):
                    self.assertEqual(set(restart.records), set() if change == 'version' else {'%2'})
                with patch.object(coordinator, '_claude_turn', wraps=coordinator._claude_turn) as consume:
                    state = self.advance(restart, consume=consume)['state']
                    self.assertEqual(consume.call_args_list[0].args[1], 0)
                self.assertEqual(state['text'], 'New verdict' if change in (
                    'inode', 'truncated', 'rewrite', 'rewrite and append') else 'Old verdict')

    def test_atomic_checkpoints_are_private_throttled_and_flush_on_exit(self):
        now = 0
        store = parser_store.ParserStore('feed', 1, clock=lambda: now)
        self.assertEqual(store.records, {})
        self.path.write_bytes(lines(claude('user', 'x' * 5000), claude('assistant', 'Done.')))
        self.advance(store)
        store.save()
        initial = store.path.read_bytes()
        digest = json.loads(initial)['records']['%1']['sha256']
        self.assertEqual(digest, hashlib.sha256(self.path.read_bytes()[-4096:]).hexdigest())
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        with self.path.open('ab') as stream:
            stream.write(lines(CLAUDE_END))
        self.advance(store)
        now = 29
        store.save()
        self.assertEqual(store.path.read_bytes(), initial)
        now = 30
        with patch.object(parser_store.os, 'replace', side_effect=OSError('cannot publish')) as replace:
            store.save()
            replace.assert_called_once()
        self.assertEqual(store.path.read_bytes(), initial)
        self.assertEqual(list(store.path.parent.iterdir()), [store.path])
        store.save(force=True)
        self.assertNotEqual(store.path.read_bytes(), initial)
        now = 60
        self.advance(store)
        with patch.object(store, '_write') as write:
            store.save()
            store.save(force=True)
            write.assert_not_called()
        store.path.write_text('{broken')
        self.assertEqual(parser_store.ParserStore(store.name, 1).records, {})

    def test_rewrite_before_first_save_cannot_checkpoint_stale_state(self):
        store = parser_store.ParserStore('feed', 1)
        self.path.write_bytes(lines(claude('assistant', 'Old reply')))
        self.advance(store)
        self.path.write_bytes(lines(claude('assistant', 'New reply')))
        store.save()
        restart = parser_store.ParserStore(store.name, 1)
        self.assertEqual(self.advance(restart)['state']['text'], 'New reply')

    def test_append_during_checkpoint_keeps_the_previous_valid_offset(self):
        store = parser_store.ParserStore('feed', 1)
        first = lines(claude('assistant', 'First reply'))
        second = lines(CLAUDE_END, claude('assistant', 'Second reply'))
        self.path.write_bytes(first)
        self.advance(store)
        store.save()
        with self.path.open('ab') as stream:
            stream.write(second)
        self.advance(store)
        with self.path.open('ab') as stream:
            stream.write(lines(CLAUDE_END))
        store.save(force=True)
        restart = parser_store.ParserStore(store.name, 1)
        self.assertEqual(restart.records['%1'][1]['offset'], len(first))
        self.assertEqual(self.advance(restart)['state']['text'], 'Second reply')

    def test_feed_restarts_at_the_saved_offset_even_after_a_revision(self):
        feed = coordinator.Feed()
        entry = {'id': '%1', 'agent': 'claude', 'transcript': self.path}
        original = lines(claude('user', 'Task'), claude('assistant', 'Old reply'), CLAUDE_END)
        self.path.write_bytes(original)
        feed.latest_outcome(entry)
        info = self.path.stat()
        self.path.write_bytes(original.replace(b'Old reply', b'New reply'))
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        outcome = feed.latest_outcome(entry)
        self.assertEqual(outcome['text'], 'New reply')
        self.assertNotEqual(feed.parsers['%1'][0][-1], 0)
        feed.parser_store.save()
        restart = coordinator.Feed()
        self.assertEqual((restart.seen, restart.outcomes), ({}, {}))
        with patch.object(coordinator, '_claude_turn', wraps=coordinator._claude_turn) as consume:
            self.assertEqual(restart.latest_outcome(entry), outcome)
            consume.assert_not_called()
        restart.parser_store.prune(set())
        restart.parser_store.save()
        self.assertEqual(coordinator.Feed().parsers, {})

    def test_review_pending_survives_restart_and_chat_cache_churn(self):
        for agent in ('claude', 'codex'):
            prompt, answer, end = ((claude('user', REQUEST), claude('assistant', 'APPROVED', stop_reason='end_turn'),
                                    CLAUDE_END) if agent == 'claude' else (
                                    codex('user', REQUEST), codex('assistant', 'APPROVED', phase='final_answer'),
                                    event('task_complete')))
            data = lines(prompt, answer, end)
            for number, cut in enumerate((len(lines(prompt)), len(lines(prompt, answer)) - 4,
                                          len(lines(prompt, answer)), len(data) - 1)):
                with self.subTest(agent=agent, cut=cut):
                    self.path.write_bytes(data)
                    entry = {'id': '%1', 'agent': agent, 'transcript': self.path}
                    baseline = parser_store.ParserStore('baseline', 1, pending=review_links.REPLY_PENDING)
                    with patch.object(server, 'REVIEW_PARSERS', baseline):
                        self.assertEqual(server.review_reply(entry, REQUEST_ID), 'APPROVED')
                    store = parser_store.ParserStore(f'{agent}-{number}', 1, pending=review_links.REPLY_PENDING)
                    self.path.write_bytes(data[:cut])
                    expected = 'APPROVED' if cut == len(data) - 1 else review_links.REPLY_PENDING
                    with patch.object(server, 'REVIEW_PARSERS', store):
                        self.assertEqual(server.review_reply(entry, REQUEST_ID), expected)
                    store.save()
                    restart = parser_store.ParserStore(store.name, 1, pending=review_links.REPLY_PENDING)
                    with patch.object(server, 'REVIEW_PARSERS', restart), patch.object(history, '_states', {}), \
                            patch.object(server, '_review_record', wraps=server._review_record) as consume:
                        result = server.review_reply(entry, REQUEST_ID)
                        if expected is review_links.REPLY_PENDING:
                            self.assertIs(result, review_links.REPLY_PENDING)
                        else:
                            self.assertEqual(result, expected)
                        for i in range(history.MAX_FILES + 3):
                            history.read(self.path, dict, lambda *_: None, namespace=('chat', i))
                        self.assertEqual(server.review_reply(entry, REQUEST_ID), expected)
                        consume.assert_not_called()
                        with self.path.open('ab') as stream:
                            stream.write(data[cut:])
                        self.assertEqual(server.review_reply(entry, REQUEST_ID), 'APPROVED')
                        if cut < len(lines(prompt, answer)):
                            self.assertEqual(consume.call_args_list[0].args[1], data[:cut].count(b'\n'))
                        self.assert_same_record(restart.records['%1'][1], baseline.records['%1'][1])

    def test_antigravity_bypasses_both_parser_stores(self):
        entry = {'id': '%1', 'agent': 'antigravity', 'transcript': self.path}
        with patch.object(parser_store.ParserStore, 'advance', side_effect=AssertionError) as advance:
            self.assertEqual(coordinator.Feed().latest_outcome(entry), {'kind': None, 'text': '', 'identity': ''})
            self.assertIsNone(server.review_reply(entry, REQUEST_ID))
        advance.assert_not_called()
