"""Reliability checklist (B209): row parsing, diff triggers, and the three results on real temporary repositories."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import reliability_checklist as checklist
import review_scope
from unittest_environment import isolate

ROOT = Path(__file__).resolve().parent.parent
ROWS = '''# Checklist

Intro text.

## R1: Stop only a private tmux server
- Paths: `\\.(py|sh)$`
- Text: `kill-server`
- Check: The command names a private socket.
- Incident: commit 62ebf51

## R2: Share tests drain preview jobs
- Paths: `^server/test_share.*\\.py$`
- Check: Wait for the preview queue.
- Incident: B22

## R3: No personal paths
- Text: `/(home|Users)/[a-z]`
- Check: Use a neutral path.
- Incident: commit 048b0da
'''


def run(folder, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                           '-c', 'commit.gpgsign=false', *args], cwd=folder, capture_output=True, check=True,
                          text=True, timeout=5, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).stdout.strip()


class ParseTests(unittest.TestCase):
    def test_rows_keep_their_fields_and_regex_sources(self):
        rows, errors = checklist.parse(ROWS)
        self.assertEqual(errors, [])
        self.assertEqual([row['id'] for row in rows], ['R1', 'R2', 'R3'])
        self.assertEqual(rows[0], {'id': 'R1', 'title': 'Stop only a private tmux server', 'paths': r'\.(py|sh)$',
                                   'text': 'kill-server', 'check': 'The command names a private socket.',
                                   'incident': 'commit 62ebf51'})
        self.assertEqual((rows[1]['text'], rows[2]['paths']), ('', ''))

    def test_invalid_rows_are_named_and_left_out(self):
        text = ('## R1: No trigger\n- Check: c\n- Incident: i\n'
                '## R2: Bad regex\n- Paths: `(`\n- Check: c\n- Incident: i\n'
                '## R3: No check\n- Text: `x`\n- Incident: i\n'
                '## R4: No incident\n- Text: `x`\n- Check: c\n'
                '## R5: Fine\n- Text: `x`\n- Check: c\n- Incident: i\n'
                '## R5: Again\n- Text: `y`\n- Check: c\n- Incident: i\n'
                '## Notes\n- Text: `not a row`\n')
        rows, errors = checklist.parse(text)
        self.assertEqual([row['title'] for row in rows], ['Fine'])
        self.assertEqual(len(errors), 5)
        for expected in ('R1 has no Paths and no Text trigger', 'R2 paths is not a valid regular expression',
                         'R3 has no Check', 'R4 has no Incident', 'R5 is defined twice'):
            self.assertTrue(any(error.startswith(expected) for error in errors), expected)

    def test_a_field_outside_a_row_and_text_after_another_heading_are_ignored(self):
        rows, errors = checklist.parse('- Text: `x`\n## R1: A\n- Text: `a`\n- Check: c\n- Incident: i\n'
                                       '# Other\n- Text: `changed`\n')
        self.assertEqual((errors, [row['text'] for row in rows]), ([], ['a']))

    def test_the_repository_checklist_is_valid_and_names_the_seed_incidents(self):
        text = (ROOT / 'docs' / 'reliability-checklist.md').read_text()
        rows, errors = checklist.parse(text)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(rows), 4)
        incidents = ' '.join(row['incident'] + row['title'] for row in rows)
        for seed in ('kill-server', 'B25', 'B188', 'B22'):
            self.assertIn(seed, incidents)

    def test_each_seed_row_hits_a_diff_like_its_incident_and_not_an_unrelated_one(self):
        rows, _ = checklist.parse((ROOT / 'docs' / 'reliability-checklist.md').read_text())
        def ids(changes):
            return [row['id'] for row in checklist.hits(rows, changes)]
        self.assertEqual(ids({'server/test_scrollback.py': ["run([paths.TMUX, 'kill-server'], capture_output=True)"]}), ['R1'])
        self.assertEqual(ids({'server/urgent_ping.py': ["return base_url + '/mobile/#session=' + pane"]}), ['R2'])
        self.assertEqual(ids({'server/project_launch.py': ["args += ['--add-dir', folder]"]}), ['R3'])
        self.assertEqual(ids({'server/test_share_api.py': ['self.assertTrue(done)']}), ['R4'])
        self.assertEqual(ids({'ui/src/Composer.tsx': ['const label = "Send"'], 'server/push.py': ['count += 1'],
                              'README.md': ['Tether is a browser chat UI.']}), [])


class HitTests(unittest.TestCase):
    def setUp(self):
        self.rows, _ = checklist.parse(ROWS)

    def ids(self, changes):
        return [(row['id'], row['files']) for row in checklist.hits(self.rows, changes)]

    def test_paths_and_text_must_match_the_same_file(self):
        self.assertEqual(self.ids({'a.py': ['tmux kill-server']}), [('R1', ['a.py'])])
        self.assertEqual(self.ids({'a.py': ['tmux ls'], 'notes.txt': ['kill-server']}), [])
        self.assertEqual(self.ids({'a.py': []}), [])  # a deleted file adds no line

    def test_paths_alone_hit_any_change_of_the_file_and_text_alone_hits_any_file(self):
        self.assertEqual(self.ids({'server/test_share_api.py': []}), [('R2', ['server/test_share_api.py'])])
        sample = '/' + 'home' + '/user/x'
        self.assertEqual(self.ids({'ui/x.cjs': [f'open({sample!r})'], 'b.md': ['/Users/name']}),
                         [('R3', ['b.md', 'ui/x.cjs'])])

    def test_added_lines_reads_only_added_lines_of_each_file(self):
        patch_text = ('diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n'
                      '-old kill-server\n+new line\n+++ not a header\n'
                      'diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n'
                      '@@ -1 +0,0 @@\n-kill-server\n'
                      'diff --git a/my file.sh b/my file.sh\nnew file mode 100644\n--- /dev/null\n+++ b/my file.sh\t\n'
                      '@@ -0,0 +1 @@\n+tmux kill-server\n')
        names = ['a.py', 'gone.py', 'my file.sh']
        self.assertEqual(checklist.added_lines(patch_text, names),
                         {'a.py': ['new line', '++ not a header'], 'gone.py': [], 'my file.sh': ['tmux kill-server']})
        for mismatched in (names[:-1], names + ['other.txt']):
            with self.subTest(names=mismatched), self.assertRaises(checklist.Unchecked):
                checklist.added_lines(patch_text, mismatched)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        isolate()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin, self.repo = self.root / 'origin.git', self.root / 'work'
        run(self.root, 'init', '--bare', '-b', 'main', str(self.origin))
        run(self.root, 'init', '-b', 'main', str(self.repo))
        run(self.repo, 'remote', 'add', 'origin', str(self.origin))
        self.write('docs/reliability-checklist.md', ROWS)
        self.write('server/app.py', 'print(1)\n')
        self.commit('Start')
        run(self.repo, 'push', '-q', 'origin', 'main')
        run(self.repo, 'checkout', '-q', '-b', 'work')

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, subject):
        run(self.repo, 'add', '-A')
        run(self.repo, 'commit', '-q', '-m', subject)
        return run(self.repo, 'rev-parse', 'HEAD')

    def check(self, **options):
        return checklist.check(self.repo, **options)

    def test_clear_hit_for_committed_uncommitted_and_untracked_work(self):
        base = run(self.repo, 'rev-parse', 'origin/main')
        self.assertEqual(self.check(), {'status': 'clear', 'rows': [], 'paths': [], 'base': base, 'reason': ''})
        self.write('server/app.py', 'print(2)\n')
        self.assertEqual((self.check()['status'], self.check()['paths']), ('clear', ['server/app.py']))
        self.write('server/test_share_api.py', 'pass\n')  # untracked
        result = self.check()
        self.assertEqual(result['status'], 'hit')
        self.assertEqual(result['rows'], [{'id': 'R2', 'title': 'Share tests drain preview jobs',
                                           'check': 'Wait for the preview queue.', 'incident': 'B22',
                                           'files': ['server/test_share_api.py']}])
        head = self.commit('Share test')
        self.write('tools/stop.sh', 'tmux kill-server\n')
        self.assertEqual([row['id'] for row in self.check()['rows']], ['R1', 'R2'])
        # --head reads the commits only: the uncommitted script is not in them.
        self.assertEqual([row['id'] for row in self.check(head=head)['rows']], ['R2'])
        self.assertEqual(self.check(head=head)['paths'], ['server/app.py', 'server/test_share_api.py'])

    def test_the_checklist_comes_from_the_base_ref_not_from_the_branch(self):
        self.write('docs/reliability-checklist.md', '# Checklist\n')  # the branch removes every row
        self.write('tools/stop.sh', 'tmux kill-server\n')
        self.commit('Remove rows')
        self.assertEqual([row['id'] for row in self.check()['rows']], ['R1'])
        result = self.check(file=self.repo / 'docs/reliability-checklist.md')
        self.assertEqual((result['status'], result['reason']), ('unchecked', 'the checklist has no rows'))

    def test_changed_paths_are_repository_relative_from_any_subfolder(self):
        self.write('server/test_share_api.py', 'pass\n')
        self.write('tools/stop.sh', 'tmux kill-server\n')
        expected = self.check()
        self.assertEqual(expected['paths'], ['server/test_share_api.py', 'tools/stop.sh'])
        self.assertEqual([row['id'] for row in expected['rows']], ['R1', 'R2'])
        for subfolder in ('server', 'tools'):
            with self.subTest(subfolder=subfolder):
                self.assertEqual(checklist.check(self.repo / subfolder), expected)

    def test_explicit_commit_check_works_in_a_bare_repository(self):
        self.assertEqual(checklist.check(self.origin, base='main', head='main')['status'], 'clear')

    def test_diff_attributes_cannot_hide_added_text_in_either_mode(self):
        self.write('.gitattributes', 'tools/stop.sh -diff\ntools/binary.py binary\n')
        self.write('tools/stop.sh', 'true\n')
        self.write('tools/binary.py', 'pass\n')
        self.commit('Add tools and attributes')
        self.write('tools/stop.sh', 'tmux kill-server\n')
        self.write('tools/binary.py', 'tmux kill-server\0\n')
        for committed in (False, True):
            head = self.commit('Change both tools') if committed else None
            with self.subTest(head=head):
                self.assertEqual(self.check(head=head)['rows'][0]['files'], ['tools/binary.py', 'tools/stop.sh'])

    def test_quoted_names_and_relative_diff_config_in_both_modes(self):
        names = ['tools/with"quote.sh', 'tools/with\ttab.py', 'tools/with-é.sh']
        for name in names:
            self.write(name, 'true\n')
        self.commit('Add tools')
        for name in names:
            self.write(name, '+tmux kill-server\ndiff --git part of the file\n')
        run(self.repo, 'config', 'diff.relative', 'true')
        for committed in (False, True):
            head = self.commit('Change tools') if committed else None
            for folder in (self.repo, self.repo / 'server'):
                with self.subTest(committed=committed, folder=folder):
                    result = checklist.check(folder, head=head)
                    self.assertEqual((result['status'], result['rows'][0]['files']), ('hit', sorted(names)))

    def test_untracked_over_the_file_limit_hits_both_kinds_of_text_rows(self):
        self.write('tools/stop.sh', 'true\n' * 3 + 'tmux kill-server\n')
        self.write('notes.txt', 'clean\n')
        with patch.object(checklist, 'MAX_FILE', 10):
            result = self.check()
            self.assertEqual([(r['id'], r['files']) for r in result['rows']],
                             [('R1', ['tools/stop.sh']), ('R3', ['tools/stop.sh'])])
            (self.repo / 'tools/stop.sh').write_text('true\n')
            self.assertEqual(self.check()['status'], 'clear')

    def test_unreadable_untracked_file_hits_text_rows(self):
        self.write('tools/unreadable.py', 'pass\n')
        path = self.repo / 'tools/unreadable.py'
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        if os.access(path, os.R_OK):
            self.skipTest('this process can read a file with no mode bits')
        result = self.check()
        self.assertEqual([(r['id'], r['files']) for r in result['rows']],
                         [('R1', ['tools/unreadable.py']), ('R3', ['tools/unreadable.py'])])

    def test_untracked_symlink_checks_the_link_text(self):
        self.write('tools/kill-server', 'true\n')
        (self.repo / 'tools/alias.sh').symlink_to('kill-server')
        result = self.check()
        self.assertEqual([(r['id'], r['files']) for r in result['rows']], [('R1', ['tools/alias.sh'])])

    def test_git_listing_warnings_do_not_hide_unreadable_directories(self):
        for name in ('ui/src/private', 'ui/public/private'):
            with self.subTest(name=name):
                self.write(f'{name}/hidden.txt', 'hidden\n')
                folder = self.repo / name
                folder.chmod(0)
                try:
                    if os.access(folder, os.R_OK | os.X_OK):
                        self.skipTest('this process can read a directory with no mode bits')
                    result = review_scope.scope(self.repo)
                    self.assertEqual(result['checklist']['status'], 'unchecked')
                    self.assertIn('could not open directory', result['checklist']['reason'])
                    self.assertIsNone(result['ui'])
                finally:
                    folder.chmod(0o700)

    def test_total_untracked_limit_is_shared_and_allows_an_exact_fit(self):
        with patch.object(checklist, 'MAX_UNTRACKED_BYTES', 10, create=True):
            self.write('one.txt', '12345')
            self.write('two.txt', '12345')
            self.assertEqual(self.check()['status'], 'clear')
            self.write('two.txt', '123456')
            result = self.check()
            self.assertEqual(result['status'], 'unchecked')
            self.assertIn('10 bytes', result['reason'])

    def test_git_checklist_read_timeout_or_failure_is_unchecked(self):
        real_run = subprocess.run
        for command in ('ls-tree', 'show'):
            for failure in (subprocess.TimeoutExpired(command, 5), subprocess.CompletedProcess([], 128, b'', b'error')):
                def fail_read(args, **options):
                    if args[3] != command:
                        return real_run(args, **options)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                with self.subTest(command=command, failure=failure):
                    with patch.object(checklist.subprocess, 'run', side_effect=fail_read):
                        result = self.check()
                    self.assertEqual(result['status'], 'unchecked')
                    self.assertIn(f'git {command}', result['reason'])

    def test_existing_origin_ref_with_an_unreadable_commit_is_not_absent(self):
        blob = run(self.repo, 'rev-parse', 'HEAD:server/app.py')
        ref = self.repo / '.git' / 'refs' / 'remotes' / 'origin' / 'main'
        for object_id in (blob, 'f' * len(blob)):
            with self.subTest(object_id=object_id):
                ref.write_text(f'{object_id}\n')
                self.assertEqual(review_scope.checklist(self.repo)['status'], 'unchecked')
                self.assertIsNone(review_scope.ui_paths(self.repo))

    def test_empty_base_checklist_and_invalid_utf8_are_unchecked(self):
        self.write('docs/reliability-checklist.md', '# Checklist\n')
        head = self.commit('An empty checklist')
        result = self.check(base=head)
        self.assertEqual((result['status'], result['reason']), ('unchecked', 'the checklist has no rows'))
        self.assertEqual(self.cli('--validate', '--base', head), (2, '', 'The checklist has no rows.\n'))

        file = self.repo / 'docs/reliability-checklist.md'
        file.write_bytes(ROWS.encode() + b'\xff')
        head = self.commit('Invalid UTF-8')
        for options in ({'base': head}, {'file': file}):
            with self.subTest(options=options):
                result = self.check(**options)
                self.assertEqual(result['status'], 'unchecked')
                self.assertIn('could not be read', result['reason'])

    def test_staged_text_is_scanned_when_the_working_copy_differs(self):
        # Review round 2, finding 2: the next commit takes the staged version.
        self.write('tools/stop.sh', 'tmux kill-server\n')
        run(self.repo, 'add', 'tools/stop.sh')
        self.write('tools/stop.sh', 'echo stop\n')
        self.assertEqual([row['id'] for row in self.check()['rows']], ['R1'])
        self.write('server/app.py', 'path = "/' + 'home' + '/user/x"\n')  # a tracked file: staged, then reverted
        run(self.repo, 'add', 'server/app.py')
        self.write('server/app.py', 'print(1)\n')
        self.assertEqual([row['id'] for row in self.check()['rows']], ['R1', 'R3'])
        run(self.repo, 'reset', '-q')
        self.assertEqual([row['id'] for row in self.check()['rows']], [])  # nothing staged, clean working copy

    def test_untracked_file_at_a_committed_path_does_not_replace_the_committed_lines(self):
        # Review round 5: the commit holds the trigger; the path was moved in the index and written again, untracked.
        self.write('tools/stop.sh', 'tmux kill-server\n')
        self.commit('Stop script')
        run(self.repo, 'mv', 'tools/stop.sh', 'tools/old.sh')
        self.write('tools/stop.sh', 'echo stop\n')
        # tools/old.sh holds the trigger too: the hit must still name the committed path, not only the moved copy.
        rows = {row['id']: row['files'] for row in self.check()['rows']}
        self.assertIn('tools/stop.sh', rows.get('R1', []), rows)
        run(self.repo, 'reset', '-q', '--hard')
        run(self.repo, 'rm', '-q', '--cached', 'tools/stop.sh')  # the same, with git rm --cached
        self.write('tools/stop.sh', 'echo stop\n')
        result = self.check()
        self.assertEqual(([row['id'] for row in result['rows']], result['rows'][0]['files']), (['R1'], ['tools/stop.sh']))

    def test_commit_under_review_is_scanned_when_a_staged_revert_hides_it(self):
        # Review round 4, finding 1: HEAD holds the change; the index and the working copy revert it.
        self.write('tools/stop.sh', 'tmux kill-server\n')
        self.commit('Stop script')
        self.write('tools/stop.sh', 'echo stop\n')
        run(self.repo, 'add', 'tools/stop.sh')
        self.assertEqual([row['id'] for row in self.check()['rows']], ['R1'])
        self.assertEqual(self.check()['paths'], ['tools/stop.sh'])
        run(self.repo, 'rm', '-q', '-f', 'tools/stop.sh')  # staged deletion: HEAD still holds the file
        self.assertEqual([row['id'] for row in self.check()['rows']], ['R1'])

    def test_ui_paths_are_independent_of_the_display_and_patch_limits(self):
        ui = 'ui/vite.config.ts'
        self.write(ui, 'export default {}\n')
        for index in range(601):
            self.write(f'notes/{index:03}.txt', 'new\n')
        run(self.repo, 'add', '.')
        run(self.repo, 'config', 'diff.relative', 'true')
        result = review_scope.scope(self.repo / 'server')
        self.assertEqual(result['checklist']['status'], 'clear')
        self.assertEqual(len(result['checklist']['paths']), 500)
        self.assertNotIn(ui, result['checklist']['paths'])
        self.assertEqual(result['ui'], [ui])

        self.write(ui, 'x' * checklist.MAX_DIFF)
        result = review_scope.scope(self.repo / 'notes')
        self.assertEqual(result['checklist']['status'], 'unchecked')
        self.assertIn('the diff is larger', result['checklist']['reason'])
        self.assertEqual(result['ui'], [ui])

    def test_a_base_without_a_checklist_is_absent_and_unreadable_input_is_unchecked(self):
        run(self.repo, 'checkout', '-q', 'main')
        run(self.repo, 'rm', '-q', 'docs/reliability-checklist.md')
        self.commit('No checklist')
        run(self.repo, 'push', '-q', 'origin', 'main')
        result = self.check()
        self.assertEqual((result['status'], result['reason']),
                         ('absent', 'origin/main has no docs/reliability-checklist.md'))
        for options, reason in (({'base': 'origin/missing'}, 'git merge-base failed'),
                                ({'head': 'no-such-rev'}, 'git merge-base failed'),
                                ({'file': self.root / 'missing.md'}, 'could not be read')):
            result = self.check(**options)
            self.assertEqual(result['status'], 'unchecked', options)
            self.assertIn(reason, result['reason'])
        self.assertEqual(checklist.check(self.root)['status'], 'unchecked')  # not a repository

    def test_invalid_rows_a_large_diff_and_a_slow_git_are_unchecked_never_clear(self):
        bad = self.root / 'bad.md'
        bad.write_text('## R1: Bad\n- Paths: `(`\n- Check: c\n- Incident: i\n')
        result = self.check(file=bad)
        self.assertEqual(result['status'], 'unchecked')
        self.assertIn('R1 paths is not a valid regular expression', result['reason'])
        self.write('server/app.py', 'x = 1\n' * 50)
        with patch.object(checklist, 'MAX_DIFF', 100):
            self.assertIn('larger than 100 bytes', self.check()['reason'])
        with patch.object(checklist, 'MAX_UNTRACKED', 0):
            self.write('new.txt', 'x')
            self.assertIn('more than 0 untracked files', self.check()['reason'])
        with patch.object(checklist.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 5)):
            self.assertEqual(self.check()['status'], 'unchecked')

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = checklist.main(['--repo', str(self.repo), *args])
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_command_line_exit_codes_and_output(self):
        self.assertEqual(self.cli(), (0, 'No checklist row hit.\n', ''))
        self.write('tools/stop.sh', 'tmux kill-server\n')
        code, out, _ = self.cli()
        self.assertEqual(code, 1)
        self.assertEqual(out, '1 checklist row(s) hit:\n- R1 Stop only a private tmux server\n'
                              '    Check: The command names a private socket.\n    Incident: commit 62ebf51\n'
                              '    Files: tools/stop.sh\n')
        code, out, _ = self.cli('--json')
        self.assertEqual((code, json.loads(out)['status'], json.loads(out)['rows'][0]['id']), (1, 'hit', 'R1'))
        code, out, err = self.cli('--base', 'origin/missing')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('Not checked: git merge-base failed', err)
        self.assertEqual(self.cli('--validate'), (0, '3 rows, all valid.\n', ''))
        bad = self.root / 'bad.md'
        bad.write_text('## R1: Bad\n- Check: c\n- Incident: i\n')
        code, _, err = self.cli('--validate', '--file', str(bad))
        self.assertEqual((code, err), (2, 'R1 has no Paths and no Text trigger\n'))

    def test_scripts_wrapper_runs_the_same_command(self):
        self.write('tools/stop.sh', 'tmux kill-server\n')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'checklist.py'), '--repo', str(self.repo), '--json'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([row['id'] for row in json.loads(result.stdout)['rows']], ['R1'])


if __name__ == '__main__':
    unittest.main()
