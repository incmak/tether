"""Real private-port Handler tests. The live server and tmux are never used."""
import http.client
import io
import json
from email.message import Message
from pathlib import Path
import socket
import threading
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import auth
import paths
import server
import share_api
import share_previews
import share_store


def drain_previews(test, timeout=10):
    """Wait for preview jobs that finished uploads queued. The worker opens the catalog in the test's folder, so it
    must finish before the folder is removed (B22)."""
    jobs = share_previews._queue
    with jobs.all_tasks_done:
        jobs.all_tasks_done.wait_for(lambda: not jobs.unfinished_tasks, timeout)
    test.assertEqual(jobs.unfinished_tasks, 0, 'Share preview jobs still run after the test.')


class ShareAPItests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(drain_previews, self)  # runs before the folder is removed
        self.root = Path(self.temp.name) / 'share'
        for item in (patch.object(auth, 'MODE', 'proxy'), patch.object(paths, 'SHARE', self.root), patch.object(share_api, '_store', None)):
            item.start()
            self.addCleanup(item.stop)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, action, body=None, headers=None):
        sent = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'}
        sent.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        try:
            connection.request(method, share_api.PREFIX + action, body, sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, data=b'hello world', name='hello.txt', mime='text/plain'):
        status, _, body = self.request('POST', 'upload', data, {'X-File-Name': name, 'X-File-Type': mime})
        self.assertEqual(status, 200, body)
        return json.loads(body)['file']

    def post_json(self, action, body):
        return self.request('POST', action, json.dumps(body).encode())

    def test_upload_list_raw_range_and_head(self):
        file = self.upload()
        status, _, body = self.request('GET', 'list')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['files'], [file])
        status, headers, body = self.request('GET', 'raw/' + file['id'], headers={'Range': 'bytes=1-4'})
        self.assertEqual((status, body), (206, b'ello'))
        self.assertEqual(headers['Content-Range'], 'bytes 1-4/11')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        for range_header, expected in [('bytes=-3', b'rld'), ('bytes=6-', b'world')]:
            status, _, body = self.request('GET', 'raw/' + file['id'], headers={'Range': range_header})
            self.assertEqual((status, body), (206, expected))
        for header in ('bytes=99-', 'bytes=5-2', 'bytes=-0', 'bytes=bad', 'bytes=' + '9' * 5000 + '-'):
            status, headers, body = self.request('GET', 'raw/' + file['id'], headers={'Range': header})
            self.assertEqual(status, 416)
            self.assertEqual(headers['Content-Range'], 'bytes */11')
            self.assertIn('error', json.loads(body))
        status, headers, body = self.request('HEAD', 'raw/' + file['id'])
        self.assertEqual((status, body, headers['Content-Length']), (200, b'', '11'))

    def test_source_filters_and_upload_ignores_source_header(self):
        status, _, body = self.request('POST', 'upload', b'browser',
                                       {'X-File-Name': 'browser.txt', 'X-Source': 'agent:x'})
        self.assertEqual(status, 200, body)
        browser = json.loads(body)['file']
        self.assertEqual(browser['source'], 'you')
        agent = share_api.get_store().put(io.BytesIO(b'agent'), 5, 'agent.txt', source='agent:codex')
        for source, expected in (('you', [browser]), ('agent', [agent])):
            status, _, body = self.request('GET', 'list?source=' + source)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['files'], expected)
        status, _, body = self.request('GET', 'list?source=bad')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {'error': 'Choose a valid source.'})

    def test_html_svg_and_claimed_images_are_attachments(self):
        for name, mime, data in [('attack.html', 'text/html', b'<script>bad</script>'),
                                  ('attack.svg', 'image/svg+xml', b'<svg onload="bad"/>'),
                                  ('fake.png', 'image/png', b'<html>bad</html>')]:
            file = self.upload(data, name, mime)
            status, headers, body = self.request('GET', 'raw/' + file['id'])
            self.assertEqual((status, body), (200, data))
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(headers['Content-Security-Policy'], 'sandbox')
            self.assertIn('private', headers['Cache-Control'])
        status, headers, body = self.request('GET', 'raw/' + file['id'] + '?text=1')
        self.assertEqual(status, 400)

    def test_safe_media_inline_download_and_bounded_text(self):
        for name, mime, data in [('pic.png', 'image/png', b'\x89PNG\r\n\x1a\ncontent'),
                                  ('a.pdf', 'application/pdf', b'%PDF-1.7\n'),
                                  ('a.mp4', 'video/mp4', b'\x00\x00\x00\x18ftypisom0000'),
                                  ('a.wav', 'audio/wav', b'RIFF0000WAVE0000')]:
            file = self.upload(data, name, mime)
            status, headers, _ = self.request('GET', 'raw/' + file['id'])
            self.assertEqual((status, headers['Content-Type'], headers['Content-Disposition']), (200, mime, 'inline'))
            status, headers, _ = self.request('GET', 'raw/' + file['id'] + '?download=1')
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        file = self.upload(b'x' * (256 * 1024 + 1))
        status, headers, body = self.request('GET', 'raw/' + file['id'] + '?text=1')
        self.assertEqual((status, len(body)), (200, 256 * 1024))
        self.assertTrue(headers['Content-Type'].startswith('text/plain'))
        empty = self.upload(b'')
        status, _, _ = self.request('GET', 'raw/' + empty['id'], headers={'Range': 'bytes=-1'})
        self.assertEqual(status, 416)

    def test_mutations_thumbnails_traversal_names_and_chat_export(self):
        file = self.upload(name="UTF-8''..%2F..%2Fr%C3%A9sum%C3%A9.txt")
        self.assertEqual(file['name'], 'résumé.txt')
        status, _, body = self.post_json('rename', {'id': file['id'], 'name': '../../new.txt'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['files'][0]['name'], 'new.txt')
        status, _, body = self.post_json('to-chat', {'id': file['id']})
        self.assertEqual(status, 200)
        export = Path(json.loads(body)['path'])
        self.assertEqual(export, self.root / 'exports' / file['id'] / 'new.txt')
        self.assertEqual(export.read_bytes(), b'hello world')
        jpeg = b'\xff\xd8\xffthumbnail'
        status, _, body = self.request('POST', 'thumb/' + file['id'], jpeg, {'Content-Type': 'image/jpeg'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['file']['has_thumb'])
        status, headers, body = self.request('GET', 'thumb/' + file['id'])
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/jpeg', jpeg))
        for action in ('delete', 'restore', 'delete', 'purge'):
            self.assertEqual(self.post_json(action, {'ids': [file['id']]})[0], 200)
            if action == 'delete':
                self.assertEqual(self.request('GET', 'raw/' + file['id'])[0], 404)
                self.assertEqual(len(json.loads(self.request('GET', 'list?trash=1')[2])['files']), 1)
        self.assertFalse(export.exists())
        self.assertEqual(self.request('GET', 'thumb/' + file['id'])[0], 404)

    def test_unauthorized_get_head_post_and_csrf(self):
        with patch.object(auth, 'signed_in', return_value=False), patch.object(auth, 'tailscale_signed_in', return_value=False):
            for method, route in [('GET', 'list'), ('GET', 'raw/missing'), ('HEAD', 'raw/missing'), ('POST', 'upload'), ('POST', 'delete')]:
                self.assertEqual(self.request(method, route, b'x' if method == 'POST' else None)[0], 401)
        for headers in ({'X-Codex-Mobile': ''}, {'Origin': 'http://evil.test'}, {'Origin': ''}):
            self.assertEqual(self.request('POST', 'upload', b'x', headers)[0], 403)
        # http.client does not inject the application header.
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        try:
            connection.request('POST', share_api.PREFIX + 'delete', b'{}', {'Host': 'phone.test', 'Origin': 'http://phone.test'})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()

    def test_bad_body_length_framing_limits_and_metadata(self):
        for headers, status in [({'Content-Length': '-1'}, 400), ({'Content-Length': '9' * 5000}, 413), ({'Transfer-Encoding': 'chunked'}, 400),
                                 ({'Content-Length': str(2 * 1024**3 + 1)}, 413),
                                 ({'X-Width': 'nan'}, 400), ({'X-File-Name': '%zz'}, 400)]:
            sent = {'X-File-Name': 'file.txt'}
            sent.update(headers)
            self.assertEqual(self.request('POST', 'upload', b'x', sent)[0], status)
        self.assertEqual(self.request('POST', 'rename', b'[]')[0], 400)
        self.assertEqual(self.post_json('delete', {'ids': [{}]})[0], 400)
        self.assertEqual(self.post_json('rename', {'id': 'missing', 'name': '\ud800'})[0], 400)
        self.assertEqual(self.request('POST', 'thumb/missing', b'x', {'Content-Type': 'image/jpeg', 'Content-Length': str(512 * 1024 + 1)})[0], 413)
        # Half-close the request so the server sees EOF rather than a client timeout.
        for with_length in (True, False):
            sock = socket.create_connection(self.http.server_address, timeout=5)
            try:
                headers = ('Content-Length: 10\r\n' if with_length else '')
                raw = ('POST /mobile/api/share/upload HTTP/1.0\r\nHost: phone.test\r\nOrigin: http://phone.test\r\n'
                       'X-Codex-Mobile: 1\r\nX-File-Name: short.txt\r\n' + headers + '\r\nshort').encode()
                sock.sendall(raw)
                sock.shutdown(socket.SHUT_WR)
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertEqual(response.status, 400)
                self.assertIn('error', json.loads(response.read()))
            finally:
                sock.close()


class FakeShareHandler(server.Handler):
    """Exercise dispatch and the real authentication gate without a socket."""
    def __init__(self, method, action, body, headers):
        self.command = method
        self.path = share_api.PREFIX + action
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self.headers = Message()
        for key, value in headers.items():
            if value is not None:
                self.headers[key] = value
        self.client_address = ('127.0.0.1', 12345)
        self.close_connection = False
        self.response_headers = {}

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


class ResumableAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(drain_previews, self)  # runs before the folder is removed
        self.root = Path(self.temp.name) / 'share'
        for item in (patch.object(auth, 'MODE', 'proxy'), patch.object(paths, 'SHARE', self.root),
                     patch.object(share_api, '_store', None),
                     patch.object(share_store.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 10**12})())):
            item.start()
            self.addCleanup(item.stop)
        self.store = share_api.get_store()

    def request(self, method, action, body=b'', headers=None):
        sent = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1', 'Content-Length': str(len(body))}
        sent.update(headers or {})
        handler = FakeShareHandler(method, action, body, sent)
        self.assertTrue(share_api.dispatch(handler))
        self.assertEqual(handler.close_connection, method == 'POST')
        self.assertEqual(handler.response_headers['X-Content-Type-Options'], 'nosniff')
        return handler.status, json.loads(handler.wfile.getvalue())

    def create(self, name='note.txt', size=6, **values):
        status, body = self.request('POST', 'uploads', json.dumps(dict(name=name, size=size, **values)).encode())
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {'upload'})
        return body['upload']

    def test_create_list_get_append_three_chunks_finalize_and_single_shot_dedupe(self):
        upload = self.create('../../note.txt', 6, mime='text/plain', width=10, height=20, duration=3)
        identifier = upload['id']
        self.assertEqual(upload['name'], 'note.txt')
        self.assertEqual(upload['offset'], 0)
        self.assertEqual(set(upload), {'id', 'name', 'size', 'offset', 'mime', 'created', 'expires'})
        self.assertEqual(self.request('GET', 'uploads'), (200, {'uploads': [upload]}))
        self.assertEqual(self.request('GET', 'uploads/' + identifier), (200, {'upload': upload}))
        for offset, data in ((0, b'ab'), (2, b'cd'), (4, b'ef')):
            status, result = self.request('POST', f'uploads/{identifier}?offset={offset}', data)
            self.assertEqual(status, 200, result)
            self.assertEqual(result['upload']['offset'], offset + 2)
            self.assertEqual('file' in result, offset == 4)
        file = result['file']
        self.assertEqual((file['size'], file['mime'], file['width'], file['height'], file['duration']), (6, 'text/plain', 10, 20, 3))
        self.assertEqual(self.request('GET', 'uploads'), (200, {'uploads': []}))
        self.assertEqual(self.request('GET', 'uploads/' + identifier)[0], 404)
        status, result = self.request('POST', 'upload', b'abcdef', {'X-File-Name': 'copy.txt'})
        self.assertEqual(status, 200, result)
        with self.store.connection() as db:
            first = self.store.row(db, file['id'])
            second = self.store.row(db, result['file']['id'])
            self.assertEqual(first['sha'], second['sha'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM objects').fetchone()[0], 1)
            self.assertEqual(self.store.object_path(first).read_bytes(), b'abcdef')

    def test_offset_conflicts_overflow_short_body_and_restart(self):
        identifier = self.create()['id']
        route = 'uploads/' + identifier
        self.assertEqual(self.request('POST', route + '?offset=0', b'ab')[0], 200)
        status, body = self.request('POST', route + '?offset=0', b'cd')
        self.assertEqual((status, body['offset']), (409, 2))
        self.assertIn('error', body)
        self.assertEqual(self.request('POST', route + '?offset=2', b'cdefg')[0], 400)
        self.assertEqual(self.request('POST', route + '?offset=2', b'c', {'Content-Length': '2'})[0], 400)
        self.assertEqual(self.request('GET', route)[1]['upload']['offset'], 2)
        self.assertEqual((self.root / 'incoming' / (identifier + '.part')).read_bytes(), b'ab')
        share_api._store = None
        self.assertEqual(self.request('GET', route)[1]['upload']['offset'], 2)
        status, body = self.request('POST', route + '?offset=2', b'cdef')
        self.assertEqual((status, body['upload']['offset'], body['file']['size']), (200, 6, 6))

    def test_busy_append_returns_offset_and_does_not_consume_body(self):
        identifier = self.create()['id']
        with self.store.connection(write=True) as db:
            db.execute('UPDATE uploads SET busy_until=?', (share_store.time.time() + 120,))
        handler = FakeShareHandler('POST', 'uploads/' + identifier + '?offset=0', b'ab',
                                   {'Host': 'phone.test', 'Origin': 'http://phone.test',
                                    'X-Codex-Mobile': '1', 'Content-Length': '2'})
        share_api.dispatch(handler)
        self.assertEqual(handler.status, 409)
        self.assertEqual(json.loads(handler.wfile.getvalue())['offset'], 0)
        self.assertEqual(handler.rfile.tell(), 0)

    def test_cancel_invalid_ids_expiration_and_other_owner(self):
        upload = self.create()
        route = 'uploads/' + upload['id']
        self.assertEqual(self.request('POST', route + '/cancel'), (200, {'ok': True}))
        self.assertFalse((self.root / 'incoming' / (upload['id'] + '.part')).exists())
        for identifier in (upload['id'], 'BAD', 'A' * 32, 'a' * 31, '../bad', 'a' * 33, '0' * 32):
            for method, suffix, body in (('GET', '', b''), ('POST', '?offset=0', b'a'), ('POST', '/cancel', b'')):
                self.assertEqual(self.request(method, 'uploads/' + identifier + suffix, body)[0], 404)
        identifier = self.create()['id']
        with self.store.connection(write=True) as db:
            db.execute("UPDATE uploads SET owner_id='someone'")
        for method, suffix, body in (('GET', '', b''), ('POST', '?offset=0', b'a'), ('POST', '/cancel', b'')):
            with patch.object(share_store, 'authorize', wraps=share_store.authorize) as policy:
                self.assertEqual(self.request(method, 'uploads/' + identifier + suffix, body)[0], 404)
                self.assertTrue(policy.called)
        self.assertEqual(self.request('GET', 'uploads'), (200, {'uploads': []}))
        with self.store.connection(write=True) as db:
            db.execute("UPDATE uploads SET owner_id='local', expires=0")
        self.assertEqual(self.request('GET', 'uploads/' + identifier)[0], 404)

    def test_all_session_routes_use_authentication_and_write_csrf_checks(self):
        identifier = self.create()['id']
        requests = [('GET', 'uploads', b''), ('GET', 'uploads/' + identifier, b''),
                    ('POST', 'uploads', b'{"name":"new","size":1}'),
                    ('POST', 'uploads/' + identifier + '?offset=0', b'a'),
                    ('POST', 'uploads/' + identifier + '/cancel', b'')]
        with patch.object(auth, 'signed_in', return_value=False), patch.object(auth, 'tailscale_signed_in', return_value=False):
            for method, route, body in requests:
                self.assertEqual(self.request(method, route, body)[0], 401)
        for method, route, body in requests[2:]:
            for headers in ({'Origin': None}, {'Origin': 'http://evil.test'}, {'X-Codex-Mobile': None}, {'X-Codex-Mobile': '0'}):
                self.assertEqual(self.request(method, route, body, headers)[0], 403)
        self.assertEqual(len(self.store.list_uploads()), 1)
        self.assertEqual(self.store.get_upload(identifier)['offset'], 0)

    def test_creation_validation_quota_and_ninth_session_limit(self):
        for body in (b'[]', b'null', b'{', b'{}', b'{"name":"x","size":true}',
                     b'{"name":"x","size":-1}', b'{"name":"x","size":1,"width":"nan"}'):
            self.assertEqual(self.request('POST', 'uploads', body)[0], 400)
        body = json.dumps({'name': 'huge', 'size': self.store.max_bytes + 1}).encode()
        self.assertEqual(self.request('POST', 'uploads', body)[0], 413)
        self.create(size=6)
        with patch.object(self.store, 'quota', 10):
            self.assertEqual(self.request('POST', 'uploads', b'{"name":"x","size":5}')[0], 507)
        for _ in range(7):
            self.create(size=1)
        self.assertEqual(self.request('POST', 'uploads', b'{"name":"x","size":1}'),
                         (409, {'error': 'Finish or cancel an upload first.'}))

    def test_append_length_framing_offset_and_chunk_limits(self):
        route = 'uploads/' + self.create()['id']
        for headers, expected in (({'Content-Length': None}, 400), ({'Content-Length': '-1'}, 400),
                                  ({'Content-Length': '0'}, 400), ({'Content-Length': str(8 * share_store.CHUNK + 1)}, 413),
                                  ({'Content-Length': '9' * 5000}, 413), ({'Transfer-Encoding': 'chunked'}, 400)):
            self.assertEqual(self.request('POST', route + '?offset=0', b'a', headers)[0], expected)
        for query in ('', '?offset=-1', '?offset=abc', '?offset=', '?offset=0&offset=0', '?offset=' + '9' * 5000):
            self.assertEqual(self.request('POST', route + query, b'a')[0], 400)
        self.assertEqual(self.store.get_upload(route[8:])['offset'], 0)

    def test_single_shot_http_upload_works_with_three_idle_sessions(self):
        for _ in range(3):
            self.create()
        status, body = self.request('POST', 'upload', b'hello', {'X-File-Name': 'hello.txt'})
        self.assertEqual((status, body['file']['size']), (200, 5))
