import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
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
                self.assertEqual(set(restart.records), set() if change == 'version' else {'%1', '%2'})
                open_fd = os.open

                def read(path, flags, *args):
                    if change == 'read error' and path == str(self.path):
                        raise PermissionError('closed transcript')
                    return open_fd(path, flags, *args)

                with patch.object(parser_store.os, 'open', side_effect=read), \
                        patch.object(coordinator, '_claude_turn', wraps=coordinator._claude_turn) as consume:
                    state = self.advance(restart, consume=consume)['state']
                    self.assertEqual(consume.call_args_list[0].args[1], 0)
                self.assertEqual(state['text'], 'New verdict' if change in (
                    'inode', 'truncated', 'rewrite', 'rewrite and append') else 'Old verdict')
                if change != 'version':
                    with patch.object(self, 'path', other), \
                            patch.object(coordinator, '_claude_turn', wraps=coordinator._claude_turn) as consume:
                        self.assertEqual(self.advance(restart, pane='%2', consume=consume)['state']['text'], 'Old verdict')
                        consume.assert_not_called()

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
        original = lines(claude('assistant', 'Old reply'))
        for number, suffix in enumerate((b'', lines(CLAUDE_END))):
            with self.subTest(append=bool(suffix)):
                store = parser_store.ParserStore(f'rewrite-{number}', 1)
                self.path.write_bytes(original)
                self.advance(store)
                self.path.write_bytes(original.replace(b'Old reply', b'New reply') + suffix)
                with patch.object(parser_store, '_digest', wraps=parser_store._digest) as digest:
                    store.save(force=True)
                    digest.assert_not_called()
                saved = json.loads(store.path.read_text())['records']['%1']
                self.assertEqual(saved['sha256'], hashlib.sha256(original).hexdigest())
                restart = parser_store.ParserStore(store.name, 1)
                self.assertEqual(self.advance(restart)['state']['text'], 'New reply')

    def test_append_before_save_persists_the_latest_parsed_offset(self):
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
        self.assertEqual(restart.records['%1'][1]['offset'], len(first + second))
        self.assertEqual(self.advance(restart)['state']['text'], 'Second reply')

    def test_rewrite_after_load_or_advance_discards_old_feed_state(self):
        original = lines(claude('assistant', 'Old reply'))
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                self.path.write_bytes(original)
                feed = coordinator.Feed()
                entry = {'id': '%1', 'agent': 'claude', 'transcript': self.path}
                self.assertEqual(feed.latest_outcome(entry)['text'], 'Old reply')
                feed.parser_store.save()
                restart = coordinator.Feed()
                self.assertIn('%1', restart.parsers)
                if resumed:
                    self.assertEqual(restart.latest_outcome(entry)['text'], 'Old reply')
                self.path.write_bytes(original.replace(b'Old reply', b'New reply') + lines(CLAUDE_END))
                self.assertEqual(restart.latest_outcome(entry)['text'], 'New reply')

    def test_feed_checks_a_same_size_rewrite_with_unchanged_mtime(self):
        original = lines(claude('assistant', 'Old reply'))
        self.path.write_bytes(original)
        os.utime(self.path, ns=(0, 0))
        feed = coordinator.Feed()
        entry = {'id': '%1', 'agent': 'claude', 'transcript': self.path}
        self.assertEqual(feed.latest_outcome(entry)['text'], 'Old reply')
        self.path.write_bytes(original.replace(b'Old reply', b'New reply'))
        os.utime(self.path, ns=(0, 0))
        self.assertEqual(feed.latest_outcome(entry)['text'], 'New reply')

    def test_feed_reparses_invalid_saved_state(self):
        self.path.write_bytes(lines(claude('assistant', 'Done.')))
        entry = {'id': '%1', 'agent': 'claude', 'transcript': self.path}
        feed = coordinator.Feed()
        expected = feed.latest_outcome(entry)
        feed.parser_store.save()
        path = feed.parser_store.path
        saved = path.read_text()
        for key, value in (('kind', None), ('text', [])):
            with self.subTest(key=key):
                malformed = json.loads(saved)
                state = malformed['records']['%1']['record']['state']
                if key == 'kind':
                    del state[key]
                else:
                    state[key] = value
                path.write_text(json.dumps(malformed))
                self.assertEqual(coordinator.Feed().latest_outcome(entry), expected)

    def test_review_reparses_saved_state_without_a_reply_map(self):
        self.path.write_bytes(lines(claude('user', REQUEST), claude('assistant', 'APPROVED', stop_reason='end_turn'),
                                    CLAUDE_END))
        entry = {'id': '%1', 'agent': 'claude', 'transcript': self.path}
        store = parser_store.ParserStore('review', 1, pending=review_links.REPLY_PENDING)
        with patch.object(server, 'REVIEW_PARSERS', store):
            self.assertEqual(server.review_reply(entry, REQUEST_ID), 'APPROVED')
        store.save()
        saved = json.loads(store.path.read_text())
        saved['records']['%1']['record']['state']['replies'] = []
        store.path.write_text(json.dumps(saved))
        restart = parser_store.ParserStore(store.name, 1, pending=review_links.REPLY_PENDING)
        with patch.object(server, 'REVIEW_PARSERS', restart):
            self.assertEqual(server.review_reply(entry, REQUEST_ID), 'APPROVED')

    def test_consume_error_on_resume_replays_from_zero(self):
        first = lines(claude('assistant', 'Done.', stop_reason='tool_use'))
        for error in (ValueError, TypeError):
            with self.subTest(error=error):
                self.path.write_bytes(first)
                store = parser_store.ParserStore('resume', 1)
                self.advance(store)
                store.save(force=True)
                saved = json.loads(store.path.read_text())
                saved['records']['%1']['record']['state']['draft'] = 'invalid draft'
                store.path.write_text(json.dumps(saved))
                with self.path.open('ab') as stream:
                    stream.write(lines(CLAUDE_END))
                calls = []

                def consume(state, index, record):
                    calls.append(index)
                    if state.get('draft') == 'invalid draft':
                        raise error('invalid draft')
                    coordinator._claude_turn(state, index, record)

                restart = parser_store.ParserStore(store.name, 1)
                self.assertEqual(self.advance(restart, consume=consume)['state']['text'], 'Done.')
                self.assertEqual(calls, [1, 0, 1])

    def test_split_crlf_matches_uninterrupted_parse(self):
        first = lines(CLAUDE_END)[:-1]
        turn = lines(claude('user', 'Task'), claude('assistant', 'Done.'))
        data = first + b'\r\n' + turn
        self.path.write_bytes(data)
        expected = self.advance(parser_store.ParserStore('baseline', 1))
        self.assertEqual(expected['state']['identity'], '1')
        for prefix, appends in ((first, [b'\r\n' + turn]), (first + b'\r', [b'\n' + turn]),
                                (first, [b'\r', b'\n' + turn])):
            with self.subTest(prefix=len(prefix), writes=len(appends)):
                self.path.write_bytes(prefix)
                store = parser_store.ParserStore('crlf', 1)
                self.advance(store)
                store.save(force=True)
                restart = parser_store.ParserStore(store.name, 1)
                for append in appends:
                    with self.path.open('ab') as stream:
                        stream.write(append)
                    record = self.advance(restart)
                self.assert_same_record(record, expected)

    def test_load_and_prune_do_not_open_a_transcript_replaced_with_a_fifo(self):
        self.path.write_bytes(lines(claude('assistant', 'Done.')))
        store = parser_store.ParserStore('fifo', 1)
        self.advance(store)
        store.save()
        fifo = self.path.with_suffix('.fifo')
        os.mkfifo(fifo)
        fifo.replace(self.path)

        def load(restart):
            return set(restart.records)

        def prune(restart):
            with patch.object(restart, '_load', wraps=restart._load) as load_store:
                restart.prune(set())
            return load_store.call_count

        checkpoint_key = store.records['%1'][0]
        info = self.path.stat()
        fifo_key = (checkpoint_key[0], str(self.path), info.st_dev, info.st_ino)
        for action, expected in ((load, {'%1'}), (prune, 0),
                                 (lambda _: parser_store._digest(checkpoint_key, 0), None),
                                 (lambda _: parser_store._digest(fifo_key, 0), None)):
            with self.subTest(action=action):
                restart = parser_store.ParserStore(store.name, 1)
                results = []
                thread = threading.Thread(target=lambda: results.append(action(restart)), daemon=True)
                thread.start()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive(), 'The checkpoint store blocked on the FIFO')
                self.assertEqual(results, [expected])

        restart = parser_store.ParserStore(store.name, 1)
        restart.prune(set())
        restart.save()
        self.assertEqual(json.loads(store.path.read_text())['records'], {})

    def test_digest_failure_stays_dirty_until_a_forced_save(self):
        self.path.write_bytes(lines(claude('assistant', 'Done.')))
        store = parser_store.ParserStore('retry', 1)
        with patch.object(parser_store, '_digest', return_value=None):
            self.advance(store)
            store.save()
        self.assertFalse(store.path.exists())
        store.save(force=True)
        saved = json.loads(store.path.read_text())['records']['%1']
        self.assertEqual(saved['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(saved['record']['state']['text'], 'Done.')

    def test_digest_retry_does_not_bind_old_state_to_a_rewrite(self):
        self.path.write_bytes(lines(claude('assistant', 'Old reply')))
        store = parser_store.ParserStore('retry', 1)
        with patch.object(parser_store, '_digest', return_value=None):
            self.advance(store)
        self.path.write_bytes(lines(claude('assistant', 'New reply'), CLAUDE_END))
        store.save(force=True)
        self.assertFalse(store.path.exists())
        self.assertEqual(self.advance(store)['state']['text'], 'New reply')
        store.save(force=True)
        self.assertEqual(self.advance(parser_store.ParserStore(store.name, 1))['state']['text'], 'New reply')

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
