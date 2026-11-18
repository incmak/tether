"""Auto-merge decisions and the trust ladder, on disposable git repositories. Nothing here touches tmux or a real remote."""
import io
import json
import os
from pathlib import Path
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

CHECKLIST_ROWS = auto_merge.checklist_rows  # the real function: Base replaces it with a fixed answer


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
        auto_merge._ladder_cache.clear()
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
                'worker': '%1', 'name': 'Worker', 'reviewer': '%2', 'reviewer_agent': 'codex'}

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
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head, fetch=False)[0]), [])
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['scope'])
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
        self.assertEqual(self.failed(auto_merge.evaluate(self.work, head, fetch=False)[0]), [])
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['git'])

    def test_a_push_url_cannot_resolve_as_a_remote_name_or_be_rewritten_again(self):
        self.git(self.repo, 'remote', 'add', 'other', str(self.origin))
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', 'other')
        head = self.change()
        refs = self.refs()
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['git'])
        self.assertEqual(self.refs(), refs)
        self.git(self.repo, 'remote', 'set-url', '--push', 'origin', str(self.origin))
        self.git(self.repo, 'config', f'url.{self.root / "other.git"}.pushInsteadOf', str(self.origin))
        self.assertEqual(self.failed(auto_merge.process(self.job(head))), ['git'])
        self.assertEqual(self.refs(), refs)

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
            self.assertEqual(self.failed(decision), ['scope'])
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

        def run(argv, **kwargs):
            if argv[0] == 'git':
                command = argv[3:]
                seen.append(command)
                if command[0] in ('fetch', 'ls-remote'):
                    self.assertEqual(kwargs['env']['GIT_CONFIG_GLOBAL'], global_config)
                else:
                    self.assertEqual(kwargs['env']['GIT_CONFIG_GLOBAL'], os.devnull)
                    self.assertEqual(kwargs['env']['GIT_CONFIG_NOSYSTEM'], '1')
            return real(argv, **kwargs)

        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=global_config), mock.patch.object(auto_merge, 'RUN', run):
            decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'would_merge')
        self.assertEqual(self.origin_main(), before)
        self.assertEqual(self.refs(), refs)
        self.assertFalse(fetch_head.exists())
        self.assertIn(['fetch', '--no-tags', '--no-write-fetch-head', str(self.origin), 'refs/heads/main'], seen)
        self.assertFalse(any(command[0] == 'fetch' and 'origin' in command[1:] for command in seen))
        self.assertFalse(any(command[0] == 'diff' for command in seen))
        self.assertFalse({'push', 'commit-tree', 'update-ref', 'merge', 'commit', 'reset', 'checkout', 'rebase',
                          'stash', 'branch'} & {command[0] for command in seen}, seen)
        self.assertEqual([item['outcome'] for item in auto_merge.records()], ['would_merge'])

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
            self.assertFalse([item for item in args if item.startswith(('-', '+'))], args)

    def test_leaving_enforce_before_the_push_stops_the_merge(self):
        self.enforce()
        head = self.change()
        before = self.origin_main()
        decision, plan = auto_merge.evaluate(self.work, head)
        auto_merge.set_mode('observe')
        with self.assertRaises(auto_merge.GitError):
            auto_merge.merge(plan, 'codex', self.job(head)['approved_at'])
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
                    auto_merge.merge(plan, 'codex', at)
        self.assertFalse(any(call.args[0][3] == 'push' for call in run.call_args_list))
        self.assertEqual(self.origin_main(), before)

    def test_push_refuses_a_tracking_plan_or_a_commit_with_the_wrong_tree(self):
        self.enforce()
        head = self.change()
        before = self.origin_main()
        _, offline = auto_merge.evaluate(self.work, head, fetch=False)
        _, online = auto_merge.evaluate(self.work, head)
        at = self.job(head)['approved_at']
        wrong_tree = self.git(self.work, 'rev-parse', 'main^{tree}')

        def run(argv, **kwargs):
            self.assertNotEqual(argv[3], 'push')
            if argv[3] == 'commit-tree':
                argv = [*argv[:4], wrong_tree, *argv[5:]]
            return subprocess.run(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', side_effect=run):
            with self.assertRaisesRegex(auto_merge.GitError, 'needs the current main branch'):
                auto_merge.merge(offline, 'codex', at)
            with self.assertRaisesRegex(auto_merge.GitError, 'does not contain the planned tree'):
                auto_merge.merge(online, 'codex', at)
        self.assertEqual(self.origin_main(), before)

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
        self.git(self.repo, 'fetch', '-q', 'origin')
        target = self.git(self.repo, 'rev-parse', 'origin/main')
        return auto_merge.ladder(str(self.repo), auto_merge.rules_at(self.repo, target), target)

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
            # An older tracking tip must neither replay the revert nor replace the snapshot after restart.
            auto_merge._ladder_cache.clear()
            self.git(self.repo, 'update-ref', 'refs/remotes/origin/main', revert)
            self.assertEqual(auto_merge.state([str(self.repo)])['trust'][0]['areas'][1]['reverts'], 1)
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
        jobs = auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False)
        self.assertEqual(jobs, [{'folder': str(self.work), 'commit': 'a' * 40, 'approved_at': 5, 'worker': '%1',
                                 'name': 'Worker', 'reviewer': '%2', 'reviewer_agent': 'codex'}])
        self.assertEqual(auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False), [])
        approvals['%1']['%2']['at'] = 6
        renewed = auto_merge.observe(entries, approvals, lambda entry: str(self.work), start=False)
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
        self.git(self.root, 'init', '-q', '-b', 'main', str(other))
        self.assertEqual(auto_merge.state([str(other)])['trust'], [])
        self.assertEqual(len(auto_merge.state([str(self.repo)])['trust']), 1)

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


class Checklist(unittest.TestCase):
    """The contract of scripts/checklist.py (B209): only status "clear" with exit 0 means no row is hit."""

    def answer(self, output, code, script=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'server').mkdir()
        (root / 'scripts').mkdir()
        if script:
            (root / 'scripts' / 'checklist.py').write_text(
                'import json, sys\n'
                f'json.dump(sys.argv[1:], open({str(root / "argv")!r}, "w"))\n'
                f'sys.stdout.write({output!r})\nsys.exit({code})\n')
        with mock.patch.object(auto_merge, 'HERE', root / 'server'):
            rows = CHECKLIST_ROWS('/repo', 'a' * 40, 'b' * 40)
        self.argv = json.loads((root / 'argv').read_text()) if script else None
        return rows

    def test_clear_means_no_rows_and_the_call_names_main_and_the_head(self):
        self.assertEqual(self.answer('{"status": "clear", "rows": [], "paths": ["ui/src/a.ts"]}', 0), [])
        self.assertEqual(self.argv, ['--repo', '/repo', '--base', 'a' * 40, '--head', 'b' * 40, '--json'])

    def test_hit_lists_the_rows(self):
        rows = self.answer('{"status": "hit", "rows": [{"id": "R3", "title": "tmux kill-server"}, {"id": "R7"}]}', 1)
        self.assertEqual(rows, ['R3 tmux kill-server', 'R7'])

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
