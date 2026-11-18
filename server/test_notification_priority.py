"""Fixture-only tests: classification, delivery, persistence and API guards."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import push
import review_links
import server
from notification_priority import classify, turn_source


def claude(role, content, **fields):
    return dict(type=role, message={'role': role, 'content': content}, **fields)


def codex(role, text, **fields):
    return {'type': 'response_item', 'payload': dict(type='message', role=role,
            content=[{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}], **fields)}


BACKGROUND = claude('user', '<task-notification><event>[Monitor expired after 30m]</event></task-notification>')
ASSISTANT = claude('assistant', 'Done.')


class ClassifierTests(unittest.TestCase):
    def test_rules_table(self):
        for agent, records in [
            ('claude', [claude('user', 'Fix it'), ASSISTANT]),
            ('codex', [codex('user', 'Fix it'), codex('assistant', 'Done', phase='final_answer')]),
            ('antigravity', [{'type': 'USER_INPUT', 'step_index': 1, 'content': '<USER_REQUEST>Fix it</USER_REQUEST>'},
                             {'type': 'AGENT_RESPONSE', 'step_index': 2, 'content': 'Done'}]),
        ]:
            for kind, reply, expected, preview in [
                ('done', 'Done.', 'important', 'Done.'),
                ('approval', 'No action needed.', 'urgent', 'No action needed.'),
                ('input', 'Needs you: no', 'urgent', ''),
                ('done', 'Done.\nNeeds you: yes', 'urgent', 'Done.'),
                ('done', 'Done?\n**Needs you: no**.\n\n', 'important', 'Done?'),
                ('done', 'Done.\n**Needs you:** `YES`.', 'urgent', 'Done.'),
                ('done', 'Done.\n`Needs you: no`', 'important', 'Done.'),
                ('done', 'Done.\n**Needs you: yes.', 'urgent', 'Done.'),
                ('done', 'Which file?', 'important', 'Which file?'),
                ('done', 'Done. Nothing needs you.', 'low', 'Done. Nothing needs you.'),
                ('done', 'Done.\nNo action needed.', 'low', 'Done.\nNo action needed.'),
                ('done', 'No change.', 'low', 'No change.'),
                ('done', 'No change. Please review.', 'important', 'No change. Please review.'),
                ('done', 'No change.\nNeeds you: no', 'important', 'No change.'),
                ('done', 'Needs you: yes\nDone.', 'important', 'Needs you: yes\nDone.'),
            ]:
                with self.subTest(agent=agent, kind=kind, reply=reply):
                    self.assertEqual(classify(kind, agent, records, reply), (expected, preview))

    def test_background_rules(self):
        for reply, expected in [('Done.', 'low'), ('Which file?', 'important'),
                                ('Which file?\nNeeds you: no', 'low'),
                                ('Nothing needs you.\nNeeds you: yes', 'urgent')]:
            with self.subTest(reply=reply):
                self.assertEqual(classify('done', 'claude', [BACKGROUND, ASSISTANT], reply)[0], expected)

    def test_turn_starter_shapes(self):
        cases = [
            ('typed', claude('user', 'Run tests'), 'user'),
            ('text blocks', claude('user', [{'type': 'text', 'text': 'Run tests'}]), 'user'),
            ('monitor', BACKGROUND, 'background'),
            ('task', claude('user', '<task-notification><summary>Task complete</summary></task-notification>'), 'background'),
            ('meta cron', claude('user', 'Scheduled cron wakeup', isMeta=True), 'background'),
            ('meta loop', claude('user', 'Loop check', isMeta=True), 'background'),
            ('injected hook', claude('user', '<hook-output>Try again</hook-output>'), 'background'),
            ('injected system', claude('user', '<system-reminder>Wake up</system-reminder>'), 'background'),
            ('schedule tag', claude('user', '<scheduled-task>Check status</scheduled-task>'), 'background'),
            ('cron tag', claude('user', '<cron-trigger>Check status</cron-trigger>'), 'background'),
            ('loop tag', claude('user', '<loop-prompt>Check status</loop-prompt>'), 'background'),
            ('schedule source', claude('user', 'Check status', source='scheduled'), 'background'),
            ('system hook', {'type': 'system', 'subtype': 'hook_response'}, 'background'),
            ('system schedule', {'type': 'system', 'subtype': 'scheduled_wakeup'}, 'background'),
            ('queued user', {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': 'Fix it'}}, 'user'),
            ('queued monitor', {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': BACKGROUND['message']['content']}}, 'background'),
            ('hook error', {'type': 'attachment', 'attachment': {'type': 'hook_blocking_error', 'blockingError': 'Check failed'}}, 'background'),
            ('peer', claude('user', '<agent-message from-name="worker">Report</agent-message>'), 'background'),
            ('typed shell', claude('user', '<bash-input>echo test</bash-input>'), 'user'),
        ]
        for label, starter, expected in cases:
            with self.subTest(label=label):
                records = [claude('user', 'Old prompt'), ASSISTANT, starter, ASSISTANT,
                           claude('user', [{'type': 'tool_result', 'content': 'No action needed'}]), ASSISTANT,
                           {'type': 'api-request-blob', 'message': {'role': 'user', 'content': 'Debug copy'}},
                           claude('user', 'Next typed prompt'),
                           claude('assistant', 'Side reply', isSidechain=True)]
                self.assertEqual(turn_source('claude', records), expected)
        # A later typed turn must reset the source.
        self.assertEqual(turn_source('claude', [BACKGROUND, ASSISTANT, claude('user', 'Fix it'), ASSISTANT]), 'user')
        # Enqueue alone is not delivery to the assistant.
        self.assertEqual(turn_source('claude', [claude('user', 'Fix it'), ASSISTANT,
                         {'type': 'queue-operation', 'operation': 'enqueue', 'content': BACKGROUND['message']['content']}, ASSISTANT]), 'user')

    def test_identified_prompt_survives_a_blocked_stop_hook(self):
        hook = {'type': 'attachment', 'attachment': {'type': 'hook_blocking_error', 'blockingError': 'Check failed'}}
        identify = lambda text: 'review-summary' if text == 'Summary prompt' else None
        for prompt, expected in (('Summary prompt', 'review-summary'), ('Fix it', 'user')):
            with self.subTest(prompt=prompt):
                records = [claude('user', prompt), ASSISTANT, hook, ASSISTANT]
                self.assertEqual(turn_source('claude', records, identify=identify), expected)
                self.assertEqual(turn_source('claude', records), 'background')  # notifier behavior is unchanged
        self.assertEqual(turn_source('claude', [BACKGROUND, ASSISTANT, hook, ASSISTANT], identify=identify), 'background')
        self.assertEqual(turn_source('claude', [hook, ASSISTANT], identify=identify), 'background')  # a clipped tail

    def test_other_agents_default_to_user(self):
        for agent, records in [('codex', [{'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Wake'}},
                                        codex('assistant', 'Reply'), {'type': 'event_msg', 'payload': {'type': 'task_complete'}}]),
                               ('codex', [codex('user', '# AGENTS.md instructions'), codex('assistant', 'Reply')]),
                               ('antigravity', [{'type': 'USER_INPUT', 'step_index': 1, 'content': 'Wake'},
                                                {'type': 'AGENT_RESPONSE', 'step_index': 3, 'content': 'Reply'},
                                                {'type': 'PLANNER_RESPONSE', 'step_index': 2, 'content': 'Old update'}]),
                               ('claude', []), ('unknown', [])]:
            with self.subTest(agent=agent):
                self.assertEqual(turn_source(agent, records), 'user')


class DeliveryTests(unittest.TestCase):
    def test_all_pane_and_global_combinations(self):
        for priority in ('urgent', 'important', 'low'):
            for level in push.PANE_LEVELS:
                for mode in ('sound', 'silent', 'off'):
                    with self.subTest(priority=priority, level=level, mode=mode):
                        send = Mock()
                        notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100,
                            context=lambda entry: ('Done.' + ('\nNeeds you: yes' if priority == 'urgent' else ''),
                                                   [BACKGROUND, ASSISTANT] if priority == 'low' else []),
                            levels=lambda: {'%7': level}, delivery=lambda: {priority: mode})
                        entry = {'id': '%7', 'agent': 'claude'}
                        notifier.observe(entry, {'state': 'Working'})
                        notifier.observe(entry, {'state': 'Idle'})
                        expected = mode != 'off' and (level == 'all' or
                                   level == 'important' and priority in ('urgent', 'important') or
                                   level == 'urgent' and priority == 'urgent')
                        self.assertEqual(send.called, expected)
                        if expected:
                            payload, urgent = send.call_args.args
                            self.assertEqual(payload['priority'], priority)
                            self.assertEqual(payload['silent'], mode == 'silent')
                            self.assertEqual(urgent, priority == 'urgent')
                            self.assertNotIn('Needs you', payload['body'])

    def test_defaults_drop_background_but_keep_approval(self):
        send = Mock()
        notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100,
                                context=lambda _: ('Nothing needs you.', [BACKGROUND, ASSISTANT]))
        entry = {'id': '%7', 'agent': 'claude'}
        notifier.observe(entry, {'state': 'Working'})
        notifier.observe(entry, {'state': 'Idle'})
        send.assert_not_called()
        notifier.observe(entry, {'state': 'Approval'})
        self.assertEqual(send.call_args.args[0]['priority'], 'urgent')

    def test_parent_stage_lowers_finished_priority_and_preserves_urgent_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'review-links.json'
            entry = {'id': '%7', 'agent': 'claude'}
            review = dict(worker='%7', reviewer='%8',
                          parent_stage=dict(pane='%9', thread=None, notice='Still broken.'))
            records = [claude('user', 'Summarize the findings.'), ASSISTANT]
            notifier = server.NOTIFIER
            send = Mock()
            with patch.object(review_links, 'FILE', store), patch.multiple(
                    notifier, send=send, watching=lambda _: False, settle=0, minimum_work=0,
                    levels=lambda: {}, delivery=lambda: {priority: 'silent' for priority in push.DEFAULT_DELIVERY}), \
                    patch.object(server, 'notification_context') as context:
                for link, state, reply, expected in (
                    (review, 'Idle', 'Still broken.\nNeeds you: no', 'low'),
                    (review, 'Idle', 'Still broken.\nNeeds you: yes', 'urgent'),
                    (review, 'Idle', 'Still broken.', 'low'),
                    (review, 'Approval', 'Needs you: no', 'urgent'),
                    (review, 'Input needed', 'Needs you: no', 'urgent'),
                    (dict(review, worker='%6'), 'Idle', 'Still broken.\nNeeds you: no', 'important'),
                    (dict(worker='%7', reviewer='%8', escalate='Still broken.'), 'Idle',
                     'Still broken.\nNeeds you: no', 'important'),
                ):
                    with self.subTest(link=link, state=state, reply=reply), patch.object(notifier, 'seen', {}):
                        store.write_text(json.dumps([link]))
                        context.return_value = reply, records
                        send.reset_mock()
                        notifier.observe(entry, {'state': 'Working'})
                        notifier.observe(entry, {'state': state})
                        send.assert_called_once()
                        payload, urgent = send.call_args.args
                        self.assertEqual(payload['priority'], expected)
                        self.assertEqual(urgent, expected == 'urgent')

    def test_bounded_context_uses_one_read_and_classifies_the_delivered_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder) / 'chat.jsonl'
            transcript.write_text('\n'.join(json.dumps(record) for record in [BACKGROUND, ASSISTANT]) + '\n')
            entry = {'id': '%7', 'agent': 'claude', 'transcript': str(transcript)}
            original = server.notification_records
            with patch.object(server, 'notification_records', wraps=original) as read:
                reply, records = server.notification_context(entry)
            read.assert_called_once_with(entry)
            self.assertEqual(classify('done', 'claude', records, reply), ('low', 'Done.'))

    def test_persistence_and_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'mutes.json'
            store.write_text(json.dumps({'%1': 'r1', '%2': {'run': 'r1', 'level': True}, '%3': 'old'}))
            self.assertEqual(push.pane_levels(store, 'r1'), {'%1': 'off', '%2': 'off'})
            push.set_level('%4', 'urgent', store, 'r1')
            self.assertEqual(push.muted(store, 'r1'), ['%1', '%2'])
            self.assertEqual(push.pane_levels(store, 'r2'), {})
            self.assertEqual(push.set_level('%5', 'important', store, 'r2'), {'%5': 'important'})
            push.set_muted('%5', True, store, 'r2')
            self.assertEqual(push.pane_levels(store, 'r2'), {'%5': 'off'})
            push.set_muted('%5', False, store, 'r2')
            self.assertEqual(push.pane_levels(store, 'r2'), {})
            settings = Path(folder) / 'settings.json'
            self.assertEqual(push.delivery_settings(settings), push.DEFAULT_DELIVERY)
            self.assertEqual(push.set_delivery({'low': 'silent'}, settings)['low'], 'silent')
            self.assertEqual(push.delivery_settings(settings)['urgent'], 'sound')
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            for invalid in ({'low': 'loud'}, {'unknown': 'off'}, [], {}, None):
                with self.assertRaises(ValueError):
                    push.set_delivery(invalid, settings)
            for invalid in ('muted', True, None, []):
                with self.assertRaises(ValueError):
                    push.set_level('%7', invalid, store, 'r2')


class RouteTests(unittest.TestCase):
    def request(self, route, body, headers=None):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/push/' + route
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'tether.test', 'Origin': 'https://tether.test',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        if headers:
            handler.headers.update(headers)
        handler.respond = Mock()
        with patch.object(handler, 'deny', return_value=False):
            handler.do_POST()
        return handler.respond.call_args.args

    def test_routes_and_write_guards(self):
        for route, body, method, result in [
            ('level', {'session': '%7', 'level': 'urgent'}, 'set_level', {'levels': {'%7': 'urgent'}}),
            ('settings', {'delivery': {'low': 'silent'}}, 'set_delivery', {'delivery': {'low': 'silent'}}),
            ('mute', {'session': '%7', 'muted': True}, 'set_muted', {'muted': ['%7']}),
        ]:
            with self.subTest(route=route), patch.object(push, method, return_value=next(iter(result.values()))) as write:
                self.assertEqual(self.request(route, body), (200, result))
                write.assert_called_once()
                write.reset_mock()
                self.assertEqual(self.request(route, body, {'X-Codex-Mobile': ''})[0], 403)
                self.assertEqual(self.request(route, body, {'Origin': 'https://other.test'})[0], 403)
                write.assert_not_called()
                write.side_effect = ValueError('Invalid setting.')
                self.assertEqual(self.request(route, body)[0], 400)

    def test_config_keeps_legacy_fields(self):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/push/config'
        handler.headers = {}
        handler.respond = Mock()
        with patch.object(handler, 'deny', return_value=False), patch.object(push, 'available', return_value=False), \
             patch.object(push, 'muted', return_value=['%1']), patch.object(push, 'pane_levels', return_value={'%1': 'off'}), \
             patch.object(push, 'delivery_settings', return_value=push.DEFAULT_DELIVERY):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[1], {'supported': False, 'publicKey': None,
                         'muted': ['%1'], 'levels': {'%1': 'off'}, 'delivery': push.DEFAULT_DELIVERY})
