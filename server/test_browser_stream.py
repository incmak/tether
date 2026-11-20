"""Capture freshness and stream pacing. All CDP traffic uses FakeChrome."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import browser
import browser_api
from test_browser import make


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.fake, self.browser = make()
        self.addCleanup(self.fake.close)
        self.addCleanup(self.browser.stop)
        self.browser.list_tabs()
        self.tab = self.browser.watch('T1', 2560, 1600, (1280, 800))

    def test_ack_on_receipt_even_without_viewer_consumption(self):
        self.browser.watch('T1', 2560, 1600, (1280, 800))
        self.assertEqual(self.tab.viewers, 2)
        with patch.object(self.browser.conn, 'send') as send:
            for seq in range(1, 5):
                self.browser._event('Page.screencastFrame', {
                    'data': str(seq), 'metadata': {}, 'sessionId': 7}, self.tab.session)
            self.assertEqual(send.call_count, 4)
            self.assertEqual(self.tab.frame[0], 4)
            self.assertEqual(self.browser.wait_frame(self.tab, 1, 0)[0], 4)
            for _ in range(2):
                self.browser.ack(self.tab, self.tab.frame)
            self.assertEqual(send.call_count, 4)

    def test_cap_is_thirty(self):
        self.assertAlmostEqual(browser_api.FRAME_INTERVAL, 1 / 30)

    def test_optional_scroll_metadata(self):
        meta = {'deviceWidth': 1280, 'deviceHeight': 800,
                'scrollOffsetX': 0, 'scrollOffsetY': 123.5, 'pageScaleFactor': 1.25}
        data = json.loads(browser.frame_event((1, meta, 'jpeg', 7)).split(b'data: ')[1])
        for field in ('scrollOffsetX', 'scrollOffsetY', 'pageScaleFactor'):
            self.assertEqual(data.get(field), meta[field])
        data = json.loads(browser.frame_event((2, {}, 'jpeg', 0)).split(b'data: ')[1])
        self.assertNotIn('scrollOffsetY', data)

    def test_click_and_typing_keep_the_sharp_size(self):
        with patch.object(self.browser, '_start_cast') as start:
            self.browser.input('T1', [{'type': 'down', 'x': 5, 'y': 5}, {'type': 'up', 'x': 5, 'y': 5},
                                      {'type': 'keydown', 'key': 'a'}, {'type': 'text', 'text': 'hello'}])
        self.assertEqual(self.tab.casting, (2560, 1600))
        start.assert_not_called()

    def test_input_records_browser_clock_once_before_dispatch(self):
        self.assertEqual(self.tab.last_input, 0.0)
        self.browser.clock = Mock(return_value=123.0)
        with patch.object(self.browser, 'ensure'), patch.object(self.browser.conn, 'call') as call:
            call.side_effect = lambda *args, **kwargs: self.assertEqual(self.tab.last_input, 123.0)
            self.browser.input('T1', [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}])
        self.browser.clock.assert_called_once_with()
        self.assertEqual(call.call_count, 2)

    def test_wheel_drag_and_scroll_keys_keep_the_sharp_size(self):
        events = [{'type': 'wheel', 'x': 5, 'y': 5, 'dx': 0, 'dy': 10},
                  {'type': 'move', 'x': 8, 'y': 9, 'buttons': 1},
                  {'type': 'keydown', 'key': 'ArrowDown'}]
        retained = (1, {}, 'sharp', 7)
        self.tab.frame = retained
        with patch.object(self.browser, '_start_cast') as start, \
                patch.object(self.browser, '_restart_cast') as restart, \
                patch.object(browser.threading, 'Timer') as timer:
            for event in events:
                with self.subTest(event=event):
                    self.browser.input('T1', [event])
                    self.assertEqual(self.tab.casting, (2560, 1600))
                    self.assertIs(self.tab.frame, retained)
            start.assert_not_called()
            restart.assert_not_called()
            timer.assert_not_called()
        self.assertEqual(self.fake.sent('Input.dispatchMouseEvent')[-2]['params']['type'], 'mouseWheel')
        self.assertEqual(self.fake.sent('Input.dispatchMouseEvent')[-1]['params']['type'], 'mouseMoved')
        self.assertEqual(self.fake.sent('Input.dispatchKeyEvent')[-1]['params']['key'], 'ArrowDown')

    def test_phone_touch_keeps_the_sharp_size(self):
        self.browser.set_device('T1', 'pixel')
        with patch.object(self.browser, '_start_cast') as start, \
                patch.object(self.browser, '_restart_cast') as restart, \
                patch.object(browser.threading, 'Timer') as timer:
            self.browser.input('T1', [{'type': 'down', 'x': 5, 'y': 5},
                                      {'type': 'move', 'x': 8, 'y': 9, 'buttons': 1},
                                      {'type': 'up', 'x': 8, 'y': 9}])
            self.assertEqual(self.tab.casting, (2560, 1600))
            start.assert_not_called()
            restart.assert_not_called()
            timer.assert_not_called()
        self.assertEqual([command['params']['type'] for command in self.fake.sent('Input.dispatchTouchEvent')],
                         ['touchStart', 'touchMove', 'touchEnd'])

    def test_largest_viewer_sets_cast_size(self):
        self.browser.watch('T1', 640, 480, (320, 240))
        self.assertEqual(self.tab.casting, (2560, 1600))
        self.browser.unwatch(self.tab, (2560, 1600), (1280, 800))
        self.assertEqual(self.tab.casting, (640, 480))
        with patch.object(browser.threading, 'Timer'):
            self.browser.unwatch(self.tab, (640, 480), (320, 240))
        self.assertIsNone(self.tab.casting)

    def test_unknown_css_size_preserves_full_resolution(self):
        with patch.object(browser.threading, 'Timer'):
            self.browser.unwatch(self.tab)
        self.browser.watch('T1', 2560, 1600)
        self.browser.input('T1', [{'type': 'wheel', 'x': 5, 'y': 5, 'dx': 0, 'dy': 10}])
        self.assertEqual(self.tab.casting, (2560, 1600))

    def test_upscaled_viewers_keep_full_resolution(self):
        with patch.object(browser.threading, 'Timer'):
            self.browser.unwatch(self.tab)
        self.browser.watch('T1', 1600, 1600, (1000, 1000))
        self.browser.input('T1', [{'type': 'wheel', 'x': 5, 'y': 5, 'dx': 0, 'dy': 10}])
        self.assertEqual(self.tab.casting, (1600, 1600))
        self.browser.watch('T1', 1600, 1600)
        self.assertEqual(self.tab.casting, (1600, 1600))
        self.browser.unwatch(self.tab, (1600, 1600))
        self.assertEqual(self.tab.casting, (1600, 1600))

    def test_css_query_validation_and_dispatch(self):
        from test_browser import FakeHandler
        handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&w=2560&h=1600&cssw=1280&cssh=800')
        with patch.object(browser_api, 'stream') as stream:
            browser_api.get(handler, self.browser)
        stream.assert_called_once_with(handler, self.browser, 'T1', 2560, 1600, 'test', css_size=(1280, 800))
        for query in ('cssw=1', 'cssw=-1&cssh=800', 'cssw=nan&cssh=800', 'cssw=99999&cssh=800'):
            handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&' + query)
            browser_api.get(handler, self.browser)
            self.assertEqual(handler.replies[0][0], 400)


class StreamTests(unittest.TestCase):
    def run_idle_stream(self, last_input, lifetime, frames=None, first_frame=None, flow=None):
        now = [100.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        clock.sleep = lambda delay: now.__setitem__(0, now[0] + delay)
        tab = SimpleNamespace(target='T1', last_input=last_input)
        fake = Mock(tabs={'T1': tab}, version=0, clock=lambda: now[0] + 1000)
        fake.watch.return_value = tab
        fake.first_frame.return_value = first_frame
        fake.state.return_value = {}
        fake.nav_state.return_value = {}
        fake.heal.return_value = True
        waits, writes = [], []
        def wait_frame(tab, seen, timeout):
            waits.append(timeout)
            delay, frame = (frames or {}).get(len(waits), (timeout + 1e-9, None))
            now[0] += delay
            return frame
        fake.wait_frame.side_effect = wait_frame
        class Sink(io.BytesIO):
            def write(self, data):
                writes.append((now[0], data))
                return super().write(data)
        with patch.object(browser_api, 'time', clock), patch.object(browser_api, 'STREAM_LIFETIME', lifetime), \
                patch.object(browser_api, 'Flow', return_value=flow):
            browser_api.stream(Mock(wfile=Sink()), fake, 'T1', 1280, 800, sid='dedupe-stream' if flow else None)
        fake.unwatch.assert_called_once_with(tab, (1280, 800), None)
        return waits, writes, fake

    def test_duplicate_is_seen_without_ack_or_pacing_reset(self):
        meta = {'deviceWidth': 1280, 'deviceHeight': 800, 'offsetTop': 0,
                'scrollOffsetX': 0, 'scrollOffsetY': 0, 'pageScaleFactor': 1}
        scrolled = dict(meta, scrollOffsetY=10)
        flow = Mock()
        _, writes, fake = self.run_idle_stream(0, 0.15, {
            1: (0.001, (2, dict(meta, timestamp=999), 'jpeg', 8)),
            2: (0.001, (3, scrolled, 'jpeg', 8)),
            3: (0.001, (4, scrolled, 'changed', 8)),
        }, first_frame=(1, meta, 'jpeg', 7), flow=flow)
        frames = [(when, json.loads(data.split(b'data: ')[1]))
                  for when, data in writes if data.startswith(b'event: frame')]
        self.assertEqual([frame['seq'] for _, frame in frames], [1, 3, 4])
        self.assertAlmostEqual(frames[1][0], 100 + browser_api.FRAME_INTERVAL + 0.002)
        self.assertEqual([args.args[0] for args in flow.sending.call_args_list], [1, 3, 4])
        self.assertEqual([args.args[1] for args in fake.wait_frame.call_args_list[:4]], [1, 2, 3, 4])
        self.assertNotIn('dedupe-stream', browser_api.FLOWS)

    def test_each_display_metadata_field_makes_a_distinct_frame(self):
        meta = {'deviceWidth': 1280, 'deviceHeight': 800, 'offsetTop': 0,
                'scrollOffsetX': 0, 'scrollOffsetY': 0, 'pageScaleFactor': 1}
        for field in meta:
            with self.subTest(field=field):
                _, writes, _ = self.run_idle_stream(0, 0.1, {
                    1: (0.001, (2, dict(meta, **{field: meta[field] + 1}), 'jpeg', 7)),
                }, first_frame=(1, meta, 'jpeg', 7))
                self.assertEqual(sum(data.startswith(b'event: frame') for _, data in writes), 2)

    def test_each_stream_writes_its_first_frame(self):
        for _ in range(2):
            _, writes, _ = self.run_idle_stream(0, 0.1, first_frame=(1, {}, 'jpeg', 7))
            self.assertEqual(sum(data.startswith(b'event: frame') for _, data in writes), 1)

    def test_duplicate_frames_preserve_warm_comments(self):
        _, writes, _ = self.run_idle_stream(1099.0, 0.3, {
            seq: (0.04, (seq + 1, {}, 'jpeg', 7)) for seq in range(1, 9)
        }, first_frame=(1, {}, 'jpeg', 7))
        self.assertEqual(sum(data.startswith(b'event: frame') for _, data in writes), 1)
        comments = [when for when, data in writes if data == b':\n\n']
        self.assertGreaterEqual(len(comments), 2)
        self.assertAlmostEqual(comments[0], 100 + browser_api.FRAME_INTERVAL + 0.08)

    def test_duplicate_frames_preserve_idle_keepalive(self):
        _, writes, _ = self.run_idle_stream(0, 16, {
            seq: (0.1, (seq + 1, {}, 'jpeg', 7)) for seq in range(1, 161)
        }, first_frame=(1, {}, 'jpeg', 7))
        self.assertEqual(sum(data.startswith(b'event: frame') for _, data in writes), 1)
        idle = [when for when, data in writes if data.startswith(b'event: idle')]
        self.assertEqual(len(idle), 1)
        self.assertAlmostEqual(idle[0], 115 + browser_api.FRAME_INTERVAL)

    def test_without_recent_input_preserves_idle_events(self):
        for last_input in (0.0, 1085.0):  # never, or exactly 15 seconds ago
            with self.subTest(last_input=last_input):
                waits, writes, _ = self.run_idle_stream(last_input, 16)
                self.assertTrue(waits)
                self.assertTrue(all(timeout == 1.0 for timeout in waits))
                self.assertFalse(any(data == b':\n\n' for _, data in writes))
                idle = [when for when, data in writes if data.startswith(b'event: idle')]
                self.assertEqual(len(idle), 1)
                self.assertAlmostEqual(idle[0], 115.0)

    def test_recent_input_writes_comments_every_eighty_ms(self):
        waits, writes, _ = self.run_idle_stream(1099.0, 0.3)
        self.assertTrue(waits)
        self.assertTrue(all(timeout == browser_api.KEEP_WARM for timeout in waits))
        comments = [when for when, data in writes if data == b':\n\n']
        self.assertGreaterEqual(len(comments), 3)
        for before, after in zip([100.0] + comments, comments):
            self.assertAlmostEqual(after - before, 0.08)
        self.assertFalse(any(data.startswith(b'event: idle') for _, data in writes))

    def test_frame_write_resets_comment_timer(self):
        _, writes, _ = self.run_idle_stream(1099.0, 0.25, {1: (0.04, (1, {}, 'jpeg', 7))})
        frames = [when for when, data in writes if data.startswith(b'event: frame')]
        self.assertEqual(frames, [100.04])
        comments = [when for when, data in writes if data == b':\n\n']
        self.assertTrue(comments)
        self.assertAlmostEqual(comments[0] - frames[0], browser_api.FRAME_INTERVAL + 0.08)

    def test_warm_window_expires(self):
        waits, writes, _ = self.run_idle_stream(1085.1, 1.2)
        self.assertEqual(waits[:2], [browser_api.KEEP_WARM] * 2)
        self.assertTrue(all(timeout == 1.0 for timeout in waits[2:]))
        self.assertEqual(sum(data == b':\n\n' for _, data in writes), 2)

    def test_pacing_takes_newest_after_sleep_and_slow_write(self):
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        clock.sleep = lambda delay: now.__setitem__(0, now[0] + delay)
        tab = SimpleNamespace(target='T1', last_input=0.0)
        emitted = []
        def newest(*args):
            seq = int(round(now[0] * 120)) + 1
            return (seq, {}, str(seq), 7)
        fake = Mock(tabs={'T1': tab}, version=0, clock=lambda: now[0] + 1000)
        fake.watch.return_value = tab
        fake.first_frame.side_effect = newest
        fake.wait_frame.side_effect = newest
        fake.state.return_value = {}
        fake.nav_state.return_value = {}
        fake.heal.return_value = True
        class Sink(io.BytesIO):
            def write(self, data):
                if data.startswith(b'event: frame'):
                    seq = json.loads(data.split(b'data: ')[1])['seq']
                    emitted.append((now[0], seq))
                    if len(emitted) == 2:
                        now[0] += 0.2  # capture continues while this socket stalls
                    if len(emitted) == 4:
                        raise BrokenPipeError()
                return super().write(data)
        handler = Mock(wfile=Sink())
        with patch.object(browser_api, 'time', clock):
            browser_api.stream(handler, fake, 'T1', 2560, 1600)
        self.assertEqual([seq for _, seq in emitted], [1, 5, 29, 33])
        self.assertTrue(all(b[0] - a[0] >= 1 / 30 - 1e-9 for a, b in zip(emitted, emitted[1:])))
        fake.ack.assert_not_called()  # CDP receipt owns acknowledgments
        fake.unwatch.assert_called_once_with(tab, (2560, 1600), None)


class FlowTests(unittest.TestCase):
    """A panel that sends `sid` gets at most ACK_WINDOW unconfirmed frames, so frames cannot queue in the network."""

    def start(self, sid='abcdef123456'):
        tab = SimpleNamespace(target='T1', last_input=0.0)
        counter = [0]
        def newest(*args):
            time.sleep(0.005)
            counter[0] += 1
            return (counter[0], {}, str(counter[0]), 7)
        fake = Mock(tabs={'T1': tab}, version=0, clock=time.monotonic)
        fake.watch.return_value = tab
        fake.first_frame.side_effect = newest
        fake.wait_frame.side_effect = newest
        fake.state.return_value = {}
        fake.nav_state.return_value = {}
        fake.heal.return_value = True
        sent, stop = [], threading.Event()
        class Sink(io.BytesIO):
            def write(self, data):
                if stop.is_set():
                    raise BrokenPipeError()
                if data.startswith(b'event: frame'):
                    sent.append(json.loads(data.split(b'data: ')[1])['seq'])
                return super().write(data)
        thread = threading.Thread(target=browser_api.stream, args=(Mock(wfile=Sink()), fake, 'T1', 800, 600),
                                  kwargs={'sid': sid}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(3)))
        return sent

    def test_unconfirmed_frames_wait_and_ack_releases_the_next(self):
        with patch.object(browser_api, 'ACK_WAIT', 5):
            sent = self.start()
            time.sleep(0.4)
            self.assertEqual(len(sent), browser_api.ACK_WINDOW, 'Only the window may be in flight without acks.')
            browser_api.ack('abcdef123456', sent[0])
            time.sleep(0.2)
            self.assertEqual(len(sent), browser_api.ACK_WINDOW + 1)
            browser_api.ack('abcdef123456', sent[-1])
            time.sleep(0.2)
            self.assertEqual(len(sent), browser_api.ACK_WINDOW + 3)

    def test_lost_acks_slow_the_stream_but_never_stop_it(self):
        with patch.object(browser_api, 'ACK_WAIT', 0.15):
            sent = self.start()
            time.sleep(0.7)
            self.assertGreaterEqual(len(sent), browser_api.ACK_WINDOW + 2)
            self.assertLess(len(sent), 10)

    def test_ack_route_and_validation(self):
        from test_browser import FakeHandler
        handler = FakeHandler(browser_api.PREFIX + 'ack', {'sid': 'unknown-stream', 'seq': 3})
        browser_api.post(handler, Mock())
        self.assertEqual(handler.replies[0], (200, {'ok': True}))
        handler = FakeHandler(browser_api.PREFIX + 'ack', {'sid': 'unknown-stream', 'seq': '3'})
        browser_api.post(handler, Mock())
        self.assertEqual(handler.replies[0][0], 400)
        handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&w=800&h=600&sid=bad!')
        fake = Mock()
        browser_api.get(handler, fake)
        self.assertEqual(handler.replies[0][0], 400)
        fake.watch.assert_not_called()

    def test_stream_end_drops_its_flow(self):
        sent = self.start('flow-to-drop')
        time.sleep(0.1)
        self.assertIn('flow-to-drop', browser_api.FLOWS)
        self.doCleanups()
        self.assertNotIn('flow-to-drop', browser_api.FLOWS)
