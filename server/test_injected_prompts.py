"""Replay native transcripts and mock terminal writes to test prompt provenance."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import datetime, timezone
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

import antigravity
import continuations
import coordinator
import delegate
import history
import injected_prompts
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
        with injected_prompts.track_send(entry, text, kind, tabs=tabs):
            self.append(entry, text)

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
                    with injected_prompts.track_send(entry, text, kind, tabs=[]):
                        self.append(entry, record=native[kind])
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
        send.assert_called_once_with(dict(self.chat, _review=True), relay, 'Working', held=True)
        self.assertEqual(self.sources(), [None])
        self.now += 40
        self.append(self.chat, record={'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': relay}})
        self.append(self.chat, relay)
        self.assertEqual(self.sources(), [None, 'tether', None])
        self.assertEqual(self.messages()[1]['delivery'], 'queued')

    def test_failed_review_send_leaves_no_receipt(self):
        with patch.object(server, 'stash_quiet_draft', return_value=None), \
                patch.object(server, 'send_text', side_effect=RuntimeError('Draft exists.')), \
                patch.object(server, 'clear_unsent'):
            self.assertFalse(review_links._send_message(
                self.chat, 'Review feedback', 'Idle', lambda *args: server.review_send(*args, receipt=True), lambda _: True))
        self.append(self.chat, 'Review feedback')
        self.assertEqual(self.sources(), [None])
        self.assertEqual(list(self.root.glob('*.json')), [])

    def test_no_delivery_expires_before_a_later_paste(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                entry = self.entry(agent)
                with injected_prompts.track_send(entry, 'Missing delivery', 'review'):
                    pass
                self.now += injected_prompts.BIND_WINDOW + 1
                self.append(entry, 'Missing delivery')
                self.assertEqual(self.sources(entry), [None])
                receipt = injected_prompts._file(entry['transcript'])
                self.assertEqual(json.loads(receipt.read_text())['receipts'], [])

    def test_delivered_record_still_binds_when_reader_arrives_after_window(self):
        self.send(self.chat, 'Delivered promptly')
        self.now += injected_prompts.BIND_WINDOW + 1
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
        self.assertFalse(dead_receipt.exists())
        self.assertTrue(live_receipt.exists())
        self.assertEqual(self.sources(), ['tether'] * 502)

    def test_antigravity_step_rewrites_are_unmarked(self):
        entry = self.entry('antigravity')
        with injected_prompts.track_send(entry, 'Relay text', 'review'):
            self.append(entry, record={'type': 'USER_INPUT', 'step_index': 2, 'content': 'Relay text'})
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
        wrapped = f'<tether_handoff_context id="{marker}">\n"context"\n</tether_handoff_context>\n\n{prefix}{text}'
        with patch.object(continuations, 'ROOT', self.folder / 'continuations'), \
                patch.object(continuations, '_read', return_value={'id': marker}):
            continuations.stage(self.chat, wrapped, marker)
            with injected_prompts.track_send(self.chat, text.replace('\n', '  '), 'status', tabs=[]):
                self.append(self.chat, wrapped)
            self.append(self.chat, prefix + text)
            marked, pasted = self.messages()
        self.assertEqual((marked['text'], marked['source'], marked['tabs']), (text, 'tether', []))
        self.assertEqual(pasted['text'], prefix + text)
        self.assertNotIn('source', pasted)

    def test_status_and_rules_only_record_successful_sends(self):
        world = test_coordinator.World()
        target, done, waiting, _ = world.entries
        target['thread'] = 'coordinator'
        done['thread'] = 'done'
        waiting['thread'] = 'waiting'
        unmarked_target = target.copy()
        with patch.object(injected_prompts, 'anchor', wraps=injected_prompts.anchor) as anchor, \
                patch.object(injected_prompts, 'record') as record:
            world.fail = RuntimeError('Draft exists.')
            with self.assertRaises(RuntimeError):
                world.change_role('%1', 'coordinator')
            anchor.assert_called_once_with(unmarked_target)
            record.assert_not_called()
            world.fail = None
            world.change_role('%1', 'coordinator')
            self.assertEqual(record.call_args.args, (unmarked_target, world.sent[-1][1], 'rules'))
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
            record.assert_not_called()
            world.fail = None
            world.deliver('%1', queue, events)
            self.assertEqual(record.call_args.args, (target, world.sent[-1][1], 'status'))
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
