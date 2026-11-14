"""task_process against a fake process table; no real process is signalled."""
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

import task_process


class FakeProcs:
    def __init__(self):
        self.procs = {}

    def add(self, pid, *, parent=100, group=None, args=('zsh', '-c', 'x'), fds=(), start=1000.0, uid=None):
        self.procs[pid] = {'parent': parent, 'group': pid if group is None else group, 'args': [a.encode() for a in args],
                           'fds': list(fds), 'start': start, 'uid': os.getuid() if uid is None else uid, 'state': 'S'}

    def patches(self, case):
        module = task_process.procinfo
        case.enterContext(patch.object(module, 'pids', side_effect=lambda: list(self.procs)))
        case.enterContext(patch.object(module, 'parent', side_effect=lambda pid: self.procs[pid]['parent']))
        case.enterContext(patch.object(module, 'uid', side_effect=lambda pid: self.procs[pid]['uid']))
        case.enterContext(patch.object(module, 'argv', side_effect=lambda pid: self.procs[pid]['args']))
        case.enterContext(patch.object(module, 'fd_targets', side_effect=lambda pid: [
            (str(index), target) for index, target in enumerate(self.procs[pid]['fds'])]))
        case.enterContext(patch.object(module, 'start_ticks', side_effect=lambda pid: str(self.procs[pid]['start'])))
        case.enterContext(patch.object(module, 'start_ms', side_effect=lambda ticks: float(ticks) * 1000))
        case.enterContext(patch.object(module, 'stat_fields', side_effect=lambda pid: [self.procs[pid]['state']]))
        case.enterContext(patch.object(task_process.os, 'getpgid', side_effect=lambda pid: self.procs[pid]['group']))


def wrapped(command):
    return ('/usr/bin/zsh', '-c', f'source snap.sh || true && eval {task_process.quoted(command)} < /dev/null && pwd -P')


class FindTests(unittest.TestCase):
    def setUp(self):
        self.procs = FakeProcs()
        self.procs.patches(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.output = str(Path(folder.name) / 'b1.output')
        Path(self.output).write_text('')

    def test_shell_is_named_by_its_output_file_even_with_a_same_command_twin(self):
        self.procs.add(200, args=wrapped('sleep 1600'), fds=[self.output])
        self.procs.add(201, args=wrapped('sleep 1600'), fds=['/other.output'])
        self.procs.add(202, parent=999, fds=[self.output])  # not a child of the owner
        self.procs.add(203, group=100, fds=[self.output])  # not a group leader
        self.assertEqual(task_process.find(100, {'kind': 'shell', 'command': 'sleep 1600'}, self.output, 1000), (200, '1000.0'))

    def test_two_processes_on_one_output_are_not_exact(self):
        self.procs.add(200, fds=[self.output])
        self.procs.add(201, fds=[self.output])
        self.assertIsNone(task_process.find(100, {'kind': 'shell', 'command': 'x'}, self.output, 1000))

    def test_shell_without_its_output_file_is_never_matched_by_command(self):
        self.procs.add(200, args=wrapped('sleep 1600'))
        self.assertIsNone(task_process.find(100, {'kind': 'shell', 'command': 'sleep 1600'}, self.output, 1000))

    def test_other_user_process_is_ignored(self):
        self.procs.add(200, fds=[self.output], uid=os.getuid() + 1)
        self.assertIsNone(task_process.find(100, {'kind': 'shell', 'command': 'x'}, self.output, 1000))

    def test_monitor_matches_quoted_command_and_start_window(self):
        command = "tail -f log | grep 'it''s'"
        self.procs.add(300, args=wrapped(command), start=995)
        task = {'kind': 'monitor', 'command': command}
        self.assertEqual(task_process.find(100, task, None, 1000).pid, 300)
        self.assertIsNone(task_process.find(100, task, None, 1000 + task_process.START_BEFORE + 10))
        self.assertIsNone(task_process.find(100, task, None, 995 - task_process.START_AFTER - 1))
        self.assertIsNone(task_process.find(100, {'kind': 'monitor', 'command': 'tail -f log'}, None, 1000))

    def test_two_matching_monitors_are_not_exact(self):
        self.procs.add(300, args=wrapped('watch it'), start=999)
        self.procs.add(301, args=wrapped('watch it'), start=999.5)
        self.assertIsNone(task_process.find(100, {'kind': 'monitor', 'command': 'watch it'}, None, 1000))

    def test_quoting_matches_posix_single_quotes(self):
        self.assertEqual(task_process.quoted("a'b"), "'a'\\''b'")


class StopTests(unittest.TestCase):
    def setUp(self):
        self.procs = FakeProcs()
        self.procs.patches(self)
        self.procs.add(200, start=1000)
        self.sent = []

    def kill(self, dies_on):
        def send(pid, sig):
            self.sent.append(sig)
            if sig in dies_on:
                del self.procs.procs[pid]
        return send

    def process(self):
        return task_process.Process(200, '1000')

    def test_sigterm_stops_group(self):
        self.assertTrue(task_process.stop(self.process(), sleep=lambda _: None, kill=self.kill({signal.SIGTERM})))
        self.assertEqual(self.sent, [signal.SIGTERM])

    def test_sigkill_after_three_seconds(self):
        self.assertTrue(task_process.stop(self.process(), sleep=lambda _: None, kill=self.kill({signal.SIGKILL})))
        self.assertEqual(self.sent, [signal.SIGTERM, signal.SIGKILL])

    def test_zombie_counts_as_gone_and_reused_pid_is_not_signalled_twice(self):
        def send(pid, sig):
            self.sent.append(sig)
            self.procs.procs[pid]['start'] = 5000  # pid reused by a new process
        self.assertTrue(task_process.stop(self.process(), sleep=lambda _: None, kill=send))
        self.assertEqual(self.sent, [signal.SIGTERM])

    def test_pid_reused_between_find_and_stop_gets_no_signal(self):
        found = task_process.Process(200, '1000')
        self.procs.add(200, start=2000)  # The task exited; a new process now has pid 200.
        self.assertTrue(task_process.stop(found, sleep=lambda _: None, kill=self.kill({signal.SIGTERM})))
        self.assertEqual(self.sent, [])

    def test_pid_that_left_its_group_gets_no_signal(self):
        self.procs.procs[200]['group'] = 100
        task_process.stop(self.process(), sleep=lambda _: None, kill=self.kill({signal.SIGTERM}))
        self.assertEqual(self.sent, [])

    def test_find_refuses_a_pid_reused_during_its_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / 'b1.output')
            self.procs.add(200, fds=[output], start=1000)
            real = task_process._writes_to

            def reused(pid, path):
                self.procs.procs[pid]['start'] = 2000
                return real(pid, path)
            with patch.object(task_process, '_writes_to', side_effect=reused):
                self.assertIsNone(task_process.find(100, {'kind': 'shell', 'command': 'x'}, output, 1000))

    def test_unkillable_reports_false(self):
        self.assertFalse(task_process.stop(self.process(), sleep=lambda _: None, kill=self.kill(set())))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(task_process, 'LEDGER', Path(folder.name) / 'cache' / 'task-stops.json'))

    def test_record_read_and_bound(self):
        self.assertEqual(task_process.stopped(), frozenset())
        task_process.record('a/b1')
        self.assertIn('a/b1', task_process.stopped())
        with patch.object(task_process, 'LEDGER_LIMIT', 3):
            for index in range(5):
                task_process.record(f'k{index}')
        self.assertEqual(len(json.loads(task_process.LEDGER.read_text())), 3)

    def test_corrupt_ledger_reads_empty_and_is_rewritten(self):
        task_process.LEDGER.parent.mkdir(parents=True)
        task_process.LEDGER.write_text('{not json')
        self.assertEqual(task_process.stopped(), frozenset())
        task_process.record('x')
        self.assertEqual(set(task_process.stopped()), {'x'})


if __name__ == '__main__':
    unittest.main()
