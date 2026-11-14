"""Advisor settings and CLI contracts, with isolated files and mocked tmux/tab processes."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import advisor
import agent_advisor_cli as cli
import autonomy
import codex_level
import delegate
import model_catalog
import paths

PID = 'abcdef012345'
MODELS = [{'slug': 'new-model', 'name': 'Newest Model', 'efforts': ['high', 'ultra']},
          {'slug': 'old-model', 'name': 'Older Model', 'efforts': ['low', 'medium', 'high']}]
REPLY = 'ANSWER: Use the tested option.\nCONFIDENCE: high\nEVIDENCE: server/example.py\nNEEDS_USER: no'
OFF = {'version': 2, 'main': dict(advisor.OFF), 'second': dict(advisor.OFF)}


def choices(second=None, **changes):
    value = copy.deepcopy(advisor.DEFAULT)
    value['main'].update(changes)
    if changes.get('agent') == 'codex':
        value['second'] = {'agent': 'claude', 'model': 'latest', 'effort': 'high'}
    if second is not None:
        value['second'] = dict(advisor.OFF) if second is False else second
    return value


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('CONFIG', 'CACHE', 'TETHER', 'CODEX', 'CLAUDE', 'HOME'):
            self.enterContext(patch.object(paths, name, self.root / name.lower()))
        self.enterContext(patch.object(model_catalog.shutil, 'which', return_value=None))
        self.models = self.enterContext(patch.object(codex_level, 'models', return_value=MODELS))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


class Settings(Base):
    def test_missing_or_invalid_file_defaults_and_reads_are_independent(self):
        self.assertEqual(advisor.path(), paths.CONFIG / 'advisor.json')
        self.assertEqual(advisor.read(), advisor.DEFAULT)
        for raw in ('not json', 'null', '[]', '{}', json.dumps(choices(effort='ultra'))):
            self.write(advisor.path(), raw)
            self.assertEqual(advisor.read(), advisor.DEFAULT)
        value = advisor.read()
        value['main']['agent'] = 'none'
        self.assertEqual(advisor.read(), advisor.DEFAULT)

    def test_v1_migration_true_false_and_disabled_main(self):
        for agent, model, effort, second in (
                ('claude', 'opus', 'medium', True), ('claude', 'claude-fable-5-2', 'high', False),
                ('codex', 'old-model', 'low', True), ('none', 'latest', 'auto', False),
                ('none', 'latest', 'auto', True)):
            old = {'agent': agent, 'model': model, 'effort': effort, 'second': second}
            raw = json.dumps(old)
            self.write(advisor.path(), raw)
            expected_second = ({'agent': 'codex' if agent == 'claude' else 'claude',
                                'model': 'latest', 'effort': 'high'}
                               if second and agent != 'none' else dict(advisor.OFF))
            self.assertEqual(advisor.read(), {'version': 2, 'main': {key: old[key] for key in advisor.OFF},
                                              'second': expected_second})
            self.assertEqual(advisor.path().read_text(), raw)  # Migration on read has no config writes.
        self.write(advisor.path(), json.dumps({'agent': 'bad', 'model': 'latest', 'effort': 'high', 'second': True}))
        self.assertEqual(advisor.read(), advisor.DEFAULT)

    def test_v2_configurable_second_and_main_off_constraint(self):
        value = choices(second={'agent': 'claude', 'model': 'opus', 'effort': 'medium'})
        advisor.write({key: value[key] for key in ('main', 'second')})
        self.assertEqual(json.loads(advisor.path().read_text()), value)
        self.assertEqual(advisor.second_opinion(value), value['second'])
        for bad in (dict(value, main=dict(advisor.OFF)), dict(value, second=True),
                    dict(value, second={'agent': 'codex', 'model': 'old-model', 'effort': 'ultra'}),
                    dict(value, version=True), dict(value, version=3), dict(value, extra=True)):
            with self.subTest(bad=bad), self.assertRaises(advisor.LevelError):
                advisor.write(bad)
        self.assertEqual(advisor.read(), value)
        self.assertIsNone(advisor.second_opinion(dict(value, main=dict(advisor.OFF))))

    def test_catalog_models_validate_and_latest_resolves_to_family_alias(self):
        provider = {'latest': {'launch': 'opus', 'slug': 'claude-opus-6-1', 'name': 'Opus 6.1'},
                    'models': [{'slug': 'claude-opus-6-1', 'name': 'Opus 6.1', 'efforts': list(delegate.EFFORTS)}]}
        with patch.object(model_catalog, 'claude', return_value=provider):
            advisor.write(choices(model='claude-opus-6-1'))
            self.assertEqual(advisor.read()['main']['model'], 'claude-opus-6-1')
            self.assertEqual(advisor.resolve('claude', 'latest'), 'opus')
            text = autonomy.rules({'level': 5, 'source': 'global'})
            self.assertIn('Claude, Opus 6.1, high effort', text)
            advisor.write(choices())
            self.assertIn('Claude, Opus 6.1 (latest), high effort',
                          autonomy.rules({'level': 5, 'source': 'global'}))

    def test_unavailable_slug_has_specific_error_but_unchecked_is_accepted(self):
        for available in (False, None, True):
            with patch.object(model_catalog, 'claude', return_value={
                    'models': [{'slug': 'claude-mythos-5-2', 'available': available}]}):
                if available is False:
                    with self.assertRaisesRegex(advisor.LevelError, 'That model is not available for your account.'):
                        advisor.write(choices(model='claude-mythos-5-2'))
                else:
                    advisor.write(choices(model='claude-mythos-5-2'))

    def test_claude_choices_round_trip_with_private_permissions(self):
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        for model in ('latest', *(slug for slug, _ in delegate.CLAUDE_MODELS)):
            for effort in ('auto', *delegate.EFFORTS):
                value = choices(model=model, effort=effort)
                advisor.write(value)
                self.assertEqual(advisor.read(), value)
                self.assertEqual(advisor.path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(advisor.path().parent.glob('*.tmp')), [])

    def test_codex_uses_each_models_efforts_including_ultra(self):
        for model, efforts in (('latest', ['auto', 'high', 'ultra']),
                              ('new-model', ['auto', 'high', 'ultra']),
                              ('old-model', ['auto', 'low', 'medium', 'high'])):
            for effort in efforts:
                value = choices(agent='codex', model=model, effort=effort)
                advisor.write(value)
                self.assertEqual(advisor.read(), value)
        for model, effort in (('old-model', 'ultra'), ('latest', 'medium'), ('new-model', 'low'), ('unknown', 'auto')):
            with self.subTest(model=model, effort=effort), self.assertRaises(advisor.LevelError):
                advisor.write(choices(agent='codex', model=model, effort=effort))

    def test_codex_without_cache_accepts_slugs_with_auto_effort(self):
        self.models.return_value = []
        for model in ('latest', 'future-model.v2'):
            advisor.write(choices(agent='codex', model=model, effort='auto'))
            self.assertEqual(advisor.read()['main']['model'], model)
        with self.assertRaises(advisor.LevelError):
            advisor.write(choices(agent='codex', effort='ultra'))
        with self.assertRaises(advisor.LevelError):
            advisor.write(choices(agent='codex', model='bad;command'))

    def test_none_has_only_the_disabled_shape(self):
        advisor.write(OFF)
        self.assertEqual(advisor.read(), OFF)
        for change in ({'model': 'opus'}, {'effort': 'high'}, {'extra': True}):
            with self.assertRaises(advisor.LevelError):
                advisor.write(dict(OFF, main=dict(OFF['main'], **change)))

    def test_bad_input_preserves_settings_and_uses_shared_error(self):
        advisor.write(advisor.DEFAULT)
        bad = [None, [], {}, choices(extra=True)]
        bad += [choices(**change) for change in (
            {'agent': []}, {'agent': 'auto'}, {'agent': True}, {'model': []}, {'model': None},
            {'model': 'new-model'}, {'effort': []}, {'effort': 'ultra'}, {'effort': 'auto '},
            {'extra': 1}, {'extra': 'yes'})]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(autonomy.LevelError) as caught:
                advisor.write(value)
            self.assertEqual((caught.exception.status, str(caught.exception)),
                             (400, 'Choose an advisor, a model and an effort.'))
            self.assertEqual(advisor.read(), advisor.DEFAULT)

    def test_latest_follows_provider_order_and_display_names(self):
        self.assertEqual(advisor.resolve('claude', 'latest'), 'fable')
        self.assertEqual(advisor.resolve('codex', 'latest'), 'new-model')
        self.assertEqual(advisor.resolve('claude', 'opus'), 'opus')
        self.assertEqual(advisor.resolve('codex', 'old-model'), 'old-model')
        self.assertEqual(advisor.latest_names(), {'claude': 'Fable 5.2', 'codex': 'Newest Model'})
        self.models.return_value = list(reversed(MODELS))
        self.assertEqual(advisor.resolve('codex', 'latest'), 'old-model')
        self.models.return_value = []
        self.assertIsNone(advisor.resolve('codex', 'latest'))
        self.assertEqual(advisor.latest_names(), {'claude': 'Fable 5.2', 'codex': None})

    def test_second_uses_other_agent_latest_and_high_effort(self):
        self.assertEqual(advisor.second_opinion(advisor.DEFAULT),
                         {'agent': 'codex', 'model': 'new-model', 'effort': 'high'})
        self.assertEqual(advisor.second_opinion(choices(agent='codex')),
                         {'agent': 'claude', 'model': 'fable', 'effort': 'high'})
        self.assertIsNone(advisor.second_opinion(choices(second=False)))
        self.assertIsNone(advisor.second_opinion(OFF))
        self.models.return_value = []
        self.assertIsNone(advisor.second_opinion(advisor.DEFAULT)['model'])


class Rules(Base):
    def test_levels_and_required_order(self):
        for level in range(6):
            text = autonomy.rules({'level': level, 'source': 'global'})
            for rule in (autonomy.EVIDENCE, autonomy.ASK_USER):
                self.assertEqual(rule in text, level >= 3)
            self.assertEqual('ask the advisor (' in text, level >= 3)
            self.assertEqual('never use it for searches' in text, level >= 3)
            self.assertEqual(autonomy.PRODUCT in text, level >= 4)
            if level >= 3:
                needles = [autonomy.RULES[level], autonomy.EVIDENCE, 'ask the advisor (', autonomy.ASK_USER]
                if level >= 4:
                    needles.append(autonomy.PRODUCT)
                needles.append(autonomy.SAFETY)
                if level >= 4:
                    needles.append(autonomy.URGENT)
                needles.append(autonomy.LAST)
                positions = [text.index(needle) for needle in needles]
                self.assertEqual(positions, sorted(positions))
                self.assertIn('Claude, Fable 5.2 (latest), high effort', text)
                self.assertIn('second advisor (Codex, Newest Model (latest), high effort)', text)
                self.assertIn(f'python3 {cli.__file__} ask "QUESTION"', text)

    def test_none_adds_no_advisor_and_keeps_evidence_product_and_user_rules(self):
        advisor.write(OFF)
        for level in (3, 4, 5):
            text = autonomy.rules({'level': level, 'source': 'global'})
            self.assertIn(autonomy.NO_ADVISOR, text)
            self.assertIn(autonomy.EVIDENCE, text)
            self.assertIn(autonomy.ASK_USER, text)
            self.assertNotIn('ask the advisor (', text)
            self.assertEqual(autonomy.PRODUCT in text, level >= 4)

    def test_resolved_codex_model_and_second_off_instruction(self):
        advisor.write(choices(agent='codex', effort='ultra', second=False))
        text = autonomy.rules({'level': 3, 'source': 'global'})
        self.assertIn('Codex, Newest Model (latest), ultra effort', text)
        self.assertIn('a second advisor is off', text)
        self.assertIn('Add --critical for security, data, money, release or architecture decisions;', text)


class Cli(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'TMUX_PANE': '%1'}))
        self.calls = []
        self.project = PID
        self.live = True
        self.wait_reply = REPLY
        self.wait_code = 0
        self.wait_status = 'finished'
        self.panes = 0
        self.run = self.enterContext(patch.object(cli.subprocess, 'run', side_effect=self.process))

    def process(self, command, **options):
        self.calls.append((command, options))
        if command[0] == paths.TMUX:
            value = command[-1]
            if value == '#{@project}':
                return SimpleNamespace(returncode=0, stdout=self.project)
            if value == '#{session_id}':
                return SimpleNamespace(returncode=0, stdout='$42\n')
            if value == '#{pane_id}':
                return SimpleNamespace(returncode=0 if self.live else 1, stdout=command[-2])
            self.fail(f'Unexpected tmux read: {command}')
        self.assertEqual(command[:2], [sys.executable, str(cli.TAB_CLI)])
        action = command[2]
        if action == 'start':
            self.panes += 1
            result, code = {'pane': f'%{self.panes + 10}', 'name': 'Advisor'}, 0
        elif action == 'send':
            result, code = {'sent': True, 'pane': command[4]}, 0
        elif action == 'wait':
            result, code = {'status': self.wait_status, 'reply': self.wait_reply}, self.wait_code
        else:
            self.fail(f'Unexpected tab command: {command}')
        return SimpleNamespace(returncode=code, stdout=json.dumps(result), stderr='')

    def invoke(self, argv=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(argv or ['ask', 'Which option?'])
        return code, json.loads(output.getvalue())

    def tab_calls(self):
        return [(command, options) for command, options in self.calls if command[0] == sys.executable]

    def test_start_new_tab_and_log_decision(self):
        code, result = self.invoke()
        self.assertEqual((code, result['status'], result['needs_user']), (0, 'decided', False))
        self.assertEqual(result['answer'], 'Use the tested option.')
        self.assertEqual(result['evidence'], 'server/example.py')
        self.assertEqual(result['advisors'][0], {
            'role': 'main', 'agent': 'claude', 'model': 'fable', 'pane': '%11',
            'answer': 'Use the tested option.', 'confidence': 'high', 'needs_user': False, 'raw': REPLY})
        calls = self.tab_calls()
        self.assertEqual(calls[0][0][2:], ['start', '--agent', 'claude', '--model', 'fable', '--effort', 'high',
                                          '--role', 'advisor', '--cwd', os.getcwd(), '-'])
        self.assertEqual(calls[0][1]['input'], cli.PROMPT + 'Which option?')
        self.assertEqual(calls[1][0][2:], ['wait', '--pane', '%11', '--timeout', '600'])
        for _, options in self.calls:
            self.assertLessEqual(options['timeout'], 630)
            self.assertFalse(options.get('shell', False))
        state = json.loads((paths.CACHE / 'advisor' / f'{PID}-1.json').read_text())
        self.assertEqual(state, {'main': {'pane': '%11', 'agent': 'claude', 'model': 'fable', 'effort': 'high'}})
        log = paths.TETHER / 'projects' / PID / 'advisor-log.md'
        self.assertRegex(log.read_text(), r'^- \d{4}-\d\d-\d\d \d\d:\d\d \[decided\] Q: Which option\? '
                                         r'-> A: Use the tested option\. \(claude fable\)\n$')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_reuse_live_pane_sends_prompt_without_starting_or_review_links(self):
        self.invoke()
        self.calls.clear()
        code, result = self.invoke(['ask', 'Next question'])
        self.assertEqual((code, result['advisors'][0]['pane']), (0, '%11'))
        commands = [command for command, _ in self.tab_calls()]
        self.assertEqual([command[2] for command in commands], ['send', 'wait'])
        self.assertEqual(commands[0][2:], ['send', '--pane', '%11', '-'])
        self.assertEqual(self.tab_calls()[0][1]['input'], cli.PROMPT + 'Next question')
        self.assertTrue(any(command[-1] == '#{pane_id}' for command, _ in self.calls))
        self.assertNotIn('--review-it', str(self.calls))
        self.assertNotIn('--reviews-me', str(self.calls))

    def test_dead_pane_or_changed_settings_restart(self):
        self.invoke()
        self.live = False
        self.assertEqual(self.invoke()[1]['advisors'][0]['pane'], '%12')
        self.live = True
        for change in ({'model': 'opus'}, {'effort': 'medium'}, {'agent': 'codex', 'model': 'latest', 'effort': 'high'}):
            settings = advisor.read()
            advisor.write(dict(settings, main=dict(settings['main'], **change)))
            self.calls.clear()
            code, _ = self.invoke()
            self.assertEqual(code, 0)
            self.assertEqual(self.tab_calls()[0][0][2], 'start')

    def test_latest_model_refresh_restarts_codex_advisor(self):
        advisor.write(choices(agent='codex'))
        first = self.invoke()[1]['advisors'][0]
        self.models.return_value = list(reversed(MODELS))
        second = self.invoke()[1]['advisors'][0]
        self.assertNotEqual(first['pane'], second['pane'])
        self.assertEqual(second['model'], 'old-model')

    def test_critical_asks_both_before_waiting_and_leaves_agreement_to_caller(self):
        code, result = self.invoke(['ask', '--critical', '--timeout', '60', 'Which design?'])
        self.assertEqual((code, result['status'], result['agree']), (0, 'decided', None))
        calls = self.tab_calls()
        self.assertEqual([command[2] for command, _ in calls], ['start', 'start', 'wait', 'wait'])
        self.assertIn('second-advisor', calls[1][0])
        self.assertEqual([row['agent'] for row in result['advisors']], ['claude', 'codex'])
        self.assertEqual(result['advisors'][1]['model'], 'new-model')
        self.assertEqual(calls[2][1]['timeout'], 90)
        self.assertEqual(calls[3][1]['timeout'], 90)
        self.calls.clear()
        self.invoke(['ask', '--critical', 'Next design?'])
        self.assertEqual([command[2] for command, _ in self.tab_calls()], ['send', 'send', 'wait', 'wait'])

    def test_critical_uses_configured_second_model_and_effort(self):
        advisor.write(choices(second={'agent': 'claude', 'model': 'opus', 'effort': 'medium'}))
        code, result = self.invoke(['ask', '--critical', 'Which design?'])
        self.assertEqual(code, 0)
        second = result['advisors'][1]
        self.assertEqual((second['agent'], second['model']), ('claude', 'opus'))
        command = self.tab_calls()[1][0]
        self.assertEqual(command[command.index('--effort') + 1], 'medium')

    def test_critical_second_off_is_harmless(self):
        advisor.write(choices(second=False))
        code, result = self.invoke(['ask', '--critical', 'Which design?'])
        self.assertEqual(code, 0)
        self.assertEqual(len(result['advisors']), 1)
        self.assertNotIn('agree', result)

    def test_any_uncertain_reply_requires_user(self):
        for raw in (REPLY.replace('high', 'low'), REPLY.replace('high', 'medium'),
                    REPLY.replace('NEEDS_USER: no', 'NEEDS_USER: yes (business facts)'),
                    'ANSWER: Incomplete', REPLY.replace('EVIDENCE:', 'SOURCES:')):
            self.wait_reply = raw
            code, result = self.invoke()
            self.assertEqual((code, result['status'], result['needs_user']), (7, 'ask_user', True))

    def test_second_uncertain_or_missing_answer_prevents_decision(self):
        original = self.process

        def process(command, **options):
            if 'wait' in command and '%12' in command:
                self.wait_reply = REPLY.replace('high', 'low')
            return original(command, **options)

        self.run.side_effect = process
        code, result = self.invoke(['ask', '--critical', 'Critical choice'])
        self.assertEqual((code, result['confidence'], result['needs_user']), (7, 'low', True))
        self.assertEqual(result['advisors'][0]['confidence'], 'high')

    def test_timeout_exit_code_and_process_timeout(self):
        self.wait_code, self.wait_status = 3, 'timeout'
        code, result = self.invoke()
        self.assertEqual((code, result['status'], result['needs_user']), (3, 'timeout', True))
        self.run.side_effect = subprocess.TimeoutExpired('tab', 60)
        code, result = self.invoke()
        self.assertEqual((code, result['status']), (3, 'timeout'))

    def test_process_timeout_still_waits_for_second(self):
        original = self.process

        def process(command, **options):
            if 'wait' in command and '%11' in command:
                self.calls.append((command, options))
                raise subprocess.TimeoutExpired(command, 60)
            return original(command, **options)

        self.run.side_effect = process
        code, result = self.invoke(['ask', '--critical', 'Critical choice'])
        self.assertEqual((code, result['status']), (3, 'timeout'))
        self.assertEqual(result['advisors'][1]['confidence'], 'high')

    def test_needs_input_is_ask_user_and_gone_is_error(self):
        for wait_code, wait_status, expected in ((5, 'needs_input', 7), (4, 'gone', 1), (6, 'no_turn', 1)):
            self.wait_code, self.wait_status = wait_code, wait_status
            code, result = self.invoke()
            self.assertEqual(code, expected)
            self.assertTrue(result['needs_user'])

    def test_protocol_errors_and_subprocess_errors_return_json(self):
        for response in (SimpleNamespace(returncode=1, stdout='', stderr='Tab unavailable'),
                         SimpleNamespace(returncode=0, stdout='[]', stderr=''),
                         SimpleNamespace(returncode=0, stdout='{}', stderr=''),
                         SimpleNamespace(returncode=1, stdout='{"error":"Guard refused"}', stderr='')):
            with patch.object(cli, 'caller_context', return_value=(PID, PID)):
                self.run.return_value = response
                self.run.side_effect = None
                code, result = self.invoke()
            self.assertEqual((code, result['status']), (1, 'error'))
        self.run.side_effect = OSError('missing tmux')
        self.assertEqual(self.invoke()[0], 1)

    def test_reuse_starts_new_tab_when_ownership_guard_refuses(self):
        self.invoke()
        original = self.process

        def process(command, **options):
            if 'send' in command:
                self.calls.append((command, options))
                return SimpleNamespace(returncode=1, stdout='', stderr='That tab is not a helper of this tab.')
            return original(command, **options)

        self.calls.clear()
        self.run.side_effect = process
        code, result = self.invoke()
        # A tab may only send to helpers it started; after /clear the send fails, so the CLI starts a fresh advisor.
        self.assertEqual((code, result['status']), (0, 'decided'))
        self.assertEqual([command[2] for command, _ in self.tab_calls()], ['send', 'start', 'wait'])

    def test_parse_ignores_footer_after_needs_user(self):
        parsed = cli.parse_reply('ANSWER: Use A.\nCONFIDENCE: high\nEVIDENCE: a.py\nNEEDS_USER: no\n\nNeeds you: no')
        self.assertEqual((parsed['confidence'], parsed['needs_user']), ('high', False))
        parsed = cli.parse_reply('ANSWER: Use A.\nCONFIDENCE: high\nEVIDENCE: a.py\nNEEDS_USER: yes (pricing)\n\nNeeds you: no')
        self.assertTrue(parsed['needs_user'])

    def test_session_fallback_is_safe_and_does_not_log_project(self):
        self.project = '../../outside'
        self.assertEqual(self.invoke()[0], 0)
        self.assertTrue((paths.CACHE / 'advisor' / '42-1.json').exists())
        self.assertFalse(paths.TETHER.exists())

    def test_bad_state_file_starts_a_new_pane(self):
        state = paths.CACHE / 'advisor' / f'{PID}-1.json'
        for text in ('broken', '[]', '{"main":null}', '{"main":{"pane":"bad;command"}}'):
            self.write(state, text)
            self.calls.clear()
            self.assertEqual(self.invoke()[0], 0)
            self.assertEqual(self.tab_calls()[0][0][2], 'start')

    def test_codex_without_models_and_auto_effort_omits_options(self):
        self.models.return_value = []
        advisor.write(choices(agent='codex', effort='auto'))
        self.assertEqual(self.invoke()[0], 0)
        command = self.tab_calls()[0][0]
        self.assertNotIn('--model', command)
        self.assertNotIn('--effort', command)

    def test_stdin_question_and_argument_errors(self):
        with patch.object(sys, 'stdin', io.StringIO('Question from stdin')):
            self.assertEqual(self.invoke(['ask', '-'])[0], 0)
        self.assertEqual(self.tab_calls()[0][1]['input'], cli.PROMPT + 'Question from stdin')
        self.run.reset_mock()
        for args in (['ask', '--timeout', '59', 'q'], ['ask', '--timeout', '601', 'q'],
                     ['ask', '--timeout', 'bad', 'q'], ['ask', ' '], ['ask']):
            self.assertEqual(self.invoke(args)[0], 1)
        self.run.assert_not_called()

    def test_off_is_exit_eight_and_needs_no_tab_or_tmux(self):
        advisor.write(OFF)
        with patch.dict(os.environ, {'TMUX_PANE': ''}):
            self.assertEqual(self.invoke(), (8, {'status': 'off'}))
        self.run.assert_not_called()
        self.assertEqual(self.invoke(), (8, {'status': 'off'}))
        self.assertEqual(self.tab_calls(), [])
        self.assertIn('[off]', (paths.TETHER / 'projects' / PID / 'advisor-log.md').read_text())
        self.run.side_effect = OSError('missing tmux')
        self.assertEqual(self.invoke(), (8, {'status': 'off'}))

    def test_requires_valid_tmux_pane(self):
        for pane in ('', 'invalid', '%1;command'):
            with patch.dict(os.environ, {'TMUX_PANE': pane}):
                code, result = self.invoke()
            self.assertEqual((code, result['status']), (1, 'error'))
        self.run.assert_not_called()

    def test_log_caps_entries_and_flattens_bounds_question_and_answer(self):
        log = paths.TETHER / 'projects' / PID / 'advisor-log.md'
        self.write(log, ''.join(f'- old {index}\n' for index in range(205)))
        self.wait_reply = REPLY.replace('Use the tested option.', 'a' * 400 + '\nmore')
        self.invoke(['ask', 'q' * 250 + '\nnext'])
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[0], '- old 6')
        self.assertIn('Q: ' + 'q' * 200 + ' -> A: ' + 'a' * 300 + ' (claude fable)', lines[-1])

    def test_raw_reply_retains_last_four_thousand_characters(self):
        self.wait_reply = 'Context ' * 1000 + '\n' + REPLY
        code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result['advisors'][0]['raw'], self.wait_reply[-4000:])


class Parse(unittest.TestCase):
    def test_last_label_wins_case_insensitive_and_evidence_spans_lines(self):
        raw = REPLY + '\nanswer: New answer\nconfidence: HIGH\nevidence: first.py\nsecond.py\nneeds_user: NO'
        self.assertEqual(cli.parse_reply(raw), {'answer': 'New answer', 'confidence': 'high',
                                              'evidence': 'first.py\nsecond.py', 'needs_user': False})

    def test_missing_empty_or_invalid_labels_fail_closed(self):
        for label in ('ANSWER', 'CONFIDENCE', 'EVIDENCE', 'NEEDS_USER'):
            raw = '\n'.join(line for line in REPLY.splitlines() if not line.startswith(label + ':'))
            self.assertEqual(cli.parse_reply(raw)['confidence'], 'low')
            self.assertTrue(cli.parse_reply(raw)['needs_user'])
        for raw in (REPLY.replace('high', 'certain'), REPLY.replace('NEEDS_USER: no', 'NEEDS_USER: maybe'),
                    REPLY.replace('Use the tested option.', '')):
            self.assertEqual(cli.parse_reply(raw)['confidence'], 'low')
            self.assertTrue(cli.parse_reply(raw)['needs_user'])


if __name__ == '__main__':
    unittest.main()
