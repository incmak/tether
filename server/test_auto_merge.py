"""Auto-merge decisions and the trust ladder, on disposable git repositories. Nothing here touches tmux or a real remote."""
import hashlib
import io
import json
import os
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
        self.assertFalse(auto_merge._dirty(self.work, scratch, head))
        for name in ('ignored.txt', 'ignored-dir/file'):
            self.write(self.work, name, 'ignored\n')
            self.assertFalse(auto_merge._dirty(self.work, scratch, head))
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
                self.assertTrue(auto_merge._dirty(self.work, scratch, head))
                decision, plan = auto_merge.evaluate(app.parent, head)
                self.assertIn('branch', self.failed(decision))
                self.assertIsNone(plan)
                self.git(self.work, 'reset', '--hard', head)
                self.git(self.work, 'clean', '-fd')
        self.assertFalse(auto_merge._dirty(self.work, scratch, head))

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
            self.assertEqual(self.failed(decision), ['branch', 'scope'])
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
        Path(global_config).write_text('[credential]\n\thelper = cache\n\thelper = store --file /srv/credentials\n')

        def run(argv, **kwargs):
            if argv[:2] == ['git', 'config']:
                self.assertEqual(argv[-3:], ['--null', '--get-all', 'credential.helper'])
                self.assertNotIn('env', kwargs)
                helping.append(1)
            elif argv[0] == 'git':
                command = argv[3:]
                seen.append(command)
                env = kwargs['env']
                self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
                self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
                self.assertEqual(env['GIT_ATTR_NOSYSTEM'], '1')
                want = GIT_SETTINGS
                if command[0] in ('fetch', 'ls-remote'):
                    want = GIT_SETTINGS + [('credential.helper', helper)
                                           for helper in ('', 'cache', 'store --file /srv/credentials')]
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
        scratch = paths.CACHE / 'auto-merge-git' / (hashlib.sha256(os.fsencode(self.repo)).hexdigest()[:16] + '.git')
        local_commands = {'rev-parse', 'symbolic-ref', 'config', 'ls-files'}
        object_commands = {'ls-remote', 'fetch', 'cat-file', 'ls-tree', 'merge-base', 'merge-tree', 'log', 'show'}
        for folder in (self.work, self.work / 'server'):
            with self.subTest(folder=folder), mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
                self.assertEqual(auto_merge.process({**self.job(head), 'folder': str(folder)})['outcome'], 'would_merge')
                seen = set()
                for call in run.call_args_list:
                    argv = call.args[0]
                    if argv[:2] != ['git', '-C']:
                        self.assertEqual(argv, ['git', 'config', '--global', '--null', '--get-all', 'credential.helper'])
                        continue
                    directory, command = Path(argv[2]), argv[3]
                    if directory in (self.work, folder, self.repo):
                        self.assertIn(command, local_commands, argv)
                        self.assertNotIn('GIT_ATTR_SOURCE', call.kwargs['env'])
                    else:
                        self.assertEqual(directory, scratch, argv)
                        self.assertEqual(call.kwargs['env']['GIT_ATTR_SOURCE'], auto_merge.EMPTY_TREE)
                        seen.add(command)
                self.assertTrue(object_commands <= seen)
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
        for args in pushes:
            flags = [item for item in args if item.startswith('-')]
            self.assertEqual(len(flags), 1, args)
            self.assertRegex(flags[0], r'^--force-with-lease=main:[0-9a-f]{40}$')
            self.assertFalse(any(item.startswith('+') for item in args), args)

    def test_lease_refuses_a_changed_behind_or_deleted_main(self):
        self.enforce()
        behind = self.origin_main()
        head = self.change()
        tree = self.git(self.origin, 'rev-parse', 'main^{tree}')
        checked = self.git(self.origin, 'commit-tree', tree, '-p', behind, '-m', 'Checked main')
        self.git(self.origin, 'update-ref', 'refs/heads/main', checked)
        newer = self.git(self.origin, 'commit-tree', tree, '-p', checked, '-m', 'Newer main')
        unrelated = self.git(self.origin, 'commit-tree', tree, '-m', 'Unrelated main')
        scratch = auto_merge._scratch(self.repo)
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
                        self.assertRaisesRegex(auto_merge.GitError, 'lease-guarded fast-forward failed'):
                    auto_merge.merge(plan, 'codex', self.job(head))
                pushes = [call.args[0] for call in run.call_args_list if call.args[0][3] == 'push']
                self.assertEqual(len(pushes), 1)
                self.assertEqual(pushes[0][:6], ['git', '-C', str(scratch), 'push',
                                                 f'--force-with-lease=main:{checked}', str(self.origin)])
                self.assertEqual(self.git(self.origin, 'for-each-ref', '--format=%(objectname)', 'refs/heads/main'),
                                 remote or '')
                self.git(self.origin, 'update-ref', 'refs/heads/main', checked)

    def test_lease_never_calls_push_for_an_unchecked_first_parent(self):
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
            for altered, message in ((unrelated, 'does not descend'), (head, 'as its first parent')):
                with self.subTest(altered=altered), self.assertRaisesRegex(auto_merge.GitError, message):
                    auto_merge.merge({**plan, 'target_sha': altered}, 'codex', self.job(head))
        self.assertNotIn('push', seen)
        self.assertEqual(self.origin_main(), checked)

    def test_lease_happy_path_uses_exact_push_argv(self):
        self.enforce()
        scratch = auto_merge._scratch(self.repo)
        for number in (2, 3):
            with self.subTest(number=number):
                head = self.change(text=f'export const app = {number}\n')
                checked = self.origin_main()
                with mock.patch.object(auto_merge, 'RUN', wraps=subprocess.run) as run:
                    decision = auto_merge.process(self.job(head))
                self.assertEqual(decision['outcome'], 'merged', decision)
                pushes = [call.args[0] for call in run.call_args_list if call.args[0][3] == 'push']
                self.assertEqual(pushes, [['git', '-C', str(scratch), 'push', f'--force-with-lease=main:{checked}',
                                          str(self.origin), f"{decision['merge']}:refs/heads/main"]])
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

    def test_the_merge_runs_when_main_moved_after_the_first_decision(self):
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

        with mock.patch.object(auto_merge, 'evaluate', evaluate):
            decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'merged')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.git(self.origin, 'show', 'main:server/server.py'), 'print(7)')


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

    def test_settings_shows_target_errors_with_or_without_decisions(self):
        root = str(self.repo)
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.root / 'gone.git'))
        for known in (False, True):
            with self.subTest(known=known):
                if known:
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
