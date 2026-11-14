"""Task launch tests use temporary prompt files and mocked processes, never live tmux."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import delegate
import paths
import model_catalog
import server

THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'


def entry(**extra):
    return dict({'id': '%12', 'agent': 'claude', 'thread': THREAD, 'writable': True,
                 'transcript': '/project/chat.jsonl'}, **extra)


def body(**extra):
    return dict({'session': '%12', 'thread': THREAD, 'agent': 'claude', 'message': 'Fix the bug'}, **extra)


class ValidationTests(unittest.TestCase):
    def test_optional_effort_must_be_supported(self):
        source = entry()
        for effort in (None, 'low', 'medium', 'high', 'xhigh', 'max'):
            self.assertIs(delegate.validate(body(effort=effort), [source]), source)
        for effort in ('auto', 'ultra', '', False, 1, [], {}, 'high; command'):
            with self.subTest(effort=effort), self.assertRaisesRegex(ValueError, '^Choose an effort from the list\\.$'):
                delegate.validate(body(effort=effort), [source])

    def test_ultra_is_supported_only_for_codex(self):
        source = entry()
        self.assertIs(delegate.validate(body(agent='codex', effort='ultra'), [source]), source)
        with self.assertRaisesRegex(ValueError, 'Choose an effort'):
            delegate.validate(body(agent='claude', effort='ultra'), [source])

    def test_optional_model_must_be_listed_for_the_target_agent(self):
        models = {'claude': [{'id': 'opus', 'label': 'Opus'}],
                  'codex': [{'id': 'test-codex', 'label': 'Test Codex'}]}
        source = entry()
        for agent, model in (('claude', 'opus'), ('codex', 'test-codex')):
            with self.subTest(agent=agent):
                self.assertIs(delegate.validate(body(agent=agent, model=model), [source], models=models), source)
        for model in ('unknown', 'test-codex', '', False, 1, [], {}):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, '^Choose a model from the list\\.$'):
                delegate.validate(body(model=model), [source], models=models)
        with patch.object(delegate, 'task_models', return_value=models) as load:
            delegate.validate(body(model='opus'), [source])
            load.assert_called_once_with()
        with patch.object(delegate, 'task_models') as load:
            for request in (body(), body(model=None)):
                self.assertIs(delegate.validate(request, [source], models={}), source)
            load.assert_not_called()

    def test_worktree_refuses_new_reviewer_but_accepts_source_reviewer(self):
        source = entry()
        with self.assertRaises(ValueError) as raised:
            delegate.validate(body(worktree='feature/task', review='new'), [source])
        self.assertEqual(str(raised.exception), "A reviewer tab stays in this tab's folder, so it can see the work. Turn off the new worktree or the new tab review.")
        self.assertIs(delegate.validate(body(worktree='feature/task', review='source'), [source]), source)

    def test_normalize_review_accepts_only_the_documented_types(self):
        for value, expected in ((False, None), (True, 'source'), ('source', 'source'), ('new', 'new')):
            with self.subTest(value=value):
                self.assertEqual(delegate.normalize_review(value), expected)
                delegate.validate(body(review=value), [entry()])
        for value in (0, 1, None, 'false', '', [], {}, 1.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Context and review'):
                delegate.normalize_review(value)

    def test_new_reviewer_requires_a_saved_writable_source_and_allows_empty_task(self):
        error = ('A reviewer tab needs a Claude or Codex chat with a saved conversation. '
                 'Send a first message, then try again.')
        for source in (entry(thread=None), entry(thread='pending-123-1'), entry(pending_conversation=True),
                       entry(transcript=None), entry(transcript=''), entry(writable=False),
                       entry(agent='shell'), entry(agent='antigravity')):
            with self.subTest(source=source), self.assertRaises(ValueError) as raised:
                delegate.validate(body(thread=source['thread'], review='new', context=True), [source])
            self.assertEqual(str(raised.exception), error)
        for agent in ('claude', 'codex'):
            delegate.validate(body(review='new', message=' \n\t '), [entry(agent=agent)])
        for message in (None, 'x' * 20001, '\r'):
            with self.subTest(message=message), self.assertRaises(ValueError):
                delegate.validate(body(review='new', message=message), [entry()])

    def test_optional_name_is_bounded_and_has_no_controls(self):
        for name in (None, 1, '', '  ', 'x' * 61, 'name\n', '\tname', 'name\x7f'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'tab name'):
                delegate.validate(body(name=name), [entry()])
        delegate.validate(body(name='  ' + 'x' * 60 + '  '), [entry()])

    def test_accepts_defaults_and_plain_tasks_from_shells(self):
        source = entry()
        self.assertIs(delegate.validate(body(), [source]), source)
        source = entry(agent='shell', thread=None, writable=False)
        self.assertIs(delegate.validate(body(thread=None), [source]), source)

    def test_rejects_bad_requests(self):
        for extra in ({'session': None}, {'session': '%1;pwd'}, {'session': '%99'},
                      {'thread': None}, {'agent': 'antigravity'}, {'agent': []},
                      {'message': None}, {'message': ' \n\t '}, {'message': 'x' * 20001},
                      {'context': 1}, {'context': None}, {'review': 'false'}, {'review': 0}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                delegate.validate(body(**extra), [entry()])

    def test_rejects_controls_even_at_stripped_edges(self):
        for control in ('\0', '\r', '\x1b', '\x7f', '\x85', '\x9b'):
            with self.subTest(control=repr(control)), self.assertRaisesRegex(ValueError, 'control characters'):
                delegate.validate(body(message=control + 'task'), [entry()])
        delegate.validate(body(message='  Hello\n\tworld  '), [entry()])
        delegate.validate(body(message=' ' + 'x' * 20000 + ' '), [entry()])

    def test_thread_error_is_specific(self):
        with self.assertRaisesRegex(ValueError, '^This tab changed conversations. Reload it and try again.$'):
            delegate.validate(body(thread='stale'), [entry()])

    def test_context_requires_a_saved_supported_conversation(self):
        for source in (entry(thread=None), entry(thread='unsafe;command'), entry(pending_conversation=True),
                       entry(agent='antigravity'), entry(agent='shell')):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'Context needs'):
                delegate.validate(body(thread=source['thread'], context=True), [source])
        for agent in ('claude', 'codex'):
            delegate.validate(body(context=True), [entry(agent=agent)])

    def test_review_requires_writable_supported_source(self):
        for source in (entry(writable=False), entry(agent='shell'), entry(agent='antigravity')):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'Review needs'):
                delegate.validate(body(review=True), [source])
        delegate.validate(body(thread=None, review=True), [entry(thread=None)])


class TaskModelsTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, 'CACHE', root))
        self.enterContext(patch.object(model_catalog.shutil, 'which', return_value=None))

    def test_cache_filters_hidden_models_sorts_priority_and_falls_back_to_slug(self):
        with tempfile.TemporaryDirectory() as home, \
                patch.object(delegate.codex_provider, 'chosen', return_value='plan'):
            cache = Path(home) / '.codex' / 'models_cache.json'
            cache.parent.mkdir()
            cache.write_text(json.dumps({'models': [
                {'slug': 'later', 'display_name': 'Later', 'visibility': 'list', 'priority': 5},
                {'slug': 'hidden', 'visibility': 'hide', 'priority': 0},
                {'slug': 'first', 'visibility': 'list', 'priority': 1},
            ]}))
            expected = {'claude': [{'id': model, 'label': label} for model, label in delegate.CLAUDE_MODELS],
                        'codex': [{'id': 'first', 'label': 'first'}, {'id': 'later', 'label': 'Later'}]}
            self.assertEqual(delegate.task_models(home), expected)
            with patch.object(delegate.codex_provider, 'chosen', return_value='foundry'):
                self.assertEqual(delegate.task_models(home), dict(expected, codex=[]))

    def test_catalog_rows_replace_version_rows_and_validate_with_legacy_choices(self):
        row = {'slug': 'claude-fable-6-1', 'name': 'Fable 6.1', 'efforts': list(delegate.EFFORTS)}
        with patch.object(model_catalog, 'claude', return_value={'models': [row]}), \
                patch.object(delegate.codex_provider, 'chosen', return_value='foundry'):
            models = delegate.task_models()
            self.assertEqual(models['claude'], [{'id': slug, 'label': name}
                                                for slug, name in delegate.CLAUDE_MODELS[:4]] +
                                               [{'id': row['slug'], 'label': row['name']}])
            for model in (row['slug'], *(slug for slug, _ in delegate.CLAUDE_MODELS)):
                delegate.validate(body(model=model), [entry()])

    def test_missing_or_invalid_cache_returns_no_codex_models(self):
        with tempfile.TemporaryDirectory() as home, \
                patch.object(delegate.codex_provider, 'chosen', return_value='plan'):
            self.assertEqual(delegate.task_models(home)['codex'], [])
            cache = Path(home) / '.codex' / 'models_cache.json'
            cache.parent.mkdir()
            for content in ('invalid JSON', '{}', '{"models": null}', '{"models": [null]}',
                            '{"models": [{"visibility": "list"}]}'):
                with self.subTest(content=content):
                    cache.write_text(content)
                    self.assertEqual(delegate.task_models(home)['codex'], [])


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='delegate test ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(paths, 'CACHE', self.root))

    def test_names_use_first_line_and_cut_at_a_word_boundary(self):
        for message, expected in (
                ('  Fix\t the   bug\nIgnore this', 'Fix the bug'),
                ('a' * 32, 'a' * 32), ('a' * 33, 'a' * 32 + '…'),
                ('One two three four five six seven eight', 'One two three four five six…'),
                ('a' * 30 + ' longer word', 'a' * 30 + '…')):
            with self.subTest(message=message):
                self.assertEqual(delegate.task_name(message), expected)

    def test_reviewer_prompt_has_exact_instructions_with_optional_recent_turns(self):
        for agent in ('claude', 'codex'):
            for recent in ('', 'User: test this'):
                source = entry(agent=agent, pane_name='Fix this', label='Fallback')
                expected = (f'You are the reviewer of the {agent.title()} tab “Fix this” (%12) in this project.'
                            '\n\nWhat to check:\nCheck this')
                if recent:
                    expected += '\n\nIts recent turns, cut short:\n\n' + recent
                expected += ('\n\nIts full transcript (JSONL) is at /project/chat.jsonl. Read it when you review.'
                             '\n\nDo not change its work and do not send to that tab yourself. Reply only "Ready." now and stop. '
                             'Tether sends you a review request after that tab finishes a turn, and it sends your reply back to that tab.')
                with self.subTest(agent=agent, recent=recent):
                    self.assertEqual(delegate.reviewer_prompt(source, ' Check this ', recent), expected)
        self.assertIn('tab “Fallback”', delegate.reviewer_prompt(entry(label='Fallback'), '', ''))
        self.assertIn('What to check:\nCheck the work in that tab.', delegate.reviewer_prompt(entry(), '', ''))
        self.assertIn('tab “that tab”', delegate.reviewer_prompt(entry(), '', ''))

    def test_new_reviewer_reverses_link_and_always_reads_context(self):
        source = entry(pane_name='Source title')
        recent = Mock(return_value=[{'role': 'user', 'text': 'Recent task'}])
        with patch.object(delegate, 'new_window_after', return_value='%13'), \
                patch.object(delegate.review_links, 'add') as add:
            result = delegate.start(source, body(review='new', message='', context=False), '/project',
                                    run=Mock(), recent=recent)
        self.assertEqual(result, ('%13', 'Review: Source title'))
        recent.assert_called_once_with()
        prompt, = (self.root / 'delegate').glob('*.md')
        self.assertEqual(prompt.read_text(), delegate.reviewer_prompt(source, '', 'User: Recent task'))
        add.assert_called_once_with(worker='%12', reviewer='%13', reviewer_thread=None,
                                    name='Source title', agent='claude', seen_working=True)

    def test_source_reviewer_with_pending_thread_stays_unpinned(self):
        for thread in (None, 'pending-123-1'):
            with patch.object(delegate, 'new_window_after', return_value='%13'), \
                    patch.object(delegate.review_links, 'add') as add:
                delegate.start(entry(thread=thread), body(review='source'), '/project', run=Mock())
            add.assert_called_once_with(worker='%13', reviewer='%12', reviewer_thread=None,
                                        name='Fix the bug', agent='claude')

    def test_name_and_cwd_overrides_and_reviewer_default_names(self):
        for request, source, expected in (
                (body(name='  Custom name  ', cwd='/ignored'), entry(), 'Custom name'),
                (body(review='new'), entry(label='Fallback'), 'Review: Fallback'),
                (body(review='new'), entry(), 'Review: that tab'),
                (body(review='new'), entry(pane_name='x' * 80), 'Review: ' + 'x' * 52),
                (body(review='new', name='Explicit'), entry(), 'Explicit')):
            with self.subTest(request=request), patch.object(delegate, 'new_window_after', return_value='%13') as window, \
                    patch.object(delegate.review_links, 'add'):
                self.assertEqual(delegate.start(source, request, '/project', run=Mock(), cwd=str(self.root)),
                                 ('%13', expected))
                self.assertEqual(window.call_args.kwargs['cwd'], str(self.root))
        for cwd in ('relative', '/does/not/exist', 123, str(self.root / 'file')):
            with self.subTest(cwd=cwd), patch.object(delegate, 'new_window_after') as window:
                with self.assertRaisesRegex(ValueError, 'absolute path'):
                    delegate.start(entry(), body(), '/project', run=Mock(), cwd=cwd)
                window.assert_not_called()

    def test_helper_role_names_tab_after_source_and_sets_kept_icon(self):
        for role, source, expected in (
                ('advisor', entry(pane_name='Fix login'), 'Advisor: Fix login'),
                ('second-advisor', entry(label='Fallback'), 'Advisor 2: Fallback'),
                ('advisor', entry(pane_name='x' * 80), 'Advisor: ' + 'x' * 31)):
            run = Mock()
            with self.subTest(role=role), patch.object(delegate, 'new_window_after', return_value='%13'):
                self.assertEqual(delegate.start(source, body(role=role), '/project', run=run), ('%13', expected))
                options = run.call_args_list[0].args[0]
                self.assertEqual(options[options.index('@chat_name') + 1], expected)
                self.assertEqual(options[options.index('@chat_icon') + 1], 'lightbulb')
                self.assertEqual(options[options.index('@chat_icon_by') + 1], 'tether')
                self.assertEqual(options[options.index('@tether_role') + 1], role)
        run = Mock()
        with patch.object(delegate, 'new_window_after', return_value='%13'):
            delegate.start(entry(), body(), '/project', run=run)
        self.assertNotIn('@chat_icon', run.call_args_list[0].args[0])

    def test_helper_role_is_validated(self):
        source = entry(writable=True)
        for request, error in ((body(role='boss'), 'tab role'), (body(role='advisor', name='X'), 'helper role'),
                               (body(role='advisor', review='source'), 'helper role'),
                               (body(role='advisor', review='new'), 'helper role'),
                               (body(role=[]), 'tab role'), (body(role={}), 'tab role'), (body(role=1), 'tab role')):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, error):
                delegate.validate(dict(request, session='%12'), [source])
        self.assertIs(delegate.validate(dict(body(role='advisor'), session='%12'), [source]), source)

    def test_prompt_context_is_a_short_brief_for_every_pair(self):
        recent = 'User: hi\n\nAgent: hello'
        for source_agent, target in (('claude', 'claude'), ('claude', 'codex'), ('codex', 'claude'), ('codex', 'codex')):
            name = 'Claude' if source_agent == 'claude' else 'Codex'
            expected = (f'Context: this task comes from another {name} chat in this project. Its recent turns, cut short:\n\n'
                        'User: hi\n\nAgent: hello\n\nIts full transcript (JSONL) is at /project/chat.jsonl. '
                        'Read it only if the task needs more.\n\nTask:\nFix it')
            with self.subTest(source=source_agent, target=target):
                self.assertEqual(delegate.prompt_text(entry(agent=source_agent), 'Fix it', target, context=True, recent=recent), expected)
        self.assertEqual(delegate.prompt_text(entry(), 'Fix it', 'claude'), 'Fix it')
        self.assertEqual(delegate.prompt_text(entry(), ' Fix it ', 'codex', review=True),
                         'Fix it\n\nWhen you finish, stop and wait: a reviewer in another tab reads your work and may send follow-ups.')

    def test_brief_keeps_recent_text_turns_and_stays_small(self):
        messages = [{'role': 'user', 'text': f'question {n}'} for n in range(20)]
        messages += [{'role': 'tool', 'text': 'x' * 9000}, {'role': 'assistant', 'text': 'y' * 5000}, {'role': 'assistant', 'text': '  '}]
        text = delegate.brief(messages)
        lines = text.split('\n\n')
        self.assertEqual(len(lines), delegate.BRIEF_TURNS)
        self.assertEqual(lines[-1], 'Agent: ' + 'y' * delegate.BRIEF_EACH + '…')
        self.assertEqual(lines[0], 'User: question 15')
        self.assertNotIn('x' * 100, text)
        self.assertLessEqual(len(text), delegate.BRIEF_TOTAL + delegate.BRIEF_TURNS * 2)
        self.assertEqual(delegate.brief([{'role': 'user', 'text': 'z' * 600 + ' tail'}] * 10).count('User:'), 4)
        self.assertEqual(delegate.brief(None), '')

    def test_commands_quote_path_and_never_fork(self):
        path = self.root / "prompt's $(danger).md"
        suffix = f' "$(cat {shlex.quote(str(path))})"'
        name = "Fix 'quotes' $(danger)"
        self.assertEqual(delegate.launch_command(entry(), 'claude', path, name), 'claude' + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'claude', path, name, context=True), 'claude' + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'codex', path, name), 'codex' + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'codex', path, name, profile='foundry'), 'codex -p foundry' + suffix)
        model = "model's $(danger); name"
        for agent, prefix in (('claude', 'claude --model '), ('codex', 'codex -m ')):
            with self.subTest(agent=agent):
                self.assertEqual(delegate.launch_command(entry(), agent, path, name, model=model),
                                 prefix + shlex.quote(model) + suffix)
                self.assertEqual(delegate.launch_command(entry(), agent, path, name, model=None), agent + suffix)
        self.assertEqual(delegate.launch_command(entry(), 'codex', path, name, profile='foundry', model=model),
                         'codex -p foundry -m ' + shlex.quote(model) + suffix)
        for profile in ('bad;command', '', 1):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                delegate.launch_command(entry(), 'codex', path, name, profile=profile)

    def test_commands_pass_and_quote_effort(self):
        path = self.root / 'prompt.md'
        suffix = f' "$(cat {shlex.quote(str(path))})"'
        for effort in ('high', 'ultra', "x; $(danger) 'quoted'"):
            self.assertEqual(delegate.launch_command(entry(), 'claude', path, 'Task', model='opus', effort=effort),
                             'claude --model opus --effort ' + shlex.quote(effort) + suffix)
            self.assertEqual(delegate.launch_command(entry(), 'codex', path, 'Task', profile='foundry',
                                                     model='test', effort=effort),
                             'codex -p foundry -m test -c ' + shlex.quote('model_reasoning_effort=' + effort) + suffix)

    def test_start_passes_model_and_effort_for_every_review_mode_and_agent(self):
        for agent in ('claude', 'codex'):
            for review in (False, 'source', 'new'):
                with self.subTest(agent=agent, review=review), \
                        patch.object(delegate, 'new_window_after', return_value='%13'), \
                        patch.object(delegate.codex_provider, 'launch_profile', return_value=None), \
                        patch.object(delegate.review_links, 'add'), \
                        patch.object(delegate, 'launch_command', return_value='launch') as launch:
                    delegate.start(entry(), body(agent=agent, review=review, model='selected', effort='high'),
                                   '/project', run=Mock())
                    self.assertEqual(launch.call_args.kwargs['model'], 'selected')
                    self.assertEqual(launch.call_args.kwargs['effort'], 'high')

    def test_prompt_files_are_private_unique_and_old_files_are_pruned(self):
        directory = self.root / 'delegate'
        directory.mkdir(mode=0o755)
        old = directory / 'old.md'
        old.write_text('old')
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        recent = directory / 'recent.md'
        recent.write_text('recent')
        first = delegate.write_prompt('task\nwith Unicode: ✓')
        second = delegate.write_prompt('second')
        self.assertNotEqual(first, second)
        self.assertRegex(first.name, r'^[a-f0-9]{32}\.md$')
        self.assertEqual(first.read_text(), 'task\nwith Unicode: ✓')
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_start_creates_next_window_names_and_sends_only_to_worker(self):
        def result(argv, **kwargs):
            output = ('@3\t/source\n' if '#{window_id}\t#{pane_current_path}\t#{@group}' in argv
                      else 'latest\t120\t40\n' if argv[1] == 'display-message' else '%13\n')
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')
        run = Mock(side_effect=result)
        with patch.object(delegate.review_links, 'add') as add, patch.object(delegate.time, 'time', return_value=123):
            pane, name = delegate.start(entry(), body(review=True), '/project', run=run)
        self.assertEqual((pane, name), ('%13', 'Fix the bug'))
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[2][1:], ['new-window', '-a', '-t', '@3', '-c', '/project', '-P', '-F', '#{pane_id}'])
        self.assertEqual(calls[3][1:], ['set-option', '-p', '-t', '%13', '@chat_name', name, ';',
                                     'set-option', '-p', '-t', '%13', '@chat_name_at', '123000'])
        path, = (self.root / 'delegate').glob('*.md')
        self.assertEqual(calls[4][1:], ['send-keys', '-t', '%13', '-l', f'claude "$(cat {shlex.quote(str(path))})"'])
        self.assertEqual(calls[5][1:], ['send-keys', '-t', '%13', 'Enter'])
        self.assertEqual(len(calls), 6)
        add.assert_called_once_with(worker='%13', reviewer='%12', reviewer_thread=THREAD, name=name, agent='claude')

    def test_start_uses_selected_codex_profile_and_does_not_register_without_review(self):
        run = Mock()
        with patch.object(delegate, 'new_window_after', return_value='%13'), \
                patch.object(delegate.codex_provider, 'launch_profile', return_value='foundry') as profile, \
                patch.object(delegate.review_links, 'add') as add:
            delegate.start(entry(), body(agent='codex'), '/project', run=run)
        profile.assert_called_once_with()
        self.assertTrue(run.call_args_list[1].args[0][-1].startswith('codex -p foundry "$(cat '))
        add.assert_not_called()

    def test_worktree_sets_pane_option_in_name_call_and_new_window_path(self):
        def result(argv, **kwargs):
            output = ('@3\t/source\n' if '#{window_id}\t#{pane_current_path}\t#{@group}' in argv
                      else 'latest\t120\t40\n' if argv[1] == 'display-message' else '%13\n')
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr='')
        run = Mock(side_effect=result)
        with patch.object(delegate.time, 'time', return_value=123):
            delegate.start(entry(), body(), '/project', run=run, cwd=str(self.root), worktree='feature/foo')
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[2][1:], ['new-window', '-a', '-t', '@3', '-c', str(self.root), '-P', '-F', '#{pane_id}'])
        self.assertEqual(calls[3][1:], ['set-option', '-p', '-t', '%13', '@chat_name', 'Fix the bug', ';',
                                     'set-option', '-p', '-t', '%13', '@chat_name_at', '123000', ';',
                                     'set-option', '-p', '-t', '%13', '@worktree', 'feature/foo'])
        self.assertEqual(len(calls), 6)

    def test_invalid_profile_does_not_open_window_or_write_prompt(self):
        with patch.object(delegate.codex_provider, 'launch_profile', return_value='bad;command'), \
                patch.object(delegate, 'new_window_after') as window:
            with self.assertRaises(ValueError):
                delegate.start(entry(), body(agent='codex'), '/project', run=Mock())
        window.assert_not_called()
        self.assertFalse((self.root / 'delegate').exists())


class EndpointTests(unittest.TestCase):
    def test_task_models_needs_no_body_and_preserves_auth_guards(self):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/task-models'
        handler.command = 'POST'
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'}
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        models = {'claude': [{'id': 'opus', 'label': 'Opus'}], 'codex': []}
        with patch.object(delegate, 'task_models', return_value=models) as load:
            handler.do_POST()
            handler.respond.assert_called_once_with(200, models)
            load.assert_called_once_with()
            load.reset_mock()
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request({}, headers=headers, path=handler.path)[0], 403)
            handler.deny.return_value = True
            handler.respond.reset_mock()
            handler.do_POST()
            handler.respond.assert_not_called()
            load.assert_not_called()

    def request(self, payload, headers=None, denied=False, path='/mobile/api/delegate'):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.command = 'POST'
        encoded = json.dumps(payload).encode()
        handler.rfile = io.BytesIO(encoded)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(encoded)), **(headers or {})}
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def test_review_stop_returns_result_without_choose_or_tmux_writes(self):
        with patch.object(server, 'choose') as choose, \
                patch.object(server.subprocess, 'run') as run, \
                patch.object(server.review_links, 'remove') as remove:
            for stopped in (True, False):
                remove.return_value = stopped
                self.assertEqual(self.request({'worker': '%13'}, path='/mobile/api/review-stop'),
                                 (200, {'stopped': stopped}))
                remove.assert_called_with('%13', None)
        choose.assert_not_called()
        run.assert_not_called()

    def test_review_stop_rejects_bad_worker_with_specific_error(self):
        with patch.object(server.review_links, 'remove') as remove:
            for worker in (None, '', '13', '%13;pwd', '%13\n', 13, [], {}):
                with self.subTest(worker=worker):
                    self.assertEqual(self.request({'worker': worker}, path='/mobile/api/review-stop'),
                                     (400, {'error': 'Select a tab to stop reviewing.'}))
        remove.assert_not_called()

    def test_review_stop_accepts_a_pair_and_rejects_invalid_reviewer(self):
        with patch.object(server.review_links, 'remove', return_value=True) as remove:
            self.assertEqual(self.request({'worker': '%13', 'reviewer': '%12'}, path='/mobile/api/review-stop'),
                             (200, {'stopped': True}))
            remove.assert_called_once_with('%13', '%12')
            remove.reset_mock()
            for reviewer in (None, '', 12, '%12\n', []):
                self.assertEqual(self.request({'worker': '%13', 'reviewer': reviewer}, path='/mobile/api/review-stop'),
                                 (400, {'error': 'Select a tab to stop reviewing.'}))
            remove.assert_not_called()

    def test_review_stop_preserves_auth_guards(self):
        with patch.object(server.review_links, 'remove') as remove:
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request({'worker': '%13'}, headers=headers,
                                              path='/mobile/api/review-stop')[0], 403)
            handler = object.__new__(server.Handler)
            handler.path = '/mobile/api/review-stop'
            handler.deny = Mock(return_value=True)
            handler.respond = Mock()
            handler.do_POST()
            handler.respond.assert_not_called()
        remove.assert_not_called()

    def test_sessions_adds_reviewers_and_survives_store_read_errors(self):
        rows = [{'id': '%13'}, {'id': '%12'}, {'id': '%20'}]
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/sessions'
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        with patch.object(server, 'sessions', return_value=rows), \
                patch.object(server, 'session_row', side_effect=lambda row: dict(row)), \
                patch.object(server, 'settings', return_value={'thread': THREAD}), \
                patch.object(server.review_links, 'reviewed_by', return_value={'%13': ['%12', '%20'], '%12': []}) as reviewed_by, \
                patch.object(server.review_links, 'paused', return_value={'%13': ['%20']}) as paused:
            handler.do_GET()
            reviewed_by.assert_called_once_with()
            paused.assert_called_once_with()
            handler.respond.assert_called_with(200, {'sessions': [
                {'id': '%13', 'reviewers': ['%12', '%20'], 'reviewers_paused': ['%20']},
                {'id': '%12', 'reviewers_paused': []}, {'id': '%20', 'reviewers_paused': []}], 'default_thread': THREAD})
            reviewed_by.reset_mock()
            reviewed_by.side_effect = OSError('disk')
            handler.do_GET()
            reviewed_by.assert_called_once_with()
            handler.respond.assert_called_with(200, {'sessions': [dict(row, reviewers_paused=[]) for row in rows],
                                                      'default_thread': THREAD})
            reviewed_by.side_effect = None
            paused.side_effect = OSError('disk')
            handler.do_GET()
            handler.respond.assert_called_with(200, {'sessions': [dict(row, reviewers_paused=[]) for row in rows],
                                                      'default_thread': THREAD})

    def test_route_bypasses_default_choose_and_holds_send_lock(self):
        source = entry()
        def start(config, request, folder, run, recent):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertIs(config, source)
            self.assertEqual(request, body())
            self.assertEqual(folder, str(paths.HOME))
            self.assertIs(run, server.subprocess.run)
            return '%13', 'Fix the bug'
        with patch.object(server, 'sessions', return_value=[source]) as sessions, \
                patch.object(server, 'agent_folder', return_value='/missing/project'), \
                patch.object(server, 'choose') as choose, patch.object(delegate, 'start', side_effect=start):
            self.assertEqual(self.request(body()), (201, {'pane': '%13', 'name': 'Fix the bug'}))
        choose.assert_not_called()
        sessions.assert_called_once_with()

    def test_existing_project_folder_is_used(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server, 'agent_folder', return_value=folder), \
                patch.object(delegate, 'start', return_value=('%13', 'Fix the bug')) as start:
            self.assertEqual(self.request(body())[0], 201)
            self.assertEqual(start.call_args.args[2], folder)

    def test_worktree_creation_is_outside_lock_and_reply_has_worktree(self):
        with tempfile.TemporaryDirectory() as folder:
            made = {'branch': 'feature/foo', 'path': folder}
            def make(request, base):
                self.assertFalse(server.SEND_LOCK.locked())
                self.assertEqual(base, folder)
                self.assertEqual(request['worktree'], 'feature/foo')
                return made
            def start(*args, **kwargs):
                self.assertTrue(server.SEND_LOCK.locked())
                self.assertEqual(kwargs['cwd'], folder)
                self.assertEqual(kwargs['worktree'], made['branch'])
                return '%13', 'task'
            with patch.object(server, 'sessions', return_value=[entry()]), \
                    patch.object(server, 'agent_folder', return_value=folder), \
                    patch.object(server.worktrees, 'from_body', side_effect=make), \
                    patch.object(delegate, 'start', side_effect=start):
                self.assertEqual(self.request(body(worktree='feature/foo')),
                                 (201, {'pane': '%13', 'name': 'task', 'worktree': made}))

    def test_worktree_thread_change_during_creation_does_not_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            made = {'branch': 'feature/foo', 'path': folder}
            entries = [entry()]
            def make(request, base):
                entries[0] = dict(entries[0], thread='changed')
                return made
            with patch.object(server, 'sessions', side_effect=lambda: entries), \
                    patch.object(server, 'agent_folder', return_value=folder), \
                    patch.object(server.worktrees, 'from_body', side_effect=make) as create, \
                    patch.object(delegate, 'start') as start:
                status, reply = self.request(body(worktree=made['branch']))
            start.assert_not_called()
            create.assert_called_once()
            self.assertEqual(status, 503)
            self.assertIn(folder, reply['error'])
            self.assertIn('This tab changed conversations', reply['error'])

    def test_worktree_session_ending_during_creation_does_not_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            made = {'branch': 'feature/foo', 'path': folder}
            entries = [entry()]
            def make(request, base):
                entries.clear()
                return made
            with patch.object(server, 'sessions', side_effect=lambda: entries), \
                    patch.object(server, 'agent_folder', return_value=folder), \
                    patch.object(server.worktrees, 'from_body', side_effect=make) as create, \
                    patch.object(delegate, 'start') as start:
                status, reply = self.request(body(worktree=made['branch']))
            start.assert_not_called()
            create.assert_called_once()
            self.assertEqual(status, 503)
            self.assertIn(folder, reply['error'])
            self.assertIn('That session has ended', reply['error'])

    def test_worktree_validation_and_creation_refusals_do_not_launch(self):
        with patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server.worktrees, 'from_body', side_effect=ValueError('Invalid branch.')) as make, \
                patch.object(server.subprocess, 'run') as run:
            self.assertEqual(self.request(body(worktree='bad', thread=None))[0], 400)
            make.assert_not_called()
            with patch.object(server, 'agent_folder', return_value=None):
                self.assertEqual(self.request(body(worktree='bad')), (400, {'error': 'Invalid branch.'}))
            self.assertEqual(make.call_args.args[1], str(paths.HOME))
        run.assert_not_called()

    def test_worktree_survives_failure_through_delegate_start(self):
        with tempfile.TemporaryDirectory() as folder:
            made = {'branch': 'feature/foo', 'path': folder}
            with patch.object(server, 'sessions', return_value=[entry()]), \
                    patch.object(server, 'agent_folder', return_value=folder), \
                    patch.object(server.worktrees, 'from_body', return_value=made), \
                    patch.object(delegate, 'new_window_after', return_value='%13'), \
                    patch.object(server.subprocess, 'run', side_effect=OSError('launch failed')) as run:
                status, reply = self.request(body(worktree='feature/foo'))
            self.assertEqual(status, 503)
            self.assertIn(folder, reply['error'])
            self.assertIn('agent did not start: launch failed', reply['error'])
            self.assertTrue(Path(folder).is_dir())
            self.assertFalse(any('remove' in call.args[0] for call in run.call_args_list))

    def test_browser_ignores_cwd_in_body(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server, 'agent_folder', return_value=folder), \
                patch.object(delegate, 'start', return_value=('%13', 'task')) as start:
            self.assertEqual(self.request(body(cwd='/untrusted/body/path'))[0], 201)
            self.assertEqual(start.call_args.args[2], folder)
            self.assertNotIn('cwd', start.call_args.kwargs)

    def test_specific_validation_errors_and_auth_guards_prevent_launch(self):
        with patch.object(server, 'sessions', return_value=[entry()]), patch.object(delegate, 'start') as start:
            self.assertEqual(self.request(body(thread=None)),
                             (400, {'error': 'This tab changed conversations. Reload it and try again.'}))
            self.assertEqual(self.request(body(agent='bad'))[0], 400)
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request(body(), headers=headers)[0], 403)
            handler = object.__new__(server.Handler)
            handler.path = '/mobile/api/delegate'
            handler.deny = Mock(return_value=True)
            handler.respond = Mock()
            handler.do_POST()
            handler.respond.assert_not_called()
        start.assert_not_called()

    def test_accepts_20000_unicode_characters_in_json(self):
        with patch.object(server, 'sessions', return_value=[entry()]), \
                patch.object(server, 'agent_folder', return_value=None), \
                patch.object(delegate, 'start', return_value=('%13', 'task')):
            self.assertEqual(self.request(body(message='😀' * 20000))[0], 201)

    def test_success_expires_session_cache(self):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = '/mobile/api/delegate', 'POST'
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        with patch.object(server, 'forget_sessions') as forget:
            handler.respond(201, {'pane': '%13', 'name': 'task'})
        forget.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
