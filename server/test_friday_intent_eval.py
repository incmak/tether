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

    def test_lookup_uses_case_result_and_scores_extra_action(self):
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
        self.assertEqual(result['passed'], 0)
        self.assertEqual(result['fails'], {'extra_action': 1})
        trace, = result['samples']
        self.assertEqual(trace['action'], {'name': 'send_to_chat',
                                           'args': {'chat': 'Friday test', 'prompt': 'Fix it'}})
        self.assertEqual(trace['actions'], [trace['action'],
                                           {'name': 'switch_chat', 'args': {'chat': 'Other chat'}}])
        self.assertEqual(trace['steps'], 2)
        self.assertEqual(payloads[1]['messages'][-2]['tool_calls'], [lookup])
        self.assertEqual(payloads[1]['messages'][-1], {'role': 'tool', 'tool_call_id': 'c1',
                         'content': json.dumps({'chats': ['Friday test']})})

    def test_duplicate_send_is_blocked_after_allowed_send(self):
        result, payloads = self.run_script(
            case('send_to_chat'),
            assistant(tool_calls=[call('send_to_chat', chat='Friday test', prompt='Fix it'),
                                  call('send_to_chat', 'c2', chat='Other chat', prompt='Check it')]))
        self.assertEqual((result['passed'], len(payloads)), (0, 1))
        self.assertEqual(result['fails'], {'blocked': 1})
        trace, = result['samples']
        self.assertEqual(trace['actions'], [trace['action']])
        blocked, = trace['blocked']
        self.assertEqual(blocked, {'name': 'send_to_chat', 'args': {'chat': 'Other chat', 'prompt': 'Check it'},
                                   'blocked': 'Only one send_to_chat is allowed per spoken turn.'})

    def test_split_speech_joins_fragments_for_guard(self):
        example = case('send_to_chat', user=["let's ask friday test to look at the logs", 'and fix it'])
        result, payloads = self.run_script(
            example, assistant(tool_calls=[call('send_to_chat', chat='friday test', prompt='Fix logs')]))
        # Consent in the first fragment applies to the whole spoken turn.
        self.assertEqual((result['passed'], len(payloads)), (1, 1))
        trace, = result['samples']
        self.assertEqual(trace['action']['name'], 'send_to_chat')
        self.assertEqual(payloads[0]['messages'][1]['content'], example['user'][0])
        self.assertEqual(payloads[0]['messages'][-1]['content'], example['user'][1])

    def test_friday_readback_scores_by_expectation_and_ends_the_run(self):
        proposal = {'name': 'send_to_chat', 'chat': ' Friday Test ', 'payload': 'Fix the login bug'}
        reply = friday.send_readback(friday.SendProposal(**proposal, turn=1))
        send = assistant('I sent it.', tool_calls=[
            call('send_to_chat', chat=proposal['chat'], prompt=proposal['payload']),
            call('backlog_send', 'c2', chat='docs-site', item='B12'),
            call('switch_chat', 'c3', chat='docs-site')])
        args = {'chat': '(?i)friday test', 'prompt': 'login bug'}
        for expect, fails in (
                ({'tool': None, 'readback_chat': 'friday test', 'args': args}, {}),
                ({'tool': 'send_to_chat', 'args': args}, {'readback_instead': 1}),
                ({'tool': None}, {'unwanted_readback': 1}),
                ({'tool': None, 'readback_chat': 'docs-site'}, {'readback': 1}),
                ({'tool': None, 'readback_chat': 'friday test', 'args': {'prompt': 'logout'}}, {'wrong_args': 1})):
            with self.subTest(expect=expect):
                result, payloads = self.run_script(case(user='friday test should fix the login bug',
                                                        expect=expect), send)
                self.assertEqual((result['passed'], result['fails'], len(payloads)), (int(not fails), fails, 1))
                trace, = result['samples']
                self.assertEqual((trace['proposal'], trace['reply']), (proposal, reply))
                self.assertEqual((trace['action'], trace['actions'], trace['blocked']), (None, [], []))

    def test_backlog_readback_requires_the_expected_tool(self):
        send = assistant(tool_calls=[call('backlog_send', chat='docs-site', item='B12')])
        expect = {'tool': None, 'readback_chat': 'docs-site'}
        example = case(user='B12 for docs-site', expect=expect)
        result, _ = self.run_script(example, send)
        self.assertEqual(result['fails'], {'readback': 1})
        expect['readback_tool'] = 'backlog_send'
        result, _ = self.run_script(example, send)
        self.assertEqual(result['passed'], 1)
        self.assertEqual(result['samples'][0]['reply'], 'Send B12 to docs-site. Yes or no?')

    def test_pending_proposal_binds_yes_to_the_last_readback_and_chat(self):
        pending = {'name': 'send_to_chat', 'chat': 'friday test', 'payload': 'Fix the login bug'}
        readback = friday.send_readback(friday.SendProposal(**pending, turn=0))
        example = case('send_to_chat', user=[' Yes, please do. ', ' And also check the logs after. '],
                       history=[assistant(readback)], pending=pending)
        for chat, last_reply, fails in (
                ('friday test', readback, {}),
                ('docs-site', readback, {'readback_instead': 1}),
                ('friday test', 'Send to friday test: Fix the logout bug. Yes or no?', {'readback_instead': 1})):
            with self.subTest(chat=chat, last_reply=last_reply):
                example['history'][-1] = assistant(last_reply)
                sent = assistant(tool_calls=[call('send_to_chat', chat=chat, prompt=pending['payload'])])
                result, _ = self.run_script(example, sent)
                self.assertEqual((result['passed'], result['fails']), (int(not fails), fails))
                trace, = result['samples']
                if fails:
                    self.assertEqual(trace['proposal'], dict(pending, chat=chat))
                else:
                    self.assertIsNone(trace['proposal'])
                    self.assertEqual(trace['action']['name'], 'send_to_chat')

    def test_declined_pending_proposal_still_blocks_send(self):
        pending = {'name': 'send_to_chat', 'chat': 'friday test', 'payload': 'Fix the login bug'}
        readback = friday.send_readback(friday.SendProposal(**pending, turn=0))
        sent = assistant(tool_calls=[call('send_to_chat', chat=pending['chat'], prompt=pending['payload'])])
        reason = 'The user declined the proposed send.'
        for user in ('no, not now', "no, don't send it"):
            with self.subTest(user=user):
                example = case(user=user, history=[assistant(readback)], pending=pending)
                result, payloads = self.run_script(example, sent, assistant('Okay.'))
                self.assertEqual(result['fails'], {'blocked': 1})
                trace, = result['samples']
                self.assertIsNone(trace['proposal'])
                self.assertEqual(trace['blocked'][0]['blocked'], reason)
                self.assertEqual(json.loads(payloads[1]['messages'][-1]['content']), {'error': reason})

    def test_readback_must_authorize_a_follow_up_yes(self):
        example = case(expect={'tool': None, 'readback_chat': 'friday test'})
        good = 'I will ask friday test to fix the login bug. Shall I send that to friday test?'
        self.assertEqual(self.run_script(example, assistant(good))[0]['passed'], 1)
        self.assertEqual(self.run_script(example, assistant('Okay.'))[0]['fails'], {'readback': 1})
        follow_up = case('send_to_chat', user='yes', history=[assistant(good), assistant(None)])
        sent = assistant(tool_calls=[call('send_to_chat', chat='friday test', prompt='Fix the login bug')])
        # Friday speaks a fresh read-back when the last assistant message cannot authorize the send.
        self.assertEqual(self.run_script(follow_up, sent)[0]['fails'], {'readback_instead': 1})
        follow_up['history'].pop()
        self.assertEqual(self.run_script(follow_up, sent)[0]['passed'], 1)

    def test_expected_lookup_with_a_write_fails_extra_action(self):
        example = case('list_chats')
        both = assistant(tool_calls=[call('list_chats'), call('switch_chat', chat='friday test')])
        self.assertEqual(self.run_script(example, both)[0]['fails'], {'extra_action': 1})
        self.assertEqual(self.run_script(example, assistant(tool_calls=[call('list_chats')]))[0]['passed'], 1)

    def test_readback_requires_send_and_the_chat_name(self):
        example = case(expect={'tool': None, 'readback_chat': 'api-server'})
        for reply in ("I'll explain the options.", 'Shall I send the fix?', 'I can fix api-server.',
                      "I won't send anything to api-server.", 'I can send that to api-server.',
                      "I won't send anything to api-server. Do you want a summary?",
                      'Should I send it to docs-site instead of api-server?',
                      'Should I send it to api-server-old?', 'Should I not send it to api-server?',
                      'Shall I send this to api-server or docs-site?',
                      'I can send that to api-server. Is that your chat?',
                      'Shall I send it to docs-site or to api-server?',
                      'Shall I send it to docs-site. Can you confirm whether you meant to refer to api-server?'):
            with self.subTest(reply=reply):
                self.assertEqual(self.run_script(example, assistant(reply))[0]['fails'], {'readback': 1})
        self.assertEqual(self.run_script(example, assistant('Shall I send the fix to api-server?'))[0]['passed'], 1)

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
        self.assertEqual(result['samples'][0]['actions'], [])
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

    def test_offline_cli_rejects_invalid_fixtures(self):
        for data, message in ((fixture(case(), case()), 'duplicate id'),
                              (fixture(case('unknown_tool')), 'unknown tool'),
                              (fixture(case(group='typo')), 'unknown group'),
                              (fixture(case(expect={'tool': None, 'reply': '['})), 'regex'),
                              (fixture(case(pending={'name': 'send_to_chat', 'chat': 'friday test'})), 'pending'),
                              (fixture(case(expect={'tool': None, 'readback_tool': 'switch_chat'})), 'readback_tool')):
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

    def test_live_brain_requires_gemini_before_importing_google_auth(self):
        for env in ({}, {'TETHER_FRIDAY_BRAIN': 'local'}):
            with self.subTest(env=env), patch.dict('os.environ', env, clear=True):
                with patch('builtins.__import__', side_effect=AssertionError('unexpected import')) as import_module:
                    with self.assertRaisesRegex(evaluation.LiveError, '^Live eval needs TETHER_FRIDAY_BRAIN=gemini$'):
                        evaluation.live_brain()
                import_module.assert_not_called()

    def test_live_brain_rejects_non_vertex_urls(self):
        credentials = Mock()
        auth = SimpleNamespace(default=Mock(return_value=(credentials, 'fixture-project')))
        modules = {'google': SimpleNamespace(auth=auth), 'google.auth': auth,
                   'google.auth.transport.requests': SimpleNamespace(Request=Mock())}
        with patch.dict('sys.modules', modules), patch.dict('os.environ', {'TETHER_FRIDAY_BRAIN': 'gemini'}, clear=True):
            with patch.object(evaluation, '_post_completion') as post:
                for url in ('http://127.0.0.1:7687/v1', 'https://aiplatform.googleapis.com.example/v1'):
                    with self.subTest(url=url), patch.object(friday, 'brain_config', return_value=SimpleNamespace(base_url=url)):
                        with self.assertRaises(evaluation.LiveError):
                            evaluation.live_brain()
                post.assert_not_called()
        credentials.refresh.assert_not_called()

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
