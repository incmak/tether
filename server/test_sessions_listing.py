"""Session list sharing and ETag tests: mocked discovery, no tmux, no terminals."""
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

import server


class SessionsListingTests(unittest.TestCase):
    def setUp(self):
        server.LISTING_CACHE.update(at=0.0, generation=None, body=b'', tag='')
        self.builds = 0
        self.rows = [{'id': '%1', 'state': 'Idle'}]

        def build():
            self.builds += 1
            return {'sessions': list(self.rows), 'default_thread': None}
        self.clock = [100.0]
        patches = [patch.object(server, 'build_listing', side_effect=build),
                   patch.object(server, 'discover', return_value=[]),
                   patch.object(server.window_size, 'apply'),
                   patch.object(server, 'settings', return_value={}),
                   patch.object(server.time, 'monotonic', side_effect=lambda: self.clock[0])]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        server.forget_sessions()

    def test_pages_within_a_second_share_one_build(self):
        first = server.sessions_listing()
        self.clock[0] += 0.5
        self.assertEqual(server.sessions_listing(), first)
        self.assertEqual(self.builds, 1)
        self.assertEqual(json.loads(first[0])['sessions'], self.rows)
        self.assertTrue(first[1].startswith('W/"'))

    def test_new_list_after_a_second_or_a_tmux_write(self):
        server.sessions_listing()
        self.clock[0] += 1.1
        server.sessions_listing()
        self.assertEqual(self.builds, 2)
        self.clock[0] += 0.1
        server.forget_sessions()  # a tmux write: the next list must be new at once
        server.sessions_listing()
        self.assertEqual(self.builds, 3)

    def test_tag_changes_with_the_body(self):
        body, tag = server.sessions_listing()
        self.rows = [{'id': '%1', 'state': 'Working'}]
        self.clock[0] += 2
        body2, tag2 = server.sessions_listing()
        self.assertNotEqual(tag, tag2)
        self.rows = [{'id': '%1', 'state': 'Idle'}]
        self.clock[0] += 2
        self.assertEqual(server.sessions_listing(), (body, tag))

    def test_waiting_pages_share_a_build_in_progress(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            self.builds += 1
            started.set()
            gate.wait(2)
            return {'sessions': [], 'default_thread': None}
        with patch.object(server, 'build_listing', side_effect=slow):
            threads = [threading.Thread(target=server.sessions_listing) for _ in range(4)]
            for thread in threads:
                thread.start()
            started.wait(2)
            gate.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(self.builds, 1)

    def test_a_write_during_a_build_is_not_cached_as_new(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            self.builds += 1
            started.set()
            gate.wait(2)
            return {'sessions': [{'id': '%1', 'name': 'old'}], 'default_thread': None}
        with patch.object(server, 'build_listing', side_effect=slow):
            thread = threading.Thread(target=server.sessions_listing)
            thread.start()
            started.wait(2)
            server.forget_sessions()  # a rename lands while the old rows are being built
            gate.set()
            thread.join(2)
        self.rows = [{'id': '%1', 'name': 'new'}]
        body, _tag = server.sessions_listing()
        self.assertEqual(self.builds, 2)
        self.assertEqual(json.loads(body)['sessions'], self.rows)

    def test_a_slow_build_still_serves_the_next_second(self):
        def slow():
            self.builds += 1
            self.clock[0] += 1.2  # the build itself takes longer than a second
            return {'sessions': [], 'default_thread': None}
        with patch.object(server, 'build_listing', side_effect=slow):
            first = server.sessions_listing()
            self.clock[0] += 0.5
            self.assertEqual(server.sessions_listing(), first)
        self.assertEqual(self.builds, 1)

    def route(self, if_none_match=None):
        handler = server.Handler.__new__(server.Handler)
        handler.command, handler.path = 'GET', '/mobile/api/sessions'
        handler.headers = {'Accept-Encoding': 'gzip', 'Sec-Fetch-Site': 'same-origin'}
        if if_none_match:
            handler.headers['If-None-Match'] = if_none_match
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        handler.deny = Mock(return_value=False)
        handler.do_GET()
        return handler.send_response.call_args.args[0], dict(c.args for c in handler.send_header.call_args_list), handler.wfile.getvalue()

    def test_unchanged_list_answers_304_without_a_body(self):
        status, headers, body = self.route()
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        status, headers2, body2 = self.route(headers['ETag'])
        self.assertEqual((status, body2), (304, b''))
        self.assertNotIn('Content-Length', headers2)  # RFC 9110 8.6: only the 200's own length would be right
        self.assertEqual(headers2['ETag'], headers['ETag'])
        self.assertNotIn('Content-Encoding', headers2)

    def test_etag_matches(self):
        tag = 'W/"abc"'
        for header, expected in ((None, False), ('', False), ('W/"abc"', True), ('"abc"', True), ('"x", W/"abc"', True),
                                 ('*', True), ('W/"abd"', False), ('abc', False)):
            with self.subTest(header=header):
                self.assertEqual(server.etag_matches(header, tag), expected)


if __name__ == '__main__':
    unittest.main()
