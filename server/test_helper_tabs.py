"""Helper commands use fake pane options, terminal delivery, clocks, and HTTP."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import agent_tab_cli as cli
import agent_tabs
import auth
import delegate
import helper_tabs as helpers
import paths
import server
from terminal_input import DraftError

OWNER = '0f8fad5b-d9cb-469f-a165-70867728950e'
HELPER = '1f8fad5b-d9cb-469f-a165-70867728950e'
OTHER = '2f8fad5b-d9cb-469f-a165-70867728950e'
REVIEW_SEND = server.review_send


class Tmux:
    def __init__(self):
        self.options = {}
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:4] == [paths.TMUX, 'display-message', '-p', '-t']:
            assert args[5] == '#{pane_id}\t#{@tether_helper}'
            assert kwargs == dict(capture_output=True, text=True, timeout=5)
            pane = args[4]
            return subprocess.CompletedProcess(args, 0 if pane in self.options else 1,
                                               stdout=pane + '\t' + self.options.get(pane, '') + '\n')
        if args[:4] == [paths.TMUX, 'set-option', '-p', '-t']:
            assert args[5] == helpers.OPTION
            assert kwargs == dict(check=True, capture_output=True, timeout=5)
            self.options[args[4]] = args[6]
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError(args)

    def record(self):
        return json.loads(self.options['%13'])

    def saves(self):
        return [args for args, _ in self.calls if args[1] == 'set-option']


class HelperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.entries = [dict(id='%12', agent='codex', thread=OWNER, writable=True),
                        dict(id='%13', agent='claude', thread=HELPER, writable=True)]
        self.run = Tmux()
        self.enterContext(patch.object(server.subprocess, 'run', side_effect=self.run))
        self.enterContext(patch.object(server, 'sessions', return_value=self.entries))
        self.enterContext(patch.object(auth, 'session_value', return_value='test-credential'))
        self.status = self.enterContext(patch.object(server, 'shared_status', return_value={'state': 'Idle'}))
        self.reply = self.enterContext(patch.object(server, 'last_reply', return_value='Old reply'))
        self.timeline = self.enterContext(patch.object(server, 'timeline', return_value={}))
        self.send = self.enterContext(patch.object(server, 'review_send', return_value='queued'))
        self.enterContext(patch.dict(helpers._missing, clear=True))
        self.reset_record()

    def reset_record(self, **changes):
        record = dict(owner_pane='%12', owner_thread=OWNER, helper_thread=HELPER,
                      sent_at=1000, before=helpers.digest('Old reply'))
        record.update(changes)
        self.run.options['%13'] = json.dumps(record)
        self.run.calls.clear()
        return record

    def request(self, action, *, real_response=False, headers=None, peer='127.0.0.1', **body):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = agent_tabs.PATH, 'POST'
        handler.client_address = (peer, 1234)
        raw = json.dumps(dict({'action': action, 'session': '%12', 'pane': '%13', 'message': 'Follow up'}, **body)).encode()
        handler.rfile = io.BytesIO(raw)
        handler.headers = {'Host': 'localhost', 'Origin': 'http://localhost', 'X-Codex-Mobile': '1',
                           'X-Tether-Hook': 'test-credential', 'Content-Length': str(len(raw)), **(headers or {})}
        handler.deny = Mock(return_value=False)
        if real_response:
            handler.wfile = io.BytesIO()
            handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
            handler.do_POST()
            return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def poll(self, now=1010, caller=True):
        return helpers.poll({'pane': '%13', 'session': '%12'}, self.entries[0] if caller else None,
                            self.entries, self.run, self.status, self.reply, self.timeline, now=lambda: now)

    def test_load_rejects_unowned_and_malformed_records_for_both_commands(self):
        for agent in ('claude', 'codex'):
            self.entries[1]['agent'] = agent
            for option in ('', 'not json', '[]', 'null', '{"owner_pane": "bad"}',
                           '{"owner_pane": "%99"}', '{"owner_pane": 12}'):
                for action in ('send', 'wait'):
                    with self.subTest(agent=agent, option=option, action=action):
                        self.run.options['%13'] = option
                        self.assertEqual(self.request(action), (400, {'error': 'That tab is not a helper of this tab.'}))
        self.send.assert_not_called()
        self.assertEqual(self.run.saves(), [])

    def test_stale_identities_refuse_before_any_bind_or_delivery(self):
        for agent in ('claude', 'codex'):
            self.entries[1]['agent'] = agent
            for owner_thread, helper_thread, message in (
                    (OTHER, HELPER, 'This tab changed conversations.'),
                    (OWNER, OTHER, 'That helper changed conversations.'),
                    (None, OTHER, 'That helper changed conversations.')):
                for action in ('send', 'wait'):
                    with self.subTest(agent=agent, owner=owner_thread, helper=helper_thread, action=action):
                        self.reset_record(owner_thread=owner_thread, helper_thread=helper_thread)
                        code, payload = self.request(action)
                        self.assertEqual(code, 409)
                        self.assertTrue(payload['error'].startswith(message))
                        self.assertEqual(self.run.saves(), [])
            self.reset_record()
            self.entries[1]['thread'] = None
            self.assertEqual(self.request('wait')[0], 409)
            self.assertEqual(self.request('send')[0], 409)
            self.entries[1]['thread'] = HELPER
        self.send.assert_not_called()

    def test_first_sight_binds_once_and_skips_pending_or_invalid_threads(self):
        for agent in ('claude', 'codex'):
            for pending in (False, True):
                with self.subTest(agent=agent, pending=pending):
                    self.entries[1].update(agent=agent, pending_conversation=pending)
                    self.entries[0]['pending_conversation'] = pending
                    self.reset_record(owner_thread=None, helper_thread=None)
                    self.poll()
                    self.assertEqual(self.run.record()['helper_thread'], None if pending else HELPER)
                    self.assertEqual(self.run.record()['owner_thread'], None if pending else OWNER)
                    self.assertEqual(len(self.run.saves()), 0 if pending else 1)
                    self.poll()
                    self.assertEqual(len(self.run.saves()), 0 if pending else 1)
        for thread in (None, 'pending-123', 1):
            self.entries[0].update(thread=thread, pending_conversation=False)
            self.assertIsNone(helpers.new_record(self.entries[0])['owner_thread'])
        self.entries[0]['thread'] = OWNER
        with patch.object(helpers.time, 'time', return_value=123):
            self.assertEqual(helpers.new_record(self.entries[0]), dict(owner_pane='%12', owner_thread=OWNER,
                             helper_thread=None, sent_at=123, before=None))

    def test_message_and_pane_validation_precedes_tmux(self):
        for pane in (None, '', 13, '%13\n', '%13;pwd'):
            for action in ('send', 'wait'):
                with self.subTest(pane=pane, action=action):
                    self.assertEqual(self.request(action, pane=pane), (400, {'error': 'Select a helper tab.'}))
        for message, error in ((None, 'Enter a message'), ('', 'Enter a message'), (' \n\t ', 'Enter a message'),
                               ('x' * 20001, 'Enter a message'), ('😀' * 20001, 'Enter a message'),
                               ('hello\x00', 'The message contains'), ('hello\x1b', 'The message contains'),
                               ('hello\x7f', 'The message contains'), ('hello\x85', 'The message contains')):
            with self.subTest(message=repr(message)[:30]):
                code, payload = self.request('send', message=message)
                self.assertEqual(code, 400)
                self.assertTrue(payload['error'].startswith(error))
        self.assertEqual(self.run.calls, [])
        self.send.assert_not_called()
        for message in ('x' * 20000, '😀' * 20000, ' hello\n\tworld '):
            self.assertEqual(self.request('send', message=message)[0], 200)
            self.assertEqual(self.send.call_args.args[1], message)

    def test_send_checks_helper_availability_and_prompts(self):
        for agent in ('claude', 'codex'):
            self.entries[1]['agent'] = agent
            for state in ('Approval', 'Input needed'):
                with self.subTest(agent=agent, state=state):
                    self.status.return_value = {'state': state}
                    self.assertEqual(self.request('send'), (503, {'draft': False, 'error':
                        'That helper waits on a prompt. Use wait to read it, then ask the user to answer it.'}))
            self.entries[1]['writable'] = False
            self.assertEqual(self.request('send'), (503, {'error': 'Messaging is unavailable for that helper.', 'draft': False}))
            self.entries[1]['writable'] = True
        helper = self.entries.pop()
        self.reset_record(helper_thread=None)
        with patch.object(helpers.time, 'time', return_value=1001):
            self.assertEqual(self.request('send'), (503, {'error': 'That helper is still starting. Try again in a moment.', 'draft': False}))
        self.entries.append(helper)
        del self.run.options['%13']
        self.assertEqual(self.request('send'), (400, {'error': 'That session has ended. Select another tab.'}))
        self.send.assert_not_called()

    def test_load_rejects_missing_or_nonnumeric_sent_at(self):
        for sent_at in (None, True, False, '1000', [], {}):
            with self.subTest(sent_at=sent_at):
                self.reset_record(sent_at=sent_at)
                self.assertEqual(helpers.load('%13', self.run), (True, None))
                for action in ('send', 'wait'):
                    self.assertEqual(self.request(action),
                                     (400, {'error': 'That tab is not a helper of this tab.'}))
        record = self.reset_record()
        del record['sent_at']
        self.run.options['%13'] = json.dumps(record)
        self.assertEqual(helpers.load('%13', self.run), (True, None))
        self.assertEqual(self.request('wait')[0], 400)
        for sent_at in (1000, 1000.5):
            self.reset_record(sent_at=sent_at)
            self.assertEqual(helpers.load('%13', self.run)[1]['sent_at'], sent_at)
        self.send.assert_not_called()

    def test_shell_helper_is_missing_instead_of_stale(self):
        self.entries[1].update(agent='shell', thread=None)
        self.assertEqual(self.request('send'),
                         (400, {'error': 'That session has ended. Select another tab.'}))
        self.assertEqual(self.poll(1010), dict(status='running', pane='%13', state='Missing'))
        self.assertEqual(self.poll(1020), dict(status='gone', pane='%13'))
        self.assertNotIn('%13', helpers._missing)
        self.send.assert_not_called()
        self.status.assert_not_called()
        self.assertEqual(self.run.saves(), [])

    def test_send_missing_helper_only_retries_unbound_start_window(self):
        self.entries.pop()
        for helper_thread in (None, HELPER):
            for elapsed in (helpers.START_WAIT - 1, helpers.START_WAIT, helpers.START_WAIT + 1):
                with self.subTest(helper_thread=helper_thread, elapsed=elapsed):
                    self.reset_record(helper_thread=helper_thread)
                    with patch.object(helpers.time, 'time', return_value=1000 + elapsed):
                        expected = (503, {'error': 'That helper is still starting. Try again in a moment.',
                                          'draft': False}) if helper_thread is None and elapsed < helpers.START_WAIT else (
                            400, {'error': 'That session has ended. Select another tab.'})
                        self.assertEqual(self.request('send'), expected)
        self.send.assert_not_called()
        self.assertEqual(self.run.saves(), [])

    def test_send_refuses_unavailable_state_without_saving_or_delivery(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                self.entries[1]['agent'] = agent
                self.status.return_value = {'state': 'Unavailable'}
                self.assertEqual(self.request('send'), (503, {
                    'error': 'Messaging is unavailable for that helper.', 'draft': False}))
        self.send.assert_not_called()
        self.assertEqual(self.run.saves(), [])

    def test_gone_clears_missing_tracking(self):
        self.entries.pop()
        self.poll(1010)
        self.assertIn('%13', helpers._missing)
        self.assertEqual(self.poll(1020)['status'], 'gone')
        self.assertNotIn('%13', helpers._missing)
        self.poll(1021)
        self.assertIn('%13', helpers._missing)
        del self.run.options['%13']
        self.assertEqual(self.poll(1022)['status'], 'gone')
        self.assertNotIn('%13', helpers._missing)

    def test_send_and_poll_do_not_hold_server_send_lock(self):
        def status(entry):
            self.assertFalse(server.SEND_LOCK.locked())
            return {'state': 'Idle'}

        def send(entry, message, state):
            self.assertFalse(server.SEND_LOCK.locked())
            self.assertTrue(helpers.SEND_LOCK.locked())
            self.assertEqual(self.run.record()['before'], helpers.digest('Old reply'))
            self.assertEqual(self.run.record()['sent_at'], 1234)
            return 'queued'

        self.status.side_effect, self.send.side_effect = status, send
        for agent in ('claude', 'codex'):
            self.entries[1]['agent'] = agent
            with self.subTest(agent=agent), patch.object(helpers.time, 'time', return_value=1234):
                self.assertEqual(self.request('send'), (200, {'sent': True, 'pane': '%13', 'delivery': 'queued'}))
                self.assertEqual(self.request('wait')[0], 200)

    def test_failed_save_never_sends_and_failed_delivery_restores_record(self):
        original = self.run.record()
        self.send.side_effect = RuntimeError('Delivery failed')
        self.assertEqual(self.request('send'), (503, {'error': 'Delivery failed', 'draft': False}))
        self.assertEqual(self.run.record(), original)
        self.assertEqual(len(self.run.saves()), 2)
        self.send.reset_mock(side_effect=True)
        with patch.object(helpers, 'save', side_effect=OSError('Cannot save')):
            self.assertEqual(self.request('send')[0], 503)
        self.send.assert_not_called()
        self.send.side_effect = RuntimeError('Delivery failed')
        with patch.object(helpers, 'save', side_effect=[None, OSError('Cannot restore')]):
            self.assertEqual(self.request('send'), (503, {'error': 'Delivery failed', 'draft': False}))

    def test_draft_guards_and_quiet_claude_stash_restore_order(self):
        self.send.side_effect = REVIEW_SEND
        original = self.run.record()
        with patch.object(server, 'stash_quiet_draft', side_effect=DraftError('Draft kept')) as stash, \
                patch.object(server, 'send_text') as paste:
            self.assertEqual(self.request('send'), (503, {'error': 'Draft kept', 'draft': True}))
            stash.assert_called_once()
            paste.assert_not_called()
        self.assertEqual(self.run.record(), original)
        order = Mock()
        with patch.object(server, 'stash_quiet_draft', return_value='quiet draft') as stash, \
                patch.object(server, 'send_text', return_value='delivered') as paste, \
                patch.object(server, 'restore_check') as restore:
            order.attach_mock(stash, 'stash')
            order.attach_mock(paste, 'paste')
            order.attach_mock(restore, 'restore')
            self.assertEqual(self.request('send')[1]['delivery'], 'delivered')
            self.assertEqual([call[0] for call in order.mock_calls], ['stash', 'paste', 'restore'])
            self.assertEqual(restore.call_args.args[1], 'quiet draft')
        self.entries[1]['agent'] = 'codex'
        self.reset_record()
        with patch.object(server, 'send_text', side_effect=DraftError('Draft kept')), \
                patch.object(server, 'stash_quiet_draft') as stash, \
                patch.object(server, 'terminal_clear_draft') as clear, \
                patch.object(server, 'clear_unsent') as unsent:
            self.assertEqual(self.request('send'), (503, {'error': 'Draft kept', 'draft': True}))
            stash.assert_not_called()
            clear.assert_not_called()
            unsent.assert_called_once()
        self.assertEqual(self.run.record(), original)

    def test_turn_completion_and_settle_contract_for_both_agents(self):
        for agent in ('claude', 'codex'):
            for seen, reply, now, state, expected in (
                    ({'finished': 1000}, 'Old reply', 1010, 'Idle', 'finished'),
                    ({}, 'New reply', 1010, 'Idle', 'finished'),
                    ({'since': 1009, 'finished': 1000}, 'New reply', 1010, 'Idle', 'running'),
                    ({'finished': 1000}, 'New reply', 1010, 'Working', 'running'),
                    ({'finished': 999}, 'Old reply', 1001, 'Idle', 'running'),
                    ({}, 'Old reply', 1059, 'Idle', 'running'),
                    ({}, 'Old reply', 1060, 'Idle', 'no_turn'),
                    ({}, ' \n', 1010, 'Idle', 'running'),
                    ({'since': 1007}, 'New reply', 1010, 'Idle', 'finished')):
                with self.subTest(agent=agent, seen=seen, state=state, now=now):
                    self.entries[1]['agent'] = agent
                    self.reset_record()
                    self.status.return_value = {'state': state}
                    self.reply.return_value = reply
                    self.timeline.return_value = seen
                    result = self.poll(now)
                    self.assertEqual(result['status'], expected)
                    if expected == 'finished':
                        self.assertEqual(result, dict(status='finished', pane='%13', reply=reply,
                                                      truncated=False))
                    elif expected == 'no_turn':
                        self.assertEqual(result, dict(status='no_turn', pane='%13', state=state))
                        self.assertNotIn('reply', result)
                    self.assertEqual(self.poll(now), result)
                    self.assertEqual(self.run.saves(), [])
            self.reply.return_value = '😀' * (helpers.MAX_REPLY + 1)
            self.assertEqual(len(self.poll()['reply']), helpers.MAX_REPLY)
            self.assertTrue(self.poll()['truncated'])

    def test_approval_attention_and_ask_bridge_prompts(self):
        questions = [{'header': 'Choice', 'question': 'Which path?', 'multiSelect': False,
                      'options': [{'label': 'A', 'description': 'First'}, {'label': 'B', 'description': 'Second'}]}]
        for agent in ('claude', 'codex'):
            self.entries[1]['agent'] = agent
            for state, key, kind in (('Approval', 'approval', 'approval'), ('Input needed', 'attention', 'input')):
                with self.subTest(agent=agent, state=state):
                    self.status.return_value = {'state': state, key: {'text': 'Please answer'}}
                    self.assertEqual(self.request('wait'), (200, dict(status='needs_input', pane='%13',
                                                                     kind=kind, prompt='Please answer')))
                    self.status.return_value[key]['text'] = 'x' * (helpers.MAX_PROMPT + 1)
                    self.assertEqual(len(self.poll()['prompt']), helpers.MAX_PROMPT)
            self.status.return_value = {'state': 'Input needed', 'attention': {
                'text': 'Claude has questions for you.', 'askBridge': {'questions': questions}}}
            prompt = self.request('wait')[1]['prompt']
            for text in ('Which path?', 'Choice', 'A', 'First', 'B', 'Second'):
                self.assertIn(text, prompt)
            for bridge in ({}, None, {'questions': []}):
                self.status.return_value['attention']['askBridge'] = bridge
                self.assertEqual(self.poll()['prompt'], 'Claude has questions for you.')

    def test_approval_choices_survive_truncation(self):
        text = 'Would you like to run the following command?\n\n$ curl -sI https://example.com'
        options = [{'id': 1, 'text': 'Yes, proceed (y)'},
                   {'id': 2, 'text': "Yes, and don't ask again (p)"},
                   {'id': 3, 'text': 'No, and tell Codex what to do differently (esc)'}]
        choices = ("1. Yes, proceed (y)\n2. Yes, and don't ask again (p)\n"
                   '3. No, and tell Codex what to do differently (esc)')
        for agent in ('claude', 'codex'):
            self.entries[1]['agent'] = agent
            for request in (text, 'x' * (helpers.MAX_PROMPT + 1), ''):
                with self.subTest(agent=agent, request_length=len(request)):
                    self.status.return_value = {'state': 'Approval', 'approval': {
                        'text': request, 'options': options}}
                    prompt = self.poll()['prompt']
                    self.assertLessEqual(len(prompt), helpers.MAX_PROMPT)
                    self.assertTrue(prompt.endswith(choices))
                    expected = request[:helpers.MAX_PROMPT - len(choices) - 2] + '\n\n' if request else ''
                    self.assertEqual(prompt, expected + choices)
            for approval in ({'text': text}, {'text': text, 'options': []}):
                with self.subTest(agent=agent, approval=approval):
                    self.status.return_value = {'state': 'Approval', 'approval': approval}
                    self.assertEqual(self.poll()['prompt'], text)
            self.status.return_value = {'state': 'Approval', 'approval': {
                'text': text, 'options': [None, 'invalid', {}, {'text': ''}, {'text': ' '},
                                         {'text': None}, {'text': 7},
                                         {'id': 'invalid', 'text': 'Fallback'},
                                         {'text': 'Missing ID'}, {'id': 12, 'text': 'Explicit ID'}]}}
            with self.subTest(agent=agent, malformed=True):
                self.assertEqual(self.poll()['prompt'], text + '\n\n8. Fallback\n9. Missing ID\n12. Explicit ID')
            self.status.return_value = {'state': 'Approval', 'approval': {
                'text': text, 'options': [{'text': 'y' * helpers.MAX_PROMPT}]}}
            with self.subTest(agent=agent, oversized_choices=True):
                self.assertEqual(self.poll()['prompt'], ('1. ' + 'y' * helpers.MAX_PROMPT)[:helpers.MAX_PROMPT])
            self.status.return_value = {'state': 'Input needed', 'attention': {
                'text': text, 'options': options}}
            with self.subTest(agent=agent, attention=True):
                self.assertEqual(self.poll()['prompt'], text)

    def test_gone_start_and_discovery_grace_and_recovery(self):
        helper = self.entries.pop()
        self.assertEqual(self.poll(), dict(status='running', pane='%13', state='Missing'))
        self.assertEqual(self.poll(1019)['status'], 'running')
        self.assertEqual(self.poll(1020), dict(status='gone', pane='%13'))
        self.entries.append(helper)
        self.poll(1021)
        self.assertNotIn('%13', helpers._missing)
        self.entries.pop()
        self.assertEqual(self.poll(1022)['state'], 'Missing')
        self.reset_record(helper_thread=None)
        self.assertEqual(self.poll(1299), dict(status='running', pane='%13', state='Starting'))
        helpers._missing.clear()
        self.assertEqual(self.poll(1300)['state'], 'Missing')
        self.assertEqual(self.poll(1310)['status'], 'gone')
        del self.run.options['%13']
        self.assertEqual(self.request('wait'), (200, dict(status='gone', pane='%13')))
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='%99\t{}')):
            self.assertEqual(self.request('wait')[1]['status'], 'gone')
        for error in (OSError('tmux hiccup'), subprocess.TimeoutExpired('tmux', 5)):
            with patch.object(server.subprocess, 'run', side_effect=error):
                self.assertEqual(self.request('wait'), (503, {'error': 'Could not read the helper tab.', 'draft': False}))

    def test_missing_owner_retries_only_after_ownership_check(self):
        self.assertEqual(self.request('wait', session='%99'),
                         (400, {'error': 'That tab is not a helper of this tab.'}))
        self.entries.pop(0)
        self.assertEqual(self.request('wait'), (503, {'error': 'Could not read this tab. Try again.', 'draft': False}))
        self.assertEqual(self.request('send'), (400, {'error': 'That session has ended. Select another tab.'}))

    def test_missing_helper_can_bind_owner_without_losing_target(self):
        self.reset_record(owner_thread=None, helper_thread=None)
        self.entries.pop()
        self.assertEqual(self.poll()['state'], 'Starting')
        self.assertEqual(self.run.record()['owner_thread'], OWNER)
        self.assertEqual(len(self.run.saves()), 1)

    def test_wait_keeps_sessions_and_send_invalidates_them(self):
        for action in ('wait', 'send'):
            with self.subTest(action=action), patch.object(server, 'forget_sessions') as forget:
                self.assertEqual(self.request(action, real_response=True)[0], 200)
                self.assertEqual(forget.call_count, 0 if action == 'wait' else 1)

    def test_helper_routes_preserve_auth_guards(self):
        for action in ('send', 'wait'):
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''},
                            {'X-Tether-Hook': ''}, {'X-Tether-Hook': 'wrong'}):
                self.assertEqual(self.request(action, headers=headers)[0], 403)
            self.assertEqual(self.request(action, peer='192.0.2.1')[0], 403)
        self.assertEqual(self.run.calls, [])
        self.send.assert_not_called()

    def test_start_saves_owner_before_launch_and_browser_does_not_pass_owner(self):
        owner = helpers.new_record(self.entries[0])
        body = dict(session='%12', thread=OWNER, agent='claude', message='Task')
        for record in (owner, None):
            launch = Mock()
            with patch.object(delegate, 'new_window_after', return_value='%13'), \
                    patch.object(delegate, 'write_prompt', return_value=self.root / 'prompt.md'):
                delegate.start(self.entries[0], body, '/Users/test', run=launch, owner=record)
            calls = [call.args[0] for call in launch.call_args_list]
            self.assertEqual(calls[0][5], '@chat_name')
            if record is not None:
                self.assertEqual(calls[1][5], helpers.OPTION)
                self.assertEqual(json.loads(calls[1][6]), owner)
                self.assertEqual(calls[2][1], 'send-keys')
            else:
                self.assertEqual(calls[1][1], 'send-keys')
        with patch.object(delegate, 'new_window_after', return_value='%13'), \
                patch.object(helpers, 'save', side_effect=OSError('Cannot save')), \
                patch.object(delegate, 'write_prompt') as prompt:
            launch = Mock()
            with self.assertRaises(OSError):
                delegate.start(self.entries[0], body, '/Users/test', run=launch, owner=owner)
            self.assertFalse(any(call.args[0][1] == 'send-keys' for call in launch.call_args_list))
            prompt.assert_not_called()
        with patch.object(delegate, 'start', return_value=('%13', 'Task')) as start, \
                patch.object(agent_tabs, '_start_group', return_value=contextlib.nullcontext('')), \
                patch.object(server, 'agent_folder', return_value=str(self.root)):
            self.assertEqual(self.request('start')[0], 201)
            self.assertEqual(start.call_args.kwargs['owner']['owner_thread'], OWNER)
            handler = object.__new__(server.Handler)
            handler.path, handler.command = '/mobile/api/delegate', 'POST'
            raw = json.dumps(body).encode()
            handler.rfile = io.BytesIO(raw)
            handler.headers = {'Host': 'localhost', 'Origin': 'http://localhost', 'X-Codex-Mobile': '1',
                               'Content-Length': str(len(raw))}
            handler.deny, handler.respond = Mock(return_value=False), Mock()
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 201)
            self.assertNotIn('owner', start.call_args.kwargs)


class HelperCLITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'TMUX_PANE': '%12', 'MOBILE_TMUX_PORT': '12345'}))
        self.stdout = self.enterContext(patch.object(sys, 'stdout', new_callable=io.StringIO))
        self.stderr = self.enterContext(patch.object(sys, 'stderr', new_callable=io.StringIO))
        token = self.enterContext(patch.object(auth, 'TOKEN_FILE'))
        token.read_text.return_value = 'test-secret'
        self.enterContext(patch.object(auth, 'session_value', return_value='test-credential'))
        self.opener = self.enterContext(patch.object(cli, 'build_opener')).return_value
        self.now = 0
        self.enterContext(patch.object(cli.time, 'monotonic', side_effect=lambda: self.now))
        self.sleep = self.enterContext(patch.object(cli.time, 'sleep', side_effect=self.advance))

    def advance(self, seconds):
        self.now += seconds

    def response(self, result):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(result).encode()
        return response

    def output(self):
        return json.loads(self.stdout.getvalue())

    def clear_output(self):
        self.stdout.seek(0)
        self.stdout.truncate()
        self.stderr.seek(0)
        self.stderr.truncate()
        self.opener.open.reset_mock(side_effect=True)
        self.now = 0

    def test_send_maps_text_and_stdin_with_longer_delivery_timeout(self):
        self.opener.open.return_value = self.response({'sent': True, 'pane': '%13', 'delivery': 'queued'})
        for message in ('text', '-'):
            with self.subTest(message=message), patch.object(sys, 'stdin', io.StringIO('stdin text')):
                self.assertEqual(cli.main(['send', '--pane', '%13', message]), cli.FINISHED)
                request = self.opener.open.call_args.args[0]
                self.assertEqual(json.loads(request.data), dict(action='send', pane='%13', session='%12',
                                 message='stdin text' if message == '-' else 'text'))
                self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 60)

    def test_usage_errors_do_not_open_requests(self):
        self.assertEqual(cli.arguments(['wait', '--pane', '%13'])['timeout'], 600)
        for seconds in (1, 600):
            self.assertEqual(cli.arguments(['wait', '--pane', '%13', '--timeout', str(seconds)])['timeout'], seconds)
        for args in (['send', '--pane', '%13', ''], ['send'], ['wait'],
                     ['wait', '--pane', '%13', '--timeout', '0'],
                     ['wait', '--pane', '%13', '--timeout', '601'],
                     ['wait', '--pane', '%13', '--timeout', '1.5'],
                     ['wait', '--pane', '%13', '--timeout', 'bad']):
            with self.subTest(args=args):
                self.assertEqual(cli.main(args), cli.ERROR)
                self.assertTrue(self.stderr.getvalue().startswith('Tether tab error: '))
        with patch.object(sys, 'stdin', io.StringIO(' \n')):
            self.assertEqual(cli.main(['send', '--pane', '%13', '-']), cli.ERROR)
        self.opener.open.assert_not_called()

    def test_wait_terminal_statuses_print_one_json_object_and_exit_codes(self):
        for status, code in (('finished', 0), ('gone', 4), ('needs_input', 5), ('no_turn', 6)):
            with self.subTest(status=status):
                self.clear_output()
                result = dict(status=status, pane='%13', reply='Report', prompt='Please answer')
                self.opener.open.return_value = self.response(result)
                self.assertEqual(cli.main(['wait', '--pane', '%13']), code)
                self.assertEqual(self.output(), result)
                self.assertEqual(len(self.stdout.getvalue().splitlines()), 1)
                self.assertEqual(self.stderr.getvalue(), '')
                request = self.opener.open.call_args.args[0]
                self.assertEqual(json.loads(request.data), dict(action='wait', session='%12', pane='%13'))
                self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 10)
                self.opener.open.return_value.__enter__.return_value.read.assert_called_once_with(2 * 1024 * 1024)

    def test_terminal_status_after_deadline_is_honored(self):
        for status, code in (('finished', cli.FINISHED), ('gone', cli.GONE),
                             ('needs_input', cli.NEEDS_INPUT), ('no_turn', cli.NO_TURN)):
            with self.subTest(status=status):
                self.clear_output()
                result = dict(status=status, pane='%13', reply='Report', prompt='Please answer')
                response = self.response(result)

                def slow_read(limit):
                    self.advance(1.2)
                    return json.dumps(result).encode()

                response.__enter__.return_value.read.side_effect = slow_read
                self.opener.open.return_value = response
                self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '1']), code)
                self.assertEqual(self.output(), result)
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(self.stderr.getvalue(), '')

    def test_unknown_status_is_an_error_even_after_deadline(self):
        for elapsed in (0, 1.2):
            with self.subTest(elapsed=elapsed):
                self.clear_output()

                def response(request, timeout):
                    self.advance(elapsed)
                    return self.response(dict(status='unknown', pane='%13'))

                self.opener.open.side_effect = response
                self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '1']), cli.ERROR)
                self.assertEqual(self.stdout.getvalue(), '')
                self.assertTrue(self.stderr.getvalue().startswith('Tether tab error: '))
                self.assertEqual(self.opener.open.call_count, 1)

    def test_wait_retries_http_exception_during_response_read(self):
        response = self.response({})
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b'partial', 10)
        result = dict(status='finished', pane='%13', reply='Report')
        self.opener.open.side_effect = [response, self.response(result)]
        self.assertEqual(cli.main(['wait', '--pane', '%13']), cli.FINISHED)
        self.assertEqual(self.output(), result)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.stderr.getvalue(), '')

    def test_wait_polls_until_finished(self):
        self.opener.open.side_effect = [self.response(dict(status='running', state='Working')),
                                       self.response(dict(status='running', state='Idle')),
                                       self.response(dict(status='finished', pane='%13', reply='Report'))]
        self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '6']), 0)
        self.assertEqual(self.output()['reply'], 'Report')
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual([call.kwargs['timeout'] for call in self.opener.open.call_args_list], [6, 4, 2])

    def test_deadline_stops_requests_and_keeps_last_state(self):
        self.opener.open.return_value = self.response(dict(status='running', state='Working'))
        self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '3']), 3)
        self.assertEqual(self.output(), dict(status='timeout', pane='%13', state='Working', timeout=3))
        self.assertEqual(self.now, 3)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual([call.kwargs['timeout'] for call in self.opener.open.call_args_list], [3, 1])
        for call in self.opener.open.call_args_list:
            self.assertEqual(json.loads(call.args[0].data)['action'], 'wait')

    def test_wait_retries_503_and_connection_errors_until_finished(self):
        self.opener.open.side_effect = [HTTPError('http://localhost', 503, 'Unavailable', {},
                                                io.BytesIO(b'{"error":"Starting"}')),
                                       URLError('Restart'), OSError('Unavailable'),
                                       self.response(dict(status='finished', pane='%13', reply='Report'))]
        self.assertEqual(cli.main(['wait', '--pane', '%13']), 0)
        self.assertEqual(self.output()['status'], 'finished')
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertEqual(self.stderr.getvalue(), '')

    def test_transient_errors_at_deadline_add_error_and_keep_last_state(self):
        for error, text in ((URLError('Restart'), 'Restart'), (OSError('Offline'), 'Offline'),
                            (http.client.IncompleteRead(b'partial', 10), 'IncompleteRead'),
                            (HTTPError('http://localhost', 503, 'Unavailable', {},
                                       io.BytesIO(b'{"error":"Starting"}')), 'Starting')):
            with self.subTest(error=error):
                self.clear_output()
                self.opener.open.side_effect = [self.response(dict(status='running', state='Working')), error]
                self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '3']), 3)
                result = self.output()
                self.assertEqual((result['status'], result['state'], result['timeout']), ('timeout', 'Working', 3))
                self.assertIn(text, result['error'])
                self.assertEqual(self.opener.open.call_count, 2)

    def test_no_successful_poll_times_out_with_null_state(self):
        self.opener.open.side_effect = URLError('Restart')
        self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '1']), 3)
        self.assertIsNone(self.output()['state'])
        self.assertIn('Restart', self.output()['error'])
        self.assertEqual(self.opener.open.call_count, 1)

    def test_request_that_uses_deadline_does_not_sleep_or_poll_again(self):
        def slow(request, timeout):
            self.advance(timeout + .2)
            raise URLError('Slow')
        self.opener.open.side_effect = slow
        self.assertEqual(cli.main(['wait', '--pane', '%13', '--timeout', '1']), 3)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertLess(self.now, 2)

    def test_400_and_409_stop_immediately(self):
        for code in (400, 409):
            with self.subTest(code=code):
                self.clear_output()
                self.opener.open.side_effect = HTTPError('http://localhost', code, 'Refused', {},
                                                         io.BytesIO(b'{"error":"Wrong helper"}'))
                self.assertEqual(cli.main(['wait', '--pane', '%13']), 1)
                self.assertEqual(self.stderr.getvalue(), 'Tether tab error: Wrong helper\n')
                self.assertEqual(self.stdout.getvalue(), '')
                self.assertEqual(self.opener.open.call_count, 1)


if __name__ == '__main__':
    unittest.main()
