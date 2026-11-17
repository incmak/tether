import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autonomy
import claude_advisor
import paths

PID = 'abcdef012345'


class Handler:
    def __init__(self, path, body=None):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = path
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        (self.home / '.claude').mkdir(parents=True)
        for name, value in (('CONFIG', self.root / 'config'), ('TETHER', self.root / 'tether'),
                            ('HOME', self.home), ('CLAUDE', self.home / '.claude')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        names = (*claude_advisor.OFF_VARIABLES, *claude_advisor.NO_FLAG_VARIABLES)
        env = mock.patch.dict(os.environ, {name: '' for name in names})
        env.start()
        self.addCleanup(env.stop)

    def settings(self, data):
        (self.home / '.claude' / 'settings.json').write_text(json.dumps(data))


class Storage(Base):
    def test_default_is_off_and_bad_files_read_as_off(self):
        self.assertEqual(claude_advisor.resolve(PID)['model'], 'off')
        self.assertEqual(claude_advisor.launch_settings(PID), {})
        claude_advisor.global_path().parent.mkdir(parents=True)
        claude_advisor.global_path().write_text('sonnet\n')
        self.assertEqual(claude_advisor.read_global(), 'off')
        claude_advisor.project_path(PID).parent.mkdir(parents=True)
        claude_advisor.project_path(PID).write_text('{"override": "yes", "model": "opus"}')
        self.assertIsNone(claude_advisor.read_project(PID))

    def test_project_override_beats_global_only_while_on(self):
        claude_advisor.set_global('opus')
        claude_advisor.set_project(PID, True, 'off')
        self.assertEqual(claude_advisor.resolve(PID)['model'], 'off')
        self.assertEqual(claude_advisor.resolve(PID)['source'], 'project')
        claude_advisor.set_project(PID, False, 'off')
        self.assertEqual(claude_advisor.launch_settings(PID), {'advisorModel': 'opus'})
        self.assertEqual(claude_advisor.launch_settings(None), {'advisorModel': 'opus'})

    def test_bad_input_is_refused(self):
        for model in ('', 'sonnet', None, 'Opus', 'claude-opus-5-5'):
            with self.assertRaises(claude_advisor.AdvisorError):
                claude_advisor.set_global(model)
        with self.assertRaises(claude_advisor.AdvisorError):
            claude_advisor.set_project('../x', True, 'opus')
        with self.assertRaises(claude_advisor.AdvisorError):
            claude_advisor.set_project(PID, 'true', 'opus')


class Problems(Base):
    def test_off_has_none_and_opus_is_clean_by_default(self):
        self.assertEqual(claude_advisor.problems('off', main_model='Fable 5.2'), [])
        self.assertEqual(claude_advisor.problems('opus', main_model='Opus 5.5'), [])

    def test_fable_consent_counts_only_for_the_active_account(self):
        self.assertEqual([item['code'] for item in claude_advisor.problems('fable')], ['consent'])
        account = {'organizationUuid': 'org-1', 'accountUuid': 'u-1'}
        for consent, ok in (({'org-2': True, 'org-2:2': True, 'acct:u-2': True}, False), ({'org-1': True}, False),
                            ({'org-1': True, 'org-1:2': True}, True), ({'acct:u-1': True, 'acct:u-1:2': True}, True),
                            ({'acct:u-1:2': True}, False), ({'org-1': 'yes', 'org-1:2': True}, False)):
            (self.home / '.claude.json').write_text(json.dumps({'oauthAccount': account, 'fableOverageConsentV2': consent}))
            self.assertEqual(claude_advisor.fable_consent(), ok, consent)
        (self.home / '.claude.json').write_text(json.dumps({'fableOverageConsentV2': {'org-1': True, 'org-1:2': True}}))
        self.assertFalse(claude_advisor.fable_consent())

    def test_main_model_ranks(self):
        for advisor, main, out in (('opus', 'Fable 5.2', True), ('opus', 'claude-fable-5-2', True),
                                   ('opus', 'Opus 5.5', False), ('opus', 'Sonnet 5.5', False),
                                   ('fable', 'Opus 5.5', False), ('opus', '', False), ('opus', 'custom-model', False),
                                   ('opus', 'opus or fable', False)):
            self.assertEqual(claude_advisor.outranked(advisor, main), out, (advisor, main))
        codes = [item['code'] for item in claude_advisor.problems('opus', main_model='Fable 5.2')]
        self.assertEqual(codes, ['outranked'])

    def test_variables_from_env_and_settings(self):
        problems = claude_advisor.problems('opus', env={'CLAUDE_CODE_DISABLE_ADVISOR_TOOL': '1'})
        self.assertEqual([item['code'] for item in problems], ['disabled'])
        self.assertEqual(claude_advisor.problems('opus', env={'DISABLE_TELEMETRY': '0'}), [])
        self.settings({'env': {'DO_NOT_TRACK': '1'}, 'advisorModel': 'fable'})
        self.assertEqual([item['code'] for item in claude_advisor.problems('opus', env={})], ['no-flags'])
        self.assertEqual(claude_advisor.user_setting(), 'fable')


class TabMark(Base):
    def test_mark_round_trip(self):
        self.assertEqual(claude_advisor.mark('opus', env={}), 'opus')
        mark = claude_advisor.mark('opus', env={'DISABLE_TELEMETRY': '1', 'CLAUDE_CODE_DISABLE_ADVISOR_TOOL': 'true'})
        self.assertEqual(mark, 'opus!disabled,no-flags')
        state = claude_advisor.tab_state(mark, 'Opus 5.5')
        self.assertEqual((state['name'], [item['code'] for item in state['problems']]), ('Opus', ['disabled', 'no-flags']))
        self.assertEqual(claude_advisor.tab_state('opus', 'Opus 5.5')['problems'], [])

    def test_unknown_values_are_refused(self):
        for value in ('', 'off', 'sonnet', 'opus!', 'opus!other', 'opus extra', None, 7):
            self.assertIsNone(claude_advisor.tab_state(value), value)

    def test_unmarked_tab_reports_the_persons_own_setting(self):
        self.settings({'advisorModel': 'sonnet'})
        state = claude_advisor.tab_state('')
        self.assertEqual((state['source'], state['name'], state['problems']), ('claude', 'sonnet', []))
        self.assertEqual(claude_advisor.tab_state('opus')['source'], 'tether')

    def test_rule_only_for_a_real_model(self):
        self.assertEqual(claude_advisor.rule('off'), '')
        self.assertIn('before you report the task done', claude_advisor.rule('opus'))
        self.assertIn('agent_advisor_cli.py', claude_advisor.rule('fable'))


class Rules(Base):
    def test_claude_rules_name_the_tab_advisor_and_codex_rules_do_not(self):
        resolved = autonomy.resolve(None)
        self.assertIn("advisor tool (Opus)", autonomy.rules(resolved, agent='claude', tab_advisor='opus'))
        self.assertNotIn("advisor tool", autonomy.rules(resolved, agent='claude'))
        self.assertNotIn("advisor tool", autonomy.rules(resolved, agent='codex', tab_advisor='opus'))


class Http(Base):
    def test_get_and_post(self):
        handler = Handler(claude_advisor.PATH)
        claude_advisor.get(handler)
        self.assertEqual((handler.status, handler.body['global'], handler.body['model']), (200, 'off', 'off'))
        self.assertEqual([item['model'] for item in handler.body['models']], ['off', 'opus', 'fable'])
        handler = Handler(claude_advisor.PATH, {'model': 'opus'})
        claude_advisor.post(handler)
        self.assertEqual((handler.status, handler.body['model']), (200, 'opus'))
        handler = Handler(claude_advisor.PATH, {'project': PID, 'override': True, 'model': 'off'})
        claude_advisor.post(handler)
        self.assertEqual((handler.body['model'], handler.body['source']), ('off', 'project'))
        handler = Handler(f'{claude_advisor.PATH}?project={PID}')
        claude_advisor.get(handler)
        self.assertEqual(handler.body['project'], {'override': True, 'model': 'off'})

    def test_bad_requests(self):
        for body, status in (({'model': ['opus']}, 400), ({'model': {'a': 1}}, 400),
                             ({'project': PID, 'override': True, 'model': []}, 400), ({'model': 'sonnet'}, 400), ([1], 400), ({'project': 'x', 'override': True, 'model': 'opus'}, 400)):
            handler = Handler(claude_advisor.PATH, body)
            claude_advisor.post(handler)
            self.assertEqual(handler.status, status, body)
        handler = Handler(claude_advisor.PATH)
        claude_advisor.post(handler)
        self.assertEqual(handler.status, 413)
        handler = Handler(f'{claude_advisor.PATH}?project=../x')
        claude_advisor.get(handler)
        self.assertEqual(handler.status, 400)
        self.assertFalse(claude_advisor.global_path().exists())
        claude_advisor.project_path(PID).parent.mkdir(parents=True)
        claude_advisor.project_path(PID).write_text('{"override": true, "model": ["opus"]}')
        self.assertEqual(claude_advisor.resolve(PID)['model'], 'off')


if __name__ == '__main__':
    unittest.main()
