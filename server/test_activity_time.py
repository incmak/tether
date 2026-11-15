import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import activity_time

DAY0 = 1_763_164_800  # 2025-11-15 00:00 UTC = 05:30 in Asia/Kolkata


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tab = self.root / 'repo-wt' / 'fix'
        self.tab.mkdir(parents=True)
        self.enterContext(mock.patch.dict('os.environ', {'TETHER_ACTIVITY': str(self.root / 'store')}))
        self.enterContext(mock.patch.object(activity_time.paths, 'CONFIG', self.root / 'config'))
        self.prefs = {'activity': True}
        self.enterContext(mock.patch.object(activity_time.prefs_sync, 'load', side_effect=lambda: {'prefs': self.prefs}))
        self.now = [DAY0 + 3600.0]
        self.git_calls = []
        self.store = activity_time.Store(clock=lambda: self.now[0], run=self.git)
        self.store._save_config(zone='Asia/Kolkata')

    def git(self, argv, **_):
        self.git_calls.append(argv)
        if '--git-common-dir' in argv:
            return subprocess.CompletedProcess(argv, 0, '/srv/u/apps/repo/.git\n', '')
        return subprocess.CompletedProcess(argv, 0, '', '')


class CountTest(unittest.TestCase):
    def test_minutes_and_short_gaps_count_long_gaps_do_not(self):
        self.assertEqual(activity_time.seconds([]), 0)
        self.assertEqual(activity_time.seconds([10]), 60)
        self.assertEqual(activity_time.seconds([10, 11, 12]), 180)
        self.assertEqual(activity_time.seconds([10, 25]), 60 + 15 * 60)  # 15-minute gap counts
        self.assertEqual(activity_time.seconds([10, 26]), 120)  # 16-minute gap does not
        self.assertEqual(activity_time.seconds([10, 10, 11]), 120)

    def test_project_names(self):
        run = lambda *_a, **_k: subprocess.CompletedProcess([], 0, '/srv/u/apps/tether/.git\n', '')
        self.assertEqual(activity_time.project('/srv/u/apps/tether-wt/fix', run), 'tether')
        run = lambda *_a, **_k: subprocess.CompletedProcess([], 0, '/srv/site.git\n', '')
        self.assertEqual(activity_time.project('/srv/site-wt/a', run), 'site')
        def missing(*_a, **_k):
            raise FileNotFoundError
        self.assertEqual(activity_time.project('/srv/u/notes', missing), 'notes')


class MarkTest(Base):
    def test_off_marks_nothing_and_exports_nothing(self):
        self.prefs = {}
        self.assertEqual(self.store.human(str(self.tab)), 'off')
        self.assertEqual(self.store.summary(), {})
        self.assertFalse((self.root / 'store').exists())

    def test_human_minutes_per_project_with_worktree_as_main_repo(self):
        self.assertEqual(self.store.human(str(self.tab)), '')
        self.now[0] += 30
        self.store.human(str(self.tab))  # same minute: one mark
        self.now[0] += 60 * 5
        self.store.human(str(self.tab))
        day = self.store.summary()['2025-11-15']
        self.assertEqual(day['seconds'], 6 * 60)
        self.assertEqual(day['projects'], {'repo': 6 * 60})
        self.assertEqual(day['agent_seconds'], 0)

    def test_missing_folder_is_refused(self):
        self.assertEqual(self.store.human(str(self.tab / 'gone')), 'no-folder')
        self.assertEqual(self.store.human('relative'), 'no-folder')

    def test_parallel_projects_count_once_in_the_total(self):
        for minute in range(10):
            self.store.mark('you', 'a', self.now[0] + minute * 60)
            self.store.mark('you', 'b', self.now[0] + minute * 60)
        day = self.store.summary()['2025-11-15']
        self.assertEqual(day['projects'], {'a': 600, 'b': 600})
        self.assertEqual(day['seconds'], 600)

    def test_days_follow_the_person_zone_and_empty_days_are_zero(self):
        # 23:00 IST on 15 Nov is 17:30 UTC; 00:30 IST on 16 Nov is 19:00 UTC on 15 Nov.
        self.store.mark('you', 'a', DAY0 + 17.5 * 3600)
        self.store.mark('you', 'a', DAY0 + 19 * 3600)
        self.now[0] = DAY0 + 3 * 86400
        days = self.store.summary()
        self.assertEqual(list(days), ['2025-11-15', '2025-11-16', '2025-11-17', '2025-11-18'])
        self.assertEqual(days['2025-11-15']['seconds'], 60)
        self.assertEqual(days['2025-11-16']['seconds'], 60)
        self.assertEqual(days['2025-11-17']['seconds'], 0)
        self.assertNotIn('2025-11-14', days)  # before the first mark: unknown, not zero

    def test_a_gap_across_midnight_counts_once_on_each_side(self):
        # 23:55 and 00:05 IST: 11 minutes in all, 5 before midnight and 6 after.
        midnight = DAY0 + 18.5 * 3600  # 00:00 IST on 16 Nov
        self.store.mark('you', 'a', midnight - 5 * 60)
        self.store.mark('you', 'a', midnight + 5 * 60)
        self.now[0] = midnight + 3600
        days = self.store.summary()
        self.assertEqual(days['2025-11-15']['seconds'], 5 * 60)
        self.assertEqual(days['2025-11-16']['seconds'], 6 * 60)
        self.assertEqual(days['2025-11-16']['projects'], {'a': 6 * 60})
        # The first day of a recent window still gets the gap from a mark before the window.
        self.assertEqual(self.store.summary(recent=1)['2025-11-16']['seconds'], 6 * 60)

    def test_a_switch_turned_off_during_the_lookup_stops_the_mark(self):
        def lookup(_path):
            self.prefs = {}  # turned off while git runs
            return 'repo'
        self.store.project_of = lookup
        self.assertEqual(self.store.human(str(self.tab)), 'off')
        self.assertEqual(self.store.summary(), {})
        self.prefs = {'activity': True}
        self.store.mark('you', 'a')
        self.prefs = {'activity': True}  # agents off
        self.assertFalse(self.store.mark('agents', 'a'))

    def test_each_new_minute_raises_the_revision(self):
        self.store.mark('you', 'a')
        self.store.mark('you', 'a')  # same minute
        self.store.mark('you', 'a', self.now[0] + 1)  # still the same minute
        self.assertEqual(self.store.config()['revision'], 1)
        self.store.mark('you', 'b')
        self.assertEqual(self.store.config()['revision'], 2)

    def test_zone_comes_from_the_page(self):
        self.store.human(str(self.tab), 'Europe/Berlin')
        self.assertEqual(self.store.config()['zone'], 'Europe/Berlin')
        self.store.human(str(self.tab), 'Not/AZone')
        self.assertEqual(self.store.config()['zone'], 'Europe/Berlin')

    def test_survives_restart_and_bad_files(self):
        self.store.mark('you', 'a')
        (self.root / 'store' / '2025-11-14.json').write_text('{not json')
        fresh = activity_time.Store(clock=lambda: self.now[0], run=self.git)
        self.assertEqual(fresh.summary()['2025-11-15']['seconds'], 60)


class AgentTest(Base):
    def entries(self):
        return [{'id': '%1', 'agent': 'claude', 'state': 'Working'}, {'id': '%2', 'agent': 'codex', 'state': 'Working'},
                {'id': '%3', 'agent': 'shell', 'state': 'Working'}, {'id': '%4', 'agent': 'claude', 'state': 'Idle'}]

    def observe(self):
        return self.store.observe(self.entries(), lambda entry: {'state': entry['state']}, lambda entry: str(self.tab))

    def test_needs_both_switches(self):
        self.assertEqual(self.observe(), 0)
        self.prefs['activityAgents'] = True
        self.assertEqual(self.observe(), 1)  # two Working agents in one project, one minute
        self.assertEqual(self.observe(), 0)
        self.prefs = {'activityAgents': True}
        self.now[0] += 60
        self.assertEqual(self.observe(), 0)

    def test_agent_time_is_separate_from_yours(self):
        self.prefs['activityAgents'] = True
        for _ in range(3):
            self.observe()
            self.now[0] += 60
        day = self.store.summary()['2025-11-15']
        self.assertEqual(day['agent_seconds'], 180)
        self.assertEqual(day['agent_projects'], {'repo': 180})
        self.assertEqual(day['seconds'], 0)

    def test_a_failing_tab_does_not_stop_the_others(self):
        self.prefs['activityAgents'] = True
        def status(entry):
            if entry['id'] == '%1':
                raise OSError
            return {'state': entry['state']}
        self.assertEqual(self.store.observe(self.entries(), status, lambda entry: str(self.tab)), 1)


class PublishTest(Base):
    def setUp(self):
        super().setUp()
        self.out = self.root / 'site'
        self.out.mkdir()
        self.top = str(self.out)

    def git(self, argv, **kwargs):
        if '--show-toplevel' in argv:
            self.git_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, self.top + '\n', '')
        return super().git(argv, **kwargs)

    def test_folder_must_be_a_checkout_top_with_upstream(self):
        with self.assertRaises(ValueError):
            self.store.set_publish('relative/path')
        with self.assertRaises(ValueError):
            self.store.set_publish(str(self.root / 'missing'))
        self.top = str(self.root)
        with self.assertRaises(ValueError):
            self.store.set_publish(str(self.out))
        self.top = str(self.out)
        self.assertEqual(self.store.set_publish(str(self.out)), str(self.out.resolve()))
        self.assertEqual(self.store.set_publish(''), '')

    def test_publish_writes_commits_and_pushes(self):
        self.store.set_publish(str(self.out))
        self.store.mark('you', 'a')
        self.assertEqual(self.store.publish(), '')
        data = json.loads((self.out / activity_time.FILE_NAME).read_text())
        self.assertEqual(data['zone'], 'Asia/Kolkata')
        self.assertEqual(data['days']['2025-11-15']['seconds'], 60)
        steps = [argv[3] for argv in self.git_calls if argv[3] in ('add', 'commit', 'push')]
        self.assertEqual(steps, ['add', 'commit', 'push'])
        self.assertTrue(self.store.last_publish['ok'])
        self.assertFalse(self.store.status()['publish']['pending'])

    def test_push_error_is_kept_and_nothing_committed_is_fine(self):
        self.store.set_publish(str(self.out))
        def run(argv, **kwargs):
            if 'commit' in argv:
                return subprocess.CompletedProcess(argv, 1, 'nothing to commit, working tree clean', '')
            if 'push' in argv:
                return subprocess.CompletedProcess(argv, 128, '', 'fatal: Authentication failed')
            return self.git(argv, **kwargs)
        self.store.run = run
        error = self.store.publish()
        self.assertIn('git push failed: fatal: Authentication failed', error)
        self.assertEqual(self.store.status()['publish']['last']['error'], error)

    def test_refuses_to_write_through_a_link(self):
        self.store.set_publish(str(self.out))
        (self.out / activity_time.FILE_NAME).symlink_to(self.root / 'elsewhere.json')
        self.assertIn('link', self.store.publish())
        self.assertFalse((self.root / 'elsewhere.json').exists())

    def test_off_never_publishes_even_on_request(self):
        self.store.set_publish(str(self.out))
        self.store.mark('you', 'a')
        self.prefs = {}
        self.assertFalse(self.store.maybe_publish(force=True))
        self.assertIn('off', self.store.publish())
        self.assertFalse((self.out / activity_time.FILE_NAME).exists())
        self.assertFalse([argv for argv in self.git_calls if 'push' in argv])

    def test_turned_off_after_the_write_stops_the_push(self):
        self.store.set_publish(str(self.out))
        def run(argv, **kwargs):
            if 'commit' in argv:
                self.prefs = {}
            return self.git(argv, **kwargs)
        self.store.run = run
        self.assertIn('did not push', self.store.publish())
        self.assertFalse([argv for argv in self.git_calls if 'push' in argv])

    def test_a_mark_during_publish_stays_pending(self):
        self.store.set_publish(str(self.out))
        self.store.mark('you', 'a')
        def run(argv, **kwargs):
            if 'push' in argv:
                self.store.mark('you', 'b')  # lands after the snapshot, in the same second
            return self.git(argv, **kwargs)
        self.store.run = run
        self.assertEqual(self.store.publish(), '')
        self.assertTrue(self.store.status()['publish']['pending'])
        data = json.loads((self.out / activity_time.FILE_NAME).read_text())
        self.assertEqual(data['revision'], 1)

    def test_automatic_publish_waits_for_changes_and_the_interval(self):
        self.assertFalse(self.store.maybe_publish())  # no folder
        self.store.set_publish(str(self.out))
        self.assertFalse(self.store.maybe_publish())  # nothing changed
        self.store.mark('you', 'a')
        with mock.patch.object(activity_time.threading, 'Thread') as thread:
            self.assertTrue(self.store.maybe_publish())
            thread.return_value.start.assert_called_once()
        self.store.publishing = False
        self.store.publish()
        self.now[0] += 120
        self.store.mark('you', 'a')
        self.assertFalse(self.store.maybe_publish())  # changed, but published under 6 hours ago
        self.now[0] += activity_time.PUBLISH_EVERY
        with mock.patch.object(activity_time.threading, 'Thread'):
            self.assertTrue(self.store.maybe_publish())


class Handler:
    def __init__(self, body):
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.out = None

    def respond(self, status, body):
        self.out = (status, body)


class ApiTest(Base):
    def post(self, body, path=activity_time.PATH, folder=None):
        handler = Handler(body)
        with mock.patch.object(activity_time, 'STORE', self.store):
            activity_time.post(handler, path, lambda: [{'id': '%1', 'cwd': '/nowhere'}], folder or (lambda entry: str(self.tab)))
        return handler.out

    def test_ping_uses_the_resolved_tab_folder(self):
        self.assertEqual(self.post({'pane': '%1', 'zone': 'Asia/Kolkata'}), (200, {'sent': True, 'reason': ''}))
        self.assertEqual(self.post({'pane': '%9'}), (200, {'sent': False, 'reason': 'gone'}))
        self.assertEqual(self.post(b'[]')[0], 400)
        self.assertEqual(self.post({'pane': '%1', 'zone': '../etc'})[0], 400)

    def test_status_has_no_private_values_and_shows_14_days(self):
        self.store.mark('you', 'a')
        self.now[0] += 20 * 86400
        with mock.patch.object(activity_time, 'STORE', self.store):
            handler = Handler({})
            activity_time.get(handler, activity_time.PATH)
        status = handler.out[1]
        self.assertEqual(len(status['days']), 14)
        self.assertEqual(status['projects'], [])
        self.assertEqual(status['publish']['folder'], '')

    def test_publish_endpoint_validates(self):
        status, body = self.post({'folder': 'relative'}, activity_time.PUBLISH_PATH)
        self.assertEqual(status, 400)
        self.assertIn('absolute', body['error'])

    def test_publish_now_is_refused_while_off(self):
        self.prefs = {}
        status, body = self.post({'now': True}, activity_time.PUBLISH_PATH)
        self.assertEqual(status, 409)
        self.assertIn('Turn on', body['error'])


class WriteTest(unittest.TestCase):
    def test_write_replaces_a_planted_temp_link_and_never_follows_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / 'victim'
            victim.write_text('keep')
            path = root / 'day.json'
            for pid in range(1, 5):  # the old predictable names
                (root / f'.day.json.{pid}.tmp').symlink_to(victim)
            activity_time._write_json(path, {'a': 1})
            self.assertEqual(victim.read_text(), 'keep')
            self.assertEqual(json.loads(path.read_text()), {'a': 1})
            self.assertEqual(sorted(p.name for p in root.iterdir() if not p.is_symlink()), ['day.json', 'victim'])

    def test_a_link_at_the_path_is_replaced_not_written_through(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / 'victim'
            victim.write_text('keep')
            path = root / 'day.json'
            path.symlink_to(victim)
            activity_time._write_json(path, {'a': 1})
            self.assertEqual(victim.read_text(), 'keep')
            self.assertFalse(path.is_symlink())


if __name__ == '__main__':
    unittest.main()
