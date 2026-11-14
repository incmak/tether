"""API response compression tests use a mocked handler: no sockets, no terminals."""
import gzip
import io
import json
import unittest
from unittest.mock import Mock

import server


class APIGzipTests(unittest.TestCase):
    payload = {'messages': [{'role': 'assistant', 'text': 'Hello, phone! ' * 20} for _ in range(20)]}

    def respond(self, *args, encoding='gzip, deflate, br', site='same-origin', **kwargs):
        handler = server.Handler.__new__(server.Handler)
        handler.command, handler.path = 'GET', '/mobile/api/chat'
        handler.headers = {} if encoding is None else {'Accept-Encoding': encoding}
        if site is not None:
            handler.headers['Sec-Fetch-Site'] = site
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.respond(*args, **kwargs)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        body = handler.wfile.getvalue()
        self.assertEqual(int(headers['Content-Length']), len(body))
        return headers, body

    def test_large_json_is_gzipped(self):
        headers, body = self.respond(200, self.payload)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(headers['Vary'], 'Accept-Encoding, Sec-Fetch-Site')
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(gzip.decompress(body)), self.payload)
        self.assertLess(len(body), len(json.dumps(self.payload)) / 4)

    def test_client_without_gzip_gets_raw_json(self):
        for encoding in (None, 'br', 'gzip;q=0', 'identity'):
            with self.subTest(encoding=encoding):
                headers, body = self.respond(200, self.payload, encoding=encoding)
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(headers['Vary'], 'Accept-Encoding, Sec-Fetch-Site')
                self.assertEqual(json.loads(body), self.payload)

    def test_only_same_origin_requests_get_gzip(self):
        # Tailscale-identity sign-in needs no cookie, so a cross-site page could time compressed
        # sizes of signed-in responses that reflect its input (BREACH). Those stay raw.
        for site, compressed in (('same-origin', True), ('none', True), (None, True),
                                 ('same-site', False), ('cross-site', False), ('bogus', False)):
            with self.subTest(site=site):
                headers, body = self.respond(200, self.payload, site=site)
                self.assertEqual('Content-Encoding' in headers, compressed)
                self.assertEqual(headers['Vary'], 'Accept-Encoding, Sec-Fetch-Site')
                self.assertEqual(json.loads(gzip.decompress(body) if compressed else body), self.payload)

    def test_size_boundary(self):
        for size, compressed in ((server.GZIP_MIN - 1, False), (server.GZIP_MIN, True)):
            with self.subTest(size=size):
                headers, body = self.respond(200, 'x' * size, 'text/plain')
                self.assertEqual('Content-Encoding' in headers, compressed)
                self.assertEqual(gzip.decompress(body) if compressed else body, b'x' * size)

    def test_error_json_is_compressed_too(self):
        headers, body = self.respond(500, {'error': 'e' * 2000})
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(json.loads(gzip.decompress(body)), {'error': 'e' * 2000})

    def test_binary_and_preencoded_bodies_stay_as_given(self):
        image = b'\x89PNG' + b'\x00' * 4096
        headers, body = self.respond(200, image, 'image/png')
        self.assertEqual(body, image)
        self.assertNotIn('Content-Encoding', headers)
        self.assertNotIn('Vary', headers)
        packed = gzip.compress(b'{}' * 2000)
        given = {'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding'}
        headers, body = self.respond(200, packed, 'application/json', headers=given)
        self.assertEqual(body, packed)
        self.assertEqual(given, {'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding'})

    def test_no_body_statuses_stay_raw(self):
        headers, body = self.respond(304, b'', 'application/json')
        self.assertNotIn('Content-Encoding', headers)
        self.assertEqual(body, b'')

    def test_accepts_gzip(self):
        cases = [(None, False), ('', False), ('gzip', True), ('GZip ; q=0.5', True), ('*;q=0.5', True),
                 ('gzip;q=0, *;q=1', False), ('xgzip', False), ('gzip;q=invalid', False), ('gzip;q=2', False)]
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(server.accepts_gzip(header), expected)


if __name__ == '__main__':
    unittest.main()
