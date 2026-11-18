from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import audit
import paths


TODAY = date(2026, 11, 18)
AT = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400
CHROME = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/128.0.0.0 Safari/537.36'


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(mock.patch.object(paths, 'CONFIG', Path(temporary.name)))
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop('TMUX_PANE', None)
        self.clock = self.enterContext(mock.patch.object(audit.time, 'time', return_value=AT))
        self.enterContext(mock.patch.object(audit, '_PRUNED', set()))
        self.enterContext(mock.patch.object(audit, '_FACETS', None))
        self.enterContext(mock.patch.object(audit, '_TABS', {}))
        self.folder = paths.CONFIG / 'audit'

    def test_append_uses_utc_day_and_owner_permissions(self):
        self.clock.return_value = AT + 0.123
        with audit.acting('ui', device='Chrome on Mac'):
            first = audit.record('setting.codex_level', 'Codex level 3 → 1', old=3, new=1, undo=True)
            second = audit.record('setting.codex_level', 'Codex level 1 → 2', old=1, new=2)
        path = self.folder / '2026-11-18.jsonl'
        self.assertEqual([json.loads(line) for line in path.read_bytes().splitlines()], [first, second])
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(first['at'], AT + 0.123)
        self.assertRegex(first['id'], rf'^{int((AT + 0.123) * 1000):016d}-[0-9a-f]{{6}}$')
        self.assertEqual(first['actor'], {'source': 'ui', 'device': 'Chrome on Mac'})
        self.assertIs(first['undo'], True)

    def test_explicit_and_lazy_prune_at_most_once_per_day(self):
        self.folder.mkdir()
        stale = self.folder / f'{TODAY - timedelta(days=91)}.jsonl'
        boundary = self.folder / f'{TODAY - timedelta(days=90)}.jsonl'
        unrelated = self.folder / 'other.jsonl'
        for path in (stale, boundary, unrelated):
            path.touch()
        audit.prune()
        self.assertFalse(stale.exists())
        self.assertTrue(boundary.exists())
        stale.touch()
        audit.record('tab.start', 'Started tab Test (codex)')
        self.assertTrue(stale.exists())
        self.clock.return_value += DAY
        audit.record('tab.close', 'Closed tab Test')
        self.assertFalse(stale.exists())
        self.assertFalse(boundary.exists())
        self.assertTrue(unrelated.exists())
        stale.touch()
        self.clock.return_value -= DAY
        audit.record('tab.start', 'Started tab Two (claude)')
        self.assertTrue(stale.exists())
        self.clock.return_value += 2 * DAY
        audit.record('tab.close', 'Closed tab Two')
        self.assertFalse(stale.exists())

    def test_filter_text_by_all_words_and_other_fields(self):
        with audit.acting('ui', tab='Audit Log', device='Chrome on Mac', pane='%12'):
            settings = audit.record('setting.codex_run', 'Changed run', project='Tether', old='auto · auto · off',
                                    new='sol · high · fast', undo=True, outcome='saved')
        self.clock.return_value += 1
        backlog = audit.record('backlog.start', 'B56 started: Audit Log', project='Tether')
        self.clock.return_value += 1
        other = audit.record('backlog.add', 'B57 added: Logging', project='Other')
        for text in ('changed SAVED', 'tether Chrome high', 'oldUNKNOWN', 'fast auto codex_run'):
            want = [] if text == 'oldUNKNOWN' else [settings]
            self.assertEqual(audit.query(text=text)['entries'], want, text)
        self.assertEqual(audit.query(kinds=['backlog'])['entries'], [other, backlog])
        self.assertEqual(audit.query(kinds=['backlog.start', 'settings'])['entries'], [backlog, settings])
        self.assertEqual(audit.query(project='tETHER')['entries'], [backlog, settings])
        self.assertEqual(audit.query(project='Teth')['entries'], [])
        for actor in ('aUdiT', 'mac', '%12', 'UI'):
            self.assertEqual(audit.query(actor=actor)['entries'], [settings], actor)
        self.assertEqual(audit.query(since=AT + 1, until=AT + 2)['entries'], [backlog])

    def test_cursor_pages_on_two_days_with_millisecond_ties(self):
        with mock.patch.object(audit.secrets, 'token_hex', side_effect=('111111', 'ffffff', 'aaaaaa', '000000', '222222')):
            oldest = audit.record('lesson.add', 'First')
            newer = audit.record('lesson.remove', 'Second')
            self.clock.return_value += DAY
            middle = audit.record('lesson.add', 'Third')
            lowest = audit.record('lesson.remove', 'Fourth')
            self.clock.return_value += 1
            newest = audit.record('lesson.dispute', 'Fifth')
        with (self.folder / '2026-11-19.jsonl').open('ab') as stream:
            stream.write(b'bad json\n\xff\n[]\n{"id":"broken"}\n')
        with mock.patch.object(audit, '_entries', wraps=audit._entries) as reads:
            first = audit.query(kinds=['lessons'], limit=2)
            reads.assert_called_once_with(self.folder / '2026-11-19.jsonl', newest_first=True)
        second = audit.query(limit=2, before=first['next'])
        third = audit.query(limit=2, before=second['next'])
        self.assertEqual(first['entries'], [newest, middle])
        self.assertEqual(second['entries'], [lowest, newer])
        self.assertEqual(third, {'entries': [oldest], 'next': None})

    def test_query_stops_inside_the_latest_day(self):
        for index in range(80):
            self.clock.return_value += 1
            audit.record('tab.start', f'Started tab {index} ' + 'x' * 150)
        with mock.patch.object(audit.json, 'loads', wraps=audit.json.loads) as parse:
            result = audit.query(limit=2, before='')
        self.assertEqual(len(result['entries']), 2)
        self.assertEqual(parse.call_count, 4)  # One more line confirms the end of the last millisecond group.
        self.assertEqual(len(audit.query(limit=100)['entries']), 80)  # Also reads across partial lines in adjacent blocks.

    def test_cursor_order_survives_clock_moving_back_on_two_days(self):
        expected = []
        for day in (AT - DAY, AT):
            self.clock.return_value = day + 5
            newer = audit.record('tab.start', 'Started tab One (claude)')
            self.clock.return_value = day + 1
            older = audit.record('tab.close', 'Closed tab One')
            self.assertEqual(older['at'], day + 1)
            expected[0:0] = [newer, older]
        self.clock.return_value = AT + 8
        expected.insert(0, audit.record('lesson.add', 'Newest write'))

        first = audit.query(limit=2)
        second = audit.query(limit=2, before=first['next'])
        last = audit.query(limit=2, before=second['next'])
        self.assertEqual(first['entries'] + second['entries'] + last['entries'], expected)
        self.assertIsNone(last['next'])

    def test_caps_and_secret_keys(self):
        secrets = dict.fromkeys(('token_hint', 'client_secret', 'Password', 'credentials', 'assertion', 'approval_action',
                                 'signature', 'public_key', 'cookie', 'key_material'), 'sensitive')
        good = {'list_value': ['body'], 'payload': {'value': 'body'}, 'not_finite': float('inf'),
                'message': 'm' * 250, 'result': True, 'none': None, **{f'field{i}': i for i in range(10)}}
        entry = audit.record('setting.reviewers', 'A' * 220, old='o' * 130, new='n' * 131, **secrets, **good)
        self.assertEqual(entry['summary'], 'A' * 200)
        self.assertEqual((entry['old'], entry['new']), ('o' * 120, 'n' * 120))
        self.assertEqual(entry['detail'], {'message': 'm' * 200, 'result': True, 'none': None,
                                           **{f'field{i}': i for i in range(5)}})
        self.assertNotIn('sensitive', self.folder.joinpath('2026-11-18.jsonl').read_text())

    def test_no_op_settings_are_quiet_and_nonsettings_keep_actions(self):
        self.assertIsNone(audit.record('setting.codex_level', 'No change', old=2, new=2))
        self.assertIsNone(audit.record('setting.advisor', 'None to None', old=None, new=None))
        self.assertFalse(self.folder.exists())
        entry = audit.record('backlog.add', 'Added', old=2, new=2)
        self.assertNotIn('old', entry)
        self.assertEqual(audit.query()['entries'], [entry])

    def test_unknown_kind_and_file_error_do_not_escape(self):
        failures = StringIO()
        with redirect_stderr(failures):
            self.assertIsNone(audit.record('not.a.kind', 'Unknown'))
            self.assertFalse(self.folder.exists())
            self.folder.touch()
            self.assertIsNone(audit.record('tab.start', 'Started tab One (claude)'))
        self.assertEqual(len(failures.getvalue().splitlines()), 2)

    def test_nonserializable_values_and_json_errors_do_not_escape(self):
        entry = audit.record('setting.codex_run', 'Changed', old=object(), custom=object())
        self.assertNotIn('old', entry)
        self.assertNotIn('detail', entry)
        failures = StringIO()
        with (redirect_stderr(failures), mock.patch.object(audit.json, 'dumps', side_effect=TypeError('no JSON'))):
            self.assertIsNone(audit.record('tab.start', 'Started tab One (claude)'))
        self.assertEqual(len(failures.getvalue().splitlines()), 1)
        self.assertEqual(audit.query()['entries'], [entry])

    def test_short_write_does_not_damage_the_next_or_previous_record(self):
        before = audit.record('tab.start', 'Started tab One (claude)')
        write = os.write
        failures = StringIO()
        with (redirect_stderr(failures), mock.patch.object(audit.os, 'write', side_effect=lambda fd, line: write(fd, line[:5]))):
            self.assertIsNone(audit.record('tab.start', 'Started tab Two (codex)'))
        self.clock.return_value += 1
        after = audit.record('tab.close', 'Closed tab One')
        self.assertEqual(len(failures.getvalue().splitlines()), 1)
        self.assertEqual(audit.query()['entries'], [after, before])

    def test_concurrent_appends_are_intact_and_actors_stay_local(self):
        start = threading.Barrier(8)

        def append(index):
            with audit.acting('hook', tab=f'Tab {index}'):
                start.wait(timeout=10)
                return audit.record('lesson.add', f'Lesson {index}')

        with ThreadPoolExecutor(max_workers=8) as threads:
            written = list(threads.map(append, range(8)))
        path = self.folder / '2026-11-18.jsonl'
        lines = [json.loads(line) for line in path.read_bytes().splitlines()]
        self.assertEqual(len(lines), 8)
        self.assertCountEqual(lines, written)
        for index, entry in enumerate(written):
            self.assertEqual(entry['actor'], {'source': 'hook', 'tab': f'Tab {index}'})

    def test_devices_and_request_attribution(self):
        devices = {
            CHROME: 'Chrome on Mac',
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1': 'Safari on iPhone',
            'Mozilla/5.0 (X11; Linux x86_64) Firefox/125.0': 'Firefox on Linux',
            'Mozilla/5.0 (Windows NT 10.0) Chrome/128.0 Safari/537.36 Edg/128.0': 'Edge on Windows',
            'Mozilla/5.0 (Linux; Android 15) Chrome/128.0 Safari/537.36': 'Chrome on Android',
            'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) Safari/604.1': 'Safari on iPad',
            'Test browser': 'Browser on Computer', 'Python-urllib/3.11': None, 'curl/8.0': None,
        }
        for agent, device in devices.items():
            self.assertEqual(audit.device_name(agent), device, agent)
        self.assertEqual(audit.actor_from_request({'X-Tether-Client': 'web', 'User-Agent': CHROME}),
                         {'source': 'ui', 'device': 'Chrome on Mac'})
        self.assertEqual(audit.actor_from_request({'User-Agent': CHROME}),
                         {'source': 'unknown', 'device': 'Chrome on Mac'})
        self.assertEqual(audit.actor_from_request({'X-Tether-Client': 'agent-cli', 'X-Tether-Pane': '%12'}),
                         {'source': 'agent-cli', 'pane': '%12'})
        self.assertEqual(audit.actor_from_request({'X-Tether-Client': 'agent-cli', 'X-Tether-Pane': '%n',
                                                   'User-Agent': 'Python-urllib/3.11'}),
                         {'source': 'unknown', 'device': 'Python-urllib/3.11'})
        self.assertEqual(audit.actor_from_request({'User-Agent': 'x' * 80}), {'source': 'unknown', 'device': 'x' * 60})
        self.assertEqual(audit.actor_from_request({}), {'source': 'unknown', 'device': 'No user agent'})

    def test_default_actor_context_and_tmux_cache(self):
        tmux = SimpleNamespace(returncode=0, stdout='Audit Log\n')
        with (mock.patch.object(audit.subprocess, 'run', return_value=tmux) as run,
              mock.patch.object(audit.time, 'monotonic', side_effect=(100, 159, 160))):
            self.assertEqual(audit.record('tab.start', 'Started')['actor'], {'source': 'cli'})
            os.environ['TMUX_PANE'] = '%12'
            expected = {'source': 'agent-cli', 'pane': '%12', 'tab': 'Audit Log'}
            self.assertEqual(audit.record('tab.start', 'Started')['actor'], expected)
            with audit.acting('server'):
                self.assertEqual(audit.record('tab.start', 'Started')['actor'], {'source': 'server'})
            self.assertEqual(audit.record('tab.start', 'Started')['actor'], expected)
            run.assert_called_once_with([paths.TMUX, 'display-message', '-p', '-t', '%12', '#{@chat_name}'],
                                        capture_output=True, text=True, timeout=1)
            tmux.stdout = ''
            self.assertEqual(audit.record('tab.start', 'Started')['actor'], {'source': 'agent-cli', 'pane': '%12'})
            self.assertEqual(run.call_count, 2)

    def test_capture_actor_retains_pane_when_tmux_is_unavailable(self):
        with (audit.acting('agent-cli', pane='%21'),
              mock.patch.object(audit.subprocess, 'run', side_effect=OSError)):
            self.assertEqual(audit.capture_actor(), {'source': 'agent-cli', 'pane': '%21'})

    def test_facets_counts_use_the_file_signature_cache(self):
        audit.record('review.link', 'Linked', project='Tether', actor={'source': 'ui', 'tab': 'Review'})
        audit.record('review.link', 'Linked again', project='Tether', actor={'source': 'ui', 'tab': 'Review'})
        with mock.patch.object(audit, '_entries', wraps=audit._entries) as read:
            facets = audit.facets()
            self.assertEqual(facets, {'kinds': [{'id': 'review.link', 'group': 'reviews', 'count': 2}],
                                      'projects': ['Tether'], 'actors': ['Review']})
            self.assertEqual(audit.facets(), facets)
            read.assert_called_once()
            audit.record('review.stop', 'Stopped', project='Other', actor={'source': 'ui', 'device': 'Chrome on Mac'})
            self.assertEqual(audit.facets()['actors'], ['Chrome on Mac', 'Review'])
            self.assertEqual(sum(not call.kwargs.get('newest_first') for call in read.call_args_list), 2)


class GetTests(unittest.TestCase):
    def test_filters_and_facets_pass_through(self):
        path = ('/mobile/api/audit?q=changed+level&kind=setting.codex_level%2C+backlog&project=Tether'
                '&actor=Mac&since=10.5&until=20&limit=3&before=last&facets=1')
        self.assertTrue(audit.handles(path))
        self.assertFalse(audit.handles('/mobile/api/audits'))
        handler = SimpleNamespace(path=path, respond=mock.Mock())
        with (mock.patch.object(audit, 'query', return_value={'entries': [], 'next': None}) as query,
              mock.patch.object(audit, 'facets', return_value={'kinds': [], 'projects': [], 'actors': []}) as facets):
            audit.get(handler)
        query.assert_called_once_with(text='changed level', kinds=['setting.codex_level', 'backlog'], project='Tether',
                                      actor='Mac', since=10.5, until=20., limit=3, before='last')
        facets.assert_called_once_with()
        handler.respond.assert_called_once_with(200, {'entries': [], 'next': None, 'retention_days': 90,
                                                      'facets': {'kinds': [], 'projects': [], 'actors': []}})

    def test_bad_number_returns_400(self):
        for suffix in ('since=soon', 'until=nan', 'limit=3.0', 'limit=201', 'limit=', 'limit=0'):
            handler = SimpleNamespace(path=f'/mobile/api/audit?{suffix}', respond=mock.Mock())
            audit.get(handler)
            status, body = handler.respond.call_args.args
            self.assertEqual(status, 400, suffix)
            self.assertIn('error', body)

    def test_read_oserror_returns_500(self):
        handler = SimpleNamespace(path='/mobile/api/audit', respond=mock.Mock())
        with mock.patch.object(audit, 'query', side_effect=OSError):
            audit.get(handler)
        handler.respond.assert_called_once_with(500, {'error': 'Tether could not read the audit log.'})
