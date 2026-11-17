"""B189: verification stamps on DEPLOYED.md lines and commit trailers. Real git in temp folders."""
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
        self.assertEqual(verification.status('Verified: looked at it'), 'missing')  # malformed counts as missing
        self.assertEqual(verification.status(None), 'missing')

    def test_line_has_time_commit_text_and_stamp(self):
        now = datetime(2026, 11, 17, 14, 5, tzinfo=timezone(timedelta(hours=5, minutes=30), 'IST'))
        self.assertEqual(verification.line(' Backend  restart\n for B189 ', 'tests(server ok)', 'abc1234', now),
                         '- 2026-11-17 14:05 IST · abc1234 Backend restart for B189 · Verified: tests(server ok)')
        with self.assertRaisesRegex(ValueError, 'Say what'):
            verification.line(' ', 'none', now=now)
        with self.assertRaisesRegex(ValueError, '--verified'):
            verification.line('x Verified: tests(a)', 'none', now=now)
        with self.assertRaises(ValueError):
            verification.line('x', 'ok', now=now)


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
            '- 2026-13-40 · bad date',
            'not dated · Verified: none')))
        found = verification.unverified_deploys(self.log, date(2026, 11, 15))
        self.assertEqual([kind for _, kind in found], ['missing', 'none'])
        self.assertTrue(found[0][0].startswith('- 2026-11-16 09:00 IST · aaa1111'))
        self.assertEqual(verification.unverified_deploys(self.root / 'absent.md', date(2026, 1, 1)), [])

    def test_log_path_prefers_the_environment_then_the_main_checkout(self):
        with patch.dict(os.environ, {'TETHER_DEPLOYED': str(self.log)}):
            self.assertEqual(verification.log_path(self.root), self.log)
        with patch.dict(os.environ, {'TETHER_DEPLOYED': ''}):
            with self.assertRaisesRegex(ValueError, 'TETHER_DEPLOYED'):
                verification.log_path(self.root)


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
                              env={**os.environ, **GIT_ENV}).stdout.strip()

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


if __name__ == '__main__':
    unittest.main()
