"""Speech tests use fake models and HTTP replies, never an agent terminal."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

import server
import tts
import tts_worker


def handler(path='/mobile/api/tts', body=None, method='POST'):
    value = server.Handler.__new__(server.Handler)
    value.path, value.command = path, method
    raw = json.dumps(body).encode() if body is not None else b''
    value.headers = {'Content-Length': str(len(raw)), 'Host': 'phone.test',
                     'Origin': 'https://phone.test', 'X-Codex-Mobile': '1'}
    value.rfile, value.wfile = io.BytesIO(raw), io.BytesIO()
    value.deny = Mock(return_value=False)
    value.send_response, value.send_header, value.end_headers = Mock(), Mock(), Mock()
    return value


def audio():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(struct.pack('<hhh', 0, 100, -100))
    return output.getvalue()


def reply(data=None, kind='audio/wav'):
    response = Mock(headers={'Content-Type': kind})
    response.read.return_value = data if data is not None else audio()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class ProxyTests(unittest.TestCase):
    def test_speak_forwards_options_and_returns_wav(self):
        with patch.object(tts, 'URL', 'http://127.0.0.1:12345'), \
                patch('urllib.request.urlopen', return_value=reply()) as open_url:
            data, kind = tts.speak(' Hello. ', 'bm_george', 1.2)
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:12345/speak')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'text': 'Hello.', 'voice': 'bm_george', 'speed': 1.2})
        self.assertEqual((data, kind), (audio(), 'audio/wav'))

    def test_defaults_are_left_to_the_worker(self):
        with patch('urllib.request.urlopen', return_value=reply()) as open_url:
            tts.speak('Hello.')
        self.assertEqual(json.loads(open_url.call_args.args[0].data), {'text': 'Hello.'})

    def test_voices(self):
        voices = {'voices': ['bm_george'], 'default': 'bm_george'}
        with patch('urllib.request.urlopen', return_value=reply(json.dumps(voices).encode(), 'application/json')) as open_url:
            self.assertEqual(tts.voices(), voices)
        self.assertTrue(open_url.call_args.args[0].full_url.endswith('/voices'))

    def test_connection_errors(self):
        for error in (urllib.error.URLError('refused'), ConnectionRefusedError(), TimeoutError()):
            with self.subTest(error=error), patch('urllib.request.urlopen', side_effect=error):
                for call in (lambda: tts.speak('Hello.'), tts.voices):
                    with self.assertRaisesRegex(RuntimeError, r'^Natural voice is not running\.$'):
                        call()

    def test_worker_http_errors(self):
        for status, data, expected in ((400, b'{"error":"Choose a voice."}', ValueError),
                                       (500, b'broken', RuntimeError)):
            error = urllib.error.HTTPError(tts.URL, status, 'error', {}, io.BytesIO(data))
            with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(expected):
                tts.speak('Hello.')

    def test_invalid_text_never_reaches_worker(self):
        with patch('urllib.request.urlopen') as open_url:
            for text in (None, 1, '', '  ', 'a' * 601):
                with self.assertRaises(ValueError):
                    tts.speak(text)
            open_url.assert_not_called()


class RouteTests(unittest.TestCase):
    def test_server_post_returns_wav_bytes_and_no_store(self):
        value = handler(body={'text': 'Hello.'})
        with patch('urllib.request.urlopen', return_value=reply()) as open_url:
            server.Handler.do_POST(value)
        open_url.assert_called_once()
        value.send_response.assert_called_once_with(200)
        value.send_header.assert_any_call('Cache-Control', 'no-store')
        self.assertTrue(dict(call.args for call in value.send_header.call_args_list)['Content-Type'].startswith('audio/wav'))
        self.assertEqual(value.wfile.getvalue(), audio())

    def test_worker_down_returns_503_or_empty_voices(self):
        for method, path, status in (('POST', tts.PATH, 503), ('GET', tts.PATH + '/voices', 200)):
            value = handler(path, {'text': 'Hello.'}, method)
            with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('refused')):
                getattr(server.Handler, 'do_' + method)(value)
            value.send_response.assert_called_once_with(status)
            result = json.loads(value.wfile.getvalue())
            self.assertEqual(result['error'], 'Natural voice is not running.')
            if method == 'GET':
                self.assertEqual(result['voices'], [])

    def test_invalid_json_body_and_size(self):
        for body in ([], {}, {'text': ''}, {'text': 'x' * 601}):
            value = handler(body=body)
            tts.post(value)
            value.send_response.assert_called_once_with(400)
        for length, status in (('0', 413), ('8193', 413), ('bad', 400)):
            value = handler(body={'text': 'Hello.'})
            value.headers['Content-Length'] = length
            tts.post(value)
            value.send_response.assert_called_once_with(status)
        value = handler(body={'text': 'Hello.'})
        value.rfile = io.BytesIO(b'not json')
        tts.post(value)
        value.send_response.assert_called_once_with(400)

    def test_auth_origin_and_mobile_header_precede_worker(self):
        for change in ('signed-out', 'origin', 'header'):
            value = handler(body={'text': 'Hello.'})
            if change == 'signed-out':
                value.deny.return_value = True
            elif change == 'origin':
                value.headers['Origin'] = 'https://other.test'
            else:
                del value.headers['X-Codex-Mobile']
            with patch('urllib.request.urlopen') as open_url:
                server.Handler.do_POST(value)
            value.deny.assert_called_once()
            open_url.assert_not_called()
            if change != 'signed-out':
                value.send_response.assert_called_once_with(403)
        value = handler(tts.PATH + '/voices', method='GET')
        value.deny.return_value = True
        with patch('urllib.request.urlopen') as open_url:
            server.Handler.do_GET(value)
        open_url.assert_not_called()


class WorkerTests(unittest.TestCase):
    def test_inputs_and_limits_without_loading_model(self):
        voices = ['bm_george', 'af_heart']
        self.assertEqual(tts_worker.inputs({'text': ' hello '}, voices), ('hello', 'af_heart', 1.0))
        self.assertEqual(len(tts_worker.inputs({'text': ' ' + 'a' * 600 + ' '}, voices)[0]), 600)
        for body in (None, [], {'text': ''}, {'text': ' '}, {'text': 1}, {'text': 'a' * 601},
                     {'text': 'x', 'voice': 'missing'}, {'text': 'x', 'voice': []},
                     *({'text': 'x', 'speed': speed} for speed in (None, True, 'fast', float('inf'), float('nan')))):
            with self.subTest(body=body), self.assertRaises(ValueError):
                tts_worker.inputs(body, voices)
        self.assertEqual(tts_worker.inputs({'text': 'x', 'speed': 0}, voices)[2], 0.7)
        self.assertEqual(tts_worker.inputs({'text': 'x', 'speed': 10}, voices)[2], 1.4)
        self.assertEqual(tts_worker.inputs({'text': 'x', 'speed': 10 ** 500}, voices)[2], 1.4)
        self.assertEqual(tts_worker.inputs({'text': 'x'}, voices, default='af_heart')[1], 'af_heart')

    def test_load_model_uses_env_paths_and_four_threads(self):
        runtime, kokoro = Mock(), Mock()
        with patch.dict('sys.modules', onnxruntime=runtime, kokoro_onnx=kokoro), \
                patch.dict(os.environ, TETHER_TTS_MODEL='/fake/model.onnx', TETHER_TTS_VOICES='/fake/voices.bin'):
            model = tts_worker.load_model()
        options = runtime.SessionOptions.return_value
        self.assertEqual(options.intra_op_num_threads, 4)
        self.assertEqual(options.inter_op_num_threads, 1)
        runtime.InferenceSession.assert_called_once_with('/fake/model.onnx', sess_options=options,
                                                        providers=['CPUExecutionProvider'])
        kokoro.Kokoro.from_session.assert_called_once_with(runtime.InferenceSession.return_value, '/fake/voices.bin')
        self.assertIs(model, kokoro.Kokoro.from_session.return_value)

    def speech(self):
        model = Mock()
        model.get_voices.return_value = ['bm_george', 'af_heart']
        model.create.return_value = ([0, 0.1, -0.1], 24000)
        return tts_worker.Speech(model, encode=Mock(return_value=audio()))

    def test_language_cache_and_normalized_key(self):
        speech = self.speech()
        self.assertEqual(speech.speak({'text': ' Hello. ', 'speed': 2}), audio())
        self.assertEqual(speech.speak({'text': 'Hello.', 'speed': 1.4}), audio())
        speech.model.create.assert_called_once_with('Hello.', voice='af_heart', speed=1.4, lang='en-us')
        speech.speak({'text': 'Hello.', 'voice': 'bm_george'})
        speech.model.create.assert_called_with('Hello.', voice='bm_george', speed=1.0, lang='en-gb')

    def test_lru_is_bounded_and_keeps_recent_hits(self):
        speech = self.speech()
        for at in range(64):
            speech.speak({'text': str(at)})
        speech.speak({'text': '0'})
        speech.speak({'text': '64'})
        self.assertEqual(len(speech.cache), 64)
        self.assertIn(('0', 'af_heart', 1.0), speech.cache)
        self.assertNotIn(('1', 'af_heart', 1.0), speech.cache)

    def test_synthesis_is_serial_and_duplicate_requests_share_cache(self):
        speech = self.speech()
        guard = threading.Lock()
        active = [0, 0]
        def create(*args, **kwargs):
            with guard:
                active[0] += 1
                active[1] = max(active)
            time.sleep(0.005)
            with guard:
                active[0] -= 1
            return [0], 24000
        speech.model.create.side_effect = create
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: speech.speak({'text': 'Hello.'}), range(4)))
        self.assertEqual(results, [audio()] * 4)
        self.assertEqual(active[1], 1)
        speech.model.create.assert_called_once()

    def test_worker_routes_use_fake_model(self):
        speech = self.speech()
        value = handler('/speak', {'text': 'Hello.'})
        value.server = Mock(speech=speech)
        value.respond = Mock()
        tts_worker.Handler.do_POST(value)
        value.respond.assert_called_once_with(200, audio(), 'audio/wav')
        for path, result in (('/voices', {'voices': speech.voices, 'default': speech.default}),
                             ('/health', {'ok': True})):
            value = handler(path, method='GET')
            value.server = Mock(speech=speech)
            value.respond = Mock()
            tts_worker.Handler.do_GET(value)
            value.respond.assert_called_once_with(200, result)

    def test_worker_rejects_bad_requests_before_synthesis(self):
        for body, length, status in (({'text': 'x' * 601}, None, 400), ({'text': 'Hello.'}, 'bad', 400),
                                      ({'text': 'Hello.'}, '8193', 413)):
            speech = self.speech()
            value = handler('/speak', body)
            if length:
                value.headers['Content-Length'] = length
            value.server, value.respond = Mock(speech=speech), Mock()
            tts_worker.Handler.do_POST(value)
            self.assertEqual(value.respond.call_args.args[0], status)
            speech.model.create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
