"""Auto-merge decisions and the trust ladder, on disposable git repositories. Nothing here touches tmux or a real remote."""
import hashlib
import io
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import auto_merge
import paths

RULES = {'version': 1, 'areas': [
    {'name': 'tests', 'label': 'Tests', 'paths': ['server/test_*.py', 'server/fixtures/**', 'ui/test_*.cjs']},
    {'name': 'ui', 'label': 'UI', 'paths': ['ui/src/**', 'ui/index.html']}]}
STAMP = 'Verified: tests(server 12 ok) screenshots(2)'
GIT_SETTINGS = [('core.fsmonitor', 'false'), ('core.untrackedCache', 'false'), ('fetch.prune', 'false'),
                ('fetch.pruneTags', 'false'), ('fetch.recurseSubmodules', 'false'), ('push.recurseSubmodules', 'no'),
                ('push.followTags', 'false'), ('submodule.recurse', 'false'), ('core.attributesFile', os.devnull)]

CHECKLIST_ROWS = auto_merge.checklist_rows  # the real function: Base replaces it with a fixed answer


def git_settings(env):
    return [(env[f'GIT_CONFIG_KEY_{index}'], env[f'GIT_CONFIG_VALUE_{index}'])
            for index in range(int(env['GIT_CONFIG_COUNT']))]


def credential_lookup(root):
    return ['git', '-C', str(root), 'config', '--includes', '--null', '--show-scope', '--get-regexp', r'^credential\.']


def is_shadow_status(argv):
    return argv[0] == 'git' and argv[1].startswith('--git-dir=') and argv[3:4] == ['status']


def expire_stat_cache(path):
    found = path.stat()
    os.utime(path, ns=(found.st_atime_ns, found.st_mtime_ns - 2_000_000_000))


class Handler:
    def __init__(self, body=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = auto_merge.PATH
        self.headers = {'Content-Length': str(len(raw)), **(headers or {})}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class Base(unittest.TestCase):
    """origin.git (bare), project (the main checkout, on main) and project-wt/work (a worker worktree on `work`)."""

    enforce_available = True

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT='0',
                   LC_ALL='C', GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            env.pop(key, None)
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        for name, value in (('CONFIG', self.root / 'config'), ('CACHE', self.root / 'cache')):
            self.enterContext(mock.patch.object(paths, name, value))
        self.enterContext(mock.patch.object(auto_merge, '_grant', None))
        if self.enforce_available:
            self.enterContext(mock.patch.object(auto_merge, 'ENFORCE_AVAILABLE', True))
        auto_merge._ladder_cache.clear()
        auto_merge._target_cache.clear()
        auto_merge._target_errors.clear()
        auto_merge._seen.clear()
        self.rows = []
        self.enterContext(mock.patch.object(auto_merge, 'checklist_rows', lambda *args: self.rows))
        self.origin = self.root / 'origin.git'
        self.repo = self.root / 'project'
        self.work = self.root / 'project-wt' / 'work'
        self.git(self.root, 'init', '--bare', '-b', 'main', str(self.origin))
        self.git(self.root, 'init', '-b', 'main', str(self.repo))
        self.git(self.repo, 'remote', 'add', 'origin', str(self.origin))
        self.write(self.repo, 'docs/auto-merge.json', json.dumps(RULES))
        self.write(self.repo, 'ui/src/App.tsx', 'export const app = 1\n')
        self.write(self.repo, 'server/server.py', 'print(1)\n')
        self.commit(self.repo, 'First')
        self.git(self.repo, 'push', '-u', 'origin', 'main')
        self.git(self.repo, 'worktree', 'add', '-b', 'work', str(self.work), 'main')

    def git(self, folder, *args):
        return subprocess.run(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, check=True, timeout=20).stdout.strip()

    def write(self, folder, name, text):
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, folder, subject, stamp=STAMP):
        self.git(folder, 'add', '-A')
        self.git(folder, 'commit', '-m', subject + ('\n\n' + stamp if stamp else ''))
        return self.git(folder, 'rev-parse', 'HEAD')

    def change(self, name='ui/src/App.tsx', text='export const app = 2\n', stamp=STAMP):
        self.write(self.work, name, text)
        return self.commit(self.work, f'Change {name}', stamp)

    def failed(self, decision):
        return [item['key'] for item in decision['checks'] if not item['ok']]

    def job(self, head, at='now'):
        at = int(auto_merge.time.time()) + 5 if at == 'now' else at
        return {'folder': str(self.work), 'commit': head, 'approved_at': at,
                'worker': '%1', 'name': 'Worker', 'reviewer': '%2', 'reviewer_agent': 'codex',
                'current': lambda: {'%1': {'%2': {'commit': head, 'at': at}}}}

    def origin_main(self):
        return self.git(self.origin, 'rev-parse', 'main')

    def refs(self):
        return self.git(self.repo, 'for-each-ref')

    def enforce(self):
        auto_merge.set_mode('enforce')


class Decision(Base):
    def test_ui_only_approved_commit_passes_every_check(self):
        head = self.change()
        decision, plan = auto_merge.evaluate(self.work, head)
        self.assertEqual(self.failed(decision), [])
        self.assertEqual(decision['outcome'], 'pass')
        self.assertEqual(decision['areas'], {'ui': 2})
        self.assertEqual(plan['head'], head)

    def test_tests_only_diff_passes_too(self):
        head = self.change('server/test_thing.py', 'import unittest\n')
        decision, plan = auto_merge.evaluate(self.work, head)
        self.assertEqual(self.failed(decision), [])
        self.assertEqual(decision['areas'], {'tests': 1})
        self.assertIsNotNone(plan)

    def test_clean_check_compares_the_index_and_raw_worktree_files(self):
        self.write(self.work, '.gitignore', 'ignored*\n')
        os.symlink('App.tsx', self.work / 'ui/src/link')
        head = self.change()
        scratch = auto_merge._scratch(self.repo)
        app = self.work / 'ui/src/App.tsx'
        link = app.with_name('link')
        self.assertFalse(auto_merge._dirty(self.work))
        for name in ('ignored.txt', 'ignored-dir/file'):
            self.write(self.work, name, 'ignored\n')
            self.assertFalse(auto_merge._dirty(self.work))
        cases = (
            ('staged', lambda: (self.write(self.work, 'ui/src/App.tsx', 'staged\n'),
                                self.git(self.work, 'add', 'ui/src/App.tsx'))),
            ('staged deletion', lambda: self.git(self.work, 'rm', '-q', 'ui/src/App.tsx')),
            ('modified', lambda: app.write_text('modified\n')),
            ('modified link', lambda: (link.unlink(), link.symlink_to('Other.tsx'))),
            ('mode flip', lambda: app.chmod(0o755)),
            ('second mode flip', lambda: (self.work / 'server/server.py').chmod(0o755)),
            ('deleted', app.unlink),
            ('deleted link', link.unlink),
            ('untracked', lambda: self.write(self.work, 'untracked.txt', 'new\n')),
            ('untracked in another folder', lambda: self.write(self.work, 'server/untracked.txt', 'new\n')),
            ('special file', lambda: (app.unlink(), os.mkfifo(app))),
            ('regular file replaced by link', lambda: (app.unlink(), app.symlink_to('link'))),
        )
        for label, change in cases:
            with self.subTest(label=label):
                change()
                self.assertTrue(auto_merge._dirty(self.work))
                decision, plan = auto_merge.evaluate(app.parent, head)
                self.assertIn('branch', self.failed(decision))
                self.assertIsNone(plan)
                self.git(self.work, 'reset', '--hard', head)
                self.git(self.work, 'clean', '-fd')
        self.assertFalse(auto_merge._dirty(self.work))

    def test_unchanged_submodule_and_crlf_checkout_are_clean(self):
        # A submodule that main already has, checked out and unchanged.
        library = self.root / 'library'
        self.git(self.root, 'init', '-q', '-b', 'main', str(library))
        self.write(library, 'lib.txt', 'library\n')
        self.commit(library, 'Library')
        self.git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(library), 'vendor/library')
        self.write(self.repo, '.gitattributes', '*.txt text eol=crlf\n')
        self.write(self.repo, 'ui/src/notes.txt', 'one\ntwo\n')
        self.commit(self.repo, 'Submodule and CRLF file')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '-q', '--ff-only', 'main')
        self.git(self.work, '-c', 'protocol.file.allow=always', 'submodule', 'update', '-q', '--init')
        self.assertEqual((self.work / 'ui/src/notes.txt').read_bytes(), b'one\r\ntwo\r\n')
        self.assertEqual(self.git(self.work, 'status', '--porcelain'), '')
        head = self.change()
        decision, plan = auto_merge.evaluate(self.work, head)
        self.assertEqual(self.failed(decision), [])
        self.assertIsNotNone(plan)
        # A real edit of the CRLF file, and a second kind: a lone CR change, are still uncommitted work.
        for data in (b'one\r\nthree\r\n', b'one\rtwo\r\n'):
            (self.work / 'ui/src/notes.txt').write_bytes(data)
            decision = auto_merge.evaluate(self.work, head)[0]
            self.assertEqual(self.failed(decision), ['branch'])
            self.assertEqual(decision['checks'][1]['text'], auto_merge.DIRTY)
        (self.work / 'ui/src/notes.txt').write_bytes(b'one\r\ntwo\r\n')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
        # Work inside the checked-out submodule is uncommitted work: untracked, edited, or another commit.
        inside = self.work / 'vendor/library'
        for label, change, undo in (
                ('untracked', lambda: self.write(inside, 'new.txt', 'new\n'), lambda: (inside / 'new.txt').unlink()),
                ('edited', lambda: self.write(inside, 'lib.txt', 'edited\n'), lambda: self.git(inside, 'checkout', '-q', '.')),
                ('other commit', lambda: (self.write(inside, 'lib.txt', 'next\n'), self.commit(inside, 'Next')),
                 lambda: self.git(inside, 'reset', '-q', '--hard', 'HEAD~1'))):
            with self.subTest(label=label):
                change()
                self.assertNotEqual(self.git(self.work, 'status', '--porcelain'), '')
                decision = auto_merge.evaluate(self.work, head)[0]
                self.assertEqual(self.failed(decision), ['branch'])
                self.assertEqual(decision['checks'][1]['text'], auto_merge.DIRTY)
                undo()
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])

    def test_crlf_bytes_are_an_edit_when_git_does_not_convert_the_file(self):
        self.write(self.repo, 'ui/src/plain.css', 'a {}\nb {}\n')
        self.write(self.repo, 'ui/src/raw.dat', 'a\nb\n')
        self.write(self.repo, '.gitattributes', '*.dat -text\n')
        self.commit(self.repo, 'Plain files')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '-q', '--ff-only', 'main')
        head = self.change()
        for name, config in (('ui/src/plain.css', 'false'), ('ui/src/plain.css', None), ('ui/src/raw.dat', 'true')):
            with self.subTest(name=name, autocrlf=config):
                if config:
                    self.git(self.repo, 'config', 'core.autocrlf', config)
                else:
                    self.git(self.repo, 'config', '--unset', 'core.autocrlf')
                self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
                old = (self.work / name).read_bytes()
                (self.work / name).write_bytes(old.replace(b'\n', b'\r\n'))
                self.assertNotEqual(self.git(self.work, 'status', '--porcelain'), '')
                decision = auto_merge.evaluate(self.work, head)[0]
                self.assertEqual(self.failed(decision), ['branch'])
                self.assertEqual(decision['checks'][1]['text'], auto_merge.DIRTY)
                (self.work / name).write_bytes(old)
        # With core.autocrlf in the repository's own config, a CRLF checkout that Git made is clean.
        self.git(self.repo, 'config', 'core.autocrlf', 'true')
        (self.work / 'ui/src/plain.css').unlink()
        self.git(self.work, 'checkout', '--', 'ui/src/plain.css')
        self.assertEqual((self.work / 'ui/src/plain.css').read_bytes(), b'a {}\r\nb {}\r\n')
        self.assertEqual(self.git(self.work, 'status', '--porcelain'), '')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])

    def test_a_filter_that_tether_does_not_run_is_unsupported_not_dirty(self):
        marker = self.root / 'filter-ran'
        for attribute, config in (('filter=evil', ('filter.evil.clean', f'touch {marker}; cat')), ('filter=lfs', None)):
            with self.subTest(attribute=attribute):
                self.git(self.work, 'reset', '-q', '--hard', 'origin/main')
                self.write(self.work, '.gitattributes', f'*.dat {attribute}\n')
                self.write(self.work, 'ui/src/blob.dat', 'stored\n')
                head = self.commit(self.work, f'Add {attribute}')
                if config:
                    self.git(self.repo, 'config', *config)
                # Unchanged or changed, Tether does not ask Git about a tree that needs the filter.
                for data in (b'stored\n', b'what the smudge filter wrote\n'):
                    (self.work / 'ui/src/blob.dat').write_bytes(data)
                    decision = auto_merge.evaluate(self.work, head)[0]
                    self.assertIn('branch', self.failed(decision))
                    self.assertEqual(decision['checks'][1]['text'], auto_merge.UNSUPPORTED)
                self.assertFalse(marker.exists())
        # An ident attribute needs no program: Git answers, and an edit is an edit.
        self.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        self.write(self.work, '.gitattributes', '*.dat ident\n')
        self.write(self.work, 'ui/src/blob.dat', 'stored\n')
        head = self.commit(self.work, 'Add ident')
        self.assertNotIn('branch', self.failed(auto_merge.evaluate(self.work, head)[0]))
        self.write(self.work, 'ui/src/blob.dat', 'edited\n')
        self.assertEqual(auto_merge.evaluate(self.work, head)[0]['checks'][1]['text'], auto_merge.DIRTY)

    def test_a_filter_attribute_that_appears_before_git_reads_it_starts_no_program(self):
        """The attributes change after Tether's own check and before `git status` reads them."""
        marker = self.root / 'late-filter-ran'
        head = self.change()
        app = self.work / 'ui/src/App.tsx'
        real = subprocess.run

        cases = [(kind, target, late_driver) for late_driver in (False, True) for kind in ('clean', 'process')
                 for target in (self.work / '.gitattributes', self.repo / '.git/info/attributes')]
        for kind, target, late_driver in cases:
            with self.subTest(kind=kind, target=target.name, late_driver=late_driver):
                driver = 'late2' if late_driver else 'late'
                key = f'filter.{driver}.{kind}'
                if not late_driver:
                    self.git(self.repo, 'config', key, f'touch "{marker}"; cat')

                def run(argv, **kwargs):
                    if is_shadow_status(argv):
                        if late_driver:
                            self.git(self.repo, 'config', key, f'touch "{marker}"; cat')
                        target.parent.mkdir(exist_ok=True)
                        target.write_text(f'* filter={driver}\n')
                    return real(argv, **kwargs)

                try:
                    expire_stat_cache(app)
                    with mock.patch.object(auto_merge, 'RUN', run):
                        result = auto_merge._dirty(self.work)
                    self.assertFalse(marker.exists())
                    self.assertEqual(result, auto_merge.UNSUPPORTED)  # Git's raw comparison does not count
                    # Control: the same change with a plain `git status` does start the program.
                    expire_stat_cache(app)
                    subprocess.run(['git', '-C', str(self.work), 'status', '--porcelain'], capture_output=True, timeout=20)
                    self.assertTrue(marker.exists())
                finally:
                    marker.unlink(missing_ok=True)
                    target.unlink(missing_ok=True)
                    self.git(self.repo, 'config', '--unset', key)
                    self.git(self.work, 'update-index', '-q', '--refresh')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])

    def test_a_late_submodule_filter_attribute_starts_no_program(self):
        library = self.root / 'library'
        self.git(self.root, 'init', '-q', '-b', 'main', str(library))
        self.write(library, 'lib.txt', 'library\n')
        self.commit(library, 'Library')
        self.git(self.work, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(library), 'vendor/lib')
        self.commit(self.work, 'Submodule')
        inside = self.work / 'vendor/lib'
        marker = self.root / 'submodule-filter-ran'
        self.git(inside, 'config', 'filter.x.clean', f'touch "{marker}"; cat')
        worktrees = []

        def run(argv, **kwargs):
            if is_shadow_status(argv):
                worktrees.append(argv[2])
                if argv[2] == f'--work-tree={self.work}':
                    self.write(inside, '.gitattributes', 'lib.txt filter=x\n')
                    expire_stat_cache(inside / 'lib.txt')
            return subprocess.run(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            self.assertEqual(auto_merge._dirty(self.work), auto_merge.UNSUPPORTED)
        self.assertEqual(worktrees, [f'--work-tree={self.work}', f'--work-tree={inside}'])
        self.assertFalse(marker.exists())
        self.assertNotEqual(self.git(self.work, 'status', '--porcelain', '--ignore-submodules=none'), '')
        self.assertTrue(marker.exists())

    def test_a_late_fsmonitor_setting_starts_no_program(self):
        marker = self.root / 'late-fsmonitor-ran'
        hook = self.root / 'late-fsmonitor'
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        hook.chmod(0o755)

        def run(argv, **kwargs):
            if is_shadow_status(argv):
                self.git(self.repo, 'config', 'core.fsmonitor', str(hook))
            return subprocess.run(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            self.assertEqual(auto_merge._dirty(self.work), '')
        self.assertFalse(marker.exists())
        self.assertEqual(self.git(self.work, 'status', '--porcelain'), '')
        self.assertTrue(marker.exists())

    def test_an_index_that_changes_during_the_check_is_never_clean(self):
        """The status reads a copy of the index. The tree is dirty before and after, in a way the copy cannot see."""
        head = self.change()
        app = self.work / 'ui/src/App.tsx'
        committed = app.read_bytes()
        real = subprocess.run

        def stage_then_restore():   # the reviewer's case: an edit is staged, then the file gets its HEAD bytes back
            self.git(self.work, 'add', 'ui/src/App.tsx')
            app.write_bytes(committed)

        def add_then_delete():      # the same kind: a new file is staged, then deleted from the worktree
            self.git(self.work, 'add', 'ui/src/New.tsx')
            (self.work / 'ui/src/New.tsx').unlink()

        cases = ((lambda: app.write_bytes(b'export const app = "edited"\n'), stage_then_restore),
                 (lambda: self.write(self.work, 'ui/src/New.tsx', 'export const fresh = 1\n'), add_then_delete))
        for prepare, during in cases:
            with self.subTest(during=during.__name__):
                prepare()
                self.assertNotEqual(self.git(self.work, 'status', '--porcelain'), '')
                done = []

                def run(argv, **kwargs):
                    if argv[0] == 'git' and 'status' in argv and not done:
                        done.append(1)
                        during()
                    return real(argv, **kwargs)

                with mock.patch.object(auto_merge, 'RUN', run):
                    result = auto_merge._dirty(self.work)
                self.assertEqual(done, [1])
                self.assertNotEqual(self.git(self.work, 'status', '--porcelain'), '')
                self.assertEqual(result, auto_merge.DIRTY)
                decision, plan = auto_merge.evaluate(self.work, head)
                self.assertIn('branch', self.failed(decision))
                self.assertIsNone(plan)
                self.git(self.work, 'reset', '-q', '--hard', head)
                self.git(self.work, 'clean', '-fdq')
        # A quiet tree is clean, and so is one whose index file is rewritten with the same content during the check.
        self.assertEqual(auto_merge._dirty(self.work), '')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
        # An index that changes and changes back still counts as changed: the answer is "not clean", never clean.
        done = []

        def run(argv, **kwargs):
            if argv[0] == 'git' and 'status' in argv and not done:
                done.append(1)
                app.write_bytes(b'export const app = "blip"\n')
                self.git(self.work, 'add', 'ui/src/App.tsx')
                self.git(self.work, 'reset', '-q', '--hard', head)
            return real(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            self.assertEqual(auto_merge._dirty(self.work), auto_merge.DIRTY)
        self.assertEqual(auto_merge._dirty(self.work), '')

    def test_the_clean_check_is_gits_own_answer(self):
        """The reviewer's cases: Tether must agree with `git status`, whatever the line-end rules are."""
        def agrees(head, expect_clean):
            listed = self.git(self.work, 'status', '--porcelain', '--untracked-files=all')
            self.assertEqual(listed == '', expect_clean, listed)
            failed = self.failed(auto_merge.evaluate(self.work, head)[0])
            self.assertEqual('branch' not in failed, expect_clean, failed)

        # 1. A real edit to a binary file with no NUL byte, under text=auto and under core.autocrlf=true.
        binary = bytes(range(1, 9)) * 40 + b'\n' + bytes(range(14, 32)) * 20 + b'\n'
        (self.repo / 'ui/src/data.bin').write_bytes(binary)
        self.commit(self.repo, 'Binary without NUL')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '-q', '--ff-only', 'main')
        head = self.change()
        attributes = self.repo / '.git/info/attributes'
        for rule in ('attribute', 'config'):
            with self.subTest(rule=rule):
                if rule == 'attribute':
                    attributes.write_text('*.bin text=auto\n')
                else:
                    attributes.unlink()
                    self.git(self.repo, 'config', 'core.autocrlf', 'true')
                agrees(head, True)
                (self.work / 'ui/src/data.bin').write_bytes(binary.replace(b'\n', b'\r\n'))
                agrees(head, False)
                (self.work / 'ui/src/data.bin').write_bytes(binary)
                self.git(self.work, 'update-index', '-q', '--refresh')
        self.git(self.repo, 'config', '--unset', 'core.autocrlf')

        # 2. A clean checkout whose CRLF line ends come from the user's global core.autocrlf.
        global_config = self.root / 'global-autocrlf.conf'
        global_config.write_text('[core]\n\tautocrlf = true\n')
        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config)):
            (self.work / 'ui/src/App.tsx').unlink()
            self.git(self.work, 'checkout', '--', 'ui/src/App.tsx')
            self.assertIn(b'\r\n', (self.work / 'ui/src/App.tsx').read_bytes())
            agrees(head, True)
            self.write(self.work, 'ui/src/App.tsx', 'export const app = "edited"\r\n')
            agrees(head, False)
            self.git(self.work, 'checkout', '--', 'ui/src/App.tsx')
            agrees(head, True)
        self.git(self.work, 'reset', '-q', '--hard', head)

        # 3. A checked-out submodule whose folder was moved out of the worktree.
        library = self.root / 'library'
        self.git(self.root, 'init', '-q', '-b', 'main', str(library))
        self.write(library, 'lib.txt', 'library\n')
        self.commit(library, 'Library')
        self.git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(library), 'vendor/lib')
        self.commit(self.repo, 'Submodule')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        head = self.change()
        agrees(head, True)   # not checked out: Git lists nothing
        self.git(self.work, '-c', 'protocol.file.allow=always', 'submodule', 'update', '-q', '--init')
        agrees(head, True)
        (self.work / 'vendor/lib').rename(self.root / 'moved-out')
        agrees(head, False)
        (self.root / 'moved-out').rename(self.work / 'vendor/lib')
        agrees(head, True)

    def test_global_excludes_and_split_indexes_in_main_and_linked_worktrees(self):
        excludes = self.root / 'ignore "this"'
        excludes.write_text('*.ignored\n')
        config = self.root / 'global-excludes.conf'
        self.git(self.repo, 'config', '--file', str(config), 'core.excludesFile', str(excludes))
        for folder in (self.repo, self.work):
            with self.subTest(folder=folder):
                self.git(folder, 'update-index', '--split-index')
                self.assertTrue(self.git(folder, 'rev-parse', '--shared-index-path'))
                self.write(folder, 'extra.ignored', 'extra\n')
                with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(config)):
                    self.assertEqual(self.git(folder, 'status', '--porcelain', '--untracked-files=all'), '')
                    self.assertEqual(auto_merge._dirty(folder), '')
                    tracked = folder / 'ui/src/App.tsx'
                    tracked.write_text('edited\n')
                    self.assertNotEqual(self.git(folder, 'status', '--porcelain', '--untracked-files=all'), '')
                    self.assertEqual(auto_merge._dirty(folder), auto_merge.DIRTY)
                    self.git(folder, 'checkout', '--', 'ui/src/App.tsx')
                self.assertEqual(self.git(folder, 'status', '--porcelain', '--untracked-files=all'), '?? extra.ignored')
                self.assertEqual(auto_merge._dirty(folder), auto_merge.DIRTY)

    def test_shadow_uses_the_effective_attributes_file(self):
        attributes = self.root / 'attributes'
        attributes.write_text('*.tsx text eol=crlf\n')
        config = self.root / 'global-attributes.conf'
        self.git(self.repo, 'config', '--file', str(config), 'core.attributesFile', os.devnull)
        self.git(self.repo, 'config', 'core.attributesFile', str(attributes))
        app = self.work / 'ui/src/App.tsx'
        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(config)):
            app.unlink()
            self.git(self.work, 'checkout', '--', 'ui/src/App.tsx')
            self.assertIn(b'\r\n', app.read_bytes())
            expire_stat_cache(app)
            self.assertEqual(self.git(self.work, 'status', '--porcelain'), '')
            self.assertEqual(auto_merge._dirty(self.work), '')
            self.git(self.repo, 'config', '--unset', 'core.attributesFile')
            expire_stat_cache(app)
            self.assertNotEqual(self.git(self.work, 'status', '--porcelain'), '')
            self.assertEqual(auto_merge._dirty(self.work), auto_merge.DIRTY)

    def test_a_repository_extension_the_shadow_cannot_mirror_is_unsupported(self):
        sha256 = self.root / 'sha256'
        self.git(self.root, 'init', '-q', '--object-format=sha256', str(sha256))
        self.write(sha256, 'file', 'content\n')
        self.commit(sha256, 'SHA-256')
        self.git(self.repo, 'config', 'core.repositoryformatversion', '1')
        self.git(self.repo, 'config', 'extensions.preciousObjects', 'true')
        for folder in (sha256, self.work):
            with self.subTest(folder=folder):
                self.assertEqual(self.git(folder, 'status', '--porcelain'), '')
                self.assertEqual(auto_merge._dirty(folder), auto_merge.UNSUPPORTED)

    def test_shadow_is_private_and_removed_on_a_status_error(self):
        cache = paths.CACHE / 'auto-merge-status'

        def run(argv, **kwargs):
            if is_shadow_status(argv):
                shadow = Path(argv[1].removeprefix('--git-dir='))
                self.assertEqual(stat.S_IMODE(shadow.stat().st_mode), 0o700)
                return subprocess.CompletedProcess(argv, 0, b'', b'\n')
            return subprocess.run(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            with self.assertRaisesRegex(auto_merge.GitError, 'git status printed a warning'):
                auto_merge._dirty(self.work)
        self.assertEqual(list(cache.iterdir()), [])
        index = Path(self.git(self.work, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        index.rename(self.root / 'saved-index')
        with self.assertRaisesRegex(auto_merge.GitError, 'no Git index'):
            auto_merge._dirty(self.work)
        self.assertEqual(list(cache.iterdir()), [])

    def test_nested_worker_folder_uses_the_whole_worktree(self):
        for name in ('ui/src/App.tsx', 'ui/index.html'):
            with self.subTest(name=name):
                head = self.change(name)
                decision, plan = auto_merge.evaluate(self.work / 'server', head)
                self.assertEqual(self.failed(decision), [])
                self.assertIsNotNone(plan)

    def test_mixed_areas_wait(self):
        for ui, tests in (('ui/src/App.tsx', 'server/test_new.py'), ('ui/index.html', 'ui/test_new.cjs')):
            self.git(self.work, 'reset', '-q', '--hard', 'main')
            self.write(self.work, ui, 'worker\n')
            decision, plan = auto_merge.evaluate(self.work, self.change(tests, 'test\n'))
            self.assertEqual(self.failed(decision), ['scope'])
            self.assertIn('The diff touches more than one area: Tests, UI. A branch may change one area.',
                          decision['checks'][-3]['text'])
            self.assertIsNone(plan)

    def test_no_approval_or_an_approval_of_another_commit_waits(self):
        first = self.change()
        second = self.change(text='export const app = 3\n')
        for approved in (None, '', 'abc', first):
            decision, plan = auto_merge.evaluate(self.work, approved)
            self.assertEqual(self.failed(decision), ['approval'], approved)
            self.assertIsNone(plan)
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, second)[0]), [])

    def test_stamp_needs_a_tests_item(self):
        for stamp in ('', 'Verified: none (docs only)', 'Verified: screenshots(2) review(codex APPROVED)', 'Verified: tests'):
            head = self.change(text=f'export const app = {stamp!r}\n', stamp=stamp)
            self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['stamp'], stamp)

    def test_a_file_outside_the_areas_waits(self):
        for name in ('server/server.py', 'ui/package.json', 'AGENTS.md', 'docs/auto-merge.json', 'ui/srcx/a.ts',
                     'server/test_dir/x.py', 'scripts/checklist.py'):
            self.write(self.work, 'ui/src/App.tsx', f'// {name}\n')
            head = self.change(name, '{"changed": true}\n')
            decision = auto_merge.evaluate(self.work, head)[0]
            self.assertEqual(self.failed(decision), ['scope'], name)
            self.assertIn(name, decision['checks'][-3]['text'])
            self.git(self.work, 'reset', '-q', '--hard', 'main')

    def test_branch_files_already_on_main_still_count_for_scope(self):
        for path, text in (('server/server.py', 'print(2)\n'), ('ui/package.json', '{}\n')):
            with self.subTest(path=path):
                self.git(self.work, 'reset', '-q', '--hard', 'main')
                self.write(self.work, path, text)
                head = self.change(text=f'// worker {path}\n')
                self.write(self.repo, path, text)
                self.commit(self.repo, f'Main changes {path} too')
                self.git(self.repo, 'push', 'origin', 'main')
                decision, plan = auto_merge.evaluate(self.work, head)
                self.assertEqual(self.failed(decision), ['scope'])
                self.assertIn(path, decision['checks'][-3]['text'])
                self.assertIsNone(plan)

    def test_lines_already_on_main_still_count_for_the_branch_limit(self):
        for path in ('ui/src/New.tsx', 'server/test_new.py'):
            with self.subTest(path=path):
                self.git(self.work, 'reset', '-q', '--hard', 'main')
                text = 'large change\n' * 120
                self.write(self.work, path, text)
                marker = 'ui/src/Small.tsx' if path.startswith('ui/') else 'server/test_small.py'
                head = self.change(marker, 'small change\n')
                self.write(self.repo, path, text)
                self.commit(self.repo, f'Main adds {path} too')
                self.git(self.repo, 'push', 'origin', 'main')
                decision, plan = auto_merge.evaluate(self.work, head)
                self.assertEqual(self.failed(decision), ['scope'])
                self.assertIn('121 changed lines', decision['checks'][-3]['text'])
                self.assertIsNone(plan)

    def test_scope_fails_if_either_diff_cannot_be_read(self):
        head = self.change()
        tree = auto_merge._trial_merge(auto_merge._scratch(self.repo), self.origin_main(), head)
        real = auto_merge.changed
        for unreadable, failed_checks in ((head, ['scope']), (tree, ['scope', 'merge'])):
            with self.subTest(failed_checks=failed_checks):
                def changed(folder, base, revision):
                    if revision == unreadable:
                        raise auto_merge.GitError('Could not read the diff.')
                    return real(folder, base, revision)

                with mock.patch.object(auto_merge, 'changed', side_effect=changed):
                    decision, plan = auto_merge.evaluate(self.work, head)
                self.assertEqual(self.failed(decision), failed_checks)
                self.assertIsNone(plan)

    def test_rules_come_from_the_main_branch_not_from_the_worker_branch(self):
        wide = {'version': 1, 'areas': [{'name': 'all', 'label': 'All', 'paths': ['s**', 'd**', 'u**']}]}
        self.write(self.work, 'docs/auto-merge.json', json.dumps(wide))
        head = self.change('server/server.py', 'print(2)\n')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['scope'])

    def test_origin_head_does_not_choose_the_rules_or_the_push_branch(self):
        self.git(self.repo, 'switch', '-c', 'integration')
        self.write(self.repo, 'docs/auto-merge.json', json.dumps({'version': 1, 'areas': [
            {'name': 'all', 'label': 'All', 'paths': ['s**', 'u**']}]}))
        integration = self.commit(self.repo, 'Permissive rules')
        self.git(self.repo, 'push', 'origin', 'integration')
        self.git(self.repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/integration')
        self.enforce()
        head = self.change('server/server.py', 'print(2)\n')
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['scope'])
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        result = auto_merge.process(self.job(self.change()))
        self.assertEqual(result['outcome'], 'merged')
        self.assertEqual(self.origin_main(), result['merge'])
        self.assertEqual(self.git(self.origin, 'rev-parse', 'integration'), integration)

    def test_the_push_repository_supplies_main_and_its_rules(self):
        fetch_origin = self.root / 'fetch.git'
        self.git(self.root, 'init', '--bare', '-b', 'main', str(fetch_origin))
        self.git(self.repo, 'remote', 'set-url', 'origin', str(fetch_origin))
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.origin))
        self.write(self.repo, 'docs/auto-merge.json', json.dumps({'version': 1, 'areas': [
            {'name': 'all', 'label': 'All', 'paths': ['s**', 'u**']}]}))
        fetch_main = self.commit(self.repo, 'Permissive rules')
        self.git(self.repo, 'push', str(fetch_origin), 'main')
        self.git(self.repo, 'fetch', 'origin')
        self.enforce()
        before = self.origin_main()
        head = self.change('server/server.py', 'print(2)\n')
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['scope'])
        with mock.patch('sys.stdout', new_callable=io.StringIO) as cli:
            self.assertEqual(auto_merge.main(['check', '--cwd', str(self.work), '--approved', head]), 3)
        self.assertIn('WAIT  scope:', cli.getvalue())
        self.assertEqual(auto_merge.state([str(self.repo)])['trust'][0]['areas'][1]['name'], 'ui')
        self.assertEqual(self.origin_main(), before)
        self.git(self.work, 'reset', '-q', '--hard', before)
        result = auto_merge.process(self.job(self.change()))
        self.assertEqual(result['outcome'], 'merged')
        self.assertEqual(self.origin_main(), result['merge'])
        self.assertEqual(self.git(fetch_origin, 'rev-parse', 'main'), fetch_main)

    def test_the_push_repository_must_have_main(self):
        head = self.change()
        self.git(self.origin, 'update-ref', 'refs/heads/integration', self.origin_main())
        self.git(self.origin, 'symbolic-ref', 'HEAD', 'refs/heads/integration')
        self.git(self.origin, 'update-ref', '-d', 'refs/heads/main')
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['git'])

    def test_a_push_url_cannot_resolve_as_a_remote_name_or_be_rewritten_again(self):
        self.git(self.repo, 'remote', 'add', 'other', str(self.origin))
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', 'other')
        head = self.change()
        refs = self.refs()
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['git'])
        self.assertEqual(self.refs(), refs)
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.origin))
        for option in ('pushInsteadOf', 'insteadOf'):
            key = f'url.{self.root / "other.git"}.{option}'
            self.git(self.repo, 'config', key, str(self.origin))
            self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['git'])
            self.assertEqual(self.refs(), refs)
            self.git(self.repo, 'config', '--unset', key)

    def test_origin_must_supply_one_literal_push_url(self):
        head = self.change()
        self.git(self.repo, 'config', '--add', 'remote.origin.url', str(self.origin))
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['git'])
        self.git(self.repo, 'config', '--add', 'remote.origin.pushurl', str(self.origin))
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
        self.git(self.repo, 'config', '--add', 'remote.origin.pushurl', str(self.origin))
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['git'])

    def test_relative_push_urls_use_the_same_root_for_settings_and_push(self):
        self.enforce()
        for count, url in enumerate(('../origin.git', './../origin.git'), 1):
            with self.subTest(url=url):
                self.git(self.repo, 'remote', 'set-url', '--push', 'origin', url)
                result = auto_merge.process(self.job(self.change(text=f'export const app = {count + 1}\n')))
                self.assertEqual(result['outcome'], 'merged', result)
                self.assertEqual(self.origin_main(), result['merge'])
                auto_merge._target_cache.clear()
                self.assertFalse(auto_merge.state([str(self.repo)])['trust'][0]['error'])

    def test_no_rules_file_on_main_means_nothing_merges(self):
        self.git(self.repo, 'rm', '-q', 'docs/auto-merge.json')
        self.commit(self.repo, 'Drop rules')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '-q', '--ff-only', 'main')
        head = self.change()
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['rules', 'scope'])

    def test_bad_rules_files_are_refused(self):
        for bad in ('[]', '{"version": 2, "areas": []}', '{"version": 1, "areas": []}',
                    '{"version": 1, "areas": [{"name": "UI", "label": "x", "paths": ["a"]}]}',
                    '{"version": 1, "areas": [{"name": "ui", "label": "x", "paths": ["**"]}]}',
                    '{"version": 1, "areas": [{"name": "ui", "label": "x", "paths": ["/abs"]}]}',
                    '{"version": 1, "areas": [{"name": "ui", "label": "x", "paths": ["a/../b"]}]}',
                    '{"version": 1, "areas": [{"name": "ui", "label": "x", "paths": []}]}'):
            with self.assertRaises(ValueError, msg=bad):
                auto_merge.parse_rules(bad)

    def test_patterns_stay_inside_one_folder_unless_they_use_two_stars(self):
        rules = auto_merge.parse_rules(json.dumps(RULES))
        self.assertEqual(auto_merge.area_of(rules, 'server/test_a.py'), 'tests')
        self.assertIsNone(auto_merge.area_of(rules, 'server/test_a/b.py'))
        self.assertIsNone(auto_merge.area_of(rules, 'x/server/test_a.py'))
        self.assertEqual(auto_merge.area_of(rules, 'ui/src/a/b/c.tsx'), 'ui')
        self.assertIsNone(auto_merge.area_of(rules, 'ui/src'))
        self.assertIsNone(auto_merge.area_of(rules, 'ui/index.html.bak'))

    def test_links_binaries_mode_changes_and_new_executables_wait(self):
        os.symlink('/etc/passwd', self.work / 'ui/src/link.ts')
        head = self.commit(self.work, 'Link')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['scope'])
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        (self.work / 'ui/src/logo.bin').write_bytes(bytes(range(256)) * 4)
        head = self.commit(self.work, 'Binary')
        self.assertIn('binary', auto_merge.evaluate(self.work, head)[0]['checks'][-3]['text'])
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        os.chmod(self.work / 'ui/src/App.tsx', 0o755)
        head = self.commit(self.work, 'Mode')
        self.assertIn('file mode', auto_merge.evaluate(self.work, head)[0]['checks'][-3]['text'])
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        self.write(self.work, 'ui/src/run.sh', '#!/bin/sh\n')
        os.chmod(self.work / 'ui/src/run.sh', 0o755)
        head = self.commit(self.work, 'Executable')
        self.assertIn('executable', auto_merge.evaluate(self.work, head)[0]['checks'][-3]['text'])

    def test_git_config_and_attributes_cannot_hide_changes_or_change_line_counts(self):
        self.git(self.repo, 'config', 'diff.ignoreSubmodules', 'all')
        self.git(self.repo, 'config', 'diff.external', 'false')
        self.git(self.repo, 'config', 'diff.hide.textconv', 'true')
        self.write(self.repo, '.gitattributes', '*.tsx binary\n*.cjs diff=hide\n')
        self.commit(self.repo, 'Set diff attributes')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '--ff-only', 'main')
        for name, expected in (('ui/src/App.tsx', {'ui': 2}), ('ui/test_new.cjs', {'tests': 1})):
            self.git(self.work, 'reset', '-q', '--hard', 'main')
            decision = auto_merge.evaluate(self.work, self.change(name, 'worker\n'))[0]
            self.assertEqual(self.failed(decision), [])
            self.assertEqual(decision['areas'], expected)
        base = self.git(self.work, 'rev-parse', 'main')
        for path in ('ui/src/submodule', 'server/fixtures/submodule'):
            self.git(self.work, 'reset', '-q', '--hard', base)
            (self.work / path).mkdir(parents=True, exist_ok=True)
            self.git(self.work, 'update-index', '--add', '--cacheinfo', f'160000,{base},{path}')
            self.git(self.work, 'commit', '-m', 'Gitlink', '-m', STAMP)
            decision = auto_merge.evaluate(self.work, self.git(self.work, 'rev-parse', 'HEAD'))[0]
            self.assertEqual(self.failed(decision), ['scope'])  # the worktree itself is clean
            self.assertIn(f'{path} is a link or a submodule', decision['checks'][-3]['text'])

    def test_late_nul_and_large_text_blobs_wait(self):
        for content in (b'a' * 9000 + b'\0\n', b'a' * (auto_merge.MAX_BLOB + 1)):
            (self.work / 'ui/src/App.tsx').write_bytes(content)
            decision = auto_merge.evaluate(self.work, self.commit(self.work, 'Blob'))[0]
            self.assertEqual(self.failed(decision), ['scope'])
            self.assertIn('binary or too large', decision['checks'][-3]['text'])

    def test_a_rename_out_of_an_area_counts_both_sides(self):
        self.git(self.work, 'mv', 'ui/src/App.tsx', 'server/App.py')
        head = self.commit(self.work, 'Move')
        decision = auto_merge.evaluate(self.work, head)[0]
        self.assertEqual(self.failed(decision), ['scope'])
        self.assertIn('server/App.py', decision['checks'][-3]['text'])

    def test_checklist_hit_or_no_answer_waits(self):
        head = self.change()
        self.rows = ['tmux kill-server']
        decision = auto_merge.evaluate(self.work, head)[0]
        self.assertEqual(self.failed(decision), ['checklist'])
        self.assertIn('tmux kill-server', decision['checks'][-2]['text'])
        self.rows = None
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['checklist'])

    def test_a_conflict_with_main_waits(self):
        self.write(self.repo, 'ui/src/App.tsx', 'export const app = 9\n')
        self.commit(self.repo, 'Main moves')
        self.git(self.repo, 'push', 'origin', 'main')
        head = self.change()
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['merge'])

    def test_clean_merge_with_unreviewed_content_in_a_changed_file_waits(self):
        middle = '\n'.join(f'line {i}' for i in range(12))
        self.write(self.repo, 'ui/src/App.tsx', f'first\n{middle}\nlast\n')
        self.commit(self.repo, 'Add separated lines')
        self.git(self.work, 'merge', '--ff-only', 'main')
        self.write(self.repo, 'ui/src/App.tsx', f'main\n{middle}\nlast\n')
        self.commit(self.repo, 'Edit first line on main')
        self.git(self.repo, 'push', 'origin', 'main')
        self.enforce()
        before = self.origin_main()
        head = self.change(text=f'first\n{middle}\nworker\n')
        decision = auto_merge.process(self.job(head))
        self.assertEqual(self.failed(decision), ['merge'])
        self.assertEqual(decision['checks'][-1]['text'],
                         'The main branch changed the same files. Rebase the branch, then ask for a new review.')
        self.assertEqual(self.origin_main(), before)
        self.git(self.work, 'reset', '-q', '--hard', 'main^')
        result = auto_merge.process(self.job(self.change('ui/src/Other.tsx', 'worker\n')))
        self.assertEqual(result['outcome'], 'merged')
        self.assertEqual(self.git(self.origin, 'show', 'main:ui/src/App.tsx'), f'main\n{middle}\nlast')

    def test_main_checkout_detached_head_and_uncommitted_changes_wait(self):
        head = self.change()
        self.write(self.work, 'ui/src/App.tsx', 'export const app = 5\n')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['branch'])
        self.git(self.work, 'checkout', '-q', '.')
        self.git(self.work, 'checkout', '-q', '--detach')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['branch'])
        self.git(self.repo, 'checkout', '-q', '-b', 'side')
        self.write(self.repo, 'ui/src/App.tsx', 'export const app = 6\n')
        side = self.commit(self.repo, 'Side')
        self.assertEqual(self.failed(auto_merge.evaluate(self.repo, side)[0]), ['branch'])

    def test_untracked_files_wait_but_ignored_files_do_not(self):
        head = self.change()
        self.write(self.repo, '.git/info/exclude', '*.ignored\n')
        self.write(self.work, 'nested/a.ignored', 'ignored\n')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
        for path in ('new.txt', 'nested/new.txt'):
            self.write(self.work, path, 'untracked\n')
            decision, plan = auto_merge.evaluate(self.work, head)
            self.assertEqual(self.failed(decision), ['branch'])
            self.assertEqual(decision['checks'][1]['text'], 'The worktree has uncommitted or untracked files.')
            self.assertIsNone(plan)
            (self.work / path).unlink()

    def test_a_failed_fetch_and_a_folder_outside_git_fail_closed(self):
        head = self.change()
        real = subprocess.run

        def run(argv, **kwargs):
            if argv[:1] == ['git'] and argv[3] == 'fetch':
                return subprocess.CompletedProcess(argv, 1, '', 'Fetch failed')
            return real(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['git'])
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.root / 'gone.git'))
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), ['git'])
        decision, plan = auto_merge.evaluate(self.root, head)
        self.assertEqual(self.failed(decision), ['git'])
        self.assertIsNone(plan)

    def test_a_commit_already_on_main_is_not_a_decision(self):
        head = self.git(self.work, 'rev-parse', 'HEAD')
        self.assertEqual(auto_merge.evaluate(self.work, head)[0]['outcome'], 'already')
        self.assertIsNone(auto_merge.process(self.job(head)))
        self.assertEqual(auto_merge.records(), [])


class Modes(Base):
    def test_missing_or_bad_mode_file_is_observe_never_enforce(self):
        self.assertEqual(auto_merge.read_mode(), 'observe')
        auto_merge.mode_path().parent.mkdir(parents=True)
        for text in ('enforce\n', '{"mode": "enforce"}', '{"mode": "enforce", "since": "now"}', '{"mode": "merge", "since": 1}',
                     '["enforce"]', *[f'{{"mode": "enforce", "since": {value}}}'
                                     for value in ('true', 'NaN', 'Infinity', '-1', '1e400', '1.5')]):
            auto_merge.mode_path().write_text(text)
            self.assertEqual(auto_merge.read_setting(), {'mode': 'observe', 'since': 0, 'paused': False}, text)
        auto_merge.set_mode('enforce')
        self.assertEqual(auto_merge.read_mode(), 'enforce')
        with self.assertRaises(auto_merge.AutoMergeError):
            auto_merge.set_mode('on')

    def test_enforce_file_without_this_process_grant_is_paused(self):
        head = self.change()
        before = self.origin_main()
        path = auto_merge.mode_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'mode': 'enforce', 'since': 1}))
        paused = {'mode': 'observe', 'since': 0, 'paused': True}
        self.assertEqual(auto_merge.read_setting(), paused)
        self.enforce()
        self.assertEqual(auto_merge.read_mode(), 'enforce')
        self.assertFalse(auto_merge.state()['paused'])
        saved = path.read_text()
        path.write_text(json.dumps({'mode': 'enforce', 'since': 2}))
        self.assertEqual(auto_merge.read_setting(), paused)
        path.write_text(saved)
        auto_merge._grant = None  # a new server process
        self.assertEqual(auto_merge.read_setting(), paused)
        self.assertTrue(auto_merge.state()['paused'])
        self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
        self.assertEqual(self.origin_main(), before)

    def test_observe_records_would_merge_and_runs_no_git_write(self):
        head = self.change()
        before = self.origin_main()
        refs = self.refs()
        fetch_head = Path(self.git(self.work, 'rev-parse', '--path-format=absolute', '--git-path', 'FETCH_HEAD'))
        self.assertFalse(fetch_head.exists())
        real = subprocess.run
        seen = []
        global_config = str(self.root / 'global.conf')
        helping = []
        Path(global_config).write_text('[credential]\n\thelper = cache\n\thelper = store --file /srv/credentials\n'
                                       '[credential "https://git.example"]\n\thelper =\n\thelper = !/usr/bin/tool get\n'
                                       '\tusername = someone\n[credential "https://other.example"]\n\tuseHttpPath = true\n'
                                       '[url "https://evil.example/"]\n\tinsteadOf = https://git.example/\n'
                                       '[core]\n\tfsmonitor = /srv/evil\n')

        def run(argv, **kwargs):
            if argv[3:5] == ['config', '--includes']:
                self.assertEqual(argv, credential_lookup(self.repo))
                self.assertNotIn('env', kwargs)
                helping.append(1)
            elif argv[:3] == ['git', 'config', '--file']:
                self.assertEqual(git_settings(kwargs['env']), GIT_SETTINGS)
            elif argv[0] == 'git':
                command = argv[3:]
                seen.append(command)
                env = kwargs['env']
                if command[0] in ('check-attr', 'diff-index') or command[:2] == ['ls-files', '-s'] or command == ['config', '--null', '--list']:
                    # The clean check asks Git with the user's own config, and only these read-only commands.
                    self.assertEqual(env['GIT_CONFIG_GLOBAL'], global_config)
                    self.assertEqual(git_settings(env)[:1], [('core.fsmonitor', 'false')])
                    return real(argv, **kwargs)
                self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
                self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
                self.assertEqual(env['GIT_ATTR_NOSYSTEM'], '1')
                want = GIT_SETTINGS
                if is_shadow_status(argv):
                    want = GIT_SETTINGS[:-1]
                if command[0] in ('fetch', 'ls-remote'):
                    # Only the user's credential settings come from the global config, host settings included.
                    want = GIT_SETTINGS + [('credential.helper', 'cache'), ('credential.helper', 'store --file /srv/credentials'),
                                           ('credential.https://git.example.helper', ''),
                                           ('credential.https://git.example.helper', '!/usr/bin/tool get'),
                                           ('credential.https://git.example.username', 'someone'),
                                           ('credential.https://other.example.usehttppath', 'true')]
                self.assertEqual(git_settings(env), want)
            result = real(argv, **kwargs)
            if argv[3] == 'merge-base':
                self.assertEqual(result.stderr, '')
            return result

        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=global_config), mock.patch.object(auto_merge, 'RUN', run):
            decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'would_merge')
        self.assertEqual(len(helping), 2)
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(self.refs(), refs)
        self.assertFalse(fetch_head.exists())
        self.assertIn(['fetch', '--no-tags', '--no-write-fetch-head', str(self.origin), 'refs/heads/main'], seen)
        self.assertFalse(any(command[0] == 'fetch' and 'origin' in command[1:] for command in seen))
        self.assertFalse(any(command[0] == 'diff' for command in seen))
        self.assertFalse({'push', 'commit-tree', 'update-ref', 'merge', 'commit', 'reset', 'checkout', 'rebase',
                          'stash', 'branch'} & {command[0] for command in seen}, seen)
        self.assertEqual([item['outcome'] for item in auto_merge.records()], ['would_merge'])

    def test_observe_runs_only_the_metadata_allowlist_in_the_worker_repository(self):
        head = self.change()
        active = ['vendor/**', ':(exclude)vendor/ignored']
        for value in active:
            self.git(self.repo, 'config', '--add', 'submodule.active', value)
        scratch = paths.CACHE / 'auto-merge-git' / (hashlib.sha256(os.fsencode(self.repo)).hexdigest()[:16] + '.git')
        local_commands = {'rev-parse', 'symbolic-ref', 'config', 'ls-files', 'check-attr', 'diff-index'}
        cache = paths.CACHE / 'auto-merge-status'
        object_commands = {'ls-remote', 'fetch', 'cat-file', 'ls-tree', 'merge-base', 'merge-tree', 'log', 'show'}
        for folder in (self.work, self.work / 'server'):
            with self.subTest(folder=folder), mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
                self.assertEqual(auto_merge.process({**self.job(head), 'folder': str(folder)})['outcome'], 'would_merge')
                seen = set()
                shadows = set()
                configs = set()
                active_copies = []
                for call in run.call_args_list:
                    argv = call.args[0]
                    if is_shadow_status(argv):
                        shadow = Path(argv[1].removeprefix('--git-dir='))
                        self.assertEqual(shadow.parent, cache)
                        self.assertEqual(argv[2], f'--work-tree={self.work}')
                        self.assertEqual(argv[4:], ['--porcelain=v2', '-z', '--ignore-submodules=dirty', '--untracked-files=all'])
                        self.assertEqual(call.kwargs['env']['GIT_OPTIONAL_LOCKS'], '0')
                        shadows.add(shadow)
                        continue
                    if argv[:3] == ['git', 'config', '--file']:
                        config = Path(argv[3])
                        self.assertEqual(config.name, 'config')
                        self.assertEqual(argv[4:6], ['--add', '--'])
                        configs.add(config.parent)
                        if argv[6] == 'submodule.active':
                            active_copies.append(argv[7])
                        continue
                    if argv[3:5] == ['config', '--includes']:
                        self.assertEqual(argv, credential_lookup(self.repo))
                        self.assertNotIn('env', call.kwargs)
                        continue
                    self.assertEqual(argv[:2], ['git', '-C'])
                    directory, command = Path(argv[2]), argv[3]
                    if directory in (self.work, folder, self.repo):
                        self.assertIn(command, local_commands, argv)
                        self.assertNotIn('GIT_ATTR_SOURCE', call.kwargs['env'])
                    else:
                        self.assertEqual(directory, scratch, argv)
                        self.assertEqual(call.kwargs['env']['GIT_ATTR_SOURCE'], auto_merge.EMPTY_TREE)
                        seen.add(command)
                self.assertTrue(object_commands <= seen)
                self.assertEqual(len(shadows), 1)
                self.assertEqual(configs, shadows)
                self.assertEqual(active_copies, active)
                self.assertEqual(list(cache.iterdir()), [])
        self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)
        self.assertEqual((scratch / 'objects/info/alternates').read_bytes(),
                         os.fsencode(self.repo / '.git/objects') + b'\n')

    def test_scratch_rewrites_alternates_and_removes_attributes_and_hooks(self):
        head = self.change()
        scratch = auto_merge._scratch(self.repo)
        sample = scratch / 'hooks/pre-push.sample'
        sample.write_text('sample\n')
        for name in ('pre-push', 'post-merge'):
            with self.subTest(name=name):
                (scratch / 'objects/info/alternates').write_text('/wrong/objects\n')
                attributes = scratch / 'info/attributes'
                attributes.write_text('* merge=evil\n')
                hook = scratch / 'hooks' / name
                hook.write_text('unexpected hook\n')
                self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
                self.assertFalse(attributes.exists())
                self.assertFalse(hook.exists())
                self.assertTrue(sample.is_file())
                self.assertEqual((scratch / 'objects/info/alternates').read_bytes(),
                                 os.fsencode(self.repo / '.git/objects') + b'\n')

    def test_scratch_recreates_any_config_other_than_the_bare_init_config(self):
        head = self.change()
        scratch = auto_merge._scratch(self.repo)
        config = scratch / 'config'
        initial = config.read_bytes()
        for extra in (b'[merge "evil"]\n\tdriver = false\n', b'\thooksPath = /wrong/hooks\n'):
            with self.subTest(extra=extra):
                config.write_bytes(initial + extra)
                stale = scratch / 'stale-file'
                stale.touch()
                self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
                self.assertEqual(config.read_bytes(), initial)
                self.assertFalse(stale.exists())

    def test_scratch_rejects_a_multiline_alternates_path(self):
        for name in ('repo\nwith newline', 'repo\rwith carriage return'):
            with self.subTest(name=name):
                repo = self.root / name
                self.git(self.root, 'init', '-b', 'main', str(repo))
                with self.assertRaisesRegex(auto_merge.GitError, 'one absolute repository path'):
                    auto_merge._scratch(repo)

    def assert_merge_driver_is_ignored(self, tracked):
        name = 'ui/src/App.tsx'
        self.write(self.repo, name, 'first\nkeep\nlast\n')
        self.commit(self.repo, 'Add three lines')
        self.git(self.work, 'reset', '--hard', 'main')
        self.write(self.repo, name, 'shared\nkeep\nlast\n')
        self.commit(self.repo, 'Main edits the first line')
        self.git(self.repo, 'push', 'origin', 'main')
        if tracked:
            self.write(self.work, 'ui/src/.gitattributes', '* merge=evil\n')
        head = self.change(name, 'shared\nkeep\nworker\n')
        baseline, plan = auto_merge.evaluate(self.work, head)
        self.assertIsNotNone(plan, baseline)
        called = self.root / 'merge-driver-called'
        if not tracked:
            self.write(self.repo, '.git/info/attributes', '* merge=evil\n')
        self.git(self.repo, 'config', 'merge.evil.driver', f'touch "{called}"; false')
        self.assertEqual((result := auto_merge.process(self.job(head)))['outcome'], 'would_merge', result)
        self.assertEqual(result['checks'], baseline['checks'])
        self.assertFalse(called.exists())
        self.enforce()
        self.assertEqual((result := auto_merge.process(self.job(head)))['outcome'], 'merged', result)
        self.assertEqual(result['checks'], baseline['checks'])
        self.assertFalse(called.exists())

    def test_info_attributes_cannot_run_a_local_merge_driver(self):
        self.assert_merge_driver_is_ignored(tracked=False)

    def test_worker_tree_attributes_cannot_run_a_local_merge_driver(self):
        self.assert_merge_driver_is_ignored(tracked=True)

    def test_observe_runs_no_clean_filter_or_textconv(self):
        for name in ('App.tsx', 'Other.tsx'):
            with self.subTest(name=name):
                head = self.change(f'ui/src/{name}', f'// {name}\n')
                called = self.root / 'git-helper-called'
                command = f'touch "{called}"; cat'
                attributes = self.repo / '.git/info/attributes'
                attributes.write_text(f'{name} filter=evil diff=evil\n')
                self.git(self.repo, 'config', 'filter.evil.clean', command)
                self.git(self.repo, 'config', 'diff.evil.textconv', command)
                path = self.work / 'ui/src' / name
                status = path.stat()
                os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns - 2_000_000_000))
                # A path with a filter is never compared: the branch waits and the filter does not run.
                decision = auto_merge.process(self.job(head))
                self.assertEqual(decision['outcome'], 'wait')
                self.assertEqual(decision['checks'][1]['text'], auto_merge.UNSUPPORTED)
                self.assertFalse(called.exists())
                # A textconv alone is not a filter: Git status does not use it, and the decision stands.
                attributes.write_text(f'{name} diff=evil\n')
                self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
                self.assertFalse(called.exists())
                attributes.unlink()

    def test_observe_disables_configured_fsmonitor_hooks(self):
        head = self.change()
        called = self.root / 'fsmonitor-called'
        self.git(self.repo, 'config', 'extensions.worktreeConfig', 'true')
        for scope in ('--local', '--worktree'):
            with self.subTest(scope=scope):
                hook = self.root / f'fsmonitor-{scope}'
                hook.write_text(f'#!/bin/sh\necho called > "{called}"\nexit 1\n')
                hook.chmod(0o755)
                self.git(self.work, 'config', scope, 'core.fsmonitor', str(hook))
                self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
                self.assertFalse(called.exists())

    def test_push_ignores_global_url_rewrites_and_the_repository_hook(self):
        other = self.root / 'other.git'
        self.git(self.root, 'clone', '--bare', str(self.origin), str(other))
        before = self.origin_main()
        config = self.root / 'global.conf'
        called = self.root / 'pre-push-called'
        hook = self.root / 'hooks' / 'pre-push'
        self.write(self.root, 'hooks/pre-push', f'#!/bin/sh\necho called >> "{called}"\n')
        hook.chmod(0o755)
        self.git(self.repo, 'config', 'core.hooksPath', str(hook.parent))
        self.enforce()
        for count, option in enumerate(('insteadOf', 'pushInsteadOf'), 1):
            with self.subTest(option=option):
                config.write_text(f'[url "{other}"]\n\t{option} = {self.origin}\n')
                head = self.change(text=f'export const app = {count + 1}\n')
                with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(config)):
                    result = auto_merge.process(self.job(head))
                self.assertEqual(result['outcome'], 'merged', result)
                self.assertEqual(self.origin_main(), result['merge'])
                self.assertEqual(self.git(other, 'rev-parse', 'main'), before)
                self.assertFalse(called.exists())

    def test_configured_refspecs_do_not_move_local_refs_in_either_mode(self):
        self.git(self.repo, 'branch', 'victim')
        self.git(self.repo, 'config', 'remote.origin.fetch', '+refs/heads/main:refs/heads/victim')
        self.git(self.repo, 'config', 'fetch.prune', 'true')
        self.git(self.repo, 'config', 'fetch.pruneTags', 'true')
        self.write(self.repo, 'server/server.py', 'print(2)\n')
        moved = self.commit(self.repo, 'Move the remote main')
        self.git(self.repo, 'push', str(self.origin), 'main')
        self.git(self.repo, 'config', 'push.followTags', 'true')
        self.git(self.repo, 'tag', '-a', 'local-only', '-m', 'Keep this tag local')
        self.assertEqual(self.git(self.origin, 'tag', '--list'), '')
        head = self.change()
        before = self.refs()
        result = auto_merge.process(self.job(head))
        self.assertEqual(result['outcome'], 'would_merge')
        self.assertEqual(self.refs(), before)
        self.enforce()
        result = auto_merge.process(self.job(head))
        self.assertEqual(result['outcome'], 'merged')
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.git(self.origin, 'log', '-1', '--format=%P', 'main').split(), [moved, head])
        self.assertEqual(self.git(self.origin, 'tag', '--list'), '')

    def test_off_decides_nothing(self):
        auto_merge.set_mode('off')
        self.assertIsNone(auto_merge.process(self.job(self.change())))
        self.assertEqual(auto_merge.records(), [])
        self.assertEqual(auto_merge.observe([{'id': '%1'}], {'%1': {'%2': {'commit': 'a' * 40, 'at': 1}}},
                                            lambda entry: str(self.work), start=False), [])

    def test_a_failed_check_is_recorded_as_wait_once(self):
        head = self.change('server/server.py', 'print(3)\n')
        self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'wait')
        auto_merge.process(self.job(head))
        self.assertEqual([item['outcome'] for item in auto_merge.records()], ['wait'])

    def test_enforce_pushes_one_merge_commit_that_a_revert_undoes(self):
        self.enforce()
        head = self.change()
        before = self.origin_main()
        local_main = self.git(self.repo, 'rev-parse', 'main')
        self.write(self.repo, 'server/server.py', 'print("uncommitted work of another session")\n')
        alerts = []
        decision = auto_merge.process(self.job(head), lambda *args: alerts.append(args))
        self.assertEqual(decision['outcome'], 'merged')
        merged = self.origin_main()
        self.assertEqual(decision['merge'], merged)
        self.assertEqual(self.git(self.origin, 'log', '-1', '--format=%P', merged).split(), [before, head])
        message = self.git(self.origin, 'log', '-1', '--format=%B', merged)
        self.assertIn(f'Verified: tests(server 12 ok) screenshots(2) review(codex APPROVED {head[:12]})', message)
        self.assertEqual(auto_merge.stamp_items(message)['review'], f'codex APPROVED {head[:12]}')
        self.assertEqual(self.git(self.origin, 'show', f'{merged}:ui/src/App.tsx'), 'export const app = 2')
        # The main checkout, its branch and its uncommitted work are untouched; the worker branch is not rewritten.
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), local_main)
        self.assertIn('uncommitted work', (self.repo / 'server/server.py').read_text())
        self.assertEqual(self.git(self.work, 'rev-parse', 'HEAD'), head)
        self.assertEqual(alerts[0][0], 'Auto-merged')
        self.assertIn('not deployed', alerts[0][1])
        # A plain revert of the merge commit restores the old content.
        self.git(self.repo, 'checkout', '-q', '.')
        self.git(self.repo, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.git(self.repo, 'revert', '--no-edit', '-m', '1', merged)
        self.assertEqual((self.repo / 'ui/src/App.tsx').read_text(), 'export const app = 1\n')

    def test_enforce_merges_a_branch_that_is_behind_main_without_a_rebase(self):
        self.enforce()
        self.write(self.repo, 'server/server.py', 'print(2)\n')
        moved = self.commit(self.repo, 'Main moves')
        self.git(self.repo, 'push', 'origin', 'main')
        head = self.change('server/test_new.py', 'import unittest\n')
        decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'merged')
        self.assertEqual(self.git(self.origin, 'log', '-1', '--format=%P', 'main').split(), [moved, head])
        self.assertEqual(self.git(self.origin, 'show', 'main:server/server.py'), 'print(2)')

    def test_enforce_never_merges_when_a_check_fails(self):
        self.enforce()
        before = self.origin_main()
        head = self.change('server/server.py', 'print(4)\n')
        self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'wait')
        first = self.change()
        self.change(text='export const app = 4\n')
        self.assertEqual(auto_merge.process(self.job(first))['outcome'], 'wait')
        self.assertEqual(self.origin_main(), before)

    def test_an_approval_older_than_enforce_does_not_merge(self):
        head = self.change()
        self.enforce()
        since = auto_merge.read_setting()['since']
        before = self.origin_main()
        decision = auto_merge.process(self.job(head, at=since - 10))
        self.assertEqual(decision['outcome'], 'would_merge')
        self.assertIn('older than enforce', decision['note'])
        for bad in (None, 'soon', True, float('nan'), float('inf')):
            self.assertEqual(auto_merge.process(self.job(head, at=bad))['outcome'], 'would_merge')
        self.assertEqual(self.origin_main(), before)

    def test_an_approval_older_than_a_day_does_not_merge(self):
        head = self.change()
        with mock.patch.object(auto_merge.time, 'time', return_value=1):
            self.enforce()
        before = self.origin_main()
        old = int(auto_merge.time.time()) - auto_merge.APPROVAL_HOURS * 3600 - 60
        decision = auto_merge.process(self.job(head, at=old))
        self.assertEqual(decision['outcome'], 'would_merge')
        self.assertIn('24 hours', decision['note'])
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(auto_merge.process(self.job(head, at=old + 3600))['outcome'], 'merged')

    def test_a_folder_git_cannot_read_is_logged_by_folder_and_approval(self):
        decision = auto_merge.process({**self.job('a' * 40), 'folder': str(self.root)})
        self.assertEqual((decision['outcome'], decision['root'], decision['head']), ('wait', str(self.root), 'a' * 40))
        other = self.root / 'elsewhere'
        other.mkdir()
        auto_merge.process({**self.job('a' * 40), 'folder': str(other)})
        self.assertEqual(len(auto_merge.records()), 2)

    def test_a_refused_push_is_a_failure_and_nothing_is_forced(self):
        self.enforce()
        head = self.change()
        hook = self.origin / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        before = self.origin_main()
        real = subprocess.run
        pushes = []

        def run(argv, **kwargs):
            if argv[0] == 'git' and argv[3] == 'push':
                pushes.append(argv[4:])
            return real(argv, **kwargs)

        alerts = []
        with mock.patch.object(auto_merge, 'RUN', run):
            decision = auto_merge.process(self.job(head), lambda *args: alerts.append(args))
        self.assertEqual(decision['outcome'], 'failed')
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(alerts[0][0], 'Auto-merge failed')
        self.assertTrue(pushes)
        self.assertEqual(len(pushes), 1)  # one plain attempt, no retry
        for args in pushes:
            self.assertFalse([item for item in args if item.startswith(('-', '+'))], args)
        self.assertIn('Nothing is forced', decision['error'])

    def pushes_of(self, run):
        return [call.args[0] for call in run.call_args_list if call.args[0][0] == 'git' and call.args[0][3] == 'push']

    def test_a_main_that_moved_before_the_push_is_refused_without_a_push(self):
        self.enforce()
        behind = self.origin_main()
        head = self.change()
        tree = self.git(self.origin, 'rev-parse', 'main^{tree}')
        checked = self.git(self.origin, 'commit-tree', tree, '-p', behind, '-m', 'Checked main')
        self.git(self.origin, 'update-ref', 'refs/heads/main', checked)
        newer = self.git(self.origin, 'commit-tree', tree, '-p', checked, '-m', 'Newer main')
        unrelated = self.git(self.origin, 'commit-tree', tree, '-m', 'Unrelated main')
        for remote in (unrelated, newer, behind, None):
            with self.subTest(remote=remote):
                decision, plan = auto_merge.evaluate(self.work, head)
                self.assertIsNotNone(plan, decision)
                self.assertEqual(plan['target_sha'], checked)
                if remote is None:
                    self.git(self.origin, 'update-ref', '-d', 'refs/heads/main')
                else:
                    self.git(self.origin, 'update-ref', 'refs/heads/main', remote)
                with mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run, \
                        self.assertRaisesRegex(auto_merge.MainMoved, 'moved after the checks'):
                    auto_merge.merge(plan, 'codex', self.job(head))
                self.assertEqual(self.pushes_of(run), [])
                self.assertEqual(self.git(self.origin, 'for-each-ref', '--format=%(objectname)', 'refs/heads/main'),
                                 remote or '')
                self.git(self.origin, 'update-ref', 'refs/heads/main', checked)

    def test_a_main_that_moves_during_the_push_gets_a_rejected_plain_push_and_no_force(self):
        self.enforce()
        real = subprocess.run
        for kind in ('newer', 'unrelated'):
            with self.subTest(kind=kind):
                self.git(self.work, 'fetch', '-q', 'origin')
                self.git(self.work, 'reset', '-q', '--hard', 'origin/main')
                head = self.change(text=f'export const app = "{kind}"\n')
                checked = self.origin_main()
                tree = self.git(self.origin, 'rev-parse', 'main^{tree}')
                parents = ['-p', checked] if kind == 'newer' else []
                moved = self.git(self.origin, 'commit-tree', tree, *parents, '-m', f'{kind} main')
                seen = []

                def run(argv, **kwargs):
                    if argv[0] == 'git':
                        seen.append(argv[3:])
                        if argv[3] == 'push':  # another session lands on main after Tether's last look
                            self.git(self.origin, 'update-ref', 'refs/heads/main', moved)
                    return real(argv, **kwargs)

                alerts = []
                with mock.patch.object(auto_merge, 'RUN', run):
                    decision = auto_merge.process(self.job(head), lambda *args: alerts.append(args))
                self.assertEqual(decision['outcome'], 'failed', decision)
                self.assertIn('did not accept a plain push', decision['error'])
                self.assertEqual(self.origin_main(), moved)  # the other session's commit stays
                pushes = [command for command in seen if command[0] == 'push']
                self.assertEqual(len(pushes), 1)
                self.assertEqual(pushes[0][1], str(self.origin))
                self.assertRegex(pushes[0][2], r'^[0-9a-f]{40}:refs/heads/main$')
                self.assertEqual(len(pushes[0]), 3)
                self.assertEqual(alerts[0][0], 'Auto-merge failed')
                self.assertEqual(self.git(self.work, 'rev-parse', 'HEAD'), head)  # the branch waits, unchanged

    def test_no_auto_merge_command_can_force(self):
        # The built push command, for any input, has no option and no forced refspec.
        self.assertEqual(auto_merge._push_argv('/srv/origin.git', 'a' * 40), ['push', '/srv/origin.git', 'a' * 40 + ':refs/heads/main'])
        for url, made in (('--force', 'a' * 40), ('+refs/heads/x', 'a' * 40), ('/srv/origin.git', '+' + 'a' * 39),
                          ('-f', 'a' * 40), ('--force-with-lease=main:' + 'a' * 40, 'a' * 40)):
            with self.assertRaises(auto_merge.GitError):
                auto_merge._push_argv(url, made)
        # No source line of the module builds a force flag or a forced refspec.
        source = Path(auto_merge.__file__).read_text()
        code = '\n'.join(line.split('#', 1)[0] for line in source.splitlines())
        code = re.sub(r'"""(?:.|\n)*?"""', '', code)
        for banned in ('--force', '--mirror', '--delete', "'-f'", '"-f"'):
            self.assertFalse(banned in code, f'{banned} appears in the code of auto_merge.py')
        # Every git argv of a whole Enforce run, merge included, is free of them too.
        self.enforce()
        head = self.change()
        with mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
            self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'merged')
        for call in run.call_args_list:
            for item in call.args[0]:
                self.assertFalse(item.startswith(('--force', '+')) or item == '-f', call.args[0])
        self.assertEqual(len(self.pushes_of(run)), 1)

    def test_push_is_never_called_for_an_unchecked_first_parent(self):
        self.enforce()
        head = self.change()
        checked = self.origin_main()
        _, plan = auto_merge.evaluate(self.work, head)
        unrelated = self.git(self.repo, 'commit-tree', 'HEAD^{tree}', '-m', 'Unrelated commit')
        seen = []

        def run(argv, **kwargs):
            seen.append(argv[3])
            if argv[3] == 'commit-tree':
                # Give merge() the originally planned commit despite the altered plan.
                argv = [*argv[:6], checked, *argv[7:]]
            return subprocess.run(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            for altered, message in ((unrelated, 'does not contain the checked main'), (head, 'as its first parent')):
                with self.subTest(altered=altered), self.assertRaisesRegex(auto_merge.GitError, message):
                    auto_merge.merge({**plan, 'target_sha': altered}, 'codex', self.job(head))
        self.assertNotIn('push', seen)
        self.assertEqual(self.origin_main(), checked)

    def test_happy_path_uses_the_exact_plain_push_argv(self):
        self.enforce()
        scratch = auto_merge._scratch(self.repo)
        for number in (2, 3):
            with self.subTest(number=number):
                head = self.change(text=f'export const app = {number}\n')
                with mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
                    decision = auto_merge.process(self.job(head))
                self.assertEqual(decision['outcome'], 'merged', decision)
                self.assertEqual(self.pushes_of(run), [['git', '-C', str(scratch), 'push', str(self.origin),
                                                       f"{decision['merge']}:refs/heads/main"]])
                self.assertEqual(self.origin_main(), decision['merge'])

    def test_leaving_enforce_before_the_push_stops_the_merge(self):
        self.enforce()
        head = self.change()
        before = self.origin_main()
        decision, plan = auto_merge.evaluate(self.work, head)
        auto_merge.set_mode('observe')
        with self.assertRaises(auto_merge.NotAuthorized):
            auto_merge.merge(plan, 'codex', self.job(head))
        self.assertEqual(self.origin_main(), before)

    def test_a_new_grant_before_the_push_rejects_the_old_approval_without_retry(self):
        now = int(auto_merge.time.time())
        with mock.patch.object(auto_merge.time, 'time', return_value=now - 1):
            self.enforce()
        head = self.change()
        before = self.origin_main()
        real = auto_merge.evaluate

        def evaluate(*args, **kwargs):
            result = real(*args, **kwargs)
            auto_merge.set_mode('observe')
            self.enforce()  # both switches happen in the approval's second
            return result

        with mock.patch.object(auto_merge.time, 'time', return_value=now), \
                mock.patch.object(auto_merge, 'evaluate', side_effect=evaluate) as check, \
                mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
            decision = auto_merge.process(self.job(head, at=now))
        self.assertEqual(decision['outcome'], 'failed')
        self.assertIn('older than enforce', decision['error'])
        check.assert_called_once()
        self.assertFalse(any(call.args[0][3] == 'push' for call in run.call_args_list))
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(auto_merge.process(self.job(head, at=now + 1))['outcome'], 'merged')

    def test_push_boundary_refuses_expired_or_nonfinite_approval(self):
        with mock.patch.object(auto_merge.time, 'time', return_value=1):
            self.enforce()
        before = self.origin_main()
        _, plan = auto_merge.evaluate(self.work, self.change())
        old = int(auto_merge.time.time()) - auto_merge.APPROVAL_HOURS * 3600 - 1
        with mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
            for at in (old, float('nan'), float('inf')):
                with self.subTest(at=at), self.assertRaises(auto_merge.NotAuthorized):
                    auto_merge.merge(plan, 'codex', self.job(plan['head'], at=at))
        self.assertFalse(any(call.args[0][3] == 'push' for call in run.call_args_list))
        self.assertEqual(self.origin_main(), before)

    def test_push_refuses_a_commit_with_the_wrong_tree(self):
        self.enforce()
        head = self.change()
        before = self.origin_main()
        _, plan = auto_merge.evaluate(self.work, head)
        wrong_tree = self.git(self.work, 'rev-parse', 'main^{tree}')

        def run(argv, **kwargs):
            self.assertNotEqual(argv[3], 'push')
            if argv[3] == 'commit-tree':
                argv = [*argv[:4], wrong_tree, *argv[5:]]
            return subprocess.run(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', side_effect=run):
            with self.assertRaisesRegex(auto_merge.GitError, 'does not contain the planned tree'):
                auto_merge.merge(plan, 'codex', self.job(head))
        self.assertEqual(self.origin_main(), before)

    def test_a_changed_worker_or_approval_fails_at_the_push_without_retry(self):
        self.enforce()
        head = self.change()
        before = self.origin_main()
        real = auto_merge.evaluate
        job = self.job(head)

        def fail_read():
            raise RuntimeError('approval store failed')

        cases = (
            (lambda: self.change(text='advanced\n'), 'branch changed'),
            (lambda: self.git(self.work, 'switch', '-c', 'other'), 'branch changed'),
            (lambda: self.write(self.work, 'nested/new.txt', 'untracked\n'), 'untracked files'),
            (lambda: self.write(self.work, 'ui/src/App.tsx', 'uncommitted\n'), 'uncommitted'),
            (lambda: job.update(current=self.job(head, at=job['approved_at'] + 1)['current']), 'approval changed'),
            (lambda: job.update(current=self.job('a' * 40, at=job['approved_at'])['current']), 'approval changed'),
            (lambda: job.update(current=lambda: {}), 'approval changed'),
            (lambda: job.update(current=None), 'approval changed'),
            (lambda: job.update(current=fail_read), 'Could not read'),
        )
        for change, reason in cases:
            with self.subTest(reason=reason):
                job = self.job(head)

                def evaluate(*args, **kwargs):
                    result = real(*args, **kwargs)
                    self.assertIsNotNone(result[1])
                    change()
                    return result

                with mock.patch.object(auto_merge, 'evaluate', side_effect=evaluate) as check, \
                        mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
                    decision = auto_merge.process(job)
                self.assertEqual(decision['outcome'], 'failed')
                self.assertIn(reason, decision['error'])
                check.assert_called_once()
                self.assertFalse(any(call.args[0][3] == 'push' for call in run.call_args_list))
                self.assertEqual(self.origin_main(), before)
                self.git(self.work, 'switch', '-q', 'work')
                self.git(self.work, 'reset', '-q', '--hard', head)
                (self.work / 'nested/new.txt').unlink(missing_ok=True)

    def test_a_main_that_moved_after_the_decision_stops_the_merge_with_one_attempt(self):
        self.enforce()
        head = self.change()
        real = auto_merge.evaluate
        calls = []

        def evaluate(folder, approved, **kwargs):
            result = real(folder, approved, **kwargs)
            if not calls:  # another session pushes between the decision and the push
                self.write(self.repo, 'server/server.py', 'print(7)\n')
                self.commit(self.repo, 'Race')
                self.git(self.repo, 'push', 'origin', 'main')
            calls.append(1)
            return result

        with mock.patch.object(auto_merge, 'evaluate', evaluate), \
                mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
            decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'failed')
        self.assertIn('moved after the checks', decision['error'])
        self.assertEqual(len(calls), 1)  # no second decision, no retry
        self.assertFalse([call for call in run.call_args_list if call.args[0][3] == 'push'])
        self.assertEqual(self.git(self.origin, 'show', 'main:server/server.py'), 'print(7)')
        self.assertEqual(self.git(self.origin, 'log', '-1', '--format=%s', 'main'), 'Race')


class Credentials(Base):
    """The lookup of the user's credential settings, with real Git and a home folder of its own."""

    def lookup(self, files, root=None, **environment):
        home = self.root / 'home'
        for name, text in files.items():
            path = home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text if isinstance(text, bytes) else text.encode())
        clean = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        clean.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), GIT_CONFIG_NOSYSTEM='1', **environment)
        with mock.patch.dict(os.environ, clean, clear=True):
            return auto_merge._credential_helpers(root or self.repo)

    def test_a_carriage_return_in_a_name_cannot_turn_another_setting_into_a_credential_helper(self):
        # In text mode the "\r" became a line break: the key ended there and the rest became the value.
        files = {'.gitconfig': b'[credential "https://git.example.helper\r!/srv/evil"]\n\tother = 1\n'
                               b'[other "credential.https://git.example.helper\r!/srv/evil"]\n\thelper = 1\n'
                               b'[credential "https://two\rlines"]\n\thelper = kept\n\tusername = a\\nb\n'}
        self.assertEqual(self.lookup(files), (('credential.https://two\rlines.helper', 'kept'),
                                              ('credential.https://two\rlines.username', 'a\nb')))

    def test_both_global_files_and_their_includes_give_the_settings_in_the_order_of_git(self):
        files = {'.config/git/config': '[credential]\n\thelper = xdg\n',
                 '.gitconfig': '[credential "https://git.example"]\n\thelper = home\n[include]\n\tpath = more\n',
                 'more': '[credential]\n\tuseHttpPath = true\n[core]\n\tfsmonitor = /srv/evil\n'}
        self.assertEqual(self.lookup(files), (('credential.helper', 'xdg'), ('credential.https://git.example.helper', 'home'),
                                              ('credential.usehttppath', 'true')))

    def test_a_conditional_include_applies_only_in_its_repository(self):
        other = self.root / 'other'
        self.git(self.root, 'init', '-q', str(other))
        files = {'.gitconfig': f'[includeIf "gitdir:{self.repo}/"]\n\tpath = work\n',
                 'work': '[credential "https://git.example"]\n\tusername = work\n'}
        self.assertEqual(self.lookup(files), (('credential.https://git.example.username', 'work'),))
        self.assertEqual(self.lookup(files, root=other), ())

    def test_settings_of_the_repository_and_of_the_command_environment_do_not_pass(self):
        self.git(self.repo, "config", "credential.helper", "!/srv/local")
        files = {'.gitconfig': '[credential]\n\thelper = mine\n'}
        found = self.lookup(files, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='!/srv/command')
        self.assertEqual(found, (('credential.helper', 'mine'),))

    def test_a_name_without_a_value_is_true_for_the_path_setting_and_an_error_for_a_helper(self):
        self.assertEqual(self.lookup({'.gitconfig': '[credential]\n\tuseHttpPath\n'}), (('credential.usehttppath', 'true'),))
        with self.assertRaises(auto_merge.GitError):
            self.lookup({'.gitconfig': '[credential]\n\thelper\n'})

    def test_no_settings_and_a_broken_config_are_different(self):
        self.assertEqual(self.lookup({'.gitconfig': '[user]\n\tname = x\n'}), ())
        with self.assertRaises(auto_merge.GitError):
            self.lookup({'.gitconfig': '[credential\n'})


class Ladder(Base):
    def levels(self):
        root = str(self.repo)
        scratch = auto_merge._scratch(root)
        _, target = auto_merge._fetch_target(root, scratch)
        return auto_merge.ladder(root, scratch, auto_merge.rules_at(scratch, target), target)

    def merge_one(self, number, name='ui/src/App.tsx'):
        self.git(self.work, 'fetch', '-q', 'origin')
        self.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        head = self.change(name, f'export const app = {number + 100}\n')
        decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'merged', decision)
        return decision['merge']

    def revert(self, commit, *flags):
        self.git(self.repo, 'checkout', '-q', 'main')
        self.git(self.repo, 'pull', '-q', '--ff-only', 'origin', 'main')
        self.git(self.repo, 'revert', '--no-edit', *flags, commit)
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        return self.git(self.repo, 'rev-parse', 'HEAD')

    def test_every_area_starts_on_the_first_rung(self):
        levels = self.levels()
        self.assertEqual({name: (area['rung'], area['limit']) for name, area in levels.items()},
                         {'tests': (1, auto_merge.RUNGS[1]), 'ui': (1, auto_merge.RUNGS[1])})

    def test_trust_reads_remotely_created_reverts_from_scratch_only(self):
        base = self.origin_main()
        for number in (2, 3):
            with self.subTest(number=number):
                changed = self.change(text=f'export const app = {number}\n')
                self.git(self.work, 'push', 'origin', f'{changed}:refs/heads/main')
                reverted = self.git(self.origin, 'commit-tree', f'{base}^{{tree}}', '-p', changed, '-m',
                                    f'Revert {number}\n\nThis reverts commit {changed}.')
                self.git(self.origin, 'update-ref', 'refs/heads/main', reverted)
                self.git(self.work, 'reset', '--hard', changed)
                head = self.change('ui/src/Another.tsx')
                result = auto_merge.process(self.job(head))
                self.assertEqual(self.failed(result), ['scope'])
                self.assertIn('trust is at zero', result['checks'][-3]['text'])
                scratch = auto_merge._scratch(self.repo)
                self.assertEqual(self.git(scratch, 'cat-file', '-t', reverted), 'commit')
                with self.assertRaises(subprocess.CalledProcessError):
                    self.git(self.repo, 'cat-file', '-t', reverted)
                self.git(self.work, 'fetch', 'origin', 'main')
                self.git(self.work, 'reset', '--hard', reverted)
                base = reverted

    def test_a_diff_over_the_limit_waits_and_a_diff_at_the_limit_passes(self):
        limit = auto_merge.RUNGS[auto_merge.START_RUNG]
        head = self.change('ui/src/Big.tsx', 'x\n' * limit)
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
        head = self.change('ui/src/Big.tsx', 'y\n' * limit + 'z\n')
        decision = auto_merge.evaluate(self.work, head)[0]
        self.assertEqual(self.failed(decision), ['scope'])
        self.assertIn(f'trust limit for this area is {limit}', decision['checks'][-3]['text'])

    def test_clean_tether_merges_widen_one_area_only(self):
        self.enforce()
        for number in range(auto_merge.CLEAN_MERGES - 1):
            self.merge_one(number)
        self.assertEqual((self.levels()['ui']['rung'], self.levels()['ui']['streak']), (1, auto_merge.CLEAN_MERGES - 1))
        self.merge_one(99)
        levels = self.levels()
        self.assertEqual((levels['ui']['rung'], levels['ui']['streak'], levels['ui']['limit']), (2, 0, auto_merge.RUNGS[2]))
        self.assertEqual(levels['tests']['rung'], 1)

    def test_edited_rungs_cannot_raise_the_limit_without_history(self):
        self.levels()
        path = auto_merge.trust_path()
        data = json.loads(path.read_text())
        for area, name in (('ui', 'ui/src/Large.tsx'), ('tests', 'server/test_large.py')):
            with self.subTest(area=area):
                data[str(self.repo)]['areas'][area].update(rung=3, streak=24, merges=24)
                path.write_text(json.dumps(data))
                level = self.levels()[area]
                self.assertEqual((level['rung'], level['limit'], level['streak'], level['merges']), (1, 60, 0, 0))
                self.git(self.work, 'reset', '-q', '--hard', 'main')
                decision, plan = auto_merge.evaluate(self.work, self.change(name, 'line\n' * 120))
                self.assertEqual(self.failed(decision), ['scope'])
                self.assertIn('120 changed lines. The trust limit for this area is 60', decision['checks'][-3]['text'])
                self.assertIsNone(plan)

    def test_edited_seen_lists_and_streak_do_not_count_as_clean_merges(self):
        self.enforce()
        self.levels()
        path = auto_merge.trust_path()
        data = json.loads(path.read_text())
        entry = data[str(self.repo)]
        for area, name in (('ui', 'ui/src/App.tsx'), ('tests', 'server/test_a.py')):
            with self.subTest(area=area):
                entry['merges_seen'] = [self.origin_main()]
                entry['areas'][area].update(streak=30, merges=30)
                path.write_text(json.dumps(data))
                self.merge_one(1, name)
                level = self.levels()[area]
                self.assertEqual((level['rung'], level['limit'], level['streak'], level['merges']), (1, 60, 1, 1))

    def test_stamped_commits_pushed_by_agents_and_observe_decisions_do_not_widen(self):
        for number in range(auto_merge.CLEAN_MERGES + 1):
            self.write(self.repo, 'ui/src/App.tsx', f'export const app = {number + 50}\n')
            self.commit(self.repo, f'Agent commit {number}', 'Verified: tests(ok) review(codex APPROVED)')
            self.git(self.repo, 'push', '-q', 'origin', 'main')
        head = self.change('ui/src/Other.tsx', 'export const other = 1\n')
        self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
        levels = self.levels()
        self.assertEqual((levels['ui']['rung'], levels['ui']['streak'], levels['ui']['merges']), (1, 0, 0))

    def test_a_log_record_for_a_merge_that_is_not_on_main_does_not_count(self):
        for _ in range(auto_merge.CLEAN_MERGES):
            auto_merge.record({'root': str(self.repo), 'outcome': 'merged', 'merge': 'a' * 40, 'areas': {'ui': 1},
                               'head': 'b' * 40, 'mode': 'enforce', 'checks': []})
        self.assertEqual(self.levels()['ui']['merges'], 0)

    def test_only_a_matching_first_parent_tether_merge_counts_once(self):
        self.write(self.repo, 'ui/src/App.tsx', 'main edit\n')
        ordinary = self.commit(self.repo, 'Ordinary commit')
        for _ in range(auto_merge.CLEAN_MERGES):
            auto_merge.record({'root': str(self.repo), 'outcome': 'merged', 'merge': ordinary,
                               'head': ordinary, 'areas': {'ui': 1}})
        branch = self.change('ui/src/Other.tsx', 'worker edit\n')
        self.git(self.repo, 'merge', '--no-ff', branch, '-m', 'Subject (Tether auto-merge)')
        forged = self.git(self.repo, 'rev-parse', 'HEAD')
        auto_merge.record({'root': str(self.repo), 'outcome': 'merged', 'merge': forged,
                           'head': branch, 'areas': {'ui': 1}})
        self.git(self.repo, 'push', 'origin', 'main')
        self.assertEqual((self.levels()['ui']['rung'], self.levels()['ui']['merges']), (1, 0))

        self.enforce()
        merged = self.merge_one(1)
        head = self.git(self.origin, 'rev-parse', merged + '^2')
        auto_merge.record({'root': str(self.repo), 'outcome': 'merged', 'merge': merged,
                           'head': head, 'areas': {'tests': 50}})
        levels = self.levels()
        self.assertEqual((levels['ui']['merges'], levels['tests']['merges']), (1, 0))

        # A new calculation must classify the real diff even if every log record lies about the area.
        log = auto_merge.log_path()
        rows = [dict(row, areas={'tests': 50}) for row in auto_merge.records()]
        log.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        auto_merge.trust_path().unlink()
        levels = self.levels()
        self.assertEqual((levels['ui']['merges'], levels['tests']['merges']), (1, 0))

    def test_a_revert_shrinks_the_area_and_the_user_restores_it(self):
        self.enforce()
        merged = self.merge_one(1)
        self.assertEqual(self.levels()['ui']['streak'], 1)
        revert = self.revert(merged, '-m', '1')
        levels = self.levels()
        self.assertEqual((levels['ui']['rung'], levels['ui']['streak'], levels['ui']['limit']), (0, 0, 0))
        self.assertEqual(levels['ui']['last_revert']['commit'], revert)
        self.assertEqual(levels['tests']['rung'], 1)
        # At zero trust the area waits, also in enforce mode.
        self.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        head = self.change(text='export const app = 77\n')
        decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'wait')
        self.assertIn('trust is at zero', decision['checks'][-3]['text'])
        with self.assertRaises(auto_merge.AutoMergeError):
            auto_merge.restore(str(self.repo), 'tests')  # not at zero
        with self.assertRaises(auto_merge.AutoMergeError):
            auto_merge.restore(str(self.work), 'ui')     # not a main checkout
        auto_merge.restore(str(self.repo), 'ui')
        self.assertEqual(self.levels()['ui']['rung'], 1)
        auto_merge._ladder_cache.clear()
        self.assertEqual(self.levels()['ui']['rung'], 1)
        saved = json.loads(auto_merge.trust_path().read_text())[str(self.repo)]
        self.assertEqual(saved['restored'], {'ui': revert})
        self.revert(self.merge_one(2), '-m', '1')
        self.assertEqual(self.levels()['ui']['rung'], 0)
        auto_merge.restore(str(self.repo), 'ui')
        self.assertEqual(self.levels()['ui']['rung'], 1)

    def test_a_restore_marker_must_be_an_ancestor_commit(self):
        worker = self.change('ui/src/Other.tsx', 'worker\n')
        self.write(self.repo, 'ui/src/App.tsx', 'main change\n')
        commit = self.commit(self.repo, 'Main change')
        self.git(self.repo, 'push', 'origin', 'main')
        self.revert(commit)
        self.assertEqual(self.levels()['ui']['rung'], 0)
        path = auto_merge.trust_path()
        data = json.loads(path.read_text())
        entry = data[str(self.repo)]
        entry['areas']['ui']['rung'] = 3
        self.git(self.repo, 'tag', '-am', 'Restore tag', 'restore-tag')
        tag = self.git(self.repo, 'rev-parse', 'restore-tag')
        for marker in (worker, tag, 'a' * 40, 'short', {'tip': self.origin_main()}):
            with self.subTest(marker=marker):
                entry['restored'] = {'ui': marker}
                path.write_text(json.dumps(data))
                self.assertEqual(self.levels()['ui']['rung'], 0)

    def test_reductions_survive_pruning_and_a_restart_then_restore(self):
        self.enforce()
        merged = self.merge_one(1)
        revert = self.revert(merged, '-m', '1')
        self.assertEqual(self.levels()['ui']['rung'], 0)
        self.write(self.repo, 'server/server.py', 'print(2)\n')
        self.commit(self.repo, 'Advance main')
        self.git(self.repo, 'push', 'origin', 'main')
        with mock.patch.object(auto_merge, 'HISTORY', 1):
            self.assertEqual(self.levels()['ui']['rung'], 0)
            saved = json.loads(auto_merge.trust_path().read_text())[str(self.repo)]
            self.assertEqual((saved['merges_seen'], saved['reverts_seen']), ([], []))
            self.assertEqual(saved['areas']['ui']['last_revert']['commit'], revert)
            # Settings uses the push repository even if a tracking ref points to an older tip.
            auto_merge._ladder_cache.clear()
            auto_merge._target_cache.clear()
            self.git(self.repo, 'update-ref', 'refs/remotes/origin/main', revert)
            self.assertEqual(auto_merge.state([str(self.repo)])['trust'][0]['areas'][1]['reverts'], 1)
            self.assertEqual(json.loads(auto_merge.trust_path().read_text())[str(self.repo)]['tip'], self.origin_main())
            auto_merge.restore(str(self.repo), 'ui')
            levels = self.levels()
            self.assertEqual((levels['ui']['rung'], levels['ui']['streak'], levels['ui']['reverts']), (1, 0, 1))

    def test_malformed_trust_replays_reverts_and_an_unreadable_revert_reduces_every_area(self):
        for name, text in (('server/test_new.py', 'test\n'), ('ui/src/App.tsx', 'worker\n')):
            self.write(self.repo, name, text)
            commit = self.commit(self.repo, 'Agent change')
            self.git(self.repo, 'push', 'origin', 'main')
            self.revert(commit)
        self.assertEqual({level['rung'] for level in self.levels().values()}, {0})
        path = auto_merge.trust_path()
        for bad in ('{', json.dumps({str(self.repo): {'areas': {'ui': {'rung': 999}}}})):
            path.write_text(bad)
            self.assertEqual({level['rung'] for level in self.levels().values()}, {0})
        self.git(self.repo, 'commit', '--allow-empty', '-m', f"Revert unknown\n\nThis reverts commit {'a' * 40}.")
        self.git(self.repo, 'push', 'origin', 'main')
        self.assertEqual({level['reverts'] for level in self.levels().values()}, {2})

    def test_a_revert_of_an_agent_commit_shrinks_and_a_revert_of_a_revert_does_not(self):
        self.write(self.repo, 'server/test_a.py', 'import unittest\n')
        commit = self.commit(self.repo, 'Agent test')
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        revert = self.revert(commit)
        self.assertEqual((self.levels()['tests']['rung'], self.levels()['ui']['rung']), (0, 1))
        self.revert(revert)
        self.assertEqual(self.levels()['tests']['reverts'], 1)

    def test_reverts_start_with_the_rules_commit_even_when_timestamps_match(self):
        self.origin = self.root / 'history.git'
        self.git(self.root, 'init', '--bare', '-b', 'main', str(self.origin))
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.origin))
        self.git(self.repo, 'rm', 'docs/auto-merge.json')
        self.git(self.repo, 'commit', '--amend', '--no-edit')
        date = {'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z'}
        with mock.patch.dict(os.environ, date):
            self.write(self.repo, 'ui/src/App.tsx', 'worker\n')
            commit = self.commit(self.repo, 'Early change')
            self.git(self.repo, 'revert', '--no-edit', commit)
            self.write(self.repo, 'docs/auto-merge.json', json.dumps(RULES))
            self.commit(self.repo, 'Add rules')
            self.git(self.repo, 'push', 'origin', 'main')
            self.assertEqual(self.levels()['ui']['reverts'], 0)
            self.write(self.repo, 'ui/src/App.tsx', 'worker\n')
            commit = self.commit(self.repo, 'Later change')
            self.git(self.repo, 'revert', '--no-edit', commit)
            self.git(self.repo, 'push', 'origin', 'main')
        self.assertEqual(self.levels()['ui']['reverts'], 1)

    def test_a_revert_of_a_commit_outside_the_areas_changes_nothing(self):
        self.write(self.repo, 'server/server.py', 'print(5)\n')
        commit = self.commit(self.repo, 'Server')
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        self.revert(commit)
        self.assertEqual({area['rung'] for area in self.levels().values()}, {1})


class Watcher(Base):
    def test_each_approval_is_queued_once_with_the_reviewer_agent(self):
        entries = [{'id': '%1', 'name': 'Worker', 'agent': 'claude'}, {'id': '%2', 'agent': 'codex'}]
        approvals = {'%1': {'%2': {'commit': 'a' * 40, 'at': 5}}, '%9': {'%2': {'commit': 'b' * 40, 'at': 5}},
                     '%2': {'%1': {'commit': 'short', 'at': 5}}}
        def current():
            return approvals

        jobs = auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False, current=current)
        self.assertEqual(jobs, [{'folder': str(self.work), 'commit': 'a' * 40, 'approved_at': 5, 'worker': '%1',
                                 'name': 'Worker', 'reviewer': '%2', 'reviewer_agent': 'codex', 'current': current}])
        self.assertEqual(auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False), [])
        approvals['%1']['%2']['at'] = 6
        renewed = auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False, current=current)
        self.assertEqual(renewed, [dict(jobs[0], approved_at=6)])
        self.assertEqual(auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False), [])
        approvals['%1']['%2'] = {'commit': 'c' * 40, 'at': 6}
        self.assertEqual(len(auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False)), 1)


class Http(Base):
    def test_state_lists_modes_decisions_and_trust(self):
        auto_merge.process(self.job(self.change()))
        handler = Handler()
        auto_merge.get(handler)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.body['mode'], 'observe')
        self.assertEqual([item['mode'] for item in handler.body['modes']], ['off', 'observe', 'enforce'])
        self.assertEqual(handler.body['decisions'][0]['outcome'], 'would_merge')
        self.assertEqual([area['name'] for area in handler.body['trust'][0]['areas']], ['tests', 'ui'])
        self.assertEqual(handler.body['trust'][0]['areas'][0]['need'], auto_merge.CLEAN_MERGES)

    def test_a_repository_tether_cannot_read_is_not_listed_and_not_asked_again(self):
        other = self.root / 'private'
        self.git(self.root, 'init', '-q', '-b', 'main', str(other))
        self.git(other, 'remote', 'add', 'origin', str(self.root / 'no-such-remote.git'))
        self.git(other, 'commit', '-q', '--allow-empty', '-m', 'First')
        auto_merge._target_errors.clear()
        with mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
            for _ in range(3):
                listed = auto_merge.state([str(other), str(self.repo)])['trust']
                self.assertEqual([item['repo'] for item in listed], ['project'])
        asked = [call.args[0] for call in run.call_args_list if 'ls-remote' in call.args[0] and str(self.root / 'no-such-remote.git') in call.args[0]]
        self.assertEqual(len(asked), 1)
        # With a decision in the log the repository is listed, with the reason.
        auto_merge.record({'root': str(other), 'head': 'a' * 40, 'mode': 'observe', 'outcome': 'wait', 'checks': []})
        listed = auto_merge.state([str(self.repo)])['trust']
        self.assertEqual([item['repo'] for item in listed], ['private', 'project'])
        self.assertIn('ls-remote', listed[0]['error'])
        auto_merge._target_errors.clear()

    def test_a_repository_without_rules_is_listed_only_when_it_has_decisions(self):
        other = self.root / 'other'
        remote = self.root / 'other.git'
        self.git(self.root, 'init', '-q', '--bare', '-b', 'main', str(remote))
        self.git(self.root, 'init', '-q', '-b', 'main', str(other))
        self.git(other, 'commit', '--allow-empty', '-m', 'No rules')
        self.git(other, 'remote', 'add', 'origin', str(remote))
        self.git(other, 'push', 'origin', 'main')
        self.assertEqual(auto_merge.state([str(other)])['trust'], [])
        self.assertEqual(len(auto_merge.state([str(self.repo)])['trust']), 1)

    def test_settings_fetches_the_push_target_then_reuses_it_for_five_minutes(self):
        root = str(self.repo)
        scratch = auto_merge._scratch(root)
        with mock.patch.object(auto_merge, '_fetch_target', wraps=auto_merge._fetch_target) as fetch, \
                mock.patch.object(auto_merge.time, 'monotonic', return_value=100) as now:
            self.assertEqual(auto_merge.state([root])['trust'][0]['areas'][1]['label'], 'UI')
            changed_rules = json.loads(json.dumps(RULES))
            changed_rules['areas'][1]['label'] = 'Browser'
            self.write(self.repo, auto_merge.RULES_FILE, json.dumps(changed_rules))
            self.commit(self.repo, 'Rename UI area')
            self.git(self.repo, 'push', 'origin', 'main')
            now.return_value = 399
            self.assertEqual(auto_merge.state([root])['trust'][0]['areas'][1]['label'], 'UI')
            fetch.assert_called_once_with(root, scratch)
            now.return_value = 400
            self.assertEqual(auto_merge.state([root])['trust'][0]['areas'][1]['label'], 'Browser')
            self.assertEqual(fetch.call_count, 2)

            changed_rules['areas'][1]['label'] = 'Frontend'
            self.write(self.repo, auto_merge.RULES_FILE, json.dumps(changed_rules))
            self.commit(self.repo, 'Rename UI area again')
            self.git(self.repo, 'push', 'origin', 'main')
            decision, plan = auto_merge.evaluate(self.work, self.change())
            self.assertEqual(self.failed(decision), [])
            self.assertEqual(plan['target_sha'], self.origin_main())
            self.assertEqual(auto_merge.state([root])['trust'][0]['areas'][1]['label'], 'Frontend')
            self.assertEqual(fetch.call_args_list, [mock.call(root, scratch)] * 3)

    def test_settings_shows_a_target_error_only_for_a_repository_with_decisions(self):
        root = str(self.repo)
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.root / 'gone.git'))
        self.assertEqual(auto_merge.state([root])['trust'], [])
        auto_merge.process(self.job(self.change()))
        trust = auto_merge.state([root])['trust']
        self.assertEqual(len(trust), 1)
        self.assertEqual(trust[0]['root'], root)
        self.assertIn('git ls-remote failed', trust[0]['error'])

    def test_post_sets_the_mode_and_refuses_agents_and_bad_values(self):
        handler = Handler({'mode': 'enforce'})
        with mock.patch.object(auto_merge.approval, 'require') as require:
            auto_merge.post(handler)
        require.assert_called_once_with(handler, 'auto-merge', {'mode': 'enforce'}, None)
        self.assertEqual((handler.status, handler.body['mode']), (200, 'enforce'))
        self.assertGreater(handler.body['since'], 0)
        handler = Handler({'mode': 'observe'}, {'X-Tether-Hook': 'credential'})
        auto_merge.post(handler)
        self.assertEqual(handler.status, 403)
        self.assertEqual(auto_merge.read_mode(), 'enforce')
        for body in ({'mode': 'on'}, {}, [1], {'restore': {'root': str(self.repo), 'area': 'ui'}}, {'restore': 1}):
            handler = Handler(body)
            with mock.patch.object(auto_merge.approval, 'require'):
                auto_merge.post(handler)
            self.assertIn(handler.status, (400, 409), body)
        self.assertEqual(auto_merge.read_mode(), 'enforce')

    def test_post_requires_approval_for_enforce_and_restore_only(self):
        self.write(self.repo, 'ui/src/App.tsx', 'worker\n')
        commit = self.commit(self.repo, 'Agent change')
        self.git(self.repo, 'revert', '--no-edit', commit)
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.repo, 'fetch', 'origin')
        roots = [str(self.repo)]
        for payload in ({'mode': 'enforce'}, {'restore': {'root': str(self.repo), 'area': 'ui'}}):
            with self.subTest(payload=payload):
                handler = Handler(payload)
                error = auto_merge.approval.ApprovalError(403, auto_merge.approval.APPROVE)
                with mock.patch.object(auto_merge.approval, 'require', side_effect=error) as require:
                    auto_merge.post(handler, roots)
                require.assert_called_once_with(handler, 'auto-merge', payload, None)
                self.assertEqual((handler.status, handler.body['approval']), (403, 'required'))
                if 'mode' in payload:
                    self.assertEqual(auto_merge.read_mode(), 'observe')
                else:
                    self.assertEqual(auto_merge.state(roots)['trust'][0]['areas'][1]['rung'], 0)
                assertion = {'credential': 'x' * 6000}
                handler = Handler({**payload, 'approval': assertion})
                with mock.patch.object(auto_merge.approval, 'require') as require:
                    auto_merge.post(handler, roots)
                require.assert_called_once_with(handler, 'auto-merge', payload, assertion)
                self.assertEqual(handler.status, 200)
                if 'mode' in payload:
                    self.assertEqual(handler.body['mode'], 'enforce')
                else:
                    self.assertEqual(handler.body['trust'][0]['areas'][1]['rung'], auto_merge.START_RUNG)
        for mode in ('off', 'observe'):
            with mock.patch.object(auto_merge.approval, 'require') as require:
                handler = Handler({'mode': mode})
                auto_merge.post(handler)
                self.assertEqual((handler.status, handler.body['mode']), (200, mode))
            require.assert_not_called()


class EnforceUnavailable(Base):
    enforce_available = False
    explanation = ('Enforce is not available yet. Auto-merge needs its own identity first, so that agents on '
                   'this account cannot change its trust or approve it.')

    def test_enforce_is_hidden_and_refused_before_passkey_validation(self):
        self.assertIs(auto_merge.ENFORCE_AVAILABLE, False)
        for mode in ('off', 'observe'):
            with self.subTest(mode=mode):
                auto_merge.set_mode(mode)
                setting = auto_merge.mode_path().read_bytes()
                with self.assertRaises(auto_merge.AutoMergeError) as error:
                    auto_merge.set_mode('enforce')
                self.assertEqual(error.exception.status, 409)
                self.assertEqual(str(error.exception), self.explanation)
                self.assertEqual(auto_merge.mode_path().read_bytes(), setting)
                handler = Handler({'mode': 'enforce', 'approval': {'id': mode}})
                with mock.patch.object(auto_merge.approval, 'require') as require:
                    auto_merge.post(handler)
                require.assert_not_called()
                self.assertEqual((handler.status, handler.body), (409, {'error': self.explanation}))
                state = auto_merge.state()
                self.assertEqual(state['mode'], mode)
                self.assertIs(state['enforce_available'], False)
                self.assertEqual([item['mode'] for item in state['modes']], ['off', 'observe'])

    def test_enforce_file_never_reaches_merge_even_with_a_matching_grant(self):
        head = self.change()
        before = self.origin_main()
        path = auto_merge.mode_path()
        path.parent.mkdir()
        for grant in (None, 1):
            with self.subTest(grant=grant):
                path.write_text(json.dumps({'mode': 'enforce', 'since': 1}))
                with mock.patch.object(auto_merge, '_grant', grant), \
                        mock.patch.object(auto_merge, 'merge', wraps=auto_merge.merge) as merge, \
                        mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
                    self.assertEqual(auto_merge.read_setting(), {'mode': 'observe', 'since': 0, 'paused': False})
                    self.assertEqual(auto_merge.process(self.job(head))['outcome'], 'would_merge')
                merge.assert_not_called()
                self.assertFalse({'commit-tree', 'push'} & {call.args[0][3] for call in run.call_args_list})
                self.assertEqual(self.origin_main(), before)


class RealChecklist(Base):
    """The real scripts/checklist.py (B209), read from the main branch of a disposable repository."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(auto_merge, 'checklist_rows', CHECKLIST_ROWS))
        self.write(self.repo, 'docs/reliability-checklist.md',
                   '# Reliability checklist\n\n## R1: Test helpers keep their socket\n- Paths: `^server/test_.*\\.py$`\n'
                   '- Text: `kill-server`\n- Check: Use a private socket.\n- Incident: B1, commit 0000000\n')
        self.commit(self.repo, 'Checklist')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '-q', '--ff-only', 'main')

    def test_a_diff_that_hits_no_row_is_clear_and_a_hit_names_the_row(self):
        head = self.change()
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        head = self.change('server/test_tmux.py', 'run("tmux kill-server")\n')
        decision = auto_merge.evaluate(self.work, head)[0]
        self.assertEqual(self.failed(decision), ['checklist'])
        self.assertIn('R1 Test helpers keep their socket', [item for item in decision['checks'] if item['key'] == 'checklist'][0]['text'])
        self.git(self.work, 'reset', '-q', '--hard', 'main')
        head = self.change('server/test_tmux.py', 'run("tmux list-sessions")\n')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head)[0]), [])

    def test_a_branch_cannot_remove_the_row_that_applies_to_it(self):
        self.write(self.work, 'server/test_tmux.py', 'run("tmux kill-server")\n')
        self.git(self.work, 'rm', '-q', 'docs/reliability-checklist.md')
        head = self.commit(self.work, 'Drop the checklist')
        self.assertIn('checklist', self.failed(auto_merge.evaluate(self.work, head)[0]))

    def test_main_without_a_checklist_counts_as_a_hit(self):
        self.git(self.repo, 'rm', '-q', 'docs/reliability-checklist.md')
        self.commit(self.repo, 'No checklist')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.work, 'merge', '-q', '--ff-only', 'main')
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, self.change())[0]), ['checklist'])


class Checklist(unittest.TestCase):
    """The contract of scripts/checklist.py (B209): only status "clear" with exit 0 means no row is hit."""

    def answer(self, output, code, script=True, base='a' * 40):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'server').mkdir()
        (root / 'scripts').mkdir()
        if script:
            (root / 'scripts' / 'checklist.py').write_text(
                'import json, os, sys\n'
                f'json.dump(sys.argv[1:], open({str(root / "argv")!r}, "w"))\n'
                'env = {key: val for key, val in os.environ.items() if key.startswith(("GIT_CONFIG_", "GIT_ATTR_", '
                '"GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS"))}\n'
                f'json.dump(env, open({str(root / "env")!r}, "w"))\n'
                f'sys.stdout.write({output!r})\nsys.exit({code})\n')
        with mock.patch.object(auto_merge, 'HERE', root / 'server'):
            rows = CHECKLIST_ROWS('/repo', base, 'b' * 40)
        self.argv = json.loads((root / 'argv').read_text()) if script else None
        self.env = json.loads((root / 'env').read_text()) if script else None
        return rows

    def test_clear_means_no_rows_and_the_call_names_main_and_the_head(self):
        self.assertEqual(self.answer('{"status": "clear", "rows": [], "paths": ["ui/src/a.ts"]}', 0), [])
        self.assertEqual(self.argv, ['--repo', '/repo', '--base', 'a' * 40, '--head', 'b' * 40, '--json'])

    def test_hit_lists_the_rows(self):
        rows = self.answer('{"status": "hit", "rows": [{"id": "R3", "title": "tmux kill-server"}, {"id": "R7"}]}', 1)
        self.assertEqual(rows, ['R3 tmux kill-server', 'R7'])

    def test_the_checklist_uses_controlled_git_settings_and_attributes_from_main(self):
        env = {'GIT_CONFIG_GLOBAL': '/srv/global-config', 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.fsmonitor',
               'GIT_CONFIG_VALUE_0': 'true', 'GIT_EXTERNAL_DIFF': '/srv/diff', 'GIT_DIFF_OPTS': '--unified=99',
               'GIT_ATTR_SOURCE': 'HEAD'}
        with mock.patch.dict(os.environ, env):
            for base in ('a' * 40, 'c' * 40):
                with self.subTest(base=base):
                    self.assertEqual(self.answer('{"status": "clear", "rows": []}', 0, base=base), [])
                    self.assertEqual(git_settings(self.env), GIT_SETTINGS)
                    self.assertEqual(self.env['GIT_CONFIG_GLOBAL'], os.devnull)
                    self.assertEqual(self.env['GIT_CONFIG_NOSYSTEM'], '1')
                    self.assertEqual(self.env['GIT_ATTR_NOSYSTEM'], '1')
                    self.assertEqual(self.env['GIT_ATTR_SOURCE'], base)
                    self.assertNotIn('GIT_EXTERNAL_DIFF', self.env)
                    self.assertNotIn('GIT_DIFF_OPTS', self.env)

    def test_every_other_answer_counts_as_a_hit(self):
        for output, code in (('{"status": "absent", "rows": [], "reason": "no checklist"}', 2),
                             ('{"status": "unchecked", "rows": []}', 2), ('{"status": "clear", "rows": []}', 1),
                             ('{"status": "clear", "rows": [{"id": "R1"}]}', 0), ('{"status": "hit", "rows": []}', 1),
                             ('{"status": "clear"}', 0), ('[]', 0), ('not json', 0), ('', 0),
                             ('{"status": "hit", "rows": [{"id": "R1"}]}', 0)):
            self.assertIsNone(self.answer(output, code), (output, code))
        self.assertIsNone(self.answer('', 0, script=False))


if __name__ == '__main__':
    unittest.main()
