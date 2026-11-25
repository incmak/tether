import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest

import server


class FindMessagesTests(unittest.TestCase):
    messages = [{'id': '1', 'role': 'user', 'text': 'Deploy the API'}, {'id': '2', 'role': 'assistant', 'text': 'The api deploy failed: timeout'},
                {'id': '3', 'role': 'assistant', 'text': 'Unrelated'}]

    def test_every_word_must_match_and_newest_comes_first(self):
        result = server.find_messages(self.messages, 'API deploy')
        self.assertEqual([(m['id'], m['from_end']) for m in result['matches']], [('2', 2), ('1', 3)])
        self.assertEqual(result['total'], 2)

    def test_empty_query_finds_nothing(self):
        self.assertEqual(server.find_messages(self.messages, '  '), {'matches': [], 'total': 0})

    def test_snippet_starts_near_the_match(self):
        text = 'x' * 200 + ' needle here'
        snippet = server.find_messages([{'id': 'a', 'text': text}], 'needle')['matches'][0]['snippet']
        self.assertTrue(snippet.startswith('…') and 'needle here' in snippet)


if __name__ == '__main__':
    unittest.main()
