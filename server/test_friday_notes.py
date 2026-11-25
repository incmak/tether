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


def revision(number=1, **fields):
    return {'rev': number, 'text': f'Full draft {number}', 'skill': None, 'summary': f'Draft {number}',
            'edit': None, 'at': NOW, **fields}


def draft(**fields):
    return {'state': 'ready', 'error': None, 'started': NOW, 'updated': NOW,
            'chat': {'pane': '%12', 'thread': '11111111-1111-4111-8111-111111111111', 'name': 'Draft Server'},
            'asked_close': False, 'revisions': [revision()], **fields}


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

    def test_drop_uses_one_based_numbers_and_removes_a_topic_with_its_last_note(self):
        self.hold('Server', ['First', 'Second'])
        for invalid in (0, 3, -1, True, '1', 1.0):
            with self.subTest(invalid=invalid), self.assertRaises(notes.NotesError) as exc:
                notes.drop('Server', invalid, now=NOW)
            self.assertEqual(exc.exception.status, 400)
        self.assertEqual(notes.drop('server', 2, now=NOW + 1), {'dropped': 'note', 'topic': 'Server', 'left': 1})
        saved, = notes.listing()['topics']
        self.assertEqual(saved['notes'], [{'text': 'First', 'at': NOW}])
        self.assertEqual(saved['updated'], NOW + 1)
        self.assertEqual(notes.drop('server', 1, now=NOW + 2), {'dropped': 'note', 'topic': 'Server', 'left': 0})
        self.assertEqual(notes.pinned(), [])
        self.assertEqual(notes.listing(), {'topics': []})
        self.assertEqual(self.hold('Server')['id'], 'T2')
        self.assertEqual(notes.drop('server', now=NOW + 3), {'dropped': 'topic', 'topic': 'Server', 'left': 0})
        # Emptying a topic frees its place: 20 topics emptied note by note leave room for a new one.
        for i in range(20):
            self.hold(f'Topic {i}')
            notes.drop(f'Topic {i}', 1, now=NOW + 4)
        self.assertEqual(self.hold('Fresh idea')['title'], 'Fresh idea')

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
        # Same count, other notes: one note removed and one added since the list showed refuses the drop.
        current = [note['text'] for topic in notes.listing(NOW)['topics'] if topic['id'] == opened['id']
                   for note in topic['notes']]
        for texts in (['A note the user never saw'], [], current * 2):
            with self.subTest(texts=texts):
                with self.assertRaises(notes.NotesError) as exc:
                    notes.drop(None, now=NOW, topic_id=opened['id'], count=1, texts=texts)
                self.assertEqual(exc.exception.status, 409)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(notes.drop(None, now=NOW, topic_id=opened['id'], count=1, texts=current),
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

    def test_load_hides_malformed_records_and_keeps_valid_neighbors(self):
        good = self.hold('Valid')
        data = {'version': 1, 'next': 1, 'topics': [None, {}, good, {**good, 'id': 'T8', 'notes': 5},
                {**good, 'id': 'T9', 'state': 'sent'}, {**good, 'id': 'T10', 'updated': float('inf')}]}
        self.path.write_text(json.dumps(data))
        self.assertEqual(notes.listing(), {'topics': [good]})
        self.assertEqual(self.hold('Fresh')['id'], 'T11')
        self.assertEqual(json.loads(self.path.read_text())['topics'][:5], data['topics'][:5])
        self.path.write_text('{"topics": true}')
        self.assertEqual(notes.listing(), {'topics': []})

    def test_draft_round_trip_hold_and_pinned_use_the_latest_revision(self):
        topic = self.hold('Server')
        saved = draft(revisions=[revision(), revision(2, text='/research\n\nFull revised prompt.',
                                                      skill='/research', edit='Add checks.')])
        data = json.loads(self.path.read_text())
        data['topics'][0]['draft'] = saved
        self.path.write_text(json.dumps(data))
        self.assertEqual(notes.listing(NOW), {'topics': [dict(topic, draft=saved)]})
        self.assertEqual(self.hold('Server', ['Extra note.'], now=NOW + 1)['draft'], saved)
        pinned, = notes.pinned(NOW + 1)
        self.assertEqual(pinned['draft'], dict(state='ready', rev=2, summary='Draft 2', skill='/research',
                                               length=len('/research\n\nFull revised prompt.')))
        self.assertNotIn('Full revised prompt.', json.dumps(pinned))
        self.assertNotIn('Full draft 1', json.dumps(pinned))

    def test_invalid_draft_fields_remain_byte_exact_and_ids_are_reserved(self):
        topic = self.hold('Valid')
        invalid = [
            'ready', draft(state='new-state'), draft(error=True), draft(error='e' * (notes.DRAFT_ERROR_LIMIT + 1)),
            draft(started=True), draft(updated=float('inf')), draft(chat=[]),
            draft(chat={'pane': '12', 'thread': 'thread', 'name': 'A'}),
            draft(chat={'pane': 12, 'thread': 'thread', 'name': 'A'}),
            draft(chat={'pane': '%12', 'thread': 3, 'name': 'A'}),
            draft(chat={'pane': '%12', 'thread': 'thread', 'name': []}),
            draft(asked_close=1), draft(revisions={}), draft(revisions=[revision(i) for i in range(1, 22)]),
            draft(revisions=[revision(2)]), draft(revisions=[revision(), revision()]),
            draft(revisions=[revision(True)]), draft(revisions=[revision(text=21)]),
            draft(revisions=[revision(text='')]), draft(revisions=[revision(text='a' * 20001)]),
            draft(revisions=[revision(skill='research')]), draft(revisions=[revision(skill=5)]),
            draft(revisions=[revision(summary=None)]), draft(revisions=[revision(summary='x' * 401)]),
            draft(revisions=[revision(edit={})]), draft(revisions=[revision(edit='x' * 1001)]),
            draft(revisions=[revision(at=False)]), draft(revisions=[{}]),
            {key: value for key, value in draft().items() if key != 'error'}, draft(extra=True),
        ]
        records = [json.dumps(dict(topic, id=f'T{i}', title=f'Hidden {i}', draft=value), separators=(' ,\r\n ', ' :  '))
                   for i, value in enumerate(invalid, 10)]
        valid = json.dumps(topic)
        ordered = [records[0], valid, *records[1:]]
        self.path.write_bytes(('{"topics":[], "metadata": {"topics": ["irrelevant"]}, "next": 1, "to\\u0070ics": ['
                               + ',\n'.join(ordered) + ']}').encode())
        self.assertEqual(notes.listing(NOW), {'topics': [topic]})
        self.assertEqual([item['topic'] for item in notes.pinned(NOW)], ['Valid'])
        self.assertIsNone(notes.find(notes.listing(NOW)['topics'], 'Hidden 10'))
        with self.assertRaisesRegex(notes.NotesError, 'No topic matches Hidden 10'):
            notes.drop('Hidden 10', now=NOW)
        self.hold('Valid', ['Still valid.'])
        fresh = self.hold('Hidden 10', ['A new topic with that title.'])
        self.assertEqual(fresh['id'], f'T{10 + len(invalid)}')
        saved = self.path.read_bytes()
        for value in records:
            self.assertTrue(value.encode() in saved, 'The original invalid record is missing.')
        offsets = [saved.index(value.encode()) for value in records]
        self.assertEqual(offsets, sorted(offsets))
        self.assertLess(offsets[0], saved.index(b'"title": "Valid"'))
        self.assertLess(saved.index(b'"title": "Valid"'), offsets[1])

    def test_stale_drafting_is_failed_on_read_and_saved_on_the_next_write(self):
        self.hold('Old job')
        self.hold('Current job')
        data = json.loads(self.path.read_text())
        data['topics'][0]['draft'] = draft(state='drafting', updated=NOW - notes.DRAFT_STALE - 1)
        data['topics'][1]['draft'] = draft(state='drafting', updated=NOW - notes.DRAFT_STALE)
        self.path.write_text(json.dumps(data))
        old, current = notes.listing(NOW)['topics']
        self.assertEqual((old['draft']['state'], old['draft']['error']),
                         ('failed', 'Drafting stopped. Ask Friday to make the prompt again.'))
        self.assertEqual(current['draft']['state'], 'drafting')
        self.assertEqual(json.loads(self.path.read_text())['topics'][0]['draft']['state'], 'drafting')
        self.hold('Current job')
        self.assertEqual(json.loads(self.path.read_text())['topics'][0]['draft'], old['draft'])

    def test_drop_would_remove_the_open_draft_identity(self):
        topic = self.hold('Server', ['First', 'Last'])
        data = json.loads(self.path.read_text())
        data['topics'][0]['draft'] = draft()
        self.path.write_text(json.dumps(data))
        self.assertEqual(notes.drop(None, 1, now=NOW, topic_id=topic['id'])['left'], 1)
        for note in (None, 1):
            with self.subTest(note=note), self.assertRaisesRegex(notes.NotesError, 'Close the draft chat first.') as exc:
                notes.drop(None, note, now=NOW, topic_id=topic['id'])
            self.assertEqual(exc.exception.status, 409)
        data = json.loads(self.path.read_text())
        data['topics'][0]['draft']['state'] = 'closed'
        self.path.write_text(json.dumps(data))
        self.assertEqual(notes.drop('Server', now=NOW)['dropped'], 'topic')

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

    def test_pinned_cuts_from_the_front_even_when_the_clock_stepped_back(self):
        # drop_notes counts from the stored order: "more": N must hide exactly notes 1..N.
        self.hold('Clock', ['One ' + 'x' * 200], now=NOW + 10)
        self.hold('Clock', ['Two ' + 'y' * 200], now=NOW)  # The clock stepped back.
        self.hold('Clock', ['Three'], now=NOW + 20)
        cut = notes.pinned(budget=300)
        self.assertEqual(cut, [{'topic': 'Clock', 'notes': ['Two ' + 'y' * 200, 'Three'], 'more': 1}])

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
        self.enterContext(patch('friday_drafts.schedule_sweep'))
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
        status, again = request('POST', '/hold', batch)
        self.assertEqual((status, again['id']), (200, 'T2'))  # The emptied topic T1 is gone.
        exact = {'id': 'T2', 'note': 1, 'text': batch['notes'][0]}
        before = self.path.read_bytes()
        for body in ({**exact, 'id': 1}, {**exact, 'text': ['Keep my answer.']},
                     {**exact, 'id': None, 'topic': batch['topic']}, {**exact, 'text': None},
                     {'id': 'T2', 'count': '1'}, {'id': 'T2', 'count': True}, {'id': 'T2', 'count': None},
                     {'id': 'T2', 'count': 1, 'texts': 'Keep my answer.'}, {'id': 'T2', 'count': 1, 'texts': [1]},
                     {'id': 'T2', 'count': 1, 'texts': None}):
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
