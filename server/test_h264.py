"""Isolated encoder tests; only the explicit ffmpeg test starts a real process."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import os
import queue
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock

import h264


def jpeg(width=320, height=240):
    return b'\xff\xd8\xff\xc0\x00\x0b\x08' + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + b'\x01\x01\x11\x00\xff\xd9'


def flv_tag(body, tick=0, tag=9):
    head = bytes([tag]) + len(body).to_bytes(3, 'big') + (tick & 0xffffff).to_bytes(3, 'big')
    head += bytes([tick >> 24]) + b'\x00\x00\x00'
    return head + body + (len(body) + 11).to_bytes(4, 'big')


FLV_HEAD = b'FLV\x01\x01\x00\x00\x00\x09\x00\x00\x00\x00'
AVCC = b'\x01\x64\x00\x28\xff\xe1\x00'


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.stdin = io.BytesIO()
        read, write = os.pipe()
        self.stdout = os.fdopen(read, 'rb')
        self.output = os.fdopen(write, 'wb', buffering=0)
        self.stderr = io.BytesIO(b'')
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        self.output.close()
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.output.close()


def elements(data):
    """Parse the fixed-width EBML sizes emitted by this small Matroska writer."""
    result = []
    while data:
        length = next(n for n in range(1, 5) if data[0] & (1 << (8 - n)))
        ident = data[:length]
        size = int.from_bytes(data[length:length + 8], 'big') & ((1 << 56) - 1)
        result.append((ident, data[length + 8:length + 8 + size]))
        data = data[length + 8 + size:]
    return dict(result)


class EncoderTests(unittest.TestCase):
    def encoder(self):
        self.messages = queue.Queue()
        self.processes = []
        def popen(*args, **kwargs):
            if self.processes:
                self.assertIsNotNone(self.processes[-1].poll(), 'Two encoders overlap')
            process = FakeProcess()
            self.processes.append(process)
            return process
        encoder = h264.Encoder(lambda *message: self.messages.put(message), finder=lambda _: 'ffmpeg', popen=popen)
        self.addCleanup(encoder.close)
        return encoder

    def test_jpeg_size(self):
        self.assertEqual(h264.jpeg_size(jpeg(321, 241)), (321, 241))
        self.assertEqual(h264.jpeg_size(b'\xff\xd8\xff\xe0\x00\x04ab' + jpeg()[2:]), (320, 240))
        self.assertEqual(h264.jpeg_size(jpeg().replace(b'\xff\xc0', b'\xff\xc2')), (320, 240))
        for data in (b'', b'garbage', b'\xff\xd8', jpeg()[:10], b'\xff\xd8\xff\xe0\x00\x00', jpeg(0, 240)):
            with self.subTest(data=data), self.assertRaises(ValueError):
                h264.jpeg_size(data)

    def test_matroska_round_trip(self):
        header = h264.matroska_header(321, 241)
        ebml_size = int.from_bytes(header[4:12], 'big') & ((1 << 56) - 1)
        self.assertEqual(elements(header[12:12 + ebml_size])[b'\x42\x82'], b'matroska')
        segment = header[12 + ebml_size:]
        self.assertEqual(segment[:12], b'\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff')
        children = elements(segment[12:])
        info = elements(children[b'\x15\x49\xa9\x66'])
        self.assertEqual(int.from_bytes(info[b'\x2a\xd7\xb1'], 'big'), 1_000_000)
        track = elements(elements(children[b'\x16\x54\xae\x6b'])[b'\xae'])
        self.assertEqual(track[b'\x86'], b'V_MJPEG')
        video = elements(track[b'\xe0'])
        self.assertEqual([int.from_bytes(video[key], 'big') for key in (b'\xb0', b'\xba')], [321, 241])
        cluster = elements(elements(h264.matroska_cluster(0x1000001, jpeg()))[b'\x1f\x43\xb6\x75'])
        self.assertEqual(int.from_bytes(cluster[b'\xe7'], 'big'), 0x1000001)
        self.assertEqual(cluster[b'\xa3'], b'\x81\x00\x00\x80' + jpeg())

    def test_split_flv_extended_timestamp(self):
        class Split(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n, 2))
        stream = Split(FLV_HEAD + flv_tag(b'config') + flv_tag(b'video', 0x12345678))
        tags = h264.flv_tags(stream)
        self.assertEqual(next(tags), (9, 0, b'config'))
        self.assertEqual(next(tags), (9, 0x12345678, b'video'))
        with self.assertRaises(EOFError):
            next(tags)

    def test_config_video_mapping_and_duplicate(self):
        encoder = self.encoder()
        encoder.push(jpeg(321, 241), {'seq': 11, 'top': 4})
        # Exercise timestamps above the 24-bit FLV boundary as well as normal ticks.
        encoder.session['tick'] = 0x1000000
        encoder.push(jpeg(321, 241), {'seq': 12, 'top': 5})
        data = FLV_HEAD + flv_tag(b'script', tag=18) + flv_tag(b'\x17\x00\x00\x00\x00' + AVCC)
        data += flv_tag(b'\x17\x01\x00\x00\x00real', 2)
        data += flv_tag(b'\x27\x01\x00\x00\x00dup', 3)
        data += flv_tag(b'\x27\x01\x00\x00\x00next', 0x1000000)
        data += flv_tag(b'\x17\x02\x00\x00\x00')
        self.processes[0].output.write(data)
        kind, header, payload = self.messages.get(timeout=1)
        self.assertEqual((kind, header, payload), ('config', {'codec': 'avc1.640028', 'width': 320, 'height': 240}, AVCC))
        for seq, key, dup, payload in ((11, True, False, b'real'), (11, False, True, b'dup'), (12, False, False, b'next')):
            kind, header, data = self.messages.get(timeout=1)
            self.assertEqual((kind, header['seq'], header['key'], header['dup'], data), ('video', seq, key, dup, payload))
        self.assertEqual(len(encoder.session['pending']), 1)

    def test_missing_ffmpeg(self):
        callback = Mock()
        encoder = h264.Encoder(callback, finder=lambda _: None, popen=Mock(side_effect=AssertionError))
        encoder.push(jpeg(), {'seq': 1})
        encoder.push(jpeg(), {'seq': 2})
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(callback.call_args.args[0], 'failed')
        encoder.close()

    def test_startup_prime_then_two_clusters_per_push(self):
        encoder = self.encoder()
        encoder.push(jpeg(), {'seq': 1})
        expected = h264.matroska_header(320, 240)
        expected += b''.join(h264.matroska_cluster(tick, jpeg()) for tick in (0, 2, 3))
        self.assertEqual(self.processes[0].stdin.getvalue(), expected)
        encoder.push(jpeg(), {'seq': 2})
        expected += b''.join(h264.matroska_cluster(tick, jpeg()) for tick in (4, 5))
        self.assertEqual(self.processes[0].stdin.getvalue(), expected)

    def test_spawn_failure(self):
        callback = Mock()
        encoder = h264.Encoder(callback, finder=lambda _: 'ffmpeg', popen=Mock(side_effect=OSError('unavailable')))
        encoder.push(jpeg(), {'seq': 1})
        self.assertEqual(callback.call_args.args[0], 'failed')
        encoder.close()

    def test_no_video_timeout_stops_process(self):
        encoder = self.encoder()
        encoder.push(jpeg(), {'seq': 1})
        self.assertEqual(self.messages.get(timeout=3)[0], 'failed')
        self.assertTrue(self.processes[0].killed)

    def test_exit_failure(self):
        encoder = self.encoder()
        encoder.push(jpeg(), {'seq': 1})
        self.processes[0].wait()
        self.assertEqual(self.messages.get(timeout=1)[0], 'failed')

    def test_close_kills_only_its_process_after_one_second(self):
        process = FakeProcess()
        original_wait = process.wait
        waits = []
        def wait(timeout=None):
            waits.append(timeout)
            if not process.killed:
                raise subprocess.TimeoutExpired('fake ffmpeg', timeout)
            return original_wait(timeout)
        process.wait = wait
        encoder = h264.Encoder(Mock(), finder=lambda _: 'ffmpeg', popen=lambda *a, **kw: process)
        encoder.push(jpeg(), {'seq': 1})
        threads = list(encoder.session['threads'])
        encoder.close()
        self.assertTrue(process.killed)
        self.assertEqual(waits, [1, 1])
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_stderr_is_bounded(self):
        process = FakeProcess()
        process.stderr = io.BytesIO(b'x' * 32_000)
        encoder = h264.Encoder(Mock(), finder=lambda _: 'ffmpeg', popen=lambda *a, **kw: process)
        self.addCleanup(encoder.close)
        encoder.push(jpeg(), {'seq': 1})
        encoder.session['threads'][1].join(timeout=1)
        self.assertEqual(len(encoder.session['stderr']), 8192)

    def test_size_change_stops_old_process(self):
        encoder = self.encoder()
        encoder.push(jpeg(), {'seq': 1})
        encoder.push(jpeg(640, 480), {'seq': 2})
        self.assertEqual(len(self.processes), 2)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertEqual(encoder.session['size'], (640, 480))
        threads = list(encoder.session['threads'])
        encoder.close()
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is optional')
    def test_real_ffmpeg_first_frame_latency_and_resize(self):
        messages = queue.Queue()
        with tempfile.TemporaryDirectory() as folder:
            def pictures(size, pattern):
                subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=' + size,
                                '-frames:v', '2', '-threads', '1', folder + '/' + pattern + '%d.jpg'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=10, check=True)
                result = []
                for number in (1, 2):
                    with open(folder + '/' + pattern + str(number) + '.jpg', 'rb') as file:
                        result.append(file.read())
                return result
            first, second = pictures('320x240', 'a')
            resized, _ = pictures('640x480', 'b')
            encoder = h264.Encoder(lambda *message: messages.put((time.monotonic(), message)))
            self.addCleanup(encoder.close)
            for seq, data, size in ((1, first, (320, 240)), (2, second, (320, 240)), (3, resized, (640, 480))):
                start = time.monotonic()
                encoder.push(data, {'seq': seq})
                config = seq == 2
                while True:
                    when, (kind, header, payload) = messages.get(timeout=max(.001, start + 1 - time.monotonic()))
                    self.assertLess(when - start, 1)
                    self.assertNotEqual(kind, 'failed', header)
                    if kind == 'config':
                        config = True
                        self.assertEqual((header['width'], header['height']), size)
                    if kind == 'video' and header['seq'] == seq and not header['dup']:
                        self.assertTrue(config)
                        if seq != 2:
                            self.assertTrue(header['key'])
                        self.assertTrue(payload)
                        break
                print('ffmpeg frame %d: real video in %.3fs (key=%s)' % (seq, when - start, header['key']))
            threads = list(encoder.session['threads'])
            encoder.close()
            self.assertTrue(all(not thread.is_alive() for thread in threads))


if __name__ == '__main__':
    unittest.main()
