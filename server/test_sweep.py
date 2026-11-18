"""sweep.py: the 30-minute worktree sweep (B210). Real git in a temporary folder; gh and the clock are stand-ins."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import factory
import paths
import sweep

ENV = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 'T',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def git(folder, *args):
    return subprocess.run(['git', '-C', str(folder), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def commit(folder, name):
    (Path(folder) / name).write_text(name)
    git(folder, 'add', name)
    git(folder, 'commit', '-q', '-m', name)


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        patch = mock.patch.object(paths, 'CACHE', self.root / 'cache')
        patch.start()
        self.addCleanup(patch.stop)
        self.main = self.root / 'repo'
        self.main.mkdir()
        git(self.main, 'init', '-q', '-b', 'main')
        commit(self.main, 'one')
        self.tree = self.root / 'wt'
        git(self.main, 'worktree', 'add', '-q', '-b', 'feature', str(self.tree))
        self.transcript = self.root / 'chat.jsonl'
        self.transcript.write_text('{}\n')
        self.now = self.transcript.stat().st_mtime

    def entry(self, pane='%7', **extra):
        return {'id': pane, 'agent': 'claude', 'pane_name': 'B1 Worker', 'transcript': str(self.transcript), **extra}

    def read(self, entry=None, folder=None, working=False, now=None, checks=lambda *_: None):
        return sweep.read_tab(entry or self.entry(), str(folder or self.tree), working, now=now or self.now, checks=checks)


class ReadTests(Case):
    def test_a_fresh_worktree_and_the_main_checkout_have_no_findings(self):
        self.assertEqual(self.read(), [])
        commit(self.main, 'two')
        self.assertEqual(self.read(folder=self.main), [])  # the main checkout is not a worktree tab

    def test_behind_counts_the_commits_main_has(self):
        commit(self.main, 'two')
        commit(self.main, 'three')
        commit(self.tree, 'mine')  # ahead does not matter
        found = self.read()
        self.assertEqual([(line['kind'], line['pane'], line['branch']) for line in found], [('behind', '%7', 'feature')])
        self.assertEqual(found[0]['text'], '2 commits behind main. Rebase by hand.')
        commit(self.root / 'wt', 'x')
        git(self.tree, 'rebase', '-q', 'main')
        self.assertEqual(self.read(), [])

    def test_behind_uses_the_larger_of_local_and_origin(self):
        commit(self.main, 'two')
        git(self.main, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        git(self.main, 'reset', '-q', '--hard', 'HEAD~1')  # local main is older than origin/main
        self.assertEqual(self.read()[0]['text'], '1 commit behind main. Rebase by hand.')

    def test_master_and_origin_head_name_the_main_branch(self):
        git(self.main, 'branch', '-m', 'main', 'trunk')
        self.assertIsNone(sweep.main_branch(str(self.tree)))
        self.assertEqual(self.read(), [])  # no main branch: no guess
        git(self.main, 'update-ref', 'refs/remotes/origin/trunk', 'HEAD')
        git(self.main, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        self.assertEqual(sweep.main_branch(str(self.tree)), 'trunk')
        commit(self.main, 'two')
        self.assertEqual(self.read()[0]['text'], '1 commit behind trunk. Rebase by hand.')

    def test_idle_needs_two_quiet_hours_and_a_tab_that_does_not_work(self):
        self.assertEqual(self.read(now=self.now + sweep.IDLE - 5), [])
        late = self.now + 3 * 3600 + 60
        self.assertEqual([(line['kind'], line['text']) for line in self.read(now=late)], [('idle', 'Idle for 3 h.')])
        self.assertEqual(self.read(now=late, working=True), [])
        self.assertEqual(self.read(self.entry(transcript=None), now=late), [])  # no transcript: not known, not idle
        self.assertEqual(self.read(now=self.now + 3 * 86400)[0]['text'], 'Idle for 3 days.')

    def test_failed_checks_name_the_commit(self):
        head = git(self.tree, 'rev-parse', 'HEAD')
        seen = []

        def checks(folder, sha, run):
            seen.append((folder, sha))
            return ['lint', 'test']
        found = self.read(checks=checks)
        self.assertEqual(seen, [(str(self.tree), head)])
        self.assertEqual(found[0]['text'], f'2 checks fail on {head[:7]}: lint, test.')
        self.assertEqual(found[0]['key'], f'%7:checks:{head[:12]}')
        self.assertEqual(self.read(checks=lambda *_: []), [])

    def test_gh_answers(self):
        def gh(stdout='', code=0, error=None):
            def run(command, **options):
                if error:
                    raise error
                self.assertEqual(command[:4], ['gh', 'api', '--method', 'GET'])
                self.assertEqual(command[4], 'repos/{owner}/{repo}/commits/abc/check-runs?per_page=100')
                self.assertEqual(options['cwd'], '/w')
                return SimpleNamespace(returncode=code, stdout=stdout, stderr='')
            return run
        runs = {'check_runs': [{'name': 'test', 'status': 'completed', 'conclusion': 'failure'},
                               {'name': 'lint', 'status': 'completed', 'conclusion': 'success'},
                               {'name': 'slow', 'status': 'in_progress', 'conclusion': None},
                               {'name': 'ui', 'status': 'completed', 'conclusion': 'timed_out'},
                               {'name': 'ui', 'status': 'completed', 'conclusion': 'timed_out'},
                               {'name': 'skip', 'status': 'completed', 'conclusion': 'cancelled'}]}
        self.assertEqual(sweep.failed_checks('/w', 'abc', gh(json.dumps(runs))), ['test', 'ui'])
        self.assertEqual(sweep.failed_checks('/w', 'abc', gh(json.dumps({'check_runs': []}))), [])
        for bad in (gh(code=1), gh('not json'), gh('{}'), gh('{"check_runs": 3}'), gh(error=FileNotFoundError()),
                    gh(error=subprocess.TimeoutExpired('gh', 1))):
            self.assertIsNone(sweep.failed_checks('/w', 'abc', bad))  # not read is never "passing" or "failing"


class SweepTests(Case):
    def sweeper(self, clock, sent, delivery='sound'):
        return sweep.Sweeper(lambda payload, urgent: sent.append((payload, urgent)), lambda: {'low': delivery}, clock=clock)

    def test_only_agent_tabs_in_worktrees_and_no_helpers(self):
        commit(self.main, 'two')
        entries = [self.entry('%1'), self.entry('%2', agent='shell'), self.entry('%3', role='advisor'),
                   self.entry('%4', helper='reviewer'), self.entry('%5', agent='codex'), self.entry('%6')]
        folders = {'%1': str(self.tree), '%5': str(self.tree), '%6': str(self.main)}
        lines = sweep.run_sweep(entries, lambda entry: folders.get(entry['id'], str(self.tree)), lambda entry: {'state': 'Idle'},
                                now=self.now, checks=lambda *_: None)
        self.assertEqual([line['pane'] for line in lines], ['%1', '%5'])

    def test_the_cap_counts_only_worktree_tabs(self):
        commit(self.main, 'two')
        entries = [self.entry(f'%{index}') for index in range(1, 6)] + [self.entry('%9')]
        folder = lambda entry: str(self.tree if entry['id'] == '%9' else self.main)  # noqa: E731
        with mock.patch.object(sweep, 'TABS', 1):
            lines = sweep.run_sweep(entries, folder, lambda entry: None, now=self.now, checks=lambda *_: None)
            self.assertEqual([line['pane'] for line in lines], ['%9'])  # 5 ordinary tabs before it do not hide it
            both = sweep.run_sweep([self.entry('%8'), self.entry('%9')], lambda entry: str(self.tree), lambda entry: None,
                                   now=self.now, checks=lambda *_: None)
            self.assertEqual([line['pane'] for line in both], ['%8'])

    def test_over_the_cap_every_tab_gets_its_turn_and_keeps_its_lines(self):
        commit(self.main, 'two')
        clock, sent = [self.now], []
        sweeper = self.sweeper(lambda: clock[0], sent)
        entries = [self.entry('%1'), self.entry('%2'), self.entry('%3')]
        args = (entries, lambda entry: str(self.tree), lambda entry: {'state': 'Working'})
        read = []
        with mock.patch.object(sweep, 'TABS', 2):
            for _ in range(3):
                clock[0] += sweep.PERIOD + 1
                sweeper.sweep(*args, checks=lambda *_: None)
                read.append((sweep.load()['cursor'], sorted(line['pane'] for line in sweep.public()['lines'])))
        # 1 and 2, then 3 and 1, then 2 and 3: no tab is skipped for good, and a tab waiting for its turn keeps its line.
        self.assertEqual(read, [('%2', ['%1', '%2']), ('%1', ['%1', '%2', '%3']), ('%3', ['%1', '%2', '%3'])])
        self.assertEqual(sum(len(payload['body'].splitlines()) for payload, _ in sent), 3)  # each finding told once
        clock[0] += sweep.PERIOD + 1
        sweeper.sweep(entries[:1], *args[1:], checks=lambda *_: None)  # tabs 2 and 3 closed
        self.assertEqual((sweep.load()['cursor'], [line['pane'] for line in sweep.public()['lines']]), ('', ['%1']))

    def test_over_the_cap_a_tab_that_left_its_worktree_loses_its_lines(self):
        commit(self.main, 'two')
        clock = [self.now]
        sweeper = self.sweeper(lambda: clock[0], [])
        entries = [self.entry('%1'), self.entry('%2'), self.entry('%3')]
        folders = {pane: str(self.tree) for pane in ('%1', '%2', '%3')}
        args = (entries, lambda entry: folders[entry['id']], lambda entry: {'state': 'Working'})
        with mock.patch.object(sweep, 'TABS', 2):
            for _ in range(2):
                clock[0] += sweep.PERIOD + 1
                sweeper.sweep(*args, checks=lambda *_: None)
            folders['%2'] = str(self.main)  # tab 2 moves to the main checkout
            clock[0] += sweep.PERIOD + 1
            sweeper.sweep(*args, checks=lambda *_: None)
        self.assertEqual(sorted(line['pane'] for line in sweep.public()['lines']), ['%1', '%3'])

    def test_a_check_that_gh_could_not_read_once_is_not_announced_twice(self):
        answers, sent = [['test'], None, ['test']], []
        sweeper = self.sweeper(lambda: self.now, sent)
        args = ([self.entry()], lambda entry: str(self.tree), lambda entry: {'state': 'Working'})
        for index, answer in enumerate(answers):
            sweeper.clock = lambda index=index: self.now + index * (sweep.PERIOD + 1)
            sweeper.sweep(*args, checks=lambda *_, answer=answer: answer)
        self.assertEqual(len(sent), 1)
        self.assertEqual([line['kind'] for line in sweep.public()['lines']], ['checks'])
        sweeper.clock = lambda: self.now + sweep.KNOWN + 3 * sweep.PERIOD + 10
        sweeper.sweep(*args, checks=lambda *_: ['test'])
        self.assertEqual(len(sent), 2)  # after a day it counts as new again

    def test_one_broken_tab_does_not_stop_the_sweep(self):
        commit(self.main, 'two')

        def folder(entry):
            if entry['id'] == '%1':
                raise KeyError('gone')
            return str(self.tree)
        lines = sweep.run_sweep([self.entry('%1'), self.entry('%2')], folder, lambda entry: None, now=self.now, checks=lambda *_: None)
        self.assertEqual([line['pane'] for line in lines], ['%2'])

    def test_runs_once_per_period_and_tells_only_new_findings(self):
        commit(self.main, 'two')
        clock, sent = [self.now], []
        sweeper = self.sweeper(lambda: clock[0], sent)
        args = ([self.entry()], lambda entry: str(self.tree), lambda entry: {'state': 'Idle'})
        with mock.patch.object(sweep, 'failed_checks', lambda *_: None):
            self.assertTrue(sweeper.observe(*args, wait=True))
            self.assertFalse(sweeper.observe(*args, wait=True))
            self.assertEqual(len(sent), 1)
            payload, urgent = sent[0]
            self.assertFalse(urgent)
            self.assertEqual((payload['priority'], payload['go'], payload['silent']), ('low', 'factory', False))
            self.assertEqual(payload['body'], 'B1 Worker: 1 commit behind main. Rebase by hand.')
            clock[0] += sweep.PERIOD + 1
            self.assertTrue(sweeper.observe(*args, wait=True))
            self.assertEqual(len(sent), 1)  # the same finding is not announced again
            clock[0] += 3 * 3600
            self.assertTrue(sweeper.observe(*args, wait=True))
            self.assertEqual(sent[1][0]['body'], 'B1 Worker: Idle for 3 h.')
        self.assertEqual([line['kind'] for line in sweep.public()['lines']], ['behind', 'idle'])
        self.assertNotIn('key', sweep.public()['lines'][0])
        self.assertEqual(sweep.public({'%9'})['lines'], [])  # a closed tab's lines are not shown

    def test_low_delivery_off_sends_nothing_and_silent_is_silent(self):
        commit(self.main, 'two')
        args = ([self.entry()], lambda entry: str(self.tree), lambda entry: {'state': 'Idle'})
        for delivery, expected in (('off', []), ('silent', [True])):
            sweep.path().unlink(missing_ok=True)
            sent = []
            with mock.patch.object(sweep, 'failed_checks', lambda *_: None):
                self.sweeper(lambda: self.now, sent, delivery).observe(*args, wait=True)
            self.assertEqual([payload['silent'] for payload, _ in sent], expected)
            self.assertEqual(len(sweep.public()['lines']), 1)  # the Factory line is there either way

    def test_a_failed_sweep_waits_for_the_next_period(self):
        clock = [self.now]
        sweeper = self.sweeper(lambda: clock[0], [])
        with mock.patch.object(sweep, 'run_sweep', side_effect=RuntimeError('boom')), mock.patch('builtins.print'):
            self.assertTrue(sweeper.observe([], None, None, wait=True))
            self.assertFalse(sweeper.observe([], None, None, wait=True))
        clock[0] += sweep.PERIOD + 1
        self.assertTrue(sweeper.observe([], None, None, wait=True))

    def test_the_monitor_thread_reads_no_file_and_a_restart_keeps_the_period(self):
        commit(self.main, 'two')
        sweep.save(self.now - 60, [])  # a sweep ran a minute before the server restarted
        sweeper = self.sweeper(lambda: self.now, [])
        args = ([self.entry()], lambda entry: str(self.tree), lambda entry: {'state': 'Idle'})
        with mock.patch.object(sweep, 'load', side_effect=AssertionError('read on the monitor thread')), \
                mock.patch.object(sweep.threading, 'Thread') as thread:
            self.assertTrue(sweeper.observe(*args))
            self.assertEqual(thread.call_count, 1)
        sweeper.busy, sweeper.tried = False, 0
        with mock.patch.object(sweep, 'failed_checks', lambda *_: None):
            sweeper.observe(*args, wait=True)
            self.assertEqual(sweep.public()['lines'], [])  # not due yet: the worker read the time and did not sweep
            sweeper.clock = lambda: self.now + sweep.PERIOD - 30
            self.assertTrue(sweeper.observe(*args, wait=True))
        self.assertEqual(len(sweep.public()['lines']), 1)

    def test_notice_caps_the_lines(self):
        lines = [{'name': f'Tab {index}', 'text': 'Idle for 3 h.'} for index in range(5)]
        payload = sweep.notice(lines)
        self.assertEqual(payload['title'], 'Worktree sweep: 5 new findings')
        self.assertEqual(payload['body'].splitlines()[-1], 'And 2 more in the Factory view.')

    def test_a_bad_file_reads_as_no_sweep(self):
        sweep.path().parent.mkdir(parents=True)
        for raw in ('x', '[]', '{"at": "1", "lines": []}', '{"at": 1, "lines": [{"kind": "other"}]}'):
            sweep.path().write_text(raw)
            self.assertEqual(sweep.load()['lines'], [])

    def test_the_sweep_never_writes_to_the_repository(self):
        commit(self.main, 'two')
        before = (git(self.tree, 'rev-parse', 'HEAD'), git(self.tree, 'status', '--porcelain'), git(self.main, 'rev-parse', 'main'))
        self.read(checks=lambda *_: ['test'])
        self.assertEqual((git(self.tree, 'rev-parse', 'HEAD'), git(self.tree, 'status', '--porcelain'),
                          git(self.main, 'rev-parse', 'main')), before)

    def test_factory_payload_carries_the_sweep(self):
        sweep.save(self.now, [{'pane': '%7', 'name': 'B1 Worker', 'branch': 'feature', 'kind': 'idle', 'text': 'Idle for 3 h.', 'key': 'k'}])
        data = factory.payload([{'id': '%7', 'agent': 'shell'}], lambda entry: None, {}, now=self.now)
        self.assertEqual(data['sweep'], {'at': int(self.now), 'every': sweep.PERIOD, 'lines': [
            {'pane': '%7', 'name': 'B1 Worker', 'branch': 'feature', 'kind': 'idle', 'text': 'Idle for 3 h.'}]})


if __name__ == '__main__':
    unittest.main()
