"""Installer tests never use the real user manager or fixed production ports."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('tether_installer', Path(__file__).resolve().parents[1] / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.repo = Path(self.temp.name) / 'repo with spaces'
        (self.repo / 'ui/src').mkdir(parents=True)
        (self.repo / 'ui/dist').mkdir()
        (self.repo / 'ui/dist/index.html').write_text('built')
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        self.log = Path(self.temp.name) / 'calls.jsonl'
        script = ('#!' + sys.executable + '\n'
                  'import json, os, pathlib, sys\n'
                  'with open(os.environ["INSTALL_TEST_LOG"], "a") as log:\n'
                  '    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")\n'
                  'if pathlib.Path(sys.argv[0]).name == "systemctl" and "is-active" in sys.argv:\n'
                  '    print("active")\n'
                  'if pathlib.Path(sys.argv[0]).name == "systemctl" and "show" in sys.argv:\n'
                  '    print("0")\n'
                  'if pathlib.Path(sys.argv[0]).name == "npm" and "build" in sys.argv:\n'
                  '    pathlib.Path("dist").mkdir(exist_ok=True)\n'
                  '    pathlib.Path("dist/index.html").write_text("built")\n'
                  '    print("build output")\n'
                  'if pathlib.Path(sys.argv[0]).name == "systemctl" and any(arg in sys.argv for arg in ("enable", "restart", "daemon-reload", "disable")):\n'
                  '    print("service output")\n')
        for name in ('systemctl', 'npm', 'node', 'tmux', 'ttyd'):
            path = self.bin / name
            path.write_text(script)
            path.chmod(0o755)
        patch = mock.patch.dict(os.environ, HOME=str(self.home), PATH=str(self.bin), INSTALL_TEST_LOG=str(self.log))
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('TETHER_WHISPER_PROMPT', None)
        os.environ.pop('MOBILE_TMUX_WHISPER_PROMPT', None)
        # Bind only a kernel-assigned port; never bind or probe port 7683.
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.sock.close()

    def plan(self, *args):
        parsed = installer.parser().parse_args(['--plan', '--port', str(self.port), '--no-terminal', *args])
        return installer.make_plan(parsed, repo=self.repo, home=self.home)

    def apply(self, plan):
        installer.apply_plan(plan, home=self.home)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_resources_default_saved_and_explicit_off(self):
        self.assertFalse(self.plan()['options']['resources'])
        plan = self.plan('--resources')
        self.assertTrue(plan['options']['resources'])
        self.assertIn('TETHER_RESOURCES=1', plan['units']['tether.service'])
        self.apply(plan)
        state = json.loads((self.home / '.config/tether/install.json').read_text())
        self.assertTrue(state['options']['resources'])
        self.assertTrue(self.plan()['options']['resources'])
        self.assertIn('resources', self.plan()['reused_saved_choices'])
        disabled = self.plan('--no-resources')
        self.assertFalse(disabled['options']['resources'])
        self.assertIn('TETHER_RESOURCES=0', disabled['units']['tether.service'])

    def test_auto_update_reuses_chat_environment(self):
        custom = dict(MOBILE_TMUX_CONFIG=str(self.home / 'custom config'),
                      TETHER_INSTALL_STATE=str(self.home / 'custom state/install.json'),
                      TETHER_REPO='example/tether')
        with mock.patch.dict(os.environ, custom):
            plan = self.plan('--updates', 'auto')
        chat = plan['units']['tether.service']
        update = plan['units']['tether-update.service']
        chat_env = [line for line in chat.splitlines() if line.startswith('Environment=')]
        self.assertEqual(chat_env, [line for line in update.splitlines() if line.startswith('Environment=')])
        for key, value in dict(custom, PATH=str(self.bin)).items():
            self.assertIn('Environment=' + installer.quote(key + '=' + value), update)
        self.assertIn('Type=oneshot', update)
        self.assertNotIn('Restart=always', update)
        self.assertIn('Restart=always', chat)

    def legacy(self, name, content=''):
        directory = self.home / '.config/systemd/user'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(content)

    def test_same_port_legacy_cutover_and_command_order(self):
        self.legacy('mobile-tmux.service', 'Environment=MOBILE_TMUX_PORT=7683\n')
        with mock.patch.object(installer, 'occupied_ports', return_value={7683}), \
                mock.patch.object(installer, 'owned_port', side_effect=lambda unit, port: unit == 'mobile-tmux.service' and port == 7683):
            plan = self.plan('--port', '7683', '--replace-legacy')
            self.assertTrue(all(check['ok'] for check in plan['checks']))
            self.assertFalse(next(check['ok'] for check in self.plan('--port', '7683')['checks']
                                  if check['name'] == 'chat port'))
        with mock.patch.object(installer, 'occupied_ports', return_value=set()), \
                mock.patch.object(installer, 'free_port', return_value=True) as free:
            self.apply(plan)
        free.assert_called_once_with(7683, set(), set(), 'tether.service')
        calls = self.calls()
        stop = calls.index(['systemctl', '--user', 'disable', '--now', 'mobile-tmux.service'])
        reload = calls.index(['systemctl', '--user', 'daemon-reload'])
        enable = calls.index(['systemctl', '--user', 'enable', '--now', 'tether.service'])
        self.assertLess(stop, reload)
        self.assertLess(reload, enable)

    def test_legacy_cutover_rechecks_all_ports_before_start(self):
        self.legacy('mobile-tmux.service')
        with mock.patch.object(installer, 'pick_port', side_effect=[self.port + 1, self.port + 2]), \
                mock.patch.object(installer, 'free_port', return_value=True):
            plan = self.plan('--replace-legacy', '--terminal', '--voice')
        for blocked in (self.port, self.port + 1, self.port + 2):
            with self.subTest(port=blocked), mock.patch.object(installer, 'occupied_ports', return_value={blocked}), \
                    mock.patch.object(installer, 'free_port', side_effect=lambda port, taken, reserved, unit=None: port not in taken), \
                    mock.patch.object(installer, 'run') as run:
                with self.assertRaisesRegex(ValueError, f'Port {blocked} is still taken.*not started'):
                    self.apply(plan)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertTrue(any('disable' in command for command in commands))
                self.assertFalse(any(word in command for command in commands for word in ('restart', 'daemon-reload')))
                started = [command[-1] for command in commands if 'enable' in command]
                self.assertEqual(started, plan['legacy_units'])  # rollback restarts legacy, never tether
                self.assertFalse((self.home / '.config/tether/install.json').exists())

    def test_repeat_replace_legacy_keeps_running_tether_and_never_restarts_legacy(self):
        self.legacy('mobile-tmux.service')
        with mock.patch.object(installer, 'free_port', return_value=True):
            plan = self.plan('--replace-legacy')
        with mock.patch.object(installer, 'occupied_ports', return_value={self.port}), \
                mock.patch.object(installer, 'owned_port', side_effect=lambda unit, port: unit == 'tether.service'):
            self.apply(plan)
        self.assertNotIn(['systemctl', '--user', 'enable', '--now', 'mobile-tmux.service'], self.calls())
        self.assertIn(['systemctl', '--user', 'enable', '--now', 'tether.service'], self.calls())

    def test_failed_legacy_restart_is_reported(self):
        self.legacy('mobile-tmux.service')
        with mock.patch.object(installer, 'free_port', return_value=True):
            plan = self.plan('--replace-legacy')

        def run(command, **kwargs):
            if command[-1] == 'mobile-tmux.service' and 'enable' in command:
                raise subprocess.CalledProcessError(1, command)
        with mock.patch.object(installer, 'occupied_ports', return_value={self.port}), \
                mock.patch.object(installer, 'owned_port', return_value=False), \
                mock.patch.object(installer, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'could not restart mobile-tmux.service'):
                self.apply(plan)

    def test_time_wait_rows_do_not_hold_a_port(self):
        rows = ('  sl  local_address rem_address   st\n'
                '   0: 0100007F:1E03 0100007F:9C40 06 0\n'
                '   1: 0100007F:1E04 00000000:0000 0A 0\n')
        with mock.patch.object(installer.Path, 'read_text', return_value=rows):
            self.assertEqual(installer.occupied_ports(), {0x1E04})

    def test_legacy_default_ports_from_effective_settings_and_unit_files(self):
        settings = {
            'mobile-tmux.service': 'Environment=MOBILE_TMUX_PORT=7683\n',
            'mobile-tmux-terminal.service': 'ExecStart={ path=/bin/ttyd ; argv[]=/bin/ttyd -p 7682 ; ignore_errors=no ; }\n',
            'mobile-tmux-whisper.service': 'Environment=MOBILE_TMUX_TRANSCRIBE_PORT=7684\n',
        }
        for name in settings:
            self.legacy(name)
        original = installer.probe

        def probe(*args):
            if '--property=Environment,ExecStart' in args:
                return subprocess.CompletedProcess(args, 0, stdout=settings[args[3]])
            return original(*args)

        args = installer.parser().parse_args(['--plan', '--replace-legacy', '--voice'])
        before = sorted(str(path) for path in self.home.rglob('*'))
        with mock.patch.object(installer, 'occupied_ports', return_value={7682, 7683, 7684}), \
                mock.patch.object(installer, 'owned_port', side_effect=lambda unit, port: dict(zip(installer.LEGACY_UNITS, (7683, 7682, 7684))).get(unit) == port):
            with mock.patch.object(installer, 'probe', side_effect=probe):
                plan = installer.make_plan(args, repo=self.repo, home=self.home)
            for name, content in settings.items():
                self.legacy(name, content)
            fallback = installer.make_plan(args, repo=self.repo, home=self.home)
        self.assertEqual(plan['options'], fallback['options'])
        self.assertEqual([plan['options'][key] for key in ('port', 'terminal_port', 'voice_port')], [7683, 7682, 7684])
        self.assertTrue(all(check['ok'] for check in plan['checks']))
        self.assertEqual(before, sorted(str(path) for path in self.home.rglob('*')))
        self.assertFalse(any(any(word in call for word in ('disable', 'enable', 'restart', 'daemon-reload'))
                             for call in self.calls()))

    def test_legacy_port_requires_same_user_mainpid_socket(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.listen()
            with mock.patch.object(installer, 'probe', return_value=subprocess.CompletedProcess([], 0, stdout=str(os.getpid()))):
                self.assertTrue(installer.owned_port('mobile-tmux.service', port))
                with mock.patch.object(installer.os, 'getuid', return_value=os.getuid() + 1):
                    self.assertFalse(installer.owned_port('mobile-tmux.service', port))
            with mock.patch.object(installer, 'probe', return_value=subprocess.CompletedProcess([], 0, stdout='0')):
                self.assertFalse(installer.owned_port('mobile-tmux.service', port))

    def test_printed_url_uses_host(self):
        for host, expected in (('localhost', '127.0.0.1'), ('127.0.0.2', '127.0.0.2'),
                               ('192.0.2.10', '192.0.2.10'), ('0.0.0.0', '127.0.0.1')):
            with self.subTest(host=host):
                plan = self.plan('--host', host)
                self.assertEqual(plan['url'], f'http://{expected}:{self.port}/chat/')
                with mock.patch('builtins.print') as output:
                    installer.print_plan(plan)
                self.assertTrue(any(f'URL: {plan["url"]}' in str(call) for call in output.call_args_list))

    def test_plan_rejects_control_characters_in_unit_values(self):
        # The OS rejects NUL in environment variables before planning can start.
        with self.assertRaisesRegex(ValueError, 'unit value.*control character'):
            installer.unit_text('description', ['python'], {'VALUE': 'bad\x00value'})
        for code in (*range(1, 0x20), 0x7f):
            with self.subTest(code=code), mock.patch.dict(os.environ, MOBILE_TMUX_CONFIG='config' + chr(code)):
                with self.assertRaisesRegex(ValueError, 'unit value.*control character'):
                    self.plan()
        with self.assertRaisesRegex(ValueError, 'control character'):
            installer.unit_text('description\nextra', ['python'], {})
        with self.assertRaisesRegex(ValueError, 'control character'):
            installer.unit_text('description', ['python\r'], {})
        self.assertFalse((self.home / '.config').exists())

    def test_voice_prompt_derived_each_run_without_saved_state(self):
        for key in ('TETHER_WHISPER_PROMPT', 'MOBILE_TMUX_WHISPER_PROMPT'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: 'carry these words'}):
                plan = self.plan('--voice')
                self.assertIn('TETHER_WHISPER_PROMPT=carry these words', plan['units']['tether-voice.service'])
                with mock.patch.object(installer, 'run'):
                    self.apply(plan)
                state = (self.home / '.config/tether/install.json').read_text()
                self.assertNotIn('WHISPER_PROMPT', state)
                self.assertNotIn('carry these words', state)
            # A later apply (or release update) without the variable keeps the installed prompt.
            self.assertIn('TETHER_WHISPER_PROMPT=carry these words', self.plan('--voice')['units']['tether-voice.service'])
            (self.home / '.config/systemd/user/tether-voice.service').unlink()
            self.assertNotIn('WHISPER_PROMPT', self.plan('--voice')['units']['tether-voice.service'])
        with mock.patch.dict(os.environ, TETHER_WHISPER_PROMPT='new', MOBILE_TMUX_WHISPER_PROMPT='old'):
            self.assertIn('TETHER_WHISPER_PROMPT=new', self.plan('--voice')['units']['tether-voice.service'])
            self.assertNotIn('tether-voice.service', self.plan('--no-voice')['units'])

    def test_voice_prompt_from_legacy_effective_environment(self):
        self.legacy('mobile-tmux-whisper.service')
        original = installer.probe
        for key in ('TETHER_WHISPER_PROMPT', 'MOBILE_TMUX_WHISPER_PROMPT'):
            def probe(*args):
                if '--property=Environment,ExecStart' in args:
                    return subprocess.CompletedProcess(args, 0, stdout=f'Environment="{key}=legacy words"\n')
                return original(*args)
            with self.subTest(key=key), mock.patch.object(installer, 'probe', side_effect=probe):
                plan = self.plan('--voice')
                self.assertIn('TETHER_WHISPER_PROMPT=legacy words', plan['units']['tether-voice.service'])
                self.assertNotIn('WHISPER_PROMPT', json.dumps(plan['options']))
                with mock.patch.dict(os.environ, TETHER_WHISPER_PROMPT='installer words'):
                    self.assertIn('TETHER_WHISPER_PROMPT=installer words', self.plan('--voice')['units']['tether-voice.service'])

    def test_plan_json_is_deterministic_and_does_not_write(self):
        before = sorted(str(path) for path in self.home.rglob('*'))
        first = self.plan()
        second = self.plan()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(before, sorted(str(path) for path in self.home.rglob('*')))
        self.assertTrue(all(call[1] == '--user' and call[2] in ('show-environment', 'list-unit-files') for call in self.calls()))

    def test_picks_a_port_after_a_bound_socket(self):
        with socket.socket() as sock:
            sock.bind(('0.0.0.0', 0))
            port = sock.getsockname()[1]
            self.assertNotEqual(installer.pick_port(port, installer.occupied_ports(), set()), port)
            plan = self.plan('--port', str(port))
            self.assertFalse(next(check['ok'] for check in plan['checks'] if check['name'] == 'chat port'))
            with self.assertRaisesRegex(ValueError, '1.*Port'):
                self.apply(plan)
        self.assertFalse((self.home / '.config/tether').exists())

    def test_other_users_ipv6_port_counts_as_taken(self):
        with mock.patch.object(installer, 'occupied_ports', return_value={self.port}):
            plan = self.plan()
        self.assertFalse(next(check['ok'] for check in plan['checks'] if check['name'] == 'chat port'))

    def test_default_port_selection_and_terminal_detection(self):
        args = installer.parser().parse_args(['--plan'])
        with mock.patch.object(installer, 'pick_port', side_effect=[self.port, self.port + 1]) as pick:
            plan = installer.make_plan(args, repo=self.repo, home=self.home)
        self.assertEqual(pick.call_args_list[0].args[0], 7683)
        self.assertEqual(plan['options']['port'], self.port)
        self.assertTrue(plan['options']['terminal'])
        self.assertFalse(plan['options']['voice'])
        self.assertFalse(plan['options']['push'])

    def test_reapply_accepts_only_a_port_owned_by_the_saved_service(self):
        self.apply(self.plan())
        original = installer.probe

        def probe(*args):
            if 'show' in args:
                return subprocess.CompletedProcess(args, 0, stdout=str(os.getpid()))
            return original(*args)

        with socket.socket() as sock:
            sock.bind(('0.0.0.0', self.port))
            sock.listen()
            with mock.patch.object(installer, 'probe', side_effect=probe):
                plan = self.plan()
            self.assertTrue(next(check['ok'] for check in plan['checks'] if check['name'] == 'chat port'))
            other = self.plan()
            self.assertFalse(next(check['ok'] for check in other['checks'] if check['name'] == 'chat port'))

    def test_apply_is_idempotent_and_reuses_saved_choices(self):
        plan = self.plan('--hooks')
        self.apply(plan)
        units = self.home / '.config/systemd/user'
        first = {path.name: path.read_text() for path in units.iterdir()}
        args = installer.parser().parse_args(['--plan'])
        second = installer.make_plan(args, repo=self.repo, home=self.home)
        self.assertEqual(second['options'], plan['options'])
        self.assertIn('port', second['reused_saved_choices'])
        self.apply(second)
        self.assertEqual(first, {path.name: path.read_text() for path in units.iterdir()})
        record = json.loads((self.home / '.config/tether/install.json').read_text())
        self.assertEqual(record['unit_names'], ['tether.service'])
        self.assertIn('timestamp', record)
        self.assertIn(str(self.repo / 'server/server.py'), first['tether.service'])
        self.assertIn('MOBILE_TMUX_TTYD_PORT=0', first['tether.service'])
        self.assertIn('MOBILE_TMUX_TRANSCRIBE_URL=http://127.0.0.1:0/transcribe', first['tether.service'])

    def test_legacy_units_are_untouched_unless_replacement_is_requested(self):
        directory = self.home / '.config/systemd/user'
        directory.mkdir(parents=True)
        legacy = directory / 'mobile-tmux.service'
        legacy.write_text('legacy configuration')
        plan = self.plan()
        self.assertEqual(plan['legacy_units'], ['mobile-tmux.service'])
        self.apply(plan)
        self.assertEqual(legacy.read_text(), 'legacy configuration')
        self.assertFalse(any('disable' in call and legacy.name in call for call in self.calls()))
        replacement = self.plan('--replace-legacy')
        self.assertEqual(replacement['legacy_action'], 'disable and stop')
        self.apply(replacement)
        self.assertIn(['systemctl', '--user', 'disable', '--now', legacy.name], self.calls())
        self.assertEqual(legacy.read_text(), 'legacy configuration')

    def test_uninstall_keeps_tokens_data_and_repo(self):
        self.apply(self.plan())
        token = self.home / '.config/mobile-tmux/token'
        token.parent.mkdir(parents=True)
        token.write_text('keep this token')
        plan = self.plan('--uninstall')
        self.assertTrue(token.exists())
        self.apply(plan)
        self.apply(self.plan('--uninstall'))
        self.assertEqual(token.read_text(), 'keep this token')
        self.assertTrue(self.repo.exists())
        self.assertFalse((self.home / '.config/tether/install.json').exists())
        self.assertFalse(list((self.home / '.config/systemd/user').glob('tether*.service')))
        self.assertTrue(any(call[:3] == ['tmux', 'show-hooks', '-g']
                            and call[-1] == 'client-attached' for call in self.calls()))

    def test_required_checks_stop_apply_with_numbered_fixes(self):
        (self.bin / 'tmux').unlink()
        (self.bin / 'ttyd').unlink()
        plan = self.plan('--terminal')
        before = len(self.calls())
        with self.assertRaisesRegex(ValueError, '1.*tmux.*\n2.*ttyd'):
            self.apply(plan)
        self.assertEqual(before, len(self.calls()))
        self.assertFalse((self.home / '.config').exists())

    def test_missing_dist_builds_and_missing_npm_is_clear(self):
        (self.repo / 'ui/dist/index.html').unlink()
        plan = self.plan()
        self.assertIn(['npm', 'ci'], plan['commands'])
        self.apply(plan)
        self.assertTrue((self.repo / 'ui/dist/index.html').exists())
        (self.bin / 'npm').unlink()
        (self.repo / 'ui/dist/index.html').unlink()
        with self.assertRaisesRegex(ValueError, 'Node.js and npm'):
            self.apply(self.plan())
        with self.assertRaisesRegex(ValueError, 'ui/dist'):
            self.apply(self.plan('--no-build-ui'))

    def test_auto_build_detects_newer_source_after_install(self):
        self.apply(self.plan())
        source = self.repo / 'ui/src/main.tsx'
        source.write_text('new source')
        stamp = (self.repo / 'ui/dist/index.html').stat().st_mtime_ns + 1000000
        os.utime(source, ns=(stamp, stamp))
        self.assertIn(['npm', 'run', 'build'], self.plan()['commands'])

    def test_hooks_preserve_existing_status_lines_and_invalid_json(self):
        claude = self.home / '.claude/settings.json'
        agy = self.home / '.gemini/antigravity-cli/settings.json'
        for path in (claude, agy):
            path.parent.mkdir(parents=True)
        claude.write_text('{"other": true}')
        agy.write_text('{"statusLine": {"command": "existing"}}')
        self.apply(self.plan('--hooks'))
        self.assertIn('claude_usage.py', json.loads(claude.read_text())['statusLine']['command'])
        self.assertEqual(json.loads(agy.read_text())['statusLine']['command'], 'existing')
        claude.write_text('invalid JSON')
        self.apply(self.plan('--hooks'))
        self.assertEqual(claude.read_text(), 'invalid JSON')

    def test_claude_hooks_merge_repeat_and_remove_only_tether(self):
        import claude_hooks
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        guards = [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'guard-one'},
                                              {'type': 'command', 'command': 'guard-two'}]}]
        notifiers = [{'hooks': [{'type': 'command', 'command': 'notify'}]}]
        original = {'statusLine': {'command': 'custom'}, 'other': True,
                    'hooks': {'PreToolUse': guards, 'PostToolUse': notifiers,
                              'UnknownEvent': [{'custom': True}]}}
        settings.write_text(json.dumps(original))
        plan = self.plan('--hooks')
        registration = next(hook for hook in plan['hooks'] if 'entries' in hook)
        self.assertEqual(set(registration['entries']), set(claude_hooks.EVENTS))
        self.assertEqual(registration['action'], 'merge')
        self.apply(plan)
        data = json.loads(settings.read_text())
        self.assertEqual(data['hooks']['PreToolUse'][:-1], guards)
        self.assertEqual(data['hooks']['PostToolUse'][:-1], notifiers)
        self.assertEqual(data['statusLine'], original['statusLine'])
        for event, entry in registration['entries'].items():
            self.assertEqual(entry['hooks'][0].get('async', False), event == 'SubagentStop')
        first = settings.read_text()
        self.apply(self.plan('--hooks'))
        self.assertEqual(settings.read_text(), first)
        self.apply(self.plan('--uninstall'))
        self.assertEqual(json.loads(settings.read_text()), original)
        self.apply(self.plan('--uninstall'))
        self.assertEqual(json.loads(settings.read_text()), original)

    def test_claude_status_line_wrap_repeat_and_backup(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        original = {'type': 'command', 'command': 'echo hi', 'padding': 0}
        settings.write_text(json.dumps({'statusLine': original}))
        plan = self.plan('--hooks')
        hook = next(hook for hook in plan['hooks'] if 'value' in hook)
        self.assertEqual(hook['action'], 'wrap')
        self.assertEqual(hook['original'], 'echo hi')
        with mock.patch('builtins.print') as output:
            installer.print_plan(plan)
        output.assert_any_call(f'Hook {settings}: wrap')
        self.apply(plan)
        status = json.loads(settings.read_text())['statusLine']
        self.assertEqual(shlex.split(status['command']),
                         [sys.executable, str(self.repo / 'server/claude_usage.py'), '--then', 'echo hi'])
        self.assertEqual(status['padding'], 0)
        backup = settings.with_name('settings.json.before-tether')
        self.assertEqual(json.loads(backup.read_text())['statusLine'], original)
        first = settings.read_bytes()
        repeated = self.plan('--hooks')
        self.assertEqual(next(hook['action'] for hook in repeated['hooks'] if 'value' in hook), 'preserve')
        self.apply(repeated)
        self.assertEqual(settings.read_bytes(), first)

    def test_claude_status_line_manual_recorder_is_preserved(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        # Install the independent session hooks before checking byte preservation.
        self.apply(self.plan('--hooks'))
        for command in ('python3 /custom/claude_usage.py; echo hi',
                        shlex.join([sys.executable, '/custom/claude_usage.py', '--then', 'echo hi'])):
            with self.subTest(command=command):
                data = json.loads(settings.read_text())
                data['statusLine'] = {'type': 'command', 'command': command}
                settings.write_text(json.dumps(data))
                first = settings.read_bytes()
                plan = self.plan('--hooks')
                self.assertEqual(next(hook['action'] for hook in plan['hooks'] if 'value' in hook), 'preserve')
                self.apply(plan)
                self.assertEqual(settings.read_bytes(), first)

    def test_claude_status_line_relocation_keeps_wrapped_command(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        old_repo = Path(self.temp.name) / 'old repo'
        self.apply(self.plan('--hooks'))
        state = self.home / '.config/tether/install.json'
        saved = json.loads(state.read_text())
        saved['repo'] = str(old_repo)
        state.write_text(json.dumps(saved))
        original = "printf '%s' 'hello world'"
        settings.write_text(json.dumps({'statusLine': {
            'type': 'command', 'command': shlex.join([
                sys.executable, str(old_repo / 'server/claude_usage.py'), '--then', original]),
            'padding': 0}}))
        plan = self.plan('--hooks')
        hook = next(hook for hook in plan['hooks'] if 'value' in hook)
        self.assertEqual(hook['action'], 'replace')
        self.assertEqual(shlex.split(hook['value']['command']),
                         [sys.executable, str(self.repo / 'server/claude_usage.py'), '--then', original])
        self.apply(plan)
        status = json.loads(settings.read_text())['statusLine']
        self.assertEqual(shlex.split(status['command']), shlex.split(hook['value']['command']))
        self.assertEqual(status['padding'], 0)

    def test_claude_status_line_uninstall_restores_original(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        original = {'type': 'command', 'command': 'echo hi', 'padding': 0, 'refreshInterval': 10}
        settings.write_text(json.dumps({'statusLine': original}))
        self.apply(self.plan('--hooks'))
        plan = self.plan('--uninstall')
        self.assertIn('unwrap', [hook['action'] for hook in plan['hooks']])
        with mock.patch('builtins.print') as output:
            installer.print_plan(plan)
        output.assert_any_call(f'Hook {settings}: unwrap')
        self.apply(plan)
        self.assertEqual(json.loads(settings.read_text())['statusLine'], original)
        backup = settings.with_name('settings.json.before-tether')
        self.assertIn('--then', json.loads(backup.read_text())['statusLine']['command'])

    def test_claude_status_line_without_hooks_is_preserved(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        settings.write_text('{"statusLine": {"type": "command", "command": "echo hi", "padding": 0}}')
        first = settings.read_bytes()
        self.apply(self.plan())
        self.assertEqual(settings.read_bytes(), first)

    def test_claude_status_line_wrap_rechecks_original(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        settings.write_text('{"statusLine": {"type": "command", "command": "echo hi"}}')
        plan = self.plan('--hooks')
        updated = {'type': 'command', 'command': 'echo changed', 'padding': 1}
        settings.write_text(json.dumps({'statusLine': updated}))
        self.apply(plan)
        self.assertEqual(json.loads(settings.read_text())['statusLine'], updated)

    def test_ask_bridge_is_opt_in_separate_and_removed_when_disabled(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        self.assertFalse(self.plan()['options']['ask_bridge'])
        off = self.plan('--hooks')
        self.apply(off)
        original = json.loads(settings.read_text())
        self.assertFalse(any(group.get('matcher') == 'AskUserQuestion'
                             for group in original['hooks']['PreToolUse']))
        on = self.plan('--ask-bridge')
        self.apply(on)
        data = json.loads(settings.read_text())
        bridge = data['hooks']['PreToolUse'][-1]
        self.assertEqual(bridge['matcher'], 'AskUserQuestion')
        self.assertEqual(bridge['hooks'][0]['timeout'], 600)
        self.assertEqual(bridge['hooks'][0]['statusMessage'], 'Question sent to Tether · back here in 30 s unless answered there')
        self.assertTrue(self.plan()['options']['ask_bridge'])
        self.assertEqual(data['hooks']['PreToolUse'][:-1], original['hooks']['PreToolUse'])
        for event, groups in original['hooks'].items():
            for group in groups:
                self.assertEqual(group['hooks'][0].get('timeout'), None if event == 'SubagentStop' else 1)
        first = settings.read_text()
        self.apply(self.plan('--ask-bridge'))
        self.assertEqual(settings.read_text(), first)
        self.apply(self.plan('--no-ask-bridge'))
        self.assertEqual(json.loads(settings.read_text()), original)
        self.assertFalse(self.plan()['options']['ask_bridge'])

    def test_ask_bridge_independent_of_passive_hooks_and_uninstall(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        self.apply(self.plan('--ask-bridge'))
        data = json.loads(settings.read_text())
        self.assertEqual(list(data['hooks']), ['PreToolUse'])
        self.assertEqual(len(data['hooks']['PreToolUse']), 1)
        self.apply(self.plan('--uninstall'))
        self.assertEqual(json.loads(settings.read_text()), {'hooks': {}})

    def test_ask_bridge_wait_clamped_saved_and_existing_hook_updated(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        self.assertEqual(self.plan()['options']['ask_bridge_wait'], 30)
        for value, seconds in [('1', 5), ('47', 47), ('999', 120)]:
            plan = self.plan('--ask-bridge', '--ask-bridge-wait', value)
            self.assertEqual(plan['options']['ask_bridge_wait'], seconds)
            self.apply(plan)
            self.assertEqual(self.plan()['options']['ask_bridge_wait'], seconds)
            groups = json.loads(settings.read_text())['hooks']['PreToolUse']
            self.assertEqual(len(groups), 1)
            hook = groups[0]['hooks'][0]
            self.assertEqual(hook['timeout'], 600)
            self.assertEqual(hook['statusMessage'], f'Question sent to Tether · back here in {seconds} s unless answered there')
        old = json.loads(settings.read_text())
        old['hooks']['PreToolUse'][0]['hooks'][0]['timeout'] = 40
        old['hooks']['PreToolUse'][0]['hooks'][0]['statusMessage'] = 'Answer on phone · back here when the phone stops'
        settings.write_text(json.dumps(old))
        self.apply(self.plan())
        hook = json.loads(settings.read_text())['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertEqual(hook['timeout'], 600)
        self.assertEqual(hook['statusMessage'], 'Question sent to Tether · back here in 120 s unless answered there')
        self.apply(self.plan('--no-ask-bridge'))
        self.assertEqual(json.loads(settings.read_text()), {'hooks': {}})

    def test_claude_hooks_preserve_malformed_configuration(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        for hooks in [None, [], {'PreToolUse': {}}, {'PostToolUse': [{'hooks': None}]},
                      {'Stop': [{'hooks': [None]}]}]:
            data = {'statusLine': {'command': 'custom'}, 'hooks': hooks}
            settings.write_text(json.dumps(data))
            first = settings.read_text()
            self.apply(self.plan('--hooks'))
            self.assertEqual(settings.read_text(), first)
            self.apply(self.plan('--uninstall'))
            self.assertEqual(settings.read_text(), first)
        settings.write_text('{"hooks": {"Stop": []}}')
        self.apply(self.plan('--uninstall'))
        self.assertEqual(json.loads(settings.read_text()), {'hooks': {'Stop': []}})

    def test_claude_hooks_recheck_settings_changed_after_plan(self):
        settings = self.home / '.claude/settings.json'
        settings.parent.mkdir()
        settings.write_text('{"statusLine": {"command": "custom"}}')
        plan = self.plan('--hooks')
        data = {'statusLine': {'command': 'custom'}, 'hooks': {'PreToolUse': [
            {'hooks': [{'type': 'command', 'command': 'new-guard'}]}]}}
        settings.write_text(json.dumps(data))
        self.apply(plan)
        self.assertEqual(json.loads(settings.read_text())['hooks']['PreToolUse'][0], data['hooks']['PreToolUse'][0])
        uninstall = self.plan('--uninstall')
        entries = next(hook['entries'] for hook in uninstall['hooks'] if 'entries' in hook)
        # If a user adds another handler to Tether's group, remove only Tether's handler.
        data = json.loads(settings.read_text())
        data['hooks']['Stop'][0]['hooks'].append({'type': 'command', 'command': 'user-stop'})
        settings.write_text(json.dumps(data))
        self.apply(uninstall)
        self.assertEqual(json.loads(settings.read_text())['hooks']['Stop'],
                         [{'hooks': [{'type': 'command', 'command': 'user-stop'}]}])
        self.assertIn('PostModelSwitch', entries)

    def test_side_ports_do_not_clash_and_units_use_server_environment(self):
        plan = self.plan('--terminal', '--voice', '--push')
        self.assertEqual(len(plan['units']), 3)
        options = plan['options']
        self.assertEqual(len({options['port'], options['terminal_port'], options['voice_port']}), 3)
        server = plan['units']['tether.service']
        self.assertIn('MOBILE_TMUX_TTYD_PORT=', server)
        self.assertIn('MOBILE_TMUX_TRANSCRIBE_URL=', server)
        self.assertIn('whisper-venv/bin/python', server)
        self.assertIn('transcribe_worker.py', plan['units']['tether-voice.service'])
        self.assertTrue(any('faster-whisper' in cmd and 'pywebpush' in cmd for cmd in plan['commands']))

    def test_images_installs_pillow_in_the_server_venv(self):
        plan = self.plan('--images')
        self.assertTrue(plan['options']['images'])
        self.assertTrue(any('pip' in cmd and 'pillow' in cmd for cmd in plan['commands']))
        self.assertIn('server-venv/bin/python', plan['units']['tether.service'])
        plain = self.plan('--no-images')
        self.assertFalse(any('pillow' in cmd for cmd in plain['commands']))
        self.assertNotIn('server-venv', plain['units']['tether.service'])

    def test_optional_dependencies_apply_and_optional_units_can_be_removed(self):
        plan = self.plan('--terminal', '--voice', '--push')
        actual_run = installer.run
        dependency_calls = []

        def run(command, **kwargs):
            if '-m' in command and ('venv' in command or 'pip' in command):
                dependency_calls.append(command)
                if 'venv' in command:
                    python = Path(command[-1]) / 'bin/python'
                    python.parent.mkdir(parents=True)
                    python.write_text('fake venv')
                return subprocess.CompletedProcess(command, 0)
            return actual_run(command, **kwargs)

        with mock.patch.object(installer, 'run', side_effect=run):
            self.apply(plan)
            repeated = self.plan('--terminal', '--voice', '--push')
            self.apply(repeated)
        self.assertEqual(sum('venv' in command for command in dependency_calls), 1)
        self.assertEqual(sum('pip' in command for command in dependency_calls), 2)
        self.apply(self.plan('--no-terminal', '--no-voice', '--no-push'))
        units = self.home / '.config/systemd/user'
        self.assertEqual(sorted(path.name for path in units.iterdir()), ['tether.service'])
        for unit in ('tether-terminal.service', 'tether-voice.service'):
            self.assertIn(['systemctl', '--user', 'disable', '--now', unit], self.calls())

    def test_missing_linux_or_user_manager_stops_before_writes(self):
        with mock.patch.object(installer.platform, 'system', return_value='Darwin'):
            with self.assertRaisesRegex(ValueError, '1.*Linux'):
                self.apply(self.plan())
        original = installer.probe

        def probe(*args):
            if 'show-environment' in args:
                return subprocess.CompletedProcess(args, 1, stdout='')
            return original(*args)

        with mock.patch.object(installer, 'probe', side_effect=probe):
            with self.assertRaisesRegex(ValueError, 'systemd user manager'):
                self.apply(self.plan())
        self.assertFalse((self.home / '.config').exists())

    def test_proxy_requires_loopback_and_invalid_port_is_rejected(self):
        plan = self.plan('--auth', 'proxy', '--host', '0.0.0.0', '--port', '70000')
        with self.assertRaisesRegex(ValueError, '127.0.0.1'):
            self.apply(plan)
        self.assertFalse(next(check['ok'] for check in plan['checks'] if check['name'] == 'chat port'))

    def test_uninstall_does_not_require_tmux_or_npm(self):
        self.apply(self.plan())
        (self.bin / 'tmux').unlink()
        (self.bin / 'npm').unlink()
        self.apply(self.plan('--uninstall'))

    def test_cli_json_and_old_entry_point(self):
        # An explicit isolated port prevents probing any production service port.
        for script in (installer.ROOT / 'install.py', installer.ROOT / 'server/deploy/install.py'):
            result = subprocess.run([sys.executable, str(script), '--plan', '--json', '--port', str(self.port),
                                     '--no-terminal', '--no-build-ui'], capture_output=True, text=True, check=True)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed['options']['port'], self.port)
            self.assertFalse((self.home / '.config').exists())

    def test_cli_apply_json_stays_parseable_and_reuses_saved_options(self):
        script = self.repo / 'install.py'
        shutil.copy2(installer.ROOT / 'install.py', script)
        result = subprocess.run([sys.executable, str(script), '--apply', '--json', '--port', str(self.port),
                                 '--no-terminal', '--build-ui'], capture_output=True, text=True, check=True)
        first = json.loads(result.stdout)
        self.assertIn('build output', result.stderr)
        self.assertIn('service output', result.stderr)
        repeated = subprocess.run([sys.executable, str(script), '--apply', '--json'],
                                  capture_output=True, text=True, check=True)
        second = json.loads(repeated.stdout)
        self.assertEqual(first['options'], second['options'])
        self.assertIn('port', second['reused_saved_choices'])


if __name__ == '__main__':
    unittest.main()
