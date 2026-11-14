"""Chat UI compression tests use temporary assets and no sockets or terminals."""
import gzip
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


class ChatUIGzipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'assets').mkdir()
        self.script = b'const message = "Hello, phone!";\n' * 100
        self.index = b'<!doctype html><title>Tether</title>'
        self.icon = b'\x89PNG\r\n\x1a\n' + b'\x00' * 2048
        (self.root / 'assets/app-abc.js').write_bytes(self.script)
        (self.root / 'index.html').write_bytes(self.index)
        (self.root / 'assets/icon.png').write_bytes(self.icon)
        root_patch = patch.object(server, 'CHAT_UI_ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        cache_patch = patch.object(server, 'CHAT_UI_GZIP_CACHE', {})
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def request(self, path='/chat/assets/app-abc.js', encoding=None):
        handler = server.Handler.__new__(server.Handler)
        handler.command, handler.path = 'GET', path
        handler.headers = {} if encoding is None else {'Accept-Encoding': encoding}
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.serve_chat_ui(path)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        body = handler.wfile.getvalue()
        self.assertEqual(int(headers['Content-Length']), len(body))
        return handler.send_response.call_args.args[0], headers, body

    def test_gzip_body_and_headers(self):
        status, headers, body = self.request(encoding='gzip, br')
        self.assertEqual(status, 200)
        self.assertEqual(gzip.decompress(body), self.script)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(headers['Vary'], 'Accept-Encoding')
        self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'max-age=31536000, immutable')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_encoding_negotiation(self):
        cases = [(None, False), ('gzip;q=0', False), ('br', False),
                 ('xgzip', False), ('gzip;q=0, *;q=1', False),
                 ('gzip;q=0.0, br', False), ('gzip;q=invalid', False),
                 ('br, GZip ; q=0.5', True), ('*;q=0.5', True)]
        for encoding, compressed in cases:
            with self.subTest(encoding=encoding):
                _, headers, body = self.request(encoding=encoding)
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                if compressed:
                    self.assertEqual(headers['Content-Encoding'], 'gzip')
                    self.assertEqual(gzip.decompress(body), self.script)
                else:
                    self.assertNotIn('Content-Encoding', headers)
                    self.assertEqual(body, self.script)

    def test_small_index_and_spa_fallback_stay_raw(self):
        for path in ('/chat/', '/chat/unknown-route'):
            with self.subTest(path=path):
                _, headers, body = self.request(path, 'gzip')
                self.assertEqual(body, self.index)
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                self.assertEqual(headers['Cache-Control'], 'no-cache')

    def test_all_text_suffixes_and_size_boundary(self):
        for suffix in ('.html', '.js', '.mjs', '.css', '.svg', '.json', '.webmanifest'):
            for size in (1023, 1024):
                with self.subTest(suffix=suffix, size=size):
                    target = self.root / ('example' + suffix)
                    original = b'x' * size
                    target.write_bytes(original)
                    _, headers, body = self.request('/chat/' + target.name, 'gzip')
                    self.assertEqual(headers['Vary'], 'Accept-Encoding')
                    if size == 1024:
                        self.assertEqual(headers['Content-Encoding'], 'gzip')
                        self.assertEqual(gzip.decompress(body), original)
                    else:
                        self.assertNotIn('Content-Encoding', headers)
                        self.assertEqual(body, original)

    def test_binary_types_stay_raw(self):
        for suffix in ('.png', '.wasm', '.onnx', '.woff2', '.ico'):
            with self.subTest(suffix=suffix):
                target = self.root / 'assets' / ('icon' + suffix)
                target.write_bytes(self.icon)
                with patch.object(server.gzip, 'compress') as compress:
                    _, headers, body = self.request('/chat/assets/' + target.name, 'gzip')
                compress.assert_not_called()
                self.assertEqual(body, self.icon)
                self.assertNotIn('Content-Encoding', headers)
                self.assertNotIn('Vary', headers)

    def test_cache_hit_does_not_recompress(self):
        with patch.object(server.gzip, 'compress', wraps=gzip.compress) as compress:
            first = self.request(encoding='gzip')[2]
            second = self.request(encoding='gzip')[2]
        self.assertEqual(first, second)
        compress.assert_called_once_with(self.script, compresslevel=6, mtime=0)
        info = (self.root / 'assets/app-abc.js').stat()
        self.assertIn(((self.root / 'assets/app-abc.js').resolve(), info.st_mtime_ns, info.st_size),
                      server.CHAT_UI_GZIP_CACHE)

    def test_changed_mtime_recompresses_same_size_file(self):
        target = self.root / 'assets/app-abc.js'
        with patch.object(server.gzip, 'compress', wraps=gzip.compress) as compress:
            self.request(encoding='gzip')
            info = target.stat()
            changed = self.script.replace(b'phone', b'world')
            target.write_bytes(changed)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            _, _, body = self.request(encoding='gzip')
        self.assertEqual(compress.call_count, 2)
        self.assertEqual(gzip.decompress(body), changed)

    def test_cache_is_bounded(self):
        with patch.object(server, 'CHAT_UI_GZIP_LIMIT', 100):
            self.request(encoding='gzip')
            (self.root / 'other.js').write_bytes(self.script + b'// different')
            self.request('/chat/other.js', 'gzip')
            self.assertLessEqual(sum(map(len, server.CHAT_UI_GZIP_CACHE.values())), 100)
            self.assertEqual(len(server.CHAT_UI_GZIP_CACHE), 1)
        with patch.object(server, 'CHAT_UI_GZIP_LIMIT', 1):
            self.request(encoding='gzip')
            self.assertEqual(server.CHAT_UI_GZIP_CACHE, {})

    def test_prebuilt_brotli_for_hashed_assets(self):
        packed = b'brotli bytes from the build'
        (self.root / 'assets/app-abc.js.br').write_bytes(packed)
        for encoding, expected in (('gzip, deflate, br, zstd', 'br'), ('BR;q=0.5', 'br'), ('*', 'br'),
                                   ('gzip', 'gzip'), ('br;q=0, gzip', 'gzip'), (None, None)):
            with self.subTest(encoding=encoding):
                status, headers, body = self.request(encoding=encoding)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
                self.assertEqual(headers['Cache-Control'], 'max-age=31536000, immutable')
                self.assertEqual(headers.get('Content-Encoding'), expected)
                if expected == 'br':
                    self.assertEqual(body, packed)
                elif expected == 'gzip':
                    self.assertEqual(gzip.decompress(body), self.script)
                else:
                    self.assertEqual(body, self.script)

    def test_brotli_only_for_assets_folder(self):
        # index.html and other files outside assets/ are not hashed, so a .br beside them could be stale.
        (self.root / 'index.html').write_bytes(self.index * 40)
        (self.root / 'index.html.br').write_bytes(b'stale')
        (self.root / 'example.js').write_bytes(self.script)
        (self.root / 'example.js.br').write_bytes(b'stale')
        (self.root / 'assets/icon.png.br').write_bytes(b'stale')
        for path in ('/chat/', '/chat/example.js', '/chat/assets/icon.png'):
            with self.subTest(path=path):
                _, headers, body = self.request(path, 'br, gzip')
                self.assertNotEqual(headers.get('Content-Encoding'), 'br')
                self.assertNotEqual(body, b'stale')

    def test_missing_brotli_file_falls_back_to_gzip(self):
        _, headers, body = self.request(encoding='br, gzip')
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(gzip.decompress(body), self.script)

    def test_path_traversal_stays_rejected(self):
        status, _, _ = self.request('/chat/%2e%2e/outside.js', 'gzip')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
