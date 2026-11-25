import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import io
import unittest
from unittest import mock

import transcribe
import server
from test_tts import handler


class Reply:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({'text': 'open src slash app.py'}).encode()


class ForwardTests(unittest.TestCase):
    def sent(self, **kwargs):
        with mock.patch('urllib.request.urlopen', return_value=Reply()) as urlopen:
            reply = transcribe.transcribe(b'x', 'audio/webm', **kwargs)
        return urlopen.call_args[0][0], reply

    def test_recording_id_is_forwarded_only_when_safe(self):
        request, reply = self.sent(live=True, recording='3f2b-11aa-cc')
        self.assertEqual(request.get_header('X-recording'), '3f2b-11aa-cc')
        self.assertTrue(request.full_url.endswith('?live=1'))
        self.assertEqual(reply['text'], 'open src/app.py')
        for bad in ('', 'short', 'a b c d e f g h', 'x' * 65, 'bad\nheader-1'):
            self.assertIsNone(self.sent(recording=bad)[0].get_header('X-recording'), bad)

    def test_live_phrase_header_reaches_worker_from_server(self):
        value = handler('/mobile/api/transcribe')
        value.headers.update({'Content-Length': '1', 'Content-Type': 'audio/webm', 'X-Live-Phrase': '1'})
        value.rfile = io.BytesIO(b'x')
        with mock.patch('urllib.request.urlopen', return_value=Reply()) as urlopen:
            server.Handler.do_POST(value)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header('X-live-phrase'), '1')
        self.assertEqual(request.full_url, transcribe.URL)
        self.assertEqual(request.data, b'x')
        value.send_response.assert_called_once_with(200)
        self.assertIsNone(self.sent()[0].get_header('X-live-phrase'))


if __name__ == '__main__':
    unittest.main()
