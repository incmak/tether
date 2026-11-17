"""A failed release run leaves nothing that stops the next run. Real git in temp folders; the slow checks are skipped."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def git(folder, *args):
    return subprocess.run(['git', *args], cwd=folder, check=True, text=True, capture_output=True).stdout.strip()


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        env = patch.dict(os.environ, {**GIT_ENV, 'HOME': str(base / 'home')})
        env.start()
        self.addCleanup(env.stop)
        self.origin, self.repo, self.other = base / 'origin.git', base / 'repo', base / 'other'
        git(base, 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        git(base, 'clone', '-q', str(self.origin), str(self.repo))
        plugin = self.repo / 'integrations/claude/tether/.claude-plugin/plugin.json'
        plugin.parent.mkdir(parents=True)
        plugin.write_text('{"version": "0.0.1"}\n')
        (self.repo / 'VERSION').write_text('0.0.1\n')
        (self.repo / 'ui').mkdir()
        (self.repo / 'server').mkdir()
        (self.repo / 'ui/keep').write_text('')
        (self.repo / 'server/keep').write_text('')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'start')
        git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        git(base, 'clone', '-q', str(self.origin), str(self.other))
        self.hooks = {}  # start of a git argument list -> a function that runs once, before the step; it may raise
        self.errors = ''

    def run_step(self, args, cwd, **kwargs):
        if args[:3] == ['git', 'rev-parse', '--show-toplevel']:
            return str(self.repo)
        if args[0] != 'git':
            return None  # npm ci, the tests and the UI build
        for start in [key for key in self.hooks if list(key) == args[:len(key)]]:
            self.hooks.pop(start)(args, cwd)
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)  # as release.run, but quiet
        if result.returncode:
            raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def release(self, *args):
        errors = io.StringIO()
        with patch.object(release, 'run', self.run_step), patch.object(release.shutil, 'which', lambda name: None), \
                patch.object(sys, 'argv', ['release.py', *args]), patch.object(sys, 'stderr', errors), \
                patch.object(sys, 'stdout', io.StringIO()):
            try:
                return release.main()
            finally:
                self.errors = errors.getvalue()

    @staticmethod
    def refuse(args, cwd):
        raise RuntimeError(f"{' '.join(args)} failed")

    def move_main(self, *_):
        (self.other / 'new').write_text('new')
        git(self.other, 'add', '-A')
        git(self.other, 'commit', '-q', '-m', 'main moves')
        git(self.other, 'push', '-q', 'origin', 'HEAD:main')

    def test_release_pushes_main_and_the_tag(self):
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'show', 'v0.0.2:VERSION'), '0.0.2')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'v0.0.2^{commit}'))

    def test_main_moves_during_the_run_then_the_next_run_works(self):
        self.hooks[('git', 'commit')] = self.move_main
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(git(self.origin, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'show', 'v0.0.2:new'), 'new')

    def test_failed_push_changes_nothing_then_the_next_run_works(self):
        self.hooks[('git', 'push')] = self.refuse
        before = git(self.origin, 'rev-parse', 'main')
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), before)
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'v0.0.2^{commit}'))

    def test_interrupt_after_the_tag_removes_it(self):
        def interrupt(args, cwd):
            raise KeyboardInterrupt
        self.hooks[('git', 'push')] = interrupt
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(self.release('0.0.2'), 0)

    def test_a_tag_from_another_process_is_not_removed(self):
        self.hooks[('git', 'tag', '-a')] = lambda args, cwd: git(self.repo, 'tag', 'v0.0.2', 'origin/main')
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'v0.0.2'), git(self.repo, 'rev-parse', 'origin/main'))

    def test_push_that_reached_origin_but_reported_a_failure_counts_as_done(self):
        def push_then_fail(args, cwd):
            subprocess.run(args, cwd=cwd, check=True, capture_output=True)
            raise RuntimeError('git push failed: the connection closed')
        self.hooks[('git', 'push')] = push_then_fail
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'v0.0.2^{commit}'))

    def test_unknown_push_result_keeps_the_tag_and_says_what_to_do(self):
        self.hooks[('git', 'push')] = self.refuse
        self.hooks[('git', 'ls-remote', 'origin', 'refs/tags/v0.0.2', 'refs/tags/v0.0.2^{}')] = self.refuse
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertIn('git ls-remote origin refs/tags/v0.0.2', self.errors)
        self.assertIn('git tag -d v0.0.2', self.errors)

    def test_origin_has_another_tag_of_this_version(self):
        def other_release(args, cwd):
            git(self.other, 'tag', 'v0.0.2')
            git(self.other, 'push', '-q', 'origin', 'v0.0.2')
        self.hooks[('git', 'push')] = other_release
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertIn('different v0.0.2', self.errors)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')

    def test_interrupt_after_the_push_arrived_keeps_the_published_tag(self):
        def push_then_interrupt(args, cwd):
            subprocess.run(args, cwd=cwd, check=True, capture_output=True)
            raise KeyboardInterrupt
        self.hooks[('git', 'push')] = push_then_interrupt
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertEqual(git(self.repo, 'rev-parse', 'v0.0.2'), git(self.origin, 'rev-parse', 'v0.0.2'))
        self.assertIn('the release is published', self.errors)

    def test_interrupt_during_the_push_with_origin_silent_keeps_the_tag(self):
        def interrupt(args, cwd):
            raise KeyboardInterrupt
        self.hooks[('git', 'push')] = interrupt
        self.hooks[('git', 'ls-remote', 'origin', 'refs/tags/v0.0.2', 'refs/tags/v0.0.2^{}')] = self.refuse
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertIn('git ls-remote origin refs/tags/v0.0.2', self.errors)

    def test_drop_tag_leaves_a_tag_at_another_commit(self):
        git(self.repo, 'tag', '-a', 'v0.0.2', '-m', 'other')
        self.assertEqual(release.drop_tag(self.repo, 'v0.0.2', '0' * 40), '')
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')

    def test_tag_that_cannot_be_removed_is_reported(self):
        def lock_then_fail(args, cwd):
            (self.repo / '.git/refs/tags/v0.0.2.lock').write_text('')
            self.refuse(args, cwd)
        self.hooks[('git', 'push')] = lock_then_fail
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertIn('git tag -d v0.0.2', self.errors)

    def test_no_push_keeps_the_local_tag(self):
        self.assertEqual(self.release('0.0.2', '--no-push'), 0)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertEqual(git(self.origin, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(self.release('0.0.2'), 1)  # a tag made on purpose still stops a second run


if __name__ == '__main__':
    unittest.main()
