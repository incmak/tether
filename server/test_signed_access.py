"""Signed grant lifecycle and fail-closed CDP scope. No live browser, profile, or tmux writes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import base64
import io
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import browser
import browser_api
import browser_cdp
import mac_browser
import paths
import signed_access as signed
from test_browser import FakeHandler
from test_browser_cdp import Chrome, Capture
from test_browser_device import playwright_modules
from own_time import own_time


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for isolated test state')


def open_grant_socket(test, access, grant):
    """An actual loopback CDP upgrade; no Chrome or user sockets."""
    sock = socket.create_connection(('127.0.0.1', access.config['port']), timeout=2)
    test.addCleanup(sock.close)
    stream = sock.makefile('rb')
    test.addCleanup(stream.close)
    sock.sendall((f'GET /cdp-signed/{grant.token} HTTP/1.1\r\n'
                  f"Host: 127.0.0.1:{access.config['port']}\r\n"
                  'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                  'Sec-WebSocket-Key: YWJjZGVmZ2hpamtsbW5vcA==\r\n\r\n').encode())
    test.assertIn(b'101', stream.readline())
    while stream.readline() != b'\r\n':
        pass
    wait_for(lambda: any(client.transport for client in access.clients))
    return stream


def assert_grant_close(test, stream, message):
    head = stream.read(2)
    test.assertEqual(head[0] & 0x0f, 8)
    test.assertLessEqual(head[1], 125)
    payload = stream.read(head[1])
    test.assertEqual(struct.unpack('!H', payload[:2])[0], 4000)
    test.assertEqual(payload[2:].decode(), 'Access ended: ' + message)
    test.assertEqual(stream.read(1), b'')


def assert_ended_http(test, url, reason, message):
    # Upgrade requests must receive the same JSON error before any 101 response.
    for headers in ({}, {'Upgrade': 'websocket', 'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
                          'Sec-WebSocket-Key': base64.b64encode(b'abcdefghijklmnop').decode()}):
        with test.assertRaises(HTTPError) as error:
            urlopen(Request(url.replace('ws:', 'http:'), headers=headers), timeout=2)
        with error.exception as response:
            test.assertEqual(response.code, 410)
            test.assertEqual(json.load(response), {'error': 'Signed-in browser access ended: ' + message,
                                                  'reason': reason})


class EndedHistoryTests(unittest.TestCase):
    def test_shared_limit_ten_minute_retention_and_fallback(self):
        now = [1000]
        server = signed.Access(browser.Browser(signed=True), clock=lambda: now[0])
        # Mac and server share one cap, including dismiss and retention.
        mac = mac_browser.MacAccess(mac_browser.Broker(), clock=lambda: now[0])
        routed = mac_browser.RoutedAccess(server, mac)
        for index in range(25):
            owner = mac if index % 2 else server
            grant = signed.Grant(dict(id=str(index), pane='%12', agent='codex', reason='Task'), 'tab', 1, now[0])
            with owner.lock:
                owner._retire(grant, 'future_reason')
                owner._retire(grant, 'tether_end')  # First cause survives repeats.
            now[0] += 1
        rows = routed.state()['ended']
        self.assertEqual([row['id'] for row in rows], [str(index) for index in range(5, 25)])
        self.assertEqual({row['browser'] for row in rows}, {'chrome', 'server'})
        self.assertEqual(rows[-1]['message'], 'Access ended.')
        self.assertEqual(rows[-1]['reason'], 'future_reason')
        versions = (server.browser.version, mac.browser.version)
        self.assertEqual(len(routed.dismiss('24')['ended']), 19)
        self.assertGreater(server.browser.version, versions[0])
        self.assertGreater(mac.browser.version, versions[1])
        self.assertIsNotNone(mac.history.lookup(grant.token))
        now[0] = 1605  # Row 5 expires at exactly ten minutes.
        self.assertEqual(routed.state()['ended'][0]['id'], '6')
        now[0] = 1624
        self.assertEqual(routed.state()['ended'], [])
        self.assertIsNotNone(server.history.lookup(grant.token))
        now[0] = 4624
        self.assertIsNone(server.history.lookup(grant.token))
        self.assertEqual(server.history.tokens, {})

    def test_reason_table(self):
        groups = [
            (('debugger_canceled',), "Chrome's debugging bar was closed."),
            (('debugger_target_closed',), 'Chrome closed the debugging connection to the tab. Ask again.'),
            (('debugger_detached',), 'Chrome ended the debugging connection.'),
            (('tab_closed',), 'The tab was closed.'),
            (('expired',), 'The time ran out.'),
            (('action_stop', 'local_stop'), 'It was stopped from the Chrome extension.'),
            (('tether_end',), 'It was ended in Tether.'),
            (('connection_lost', 'heartbeat_lost', 'connection_changed'), 'Chrome disconnected.'),
            (('backpressure', 'command_limit'), 'Too many commands at once.'),
            (('stream_backpressure',),
             'Your Chrome sent page frames faster than Tether could take them. Update the Tether extension.'),
            (('stream_overflow',), 'Your Chrome could not send page updates fast enough.'),
            (('attach_failed',), 'Chrome did not allow the debugger.'),
            (('browser_stopped',), 'The browser stopped.'),
            (('unknown',), 'Access ended.'),
        ]
        for codes, message in groups:
            for code in codes:
                with self.subTest(code=code):
                    self.assertEqual(signed.end_message(code), message)


class SignedChrome(Chrome):
    def __init__(self):
        self.session_number = 0
        super().__init__()

    def answer(self, message):
        if message['method'] == 'Target.attachToTarget':
            self.session_number += 1
            return {'sessionId': 'S%d' % self.session_number}
        return super().answer(message)


class Requests:
    def ask(self, pane='%12', duration=15, **kwargs):
        results = []
        before = {row['id'] for row in self.access.state()['requests']}
        thread = threading.Thread(target=lambda: results.append(self.access.ask('Read the requested account', pane,
                                                                               duration, **kwargs)), daemon=True)
        thread.start()
        self.waiters.append(thread)
        wait_for(lambda: any(row['id'] not in before for row in self.access.state()['requests']))
        row = next(row for row in self.access.state()['requests'] if row['id'] not in before)
        return row['id'], thread, results

    def grant(self, pane='%12', duration=15, **kwargs):
        ident, thread, results = self.ask(pane, duration, **kwargs)
        self.access.allow(ident)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]['ok'])
        return self.access.grants[ident], results[0]


class AccessTests(Requests, unittest.TestCase):
    def setUp(self):
        self.fake = SignedChrome()
        self.real = browser.Browser(signed=True, profile=Path('/nonexistent/signed-grant-test'),
                                    chrome_finder=lambda: '/fake/chrome', launcher=self.fake.launcher)
        self.notifier = Mock()
        self.access = signed.Access(self.real, self.notifier, agent_finder=lambda pane: 'codex', request_seconds=3)
        self.waiters = []
        self.addCleanup(self.cleanup)
        self.ident = 0

    def cleanup(self):
        self.access.stop()
        self.real.stop()
        self.fake.close()
        for thread in self.waiters:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        if self.access.thread:
            self.assertFalse(self.access.thread.is_alive())
        if self.real.watcher:
            self.real.watcher.join(timeout=2)

    def client(self, grant):
        client = signed.Client(self.access, Capture(), grant)
        self.access.clients.add(client)
        client.open()
        self.addCleanup(client.cleanup)
        return client

    def command(self, client, method, params=None, session=None):
        self.ident += 1
        message = {'id': self.ident, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        client.handle(message)
        return client.transport.reply(self.ident)

    def test_deny_timeout_replacement_and_notification(self):
        ident, first, result = self.ask()
        request = self.access.state()['requests'][0]
        self.assertEqual(set(request), {'id', 'pane', 'agent', 'reason', 'minutes', 'url', 'created', 'browser'})
        self.assertEqual(request['browser'], 'server')
        self.assertEqual(request['agent'], 'codex')
        payload = self.notifier.call_args.args[0]
        self.assertEqual(payload['body'], 'codex wants to use your signed-in browser: Read the requested account')
        self.assertTrue(self.notifier.call_args.kwargs['urgent'])
        next_id, second, next_result = self.ask()
        first.join(timeout=2)
        self.assertEqual(result, [{'ok': False, 'reason': 'denied'}])
        with self.assertRaises(ValueError):
            self.access.allow(ident)
        self.access.deny(next_id)
        second.join(timeout=2)
        self.assertEqual(next_result, [{'ok': False, 'reason': 'denied'}])
        self.access.request_seconds = .05
        self.assertEqual(self.access.ask('Timeout', '%1'), {'ok': False, 'reason': 'timeout'})
        self.assertEqual(self.access.state(), {'requests': [], 'grants': [], 'ended': [], 'auto': False})

    def test_grant_ends_when_the_pane_runs_another_chat(self):
        chat = {'%12': ('claude', 41, '100', None)}
        self.access.identity_finder = lambda pane: chat.get(pane, ('gone',))
        self.access.identity_seconds = .05
        grant, _ = self.grant()
        chat['%12'] = ('claude', 41, '100', 'thread-a')  # the chat id appears: same chat
        time.sleep(.3)
        self.assertIn(grant.row['id'], self.access.grants)
        self.assertEqual(grant.identity[3], 'thread-a')
        chat['%12'] = None  # discovery does not know: same chat
        time.sleep(.2)
        self.assertIn(grant.row['id'], self.access.grants)
        chat['%12'] = ('claude', 41, '100', 'thread-b')  # /clear: a new chat in the same process
        wait_for(lambda: grant.row['id'] not in self.access.grants)
        self.assertEqual(grant.end_reason, 'conversation_changed')
        row = self.access.state()['ended'][0]
        self.assertEqual(row['message'], "The agent's chat changed or ended.")

    def test_grant_survives_one_missed_discovery_but_ends_when_the_agent_exits(self):
        answers = iter([('codex', 7, '5', 't'), ('gone',), ('codex', 7, '5', 't')])
        chat = {'now': ('codex', 7, '5', 't')}
        self.access.identity_finder = lambda pane: next(answers, chat['now'])
        self.access.identity_seconds = .05
        grant, _ = self.grant()
        time.sleep(.3)
        self.assertIn(grant.row['id'], self.access.grants)
        chat['now'] = ('gone',)
        wait_for(lambda: grant.row['id'] not in self.access.grants)
        self.assertEqual(grant.end_reason, 'conversation_changed')

    def test_same_chat_rules(self):
        same = signed.same_chat
        self.assertTrue(same(('claude', 1, 's', 'a'), None))
        self.assertTrue(same(('claude', 1, 's', None), ('claude', 1, 's', 'a')))
        self.assertFalse(same(('claude', 1, 's', 'a'), ('claude', 1, 's', 'b')))
        self.assertFalse(same(('claude', 1, 's', 'a'), ('claude', 2, 's', 'a')))
        self.assertFalse(same(('claude', 1, 's', 'a'), ('gone',)))
        self.assertTrue(same(('gone',), ('gone',)))

    def test_validation_shortening_limit_and_exact_contract(self):
        for duration in (0, 61, True, 1.2, '15'):
            with self.assertRaises(ValueError):
                self.access.ask('Task', '%1', duration)
        for pane in (None, '', '%１２', '%1\n'):
            with self.assertRaises(ValueError):
                self.access.ask('Task', pane)
        with self.assertRaises(ValueError):
            self.access.ask('Task', '%1', url='chrome://settings')
        ident, thread, results = self.ask(duration=10, url='example.com')
        with self.assertRaises(ValueError):
            self.access.allow(ident, 11)
        self.access.allow(ident, 5)
        thread.join(timeout=2)
        result = results[0]
        self.assertEqual(set(result), {'ok', 'cdp', 'tab', 'expires', 'browser'})
        self.assertAlmostEqual(result['expires'], time.time() + 300, delta=3)
        self.assertRegex(result['cdp'], r'^ws://127\.0\.0\.1:\d+/cdp-signed/[A-Za-z0-9_-]{43}$')
        grant = self.access.grants[ident]
        row = self.real.tab(result['tab']).row()
        self.assertEqual((row['mode'], row['owner'], row['controller'], row['grant']),
                         ('agent', {'pane': '%12'}, 'agent', {'id': ident, 'expires': result['expires']}))
        self.assertEqual(row['url'], 'https://example.com')
        self.grant('%13')
        third, waiter, _ = self.ask('%14')
        with self.assertRaisesRegex(ValueError, 'limit is 2'):
            self.access.allow(third)
        self.assertEqual(len(self.access.state()['grants']), 2)
        self.access.deny(third)
        waiter.join(timeout=2)
        self.assertNotIn(grant.token, json.dumps(self.real.state()))

    def test_targets_sessions_events_and_every_forbidden_command(self):
        grant, result = self.grant()
        foreign = self.real.new_tab()['id']
        other_grant, _ = self.grant('%13')
        client = self.client(grant)
        targets = self.command(client, 'Target.getTargets')['result']['targetInfos']
        self.assertEqual([info['targetId'] for info in targets], [result['tab']])
        for target in ('T0', foreign, other_grant.row['tab']):
            for method in ('Target.attachToTarget', 'Target.closeTarget', 'Target.getTargetInfo', 'Target.activateTarget'):
                self.assertIn('error', self.command(client, method, {'targetId': target, 'flatten': True}))
        session = self.command(client, 'Target.attachToTarget', {'targetId': result['tab'], 'flatten': True})['result']['sessionId']
        for method in ('Runtime.enable', 'Page.enable', 'Network.enable', 'Input.insertText', 'DOM.getDocument',
                       'Fetch.enable', 'Emulation.setTouchEmulationEnabled'):
            self.assertIn('result', self.command(client, method, session=session), method)
        forbidden = ['Storage.getCookies', 'Storage.clearDataForOrigin', 'Storage.clearCookies', 'Network.getAllCookies',
                     'Network.clearBrowserCookies', 'Network.clearBrowserCache', 'Network.setCookies', 'Browser.close',
                     'Browser.crash', 'Page.setDownloadBehavior', 'Browser.setPermission',
                     'Browser.grantPermissions', 'Browser.getBrowserCommandLine', 'SystemInfo.getInfo', 'SystemInfo.getProcessInfo',
                     'Target.attachToBrowserTarget', 'Target.createBrowserContext', 'Target.disposeBrowserContext',
                     'Target.sendMessageToTarget', 'Target.autoAttachRelated', 'Future.dangerousCommand']
        for method in forbidden:
            for at in (None, session):
                before = len(self.fake.commands)
                self.assertIn('error', self.command(client, method, session=at), (method, at))
                self.assertEqual(len(self.fake.commands), before, method)
        before = len(self.fake.commands)
        self.assertIn('result', self.command(client, 'Browser.setDownloadBehavior', {'behavior': 'allowAndName'}))
        self.assertEqual(len(self.fake.commands), before)
        self.assertIn('error', self.command(client, 'Browser.setDownloadBehavior', {'behavior': 'allow'}, session))
        self.assertIn('error', self.command(client, 'Network.getCookies', {'urls': ['https://other.test']}, session))
        self.assertIn('error', self.command(client, 'Page.navigate', {'url': 'file:///etc/passwd'}, session))
        self.assertIn('error', self.command(client, 'Page.enable', session='S-panel'))
        for params in ({'targetId': foreign}, {'browserContextId': 'default'}, {'nested': [{'targetId': foreign}]}):
            self.assertIn('error', self.command(client, 'Runtime.evaluate', params, session))
        self.command(client, 'Target.setDiscoverTargets', {'discover': True})
        self.command(client, 'Target.setAutoAttach', {'autoAttach': True, 'flatten': True, 'waitForDebuggerOnStart': True})
        # The real browser receives only its own startup discovery, never agent auto-attach.
        self.assertEqual(len(self.fake.sent('Target.setDiscoverTargets')), 1)
        self.assertEqual(self.fake.sent('Target.setAutoAttach'), [])
        before = len(client.transport.messages)
        for method in ('Target.targetCreated', 'Target.targetInfoChanged', 'Target.targetDestroyed'):
            client.target_event(method, {'targetInfo': {'targetId': foreign, 'type': 'page'}, 'targetId': foreign})
        self.assertEqual(len(client.transport.messages), before)

    def test_popup_extra_tabs_disconnect_and_end_keep_saved_tabs(self):
        grant, result = self.grant()
        client = self.client(grant)
        second = self.client(grant)
        self.command(client, 'Target.setAutoAttach', {'autoAttach': True, 'flatten': True})
        self.command(second, 'Target.setAutoAttach', {'autoAttach': True, 'flatten': True})
        popup = {'targetId': 'POPUP', 'type': 'page', 'openerId': result['tab'], 'url': 'about:blank'}
        self.fake.infos['POPUP'] = popup
        self.access.target_event('Target.targetCreated', {'targetInfo': popup})
        wait_for(lambda: 'POPUP' in self.real.tabs and 'POPUP' in client.sessions.values() and 'POPUP' in second.sessions.values())
        self.assertEqual(self.real.tab('POPUP').row()['grant']['id'], grant.row['id'])
        for _ in range(2):
            self.assertIn('result', self.command(client, 'Target.createTarget', {'url': 'https://example.test'}))
        self.assertIn('error', self.command(second, 'Target.createTarget'))
        client.cleanup()
        self.assertTrue(grant.active)
        self.assertEqual(self.access.state()['grants'][0]['id'], grant.row['id'])
        third = self.client(grant)  # Reconnect does not reset the extra-tab allowance.
        self.assertIn('error', self.command(third, 'Target.createTarget'))
        self.access.end(grant.row['id'])
        self.assertTrue(second.transport.closes)
        self.assertTrue(third.transport.closes)
        for target in grant.targets:
            tab = self.real.tab(target)
            self.assertEqual((tab.mode, tab.controller, tab.grant, tab.agent_client), ('saved', 'user', None, None))
        with self.assertRaises(browser.BrowserError):
            self.client(grant)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.access.url(grant).replace('ws:', 'http:'), timeout=2)
        self.assertEqual(error.exception.code, 410)

    def test_expiry_rows_socket_http_dismiss_and_tombstone_lifetime(self):
        now = [time.time()]
        self.access.clock = self.access.history.clock = lambda: now[0]
        grant, result = self.grant(duration=1)
        stream = open_grant_socket(self, self.access, grant)
        grant.timer.cancel()
        now[0] = grant.row['expires']
        grant.timer.function(*grant.timer.args)
        message = 'The time ran out.'
        assert_grant_close(self, stream, message)
        assert_ended_http(self, result['cdp'], 'expired', message)
        handler = FakeHandler(browser_api.PREFIX + 'signed/access')
        browser_api.get(handler, signed_browser=self.real)
        row = handler.replies[0][1]['ended'][0]
        self.assertEqual(row, dict(id=grant.row['id'], pane='%12', agent='codex', browser='server',
                                   reason='expired', message=message, at=now[0]))
        self.assertNotIn(grant.token, json.dumps(handler.replies))
        self.assertNotIn(grant.token, repr(self.access.history.__dict__))
        handler = FakeHandler(browser_api.PREFIX + 'signed/dismiss', {'id': row['id']})
        browser_api.post(handler, signed_browser=self.real)
        self.assertEqual(handler.replies, [(200, dict(browser='signed', requests=[], grants=[], ended=[], auto=False))])
        assert_ended_http(self, result['cdp'], 'expired', message)  # Dismiss does not restore access.
        now[0] += 3599
        assert_ended_http(self, result['cdp'], 'expired', message)
        now[0] += 1
        for url in (result['cdp'], result['cdp'].rsplit('/', 1)[0] + '/unknown'):
            with self.assertRaises(HTTPError) as error:
                urlopen(url.replace('ws:', 'http:'), timeout=2)
            error.exception.close()
            self.assertEqual(error.exception.code, 404)

    def test_tab_close_stop_and_tether_end_reasons(self):
        grant, result = self.grant()
        self.access.target_event('Target.targetDestroyed', {'targetId': grant.row['tab']})
        wait_for(lambda: grant.row['id'] not in self.access.grants)
        self.assertEqual(self.access.state()['ended'][0]['reason'], 'tab_closed')
        assert_ended_http(self, result['cdp'], 'tab_closed', 'The tab was closed.')
        other, result = self.grant()
        handler = FakeHandler(browser_api.PREFIX + 'signed/end', {'id': other.row['id']})
        browser_api.post(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][0], 200)
        self.assertEqual(len(self.access.state()['ended']), 1)
        assert_ended_http(self, result['cdp'], 'tether_end', 'It was ended in Tether.')
        last, result = self.grant()
        self.real.stop()
        self.assertEqual(self.access.state()['ended'][-1]['reason'], 'browser_stopped')
        assert_ended_http(self, result['cdp'], 'browser_stopped', 'The browser stopped.')

    def test_expired_token_is_explained_before_timer_callback(self):
        grant, result = self.grant()
        grant.row['expires'] = time.time() - 1
        assert_ended_http(self, result['cdp'], 'expired', 'The time ran out.')
        self.assertEqual(self.access.state()['ended'][0]['reason'], 'expired')

    def test_timer_tab_close_browser_stop_and_handoff(self):
        grant, result = self.grant()
        client = self.client(grant)
        session = self.command(client, 'Target.attachToTarget', {'targetId': result['tab'], 'flatten': True})['result']['sessionId']
        self.real.set_device(result['tab'], 'phone')
        for method in ('Emulation.setDeviceMetricsOverride', 'Emulation.setLocaleOverride', 'Network.setUserAgentOverride'):
            self.assertIn('error', self.command(client, method, session=session))
        self.real.set_device(result['tab'], 'agent')
        self.assertIn('result', self.command(client, 'Emulation.setDeviceMetricsOverride', {'width': 800, 'height': 600}, session))
        self.assertIn('metrics', grant.emulation[result['tab']])
        self.access.target_event('Target.targetDestroyed', {'targetId': result['tab']})
        wait_for(lambda: not self.access.state()['grants'])
        self.assertTrue(client.closed)
        grant2, _ = self.grant()
        client2 = self.client(grant2)
        grant2.timer.cancel()
        grant2.row['expires'] = time.time() - 1
        before = len(self.fake.commands)
        client2.handle({'id': 888, 'method': 'Browser.getVersion'})
        self.assertEqual(len(self.fake.commands), before)
        self.assertTrue(client2.closed)
        # Exercise the same callback that the real expiry timer invokes.
        grant2.timer.function(*grant2.timer.args)
        self.assertEqual(self.real.tab(grant2.row['tab']).mode, 'saved')
        grant3, _ = self.grant()
        self.real.stop()
        self.assertFalse(grant3.active)
        self.assertEqual(self.access.state()['grants'], [])

    def test_timer_revokes_sockets_before_waiting_for_inflight_commands(self):
        # The production timer must revoke even while another worker holds Browser.lock.
        with patch.object(signed, 'minutes', return_value=1), \
                patch.object(signed.threading, 'Timer', wraps=threading.Timer) as timer:
            grant, _ = self.grant()
        self.assertAlmostEqual(timer.call_args.args[0], 60, delta=1)
        client = self.client(grant)
        grant.timer.cancel()
        with self.real.lock:
            timer_thread = threading.Timer(.01, self.access.end, args=(grant.row['id'],))
            timer_thread.start()
            wait_for(lambda: client.closed)
            self.assertFalse(grant.active)
            self.assertEqual(self.access.state()['grants'], [])
        timer_thread.join(timeout=2)
        self.assertFalse(timer_thread.is_alive())
        self.assertEqual(self.real.tab(grant.row['tab']).mode, 'saved')

    def test_popup_close_reaches_all_clients_and_browser_tab_session_is_hidden(self):
        grant, _ = self.grant()
        clients = [self.client(grant), self.client(grant)]
        popup = {'targetId': 'POPUP', 'type': 'page', 'openerId': grant.row['tab'], 'url': 'about:blank'}
        self.fake.infos['POPUP'] = popup
        self.access.target_event('Target.targetCreated', {'targetInfo': popup})
        wait_for(lambda: 'POPUP' in self.real.tabs)
        for client in clients:
            self.command(client, 'Target.setDiscoverTargets', {'discover': True})
            attached = self.command(client, 'Target.attachToTarget', {'targetId': 'POPUP', 'flatten': True})
            self.assertNotEqual(attached['result']['sessionId'], self.real.tab('POPUP').session)
        self.access.target_event('Target.targetDestroyed', {'targetId': 'POPUP'})
        for client in clients:
            self.assertNotIn('POPUP', client.sessions.values())
            self.assertTrue(any(event.get('method') == 'Target.detachedFromTarget' for event in client.transport.messages))
        self.assertNotIn('POPUP', grant.targets)
        for params in ({'targetId': []}, {'sessionId': {}}, {'nested': {'browserContextId': None}}):
            self.assertIn('error', self.command(clients[0], 'Browser.getVersion', params))
        for client in clients:
            before = len(client.transport.messages)
            client.event({'method': 'Target.attachedToTarget', 'sessionId': 'other', 'params': {'targetInfo': {'targetId': 'T0'}}})
            self.assertEqual(len(client.transport.messages), before)

    def test_panel_routes_and_stream_lists(self):
        ident, thread, _ = self.ask()
        handler = FakeHandler(browser_api.PREFIX + 'signed/access')
        browser_api.get(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][1]['requests'][0]['id'], ident)
        handler = FakeHandler(browser_api.PREFIX + 'signed/allow', {'id': ident, 'minutes': 2})
        browser_api.post(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][0], 200)
        thread.join(timeout=2)
        handler = FakeHandler(browser_api.PREFIX + 'tabs?browser=signed')
        browser_api.get(handler, signed_browser=self.real)
        state = handler.replies[0][1]
        self.assertEqual(state['requests'], [])
        self.assertEqual(state['grants'][0]['id'], ident)
        target = state['grants'][0]['tab']
        handler = FakeHandler('unused')
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = Mock()
        frame = (1, {'deviceWidth': 1280, 'deviceHeight': 800}, 'jpeg', 0)
        def changed(*args):
            self.access.end(ident)
            return frame
        def closed(*args):
            self.real.tabs.pop(target)
            return None
        with patch.object(self.real, 'first_frame', return_value=frame), \
                patch.object(self.real, 'wait_frame', side_effect=lambda *args: changed() if self.access.grants else closed()), \
                own_time(browser_api, sleep=Mock()):
            browser_api.stream(handler, self.real, target, 640, 480, 'signed')
        self.assertIn('"id": "' + ident + '"', handler.wfile.getvalue().decode())
        self.assertIn('"requests": []', handler.wfile.getvalue().decode())
        self.assertIn('"grants": []', handler.wfile.getvalue().decode())
        handler = FakeHandler(browser_api.PREFIX + 'signed/end', {'id': ident})
        browser_api.post(handler, signed_browser=self.real)
        self.assertEqual(handler.replies[0][0], 200)
        denied, waiter, results = self.ask()
        handler = FakeHandler(browser_api.PREFIX + 'signed/deny', {'id': denied})
        browser_api.post(handler, signed_browser=self.real)
        waiter.join(timeout=2)
        self.assertEqual(results, [{'ok': False, 'reason': 'denied'}])

    def test_loopback_host_origin_and_cli_intake(self):
        # Start only a private bridge. No real tmux environment update or socket lookup occurs.
        with tempfile.TemporaryDirectory() as folder:
            proxy = browser_cdp.Proxy(config_path=Path(folder) / 'config.json', env_runner=Mock(), pane_finder=lambda *a: '%12')
            proxy.signed_access = mac_browser.RoutedAccess(self.access, mac_browser.MacAccess(mac_browser.Broker()))
            proxy.start()
            self.addCleanup(proxy.stop)
            url = proxy.url.replace('ws:', 'http:').replace('/cdp/', '/ask-signed/')
            results = []
            def request():
                with urlopen(Request(url, data=json.dumps({'text': 'Requested task'}).encode()), timeout=5) as response:
                    results.append(json.load(response))
            thread = threading.Thread(target=request, daemon=True)
            self.waiters.append(thread)
            thread.start()
            wait_for(lambda: self.access.state()['requests'])
            ident = self.access.state()['requests'][0]['id']
            self.access.deny(ident)
            thread.join(timeout=3)
            self.assertEqual(results, [{'ok': False, 'reason': 'denied'}])
        grant, _ = self.grant()
        endpoint = self.access.url(grant).replace('ws:', 'http:')
        for headers in ({'Origin': 'http://evil.test'}, {'Host': 'evil.test'}, {'Host': 'localhost:1'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(endpoint, headers=headers), timeout=2)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.access.httpd.server_address[0], '127.0.0.1')


class AutomaticAccessTests(unittest.TestCase):
    def setUp(self):
        self.fake = SignedChrome()
        self.real = browser.Browser(signed=True, profile=Path('/nonexistent/signed-auto-test'),
                                    chrome_finder=lambda: '/fake/chrome', launcher=self.fake.launcher)
        self.notified = threading.Event()
        self.notifier = Mock(side_effect=lambda *args, **kwargs: self.notified.set())
        self.auto = True
        self.identity = ('codex', 123, 'start', 'thread')
        self.server = signed.Access(self.real, self.notifier, auto=lambda: self.auto,
                                    identity_finder=lambda pane: self.identity, request_seconds=3)
        self.server._listen = Mock()
        self.routed = mac_browser.RoutedAccess(self.server, mac_browser.MacAccess(mac_browser.Broker()))
        self.addCleanup(self.fake.close)
        self.addCleanup(self.real.stop)
        self.addCleanup(self.routed.stop)

    def ask(self, pane='%12', **kwargs):
        return self.routed.ask('Read the requested account', pane, browser='server', **kwargs)

    def phone(self, verified=True, allow=False):
        self.notified.clear()
        results = []
        waiter = threading.Thread(target=lambda: results.append(self.ask(verified=verified)))
        waiter.start()
        try:
            self.assertTrue(self.notified.wait(2))
            self.assertTrue(self.notifier.call_args.kwargs['urgent'])
            row, = self.routed.state()['requests']
            (self.routed.allow if allow else self.routed.deny)(row['id'])
        finally:
            waiter.join(timeout=4)
            self.assertFalse(waiter.is_alive())
        return results[0]

    def test_verified_auto_reuses_an_active_grant_and_enforces_total_limit(self):
        result = self.ask(verified=True)
        self.assertTrue(result['ok'])
        tabs = set(self.real.tabs)
        grant, = self.routed.state()['grants']
        self.assertTrue(grant['auto'])
        self.assertTrue(self.routed.state()['auto'])
        self.assertEqual(self.routed.ask('Same task', '%12', browser='chrome', verified=True), result)
        self.assertEqual(set(self.real.tabs), tabs)
        self.assertTrue(self.ask('%13', verified=True)['ok'])
        self.assertEqual(self.ask('%14', verified=True), {
            'ok': False, 'reason': 'busy', 'error': 'End a grant first (the limit is 2).'})
        self.assertEqual(len(self.routed.state()['grants']), 2)
        self.assertFalse(self.server.pending)
        self.notifier.assert_not_called()

    def test_unverified_or_missing_identity_or_disabled_policy_uses_phone(self):
        cases = [(False, True, self.identity), (True, False, self.identity), (True, True, None),
                 (True, True, ('gone',)), (True, True, ('codex', 123, 'start'))]
        for auto, verified, identity in cases:
            with self.subTest(auto=auto, verified=verified, identity=identity):
                self.auto, self.identity = auto, identity
                self.assertEqual(self.phone(verified), {'ok': False, 'reason': 'denied'})
        self.assertFalse(self.server.grants)

    def test_manual_end_holds_process_until_phone_allow(self):
        original = self.ask(verified=True)
        grant, = self.routed.state()['grants']
        self.routed.end(grant['id'])
        self.identity = (*self.identity[:3], 'another-thread')
        self.assertEqual(self.phone(), {'ok': False, 'reason': 'denied'})
        allowed = self.phone(allow=True)
        self.assertTrue(allowed['ok'])
        self.assertNotEqual(allowed['tab'], original['tab'])
        grant, = self.routed.state()['grants']
        self.assertFalse(grant['auto'])
        self.server.end(grant['id'], 'expired')
        self.assertTrue(self.ask(verified=True)['ok'])
        self.assertTrue(self.routed.state()['grants'][0]['auto'])

    def test_expiry_reuses_saved_tab_with_a_new_token_and_navigates(self):
        original = self.ask(verified=True)
        tabs = set(self.real.tabs)
        grant, = self.routed.state()['grants']
        self.server.end(grant['id'], 'expired')
        result = self.ask(url='https://example.test/next', verified=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['tab'], original['tab'])
        self.assertNotEqual(result['cdp'], original['cdp'])
        self.assertEqual(self.real.tab(result['tab']).url, 'https://example.test/next')
        self.assertEqual(set(self.real.tabs), tabs)
        self.notifier.assert_not_called()

    def test_user_end_during_tab_creation_prevents_an_automatic_grant(self):
        self.assertTrue(self.ask(verified=True)['ok'])
        grant, = self.routed.state()['grants']
        self.identity = (*self.identity[:3], 'another-thread')
        new_tab = self.real.new_tab

        def end_before_open(*args):
            self.routed.end(grant['id'])
            return new_tab(*args)

        with patch.object(self.real, 'new_tab', side_effect=end_before_open):
            self.assertEqual(self.ask(verified=True),
                             {'ok': False, 'reason': 'failed', 'error': 'This chat needs phone approval.'})
        self.assertFalse(self.routed.state()['grants'])
        self.assertFalse(self.server.pending)
        self.assertEqual(self.phone(), {'ok': False, 'reason': 'denied'})

    def test_auto_does_not_reuse_a_tab_that_is_gone_controlled_or_used_by_mak(self):
        identity = self.identity

        def watched(tab): tab.viewers = 1
        def typed(tab): tab.last_input = self.real.clock() + 1
        def moved(tab): tab.url = 'https://bank.example/transfer'
        def other_process(tab): self.identity = ('claude', 999, 'later', 'thread')
        changes = {'removed': lambda tab: self.real.tabs.pop(tab.target), 'controlled': lambda tab: setattr(tab, 'agent_client', object()),
                   'watched': watched, 'typed': typed, 'moved': moved, 'other_process': other_process}
        for name, change in changes.items():
            with self.subTest(name):
                self.identity = identity
                original = self.ask(verified=True)
                grant, = self.routed.state()['grants']
                self.server.end(grant['id'], 'expired')
                tab = self.real.tab(original['tab'])
                change(tab)
                result = self.ask(verified=True)
                self.assertTrue(result['ok'])
                self.assertNotEqual(result['tab'], original['tab'])
                if name in ('watched', 'typed', 'moved'):
                    self.assertEqual(tab.mode, 'saved')
                    self.assertIsNone(tab.grant)
                self.server.end(self.routed.state()['grants'][0]['id'], 'expired')
                for target in [row for row in list(self.real.tabs) if row != result['tab']]:
                    self.real.tabs.pop(target)

    def test_auto_failure_removes_slot_and_hides_capability_urls_and_paths(self):
        for message, error in [
            ('Chrome refused the tab.', 'Chrome refused the tab.'),
            ('Failed to open /private/profile', 'The signed-in browser could not open a tab.'),
            ('Failed at ws://localhost:1/cdp-signed/secret', 'The signed-in browser could not open a tab.'),
        ]:
            with self.subTest(message=message), patch.object(self.real, 'new_tab', side_effect=browser.BrowserError(message)):
                self.assertEqual(self.ask(verified=True), {'ok': False, 'reason': 'failed', 'error': error})
                self.assertFalse(self.server.pending)
        self.notifier.assert_not_called()
        self.assertTrue(self.ask(verified=True)['ok'])

    def test_policy_route_persists_strict_booleans_without_ending_live_grants(self):
        proxy = Mock(signed_access=self.routed)
        with patch.object(paths, 'CONFIG', paths.CONFIG / signed.secrets.token_hex(8)):
            self.server.auto = signed.agents_allowed
            file = paths.CONFIG / 'signed-browser-agents'
            self.assertTrue(signed.agents_allowed())
            result = self.ask(verified=True)
            for value in ({}, {'auto': 0}, {'auto': 'off'}, {'auto': None}):
                handler = FakeHandler(browser_api.PREFIX + 'signed/policy', value)
                browser_api.post(handler, the_proxy=proxy, signed_browser=self.real)
                self.assertEqual(handler.replies[0][0], 400)
                self.assertFalse(file.exists())
            for auto in (False, True):
                version = self.real.version
                handler = FakeHandler(browser_api.PREFIX + 'signed/policy', {'auto': auto})
                browser_api.post(handler, the_proxy=proxy, signed_browser=self.real)
                status, state = handler.replies[0]
                self.assertEqual((status, state['browser'], state['auto']), (200, 'signed', auto))
                self.assertEqual(state['grants'][0]['tab'], result['tab'])
                self.assertGreater(self.real.version, version)
                self.assertEqual(file.read_text().strip(), 'on' if auto else 'off')
                self.assertIs(signed.agents_allowed(), auto)
                if not auto:
                    self.assertEqual(self.phone(), {'ok': False, 'reason': 'denied'})
            self.assertEqual(file.parent.stat().st_mode & 0o777, 0o700)
            file.write_text('other')
            self.assertTrue(signed.agents_allowed())


REAL_CLIENT = r'''
process.on('uncaughtException', error => {console.log(JSON.stringify({error:error.stack})); process.exit(1)});
const {chromium} = require('playwright-core');
(async () => {
  const connection = await chromium.connectOverCDP(process.env.E2E_CDP_URL, {timeout:15000});
  const pages = connection.contexts()[0].pages();
  if (pages.length !== 1) throw Error('Expected exactly one granted page: ' + pages.length);
  await pages[0].goto(process.env.E2E_PAGE_URL);
  const title = await pages[0].title();
  console.log(JSON.stringify({ready:true, pages:pages.length, title}));
  await new Promise(resolve => connection.on('disconnected', resolve));
  let refused = false;
  try { await chromium.connectOverCDP(process.env.E2E_CDP_URL, {timeout:3000}); }
  catch { refused = true; }
  console.log(JSON.stringify({disconnected:true, refused}));
})().catch(error => {console.log(JSON.stringify({error:error.stack})); process.exit(1)});
'''


@unittest.skipUnless(shutil.which('google-chrome') and shutil.which('node') and playwright_modules(),
                     'Google Chrome, node, and ui/npm ci are required')
class RealSignedAccessTests(Requests, unittest.TestCase):
    def test_allow_playwright_navigation_end_and_token_refusal(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = b'<title>Signed grant test</title><h1>Only this tab</h1>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        with tempfile.TemporaryDirectory(prefix='tether-signed-grant-real-') as folder:
            self.real = browser.Browser(signed=True, profile=Path(folder) / 'profile',
                                        chrome_finder=lambda: shutil.which('google-chrome'))
            self.access = signed.Access(self.real)
            self.waiters = []
            page_server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
            page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
            page_thread.start()
            node = None
            try:
                self.real.new_tab('about:blank')  # Mak's unrelated saved tabs must remain invisible.
                grant, result = self.grant()
                print(json.dumps({'chrome_pid': self.real.pid}), flush=True)
                node = subprocess.Popen(['timeout', '45', 'node', '-e', REAL_CLIENT], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                                        env=dict(os.environ, NODE_PATH=playwright_modules(), E2E_CDP_URL=result['cdp'],
                                                 E2E_PAGE_URL=f'http://127.0.0.1:{page_server.server_port}/'))
                print(json.dumps({'node_timeout_pid': node.pid}), flush=True)
                lines = queue.Queue()
                def read():
                    for line in node.stdout:
                        lines.put(line)
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                first = lines.get(timeout=25)
                print(first, end='', flush=True)
                self.assertEqual(json.loads(first), {'ready': True, 'pages': 1, 'title': 'Signed grant test'})
                self.access.end(grant.row['id'])
                last = lines.get(timeout=10)
                print(last, end='', flush=True)
                self.assertEqual(json.loads(last), {'disconnected': True, 'refused': True})
                self.assertEqual(node.wait(timeout=5), 0)
                self.assertEqual(self.real.tab(grant.row['tab']).mode, 'saved')
            finally:
                if node:
                    if node.poll() is None:
                        os.killpg(node.pid, signal.SIGTERM)
                        node.wait(timeout=5)
                    reader.join(timeout=2)
                    node.stdout.close()
                self.access.stop()
                self.real.stop()
                if self.real.watcher:
                    self.real.watcher.join(timeout=2)
                for waiter in self.waiters:
                    waiter.join(timeout=2)
                page_server.shutdown()
                page_server.server_close()
                page_thread.join(timeout=2)
