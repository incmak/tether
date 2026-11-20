"""side_shells against a real tmux server in its own socket folder (TMUX_TMPDIR), never the user's tmux."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import functools
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock

import paths
import side_shells


class ShellListTests(unittest.TestCase):
    rows = ('%23\ttether-shells\t/projects\tbash\n'
            '%24\ttether-shells\t/other\t-zsh\n'
            '%25\ttether-shells\t/projects\tpython3\n'
            '%26\ttether-shells\t\tbtop\n')

    def fake_run(self, rows=None, missing=(), failed=()):
        def run(command, **kwargs):
            action = command[1]
            if action == 'list-panes':
                output = self.rows if rows is None else rows
                if command[-1] == side_shells.FORMAT:
                    output = '\n'.join(line.rsplit('\t', 1)[0] for line in output.splitlines())
                return subprocess.CompletedProcess(command, 0, output, '')
            pane = command[command.index('-t') + 1]
            if action == 'display-message':
                return subprocess.CompletedProcess(command, 0, 'work' if pane in missing else side_shells.SESSION, '')
            self.assertEqual(action, 'kill-window')
            return subprocess.CompletedProcess(command, int(pane in failed), '', 'Could not end ' + pane if pane in failed else '')
        return Mock(side_effect=run)

    def kills(self, run):
        return [call.args[0] for call in run.call_args_list if call.args[0][1] == 'kill-window']

    def test_list_shells_parses_commands_and_excludes_other_sessions(self):
        run = self.fake_run(self.rows + '%27\twork\t/projects\tsh\n')
        self.assertEqual(side_shells.list_shells(run), [
            {'pane': '%23', 'folder': '/projects', 'command': 'bash', 'idle': True, 'monitor': False},
            {'pane': '%24', 'folder': '/other', 'command': '-zsh', 'idle': True, 'monitor': False},
            {'pane': '%25', 'folder': '/projects', 'command': 'python3', 'idle': False, 'monitor': False},
            {'pane': '%26', 'folder': '', 'command': 'btop', 'idle': False, 'monitor': True},
        ])
        self.assertEqual(run.call_args.args[0], [paths.TMUX, 'list-panes', '-s', '-t', '=tether-shells', '-f',
                         '#{&&:#{==:#{@zoom_for},},#{!=:#{window_name},tether-zoom}}', '-F',
                         '#{pane_id}\t#{session_name}\t#{@tether_folder}\t#{pane_current_command}'])
        self.assertEqual(side_shells._panes(run), [('%23', '/projects'), ('%24', '/other'),
                                                  ('%25', '/projects'), ('%26', '')])

    def test_list_shells_returns_empty_when_session_is_absent(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'no session'))
        self.assertEqual(side_shells.list_shells(run), [])

    def test_only_one_login_prefix_is_removed(self):
        run = self.fake_run('%23\ttether-shells\t/projects\t--bash\n')
        self.assertFalse(side_shells.list_shells(run)[0]['idle'])

    def test_end_idle_shells_only(self):
        run = self.fake_run()
        self.assertEqual(side_shells.end_shells('idle', run), ['%23', '%24'])
        self.assertEqual(self.kills(run), [[paths.TMUX, 'kill-window', '-t', '%23'],
                                          [paths.TMUX, 'kill-window', '-t', '%24']])

    def test_end_all_shells_skips_a_pane_that_is_no_longer_owned(self):
        run = self.fake_run(missing=('%24',))
        self.assertEqual(side_shells.end_shells('all', run), ['%23', '%25', '%26'])
        self.assertEqual(self.kills(run), [[paths.TMUX, 'kill-window', '-t', pane]
                                          for pane in ('%23', '%25', '%26')])

    def test_end_all_shells(self):
        run = self.fake_run()
        self.assertEqual(side_shells.end_shells('all', run), ['%23', '%24', '%25', '%26'])
        self.assertEqual(len(self.kills(run)), 4)

    def test_end_shells_rejects_invalid_selection_before_listing(self):
        run = self.fake_run()
        with self.assertRaisesRegex(ValueError, '^Choose which shells to end\\.$'):
            side_shells.end_shells('bogus', run)
        run.assert_not_called()

    def test_end_shells_continues_after_failure_when_another_pane_ends(self):
        run = self.fake_run(failed=('%23',))
        self.assertEqual(side_shells.end_shells('idle', run), ['%24'])
        self.assertEqual(len(self.kills(run)), 2)

    def test_end_shells_collects_failures_when_no_pane_ends(self):
        run = self.fake_run(failed=('%23', '%24'))
        with self.assertRaisesRegex(RuntimeError, 'Could not end %23; Could not end %24'):
            side_shells.end_shells('idle', run)
        self.assertEqual(len(self.kills(run)), 2)

    def test_end_shells_returns_empty_when_nothing_can_end(self):
        for rows, missing in (('', ()), (self.rows, ('%23', '%24', '%25', '%26'))):
            with self.subTest(rows=rows):
                run = self.fake_run(rows, missing=missing)
                self.assertEqual(side_shells.end_shells('all', run), [])
                self.assertEqual(self.kills(run), [])


@unittest.skipUnless(shutil.which(paths.TMUX) or os.path.exists(paths.TMUX), 'tmux is not installed')
class SideShellTests(unittest.TestCase):
    def setUp(self):
        self.socket_dir = tempfile.mkdtemp(prefix='tss', dir='/tmp')  # short: long socket paths fail
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        env['TMUX_TMPDIR'] = self.socket_dir
        self.run_tmux = functools.partial(subprocess.run, env=env)
        self.folder = tempfile.mkdtemp(prefix='folder')
        self.other = tempfile.mkdtemp(prefix='other')
        # A user session that must stay untouched.
        self.tmux('new-session', '-d', '-s', 'work', '-c', self.other)
        self.user_pane = self.tmux('display-message', '-p', '-t', '=work:', '#{pane_id}').stdout.strip()

    def tearDown(self):
        self.tmux('kill-server')
        shutil.rmtree(self.socket_dir, ignore_errors=True)
        shutil.rmtree(self.folder, ignore_errors=True)
        shutil.rmtree(self.other, ignore_errors=True)

    def tmux(self, *args):
        return self.run_tmux([paths.TMUX, *args], capture_output=True, text=True, timeout=5)

    def sessions(self):
        return sorted(self.tmux('list-sessions', '-F', '#{session_name}').stdout.split())

    def test_creates_reuses_and_adds_shells_in_their_own_session(self):
        first = side_shells.open_shell(self.folder, run=self.run_tmux)
        self.assertRegex(first, r'^%\d+$')
        self.assertEqual(self.sessions(), ['tether-shells', 'work'])
        path = self.tmux('display-message', '-p', '-t', first, '#{pane_current_path}').stdout.strip()
        self.assertEqual(os.path.realpath(path), os.path.realpath(self.folder))
        self.assertEqual(side_shells.open_shell(self.folder, run=self.run_tmux), first)
        self.assertEqual(side_shells.open_shell(self.folder, pane=first, new=True, run=self.run_tmux), first)
        second = side_shells.open_shell(self.folder, new=True, run=self.run_tmux)
        self.assertNotEqual(second, first)
        third = side_shells.open_shell(self.other, run=self.run_tmux)
        self.assertNotIn(third, (first, second))
        windows = self.tmux('list-windows', '-t', '=tether-shells', '-F', '#{window_id}').stdout.split()
        self.assertEqual(len(windows), 3)
        self.assertEqual(self.tmux('list-windows', '-t', '=work', '-F', '#{window_id}').stdout.count('@'), 1)

    def test_a_dead_pane_or_bad_folder_falls_back(self):
        pane = side_shells.open_shell('relative/path', pane='%999', run=self.run_tmux)
        path = self.tmux('display-message', '-p', '-t', pane, '#{@tether_folder}').stdout.strip()
        self.assertEqual(path, str(paths.HOME))

    def test_end_kills_only_its_own_shell(self):
        first = side_shells.open_shell(self.folder, run=self.run_tmux)
        second = side_shells.open_shell(self.folder, new=True, run=self.run_tmux)
        side_shells.end_shell(first, run=self.run_tmux)
        self.assertFalse(side_shells.owns(first, run=self.run_tmux))
        self.assertTrue(side_shells.owns(second, run=self.run_tmux))
        with self.assertRaises(ValueError):
            side_shells.end_shell(self.user_pane, run=self.run_tmux)
        with self.assertRaises(ValueError):
            side_shells.end_shell('%1; kill-server', run=self.run_tmux)
        self.assertIn('work', self.sessions())
        side_shells.end_shell(second, run=self.run_tmux)
        self.assertEqual(self.sessions(), ['work'])

    def test_discovery_hides_the_shell_session(self):
        import agents
        side_shells.open_shell(self.folder, run=self.run_tmux)
        listed = self.tmux('list-panes', '-a', '-F', '#{session_name}').stdout.split()
        self.assertIn('tether-shells', listed)
        entries = agents.discover({'thread': None, 'transcript': None}, self.run_tmux)
        self.assertEqual({entry['label'] for entry in entries}, {'work'})

    def test_discovery_reads_the_pane_icon_and_drops_unknown_text(self):
        import agents
        self.tmux('set-option', '-p', '-t', self.user_pane, '@chat_name', 'api')
        self.tmux('set-option', '-p', '-t', self.user_pane, '@chat_icon', 'bug')
        entry, = agents.discover({'thread': None, 'transcript': None}, self.run_tmux)
        self.assertEqual((entry['pane_name'], entry['pane_icon']), ('api', 'bug'))
        self.assertTrue(os.path.isdir(entry['cwd']))  # the icon field does not leak into the folder
        self.tmux('set-option', '-p', '-t', self.user_pane, '@chat_icon', 'Not An Icon')
        entry, = agents.discover({'thread': None, 'transcript': None}, self.run_tmux)
        self.assertEqual(entry['pane_icon'], '')

    def test_folder_names_are_not_tmux_formats(self):
        # tmux expands formats in -c and -n; #(...) would run a command. The shell must start in the folder as named.
        marker = os.path.join(self.folder, 'ran')
        odd = os.path.join(self.folder, 'a#{session_name}#(touch ' + marker + ')')
        os.makedirs(odd)
        pane = side_shells.open_shell(odd, run=self.run_tmux)
        second = side_shells.open_shell(odd, new=True, run=self.run_tmux)
        for item in (pane, second):
            # Right after the window opens, tmux can still report its own folder. Wait for the shell's folder.
            deadline = time.monotonic() + 5
            while True:
                path = self.tmux('display-message', '-p', '-t', item, '#{pane_current_path}').stdout.strip()
                if os.path.realpath(path) == os.path.realpath(odd) or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            self.assertEqual(os.path.realpath(path), os.path.realpath(odd))
        names = self.tmux('list-windows', '-t', '=tether-shells', '-F', '#{window_name}').stdout.split('\n')
        self.assertIn(os.path.basename(odd), names)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(side_shells.open_shell(odd, run=self.run_tmux), pane)


class FolderInRootTests(unittest.TestCase):
    """The "Terminal here" folder: inside the files root, a real folder, not secret."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='root'))
        self.out = tempfile.mkdtemp(prefix='out')
        os.makedirs(os.path.join(self.root, 'proj', 'src'))
        os.makedirs(os.path.join(self.root, '.ssh'))
        open(os.path.join(self.root, 'proj', 'a.txt'), 'w').close()
        os.symlink(self.out, os.path.join(self.root, 'escape'))
        os.symlink(os.path.join(self.root, 'proj'), os.path.join(self.root, 'inner'))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_accepts_folders_inside_the_root(self):
        self.assertEqual(side_shells.folder_in_root('~', self.root), self.root)
        self.assertEqual(side_shells.folder_in_root('~/proj/src', self.root), os.path.join(self.root, 'proj', 'src'))
        self.assertEqual(side_shells.folder_in_root(os.path.join(self.root, 'proj'), self.root), os.path.join(self.root, 'proj'))
        self.assertEqual(side_shells.folder_in_root('~/inner', self.root), os.path.join(self.root, 'proj'))

    def test_refuses_everything_else(self):
        import files
        for text, code in (('~/..', 'outside'), ('~/proj/../../etc', 'outside'), ('/etc', 'outside'), ('~/escape', 'outside'),
                           ('~/.ssh', 'secret'), ('~/proj/a.txt', 'invalid'), ('~/nope', 'missing'), ('proj', 'invalid'),
                           ('', 'invalid'), (None, 'invalid'), (['~'], 'invalid'), ('~/proj\0x', 'invalid')):
            with self.subTest(text=text):
                with self.assertRaises(files.FilesError) as caught:
                    side_shells.folder_in_root(text, self.root)
                self.assertEqual(caught.exception.code, code)
                self.assertIsInstance(caught.exception, ValueError)


if __name__ == '__main__':
    unittest.main()
