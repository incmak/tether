import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import login_flow as login
import server

FIXTURES = Path(__file__).parent / 'test_fixtures/login'


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.runner = login.LoginRunner()
        self.screen = (FIXTURES / 'claude.txt').read_text()
        self.dead = '0:'
        self.calls = []
        self.thread = patch.object(login.threading, 'Thread').start()
        self.run = patch.object(login.subprocess, 'run', side_effect=self.tmux).start()
        self.addCleanup(patch.stopall)

    def tmux(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertEqual(argv[1:5], ['-L', self.runner.socket, '-f', '/dev/null'])
        cmd = argv[5]
        output = '$1' if cmd == 'new-session' else self.screen if cmd == 'capture-pane' else self.dead if cmd == 'display-message' else ''
        return subprocess.CompletedProcess(argv, 0, stdout=output)

    def test_fixture_screens(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                parsed = login.parse_screen(agent, '\x1b[32m' + (FIXTURES / (agent + '.txt')).read_text() + '\x1b[0m')
                self.assertTrue(parsed['url'].startswith('https://'))
                self.assertEqual(parsed['user_code'], 'ABCD-1234' if agent == 'codex' else None)
        self.assertIsNone(login.parse_screen('codex', 'token: secret\n1234-5678-not-a-code')['user_code'])
        self.assertEqual(login.parse_screen('codex', '2. Enter this one-time code after you are signed in (expires in 15 minutes)\n   K7QX-P2MZ9')['user_code'], 'K7QX-P2MZ9')
        self.assertEqual(login.parse_screen('codex', '2. Enter this one-time\n code (expires in 15\n minutes)\n   ABCD-1234')['user_code'], 'ABCD-1234')

    def test_claude_cli_2_1_link_on_claude_com(self):
        # Claude Code 2.1.316 prints its sign-in link on claude.com. Tether waited for a link forever.
        screen = "Opening browser to sign in\u2026\nIf the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=x\nPaste code here if prompted >"
        self.assertEqual(login.parse_screen('claude', screen)['url'], 'https://claude.com/cai/oauth/authorize?code=true&state=x')
        self.assertIsNone(login.parse_screen('claude', 'https://claude.com.evil.test/x')['url'])

    def test_wrapped_url_keeps_every_character(self):
        url = 'https://claude.com/cai/oauth/authorize?code=true&state=' + 'x' * 450
        screen = '\n'.join(url[i:i + 50] for i in range(0, len(url), 50)) + '\n\nPaste code here if prompted >'
        self.assertEqual(login.parse_screen('claude', screen)['url'], url)

    def test_failed_code_reports_failure_and_closes_its_session(self):
        flow = self.start()
        self.runner.code('claude', 'not-a-real-code')
        self.screen += '\nInvalid code. Please try again.\nPaste code here if prompted >'
        self.runner.update('claude', flow)
        self.assertEqual(self.runner.state('claude')['phase'], 'failed')
        self.assertIsNone(flow['session'])
        self.assertIn('failed', self.runner.state('claude')['message'].lower())

    def test_vendor_hosts_and_url_validation(self):
        for agent, hosts in login.HOSTS.items():
            for host in hosts:
                self.assertEqual(login.parse_screen(agent, f'https://{host}/login')['url'], f'https://{host}/login')
        for url in ('javascript:alert(1)', 'https://evil.test/', 'https://claude.ai.evil.test', 'https://claude.ai@evil.test',
                    'https://evil@claude.ai/', 'https://claude.ai:1234/', 'https://claude.ai:bad/', 'https://auth.openai.com/codex'):
            self.assertIsNone(login.parse_screen('claude', url)['url'], url)
        self.assertIsNone(login.parse_screen('codex', 'https://claude.ai/oauth')['url'])

    def test_commands_and_validation(self):
        for method in ('claudeai', 'console', 'sso'):
            self.assertEqual(login.command('claude', method, 'a@example.com'), ['claude', 'auth', 'login', '--' + method, '--email', 'a@example.com'])
        self.assertEqual(login.command('codex'), ['codex', 'login', '--device-auth'])
        for args in ((None,), ([],), ('shell',), ('claude', '--evil'), ('claude', 'sso', 'a\nb'), ('codex', 'sso')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.runner.start(*args)
        self.assertFalse(self.calls)
        for code in ('', ' ', None, 123, 'a\nb', 'a\rb', 'a\x00b', 'a\x1bb', 'a\x7fb', 'a\x85b', 'a' * 2001):
            with self.subTest(code=repr(code)), self.assertRaises(ValueError):
                self.runner.code('claude', code)
        self.assertFalse(self.calls)
        self.assertEqual(login.plain('a' * 2000, 2000, 'Code'), 'a' * 2000)

    def start(self, agent='claude'):
        self.runner.start(agent)
        flow = self.runner.flows[agent]
        self.runner.update(agent, flow)
        return flow

    def test_only_owned_session_receives_literal_code_and_cancel(self):
        flow = self.start()
        self.assertEqual(self.runner.state('claude')['phase'], 'waiting_code')
        self.runner.code('claude', 'value; $(literal)')
        sends = [argv for argv, _ in self.calls if argv[5] == 'send-keys']
        self.assertEqual([argv[6:] for argv in sends], [['-t', '$1:0.0', '-l', '--', 'value; $(literal)'], ['-t', '$1:0.0', 'Enter']])
        self.assertNotIn('value;', repr(flow))
        self.assertNotIn('value;', repr(self.runner.state('claude')))
        self.assertEqual(self.runner.state('claude')['phase'], 'waiting_browser')
        self.runner.cancel('claude')
        self.assertIsNone(flow['session'])
        self.assertEqual(self.calls[-1][0][5:], ['kill-session', '-t', '$1'])
        with self.assertRaises(ValueError):
            self.runner.code('claude', 'another')
        self.assertEqual(self.runner.cancel('codex')['phase'], 'idle')

    def test_no_code_for_codex_or_before_link(self):
        self.screen = 'Starting…'
        self.start()
        with self.assertRaises(ValueError):
            self.runner.code('claude', 'valid')
        self.runner.start('codex')
        with self.assertRaises(ValueError):
            self.runner.code('codex', 'valid')
        self.assertFalse(any(argv[5] == 'send-keys' for argv, _ in self.calls))

    def test_duplicate_start_does_not_kill_any_session(self):
        self.start()
        with self.assertRaises(ValueError):
            self.runner.start('claude')
        self.assertFalse(any(argv[5] == 'kill-session' for argv, _ in self.calls))
        self.assertEqual(self.calls[0][0][5:11], ['new-session', '-d', '-P', '-F', '#{session_id}', '-s'])
        self.assertIn('remain-on-exit', self.calls[0][0][-1])

    def test_success_requires_zero_exit_then_reads_account(self):
        flow = self.start()
        self.screen += '\nSuccessfully logged in\naccess_token: secret'
        account = {'email': 'new@example.com', 'plan': 'Pro'}
        with patch.object(login.accounts, 'claude', return_value=account) as read:
            self.runner.update('claude', flow)
            read.assert_not_called()
            self.dead = '1:0'
            self.runner.update('claude', flow)
            read.assert_called_once_with()
        self.assertEqual(self.runner.state('claude'), dict(login.empty('done', 'Signed in.'), account=account))
        self.assertIsNone(flow['session'])

    def test_failure_and_timeout_cleanup(self):
        for dead in ('1:1', '0:'):
            with self.subTest(dead=dead):
                self.dead = '0:'
                flow = self.start()
                self.dead = dead
                if dead == '0:':
                    flow['deadline'] = 0
                self.runner.update('claude', flow)
                self.assertEqual(self.runner.state('claude')['phase'], 'failed')
                self.assertIsNone(flow['session'])
                self.assertEqual(self.calls[-1][0][5:], ['kill-session', '-t', '$1'])

    def test_cancel_missing_session_allows_a_new_login(self):
        self.start()
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr="can't find session: $1")
        self.run.side_effect = None
        self.assertEqual(self.runner.cancel('claude')['phase'], 'idle')
        self.run.side_effect = self.tmux
        self.assertEqual(self.runner.start('claude')['phase'], 'waiting_browser')

    def test_watcher_cleans_up_without_state_polling(self):
        flow = self.start()
        flow['deadline'] = 0
        self.runner.watch('claude', flow)
        self.assertIsNone(flow['session'])
        self.assertEqual(self.runner.state('claude')['phase'], 'failed')

    def test_tmux_errors_do_not_leak_output_or_code(self):
        self.start()
        self.run.side_effect = subprocess.CalledProcessError(1, ['secret-code'], output='access_token=secret')
        with self.assertRaisesRegex(RuntimeError, '^Could not reach the login session.$'):
            self.runner.code('claude', 'secret-code')


class LoginRouteTests(unittest.TestCase):
    def handler(self, action, body):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/mobile/api/login/' + action
        raw = json.dumps(body).encode()
        handler.headers = {'Content-Length': str(len(raw)), 'Origin': 'http://localhost', 'Host': 'localhost', 'X-Codex-Mobile': '1'}
        handler.rfile = io.BytesIO(raw)
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        return handler

    def test_post_allowlist_dispatch_and_validation_messages(self):
        for action in ('start', 'code', 'cancel'):
            with self.subTest(action=action), patch.object(login.runner, action, return_value=login.empty()) as method:
                handler = self.handler(action, {'agent': 'claude', 'code': 'example'})
                handler.do_POST()
                method.assert_called_once()
                handler.respond.assert_called_once_with(200, login.empty())
            with patch.object(login.runner, action, side_effect=ValueError('Specific login error.')):
                handler = self.handler(action, {'agent': 'claude'})
                handler.do_POST()
                handler.respond.assert_called_once_with(400, {'error': 'Specific login error.'})

    def test_routes_require_auth_and_same_origin(self):
        for action in ('start', 'code', 'cancel'):
            with patch.object(login.runner, action) as method:
                handler = self.handler(action, {'agent': 'codex'})
                handler.headers['Origin'] = 'http://evil.test'
                handler.do_POST()
                handler.respond.assert_called_once_with(403, {'error': 'Reload the page and try again.'})
                handler = self.handler(action, {'agent': 'codex'})
                handler.deny.return_value = True
                handler.do_POST()
                method.assert_not_called()

    def test_state_and_bad_agent(self):
        handler = self.handler('state?agent=codex', {})
        handler.do_GET()
        handler.respond.assert_called_once_with(200, login.empty())
        handler = self.handler('state?agent=shell', {})
        handler.do_GET()
        handler.respond.assert_called_once_with(400, {'error': 'Select Claude or Codex.'})

    def test_account_reads_are_passive_and_refresh_requires_a_protected_post(self):
        cached = {'available': True, 'claude': [], 'codex': []}
        with patch.object(server.saved_accounts, 'snapshot', return_value=cached) as snapshot, patch.object(server.saved_accounts, 'listing', return_value=cached) as listing:
            handler = self.handler('state', {})
            handler.path = '/mobile/api/accounts'
            handler.do_GET()
            snapshot.assert_called_once_with()
            listing.assert_not_called()
            handler.respond.assert_called_once_with(200, cached)
            handler = self.handler('state', {})
            handler.path = '/mobile/api/accounts/refresh'
            handler.do_POST()
            listing.assert_called_once_with()
            handler.respond.assert_called_once_with(200, cached)
            for missing in ('Origin', 'X-Codex-Mobile'):
                listing.reset_mock()
                handler = self.handler('state', {})
                handler.path = '/mobile/api/accounts/refresh'
                handler.headers.pop(missing)
                handler.do_POST()
                listing.assert_not_called()
                handler.respond.assert_called_once_with(403, {'error': 'Reload the page and try again.'})

    def test_passive_usage_requests_cached_reports(self):
        with patch.object(server, 'usage_summary', return_value={}) as summary:
            handler = self.handler('state', {})
            handler.path = '/mobile/api/usage'
            handler.do_GET()
            summary.assert_called_once_with(live_age=float('inf'))


if __name__ == '__main__':
    unittest.main()
