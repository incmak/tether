"""B189: verification stamps on DEPLOYED.md lines and commit trailers. Real git in temp folders."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import date, datetime, timedelta, timezone
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verification  # noqa: E402

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


class StampTest(unittest.TestCase):
    def test_valid_stamps_are_tidied(self):
        for given, expected in (
                ('tests(server 412 ok, ui 91/93) screenshots(24) review(codex APPROVED)',
                 'tests(server 412 ok, ui 91/93) screenshots(24) review(codex APPROVED)'),
                ('Verified:  tests(a)\n review(b) ', 'tests(a) review(b)'),
                ('verified: none', 'none'),
                ('none (docs only)', 'none (docs only)'),
                ('live-check(stop on a throwaway tab)', 'live-check(stop on a throwaway tab)')):
            with self.subTest(given=given):
                self.assertEqual(verification.valid(given), expected)

    def test_vague_or_empty_stamps_are_refused(self):
        for given in (None, '', 'Verified:', 'yes', 'tested', 'tests', 'tests()', 'Tests(ok)', 'none yet',
                      'tests(ok) and review(ok)', 'tests((ok))', 'none (a) tests(b)'):
            with self.subTest(given=given), self.assertRaises(ValueError):
                verification.valid(given)

    def test_status_of_lines_and_messages(self):
        self.assertEqual(verification.status('- 2026-11-17 · abc · Verified: tests(ok)'), 'verified')
        self.assertEqual(verification.status('Fix\n\nVerified: none (copy)\nCo-Authored-By: x'), 'none')
        self.assertEqual(verification.status('- 2026-11-17 · deployed abc'), 'missing')
        self.assertEqual(verification.status('Fix\n\nVerified: Verified: tests(ok)'), 'missing')
        self.assertEqual(verification.status('Verified: none-such(ok)'), 'verified')
        self.assertEqual(verification.status('Verified: none(ok)'), 'verified')
        # In a commit message the line starts with "Verified:"; a DEPLOYED.md line carries it inline.
        self.assertEqual(verification.message_status('Document Verified: none'), 'missing')
        self.assertEqual(verification.message_status('Fix\n\nVerified: none (copy)\nCo-Authored-By: x'), 'none')
        self.assertEqual(verification.status('Verified: looked at it'), 'missing')  # malformed counts as missing
        self.assertEqual(verification.status(None), 'missing')

    def test_line_has_time_commit_text_and_stamp(self):
        now = datetime(2026, 11, 17, 14, 5, tzinfo=timezone(timedelta(hours=5, minutes=30), 'IST'))
        self.assertEqual(verification.line(' Backend  restart\n for B189 ', 'tests(server ok)', 'abc1234', now),
                         '- 2026-11-17 14:05 +0530 · abc1234 Backend restart for B189 · Verified: tests(server ok)')
        with self.assertRaisesRegex(ValueError, 'Say what'):
            verification.line(' ', 'none', now=now)
        with self.assertRaisesRegex(ValueError, '--verified'):
            verification.line('x Verified: tests(a)', 'none', now=now)
        with self.assertRaises(ValueError):
            verification.line('x', 'ok', now=now)

    def test_line_with_shots_keeps_the_stamp_last(self):
        now = datetime(2026, 11, 18, 5, 30, tzinfo=timezone.utc)
        line = verification.line('UI deploy', 'tests(ui ok)', 'abc1234', now, shots='proof/b207')
        self.assertEqual(line, '- 2026-11-18 05:30 +0000 · abc1234 UI deploy · Shots: proof/b207 '
                               '· Verified: tests(ui ok)')
        self.assertEqual(verification.status(line), 'verified')
        for invalid in ('Verified: proof', 'two · parts', 'two\nlines', 'ending\r'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '--shots'):
                verification.line('UI deploy', 'none', shots=invalid)


class FileTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.log = self.root / 'DEPLOYED.md'

    def test_append_adds_a_newline_only_when_missing(self):
        verification.append(self.log, 'one')
        self.log.write_text(self.log.read_text() + 'two')
        verification.append(self.log, 'three')
        self.assertEqual(self.log.read_text(), 'one\ntwo\nthree\n')

    def test_unverified_deploys_lists_none_and_missing_since_a_day(self):
        self.log.write_text('\n'.join((
            '# Deployed',
            '- 2026-11-10 09:00 IST · old · Verified: none',
            '- 2026-11-16 09:00 IST · aaa1111 hand line',
            '2026-11-16 · bbb2222 · Verified: tests(ok)',
            '- 2026-11-17 10:00 IST · ccc3333 UI · Verified: none (copy)',
            '- 2026-11-17 21:00 -0500 · ddd4444 · Verified: none (follow-up)',
            '- 2026-11-17 22:00 -05:00 · eee5555 hand line',
            '- 2026-13-40 · bad date',
            'not dated · Verified: none')))
        found = verification.unverified_deploys(self.log, date(2026, 11, 15))
        self.assertEqual([kind for _, kind in found], ['missing', 'none', 'none', 'missing'])
        self.assertTrue(found[0][0].startswith('- 2026-11-16 09:00 IST · aaa1111'))
        self.assertEqual(verification.unverified_deploys(self.root / 'absent.md', date(2026, 1, 1)), [])

    def test_log_path_prefers_the_environment_then_the_main_checkout(self):
        with patch.dict(os.environ, {'TETHER_DEPLOYED': str(self.log)}):
            self.assertEqual(verification.log_path(self.root), self.log)
        with patch.dict(os.environ, {'TETHER_DEPLOYED': ''}):
            with self.assertRaisesRegex(ValueError, 'TETHER_DEPLOYED'):
                verification.log_path(self.root)

    def test_shots_folder_requires_a_directory_with_images(self):
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            verification.shots_folder(self.root / 'missing', self.root)
        (self.root / 'notes.md').touch()
        with self.assertRaisesRegex(ValueError, 'no images'):
            verification.shots_folder(self.root, self.root)
        with self.assertRaisesRegex(ValueError, 'not a folder'):
            verification.shots_folder(self.root / 'notes.md', self.root)
        loop = self.root / 'loop'
        loop.symlink_to(loop)
        with self.assertRaisesRegex(ValueError, 'Screenshots folder'):
            verification.shots_folder(loop, self.root)

    def test_shots_folder_counts_images_and_rewrites_paths(self):
        proof = self.root / 'proof'
        after = proof / 'after'
        after.mkdir(parents=True)
        for name in ('one.PNG', 'two.jpg', 'three.jpeg', 'four.webp', 'five.gif', 'notes.txt'):
            (after / name).touch()
        (after / 'folder.png').mkdir()
        shown, count, warnings = verification.shots_folder(proof, self.root)
        self.assertEqual((shown, count), ('proof', 5))
        self.assertEqual(len(warnings), 1)
        self.assertIn('temporary', warnings[0])
        with patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(verification.shots_folder(proof, self.root / 'other')[0], '~/proof')
        with patch.object(Path, 'home', return_value=self.root / 'other'):
            self.assertEqual(verification.shots_folder(proof, self.root / 'other')[0], str(proof.resolve()))


class GitTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.repo = self.root / 'main'
        self.repo.mkdir()
        self.git(self.repo, 'init', '-q', '-b', 'main')
        self.env = patch.dict(os.environ, GIT_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, folder, *args):
        return subprocess.run(['git', '-C', str(folder), *args], check=True, capture_output=True, text=True,
                              timeout=5, env={**os.environ, **GIT_ENV, 'GIT_OPTIONAL_LOCKS': '0'}).stdout.strip()

    def commit(self, message):
        self.git(self.repo, 'commit', '-q', '--allow-empty', '-m', message)
        return self.git(self.repo, 'rev-parse', '--short=7', 'HEAD')

    def test_log_path_from_a_linked_worktree_is_the_main_checkout(self):
        self.commit('base')
        linked = self.root / 'linked'
        self.git(self.repo, 'worktree', 'add', '-q', str(linked), '-b', 'side')
        with patch.dict(os.environ, {'TETHER_DEPLOYED': ''}):
            self.assertEqual(verification.log_path(linked / '.').resolve(), (self.repo / 'DEPLOYED.md').resolve())

    def test_unverified_commits_and_ranges(self):
        base = self.commit('base\n\nVerified: tests(ok)')
        bare = self.commit('bare')
        none = self.commit('docs\n\nVerified: none (docs)\n\nCo-Authored-By: x <x@example.com>')
        good = self.commit('fix\n\nVerified: tests(server ok) review(codex APPROVED)')
        self.commit('bad\n\nVerified: I checked')
        since = date.today() - timedelta(days=1)
        found = verification.unverified_commits(self.repo, since)
        self.assertEqual([(short, kind) for short, _, kind in found][::-1], [(bare, 'missing'), (none, 'none'),
                                                                           (found[0][0], 'missing')])
        self.assertNotIn(good, [short for short, _, _ in found])
        self.assertNotIn(base, [short for short, _, _ in found])
        self.assertEqual([short for short, _, _ in verification.unverified_commit_range(self.repo, bare, good)], [none])
        self.assertEqual(verification.unverified_commit_range(self.repo, 'f' * 40, 'HEAD'), [])

    def test_cli_log_and_unverified(self):
        sha = self.commit('work\n\nVerified: tests(ok)')
        log = self.root / 'DEPLOYED.md'
        with patch.dict(os.environ, {'TETHER_DEPLOYED': str(log)}), patch('sys.stdout', new=io.StringIO()) as out:
            verification.main(['log', '--verified', 'none (backend restart only)', '--commit', sha,
                               'Backend restart'])
            verification.main(['unverified', '--repo', str(self.repo), '--days', '1'])
        self.assertRegex(log.read_text(), rf'^- .* · {sha} Backend restart · Verified: none \(backend restart only\)\n$')
        self.assertIn('Deploys since', out.getvalue())
        self.assertIn('[none] - ', out.getvalue())
        self.assertIn('Commits since', out.getvalue())
        for argv in (['log', 'text'], ['log', '--verified', 'ok', 'text'],
                     ['log', '--verified', 'none', '--commit', 'HEAD', 'text'], ['check', 'tested']):
            with self.subTest(argv=argv), patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
                verification.main(argv)
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_cli_log_with_shots_records_the_folder_and_warns_on_stderr(self):
        proof = self.repo / 'proof'
        proof.mkdir()
        (proof / 'after.png').touch()
        log = self.root / 'DEPLOYED.md'
        with patch.dict(os.environ, {'TETHER_DEPLOYED': str(log)}), \
                patch.object(verification, 'HERE', self.repo), patch('sys.stdout'), \
                patch('sys.stderr', new=io.StringIO()) as err:
            verification.main(['log', '--verified', 'tests(ok)', '--shots', str(proof), 'UI deploy'])
        self.assertIn('Screenshots folder proof is temporary', err.getvalue())
        self.assertTrue(log.read_text().endswith('UI deploy · Shots: proof · Verified: tests(ok)\n'))

    def test_cli_log_with_invalid_shots_warns_on_stderr_and_writes_the_line(self):
        empty = self.root / 'empty'
        empty.mkdir()
        unsafe = self.root / 'proof · after'
        unsafe.mkdir()
        (unsafe / 'after.png').touch()
        log = self.root / 'DEPLOYED.md'
        with patch.dict(os.environ, {'TETHER_DEPLOYED': str(log)}), patch('sys.stdout'):
            for path, reason in ((self.root / 'missing', 'Screenshots folder does not exist or is not a folder'),
                                 (empty, 'Screenshots folder has no images'),
                                 (unsafe, 'Give --shots a folder name')):
                with self.subTest(path=path), patch('sys.stderr', new=io.StringIO()) as err:
                    verification.main(['log', '--verified', 'tests(ok)', '--shots', str(path), 'UI deploy'])
                    self.assertIn(f'Warning: --shots: {reason}', err.getvalue())
                    self.assertIn('The deploy line gets no Shots field (observe mode, not refused).', err.getvalue())
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.endswith('UI deploy · Verified: tests(ok)') for line in lines))


if __name__ == '__main__':
    unittest.main()
