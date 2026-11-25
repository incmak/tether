"""Sizing uses fake tmux calls; integration proof uses a private socket only."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import subprocess
import unittest
from unittest.mock import patch

import window_size


class WindowSizeTests(unittest.TestCase):
    def test_target_counts_control_clients_but_not_ignore_size(self):
        self.assertEqual(window_size.target([], (120, 40)), (120, 40))
        self.assertEqual(window_size.target([(100, 5, ''), (200, 30, 'control-mode'),
                                             (80, 60, ''), (900, 900, 'control-mode,ignore-size')],
                                            (120, 40)), (200, 60))

    def test_apply_only_owns_agent_windows_and_only_changes_sizes_when_needed(self):
        calls = []
        registry = '%1,%2,%3'
        rows = [
            ['$1', '@1', '100', '5', 'latest', '', '', '%1|', registry],
            ['$1', '@2', '120', '40', 'manual', '1', '', '%2|', registry],
            ['$1', '@3', '50', '20', 'manual', '', '1', '%3|', registry],
            ['$1', '@4', '100', '5', 'latest', '', '', '%4|', registry],
        ]
        def run(argv, **kwargs):
            calls.append(argv[1:])
            if argv[1] == 'list-windows':
                out = '\n'.join('\t'.join(row) for row in rows)
            elif argv[1] == 'list-clients':
                out = '$1\t100\t5\tcontrol-mode\t100\t\n$1\t999\t999\tignore-size\t101\t\n'
            else:
                out = ''
                rows[0][2:6] = ['120', '40', 'manual', '1']
            return subprocess.CompletedProcess(argv, 0, out, '')
        self.assertTrue(window_size.apply(run))
        writes = calls[2:]
        self.assertEqual(writes, [['set-option', '-w', '-t', '@1', 'window-size', 'manual', ';',
                                   'set-option', '-w', '-t', '@1', window_size.OWNED, '1', ';',
                                   'resize-window', '-t', '@1', '-x', '120', '-y', '40']])
        calls.clear()
        self.assertTrue(window_size.apply(run))
        self.assertEqual([c[0] for c in calls], ['list-windows', 'list-clients'])

    def test_control_height_registry_and_linked_sessions_use_each_dimension(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv[1:])
            if argv[1] == 'list-windows':
                out = ('$1\t@1\t120\t40\tmanual\t1\t\t%1|\t%1\n'
                       '$2\t@1\t120\t40\tmanual\t1\t\t%1|\t%1\n')
            elif argv[1] == 'list-clients':
                out = ('$1\t200\t\tcontrol-mode\t123\t|123=200x30|\n'
                       '$2\t80\t60\t\t124\t|123=200x30|\n'
                       '$2\t999\t\tcontrol-mode,ignore-size\t125\t|123=200x30|\n')
            else:
                out = ''
            return subprocess.CompletedProcess(argv, 0, out, '')
        self.assertTrue(window_size.apply(run))
        self.assertEqual(calls[-1], ['resize-window', '-t', '@1', '-x', '200', '-y', '60'])

    def test_discovery_publishes_registry_and_releases_former_agent_window(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv[1:])
            out = ('$1\t@1\t120\t40\tmanual\t1\t\t%1|\t%1\n'
                   if argv[1] == 'list-windows' else '')
            return subprocess.CompletedProcess(argv, 0, out, '')
        self.assertTrue(window_size.apply(run, entries=[{'id': '%1', 'agent': 'shell'}]))
        self.assertEqual(calls[-1], ['set-option', '-g', window_size.REGISTRY, '', ';',
                                    'set-option', '-wu', '-t', '@1', 'window-size', ';',
                                    'set-option', '-wu', '-t', '@1', window_size.OWNED])

    def test_uninstall_removes_only_owned_hooks_and_window_options(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv[1:])
            if argv[1] == 'show-hooks':
                hook = argv[-1]
                out = f'{hook}[0] set-option -g @user_hook yes\n{hook}[3] run-shell {window_size.MARK}\n'
            elif argv[1] == 'list-windows':
                out = '@1\t1\n@2\t\n@3\t\n'
            else:
                out = ''
            return subprocess.CompletedProcess(argv, 0, out, '')
        window_size.uninstall(run)
        removed = [call for call in calls if call[0] == 'set-hook']
        self.assertEqual(removed, [['set-hook', '-gu', f'{hook}[3]'] for hook in window_size.HOOKS])
        writes = [call for call in calls if call[0] == 'set-option']
        self.assertEqual(writes, [
            ['set-option', '-wu', '-t', '@1', 'window-size', ';',
             'set-option', '-wu', '-t', '@1', window_size.OWNED],
            ['set-option', '-gu', window_size.REGISTRY],
            ['set-option', '-gu', window_size.CONTROL]])

    def test_install_keeps_user_hook_and_reuses_its_own_slot(self):
        slots, calls = {}, []
        def run(argv, **kwargs):
            calls.append(argv[1:])
            if argv[1] == '-V':
                out = 'tmux 3.4\n'
            elif argv[1] == 'show-hooks':
                hook = argv[-1]
                out = f'{hook}[0] set-option -g @user_hook yes\n'
                if hook in slots:
                    out += f'{hook}[1] {slots[hook]}\n'
            else:
                out = ''
                for index, arg in enumerate(argv):
                    if arg == 'set-hook':
                        slot, command = argv[index + 2:index + 4]
                        self.assertTrue(slot.endswith('[1]'))
                        slots[slot.split('[')[0]] = command
            return subprocess.CompletedProcess(argv, 0, out, '')
        for _ in range(2):
            self.assertTrue(window_size.install(run))
        self.assertEqual(set(slots), set(window_size.HOOKS))
        for command in slots.values():
            self.assertIn('run-shell -b', command)
            self.assertIn(window_size.MARK, command)
            self.assertIn('apply --socket #{q:socket_path}', command)
        with patch.object(window_size.logging, 'warning') as warning:
            old = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, 'tmux 3.1c\n', '')
            self.assertFalse(window_size.install(old))
            self.assertFalse(window_size.install(old))
            warning.assert_called_once()


if __name__ == '__main__':
    unittest.main()
