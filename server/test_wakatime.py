import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wakatime


class Result:
    def __init__(self, returncode=0, stdout=''):
        self.returncode = returncode
        self.stdout = stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.prefs = {'wakatime': True}
        self.enterContext(mock.patch.object(wakatime.prefs_sync, 'load', side_effect=lambda: {'prefs': self.prefs}))
        self.calls = []
        self.now = [1000.0]
        self.git = Result(0, '/repos/tether/.git\n')
        self.tracker = wakatime.Tracker(run=self.fake_run, clock=lambda: self.now[0], find=lambda: '/bin/wakatime-cli')

    def fake_run(self, argv, **_):
        if argv[0] == 'git':
            return self.git
        self.calls.append(argv)
        return Result(0, '1 hr 5 mins Coding')

    def drain(self):
        self.tracker.jobs.join()


class HeartbeatTest(Base):
    def test_off_by_default_sends_nothing(self):
        self.prefs = {}
        self.assertEqual(self.tracker.heartbeat({'cwd': self.folder.name}), 'off')
        self.assertEqual(self.tracker.observe(), False)
        self.assertEqual(self.calls, [])

    def test_sends_one_heartbeat_for_the_tab_folder_without_parsing_transcripts(self):
        self.assertEqual(self.tracker.heartbeat({'cwd': self.folder.name}), '')
        self.drain()
        argv = self.calls[0]
        self.assertEqual(argv[0], '/bin/wakatime-cli')
        self.assertEqual(argv[argv.index('--entity') + 1], self.folder.name)
        self.assertEqual(argv[argv.index('--category') + 1], 'coding')
        self.assertEqual(argv[argv.index('--time') + 1], '1000.000')
        self.assertEqual(argv[argv.index('--project') + 1], 'tether')
        self.assertIn('--sync-ai-disabled', argv)
        self.assertNotIn('--key', argv)  # the CLI reads the key from its own config

    def test_same_folder_waits_two_minutes(self):
        self.tracker.heartbeat({'cwd': self.folder.name})
        self.drain()
        self.now[0] += 60
        self.assertEqual(self.tracker.heartbeat({'cwd': self.folder.name}), 'recent')
        self.now[0] += 61
        self.assertEqual(self.tracker.heartbeat({'cwd': self.folder.name}), '')
        self.drain()
        self.assertEqual(len(self.calls), 2)

    def test_refuses_missing_folder_and_missing_cli(self):
        self.assertEqual(self.tracker.heartbeat({'cwd': ''}), 'no-folder')
        self.assertEqual(self.tracker.heartbeat({'cwd': self.folder.name + '/gone'}), 'no-folder')
        tracker = wakatime.Tracker(run=self.fake_run, clock=lambda: self.now[0], find=lambda: None)
        self.assertEqual(tracker.heartbeat({'cwd': self.folder.name}), 'no-cli')

    def test_folder_outside_git_is_only_a_fallback_name(self):
        self.git = Result(128, '')
        self.tracker.heartbeat({'cwd': self.folder.name})
        self.drain()
        argv = self.calls[0]
        self.assertNotIn('--project', argv)
        self.assertEqual(argv[argv.index('--alternate-project') + 1], Path(self.folder.name).name)

    def test_cli_error_is_reported_not_raised(self):
        self.tracker.run = lambda argv, **_: self.git if argv[0] == 'git' else Result(104)
        self.tracker.heartbeat({'cwd': self.folder.name})
        self.drain()
        self.assertEqual(self.tracker.last['ok'], False)
        self.assertIn('API key', self.tracker.last['error'])


class TodayTest(Base):
    def test_status_reads_today_once_and_keeps_send_errors(self):
        self.tracker.last = {'at': 1, 'ok': False, 'error': 'kept'}
        with mock.patch.object(wakatime, 'has_key', return_value=True):
            self.tracker.status()
            self.drain()
            self.assertEqual(self.tracker.status()['today'], '1 hr 5 mins Coding')
        self.assertEqual(sum('--today' in argv for argv in self.calls), 1)
        self.assertEqual(self.tracker.last['error'], 'kept')


class ProjectTest(unittest.TestCase):
    def test_worktree_counts_as_its_main_repository(self):
        run = lambda *_a, **_k: Result(0, '/srv/u/apps/tether/.git\n')
        self.assertEqual(wakatime.project('/srv/u/apps/tether-wt/fix', run), ('tether', True))

    def test_bare_repository(self):
        run = lambda *_a, **_k: Result(0, '/srv/site.git\n')
        self.assertEqual(wakatime.project('/srv/site-wt/a', run), ('site', True))

    def test_git_missing(self):
        def run(*_a, **_k):
            raise FileNotFoundError
        self.assertEqual(wakatime.project('/srv/u/notes', run), ('notes', False))


class AgentSyncTest(Base):
    def test_agent_parse_needs_both_switches_and_waits_between_runs(self):
        self.assertFalse(self.tracker.observe())
        self.prefs['wakatimeAgents'] = True
        self.assertTrue(self.tracker.observe())
        self.drain()
        self.assertIn('--sync-ai-activity', self.calls[0])
        self.now[0] += 60
        self.assertFalse(self.tracker.observe())
        self.now[0] += wakatime.SYNC_EVERY
        self.assertTrue(self.tracker.observe())


class KeyTest(unittest.TestCase):
    def test_reads_only_whether_a_key_exists(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict('os.environ', {'WAKATIME_HOME': home}, clear=False):
            with mock.patch.dict('os.environ', {'WAKATIME_API_KEY': ''}):
                self.assertFalse(wakatime.has_key())
                (Path(home) / '.wakatime.cfg').write_text('[settings]\napi_key = waka_secret\n')
                self.assertTrue(wakatime.has_key())
                with mock.patch.object(wakatime.prefs_sync, 'load', return_value={'prefs': {}}):
                    status = wakatime.Tracker(find=lambda: None).status()
                self.assertNotIn('waka_secret', json.dumps(status))
                self.assertEqual(status['key'], True)
                self.assertEqual(status['cli'], False)


class Handler:
    def __init__(self, body):
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.out = None

    def respond(self, status, body):
        self.out = (status, body)


class PostTest(Base):
    def test_unknown_pane_and_bad_body(self):
        handler = Handler({'pane': '%9'})
        wakatime.post(handler, lambda: [{'id': '%1', 'cwd': self.folder.name}])
        self.assertEqual(handler.out, (200, {'sent': False, 'reason': 'gone'}))
        handler = Handler(b'[]')
        wakatime.post(handler, lambda: [])
        self.assertEqual(handler.out[0], 400)

    def test_known_pane_sends(self):
        handler = Handler({'pane': '%1'})
        with mock.patch.object(wakatime, 'TRACKER', self.tracker):
            wakatime.post(handler, lambda: [{'id': '%1', 'cwd': self.folder.name}])
        self.assertEqual(handler.out, (200, {'sent': True, 'reason': ''}))
        self.drain()


if __name__ == '__main__':
    unittest.main()
