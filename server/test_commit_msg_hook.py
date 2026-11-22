"""B231: exercise the installed commit-msg hook with real Git and isolated state."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import hashlib
import hmac
import json
import os
import select
import shlex
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import commit_gate
import paths
import stamp

GATE = Path(commit_gate.__file__).resolve()
HELPER = GATE.with_name('commit_msg_hook.sh')
PID = 'abcdef012345'
SESSION = 'test-session'
STAMP = 'Verified: tests(server 412 ok)'
SCISSORS = '# ------------------------ >8 ------------------------'
# A well-formed start token that cannot belong to the current process.
EXITED_GIT_IDENTITY = {'git_pid': os.getpid(),
                       'git_started': 0 if sys.platform.startswith('linux') else 'Sat Jan  1 00:00:00 2000'}
HOLD_COMMIT_HOOK = (
    f'exec {shlex.quote(sys.executable)} -c '
    + shlex.quote('import os; print("hook ready", flush=True); os.read(int(os.environ["TETHER_TEST_HOOK_FD"]), 1)')
)


@contextmanager
def held_commit(command, *, cwd, env):
    """Wait for the hook, then release it and reap Git even when an assertion fails."""
    hook_read, release = os.pipe()
    with os.fdopen(hook_read, 'rb') as reader, os.fdopen(release, 'wb') as writer:
        env = dict(env, TETHER_TEST_HOOK_FD=str(reader.fileno()))
        with subprocess.Popen(command, cwd=cwd, env=env, pass_fds=(reader.fileno(),),
                              stderr=subprocess.PIPE, text=True) as git:
            reader.close()
            try:
                if not select.select([git.stderr], [], [], 10)[0]:
                    raise AssertionError('Git hook did not report readiness')
                ready = git.stderr.readline()
                if ready != 'hook ready\n':
                    raise AssertionError(f'Git hook did not report readiness: {ready!r}')
                yield git
            finally:
                writer.close()
                try:
                    _, stderr = git.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    git.kill()
                    git.communicate()
                    raise
            if git.returncode:
                raise subprocess.CalledProcessError(git.returncode, command, stderr=stderr)


class AncestorTimes(unittest.TestCase):
    def test_ps_elapsed_time_sets_start_without_stopping_on_parse_failure(self):
        now = 1_000_000
        rows = [subprocess.CompletedProcess([], 0, stdout=row) for row in
                ('40 05:03 /usr/bin/git\n', '30 1:02:03 /bin/sh\n', '20 invalid relay\n',
                 '10 2-01:02:03 claude\n', '0 00:00 init\n')]
        with (mock.patch.object(commit_gate.sys, 'platform', 'darwin'),
              mock.patch.object(commit_gate.os, 'getppid', return_value=50),
              mock.patch.object(commit_gate.subprocess, 'run', side_effect=rows) as ps,
              mock.patch.object(commit_gate.time, 'time', return_value=now),
              mock.patch.object(commit_gate.time, 'monotonic', return_value=0)):
            ancestors = list(commit_gate._ancestors())
        self.assertEqual(ancestors, [(50, 'git', now - 303), (40, 'sh', now - 3723), (30, 'relay', None),
                                     (20, 'claude', now - 176523), (10, 'init', now)])
        self.assertEqual(ps.call_args_list[0].args[0], ['ps', '-o', 'ppid=,etime=,comm=', '-p', '50'])


class CommitMsgHook(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.config = self.root / 'config'
        self.cache = self.root / 'cache'
        self.enterContext(mock.patch.object(paths, 'CACHE', self.cache))
        self.tether = self.root / 'tether'
        self.log = self.root / 'log' / 'commit-gate.log'
        self.socket = self.root / 'tmux-sockets'
        for folder in (self.repo, self.home, self.config, self.socket):
            folder.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('GIT_') and key not in {
                        'TMUX', 'TMUX_PANE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDECODE', 'CODEX_THREAD_ID',
                        'CODEX_SESSION_ID', 'CLAUDE_PID', 'MOBILE_TMUX_TMUX', 'TETHER_PYTHON',
                    }}
        self.env.update(HOME=str(self.home), MOBILE_TMUX_HOME=str(self.home), MOBILE_TMUX_CACHE=str(self.cache),
                        MOBILE_TMUX_CONFIG=str(self.config), TETHER_HOME=str(self.tether),
                        TETHER_COMMIT_GATE_LOG=str(self.log), TMUX_TMPDIR=str(self.socket),
                        GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Tester', GIT_COMMITTER_NAME='Tester',
                        GIT_AUTHOR_EMAIL='test@example.com', GIT_COMMITTER_EMAIL='test@example.com', LC_ALL='C')
        # The sandbox rejects Unix socket connections before tmux can report that its server is absent.
        self.env['MOBILE_TMUX_TMUX'] = self.script(self.root / 'tmux', 'printf "no server running\\n" >&2\nexit 1')
        git_init = self.git('init', '-q', '-b', 'main', '--template=')
        self.assertEqual(git_init.stderr, '')
        self.hooks = self.repo / '.git' / 'hooks'
        self.hook = self.hooks / 'commit-msg'
        # Keep the real parent walk inside this fixture even when an agent started the test runner.
        launcher = f'''import runpy
import sys

gate = runpy.run_path(sys.argv[1])
main = gate['_main']
ancestors = gate['_ancestors']

def fixture_ancestors():
    for ancestor in ancestors():
        yield ancestor
        if ancestor.pid == {os.getpid()}:
            break

main.__globals__['_ancestors'] = fixture_ancestors
sys.exit(main(sys.argv[2:]))
'''
        self.python = self.script(self.root / 'python', f'exec {shlex.quote(sys.executable)} -c '
                                  f'{shlex.quote(launcher)} "$@"')
        self.env['TETHER_PYTHON'] = self.python

    def git(self, *args, check=True, command=('git',), cwd=None):
        return subprocess.run([*command, *args], cwd=cwd or self.repo, env=self.env, capture_output=True,
                              text=True, check=check, timeout=20)

    def cli(self, *args, check=True):
        return self.git(str(GATE), *args, check=check, command=(sys.executable,))

    def install(self, repo=None, **options):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return commit_gate.install_hook(repo or self.repo, **{'gate': GATE, 'python': self.python, **options})

    def gate_with_import_error(self):
        gate = self.root / 'broken' / GATE.name
        gate.parent.mkdir()
        gate.write_text('import tether_missing_module\n' + GATE.read_text())
        return gate

    def mode(self, mode):
        (self.config / 'commit-gate').write_text(mode + '\n')

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def user_hook_line(self, **options):
        self.script(self.hook, 'exit 0')
        with self.assertRaises(commit_gate.GateError) as raised:
            self.install(**options)
        return str(raised.exception).splitlines()[-1]

    def commit(self, *args, check=True, command=('git',)):
        return self.git('commit', '--allow-empty', *args, check=check, command=command)

    def write_file(self, text):
        (self.repo / 'file').write_text(text + '\n')
        self.git('add', 'file')

    def script(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
        return str(path)

    def agent(self, name, child='git'):
        # Keep the process alive as a parent of Git, even in shells that optimize a final exec.
        path = self.script(self.root / 'bin' / name, f'{shlex.quote(child)} "$@"\nexit $?')
        return (path,)

    def pre(self, command, event='PreToolUse', tool='tool-1', session=SESSION, *, now=None):
        data = {'hook_event_name': event, 'tool_name': 'Bash', 'session_id': session, 'cwd': str(self.repo),
                'tool_use_id': tool, 'tool_input': {'command': command}}
        cli = [sys.executable]
        if now is not None:
            cli += ['-c', 'import runpy, sys, time; now = float(sys.argv.pop(1)); time.time = lambda: now; '
                    'runpy.run_path(sys.argv[1], run_name="__main__")', str(now)]
        return subprocess.run([*cli, str(GATE.with_name('claude_hooks.py'))], input=json.dumps(data),
                              cwd=self.repo, env=self.env, capture_output=True, text=True, check=True, timeout=20)

    def test_observe_logs_one_terminal_verdict_without_message_text(self):
        self.install()
        self.commit('-m', 'private test message')
        check, = self.rows()
        key_path = self.cache / 'commit-gate' / 'commit-gate.key'
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        key = key_path.read_bytes()
        self.assertEqual(len(key), 32)
        digest = hmac.digest(key, b'private test message', 'sha256').hex()[:32]
        self.assertRegex(check['messages'][0], r'^[0-9a-f]{32}$')
        started = check.pop('started')
        self.assertGreater(check.pop('git_pid'), 0)
        self.assertTrue(check.pop('git_started'))
        author_date, committed = self.git('log', '-1', '--date=raw', '--format=%ad%n%ct').stdout.splitlines()
        self.assertIs(type(started), int)
        self.assertLessEqual(started, int(committed))
        self.assertLessEqual(int(committed), check['at'] + commit_gate.LOG_TIME_TOLERANCE)
        self.assertEqual({key: value for key, value in check.items() if key != 'at'}, {
            'event': 'check', 'via': 'commit-msg', 'source': 'terminal', 'id': None, 'session': None,
            'project': None, 'mode': 'observe', 'verdict': 'would_block', 'reason': 'no-line',
            'count': 1, 'folder': str(self.repo), 'head': None,
            'tree': self.git('rev-parse', 'HEAD^{tree}').stdout.strip(),
            'reflog': {'size': 0, 'ino': None, 'dev': None},
            'git_dir': self.git_dir_fields(),
            'author_date': author_date,
            'messages': [digest],
        })
        self.assertIs(commit_gate.record_landed(check), True)
        self.assertNotIn('private test message', self.log.read_text())

    def test_record_landed_for_commit_and_amend_after_reset(self):
        self.commit('-m', 'base')
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        before = commit_gate._head_reflog_position(self.repo)
        self.install()
        self.write_file('initial change')
        self.commit('-m', 'initial')
        initial = self.git('rev-parse', 'HEAD').stdout.strip()
        self.write_file('amended change')
        self.commit('--amend', '-m', 'amended')
        commit, amend = self.rows()
        self.assertEqual(commit['reflog'], before)
        self.assertEqual(amend['head'], initial)
        self.assertEqual(self.git('rev-parse', 'HEAD^').stdout.strip(), base)
        self.git('reset', '--hard', base)
        self.assertIs(commit_gate.record_landed(commit), True)
        self.assertIs(commit_gate.record_landed(amend), True)

    def test_hook_records_linked_worktree_git_directory(self):
        self.commit('-m', 'base')
        self.install()
        worktree = self.root / 'linked worktree'
        self.git('worktree', 'add', '-qb', 'linked', str(worktree))
        self.git('commit', '--allow-empty', '-m', 'linked commit', cwd=worktree)
        row, = self.rows()
        self.assertEqual(row['git_dir'], self.git_dir_fields(cwd=worktree))
        self.assertIs(commit_gate.record_landed(row), True)

    def test_record_landed_for_merge(self):
        self.prepare_merge()
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('merge', '--no-ff', '--no-commit', 'side')
        self.commit('-m', 'merge side')
        row, = self.rows()
        self.assertEqual(row['head'], head)
        self.assertEqual(len(self.git('show', '-s', '--format=%P').stdout.split()), 2)
        self.assertIs(commit_gate.record_landed(row), True)

    def test_record_landed_for_direct_merge_with_hook(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-edit', 'side')
        row, = self.rows()
        self.assertIs(commit_gate.record_landed(row), True)

    def test_record_landed_for_in_index_merge(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-edit', '-s', 'resolve', 'side')
        row, = self.rows()
        self.assertIn('In-index merge', self.git('reflog', '-1', '--format=%gs').stdout)
        self.assertIs(commit_gate.record_landed(row), True)

    def test_record_landed_for_empty_commit_message(self):
        self.install()
        self.commit('--allow-empty-message', '-m', '')
        row, = self.rows()
        self.assertIs(commit_gate.record_landed(row), True)

    def test_record_landed_for_same_oid_amend(self):
        date = '2000-01-01T00:00:00+00:00'
        self.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.commit('-m', 'base')
        self.install()
        self.commit('--amend', '--no-edit')
        row, = self.rows()
        self.assertEqual(row['head'], self.git('rev-parse', 'HEAD').stdout.strip())
        self.assertIs(commit_gate.record_landed(row), True)

    def test_detached_same_oid_amend_without_reflog_is_unknown(self):
        date = '2000-01-01T00:00:00+00:00'
        self.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'side')
        self.write_file('other tree')
        self.commit('-m', 'side')
        self.git('switch', '-q', '--detach', 'side~1')
        before = commit_gate._head_reflog_position(self.repo)
        self.install()
        self.commit('--amend', '--no-edit')
        row, = self.rows()
        self.assertEqual(row['head'], self.git('rev-parse', 'HEAD').stdout.strip())
        self.assertEqual(commit_gate._head_reflog_position(self.repo), before)
        self.assertIsNone(commit_gate.record_landed(row))
        # A later checkout of another tree must not turn the landed amend into a proven non-landing.
        self.git('checkout', '-q', '--detach', 'side')
        self.assertNotEqual(self.git('rev-parse', 'HEAD^{tree}').stdout.strip(), row['tree'])
        self.assertIsNone(commit_gate.record_landed(row))

    def test_detached_same_oid_amend_after_a_hook_restores_the_message_is_unknown(self):
        date = '2000-01-01T00:00:00+00:00'
        self.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.commit('-m', 'base')
        self.git('switch', '--detach', 'HEAD')
        before = commit_gate._head_reflog_position(self.repo)
        # Tether reads "changed"; the repo hook after it writes the old message back, so the OID stays the same.
        self.script(self.hook, f'{self.user_hook_line()}\nprintf "base\\n" > "$1"')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.commit('--amend', '-m', 'changed')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), head)
        self.assertEqual(commit_gate._head_reflog_position(self.repo), before)
        row, = self.rows()
        self.assertIsNone(commit_gate.record_landed(row))

    def test_amend_at_shallow_boundary_uses_gits_parent_list(self):
        self.commit('-m', 'base')
        self.commit('-m', 'tip')
        shallow = self.root / 'shallow'
        self.git('clone', '--depth=1', self.repo.as_uri(), str(shallow))
        self.install(repo=shallow)
        self.git('commit', '--amend', '--allow-empty', '-m', 'Amended shallow tip', cwd=shallow)
        row, = self.rows()
        self.assertIs(commit_gate.record_landed(row), True)

    def test_later_commit_with_different_message_cannot_confirm_an_aborted_row(self):
        self.commit('-m', 'base')
        self.write_file('change')
        hook = self.user_hook_line()
        self.script(self.hook, f'{hook}\nexit 1')
        self.assertNotEqual(self.commit('-m', 'Same subject', '-m', 'Aborted body', check=False).returncode, 0)
        self.script(self.hook, hook)
        self.commit('-m', 'Same subject', '-m', 'Landed body')
        aborted, landed = self.rows()
        self.assertEqual(aborted['tree'], landed['tree'])
        self.assertEqual(aborted['head'], landed['head'])
        self.assertIsNone(commit_gate.record_landed(aborted))
        self.assertIs(commit_gate.record_landed(landed), True)

    def test_message_rewritten_after_the_gate_is_unknown(self):
        self.script(self.hook, f'{self.user_hook_line()}\nprintf "Rewritten message\\n" > "$1"')
        self.commit('-m', 'Original message')
        row, = self.rows()
        self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 'Rewritten message')
        self.assertIsNone(commit_gate.record_landed(row))

    def test_merge_row_requires_the_recorded_merge_parents(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-edit', 'side')
        row, = self.rows()
        self.assertEqual(row['merge_heads'], [self.git('rev-parse', 'side').stdout.strip()])
        self.assertIs(commit_gate.record_landed(row), True)
        self.assertIsNone(commit_gate.record_landed({**row, 'merge_heads': [row['head']]}))

    def test_landing_requires_explicit_dates_when_recorded(self):
        self.commit('-m', 'base')
        date = '2000-01-01T00:00:00+00:00'
        self.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.install()
        self.commit('-m', 'dated commit')
        row, = self.rows()
        self.assertIs(commit_gate.record_landed(row), True)
        for field in ('author_date', 'committer_date'):
            with self.subTest(field=field):
                self.assertIsNone(commit_gate.record_landed({**row, field: '1 +0000'}))

    def git_dir_fields(self, *, cwd=None):
        path = Path(self.git('rev-parse', '--absolute-git-dir', cwd=cwd).stdout.rstrip('\n'))
        info = path.stat()
        return {'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino}

    def test_record_landed_is_unknown_after_checkout_to_staged_tree(self):
        self.commit('-m', 'base')
        self.git('switch', '-qc', 'same-tree')
        self.write_file('same change')
        self.commit('-m', 'matching tree')
        self.git('switch', '-q', 'main')
        self.script(self.hook, f'{self.user_hook_line()}\nexit 1')
        self.write_file('same change')
        self.assertNotEqual(self.commit('-m', 'abort', check=False).returncode, 0)
        row, = self.rows()
        self.git('switch', '-qf', 'same-tree')
        self.assertNotEqual(self.git('rev-parse', 'HEAD').stdout.strip(), row['head'])
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}').stdout.strip(), row['tree'])
        self.assertIsNone(commit_gate.record_landed(row))

    def test_record_landed_is_unknown_after_cherry_pick_fast_forward(self):
        self.commit('-m', 'base')
        self.git('switch', '-qc', 'existing')
        self.write_file('same change')
        self.commit('-m', 'existing commit')
        self.git('switch', '-q', 'main')
        self.script(self.hook, f'{self.user_hook_line()}\nexit 1')
        self.write_file('same change')
        self.assertNotEqual(self.commit('-m', 'abort', check=False).returncode, 0)
        row, = self.rows()
        self.git('reset', '--hard', 'HEAD')
        self.git('cherry-pick', '--ff', 'existing')
        self.assertEqual(self.git('reflog', '-1', '--format=%gs').stdout.strip(), 'cherry-pick: fast-forward')
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}').stdout.strip(), row['tree'])
        self.assertIsNone(commit_gate.record_landed(row))

    def test_record_landed_is_false_when_reflog_write_precedes_failed_ref_update(self):
        self.commit('-m', 'base')
        self.script(self.hook, f'{self.user_hook_line()}\nexit 1')
        self.write_file('aborted change')
        self.assertNotEqual(self.commit('-m', 'abort', check=False).returncode, 0)
        row, = self.rows()
        dangling = self.git('commit-tree', row['tree'], '-p', row['head'], '-m', 'abort').stdout.strip()
        reflog = self.repo / '.git' / 'logs' / 'HEAD'
        ident = self.git('var', 'GIT_COMMITTER_IDENT').stdout.strip()
        with reflog.open('a') as stream:
            stream.write(f"{row['head']} {dangling} {ident}\tcommit: abort\n")
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), row['head'])
        self.assertIs(commit_gate.record_landed(row), False)

    def test_record_landed_is_unknown_for_root_row_in_replacement_repository(self):
        self.script(self.hook, f'{self.user_hook_line()}\nexit 1')
        self.assertNotEqual(self.commit('-m', 'abort', check=False).returncode, 0)
        row, = self.rows()
        self.assertIsNone(row['head'])
        self.repo.rename(self.root / 'original')
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main', '--template=')
        self.commit('-m', 'same tree')
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}').stdout.strip(), row['tree'])
        self.assertIsNone(commit_gate.record_landed(row))

    def test_record_landed_rejects_aborted_commit_and_unrelated_checkout(self):
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'other')
        self.write_file('unrelated change')
        self.commit('-m', 'other')
        self.git('checkout', '-q', 'main')
        self.script(self.hook, f'{self.user_hook_line()}\nexit 1')
        self.write_file('aborted change')
        self.assertNotEqual(self.commit('-m', 'abort', check=False).returncode, 0)
        row, = self.rows()
        self.assertEqual(row['verdict'], 'would_block')
        self.assertIs(commit_gate.record_landed(row), False)
        self.git('checkout', '-qf', 'other')
        self.assertNotEqual(self.git('rev-parse', 'HEAD^{tree}').stdout.strip(), row['tree'])
        self.assertIs(commit_gate.record_landed(row), False)

    def test_record_landed_is_unknown_while_hook_runs_without_process_identity(self):
        self.commit('-m', 'base')
        self.script(self.hook, f'{self.user_hook_line()}\n{HOLD_COMMIT_HOOK}')
        with held_commit(['git', 'commit', '--allow-empty', '-qm', 'Held commit'], cwd=self.repo, env=self.env) as git:
            row, = self.rows()
            self.assertEqual(row['git_pid'], git.pid)
            self.assertEqual(commit_gate.record_landed(row), 'pending')
            self.assertEqual(commit_gate._head_reflog_position(self.repo), row['reflog'])
            no_identity = {key: value for key, value in row.items() if key not in ('git_pid', 'git_started')}
            for identity in ({}, {'git_pid': git.pid}, {'git_started': row['git_started']},
                             {'git_pid': True, 'git_started': row['git_started']},
                             {'git_pid': git.pid, 'git_started': 'invalid'}):
                with self.subTest(identity=identity):
                    self.assertIsNone(commit_gate.record_landed(no_identity | identity))
        self.assertIs(commit_gate.record_landed(no_identity), True)

    def test_hook_git_alive_is_unknown_when_the_process_cannot_be_read(self):
        """A slow ps or an unreadable /proc must not look like an exited Git (that would skip a live commit)."""
        deadline = time.monotonic() + 5
        linux_row = {'git_pid': 4242, 'git_started': 100}
        mac_row = {'git_pid': 4242, 'git_started': 'Sat Jan  1 00:00:00 2000'}
        with mock.patch.object(commit_gate.sys, 'platform', 'linux'):
            for legacy in (100.0, -1, None, '100'):  # a wall-clock start from older rows is unknown
                with self.subTest(legacy=legacy):
                    self.assertIsNone(commit_gate._hook_git_alive({**linux_row, 'git_started': legacy},
                                                                  deadline=deadline))
            for error, expected in ((PermissionError(), None), (ValueError(), None),
                                    (FileNotFoundError(), False), (ProcessLookupError(), False)):
                with self.subTest(linux=type(error).__name__), \
                        mock.patch.object(commit_gate, '_linux_start_ticks', side_effect=error):
                    self.assertIs(commit_gate._hook_git_alive(linux_row, deadline=deadline), expected)
            # Raw start ticks: a corrected wall-clock boot time (btime) does not change the answer.
            for answer, expected in (((100, b'S'), True), ((101, b'S'), False), ((100, b'Z'), False)):
                with self.subTest(linux=answer), \
                        mock.patch.object(commit_gate, '_linux_start_ticks', return_value=answer), \
                        mock.patch.object(commit_gate, '_boot_clock', side_effect=AssertionError('btime read')):
                    self.assertIs(commit_gate._hook_git_alive(linux_row, deadline=deadline), expected)
        done = subprocess.CompletedProcess
        with mock.patch.object(commit_gate.sys, 'platform', 'darwin'):
            for result, expected in ((subprocess.TimeoutExpired('ps', 0.5), None), (OSError(), None),
                                     (done([], 1, '', ''), False), (done([], 2, '', 'ps: bad'), None),
                                     (done([], 1, '', 'ps: kvm_getprocs: error\n'), None),
                                     (done([], 0, 'S    Sat Jan  1 00:00:00 2000\n', ''), True),
                                     (done([], 0, 'Z    Sat Jan  1 00:00:00 2000\n', ''), False),
                                     (done([], 0, 'S    Sun Jan  2 00:00:00 2000\n', ''), False),
                                     (done([], 0, '', ''), None)):
                kwargs = {'side_effect': result} if isinstance(result, BaseException) else {'return_value': result}
                with self.subTest(mac=repr(result)), mock.patch.object(commit_gate.subprocess, 'run', **kwargs):
                    self.assertIs(commit_gate._hook_git_alive(mac_row, deadline=deadline), expected)

    def test_record_landed_uses_all_reflog_entries_for_the_same_commit(self):
        date = '2000-01-01T00:00:00+00:00'
        self.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'other')
        self.write_file('change')
        self.commit('-m', 'other')
        self.git('checkout', '-q', 'main')
        self.git('checkout', '-qb', 'intermediate')
        self.write_file('intermediate change')
        self.commit('-m', 'intermediate')
        self.git('checkout', '-q', 'main')
        row = {'folder': str(self.repo), 'head': self.git('rev-parse', 'HEAD').stdout.strip(),
               'tree': self.git('rev-parse', 'other^{tree}').stdout.strip(),
               'reflog': commit_gate._head_reflog_position(self.repo), 'git_dir': self.git_dir_fields(),
               'messages': [commit_gate._message_digest(commit_gate._message_key(create=True), 'other')],
               **EXITED_GIT_IDENTITY}
        self.git('checkout', '-q', 'intermediate')
        self.git('checkout', '-q', 'other')
        self.assertIs(commit_gate.record_landed(row), False)
        self.git('checkout', '-q', 'main')
        self.write_file('change')
        self.commit('-m', 'other')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, self.git('rev-parse', 'other').stdout)
        self.assertIs(commit_gate.record_landed(row), True)

    def test_record_landed_is_unknown_without_reliable_git_evidence(self):
        self.commit('-m', 'base')
        self.install()
        self.commit('-m', 'landed')
        row, = self.rows()
        for field in ('tree', 'head', 'reflog', 'git_dir', 'messages'):
            with self.subTest(missing=field):
                self.assertIsNone(commit_gate.record_landed({k: v for k, v in row.items() if k != field}))
        for fields in ({'tree': 'bad'}, {'head': 'bad'}, {'reflog': {'size': -1, 'ino': None, 'dev': None}},
                       {'folder': str(self.root / 'removed')}, {'folder': '.'}):
            with self.subTest(fields=fields):
                self.assertIsNone(commit_gate.record_landed(row | fields))
        reflog = self.repo / '.git' / 'logs' / 'HEAD'
        original = reflog.read_bytes()
        for tail in (b'malformed\n', b'x' * commit_gate.REFLOG_BYTES):
            reflog.write_bytes(original + tail)
            with self.subTest(tail_length=len(tail)):
                self.assertIsNone(commit_gate.record_landed(row))
        replacement = reflog.with_name('HEAD.replaced')
        replacement.write_bytes(original)
        replacement.replace(reflog)
        self.assertIsNone(commit_gate.record_landed(row))

    def test_enforce_checks_stamp_grammar_and_never_counts_terminal_blocks(self):
        self.mode('enforce')
        self.install()
        for message in ('no stamp', 'bad stamp\n\nVerified: tests()', 'still missing'):
            with self.subTest(message=message):
                failed = self.commit('-m', message, check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(failed.stderr.splitlines(), [
                    'Tether commit gate: this commit message has no valid "Verified:" line. Fix: add a last line '
                    '"Verified: none (reason)", or a stamp such as "Verified: tests(server 412 ok)", and commit again.'])
                self.assertNotEqual(self.git('rev-parse', '--verify', 'HEAD', check=False).returncode, 0)
        self.commit('-m', 'checked', '-m', STAMP)
        self.commit('-m', 'unchecked', '-m', 'Verified: none (x)')
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')
        self.assertEqual([row['verdict'] for row in self.rows()], ['block'] * 3 + ['pass', 'pass'])

    def test_hook_message_file_requires_valid_utf8_for_a_verdict(self):
        self.mode('enforce')
        message = self.repo / '.git' / 'COMMIT_EDITMSG'
        for content, verdict, reason in (
            (b'Legacy\n\nVerified: none\xa0\n', 'unknown', 'encoding'),
            (b'Legacy: \xc3\n\nVerified: none\n', 'unknown', 'encoding'),
            ('Café\n\nVerified: none (café)\n'.encode(), 'pass', ''),
            ('Café\n\nVerified: café\n'.encode(), 'block', 'no-line'),
        ):
            with self.subTest(content=content):
                message.write_bytes(content)
                result = self.git(str(HELPER), str(message), self.python, str(GATE), command=('/bin/sh',), check=False)
                self.assertEqual(result.returncode, int(verdict == 'block'), result.stderr)
                row = self.rows()[-1]
                self.assertEqual((row['mode'], row['verdict'], row['reason']), ('enforce', verdict, reason))

    def test_off_has_no_row(self):
        self.mode('off')
        self.install()
        self.commit('-m', 'no stamp')
        self.assertEqual(self.rows(), [])

    def test_project_override_by_pane_and_ancestor_pid(self):
        self.mode('observe')
        path = self.tether / 'projects' / PID / 'commit-gate'
        path.parent.mkdir(parents=True)
        path.write_text('{"override": true, "mode": "enforce"}')
        self.install()
        # The test process is an ancestor in both hook invocations. The cwd query has no match.
        self.env['MOBILE_TMUX_TMUX'] = self.script(self.root / 'tmux', f'''
case "$1:$4" in
    display-message:%19) printf '%s\\n' {PID} ;;
    "list-panes:#{{pane_pid}}"*) printf '%s\\t%s\\n' {os.getpid()} {PID} ;;
    *) exit 2 ;;
esac''')
        for pane in (True, False):
            if pane:
                self.env['TMUX_PANE'] = '%19'
            else:
                del self.env['TMUX_PANE']
            self.assertNotEqual(self.commit('-m', 'no stamp', check=False).returncode, 0)
        self.assertEqual([(r['project'], r['mode'], r['verdict']) for r in self.rows()],
                         [(PID, 'enforce', 'block')] * 2)

    def test_tmux_failure_cannot_enforce(self):
        self.mode('enforce')
        self.install()
        self.env['MOBILE_TMUX_TMUX'] = self.script(self.root / 'tmux', 'exit 2')
        self.commit('-m', 'no stamp')
        row, = self.rows()
        self.assertEqual((row['verdict'], row['project_unsure']), ('would_block', True))

    def test_project_lookup_falls_back_to_worktree_path(self):
        path = self.tether / 'projects' / PID / 'commit-gate'
        path.parent.mkdir(parents=True)
        path.write_text('{"override": true, "mode": "enforce"}')
        self.install()
        self.env['MOBILE_TMUX_TMUX'] = self.script(self.root / 'tmux', f'''
case "$4" in
    "#{{pane_pid}}"*) printf '0\\t\\n' ;;
    "#{{pane_current_path}}"*) printf '%s\\t%s\\n' {shlex.quote(str(self.repo))} {PID} ;;
    *) exit 2 ;;
esac''')
        self.assertNotEqual(self.commit('-m', 'no stamp', check=False).returncode, 0)
        row, = self.rows()
        self.assertEqual((row['project'], row['mode'], row['verdict']), (PID, 'enforce', 'block'))

    def test_corrupt_agent_session_fails_open_and_logs_unknown(self):
        self.mode('enforce')
        self.install()
        self.env['CODEX_THREAD_ID'] = SESSION
        path = self.cache / 'commit-gate' / f'{SESSION}.json'
        path.parent.mkdir(parents=True)
        path.write_text('{')
        result = self.commit('-m', 'no stamp')
        self.assertEqual(result.stderr, '')
        self.assertEqual(path.read_text(), '{')
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason'], row['session']), ('unknown', 'error', SESSION))

    def test_broken_log_and_missing_gate_fail_open(self):
        self.mode('enforce')
        self.install()
        self.log.parent.write_text('cannot create the log under a file')
        self.assertEqual(self.commit('-m', 'log failure').stderr, '')
        self.install(gate=self.root / 'missing gate.py')
        self.assertEqual(self.commit('-m', 'gate missing').stderr, '')
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')

    def test_cli_installs_updates_and_uninstalls_hook(self):
        self.assertIn('commit-msg', self.cli('install-hook', '--repo', str(self.repo)).stdout)
        self.assertEqual(self.hook.stat().st_mode & 0o777, 0o755)
        self.assertIn('# tether-commit-gate-hook v1', self.hook.read_text().splitlines())
        self.commit('-m', 'direct CLI', '-m', STAMP)
        self.assertEqual(self.rows()[0]['verdict'], 'pass')
        self.install()
        self.assertEqual(self.cli('commit-msg').returncode, 0)
        self.cli('install-hook', '--uninstall')
        self.assertFalse(self.hook.exists())

    def test_custom_hooks_directory_and_paths_with_shell_characters(self):
        hooks = self.root / "external hooks' dir"
        self.git('config', 'core.hooksPath', str(hooks))
        python = self.root / "python' path"
        python.symlink_to(sys.executable)
        # Python still imports Tether's modules relative to the resolved script path.
        gate = self.root / "gate' path.py"
        gate.symlink_to(GATE)
        self.install(gate=gate, python=python)
        self.commit('-m', 'no stamp')
        self.assertEqual(self.rows()[0]['verdict'], 'would_block')
        self.assertTrue((hooks / 'commit-msg').is_file())

    def test_linked_worktree_shares_hook_but_logs_its_own_root(self):
        self.commit('-m', 'base')
        linked = self.root / 'linked'
        self.git('worktree', 'add', '-b', 'linked', str(linked))
        self.install()
        child = linked / 'subdir'
        child.mkdir()
        self.git('commit', '--allow-empty', '-m', 'linked', cwd=child)
        row, = self.rows()
        self.assertEqual((row['folder'], row['verdict']), (str(linked), 'would_block'))

    def test_foreign_hook_is_refused_and_left_intact(self):
        self.script(self.hook, 'test "${0##*/}" = commit-msg')
        self.hook.chmod(0o711)
        original = self.hook.read_bytes()
        python = self.root / "python's path"
        gate = self.root / 'gate path.py'
        with self.assertRaises(commit_gate.GateError) as raised:
            self.install(gate=gate, python=python)
        self.assertIn(str(self.hook), str(raised.exception))
        self.assertIn('add this line to your hook', str(raised.exception))
        self.assertIn(f'/bin/sh {shlex.quote(str(HELPER))} "$1" '
                      '|| [ $? -ne 1 ] || exit 1  # tether-commit-gate-call v1',
                      str(raised.exception))
        self.install(uninstall=True)
        self.assertEqual(self.hook.read_bytes(), original)
        self.assertEqual(self.hook.stat().st_mode & 0o777, 0o711)
        self.assertEqual(list(self.hooks.glob('commit-msg*')), [self.hook])

    def concurrent_installs(self):
        barrier = threading.Barrier(2, timeout=5)

        def install():
            barrier.wait()
            try:
                commit_gate.install_hook(self.repo, gate=GATE, python=self.python)
            except commit_gate.GateError as exc:
                return str(exc)
            return None

        with mock.patch.dict(os.environ, self.env, clear=True), ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(install) for _ in range(2)]
            return [attempt.result(timeout=10) for attempt in attempts]

    def test_concurrent_installs_refuse_the_same_foreign_hook(self):
        self.script(self.hook, 'exit 42')
        original = self.hook.read_bytes()
        for result in self.concurrent_installs():
            self.assertIn('add this line to your hook', result or '')
        self.assertEqual(self.hook.read_bytes(), original)
        self.assertEqual(list(self.hooks.glob('commit-msg*')), [self.hook])

    def test_user_hook_line_passes_stamped_commits_and_only_blocks_in_enforce(self):
        self.mode('enforce')
        line = self.user_hook_line()
        self.script(self.hook, line)
        original = self.hook.read_bytes()
        self.assertTrue(line.endswith('  # tether-commit-gate-call v1'))
        with self.assertRaises(commit_gate.GateError):
            self.install()
        self.install(uninstall=True)
        self.assertEqual(self.hook.read_bytes(), original)
        self.commit('-m', 'checked', '-m', STAMP)
        self.assertNotEqual(self.commit('-m', 'unstamped', check=False).returncode, 0)
        self.mode('observe')
        self.commit('-m', 'unstamped')
        self.assertEqual([row['verdict'] for row in self.rows()], ['pass', 'block', 'would_block'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')

    def test_missing_helper_fails_open_for_both_integrations(self):
        self.mode('enforce')
        moved = self.root / "checkout's old path"
        with mock.patch.object(commit_gate, 'HERE', moved):
            line = self.user_hook_line()
            self.assertIn(shlex.quote(str(moved / HELPER.name)), line)
            self.script(self.hook, f'set -e\n{line}')
            self.commit('-m', 'manual hook with no helper')
            self.hook.unlink()
            self.install()
            self.commit('-m', 'installed hook with no helper')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')

    def test_claude_checks_user_hooks_even_with_a_call_line(self):
        line = self.user_hook_line()
        self.mode('enforce')
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        command = f'git commit --allow-empty -m checked -m {shlex.quote(STAMP)}'
        self.script(self.hook, f'exit 0\n  {line}')
        self.pre(command)
        self.git('-c', command, command=claude)
        row, = self.rows()
        self.assertNotIn('via', row)
        self.log.unlink()
        self.script(self.hook, f'  {line}')
        self.pre(command, tool='active')
        self.git('-c', command, command=claude)
        pre, hook = self.rows()
        self.assertNotIn('via', pre)
        self.assertEqual((hook['via'], hook['verdict']), ('commit-msg', 'pass'))
        self.pre(command, 'PostToolUse', tool='active')
        self.assertEqual(self.rows()[-1]['landed'], 'verified')

    def test_verbose_commit_under_user_hook_reports_cleanup_disagreement(self):
        self.mode('enforce')
        line = self.user_hook_line()
        self.script(self.hook, f'/bin/sh -c {shlex.quote(line)} gate "$1"\nexit $?')
        self.commit('-v', '-m', 'subject', '-m', SCISSORS, '-m', STAMP)
        self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 'subject')
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_concurrent_fresh_installs_leave_one_tether_hook(self):
        results = self.concurrent_installs()
        self.assertIn(None, results)
        for result in results:
            if result is not None:
                self.assertIn('add this line to your hook', result)
        self.assertEqual(list(self.hooks.glob('commit-msg*')), [self.hook])
        self.assertIn(commit_gate.HOOK_MARK, self.hook.read_text().splitlines())
        self.commit('-m', 'checked', '-m', STAMP)
        self.assertEqual([row['verdict'] for row in self.rows()], ['pass'])

    def test_install_leaves_an_identical_wrapper_on_the_same_inode(self):
        self.install()
        before = self.hook.stat()
        self.install()
        after = self.hook.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_update_restores_foreign_hook_swapped_in_after_ownership_check(self):
        self.install()
        foreign = b'#!/bin/sh\nexit 42\n'
        rename = os.rename

        def swap(source, destination):
            self.hook.unlink()
            self.hook.write_bytes(foreign)
            return rename(source, destination)

        with mock.patch.object(commit_gate.os, 'rename', side_effect=swap):
            with self.assertRaisesRegex(commit_gate.GateError, 'cannot replace'):
                self.install(deadline=9)
        self.assertEqual(self.hook.read_bytes(), foreign)
        self.assertEqual(list(self.hooks.glob('commit-msg*')), [self.hook])

    def test_uninstall_restores_foreign_hook_swapped_in_after_ownership_check(self):
        self.install()
        foreign = b'#!/bin/sh\nexit 42\n'
        rename = os.rename

        def swap(source, destination):
            self.hook.unlink()
            self.hook.write_bytes(foreign)
            return rename(source, destination)

        with mock.patch.object(commit_gate.os, 'rename', side_effect=swap):
            self.assertIn('No Tether', self.install(uninstall=True))
        self.assertEqual(self.hook.read_bytes(), foreign)
        self.assertEqual(list(self.hooks.glob('commit-msg*')), [self.hook])

    def test_reinstall_restores_a_hook_that_lost_its_execute_bit(self):
        self.install()
        self.hook.chmod(0o644)
        self.assertIn('Updated', self.install())
        self.assertTrue(os.access(self.hook, os.X_OK))
        self.commit('-m', 'terminal commit')
        row, = self.rows()
        self.assertEqual((row['via'], row['verdict']), ('commit-msg', 'would_block'))

    def test_update_preserves_hook_placed_before_publish(self):
        self.install()
        foreign = b'#!/bin/sh\nexit 42\n'
        link = os.link

        def publish(source, destination, **options):
            self.hook.write_bytes(foreign)
            return link(source, destination, **options)

        with mock.patch.object(commit_gate.os, 'link', side_effect=publish):
            with self.assertRaisesRegex(commit_gate.GateError, 'cannot replace'):
                self.install(deadline=9)
        self.assertEqual(self.hook.read_bytes(), foreign)
        self.assertEqual(list(self.hooks.glob('commit-msg*')), [self.hook])

    def test_update_names_the_backup_if_a_second_foreign_hook_prevents_restore(self):
        self.install()
        foreign = b'#!/bin/sh\nexit 42\n'
        second = b'#!/bin/sh\nexit 43\n'
        backup = self.hook.with_name(f'commit-msg.tether-old.{os.getpid()}')
        rename, link = os.rename, os.link

        def swap(source, destination):
            self.hook.unlink()
            self.hook.write_bytes(foreign)
            rename(source, destination)

        def restore(source, destination, **options):
            self.hook.write_bytes(second)
            return link(source, destination, **options)

        with mock.patch.object(commit_gate.os, 'rename', side_effect=swap), \
                mock.patch.object(commit_gate.os, 'link', side_effect=restore):
            with self.assertRaises(commit_gate.GateError) as raised:
                self.install(deadline=9)
        self.assertIn(str(backup), str(raised.exception))
        self.assertEqual(backup.read_bytes(), foreign)
        self.assertEqual(self.hook.read_bytes(), second)

    def test_installer_refuses_symlink_non_file_and_worktree_hooks(self):
        self.hooks.mkdir()
        for kind in ('symlink', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.hook.symlink_to(self.root / 'missing')
                else:
                    self.hook.mkdir()
                try:
                    with self.assertRaisesRegex(commit_gate.GateError, 'add this line to your hook') as raised:
                        self.install()
                    self.assertIn(str(self.hook), str(raised.exception))
                    self.assertTrue(self.hook.is_symlink() if kind == 'symlink' else self.hook.is_dir())
                finally:
                    self.hook.unlink() if kind == 'symlink' else self.hook.rmdir()
        self.git('config', 'core.hooksPath', '.tracked-hooks')
        with self.assertRaisesRegex(commit_gate.GateError, 'work tree'):
            self.install()
        self.assertFalse((self.repo / '.tracked-hooks').exists())

    def test_wrapper_kills_a_stuck_gate_with_and_without_timeout_on_path(self):
        gate = self.root / 'stuck-gate.py'
        gate.write_text('''
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(20)
''')

        self.install(gate=gate, python=sys.executable)
        original_path = self.env['PATH']
        bin_dir = self.root / 'limited-bin'
        bin_dir.mkdir()
        for command in ('git', 'sh', 'sleep', 'kill', 'dirname'):
            if program := shutil.which(command, path=original_path):
                (bin_dir / command).symlink_to(program)
        for name, path in (('normal', original_path), ('without timeout', str(bin_dir))):
            with self.subTest(path=name):
                self.env['PATH'] = path
                start = time.monotonic()
                self.commit('-m', 'the gate must fail open')
                self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')

    def test_fast_gate_releases_captured_pipes(self):
        self.install()
        start = time.monotonic()
        self.commit('-m', 'checked', '-m', STAMP)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual([row['verdict'] for row in self.rows()], ['pass'])

    def test_watchdog_limits_python_startup_for_both_integrations(self):
        self.mode('enforce')
        line = self.user_hook_line()
        (self.root / 'sitecustomize.py').write_text('import time\ntime.sleep(30)\n')
        self.env['PYTHONPATH'] = str(self.root)
        for integration in ('manual', 'installed'):
            with self.subTest(integration=integration):
                if integration == 'manual':
                    self.script(self.hook, f'set -e\n{line}')
                else:
                    self.hook.unlink()
                    self.install()
                start = time.monotonic()
                self.commit('-m', 'the gate must fail open')
                self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')
        self.assertEqual(self.rows(), [])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'process argv inspection requires /proc')
    def test_fast_gate_reaps_watchdog_sleep(self):
        self.install(deadline=37)
        self.commit('-m', 'checked', '-m', STAMP)
        # The hook waits for the watcher to reap its sleep, so no retry is needed.
        for process in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                argv = process.read_bytes().split(b'\0')
            except OSError:
                continue
            self.assertNotEqual(argv, [b'sleep', b'37', b''], f'timer survived the hook at {process}')

    def test_cli_fails_open_after_five_seconds_without_the_wrapper(self):
        launcher = (
            'import runpy, sys, time\n'
            'main = runpy.run_path(sys.argv[1])["_main"]\n'
            'main.__globals__["commit_msg"] = lambda *args, **kwargs: time.sleep(60)\n'
            'sys.exit(main(["commit-msg", "unused"]))\n'
        )
        start = time.monotonic()
        self.git('-c', launcher, str(GATE), command=(sys.executable,))
        self.assertLess(time.monotonic() - start, 10)

    def test_claude_defers_only_when_the_hook_will_run(self):
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude')
        command = 'git commit -m x'
        self.assertEqual(self.pre(command).stdout, '')
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'x', command=claude)
        self.assertEqual(self.pre(command, 'PostToolUse').stdout, '')
        row, landed = self.rows()
        self.assertEqual((row['via'], row['source'], row['session']), ('commit-msg', 'claude', SESSION))
        self.assertEqual((landed['event'], landed['landed']), ('landed', 'missing'))
        for index, command in enumerate((
            'git commit --no-verify -m x', 'git commit -qn -m x', 'git commit -an -m x',
            'git commit -"qn" -m x', 'git -c core.hooksPath=other commit -m x',
            'git --config-env=core.hooksPath=OTHER commit -m x', 'GIT_CONFIG_COUNT=0 git commit -m x',
            'git --git-dir=.git commit -m x', 'GIT_DIR=.git git commit -m x',
            'git -C "$OTHER" commit -m x', 'cd "$OTHER" && git commit -m x',
        ), start=2):
            with self.subTest(command=command):
                before = len(self.rows())
                self.pre(command, tool=f'tool-{index}')
                row, = self.rows()[before:]
                self.assertNotIn('via', row)

    def test_claude_records_a_check_when_git_add_filter_moves_the_hook(self):
        self.mode('enforce')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        self.git('config', 'filter.mv.clean', 'mv .git/hooks/commit-msg .git/hooks/commit-msg.moved && cat')
        (self.repo / '.gitattributes').write_text('one.txt filter=mv\n')
        (self.repo / 'one.txt').write_text('one\n')
        command = 'git add one.txt && git commit -m subject'
        self.assertEqual(self.pre(command).stdout, '')
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.assertTrue(self.hook.with_name('commit-msg.moved').is_file())
        self.assertEqual(self.pre(command, 'PostToolUse').stdout, '')
        check, landed = self.rows()
        self.assertEqual({key: value for key, value in check.items() if key != 'at'}, {
            'event': 'check', 'via': 'landed', 'id': 'tool-1', 'session': SESSION, 'mode': 'enforce',
            'project': None, 'folder': str(self.repo), 'count': 1, 'reason': 'hook-silent', 'verdict': 'would_block',
            'commit': self.git('rev-parse', 'HEAD').stdout.strip(),
        })
        self.assertEqual((landed['event'], landed['id'], landed['landed'], landed['count']),
                         ('landed', 'tool-1', 'missing', 1))

    def test_claude_records_a_check_when_the_gate_cannot_import(self):
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        self.git('config', 'i18n.commitEncoding', 'ISO-8859-1')
        self.git('config', 'i18n.logOutputEncoding', 'ISO-8859-1')
        (self.repo / 'msg.txt').write_bytes(b'Legacy\n\nVerified: none\xa0\n')
        for args, status, verdict in (
            (f'-m subject -m {shlex.quote(STAMP)}', 'verified', 'pass'),
            ("--allow-empty-message -m ''", 'missing', 'would_block'),
            ('-F msg.txt', 'unknown', 'unknown'),
        ):
            with self.subTest(status=status):
                command = f'git commit --allow-empty {args}'
                before = len(self.rows())
                self.assertEqual(self.pre(command, tool=status).stdout, '')
                self.assertEqual(len(self.rows()), before)
                self.git('-c', command, command=claude)
                self.assertEqual(self.pre(command, 'PostToolUse', tool=status).stdout, '')
                check, landed = self.rows()[before:]
                self.assertEqual((check['event'], check['reason'], check['verdict']), ('check', 'hook-silent', verdict))
                self.assertEqual(check['commit'], self.git('rev-parse', 'HEAD').stdout.strip())
                self.assertEqual((landed['event'], landed['id'], landed['landed']), ('landed', status, status))

    def test_claude_matches_hook_checks_only_in_the_deferred_worktree(self):
        other = self.root / 'other'
        other.mkdir()
        self.git('init', '-q', '-b', 'main', '--template=', cwd=other)
        subdir = self.repo / 'subdir'
        subdir.mkdir()
        self.install()
        self.install(repo=other, gate=self.gate_with_import_error(), python=sys.executable)
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        calls = {
            'working': f'git -C {shlex.quote(str(subdir))} commit --allow-empty -m working',
            'silent': f'git -C {shlex.quote(str(other))} commit --allow-empty -m silent',
        }
        for tool, command in calls.items():
            self.pre(command, tool=tool)
        self.assertEqual(self.rows(), [])
        for command in calls.values():
            self.git('-c', command, command=claude)
        for tool, command in reversed(calls.items()):
            self.pre(command, 'PostToolUse', tool=tool)
        working, = [row for row in self.rows() if row.get('via') == 'commit-msg']
        self.assertEqual((working['folder'], working['session']), (str(self.repo), SESSION))
        silent, = [row for row in self.rows() if row.get('reason') == 'hook-silent']
        self.assertEqual((silent['id'], silent['folder'], silent['count'], silent['verdict']),
                         ('silent', str(other), 1, 'would_block'))
        self.assertEqual(silent['commit'], self.git('rev-parse', 'HEAD', cwd=other).stdout.strip())

    def test_claude_does_not_match_a_different_tree_with_the_same_parent(self):
        self.commit('-m', 'base')
        self.git('branch', 'silent')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        working = f'git commit --allow-empty -m working -m {shlex.quote(STAMP)}'
        silent = 'git commit --allow-empty -m silent'
        self.pre(working, tool='working')
        self.pre(silent, tool='silent')
        self.assertEqual(self.rows(), [])
        self.git('-c', working, command=claude)
        self.pre(working, 'PostToolUse', tool='working')
        self.git('checkout', '-q', 'silent')
        self.write_file('silent tree')
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.git('-c', silent, command=claude)
        self.pre(silent, 'PostToolUse', tool='silent')
        check, first, fallback, second = self.rows()
        self.assertEqual((check['via'], check['verdict']), ('commit-msg', 'pass'))
        self.assertEqual((first['id'], first['landed'], first['count']), ('working', 'verified', 1))
        self.assertEqual((fallback['id'], fallback['reason'], fallback['count'], fallback['verdict']),
                         ('silent', 'hook-silent', 1, 'would_block'))
        self.assertEqual(fallback['commit'], self.git('rev-parse', 'HEAD').stdout.strip())
        self.assertEqual((second['id'], second['landed'], second['count']), ('silent', 'missing', 1))

    def test_claude_block_row_does_not_cover_an_overlapping_silent_commit(self):
        self.mode('enforce')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        blocked = 'git commit --allow-empty -m blocked'
        silent = 'git commit --allow-empty -m silent'
        self.pre(blocked, tool='blocked')
        self.pre(silent, tool='silent')
        self.assertEqual(self.rows(), [])
        self.assertNotEqual(self.git('-c', blocked, command=claude, check=False).returncode, 0)
        self.pre(blocked, 'PostToolUseFailure', tool='blocked')
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.git('-c', silent, command=claude)
        self.pre(silent, 'PostToolUse', tool='silent')
        block, fallback = [row for row in self.rows() if row['event'] == 'check']
        self.assertEqual((block['via'], block['verdict']), ('commit-msg', 'block'))
        self.assertEqual((fallback['id'], fallback['reason'], fallback['commit']),
                         ('silent', 'hook-silent', self.git('rev-parse', 'HEAD').stdout.strip()))

    def test_claude_overlapping_calls_can_both_match_the_same_hook_rows(self):
        self.commit('-m', 'base')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        calls = ('git commit --allow-empty -m first', 'git commit --allow-empty -m second')
        for tool in calls:
            self.pre(tool, tool=tool)
        self.assertEqual(self.rows(), [])
        for command in calls:
            self.git('-c', command, command=claude)
        for tool in calls:
            self.pre(tool, 'PostToolUse', tool=tool)
        self.assertEqual([row.get('via') for row in self.rows() if row['event'] == 'check'], ['commit-msg'] * 2)
        self.assertEqual([row['count'] for row in self.rows() if row['event'] == 'landed'], [2, 2])

    def test_claude_overlapping_landings_log_a_silent_commit_once(self):
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        command = 'git commit --allow-empty -m silent'
        (self.repo / 'subdir').mkdir()
        calls = {'first': command, 'second': 'git -C subdir commit --allow-empty -m silent'}
        for tool, call in calls.items():
            self.pre(call, tool=tool)
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(self.pre, call, 'PostToolUse', tool=tool) for tool, call in calls.items()]:
                future.result()
        row, = [row for row in self.rows() if row['event'] == 'check']
        self.assertEqual((row['reason'], row['folder'], row['commit'], row['count']),
                         ('hook-silent', str(self.repo), self.git('rev-parse', 'HEAD').stdout.strip(), 1))
        self.assertEqual({row['id'] for row in self.rows() if row['event'] == 'landed'}, {'first', 'second'})

    def test_claude_matches_a_hook_check_from_another_session(self):
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = 'another-session'
        command = 'git commit --allow-empty -m subject'
        self.pre(command, session=SESSION)
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.pre(command, 'PostToolUse', session=SESSION)
        check, landed = self.rows()
        self.assertEqual((check['via'], check['session']), ('commit-msg', 'another-session'))
        self.assertEqual((landed['event'], landed['session'], landed['count']), ('landed', SESSION, 1))

    def test_claude_matches_amend_with_a_partial_index_and_allow_empty(self):
        self.write_file('base')
        self.commit('-m', 'base')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        (self.repo / 'file').write_text('amend only this file\n')
        (self.repo / 'other').write_text('keep this file staged\n')
        self.git('add', 'other')
        cases = ('--amend --only -m amended -- file', '--allow-empty -m empty', '--amend --allow-empty -m new',
                 '--amend --no-edit --allow-empty')
        for day, args in enumerate(cases, start=1):
            with self.subTest(args=args):
                self.env['GIT_COMMITTER_DATE'] = f'2001-01-0{day}T00:00:00+00:00'
                if args.startswith('--allow-empty'):
                    self.git('restore', '--staged', 'other')
                command = f'git commit {args}'
                head = self.git('rev-parse', 'HEAD').stdout.strip()
                before = len(self.rows())
                self.pre(command)
                self.assertEqual(len(self.rows()), before)
                self.git('-c', command, command=claude)
                self.pre(command, 'PostToolUse')
                check, landed = self.rows()[before:]
                self.assertEqual((check['via'], check['head'], check['tree']),
                                 ('commit-msg', head, self.git('rev-parse', 'HEAD^{tree}').stdout.strip()))
                self.assertEqual((landed['event'], landed['count']), ('landed', 1))
                self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD').stdout.strip(), 'file')

    def assert_hook_silent(self, command, verdict='would_block'):
        self.pre(command, 'PostToolUse')
        fallback, = [row for row in self.rows() if row.get('reason') == 'hook-silent']
        self.assertEqual((fallback['commit'], fallback['verdict']),
                         (self.git('rev-parse', 'HEAD').stdout.strip(), verdict))

    def test_claude_logs_a_silent_author_date_only_amend(self):
        self.install()
        self.write_file('tracked content')
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+05:30',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        self.git('commit', '--amend', '--no-edit', '--no-verify', '--date=2001-01-02T00:00:00+05:30')
        self.assert_hook_silent(command)

    def test_claude_logs_a_silent_no_edit_amend_after_the_hook_time(self):
        self.install()
        self.write_file('tracked content')
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        check, = self.rows()
        self.env['GIT_COMMITTER_DATE'] = f'@{int(check["at"]) + 2} +0000'
        self.git('commit', '--amend', '--no-edit', '--no-verify')
        self.assert_hook_silent(command)

    def test_claude_logs_a_silent_commit_after_a_failed_signed_commit(self):
        self.commit('-m', 'base')
        self.install()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        self.git('config', 'gpg.program', 'false')
        command = 'git commit -S --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        head = self.git('rev-parse', 'HEAD').stdout
        failed = self.git('commit', '-S', '--allow-empty', '-m', 'same', check=False)
        self.assertIn('failed to sign', failed.stderr)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, head)
        check, = self.rows()
        self.assertEqual(check['via'], 'commit-msg')
        self.env['GIT_AUTHOR_DATE'] = '2001-01-02T00:00:00+00:00'
        self.commit('--no-verify', '-m', 'same')
        self.assert_hook_silent(command)

    def test_claude_logs_a_silent_no_edit_retry_after_a_failed_amend(self):
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+00:00'
        self.write_file('tracked content')
        self.commit('-m', 'same')
        self.install()
        self.git('config', 'gpg.program', 'false')
        command = 'git commit -S --amend --no-edit'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        head = self.git('rev-parse', 'HEAD').stdout
        failed = self.git('commit', '-S', '--amend', '--no-edit', check=False)
        self.assertIn('failed to sign', failed.stderr)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, head)
        check, = self.rows()
        self.assertEqual(check['via'], 'commit-msg')
        self.env['GIT_COMMITTER_DATE'] = f'@{int(check["at"]) + 2} +0000'
        self.git('commit', '--amend', '--no-edit', '--no-verify')
        self.assert_hook_silent(command)

    def test_claude_logs_a_silent_retry_outside_the_failed_git_process_time(self):
        self.commit('-m', 'base')
        self.install()
        self.env['GIT_AUTHOR_DATE'] = '2001-01-01T00:00:00+05:30'
        self.git('config', 'gpg.program', 'false')
        command = 'git commit -S --allow-empty -m same'
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.git('reset', '--keep', base)
                tool = f'retry-{direction}'
                before = len(self.rows())
                self.pre(command, tool=tool)
                self.assertEqual(len(self.rows()), before)
                failed = self.git('commit', '-S', '--allow-empty', '-m', 'same', check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('failed to sign', failed.stderr)
                self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), base)
                check, = self.rows()[before:]
                self.assertEqual(check['via'], 'commit-msg')
                date = f'@{int(check["at"]) + direction * 86400} +0000'
                with mock.patch.dict(self.env, GIT_COMMITTER_DATE=date):
                    self.commit('--no-verify', '-m', 'same')
                self.assertEqual(self.git('log', '-1', '--date=raw', '--format=%ad').stdout.strip(),
                                 check['author_date'])
                self.pre(command, 'PostToolUse', tool=tool)
                fallback, landed = self.rows()[before + 1:]
                self.assertEqual((fallback['reason'], fallback['verdict'], fallback['commit']),
                                 ('hook-silent', 'would_block', self.git('rev-parse', 'HEAD').stdout.strip()))
                self.assertEqual(landed['event'], 'landed')

    def test_claude_covers_a_commit_with_an_explicit_committer_date(self):
        self.install()
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+05:30'
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual(check['via'], 'commit-msg')
        self.assertEqual(check['committer_date'], self.git('log', '-1', '--date=raw', '--format=%cd').stdout.strip())
        self.assertNotIn('committer_override', check)
        self.assertEqual(landed['event'], 'landed')

    def test_claude_covers_a_commit_with_an_empty_committer_date(self):
        self.install()
        self.env['GIT_COMMITTER_DATE'] = ''
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual(check['via'], 'commit-msg')
        self.assertNotIn('committer_date', check)
        self.assertNotIn('committer_override', check)
        self.assertEqual(landed['event'], 'landed')

    def test_claude_uses_the_hook_time_when_the_committer_date_is_unknown(self):
        self.commit('-m', 'base')
        self.install()
        command = 'git commit --allow-empty -m same'
        cases = (('missing-started', False), ('unresolved-override', False), ('unresolved-override', True))
        for case, future in cases:
            with self.subTest(case=case, future=future):
                tool = f'{case}-{future}'
                before = len(self.rows())
                self.pre(command, tool=tool)
                self.assertEqual(len(self.rows()), before)
                committed = int(time.time()) + 86400 if future else 946684800
                with mock.patch.dict(self.env, GIT_COMMITTER_DATE=f'@{committed} +0000'):
                    self.commit('-m', 'same')
                *previous, check = self.rows()
                self.assertEqual(check['via'], 'commit-msg')
                check.pop('committer_date')
                self.assertEqual(committed > check['at'], future)
                if case == 'missing-started':
                    check.pop('started')
                else:
                    check['committer_override'] = True
                self.log.write_text(''.join(json.dumps(row) + '\n' for row in [*previous, check]))
                self.pre(command, 'PostToolUse', tool=tool)
                if future:
                    fallback, landed = self.rows()[before + 1:]
                    self.assertEqual((fallback['reason'], fallback['commit']),
                                     ('hook-silent', self.git('rev-parse', 'HEAD').stdout.strip()))
                else:
                    landed, = self.rows()[before + 1:]
                self.assertEqual(landed['event'], 'landed')

    def test_claude_does_not_claim_an_old_row_without_an_author_date(self):
        self.install()
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        check, = self.rows()
        check.pop('author_date', None)
        self.log.write_text(json.dumps(check) + '\n')
        self.assert_hook_silent(command)

    def prepare_merge(self):
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'side')
        self.write_file('side')
        self.commit('-m', 'side')
        self.git('checkout', '-q', 'main')
        self.install()

    def test_claude_covers_a_direct_merge_and_excludes_it_from_unstamped_stats(self):
        self.prepare_merge()
        command = 'git commit --no-edit'
        # A deferred call can see a merge made in the same worktree.
        self.pre(command)
        self.assertEqual(self.rows(), [])
        env = {name: value for name, value in self.env.items() if not name.startswith('GIT_AUTHOR_')}
        self.git('config', 'user.name', 'Tester')
        self.git('config', 'user.email', 'test@example.com')
        with mock.patch.object(self, 'env', env):
            self.git('merge', '--no-ff', '-m', 'merge side', 'side')
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual((check['via'], check['verdict'], check['reason']), ('commit-msg', 'skip', 'merge'))
        self.assertEqual(check['merge_heads'], [self.git('rev-parse', 'side').stdout.strip()])
        self.assertNotIn('author_date', check)
        self.assertEqual((landed['event'], landed['count']), ('landed', 1))
        with mock.patch.object(paths, 'COMMIT_GATE_LOG', self.log), mock.patch.object(paths, 'CACHE', self.cache):
            stats = commit_gate.stats()
        self.assertEqual((stats['commits'], stats['would_block'], stats['unstamped']), (0, 0, 0))

    def test_claude_covers_a_commit_on_a_pending_merge(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-commit', 'side')
        command = 'git commit --no-edit'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('commit', '--no-edit')
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual((check['via'], check['verdict'], check['reason']), ('commit-msg', 'skip', 'merge'))
        self.assertEqual(check['author_date'], self.git('log', '-1', '--date=raw', '--format=%ad').stdout.strip())
        self.assertEqual((landed['event'], landed['count']), ('landed', 1))

    def test_claude_covers_conflicted_cherry_pick_and_revert_continuations(self):
        self.write_file('base')
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'side')
        self.write_file('side')
        self.commit('-m', 'side')
        self.git('checkout', '-q', 'main')
        self.write_file('main')
        self.commit('-m', 'main')
        self.git('branch', 'revert-start')
        self.install()
        self.env['GIT_EDITOR'] = 'true'
        for action, target in (('cherry-pick', 'side'), ('revert', 'HEAD^')):
            with self.subTest(action=action):
                if action == 'revert':
                    self.git('checkout', '-q', 'revert-start')
                failed = self.git(action, target, check=False)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('CONFLICT', failed.stdout)
                self.write_file('resolution')
                command = 'git commit --no-edit'
                before = len(self.rows())
                self.pre(command, tool=action)
                self.assertEqual(len(self.rows()), before)
                self.git(action, '--continue')
                self.pre(command, 'PostToolUse', tool=action)
                check, landed = self.rows()[before:]
                self.assertEqual((check['via'], check['verdict'], check['reason']), ('commit-msg', 'skip', 'replay'))
                self.assertEqual((landed['event'], landed['count']), ('landed', 1))

    def test_claude_covers_an_amend_at_a_rebase_edit_stop(self):
        self.commit('-m', 'base')
        self.write_file('edit')
        self.commit('-m', 'edit')
        self.install()
        self.env['GIT_SEQUENCE_EDITOR'] = self.script(
            self.root / 'sequence-editor', 'sed "1s/^pick/edit/" "$1" > "$1.new" && mv "$1.new" "$1"')
        self.git('rebase', '-i', 'HEAD^')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        command = 'git commit --amend -m amended'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('commit', '--amend', '-m', 'amended')
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual((check['via'], check['verdict'], check['reason'], check['head']),
                         ('commit-msg', 'skip', 'replay', head))
        self.assertNotEqual(check['head'], self.git('rev-parse', 'HEAD^').stdout.strip())
        self.assertEqual((landed['event'], landed['count']), ('landed', 1))

    def test_claude_logs_a_silent_merge_as_skip(self):
        self.prepare_merge()
        command = 'git commit --no-edit'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('merge', '--no-ff', '--no-verify', '-m', 'merge side', 'side')
        self.assertEqual(self.rows(), [])
        self.assert_hook_silent(command, 'skip')

    def test_claude_does_not_claim_a_merge_row_for_a_different_second_parent(self):
        self.prepare_merge()
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        tree = self.git('rev-parse', 'side^{tree}').stdout.strip()
        other = self.git('commit-tree', tree, '-p', base, '-m', 'other tip').stdout.strip()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        command = 'git commit --no-edit'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('merge', '--no-ff', '-m', 'same merge', 'side')
        check, = self.rows()
        self.assertEqual((check['tree'], check['head']), (tree, base))
        self.git('reset', '--keep', base)
        self.git('merge', '--no-ff', '--no-verify', '-m', 'same merge', other)
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}', 'HEAD^', 'HEAD^2').stdout.split(), [tree, base, other])
        self.assertEqual(check['committer_date'], self.git('log', '-1', '--date=raw', '--format=%cd').stdout.strip())
        self.assert_hook_silent(command, 'skip')

    def test_claude_does_not_claim_a_fresh_commit_row_for_a_silent_merge(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-verify', '-m', 'original merge', 'side')
        original = self.git('rev-parse', 'HEAD').stdout.strip()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        check, = self.rows()
        self.assertEqual(check['head'], original)
        self.git('reset', '--keep', f'{original}^')
        self.git('merge', '--no-ff', '--no-verify', '-m', 'same', 'side')
        landed = self.git('log', '-1', '--date=raw', '--format=%T%n%ad%n%cd').stdout.splitlines()
        self.assertEqual(landed, [check['tree'], check['author_date'], check['committer_date']])
        self.assert_hook_silent(command, 'skip')

    def test_claude_logs_a_silent_remerge_as_skip(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-verify', '-m', 'merge side', 'side')
        original = self.git('rev-parse', 'HEAD').stdout.strip()
        parents = self.git('log', '-1', '--format=%P').stdout
        command = 'git commit --no-edit'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('reset', '--hard', 'HEAD~1')
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+00:00'
        self.git('merge', '--no-ff', '--no-verify', '-m', 'merge side', 'side')
        self.assertNotEqual(self.git('rev-parse', 'HEAD').stdout.strip(), original)
        self.assertEqual(self.git('log', '-1', '--format=%P').stdout, parents)
        self.assert_hook_silent(command, 'skip')

    def test_claude_covers_stamped_merge_amendments(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '-m', f'merge side\n\n{STAMP}', 'side')
        parents = self.git('log', '-1', '--format=%P').stdout
        for day, args in enumerate((('--no-edit',), ('-m', 'amended merge', '-m', STAMP)), start=1):
            with self.subTest(args=args):
                self.env['GIT_COMMITTER_DATE'] = f'2001-01-0{day}T00:00:00+00:00'
                command = f'git commit --amend {shlex.join(args)}'
                old = self.git('rev-parse', 'HEAD').stdout.strip()
                before = len(self.rows())
                self.pre(command)
                self.assertEqual(len(self.rows()), before)
                self.git('commit', '--amend', *args)
                self.assertNotEqual(self.git('rev-parse', 'HEAD').stdout.strip(), old)
                self.assertEqual(self.git('log', '-1', '--format=%P').stdout, parents)
                self.pre(command, 'PostToolUse')
                check, landed = self.rows()[before:]
                self.assertEqual((check['via'], check['verdict'], check['head']), ('commit-msg', 'pass', old))
                self.assertEqual(landed['event'], 'landed')

    def test_claude_covers_an_amend_when_post_commit_switches_branch(self):
        self.commit('-m', 'base', '-m', STAMP)
        self.install()
        post = self.repo / '.git' / 'hooks' / 'post-commit'
        post.write_text('#!/bin/sh\ngit checkout -q -B moved\n')
        post.chmod(0o755)
        old = self.git('rev-parse', 'HEAD').stdout.strip()
        command = f'git commit --amend --allow-empty -m amended -m {shlex.quote(STAMP)}'
        self.pre(command)
        self.git('commit', '--amend', '--allow-empty', '-m', 'amended', '-m', STAMP)
        self.assertNotEqual(self.git('rev-parse', 'HEAD').stdout.strip(), old)
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip(), 'moved')
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual((check['via'], check['verdict'], landed['event']), ('commit-msg', 'pass', 'landed'))

    def test_claude_keeps_the_amend_verdict_after_checkout_and_return(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '-m', 'merge side', 'side')
        command = 'git commit --amend --no-edit'
        self.pre(command)
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+00:00'
        self.git('commit', '--amend', '--no-edit', '--no-verify')
        amended = self.git('rev-parse', 'HEAD').stdout.strip()
        branch = self.git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
        self.git('checkout', '-q', 'HEAD^')
        self.git('checkout', '-q', branch)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), amended)
        self.assert_hook_silent(command, 'would_block')

    def test_claude_does_not_claim_a_fresh_commit_row_for_a_revisited_remerge(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-verify', '-m', 'original merge', 'side')
        original = self.git('rev-parse', 'HEAD').stdout.strip()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.commit('-m', 'same')
        check, = self.rows()
        fresh = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('reset', '-q', '--keep', f'{original}^')
        self.git('merge', '--no-ff', '--no-verify', '-m', 'same', 'side')
        remerge = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('reset', '-q', '--keep', fresh)
        self.git('reset', '-q', '--keep', original)  # The last move to the re-merge now starts at the row's head.
        self.git('reset', '-q', '--keep', remerge)
        self.assertEqual(check['head'], original)
        self.assert_hook_silent(command, 'skip')

    def test_claude_does_not_claim_a_fresh_commit_row_for_a_remerge_of_an_old_amend(self):
        self.prepare_merge()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        self.git('merge', '--no-ff', '--no-verify', '-m', 'original merge', 'side')
        original = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('commit', '--amend', '--no-verify', '-m', 'same')
        amended = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('reset', '--keep', original)

        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.commit('-m', 'same')
        check, = self.rows()
        self.assertEqual((check['via'], check['head']), ('commit-msg', original))
        self.git('reset', '--keep', f'{original}^')
        self.git('merge', '--no-ff', '--no-verify', '-m', 'same', 'side')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), amended)
        self.assert_hook_silent(command, 'skip')

    def test_claude_covers_a_checked_amend_that_recreates_an_old_merge(self):
        self.prepare_merge()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        self.git('merge', '--no-ff', '--no-verify', '-m', f'same\n\n{STAMP}', 'side')
        merged = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('reset', '--keep', 'HEAD^')
        self.git('merge', '--no-ff', '--no-verify', '-m', 'original merge', 'side')
        original = self.git('rev-parse', 'HEAD').stdout.strip()

        command = f'git commit --amend -m same -m {shlex.quote(STAMP)}'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('commit', '--amend', '-m', 'same', '-m', STAMP)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), merged)
        self.pre(command, 'PostToolUse')
        check, *results = self.rows()
        self.assertEqual((check['via'], check['verdict'], check['head']), ('commit-msg', 'pass', original))
        self.assertEqual([(row['event'], row.get('reason')) for row in results], [('landed', None)])

    def test_claude_skips_a_silent_merge_amendment_with_an_old_pending_record(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-verify', '-m', 'merge side', 'side')
        command = 'git commit --amend -m amended'
        self.pre(command)
        session = self.cache / 'commit-gate' / f'{SESSION}.json'
        state = json.loads(session.read_text())
        del state['pending']['tool-1']['reflog']
        session.write_text(json.dumps(state))
        self.git('commit', '--amend', '--no-verify', '-m', 'amended')
        self.assert_hook_silent(command, 'skip')

    def test_claude_skips_a_silent_merge_amendment_after_a_reflog_rewrite(self):
        self.assert_reflog_rewrite(empty=False)

    def test_claude_skips_a_silent_merge_amendment_after_an_empty_reflog_rewrite(self):
        self.assert_reflog_rewrite(empty=True)

    def assert_reflog_rewrite(self, *, empty):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-verify', '-m', 'merge side', 'side')
        reflog = self.repo / '.git' / 'logs' / 'HEAD'
        if empty:
            self.git('reflog', 'expire', '--expire=all', 'HEAD')
            self.assertEqual(reflog.stat().st_size, 0)
        command = 'git commit --amend -m amended'
        self.pre(command)
        original = reflog.stat()
        rewritten = reflog.with_name('HEAD.rewritten')
        rewritten.write_bytes(reflog.read_bytes())
        rewritten.replace(reflog)
        self.assertEqual(reflog.stat().st_size, original.st_size)
        self.assertNotEqual(reflog.stat().st_ino, original.st_ino)
        self.git('commit', '--amend', '--no-verify', '-m', 'amended')
        self.assert_hook_silent(command, 'skip')

    def test_claude_does_not_claim_a_fresh_commit_row_for_a_remerge_without_a_reflog(self):
        self.prepare_merge()
        self.git('config', 'core.logAllRefUpdates', 'false')
        (self.repo / '.git' / 'logs' / 'HEAD').unlink()
        self.git('merge', '--no-ff', '--no-verify', '-m', 'original merge', 'side')
        original = self.git('rev-parse', 'HEAD').stdout.strip()
        self.env.update(GIT_AUTHOR_DATE='2001-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+00:00')
        command = 'git commit --allow-empty -m same'
        self.pre(command)
        self.commit('-m', 'same')
        self.git('reset', '-q', '--keep', f'{original}^')
        self.git('merge', '--no-ff', '--no-verify', '-m', 'same', 'side')
        self.assertFalse((self.repo / '.git' / 'logs' / 'HEAD').exists())
        self.assert_hook_silent(command, 'skip')

    def test_claude_applies_the_normal_verdict_to_a_silent_merge_amendment(self):
        self.assert_silent_merge_amendment('would_block')

    def test_claude_skips_a_silent_merge_amendment_without_a_reflog(self):
        self.assert_silent_merge_amendment('skip', reflog=False)

    def assert_silent_merge_amendment(self, verdict, *, reflog=True):
        self.prepare_merge()
        self.git('merge', '--no-ff', '-m', 'merge side', 'side')
        if not reflog:
            self.git('config', 'core.logAllRefUpdates', 'false')
            (self.repo / '.git' / 'logs' / 'HEAD').unlink()
        old = self.git('rev-parse', 'HEAD').stdout.strip()
        command = 'git commit --amend --no-edit'
        self.pre(command)
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+00:00'
        self.git('commit', '--amend', '--no-edit', '--no-verify')
        self.assertNotEqual(self.git('rev-parse', 'HEAD').stdout.strip(), old)
        self.assert_hook_silent(command, verdict)

    def test_claude_does_not_claim_a_merge_skip_row_for_a_different_message(self):
        self.prepare_merge()
        self.git('merge', '--no-ff', '--no-commit', 'side')
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+00:00'
        command = 'git commit --no-edit'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('commit', '--no-edit')
        check, = self.rows()
        self.assertTrue(check.get('messages'))
        self.git('commit', '--amend', '--no-verify', '-m', 'different message')
        self.assertEqual(check['tree'], self.git('rev-parse', 'HEAD^{tree}').stdout.strip())
        self.assertEqual(check['head'], self.git('rev-parse', 'HEAD^').stdout.strip())
        self.assert_hook_silent(command)

    def test_claude_logs_a_silent_message_only_amend(self):
        self.install()
        self.write_file('tracked content')
        claude = self.agent('claude', '/bin/sh')
        first = 'git commit --allow-empty -m old'
        self.pre(first, tool='first')
        self.git('-c', first, command=claude)
        check, = self.rows()
        self.pre(first, 'PostToolUse', tool='first')
        amend = 'git commit --amend -m new'
        # Keep the first row in the second call's timestamp window, without relying on wall-clock timing.
        self.pre(amend, tool='amend', now=check['at'])
        self.assertEqual([row['event'] for row in self.rows()], ['check', 'landed'])
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.git('-c', amend, command=claude)
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}').stdout.strip(), check['tree'])
        self.pre(amend, 'PostToolUse', tool='amend')
        fallback, = [row for row in self.rows() if row.get('reason') == 'hook-silent']
        self.assertEqual((fallback['id'], fallback['commit']), ('amend', self.git('rev-parse', 'HEAD').stdout.strip()))

    def test_claude_does_not_use_a_later_empty_commit_row_for_its_parent(self):
        self.commit('-m', 'base')
        self.install()
        working = self.hook.with_name('commit-msg.working')
        working.write_bytes(self.hook.read_bytes())
        working.chmod(0o755)
        command = 'git commit --allow-empty -m same'
        self.script(self.hooks / 'post-commit',
                    'rm .git/hooks/post-commit\n'
                    'mv .git/hooks/commit-msg.working .git/hooks/commit-msg\n'
                    f'{command}')
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.pre(command, 'PostToolUse')
        check, fallback, landed = self.rows()
        self.assertEqual((check['via'], check['head']),
                         ('commit-msg', self.git('rev-parse', 'HEAD^').stdout.strip()))
        self.assertEqual((fallback['reason'], fallback['commit']), ('hook-silent', check['head']))
        self.assertEqual((landed['event'], landed['count']), ('landed', 2))

    def test_claude_logs_identical_silent_root_commits_in_separate_repos(self):
        other = self.root / 'other'
        other.mkdir()
        self.git('init', '-q', '-b', 'main', '--template=', cwd=other)
        gate = self.gate_with_import_error()
        self.install(gate=gate, python=sys.executable)
        self.install(repo=other, gate=gate, python=sys.executable)
        date = '2001-01-01T00:00:00+00:00'
        self.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        claude = self.agent('claude', '/bin/sh')
        for repo in (self.repo, other):
            command = f'git -C {shlex.quote(str(repo))} commit --allow-empty -m identical'
            before = len(self.rows())
            self.pre(command)
            self.assertEqual(len(self.rows()), before)
            self.git('-c', command, command=claude)
            self.pre(command, 'PostToolUse')
        oid = self.git('rev-parse', 'HEAD').stdout.strip()
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=other).stdout.strip(), oid)
        self.assertEqual([(row['folder'], row['commit']) for row in self.rows() if row.get('reason') == 'hook-silent'],
                         [(str(self.repo), oid), (str(other), oid)])

    def test_claude_does_not_reuse_a_claimed_row_for_a_different_commit(self):
        self.install()
        self.write_file('tracked content')
        claude = self.agent('claude', '/bin/sh')
        first = 'git commit --allow-empty -m same'
        amend = 'git commit --amend --no-edit'
        self.pre(first, tool='first')
        self.pre(amend, tool='amend', session='another-session')
        self.assertEqual(self.rows(), [])
        self.env['GIT_COMMITTER_DATE'] = '2001-01-01T00:00:00+00:00'
        self.git('-c', first, command=claude)
        old = self.git('rev-parse', 'HEAD').stdout.strip()
        self.pre(first, 'PostToolUse', tool='first')
        self.install(gate=self.gate_with_import_error(), python=sys.executable)
        self.env['GIT_COMMITTER_DATE'] = '2001-01-02T00:00:00+00:00'
        self.git('-c', amend, command=claude)
        new = self.git('rev-parse', 'HEAD').stdout.strip()
        self.assertNotEqual(old, new)
        self.pre(amend, 'PostToolUse', tool='amend', session='another-session')
        fallback, = [row for row in self.rows() if row.get('reason') == 'hook-silent']
        self.assertEqual((fallback['id'], fallback['commit']), ('amend', new))

    def test_claude_uses_landed_message_when_commit_msg_hook_rewrites_it_after_gate(self):
        line = self.user_hook_line()
        self.script(self.hook, f'{line}\nprintf "rewritten\\n" > "$1"')
        # A deferred call sees the installed wrapper and Git later runs the composed hook.
        wrapper = self.hook.with_name('commit-msg.composed')
        self.hook.rename(wrapper)
        self.install()
        command = f'git commit --allow-empty -m original -m {shlex.quote(STAMP)}'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        wrapper.replace(self.hook)
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.pre(command, 'PostToolUse')
        check, fallback, landed = self.rows()
        self.assertEqual((check['via'], check['verdict']), ('commit-msg', 'pass'))
        self.assertEqual((fallback['reason'], fallback['verdict']), ('hook-silent', 'would_block'))
        self.assertEqual(landed['landed'], 'missing')

    def test_claude_uses_landed_message_when_gate_key_is_short(self):
        self.commit('-m', 'base')
        oid = self.git('rev-parse', 'HEAD').stdout.strip()
        self.install()
        key = self.cache / 'commit-gate' / 'commit-gate.key'
        key.parent.mkdir(parents=True)
        key.write_bytes(b'short')
        ledger = key.with_name(f'claims.{hashlib.sha256(os.fsencode(self.repo)).hexdigest()}.json')
        now = time.time()
        fresh = f'{now}:{self.repo}'
        old = f'{now - 90000}:{self.repo}'
        ledger.write_text(json.dumps({old: oid, fresh: oid}))
        command = 'git commit --allow-empty -m subject'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.pre(command, 'PostToolUse')
        check, fallback, landed = self.rows()
        self.assertEqual(check['via'], 'commit-msg')
        self.assertNotIn('messages', check)
        self.assertEqual(key.read_bytes(), b'short')
        self.assertEqual(json.loads(ledger.read_text()), {fresh: oid})
        self.assertEqual((fallback['reason'], landed['landed']), ('hook-silent', 'missing'))

    def test_broken_key_and_claim_files_fail_open_in_enforce(self):
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        folder = self.cache / 'commit-gate'
        folder.mkdir(parents=True, exist_ok=True)
        ledger = folder / f'claims.{hashlib.sha256(os.fsencode(self.repo)).hexdigest()}.json'
        for case in ('directory', 'unreadable'):
            with self.subTest(case=case):
                key = folder / 'commit-gate.key'
                if key.is_dir():
                    key.rmdir()
                elif key.exists():
                    key.chmod(0o600)
                    key.unlink()
                if case == 'directory':
                    key.mkdir()
                else:
                    key.write_bytes(b'k' * 32)
                    key.chmod(0)
                ledger.write_text('{not json')
                command = f'git commit --allow-empty -m subject -m {shlex.quote(STAMP)}'
                self.pre(command)
                result = self.git('-c', command, command=self.agent('claude', '/bin/sh'), check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.pre(command, 'PostToolUse')
                verdicts = [row.get('verdict') for row in self.rows() if row.get('event') == 'check']
                self.assertNotIn('block', verdicts)
        key.chmod(0o600)

    def test_claude_matches_normalized_cleanup_readings(self):
        self.install()
        self.git('config', 'core.commentChar', ';')
        cut = SCISSORS.replace('#', ';', 1)
        trailer = 'trailer\u00a0'  # Git stripspace keeps the non-breaking space.
        (self.repo / 'draft').write_text(f'\n\nsubject \t\n\n\n# hash \t\n; comment \t\n{cut}\n\n{trailer} \t\n\n')
        self.script(self.hooks / 'prepare-commit-msg', 'cat draft > "$1"')
        command = 'git commit --allow-empty --cleanup=strip -m ignored'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.pre(command, 'PostToolUse')
        check, landed = self.rows()
        self.assertEqual((check['via'], landed['event']), ('commit-msg', 'landed'))
        self.assertEqual(self.git('log', '-1', '--format=%B').stdout.rstrip('\n'), f'subject\n\n# hash\n\n{trailer}')
        key = (self.cache / 'commit-gate' / 'commit-gate.key').read_bytes()
        messages = (f'subject\n\n# hash\n; comment\n{cut}\n\n{trailer}', f'subject\n\n; comment\n{cut}\n\n{trailer}',
                    'subject\n\n# hash\n; comment', f'subject\n\n# hash\n\n{trailer}', 'subject\n\n# hash')
        self.assertEqual(check['messages'], sorted(hmac.digest(key, message.encode(), 'sha256').hex()[:32]
                                                   for message in messages))

    def test_claude_counts_commits_without_hook_checks_in_one_deferred_call(self):
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        # Git runs post-commit after the first commit lands. The nested commits skip commit-msg.
        self.script(self.hooks / 'post-commit',
                    'rm .git/hooks/post-commit\n'
                    'git commit --allow-empty --no-verify -m unchecked -m "Verified: none (x)"\n'
                    'git commit --allow-empty --no-verify -m unstamped')
        command = f'git commit --allow-empty -m working -m {shlex.quote(STAMP)}'
        self.pre(command)
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        newest, previous, _ = self.git('rev-list', 'HEAD').stdout.splitlines()
        self.pre(command, 'PostToolUse')
        check, *fallbacks, landed = self.rows()
        self.assertEqual((check['via'], check['verdict']), ('commit-msg', 'pass'))
        self.assertCountEqual(
            [(row['commit'], row['via'], row['reason'], row['count'], row['verdict']) for row in fallbacks],
            [(newest, 'landed', 'hook-silent', 1, 'would_block'), (previous, 'landed', 'hook-silent', 1, 'pass')])
        self.assertEqual((landed['event'], landed['landed'], landed['count']), ('landed', 'missing', 3))
        with mock.patch.object(paths, 'COMMIT_GATE_LOG', self.log), mock.patch.object(paths, 'CACHE', self.cache):
            stats = commit_gate.stats()
        self.assertEqual((stats['stamped'], stats['unstamped'], stats['false_blocks']), (2, 1, 0))

    def test_claude_does_not_duplicate_the_hook_check_after_a_deferred_commit(self):
        self.mode('enforce')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        command = f'git commit --allow-empty -m subject -m {shlex.quote(STAMP)}'
        self.assertEqual(self.pre(command).stdout, '')
        self.assertEqual(self.rows(), [])
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        self.assertEqual(self.pre(command, 'PostToolUse').stdout, '')
        check, landed = self.rows()
        self.assertEqual((check['event'], check['via'], check['verdict']), ('check', 'commit-msg', 'pass'))
        self.assertEqual((landed['event'], landed['id'], landed['landed']), ('landed', 'tool-1', 'verified'))

    def test_claude_does_not_add_a_check_after_the_hook_refuses_a_deferred_commit(self):
        self.mode('enforce')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        command = 'git commit --allow-empty -m subject'
        self.assertEqual(self.pre(command).stdout, '')
        self.assertEqual(self.rows(), [])
        result = self.git('-c', command, command=self.agent('claude', '/bin/sh'), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pre(command, 'PostToolUseFailure').stdout, '')
        check, landed = self.rows()
        self.assertEqual((check['event'], check['via'], check['verdict']), ('check', 'commit-msg', 'block'))
        self.assertEqual((landed['event'], landed['id'], landed['landed']), ('landed', 'tool-1', 'no-commit'))

    def test_claude_keeps_the_pretooluse_check_without_a_session_id(self):
        self.install()
        self.assertEqual(self.pre('git commit -m x', session=None).stdout, '')
        row, = self.rows()
        self.assertEqual((row['event'], row['session'], row['verdict']), ('check', None, 'unknown'))
        self.assertNotIn('via', row)

    def test_claude_checks_when_the_command_moves_the_hook_before_commit(self):
        self.mode('enforce')
        self.install()
        command = f'mv .git/hooks/commit-msg x && git commit --allow-empty -m {shlex.quote(STAMP)}'
        self.assertEqual(self.pre(command).stdout, '')
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        row, = self.rows()
        self.assertNotIn('via', row)
        self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)

    def test_claude_defers_for_literal_cd_and_git_add_prefixes(self):
        self.mode('enforce')
        self.install()
        (self.repo / 'f').touch()
        claude = self.agent('claude', '/bin/sh')
        for args in ('git add f', "cd '.' && git add -- 'f'"):
            with self.subTest(args=args):
                (self.repo / 'f').write_text(args)
                command = f'{args} && git commit -m {shlex.quote(STAMP)}'
                before = len(self.rows())
                self.assertEqual(self.pre(command).stdout, '')
                self.git('-c', command, command=claude)
                row, = self.rows()[before:]
                self.assertEqual((row['via'], row['verdict']), ('commit-msg', 'pass'))

    def test_claude_checks_a_relative_cd_that_cdpath_can_redirect(self):
        self.install()
        (self.repo / 'target').mkdir()
        other = self.root / 'elsewhere' / 'target'
        other.mkdir(parents=True)
        self.git('init', '-q', '-b', 'main', '--template=', cwd=other)
        command = f'cd target && git commit --allow-empty -m {shlex.quote(STAMP)}'
        self.assertEqual(self.pre(command).stdout, '')
        self.env['CDPATH'] = str(other.parent)
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        row, = self.rows()
        self.assertNotIn('via', row)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD', cwd=other).stdout.strip(), '1')

    def test_claude_checks_when_the_wrapper_cannot_reach_its_gate(self):
        self.install(gate=self.root / 'gone' / 'commit_gate.py')
        command = f'git commit --allow-empty -m {shlex.quote(STAMP)}'
        self.assertEqual(self.pre(command).stdout, '')
        self.git('-c', command, command=self.agent('claude', '/bin/sh'))
        row, = self.rows()
        self.assertNotIn('via', row)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '1')

    def test_claude_keeps_its_check_for_other_shell_syntax(self):
        self.install()
        commit = 'git commit -m subject'
        for index, command in enumerate((
            f'true && {commit}', f'git status && {commit}', f'{commit} && git status',
            f'cd .; {commit}', f'false || {commit}', f'{commit} | cat', f'{commit} &',
            f'({commit})', f'{commit} > .git/hooks/commit-msg', f'{commit} < .git/COMMIT_EDITMSG',
            f'git add "$FILE" && {commit}', f'{commit} && {commit}', f'true\n{commit}',
        ), start=1):
            with self.subTest(command=command):
                before = len(self.rows())
                self.pre(command, tool=f'compound-{index}')
                row, = self.rows()[before:]
                self.assertNotIn('via', row)

    def test_claude_expansions_and_environment_changes_keep_the_pretooluse_check(self):
        hooks = self.root / 'global-hooks'
        self.git('config', '--global', 'core.hooksPath', str(hooks))
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        self.env['B231_FLAG'] = '-qn'
        other_home = shlex.quote(str(self.root / 'other-home'))
        claude = self.agent('claude', '/bin/sh')
        # The glob expands to the short option cluster -qn before Git sees it.
        (self.repo / '-qn').touch()
        for index, command in enumerate((
            'B231_FLAG=-qn; git commit "$B231_FLAG" --allow-empty -m s',
            'git commit "$B231_FLAG" --allow-empty -m s',
            'git commit "$\\\nB231_FLAG" --allow-empty -m s',
            'git commit `printf %s -qn` --allow-empty -m s',
            'git commit -q[n] --allow-empty -m s',
            f'HOME={other_home} git commit --allow-empty -m s',
            f'export HOME={other_home}; git commit --allow-empty -m s',
        ), start=1):
            with self.subTest(command=command):
                before = len(self.rows())
                self.pre(command, tool=f'pre-{index}')
                self.git('-c', command, command=claude)
                row, = self.rows()[before:]
                self.assertNotIn('via', row)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '7')

    def test_claude_literal_messages_and_the_quoted_cat_heredoc_defer(self):
        self.mode('enforce')
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        stamp_arg = shlex.quote(STAMP)
        commands = (
            f'git commit -m "Document the -n flag" -m {stamp_arg}',
            f'git commit -m -n -m {stamp_arg}',
            f'git commit --message=GIT_CONFIG_COUNT --message=hooksPath --message={stamp_arg}',
            f'git commit -m "$(cat <<\'EOF\'\nDocument hooksPath and -qn\n\n{STAMP}\nEOF\n)"',
        )
        for index, command in enumerate(commands, start=1):
            with self.subTest(command=command):
                before = len(self.rows())
                self.pre(f'{command} --allow-empty', tool=f'pre-{index}')
                self.git('-c', f'{command} --allow-empty', command=claude)
                row, = self.rows()[before:]
                self.assertEqual((row['via'], row['verdict']), ('commit-msg', 'pass'))

    def test_claude_checks_when_global_config_includes_a_different_hooks_path(self):
        self.install()
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        included = self.root / 'included'
        hooks = self.root / 'no-hooks'
        self.git('config', '--file', str(included), 'core.hooksPath', str(hooks))
        self.env['B231_INCLUDE'] = str(included)
        claude = self.agent('claude', '/bin/sh')
        for index, options in enumerate((f'-c include.path={shlex.quote(str(included))}',
                                         '--config-env include.path=B231_INCLUDE'), start=1):
            with self.subTest(options=options):
                command = f'git {options} commit --allow-empty -m checked -m {shlex.quote(STAMP)}'
                self.pre(command, tool=f'include-{index}')
                self.git('-c', command, command=claude)
                row, = self.rows()[index - 1:]
                self.assertNotIn('via', row)

    def test_codex_source_from_process_env_and_nested_agents(self):
        self.install()
        codex = self.agent('codex')
        claude = self.agent('claude', codex[0])
        self.env.update(CODEX_THREAD_ID=SESSION, CLAUDE_CODE_SESSION_ID='other-session')
        for command in (codex, ('git',), claude):
            self.commit('-m', 'no stamp', command=command)
        # An agent in the process tree takes precedence over the env fallback.
        self.commit('-m', 'no stamp', command=self.agent('claude'))
        self.assertEqual([(row['source'], row['session']) for row in self.rows()],
                         [('codex', SESSION)] * 3 + [('claude', 'other-session')])

    def test_agent_without_valid_id_uses_its_ancestor_pid(self):
        self.install()
        self.env['CODEX_THREAD_ID'] = '../invalid'
        self.commit('-m', 'no stamp', command=self.agent('codex'))
        row, = self.rows()
        self.assertRegex(row['session'], r'^codex-pid-[1-9][0-9]*$')

    def test_claude_without_an_ancestor_uses_pid_then_pane_or_lets_through(self):
        self.mode('enforce')
        self.install()
        self.env['CLAUDECODE'] = '1'
        self.env['MOBILE_TMUX_TMUX'] = self.script(self.root / 'tmux', '''
case "$1" in
    display-message) printf '\\n' ;;
    *) printf 'no server running\\n' >&2; exit 1 ;;
esac''')
        for env, session in (({'CLAUDE_PID': '1234'}, 'claude-pid-1234'),
                             ({'TMUX_PANE': '%92'}, 'claude-pane-92'),
                             ({}, None)):
            with self.subTest(session=session), mock.patch.dict(self.env, env):
                for attempt in range(3):
                    result = self.commit('-m', 'no stamp', check=False)
                    allowed = session is None or attempt == 2
                    self.assertEqual(result.returncode == 0, allowed)
                    row = self.rows()[-1]
                    self.assertEqual((row['source'], row['session'], row['verdict']),
                                     ('claude', session, 'let_through' if allowed else 'block'))
                    if session is None:
                        self.assertEqual(row['reason'], 'no-session')
                        self.assertEqual(result.stderr, '')
        self.assertEqual(len(self.rows()), 9)
        self.assertEqual({item.name for item in (self.cache / 'commit-gate').glob('*.json')},
                         {'claude-pid-1234.json', 'claude-pane-92.json'})
        self.commit('-m', 'checked', '-m', STAMP)
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['session']), ('pass', None))

    def test_agent_block_limit_and_claude_shared_state(self):
        self.mode('enforce')
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        denied = self.pre('git commit -m x')
        self.assertEqual(json.loads(denied.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.install()
        claude = self.agent('claude')
        second = self.commit('-m', 'no stamp', command=claude, check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(second.stderr.splitlines()[1:], ['Block 2 of 2 in this session.'])
        self.assertEqual(self.commit('-m', 'no stamp', command=claude).stderr, '')

        self.env['CODEX_THREAD_ID'] = 'codex-session'
        codex = self.agent('codex')
        for number in (1, 2):
            denied = self.commit('-m', 'no stamp', command=codex, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(denied.stderr.splitlines()[1:], [f'Block {number} of 2 in this session.'])
        self.assertEqual(self.commit('-m', 'no stamp', command=codex).stderr, '')
        self.assertEqual([row['verdict'] for row in self.rows()], ['block', 'block', 'let_through'] * 2)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').stdout.strip(), '2')

    def test_no_ff_merge_skips(self):
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'side')
        self.commit('-m', 'side')
        self.git('checkout', '-q', 'main')
        self.mode('enforce')
        self.install()
        self.git('merge', '--no-ff', '--no-edit', 'side')
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason']), ('skip', 'merge'))

    def test_merge_and_cherry_pick_continue_skip(self):
        self.write_file('base')
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'side')
        self.write_file('side')
        self.commit('-m', 'side')
        self.git('checkout', '-q', 'main')
        self.write_file('main')
        self.commit('-m', 'main')
        self.git('branch', 'pick')
        self.mode('enforce')
        self.install()
        self.env['GIT_EDITOR'] = 'true'
        for action, reason in (('merge', 'merge'), ('cherry-pick', 'replay')):
            with self.subTest(action=action):
                self.assertNotEqual(self.git(action, 'side', check=False).returncode, 0)
                self.write_file('resolution')
                self.git(action, '--continue')
                self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('skip', reason))
                self.git('checkout', '-q', 'pick')
        self.assertEqual(len(self.rows()), 2)

    def test_fixup_reword_and_amend(self):
        self.write_file('base')
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        self.assertNotEqual(self.git('commit', '--amend', '--no-edit', check=False).returncode, 0)
        for flag in ('--fixup=HEAD', '--squash=HEAD', '--fixup=amend:HEAD'):
            self.write_file(flag)
            self.git('commit', flag, '--no-edit')
        editor = self.script(self.root / 'sequence-editor',
                             'sed "1s/^pick/reword/" "$1" > "$1.new" && mv "$1.new" "$1"')
        self.env.update(GIT_SEQUENCE_EDITOR=editor, GIT_EDITOR='true')
        self.git('rebase', '-i', '--root')
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('block', 'no-line'), ('skip', 'fixup'), ('skip', 'fixup'),
                          ('skip', 'fixup'), ('skip', 'replay')])

    def test_plain_fixup_and_squash_skip(self):
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        self.env['GIT_EDITOR'] = 'true'
        for kind in ('fixup', 'squash'):
            with self.subTest(kind=kind):
                self.write_file(kind)
                self.git('commit', f'--{kind}=HEAD')
                self.assertTrue(self.git('log', '-1', '--format=%s').stdout.startswith(f'{kind}! '))
                row = self.rows()[-1]
                self.assertEqual((row['verdict'], row['reason']), ('skip', 'fixup'))

    def test_verbose_whitespace_with_v_scissors_keeps_the_stamp_above(self):
        self.git('config', 'core.commentChar', 'V')
        self.install()
        editor = self.root / 'editor'
        self.env['GIT_EDITOR'] = str(editor)
        for mode in ('observe', 'enforce'):
            self.mode(mode)
            for count in (1, 2):
                with self.subTest(mode=mode, malformed_below=count):
                    self.write_file(f'{mode}-{count}')
                    bad = shlex.join(['Verified: bad'] * count)
                    self.script(editor, f'''
{{ printf '%s\\n\\n' subject {shlex.quote(STAMP)}; cat "$1";
   printf '%s\\n' {bad}; }} > "$1.new"
mv "$1.new" "$1"''')
                    result = self.git('commit', '-v', '--cleanup=whitespace', check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(stamp.message_status(self.git('log', '-1', '--format=%B').stdout), 'verified')
                    row = self.rows()[-1]
                    self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_cleanup_matrix_never_blocks_a_stamp_or_autosquash_subject(self):
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        editor = self.root / 'editor'
        self.env['GIT_EDITOR'] = str(editor)
        prepare = self.hooks / 'prepare-commit-msg'
        for char, changes_config in (('#', False), (';', False), ('V', False), ('f', False), (';', True)):
            if changes_config:
                self.script(prepare, "git config core.commentChar '#'")
            scissors = char + SCISSORS[1:]
            messages = {'above': ('subject', STAMP, scissors, 'Verified: bad'),
                        'below': ('subject', scissors, STAMP), 'unstamped': ('subject',)}
            for cleanup in ('default', 'strip', 'whitespace', 'verbatim', 'scissors'):
                for verbose in (False, True):
                    for kind in ('above', 'below', 'unstamped', 'fixup'):
                        with self.subTest(char=char, changes_config=changes_config, cleanup=cleanup,
                                          verbose=verbose, message=kind):
                            self.git('config', 'core.commentChar', char)
                            # A configured 'f' can strip the entire fixup subject.
                            args = [f'--cleanup={cleanup}', '--allow-empty-message']
                            if verbose:
                                args.append('-v')
                            if kind == 'fixup':
                                args.append('--fixup=HEAD')
                            else:
                                self.script(editor, f'printf "%s\\n\\n" {shlex.join(messages[kind])} > "$1"')
                            result = self.commit(*args, check=False)
                            if result.returncode:
                                # Git without the hook is the oracle for a candidate the gate refused.
                                self.git('config', 'core.commentChar', char)
                                self.commit(*args, '--no-verify')
                            body = self.git('log', '-1', '--format=%B').stdout
                            if (stamp.message_status(body) != 'missing'
                                    or body.startswith(('fixup! ', 'squash! ', 'amend! '))):
                                self.assertEqual(result.returncode, 0, result.stderr)
                            if char == '#' and cleanup == 'default' and kind == 'fixup' and not verbose:
                                row = self.rows()[-1]
                                self.assertEqual((row['verdict'], row['reason']), ('skip', 'fixup'))

    def test_verbose_commit_with_stamp_below_scissors_is_unknown(self):
        self.install()
        self.write_file('one')
        editor = self.root / 'editor'
        self.env['GIT_EDITOR'] = self.script(editor, f'''
{{ printf '%s\\n\\n%s\\n' 'above' {shlex.quote(STAMP)}; cat "$1"; }} > "$1.new"
mv "$1.new" "$1"''')
        self.git('commit', '-v')
        self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.write_file('two')
        self.script(editor, f'''
{{ printf 'below\\n'; cat "$1"; printf '%s\\n' {shlex.quote(STAMP)}; }} > "$1.new"
mv "$1.new" "$1"''')
        self.git('commit', '-v')
        self.assertNotIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('pass', ''), ('unknown', 'cleanup')])
        self.mode('enforce')
        self.write_file('three')
        self.git('commit', '-v')
        self.assertNotIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))

    def test_literal_scissors_and_instruction_in_message_preserve_stamp(self):
        self.mode('enforce')
        self.install()
        for scissors in (SCISSORS, 'X' + SCISSORS[1:]):
            literal = scissors + '\n' + scissors[0] + ' Do not modify or remove the line above.'
            self.commit('-m', 'subject', '-m', literal, '-m', STAMP)
            self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('unknown', 'cleanup'), ('pass', '')])

    def test_leading_comment_and_fixup_subject_disagree(self):
        self.mode('enforce')
        self.install()
        self.commit('--no-verbose', '--cleanup=default', '-m', '# actual subject', '-m', 'fixup! base')
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), '# actual subject')
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_cleanup_disagreement_allows_each_message_and_editor_mode(self):
        self.install()
        message = ('# leading comment', 'subject', SCISSORS, STAMP)
        self.env['GIT_EDITOR'] = self.script(
            self.root / 'editor', f'printf "%s\\n\\n" {shlex.join(message)} > "$1"')
        for editor in (False, True):
            for cleanup in ('default', 'strip', 'whitespace', 'scissors'):
                for verbose in (False, True):
                    with self.subTest(editor=editor, cleanup=cleanup, verbose=verbose):
                        args = [f'--cleanup={cleanup}']
                        if verbose:
                            args.append('-v')
                        if not editor:
                            for paragraph in message:
                                args.extend(('-m', paragraph))
                        self.mode('observe')
                        self.commit(*args)
                        landed = self.git('log', '-1', '--format=%B').stdout
                        kept = STAMP in landed
                        self.assertEqual('# leading comment' in landed,
                                         cleanup != 'strip' and not (cleanup == 'default' and editor))
                        self.assertEqual(kept, not (verbose or (cleanup == 'scissors' and editor)))
                        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']),
                                         ('unknown', 'cleanup'))
                        self.mode('enforce')
                        result = self.commit(*args, check=False)
                        self.assertFalse(kept and result.returncode != 0, result.stderr)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(self.git('log', '-1', '--format=%B').stdout, landed)
                        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']),
                                         ('unknown', 'cleanup'))

    def test_prepare_hook_can_change_verbose_config_after_git_reads_it(self):
        self.mode('enforce')
        self.install()
        self.git('config', 'commit.verbose', 'false')
        self.script(self.hooks / 'prepare-commit-msg', 'git config commit.verbose true')
        self.commit('-m', 'subject', '-m', SCISSORS, '-m', STAMP)
        self.assertEqual(self.git('config', 'commit.verbose').stdout.strip(), 'true')
        self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_prepare_hook_can_change_comment_char_after_git_reads_it(self):
        self.mode('enforce')
        self.install()
        self.script(self.hooks / 'prepare-commit-msg', "git config core.commentChar '#'")
        for before in (';', '!'):
            for options in ((), ('--no-verbose',)):
                with self.subTest(before=before, options=options):
                    self.git('config', 'core.commentChar', before)
                    result = self.commit(*options, '-m', 's', '-m', SCISSORS, '-m', STAMP, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
                    row = self.rows()[-1]
                    self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_editor_comment_char_can_differ_from_config_seen_by_hook(self):
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        self.script(self.hooks / 'prepare-commit-msg', "git config core.commentChar '#'")
        for char, subject in ((';', 'fixup! base'), ('!', 'squash! base'), ('V', 'amend! base')):
            with self.subTest(char=char, subject=subject):
                self.git('config', 'core.commentChar', char)
                message = f'{char} comment\n{subject}\n'
                self.env['GIT_EDITOR'] = self.script(self.root / 'editor',
                                                     f'printf %s {shlex.quote(message)} > "$1"')
                result = self.commit('--cleanup=strip', check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), subject)
                row = self.rows()[-1]
                self.assertIn((row['verdict'], row['reason']), (('unknown', 'cleanup'), ('skip', 'fixup')))

    def test_messages_with_common_line_starts_keep_normal_verdicts(self):
        self.install()
        body = 'Body text\nMore details\n- one\n* two\n1. three'
        self.commit('-m', 'subject', '-m', body, '-m', STAMP)
        self.commit('-m', 'subject', '-m', body)
        self.mode('enforce')
        result = self.commit('-m', 'subject', '-m', body, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.commit('-m', 'fixup! base', '-m', body)
        self.assertEqual([row['verdict'] for row in self.rows()[:3]], ['pass', 'would_block', 'block'])
        row = self.rows()[-1]
        self.assertEqual((row['verdict'], row['reason']), ('skip', 'fixup'))

    def test_many_line_start_characters_do_not_change_the_verdict(self):
        self.mode('enforce')
        self.install()
        starts = string.ascii_letters + string.digits + '-*+'
        body = '\n'.join(char + ' text' for char in starts)
        self.commit('-m', body, '-m', STAMP)
        self.assertNotEqual(self.commit('-m', body, check=False).returncode, 0)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('pass', ''), ('block', 'no-line')])

    def test_plain_and_verbose_editor_commits_pass_or_block_without_unknown(self):
        self.mode('enforce')
        self.install()
        editor = self.root / 'editor'
        self.env['GIT_EDITOR'] = str(editor)
        for verbose in ((), ('-v',)):
            for stamped in (False, True):
                with self.subTest(verbose=verbose, stamped=stamped):
                    message = f'editor subject\n\n{STAMP}\n' if stamped else 'editor subject\n'
                    self.script(editor, f'printf %s {shlex.quote(message)} > "$1.new"\n'
                                        'cat "$1" >> "$1.new"\nmv "$1.new" "$1"')
                    self.write_file(str((verbose, stamped)))
                    result = self.commit(*verbose, check=False)
                    self.assertEqual(result.returncode == 0, stamped, result.stderr)
                    self.assertEqual(self.rows()[-1]['verdict'], 'pass' if stamped else 'block')

    def test_verbose_cleanup_strips_comments_only_in_strip_mode(self):
        self.mode('enforce')
        self.install()
        message = ('# actual subject', 'fixup! replacement', SCISSORS, STAMP)
        self.env['GIT_EDITOR'] = self.script(
            self.root / 'editor', f'printf "%s\\n\\n" {shlex.join(message)} > "$1"')
        self.commit('-v', '--cleanup=whitespace')
        self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), '# actual subject\n\nfixup! replacement')
        self.commit('-v', '--cleanup=strip')
        self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 'fixup! replacement')
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()], [('unknown', 'cleanup')] * 2)

    def test_cleanup_disagreement_ignores_git_options_and_config(self):
        self.mode('enforce')
        self.install()
        for subject in ('-v', '--cleanup=strip'):
            self.commit('-m', subject, '-m', SCISSORS, '-m', STAMP)
            self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        (self.repo / '-v').touch()
        self.git('add', '--', '-v')
        self.commit('-m', 's', '-m', SCISSORS, '-m', STAMP, '--', '-v')
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        for options in (('-v', '-m', 's'), ('-vm', 's'), ('-qvms',), ('--no-verbose', '-vm', 's')):
            with self.subTest(options=options):
                self.commit(*options, '-m', SCISSORS, '-m', STAMP)
                self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 's')
                self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        for global_options in (('-c', 'commit.verbose=true'),
                               ('-C', str(self.repo), '--git-dir=.git', '--work-tree', str(self.repo),
                                '--namespace', 'example', '--no-pager', '--literal-pathspecs', '-p',
                                '-c', 'commit.verbose=true')):
            with self.subTest(global_options=global_options):
                args = (*global_options, 'commit', '--allow-empty', '-m', 's', '-m', SCISSORS, '-m', STAMP)
                self.git(*args)
                self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 's')
                self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        self.git('config', 'commit.verbose', 'true')
        self.commit('-v', '--no-verbose', '-m', 's', '-m', SCISSORS, '-m', STAMP)
        self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        self.git('config', 'commit.cleanup', 'strip')
        self.commit('--no-verbose', '-m', '# hidden', '-m', 'fixup! base')
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        self.commit('--no-verbose', '--cleanup=strip', '-m', '# hidden', '-m', 'fixup! base')
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), 'fixup! base')
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))
        self.commit('-m', 's', '-m', SCISSORS, '-m', STAMP, '--cleanup=whitespace', '--no-cleanup', '--no-verbose')
        self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.assertEqual((self.rows()[-1]['verdict'], self.rows()[-1]['reason']), ('unknown', 'cleanup'))

    def test_unresolved_cleanup_lets_alias_and_verbatim_through(self):
        self.mode('enforce')
        self.install()
        for command in (('-c', 'alias.ci=commit', 'ci'), ('commit', '--cleanup=verbatim')):
            with self.subTest(command=command):
                self.git(*command, '--allow-empty', '-m', '# subject', '-m', 'fixup! base')
                self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), '# subject')
                row = self.rows()[-1]
                self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_message_source_and_edit_flags_choose_whether_to_strip_comments(self):
        self.commit('-m', 'base')
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('branch', 'amend', base)
        self.install()
        template = self.root / 'template'
        template.write_text('template\n')
        visited = self.root / 'editor-ran'
        self.env['GIT_EDITOR'] = self.script(self.root / 'editor', f': > {shlex.quote(str(visited))}')
        message = shlex.quote('# comment\n\nfixup! replacement\n')
        self.script(self.hooks / 'prepare-commit-msg', f'printf %s {message} > "$1"')
        for args, editor in (
            ((), True), (('-am', 'subject'), False), (('-F', str(template)), False),
            (('-m', '--cleanup=strip'), False), (('--message=--cleanup=strip',), False),
            (('-C', base), False), (('-c', base), True), (('--no-edit',), False),
            (('--fixup=' + base,), False), (('--fixup=amend',), False), (('--fixup=amend:' + base,), True),
            (('--fixup=reword:' + base,), True), (('--squash=' + base,), True),
            (('-t', str(template)), True), (('-m', 'subject', '--edit'), True),
            (('-F', str(template), '-e'), True), (('-C', base, '-e'), True),
            (('--fixup=' + base, '-e'), True), (('-e', '--no-edit'), False),
        ):
            with self.subTest(args=args):
                visited.unlink(missing_ok=True)
                self.commit('--no-verbose', '--cleanup=default', *args)
                self.assertEqual(visited.exists(), editor)
                self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(),
                                 'fixup! replacement' if editor else '# comment')
                row = self.rows()[-1]
                self.assertEqual((row['verdict'], row['reason']), ('unknown', 'cleanup'))

    def test_edited_fixup_subject_follows_blank_lines_and_comments(self):
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        for prefix, subject in (('   \\n\\n', 'fixup! base'), ('# editor comment\\n\\t\\n', 'squash! base')):
            with self.subTest(subject=subject):
                self.env['GIT_EDITOR'] = self.script(self.root / 'fixup-editor',
                                                     f'printf "{prefix}%s\\n" {shlex.quote(subject)} > "$1"')
                self.commit('--fixup=HEAD', '-e', '--no-verbose', '--cleanup=default')
                self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), subject)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('skip', 'fixup'), ('unknown', 'cleanup')])

    def test_unambiguous_non_default_comment_configs_and_large_messages(self):
        self.mode('enforce')
        self.install()
        for key, value in (('core.commentChar', ';'), ('core.commentString', '//'), ('core.commentChar', 'auto')):
            with self.subTest(key=key, value=value):
                self.git('config', key, value)
                self.assertNotEqual(self.commit('-m', 'no stamp', check=False).returncode, 0)
                self.git('config', '--unset', key)
        message = self.root / 'large-message'
        message.write_text('x' * 1_000_001 + '\n')
        self.commit('-F', str(message))
        self.git('config', 'core.commentChar', '#')
        self.commit('-m', 'default comment', '-m', STAMP)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('block', 'no-line')] * 3 + [('unknown', 'size'), ('pass', '')])

    def test_stats_counts_final_messages_without_landed_rows(self):
        self.commit('-m', 'base')
        self.git('checkout', '-qb', 'side')
        self.commit('-m', 'side')
        self.git('checkout', '-q', 'main')
        self.install()
        self.commit('-m', 'pass', '-m', STAMP)
        self.commit('-m', 'observe')
        self.git('merge', '--no-ff', '--no-edit', 'side')
        self.mode('enforce')
        self.commit('-m', '# comment', '-m', 'fixup! base')
        self.env['CODEX_THREAD_ID'] = SESSION
        for _ in range(3):
            self.commit('-m', 'enforce', check=False)
        with mock.patch.object(paths, 'COMMIT_GATE_LOG', self.log), mock.patch.object(paths, 'CACHE', self.cache):
            self.assertEqual(commit_gate.stats(), {
                'days': 7, 'commits': 6, 'passed': 1, 'would_block': 1, 'blocked': 2, 'let_through': 1,
                'unknown': 1, 'stamped': 1, 'unstamped': 2, 'false_blocks': 0, 'rate': 0.0,
            })


if __name__ == '__main__':
    unittest.main()
