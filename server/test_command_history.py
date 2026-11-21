import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import datetime, timezone
from http.client import HTTPMessage
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import auth
import command_history as history
import paths
import server


def stamp(year, month, day=1):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp()) * 1000


def entry(entry_id='entry-000', **fields):
    return {'id': entry_id, 'channel': 'typed', 'source': 'browser speech', 'text': 'Check the build.', **fields}


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.directory = self.root / 'command'
        self.enterContext(patch.object(paths, 'CONFIG', self.root))
        self.now = self.enterContext(patch.object(history, '_timestamp', return_value=stamp(2026, 11)))


class StoreTests(HistoryFixture):
    def test_validation_refuses_unknown_fields_invalid_nested_values_and_overflow(self):
        for fields in (
            {'unknown': ''}, {'audio': 'encoded'}, {'id': 'short'}, {'id': 'invalid.id'},
            {'channel': 'other'}, {'source': 'Friday'}, {'text': 'x' * 4001}, {'answer': 'x' * 4001},
            {'text': '😀' * 4000, 'answer': '😀' * 4000}, {'text': ' ', 'answer': '\n'},
            {'route': {'audio': 'encoded'}}, {'route': {'target': 'x' * 201}}, {'route': {'ask': 5}},
            {'result': {'status': 'unknown'}}, {'result': {'reason': 'Reason only'}},
            {'result': {'status': 'sent', 'reason': 'x' * 501}},
            {'result': {'status': 'sent', 'audio': 'encoded'}}, {'turn': True}, {'call': 'x' * 65},
            {'project': 'x' * 201}, {'device': 12},
        ):
            with self.subTest(fields=tuple(fields)), self.assertRaises(ValueError):
                history.append(entry(**fields))
        self.assertEqual(list(self.directory.glob('history-*')), [])

    def test_append_uses_utc_server_time_private_modes_and_deduplicates_across_new_year(self):
        self.now.return_value = stamp(2026, 12, 31) + 86_399_999
        first = history.append(entry(time='ignored', route={'kind': 'task', 'target': 'Build'},
                                     result={'status': 'sent', 'reason': 'Started'}))
        december = self.directory / 'history-2026-12.jsonl'
        self.assertEqual(first['time'], self.now.return_value)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(december.stat().st_mode), 0o600)
        self.assertEqual(json.loads(december.read_bytes()), first)
        self.assertEqual(history.append(entry(text='Retried.')), first)
        self.now.return_value += 1
        self.assertEqual(history.append(entry()), first)
        january = self.directory / 'history-2027-01.jsonl'
        self.assertFalse(january.exists())
        second = history.append(entry('entry-001', text='', answer='Done.', call='call', turn=1,
                                      project='Tether', device='Browser'))
        self.assertEqual(json.loads(january.read_bytes()), second)
        self.assertEqual(len(december.read_bytes().splitlines()), 1)
        self.now.return_value = stamp(2027, 2)
        self.assertEqual(history.append(entry())['time'], stamp(2027, 2))

    def test_search_matches_text_answer_target_and_task_with_filters(self):
        cases = [
            entry('entry-100', text='Uses Straẞe', channel='dictation', project='A',
                  result={'status': 'refused'}),
            entry('entry-101', answer='STRASSE', channel='dictation', project='A',
                  result={'status': 'cancelled'}),
            entry('entry-102', route={'target': 'On Strasse'}, project='A', result={'status': 'sent'}),
            entry('entry-103', route={'task': 'strasse checks'}, project='a'),
            entry('entry-104', route={'kind': 'strasse'}, project='A'),
        ]
        for case in cases:
            history.append(case)
        self.assertEqual(self.ids(query='strasse'), ['entry-103', 'entry-102', 'entry-101', 'entry-100'])
        self.assertEqual(self.ids(query='STRASSE', channel='dictation', failed=True, project='A'),
                         ['entry-101', 'entry-100'])
        self.assertEqual(self.ids(project='a'), ['entry-103'])
        self.assertEqual(self.ids(channel='conversation'), [])

    def ids(self, **params):
        return [row['id'] for row in history.search(**params)['entries']]

    def test_search_pages_newest_first_with_same_timestamp_and_skips_bad_lines(self):
        self.now.return_value = stamp(2026, 10)
        older = history.append(entry('older-000'))
        self.now.return_value = stamp(2026, 11)
        entries = [history.append(entry(f'entry-{i:03}')) for i in reversed(range(51))]
        with (self.directory / 'history-2026-11.jsonl').open('ab') as file:
            file.write(b'bad JSON\n{"time": 4}\n\xff\n')
        open_lines = history._lines

        def verify_month(path):
            self.assertEqual(path.name, 'history-2026-11.jsonl')
            return open_lines(path)

        with self.assertLogs(history.LOG, level='WARNING') as log, patch.object(history, '_lines', verify_month):
            page = history.search(limit=500)
        self.assertEqual(len(page['entries']), 50)
        self.assertIn('Skipped 3 unreadable', log.output[0])
        self.assertEqual(page['entries'], entries[:50])
        self.assertIsInstance(page['next'], str)
        with self.assertLogs(history.LOG, level='WARNING'):
            self.assertEqual(history.search(before=page['next']), {'entries': [entries[-1], older], 'next': None})
        with self.assertRaises(ValueError):
            history.search(before='invalid')

    def test_append_after_truncated_line_keeps_the_next_entry_readable(self):
        history.append(entry())
        path = self.directory / 'history-2026-11.jsonl'
        with path.open('ab') as file:
            file.write(b'{"id":')
        with self.assertLogs(history.LOG, level='WARNING'):
            second = history.append(entry('entry-001'))
        with self.assertLogs(history.LOG, level='WARNING'):
            self.assertEqual(history.search()['entries'][0], second)

    def test_selective_deletes_match_their_fields_and_remove_an_empty_month(self):
        self.now.return_value = stamp(2026, 10)
        start = history.append(entry('range-001'))
        self.now.return_value = stamp(2026, 10, 14)
        end = history.append(entry('range-002'))
        old = self.directory / 'history-2026-10.jsonl'
        self.now.return_value = stamp(2026, 11)
        history.append(entry('remove-id'))
        history.append(entry('project-A', project='A'))
        history.append(entry('project-a', project='a'))
        history.append(entry('keyword-1', route={'target': 'Documentation'}))
        history.append(entry('keyword-2', answer='New document'))
        self.assertEqual(history.delete_ids(['remove-id', 'unmatched']), 1)
        self.assertEqual(history.delete_range(start['time'], end['time']), 2)
        self.assertFalse(old.exists())
        self.assertEqual(history.delete_project('A'), 1)
        self.assertEqual(history.delete_keyword('DOCUMENT'), 2)
        self.assertEqual(self.ids(), ['project-a'])
        self.assertEqual(stat.S_IMODE((self.directory / 'history-2026-11.jsonl').stat().st_mode), 0o600)
        self.assertEqual(history.delete_all(), 1)
        self.assertEqual(history.delete_all(), 0)
        self.assertFalse(list(self.directory.glob('history-*')))

    def test_delete_all_clears_corrupt_lines_and_preserves_the_token(self):
        token = history.friday_token()
        history.append(entry())
        with (self.directory / 'history-2026-11.jsonl').open('ab') as file:
            file.write(b'corrupt\n')
        with self.assertLogs(history.LOG, level='WARNING'):
            self.assertEqual(history.delete_all(), 1)
        self.assertEqual(list(self.directory.glob('history-*')), [])
        self.assertEqual(history.friday_token(), token)

    def test_token_creation_permissions_reuse_and_race(self):
        token = history.friday_token()
        path = self.directory / 'friday-token'
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(), token)
        self.assertEqual(history.friday_token(), token)
        path.unlink()
        real_open = os.open

        def competing_create(name, flags, mode):
            if name == path:
                with os.fdopen(real_open(name, flags, mode), 'w') as file:
                    file.write(token)
                raise FileExistsError
            return real_open(name, flags, mode)

        with patch.object(history.os, 'open', side_effect=competing_create):
            self.assertEqual(history.friday_token(), token)

    def test_two_processes_append_complete_lines_and_dedupe_a_shared_id(self):
        script = """
import command_history
import sys

for number in range(30):
    command_history.append(dict(id=f'{sys.argv[1]}-{number:03}', text='é' * 4000,
                                channel='conversation', source='friday'))
command_history.append(dict(id='shared-id', text=sys.argv[1], channel='conversation', source='friday'))
"""
        env = dict(os.environ, MOBILE_TMUX_CONFIG=str(self.root))
        workers = []
        try:
            for name in ('first', 'second'):
                workers.append(subprocess.Popen([sys.executable, '-c', script, name],
                                                cwd=Path(__file__).parent, env=env))
            for worker in workers:
                self.assertEqual(worker.wait(timeout=15), 0)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=5)
        ids = [json.loads(line)['id'] for path in self.directory.glob('history-*')
               for line in path.read_bytes().splitlines()]
        self.assertCountEqual(ids, [f'{name}-{n:03}' for name in ('first', 'second') for n in range(30)] + ['shared-id'])


class RouteTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.token = history.friday_token()
        self.enterContext(patch.object(auth, 'MODE', 'token'))
        self.enterContext(patch.object(auth, 'session_value', return_value='browser-session'))
        self.browser = {'Host': 'localhost', 'Cookie': f'{auth.COOKIE}=browser-session',
                        'Origin': 'http://localhost', 'X-Codex-Mobile': '1'}
        self.friday = {history.FRIDAY_HEADER: self.token}

    def request(self, path, payload=None, headers=None, peer='127.0.0.1', method='POST'):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = path, method
        handler.client_address = (peer, 1234)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        handler.headers = HTTPMessage()
        for name, value in {'Content-Length': str(len(raw)), **(headers or {})}.items():
            handler.headers[name] = value
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        getattr(handler, f'do_{method}')()
        handler.send_response.assert_called_once()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_friday_route_appends_with_forced_channel_and_source_and_touches_no_other_handler(self):
        body = {'id': 'friday-001', 'answer': 'The build is ready.', 'result': {'status': 'answered'},
                'call': 'call', 'turn': 2, 'route': {'task': 'Build'}, 'project': 'Tether'}
        with (patch.object(server.Handler, 'deny', side_effect=AssertionError),
              patch.object(server, 'forget_sessions', side_effect=AssertionError),
              patch.object(server.undo_api, 'note_start', side_effect=AssertionError),
              patch.object(server.undo_api, 'headers', side_effect=AssertionError),
              patch.object(server.subprocess, 'run', side_effect=AssertionError)):
            self.assertEqual(self.request(history.FRIDAY_PATH, body, self.friday),
                             (200, {'ok': True, 'id': body['id']}))
            self.assertEqual(self.request(history.FRIDAY_PATH, body, self.friday, peer='::1')[0], 200)
        self.assertEqual(history.search()['entries'], [
            dict(body, channel='conversation', source='friday', time=self.now.return_value)])

    def test_friday_route_refuses_wrong_or_missing_token_non_loopback_and_proxy_headers(self):
        tests = [({}, '127.0.0.1'), ({history.FRIDAY_HEADER: 'wrong'}, '127.0.0.1'),
                 ({history.FRIDAY_HEADER: 'é'}, '127.0.0.1'), (self.friday, '192.0.2.17')]
        tests.extend((dict(self.friday, **{header: ''}), '127.0.0.1')
                     for header in ('origin', 'Cookie', 'X-Forwarded-For', 'Forwarded'))
        for headers, peer in tests:
            with self.subTest(headers=headers.keys(), peer=peer):
                self.assertEqual(self.request(history.FRIDAY_PATH, {'id': 'friday-001', 'text': 'Check it.'},
                                              headers, peer)[0], 403)
        self.assertEqual(history.search()['entries'], [])

    def test_friday_route_rejects_invalid_bodies_and_reports_storage_errors(self):
        body = {'id': 'friday-001', 'text': 'Check it.'}
        for invalid in (b'{', b' ' * (history.MAX_BYTES + 1), [], dict(body, text=' '),
                        dict(body, channel='typed'), dict(body, source='browser speech'),
                        dict(body, time=4), dict(body, device='Browser')):
            with self.subTest(kind=type(invalid)):
                self.assertEqual(self.request(history.FRIDAY_PATH, invalid, self.friday)[0], 400)
        with patch.object(history, 'append', side_effect=OSError):
            self.assertEqual(self.request(history.FRIDAY_PATH, body, self.friday)[0], 503)
        with patch.object(history, 'friday_token', side_effect=OSError):
            self.assertEqual(self.request(history.FRIDAY_PATH, body, self.friday)[0], 503)
        self.assertEqual(history.search()['entries'], [])

    def test_friday_token_cannot_read_delete_or_send_a_message_even_in_proxy_mode(self):
        for mode in ('token', 'proxy'):
            with self.subTest(mode=mode), patch.object(auth, 'MODE', mode):
                for path, method, body in ((history.PATH + '?q=hi', 'GET', None),
                                           (history.DELETE_PATH, 'POST', {'all': True}),
                                           ('/mobile/api/message', 'POST', {'text': 'hi'})):
                    self.assertEqual(self.request(path, body, self.friday, method=method)[0], 403)

    def test_browser_search_and_deletes_require_the_normal_browser_credentials(self):
        self.assertEqual(self.request(history.PATH, method='GET')[0], 401)
        self.assertEqual(self.request(history.DELETE_PATH, {'all': True})[0], 401)
        for change in ({'Origin': ''}, {'Origin': 'https://elsewhere.test'}, {'X-Codex-Mobile': ''}):
            self.assertEqual(self.request(history.DELETE_PATH, {'all': True}, self.browser | change)[0], 403)
        saved = history.append(entry(project='Tether', result={'status': 'refused'}))
        self.assertEqual(self.request(history.PATH + '?q=BUILD&channel=typed&failed=1&project=Tether',
                                      headers=self.browser, method='GET'),
                         (200, {'entries': [saved], 'next': None}))
        self.assertEqual(self.request(history.PATH + '?before=invalid', headers=self.browser, method='GET')[0], 400)

    def test_browser_delete_accepts_each_operation_and_rejects_mixed_or_malformed_operations(self):
        history.append(entry(project='Tether'))
        for payload in ({'ids': ['entry-000'] * 501}, {'ids': ['bad']}, {'ids': 'entry-000'},
                        {'from': True, 'to': 5}, {'from': 6, 'to': 5}, {'from': 0}, {'project': ['Tether']},
                        {'keyword': ' '}, {'all': False}, {'all': 1}, {'all': True, 'keyword': 'build'}, {}):
            self.assertEqual(self.request(history.DELETE_PATH, payload, self.browser)[0], 400)
        time = self.now.return_value
        for payload in ({'ids': ['entry-000']}, {'from': time, 'to': time}, {'project': 'Tether'},
                        {'keyword': 'BUILD'}, {'all': True}):
            with self.subTest(kind=tuple(payload)):
                history.append(entry(project='Tether'))
                self.assertEqual(self.request(history.DELETE_PATH, payload, self.browser), (200, {'removed': 1}))
                self.assertEqual(history.search()['entries'], [])
