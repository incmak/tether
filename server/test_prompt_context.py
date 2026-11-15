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
        for _ in range(3):
            self.assertEqual(prompt_context.once({'session_id': 's1', 'agent_id': 'a1'}, 'autonomy', RULES), RULES,
                             'a Codex subagent always gets the full text: no SessionStart after its compaction')

    def test_a_change_sends_the_full_text_again(self):
        data = {'session_id': 's1'}
        prompt_context.once(data, 'autonomy', RULES)
        changed = RULES + '\nNew lesson.'
        self.assertEqual(prompt_context.once(data, 'autonomy', changed), changed)
        self.assertNotEqual(prompt_context.once(data, 'autonomy', changed), changed)

    def test_no_conversation_or_errors_send_the_full_text(self):
        for data in ({}, {'session_id': '../x'}, None, {'session_id': 5}):
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

    # ---- lessons matched to the prompt (B74 slice 2) ----

    PID = 'a1b2c3d4e5f6'

    def lesson(self, text):
        import lessons
        (paths.TETHER / 'projects' / self.PID).mkdir(parents=True, exist_ok=True)
        return lessons.add(self.PID, text, 'avoid', 'user', 'claude', force_new=True)['lesson']

    def project(self, data, **kw):
        with mock.patch.object(autonomy, 'hook_project', return_value=self.PID):
            return self.autonomy(data, **kw)

    def test_a_prompt_gets_only_the_lessons_that_match_it_once(self):
        ci = self.lesson('When checking CI for a repo, read the run logs before saying CI did not run.')
        zoom = self.lesson('When a phone layout looks cut off, look for pinch zoom before a fix.')
        data = {'session_id': 's1', 'prompt': 'rename the branch'}
        first = self.project(data)
        self.assertIn('Choose a reviewer by the risk', first)
        self.assertIn('More lessons: ', first)
        self.assertNotIn('Lessons from past feedback', first)
        self.assertNotIn(ci['id'], first)
        with_ci = self.project(dict(data, prompt='ci is red'))
        self.assertTrue(with_ci.startswith('Autonomy level 4 of 5'))
        self.assertNotIn('Choose a reviewer', with_ci, 'slice 1: the standing rules stay one line')
        self.assertIn('Lessons from past feedback', with_ci)
        self.assertIn(f'- Avoid ({ci["id"]}, seen once): {ci["text"]}', with_ci)
        self.assertNotIn(zoom['id'], with_ci)
        self.assertLess(len(self.project(dict(data, prompt='ci is red again'))), 300, 'the conversation has it')
        more = self.project(dict(data, prompt='ci is red and the layout is cut off on my phone'))
        self.assertIn(autonomy.LESSONS_AGAIN, more)
        self.assertNotIn('Lessons from past feedback,', more)
        self.assertIn(zoom['id'], more)
        self.assertNotIn(ci['id'], more)
        other = self.project({'session_id': 's2', 'prompt': 'ci is red'})
        self.assertIn(ci['id'], other, 'each conversation on its own')

    def test_full_rules_again_carry_the_lessons_the_conversation_has(self):
        ci = self.lesson('When checking CI for a repo, read the run logs before saying CI did not run.')
        data = {'session_id': 's1', 'prompt': 'ci is red'}
        self.assertIn(ci['id'], self.project(data))
        sent = [self.project(dict(data, prompt='go on')) for _ in range(prompt_context.FULL_EVERY)]
        self.assertTrue(all(ci['id'] not in text for text in sent[:-1]))
        self.assertIn('Choose a reviewer', sent[-1])
        self.assertIn(ci['id'], sent[-1], 'a rewind may have dropped the lesson with the rules')
        self.assertIn('Lessons from past feedback', sent[-1])

    def test_compaction_resends_the_lessons_and_clear_forgets_them(self):
        ci = self.lesson('When checking CI for a repo, read the run logs before saying CI did not run.')
        data = {'session_id': 's1', 'prompt': 'ci is red'}
        self.project(data)
        text = self.start('compact')
        self.assertIn('Choose a reviewer', text)
        self.assertIn(f'- Avoid ({ci["id"]}, seen once)', text)
        self.assertIn('Lessons from past feedback', text)
        self.assertLess(len(self.project(data)), 300, 'the compact text counts as sent')
        self.assertEqual(self.start('clear'), '')
        self.assertIn(ci['id'], self.project(data))

    def test_removed_or_changed_lessons_leave_the_record_and_new_text_goes_out(self):
        import lessons
        ci = self.lesson('When checking CI for a repo, read the run logs before saying CI did not run.')
        data = {'session_id': 's1', 'prompt': 'ci is red'}
        self.project(data)
        lessons.edit(self.PID, ci['id'], 'When checking CI for a repo, read the logs of the run.')
        changed = self.project(data)
        self.assertIn(f'- Avoid ({ci["id"]}): When checking CI for a repo, read the logs of the run.', changed)
        self.assertIn('Lessons from past feedback', changed, 'nothing valid was left, so the rules of use go again')
        lessons.remove(self.PID, ci['id'])
        self.project(dict(data, prompt='go'))
        self.assertEqual(prompt_context.listed(data, 'lessons'), [])
        self.assertNotIn('lessons', prompt_context.saved(data))

    def test_many_matches_are_cut_and_the_rest_come_with_the_next_prompt(self):
        rows = [self.lesson(f'When the gizmo {index} overheats, cool it down slowly.') for index in range(10)]
        data = {'session_id': 's1', 'prompt': 'the gizmo overheats'}
        first = self.project(data)
        self.assertEqual(first.count('- Avoid ('), 8)
        self.assertIn('2 more lessons match this prompt. Read them in ', first)
        second = self.project(data)
        self.assertEqual(second.count('- Avoid ('), 2)
        self.assertNotIn('more lessons match', second)
        self.assertEqual({row['id'] for row in rows},
                         {line[9:17] for line in prompt_context.listed(data, 'lessons')[1:]})
        self.assertLess(len(self.project(data)), 300)

    def test_a_full_resend_keeps_to_the_cap_of_one_prompt(self):
        import lessons
        rows = [self.lesson(f'When the gizmo {index} overheats, cool it down slowly.') for index in range(10)]
        extra = self.lesson('When the widget rattles, tighten each bolt by hand.')
        data = {'session_id': 's1', 'prompt': 'the gizmo overheats'}
        self.project(data)
        self.project(data)
        self.assertEqual(len(prompt_context.listed(data, 'lessons')), 11, 'ten lessons in two prompts')
        for _ in range(prompt_context.FULL_EVERY - 2):
            self.assertLess(len(self.project(dict(data, prompt='go on'))), 300)
        full = self.project(dict(data, prompt='the widget rattles'))
        self.assertIn('Choose a reviewer', full)
        sent = [line for line in full.splitlines() if line.startswith('- Avoid (')]
        self.assertEqual(len(sent), lessons.PROMPT_ROWS)
        self.assertIn(extra['id'], full, 'the lesson of this prompt comes first')
        self.assertEqual(sorted(sent), sorted(prompt_context.listed(data, 'lessons')[1:]), 'the record holds what went out')
        dropped = [row for row in rows if row['id'] not in full]
        self.assertEqual(len(dropped), 3)
        again = self.project(data)
        self.assertTrue(all(row['id'] in again for row in dropped), 'the next match sends what did not fit')
        self.assertEqual(again.count('- Avoid ('), 3)

    def test_a_full_resend_puts_a_recorded_match_of_this_prompt_first(self):
        oldest = self.lesson('When the widget rattles, tighten each bolt by hand.')
        self.lesson('When the sprocket squeaks, oil the axle lightly.')
        data = {'session_id': 's1', 'prompt': 'the widget rattles'}
        self.project(data)
        for index in range(10):
            self.lesson(f'When the gizmo {index} overheats, cool it down slowly.')
        self.project(dict(data, prompt='the gizmo overheats'))
        self.project(dict(data, prompt='the gizmo overheats'))
        for _ in range(prompt_context.FULL_EVERY - 3):
            self.project(dict(data, prompt='go on'))
        for prompt in ('the widget rattles', 'the widget rattles and the sprocket squeaks'):
            prompt_context.once(data, 'autonomy', 'other rules')  # the next prompt sends the full rules again
            full = self.project(dict(data, prompt=prompt))
            self.assertIn('Choose a reviewer', full)
            self.assertIn(oldest['id'], full, 'it matches this prompt, so newer lessons do not push it out')
            self.assertEqual(full.count('- Avoid ('), 8)
        self.assertIn('sprocket', full)

    def test_a_subagent_and_a_prompt_without_a_conversation_get_the_matches_each_time(self):
        ci = self.lesson('When checking CI for a repo, read the run logs before saying CI did not run.')
        for data in ({'session_id': 's1', 'agent_id': 'a1', 'prompt': 'ci is red'}, {'prompt': 'ci is red'}):
            for _ in range(2):
                text = self.project(data, agent='codex')
                self.assertIn('Choose a reviewer', text, 'the full rules, as before')
                self.assertIn('Lessons from past feedback', text)
                self.assertIn(ci['id'], text)
        self.assertNotIn(ci['id'], self.project({'session_id': 's1', 'agent_id': 'a1', 'prompt': 'rename it'}))

    def test_the_tab_name_and_the_folder_select_lessons(self):
        ci = self.lesson('When checking CI for a repo, read the run logs before saying CI did not run.')
        zoom = self.lesson('When a phone layout looks cut off, look for pinch zoom before a fix.')

        def tmux(*args):
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout={'#{@chat_name}': 'Fix CI on main\n'}.get(args[-1], ''))
        with mock.patch.object(autonomy, 'hook_project', return_value=self.PID):
            out = autonomy.hook_output({'session_id': 's1', 'prompt': 'go', 'cwd': '/work/pinch-zoom/'},
                                       {'TMUX': '/fixture', 'TMUX_PANE': '%3'}, tmux)
            text = json.loads(out)['hookSpecificOutput']['additionalContext']
            self.assertIn(ci['id'], text)
            self.assertIn(zoom['id'], text)

            def broken(*args):
                if args[-1] == '#{@chat_name}':
                    raise OSError
                return tmux(*args)
            out = autonomy.hook_output({'session_id': 's2', 'prompt': 'ci?'}, {'TMUX': '/f', 'TMUX_PANE': '%3'}, broken)
            self.assertIn(ci['id'], json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_codex_level_hook_sends_rules_once(self):
        data = {'session_id': 's1'}
        first = json.loads(codex_level.hook_output(data, {}, mock.Mock()))['hookSpecificOutput']['additionalContext']
        again = json.loads(codex_level.hook_output(data, {}, mock.Mock()))['hookSpecificOutput']['additionalContext']
        self.assertIn('codex-lane', first)
        self.assertTrue(again.startswith('Codex level 1 of 5'))
        self.assertNotIn('codex-lane', again)

    def start(self, source, agent='claude', env=None):
        """SessionStart as main() runs it: the text, then the marks."""
        data = {'session_id': 's1', 'hook_event_name': 'SessionStart', 'source': source}
        text, rules = claude_hooks.session_start_context(data, agent, env=env or {})
        for kind, rule in rules.items():
            prompt_context.mark(data, kind, rule)
        return text

    def codex(self, data):
        return json.loads(codex_level.hook_output(data, {}, mock.Mock()))['hookSpecificOutput']['additionalContext']

    def test_session_start_after_compact_resends_the_saved_rules(self):
        data = {'session_id': 's1', 'prompt': 'go'}
        self.autonomy(data)
        self.codex(data)
        self.assertLess(len(self.autonomy(data)), 300)
        with mock.patch.object(autonomy, 'hook_output', side_effect=AssertionError('no hook runs at SessionStart')), \
                mock.patch.object(codex_level, 'hook_output', side_effect=AssertionError):
            text = self.start('compact')
        self.assertIn('Choose a reviewer by the risk', text)
        self.assertIn('codex-lane', text)
        self.assertLess(len(self.autonomy(data)), 300, 'the compact text counts as sent')
        self.assertLess(len(self.codex(data)), 300)

    def test_session_start_sends_only_rules_this_conversation_got(self):
        self.codex({'session_id': 's1'})
        text = self.start('compact')
        self.assertIn('codex-lane', text)
        self.assertNotIn('Choose a reviewer', text)
        claude_hooks.session_start_context({'session_id': 's1', 'source': 'compact'}, 'claude', env={})
        self.assertEqual(self.start('compact'), '', 'the first compact output was not marked, so nothing is saved')

    def test_session_start_clear_or_resume_resends_on_the_next_prompt(self):
        data = {'session_id': 's1', 'prompt': 'go'}
        self.autonomy(data)
        for source in ('clear', 'resume', 'startup'):
            self.assertEqual(self.start(source), '')
            self.assertIn('Choose a reviewer', self.autonomy(data), source)

    def run_main(self, data, argv=('claude_hooks.py',), out=None):
        import io
        out = out or io.StringIO()
        with mock.patch.object(claude_hooks.sys, 'argv', list(argv)), \
                mock.patch.object(claude_hooks.sys, 'stdin', io.StringIO(json.dumps(data))), \
                mock.patch.object(claude_hooks, 'capture'), mock.patch('sys.stdout', out):
            claude_hooks.main()
        return out.getvalue()

    def test_main_marks_only_after_printing(self):
        data = {'session_id': 's1', 'prompt': 'go'}
        self.autonomy(data)
        start = {'session_id': 's1', 'hook_event_name': 'SessionStart', 'source': 'compact'}
        broken = mock.Mock()
        broken.write.side_effect = BrokenPipeError
        self.run_main(start, out=broken)
        self.assertIn('Choose a reviewer', self.autonomy(data), 'nothing was printed, so the next prompt has it all')
        self.autonomy(data)
        printed = json.loads(self.run_main(start))['hookSpecificOutput']['additionalContext']
        self.assertIn('Choose a reviewer', printed)
        self.assertLess(len(self.autonomy(data)), 300)

    def test_browser_note_failure_keeps_the_rules(self):
        self.autonomy({'session_id': 's1', 'prompt': 'go'})
        with mock.patch.dict('sys.modules', {'agent_browser_cli': None}):
            text = self.start('compact', env={'TETHER_CDP_URL': 'ws://x'})
        self.assertIn('Choose a reviewer', text)


if __name__ == '__main__':
    unittest.main()
