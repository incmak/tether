"""Group suggestions use fake processes, HTTP responses and socket-free route fixtures."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import auth
import server
import suggest_groups
from test_tts import handler


def session(identifier, folder='/work/project', **extra):
    return dict({'id': identifier, 'name': 'Session', 'dir': folder, 'chats': ['First chat']}, **extra)


class SuggestGroupsTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = self.enterContext(patch('urllib.request.urlopen'))
        self.which = self.enterContext(patch('suggest_groups.shutil.which', return_value=None))
        self.popen = self.enterContext(patch('suggest_groups.subprocess.Popen'))
        self.fast_model = self.enterContext(patch('suggest_groups.codex_fast_model', return_value=None))
        self.entries = [session('$6'), session('$9', '/other/project/'), session('$12', '/work/single')]

    def reply(self, output):
        self.urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            {'choices': [{'message': {'content': output}}]}).encode()

    def suggest(self, groups, entries=None):
        self.reply(json.dumps({'groups': groups}))
        return suggest_groups.suggest({'sessions': self.entries if entries is None else entries})

    def agent_process(self, command, **kwargs):
        folder = Path(kwargs['cwd'])
        self.assertTrue(folder.is_dir())
        self.assertEqual(list(folder.iterdir()), [])
        if '-o' in command:
            outfile = Path(command[command.index('-o') + 1])
            self.assertEqual(outfile.parent, folder)
            outfile.write_text('Project: 6 9', encoding='utf-8')
        return Mock(returncode=0, pid=12345, communicate=Mock(return_value=('Project: 6 9', None)))

    def test_no_main_agent_setting_skips_agents(self):
        for extra in ({}, {'agent': None}, {'agent': ''}, {'agent': 'unknown'}):
            with self.subTest(extra=extra):
                self.reply('Project: 6 9')
                self.assertEqual(suggest_groups.suggest({'sessions': self.entries, **extra})['source'], 'llm')
        self.which.assert_not_called()
        self.popen.assert_not_called()

    def test_auto_uses_first_installed_agent_else_local_model(self):
        for installed, expected in ((('claude', 'codex'), 'claude'), (('codex',), 'codex'), ((), 'llm')):
            with self.subTest(installed=installed):
                self.popen.reset_mock()
                self.which.side_effect = lambda name: '/bin/' + name if name in installed else None
                self.popen.side_effect = self.agent_process
                self.reply('Project: 6 9')
                self.assertEqual(suggest_groups.suggest({'sessions': self.entries, 'agent': 'auto'})['source'], expected)
                self.assertEqual(self.popen.call_count, 1 if installed else 0)

    def test_claude_agent_uses_exact_command(self):
        for extra in ({'agent': 'claude'},):
            with self.subTest(extra=extra):
                self.which.reset_mock()
                self.popen.reset_mock()
                self.which.return_value = '/bin/claude'
                self.popen.side_effect = self.agent_process
                result = suggest_groups.suggest({'sessions': self.entries, **extra})
                self.assertEqual(result, {'groups': [{'name': 'Project', 'sessions': ['$6', '$9']},
                                                    {'name': 'Other', 'sessions': ['$12']}], 'source': 'claude'})
                self.which.assert_called_once_with('claude')
                self.popen.assert_called_once()
                command = self.popen.call_args.args[0]
                folder = self.popen.call_args.kwargs['cwd']
                self.assertEqual(command, ['/bin/claude', '-p', '--model', 'haiku', '--tools', '',
                                           '--setting-sources', '', '--strict-mcp-config',
                                           '--no-session-persistence', '--system-prompt', suggest_groups.PROMPT])
                self.assertEqual(self.popen.call_args.kwargs,
                                 dict(cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, start_new_session=True))
                self.assertFalse(Path(folder).exists())
        self.urlopen.assert_not_called()

    def test_claude_receives_session_data_as_text_stdin(self):
        self.which.return_value = '/bin/claude'
        process = Mock(returncode=0, communicate=Mock(return_value=('Project: 6 9', None)))
        self.popen.return_value = process
        self.entries[0]['name'] = '$(touch nope); `whoami`'
        suggest_groups.suggest({'sessions': self.entries, 'agent': 'claude'})
        process.communicate.assert_called_once_with(input=suggest_groups.model_input(self.entries), timeout=45)
        self.assertEqual(suggest_groups.AGENT_TIMEOUT, 45)
        self.assertNotIn(self.entries[0]['name'], self.popen.call_args.args[0])

    def test_codex_command_uses_devnull_and_reads_output_file(self):
        self.which.return_value = '/bin/codex'
        # stdout must be ignored: Codex writes the answer to the -o file.
        process = Mock(returncode=0, communicate=Mock(return_value=(None, None)))

        def launch(command, **kwargs):
            self.agent_process(command, **kwargs)
            return process

        self.popen.side_effect = launch
        result = suggest_groups.suggest({'sessions': self.entries, 'agent': 'codex'})
        self.assertEqual(result, {'groups': [{'name': 'Project', 'sessions': ['$6', '$9']},
                                            {'name': 'Other', 'sessions': ['$12']}], 'source': 'codex'})
        self.which.assert_called_once_with('codex')
        self.popen.assert_called_once()
        folder = self.popen.call_args.kwargs['cwd']
        self.assertEqual(self.popen.call_args.args[0],
                         ['/bin/codex', 'exec', '--skip-git-repo-check', '--ephemeral', '-s', 'read-only',
                          '-c', 'model_reasoning_effort=low', '-o', str(Path(folder) / 'groups.txt'), '--',
                          suggest_groups.PROMPT + '\n\n' + suggest_groups.model_input(self.entries)])
        self.assertEqual(self.popen.call_args.kwargs,
                         dict(cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, text=True, start_new_session=True))
        process.communicate.assert_called_once_with(input=None, timeout=45)
        self.assertFalse(Path(folder).exists())
        self.urlopen.assert_not_called()

    def test_main_agent_not_installed_uses_local_model(self):
        for main in ('claude', 'codex'):
            with self.subTest(main=main):
                self.which.reset_mock()
                self.reply('Project: 6 9')
                self.assertEqual(suggest_groups.suggest({'sessions': self.entries, 'agent': main})['source'], 'llm')
                self.which.assert_called_once_with(main)
        self.popen.assert_not_called()

    def test_codex_command_uses_luna_model_when_available(self):
        self.fast_model.return_value = 'gpt-6-luna'
        self.popen.side_effect = self.agent_process
        suggest_groups.agent_output('codex', '/bin/codex', self.entries)
        folder = self.popen.call_args.kwargs['cwd']
        self.assertEqual(self.popen.call_args.args[0],
                         ['/bin/codex', 'exec', '--skip-git-repo-check', '--ephemeral', '-s', 'read-only',
                          '-m', 'gpt-6-luna', '-c', 'model_reasoning_effort=low',
                          '-o', str(Path(folder) / 'groups.txt'), '--',
                          suggest_groups.PROMPT + '\n\n' + suggest_groups.model_input(self.entries)])
        self.fast_model.assert_called_once_with()
        self.urlopen.assert_not_called()

    def test_agent_timeout_kills_process_group_then_uses_local_model(self):
        for agent in ('claude', 'codex'):
            for missing_process in (False, True):
                with self.subTest(agent=agent, missing_process=missing_process):
                    self.which.reset_mock()
                    self.which.return_value = '/bin/' + agent
                    process = Mock(pid=12345)
                    process.communicate.side_effect = subprocess.TimeoutExpired(agent, 45)
                    self.popen.return_value = process
                    self.reply('Project: 6 9')
                    with patch('suggest_groups.os.killpg',
                               side_effect=ProcessLookupError() if missing_process else None) as killpg:
                        result = suggest_groups.suggest({'sessions': self.entries, 'agent': agent})
                    self.assertEqual(result['source'], 'llm')
                    killpg.assert_called_once_with(12345, signal.SIGKILL)
                    process.wait.assert_called_once_with()
                    self.which.assert_called_once_with(agent)
                    self.assertFalse(Path(self.popen.call_args.kwargs['cwd']).exists())

    def test_agent_failures_use_local_model_without_trying_other_agent(self):
        self.which.return_value = '/bin/claude'
        for output, code in (('Project: 6 9', 1), ('broken', 0), ('{"groups":null}', 0),
                             ('One: 6\nTwo: 9\nThree: 12', 0)):
            with self.subTest(output=output, code=code):
                self.which.reset_mock()
                self.popen.return_value = Mock(returncode=code, communicate=Mock(return_value=(output, None)))
                self.reply('Local: 6 9')
                result = suggest_groups.suggest({'sessions': self.entries, 'agent': 'claude'})
                self.assertEqual(result['source'], 'llm')
                self.assertEqual(result['groups'][0]['name'], 'Local')
                self.which.assert_called_once_with('claude')
        self.popen.side_effect = OSError('could not start')
        self.assertEqual(suggest_groups.suggest({'sessions': self.entries, 'agent': 'claude'})['source'], 'llm')

    def test_agent_singletons_then_local_singletons_use_folders(self):
        self.which.return_value = '/bin/claude'
        output = 'One: 6\nTwo: 9\nThree: 12'
        self.popen.return_value = Mock(returncode=0, communicate=Mock(return_value=(output, None)))
        self.reply(output)
        self.assertEqual(suggest_groups.suggest({'sessions': self.entries, 'agent': 'claude'}),
                         {'groups': [{'name': 'project', 'sessions': ['$6', '$9']},
                                     {'name': 'Other', 'sessions': ['$12']}], 'source': 'folder'})
        self.popen.assert_called_once()
        self.urlopen.assert_called_once()

    def test_non_string_agent_is_rejected_before_any_attempt(self):
        for agent in (1, True, [], {}):
            for entries in ([], self.entries):
                with self.subTest(agent=agent, entries=entries), self.assertRaises(ValueError):
                    suggest_groups.suggest({'sessions': entries, 'agent': agent})
        self.which.assert_not_called()
        self.popen.assert_not_called()
        self.urlopen.assert_not_called()

    def test_good_output_request_settings_and_timeout(self):
        groups = [{'name': ' Project ', 'sessions': ['$6', '$9']}, {'name': 'Single', 'sessions': ['$12']}]
        with patch.dict(os.environ, TETHER_LLM_URL='http://model.test/'):
            result = self.suggest(groups)
        self.assertEqual(result, {'groups': [dict(groups[0], name='Project'),
                                              {'name': 'Other', 'sessions': ['$12']}], 'source': 'llm'})
        request = self.urlopen.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), ('http://model.test/v1/chat/completions', 'POST'))
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        body = json.loads(request.data)
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(body['max_tokens'], 1024)
        self.assertEqual(body['temperature'], 0.2)
        self.assertIn('2–8 groups', body['messages'][0]['content'])
        self.assertIn('Every group must have 2 or more sessions.', body['messages'][0]['content'])
        self.assertIn('Do not just repeat one session name', body['messages'][0]['content'])
        self.assertIn('Web app: 3 8 12', body['messages'][0]['content'])
        self.assertEqual(body['messages'][1]['content'],
                         '6 | Session | work/project | First chat\n'
                         '9 | Session | other/project | First chat\n'
                         '12 | Session | work/single | First chat')
        self.assertEqual(suggest_groups.MODEL_TIMEOUT, 90)
        self.assertEqual(self.urlopen.call_args.kwargs['timeout'], suggest_groups.MODEL_TIMEOUT)

    def test_line_output_maps_ids_and_uses_validation(self):
        self.reply('Project: 6 9, $6 99\n- Single: 12\n')
        self.assertEqual(suggest_groups.suggest({'sessions': self.entries}),
                         {'groups': [{'name': 'Project', 'sessions': ['$6', '$9']},
                                     {'name': 'Other', 'sessions': ['$12']}], 'source': 'llm'})

    def test_line_output_ignores_trailer_and_other_stray_lines(self):
        self.reply('Here are the groups:\nProject: 6 9\n\nNeeds you: no')
        self.assertEqual(suggest_groups.suggest({'sessions': self.entries}),
                         {'groups': [{'name': 'Project', 'sessions': ['$6', '$9']},
                                     {'name': 'Other', 'sessions': ['$12']}], 'source': 'llm'})

    def test_compact_output_uses_existing_group_shape_and_validation(self):
        self.reply('{"g":[{"n":" Project ","s":["$6","$9","$6","$99"]},{"n":"Single","s":["$12"]}]}')
        self.assertEqual(suggest_groups.suggest({'sessions': self.entries}),
                         {'groups': [{'name': 'Project', 'sessions': ['$6', '$9']},
                                     {'name': 'Other', 'sessions': ['$12']}], 'source': 'llm'})

    def test_json_with_extra_text_uses_model_groups(self):
        groups = [{'name': 'Project', 'sessions': ['$6', '$9', '$12']}]
        output = json.dumps({'groups': groups})
        for answer in (output + '}', '  ```json\n' + output + '\n```  ',
                       'Here are the groups:\n' + output):
            with self.subTest(answer=answer):
                self.reply(answer)
                self.assertEqual(suggest_groups.suggest({'sessions': self.entries}),
                                 {'groups': groups, 'source': 'llm'})

    def test_bad_json_and_bad_envelopes_use_folder_fallback(self):
        expected = {'groups': [{'name': 'project', 'sessions': ['$6', '$9']}, {'name': 'Other', 'sessions': ['$12']}],
                    'source': 'folder'}
        for output in ('not JSON', '```json\n{}\n```', 'null', '[]', '{}', '{"groups":null}',
                       '{"groups":[null]}', '{"groups":[{"name":2,"sessions":[]}]}',
                       '{"groups":[{"name":"Project","sessions":{}}]}', '[' * 2000,
                       '{"g":null}', '{"g":[null]}', '{"g":[{}]}',
                       '{"g":[{"n":2,"s":[]}]}', '{"g":[{"n":"Project","s":{}}]}'):
            with self.subTest(output=output):
                self.reply(output)
                self.assertEqual(suggest_groups.suggest({'sessions': self.entries}), expected)
        for raw in (b'not JSON', b'{}', b'{"choices":[]}', b'{"choices":[{"message":{"content":null}}]}'):
            self.urlopen.return_value.__enter__.return_value.read.return_value = raw
            self.assertEqual(suggest_groups.suggest({'sessions': self.entries}), expected)

    def test_model_errors_and_timeout_use_folder_fallback(self):
        for error in (TimeoutError(), urllib.error.URLError('down'), ConnectionError(),
                      urllib.error.HTTPError('http://model.test', 500, 'error', {}, None)):
            with self.subTest(error=error):
                self.urlopen.side_effect = error
                self.assertEqual(suggest_groups.suggest({'sessions': self.entries})['source'], 'folder')

    def test_unknown_ids_duplicates_missing_sessions_and_empty_groups(self):
        result = self.suggest([{'name': ' Project ', 'sessions': ['$6', '$9', '$unknown', '$6', None, []]},
                               {'name': 'Duplicate', 'sessions': ['$6']},
                               {'name': ' ', 'sessions': ['$9']}, {'name': 'Empty', 'sessions': []}])
        self.assertEqual(result, {'groups': [{'name': 'Project', 'sessions': ['$6', '$9']},
                                             {'name': 'Other', 'sessions': ['$12']}], 'source': 'llm'})

    def test_names_are_trimmed_and_capped_at_forty(self):
        result = self.suggest([{'name': '  ' + 'n' * 50 + '  ', 'sessions': ['$6', '$9', '$12']}])
        self.assertEqual(result['groups'][0]['name'], 'n' * 40)

    def test_at_most_eight_groups_including_missing_sessions(self):
        entries = [session('$' + str(index)) for index in range(24)]
        groups = [{'name': 'Group ' + str(index), 'sessions': [entries[index]['id'], entries[index + 1]['id']]}
                  for index in range(0, len(entries), 2)]
        result = self.suggest(groups, entries)
        self.assertEqual(len(result['groups']), 8)
        self.assertEqual(result['groups'][-1], {'name': 'Other', 'sessions': [item['id'] for item in entries[14:]]})
        self.assertEqual([identifier for group in result['groups'] for identifier in group['sessions']],
                         [item['id'] for item in entries])

    def test_missing_sessions_reuse_other_group(self):
        entries = [session('$' + str(index)) for index in range(6)]
        result = self.suggest([{'name': 'Project', 'sessions': ['$0', '$1']},
                               {'name': 'Other', 'sessions': ['$2', '$3']},
                               {'name': 'Single', 'sessions': ['$4', '$0']}], entries)
        self.assertEqual(result, {'groups': [{'name': 'Project', 'sessions': ['$0', '$1']},
                                             {'name': 'Other', 'sessions': ['$2', '$3', '$4', '$5']}],
                                  'source': 'llm'})

    def test_observed_singleton_reply_uses_folder_fallback(self):
        self.reply('CI Audit: 1\nEdgar submission live integration and fixes: 26\nFriday test: 19\n'
                   'Goals and Cleanup: 27\nRelease: 13\nReview to Improve app: 29\nTether: 0\n'
                   'Triage: 16\nedgarization-31-oct: 5\nweekly-reporting: 8')
        identifiers = [1, 26, 19, 27, 13, 29, 0, 16, 5, 8, 30, 31, 32, 33, 34]
        entries = [session('$' + str(identifier)) for identifier in identifiers]
        self.assertEqual(suggest_groups.suggest({'sessions': entries}),
                         {'groups': [{'name': 'project', 'sessions': [item['id'] for item in entries]}],
                          'source': 'folder'})

    def test_only_other_model_group_uses_unique_folder_fallback(self):
        entries = [session('$1', '/one'), session('$2', '/two'), session('$3', '/three')]
        for groups in ([], [{'name': 'Other', 'sessions': ['$1', '$2', '$3']}],
                       [{'name': 'One', 'sessions': ['$1']}, {'name': 'Two', 'sessions': ['$2']}]):
            with self.subTest(groups=groups):
                self.assertEqual(self.suggest(groups, entries),
                                 {'groups': [{'name': 'Other', 'sessions': ['$1', '$2', '$3']}],
                                  'source': 'folder'})

    def test_folder_fallback_handles_root_empty_paths_and_last_segment(self):
        entries = [session('$1', '/a/shared'), session('$2', '/b/shared/'), session('$3', '/'),
                   session('$4', ''), session('$5', '/unique')]
        self.reply('broken')
        self.assertEqual(suggest_groups.suggest({'sessions': entries}),
                         {'groups': [{'name': 'shared', 'sessions': ['$1', '$2']},
                                     {'name': 'Other', 'sessions': ['$3', '$4', '$5']}], 'source': 'folder'})

    def test_empty_sessions_do_not_call_model(self):
        self.assertEqual(suggest_groups.suggest({'sessions': []}), {'groups': [], 'source': 'folder'})
        self.urlopen.assert_not_called()

    def test_request_trims_and_clips_names_folders_and_twelve_chats(self):
        entries = [session('$6', '  /' + 'd' * 200 + '  ', name='  ' + 'n' * 200 + '  ',
                           chats=['  ' + 'c' * 200 + '  '] * 12)]
        self.suggest([], entries)
        stored = suggest_groups.sessions({'sessions': entries})[0]
        self.assertEqual(stored, session('$6', '/' + 'd' * 119, name='n' * 120, chats=['c' * 120] * 12))
        sent = json.loads(self.urlopen.call_args.args[0].data)['messages'][1]['content']
        self.assertEqual(sent, '6 | ' + 'n' * 120 + ' | ' + 'd' * 119 + ' | ' + '; '.join(['c' * 40] * 3))

    def test_compact_input_cleans_values_drops_empty_and_duplicate_chats(self):
        entries = [session('$12', '/work/pro|ject\n\r/', name='Ses|sion\n\r',
                           chats=['Session', '', '|\n\r', 'First| chat\n\r', 'x' * 50, 'Third', 'Fourth']),
                   session('$13', '/', name='|\n\r', chats=['', '|'])]
        self.suggest([], entries)
        sent = json.loads(self.urlopen.call_args.args[0].data)['messages'][1]['content']
        self.assertEqual(sent, '12 | Session | work/project | First chat; ' + 'x' * 40 + '; Third\n13')
        self.assertEqual(entries[0]['name'], 'Ses|sion\n\r')

    def test_model_input_uses_last_two_folder_parts_and_cleans_each(self):
        entries = [session('$1', '/work/app/server'), session('$2', '/server'),
                   session('$3', '/work/ap|p/ser|ver'), session('$4', '/work/|\n\r/server'),
                   session('$5', '/'), session('$6', '/work//app/server/')]
        self.assertEqual(suggest_groups.model_input(entries),
                         '1 | Session | app/server | First chat\n'
                         '2 | Session | server | First chat\n'
                         '3 | Session | app/server | First chat\n'
                         '4 | Session | server | First chat\n'
                         '5 | Session | First chat\n'
                         '6 | Session | app/server | First chat')

    def test_two_hundred_sessions_are_accepted(self):
        entries = [session('$' + str(index)) for index in range(200)]
        result = self.suggest([], entries)
        self.assertEqual(result, {'groups': [{'name': 'project', 'sessions': [item['id'] for item in entries]}],
                                  'source': 'folder'})

    def test_bad_requests_and_size_limits_never_call_model(self):
        bad = [None, [], {}, {'sessions': None}, {'sessions': {}},
               {'sessions': [session('$' + str(index)) for index in range(201)]},
               {'sessions': [None]}, {'sessions': [{}]}, {'sessions': [session('%1')]},
               {'sessions': [session('$1'), session('$1')]}, {'sessions': [session('$1', chats=['x'] * 13)]}]
        for key, value in (('id', None), ('name', 12), ('dir', []), ('chats', None), ('chats', [1])):
            bad.append({'sessions': [session('$1', **{key: value})]})
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                suggest_groups.suggest(body)
        self.urlopen.assert_not_called()


class CodexFastModelTests(unittest.TestCase):
    def test_first_luna_model_in_both_cache_shapes(self):
        models = [{'slug': slug} for slug in
                  ('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-luna', 'gpt-5.6-luna')]
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            for cache in ({'models': models}, models):
                with self.subTest(cache=cache):
                    (Path(folder) / 'models_cache.json').write_text(json.dumps(cache), encoding='utf-8')
                    self.assertEqual(suggest_groups.codex_fast_model(), 'gpt-6-luna')

    def test_missing_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            self.assertIsNone(suggest_groups.codex_fast_model())

    def test_bad_json_wrong_types_and_no_luna_return_none(self):
        caches = ('not JSON', 'null', '42', '"models"', '{}', '{"models":null}',
                  '{"models":{}}', '[null]', '[{}]', '[{"slug":6}]',
                  '[{"slug":"gpt-6.1-sol"}]',
                  '[{"slug":"gpt-6-luna-extra"},{"slug":"other-gpt-6-luna"}]')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            for cache in caches:
                with self.subTest(cache=cache):
                    (Path(folder) / 'models_cache.json').write_text(cache, encoding='utf-8')
                    self.assertIsNone(suggest_groups.codex_fast_model())

    def test_empty_codex_home_uses_default_path(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=''), \
                patch.object(suggest_groups.paths, 'CODEX', Path(folder)):
            (Path(folder) / 'models_cache.json').write_text('[{"slug":"gpt-6-luna"}]', encoding='utf-8')
            self.assertEqual(suggest_groups.codex_fast_model(), 'gpt-6-luna')


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = self.enterContext(patch('urllib.request.urlopen', side_effect=TimeoutError()))
        self.enterContext(patch('suggest_groups.shutil.which', return_value=None))

    def test_authenticated_route_and_model_failure_return_200(self):
        value = handler(suggest_groups.PATH, {'sessions': [session('$6')]})
        server.Handler.do_POST(value)
        value.deny.assert_called_once_with()
        value.send_response.assert_called_once_with(200)
        self.assertEqual(json.loads(value.wfile.getvalue()), {'groups': [{'name': 'Other', 'sessions': ['$6']}],
                                                              'source': 'folder'})

    def test_route_rejects_missing_cookie_and_forged_cookie(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(auth, 'MODE', 'token'), \
                patch.object(auth, 'TOKEN_FILE', Path(folder) / 'token'), patch.object(auth, 'TAILSCALE', False):
            for cookie in ('', auth.COOKIE + '=forged'):
                value = handler(suggest_groups.PATH, {'sessions': [session('$6')]})
                value.client_address = ('127.0.0.1', 12345)
                value.deny = server.Handler.deny.__get__(value)
                value.headers['Cookie'] = cookie
                server.Handler.do_POST(value)
                value.send_response.assert_called_once_with(401)
        self.urlopen.assert_not_called()

    def test_real_cookie_passes_authentication(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(auth, 'MODE', 'token'), \
                patch.object(auth, 'TOKEN_FILE', Path(folder) / 'token'), patch.object(auth, 'TAILSCALE', False):
            value = handler(suggest_groups.PATH, {'sessions': []})
            value.client_address = ('127.0.0.1', 12345)
            value.deny = server.Handler.deny.__get__(value)
            value.headers['Cookie'] = auth.COOKIE + '=' + auth.session_value()
            server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(200)

    def test_route_checks_origin_and_write_header_before_model(self):
        for headers in ({'Origin': 'https://evil.test'}, {'Origin': ''}, {'X-Codex-Mobile': ''}):
            value = handler(suggest_groups.PATH, {'sessions': [session('$6')]})
            value.headers.update(headers)
            server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(403)
        self.urlopen.assert_not_called()

    def test_bad_bodies_and_lengths_return_400(self):
        for body in (None, [], {}, {'sessions': [session('$6', chats=['x'] * 13)]}):
            value = handler(suggest_groups.PATH, body)
            server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(400)
        for length in ('0', '-1', 'bad', str(suggest_groups.BODY_LIMIT + 1)):
            value = handler(suggest_groups.PATH, {'sessions': []})
            value.headers['Content-Length'] = length
            server.Handler.do_POST(value)
            value.send_response.assert_called_once_with(400)
        value = handler(suggest_groups.PATH, {'sessions': []})
        value.rfile = io.BytesIO(b'not valid JSON')
        server.Handler.do_POST(value)
        value.send_response.assert_called_once_with(400)
        self.urlopen.assert_not_called()

    def test_route_accepts_full_sized_unicode_request(self):
        text = '\U0001f600' * 120
        entries = [session('$' + str(index), text, name=text, chats=[text] * 12) for index in range(200)]
        value = handler(suggest_groups.PATH, {'sessions': entries})
        self.assertGreater(int(value.headers['Content-Length']), 65536)
        server.Handler.do_POST(value)
        value.send_response.assert_called_once_with(200)
        result = json.loads(value.wfile.getvalue())
        self.assertEqual(len(result['groups'][0]['sessions']), 200)


if __name__ == '__main__':
    unittest.main()
