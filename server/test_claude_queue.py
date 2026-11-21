"""Claude's pending inputs follow the transcript queue without becoming chat messages."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agents
import server


TIME = '2026-11-21T09:00:00Z'
NOTICE = '  <task-notification><summary>Done</summary></task-notification>'
PEER = '<cross-session-message from-name="Claude">Check the job.</cross-session-message>'
PEER_PREFIX = 'Another Claude session sent a message:\n'


def operation(name, **fields):
    return dict(type='queue-operation', operation=name, **fields)


def enqueue(text, **fields):
    return operation('enqueue', content=text, timestamp=TIME, **fields)


def user(content, **fields):
    return dict(type='user', message={'role': 'user', 'content': content}, **fields)


def attachment(text, **fields):
    return dict(type='attachment', attachment={'type': 'queued_command', 'prompt': text}, **fields)


class ClaudeQueueTests(unittest.TestCase):
    def setUp(self):
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
        state = {'queue': []}
        for record in records:
            agents.claude_queue(state, record)
        return [entry['text'] for entry in state['queue']]

    def test_enqueue_shows_display_text_and_time_without_adding_a_message(self):
        self.append(enqueue('<pasted_content id="1">Queued prompt</pasted_content>'),
                    enqueue('  '))
        data = self.snapshot()
        self.assertEqual(data['queued'], [{'text': 'Queued prompt', 'time': TIME}])
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

    def test_peer_entry_is_not_shown(self):
        self.append(enqueue(PEER))
        self.assertEqual(self.snapshot()['queued'], [])

    def test_sidechain_records_do_not_change_the_queue(self):
        self.assertEqual(self.fold(
            enqueue('A'), enqueue('B', isSidechain=True), operation('dequeue', isSidechain=True),
            operation('remove', content='A', isSidechain=True), operation('popAll', isSidechain=True),
            user('A', isSidechain=True), attachment('A', isSidechain=True),
        ), ['A'])

    def test_queue_keeps_the_latest_50_entries_in_order(self):
        self.assertEqual(self.fold(*(enqueue(str(index)) for index in range(52))),
                         [str(index) for index in range(2, 52)])

    def test_incremental_read_preserves_queue_without_sharing_output(self):
        self.append(enqueue(NOTICE), enqueue('A'), enqueue('B'))
        first = self.snapshot()['queued']
        self.assertEqual(first, [{'text': 'A', 'time': TIME}, {'text': 'B', 'time': TIME}])
        first[1]['text'] = 'changed by caller'
        added = [operation('dequeue'), enqueue('C')]
        self.append(*added)
        with patch.object(agents, 'claude_queue', wraps=agents.claude_queue) as consume:
            self.assertEqual(self.snapshot()['queued'], [{'text': 'B', 'time': TIME}, {'text': 'C', 'time': TIME}])
        self.assertEqual([call.args[1] for call in consume.call_args_list], added)

    def test_payload_only_exposes_queue_while_claude_is_working(self):
        self.append(enqueue('A'))
        with patch.object(server, 'live_status') as status, \
             patch.object(server, 'snapshot', side_effect=lambda entry: agents.claude_snapshot(entry, server.display_text)), \
             patch.object(server.shell_log, 'merge', side_effect=lambda _, messages: messages), \
             patch.object(server.reply_preview, 'sample', return_value=('', 80)):
            for live, busy, expected in (('Working', False, 'Working'), ('Idle', True, 'Working'),
                                         ('Idle', False, 'Idle'), ('Approval', False, 'Approval'),
                                         ('Input needed', False, 'Input needed'), ('Unknown', False, 'Unknown')):
                with self.subTest(live=live, busy=busy):
                    status.return_value = {'state': live, 'approval': None}
                    data = server.chat_payload(dict(self.entry, busy=busy))
                    self.assertEqual(data['state'], expected)
                    self.assertEqual(data['queued'], [{'text': 'A', 'time': TIME}] if expected in server.QUEUE_STATES else [])
                    self.assertEqual(data['messages'], [])

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
