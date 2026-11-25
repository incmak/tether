import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
from unittest import mock

import server


class Fake:
    """A handler with only what respond() reads, so the test does not open a socket."""
    def __init__(self, command, path):
        self.command, self.path, self.sent = command, path, []
        self.wfile = mock.Mock()
    send_response = lambda self, status: self.sent.append(status)
    send_header = lambda self, *args: None
    end_headers = lambda self: None


class SessionsCacheTest(unittest.TestCase):
    def setUp(self):
        server.SESSIONS_CACHE[:] = [10 ** 9, ['old']]  # fresh far into the future

    def respond(self, command, path, status=200):
        server.Handler.respond(Fake(command, path), status, {})

    def test_split_and_close_expire_the_list(self):
        for command, path in (('POST', '/mobile/api/split'), ('DELETE', '/mobile/api/session'), ('POST', '/mobile/api/fork')):
            server.SESSIONS_CACHE[0] = 10 ** 9
            self.respond(command, path, 201)
            self.assertEqual(server.SESSIONS_CACHE[0], 0.0, path)

    def test_other_writes_and_failures_keep_it(self):
        self.respond('POST', '/mobile/api/prefs')
        self.respond('POST', '/mobile/api/split', 400)
        self.respond('GET', '/mobile/api/sessions')
        self.assertEqual(server.SESSIONS_CACHE[0], 10 ** 9)


if __name__ == '__main__':
    unittest.main()
