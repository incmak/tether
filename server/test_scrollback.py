"""A pane left in tmux copy mode (a mouse-wheel scroll) no longer blocks a send. Uses its own tmux server."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import paths
import terminal_input


class Scrollback(unittest.TestCase):
    def test_leaves_copy_mode_only(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            socket = str(Path(folder) / 's')
            def run(argv, **kwargs):
                return subprocess.run(['tmux', '-S', socket, *argv[1:]], **kwargs)
            run([paths.TMUX, 'new-session', '-d', '-s', 't', 'bash', '--noprofile', '--norc'], check=True)
            try:
                pane = run([paths.TMUX, 'display-message', '-p', '-t', 't', '#{pane_id}'], capture_output=True, text=True).stdout.strip()
                mode = lambda: run([paths.TMUX, 'display-message', '-p', '-t', pane, '#{pane_in_mode}'], capture_output=True, text=True).stdout.strip()
                terminal_input.leave_scrollback({'id': pane}, run)  # not in a mode: nothing happens
                self.assertEqual(mode(), '0')
                run([paths.TMUX, 'copy-mode', '-t', pane], check=True)
                self.assertEqual(mode(), '1')
                terminal_input.leave_scrollback({'id': pane}, run)
                time.sleep(.1)
                self.assertEqual(mode(), '0')
            finally:
                run([paths.TMUX, 'kill-server'], capture_output=True)


if __name__ == '__main__':
    unittest.main()
