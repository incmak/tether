"""Worktree creation uses disposable git repositories and never touches tmux."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import worktrees
from unittest_environment import isolate


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        isolate()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'project'
        self.base = self.root / 'project-wt'
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_TERMINAL_PROMPT='0', LC_ALL='C')
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            env.pop(key, None)
        self.enterContext(patch.dict(os.environ, env, clear=True))
        self.git(self.root, 'init', '-b', 'main', str(self.repo))
        self.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-m', 'First')

    def git(self, folder, *args):
        return subprocess.run(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()

    def state(self):
        return (self.git(self.repo, 'worktree', 'list', '--porcelain'),
                self.git(self.repo, 'branch', '--list'),
                sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')
                       if '.git' not in path.relative_to(self.root).parts))

    def refusal(self, branch, message, folder=None):
        before = self.state()
        with self.assertRaises(ValueError) as raised:
            worktrees.create(str(folder or self.repo), branch)
        self.assertEqual(str(raised.exception), message)
        self.assertEqual(self.state(), before)

    def test_root_and_subfolder_use_main_head_and_slug(self):
        subfolder = self.repo / 'src'
        subfolder.mkdir()
        for folder, branch in ((self.repo, 'feature/foo'), (subfolder, 'feature/bar')):
            with self.subTest(folder=folder):
                made = worktrees.create(str(folder), branch)
                path = self.base / worktrees.slug(branch)
                self.assertEqual(made, {'branch': branch, 'path': str(path)})
                self.assertEqual(self.git(path, 'rev-parse', 'HEAD'), self.git(self.repo, 'rev-parse', 'HEAD'))
                self.assertEqual(self.git(path, 'branch', '--show-current'), branch)
        self.assertEqual(worktrees.slug('feature/foo'), 'feature-foo')
        self.assertEqual(worktrees.slug('..feature///foo--'), 'feature-foo')

    def test_linked_worktree_uses_main_path_and_main_head(self):
        linked = self.root / 'linked'
        self.git(self.repo, 'worktree', 'add', '-b', 'linked', str(linked))
        self.git(linked, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-m', 'Linked only')
        self.assertNotEqual(self.git(linked, 'rev-parse', 'HEAD'), self.git(self.repo, 'rev-parse', 'HEAD'))
        made = worktrees.create(str(linked), 'feature/from-linked')
        self.assertEqual(made['path'], str(self.base / 'feature-from-linked'))
        self.assertEqual(self.git(made['path'], 'rev-parse', 'HEAD'), self.git(self.repo, 'rev-parse', 'HEAD'))
        for folder in (self.repo, linked):
            self.assertEqual(worktrees.info(str(folder)), {'main': str(self.repo), 'base': str(self.base)})

    def test_bad_names_change_nothing(self):
        self.git(self.repo, 'checkout', '-b', 'other')
        self.git(self.repo, 'checkout', 'main')
        for branch in ('feature/x y', '-bad', '@{-1}', 'a..b', ' spaced ', ' '):
            with self.subTest(branch=branch):
                self.refusal(branch, f'"{branch}" is not a valid branch name.')
        for branch in ('', None, 123, [], {}):
            with self.subTest(branch=branch):
                self.refusal(branch, 'Enter a branch name for the new worktree.')
        self.refusal('你好', '"你好" needs a letter or a number for its folder name.')

    def test_trailing_semicolon_changes_nothing_but_internal_semicolon_is_valid(self):
        self.refusal('a;', '"a;" ends in ";", which tmux cannot store. Choose another branch name.')
        self.assertFalse(self.base.exists())
        made = worktrees.create(str(self.repo), 'b;c')
        self.assertEqual(made, {'branch': 'b;c', 'path': str(self.base / 'b-c')})
        self.assertEqual(self.git(made['path'], 'branch', '--show-current'), 'b;c')

    def test_existing_branch_path_and_dangling_symlink_change_nothing(self):
        self.refusal('main', 'Branch "main" already exists.')
        self.base.mkdir()
        path = self.base / 'feature-foo'
        path.mkdir()
        self.refusal('feature/foo', f'{path} already exists.')
        path.rmdir()
        path.symlink_to(self.root / 'missing')
        self.refusal('feature/foo', f'{path} already exists.')

    def test_base_file_nonrepo_and_unborn_head_change_nothing(self):
        self.base.write_text('keep')
        self.refusal('feature/foo', f'{self.base} is not a folder.')
        plain = self.root / 'plain'
        plain.mkdir()
        self.refusal('feature/foo', f'{plain} is not in a git repository.', folder=plain)
        unborn = self.root / 'unborn'
        self.git(self.root, 'init', '-b', 'main', str(unborn))
        before = self.git(unborn, 'worktree', 'list', '--porcelain'), self.git(unborn, 'branch', '--list')
        self.refusal('feature/foo', 'The repository has no commits yet.', folder=unborn)
        self.assertEqual(before, (self.git(unborn, 'worktree', 'list', '--porcelain'), self.git(unborn, 'branch', '--list')))
        self.assertFalse((self.root / 'unborn-wt').exists())

    def test_info_for_root_subfolder_plain_missing_and_bare(self):
        child = self.repo / 'src'
        child.mkdir()
        expected = {'main': str(self.repo), 'base': str(self.base)}
        self.assertEqual(worktrees.info(str(self.repo)), expected)
        self.assertEqual(worktrees.info(str(child)), expected)
        self.assertIsNone(worktrees.info(str(self.root)))
        self.assertIsNone(worktrees.info(str(self.root / 'missing')))
        self.assertIsNone(worktrees.info(str(self.repo / '.git')))
        bare = self.root / 'bare.git'
        self.git(self.root, 'init', '--bare', str(bare))
        self.assertIsNone(worktrees.info(str(bare)))

    def test_info_handles_missing_git_and_timeout(self):
        for error in (FileNotFoundError('git'), subprocess.TimeoutExpired(['git'], 5)):
            with self.subTest(error=error), patch.object(worktrees.subprocess, 'run', side_effect=error):
                self.assertIsNone(worktrees.info(str(self.repo)))

    def test_git_calls_use_lists_sanitized_environment_and_timeouts(self):
        original = subprocess.run
        with patch.dict(os.environ, GIT_DIR='bad', GIT_WORK_TREE='bad', GIT_INDEX_FILE='bad'), \
                patch.object(worktrees.subprocess, 'run', wraps=original) as run:
            made = worktrees.create(str(self.repo), 'feature/safe')
        self.assertTrue(Path(made['path']).is_dir())
        for call in run.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertNotIn('shell', call.kwargs)
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(call.kwargs['capture_output'])
            self.assertTrue(call.kwargs['text'])
            for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
                self.assertNotIn(key, call.kwargs['env'])
            self.assertEqual(call.kwargs['env']['LC_ALL'], 'C')
            self.assertEqual(call.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(call.kwargs['timeout'], 120 if 'add' in call.args[0] else 5)
            self.assertNotIn('remove', call.args[0])

    def test_add_errors_report_last_line_or_path_without_cleanup(self):
        original = subprocess.run
        path = self.base / 'feature-fails'
        def run(args, **kwargs):
            if 'add' in args:
                path.mkdir(parents=True)
                if timeout:
                    raise subprocess.TimeoutExpired(args, 120)
                return subprocess.CompletedProcess(args, 1, '', 'first line\nlast error\n')
            return original(args, **kwargs)
        for timeout in (False, True):
            with self.subTest(timeout=timeout), patch.object(worktrees.subprocess, 'run', side_effect=run) as mocked:
                with self.assertRaises(RuntimeError) as raised:
                    worktrees.create(str(self.repo), 'feature/fails')
                if timeout:
                    self.assertIn(str(path), str(raised.exception))
                    self.assertIn('Check', str(raised.exception))
                else:
                    self.assertEqual(str(raised.exception), 'Could not create the worktree: last error')
                self.assertTrue(path.is_dir())
                self.assertFalse(any('remove' in call.args[0] for call in mocked.call_args_list))
            path.rmdir()

    def test_from_body_and_keeping_never_remove_created_worktree(self):
        self.assertIsNone(worktrees.from_body({}, str(self.repo)))
        made = worktrees.from_body({'worktree': 'feature/kept'}, str(self.repo))
        for error in (OSError('failed'), RuntimeError('failed'), ValueError('failed'),
                      subprocess.SubprocessError('failed')):
            with self.subTest(error=error):
                with self.assertRaises(RuntimeError) as raised:
                    with worktrees.keeping(made):
                        raise error
                self.assertIn(made['path'], str(raised.exception))
                self.assertIn(made['branch'], str(raised.exception))
                self.assertIn('agent did not start: failed', str(raised.exception))
                self.assertIs(raised.exception.__cause__, error)
                self.assertTrue(Path(made['path']).is_dir())
                with self.assertRaises(type(error)) as unchanged:
                    with worktrees.keeping(None):
                        raise error
                self.assertIs(unchanged.exception, error)
        with worktrees.keeping(made):
            pass
