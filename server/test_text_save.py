"""Text saves use isolated files and authenticated local HTTP requests."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest import mock

import auth
import files
import media_files
import paths
import server


def digest(data):
    return hashlib.sha256(data).hexdigest()


class TextSaveFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'home'
        self.root.mkdir()
        self.path = self.root / 'notes.txt'
        self.path.write_bytes(b'original')
        self.base = digest(b'original')
        for patch in (mock.patch.dict(os.environ, {'MOBILE_TMUX_FILES_ROOT': str(self.root)}),
                      mock.patch.object(paths, 'SHARE', self.root / 'Share')):
            patch.start()
            self.addCleanup(patch.stop)

    def save(self, text='edited', base=None, path='~/notes.txt'):
        return files.save_text(path, text, self.base if base is None else base)

    def assert_error(self, status, code, call):
        with self.assertRaises(files.FilesError) as error:
            call()
        self.assertEqual((error.exception.status, error.exception.code), (status, code))
        return str(error.exception)


class TextSaveTests(TextSaveFixture):
    def test_save_keeps_mode_and_returns_base_for_second_save(self):
        self.path.chmod(0o640)
        before = self.path.stat().st_ino
        result = self.save('Hello, 世界\n')
        self.assertEqual(result, {'base': digest('Hello, 世界\n'.encode())})
        self.assertEqual(self.path.read_text(), 'Hello, 世界\n')
        self.assertEqual(self.path.stat().st_mode & 0o7777, 0o640)
        self.assertNotEqual(self.path.stat().st_ino, before)
        result = self.save('', result['base'], str(self.path))
        self.assertEqual(result, {'base': digest(b'')})
        self.assertEqual(self.path.read_bytes(), b'')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_wrong_base_preserves_file(self):
        message = self.assert_error(409, 'changed', lambda: self.save(base='0' * 64))
        self.assertEqual(message, 'The file changed on disk. Reload it, then edit again.')
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_non_utf8_preserves_file(self):
        self.path.write_bytes(b'\xff\xfe')
        message = self.assert_error(409, 'binary', lambda: self.save(base=digest(b'\xff\xfe')))
        self.assertEqual(message, 'Tether edits only UTF-8 text files.')
        self.assertEqual(self.path.read_bytes(), b'\xff\xfe')

    def test_refuses_unsafe_and_non_text_items(self):
        (self.root / 'link.txt').symlink_to(self.path)
        (self.root / '.env').write_bytes(b'original')
        (self.root / 'report.docx').write_bytes(b'original')
        (self.root / 'folder.txt').mkdir()
        (self.root / 'Share').mkdir()
        (self.root / 'Share' / 'notes.txt').write_bytes(b'original')
        cases = [('~/link.txt', 403, 'link'), ('~/.env', 403, 'secret'),
                 ('~/report.docx', 400, 'invalid'), ('~/folder.txt', 400, 'invalid'),
                 ('~/missing.txt', 404, 'missing'), ('~/Share/notes.txt', 403, 'share'),
                 (str(self.root.parent / 'outside.txt'), 403, 'outside')]
        for path, status, code in cases:
            with self.subTest(path=path):
                message = self.assert_error(status, code, lambda: self.save(path=path))
                if path.endswith('.docx'):
                    self.assertEqual(message, 'Tether edits only text files.')
        for path in (self.path, self.root / '.env', self.root / 'report.docx', self.root / 'Share' / 'notes.txt'):
            self.assertEqual(path.read_bytes(), b'original')

    def test_refuses_fifo_without_blocking(self):
        os.mkfifo(self.root / 'pipe.txt')
        self.assert_error(400, 'invalid', lambda: self.save(path='~/pipe.txt'))

    def test_uses_media_text_rules(self):
        for name in ('source.PY', 'Makefile', 'README', 'notes.md'):
            with self.subTest(name=name):
                (self.root / name).write_bytes(b'original')
                self.assertEqual(self.save(path='~/' + name), {'base': digest(b'edited')})
        # The explorer also calls .rst text; the media rules do not.
        (self.root / 'notes.rst').write_bytes(b'original')
        self.assert_error(400, 'invalid', lambda: self.save(path='~/notes.rst'))

    def test_text_limit_counts_utf8_bytes(self):
        text = 'é' * (media_files.TEXT_LIMIT // 2)
        result = self.save(text)
        self.assertEqual(result, {'base': digest(text.encode())})
        self.assert_error(413, 'too-large', lambda: self.save(text + 'é', result['base']))
        self.assertEqual(self.path.read_bytes(), text.encode())

    def test_bad_text_and_base(self):
        for base in ('', '0' * 63, '0' * 65, 'A' * 64, 'g' * 64, '0' * 64 + '\n', 1, [], {}):
            with self.subTest(base=base):
                self.assert_error(400, 'invalid', lambda: self.save(base=base))
        for text in (None, 123, [], {}, '\ud800'):
            with self.subTest(text=repr(text)):
                self.assert_error(400, 'invalid', lambda: self.save(text))
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_external_change_during_write_is_preserved(self):
        write = files.write_all

        def external_edit(fd, data):
            write(fd, data)
            self.path.write_bytes(b'external edit')

        with mock.patch.object(files, 'write_all', side_effect=external_edit):
            self.assert_error(409, 'changed', self.save)
        self.assertEqual(self.path.read_bytes(), b'external edit')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_target_swap_during_write_is_preserved(self):
        write = files.write_all
        original = self.path.stat()

        def swap_target(fd, data):
            write(fd, data)
            replacement = self.root / 'replacement'
            replacement.write_bytes(b'external')  # Same size and mtime; inode differs.
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            replacement.replace(self.path)

        with mock.patch.object(files, 'write_all', side_effect=swap_target):
            self.assert_error(409, 'changed', self.save)
        self.assertEqual(self.path.read_bytes(), b'external')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_failed_replace_cleans_temp_and_maps_disk_error(self):
        with mock.patch.object(os, 'rename', side_effect=OSError(errno.ENOSPC, 'No space')):
            self.assert_error(507, 'failed', self.save)
        self.assertEqual(self.path.read_bytes(), b'original')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_concurrent_saves_with_one_base_allow_one_writer(self):
        start = threading.Barrier(2, timeout=3)

        def save(text):
            start.wait()
            try:
                return 200, self.save(text)
            except files.FilesError as exc:
                return exc.status, exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ('first', 'second')))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(result for status, result in results if status == 200)
        self.assertEqual(winner, {'base': digest(self.path.read_bytes())})


class TextSaveRouteTests(TextSaveFixture):
    def setUp(self):
        super().setUp()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        for patch in (mock.patch.object(auth, 'signed_in', return_value=True),
                      mock.patch.object(auth, 'tailscale_signed_in', return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request(self, body=None, headers=None):
        if body is None:
            body = {'path': '~/notes.txt', 'text': 'edited', 'base': self.base}
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if headers is None:
            headers = {'Host': 'tether.test', 'Origin': 'http://tether.test', 'X-Codex-Mobile': '1'}
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            connection.request('POST', '/mobile/api/files/save', body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_route_saves_and_resolves_relative_session_path(self):
        folder = self.root / 'project'
        folder.mkdir()
        target = folder / 'code.py'
        target.write_bytes(b'original')
        entry = {'id': 'selected-tab'}
        with mock.patch.object(server, 'choose', return_value=entry) as choose, \
                mock.patch.object(server, 'tab_folder', return_value=str(folder)) as tab_folder:
            status, body = self.request({'path': 'code.py', 'session': 'selected-tab', 'text': 'new', 'base': self.base})
        self.assertEqual((status, body), (200, {'base': digest(b'new')}))
        choose.assert_called_once_with('selected-tab')
        tab_folder.assert_called_once_with(entry)
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_route_missing_header_or_wrong_origin_writes_nothing(self):
        for headers in ({'Host': 'tether.test', 'Origin': 'http://tether.test'},
                        {'Host': 'tether.test', 'Origin': 'http://other.test', 'X-Codex-Mobile': '1'}):
            with self.subTest(headers=headers), mock.patch.object(files, 'save_text') as save:
                self.assertEqual(self.request(headers=headers)[0], 403)
                save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_route_signed_out_uses_deny(self):
        deny = server.Handler.deny
        with mock.patch.object(auth, 'signed_in', return_value=False), \
                mock.patch.object(server.Handler, 'deny', autospec=True, side_effect=deny) as checked, \
                mock.patch.object(files, 'save_text') as save:
            self.assertEqual(self.request(), (401, {'error': 'Sign in required.'}))
            checked.assert_called_once()
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_route_bad_json_and_fields(self):
        invalid = [b'{', b'\xff', [], b'null', b'1', b'"text"',
                   {}, {'path': '~/notes.txt', 'text': 1, 'base': self.base},
                   {'path': '~/notes.txt', 'text': 'edited', 'base': 'A' * 64}]
        for value in invalid:
            with self.subTest(value=value):
                status, body = self.request(value)
                self.assertEqual((status, body['code']), (400, 'invalid'))
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_route_checks_content_length_and_text_byte_limit(self):
        for length, expected in (('bad', 400), ('-1', 400), ('0', 400), (str(3 * 1024 * 1024 + 1), 413)):
            headers = {'Host': 'tether.test', 'Origin': 'http://tether.test', 'X-Codex-Mobile': '1', 'Content-Length': length}
            with self.subTest(length=length):
                self.assertEqual(self.request(b'{}', headers)[0], expected)
        status, body = self.request({'path': '~/notes.txt', 'text': 'x' * (media_files.TEXT_LIMIT + 1), 'base': self.base})
        self.assertEqual((status, body['code']), (413, 'too-large'))
        self.assertEqual(self.path.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
