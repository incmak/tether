"""Archive contents, release fallback, and the signed-in download route."""
import http.client
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from functools import partial
from pathlib import Path
from unittest.mock import patch

import auth
import browser_api
import extension_zip
import files
import server
from test_browser import FakeHandler


ROUTE = '/mobile/api/browser/extension/download'
FILENAME = 'tether-extension-1.2.3.zip'
NAMES = ['tether-extension/a.js', 'tether-extension/manifest.json', 'tether-extension/sub/b.css']


def git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=Extension Test',
                           '-c', 'user.email=extension@example.test', '-c', 'core.filemode=true',
                           '-c', 'core.symlinks=true', '-c', 'core.hooksPath=/dev/null',
                           '-c', 'commit.gpgsign=false', *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=10, check=True).stdout


class DownloadHandler(FakeHandler):
    def __init__(self):
        super().__init__(ROUTE)
        self.status = None
        self.response_headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


class ExtensionZipTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp) / 'repo'
        self.extension = self.root / 'extension'
        self.extension.mkdir(parents=True)
        self.manifest = self.extension / 'manifest.json'
        self.manifest.write_text('{"version": "1.2.3"}')

    def checkout(self):
        (self.extension / 'a.js').write_bytes(b'committed script')
        sub = self.extension / 'sub'
        sub.mkdir()
        css = sub / 'b.css'
        css.write_bytes(b'committed style')
        css.chmod(0o755)
        (self.extension / 'link.js').symlink_to('a.js')
        (self.root / 'other.txt').write_bytes(b'outside the extension')
        git(self.root, 'init', '-q')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'Add extension fixture')
        (self.extension / 'untracked.js').write_bytes(b'untracked script')
        (self.extension / 'a.js').write_bytes(b'edited script')
        self.manifest.write_text('{"version": "9.9.9"}')

    def assert_checkout_zip(self, root):
        filename, data = extension_zip.build(root)
        self.assertEqual(filename, FILENAME)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), NAMES)
            self.assertEqual(archive.read(NAMES[0]), b'committed script')
            self.assertEqual(json.loads(archive.read(NAMES[1])), {'version': '1.2.3'})

    def test_checkout_uses_only_committed_extension_files(self):
        self.checkout()
        with patch.dict(os.environ, {'GIT_DIR': '/missing/git', 'GIT_WORK_TREE': '/missing/tree',
                                     'GIT_INDEX_FILE': '/missing/index'}):
            self.assert_checkout_zip(self.root)
        script = self.extension / 'a.js'
        script.unlink()
        script.symlink_to('../other.txt')
        with zipfile.ZipFile(io.BytesIO(extension_zip.build(self.root, use_cache=False)[1])) as archive:
            self.assertEqual(archive.namelist(), NAMES)
            self.assertEqual(archive.read(NAMES[0]), b'committed script')

    def test_checkout_is_deterministic_with_fixed_metadata(self):
        self.checkout()
        first = extension_zip.build(self.root, use_cache=False)
        self.assertEqual(extension_zip.build(self.root, use_cache=False), first)
        script = self.extension / 'a.js'
        stat = script.stat()
        os.utime(script, ns=(stat.st_atime_ns, stat.st_mtime_ns + 3_000_000_000))
        self.assertEqual(extension_zip.build(self.root, use_cache=False), first)
        with zipfile.ZipFile(io.BytesIO(first[1])) as archive:
            for entry, mode in zip(archive.infolist(), (0o100644, 0o100644, 0o100755), strict=True):
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(entry.create_system, 3)
                self.assertEqual(entry.external_attr, mode << 16)
                self.assertEqual(entry.compress_type, zipfile.ZIP_DEFLATED)

    def test_linked_worktree(self):
        self.checkout()
        worktree = Path(self.temp) / 'linked'
        git(self.root, 'worktree', 'add', '-q', '--detach', str(worktree))
        self.assertTrue((worktree / '.git').is_file())
        self.assert_checkout_zip(worktree)

    def test_checkout_cache_follows_head(self):
        self.checkout()
        self.assertEqual(extension_zip.build(self.root)[0], FILENAME)
        git(self.root, 'add', 'extension/manifest.json')
        git(self.root, 'commit', '-qm', 'Update extension version')
        self.assertEqual(extension_zip.build(self.root)[0], 'tether-extension-9.9.9.zip')

    def test_release_ignores_symlinks_and_special_files(self):
        script = self.extension / 'launch'
        script.write_bytes(b'release script')
        script.chmod(0o700)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'private').write_bytes(b'outside the extension')
        (self.extension / 'link').symlink_to(outside / 'private')
        (self.extension / 'link-dir').symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.extension / 'pipe')
        filename, data = extension_zip.build(self.root)
        self.assertEqual(filename, FILENAME)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), ['tether-extension/launch', NAMES[1]])
            self.assertEqual(archive.read('tether-extension/launch'), b'release script')
            self.assertEqual(archive.getinfo('tether-extension/launch').external_attr, 0o100755 << 16)
        self.manifest.write_text('{"version": "4"}')
        self.assertEqual(extension_zip.build(self.root)[0], 'tether-extension-4.zip')

    def test_release_refuses_symlinked_extension_folder(self):
        outside = Path(self.temp) / 'outside'
        self.extension.rename(outside)
        self.extension.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(extension_zip.ExtensionZipError):
            extension_zip.build(self.root)

    def test_invalid_manifests_fail_the_build_and_route(self):
        self.manifest.unlink()
        build = partial(extension_zip.build, self.root)
        for manifest in (None, b'broken JSON', b'\xff', b'{"version": "1", "field": NaN}',
                         b'[]', b'{}', b'{"version": 1}',
                         b'{"version": "1.2.3.4.5"}', b'{"version": "1.2\\n"}'):
            with self.subTest(manifest=manifest):
                if manifest is not None:
                    self.manifest.write_bytes(manifest)
                with self.assertRaises(extension_zip.ExtensionZipError) as result:
                    build()
                handler = DownloadHandler()
                with patch.object(extension_zip, 'build', build):
                    browser_api.get(handler)
                self.assertEqual(handler.replies, [(500, {'error': 'Could not build the extension zip. ' + str(result.exception)})])
                self.assertEqual(handler.wfile.getvalue(), b'')

    def test_file_count_cap_in_release_and_checkout(self):
        for index in range(199):
            (self.extension / str(index)).touch()
        with zipfile.ZipFile(io.BytesIO(extension_zip.build(self.root)[1])) as archive:
            self.assertEqual(len(archive.namelist()), 200)
        (self.extension / 'one-too-many').touch()
        with self.assertRaises(extension_zip.ExtensionZipError):
            extension_zip.build(self.root)
        git(self.root, 'init', '-q')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'Add too many files')
        with self.assertRaises(extension_zip.ExtensionZipError):
            extension_zip.build(self.root)

    def test_total_size_cap_in_release_and_checkout(self):
        for name in ('a', 'b'):
            (self.extension / name).write_bytes(b'x' * (3 * 1024 * 1024))
        with self.assertRaises(extension_zip.ExtensionZipError):
            extension_zip.build(self.root)
        git(self.root, 'init', '-q')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'Add oversized extension')
        with self.assertRaises(extension_zip.ExtensionZipError):
            extension_zip.build(self.root)

    def test_browser_route_sends_zip_and_download_headers(self):
        handler = DownloadHandler()
        with patch.object(extension_zip, 'build', partial(extension_zip.build, self.root)):
            browser_api.get(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.replies, [])
        data = handler.wfile.getvalue()
        self.assertEqual(handler.response_headers, {
            'Content-Type': 'application/zip',
            'Content-Length': str(len(data)),
            'Content-Disposition': files.disposition(FILENAME),
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        })
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), [NAMES[1]])
            self.assertEqual(archive.read(NAMES[1]), self.manifest.read_bytes())

    def test_browser_route_does_not_expose_paths_on_build_failure(self):
        for error in (OSError(2, 'No such file', '/private/location'),
                      subprocess.CalledProcessError(1, ['git', '-C', '/private/location'])):
            with self.subTest(error=error):
                handler = DownloadHandler()
                with patch.object(extension_zip, 'build', side_effect=error):
                    browser_api.get(handler)
                [(status, body)] = handler.replies
                self.assertEqual(status, 500)
                self.assertTrue(body['error'].startswith('Could not build the extension zip. '))
                self.assertNotIn('/private/location', body['error'])
                self.assertEqual(handler.wfile.getvalue(), b'')

    def test_real_checkout_matches_tracked_extension_files(self):
        root = Path(__file__).resolve().parents[1]
        paths = git(root, 'ls-files', '-z', '--', 'extension').split(b'\0')
        expected = ['tether-extension/' + path.decode().removeprefix('extension/')
                    for path in sorted(filter(None, paths))]
        with zipfile.ZipFile(io.BytesIO(extension_zip.build()[1])) as archive:
            self.assertEqual(archive.namelist(), expected)

    def test_real_handler_requires_sign_in_to_download(self):
        self.enterContext(patch.object(auth, 'MODE', 'token'))
        self.enterContext(patch.object(auth, 'TAILSCALE', False))
        self.enterContext(patch.object(auth, 'token', return_value='test-extension-token'))
        self.enterContext(patch.object(extension_zip, 'build', partial(extension_zip.build, self.root)))
        listener = self.enterContext(server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler))
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(listener.shutdown)

        def request(headers):
            connection = http.client.HTTPConnection(*listener.server_address, timeout=5)
            try:
                connection.request('GET', ROUTE, headers=headers)
                response = connection.getresponse()
                return response.status, response.getheader('Content-Type'), response.read()
            finally:
                connection.close()

        status, content_type, data = request({})
        self.assertEqual((status, content_type), (401, 'application/json; charset=utf-8'))
        self.assertEqual(json.loads(data), {'error': 'Sign in required.'})
        cookie = f'{auth.COOKIE}={auth.session_value()}'
        status, content_type, data = request({'Cookie': cookie})
        self.assertEqual((status, content_type), (200, 'application/zip'))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), [NAMES[1]])
            self.assertEqual(archive.read(NAMES[1]), self.manifest.read_bytes())
