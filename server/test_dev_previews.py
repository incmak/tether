"""Dev server previews: which ports Tether forwards, the token and cookie flow, and the proxy on real sockets."""
import http.client
import ipaddress
import json
import os
import socket
import sys
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import dev_previews
from dev_previews import DevPreviews, Refused

UID = 1000
PROC_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1435 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 5001 1 0000000000000000 100 0 0 10 0
   1: 00000000:0BB8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 5002 1 0000000000000000 100 0 0 10 0
   2: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 5003 1 0000000000000000 100 0 0 10 0
   3: 0500A8C0:2328 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 5004 1 0000000000000000 100 0 0 10 0
   4: 0100007F:1436 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1000        0 5005 1 0000000000000000 20 4 30 10 -1
   5: 0100007F:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 5006 1 0000000000000000 100 0 0 10 0
   6: 0100007F:2399 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 5007 1 0000000000000000 100 0 0 10 0
broken line
"""
PROC_TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:1436 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 6001 1 0000000000000000 100 0 0 10 0
   1: 00000000000000000000000000000000:1F40 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 6002 1 0000000000000000 100 0 0 10 0
   2: 0000000000000000FFFF00000100007F:1F41 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 6003 1 0000000000000000 100 0 0 10 0
"""


def fake_listeners():
    return dev_previews.parse_proc_net(PROC_TCP) + dev_previews.parse_proc_net(PROC_TCP6)


@unittest.skipUnless(sys.byteorder == 'little', 'the samples are from a little-endian machine')
class PortTests(unittest.TestCase):
    def setUp(self):
        self.previews = DevPreviews(ports=[7700, 7701], denied={7683}, listeners=fake_listeners, uid=UID, names=lambda *_: {})

    def test_pool_setting(self):
        self.assertEqual(dev_previews.parse_ports('7700-7709'), list(range(7700, 7710)))
        self.assertEqual(dev_previews.parse_ports(' 7705, 7700-7701 '), [7700, 7701, 7705])
        for off in ('', '0', 'off'):
            self.assertEqual(dev_previews.parse_ports(off), [])
        for bad in ('80-90', 'x', '7700-', '7709-7700', '2000-9000', '70000'):
            with self.assertRaises(ValueError, msg=bad):
                dev_previews.parse_ports(bad)
        self.assertEqual(dev_previews.pool_ports({}), list(range(7700, 7710)))
        self.assertEqual(dev_previews.pool_ports({'MOBILE_TMUX_DEV_PREVIEW_PORTS': '7800-7801'}), [7800, 7801])

    def test_proc_lines_become_listeners(self):
        found = {entry['port']: entry for entry in fake_listeners()}
        self.assertEqual(str(found[5173]['address']), '127.0.0.1')
        self.assertEqual(str(found[3000]['address']), '0.0.0.0')
        self.assertEqual(str(found[9000]['address']), '192.168.0.5')
        self.assertEqual(str(found[5174]['address']), '::1')  # the connected IPv4 socket on 5174 is not a listener
        self.assertEqual(str(found[8000]['address']), '::')
        self.assertEqual(found[8001]['address'].ipv4_mapped, ipaddress.ip_address('127.0.0.1'))
        self.assertEqual((found[8080]['uid'], found[5173]['uid'], found[5173]['inode']), (0, UID, 5001))
        self.assertEqual(len(found), 9)

    def test_lsof_lines_become_listeners(self):
        text = 'p311\ncnode\nf23\nn127.0.0.1:5173\nf24\nn[::1]:5173\np400\ncpython3.12\nf3\nn*:8000\nf4\nnbad\n'
        found = dev_previews.parse_lsof(text, UID)
        self.assertEqual([(str(entry['address']), entry['port'], entry['name']) for entry in found],
                         [('127.0.0.1', 5173, 'node'), ('::1', 5173, 'node'), ('0.0.0.0', 8000, 'python3.12')])
        self.assertTrue(all(entry['uid'] == UID for entry in found))

    def test_own_loopback_and_wildcard_listeners_are_forwarded(self):
        self.assertEqual(self.previews.target(5173), '127.0.0.1')
        self.assertEqual(self.previews.target(3000), '127.0.0.1')
        self.assertEqual(self.previews.target(5174), '::1')
        self.assertEqual(self.previews.target(8000), '::1')
        self.assertEqual(self.previews.target(8001), '127.0.0.1')

    def test_refused_ports(self):
        cases = {8080: 'denied',         # another user's listener
                 9000: 'denied',         # bound to a LAN address only
                 80: 'denied',           # a system port
                 9241: 'not-listening',
                 5175: 'not-listening',
                 0: 'bad-port', 70000: 'bad-port', '5173': 'bad-port', None: 'bad-port', True: 'bad-port', 5173.0: 'bad-port'}
        for port, code in cases.items():
            with self.assertRaises(Refused, msg=port) as caught:
                self.previews.target(port)
            self.assertEqual(caught.exception.code, code, port)

    def test_deny_list_wins_over_a_listener(self):
        # 9113 = 0x2399 listens for this user; once it is on the deny list it is refused, like the fixed ports.
        self.assertEqual(self.previews.target(9113), '127.0.0.1')
        listening = DevPreviews(ports=[7700], denied={9113}, listeners=fake_listeners, uid=UID)
        with self.assertRaises(Refused) as caught:
            listening.target(9113)
        self.assertEqual(caught.exception.code, 'denied')
        for port in (22, 3306, 5432, 6379, 9200, 9222, 9229, 11211, 27017, 7683, 7700, 7701):
            with self.assertRaises(Refused, msg=port) as caught:
                self.previews.target(port)
            self.assertEqual(caught.exception.code, 'denied', port)

    def test_servers_lists_only_what_can_be_forwarded(self):
        previews = DevPreviews(ports=[7700], listeners=fake_listeners, uid=UID,
                               names=lambda inodes, uid: {5001: {'name': 'node', 'folder': 'shop'}})
        found = previews.servers()
        self.assertEqual([entry['port'] for entry in found], [3000, 5173, 5174, 8000, 8001, 9113])
        self.assertEqual(found[1], {'port': 5173, 'name': 'node', 'folder': 'shop'})

    def test_process_names_come_from_proc(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            inode = os.fstat(listener.fileno()).st_ino
            found = dev_previews.process_names([inode], os.getuid())
        if sys.platform.startswith('linux'):
            self.assertEqual(found[inode]['folder'], os.path.basename(os.getcwd()))
            self.assertTrue(found[inode]['name'])


@unittest.skipUnless(sys.byteorder == 'little', 'the samples are from a little-endian machine')
class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.listening = fake_listeners()
        self.previews = DevPreviews(ports=[7700, 7701], listeners=lambda: self.listening, uid=UID, clock=lambda: self.now[0])

    def token(self, link):
        return link.split('token=')[1].split('&')[0]

    def cookie(self, lease):
        return ['a=1; %s%d=%s' % (dev_previews.COOKIE_PREFIX, lease.pool_port, lease.secret)]

    def test_a_dev_port_gets_a_pool_port_and_two_links(self):
        link = self.previews.open(5173, '/app?x=1#top')
        self.assertEqual((link['port'], link['dev_port']), (7700, 5173))
        self.assertTrue(link['frame'].startswith('/__tether_preview__/open?token='))
        self.assertTrue(link['frame'].endswith('&next=%2Fapp%3Fx%3D1%23top'))
        self.assertNotEqual(self.token(link['frame']), self.token(link['browser']))
        again = self.previews.open(5173)
        self.assertEqual(again['port'], 7700)  # the same dev server keeps its origin (its storage and cookies)
        self.assertEqual(self.previews.open(3000)['port'], 7701)

    def test_a_token_works_once(self):
        link = self.previews.open(5173)
        token = self.token(link['frame'])
        self.assertIsNone(self.previews.redeem(7701, token))  # another port's token
        self.assertIsNone(self.previews.redeem(7700, ''))
        self.assertIsNone(self.previews.redeem(7700, token + 'x'))
        lease = self.previews.redeem(7700, token)
        self.assertEqual(lease.dev_port, 5173)
        self.assertIsNone(self.previews.redeem(7700, token))

    def test_the_frame_token_is_short_lived_and_the_browser_token_is_not(self):
        link = self.previews.open(5173)
        self.now[0] += dev_previews.TICKET_LIFE + 1
        self.assertIsNone(self.previews.redeem(7700, self.token(link['frame'])))
        self.assertIsNotNone(self.previews.redeem(7700, self.token(link['browser'])))

    def test_the_cookie_is_the_key_for_its_own_port_only(self):
        self.previews.open(5173)
        self.previews.open(3000)
        first, second = self.previews.leases[7700], self.previews.leases[7701]
        self.assertIs(self.previews.authorize(7700, self.cookie(first)), first)
        self.assertIsNone(self.previews.authorize(7701, self.cookie(first)))
        self.assertIsNone(self.previews.authorize(7700, ['%s7700=%s' % (dev_previews.COOKIE_PREFIX, second.secret)]))
        self.assertIsNone(self.previews.authorize(7700, None))
        self.assertIsNone(self.previews.authorize(7700, ['%s=%s' % (auth.COOKIE, auth.session_value('secret'))]))

    def test_a_lease_ends_after_idle_time(self):
        self.previews.open(5173)
        lease = self.previews.leases[7700]
        self.now[0] += dev_previews.IDLE - 10
        self.assertIs(self.previews.authorize(7700, self.cookie(lease)), lease)  # a request keeps it alive
        self.now[0] += dev_previews.IDLE - 10
        self.assertIs(self.previews.authorize(7700, self.cookie(lease)), lease)
        self.now[0] += dev_previews.IDLE + 1
        self.assertIsNone(self.previews.authorize(7700, self.cookie(lease)))
        self.assertEqual(self.previews.leases, {})

    def test_a_lease_ends_some_time_after_its_dev_server_stops(self):
        self.previews.open(5173)
        lease = self.previews.leases[7700]
        self.previews.mark(lease, False)
        self.now[0] += dev_previews.GONE - 5
        self.assertIs(self.previews.authorize(7700, self.cookie(lease)), lease)
        self.previews.mark(lease, True)  # it came back: a restart keeps the origin
        self.now[0] += dev_previews.GONE
        self.assertIs(self.previews.authorize(7700, self.cookie(lease)), lease)
        self.previews.mark(lease, False)
        self.previews.mark(lease, False)  # the first time counts
        self.now[0] += dev_previews.GONE + 1
        self.assertIsNone(self.previews.authorize(7700, self.cookie(lease)))

    def test_a_full_pool_reuses_dead_then_idle_leases(self):
        self.previews.open(5173)
        self.previews.open(3000)
        with self.assertRaises(Refused) as caught:
            self.previews.open(5174)
        self.assertEqual(caught.exception.code, 'pool-full')
        self.assertIn('2 preview ports', str(caught.exception))
        # The server on 3000 stops: its pool port is free for the next dev server at once.
        self.listening = [entry for entry in self.listening if entry['port'] != 3000]
        self.assertEqual(self.previews.open(5174)['port'], 7701)
        self.assertEqual(self.previews.leases[7701].dev_port, 5174)
        # Both servers run; the lease that is idle the longest goes once it is idle long enough.
        self.now[0] += 60
        self.previews.authorize(7701, self.cookie(self.previews.leases[7701]))
        with self.assertRaises(Refused):
            self.previews.open(8000)
        self.now[0] += dev_previews.REUSE_AFTER
        old_secret = self.previews.leases[7700].secret
        self.assertEqual(self.previews.open(8000)['port'], 7700)
        self.assertNotEqual(self.previews.leases[7700].secret, old_secret)  # the old cookie does not open the new server

    def test_refusals(self):
        with self.assertRaises(Refused) as caught:
            DevPreviews(ports=[], listeners=fake_listeners, uid=UID).open(5173)
        self.assertEqual(caught.exception.code, 'off')
        for port, code in ((8080, 'denied'), (9241, 'not-listening'), (7700, 'denied'), ('x', 'bad-port')):
            with self.assertRaises(Refused) as caught:
                self.previews.open(port)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.previews.leases, {})

    def test_state_lists_leases_with_hints(self):
        self.previews.open(5173)
        self.previews.leases[7700].hint = 'next'
        self.assertEqual(self.previews.state(), [{'port': 7700, 'dev_port': 5173, 'hint': 'next'}])


class HeaderTests(unittest.TestCase):
    def test_next_path_stays_on_the_dev_server(self):
        for path in ('/', '/a/b?c=d#e', '/%20x'):
            self.assertEqual(dev_previews.safe_path(path), path)
        for path in ('', 'https://evil.example/', '//evil.example/', '/\\evil.example', '/a b', '/a\nb', None, 5, '/' + 'a' * 3000):
            self.assertEqual(dev_previews.safe_path(path), '/', path)

    def test_tether_cookies_do_not_reach_the_dev_server(self):
        session = '%s=%s' % (auth.COOKIE, 'abc')
        self.assertEqual(dev_previews.dev_cookies(['sid=1; %s; theme=dark; tether_preview_7700=s; Tether_Preview_7701=t' % session]),
                         'sid=1; theme=dark')
        self.assertEqual(dev_previews.dev_cookies([session, 'a=b=c']), 'a=b=c')
        self.assertIsNone(dev_previews.dev_cookies([session.upper()]))
        self.assertIsNone(dev_previews.dev_cookies(None))

    def test_the_dev_server_cannot_set_tether_cookies(self):
        self.assertIsNone(dev_previews.set_cookie('%s=x; Path=/' % auth.COOKIE))
        self.assertIsNone(dev_previews.set_cookie(' %s =x' % auth.COOKIE.upper()))
        self.assertIsNone(dev_previews.set_cookie('tether_preview_7700=x; HttpOnly'))
        self.assertEqual(dev_previews.set_cookie('sid=1; Path=/; Domain=localhost; HttpOnly'), 'sid=1; Path=/; HttpOnly')
        self.assertEqual(dev_previews.set_cookie('sid=1; domain = .localhost;SameSite=Lax'), 'sid=1;SameSite=Lax')
        self.assertEqual(dev_previews.set_cookie('v=Domain=x'), 'v=Domain=x')

    def test_origin_and_location_translate_between_the_two_origins(self):
        public, local = 'https://box.example:7700', 'http://localhost:5173'
        self.assertEqual(dev_previews.to_local(public, public, local), local)
        self.assertEqual(dev_previews.to_local(public + '/a?b', public, local), local + '/a?b')
        for other in ('https://box.example:77001', 'https://box.example', 'https://evil.example', 'null'):
            self.assertEqual(dev_previews.to_local(other, public, local), other)
        for name in ('localhost', '127.0.0.1', '0.0.0.0', '[::1]', 'LOCALHOST'):
            self.assertEqual(dev_previews.to_public('http://%s:5173/login?next=/' % name, public, 5173), public + '/login?next=/')
        self.assertEqual(dev_previews.to_public('https://localhost:5173', public, 5173), public)
        for other in ('/login', 'http://localhost:51730/', 'http://localhost:5174/', 'http://localhost/', 'https://example.com/', 'http://localhost:5173.evil.example/'):
            self.assertEqual(dev_previews.to_public(other, public, 5173), other)

    def test_malformed_response_heads_are_refused(self):
        self.assertEqual(dev_previews.response_code(b'HTTP/1.1 200 OK\r\nA: b\r\nX-Text: caf\xc3\xa9\tx'), 200)
        self.assertEqual(dev_previews.response_code(b'HTTP/1.0 404'), 404)
        for head in (b'HTTP/1.1 200 OK\r\nA: b\nSet-Cookie: x=1', b'HTTP/1.1 200 OK\r\nA: b\r\n folded', b'HTTP/1.1 200 OK\r\nBad Name: x',
                     b'HTTP/2 200', b'HTTP/1.1 20 OK', b'HTTP/1.1 200 OK\r\nA: b\rSet-Cookie: x=1', b'HTTP/1.1 200 OK\r\nno colon', b''):
            self.assertIsNone(dev_previews.response_code(head), head)

    def test_only_tether_may_frame_the_page(self):
        self.assertEqual(dev_previews.frame_ancestors('box.example:7700', 7683),
                         "'self' https://box.example http://box.example:7683 https://box.example:7683")
        self.assertEqual(dev_previews.frame_ancestors('bad host;x', 7683), "'self'")

    def test_serve_commands(self):
        self.assertEqual(dev_previews.serve_commands([7700]), ['tailscale serve --bg --https=7700 http://127.0.0.1:7700'])


class Upstream(BaseHTTPRequestHandler):
    """A tiny dev server. It records what it gets and answers by path."""
    protocol_version = 'HTTP/1.1'
    seen = []
    release = threading.Event()

    def log_message(self, *_args):
        pass

    def reply(self, status, body=b'', headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        length = self.headers.get('Content-Length')
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            body = b''
            while True:
                size = int(self.rfile.readline(), 16)
                body += self.rfile.read(size)
                self.rfile.readline()
                if size == 0:
                    break
        else:
            body = self.rfile.read(int(length)) if length else b''
        Upstream.seen.append({'method': self.command, 'path': self.path, 'headers': [(name.lower(), value) for name, value in self.headers.items()], 'body': body})
        port = self.server.server_port
        if self.path == '/redirect':
            self.reply(302, headers=[('Location', 'http://localhost:%d/login?from=/redirect' % port)])
        elif self.path == '/away':
            self.reply(302, headers=[('Location', 'https://example.com/x')])
        elif self.path == '/cookies':
            self.reply(200, b'ok', [('Set-Cookie', 'sid=1; Path=/; Domain=localhost; HttpOnly'), ('Set-Cookie', '%s=stolen; Path=/' % auth.COOKIE),
                                    ('Set-Cookie', 'tether_preview_1=x'), ('Clear-Site-Data', '"cookies"'), ('X-Frame-Options', 'DENY'),
                                    ('Access-Control-Allow-Origin', 'http://localhost:%d' % port)])
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(b'5\r\nfirst\r\n')
            self.wfile.flush()
            Upstream.release.wait(10)
            self.wfile.write(b'6\r\nsecond\r\n0\r\n\r\n')
            self.close_connection = True
        elif self.path == '/ws':
            if self.headers.get('Upgrade', '').lower() != 'websocket':
                self.reply(400, b'no upgrade')
                return
            self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSet-Cookie: %s=x\r\n\r\n' % auth.COOKIE.encode())
            while True:
                data = self.connection.recv(4096)
                if not data:
                    break
                self.connection.sendall(b'echo:' + data)
            self.close_connection = True
        elif self.path == '/early':
            self.connection.sendall(b'HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\nSet-Cookie: %s=x\r\n\r\n' % auth.COOKIE.encode())
            self.reply(200, b'late')
        elif self.path == '/malformed':
            self.connection.sendall(b'HTTP/1.1 200 OK\r\nA: b\nSet-Cookie: %s=x\r\nContent-Length: 2\r\n\r\nhi' % auth.COOKIE.encode())
            self.close_connection = True
        elif self.path == '/keepalive':
            # A dev server that ignores "Connection: close": the proxy still ends the answer at its length.
            self.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nheld')
            Upstream.release.wait(10)
            self.close_connection = True
        elif self.path == '/_next/static/chunk.js':
            self.reply(403, b'Unauthorized')
        else:
            self.reply(200, json.dumps({'path': self.path}).encode(), [('Content-Type', 'application/json')])

    do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = do_GET


@unittest.skipUnless(sys.platform.startswith('linux'), 'reads the real listener table from /proc')
class ProxyTests(unittest.TestCase):
    """The proxy between a real client socket and a real dev server."""

    def setUp(self):
        Upstream.seen = []
        Upstream.release = threading.Event()
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.dev_port = self.upstream.server_port
        self.previews = DevPreviews()
        self.logged = []
        self.servers = dev_previews.start('127.0.0.1', 7683, previews=self.previews, ports=[0], log=self.logged.append)
        self.port = self.servers[0].server_port
        self.host = 'box.example:%d' % self.port
        self.cookie = None

    def tearDown(self):
        Upstream.release.set()
        for server in self.servers + [self.upstream]:
            server.shutdown()
            server.server_close()

    def request(self, method, path, headers=None, body=None, cookie=True):
        headers = dict({'Host': self.host}, **(headers or {}))
        if cookie and self.cookie and 'Cookie' not in headers:
            headers['Cookie'] = self.cookie
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def sign_in(self, path='/', secure=False):
        link = self.previews.open(self.dev_port, path)
        response, _ = self.request('GET', link['frame'], {'X-Forwarded-Proto': 'https'} if secure else None, cookie=False)
        self.assertEqual(response.status, 302)
        self.cookie = response.getheader('Set-Cookie').split(';')[0]
        return link, response

    def last(self):
        entry = Upstream.seen[-1]
        return entry, dict(entry['headers'])

    def test_start_reports_the_pool_and_the_serve_command(self):
        self.assertEqual(self.previews.ports, [self.port])
        self.assertIn(self.port, self.previews.denied)
        self.assertIn(7683, self.previews.denied)
        self.assertIn('tailscale serve --bg --https=%d http://127.0.0.1:%d' % (self.port, self.port), self.logged[0])

    def test_no_cookie_and_no_token_get_401(self):
        self.previews.open(self.dev_port)
        for method, path, headers in (('GET', '/', None), ('POST', '/', None), ('GET', dev_previews.OPEN_PATH + '?token=wrong&next=/', None),
                                      ('GET', '/', {'Cookie': 'tether_preview_%d=guess' % self.port}),
                                      ('GET', '/', {'Tailscale-User-Login': 'owner@example.com'}),
                                      ('GET', '/ws', {'Connection': 'Upgrade', 'Upgrade': 'websocket'})):
            response, data = self.request(method, path, headers, cookie=False)
            self.assertEqual(response.status, 401, (method, path))
            self.assertIn(b'This preview has ended', data)
            self.assertIn(b'tetherPreview:"expired"', data)
            self.assertIn("frame-ancestors 'self' https://box.example", response.getheader('Content-Security-Policy'))
        self.assertEqual(Upstream.seen, [])  # nothing reached the dev server

    def test_a_pool_port_without_a_lease_serves_nothing(self):
        response, _ = self.request('GET', '/', cookie=False)
        self.assertEqual(response.status, 401)

    def test_the_token_becomes_a_cookie_and_a_clean_url(self):
        link, response = self.sign_in('/app?tab=1#top', secure=True)
        self.assertEqual(response.getheader('Location'), '/app?tab=1#top')
        cookie = response.getheader('Set-Cookie')
        self.assertTrue(cookie.startswith('tether_preview_%d=' % self.port))
        for part in ('; HttpOnly', '; SameSite=Strict', '; Secure', '; Path=/'):
            self.assertIn(part, cookie)
        self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
        # The used link: 401 without the cookie, and the page again with it (Reload, a second tap).
        self.assertEqual(self.request('GET', link['frame'], cookie=False)[0].status, 401)
        self.assertEqual(self.request('GET', link['frame'])[0].status, 302)
        self.assertNotIn('Secure', self.request('GET', link['browser'], cookie=False)[0].getheader('Set-Cookie'))
        response, data = self.request('GET', '/app?tab=1')
        self.assertEqual((response.status, json.loads(data)), (200, {'path': '/app?tab=1'}))
        self.assertEqual([entry['path'] for entry in Upstream.seen], ['/app?tab=1'])  # the token link never reaches the dev server

    def test_the_redirect_after_sign_in_stays_on_this_origin(self):
        link = self.previews.open(self.dev_port)
        token = link['frame'].split('token=')[1].split('&')[0]
        response, _ = self.request('GET', '%s?token=%s&next=%s' % (dev_previews.OPEN_PATH, token, '%2F%2Fevil.example%2F'), cookie=False)
        self.assertEqual((response.status, response.getheader('Location')), (302, '/'))

    def test_the_dev_server_sees_a_localhost_request_without_tether_data(self):
        self.sign_in()
        public = 'https://' + self.host  # tailscale serve sends X-Forwarded-Proto: https
        response, _ = self.request('POST', '/api/save?x=1', {
            'Cookie': '%s; sid=7; %s=%s; tether_preview_9=other' % (self.cookie, auth.COOKIE, auth.session_value('secret')),
            'Origin': public, 'Referer': public + '/page', 'X-Forwarded-For': '100.64.0.9', 'X-Forwarded-Host': self.host,
            'X-Forwarded-Proto': 'https', 'Tailscale-User-Login': 'owner@example.com', 'Tailscale-User-Name': 'Owner',
            'Forwarded': 'for=100.64.0.9', 'Authorization': 'Bearer app-token', 'Content-Type': 'application/json'}, body=b'{"a":1}')
        self.assertEqual(response.status, 200)
        entry, headers = self.last()
        local = 'localhost:%d' % self.dev_port
        self.assertEqual((entry['method'], entry['path'], entry['body']), ('POST', '/api/save?x=1', b'{"a":1}'))
        self.assertEqual(headers['host'], local)
        self.assertEqual(headers['origin'], 'http://' + local)
        self.assertEqual(headers['referer'], 'http://' + local + '/page')
        self.assertEqual(headers['cookie'], 'sid=7')
        self.assertEqual(headers['authorization'], 'Bearer app-token')  # the app's own sign-in passes
        self.assertEqual(headers['connection'], 'close')
        for name in headers:
            self.assertFalse(name.startswith(('x-forwarded-', 'tailscale-')) or name == 'forwarded', name)
        raw = repr(entry)
        self.assertNotIn(auth.COOKIE, raw)
        self.assertNotIn('tether_preview_', raw)

    def test_a_request_with_only_tether_cookies_sends_no_cookie_header(self):
        self.sign_in()
        self.request('GET', '/')
        self.assertNotIn('cookie', self.last()[1])

    def test_another_sites_origin_is_not_translated(self):
        self.sign_in()
        self.request('POST', '/api', {'Origin': 'https://evil.example'}, body=b'x')
        self.assertEqual(self.last()[1]['origin'], 'https://evil.example')

    def test_location_names_the_public_origin(self):
        self.sign_in()
        response, _ = self.request('GET', '/redirect', {'X-Forwarded-Proto': 'https'})
        self.assertEqual((response.status, response.getheader('Location')), (302, 'https://%s/login?from=/redirect' % self.host))
        response, _ = self.request('GET', '/redirect')
        self.assertEqual(response.getheader('Location'), 'http://%s/login?from=/redirect' % self.host)
        self.assertEqual(self.request('GET', '/away')[0].getheader('Location'), 'https://example.com/x')

    def test_response_headers_cannot_act_on_tether(self):
        self.sign_in()
        response, data = self.request('GET', '/cookies')
        self.assertEqual((response.status, data), (200, b'ok'))
        self.assertEqual(response.msg.get_all('Set-Cookie'), ['sid=1; Path=/; HttpOnly'])
        self.assertIsNone(response.getheader('Clear-Site-Data'))
        self.assertIsNone(response.getheader('X-Frame-Options'))
        self.assertEqual(response.getheader('Access-Control-Allow-Origin'), 'http://' + self.host)
        self.assertEqual(response.getheader('Content-Security-Policy'),
                         "frame-ancestors 'self' https://box.example http://box.example:7683 https://box.example:7683")
        self.assertEqual(response.getheader('Connection'), 'close')

    def test_bodies_stream_in_both_directions(self):
        self.sign_in()

        def chunks():
            yield b'part one, '
            yield b'part two'
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('PUT', '/upload', body=chunks(), headers={'Host': self.host, 'Cookie': self.cookie}, encode_chunked=True)
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
        self.assertEqual(self.last()[0]['body'], b'part one, part two')
        # The answer: the first chunk arrives while the dev server still holds the second one.
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('GET', '/stream', headers={'Host': self.host, 'Cookie': self.cookie})
        response = connection.getresponse()
        self.assertEqual(response.read1(5), b'first')
        self.assertFalse(Upstream.release.is_set())
        Upstream.release.set()
        self.assertEqual(response.read(), b'second')
        connection.close()

    def test_head_and_length_end_the_answer(self):
        self.sign_in()
        response, data = self.request('HEAD', '/x')
        self.assertEqual((response.status, data), (200, b''))
        response, data = self.request('GET', '/keepalive')  # returns before the dev server lets go of the connection
        self.assertEqual((response.status, data), (200, b'held'))
        self.assertFalse(Upstream.release.is_set())

    def test_websocket_frames_pass_both_ways(self):
        self.sign_in()
        client = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        self.addCleanup(client.close)
        client.sendall(('GET /ws HTTP/1.1\r\nHost: %s\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n'
                        'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nOrigin: http://%s\r\nCookie: %s\r\n\r\n' % (self.host, self.host, self.cookie)).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            head += client.recv(4096)
        self.assertTrue(head.startswith(b'HTTP/1.1 101 '), head)
        self.assertIn(b'upgrade: websocket', head.lower())
        self.assertNotIn(auth.COOKIE.encode(), head)
        for message in (b'one', b'two'):
            client.sendall(message)
            self.assertEqual(client.recv(4096), b'echo:' + message)
        _, headers = self.last()
        self.assertEqual((headers['upgrade'], headers['connection'], headers['origin'], headers['sec-websocket-version']),
                         ('websocket', 'Upgrade', 'http://localhost:%d' % self.dev_port, '13'))
        self.assertNotIn('cookie', headers)

    def test_interim_answers_are_dropped(self):
        self.sign_in()
        response, data = self.request('GET', '/early')
        self.assertEqual((response.status, data), (200, b'late'))
        self.assertIsNone(response.getheader('Set-Cookie'))

    def test_a_malformed_answer_is_refused(self):
        self.sign_in()
        response, data = self.request('GET', '/malformed')
        self.assertEqual(response.status, 502)
        self.assertIsNone(response.getheader('Set-Cookie'))
        self.assertIn(b'malformed', data)

    def test_a_stopped_dev_server_gets_a_page_that_tries_again(self):
        self.sign_in()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.previews.scan = None
        response, data = self.request('GET', '/')
        self.assertEqual(response.status, 502)
        self.assertIn(b'The dev server is not running', data)
        self.assertIn(b'http-equiv="refresh"', data)
        self.assertIsNotNone(self.previews.leases[self.port].gone)

    def test_next_dev_block_sets_the_hint(self):
        self.sign_in()
        self.assertEqual(self.previews.state()[0]['hint'], None)
        self.assertEqual(self.request('GET', '/_next/static/chunk.js')[0].status, 403)
        self.assertEqual(self.previews.state()[0]['hint'], 'next')

    def test_tether_ports_are_not_forwarded(self):
        with self.assertRaises(Refused) as caught:
            self.previews.open(self.port)
        self.assertEqual(caught.exception.code, 'denied')


@unittest.skipUnless(sys.platform.startswith('linux'), 'reads the real listener table from /proc')
class ApiTests(unittest.TestCase):
    """POST and GET /mobile/api/dev-preview on Tether's main origin, through the real request handler."""

    def setUp(self):
        import server
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        quiet = unittest.mock.patch.object(server.Handler, 'log_message', lambda *_args: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.main = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.main.serve_forever, daemon=True).start()
        self.previews = DevPreviews(ports=[7700], denied={self.main.server_port})
        patcher = unittest.mock.patch.object(dev_previews, 'PREVIEWS', self.previews)
        patcher.start()
        self.addCleanup(patcher.stop)
        mode = unittest.mock.patch.object(auth, 'MODE', 'token')
        mode.start()
        self.addCleanup(mode.stop)
        secret = unittest.mock.patch.object(auth, 'token', lambda: 'test-access-token')
        secret.start()
        self.addCleanup(secret.stop)
        self.origin = 'http://127.0.0.1:%d' % self.main.server_port
        self.headers = {'Origin': self.origin, 'X-Codex-Mobile': '1', 'Content-Type': 'application/json',
                        'Cookie': '%s=%s' % (auth.COOKIE, auth.session_value('test-access-token'))}

    def tearDown(self):
        for item in (self.main, self.upstream):
            item.shutdown()
            item.server_close()

    def call(self, method, body=None, **changed):
        connection = http.client.HTTPConnection('127.0.0.1', self.main.server_port, timeout=10)
        headers = {name: value for name, value in dict(self.headers, **changed).items() if value is not None}
        connection.request(method, '/mobile/api/dev-preview', json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = json.loads(response.read() or b'{}')
        connection.close()
        return response.status, data

    def test_only_a_signed_in_same_origin_post_gets_a_link(self):
        body = {'port': self.upstream.server_port, 'path': '/app'}
        self.assertEqual(self.call('POST', body, Cookie=None)[0], 401)
        self.assertEqual(self.call('POST', body, Origin='https://evil.example')[0], 403)
        self.assertEqual(self.call('POST', body, Origin='http://127.0.0.1:7700')[0], 403)  # a forwarded dev page
        self.assertEqual(self.call('POST', body, **{'X-Codex-Mobile': None})[0], 403)
        self.assertEqual(self.previews.leases, {})
        status, link = self.call('POST', body)
        self.assertEqual((status, link['port'], link['dev_port']), (200, 7700, self.upstream.server_port))
        self.assertTrue(link['frame'].endswith('&next=%2Fapp') and link['browser'].endswith('&next=%2Fapp'))
        token = link['frame'].split('token=')[1].split('&')[0]
        self.assertIsNotNone(self.previews.redeem(7700, token))

    def test_refusals_carry_a_reason_and_a_code(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        status, data = self.call('POST', {'port': self.upstream.server_port})
        self.assertEqual((status, data['code']), (404, 'not-listening'))
        self.assertIn('Nothing listens on port %d' % self.upstream.server_port, data['error'])
        for port, code in ((self.main.server_port, 'denied'), (22, 'denied'), ('5173', 'bad-port'), (None, 'bad-port')):
            status, data = self.call('POST', {'port': port})
            self.assertEqual((status, data['code']), (404, code), port)
        self.previews.ports = []
        self.assertEqual(self.call('POST', {'port': 5173}), (503, {'error': 'Dev server previews are turned off on this server.', 'code': 'off'}))

    def test_get_lists_the_dev_servers_and_the_open_previews(self):
        self.assertEqual(self.call('GET', Cookie=None)[0], 401)
        self.call('POST', {'port': self.upstream.server_port})
        status, data = self.call('GET')
        self.assertEqual(status, 200)
        self.assertIn(self.upstream.server_port, [row['port'] for row in data['servers']])
        self.assertNotIn(self.main.server_port, [row['port'] for row in data['servers']])
        self.assertEqual(data['previews'], [{'port': 7700, 'dev_port': self.upstream.server_port, 'hint': None}])


if __name__ == '__main__':
    unittest.main()
