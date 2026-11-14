import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

import push
import server


class PushTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(server.WATCHING, clear=True))
        self.enterContext(patch.dict(server.WATCHED, clear=True))
        self.clock = self.enterContext(patch.object(server.time, 'monotonic', return_value=100))
        self.entry = {'id': '%7', 'label': 'Agent task'}

    def poll(self, watch):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/chat?session=%257' + ('&watch=1' if watch else '')
        handler.headers = {}
        handler.client_address = ('127.0.0.1', 1234)
        handler.respond = Mock()
        with patch.object(handler, 'deny', return_value=False), patch.object(server, 'choose', return_value=self.entry), patch.object(server, 'chat_payload', return_value={}):
            handler.do_GET()
        handler.respond.assert_called_once_with(200, server.with_version({}))

    def test_host_route_returns_short_hostname(self):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/host'
        handler.headers = {}
        handler.client_address = ('127.0.0.1', 1234)
        handler.respond = Mock()
        with patch.object(handler, 'deny', return_value=False), patch.object(server.socket, 'gethostname', return_value='box.example.net'), patch.object(server.host_stats, 'enabled', return_value=False), patch.object(server, 'TTYD_PORT', 0), patch.object(server.office_addon, 'enabled', return_value=False):
            handler.do_GET()
        handler.respond.assert_called_once_with(200, {'name': 'box', 'resources': False, 'terminal': False, 'office': False})

    def test_watch_poll_expires_and_regular_poll_does_not_refresh_it(self):
        self.poll(False)
        self.assertFalse(server.watching('%7'))
        self.poll(True)
        self.assertTrue(server.watching('%7'))
        self.assertFalse(server.watching('%8'))
        self.clock.return_value = 109.9
        self.assertTrue(server.watching('%7'))
        self.poll(False)
        self.clock.return_value = 110
        self.assertFalse(server.watching('%7'))
        self.poll(True)
        self.assertTrue(server.watching('%7'))

    def test_open_stream_stays_watched_after_poll_expires(self):
        self.poll(True)
        server.WATCHING['%7'] = 1
        self.clock.return_value = 200
        self.assertTrue(server.watching('%7'))
        server.WATCHING['%7'] = 0
        self.assertFalse(server.watching('%7'))

    def test_notification_payload_contains_session_and_legacy_url(self):
        for state, kind in [('Approval', 'approval'), ('Input needed', 'input'), ('Idle', 'done')]:
            with self.subTest(state=state):
                send = Mock()
                notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100)
                notifier.observe(self.entry, {'state': 'Working'})
                notifier.observe(self.entry, {'state': state})
                payload, urgent = send.call_args.args
                self.assertEqual(payload['session'], '%7')
                self.assertEqual(payload['url'], '/mobile/#session=' + quote('%7', safe=''))
                self.assertEqual(payload['tag'], 'session-%7')
                self.assertEqual(payload['body'], push.Notifier.MESSAGES[kind])
                self.assertEqual(urgent, kind != 'done')
                notifier.observe(self.entry, {'state': state})
                send.assert_called_once()

    def test_approval_push_offers_approve_and_deny_only_for_a_plain_yes_no_prompt(self):
        options = lambda *texts: [{'id': i + 1, 'text': t} for i, t in enumerate(texts)]
        command = {'id': 'abc', 'actionable': True, 'text': '╭──╮\n│ Bash command │\n│   git push -q example-user HEAD │\n│ Push the branch │',
                   'options': options('Yes', "Yes, and don't ask again for git push", 'No, and tell Claude what to do differently (esc)')}
        cases = [(command, True),
                 (dict(command, actionable=False), False),
                 (dict(command, id=None), False),
                 (dict(command, options=options('Yes, auto mode', 'Yes, manually approve edits', 'No, keep planning')), False),
                 (dict(command, options=options('Yes, I trust this folder', 'No, exit')), False),
                 (dict(command, options=options("Yes, and don't ask again", 'No')), False),
                 (dict(command, text='Bash command\n  rm -rf build/'), False),
                 (dict(command, text='Bash command\n  git push --force origin main'), False),
                 (dict(command, text='Bash command\n  git push -qf origin main'), False),
                 (dict(command, text='Bash command\n  gcloud run deploy api'), False),
                 (dict(command, text='Bash command\n  echo ' + 'x' * 200), False)]
        for approval, offered in cases:
            with self.subTest(options=approval['options'][0]['text'], actionable=approval['actionable'], id=approval['id']):
                send = Mock()
                notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100)
                notifier.observe(dict(self.entry, thread='t1'), {'state': 'Approval', 'approval': approval})
                payload, urgent = send.call_args.args
                self.assertTrue(urgent)
                if offered:
                    self.assertEqual((payload['approval'], payload['thread']), ('abc', 't1'))
                    self.assertEqual(payload['body'], 'Needs approval: Bash command git push -q example-user HEAD Push the branch')
                else:
                    self.assertNotIn('approval', payload)
                    self.assertTrue(payload['body'].startswith('Needs approval: Bash command'))

    def test_notifier_does_not_send_while_watched(self):
        self.poll(True)
        send = Mock()
        notifier = push.Notifier(send, server.watching, settle=0, clock=lambda: self.clock.return_value)
        notifier.observe(self.entry, {'state': 'Approval', 'approval': {'id': 'first'}})
        send.assert_not_called()
        self.clock.return_value = 111
        notifier.observe(self.entry, {'state': 'Input needed', 'attention': {'id': 'second'}})
        self.assertEqual(send.call_args.args[0]['session'], '%7')


class ReplyPreviewTests(unittest.TestCase):
    def notify(self, state, reply, **status):
        send = Mock()
        notifier = push.Notifier(send, settle=0, minimum_work=0, clock=lambda: 100, reply=reply)
        entry = {'id': '%7', 'label': 'Task'}
        notifier.observe(entry, {'state': 'Working'})
        notifier.observe(entry, dict(status, state=state))
        return send.call_args.args[0]['body']

    def test_snippet_cleans_markdown(self):
        text = '# Result\n- **Fixed** _the_ `file_name`.\n1. See [details](https://example.test).\n' \
               '> ~~Old~~\n```python\nprint("hidden")\n```\nNext\n~~~sh\necho hidden\n~~~'
        self.assertEqual(push.reply_summary(text), 'Result Fixed the file_name. See details. Old [code] Next [code]')
        self.assertEqual(push.reply_summary('## Title\n===\n* [Guide][docs]\n[docs]: https://example.test'), 'Title Guide')
        self.assertEqual(push.reply_summary('Before\n```sh\nunclosed code'), 'Before [code]')

    def test_snippet_collapses_whitespace_and_truncates(self):
        self.assertEqual(push.reply_summary(' a\n\t b   c '), 'a b c')
        self.assertEqual(push.reply_summary('x' * 160), 'x' * 160)
        self.assertEqual(push.reply_summary('x' * 161), 'x' * 159 + '…')
        self.assertEqual(push.reply_summary(None), '')
        table = 'Each window shows its status.\n\n| Window | Item | Now |\n|---|---|---|\n| uncategorized | t | done |'
        self.assertEqual(push.reply_summary(table), 'Each window shows its status.')
        self.assertEqual(push.reply_summary('| Window | Now |\n| :--- | ---: |\n| one | done |'), 'Window · Now one · done')

    def test_done_body_uses_reply(self):
        self.assertEqual(self.notify('Idle', Mock(return_value='**All done.**')), 'Finished: All done.')

    def test_input_prefers_question_then_attention_text_then_reply(self):
        reply = Mock(return_value='Which **file**?')
        self.assertEqual(self.notify('Input needed', reply, attention={'question': 'Which branch?', 'text': 'Menu'}),
                         'Needs your input: Which branch?')
        self.assertEqual(self.notify('Input needed', reply, attention={'text': 'Choose a folder.'}),
                         'Needs your input: Choose a folder.')
        reply.assert_not_called()
        self.assertEqual(self.notify('Input needed', reply), 'Needs your input: Which file?')

    def test_empty_reply_and_parser_errors_preserve_plain_bodies(self):
        for reply in (Mock(return_value=''), Mock(side_effect=RuntimeError('parser failed'))):
            self.assertEqual(self.notify('Idle', reply), 'Finished')
            self.assertEqual(self.notify('Input needed', reply), 'Needs your input')

    def transcript(self, agent, records):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(folder) / 'transcript.jsonl'
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        return {'agent': agent, 'transcript': str(path)}

    def test_claude_uses_last_assistant_text_not_tools_thinking_or_debug_copies(self):
        entry = self.transcript('claude', [
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': 'Earlier'}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'Private reasoning'}, {'type': 'text', 'text': 'Last reply'}]}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'tool', 'name': 'Bash', 'input': {'command': 'echo tool'}}]}},
            {'type': 'api-request-blob', 'message': {'role': 'assistant', 'content': 'Debug copy'}},
            {'type': 'user', 'message': {'role': 'user', 'content': 'Next request'}},
        ])
        self.assertEqual(server.notification_reply(entry), 'Last reply')

    def test_codex_uses_response_item_not_event_copy_reasoning_or_tool_output(self):
        def message(text, **extra):
            return {'type': 'response_item', 'payload': dict(type='message', role='assistant',
                    content=[{'type': 'output_text', 'text': text}], **extra)}
        entry = self.transcript('codex', [message('Commentary', phase='commentary'), message('Final reply', phase='final_answer'),
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': 'Reasoning'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'tool', 'output': 'Tool output'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Event copy'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
        ])
        self.assertEqual(server.notification_reply(entry), 'Final reply')

    def test_antigravity_uses_latest_step_and_last_version_of_step(self):
        entry = self.transcript('antigravity', [
            {'type': 'PLANNER_RESPONSE', 'step_index': 1, 'content': 'Plan'},
            {'type': 'AGENT_RESPONSE', 'step_index': 3, 'content': 'Partial'},
            {'type': 'AGENT_RESPONSE', 'step_index': 3, 'content': 'Final reply'},
            {'type': 'PLANNER_RESPONSE', 'step_index': 2, 'content': 'Older step updated'},
            {'type': 'USER_INPUT', 'step_index': 4, 'content': 'Question'},
            {'type': 'ERROR_MESSAGE', 'step_index': 5, 'content': 'Error'},
        ])
        self.assertEqual(server.notification_reply(entry), 'Final reply')
        planner = self.transcript('antigravity', [{'type': 'PLANNER_RESPONSE', 'content': 'Planner reply'}])
        self.assertEqual(server.notification_reply(planner), 'Planner reply')

    def test_transcript_missing_or_parser_throws_still_sends(self):
        entry = self.transcript('claude', [{'type': 'assistant', 'message': {'role': 'assistant', 'content': 'Reply'}}])
        with patch.object(server, 'claude_consume', side_effect=RuntimeError('bad parser')):
            self.assertEqual(self.notify('Idle', lambda _: server.notification_reply(entry)), 'Finished')
        Path(entry['transcript']).unlink()
        self.assertEqual(self.notify('Idle', lambda _: server.notification_reply(entry)), 'Finished')
        self.assertEqual(self.notify('Input needed', lambda _: server.notification_reply(entry)), 'Needs your input')

    def test_tail_is_bounded_and_skips_clipped_and_malformed_records(self):
        entry = self.transcript('antigravity', [{'type': 'AGENT_RESPONSE', 'content': 'Last reply'}])
        path = Path(entry['transcript'])
        path.write_bytes(b'x' * (server.NOTIFICATION_TAIL_BYTES * 2) + b'\nnull\ninvalid\n' + path.read_bytes() + b'{"partial":')
        real_open = open
        streams = []
        def tracked_open(*args):
            stream = self.enterContext(real_open(*args))
            wrapped = Mock(wraps=stream)
            wrapped.__enter__ = Mock(return_value=wrapped)
            wrapped.__exit__ = Mock(return_value=False)
            streams.append(wrapped)
            return wrapped
        with patch('server.open', side_effect=tracked_open):
            self.assertEqual(server.notification_reply(entry), 'Last reply')
        streams[0].read.assert_called_once_with(server.NOTIFICATION_TAIL_BYTES)
        path.write_bytes(b'x' * (server.NOTIFICATION_TAIL_BYTES * 2))
        self.assertEqual(server.notification_reply(entry), '')


class MuteTests(unittest.TestCase):
    def test_set_muted_round_trip_and_validation(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'muted.json'
            self.assertEqual(push.muted(store, 'r1'), [])
            self.assertEqual(push.set_muted('%18', True, store, 'r1'), ['%18'])
            self.assertEqual(push.set_muted('%3', True, store, 'r1'), ['%18', '%3'])
            self.assertEqual(push.set_muted('%18', False, store, 'r1'), ['%3'])
            for bad in ('18', '%x', '../a', None):
                with self.assertRaises(ValueError):
                    push.set_muted(bad, True, store, 'r1')
            with self.assertRaises(ValueError):
                push.set_muted('%3', 'yes', store, 'r1')

    def test_mute_does_not_survive_a_tmux_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'muted.json'
            push.set_muted('%3', True, store, 'run-a')
            self.assertEqual(push.muted(store, 'run-b'), [])  # %3 is a different pane now
            self.assertEqual(push.muted(store, ''), [])
            self.assertEqual(push.set_muted('%5', True, store, 'run-b'), ['%5'])  # old entries are dropped
            self.assertEqual(push.muted(store, 'run-a'), [])

    def test_muted_pane_sends_nothing(self):
        sent, now = [], [0]
        notifier = push.Notifier(lambda payload, urgent: sent.append(payload), clock=lambda: now[0], muted=lambda: ['%1'])
        for pane in ('%1', '%2'):
            notifier.observe({'id': pane, 'label': pane}, {'state': 'Approval', 'approval': {'id': 'a'}})
        now[0] = 10
        for pane in ('%1', '%2'):
            notifier.observe({'id': pane, 'label': pane}, {'state': 'Approval', 'approval': {'id': 'a'}})
        self.assertEqual([payload['session'] for payload in sent], ['%2'])


class TitleTests(unittest.TestCase):
    def test_title_uses_the_pane_name(self):
        title = push.notification_title
        self.assertEqual(title({'label': 'web:0.1', 'tmux_session': '$0', 'pane_name': 'api'}), 'web · api')
        self.assertEqual(title({'label': 'web:0.1', 'tmux_session': '$0', 'agent_name': 'fix login'}), 'web · fix login')
        self.assertEqual(title({'label': 'explorer-tether', 'tmux_session': '$0', 'agent_name': 'explorer-tether'}), 'explorer-tether')
        self.assertEqual(title({'label': 'web:0.0', 'tmux_session': '$0'}), 'web')
        self.assertEqual(title({'label': 'edgarization-31-oct', 'tmux_session': '$3', 'pane_name': 'review'}), 'edgarization-31-oct · review')
        self.assertEqual(title({'label': 'Agent task'}), 'Agent task')
        self.assertEqual(title({}), 'Agent')


if __name__ == '__main__':
    unittest.main()
