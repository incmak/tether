"""Agent CDP contract tests. Terminal writes and browser transport use isolated fakes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import errno
import json
from email.message import Message
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_cdp as cdp
from test_browser import FakeChrome, FakeHandler


class Chrome(FakeChrome):
    def __init__(self):
        self.context_number = self.browser_number = 0
        self.infos = {'T0': {'targetId': 'T0', 'type': 'page', 'url': 'about:blank'}}
        super().__init__()

    def answer(self, message):
        method, params = message['method'], message.get('params', {})
        if method == 'Browser.getVersion':
            return {'product': 'Chrome/147.0.1.2', 'userAgent': 'HeadlessChrome/147.0.1.2'}
        if method == 'Target.attachToBrowserTarget':
            self.browser_number += 1
            return {'sessionId': 'B%d' % self.browser_number}
        if method == 'Target.createBrowserContext':
            self.context_number += 1
            return {'browserContextId': 'C%d' % self.context_number}
        if method == 'Target.getBrowserContexts':
            return {'browserContextIds': ['C%d' % n for n in range(1, self.context_number + 1)]}
        if method == 'Target.createTarget':
            result = super().answer(message)
            info = {'targetId': result['targetId'], 'type': 'page', 'browserContextId': params.get('browserContextId'),
                    'url': params.get('url', 'about:blank'), 'title': ''}
            self.infos[info['targetId']] = info
            self.emit({'method': 'Target.targetCreated', 'params': {'targetInfo': info}})
            return result
        if method == 'Target.getTargets':
            return {'targetInfos': list(self.infos.values())}
        if method == 'Target.getTargetInfo':
            return {'targetInfo': self.infos.get(params.get('targetId'), {'targetId': 'browser', 'type': 'browser'})}
        if method == 'Page.getLayoutMetrics':
            return {'cssVisualViewport': {'clientWidth': 777, 'clientHeight': 555}}
        return super().answer(message)


class Capture:
    def __init__(self):
        self.messages, self.closes, self.close_reasons = [], [], []
        self.changed = threading.Condition()

    def message(self, message):
        with self.changed:
            self.messages.append(message)
            self.changed.notify_all()

    def close(self, code=1000, reason=''):
        self.closes.append(code)
        self.close_reasons.append(reason)

    def reply(self, ident):
        with self.changed:
            deadline = time.monotonic() + 3
            while True:
                for message in self.messages:
                    if message.get('id') == ident:
                        return message
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError('No reply to %d: %r' % (ident, self.messages))
                self.changed.wait(left)


def masked(opcode, payload=b'', final=True):
    key = b'abcd'
    data = cdp.frame(opcode, payload)
    if not final:
        data = bytes([data[0] & 0x7f]) + data[1:]
    header_size = 2 if len(payload) < 126 else (4 if len(payload) < 65536 else 10)
    header = bytes([data[0], data[1] | 0x80]) + data[2:header_size]
    return header + key + bytes(value ^ key[index % 4] for index, value in enumerate(payload))


class FramingTests(unittest.TestCase):
    def test_close_reason_fits_utf8_control_frame(self):
        transport = object.__new__(cdp.Transport)
        transport.send = Mock()
        for reason in ('Access ended: The tab was closed.', 'Access ended: ' + '界' * 80):
            transport.close(4000, reason)
            opcode, payload = transport.send.call_args.args
            self.assertEqual(opcode, 8)
            self.assertEqual(struct.unpack('!H', payload[:2])[0], 4000)
            self.assertLessEqual(len(payload), 125)
            text = payload[2:].decode('utf-8')
            self.assertTrue(reason.startswith(text))
            self.assertGreaterEqual(len(payload), min(len(reason.encode()) + 2, 123))

    def read(self, data, limit=cdp.MAX_MESSAGE):
        controls = []
        messages = list(cdp.websocket_messages(io.BytesIO(data), lambda *args: controls.append(args), limit))
        return messages, controls

    def test_masked_text_fragments_ping_pong_close(self):
        data = (masked(1, b'{"x":', False) + masked(9, b'ping') + masked(10, b'pong')
                + masked(0, b'1}') + masked(8, struct.pack('!H', 1000)))
        messages, controls = self.read(data)
        self.assertEqual(messages, ['{"x":1}'])
        self.assertEqual(controls, [(10, b'ping'), (8, struct.pack('!H', 1000))])

    def test_extended_lengths_and_unlimited_server_frames(self):
        for size in (126, 65536, cdp.MAX_MESSAGE + 1):
            payload = b'a' * size
            output = cdp.frame(1, payload)
            self.assertEqual(output[-size:], payload)
            if size <= 65536:
                self.assertEqual(self.read(masked(1, payload) + masked(8))[0], [payload.decode()])

    def test_size_limit_checks_header_and_combined_fragments(self):
        for data in (masked(1, b'12345'), masked(1, b'123', False) + masked(0, b'45')):
            with self.assertRaises(cdp.WebSocketError) as error:
                self.read(data, limit=4)
            self.assertEqual(error.exception.code, 1009)
        # No payload allocation/read when a 32 MiB header exceeds the cap.
        with self.assertRaises(cdp.WebSocketError) as error:
            self.read(b'\x81\xff' + struct.pack('!Q', cdp.MAX_MESSAGE + 1))
        self.assertEqual(error.exception.code, 1009)

    def test_bad_mask_opcode_fragment_control_utf8_and_close(self):
        cases = [(cdp.frame(1, b'x'), 1002), (masked(0, b'x'), 1002),
                 (masked(1, b'x', False) + masked(1, b'y'), 1002), (masked(9, b'x', False), 1002),
                 (masked(8, b'x'), 1002), (masked(8, struct.pack('!H', 1005)), 1002),
                 (masked(1, b'\xff'), 1007), (masked(2, b'x'), 1003)]
        for data, code in cases:
            with self.subTest(code=code, data=data):
                with self.assertRaises(cdp.WebSocketError) as error:
                    self.read(data)
                self.assertEqual(error.exception.code, code)

    def test_reader_replies_and_sink_registration_do_not_wait_on_writer_lock(self):
        connection = object.__new__(browser.Connection)
        connection.lock = threading.Lock()
        connection.waiting = {7: Mock()}
        connection.sinks = {}
        connection.on_event = Mock()
        callback = connection.waiting[7]
        with connection.lock:
            connection._dispatch(b'{"id":7,"result":{}}')
            connection.register_sink('child', Mock())
            connection._dispatch(b'{"method":"Runtime.ready","sessionId":"child"}')
            connection.unregister_sink('child')
        callback.assert_called_once_with({'id': 7, 'result': {}})


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.sock, self.peer = socket.socketpair()
        self.peer.settimeout(3)
        self.transport = cdp.Transport(self.sock)

    def tearDown(self):
        self.transport.abort()
        self.transport.writer.join(timeout=2)
        self.sock.close()
        self.peer.close()
        self.assertFalse(self.transport.writer.is_alive())

    def receive(self, expected):
        received = bytearray()
        while len(received) < len(expected):
            chunk = self.peer.recv(len(expected) - len(received))
            self.assertTrue(chunk)
            received.extend(chunk)
        self.assertEqual(received, expected)

    def test_small_frame_burst_stays_connected_and_preserves_order(self):
        payloads = [struct.pack('!I', index) + b'x' * 64 for index in range(5000)]
        for payload in payloads:
            self.transport.send(1, payload)
        self.assertFalse(self.transport.closed)
        self.receive(b''.join(cdp.frame(1, payload) for payload in payloads))
        self.assertFalse(self.transport.closed)

    def test_pending_bytes_over_limit_aborts(self):
        payload = b'x' * (self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) + 1)
        size = len(cdp.frame(1, payload))
        with patch.object(cdp, 'MAX_QUEUED_BYTES', 2 * size):
            self.transport.send(1, payload)
            self.transport.send(1, payload)
            self.assertFalse(self.transport.closed)
            self.transport.send(1, payload)
            self.assertTrue(self.transport.closed)

    def test_pending_bytes_returns_to_zero_after_frames_drain(self):
        payload = b'x' * 65536
        for _ in range(3):
            self.transport.send(1, payload)
        self.receive(cdp.frame(1, payload) * 3)
        deadline = time.monotonic() + 2
        while self.transport.pending_bytes and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(self.transport.pending_bytes, 0)
        self.assertFalse(self.transport.closed)

    def test_single_frame_over_limit_is_allowed_when_nothing_is_pending(self):
        payload = b'x' * 65536
        with patch.object(cdp, 'MAX_QUEUED_BYTES', 16):
            self.transport.send(1, payload)
            self.assertFalse(self.transport.closed)
            self.receive(cdp.frame(1, payload))

    def test_large_devanagari_reply_and_event_stay_connected(self):
        block = 'अ' * 1_000_000
        prefix = b'{"id": 1, "result": {"value": "'
        suffix = b'"}}'
        event = {'method': 'Runtime.consoleAPICalled', 'params': {}}
        self.transport.message({'id': 1, 'result': {'value': block * 12}})
        self.transport.message(event)
        self.assertFalse(self.transport.closed)
        encoded = block.encode()
        length = len(prefix) + 12 * len(encoded) + len(suffix)
        with self.peer.makefile('rb') as stream:
            self.assertEqual(stream.read(10), struct.pack('!BBQ', 0x81, 127, length))
            self.assertEqual(stream.read(len(prefix)), prefix)
            for _ in range(12):
                self.assertEqual(stream.read(len(encoded)), encoded)
            self.assertEqual(stream.read(len(suffix)), suffix)
            expected = cdp.frame(1, json.dumps(event).encode())
            self.assertEqual(stream.read(len(expected)), expected)

    def test_64_mib_reply_with_32_mib_of_queued_events_stays_connected(self):
        block = b'A' * (1024 * 1024)
        prefix = b'{"id": 1, "result": {"data": "'
        suffix = b'"}}'
        length = 64 * len(block) - 10
        self.transport.message({'id': 1, 'result': {'data': 'A' * (length - len(prefix) - len(suffix))}})
        event_prefix = b'{"method": "Runtime.consoleAPICalled", "params": {"value": "'
        event_length = len(block) - 10 - len(event_prefix) - len(suffix)
        event = {'method': 'Runtime.consoleAPICalled', 'params': {'value': 'A' * event_length}}
        for _ in range(32):
            self.transport.message(event)
        self.assertFalse(self.transport.closed)
        with self.peer.makefile('rb') as stream:
            self.assertEqual(stream.read(10), struct.pack('!BBQ', 0x81, 127, length))
            self.assertEqual(stream.read(len(prefix)), prefix)
            for _ in range(63):
                self.assertEqual(stream.read(len(block)), block)
            remaining = length - 63 * len(block) - len(prefix) - len(suffix)
            self.assertEqual(stream.read(remaining), block[:remaining])
            self.assertEqual(stream.read(len(suffix)), suffix)
            expected = cdp.frame(1, event_prefix + block[:event_length] + suffix)
            for _ in range(32):
                self.assertEqual(stream.read(len(expected)), expected)

    def test_message_with_lone_surrogate_is_valid_json_text(self):
        reply = {'id': 1, 'result': {'value': 'अ\ud800'}}
        self.transport.message(reply)
        with self.peer.makefile('rb') as stream:
            opcode, size = stream.read(2)
            self.assertEqual(opcode, 0x81)
            payload = stream.read(size).decode('utf-8')
        self.assertIn(r'\ud800', payload)
        self.assertEqual(json.loads(payload), reply)


class ConfigTests(unittest.TestCase):
    def test_new_config_port_is_in_range(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config/agent-browser.json'
            config = cdp.load_config(path)
            self.assertTrue(29000 <= config['port'] <= 29999)
            self.assertEqual(json.loads(path.read_text()), config)

    def test_saved_port_migrates_and_preserves_token_and_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config/agent-browser.json'
            for port in (47281, 28999, 30000):
                for enabled in (True, False):
                    with self.subTest(port=port, enabled=enabled):
                        saved = {'enabled': enabled, 'port': port, 'token': 'a' * 43}
                        cdp.save_config(path, saved)
                        config = cdp.load_config(path)
                        self.assertTrue(29000 <= config['port'] <= 29999)
                        self.assertEqual(config, dict(saved, port=config['port']))
                        self.assertEqual(json.loads(path.read_text()), config)

    def test_saved_in_range_port_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config/agent-browser.json'
            for port in (29000, 29999):
                with self.subTest(port=port):
                    saved = {'enabled': True, 'port': port, 'token': 'a' * 43}
                    cdp.save_config(path, saved)
                    with patch.object(cdp.secrets, 'randbelow') as random_port:
                        self.assertEqual(cdp.load_config(path), saved)
                    random_port.assert_not_called()


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.fake = Chrome()
        self.browser = browser.Browser(launcher=self.fake.launcher, chrome_finder=lambda: '/fake/chrome')
        self.env, self.notify = Mock(), Mock()
        self.proxy = cdp.Proxy(self.browser, Path(self.folder.name) / 'config/agent-browser.json',
                               notifier=self.notify, env_runner=self.env)
        self.client = self.connect('%12')
        self.ident = 0

    def tearDown(self):
        for client in list(self.proxy.clients):
            client.cleanup()
        self.fake.close()
        # Synchronize with the shared reader before temporary files vanish.
        self.browser.conn.reader.join(timeout=2) if self.browser.conn else None
        self.folder.cleanup()

    def connect(self, pane):
        client = cdp.AgentClient(self.proxy, Capture(), pane)
        self.proxy.clients.add(client)
        client.open()
        return client

    def request(self, method, params=None, session=None, client=None):
        self.ident += 1
        client = client or self.client
        message = {'id': self.ident, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        client.handle(message)
        return client.transport.reply(self.ident)

    def test_global_ids_and_root_child_remapping(self):
        second = self.connect(None)
        self.client.handle({'id': 900, 'method': 'Browser.getVersion'})
        second.handle({'id': 900, 'method': 'Browser.getVersion'})
        self.assertEqual(self.client.transport.reply(900)['id'], 900)
        self.assertEqual(second.transport.reply(900)['id'], 900)
        commands = self.fake.sent('Browser.getVersion')[-2:]
        self.assertNotEqual(commands[0]['id'], commands[1]['id'])
        self.assertEqual({cmd['sessionId'] for cmd in commands}, {self.client.root, second.root})
        self.assertNotIn('sessionId', self.client.transport.reply(900))
        self.client.sessions['child'] = 'owned'
        self.browser.conn.register_sink('child', self.client.event)
        reply = self.request('Runtime.evaluate', {'expression': '1'}, 'child')
        self.assertEqual(reply['sessionId'], 'child')
        self.assertIn('error', self.request('Runtime.evaluate', session='foreign'))

    def test_targets_contexts_default_rewrite_and_user_startup_hidden(self):
        result = self.request('Target.createTarget', {'url': 'about:blank'})['result']
        target = result['targetId']
        self.assertEqual(self.fake.sent('Target.createTarget')[-1]['params']['browserContextId'], self.client.default)
        targets = self.request('Target.getTargets')['result']['targetInfos']
        self.assertEqual([info['targetId'] for info in targets], [target])
        self.assertEqual(self.request('Target.getBrowserContexts')['result'], {'browserContextIds': []})
        context = self.request('Target.createBrowserContext')['result']['browserContextId']
        self.assertEqual(self.request('Target.getBrowserContexts')['result'], {'browserContextIds': [context]})
        for method in cdp.SCOPED:
            self.assertNotIn('error', self.request(method))
            self.assertEqual(self.fake.sent(method)[-1]['params']['browserContextId'], self.client.default)
            self.assertIn('error', self.request(method, {'browserContextId': 'foreign'}))
        for method in cdp.TARGET_COMMANDS:
            self.assertIn('error', self.request(method, {'targetId': 'T0'}))
        self.assertEqual(self.request('Target.getTargetInfo')['result']['targetInfo']['type'], 'browser')
        self.assertIn('error', self.request('Target.disposeBrowserContext', {'browserContextId': 'foreign'}))
        self.assertIn('error', self.request('Target.createTarget', {'browserContextId': 'foreign'}))

    def test_foreign_attachment_is_resumed_detached_and_not_forwarded(self):
        before = len(self.client.transport.messages)
        event = {'method': 'Target.attachedToTarget', 'sessionId': self.client.root,
                 'params': {'sessionId': 'foreign-child', 'waitingForDebugger': True,
                            'targetInfo': {'targetId': 'T0', 'browserContextId': 'foreign'}}}
        self.browser.conn._dispatch(json.dumps(event).encode())
        deadline = time.monotonic() + 2
        while not self.fake.sent('Target.detachFromTarget') and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.client.transport.messages), before)
        detach = self.fake.sent('Target.detachFromTarget')[-1]
        self.assertEqual(detach['params'], {'sessionId': 'foreign-child'})
        self.assertEqual(detach['sessionId'], self.client.root)
        self.assertTrue(self.fake.sent('Runtime.runIfWaitingForDebugger'))
        self.assertNotIn('foreign-child', self.client.sessions)

    def test_owned_nested_sessions_events_and_foreign_lifecycle_filter(self):
        info = {'targetId': 'owned', 'browserContextId': self.client.default, 'type': 'worker'}
        self.client.event({'method': 'Target.attachedToTarget', 'sessionId': self.client.root,
                           'params': {'sessionId': 'child', 'targetInfo': info}})
        self.assertNotIn('sessionId', self.client.transport.messages[-1])
        info2 = dict(info, targetId='worker2')
        self.browser.conn._dispatch(json.dumps({'method': 'Target.attachedToTarget', 'sessionId': 'child',
                                              'params': {'sessionId': 'nested', 'targetInfo': info2}}).encode())
        self.assertEqual(self.client.transport.messages[-1]['sessionId'], 'child')
        self.assertIn('nested', self.client.sessions)
        before = len(self.client.transport.messages)
        for method, params in [('Target.targetCreated', {'targetInfo': {'targetId': 'foreign', 'browserContextId': 'foreign'}}),
                               ('Target.targetInfoChanged', {'targetInfo': {'targetId': 'foreign', 'browserContextId': 'foreign'}}),
                               ('Target.targetDestroyed', {'targetId': 'T0'}),
                               ('Target.detachedFromTarget', {'sessionId': 'foreign'})]:
            self.client.event({'method': method, 'params': params, 'sessionId': self.client.root})
        self.assertEqual(len(self.client.transport.messages), before)
        self.client.event({'method': 'Target.detachedFromTarget', 'sessionId': self.client.root,
                           'params': {'sessionId': 'child'}})
        self.assertNotIn('child', self.browser.conn.sinks)

    def test_tab_limit_and_popup_closure(self):
        for _ in range(3):
            self.assertNotIn('error', self.request('Target.createTarget'))
        count = len(self.fake.sent('Target.createTarget'))
        self.assertEqual(self.request('Target.createTarget')['error']['message'], cdp.TAB_ERROR)
        self.assertEqual(len(self.fake.sent('Target.createTarget')), count)
        popup = {'targetId': 'popup', 'type': 'page', 'browserContextId': self.client.default, 'openerId': 'T2'}
        self.client.target_event('Target.targetCreated', {'targetInfo': popup})
        deadline = time.monotonic() + 2
        while not self.fake.sent('Target.closeTarget') and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.fake.sent('Target.closeTarget')[-1]['params'], {'targetId': 'popup'})
        self.assertNotIn('popup', self.client.targets)
        target = next(iter(self.client.targets))
        self.client.target_event('Target.targetDestroyed', {'targetId': target})
        self.assertNotIn('error', self.request('Target.createTarget'))

    def test_close_crash_swallowed_and_unknown_logged_once(self):
        for method in cdp.SWALLOWED:
            self.assertEqual(self.request(method)['result'], {})
            self.assertFalse(self.fake.sent(method))
        with self.assertLogs(cdp.LOG, level='WARNING') as logs:
            for _ in range(2):
                reply = self.request('Browser.executeBrowserCommand')
                self.assertEqual(reply['error'], {'code': -32601, 'message': 'Not allowed through Tether.'})
        self.assertEqual(len(logs.output), 1)
        self.assertIn('error', self.request('Target.attachToBrowserTarget'))

    def test_cleanup_disposes_all_contexts_and_detaches_root(self):
        context = self.request('Target.createBrowserContext')['result']['browserContextId']
        self.client.cleanup()
        disposed = {cmd['params']['browserContextId'] for cmd in self.fake.sent('Target.disposeBrowserContext')}
        self.assertEqual(disposed, {self.client.default, context})
        self.assertEqual(self.fake.sent('Target.detachFromTarget')[-1]['params'], {'sessionId': self.client.root})
        self.assertFalse(self.proxy.busy())
        self.assertNotIn(self.client.root, self.browser.conn.sinks)

    def test_browser_stop_closes_clients_with_1011_and_idle_watcher_sees_busy(self):
        self.proxy.browser_stopped()
        self.assertEqual(self.client.transport.closes, [1011])
        self.assertTrue(self.proxy.busy())
        self.browser.used = -browser.IDLE_SECONDS * 2
        with patch.object(self.browser.idle_stop, 'wait', side_effect=[False, RuntimeError('end watch')]), \
             patch.object(self.browser, 'stop') as stop:
            with self.assertRaisesRegex(RuntimeError, 'end watch'):
                self.browser._watch(self.browser.idle_stop)
            stop.assert_not_called()
        self.assertGreater(self.browser.used, 0)

    def test_panel_adoption_size_no_emulation_and_panel_close_keeps_context(self):
        target = self.request('Target.createTarget')['result']['targetId']
        deadline = time.monotonic() + 2
        while target not in self.browser.tabs and time.monotonic() < deadline:
            time.sleep(.01)
        with self.browser.lock:
            tab = self.browser.tabs[target]
            row = tab.row()
            self.assertEqual((row['mode'], row['owner'], row['width'], row['height']), ('agent', {'pane': '%12'}, 777, 555))
        self.assertFalse([cmd for cmd in self.fake.sent('Emulation.setDeviceMetricsOverride') if cmd.get('sessionId') == tab.session])
        self.browser._apply_preset(tab, 'pixel')
        self.assertTrue(self.browser.heal(tab, (1, {'deviceWidth': 321, 'deviceHeight': 456}, '', 4)))
        self.assertEqual(tab.size, (321, 456))
        handler = FakeHandler('/mobile/api/browser/device', {'tab': target, 'preset': 'desktop'})
        browser_api.post(handler, self.browser, self.proxy)
        self.assertEqual(handler.replies[0][0], 200)
        self.assertEqual(handler.replies[0][1]['controller'], 'user')
        self.browser.close_tab(target)
        self.assertFalse(self.fake.sent('Target.disposeBrowserContext'))

    def adopted(self):
        target = self.request('Target.createTarget')['result']['targetId']
        deadline = time.monotonic() + 2
        while target not in self.browser.tabs and time.monotonic() < deadline:
            time.sleep(.01)
        with self.browser.lock:  # wait for adoption, including the client reference
            tab = self.browser.tabs[target]
        session = self.request('Target.attachToTarget', {'targetId': target, 'flatten': True})['result']['sessionId']
        return tab, session

    def test_device_handoff_preserves_exact_agent_emulation_and_rejects_competing_writes(self):
        tab, session = self.adopted()
        commands = {
            'Emulation.setDeviceMetricsOverride': {'width': 901, 'height': 601, 'mobile': False, 'deviceScaleFactor': 1.5,
                                                  'screenOrientation': {'type': 'landscapePrimary', 'angle': 90}},
            'Emulation.setTouchEmulationEnabled': {'enabled': True, 'maxTouchPoints': 2},
            'Emulation.setUserAgentOverride': {'userAgent': 'Agent test UA', 'acceptLanguage': 'fr'},
        }
        for method, params in commands.items():
            self.assertNotIn('error', self.request(method, params, session))
        self.browser.watch(tab.target, 400, 800)
        row = self.browser.set_device(tab.target, 'phone')
        self.assertEqual((row['mode'], row['controller'], row['preset'], row['width'], row['height'], row['touch']),
                         ('agent', 'user', 'pixel', 412, 915, True))
        self.assertEqual(row['owner'], {'pane': '%12'})
        count = len(self.fake.sent('Emulation.setDeviceMetricsOverride'))
        reply = self.request('Emulation.setDeviceMetricsOverride', {'width': 1000}, session)
        self.assertIn('user controls', reply['error']['message'])
        self.assertEqual(len(self.fake.sent('Emulation.setDeviceMetricsOverride')), count)
        self.assertTrue(self.browser.heal(tab, (1, {'deviceWidth': 412, 'deviceHeight': 915}, '', 4)))
        self.assertEqual(tab.row()['width'], 412)
        self.assertIsNone(tab.frame)
        row = self.browser.set_device(tab.target, 'desktop', 500, 700)
        self.assertEqual((row['width'], row['height'], row['controller']), (500, 700, 'user'))
        restored = self.browser.set_device(tab.target, 'agent')
        self.assertEqual(restored['controller'], 'agent')
        for method, params in commands.items():
            self.assertEqual(self.fake.sent(method)[-1]['params'], params)
        self.assertEqual(self.fake.sent('Emulation.setEmitTouchEventsForMouse')[-1]['params'], {'enabled': False})
        self.assertNotIn('error', self.request('Emulation.setDeviceMetricsOverride', commands['Emulation.setDeviceMetricsOverride'], session))
        self.browser.unwatch(tab)

    def test_handoff_without_agent_overrides_clears_panel_emulation_and_is_idempotent(self):
        tab, session = self.adopted()
        self.browser.set_device(tab.target, 'phone')
        self.browser.set_device(tab.target, 'agent')
        for method, params in browser.AGENT_DEFAULTS.values():
            self.assertEqual(self.fake.sent(method)[-1]['params'], params)
        count = len(self.fake.sent('Emulation.clearDeviceMetricsOverride'))
        self.browser.set_device(tab.target, 'agent')
        self.assertEqual(len(self.fake.sent('Emulation.clearDeviceMetricsOverride')), count)
        self.assertNotIn('error', self.request('Emulation.clearDeviceMetricsOverride', session=session))
        self.browser.set_device(tab.target, 'tablet')
        self.browser.set_device(tab.target, 'agent')
        self.assertEqual(len(self.fake.sent('Emulation.clearDeviceMetricsOverride')), count + 2)

    def test_phone_alias_for_user_tab_and_invalid_handoff_do_not_change_agent_tab(self):
        tab, _ = self.adopted()
        with self.assertRaisesRegex(ValueError, 'Unknown device'):
            self.browser.set_device(tab.target, 'toaster')
        self.assertEqual(tab.controller, 'agent')
        user = self.browser.new_tab()
        self.assertEqual(self.browser.set_device(user['id'], 'phone')['width'], 412)
        with self.assertRaisesRegex(ValueError, 'no agent'):
            self.browser.set_device(user['id'], 'agent')

    def test_failed_agent_emulation_does_not_replace_the_saved_viewport(self):
        tab, session = self.adopted()
        params = {'width': 900, 'height': 600, 'deviceScaleFactor': 1, 'mobile': False}
        self.assertNotIn('error', self.request('Emulation.setDeviceMetricsOverride', params, session))
        with patch.object(self.browser.conn, 'call', side_effect=browser.BrowserError('Invalid parameters')):
            rejected = self.request('Emulation.setDeviceMetricsOverride', {'width': -1}, session)
        self.assertIn('Invalid parameters', rejected['error']['message'])
        self.browser.set_device(tab.target, 'phone')
        self.browser.set_device(tab.target, 'agent')
        self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params'], params)

    def test_live_agent_screenshots_keep_the_css_pixel_default_and_explicit_density(self):
        tab, session = self.adopted()
        with patch.object(self.browser, 'container', object()):
            self.assertNotIn('error', self.request('Page.captureScreenshot', session=session))
            default = {'width': 0, 'height': 0, 'deviceScaleFactor': 1, 'mobile': False}
            metrics = self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]
            self.assertEqual((metrics['sessionId'], metrics['params']), (session, default))
            self.assertLess(metrics['id'], self.fake.sent('Page.captureScreenshot')[-1]['id'])

            clip = {'x': 13, 'y': 19, 'width': 125, 'height': 80, 'scale': 0.75}
            self.assertNotIn('error', self.request('Page.captureScreenshot', {'clip': clip}, session))
            self.assertEqual(self.fake.sent('Page.captureScreenshot')[-1]['params'], {'clip': clip})
            self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1], metrics)

            custom = {'width': 430, 'height': 350, 'deviceScaleFactor': 2, 'mobile': False}
            self.assertNotIn('error', self.request('Emulation.setDeviceMetricsOverride', custom, session))
            self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params'], custom)
            self.assertNotIn('error', self.request('Emulation.clearDeviceMetricsOverride', session=session))
            restored = self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]
            clear = self.fake.sent('Emulation.clearDeviceMetricsOverride')[-1]
            self.assertEqual((clear['sessionId'], restored['params']), (session, default))
            self.assertLess(clear['id'], restored['id'])

            self.browser.set_device(tab.target, 'phone')
            self.browser.set_device(tab.target, 'desktop')
            self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params'], default)
            self.browser.set_device(tab.target, 'agent')
            self.assertEqual(self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]['params'], default)

    def test_agent_tabs_do_not_use_user_tab_limit(self):
        self.request('Target.createTarget')
        with patch.object(browser, 'MAX_TABS', 2):
            self.assertEqual(self.browser.new_tab()['mode'], 'private')
            with self.assertRaises(browser.BrowserError):
                self.browser.new_tab()

    def test_unknown_pane_agent_owner_and_screencast_uses_real_viewport(self):
        second = self.connect(None)
        target = self.request('Target.createTarget', client=second)['result']['targetId']
        deadline = time.monotonic() + 2
        while target not in self.browser.tabs and time.monotonic() < deadline:
            time.sleep(.01)
        with self.browser.lock:
            tab = self.browser.tabs[target]
            self.assertEqual(tab.row()['owner'], {'pane': None})
        self.browser.watch(target, 400, 300)
        self.assertEqual(tab.size, (777, 555))
        self.assertTrue(self.browser.heal(tab, (1, {'deviceWidth': 777, 'deviceHeight': 555}, '', 2)))
        self.browser.unwatch(tab)

    def test_start_fallback_port_and_shutdown_hooks_without_real_sockets_or_tmux(self):
        port = 29281
        self.proxy.config['port'] = port
        cdp.save_config(self.proxy.config_path, self.proxy.config)
        httpd = Mock(server_port=port + 1)
        saved_handler = []
        previous = Mock()
        with patch.object(cdp, 'ThreadingHTTPServer', side_effect=[OSError(errno.EADDRINUSE, 'occupied'), httpd]) as factory, \
             patch.object(cdp.atexit, 'register') as register, \
             patch.object(cdp.signal, 'getsignal', return_value=previous), \
             patch.object(cdp.signal, 'signal', side_effect=lambda sig, handler: saved_handler.append(handler)):
            self.proxy.start(shutdown_hooks=True)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual([call.args[0] for call in factory.call_args_list],
                             [('127.0.0.1', port), ('127.0.0.1', port + 1)])
            self.assertEqual(cdp.load_config(self.proxy.config_path)['port'], port + 1)
            register.assert_called_once_with(self.proxy.stop)
            saved_handler[0](cdp.signal.SIGTERM, None)
            previous.assert_called_once_with(cdp.signal.SIGTERM, None)
            self.assertEqual(self.env.call_args_list[-1].args[0][-2:], ['-gu', 'TETHER_CDP_URL'])
            httpd.shutdown.assert_called_once()
        with patch.object(cdp, 'ThreadingHTTPServer', side_effect=PermissionError(errno.EPERM, 'blocked')) as factory:
            with self.assertRaises(PermissionError):
                self.proxy.start()
            self.assertEqual(factory.call_count, 1)

    def test_busy_top_port_wraps_to_base(self):
        self.proxy.config['port'] = 29999
        cdp.save_config(self.proxy.config_path, self.proxy.config)
        httpd = Mock(server_port=cdp.PORT_BASE)
        with patch.object(cdp, 'ThreadingHTTPServer',
                          side_effect=[OSError(errno.EADDRINUSE, 'occupied'), httpd]) as factory:
            try:
                self.proxy.start()
                self.assertEqual([call.args[0] for call in factory.call_args_list],
                                 [('127.0.0.1', 29999), ('127.0.0.1', cdp.PORT_BASE)])
                self.assertEqual(cdp.load_config(self.proxy.config_path)['port'], cdp.PORT_BASE)
            finally:
                self.proxy.stop()

    def test_all_busy_ports_stay_in_range(self):
        self.proxy.config['port'] = 29999
        with patch.object(cdp, 'ThreadingHTTPServer',
                          side_effect=OSError(errno.EADDRINUSE, 'occupied')) as factory:
            with self.assertRaisesRegex(OSError, 'No free agent browser port'):
                self.proxy.start()
        ports = [call.args[0][1] for call in factory.call_args_list]
        self.assertEqual(ports, [29999, *range(29000, 29999)])

    def test_binary_check_does_not_start_chromium(self):
        self.client.cleanup()
        quiet = browser.Browser(chrome_finder=Mock(return_value='/fake/chrome'))
        proxy = cdp.Proxy(quiet, self.proxy.config_path, env_runner=Mock())
        self.assertTrue(proxy.availability()[0])
        self.assertIsNone(quiet.conn)
        quiet.chrome_finder.return_value = None
        self.assertEqual(proxy.availability(), (False, 'No Chromium found.'))
        self.assertIsNone(quiet.conn)

    @patch.object(browser_api.agent_browser_setup, 'status', return_value={'claude': 'ok', 'codex': 'none'})
    @patch.object(browser_api.agent_browser_setup, 'stale_panes', return_value=[])
    def test_availability_config_env_asks_and_panel_routes(self, _stale, _setup):
        config = self.proxy.config_path
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(self.proxy.config['token']), 43)
        self.assertEqual(cdp.load_config(config), self.proxy.config)
        self.proxy.sync_env()
        command = self.env.call_args_list[-1].args[0]
        self.assertEqual(command, [cdp.paths.TMUX, 'set-environment', '-g', 'TETHER_CDP_URL', self.proxy.url])
        handler = FakeHandler('/mobile/api/browser/agents', {'enabled': False})
        browser_api.post(handler, self.browser, self.proxy)
        self.assertFalse(handler.replies[0][1]['available'])
        self.assertEqual(self.env.call_args_list[-1].args[0][-2:], ['-gu', 'TETHER_CDP_URL'])
        self.assertEqual(self.client.transport.closes, [1011])
        self.proxy.set_enabled(True)
        self.proxy.version_checked('Chrome/119.0.0.0')
        self.assertFalse(self.proxy.state()['available'])
        self.proxy.version_checked('Chrome/120.0.0.0')
        self.assertTrue(self.proxy.state()['available'])
        ask = self.proxy.ask('Please sign in.', '%12')
        self.proxy.ask('Use this tab.', '%12')
        self.assertEqual(len(self.proxy.asks), 1)
        for n in range(25):
            self.proxy.ask('Help', '%%%d' % (100 + n))
        self.assertEqual(len(self.proxy.asks), 20)
        self.assertEqual(self.notify.call_args.kwargs, {'urgent': True})
        self.assertEqual(self.notify.call_args.args[0]['title'], 'Agent needs you in the browser')
        self.assertEqual(self.notify.call_args.args[0]['url'], '/mobile/#session=%25124')
        handler = FakeHandler('/mobile/api/browser/ask/dismiss', {'id': self.proxy.asks[-1]['id']})
        browser_api.post(handler, self.browser, self.proxy)
        self.assertEqual(len(handler.replies[0][1]['asks']), 19)
        handler = FakeHandler('/mobile/api/browser/agents')
        browser_api.get(handler, self.browser, self.proxy)
        self.assertEqual(set(handler.replies[0][1]), {'enabled', 'available', 'reason', 'agents', 'asks', 'setup', 'stale'})
        for text in ('', 'a' * 301):
            with self.assertRaises(ValueError):
                self.proxy.ask(text, None)
        self.proxy.dismiss(ask['id'])


class HTTPTests(unittest.TestCase):
    def handler(self, path, headers=None, proxy=None):
        handler = object.__new__(cdp.Handler)
        handler.path = path
        handler.headers = Message()
        for key, value in (headers or {'Host': '127.0.0.1:47123'}).items():
            handler.headers[key] = value
        handler.server = SimpleNamespace(proxy=proxy or SimpleNamespace(config={'port': 47123, 'token': 'secret'}))
        handler.respond = Mock()
        return handler

    def test_token_origin_and_host_on_all_routes(self):
        for prefix in ('cdp', 'health', 'ask'):
            for token in ('wrong', ''):
                handler = self.handler('/%s/%s' % (prefix, token))
                self.assertIsNone(handler.route())
                handler.respond.assert_called_once_with(404, {})
            for headers in ({'Host': 'evil.test:47123'}, {'Host': '127.0.0.1:80'},
                            {'Host': 'localhost:47123', 'Origin': ''},
                            {'Host': '127.0.0.1:47123', 'Origin': 'http://localhost'}):
                handler = self.handler('/%s/secret' % prefix, headers)
                self.assertIsNone(handler.route())
                handler.respond.assert_called_once_with(403, {})
            for host in ('127.0.0.1:47123', 'localhost:47123'):
                self.assertEqual(self.handler('/%s/secret' % prefix, {'Host': host}).route(), '/' + prefix)
        handler = self.handler('/health/secret')
        handler.headers['Host'] = 'evil.test'
        self.assertIsNone(handler.route())

    def test_unsupported_methods_still_require_token_and_safe_headers(self):
        for path, expected in (('/health/wrong', 404), ('/health/secret', 403)):
            handler = self.handler(path, {'Host': 'evil.test'})
            handler.raw_requestline = ('OPTIONS %s HTTP/1.1\r\n' % path).encode()
            handler.rfile = io.BytesIO(b'Host: evil.test\r\n\r\n')
            self.assertFalse(handler.parse_request())
            handler.respond.assert_called_once_with(expected, {})

    def test_health_discovery_and_max_clients_before_upgrade(self):
        proxy = SimpleNamespace(config={'port': 47123, 'token': 'secret'}, url='ws://127.0.0.1:47123/cdp/secret',
                                browser=SimpleNamespace(product='Chrome/147.0.0.0'), availability=lambda: (True, ''),
                                lock=threading.RLock(), clients=set(range(8)))
        for suffix in ('json/version', 'json/version/'):
            handler = self.handler('/cdp/secret/' + suffix, proxy=proxy)
            handler.do_GET()
            handler.respond.assert_called_once_with(200, {'Browser': 'Chrome/147.0.0.0', 'webSocketDebuggerUrl': proxy.url})
        handler = self.handler('/health/secret', proxy=proxy)
        handler.do_GET()
        handler.respond.assert_called_once_with(200, {'ok': True})
        handler = self.handler('/cdp/secret', {'Host': '127.0.0.1:47123', 'Upgrade': 'websocket', 'Connection': 'Upgrade',
                                             'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'YWJjZGVmZ2hpamtsbW5vcA=='}, proxy)
        handler.do_GET()
        handler.respond.assert_called_once_with(503, {})
        proxy.availability = lambda: (False, 'disabled')
        handler = self.handler('/health/secret', proxy=proxy)
        handler.do_GET()
        handler.respond.assert_called_once_with(503, {'ok': False, 'reason': 'disabled'})

    def test_ask_infers_pane_and_checks_body(self):
        proxy = SimpleNamespace(config={'port': 47123, 'token': 'secret'}, pane_finder=Mock(return_value='%12'),
                                ask=Mock(return_value={'id': 'a'}))
        handler = self.handler('/ask/secret', proxy=proxy)
        handler.connection = Mock()
        handler.client_address = ('127.0.0.1', 55001)
        body = json.dumps({'text': 'Click sign in.'}).encode()
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.do_POST()
        proxy.pane_finder.assert_called_once_with(55001, 47123)
        proxy.ask.assert_called_once_with('Click sign in.', '%12')
        handler.respond.assert_called_once_with(200, {'id': 'a'})

    def test_ask_uses_the_socket_pane_over_the_named_pane(self):
        for found, expected in (('%12', '%12'), (None, '%99')):
            proxy = SimpleNamespace(config={'port': 47123, 'token': 'secret'}, pane_finder=Mock(return_value=found),
                                    ask=Mock(return_value={'id': 'a'}))
            handler = self.handler('/ask/secret', proxy=proxy)
            handler.connection = Mock()
            handler.client_address = ('127.0.0.1', 55001)
            body = json.dumps({'text': 'Click sign in.', 'pane': '%99'}).encode()
            handler.headers['Content-Length'] = str(len(body))
            handler.rfile = io.BytesIO(body)
            handler.do_POST()
            proxy.ask.assert_called_once_with('Click sign in.', expected)

    def test_signed_ask_verifies_only_the_socket_pane(self):
        access = Mock()
        access.ask.return_value = {'ok': True}
        proxy = SimpleNamespace(config={'port': 47123, 'token': 'secret'}, pane_finder=Mock(), signed_access=access)
        for found in ('%12', None):
            with self.subTest(found=found):
                proxy.pane_finder.return_value = found
                handler = self.handler('/ask-signed/secret', proxy=proxy)
                handler.connection = Mock()
                handler.client_address = ('127.0.0.1', 55001)
                body = json.dumps({'text': 'Read the account', 'pane': '%13', 'verified': True,
                                   'browser': 'server'}).encode()
                handler.headers['Content-Length'] = str(len(body))
                handler.rfile = io.BytesIO(body)
                handler.do_POST()
                access.ask.assert_called_with('Read the account', found or '%13', 15, None,
                                              browser='server', verified=bool(found))
                handler.respond.assert_called_once_with(200, {'ok': True})

    def test_signed_policy_is_not_an_agent_proxy_route(self):
        handler = self.handler('/signed/secret/policy')
        handler.do_POST()
        handler.respond.assert_called_once_with(404, {})


class PeerTests(unittest.TestCase):
    def fixture(self, tcp6=False):
        tcp = 'header\n 0: 0100007F:D6D9 0100007F:B813 01 0:0 00:0 0 1001 0 12345\n'
        files = {'/proc/net/tcp': 'header\n' if tcp6 else tcp, '/proc/net/tcp6': tcp if tcp6 else 'header\n',
                 '/proc/42/stat': '42 (node (script)) S 41 0 0', '/proc/41/stat': '41 (shell) S 40 0 0',
                 '/proc/40/stat': '40 (tmux pane) S 1 0 0'}
        return dict(read_text=lambda path: files[path], list_dir=lambda path: ['1', '42'] if path == '/proc' else ['0', '3'],
                    read_link=lambda path: 'socket:[12345]' if path.endswith('/3') else '/dev/null',
                    uid_of=lambda path: 0 if path.endswith('/1') else 1001,
                    pane_reader=lambda timeout: '%12 40\n%14 50\n', uid=1001)

    def test_inode_pid_ancestor_pane_ipv4_ipv6_and_other_user(self):
        for tcp6 in (False, True):
            self.assertEqual(cdp.peer_pane(55001, 47123, **self.fixture(tcp6)), '%12')
        fixture = self.fixture()
        fixture['uid_of'] = lambda path: 0
        self.assertIsNone(cdp.peer_pane(55001, 47123, **fixture))
        self.assertIsNone(cdp.peer_pane(55002, 47123, **self.fixture()))

    def test_missing_process_no_matching_pane_and_two_second_cap(self):
        fixture = self.fixture()
        fixture['pane_reader'] = lambda timeout: ''
        fixture['read_text'] = lambda path: '42 (gone) S 0 0 0' if path.startswith('/proc/42/') else self.fixture()['read_text'](path)
        self.assertIsNone(cdp.peer_pane(55001, 47123, **fixture))
        fixture = self.fixture()
        ticks = iter([0, 0, 0, 3])
        fixture['clock'] = lambda: next(ticks, 3)
        self.assertIsNone(cdp.peer_pane(55001, 47123, **fixture))


if __name__ == '__main__':
    unittest.main()
