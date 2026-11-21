"""A pane left in tmux copy mode (a mouse-wheel scroll) no longer blocks a send. Uses its own tmux server."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import time
import unittest

import terminal_input
from test_tmux_isolation import private_tmux


class Scrollback(unittest.TestCase):
    def test_leaves_copy_mode_only(self):
        private = private_tmux(self, 'tscroll-')
        tmux = private.tmux
        tmux('new-session', '-d', '-s', 't', 'bash', '--noprofile', '--norc').check_returncode()
        pane = tmux('display-message', '-p', '-t', 't', '#{pane_id}').stdout.strip()
        mode = lambda: tmux('display-message', '-p', '-t', pane, '#{pane_in_mode}').stdout.strip()
        terminal_input.leave_scrollback({'id': pane}, private.run)  # not in a mode: nothing happens
        self.assertEqual(mode(), '0')
        tmux('copy-mode', '-t', pane).check_returncode()
        self.assertEqual(mode(), '1')
        terminal_input.leave_scrollback({'id': pane}, private.run)
        time.sleep(.1)
        self.assertEqual(mode(), '0')


if __name__ == '__main__':
    unittest.main()
