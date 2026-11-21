"""Topic note tests use a private store and in-memory HTTP requests."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import friday_notes as notes
import paths
import server

NOW = 1_800_000_000


class FridayNotesTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.folder = Path(root) / 'friday-notes'
        self.path = self.folder / 'notes.json'
        self.enterContext(patch.dict(os.environ, {'TETHER_FRIDAY_NOTES': str(self.folder)}))

    def hold(self, title, texts=None, **kwargs):
        return notes.hold(title, ['Use the new port.'] if texts is None else texts, **{'now': NOW, **kwargs})

    def test_paths_and_empty_reads_do_not_create_a_store(self):
        self.assertEqual(paths.friday_notes(), self.folder)
        with patch.dict(os.environ, {'TETHER_FRIDAY_NOTES': ''}):
            self.assertEqual(paths.friday_notes(), paths.TETHER / 'friday-notes')
        self.assertEqual(notes.listing(), {'topics': []})
        self.assertEqual(notes.pinned(), [])
        self.assertFalse(self.folder.exists())

    def test_hold_creates_then_appends_by_exact_name_and_whole_words(self):
        first = self.hold('  Urgent \t Call  Spec ', ['Keep Q1.'], chat='Spec Chat')
        self.assertEqual(first, {
            'id': 'T1', 'title': 'Urgent Call Spec', 'chat': 'Spec Chat',
            'notes': [{'text': 'Keep Q1.', 'at': NOW}], 'state': 'open',
            'created': NOW, 'updated': NOW, 'sent_at': None, 'sent_to': None, 'draft': None,
        })
        self.hold('URGENT-call spec', ['Keep Q2.'], now=NOW + 1)
        self.hold('call spec', ['Keep Q3.'], now=NOW + 2)
        saved = self.hold('The urgent CALL spec chat', ['Keep Q4.'], chat='New Chat', now=NOW + 3)
        self.assertEqual((saved['id'], saved['chat'], saved['created'], saved['updated']),
                         ('T1', 'New Chat', NOW, NOW + 3))
        self.assertEqual([note['text'] for note in saved['notes']], [f'Keep Q{i}.' for i in range(1, 5)])
        for title, expected in (('Specifier', 'T2'), ('Straße changes', 'T3'), ('STRASSE changes', 'T3')):
            self.assertEqual(self.hold(title)['id'], expected)

    def test_ambiguous_containment_refuses_writes_and_exact_match_wins(self):
        first = self.hold('API Server')
        second = self.hold('API Client')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(notes.NotesError, r'^Several topics match: API Server, API Client\. Which one\?$'):
            self.hold('api')
        with self.assertRaises(notes.NotesError):
            notes.drop('api')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.hold('API client')['id'], second['id'])
        short = {**first, 'title': 'API', 'id': 'T3'}
        self.assertIs(notes.find([first, second, short], 'aPi'), short)

    def test_all_write_limits_and_no_partial_append(self):
        for title, batch in (('', ['note']), (' \t ', ['note']), (False, ['note']), ('x' * 61, ['note']),
                             ('Topic', []), ('Topic', 'text'), ('Topic', ['note'] * 6),
                             ('Topic', [None]), ('Topic', ['  ']), ('Topic', ['x' * 1001])):
            with self.subTest(title=title, count=len(batch)), self.assertRaises(notes.NotesError) as exc:
                self.hold(title, batch)
            self.assertEqual(exc.exception.status, 400)
        self.assertFalse(self.path.exists())
        title = 'x' * 60
        for _ in range(10):
            saved = self.hold(title, ['n' * 1000] * 5)
        self.assertEqual(len(saved['notes']), 50)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(notes.NotesError, '50 notes'):
            self.hold(title)
        self.assertEqual(self.path.read_bytes(), before)
        for i in range(19):
            self.hold(f'Topic-{i}')
        self.hold('Topic-1', ['Append at capacity.'])
        with self.assertRaisesRegex(notes.NotesError, '20 open topics'):
            self.hold('Topic-19')
        self.assertEqual(len(notes.listing()['topics']), 20)

    def test_drop_uses_one_based_numbers_and_leaves_empty_topics_open(self):
        self.hold('Server', ['First', 'Second'])
        for invalid in (0, 3, -1, True, '1', 1.0):
            with self.subTest(invalid=invalid), self.assertRaises(notes.NotesError) as exc:
                notes.drop('Server', invalid, now=NOW)
            self.assertEqual(exc.exception.status, 400)
        self.assertEqual(notes.drop('server', 2, now=NOW + 1), {'dropped': 'note', 'topic': 'Server', 'left': 1})
        saved, = notes.listing()['topics']
        self.assertEqual(saved['notes'], [{'text': 'First', 'at': NOW}])
        self.assertEqual(saved['updated'], NOW + 1)
        self.assertEqual(notes.drop('server', 1, now=NOW + 2)['left'], 0)
        self.assertEqual(notes.pinned(), [{'topic': 'Server', 'notes': []}])
        self.assertEqual(notes.drop('server', now=NOW + 3), {'dropped': 'topic', 'topic': 'Server', 'left': 0})
        self.assertEqual(notes.listing(), {'topics': []})
        self.assertEqual(self.hold('Server')['id'], 'T2')

    def test_missing_topic_lists_at_most_five_open_titles(self):
        for i in range(6):
            self.hold(f'Topic-{i}', now=NOW + i)
        with self.assertRaises(notes.NotesError) as exc:
            notes.drop('Other')
        self.assertEqual(exc.exception.status, 404)
        self.assertEqual(str(exc.exception),
                         'No topic matches Other. Open topics: Topic-5, Topic-4, Topic-3, Topic-2, Topic-1.')

    def test_drop_topic_by_id_only_removes_an_open_topic(self):
        opened = self.hold('Open')
        sent = self.hold('Sent')
        data = json.loads(self.path.read_text())
        data['topics'][1].update(state='sent', sent_at=NOW, sent_to='Docs')
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        for topic_id in ('T99', sent['id']):
            with self.subTest(topic_id=topic_id):
                with self.assertRaises(notes.NotesError) as exc:
                    notes.drop(None, now=NOW, topic_id=topic_id)
                self.assertEqual(exc.exception.status, 409)
                self.assertEqual(self.path.read_bytes(), before)
        before = self.path.read_bytes()
        for count in (0, 2):  # The topic holds one note: a count from an older read-back refuses the drop.
            with self.subTest(count=count):
                with self.assertRaises(notes.NotesError) as exc:
                    notes.drop(None, now=NOW, topic_id=opened['id'], count=count)
                self.assertEqual(exc.exception.status, 409)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(notes.drop(None, now=NOW, topic_id=opened['id'], count=1),
                         {'dropped': 'topic', 'topic': 'Open', 'left': 0})
        self.assertEqual([topic['id'] for topic in notes.listing(NOW)['topics']], [sent['id']])

    def test_drop_note_by_id_and_number_requires_matching_text(self):
        topic = self.hold('Server', ['First', 'Second'])
        before = self.path.read_bytes()
        with self.assertRaises(notes.NotesError) as exc:
            notes.drop(None, 2, now=NOW + 1, topic_id=topic['id'], text='First')
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(notes.drop(None, 2, now=NOW + 1, topic_id=topic['id'], text='Second'),
                         {'dropped': 'note', 'topic': 'Server', 'left': 1})
        saved, = notes.listing(NOW + 1)['topics']
        self.assertEqual(saved['notes'], [{'text': 'First', 'at': NOW}])

    def test_sent_retention_order_and_pruning_on_next_save(self):
        self.hold('Old open', now=NOW - 365 * 86400)
        self.hold('New open', now=NOW - 100)
        self.hold('Stale sent')
        self.hold('Recent sent')
        data = json.loads(self.path.read_text())
        data['topics'][2].update(state='sent', sent_at=NOW - notes.SENT_AGE - 1, sent_to='Finished')
        data['topics'][3].update(state='sent', sent_at=NOW - notes.SENT_AGE, sent_to='Finished')
        self.path.write_text(json.dumps(data))
        self.assertEqual([t['title'] for t in notes.listing(NOW)['topics']], ['New open', 'Old open', 'Recent sent'])
        self.assertEqual([t['topic'] for t in notes.pinned(NOW)], ['New open', 'Old open'])
        self.assertEqual(len(json.loads(self.path.read_text())['topics']), 4)
        self.assertEqual(self.hold('Later', now=NOW + 1)['id'], 'T5')
        self.assertEqual([t['title'] for t in json.loads(self.path.read_text())['topics']],
                         ['Old open', 'New open', 'Later'])
        self.assertEqual(len(notes.listing(NOW + 365 * 86400)['topics']), 3)

    def test_corrupt_json_reads_empty_and_writes_leave_bytes_intact(self):
        self.folder.mkdir()
        for corrupt in (b'{broken', b'\xff', b'[' * 2500):
            with self.subTest(corrupt=corrupt[:10]):
                self.path.write_bytes(corrupt)
                self.assertEqual(notes.listing(), {'topics': []})
                self.assertEqual(notes.pinned(), [])
                for write in (lambda: self.hold('Topic'), lambda: notes.drop('Topic')):
                    with self.assertRaises(notes.NotesError) as exc:
                        write()
                    self.assertEqual(exc.exception.status, 503)
                    self.assertEqual(self.path.read_bytes(), corrupt)

    def test_load_discards_malformed_records_and_keeps_valid_neighbors(self):
        good = self.hold('Valid')
        data = {'version': 1, 'next': 1, 'topics': [None, {}, good, {**good, 'id': 'T8', 'notes': 5},
                {**good, 'id': 'T9', 'state': 'sent'}, {**good, 'id': 'T10', 'updated': float('inf')}]}
        self.path.write_text(json.dumps(data))
        self.assertEqual(notes.listing(), {'topics': [good]})
        self.assertEqual(self.hold('Fresh')['id'], 'T2')
        self.path.write_text('{"topics": true}')
        self.assertEqual(notes.listing(), {'topics': []})

    def test_pinned_drops_globally_oldest_and_keeps_newest_in_each_topic(self):
        first, second = 'α' * 120, '"' * 300
        self.hold('First', [first], now=NOW)
        self.hold('Second', [second], chat='Destination', now=NOW + 1)
        self.hold('First', ['Middle', 'Newest first'], now=NOW + 2)
        self.hold('Second', ['Newest second'], now=NOW + 3)
        before = self.path.read_bytes()
        both_cut = [{'topic': 'Second', 'chat': 'Destination', 'notes': ['Newest second'], 'more': 1},
                    {'topic': 'First', 'notes': ['Middle', 'Newest first'], 'more': 1}]
        self.assertEqual(notes.pinned(budget=len(json.dumps(both_cut))), both_cut)
        self.assertEqual(notes.pinned(budget=1), [both_cut[0], {
            'topic': 'First', 'notes': ['Newest first'], 'more': 2,
        }])
        self.assertEqual(notes.pinned()[1]['notes'], [first, 'Middle', 'Newest first'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_lock_serializes_writes_and_atomic_save_cleans_up(self):
        self.hold('Concurrent', ['Start'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.hold('Concurrent', [str(i)]), range(12)))
        topic, = notes.listing()['topics']
        self.assertCountEqual([note['text'] for note in topic['notes']], ['Start'] + list(map(str, range(12))))
        before = self.path.read_bytes()
        with patch.object(notes.os, 'replace', side_effect=PermissionError), self.assertRaises(PermissionError):
            self.hold('Concurrent', ['Discard this failed write.'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({file.name for file in self.folder.iterdir()}, {'.lock', 'notes.json'})
        self.assertEqual(S_IMODE(self.folder.stat().st_mode), 0o700)
        for file in self.folder.iterdir():
            self.assertEqual(S_IMODE(file.stat().st_mode), 0o600)

    def test_http_routes_preserve_auth_origin_and_write_header(self):
        self.enterContext(patch.object(server.auth, 'MODE', 'token'))
        self.enterContext(patch.object(server.auth, 'token', return_value='notes-test-secret'))

        def request(method, suffix='', body=None, *, signed_in=True, write_header=True, origin='https://phone.test'):
            handler = object.__new__(server.Handler)
            handler.client_address = ('127.0.0.1', 1)
            handler.log_request = Mock()
            data = json.dumps(body).encode() if body is not None else b''
            headers = [f'{method} {notes.PREFIX}{suffix} HTTP/1.1', 'Host: phone.test',
                       f'Origin: {origin}', f'Content-Length: {len(data)}']
            if signed_in:
                headers.append(f'Cookie: {server.auth.COOKIE}={server.auth.session_value()}')
            if write_header:
                headers.append('X-Codex-Mobile: 1')
            handler.rfile = BytesIO('\r\n'.join(headers + ['', '']).encode() + data)
            handler.wfile = BytesIO()
            handler.handle_one_request()
            head, payload = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
            return int(head.split()[1]), json.loads(payload)

        batch = {'topic': 'Urgent Call Spec', 'chat': 'Urgent call', 'notes': ['Keep my answer.']}
        self.assertEqual(request('GET', signed_in=False)[0], 401)
        self.assertEqual(request('GET'), (200, {'topics': []}))
        for options, status in (({'write_header': False}, 403), ({'origin': 'https://other.test'}, 403),
                                ({'signed_in': False}, 401)):
            self.assertEqual(request('POST', '/hold', batch, **options)[0], status)
        self.assertFalse(self.path.exists())
        status, saved = request('POST', '/hold', batch)
        self.assertEqual((status, saved['title'], saved['notes'][0]['text']), (200, batch['topic'], batch['notes'][0]))
        self.assertEqual(request('GET'), (200, {'topics': [saved]}))
        self.assertEqual(request('GET', '?' + urlencode({'topic': 'Call spec'})), (200, {'topics': [saved]}))
        self.assertEqual(request('GET', '?topic=Missing')[0], 404)
        self.assertEqual(request('POST', '/drop', {'topic': 'urgent call spec', 'note': 1}),
                         (200, {'dropped': 'note', 'topic': 'Urgent Call Spec', 'left': 0}))
        self.assertEqual(request('POST', '/hold', batch)[0], 200)
        exact = {'id': 'T1', 'note': 1, 'text': batch['notes'][0]}
        before = self.path.read_bytes()
        for body in ({**exact, 'id': 1}, {**exact, 'text': ['Keep my answer.']},
                     {**exact, 'id': None, 'topic': batch['topic']}, {**exact, 'text': None},
                     {'id': 'T1', 'count': '1'}, {'id': 'T1', 'count': True}, {'id': 'T1', 'count': None}):
            with self.subTest(body=body):
                self.assertEqual(request('POST', '/drop', body)[0], 400)
                self.assertEqual(self.path.read_bytes(), before)
        data = json.loads(before)
        data['topics'][0]['notes'][0]['text'] = 'Edited answer.'
        self.path.write_text(json.dumps(data))
        self.assertEqual(request('POST', '/drop', exact),
                         (409, {'error': 'Those notes changed. Look at the list again.'}))
        self.assertEqual(request('GET')[1]['topics'][0]['notes'][0]['text'], 'Edited answer.')
        exact['text'] = 'Edited answer.'
        self.assertEqual(request('POST', '/drop', exact),
                         (200, {'dropped': 'note', 'topic': 'Urgent Call Spec', 'left': 0}))
