"""Signaling tests mock the upstream and never touch a terminal or worker."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error

import friday_proxy
import server


def handler(method='POST', path=friday_proxy.OFFER, body=None):
    value = server.Handler.__new__(server.Handler)
    value.path, value.command = path, method
    raw = json.dumps(body or {'sdp': 'offer', 'type': 'offer'}).encode()
    value.headers = {'Content-Length': str(len(raw)), 'Content-Type': 'application/json',
                     'Host': 'phone.test', 'Origin': 'https://phone.test', 'X-Codex-Mobile': '1'}
    value.rfile = io.BytesIO(raw)
    value.respond = Mock()
    value.deny = Mock(return_value=False)
    return value


def reply(body, status=200):
    value = Mock(status=status)
    value.read.return_value = json.dumps(body).encode()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    return value


class FridayProxyTests(unittest.TestCase):
    def test_answer_and_patch_pass_through(self):
        answer = {'sdp': 'answer', 'type': 'answer', 'pc_id': 'peer'}
        for method, body, result in [('POST', {'sdp': 'offer', 'type': 'offer'}, answer),
                                      ('PATCH', {'pc_id': 'peer', 'candidates': []}, {})]:
            value = handler(method, body=body)
            with patch('urllib.request.urlopen', return_value=reply(result)) as upstream:
                getattr(server.Handler, 'do_' + method)(value)
            value.respond.assert_called_once_with(200, result)
            req = upstream.call_args.args[0]
            self.assertEqual(req.get_method(), method)
            self.assertEqual(req.full_url, friday_proxy.URL + '/offer')
            self.assertEqual(json.loads(req.data), body)
            self.assertEqual(upstream.call_args.kwargs['timeout'], 15)

    def test_auth_and_csrf_precede_upstream(self):
        for method in ('POST', 'PATCH', 'GET'):
            for change in ('signed-out', 'origin', 'header') if method != 'GET' else ('signed-out',):
                value = handler(method, friday_proxy.AGENT if method == 'GET' else friday_proxy.OFFER)
                if change == 'signed-out':
                    value.deny.return_value = True
                elif change == 'origin':
                    value.headers['Origin'] = 'https://other.test'
                else:
                    del value.headers['X-Codex-Mobile']
                with patch('urllib.request.urlopen') as upstream:
                    getattr(server.Handler, 'do_' + method)(value)
                upstream.assert_not_called()
                value.deny.assert_called_once()
                if change != 'signed-out':
                    value.respond.assert_called_once_with(403, {'error': 'Reload the page and try again.'})

    def test_limits_and_json(self):
        for method in ('POST', 'PATCH'):
            for size, status in [('0', 413), ('65537', 413), ('-1', 413), ('bad', 400)]:
                value = handler(method)
                value.headers['Content-Length'] = size
                with patch('urllib.request.urlopen') as upstream:
                    getattr(server.Handler, 'do_' + method)(value)
                self.assertEqual(value.respond.call_args.args[0], status)
                upstream.assert_not_called()
            for raw in (b'[]', b'null', b'bad', b'\xff'):
                value = handler(method)
                value.rfile = io.BytesIO(raw)
                value.headers['Content-Length'] = str(len(raw))
                with patch('urllib.request.urlopen') as upstream:
                    getattr(server.Handler, 'do_' + method)(value)
                self.assertEqual(value.respond.call_args.args[0], 400)
                upstream.assert_not_called()
            value = handler(method)
            value.headers['Content-Type'] = 'text/plain'
            with patch('urllib.request.urlopen') as upstream:
                getattr(server.Handler, 'do_' + method)(value)
            self.assertEqual(value.respond.call_args.args[0], 415)
            upstream.assert_not_called()

    def test_worker_down(self):
        for method in ('POST', 'PATCH'):
            for error in (urllib.error.URLError('refused'), TimeoutError(), ConnectionRefusedError()):
                value = handler(method)
                with patch('urllib.request.urlopen', side_effect=error):
                    getattr(server.Handler, 'do_' + method)(value)
                value.respond.assert_called_once_with(503, {'error': 'Friday is not running.'})

    def test_health_and_fallback(self):
        healthy = {'ok': True, 'llm': False, 'stt': True, 'tts': True}
        for upstream_reply, expected in [(reply(healthy), healthy), (reply({}, 500), {'ok': False}),
                                         (reply([]), {'ok': False})]:
            value = handler('GET', friday_proxy.AGENT)
            with patch('urllib.request.urlopen', return_value=upstream_reply) as upstream:
                server.Handler.do_GET(value)
            value.respond.assert_called_once_with(200, expected)
            self.assertEqual(upstream.call_args.kwargs['timeout'], 1)
        value = handler('GET', friday_proxy.AGENT)
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('refused')):
            server.Handler.do_GET(value)
        value.respond.assert_called_once_with(200, {'ok': False})

    def test_worker_error_pass_through(self):
        value = handler('PATCH')
        error = urllib.error.HTTPError(friday_proxy.URL, 404, 'not found', {}, io.BytesIO(b'{"detail":"Peer connection not found"}'))
        with patch('urllib.request.urlopen', side_effect=error):
            server.Handler.do_PATCH(value)
        value.respond.assert_called_once_with(404, {'detail': 'Peer connection not found'})

    def test_exact_routes(self):
        self.assertTrue(friday_proxy.handles(friday_proxy.OFFER + '?x=1'))
        self.assertFalse(friday_proxy.handles('/mobile/api/live/state'))
        value = handler('PATCH', friday_proxy.AGENT)
        server.Handler.do_PATCH(value)
        self.assertEqual(value.respond.call_args.args[0], 404)


if __name__ == '__main__':
    unittest.main()
