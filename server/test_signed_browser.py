"""Signed browser isolation and lifecycle. Real tests use temporary profiles, never the live bridge."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_cdp
from test_browser import FakeHandler
from test_browser_container import FakeContainer, WindowChrome


class SignedTests(unittest.TestCase):
    def setUp(self):
        self.fake = WindowChrome()
        container = FakeContainer(self.fake)
        self.launcher = Mock(side_effect=container.launch)
        container.launch = self.launcher
        container.stop = Mock(side_effect=container.stop)
        self.container = container
        self.signed = browser.Browser(signed=True, container=container)
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
        self.launcher.assert_called_once_with('container', None)
        self.assertEqual(self.fake.sent('Target.createBrowserContext'), [])
        self.assertEqual(self.fake.sent('Runtime.enable'), [])
        self.assertEqual({c['method'] for c in self.fake.commands if c['method'].endswith('.enable')}, {'Page.enable'})
        self.assertEqual(self.fake.sent('Emulation.setUserAgentOverride')[-1]['params'], {'userAgent': ''})

    def test_signed_defaults_have_no_local_fallback(self):
        signed = browser.Browser(signed=True)
        self.assertIsNone(signed.profile)
        with patch.object(browser, 'find_chrome') as find_chrome, \
                self.assertRaisesRegex(browser.BrowserError, 'needs Docker'):
            signed.ensure()
        find_chrome.assert_not_called()

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
        signed = browser.Browser(signed=True)
        with patch.object(self, 'signed', signed):
            rows = self.get(query='')[1]['browsers']
            self.assertFalse(rows[1]['available'])
            self.assertIn('needs Docker', rows[1]['reason'])
            self.assertEqual(self.get()[0], 503)
        self.launcher.assert_not_called()

    def test_idle_stop_and_failed_launch_stop_container(self):
        self.get()
        self.signed.used = -browser.IDLE_SECONDS * 2
        self.signed.idle_stop.set()
        self.signed.watcher.join(timeout=2)
        stopped = Mock()
        stopped.wait.return_value = False
        stopped.is_set.return_value = False
        self.signed._watch(stopped)
        self.container.stop.assert_called_once()
        self.assertFalse(self.signed.running())
        self.launcher.side_effect = OSError('spawn failed')
        with self.assertRaisesRegex(browser.BrowserError, 'spawn failed'):
            self.signed.ensure()
        self.assertEqual(self.container.stop.call_count, 2)

    def test_legacy_window_endpoints_are_gone(self):
        for which in ('test', 'signed', 'chrome'):
            with self.subTest(browser=which):
                self.assertEqual(self.get('window', 'browser=' + which)[0], 404)
                self.assertEqual(self.post('window/input', browser=which, events=[])[0], 404)
        handler = FakeHandler(browser_api.PREFIX + 'window/input?browser=signed')
        browser_api.post(handler, self.test, signed_browser=self.signed)
        self.assertEqual(handler.replies[0][0], 404)

    def test_window_view_preserves_preset_and_never_enables_on_other_browsers(self):
        state = self.get()[1]
        self.assertIs(state['window_view'], False)
        target = state['tabs'][0]['id']
        self.post('device', browser='signed', tab=target, preset='phone')
        for invalid in ('test', 'chrome'):
            self.assertEqual(self.post('window-view', browser=invalid, tab=target, enabled=True)[0], 400)
        self.assertEqual(self.post('window-view', browser='signed', tab=target, enabled='true')[0], 400)
        self.assertEqual(self.post('window-view', browser='signed', tab='closed', enabled=True)[0], 503)
        self.assertEqual(self.post('window-view', browser='signed', tab=target, enabled=True),
                         (200, {'window_view': True, 'browser': 'signed'}))
        self.assertEqual([call['params']['bounds']['windowState'] for call in self.fake.sent('Browser.setWindowBounds')],
                         ['fullscreen', 'normal', 'maximized'])
        state = self.get()[1]
        self.assertIs(state['window_view'], True)
        self.assertEqual(state['tabs'][0]['preset'], 'pixel')
        self.assertEqual(self.fake.sent('Emulation.setEmitTouchEventsForMouse')[-1]['params'], {'enabled': False})
        other = self.post('new', browser='signed')[1]['id']
        self.assertEqual(self.post('show', browser='signed', tab=other)[0], 200)
        self.assertEqual(self.fake.window_state, 'maximized')

        self.assertEqual(self.post('window-view', browser='signed', tab=other, enabled=False)[0], 200)
        self.assertEqual(self.fake.window_state, 'fullscreen')
        self.assertEqual(self.post('show', browser='signed', tab=target)[0], 200)
        self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params']['width'], 412)
        self.assertIs(self.get()[1]['window_view'], False)


if __name__ == '__main__':
    unittest.main()
