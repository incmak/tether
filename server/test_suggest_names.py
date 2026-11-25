"""Tests for name suggestions: request checks, cleaning, the agent → local model → words order, and the route."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import server
import suggest_names
from test_tts import handler


class SuggestNamesTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = self.enterContext(patch('urllib.request.urlopen', side_effect=TimeoutError()))
        self.which = self.enterContext(patch('suggest_names.shutil.which', return_value=None))
        self.agent = self.enterContext(patch('suggest_names.agent_output'))

    def reply(self, output):
        self.urlopen.side_effect = None
        self.urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            {'choices': [{'message': {'content': output}}]}).encode()

    def test_request_rejects_bad_kinds_lists_and_types(self):
        for body in (None, [], {}, {'kind': 'group'}, {'kind': 'tab', 'tabs': 'x'}, {'kind': 'tab', 'tabs': [1]},
                     {'kind': 'tab', 'taken': ['x'] * 201}, {'kind': 'tab', 'tabs': ['x'] * 31},
                     {'kind': 'tab', 'agent': 3}, {'kind': 'tab', 'task': 5}, {'kind': 'tab', 'task': 'x' * 20001},
                     {'kind': 'tab', 'folder': 'x' * 1025}):
            with self.assertRaises(ValueError):
                suggest_names.request(body)

    def test_request_cleans_control_characters_and_clips(self):
        info = suggest_names.request({'kind': 'stack', 'task': 'Fix\x00 the\n\nlogin ' + 'x' * 2000,
                                      'tabs': [' ', 'Build\tUI'] + ['t'] * 20, 'taken': [' Main ']})
        self.assertTrue(info['task'].startswith('Fix the login '))
        self.assertEqual(len(info['task']), 1200)
        self.assertEqual(info['tabs'][0], 'Build UI')
        self.assertEqual(len(info['tabs']), 12)
        self.assertEqual(info['taken'], ['main'])

    def test_clean_rules_per_kind(self):
        self.assertEqual(suggest_names.clean('1. "Login fix."', 'tab'), 'Login fix')
        self.assertEqual(suggest_names.clean('- Auth: rework!', 'session'), 'Auth rework')
        self.assertEqual(suggest_names.clean('Unstacked', 'stack'), '')
        self.assertEqual(suggest_names.clean('main', 'stack'), '')
        self.assertEqual(suggest_names.clean('Main', 'tab'), 'Main')
        self.assertEqual(suggest_names.clean('', 'tab'), '')
        self.assertEqual(suggest_names.clean('!!!', 'session'), '')
        self.assertEqual(len(suggest_names.clean('a' * 100, 'stack')), 40)

    def test_pick_skips_taken_and_duplicates_and_stops_at_three(self):
        info = suggest_names.request({'kind': 'tab', 'taken': ['Old']})
        self.assertEqual(suggest_names.pick(['old', 'One', 'one', 'Two', 'Three', 'Four'], info),
                         ['One', 'Two', 'Three'])

    def test_empty_input_uses_numbered_name_without_models(self):
        self.assertEqual(suggest_names.suggest({'kind': 'session', 'taken': ['chat 2']}),
                         {'names': ['chat 3'], 'source': 'words'})
        self.assertEqual(suggest_names.suggest({'kind': 'stack'}), {'names': ['Stack 2'], 'source': 'words'})
        self.urlopen.assert_not_called()
        self.agent.assert_not_called()

    def test_words_fallback_uses_task_folder_and_tabs(self):
        value = suggest_names.suggest({'kind': 'stack', 'task': 'Please fix the login redirect bug\nmore',
                                       'folder': '~/apps/tether', 'tabs': ['Review']})
        self.assertEqual(value, {'names': ['Fix login redirect', 'Fix login', 'tether'], 'source': 'words'})

    def test_local_model_names_are_cleaned_and_used(self):
        self.reply('1. Login fix\n2. Unstacked\n3. Auth redirect\n4. Session work')
        value = suggest_names.suggest({'kind': 'stack', 'task': 'fix login'})
        self.assertEqual(value, {'names': ['Login fix', 'Auth redirect', 'Session work'], 'source': 'llm'})
        payload = json.loads(self.urlopen.call_args.args[0].data)
        self.assertIn('Task: fix login', payload['messages'][1]['content'])
        self.assertEqual(self.urlopen.call_args.kwargs['timeout'], suggest_names.MODEL_TIMEOUT)

    def test_agent_is_used_first_then_local_model_on_failure(self):
        self.which.side_effect = lambda name: '/bin/' + name if name == 'codex' else None
        self.agent.return_value = 'Login fix\nAuth'
        self.assertEqual(suggest_names.suggest({'kind': 'tab', 'task': 'fix login', 'agent': 'codex'}),
                         {'names': ['Login fix', 'Auth'], 'source': 'codex'})
        self.assertEqual(self.agent.call_args.args[:2], ('codex', '/bin/codex'))
        self.urlopen.assert_not_called()
        for error in (subprocess.TimeoutExpired('codex', 30), OSError('x'), None):
            self.agent.side_effect = error
            self.agent.return_value = ''
            self.reply('Model name')
            self.assertEqual(suggest_names.suggest({'kind': 'tab', 'task': 'fix', 'agent': 'auto'})['source'], 'llm')

    def test_unknown_or_missing_agent_skips_to_local_model(self):
        self.reply('Name')
        for agent in (None, 'gemini', 'claude'):
            self.assertEqual(suggest_names.suggest({'kind': 'tab', 'task': 'fix', 'agent': agent})['source'], 'llm')
        self.agent.assert_not_called()

    def test_model_errors_use_words(self):
        for output in ('', '!!!\n', None):
            if output is None:
                self.urlopen.side_effect = OSError('down')
            else:
                self.reply(output)
            self.assertEqual(suggest_names.suggest({'kind': 'session', 'task': 'fix login'}),
                             {'names': ['Fix login'], 'source': 'words'})


class SuggestNamesRouteTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = self.enterContext(patch('urllib.request.urlopen', side_effect=TimeoutError()))
        self.enterContext(patch('suggest_names.shutil.which', return_value=None))

    def test_route_returns_names_and_checks_origin(self):
        value = handler(suggest_names.PATH, {'kind': 'tab', 'task': 'fix login'})
        server.Handler.do_POST(value)
        value.deny.assert_called_once_with()
        value.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(value.wfile.getvalue()), {'names': ['Fix login'], 'source': 'words'})
        for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
            value = handler(suggest_names.PATH, {'kind': 'tab', 'task': 'fix'})
            value.headers.update(headers)
            server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(403)

    def test_bad_bodies_and_lengths_return_400(self):
        for body in (None, {}, {'kind': 'x'}):
            value = handler(suggest_names.PATH, body)
            server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(400)
        value = handler(suggest_names.PATH, {'kind': 'tab'})
        value.headers['Content-Length'] = str(suggest_names.BODY_LIMIT + 1)
        server.Handler.do_POST(value)
        value.send_response.assert_called_once_with(400)
        value = handler(suggest_names.PATH, {'kind': 'tab'})
        value.rfile = io.BytesIO(b'not JSON')
        server.Handler.do_POST(value)
        value.send_response.assert_called_once_with(400)


if __name__ == '__main__':
    unittest.main()
