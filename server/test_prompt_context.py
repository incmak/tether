"""The standing rules go out once per conversation (B74). Run: python3 -m unittest test_prompt_context"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autonomy
import claude_hooks
import codex_level
import model_catalog
import paths
import prompt_context

RULES = 'Level 4 of 5 (Autopilot).\nRule one.\nRule two.'


class OnceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('CONFIG', 'CACHE', 'TETHER', 'CODEX', 'CLAUDE'):
            patch = mock.patch.object(paths, name, self.root / name.lower())
            patch.start()
            self.addCleanup(patch.stop)

    def test_full_text_once_then_a_reminder(self):
        data = {'session_id': 's1'}
        self.assertEqual(prompt_context.once(data, 'autonomy', RULES), RULES)
        short = prompt_context.once(data, 'autonomy', RULES)
        self.assertEqual(short, 'Level 4 of 5 (Autopilot). ' + prompt_context.SAME)
        self.assertEqual(prompt_context.once(data, 'codex-level', RULES), RULES, 'each kind on its own')
        self.assertEqual(prompt_context.once({'session_id': 's2'}, 'autonomy', RULES), RULES, 'each conversation')
        self.assertEqual(prompt_context.once({'session_id': 's1', 'agent_id': 'a1'}, 'autonomy', RULES), RULES,
                         'a Codex subagent has its own context')

    def test_a_change_sends_the_full_text_again(self):
        data = {'session_id': 's1'}
        prompt_context.once(data, 'autonomy', RULES)
        changed = RULES + '\nNew lesson.'
        self.assertEqual(prompt_context.once(data, 'autonomy', changed), changed)
        self.assertNotEqual(prompt_context.once(data, 'autonomy', changed), changed)

    def test_no_conversation_or_errors_send_the_full_text(self):
        for data in ({}, {'session_id': '../x'}, {'session_id': 's', 'agent_id': 'a/b'}, None, {'session_id': 5}):
            self.assertEqual(prompt_context.once(data, 'autonomy', RULES), RULES, data)
            self.assertEqual(prompt_context.once(data, 'autonomy', RULES), RULES, data)
        self.assertEqual(prompt_context.once({'session_id': 's'}, 'other', RULES), RULES)
        with mock.patch.object(prompt_context, '_write', side_effect=OSError):
            for _ in range(3):
                self.assertEqual(prompt_context.once({'session_id': 'w'}, 'autonomy', RULES), RULES)

    def test_full_text_again_every_so_often(self):
        data = {'session_id': 's1'}
        sent = [prompt_context.once(data, 'autonomy', RULES) == RULES for _ in range(prompt_context.FULL_EVERY * 2)]
        self.assertEqual(sent.count(True), 2)
        self.assertTrue(sent[0] and sent[prompt_context.FULL_EVERY])

    def test_forget_and_mark(self):
        data = {'session_id': 's1'}
        prompt_context.once(data, 'autonomy', RULES)
        prompt_context.forget(data)
        self.assertEqual(prompt_context.once(data, 'autonomy', RULES), RULES)
        prompt_context.forget(data)
        prompt_context.mark(data, 'autonomy', RULES)
        self.assertNotEqual(prompt_context.once(data, 'autonomy', RULES), RULES)

    def test_forget_drops_old_records(self):
        prompt_context.once({'session_id': 'old'}, 'autonomy', RULES)
        prompt_context.once({'session_id': 'new'}, 'autonomy', RULES)
        old = prompt_context.folder() / 'old.autonomy'
        os.utime(old, (1, 1))
        prompt_context.forget({})
        self.assertFalse(old.exists())
        self.assertTrue((prompt_context.folder() / 'new.autonomy').exists())


class HookTests(OnceTests):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(model_catalog.shutil, 'which', return_value=None))
        self.enterContext(mock.patch.object(codex_level, 'models', return_value=[]))
        (paths.CONFIG).mkdir(parents=True)
        (paths.CONFIG / 'autonomy-level').write_text('4')
        (paths.CONFIG / 'codex-level').write_text('1')

    def autonomy(self, data, **kw):
        return json.loads(autonomy.hook_output(data, {}, mock.Mock(), **kw))['hookSpecificOutput']['additionalContext']

    def test_autonomy_hook_sends_rules_once_and_prompt_lines_always(self):
        data = {'session_id': 's1', 'prompt': 'go'}
        full = self.autonomy(data)
        self.assertIn('Choose a reviewer by the risk', full)
        short = self.autonomy(data)
        self.assertLess(len(short), 300)
        self.assertTrue(short.startswith('Autonomy level 4 of 5'))
        pid = 'a1b2c3d4e5f6'
        (paths.TETHER / 'projects' / pid).mkdir(parents=True)
        with mock.patch.object(autonomy, 'hook_project', return_value=pid):
            self.autonomy(data)  # the project adds the learning rules: a change, so the full text again
            nudge = self.autonomy({'session_id': 's1', 'prompt': 'that is wrong'})
        self.assertNotIn('Choose a reviewer', nudge)
        self.assertIn('may correct your last turn', nudge)

    def test_codex_level_hook_sends_rules_once(self):
        data = {'session_id': 's1'}
        first = json.loads(codex_level.hook_output(data, {}, mock.Mock()))['hookSpecificOutput']['additionalContext']
        again = json.loads(codex_level.hook_output(data, {}, mock.Mock()))['hookSpecificOutput']['additionalContext']
        self.assertIn('codex-lane', first)
        self.assertTrue(again.startswith('Codex level 1 of 5'))
        self.assertNotIn('codex-lane', again)

    def install(self, *scripts):
        groups = [{'hooks': [{'type': 'command', 'command': f'/usr/bin/python3 /x/{name} hook'}]} for name in scripts]
        paths.CLAUDE.mkdir(parents=True, exist_ok=True)
        (paths.CLAUDE / 'settings.json').write_text(json.dumps({'hooks': {'UserPromptSubmit': groups}}))

    def test_session_start_after_compact_resends_the_rules(self):
        self.install('autonomy.py', 'codex_level.py')
        data = {'session_id': 's1', 'prompt': 'go'}
        self.autonomy(data)
        self.assertLess(len(self.autonomy(data)), 300)
        start = {'session_id': 's1', 'hook_event_name': 'SessionStart', 'source': 'compact'}
        text = claude_hooks.session_start_context(start, 'claude', env={})
        self.assertIn('Choose a reviewer by the risk', text)
        self.assertIn('codex-lane', text)
        self.assertLess(len(self.autonomy(data)), 300, 'the compact text counts as sent')

    def test_session_start_clear_or_resume_resends_on_the_next_prompt(self):
        self.install('autonomy.py')
        data = {'session_id': 's1', 'prompt': 'go'}
        self.autonomy(data)
        for source in ('clear', 'resume', 'startup'):
            text = claude_hooks.session_start_context({'session_id': 's1', 'source': source}, 'claude', env={})
            self.assertEqual(text, '')
            self.assertIn('Choose a reviewer', self.autonomy(data), source)

    def test_session_start_skips_rules_whose_hook_is_not_installed(self):
        self.install('codex_level.py')
        text = claude_hooks.session_start_context({'session_id': 's1', 'source': 'compact'}, 'claude', env={})
        self.assertIn('codex-lane', text)
        self.assertNotIn('Choose a reviewer', text)
        codex = claude_hooks.session_start_context({'session_id': 's2', 'source': 'compact'}, 'codex', env={})
        self.assertIn('Choose a reviewer', codex)
        self.assertNotIn('codex-lane', codex)


if __name__ == '__main__':
    unittest.main()
