"""Chat stream compression tests use a mocked handler: no sockets, no terminals."""
import gzip
import io
import json
import unittest
import zlib
from unittest.mock import Mock, patch

import server


class StreamGzipTests(unittest.TestCase):
    def stream(self, encoding='gzip, deflate, br', site='same-origin'):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        if encoding is not None:
            handler.headers['Accept-Encoding'] = encoding
        if site is not None:
            handler.headers['Sec-Fetch-Site'] = site
        chunks = []

        class Sink(io.BytesIO):
            def flush(self):
                chunks.append(self.getvalue()[sum(map(len, chunks)):])

        handler.wfile = Sink()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        calls = []

        def payload(*args, **kwargs):
            calls.append(1)
            return {'state': 'Idle', 'messages': [{'text': f'message {min(len(calls), 3)} ' * 50}]}
        clock, ticks = iter(range(1000)), iter(range(1000))
        with patch.object(server, 'choose', return_value={'id': 'p1', 'agent': 'codex'}), \
                patch.object(server, 'change_signature', side_effect=lambda config: next(clock)), \
                patch.object(server, 'capture', return_value='screen'), \
                patch.object(server, 'chat_payload', side_effect=payload), \
                patch.object(server.time, 'monotonic', side_effect=lambda: next(ticks) * 0.5), \
                patch.object(server.time, 'sleep'):
            handler.stream_chat('p1', False, lifetime=10)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        return headers, [c for c in chunks if c]

    def test_stream_is_one_gzip_stream_and_each_flush_decodes_alone(self):
        headers, chunks = self.stream()
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(headers['Vary'], 'Accept-Encoding, Sec-Fetch-Site')
        self.assertNotIn('Content-Length', headers)
        reader, text = zlib.decompressobj(31), ''
        for chunk in chunks[:-1]:  # each flush must decode at once: the client sees the event without waiting
            piece = reader.decompress(chunk).decode()
            self.assertTrue(piece.endswith('\n\n'), piece)
            text += piece
        self.assertEqual(reader.decompress(chunks[-1]), b'')  # the last chunk is the gzip trailer
        self.assertTrue(reader.eof)
        self.assertEqual(gzip.decompress(b''.join(chunks)).decode(), text)
        self.assertTrue(text.startswith('retry: 2000\n\n'))
        events = [json.loads(line[6:]) for line in text.split('\n') if line.startswith('data: {"')]
        self.assertGreaterEqual(len(events), 2)
        self.assertIn('message 1', events[0]['messages'][0]['text'])

    def test_raw_stream_without_gzip_or_from_another_site(self):
        for encoding, site in ((None, 'same-origin'), ('identity', 'same-origin'), ('gzip', 'cross-site'), ('gzip', 'same-site')):
            with self.subTest(encoding=encoding, site=site):
                headers, chunks = self.stream(encoding, site)
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(headers['Vary'], 'Accept-Encoding, Sec-Fetch-Site')
                self.assertTrue(b''.join(chunks).startswith(b'retry: 2000\n\ndata: {'))


if __name__ == '__main__':
    unittest.main()
