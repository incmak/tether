"""Dev server previews: which ports Tether forwards, the token and cookie flow, and the proxy on real sockets."""
import http.client
import ipaddress
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auth
import dev_previews
from dev_previews import DevPreviews, Guard, Refused

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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'config' / dev_previews.STATE_FILE

    def pool(self, ports=range(7700, 7710), **more):
        return DevPreviews(ports=list(ports), listeners=lambda: self.listening, uid=UID, clock=lambda: self.now[0], store=self.file, **more)

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

    def test_a_full_pool_is_an_error_and_nothing_moves_by_itself(self):
        # Review of 8a40fd3, item 1: a pool port that moves hands an open page of the old app the new app's cookie.
        self.previews.open(5173)
        self.previews.open(3000)
        with self.assertRaises(Refused) as caught:
            self.previews.open(5174)
        self.assertEqual(caught.exception.code, 'pool-full')
        self.assertEqual(str(caught.exception), 'All 2 preview ports are in use (localhost:5173, localhost:3000). Release one to preview port 5174.')
        # Not when a dev server stops, not when its lease ends, not after a long time.
        self.listening = [entry for entry in self.listening if entry['port'] != 3000]
        self.now[0] += 30 * 86400
        self.assertEqual(self.previews.leases and self.previews.state(), [])
        with self.assertRaises(Refused) as caught:
            self.previews.open(5174)
        self.assertEqual(caught.exception.code, 'pool-full')
        self.assertEqual(self.previews.assignments(), {'assigned': [{'port': 7700, 'dev_port': 5173}, {'port': 7701, 'dev_port': 3000}], 'free': 0})
        self.assertEqual(self.previews.open(5173)['port'], 7700)  # an assigned dev port still opens

    def test_release_frees_the_port_and_ends_the_lease(self):
        self.previews.open(5173)
        self.previews.open(3000)
        lease = self.previews.leases[7701]
        self.assertEqual(self.previews.release(3000), 7701)
        self.assertFalse(self.previews.alive(lease))
        self.assertIsNone(self.previews.authorize(7701, self.cookie(lease)))
        self.assertEqual(self.previews.assignments(), {'assigned': [{'port': 7700, 'dev_port': 5173}], 'free': 1})
        new = self.previews.open(5174)
        self.assertEqual(new['port'], 7701)
        self.assertNotEqual(self.previews.leases[7701].secret, lease.secret)
        for port, code in ((3000, 'not-assigned'), (9999, 'not-assigned'), ('5173', 'bad-port'), (None, 'bad-port')):
            with self.assertRaises(Refused) as caught:
                self.previews.release(port)
            self.assertEqual(caught.exception.code, code)

    def test_assignments_survive_lease_expiry_and_restart(self):
        first = self.pool()
        self.assertEqual(first.open(3000)['port'], 7700)  # the first free pool port
        self.assertEqual(first.open(5173)['port'], 7701)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.now[0] += dev_previews.IDLE + 1  # every lease ends
        # The reviewer's case: 3000 and 8000 collided on 7700 while nine pool ports were free.
        self.assertEqual(first.open(8000)['port'], 7702)
        self.assertEqual(first.open(3000)['port'], 7700)
        restarted = self.pool()
        self.assertEqual(restarted.assignments()['assigned'], [{'port': 7700, 'dev_port': 3000}, {'port': 7701, 'dev_port': 5173}, {'port': 7702, 'dev_port': 8000}])
        self.assertEqual(restarted.open(5174)['port'], 7703)
        self.assertEqual(restarted.open(5173)['port'], 7701)
        restarted.release(5173)
        self.assertEqual(self.pool().open(8001)['port'], 7701)  # the release is on disk too
        # A smaller pool keeps what it knows about its ports and assigns no port twice.
        small = self.pool(ports=[7700, 7702])
        self.assertEqual(small.assignments(), {'assigned': [{'port': 7700, 'dev_port': 3000}, {'port': 7702, 'dev_port': 8000}], 'free': 0})
        with self.assertRaises(Refused):
            small.open(5173)

    def test_an_unreadable_list_assigns_nothing(self):
        self.file.parent.mkdir(parents=True)
        for text in ('not json', '{}', '{"ports": []}', '{"ports": {"7700": "3000"}}', '{"ports": {"x": 3000}}', '{"ports": {"7700": 80}}',
                     '{"ports": {"7700": 3000, "7701": 3000}}'):
            self.file.write_text(text)
            previews = self.pool()
            for action in (lambda: previews.open(5173), lambda: previews.release(5173), previews.assignments):
                with self.assertRaises(Refused, msg=text) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'state')
            self.assertIn(dev_previews.STATE_FILE, str(caught.exception))
            self.assertEqual(self.file.read_text(), text)  # Tether does not overwrite what it could not read

    def test_a_failed_save_assigns_nothing(self):
        self.file.parent.mkdir(parents=True)
        self.file.parent.chmod(0o500)
        self.addCleanup(self.file.parent.chmod, 0o700)
        previews = self.pool()
        with self.assertRaises(Refused) as caught:
            previews.open(5173)
        self.assertEqual(caught.exception.code, 'state')
        self.assertEqual((previews.leases, previews.assignments()['assigned']), ({}, []))

    def test_refusals(self):
        with self.assertRaises(Refused) as caught:
            DevPreviews(ports=[], listeners=fake_listeners, uid=UID).open(5173)
        self.assertEqual(caught.exception.code, 'off')
        for port, code in ((8080, 'denied'), (9241, 'not-listening'), (7700, 'denied'), ('x', 'bad-port')):
            with self.assertRaises(Refused) as caught:
                self.previews.open(port)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.previews.leases, {})
        self.assertEqual(self.previews.assignments()['assigned'], [])  # a refused port takes no pool port
        # Review of 8a40fd3, item 2: no socket table, no forwarding. The preview does not even start.
        with self.assertRaises(Refused) as caught:
            DevPreviews(ports=[7700], listeners=fake_listeners, uid=UID, peer_uid=None).open(5173)
        self.assertEqual(caught.exception.code, 'unsupported')
        self.assertIn('need a Linux server', str(caught.exception))

    def test_alive_ends_with_the_lease(self):
        self.previews.open(5173)
        lease = self.previews.leases[7700]
        self.assertTrue(self.previews.alive(lease))
        self.previews.release(5173)
        self.assertFalse(self.previews.alive(lease))
        self.previews.open(3000)
        self.assertFalse(self.previews.alive(lease))
        self.assertTrue(self.previews.alive(self.previews.leases[7700]))
        self.now[0] += dev_previews.IDLE + 1
        self.assertFalse(self.previews.alive(self.previews.leases[7700]))

    def test_state_lists_leases_with_hints(self):
        self.previews.open(5173)
        self.previews.leases[7700].hint = 'next'
        self.assertEqual(self.previews.state(), [{'port': 7700, 'dev_port': 5173, 'hint': 'next', 'worker': False}])


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
        with unittest.mock.patch.object(dev_previews.prefs_sync, 'host_origins', return_value=[]):
            self.assertEqual(dev_previews.frame_ancestors('box.example:7700', 7683),
                             "'self' https://box.example http://box.example:7683 https://box.example:7683")
            self.assertEqual(dev_previews.frame_ancestors('bad host;x', 7683), "'self'")
        with unittest.mock.patch.object(dev_previews.prefs_sync, 'host_origins', return_value=['https://mac.example']):
            self.assertTrue(dev_previews.frame_ancestors('box.example:7700', 7683).endswith(' https://mac.example'))

    def test_who_may_send_a_request(self):
        # Review finding 1: ports share a site, so the cookie comes with requests from other pool ports too.
        public = 'https://box.example:7700'
        allowed = dev_previews.allowed_source
        nav = {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe'}
        for method, upgrading, headers in (('GET', False, {}), ('HEAD', False, {}), ('GET', False, nav),
                                           ('GET', False, dict(nav, **{'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Dest': 'document'})),
                                           ('GET', False, {'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate'}),
                                           ('GET', False, {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'no-cors'}),
                                           ('POST', False, {'Origin': public}), ('GET', True, {'Origin': public}),
                                           ('POST', False, {'Sec-Fetch-Site': 'same-origin'}),
                                           ('GET', False, {'Origin': public, 'Sec-Fetch-Site': 'same-origin'})):
            self.assertTrue(allowed(method, upgrading, headers, public), (method, upgrading, headers))
        for method, upgrading, headers in (('POST', False, {}), ('DELETE', False, {}), ('OPTIONS', False, {}), ('GET', True, {}),
                                           ('POST', False, {'Origin': 'https://box.example:7701'}),
                                           ('POST', False, {'Origin': 'http://box.example:7700'}),
                                           ('POST', False, {'Origin': 'https://box.example'}),
                                           ('POST', False, {'Origin': 'null'}), ('POST', False, {'Origin': ''}),
                                           ('GET', False, {'Origin': 'https://box.example:7701'}),
                                           ('GET', True, {'Origin': 'https://box.example:7701'}),
                                           ('POST', False, nav), ('GET', True, nav),
                                           ('GET', False, dict(nav, **{'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script'})),
                                           ('GET', False, {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'cors'}),
                                           ('GET', False, {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'image'}),
                                           ('GET', False, dict(nav, **{'Sec-Fetch-Dest': 'object'}))):
            self.assertFalse(allowed(method, upgrading, headers, public), (method, upgrading, headers))

    def test_request_framing_has_one_meaning(self):
        import http.client as client
        import io

        def framing(lines, version='HTTP/1.1'):
            return dev_previews.request_framing(client.parse_headers(io.BytesIO(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))), version)
        self.assertEqual(framing(['Host: x']), ('none', 0))
        self.assertEqual(framing(['Content-Length: 12']), ('length', 12))
        self.assertEqual(framing(['Content-Length: 12', 'content-length:  12 ']), ('length', 12))
        self.assertEqual(framing(['Transfer-Encoding: Chunked']), ('chunked', None))
        for lines in (['Content-Length: 12', 'Content-Length: 13'], ['Content-Length: 12', 'Transfer-Encoding: chunked'],
                      ['Transfer-Encoding: gzip, chunked'], ['Transfer-Encoding: chunked', 'Transfer-Encoding: chunked'],
                      ['Transfer-Encoding: identity'], ['Content-Length: 1x'], ['Content-Length: +1'], ['Content-Length: 1, 1'],
                      ['Content-Length: -1'], ['Content-Length:'], ['A: b', ' folded'], ['A: b\x01c'],
                      ['Bad Name: x'], ['no colon here', 'A: b']):
            with self.assertRaises(ValueError, msg=lines):
                framing(lines)
        with self.assertRaises(ValueError):
            framing(['Transfer-Encoding: chunked'], 'HTTP/1.0')

    def test_raw_request_heads_are_checked_line_by_line(self):
        ok = dev_previews.raw_head_ok
        self.assertTrue(ok([b'GET /a?b=c HTTP/1.1\r\n', b'Host: x\r\n', b'X-Text: caf\xc3\xa9\tx\r\n', b'\r\n']))
        self.assertTrue(ok([b'GET / HTTP/1.0\r\n', b'\r\n']))
        for lines in ([b'GET / HTTP/1.1\r\n', b'A: b\rX: y\r\n', b'\r\n'],     # the parser would make two headers of it
                      [b'GET / HTTP/1.1\r\n', b'A: b\n', b'\r\n'],               # a bare LF ends a line for some servers
                      [b'GET / HTTP/1.1\r\n', b'A: b\r\n', b' folded\r\n', b'\r\n'],
                      [b'GET / HTTP/1.1\r\n', b'A : b\r\n', b'\r\n'], [b'GET / HTTP/1.1\r\n', b'A: b\x00\r\n', b'\r\n'],
                      [b'GET / HTTP/1.1\r\n', b'no colon\r\n', b'\r\n'], [b'GET  / HTTP/1.1\r\n', b'\r\n'],
                      [b'GET /a\rb HTTP/1.1\r\n', b'\r\n'], [b'GET / HTTP/1.1\r\n', b'A: b\r\n'], [b'GET / HTTP/1.1\r\n'], [],
                      [b'GET / HTTP/1.1\r\n'] + [b'A: b\r\n'] * 300 + [b'\r\n']):
            self.assertFalse(ok(lines), lines[:3])

    def test_response_framing_has_one_meaning(self):
        def framing(lines, code=200, method='GET', upgrading=False):
            return dev_previews.response_framing(b'\r\n'.join([b'HTTP/1.1 %d X' % code] + lines), code, method, upgrading)
        self.assertEqual(framing([b'Content-Length: 5']), ('length', 5))
        self.assertEqual(framing([b'Content-Length: 5', b'content-length: 5']), ('length', 5))
        self.assertEqual(framing([b'Transfer-Encoding: chunked']), ('chunked', None))
        self.assertEqual(framing([b'A: b']), ('close', None))
        self.assertEqual(framing([b'Content-Length: 5'], method='HEAD'), ('none', 5))
        self.assertEqual(framing([], code=304), ('none', None))
        self.assertEqual(framing([b'Upgrade: websocket'], code=101, upgrading=True), ('upgrade', None))
        self.assertIsNone(framing([b'Upgrade: websocket'], code=101))  # a switch that nobody asked for
        for lines in ([b'Content-Length: 5', b'Content-Length: 6'], [b'Content-Length: 5', b'Transfer-Encoding: chunked'],
                      [b'Transfer-Encoding: gzip'], [b'Transfer-Encoding: chunked', b'Transfer-Encoding: chunked'],
                      [b'Content-Length: 5x'], [b'Content-Length: -5'], [b'Content-Length:']):
            self.assertIsNone(framing(lines), lines)
        self.assertIsNone(framing([b'Content-Length: 5', b'Transfer-Encoding: chunked'], method='HEAD'))
        self.assertIsNone(dev_previews.response_code(b'HTTP/1.1 200 OK\r\nA: b\x01c'))
        self.assertIsNone(dev_previews.response_code(b'HTTP/1.1 200 OK\r\nA: b\x7f'))

    def test_the_marker_is_signed_per_port_and_dev_server(self):
        with unittest.mock.patch.object(dev_previews, 'marker_key', lambda: b'key'):
            first = dev_previews.marker(7700, 5173)
            self.assertTrue(first.startswith('5173.'))
            self.assertEqual(first, dev_previews.marker(7700, 5173))
            self.assertNotEqual(first.split('.')[1], dev_previews.marker(7701, 5173).split('.')[1])
            self.assertNotEqual(first.split('.')[1], dev_previews.marker(7700, 5174).split('.')[1])
        with unittest.mock.patch.object(dev_previews, 'marker_key', lambda: b'other'):
            self.assertNotEqual(first, dev_previews.marker(7700, 5173))

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
        elif self.path == '/flood':
            # A long answer for a client that stops reading: the write blocks once the buffers are full.
            self.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2000000000\r\n\r\n')
            try:
                for _ in range(4000):
                    self.connection.sendall(b'x' * 65536)
            except OSError:
                pass
            self.close_connection = True
        elif self.path == '/slow-head':
            try:
                self.connection.sendall(b'HTTP/1.1 200 OK\r\n')
                for index in range(40):
                    time.sleep(0.1)
                    self.connection.sendall(b'X-Drip-%d: 1\r\n' % index)
                self.connection.sendall(b'Content-Length: 2\r\n\r\nok')
            except OSError:
                pass
            self.close_connection = True
        elif self.path.startswith('/raw/'):
            answers = {
                'two-lengths': b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 5\r\n\r\nhello',
                'length-and-chunked': b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n',
                'odd-coding': b'HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\nhello',
                'bad-length': b'HTTP/1.1 200 OK\r\nContent-Length: 2x\r\n\r\nhi',
                'control': b'HTTP/1.1 200 OK\r\nX-A: b\x01c\r\nContent-Length: 2\r\n\r\nhi',
                'same-length-twice': b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\ncontent-length: 2\r\n\r\nhi',
                'chunk-ext': b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Trailer\r\n\r\n5;ext=1\r\nfirst\r\n6\r\nsecond\r\n0\r\nX-Trailer: hidden\r\n\r\n',
                'bad-chunk': b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\nzz\r\nsecond\r\n0\r\n\r\n',
                'worker-allowed': b'HTTP/1.1 200 OK\r\nService-Worker-Allowed: /\r\nContent-Type: text/javascript\r\nContent-Length: 2\r\n\r\nhi',
                'unasked-switch': b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nraw bytes',
            }
            self.connection.sendall(answers[self.path[len('/raw/'):]])
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
        self.upstream.handle_error = lambda *_args: None  # a cut connection is part of several tests
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.dev_port = self.upstream.server_port
        self.previews = DevPreviews()
        self.logged = []
        key = unittest.mock.patch.object(dev_previews, 'marker_key', lambda: b'test-marker-key')  # not the real access token
        key.start()
        self.addCleanup(key.stop)
        self.guard = self.make_guard()
        self.servers = dev_previews.start('127.0.0.1', 7683, previews=self.previews, ports=[0], log=self.logged.append, guard=self.guard)
        self.port = self.servers[0].server_port
        self.host = 'box.example:%d' % self.port
        self.cookie = None

    def tearDown(self):
        Upstream.release.set()
        for server in self.servers + [self.upstream]:
            server.shutdown()
            server.server_close()

    def make_guard(self):
        return Guard(lease_check=0.2)

    def request(self, method, path, headers=None, body=None, cookie=True, origin=True):
        headers = dict({'Host': self.host}, **(headers or {}))
        if cookie and self.cookie and 'Cookie' not in headers:
            headers['Cookie'] = self.cookie
        if origin and method not in ('GET', 'HEAD') and 'Origin' not in headers:
            # A browser sends Origin with every write. tailscale serve adds X-Forwarded-Proto: https.
            headers['Origin'] = ('https://' if headers.get('X-Forwarded-Proto') == 'https' else 'http://') + self.host
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
        cookies = response.msg.get_all('Set-Cookie')
        self.cookie = cookies[0].split(';')[0]
        self.marker = cookies[1].split(';')[0] if len(cookies) > 1 else None
        return link, response

    def raw(self, request, wait=10):
        """Send bytes as they are and return everything the proxy answers until it closes."""
        client = socket.create_connection(('127.0.0.1', self.port), timeout=wait)
        self.addCleanup(client.close)
        client.sendall(request)
        answer = b''
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                answer += data
        except OSError:
            pass
        client.close()  # as a browser does after an answer with Connection: close
        return answer

    def head(self, method='GET', path='/', version='HTTP/1.1', origin=True):
        lines = ['%s %s %s' % (method, path, version), 'Host: ' + self.host, 'Cookie: ' + self.cookie]
        if origin:
            lines.append('Origin: http://' + self.host)
        return '\r\n'.join(lines).encode() + b'\r\n'

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
            response, data = self.request(method, path, dict(headers or {}, Origin='http://' + self.host), cookie=False)
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
        cookie = response.msg.get_all('Set-Cookie')[0]
        self.assertTrue(cookie.startswith('tether_preview_%d=' % self.port))
        for part in ('; HttpOnly', '; SameSite=Strict', '; Secure', '; Path=/'):
            self.assertIn(part, cookie)
        self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
        # The used link: 401 without the cookie, and the page again with it (Reload, a second tap).
        self.assertEqual(self.request('GET', link['frame'], cookie=False)[0].status, 401)
        self.assertEqual(self.request('GET', link['frame'])[0].status, 302)
        self.assertNotIn('Secure', self.request('GET', link['browser'], cookie=False)[0].msg.get_all('Set-Cookie')[0])
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

    def test_requests_from_another_origin_never_reach_the_dev_server(self):
        # Review finding 1. A page on another pool port is the same site: the browser sends this port's cookie with
        # its requests. The cookie is valid here; the request is still refused.
        self.sign_in()
        other = 'http://box.example:%d' % (self.port + 1)
        script = {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script'}
        upgrade = {'Connection': 'Upgrade', 'Upgrade': 'websocket'}
        for method, path, headers in (('POST', '/api/delete-all', {'Origin': other}), ('PUT', '/api', {'Origin': other}),
                                      ('DELETE', '/api', {'Origin': 'https://evil.example'}), ('POST', '/api', {'Origin': 'null'}),
                                      ('POST', '/api', {'Origin': 'https://' + self.host}),  # another scheme is another origin
                                      ('GET', '/api/private', {'Origin': other, 'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'cors'}),
                                      ('GET', '/src/secret.js', script), ('GET', '/ws', dict(upgrade, Origin=other)),
                                      ('POST', '/form', {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})):
            response, data = self.request(method, path, headers, body=b'x' if method in ('POST', 'PUT') else None, origin=False)
            self.assertEqual(response.status, 403, (method, path, headers))
            self.assertIn(b'Tether refused this request', data)
        for method, headers in (('POST', {}), ('GET', upgrade)):  # a write or a WebSocket that names no origin at all
            self.assertEqual(self.request(method, '/ws', headers, origin=False)[0].status, 403)
        self.assertEqual(Upstream.seen, [])

    def test_the_page_itself_and_navigations_reach_the_dev_server(self):
        self.sign_in()
        own = 'http://' + self.host
        for method, headers in (('GET', {}), ('GET', {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe'}),
                                ('GET', {'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
                                ('GET', {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors'}),
                                ('POST', {'Origin': own}), ('DELETE', {'Origin': own, 'Sec-Fetch-Site': 'same-origin'}),
                                ('POST', {'Sec-Fetch-Site': 'same-origin'})):
            self.assertEqual(self.request(method, '/ok', headers, origin=False)[0].status, 200, (method, headers))
        self.assertEqual(len(Upstream.seen), 7)

    def test_the_first_sign_in_clears_the_origin_and_later_ones_do_not(self):
        # Review finding 2: this browser may hold storage of another dev server on this origin.
        link, response = self.sign_in(secure=True)
        self.assertEqual(response.getheader('Clear-Site-Data'), '"cache", "storage", "executionContexts"')
        self.assertNotIn('cookies', response.getheader('Clear-Site-Data'))  # cookies span ports: Tether's session too
        mark = response.msg.get_all('Set-Cookie')[1]
        self.assertTrue(mark.startswith('tether_preview_%d_app=%d.' % (self.port, self.dev_port)))
        for part in ('; HttpOnly', '; SameSite=Strict', '; Secure', '; Path=/'):
            self.assertIn(part, mark)
        both = '%s; %s' % (self.cookie, self.marker)
        again = self.previews.open(self.dev_port)
        response, _ = self.request('GET', again['frame'], {'Cookie': both})
        self.assertEqual(response.status, 302)
        self.assertIsNone(response.getheader('Clear-Site-Data'))
        self.assertEqual(len(response.msg.get_all('Set-Cookie')), 1)
        # A marker for another dev server, a marker with no signature and a marker for another pool port do not count.
        name = 'tether_preview_%d_app' % self.port
        for forged in (dev_previews.marker(self.port, self.dev_port + 1), '%d.' % self.dev_port, str(self.dev_port),
                       dev_previews.marker(self.port + 1, self.dev_port)):
            response, _ = self.request('GET', self.previews.open(self.dev_port)['frame'], {'Cookie': '%s; %s=%s' % (self.cookie, name, forged)})
            self.assertEqual(response.getheader('Clear-Site-Data'), '"cache", "storage", "executionContexts"', forged)
        self.assertEqual(self.request('GET', '/', {'Cookie': both})[0].status, 200)  # the marker is no key ...
        self.assertNotIn('cookie', self.last()[1])                                   # ... and stays with Tether

    def test_a_released_pool_port_ends_the_old_dev_server_there(self):
        # The person released the port: the old cookie, the old WebSocket and the old storage must not carry over.
        self.sign_in()
        old_cookie, old_marker = self.cookie, self.marker
        client = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        self.addCleanup(client.close)
        client.sendall(self.head(path='/ws') + b'Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n')
        self.assertTrue(client.recv(4096).startswith(b'HTTP/1.1 101 '))
        client.sendall(b'one')
        self.assertEqual(client.recv(4096), b'echo:one')
        second = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        second.daemon_threads = True
        second.handle_error = lambda *_args: None
        threading.Thread(target=second.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (second.shutdown(), second.server_close()))
        with self.assertRaises(Refused) as caught:  # the pool has one port: nothing moves by itself
            self.previews.open(second.server_port)
        self.assertEqual(caught.exception.code, 'pool-full')
        client.sendall(b'two')
        self.assertEqual(client.recv(4096), b'echo:two')
        self.previews.release(self.dev_port)
        self.assertEqual(client.recv(4096), b'')  # the old page's live connection ended
        link = self.previews.open(second.server_port)
        self.assertEqual(link['port'], self.port)
        self.assertEqual(self.request('GET', '/', {'Cookie': old_cookie})[0].status, 401)
        self.assertEqual(self.request('POST', '/api', {'Cookie': old_cookie})[0].status, 401)
        response, _ = self.request('GET', link['frame'], {'Cookie': '%s; %s' % (old_cookie, old_marker)})
        self.assertEqual((response.status, response.getheader('Clear-Site-Data')), (302, '"cache", "storage", "executionContexts"'))
        self.assertTrue(response.msg.get_all('Set-Cookie')[1].startswith('tether_preview_%d_app=%d.' % (self.port, second.server_port)))

    def test_a_connection_that_another_user_owns_gets_no_bytes(self):
        # Review finding 3: the listener check is cached; the port may belong to someone else when the proxy connects.
        self.sign_in()
        self.assertEqual(self.request('GET', '/')[0].status, 200)  # the listener table now says "ours", for 2 s
        Upstream.seen = []
        for owner in (os.getuid() + 1, 0):
            self.previews.peer_uid = lambda sock, owner=owner: owner
            response, data = self.request('POST', '/api/secret', body=b'token=1')
            self.assertEqual(response.status, 502, owner)
            self.assertIn(b'now belongs to another program or user', data)
        self.assertIsNotNone(self.previews.leases[self.port].gone)

        # Review of 8a40fd3, item 2: when the owner of this very connection cannot be read, nothing is forwarded.
        # A second look at the listeners does not name the connection, so it is no substitute.
        def broken(sock):
            raise OSError('no socket table')
        for reader in (lambda sock: None, broken, None):  # not in the table; the table cannot be read; no table (macOS)
            self.previews.peer_uid = reader
            response, data = self.request('POST', '/api/secret', body=b'token=1')
            self.assertEqual(response.status, 502)
            self.assertIn(b'Tether cannot check who owns the connection to port %d' % self.dev_port, data)
        self.assertEqual(Upstream.seen, [])
        self.previews.peer_uid = dev_previews.connection_uid
        self.assertEqual(self.request('GET', '/')[0].status, 200)

    def test_the_socket_table_names_the_owner_of_this_connection(self):
        for family, address in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1'), (socket.AF_INET6, '::')):
            with socket.socket(family) as listener:
                try:
                    listener.bind((address, 0))
                except OSError:
                    continue  # no IPv6 here
                listener.listen(2)
                port = listener.getsockname()[1]
                targets = ['127.0.0.1', '::1'] if address == '::' else [address]
                for target in targets:
                    try:
                        client = socket.create_connection((target, port), timeout=5)
                    except OSError:
                        continue  # an IPv6-only wildcard listener
                    with client:
                        self.assertEqual(dev_previews.connection_uid(client), os.getuid(), (address, target))  # before accept
                        accepted, _ = listener.accept()
                        with accepted:
                            self.assertEqual(dev_previews.connection_uid(client), os.getuid(), (address, target))
                            with tempfile.TemporaryDirectory() as empty:
                                self.assertIsNone(dev_previews.connection_uid(client, tries=1, root=empty))

    def test_no_service_worker_can_be_installed(self):
        # Round 3: app A's worker survived the release of its port and intercepted app B's sign-in link.
        link = self.previews.open(self.dev_port)
        token_link = link['frame']
        worker = {'Service-Worker': 'script', 'Sec-Fetch-Dest': 'serviceworker', 'Sec-Fetch-Site': 'same-origin'}
        response, data = self.request('GET', token_link, worker, cookie=False)  # the sign-in link as a worker script
        self.assertEqual(response.status, 403)
        self.assertIn(b'Service workers are off in previews', data)
        self.assertIsNone(response.getheader('Set-Cookie'))
        response, _ = self.request('GET', token_link, cookie=False)  # the token was not spent by the refused fetch
        self.assertEqual(response.status, 302)
        self.cookie = response.msg.get_all('Set-Cookie')[0].split(';')[0]
        self.assertFalse(self.previews.state()[0]['worker'])
        for path, headers in (('/sw.js', worker), ('/sw.js', {'Service-Worker': 'script'}), ('/', {'service-worker': 'anything'}),
                              ('/src/sw.ts?worker_file&type=module', worker)):
            response, data = self.request('GET', path, headers)
            self.assertEqual(response.status, 403, (path, headers))
            self.assertIn(b'Service workers are off in previews', data)
        self.assertEqual(self.request('GET', '/sw.js', worker, cookie=False)[0].status, 403)
        self.assertEqual(Upstream.seen, [])
        self.assertTrue(self.previews.state()[0]['worker'])  # the drawer can say why the worker is missing
        # The same file as a plain script is fine; a header that widens a worker's scope never passes.
        response, data = self.request('GET', '/raw/worker-allowed')
        self.assertEqual((response.status, data), (200, b'hi'))
        self.assertIsNone(response.getheader('Service-Worker-Allowed'))

    def test_a_request_head_has_a_size_limit(self):
        # Review of 8a40fd3, item 3: 120 KB and 240 KB heads reached the sign-in check.
        self.sign_in()
        calls = []
        real = self.previews.authorize
        self.previews.authorize = lambda *args: calls.append(args) or real(*args)
        for size in (120, 240, 2000):
            many = b''.join(b'X-Fill-%d: %s\r\n' % (index, b'a' * 4000) for index in range(size // 4))
            for head in (self.head(), b'GET / HTTP/1.1\r\nHost: x\r\n'):  # signed in or not
                answer = self.raw(head + many + b'\r\n')
                self.assertTrue(answer.startswith(b'HTTP/1.1 431 '), (size, answer[:40]))
        one = self.raw(self.head() + b'X-Big: ' + b'a' * 70000 + b'\r\n\r\n')  # one long line
        self.assertTrue(one.startswith(b'HTTP/1.1 431 '), one[:40])
        self.assertEqual((calls, Upstream.seen), ([], []))
        fits = b''.join(b'X-Fill-%d: %s\r\n' % (index, b'a' * 4000) for index in range(15))  # 60 KB
        self.assertTrue(self.raw(self.head() + fits + b'\r\n').startswith(b'HTTP/1.1 200 '))
        self.assertEqual(len(calls), 1)

    def late(self, first, rest):
        """Send `first`, wait until the proxy answers, send `rest`, then close our side: what did the proxy answer?"""
        client = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        self.addCleanup(client.close)
        client.sendall(first)
        answer = client.recv(65536)  # Tether has answered and ended its side
        time.sleep(0.2)
        client.sendall(rest)
        client.shutdown(socket.SHUT_WR)
        closed = time.monotonic()
        while data := client.recv(65536):
            answer += data
        self.assertLess(time.monotonic() - closed, 1, 'Tether stops reading when the client closes')
        return answer

    def test_an_answer_survives_a_client_that_is_still_sending(self):
        # B139: Tether answers before the client has sent everything. A close with unread input made the kernel reset
        # the connection, and the reset dropped the answer before the client read it (RFC 9112, 9.6: lingering close).
        self.sign_in()
        many = b''.join(b'X-Many-%d: a\r\n' % index for index in range(120))
        cases = [
            ('431 from Tether, signed in', self.head() + b'X-Big: ' + b'a' * 70000 + b'\r\n', b'431', b'</html>'),
            ('431 from Tether, no cookie', b'GET / HTTP/1.1\r\nHost: x\r\nX-Big: ' + b'a' * 70000 + b'\r\n', b'431', b'</html>'),
            ('431 from the parser: too many headers', self.head() + many + b'\r\n', b'431', b'</html>'),
            ('302 from the sign-in link, body unread', ('GET %s HTTP/1.1\r\nHost: x\r\nContent-Length: 200000\r\n\r\n'
                                                        % self.previews.open(self.dev_port, '/')['frame']).encode() + b'a' * 1000,
             b'302', b'\r\n\r\n')]
        for name, first, status, end in cases:
            with self.subTest(name):
                answer = self.late(first, b'X-More: ' + b'a' * 100000 + b'\r\n\r\n')
                self.assertTrue(answer.startswith(b'HTTP/1.1 ' + status + b' '), answer[:40])
                self.assertIn(end, answer)

    def test_ambiguous_requests_are_refused(self):
        # Review finding 5 (RFC 9112, 6.3): the proxy and the dev server must read the same message.
        self.sign_in()
        post = self.head('POST', '/api')
        for name, request in (
                ('two lengths', post + b'Content-Length: 5\r\nContent-Length: 11\r\n\r\nhelloGET /x'),
                ('length and chunked', post + b'Content-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n'),
                ('chunked and length', post + b'Transfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n0\r\n\r\n'),
                ('two codings', post + b'Transfer-Encoding: gzip, chunked\r\n\r\n0\r\n\r\n'),
                ('chunked twice', post + b'Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n'),
                ('not a number', post + b'Content-Length: 5x\r\n\r\nhello'),
                ('signed number', post + b'Content-Length: +5\r\n\r\nhello'),
                ('folded header', post + b'X-A: b\r\n Transfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\nhello'),
                ('control character', post + b'X-A: b\x01c\r\nContent-Length: 5\r\n\r\nhello'),
                ('bare CR', post + b'X-A: b\rX-B: c\r\nContent-Length: 5\r\n\r\nhello'),
                ('bare LF', post + b'X-A: b\nContent-Length: 5\r\n\r\nhello'),
                ('space before the colon', post + b'Content-Length : 5\r\n\r\nhello'),
                ('chunked in HTTP/1.0', self.head('POST', '/api', 'HTTP/1.0') + b'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n'),
                ('WebSocket with a body', self.head('GET', '/ws') + b'Connection: Upgrade\r\nUpgrade: websocket\r\nContent-Length: 5\r\n\r\nhello')):
            answer = self.raw(request)
            self.assertTrue(answer.startswith(b'HTTP/1.1 400 '), (name, answer[:60]))
        for name, body in (('bad chunk size', b'5x\r\nhello\r\n0\r\n\r\n'), ('no chunk end', b'5\r\nhelloXX0\r\n\r\n'),
                           ('negative size', b'-5\r\nhello\r\n0\r\n\r\n'), ('bad trailer', b'5\r\nhello\r\n0\r\nno colon\r\n\r\n')):
            self.assertNotIn(b' 200 ', self.raw(post + b'Transfer-Encoding: chunked\r\n\r\n' + body)[:20], name)
        self.assertEqual(Upstream.seen, [])  # no complete request reached the dev server

    def test_request_framing_is_written_again(self):
        self.sign_in()
        post = self.head('POST', '/api')
        answer = self.raw(post + b'Transfer-Encoding: chunked\r\nTrailer: X-Trailer\r\n\r\n5;ext=1\r\nhello\r\n6 ;x\r\n world\r\n0\r\nX-Trailer: hidden\r\n\r\n')
        self.assertTrue(answer.startswith(b'HTTP/1.1 200 '), answer[:60])
        entry, headers = self.last()
        self.assertEqual(entry['body'], b'hello world')
        self.assertEqual(headers['transfer-encoding'], 'chunked')
        names = [name for name, _ in entry['headers']]
        for gone in ('content-length', 'trailer', 'x-trailer'):
            self.assertNotIn(gone, names)
        answer = self.raw(post + b'Content-Length: 5\r\ncontent-length: 5\r\n\r\nhello')  # the same length twice is one length
        self.assertTrue(answer.startswith(b'HTTP/1.1 200 '), answer[:60])
        entry, _ = self.last()
        self.assertEqual(entry['body'], b'hello')
        self.assertEqual([value for name, value in entry['headers'] if name == 'content-length'], ['5'])

    def test_ambiguous_answers_are_refused(self):
        self.sign_in()
        for name in ('two-lengths', 'length-and-chunked', 'odd-coding', 'bad-length', 'control', 'unasked-switch'):
            response, data = self.request('GET', '/raw/' + name)
            self.assertEqual(response.status, 502, name)
            self.assertNotIn(b'hello', data)
            self.assertNotIn(b'raw bytes', data)

    def test_answer_framing_is_written_again(self):
        self.sign_in()
        answer = self.raw(self.head(path='/raw/chunk-ext') + b'\r\n')
        head, _, body = answer.partition(b'\r\n\r\n')
        self.assertTrue(head.startswith(b'HTTP/1.1 200 '))
        self.assertIn(b'transfer-encoding: chunked', head.lower())
        self.assertNotIn(b'trailer', head.lower())
        self.assertEqual(body, b'5\r\nfirst\r\n6\r\nsecond\r\n0\r\n\r\n')  # no extension, no trailer
        response, data = self.request('GET', '/raw/chunk-ext')
        self.assertEqual((response.status, data), (200, b'firstsecond'))
        # An HTTP/1.0 client cannot read chunks: the body comes plain and ends with the connection.
        answer = self.raw(self.head(path='/raw/chunk-ext', version='HTTP/1.0') + b'\r\n')
        head, _, body = answer.partition(b'\r\n\r\n')
        self.assertNotIn(b'transfer-encoding', head.lower())
        self.assertEqual(body, b'firstsecond')
        response, data = self.request('GET', '/raw/same-length-twice')
        self.assertEqual((response.status, data, response.msg.get_all('Content-Length')), (200, b'hi', ['2']))
        # A broken chunk ends the answer without the final chunk: the client knows it is incomplete.
        answer = self.raw(self.head(path='/raw/bad-chunk') + b'\r\n')
        self.assertTrue(answer.endswith(b'5\r\nfirst\r\n'), answer[-40:])

    def test_location_names_the_public_origin(self):
        self.sign_in()
        response, _ = self.request('GET', '/redirect', {'X-Forwarded-Proto': 'https'})
        self.assertEqual((response.status, response.getheader('Location')), (302, 'https://%s/login?from=/redirect' % self.host))
        response, _ = self.request('GET', '/redirect')
        self.assertEqual(response.getheader('Location'), 'http://%s/login?from=/redirect' % self.host)
        self.assertEqual(self.request('GET', '/away')[0].getheader('Location'), 'https://example.com/x')

    def test_response_headers_cannot_act_on_tether(self):
        patcher = unittest.mock.patch.object(dev_previews.prefs_sync, 'host_origins', return_value=[])  # not this machine's list
        patcher.start()
        self.addCleanup(patcher.stop)
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
        connection.request('PUT', '/upload', body=chunks(), headers={'Host': self.host, 'Cookie': self.cookie, 'Origin': 'http://' + self.host}, encode_chunked=True)
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


class LimitTests(ProxyTests):
    """Review finding 4: stalled clients and dev servers do not hold workers. Small limits make the tests fast."""

    def make_guard(self):
        return Guard(connections=3, sockets=1, accept=0.3, head=0.6, write=0.5, upstream_head=0.8, lease_check=0.2)

    def free_slots(self, want, wait=6):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and self.guard.connections._value != want:
            time.sleep(0.05)
        return self.guard.connections._value

    def test_a_dripping_request_head_has_one_deadline(self):
        client = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        self.addCleanup(client.close)
        client.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n')
        started, closed = time.monotonic(), False
        try:
            while time.monotonic() - started < 4:
                time.sleep(0.2)  # each read gets bytes in time; the head as a whole does not
                client.sendall(b'X-Drip: 1\r\n')
                client.settimeout(0.01)
                try:
                    if client.recv(1) == b'':
                        closed = True
                        break
                except socket.timeout:
                    pass
        except OSError:
            closed = True
        self.assertTrue(closed, 'the dripping connection stayed open')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.free_slots(3), 3)

    def test_connections_have_a_limit(self):
        self.guard.head = 30  # the idle connections below stay
        idle = [socket.create_connection(('127.0.0.1', self.port), timeout=5) for _ in range(3)]
        for sock in idle:
            self.addCleanup(sock.close)
        self.assertEqual(self.free_slots(0), 0)
        started = time.monotonic()
        self.assertEqual(self.raw(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n', wait=5), b'')  # closed, no worker
        self.assertLess(time.monotonic() - started, 3)
        idle[0].close()
        self.assertTrue(self.raw(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n').startswith(b'HTTP/1.1 401 '))
        for sock in idle:
            sock.close()
        self.assertEqual(self.free_slots(3), 3)

    def test_websockets_have_a_limit(self):
        self.sign_in()
        upgrade = self.head(path='/ws') + b'Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n'
        first = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        self.addCleanup(first.close)
        first.sendall(upgrade)
        self.assertTrue(first.recv(4096).startswith(b'HTTP/1.1 101 '))
        self.assertTrue(self.raw(upgrade).startswith(b'HTTP/1.1 503 '))
        self.assertEqual(self.request('GET', '/')[0].status, 200)  # plain requests still pass
        first.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.guard.sockets._value != 1:
            time.sleep(0.05)
        second = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        self.addCleanup(second.close)
        second.sendall(upgrade)
        self.assertTrue(second.recv(4096).startswith(b'HTTP/1.1 101 '))

    def test_a_client_that_stops_reading_loses_its_worker(self):
        self.sign_in()
        client = socket.socket()
        self.addCleanup(client.close)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(5)
        client.connect(('127.0.0.1', self.port))
        client.sendall(self.head(path='/flood') + b'\r\n')
        self.assertTrue(client.recv(12).startswith(b'HTTP/1.1 200'))
        self.assertEqual(self.free_slots(2, wait=1), 2)  # the worker is busy now
        self.assertEqual(self.free_slots(3), 3)          # ... and gives up when the client takes no more bytes

    def test_a_websocket_peer_that_stops_reading_loses_its_worker(self):
        self.sign_in()
        client = socket.socket()
        self.addCleanup(client.close)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(5)
        client.connect(('127.0.0.1', self.port))
        client.sendall(self.head(path='/ws') + b'Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n')
        self.assertTrue(client.recv(4096).startswith(b'HTTP/1.1 101 '))
        try:
            for _ in range(2000):  # the echo comes back; nobody reads it
                client.sendall(b'x' * 65536)
        except OSError:
            pass
        self.assertEqual(self.free_slots(3), 3)
        self.assertEqual(self.guard.sockets._value, 1)

    def test_a_dripping_answer_head_has_one_deadline(self):
        self.sign_in()
        started = time.monotonic()
        response, data = self.request('GET', '/slow-head')
        self.assertEqual(response.status, 502)
        self.assertIn(b'took too long', data)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.free_slots(3), 3)


class LingerTests(unittest.TestCase):
    """The lingering close after Tether's own answer ends when the client closes, goes quiet, or takes too long."""

    def pair(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return ours, theirs

    def timed(self, sock, **limits):
        started = time.monotonic()
        dev_previews.linger(sock, **limits)
        return time.monotonic() - started

    def test_it_ends_when_the_client_closes(self):
        ours, theirs = self.pair()
        theirs.sendall(b'late bytes')
        theirs.shutdown(socket.SHUT_WR)
        self.assertLess(self.timed(ours, seconds=5, idle=5), 1)
        self.assertEqual(theirs.recv(1), b'', 'our side was ended first')

    def test_a_quiet_client_holds_it_only_for_the_idle_time(self):
        ours, _theirs = self.pair()
        self.assertLess(self.timed(ours, seconds=5, idle=0.2), 1)

    def test_a_client_that_never_stops_holds_it_only_for_the_total_time(self):
        ours, theirs = self.pair()
        stop = threading.Event()
        def send():
            while not stop.is_set():
                try:
                    theirs.sendall(b'x' * 1024)
                except OSError:
                    return
                time.sleep(0.01)
        sender = threading.Thread(target=send)
        sender.start()
        self.addCleanup(sender.join, 5)
        self.addCleanup(stop.set)
        took = self.timed(ours, seconds=0.5, idle=0.2)
        self.assertGreater(took, 0.4, 'it read while the bytes came')
        self.assertLess(took, 1.5)


class GuardThreadTests(unittest.TestCase):
    """The head-deadline thread runs only while a connection waits for its head."""

    def wait_for(self, check, wait=5):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not check():
            time.sleep(0.02)
        return check()

    def watched(self, guard):
        """A socket pair whose head deadline (60 s) cannot pass during the test, and the guard thread watching it."""
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        guard.watch(ours)
        with guard.lock:
            return ours, theirs, guard.thread

    def test_thread_closes_a_late_head_then_stops_and_starts_again(self):
        guard = Guard(head=60)
        for round in range(2):
            with self.subTest(round=round):
                ours, theirs, thread = self.watched(guard)
                self.assertTrue(thread.is_alive())
                with guard.lock:
                    guard.deadlines[id(ours)] = (ours, 0)  # the head is now late
                theirs.settimeout(5)
                self.assertEqual(theirs.recv(1), b'')  # the late socket was shut down
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(self.wait_for(lambda: guard.thread is None))

    def test_thread_keeps_running_while_a_connection_waits(self):
        guard = Guard(head=60)
        ours, theirs, thread = self.watched(guard)
        time.sleep(0.6)  # two or more checks of the deadlines
        self.assertTrue(thread.is_alive())
        guard.unwatch(ours)
        thread.join(5)
        self.assertFalse(thread.is_alive())


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

    def call(self, method, body=None, path='/mobile/api/dev-preview', **changed):
        connection = http.client.HTTPConnection('127.0.0.1', self.main.server_port, timeout=10)
        headers = {name: value for name, value in dict(self.headers, **changed).items() if value is not None}
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
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
        self.assertEqual(data['previews'], [{'port': 7700, 'dev_port': self.upstream.server_port, 'hint': None, 'worker': False}])
        self.assertEqual((data['assigned'], data['free']), ([{'port': 7700, 'dev_port': self.upstream.server_port}], 0))

    def test_only_the_person_releases_a_port(self):
        # Review of 8a40fd3, item 1: a full pool is an error; the release is a signed-in, same-origin POST.
        release, dev = '/mobile/api/dev-preview/release', self.upstream.server_port
        self.call('POST', {'port': dev})
        status, data = self.call('POST', {'port': self.main.server_port + 1})
        self.assertEqual(status, 404)  # nothing listens there: not-listening comes before pool-full
        other = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.addCleanup(other.server_close)
        status, data = self.call('POST', {'port': other.server_port})
        self.assertEqual((status, data['code']), (503, 'pool-full'))
        self.assertIn('localhost:%d' % dev, data['error'])
        self.assertEqual(self.call('POST', {'port': dev}, path=release, Cookie=None)[0], 401)
        self.assertEqual(self.call('POST', {'port': dev}, path=release, Origin='http://127.0.0.1:7700')[0], 403)  # a forwarded dev page
        self.assertEqual(self.call('POST', {'port': dev}, path=release, **{'X-Codex-Mobile': None})[0], 403)
        self.assertEqual(self.call('GET', path=release)[0], 404)
        self.assertIn(7700, self.previews.leases)
        self.assertEqual(self.call('POST', {'port': dev}, path=release), (200, {'ok': True, 'assigned': [], 'free': 1}))
        self.assertEqual(self.previews.leases, {})
        status, data = self.call('POST', {'port': dev}, path=release)
        self.assertEqual((status, data['code']), (404, 'not-assigned'))
        self.assertEqual(self.call('POST', {'port': other.server_port})[1]['port'], 7700)


if __name__ == '__main__':
    unittest.main()
