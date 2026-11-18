"""Recorded Darwin process samples and forced-Darwin callers; no Mac I/O."""
import ctypes
import errno
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import procinfo
from own_time import own_time

PS = '21717 21716 21717 21765 Ss     501\n21765 21717 21765 21765 S+     501\n'
ZSH = struct.pack('i', 2) + b'/bin/zsh\0\0\0\0zsh\0-i\0\0\0'
CLAUDE = (struct.pack('i', 2) + b'/Users/user/.local/share/claude/2.1.316\0\0'
          b'/Users/user/.local/bin/claude\0--resume\0\0KEY=value\0TETHER_CDP_URL=endpoint\0')
START = struct.pack('qi', 1_700_000_000, 123456).ljust(648, b'\0')
LSOF = ('p21717\nfcwd\ntDIR\nn/Users/user\nftxt\ntREG\nn/bin/zsh\n'
        'f1\ntCHR\nn/dev/ttys001\nf3\ntREG\nn/Users/user/log with spaces\n')


class ParseTests(unittest.TestCase):
    def test_ps_snapshot(self):
        self.assertEqual(procinfo._parse_ps('\nshort\n' + PS), {
            21717: {'ppid': 21716, 'pgid': 21717, 'tpgid': 21765, 'stat': 'Ss', 'uid': 501},
            21765: {'ppid': 21717, 'pgid': 21765, 'tpgid': 21765, 'stat': 'S+', 'uid': 501}})

    def test_procargs_without_environment(self):
        self.assertEqual(procinfo._parse_procargs(ZSH), (b'/bin/zsh', [b'zsh', b'-i'], []))

    def test_procargs_with_environment_and_padding(self):
        self.assertEqual(procinfo._parse_procargs(CLAUDE), (
            b'/Users/user/.local/share/claude/2.1.316',
            [b'/Users/user/.local/bin/claude', b'--resume'],
            [b'KEY=value', b'TETHER_CDP_URL=endpoint']))

    def test_procargs_rejects_incomplete_records(self):
        for data in (b'', b'bad', struct.pack('i', -1) + b'x\0',
                     struct.pack('i', 2) + b'x', struct.pack('i', 2) + b'x\0arg\0'):
            with self.subTest(data=data), self.assertRaises(OSError):
                procinfo._parse_procargs(data)

    def test_kinfo_start_and_boottime(self):
        self.assertEqual(procinfo._parse_timeval(START), (1_700_000_000, 123456))
        self.assertEqual(procinfo._parse_timeval(struct.pack('qi', 100, 500000)), (100, 500000))
        for data in (b'', b'short'):
            with self.subTest(data=data), self.assertRaises(OSError):
                procinfo._parse_timeval(data)

    def test_lsof_file_records(self):
        self.assertEqual(procinfo._parse_lsof(LSOF), {
            'cwd': '/Users/user', 'txt': '/bin/zsh', '1': '/dev/ttys001',
            '3': '/Users/user/log with spaces'})
        self.assertEqual(procinfo._parse_lsof('p21717\n'), {})

    def test_lsof_without_process_raises(self):
        for text in ('', 'fcwd\ntDIR\nn/Users/user\n'):
            with self.subTest(text=text), self.assertRaises(OSError):
                procinfo._parse_lsof(text)

    def test_socket_owner_ipv4_and_ipv6(self):
        for name in ('127.0.0.1:55001->127.0.0.1:29000', '[::1]:55001->[::1]:29000'):
            with self.subTest(name=name):
                self.assertEqual(procinfo._parse_socket_owner('p21765\nn' + name + '\n', 55001, 29000), 21765)

    def test_socket_owner_matches_direction_and_process(self):
        wrong = ('p12\nn127.0.0.1:29000->127.0.0.1:55001\n'
                 'p13\nn[::1]:29000->[::1]:55001\n'
                 'p14\nn192.0.2.1:55001->127.0.0.1:29000\n')
        self.assertIsNone(procinfo._parse_socket_owner(wrong, 55001, 29000))
        self.assertEqual(procinfo._parse_socket_owner(
            wrong + 'p15\nn127.0.0.1:55001->127.0.0.1:29000\n', 55001, 29000), 15)
        self.assertIsNone(procinfo._parse_socket_owner('', 55001, 29000))


class DarwinFixtures(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(procinfo, 'DARWIN', True))
        self.enterContext(patch.object(procinfo, '_DARWIN_SNAPSHOT', None))
        self.enterContext(patch.object(procinfo, '_DARWIN_SNAPSHOT_AT', 0))
        self.ps = self.enterContext(patch.object(procinfo, '_darwin_ps', return_value=PS))
        self.sysctl = self.enterContext(patch.object(procinfo, '_darwin_sysctl', side_effect=self.sysctl_data))

    def sysctl_data(self, mib):
        if mib[:2] == [1, 49]:
            return CLAUDE if mib[2] == 21765 else ZSH
        if mib[:3] == [1, 14, 1]:
            return START
        if mib == [1, 21]:
            return struct.pack('qi', 100, 500000)
        raise AssertionError(mib)


class DarwinTests(DarwinFixtures):
    def test_snapshot_cached_for_one_second(self):
        with own_time(procinfo, monotonic=Mock(return_value=10)) as clock:
            self.assertEqual(procinfo.pids(), [21717, 21765])
            self.assertEqual(procinfo.parent(21765), 21717)
            self.assertEqual(procinfo.uid(21717), 501)
            self.assertTrue(procinfo.stat_exists(21717))
            clock.return_value = 10.999
            procinfo.pids()
            self.ps.assert_called_once()
            clock.return_value = 11
            procinfo.pids()
            self.assertEqual(self.ps.call_count, 2)

    def test_fresh_table_reads_ps_again_inside_the_cache_second(self):
        with own_time(procinfo, monotonic=Mock(return_value=10)):
            procinfo.table()
            procinfo.table()
            self.ps.assert_called_once()
            procinfo.table(fresh=True)
            self.assertEqual(self.ps.call_count, 2)

    def test_missing_pid_refreshes_cached_snapshot_once(self):
        with own_time(procinfo, monotonic=Mock(return_value=10)):
            procinfo.pids()
            self.ps.return_value += '42 1 42 -1 S 501\n'
            self.assertEqual(procinfo.parent(42), 1)
            self.assertEqual(self.ps.call_count, 2)
            with self.assertRaises(ProcessLookupError):
                procinfo.parent(99)
            self.assertEqual(self.ps.call_count, 3)
            self.assertFalse(procinfo.stat_exists(99))
            self.assertEqual(self.ps.call_count, 4)

    def test_missing_pid_in_fresh_snapshot_reads_once(self):
        with self.assertRaises(OSError):
            procinfo.uid(99)
        self.ps.assert_called_once()

    def test_stat_fields_and_foreground(self):
        fields = procinfo.stat_fields(21717)
        self.assertEqual(len(fields), 20)
        self.assertEqual([fields[i] for i in (0, 1, 2, 5, 19)],
                         ['S', '21716', '21717', '21765', '1700000000.123456'])
        self.assertTrue(all(value == '0' for i, value in enumerate(fields) if i not in (0, 1, 2, 5, 19)))
        self.assertTrue(procinfo.foreground_differs(21717))
        self.assertFalse(procinfo.foreground_differs(21765))

    def test_start_token_and_wall_clock_math(self):
        self.assertEqual(procinfo.start_ticks(21765), '1700000000.123456')
        self.sysctl.assert_called_with([1, 14, 1, 21765])
        self.assertEqual(procinfo.start_ms('1700000000.123456', now=0), 1_700_000_000_123)
        with own_time(procinfo, time=Mock(return_value=200.75)):
            self.assertEqual(procinfo.uptime(), 100.25)
        self.sysctl.assert_called_with([1, 21])
        self.sysctl.return_value, self.sysctl.side_effect = b'', None
        with self.assertRaises(OSError):
            procinfo.start_ticks(99)

    def test_comm_uses_argv_not_versioned_executable(self):
        self.assertEqual(procinfo.comm(21765), 'claude')
        self.assertEqual(procinfo.comm(21717), 'zsh')
        self.assertEqual(procinfo.argv(21717), [b'zsh', b'-i'])
        self.sysctl.assert_called_with([1, 49, 21717])

    def test_comm_fallbacks(self):
        with patch.object(procinfo, '_darwin_procargs', return_value=(b'/tools/codex', [], [])):
            self.assertEqual(procinfo.comm(42), 'codex')
        with patch.object(procinfo, '_darwin_procargs', return_value=(b'', [], [])), \
                patch.object(procinfo, '_darwin_ucomm', return_value='zsh') as ucomm:
            self.assertEqual(procinfo.comm(42), 'zsh')
            ucomm.assert_called_once_with(42)
        with patch.object(procinfo, '_darwin_procargs', side_effect=OSError(errno.EINVAL, 'hidden')), \
                patch.object(procinfo, '_darwin_ucomm', return_value='sleep'):
            self.assertEqual(procinfo.comm(1), 'sleep')

    def test_comm_missing_process_raises_oserror(self):
        self.sysctl.side_effect = OSError(errno.EINVAL, 'hidden')
        with patch.object(procinfo.subprocess, 'run', return_value=SimpleNamespace(stdout='', returncode=1)):
            with self.assertRaises(OSError):
                procinfo.comm(99)

    def test_environment_hidden_and_present(self):
        self.assertIsNone(procinfo.environ(21717))
        self.assertEqual(procinfo.environ(21765), [b'KEY=value', b'TETHER_CDP_URL=endpoint'])
        self.sysctl.side_effect = OSError(errno.EINVAL, 'hidden')
        with self.assertRaises(OSError):
            procinfo.argv(1)
        with self.assertRaises(OSError):
            procinfo.environ(1)

    def test_table_skips_unreadable_argv(self):
        self.ps.return_value += '1 0 1 -1 Ss 0\n'
        def data(mib):
            if mib == [1, 49, 1]:
                raise OSError(errno.EINVAL, 'hidden')
            return self.sysctl_data(mib)
        self.sysctl.side_effect = data
        run = Mock(side_effect=AssertionError('Linux table ran'))
        self.assertEqual(procinfo.table(run), {21717: (21716, 'zsh'), 21765: (21717, 'claude')})
        run.assert_not_called()

    def test_lsof_readers_use_one_call_and_numeric_fds(self):
        with patch.object(procinfo.subprocess, 'run', return_value=SimpleNamespace(stdout=LSOF, returncode=0)) as run:
            operations = [
                (lambda: procinfo.fd_names(21717), ['1', '3']),
                (lambda: procinfo.fd_target(21717, 1, readlink=os.readlink), '/dev/ttys001'),
                (lambda: procinfo.fd_targets(21717), [('1', '/dev/ttys001'), ('3', '/Users/user/log with spaces')]),
                (lambda: list(procinfo.resolved_fds(21717)),
                 [Path('/dev/ttys001').resolve(), Path('/Users/user/log with spaces').resolve()]),
                (lambda: procinfo.cwd(21717, readlink=os.readlink), '/Users/user')]
            for operation, expected in operations:
                with self.subTest(operation=operation):
                    run.reset_mock()
                    self.assertEqual(operation(), expected)
                    run.assert_called_once_with(['lsof', '-nP', '-a', '-p', '21717', '-F', 'ftn'],
                                                capture_output=True, text=True, timeout=1)

    def test_lsof_missing_process_or_file(self):
        with patch.object(procinfo.subprocess, 'run', return_value=SimpleNamespace(stdout='', returncode=1)) as run:
            with self.assertRaises(OSError):
                procinfo.fd_names(1)
            run.return_value = SimpleNamespace(stdout='p21717\n', returncode=0)
            for operation in (lambda: procinfo.fd_target(21717, 'cwd'), lambda: procinfo.fd_target(21717, 9),
                              lambda: procinfo.cwd(21717)):
                with self.assertRaises(OSError):
                    operation()

    def test_socket_owner_lsof_command(self):
        with patch.object(procinfo.subprocess, 'run', return_value=SimpleNamespace(
                stdout='p21765\nn127.0.0.1:55001->127.0.0.1:29000\n', returncode=0)) as run:
            self.assertEqual(procinfo.socket_owner(55001, 29000, 501), 21765)
            run.assert_called_once_with(['lsof', '-nP', '-a', '-u', '501', '-iTCP:55001',
                                        '-sTCP:ESTABLISHED', '-F', 'pn'],
                                       capture_output=True, text=True, timeout=1)


class SysctlIOTests(unittest.TestCase):
    def test_size_query_then_read(self):
        def sysctl(names, count, data, size_pointer, new, new_size):
            self.assertEqual(list(names), [1, 49, 21717])
            self.assertEqual(count, 3)
            size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
            if data is not None:
                self.assertEqual(size.contents.value, len(ZSH))
                ctypes.memmove(data, ZSH, len(ZSH))
            size.contents.value = len(ZSH)
            return 0
        call = Mock(side_effect=sysctl)
        with patch.object(procinfo.ctypes, 'CDLL', return_value=SimpleNamespace(sysctl=call)) as libc:
            self.assertEqual(procinfo._darwin_sysctl([1, 49, 21717]), ZSH)
        libc.assert_called_once_with(None, use_errno=True)
        self.assertEqual(call.call_count, 2)

    def test_sysctl_errno_at_either_step(self):
        for values in ([-1], [0, -1]):
            with self.subTest(values=values), \
                    patch.object(procinfo.ctypes, 'CDLL', return_value=SimpleNamespace(sysctl=Mock(side_effect=values))), \
                    patch.object(procinfo.ctypes, 'get_errno', return_value=errno.EINVAL):
                with self.assertRaises(OSError) as raised:
                    procinfo._darwin_sysctl([1, 49, 1])
                self.assertEqual(raised.exception.errno, errno.EINVAL)


class CallerTests(DarwinFixtures):
    def test_codex_level_processes_shape(self):
        import codex_level
        self.assertEqual(codex_level.processes(), {
            21717: (21716, ['zsh', '-i']), 21765: (21717, ['/Users/user/.local/bin/claude', '--resume'])})
        self.sysctl.side_effect = OSError(errno.EINVAL, 'hidden')
        self.assertEqual(codex_level.processes(), {})

    def test_live_processes_shape(self):
        import live
        self.assertEqual(live._processes()[21765], {
            'uid': 501, 'group': 21765, 'start': '1700000000.123456', 'parent': 21717,
            'state': 'S', 'name': 'claude', 'args': ['/Users/user/.local/bin/claude', '--resume']})

    def test_peer_pane_walks_ancestors(self):
        import browser_cdp
        panes = Mock(return_value='%12 21717\n')
        with patch.object(procinfo, 'socket_owner', return_value=21765) as owner:
            self.assertEqual(browser_cdp.peer_pane(55001, 29000, pane_reader=panes, clock=lambda: 0, uid=501), '%12')
        owner.assert_called_once_with(55001, 29000, 501)
        panes.assert_called_once_with(2)

    def test_peer_pane_handles_missing_owner_and_deadline(self):
        import browser_cdp
        panes = Mock(return_value='%12 21717\n')
        with patch.object(procinfo, 'socket_owner', return_value=None):
            self.assertIsNone(browser_cdp.peer_pane(55001, 29000, pane_reader=panes, clock=lambda: 0))
        panes.assert_not_called()
        clock = Mock(side_effect=[0, 0, 2])
        with patch.object(procinfo, 'socket_owner', return_value=21765):
            self.assertIsNone(browser_cdp.peer_pane(55001, 29000, pane_reader=panes, clock=clock))
        panes.assert_not_called()

    def test_stale_panes_hidden_environment_is_not_stale(self):
        import agent_browser_setup
        panes = Mock(return_value='%12 21717\n')
        with patch.object(procinfo, 'environ', return_value=None):
            self.assertEqual(agent_browser_setup.stale_panes('endpoint', pane_reader=panes,
                                                           clock=lambda: 0, uid=501, cache={}), [])

    def test_stale_panes_wrong_env_cache_and_deadline(self):
        import agent_browser_setup
        now, cache = [0], {}
        clock = lambda: now[0]
        panes = Mock(return_value='%12 21717\n')
        def scan():
            return agent_browser_setup.stale_panes('endpoint', pane_reader=panes, clock=clock, uid=501, cache=cache)
        with patch.object(procinfo, 'environ', return_value=[b'TETHER_CDP_URL=old']) as env:
            self.assertEqual(scan(), ['%12'])
            env.return_value = [b'TETHER_CDP_URL=endpoint']
            now[0] = 9.99
            self.assertEqual(scan(), ['%12'])
            now[0] = 10
            self.assertEqual(scan(), [])
            self.assertEqual(panes.call_count, 2)
        with patch.object(procinfo, 'argv') as argv:
            self.assertEqual(agent_browser_setup.stale_panes('endpoint', pane_reader=panes,
                             clock=Mock(side_effect=[0, 0, 2, 2, 2]), uid=501, cache={}), [])
            argv.assert_not_called()

    def test_verify_accepts_version_only_for_darwin_claude(self):
        import terminal_input
        for darwin, agent, command, allowed in (
                (True, 'claude', '2.1.316', True), (False, 'claude', '2.1.316', False),
                (True, 'codex', '2.1.316', False), (True, 'antigravity', '2.1.316', False),
                (True, 'claude', '2.1.316-extra', False), (True, 'claude', '2.1', False)):
            with self.subTest(darwin=darwin, agent=agent, command=command), \
                    patch.object(procinfo, 'DARWIN', darwin), \
                    patch.object(procinfo, 'comm', return_value={'antigravity': 'agy'}.get(agent, agent)), \
                    patch.object(terminal_input, 'process_start', return_value='1700000000.123456'):
                entry = {'owner_pid': 21765, 'process_start': '1700000000.123456', 'agent': agent,
                         'id': '%12', 'pending_conversation': True}
                run = Mock(return_value=SimpleNamespace(stdout='21765\t' + command + '\t0\n'))
                if allowed:
                    terminal_input.verify(entry, run)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Return to the agent prompt'):
                        terminal_input.verify(entry, run)

    def test_shell_prompt_uses_darwin_stat_fields(self):
        import terminal_input
        self.ps.return_value = PS.replace('21717 21716 21717 21765', '21717 21716 21717 21717')
        entry = {'agent': 'shell', 'id': '%12', 'pane_pid': 21717, 'pane_start': '1700000000.123456'}
        run = Mock(side_effect=[SimpleNamespace(stdout='21717\tzsh\t0\t0\t2\t80\n'),
                                SimpleNamespace(stdout='$ \n')])
        self.assertEqual(terminal_input.shell_prompt(entry, run), '$')

    def test_codex_start_tokens_sort_and_match(self):
        import agents
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE threads (id,rollout_path,cwd,archived,source,created_at_ms,updated_at_ms)')
        db.execute("INSERT INTO threads VALUES ('first','/r/first','/work',0,'cli',1700000005000,1700000005000)")
        entries = [{'agent': 'codex', 'process_start': '1700000010.123456', 'pending_conversation': True},
                   {'agent': 'codex', 'process_start': '1700000000.123456', 'pending_conversation': True}]
        agents.match_unresolved_codex(db, entries)
        self.assertEqual(entries[0]['thread'], 'first')
        self.assertNotIn('thread', entries[1])

    def test_discover_keeps_older_codex_owner_with_decimal_tokens(self):
        import agents
        panes = ('%2\t$1\tnew\t0.0\t200\t/work\n'
                 '%1\t$1\told\t0.1\t100\t/work\n')
        run = Mock(return_value=SimpleNamespace(stdout=panes, returncode=0))
        processes = {100: (1, 'codex'), 200: (1, 'codex')}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(agents, 'HOME', Path(folder)), \
                patch.object(agents.pane_view, 'zoom_repair_all'), \
                patch.object(procinfo, 'table', return_value=processes) as table, \
                patch.object(agents, 'process_start', side_effect=lambda pid: '%d.123456' % pid), \
                patch.object(agents, 'codex_database'), \
                patch.object(agents, 'codex_claim', side_effect=lambda *_: {
                    'kind': 'open', 'rows': [('same-thread', '/r/transcript')], 'unnamed': False, 'named': False}), \
                patch.object(agents.codex_provider, 'pane_profile', return_value=None), \
                patch.object(agents, 'codex_names', return_value={}):
            entries = agents.discover({}, run)
        table.assert_called_once_with(run)
        self.assertEqual(entries[1]['thread'], 'same-thread')
        self.assertEqual(entries[0]['thread'], 'pending-200-200.123456')

    def test_antigravity_cache_uses_wall_clock_start(self):
        import antigravity
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            (cache / 'current.json').write_text(json.dumps({
                'conversation_id': 'current', 'cwd': '/work', 'reported_at': 1_700_000_001,
                'transcript_path': '/r/current'}))
            (cache / 'old.json').write_text(json.dumps({
                'conversation_id': 'old', 'cwd': '/work', 'reported_at': 1_699_999_999,
                'transcript_path': '/r/old'}))
            with patch.object(antigravity, 'CACHE', cache), \
                    patch.object(antigravity, 'owner_pid', return_value=21765), \
                    patch.object(antigravity, 'locked_elsewhere', return_value=set()), \
                    patch.object(procinfo, 'resolved_fds', return_value=[]):
                entry = antigravity.live_entry([21765], cwd='/work')
            self.assertEqual(entry['thread'], 'current')
            self.assertEqual(entry['process_start'], '1700000000.123456')


if __name__ == '__main__':
    unittest.main()
