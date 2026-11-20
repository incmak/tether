"""Isolated device credentials, connection lifetime, routing and one-tab CDP contracts."""
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_cdp as cdp
import mac_browser as mac
import signed_access as signed
from test_browser import FakeHandler
from test_browser_cdp import Capture
from test_browser_device import playwright_modules
from test_signed_access import wait_for, open_grant_socket, assert_grant_close, assert_ended_http

ORIGIN = 'chrome-extension://' + 'a' * 32


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p20-pair-')
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.broker = mac.Broker(Path(self.temp.name) / 'devices.json', clock=lambda: self.now)

    def test_once_expiry_hash_reload_origin_and_revoke(self):
        code = self.broker.pair()
        self.assertEqual(code['expires'], 1300)
        pair = self.broker.exchange(code['code'], ORIGIN, 'Test Chrome')
        content = self.broker.store.read_text()
        self.assertNotIn(pair['token'], content)
        self.assertIn('hash', content)
        self.assertEqual(self.broker.store.stat().st_mode & 0o777, 0o600)
        other = mac.Broker(self.broker.store)
        self.assertEqual(other.authenticate(pair['token'], ORIGIN), pair['id'])
        self.assertNotIn('hash', self.broker.rows()[0])
        for token, origin in ((pair['token'], 'https://example.test'), ('x' * 43, ORIGIN),
                              (pair['token'], 'chrome-extension://' + 'b' * 32)):
            with self.assertRaises(ValueError):
                other.authenticate(token, origin)
        with self.assertRaises(ValueError):
            self.broker.exchange(code['code'], ORIGIN, 'Again')
        expired = self.broker.pair()
        self.now += 301
        with self.assertRaises(ValueError):
            self.broker.exchange(expired['code'], ORIGIN, 'Late')
        self.broker.revoke(pair['id'])
        with self.assertRaises(ValueError):
            self.broker.authenticate(pair['token'], ORIGIN)
        self.assertEqual(self.broker.rows(), [])

    def test_invalid_origin_does_not_consume_code_and_routes_hide_credentials(self):
        code = self.broker.pair()['code']
        with self.assertRaises(ValueError):
            self.broker.exchange(code, 'null', 'Chrome')
        self.broker.exchange(code, ORIGIN, 'Chrome')
        with patch.object(mac, 'BROKER', self.broker):
            handler = FakeHandler(browser_api.PREFIX + 'extension/devices')
            browser_api.get(handler)
            self.assertEqual(handler.replies[0][0], 200)
            self.assertNotIn('hash', json.dumps(handler.replies))
            handler = FakeHandler(browser_api.PREFIX + 'extension/pair', {})
            browser_api.post(handler)
            self.assertIn('code', handler.replies[0][1])


class FakeTransport:
    closed = False
    def __init__(self):
        self.device = None
        self.sent = []

    def message(self, message):
        self.sent.append(message)
        if message['type'] == 'grant':
            self.device.handle({'type': 'granted', 'id': message['id'], 'tab': 7, 'target': 'T7',
                                'expires': (time.time() + 60) * 1000, 'epoch': self.device.epoch})
        elif message['type'] == 'command':
            self.device.handle({'type': 'result', 'id': message['id'], 'result': {}, 'epoch': self.device.epoch})

    def abort(self):
        self.closed = True


class ChromiumTransport(FakeTransport):
    """One real debugger session, shared by every synthetic Mac session."""
    def __init__(self):
        super().__init__()
        self.temp = tempfile.TemporaryDirectory(prefix='tether-mac-contexts-')
        self.events = []
        self.pid, read_fd, write_fd = browser.launch(browser.find_chrome(), Path(self.temp.name) / 'profile')
        self.conn = browser.Connection(read_fd, write_fd, self.event)
        try:
            # Chrome opens its first page a moment after the pipe is ready; on a busy machine the first list is empty.
            pages = []
            def page_ready():
                pages[:] = [row['targetId'] for row in self.conn.call('Target.getTargets')['targetInfos']
                            if row['type'] == 'page']
                return pages
            wait_for(page_ready, seconds=10)
            self.target = pages[0]
            self.session = self.conn.call('Target.attachToTarget',
                                          {'targetId': self.target, 'flatten': True})['sessionId']
        except Exception:
            self.abort()
            raise

    def event(self, method, params, session):
        if session == self.session and self.device:
            self.events.append((method, params))
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': self.grant,
                                'tab': 7, 'method': method, 'params': params})

    def message(self, message):
        self.sent.append(message)
        if message['type'] == 'grant':
            self.grant = message['id']
            self.device.handle({'type': 'granted', 'id': self.grant, 'tab': 7, 'target': self.target,
                                'expires': (time.time() + 60) * 1000, 'epoch': self.device.epoch})
        elif message['type'] == 'command':
            def reply(result):
                self.device.handle({'type': 'error' if 'error' in result else 'result', 'id': message['id'],
                                    'result': result.get('result', {}), 'epoch': self.device.epoch})
            self.conn.forward({'method': message['method'], 'params': message['params'],
                               'sessionId': self.session}, reply)

    def abort(self):
        if self.closed:
            return
        super().abort()
        try:
            os.killpg(self.pid, signal.SIGTERM)
            try:
                wait_for(lambda: os.waitpid(self.pid, os.WNOHANG)[0], seconds=5)
            except AssertionError:
                os.killpg(self.pid, signal.SIGKILL)
                wait_for(lambda: os.waitpid(self.pid, os.WNOHANG)[0], seconds=5)
        finally:
            self.conn._shut()
            self.conn.reader.join(timeout=2)
            self.temp.cleanup()


class GrantHarness:
    transport_type = FakeTransport

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p20-grant-')
        self.addCleanup(self.temp.cleanup)
        self.broker = mac.Broker(Path(self.temp.name) / 'devices.json')
        pair = self.broker.exchange(self.broker.pair()['code'], ORIGIN, 'Test Chrome')
        self.transport = self.transport_type()
        self.device = self.broker.register(pair['id'], self.transport, 'Chrome/147.0')
        self.transport.device = self.device
        self.access = mac.MacAccess(self.broker, request_seconds=3)
        self.access._listen = lambda: None
        self.waiters = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.broker.stop()
        for waiter in self.waiters:
            waiter.join(timeout=4)
            self.assertFalse(waiter.is_alive())

    def grant(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.access.ask('Test task', '%12', 1)))
        thread.start()
        self.waiters.append(thread)
        wait_for(lambda: self.access.state()['requests'])
        row = self.access.state()['requests'][0]
        self.assertEqual(row['browser'], 'chrome')
        self.access.allow(row['id'])
        thread.join(timeout=3)
        self.assertEqual(result[0]['browser'], 'chrome')
        self.assertTrue(result[0]['ok'])
        return self.access.grants[row['id']]

    def client(self, grant):
        capture = Capture()
        client = signed.Client(self.access, capture, grant)
        self.access.clients.add(client)
        client.open()
        self.addCleanup(client.cleanup)
        return client, capture


CONTEXT_CLIENT = r'''
const assert = require('node:assert/strict');
const {chromium} = require('playwright-core');
(async () => {
  const connections = [];
  const connect = async () => {
    const connection = await chromium.connectOverCDP(process.env.E2E_CDP_URL, {timeout:10000});
    connections.push(connection);
    assert.equal(connection.contexts()[0].pages().length, 1);
    return connection;
  };
  const exercise = async (connection, label) => {
    const page = connection.contexts()[0].pages()[0];
    page.setDefaultTimeout(5000);
    assert.equal(await page.evaluate(() => document.body.tagName), 'BODY');
    assert.match(await page.locator('h1').textContent(), /^Mac context/);
    await page.goto(process.env.E2E_PAGE_URL + label, {timeout:10000});
    assert.equal(await page.evaluate(() => document.querySelector('h1').textContent), 'Mac context /' + label);
    assert.equal(await page.locator('h1').textContent(), 'Mac context /' + label);
    const png = await page.screenshot({timeout:5000});
    assert.equal(png.subarray(1,4).toString(), 'PNG');
  };
  try {
    if (process.env.E2E_MODE === 'concurrent') {
      const [first, second] = await Promise.all([connect(), connect()]);
      await exercise(first, 'first');
      await exercise(second, 'second');
      await first.close();
      await exercise(second, 'survivor');
    } else {
      await exercise(await connect(), 'first');
      await connections[0].close();
      await exercise(await connect(), 'second');
    }
    console.log(JSON.stringify({ok:true, mode:process.env.E2E_MODE}));
  } finally {
    for (const connection of connections) await connection.close();
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
'''


@unittest.skipUnless(os.environ.get('TETHER_REAL_BROWSER_TEST') == '1',
                     'Set TETHER_REAL_BROWSER_TEST=1 to run the real Chromium tests')
@unittest.skipUnless(browser.find_chrome(), 'Headless Chromium is required')
class RealContextTests(GrantHarness, unittest.TestCase):
    transport_type = ChromiumTransport

    def command(self, client, capture, method, params=None, session=None):
        message = {'id': 1, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        client.handle(message)
        reply = capture.messages[-1]
        self.assertNotIn('error', reply)
        return reply['result']

    def attach(self, grant):
        client, capture = self.client(grant)
        session = self.command(client, capture, 'Target.attachToTarget',
                               {'targetId': grant.row['tab'], 'flatten': True})['sessionId']
        return client, capture, session

    def test_panel_refresh_reads_real_url_title_and_history_events(self):
        url = self.serve_page()
        grant = self.grant()
        tab = self.broker.browser.tab(grant.row['tab'])
        conn, session = self.transport.conn, self.transport.session
        conn.call('Page.navigate', {'url': url}, session)
        wait_for(lambda: conn.call('Runtime.evaluate',
                 {'expression': 'location.href + " " + document.readyState'},
                 session)['result'].get('value') == url + ' complete')

        def refreshed():
            self.broker.browser.refresh(tab)
            return (tab.url, tab.title) == (url, 'Mac context test')

        wait_for(refreshed)
        version = self.broker.browser.version
        conn.call('Runtime.evaluate', {'expression': 'history.pushState({}, "", location.href)'}, session)
        wait_for(lambda: self.broker.browser.version > version)
        self.assertTrue(self.broker.browser.nav_state(tab.target)['back'])
        conn.call('Runtime.evaluate', {'expression': 'document.title = "Updated title"'}, session)

        def retitled():
            self.broker.browser.refresh(tab)
            return (tab.url, tab.title) == (url, 'Updated title')

        wait_for(retitled)

    def test_runtime_already_enabled_second_session_gets_main_world(self):
        grant = self.grant()
        first, out, session = self.attach(grant)
        self.command(first, out, 'Runtime.enable', session=session)
        self.assertTrue(any(event.get('method') == 'Runtime.executionContextCreated' for event in out.messages))
        second, out, session = self.attach(grant)
        self.command(second, out, 'Runtime.enable', session=session)
        contexts = [event['params']['context'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        self.assertTrue(contexts, 'Second synthetic session never receives a main-world execution context')
        main = next(context for context in contexts if context.get('auxData', {}).get('isDefault'))
        result = self.command(second, out, 'Runtime.evaluate',
                              {'expression': 'document.body.tagName', 'contextId': main['id']}, session)
        self.assertEqual(result['result']['value'], 'BODY')

    def test_log_history_replayed_to_second_session(self):
        url = self.serve_page()
        grant = self.grant()
        first, a, one = self.attach(grant)
        # console.error belongs to Runtime; a failed resource supplies a real
        # browser-console error in Log.entryAdded as well.
        self.command(first, a, 'Page.navigate', {'url': url}, one)
        wait_for(lambda: self.transport.conn.call('Runtime.evaluate',
                 {'expression': 'location.href + " " + document.readyState'},
                 self.transport.session)['result'].get('value') == url + ' complete')
        self.command(first, a, 'Runtime.evaluate',
                     {'expression': "console.error('p28'); fetch('/missing-log').catch(() => {})",
                      'awaitPromise': True}, one)
        self.command(first, a, 'Log.enable', session=one)
        wait_for(lambda: any(event.get('method') == 'Log.entryAdded' for event in a.messages))
        entries = [event['params'] for event in a.messages if event.get('method') == 'Log.entryAdded']
        self.assertTrue(any(entry['entry']['level'] == 'error' for entry in entries))
        second, b, two = self.attach(grant)
        self.command(second, b, 'Log.enable', session=two)
        replay = [event['params'] for event in b.messages if event.get('method') == 'Log.entryAdded']
        self.assertEqual(replay, entries, 'Second synthetic session never receives Log history')
        self.assertEqual([event['params'] for event in a.messages
                          if event.get('method') == 'Log.entryAdded'], entries)
        self.assertEqual(sum(row.get('method') == 'Log.enable' for row in self.transport.sent), 1)

    def test_chromium_navigation_clears_native_log_but_relay_retains_it(self):
        url = self.serve_page()
        grant = self.grant()
        conn, session = self.transport.conn, self.transport.session
        conn.call('Log.enable', session=session)
        conn.call('Runtime.evaluate',
                  {'expression': "fetch('" + url + "missing-log').catch(() => {})", 'awaitPromise': True}, session)
        wait_for(lambda: any(method == 'Log.entryAdded' for method, _ in self.transport.events))
        entries = [params for method, params in self.transport.events if method == 'Log.entryAdded']
        conn.call('Log.disable', session=session)
        self.transport.events.clear()
        conn.call('Log.enable', session=session)
        before_navigation = [params for method, params in self.transport.events if method == 'Log.entryAdded']
        self.assertEqual(before_navigation, entries)
        cached = list(self.broker.browser.conn.log_entries[grant.row['tab']])
        conn.call('Page.navigate', {'url': url}, session)
        wait_for(lambda: conn.call('Runtime.evaluate', {'expression': 'document.readyState'},
                                  session)['result'].get('value') == 'complete')
        conn.call('Log.disable', session=session)
        self.transport.events.clear()
        conn.call('Log.enable', session=session)
        replay = [params for method, params in self.transport.events if method == 'Log.entryAdded']
        self.assertEqual(replay, [], 'Chromium navigation behavior changed')
        client, capture, synthetic = self.attach(grant)
        self.command(client, capture, 'Log.enable', session=synthetic)
        self.assertEqual([event['params'] for event in capture.messages
                          if event.get('method') == 'Log.entryAdded'], cached)

    def serve_page(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = ('<title>Mac context test</title><h1>Mac context ' + self.path + '</h1>').encode()
                self.send_response(404 if self.path == '/missing-log' else 200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name='mac-context-test-http')
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return f'http://127.0.0.1:{server.server_port}/'

    def playwright(self, mode):
        url = self.serve_page()
        signed.Access._listen(self.access)
        grant = self.grant()
        panel = self.broker.browser.tab(grant.row['tab']).session
        self.broker.browser.conn.call('Runtime.enable', session=panel)
        self.broker.browser.conn.call('Page.navigate', {'url': url}, panel)
        wait_for(lambda: self.transport.conn.call('Runtime.evaluate',
                 {'expression': 'document.readyState'}, self.transport.session)['result'].get('value') == 'complete')
        run = subprocess.run(['timeout', '45', 'node', '-e', CONTEXT_CLIENT],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=50,
                             env=dict(os.environ, NODE_PATH=playwright_modules(), E2E_CDP_URL=self.access.url(grant),
                                      E2E_PAGE_URL=url, E2E_MODE=mode))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout), {'ok': True, 'mode': mode})

    @unittest.skipUnless(shutil.which('node') and playwright_modules(), 'Node and ui/npm ci are required')
    def test_two_sequential_playwright_clients(self):
        self.playwright('sequential')

    @unittest.skipUnless(shutil.which('node') and playwright_modules(), 'Node and ui/npm ci are required')
    def test_two_concurrent_playwright_clients_and_disconnect(self):
        self.playwright('concurrent')

    def test_navigation_replays_only_live_contexts(self):
        url = self.serve_page()
        grant = self.grant()
        first, out, session = self.attach(grant)
        self.command(first, out, 'Runtime.enable', session=session)
        original = {event['params']['context']['uniqueId'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated'}
        # Disconnect the only subscriber, then navigate with no Runtime subscribers.
        first.cleanup()
        panel = self.broker.browser.tab(grant.row['tab']).session
        self.broker.browser.conn.call('Page.navigate', {'url': url}, panel)
        wait_for(lambda: any(method == 'Runtime.executionContextsCleared' for method, _ in self.transport.events))
        wait_for(lambda: self.transport.conn.call('Runtime.evaluate',
                 {'expression': 'document.readyState'}, self.transport.session)['result'].get('value') == 'complete')
        second, out, session = self.attach(grant)
        self.command(second, out, 'Runtime.enable', session=session)
        contexts = [event['params']['context'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        self.assertTrue(contexts)
        self.assertTrue(original.isdisjoint(context['uniqueId'] for context in contexts))
        main = next(context for context in contexts if context.get('auxData', {}).get('isDefault'))
        result = self.command(second, out, 'Runtime.evaluate',
                              {'expression': 'document.querySelector("h1").textContent', 'contextId': main['id']}, session)
        self.assertEqual(result['result']['value'], 'Mac context /')

    def test_removed_iframe_context_not_replayed(self):
        grant = self.grant()
        first, out, session = self.attach(grant)
        self.command(first, out, 'Runtime.enable', session=session)
        self.command(first, out, 'Runtime.evaluate', {'expression': '''new Promise(resolve => {
          const frame = document.createElement('iframe'); frame.id = 'child';
          frame.onload = () => resolve(true); document.body.append(frame);
        })''', 'awaitPromise': True}, session)
        contexts = [event['params']['context'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        child = next(context for context in contexts if context['auxData'].get('frameId') != self.transport.target)
        self.command(first, out, 'Runtime.evaluate', {'expression': 'document.getElementById("child").remove()'}, session)
        wait_for(lambda: any(method == 'Runtime.executionContextDestroyed'
                             and params.get('executionContextId') == child['id']
                             for method, params in self.transport.events))
        second, out, session = self.attach(grant)
        self.command(second, out, 'Runtime.enable', session=session)
        replayed = [event['params']['context']['uniqueId'] for event in out.messages
                    if event.get('method') == 'Runtime.executionContextCreated']
        self.assertTrue(replayed)
        self.assertNotIn(child['uniqueId'], replayed)


class GrantTests(GrantHarness, unittest.TestCase):
    def test_panel_alias_query_body_and_stream_emit_chrome(self):
        panel_browser = Mock()
        panel_browser.list_tabs.return_value = {'tabs': []}
        panel_browser.input.return_value = {'ok': True}
        with patch.object(mac, 'BROKER', self.broker), patch.object(self.broker, 'browser', panel_browser):
            for ident in ('mac', 'chrome'):
                handler = FakeHandler(browser_api.PREFIX + 'tabs?browser=' + ident)
                browser_api.get(handler)
                self.assertEqual(handler.replies[0][0], 200)
                self.assertEqual(handler.replies[0][1]['browser'], 'chrome')
                handler = FakeHandler(browser_api.PREFIX + 'input', {'browser': ident, 'tab': 'T7', 'events': []})
                browser_api.post(handler)
                self.assertEqual(handler.replies[0], (200, {'ok': True, 'browser': 'chrome'}))
                with patch.object(browser_api, 'stream') as stream:
                    handler = FakeHandler(browser_api.PREFIX + 'stream?browser=' + ident + '&tab=T7')
                    browser_api.get(handler)
                    self.assertEqual(stream.call_args.args[5], 'chrome')
                self.assertIn('"browser": "chrome"', browser.frame_event((1, {}, 'jpeg', 0), ident).decode())

    def test_ask_signed_body_alias_emits_chrome_through_grant_and_end(self):
        from urllib.request import Request, urlopen
        with tempfile.TemporaryDirectory() as folder:
            proxy = cdp.Proxy(config_path=Path(folder) / 'config.json', env_runner=Mock(), pane_finder=lambda *a: '%12')
            proxy.signed_access = mac.RoutedAccess(signed.Access(Mock()), self.access)
            proxy.start()
            self.addCleanup(proxy.stop)
            url = proxy.url.replace('ws:', 'http:').replace('/cdp/', '/ask-signed/')
            for ident in ('mac', 'chrome'):
                results = []
                def request():
                    body = json.dumps({'text': 'Requested task', 'browser': ident}).encode()
                    with urlopen(Request(url, data=body), timeout=5) as response:
                        results.append(json.load(response))
                thread = threading.Thread(target=request)
                self.waiters.append(thread)
                thread.start()
                wait_for(lambda: self.access.state()['requests'])
                row = self.access.state()['requests'][0]
                self.assertEqual(row['browser'], 'chrome')
                self.access.allow(row['id'])
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0]['browser'], 'chrome')
                self.assertEqual(self.access.state()['grants'][0]['browser'], 'chrome')
                self.access.end(row['id'], 'expired')
                self.assertEqual(self.access.state()['ended'][-1]['browser'], 'chrome')

    def test_extension_debugger_canceled(self):
        self.assert_extension_end_reason('debugger_canceled', "Chrome's debugging bar was closed.")

    def test_extension_debugger_target_closed(self):
        self.assert_extension_end_reason('debugger_target_closed', 'Chrome closed the debugging connection to the tab. Ask again.')

    def test_extension_legacy_debugger_detached(self):
        self.assert_extension_end_reason('debugger_detached', 'Chrome ended the debugging connection.')

    def assert_extension_end_reason(self, reason, message):
        signed.Access._listen(self.access)
        grant = self.grant()
        url = self.access.url(grant)
        stream = open_grant_socket(self, self.access, grant)
        self.device.handle({'type': 'ended', 'epoch': self.device.epoch, 'id': grant.row['id'],
                            'reason': reason})
        assert_grant_close(self, stream, message)
        wait_for(lambda: grant.row['id'] not in self.access.grants)
        self.assertEqual(grant.end_reason, reason)
        row = self.access.state()['ended'][0]
        self.assertEqual(row, dict(id=grant.row['id'], pane='%12', agent='agent', browser='chrome',
                                   reason=reason, message=message, at=row['at']))
        assert_ended_http(self, url, reason, message)
        panel = FakeHandler(browser_api.PREFIX + 'signed/access')
        browser_api.get(panel, signed_browser=self.broker.browser)
        self.assertNotIn(grant.token, json.dumps(panel.replies))
        self.assertEqual(panel.replies[0][1]['ended'], [row])
        panel = FakeHandler(browser_api.PREFIX + 'signed/dismiss', {'id': grant.row['id']})
        browser_api.post(panel, signed_browser=self.broker.browser)
        self.assertEqual(panel.replies[0][1]['ended'], [])
        assert_ended_http(self, url, reason, message)

    def test_tether_end_wins_over_extension_echo(self):
        signed.Access._listen(self.access)
        grant = self.grant()
        url = self.access.url(grant)
        client, output = self.client(grant)
        # Echo arrives synchronously while Tether sends its end command.
        send = self.transport.message
        def echo(message):
            send(message)
            if message['type'] == 'end':
                self.device.handle({'type': 'ended', 'epoch': self.device.epoch, 'id': message['id'],
                                    'reason': 'debugger_detached'})
        self.transport.message = echo
        self.access.end(grant.row['id'])
        self.assertEqual(grant.end_reason, 'tether_end')
        self.assertTrue(client.closed)
        self.assertEqual(output.closes, [4000])
        self.assertEqual(output.close_reasons, ['Access ended: It was ended in Tether.'])
        self.assertEqual(self.access.state()['ended'], [])
        assert_ended_http(self, url, 'tether_end', 'It was ended in Tether.')

    def test_expiry_and_disconnect_reasons(self):
        grant = self.grant()
        grant.timer.cancel()
        grant.timer.function(*grant.timer.args)
        self.assertEqual(self.access.state()['ended'][0]['reason'], 'expired')
        grant = self.grant()
        self.device.close()
        wait_for(lambda: not self.access.grants)
        self.assertEqual(self.access.state()['ended'][-1]['reason'], 'connection_lost')

    def test_context_subscription_replay_disable_detach_and_removal(self):
        grant = self.grant()
        conn = self.broker.browser.conn
        target = grant.row['tab']
        first = conn.call('Target.attachToTarget', {'targetId': target})['sessionId']
        second = conn.call('Target.attachToTarget', {'targetId': target})['sessionId']
        a, b = [], []
        conn.register_sink(first, a.append)
        conn.register_sink(second, b.append)
        conn.call('Runtime.enable', session=first)
        def emit(method, params, ident=None):
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': ident or grant.row['id'],
                                'tab': 7, 'method': method, 'params': params})
        emit('Runtime.executionContextCreated', {'context': {'id': 1, 'uniqueId': 'main'}})
        emit('Runtime.executionContextCreated', {'context': {'id': 2, 'uniqueId': 'child'}})
        self.assertEqual(b, [])  # An attached but disabled session receives no Runtime events.
        emit('Runtime.executionContextDestroyed', {'executionContextUniqueId': 'child'})
        emit('Runtime.executionContextCreated', {'context': {'id': 3, 'uniqueId': 'old'}}, 'stale-grant')
        before = len(a)
        conn.call('Runtime.enable', session=second)
        self.assertEqual(len(a), before)
        self.assertEqual([row['params']['context']['id'] for row in b], [1])
        conn.call('Runtime.enable', session=second)
        self.assertEqual(len(b), 1)  # Same-session enable is idempotent, as in Chrome.
        conn.call('Runtime.disable', session=first)
        emit('Runtime.executionContextsCleared', {})
        self.assertEqual(len(a), before)
        self.assertEqual(b[-1]['method'], 'Runtime.executionContextsCleared')
        conn.call('Runtime.enable', session=first)
        self.assertEqual(len(a), before)  # The cleared context must not return.
        methods = [row.get('method') for row in self.transport.sent]
        self.assertEqual(methods.count('Runtime.enable'), 1)
        self.assertNotIn('Runtime.disable', methods)
        conn.send('Target.detachFromTarget', {'sessionId': first})
        self.assertNotIn(first, conn.sessions)
        self.assertNotIn(first, conn.enabled)
        self.assertNotIn(first, conn.sinks)
        with patch.object(self.broker.browser, '_event', side_effect=lambda *args: self.access.end(grant.row['id'])):
            emit('Runtime.executionContextCreated', {'context': {'id': 4}})
        self.assertEqual(conn.contexts, {})
        self.assertEqual(conn.enabled, {})
        self.assertEqual(conn.runtime_started, set())

    def test_log_history_bound_clear_and_isolation(self):
        grant = self.grant()
        conn = self.broker.browser.conn
        target = grant.row['tab']
        one, two = [conn.call('Target.attachToTarget', {'targetId': target})['sessionId'] for _ in range(2)]
        a, b = [], []
        conn.register_sink(one, a.append)
        conn.register_sink(two, b.append)
        conn.call('Log.enable', session=one)
        for index in range(105):
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': grant.row['id'],
                                'tab': 7, 'method': 'Log.entryAdded',
                                'params': {'entry': {'text': str(index)}}})
        self.assertEqual(b, [])
        conn.call('Log.enable', session=two)
        self.assertEqual([row['params']['entry']['text'] for row in b], [str(i) for i in range(5, 105)])
        self.assertEqual(len(a), 105)
        conn.call('Log.enable', session=two)
        self.assertEqual(len(b), 100)
        conn.call('Log.clear', session=one)
        self.assertEqual(self.transport.sent[-1]['method'], 'Log.clear')
        conn.call('Log.disable', session=two)
        b.clear()
        conn.call('Log.enable', session=two)
        self.assertEqual(b, [])
        conn.call('Log.disable', session=one)
        conn.call('Log.disable', session=two)
        self.assertNotIn('Log.disable', [row.get('method') for row in self.transport.sent])
        conn.detach(one)
        conn.detach(two)
        three = conn.call('Target.attachToTarget', {'targetId': target})['sessionId']
        before = len(self.transport.sent)
        conn.call('Log.enable', session=three)
        self.assertEqual(len(self.transport.sent), before)  # Real Log remains enabled after detach.
        self.access.end(grant.row['id'])
        self.assertEqual(conn.log_entries, {})
        self.assertEqual(conn.log_started, set())

    def test_shared_domains_disable_only_when_last_subscriber_disables(self):
        grant = self.grant()
        conn = self.broker.browser.conn
        sessions = [conn.call('Target.attachToTarget', {'targetId': grant.row['tab']})['sessionId'] for _ in range(2)]
        for domain in sorted(mac.SHARED_DOMAINS - {'Runtime', 'Log'}):  # these stay on in Chrome
            with self.subTest(domain=domain):
                for session in sessions:
                    conn.call(domain + '.enable', session=session)
                before = len(self.transport.sent)
                conn.call(domain + '.disable', session=sessions[0])
                self.assertEqual(len(self.transport.sent), before)
                conn.call(domain + '.disable', session=sessions[1])
                if domain == 'Page':
                    self.assertEqual(len(self.transport.sent), before)
                    # The panel keeps Page events for history changes until it also unsubscribes.
                    panel = self.broker.browser.tab(grant.row['tab']).session
                    conn.call('Page.disable', session=panel)
                self.assertEqual(self.transport.sent[-1]['method'], domain + '.disable')
                self.assertEqual(len(self.transport.sent), before + 1)
        for session in sessions:
            conn.call('Page.setLifecycleEventsEnabled', {'enabled': True}, session)
        before = len(self.transport.sent)
        conn.call('Page.setLifecycleEventsEnabled', {'enabled': False}, sessions[0])
        self.assertEqual(len(self.transport.sent), before)
        conn.call('Page.setLifecycleEventsEnabled', {'enabled': False}, sessions[1])
        self.assertEqual(len(self.transport.sent), before + 1)

    def test_discovery_policy_identity_takeover_and_end(self):
        grant = self.grant()
        client, output = self.client(grant)
        def call(method, params=None, session=None):
            message = {'id': len(output.messages) + 1, 'method': method, 'params': params or {}}
            if session:
                message['sessionId'] = session
            client.handle(message)
            return output.messages[-1]
        result = call('Target.getTargets')['result']['targetInfos']
        self.assertEqual([row['targetId'] for row in result], [grant.row['tab']])
        session = call('Target.attachToTarget', {'targetId': grant.row['tab'], 'flatten': True})['result']['sessionId']
        before = len(self.transport.sent)
        for method in ('Network.getCookies', 'Network.getAllCookies', 'Storage.getCookies', 'DOM.setFileInputFiles',
                       'Browser.close', 'Target.createBrowserContext', 'Target.createTarget'):
            self.assertIn('error', call(method, session=session if not method.startswith(('Browser.', 'Target.')) else None), method)
        self.assertIn('error', call('Runtime.evaluate', {'expression': '1'}, 'wrong-session'))
        self.assertIn('error', call('Target.attachToTarget', {'targetId': 'other', 'flatten': True}))
        self.assertIn('error', call('Page.navigate', {'url': 'file:///etc/passwd'}, session))
        self.assertEqual(len(self.transport.sent), before)
        self.assertIn('result', call('Runtime.evaluate', {'expression': '1+1'}, session))
        tab = self.broker.browser.tab(grant.row['tab'])
        tab.controller = 'user'
        self.assertIn('error', call('Runtime.evaluate', {'expression': '1+1'}, session))
        self.assertIn('error', call('Input.insertText', {'text': 'no'}, session))
        tab.controller = 'agent'
        self.access.end(grant.row['id'])
        self.assertFalse(grant.active)
        self.assertTrue(client.closed)
        self.assertEqual(self.broker.browser.tabs, {})
        self.assertEqual(self.access.state()['grants'], [])
        self.assertEqual(self.transport.sent[-1]['type'], 'end')

    def test_disconnect_revoke_closes_clients_and_reconnect_never_restores(self):
        grant = self.grant()
        client, _ = self.client(grant)
        self.broker.revoke(self.device.ident)
        self.assertFalse(self.access.valid(grant))
        self.assertTrue(client.closed)
        wait_for(lambda: not self.broker.browser.tabs)
        self.assertFalse(self.broker.connected())
        with self.assertRaises(ValueError):
            self.broker.register(self.device.ident, FakeTransport(), '')

    def test_frames_ack_on_receipt_newest_only_and_isolation(self):
        grant = self.grant()
        tab = self.broker.browser.tab(grant.row['tab'])
        for i in range(3):
            self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': grant.row['id'], 'tab': 7,
                                'method': 'Page.screencastFrame', 'params': {'sessionId': 1, 'data': str(i),
                                'metadata': {'deviceWidth': 1280, 'deviceHeight': 800}}})
        self.assertEqual(tab.frame[2], '2')
        self.assertEqual(tab.seq, 3)
        acks = [m for m in self.transport.sent if m.get('method') == 'Page.screencastFrameAck']
        self.assertEqual(len(acks), 3)
        self.device.handle({'type': 'event', 'epoch': self.device.epoch, 'grant': 'old', 'tab': 7,
                            'method': 'Page.screencastFrame', 'params': {'sessionId': 1, 'data': 'wrong'}})
        self.assertEqual(tab.seq, 3)
        self.assertIn('"browser": "chrome"', browser.frame_event(tab.frame, 'chrome').decode())

    def test_auto_routing_is_pinned_and_explicit_chrome_fails_offline(self):
        server = Mock()
        routed = mac.RoutedAccess(server, self.access)
        with patch.object(self.access, 'ask', return_value={'ok': True}) as ask:
            routed.ask('Task', '%12')
            ask.assert_called_once()
            routed.ask('Task', '%12', browser='server')
            server.ask.assert_called_once()
        self.device.close()
        routed.ask('Task', '%12')
        self.assertEqual(server.ask.call_count, 2)
        with self.assertRaises(ValueError):
            routed.ask('Task', '%12', browser='chrome')
        with self.assertRaises(ValueError):
            routed.ask('Task', '%12', browser='bad')

    def test_bad_epoch_and_routing_fields(self):
        with self.assertRaises(ValueError):
            self.device.handle({'type': 'ping', 'epoch': 'old'})
        for method in ('Network.getCookies', 'Network.getAllCookies', 'Storage.getCookies', 'DOM.setFileInputFiles',
                       'Browser.getVersion', 'Target.setAutoAttach'):
            with self.assertRaises(browser.BrowserError):
                mac.check_command(method, {})
        for params in ({'sessionId': 'other'}, {'nested': [{'targetId': 'other'}]}, {'browserContextId': 'other'}):
            with self.assertRaises(browser.BrowserError):
                mac.check_command('Runtime.evaluate', params)
        mac.check_command('Page.screencastFrameAck', {'sessionId': 1})

    def test_pending_request_stays_bound_to_its_original_device_connection(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.access.ask('Pinned task', '%12', 1)))
        self.waiters.append(thread)
        thread.start()
        wait_for(lambda: self.access.pending)
        ident = next(iter(self.access.pending))
        self.assertIs(self.access.pending[ident]['binding'], self.device)
        pair = self.broker.exchange(self.broker.pair()['code'], ORIGIN, 'Second Chrome')
        transport = FakeTransport()
        other = self.broker.register(pair['id'], transport, 'Test Chrome')
        transport.device = other
        self.assertIs(self.broker.connected(), other)
        self.access.allow(ident)
        thread.join(timeout=1)
        self.assertIs(self.access.grants[ident].device, self.device)
        self.assertFalse(transport.sent)

    def test_end_during_inflight_command_wakes_waiter_without_disconnect(self):
        grant = self.grant()
        sent = threading.Event()
        errors = []
        original = self.transport.message
        def hold(message):
            if message['type'] == 'command':
                self.transport.sent.append(message)
                sent.set()
            else:
                original(message)
        self.transport.message = hold
        def command():
            try:
                with self.broker.browser.lock:
                    self.broker.browser.conn.call('Runtime.evaluate', {'expression': '1'},
                                                  self.broker.browser.tab(grant.row['tab']).session)
            except browser.BrowserError as exc:
                errors.append(str(exc))
        waiter = threading.Thread(target=command)
        self.waiters.append(waiter)
        waiter.start()
        self.assertTrue(sent.wait(1))
        self.access.end(grant.row['id'])
        waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(errors)
        self.assertFalse(self.device.closed)

    def test_cross_browser_pending_replacement_and_global_grant_limit(self):
        server = signed.Access(browser.Browser(signed=True), request_seconds=3)
        self.addCleanup(server.stop)
        routed = mac.RoutedAccess(server, self.access)
        results = []
        first = threading.Thread(target=lambda: results.append(routed.ask('Mac task', '%12', browser='chrome')))
        self.waiters.append(first)
        first.start()
        wait_for(lambda: self.access.pending)
        second = threading.Thread(target=lambda: results.append(routed.ask('Server task', '%12', browser='server')))
        self.waiters.append(second)
        second.start()
        wait_for(lambda: server.pending)
        first.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertEqual(results, [{'ok': False, 'reason': 'denied'}])
        self.assertFalse(self.access.pending)
        server.grants = {'one': Mock(), 'two': Mock()}
        with self.assertRaisesRegex(ValueError, 'limit is 2'):
            routed.allow(next(iter(server.pending)))
        server.grants = {}
        with self.assertRaises(ValueError):
            routed.allow([])
        routed.deny(next(iter(server.pending)))
        second.join(timeout=1)
        self.assertFalse(second.is_alive())

    def test_js_python_policy_match_and_extension_identity_checks(self):
        script = '''import {ALLOWED,command} from './extension/policy.mjs';
const g={id:'g',tab:7,epoch:'e',expires:Date.now()+60000};
const c={type:'command',id:'c',grant:'g',tab:7,epoch:'e',method:'Runtime.evaluate',params:{expression:'1'}};
command(c,g,'e');
for(const d of [{tab:8},{epoch:'old'},{grant:'old'},{sessionId:'other'},{method:'Network.getCookies'},
 {method:'DOM.setFileInputFiles'},{params:{nested:[{targetId:'other'}]}}]) {
 let refused=false; try{command({...c,...d},g,'e')}catch{refused=true} if(!refused)throw Error(JSON.stringify(d));
}
console.log(JSON.stringify([...ALLOWED].sort()));'''
        run = subprocess.run(['node', '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), sorted(mac.PAGE_ALLOWED))

    def test_extension_grant_survives_backpressure(self):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run(['node', 'server/fixtures/extension_backpressure.mjs'], cwd=root,
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()


class WebSocketAuthTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.temp = tempfile.TemporaryDirectory(prefix='p20-ws-')
        self.addCleanup(self.temp.cleanup)
        self.broker = mac.Broker(Path(self.temp.name) / 'devices.json')
        self.pair = self.broker.exchange(self.broker.pair()['code'], ORIGIN, 'Test Chrome')
        broker = self.broker
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def respond(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                mac.upgrade(self, broker)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.broker.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def websocket(self, token, origin=ORIGIN):
        import socket
        from test_browser_cdp import masked
        sock = socket.create_connection(('127.0.0.1', self.httpd.server_port), timeout=2)
        self.addCleanup(sock.close)
        sock.sendall((f'GET {mac.PREFIX}connect HTTP/1.1\r\nHost: 127.0.0.1:{self.httpd.server_port}\r\n'
                      'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                      'Sec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n' + f'Origin: {origin}\r\n\r\n').encode())
        stream = sock.makefile('rb')
        self.addCleanup(stream.close)
        status = stream.readline()
        while stream.readline() != b'\r\n':
            pass
        if b'101' not in status:
            return status, None, sock
        sock.sendall(masked(1, json.dumps({'type': 'hello', 'token': token, 'chrome': 'Test Chrome'}).encode()))
        head = stream.read(2)
        if not head:
            return status, None, sock
        size = head[1] & 0x7f
        if size == 126:
            size = int.from_bytes(stream.read(2), 'big')
        message = json.loads(stream.read(size))
        return status, message, sock

    def test_bad_origin_bad_token_ready_and_revoke(self):
        status, _, _ = self.websocket(self.pair['token'], 'https://evil.test')
        self.assertIn(b'403', status)
        _, result, _ = self.websocket('x' * 43)
        self.assertIsNone(result)
        self.assertIsNone(self.broker.connected())
        _, result, _ = self.websocket(self.pair['token'])
        self.assertEqual(result['type'], 'ready')
        self.assertTrue(self.broker.connected())
        self.broker.revoke(self.pair['id'])
        self.assertIsNone(self.broker.connected())
        _, result, _ = self.websocket(self.pair['token'])
        self.assertIsNone(result)
