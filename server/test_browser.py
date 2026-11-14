import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import browser
import browser_api


class FakeChrome:
    """Speaks the pipe protocol (NUL-separated JSON) on a pair of os.pipe()s and records every command."""

    def __init__(self):
        self.to_chrome_r, self.to_chrome_w = os.pipe()
        self.from_chrome_r, self.from_chrome_w = os.pipe()
        self.commands = []
        self.targets = ['T0']  # the hidden start-up page
        self.lock = threading.Lock()
        threading.Thread(target=self.serve, daemon=True).start()

    def launcher(self, chrome, profile):
        self.launched = (chrome, profile)
        return None, self.from_chrome_r, self.to_chrome_w

    def emit(self, message):
        with self.lock:
            if self.from_chrome_w is None:
                return
            os.write(self.from_chrome_w, json.dumps(message).encode() + b'\0')

    def serve(self):
        buffer = b''
        while True:
            chunk = os.read(self.to_chrome_r, 65536)
            if not chunk:  # the Connection closed its end: release ours (each fd is closed exactly once, by its owner)
                os.close(self.to_chrome_r)
                return
            buffer += chunk
            while b'\0' in buffer:
                raw, buffer = buffer.split(b'\0', 1)
                message = json.loads(raw)
                self.commands.append(message)
                self.emit({'id': message['id'], 'result': self.answer(message)})

    def answer(self, message):
        method, params = message['method'], message.get('params', {})
        if method == 'Browser.getVersion':
            return {'userAgent': 'Mozilla/5.0 HeadlessChrome/147.0.1.2 Safari/537.36'}
        if method == 'Target.getTargets':
            return {'targetInfos': [{'targetId': target, 'type': 'page', 'url': 'about:blank', 'title': ''} for target in self.targets]}
        if method == 'Target.createTarget':
            self.targets.append('T%d' % len(self.targets))
            return {'targetId': self.targets[-1]}
        if method == 'Target.createBrowserContext':
            return {'browserContextId': 'C%d' % len(self.targets)}
        if method == 'Target.attachToTarget':
            return {'sessionId': 'S-' + params['targetId']}
        if method == 'Target.getTargetInfo':
            return {'targetInfo': {'targetId': params['targetId'], 'type': 'page', 'url': 'https://b.test/', 'title': 'Title set by script'}}
        if method == 'Page.getNavigationHistory':
            return {'currentIndex': 1, 'entries': [{'id': 10}, {'id': 11}, {'id': 12}]}
        return {}

    def sent(self, method):
        return [command for command in self.commands if command['method'] == method]

    def close(self):
        with self.lock:
            if self.from_chrome_w is not None:
                os.close(self.from_chrome_w)
                self.from_chrome_w = None


def make():
    fake = FakeChrome()
    return fake, browser.Browser(launcher=fake.launcher, chrome_finder=lambda: '/fake/chrome', profile=Path('/nonexistent/profile'))


class UrlTests(unittest.TestCase):
    def test_settings_pages_only_with_the_signed_flag(self):
        self.assertEqual(browser.normalize_url('chrome://settings', settings=True), 'chrome://settings')
        self.assertEqual(browser.normalize_url('CHROME://settings/passwords', settings=True), 'chrome://settings/passwords')
        self.assertEqual(browser.normalize_url('chrome://settings/searchEngines?search=x', settings=True), 'chrome://settings/searchEngines?search=x')
        for text in ('chrome://settings', 'chrome://settings/'):
            with self.assertRaises(ValueError):
                browser.normalize_url(text)
        for text in ('chrome://version', 'chrome://settings-x', 'chrome://settings/../flags', 'chrome://settingsx', 'chrome://flags'):
            with self.assertRaises(ValueError, msg=text):
                browser.normalize_url(text, settings=True)

    def test_local_shortcuts(self):
        self.assertEqual(browser.normalize_url('8765'), 'http://127.0.0.1:8765')
        self.assertEqual(browser.normalize_url('localhost:8765'), 'http://127.0.0.1:8765')
        self.assertEqual(browser.normalize_url('localhost:8765/a?b=1'), 'http://127.0.0.1:8765/a?b=1')
        self.assertEqual(browser.normalize_url('127.0.0.1:3000'), 'http://127.0.0.1:3000')
        self.assertEqual(browser.normalize_url('example-host:5173'), 'http://example-host:5173')
        # MCP OAuth callbacks land on the agent's local callback server on any port.
        self.assertEqual(browser.normalize_url('http://localhost:53682/callback?code=x&state=y'), 'http://localhost:53682/callback?code=x&state=y')
        self.assertEqual(browser.normalize_url('http://127.0.0.1:1455/auth/callback'), 'http://127.0.0.1:1455/auth/callback')
        self.assertEqual(browser.normalize_url('8765/?phone'), 'http://127.0.0.1:8765/?phone')
        self.assertEqual(browser.normalize_url('http://[::1]:8080/'), 'http://[::1]:8080/')

    def test_web_addresses(self):
        self.assertEqual(browser.normalize_url('example.com'), 'https://example.com')
        self.assertEqual(browser.normalize_url('http://example.com/x'), 'http://example.com/x')
        self.assertEqual(browser.normalize_url('HTTPS://Example.com'), 'HTTPS://Example.com')
        self.assertEqual(browser.normalize_url(''), 'about:blank')
        self.assertEqual(browser.normalize_url('about:blank'), 'about:blank')

    def test_blocked_schemes(self):
        for bad in ('file:///etc/passwd', 'chrome://settings', 'javascript:alert(1)', 'JavaScript:alert(1)',
                    'data:text/html,hi', 'about:config', 'view-source:http://x', 'http://', 'http://x\n.com'):
            with self.assertRaises(ValueError, msg=bad):
                browser.normalize_url(bad)


class InputTests(unittest.TestCase):
    def test_mouse(self):
        method, params = browser.input_command({'type': 'down', 'x': 10.5, 'y': 4, 'clicks': 2})
        self.assertEqual(method, 'Input.dispatchMouseEvent')
        self.assertEqual(params['type'], 'mousePressed')
        self.assertEqual((params['button'], params['buttons'], params['clickCount']), ('left', 1, 2))
        self.assertEqual(browser.input_command({'type': 'move', 'x': 1, 'y': 2})[1]['button'], 'none')

    def test_wheel(self):
        params = browser.input_command({'type': 'wheel', 'x': 1, 'y': 2, 'dx': 0, 'dy': 120})[1]
        self.assertEqual((params['type'], params['deltaY']), ('mouseWheel', 120.0))

    def test_keys(self):
        down = browser.input_command({'type': 'keydown', 'key': 'a', 'code': 'KeyA'})[1]
        self.assertEqual((down['type'], down['text'], down['windowsVirtualKeyCode']), ('keyDown', 'a', 65))
        enter = browser.input_command({'type': 'keydown', 'key': 'Enter', 'code': 'Enter'})[1]
        self.assertEqual((enter['type'], enter['text'], enter['windowsVirtualKeyCode']), ('keyDown', '\r', 13))
        arrow = browser.input_command({'type': 'keydown', 'key': 'ArrowLeft'})[1]
        self.assertEqual((arrow['type'], arrow.get('text')), ('rawKeyDown', None))
        select = browser.input_command({'type': 'keydown', 'key': 'a', 'modifiers': 2, 'commands': ['selectAll', 'rm -rf']})[1]
        self.assertEqual((select['type'], select['commands']), ('rawKeyDown', ['selectAll']))
        self.assertEqual(browser.input_command({'type': 'keyup', 'key': 'a'})[1]['type'], 'keyUp')

    def test_key_virtual_codes(self):
        cases = [('.', 'Period', 190), (',', 'Comma', 188), ('/', 'Slash', 191), (';', 'Semicolon', 186),
                 ('-', 'Minus', 189), ('=', 'Equal', 187), ("'", 'Quote', 222), ('[', 'BracketLeft', 219),
                 (']', 'BracketRight', 221), ('\\', 'Backslash', 220), ('`', 'Backquote', 192)]
        for key, code, expected in cases:
            with self.subTest(key=key, code=code):
                down = browser.input_command({'type': 'keydown', 'key': key, 'code': code})[1]
                self.assertEqual(down['text'], key)
                self.assertEqual(down['windowsVirtualKeyCode'], expected)
                self.assertNotIn(down['windowsVirtualKeyCode'], (*range(33, 41), 45, 46))
        for key, code, modifiers, expected in [('>', 'Period', 8, 190), ('#', 'Digit3', 8, 51),
                                                ('(', 'Digit9', 0, 57), ('.', '', 0, 190),
                                                ('é', '', 0, 0), ('a', 'KeyA', 0, 65)]:
            with self.subTest(key=key, code=code, modifiers=modifiers):
                event = {'type': 'keydown', 'key': key, 'modifiers': modifiers}
                if code:
                    event['code'] = code
                down = browser.input_command(event)[1]
                self.assertEqual((down['text'], down['windowsVirtualKeyCode']), (key, expected))

    def test_text_and_bad_input(self):
        self.assertEqual(browser.input_command({'type': 'text', 'text': 'héllo'}), ('Input.insertText', {'text': 'héllo'}))
        for bad in ({'type': 'down', 'x': 'a', 'y': 1}, {'type': 'eval'}, {'type': 'keydown'}, {'type': 'text', 'text': ''}, 'x',
                    {'type': 'down', 'x': True, 'y': 1}):
            with self.assertRaises(ValueError):
                browser.input_command(bad)


class FakePipeTests(unittest.TestCase):
    def setUp(self):
        self.fake, self.browser = make()

    def tearDown(self):
        self.fake.close()

    def test_lazy_start_adopts_first_tab_and_sets_desktop(self):
        self.assertEqual(self.fake.commands, [])
        state = self.browser.list_tabs()
        self.assertEqual([tab['id'] for tab in state['tabs']], ['T1'])
        attach = self.fake.sent('Target.attachToTarget')[0]
        self.assertEqual(attach['params'], {'targetId': 'T1', 'flatten': True})
        self.assertEqual(self.fake.sent('Browser.setDownloadBehavior')[0]['params'], {'behavior': 'deny'})
        agent = self.fake.sent('Emulation.setUserAgentOverride')[0]
        self.assertEqual(agent['sessionId'], 'S-T1')
        self.assertNotIn('Headless', agent['params']['userAgent'])

    def test_navigate_validates_and_uses_session(self):
        self.browser.list_tabs()
        self.browser.navigate('T1', '8765')
        self.assertEqual(self.fake.sent('Page.navigate')[-1], {'id': self.fake.sent('Page.navigate')[-1]['id'], 'method': 'Page.navigate',
                                                               'params': {'url': 'http://127.0.0.1:8765'}, 'sessionId': 'S-T1'})
        with self.assertRaises(ValueError):
            self.browser.navigate('T1', 'file:///etc/passwd')
        with self.assertRaises(browser.BrowserError):
            self.browser.navigate('nope', 'example.com')

    def test_history_back_and_forward(self):
        self.browser.list_tabs()
        self.browser.history('T1', 'back')
        self.browser.history('T1', 'forward')
        self.assertEqual([command['params']['entryId'] for command in self.fake.sent('Page.navigateToHistoryEntry')], [10, 12])
        self.assertEqual(self.browser.nav_state('T1'), {'back': True, 'forward': True})

    def test_new_and_close_tab(self):
        self.browser.list_tabs()
        row = self.browser.new_tab('localhost:3000')
        self.assertEqual(row['id'], 'T2')
        state = self.browser.close_tab('T2')
        self.assertEqual([tab['id'] for tab in state['tabs']], ['T1'])
        self.assertEqual(self.fake.sent('Target.closeTarget')[-1]['params'], {'targetId': 'T2'})

    def test_first_tab_is_private_and_start_page_stays_hidden(self):
        state = self.browser.list_tabs()
        self.assertEqual([(tab['id'], tab['mode']) for tab in state['tabs']], [('T1', 'private')])
        self.assertEqual(self.fake.sent('Target.createBrowserContext')[0]['params'], {'disposeOnDetach': True})
        self.assertEqual(self.fake.sent('Target.createTarget')[0]['params'], {'url': 'about:blank', 'browserContextId': 'C1'})
        self.assertNotIn('T0', self.browser.tabs)

    def test_saved_tab_uses_profile_and_private_close_disposes_context(self):
        self.browser.list_tabs()
        saved = self.browser.new_tab('example.com', 'saved')
        self.assertEqual(saved['mode'], 'saved')
        self.assertNotIn('browserContextId', self.fake.sent('Target.createTarget')[-1]['params'])
        with self.assertRaises(ValueError):
            self.browser.new_tab('', 'incognito')
        self.browser.close_tab('T1')
        deadline = time.time() + 2
        while not self.fake.sent('Target.disposeBrowserContext') and time.time() < deadline:
            time.sleep(.01)
        self.assertEqual(self.fake.sent('Target.disposeBrowserContext')[0]['params'], {'browserContextId': 'C1'})

    def test_popup_keeps_opener_mode(self):
        self.browser.list_tabs()
        self.fake.emit({'method': 'Target.targetCreated', 'params': {'targetInfo': {
            'targetId': 'P1', 'type': 'page', 'openerId': 'T1', 'browserContextId': 'C1', 'url': 'https://a.test/'}}})
        deadline = time.time() + 2
        while 'P1' not in self.browser.tabs and time.time() < deadline:
            time.sleep(.01)
        self.assertEqual((self.browser.tabs['P1'].mode, self.browser.tabs['P1'].context), ('private', 'C1'))

    def test_heal_applies_the_device_again_when_frames_lose_it(self):
        self.browser.list_tabs()
        self.browser.set_device('T1', 'pixel')
        tab = self.browser.watch('T1', 640, 768)
        sent = len(self.fake.sent('Emulation.setDeviceMetricsOverride'))
        self.assertTrue(self.browser.heal(tab, (1, {'deviceWidth': 412, 'deviceHeight': 915}, 'x', 3)))
        self.assertFalse(self.browser.heal(tab, (2, {'deviceWidth': 1280, 'deviceHeight': 657}, 'x', 3)))
        self.assertEqual(len(self.fake.sent('Emulation.setDeviceMetricsOverride')), sent + 1)
        self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params']['width'], 412)
        self.assertFalse(self.browser.heal(tab, (3, {'deviceWidth': 1280, 'deviceHeight': 657}, 'x', 3)))  # once a second
        self.assertEqual(len(self.fake.sent('Emulation.setDeviceMetricsOverride')), sent + 1)
        self.browser.unwatch(tab)

    def test_phone_tab_turns_a_press_into_touch(self):
        self.browser.list_tabs()
        self.browser.set_device('T1', 'pixel')
        self.browser.input('T1', [{'type': 'move', 'x': 5, 'y': 6, 'buttons': 0}, {'type': 'down', 'x': 10, 'y': 20, 'button': 'left'},
                                  {'type': 'move', 'x': 12, 'y': 30, 'buttons': 1, 'button': 'left'}, {'type': 'up', 'x': 12, 'y': 30, 'button': 'left'},
                                  {'type': 'wheel', 'x': 1, 'y': 1, 'dx': 0, 'dy': 50}])
        touches = [(command['params']['type'], command['params']['touchPoints']) for command in self.fake.sent('Input.dispatchTouchEvent')]
        self.assertEqual(touches, [('touchStart', [{'x': 10, 'y': 20}]), ('touchMove', [{'x': 12, 'y': 30}]), ('touchEnd', [])])
        self.assertEqual([command['params']['type'] for command in self.fake.sent('Input.dispatchMouseEvent')], ['mouseWheel'])
        self.assertFalse(self.fake.sent('Emulation.setEmitTouchEventsForMouse')[-1]['params']['enabled'])

    def test_device_preset_mobile(self):
        self.browser.list_tabs()
        row = self.browser.set_device('T1', 'pixel')
        self.assertEqual((row['preset'], row['width'], row['touch']), ('pixel', 412, True))
        metrics = self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params']
        self.assertEqual((metrics['width'], metrics['mobile'], metrics['deviceScaleFactor']), (412, True, 2.625))
        self.assertTrue(self.fake.sent('Emulation.setTouchEmulationEnabled')[-1]['params']['enabled'])
        self.assertIn('Android', self.fake.sent('Emulation.setUserAgentOverride')[-1]['params']['userAgent'])
        self.assertIn('147.0.1.2', self.fake.sent('Emulation.setUserAgentOverride')[-1]['params']['userAgent'])
        with self.assertRaises(ValueError):
            self.browser.set_device('T1', 'toaster')

    def test_screencast_frame_ack_and_stop(self):
        self.browser.list_tabs()
        tab = self.browser.watch('T1', 900, 700)
        start = self.fake.sent('Page.startScreencast')[-1]
        self.assertEqual((start['params']['maxWidth'], start['params']['format']), (900, 'jpeg'))
        jpeg = base64.b64encode(b'\xff\xd8fake').decode()
        self.fake.emit({'method': 'Page.screencastFrame', 'sessionId': 'S-T1',
                        'params': {'data': jpeg, 'sessionId': 7, 'metadata': {'deviceWidth': 1280, 'deviceHeight': 800}}})
        frame = self.browser.wait_frame(tab, 0, 2)
        self.assertEqual((frame[0], frame[2], frame[3]), (1, jpeg, 7))
        message = browser.frame_event(frame).decode()
        self.assertTrue(message.startswith('event: frame\ndata: '))
        self.assertEqual(json.loads(message.split('data: ', 1)[1])['jpeg'], jpeg)
        self.browser.ack(tab, frame)
        self.browser.ack(tab, frame)  # a second viewer: no duplicate ack
        # Chromium gives every frame of one screencast the same sessionId, so the next frame must be acked too.
        self.fake.emit({'method': 'Page.screencastFrame', 'sessionId': 'S-T1',
                        'params': {'data': jpeg, 'sessionId': 7, 'metadata': {'deviceWidth': 1280, 'deviceHeight': 800}}})
        self.browser.ack(tab, self.browser.wait_frame(tab, 1, 2))
        deadline = time.time() + 2
        while len(self.fake.sent('Page.screencastFrameAck')) < 2 and time.time() < deadline:
            time.sleep(.01)
        time.sleep(.05)
        self.assertEqual([command['params'] for command in self.fake.sent('Page.screencastFrameAck')], [{'sessionId': 7}] * 2)
        self.browser.unwatch(tab)
        deadline = time.time() + 2
        while not self.fake.sent('Page.stopScreencast') and time.time() < deadline:
            time.sleep(.01)
        self.assertTrue(self.fake.sent('Page.stopScreencast'))

    def test_new_stream_size_stops_the_running_screencast_first(self):
        # Headed Chrome (the signed-in browser) answers "Screencast is already active" to a second start.
        self.browser.list_tabs()
        self.browser.watch('T1', 960, 1280)
        self.browser.watch('T1', 1024, 1344)
        casts = [command['method'] for command in self.fake.commands if command['method'] in ('Page.startScreencast', 'Page.stopScreencast')]
        self.assertEqual(casts, ['Page.startScreencast', 'Page.stopScreencast', 'Page.startScreencast'])
        self.assertEqual(self.fake.sent('Page.startScreencast')[-1]['params']['maxHeight'], 1344)

    def test_watch_brings_a_signed_tab_to_the_front(self):
        # Headed Chrome stops painting a tab behind another one, so its stream dropped to 0.2 fps.
        self.browser.list_tabs()
        self.browser.watch('T1', 960, 1280)
        self.assertEqual(self.fake.sent('Page.bringToFront'), [])
        self.browser.signed = True
        self.browser.watch('T1', 960, 1280)
        self.assertEqual(len(self.fake.sent('Page.bringToFront')), 1)

    def test_target_destroyed_and_info_changed(self):
        self.browser.list_tabs()
        self.browser.new_tab()
        version = self.browser.version
        self.fake.emit({'method': 'Target.targetInfoChanged', 'params': {'targetInfo': {'targetId': 'T1', 'url': 'https://a.test/', 'title': 'A'}}})
        self.fake.emit({'method': 'Target.targetDestroyed', 'params': {'targetId': 'T2'}})
        deadline = time.time() + 2
        while 'T2' in self.browser.tabs and time.time() < deadline:
            time.sleep(.01)
        self.assertNotIn('T2', self.browser.tabs)
        self.assertEqual((self.browser.tabs['T1'].url, self.browser.tabs['T1'].title), ('https://a.test/', 'A'))
        self.assertGreater(self.browser.version, version)

    def test_refresh_reads_title_chromium_did_not_send(self):
        self.browser.list_tabs()
        tab = self.browser.tabs['T1']
        version = self.browser.version
        self.browser.refresh(tab)
        self.assertEqual((tab.url, tab.title), ('https://b.test/', 'Title set by script'))
        self.assertGreater(self.browser.version, version)
        version = self.browser.version
        self.browser.refresh(tab)  # nothing changed: no new tab list for viewers
        self.assertEqual(self.browser.version, version)

    def test_browser_exit_fails_calls_and_restarts_lazily(self):
        self.browser.list_tabs()
        self.fake.close()
        deadline = time.time() + 2
        while self.browser.running() and time.time() < deadline:
            time.sleep(.01)
        self.assertFalse(self.browser.running())
        replacement = FakeChrome()
        self.browser.launcher = replacement.launcher
        self.assertEqual([tab['id'] for tab in self.browser.list_tabs()['tabs']], ['T1'])
        replacement.close()

    def test_no_chrome(self):
        quiet = browser.Browser(launcher=self.fake.launcher, chrome_finder=lambda: None, profile=Path('/nonexistent/profile'))
        with self.assertRaises(browser.BrowserError):
            quiet.list_tabs()


class FakeHandler:
    def __init__(self, path, body=None):
        self.path = path
        data = json.dumps(body).encode() if body is not None else b''
        self.headers = {'Content-Length': str(len(data))}
        self.rfile = io.BytesIO(data)
        self.replies = []

    def respond(self, status, body, *rest):
        self.replies.append((status, body))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.fake, self.browser = make()

    def tearDown(self):
        self.fake.close()

    def test_routes(self):
        handler = FakeHandler('/mobile/api/browser/tabs')
        browser_api.get(handler, self.browser)
        self.assertEqual(handler.replies[0][0], 200)
        self.assertNotIn('nav', handler.replies[0][1])
        target = self.browser.order[0]
        handler = FakeHandler('/mobile/api/browser/tabs?tab=' + target)  # the live view polls with its tab
        browser_api.get(handler, self.browser)
        self.assertEqual(handler.replies[0][1]['nav'], {'back': True, 'forward': True})
        self.assertEqual(handler.replies[0][1]['tabs'][0]['title'], 'Title set by script')
        handler = FakeHandler('/mobile/api/browser/navigate', {'tab': 'T1', 'url': 'javascript:alert(1)'})
        browser_api.post(handler, self.browser)
        self.assertEqual(handler.replies[0][0], 400)
        handler = FakeHandler('/mobile/api/browser/input', {'tab': 'T1', 'events': [{'type': 'text', 'text': 'hi'}]})
        browser_api.post(handler, self.browser)
        self.assertEqual(handler.replies[0], (200, {'ok': True}))
        handler = FakeHandler('/mobile/api/browser/navigate', {'tab': 'gone', 'url': 'example.com'})
        browser_api.post(handler, self.browser)
        self.assertEqual(handler.replies[0][0], 503)
        handler = FakeHandler('/mobile/api/browser/eval', {'tab': 'T1'})
        browser_api.post(handler, self.browser)
        self.assertEqual(handler.replies[0][0], 404)


@unittest.skipUnless(os.environ.get('TETHER_REAL_BROWSER_TEST') == '1',
                     'Set TETHER_REAL_BROWSER_TEST=1 to run the real Chromium test')
@unittest.skipUnless(browser.find_chrome(), 'Chromium is not installed')
class RealChromiumSmokeTest(unittest.TestCase):
    def test_launch_load_and_get_one_frame(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        real = browser.Browser(profile=Path(folder.name) / 'browser' / 'profile')
        try:
            state = real.list_tabs()
            target = state['tabs'][0]['id']
            pid = real.pid
            with self.assertRaises(OSError):  # the DevTools channel cannot be opened through /proc (a pipe could)
                os.open('/proc/%d/fd/3' % pid, os.O_WRONLY | os.O_NONBLOCK)
            tab = real.watch(target, 640, 480)
            real.conn.call('Page.navigate', {'url': 'data:text/html,<h1>Tether</h1>'}, tab.session)
            frame = real.first_frame(tab, 3)
            self.assertIsNotNone(frame)
            self.assertTrue(base64.b64decode(frame[2]).startswith(b'\xff\xd8'))  # JPEG
            self.assertEqual(frame[1]['deviceWidth'], 1280)
            real.unwatch(tab)
            self.assertEqual(state['tabs'][0]['mode'], 'private')
            for folder_path in (Path(folder.name) / 'browser', Path(folder.name) / 'browser' / 'profile'):
                self.assertEqual(folder_path.stat().st_mode & 0o777, 0o700)
        finally:
            real.stop()
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)  # reaped: nothing of ours is left running


if __name__ == '__main__':
    unittest.main()
