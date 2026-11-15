"""Process readers against a small fake proc tree."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import procinfo
from own_time import own_time


class ProcinfoTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.proc = Path(folder.name)
        self.pid = self.proc / '42'
        (self.pid / 'fd').mkdir(parents=True)
        (self.proc / 'net').mkdir()
        self.fields = ['S', '7', '42', '0', '0', '99'] + ['0'] * 13 + ['12345']
        (self.pid / 'stat').write_text('42 (name with ) and spaces) ' + ' '.join(self.fields))
        (self.pid / 'comm').write_text('name with ) and spaces\n')
        (self.pid / 'cmdline').write_bytes(b'codex\0\xff\0\0')
        (self.pid / 'environ').write_bytes(b'KEY=value\0')
        (self.proc / 'uptime').write_text('1000.25 50.0\n')

    def test_identity_and_raw_arguments(self):
        self.assertEqual(procinfo.pids(self.proc), [42])
        self.assertEqual(procinfo.stat_fields(42, self.proc), self.fields)
        self.assertEqual(procinfo.parent(42, self.proc), 7)
        self.assertEqual(procinfo.start_ticks(42, self.proc), '12345')
        self.assertEqual(procinfo.comm(42, self.proc), 'name with ) and spaces')
        self.assertEqual(procinfo.argv(42, self.proc), [b'codex', b'\xff', b'', b''])
        self.assertEqual(procinfo.environ(42, self.proc), [b'KEY=value', b''])
        self.assertEqual(procinfo.uid(42, self.proc), os.stat(self.pid).st_uid)
        self.assertTrue(procinfo.stat_exists(42, self.proc))
        self.assertTrue(procinfo.foreground_differs(42, self.proc))
        self.fields[5] = '42'
        (self.pid / 'stat').write_text('42 (shell) ' + ' '.join(self.fields))
        self.assertFalse(procinfo.foreground_differs(42, self.proc))

    def test_start_time_math(self):
        with patch.object(procinfo.os, 'sysconf', return_value=100), own_time(procinfo, time=Mock(return_value=2000.5)):
            expected = int((2000.5 - 1000.25 + 12345 / 100) * 1000)
            self.assertEqual(procinfo.start_ms('12345', self.proc, now=2000.5), expected)
            self.assertEqual(procinfo.start_ms('12345', self.proc), expected)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux process clock')
    def test_start_time_ignores_container_uptime(self):
        fixed = 2_000_000.0
        boottime = 500_000.0
        ticks = '12345'
        expected = int((fixed - boottime + int(ticks) / os.sysconf('SC_CLK_TCK')) * 1000)
        with patch.object(procinfo, 'uptime', return_value=boottime - 4 * 86400), \
                own_time(procinfo, clock_gettime=Mock(return_value=boottime)):
            self.assertEqual(procinfo.start_ms(ticks, now=fixed), expected)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux process clock')
    def test_current_process_start_time_is_recent_and_in_the_past(self):
        started = procinfo.start_ms(procinfo.start_ticks(os.getpid())) / 1000
        now = time.time()
        self.assertLess(started, now)
        self.assertGreater(started, now - 86400)

    def test_fd_links_and_resolve_semantics(self):
        target = self.proc / 'target'
        target.write_text('')
        (self.pid / 'fd/1').symlink_to(target)
        (self.pid / 'fd/2').symlink_to('../../missing')
        (self.pid / 'fd/3').write_text('not a symlink')
        (self.pid / 'cwd').symlink_to(self.proc)
        self.assertEqual(dict(procinfo.fd_targets(42, self.proc)), {'1': str(target), '2': '../../missing'})
        self.assertEqual(procinfo.fd_target(42, 1, self.proc), str(target))
        self.assertEqual(procinfo.cwd(42, self.proc), str(self.proc))
        self.assertEqual(set(procinfo.resolved_fds(42, self.proc)),
                         {target, self.proc / 'missing', self.pid / 'fd/3'})

    def test_socket_lookup(self):
        row = '0: 0100007F:1234 0100007F:5678 01 0 0 0 %d 0 987\n' % os.getuid()
        (self.pid / 'fd/4').symlink_to('socket:[987]')
        for table in ('tcp', 'tcp6'):
            with self.subTest(table=table):
                (self.proc / 'net/tcp').unlink(missing_ok=True)
                (self.proc / 'net/tcp6').unlink(missing_ok=True)
                (self.proc / 'net' / table).write_text('header\nshort row\n' + row)
                self.assertEqual(procinfo.tcp_socket_inode(0x1234, 0x5678, os.getuid(), self.proc), '987')
                self.assertIsNone(procinfo.tcp_socket_inode(1, 2, os.getuid(), self.proc))
                self.assertIsNone(procinfo.tcp_socket_inode(0x1234, 0x5678, os.getuid() + 1, self.proc))
        self.assertEqual(procinfo.pid_with_socket('987', os.getuid(), self.proc), 42)
        self.assertIsNone(procinfo.pid_with_socket('987', os.getuid() + 1, self.proc))
        self.assertIsNone(procinfo.pid_with_socket('missing', os.getuid(), self.proc))

    def test_errors_propagate(self):
        for read in (procinfo.stat_fields, procinfo.comm, procinfo.argv, procinfo.environ,
                     procinfo.uid, procinfo.fd_targets, procinfo.cwd):
            with self.subTest(read=read.__name__), self.assertRaises(OSError):
                read(999, self.proc)
        with self.assertRaises(OSError):
            procinfo.fd_target(42, 'missing', self.proc)
        (self.pid / 'stat').write_text('malformed')
        with self.assertRaises(IndexError):
            procinfo.stat_fields(42, self.proc)
        (self.pid / 'stat').write_text('42 (name) S invalid')
        with self.assertRaises(ValueError):
            procinfo.parent(42, self.proc)
        (self.proc / 'uptime').write_text('invalid')
        with self.assertRaises(ValueError):
            procinfo.start_ms('12345', self.proc, now=2000)


class ProcessTableTests(unittest.TestCase):
    def test_linux_table_preserves_discovery_run_contract(self):
        run = Mock(return_value=SimpleNamespace(stdout=' 42 7 codex\n 7 1 name with spaces\nshort\n'))
        with patch.object(procinfo, 'DARWIN', False):
            self.assertEqual(procinfo.table(run), {42: (7, 'codex'), 7: (1, 'name with spaces')})
        run.assert_called_once_with(['ps', '-eo', 'pid=,ppid=,comm='],
                                    capture_output=True, text=True, timeout=5)

    def test_linux_socket_owner_uses_inode_and_uid(self):
        with patch.object(procinfo, 'DARWIN', False), \
                patch.object(procinfo, 'tcp_socket_inode', return_value='987') as inode, \
                patch.object(procinfo, 'pid_with_socket', return_value=42) as owner:
            self.assertEqual(procinfo.socket_owner(1234, 5678, 1000), 42)
            inode.assert_called_once_with(1234, 5678, 1000)
            owner.assert_called_once_with('987', 1000)
            inode.return_value = None
            owner.reset_mock()
            self.assertIsNone(procinfo.socket_owner(1234, 5678, 1000))
            owner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
