"""Autonomy settings use temporary paths and mocked processes, never live tmux."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import autonomy
import advisor
import approval
import codex_level
import decisions
import lessons
import paths
import model_catalog
import server
from test_approval import HAS_CRYPTO, Handler as PasskeyHandler, PasskeyCase

PID = 'abcdef012345'


class Handler:
    def __init__(self, path=autonomy.PATH, body=None):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = path
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


def reviewers(**changes):
    return {tier: dict(autonomy.TIER_DEFAULT, **changes.get(tier, {})) for tier in autonomy.TIERS}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('CONFIG', 'CACHE', 'TETHER', 'CODEX', 'CLAUDE'):
            patch = mock.patch.object(paths, name, self.root / name.lower())
            patch.start()
            self.addCleanup(patch.stop)
        self.enterContext(mock.patch.object(model_catalog.shutil, 'which', return_value=None))
        models = mock.patch.object(codex_level, 'models', return_value=[])
        models.start()
        self.addCleanup(models.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


class Resolve(Base):
    def test_missing_and_bad_global_files_mean_standard(self):
        self.assertEqual(autonomy.resolve(PID), {'level': 2, 'source': 'global', 'global': 2, 'project': None})
        for text in ('7', 'two', '', '-1', '2 3', 'true'):
            with self.subTest(text=text):
                self.write(autonomy.global_path(), text)
                self.assertEqual(autonomy.read_global(), 2)

    def test_override_on_beats_global_and_can_lower_it(self):
        autonomy.set_global(4)
        autonomy.set_project(PID, True, 1)
        self.assertEqual(autonomy.global_path().read_text(), '4\n')
        self.assertEqual(autonomy.resolve(PID), {'level': 1, 'source': 'project', 'global': 4,
                                               'project': {'override': True, 'level': 1}})

    def test_override_off_retains_project_level(self):
        autonomy.set_global(1)
        autonomy.set_project(PID, False, 5)
        self.assertEqual(autonomy.resolve(PID), {'level': 1, 'source': 'global', 'global': 1,
                                               'project': {'override': False, 'level': 5}})

    def test_bad_project_files_and_ids_are_ignored(self):
        for text in ('bad json', '[]', '{"override": true, "level": 9}', '{"override": "yes", "level": 3}',
                     '{"override": true, "level": true}', '{"level": 3}'):
            self.write(autonomy.project_path(PID), text)
            self.assertIsNone(autonomy.read_project(PID), text)
        self.assertEqual(autonomy.resolve('../../etc')['level'], 2)

    def test_invalid_settings_do_not_write(self):
        for level in (6, -1, '2', True, None, 2.0, [], {}):
            with self.subTest(level=level), self.assertRaises(autonomy.LevelError):
                autonomy.set_global(level)
        for pid, override, level in ((PID, 'on', 2), ('../x', True, 2), (PID, True, False)):
            with self.assertRaises(autonomy.LevelError):
                autonomy.set_project(pid, override, level)
        self.assertFalse(paths.CONFIG.exists())
        self.assertFalse(paths.TETHER.exists())


class Rules(Base):
    def test_each_level_has_its_rules_and_safety_and_last_line(self):
        safety = ('At every level, ask before you delete data, force-push, run a destructive migration, spend money, '
                  'make a public release, or send a message to another person.')
        for level, (name, _) in enumerate(autonomy.LEVELS):
            with self.subTest(level=level):
                text = autonomy.rules({'level': level, 'source': 'global'})
                self.assertEqual(text.splitlines()[0],
                                 f'Autonomy level {level} of 5 ({name}), set by the user for all projects.')
                self.assertIn(autonomy.RULES[level], text)
                self.assertIn(safety, text.splitlines())
                self.assertEqual('Needs you: yes' in text, level >= 4)
                self.assertEqual('Urgent: <the question in one sentence>' in text, level >= 4)
                self.assertNotIn('Before you ask the user, read', text)
                self.assertEqual(text.splitlines()[-1], 'Do not change the level. The user sets it in Tether.')

    def test_decisions_replace_assumptions_and_sensible_defaults_at_high_levels(self):
        self.assertTrue(autonomy.RULES[3].startswith('Decide from the evidence and continue.'))
        self.assertIn('Decide from the evidence, list each decision in the final reply', autonomy.RULES[4])
        self.assertIn('list each decision in the final reply', autonomy.RULES[5])
        for level in (3, 4, 5):
            self.assertNotIn('sensible defaults', autonomy.RULES[level])
            self.assertNotIn('assumptions', autonomy.RULES[level])

    def test_saved_answers_only_at_levels_three_to_five_for_the_selected_project(self):
        folder = paths.TETHER / 'projects' / PID
        folder.mkdir(parents=True)
        other = '012345abcdef'
        (folder.parent / other).mkdir()
        legacy = paths.CONFIG / 'decisions.md'
        self.write(legacy, 'Old global decisions.')
        for level in range(6):
            resolved = {'level': level, 'source': 'project'}
            self.assertNotIn('The user saved answers', autonomy.rules(resolved, PID))
        row = decisions.record(PID, 'Choose?', 'Default')
        expected = (f'The user saved answers for this project in {decisions.path(PID).resolve()}. '
                    'Reuse one only when it answers the same question; if in doubt, ask.')
        for level in range(6):
            resolved = {'level': level, 'source': 'project'}
            text = autonomy.rules(resolved, PID)
            self.assertIn('set by the user for this project.', text)
            self.assertEqual(expected in text, level >= 3)
            self.assertNotIn('The user saved answers', autonomy.rules(resolved, other))
            self.assertNotIn('The user saved answers', autonomy.rules(resolved))
            self.assertNotIn(str(legacy), text)
        decisions.delete(PID, row['id'])
        self.assertNotIn('The user saved answers', autonomy.rules({'level': 5, 'source': 'global'}, PID))
        self.assertEqual(legacy.read_text(), 'Old global decisions.')

    def test_missing_project_does_not_break_rules(self):
        self.assertNotIn('The user saved answers', autonomy.rules({'level': 5, 'source': 'global'}, PID))


class Reviewers(Base):
    def test_missing_or_bad_file_means_all_auto(self):
        self.assertEqual(autonomy.read_reviewers(), reviewers())
        for text in ('not json', '[]', '{}', json.dumps(reviewers(normal={'agent': 'bad'}))):
            self.write(autonomy.reviewers_path(), text)
            self.assertEqual(autonomy.read_reviewers(), reviewers())
        first = autonomy.read_reviewers()
        first['normal']['effort'] = 'high'
        self.assertEqual(first['light']['effort'], 'auto')
        self.assertEqual(autonomy.read_reviewers(), reviewers())

    def test_valid_choices_round_trip(self):
        for agent, models in (('auto', ('auto',)), ('none', ('auto',)),
                              ('claude', ('auto', 'latest', *autonomy.CLAUDE_REVIEW_MODELS)),
                              ('codex', ('auto', 'latest', 'gpt-6.1-sol', 'new_slug.1'))):
            for model in models:
                for effort in (('auto',) if agent == 'none' else ('auto', 'low', 'medium', 'high', 'xhigh', 'max')):
                    with self.subTest(agent=agent, model=model, effort=effort):
                        value = reviewers(normal={'agent': agent, 'model': model, 'effort': effort})
                        autonomy.set_reviewers(value)
                        self.assertEqual(autonomy.read_reviewers(), value)

    def test_invalid_shapes_and_choices_are_rejected(self):
        invalid_tiers = [None, [], {}, {'agent': 'auto'}, dict(autonomy.TIER_DEFAULT, extra=True)]
        invalid_tiers += [dict(autonomy.TIER_DEFAULT, **change) for change in (
            {'agent': 'other'}, {'agent': []}, {'agent': True}, {'model': None}, {'model': []},
            {'model': '-bad'}, {'model': 'x' * 65}, {'model': 'x; command'}, {'effort': 'ultra'},
            {'effort': 'auto '}, {'effort': []}, {'effort': True}, {'agent': 'auto', 'model': 'opus'},
            {'agent': 'none', 'model': 'opus'}, {'agent': 'none', 'effort': 'low'},
            {'agent': 'claude', 'model': 'gpt-6.1-sol'}, {'agent': 'codex', 'model': '-bad'})]
        for tier in invalid_tiers:
            value = reviewers()
            value['normal'] = tier
            with self.subTest(tier=tier), self.assertRaises(autonomy.LevelError) as raised:
                autonomy.set_reviewers(value)
            self.assertEqual(raised.exception.status, 400)
        for value in (None, [], {}, {'critical': dict(autonomy.TIER_DEFAULT)}, dict(reviewers(), extra={})):
            with self.subTest(value=value), self.assertRaises(autonomy.LevelError):
                autonomy.set_reviewers(value)

    def test_codex_model_must_be_in_nonempty_cache(self):
        value = reviewers(critical={'agent': 'codex', 'model': 'listed'})
        autonomy.set_reviewers(value)  # no cache accepts any slug
        with mock.patch.object(codex_level, 'models', return_value=[{'slug': 'listed', 'name': 'Listed'}]):
            autonomy.set_reviewers(value)
            with self.assertRaises(autonomy.LevelError):
                autonomy.set_reviewers(reviewers(critical={'agent': 'codex', 'model': 'unlisted'}))
        with mock.patch.object(codex_level, 'models', return_value=[{'slug': 'different', 'name': 'Different'}]):
            self.assertEqual(autonomy.read_reviewers(), reviewers())

    def test_default_algorithm_and_override_lines(self):
        default = autonomy.reviewer_rules(reviewers())
        self.assertTrue(default.startswith('Choose a reviewer by the risk of the task:\n- Critical:'))
        self.assertIn('with the strongest model and high effort.', default)
        self.assertIn('with its default model and medium effort.', default)
        self.assertIn('Do not start a reviewer tab. Review the change yourself.', default)
        self.assertIn(f'`python3 {Path(autonomy.__file__).parent / "agent_tab_cli.py"} start --reviews-me '
                      '--agent AGENT [--model MODEL] [--effort EFFORT]`.', default)
        self.assertNotIn('The user set', default)
        value = reviewers(critical={'agent': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'xhigh'},
                          normal={'effort': 'high'}, light={'agent': 'none'})
        self.assertEqual(autonomy.reviewer_rules(value), default + '\n' + '\n'.join((
            'The user set the Critical reviewer: Codex, model gpt-6.1-sol, effort xhigh. This overrides the default.',
            'The user set the Normal reviewer effort: high.',
            'The user set the Light reviewer: none. Do not start a reviewer for light tasks.')))

    def test_codex_efforts_follow_the_chosen_model_and_latest_resolves(self):
        models = [{'slug': 'new', 'name': 'New', 'efforts': ['high', 'ultra']},
                  {'slug': 'old', 'name': 'Old', 'efforts': ['medium']}]
        with mock.patch.object(codex_level, 'models', return_value=models):
            for model in ('auto', 'latest', 'new'):
                value = reviewers(normal={'agent': 'codex', 'model': model, 'effort': 'ultra'})
                autonomy.set_reviewers(value)
                self.assertEqual(autonomy.read_reviewers(), value)
            for model, effort in (('old', 'ultra'), ('new', 'medium'), ('latest', 'medium')):
                with self.assertRaises(autonomy.LevelError):
                    autonomy.set_reviewers(reviewers(normal={'agent': 'codex', 'model': model, 'effort': effort}))
            text = autonomy.reviewer_rules(reviewers(
                critical={'agent': 'claude', 'model': 'latest'}, normal={'agent': 'codex', 'model': 'latest'}))
            self.assertIn('Critical reviewer: Claude, model fable.', text)
            self.assertIn('Normal reviewer: Codex, model new.', text)

    def test_catalog_claude_slugs_and_existing_aliases_validate(self):
        with mock.patch.object(model_catalog, 'claude', return_value={
                'models': [{'slug': 'claude-opus-6-2'}], 'latest': {'launch': 'opus'}}):
            for model in ('claude-opus-6-2', 'opus', 'latest'):
                value = reviewers(normal={'agent': 'claude', 'model': model})
                autonomy.set_reviewers(value)
                self.assertEqual(autonomy.read_reviewers(), value)
            self.assertIn('Normal reviewer: Claude, model opus.', autonomy.reviewer_rules(value))

    def test_override_omits_auto_parts(self):
        for choice, expected in (
                ({'agent': 'claude'}, 'Claude'),
                ({'agent': 'claude', 'model': 'opus'}, 'Claude, model opus'),
                ({'agent': 'codex', 'effort': 'max'}, 'Codex, effort max')):
            text = autonomy.reviewer_rules(reviewers(normal=choice))
            self.assertEqual(text.splitlines()[-1],
                             f'The user set the Normal reviewer: {expected}. This overrides the default.')


class Hook(Base):
    def test_hook_shape_project_and_both_context_parts(self):
        autonomy.set_global(1)
        autonomy.set_project(PID, True, 3)
        decisions.record(PID, 'Choose?', 'Default')
        tmux = mock.Mock(side_effect=lambda *args: SimpleNamespace(returncode=0,
                         stdout='' if args[-1] == '#{@tether_helper}' else PID + '\n'))
        out = json.loads(autonomy.hook_output({'cwd': '/work'}, {'TMUX': '/tmux', 'TMUX_PANE': '%7'}, tmux))
        self.assertEqual(out, {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext':
                         autonomy.rules(autonomy.resolve(PID), PID) + '\n\n' + autonomy.reviewer_rules(reviewers())}})
        tmux.assert_any_call('display-message', '-p', '-t', '%7', '#{@project}')
        tmux.assert_any_call('display-message', '-p', '-t', '%7', '#{@tether_helper}')

    def test_codex_full_context_size_and_no_claude_only_features(self):
        autonomy.set_global(5)
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        for index in range(3):
            decisions.record(PID, f'Question {index}?', f'Answer {index}.')
        for index in range(8):
            lessons.add(PID, 'When case ' + str(index) + ' appears, ' + 'x' * 129, 'avoid', 'user', 'codex', force_new=True)
        models = [{'slug': 'gpt-new', 'name': 'GPT New', 'efforts': ['high', 'ultra']}]
        with mock.patch.object(codex_level, 'models', return_value=models):
            advisor.write(advisor.DEFAULT)
            autonomy.set_reviewers(reviewers(**{tier: {'agent': 'codex', 'model': 'latest', 'effort': 'ultra'}
                                                for tier in autonomy.TIERS}))
            tmux = mock.Mock(side_effect=lambda *args: SimpleNamespace(returncode=0,
                             stdout='' if args[-1] == '#{@tether_helper}' else PID))
            out = json.loads(autonomy.hook_output({'cwd': '/work'}, {'TMUX': '/fixture', 'TMUX_PANE': '%7'}, tmux, agent='codex'))
        text = out['hookSpecificOutput']['additionalContext']
        selected = lessons.for_prompt(PID)
        rows = selected['confirmed'] + selected['trial']
        self.assertEqual(len(rows), 8)
        self.assertEqual([len(row['text']) for row in rows], [150] * 8)
        self.assertIn('Lessons from past feedback', text)
        self.assertLess(len(text), 9000)
        self.assertIn('Autonomy level 5', text)
        self.assertIn('second advisor (Codex, GPT New (latest), high effort)', text)
        self.assertIn('The user saved answers', text)
        for tier in autonomy.TIERS:
            self.assertIn(f'{tier.title()} reviewer: Codex, model gpt-new, effort ultra', text)
        for word in ('AskUserQuestion', 'subagent', 'Claude Code', 'UserPromptSubmit', 'hooks'):
            self.assertNotIn(word, text)
        self.assertIn('agent_tab_cli.py', text)
        for level in range(6):
            text = autonomy.rules({'level': level, 'source': 'global'}, agent='codex')
            for word in ('AskUserQuestion', 'subagent', 'Claude Code', 'hooks'):
                self.assertNotIn(word, text)

    def test_tmux_failure_uses_global(self):
        for error in (OSError('missing tmux'), subprocess.TimeoutExpired('tmux', 2)):
            out = json.loads(autonomy.hook_output({'cwd': '/work'}, {}, mock.Mock(side_effect=error)))
            self.assertIn('Autonomy level 2 of 5', out['hookSpecificOutput']['additionalContext'])

    def test_script_hook_fails_open_and_show_prints_both_parts(self):
        env = dict(os.environ, MOBILE_TMUX_CONFIG=str(paths.CONFIG), TETHER_HOME=str(paths.TETHER),
                   MOBILE_TMUX_TMUX='/bin/false')
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        for raw in ('{"cwd": "/nowhere-at-all"}', '', '[]'):
            done = subprocess.run([sys.executable, autonomy.__file__, 'hook'], input=raw,
                                  capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(done.returncode, 0)
            self.assertIn('Autonomy level 2 of 5', json.loads(done.stdout)['hookSpecificOutput']['additionalContext'])
        bad = subprocess.run([sys.executable, autonomy.__file__, 'hook'], input='{not json',
                             capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual((bad.returncode, bad.stdout), (0, ''))
        shown = subprocess.run([sys.executable, autonomy.__file__, 'show'],
                               capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(shown.stdout, autonomy.rules(autonomy.resolve()) + '\n\n' +
                         autonomy.reviewer_rules(reviewers()) + '\n')


class Http(Base):
    def setUp(self):
        super().setUp()
        # These tests isolate the existing route contracts. Real passkey gates are tested below.
        patcher = mock.patch.object(approval, 'require')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_state_includes_project_answers_only_when_project_is_given(self):
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        self.assertEqual(autonomy.state(PID)['decisions'], [])
        row = decisions.record(PID, 'Choose?', 'Default')
        self.assertEqual(autonomy.state(PID)['decisions'], [row])
        self.assertNotIn('decisions', autonomy.state())
        handler = Handler(f'{autonomy.PATH}?project={PID}')
        autonomy.get(handler)
        self.assertEqual((handler.status, handler.body['decisions']), (200, [row]))

    def test_setup_get_and_each_explicit_action(self):
        route = autonomy.PATH + '/setup'
        status = {'hook': {'state': 'missing'}, 'ping': {'state': 'off'}}
        with mock.patch.object(autonomy, 'setup_state', return_value=status):
            handler = Handler(route)
            autonomy.get(handler)
            self.assertEqual((handler.status, handler.body), (200, status))
            for body, function, args in (
                    ({'action': 'hook'}, 'install_hook', ()),
                    ({'action': 'ping', 'preset': 'briskbot', 'fields': {'mention': 'user'},
                      'base_url': 'https://tether.test'}, 'save_ping',
                     ('briskbot', {'mention': 'user'}, 'https://tether.test')),
                    ({'action': 'ping-off'}, 'disable_ping', ()),
                    ({'action': 'ping-test'}, 'test_ping', ())):
                with self.subTest(action=body['action']), mock.patch.object(
                        autonomy.autonomy_setup, function, return_value={'ok': True, 'code': 0, 'detail': ''}) as action:
                    handler = Handler(route, body)
                    autonomy.post(handler)
                    action.assert_called_once_with(*args)
                    expected = dict(status)
                    if body['action'] == 'ping-test':
                        expected['test'] = {'ok': True, 'code': 0, 'detail': ''}
                    self.assertEqual((handler.status, handler.body), (200, expected))

    def test_setup_validation_errors_preserve_the_reason(self):
        for action, function in (('hook', 'install_hook'), ('ping', 'save_ping'),
                                 ('ping-off', 'disable_ping'), ('ping-test', 'test_ping')):
            with self.subTest(action=action), mock.patch.object(
                    autonomy.autonomy_setup, function, side_effect=ValueError('Specific reason.')):
                handler = Handler(autonomy.PATH + '/setup', {'action': action})
                autonomy.post(handler)
                self.assertEqual((handler.status, handler.body), (400, {'error': 'Specific reason.'}))
        handler = Handler(autonomy.PATH + '/setup', {'action': 'unknown'})
        autonomy.post(handler)
        self.assertEqual(handler.status, 400)

    def test_save_answer_uses_the_panes_project_and_delete_round_trip(self):
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        route = autonomy.PATH + '/decisions'
        handler = Handler(route, {'pane': '%12', 'project': '000000000000',
                                  'question': 'Choose?', 'answer': 'Default'})
        with mock.patch.object(codex_level, '_tmux', return_value=SimpleNamespace(
                returncode=0, stdout=PID + '\n')) as tmux:
            autonomy.post(handler)
        tmux.assert_called_once_with('display-message', '-p', '-t', '%12', '#{@project}')
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.body['project'], PID)
        row = handler.body['entry']
        self.assertEqual(decisions.entries(PID), [row])
        for expected in (200, 404):
            handler = Handler(route, {'project': PID, 'delete': row['id']})
            autonomy.post(handler)
            self.assertEqual(handler.status, expected)
            if expected == 200:
                self.assertEqual(handler.body, {'decisions': []})

    def test_save_answer_missing_project_bad_pane_and_bad_entries(self):
        route = autonomy.PATH + '/decisions'
        for shown in (SimpleNamespace(returncode=0, stdout=''), SimpleNamespace(returncode=1, stdout=PID),
                      SimpleNamespace(returncode=0, stdout='../x'), SimpleNamespace(returncode=0, stdout=PID)):
            with self.subTest(shown=shown), mock.patch.object(codex_level, '_tmux', return_value=shown):
                handler = Handler(route, {'pane': '%12', 'question': 'Choose?', 'answer': 'Default'})
                autonomy.post(handler)
                self.assertEqual((handler.status, handler.body), (409, {'error':
                    'This tab is not in a Tether project, so Tether cannot save the answer.'}))
        for error in (OSError('no tmux'), subprocess.TimeoutExpired('tmux', 2)):
            with mock.patch.object(codex_level, '_tmux', side_effect=error):
                handler = Handler(route, {'pane': '%12', 'question': 'Choose?', 'answer': 'Default'})
                autonomy.post(handler)
                self.assertEqual(handler.status, 409)
        with mock.patch.object(codex_level, '_tmux') as tmux:
            for pane in (None, [], '%12;sh', '%12\n', '12', '%-1'):
                handler = Handler(route, {'pane': pane, 'question': 'Choose?', 'answer': 'Default'})
                autonomy.post(handler)
                self.assertEqual(handler.status, 400)
            tmux.assert_not_called()
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        with mock.patch.object(codex_level, '_tmux', return_value=SimpleNamespace(returncode=0, stdout=PID)):
            for change in ({'question': ''}, {'answer': '\x00\n'}, {'answer': []}):
                handler = Handler(route, dict({'pane': '%12', 'question': 'Choose?', 'answer': 'Default'}, **change))
                autonomy.post(handler)
                self.assertEqual(handler.status, 400)
        for body in ({'project': '../x', 'delete': '1234abcd'}, {'project': PID, 'delete': None}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual(handler.status, 400)

    def test_setup_and_decisions_body_limits(self):
        for route, limit in ((autonomy.PATH + '/setup', 16384), (autonomy.PATH + '/decisions', 16384)):
            for size, expected in ((limit, 400), (limit + 1, 413)):
                handler = Handler(route)
                handler.rfile = io.BytesIO(b'{}' + b' ' * (size - 2))
                handler.headers['Content-Length'] = str(size)
                autonomy.post(handler)
                self.assertEqual(handler.status, expected)
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        handler = Handler(autonomy.PATH + '/decisions', {'pane': '%12', 'question': 'q', 'answer': 'a' * 5000})
        with mock.patch.object(codex_level, '_tmux', return_value=SimpleNamespace(returncode=0, stdout=PID)):
            autonomy.post(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(len(handler.body['entry']['answer']), 500)

    def test_get_and_post_round_trips(self):
        handler = Handler()
        autonomy.get(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual((handler.body['global'], handler.body['level'], len(handler.body['levels'])), (2, 2, 6))
        self.assertEqual(handler.body['reviewers'], reviewers())
        self.assertEqual(handler.body['tiers'], ['critical', 'normal', 'light'])
        self.assertEqual(handler.body['advisor'], {key: advisor.DEFAULT[key] for key in ('main', 'second')})
        self.assertEqual(handler.body['catalog']['claude']['latest']['name'], 'Fable 5.2')
        self.assertEqual(handler.body['latest'], {'claude': 'Fable 5.2', 'codex': None})
        self.assertEqual(handler.body['efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertEqual(handler.body['models'], {'claude': [{'slug': s, 'name': n} for s, n in autonomy.delegate.CLAUDE_MODELS], 'codex': []})
        handler = Handler(body={'level': 4})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['global']), (200, 4))
        handler = Handler(body={'project': PID, 'override': True, 'level': 1})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['level'], handler.body['source']), (200, 1, 'project'))
        handler = Handler(f'{autonomy.PATH}?project={PID}')
        autonomy.get(handler)
        self.assertEqual(handler.body['project'], {'override': True, 'level': 1})
        value = reviewers(light={'agent': 'claude', 'model': 'haiku'})
        handler = Handler(body={'reviewers': value})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['reviewers'], handler.body['global']), (200, value, 4))
        handler = Handler()
        autonomy.get(handler)
        self.assertEqual(handler.body['reviewers'], value)

    def test_codex_model_state_shape(self):
        with mock.patch.object(codex_level, 'models', return_value=[
                {'slug': 'gpt-test', 'name': 'Test', 'efforts': ['low', 'high', 'ultra'], 'fast': True}]):
            self.assertEqual(autonomy.state()['models']['codex'],
                             [{'slug': 'gpt-test', 'name': 'Test', 'efforts': ['low', 'high', 'ultra']}])

    def test_advisor_post_round_trip_and_validation_without_passkey_gate(self):
        value = {'main': dict(advisor.DEFAULT['main'], model='opus'), 'second': dict(advisor.OFF)}
        with mock.patch.object(approval, 'require') as require:
            handler = Handler(body={'advisor': value})
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['advisor']), (200, value))
            self.assertEqual(handler.body['level'], 2)
            require.assert_not_called()
        handler = Handler(body={'advisor': dict(value, main=dict(value['main'], effort='ultra'))})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body),
                         (400, {'error': 'Choose an advisor, a model and an effort.'}))
        self.assertEqual(advisor.read(), dict(value, version=2))

    def test_bad_requests(self):
        for body in ({'level': 9}, {'level': '2'}, {'level': True}, {'project': 'x', 'override': True, 'level': 1},
                     {'project': PID, 'override': 'yes', 'level': 1}, {'reviewers': {}}, [1]):
            handler = Handler(body=body)
            autonomy.post(handler)
            self.assertEqual(handler.status, 400, body)
        handler = Handler(autonomy.PATH + '?project=../x')
        autonomy.get(handler)
        self.assertEqual(handler.status, 400)
        self.assertTrue(autonomy.handles(autonomy.PATH + '?project=abc'))
        self.assertFalse(autonomy.handles(autonomy.PATH + '-other'))

    def test_bad_json_sizes_and_save_failure(self):
        for size in ('0', '-1', '16385'):
            handler = Handler(body={'level': 1})
            handler.headers['Content-Length'] = size
            autonomy.post(handler)
            self.assertEqual(handler.status, 413)
        for raw in (b'{bad json', b'\xff'):
            handler = Handler()
            handler.rfile = io.BytesIO(raw)
            handler.headers['Content-Length'] = str(len(raw))
            autonomy.post(handler)
            self.assertEqual(handler.status, 400)
        handler = Handler(body={'level': 1})
        with mock.patch.object(autonomy, '_write', side_effect=OSError('disk full')):
            autonomy.post(handler)
        self.assertEqual(handler.status, 500)

    def test_server_post_dispatch_preserves_auth_and_origin_guards(self):
        handler = object.__new__(server.Handler)
        handler.command = 'POST'
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'}
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        with mock.patch.object(autonomy, 'post') as post:
            for route in (autonomy.PATH, autonomy.PATH + '/setup', autonomy.PATH + '/decisions', autonomy.PATH + '/lessons', autonomy.PATH + '/review'):
                handler.path = route
                handler.headers.update({'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'})
                handler.deny.return_value = False
                handler.do_POST()
                post.assert_called_once_with(handler)
                post.reset_mock()
                for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                    handler.headers.update(headers)
                    handler.do_POST()
                    handler.respond.assert_called_with(403, {'error': 'Reload the page and try again.'})
                post.assert_not_called()
                handler.deny.return_value = True
                handler.do_POST()
                post.assert_not_called()

    def test_server_get_dispatch_preserves_auth_guards(self):
        handler = object.__new__(server.Handler)
        handler.command = 'GET'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        with mock.patch.object(autonomy, 'get') as get:
            for route in (autonomy.PATH, autonomy.PATH + '/setup', autonomy.PATH + '/decisions', autonomy.PATH + '/lessons', autonomy.PATH + '/review'):
                handler.path = route
                self.assertTrue(autonomy.handles(route + '?project=' + PID))
                handler.deny.return_value = False
                handler.do_GET()
                get.assert_called_once_with(handler)
                get.reset_mock()
                handler.deny.return_value = True
                handler.do_GET()
                get.assert_not_called()
        self.assertFalse(autonomy.handles(autonomy.PATH + '/setup-other'))


@unittest.skipUnless(HAS_CRYPTO, 'cryptography is not installed')
class LevelApproval(PasskeyCase):
    def post(self, body):
        handler = PasskeyHandler(autonomy.PATH, body)
        autonomy.post(handler)
        return handler

    def test_global_raise_refused_without_key_or_approval_and_accepted_with_assertion(self):
        payload = {'level': 4}
        handler = self.post(payload)
        self.assertEqual((handler.status, handler.body), (403, {'error': approval.NO_PASSKEY, 'approval': 'no-passkey'}))
        self.assertFalse(autonomy.global_path().exists())
        self.add()
        handler = self.post(payload)
        self.assertEqual((handler.status, handler.body), (403, {'error': approval.APPROVE, 'approval': 'required'}))
        self.assertFalse(autonomy.global_path().exists())
        handler = self.post(dict(payload, approval=self.assertion('level-raise', payload)))
        self.assertEqual((handler.status, autonomy.read_global()), (200, 4))
        self.assertEqual(self.post({'level': 1}).status, 200)
        self.assertEqual(self.post({'level': 1}).status, 200)

    def test_project_raises_and_disabling_lower_override_require_approval(self):
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        self.add()
        autonomy.set_global(4)
        for current, payload in (
                (None, {'project': PID, 'override': True, 'level': 5}),
                ({'override': True, 'level': 1}, {'project': PID, 'override': True, 'level': 2}),
                ({'override': True, 'level': 1}, {'project': PID, 'override': False, 'level': 0})):
            with self.subTest(current=current, payload=payload):
                autonomy.project_path(PID).unlink(missing_ok=True)
                if current:
                    autonomy.set_project(PID, **current)
                handler = self.post(payload)
                self.assertEqual(handler.status, 403)
                self.assertEqual(autonomy.read_project(PID), current)
                handler = self.post(dict(payload, approval=self.assertion('level-raise', payload)))
                self.assertEqual(handler.status, 200, handler.body)
                self.assertEqual(autonomy.read_project(PID), {k: payload[k] for k in ('override', 'level')})

    def test_lower_equal_disabled_override_and_reviewers_are_open(self):
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        autonomy.set_global(3)
        for payload in ({'project': PID, 'override': True, 'level': 2},
                        {'project': PID, 'override': True, 'level': 2},
                        {'project': PID, 'override': False, 'level': 5},
                        {'reviewers': reviewers()}, {'advisor': {'main': dict(advisor.DEFAULT['main']), 'second': dict(advisor.OFF)}}):
            if payload.get('override') is False:
                autonomy.set_global(1)
            handler = self.post(payload)
            self.assertEqual(handler.status, 200, handler.body)
        self.assertFalse((paths.CONFIG / 'passkeys.json').exists())

    def test_raise_uses_current_stored_level_and_payload_cannot_change(self):
        self.add()
        payload = {'level': 5}
        proof = self.assertion('level-raise', payload)
        handler = self.post({'level': 4, 'approval': proof})
        self.assertEqual(handler.status, 403)
        self.assertFalse(autonomy.global_path().exists())
        autonomy.set_global(1)
        proof = self.assertion('level-raise', {'level': 2})
        autonomy.set_global(0)
        self.assertEqual(self.post({'level': 2}).status, 403)
        self.assertEqual(self.post({'level': 2, 'approval': proof}).status, 200)


class Learning(Base):
    def setUp(self):
        super().setUp()
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)

    def add(self, text='When checks fail, read the exact output.', kind='avoid'):
        return lessons.add(PID, text, kind, 'user', 'codex', force_new=True)['lesson']

    def test_every_level_learns_and_keeps_order_and_agent_memory(self):
        avoid = self.add()
        keep = self.add('When checks pass, preserve the useful evidence.', 'keep')
        for agent in ('claude', 'codex'):
            for level in range(6):
                text = autonomy.rules({'level': level, 'source': 'project'}, PID, agent, prompt='wrong again?')
                self.assertIn('Learn from feedback.', text)
                self.assertIn(f'- Avoid ({avoid["id"]}): When checks fail', text)
                self.assertIn(f'- Keep doing ({keep["id"]}): When checks pass', text)
                self.assertEqual('Do this as well as your own memory.' in text, agent == 'claude')
                needles = [autonomy.RULES[level], 'Learn from feedback.', 'This message may correct',
                           autonomy.LESSONS, autonomy.SAFETY, autonomy.LAST]
                self.assertEqual([text.index(n) for n in needles], sorted(text.index(n) for n in needles))

    def test_off_helper_no_project_and_missing_project_have_no_learning(self):
        for options in ({'helper': True, 'pid': PID}, {'pid': None}, {'pid': '000000000000'}):
            text = autonomy.rules({'level': 5, 'source': 'global'}, **options)
            self.assertNotIn('Learn from feedback.', text)
        lessons.set_enabled(False)
        self.assertNotIn('Learn from feedback.', autonomy.rules({'level': 5, 'source': 'global'}, PID))

    def test_nudges_only_for_short_human_prompts_and_review_exception(self):
        def text(prompt):
            return autonomy.rules({'level': 2, 'source': 'global'}, PID, prompt=prompt)
        for prompt in ('WRONG', 'not what i asked', 'undo', 'revert', 'awful', 'terrible', 'again?',
                       'you broke', 'that broke', 'why did you'):
            self.assertIn('This message may correct', text(prompt))
        for prompt in ('perfect', 'EXACTLY RIGHT', 'exactly what', 'great job', 'well done', 'love it', "that's it"):
            self.assertIn('If this message confirms a choice', text(prompt))
        for prompt in ('[Tether status] wrong', 'You are the advisor wrong', 'wrong' + 'x' * 300, 'Do the task.'):
            self.assertNotIn('This message may correct', text(prompt))
            self.assertNotIn('If this message confirms a choice', text(prompt))
        self.assertIn('--avoid --source reviewer.', text('[Tether review] wrong'))
        for index in range(9):
            self.add(f'When case {index} occurs, inspect its exact output.')
        self.assertIn(f'More lessons: {lessons.path(PID)}.', text(''))

    def test_hook_reads_prompt_and_helper_metadata(self):
        def tmux(*args):
            return SimpleNamespace(returncode=0, stdout='helper' if args[-1] == '#{@tether_helper}' else PID)
        out = json.loads(autonomy.hook_output({'prompt': 'wrong'}, {'TMUX': '/fixture', 'TMUX_PANE': '%1'}, tmux))
        self.assertNotIn('Learn from feedback.', out['hookSpecificOutput']['additionalContext'])
        tmux = mock.Mock(side_effect=lambda *args: SimpleNamespace(returncode=0,
                         stdout='' if args[-1] == '#{@tether_helper}' else PID))
        out = json.loads(autonomy.hook_output({'prompt': 'wrong'}, {'TMUX': '/fixture', 'TMUX_PANE': '%1'}, tmux))
        self.assertIn('This message may correct', out['hookSpecificOutput']['additionalContext'])

    def test_api_learning_keys_and_all_user_actions(self):
        review = {'due': False, 'reason': None, 'new': 0, 'disputed': 0, 'total': 0}
        self.assertEqual(autonomy.state()['learning'], {'enabled': True, 'global': [], 'review': review})
        self.assertEqual(autonomy.state(PID)['learning'],
                         {'enabled': True, 'global': [], 'review': review, 'lessons': [], 'path': str(lessons.path(PID))})
        row = self.add()
        route = autonomy.PATH + '/lessons'
        self.assertTrue(autonomy.handles(route))
        handler = Handler(route, {'project': PID, 'edit': row['id'], 'text': 'When tests fail, read every error.'})
        autonomy.post(handler)
        self.assertEqual(handler.status, 200, handler.body)
        self.assertEqual(handler.body['learning']['lessons'][0]['text'], 'When tests fail, read every error.')
        for body in ({'enabled': False}, {'enabled': True}, {'project': PID, 'remove': row['id']}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual(handler.status, 200, handler.body)
        for body in ({'project': PID, 'remove': row['id']}, {'remove_global': row['id']},
                     {'project': PID, 'edit': row['id'], 'text': 'When tests fail, read every error.'},
                     {'project': PID, 'promote': row['id']}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['error']), (404, 'That lesson is gone.'))
        for body in ({'enabled': 'yes'}, {'enabled': True, 'remove_global': row['id']},
                     {'project': PID, 'add': 'When tests fail, read every error.'},
                     {'project': '../x', 'remove': row['id']}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual(handler.status, 400, handler.body)
        row = self.add()
        lessons.keep(PID, row['id'])
        lessons.promote(PID, row['id'])
        handler = Handler(route, {'remove_global': row['id']})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['learning']['global']), (200, []))

    def test_prompt_sections_ids_disputes_and_quote_stays_private(self):
        row = lessons.add(PID, 'When tests fail, inspect each error.', 'avoid', 'user', 'codex',
                          quote='Only the user interface may show this quote.')['lesson']
        rules = lambda: autonomy.rules({'level': 4, 'source': 'project'}, PID, agent='codex')
        text = rules()
        self.assertIn('Seen once, apply only when the situation clearly matches:', text)
        self.assertNotIn('Confirmed:', text)
        self.assertIn('--quote', text)
        self.assertNotIn(row['quote'], text)
        self.assertIn('The user can be wrong:', text)
        self.assertIn(f'dispute ID "why"', text)
        lessons.dispute(PID, row['id'], '%1', 'Evidence differs.')
        text = rules()
        self.assertNotIn(f'- Avoid ({row["id"]})', text)
        self.assertIn(f'More lessons: {lessons.path(PID)}.', text)
        lessons.keep(PID, row['id'])
        self.assertIn('Confirmed:', rules())
        lessons.dispute(PID, row['id'], '%1', 'Evidence differs.')
        text = rules()
        self.assertNotIn('Confirmed:', text)
        self.assertIn(f'- Avoid ({row["id"]}): {row["text"]} (disputed)', text)

    def test_keep_actions_and_one_action_rule(self):
        row = self.add()
        route = autonomy.PATH + '/lessons'
        lessons.dispute(PID, row['id'], '%1', 'Evidence differs.')
        handler = Handler(route, {'project': PID, 'keep': row['id']})
        autonomy.post(handler)
        self.assertEqual(handler.status, 200, handler.body)
        kept = handler.body['learning']['lessons'][0]
        self.assertEqual((kept['status'], kept['disputes']), ('confirmed', []))
        lessons.promote(PID, row['id'])
        handler = Handler(route, {'keep_global': row['id']})
        autonomy.post(handler)
        self.assertEqual(handler.status, 200, handler.body)
        self.assertEqual(handler.body['learning']['global'][0]['status'], 'confirmed')
        for body in ({'project': PID, 'keep': '00000000'}, {'keep_global': '00000000'}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['error']), (404, 'That lesson is gone.'))
        for body in ({'keep': row['id']}, {'project': PID, 'keep_global': row['id']},
                     {'project': PID, 'keep': row['id'], 'remove': row['id']},
                     {'keep_global': row['id'], 'enabled': True}, {'keep_global': []}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual(handler.status, 400, handler.body)

    def test_review_route_get_done_snooze_and_bad_bodies(self):
        route = autonomy.PATH + '/review'
        self.assertTrue(autonomy.handles(route + '?unused=1'))
        handler = Handler(route)
        autonomy.get(handler)
        self.assertEqual((handler.status, handler.body['due'], handler.body['groups']), (200, False, []))
        row = self.add()
        lessons.dispute(PID, row['id'], '%1', 'Evidence differs.')
        handler = Handler(route, {'action': 'snooze'})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['due']), (200, False))
        self.assertGreater(handler.body['snoozed_until'], handler.body['last_review'])
        handler = Handler(route, {'action': 'done'})
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body['snoozed_until']), (200, 0))
        self.assertEqual(handler.body['reason'], 'disputed')
        for body in ({}, [], None, {'action': 'bad'}, {'action': []}, {'action': 'done', 'extra': True}):
            handler = Handler(route, body)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body), (400, {'error': 'Choose done or snooze.'}))
        handler = Handler(route, {})
        handler.rfile = io.BytesIO(b'{x')
        autonomy.post(handler)
        self.assertEqual((handler.status, handler.body), (400, {'error': 'Choose done or snooze.'}))

    def test_review_summary_is_small_and_errors_do_not_break_state(self):
        self.add()
        summary = autonomy.state(PID)['learning']['review']
        self.assertEqual(set(summary), {'due', 'reason', 'new', 'disputed', 'total'})
        self.assertEqual(summary['total'], 1)
        for error in (ValueError('bad file'), OSError('disk error'), RuntimeError('review unavailable')):
            with mock.patch.object(lessons, 'review', side_effect=error):
                result = autonomy.state(PID)
                self.assertIsNone(result['learning']['review'])
                self.assertEqual(len(result['learning']['lessons']), 1)

    def test_recheck_and_unavailable_reviewer(self):
        with mock.patch.object(model_catalog, 'recheck') as recheck:
            handler = Handler(body={'catalog': 'recheck'})
            autonomy.post(handler)
            self.assertEqual(handler.status, 200)
            recheck.assert_called_once_with()
        for available in (False, None, True):
            with mock.patch.object(model_catalog, 'claude', return_value={
                    'models': [{'slug': 'claude-mythos-5-2', 'available': available}]}):
                choice = reviewers(normal={'agent': 'claude', 'model': 'claude-mythos-5-2'})
                if available is False:
                    with self.assertRaisesRegex(autonomy.LevelError, 'That model is not available for your account.'):
                        autonomy.set_reviewers(choice)
                else:
                    autonomy.set_reviewers(choice)


@unittest.skipUnless(HAS_CRYPTO, 'cryptography is not installed')
class LessonApproval(PasskeyCase):
    def test_promotion_requires_bound_passkey_and_preserves_source(self):
        (paths.TETHER / 'projects' / PID).mkdir(parents=True)
        row = lessons.add(PID, 'When checks fail, read every error.', 'avoid', 'reviewer', 'codex')['lesson']
        lessons.keep(PID, row['id'])
        payload = {'project': PID, 'promote': row['id']}
        route = autonomy.PATH + '/lessons'
        with mock.patch.object(model_catalog.shutil, 'which', return_value=None), \
                mock.patch.object(paths, 'CACHE', self.root / 'cache'):
            handler = PasskeyHandler(route, payload)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['approval']), (403, 'no-passkey'))
            self.assertEqual(len(lessons.entries(PID)), 1)
            self.assertFalse(lessons.global_entries())
            self.add()
            handler = PasskeyHandler(route, payload)
            autonomy.post(handler)
            self.assertEqual((handler.status, handler.body['approval']), (403, 'required'))
            proof = self.assertion('lesson-promote', payload)
            handler = PasskeyHandler(route, dict(payload, approval=proof))
            autonomy.post(handler)
            self.assertEqual(handler.status, 200, handler.body)
            self.assertEqual(lessons.global_entries()[0]['source'], 'reviewer')
            self.assertEqual(lessons.entries(PID), [])


if __name__ == '__main__':
    unittest.main()
