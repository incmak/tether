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
        self.before_commit = None  # called once, just before the release commit
        self.fail_once = None  # a git argument list that fails once

    def run_step(self, args, cwd, **kwargs):
        if args[:3] == ['git', 'rev-parse', '--show-toplevel']:
            return str(self.repo)
        if args[0] != 'git':
            return None  # npm ci, the tests and the UI build
        if args[:2] == ['git', 'commit'] and self.before_commit:
            step, self.before_commit = self.before_commit, None
            step()
        if self.fail_once and args[:len(self.fail_once)] == self.fail_once:
            self.fail_once = None
            raise RuntimeError(f"{' '.join(args)} failed")
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)  # as release.run, but quiet
        if result.returncode:
            raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def release(self, *args):
        with patch.object(release, 'run', self.run_step), patch.object(release.shutil, 'which', lambda name: None), \
                patch.object(sys, 'argv', ['release.py', *args]), patch.object(sys, 'stderr', io.StringIO()):
            return release.main()

    def move_main(self):
        (self.other / 'new').write_text('new')
        git(self.other, 'add', '-A')
        git(self.other, 'commit', '-q', '-m', 'main moves')
        git(self.other, 'push', '-q', 'origin', 'HEAD:main')

    def test_release_pushes_main_and_the_tag(self):
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'show', 'v0.0.2:VERSION'), '0.0.2')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'v0.0.2^{commit}'))

    def test_main_moves_during_the_run_then_the_next_run_works(self):
        self.before_commit = self.move_main
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(git(self.origin, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'show', 'v0.0.2:new'), 'new')

    def test_failed_push_changes_nothing_then_the_next_run_works(self):
        self.fail_once = ['git', 'push']
        before = git(self.origin, 'rev-parse', 'main')
        self.assertEqual(self.release('0.0.2'), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), before)
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'v0.0.2^{commit}'))

    def test_interrupt_after_the_tag_removes_it(self):
        def interrupt(args, cwd, **kwargs):
            if args[:2] == ['git', 'push']:
                raise KeyboardInterrupt
            return self.run_step(args, cwd, **kwargs)
        with patch.object(release, 'run', interrupt), patch.object(sys, 'argv', ['release.py', '0.0.2']), \
                patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(release.main(), 1)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(self.release('0.0.2'), 0)

    def test_no_push_keeps_the_local_tag(self):
        self.assertEqual(self.release('0.0.2', '--no-push'), 0)
        self.assertEqual(git(self.repo, 'tag', '--list', 'v0.0.2'), 'v0.0.2')
        self.assertEqual(git(self.origin, 'tag', '--list', 'v0.0.2'), '')
        self.assertEqual(self.release('0.0.2'), 1)  # a tag made on purpose still stops a second run


if __name__ == '__main__':
    unittest.main()
