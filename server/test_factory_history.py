import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import auth
import factory_history as store
import paths
import server


GET = '/mobile/api/factory'
POST = GET + '/history'
NOW = 1794884700123


def move(task, phase, **extra):
    return {'task': task, 'phase': phase, **extra}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(mock.patch.object(paths, 'TETHER', Path(folder)))
        self.clock = self.enterContext(mock.patch.object(store.time, 'time_ns', return_value=NOW * 1_000_000))

    def record(self, *moves):
        store.record({'moves': list(moves)})

    def request(self, method, path, raw=b'', **headers):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.command = method
        handler.client_address = ('127.0.0.1', 12345)
        handler.headers = {'Host': 'tether.test', 'Origin': 'http://tether.test', 'X-Codex-Mobile': '1',
                           'Cookie': f'{auth.COOKIE}={auth.session_value("test-token")}',
                           'Content-Length': str(len(raw)), **headers}
        handler.rfile = io.BytesIO(raw)
        handler.respond = mock.Mock()
        with mock.patch.object(auth, 'MODE', 'token'), mock.patch.object(auth, 'token', return_value='test-token'):
            getattr(handler, f'do_{method}')()
        handler.respond.assert_called_once()
        return handler.respond.call_args.args[:2]

    def test_server_time_and_only_phase_changes_are_appended(self):
        self.record(move('B12', 'build', at=3), move('%27', 'spec'))
        self.clock.return_value += 6_000_000
        with mock.patch.object(store, '_write', wraps=store._write) as write:
            self.record(move('B12', 'build'), move('%27', 'spec'))
            write.assert_not_called()
        self.record(move('B12', 'verify'), move('B12', 'build'), move('%27', 'review', at='ignored'))
        self.assertEqual(store.history(), {
            'B12': [{'phase': 'build', 'at': NOW}, {'phase': 'verify', 'at': NOW + 6},
                    {'phase': 'build', 'at': NOW + 6}],
            '%27': [{'phase': 'spec', 'at': NOW}, {'phase': 'review', 'at': NOW + 6}],
        })

    def test_caps_keep_the_latest_40_phases_and_latest_tasks(self):
        moves = [move('B1', store.PHASES[i % 7]) for i in range(45)]
        self.record(*moves)
        self.assertEqual([entry['phase'] for entry in store.history()['B1']], [m['phase'] for m in moves[-40:]])
        with mock.patch.object(store, 'MAX_TASKS', 3):
            self.record(move('%1', 'done'), move('%2', 'inbox'), move('B1', 'verify'), move('%3', 'done'))
        self.assertEqual(set(store.history()), {'%2', 'B1', '%3'})

    def test_prune_uses_the_last_entry_and_expires_before_recording(self):
        self.record(move('B2', 'build'), move('%3', 'review'))
        self.clock.return_value = (NOW + store.WEEK_MS) * 1_000_000
        self.assertEqual(set(store.history()), {'B2', '%3'})
        self.record(move('%3', 'verify'))
        self.clock.return_value += 1_000_000
        self.assertEqual(set(store.history()), {'%3'})
        self.record(move('B2', 'build'))
        data = json.loads(store.file().read_text())
        self.assertEqual(len(data['%3']), 2)
        self.assertEqual(data['B2'], [{'phase': 'build', 'at': NOW + store.WEEK_MS + 1}])
        self.clock.return_value += (store.WEEK_MS + 1) * 1_000_000
        self.record()
        self.assertEqual(json.loads(store.file().read_text()), {})

    def test_bad_bodies_return_400_without_applying_any_moves(self):
        self.record(move('B1', 'spec'))
        invalid_moves = [None, {}, move(1, 'build'), move('worker', 'build'), move('B1\n', 'build'),
                         move('%１２', 'build'), move('B' + '9' * 20, 'build'), move('%2', 'Build'),
                         move('%2', []), move('%2', 'spec', other=True)]
        bodies = [None, [], {}, {'moves': {}}, {'moves': [], 'extra': 1},
                  {'moves': [move('B1', 'build')] * (store.MAX_MOVES + 1)}]
        bodies += [{'moves': [move('B1', 'build'), bad]} for bad in invalid_moves]
        for raw in (b'', b'{', b' ' * (store.MAX_BODY + 1), *(json.dumps(body).encode() for body in bodies)):
            with self.subTest(body=raw[:120]):
                self.assertEqual(self.request('POST', POST, raw)[0], 400)
        self.assertEqual(store.history(), {'B1': [{'phase': 'spec', 'at': NOW}]})
        self.assertEqual(self.request('POST', POST, json.dumps({'moves': [move('B' + '9' * 19, 'build')]}).encode())[0], 200)

    def test_write_requires_cookie_same_origin_and_header_and_get_returns_history(self):
        raw = json.dumps({'moves': [move('B5', 'review', at=17), move('%6', 'deploy')]}).encode()
        self.assertEqual(self.request('POST', POST, raw, Cookie='bad')[0], 401)
        for headers in ({'Origin': 'http://elsewhere.test'}, {'Origin': ''}, {'X-Codex-Mobile': ''}):
            self.assertEqual(self.request('POST', POST, raw, **headers)[0], 403)
        self.assertEqual(store.history(), {})
        self.assertEqual(self.request('POST', POST, raw), (200, {'ok': True}))
        with mock.patch.object(server, 'sessions', return_value=[]):
            status, body = self.request('GET', GET)
        self.assertEqual(status, 200)
        self.assertEqual(body['history'], {'B5': [{'phase': 'review', 'at': NOW}], '%6': [{'phase': 'deploy', 'at': NOW}]})

    def test_concurrent_appends_hold_the_lock_and_keep_all_tasks(self):
        count = 8
        barrier = threading.Barrier(count)
        write = store._write

        def checked_write(target, data):
            self.assertTrue(store._lock.locked())
            write(target, data)

        def submit(n):
            barrier.wait(timeout=5)
            self.record(move('B535', 'review'), move(f'%{n}', 'verify'))

        with mock.patch.object(store, '_write', side_effect=checked_write), ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(submit, range(count), timeout=10))
        self.assertEqual(store.history(), {**{f'%{n}': [{'phase': 'verify', 'at': NOW}] for n in range(count)},
                                          'B535': [{'phase': 'review', 'at': NOW}]})

    def test_unreadable_file_keeps_the_view_and_the_next_write_starts_over(self):
        for broken in ('{not json', '[]', '{"B1": []}', '{"B1": [{"phase": "lunch", "at": 1}]}', '{"B1": [{"phase": "spec"}]}'):
            with self.subTest(broken=broken):
                store.file().write_text(broken, encoding='utf-8')
                self.assertRaises(OSError, store.history)
                with mock.patch.object(server, 'sessions', return_value=[]):
                    status, body = self.request('GET', GET)
                self.assertEqual((status, body['history']), (200, {}))
                self.record(move('B2', 'spec'))
                self.assertEqual(store.history(), {'B2': [{'phase': 'spec', 'at': NOW}]})
                self.assertEqual(Path(str(store.file()) + '.bad').read_text(encoding='utf-8'), broken)
                store.file().unlink()

    def test_failed_replace_keeps_the_previous_file(self):
        self.record(move('B9', 'spec'))
        raw = json.dumps({'moves': [move('B9', 'build')]}).encode()
        with mock.patch.object(store.os, 'replace', side_effect=OSError):
            self.assertEqual(self.request('POST', POST, raw)[0], 503)
        self.assertEqual(store.history(), {'B9': [{'phase': 'spec', 'at': NOW}]})
        self.assertEqual(list(paths.TETHER.iterdir()), [store.file()])
