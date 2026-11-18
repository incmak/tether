"""Offline checks for Friday's intent eval runner."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import friday_agent as friday
import friday_intent_eval as evaluation


def case(tool=None, **fields):
    return dict({'id': 'ask-friday', 'group': 'send_direct', 'split': 'dev',
                 'source': 'hand', 'history': [], 'user': 'Ask friday test to fix it',
                 'expect': {'tool': tool}}, **fields)


def fixture(*cases, **fields):
    return dict({'version': 1, 'state': {'target': 'friday test'}, 'tool_results': {},
                 'cases': list(cases)}, **fields)


def call(name, ident='c1', **args):
    return {'id': ident, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


def assistant(content=None, **fields):
    return dict(role='assistant', content=content, **fields)


class IntentEvalTests(unittest.TestCase):
    def run_script(self, example, *messages, **kwargs):
        complete = Mock(side_effect=messages)
        result = evaluation.run_case(example, kwargs.pop('data', fixture(example)), complete, **kwargs)
        return result, [invocation.args[0] for invocation in complete.call_args_list]

    def test_text_reply_and_false_action_claim(self):
        example = case(expect={'tool': None, 'reply': 'it'})
        result, _ = self.run_script(example, assistant('I can switch to it.'))
        self.assertEqual(result['passed'], 1)
        for phrase in ('I have switched to it.', "I've just opened it."):
            with self.subTest(phrase=phrase):
                result, _ = self.run_script(example, assistant(phrase))
                self.assertEqual(result['passed'], 0)
                self.assertEqual(result['fails'], {'claimed_action': 1})

    def test_lookup_uses_case_result_and_stops_on_first_action(self):
        example = case(expect={'tool': 'send_to_chat', 'args': {'chat': '(?i)friday test'}},
                       tool_results={'list_chats': {'chats': ['Friday test']}})
        data = fixture(example, tool_results={'list_chats': {'chats': ['Other chat']},
                                              'backlog_list': {'items': ['B12']}})
        lookup = call('list_chats')
        lookup['extra_content'] = {'google': {'thought_signature': 'returned'}}
        result, payloads = self.run_script(
            example, assistant(tool_calls=[lookup]),
            assistant(tool_calls=[call('send_to_chat', 'c2', chat='Friday test', prompt='Fix it'),
                                  call('switch_chat', 'c3', chat='Other chat')]), data=data)
        self.assertEqual(result['passed'], 1)
        trace, = result['samples']
        self.assertEqual(trace['action'], {'name': 'send_to_chat',
                                           'args': {'chat': 'Friday test', 'prompt': 'Fix it'}})
        self.assertEqual(trace['steps'], 2)
        self.assertEqual(payloads[1]['messages'][-2]['tool_calls'], [lookup])
        self.assertEqual(payloads[1]['messages'][-1], {'role': 'tool', 'tool_call_id': 'c1',
                         'content': json.dumps({'chats': ['Friday test']})})

    def test_split_speech_uses_only_final_fragment_for_guard(self):
        example = case('send_to_chat', user=["let's ask friday test to look at the logs", 'and fix it'])
        result, payloads = self.run_script(
            example, assistant(tool_calls=[call('send_to_chat', chat='friday test', prompt='Fix logs')]),
            assistant('Shall I send that?'))
        self.assertEqual(result['fails'], {'blocked': 1, 'wrong_tool': 1})
        trace, = result['samples']
        self.assertIsNone(trace['action'])
        self.assertEqual(payloads[0]['messages'][1]['content'], example['user'][0])
        self.assertEqual(payloads[0]['messages'][-1]['content'], example['user'][1])
        reason = trace['blocked'][0]['blocked']
        self.assertEqual(json.loads(payloads[1]['messages'][-1]['content']), {'error': reason})

    def test_readback_must_authorize_a_follow_up_yes(self):
        example = case(expect={'tool': None, 'readback_chat': 'friday test'})
        good = 'I will ask friday test to fix the login bug. Shall I send that?'
        self.assertEqual(self.run_script(example, assistant(good))[0]['passed'], 1)
        self.assertEqual(self.run_script(example, assistant('Okay.'))[0]['fails'], {'readback': 1})
        follow_up = case('send_to_chat', user='yes', history=[assistant(good), assistant(None)])
        sent = assistant(tool_calls=[call('send_to_chat', chat='friday test', prompt='Fix the login bug')])
        self.assertIn('blocked', self.run_script(follow_up, sent, assistant())[0]['fails'])
        follow_up['history'].pop()
        self.assertEqual(self.run_script(follow_up, sent)[0]['passed'], 1)

    def test_event_prefix_disables_actions_and_preserves_stt_text(self):
        for tool in ('send_to_chat', 'switch_chat'):
            with self.subTest(tool=tool):
                example = case(event=True, user='Ask friday test about teether')
                result, payloads = self.run_script(example, assistant(tool_calls=[call(tool)]),
                                                   assistant('friday test is done.'))
                self.assertEqual(result['fails'], {'blocked': 1})
                self.assertEqual(payloads[0]['messages'][-1]['content'], friday.EVENT_PREFIX + example['user'])

    def test_history_signs_only_the_first_call_and_preserves_existing_extra_content(self):
        unsigned = [call('list_chats'), call('backlog_list', 'c2')]
        signed = [call('list_chats', 'c3'), call('backlog_list', 'c4')]
        signed[1]['extra_content'] = {'google': {'thought_signature': 'original'}}
        history = []
        for calls in (unsigned, signed):
            history.append(assistant(tool_calls=calls))
            history.extend({'role': 'tool', 'tool_call_id': item['id'], 'content': '{}'} for item in calls)
        original = deepcopy(history)
        _, payloads = self.run_script(case(history=history), assistant('Okay.'))
        first, second = [message['tool_calls'] for message in payloads[0]['messages'] if 'tool_calls' in message]
        self.assertEqual(first[0]['extra_content'], {'google': {'thought_signature':
                         'skip_thought_signature_validator'}})
        self.assertEqual(first[1], original[0]['tool_calls'][1])
        self.assertEqual(second, original[3]['tool_calls'])
        self.assertEqual(history, original)

    def test_payload_uses_friday_prefix_state_stt_and_brain_settings(self):
        example = case(user='Tell teether', state={'idle': 3})
        brain = SimpleNamespace(model='example', max_tokens=42, extra={'reasoning_effort': 'low'})
        _, payloads = self.run_script(example, assistant('Okay.'), brain=brain)
        payload, = payloads
        prefix = friday.llm_prefix()
        self.assertEqual(payload['messages'][:1], prefix['messages'])
        self.assertEqual(payload['tools'], prefix['tools'])
        messages, _ = friday.add_state(prefix['messages'] + [{'role': 'user', 'content': 'Tell Tether'}],
                                       example['state'], [], None)
        self.assertEqual(payload['messages'], messages)
        self.assertEqual((payload['model'], payload['max_tokens'], payload['reasoning_effort']), ('example', 42, 'low'))

    def test_expected_lookup_alternative_and_bad_json_arguments(self):
        example = case(expect={'any_tool': ['list_chats', 'backlog_list'], 'args': {'session': 'Friday'}})
        named = call('list_chats', session='Friday test')
        result, payloads = self.run_script(example, assistant(tool_calls=[named]))
        self.assertEqual((result['passed'], len(payloads)), (1, 1))
        bad = call('list_chats')
        bad['function']['arguments'] = '{'
        result, _ = self.run_script(example, assistant(tool_calls=[bad]))
        self.assertEqual(result['fails'], {'wrong_args': 1})
        self.assertEqual(result['samples'][0]['action']['args'], {})

    def test_claim_ok_allows_a_true_claim_from_history(self):
        example = case(expect={'tool': None, 'claim_ok': True})
        result, _ = self.run_script(example, assistant(content='I sent it when you said yes.'))
        self.assertEqual(result['passed'], 1)

    def test_stray_or_empty_reply_fails(self):
        for text in ('层', '', 'OK.'):
            result, _ = self.run_script(case(), assistant(content=text))
            self.assertEqual(result['passed'], int(text == 'OK.'), text)

    def test_step_limit_and_run_samples_and_usage(self):
        lookup = assistant(tool_calls=[call('backlog_list')], _usage={'prompt_tokens': 10})
        result, payloads = self.run_script(case(), *([lookup] * 12), runs=3)
        # Four lookups and no spoken reply: Friday would say nothing, so the run fails.
        self.assertEqual((len(payloads), result['passed'], result['runs']), (12, 0, 3))
        self.assertEqual(result['fails'], {'stray_text': 3})
        self.assertEqual((result['prompt_tokens'], result['llm_calls']), (120, 12))
        self.assertEqual([sample['steps'] for sample in result['samples']], [4, 4])
        self.assertNotIn('_usage', payloads[1]['messages'][-2])
        self.assertEqual(json.loads(payloads[1]['messages'][-1]['content']), {'ok': True})

    def test_offline_cli_rejects_duplicate_id_unknown_tool_group_and_invalid_regex(self):
        for data, message in ((fixture(case(), case()), 'duplicate id'),
                              (fixture(case('unknown_tool')), 'unknown tool'),
                              (fixture(case(group='typo')), 'unknown group'),
                              (fixture(case(expect={'tool': None, 'reply': '['})), 'regex')):
            with self.subTest(message=message), TemporaryDirectory() as folder:
                path = Path(folder) / 'cases.json'
                path.write_text(json.dumps(data), encoding='utf-8')
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertNotEqual(evaluation.main(['--cases', str(path)]), 0)
                self.assertIn(message, errors.getvalue())

    def test_cli_filters_and_writes_live_report_with_mock_transport(self):
        data = fixture(case(), case('list_chats', id='holdout', group='list_chats', split='holdout'))
        with TemporaryDirectory() as folder:
            cases_path, report_path = (Path(folder) / name for name in ('cases.json', 'report.json'))
            cases_path.write_text(json.dumps(data), encoding='utf-8')
            with patch.object(evaluation, 'live_brain') as live, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(evaluation.main(['--cases', str(cases_path)]), 0)
                live.assert_not_called()
            self.assertIn('send_direct', output.getvalue())
            self.assertIn('holdout', output.getvalue())
            complete = Mock(return_value=assistant('Okay.', _usage={'prompt_tokens': 50}))
            brain = SimpleNamespace(model='example', max_tokens=42, extra={})
            with patch.object(evaluation, 'live_brain', return_value=(brain, complete)), redirect_stdout(io.StringIO()):
                self.assertEqual(evaluation.main(['--cases', str(cases_path), '--live', '--split', 'holdout',
                                                  '--out', str(report_path), '--runs', '2']), 0)
            report = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(report['overall'], {'passed': 0, 'total': 2})
        self.assertEqual(report['cases'][0]['id'], 'holdout')
        self.assertEqual(report['cases'][0]['fails'], {'wrong_tool': 2})
        self.assertEqual(report['prompt_tokens_mean'], 50)
        self.assertEqual(report['system_prompt_chars'], len(friday.SYSTEM_PROMPT))

    def test_transport_retries_only_transient_errors_and_uses_response_usage(self):
        url = 'https://provider.invalid/chat/completions'
        payload = {'model': 'test-model', 'messages': []}
        for error in (HTTPError(url, 429, '', None, io.BytesIO()),
                      HTTPError(url, 503, '', None, io.BytesIO()), TimeoutError(), URLError(TimeoutError())):
            with self.subTest(error=type(error).__name__):
                reply = assistant('Okay.')
                usage = {'prompt_tokens': 23}
                response = io.BytesIO(json.dumps({'choices': [{'message': reply}], 'usage': usage}).encode())
                with patch.object(evaluation, 'urlopen', side_effect=[error, response]) as post:
                    message = evaluation._post_completion(url, payload, 'test-token')
                self.assertEqual(message, dict(reply, _usage=usage))
                self.assertEqual(post.call_count, 2)
                request = post.call_args.args[0]
                self.assertEqual((request.full_url, request.method), (url, 'POST'))
                self.assertEqual(json.loads(request.data), payload)
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')
                self.assertEqual(post.call_args.kwargs['timeout'], 30)

    def test_transport_failure_reports_only_status_and_redacted_provider_message(self):
        url = 'https://provider.invalid/chat/completions'
        content = {'error': {'message': 'Rejected Bearer echoed-token and test-token.',
                             'headers': 'unrelated-secret'}}
        for status, attempts in ((403, 1), (500, 2)):
            with self.subTest(status=status):
                errors = [HTTPError(url, status, 'secret-reason', None,
                                    io.BytesIO(json.dumps(content).encode())) for _ in range(attempts)]
                with patch.object(evaluation, 'urlopen', side_effect=errors) as post:
                    with self.assertRaises(evaluation.LiveError) as failure:
                        evaluation._post_completion(url, {}, 'test-token')
                self.assertEqual(str(failure.exception), f'HTTP {status} Rejected [REDACTED] and [REDACTED].')
                self.assertEqual(post.call_count, attempts)

    def test_live_brain_wires_adc_scope_project_and_token_with_mocks(self):
        credentials = SimpleNamespace(valid=False, token=None)

        def refresh(request):
            credentials.valid, credentials.token = True, 'test-token'

        credentials.refresh = Mock(side_effect=refresh)
        auth = SimpleNamespace(default=Mock(return_value=(credentials, 'fixture-project')))
        auth_request = Mock()
        modules = {'google': SimpleNamespace(auth=auth), 'google.auth': auth,
                   'google.auth.transport.requests': SimpleNamespace(Request=auth_request)}
        with patch.dict('sys.modules', modules), patch.dict('os.environ', {'TETHER_FRIDAY_BRAIN': 'gemini'}, clear=True):
            with patch.object(evaluation, '_post_completion', return_value=assistant('Okay.')) as post:
                brain, complete = evaluation.live_brain()
                for _ in range(2):
                    self.assertEqual(complete({}), assistant('Okay.'))
        auth.default.assert_called_once_with(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        credentials.refresh.assert_called_once_with(auth_request.return_value)
        self.assertIn('/projects/fixture-project/', brain.base_url)
        self.assertEqual(post.call_count, 2)
        post.assert_called_with(brain.base_url + '/chat/completions', {}, 'test-token')
