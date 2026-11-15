"""Regression checks for preserving open-tab assets during an internal UI deploy."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deploy_ui', Path(__file__).with_name('deploy-ui.py'))
deploy_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_ui)


class DeployUiTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.build = Path(self.folder.name) / 'build'
        self.target = Path(self.folder.name) / 'dist'
        for root in (self.build, self.target):
            (root / 'assets').mkdir(parents=True)
        (self.build / 'assets/new.js').write_text('new')
        (self.build / 'index.html').write_text('<script src="/chat/assets/new.js"></script>')
        (self.target / 'assets/old.js').write_text('old')
        (self.target / 'index.html').write_text('old entry')

    def deploy(self):
        # These checks cover the install itself. The commit guard has its own tests below.
        return deploy_ui.deploy(self.build, self.target, force=True, warn=lambda text: None)

    def test_preserves_old_chunks_and_publishes_entry_last(self):
        replace = deploy_ui.os.replace
        installed = []
        def record(source, target):
            installed.append(Path(target).name)
            replace(source, target)
        with patch.object(deploy_ui.os, 'replace', side_effect=record):
            self.assertEqual(self.deploy(), 2)
        self.assertEqual(installed[-1], 'index.html')
        self.assertEqual((self.target / 'assets/old.js').read_text(), 'old')
        self.assertEqual((self.target / 'assets/new.js').read_text(), 'new')
        self.assertEqual((self.target / '.live-ui').read_bytes(), b'')
        self.assertFalse((self.target / '.live-commit').exists())

    def test_rejects_missing_assets_before_install(self):
        (self.build / 'assets/new.js').unlink()
        with self.assertRaises(ValueError):
            deploy_ui.deploy(self.build, self.target)
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')

    def test_failed_asset_install_keeps_old_entry(self):
        with patch.object(deploy_ui.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.deploy()
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')


class DeployGuardTest(unittest.TestCase):
    """B92: a build must contain the live commit, or the install would remove deployed work."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.repo, self.build, self.target = root / 'repo', root / 'build', root / 'dist'
        self.repo.mkdir()
        self.git('init', '-q')
        self.base = self.commit('base')
        self.live = self.commit('live feature')
        (self.build / 'assets').mkdir(parents=True)
        (self.build / 'assets/new.js').write_text('new')
        (self.build / 'index.html').write_text('<script src="/chat/assets/new.js"></script>')
        (self.target / 'assets').mkdir(parents=True)
        (self.target / 'index.html').write_text('old entry')
        self.warnings = []

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@example.invalid', '-c', 'user.name=t',
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def stamp(self, commit, dirty=False, changes=()):
        (self.build / '.build-commit').write_text(json.dumps({'commit': commit, 'dirty': dirty, 'changes': list(changes)}))

    def set_live(self, commit):
        (self.target / '.live-commit').write_text(json.dumps({'commit': commit, 'dirty': False}))

    def deploy(self, **options):
        return deploy_ui.deploy(self.build, self.target, repo=self.repo, warn=self.warnings.append, **options)

    def record(self):
        return json.loads((self.target / '.live-commit').read_text())

    def assert_untouched(self):
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')
        self.assertFalse((self.target / 'assets/new.js').exists())

    def test_build_that_contains_live_commit_installs_and_records(self):
        self.set_live(self.base)
        self.stamp(self.live)
        self.assertEqual(self.deploy(), 2)
        self.assertEqual(self.record()['commit'], self.live)
        self.assertEqual(self.record()['entry'], 'assets/new.js')
        self.assertFalse((self.target / '.build-commit').exists())

    def test_older_base_is_refused_and_names_lost_commits(self):
        self.git('checkout', '-q', '-b', 'old', self.base)
        older = self.commit('other agent work')
        self.set_live(self.live)
        self.stamp(older)
        with self.assertRaises(deploy_ui.Refused) as caught:
            self.deploy()
        message = str(caught.exception)
        self.assertIn(self.live, message)
        self.assertIn(older, message)
        self.assertIn(f'{self.live[:7]} live feature', message)
        self.assertIn('rebase', message)
        self.assert_untouched()
        self.assertEqual(self.record()['commit'], self.live)

    def test_force_installs_older_base_with_warning(self):
        self.set_live(self.live)
        self.stamp(self.base)
        self.assertEqual(self.deploy(force=True), 2)
        self.assertTrue(any('--force' in text and 'drops live commits' in text for text in self.warnings))
        self.assertEqual(self.record()['commit'], self.base)

    def test_first_deploy_is_allowed_and_recorded(self):
        self.stamp(self.base)
        self.assertEqual(self.deploy(), 2)
        self.assertEqual(self.record()['commit'], self.base)
        self.assertTrue(any('No .live-commit yet' in text for text in self.warnings))

    def test_unknown_live_commit_is_refused_with_fetch_hint(self):
        self.set_live('f' * 40)
        self.stamp(self.live)
        with self.assertRaisesRegex(deploy_ui.Refused, 'fetch'):
            self.deploy()
        self.assert_untouched()
        self.assertEqual(self.deploy(force=True), 2)
        self.assertEqual(self.record()['commit'], self.live)

    def test_dirty_build_is_refused_unless_forced_and_recorded_dirty(self):
        self.set_live(self.base)
        self.stamp(self.live, dirty=True, changes=['ui/src/App.tsx'])
        with self.assertRaisesRegex(deploy_ui.Refused, 'uncommitted changes .*ui/src/App.tsx'):
            self.deploy()
        self.assert_untouched()
        self.assertEqual(self.deploy(force=True), 2)
        self.assertTrue(self.record()['dirty'])

    def test_missing_stamp_is_refused_and_force_clears_the_record(self):
        self.set_live(self.base)
        with self.assertRaisesRegex(deploy_ui.Refused, 'no .build-commit'):
            self.deploy()
        self.assert_untouched()
        self.assertEqual(self.deploy(force=True), 2)
        self.assertFalse((self.target / '.live-commit').exists())

    def test_dry_run_checks_without_installing(self):
        self.set_live(self.live)
        self.stamp(self.base)
        with self.assertRaises(deploy_ui.Refused):
            self.deploy(dry_run=True)
        self.stamp(self.live)
        self.assertEqual(self.deploy(dry_run=True), 0)
        self.assert_untouched()
        self.assertEqual(self.record()['commit'], self.live)

    def test_record_live_writes_full_commit_and_entry(self):
        (self.target / 'index.html').write_text('<script type="module" src="/chat/assets/index-abc.js"></script>')
        self.assertEqual(deploy_ui.record_live(self.live[:8], self.target, repo=self.repo), self.live)
        self.assertEqual(self.record()['commit'], self.live)
        self.assertEqual(self.record()['entry'], 'assets/index-abc.js')
        with self.assertRaises(ValueError):
            deploy_ui.record_live('f' * 40, self.target, repo=self.repo)

    def test_parallel_deploy_to_the_same_target_is_refused(self):
        self.stamp(self.live)
        with deploy_ui.locked(self.target.resolve()):
            with self.assertRaisesRegex(ValueError, 'Another UI deploy'):
                self.deploy(force=True)
        self.assert_untouched()
        self.assertEqual(self.deploy(), 2)

    def test_record_write_failure_after_publish_blocks_the_next_deploy(self):
        self.set_live(self.base)
        self.stamp(self.live)
        write = deploy_ui.write_record
        def fail_final(target, record, name=deploy_ui.RECORD):
            if name == deploy_ui.RECORD:
                raise OSError('disk full')
            write(target, record, name)
        with patch.object(deploy_ui, 'write_record', side_effect=fail_final):
            with self.assertRaises(OSError):
                self.deploy()
        self.assertEqual((self.target / 'assets/new.js').read_text(), 'new')
        self.stamp(self.base)  # a rollback would now pass against the stale record
        with self.assertRaisesRegex(deploy_ui.Refused, 'did not finish'):
            self.deploy()
        deploy_ui.record_live(self.live, self.target, repo=self.repo)
        self.assertFalse((self.target / '.live-commit.pending').exists())
        with self.assertRaisesRegex(deploy_ui.Refused, 'drops live commits'):
            self.deploy()

    def test_failure_before_publish_keeps_the_old_record_valid(self):
        self.set_live(self.base)
        self.stamp(self.live)
        with self.fail_asset_install(), self.assertRaises(OSError):
            self.deploy()
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')
        self.assertFalse((self.target / '.live-commit.pending').exists())
        self.assertEqual(self.deploy(), 2)

    def test_force_overrides_git_that_does_not_start(self):
        self.set_live(self.base)
        self.stamp(self.live)
        with patch.object(deploy_ui.subprocess, 'run', side_effect=FileNotFoundError('git')):
            with self.assertRaisesRegex(deploy_ui.Refused, 'git did not start'):
                self.deploy()
            self.assertEqual(self.deploy(force=True), 2)

    def fail_asset_install(self):
        replace = deploy_ui.os.replace
        def fail_asset(source, target):
            if Path(target).name == 'new.js':
                raise OSError('disk error')
            replace(source, target)
        return patch.object(deploy_ui.os, 'replace', side_effect=fail_asset)

    def test_failed_unstamped_force_keeps_the_live_record(self):
        self.set_live(self.live)
        with self.fail_asset_install(), self.assertRaises(OSError):
            self.deploy(force=True)
        self.assertEqual(self.record()['commit'], self.live)
        self.assertFalse((self.target / '.live-commit.pending').exists())
        self.stamp(self.base)
        with self.assertRaisesRegex(deploy_ui.Refused, 'drops live commits'):
            self.deploy()

    def test_failed_forced_recovery_keeps_the_earlier_pending_file(self):
        self.set_live(self.base)
        (self.target / '.live-commit.pending').write_text(json.dumps({'commit': self.live}))
        self.stamp(self.live)
        with self.fail_asset_install(), self.assertRaises(OSError):
            self.deploy(force=True)
        self.assertEqual(json.loads((self.target / '.live-commit.pending').read_text()), {'commit': self.live})
        self.stamp(self.base)
        with self.assertRaisesRegex(deploy_ui.Refused, 'did not finish'):
            self.deploy()


if __name__ == '__main__':
    unittest.main()
