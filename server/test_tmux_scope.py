import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import unittest
from unittest import mock

import paths


class TmuxScopeTest(unittest.TestCase):
    def test_service_starts_tmux_in_its_own_scope(self):
        with mock.patch.object(paths, 'SCOPE_TMUX', True), mock.patch.object(paths, 'SYSTEMD_RUN', '/usr/bin/systemd-run'):
            self.assertEqual(paths.tmux_new_session(), ['/usr/bin/systemd-run', '--user', '--scope', '--quiet', '--collect', paths.TMUX])

    def test_outside_a_service_plain_tmux(self):
        with mock.patch.object(paths, 'SCOPE_TMUX', False):
            self.assertEqual(paths.tmux_new_session(), [paths.TMUX])


if __name__ == '__main__':
    unittest.main()
