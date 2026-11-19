"""B231: exercise the installed commit-msg hook with real Git and isolated state."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import commit_gate
import paths

GATE = Path(commit_gate.__file__).resolve()
PID = 'abcdef012345'
SESSION = 'test-session'
STAMP = 'Verified: tests(server 412 ok)'


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
                        'CODEX_SESSION_ID', 'MOBILE_TMUX_TMUX',
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
        self.local = self.hooks / 'commit-msg.local'
        # Keep the real parent walk inside this fixture even when an agent started the test runner.
        launcher = f'''import runpy
import sys

gate = runpy.run_path(sys.argv[1])
main = gate['_main']
ancestors = gate['_ancestors']

def fixture_ancestors():
    for pid, comm in ancestors():
        yield pid, comm
        if pid == {os.getpid()}:
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
        self.assertFalse(self.local.exists())
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

    def test_foreign_hook_runs_first_and_uninstall_restores_it(self):
        self.mode('enforce')
        self.script(self.hook, 'printf "\\n\\nVerified: none (local hook)\\n" >> "$1"')
        self.hook.chmod(0o711)
        original = self.hook.read_bytes()
        self.assertIn('commit-msg.local', self.install())
        self.assertEqual(self.local.stat().st_mode & 0o777, 0o711)
        self.commit('-m', 'the local hook supplies the stamp')
        self.assertEqual([row['verdict'] for row in self.rows()], ['pass'])
        self.script(self.local, 'printf "local hook rejected\\n" >&2\nexit 9')
        failed = self.commit('-m', 'local rejection', '-m', STAMP, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stderr, 'local hook rejected\n')
        self.assertEqual(len(self.rows()), 1)
        self.local.write_bytes(original)
        self.local.chmod(0o711)
        self.install(uninstall=True)
        self.assertFalse(self.local.exists())
        self.assertEqual(self.hook.read_bytes(), original)
        self.assertEqual(self.hook.stat().st_mode & 0o777, 0o711)
        self.install(uninstall=True)
        self.assertEqual(self.hook.read_bytes(), original)

    def test_installer_refuses_collision_symlink_and_worktree_hooks(self):
        self.script(self.hook, 'exit 0')
        self.script(self.local, 'exit 1')
        with self.assertRaisesRegex(commit_gate.GateError, 'commit-msg.local'):
            self.install()
        self.assertIn('exit 0', self.hook.read_text())
        self.hook.unlink()
        self.hook.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(commit_gate.GateError, 'symlink'):
            self.install()
        self.git('config', 'core.hooksPath', '.tracked-hooks')
        with self.assertRaisesRegex(commit_gate.GateError, 'work tree'):
            self.install()
        self.assertFalse((self.repo / '.tracked-hooks').exists())

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
