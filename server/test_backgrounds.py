"""Background routes use temporary storage and fake Codex processes only."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import backgrounds
import prefs_sync
import server


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOuoAAAAASUVORK5CYII=')
# Header fixtures: the server checks signatures, not full image decoding.
JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + bytes(16) + b'\xff\xd9'
WEBP = b'RIFF\x16\x00\x00\x00WEBPVP8L\x09\x00\x00\x00\x2f' + bytes(9)


def handler(path='', body=None, kind='application/json', method='POST'):
    value = object.__new__(server.Handler)
    value.path = backgrounds.PATH + path
    value.command = method
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    value.rfile = io.BytesIO(raw)
    value.headers = {'Content-Type': kind, 'Content-Length': str(len(raw)), 'Host': 'phone.test',
                     'Origin': 'https://phone.test', 'X-Codex-Mobile': '1'}
    value.deny = mock.Mock(return_value=False)
    value.respond = mock.Mock()
    return value


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for patcher in (mock.patch.object(backgrounds.paths, 'BACKGROUNDS', self.root / 'backgrounds'),
                        mock.patch.object(backgrounds.paths, 'CODEX', self.root / 'codex'),
                        mock.patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}),
                        mock.patch.object(backgrounds, '_jobs', {}),
                        # Header fixtures cannot be decoded; tests that shrink patch this themselves.
                        mock.patch.object(backgrounds.image_shrink, 'shrink', return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, path, body, kind='application/json'):
        value = handler(path, body, kind)
        value.do_POST()
        return value.respond.call_args.args

    def get(self, path=''):
        value = handler(path, method='GET')
        value.do_GET()
        return value.respond.call_args.args

    def start(self, prompt='quiet hills', tuned=True, size='landscape'):
        # Capture the worker rather than starting an untracked background thread.
        with mock.patch.object(backgrounds.threading, 'Thread') as thread:
            status, result = self.post('/generate', {'prompt': prompt, 'tuned': tuned, 'size': size})
        self.assertEqual(status, 202)
        thread.return_value.start.assert_called_once()
        return result['job'], thread.call_args.kwargs['args']

    def run_job(self, args, write=True, code=0, features='image_generation stable true\n', timeout=False):
        def launch(command, **kwargs):
            if write:
                (kwargs['cwd'] / 'bg.png').write_bytes(PNG)
            process = mock.Mock(pid=123456789)
            process.wait.side_effect = [subprocess.TimeoutExpired(command, 300), 0] if timeout else None
            process.wait.return_value = code
            return process

        with mock.patch.object(backgrounds.shutil, 'which', return_value='/fake/codex'), \
                mock.patch.object(backgrounds.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, features, '')) as feature, \
                mock.patch.object(backgrounds.subprocess, 'Popen', side_effect=launch) as process, \
                mock.patch.object(backgrounds.os, 'killpg') as kill:
            backgrounds._run_job(*args)
        return feature, process, kill

    def test_upload_magic_types_metadata_and_serving(self):
        for data, kind, ext in ((PNG, 'image/png', 'png'), (JPEG, 'image/jpeg', 'jpg'), (WEBP, 'image/webp', 'webp')):
            with self.subTest(kind=kind):
                status, item = self.post('/upload', data, kind)
                self.assertEqual(status, 200)
                self.assertRegex(item['id'], r'^[a-f0-9]{32}$')
                self.assertEqual(item['url'], backgrounds.PATH + '/' + item['id'] + '.' + ext)
                self.assertEqual(item['source'], 'upload')
                self.assertIn('created', item)
                response = self.get('/' + item['url'].rsplit('/', 1)[1])
                self.assertEqual(response[:3], (200, data, kind))
                self.assertIn('max-age=31536000', response[3])
                if ext == 'png':
                    self.assertEqual((item['width'], item['height']), (1, 1))
        self.assertEqual(len(json.loads((backgrounds.paths.BACKGROUNDS / 'index.json').read_text())), 3)
        self.assertFalse((backgrounds.paths.BACKGROUNDS / 'index.tmp').exists())

    def small(self):
        return {'image': WEBP + b'page', 'thumb': WEBP + b'thumb', 'placeholder': 'data:image/webp;base64,UklGRg==',
                'width': 2560, 'height': 1440}

    def test_upload_saves_small_copies_when_an_encoder_works(self):
        with mock.patch.object(backgrounds.image_shrink, 'shrink', return_value=self.small()) as shrink:
            status, item = self.post('/upload', PNG, 'image/png')
        self.assertEqual(status, 200)
        shrink.assert_called_once_with(PNG)
        ident = item['id']
        self.assertEqual(item['url'], f'{backgrounds.PATH}/{ident}.webp')
        self.assertEqual(item['thumb'], f'{backgrounds.PATH}/{ident}.thumb.webp')
        self.assertEqual(item['placeholder'], 'data:image/webp;base64,UklGRg==')
        self.assertEqual((item['width'], item['height']), (2560, 1440))
        self.assertNotIn('original', item)
        self.assertEqual(self.get(f'/{ident}.webp')[:3], (200, WEBP + b'page', 'image/webp'))
        self.assertEqual(self.get(f'/{ident}.thumb.webp')[:3], (200, WEBP + b'thumb', 'image/webp'))
        # A new upload keeps only the small copies.
        self.assertEqual(sorted(path.name for path in backgrounds.paths.BACKGROUNDS.iterdir()),
                         sorted(['index.json', f'{ident}.webp', f'{ident}.thumb.webp']))
        self.assertEqual(self.post('/delete', {'id': ident}), (200, {'ok': True}))
        self.assertEqual(sorted(path.name for path in backgrounds.paths.BACKGROUNDS.iterdir()), ['index.json'])

    def test_thumb_names_are_strict(self):
        item = backgrounds.save(PNG)
        for path in (f'/{item["id"]}.thumb.thumb.webp', f'/{item["id"]}.thumbwebp', f'/{item["id"]}.small.thumb.webp', f'/{item["id"]}.small.webp'):
            self.assertEqual(self.get(path)[0], 404)

    def test_upgrade_adds_small_copies_and_keeps_the_original(self):
        old = backgrounds.save(PNG)
        big = WEBP + bytes(5000)
        large = backgrounds.save(big, 'image/webp')
        tiny = backgrounds.save(WEBP, 'image/webp')
        with mock.patch.object(backgrounds.image_shrink, 'shrink', return_value=self.small()):
            self.assertEqual(backgrounds.upgrade(), 3)
            self.assertEqual(backgrounds.upgrade(), 0)
        items = {item['id']: item for item in self.get()[1]['items']}
        png = items[old['id']]
        self.assertEqual(png['url'], f'{backgrounds.PATH}/{old["id"]}.webp')
        self.assertEqual(png['original'], old['url'])
        self.assertEqual(self.get('/' + old['url'].rsplit('/', 1)[1])[:2], (200, PNG))
        # A large old WebP gets <id>.small.webp: the copy cannot reuse the original's name.
        webp = items[large['id']]
        self.assertEqual(webp['url'], f'{backgrounds.PATH}/{large["id"]}.small.webp')
        self.assertEqual(webp['original'], large['url'])
        self.assertEqual(self.get(f'/{large["id"]}.small.webp')[:3], (200, WEBP + b'page', 'image/webp'))
        self.assertEqual(self.get('/' + large['url'].rsplit('/', 1)[1])[:2], (200, big))
        # A WebP that is already smaller than the copy keeps its page file.
        kept = items[tiny['id']]
        self.assertEqual(kept['url'], tiny['url'])
        self.assertNotIn('original', kept)
        self.assertEqual(kept['thumb'], f'{backgrounds.PATH}/{tiny["id"]}.thumb.webp')
        backgrounds.delete(old['id'])
        backgrounds.delete(large['id'])
        self.assertEqual(sorted(path.name for path in backgrounds.paths.BACKGROUNDS.iterdir()),
                         sorted(['index.json', f'{tiny["id"]}.webp', f'{tiny["id"]}.thumb.webp']))

    def test_failed_writes_leave_no_stray_copies(self):
        real = backgrounds._write_file

        def page_fails(name, data):
            if not name.endswith('.thumb.webp'):
                raise OSError('disk full')
            real(name, data)
        with mock.patch.object(backgrounds.image_shrink, 'shrink', return_value=self.small()), \
                mock.patch.object(backgrounds, '_write_file', side_effect=page_fails):
            with self.assertRaises(OSError):
                backgrounds.save(PNG)
        self.assertEqual([path.name for path in backgrounds.paths.BACKGROUNDS.iterdir()], [])
        old = backgrounds.save(PNG)
        with mock.patch.object(backgrounds.image_shrink, 'shrink', return_value=self.small()), \
                mock.patch.object(backgrounds, '_write', side_effect=OSError('disk full')):
            self.assertEqual(backgrounds.upgrade(), 0)
        self.assertEqual(sorted(path.name for path in backgrounds.paths.BACKGROUNDS.iterdir()),
                         sorted(['index.json', old['url'].rsplit('/', 1)[1]]))
        self.assertNotIn('thumb', self.get()[1]['items'][0])

    def test_upgrade_without_an_encoder_changes_nothing(self):
        item = backgrounds.save(PNG)
        before = (backgrounds.paths.BACKGROUNDS / 'index.json').read_text()
        self.assertEqual(backgrounds.upgrade(), 0)
        self.assertEqual((backgrounds.paths.BACKGROUNDS / 'index.json').read_text(), before)
        self.assertEqual(self.get()[1]['items'][0]['url'], item['url'])

    def test_upgrade_skips_a_photo_deleted_while_it_shrinks(self):
        item = backgrounds.save(PNG)

        def shrink(data):
            backgrounds.delete(item['id'])
            return self.small()
        with mock.patch.object(backgrounds.image_shrink, 'shrink', side_effect=shrink):
            self.assertEqual(backgrounds.upgrade(), 0)
        self.assertEqual(self.get(), (200, {'items': []}))

    def test_rejects_wrong_magic_and_mismatched_content_type(self):
        for data, kind in ((b'not an image', 'image/png'), (PNG, 'image/jpeg'), (PNG, 'text/html'),
                           (b'RIFFxxxxWEBPbad!', 'image/webp')):
            self.assertIn(self.post('/upload', data, kind)[0], (400, 415))
        self.assertEqual(self.get(), (200, {'items': []}))

    def test_upload_limits_and_truncated_body(self):
        for length in ('0', '-1', str(backgrounds.LIMIT + 1)):
            value = handler('/upload', PNG, 'image/png')
            value.headers['Content-Length'] = length
            value.do_POST()
            self.assertEqual(value.respond.call_args.args[0], 413)
            self.assertTrue(value.close_connection)
        for length in ('bad', str(len(PNG) + 10)):
            value = handler('/upload', PNG, 'image/png')
            value.headers['Content-Length'] = length
            value.do_POST()
            self.assertEqual(value.respond.call_args.args[0], 400)
        with self.assertRaises(backgrounds.Bad) as caught:
            backgrounds.save(PNG + bytes(backgrounds.LIMIT))
        self.assertEqual(caught.exception.status, 413)

    def test_list_order_and_delete(self):
        with mock.patch.object(backgrounds.time, 'time', side_effect=[10, 30, 20]):
            items = [backgrounds.save(PNG) for _ in range(3)]
        self.assertEqual([item['created'] for item in self.get()[1]['items']], [30, 20, 10])
        self.assertEqual(self.post('/delete', {'id': items[1]['id']}), (200, {'ok': True}))
        self.assertEqual(self.get('/' + items[1]['url'].rsplit('/', 1)[1])[0], 404)
        self.assertEqual(len(self.get()[1]['items']), 2)
        self.assertEqual(self.post('/delete', {'id': items[1]['id']})[0], 404)

    def test_strict_ids_paths_and_symlinks(self):
        item = backgrounds.save(PNG)
        filename = item['url'].rsplit('/', 1)[1]
        for ident in ('../index', item['id'] + '\n', '/' + item['id'], 2, None):
            self.assertEqual(self.post('/delete', {'id': ident})[0], 400)
        for path in ('/../index.json', '/%2e%2e/index.json', '/index.json', '/' + filename + '/extra',
                     '/' + item['id'] + '.svg', '/sub/../' + filename, '/%2f' + filename):
            self.assertEqual(self.get(path)[0], 404)
        target = backgrounds.paths.BACKGROUNDS / filename
        target.unlink()
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        target.symlink_to(outside)
        self.assertEqual(self.get('/' + filename)[0], 404)

    def test_thirty_limit_and_reuse_after_delete(self):
        items = [backgrounds.save(PNG) for _ in range(30)]
        status, result = self.post('/upload', PNG, 'image/png')
        self.assertEqual(status, 409)
        self.assertIn('30', result['error'])
        with mock.patch.object(backgrounds.threading, 'Thread') as thread:
            self.assertEqual(self.post('/generate', {'prompt': 'sky', 'tuned': True})[0], 409)
        thread.assert_not_called()
        backgrounds.delete(items[0]['id'])
        self.assertEqual(self.post('/upload', PNG, 'image/png')[0], 200)

    def test_atomic_index_failure_preserves_existing_items(self):
        first = backgrounds.save(PNG)
        replace = os.replace

        def fail_index(source, target):
            if target.name == 'index.json':
                raise OSError('disk full')
            replace(source, target)

        with mock.patch.object(backgrounds.os, 'replace', side_effect=fail_index):
            self.assertEqual(self.post('/upload', PNG, 'image/png')[0], 503)
        self.assertEqual(self.get()[1]['items'], [first])
        self.assertEqual(len(list(backgrounds.paths.BACKGROUNDS.glob('*.png'))), 1)

    def test_generate_done_command_and_metadata(self):
        ident, args = self.start()
        self.assertEqual(self.get('/generate?job=' + ident), (200, {'state': 'running'}))
        feature, process, _ = self.run_job(args)
        state = self.get('/generate?job=' + ident)[1]
        self.assertEqual(state['state'], 'done')
        self.assertEqual((state['item']['source'], state['item']['prompt']), ('ai', 'quiet hills'))
        self.assertEqual(self.get()[1]['items'], [state['item']])
        command = process.call_args.args[0]
        for flag in ('exec', '--sandbox', 'workspace-write', '--skip-git-repo-check', '--enable', 'image_generation'):
            self.assertIn(flag, command)
        self.assertEqual(feature.call_args.args[0], ['/fake/codex', 'features', 'list'])
        self.assertEqual(process.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(process.call_args.kwargs['start_new_session'])
        self.assertFalse(process.call_args.kwargs['cwd'].exists())

    def test_worker_thread_finishes_and_releases_the_slot(self):
        threads = []
        thread_type = threading.Thread

        def worker(**kwargs):
            thread = thread_type(**kwargs)
            threads.append(thread)
            return thread

        def launch(command, **kwargs):
            (kwargs['cwd'] / 'bg.png').write_bytes(PNG)
            process = mock.Mock()
            process.wait.return_value = 0
            return process

        with mock.patch.object(backgrounds.threading, 'Thread', side_effect=worker), \
                mock.patch.object(backgrounds.shutil, 'which', return_value='/fake/codex'), \
                mock.patch.object(backgrounds.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'image_generation stable true\n', '')), \
                mock.patch.object(backgrounds.subprocess, 'Popen', side_effect=launch):
            ident = backgrounds.generate({'prompt': 'hills', 'tuned': True})['job']
            threads[0].join(timeout=5)
            self.assertFalse(threads[0].is_alive())
        self.assertEqual(self.get('/generate?job=' + ident)[1]['state'], 'done')
        self.assertEqual(len(self.get()[1]['items']), 1)
        self.start()

    def test_missing_codex_and_feature_off_fail_the_job(self):
        ident, args = self.start()
        with mock.patch.object(backgrounds.shutil, 'which', return_value=None), \
                mock.patch.object(backgrounds.subprocess, 'Popen') as process:
            backgrounds._run_job(*args)
        process.assert_not_called()
        self.assertIn('not installed', self.get('/generate?job=' + ident)[1]['error'])
        for features in ('image_generation stable false\n', 'other stable true\n'):
            ident, args = self.start()
            _, process, _ = self.run_job(args, features=features)
            process.assert_not_called()
            self.assertEqual(self.get('/generate?job=' + ident)[1]['state'], 'failed')
            self.assertIn('off or unavailable', self.get('/generate?job=' + ident)[1]['error'])

    def test_conflict_and_job_slot_reservation(self):
        for _ in range(28):
            backgrounds.save(PNG)
        ident, args = self.start()
        self.assertEqual(self.post('/generate', {'prompt': 'sky', 'tuned': True})[0], 409)
        self.assertEqual(self.post('/upload', PNG, 'image/png')[0], 200)
        self.assertEqual(self.post('/upload', PNG, 'image/png')[0], 409)
        self.run_job(args)
        self.assertEqual(self.get('/generate?job=' + ident)[1]['state'], 'done')
        self.assertEqual(len(self.get()[1]['items']), 30)

    def test_tuned_on_off_and_square_prompts(self):
        for tuned, size in ((True, 'landscape'), (True, 'square'), (False, 'landscape'), (False, 'square')):
            ident, args = self.start('green hills', tuned, size)
            _, process, _ = self.run_job(args)
            text = process.call_args.args[0][-1]
            self.assertIn('green hills', text)
            self.assertIn('bg.png', text)
            if tuned:
                for expected in ('frosted-glass', 'center and left', 'no text', 'no logos', 'faces', 'gentle contrast',
                                 '2048x2048' if size == 'square' else '2560x1440'):
                    self.assertIn(expected, text)
            else:
                self.assertTrue(text.startswith('green hills\n\nGenerate one image'))
                self.assertNotIn('frosted-glass', text)
                self.assertNotIn('2560', text)
            self.assertEqual(self.get('/generate?job=' + ident)[1]['state'], 'done')

    def test_generation_validation_and_job_ids(self):
        for body in ({}, {'prompt': '', 'tuned': True}, {'prompt': ' ' * 10, 'tuned': True},
                     {'prompt': 'x' * 1001, 'tuned': True}, {'prompt': 'sky', 'tuned': 1},
                     {'prompt': 'sky', 'tuned': False, 'size': 'wide'}, []):
            self.assertEqual(self.post('/generate', body)[0], 400)
        self.assertEqual(self.get('/generate?job=../x')[0], 400)
        self.assertEqual(self.get('/generate?job=' + 'a' * 32)[0], 404)

    def test_timeout_nonzero_exit_and_missing_image(self):
        for options, expected in (({'timeout': True}, 'too long'), ({'code': 1}, 'could not make'),
                                  ({'write': False}, 'did not save')):
            ident, args = self.start()
            _, _, kill = self.run_job(args, **options)
            state = self.get('/generate?job=' + ident)[1]
            self.assertEqual(state['state'], 'failed')
            self.assertIn(expected, state['error'])
            if options.get('timeout'):
                kill.assert_called_once_with(123456789, backgrounds.signal.SIGKILL)
            self.assertEqual(self.get()[1]['items'], [])

    def test_discovery_only_uses_fresh_scoped_images(self):
        folder = self.root / 'job'
        folder.mkdir()
        generated = backgrounds.paths.CODEX / 'generated_images'
        generated.mkdir(parents=True)
        stale = generated / 'stale.png'
        fresh = generated / 'fresh.png'
        unrelated = generated / 'unrelated.png'
        for at, image in ((1, stale), (3, fresh), (4, unrelated)):
            image.write_bytes(PNG)
            os.utime(image, ns=(at, at))
        with self.assertRaises(backgrounds.Bad):
            backgrounds.find_generated_image(folder, 2, str(stale))
        self.assertEqual(backgrounds.find_generated_image(folder, 2, json.dumps({'path': str(fresh)})), fresh)
        local = folder / 'bg.png'
        local.write_bytes(PNG)
        self.assertEqual(backgrounds.find_generated_image(folder, 2, str(fresh)), local)
        local.unlink()
        local.symlink_to(unrelated)
        with self.assertRaises(backgrounds.Bad):
            backgrounds.find_generated_image(folder, 2, '')

    def test_auth_and_csrf_precede_storage_or_jobs(self):
        for route, body, kind in (('/upload', PNG, 'image/png'), ('/delete', {'id': 'a' * 32}, 'application/json'),
                                  ('/generate', {'prompt': 'sky', 'tuned': True}, 'application/json')):
            for failure in ('cookie', 'origin', 'header'):
                value = handler(route, body, kind)
                if failure == 'cookie':
                    value.deny.return_value = True
                elif failure == 'origin':
                    value.headers['Origin'] = 'https://other.test'
                else:
                    del value.headers['X-Codex-Mobile']
                with mock.patch.object(backgrounds, 'post') as post:
                    value.do_POST()
                post.assert_not_called()
                if failure != 'cookie':
                    self.assertEqual(value.respond.call_args.args[0], 403)
        value = handler(method='GET')
        value.deny.return_value = True
        with mock.patch.object(backgrounds, 'get') as get:
            value.do_GET()
        get.assert_not_called()

    def test_tuned_preference_sync_round_trip(self):
        with mock.patch.object(prefs_sync, 'FILE', self.root / 'prefs.json'):
            for tuned in (False, True):
                prefs_sync.save({'imagePromptTuned': tuned})
                self.assertEqual(prefs_sync.load()['prefs']['imagePromptTuned'], tuned)
            with self.assertRaises(ValueError):
                prefs_sync.save({'imagePromptTuned': 'false'})


if __name__ == '__main__':
    unittest.main()
