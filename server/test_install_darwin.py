"""LaunchAgent plans and applies use temporary homes and mocked processes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('darwin_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
loader = importlib.machinery.SourceFileLoader('darwin_tether_cli', str(ROOT / 'tether'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class DarwinInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'home'
        self.home.mkdir()
        self.repo = Path(temporary.name) / 'repo with spaces'
        (self.repo / 'ui/src').mkdir(parents=True)
        (self.repo / 'ui/dist').mkdir()
        (self.repo / 'ui/dist/index.html').write_text('built')
        self.units = self.home / 'Library/LaunchAgents'
        self.state = self.home / '.config/tether/install.json'
        self.domain = f'gui/{os.getuid()}'
        self.patch(mock.patch.dict(os.environ, HOME=str(self.home), PATH='/usr/bin:/bin', clear=True))
        self.patch(mock.patch.object(installer, 'DARWIN', True))
        self.patch(mock.patch.object(cli, 'DARWIN', True))
        self.process = self.patch(mock.patch.object(subprocess, 'run', side_effect=AssertionError('Unmocked process')))
        self.probe = self.patch(mock.patch.object(installer, 'probe', return_value=self.result()))
        self.run = self.patch(mock.patch.object(installer, 'run'))
        self.cli_run = self.patch(mock.patch.object(cli, 'run'))
        self.socket = self.patch(mock.patch.object(installer.socket, 'socket'))
        self.patch(mock.patch.object(installer.shutil, 'which', side_effect=lambda name: f'/mock/bin/{name}'))
        self.legacy = self.patch(mock.patch.object(installer, 'legacy_settings', side_effect=AssertionError('Linux only')))
        self.wait_gone = self.patch(mock.patch.object(installer, 'launchd_wait_gone', return_value=True))
        self.cli_gone = self.patch(mock.patch.object(cli, 'launchd_gone', return_value=True))

    def patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def result(self, output='', code=0):
        return subprocess.CompletedProcess([], code, output, '')

    def plan(self, *args):
        return installer.make_plan(installer.parser().parse_args(['--plan', *args]), repo=self.repo, home=self.home)

    def plist(self, plan, name='tether.service'):
        return plistlib.loads(plan['units'][name].encode())

    def apply(self, plan):
        installer.apply_plan(plan, home=self.home)

    def test_default_plan_labels_paths_arguments_environment_and_commands(self):
        plan = self.plan()
        self.assertEqual(set(plan['units']), {'tether.service', 'tether-terminal.service'})
        self.assertEqual(plan['units_dir'], str(self.units))
        self.assertEqual(plan['legacy_units'], [])
        self.assertTrue(all(check['ok'] for check in plan['checks']))
        self.assertEqual(self.plist(plan)['ProgramArguments'][:2],
                         [os.path.abspath(sys.executable), str(self.repo / 'server/server.py')])
        for name in plan['units']:
            data = self.plist(plan, name)
            label = installer.launchd_label(name)
            self.assertEqual(data['Label'], label)
            self.assertTrue(data['KeepAlive'])
            self.assertTrue(data['RunAtLoad'])
            self.assertEqual(data['ThrottleInterval'], 3)
            self.assertEqual(data['StandardOutPath'], str(self.home / 'Library/Logs/tether' / (label + '.log')))
            self.assertEqual(data['StandardErrorPath'], data['StandardOutPath'])
            self.assertEqual(data['EnvironmentVariables']['PATH'], '/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin')
            self.assertTrue(all(isinstance(value, str) for value in data['EnvironmentVariables'].values()))
        labels = ['local.tether.terminal', 'local.tether.chat']
        expected = [['launchctl', 'bootout', f'{self.domain}/{label}'] for label in labels]
        expected += [['launchctl', 'bootstrap', self.domain, str(self.units / (label + '.plist'))] for label in labels]
        self.assertEqual(plan['commands'], expected)
        self.assertFalse(any('systemctl' in call.args for call in self.probe.call_args_list))
        self.legacy.assert_not_called()

    def test_auto_updates_use_calendar_without_timer(self):
        plan = self.plan('--updates', 'auto')
        self.assertNotIn('tether-update.timer', plan['units'])
        self.assertNotIn('tether-update.timer', plan['remove_units'])
        data = self.plist(plan, 'tether-update.service')
        self.assertEqual(data['Label'], 'local.tether.update')
        self.assertEqual(data['StartCalendarInterval'], {'Hour': 4, 'Minute': 17})
        self.assertFalse(data['RunAtLoad'])
        self.assertNotIn('KeepAlive', data)
        self.assertNotIn('ThrottleInterval', data)
        self.assertEqual(data['ProgramArguments'][-3:], [str(self.repo / 'tether'), 'update', '--auto'])
        self.apply(plan)
        self.assertEqual(json.loads(self.state.read_text())['unit_names'], sorted(plan['units']))

    def test_apply_ignores_bootout_failure_and_writes_once_before_bootstrap(self):
        plan = self.plan()
        events = []
        write_text = Path.write_text

        def write(path, content, *args, **kwargs):
            if path.suffix == '.plist':
                self.assertTrue((self.home / 'Library/Logs/tether').is_dir())
                events.append(('write', path.name))
            return write_text(path, content, *args, **kwargs)

        def bootout(*command):
            self.assertEqual(command[:2], ('launchctl', 'bootout'))
            events.append(('bootout', command[-1]))
            return self.result(code=1)

        def bootstrap(command, **kwargs):
            self.assertEqual(command[:2], ['launchctl', 'bootstrap'])
            for name, content in plan['units'].items():
                self.assertEqual(installer.unit_path(self.units, name).read_text(), content)
            events.append(('bootstrap', command[-1]))

        self.probe.side_effect = bootout
        self.run.side_effect = bootstrap
        with mock.patch.object(Path, 'write_text', autospec=True, side_effect=write):
            self.apply(plan)
        self.assertEqual([event[0] for event in events], ['bootout', 'bootout', 'write', 'write', 'bootstrap', 'bootstrap'])
        self.assertEqual({event[1] for event in events if event[0] == 'write'},
                         {'local.tether.chat.plist', 'local.tether.terminal.plist'})

    def test_defer_restart_writes_after_package_and_build_commands(self):
        plan = self.plan('--defer-restart', '--voice', '--push', '--build-ui')
        self.assertFalse(any(command[0] in ('launchctl', 'systemctl') for command in plan['commands']))
        self.assertEqual(plan['commands'][-2:], [['npm', 'ci'], ['npm', 'run', 'build']])
        self.run.side_effect = lambda *args, **kwargs: self.assertFalse(self.units.exists())
        self.apply(plan)
        self.assertTrue((self.units / 'local.tether.voice.plist').is_file())
        self.assertTrue(self.state.is_file())
        self.assertEqual(self.plist(plan, 'tether-voice.service')['EnvironmentVariables']['PATH'],
                         '/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin')

    def test_uninstall_boots_out_installed_units_and_removes_plists_and_state(self):
        plan = self.plan('--voice', '--updates', 'auto')
        self.apply(plan)
        (self.units / 'foreign.plist').write_text('keep')
        self.run.reset_mock()
        uninstall = self.plan('--uninstall')
        self.assertEqual(uninstall['units'], {})
        self.assertEqual(set(uninstall['remove_units']), set(plan['units']))
        self.assertEqual({tuple(command) for command in uninstall['commands']},
                         {('launchctl', 'bootout', f'{self.domain}/{installer.launchd_label(name)}') for name in plan['units']})
        self.apply(uninstall)
        self.assertFalse(self.state.exists())
        self.assertEqual([path.name for path in self.units.iterdir()], ['foreign.plist'])
        self.assertFalse(any(call.args[0][0] == 'systemctl' for call in self.run.call_args_list))

    def test_uninstall_also_boots_out_saved_unit_with_missing_plist(self):
        self.apply(self.plan('--updates', 'auto'))
        (self.units / 'local.tether.update.plist').unlink()
        plan = self.plan('--uninstall')
        self.assertIn(['launchctl', 'bootout', f'{self.domain}/local.tether.update'], plan['commands'])
        self.apply(plan)
        self.assertFalse(self.state.exists())

    def test_disabled_units_boot_out_then_remove_before_bootstrap(self):
        self.apply(self.plan('--updates', 'auto'))
        plan = self.plan('--no-terminal', '--updates', 'off')
        self.assertEqual(plan['remove_units'], ['tether-terminal.service', 'tether-update.service'])
        self.assertEqual([command[1] for command in plan['commands']], ['bootout', 'bootout', 'bootout', 'bootstrap'])

        def bootstrap(command, **kwargs):
            for label in ('terminal', 'update'):
                self.assertFalse((self.units / f'local.tether.{label}.plist').exists())

        self.run.side_effect = bootstrap
        self.apply(plan)

    def test_gui_session_failure_prevents_apply(self):
        self.probe.side_effect = lambda *command: self.result(code=1 if command[0] == 'launchctl' else 0)
        plan = self.plan()
        check = next(check for check in plan['checks'] if check['name'] == 'launchd GUI session')
        self.assertFalse(check['ok'])
        self.assertEqual(check['fix'], 'Log in to the Mac desktop as this user, and run the installer from Terminal there, not over SSH.')
        with self.assertRaisesRegex(ValueError, 'Required checks failed'):
            self.apply(plan)
        self.run.assert_not_called()
        self.assertFalse(self.units.exists())

    def test_tmux_homebrew_fallback_and_python_check(self):
        with mock.patch.object(installer.shutil, 'which', return_value=None), \
                mock.patch.object(installer.os, 'access', side_effect=lambda path, mode: path == '/opt/homebrew/bin/tmux'), \
                mock.patch.object(installer.sys, 'version_info', (3, 9)):
            plan = self.plan()
        self.assertTrue(next(check['ok'] for check in plan['checks'] if check['name'] == 'tmux'))
        check = next(check for check in plan['checks'] if check['name'] == 'Python >= 3.11')
        self.assertFalse(check['ok'])
        self.assertEqual(check['fix'], 'Use Python 3.11 or newer, for example: brew install python, then run /opt/homebrew/bin/python3 install.py.')

    def test_path_is_not_duplicated_and_voice_prompt_survives_reapply(self):
        with mock.patch.dict(os.environ, PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin', TETHER_WHISPER_PROMPT='Keep this prompt'):
            plan = self.plan('--voice')
        self.apply(plan)
        repeated = self.plan('--voice')
        env = self.plist(repeated, 'tether-voice.service')['EnvironmentVariables']
        self.assertEqual(env['TETHER_WHISPER_PROMPT'], 'Keep this prompt')
        self.assertEqual(self.plist(plan)['EnvironmentVariables']['PATH'], '/opt/homebrew/bin:/usr/local/bin:/usr/bin')
        self.legacy.assert_not_called()

    def test_service_file_omits_empty_environment(self):
        data = plistlib.loads(installer.service_file('tether.service', 'Chat', ['python', 'server.py'], {}).encode())
        self.assertNotIn('EnvironmentVariables', data)

    def test_replace_legacy_never_probes_legacy_port_owners(self):
        self.apply(self.plan('--voice'))
        with mock.patch.object(installer, 'occupied_ports', return_value={7684}), \
                mock.patch.object(installer, 'owned_port', return_value=False) as owner:
            plan = self.plan('--voice', '--replace-legacy')
        self.assertEqual(plan['legacy_units'], [])
        self.assertTrue(owner.called)
        self.assertFalse(any(call.args[0].startswith('mobile-tmux') for call in owner.call_args_list))
        self.legacy.assert_not_called()

    def test_lsof_ports_and_failures(self):
        self.probe.return_value = self.result('p123\nn127.0.0.1:7683\nn*:7684\nn[::1]:7685\nn*:7683\n')
        self.assertEqual(installer.occupied_ports(), {7683, 7684, 7685})
        self.probe.assert_called_once_with('lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-F', 'n')
        self.probe.return_value = self.result('n*:7683', code=1)
        self.assertEqual(installer.occupied_ports(), set())
        self.probe.return_value = None
        self.assertEqual(installer.occupied_ports(), set())

    def test_owned_port_reads_launchctl_pid_and_lsof(self):
        self.probe.side_effect = [self.result('service = {\n\tpid = 123\n}\n'), self.result('p123\n')]
        self.assertTrue(installer.owned_port('tether.service', 7683))
        self.assertEqual(self.probe.call_args_list, [
            mock.call('launchctl', 'print', f'{self.domain}/local.tether.chat'),
            mock.call('lsof', '-nP', '-a', '-p', '123', '-iTCP:7683', '-sTCP:LISTEN', '-F', 'p')])
        for results in ([None], [self.result(code=1)], [self.result('pid = 0')], [self.result('pid = abc')],
                        [self.result('pid = 123'), None], [self.result('pid = 123'), self.result('p123', code=1)],
                        [self.result('pid = 123'), self.result('')]):
            with self.subTest(results=results):
                self.probe.side_effect = results
                self.assertFalse(installer.owned_port('tether.service', 7683))

    def test_free_port_requires_loopback_bind_without_reuse(self):
        wildcard, loopback = mock.MagicMock(), mock.MagicMock()
        self.socket.side_effect = [wildcard, loopback]
        self.assertTrue(installer.free_port(7683, set(), set()))
        wildcard.__enter__.return_value.bind.assert_called_once_with(('0.0.0.0', 7683))
        loopback.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', 7683))
        loopback.__enter__.return_value.setsockopt.assert_not_called()
        self.socket.side_effect = [wildcard, loopback]
        loopback.__enter__.return_value.bind.side_effect = OSError('Address in use')
        self.assertFalse(installer.free_port(7683, set(), set()))

    def test_print_plan_uses_launchagents_and_desktop_notice(self):
        plan = self.plan()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.print_plan(plan)
        self.assertIn('Write ~/Library/LaunchAgents/local.tether.chat.plist', output.getvalue().splitlines())
        self.assertNotIn('<?xml', output.getvalue())
        self.assertNotIn('<plist', output.getvalue())
        self.assertIn('LaunchAgents run while you are logged in to the Mac desktop.', output.getvalue())
        self.assertNotIn('loginctl', output.getvalue())
        self.apply(plan)
        for verbose in (False, True):
            with self.subTest(verbose=verbose), contextlib.redirect_stdout(io.StringIO()) as output:
                installer.print_plan(plan, verbose=verbose)
            self.assertIn('Keep ~/Library/LaunchAgents/local.tether.chat.plist', output.getvalue().splitlines())
            self.assertEqual(plan['units']['tether.service'] in output.getvalue(), verbose)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.print_plan(self.plan('--uninstall'))
        self.assertIn('Remove ~/Library/LaunchAgents/local.tether.chat.plist', output.getvalue())

    def test_cli_restart_labels_and_bootout_failures(self):
        names = ['tether.service', 'tether-terminal.service', 'tether-voice.service', 'tether-update.service']
        for name in names:
            self.assertEqual(cli.launchd_label(name), installer.launchd_label(name))

        def restart(command, **kwargs):
            if command[1] == 'bootout':
                raise subprocess.CalledProcessError(1, command)

        self.cli_run.side_effect = restart
        cli.restart({'unit_names': [*names[1:], 'foreign.service']})
        expected = []
        for name in names:
            label = cli.launchd_label(name)
            expected.extend([mock.call(['launchctl', 'bootout', f'{self.domain}/{label}'], quiet=True),
                             mock.call(['launchctl', 'bootstrap', self.domain, str(self.units / (label + '.plist'))])])
        self.assertEqual(self.cli_run.call_args_list, expected)

    def test_apply_waits_for_each_booted_out_job_before_bootstrap(self):
        plan = self.plan()
        events = []
        self.probe.side_effect = lambda *command: events.append(('bootout', command[-1])) or self.result(code=0)
        self.wait_gone.side_effect = lambda target: events.append(('wait', target)) or True
        self.run.side_effect = lambda command, **kwargs: events.append(('bootstrap', command[-1]))
        self.apply(plan)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ['bootout', 'wait', 'bootout', 'wait', 'bootstrap', 'bootstrap'])
        self.assertCountEqual([target for kind, target in events if kind == 'wait'],
                              [f'{self.domain}/local.tether.chat', f'{self.domain}/local.tether.terminal'])
        for i, (kind, target) in enumerate(events):  # each wait is for the job just booted out
            if kind == 'wait':
                self.assertEqual(events[i - 1], ('bootout', target))

    def test_apply_retries_a_bootstrap_that_hits_the_unload_race(self):
        plan = self.plan()
        failures = {'local.tether.chat.plist': 2}

        def bootstrap(command, **kwargs):
            name = Path(command[-1]).name
            if failures.get(name):
                failures[name] -= 1
                raise subprocess.CalledProcessError(5, command)
        self.run.side_effect = bootstrap
        with mock.patch.object(installer.time, 'sleep') as sleep:
            self.apply(plan)
        self.assertEqual(failures['local.tether.chat.plist'], 0)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(self.state.exists())  # choices are saved once the services are back

    def test_apply_gives_up_after_repeated_bootstrap_failures(self):
        plan = self.plan()
        self.run.side_effect = lambda command, **kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(5, command))
        with mock.patch.object(installer.time, 'sleep'), self.assertRaises(subprocess.CalledProcessError):
            self.apply(plan)
        self.assertEqual(self.run.call_count, installer.LAUNCHD_BOOTSTRAP_TRIES)

    def test_cli_restart_waits_between_bootout_and_bootstrap(self):
        events = []
        self.cli_run.side_effect = lambda command, **kwargs: events.append(command[1])
        self.cli_gone.side_effect = lambda target: events.append('wait') or True
        cli.restart({'unit_names': ['tether.service']})
        self.assertEqual(events, ['bootout', 'wait', 'bootstrap'])

    def test_cli_restart_retries_bootstrap_error_5(self):
        tries = []

        def run(command, **kwargs):
            if command[1] == 'bootstrap':
                tries.append(1)
                if len(tries) < 3:
                    raise subprocess.CalledProcessError(5, command)
        self.cli_run.side_effect = run
        with mock.patch.object(cli.time, 'sleep'):
            cli.restart({'unit_names': ['tether.service']})
        self.assertEqual(len(tries), 3)

    def test_cli_restart_inside_update_job_keeps_the_update_job(self):
        self.cli_run.side_effect = None
        with mock.patch.dict(os.environ, {'XPC_SERVICE_NAME': 'local.tether.update'}):
            cli.restart({'unit_names': ['tether.service', 'tether-update.service']})
        commands = [call.args[0] for call in self.cli_run.call_args_list]
        self.assertNotIn(['launchctl', 'bootout', f'{self.domain}/local.tether.update'], commands)
        self.assertIn(['launchctl', 'bootout', f'{self.domain}/local.tether.chat'], commands)

    def test_cli_snapshot_restores_plists_after_failed_update_health_check(self):
        self.apply(self.plan('--updates', 'auto'))
        root = self.home / '.local/share/tether'
        old, target = root / 'releases/0.1.0', root / 'releases/0.2.0'
        old.mkdir(parents=True)
        target.mkdir()
        link = root / 'current'
        link.symlink_to(old)
        before = cli.snapshot(self.state)
        self.assertIn(self.units / 'local.tether.update.plist', before[1])
        self.assertFalse(any('.config/systemd' in str(path) for path in before[1]))

        def update(command, **kwargs):
            if '--apply' in command:
                (self.units / 'local.tether.chat.plist').write_text('changed')
                (self.units / 'local.tether.update.plist').unlink()

        self.cli_run.side_effect = update
        with mock.patch.object(cli, 'healthy', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'rolled back to 0.1.0'):
                cli.activate(root, target, self.state, json.loads(self.state.read_text()))
        self.assertEqual(cli.snapshot(self.state), before)
        self.assertEqual(link.resolve(), old)

    def test_linux_plan_has_units_dir_and_no_launchctl(self):
        with mock.patch.object(installer, 'DARWIN', False), \
                mock.patch.object(installer, 'occupied_ports', return_value=set()):
            plan = self.plan()
        self.assertEqual(plan['units_dir'], str(self.home / '.config/systemd/user'))
        self.assertFalse(any(command[0] == 'launchctl' for command in plan['commands']))
        self.assertTrue(plan['units']['tether.service'].startswith('[Unit]\n'))
        self.assertEqual([check['name'] for check in plan['checks']][:3],
                         ['Linux', 'Python >= 3.11', 'systemd user session'])


class LaunchdHelperTests(unittest.TestCase):
    """The real helpers, with launchctl and the clock replaced."""
    done = subprocess.CompletedProcess([], 113, '', 'Could not find service')
    loaded = subprocess.CompletedProcess([], 0, 'state = running', '')

    def test_wait_gone_returns_at_once_when_the_job_is_not_loaded(self):
        sleeps = []
        with mock.patch.object(installer, 'probe', return_value=self.done) as probe:
            self.assertTrue(installer.launchd_wait_gone('gui/1/local.tether.chat', sleep=sleeps.append))
        probe.assert_called_once_with('launchctl', 'print', 'gui/1/local.tether.chat')
        self.assertEqual(sleeps, [])

    def test_wait_gone_polls_until_launchd_drops_the_job(self):
        answers, sleeps = iter([self.loaded, self.loaded, self.done]), []
        with mock.patch.object(installer, 'probe', side_effect=lambda *a: next(answers)):
            self.assertTrue(installer.launchd_wait_gone('gui/1/x', sleep=sleeps.append))
        self.assertEqual(len(sleeps), 2)

    def test_wait_gone_gives_up_after_the_settle_time(self):
        sleeps = []
        with mock.patch.object(installer, 'probe', return_value=self.loaded):
            self.assertFalse(installer.launchd_wait_gone('gui/1/x', seconds=1, sleep=sleeps.append))
        self.assertEqual(len(sleeps), 4)

    def test_wait_gone_treats_a_missing_launchctl_as_gone(self):
        with mock.patch.object(installer, 'probe', return_value=None):
            self.assertTrue(installer.launchd_wait_gone('gui/1/x', sleep=lambda s: None))

    def test_bootstrap_retries_then_succeeds_or_raises(self):
        calls = []

        def flaky(command, **kwargs):
            calls.append(command)
            if len(calls) < 3:
                raise subprocess.CalledProcessError(5, command)
        with mock.patch.object(installer, 'run', side_effect=flaky):
            installer.launchd_bootstrap(['launchctl', 'bootstrap', 'gui/1', 'a.plist'], sleep=lambda s: None)
        self.assertEqual(len(calls), 3)
        with mock.patch.object(installer, 'run', side_effect=subprocess.CalledProcessError(5, [])), \
                self.assertRaises(subprocess.CalledProcessError):
            installer.launchd_bootstrap(['launchctl', 'bootstrap', 'gui/1', 'a.plist'], tries=2, sleep=lambda s: None)

    def test_cli_launchd_gone_reads_launchctl_print(self):
        with mock.patch.object(cli.subprocess, 'run', return_value=self.done) as run:
            self.assertTrue(cli.launchd_gone('gui/1/x'))
        self.assertEqual(run.call_args.args[0], ['launchctl', 'print', 'gui/1/x'])
        with mock.patch.object(cli.subprocess, 'run', side_effect=OSError), mock.patch.object(cli.time, 'sleep'):
            self.assertTrue(cli.launchd_gone('gui/1/x'))


class NewerPythonTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bin = Path(temporary.name)

    def exe(self, name):
        path = self.bin / name
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)
        return str(path)

    def test_a_new_enough_python_is_kept(self):
        self.assertIsNone(cli.newer_python(current=(3, 11, 0), candidates=[], version_of=lambda p: (3, 15)))
        self.assertIsNone(cli.newer_python(current=(3, 15, 2), candidates=[], version_of=lambda p: (3, 15)))

    def test_an_old_python_finds_the_first_new_enough_one(self):
        old, new = self.exe('python3.9'), self.exe('python3.15')
        versions = {old: (3, 9), new: (3, 15)}
        self.assertEqual(cli.newer_python(current=(3, 9, 6), candidates=[old, new], version_of=versions.get), new)

    def test_non_executables_and_old_ones_are_skipped(self):
        plain = self.bin / 'python3.14'
        plain.write_text('')  # not executable
        old = self.exe('python3.10')
        self.assertIsNone(cli.newer_python(current=(3, 9, 6), candidates=[str(plain), old, '/no/such/python'],
                                           version_of=lambda p: (3, 10)))

    def test_an_unreadable_version_does_not_count(self):
        broken = self.exe('python3')
        self.assertIsNone(cli.newer_python(current=(3, 9, 6), candidates=[broken], version_of=lambda p: ()))


if __name__ == '__main__':
    unittest.main()
