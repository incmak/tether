"""Setup tests use temporary homes and mocked ping processes."""
import json
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import agent_browser_setup
import approval
import autonomy
import autonomy_setup as setup
import paths
import urgent_ping
from test_approval import HAS_CRYPTO, Handler, PasskeyCase


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.settings = self.home / '.claude/settings.json'
        self.settings.parent.mkdir()
        self.script = self.home / 'bot/post-update.py'
        self.script.parent.mkdir(parents=True)
        self.script.touch()
        (self.home / 'config').mkdir()
        (self.home / 'config/briskbot.json').write_text(json.dumps({'script': str(self.script)}))
        for patcher in (mock.patch.object(paths, 'CONFIG', self.home / 'config'),
                        mock.patch.object(paths, 'CLAUDE', self.settings.parent),
                        mock.patch.object(Path, 'home', return_value=self.home),
                        mock.patch.object(setup, '_LAST_TEST', None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_settings(self, data):
        self.settings.write_text(json.dumps(data, indent=2) + '\n')

    def config(self):
        return json.loads((paths.CONFIG / 'urgent-ping.json').read_text())

    def save(self, **changes):
        values = dict(preset='briskbot', fields={'mention': 'user@example.test'}, base_url='https://tether.test')
        setup.save_ping(**dict(values, **changes))


class Hook(Base):
    def test_status_none_missing_and_ok(self):
        self.assertEqual(agent_browser_setup.claude_settings_path(), self.settings)
        status = setup.hook_status()
        self.assertEqual(status['state'], 'missing')
        self.assertEqual(status['path'], str(self.settings))
        self.assertEqual(status['entry'], {'type': 'command', 'timeout': 5, 'command':
                         shlex.join([sys.executable, str(Path(setup.__file__).resolve().with_name('autonomy.py')), 'hook'])})
        setup.install_hook()
        self.assertEqual(setup.hook_status()['state'], 'ok')
        none = self.home / 'absent/settings.json'
        self.assertEqual(setup.hook_status(none)['state'], 'none')
        with self.assertRaisesRegex(ValueError, 'folder does not exist'):
            setup.install_hook(none)
        self.assertFalse(none.parent.exists())

    def test_status_other_path_is_missing(self):
        self.write_settings({'hooks': {'UserPromptSubmit': [{'hooks': [
            {'type': 'command', 'command': 'python3 /other/server/autonomy.py hook'}]}]}})
        self.assertEqual(setup.hook_status()['state'], 'missing')

    def test_invalid_json_and_shapes_are_manual_and_never_overwritten(self):
        for raw in (b'{bad', b'\xff', b'[]', b'NaN', b'{"hooks":null}', b'{"hooks":[]}',
                    b'{"hooks":{"UserPromptSubmit":{}}}', b'{"hooks":{"UserPromptSubmit":[null]}}',
                    b'{"hooks":{"UserPromptSubmit":[{}]}}',
                    b'{"hooks":{"UserPromptSubmit":[{"hooks":[false]}]}}'):
            with self.subTest(raw=raw):
                self.settings.write_bytes(raw)
                status = setup.hook_status()
                self.assertEqual(status['state'], 'manual')
                self.assertTrue(status['reason'])
                with self.assertRaises(ValueError) as error:
                    setup.install_hook()
                self.assertEqual(str(error.exception), status['reason'])
                self.assertEqual(self.settings.read_bytes(), raw)
                self.assertFalse(Path(str(self.settings) + '.tether-bak').exists())

    def test_install_preserves_order_other_hooks_backup_mode_and_idempotence(self):
        keep_a = {'type': 'command', 'command': 'keep-first'}
        keep_b = {'type': 'command', 'command': 'keep-last'}
        old = {'type': 'command', 'command': "python3 '/old path/server/autonomy.py' hook"}
        other_command = {'type': 'command', 'command': 'python3 /other/autonomy.py show'}
        other_type = {'type': 'prompt', 'command': 'python3 /other/autonomy.py hook'}
        malformed_command = {'type': 'command', 'command': "python3 'autonomy.py hook"}
        data = {'first': True, 'hooks': {'SessionStart': [{'hooks': [keep_b]}], 'UserPromptSubmit': [
            {'matcher': 'first', 'hooks': [keep_a, old, other_type]}, {'hooks': [old]},
            {'matcher': 'last', 'hooks': [keep_b, other_command, malformed_command]}], 'Stop': []}, 'last': []}
        self.write_settings(data)
        original = self.settings.read_bytes()
        self.settings.chmod(0o640)
        setup.install_hook()
        result = json.loads(self.settings.read_text())
        self.assertEqual(list(result), ['first', 'hooks', 'last'])
        self.assertEqual(list(result['hooks']), ['SessionStart', 'UserPromptSubmit', 'Stop'])
        self.assertEqual(result['hooks']['SessionStart'], data['hooks']['SessionStart'])
        self.assertEqual(result['hooks']['UserPromptSubmit'], [
            {'matcher': 'first', 'hooks': [keep_a, other_type]},
            {'matcher': 'last', 'hooks': [keep_b, other_command, malformed_command]},
            {'hooks': [setup.hook_status()['entry']]}])
        backup = Path(str(self.settings) + '.tether-bak')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)
        self.assertTrue(self.settings.read_text().endswith('\n'))
        installed = self.settings.read_bytes()
        with mock.patch.object(agent_browser_setup, '_write') as write:
            setup.install_hook()
        write.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), installed)
        self.assertEqual(backup.read_bytes(), original)
        # A later repair must keep the first backup.
        self.write_settings({})
        setup.install_hook()
        self.assertEqual(backup.read_bytes(), original)

    def test_backup_writer_failure_keeps_settings_and_cleans_temporary_file(self):
        self.write_settings({})
        old = self.settings.read_bytes()
        with mock.patch.object(agent_browser_setup.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                setup.install_hook()
        self.assertEqual(self.settings.read_bytes(), old)
        self.assertEqual(sorted(file.name for file in self.settings.parent.iterdir()),
                         ['settings.json', 'settings.json.tether-bak'])


class Ping(Base):
    def test_status_save_command_and_private_mode(self):
        self.assertEqual(setup.ping_status(), {'state': 'off', 'preset': None, 'fields': {}, 'base_url': '',
            'presets': [{'id': 'briskbot', 'name': 'BriskBot (Microsoft Teams)', 'available': True}]})
        self.save()
        config = self.config()
        self.assertEqual(config, {'enabled': True, 'preset': 'briskbot',
            'fields': {'mention': 'user@example.test', 'space': 'team'}, 'base_url': 'https://tether.test',
            'command': [sys.executable, str(self.script), '{title}', 'Needs you', '--space', 'team',
                        '--mention', 'user@example.test', '--urgent', '--tone', 'attention', '--topic',
                        'tether-urgent:{pane}', '--force', '--line', 'Question|{question}', '--button',
                        'Open the tab|{url}']})
        self.assertEqual(stat.S_IMODE((paths.CONFIG / 'urgent-ping.json').stat().st_mode), 0o600)
        self.assertEqual(setup.ping_status()['state'], 'on')
        self.assertEqual(setup.ping_status()['fields'], config['fields'])
        self.assertEqual(setup.ping_status()['preset'], 'briskbot')
        self.assertEqual(setup.ping_status()['base_url'], 'https://tether.test')
        argv = urgent_ping._argv({'pane': '%12', 'name': 'Tab', 'next': 'Choose?'})
        self.assertEqual(argv[2:4], ['Tether: Tab', 'Needs you'])
        self.assertIn('Question|Choose?', argv)
        self.assertIn('Open the tab|https://tether.test/mobile/#session=%2512', argv)

    def test_bad_mentions_spaces_fields_and_presets_do_not_write(self):
        for mention in (None, [], '', 'a b', 'a;sh', '-x\n', 'x' * 65, 'Ü'):
            with self.subTest(mention=mention), self.assertRaises(ValueError):
                self.save(fields={'mention': mention})
        for space in (None, [], 'other', 'Team', ''):
            with self.subTest(space=space), self.assertRaises(ValueError):
                self.save(fields={'mention': 'user', 'space': space})
        for fields in (None, [], 'user', {}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.save(fields=fields)
        for preset in ('other', None, []):
            with self.subTest(preset=preset), self.assertRaises(ValueError):
                self.save(preset=preset)
        self.script.unlink()
        self.assertFalse(setup.ping_status()['presets'][0]['available'])
        with self.assertRaisesRegex(ValueError, 'available'):
            self.save()
        self.script.touch()
        (paths.CONFIG / 'briskbot.json').unlink()
        self.assertFalse(setup.ping_status()['presets'][0]['available'])
        (paths.CONFIG / 'briskbot.json').write_text(json.dumps({'script': 'post-update.py'}))
        self.assertFalse(setup.ping_status()['presets'][0]['available'])
        self.assertFalse((paths.CONFIG / 'urgent-ping.json').exists())

    def test_bad_base_urls_do_not_overwrite_saved_config(self):
        self.save()
        old = (paths.CONFIG / 'urgent-ping.json').read_bytes()
        for url in (None, [], '', 'tether.test', 'ftp://tether.test', 'http:///path',
                    'https://u:p@tether.test', 'https://u@tether.test', 'https://@tether.test',
                    'https://tether.test?x=1', 'https://tether.test?', 'https://tether.test#x',
                    'https://tether.test#', 'https://[broken', 'http://tether.test:bad',
                    'https://tether.test\n', 'https://te ther.test', 'http://tether.test/\x00'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.save(base_url=url)
            self.assertEqual((paths.CONFIG / 'urgent-ping.json').read_bytes(), old)

    def test_valid_spaces_mentions_and_urls(self):
        for space in setup.SPACES:
            for url in ('http://localhost:8765', 'https://tether.test/chat/', 'http://[::1]:8080'):
                with self.subTest(space=space, url=url):
                    self.save(fields={'mention': 'A.b_c-1@x', 'space': space}, base_url=url)
                    self.assertEqual(self.config()['fields']['space'], space)
                    self.assertEqual(self.config()['base_url'], url)

    def test_disable_preserves_fields_and_legacy_config_remains_enabled(self):
        self.save()
        config = self.config()
        file = paths.CONFIG / 'urgent-ping.json'
        file.chmod(0o644)
        setup.disable_ping()
        self.assertEqual(self.config(), dict(config, enabled=False))
        self.assertIsNone(urgent_ping._argv({'pane': '%1'}))
        self.assertEqual(setup.ping_status()['state'], 'off')
        self.assertEqual(setup.ping_status()['fields'], config['fields'])
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        del config['enabled']
        file.write_text(json.dumps(config))
        self.assertIsNotNone(urgent_ping._argv({'pane': '%1', 'name': 'Tab'}))
        self.assertEqual(setup.ping_status()['state'], 'on')

    def test_disable_missing_config(self):
        setup.disable_ping()
        self.assertEqual(self.config(), {'enabled': False})
        self.assertEqual(setup.ping_status()['state'], 'off')

    def test_save_is_atomic_and_failure_keeps_old_file(self):
        self.save()
        file = paths.CONFIG / 'urgent-ping.json'
        old = file.read_bytes()
        with mock.patch.object(setup.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.save(base_url='http://localhost')
        self.assertEqual(file.read_bytes(), old)
        self.assertEqual(sorted(paths.CONFIG.iterdir()), [paths.CONFIG / 'briskbot.json', file])

    def test_foreground_test_returns_status_and_last_three_hundred_chars(self):
        self.save()
        for code, stderr, stdout, expected in (
                (0, b'', b'x' * 400, 'x' * 300),
                (3, b'y' * 400, b'ignore', 'y' * 300),
                (1, 'bad result', '', 'bad result')):
            with self.subTest(code=code), mock.patch.object(setup, '_LAST_TEST', None), \
                    mock.patch.object(urgent_ping, '_name', return_value='Tab'), \
                    mock.patch.object(urgent_ping.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=code, stderr=stderr, stdout=stdout)) as run:
                self.assertEqual(setup.test_ping(), {'ok': code == 0, 'code': code, 'detail': expected})
                self.assertEqual(run.call_args.kwargs,
                                 {'stdin': subprocess.DEVNULL, 'capture_output': True, 'timeout': 60})
                self.assertIn('Question|Test ping from Tether', run.call_args.args[0])

    def test_test_cooldown_is_shared_and_allows_sixty_seconds(self):
        with mock.patch.object(setup.time, 'monotonic', side_effect=[0, 59.99, 60]), \
                mock.patch.object(urgent_ping, 'run_test', return_value={'ok': True}) as run:
            self.assertEqual(setup.test_ping(), {'ok': True})
            with self.assertRaisesRegex(ValueError, '^Wait a minute before the next test\\.$'):
                setup.test_ping()
            self.assertEqual(setup.test_ping(), {'ok': True})
            self.assertEqual(run.call_count, 2)

    def test_foreground_test_off_and_process_errors(self):
        with mock.patch.object(urgent_ping.subprocess, 'run') as run:
            self.assertEqual(setup.test_ping()['code'], None)
        run.assert_not_called()
        self.save()
        for error in (OSError('missing tool'), subprocess.TimeoutExpired('ping', 60)):
            with self.subTest(error=error), mock.patch.object(setup, '_LAST_TEST', None), \
                    mock.patch.object(urgent_ping, '_name', return_value='Tab'), \
                    mock.patch.object(urgent_ping.subprocess, 'run', side_effect=error):
                result = setup.test_ping()
                self.assertEqual(result, {'ok': False, 'code': None, 'detail': str(error)[-300:]})

    def test_timeout_returns_the_captured_output_tail(self):
        self.save()
        with mock.patch.object(urgent_ping, '_name', return_value='Tab'), \
                mock.patch.object(urgent_ping.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                    'ping', 60, output=b'stdout', stderr=b'e' * 400)):
            self.assertEqual(setup.test_ping(), {'ok': False, 'code': None, 'detail': 'e' * 300})


@unittest.skipUnless(HAS_CRYPTO, 'cryptography is not installed')
class SetupApproval(PasskeyCase):
    def setUp(self):
        super().setUp()
        paths.CLAUDE.mkdir()
        self.settings = paths.CLAUDE / 'settings.json'
        self.script = self.root / 'post-update.py'
        self.script.touch()
        for patcher in (mock.patch.object(setup, '_briskbot_path', return_value=self.script),
                        mock.patch.object(setup, '_LAST_TEST', None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, payload):
        handler = Handler(autonomy.PATH + '/setup', payload)
        autonomy.post(handler)
        return handler

    def test_hook_and_ping_refused_before_write_and_accepted_with_assertion(self):
        for payload, target in (({'action': 'hook'}, self.settings),
                                ({'action': 'ping', 'preset': 'briskbot', 'fields': {'mention': 'user'},
                                  'base_url': 'https://tether.test'}, paths.CONFIG / 'urgent-ping.json')):
            with self.subTest(payload=payload):
                handler = self.post(payload)
                self.assertEqual((handler.status, handler.body['approval']), (403, 'no-passkey'))
                self.assertFalse(target.exists())
                self.assertFalse(paths.CONFIG.exists())
        self.add()
        for payload, target in (({'action': 'hook'}, self.settings),
                                ({'action': 'ping', 'preset': 'briskbot', 'fields': {'mention': 'user'},
                                  'base_url': 'https://tether.test'}, paths.CONFIG / 'urgent-ping.json')):
            with self.subTest(payload=payload):
                handler = self.post(payload)
                self.assertEqual((handler.status, handler.body['approval']), (403, 'required'))
                self.assertFalse(target.exists())
                proof = self.assertion('setup-' + payload['action'], payload)
                handler = self.post(dict(payload, approval=proof))
                self.assertEqual(handler.status, 200, handler.body)
                self.assertTrue(target.exists())

    def test_ping_off_and_ping_test_stay_open_with_sixty_second_cooldown(self):
        setup.save_ping('briskbot', {'mention': 'user'}, 'https://tether.test')
        handler = self.post({'action': 'ping-off'})
        self.assertEqual((handler.status, handler.body['ping']['state']), (200, 'off'))
        with mock.patch.object(setup.time, 'monotonic', side_effect=[0, 59.99, 60]), \
                mock.patch.object(urgent_ping, 'run_test', return_value={'ok': True}) as run:
            self.assertEqual(self.post({'action': 'ping-test'}).status, 200)
            handler = self.post({'action': 'ping-test'})
            self.assertEqual((handler.status, handler.body), (400, {'error': 'Wait a minute before the next test.'}))
            self.assertEqual(self.post({'action': 'ping-test'}).status, 200)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
