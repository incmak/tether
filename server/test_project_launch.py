import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
import shlex
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
import autonomy
import codex_level
import project_launch
import projects
from agent_browser_cli import note
from test_tmux_isolation import private_tmux

HERE = Path(__file__).resolve().parent
PID = 'a1b2c3d4e5f6'
SUBAGENT_INSTRUCTIONS = 'features.multi_agent_v2.subagent_developer_instructions'


class UnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        for name, value in (('TETHER', self.temp / 'tether'), ('CODEX', self.temp / 'codex'),
                            ('CONFIG', self.temp / 'config')):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name, file in (('SYSTEM_CONFIG', 'system.toml'), ('MANAGED_CONFIG', 'managed.toml'),
                           ('MANAGED_PREFERENCES', 'managed-preferences')):
            patch = mock.patch.object(project_launch, name, self.temp / file)
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
        for args in (['-s', 'workspace-write'], ['-sworkspace-write'], ['--sandbox=danger-full-access'],
                     ['--yolo'], ['-c', 'sandbox_mode="workspace-write"'], ['--config=sandbox_mode=workspace-write']):
            self.assertTrue(project_launch.codex_writable(args, folder), args)
        for args in (['-sread-only'], ['--config', 'sandbox_mode=read-only'], ['-csandbox_mode=read-only'],
                     ['-s', 'read-only', '-c', 'sandbox_mode=workspace-write'], ['-p', 'safe'], ['--profile=safe'],
                     ['-c', 'sandbox_mode=other'], ['--full-auto']):
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
            for args in (['--sandbox', 'workspace-write'], ['--yolo']):
                self.assertFalse(project_launch.codex_writable(args, str(deep)), args)  # requirements apply after

    def test_writable_macos_forced_preferences_and_other_systems(self):
        prefs = self.temp / 'managed'
        (prefs / 'someone').mkdir(parents=True)
        with mock.patch.object(project_launch, 'MANAGED_PREFERENCES', str(prefs)), \
                mock.patch.object(project_launch, 'SYSTEM_FILES', ()), \
                mock.patch.object(project_launch.sys, 'platform', 'darwin'):
            self.assertTrue(project_launch.codex_writable(['--yolo'], str(self.temp)))
            (prefs / 'someone' / 'com.openai.codex.plist').write_text('')
            self.assertFalse(project_launch.codex_writable(['--yolo'], str(self.temp)), 'per-user forced prefs')
            (prefs / 'someone' / 'com.openai.codex.plist').unlink()
            (prefs / 'com.openai.codex.plist').write_text('')
            self.assertFalse(project_launch.codex_writable(['--sandbox', 'workspace-write'], str(self.temp)))
        with mock.patch.object(project_launch, 'MANAGED_PREFERENCES', str(self.temp / 'none')), \
                mock.patch.object(project_launch, 'SYSTEM_FILES', ()), \
                mock.patch.object(project_launch.sys, 'platform', 'darwin'):
            self.assertTrue(project_launch.codex_writable(['--yolo'], str(self.temp)))
        with mock.patch.object(project_launch.sys, 'platform', 'win32'):
            self.assertFalse(project_launch.codex_writable(['--yolo'], str(self.temp)))

    def test_writable_subcommand_flags_replace_root_flags(self):
        folder = str(self.temp)
        for args in (['--yolo', 'resume', '--last', '--sandbox', 'read-only'], ['--yolo', 'fork', '-s', 'read-only'],
                     ['-s', 'workspace-write', 'resume', '--last', '--approve-for-me', '-c', 'default_permissions=":read-only"'],
                     ['--full-auto', 'resume', '-c', 'sandbox_mode=read-only'],
                     ['-s', 'workspace-write', '-c', 'default_permissions=":read-only"']):
            self.assertFalse(project_launch.codex_writable(args, folder), args)
        for args in (['--yolo', 'resume', '--last'], ['-s', 'workspace-write', 'fork', 'abc']):
            self.assertTrue(project_launch.codex_writable(args, folder), args)

    def test_writable_submodule_and_separate_git_dir_have_no_trust_root(self):
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', '-c', 'protocol.file.allow=always']
        inner = self.temp / 'inner'
        subprocess.run([*git, 'init', '-q', str(inner)], check=True)
        subprocess.run([*git, '-C', str(inner), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
        outer = self.temp / 'outer'
        subprocess.run([*git, 'init', '-q', str(outer)], check=True)
        subprocess.run([*git, '-C', str(outer), 'submodule', '-q', 'add', str(inner), 'sub'], check=True,
                       capture_output=True)
        src = outer / 'sub' / 'src'
        src.mkdir()
        self.config(f'[projects."{os.path.realpath(outer / "sub")}"]\ntrust_level = "trusted"\n')
        self.assertFalse(project_launch.codex_writable([], str(src)), 'Codex rejects the submodule .git file')
        self.assertTrue(project_launch.codex_writable([], str(outer / 'sub')), 'the cwd entry itself still counts')
        split = self.temp / 'split'
        subprocess.run([*git, 'init', '-q', '--separate-git-dir', str(self.temp / 'split.git'), str(split)], check=True)
        (split / 'a').mkdir()
        self.config(f'[projects."{os.path.realpath(split)}"]\ntrust_level = "trusted"\n')
        self.assertFalse(project_launch.codex_writable([], str(split / 'a')))

    def test_writable_root_keeps_spaces_in_folder_names(self):
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        for name in ('repo', 'repo ', 'repo\u00a0'):
            subprocess.run([*git, 'init', '-q', str(self.temp / name)], check=True)
            (self.temp / name / 'src').mkdir()
        self.config(f'[projects."{os.path.realpath(self.temp / "repo")}"]\ntrust_level = "trusted"\n')
        self.assertTrue(project_launch.codex_writable([], str(self.temp / 'repo' / 'src')))
        for name in ('repo ', 'repo\u00a0'):
            self.assertFalse(project_launch.codex_writable([], str(self.temp / name / 'src')), repr(name))

    def test_writable_skips_a_git_folder_without_head(self):
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        outer = self.temp / 'outer'
        subprocess.run([*git, 'init', '-q', str(outer)], check=True)
        (outer / 'inner' / '.git').mkdir(parents=True)
        self.config(f'[projects."{os.path.realpath(outer)}"]\ntrust_level = "trusted"\n')
        self.assertEqual(project_launch._repo_root(str(outer / 'inner')), os.path.realpath(outer))

    def test_writable_dangling_head_and_cd_symlink(self):
        repo = self.temp / 'repo'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'HEAD').symlink_to(self.temp / 'missing')
        (repo / 'src').mkdir()
        self.config(f'[projects."{os.path.realpath(repo)}"]\ntrust_level = "trusted"\n')
        self.assertFalse(project_launch.codex_writable([], str(repo / 'src')), 'Codex skips a .git without HEAD')
        main = self.temp / 'main'
        subprocess.run(['git', 'init', '-q', str(main)], check=True)
        (main / 'inner' / 'src').mkdir(parents=True)
        (self.temp / 'alias').symlink_to(main / 'inner')
        self.config(f'[projects."{os.path.realpath(main)}"]\ntrust_level = "trusted"\n')
        self.assertTrue(project_launch.codex_writable(['--cd', str(main / 'inner' / 'src')], str(self.temp)))
        self.assertFalse(project_launch.codex_writable(['--cd', str(self.temp / 'alias' / 'src')], str(self.temp)))

    def test_writable_round_six_paths_and_modes(self):
        folder = str(self.temp)
        for args in (['-s', ' workspace-write '], ['-s', '"workspace-write"']):
            self.assertFalse(project_launch.codex_writable(args, folder), args)  # Codex exits 2 on these
        self.assertTrue(project_launch.codex_writable(['-c', 'sandbox_mode=" workspace-write"'.replace(' w', 'w')], folder))
        main, other = self.temp / 'main', self.temp / 'other'
        subprocess.run(['git', 'init', '-q', str(main)], check=True)
        for d in (main / 'src', other / 'child', other / 'src'):
            d.mkdir(parents=True)
        (main / 'alias').symlink_to(other / 'child')
        self.config(f'[projects."{os.path.realpath(main)}"]\ntrust_level = "trusted"\n')
        self.assertTrue(project_launch.codex_writable(['--cd', str(main / 'src')], folder))
        self.assertFalse(project_launch.codex_writable(['--cd', f'{main}/alias/../src'], folder))

    def test_writable_round_seven_equals_short_options_and_double_slash(self):
        folder = str(self.temp)
        self.config(f'[projects."{os.path.realpath(folder)}"]\ntrust_level = "trusted"\n')
        for args in (['-c', 'sandbox_mode=workspace-write', '-c=sandbox_mode=read-only'],
                     ['--yolo', '-c=default_permissions=":read-only"'], ['-s=read-only'],
                     ['--yolo', '-s', ' workspace-write '], ['--yolo', '-s', '"workspace-write"']):
            self.assertFalse(project_launch.codex_writable(args, folder), args)
        self.assertTrue(project_launch.codex_writable(['-s=workspace-write'], folder))
        self.assertTrue(project_launch.codex_writable(['--yolo', '-s', 'read-only'], folder))
        self.assertEqual(project_launch._join('/', '//a//b/../c'), '/a/c')  # PathUri keeps one leading slash
        for args in (['--worktree'], ['exec', '--ignore-user-config'], ['--yolo', '--worktree']):
            self.assertFalse(project_launch.codex_writable(args, folder), args)  # new checkout or no config.toml

    def test_writable_worktree_pointer_must_match_its_registration(self):
        git = ['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        main = self.temp / 'main'
        subprocess.run([*git, 'init', '-q', str(main)], check=True)
        subprocess.run([*git, '-C', str(main), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
        linked = self.temp / 'linked'
        subprocess.run([*git, '-C', str(main), 'worktree', 'add', '-q', str(linked)], check=True, capture_output=True)
        self.config(f'[projects."{os.path.realpath(main)}"]\ntrust_level = "trusted"\n')
        self.assertTrue(project_launch.codex_writable([], str(linked)))
        copy = self.temp / 'copy'
        copy.mkdir()
        (copy / '.git').write_text((linked / '.git').read_text())
        self.assertFalse(project_launch.codex_writable([], str(copy)), 'the backlink names another checkout')
        base = self.temp / 'base'
        (base / 'worktrees').mkdir(parents=True)
        subprocess.run([*git, 'init', '-q', '--separate-git-dir', str(base / 'worktrees' / 'split.git'),
                        str(base / 'split')], check=True)
        (base / 'split' / 'src').mkdir()
        self.config(f'[projects."{os.path.realpath(base / "split")}"]\ntrust_level = "trusted"\n')
        self.assertFalse(project_launch.codex_writable([], str(base / 'split' / 'src')), 'no worktree metadata')
        self.config(f'[projects."{os.path.realpath(main)}"]\ntrust_level = "trusted"\n')
        self.assertFalse(project_launch.codex_writable(['--full-auto'], str(linked)), 'Codex rejects the flag')
        for args in (['--yolo', '--full-auto=true'], ['--full-auto=false']):
            self.assertFalse(project_launch.codex_writable(args, str(linked)), args)
        registration = Path(os.path.realpath(main / '.git' / 'worktrees' / 'linked'))
        for name in ('gitdir', 'commondir'):
            original = (registration / name).read_bytes()
            (self.temp / name).write_bytes(original)
            (registration / name).unlink()
            (registration / name).symlink_to(self.temp / name)
            self.assertFalse(project_launch.codex_writable([], str(linked)), f'{name} as a symlink')
            (registration / name).unlink()
            (registration / name).write_bytes(original)
            self.assertTrue(project_launch.codex_writable([], str(linked)))
        original = (registration / 'gitdir').read_bytes()
        (registration / 'gitdir').write_bytes(original.rstrip() + b'\xc2\xa0\n')
        self.assertFalse(project_launch.codex_writable([], str(linked)), 'Codex trims ASCII space only')
        (registration / 'gitdir').write_bytes(original)
        self.assertTrue(project_launch.codex_writable([], str(linked)))
        original = (registration / 'gitdir').read_bytes()
        (registration / 'gitdir').write_bytes(str(linked).encode() + b'/\x00/../.git\n')
        self.assertFalse(project_launch.codex_writable([], str(linked)), 'Codex rejects NUL before normalizing')
        (registration / 'gitdir').write_bytes(original)
        (self.temp / 'broken').symlink_to(f'{self.temp}/missing/../{linked.name}')
        (self.temp / 'brokenmain').symlink_to(f'{self.temp}/missing/../main')
        for name, text in (('gitdir', f'{self.temp}/broken/.git\n'), ('commondir', f'{self.temp}/brokenmain/.git\n')):
            saved = (registration / name).read_bytes()
            (registration / name).write_text(text)
            self.assertFalse(project_launch.codex_writable([], str(linked)), f'broken symlink in {name}')
            (registration / name).write_bytes(saved)
        self.assertTrue(project_launch.codex_writable([], str(linked)))
        (self.temp / 'mainalias').symlink_to(main)
        saved = (linked / '.git').read_bytes()
        (linked / '.git').write_text(f'gitdir: {self.temp}/mainalias/.git/worktrees/linked\n')
        self.config(f'[projects."{self.temp}/mainalias"]\ntrust_level = "trusted"\n[projects."{os.path.realpath(main)}"]\n')
        self.assertFalse(project_launch.codex_writable([], str(linked)), 'the canonical root key comes first')
        (linked / '.git').write_bytes(saved)
        self.config(f'[projects."{os.path.realpath(main)}"]\ntrust_level = "trusted"\n')
        aliases = self.temp / 'aliases'
        aliases.mkdir()
        (linked / 'child').mkdir()
        (aliases / 'hop').symlink_to(linked / 'child')
        original = (registration / 'gitdir').read_bytes()
        (registration / 'gitdir').write_text(f'{aliases}/hop/../.git\n')
        self.assertFalse(project_launch.codex_writable([], str(linked)), 'Codex resolves .. before symlinks')
        (registration / 'gitdir').write_bytes(original)
        self.assertTrue(project_launch.codex_writable([], str(linked)))
        (registration / 'gitdir').write_bytes((registration / 'gitdir').read_bytes() + b' ' * 70000)
        self.assertFalse(project_launch.codex_writable([], str(linked)), 'gitdir over 64 KiB')

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
        # No --add-dir: from Claude Code 2.1.326 it stops a new tab on a trust prompt (B188).
        self.assertNotIn('--add-dir', args)
        self.assertEqual([*args[:1], *args[2:]], ['--settings', 'fix the bug'])
        self.assertEqual(json.loads(args[1])['permissions']['additionalDirectories'], [str(projects.bundle(PID))])
        hook = json.loads(args[1])['hooks']['SessionStart'][0]
        self.assertEqual(hook['matcher'], 'startup|resume|clear|compact')
        self.assertIn(f'hook {PID}', hook['hooks'][0]['command'])
        self.assertNotIn('autoMemoryDirectory', json.loads(args[1]))
        self.assertTrue((projects.bundle(PID) / 'notes').is_dir())

    def test_claude_advisor_goes_into_settings_never_as_a_flag(self):
        import claude_advisor
        self.assertNotIn('advisorModel', json.loads(project_launch.claude_args(PID, [])[1]))
        claude_advisor.set_global('opus')
        args = project_launch.claude_args(PID, ['fix the bug'])
        self.assertEqual(json.loads(args[1])['advisorModel'], 'opus')
        self.assertNotIn('--advisor', args)
        claude_advisor.set_project(PID, True, 'off')
        self.assertNotIn('advisorModel', json.loads(project_launch.claude_args(PID, [])[1]))
        # A tab outside any project gets the global choice; plain runs (-p, --settings) get nothing.
        self.assertEqual(project_launch.claude_plain_args(['hi']), ['--settings', '{"advisorModel": "opus"}', 'hi'])
        for args in (['-p', 'hi'], ['--settings', 'x.json']):
            self.assertEqual(project_launch.claude_plain_args(args), args)
        claude_advisor.set_global('off')
        self.assertEqual(project_launch.claude_plain_args(['hi']), ['hi'])

    def test_claude_advisor_marks_the_pane(self):
        import claude_advisor
        claude_advisor.set_global('opus')
        with mock.patch.dict(os.environ, {'TMUX': '/tmp/x,1,0', 'TMUX_PANE': '%7', 'DISABLE_TELEMETRY': '1'}), \
                mock.patch.object(project_launch, '_tmux') as tmux, mock.patch.object(paths, 'CLAUDE', self.temp / 'c'):
            project_launch.advisor_settings(None)
            tmux.assert_called_with('set-option', '-p', '-t', '%7', '@advisor', 'opus!no-flags')
            claude_advisor.set_global('off')
            project_launch.advisor_settings(None)
            tmux.assert_called_with('set-option', '-p', '-u', '-t', '%7', '@advisor')
            # A restart with its own --settings clears the old mark; a plain -p run leaves the pane alone.
            claude_advisor.set_global('opus')
            tmux.reset_mock()
            self.assertEqual(project_launch.claude_args(PID, ['--settings', 'x.json']), ['--settings', 'x.json'])
            tmux.assert_called_once_with('set-option', '-p', '-u', '-t', '%7', '@advisor')
            tmux.reset_mock()
            project_launch.claude_plain_args(['-p', 'hi'])
            tmux.assert_not_called()

    def test_claude_plain_cases(self):
        for args in (['-p', 'hi'], ['--print'], ['mcp', 'list'], ['--settings', 'x.json'], ['--settings=x.json'], ['--version'], ['update']):
            self.assertEqual(project_launch.claude_args(PID, args), args)

    def test_codex_gets_the_notes_part_even_for_an_empty_project(self):
        args = project_launch.codex_args(PID, ['resume', '--last'])
        folder = str(backlog.folder())
        items = str(paths.items())
        rem = str(paths.reminders())
        self.assertEqual(args[2:], ['--add-dir', folder, '--add-dir', items, '--add-dir', rem, 'resume', '--last'])
        self.assertTrue(paths.items().is_dir())
        self.assertTrue(backlog.folder().is_dir())
        self.assertIn('## Notes', tomllib.loads('v = ' + args[1].partition('=')[2])['v'])
        projects.set_instructions(PID, 'Use "pnpm".\nNo emoji 🙂', 0)
        args = project_launch.codex_args(PID, ['resume', '--last'])
        self.assertEqual(args[0], '-c')
        self.assertEqual(args[2:], ['--add-dir', folder, '--add-dir', items, '--add-dir', rem, 'resume', '--last'])
        key, _, value = args[1].partition('=')
        self.assertEqual(key, 'developer_instructions')
        self.assertIn('Use "pnpm".', tomllib.loads('v = ' + value)['v'])

    def test_codex_plain_cases(self):
        projects.set_instructions(PID, 'x', 0)
        for args in (['exec', 'hi'], ['e', 'hi'], ['login'], ['--help'], ['review']):
            self.assertEqual(project_launch.codex_args(PID, args), args)

    def standing_instructions(self, argv):
        options = argv[:argv.index('--')] if '--' in argv else argv
        values = {}
        for arg in project_launch._option(options, '-c', '--config'):
            key, _, value = arg.partition('=')
            if key in ('developer_instructions', SUBAGENT_INSTRUCTIONS):
                values[key] = value
        self.assertNotIn(SUBAGENT_INSTRUCTIONS, values)  # It would replace what subagents inherit.
        return {key: tomllib.loads('v = ' + value)['v'] for key, value in values.items()}

    @staticmethod
    def block(pid):
        return autonomy.codex_standing_rules(autonomy.resolve(pid), codex_level.resolve(pid))

    def test_codex_gets_standing_rules_for_the_project_or_the_global_settings(self):
        autonomy.set_global(2)
        codex_level.set_global(1)
        autonomy.set_project(PID, True, 0)
        codex_level.set_project(PID, True, 4)
        for pid, level, codex in ((PID, 0, 4), (None, 2, 1)):
            with self.subTest(project=pid):
                overrides = self.standing_instructions(project_launch.codex_args(pid, ['resume', '--last']))
                block = self.block(pid)
                self.assertIn(autonomy.SAFETY, block)
                self.assertIn(f'Autonomy level {level} (', block)
                self.assertIn(f'Codex level {codex} (', block)
                self.assertTrue(overrides['developer_instructions'].endswith(block))

    def test_codex_keeps_home_and_cli_instructions_with_standing_rules(self):
        home = 'Keep "quotes" and \\slashes.\nNext line 🙂'
        self.config('"developer_instructions" = \'\'\'\n' + home + "'''\n")
        cli = 'Keep "CLI" and \\slashes.\nTake precedence 🙂'
        value = 'developer_instructions=' + project_launch.toml_string(cli)
        for args, expected in (([], home), (['-c', value], cli), (['-c' + value], cli),
                               (['--config=' + value], cli), (['-c=' + value], cli),
                               (['-c', 'developer_instructions=older', 'resume', '--config', value,
                                 '--', 'developer_instructions=prompt'], cli),
                               (['--', '--config=developer_instructions=prompt'], home),
                               (['-c', 'developer_instructions=unquoted CLI text'], 'unquoted CLI text')):
            with self.subTest(args=args), mock.patch.dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x'):
                argv = project_launch.codex_args(PID, args)
                overrides = self.standing_instructions(argv)
                self.assertEqual(overrides['developer_instructions'],
                                 expected + '\n\n' + self.block(PID))
                folders = ['--add-dir', str(backlog.folder()), '--add-dir', str(paths.items()),
                           '--add-dir', str(paths.reminders())]
                self.assertIn(folders, [argv[i:i+len(folders)] for i in range(len(argv))])
                self.assertFalse(any(arg.startswith('shell_environment_policy.set.TETHER_CDP_URL') for arg in argv))

    def instruction_repo(self, name='repo'):
        repo = self.temp / name
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        return repo

    @staticmethod
    def instruction_layer(folder, contents):
        layer = folder / '.codex' / 'config.toml'
        layer.parent.mkdir(parents=True, exist_ok=True)
        layer.write_text(contents)
        return layer

    @staticmethod
    def project_trust(folder, level='trusted'):
        key = project_launch.toml_string(os.path.realpath(folder))
        return f'\n[projects.{key}]\n' + (f'trust_level = "{level}"\n' if level else '')

    def assert_codex_text(self, cwd, text, args=()):
        heading = text + '\n\n' if text is not None else ''
        override = 'developer_instructions=' + project_launch.toml_string(heading + self.block(None))
        self.assertEqual(project_launch.codex_args(None, list(args), cwd=cwd), ['-c', override, *args])

    def test_codex_trusted_instructions_replace_home_text(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        self.assert_codex_text(repo, 'repo')

    def test_codex_untrusted_instructions_leave_home_or_tether_text(self):
        repo = self.instruction_repo()
        without_layer = project_launch.codex_args(PID, [], cwd=repo)
        self.instruction_layer(repo, 'developer_instructions = "repo"\n' + self.project_trust(repo))
        for trust in ('', self.project_trust(repo, 'untrusted')):
            with self.subTest(trust=trust):
                self.config(trust)
                self.assertEqual(project_launch.codex_args(PID, [], cwd=repo), without_layer)
                self.config('developer_instructions = "home"' + trust)
                self.assert_codex_text(repo, 'home')

    def test_codex_empty_project_layers_leave_home_text(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        (repo / '.codex').mkdir()
        self.assert_codex_text(repo, 'home')
        self.instruction_layer(repo, 'sandbox_mode = "workspace-write"')
        self.assert_codex_text(repo, 'home')

    def test_codex_nested_trust_and_layer_order(self):
        repo = self.instruction_repo()
        nested = repo / 'src'
        nested.mkdir()
        root_trust = self.project_trust(repo)
        self.config(root_trust)
        self.instruction_layer(repo, 'developer_instructions = "root"')
        self.assert_codex_text(nested, 'root')
        self.instruction_layer(nested, 'developer_instructions = "nested"')
        for level, expected in (('trusted', 'nested'), ('untrusted', 'root'), (None, 'nested')):
            with self.subTest(level=level):
                self.config(root_trust + self.project_trust(nested, level))
                self.assert_codex_text(nested, expected)

    def test_codex_project_layers_stop_at_git_root(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(self.temp))
        self.instruction_layer(self.temp, 'developer_instructions = "above root"')
        self.assert_codex_text(repo, 'home')

    def test_codex_worktree_uses_main_checkout_trust_only(self):
        repo = self.instruction_repo()
        git = ['git', '-C', str(repo), '-c', 'user.email=t@example.com', '-c', 'user.name=t']
        subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
        linked = self.temp / 'linked'
        subprocess.run([*git, 'worktree', 'add', '-q', str(linked)], check=True)
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = "main"')
        self.assert_codex_text(linked, 'home')
        self.instruction_layer(linked, 'developer_instructions = "worktree"')
        self.assert_codex_text(linked, 'worktree')

    def test_codex_cli_instructions_win_over_trusted_project_layer(self):
        repo = self.instruction_repo()
        self.config(self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        args = ['resume', '--config=developer_instructions="cli"']
        self.assertEqual(project_launch.codex_args(None, args, cwd=repo), [
            'resume', '--config=developer_instructions=' + project_launch.toml_string('cli\n\n' + self.block(None))])

    def test_codex_ignore_user_config_drops_user_and_profile_but_keeps_cli_trust(self):
        repo = self.instruction_repo()
        trust = self.project_trust(repo)
        self.config('developer_instructions = "home"' + trust)
        (paths.CODEX / 'work.config.toml').write_text('developer_instructions = "profile"' + trust)
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        args = ['--ignore-user-config', '-p', 'work']
        self.assert_codex_text(repo, None, args)
        self.assert_codex_text(repo, 'repo', [*args, '-c', f'projects.{os.path.realpath(repo)}.trust_level="trusted"'])

    def test_codex_invalid_or_unreadable_config_omits_override(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        files = (project_launch.SYSTEM_CONFIG, paths.CODEX / 'config.toml', paths.CODEX / 'work.config.toml',
                 project_launch.MANAGED_CONFIG, self.instruction_layer(repo, 'developer_instructions = "repo"'))
        args = ['-p', 'work']
        read_text = Path.read_text
        for file in files:
            with self.subTest(file=file):
                original = file.read_text() if file.exists() else ''
                file.write_text('invalid [toml')
                self.assertEqual(project_launch.codex_args(None, args, cwd=repo), args)
                file.write_text(original)

                def read(path, *a, **kw):
                    if os.path.realpath(path) == os.path.realpath(file):
                        raise PermissionError(path)
                    return read_text(path, *a, **kw)

                with mock.patch.object(Path, 'read_text', read):
                    self.assertEqual(project_launch.codex_args(None, args, cwd=repo), args)

    def test_codex_legacy_profile_or_invalid_markers_omit_override(self):
        repo = self.instruction_repo()
        for config, args in (('profile = "old"', []), ('', ['-c', 'profile="old"']),
                             ('project_root_markers = false', []), ('project_root_markers = [1]', []),
                             ('', ['-c', 'project_root_markers.invalid=".git"'])):
            with self.subTest(config=config, args=args):
                self.config(config)
                self.assertEqual(project_launch.codex_args(None, args, cwd=repo), args)

    def test_codex_unmodelled_managed_config_uses_home_and_rules(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        self.instruction_layer(repo, 'invalid [toml')  # The trusted project file would normally cancel the override.
        prefs = project_launch.MANAGED_PREFERENCES / 'user'
        prefs.mkdir(parents=True)
        (prefs / 'com.openai.codex.plist').touch()
        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform), mock.patch.object(project_launch.sys, 'platform', platform):
                self.assert_codex_text(repo, 'home')

    def test_codex_never_opens_untrusted_instruction_layer(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo, 'untrusted'))
        without_layer = project_launch.codex_args(None, [], cwd=repo)
        layer = self.instruction_layer(repo, 'invalid [toml')
        with mock.patch.object(project_launch, '_toml', wraps=project_launch._toml) as load:
            self.assertEqual(project_launch.codex_args(None, [], cwd=repo), without_layer)
            self.assertNotIn(os.path.realpath(layer), [os.path.realpath(call.args[0]) for call in load.call_args_list])
        layer_dir = os.path.realpath(layer.parent)
        stat = os.stat

        def deny_layer_dir(path, *args, **kwargs):
            if str(path) == layer_dir:
                raise PermissionError(path)
            return stat(path, *args, **kwargs)

        with mock.patch.object(os, 'stat', deny_layer_dir):
            self.assertEqual(project_launch.codex_args(None, [], cwd=repo), without_layer)

    def test_codex_profile_overrides_home_text_and_trust(self):
        repo = self.instruction_repo()
        profile = paths.CODEX / 'work.config.toml'
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        profile.write_text('developer_instructions = "profile"' + self.project_trust(repo, 'untrusted'))
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        for args in (['-p', 'work'], ['--profile', 'work'], ['--profile=work']):
            with self.subTest(args=args):
                self.assert_codex_text(repo, 'profile', args)
        self.config('developer_instructions = "home"')
        profile.write_text('developer_instructions = "profile"' + self.project_trust(repo))
        self.assert_codex_text(repo, 'repo', ['-p', 'work'])

    def test_codex_cli_discovery_splits_dots_literally_and_replaces_intermediate_scalars(self):
        repo = self.instruction_repo('repo.with.dot')
        self.instruction_layer(repo, 'developer_instructions = "dotted"')
        dotted_key = project_launch.toml_string(os.path.realpath(repo))
        self.assert_codex_text(repo, None, ['-c', f'projects.{dotted_key}.trust_level="trusted"'])
        repo = self.instruction_repo('plain')
        self.instruction_layer(repo, 'developer_instructions = "plain"')
        key = os.path.realpath(repo)
        trust = ['-c', f'projects.{key}.trust_level="trusted"']
        self.assert_codex_text(repo, 'plain', trust)
        self.assert_codex_text(repo, 'plain', ['-c', 'projects=1', '-c', f'projects.{key}=false', *trust])
        self.assert_codex_text(repo, None, [*trust, '-c', 'projects={}'])

    def test_codex_managed_instructions_and_trust_have_highest_precedence(self):
        repo = self.instruction_repo()
        self.config(self.project_trust(repo, 'untrusted'))
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        args = ['-c', f'projects.{os.path.realpath(repo)}.trust_level="untrusted"']
        project_launch.MANAGED_CONFIG.write_text(self.project_trust(repo))
        self.assert_codex_text(repo, 'repo', args)
        project_launch.MANAGED_CONFIG.write_text('developer_instructions = "managed"')
        self.assertEqual(project_launch.codex_args(None, args, cwd=repo), args)

    def test_codex_system_config_provides_instructions_and_trust(self):
        repo = self.instruction_repo()
        project_launch.SYSTEM_CONFIG.write_text('developer_instructions = "system"' + self.project_trust(repo))
        self.assert_codex_text(repo, 'system')
        self.config('developer_instructions = "home"')
        self.assert_codex_text(repo, 'home')
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        self.assert_codex_text(repo, 'repo')

    def test_codex_cd_selects_start_folder(self):
        repo = self.instruction_repo()
        self.config(self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = "root"')
        self.instruction_layer(repo / 'src', 'developer_instructions = "nested"')
        self.assert_codex_text(repo, 'root')
        self.assert_codex_text(repo, 'nested', ['-C', 'src'])
        self.assert_codex_text(self.temp, 'nested', ['--cd=repo/src'])

    def test_codex_cd_tries_canonical_trust_key_before_logical_key(self):
        repo = self.instruction_repo()
        alias = self.temp / 'alias'
        alias.symlink_to(repo)
        self.instruction_layer(repo, 'developer_instructions = "repo"')
        logical = f'[projects."{os.path.realpath(self.temp)}/alias"]\ntrust_level = "trusted"\n'
        self.config(logical)
        self.assert_codex_text(self.temp, 'repo', ['-C', 'alias'])
        self.config(logical + self.project_trust(repo, 'untrusted'))
        self.assert_codex_text(self.temp, None, ['-C', 'alias'])

    def test_codex_cd_keeps_logical_ancestors_for_git_trust(self):
        repo = self.instruction_repo()
        nested = repo / 'src'
        self.instruction_layer(nested, 'developer_instructions = "nested"')
        (self.temp / 'alias').symlink_to(nested)
        self.config(self.project_trust(repo))
        self.assert_codex_text(repo, 'nested', ['-C', 'src'])
        self.assert_codex_text(self.temp, None, ['-C', 'alias'])

    def test_codex_managed_worktree_does_not_use_source_instructions(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = "source repo"')
        self.assert_codex_text(repo, 'home', ['--worktree'])

    def test_codex_project_markers_can_change_or_disable_root_discovery(self):
        repo = self.instruction_repo()
        nested = repo / 'src'
        nested.mkdir()
        (nested / '.git').mkdir()  # Missing HEAD, so it is not a marker.
        self.config(self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = "root"')
        self.assert_codex_text(nested, 'root')
        self.assert_codex_text(nested, None, ['-c', 'project_root_markers=[]'])
        (nested / '.project').touch()
        self.config('project_root_markers = [".project"]' + self.project_trust(repo))
        self.assert_codex_text(nested, None)
        self.assert_codex_text(nested, 'root', ['-c', 'project_root_markers=[".git"]'])

    def test_codex_home_directory_is_not_a_project_layer_even_through_a_symlink(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        (paths.CODEX / 'work.config.toml').write_text('developer_instructions = "profile"')
        (repo / '.codex').symlink_to(paths.CODEX)
        self.assert_codex_text(repo, 'profile', ['-p', 'work'])

    def test_codex_non_string_project_instructions_omit_override(self):
        repo = self.instruction_repo()
        self.config('developer_instructions = "home"' + self.project_trust(repo))
        self.instruction_layer(repo, 'developer_instructions = 1')
        self.assertEqual(project_launch.codex_args(None, ['resume'], cwd=repo), ['resume'])

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
                    text = tomllib.loads(args[1])['developer_instructions']
                    self.assertEqual(note() in text, enabled)
                    self.standing_instructions(args)
                    block = self.block(pid)
                    if pid:
                        self.assertIn('## Notes', text)
                        if enabled:
                            self.assertTrue(text.endswith('\n\n' + note() + '\n\n' + block))
                    else:
                        self.assertEqual(text, note() + '\n\n' + block if enabled else block)

    def test_codex_browser_keeps_plain_commands(self):
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
            args = project_launch.codex_args(None, [])
            self.assertFalse(any(arg.startswith('mcp_servers.') for arg in args))

    def test_codex_launch_without_project_and_fail_open(self):
        with mock.patch.dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x'), \
                mock.patch.object(project_launch.shutil, 'which', return_value='/fake/codex'), \
                mock.patch.object(project_launch, 'current_project', return_value=None), \
                mock.patch.object(project_launch.os, 'execv') as execute:
            project_launch.launch('codex', ['resume'])
            overrides = self.standing_instructions(execute.call_args.args[1][1:])
            self.assertEqual(overrides['developer_instructions'], note() + '\n\n' + self.block(None))
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
        env = dict(PATH=f'{bin_dir}:{os.environ["PATH"]}', TETHER_HOME=str(self.temp / 'tether'),
                   CODEX_HOME=str(self.temp / 'codex'), SHELL='/bin/sh', MOBILE_TMUX_CONFIG=str(self.temp / 'config'))
        self.private = private_tmux(self, 'tlaunch-', env)
        self.env = self.private.env
        for key in ('TETHER_LAUNCH', 'INVOCATION_ID', 'TETHER_CDP_URL'):
            self.env.pop(key, None)
        self.socket = self.private.socket
        self.tmux = self.private.tmux
        # The launcher reads its pane context. The tmux client needs only the socket.
        client = bin_dir / 'private-tmux'
        binary = shlex.quote(shutil.which('tmux'))
        client.write_text(f'#!/bin/sh\nunset TMUX TMUX_PANE\nexec {binary} -S {self.socket} -f /dev/null "$@"\n')
        client.chmod(0o755)
        self.env['MOBILE_TMUX_TMUX'] = str(client)
        (self.temp / 'codex').mkdir()
        (self.temp / 'codex' / 'config.toml').write_text('sandbox_mode = "workspace-write"\n')  # --add-dir needs a writable sandbox
        self.tmux('new-session', '-d', '-s', 'proj', '-c', str(self.temp),  # no real .codex folder above it
                  '-x', '120', '-y', '30', '/bin/sh').check_returncode()  # A login shell would reset PATH.

    def run_in_pane(self, command):
        target = self.temp / 'argv'
        target.unlink(missing_ok=True)
        self.tmux('send-keys', '-t', 'proj',
                  f'{command}; env -u TMUX -u TMUX_PANE tmux -S {self.socket} -f /dev/null wait-for -S done', 'Enter')
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
        self.assertEqual(argv[1], '--settings')
        self.assertEqual(json.loads(argv[2])['permissions']['additionalDirectories'], [str(self.temp / 'tether/projects' / pid)])
        self.assertEqual(argv[-2:], ['fix it', '1'])
        pane = self.tmux('display-message', '-p', '-t', 'proj', '#{@project_rev}').stdout.strip()
        self.assertEqual(pane, f'{pid}:0')
        argv = self.run_in_pane(f'{launcher} codex resume --last')
        folder = str(self.temp / 'tether/backlog')
        items = str(self.temp / 'tether/items')
        rem = str(self.temp / 'tether/reminders')
        self.assertEqual((argv[1], argv[3:]), ('-c', ['--add-dir', folder, '--add-dir', items, '--add-dir', rem, 'resume', '--last', '1']))  # the Notes part, even with no instructions
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
