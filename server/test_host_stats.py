"""Host resources use fake proc/cgroup files and mocked terminal writes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import host_stats
import server
import side_shells


class HostStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp) / 'cgroup'
        self.proc = Path(self.temp) / 'proc'
        self.now = 10
        self.sampler = host_stats.Sampler(self.root, self.proc, clock=lambda: self.now)
        self.enterContext(patch.dict(host_stats.os.environ, TETHER_RESOURCES='1'))
        self.glances = self.enterContext(patch.object(host_stats, 'glances', return_value={}))
        self.write(self.root / 'cgroup.controllers', 'cpu memory io pids')
        self.write(self.proc / 'stat', 'cpu 10 0 10 80 0 0 0 0 0 0')
        self.write(self.proc / 'meminfo', 'MemTotal: 1000 kB\nMemAvailable: 300 kB')
        self.write(self.proc / 'loadavg', '1.0 2.0 3.0 1/20 40')
        self.write(self.proc / 'uptime', '12345.0 100')

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def group(self, name, usage=1000000, memory='max', cpu='max 100000'):
        path = self.root / name
        for file, text in {'cpu.stat': f'usage_usec {usage}', 'cpu.max': cpu, 'memory.current': '1048576',
                           'memory.max': memory, 'io.stat': '8:0 rbytes=100 wbytes=200 rios=1\n8:1 rbytes=5 wbytes=7',
                           'pids.current': '4'}.items():
            self.write(path / file, text)
        return path

    def test_lxc_limits_and_unlimited(self):
        self.group('lxc.payload.worker', memory='2097152', cpu='200000 100000')
        self.group('lxc.payload.helper')
        groups = {g['name']: g for g in self.sampler.payload()['groups']}
        self.assertEqual(groups['worker'], dict(kind='lxc', name='worker', cpu_pct=None, cpu_limit=2,
                         mem_bytes=1048576, mem_limit=2097152, io_read=105, io_write=207, pids=4))
        self.assertIsNone(groups['helper']['mem_limit'])
        self.assertIsNone(groups['helper']['cpu_limit'])

    def test_memory_excludes_inactive_file_cache(self):
        # memory.current counts page cache. The kernel frees inactive cache on demand, so it is not memory the group holds.
        path = self.group('lxc.payload.worker')
        self.write(path / 'memory.stat', 'anon 300000\nfile 748576\ninactive_file 648576\nactive_file 100000')
        self.assertEqual(self.sampler.payload()['groups'][0]['mem_bytes'], 1048576 - 648576)

    def test_cpu_delta_and_host_metrics(self):
        path = self.group('lxc.payload.worker')
        first = self.sampler.payload()
        self.assertIsNone(first['host']['cpu_pct'])
        self.assertEqual(first['host']['mem_bytes'], 700 * 1024)
        self.assertEqual(first['host']['mem_total'], 1000 * 1024)
        self.assertEqual(first['host']['load'], [1, 2, 3])
        self.assertEqual(first['host']['uptime'], 12345)
        self.assertGreater(first['host']['disk_total'], 0)
        self.write(path / 'cpu.stat', 'usage_usec 3500000')
        self.write(self.proc / 'stat', 'cpu 30 0 10 160 0 0 0 0 0 0')
        self.now += 5
        second = self.sampler.payload()
        self.assertEqual(second['groups'][0]['cpu_pct'], 50)
        self.assertEqual(second['host']['cpu_pct'], 20)
        self.write(path / 'cpu.stat', 'usage_usec 100')
        self.now += 5
        self.assertIsNone(self.sampler.payload()['groups'][0]['cpu_pct'])

    def test_host_exposes_cpu_count(self):
        with patch.object(host_stats.os, 'cpu_count', return_value=16):
            self.assertEqual(self.sampler.payload()['host']['cpus'], 16)

    def test_slow_docker_lookup_does_not_inflate_cpu(self):
        self.group('lxc.payload.worker')
        self.group('system.slice/docker-abcdef0123456789.scope')
        self.group('user.slice/user-1234.slice')
        original_read = host_stats.read

        def read_with_workload(path):
            if Path(path).name == 'cpu.stat':
                return f'usage_usec {int(self.now * 1000000)}'
            # Give later groups a different sample time, too.
            if Path(path).name == 'memory.current':
                self.now += .5
            return original_read(path)

        def slow_docker(*args, **kwargs):
            self.now += 3
            raise subprocess.TimeoutExpired('docker', 3)

        with patch.object(host_stats, 'read', side_effect=read_with_workload), \
                patch.object(host_stats.subprocess, 'run', return_value=Mock(returncode=0, stdout='')) as docker:
            self.sampler.payload()
            self.now += 5
            host_sample_time = self.now
            docker.side_effect = slow_docker
            second = self.sampler.payload()
        self.assertEqual([g['cpu_pct'] for g in second['groups']], [100, 100, 100])
        self.assertEqual(self.sampler.host_previous[0], host_sample_time)

    def test_cache_expires_at_five_seconds(self):
        self.group('lxc.payload.worker')
        first = self.sampler.payload()
        self.now += 4.9
        self.assertIs(self.sampler.payload(), first)
        self.glances.assert_called_once()
        self.now += .1
        self.assertIsNot(self.sampler.payload(), first)
        self.assertEqual(self.glances.call_count, 2)

    def test_missing_cgroup_and_files_are_safe(self):
        self.root.joinpath('cgroup.controllers').unlink()
        self.assertEqual(self.sampler.payload()['groups'], [])
        self.now += 5
        self.write(self.root / 'cgroup.controllers', '')
        self.root.joinpath('lxc.payload.empty').mkdir()
        self.assertIsNone(self.sampler.payload()['groups'][0]['cpu_pct'])

    def test_docker_fallback_and_cli_names(self):
        self.group('system.slice/docker-abcdef0123456789.scope')
        for failure in [FileNotFoundError(), subprocess.TimeoutExpired('docker', 3)]:
            with self.subTest(failure=failure), patch.object(host_stats.subprocess, 'run', side_effect=failure):
                self.assertEqual(self.sampler.groups()[0]['name'], 'abcdef012345')
        with patch.object(host_stats.subprocess, 'run', return_value=Mock(returncode=0, stdout='abcdef012345\tweb\n')) as run:
            self.assertEqual(self.sampler.groups()[0]['name'], 'web')
        self.assertEqual(run.call_args.kwargs['timeout'], 3)

    def test_user_name_and_removed_group(self):
        path = self.group('user.slice/user-1234.slice')
        with patch.object(host_stats.pwd, 'getpwuid', return_value=Mock(pw_name='worker')):
            self.assertEqual(self.sampler.groups()[0]['name'], 'worker')
        with patch.object(host_stats.pwd, 'getpwuid', side_effect=KeyError):
            self.assertEqual(self.sampler.groups()[0]['name'], '1234')
        for file in path.iterdir():
            file.unlink()
        path.rmdir()
        self.sampler.groups()
        self.assertEqual(self.sampler.previous, {})

    def test_disabled_does_not_compute(self):
        with patch.dict(host_stats.os.environ, TETHER_RESOURCES='0'), patch.object(self.sampler, 'host') as host:
            self.assertIsNone(self.sampler.payload())
            host.assert_not_called()
            self.glances.assert_not_called()

    def handler(self, path):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.headers = {}
        handler.client_address = ('127.0.0.1', 1234)
        handler.respond = Mock()
        return handler

    def test_flag_off_route_is_404_and_route_uses_auth(self):
        handler = self.handler('/mobile/api/host-stats')
        with patch.object(handler, 'deny', return_value=False), patch.dict(host_stats.os.environ, TETHER_RESOURCES='0'):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0], 404)
        handler.respond.reset_mock()
        with patch.object(handler, 'deny', return_value=True), patch.object(host_stats, 'payload') as sample:
            handler.do_GET()
        sample.assert_not_called()
        handler.respond.assert_not_called()

    def test_enabled_route_and_capabilities(self):
        handler = self.handler('/mobile/api/host-stats')
        with patch.object(handler, 'deny', return_value=False), patch.object(host_stats, 'SAMPLER', self.sampler):
            handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0], 200)
        self.assertIn('host', handler.respond.call_args.args[1])
        handler = self.handler('/mobile/api/host')
        with patch.object(handler, 'deny', return_value=False), patch.object(server, 'TTYD_PORT', 0):
            handler.do_GET()
        self.assertTrue(handler.respond.call_args.args[1]['resources'])
        self.assertFalse(handler.respond.call_args.args[1]['terminal'])

    def test_btop_starts_fresh_side_shell_without_sending_keys(self):
        run = Mock(side_effect=[Mock(returncode=0, stdout='%1\ttether-shells\t/\n'), Mock(returncode=0, stdout='%2'),
                                Mock(returncode=0), Mock(returncode=0)])
        with patch.object(side_shells.shutil, 'which', return_value='/bin/btop'):
            self.assertEqual(side_shells.open_shell('/', pane='%1', run=run, program='btop'), '%2')
        command = run.call_args_list[1].args[0]
        self.assertIn('new-window', command)
        self.assertEqual(command[-1], '/bin/btop')
        self.assertEqual(run.call_args_list[2].args[0][-1], '')
        # A fixed window size: a narrow viewer must not shrink btop below the size it draws in.
        self.assertEqual(run.call_args_list[3].args[0][1:], ['resize-window', '-t', '%2', '-x', '80', '-y', '24'])
        phone = Mock(side_effect=[Mock(returncode=0, stdout=''), Mock(returncode=0, stdout='%4'),
                                  Mock(returncode=0), Mock(returncode=0)])
        with tempfile.TemporaryDirectory() as cache, patch.object(side_shells.paths, 'CACHE', Path(cache)), \
                patch.object(side_shells.shutil, 'which', return_value='/bin/btop'):
            side_shells.open_shell('/', run=phone, program='btop', compact=True)
            home = Path(cache) / 'btop-compact'
            self.assertEqual((home / 'btop/btop.conf').read_text(), 'shown_boxes = "cpu proc"\n')
        self.assertEqual(phone.call_args_list[1].args[0][-3:], ['env', f'XDG_CONFIG_HOME={home}', '/bin/btop'])
        self.assertEqual(phone.call_args_list[3].args[0][-4:], ['-x', '60', '-y', '30'])
        self.assertFalse(any('send-keys' in call.args[0] for call in run.call_args_list))
        with self.assertRaises(ValueError):
            side_shells.open_shell('/', run=run, program='anything')

    def test_btop_reuses_live_monitor_and_recreates_dead_monitor(self):
        with patch.object(side_shells.shutil, 'which', return_value='/bin/btop'):
            live = Mock(return_value=Mock(returncode=0, stdout='%2\ttether-shells\t\n'))
            self.assertEqual(side_shells.open_shell('/', pane='%2', run=live, program='btop'), '%2')
            self.assertEqual(live.call_count, 1)
            # The old monitor exited. A regular shell must remain untouched.
            dead = Mock(side_effect=[Mock(returncode=0, stdout='%1\ttether-shells\t/\n'),
                                     Mock(returncode=0, stdout='%3'), Mock(returncode=0), Mock(returncode=0)])
            self.assertEqual(side_shells.open_shell('/', pane='%2', run=dead, program='btop'), '%3')
            self.assertIn('new-window', dead.call_args_list[1].args[0])
            self.assertEqual(dead.call_args_list[1].args[0][-1], '/bin/btop')


class GlancesTests(unittest.TestCase):
    def test_fixed_local_url_and_top_five(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        processes = [dict(pid=i, name=f'process-{i}', cpu_percent=i) for i in range(8)]
        with patch.dict(host_stats.os.environ, TETHER_GLANCES_URL=host_stats.GLANCES_URL), patch.object(host_stats, 'build_opener', return_value=opener), patch.object(host_stats.json, 'load', side_effect=[{'status': 'OK'}, processes]):
            result = host_stats.glances()
        self.assertTrue(result['glances'])
        self.assertEqual([p['pid'] for p in result['processes']], [7, 6, 5, 4, 3])
        self.assertEqual(opener.open.call_args_list[0].args[0], host_stats.GLANCES_URL + '/api/4/status')
        self.assertTrue(all(call.kwargs['timeout'] == 1 for call in opener.open.call_args_list))
        with patch.dict(host_stats.os.environ, TETHER_GLANCES_URL='http://example.invalid'), patch.object(host_stats, 'build_opener') as build:
            self.assertEqual(host_stats.glances(), {})
            build.assert_not_called()

    def test_unavailable_and_no_redirects(self):
        with patch.dict(host_stats.os.environ, TETHER_GLANCES_URL=host_stats.GLANCES_URL), patch.object(host_stats, 'build_opener', side_effect=None) as build:
            build.return_value.open.side_effect = OSError()
            self.assertEqual(host_stats.glances(), {})
        self.assertIsNone(host_stats.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.invalid'))
