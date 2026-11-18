"""ops_agent.py and lesson_candidates.py: the daily ops report (B212). Real git in a temporary folder; tmux, the tab
launcher and the clock are stand-ins. No model runs."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import factory
import lesson_candidates
import lessons
import ops_agent
import paths
import projects

PID = 'c' * 12
DAY = '2026-11-17'
ENV = {**os.environ, 'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 'T',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def at(hour, day=DAY):
    return datetime.strptime(f'{day} {hour:02d}:00', '%Y-%m-%d %H:%M').timestamp()


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name in ('CONFIG', 'TETHER', 'CACHE'):
            patch = mock.patch.object(paths, name, self.root / name.lower())
            patch.start()
            self.addCleanup(patch.stop)
        projects.bundle(PID).mkdir(parents=True)
        projects.set_name(PID, 'tether')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        factory._root_cache.clear()

    def git(self, *args, when=None):
        env = dict(ENV)
        if when is not None:
            env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{int(when)} +0000'
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

    def commit(self, message, when):
        self.git('commit', '-q', '--allow-empty', '-m', message, when=when)
        return self.git('rev-parse', '--short', 'HEAD')

    def run_tmux(self, project=PID, name='Tether'):
        def run(command, **options):
            if command[0] == paths.TMUX:
                return SimpleNamespace(returncode=0, stdout=f'{project}\t{name}\n', stderr='')
            return subprocess.run(command, **options)
        return run

    def entries(self):
        return [{'id': '%1', 'agent': 'claude', 'tmux_session': '$1', 'writable': True, 'thread': 't1'},
                {'id': '%2', 'agent': 'shell', 'tmux_session': '$1'}]


class FactTests(Case):
    def test_stamp_follows_the_verification_rule(self):
        self.assertEqual(ops_agent.stamp('Fix\n\nVerified: tests(server 412 ok) review(codex APPROVED)'),
                         ('verified', 'tests(server 412 ok) review(codex APPROVED)'))
        self.assertEqual(ops_agent.stamp('Fix\n\nVerified: none (docs only)'), ('none', 'none (docs only)'))
        for text in ('Fix', 'Fix\n\nVerified: yes', 'Fix\n\nVerified:', None):
            self.assertEqual(ops_agent.stamp(text), ('missing', ''))

    def test_commits_of_the_day_with_stamps_and_reverts(self):
        self.commit('Before the day\n\nVerified: none (old)', at(23, '2026-11-16'))
        good = self.commit('Good change\n\nVerified: tests(server 3 ok)', at(9))
        none = self.commit('Docs\n\nVerified: none (docs only)', at(10))
        bare = self.commit('No stamp at all', at(11))
        self.git('checkout', '-q', '-b', 'side')
        revert = self.commit(f'Revert "Good change"\n\nThis reverts commit {self.git("rev-parse", "main")}.\n\nVerified: none (revert)', at(12))
        self.commit('After the day', at(1, '2026-11-18'))
        start, end = ops_agent.day_bounds(DAY)
        found = ops_agent.commits(str(self.repo), start, end)
        self.assertEqual([(row['hash'], row['stamp'], row['revert']) for row in found],
                         [(good, 'verified', False), (none, 'none', False), (bare, 'missing', False), (revert, 'none', True)])
        self.assertEqual(found[3]['reverts'], self.git('rev-parse', 'main')[:12])
        self.assertEqual(ops_agent.commits(str(self.root / 'nowhere'), start, end), [])

    def test_deploy_lines_of_the_day(self):
        (self.repo / 'DEPLOYED.md').write_text('\n'.join([
            '- 2026-11-16 23:00 UTC · aaaaaaa Old · Verified: none (old)',
            '- 2026-11-17 09:00 UTC · bbbbbbb Sweep · Verified: tests(server 9 ok)',
            '- 2026-11-17 10:00 UTC · ccccccc Hotfix · Verified: none (urgent)',
            '- 2026-11-17 11:00 UTC · ddddddd No stamp line',
            'not a deploy line']))
        start, end = at(0), at(0) + 86400
        with mock.patch.dict(os.environ, {'TZ': 'UTC'}):
            time.tzset()
            self.addCleanup(time.tzset)
            found = ops_agent.deploys(str(self.repo), *ops_agent.day_bounds(DAY))
        self.assertEqual([(row['commit'], row['stamp']) for row in found],
                         [('bbbbbbb', 'verified'), ('ccccccc', 'none'), ('ddddddd', 'missing')])
        self.assertEqual(found[1]['text'], 'ccccccc Hotfix')
        self.assertEqual(ops_agent.deploys(str(self.root), start, end), [])

    def test_review_log_keeps_only_pauses(self):
        entries = self.entries()
        ops_agent.record_review('Review paused', '“B1”  reached the\nround limit.', '%1', entries, now=at(9))
        ops_agent.record_review('Review needs you', 'still blocked', '%1', entries, now=at(10))
        ops_agent.record_review('Review stalled', 'not a pause', '%1', entries, now=at(11))
        ops_agent.record_review('Review paused', 'next day', '%1', entries, now=at(9, '2026-11-18'))
        with open(ops_agent.events_path(), 'a') as out:
            out.write('not json\n')
        found = ops_agent.reviews(*ops_agent.day_bounds(DAY))
        self.assertEqual([(row['text'], row['session'], row['limit']) for row in found],
                         [('“B1” reached the round limit.', '$1', False), ('still blocked', '$1', True)])

    def test_note_lists_only_the_problems(self):
        self.commit('Good change\n\nVerified: tests(server 3 ok)', at(9))
        bare = self.commit('No stamp at all', at(11))
        facts = ops_agent.gather(DAY, [str(self.repo)], [{'at': at(13), 'text': 'B1 reached the round limit.'}])
        self.assertEqual(facts['problems'], 2)
        text = ops_agent.note_text(facts, 'tether')
        self.assertIn('# Postmortem 2026-11-17: tether', text)
        self.assertIn('- Commits: 2 (1 verified, 0 "Verified: none", 1 no stamp)', text)
        self.assertIn(f'- `{bare}` 11:00 No stamp at all (no Verified stamp; repo)', text)
        self.assertNotIn('Good change', text)
        self.assertIn('- 13:00 B1 reached the round limit.', text)
        self.assertIn('## Reverts\n- None.', text)
        self.assertTrue(text.rstrip().endswith('## What to learn\n' + ops_agent.PLACEHOLDER))


class RunTests(Case):
    def start(self):
        started = []

        def start(anchor, body, folder, run=None, group=None):
            started.append((anchor['id'], body, folder, group))
            return '%9', body['name']
        return started, start

    def test_a_day_with_a_problem_writes_the_note_and_starts_one_cheap_tab(self):
        self.commit('No stamp at all', at(11))
        started, start = self.start()
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux(), start=start)
        note = projects.bundle(PID) / 'notes' / 'postmortems' / f'{DAY}.md'
        self.assertEqual([(row['status'], row['pane'], row['project'], row['problems'], row['note']) for row in records],
                         [('started', '%9', PID, 1, str(note))])
        self.assertIn('No stamp at all', note.read_text())
        (pane, body, folder, group), = started
        self.assertEqual((pane, folder, group), ('%1', str(self.repo), ''))
        self.assertEqual((body['agent'], body['model'], body['effort'], body['name']), ('claude', 'haiku', 'low', f'Ops report {DAY}'))
        for part in (str(note), 'agent_lessons_cli.py propose --avoid', 'Do not run `add`', 'close --self', 'Do not change code'):
            self.assertIn(part, body['message'])

    def test_a_clean_day_writes_the_note_and_starts_no_tab(self):
        self.commit('Good change\n\nVerified: tests(server 3 ok)', at(9))
        started, start = self.start()
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux(), start=start)
        self.assertEqual((records[0]['status'], started), ('quiet', []))
        self.assertIn(ops_agent.QUIET, Path(records[0]['note']).read_text())

    def test_a_day_with_no_work_writes_nothing(self):
        self.commit('Other day', at(9, '2026-11-15'))
        started, start = self.start()
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux(), start=start)
        self.assertEqual((records[0]['status'], records[0]['why'], started),
                         ('quiet', 'No commits, deploys or paused reviews that day.', []))
        self.assertFalse((projects.bundle(PID) / 'notes' / 'postmortems').exists())

    def test_a_session_without_a_project_is_skipped_and_gets_none(self):
        self.commit('No stamp at all', at(11))
        commands = []

        def run(command, **options):
            commands.append(command)
            return self.run_tmux(project='')(command, **options)
        self.assertEqual(ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), run, start=self.start()[1]), [])
        self.assertFalse(any('set-option' in command for command in commands))

    def test_a_launch_failure_is_recorded_and_the_note_stays(self):
        self.commit('No stamp at all', at(11))

        def start(*args, **options):
            raise RuntimeError('tmux said no')
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux(), start=start)
        self.assertEqual((records[0]['status'], records[0]['why']), ('failed', 'tmux said no'))
        self.assertTrue(Path(records[0]['note']).is_file())

    def test_a_second_run_never_replaces_the_note_or_starts_a_second_tab(self):
        self.commit('No stamp at all', at(11))
        started, start = self.start()
        args = (DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux())
        first = ops_agent.run_day(*args, start=start)
        note = Path(first[0]['note'])
        text = note.read_text()
        again = ops_agent.run_day(*args, start=start)  # the first tab still works: the placeholder is there
        self.assertEqual((again[0]['status'], again[0]['why'], len(started)),
                         ('quiet', 'This project already got its ops tab for that day.', 1))
        self.assertEqual(note.read_text(), text)
        note.write_text(text.replace(ops_agent.PLACEHOLDER, '- The ops tab wrote this.'))
        ops_agent.state_path().unlink(missing_ok=True)  # even with the state file lost, a finished note is never replaced
        last = ops_agent.run_day(*args, start=start)
        self.assertEqual((last[0]['status'], len(started)), ('quiet', 1))
        self.assertIn('exists already', last[0]['why'])
        self.assertIn('- The ops tab wrote this.', note.read_text())

    def test_the_launch_record_outlives_the_cache_and_the_run_list(self):
        import shutil
        self.commit('No stamp at all', at(11))
        started, start = self.start()
        args = (DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux())
        ops_agent.run_day(*args, start=start)
        self.assertEqual(ops_agent.launched(DAY), [PID])
        ops_agent._change(lambda data: data['runs'].extend({'day': 'x', 'status': 'quiet'} for _ in range(3 * ops_agent.RUNS)))
        self.assertEqual((ops_agent.run_day(*args, start=start)[0]['status'], len(started)), ('quiet', 1))
        shutil.rmtree(paths.CACHE)  # the cache is cleared while the first tab still works
        self.assertEqual((ops_agent.run_day(*args, start=start)[0]['status'], len(started)), ('quiet', 1))
        paths.CACHE.mkdir()
        ops_agent.state_path().write_text('{"version": 1, "last_day": "", "runs": [], "launched": {"2026-11-17": 5}}')
        self.assertEqual((ops_agent.run_day(*args, start=start)[0]['status'], len(started)), ('quiet', 1))

    def test_a_day_gets_three_tabs_across_every_run(self):
        for pid in 'abdef':
            projects.bundle(pid * 12).mkdir(parents=True)
        self.assertEqual([ops_agent.reserve(DAY, pid * 12) for pid in 'abe'], ['', '', ''])
        self.assertIn('at most 3', ops_agent.reserve(DAY, 'd' * 12))
        self.assertIn('already got', ops_agent.reserve(DAY, 'a' * 12))
        self.assertEqual(ops_agent.reserve('2026-11-18', 'd' * 12), '')  # another day has its own three
        self.commit('No stamp at all', at(11))
        started, start = self.start()
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux(), start=start)
        self.assertEqual((records[0]['status'], started), ('failed', []))  # the fourth project of the day, in a later run
        self.assertIn('at most 3', records[0]['why'])
        ops_agent.release(DAY, 'a' * 12)
        self.assertEqual(ops_agent.reserve(DAY, 'f' * 12), '')
        folder = projects.bundle('a' * 12) / 'notes' / 'postmortems'
        mine = {'.2026-09-17.draft.tab': 'my draft', '.2020-01-01.tab': 'not empty, so not a record', '.notes.tab': '',
                '2020-01-02.md': '# an old postmortem'}
        for name, text in mine.items():
            (folder / name).write_text(text)
        for index in range(ops_agent.LAUNCH_DAYS + 5):
            ops_agent.reserve(f'2027-{1 + index // 28:02d}-{1 + index % 28:02d}', 'a' * 12)
        self.assertEqual({name: (folder / name).read_text() for name in mine}, mine)  # the person's files stay
        records = [file for file in folder.iterdir() if ops_agent.MARKER.fullmatch(file.name) and not file.stat().st_size]
        self.assertEqual(len(records), ops_agent.LAUNCH_DAYS)
        (projects.root() / ('9' * 12)).symlink_to(projects.bundle('b' * 12), target_is_directory=True)
        self.assertEqual(ops_agent.launched(DAY), ['b' * 12, 'e' * 12, 'f' * 12])  # a linked folder is not counted twice
        self.assertEqual(projects.notes('a' * 12), [])  # the records are not notes

    def test_a_launch_record_never_follows_a_link(self):
        outside = self.root / 'outside'
        outside.mkdir()
        notes = projects.bundle(PID) / 'notes'
        notes.mkdir()
        (notes / 'postmortems').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'is a link'):
            ops_agent.reserve(DAY, PID)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_second_run_starts_the_tab_that_the_first_could_not_ask_for(self):
        self.commit('No stamp at all', at(11))
        args = (DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux())
        started, start = self.start()
        first = ops_agent.run_day(*args, start=start, sessions=lambda: [])  # the anchor tab closed: tmux was not asked
        self.assertEqual((first[0]['status'], first[0]['why'], started), ('failed', 'The session has no open tab now.', []))
        text = Path(first[0]['note']).read_text()
        again = ops_agent.run_day(*args, start=start)
        self.assertEqual((again[0]['status'], len(started)), ('started', 1))
        self.assertEqual(Path(first[0]['note']).read_text(), text)  # the note was not written again

    def test_a_start_that_fails_after_tmux_was_asked_is_not_tried_again(self):
        self.commit('No stamp at all', at(11))
        args = (DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux())

        def broken(*_, **__):
            raise RuntimeError('tmux said no')  # a pane may exist already
        self.assertEqual(ops_agent.run_day(*args, start=broken)[0]['status'], 'failed')
        started, start = self.start()
        self.assertEqual((ops_agent.run_day(*args, start=start)[0]['status'], started), ('quiet', []))

    def test_a_linked_postmortems_folder_is_never_read_or_written(self):
        self.commit('No stamp at all', at(11))
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / f'{DAY}.md').write_text('private')
        notes = projects.bundle(PID) / 'notes'
        notes.mkdir()
        (notes / 'postmortems').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'is a link'):
            ops_agent.read_note(PID, DAY)
        started, start = self.start()
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: str(self.repo), self.run_tmux(), start=start)
        self.assertEqual((records[0]['status'], started), ('failed', []))
        self.assertEqual((outside / f'{DAY}.md').read_text(), 'private')
        (notes / 'postmortems').unlink()
        notes.rename(self.root / 'real-notes')
        notes.symlink_to(self.root / 'real-notes', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'is a link'):
            ops_agent.note_path(PID, DAY)
        notes.unlink()
        (self.root / 'real-notes').rename(notes)
        ops_agent.note_path(PID, DAY)  # plain folders again: allowed
        bundle = projects.bundle(PID)
        bundle.rename(self.root / 'real-project')
        (self.root / 'real-project' / 'notes' / 'postmortems').mkdir()
        (self.root / 'real-project' / 'notes' / 'postmortems' / f'{DAY}.md').write_text('elsewhere')
        bundle.symlink_to(self.root / 'real-project', target_is_directory=True)  # the project folder itself is a link
        for work in (lambda: ops_agent.read_note(PID, DAY), lambda: ops_agent.note_path(PID, DAY)):
            with self.assertRaisesRegex(ValueError, 'is a link'):
                work()

    def test_a_linked_projects_folder_is_the_persons_own_choice(self):
        real = self.root / 'elsewhere'
        paths.TETHER.rename(real)
        paths.TETHER.symlink_to(real, target_is_directory=True)
        self.assertEqual(ops_agent.note_path(PID, DAY).name, f'{DAY}.md')

    def test_a_repository_goes_to_one_session(self):
        entries = self.entries() + [{'id': '%5', 'agent': 'codex', 'tmux_session': '$2', 'writable': True},
                                    {'id': '%6', 'agent': 'claude', 'tmux_session': '$1', 'writable': True}]
        found = ops_agent.plan(entries, lambda entry: str(self.repo), self.run_tmux())
        self.assertEqual([(item['session'], item['roots']) for item in found], [('$1', [str(self.repo)]), ('$2', [])])

    def test_paused_reviews_go_to_their_session_only(self):
        ops_agent.record_review('Review paused', 'B1 reached the round limit.', '%1', self.entries(), now=at(9))
        ops_agent.record_review('Review paused', 'other session', '%5', [{'id': '%5', 'tmux_session': '$2'}], now=at(9))
        started, start = self.start()
        records = ops_agent.run_day(DAY, self.entries(), lambda entry: None, self.run_tmux(), start=start)
        text = Path(records[0]['note']).read_text()
        self.assertIn('B1 reached the round limit.', text)
        self.assertNotIn('other session', text)
        self.assertEqual(len(started), 1)


class ScheduleTests(Case):
    def ops(self, now):
        clock = [now]
        ops = ops_agent.Ops(clock=lambda: clock[0])
        return ops, clock

    def observe(self, ops):
        ops.next = 0.0
        with mock.patch.object(ops_agent, 'run_day', return_value=[{'day': 'x', 'status': 'quiet'}]) as run_day:
            ops.observe(self.entries(), lambda entry: None, wait=True)
        days = [call.args[0] for call in run_day.call_args_list]
        return bool(days), days  # (a run started, the days it reported on)

    def test_off_by_default_and_the_switch_is_a_boolean(self):
        self.assertFalse(ops_agent.enabled())
        ops, _ = self.ops(datetime(2026, 11, 18, 9, 0))
        self.assertEqual(self.observe(ops), (False, []))
        with self.assertRaises(ValueError):
            ops_agent.set_enabled('yes')
        ops_agent.set_enabled(False)
        ops_agent.settings_path().write_text('{"enabled": 1}')
        self.assertFalse(ops_agent.enabled())

    def test_once_a_day_after_the_hour_for_yesterday(self):
        ops_agent.set_enabled(True)
        ops, clock = self.ops(datetime(2026, 11, 18, ops_agent.HOUR - 1, 59))
        self.assertEqual(self.observe(ops), (False, []))
        clock[0] = datetime(2026, 11, 18, ops_agent.HOUR, 0)
        self.assertEqual(self.observe(ops), (True, [DAY]))
        clock[0] = datetime(2026, 11, 18, 23, 0)
        self.assertEqual(self.observe(ops), (False, []))
        clock[0] = datetime(2026, 11, 19, ops_agent.HOUR, 1)
        self.assertEqual(self.observe(ops), (True, ['2026-11-18']))
        self.assertEqual(len(ops_agent.load()['runs']), 2)

    def test_run_now_works_when_off_and_counts_as_the_days_run(self):
        ops, clock = self.ops(datetime(2026, 11, 18, 3, 0))
        ops_agent.request()
        self.assertTrue(ops_agent.state()['busy'])
        self.assertEqual(self.observe(ops), (True, [DAY]))
        self.assertFalse(ops_agent.state()['busy'])
        ops.next = 0.0
        ops.observe(self.entries(), lambda entry: None, wait=True)  # a look that finds nothing due
        self.assertEqual((ops.busy, ops.running), (False, False))
        ops_agent.set_enabled(True)
        clock[0] = datetime(2026, 11, 18, 9, 0)
        self.assertEqual(self.observe(ops), (False, []))

    def test_a_crash_does_not_run_again_the_same_day(self):
        ops_agent.set_enabled(True)
        ops, _ = self.ops(datetime(2026, 11, 18, 9, 0))
        ops.next = 0.0
        with mock.patch.object(ops_agent, 'run_day', side_effect=RuntimeError('boom')), mock.patch('builtins.print'):
            self.assertTrue(ops.observe([], None, wait=True))
        self.assertEqual(self.observe(ops), (False, []))

    def test_a_state_write_that_fails_starts_nothing_and_frees_the_scheduler(self):
        ops_agent.set_enabled(True)
        ops, _ = self.ops(datetime(2026, 11, 18, 9, 0))
        ops.next = 0.0
        with mock.patch.object(ops_agent, '_write', side_effect=OSError('disk')), mock.patch.object(ops_agent, 'run_day') as run_day, \
                mock.patch('builtins.print'):
            ops.observe(self.entries(), lambda entry: None, wait=True)
        self.assertEqual((ops.busy, run_day.call_count), (False, 0))
        self.assertGreater(ops.next, time.monotonic() + 200)  # not asked again on every tick
        self.assertEqual(self.observe(ops), (True, [DAY]))

    def test_a_state_file_in_trouble_stops_the_scheduler_not_settings(self):
        ops_agent.set_enabled(True)
        ops_agent.state_path().parent.mkdir(parents=True, exist_ok=True)
        ops_agent.state_path().write_text('{"version": 1}')  # there, but not valid
        self.assertEqual(ops_agent.state()['runs'], [])  # Settings still opens
        with self.assertRaisesRegex(ValueError, 'invalid'):
            ops_agent.request()
        ops, _ = self.ops(datetime(2026, 11, 18, 9, 0))
        with mock.patch('builtins.print'):
            self.assertEqual(self.observe(ops), (False, []))
        self.assertEqual(ops_agent.state_path().read_text(), '{"version": 1}')  # never replaced by a guess

    def test_the_monitor_call_does_not_wait_for_a_state_write(self):
        ops, _ = self.ops(datetime(2026, 11, 18, 9, 0))
        ops.next = 0.0
        done = []
        caller = ops_agent.threading.Thread(target=lambda: done.append(ops.observe([], None)))
        with ops_agent._LOCK, mock.patch.object(ops_agent.threading, 'Thread') as thread:  # a state write holds _LOCK
            caller.start()
            caller.join(5)
        self.assertEqual((done, thread.call_count), ([True], 1))

    def test_state_lists_runs_newest_first_and_reads_a_note(self):
        note = ops_agent.note_path(PID, DAY)
        projects._write(note, '# Postmortem\n')
        ops_agent._change(lambda data: data['runs'].extend([
            {'day': '2026-11-16', 'project': PID, 'project_name': 'tether', 'status': 'quiet', 'at': 1, 'note': str(note) + 'x'},
            {'day': DAY, 'project': PID, 'project_name': 'tether', 'status': 'started', 'pane': '%9', 'at': 2, 'note': str(note)}]))
        state = ops_agent.state()
        self.assertEqual([(row['day'], row['note']) for row in state['runs']], [(DAY, True), ('2026-11-16', False)])
        self.assertEqual((state['enabled'], state['model'], state['hour']), (False, 'Claude Haiku, low effort', ops_agent.HOUR))
        self.assertEqual(ops_agent.read_note(PID, DAY)['text'], '# Postmortem\n')
        for pid, day in ((PID, '2026-11-01'), (PID, '../x'), ('nope', DAY)):
            with self.assertRaises((projects.ProjectError, ValueError)):
                ops_agent.read_note(pid, day)


class CandidateTests(Case):
    TEXT = 'When a commit has no stamp, write Verified: none with the reason.'
    WORDS = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet', 'kilo', 'lima',
             'mike', 'november', 'oscar', 'papa', 'quebec', 'romeo', 'sierra', 'tango', 'uniform']

    def test_a_proposal_is_not_a_lesson_until_the_person_adds_it(self):
        result = lesson_candidates.propose(PID, self.TEXT, 'avoid', 'claude', quote='3 commits had no stamp', tab='Ops report', pane='%9')
        self.assertEqual(result['status'], 'proposed')
        self.assertEqual(lessons.entries(PID), [])
        self.assertEqual(lessons.for_prompt(PID, 'commit stamp')['usable'], [])
        self.assertEqual([row['project'] for row in ops_agent.state()['candidates']], [PID])
        lesson = lesson_candidates.accept(PID, result['candidate']['id'])
        self.assertEqual((lesson['text'], lesson['source'], lesson['status'], lesson['quote'], lesson['tab']),
                         (self.TEXT, 'agent', 'trial', '3 commits had no stamp', 'Ops report'))
        self.assertEqual(lesson_candidates.entries(PID), [])
        self.assertIsNone(lesson_candidates.accept(PID, result['candidate']['id']))

    def test_two_add_taps_make_one_lesson(self):
        import threading
        row = lesson_candidates.propose(PID, self.TEXT, 'avoid', 'claude')['candidate']
        results = []
        threads = [threading.Thread(target=lambda: results.append(lesson_candidates.accept(PID, row['id']))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(lessons.entries(PID)), 1)

    def test_a_failed_save_never_leaves_a_lesson_and_its_candidate(self):
        row = lesson_candidates.propose(PID, self.TEXT, 'avoid', 'claude')['candidate']
        with mock.patch.object(lessons, 'add', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                lesson_candidates.accept(PID, row['id'])
        self.assertEqual((len(lesson_candidates.entries(PID)), lessons.entries(PID)), (1, []))  # the candidate stays
        with mock.patch.object(lesson_candidates, '_save', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                lesson_candidates.accept(PID, row['id'])
        # The lesson was written and the candidate could not go: neither is lost, and a retry adds no second lesson.
        self.assertEqual((len(lesson_candidates.entries(PID)), len(lessons.entries(PID))), (1, 1))
        lessons.keep(PID, lessons.entries(PID)[0]['id'])
        lessons.promote(PID, lessons.entries(PID)[0]['id'])  # the person moved it to all projects before the retry
        lesson_candidates.accept(PID, row['id'])
        self.assertEqual((lesson_candidates.entries(PID), lessons.entries(PID), len(lessons.global_entries())), ([], [], 1))

    def test_add_at_the_lesson_limit_refuses_and_pushes_no_lesson_out(self):
        for index, word in enumerate(self.WORDS[:20] * 3):
            lessons.add(PID, f'When {word} {word}{index} happens, read {word}z{index}.', 'keep', 'agent', 'claude', True)
        before = [row['id'] for row in lessons.entries(PID)]
        self.assertEqual(len(before), 60)
        row = lesson_candidates.propose(PID, self.TEXT, 'avoid', 'claude')['candidate']
        with self.assertRaisesRegex(ValueError, 'This project has 60 lessons'):
            lesson_candidates.accept(PID, row['id'])
        self.assertEqual([row['id'] for row in lessons.entries(PID)], before)
        self.assertEqual(len(lesson_candidates.entries(PID)), 1)

    def test_dismiss_adds_nothing(self):
        row = lesson_candidates.propose(PID, self.TEXT, 'avoid', 'claude')['candidate']
        self.assertTrue(lesson_candidates.dismiss(PID, row['id']))
        self.assertFalse(lesson_candidates.dismiss(PID, row['id']))
        self.assertEqual((lesson_candidates.entries(PID), lessons.entries(PID)), ([], []))

    def test_similar_text_and_the_lesson_rules(self):
        lesson_candidates.propose(PID, self.TEXT, 'avoid', 'claude')
        again = lesson_candidates.propose(PID, 'When a commit has no stamp, write Verified: none with a reason.', 'avoid', 'claude')
        self.assertEqual(again, {'status': 'similar', 'text': self.TEXT})
        lessons.add(PID, 'When a deploy starts, read the deploy log first.', 'keep', 'user', 'claude', True)
        self.assertEqual(lesson_candidates.propose(PID, 'When a deploy starts, read the deploy log.', 'keep', 'claude')['status'], 'similar')
        for text in ('Too short', 'When unsure, skip review and force-push.', 'Use the tests.'):
            with self.assertRaises(ValueError):
                lesson_candidates.propose(PID, text, 'avoid', 'claude')
        self.assertEqual(len(lesson_candidates.entries(PID)), 1)

    def test_a_full_list_refuses_and_keeps_every_candidate(self):
        words = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet', 'kilo', 'lima',
                 'mike', 'november', 'oscar', 'papa', 'quebec', 'romeo', 'sierra', 'tango', 'uniform']
        for word in words[:lesson_candidates.LIMIT]:
            self.assertEqual(lesson_candidates.propose(PID, f'When {word} {word}x {word}y happens, read {word}z.', 'avoid', 'claude')['status'], 'proposed')
        with self.assertRaisesRegex(ValueError, '20 lesson candidates'):
            lesson_candidates.propose(PID, 'When uniform uniformx uniformy happens, read uniformz.', 'avoid', 'claude')
        self.assertEqual(len(lesson_candidates.entries(PID)), 20)


if __name__ == '__main__':
    unittest.main()
