import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import urlencode

import auth
import server


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.patches = [mock.patch.object(auth, 'MODE', 'token'), mock.patch.object(auth, 'TOKEN_FILE', Path(self.folder.name) / 'token'),
                        mock.patch.dict(auth._failures, clear=True), mock.patch.object(auth, 'TAILSCALE', True),
                        mock.patch.object(auth, 'tailscale_users', lambda: {'owner@example.com'})]
        for patch in self.patches:
            patch.start()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_address[1]

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        for patch in self.patches:
            patch.stop()
        self.folder.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port)
        connection.request(method, path, body=body, headers=dict({'Host': 'phone.test'}, **(headers or {})))
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def sign_in(self, token):
        return self.request('POST', '/mobile/login', urlencode({'token': token}),
                            {'Origin': 'http://phone.test', 'Content-Type': 'application/x-www-form-urlencoded'})

    def test_token_file_is_created_private_and_stable(self):
        first = auth.token()
        self.assertEqual(first, auth.token())
        self.assertEqual(auth.TOKEN_FILE.stat().st_mode & 0o777, 0o600)

    def test_pages_and_api_need_sign_in(self):
        page, _ = self.request('GET', '/mobile/chat/')
        self.assertEqual((page.status, page.getheader('Location')), (302, '/mobile/login'))
        for path in ('/', '/mobile/', '/chat'):  # the tmux-chat UI is the default page
            page, _ = self.request('GET', path)
            self.assertEqual((page.status, page.getheader('Location')), (301, '/chat/'), path)
        for path in ('/mobile/api/sessions', '/mobile/api/chat?session=%251', '/mobile/api/stream', '/mobile/app.js'):
            self.assertIn(self.request('GET', path)[0].status, (302, 401), path)
        post, _ = self.request('POST', '/mobile/api/message', json.dumps({'message': 'x'}),
                               {'Origin': 'http://phone.test', 'X-Codex-Mobile': '1', 'Content-Length': '15'})
        self.assertEqual(post.status, 401)
        self.assertEqual(self.request('DELETE', '/mobile/api/session', '{}', {'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'})[0].status, 401)

    def test_your_tailscale_login_signs_in_without_the_token(self):
        page, _ = self.request('GET', '/mobile/chat/', headers={'Tailscale-User-Login': 'Owner@example.com'})
        self.assertNotEqual(page.getheader('Location'), '/mobile/login')
        login, _ = self.request('GET', '/mobile/login', headers={'Tailscale-User-Login': 'owner@example.com'})
        self.assertEqual((login.status, login.getheader('Location')), (302, '/chat/'))

    def test_other_tailscale_logins_and_the_off_switch_still_need_the_token(self):
        page, _ = self.request('GET', '/mobile/chat/', headers={'Tailscale-User-Login': 'friend@example.com'})
        self.assertEqual((page.status, page.getheader('Location')), (302, '/mobile/login'))
        self.assertEqual(self.request('GET', '/mobile/login', headers={'Tailscale-User-Login': 'friend@example.com'})[0].status, 200)
        with mock.patch.object(auth, 'TAILSCALE', False):
            page, _ = self.request('GET', '/mobile/chat/', headers={'Tailscale-User-Login': 'owner@example.com'})
            self.assertEqual(page.getheader('Location'), '/mobile/login')
        self.assertFalse(auth.tailscale_signed_in('owner@example.com', '100.64.0.5'))  # only Serve on this machine may vouch

    def test_login_page_and_icons_are_public(self):
        self.assertEqual(self.request('GET', '/mobile/login')[0].status, 200)
        self.assertEqual(self.request('GET', '/mobile/icon-192.png')[0].status, 200)

    def test_right_token_sets_a_session_cookie(self):
        response, _ = self.sign_in(auth.token())
        cookie = response.getheader('Set-Cookie')
        self.assertEqual((response.status, response.getheader('Location')), (303, '/chat/'))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn(auth.token(), cookie)
        page, _ = self.request('GET', '/mobile/chat/', headers={'Cookie': cookie.split(';')[0]})
        self.assertEqual(page.status, 200)

    def test_wrong_token_and_forged_cookie_are_refused(self):
        self.assertEqual(self.sign_in('nope')[0].status, 401)
        self.assertEqual(self.request('GET', '/mobile/api/sessions', headers={'Cookie': auth.COOKIE + '=forged'})[0].status, 401)

    def test_client_id_uses_the_proxy_address_only_when_trusted_and_local(self):
        with mock.patch.object(auth, 'TRUST_FORWARDED', False):
            self.assertEqual(auth.client_id('127.0.0.1', '9.9.9.9'), '127.0.0.1')  # off by default
        with mock.patch.object(auth, 'TRUST_FORWARDED', True):
            self.assertEqual(auth.client_id('127.0.0.1', '1.1.1.1, 9.9.9.9'), '9.9.9.9')  # the proxy's own entry, not the client's
            self.assertEqual(auth.client_id('127.0.0.1', None), '127.0.0.1')
            self.assertEqual(auth.client_id('203.0.113.5', '9.9.9.9'), '203.0.113.5')  # a direct caller cannot pick its own identity

    def test_repeated_failures_are_blocked_even_for_the_right_token(self):
        for _ in range(auth.LIMIT):
            self.sign_in('nope')
        response, data = self.sign_in(auth.token())
        self.assertEqual(response.status, 401)
        self.assertIn(b'Too many', data)

    def test_login_from_another_site_is_refused(self):
        response, _ = self.request('POST', '/mobile/login', urlencode({'token': auth.token()}), {'Origin': 'https://evil.test'})
        self.assertEqual(response.status, 403)

    def test_origin_must_match_host_but_either_scheme_works(self):
        self.assertTrue(auth.same_origin('http://a:1', 'a:1'))
        self.assertTrue(auth.same_origin('https://a', 'a'))
        self.assertFalse(auth.same_origin('https://b', 'a'))
        self.assertFalse(auth.same_origin('', 'a'))
        self.assertFalse(auth.same_origin('null', 'a'))

    def test_proxy_mode_trusts_the_proxy_only_on_loopback(self):
        with mock.patch.object(auth, 'MODE', 'proxy'):
            self.assertTrue(auth.signed_in(None))
            auth.check_startup('127.0.0.1')
            with self.assertRaises(SystemExit):
                auth.check_startup('0.0.0.0')

    def test_non_ascii_cookie_and_token_are_refused_without_crashing(self):
        self.assertFalse(auth.signed_in(auth.COOKIE + '=caf\u00e9'))
        self.assertEqual(auth.attempt('client', 'caf\u00e9'), 'wrong')
        self.assertEqual(len(auth._failures['client']), 1)  # and it counts as a failed attempt
        page, _ = self.request('GET', '/mobile/chat/', headers={'Cookie': (auth.COOKIE + '=caf\u00e9').encode('latin-1')})
        self.assertEqual(page.status, 302)

    def test_login_with_a_non_numeric_content_length_is_a_bad_request(self):
        response, _ = self.request('POST', '/mobile/login', None, {'Origin': 'http://phone.test', 'Content-Length': 'abc'})
        self.assertEqual(response.status, 400)

    def test_live_terminal_is_proxied_only_after_sign_in(self):
        import socket
        upstream = socket.socket()
        upstream.bind(('127.0.0.1', 0))
        upstream.listen(1)
        seen = []

        def serve():
            connection, _ = upstream.accept()
            seen.append(connection.recv(4096).decode())
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi')
            connection.close()
        threading.Thread(target=serve, daemon=True).start()
        with mock.patch.object(server, 'TTYD_PORT', upstream.getsockname()[1]):
            self.assertEqual(self.request('GET', '/terminal/live/')[0].status, 302)
            self.assertEqual(seen, [])
            cookie = self.sign_in(auth.token())[0].getheader('Set-Cookie').split(';')[0]
            response, body = self.request('GET', '/terminal/live/?arg=%241', headers={'Cookie': cookie})
        upstream.close()
        self.assertEqual((response.status, body), (200, b'hi'))
        self.assertIn('GET /terminal/live/?arg=%241 HTTP/1.1', seen[0])
        self.assertNotIn(auth.COOKIE, seen[0])   # the sign-in cookie is not handed on to ttyd

    def live_listener(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.BrowserLiveOriginHandler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        return http.server_address[1]

    def live_request(self, port, method, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port)
        connection.request(method, path, headers=dict({'Host': 'phone.test:7698'}, **(headers or {})))
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def fake_container(self, answers):
        """A Unix socket that stands in for the container: it records each request and sends the next answer."""
        import socket
        import tempfile
        from types import SimpleNamespace
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = folder.name + '/web.sock'
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.bind(path)
        upstream.listen(4)
        self.addCleanup(upstream.close)
        seen = []

        def serve():
            for answer in answers:
                connection, _ = upstream.accept()
                seen.append(connection.recv(4096).decode())
                connection.sendall(answer)
                if b'101 Switching' in answer:
                    seen.append(connection.recv(4096))
                    connection.sendall(b'\x81\x02hi')
                connection.close()
        threading.Thread(target=serve, daemon=True).start()
        return SimpleNamespace(web_auth=lambda: 'Basic c2VjcmV0', web_sock=path), seen

    def test_main_origin_never_serves_the_live_view(self):
        import browser
        container, seen = self.fake_container([b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok'])
        cookie = self.sign_in(auth.token())[0].getheader('Set-Cookie').split(';')[0]
        with mock.patch.object(browser.BROWSER, 'container', container):
            response, body = self.request('GET', '/browser/live/index.html', headers={'Cookie': cookie})
        self.assertNotEqual((response.status, body), (200, b'ok'))
        self.assertEqual(seen, [])

    def test_live_origin_serves_only_the_live_view_after_sign_in(self):
        import browser
        hostile = (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nSet-Cookie: tether=planted; Path=/\r\n'
                   b'set-cookie2: x=1\r\nClear-Site-Data: "cookies"\r\nX-Frame-Options: DENY\r\n'
                   b"Content-Security-Policy: frame-ancestors *\r\nAccess-Control-Allow-Origin: *\r\n"
                   b'X-Kept: yes\r\n Set-Cookie: folded=1\r\nNoColon\r\n\r\nok')
        container, seen = self.fake_container([hostile])
        port = self.live_listener()
        with mock.patch.object(browser.BROWSER, 'container', container):
            self.assertEqual(self.live_request(port, 'GET', '/browser/live/')[0].status, 401)
            cookie = self.sign_in(auth.token())[0].getheader('Set-Cookie').split(';')[0]
            for path in ('/chat/', '/mobile/api/sessions', '/mobile/login', '/terminal/live/', '/'):
                self.assertEqual(self.live_request(port, 'GET', path, {'Cookie': cookie})[0].status, 404, path)
            self.assertEqual(self.live_request(port, 'POST', '/mobile/api/message', {'Cookie': cookie})[0].status, 404)
            refused, _ = self.live_request(port, 'GET', '/browser/live/', {'Cookie': cookie, 'Origin': 'http://phone.test'})
            self.assertEqual(refused.status, 403)  # Tether's main origin is a different origin here
            self.assertEqual(seen, [])
            response, body = self.live_request(port, 'GET', '/browser/live/index.html', {
                'Cookie': cookie, 'Authorization': 'Basic bWluZQ==', 'Origin': 'http://phone.test:7698',
                'Tailscale-User-Login': 'owner@example.com'})
        self.assertEqual((response.status, body), (200, b'ok'))
        self.assertIn('GET /browser/live/index.html HTTP/1.1', seen[0])
        self.assertNotIn(auth.COOKIE, seen[0])  # Tether's cookie stays here
        self.assertNotIn('owner@example.com', seen[0])
        self.assertIn('Authorization: Basic c2VjcmV0', seen[0])  # only the container's own password goes on
        self.assertNotIn('bWluZQ==', seen[0])
        names = [name.lower() for name, _ in response.getheaders()]
        for dropped in ('set-cookie', 'set-cookie2', 'clear-site-data', 'x-frame-options', 'access-control-allow-origin'):
            self.assertNotIn(dropped, names)
        self.assertEqual(response.getheader('X-Kept'), 'yes')
        self.assertEqual(response.getheader('Content-Security-Policy'),
                         'frame-ancestors https://phone.test http://phone.test:%d https://phone.test:%d' % (server.MAIN_PORT, server.MAIN_PORT))
        self.assertNotIn('folded', str(response.getheaders()))

    def test_live_origin_filters_the_websocket_answer_then_pipes_frames(self):
        import browser
        import socket
        container, seen = self.fake_container([b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                                               b'Connection: Upgrade\r\nSet-Cookie: a=1\r\n\r\n'])
        port = self.live_listener()
        cookie = self.sign_in(auth.token())[0].getheader('Set-Cookie').split(';')[0]
        with mock.patch.object(browser.BROWSER, 'container', container):
            client = socket.create_connection(('127.0.0.1', port), timeout=5)
            client.sendall(('GET /browser/live/api/websockets HTTP/1.1\r\nHost: phone.test:7698\r\nCookie: %s\r\n'
                            'Connection: Upgrade\r\nUpgrade: websocket\r\nOrigin: http://phone.test:7698\r\n\r\n' % cookie).encode())
            data = b''
            while not data.endswith(b'hi'):
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b'\r\n\r\n' in data and not data.endswith(b'hi'):
                    client.sendall(b'\x81\x80mask')
            client.close()
        head = data.split(b'\r\n\r\n', 1)[0]
        self.assertTrue(head.startswith(b'HTTP/1.1 101'))
        self.assertNotIn(b'Set-Cookie', head)
        self.assertIn(b'Connection: Upgrade', seen[0].encode())
        self.assertTrue(data.endswith(b'\x81\x02hi'))
        self.assertEqual(seen[1], b'\x81\x80mask')

    def test_live_ancestors_allow_only_the_main_origin(self):
        self.assertEqual(server.live_ancestors('phone.example.test:7698'),
                         'https://phone.example.test http://phone.example.test:%d https://phone.example.test:%d' % ((server.MAIN_PORT,) * 2))
        for bad in ('', 'evil.test x', "a'b", '[::1]:7698'):
            self.assertEqual(server.live_ancestors(bad), "'none'", bad)

    def test_panel_browser_live_view_without_a_container_is_unavailable(self):
        import browser
        port = self.live_listener()
        cookie = self.sign_in(auth.token())[0].getheader('Set-Cookie').split(';')[0]
        with mock.patch.object(browser.BROWSER, 'container', None):
            reply, body = self.live_request(port, 'GET', '/browser/live/', {'Cookie': cookie})
            self.assertEqual(reply.status, 503)
            self.assertIn('text/html', reply.getheader('Content-Type'))  # the frame shows it and loads again
            self.assertIn(b'http-equiv="refresh"', body)

if __name__ == '__main__':
    unittest.main()
