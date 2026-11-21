"""Replay native transcripts and mock terminal writes to test prompt provenance."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

import agents
import antigravity
import auto_resume
import continuations
import coordinator
import delegate
import history
import injected_prompts
import notification_priority
import review_links
import server
import shell_log
import tab_brief
import test_coordinator
import test_delegate

FIXTURES = Path(__file__).with_name('fixtures') / 'injected_prompts'
CHAT_ID = str(UUID(int=101))


class InjectedPromptTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.folder / 'injected'
        self.enterContext(patch.object(injected_prompts.paths, 'CACHE', self.folder))
        self.enterContext(patch.object(injected_prompts, 'ROOT', self.root))
        self.clock = self.enterContext(patch.object(injected_prompts.time, 'time_ns'))
        self.now = 1_800_000_000
        self.sequence = 0
        self.chat = self.entry('claude')
        self.addCleanup(history.forget)

    @property
    def now(self):
        return self.clock.return_value // 1_000_000_000

    @now.setter
    def now(self, seconds):
        self.clock.return_value = seconds * 1_000_000_000

    def entry(self, agent, path=None):
        return {'id': '%1', 'thread': CHAT_ID, 'writable': True, 'tmux_session': '$1', 'agent': agent,
                'transcript': path or self.folder / f'{agent}.jsonl'}

    def stamp(self, delta=0):
        return datetime.fromtimestamp(self.now + delta, timezone.utc).isoformat(timespec='milliseconds')

    def append(self, entry, text=None, *, record=None, timestamp=True, assistant=False):
        self.sequence += 1
        if record is None:
            role = 'assistant' if assistant else 'user'
            if entry['agent'] == 'claude':
                record = {'type': role, 'message': {'role': role, 'content': text}}
            else:
                record = {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                          'content': [{'type': 'output_text' if assistant else 'input_text', 'text': text}]}}
        if entry['agent'] == 'claude':
            record = dict(record, uuid=str(UUID(int=self.sequence)))
        if timestamp:
            record = dict(record, timestamp=self.stamp())
        path = entry['transcript']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        return record

    def raw(self, entry):
        if entry['agent'] == 'claude':
            return server.claude_snapshot(entry, server.display_text)['messages']
        return history.read(entry['transcript'], server.codex_state, server.codex_consume, with_offset=True)['messages']

    def messages(self, entry=None):
        entry = entry or self.chat
        return injected_prompts.mark(entry, self.raw(entry))

    def send(self, entry, text, kind='review', tabs=None):
        with injected_prompts.track_send(entry, text, kind, tabs=tabs) as delivery:
            self.append(entry, delivery)

    def sources(self, entry=None):
        return [m.get('source') for m in self.messages(entry)]

    def test_paste_before_send_at_same_timestamp(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.append(entry, 'Identical prompt')
                self.send(entry, 'Identical prompt')
                self.assertEqual(self.sources(entry), [None, 'tether'])

    def test_two_sends_with_identical_paste_in_between(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.send(entry, 'Repeated prompt', 'rules')
                self.append(entry, 'Repeated prompt')
                self.send(entry, 'Repeated prompt', 'status', tabs=[])
                self.assertEqual([m.get('prompt') for m in self.messages(entry)], ['rules', None, 'status'])
                first, _, last = self.raw(entry)
                self.assertNotEqual(first['text'], last['text'])

    def test_review_replay_marks_the_second_identical_record(self):
        fixture = json.loads((FIXTURES / 'transcript_replay.json').read_text())
        with patch.object(coordinator, 'cli', return_value=fixture['status']['cli']):
            status = coordinator.message([fixture['status']['event']], fixture['status']['mode'])
        for agent, native in fixture['records'].items():
            row = fixture['review'][agent]
            review = (f"[Tether review] Feedback from the reviewer tab ({row['pane']}):\n\n"
                      f"{row['reply']}\n\nFix what it raises, then stop and wait.")
            for kind, text in [('status', status), ('review', review)]:
                with self.subTest(agent=agent, kind=kind):
                    entry = self.entry(agent, self.folder / f'{agent}-{kind}.jsonl')
                    self.append(entry, record=native[kind])
                    self.now += 1
                    with injected_prompts.track_send(entry, text, kind, tabs=[]) as delivery:
                        record = deepcopy(native[kind])
                        if agent == 'claude':
                            record['message']['content'] = record['message']['content'].replace(text, delivery)
                        else:
                            record['payload']['content'][0]['text'] = delivery
                        self.append(entry, record=record)
                    first, second = self.messages(entry)
                    self.assertEqual(first['text'].strip(), text)
                    self.assertEqual(second['text'].strip(), text)
                    self.assertNotIn('source', first)
                    self.assertEqual((second['source'], second['prompt']), ('tether', kind))

    def test_working_claude_binds_delayed_queued_attachment(self):
        self.append(self.chat, 'Start the task')
        relay = 'Review feedback while working'
        send = Mock(return_value='queued')
        with patch.object(server, 'stash_quiet_draft', return_value=None), patch.object(server, 'send_text', send):
            delivered = review_links._send_message(
                self.chat, relay, 'Working', lambda *args: server.review_send(*args, receipt=True), lambda _: True)
        self.assertTrue(delivered)
        delivery = send.call_args.args[1]
        self.assertRegex(delivery, r'^Review feedback while working\n\n\[Tether id [0-9a-f]{16}\]$')
        send.assert_called_once_with(dict(self.chat, _review=True), delivery, 'Working', held=True)
        self.assertEqual(self.sources(), [None])
        self.now += 40
        self.append(self.chat, record={'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': delivery}})
        self.append(self.chat, relay)
        self.assertEqual(self.sources(), [None, 'tether', None])
        self.assertEqual(self.messages()[1]['delivery'], 'queued')

    def assert_delivery_keeps_its_identity(self, agent):
        entry = self.entry(agent)
        relay = 'Same review text'
        self.append(entry, 'Start the task')
        send = Mock(return_value='queued')
        with patch.object(server, 'stash_quiet_draft', return_value=None), patch.object(server, 'send_text', send):
            self.assertEqual(server.review_send(entry, relay, 'Working', receipt=True), 'queued')
        self.append(entry, relay)
        delivery = send.call_args.args[1]
        if agent == 'claude':
            self.append(entry, record={'type': 'attachment', 'attachment': {
                'type': 'queued_command', 'prompt': delivery}})
        else:
            self.append(entry, delivery)
        self.assertEqual(self.sources(entry), [None, None, 'tether'])
        self.assertEqual([m['text'] for m in self.messages(entry)[-2:]], [relay, relay])

    def test_claude_person_paste_lands_before_queued_delivery(self):
        self.assert_delivery_keeps_its_identity('claude')

    def test_codex_person_paste_lands_before_queued_delivery(self):
        self.assert_delivery_keeps_its_identity('codex')

    def test_chat_read_after_ten_minutes_keeps_receipt_for_late_claude_attachment(self):
        self.append(self.chat, 'Start the task')
        with injected_prompts.track_send(self.chat, 'Late review', 'review') as delivery:
            pass
        self.now += 601
        self.assertEqual(self.sources(), [None])
        self.append(self.chat, record={'type': 'attachment', 'attachment': {
            'type': 'queued_command', 'prompt': delivery}})
        self.assertEqual(self.sources(), [None, 'tether'])

    def test_receipt_requires_both_the_id_and_the_text(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, 'Review text', 'review') as delivery:
                    pass
                edited = delivery.replace('Review text', 'Different text')
                self.append(entry, edited)
                self.append(entry, delivery)
                self.append(entry, delivery)
                self.assertEqual(self.sources(entry), [None, 'tether', None])
                self.assertEqual([m['text'] for m in self.messages(entry)], [edited, 'Review text', delivery])

    def test_receipt_accepts_native_whitespace_changes_to_the_prompt(self):
        with injected_prompts.track_send(self.chat, 'First line  second line', 'review') as delivery:
            self.append(self.chat, delivery.replace('line  second', 'line\nsecond'))
        marked, = self.messages()
        self.assertEqual((marked['source'], marked['text']), ('tether', 'First line\nsecond line'))

    def test_failed_review_send_keeps_its_pending_receipt(self):
        def fail(entry, delivery, _state, *, held):
            self.assertTrue(held)
            pending, = json.loads(injected_prompts._file(entry['transcript']).read_text())['receipts']
            self.assertNotIn('id', pending)
            self.append(entry, delivery)
            raise RuntimeError('Delivery could not be confirmed.')

        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent), patch.object(server, 'stash_quiet_draft', return_value=None), \
                    patch.object(server, 'send_text', side_effect=fail), patch.object(server, 'clear_unsent'):
                entry = self.entry(agent)
                send = partial(server.review_send, receipt=True)
                self.assertFalse(review_links._send_message(entry, 'Review feedback', 'Idle', send, lambda _: True))
                self.append(entry, 'Review feedback')
                self.assertEqual(self.sources(entry), ['tether', None])

    def test_read_only_review_refuses_plain_and_receipt_sends_before_terminal_access(self):
        for agent in ('claude', 'codex'):
            for receipt in (False, True):
                with self.subTest(agent=agent, receipt=receipt), \
                        patch.object(server, 'terminal_submit') as terminal, \
                        patch.object(server, 'stash_quiet_draft') as stash, \
                        patch.object(server, 'clear_unsent') as clear:
                    entry = dict(self.entry(agent), writable=False)
                    with self.assertRaisesRegex(RuntimeError, '^Messaging is unavailable for this session. Use Termius.$'):
                        server.review_send(entry, 'Review text', 'Working', receipt=receipt)
                    terminal.assert_not_called()
                    stash.assert_not_called()
                    clear.assert_not_called()
        self.assertEqual(list(self.root.glob('*.json')), [])

    def test_pending_delivery_expires_after_seven_days(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, 'Missing delivery', 'review') as delivery:
                    pass
                self.append(entry, 'Unrelated prompt')
                self.now += 7 * 24 * 60 * 60 + 1
                self.assertEqual(self.sources(entry), [None])
                receipt = injected_prompts._file(entry['transcript'])
                self.assertEqual(json.loads(receipt.read_text())['receipts'], [])
                self.append(entry, delivery)
                self.assertEqual(self.sources(entry), [None, None])

    def test_first_chat_read_eight_days_after_delivery_marks_and_strips_id(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.send(entry, 'Delivered promptly')
                self.now += 8 * 24 * 60 * 60
                marked, = self.messages(entry)
                self.assertEqual((marked.get('source'), marked['text']), ('tether', 'Delivered promptly'))

    def test_old_delivery_binds_to_final_json_with_or_without_newline(self):
        summary = review_links.PARENT_SUMMARY + '\n\nCheck the rows.\n\n' + review_links.PARENT_SUMMARY_END
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.append(entry, 'An earlier prompt')
                self.send(entry, summary)
                path = entry['transcript']
                path.write_bytes(path.read_bytes().removesuffix(b'\n'))
                self.now += 8 * 24 * 60 * 60
                raw = self.raw(entry)[-1]
                record = server.notification_records(entry, with_offset=True)[-1]
                self.assertEqual(review_links.prompt_source(entry, record, raw['text']), 'review-summary')
                self.assertEqual(set(injected_prompts.bindings(entry)), {raw['_record_id']})
                self.assertEqual(self.sources(entry), [None, 'tether'])
                with path.open('ab') as transcript:
                    transcript.write(b'\n')
                self.append(entry, 'Reviewed.', assistant=True)
                self.assertEqual(server.review_turn_source(entry)[0], 'review-summary')
                self.assertEqual(self.sources(entry), [None, 'tether', None])

    def test_old_pending_delivery_survives_partial_json_until_it_is_readable(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.append(entry, 'An earlier prompt')
                self.send(entry, 'Late delivery')
                path = entry['transcript']
                complete = path.read_bytes()
                path.write_bytes(complete[:-8])
                self.now += 8 * 24 * 60 * 60
                self.assertEqual(self.sources(entry), [None])
                receipt = injected_prompts._file(path)
                pending, = json.loads(receipt.read_text())['receipts']
                self.assertNotIn('id', pending)
                with path.open('ab') as transcript:
                    transcript.write(complete[-8:])
                self.assertEqual(self.sources(entry), [None, 'tether'])

    def test_replacement_binds_pending_delivery_before_its_old_anchor(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.append(entry, 'An earlier prompt')
                with injected_prompts.track_send(entry, 'Delayed delivery', 'review') as delivery:
                    pass
                path = entry['transcript']
                path.rename(path.with_suffix('.old'))
                self.append(entry, delivery)
                self.assertEqual(self.sources(entry), ['tether'])
                receipt = injected_prompts._file(path)
                bound, = json.loads(receipt.read_text())['receipts']
                self.assertIn('id', bound)

    def test_initialize_keeps_young_pending_and_bound_receipts_for_missing_transcripts(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                path = entry['transcript']
                self.send(entry, 'Earlier delivery')
                self.assertEqual(self.sources(entry), ['tether'])
                with injected_prompts.track_send(entry, 'Pending delivery', 'review'):
                    pass
                receipt = injected_prompts._file(path)
                before = receipt.read_bytes()
                path.unlink()
                injected_prompts.initialize()
                self.assertTrue(receipt.exists())
                self.assertEqual(json.loads(receipt.read_text())['receipts'], json.loads(before)['receipts'])
                before = receipt.read_bytes()
                self.now += 8 * 24 * 60 * 60
                with self.assertRaises(injected_prompts.ReceiptsUnreadable):
                    injected_prompts.bindings(entry)
                self.assertEqual(receipt.read_bytes(), before)
                injected_prompts.initialize()
                self.assertFalse(receipt.exists())

    def test_old_receipts_survive_a_brief_transcript_outage_at_startup(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                path = entry['transcript']
                self.send(entry, 'Bound delivery')
                with injected_prompts.track_send(entry, 'Pending delivery', 'review') as pending:
                    pass
                self.append(entry, pending)  # Delivered, not yet bound: the outage comes first.
                receipt = injected_prompts._file(path)
                self.now += 8 * 24 * 60 * 60
                aside = path.with_name('aside.jsonl')
                for _ in range(2):  # Two outages more than the age cap apart, each fixed before the next read.
                    path.rename(aside)
                    injected_prompts.initialize()
                    self.assertTrue(receipt.exists())
                    aside.rename(path)
                    self.assertEqual(self.sources(entry), ['tether', 'tether'])
                    self.assertNotIn('missing_since', json.loads(receipt.read_text()))
                    self.now += 8 * 24 * 60 * 60

    def test_bound_delivery_survives_a_copied_transcript(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, 'Bound delivery', 'review') as delivery:
                    pass
                self.append(entry, delivery)
                pending = injected_prompts._file(entry['transcript'])
                with injected_prompts.track_send(entry, 'Never delivered', 'review'):
                    pass  # a pending receipt makes the next read scan and merge
                bound = set(injected_prompts.bindings(entry))
                self.assertEqual(len(bound), 1)
                path = entry['transcript']
                copy = path.with_suffix('.copy')
                copy.write_bytes(path.read_bytes())
                copy.replace(path)  # a copy or move gives the same records a new inode
                self.assertEqual(set(injected_prompts.bindings(entry)), bound)
                self.assertTrue(pending.exists())

    def test_pending_binding_reads_only_new_transcript_bytes(self):
        path = self.chat['transcript']
        with injected_prompts.track_send(self.chat, 'Delayed delivery', 'review') as delivery:
            pass
        for _ in range(24):
            self.append(self.chat, 'x' * 100_000, assistant=True)
        path_open = Path.open
        reads = []
        opens = []

        def open_path(file, *args, **kwargs):
            stream = path_open(file, *args, **kwargs)
            if file != path:
                return stream
            opens.append(file)
            wrapped = Mock(wraps=stream)
            wrapped.__enter__ = Mock(return_value=wrapped)
            wrapped.__exit__ = Mock(side_effect=stream.__exit__)

            def read(method, size):
                data = method(size)
                reads.append((len(data), injected_prompts._lock.locked()))
                return data

            wrapped.read.side_effect = lambda size=-1: read(stream.read, size)
            wrapped.readline.side_effect = lambda size=-1: read(stream.readline, size)
            return wrapped

        with patch.object(Path, 'open', open_path):
            self.assertEqual(injected_prompts.bindings(self.chat), {})
            first_reads = reads.copy()
            reads.clear()
            self.assertEqual(injected_prompts.bindings(self.chat), {})
            self.assertEqual(sum(size for size, _ in reads), 0)
            self.assertGreater(sum(size for size, _ in first_reads), 2 * 1024 * 1024)
            self.assertFalse(any(locked for _, locked in first_reads))
            info = path.stat()
            receipt = injected_prompts._file(path)
            self.assertEqual(json.loads(receipt.read_text())['scanned'],
                             {'file': [info.st_dev, info.st_ino], 'offset': info.st_size})
            self.append(self.chat, delivery)
            reads.clear()
            self.assertEqual(len(injected_prompts.bindings(self.chat)), 1)
            self.assertLess(sum(size for size, _ in reads), 1024)
            opens.clear()
            self.assertEqual(len(injected_prompts.bindings(self.chat)), 1)
            self.assertEqual(opens, [])

    def test_new_send_binds_old_delivery_before_pruning_undelivered_receipts(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, 'Never delivered', 'review'):
                    pass
                self.send(entry, 'Old delivery')
                self.now += 8 * 24 * 60 * 60
                self.send(entry, 'New delivery')
                receipt = injected_prompts._file(entry['transcript'])
                saved = json.loads(receipt.read_text())['receipts']
                self.assertEqual(len(saved), 2)
                old, new = saved
                self.assertIn('id', old)
                self.assertEqual(new['at'], self.now * 1000)
                self.assertEqual([m['text'] for m in self.messages(entry)], ['Old delivery', 'New delivery'])
                self.assertEqual(self.sources(entry), ['tether', 'tether'])

    def test_initialize_binds_eight_day_old_delivery_before_pruning(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, 'Never delivered', 'review'):
                    pass
                self.send(entry, 'Delivered before restart')
                self.now += 8 * 24 * 60 * 60
                receipt = injected_prompts._file(entry['transcript'])
                self.assertTrue(all('id' not in r for r in json.loads(receipt.read_text())['receipts']))
                injected_prompts.initialize()
                self.assertTrue(receipt.exists())
                bound, = json.loads(receipt.read_text())['receipts']
                self.assertIn('id', bound)
                marked, = self.messages(entry)
                self.assertEqual((marked.get('source'), marked['text']), ('tether', 'Delivered before restart'))

    def test_unreadable_transcript_defers_age_pruning(self):
        self.send(self.chat, 'Old delivery')
        self.now += 8 * 24 * 60 * 60
        raw = self.raw(self.chat)
        path_open = Path.open

        def open_path(file, *args, **kwargs):
            if file == self.chat['transcript'] and args == ('rb',):
                raise PermissionError('transcript unavailable')
            return path_open(file, *args, **kwargs)

        with patch.object(Path, 'open', open_path):
            with self.assertRaises(injected_prompts.ReceiptsUnreadable):
                injected_prompts.bindings(self.chat)
            self.assertIsNone(injected_prompts.mark(self.chat, raw)[0].get('source'))
            self.send(self.chat, 'Another delivery')
            receipt = injected_prompts._file(self.chat['transcript'])
            before = receipt.read_bytes()
            injected_prompts.initialize()
            self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.sources(), ['tether', 'tether'])

    def test_unreadable_receipts_allow_chat_and_previews_to_recover(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.send(entry, 'Relay started this chat')
                self.append(entry, 'Actual user request')
                path = entry['transcript']
                if agent == 'codex':
                    with sqlite3.connect(self.folder / 'state_1.sqlite') as db:
                        db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, first_user_message TEXT)')
                        db.execute('INSERT INTO threads VALUES (?, ?, ?)', (CHAT_ID, str(path), 'Relay started this chat'))
                receipt = injected_prompts._file(path)
                saved = receipt.read_bytes()
                receipt.write_text('{')
                with patch.object(history.paths, 'CODEX', self.folder):
                    def preview():
                        return (history.preview(path, path.stat()) if agent == 'claude'
                                else history.find_chat(CHAT_ID, 'codex'))

                    self.assertEqual(self.sources(entry), [None, None])
                    self.assertTrue(preview()['text'].startswith('Relay started this chat'))
                    self.assertNotIn(path, history._previews)
                    receipt.write_bytes(saved)
                    self.assertEqual(self.sources(entry), ['tether', None])
                    self.assertEqual(preview()['text'], 'Actual user request')

    def test_binding_does_not_use_the_native_timestamp(self):
        with injected_prompts.track_send(self.chat, 'Clock skew', 'review') as delivery:
            pass
        self.now -= 1
        self.append(self.chat, delivery)
        self.assertEqual(self.sources(), ['tether'])

    def test_delivered_record_still_binds_when_reader_arrives_after_ten_minutes(self):
        self.send(self.chat, 'Delivered promptly')
        self.now += 601
        self.append(self.chat, 'Delivered promptly')
        self.assertEqual(self.sources(), ['tether', None])

    def test_pane_move_keeps_binding_and_other_transcripts_do_not_share_it(self):
        self.send(self.chat, 'Follow the transcript')
        self.assertEqual(self.sources(), ['tether'])
        moved = dict(self.chat, id='%9', tmux_session='$2')
        self.assertEqual(self.sources(moved), ['tether'])
        other = self.entry('claude', self.folder / 'other.jsonl')
        self.append(other, 'Follow the transcript')
        self.assertEqual(self.sources(other), [None])
        for flag in ['ambiguous', 'pending_conversation']:
            self.assertEqual(self.sources(dict(moved, **{flag: True})), [None])

    def test_fresh_module_uses_saved_ids_without_hashing(self):
        for agent in ('claude', 'codex'):
            entry = self.entry(agent)
            self.send(entry, 'Survives restart')
            self.append(entry, 'Survives restart')
            self.assertEqual(self.sources(entry), ['tether', None])
        importlib.reload(injected_prompts)
        history.forget()
        with patch.object(injected_prompts, '_hash', side_effect=AssertionError('Already bound')):
            for agent in ('claude', 'codex'):
                self.assertEqual(self.sources(self.entry(agent)), ['tether', None])

    def test_bound_legacy_receipts_survive_but_unbound_legacy_receipts_cannot_claim_a_paste(self):
        old = self.append(self.chat, 'Previously marked')
        before = injected_prompts.anchor(self.chat)
        # A receipt from the old format has no delivery ID.
        file = injected_prompts._file(self.chat['transcript'])
        injected_prompts._write(file, {'transcript': str(self.chat['transcript']), 'receipts': [
            dict(before, id=old['uuid'], metadata={'prompt': 'review'}),
            dict(before, hash=injected_prompts._hash('Same text'), metadata={'prompt': 'review'}),
        ]})
        with injected_prompts.track_send(self.chat, 'Current send', 'review') as delivery:
            self.append(self.chat, 'Same text')
            self.append(self.chat, delivery)
        self.assertEqual(self.sources(), ['tether', None, 'tether'])

    def test_live_bindings_survive_age_and_count_and_prune_deleted_transcripts(self):
        for index in range(502):
            self.send(self.chat, f'Status {index}', 'status', tabs=[])
        self.assertEqual(self.sources(), ['tether'] * 502)
        deleted = self.entry('claude', self.folder / 'deleted.jsonl')
        self.send(deleted, 'Will be removed')
        self.assertEqual(self.sources(deleted), ['tether'])
        deleted['transcript'].unlink()
        dead_receipt = injected_prompts._file(deleted['transcript'])
        live_receipt = injected_prompts._file(self.chat['transcript'])
        os.utime(live_receipt, (0, 0))
        self.now += 40 * 24 * 60 * 60
        injected_prompts.initialize()
        self.assertTrue(dead_receipt.exists())  # The absence clock starts at the first startup that sees it.
        self.now += 8 * 24 * 60 * 60
        injected_prompts.initialize()
        self.assertFalse(dead_receipt.exists())
        self.assertTrue(live_receipt.exists())
        self.assertEqual(self.sources(), ['tether'] * 502)

    def test_antigravity_step_rewrites_are_unmarked(self):
        entry = self.entry('antigravity')
        with injected_prompts.track_send(entry, 'Relay text', 'review') as delivery:
            self.assertEqual(delivery, 'Relay text')
            self.append(entry, record={'type': 'USER_INPUT', 'step_index': 2, 'content': delivery})
        self.append(entry, record={'type': 'USER_INPUT', 'step_index': 1, 'content': 'Earlier text'})
        self.append(entry, record={'type': 'USER_INPUT', 'step_index': 2, 'content': 'Relay text rewritten'})
        data = antigravity.snapshot(entry, server.display_text, server.records, server.usage)
        messages = injected_prompts.mark(entry, data['messages'])
        self.assertEqual([(m['id'], m['text']) for m in messages], [('1', 'Earlier text'), ('2', 'Relay text rewritten')])
        self.assertTrue(all('source' not in m for m in messages))

    def test_cutover_is_strict_and_survives_initialize_and_reload(self):
        self.append(self.chat, 'Before the cutover')
        self.append(self.chat, 'No time', timestamp=False)
        self.append(self.chat, record={'type': 'user', 'message': {'role': 'user', 'content': 'Invalid time'},
                                      'timestamp': 'bad time'}, timestamp=False)
        self.append(self.chat, 'Earlier assistant', assistant=True)
        self.assertTrue(all('legacy' not in m for m in self.messages()))
        self.now += 1
        injected_prompts.initialize()
        self.append(self.chat, 'At the cutover')
        self.now += 1
        self.append(self.chat, 'After the cutover')
        importlib.reload(injected_prompts)
        injected_prompts.initialize()
        self.assertEqual([m.get('legacy') for m in self.messages()], [True, None, None, None, None, None])
        self.assertEqual(json.loads((self.folder / 'injected-since.json').read_text())['since_ms'], (self.now - 1) * 1000)

    def test_folder_note_and_saved_continuation(self):
        marker = 'a' * 32
        prefix = shell_log._FOLDER_NOTE % '/srv/project'
        text = '[Tether status] A line\nwith a row'
        link = {'id': marker, 'brief': 'context', 'sent': False}
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent), patch.object(continuations, 'ROOT', self.folder / 'continuations'), \
                    patch.object(continuations, '_read', return_value=link):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, text, 'status', tabs=[]) as delivery:
                    wrapped, found = continuations.prepare(entry, prefix + delivery)
                    self.assertEqual(found, marker)
                    continuations.stage(entry, wrapped, found)
                    self.assertEqual(continuations.unwrap(wrapped), prefix + delivery)
                    self.append(entry, wrapped)
                self.append(entry, prefix + text)
                marked, pasted = self.messages(entry)
                self.assertEqual((marked['text'], marked['source'], marked['tabs']), (text, 'tether', []))
                self.assertEqual(pasted['text'], prefix + text)
                self.assertNotIn('source', pasted)

    def test_review_source_recognises_tagged_feedback_summary_notice_and_correction(self):
        injected_prompts.initialize()
        samples = json.loads((FIXTURES / 'other.json').read_text())
        feedback = '[Tether review] Feedback from the reviewer tab (%2):\n\nCheck the rows.\n\nFix what it raises, then stop and wait.'
        correction = (f"{review_links.CORRECTION_HEAD}{'1' * 40} only. This tab is now at {'2' * 40}. "
                      f'{review_links.CORRECTION_ACTION} {review_links.CORRECTION_WAIT}')
        old_options = samples['review_notice'].split('\n\n', 1)[1]
        for source, text in [
            ('review-feedback', feedback), ('review-summary', samples['review_summary']),
            ('review-notice', samples['review_notice']), ('review-correction', correction),
        ]:
            for agent in ('claude', 'codex'):
                with self.subTest(source=source, agent=agent), patch.object(review_links, '_parent_options', return_value=old_options):
                    entry = self.entry(agent, self.folder / f'{agent}-{source}.jsonl')
                    prefix = shell_log._FOLDER_NOTE % '/srv/project'
                    with injected_prompts.track_send(entry, text, 'review') as delivery:
                        if agent == 'claude':
                            raw = f'<pasted_content id="349c">\n{prefix}{delivery}\n</pasted_content id="349c">'
                            self.append(entry, record={'type': 'attachment', 'attachment': {
                                'type': 'queued_command', 'prompt': raw}})
                        else:
                            self.append(entry, record={'type': 'response_item', 'payload': {
                                'type': 'message', 'role': 'user',
                                'content': [{'type': 'input_text', 'text': '# AGENTS.md instructions'},
                                            {'type': 'input_text', 'text': prefix + delivery}],
                                'internal_chat_message_metadata_passthrough': {
                                    'content_item_kinds': ['agents_md.instructions', 'text'],
                                    'content_item_metadata': [{'provenance': {'type': 'agents_md'}},
                                                              {'provenance': {'type': 'user'}}],
                                },
                            }})
                    self.append(entry, 'Reviewed.', assistant=True)
                    self.assertEqual(server.review_turn_source(entry)[0], source)
                    self.assertEqual(notification_priority.classify('finished', agent,
                                     server.notification_records(entry), 'Reviewed.'),
                                     ('important', 'Reviewed.'))

    def test_unbound_delivery_id_survives_native_prompt_shapes(self):
        injected_prompts.initialize()
        feedback = ('[Tether review] Feedback from the reviewer tab (%2):\n\nCheck the rows.\n\n'
                    'Fix what it raises, then stop and wait.')
        delivery = feedback + '\n\n[Tether id 0123456789abcdef]'
        link = {'id': 'a' * 32, 'brief': 'Earlier context', 'sent': False}
        for shape in ('claude-text', 'claude-blocks', 'claude-queued', 'codex-text', 'codex-metadata', 'codex-event'):
            with self.subTest(shape=shape), patch.object(continuations, 'ROOT', self.folder / 'continuations'), \
                    patch.object(continuations, '_read', return_value=link):
                agent, kind = shape.split('-')
                entry = self.entry(agent, self.folder / f'{shape}.jsonl')
                prompt = shell_log._FOLDER_NOTE % '/srv/project' + delivery
                wrapped, marker = continuations.prepare(entry, prompt)
                continuations.stage(entry, wrapped, marker)
                if agent == 'claude':
                    raw = f'<pasted_content id="349c">\n{wrapped}\n</pasted_content id="349c">'
                    if kind == 'queued':
                        self.append(entry, record={'type': 'attachment', 'attachment': {
                            'type': 'queued_command', 'prompt': raw}})
                    elif kind == 'blocks':
                        self.append(entry, record={'type': 'user', 'message': {'role': 'user',
                                    'content': [{'type': 'text', 'text': raw}]}})
                    else:
                        self.append(entry, raw)
                elif kind == 'event':
                    self.append(entry, record={'type': 'event_msg', 'payload': {'type': 'user_message',
                                'message': wrapped}})
                elif kind == 'metadata':
                    self.append(entry, record={'type': 'response_item', 'payload': {
                        'type': 'message', 'role': 'user',
                        'content': [{'type': 'input_text', 'text': wrapped},
                                    {'type': 'input_text', 'text': '<environment_context />'}],
                        'internal_chat_message_metadata_passthrough': {
                            'content_item_kinds': ['text', 'environments.environment_context'],
                            'content_item_metadata': [{'provenance': {'type': 'user'}},
                                                      {'provenance': {'type': 'harness'}}],
                        },
                    }})
                else:
                    self.append(entry, wrapped)
                self.append(entry, 'Reviewed.', assistant=True)
                self.assertEqual(server.review_turn_source(entry)[0], 'unknown-receipt')
                identify = Mock(wraps=partial(review_links.prompt_source, entry))
                self.assertEqual(notification_priority.turn_source(
                    agent, server.notification_records(entry, with_offset=True), identify=identify), 'background')
                self.assertTrue(identify.call_args.args[1].endswith('[Tether id 0123456789abcdef]'))
                self.assertEqual(injected_prompts.bindings(entry), {})
                if kind != 'event':
                    message = self.messages(entry)[0]
                    self.assertNotIn('source', message)
                    self.assertTrue(message['text'].endswith('[Tether id 0123456789abcdef]'))

    def test_only_a_valid_trailing_delivery_id_leaves_unbound_status_source_unknown(self):
        injected_prompts.initialize()
        for agent in ('claude', 'codex'):
            for suffix, source in (('\n\n[Tether id 0123456789abcdef]', 'unknown-receipt'),
                                   ('\n\n[Tether id 0123456789abcdeg]', 'user'),
                                   ('\n\n[Tether id 0123456789abcdef]\nMore text', 'user'),
                                   ('', 'user')):
                with self.subTest(agent=agent, suffix=suffix):
                    entry = self.entry(agent)
                    self.append(entry, '[Tether status] Update from another tab' + suffix)
                    self.append(entry, 'Read.', assistant=True)
                    self.assertEqual(server.review_turn_source(entry)[0], source)

    def test_review_source_requires_a_binding_for_summary_and_feedback_pastes(self):
        summary = json.loads((FIXTURES / 'other.json').read_text())['review_summary']
        feedback = ('[Tether review] Feedback from the reviewer tab (%2):\n\nCheck the rows.\n\n'
                    'Fix what it raises, then stop and wait.')
        injected_prompts.initialize()
        for agent in ('claude', 'codex'):
            for label, text in [('summary', summary), ('feedback', feedback)]:
                with self.subTest(agent=agent, label=label):
                    entry = self.entry(agent, self.folder / f'{agent}-paste-{label}.jsonl')
                    self.append(entry, text)
                    self.append(entry, 'Read.', assistant=True)
                    self.assertEqual(server.review_turn_source(entry)[0], 'user')
                    self.assertEqual(self.sources(entry), [None, None])

    def test_review_source_uses_legacy_rule_only_before_cutover(self):
        summary = json.loads((FIXTURES / 'other.json').read_text())['review_summary']
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.append(entry, summary)
                self.append(entry, 'Read.', assistant=True)
                self.now += 1
                injected_prompts.initialize()
                self.assertEqual(server.review_turn_source(entry)[0], 'review-summary')
                self.append(entry, summary, timestamp=False)
                self.append(entry, 'Read again.', assistant=True)
                self.assertEqual(server.review_turn_source(entry)[0], 'user')
                (self.folder / 'injected-since.json').unlink()

    def test_review_source_keeps_the_text_rule_where_tether_keeps_no_receipts(self):
        # track_send records nothing for Antigravity or an unsettled conversation, so a relay there has no receipt.
        summary = json.loads((FIXTURES / 'other.json').read_text())['review_summary']
        injected_prompts.initialize()
        late = {'timestamp': '2099-01-01T00:00:00.000Z', 'created_at': '2099-01-01T00:00:00.000Z', 'uuid': 'u1'}
        cases = [{'agent': 'antigravity', 'transcript': str(self.folder / 'steps.json')},
                 dict(self.entry('claude'), pending_conversation=True), dict(self.entry('codex'), ambiguous=True)]
        for entry in cases:
            with self.subTest(entry=entry.get('agent')):
                self.assertEqual(review_links.prompt_source(entry, late, summary), 'review-summary')
        # Where Tether keeps receipts, the same unbound text after the cutover is the person's.
        self.assertIsNone(review_links.prompt_source(self.entry('claude'), late, summary))

    def test_review_source_finds_codex_binding_at_nonzero_offset_in_both_tails(self):
        summary = json.loads((FIXTURES / 'other.json').read_text())['review_summary']
        injected_prompts.initialize()
        entry = self.entry('codex')
        self.append(entry, 'An earlier task é ' + 'x' * server.NOTIFICATION_TAIL_BYTES)
        self.send(entry, summary)
        self.append(entry, 'Short reply.', assistant=True)
        self.assertEqual(server.review_turn_source(entry)[0], 'review-summary')
        self.append(entry, 'x' * server.NOTIFICATION_TAIL_BYTES, assistant=True)
        self.append(entry, 'Final reply.', assistant=True)
        self.assertEqual(server.review_turn_source(entry)[0], 'review-summary')

    def test_review_source_keeps_bound_status_and_rules_out_of_user_turns(self):
        samples = json.loads((FIXTURES / 'other.json').read_text())
        injected_prompts.initialize()
        for agent in ('claude', 'codex'):
            entry = self.entry(agent)
            for kind, text in [('status', '[Tether status] An update'), ('rules', samples['coordinator_rules'])]:
                with self.subTest(agent=agent, kind=kind):
                    self.send(entry, text, kind, tabs=[])
                    self.append(entry, 'Read.', assistant=True)
                    self.assertEqual(server.review_turn_source(entry)[0], 'tether')
                    self.append(entry, text)
                    self.append(entry, 'Read again.', assistant=True)
                    self.assertEqual(server.review_turn_source(entry)[0], 'user')

    def test_tagged_review_request_still_starts_a_claude_turn_and_gets_its_reply(self):
        request_id = '0a1b2c3d'
        text = (f'[Tether review] The Codex tab “Worker” (%2) finished a turn. {review_links.INTRO} '
                f'Review id: {request_id}. Its last reply:\n\nDone.\n\nTranscript: /srv/project/chat.jsonl')
        with injected_prompts.track_send(self.chat, text, 'review') as delivery:
            record = self.append(self.chat, delivery)
        self.assertTrue(agents.person_prompt(record))
        self.assertTrue(auto_resume.new_turn(agent='claude', transcript=self.chat['transcript'], offset=0))
        self.assertEqual(server.review_reply(self.chat, request_id), review_links.REPLY_PENDING)
        self.append(self.chat, record={'type': 'assistant', 'message': {
            'role': 'assistant', 'content': [{'type': 'text', 'text': 'APPROVED'}], 'stop_reason': 'end_turn'}})
        self.append(self.chat, record={'type': 'system', 'subtype': 'turn_duration'})
        self.assertEqual(server.review_reply(self.chat, request_id), 'APPROVED')

    def test_tagged_codex_rollout_keeps_the_native_turn_start(self):
        entry = self.entry('codex')
        with injected_prompts.track_send(entry, '[Tether status] An update', 'status', tabs=[]) as delivery:
            self.append(entry, record={'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'sample'}})
            self.append(entry, delivery)
        self.assertTrue(auto_resume.new_turn(agent='codex', transcript=entry['transcript'], offset=0))

    def test_coordinator_reads_the_result_of_its_tagged_queued_status(self):
        status = '[Tether status] Update from another tab'
        with injected_prompts.track_send(self.chat, status, 'status', tabs=[]) as delivery:
            queued = self.append(self.chat, record={'type': 'attachment', 'promptId': 'sample', 'attachment': {
                'type': 'queued_command', 'prompt': delivery}})
        self.assertTrue(agents.person_prompt(queued))
        self.append(self.chat, 'Summarised the update.', assistant=True)
        self.append(self.chat, record={'type': 'system', 'subtype': 'turn_duration'})
        outcome = coordinator.Feed().latest_outcome(self.chat)
        self.assertEqual(outcome, {'kind': 'done', 'text': 'Summarised the update.', 'identity': queued['uuid'], 'open': False})

    def test_status_and_rules_record_even_when_send_fails(self):
        world = test_coordinator.World()
        target, done, waiting, _ = world.entries
        target['thread'] = 'coordinator'
        done['thread'] = 'done'
        waiting['thread'] = 'waiting'
        unmarked_target = target.copy()
        with patch.object(injected_prompts, 'anchor', wraps=injected_prompts.anchor) as anchor, \
                patch.object(injected_prompts, '_record') as record:
            world.fail = RuntimeError('Draft exists.')
            with self.assertRaises(RuntimeError):
                world.change_role('%1', 'coordinator')
            anchor.assert_called_once_with(unmarked_target)
            record.assert_called_once()
            world.fail = None
            world.change_role('%1', 'coordinator')
            path, text, kind = record.call_args.args
            self.assertEqual((path, kind), (Path(unmarked_target['transcript']).absolute(), 'rules'))
            self.assertEqual(text, injected_prompts.strip_delivery_id(world.sent[-1][1]))
            self.assertIn('before', record.call_args.kwargs)

            waiting_status = {'state': 'Input needed', 'attention': {'id': 'question'}}
            world.states['%3'] = waiting_status
            world.outcomes['%2'] = {'kind': 'done', 'text': 'Done.', 'identity': '2'}

            def event(entry, **detail):
                return dict(detail, pane=entry['id'], name=entry['pane_name'],
                            session=entry['tmux_session'], chat=entry['transcript'])

            events = [event(waiting, kind='needs', text='A question.', token=coordinator.prompt_identity(waiting_status)),
                      event(done, **world.outcomes['%2'])]
            queue = dict(session=target['tmux_session'], chat=target['transcript'])
            record.reset_mock()
            world.fail = RuntimeError('Draft exists.')
            with self.assertRaises(RuntimeError):
                world.deliver('%1', queue, events)
            record.assert_called_once()
            world.fail = None
            world.deliver('%1', queue, events)
            path, text, kind = record.call_args.args
            self.assertEqual((path, kind), (Path(target['transcript']).absolute(), 'status'))
            self.assertEqual(text, injected_prompts.strip_delivery_id(world.sent[-1][1]))
            self.assertEqual(record.call_args.kwargs['tabs'], [
                {'pane': '%3', 'session': '$0', 'thread': 'waiting'},
                {'pane': '%2', 'session': '$0', 'thread': 'done'},
            ])

    def test_chat_window_and_search_keep_paste_unmarked_even_when_binding_is_first_seen_clipped(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                self.send(entry, 'Searchable relay')
                self.append(entry, 'Searchable relay')
                clipped = {'messages': self.raw(entry)[-1:], 'dropped': 1}
                with patch.object(server, 'snapshot', return_value=clipped), \
                        patch.object(server, 'live_status', return_value={'state': 'Idle'}):
                    result = server.chat_payload(entry)
                self.assertEqual(result['earlier'], 1)
                self.assertNotIn('source', result['messages'][0])
                with patch.object(server, 'snapshot', return_value={'messages': self.raw(entry)}):
                    matches = server.search_payload(entry, 'searchable relay')['matches']
                self.assertEqual([(m['from_end'], m.get('source')) for m in matches], [(1, None), (2, 'tether')])

    def test_claude_past_chat_title_uses_next_real_user_beyond_preview_head(self):
        entry = self.entry('claude', self.folder / 'projects' / 'sample' / (CHAT_ID + '.jsonl'))
        self.send(entry, 'Relay started this chat')
        self.append(entry, record={'type': 'ai-title', 'aiTitle': 'Relay started this chat'})
        self.append(entry, 'x' * history.PREVIEW_BYTES, assistant=True)
        self.append(entry, 'Real user asks for a report')
        self.append(entry, 'y' * history.PREVIEW_BYTES, assistant=True)
        with patch.object(history.paths, 'CLAUDE', self.folder):
            indexed = history.find_chat(CHAT_ID)
            self.assertEqual((indexed['title'], indexed['text']), ('Real user asks for a report',) * 2)
            self.append(entry, record={'type': 'custom-title', 'customTitle': 'My report'})
            self.assertEqual(history.find_chat(CHAT_ID)['title'], 'My report')

    def test_codex_past_chat_title_and_preview_use_first_unbound_rollout_record(self):
        entry = self.entry('codex')
        self.send(entry, 'Relay started this chat')
        self.append(entry, 'Actual user request')
        with sqlite3.connect(self.folder / 'state_1.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, name TEXT, title TEXT, first_user_message TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?, NULL, ?, ?)',
                       (CHAT_ID, str(entry['transcript']), 'Relay started this chat', 'Relay started this chat'))
            db.commit()
            with patch.object(history.paths, 'CODEX', self.folder):
                indexed = history.find_chat(CHAT_ID, 'codex')
                self.assertEqual((indexed['title'], indexed['text']), ('Actual user request',) * 2)
                with patch.object(server, 'sessions', return_value=[{'id': '%2', 'agent': 'shell'}]), \
                        patch.object(server, 'new_window_after', return_value='%3'), \
                        patch.object(server.codex_provider, 'launch_profile', return_value=None), \
                        patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='$1\n')) as run:
                    status, resumed = test_delegate.EndpointTests.request(
                        self, {'id': CHAT_ID, 'agent': 'codex', 'after': '%2'}, path='/mobile/api/resume')
                    self.assertEqual((status, resumed['name']), (201, 'Actual user request'))
                    self.assertEqual(run.call_args_list[0].args[0][5:7], ['@chat_name', 'Actual user request'])
                db.execute("UPDATE threads SET name='My report'")
                db.commit()
                self.assertEqual(history.find_chat(CHAT_ID, 'codex')['title'], 'My report')

    def test_briefs_skip_bound_prompts_and_include_legacy_and_pasted_prompts(self):
        self.append(self.chat, 'Earlier request from the person')
        self.append(self.chat, 'Earlier answer', assistant=True)
        self.now += 1
        injected_prompts.initialize()
        self.send(self.chat, 'Relay text')
        self.append(self.chat, 'Relay-only answer', assistant=True)
        self.append(self.chat, 'Relay text')  # identical real user paste
        self.append(self.chat, 'Answer to paste', assistant=True)
        messages = server.brief_messages(self.chat)
        self.assertTrue(messages[0]['legacy'])
        self.assertEqual(messages[2].get('source'), 'tether')
        pairs, count = tab_brief.turns(messages)
        self.assertEqual(pairs, [('Earlier request from the person', 'Earlier answer\n\nRelay-only answer'), ('Relay text', 'Answer to paste')])
        self.assertEqual(count, 2)
        self.assertEqual(tab_brief.decide(messages), 'go')
        model = tab_brief.model_input(pairs)
        self.assertEqual(model.count('Person: Relay text'), 1)
        for brief in (continuations.brief(self.chat, messages), delegate.brief(messages)):
            self.assertEqual(brief.count('Relay text'), 1)
            self.assertIn('Earlier request from the person', brief)

    def test_continue_and_delegate_routes_mark_the_transcript_before_building_context(self):
        self.append(self.chat, 'User request stays')
        self.send(self.chat, 'Tether relay to omit')
        self.append(self.chat, 'User pasted this relay prefix')
        raw = {'messages': self.raw(self.chat), 'busy': False}
        body = {'session': self.chat['id'], 'thread': self.chat['thread']}
        with patch.object(server, 'sessions', return_value=[self.chat]), \
                patch.object(server, 'snapshot', return_value=raw), \
                patch.object(server, 'live_status', return_value={'state': 'Idle'}), \
                patch.object(server, 'agent_folder', return_value='/srv/project'), \
                patch.object(server.live_state, 'for_thread', return_value={}), \
                patch.object(server.codex_provider, 'readiness', return_value=(True, None)):
            status, preview = test_delegate.EndpointTests.request(
                self, dict(body, target='codex-plan'), path='/mobile/api/continue-preview')
            self.assertEqual(status, 200)
            self.assertIn('User request stays', preview['brief'])
            self.assertIn('User pasted this relay prefix', preview['brief'])
            self.assertNotIn('Tether relay to omit', preview['brief'])

            with patch.object(delegate, 'start', return_value=('%3', 'Sample task')) as start:
                status, _ = test_delegate.EndpointTests.request(
                    self, dict(body, agent='claude', message='Sample task', context=True))
            self.assertEqual(status, 201)
            brief = delegate.brief(start.call_args.kwargs['recent']())
            self.assertIn('User request stays', brief)
            self.assertIn('User pasted this relay prefix', brief)
            self.assertNotIn('Tether relay to omit', brief)

    def test_continued_chat_keeps_saved_receipts_and_dates_older_saved_messages(self):
        self.append(self.chat, 'Archived user request')
        self.now += 1
        injected_prompts.initialize()
        self.send(self.chat, 'Archived Tether prompt')
        target = {'id': '%2', 'pane_pid': 20, 'pane_start': '10'}
        with patch.object(continuations, 'ROOT', self.folder / 'continuations'):
            archived, relay = self.messages()
            archived.pop('legacy')  # a handoff saved before the cutover feature
            link = continuations.save_link(target, self.chat, 'codex-plan', [archived, relay], '', False)
            with patch.object(continuations, '_read', return_value=link):
                frozen, marked, _ = continuations.decorate(target, {'messages': []})['messages']
        self.assertTrue(frozen['legacy'])
        self.assertNotIn('source', frozen)
        self.assertEqual((marked['source'], marked['prompt']), ('tether', 'review'))
