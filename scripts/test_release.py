"""A failed release run leaves nothing that stops the next run. Real git in temp folders; the slow checks are skipped."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import importlib.util
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

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
RUN_URL = 'https://github.com/example/tether/actions/runs/42'
RELEASE_URL = 'https://github.com/example/tether/releases/tag/v0.0.2'
run_process = subprocess.run


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
        self.ci_responses = None  # None means gh is missing
        self.failed_jobs = []
        self.gh_error = None
        self.release_responses = []

    def subprocess_step(self, args, **kwargs):
        if args[0] != 'gh':
            return run_process(args, **kwargs)
        self.commands.append(tuple(args))
        self.assertEqual(args, ['gh', 'release', 'view', 'v0.0.2', '-R', 'example/tether',
                                '--json', 'url', '--jq', '.url'])
        url = self.release_responses.pop(0)
        return subprocess.CompletedProcess(args, 0 if url else 1, stdout=url)

    def run_step(self, args, cwd, **kwargs):
        self.commands.append(tuple(args))
        if args[:3] == ['git', 'rev-parse', '--show-toplevel']:
            return str(self.repo)
        if args == ['git', 'remote', 'get-url', 'origin']:
            return 'https://github.com/example/tether.git'
        if args[0] == 'gh':
            self.assertIsNotNone(self.ci_responses)
            if self.gh_error:
                raise RuntimeError(self.gh_error)
            sha = git(self.origin, 'rev-parse', 'main')
            runs = f'repos/example/tether/actions/workflows/test.yml/runs?head_sha={sha}&event=push&per_page=100'
            jobs = 'repos/example/tether/actions/runs/42/jobs?per_page=100'
            self.assertEqual(args[:2], ['gh', 'api'])
            self.assertIn(args[2], (runs, jobs))
            if args[2] == jobs:
                return json.dumps([{'jobs': self.failed_jobs}])
            result = self.ci_responses.pop(0) if len(self.ci_responses) > 1 else self.ci_responses[0]
            return json.dumps([{'workflow_runs': result}])
        if args[0] != 'git':
            return None  # npm ci, the tests and the UI build
        for start in [key for key in self.hooks if list(key) == args[:len(key)]]:
            self.hooks.pop(start)(args, cwd)
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)  # as release.run, but quiet
        if result.returncode:
            raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def release(self, *args, fast=False):
        errors = io.StringIO()
        output = io.StringIO()
        self.commands = []
        self.sleeps = []
        clock = SimpleNamespace(monotonic=lambda: sum(self.sleeps), sleep=self.sleeps.append)
        gh_path = '/fake/gh' if self.ci_responses is not None else None
        if not fast:
            args += ('--local-checks',)
        with patch.object(release, 'run', self.run_step), patch.object(release.shutil, 'which', return_value=gh_path), \
                patch.object(release, 'time', clock), patch.object(release.subprocess, 'run', self.subprocess_step), \
                patch.object(sys, 'argv', ['release.py', *args]), patch.object(sys, 'stderr', errors), \
                patch.object(sys, 'stdout', output):
            try:
                return release.main()
            finally:
                self.errors = errors.getvalue()
                self.output = output.getvalue()

    @staticmethod
    def refuse(args, cwd):
        raise RuntimeError(f"{' '.join(args)} failed")

    def move_main(self, *_):
        (self.other / 'new').write_text('new')
        git(self.other, 'add', '-A')
        git(self.other, 'commit', '-q', '-m', 'main moves')
        git(self.other, 'push', '-q', 'origin', 'HEAD:main')

    def ci_run(self, status='completed', conclusion='success', **fields):
        return {
            'id': 42, 'html_url': RUN_URL, 'status': status, 'conclusion': conclusion,
            'head_sha': git(self.origin, 'rev-parse', 'main'), 'event': 'push',
            **fields,
        }

    def assert_no_release_writes(self):
        self.assertEqual(git(self.repo, 'tag', '--list'), '')
        self.assertEqual(git(self.origin, 'tag', '--list'), '')
        for command in [('git', 'worktree'), ('git', 'commit'), ('git', 'tag', '-a'), ('git', 'push')]:
            self.assertFalse(any(args[:len(command)] == command for args in self.commands), command)
        self.assertFalse((Path.home() / '.cache' / 'tether-release').exists())

    def assert_ci_refused(self, message):
        self.assertEqual(self.release('0.0.2', '--no-push', fast=True), 1)
        self.assertIn(message, self.errors)
        self.assertIn('fix main, or run with --local-checks', self.errors)
        self.assert_no_release_writes()

    def test_release_pushes_main_and_the_tag(self):
        self.assertEqual(self.release('0.0.2'), 0)
        self.assertEqual(git(self.origin, 'show', 'v0.0.2:VERSION'), '0.0.2')
        self.assertEqual(git(self.origin, 'rev-parse', 'main'), git(self.origin, 'rev-parse', 'v0.0.2^{commit}'))
        # B189: the release commit carries the stamp that its own checks earned.
        self.assertEqual(git(self.origin, 'log', '-1', '--format=%B', 'main').strip(),
                         f'Release 0.0.2\n\n{release.VERIFIED}')
        self.assertEqual([args for args in self.commands if args[0] != 'git'], [
            ('npm', 'ci'),
            ('python3', '-m', 'unittest', 'discover', '-q'),
            ('/bin/bash', '-c', 'npx tsc -b && npx eslint . && npm run build'),
        ])

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

    def test_fast_release_uses_passing_ci_without_local_checks(self):
        self.ci_responses = [[self.ci_run()]]
        sha = git(self.origin, 'rev-parse', 'main')
        self.assertEqual(self.release('0.0.2', '--no-push', fast=True), 0, self.errors)
        self.assertTrue(all(args[0] in ('git', 'gh') for args in self.commands))
        self.assertEqual(git(self.repo, 'log', '-1', '--format=%B', 'v0.0.2^{commit}'),
                         f'Release 0.0.2\n\nVerified: ci(tests run 42 success at {sha[:7]})')
        self.assertIn(RUN_URL, self.output)
        self.assertIn('total', self.output.splitlines()[-1])

    def test_fast_release_polls_ci_and_prints_only_state_changes(self):
        queued = self.ci_run('queued', None)
        running = self.ci_run('in_progress', None)
        self.ci_responses = [[queued], [queued], [running], [self.ci_run()]]
        self.assertEqual(self.release('0.0.2', '--no-push', '--ci-wait', '60', fast=True), 0, self.errors)
        self.assertEqual(self.sleeps, [15, 15, 15])
        self.assertEqual([line for line in self.output.splitlines() if line.startswith('CI:')], [
            f'CI: running (queued) {RUN_URL}', f'CI: running (in_progress) {RUN_URL}', f'CI: success {RUN_URL}',
        ])

    def test_fast_release_waits_for_the_published_release(self):
        self.ci_responses = [[self.ci_run()]]
        self.release_responses = ['', RELEASE_URL]
        self.assertEqual(self.release('0.0.2', fast=True), 0, self.errors)
        self.assertEqual(self.sleeps, [15])
        self.assertIn(('git', 'push', '--atomic', 'origin', 'HEAD:main', 'v0.0.2'), self.commands)
        self.assertIn(RELEASE_URL, self.output.splitlines())
        self.assertIn('GitHub build 15 s', self.output.splitlines()[-1])

    def test_fast_release_stops_at_ci_timeout(self):
        self.ci_responses = [[self.ci_run('in_progress', None)]]
        self.assertEqual(self.release('0.0.2', '--no-push', '--ci-wait', '20', fast=True), 1)
        self.assertEqual(self.sleeps, [15, 5])
        self.assertIn('timed out after 20 s', self.errors)
        self.assertIn(RUN_URL, self.errors)
        self.assertIn('--local-checks', self.errors)
        self.assert_no_release_writes()

    def test_failed_ci_reports_the_failed_jobs_and_url(self):
        self.ci_responses = [[self.ci_run(conclusion='failure')]]
        self.failed_jobs = [{'name': 'server', 'conclusion': 'success'},
                            {'name': 'browser', 'conclusion': 'failure'}]
        self.assert_ci_refused('failed jobs: browser')
        self.assertIn(RUN_URL, self.errors)

    def test_cancelled_or_skipped_ci_refuses_to_release(self):
        for conclusion in ('cancelled', 'skipped'):
            with self.subTest(conclusion=conclusion):
                self.ci_responses = [[self.ci_run(conclusion=conclusion)]]
                self.assert_ci_refused(conclusion)
                self.assertIn(RUN_URL, self.errors)

    def test_no_matching_ci_run_refuses_to_release(self):
        for runs in ([], [self.ci_run(head_sha='0' * 40), self.ci_run(event='pull_request')]):
            with self.subTest(runs=runs):
                self.ci_responses = [runs]
                self.assert_ci_refused('no tests run')

    def test_missing_gh_refuses_to_release(self):
        self.assert_ci_refused('gh is missing')

    def test_signed_out_gh_refuses_to_release(self):
        self.ci_responses = [[]]
        self.gh_error = 'To get started with GitHub CLI, please run: gh auth login'
        self.assert_ci_refused('gh auth login')

    def test_check_reports_the_live_base_without_updating_local_refs(self):
        local_sha = git(self.repo, 'rev-parse', 'origin/main')
        self.move_main()
        remote_sha = git(self.origin, 'rev-parse', 'main')
        self.ci_responses = [[self.ci_run()]]
        self.assertEqual(self.release('0.0.2', '--check', fast=True), 0, self.errors)
        self.assertEqual(self.output.splitlines()[:4], [
            'Version: 0.0.2 valid', 'Tag: v0.0.2 free locally, free on origin', f'Base: {remote_sha}',
            f'CI: success {RUN_URL}',
        ])
        self.assertFalse(any(args[:2] == ('git', 'fetch') for args in self.commands))
        self.assertEqual(git(self.repo, 'rev-parse', 'origin/main'), local_sha)
        self.assert_no_release_writes()

    def test_check_reports_running_ci_without_waiting(self):
        self.ci_responses = [[self.ci_run('in_progress', None)]]
        self.assertEqual(self.release('0.0.2', '--check', fast=True), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn(f'CI: running (in_progress) {RUN_URL}', self.output)
        self.assert_no_release_writes()

    def test_fast_release_checks_out_the_sha_even_if_the_tracking_ref_moves(self):
        self.ci_responses = [[self.ci_run()]]
        sha = git(self.repo, 'rev-parse', 'origin/main')

        def move_tracking_ref(*_):
            self.move_main()
            git(self.repo, 'fetch', 'origin')

        self.hooks[('git', 'worktree', 'add')] = move_tracking_ref
        self.assertEqual(self.release('0.0.2', '--no-push', '--keep', fast=True), 0, self.errors)
        worktree = Path.home() / '.cache' / 'tether-release' / '0.0.2'
        self.assertNotEqual(git(self.repo, 'rev-parse', 'origin/main'), sha)
        self.assertEqual(git(worktree, 'rev-parse', 'HEAD^'), sha)


if __name__ == '__main__':
    unittest.main()
