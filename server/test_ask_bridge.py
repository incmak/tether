"""Bridge tests use fake pane identities and an isolated, ephemeral HTTP server."""
import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import ask_bridge as bridge
import auth
import claude_hooks
import server
import terminal_input
import agents
from test_tts import handler
from own_time import own_time


QUESTIONS = [
    {'question': 'Which library?', 'header': 'Library', 'multiSelect': False,
     'options': [{'label': 'React', 'description': 'Components', 'preview': '<p>Example</p>'},
                 {'label': 'Vue', 'description': 'Framework'}]},
    {'question': 'Which features?', 'header': 'Features', 'multiSelect': True,
     'options': [{'label': 'Search', 'description': ''}, {'label': 'Export', 'description': ''}]},
    {'question': 'Which name?', 'header': 'Name', 'options': [{'label': 'A'}, {'label': 'B'}]},
]
ANSWERS = {'Which library?': 'React', 'Which features?': ['Search', 'Export'], 'Which name?': 'My own name'}
ENTRY = dict(id='%9', thread='session-1', agent='claude', writable=True, owner_pid=123, process_start='start-1')
DATA = dict(hook_event_name='PreToolUse', tool_name='AskUserQuestion', tool_use_id='tool-1',
            session_id='session-1', tool_input={'questions': QUESTIONS, 'metadata': {'source': 'test'}})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.watched = {'%9': self.now, '%10': self.now}
        self.streams = set()
        self.store = bridge.Pending(lambda: self.now, lambda: self.now,
                                    lambda pane, window: pane in self.streams or
                                    self.store.monotonic() - self.watched.get(pane, float('-inf')) < window)
        settings = patch.object(bridge, 'install_settings', return_value={})
        settings.start()
        self.addCleanup(settings.stop)
        for name, value in [('hook_process', 'hook-start'), ('process_alive', True)]:
            p = patch.object(bridge, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.registration = dict(session='%9', session_id='session-1', nonce='nonce-12345678901234567890',
                                 tool_use_id='tool-1', tool_input=copy.deepcopy(DATA['tool_input']), pid=456)
        self.ask = self.store.register(ENTRY, self.registration)
        self.answer = dict(session='%9', **self.ask, answers=ANSWERS, requestId='tap-1')

    def test_no_recent_watch_registers_no_wait_without_retaining_payload(self):
        store = bridge.Pending()
        self.assertEqual(store.register(ENTRY, self.registration), {'wait': 0})
        self.assertEqual(store.records, {})

    def test_engage_extends_deadline_and_attention(self):
        self.now += 2
        self.assertEqual(self.store.engage(ENTRY, self.ask), {'expires': self.now + 60})
        shown = self.store.attention(ENTRY)
        self.assertEqual(shown['expires'], self.now + 60)
        self.assertEqual(shown['idle'], 60)

    def test_engage_rate_limit_uses_monotonic_time_per_record(self):
        monotonic = [self.now]
        self.store.monotonic = lambda: monotonic[0]
        first = self.store.engage(ENTRY, self.ask)['expires']
        self.now += 2
        monotonic[0] += .999
        self.assertEqual(self.store.engage(ENTRY, self.ask)['expires'], first)
        other = self.store.register(dict(ENTRY, id='%10'),
                                    dict(self.registration, nonce='another-12345678901234567890'))
        self.assertEqual(self.store.engage(dict(ENTRY, id='%10'), other)['expires'], self.now + 60)
        monotonic[0] = 1001
        self.assertEqual(self.store.engage(ENTRY, self.ask)['expires'], self.now + 60)

    def test_engage_never_shortens_long_notice(self):
        self.store.records.clear()
        with patch.object(bridge, 'wait_seconds', return_value=120):
            ask = self.store.register(ENTRY, self.registration)
        self.assertEqual(self.store.engage(ENTRY, ask), {'expires': self.now + 120})

    def test_engage_caps_at_registration_plus_max_wait(self):
        self.streams.add('%9')
        registered = self.now
        for elapsed in range(0, 590, 4):
            self.now = registered + elapsed
            self.store.poll(ENTRY, self.ask)
            self.assertEqual(self.store.engage(ENTRY, self.ask)['expires'],
                             min(registered + 590, self.now + 60))
        self.now = registered + 590
        with self.assertRaises(bridge.Conflict):
            self.store.engage(ENTRY, self.ask)

    def test_stream_alone_never_renews_notice(self):
        self.streams.add('%9')
        for _ in range(29):
            self.now += 1
            self.assertEqual(self.store.poll(ENTRY, self.ask)['expires'], 1030)
        self.now += 1
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)

    def test_closed_stream_stops_within_grace_after_last_poll(self):
        self.streams.add('%9')
        self.now += 2
        self.streams.clear()
        for _ in range(7):
            self.now += 1
            self.assertEqual(self.store.poll(ENTRY, self.ask)['state'], 'pending')
        self.now += 1
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)

    def test_hook_uses_renewed_deadline_and_accepts_later_answer(self):
        for answer in (False, True):
            with self.subTest(answer=answer):
                self.now = 1000
                self.store.records.clear()
                self.streams.add('%9')
                def request(body):
                    if body['action'] == 'poll':
                        if self.now == 1000:
                            self.store.engage(ENTRY, body)
                        if answer and self.now == 1040:
                            self.store.answer(ENTRY, dict(body, answers=ANSWERS, requestId='later'))
                    return getattr(self.store, body['action'])(ENTRY, body)
                def wait(delay):
                    self.now += delay
                with patch.dict(os.environ, TMUX_PANE='%9'):
                    result = bridge.hook(DATA, request, wait, clock=lambda: self.now, wall_clock=lambda: self.now)
                self.assertEqual(self.now, 1040 if answer else 1060)
                if answer:
                    self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'allow')
                else:
                    self.assertIsNone(result)
                self.assertIsNone(self.store.attention(ENTRY))

    def test_stream_with_old_poll_passes_gate_and_keeps_wait(self):
        with patch.dict(server.WATCHED, {'%9': self.now - 100}, clear=True), \
                patch.dict(server.WATCHING, {'%9': 1}, clear=True), \
                own_time(server, monotonic=Mock(side_effect=lambda: self.now)):
            store = bridge.Pending(lambda: self.now, lambda: self.now, watching=server.ask_watching)
            ask = store.register(ENTRY, self.registration)
            self.assertEqual(ask['wait'], 30)
            for _ in range(4):
                self.now += 4
                self.assertEqual(store.poll(ENTRY, ask)['state'], 'pending')
            server.WATCHING['%9'] = 0
            with self.assertRaises(bridge.Conflict):
                store.poll(ENTRY, ask)

    def test_watch_window_is_per_pane_and_monotonic(self):
        self.store.records.clear()
        for age, seconds in [(60, 0), (61, 0), (59, 30)]:
            with self.subTest(age=age):
                self.store.records.clear()
                self.watched['%9'] = self.now - age
                self.assertEqual(self.store.register(ENTRY, self.registration)['wait'], seconds)
        self.store.records.clear()
        self.watched.pop('%9')
        self.assertEqual(self.store.register(ENTRY, self.registration), {'wait': 0})

    def test_watch_stops_ends_wait_despite_hook_heartbeat(self):
        for _ in range(9):
            self.now += 1
            self.assertEqual(self.store.poll(ENTRY, self.ask)['state'], 'pending')
        self.now += 1
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)
        self.assertEqual(self.store.records, {})

    def test_already_idle_watch_falls_through_on_first_poll(self):
        self.store.records.clear()
        self.watched['%9'] = self.now - 10
        ask = self.store.register(ENTRY, self.registration)
        self.assertEqual(ask['wait'], 30)
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, ask)

    def test_stopped_watch_makes_hook_fall_through_without_a_decision(self):
        self.store.records.clear()
        started = self.now
        def request(body):
            return getattr(self.store, body['action'])(ENTRY, body)
        def wait(delay):
            self.now += delay
        with patch.dict(os.environ, TMUX_PANE='%9'):
            self.assertIsNone(bridge.hook(DATA, request, wait=wait, clock=lambda: self.now, wall_clock=lambda: self.now))
        self.assertEqual(self.now - started, 10)
        self.assertEqual(self.store.records, {})

    def test_watch_poll_renews_grace_and_wall_clock_does_not_control_it(self):
        monotonic = [1000]
        self.store.monotonic = lambda: monotonic[0]
        for _ in range(8):
            monotonic[0] += 4
            self.watched['%9'] = monotonic[0]
            self.assertEqual(self.store.poll(ENTRY, self.ask)['state'], 'pending')
        monotonic[0] += 10
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)

    def test_configured_wait_and_clamp_are_read_at_registration(self):
        for value, seconds in [(5, 5), (47, 47), (120, 120), (0, 5), (999, 120), ('bad', 30), (True, 30)]:
            with self.subTest(value=value), patch.object(bridge, 'install_settings', return_value={'ask_bridge_wait': value}):
                self.store.records.clear()
                ask = self.store.register(ENTRY, self.registration)
                self.assertEqual(ask['wait'], seconds)
                self.assertEqual(ask['expires'], self.now + seconds)
                self.now += seconds - 1
                self.watched['%9'] = self.now
                self.store.records[ask['id']]['lease'] = self.now + 5
                self.assertEqual(self.store.poll(ENTRY, ask)['state'], 'pending')
                self.now += 1
                with self.assertRaises(bridge.Conflict):
                    self.store.poll(ENTRY, ask)
                self.watched['%9'] = self.now

    def test_one_answer_dedupes_before_and_after_delivery(self):
        self.assertEqual(self.store.answer(ENTRY, self.answer), {'status': 'submitted'})
        self.assertEqual(self.store.answer(ENTRY, self.answer), {'status': 'submitted'})
        out = self.store.poll(ENTRY, self.ask)
        self.assertEqual(out['answers'], dict(ANSWERS, **{'Which features?': 'Search, Export'}))
        self.assertEqual(self.store.answer(ENTRY, self.answer), {'status': 'submitted'})
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, dict(self.answer, requestId='tap-2'))
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, dict(self.answer, answers=dict(ANSWERS, **{'Which name?': 'changed'})))
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)
        self.store.retire(ENTRY, self.ask)
        item = self.store.records[self.ask['id']]
        self.assertNotIn('questions', item)
        self.assertNotIn('answers', item)

    def test_attention_is_complete_and_does_not_mutate_payload(self):
        shown = self.store.attention(ENTRY)
        self.assertEqual(set(shown), {'id', 'nonce', 'questions', 'expires', 'idle'})
        self.assertEqual(shown['questions'], QUESTIONS)
        shown['questions'][0]['question'] = 'changed'
        self.assertEqual(self.store.attention(ENTRY)['questions'], QUESTIONS)
        self.assertIsNone(self.store.attention(dict(ENTRY, thread='other')))

    def test_expiry_disconnect_and_reused_pane_reject_answers(self):
        for changed in [dict(ENTRY, thread='new'), dict(ENTRY, owner_pid=124), dict(ENTRY, process_start='new')]:
            with self.assertRaises(bridge.Conflict):
                self.store.answer(changed, self.answer)
        for bad in [dict(self.answer, nonce='other'), dict(self.answer, id='unknown')]:
            with self.assertRaises(bridge.Conflict):
                self.store.answer(ENTRY, bad)
        self.now += bridge.LEASE_SECONDS
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)
        self.assertEqual(self.store.records, {})

    def test_absolute_expiry_even_with_heartbeat(self):
        self.now += bridge.WAIT_SECONDS - 1
        self.watched['%9'] = self.now
        self.store.records[self.ask['id']]['lease'] = self.now + 5
        self.store.poll(ENTRY, self.ask)
        self.now += 1
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)

    def test_dead_hook_retires_prompt(self):
        with patch.object(bridge, 'process_alive', return_value=False):
            self.assertIsNone(self.store.attention(ENTRY))
            with self.assertRaises(bridge.Conflict):
                self.store.answer(ENTRY, self.answer)

    def test_handoff_supplies_no_answer(self):
        self.assertEqual(self.store.answer(ENTRY, dict(self.answer, answers=None)), {'status': 'handoff'})
        self.assertIsNone(self.store.attention(ENTRY))
        with self.assertRaises(bridge.Conflict):
            self.store.poll(ENTRY, self.ask)
        with self.assertRaises(bridge.Conflict):
            self.store.answer(ENTRY, self.answer)

    def test_bad_answers_do_not_consume_the_nonce(self):
        missing = dict(self.answer)
        missing.pop('answers')
        with self.assertRaises(ValueError):
            self.store.answer(ENTRY, missing)
        for answers in [{}, [], {'other': 'React'}, dict(ANSWERS, **{'Which name?': ''}),
                        dict(ANSWERS, **{'Which library?': ['React']}),
                        dict(ANSWERS, **{'Which features?': ['Unknown']}),
                        dict(ANSWERS, **{'Which features?': ['Search', 'Search']}),
                        dict(ANSWERS, **{'Which name?': 'x' * bridge.MAX_ANSWER})]:
            with self.subTest(answers=type(answers).__name__), self.assertRaises(ValueError):
                self.store.answer(ENTRY, dict(self.answer, answers=answers))
        self.assertIsNotNone(self.store.attention(ENTRY))
        self.store.answer(ENTRY, self.answer)

    def test_bounds_and_duplicate_registration(self):
        with self.assertRaises(bridge.Conflict):
            self.store.register(ENTRY, self.registration)
        with patch.object(bridge, 'MAX_PENDING', 1), self.assertRaises(bridge.Conflict):
            self.store.register(dict(ENTRY, id='%10'), dict(self.registration, nonce='another-12345678901234567890'))
        for tool in [{'questions': []}, {'questions': QUESTIONS * 2}, {'questions': [{'kind': 'text'}]},
                     {'questions': QUESTIONS, 'metadata': 'x' * bridge.MAX_PAYLOAD},
                     {'questions': [QUESTIONS[0], QUESTIONS[0]]}]:
            with self.assertRaises(ValueError):
                bridge.questions_input(tool)

    def test_live_status_and_footer_expose_wait_without_busy_spinner(self):
        with patch.object(bridge, 'enabled', return_value=True), patch.object(bridge, 'pending', self.store), \
                patch.object(terminal_input, 'capture', return_value=''), \
                patch.object(terminal_input, 'login_capture', return_value=''), \
                patch.object(terminal_input, 'login_progress', return_value=None), \
                patch.object(terminal_input, '_live_status', return_value={'state': 'Working', 'approval': None}):
            status = terminal_input.live_status(ENTRY)
            self.assertEqual(status['state'], 'Input needed')
            self.assertEqual(status['attention']['askBridge']['questions'], QUESTIONS)
            with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
                footer = claude_hooks.overlay(ENTRY, {'spinner': {'verb': 'Working'}, 'running': {}})
            self.assertIsNone(footer['spinner'])
        with patch.object(bridge, 'enabled', return_value=False), patch.object(bridge, 'pending', self.store):
            self.assertEqual(bridge.overlay(ENTRY, {'state': 'Idle'}), {'state': 'Idle'})
            self.assertEqual(self.store.records, {})

    def test_general_status_cache_does_not_retain_question_payloads(self):
        status = {'state': 'Input needed', 'attention': {'askBridge': self.ask}}
        with patch.object(server, 'STATUS_CACHE', {}), patch.object(server, 'live_status', return_value=status):
            self.assertEqual(server.shared_status(ENTRY), status)
            self.assertNotIn(ENTRY['id'], server.STATUS_CACHE)


class HookTests(unittest.TestCase):
    def test_bridge_verifies_owner_registry_and_socket_without_keyboard_focus(self):
        entry = dict(ENTRY, registry='/isolated/registry.json')
        registry = {'pid': 123, 'sessionId': 'session-1'}
        def read(path, *args, **kwargs):
            if str(path).endswith('/comm'):
                return 'claude\n'
            if str(path).endswith('/stat'):
                return '123 (claude) S 100 0'
            return json.dumps(registry)
        with patch.object(agents, 'process_start', return_value='start-1'), \
                patch.object(Path, 'read_text', read), \
                patch.object(bridge.subprocess, 'run', return_value=Mock(stdout='100\t/private/socket\n')) as run:
            self.assertEqual(bridge.verify_entry(entry), '/private/socket')
            self.assertEqual(run.call_args.args[0][-1], '#{pane_pid}\t#{socket_path}')
            registry['sessionId'] = 'other'
            with self.assertRaises(bridge.Conflict):
                bridge.verify_entry(entry)
            registry['sessionId'] = 'session-1'
            with self.assertRaises(bridge.Conflict):
                bridge.verify_entry(dict(entry, process_start='old'))
            with self.assertRaises(bridge.Conflict):
                bridge.verify_entry(dict(entry, pending_conversation=True))

    def test_register_wait_answer_preserves_original_input(self):
        calls = []
        def request(body):
            calls.append(body)
            if body['action'] == 'register':
                return {'id': 'ask-1', 'wait': 30, 'expires': time.time() + 30}
            if body['action'] == 'poll':
                return dict({'state': 'pending'} if len(calls) == 2 else
                            {'state': 'answered', 'answers': ANSWERS}, expires=time.time() + 30)
            return {'state': 'retired'}
        wait = Mock()
        original = copy.deepcopy(DATA)
        with patch.dict(os.environ, TMUX_PANE='%9'):
            result = bridge.hook(DATA, request, wait=wait)
        self.assertEqual(DATA, original)
        self.assertEqual([c['action'] for c in calls], ['register', 'poll', 'poll', 'retire'])
        self.assertEqual(calls[0]['tool_use_id'], 'tool-1')
        self.assertEqual(calls[0]['session_id'], 'session-1')
        self.assertEqual(calls[0]['session'], '%9')
        self.assertGreaterEqual(len(calls[0]['nonce']), 20)
        wait.assert_called_once_with(.5)
        output = result['hookSpecificOutput']
        self.assertEqual(output['permissionDecision'], 'allow')
        self.assertEqual(output['updatedInput'], dict(DATA['tool_input'], answers=dict(ANSWERS, **{'Which features?': 'Search, Export'})))

    def test_timeout_retires_before_empty_stdout_exit_zero(self):
        now = [0]
        def wait(delay):
            now[0] += delay
        def response(body):
            return {'id': 'ask', 'wait': 30, 'state': 'pending', 'expires': 30}
        request = Mock(side_effect=response)
        self.assertIsNone(bridge.hook(DATA, request, wait=wait, clock=lambda: now[0], wall_clock=lambda: now[0]))
        self.assertEqual(now[0], 30)
        self.assertEqual(request.call_args.args[0]['action'], 'retire')
        with patch.object(bridge, 'hook', return_value=None), patch('sys.stdin', io.StringIO(json.dumps(DATA))), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertIsNone(bridge.main())
        self.assertEqual(stdout.getvalue(), '')

    def test_failures_never_emit_allow_or_answers(self):
        for request in [Mock(side_effect=ConnectionRefusedError),
                        Mock(side_effect=[{'id': 'ask', 'wait': 30, 'expires': time.time() + 30}, KeyboardInterrupt, {}]),
                        Mock(side_effect=[{'id': 'ask', 'wait': 30, 'expires': time.time() + 30}, TimeoutError, {}]),
                        Mock(side_effect=[{'id': 'ask', 'wait': 30, 'expires': time.time() + 30}, {'state': 'retired'}, {}]),
                        Mock(side_effect=[{'id': 'ask', 'wait': 30, 'expires': time.time() + 30}, {'state': 'answered', 'answers': {}, 'expires': time.time() + 30}, {}])]:
            self.assertIsNone(bridge.hook(DATA, request, wait=Mock()))
        request = Mock()
        self.assertIsNone(bridge.hook(dict(DATA, agent_id='child'), request))
        request.assert_not_called()

    def test_no_wait_and_invalid_register_reply_never_poll_or_sleep(self):
        for reply in [{'wait': 0}, {'wait': True}, {'wait': '30'}, {}]:
            request, wait = Mock(return_value=reply), Mock()
            self.assertIsNone(bridge.hook(DATA, request, wait=wait))
            self.assertEqual(request.call_count, 1)
            wait.assert_not_called()

    def test_hook_uses_deadline_and_caps_untrusted_server_renewals(self):
        for seconds, cap in [(5, 5), (47, 47), (999, 590)]:
            now = [0]
            def wait(delay):
                now[0] += delay
            def request(body):
                if body['action'] == 'register':
                    return {'id': 'ask', 'wait': seconds, 'expires': seconds}
                return {'state': 'pending', 'expires': seconds}
            self.assertIsNone(bridge.hook(DATA, request, wait=wait, clock=lambda: now[0], wall_clock=lambda: now[0]))
            self.assertEqual(now[0], cap)

    def test_invalid_or_late_expiry_never_allows(self):
        for expires in [None, True, '60', float('inf'), float('nan'), 0]:
            request = Mock(side_effect=[{'id': 'ask', 'wait': 30, 'expires': expires}, {}])
            self.assertIsNone(bridge.hook(DATA, request, wait=Mock(), clock=lambda: 0, wall_clock=lambda: 0))
            self.assertEqual(request.call_args.args[0]['action'], 'retire')
        for result in [{'state': 'pending'}, {'state': 'answered', 'answers': ANSWERS, 'expires': 0}]:
            request = Mock(side_effect=[{'id': 'ask', 'wait': 30, 'expires': 30}, result, {}])
            self.assertIsNone(bridge.hook(DATA, request, wait=Mock(), clock=lambda: 0, wall_clock=lambda: 0))
            self.assertEqual(request.call_args.args[0]['action'], 'retire')

    def test_poll_that_finishes_after_expiry_never_allows(self):
        now = [0]
        def request(body):
            if body['action'] == 'register':
                return {'id': 'ask', 'wait': 30, 'expires': 30}
            now[0] = 31
            return {'state': 'answered', 'answers': ANSWERS, 'expires': 30}
        self.assertIsNone(bridge.hook(DATA, request, clock=lambda: now[0], wall_clock=lambda: now[0]))

    def test_server_down_subprocess_falls_through_fast_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / 'config'
            config.mkdir()
            (config / 'token').write_text('isolated-test-token')
            state = home / 'install.json'
            # A closed kernel-assigned port, never a production port.
            import socket
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            state.write_text(json.dumps({'options': {'ask_bridge': True, 'port': port}}))
            env = dict(os.environ, HOME=folder, CLAUDE_CONFIG_DIR=str(home / 'claude'), CODEX_HOME=str(home / 'codex'),
                       MOBILE_TMUX_HOME=folder, MOBILE_TMUX_CONFIG=str(config), TETHER_INSTALL_STATE=str(state))
            env.pop('MOBILE_TMUX_PORT', None)
            env.pop('TMUX', None)  # the hook reads the client socket; keep it out of these tests
            before = {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()}
            start = time.monotonic()
            result = subprocess.run([sys.executable, str(Path(bridge.__file__))], input=json.dumps(DATA),
                                    capture_output=True, text=True, env=env, timeout=5)
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            self.assertEqual(before, {p.relative_to(home): p.read_bytes() for p in home.rglob('*') if p.is_file()})


class RouteTests(unittest.TestCase):
    setUp = StoreTests.setUp
    # Reuse the isolated record for the real server handler, never a terminal.
    def post(self, body, path=bridge.ANSWER_PATH, bridge_enabled=True, **headers):
        value = handler(path, body)
        value.client_address = ('127.0.0.1', 1234)
        value.headers.update(headers)
        with patch.object(bridge, 'enabled', return_value=bridge_enabled), patch.object(bridge, 'pending', self.store), \
                patch.object(server, 'choose', return_value=ENTRY), patch.object(bridge, 'verify_entry', return_value='') as verify:
            value.do_POST()
        return value.send_response.call_args.args[0], json.loads(value.wfile.getvalue()), verify

    def test_route_second_answer_409_duplicate_200(self):
        self.assertEqual(self.post(self.answer)[0], 200)
        self.assertEqual(self.post(self.answer)[0], 200)
        self.assertEqual(self.post(dict(self.answer, requestId='tap-2'))[0], 409)
        self.assertEqual(self.post(dict(self.answer, answers={}))[0], 409)

    def test_engage_route_extends_rate_limits_and_rejects_stale_requests(self):
        status, body, _ = self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH)
        self.assertEqual((status, body), (200, {'expires': self.now + 60}))
        self.now += .5
        self.assertEqual(self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH)[:2], (status, body))
        self.assertEqual(self.post(dict(self.ask, session='%9', nonce='wrong'), bridge.ENGAGE_PATH)[0], 409)
        self.assertEqual(self.post(dict(self.ask, session='%9', id='old'), bridge.ENGAGE_PATH)[0], 409)
        self.assertEqual(self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH, bridge_enabled=False)[0], 409)
        self.assertEqual(self.post(self.answer)[0], 200)
        self.assertEqual(self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH)[0], 409)

    def test_engage_route_rejects_expired_or_handed_off_record(self):
        self.assertEqual(self.post(dict(self.answer, answers=None))[0], 200)
        self.assertEqual(self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH)[0], 409)
        self.now += 30
        self.assertEqual(self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH)[0], 409)

    def test_engage_route_cannot_revive_an_expired_pending_record(self):
        self.streams.add('%9')
        self.now += 30
        self.store.records[self.ask['id']]['lease'] = self.now + 5
        self.assertEqual(self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH)[0], 409)

    def test_engage_uses_answer_auth_origin_and_write_checks(self):
        value = handler(bridge.ENGAGE_PATH, dict(self.ask, session='%9'))
        value.deny.return_value = True
        with patch.object(bridge, 'serve') as serve:
            value.do_POST()
        serve.assert_not_called()
        for headers in [{'Origin': 'https://evil.test'}, {'X-Codex-Mobile': '0'}]:
            status, _, verify = self.post(dict(self.ask, session='%9'), bridge.ENGAGE_PATH, **headers)
            self.assertEqual(status, 403)
            verify.assert_not_called()

    def test_auth_origin_and_write_header_precede_bridge(self):
        value = handler(bridge.ANSWER_PATH, self.answer)
        value.deny.return_value = True
        with patch.object(bridge, 'serve') as serve:
            value.do_POST()
        serve.assert_not_called()
        for headers in [{'Origin': 'https://evil.test'}, {'X-Codex-Mobile': '0'}]:
            status, _, verify = self.post(self.answer, **headers)
            self.assertEqual(status, 403)
            verify.assert_not_called()
        self.assertEqual(self.post(dict(self.ask, session='%9', action='poll'), bridge.HOOK_PATH)[0], 403)

    def test_expired_and_stale_identity_are_409(self):
        self.now += bridge.WAIT_SECONDS
        self.assertEqual(self.post(self.answer)[0], 409)
        with patch.object(bridge, 'verify_entry', side_effect=RuntimeError('stale')):
            value = handler(bridge.ANSWER_PATH, self.answer)
            with patch.object(bridge, 'enabled', return_value=True), patch.object(server, 'choose', return_value=ENTRY):
                value.do_POST()
            self.assertEqual(value.send_response.call_args.args[0], 409)

    def test_internal_route_requires_token_and_matching_socket(self):
        body = dict(self.registration, action='register', socket='wrong-socket')
        value = handler(bridge.HOOK_PATH, body)
        value.client_address = ('127.0.0.1', 1)
        value.headers['X-Tether-Hook'] = 'credential'
        with patch.object(bridge, 'enabled', return_value=True), patch.object(bridge, 'pending', self.store), \
                patch.object(server, 'choose', return_value=ENTRY), \
                patch.object(bridge, 'verify_entry', return_value='/private/socket'), \
                patch.object(auth, 'session_value', return_value='credential'):
            value.do_POST()
        self.assertEqual(value.send_response.call_args.args[0], 409)

    def test_bad_nonce_types_return_400(self):
        self.assertEqual(self.post(dict(self.answer, id=[]))[0], 400)
        self.assertEqual(self.post(dict(self.answer, nonce={}))[0], 400)


class WatchPollTests(unittest.TestCase):
    def test_only_visible_chat_polls_count_for_the_selected_pane(self):
        store = bridge.Pending(monotonic=lambda: 100, watching=server.ask_watching)
        registration = dict(session_id=ENTRY['thread'], nonce='nonce-12345678901234567890',
                            tool_use_id='tool-1', tool_input=DATA['tool_input'], pid=456)
        with patch.dict(server.WATCHED, clear=True), patch.dict(server.WATCHING, clear=True), \
                own_time(server, monotonic=Mock(return_value=100)), \
                patch.object(server, 'choose', return_value=ENTRY), patch.object(server, 'chat_payload', return_value={}), \
                patch.object(bridge, 'hook_process', return_value='start'), patch.object(bridge, 'install_settings', return_value={}):
            for suffix in ['', '&watch=0', '&preview=1']:
                value = handler('/mobile/api/chat?session=%259' + suffix, method='GET')
                value.do_GET()
                self.assertEqual(value.send_response.call_args.args[0], 200)
                self.assertEqual(store.register(ENTRY, registration), {'wait': 0})
            value = handler('/mobile/api/chat?session=%259&watch=1', method='GET')
            value.do_GET()
            self.assertTrue(server.ask_watching('%9', bridge.WATCH_WINDOW))
            self.assertEqual(store.register(ENTRY, registration)['wait'], 30)
            self.assertEqual(store.register(dict(ENTRY, id='%10'), registration), {'wait': 0})


class HttpRoundtripTests(unittest.TestCase):
    def test_real_hook_process_registers_and_receives_route_answer(self):
        self.run_hook_process(watched=True)

    def test_no_phone_hook_exits_under_300ms_without_polling_or_output(self):
        self.run_hook_process(watched=False)

    def run_hook_process(self, watched):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            token = home / 'token'
            token.write_text('isolated-test-token')
            state = home / 'install.json'
            store = bridge.Pending(watching=server.ask_watching)
            entry = dict(ENTRY, owner_pid=os.getpid(), process_start='mocked-owner')
            class QuietHandler(server.Handler):
                def log_message(self, *_):
                    pass
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
            worker = threading.Thread(target=httpd.serve_forever)
            state.write_text(json.dumps({'options': {'ask_bridge': True, 'port': httpd.server_port}}))
            env = dict(os.environ, HOME=folder, CLAUDE_CONFIG_DIR=str(home / 'claude'), CODEX_HOME=str(home / 'codex'),
                       MOBILE_TMUX_HOME=folder, MOBILE_TMUX_CONFIG=folder, TETHER_INSTALL_STATE=str(state), TMUX_PANE='%9')
            env.pop('MOBILE_TMUX_PORT', None)
            env.pop('TMUX', None)  # verify_entry is mocked to the empty socket
            process = None
            with patch.object(bridge, 'pending', store), patch.object(bridge, 'enabled', return_value=True), \
                    patch.object(server, 'choose', return_value=entry), patch.object(bridge, 'verify_entry', return_value=''), \
                    patch.object(auth, 'TOKEN_FILE', token), patch.object(auth, 'MODE', 'token'), \
                    patch.dict(server.WATCHED, {'%9': time.monotonic()} if watched else {}, clear=True), \
                    patch.dict(server.WATCHING, clear=True), \
                    patch.object(store, 'register', wraps=store.register) as register, \
                    patch.object(store, 'poll', wraps=store.poll) as poll:
                worker.start()
                try:
                    if not watched:
                        # Python startup and imports slow down under full-suite load. Time them here and
                        # bound only the hook's own work, so the check fails on a wait, not on CPU load.
                        load = f'import sys; sys.path.insert(0, {str(Path(bridge.__file__).parent)!r}); import ask_bridge'
                        startup = time.monotonic()
                        subprocess.run([sys.executable, '-c', load], env=env, check=True, capture_output=True, timeout=3)
                        startup = time.monotonic() - startup
                    started = time.monotonic()
                    process = subprocess.Popen([sys.executable, str(Path(bridge.__file__))], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                    process.stdin.write(json.dumps(DATA))
                    process.stdin.close()
                    process.stdin = None
                    if not watched:
                        stdout, stderr = process.communicate(timeout=3)
                        elapsed = time.monotonic() - started
                        self.assertLess(elapsed - startup, .3)
                        self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                        self.assertEqual(register.call_count, 1)
                        poll.assert_not_called()
                        self.assertEqual(store.records, {})
                        self.assertEqual(set(p.name for p in home.iterdir()), {'token', 'install.json'})
                        return
                    deadline = time.monotonic() + 5
                    ask = None
                    while time.monotonic() < deadline:
                        ask = store.attention(entry)
                        if ask:
                            break
                        time.sleep(.02)
                    self.assertIsNotNone(ask)
                    connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                    try:
                        url = f'http://127.0.0.1:{httpd.server_port}'
                        headers = {'Origin': url, 'X-Codex-Mobile': '1',
                                   'Cookie': auth.COOKIE + '=' + auth.session_value()}
                        engage = dict(session='%9', id=ask['id'], nonce=ask['nonce'])
                        for changed, expected in [({'Cookie': ''}, 401),
                                                  ({'Origin': 'https://evil.test'}, 403),
                                                  ({'X-Codex-Mobile': '0'}, 403), ({}, 200)]:
                            connection.request('POST', bridge.ENGAGE_PATH, json.dumps(engage),
                                               headers=dict(headers, **changed))
                            response = connection.getresponse()
                            self.assertEqual(response.status, expected)
                            body = json.loads(response.read())
                            if expected == 200:
                                self.assertEqual(set(body), {'expires'})
                                self.assertGreater(body['expires'], ask['expires'])
                                self.assertEqual(store.attention(entry)['expires'], body['expires'])
                        connection.request('POST', bridge.ANSWER_PATH, json.dumps(dict(session='%9', id=ask['id'], nonce=ask['nonce'],
                            answers=ANSWERS, requestId='http-tap')), headers=headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read()), {'status': 'submitted'})
                    finally:
                        connection.close()
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual((process.returncode, stderr), (0, ''))
                    self.assertEqual(json.loads(stdout)['hookSpecificOutput']['updatedInput']['answers']['Which features?'], 'Search, Export')
                    self.assertIsNone(store.attention(entry))
                    self.assertEqual(set(p.name for p in home.iterdir()), {'token', 'install.json'})
                finally:
                    if process and process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)
                    httpd.shutdown()
                    httpd.server_close()
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
