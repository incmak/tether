"""The Changes and Files side panels: repositories, per-file counts, diffs and the folder tree, on temporary Git repositories."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock

import prefs_sync
import repo_panel


def run(folder, *args):
    subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=Test', '-c', 'init.defaultBranch=main', '-C', str(folder), *args],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    run(path, 'init', '-q')
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    run(path, 'add', '-A')
    run(path, 'commit', '-qm', 'first')
    return path


class RepoPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def test_single_repo_lists_branch_status_and_counts(self):
        repo = make_repo(self.home / 'proj', {'a.txt': 'one\ntwo\n', 'gone.txt': 'x\n', 'src/b.py': 'print(1)\n'})
        (repo / 'a.txt').write_text('one\nTWO\nthree\n')
        (repo / 'gone.txt').unlink()
        (repo / 'new.md').write_text('hello\nworld\n')
        (repo / 'src/b.py').write_text('print(2)\n')
        run(repo, 'add', 'src/b.py')
        result = repo_panel.changes(str(repo / 'src'), base=self.home)
        self.assertEqual(result['mode'], 'repo')
        self.assertEqual(result['folder'], '~/proj/src')
        [found] = result['repos']
        self.assertEqual((found['id'], found['branch'], found['path']), ('.', 'main', '~/proj'))
        files = {item['path']: item for item in found['files']}
        self.assertEqual((files['a.txt']['status'], files['a.txt']['added'], files['a.txt']['deleted']), ('M', 2, 1))
        self.assertEqual((files['gone.txt']['status'], files['gone.txt']['deleted']), ('D', 1))
        self.assertEqual((files['new.md']['status'], files['new.md']['added']), ('?', 2))
        self.assertTrue(files['src/b.py']['staged'])
        text = repo_panel.diff(str(repo), '.', 'a.txt', base=self.home)
        self.assertIn('-two', text['diff'])
        self.assertIn('+three', text['diff'])
        added = repo_panel.diff(str(repo), '.', 'new.md', base=self.home)
        self.assertIn('+world', added['diff'])
        self.assertEqual(repo_panel.diff(str(repo), '.', 'src/b.py', base=self.home)['diff'].count('+print(2)'), 1)

    def test_binary_and_no_changes(self):
        repo = make_repo(self.home / 'proj', {'a.txt': 'x\n'})
        (repo / 'pic.bin').write_bytes(b'\0\1\2')
        run(repo, 'add', 'pic.bin')
        result = repo_panel.diff(str(repo), '.', 'pic.bin', base=self.home)
        self.assertTrue(result['binary'])
        self.assertEqual(result['diff'], '')
        self.assertEqual(repo_panel.diff(str(repo), '.', 'a.txt', base=self.home)['note'], 'This file has no changes.')

    def test_parent_folder_finds_repos_and_skips_ignored_folders(self):
        parent = self.home / 'work'
        make_repo(parent / 'alpha', {'a.txt': 'a\n'})
        make_repo(parent / 'group/beta', {'b.txt': 'b\n'})
        make_repo(parent / 'node_modules/dep', {'c.txt': 'c\n'})
        make_repo(parent / '.cache/hidden', {'d.txt': 'd\n'})
        make_repo(parent / 'x/y/z/too-deep', {'e.txt': 'e\n'})
        (parent / 'alpha/a.txt').write_text('A\n')
        result = repo_panel.changes(str(parent), base=self.home)
        self.assertEqual(result['mode'], 'parent')
        self.assertEqual([repo['id'] for repo in result['repos']], ['alpha', 'group/beta'])
        self.assertEqual(result['repos'][0]['files'][0]['path'], 'a.txt')
        self.assertEqual(result['repos'][1]['files'], [])
        self.assertIn('-a', repo_panel.diff(str(parent), 'alpha', 'a.txt', base=self.home)['diff'])

    def test_folder_with_no_repo_is_an_empty_parent(self):
        (self.home / 'plain').mkdir()
        result = repo_panel.changes(str(self.home / 'plain'), base=self.home)
        self.assertEqual((result['mode'], result['repos']), ('parent', []))
        with self.assertRaises(ValueError):
            repo_panel.diff(str(self.home / 'plain'), '.', 'a.txt', base=self.home)

    def test_paths_outside_the_folder_or_home_are_refused(self):
        parent = self.home / 'work'
        make_repo(parent / 'alpha', {'a.txt': 'a\n'})
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        for folder in (outside.name, str(self.home / 'missing'), str(self.home / '.ssh'), 'relative/path'):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                (self.home / '.ssh').mkdir(exist_ok=True)
                repo_panel.changes(folder, base=self.home)
        for repo, name in (('../work', 'alpha/a.txt'), ('alpha', '../alpha/a.txt'), ('alpha', '/etc/passwd'), ('alpha', '.git/config'), ('/tmp', 'a')):
            with self.subTest(repo=repo, name=name), self.assertRaises(ValueError):
                repo_panel.diff(str(parent), repo, name, base=self.home)
        (parent / 'link').symlink_to(outside.name)
        for sub in ('..', 'link', '../..'):
            with self.subTest(sub=sub), self.assertRaises(ValueError):
                repo_panel.listing(str(parent), sub, base=self.home)

    def test_secret_files_are_hidden_and_refused(self):
        repo = make_repo(self.home / 'proj', {'a.txt': 'a\n', 'src/keep.ts': 'x\n'})
        (repo / '.env').write_text('KEY=1\n')
        (repo / 'server.pem').write_text('x\n')
        (repo / '.ssh').mkdir()
        listing = repo_panel.listing(str(repo), '', base=self.home)
        self.assertEqual([(item['name'], item['dir']) for item in listing['entries']], [('src', True), ('a.txt', False)])
        self.assertEqual([item['name'] for item in repo_panel.listing(str(repo), 'src', base=self.home)['entries']], ['keep.ts'])
        with self.assertRaises(ValueError):
            repo_panel.diff(str(repo), '.', '.env', base=self.home)
        files = {item['path']: item for item in repo_panel.changes(str(repo), base=self.home)['repos'][0]['files']}
        self.assertIsNone(files['.env'].get('added'))  # a secret-looking file is listed but never read

    def test_repo_filter_commands_never_run(self):
        repo = make_repo(self.home / 'proj', {'a.txt': 'a\n'})
        marker = self.home / 'ran'
        (repo / '.gitattributes').write_text('*.txt filter=evil diff=evil\n')
        run(repo, 'config', 'filter.evil.clean', 'touch %s; cat' % marker)
        run(repo, 'config', 'diff.evil.textconv', 'touch %s; cat' % marker)
        run(repo, 'config', 'core.fsmonitor', 'touch %s; true' % marker)
        (repo / 'a.txt').write_text('b\n')
        repo_panel.changes(str(repo), base=self.home)
        repo_panel.diff(str(repo), '.', 'a.txt', base=self.home)
        self.assertFalse(marker.exists())

    def test_status_parser_handles_renames_and_spaces(self):
        out = (b'# branch.oid abc\0# branch.head feature/x\0# branch.upstream origin/feature/x\0# branch.ab +2 -1\0'
               b'1 .M N... 100644 100644 100644 a a dir/with space.txt\0'
               b'2 R. N... 100644 100644 100644 a a R100 new name.txt\0old name.txt\0? untracked file\0')
        branch, files = repo_panel.parse_status(out)
        self.assertEqual((branch['branch'], branch['ahead'], branch['behind']), ('feature/x', 2, 1))
        self.assertEqual(files['new name.txt']['from'], 'old name.txt')
        self.assertEqual(files['new name.txt']['status'], 'R')
        self.assertEqual(files['dir/with space.txt']['status'], 'M')
        self.assertEqual(files['untracked file']['status'], '?')
        counts = repo_panel.parse_numstat(b'1\t2\ta.txt\0-\t-\tb.bin\0' + b'3\t0\t\0old.txt\0new.txt\0')
        self.assertEqual(counts, {'a.txt': (1, 2), 'b.bin': None, 'new.txt': (3, 0)})


class RepoChoicePrefsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = unittest.mock.patch.object(prefs_sync, 'FILE', Path(self.temp.name) / 'prefs.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_repo_choice_merges_per_folder(self):
        prefs_sync.save({'repos': {'~/work': ['alpha']}})
        stored = prefs_sync.save({'repos': {'~/other': ['x', 'y']}})
        self.assertEqual(stored['prefs']['repos'], {'~/work': ['alpha'], '~/other': ['x', 'y']})
        stored = prefs_sync.save({'repos': {'~/work': []}})
        self.assertEqual(stored['prefs']['repos'], {'~/other': ['x', 'y']})
        for bad in ({'~/w': 'alpha'}, {'~/w': [1]}, {'~/w': ['']}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                prefs_sync.save({'repos': bad})


if __name__ == '__main__':
    unittest.main()
