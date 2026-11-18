"""Claude advisor transcript records as they appear in chat."""
import json
from pathlib import Path
import unittest

from activity import group_actions
from agents import claude_consume


FIXTURES = Path(__file__).parent / 'fixtures' / 'claude_2.1.327_advisor'


def records(name):
    return [json.loads(line) for line in (FIXTURES / f'{name}.jsonl').read_text().splitlines()]


def chat(*rows):
    messages = []
    for index, record in enumerate(rows):
        claude_consume(messages, index, record, lambda value: value)
    return group_actions(messages)


class ClaudeAdvisorChatTests(unittest.TestCase):
    def test_answered_keeps_its_place_before_bash(self):
        thinking, call, result, more_thinking, bash = records('answered')
        messages = chat(thinking, call, result, more_thinking, bash)
        self.assertEqual([message['kind'] for message in messages], ['advisor', 'actions'])
        item, actions = messages
        self.assertEqual(actions['tools'][0]['name'], 'Bash')
        self.assertEqual(item, {
            'id': '1.0', 'role': 'assistant', 'kind': 'advisor',
            'text': 'Asked the advisor · answered', 'time': call['timestamp'],
            'advisor': {
                'id': call['message']['content'][0]['id'], 'model': 'claude-fable-5-2',
                'status': 'answered', 'started': call['timestamp'], 'ended': result['timestamp'],
            },
        })
        output = json.dumps(messages)
        self.assertNotIn('encrypted', output)
        self.assertNotIn('REDACTED', output)

    def test_error(self):
        call, result = records('error')
        item, = chat(call, result)
        self.assertEqual(item['text'], 'Asked the advisor · no answer')
        self.assertEqual(item['advisor']['status'], 'error')
        self.assertEqual(item['advisor']['error'], 'too_many_requests')
        self.assertEqual(item['advisor']['ended'], result['timestamp'])

    def test_pending_and_model_fallback(self):
        call, _ = records('error')
        self.assertEqual(call['advisorModel'], 'claude-fable-5-2')  # real 2.1.327 records carry it
        item, = chat(call)
        self.assertEqual(item['text'], 'Asked the advisor')
        self.assertEqual(item['advisor']['status'], 'pending')
        self.assertEqual(item['advisor']['model'], 'claude-fable-5-2')
        del call['advisorModel']
        self.assertEqual(chat(call)[0]['advisor']['model'], '')
        self.assertNotIn('ended', item['advisor'])
        call['advisorModel'] = 'claude-opus-5-5'
        fallback, = chat(call)
        self.assertEqual(fallback['advisor']['model'], 'claude-opus-5-5')

    def test_orphan_result(self):
        _, result = records('error')
        self.assertEqual(chat(result), [])

    def test_unknown_result_type_and_malformed_content(self):
        call, result = records('error')
        block = result['message']['content'][0]
        for content, expected in [({'type': 'new_reply'}, 'new_reply'),
                                  ({'type': 'unsafe!'}, 'unknown'), (None, 'unknown')]:
            with self.subTest(content=content):
                block['content'] = content
                item, = chat(call, result)
                self.assertEqual(item['text'], 'Asked the advisor · reply in an unknown form')
                self.assertEqual(item['advisor']['status'], 'unknown')
                self.assertEqual(item['advisor']['result_type'], expected)

    def test_error_codes_must_be_safe_and_bounded(self):
        call, result = records('error')
        content = result['message']['content'][0]['content']
        for code in ['bad error', 'x' * 81, 123]:
            with self.subTest(code=code):
                content['error_code'] = code
                item, = chat(call, result)
                self.assertEqual(item['advisor']['error'], 'unknown')

    def test_unrelated_server_tool(self):
        call, _ = records('error')
        call['message']['content'][0]['name'] = 'another_server_tool'
        self.assertEqual(chat(call), [])

    def test_multiple_calls_match_by_id_and_keep_block_order(self):
        _, call, result, _, bash = records('answered')
        call['advisorModel'] = 'claude-opus-5-5'
        first, = call['message']['content']
        second = dict(first, id='srvtoolu_second')
        call['message']['content'] = [first, *bash['message']['content'], second, first]
        older, actions, other, matched = chat(call, result)
        self.assertEqual([item['id'] for item in (older, actions, other, matched)],
                         ['0.0', '0.1', '0.2', '0.3'])
        self.assertEqual(older['advisor']['status'], 'pending')
        self.assertEqual(other['advisor']['status'], 'pending')
        self.assertEqual(matched['advisor']['status'], 'answered')
        self.assertEqual(matched['advisor']['model'], 'claude-fable-5-2')

    def test_missing_ids(self):
        call, result = records('error')
        del result['message']['content'][0]['tool_use_id']
        item, = chat(call, result)
        self.assertEqual(item['advisor']['status'], 'pending')
        del call['message']['content'][0]['id']
        self.assertEqual(chat(call, result), [])
