"""B231: exercise the installed commit-msg hook with real Git and isolated state."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import commit_gate
import paths

GATE = Path(commit_gate.__file__).resolve()
PID = 'abcdef012345'
SESSION = 'test-session'
STAMP = 'Verified: tests(server 412 ok)'
SCISSORS = '# ------------------------ >8 ------------------------'


class CommitMsgHook(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.config = self.root / 'config'
        self.cache = self.root / 'cache'
        self.tether = self.root / 'tether'
        self.log = self.root / 'log' / 'commit-gate.log'
        self.socket = self.root / 'tmux-sockets'
        for folder in (self.repo, self.home, self.config, self.socket):
            folder.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('GIT_') and key not in {
                        'TMUX', 'TMUX_PANE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDECODE', 'CODEX_THREAD_ID',
                        'CODEX_SESSION_ID', 'CLAUDE_PID', 'MOBILE_TMUX_TMUX',
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

    def git(self, *args, check=True, command=('git',), cwd=None):
        return subprocess.run([*command, *args], cwd=cwd or self.repo, env=self.env, capture_output=True,
                              text=True, check=check, timeout=20)

    def cli(self, *args, check=True):
        return self.git(str(GATE), *args, check=check, command=(sys.executable,))

    def install(self, **options):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return commit_gate.install_hook(self.repo, **{'gate': GATE, 'python': self.python, **options})

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

    def pre(self, command, event='PreToolUse', tool='tool-1'):
        data = {'hook_event_name': event, 'tool_name': 'Bash', 'session_id': SESSION, 'cwd': str(self.repo),
                'tool_use_id': tool, 'tool_input': {'command': command}}
        return subprocess.run([sys.executable, str(GATE.with_name('claude_hooks.py'))], input=json.dumps(data),
                              cwd=self.repo, env=self.env, capture_output=True, text=True, check=True, timeout=20)

    def test_observe_logs_one_terminal_verdict_without_message_text(self):
        self.install()
        self.commit('-m', 'private test message')
        check, = self.rows()
        self.assertEqual({key: value for key, value in check.items() if key != 'at'}, {
            'event': 'check', 'via': 'commit-msg', 'source': 'terminal', 'id': None, 'session': None,
            'project': None, 'mode': 'observe', 'verdict': 'would_block', 'reason': 'no-line',
            'count': 1, 'folder': str(self.repo),
        })
        self.assertNotIn('private test message', self.log.read_text())

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
        self.assertIn(f'{shlex.quote(str(python))} {shlex.quote(str(gate))} commit-msg "$1" '
                      '|| [ $? -ne 3 ] || exit 1  # tether-commit-gate-call v1',
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

    def test_user_hook_with_set_e_lets_a_missing_gate_through(self):
        self.mode('enforce')
        line = self.user_hook_line(gate=self.root / 'missing gate.py')
        self.script(self.hook, f'set -e\n{line}')
        self.commit('-m', 'unstamped')
        self.assertEqual(self.rows(), [])

    def test_claude_defers_to_active_user_hook_call_only(self):
        line = self.user_hook_line()
        self.mode('enforce')
        self.env['CLAUDE_CODE_SESSION_ID'] = SESSION
        claude = self.agent('claude', '/bin/sh')
        command = f'git commit --allow-empty -m checked -m {shlex.quote(STAMP)}'
        self.script(self.hook, f'  # {line}\nexit 0')
        self.pre(command)
        self.git('-c', command, command=claude)
        row, = self.rows()
        self.assertNotIn('via', row)
        self.log.unlink()
        self.script(self.hook, f'  {line}')
        self.pre(command, tool='active')
        self.git('-c', command, command=claude)
        self.pre(command, 'PostToolUse', tool='active')
        row, = self.rows()
        self.assertEqual((row['via'], row['verdict']), ('commit-msg', 'pass'))

    def test_verbose_commit_under_user_hook_finds_git_past_extra_shell(self):
        self.mode('enforce')
        line = self.user_hook_line()
        self.script(self.hook, f'/bin/sh -c {shlex.quote(line)} gate "$1"\nexit $?')
        self.assertNotEqual(self.commit('-v', '-m', 'subject', '-m', SCISSORS, '-m', STAMP, check=False).returncode, 0)
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason']), ('block', 'no-line'))

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
        self.assertFalse((self.cache / 'commit-gate' / f'{SESSION}.json').exists())
        self.commit('-m', 'x', command=claude)
        self.assertEqual(self.pre(command, 'PostToolUse').stdout, '')
        row, = self.rows()
        self.assertEqual((row['via'], row['source'], row['session']), ('commit-msg', 'claude', SESSION))
        for index, command in enumerate((
            'git commit --no-verify -m x', 'git commit -qn -m x', 'git commit -an -m x',
            'git commit -"qn" -m x', 'git -c core.hooksPath=other commit -m x',
            'git --config-env=core.hooksPath=OTHER commit -m x', 'GIT_CONFIG_COUNT=0 git commit -m x',
            'git --git-dir=.git commit -m x', 'GIT_DIR=.git git commit -m x',
            'git -C "$OTHER" commit -m x', 'cd "$OTHER" && git commit -m x',
        ), start=2):
            with self.subTest(command=command):
                self.pre(command, tool=f'tool-{index}')
                self.assertEqual(len(self.rows()), index)
                self.assertNotIn('via', self.rows()[-1])

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
                         [('block', 'no-line')] + [('skip', 'fixup')] * 3 + [('skip', 'replay')])

    def test_verbose_commit_cuts_scissors_before_checking_stamp(self):
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
        self.assertEqual([row['verdict'] for row in self.rows()], ['pass', 'would_block'])
        self.mode('enforce')
        self.write_file('three')
        self.assertNotEqual(self.git('commit', '-v', check=False).returncode, 0)
        self.assertEqual(self.rows()[-1]['verdict'], 'block')

    def test_literal_scissors_and_instruction_in_message_preserve_stamp(self):
        self.mode('enforce')
        self.install()
        for scissors in (SCISSORS, 'X' + SCISSORS[1:]):
            literal = scissors + '\n' + scissors[0] + ' Do not modify or remove the line above.'
            self.commit('-m', 'subject', '-m', literal, '-m', STAMP)
            self.assertIn(STAMP, self.git('log', '-1', '--format=%B').stdout)
        self.assertEqual([row['verdict'] for row in self.rows()], ['pass', 'pass'])

    def test_message_keeps_the_leading_comment_as_subject(self):
        self.mode('enforce')
        self.install()
        result = self.commit('-m', '# actual subject', '-m', 'fixup! base', check=False)
        self.assertNotEqual(result.returncode, 0)
        row, = self.rows()
        self.assertEqual((row['verdict'], row['reason']), ('block', 'no-line'))

    def test_cleanup_modes_match_real_git_for_message_and_editor(self):
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
                        self.assertEqual(self.rows()[-1]['verdict'], 'pass' if kept else 'would_block')
                        self.mode('enforce')
                        result = self.commit(*args, check=False)
                        self.assertEqual(result.returncode == 0, kept, result.stderr)
                        self.assertEqual(self.rows()[-1]['verdict'], 'pass' if kept else 'block')

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
        result = self.commit('-v', '--cleanup=whitespace', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.commit('-v', '--cleanup=strip')
        self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 'fixup! replacement')
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('block', 'no-line'), ('skip', 'fixup')])

    def test_commit_options_distinguish_values_from_flags_and_use_config_defaults(self):
        self.mode('enforce')
        self.install()
        for subject in ('-v', '--cleanup=strip'):
            self.commit('-m', subject, '-m', SCISSORS, '-m', STAMP)
            self.assertEqual(self.rows()[-1]['verdict'], 'pass')
        (self.repo / '-v').touch()
        self.git('add', '--', '-v')
        self.commit('-m', 's', '-m', SCISSORS, '-m', STAMP, '--', '-v')
        self.assertEqual(self.rows()[-1]['verdict'], 'pass')
        for options in (('-v', '-m', 's'), ('-vm', 's'), ('-qvms',)):
            with self.subTest(options=options):
                result = self.commit(*options, '-m', SCISSORS, '-m', STAMP, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.rows()[-1]['verdict'], 'block')
        for global_options in (('-c', 'commit.verbose=true'),
                               ('-C', str(self.repo), '--git-dir=.git', '--work-tree', str(self.repo),
                                '--namespace', 'example', '--no-pager', '--literal-pathspecs', '-p',
                                '-c', 'commit.verbose=true')):
            with self.subTest(global_options=global_options):
                args = (*global_options, 'commit', '--allow-empty', '-m', 's', '-m', SCISSORS, '-m', STAMP)
                self.assertNotEqual(self.git(*args, check=False).returncode, 0)
                self.assertEqual(self.rows()[-1]['verdict'], 'block')
                self.mode('observe')
                self.git(*args)
                self.assertEqual(self.git('log', '-1', '--format=%B').stdout.strip(), 's')
                self.assertEqual(self.rows()[-1]['verdict'], 'would_block')
                self.mode('enforce')
        self.git('config', 'commit.verbose', 'true')
        self.commit('-v', '--no-verbose', '-m', 's', '-m', SCISSORS, '-m', STAMP)
        self.assertEqual(self.rows()[-1]['verdict'], 'pass')
        self.git('config', 'commit.cleanup', 'strip')
        self.commit('--no-verbose', '-m', '# hidden', '-m', 'fixup! base')
        self.assertEqual(self.rows()[-1]['verdict'], 'skip')
        self.commit('-m', 's', '-m', SCISSORS, '-m', STAMP, '--cleanup=whitespace', '--no-cleanup', '--no-verbose')
        self.assertEqual(self.rows()[-1]['verdict'], 'pass')

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
                self.commit(*args)
                self.assertEqual(visited.exists(), editor)
                self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(),
                                 'fixup! replacement' if editor else '# comment')
                row = self.rows()[-1]
                self.assertEqual((row['verdict'], row['reason']),
                                 ('skip', 'fixup') if editor else ('would_block', 'no-line'))

    def test_edited_fixup_subject_follows_blank_lines_and_comments(self):
        self.commit('-m', 'base')
        self.mode('enforce')
        self.install()
        for prefix, subject in (('   \\n\\n', 'fixup! base'), ('# editor comment\\n\\t\\n', 'squash! base')):
            with self.subTest(subject=subject):
                self.env['GIT_EDITOR'] = self.script(self.root / 'fixup-editor',
                                                     f'printf "{prefix}%s\\n" {shlex.quote(subject)} > "$1"')
                self.commit('--fixup=HEAD', '-e')
                self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), subject)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()], [('skip', 'fixup')] * 2)

    def test_non_default_comment_config_and_large_messages_are_unknown(self):
        self.mode('enforce')
        self.install()
        for key, value in (('core.commentChar', ';'), ('core.commentString', '//'), ('core.commentChar', 'auto')):
            self.git('config', key, value)
            self.commit('-m', 'no stamp')
            self.git('config', '--unset', key)
        message = self.root / 'large-message'
        message.write_text('x' * 1_000_001 + '\n')
        self.commit('-F', str(message))
        self.git('config', 'core.commentChar', '#')
        self.commit('-m', 'default comment', '-m', STAMP)
        self.assertEqual([(row['verdict'], row['reason']) for row in self.rows()],
                         [('unknown', 'comment-char')] * 3 + [('unknown', 'size'), ('pass', '')])

    def test_stats_counts_final_messages_without_landed_rows(self):
        self.install()
        self.commit('-m', 'pass', '-m', STAMP)
        self.commit('-m', 'observe')
        self.commit('--fixup=HEAD')
        self.mode('enforce')
        self.git('config', 'core.commentChar', ';')
        self.commit('-m', 'unknown')
        self.git('config', '--unset', 'core.commentChar')
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
