"""Real loopback WebSockets, fake browser and encoder. No live CDP or terminals."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import base64
import json
import select
import socket
import struct
import threading
import time
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import browser
import browser_api
import browser_cdp
import browser_live
from test_h264 import jpeg, AVCC


class FakeBrowser:
    def __init__(self):
        self.tab = SimpleNamespace(target='T1', last_input=0)
        self.tabs = {'T1': self.tab}
        self.version = 0
        self.changed = threading.Condition()
        self.picture = None
        self.inputs = []
        self.unwatched = threading.Event()
        self.watches = []
        self.refreshes = self.touches = 0
        self.clock = time.monotonic
        self.accept_frame = True

    def watch(self, target, width, height, css=None):
        if target == 'broken':
            raise browser.BrowserError('Browser unavailable.')
        if target != 'T1':
            raise ValueError('Choose a tab.')
        self.watches.append((target, width, height, css))
        return self.tab

    def unwatch(self, tab, size, css):
        self.unwatch_args = (tab, size, css)
        self.unwatched.set()

    def state(self):
        return {'tabs': [{'id': 'T1'}]}

    def nav_state(self, target):
        return {'back': False, 'forward': True}

    def refresh(self, tab):
        with self.changed:
            self.refreshes += 1
            self.changed.notify_all()

    def touch(self):
        self.touches += 1

    def heal(self, tab, picture):
        return self.accept_frame

    def first_frame(self, tab):
        return self.wait_frame(tab, 0, .01)

    def wait_frame(self, tab, seen, timeout):
        with self.changed:
            self.changed.wait_for(lambda: 'T1' not in self.tabs or (self.picture and self.picture[0] > seen), timeout)
            return self.picture if 'T1' in self.tabs and self.picture and self.picture[0] > seen else None

    def put(self, seq, data=None, **metadata):
        meta = {'deviceWidth': 320, 'deviceHeight': 240, 'offsetTop': 3,
                'scrollOffsetX': 4, 'scrollOffsetY': 5, 'pageScaleFactor': 1.25, **metadata}
        with self.changed:
            self.picture = (seq, meta, base64.b64encode(data or jpeg()).decode(), 7)
            self.changed.notify_all()

    def input(self, target, events):
        if {'type': 'text', 'text': 'browser-error'} in events:
            raise browser.BrowserError('Input failed.')
        with self.changed:
            self.inputs.extend((target, event) for event in events)
            self.changed.notify_all()


class FakeEncoder:
    instances = []

    def __init__(self, callback, **kwargs):
        self.callback = callback
        self.closed = threading.Event()
        self.pushed = []
        self.output_times = {}
        self.instances.append(self)

    def push(self, data, header):
        self.output_times[header['seq']] = time.monotonic()
        if not self.pushed:
            self.callback('config', {'codec': 'avc1.640028', 'width': 320, 'height': 240}, AVCC)
        self.pushed.append(header['seq'])
        self.callback('video', dict(header, key=True, dup=False), b'nals')
        self.callback('video', dict(header, key=False, dup=True), b'dup')

    def close(self):
        self.closed.set()


class Client:
    def __init__(self, address, path, origin=None, key=None, extra=()):
        self.socket = socket.create_connection(address, timeout=2)
        self.file = self.socket.makefile('rb')
        host = '%s:%d' % address
        headers = ['GET ' + path + ' HTTP/1.1', 'Host: ' + host, 'Origin: ' + (origin or 'http://' + host),
                   'Upgrade: websocket', 'Connection: keep-alive, Upgrade', 'Sec-WebSocket-Version: 13',
                   'Sec-WebSocket-Key: ' + (key or base64.b64encode(b'0123456789abcdef').decode()), *extra]
        self.socket.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode())
        self.status = int(self.file.readline().split()[1])
        self.headers = {}
        while True:
            line = self.file.readline()
            if line == b'\r\n' or not line:
                break
            name, value = line.decode().split(':', 1)
            self.headers[name.lower()] = value.strip()

    def send(self, body):
        self.raw(json.dumps(body).encode())

    def raw(self, data, opcode=1):
        size = len(data)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += b'\xfe' + struct.pack('!H', size)
        else:
            head += b'\xff' + struct.pack('!Q', size)
        mask = b'abcd'
        self.socket.sendall(head + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))

    def packet(self):
        head = browser_cdp._exact(self.file, 2)
        assert head[0] & 0x80 and not head[1] & 0x80
        size = head[1] & 127
        if size == 126:
            size = struct.unpack('!H', browser_cdp._exact(self.file, 2))[0]
        elif size == 127:
            size = struct.unpack('!Q', browser_cdp._exact(self.file, 8))[0]
        return head[0] & 15, browser_cdp._exact(self.file, size)

    def message(self, kind=None):
        for _ in range(100):
            opcode, data = self.packet()
            if opcode == 9:
                self.raw(data, opcode=10)
                continue
            if opcode != 2:
                raise AssertionError('Unexpected opcode: %s' % opcode)
            length = struct.unpack('!I', data[:4])[0]
            header = json.loads(data[4:4 + length])
            if kind is None or header['type'] == kind:
                return header, data[4 + length:]
        raise AssertionError('Message not received: ' + str(kind))

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.file.close()
        self.socket.close()


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.browser = FakeBrowser()
        self.clients = []
        FakeEncoder.instances = []
        self.finder = lambda _: None
        self.factory = FakeEncoder
        test = self
        original = browser_live.serve
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with patch.object(browser_live, 'serve', partial(original, finder=test.finder, encoder_factory=test.factory)):
                    browser_api.get(self, test.browser, signed_browser=test.browser)

            def respond(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        for client in self.clients:
            client.close()
        if self.browser.watches:
            self.assertTrue(self.browser.unwatched.wait(3), 'Stream did not unwatch')
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(all(encoder.closed.is_set() for encoder in FakeEncoder.instances))

    def connect(self, query='', **kwargs):
        client = Client(self.server.server_address, '/mobile/api/browser/live?tab=T1' + query, **kwargs)
        self.clients.append(client)
        return client

    def test_bad_origin_and_duplicate_origin(self):
        self.assertEqual(self.connect(origin='https://evil.invalid').status, 403)
        host = 'http://%s:%d' % self.server.server_address
        self.assertEqual(self.connect(extra=['Origin: ' + host]).status, 403)
        self.assertFalse(self.browser.watches)

    def test_bad_key(self):
        for key in ('garbage', base64.b64encode(b'short').decode()):
            self.assertEqual(self.connect(key=key).status, 400)
        self.assertFalse(self.browser.watches)

    def test_other_handshake_fields(self):
        # Duplicate fields leave the first value in place, so use a direct handler for these rejections.
        from email.message import Message
        for field, value, status in (('Origin', None, 403), ('Upgrade', 'http', 400),
                                     ('Connection', 'close', 400), ('Sec-WebSocket-Version', '12', 400)):
            headers = Message()
            for name, content in {'Host': 'localhost', 'Origin': 'http://localhost', 'Upgrade': 'websocket',
                                  'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
                                  'Sec-WebSocket-Key': base64.b64encode(b'0123456789abcdef').decode()}.items():
                if name != field:
                    headers[name] = content
            if value is not None:
                headers[field] = value
            handler = Mock(headers=headers)
            browser_live.serve(handler, self.browser, 'T1', 320, 240)
            self.assertEqual(handler.respond.call_args.args[0], status)
        self.assertFalse(self.browser.watches)

    def test_start_cast_allows_six_frames_in_flight(self):
        owner = SimpleNamespace(conn=Mock())
        tab = SimpleNamespace(controller='user', casting=None, session='S-T1')
        browser.Browser._start_cast(owner, tab, (320, 240))
        self.assertEqual(owner.conn.call.call_args.args[1]['maxFramesInFlight'], 6)

    def test_unknown_browser_and_tab_fail_before_upgrade(self):
        for query, status in (('&browser=unknown', 400), ('&tab=other', 400), ('&tab=broken', 503)):
            # A second tab parameter would select the first; construct the requested target explicitly.
            path = '/mobile/api/browser/live?tab=' + ('broken' if 'broken' in query else 'other')
            if 'browser' in query:
                path += query
            client = Client(self.server.server_address, path)
            self.clients.append(client)
            self.assertEqual(client.status, status)

    def test_hello_jpeg_and_unwatch_on_disconnect(self):
        client = self.connect('&w=640&h=480&cssw=320&cssh=240')
        self.assertEqual(client.status, 101)
        self.assertEqual(client.message(), ({'type': 'hello', 'codec': 'jpeg'}, b''))
        client.close()
        self.assertTrue(self.browser.unwatched.wait(2))
        self.assertEqual(self.browser.watches, [('T1', 640, 480, (320, 240))])
        self.assertEqual(self.browser.unwatch_args[1:], ((640, 480), (320, 240)))

    def test_h264_request_without_ffmpeg(self):
        client = self.connect('&codec=h264')
        self.assertEqual(client.message()[0], {'type': 'hello', 'codec': 'jpeg'})
        self.assertFalse(FakeEncoder.instances)

    def test_jpeg_frame_format_and_tabs(self):
        self.browser.put(1)
        client = self.connect('&browser=signed')
        self.assertEqual(client.message()[0]['codec'], 'jpeg')
        tabs, payload = client.message('tabs')
        self.assertEqual(tabs, {'type': 'tabs', 'tabs': [{'id': 'T1'}], 'browser': 'signed',
                                'nav': {'back': False, 'forward': True}})
        expected = json.loads(browser.frame_event(self.browser.picture, 'signed').split(b'data: ')[1])
        del expected['jpeg']
        self.assertEqual(client.message('frame'), (dict(expected, type='frame'), jpeg()))
        self.assertEqual(payload, b'')

    def test_socket_ack_advances_flow(self):
        self.browser.put(1)
        flows = []
        original = browser_api.Flow
        def flow():
            instance = original()
            flows.append(instance)
            return instance
        with patch.object(browser_api, 'Flow', flow):
            client = self.connect()
            client.message('frame')
            self.browser.put(2, scrollOffsetY=6)
            self.assertEqual(client.message('frame')[0]['seq'], 2)
            self.browser.put(3, scrollOffsetY=7)
            time.sleep(.08)
            self.assertEqual(flows[0].sent, [1, 2])
            start = time.monotonic()
            client.send({'type': 'ack', 'seq': 2})
            self.assertEqual(client.message('frame')[0]['seq'], 3)
            self.assertLess(time.monotonic() - start, .5)
            self.assertEqual(flows[0].acked, 2)

    def test_input_order_errors_and_socket_remains_open(self):
        events = [{'type': 'text', 'text': text} for text in ('one', 'two', 'three')]
        client = self.connect()
        client.message('hello')
        client.send({'type': 'input', 'events': events[:1]})
        client.send({'type': 'input', 'events': events[1:2]})
        for invalid in ('bad', events[:1] * 201):
            client.send({'type': 'input', 'events': invalid})
            self.assertEqual(client.message('error')[0]['error'], 'Invalid input.')
        # A browser failure in the input worker is reported, and later input still goes through.
        # Wait for the first two events first, so the failing event cannot share their batch.
        self.assert_input_events(events[:2])
        client.send({'type': 'input', 'events': [{'type': 'text', 'text': 'browser-error'}]})
        self.assertEqual(client.message('error')[0]['error'], 'Input failed.')
        client.raw(b'not json')
        client.send(['ignore'])
        client.send({'type': 'unknown'})
        client.send({'type': 'input', 'events': events[2:]})
        self.assert_input_events(events)

    def test_queued_input_coalesces_moves_and_preserves_key_order(self):
        started, release = threading.Event(), threading.Event()
        record_input = self.browser.input

        def blocked_input(target, events):
            started.set()
            release.wait()
            record_input(target, events)

        self.browser.input = blocked_input
        client = self.connect()
        client.message('tabs')
        down = {'type': 'keydown', 'key': 'a'}
        move = {'type': 'move', 'x': 4, 'y': 8}
        up = {'type': 'keyup', 'key': 'a'}
        try:
            client.send({'type': 'input', 'events': [down]})
            self.assertTrue(started.wait(1))
            client.send({'type': 'input', 'events': [dict(move, x=2)]})
            client.send({'type': 'input', 'events': [move, up]})
            # The pong confirms the reader queued both batches while input was blocked.
            client.raw(b'queued', opcode=9)
            self.assertEqual(client.packet(), (10, b'queued'))
        finally:
            release.set()
        self.assert_input_events([down, move, up])

    def assert_input_events(self, events):
        with self.browser.changed:
            self.assertTrue(self.browser.changed.wait_for(lambda: len(self.browser.inputs) >= len(events), 1),
                            self.browser.inputs)
            self.assertEqual(self.browser.inputs, [('T1', event) for event in events])

    def test_closed_when_tab_disappears(self):
        client = self.connect()
        client.message('tabs')
        with self.browser.changed:
            self.browser.tabs.clear()
            self.browser.changed.notify_all()
        self.assertEqual(client.message('closed'), ({'type': 'closed'}, b''))

    def test_h264_still_after_idle(self):
        self.finder = lambda _: 'ffmpeg'
        self.browser.put(1)
        client = self.connect('&codec=h264')
        self.assertEqual(client.message(), ({'type': 'hello', 'codec': 'h264'}, b''))
        self.assertEqual(client.message('config')[1], AVCC)
        video, data = client.message('video')
        self.assertEqual((video['seq'], video['key'], video['dup'], data), (1, True, False, b'nals'))
        start = FakeEncoder.instances[0].output_times[1]
        duplicate, _ = client.message('video')
        self.assertTrue(duplicate['dup'])
        still, data = client.message('still')
        self.assertGreaterEqual(time.monotonic() - start, browser_live.STILL_AFTER - .03)
        self.assertLess(time.monotonic() - start, .6)
        self.assertEqual((still['seq'], data), (1, jpeg()))
        self.assertNotIn('key', still)
        self.assertNotIn('dup', still)
        client.send({'type': 'ack', 'seq': 1})
        self.browser.put(2)  # identical picture: skip without another video or still
        time.sleep(.35)
        self.assertEqual(FakeEncoder.instances[0].pushed, [1])
        self.assertFalse(select.select([client.socket], [], [], .05)[0])

    def test_encoder_failure_falls_back_and_continues(self):
        class Failing(FakeEncoder):
            def push(self, data, header):
                self.callback('failed', {'error': 'ffmpeg failed'}, b'')
        self.factory = Failing
        self.finder = lambda _: 'ffmpeg'
        self.browser.put(1)
        client = self.connect('&codec=h264')
        self.assertEqual(client.message('hello')[0]['codec'], 'h264')
        self.assertEqual(client.message('hello')[0]['codec'], 'jpeg')
        self.assertEqual(client.message('frame')[0]['seq'], 1)
        client.send({'type': 'ack', 'seq': 1})
        self.browser.put(2, scrollOffsetY=8)
        self.assertEqual(client.message('frame')[0]['seq'], 2)

    def test_still_timer_with_asynchronous_encoder_output(self):
        class AsyncEncoder(FakeEncoder):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.workers = []

            def push(self, data, header):
                def output():
                    if not self.closed.wait(.04):
                        super(AsyncEncoder, self).push(data, header)
                worker = threading.Thread(target=output, daemon=True)
                self.workers.append(worker)
                worker.start()

            def close(self):
                super().close()
                for worker in self.workers:
                    worker.join(timeout=1)
        self.factory = AsyncEncoder
        self.finder = lambda _: 'ffmpeg'
        self.browser.put(1)
        client = self.connect('&codec=h264')
        for seq in (1, 2):
            client.message('video')
            client.message('video')
            start = FakeEncoder.instances[0].output_times[seq]
            self.assertEqual(client.message('still')[0]['seq'], seq)
            self.assertGreaterEqual(time.monotonic() - start, browser_live.STILL_AFTER - .03)
            self.assertLess(time.monotonic() - start, .5)
            client.send({'type': 'ack', 'seq': seq})
            if seq == 1:
                self.browser.put(2, scrollOffsetY=8)

    def test_oversized_message_closes_1009(self):
        client = self.connect()
        client.message('tabs')
        client.socket.sendall(b'\x81\xff' + struct.pack('!Q', browser_live.MAX_MESSAGE + 1))
        opcode, data = client.packet()
        self.assertEqual((opcode, struct.unpack('!H', data)[0]), (8, 1009))

    def test_keep_warm_ping_idle_and_refresh(self):
        self.browser.tab.last_input = time.monotonic()
        client = self.connect()
        client.message('tabs')
        self.assertEqual(client.packet(), (9, b''))
        client.raw(b'ping', opcode=9)
        self.assertEqual(client.packet(), (10, b'ping'))
        self.browser.tab.last_input = 0
        with patch.object(browser_api, 'KEEPALIVE', .1):
            self.assertEqual(client.message('idle'), ({'type': 'idle'}, b''))
        with self.browser.changed:
            self.assertTrue(self.browser.changed.wait_for(lambda: self.browser.refreshes > 0, 2))
        self.assertGreater(self.browser.touches, 0)

    def test_heal_skip_duplicates_and_tabs_version(self):
        self.browser.put(1)
        self.browser.accept_frame = False
        client = self.connect()
        client.message('tabs')
        time.sleep(.05)
        self.assertFalse(select.select([client.socket], [], [], .05)[0])
        self.browser.accept_frame = True
        self.browser.put(2)
        self.assertEqual(client.message('frame')[0]['seq'], 2)
        client.send({'type': 'ack', 'seq': 2})
        self.browser.put(3)  # duplicate, but still advances the capture sequence
        time.sleep(.05)
        self.assertFalse(select.select([client.socket], [], [], .05)[0])
        self.browser.version += 1
        self.browser.put(4, scrollOffsetY=99)
        self.assertEqual(client.message('frame')[0]['seq'], 4)
        client.send({'type': 'ack', 'seq': 4})
        self.assertEqual(client.message('tabs')[0]['tabs'], [{'id': 'T1'}])

    def test_failed_write_stops_encoder_and_unwatches(self):
        from email.message import Message
        headers = Message()
        for name, value in {'Host': 'localhost', 'Origin': 'http://localhost', 'Upgrade': 'websocket',
                            'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
                            'Sec-WebSocket-Key': base64.b64encode(b'0123456789abcdef').decode()}.items():
            headers[name] = value
        handler = Mock(headers=headers)
        reads = threading.Event()
        def read(size):
            reads.wait(2)
            return b''
        handler.rfile.read.side_effect = read
        handler.connection.shutdown.side_effect = lambda *args: reads.set()
        writes = []
        def write(data):
            writes.append(data)
            if b'"type":"video"' in data:
                raise BrokenPipeError
        handler.wfile.write.side_effect = write
        self.browser.put(1)
        browser_live.serve(handler, self.browser, 'T1', 320, 240, codec='h264',
                           finder=lambda _: 'ffmpeg', encoder_factory=FakeEncoder)
        self.assertTrue(any(b'"type":"video"' in data for data in writes))
        self.assertTrue(self.browser.unwatched.is_set())
        self.assertTrue(FakeEncoder.instances[0].closed.is_set())


if __name__ == '__main__':
    unittest.main()
