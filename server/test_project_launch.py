import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import paths
import backlog
import project_launch
import projects
from agent_browser_cli import note

HERE = Path(__file__).resolve().parent
PID = 'a1b2c3d4e5f6'


class UnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        for name, value in (('TETHER', self.temp / 'tether'), ('CODEX', self.temp / 'codex')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {'TMUX': '', 'TMUX_PANE': '', 'CODEX_HOME': '',
                                             'TETHER_CDP_URL': '', 'PLAYWRIGHT_MCP_CDP_ENDPOINT': '',
                                             'TETHER_LAUNCH': ''})
        patch.start()
        self.addCleanup(patch.stop)
        if not self._testMethodName.startswith('test_writable'):
            patch = mock.patch.object(project_launch, 'codex_writable', return_value=True)
            patch.start()
            self.addCleanup(patch.stop)

    def config(self, text):
        paths.CODEX.mkdir(exist_ok=True)
        (paths.CODEX / 'config.toml').write_text(text)

    def test_writable_follows_codex_trust(self):
        folder = self.temp / 'work'
        folder.mkdir()
        self.assertFalse(project_launch.codex_writable([], str(folder)))  # no config: no entry, read-only
        for level, writable in (('trusted', True), ('untrusted', True)):  # any trust level gives the workspace profile
            self.config(f'[projects."{folder}"]\ntrust_level = "{level}"\n')
            self.assertEqual(project_launch.codex_writable([], str(folder)), writable, level)
        self.config(f'[projects."{folder}"]\n')
        self.assertFalse(project_launch.codex_writable([], str(folder)))

    def test_writable_uses_the_git_root_and_the_main_checkout(self):
        repo = self.temp / 'repo'
        sub = repo / 'src'
        sub.mkdir(parents=True)
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        subprocess.run([*git, 'init', '-q', str(repo)], check=True)
        subprocess.run([*git, '-C', str(repo), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
        tree = self.temp / 'tree'
        subprocess.run([*git, '-C', str(repo), 'worktree', 'add', '-q', str(tree)], check=True)
        trusted = f'[projects."{os.path.realpath(repo)}"]\ntrust_level = "trusted"\n'
        self.config(trusted)
        self.assertTrue(project_launch.codex_writable([], str(sub)))
        self.assertTrue(project_launch.codex_writable([], str(tree)))
        self.assertFalse(project_launch.codex_writable([], str(self.temp)))
        self.config(trusted + f'[projects."{sub}"]\n')  # the first entry found wins, as in Codex
        self.assertFalse(project_launch.codex_writable([], str(sub)))
        self.config(trusted)
        (repo / '.codex').mkdir()
        (repo / '.codex' / 'config.toml').write_text('sandbox_mode = "read-only"\n')
        self.assertFalse(project_launch.codex_writable([], str(sub)))

    def test_writable_sandbox_args_and_config_win(self):
        folder = str(self.temp)
        for args in (['-s', 'workspace-write'], ['-sworkspace-write'], ['--sandbox=danger-full-access'], ['--full-auto'],
                     ['--yolo'], ['-c', 'sandbox_mode="workspace-write"'], ['--config=sandbox_mode=workspace-write']):
            self.assertTrue(project_launch.codex_writable(args, folder), args)
        for args in (['-sread-only'], ['--config', 'sandbox_mode=read-only'], ['-csandbox_mode=read-only'],
                     ['-s', 'read-only', '-c', 'sandbox_mode=workspace-write'], ['-p', 'safe'], ['--profile=safe'],
                     ['-c', 'sandbox_mode=other']):
            self.assertFalse(project_launch.codex_writable(args, folder), args)
        self.config(f'sandbox_mode = "workspace-write"\n')
        self.assertTrue(project_launch.codex_writable([], folder))
        self.assertFalse(project_launch.codex_writable(['-s', 'read-only'], folder))
        self.config('sandbox_mode = "read-only"\n')
        self.assertFalse(project_launch.codex_writable([], folder))
        self.config('default_permissions = "mine"\n')
        self.assertFalse(project_launch.codex_writable([], folder))
        self.config('not toml [')
        self.assertFalse(project_launch.codex_writable([], folder))
        self.config(f'[projects."{folder}"]\ntrust_level = "trusted"\n')
        self.assertTrue(project_launch.codex_writable([], folder))
        for args in (['-C', 'elsewhere'], ['--cd=elsewhere'], ['-Celsewhere']):
            self.assertFalse(project_launch.codex_writable(args, folder), args)

    def test_writable_round_two_cases(self):
        repo = self.temp / 'repo'
        deep = repo / 'a' / 'b'
        deep.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        trusted = f'[projects."{os.path.realpath(repo)}"]\ntrust_level = "trusted"\n'
        self.config(trusted)
        self.assertTrue(project_launch.codex_writable([], str(deep)))
        # A project layer between the start folder and the repository root counts too.
        (repo / 'a' / '.codex').mkdir()
        (repo / 'a' / '.codex' / 'config.toml').write_text('sandbox_mode = "read-only"\n')
        self.assertFalse(project_launch.codex_writable([], str(deep)))
        (repo / 'a' / '.codex' / 'config.toml').unlink()
        # An empty entry for the real path of an alias shadows the trusted root.
        alias = self.temp / 'alias'
        alias.symlink_to(deep)
        self.config(trusted + f'[projects."{os.path.realpath(deep)}"]\n')
        self.assertFalse(project_launch.codex_writable([], str(alias)))
        self.config(trusted)
        self.assertTrue(project_launch.codex_writable([], str(alias)))
        # Permission profiles win over sandbox_mode in any order; text after -- is the prompt.
        self.assertFalse(project_launch.codex_writable(
            ['-c', 'default_permissions=":read-only"', '-c', 'sandbox_mode="workspace-write"'], str(deep)))
        self.assertFalse(project_launch.codex_writable(['--', '--yolo'], str(self.temp)))
        self.assertTrue(project_launch.codex_writable(['--yolo', '--', 'hi'], str(self.temp)))
        # System or managed config is not modelled.
        system = self.temp / 'system.toml'
        system.write_text('')
        with mock.patch.object(project_launch, 'SYSTEM_FILES', (str(system),)):
            self.assertFalse(project_launch.codex_writable([], str(deep)))
            self.assertFalse(project_launch.codex_writable(['-c', 'sandbox_mode="workspace-write"'], str(deep)))
            for args in (['--sandbox', 'workspace-write'], ['--yolo'], ['--full-auto']):
                self.assertFalse(project_launch.codex_writable(args, str(deep)), args)  # requirements apply after

    def test_writable_round_three_cli_project_overrides(self):
        repo = self.temp / 'repo'
        work = repo / 'work'
        work.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        self.config(f'[projects."{os.path.realpath(repo)}"]\ntrust_level = "trusted"\n')
        self.assertTrue(project_launch.codex_writable([], str(work)))
        for key in (f'projects.{work}', f'projects."{work}"', 'projects', ' projects.x.trust_level',
                    'project_root_markers'):
            self.assertFalse(project_launch.codex_writable(['-c', f'{key}={{}}'], str(work)), key)
        self.assertTrue(project_launch.codex_writable(['-c', 'sandbox_mode="workspace-write"', '-c', 'projects={}'],
                                                      str(work)), 'an explicit sandbox_mode wins')

    def test_writable_read_only_codex_gets_no_folders(self):
        with mock.patch('os.getcwd', return_value=str(self.temp)):
            args = project_launch.codex_args(PID, ['resume', '--last'])
        self.assertNotIn('--add-dir', args)
        self.assertEqual(args[-2:], ['resume', '--last'])

    def test_toml_string_round_trips(self):
        for text in ('plain', 'a "quote" and \\ slash', 'line\nnext\ttab\r', 'emoji 🙂 and ü', 'del \x7f bell \x07', "'''"):
            self.assertEqual(tomllib.loads('v = ' + project_launch.toml_string(text))['v'], text)

    def test_claude_gets_folder_and_hook_before_the_prompt(self):
        args = project_launch.claude_args(PID, ['fix the bug'])
        self.assertEqual(args[:2], ['--add-dir', str(projects.bundle(PID))])
        self.assertEqual(args[2], '--settings')
        self.assertEqual(args[-1], 'fix the bug')
        hook = json.loads(args[3])['hooks']['SessionStart'][0]
        self.assertEqual(hook['matcher'], 'startup|resume|clear|compact')
        self.assertIn(f'hook {PID}', hook['hooks'][0]['command'])
        self.assertNotIn('autoMemoryDirectory', json.loads(args[3]))
        self.assertTrue((projects.bundle(PID) / 'notes').is_dir())

    def test_claude_plain_cases(self):
        for args in (['-p', 'hi'], ['--print'], ['mcp', 'list'], ['--settings', 'x.json'], ['--settings=x.json'], ['--version'], ['update']):
            self.assertEqual(project_launch.claude_args(PID, args), args)

    def test_codex_gets_the_notes_part_even_for_an_empty_project(self):
        args = project_launch.codex_args(PID, ['resume', '--last'])
        folder = str(backlog.folder())
        items = str(paths.items())
        self.assertEqual((args[0], args[2:]), ('-c', ['--add-dir', folder, '--add-dir', items, 'resume', '--last']))
        self.assertTrue(paths.items().is_dir())
        self.assertTrue(backlog.folder().is_dir())
        self.assertIn('## Notes', tomllib.loads('v = ' + args[1].partition('=')[2])['v'])
        projects.set_instructions(PID, 'Use "pnpm".\nNo emoji 🙂', 0)
        args = project_launch.codex_args(PID, ['resume', '--last'])
        self.assertEqual(args[0], '-c')
        self.assertEqual(args[2:], ['--add-dir', folder, '--add-dir', items, 'resume', '--last'])
        key, _, value = args[1].partition('=')
        self.assertEqual(key, 'developer_instructions')
        self.assertIn('Use "pnpm".', tomllib.loads('v = ' + value)['v'])

    def test_codex_plain_cases(self):
        projects.set_instructions(PID, 'x', 0)
        for args in (['exec', 'hi'], ['e', 'hi'], ['login'], ['--help'], ['review']):
            self.assertEqual(project_launch.codex_args(PID, args), args)
        # Own instructions: Tether adds none, but the item and backlog folders still open, so `tether item` works.
        folders = ['--add-dir', str(backlog.folder()), '--add-dir', str(paths.items())]
        mine = ['-c', 'developer_instructions="mine"']
        self.assertEqual(project_launch.codex_args(PID, mine), [*folders, *mine])
        (paths.CODEX).mkdir()
        (paths.CODEX / 'config.toml').write_text('model = "x"\ndeveloper_instructions = "own"\n')
        self.assertEqual(project_launch.codex_args(PID, []), folders)
        self.assertEqual(project_launch.codex_args('', []), [])

    def test_hook_prints_context_or_nothing(self):
        def run_hook(pid):
            out = subprocess.run([sys.executable, str(HERE / 'project_launch.py'), 'hook', pid], input='{}', capture_output=True,
                                 text=True, env=dict(os.environ, TETHER_HOME=str(self.temp / 'tether')), timeout=10)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout
        self.assertIn('## Notes', json.loads(run_hook(PID))['hookSpecificOutput']['additionalContext'])
        self.assertEqual(run_hook('../bad'), '')
        projects.set_instructions(PID, 'Use pnpm.', 0)
        data = json.loads(run_hook(PID))['hookSpecificOutput']
        self.assertEqual(data['hookEventName'], 'SessionStart')
        self.assertIn('Use pnpm.', data['additionalContext'])

    def test_codex_browser_note_with_and_without_project(self):
        for pid in (None, PID):
            for enabled in (False, True):
                with self.subTest(pid=pid, enabled=enabled), mock.patch.dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x' if enabled else ''):
                    args = project_launch.codex_args(pid, ['resume', '--last'])
                    if not pid and not enabled:
                        self.assertEqual(args, ['resume', '--last'])
                        continue
                    text = tomllib.loads(args[1])['developer_instructions']
                    self.assertEqual(note() in text, enabled)
                    if pid:
                        self.assertIn('## Notes', text)
                        if enabled:
                            self.assertTrue(text.endswith('\n\n' + note()))
                    else:
                        self.assertEqual(text, note())

    def test_codex_browser_keeps_plain_commands_and_own_instructions(self):
        with mock.patch.dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x'):
            self.test_codex_plain_cases()

    def test_codex_browser_shell_handoff_without_mcp(self):
        paths.CODEX.mkdir()
        (paths.CODEX / 'config.toml').write_text(
            '[shell_environment_policy]\ninherit = "core"\n'
            '[shell_environment_policy.set]\nKEEP = "user value"\n')
        values = {'TETHER_CDP_URL': 'ws://127.0.0.1:1/cdp/test',
                  'PLAYWRIGHT_MCP_CDP_ENDPOINT': 'ws://127.0.0.1:1/cdp/test',
                  'TMUX': '/tmp/test-tmux,1,0', 'TMUX_PANE': '%42'}
        with mock.patch.dict(os.environ, values):
            args = project_launch.codex_args(None, ['resume', '--last'])
        overrides = {}
        for flag, value in zip(args[::2], args[1::2]):
            if flag == '-c':
                parsed = tomllib.loads(value)
                overrides.update(parsed.get('shell_environment_policy', {}).get('set', {}))
        self.assertEqual(overrides, values)
        self.assertFalse(any('shell_environment_policy.inherit=' in arg for arg in args))
        self.assertFalse(any('shell_environment_policy.set=' in arg for arg in args))

    def test_codex_browser_env_preserves_configured_names(self):
        paths.CODEX.mkdir()
        (paths.CODEX / 'config.toml').write_text('''
[mcp_servers."browser.tool"]
command = "npx"
args = ["@playwright/mcp"]
env_vars = ["USER_TOKEN", {name = "REMOTE_TOKEN", source = "remote"}]
[mcp_servers.direct]
command = "playwright-mcp"
env_vars = ["PLAYWRIGHT_MCP_CDP_ENDPOINT"]
[mcp_servers.other]
command = "other"
[mcp_servers.http]
url = "https://example.test/playwright"
''')
        with mock.patch.dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x'):
            args = project_launch.codex_args(None, [])
        config = tomllib.loads(next(arg for arg in args if arg.startswith('mcp_servers.')))
        self.assertEqual(config['mcp_servers']['browser.tool']['env_vars'],
                         ['USER_TOKEN', {'name': 'REMOTE_TOKEN', 'source': 'remote'}, 'PLAYWRIGHT_MCP_CDP_ENDPOINT'])
        with mock.patch.dict(os.environ, TETHER_CDP_URL=''):
            self.assertEqual(project_launch.codex_args(None, []), [])

    def test_codex_launch_without_project_and_fail_open(self):
        with mock.patch.dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x'), \
                mock.patch.object(project_launch.shutil, 'which', return_value='/fake/codex'), \
                mock.patch.object(project_launch, 'current_project', return_value=None), \
                mock.patch.object(project_launch.os, 'execv') as execute:
            project_launch.launch('codex', ['resume'])
            self.assertEqual(tomllib.loads(execute.call_args.args[1][2])['developer_instructions'], note())
            os.environ.pop('TETHER_LAUNCH')
            with mock.patch.object(project_launch, 'codex_browser_env_args', side_effect=ValueError('bad config')), \
                    mock.patch.object(project_launch.sys, 'stderr'):
                project_launch.launch('codex', ['resume'])
            execute.assert_called_with('/fake/codex', ['codex', 'resume'])


@unittest.skipUnless(shutil.which('tmux'), 'needs tmux')
class WrapperTests(unittest.TestCase):
    """The real launcher in an isolated tmux server, with fake claude and codex that write their argv to a file."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        bin_dir = self.temp / 'bin'
        bin_dir.mkdir()
        for agent in ('claude', 'codex'):
            fake = bin_dir / agent
            fake.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                            f'open({str(self.temp / "argv")!r}, "a").write(json.dumps([sys.argv[0]] + sys.argv[1:] + [os.environ.get("TETHER_LAUNCH", "")]) + "\\n")\n')
            fake.chmod(0o755)
        self.socket = str(self.temp / 'tmux.sock')
        self.env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE', 'TETHER_LAUNCH', 'INVOCATION_ID', 'TETHER_CDP_URL')}
        self.env.update(PATH=f'{bin_dir}:{os.environ["PATH"]}', TETHER_HOME=str(self.temp / 'tether'), CODEX_HOME=str(self.temp / 'codex'), SHELL='/bin/sh')
        (self.temp / 'codex').mkdir()
        (self.temp / 'codex' / 'config.toml').write_text('sandbox_mode = "workspace-write"\n')  # --add-dir needs a writable sandbox
        self.tmux('new-session', '-d', '-s', 'proj', '-c', str(self.temp),  # no real .codex folder above it
                   '-x', '120', '-y', '30', '/bin/sh')  # not a login shell: /etc/profile would reset PATH
        self.addCleanup(self.tmux, 'kill-server')

    def tmux(self, *args):
        assert self.socket
        return subprocess.run(['tmux', '-f', '/dev/null', '-S', self.socket, *args], capture_output=True, text=True, env=self.env, timeout=5)

    def run_in_pane(self, command):
        target = self.temp / 'argv'
        target.unlink(missing_ok=True)
        self.tmux('send-keys', '-t', 'proj', f'{command}; tmux -S {self.socket} wait-for -S done', 'Enter')
        self.tmux('wait-for', 'done')
        for _ in range(50):
            if target.exists():
                return [json.loads(line) for line in target.read_text().splitlines()][-1]
            time.sleep(0.1)
        self.fail(f'no run for {command}: {self.tmux("capture-pane", "-p", "-t", "proj").stdout}')

    def project(self):
        return self.tmux('show-options', '-t', 'proj', '-v', '@project').stdout.strip()

    def test_wrapper_paths(self):
        launcher = f'{sys.executable} {HERE / "project_launch.py"}'
        argv = self.run_in_pane(f'{launcher} claude "fix it"')
        pid = self.project()
        self.assertRegex(pid, projects.ID)
        self.assertEqual(argv[1:3], ['--add-dir', str(self.temp / 'tether/projects' / pid)])
        self.assertEqual(argv[-2:], ['fix it', '1'])
        pane = self.tmux('display-message', '-p', '-t', 'proj', '#{@project_rev}').stdout.strip()
        self.assertEqual(pane, f'{pid}:0')
        argv = self.run_in_pane(f'{launcher} codex resume --last')
        folder = str(self.temp / 'tether/backlog')
        items = str(self.temp / 'tether/items')
        self.assertEqual((argv[1], argv[3:]), ('-c', ['--add-dir', folder, '--add-dir', items, 'resume', '--last', '1']))  # the Notes part, even with no instructions
        with mock.patch.object(paths, 'TETHER', self.temp / 'tether'):
            projects.set_instructions(pid, 'Use pnpm.', 0)
        argv = self.run_in_pane(f'{launcher} codex resume --last')
        self.assertEqual(argv[1], '-c')
        self.assertIn('Use pnpm.', argv[2])
        self.assertEqual(self.run_in_pane(f'{launcher} codex exec hi')[1:], ['exec', 'hi', '1'])
        self.assertEqual(self.run_in_pane(f'TETHER_LAUNCH=1 {launcher} claude x')[1:], ['x', '1'])
        # A broken project folder: the agent still starts, plain.
        (self.temp / 'tether/projects').chmod(0o500)  # read-only: the new project's notes folder cannot be made
        self.addCleanup((self.temp / 'tether/projects').chmod, 0o700)
        self.tmux('set-option', '-t', 'proj', '@project', 'b1b2c3d4e5f6')
        self.assertEqual(self.run_in_pane(f'{launcher} claude y')[1:], ['y', '1'])

    def test_outside_tmux_runs_plain(self):
        out = subprocess.run([sys.executable, str(HERE / 'project_launch.py'), 'claude', 'z'], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads((self.temp / 'argv').read_text())[1:], ['z', '1'])


if __name__ == '__main__':
    unittest.main()
