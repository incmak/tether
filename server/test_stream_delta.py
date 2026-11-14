"""Delta chat stream tests (B66) use a mocked handler: no sockets, no terminals."""
import copy
import io
import json
import random
import unittest
from unittest.mock import Mock, patch

import server


def apply(shown, delta):
    """The same rules as applyDelta in ui/src/hooks.ts."""
    if shown.get('version') != delta['base']:
        return None
    result = dict(shown)
    for key in delta['unset']:
        result.pop(key, None)
    result.update(delta['set'])
    if 'messages' in delta:
        part = delta['messages']
        if part['drop'] + part['keep'] > len(shown['messages']):
            return None
        result['messages'] = shown['messages'][part['drop']:part['drop'] + part['keep']] + part['tail']
    result['version'] = delta['version']
    return result


def message(n, text=None):
    return {'id': f'm{n}', 'role': 'assistant' if n % 2 else 'user', 'text': text or f'message {n} ' + 'x' * 200}


def chat(first, last, state='Idle', **extra):
    return dict({'state': state, 'earlier': first, 'messages': [message(n) for n in range(first, last)]}, **extra)


class ChatDeltaTests(unittest.TestCase):
    def check(self, old, new):
        old, new = server.with_version(copy.deepcopy(old)), server.with_version(copy.deepcopy(new))
        delta = json.loads(json.dumps(server.chat_delta(old, new)))
        result = apply(json.loads(json.dumps(old)), delta)
        self.assertEqual(json.dumps(result, sort_keys=True), json.dumps(new, sort_keys=True))  # exact: == treats True as 1
        return delta

    def test_window_slide_sends_only_the_new_message(self):
        delta = self.check(chat(0, 40), chat(1, 41))
        self.assertEqual(delta['messages'], {'drop': 1, 'keep': 39, 'tail': [message(40)]})
        self.assertEqual(delta['set'], {'earlier': 1})
        self.assertEqual(delta['unset'], [])

    def test_growing_last_message_and_state_change(self):
        new = chat(0, 40, state='Working')
        new['messages'][-1] = message(39, 'longer text')
        delta = self.check(chat(0, 40), new)
        self.assertEqual(delta['messages'], {'drop': 0, 'keep': 39, 'tail': [message(39, 'longer text')]})
        self.assertEqual(delta['set'], {'state': 'Working'})

    def test_preview_only_change_has_no_messages(self):
        delta = self.check(chat(0, 40, state='Working', preview={'text': 'a'}), chat(0, 40, state='Working', preview={'text': 'ab'}))
        self.assertNotIn('messages', delta)
        self.assertEqual(delta['set'], {'preview': {'text': 'ab'}})

    def test_removed_keys_and_cleared_or_replaced_lists(self):
        self.assertEqual(self.check(chat(0, 5, approval={'a': 1}), chat(0, 5))['unset'], ['approval'])
        self.assertEqual(self.check(chat(0, 5), chat(0, 0))['messages'], {'drop': 5, 'keep': 0, 'tail': []})
        self.assertEqual(self.check(chat(0, 5), chat(9, 12))['messages']['keep'], 0)
        self.check({'state': 'Shell', 'messages': None}, chat(0, 3))  # not a list before: sent whole in `set`
        self.check(chat(0, 3), {'state': 'Shell'})

    def test_bool_and_number_are_different_values(self):
        delta = self.check(chat(0, 3, busy=True, count=1), chat(0, 3, busy=1, count=True))
        self.assertEqual(delta['set'], {'busy': 1, 'count': True})
        old, new = chat(0, 3), chat(0, 3)
        old['messages'][-1]['done'], new['messages'][-1]['done'] = 1, True
        self.assertEqual(self.check(old, new)['messages']['keep'], 2)

    def test_random_changes_round_trip(self):
        rng = random.Random(66)
        for _ in range(300):
            old = chat(rng.randrange(0, 20), rng.randrange(20, 60), state=rng.choice(['Idle', 'Working']))
            first = old['earlier'] + rng.randrange(0, 5)
            new = chat(first, first + rng.randrange(0, 60), state=rng.choice(['Idle', 'Working']))
            if new['messages'] and rng.random() < .5:
                new['messages'][rng.randrange(len(new['messages']))]['text'] = 'edited'
            if rng.random() < .3:
                new['messages'].insert(0, message(0))  # a duplicate of an older message
            if rng.random() < .3:
                new['preview'] = {'text': str(rng.random())}
            self.check(old, new)

    def test_wrong_base_does_not_apply(self):
        old, new = server.with_version(chat(0, 3)), server.with_version(chat(1, 4))
        self.assertIsNone(apply(dict(old, version='other'), server.chat_delta(old, new)))


class StreamDeltaTests(unittest.TestCase):
    def stream(self, payloads, deltas=True, have=''):
        """Run stream_chat with the given chat payloads (the last one repeats). Return the events."""
        items = iter(payloads + [payloads[-1]] * 1000)
        return self.stream_with(lambda *a, **k: copy.deepcopy(next(items)), deltas, have)

    def stream_with(self, chat_payload, deltas=True, have=''):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        clock, ticks = iter(range(1000)), iter(range(1000))
        with patch.object(server, 'choose', return_value={'id': 'p1', 'agent': 'codex'}), \
                patch.object(server, 'change_signature', side_effect=lambda config: next(clock)), \
                patch.object(server, 'capture', return_value='screen'), \
                patch.object(server, 'chat_payload', side_effect=chat_payload), \
                patch.object(server.time, 'monotonic', side_effect=lambda: next(ticks) * 0.5), \
                patch.object(server.time, 'sleep'):
            handler.stream_chat('p1', False, lifetime=10, have=have, deltas=deltas)
        text = handler.wfile.getvalue().decode()
        return [block for block in text.split('\n\n') if block and not block.startswith('retry') and 'event: idle' not in block]

    def replay(self, events, shown=None):
        """Apply events the way the UI does. Return the final data and the kinds seen."""
        kinds = []
        for event in events:
            if event.startswith('data: '):
                shown = json.loads(event[6:]); kinds.append('full')
            elif event.startswith('event: delta\ndata: '):
                shown = apply(shown, json.loads(event[len('event: delta\ndata: '):])); kinds.append('delta')
                self.assertIsNotNone(shown)
            elif event.startswith('event: same\n'):
                kinds.append('same')
        return shown, kinds

    def test_first_event_is_full_then_deltas_rebuild_each_payload(self):
        payloads = [chat(0, 40), chat(0, 40, state='Working'), chat(1, 41, state='Working'), chat(1, 41)]
        events = self.stream(payloads)
        shown, kinds = self.replay(events)
        self.assertEqual(kinds, ['full', 'delta', 'delta', 'delta'])
        self.assertEqual(shown, json.loads(json.dumps(server.with_version(chat(1, 41)))))
        full = len(events[0])
        self.assertTrue(all(len(event) < full / 5 for event in events[1:]), [len(event) for event in events])

    def test_same_then_delta_uses_the_shown_version_as_base(self):
        shown = json.loads(json.dumps(server.with_version(chat(0, 40))))
        events = self.stream([chat(0, 40), chat(1, 41)], have=shown['version'])
        final, kinds = self.replay(events, shown)
        self.assertEqual(kinds, ['same', 'delta'])
        self.assertEqual(final['version'], server.with_version(chat(1, 41))['version'])

    def test_without_opt_in_every_event_is_a_full_payload(self):
        _, kinds = self.replay(self.stream([chat(0, 40), chat(1, 41), chat(2, 42)], deltas=False))
        self.assertEqual(kinds, ['full', 'full', 'full'])

    def test_full_payload_when_the_delta_is_not_shorter(self):
        _, kinds = self.replay(self.stream([chat(0, 3), chat(10, 13)]))
        self.assertEqual(kinds, ['full', 'full'])

    def test_payload_changed_in_place_after_sending_still_gives_a_true_delta(self):
        shared = chat(0, 10)
        expected = chat(0, 10, state='Working')
        expected['messages'][-1]['text'] = 'edited later'
        calls = []

        def next_payload(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return shared
            shared['messages'][-1]['text'] = 'edited later'  # a cache object changed after it was sent
            return copy.deepcopy(expected)
        events = self.stream_with(next_payload)
        final, kinds = self.replay(events)
        self.assertEqual(kinds[:2], ['full', 'delta'])
        self.assertEqual({k: v for k, v in final.items() if k != 'version'}, expected)

    def test_message_changed_during_hashing_cannot_split_version_and_base(self):
        """Review finding: hashing releases the GIL, so a cache writer can change a message between the hash and the
        body. Client holds A; A's hash is taken while the message turns into B; the next payload is B plus a state."""
        shared = chat(0, 10)
        shown = server.with_version(copy.deepcopy(shared))
        real_sha1, armed = server.hashlib.sha1, [True]

        def racing_sha1(data):
            if armed[0]:
                armed[0] = False
                shared['messages'][-1]['text'] = 'B'  # the writer runs while the hash is computed
            return real_sha1(data)
        later = chat(0, 10, state='Working')
        later['messages'][-1]['text'] = 'B'
        calls = []

        def next_payload(*args, **kwargs):
            calls.append(1)
            return shared if len(calls) == 1 else copy.deepcopy(later)
        with patch.object(server.hashlib, 'sha1', side_effect=racing_sha1):
            events = self.stream_with(next_payload, have=shown['version'])
        final, kinds = self.replay(events, json.loads(json.dumps(shown)))
        self.assertEqual(kinds[0], 'same')
        self.assertEqual({k: v for k, v in final.items() if k != 'version'}, later)
        self.assertEqual(final['version'], server.with_version(copy.deepcopy(later))['version'])

    def test_version_matches_the_returned_body_even_if_the_source_changes_while_hashing(self):
        source = chat(0, 3)
        real_sha1 = server.hashlib.sha1

        def racing_sha1(data):
            source['messages'][0]['text'] = 'changed'
            return real_sha1(data)
        with patch.object(server.hashlib, 'sha1', side_effect=racing_sha1):
            frozen = server.with_version(source)
        body = {k: v for k, v in frozen.items() if k != 'version'}
        self.assertEqual(frozen['version'], real_sha1(json.dumps(body).encode()).hexdigest()[:20])
        self.assertNotEqual(body['messages'][0]['text'], 'changed')

    def test_route_opts_in_with_delta_1(self):
        for query, expected in (('&delta=1', True), ('', False), ('&delta=0', False)):
            handler = server.Handler.__new__(server.Handler)
            handler.path = '/mobile/api/stream?session=p1&have=v1' + query
            handler.headers = {}
            handler.deny = Mock(return_value=False)
            handler.stream_chat = Mock()
            handler.do_GET()
            self.assertEqual(handler.stream_chat.call_args.kwargs['deltas'], expected, query)
            self.assertEqual(handler.stream_chat.call_args.kwargs['have'], 'v1')


if __name__ == '__main__':
    unittest.main()
