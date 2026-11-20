"""Session rows share cached pane status without serializing different panes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

import server


class StatusParallelTests(unittest.TestCase):
    def setUp(self):
        self.entries = [{'id': f'%{index}', 'agent': 'claude'} for index in range(3)]
        self.enterContext(patch.object(server, 'STATUS_CACHE', {}))
        self.enterContext(patch.object(server.auto_resume, 'overlay', side_effect=lambda entry, status: status))
        self.enterContext(patch.object(server, 'track'))
        self.enterContext(patch.object(server, 'tab_folder', return_value='/work'))
        self.enterContext(patch.object(server.attention.STORE, 'ask', return_value=None))
        self.enterContext(patch.object(server.needs, 'summary', return_value=None))

    def test_rows_preserve_input_order_when_later_row_finishes_first(self):
        later_finished = threading.Event()

        def row(entry):
            if entry is self.entries[0]:
                self.assertTrue(later_finished.wait(timeout=2))
            else:
                later_finished.set()
            return {'id': entry['id']}

        with patch.object(server, 'session_row', side_effect=row):
            rows = server.session_rows(self.entries)
        self.assertEqual([row['id'] for row in rows], [entry['id'] for entry in self.entries])

    def test_different_panes_compute_at_the_same_time(self):
        together = threading.Barrier(2, timeout=2)

        def status(entry):
            together.wait()
            return {'state': 'Idle', 'approval': None}

        with patch.object(server, 'live_status', side_effect=status) as read:
            rows = server.session_rows(self.entries[:2])
        self.assertEqual([row['state'] for row in rows], ['Idle', 'Idle'])
        self.assertEqual(read.call_count, 2)

    def test_same_pane_computes_once_for_concurrent_callers(self):
        together = threading.Barrier(2, timeout=2)
        started = threading.Event()
        release = threading.Event()
        status = {'state': 'Idle', 'approval': None}

        def read_status(entry):
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return status

        def read():
            together.wait()
            return server.shared_status(self.entries[0])

        with patch.object(server, 'live_status', side_effect=read_status) as live_status, \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read) for _ in range(2)]
            try:
                self.assertTrue(started.wait(timeout=2))
            finally:
                release.set()
            self.assertEqual([future.result(timeout=2) for future in futures], [status, status])
        live_status.assert_called_once_with(self.entries[0])

    def test_live_status_error_leaves_other_rows_available(self):
        def status(entry):
            if entry is self.entries[1]:
                raise OSError('Pane closed')
            return {'state': 'Idle', 'approval': None}

        with patch.object(server, 'live_status', side_effect=status):
            rows = server.session_rows(self.entries)
        self.assertEqual([row['state'] for row in rows], ['Idle', 'Unavailable', 'Idle'])
        self.assertIsNone(rows[1]['approval'])

    def test_unexpected_row_errors_propagate(self):
        with patch.object(server, 'session_row', side_effect=RuntimeError('Unexpected row error')):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected row error'):
                server.session_rows(self.entries)


if __name__ == '__main__':
    unittest.main()
