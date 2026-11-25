"""Offline release/bootstrap and migration proofs; only temporary homes and fake tools."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest import mock

import test_install
import test_updates
from test_updates import ROOT, cli

installer = test_install.installer
BOOTSTRAP = ROOT / 'integrations/claude/tether/skills/setup/bootstrap.py'


class ReleaseInstallTests(unittest.TestCase):
    setUp = test_install.InstallTests.setUp
    plan = test_install.InstallTests.plan
    apply = test_install.InstallTests.apply
    calls = test_install.InstallTests.calls
    bundle = test_updates.ReleaseTests.bundle

    def assets(self, unsafe=None):
        self.asset_dir = Path(self.temp.name) / 'assets'
        self.asset_dir.mkdir(exist_ok=True)
        self.base = 'https://example.invalid'
        self.bundle(unsafe)
        self.archive = self.asset_dir / 'tether-0.2.0.tar.gz'
        self.sums = self.asset_dir / 'SHA256SUMS'
        self.root = self.home / '.local/share/tether'
        self.link = self.root / 'current'
        return ['--archive', str(self.archive), '--sums', str(self.sums)]

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            result = cli.main(list(args))
        self.output, self.errors = output.getvalue(), errors.getvalue()
        return result

    def bootstrap(self, *args):
        # Copy only the script outside the repo: prove no checkout or import is needed.
        script = Path(self.temp.name) / 'bootstrap.py'
        shutil.copyfile(BOOTSTRAP, script)
        return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, timeout=10)

    def test_standalone_bootstrap_and_installer_cli_link(self):
        args = self.assets()
        result = self.bootstrap(*args, '--to', 'v0.2.0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link.resolve(), self.root / 'releases/0.2.0')
        self.assertFalse((self.home / '.config').exists())
        self.assertEqual(self.calls(), [])
        parsed = installer.parser().parse_args(['--plan', '--port', str(self.port), '--no-terminal', '--no-build-ui'])
        plan = installer.make_plan(parsed, repo=self.link.resolve(), home=self.home)
        self.assertEqual(plan['cli_link']['action'], 'create')
        self.assertTrue(any('export PATH=' in warning for warning in plan['warnings']))
        self.apply(plan)
        command = self.home / '.local/bin/tether'
        self.assertEqual(command.readlink(), self.link / 'tether')
        result = subprocess.run([str(command), '--help'], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PATH=str(self.bin) + os.pathsep + test_updates.REAL_PATH))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('migrate', result.stdout)
        self.assertEqual(installer.make_plan(parsed, repo=self.link.resolve(), home=self.home)['cli_link']['action'], 'keep')
        self.apply(installer.make_plan(parsed, repo=self.link.resolve(), home=self.home))
        self.assertEqual(command.readlink(), self.link / 'tether')
        # Repeated bootstrap refuses to overwrite an established current link.
        self.assertEqual(self.bootstrap(*args).returncode, 1)

    def test_foreign_cli_paths_are_preserved(self):
        args = self.assets()
        self.assertEqual(self.invoke('install', *args), 0, self.errors)
        command = self.home / '.local/bin/tether'
        command.parent.mkdir(parents=True)
        parsed = installer.parser().parse_args(['--plan', '--port', str(self.port), '--no-terminal', '--no-build-ui'])
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if symlink:
                    command.symlink_to(self.home / 'foreign-missing-command')
                else:
                    command.write_bytes(b'foreign command\n')
                plan = installer.make_plan(parsed, repo=self.link.resolve(), home=self.home)
                self.assertEqual(plan['cli_link']['action'], 'preserve foreign path')
                with (Path(self.temp.name) / 'stderr.log').open('w+') as errors, contextlib.redirect_stderr(errors):
                    self.apply(plan)
                    errors.seek(0)
                    self.assertIn('kept unchanged', errors.read())
                if symlink:
                    self.assertEqual(command.readlink(), self.home / 'foreign-missing-command')
                else:
                    self.assertEqual(command.read_bytes(), b'foreign command\n')
                command.unlink()

    def test_bad_checksum_and_unsafe_archives_leave_no_current(self):
        args = self.assets()
        self.sums.write_text('0' * 64 + '  ' + self.archive.name + '\n')
        for entry in ('bootstrap', 'install'):
            result = self.bootstrap(*args).returncode if entry == 'bootstrap' else self.invoke('install', *args)
            self.assertEqual(result, 1)
            self.assertFalse(self.link.exists())
        for name in ('../escape', '/absolute', './nested/../../escape'):
            self.bundle(name)
            self.assertEqual(self.bootstrap(*args).returncode, 1)
            self.assertEqual(self.invoke('install', *args), 1)
            self.assertFalse(self.link.exists())
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with tarfile.open(self.archive, 'w:gz') as archive:
                info = tarfile.TarInfo('bad')
                info.type, info.linkname = kind, '../escape'
                archive.addfile(info)
            self.sums.write_text(hashlib.sha256(self.archive.read_bytes()).hexdigest() + '  ' + self.archive.name + '\n')
            self.assertEqual(self.bootstrap(*args).returncode, 1)
            self.assertFalse(self.link.exists())

    def test_archive_arguments_and_tag_must_match(self):
        args = self.assets()
        for bad in (['--archive', str(self.archive)], ['--sums', str(self.sums)],
                    [*args, '--to', 'v0.3.0'], [*args, '--to', '../escape']):
            self.assertEqual(self.bootstrap(*bad).returncode, 1)
            self.assertEqual(self.invoke('install', *bad), 1)
            self.assertFalse(self.link.exists())

    def test_cli_install_without_saved_state_and_network_selection(self):
        self.assets()
        release = self.bundle()
        downloads = {release['assets'][0]['browser_download_url']: self.archive,
                     release['assets'][1]['browser_download_url']: self.sums}

        def download(url, target):
            shutil.copyfile(downloads[url], target)

        with mock.patch.object(cli.bootstrap, 'fetch_release', return_value=release) as fetch, \
                mock.patch.object(cli.bootstrap, 'download', side_effect=download):
            self.assertEqual(self.invoke('install', '--to', 'v0.2.0'), 0, self.errors)
            fetch.assert_called_once_with('v0.2.0')
        self.assertFalse((self.home / '.config').exists())
        self.assertEqual(self.link.resolve().name, '0.2.0')

    def test_bootstrap_latest_endpoint_matches_updater(self):
        import updates
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"tag_name":"v0.2.0"}'
        for repo in ('incmak/tether', 'example/fork'):
            with mock.patch.dict(os.environ, TETHER_REPO=repo), \
                    mock.patch.object(cli.bootstrap, 'urlopen', return_value=response) as fetch:
                cli.bootstrap.fetch_release()
                self.assertEqual(fetch.call_args.args[0].full_url, updates.release_endpoint())
                cli.bootstrap.fetch_release('v0.2.0')
                self.assertEqual(fetch.call_args.args[0].full_url, updates.release_endpoint('v0.2.0'))

    def checkout(self, hooks=True):
        args = self.assets()
        (self.repo / '.git').mkdir()
        (self.repo / 'local-changes.txt').write_bytes(b'keep checkout changes')
        for folder in ('.claude', '.gemini/antigravity-cli'):
            (self.home / folder).mkdir(parents=True)
        settings = self.home / '.claude/settings.json'
        settings.write_text(json.dumps({'other': True, 'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': 'user-stop'}]}]}}))
        # Fake optional dependencies, so saved voice and push choices never run real pip.
        vpython = self.home / '.local/share/tether/whisper-venv/bin/python'
        vpython.parent.mkdir(parents=True)
        shutil.copyfile(self.bin / 'systemctl', vpython)
        vpython.chmod(0o755)
        self.apply(self.plan('--terminal', '--voice', '--push', '--hooks' if hooks else '--no-hooks', '--resources',
                             '--updates', 'auto', '--host', '127.0.0.2', '--auth', 'proxy'))
        self.state = self.home / '.config/tether/install.json'
        patch = mock.patch.dict(os.environ, TETHER_INSTALL_STATE=str(self.state))
        patch.start()
        self.addCleanup(patch.stop)
        self.saved = json.loads(self.state.read_text())
        config = self.home / '.config/mobile-tmux'
        config.mkdir(parents=True)
        self.data = {config / 'token': b'token\x00\xff\n', config / 'prefs.json': b'{"keep":"prefs"}',
                     config / 'config.json': b'{"keep":"config"}',
                     self.home / '.claude/transcripts/session.jsonl': b'chat history\n'}
        for path, content in self.data.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return args

    def assert_data_kept(self):
        for path, content in self.data.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual((self.repo / 'local-changes.txt').read_bytes(), b'keep checkout changes')
        self.assertTrue((self.repo / '.git').is_dir())

    def test_migrate_keeps_data_and_moves_every_owned_path(self):
        args = self.checkout()
        git = self.bin / 'git'
        git.write_text('#!' + sys.executable + '\nprint(" M local-changes.txt")\n')
        git.chmod(0o755)
        # Relocate owned handlers even when the original installer used another interpreter.
        for folder in ('.claude', '.gemini/antigravity-cli'):
            settings = self.home / folder / 'settings.json'
            settings.write_text(settings.read_text().replace(sys.executable, '/old/python'))
        with mock.patch.object(cli, 'healthy', return_value=True) as health:
            self.assertEqual(self.invoke('migrate', '--from', str(self.repo), *args, '--to', 'v0.2.0'), 0, self.errors)
        self.assertIn('Warning:', self.output)
        self.assertIn('no longer used', self.output)
        self.assertEqual(self.output.splitlines()[-1], 'Result: PASS (migrated to v0.2.0)')
        health.assert_called_once_with(self.port, host='127.0.0.2')
        self.assert_data_kept()
        state = json.loads(self.state.read_text())
        self.assertEqual(state['version'], 1)  # Installer schema is independent of release version.
        for key, value in self.saved['options'].items():
            if key != 'build_ui':
                self.assertEqual(state['options'][key], value, key)
        self.assertFalse(state['options']['build_ui'])
        self.assertEqual(state['repo'], str(self.link))
        units = self.home / '.config/systemd/user'
        for name in ('tether.service', 'tether-terminal.service', 'tether-voice.service', 'tether-update.service'):
            content = (units / name).read_text()
            self.assertNotIn(str(self.repo), content)
            self.assertIn(str(self.link), content)
        for folder in ('.claude', '.gemini/antigravity-cli'):
            content = (self.home / folder / 'settings.json').read_text()
            self.assertNotIn(str(self.repo), content)
            self.assertIn(str(self.link), content)
        hooks = json.loads((self.home / '.claude/settings.json').read_text())
        self.assertTrue(hooks['other'])
        self.assertEqual(hooks['hooks']['Stop'][0]['hooks'][0]['command'], 'user-stop')
        self.assertEqual(len(hooks['hooks']['Stop']), 2)
        self.assertEqual((self.home / '.local/bin/tether').readlink(), self.link / 'tether')
        self.assertIn(['systemctl', '--user', 'restart', 'tether-update.timer'], self.calls())

    def test_migrate_keeps_hooks_disabled_and_user_settings_unchanged(self):
        args = self.checkout(hooks=False)
        settings = self.home / '.claude/settings.json'
        before = settings.read_bytes()
        with mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.invoke('migrate', *args), 0, self.errors)
        self.assertEqual(settings.read_bytes(), before)
        self.assertFalse((self.home / '.gemini/antigravity-cli/settings.json').exists())
        self.assertFalse(json.loads(self.state.read_text())['options']['hooks'])
        self.assert_data_kept()

    def test_migrate_reuses_bootstrap_prepared_release(self):
        args = self.checkout()
        self.assertEqual(self.bootstrap(*args).returncode, 0)
        # Real CLI imports can add bytecode caches before migration starts.
        cache = self.link / 'server/__pycache__'
        cache.mkdir(parents=True)
        (cache / 'updates.cpython-312.pyc').write_bytes(b'cache')
        with mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.invoke('migrate', *args), 0, self.errors)
        self.assertEqual(json.loads(self.state.read_text())['repo'], str(self.link))
        self.assert_data_kept()

    def test_migrate_rejects_changed_prepared_release(self):
        args = self.checkout()
        self.assertEqual(self.bootstrap(*args).returncode, 0)
        (self.link / 'install.py').write_text('unverified changed installer')
        before = cli.snapshot(self.state)
        with mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.invoke('migrate', *args), 1)
            run.assert_not_called()
        self.assertEqual(cli.snapshot(self.state), before)
        self.assert_data_kept()

    def test_migrate_failed_health_restores_checkout_and_all_settings(self):
        args = self.checkout()
        before = cli.snapshot(self.state)
        with mock.patch.object(cli, 'healthy', return_value=False):
            self.assertEqual(self.invoke('migrate', *args), 1)
        self.assertEqual(self.errors.splitlines()[-1], f'Health check failed; rolled back to checkout {self.repo}')
        self.assertEqual(self.output.splitlines()[-1], 'Result: FAIL (see the error above)')
        self.assertEqual(cli.snapshot(self.state), before)
        self.assertFalse(self.link.is_symlink())
        self.assertFalse((self.root / 'releases/0.2.0').exists())
        self.assert_data_kept()
        # A failed migration can retry the same archive.
        with mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.invoke('migrate', *args), 0, self.errors)

    def test_migrate_installer_failure_restores_existing_current_and_cli(self):
        args = self.checkout()
        old = self.root / 'other-release'
        old.mkdir()
        self.link.symlink_to(old)
        command = self.home / '.local/bin/tether'
        command.parent.mkdir(parents=True)
        command.symlink_to(self.repo / 'tether')
        before = cli.snapshot(self.state)
        real_run = cli.run

        def fail(command, quiet=False):
            if '--apply' in command:
                self.state.write_text('{}')
                raise RuntimeError('installer failed')
            real_run(command, quiet)

        with mock.patch.object(cli, 'run', side_effect=fail):
            self.assertEqual(self.invoke('migrate', *args), 1)
        self.assertEqual(self.link.readlink(), old)
        self.assertEqual(cli.snapshot(self.state), before)
        self.assert_data_kept()

    def test_migrate_refuses_unrelated_source_before_download(self):
        args = self.checkout()
        before = cli.snapshot(self.state)
        self.assertEqual(self.invoke('migrate', '--from', str(self.home), *args), 1)
        self.assertEqual(cli.snapshot(self.state), before)
        self.assertFalse(self.link.exists())


if __name__ == '__main__':
    unittest.main()
