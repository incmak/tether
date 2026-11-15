"""Office integration against isolated files, mocked Docker and local HTTP servers."""
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import auth
import files
import office_addon as office
import paths
import server
from own_time import own_time


class ResultHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.command, self.path, dict(self.headers), b''))
        if self.path == '/healthcheck':
            data = b'true'
        else:
            data = self.server.data
        if self.server.redirect:
            self.send_response(302)
            self.send_header('Location', self.server.redirect)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(200)
        if not self.server.no_length:
            self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.headers.get('Transfer-Encoding') == 'chunked':
            data = b''
            while size := int(self.rfile.readline(), 16):
                data += self.rfile.read(size)
                self.rfile.read(2)
            self.rfile.read(2)
        else:
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append((self.command, self.path, dict(self.headers), data))
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_PUT = do_POST


class UpgradeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.received = (self.path, dict(self.headers))
        self.send_response(101)
        self.send_header('Connection', 'Upgrade')
        self.send_header('Upgrade', 'websocket')
        self.end_headers()
        self.wfile.write(self.rfile.read(4))


class OfficeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'home'
        self.root.mkdir()
        self.path = self.root / 'Report.docx'
        self.path.write_bytes(b'original')
        self.config_path = self.base / 'office.json'
        patches = [mock.patch.dict(os.environ, {'MOBILE_TMUX_FILES_ROOT': str(self.root),
                                               'TETHER_OFFICE_CONFIG': str(self.config_path)}),
                   mock.patch.object(paths, 'SHARE', self.root / 'Share'),
                   mock.patch.dict(office._config_cache, {'path': None, 'until': 0, 'value': None}, clear=True),
                   mock.patch.dict(office._health_cache, {'url': None, 'until': 0, 'value': False}, clear=True),
                   mock.patch.dict(office._saved, clear=True), mock.patch.dict(office._copies, clear=True),
                   mock.patch.dict(office._locks, clear=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.config = {'url': 'http://127.0.0.1:7695', 'secret': 's' * 43, 'listen_host': '127.0.0.1',
                       'listen_port': 7696, 'editor_port': 7697, 'public_host': 'host.docker.internal',
                       'container': 'tether-office', 'image': 'onlyoffice/documentserver:10.0'}

    def serve(self, handler):
        http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=lambda: http.serve_forever(poll_interval=.01), daemon=True)
        thread.start()

        def stop():
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        return http

    def enable(self, upstream=False):
        if upstream:
            self.upstream = self.serve(ResultHandler)
            self.upstream.data = b'edited'
            self.upstream.requests = []
            self.upstream.redirect = None
            self.upstream.no_length = False
            self.config['url'] = 'http://127.0.0.1:%d' % self.upstream.server_port
        office.write_config(self.config)

    def document(self, **kwargs):
        result = office.open_document('~/Report.docx', 'Owner', False, 'en', **kwargs)
        self.cap_token = result['config']['document']['url'].rsplit('/', 1)[1]
        self.cap = office.verify_capability(self.cap_token, self.config['secret'])
        return result

    def request(self, listener, method, path, body=None, headers=None, chunked=False):
        connection = http.client.HTTPConnection('127.0.0.1', listener.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request(method, path, body, headers or {}, encode_chunked=chunked)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def callback(self, status=2, header=False, **values):
        claims = dict(key=self.cap['k'], status=status, url='https://untrusted.invalid/cache/files/result?download=1')
        claims.update(values)
        headers = {}
        if header:
            headers['Authorization'] = 'Bearer ' + office.sign({'payload': claims}, self.config['secret'])
            body = {'key': 'unverified', 'status': 7}
        else:
            body = {'token': office.sign(claims, self.config['secret']), 'status': 7}
        code, data = self.request(self.listener, 'POST', '/office/callback/' + self.cap_token, json.dumps(body), headers)
        return code, json.loads(data) if data else None

    def ready(self):
        self.enable(upstream=True)
        self.document()
        self.listener = self.serve(office.OfficeHandler)

    def test_disabled(self):
        self.assertIsNone(office.load())
        self.assertFalse(office.enabled())
        self.assertEqual(office.status(), {'enabled': False, 'healthy': False})
        with mock.patch.object(office, 'ThreadingHTTPServer') as bind:
            self.assertIsNone(office.start_listener())
            bind.assert_not_called()
        with self.assertRaises(files.FilesError) as error:
            office.open_document('~/Report.docx')
        self.assertEqual((error.exception.status, error.exception.code), (404, 'office-off'))

    def test_config_validation_and_cache(self):
        invalid = ['{', '[]', '{}', json.dumps({key: value for key, value in self.config.items() if key != 'editor_port'})]
        for key, value in [('secret', 3), ('listen_port', True), ('url', 'https://127.0.0.1:7695'),
                           ('url', 'http://evil.test:7695'), ('url', 'http://localhost:99999'),
                           ('url', 'http://127.0.0.1:7695/path'), ('listen_host', 1), ('public_host', 'x/y'),
                           ('editor_port', True), ('editor_port', '7697'), ('editor_port', None),
                           ('editor_port', 0), ('editor_port', -1), ('editor_port', 65536),
                           ('editor_port', 7695), ('editor_port', 7696)]:
            invalid.append(json.dumps(dict(self.config, **{key: value})))
        for text in invalid:
            self.config_path.write_text(text)
            office._config_cache['until'] = 0
            with self.assertLogs(office.LOG, level='WARNING') as logs:
                self.assertIsNone(office.load())
                self.assertIsNone(office.load())
            self.assertEqual(len(logs.output), 1)
        self.enable()
        self.assertEqual(office.load(), self.config)
        self.config_path.unlink()
        self.assertEqual(office.load(), self.config)
        office._config_cache['until'] = 0
        self.assertIsNone(office.load())

    def test_health_cache(self):
        self.enable(upstream=True)
        self.assertEqual(office.status(), {'enabled': True, 'healthy': True})
        self.assertTrue(office.status()['healthy'])
        self.assertEqual(len(self.upstream.requests), 1)
        self.upstream.redirect = '/somewhere'
        office._health_cache['until'] = 0
        self.assertFalse(office.status()['healthy'])

    def test_jwt_round_trip(self):
        self.assertEqual(office.verify(office.sign({'hello': 1}, 'secret'), 'secret'), {'hello': 1})

    def test_jwt_refuses_invalid_tokens(self):
        token = office.sign({'hello': 1}, 'secret')
        head, body, signature = token.split('.')
        none = office._encode(b'{"alg":"none"}') + '.' + body + '.' + signature
        invalid = [none, head + '.' + office._encode(b'{"hello":2}') + '.' + signature,
                   office.sign({'exp': time.time() - 1}, 'secret'), 'x' * 65537, 'a.b', 'a.b.c',
                   office.sign([], 'secret'), office.sign({'exp': 'tomorrow'}, 'secret'),
                   '!.e30.invalid']
        for value in invalid:
            with self.subTest(token=value[:40]), self.assertRaises(ValueError):
                office.verify(value, 'secret')
        malformed = office._encode(b'{')
        content = head + '.' + malformed
        invalid_json = content + '.' + office._encode(hmac.digest(b'secret', content.encode(), 'sha256'))
        with self.assertRaises(ValueError):
            office.verify(invalid_json, 'secret')
        for payload in (b'{"exp":NaN}', b'[' * 1100 + b']' * 1100):
            content = head + '.' + office._encode(payload)
            token = content + '.' + office._encode(hmac.digest(b'secret', content.encode(), 'sha256'))
            with self.assertRaises(ValueError):
                office.verify(token, 'secret')

    def test_capability(self):
        payload = {'p': '~/Report.docx', 'k': 'key', 'm': 1, 's': 2, 'e': time.time() + 60}
        token = office.capability(payload, 'secret')
        self.assertEqual(office.verify_capability(token, 'secret'), payload)
        invalid = [token + 'x', office.capability(dict(payload, e=1), 'secret'),
                   office.capability(dict(payload, m='1'), 'secret'), 'a.b.c']
        for value in invalid:
            with self.assertRaises(ValueError):
                office.verify_capability(value, 'secret')

    def test_open_config_and_key(self):
        self.enable()
        result = self.document()
        self.assertEqual(set(result), {'editorPort', 'apiPath', 'config'})
        self.assertEqual(result['editorPort'], self.config['editor_port'])
        self.assertEqual(result['apiPath'], '/web-apps/apps/api/documents/api.js')
        editor = result['config']
        claims = office.verify(editor.pop('token'), self.config['secret'])
        self.assertEqual(claims, editor)
        self.assertEqual(editor['documentType'], 'word')
        self.assertEqual(editor['document']['title'], 'Report.docx')
        self.assertEqual(editor['editorConfig']['user'], {'id': 'tether', 'name': 'Owner'})
        self.assertFalse(editor['editorConfig']['customization']['macros'])
        self.assertFalse(editor['editorConfig']['customization']['plugins'])
        key = 't' + hashlib.sha256(f'{self.path}|{self.path.stat().st_mtime_ns}|8'.encode()).hexdigest()[:32]
        self.assertEqual(editor['document']['key'], key)
        old = self.path.stat().st_mtime_ns
        os.utime(self.path, ns=(old, old + 1_000_000))
        self.assertNotEqual(self.document()['config']['document']['key'], key)
        for suffix, kind in [('.xlsx', 'cell'), ('.pptx', 'slide')]:
            (self.root / ('test' + suffix)).write_bytes(b'x')
            result = office.open_document('~/test' + suffix, '', True, 'fr-CA')['config']
            self.assertEqual((result['documentType'], result['type']), (kind, 'mobile'))
            self.assertEqual(result['editorConfig']['lang'], 'fr-CA')
            self.assertEqual(result['editorConfig']['user']['name'], 'Tether')
        self.assertEqual(office.open_document('~/Report.docx', lang='EN')['config']['editorConfig']['lang'], 'en')
        themes = [office.open_document('~/Report.docx', theme=theme)['config']['editorConfig']['customization']['uiTheme']
                  for theme in ('light', 'dark', 'other')]
        self.assertEqual(themes, ['default-light', 'default-dark', 'default-dark'])

    def test_open_refuses_unsafe_files(self):
        self.enable()
        (self.root / 'passwords.xlsx').touch()
        (self.root / '.ssh').mkdir()
        (self.root / '.ssh' / 'x.docx').touch()
        (self.root / 'link.docx').symlink_to(self.path)
        (self.root / 'Share').mkdir()
        (self.root / 'Share' / 'x.docx').touch()
        (self.root / 'plain.txt').touch()
        for text in ['~/passwords.xlsx', '~/.ssh/x.docx', '~/link.docx', str(self.base / 'outside.docx'),
                     '~/plain.txt', '~/missing.docx', '~/Share/x.docx', '~', '~/.ssh']:
            with self.subTest(path=text), self.assertRaises(files.FilesError):
                office.open_document(text)
        with mock.patch.object(office, 'LIMIT', 2), self.assertRaises(files.FilesError) as error:
            office.open_document('~/Report.docx')
        self.assertEqual(error.exception.status, 413)

    def test_file_route_streams_and_authenticates(self):
        self.ready()
        self.path.write_bytes(b'x' * (office.CHUNK + 1))
        self.document()
        route = '/office/file/' + self.cap_token
        jwt = {'Authorization': 'Bearer ' + office.sign({'url': route}, self.config['secret'])}
        self.assertEqual(self.request(self.listener, 'GET', route, headers=jwt), (200, self.path.read_bytes()))
        self.assertEqual(self.request(self.listener, 'GET', route)[0], 403)
        self.assertEqual(self.request(self.listener, 'GET', route, headers={'Authorization': 'Bearer bad'})[0], 403)
        self.assertEqual(self.request(self.listener, 'GET', '/office/file/bad', headers=jwt)[0], 403)
        self.path.write_bytes(b'changed')
        self.assertEqual(self.request(self.listener, 'GET', route, headers=jwt)[0], 409)
        self.assertEqual(self.request(self.listener, 'GET', '/elsewhere')[0], 404)
        self.assertEqual(self.request(self.listener, 'POST', '/elsewhere')[0], 404)
        self.assertEqual(self.request(self.listener, 'TRACE', '/elsewhere'), (404, b''))

    def test_callback_body_and_header_tokens_save_twice(self):
        self.ready()
        self.assertEqual(self.callback(), (200, {'error': 0}))
        self.assertEqual(self.path.read_bytes(), b'edited')
        self.upstream.data = b'force saved'
        self.assertEqual(self.callback(6, header=True), (200, {'error': 0}))
        self.assertEqual(self.path.read_bytes(), b'force saved')
        self.assertEqual(list(self.root.iterdir()), [self.path])
        self.assertEqual(self.upstream.requests[0][1], '/cache/files/result?download=1')
        jwt = {'Authorization': 'Bearer ' + office.sign({}, self.config['secret'])}
        self.assertEqual(self.request(self.listener, 'GET', '/office/file/' + self.cap_token, headers=jwt), (200, b'force saved'))

    def test_callback_non_saving_statuses(self):
        self.ready()
        for status in (1, 4, 99, 3, 7):
            with self.subTest(status=status):
                self.assertEqual(self.callback(status), (200, {'error': 0}))
        self.assertEqual(self.path.read_bytes(), b'original')
        self.assertEqual(self.upstream.requests, [])

    def test_callback_auth_and_size_failures(self):
        self.ready()
        self.assertEqual(self.callback(key='wrong'), (403, None))
        route = '/office/callback/' + self.cap_token
        for body in [b'{}', b'[]', b'{', b'{"token":"bad"}']:
            self.assertEqual(self.request(self.listener, 'POST', route, body)[0], 403)
        self.assertEqual(self.request(self.listener, 'POST', route, b'{}', {'Content-Length': str(office.CHUNK + 1)})[0], 413)
        self.assertEqual(self.request(self.listener, 'POST', '/office/callback/bad', b'{}')[0], 403)
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_callback_download_restrictions(self):
        self.ready()
        for url in ['http://elsewhere.invalid/private', 'http://elsewhere.invalid/cache/files/../../private',
                    'http://elsewhere.invalid/cache/files/%2e%2e/private',
                    'http://elsewhere.invalid/cache/files/./private',
                    'http://elsewhere.invalid/cache/files/%5cprivate',
                    'http://elsewhere.invalid/cache/files/\\private',
                    'http://elsewhere.invalid/office/cache/files/result']:
            self.assertEqual(self.callback(url=url), (200, {'error': 1}))
        self.assertEqual(self.upstream.requests, [])
        self.upstream.redirect = 'http://elsewhere.invalid/cache/files/redirect'
        self.assertEqual(self.callback(), (200, {'error': 1}))
        self.assertEqual(len(self.upstream.requests), 1)
        self.upstream.redirect = None
        with mock.patch.object(office, 'LIMIT', 9):
            self.upstream.data = b'x' * 10
            self.assertEqual(self.callback(), (200, {'error': 1}))
            self.upstream.no_length = True
            self.assertEqual(self.callback(), (200, {'error': 1}))
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_save_keeps_mode_and_is_atomic(self):
        self.enable()
        self.document()
        self.path.chmod(0o640)
        before = self.path.stat().st_ino
        office.save(self.cap, b'new')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertNotEqual(self.path.stat().st_ino, before)
        office.save(self.cap, b'new again')
        self.assertEqual(self.path.read_bytes(), b'new again')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_save_cleans_failed_temp(self):
        self.enable()
        self.document()
        with mock.patch.object(os, 'rename', side_effect=OSError('failed')), self.assertRaises(OSError):
            office.save(self.cap, b'new')
        self.assertEqual(self.path.read_bytes(), b'original')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_save_external_change_during_write_makes_copy(self):
        self.enable()
        self.document()
        self.assertIsNone(office.copy_for(self.cap['k']))
        write = files.write_all
        changed = False

        def external_edit(fd, data):
            nonlocal changed
            write(fd, data)
            if not changed:
                self.path.write_bytes(b'external during save')
                changed = True

        with mock.patch.object(files, 'write_all', side_effect=external_edit):
            office.save(self.cap, b'editor bytes')
        self.assertEqual(self.path.read_bytes(), b'external during save')
        self.assertEqual((self.root / 'Report (Tether edit).docx').read_bytes(), b'editor bytes')
        self.assertEqual(office.copy_for(self.cap['k']), '~/Report (Tether edit).docx')
        self.assertFalse(list(self.root.glob('.*.tether-*')))

    def test_copy_for_refuses_malformed_keys(self):
        for key in ('', 'bad', 't' + 'a' * 31, 't' + 'a' * 33, 't' + 'A' * 32,
                    't' + 'g' * 32, 't' + 'a' * 32 + '\n', None, []):
            with self.subTest(key=key), mock.patch.object(office, '_key_lock') as lock:
                self.assertIsNone(office.copy_for(key))
                lock.assert_not_called()

    def test_copy_route(self):
        key = 't' + 'a' * 32
        copy = '~/Report (Tether edit).docx'
        for enabled, denied, result in ((False, False, None), (True, False, copy),
                                        (True, False, None), (True, True, None)):
            with self.subTest(enabled=enabled, denied=denied, result=result):
                handler = object.__new__(server.Handler)
                handler.path = '/mobile/api/office/copy?key=' + key
                handler.headers = {}
                handler.client_address = ('127.0.0.1', 1234)
                handler.respond = mock.Mock()
                with mock.patch.object(handler, 'deny', return_value=denied) as deny, \
                        mock.patch.object(office, 'enabled', return_value=enabled) as installed, \
                        mock.patch.object(office, 'copy_for', return_value=result) as lookup:
                    handler.do_GET()
                deny.assert_called_once_with()
                if denied:
                    installed.assert_not_called()
                    lookup.assert_not_called()
                    handler.respond.assert_not_called()
                elif not enabled:
                    lookup.assert_not_called()
                    handler.respond.assert_called_once_with(404, {
                        'error': 'The Office editor add-on is not installed.', 'code': 'office-off'})
                else:
                    lookup.assert_called_once_with(key)
                    handler.respond.assert_called_once_with(200, {'copy': result})

    def test_save_conflict_copy_is_reused(self):
        self.enable()
        self.document()
        self.path.write_bytes(b'external change')
        occupied = self.root / 'Report (Tether edit).docx'
        occupied.write_bytes(b'keep')
        office.save(self.cap, b'editor change')
        copy = self.root / 'Report (Tether edit) (2).docx'
        self.assertEqual(copy.read_bytes(), b'editor change')
        office.save(self.cap, b'editor again')
        self.assertEqual(copy.read_bytes(), b'editor again')
        self.assertEqual(occupied.read_bytes(), b'keep')
        self.assertEqual(self.path.read_bytes(), b'external change')
        copy.write_bytes(b'external copy edit')
        office.save(self.cap, b'editor third')
        self.assertEqual(copy.read_bytes(), b'external copy edit')
        self.assertEqual((self.root / 'Report (Tether edit) (3).docx').read_bytes(), b'editor third')

    def test_save_refuses_symlink_or_secret_swap(self):
        self.enable()
        self.document()
        target = self.base / 'target.docx'
        target.write_bytes(b'outside')
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(files.FilesError):
            office.save(self.cap, b'bad')
        self.assertEqual(target.read_bytes(), b'outside')
        self.path.unlink()
        self.path.write_bytes(b'external edit')
        office.save(self.cap, b'copy')
        copy = self.root / 'Report (Tether edit).docx'
        copy.unlink()
        copy.symlink_to(target)
        with self.assertRaises(files.FilesError):
            office.save(self.cap, b'bad')
        self.assertEqual(target.read_bytes(), b'outside')

    def test_listener_start_bind_failure_and_success(self):
        self.enable()
        with mock.patch.object(office, 'ThreadingHTTPServer', side_effect=OSError('busy')):
            with self.assertLogs(office.LOG, level='WARNING'):
                self.assertIsNone(office.start_listener())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.config['listen_port'] = sock.getsockname()[1]
        office.write_config(self.config)
        http = office.start_listener()
        self.assertIsNotNone(http)
        thread = next(thread for thread in threading.enumerate() if thread.name == 'office-listener')
        try:
            self.assertEqual(self.request(http, 'GET', '/unknown'), (404, b''))
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_proxy_head(self):
        headers = {'Host': 'tether.test:7697', 'Cookie': 'private', 'X-Forwarded-Host': 'spoof',
                   'X-Forwarded-Prefix': 'spoof', 'X-Forwarded-Proto': 'https',
                   'Tailscale-User-Login': 'private', 'Connection': 'keep-alive', 'Content-Length': '4'}
        lines = office.office_upstream_head('POST', '/path?x=1', headers, '127.0.0.1')
        self.assertEqual(lines[0], 'POST /path?x=1 HTTP/1.1')
        head = '\n'.join(lines)
        self.assertNotIn('private', head)
        self.assertNotIn('spoof', head)
        self.assertIn('X-Forwarded-Host: tether.test:7697', lines)
        self.assertEqual(office.office_upstream_head('GET', '/office/path', headers, '127.0.0.1')[0],
                         'GET /office/path HTTP/1.1')
        self.assertIn('X-Forwarded-Proto: https', lines)
        self.assertIn('Connection: close', lines)
        for ip in ('192.0.2.1', 'invalid'):
            self.assertIn('X-Forwarded-Proto: http', office.office_upstream_head('GET', '/', headers, ip))
        headers.update(Connection='Upgrade', Upgrade='websocket')
        self.assertIn('Connection: Upgrade', office.office_upstream_head('GET', '/ws', headers, '::1'))

    def test_cli_install_and_remove(self):
        def docker(argv, **kwargs):
            if argv[1] == 'network':
                return subprocess.CompletedProcess(argv, 0, '127.0.0.1\n', '')
            return subprocess.CompletedProcess(argv, 0, 'running\n', '')

        with mock.patch.object(subprocess, 'run', side_effect=docker) as run, \
                mock.patch.object(office, '_healthy', return_value=True), \
                mock.patch.object(office, '_free_port', side_effect=[17695, 17696, 17697]) as free_port, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(office.main(['install']), 0)
            config = json.loads(self.config_path.read_bytes())
            self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config['url'], 'http://127.0.0.1:17695')
            self.assertEqual(config['editor_port'], 17697)
            self.assertEqual(free_port.call_args_list, [mock.call('127.0.0.1', 7695, False),
                                                       mock.call('127.0.0.1', 17696, False),
                                                       mock.call('127.0.0.1', 7697, False)])
            self.assertEqual(output.getvalue().splitlines()[-1],
                             'If you reach Tether through tailscale serve, run: '
                             'tailscale serve --bg --https=17697 http://127.0.0.1:17697')
            call = next(call for call in run.call_args_list if call.args[0][1] == 'run')
            argv = call.args[0]
            self.assertIn('JWT_SECRET', argv)
            self.assertNotIn(config['secret'], ' '.join(argv))
            self.assertEqual(call.kwargs['env']['JWT_SECRET'], config['secret'])
            for part in ['--restart', 'unless-stopped', '127.0.0.1:17695:80', 'ALLOW_PRIVATE_IP_ADDRESS=true', 'PLUGINS_ENABLED=false']:
                self.assertIn(part, argv)
            for call in run.call_args_list:
                self.assertIsInstance(call.args[0], list)
                self.assertIn('timeout', call.kwargs)
                self.assertNotIn('shell', call.kwargs)
            with mock.patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(office.main(['install']), 1)
            self.assertEqual(office.main(['status']), 0)
            self.assertEqual(office.main(['remove']), 0)
            self.assertFalse(self.config_path.exists())
            self.assertIsNone(office.load())

    def test_cli_docker_unavailable_and_missing_container(self):
        with mock.patch.object(subprocess, 'run', side_effect=FileNotFoundError), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(office.main(['install']), 1)
            self.assertIn('Docker is not ready', error.getvalue())
        self.enable()
        missing = subprocess.CalledProcessError(1, ['docker'], stderr='No such container: tether-office')
        with mock.patch.object(subprocess, 'run', side_effect=missing), mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(office.main(['remove']), 0)
            self.assertFalse(self.config_path.exists())

    def test_cli_timeout_and_macos(self):
        result = subprocess.CompletedProcess(['docker'], 0, '127.0.0.1\n', '')
        with mock.patch.object(subprocess, 'run', return_value=result) as run, \
                mock.patch.object(office, '_free_port', side_effect=[17695, 17696, 17697]), \
                mock.patch.object(office, '_healthy', return_value=False), \
                own_time(office, monotonic=mock.Mock(side_effect=[0, 181])), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(office.main(['install']), 1)
            self.assertIn('180 seconds', error.getvalue())
            self.assertFalse(self.config_path.exists())
        with mock.patch.object(subprocess, 'run', return_value=result) as run, \
                mock.patch.object(office, '_free_port', side_effect=[17695, 17696, 17697]), \
                mock.patch.object(office, '_healthy', return_value=True), \
                mock.patch.object(office.sys, 'platform', 'darwin'), \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(office.main(['install']), 0)
            self.assertEqual(office.load()['listen_host'], '127.0.0.1')
            self.assertFalse(any(call.args[0][1] == 'network' for call in run.call_args_list))

    def test_cli_editor_port_validation(self):
        for value in ('0', '-1', '65536'):
            with self.subTest(port=value), mock.patch.object(office, '_docker') as docker, \
                    mock.patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(office.main(['install', '--editor-port', value]), 1)
                docker.assert_not_called()
        for port in (17695, 17696):
            with self.subTest(port=port), mock.patch.object(office.sys, 'platform', 'darwin'), \
                    mock.patch.object(office, '_docker') as docker, \
                    mock.patch.object(office, '_free_port', side_effect=[17695, 17696, port]) as free_port, \
                    mock.patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(office.main(['install', '--editor-port', str(port)]), 1)
                free_port.assert_called_with('127.0.0.1', port, True)
                self.assertEqual(docker.call_args_list, [mock.call('info')])
                self.assertFalse(self.config_path.exists())
        with mock.patch.object(office.sys, 'platform', 'darwin'), mock.patch.object(office, '_docker'), \
                mock.patch.object(office, '_free_port', side_effect=[17695, 17696, 18000]) as free_port, \
                mock.patch.object(office, '_healthy', return_value=True), \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(office.main(['install', '--editor-port', '18000']), 0)
            free_port.assert_called_with('127.0.0.1', 18000, True)
            self.assertEqual(office.load()['editor_port'], 18000)

    def test_free_port_skips_busy_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            with self.assertRaises(ValueError):
                office._free_port('127.0.0.1', port, exact=True)
            self.assertGreater(office._free_port('127.0.0.1', port), port)

    def test_office_and_terminal_websocket_byte_pipe(self):
        upstream = self.serve(UpgradeHandler)
        self.config['url'] = 'http://127.0.0.1:%d' % upstream.server_port
        self.enable()
        http = self.serve(server.Handler)
        editor = self.serve(server.OfficeOriginHandler)
        with mock.patch.object(auth, 'signed_in', return_value=True), \
                mock.patch.object(server, 'TTYD_PORT', upstream.server_port):
            for listener, route in ((editor, '/socket?x=1'), (http, '/terminal/live/socket?x=1')):
                with socket.create_connection(('127.0.0.1', listener.server_port), timeout=3) as connection:
                    head = f'GET {route} HTTP/1.1\r\nHost: tether.test\r\nOrigin: http://tether.test\r\nCookie: private\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n'
                    connection.sendall(head.encode())
                    response = b''
                    while b'\r\n\r\n' not in response:
                        data = connection.recv(4096)
                        self.assertTrue(data)
                        response += data
                    self.assertTrue(response.startswith(b'HTTP/1.0 101'))
                    connection.sendall(b'ping')
                    self.assertEqual(connection.recv(4), b'ping')
                path, headers = upstream.received
                self.assertEqual(path, route)
                self.assertNotIn('Cookie', headers)

    def test_server_host_open_and_proxy_routes(self):
        http = self.serve(server.Handler)
        patch = mock.patch.object(auth, 'signed_in', return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        headers = {'Host': 'tether.test', 'Origin': 'http://tether.test', 'X-Codex-Mobile': '1', 'Cookie': 'private'}
        code, data = self.request(http, 'GET', '/mobile/api/host', headers=headers)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(data)['office'])
        for method in ('GET', 'POST', 'PUT'):
            self.assertEqual(self.request(http, method, '/office/asset', headers=headers)[0], 404)
        body = json.dumps({'path': '~/Report.docx', 'mobile': True, 'lang': 'fr'})
        code, data = self.request(http, 'POST', '/mobile/api/office/open', body, headers)
        self.assertEqual((code, json.loads(data)['code']), (404, 'office-off'))
        self.enable(upstream=True)
        self.assertTrue(json.loads(self.request(http, 'GET', '/mobile/api/host', headers=headers)[1])['office'])
        code, data = self.request(http, 'POST', '/mobile/api/office/open', body, headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)['editorPort'], self.config['editor_port'])
        self.assertEqual(json.loads(data)['apiPath'], '/web-apps/apps/api/documents/api.js')
        self.assertEqual(json.loads(data)['config']['type'], 'mobile')
        bad = dict(headers, Origin='http://other.test')
        self.assertEqual(self.request(http, 'POST', '/mobile/api/office/open', body, bad)[0], 403)
        self.assertEqual(self.request(http, 'POST', '/mobile/api/office/open', body, {'Host': 'tether.test'})[0], 403)
        with mock.patch.object(server, 'socket', wraps=socket) as sockets:
            for method in ('GET', 'POST', 'PUT'):
                self.assertEqual(self.request(http, method, '/office/asset', headers=headers)[0], 404)
            sockets.create_connection.assert_not_called()
        self.assertEqual(self.upstream.requests, [])

    def test_editor_origin_proxy_routes(self):
        self.enable(upstream=True)
        http = self.serve(server.OfficeOriginHandler)
        patch = mock.patch.object(auth, 'signed_in', return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        headers = {'Host': 'tether.test:7697', 'Origin': 'http://tether.test:7697', 'Cookie': 'private'}
        bad = dict(headers, Origin='http://tether.test')
        with mock.patch.object(server, 'socket', wraps=socket) as sockets:
            self.assertEqual(self.request(http, 'GET', '/asset', headers=bad)[0], 403)
            sockets.create_connection.assert_not_called()
        for method in ('GET', 'POST', 'PUT'):
            code, data = self.request(http, method, '/asset?query=1', b'buffered body' if method != 'GET' else None, headers)
            self.assertEqual(code, 200)
            sent = self.upstream.requests[-1]
            self.assertEqual(sent[1], '/asset?query=1')
            self.assertNotIn('Cookie', sent[2])
            self.assertEqual(sent[2]['X-Forwarded-Host'], 'tether.test:7697')
            if method != 'GET':
                self.assertEqual(data, b'buffered body')
        for path in ('/', '/chat/', '/mobile/login', '/mobile/api/host', '/mobile/api/office/open', '/office/asset'):
            self.assertEqual(self.request(http, 'GET', path, headers=headers), (200, b'edited'))
            self.assertEqual(self.upstream.requests[-1][1], path)
        self.assertEqual(self.request(http, 'POST', '/chunks', [b'first', b'second'],
                                      dict(headers, **{'Transfer-Encoding': 'chunked'}), chunked=True), (200, b'firstsecond'))

    def test_editor_origin_auth_for_all_methods(self):
        self.enable(upstream=True)
        http = self.serve(server.OfficeOriginHandler)
        methods = ('GET', 'POST', 'PUT', 'HEAD', 'DELETE', 'OPTIONS', 'PATCH')
        with mock.patch.object(auth, 'signed_in', return_value=False) as cookie, \
                mock.patch.object(auth, 'tailscale_signed_in', return_value=False) as tailscale, \
                mock.patch.object(server, 'socket', wraps=socket) as sockets:
            for method in methods:
                with self.subTest(method=method):
                    code, data = self.request(http, method, '/mobile/login')
                    self.assertEqual(code, 401)
                    if method != 'HEAD':
                        self.assertEqual(json.loads(data), {'error': 'Sign in required.'})
            sockets.create_connection.assert_not_called()
            cookie.assert_called_with(None)
            tailscale.assert_called_with(None, '127.0.0.1')
        for cookie_auth, tailscale_auth in ((True, False), (False, True)):
            with mock.patch.object(auth, 'signed_in', return_value=cookie_auth) as cookie, \
                    mock.patch.object(auth, 'tailscale_signed_in', return_value=tailscale_auth) as tailscale, \
                    mock.patch.object(server.OfficeOriginHandler, 'proxy_office', autospec=True,
                                      side_effect=lambda handler: handler.respond(200, b'')) as proxy:
                for method in methods:
                    with self.subTest(method=method, cookie=cookie_auth):
                        self.assertEqual(self.request(http, method, '/mobile/api/host', headers={
                            'Cookie': 'session-cookie', 'Tailscale-User-Login': 'owner@test.invalid'}), (200, b''))
                self.assertEqual(proxy.call_count, len(methods))
                cookie.assert_called_with('session-cookie')
                if not cookie_auth:
                    tailscale.assert_called_with('owner@test.invalid', '127.0.0.1')

    def test_editor_origin_listener_start(self):
        with mock.patch.object(server, 'ThreadingHTTPServer') as bind:
            self.assertIsNone(server.start_office_origin('127.0.0.1'))
            bind.assert_not_called()
        self.enable()
        with mock.patch.object(server, 'ThreadingHTTPServer', side_effect=OSError('busy')) as bind, \
                self.assertLogs(server.__name__, level='WARNING'):
            self.assertIsNone(server.start_office_origin('127.0.0.1'))
            bind.assert_called_once_with(('127.0.0.1', self.config['editor_port']), server.OfficeOriginHandler)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.config['editor_port'] = sock.getsockname()[1]
        office.write_config(self.config)
        http = server.start_office_origin('127.0.0.1')
        self.assertIsNotNone(http)
        thread = next(thread for thread in threading.enumerate() if thread.name == 'office-origin-listener')
        try:
            self.assertTrue(thread.daemon)
            with mock.patch.object(auth, 'signed_in', return_value=False), \
                    mock.patch.object(auth, 'tailscale_signed_in', return_value=False):
                self.assertEqual(self.request(http, 'GET', '/')[0], 401)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
