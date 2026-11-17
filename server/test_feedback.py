import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import feedback


class Handler:
    def __init__(self, path, body=b''):
        self.path = path
        self.headers = {'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)
        self.status = None
        self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(mock.patch.object(feedback.paths, 'CONFIG', Path(folder.name)))

    def post(self, body):
        handler = Handler(feedback.PATH, json.dumps(body).encode())
        feedback.post(handler)
        return handler

    def get(self, thread):
        handler = Handler(f'{feedback.PATH}?thread={thread}')
        feedback.get(handler)
        return handler

    def test_save_change_and_remove(self):
        self.assertEqual(self.post({'thread': 't1', 'message': 'm1', 'rating': 'up'}).body, {'saved': {'rating': 'up', 'note': ''}})
        self.assertEqual(self.get('t1').body, {'ratings': {'m1': {'rating': 'up', 'note': ''}}})
        self.post({'thread': 't1', 'message': 'm1', 'rating': 'down', 'note': ' Too long ', 'excerpt': 'The numbers add up'})
        self.assertEqual(self.get('t1').body['ratings']['m1'], {'rating': 'down', 'note': 'Too long'})
        stored = json.loads(feedback.file().read_text())
        self.assertEqual(stored['t1']['m1']['excerpt'], 'The numbers add up')
        self.assertEqual(self.post({'thread': 't1', 'message': 'm1', 'rating': ''}).body, {'saved': None})
        self.assertEqual(self.get('t1').body, {'ratings': {}})
        self.assertEqual(json.loads(feedback.file().read_text()), {})

    def test_threads_are_separate(self):
        self.post({'thread': 't1', 'message': 'm1', 'rating': 'up'})
        self.post({'thread': 't2', 'message': 'm1', 'rating': 'down'})
        self.assertEqual(self.get('t1').body['ratings']['m1']['rating'], 'up')
        self.assertEqual(self.get('t2').body['ratings']['m1']['rating'], 'down')

    def test_rejects_bad_input(self):
        for body in ({'thread': 't1', 'message': 'm1', 'rating': 'meh'}, {'thread': '../x', 'message': 'm1', 'rating': 'up'},
                     {'thread': 't1', 'message': '', 'rating': 'up'}, {'thread': 't1', 'message': 'a b', 'rating': 'up'}, []):
            self.assertEqual(self.post(body).status, 400, body)
        handler = Handler(feedback.PATH, b'not json')
        feedback.post(handler)
        self.assertEqual(handler.status, 400)
        self.assertEqual(self.get('').status, 400)
        self.assertFalse(feedback.file().exists())

    def test_long_note_is_cut(self):
        self.post({'thread': 't1', 'message': 'm1', 'rating': 'down', 'note': 'x' * 5000})
        self.assertEqual(len(self.get('t1').body['ratings']['m1']['note']), feedback.NOTE)

    def test_oldest_rows_drop_past_the_limit(self):
        with mock.patch.object(feedback, 'MAX_ROWS', 3):
            for index in range(5):
                feedback.rate('t1', f'm{index}', 'up', now=1000 + index)
        self.assertEqual(sorted(feedback.ratings('t1')), ['m2', 'm3', 'm4'])

    def test_broken_file_reads_as_empty(self):
        feedback.file().write_text('{oops')
        self.assertEqual(feedback.ratings('t1'), {})
        feedback.rate('t1', 'm1', 'up')
        self.assertEqual(feedback.ratings('t1'), {'m1': {'rating': 'up', 'note': ''}})


if __name__ == '__main__':
    unittest.main()
