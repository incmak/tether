"""Voice reports use fake HTTP handlers and never access a terminal."""
import io
import json
import unittest
from unittest.mock import Mock, patch

import server
import voice_log


def handler(body=None, path=voice_log.PATH):
    value = server.Handler.__new__(server.Handler)
    value.path, value.command = path, 'POST'
    raw = json.dumps(body).encode() if body is not None else b''
    value.headers = {'Content-Length': str(len(raw)), 'Host': 'phone.test',
                     'Origin': 'https://phone.test', 'X-Codex-Mobile': '1'}
    value.rfile, value.wfile = io.BytesIO(raw), io.BytesIO()
    value.deny = Mock(return_value=False)
    value.send_response, value.send_header, value.end_headers = Mock(), Mock(), Mock()
    return value


class VoiceLogTests(unittest.TestCase):
    def setUp(self):
        voice_log.REPORTS.clear()
        self.stderr = io.StringIO()
        self.capture = patch('sys.stderr', self.stderr)
        self.capture.start()
        self.addCleanup(self.capture.stop)
        self.addCleanup(voice_log.REPORTS.clear)

    def test_fields_trimmed_unknown_and_non_string_fields_dropped(self):
        value = handler({'kind': ' dictation ', 'event': ' ' + 'e' * 50,
                         'trail': 't' * 450, 'detail': 'd' * 350, 'ua': 'u' * 250,
                         'ms': 900000, 'transcript': 'private words', 'audio': 'private audio'})
        voice_log.post(value)
        value.send_response.assert_called_once_with(204)
        self.assertEqual(value.wfile.getvalue(), b'')
        self.assertEqual(self.stderr.getvalue(),
                         f'voice-log kind="dictation" event="{"e" * 40}" ms=600000 '
                         f'trail="{"t" * 400}" detail="{"d" * 300}" ua="{"u" * 200}"\n')
        self.stderr.seek(0)
        self.stderr.truncate()
        voice_log.post(handler({'kind': 'friday', 'event': 7, 'trail': [], 'detail': {}, 'ms': True}))
        self.assertEqual(self.stderr.getvalue(),
                         'voice-log kind="friday" event="" ms=- trail="" detail="" ua=""\n')

    def test_log_format_quotes_and_newlines(self):
        value = handler({'kind': 'friday', 'event': 'closed', 'ms': -3,
                         'trail': ' start\nend ', 'detail': 'a "quote"\r\nnext', 'ua': 'browser\\test'})
        voice_log.post(value)
        self.assertEqual(self.stderr.getvalue(),
                         'voice-log kind="friday" event="closed" ms=0 trail="start end" '
                         'detail="a \\"quote\\"  next" ua="browser\\\\test"\n')

    def test_big_body_rejected_without_read(self):
        value = handler({'kind': 'dictation', 'trail': 'x' * 4096})
        value.rfile = Mock()
        voice_log.post(value)
        value.send_response.assert_called_once_with(413)
        value.rfile.read.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), '')

    def test_invalid_json_and_body(self):
        for raw in (b'not json', b'\xff', b'[]', b'{}', b'', b'{"kind": "other"}'):
            with self.subTest(raw=raw):
                value = handler()
                value.rfile = io.BytesIO(raw)
                value.headers['Content-Length'] = str(len(raw))
                voice_log.post(value)
                value.send_response.assert_called_once_with(400)
        value = handler()
        value.headers['Content-Length'] = 'bad'
        voice_log.post(value)
        value.send_response.assert_called_once_with(400)
        self.assertEqual(self.stderr.getvalue(), '')

    def test_rate_limit_and_expiry(self):
        with patch.object(voice_log.time, 'monotonic', return_value=100) as clock:
            for _ in range(60):
                value = handler({'kind': 'dictation', 'event': 'empty'})
                voice_log.post(value)
                value.send_response.assert_called_once_with(204)
            clock.return_value = 159.9
            value = handler({'kind': 'friday', 'event': 'closed'})
            voice_log.post(value)
            value.send_response.assert_called_once_with(429)
            self.assertEqual(len(self.stderr.getvalue().splitlines()), 60)
            clock.return_value = 160
            value = handler({'kind': 'friday', 'event': 'closed'})
            voice_log.post(value)
            value.send_response.assert_called_once_with(204)

    def test_server_route_with_query_and_security_checks(self):
        self.assertTrue(voice_log.handles(voice_log.PATH + '?source=voice'))
        self.assertFalse(voice_log.handles(voice_log.PATH + '/other'))
        value = handler({'kind': 'dictation', 'event': 'ok'}, voice_log.PATH + '?source=voice')
        server.Handler.do_POST(value)
        value.send_response.assert_called_once_with(204)
        for change in ('signed-out', 'origin', 'header'):
            value = handler({'kind': 'dictation', 'event': 'ok'})
            if change == 'signed-out':
                value.deny.return_value = True
            elif change == 'origin':
                value.headers['Origin'] = 'https://other.test'
            else:
                del value.headers['X-Codex-Mobile']
            with patch.object(voice_log, 'post') as post:
                server.Handler.do_POST(value)
            post.assert_not_called()
            if change != 'signed-out':
                value.send_response.assert_called_once_with(403)


if __name__ == '__main__':
    unittest.main()
