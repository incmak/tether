"""Claude's pending inputs follow the transcript queue without becoming chat messages."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents
import server


TIME = '2026-11-21T09:00:00Z'
SAMPLED = datetime(2026, 11, 21, 9, 0, 1, tzinfo=timezone.utc).timestamp()
NOTICE = '  <task-notification><summary>Done</summary></task-notification>'
PEER = '<cross-session-message from-name="Claude">Check the job.</cross-session-message>'
PEER_PREFIX = 'Another Claude session sent a message:\n'


def operation(name, **fields):
    return dict(type='queue-operation', operation=name, **fields)


def enqueue(text, timestamp=TIME, **fields):
    return operation('enqueue', content=text, timestamp=timestamp, **fields)


def user(content, **fields):
    return dict(type='user', message={'role': 'user', 'content': content}, **fields)


def attachment(text, **fields):
    return dict(type='attachment', attachment={'type': 'queued_command', 'prompt': text}, **fields)


class ClaudeQueueTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(server, 'STALE_QUEUE', {}))
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'chat.jsonl'
        self.entry = {'id': '%fixture', 'agent': 'claude', 'thread': 'fixture', 'transcript': str(self.path)}

    def append(self, *records):
        with self.path.open('a') as stream:
            for record in records:
                stream.write(json.dumps(record) + '\n')

    def snapshot(self):
        return agents.claude_snapshot(self.entry, server.display_text)

    def fold(self, *records):
        state = {'queue': [], 'taken': []}
        for index, record in enumerate(records):
            agents.claude_queue(state, index, record)
        return [entry['text'] for entry in state['queue']]

    def payload_status(self):
        status = self.enterContext(patch.object(server, 'live_status'))
        self.enterContext(patch.object(server, 'snapshot', side_effect=lambda _: self.snapshot()))
        self.enterContext(patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages))
        self.enterContext(patch.object(server.reply_preview, 'sample', return_value=('', 80)))
        return status

    def test_enqueue_shows_display_text_and_time_without_adding_a_message(self):
        self.append(enqueue('<pasted_content id="1">Queued prompt</pasted_content>'),
                    enqueue('  '))
        data = self.snapshot()
        self.assertEqual(data['queued'], [{'id': 0, 'text': 'Queued prompt', 'time': TIME}])
        self.assertEqual(data['messages'], [])

    def test_dequeue_prefers_the_first_person_prompt(self):
        for texts, expected in ((('A', NOTICE), [NOTICE]), ((NOTICE, 'B'), [NOTICE]),
                                ((NOTICE, 'C', 'D'), [NOTICE, 'D'])):
            with self.subTest(texts=texts):
                records = [enqueue(text) for text in texts] + [operation('dequeue')]
                self.assertEqual(self.fold(*records), expected)
                self.append(operation('popAll'), *records)
                self.assertEqual([entry['text'] for entry in self.snapshot()['queued']], expected[1:])

    def test_dequeue_takes_oldest_notice_if_there_are_no_person_prompts(self):
        second_notice = NOTICE.replace('Done', 'Also done')
        self.assertEqual(self.fold(enqueue(NOTICE), enqueue(second_notice), operation('dequeue')), [second_notice])
        self.assertEqual(self.fold(enqueue(NOTICE), operation('dequeue'), operation('dequeue')), [])

    def test_remove_is_exact_and_takes_only_the_first_match(self):
        for extra in ({}, {'commandUuid': 'command-id'}):
            with self.subTest(extra=extra):
                remove = operation('remove', content='A', reason='absorbed_mid_turn', **extra)
                self.assertEqual(self.fold(enqueue('A'), remove), [])
                self.assertEqual(self.fold(enqueue('A'), enqueue('B'), enqueue('A'), remove), ['B', 'A'])
                self.assertEqual(self.fold(enqueue(' A'), enqueue('a'), remove), [' A', 'a'])

    def test_pop_all_ignores_content(self):
        for extra in ({}, {'content': 'A'}):
            with self.subTest(extra=extra):
                self.assertEqual(self.fold(enqueue('A'), enqueue('B'), operation('popAll', **extra)), [])

    def test_pop_all_clears_taken_before_a_later_delivery(self):
        self.assertEqual(self.fold(enqueue('A'), operation('dequeue'), operation('popAll'),
                                   enqueue('A'), attachment('A')), [])

    def test_taken_lasts_through_one_turn_end_of_the_same_chain(self):
        turn_end = dict(type='system', subtype='turn_duration')
        side_end = dict(turn_end, isSidechain=True)
        # A prompt dequeued in one turn can arrive just after that turn ends: it must not take a later copy.
        self.assertEqual(self.fold(enqueue('A'), operation('dequeue'), turn_end, enqueue('A'), user('A')), ['A'])
        # Two turn ends later, an undelivered taken text is gone, so the delivery repairs the queue.
        self.assertEqual(self.fold(enqueue('A'), operation('dequeue'), turn_end, turn_end,
                                   enqueue('A'), user('A')), [])
        # Sidechain turn ends do not count.
        self.assertEqual(self.fold(enqueue('A'), operation('dequeue'), turn_end, side_end, side_end,
                                   enqueue('A'), user('A')), ['A'])

    def test_enqueue_requires_string_content(self):
        self.append(operation('enqueue'), operation('enqueue', content=None), operation('enqueue', content=42))
        self.assertEqual(self.snapshot()['queued'], [])

    def test_delivered_user_and_attachment_records_repair_the_queue(self):
        parts = [{'type': 'text', 'text': 'A'}, {'type': 'image'}, {'type': 'text', 'text': 'B'}]
        for text, delivered in (('A', user('A')), ('B', attachment('B')), ('A\nB', user(parts))):
            with self.subTest(delivered=delivered):
                self.assertEqual(self.fold(enqueue(text), delivered), [])

    def test_repair_can_match_with_the_peer_prefix_on_either_side(self):
        self.assertEqual(self.fold(enqueue(PEER), user(PEER_PREFIX + PEER)), [])
        self.assertEqual(self.fold(enqueue(PEER_PREFIX + PEER), attachment(PEER)), [])

    def test_dequeue_and_delivery_take_only_one_copy(self):
        for delivered in (attachment('A'), user('A'), user([{'type': 'text', 'text': 'A'}])):
            with self.subTest(delivered=delivered):
                self.assertEqual(self.fold(enqueue('A'), enqueue('A'), operation('dequeue'), delivered), ['A'])
        self.assertEqual(self.fold(enqueue(PEER_PREFIX + PEER), enqueue(PEER), operation('dequeue'),
                                   attachment(PEER)), [PEER])

    def test_delivery_matches_taken_once_and_can_still_repair_the_queue(self):
        self.assertEqual(self.fold(enqueue('A'), enqueue('B'), operation('dequeue'),
                                   attachment('A'), user('B')), [])
        self.assertEqual(self.fold(enqueue('A'), operation('dequeue'), attachment('A'),
                                   enqueue('A'), user('A')), [])

    def test_remove_and_delivery_take_only_one_copy(self):
        self.assertEqual(self.fold(enqueue('A'), enqueue('A'), operation('remove', content='A'),
                                   attachment('A')), ['A'])

    def test_non_string_attachment_prompt_does_not_repair_the_queue(self):
        self.assertEqual(self.fold(enqueue('A'), attachment(['A'])), ['A'])

    def test_taken_keeps_only_the_latest_50_explicit_removals(self):
        texts = [str(index) for index in range(51)]
        records = [enqueue(text) for text in texts] + [operation('dequeue')] * len(texts)
        records.extend((enqueue('0'), enqueue('1'), attachment('0'), attachment('1')))
        self.assertEqual(self.fold(*records), ['1'])
        self.assertEqual(self.fold(enqueue('A'), operation('popAll'), enqueue('A'), attachment('A')), [])

    def test_peer_entry_is_not_shown(self):
        self.append(enqueue(PEER))
        self.assertEqual(self.snapshot()['queued'], [])

    def test_sidechain_records_do_not_change_the_queue(self):
        self.assertEqual(self.fold(
            enqueue('A'), enqueue('B', isSidechain=True), operation('dequeue', isSidechain=True),
            operation('remove', content='A', isSidechain=True), operation('popAll', isSidechain=True),
            user('A', isSidechain=True), attachment('A', isSidechain=True),
        ), ['A'])

    def test_snapshot_keeps_all_person_prompts_and_dequeue_uses_the_full_queue(self):
        records = [enqueue(str(index)) for index in range(1, 52)] + [enqueue(NOTICE), enqueue('  ')]
        self.append(*records)
        expected = [str(index) for index in range(1, 52)]
        self.assertEqual([entry['text'] for entry in self.snapshot()['queued']], expected)
        dequeue = operation('dequeue')
        self.assertEqual(self.fold(*records, dequeue), expected[1:] + [NOTICE, '  '])
        self.append(dequeue)
        self.assertEqual([entry['text'] for entry in self.snapshot()['queued']], expected[1:])

    def test_incremental_read_preserves_queue_without_sharing_output(self):
        self.append(enqueue(NOTICE), enqueue('A'), enqueue('B'))
        first = self.snapshot()['queued']
        self.assertEqual(first, [{'id': 1, 'text': 'A', 'time': TIME}, {'id': 2, 'text': 'B', 'time': TIME}])
        first[1]['text'] = 'changed by caller'
        added = [operation('dequeue'), enqueue('C')]
        self.append(*added)
        with patch.object(agents, 'claude_queue', wraps=agents.claude_queue) as consume:
            self.assertEqual(self.snapshot()['queued'], [
                {'id': 2, 'text': 'B', 'time': TIME}, {'id': 4, 'text': 'C', 'time': TIME},
            ])
        self.assertEqual([call.args[1:] for call in consume.call_args_list], list(enumerate(added, 3)))

    def test_payload_only_exposes_queue_while_claude_is_working(self):
        self.append(enqueue('A'))
        with patch.object(server, 'live_status') as status, \
             patch.object(server, 'snapshot', side_effect=lambda entry: agents.claude_snapshot(entry, server.display_text)), \
             patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages), \
             patch.object(server.reply_preview, 'sample', return_value=('', 80)):
            for live, busy, expected in (('Working', False, 'Working'), ('Idle', True, 'Working'),
                                         ('Approval', False, 'Approval'), ('Input needed', False, 'Input needed'),
                                         ('Idle', False, 'Idle'), ('Unknown', False, 'Unknown')):
                with self.subTest(live=live, busy=busy):
                    status.return_value = {'state': live, 'approval': None}
                    data = server.chat_payload(dict(self.entry, busy=busy))
                    self.assertEqual(data['state'], expected)
                    self.assertEqual(data['queued'], [{'id': 0, 'text': 'A', 'time': TIME}]
                                     if expected in server.QUEUE_STATES else [])
                    self.assertEqual(data['messages'], [])

    def test_payload_hides_stale_entries_on_the_next_turn(self):
        self.enterContext(patch.object(server.time, 'time', return_value=SAMPLED))
        self.append(enqueue('A'))
        first = self.snapshot()
        queued_a = {'id': 0, 'text': 'A', 'time': TIME}
        with patch.object(server, 'live_status') as status, \
             patch.object(server, 'snapshot', return_value=first) as snapshot, \
             patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages), \
             patch.object(server.reply_preview, 'sample', return_value=('', 80)):
            for live, expected in (('Working', [queued_a]), ('Idle', []), ('Working', [])):
                with self.subTest(live=live, queued=expected):
                    status.return_value = {'state': live, 'approval': None}
                    self.assertEqual(server.chat_payload(self.entry)['queued'], expected)
                    self.assertEqual(first['queued'], [queued_a])
            self.append(enqueue('B'))
            snapshot.return_value = self.snapshot()
            self.assertEqual(server.chat_payload(self.entry)['queued'], [{'id': 1, 'text': 'B', 'time': TIME}])
            self.assertEqual(len(snapshot.return_value['queued']), 2)

            self.append(operation('remove', content='A'))
            snapshot.return_value = self.snapshot()
            status.return_value = {'state': 'Idle', 'approval': None}
            self.assertEqual(server.chat_payload(self.entry)['queued'], [])
            self.assertEqual(server.STALE_QUEUE[self.entry['transcript']], {1})

    def test_idle_does_not_mark_an_enqueue_after_the_status_sample_stale(self):
        self.append(enqueue('Older'))
        status = self.payload_status()
        clock = self.enterContext(patch.object(server.time, 'time', return_value=SAMPLED))
        new_time = '2026-11-21T14:30:02+05:30'

        def enqueue_during_status(_):
            self.append(enqueue('Newer', timestamp=new_time))
            clock.return_value = SAMPLED + 2
            return {'state': 'Idle', 'approval': None}

        status.side_effect = enqueue_during_status
        self.assertEqual(server.chat_payload(self.entry)['queued'], [])
        status.side_effect = None
        status.return_value = {'state': 'Working', 'approval': None}
        self.assertEqual(server.chat_payload(self.entry)['queued'], [
            {'id': 1, 'text': 'Newer', 'time': new_time},
        ])

    def test_idle_preserves_entries_without_a_time_strictly_before_the_sample(self):
        self.enterContext(patch.object(server.time, 'time', return_value=SAMPLED))
        self.append(operation('enqueue', content='Missing'),
                    enqueue('Invalid', timestamp='invalid'),
                    enqueue('No offset', timestamp='2026-11-21T09:00:00'),
                    enqueue('Equal', timestamp='2026-11-21T09:00:01.000Z'))
        status = self.payload_status()
        status.return_value = {'state': 'Idle', 'approval': None}
        self.assertEqual(server.chat_payload(self.entry)['queued'], [])
        status.return_value = {'state': 'Working', 'approval': None}
        queued = server.chat_payload(self.entry)['queued']
        self.assertEqual([item['text'] for item in queued], ['Missing', 'Invalid', 'No offset', 'Equal'])

    def test_unknown_hides_the_queue_without_marking_entries_stale(self):
        self.append(enqueue('A'))
        status = self.payload_status()
        queued = [{'id': 0, 'text': 'A', 'time': TIME}]
        for live, expected in (('Working', queued), ('Unknown', []), ('Working', queued)):
            with self.subTest(live=live):
                status.return_value = {'state': live, 'approval': None}
                self.assertEqual(server.chat_payload(self.entry)['queued'], expected)
                self.assertNotIn(self.entry['transcript'], server.STALE_QUEUE)

    def test_idle_marks_even_entries_outside_the_display_cap_stale(self):
        self.enterContext(patch.object(server.time, 'time', return_value=SAMPLED))
        self.append(*(enqueue(str(index)) for index in range(51)))
        status = self.payload_status()
        status.return_value = {'state': 'Idle', 'approval': None}
        self.assertEqual(server.chat_payload(self.entry)['queued'], [])

        self.append(operation('remove', content='50'))
        status.return_value = {'state': 'Working', 'approval': None}
        self.assertEqual(server.chat_payload(self.entry)['queued'], [])
        self.append(enqueue('B'))
        self.assertEqual(server.chat_payload(self.entry)['queued'], [{'id': 52, 'text': 'B', 'time': TIME}])

    def test_payload_shows_the_newest_50_live_entries(self):
        self.append(*(enqueue(str(index)) for index in range(60)))
        status = self.payload_status()
        status.return_value = {'state': 'Working', 'approval': None}
        self.assertEqual(server.chat_payload(self.entry)['queued'], [
            {'id': index, 'text': str(index), 'time': TIME} for index in range(10, 60)
        ])

    def test_stale_cache_evicts_the_oldest_transcript_and_skips_missing_paths(self):
        self.enterContext(patch.object(server.time, 'time', return_value=SAMPLED))
        server.STALE_QUEUE.update({f'transcript-{index}': {0} for index in range(200)})
        with patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}), \
             patch.object(server, 'snapshot', return_value={'messages': [], 'queued': [{'id': 0, 'time': TIME}]}), \
             patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages):
            server.chat_payload(dict(self.entry, transcript=None))
            self.assertIn('transcript-0', server.STALE_QUEUE)
            self.assertNotIn(None, server.STALE_QUEUE)
            server.chat_payload(self.entry)
        self.assertEqual(len(server.STALE_QUEUE), 200)
        self.assertNotIn('transcript-0', server.STALE_QUEUE)
        self.assertIn('transcript-1', server.STALE_QUEUE)
        self.assertEqual(server.STALE_QUEUE[self.entry['transcript']], {0})

    def test_payload_leaves_other_agents_and_shell_mode_alone(self):
        with patch.object(server, 'live_status', return_value={'state': 'Idle', 'approval': None}), \
             patch.object(server, 'snapshot', side_effect=lambda _: {'messages': []}), \
             patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages), \
             patch.object(server, 'shell_snapshot', return_value={'messages': []}) as shell:
            for agent in ('codex', 'antigravity', 'shell'):
                with self.subTest(agent=agent):
                    payload = server.chat_payload(dict(self.entry, agent=agent), shell_mode=agent == 'shell')
                    self.assertNotIn('queued', payload)
            shell.assert_called_once()
            self.assertEqual(payload['state'], 'Shell')
