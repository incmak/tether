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

    def job(self, head, at=2 ** 40):
        return {'folder': str(self.work), 'commit': head, 'approved_at': at,
                'worker': '%1', 'name': 'Worker', 'reviewer': '%2', 'reviewer_agent': 'codex'}

    def origin_main(self):
        return self.git(self.origin, 'rev-parse', 'main')

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

    def test_a_failed_fetch_and_a_folder_outside_git_fail_closed(self):
        head = self.change()
        self.git(self.repo, 'remote', 'set-url', 'origin', str(self.root / 'gone.git'))
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
                     '["enforce"]', '{"mode": "enforce", "since": true}'):
            auto_merge.mode_path().write_text(text)
            self.assertEqual(auto_merge.read_mode(), 'observe', text)
        auto_merge.set_mode('enforce')
        self.assertEqual(auto_merge.read_mode(), 'enforce')
        with self.assertRaises(auto_merge.AutoMergeError):
            auto_merge.set_mode('on')

    def test_observe_records_would_merge_and_runs_no_git_write(self):
        head = self.change()
        before = self.origin_main()
        real = subprocess.run
        seen = []

        def run(argv, **kwargs):
            seen.append(argv[3] if argv[0] == 'git' else argv[0])
            return real(argv, **kwargs)

        with mock.patch.object(auto_merge, 'RUN', run):
            decision = auto_merge.process(self.job(head))
        self.assertEqual(decision['outcome'], 'would_merge')
        self.assertEqual(self.origin_main(), before)
        self.assertFalse({'push', 'commit-tree', 'update-ref', 'merge', 'commit', 'reset', 'checkout', 'rebase',
                          'stash', 'branch'} & set(seen), seen)
        self.assertEqual([item['outcome'] for item in auto_merge.records()], ['would_merge'])

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
        for bad in (None, 'now'):
            self.assertEqual(auto_merge.process(self.job(head, at=bad))['outcome'], 'would_merge')
        self.assertEqual(self.origin_main(), before)

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
            auto_merge.merge(plan, 'codex')
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
        with mock.patch.object(auto_merge.time, 'time', lambda: 2 ** 40):
            auto_merge.restore(str(self.repo), 'ui')
        self.assertEqual(self.levels()['ui']['rung'], 1)

    def test_a_revert_of_an_agent_commit_shrinks_and_a_revert_of_a_revert_does_not(self):
        self.write(self.repo, 'server/test_a.py', 'import unittest\n')
        commit = self.commit(self.repo, 'Agent test')
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        revert = self.revert(commit)
        self.assertEqual((self.levels()['tests']['rung'], self.levels()['ui']['rung']), (0, 1))
        self.revert(revert)
        self.assertEqual(self.levels()['tests']['reverts'], 1)

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
        auto_merge.post(handler)
        self.assertEqual((handler.status, handler.body['mode']), (200, 'enforce'))
        self.assertGreater(handler.body['since'], 0)
        handler = Handler({'mode': 'observe'}, {'X-Tether-Hook': 'credential'})
        auto_merge.post(handler)
        self.assertEqual(handler.status, 403)
        self.assertEqual(auto_merge.read_mode(), 'enforce')
        for body in ({'mode': 'on'}, {}, [1], {'restore': {'root': str(self.repo), 'area': 'ui'}}, {'restore': 1}):
            handler = Handler(body)
            auto_merge.post(handler)
            self.assertIn(handler.status, (400, 409), body)
        self.assertEqual(auto_merge.read_mode(), 'enforce')


if __name__ == '__main__':
    unittest.main()
