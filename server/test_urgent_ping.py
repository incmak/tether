from collections import deque
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.parse
from unittest import mock

import paths
import urgent_ping

REAL_NAME = urgent_ping._name


class UrgentPingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)
        for patcher in (mock.patch.object(paths, 'CONFIG', self.config),
                        mock.patch.object(urgent_ping, '_PANES', {}),
                        mock.patch.object(urgent_ping, '_SENT', deque()),
                        mock.patch.object(urgent_ping, '_ALERTS', deque()),
                        mock.patch.object(urgent_ping, '_name', lambda pane: '')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = 1_000_000
        self.runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stderr=b''))
        self.workers = []
        thread = threading.Thread
        def make_thread(*args, **kwargs):
            worker = thread(*args, **kwargs)
            self.workers.append(worker)
            return worker
        patcher = mock.patch.object(urgent_ping.threading, 'Thread', side_effect=make_thread)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.join_workers)

    def join_workers(self):
        for worker in self.workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def write_config(self, config=None):
        config = {'command': ['ping-tool', '{title}', '{question}', '{url}', '{pane}']} if config is None else config
        (self.config / 'urgent-ping.json').write_text(json.dumps(config))

    def send(self, pane='%1', **extra):
        result = urgent_ping.send(dict(pane=pane, next='Choose?', **extra), clock=lambda: self.now, runner=self.runner)
        self.join_workers()
        return result

    def test_security_alert_ignores_ping_off_and_has_its_own_limit(self):
        # Ping-off needs no passkey, so an agent could switch it off before it adds the first passkey.
        alert = lambda: urgent_ping.alert('First passkey added', clock=lambda: self.now, runner=self.runner)
        self.assertFalse(alert())  # No command set up: nothing to send.
        self.write_config({'enabled': False, 'command': ['ping-tool', '{title}', '{question}', '{pane}']})
        self.assertFalse(self.send())
        self.assertTrue(alert())
        self.join_workers()
        self.assertEqual(self.runner.call_args.args[0], ['ping-tool', 'Tether: Passkeys', 'First passkey added', ''])
        self.write_config()
        for pane in range(6):  # Routine pings fill their own limit and the empty-pane slot ...
            self.assertTrue(self.send('' if pane == 0 else f'%{pane}'))
        self.assertFalse(self.send('%9'))
        self.assertTrue(alert())  # ... but a security alert still goes out.
        self.assertTrue(alert())  # And a second event within ten minutes is not merged into the first.
        for _ in range(7):
            alert()
        self.assertFalse(alert())  # Ten an hour at most.
        self.now += 3600
        self.assertTrue(alert())
        self.join_workers()

    def test_missing_and_bad_config_do_nothing(self):
        self.assertFalse(self.send())
        for config in (None, [], {}, {'command': 'shell command'}, {'command': []}, {'command': [2]},
                       {'command': ['']}, {'command': ['tool', None]}, {'command': ['tool\0']},
                       {'command': ['tool'], 'base_url': None}):
            with self.subTest(config=config):
                (self.config / 'urgent-ping.json').write_text(json.dumps(config))
                self.assertFalse(self.send())
        for raw in (b'not json', b'\xff'):
            (self.config / 'urgent-ping.json').write_bytes(raw)
            self.assertFalse(self.send())
        self.runner.assert_not_called()
        self.assertEqual(self.workers, [])
        self.assertFalse(urgent_ping._SENT)

    def test_placeholders_use_one_argv_with_no_shell(self):
        self.write_config({'command': ['ping-tool', '{title}', 'Q={question}', '{url}', '{pane}', '{unknown}'],
                           'base_url': 'https://tether.test///'})
        self.assertTrue(self.send('%12', name='Build tab'))
        self.runner.assert_called_once_with(
            ['ping-tool', 'Tether: Build tab', 'Q=Choose?', 'https://tether.test/chat/open/12', '%12', '{unknown}'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertTrue(self.workers[0].daemon)

    def test_without_base_url_or_name_uses_empty_url_and_pane_title(self):
        self.write_config()
        self.assertTrue(self.send())
        self.assertEqual(self.runner.call_args.args[0], ['ping-tool', 'Tether: %1', 'Choose?', '', '%1'])

    def test_title_uses_the_tab_name_from_tmux(self):
        self.write_config()
        with mock.patch.object(urgent_ping, '_name', lambda pane: 'Deploy fix'):
            self.assertTrue(self.send())
        self.assertEqual(self.runner.call_args.args[0][1], 'Tether: Deploy fix')

    def test_name_lookup_fails_open(self):
        with mock.patch.object(subprocess, 'run', side_effect=OSError('no tmux')):
            self.assertEqual(REAL_NAME('%1'), '')
        with mock.patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='Deploy fix\n')) as run:
            self.assertEqual(REAL_NAME('%1'), 'Deploy fix')
        self.assertIn('-t', run.call_args.args[0])
        self.assertEqual(REAL_NAME(''), '')

    def test_question_metacharacters_remain_literal(self):
        self.write_config()
        question = '`echo no` $(echo no); "quoted" {unknown}'
        self.assertTrue(urgent_ping.send({'pane': '%1', 'next': question}, runner=self.runner))
        self.join_workers()
        self.assertEqual(self.runner.call_args.args[0][2], question)

    def test_per_pane_limit_expires_at_ten_minutes(self):
        self.write_config()
        self.assertTrue(self.send())
        self.now += 599
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertFalse(self.send())
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertEqual(self.runner.call_count, 1)
        self.assertTrue(self.send('%2'))
        self.now += 1
        self.assertTrue(self.send())

    def test_global_limit_is_six_in_a_rolling_hour(self):
        self.write_config()
        self.assertTrue(self.send('%0'))
        self.now += 10
        for pane in range(1, 6):
            self.assertTrue(self.send(f'%{pane}'))
        self.now += 3590
        self.assertTrue(self.send('%6'))  # Only the first ping expired.
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertFalse(self.send('%7'))
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.now += 10
        self.assertTrue(self.send('%7'))
        self.assertEqual(self.runner.call_count, 8)

    def test_send_returns_while_the_daemon_runner_is_busy(self):
        self.write_config()
        started, finish = threading.Event(), threading.Event()
        def run(*args, **kwargs):
            started.set()
            finish.wait(timeout=2)
        try:
            self.assertTrue(urgent_ping.send({'pane': '%1'}, runner=run))
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(self.workers[0].is_alive())
        finally:
            finish.set()

    def test_runner_errors_do_not_escape_the_worker(self):
        self.write_config()
        for index, error in enumerate((OSError('missing executable'), subprocess.TimeoutExpired('tool', 60))):
            with self.subTest(error=error), mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                self.runner.side_effect = error
                self.assertTrue(self.send(f'%{index}'))
            self.assertIn('Urgent ping failed:', err.getvalue())

    def test_link_opens_the_tab_inside_the_installed_app_scope(self):
        # The PWA scope is /chat/ and the chat UI reads ?session=. A link outside the scope opens a browser tab instead.
        self.write_config({'command': ['ping-tool', '{url}'], 'base_url': 'https://tether.test'})
        manifest = json.loads((Path(urgent_ping.__file__).parent.parent / 'ui' / 'public' / 'manifest.webmanifest').read_text())
        # A tmux pane gets its own folder (/chat/open/N): Chrome on Android only brings a running app to the front
        # for a link in the open page's folder (/chat/?session=), so the app kept showing its last tab.
        for pane, path in (('%12', '/chat/open/12'), ('%308', '/chat/open/308')):
            with self.subTest(pane=pane):
                url = urllib.parse.urlsplit(urgent_ping._argv({'pane': pane})[1])
                self.assertEqual((url.path, url.query, url.fragment), (path, '', ''))
                self.assertTrue(url.path.startswith(manifest['scope']), url.path)
        # Any other id keeps the ?session= form, escaped.
        url = urllib.parse.urlsplit(urgent_ping._argv({'pane': '%1&x=#y'})[1])
        self.assertEqual((url.path, url.fragment), ('/chat/', ''))
        self.assertEqual(urllib.parse.parse_qs(url.query), {'session': ['%1&x=#y']})
        # An app that is already open must load the link's URL, or it shows the last tab.
        self.assertEqual(manifest.get('launch_handler'), {'client_mode': 'navigate-existing'})

    def test_test_ping_names_no_real_tab(self):
        # A real pane id (such as %0) would open someone else's tab from the test card.
        self.write_config({'command': ['ping-tool', '{title}', '{url}', '{pane}'], 'base_url': 'https://tether.test'})
        with mock.patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'', stderr=b'')) as run:
            self.assertTrue(urgent_ping.run_test()['ok'])
        self.assertEqual(run.call_args.args[0], ['ping-tool', 'Tether: Test', 'https://tether.test/chat/', ''])

    def test_cli_test_runs_in_foreground_and_prints_exit_code_and_stderr(self):
        self.write_config({'command': [sys.executable, '-c',
            'import sys; print(sys.argv[1], file=sys.stderr); print(repr(sys.argv[2]), file=sys.stderr); sys.exit(3)',
            '{question}', '{pane}']})
        result = subprocess.run([sys.executable, str(Path(urgent_ping.__file__)), 'test'],
                                env=dict(os.environ, MOBILE_TMUX_CONFIG=str(self.config)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 3)
        self.assertIn('Exit code: 3', result.stdout)
        self.assertIn('Stderr tail:\nTest ping from Tether\n\x27\x27', result.stdout)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
