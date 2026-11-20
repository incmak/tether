"""Signed browser isolation and lifecycle. Real tests use temporary profiles, never the live bridge."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import base64
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_cdp
import browser_display
from browser_display import VirtualDisplay
from test_browser import FakeChrome, FakeHandler


class SignedTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeChrome()
        self.display = Mock()
        self.display.start.return_value = {'DISPLAY': ':213', 'XAUTHORITY': '/fake/authority'}
        self.display.finder.return_value = '/fake/Xvfb'
        self.launcher = Mock(side_effect=lambda chrome, profile, **kwargs: self.fake.launcher(chrome, profile))
        self.signed = browser.Browser(launcher=self.launcher, chrome_finder=lambda: '/fake/google-chrome',
                                      profile=Path('/nonexistent/signed/profile'), signed=True, display=self.display)
        self.test = Mock(signed=False, container=None)
        self.test.list_tabs.return_value = {'tabs': []}
        self.test.new_tab.return_value = {'id': 'TEST'}
        self.test.running.return_value = False
        self.test.chrome_finder.return_value = '/fake/chrome'
        self.addCleanup(self.fake.close)
        self.addCleanup(self.signed.stop)

    def get(self, name='tabs', query='browser=signed'):
        handler = FakeHandler(browser_api.PREFIX + name + '?' + query)
        browser_api.get(handler, self.test, signed_browser=self.signed)
        return handler.replies[0]

    def post(self, name, **body):
        handler = FakeHandler(browser_api.PREFIX + name, body)
        browser_api.post(handler, self.test, signed_browser=self.signed)
        return handler.replies[0]

    def test_routes_dispatch_and_unchanged_default(self):
        status, state = self.get()
        self.assertEqual((status, state['browser']), (200, 'signed'))
        self.assertEqual(state['tabs'][0]['mode'], 'saved')
        self.test.list_tabs.assert_not_called()
        self.assertEqual(self.get(query='')[1]['tabs'], [])
        self.test.list_tabs.assert_called_once()
        self.assertEqual([(b['id'], b['available'], b['running']) for b in state['browsers']],
                         [('test', True, False), ('signed', True, True), ('chrome', False, False)])
        self.assertEqual(self.get(query='browser=other')[0], 400)
        self.assertEqual(self.post('new', browser=['signed'])[0], 400)
        self.assertEqual(self.post('new')[1], {'id': 'TEST'})
        self.assertEqual(self.post('new', browser='signed')[1]['mode'], 'saved')
        self.assertEqual(self.post('new', browser='signed', mode='private')[0], 400)
        self.launcher.assert_called_once_with('/fake/google-chrome', self.signed.profile,
                                              headed=True, env=self.display.start.return_value)
        self.assertEqual(self.fake.sent('Target.createBrowserContext'), [])
        self.assertEqual(self.fake.sent('Runtime.enable'), [])
        self.assertEqual({c['method'] for c in self.fake.commands if c['method'].endswith('.enable')}, {'Page.enable'})
        self.assertEqual(self.fake.sent('Emulation.setUserAgentOverride')[-1]['params'], {'userAgent': ''})

    def test_signed_defaults_never_use_test_profile_or_override(self):
        signed = browser.Browser(signed=True)
        self.assertEqual(signed.profile, browser.SIGNED_PROFILE)
        self.assertIs(signed.chrome_finder, browser.find_signed_chrome)
        with patch.dict(os.environ, MOBILE_TMUX_CHROME='/fake/agent-chrome'), \
                patch.object(browser.os, 'access', return_value=True):
            self.assertEqual(signed.chrome_finder(), '/usr/bin/google-chrome')

    def test_all_panel_writes_dispatch(self):
        target = self.get()[1]['tabs'][0]['id']
        for name, body in (
                ('navigate', {'url': 'example.com'}), ('history', {'action': 'reload'}),
                ('input', {'events': [{'type': 'text', 'text': 'test'}]}),
                ('device', {'preset': 'phone'}), ('close', {})):
            status, result = self.post(name, browser='signed', tab=target, **body)
            self.assertEqual((status, result['browser']), (200, 'signed'), name)
        for name in ('navigate', 'history', 'input', 'set_device', 'close_tab'):
            getattr(self.test, name).assert_not_called()
        self.assertTrue(all(t.mode == 'saved' for t in self.signed.tabs.values()))
        handler = FakeHandler(browser_api.PREFIX + 'new?browser=signed', {})
        browser_api.post(handler, self.test, signed_browser=self.signed)
        self.assertEqual(handler.replies[0][1]['browser'], 'signed')

    def test_stream_dispatch_and_frame_tag(self):
        handler = FakeHandler(browser_api.PREFIX + 'stream?browser=signed&tab=T1&w=600&h=700')
        with patch.object(browser_api, 'stream') as stream:
            browser_api.get(handler, self.test, signed_browser=self.signed)
        stream.assert_called_once_with(handler, self.signed, 'T1', 600, 700, 'signed')
        frame = (1, {'deviceWidth': 1280, 'deviceHeight': 800}, 'jpeg', 1)
        self.assertEqual(json.loads(browser.frame_event(frame, 'signed').decode().split('data: ')[1])['browser'], 'signed')
        self.assertNotIn('browser', json.loads(browser.frame_event(frame).decode().split('data: ')[1]))

    def test_stream_emits_signed_tabs_frames_and_closed(self):
        target = self.get()[1]['tabs'][0]['id']
        frame = (1, {'deviceWidth': 1280, 'deviceHeight': 800}, 'jpeg', 0)
        handler = FakeHandler('unused')
        handler.wfile = io.BytesIO()
        for method in ('send_response', 'send_header', 'end_headers'):
            setattr(handler, method, Mock())
        def closed(*args):
            self.signed.tabs.pop(target)
            return None
        with patch.object(self.signed, 'first_frame', return_value=frame), \
                patch.object(self.signed, 'wait_frame', side_effect=closed):
            browser_api.stream(handler, self.signed, target, 640, 480, 'signed')
        events = [event.split('\ndata: ', 1) for event in handler.wfile.getvalue().decode().split('\n\n') if '\ndata: ' in event]
        self.assertEqual([event[0] for event in events], ['event: tabs', 'event: frame', 'event: closed'])
        self.assertTrue(all(json.loads(event[1])['browser'] == 'signed' for event in events))
        self.assertEqual(self.signed.by_session['S-' + target].viewers, 0)

    def test_no_agents_or_private_contexts(self):
        self.get()
        client = Mock(closed=False, targets={'AGENT'})
        self.signed.adopt_agent({'targetId': 'AGENT'}, client)
        self.assertNotIn('AGENT', self.signed.tabs)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'Agents cannot'):
                browser_cdp.Proxy(self.signed, Path(folder) / 'config.json')
        self.assertIsNone(self.signed.agent_proxy)
        for name in ('agents', 'agents/setup', 'ask/dismiss'):
            self.assertEqual(self.post(name, browser='signed')[0], 400)
        self.assertEqual(self.get('agents')[0], 400)
        with self.assertRaises(browser.BrowserError):
            self.signed._adopt('AGENT', mode='agent')

    def test_missing_dependencies_are_visible_without_launch(self):
        self.signed.chrome_finder = lambda: None
        rows = self.get(query='')[1]['browsers']
        self.assertFalse(rows[1]['available'])
        self.assertIn('Google Chrome', rows[1]['reason'])
        self.assertEqual(self.get()[0], 503)
        self.signed.chrome_finder = lambda: '/fake/chrome'
        self.display.finder.return_value = None
        rows = self.get(query='')[1]['browsers']
        self.assertFalse(rows[1]['available'])
        self.assertIn('Xvfb', rows[1]['reason'])
        self.launcher.assert_not_called()

    def test_idle_stop_and_failed_launch_stop_display(self):
        self.get()
        self.signed.pid = 12345
        self.signed.used = -browser.IDLE_SECONDS * 2
        self.signed.idle_stop.set()  # stop the actual idle thread; test one tick synchronously
        self.signed.watcher.join(timeout=2)
        stopped = Mock()
        stopped.wait.return_value = False
        stopped.is_set.return_value = False
        with patch.object(browser, '_reap') as reap:
            self.signed._watch(stopped)
        reap.assert_called_once_with(12345)
        self.display.stop.assert_called_once()
        self.assertFalse(self.signed.running())
        self.assertTrue(self.signed.idle_stop.is_set())
        self.launcher.side_effect = OSError('spawn failed')
        with self.assertRaisesRegex(browser.BrowserError, 'spawn failed'):
            self.signed.ensure()
        self.assertEqual(self.display.stop.call_count, 2)

    def test_pipe_exit_stops_display(self):
        self.get()
        self.fake.close()
        deadline = time.monotonic() + 3
        while self.display.stop.call_count == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        self.display.stop.assert_called_once()
        self.assertFalse(self.signed.running())

    def test_headed_launch_flags_and_display_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(browser.os, 'posix_spawn', return_value=321) as spawn:
                pid, read_fd, write_fd = browser.launch('/fake/google-chrome', Path(folder) / 'signed/profile',
                                                       headed=True, env={'DISPLAY': ':213', 'XAUTHORITY': '/fake/auth'})
            os.close(read_fd)
            os.close(write_fd)
            args = spawn.call_args.args[1]
            env = spawn.call_args.args[2]
            self.assertEqual(pid, 321)
            self.assertIn('--remote-debugging-pipe', args)
            self.assertIn('--disable-blink-features=AutomationControlled', args)
            self.assertFalse(any(a.startswith(('--headless', '--enable-automation', '--remote-debugging-port')) for a in args))
            self.assertEqual((env['DISPLAY'], env['XAUTHORITY']), (':213', '/fake/auth'))
            self.assertEqual((Path(folder) / 'signed/profile').stat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(folder) / 'signed').stat().st_mode & 0o777, 0o700)


class DisplayTests(unittest.TestCase):
    def test_private_display_allocation_auth_and_cleanup(self):
        display = VirtualDisplay(finder=lambda: '/fake/Xvfb')
        process = Mock(pid=23456)
        process.poll.return_value = None
        def spawn(args, **kwargs):
            os.write(kwargs['pass_fds'][0], b'213\n')
            self.assertIn('-displayfd', args)
            self.assertEqual(args[args.index('-nolisten') + 1], 'tcp')
            self.assertNotIn('-ac', args)
            self.assertTrue(kwargs['start_new_session'])
            return process
        with patch.object(browser_display.subprocess, 'Popen', side_effect=spawn), \
                patch.object(browser_display.os, 'killpg') as kill:
            environment = display.start()
            authority = Path(environment['XAUTHORITY'])
            self.assertEqual(environment['DISPLAY'], ':213')
            self.assertEqual(authority.stat().st_mode & 0o777, 0o600)
            self.assertIn(b'213', authority.read_bytes())
            self.assertEqual(display.start(), environment)
            display.stop()
            kill.assert_called_once_with(23456, signal.SIGTERM)
            process.wait.assert_called_once_with(timeout=3)
            self.assertFalse(authority.parent.exists())

    def test_missing_xvfb_and_failed_spawn_leave_no_credentials(self):
        display = VirtualDisplay(finder=lambda: None)
        with self.assertRaisesRegex(RuntimeError, 'not installed'):
            display.start()
        display.finder = lambda: '/fake/Xvfb'
        with patch.object(browser_display.subprocess, 'Popen', side_effect=OSError('spawn failed')):
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                display.start()
        self.assertIsNone(display.folder)
        self.assertIsNone(display.process)


@unittest.skipUnless(browser.find_signed_chrome() and VirtualDisplay().finder(), 'Google Chrome and Xvfb are required')
class RealSignedTests(unittest.TestCase):
    def test_webdriver_frames_cookies_restart_and_process_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='tether-signed-test-') as folder:
            real = browser.Browser(chrome_finder=browser.find_signed_chrome, profile=Path(folder) / 'signed/profile', signed=True)
            processes = []
            try:
                for restart in (False, True):
                    state = real.list_tabs()
                    processes.append((real.pid, real.display.process.pid))
                    tab = real.tab(state['tabs'][0]['id'])
                    self.assertEqual(tab.mode, 'saved')
                    real.conn.call('Page.navigate', {'url': 'data:text/html,<h1>Tether signed browser</h1>'}, tab.session)
                    value = real.conn.call('Runtime.evaluate', {'expression': 'navigator.webdriver', 'returnByValue': True}, tab.session)
                    self.assertIs(value['result']['value'], False)
                    if not restart:
                        real.conn.call('Network.setCookie', {'name': 'tether_persistence', 'value': 'saved',
                                                            'url': 'https://example.test/', 'expires': time.time() + 3600}, tab.session)
                    else:
                        cookies = real.conn.call('Network.getCookies', {'urls': ['https://example.test/']}, tab.session)['cookies']
                        self.assertTrue(any(c['name'] == 'tether_persistence' and c['value'] == 'saved' for c in cookies))
                    real.watch(tab.target, 640, 480)
                    frame = real.first_frame(tab, 3)
                    self.assertTrue(base64.b64decode(frame[2]).startswith(b'\xff\xd8'))
                    real.unwatch(tab)
                    real.stop()
                    for pid in processes[-1]:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                        with self.assertRaises(ChildProcessError):
                            os.waitpid(pid, os.WNOHANG)
                print('SIGNED_REAL:', json.dumps({'processes': processes, 'webdriver': False, 'cookie_restart': True, 'stopped': True}), flush=True)
            finally:
                real.stop()


if __name__ == '__main__':
    unittest.main()
