"""Regression checks for preserving open-tab assets during an internal UI deploy."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from functools import partial
import importlib.util
import io
import json
import os
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
        self.enterContext(patch.object(deploy_ui.review_gate, 'pane_identity', return_value=None))
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
        return deploy_ui.deploy(self.build, self.target, force=True, warn=lambda text: None,
                                verified='none (test)', log=Path(self.folder.name) / 'DEPLOYED.md')

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
            deploy_ui.deploy(self.build, self.target, verified='none (test)', log=Path(self.folder.name) / 'DEPLOYED.md')
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')

    def test_failed_asset_install_keeps_old_entry(self):
        with patch.object(deploy_ui.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.deploy()
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')


class GuardCase(unittest.TestCase):
    """A real git repository, a stamped build and a live target."""

    def setUp(self):
        self.identity = self.enterContext(patch.object(deploy_ui.review_gate, 'pane_identity', return_value=None))
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
        self.log = root / 'DEPLOYED.md'

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@example.invalid', '-c', 'user.name=t',
                               *args], check=True, capture_output=True, text=True, timeout=5,
                              env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).stdout.strip()

    def commit(self, message):
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def stamp(self, commit, dirty=False, changes=()):
        (self.build / '.build-commit').write_text(json.dumps({'commit': commit, 'dirty': dirty, 'changes': list(changes)}))

    def set_live(self, commit):
        (self.target / '.live-commit').write_text(json.dumps({'commit': commit, 'dirty': False}))

    def deploy(self, **options):
        options.setdefault('verified', 'tests(scripts ok)')
        options.setdefault('log', self.log)
        return deploy_ui.deploy(self.build, self.target, repo=self.repo, warn=self.warnings.append, **options)

    def record(self):
        return json.loads((self.target / '.live-commit').read_text())

    def assert_untouched(self):
        self.assertEqual((self.target / 'index.html').read_text(), 'old entry')
        self.assertFalse((self.target / 'assets/new.js').exists())



class DeployGuardTest(GuardCase):
    """B92: a build must contain the live commit, or the install would remove deployed work."""

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


class VerifiedStampTest(GuardCase):
    """B189: every UI deploy says how it was verified, and its DEPLOYED.md line carries the stamp."""

    def test_missing_or_malformed_stamp_is_refused_before_any_check_or_change(self):
        self.set_live(self.base)
        self.stamp(self.live)
        for stamp in (None, '', '  ', 'tested it', 'tests', 'Verified:', 'none of it'):
            for dry_run in (False, True):
                with self.subTest(stamp=stamp, dry_run=dry_run):
                    with self.assertRaisesRegex(ValueError, '--verified is missing'):
                        self.deploy(verified=stamp, dry_run=dry_run, force=True)
        self.assert_untouched()
        self.assertFalse(self.log.exists())
        self.assertEqual(self.record()['commit'], self.base)

    def test_install_records_the_stamp_and_adds_one_deployed_line(self):
        self.set_live(self.base)
        self.stamp(self.live)
        stamp = 'Verified:  tests(server 412 ok, ui 91/93)   screenshots(24) review(codex APPROVED)'
        self.assertEqual(self.deploy(verified=stamp), 2)
        clean = 'tests(server 412 ok, ui 91/93) screenshots(24) review(codex APPROVED)'
        self.assertEqual(self.record()['verified'], clean)
        line, = self.log.read_text().splitlines()
        self.assertRegex(line, r'^- \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+ · ')
        self.assertTrue(line.endswith(f'{self.live[:7]} UI deploy: live feature · Verified: {clean}'), line)

    def test_explicit_none_installs_and_is_listed_as_unverified(self):
        self.stamp(self.live)
        self.log.write_text('# Deploys\n- 2026-01-01 10:00 IST · old line')  # no final newline
        self.assertEqual(self.deploy(verified='none (copy fix)'), 2)
        self.assertEqual(self.record()['verified'], 'none (copy fix)')
        lines = self.log.read_text().splitlines()
        self.assertEqual(lines[:2], ['# Deploys', '- 2026-01-01 10:00 IST · old line'])
        found = deploy_ui.verification.unverified_deploys(self.log, deploy_ui.datetime(2026, 1, 1).date())
        self.assertEqual([kind for _, kind in found], ['missing', 'none'])
        self.assertIn('none (copy fix)', found[1][0])

    def test_dry_run_writes_no_line(self):
        self.stamp(self.live)
        self.assertEqual(self.deploy(dry_run=True), 0)
        self.assertFalse(self.log.exists())

    def test_commits_without_a_trailer_warn_but_do_not_refuse(self):
        self.set_live(self.base)
        stamped = self.commit('stamped work\n\nVerified: tests(server ok)')
        bare = self.commit('bare work')
        self.stamp(bare)
        self.assertEqual(self.deploy(dry_run=True), 0)
        warning, = [text for text in self.warnings if 'observe mode' in text]
        self.assertIn(f'[missing] {bare[:7]} bare work', warning)
        self.assertIn(f'[missing] {self.live[:7]} live feature', warning)
        self.assertNotIn(stamped[:7], warning)
        self.warnings.clear()
        self.set_live(bare)
        none = self.commit('docs only\n\nVerified: none (docs)')
        self.stamp(none)
        self.assertEqual(self.deploy(), 2)
        warning, = [text for text in self.warnings if 'observe mode' in text]
        self.assertIn(f'[none] {none[:7]} docs only', warning)

    def test_fully_stamped_range_gives_no_warning(self):
        self.set_live(self.live)
        self.stamp(self.commit('fix\n\nVerified: review(codex APPROVED)'))
        self.assertEqual(self.deploy(), 2)
        self.assertFalse([text for text in self.warnings if 'observe mode' in text])

    def test_subject_that_says_verified_still_gets_its_line(self):
        self.set_live(self.live)
        stamped = self.commit('Document Verified: trailer rules')
        self.stamp(stamped)
        self.assertEqual(self.deploy(), 2)
        line, = self.log.read_text().splitlines()
        self.assertIn(f'{stamped[:7]} UI deploy: Document Verified trailer rules · Verified: tests(scripts ok)', line)
        self.assertEqual(deploy_ui.verification.status(line), 'verified')

    def test_log_write_failure_after_install_warns_with_the_command(self):
        self.stamp(self.live)
        self.assertEqual(self.deploy(log=self.target / 'missing-folder' / 'DEPLOYED.md'), 2)
        self.assertTrue(any('verification.py log --verified "tests(scripts ok)"' in text for text in self.warnings))
        self.assertEqual(self.record()['commit'], self.live)

    def test_cli_refuses_install_without_verified(self):
        self.stamp(self.live)
        argv = ['deploy-ui.py', str(self.build), str(self.target)]
        with patch.object(deploy_ui.sys, 'argv', argv), patch.object(deploy_ui, 'REPO', self.repo), \
                patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
            deploy_ui.main()
        self.assertEqual(caught.exception.code, 1)
        self.assert_untouched()


class ReviewGateTest(GuardCase):
    def setUp(self):
        super().setUp()
        self.identity.return_value = '%42'
        self.store = self.repo.parent / 'review-links.json'
        self.enterContext(patch.object(deploy_ui.review_gate, '_store_path', return_value=self.store))
        self.approve(self.base)

    def approve(self, commit):
        self.store.write_text(json.dumps([{'worker': '%42', 'reviewer': '%43', 'approved_commit': commit}]))

    def test_force_cannot_install_a_commit_the_reviewer_did_not_approve(self):
        self.stamp(self.live)
        with self.assertRaisesRegex(deploy_ui.ReviewRefused, self.live[:12]):
            self.deploy(force=True)
        self.assert_untouched()
        self.assertFalse(self.log.exists())

    def test_dry_run_cli_reports_the_refusal_and_exits_one(self):
        self.stamp(self.live)
        argv = ['deploy-ui.py', '--verified', 'tests(ui ok)', '--force', '--dry-run', str(self.build), str(self.target)]
        stderr = io.StringIO()
        with patch.object(deploy_ui.sys, 'argv', argv), patch.object(deploy_ui, 'deploy', partial(
                deploy_ui.deploy, repo=self.repo, log=self.log)), patch('sys.stderr', stderr), \
                self.assertRaises(SystemExit) as caught:
            deploy_ui.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('Deployment refused by the review gate.', stderr.getvalue())
        self.assertIn(self.live[:12], stderr.getvalue())
        self.assertIn('stop the review link with Mak', stderr.getvalue())
        self.assert_untouched()

    def test_force_cannot_install_a_dirty_or_missing_stamp_for_a_linked_pane(self):
        with self.assertRaisesRegex(deploy_ui.ReviewRefused, 'no valid commit stamp'):
            self.deploy(force=True)
        with self.assertRaisesRegex(deploy_ui.ReviewRefused, 'no valid commit stamp'):
            self.deploy(dry_run=True)
        self.stamp(self.base, dirty=True)
        with self.assertRaisesRegex(deploy_ui.ReviewRefused, 'does not show a clean checkout'):
            self.deploy(force=True)
        self.assert_untouched()

    def test_checked_commit_comes_from_the_stamp_not_head(self):
        self.stamp(self.base)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.live)
        self.assertEqual(self.deploy(), 2)
        self.assertEqual(self.record()['commit'], self.base)

    def test_unlinked_pane_can_use_the_existing_force_behavior(self):
        self.store.write_text('[]')
        self.assertEqual(self.deploy(force=True), 2)
        self.assertFalse((self.target / '.live-commit').exists())

    def test_missing_review_store_stops_force(self):
        self.store.unlink()
        self.stamp(self.base)
        with self.assertRaisesRegex(deploy_ui.ReviewRefused, 'could not read the review store'):
            self.deploy(force=True)
        self.assert_untouched()

    def test_merge_forward_base_comes_from_the_remote_not_the_tracking_ref(self):
        self.git('remote', 'add', 'origin', str(self.repo))
        self.git('update-ref', 'refs/heads/main', self.base)
        self.git('update-ref', 'refs/remotes/origin/main', self.live)
        self.assertEqual(deploy_ui.review_base(self.repo), self.base)

    def test_merge_forward_base_must_be_available_locally(self):
        result = subprocess.CompletedProcess([], 0, stdout=f'{"f" * 40}\trefs/heads/main\n')
        git = deploy_ui.git
        with patch.object(deploy_ui, 'git', side_effect=lambda repo, *args, **options:
                          result if args[0] == 'ls-remote' else git(repo, *args, **options)):
            self.assertIsNone(deploy_ui.review_base(self.repo))


class ScreenshotProofTest(GuardCase):
    NO_LIVE_WARNING = ('This deploy names no screenshots folder and there is no live record to compare with. '
                       'Add --shots DIR (observe mode, not refused).')

    def setUp(self):
        super().setUp()
        self.set_live(self.live)
        files = ('ui/src/App.tsx', 'ui/public/new logo.webp', 'ui/tests/cards.ts', 'docs/guide.md', 'server/app.js')
        for name in files:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('changed\n')
        self.git('add', '.')
        self.stamp(self.commit('UI changes\n\nVerified: tests(ui ok)'))

    def cli(self, *options):
        argv = ['deploy-ui.py', '--verified', 'tests(ui ok)', *options, str(self.build), str(self.target)]
        deploy = partial(deploy_ui.deploy, repo=self.repo, log=self.log, warn=self.warnings.append)
        with patch.object(deploy_ui.sys, 'argv', argv), patch.object(deploy_ui, 'deploy', deploy), patch('sys.stdout'):
            deploy_ui.main()

    def test_cli_records_screenshot_folder_and_count_in_the_record_and_log(self):
        proof = self.repo / 'proof'
        proof.mkdir()
        (proof / 'before.png').touch()
        (proof / 'after.jpg').touch()
        self.cli('--shots', str(proof))
        self.assertEqual((self.record()['shots'], self.record()['shots_count']), ('proof', 2))
        self.assertIn('UI deploy: UI changes · Shots: proof · Verified: tests(ui ok)\n', self.log.read_text())
        self.assertEqual(self.warnings, ['Screenshots folder proof is temporary. A restart can remove the proof.'])

    def test_ui_changes_without_shots_warn_but_install(self):
        self.assertEqual(self.deploy(), 2)
        # App.tsx, the logo and the legacy server/app.js; the test file and the guide are not UI changes.
        self.assertEqual(self.warnings, ['This deploy changes 3 UI file(s) and names no screenshots folder. '
                                         'Add --shots DIR (observe mode, not refused).'])
        self.assertNotIn('shots', self.record())
        self.assertNotIn('Shots:', self.log.read_text())

    def test_build_config_change_alone_warns_like_the_review_request(self):
        # Review round 2, finding 4: the review asks for screenshots for ui/vite.config.ts; the deploy was silent.
        # Round 3, finding 2: the lock file alone changes what `npm ci` builds.
        for name in ('ui/vite.config.ts', 'ui/package.json', 'ui/package-lock.json', 'server/ui_manifest.json'):
            with self.subTest(name=name):
                self.set_live(self.git('rev-parse', 'HEAD'))
                (self.repo / name).write_text(f'changed {name}\n')
                self.git('add', '.')
                self.stamp(self.commit(f'Change {name}\n\nVerified: tests(ui ok)'))
                self.warnings.clear()
                self.assertEqual(self.deploy(dry_run=True), 0)
                self.assertEqual(self.warnings, ['This deploy changes 1 UI file(s) and names no screenshots folder. '
                                                 'Add --shots DIR (observe mode, not refused).'])

    def test_unreadable_ui_diff_has_no_shots_warning(self):
        self.assertEqual(self.deploy(dry_run=True), 0)
        self.assertTrue(any('names no screenshots folder' in warning for warning in self.warnings))
        self.warnings.clear()
        git = deploy_ui.verification.git

        def unreadable_diff(repo, *args):
            return None if args[0] == 'diff' else git(repo, *args)

        with patch.object(deploy_ui.verification, 'git', side_effect=unreadable_diff):
            self.assertEqual(self.deploy(dry_run=True), 0)
        self.assertFalse(self.warnings)

    def test_first_deploy_without_screenshots_warns(self):
        (self.target / '.live-commit').unlink()
        self.assertEqual(self.deploy(), 2)
        self.assertIn(self.NO_LIVE_WARNING, self.warnings)

    def test_first_deploy_with_missing_screenshots_folder_warns_and_installs(self):
        (self.target / '.live-commit').unlink()
        missing = self.repo / 'missing'
        self.cli('--shots', str(missing))
        self.assertIn(f'--shots: Screenshots folder does not exist or is not a folder: {missing.resolve()}. '
                      'The deploy line gets no Shots field (observe mode, not refused).', self.warnings)
        self.assertIn(self.NO_LIVE_WARNING, self.warnings)
        self.assertEqual((self.target / 'index.html').read_text(), (self.build / 'index.html').read_text())
        self.assertNotIn('shots', self.record())
        self.assertIn('UI deploy: UI changes · Verified: tests(ui ok)\n', self.log.read_text())

    def test_deploy_with_empty_screenshots_folder_warns_and_installs(self):
        empty = self.repo / 'empty'
        empty.mkdir()
        self.assertEqual(self.deploy(shots=empty), 2)
        self.assertTrue(self.warnings[0].startswith('--shots: Screenshots folder has no images:'))
        self.assertIn('names no screenshots folder', self.warnings[1])
        self.assertNotIn('shots', self.record())
        self.assertNotIn('Shots:', self.log.read_text())

    def test_screenshots_folder_cannot_bypass_guard(self):
        self.stamp(self.base)
        proof = self.repo / 'proof'
        proof.mkdir()
        (proof / 'after.png').touch()
        with patch.object(deploy_ui.verification, 'shots_folder', wraps=deploy_ui.verification.shots_folder) as scan:
            with self.assertRaisesRegex(deploy_ui.Refused, 'drops live commits'):
                self.deploy(shots=proof)
            scan.assert_not_called()
        self.assert_untouched()
        self.assertEqual(self.record()['commit'], self.live)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.warnings)

    def test_dry_run_reports_an_invalid_screenshots_folder_and_the_missing_proof(self):
        # Review round 2, finding 4: --dry-run with a folder that does not exist gave no warning at all.
        self.assertEqual(self.deploy(dry_run=True, shots=self.repo / 'missing'), 0)
        self.assertEqual(len(self.warnings), 2)
        self.assertTrue(self.warnings[0].startswith('--shots: Screenshots folder does not exist or is not a folder:'))
        self.assertTrue(self.warnings[1].startswith('This deploy changes 3 UI file(s) and names no screenshots folder.'))
        self.assert_untouched()
        self.assertFalse(self.log.exists())

    def test_dry_run_with_valid_screenshots_gives_no_proof_warning(self):
        proof = self.repo / 'proof'
        proof.mkdir()
        (proof / 'after.png').touch()
        self.assertEqual(self.deploy(dry_run=True, shots=proof), 0)
        self.assertFalse(any('names no screenshots folder' in warning or warning.startswith('--shots:')
                             for warning in self.warnings))
        self.assert_untouched()

    def test_force_bypasses_guard_even_when_screenshots_path_is_a_file(self):
        self.stamp(self.base)
        proof = self.repo / 'proof.png'
        proof.touch()
        self.assertEqual(self.deploy(force=True, shots=proof), 2)
        self.assertTrue(self.warnings[0].startswith('--force: installing anyway. The build drops live commits.'))
        self.assertTrue(self.warnings[1].startswith('--shots: Screenshots folder does not exist or is not a folder:'))
        self.assertEqual(self.record()['commit'], self.base)
        self.assertNotIn('Shots:', self.log.read_text())


if __name__ == '__main__':
    unittest.main()
