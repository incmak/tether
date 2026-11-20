"""tmux_names hooks against a real tmux server on its own socket (-L, in its own TMUX_TMPDIR), never the user's tmux."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import agents
import paths
import tmux_names


@unittest.skipUnless(shutil.which(paths.TMUX) or os.path.exists(paths.TMUX), 'tmux is not installed')
class TmuxNameHookTests(unittest.TestCase):
    def setUp(self):
        self.socket_dir = tempfile.mkdtemp(prefix='tnh', dir='/tmp')  # short: long socket paths fail
        self.env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        self.env['TMUX_TMPDIR'] = self.socket_dir
        # A plain /bin/sh, never the user's shell: an interactive zsh reads ~/.zshrc and locks the real
        # ~/.zsh_history before it runs a typed line, and a held lock delayed that past the waits below (B148).
        self.env['SHELL'] = '/bin/sh'
        self.tmux('new-session', '-d', '-s', 'work', 'exec /bin/sh')  # exec: no outer sh -c keeps the foreground
        self.pane = self.tmux('display-message', '-p', '-t', '=work:', '#{pane_id}').stdout.strip()

    def tearDown(self):
        self.tmux('kill-server')  # only this test socket's server
        shutil.rmtree(self.socket_dir, ignore_errors=True)

    def run_tmux(self, command, **kwargs):
        """subprocess.run for code under test: every tmux call goes to the test socket."""
        if command and command[0] == paths.TMUX:
            command = [paths.TMUX, '-L', 'tether-test', '-f', '/dev/null', *command[1:]]  # no ~/.tmux.conf, no plugins
        return subprocess.run(command, env=self.env, **kwargs)

    def tmux(self, *args):
        return self.run_tmux([paths.TMUX, *args], capture_output=True, text=True, timeout=5)

    def option(self, name, pane=None):
        return self.tmux('display-message', '-p', '-t', pane or self.pane, '#{' + name + '}').stdout.strip()

    def wait_for(self, check, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.05)
        return check()

    def settle(self):
        """Hooks run in tmux's queue and the stamp in a shell job: give both time to finish."""
        self.tmux('display-message', '-p', '')
        time.sleep(0.6)

    def tether_names(self, name, pane=None):
        """What Tether does on a rename (server.py pane-name + name_in_tmux): the option first, then the window."""
        pane = pane or self.pane
        self.tmux('set-option', '-p', '-t', pane, '@chat_name', name, ';', 'set-option', '-p', '-t', pane, '@chat_name_at', '123')
        self.tmux('rename-window', '-t', pane, name, ';', 'set-option', '-w', '-t', pane, 'automatic-rename', 'off')

    def test_rename_window_imports_the_name_and_discovery_shows_it(self):
        self.assertTrue(tmux_names.install(run=self.run_tmux))
        before = int(time.time()) * 1000
        self.tmux('rename-window', '-t', self.pane, 'api, "review" #1')
        self.assertTrue(self.wait_for(lambda: self.option('@chat_name_at').isdigit()))
        self.assertEqual(self.option('@chat_name'), 'api, "review" #1')
        self.assertGreaterEqual(int(self.option('@chat_name_at')), before)
        entry = next(e for e in agents.discover({'thread': None}, self.run_tmux) if e['id'] == self.pane)
        self.assertEqual(entry['pane_name'], 'api, "review" #1')
        self.assertGreaterEqual(entry['pane_named_at'], before)

    def test_automatic_rename_never_overwrites_the_name(self):
        self.assertTrue(tmux_names.install(run=self.run_tmux))
        self.tether_names('logs')
        # Record the event after Tether's hook has handled it.
        hook = self.tmux('set-hook', '-ag', 'window-renamed',
                         "set-option -p -F @test_renamed '#{hook_window_name}'")
        self.assertEqual(hook.returncode, 0, hook.stderr)
        for name in ('shell', 'worker'):
            with self.subTest(window_name=name):
                # Setting automatic-rename on makes tmux recheck the active pane without pane output.
                rename = self.tmux('set-option', '-w', '-t', self.pane, 'automatic-rename-format', name,
                                   ';', 'set-option', '-w', '-t', self.pane, 'automatic-rename', 'on')
                self.assertEqual(rename.returncode, 0, rename.stderr)
                self.assertTrue(self.wait_for(lambda: self.option('@test_renamed') == name),
                                f'window-renamed hook did not run for {name!r}')
                self.assertEqual(self.option('window_name'), name)
                self.assertEqual((self.option('@chat_name'), self.option('@chat_name_at')), ('logs', '123'))

    def test_tethers_own_rename_is_not_imported_again(self):
        tmux_names.install(run=self.run_tmux)
        for name in ('build', 'cost $5', 'a\\b'):  # special names stay tmux-only even when tmux escapes them
            self.tether_names(name)
            self.settle()
            self.assertEqual((self.option('@chat_name'), self.option('@chat_name_at')), (name, '123'))
        self.tmux('rename-window', '-t', self.pane, 'user $name')  # a user name with "$" stays tmux-only
        self.settle()
        self.assertEqual(self.option('@chat_name'), 'a\\b')

    def test_a_window_with_several_panes_is_left_alone(self):
        tmux_names.install(run=self.run_tmux)
        second = self.tmux('split-window', '-d', '-t', self.pane, '-P', '-F', '#{pane_id}').stdout.strip()
        self.tmux('rename-window', '-t', self.pane, 'shared')
        self.settle()
        self.assertEqual((self.option('@chat_name'), self.option('@chat_name', second)), ('', ''))

    def test_unescaped_dollar_name_is_not_imported(self):
        # Supply a literal name to exercise newer tmux's unescaped dollar names on older tmux too.
        self.tether_names('keep this')
        result = self.run_tmux([paths.TMUX, 'source-file', '-'],
                               input=tmux_names._command('user $name', []) + '\n',
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settle()
        self.assertEqual((self.option('@chat_name'), self.option('@chat_name_at')), ('keep this', '123'))

    def test_session_rename_names_a_pane_alone_in_its_session(self):
        tmux_names.install(run=self.run_tmux)
        self.tmux('rename-session', '-t', '=work', 'triage')
        self.assertTrue(self.wait_for(lambda: self.option('@chat_name_at').isdigit()))
        self.assertEqual(self.option('@chat_name'), 'triage')
        self.tmux('new-window', '-d', '-t', '=triage:')  # now two windows: whose name would it be?
        self.tmux('set-option', '-p', '-t', self.pane, '@chat_name_at', '123')
        self.tmux('rename-session', '-t', '=triage', 'later')
        self.settle()
        self.assertEqual((self.option('@chat_name'), self.option('@chat_name_at')), ('triage', '123'))

    def test_install_is_idempotent_and_keeps_user_hooks(self):
        self.tmux('set-hook', '-g', 'window-renamed[0]', 'set-option -p @user_saw yes')
        self.tmux('set-hook', '-g', 'session-renamed[0]', 'set-option -g @user_session yes')
        for _ in range(3):
            self.assertTrue(tmux_names.install(run=self.run_tmux))
        for hook in ('window-renamed', 'session-renamed'):
            lines = self.tmux('show-hooks', '-g', hook).stdout.splitlines()
            self.assertEqual(len(lines), 2, lines)
            self.assertNotIn(tmux_names.MARK, lines[0])  # the user's slot 0 is untouched
            self.assertIn(tmux_names.MARK, lines[1])
        self.tmux('rename-window', '-t', self.pane, 'both')
        self.assertTrue(self.wait_for(lambda: self.option('@chat_name') == 'both'))
        self.assertEqual(self.option('@user_saw'), 'yes')  # the user's hooks still run
        self.tmux('rename-session', '-t', '=work', 'renamed')
        self.assertTrue(self.wait_for(lambda: self.tmux('show-options', '-gv', '@user_session').stdout.strip() == 'yes'))

    def test_install_without_a_tmux_server(self):
        self.tmux('kill-server')
        self.assertFalse(tmux_names.install(run=self.run_tmux))


if __name__ == '__main__':
    unittest.main()
