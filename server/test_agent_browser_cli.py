"""The CLI uses a local HTTP stub and never writes to real agent panes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import unittest
from unittest import mock

import agent_browser_cli


class NoteTests(unittest.TestCase):
    def test_note_explains_ended_signed_access(self):
        text = agent_browser_cli.note()
        self.assertIn('signed CDP URL returns 410', text)
        self.assertIn('socket closes with "Access ended"', text)
        self.assertIn('tell Mak the reason in chat', text)
        self.assertIn('Ask again only if the task still needs access.', text)

    def test_note_contains_absolute_ask_command(self):
        text = agent_browser_cli.note()
        command = text.rsplit('`', 2)[1]
        self.assertEqual(shlex.split(command),
                         [sys.executable, str(Path(agent_browser_cli.__file__).resolve()), 'ask', '<what you need>'])
        self.assertEqual(len(command.splitlines()), 1)
        self.assertTrue(Path(shlex.split(command)[1]).is_absolute())

    def test_note_quotes_runtime_paths(self):
        script = Path("/install path/user's tether/server/agent_browser_cli.py")
        executable = "/python path/user's python"
        with mock.patch.object(agent_browser_cli, '__file__', str(script)), \
                mock.patch.object(agent_browser_cli.sys, 'executable', executable):
            command = agent_browser_cli.note().rsplit('`', 2)[1]
        self.assertEqual(shlex.split(command), [executable, str(script), 'ask', '<what you need>'])
        self.assertEqual(len(command.splitlines()), 1)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.code = 200
        self.result = {'id': 'ask-id'}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else None
                test.requests.append((self.command, self.path, body, dict(self.headers)))
                self.send_response(test.code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(test.result).encode())

            do_POST = do_GET

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f'ws://127.0.0.1:{self.server.server_port}/cdp/test-token'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def run_cli(self, *args, **env):
        return subprocess.run([sys.executable, str(Path(agent_browser_cli.__file__)), *args], capture_output=True,
                              text=True, timeout=7, env=dict(os.environ, TETHER_CDP_URL=self.url, TMUX_PANE='', **env))

    def test_ask_preserves_text_and_only_valid_panes(self):
        for pane in ('', '%12', '%１２', '%1\n', 'invalid'):
            env = dict(os.environ, TETHER_CDP_URL=self.url, TMUX_PANE=pane)
            run = subprocess.run([sys.executable, agent_browser_cli.__file__, 'ask', 'Please enter the 2FA code.'],
                                 capture_output=True, text=True, timeout=7, env=env)
            self.assertEqual((run.returncode, run.stdout, run.stderr),
                             (0, 'Asked the user. Wait for their reply in chat.\n', ''))
            method, path, body, headers = self.requests[-1]
            self.assertEqual((method, path), ('POST', '/ask/test-token'))
            self.assertEqual(body, {'text': 'Please enter the 2FA code.', **({'pane': pane} if pane == '%12' else {})})
            self.assertEqual(headers['Content-Type'], 'application/json')
            self.assertNotIn('Origin', headers)

    def test_status_on_and_off(self):
        for code, result, output, exit_code in ((200, {'ok': True}, 'on\n', 0),
                                               (503, {'ok': False, 'reason': 'disabled'}, 'off: disabled\n', 1)):
            self.code, self.result = code, result
            run = self.run_cli('status')
            self.assertEqual((run.returncode, run.stdout, run.stderr), (exit_code, output, ''))
            self.assertEqual(self.requests[-1][:3], ('GET', '/health/test-token', None))
            self.assertNotIn('Origin', self.requests[-1][3])

    def test_missing_env(self):
        self.url = ''
        run = self.run_cli('ask', 'help')
        self.assertEqual((run.returncode, run.stdout, run.stderr),
                         (1, 'Tether shared browser is not available in this shell.\n', ''))
        self.assertEqual(self.requests, [])

    def test_server_down_and_bad_response_are_one_line_errors(self):
        self.code = 403
        for args in (('ask', 'help'), ('status',)):
            run = self.run_cli(*args)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(len(run.stdout.splitlines()), 1)
            self.assertEqual(run.stderr, '')
            self.assertNotIn('test-token', run.stdout)
        self.code, self.result = 200, []
        self.assertEqual(self.run_cli('ask', 'help').returncode, 1)
        self.server.server_close()
        self.assertEqual(self.run_cli('ask', 'help').returncode, 1)
        self.assertEqual(self.run_cli('status').returncode, 1)

    def test_endpoint_forms_and_invalid_input(self):
        self.assertEqual(agent_browser_cli.endpoint('http://127.0.0.1:123/cdp/x', 'ask'), 'http://127.0.0.1:123/ask/x')
        self.assertEqual(agent_browser_cli.endpoint('wss://example.test:123/cdp/x', 'health'), 'https://example.test:123/health/x')
        for value in ('bad', 'ws://127.0.0.1:1/wrong/x', 'ws://127.0.0.1:1/cdp/x?secret=x'):
            with self.assertRaises(ValueError):
                agent_browser_cli.endpoint(value, 'ask')
        for args in ((), ('ask',), ('ask', ''), ('unknown',)):
            self.assertEqual(self.run_cli(*args).returncode, 1)

    def test_requests_use_five_second_timeout(self):
        with mock.patch.dict(os.environ, TETHER_CDP_URL=self.url), \
                mock.patch.object(agent_browser_cli, 'urlopen', side_effect=TimeoutError) as request, \
                mock.patch('builtins.print') as output:
            self.assertEqual(agent_browser_cli.main(['status']), 1)
            self.assertEqual(request.call_args.kwargs, {'timeout': 5})
            output.assert_called_once_with('off: shared browser request failed')

    def test_signed_allow_deny_timeout_and_flags(self):
        grant = {'ok': True, 'cdp': 'ws://127.0.0.1:123/cdp-signed/approved-token', 'tab': 'tab-id', 'expires': 1800000000}
        for result, expected in ((grant, 0), ({'ok': False, 'reason': 'denied'}, 1),
                                 ({'ok': False, 'reason': 'timeout'}, 1)):
            self.result = result
            run = self.run_cli('ask-signed', 'Read the task account', '--minutes', '10', '--url', 'https://example.test')
            self.assertEqual((run.returncode, run.stderr), (expected, ''))
            self.assertEqual(json.loads(run.stdout), result)
            self.assertEqual(len(run.stdout.splitlines()), 1)
            self.assertEqual(self.requests[-1][:3], ('POST', '/ask-signed/test-token',
                             {'text': 'Read the task account', 'minutes': 10, 'url': 'https://example.test', 'browser': 'auto'}))
        self.result = grant
        run = subprocess.run([sys.executable, agent_browser_cli.__file__, 'ask-signed', 'Task'], capture_output=True,
                             text=True, timeout=7, env=dict(os.environ, TETHER_CDP_URL=self.url, TMUX_PANE='%12'))
        self.assertEqual(run.returncode, 0)
        self.assertEqual(self.requests[-1][2], {'text': 'Task', 'minutes': 15, 'pane': '%12', 'browser': 'auto'})
        self.run_cli('ask-signed', 'Task', '--browser', 'mac')
        self.assertEqual(self.requests[-1][2]['browser'], 'chrome')
        self.run_cli('ask-signed', 'Task', '--browser', 'chrome')
        self.assertEqual(self.requests[-1][2]['browser'], 'chrome')
        self.run_cli('ask-signed', 'Task', '--browser', 'server')
        self.assertEqual(self.requests[-1][2]['browser'], 'server')

    def test_signed_wait_timeout_validation_and_redacted_errors(self):
        with mock.patch.dict(os.environ, TETHER_CDP_URL=self.url), \
                mock.patch.object(agent_browser_cli, 'urlopen', side_effect=TimeoutError) as request, \
                mock.patch('builtins.print') as output:
            self.assertEqual(agent_browser_cli.main(['ask-signed', 'Task']), 1)
            self.assertEqual(request.call_args.kwargs, {'timeout': 125})
            self.assertEqual(json.loads(output.call_args.args[0]), {'ok': False, 'reason': 'timeout'})
        before = len(self.requests)
        for args in ((), ('Task', '--minutes', '61'), ('Task', '--minutes', '0'), ('',), ('Task', '--unknown')):
            run = self.run_cli('ask-signed', *args)
            self.assertEqual((run.returncode, run.stderr), (1, ''))
            self.assertEqual(json.loads(run.stdout), {'ok': False, 'reason': 'request failed'})
            self.assertNotIn('test-token', run.stdout)
        self.assertEqual(len(self.requests), before)


if __name__ == '__main__':
    unittest.main()
