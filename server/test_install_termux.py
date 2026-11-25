"""Termux plans and applies use temporary homes, prefixes, and mocked processes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('termux_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
loader = importlib.machinery.SourceFileLoader('termux_tether_cli', str(ROOT / 'tether'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class TermuxInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'home'
        self.home.mkdir()
        self.prefix = Path(temporary.name) / 'prefix with spaces'
        self.units = self.prefix / 'var/service'
        self.units.mkdir(parents=True)
        self.repo = Path(temporary.name) / 'repo with spaces'
        (self.repo / 'ui/src').mkdir(parents=True)
        (self.repo / 'ui/dist').mkdir()
        (self.repo / 'ui/dist/index.html').write_text('built')
        self.state = self.home / '.config/tether/install.json'
        self.patch(mock.patch.dict(os.environ, HOME=str(self.home), PREFIX=str(self.prefix), PATH='/mock/bin', clear=True))
        for module in (installer, cli):
            self.patch(mock.patch.object(module, 'DARWIN', False))
            self.patch(mock.patch.object(module, 'TERMUX', True))
        self.process = self.patch(mock.patch.object(subprocess, 'run', side_effect=AssertionError('Unmocked process')))
        self.probe = self.patch(mock.patch.object(installer, 'probe', return_value=subprocess.CompletedProcess([], 0, '', '')))
        self.run = self.patch(mock.patch.object(installer, 'run'))
        self.cli_run = self.patch(mock.patch.object(cli, 'run'))
        self.socket = self.patch(mock.patch.object(installer.socket, 'socket'))
        self.patch(mock.patch.object(installer.shutil, 'which', side_effect=lambda name: f'/mock/bin/{name}'))
        self.legacy = self.patch(mock.patch.object(installer, 'legacy_settings', side_effect=AssertionError('Linux only')))
        self.ports = self.patch(mock.patch.object(installer, 'occupied_ports', return_value=set()))

    def patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def plan(self, *args):
        args = installer.parser().parse_args(['--plan', *args])
        return installer.make_plan(args, repo=self.repo, home=self.home)

    def apply(self, plan):
        installer.apply_plan(plan, home=self.home)

    def checks(self, plan):
        return {item['name']: item for item in plan['checks']}

    def test_default_plan_paths_commands_and_environment(self):
        python = self.prefix / 'bin/python'
        python.parent.mkdir()
        python.symlink_to(sys.executable)
        with mock.patch.object(sys, 'executable', str(python)), \
                mock.patch.object(installer.platform, 'system', return_value='Android'):
            plan = self.plan('--replace-legacy')
        self.assertEqual(set(plan['units']), {'tether.service', 'tether-terminal.service'})
        self.assertEqual(plan['units_dir'], str(self.units))
        self.assertEqual(plan['legacy_units'], [])
        self.legacy.assert_not_called()
        self.assertTrue(all(item['ok'] for item in plan['checks']))
        self.assertEqual([item['name'] for item in plan['checks']][:3],
                         ['Termux', 'Python >= 3.11', 'termux-services'])
        text = plan['units']['tether.service']
        self.assertTrue(text.startswith(f'#!{self.prefix}/bin/sh\n# Tether chat (written by Tether install.py)\nexec 2>&1\nexec env '))
        words = shlex.split(text.splitlines()[-1])
        self.assertEqual(words[-2:], [str(python), str(self.repo / 'server/server.py')])
        self.assertIn('MOBILE_TMUX_PORT=7683', words)
        self.assertIn(f'PATH=/mock/bin:{self.prefix}/bin', words)
        self.assertEqual(plan['commands'], [['sv', 'restart', str(self.units / installer.runit_name(name))]
                                            for name in sorted(plan['units'])])
        self.probe.assert_not_called()

    def test_path_does_not_duplicate_prefix(self):
        path = f'{self.prefix}/bin:/mock/bin'
        with mock.patch.dict(os.environ, PATH=path):
            words = shlex.split(self.plan()['units']['tether.service'].splitlines()[-1])
        self.assertIn('PATH=' + path, words)

    def test_unsupported_options_fail_without_timer_or_legacy_calls(self):
        for args, name, fix in ((('--voice',), 'voice', 'Voice is not supported on Termux; use --no-voice.'),
                                (('--updates', 'auto'), 'automatic updates', 'Termux has no timer service; use --updates notify.')):
            with self.subTest(name=name):
                plan = self.plan(*args)
                self.assertEqual(self.checks(plan)[name], dict(name=name, ok=False, fix=fix))
                self.assertFalse(set(installer.UPDATE_UNITS) & plan['units'].keys())
                with self.assertRaisesRegex(ValueError, 'Required checks failed'):
                    self.apply(plan)
        self.legacy.assert_not_called()
        self.probe.assert_not_called()
        self.run.assert_not_called()

    def test_missing_services_and_fix_texts(self):
        for missing in ('sv', 'svlogd'):
            with self.subTest(missing=missing), mock.patch.object(installer.shutil, 'which', side_effect=lambda name: None if name == missing else '/mock/bin/' + name):
                check = self.checks(self.plan())['termux-services']
                self.assertFalse(check['ok'])
                self.assertEqual(check['fix'], 'Run: pkg install termux-services, then close and reopen Termux.')
        self.units.rmdir()
        self.assertFalse(self.checks(self.plan())['termux-services']['ok'])
        with mock.patch.object(installer.shutil, 'which', return_value=None):
            checks = self.checks(self.plan('--terminal', '--build-ui'))
        for name, fix in (('Python >= 3.11', 'Run: pkg install python'), ('tmux', 'Run: pkg install tmux'),
                          ('ttyd', 'Run: pkg install ttyd, or use --no-terminal.'), ('node/npm', 'Run: pkg install nodejs')):
            self.assertEqual(checks[name]['fix'], fix)

    def test_closed_socket_table_is_not_a_failed_check(self):
        self.ports.side_effect = PermissionError('Android')
        self.assertTrue(all(item['ok'] for item in self.plan()['checks']))

    def test_apply_writes_once_before_restart_and_ignores_failure(self):
        plan = self.plan('--push', '--build-ui')
        events = []
        self.run.side_effect = lambda command, **kwargs: events.append(command)

        def probe(*command):
            events.append(list(command))
            for name in plan['units']:
                path = installer.unit_path(self.units, name)
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                logger = path.parent / 'log/run'
                self.assertEqual(logger.stat().st_mode & 0o777, 0o755)
                log = shlex.quote(str(self.prefix / 'var/log/sv' / installer.runit_name(name)))
                self.assertEqual(logger.read_text(), f'#!{self.prefix}/bin/sh\nmkdir -p {log}\nexec svlogd -tt {log}\n')
            return subprocess.CompletedProcess(command, 1, '', 'no supervise yet')

        self.probe.side_effect = probe
        with mock.patch.object(installer, 'write_units', wraps=installer.write_units) as write:
            self.apply(plan)
        write.assert_called_once_with(plan, self.units)
        self.assertEqual(events, plan['commands'])
        self.assertEqual(events[-2][0:2], ['sv', 'restart'])
        state = json.loads(self.state.read_text())
        self.assertEqual(state['unit_names'], sorted(plan['units']))

    def test_deferred_restart_writes_without_sv(self):
        plan = self.plan('--defer-restart')
        self.assertFalse(any(command[0] == 'sv' for command in plan['commands']))
        with mock.patch.object(installer, 'write_units', wraps=installer.write_units) as write:
            self.apply(plan)
        write.assert_called_once_with(plan, self.units)
        self.assertTrue((self.units / 'tether/run').is_file())
        self.probe.assert_not_called()

    def test_removal_happens_after_down_before_restart(self):
        self.apply(self.plan())
        (self.units / 'tether-terminal/run').unlink()
        plan = self.plan('--no-terminal')
        removed = self.units / 'tether-terminal'
        seen = []

        def probe(*command):
            seen.append(list(command))
            self.assertEqual(removed.exists(), command[1] == 'down')

        self.probe.side_effect = probe
        with mock.patch.object(installer, 'write_units', wraps=installer.write_units) as write:
            self.apply(plan)
        write.assert_called_once_with(plan, self.units)
        self.assertEqual(seen, [['sv', 'down', str(removed)], ['sv', 'restart', str(self.units / 'tether')]])

    def test_uninstall_stops_installed_units_and_removes_folders_and_state(self):
        self.apply(self.plan())
        # State still selects an installed unit whose run file went missing.
        (self.units / 'tether-terminal/run').unlink()
        plan = self.plan('--uninstall')
        self.assertEqual(plan['commands'], [['sv', 'down', str(self.units / name)] for name in ('tether', 'tether-terminal')])
        self.probe.reset_mock()
        self.probe.side_effect = lambda *command: self.assertTrue((self.units / 'tether').exists())
        self.apply(plan)
        self.assertFalse((self.units / 'tether').exists())
        self.assertFalse((self.units / 'tether-terminal').exists())
        self.assertFalse(self.state.exists())
        self.assertEqual([list(call.args) for call in self.probe.call_args_list], plan['commands'])

    def test_bind_conflict_trusts_live_owned_pid_when_tcp_is_closed(self):
        self.socket.return_value.__enter__.return_value.bind.side_effect = OSError('in use')
        pid = self.units / 'tether/supervise/pid'
        pid.parent.mkdir(parents=True)
        pid.write_text(str(os.getpid()))
        read = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == Path('/proc/net/tcp'):
                raise PermissionError('Android')
            return read(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', read_text):
            self.assertTrue(installer.free_port(7683, set(), set(), 'tether.service'))
            self.assertFalse(installer.free_port(7683, set(), {7683}, 'tether.service'))
            pid.unlink()
            self.assertFalse(installer.free_port(7683, set(), set(), 'tether.service'))
        self.probe.assert_not_called()

    def test_owned_port_rejects_invalid_dead_and_foreign_pids(self):
        pid = self.units / 'tether/supervise/pid'
        pid.parent.mkdir(parents=True)
        for value in ('invalid', '0', '-1', '999999999999999'):
            pid.write_text(value)
            self.assertFalse(installer.owned_port('tether.service', 7683))
        pid.write_text(str(os.getpid()))
        with mock.patch.object(installer.os, 'getuid', return_value=os.getuid() + 1):
            self.assertFalse(installer.owned_port('tether.service', 7683))

    def test_cli_restart_matches_logical_names_and_skips_timers(self):
        names = [*installer.UNIT_NAMES, *installer.UPDATE_UNITS, 'foreign.service']
        for name in names:
            self.assertEqual(cli.runit_name(name), installer.runit_name(name))
        cli.restart({'unit_names': names[1:]})
        self.assertEqual(self.cli_run.call_args_list,
                         [mock.call(['sv', 'restart', str(self.units / installer.runit_name(name))]) for name in installer.UNIT_NAMES])

    def test_cli_snapshot_restores_service_files_and_permissions(self):
        self.apply(self.plan())
        before = cli.snapshot(self.state)
        self.assertTrue(any(path == self.units / 'tether/log/run' for path in before[1]))
        self.assertFalse(any('systemd' in str(path) for path in before[1]))
        (self.units / 'tether/run').unlink()
        (self.units / 'tether/log/run').write_text('changed')
        cli.restore(self.state, before)
        self.assertEqual(cli.snapshot(self.state), before)
        self.assertEqual((self.units / 'tether/run').stat().st_mode & 0o777, 0o755)

    def test_cli_activation_rolls_back_runit_files_after_failed_health_check(self):
        self.apply(self.plan())
        root = self.home / '.local/share/tether'
        old, target = root / 'releases/0.1.0', root / 'releases/0.2.0'
        old.mkdir(parents=True)
        target.mkdir()
        link = root / 'current'
        link.symlink_to(old)
        before = cli.snapshot(self.state)

        def run(command, **kwargs):
            if '--apply' in command:
                (self.units / 'tether/run').write_text('new release')
                (self.units / 'tether-terminal/log/run').unlink()

        self.cli_run.side_effect = run
        with mock.patch.object(cli, 'healthy', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'rolled back to 0.1.0'):
                cli.activate(root, target, self.state, json.loads(self.state.read_text()))
        self.assertEqual(cli.snapshot(self.state), before)
        self.assertEqual(link.resolve(), old)
        commands = [call.args[0] for call in self.cli_run.call_args_list]
        restarts = [['sv', 'restart', str(self.units / name)] for name in ('tether-terminal', 'tether')]
        self.assertEqual(commands[1:], restarts * 2)
        self.assertIn('--defer-restart', commands[0])

    def test_service_text_without_environment_and_plan_output(self):
        command = ['a b', "c'd"]
        self.assertEqual(installer.service_file('tether.service', 'Test', ['a b', "c'd"], {}),
                         f'#!{self.prefix}/bin/sh\n# Test (written by Tether install.py)\nexec 2>&1\nexec {shlex.join(command)}\n')
        self.apply(self.plan())
        plan = self.plan('--no-terminal')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.print_plan(plan)
        text = output.getvalue()
        self.assertIn('Write $PREFIX/var/service/tether/run', text.splitlines())
        self.assertIn('Remove $PREFIX/var/service/tether-terminal\n', text)
        self.assertIn('Termux services run while Termux runs. Run termux-wake-lock so Android does not stop them. '
                      'Install the Termux:Boot app to start them after a reboot.', text)
        self.assertNotIn('loginctl', text)
        self.assertNotIn('#!', text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.print_plan(plan, verbose=True)
        self.assertIn(plan['units']['tether.service'], output.getvalue())

    def test_linux_plan_has_no_runit_paths_or_commands(self):
        with mock.patch.object(installer, 'TERMUX', False):
            plan = self.plan()
        self.assertEqual(plan['units_dir'], str(self.home / '.config/systemd/user'))
        self.assertFalse(any(command[0] == 'sv' for command in plan['commands']))
        self.assertNotIn('var/service', json.dumps(plan))

    def test_darwin_plan_has_no_runit_paths_or_commands(self):
        with mock.patch.object(installer, 'TERMUX', False), mock.patch.object(installer, 'DARWIN', True):
            plan = self.plan()
        self.assertEqual(plan['units_dir'], str(self.home / 'Library/LaunchAgents'))
        self.assertFalse(any(command[0] == 'sv' for command in plan['commands']))
        self.assertNotIn('var/service', json.dumps(plan))

    def test_shell_and_task_temp_fallbacks(self):
        import shell_log
        import live

        def module_copy(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with mock.patch.object(os, 'access', return_value=False), \
                mock.patch.object(installer.shutil, 'which', return_value=str(self.prefix / 'bin/bash')):
            shell = module_copy('termux_shell_log', shell_log.__file__)
            self.assertEqual(shell.BASH, str(self.prefix / 'bin/bash'))
            self.assertEqual(shell.login_shell(), shell.BASH)
        original = Path.is_dir
        with mock.patch.object(Path, 'is_dir', lambda path: False if path == Path('/tmp') else original(path)), \
                mock.patch('tempfile.gettempdir', return_value=str(self.prefix / 'tmp')):
            tasks = module_copy('termux_live', live.__file__)
        self.assertEqual(tasks._TMP, self.prefix / 'tmp')
        state = {}
        tasks._consume(state, dict(cwd='/work', sessionId='session', type='other'))
        self.assertEqual(state['output_dir'], self.prefix / f'tmp/claude-{os.getuid()}/-work/session/tasks')


if __name__ == '__main__':
    unittest.main()
