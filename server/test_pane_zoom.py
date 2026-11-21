"""Zoom uses mocks or a private tmux server; no commands reach a user's terminal."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
import time
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pane_view
from test_tmux_isolation import private_tmux


class ZoomExit(unittest.TestCase):
    def run_view(self, zoom=True, exit_path='stdin', initial_zero=False):
        control = Mock()
        events, handlers = [], {}
        control.kill.side_effect = lambda: events.append('kill')
        control.wait.side_effect = lambda **_: events.append('wait')
        def leave(_):
            events.append('restore')
        def ready(*_):
            if initial_zero and not events:
                events.append('zero-size')
                return [], [], []
            if exit_path in (signal.SIGHUP, signal.SIGTERM):
                handlers[exit_path]()
            if exit_path == 'broken-pipe':
                raise BrokenPipeError()
            return ([control.stdout] if exit_path in ('control-eof', 'control-exit') else [0]), [], []
        def install(sig, handler):
            handlers[sig] = handler
        shot = None if exit_path == 'pane-ended' else ((0, 0, 1, 50, 20), [''] * 20, 0)
        with patch.object(pane_view.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '$1', '')), \
             patch.object(pane_view.subprocess, 'Popen', return_value=control), \
             patch.object(pane_view.os, 'isatty', return_value=False), \
             patch.object(pane_view.os, 'write'), \
             patch.object(pane_view.os, 'get_terminal_size', side_effect=[os.terminal_size((0, 0))] * 2 +
                          [os.terminal_size((50, 20))] * 2 if initial_zero else None,
                          return_value=os.terminal_size((50, 20))), \
             patch.object(pane_view.os, 'read', return_value=b'%exit\n' if exit_path == 'control-exit' else b''), \
             patch.object(pane_view.signal, 'signal', side_effect=install), \
             patch.object(pane_view, 'snapshot', return_value=shot), \
             patch.object(pane_view.select, 'select', side_effect=ready), \
             patch.object(pane_view, 'zoom_resize') as resize, \
             patch.object(pane_view, 'zoom_enter', return_value='view') as enter, \
             patch.object(pane_view, 'zoom_leave', side_effect=leave) as cleanup:
            try:
                pane_view.run('%7', zoom=zoom)
            except (SystemExit, BrokenPipeError):
                if exit_path not in (signal.SIGHUP, signal.SIGTERM, 'broken-pipe'):
                    raise
        return control, events, enter, cleanup, resize

    def test_exit_paths_detach_before_restore(self):
        for exit_path in ('stdin', 'pane-ended', 'control-eof', 'control-exit', 'broken-pipe', signal.SIGHUP, signal.SIGTERM):
            with self.subTest(exit_path=exit_path):
                control, events, enter, cleanup, resize = self.run_view(exit_path=exit_path)
                self.assertEqual(events, ['kill', 'wait', 'restore'])
                enter.assert_called_once_with('%7')
                cleanup.assert_called_once_with('view')
                resize.assert_called_once_with('view', 50, 20)
                control.stdin.write.assert_not_called()

    def test_non_zoom_never_resizes_or_saves_state(self):
        control, events, enter, cleanup, resize = self.run_view(zoom=False)
        self.assertEqual(events, ['kill', 'wait'])
        enter.assert_not_called()
        cleanup.assert_not_called()
        resize.assert_not_called()
        control.stdin.write.assert_not_called()

    def test_zero_initial_size_waits_before_private_window_resize(self):
        control, events, enter, cleanup, resize = self.run_view(initial_zero=True)
        self.assertEqual(events, ['zero-size', 'kill', 'wait', 'restore'])
        enter.assert_called_once_with('%7')
        cleanup.assert_called_once_with('view')
        resize.assert_called_once_with('view', 50, 20)
        control.stdin.write.assert_not_called()


class EmptyZoomCache(unittest.TestCase):
    def test_repair_without_state_never_calls_tmux(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pane_view.paths, 'CACHE', Path(folder)), \
             patch.object(pane_view, 'tmux') as tmux:
            pane_view.zoom_repair_all()
            tmux.assert_not_called()


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class IsolatedZoom(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = dict(HOME=self.folder.name, CLAUDE_CONFIG_DIR=self.folder.name + '/claude',
                   CODEX_HOME=self.folder.name + '/codex')
        self.private = private_tmux(self, 'tzoom-', env)
        self.socket = self.private.socket
        self.env = self.private.env
        self.tmux = self.private.tmux
        self.binary = shutil.which('tmux')
        self.tmux('new-session', '-d', '-s', 'zoom-test', '-x', '120', '-y', '40').check_returncode()
        self.addCleanup(self.stop_server)
        self.pane = self.tmux('display-message', '-p', '-t', 'zoom-test', '#{pane_id}').stdout.strip()
        self.home = self.tmux('display-message', '-p', '-t', self.pane, '#{window_id}').stdout.strip()
        self.tmux('split-window', '-h', '-t', self.pane).check_returncode()
        self.tmux('split-window', '-h', '-t', self.pane).check_returncode()
        self.other = self.tmux('display-message', '-p', '-t', self.home, '#{pane_id}').stdout.strip()
        for replacement in (patch.object(pane_view, 'tmux', side_effect=self.tmux),
                            patch.object(pane_view.paths, 'CACHE', Path(self.folder.name) / 'cache')):
            replacement.start()
            self.addCleanup(replacement.stop)

    def stop_server(self):
        # kill-server returns before the pane shells exit, and a shell can still write into HOME (the
        # temporary folder) while the folder cleanup runs. Wait until every pane process has gone.
        listed = self.tmux('list-panes', '-a', '-F', '#{pane_pid}').stdout.split()
        self.tmux('kill-server')
        deadline = time.monotonic() + 5
        for pid in map(int, listed):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f'tmux pane process {pid} did not exit after kill-server')
                os.sched_yield()

    def dimensions(self):
        return self.tmux('display-message', '-p', '-t', self.pane, '#{window_width} #{window_height}').stdout.strip()

    def test_detached_window_restores_dimensions_and_option_scope(self):
        for option in ('', 'manual', 'latest', 'smallest', 'largest'):
            with self.subTest(option=option):
                self.tmux('resize-window', '-t', self.pane, '-x', '120', '-y', '40').check_returncode()
                if option:
                    self.tmux('set-option', '-w', '-t', self.pane, 'window-size', option).check_returncode()
                else:
                    self.tmux('set-option', '-wu', '-t', self.pane, 'window-size').check_returncode()
                before = self.dimensions()
                global_size = self.tmux('show-options', '-gwv', 'window-size').stdout
                view = pane_view.zoom_enter(self.pane)
                pane_view.zoom_resize(view, 50, 20)
                pane_view.zoom_leave(view)
                self.assertEqual(self.dimensions(), before)
                self.assertEqual(self.tmux('show-options', '-wqv', '-t', self.pane, 'window-size').stdout.strip(), option)
                self.assertEqual(self.tmux('show-options', '-gwv', 'window-size').stdout, global_size)

    def home_layout(self):
        return self.tmux('list-panes', '-t', self.home, '-F',
                         '#{pane_id} #{pane_index} #{pane_width} #{pane_height} #{pane_active}').stdout

    def repair_from_fresh_process(self):
        code = '''
import subprocess, sys
from pathlib import Path
import pane_view
binary, socket, cache = sys.argv[1:]
pane_view.tmux = lambda *args: subprocess.run(
    [binary, '-N', '-S', socket, '-f', '/dev/null', *args], capture_output=True, text=True, timeout=5)
pane_view.paths.CACHE = Path(cache)
pane_view.zoom_repair_all()
'''
        subprocess.run([sys.executable, '-c', code, self.binary, self.socket, str(pane_view.paths.CACHE)],
                       cwd=Path(__file__).parent, env=self.env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=10, check=True)

    def test_five_narrow_panes_restore_exactly_after_exit_or_restart(self):
        program = 'timeout 120 tail -f /dev/null </dev/null'
        self.home, self.pane = self.tmux('new-window', '-d', '-t', 'zoom-test:', '-n', 'arranged',
                                        '-P', '-F', '#{window_id} #{pane_id}', program).stdout.split()
        right = self.pane
        panes = [right]
        for width in (105, 91, 46, 22):
            result = self.tmux('split-window', '-h', '-t', right, '-l', str(width), '-P', '-F', '#{pane_id}', program)
            result.check_returncode()
            right = result.stdout.strip()
            panes.append(right)
        self.assertEqual(self.tmux('list-panes', '-t', self.home, '-F', '#{pane_width}').stdout.split(),
                         ['14', '13', '44', '23', '22'])
        self.tmux('select-window', '-t', self.home).check_returncode()
        self.tmux('set-option', '-w', '-t', self.home, '@home_option', 'keep').check_returncode()
        for index, pane in enumerate(panes):
            self.tmux('select-pane', '-t', pane, '-T', f'title {index}').check_returncode()
            for option, value in (('@chat_name', f'chat {index}'), ('@chat_name_at', '123'),
                                  ('@chat_icon', 'terminal'), ('@custom', 'keep')):
                self.tmux('set-option', '-p', '-t', pane, option, value).check_returncode()
        def names():
            return (self.tmux('display-message', '-p', '-t', self.home,
                              '#{session_id} #{session_name} #{window_name} #{window_active}').stdout,
                    self.tmux('show-options', '-w', '-t', self.home).stdout,
                    [self.tmux('display-message', '-p', '-t', p, '#{pane_id} #{pane_title}').stdout +
                     self.tmux('show-options', '-p', '-t', p).stdout for p in panes])
        for active in (self.pane, panes[-1]):
            for restart in (False, True):
                with self.subTest(active=active, restart=restart):
                    self.tmux('select-pane', '-t', active).check_returncode()
                    before, labels = self.home_layout(), names()
                    layout = self.tmux('display-message', '-p', '-t', self.home, '#{window_layout}').stdout
                    view = pane_view.zoom_enter(self.pane)
                    self.addCleanup(pane_view.zoom_leave, view)
                    self.assertEqual(self.dimensions(), '100 30')
                    state = json.loads((view[0] / 'state.json').read_text())
                    for width, height, expected in ((40, 20, '100 30'), (150, 20, '150 30'),
                                                    (40, 60, '100 60'), (40, 20, '100 30')):
                        pane_view.zoom_resize(view, width, height)
                        self.assertEqual(self.dimensions(), expected)
                        self.assertEqual(self.home_layout().replace(state['placeholder'], self.pane), before)
                    if restart:
                        self.repair_from_fresh_process()  # A server restart preserves a live view's lease.
                        self.assertEqual(self.dimensions(), '100 30')
                        view[2].close()  # The browser/ttyd owner then disappears.
                        self.repair_from_fresh_process()
                    else:
                        pane_view.zoom_leave(view)
                    self.assertEqual(self.home_layout(), before)
                    self.assertEqual(names(), labels)
                    self.assertEqual(self.tmux('display-message', '-p', '-t', self.home,
                                              '#{window_layout}').stdout, layout)
                    self.assertFalse((view[0] / 'state.json').exists())

    def test_other_panes_and_home_slot_focus_stay_exact(self):
        for zoomed in (False, True):
            for active_target in (self.pane, self.other):
                with self.subTest(zoomed=zoomed, active=active_target):
                    self.tmux('select-pane', '-t', active_target).check_returncode()
                    if zoomed:
                        self.tmux('resize-pane', '-Z', '-t', active_target).check_returncode()
                    before = self.home_layout()
                    flags = self.tmux('display-message', '-p', '-t', self.home,
                                      '#{window_zoomed_flag} #{window_active}').stdout
                    view = pane_view.zoom_enter(self.pane)
                    self.addCleanup(pane_view.zoom_leave, view)
                    state = json.loads((view[0] / 'state.json').read_text())
                    pane_view.zoom_resize(view, 50, 20)
                    self.assertEqual(self.home_layout().replace(state['placeholder'], self.pane), before)
                    self.assertEqual(self.tmux('display-message', '-p', '-t', self.home,
                                              '#{window_zoomed_flag} #{window_active}').stdout, flags)
                    self.assertEqual(self.dimensions(), '100 30')
                    pane_view.zoom_leave(view)
                    self.assertEqual(self.home_layout(), before)
                    self.assertEqual(self.tmux('display-message', '-p', '-t', self.home,
                                              '#{window_zoomed_flag} #{window_active}').stdout, flags)
                    self.assertEqual(self.tmux('list-windows', '-t', 'zoom-test', '-F', '#{window_id}').stdout.split(), [self.home])
                    if zoomed:
                        self.tmux('resize-pane', '-Z', '-t', active_target).check_returncode()

    def test_views_share_window_and_latest_resize_wins(self):
        before = self.home_layout()
        first = pane_view.zoom_enter(self.pane)
        self.addCleanup(pane_view.zoom_leave, first)
        pane_view.zoom_resize(first, 50, 20)
        second = pane_view.zoom_enter(self.pane)
        self.addCleanup(pane_view.zoom_leave, second)
        self.assertEqual(first[:2], second[:2])
        self.assertEqual(len(self.tmux('list-windows', '-t', 'zoom-test', '-F', '#{window_id}').stdout.split()), 2)
        pane_view.zoom_resize(second, 140, 50)
        self.assertEqual(self.dimensions(), '140 50')
        pane_view.zoom_repair_all()  # Discovery must not disturb live owners.
        pane_view.zoom_leave(first)
        self.assertEqual(self.dimensions(), '140 50')
        pane_view.zoom_leave(second)
        self.assertEqual(self.home_layout(), before)

    def killed_view(self, while_live=None):
        code = '''
import subprocess, sys
from pathlib import Path
import pane_view
binary, socket, cache, pane = sys.argv[1:]
def tmux(*args):
    return subprocess.run([binary, '-N', '-f', '/dev/null', '-S', socket, *args],
                          capture_output=True, text=True, timeout=5)
pane_view.tmux = tmux
pane_view.paths.CACHE = Path(cache)
view = pane_view.zoom_enter(pane)
pane_view.zoom_resize(view, 50, 20)
print('ready', flush=True)
sys.stdin.buffer.read()
pane_view.zoom_leave(view)
'''
        process = subprocess.Popen([sys.executable, '-c', code, self.binary, self.socket,
                                    str(pane_view.paths.CACHE), self.pane], cwd=Path(__file__).parent, env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'Zoom child did not start')
            self.assertEqual(process.stdout.readline(), b'ready\n')
            self.assertEqual(self.dimensions(), '100 30')
            if while_live:
                while_live()
            process.kill()
            self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()

    def test_dead_owner_does_not_restore_a_live_view(self):
        before = self.home_layout()
        views = []
        self.killed_view(lambda: views.append(pane_view.zoom_enter(self.pane)))
        view, = views
        self.addCleanup(pane_view.zoom_leave, view)
        pane_view.zoom_repair_all()
        self.assertEqual(self.dimensions(), '100 30')
        pane_view.zoom_leave(view)
        self.assertEqual(self.home_layout(), before)

    def test_two_panes_in_one_home_have_separate_private_windows(self):
        before = self.home_layout()
        other = self.tmux('display-message', '-p', '-t', self.home, '#{pane_id}').stdout.strip()
        first = pane_view.zoom_enter(self.pane)
        self.addCleanup(pane_view.zoom_leave, first)
        second = pane_view.zoom_enter(other)
        self.addCleanup(pane_view.zoom_leave, second)
        self.assertNotEqual(first[:2], second[:2])
        pane_view.zoom_resize(first, 50, 20)
        pane_view.zoom_resize(second, 140, 50)
        self.assertEqual(self.dimensions(), '100 30')
        pane_view.zoom_leave(second)
        pane_view.zoom_leave(first)
        self.assertEqual(self.home_layout(), before)

    def test_partial_swap_failure_repairs_immediately(self):
        before = self.home_layout()
        def failed(*args):
            result = self.tmux(*args)
            if 'swap-pane' in args and args[args.index('-s') + 1] == self.pane:
                raise subprocess.TimeoutExpired('tmux', 5)
            return result
        with patch.object(pane_view, 'tmux', side_effect=failed):
            with self.assertRaises(subprocess.TimeoutExpired):
                pane_view.zoom_enter(self.pane)
        self.assertEqual(self.home_layout(), before)
        self.assertFalse(list(pane_view.paths.CACHE.glob('zoom/*/state.json')))

    def test_sigkill_repair_all_swaps_home(self):
        before = self.home_layout()
        self.killed_view()
        pane_view.zoom_repair_all()
        self.assertEqual(self.home_layout(), before)
        self.assertFalse(list(pane_view.paths.CACHE.glob('zoom/*/state.json')))

    def test_sigkill_next_zoom_repairs_before_new_swap(self):
        before = self.home_layout()
        self.killed_view()
        view = pane_view.zoom_enter(self.pane)
        pane_view.zoom_leave(view)
        self.assertEqual(self.home_layout(), before)

    def test_old_format_repairs_dimensions_options_zoom_and_focus(self):
        import hashlib
        for option in ('', 'manual', 'latest', 'smallest', 'largest'):
            with self.subTest(option=option):
                shown = self.tmux('display-message', '-p', '-t', self.pane,
                                  '#{socket_path}\n#{pid}\n#{window_id}').stdout.splitlines()
                folder = pane_view.paths.CACHE / 'zoom' / hashlib.sha256('\n'.join(shown).encode()).hexdigest()
                folder.mkdir(parents=True, exist_ok=True)
                active = self.tmux('display-message', '-p', '-t', self.home, '#{pane_id}').stdout.strip()
                before = self.home_layout()
                pane_view.zoom_save(folder, dict(pane=self.pane, width=120, height=40, window_size=option,
                                                zooming=True, active=active, owners=[]))
                self.tmux('resize-pane', '-Z', '-t', self.pane).check_returncode()
                self.tmux('resize-window', '-t', self.pane, '-x', '50', '-y', '20').check_returncode()
                pane_view.zoom_repair_all()
                self.assertEqual(self.dimensions(), '120 40')
                self.assertEqual(self.home_layout(), before)
                self.assertEqual(self.tmux('show-options', '-wqv', '-t', self.home, 'window-size').stdout.strip(), option)
                self.assertFalse((folder / 'state.json').exists())

    def test_discover_hides_placeholder_keeps_position_and_pane_options(self):
        import agents
        def run(arguments, **kwargs):
            if arguments[0] == pane_view.paths.TMUX:
                return self.tmux(*arguments[1:])
            return subprocess.run(arguments, **kwargs)
        before = agents.discover({}, run)
        self.tmux('set-option', '-p', '-t', self.pane, '@chat_name', 'My chat').check_returncode()
        self.tmux('set-option', '-p', '-t', self.pane, '@chat_icon', 'terminal').check_returncode()
        view = pane_view.zoom_enter(self.pane)
        self.addCleanup(pane_view.zoom_leave, view)
        after = agents.discover({}, run)
        self.assertEqual({e['id']: (e['tmux_session'], e['position'], e['label']) for e in after},
                         {e['id']: (e['tmux_session'], e['position'], e['label']) for e in before})
        entry = next(e for e in after if e['id'] == self.pane)
        self.assertEqual((entry['pane_name'], entry['pane_icon']), ('My chat', 'terminal'))
        # Exercise the real registry matcher with its pre-swap window ID.
        home = Path(self.folder.name) / 'home'
        registries = home / '.claude/sessions'
        registries.mkdir(parents=True)
        registry = dict(kind='interactive', pid=entry['pane_pid'], tmux='zoom-test:' + self.home + '.' + self.pane,
                        cwd='/work', sessionId='zoom-thread', nameSource='user', name='Claude name', nameSince=1)
        (registries / 'zoom.json').write_text(json.dumps(registry))
        with patch.object(agents, 'HOME', home):
            entry = next(e for e in agents.discover({}, run) if e['id'] == self.pane)
        self.assertEqual((entry['agent'], entry['thread']), ('claude', 'zoom-thread'))
        # A newer /rename hides the tmux name from the tab, but "Copy tmux pane name" still gets it.
        self.assertEqual((entry['agent_name'], entry['pane_name'], entry['tmux_pane_name']), ('Claude name', '', 'My chat'))

    def test_discover_repairs_killed_view_before_listing(self):
        import agents
        before = self.home_layout()
        self.killed_view()
        def run(arguments, **kwargs):
            return self.tmux(*arguments[1:]) if arguments[0] == pane_view.paths.TMUX else subprocess.run(arguments, **kwargs)
        entries = agents.discover({}, run)
        self.assertEqual(self.home_layout(), before)
        self.assertEqual(len(entries), 3)

    def test_old_format_repairs_window_after_pane_ends(self):
        import hashlib
        shown = self.tmux('display-message', '-p', '-t', self.pane,
                          '#{socket_path}\n#{pid}\n#{window_id}').stdout.splitlines()
        folder = pane_view.paths.CACHE / 'zoom' / hashlib.sha256('\n'.join(shown).encode()).hexdigest()
        folder.mkdir(parents=True)
        pane_view.zoom_save(folder, dict(pane=self.pane, width=120, height=40, window_size='',
                                        zooming=False, active=None, owners=[]))
        self.tmux('resize-window', '-t', self.home, '-x', '50', '-y', '20').check_returncode()
        self.tmux('kill-pane', '-t', self.pane).check_returncode()
        pane_view.zoom_repair_all()
        self.assertEqual(self.tmux('display-message', '-p', '-t', self.home, '#{window_width} #{window_height}').stdout.strip(), '120 40')
        self.assertFalse((folder / 'state.json').exists())

    def test_missing_placeholder_keeps_live_pane_visible(self):
        view = pane_view.zoom_enter(self.pane)
        state = json.loads((view[0] / 'state.json').read_text())
        self.tmux('kill-pane', '-t', state['placeholder']).check_returncode()
        view[2].close()
        pane_view.zoom_repair_all()
        self.assertEqual(self.tmux('display-message', '-p', '-t', self.pane, '#{automatic-rename}').stdout.strip(), '1')
        self.assertEqual(self.tmux('show-options', '-pqv', '-t', self.pane, '@chat_name').stdout.strip(), '')
        self.assertFalse((view[0] / 'state.json').exists())

    def test_missing_pane_removes_placeholder_and_private_window(self):
        view = pane_view.zoom_enter(self.pane)
        state = json.loads((view[0] / 'state.json').read_text())
        self.tmux('kill-pane', '-t', self.pane).check_returncode()
        view[2].close()
        pane_view.zoom_repair_all()
        self.assertEqual(self.tmux('display-message', '-p', '-t', state['placeholder'], '#{pane_id}').stdout.strip(), '')
        self.assertFalse((view[0] / 'state.json').exists())

    def test_restore_failure_keeps_record_for_retry(self):
        before = self.home_layout()
        view = pane_view.zoom_enter(self.pane)
        with patch.object(pane_view, 'zoom_restore', side_effect=subprocess.TimeoutExpired('tmux', 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                pane_view.zoom_leave(view)
        self.assertTrue((view[0] / 'state.json').exists())
        pane_view.zoom_repair_all()
        self.assertEqual(self.home_layout(), before)

    def test_control_client_ignores_size_for_whole_zoom(self):
        client = subprocess.Popen([self.binary, '-N', '-S', self.socket, '-f', '/dev/null',
                                   '-C', 'attach-session', '-f', 'ignore-size', '-t', 'zoom-test'],
                                  env=self.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Establish the fake client's size before arranging the fixture. Explicit
            # refresh-client -C can resize windows even with ignore-size; run() never sends it.
            client.stdin.write(b'refresh-client -C 40,20\nwait-for -S sized\n')
            client.stdin.flush()
            self.tmux('wait-for', 'sized').check_returncode()
            self.assertEqual(self.tmux('list-clients', '-F', '#{client_width}').stdout.strip(), '40')
            self.tmux('resize-window', '-t', self.home, '-x', '120', '-y', '40').check_returncode()
            before = self.home_layout()
            view = pane_view.zoom_enter(self.pane)
            self.addCleanup(pane_view.zoom_leave, view)
            pane_view.zoom_resize(view, 40, 20)
            self.assertIn('ignore-size', self.tmux('list-clients', '-F', '#{client_flags}').stdout)
            for aggressive in ('on', 'off'):
                self.tmux('set-option', '-w', '-t', view[1], 'aggressive-resize', aggressive).check_returncode()
                self.assertEqual(self.dimensions(), '100 30')
                self.assertEqual(self.tmux('show-options', '-wv', '-t', view[1], 'window-size').stdout.strip(), 'manual')
            state = json.loads((view[0] / 'state.json').read_text())
            self.assertEqual(self.home_layout().replace(state['placeholder'], self.pane), before)
            pane_view.zoom_leave(view)
            self.assertEqual(self.home_layout(), before)
        finally:
            client.kill()
            client.wait(timeout=5)
            client.stdin.close()


if __name__ == '__main__':
    unittest.main()
