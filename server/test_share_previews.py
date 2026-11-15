"""Optional preview tools, with isolated catalogs and temporary fake executables."""
import io
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import share_api
import share_previews as previews
import share_store as share
from own_time import own_time

JPEG = b'\xff\xd8\xffpreview'
PROBE = {'format': {'duration': '2.4'},
         'streams': [{'codec_type': 'video', 'width': 1920, 'height': 1080},
                     {'codec_type': 'audio', 'duration': '2.4'}]}


class Handler:
    def __init__(self, method, action, body=b'', headers=None, denied=False):
        self.command, self.path = method, share_api.PREFIX + action
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self.headers = {'Host': 'share.test', 'Origin': 'http://share.test', 'X-Codex-Mobile': '1',
                        'Content-Length': str(len(body)), **(headers or {})}
        self.denied, self.status, self.sent = denied, None, {}

    def deny(self):
        return self.denied

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent[key] = value

    def end_headers(self):
        pass


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patches = [patch.dict(os.environ, {
            'TETHER_FFMPEG': str(self.root / 'absent-ffmpeg'),
            'TETHER_FFPROBE': str(self.root / 'absent-ffprobe'),
            'TETHER_PDFTOPPM': str(self.root / 'absent-pdftoppm')}),
            patch.object(share, 'MIN_FREE', 0),
            patch.object(previews, '_queue', queue.Queue(maxsize=64)),
            patch.object(previews, '_thread', None),
            patch.object(previews, '_pending', set()),
            patch.object(previews, '_failed', set()),
            patch.object(previews, '_backfills', {})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.store = share.Store(self.root / 'share')
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        thread = previews._thread
        if thread is not None and isinstance(thread, threading.Thread):
            previews._queue.put(None, timeout=5)
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'Preview test worker did not stop.')

    def tool(self, name, source=None):
        if source is None:
            source = ('import sys\nsys.stdout.buffer.write(' + repr(JPEG) + ')') if name != 'ffprobe' else (
                'print(' + repr(json.dumps(PROBE)) + ')')
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + source + '\n')
        path.chmod(0o700)
        os.environ['TETHER_' + name.upper()] = str(path)
        return str(path)

    def put(self, kind='video', **values):
        data, name = {'video': (b'\x00\x00\x00\x18ftypisom0000', 'clip.mp4'),
                      'audio': (b'RIFF0000WAVE0000', 'clip.wav'),
                      'pdf': (b'%PDF-1.7\n', 'doc.pdf'),
                      'image': (b'\x89PNG\r\n\x1a\n', 'pic.png'),
                      'text': (b'text', 'note.txt')}[kind]
        return self.store.put(io.BytesIO(data), len(data), name, **values)

    def run_job(self, file):
        previews.enqueue(self.store, file['id'])
        self.wait_jobs()
        return self.store.stat(file['id'])

    def wait_jobs(self):
        deadline = time.monotonic() + 5
        with previews._queue.all_tasks_done:
            while previews._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, 'Preview job did not finish.')
                previews._queue.all_tasks_done.wait(remaining)

    def failed(self, file):
        return (str(self.store.root), file['id']) in previews._failed

    def test_absent_tools_do_not_start_thread_or_access_store(self):
        store = Mock()
        self.assertEqual(previews.tools(), {'ffmpeg': False, 'ffprobe': False, 'pdftoppm': False})
        with patch.object(previews.threading, 'Thread') as thread:
            previews.enqueue(store, 'id')
            previews.backfill(store)
            thread.assert_not_called()
        self.assertEqual(store.mock_calls, [])
        self.assertTrue(previews._queue.empty())
        self.assertEqual(previews._backfills, {})

    def test_tools_use_overrides_and_path_fallback(self):
        self.tool('ffmpeg')
        self.assertEqual(previews.tools(), {'ffmpeg': True, 'ffprobe': False, 'pdftoppm': False})
        with patch.dict(os.environ, {'PATH': str(self.root)}):
            del os.environ['TETHER_FFMPEG']
            self.assertTrue(previews.tools()['ffmpeg'])

    def test_video_stores_thumbnail_and_metadata_with_expected_arguments(self):
        checks = """import sys
assert sys.stdin.buffer.read() == b''
for flag in ('-hide_banner', '-loglevel', '-protocol_whitelist'):
    assert flag in sys.argv
assert ('-nostdin' in sys.argv) == sys.argv[0].endswith('ffmpeg')  # real ffprobe rejects -nostdin
assert sys.argv[sys.argv.index('-protocol_whitelist') + 1] == 'file'
assert any(arg.startswith('file:/') for arg in sys.argv)
"""
        self.tool('ffprobe', checks + 'print(' + repr(json.dumps(PROBE)) + ')')
        self.tool('ffmpeg', checks + """assert sys.argv[sys.argv.index('-ss') + 1] == str(2.4 / 3)
assert '480' in sys.argv[sys.argv.index('-vf') + 1]
assert sys.argv[sys.argv.index('-q:v') + 1] == '5'
assert sys.argv[-1] == 'pipe:1'
assert sys.argv[sys.argv.index('-threads') + 1] == '1'
sys.stdout.buffer.write(""" + repr(JPEG) + ')')
        file = self.put()
        result = self.run_job(file)
        self.assertEqual((result['duration'], result['width'], result['height']), (2.4, 1920, 1080))
        self.assertTrue(result['has_thumb'])
        self.assertEqual((self.store.root / 'thumbs' / file['id']).read_bytes(), JPEG)
        self.assertFalse(self.failed(file))
        self.assertTrue(previews._thread.daemon)

    def test_video_seek_caps_at_one_second(self):
        self.tool('ffmpeg', "import sys\nassert sys.argv[sys.argv.index('-ss') + 1] == '1'\nsys.stdout.buffer.write(" + repr(JPEG) + ')')
        file = self.put(width=640, height=480, duration=12)
        self.assertTrue(self.run_job(file)['has_thumb'])

    def test_audio_stores_duration_only(self):
        self.tool('ffprobe')
        self.tool('ffmpeg', 'raise AssertionError("Audio must not make a thumbnail")')
        file = self.put('audio')
        result = self.run_job(file)
        self.assertEqual(result['duration'], 2.4)
        self.assertNotIn('width', result)
        self.assertNotIn('height', result)
        self.assertFalse(result['has_thumb'])
        self.assertFalse(self.failed(file))

    def test_pdf_stores_first_page_thumbnail(self):
        self.tool('pdftoppm', """import sys
assert sys.argv[1:-1] == ['-f', '1', '-l', '1', '-scale-to', '480', '-jpeg', '-jpegopt', 'quality=80']
assert sys.argv[-1].startswith('/')
sys.stdout.buffer.write(""" + repr(JPEG) + ')')
        result = self.run_job(self.put('pdf'))
        self.assertTrue(result['has_thumb'])
        self.assertNotIn('duration', result)

    def test_small_browser_image_gets_dimensions_without_thumbnail(self):
        self.tool('ffprobe')
        self.tool('ffmpeg', 'raise AssertionError("Small browser image")')
        file = self.put('image')
        result = self.run_job(file)
        self.assertEqual((result['width'], result['height']), (1920, 1080))
        self.assertFalse(result['has_thumb'])
        self.assertNotIn('duration', result)
        self.assertFalse(self.failed(file))

    def test_large_and_unsupported_images_get_thumbnail(self):
        self.tool('ffprobe')
        self.tool('ffmpeg')
        for mime in ('image/heic', 'image/heif', 'image/tiff', 'image/bmp', 'image/png'):
            with self.subTest(mime=mime):
                file = self.put('image')
                with self.store.connection(write=True) as db:
                    db.execute('UPDATE files SET mime=?, size=? WHERE id=?',
                               (mime, 2 * 1024**2 + 1 if mime == 'image/png' else file['size'], file['id']))
                result = self.run_job(file)
                self.assertTrue(result['has_thumb'])
                self.assertEqual((result['width'], result['height']), (1920, 1080))

    def test_existing_thumbnail_and_metadata_survive(self):
        self.tool('ffprobe')
        self.tool('ffmpeg', 'raise AssertionError("Existing thumbnail")')
        file = self.put(width=12, duration=0)
        original = b'\xff\xd8\xffbrowser'
        self.store.thumbnail(file['id'], original, 'image/jpeg')
        result = self.run_job(file)
        self.assertEqual((result['width'], result['height'], result['duration']), (12, 1080, 0))
        self.assertEqual((self.store.root / 'thumbs' / file['id']).read_bytes(), original)
        self.assertFalse(self.failed(file))

    def test_thumbnail_created_during_conversion_is_preserved(self):
        self.tool('ffmpeg')
        file = self.put(width=1, height=1, duration=1)
        original = b'\xff\xd8\xffbrowser'
        run = previews._run
        def convert(arguments):
            result = run(arguments)
            self.store.thumbnail(file['id'], original, 'image/jpeg')
            return result
        with patch.object(previews, '_run', side_effect=convert):
            self.run_job(file)
        self.assertEqual((self.store.root / 'thumbs' / file['id']).read_bytes(), original)

    def test_store_methods_validate_and_enforce_owner_and_live_file(self):
        file = self.put()
        for value in (-1, 'nan', 'inf', 1.5, 1000001):
            with self.assertRaises(share.ShareError):
                self.store.fill_metadata(file['id'], width=value)
        for body, mime in ((b'bad', 'image/jpeg'), (JPEG, 'image/png'), (JPEG + b'x' * share.THUMB_LIMIT, 'image/jpeg')):
            with self.assertRaises(share.ShareError):
                self.store.thumbnail_if_missing(file['id'], body, mime)
        for function, args in ((self.store.fill_metadata, (file['id'],)),
                               (self.store.thumbnail_if_missing, (file['id'], JPEG, 'image/jpeg'))):
            with self.assertRaises(share.ShareError) as caught:
                function(*args, actor='other')
            self.assertEqual(caught.exception.status, 403)
        self.store.fill_metadata(file['id'], width=0, height=12, duration=0)
        result = self.store.fill_metadata(file['id'], width=640, height=480, duration=12)
        self.assertEqual((result['width'], result['height'], result['duration']), (0, 12, 0))
        self.store.change('delete', [file['id']])
        for function, args in ((self.store.fill_metadata, (file['id'],)),
                               (self.store.thumbnail_if_missing, (file['id'], JPEG, 'image/jpeg'))):
            with self.assertRaises(share.ShareError) as caught:
                function(*args)
            self.assertEqual(caught.exception.status, 404)

    def test_tool_limits_and_closed_stdin(self):
        tool = self.tool('ffprobe', """import json, os, resource, sys
assert sys.stdin.buffer.read() == b''
print(json.dumps([os.getpriority(os.PRIO_PROCESS, 0),
    resource.getrlimit(resource.RLIMIT_AS), resource.getrlimit(resource.RLIMIT_CPU),
    resource.getrlimit(resource.RLIMIT_FSIZE)]))
""")
        limits = json.loads(previews._run([tool]))
        self.assertGreaterEqual(limits[0], 10)
        self.assertEqual(limits[1:], [[1024**3, 1024**3], [30, 30], [0, 0]])

    def test_timeout_kills_process_group_and_remembers_failure(self):
        self.tool('ffmpeg', 'import os, time\nos.fork()\ntime.sleep(10)')
        file = self.put(width=1, height=1, duration=1)
        started = time.monotonic()
        with patch.object(previews, 'TIMEOUT', 0.2), patch.object(previews.os, 'killpg', wraps=os.killpg) as kill:
            result = self.run_job(file)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(result['has_thumb'])
        self.assertEqual(kill.call_count, 1)
        self.assertEqual(kill.call_args.args[1], signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.kill(kill.call_args.args[0], 0)
        self.assertTrue(self.failed(file))

    def test_oversized_and_non_jpeg_outputs_fail_without_retry(self):
        for source in ("import sys\nsys.stdout.buffer.write(b'\\xff\\xd8\\xff' + b'x' * (512 * 1024))",
                       "print('not JPEG')", "import sys\nsys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\n')"):
            with self.subTest(source=source):
                self.tool('ffmpeg', source)
                file = self.put(width=1, height=1, duration=1)
                self.assertFalse(self.run_job(file)['has_thumb'])
                self.assertTrue(self.failed(file))
                with patch.object(previews, '_preview') as job:
                    self.run_job(file)
                    job.assert_not_called()

    def test_bad_probe_and_nonzero_exit_fail_without_killing_worker(self):
        for source in ("print('bad JSON')", "print('[]')", "print('{\"streams\": [null]}')",
                       "print('{\"format\": []}')", 'raise SystemExit(1)',
                       "import sys\nsys.stdout.write('x' * (512 * 1024 + 1))"):
            self.tool('ffprobe', source)
            file = self.put('audio')
            self.run_job(file)
            self.assertTrue(self.failed(file))
            self.assertTrue(previews._thread.is_alive())

    def test_jpeg_at_output_limit_is_accepted(self):
        self.tool('ffmpeg', "import sys\nsys.stdout.buffer.write(b'\\xff\\xd8\\xff' + b'x' * (512 * 1024 - 3))")
        file = self.put(width=1, height=1, duration=1)
        self.assertTrue(self.run_job(file)['has_thumb'])
        self.assertEqual((self.store.root / 'thumbs' / file['id']).stat().st_size, share.THUMB_LIMIT)

    def test_trash_file_is_not_processed_even_when_enqueued(self):
        self.tool('ffmpeg')
        file = self.put()
        self.store.change('delete', [file['id']])
        with patch.object(previews, '_run') as run:
            self.assertFalse(self.run_job(file)['has_thumb'])
            run.assert_not_called()

    def test_partial_tools_process_available_work_without_failure(self):
        self.tool('ffprobe')
        file = self.put()
        result = self.run_job(file)
        self.assertEqual(result['duration'], 2.4)
        self.assertFalse(result['has_thumb'])
        self.assertFalse(self.failed(file))

    def test_queue_is_bounded_and_deduplicates_pending_ids(self):
        self.tool('ffmpeg')
        with patch.object(previews.threading, 'Thread') as thread:
            for identifier in range(70):
                previews.enqueue(self.store, str(identifier))
            previews.enqueue(self.store, '0')
        self.assertEqual(previews._queue.maxsize, 64)
        self.assertEqual(previews._queue.qsize(), 64)
        self.assertEqual(len(previews._pending), 64)
        thread.assert_called_once()
        self.assertNotIn((str(self.store.root), '64'), previews._pending)

    def test_jobs_are_serial_and_share_one_worker(self):
        self.tool('ffmpeg')
        first, second = self.put(), self.put()
        started, release = threading.Event(), threading.Event()
        calls = []
        def job(store, identifier):
            calls.append(identifier)
            started.set()
            if not release.wait(5):
                raise ValueError('Test release timeout')
        with patch.object(previews, '_preview', side_effect=job):
            try:
                previews.enqueue(self.store, first['id'])
                self.assertTrue(started.wait(5))
                worker = previews._thread
                previews.enqueue(self.store, second['id'])
                self.assertIs(previews._thread, worker)
                self.assertEqual(calls, [first['id']])
            finally:
                release.set()
            self.wait_jobs()
        self.assertEqual(calls, [first['id'], second['id']])

    def test_backfill_throttle_trash_failed_complete_and_unrelated_files(self):
        self.tool('ffmpeg')
        video, audio, pdf, image = self.put(), self.put('audio'), self.put('pdf'), self.put('image')
        trash, failed = self.put(), self.put()
        self.put('text')
        complete = self.put(width=1, height=1, duration=1)
        self.store.thumbnail(complete['id'], JPEG, 'image/jpeg')
        self.store.change('delete', [trash['id']])
        previews._failed.add((str(self.store.root), failed['id']))
        with patch.object(previews, 'enqueue') as enqueue, own_time(previews, monotonic=Mock(return_value=100)) as now:
            previews.backfill(self.store)
            self.assertEqual({call.args[1] for call in enqueue.call_args_list}, {f['id'] for f in (video, audio, pdf, image)})
            enqueue.reset_mock()
            now.return_value = 159.99
            previews.backfill(self.store)
            enqueue.assert_not_called()
            now.return_value = 160
            previews.backfill(self.store)
            self.assertEqual(enqueue.call_count, 4)

    def test_backfill_enqueues_at_most_100_live_candidates(self):
        self.tool('ffmpeg')
        file = self.put()
        with self.store.connection(write=True) as db:
            columns = [row['name'] for row in db.execute('PRAGMA table_info(files)')]
            row = dict(self.store.row(db, file['id']))
            for index in range(110):
                row['id'] = 'candidate-' + str(index)
                db.execute('INSERT INTO files (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                           [row[column] for column in columns])
        with patch.object(previews, 'enqueue') as enqueue:
            previews.backfill(self.store)
        self.assertEqual(enqueue.call_count, 100)

    def test_api_previews_list_and_successful_upload_hooks(self):
        self.tool('ffmpeg')
        with patch.object(share_api, 'get_store', return_value=self.store):
            handler = Handler('GET', 'previews')
            self.assertTrue(share_api.dispatch(handler))
            self.assertEqual(handler.status, 200)
            self.assertEqual(json.loads(handler.wfile.getvalue()),
                             {'tools': {'ffmpeg': True, 'ffprobe': False, 'pdftoppm': False}})
            with patch.object(previews, 'backfill') as backfill:
                handler = Handler('GET', 'list')
                share_api.dispatch(handler)
                self.assertEqual(handler.status, 200)
                backfill.assert_called_once_with(self.store)
            with patch.object(previews, 'enqueue') as enqueue:
                handler = Handler('POST', 'upload', b'text', {'X-File-Name': 'note.txt'})
                share_api.dispatch(handler)
                self.assertEqual(handler.status, 200)
                enqueue.assert_called_once_with(self.store, json.loads(handler.wfile.getvalue())['file']['id'])
                enqueue.reset_mock()
                handler = Handler('POST', 'upload', b'text')
                share_api.dispatch(handler)
                self.assertEqual(handler.status, 400)
                enqueue.assert_not_called()

    def test_resumable_finalize_enqueues_only_the_finished_file(self):
        self.tool('ffmpeg')
        with patch.object(share_api, 'get_store', return_value=self.store), patch.object(previews, 'enqueue') as enqueue:
            handler = Handler('POST', 'uploads', json.dumps({'name': 'clip.bin', 'size': 4}).encode())
            share_api.dispatch(handler)
            identifier = json.loads(handler.wfile.getvalue())['upload']['id']
            handler = Handler('POST', f'uploads/{identifier}?offset=0', b'ab')
            share_api.dispatch(handler)
            self.assertEqual(handler.status, 200)
            enqueue.assert_not_called()
            handler = Handler('POST', f'uploads/{identifier}?offset=2', b'cd')
            share_api.dispatch(handler)
            self.assertEqual(handler.status, 200)
            enqueue.assert_called_once_with(self.store, json.loads(handler.wfile.getvalue())['file']['id'])

    def test_api_previews_keeps_authentication_gate(self):
        with patch.object(share_api, 'get_store') as store:
            handler = Handler('GET', 'previews', denied=True)
            self.assertTrue(share_api.dispatch(handler))
            store.assert_not_called()
