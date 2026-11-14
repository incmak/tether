"""Chat version and stream `have` tests (B63) use a mocked handler: no sockets, no terminals."""
import io
import json
import unittest
from unittest.mock import Mock, patch

import server


def payload_for(text):
    return {'state': 'Idle', 'messages': [{'text': text}]}


class StreamVersionTests(unittest.TestCase):
    def stream(self, have, texts):
        """Run stream_chat with chat payloads from texts (the last one repeats). Return the raw event text."""
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        texts = iter(texts + [texts[-1]] * 1000)
        clock, ticks = iter(range(1000)), iter(range(1000))
        with patch.object(server, 'choose', return_value={'id': 'p1', 'agent': 'codex'}), \
                patch.object(server, 'change_signature', side_effect=lambda config: next(clock)), \
                patch.object(server, 'capture', return_value='screen'), \
                patch.object(server, 'chat_payload', side_effect=lambda *a, **k: payload_for(next(texts))), \
                patch.object(server.time, 'monotonic', side_effect=lambda: next(ticks) * 0.5), \
                patch.object(server.time, 'sleep'):
            handler.stream_chat('p1', False, lifetime=10, have=have)
        return handler.wfile.getvalue().decode()

    def events(self, text):
        return [block for block in text.split('\n\n') if block and not block.startswith('retry')]

    def test_version_is_a_stable_hash_of_the_payload(self):
        one, two = server.with_version(payload_for('a')), server.with_version(payload_for('a'))
        self.assertEqual(one['version'], two['version'])
        self.assertEqual(len(one['version']), 20)
        self.assertNotEqual(one['version'], server.with_version(payload_for('b'))['version'])

    def test_matching_have_sends_same_then_only_real_changes(self):
        have = server.with_version(payload_for('a'))['version']
        events = self.events(self.stream(have, ['a', 'a', 'b']))
        self.assertEqual(events[0], 'event: same\ndata: ' + json.dumps({'version': have}))
        data = [json.loads(e[6:]) for e in events if e.startswith('data: {"')]
        self.assertEqual([d['messages'][0]['text'] for d in data], ['b'])  # the unchanged 'a' is never sent
        self.assertEqual(data[0]['version'], server.with_version(payload_for('b'))['version'])

    def test_other_or_missing_have_sends_the_full_payload(self):
        for have in ('', 'old-version', server.with_version(payload_for('z'))['version']):
            with self.subTest(have=have):
                events = self.events(self.stream(have, ['a']))
                self.assertTrue(events[0].startswith('data: {"'), events[0])
                first = json.loads(events[0][6:])
                self.assertEqual(first['version'], server.with_version(payload_for('a'))['version'])
                self.assertNotIn('event: same', self.stream(have, ['a']))

    def test_chat_route_and_stream_give_the_same_version(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/mobile/api/chat?session=p1&limit=40&preview=1&watch=1'
        handler.headers = {}
        handler.respond = Mock()
        handler.deny = Mock(return_value=False)
        with patch.object(server, 'choose', return_value={'id': 'p1', 'agent': 'codex'}), \
                patch.object(server, 'chat_payload', return_value=payload_for('a')):
            handler.do_GET()
        status, body = handler.respond.call_args.args[:2]
        self.assertEqual(status, 200)
        events = self.events(self.stream(body['version'], ['a']))
        self.assertEqual(events[0], 'event: same\ndata: ' + json.dumps({'version': body['version']}))


if __name__ == '__main__':
    unittest.main()
