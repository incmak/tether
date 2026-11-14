"""Whole-window capture and input, using mocked processes only."""
import base64
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import browser_display
from browser_display import JpegSplitter, VirtualDisplay
from test_browser import FakeHandler


class WindowDisplayTests(unittest.TestCase):
    def setUp(self):
        self.display = VirtualDisplay()
        self.display.environment = {'DISPLAY': ':999', 'XAUTHORITY': '/fixture/auth'}
        self.display._fit_window = Mock()  # WindowPlacementTests covers it

    def test_headed_position_only(self):
        with tempfile.TemporaryDirectory() as folder:
            for headed in (True, False):
                with patch.object(browser.os, 'posix_spawn', return_value=321) as spawn:
                    _, read_fd, write_fd = browser.launch('/fixture/chrome', Path(folder) / 'profile', headed=headed)
                os.close(read_fd)
                os.close(write_fd)
                self.assertEqual('--window-position=0,0' in spawn.call_args.args[1], headed)

    def test_splitter_chunk_boundaries(self):
        frames = [b'\xff\xd8first\xff\xd9', b'\xff\xd8second\xff\xd9']
        source = b'garbage' + b''.join(frames) + b'\xff\xd8partial'
        for step in range(1, len(source) + 1):
            splitter = JpegSplitter()
            actual = []
            for start in range(0, len(source), step):
                actual.extend(splitter.feed(source[start:start + step]))
            self.assertEqual(actual, frames)

    def test_capture_shared_sized_and_last_unwatch_stops(self):
        processes = [Mock(pid=23456), Mock(pid=23457)]
        for process in processes:
            process.poll.return_value = None
            process.stdout = io.BytesIO(b'\xff\xd8frame\xff\xd9')
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/ffmpeg'), \
                patch.object(browser_display.subprocess, 'Popen', side_effect=processes) as spawn, \
                patch.object(browser_display.threading, 'Thread') as thread, \
                patch.object(browser_display.os, 'killpg') as kill:
            self.display.watch(800, 800)
            args, kwargs = spawn.call_args
            self.assertEqual(args[0][1:], ['-loglevel', 'error', '-f', 'x11grab', '-draw_mouse', '1',
                '-framerate', '10', '-video_size', '2560x1600', '-i', ':999', '-vf', 'scale=800:500',
                '-q:v', '6', '-f', 'image2pipe', '-c:v', 'mjpeg', '-'])
            self.assertEqual(kwargs['env']['XAUTHORITY'], '/fixture/auth')
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            self.display._read_capture(processes[0], (800, 500))
            self.assertEqual(self.display.wait_frame(0, 0), (1, 800, 500, base64.b64encode(b'\xff\xd8frame\xff\xd9').decode()))
            with self.display.changed:
                self.display.capture_ended = False
            self.assertIsNone(self.display.wait_frame(1, 0))
            self.display.watch(640, 400)
            self.assertEqual(spawn.call_count, 1)
            self.display.watch(9000, 9000)
            self.assertEqual(self.display.capture_size, (2560, 1600))
            self.assertEqual(spawn.call_count, 2)
            kill.assert_called_once_with(23456, signal.SIGTERM)
            self.display.unwatch()
            self.display.unwatch()
            self.assertEqual(kill.call_count, 1)
            self.display.unwatch()
            self.assertEqual(kill.call_args.args, (23457, signal.SIGTERM))
            self.assertEqual(self.display.viewers, 0)
            self.assertIsNone(self.display.capture)
            self.assertTrue(processes[1].stdout.closed)
            self.assertEqual(thread.return_value.join.call_count, 2)

    def test_stop_escalates_and_stops_capture(self):
        process = Mock(pid=23456)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('ffmpeg', 3), 0]
        self.display.capture = process
        self.display.viewers = 1
        with patch.object(browser_display.os, 'killpg') as kill:
            self.display.stop()
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(23456, signal.SIGTERM), (23456, signal.SIGKILL)])
        self.assertEqual(self.display.viewers, 1)
        self.assertIsNone(self.display.environment)
        self.display.unwatch()
        self.assertEqual(self.display.viewers, 0)

    def test_input_mapping_and_text_are_one_batch(self):
        text = '$(rm -rf ~); "x"'
        events = [
            {'type': 'down', 'x': -20, 'y': 9000, 'button': 'left'},
            {'type': 'move', 'x': 15, 'y': 30, 'buttons': 1},
            {'type': 'up', 'x': 15, 'y': 30, 'button': 'left'},
            {'type': 'down', 'x': 10, 'y': 20, 'button': 'right'},
            {'type': 'up', 'x': 10, 'y': 20, 'button': 'right'},
            {'type': 'wheel', 'x': 20, 'y': 30, 'dx': -100, 'dy': 2000},
            {'type': 'text', 'text': text},
            {'type': 'keydown', 'key': 'l', 'modifiers': 2},
            {'type': 'keyup', 'key': 'l'},
            {'type': 'keydown', 'key': 'Enter'}, {'type': 'keyup', 'key': 'Enter'},
            {'type': 'keydown', 'key': 'Unknown'},
            {'type': 'keydown', 'key': 'a'}, {'type': 'keyup', 'key': 'a'},
        ]
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/xdotool'), \
                patch.object(browser_display.subprocess, 'run') as run:
            self.assertEqual(self.display.input(events), {'ok': True})
        args = run.call_args.args[0]
        self.assertEqual(args[:13], ['/fixture/xdotool', 'mousemove', '0', '1599', 'mousedown', '1',
                                    'mousemove', '15', '30', 'mousemove', '15', '30', 'mouseup'])
        self.assertEqual(args.count('5'), 10)
        self.assertIn('6', args)
        self.assertEqual(args[-20:], ['type', '--delay', '0', '--args', '1', '--', text, 'key', 'ctrl+l',
                                    'keydown', 'Return', 'keyup', 'Return', 'type', '--delay', '0', '--args', '1', '--', 'a'])
        self.assertNotIn('Unknown', args)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertEqual(run.call_args.kwargs['env']['DISPLAY'], ':999')
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_invalid_input_and_missing_tools(self):
        invalid = [None, [{}] * 201, [None], [{'type': 'invalid'}], [{'type': 'text', 'text': 'x' * 20001}],
                   [{'type': 'text', 'text': ''}], [{'type': 'text', 'text': '\0'}],
                   [{'type': 'move', 'x': True, 'y': 0}], [{'type': 'keydown', 'key': ''}]]
        with patch.object(browser_display.subprocess, 'run') as run:
            for events in invalid:
                with self.subTest(events=str(events)[:60]), self.assertRaises(ValueError):
                    self.display.input(events)
            run.assert_not_called()
        with patch.object(browser_display.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '^ffmpeg is not installed\\.$'):
                self.display.watch(100, 100)
            with self.assertRaisesRegex(RuntimeError, '^xdotool is not installed\\.$'):
                self.display.input([])

    def test_modified_special_key_releases_modifiers_after_focus_changes(self):
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/xdotool'), \
                patch.object(browser_display.subprocess, 'run') as run:
            self.display.input([{'type': 'keydown', 'key': 'ArrowLeft', 'modifiers': 1}])
            self.assertEqual(run.call_args.args[0][-2:], ['keydown', 'alt+Left'])
            self.display.input([{'type': 'keyup', 'key': 'ArrowLeft', 'modifiers': 0}])
            self.assertEqual(run.call_args.args[0][-2:], ['keyup', 'alt+Left'])
            self.assertEqual(self.display.held_keys, {})
            for before, after in ((1, 0), (0, 8)):
                self.display.input([{'type': 'keydown', 'key': 'ArrowLeft', 'modifiers': before}])
                old = run.call_args.args[0][-1]
                self.display.input([{'type': 'keydown', 'key': 'ArrowLeft', 'modifiers': after}])
                self.assertEqual(run.call_args.args[0][1:3], ['keyup', old])
                new = run.call_args.args[0][-1]
                self.display.input([{'type': 'keyup', 'key': 'ArrowLeft', 'modifiers': 0}])
                self.assertEqual(run.call_args.args[0][-2:], ['keyup', new])

    def test_old_unwatch_after_restart_keeps_new_capture(self):
        process = Mock(pid=23456)
        process.poll.return_value = None
        process.stdout = io.BytesIO()
        self.display.viewers = 1  # one stream still owns its registration when the browser stops
        self.display.stop()
        self.display.environment = {'DISPLAY': ':999', 'XAUTHORITY': '/fixture/auth'}
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/ffmpeg'), \
                patch.object(browser_display.subprocess, 'Popen', return_value=process), \
                patch.object(browser_display.threading, 'Thread'), \
                patch.object(browser_display.os, 'killpg') as kill:
            self.display.watch(640, 400)
            self.display.unwatch()  # old stream finally exits
            self.assertEqual(self.display.viewers, 1)
            self.assertIs(self.display.capture, process)
            kill.assert_not_called()
            self.display.unwatch()
            kill.assert_called_once_with(process.pid, signal.SIGTERM)


class WindowRouteTests(unittest.TestCase):
    def test_routes_reject_other_browsers(self):
        for ident in ('test', 'chrome'):
            handler = FakeHandler(browser_api.PREFIX + 'window?browser=' + ident)
            browser_api.get(handler, Mock())
            self.assertEqual(handler.replies, [(400, {'error': 'The whole-window view is only for the signed-in browser.',
                                                     **({'browser': 'chrome'} if ident == 'chrome' else {})})])
            handler = FakeHandler(browser_api.PREFIX + 'window/input', {'browser': ident, 'events': []})
            browser_api.post(handler, Mock())
            self.assertEqual(handler.replies[0][0], 400)

    def test_dispatch_needs_no_tab_and_maps_display_errors(self):
        signed = browser.Browser(signed=True, display=Mock())
        signed.ensure = Mock()
        signed.touch = Mock()
        signed.display.input.return_value = {'ok': True}
        handler = FakeHandler(browser_api.PREFIX + 'window/input', {'browser': 'signed', 'events': []})
        browser_api.post(handler, signed_browser=signed)
        self.assertEqual(handler.replies, [(200, {'ok': True, 'browser': 'signed'})])
        signed.display.input.assert_called_once_with([])
        signed.touch.assert_called_once()
        signed.window_watch(640, 400)
        signed.display.watch.assert_called_once_with(640, 400)
        signed.window_unwatch()
        signed.display.unwatch.assert_called_once_with()
        for method, args in (('window_watch', (640, 400)), ('window_input', ([],)), ('window_unwatch', ())):
            target = {'window_watch': 'watch', 'window_input': 'input', 'window_unwatch': 'unwatch'}[method]
            getattr(signed.display, target).side_effect = RuntimeError('missing tool')
            with self.assertRaisesRegex(browser.BrowserError, 'missing tool'):
                getattr(signed, method)(*args)
            with self.assertRaises(browser.BrowserError):
                getattr(browser.Browser(), method)(*args)

    def test_window_sse_metadata_duplicates_flow_and_cleanup(self):
        signed = Mock(signed=True)
        signed.display.size = (2560, 1600)
        handler = FakeHandler(browser_api.PREFIX + 'window?browser=signed&w=640&h=400&sid=window_test')
        handler.wfile = io.BytesIO()
        for name in ('send_response', 'send_header', 'end_headers'):
            setattr(handler, name, Mock())
        frames = iter([(1, 640, 400, 'jpeg'), (2, 640, 400, 'jpeg'), (3, 640, 400, 'different')])
        def next_frame(seen, timeout):
            try:
                return next(frames)
            except StopIteration:
                raise BrokenPipeError
        signed.display.wait_frame.side_effect = next_frame
        with patch.object(browser_api.time, 'sleep') as sleep, patch.object(browser_api.Flow, 'wait_room') as room:
            browser_api.get(handler, signed_browser=signed)
        messages = handler.wfile.getvalue().decode()
        data = [json.loads(line[6:]) for line in messages.splitlines() if line.startswith('data: ')]
        self.assertEqual([row['seq'] for row in data], [1, 3])
        self.assertEqual(data[0], {'seq': 1, 'width': 2560, 'height': 1600, 'top': 0,
                                   'browser': 'signed', 'window': True, 'jpeg': 'jpeg'})
        self.assertIn('retry: 1500', messages)
        self.assertTrue(any(call.args[0] > 0 for call in sleep.call_args_list))
        self.assertTrue(room.called)
        self.assertNotIn('window_test', browser_api.FLOWS)
        signed.window_watch.assert_called_once_with(640, 400)
        signed.window_unwatch.assert_called_once_with()

    def test_stream_setup_failure_and_invalid_sid(self):
        signed = Mock(signed=True)
        signed.window_watch.side_effect = browser.BrowserError('ffmpeg is not installed.')
        for query, status in (('browser=signed', 503), ('browser=signed&sid=bad', 400)):
            handler = FakeHandler(browser_api.PREFIX + 'window?' + query)
            browser_api.get(handler, signed_browser=signed)
            self.assertEqual(handler.replies[0][0], status)


class WindowPlacementTests(unittest.TestCase):
    def test_unwatch_never_starts_chrome(self):
        signed = browser.Browser(signed=True, display=Mock())
        signed.ensure = Mock()
        signed.window_unwatch()
        signed.ensure.assert_not_called()
        signed.display.unwatch.assert_called_once_with()


class WindowReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.display = VirtualDisplay()
        self.display.environment = {'DISPLAY': ':999', 'XAUTHORITY': '/fixture/auth'}

    def send(self, events):
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/xdotool'), \
                patch.object(browser_display.subprocess, 'run') as run:
            self.display.input(events)
        return run.call_args.args[0] if run.called else []

    def test_small_wheel_deltas_add_up(self):
        wheel = {'type': 'wheel', 'x': 50, 'y': 60, 'dx': 0, 'dy': 30}
        clicks = [self.send([wheel]).count('5') for _ in range(7)]
        self.assertEqual(clicks, [0, 0, 0, 1, 0, 0, 1])  # 30 px a batch: one click per 100 px
        up = self.send([dict(wheel, dy=-250)])
        self.assertEqual((up.count('4'), up.count('5')), (2, 0))

    def test_cmd_acts_as_ctrl(self):
        args = self.send([{'type': 'keydown', 'key': 'l', 'modifiers': 4},
                          {'type': 'keydown', 'key': 'a', 'modifiers': 6},
                          {'type': 'keydown', 'key': 'ArrowLeft', 'modifiers': 4}, {'type': 'keyup', 'key': 'ArrowLeft'}])
        self.assertEqual(args[1:], ['key', 'ctrl+l', 'key', 'ctrl+a', 'keydown', 'ctrl+Left', 'keyup', 'ctrl+Left'])
        self.assertNotIn('super', ' '.join(args))

    def test_fit_moves_only_the_largest_window_once(self):
        shapes = {'11': 'WIDTH=400\nHEIGHT=300\n', '22': 'WIDTH=2559\nHEIGHT=1599\n', '33': 'WIDTH=800\nHEIGHT=600\n'}
        def run(args, **kwargs):
            if args[1] == 'search':
                return Mock(stdout='11\n22\n33\n')
            if args[1] == 'getwindowgeometry':
                return Mock(stdout=shapes[args[-1]])
            return Mock(stdout='')
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/xdotool'), \
                patch.object(browser_display.subprocess, 'run', side_effect=run) as spawn:
            self.display._fit_window()
            self.display._fit_window()
        moves = [call.args[0] for call in spawn.call_args_list if call.args[0][1] == 'windowmove']
        self.assertEqual(moves, [['/fixture/xdotool', 'windowmove', '22', '0', '0', 'windowsize', '22', '2560', '1600']])

    def test_fit_waits_for_a_window(self):
        with patch.object(browser_display.shutil, 'which', return_value='/fixture/xdotool'), \
                patch.object(browser_display.subprocess, 'run', return_value=Mock(stdout='')):
            self.display._fit_window()
        self.assertFalse(self.display.fitted)


if __name__ == '__main__':
    unittest.main()
