from collections import deque
import contextlib
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import agent_reminder_cli
import paths
import reminders
import urgent_ping

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).timestamp()  # 17:30 IST


class Handler:
    def __init__(self, path):
        self.path, self.sent = path, None

    def respond(self, status, body):
        self.sent = (status, body)


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / 'reminders'
        patcher = mock.patch.dict(os.environ, {'TETHER_REMINDERS': str(self.folder)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, text='Check usage', due=NOW + 3600, **extra):
        return reminders.add(text, due, now=NOW, **extra)

    def stored(self):
        return json.loads((self.folder / 'reminders.json').read_text())


class StoreTests(Base):
    def test_folder_comes_from_paths_with_env_override(self):
        self.assertEqual(paths.reminders(), self.folder)
        with mock.patch.dict(os.environ, {'TETHER_REMINDERS': ''}):
            self.assertEqual(paths.reminders(), paths.TETHER / 'reminders')

    def test_add_stores_utc_epoch_and_counts_ids(self):
        first = self.add(link={'pane': '%12', 'thread': 'abc-1', 'item': 'B224', 'project': 'tether'})
        second = self.add('Second')
        self.assertEqual((first['id'], second['id']), ('R1', 'R2'))
        self.assertEqual(first['state'], 'scheduled')
        self.assertEqual(first['occ'], 1)
        self.assertEqual(first['link'], {'pane': '%12', 'thread': 'abc-1', 'item': 'B224', 'project': 'tether'})
        data = self.stored()
        self.assertEqual((data['version'], data['next']), (2, 3))
        self.assertEqual(data['reminders'][0]['due'], NOW + 3600)
        self.assertEqual(os.stat(self.folder).st_mode & 0o777, 0o700)

    def test_add_refuses_bad_input(self):
        for text, due, link in (('', NOW + 60, None), ('   ', NOW + 60, None), ('x' * 501, NOW + 60, None),
                                ('ok', NOW - 1, None), ('ok', NOW, None), ('ok', 'tomorrow', None),
                                ('ok', True, None), ('ok', float('nan'), None), ('ok', NOW + 6 * 366 * 86400, None),
                                ('ok', NOW + 60, {'pane': 'x; rm'}), ('ok', NOW + 60, {'item': '../B1'}),
                                ('ok', NOW + 60, {'project': 'a/b'}), ('ok', NOW + 60, 'pane')):
            with self.subTest(text=text[:5], due=due, link=link), self.assertRaises(reminders.ReminderError):
                reminders.add(text, due, now=NOW, link=link)
        self.assertFalse((self.folder / 'reminders.json').exists())

    def test_thread_without_pane_is_dropped(self):
        self.assertIsNone(self.add(link={'thread': 'abc'})['link'])

    def test_ids_are_not_reused_after_delete(self):
        self.add()
        second = self.add()
        reminders.change(second['id'], 'delete', now=NOW)
        self.assertEqual(self.add()['id'], 'R3')

    def test_bad_records_are_skipped_and_a_broken_file_is_an_error(self):
        self.folder.mkdir()
        (self.folder / 'reminders.json').write_text(json.dumps({'reminders': [
            {'id': 'R4', 'text': 'kept', 'due': NOW, 'state': 'due'}, {'id': 'x', 'text': 'bad'}, 'junk']}))
        listed = reminders.listing(now=NOW)['reminders']
        self.assertEqual([r['id'] for r in listed], ['R4'])
        self.assertEqual(self.add()['id'], 'R5')
        (self.folder / 'reminders.json').write_text('{broken')
        with self.assertRaises(reminders.ReminderError) as caught:
            reminders.listing()
        self.assertEqual(caught.exception.status, 500)

    def test_only_the_newest_done_reminders_are_kept(self):
        for at in range(reminders.DONE_KEPT + 3):
            reminders.change(self.add(f'n{at}')['id'], 'done', {'occ': 1}, now=NOW + at)
        kept = {r['text'] for r in self.stored()['reminders']}
        self.assertEqual(len(kept), reminders.DONE_KEPT)
        self.assertNotIn('n0', kept)
        self.assertIn(f'n{reminders.DONE_KEPT + 2}', kept)


class StateTests(Base):
    def test_done_and_reopen(self):
        rid = self.add()['id']
        done = reminders.change(rid, 'done', {'occ': 1}, now=NOW + 10)
        self.assertEqual((done['state'], done['done_at']), ('done', NOW + 10))
        reopened = reminders.change(rid, 'reopen', now=NOW + 20)
        self.assertEqual((reopened['state'], reopened['occ']), ('scheduled', 2))  # still in the future
        reminders.change(rid, 'done', {'occ': 2}, now=NOW + 30)
        again = reminders.change(rid, 'reopen', now=NOW + 7200)
        self.assertEqual((again['state'], again['done_at']), ('due', None))
        self.assertEqual(again['occ'], 2)
        self.assertEqual(reminders.tick({'push': mock.Mock(return_value=True)}, now=NOW + 7201), [(rid, 'push')])

    def test_reopen_keeps_each_channels_delivery_state(self):
        rid = self.add(due=NOW + 60)['id']
        push = mock.Mock(side_effect=[False, True])
        ping = mock.Mock(return_value=True)
        channels = {'push': push, 'ping': ping}
        self.assertEqual(reminders.tick(channels, now=NOW + 60), [(rid, 'ping')])
        reminders.change(rid, 'done', {'occ': 1}, now=NOW + 61)
        reopened = reminders.change(rid, 'reopen', now=NOW + 62)
        self.assertEqual(reopened['occ'], 1)
        self.assertEqual(reminders.tick(channels, now=NOW + 89), [])
        self.assertEqual(reminders.tick(channels, now=NOW + 90), [(rid, 'push')])
        self.assertEqual(reminders.tick(channels, now=NOW + 91), [])
        self.assertEqual(push.call_count, 2)
        ping.assert_called_once()

    def test_snooze_schedules_again_and_counts(self):
        rid = self.add(due=NOW + 60)['id']
        reminders.tick({'push': lambda r: True}, now=NOW + 61)
        snoozed = reminders.change(rid, 'snooze', {'occ': 1, 'until': NOW + 4000}, now=NOW + 70)
        self.assertEqual((snoozed['state'], snoozed['due'], snoozed['snoozes'], snoozed['fired_at']),
                         ('scheduled', NOW + 4000, 1, None))
        deliver = mock.Mock(return_value=True)
        self.assertEqual(snoozed['occ'], 2)
        self.assertEqual(self.stored()['reminders'][0]['sent'], [])
        self.assertEqual(reminders.tick({'push': deliver}, now=NOW + 4000), [(rid, 'push')])
        with self.assertRaises(reminders.ReminderError):
            reminders.change(rid, 'snooze', {'occ': 2, 'until': NOW}, now=NOW + 4001)  # in the past
        reminders.change(rid, 'done', {'occ': 2}, now=NOW + 4002)
        with self.assertRaises(reminders.ReminderError) as caught:
            reminders.change(rid, 'snooze', {'occ': 2, 'until': NOW + 9000}, now=NOW + 4003)
        self.assertEqual(caught.exception.status, 409)

    def test_edit_text_link_and_time(self):
        rid = self.add(due=NOW + 60)['id']
        reminders.tick({'push': lambda r: True}, now=NOW + 61)
        edited = reminders.change(rid, 'edit', {'text': ' New text ', 'link': {'item': 'B9'}}, now=NOW + 62)
        self.assertEqual((edited['text'], edited['link'], edited['state']), ('New text', {'item': 'B9'}, 'due'))
        self.assertEqual(edited['occ'], 1)
        moved = reminders.change(rid, 'edit', {'due': NOW + 900}, now=NOW + 63)
        self.assertEqual((moved['state'], moved['due'], moved['snoozes']), ('scheduled', NOW + 900, 0))
        self.assertEqual(moved['occ'], 2)
        with self.assertRaises(reminders.ReminderError):
            reminders.change(rid, 'edit', {'text': ''}, now=NOW + 64)

    def test_delete_then_restore_keeps_the_same_record(self):
        rid = self.add(link={'item': 'B1'})['id']
        reminders.change(rid, 'snooze', {'occ': 1, 'until': NOW + 60}, now=NOW)
        reminders.tick({'push': lambda r: True, 'ping': lambda r: False}, now=NOW + 60)
        before = self.stored()['reminders'][0]
        self.assertEqual((before['occ'], before['state'], before['sent'], before['tries']),
                         (2, 'due', ['push'], {'ping': 1}))
        self.assertIsNone(reminders.change(rid, 'delete', now=NOW + 61))
        self.assertEqual(self.stored()['deleted'], [dict(before, deleted_at=NOW + 61)])
        self.assertEqual(reminders.listing(now=NOW + 61),
                         {'reminders': [], 'deleted': [dict(reminders.public(before), deleted_at=NOW + 61)],
                          'now': NOW + 61})
        restored = reminders.restore(rid, now=NOW + 62)
        self.assertEqual(restored, reminders.public(before))
        self.assertEqual(self.stored()['reminders'], [before])
        self.assertEqual(self.stored()['deleted'], [])
        self.assertEqual(reminders.restore(rid, now=NOW + 63), restored)  # no duplicate

    def test_restore_an_old_done_reminder_at_the_done_limit(self):
        rid = self.add()['id']
        done = reminders.change(rid, 'done', {'occ': 1}, now=NOW)
        reminders.change(rid, 'delete', now=NOW)
        for i in range(1, reminders.DONE_KEPT + 1):
            reminders.change(self.add()['id'], 'done', {'occ': 1}, now=NOW + i)
        self.assertEqual(reminders.restore(rid, now=NOW + 60), done)
        records = reminders.listing(now=NOW + 61)['reminders']
        self.assertEqual(len(records), reminders.DONE_KEPT)
        self.assertIn(done, records)

    def test_deleted_reminders_are_kept_for_30_days_without_a_count_cap(self):
        for i in range(25):
            rid = self.add(f'Deleted {i}')['id']
            reminders.change(rid, 'delete', now=NOW + i)
        first = reminders.restore('R1', now=NOW + 29 * 86400)
        self.assertEqual((first['id'], first['text']), ('R1', 'Deleted 0'))
        rid = self.add('Server clock moved back')['id']
        reminders.change(rid, 'delete', now=NOW + 1)
        self.assertEqual(len(self.stored()['deleted']), 25)
        cutoff = NOW + 30 * 86400
        expected = [f'R{i}' for i in range(25, 3, -1)]
        self.assertEqual([r['id'] for r in reminders.listing(now=cutoff + 2)['deleted']], expected)
        with self.assertRaises(reminders.ReminderError) as expired:
            reminders.restore('R3', now=cutoff + 2)
        self.assertEqual(expired.exception.status, 404)
        reminders.tick({}, now=cutoff + 24)
        self.assertEqual(self.stored()['deleted'], [])

    def test_snooze_requires_an_integer_duration_within_bounds(self):
        rid = self.add()['id']
        for seconds in (59, 60.0, True, '3600', None, 5 * 366 * 86400 + 1):
            with self.subTest(seconds=seconds), self.assertRaises(reminders.ReminderError):
                reminders.change(rid, 'snooze', {'occ': 1, 'for': seconds}, now=NOW)
        snoozed = reminders.change(rid, 'snooze', {'occ': 1, 'for': 60}, now=NOW)
        self.assertEqual((snoozed['due'], snoozed['occ']), (NOW + 60, 2))

    def test_unknown_ids(self):
        with self.assertRaises(reminders.ReminderError) as caught:
            reminders.change('R99', 'done', {'occ': 1})
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(reminders.ReminderError):
            reminders.change('../1', 'done', {'occ': 1})
        self.assertEqual(reminders.change(self.add()['id'].lower(), 'done', {'occ': 1}, now=NOW)['state'], 'done')


class TickTests(Base):
    def test_fires_once_at_its_time(self):
        rid = self.add(due=NOW + 60)['id']
        deliver = mock.Mock(return_value=True)
        channels = {'push': deliver}
        self.assertEqual(reminders.tick(channels, now=NOW + 59), [])
        self.assertEqual(reminders.tick(channels, now=NOW + 61), [(rid, 'push')])
        self.assertEqual(reminders.tick(channels, now=NOW + 63), [])
        sent = deliver.call_args.args[0]
        self.assertEqual((sent['id'], sent['state'], sent['late']), (rid, 'due', False))
        stored = self.stored()['reminders'][0]
        self.assertEqual((stored['state'], stored['fired_at'], stored['sent']), ('due', NOW + 61, ['push']))

    def test_concurrent_ticks_claim_each_channel_once(self):
        rid = self.add(due=NOW + 60)['id']
        entered, release = threading.Event(), threading.Event()
        results = []

        def slow_send(_record):
            entered.set()
            return release.wait(5)

        push = mock.Mock(side_effect=slow_send)
        ping = mock.Mock(return_value=True)
        channels = {'push': push, 'ping': ping}
        first = threading.Thread(target=lambda: results.extend(reminders.tick(channels, now=NOW + 60)))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            # The other tick can send ping while push is still in flight.
            self.assertEqual(reminders.tick(channels, now=NOW + 60), [(rid, 'ping')])
        finally:
            release.set()
            first.join(2)
        self.assertFalse(first.is_alive())
        self.assertEqual(results, [(rid, 'push')])
        push.assert_called_once()
        ping.assert_called_once()
        self.assertEqual(self.stored()['reminders'][0]['sending'], {})

    def test_claim_survives_delete_restore_and_reopen_until_it_expires(self):
        rid = self.add(due=NOW + 60)['id']
        started = NOW + 60
        with self.assertRaises(SystemExit):
            reminders.tick({'push': mock.Mock(side_effect=SystemExit)}, now=started)
        expires = started + 120
        claim = {'push': {'until': expires}}
        self.assertEqual(self.stored()['reminders'][0].get('sending'), claim)
        reminders.change(rid, 'done', {'occ': 1}, now=started)
        reminders.change(rid, 'delete', now=started)
        self.assertEqual(self.stored()['deleted'][0]['sending'], claim)
        self.assertNotIn('sending', reminders.restore(rid, now=started))
        self.assertNotIn('sending', reminders.change(rid, 'reopen', now=started))
        self.assertEqual(self.stored()['reminders'][0]['sending'], claim)
        deliver = mock.Mock(return_value=True)
        self.assertEqual(reminders.tick({'push': deliver}, now=expires - 1), [])
        deliver.assert_not_called()
        self.assertEqual(reminders.tick({'push': deliver}, now=expires), [(rid, 'push')])
        deliver.assert_called_once()
        self.assertEqual(self.stored()['reminders'][0]['sending'], {})

    def test_missed_while_down_fires_late_on_start(self):
        rid = self.add(due=NOW + 60)['id']
        deliver = mock.Mock(return_value=True)
        self.assertEqual(reminders.tick({'push': deliver}, now=NOW + 3 * 3600), [(rid, 'push')])
        self.assertTrue(deliver.call_args.args[0]['late'])
        self.assertIn('late', reminders.payload(deliver.call_args.args[0], now=NOW + 3 * 3600)['body'])

    def test_only_the_failed_channel_is_retried_at_its_backoff(self):
        rid = self.add(due=NOW + 60)['id']
        push = mock.Mock(return_value=True)
        ping = mock.Mock(side_effect=[False, RuntimeError('ping down'), True])
        channels = {'push': push, 'ping': ping}
        self.assertEqual(reminders.tick(channels, now=NOW + 61), [(rid, 'push')])
        for before, at, sent, calls in ((90, 91, [], 2), (210, 211, [(rid, 'ping')], 3)):
            self.assertEqual(reminders.tick(channels, now=NOW + before), [])
            self.assertEqual(ping.call_count, calls - 1)
            self.assertEqual(reminders.tick(channels, now=NOW + at), sent)
            self.assertEqual(ping.call_count, calls)
        push.assert_called_once()
        self.assertFalse(ping.call_args.args[0]['late'])  # fired_at stays the first tick, within LATE

    def test_a_save_failure_repeats_only_the_unrecorded_channel(self):
        for failed, expected_calls in (('push', (2, 1)), ('ping', (1, 2))):
            with self.subTest(failed=failed):
                rid = self.add(due=NOW + 60)['id']
                channels = {'push': mock.Mock(return_value=True), 'ping': mock.Mock(return_value=True)}
                save = reminders._save

                def fail_once(data):
                    if failed in data['reminders'][-1]['sent']:
                        raise OSError('disk full')
                    save(data)

                with mock.patch.object(reminders, '_save', side_effect=fail_once), self.assertRaises(OSError):
                    reminders.tick(channels, now=NOW + 61)
                remaining = list(channels)[list(channels).index(failed):]
                self.assertEqual(reminders.tick({failed: channels[failed]}, now=NOW + 62), [])
                expires = NOW + 61 + 120
                self.assertEqual(reminders.tick(channels, now=expires), [(rid, channel) for channel in remaining])
                self.assertEqual(reminders.tick(channels, now=expires + 1), [])
                self.assertEqual(tuple(c.call_count for c in channels.values()), expected_calls)

    def test_failed_channel_stops_after_eight_tries_and_snooze_resets_it(self):
        rid = self.add(due=NOW + 60)['id']
        deliver = mock.Mock(return_value=False)
        channels = {'push': deliver}
        now = NOW + 60
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for attempt, delay in enumerate((30, 120, 600, 1800, 1800, 1800, 1800), 1):
                self.assertEqual(reminders.tick(channels, now=now), [])
                self.assertEqual(self.stored()['reminders'][0]['retry_at'], {'push': now + delay})
                now += delay
                reminders.tick(channels, now=now - 1)
                self.assertEqual(deliver.call_count, attempt)
            self.assertEqual(reminders.tick(channels, now=now), [])
            self.assertEqual(reminders.tick(channels, now=now + 1800), [])
        self.assertEqual(deliver.call_count, 8)
        self.assertEqual(err.getvalue().splitlines(), [f'Reminder {rid} occurrence 1: push failed after 8 tries'])
        record = self.stored()['reminders'][0]
        self.assertEqual((record['sent'], record['tries']), (['push'], {'push': 8}))
        self.assertEqual(reminders.listing(now=now)['reminders'][0]['failed'], ['push'])
        reminders.change(rid, 'done', {'occ': 1}, now=now)
        reopened = reminders.change(rid, 'reopen', now=now)
        self.assertEqual(reopened['failed'], ['push'])
        ping = mock.Mock(return_value=True)
        self.assertEqual(reminders.tick({'push': deliver, 'ping': ping}, now=now), [(rid, 'ping')])
        self.assertEqual(deliver.call_count, 8)
        reminders.change(rid, 'snooze', {'occ': 1, 'for': 3600}, now=now)
        record = self.stored()['reminders'][0]
        self.assertEqual((record['occ'], record['sent'], record['tries'], record['retry_at'], record['failed']),
                         (2, [], {}, {}, []))

    def test_done_during_tick_is_skipped_by_the_other_sends(self):
        first = self.add('First', due=NOW + 60)['id']
        second = self.add('Second', due=NOW + 60)['id']

        def deliver(record):
            self.assertEqual(record['id'], first)
            reminders.change(first, 'done', {'occ': 1}, now=NOW + 61)
            reminders.change(second, 'done', {'occ': 1}, now=NOW + 61)
            return True

        push = mock.Mock(side_effect=deliver)
        ping = mock.Mock(return_value=True)
        self.assertEqual(reminders.tick({'push': push, 'ping': ping}, now=NOW + 61), [(first, 'push')])
        push.assert_called_once()
        ping.assert_not_called()
        self.assertEqual([(r['state'], r['sent']) for r in self.stored()['reminders']],
                         [('done', ['push']), ('done', [])])

    def test_snooze_during_send_does_not_record_delivery_on_the_new_occurrence(self):
        rid = self.add(due=NOW + 60)['id']

        def deliver(record):
            reminders.change(record['id'], 'snooze', {'occ': record['occ'], 'for': 3600}, now=NOW + 61)
            return True

        ping = mock.Mock(return_value=True)
        self.assertEqual(reminders.tick({'push': deliver, 'ping': ping}, now=NOW + 61), [(rid, 'push')])
        state = self.stored()['reminders'][0]
        self.assertEqual((state['occ'], state['state'], state['sent']), (2, 'scheduled', []))
        ping.assert_not_called()

    def test_delete_during_send_keeps_the_result_for_undo(self):
        rid = self.add(due=NOW + 60)['id']

        def deliver(record):
            reminders.change(record['id'], 'delete', now=NOW + 61)
            return True

        ping = mock.Mock(return_value=True)
        channels = {'push': mock.Mock(side_effect=deliver), 'ping': ping}
        self.assertEqual(reminders.tick(channels, now=NOW + 61), [(rid, 'push')])
        ping.assert_not_called()
        reminders.restore(rid, now=NOW + 62)
        self.assertEqual(reminders.tick(channels, now=NOW + 62), [(rid, 'ping')])
        channels['push'].assert_called_once()

    def test_legacy_delivered_reminder_is_not_resent(self):
        self.folder.mkdir()
        record = {'id': 'R7', 'text': 'Legacy', 'due': NOW, 'state': 'due', 'notified': True}
        (self.folder / 'reminders.json').write_text(json.dumps({'reminders': [record]}))
        self.assertEqual(reminders.listing(now=NOW)['reminders'][0]['occ'], 1)
        deliver = mock.Mock(return_value=True)
        self.assertEqual(reminders.tick({'push': deliver, 'ping': deliver}, now=NOW), [])
        deliver.assert_not_called()
        self.add()
        migrated = self.stored()['reminders'][0]
        self.assertEqual((migrated['occ'], migrated['sent']), (1, ['push', 'ping']))
        self.assertNotIn('notified', migrated)

    def test_payload_for_push(self):
        body = reminders.payload({'id': 'R3', 'text': 'Hi', 'due': NOW, 'late': False, 'occ': 2}, silent=True)
        self.assertEqual(body, {'title': 'Reminder', 'body': 'Hi', 'tag': 'reminder-R3', 'priority': 'urgent',
                                'silent': True, 'go': 'reminders', 'reminder': 'R3', 'occ': 2})

    def test_ticker_survives_errors(self):
        calls = []
        stop = threading.Event()
        def failing(*args):
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
                raise SystemExit  # end the thread for the test
            raise ValueError('bad file')
        with mock.patch.object(reminders, 'tick', failing), contextlib.redirect_stdout(io.StringIO()):
            ticker = reminders.Ticker({'push': lambda r: True}, every=0.01)
            ticker.start()
            self.assertTrue(stop.wait(2))
            ticker.thread.join(2)
        self.assertEqual(len(calls), 2)

    def test_second_ticker_waits_for_the_same_store_scheduler_lock(self):
        first = reminders.Ticker({'push': None}, every=0.01)
        second = reminders.Ticker({'ping': None}, every=0.01)
        first_called, second_called = threading.Event(), threading.Event()
        release, contended = threading.Event(), threading.Event()
        flock = reminders.fcntl.flock

        def attempt_lock(*args):
            try:
                flock(*args)
            except BlockingIOError:
                contended.set()
                raise

        def run(channels):
            if channels is first.channels:
                first_called.set()
                release.wait(5)
            else:
                second_called.set()
            raise SystemExit

        with mock.patch.object(reminders, 'tick', run), mock.patch.object(reminders.fcntl, 'flock', attempt_lock):
            first.start()
            try:
                self.assertTrue(first_called.wait(2))
                second.start()
                self.assertTrue(contended.wait(2))
                self.assertFalse(second_called.is_set())
            finally:
                release.set()
                first.thread.join(2)
                if second.thread is not None:
                    second.thread.join(2)
        self.assertTrue(second_called.is_set())
        self.assertFalse(first.thread.is_alive())
        self.assertFalse(second.thread.is_alive())


class WhenTests(unittest.TestCase):
    def test_first_reminder_time(self):
        self.assertEqual(reminders.parse_when('2026-11-19 10:00', now=NOW),
                         datetime(2026, 11, 19, 4, 30, tzinfo=timezone.utc).timestamp())

    def test_forms(self):
        ist = lambda *a: datetime(*a, tzinfo=reminders.ZONE).timestamp()
        cases = {'in 2h': NOW + 7200, 'in 90m': NOW + 5400, 'in 1 day': NOW + 86400,
                 'tomorrow 10:00': ist(2026, 11, 19, 10, 0), 'tomorrow': ist(2026, 11, 19, 9, 0),
                 '18:00': ist(2026, 11, 18, 18, 0), '09:00': ist(2026, 11, 19, 9, 0),
                 '2026-11-24': ist(2026, 11, 24, 10, 0),
                 '2026-11-19T10:00+00:00': datetime(2026, 11, 19, 10, tzinfo=timezone.utc).timestamp(),
                 '2026-11-19T10:00': ist(2026, 11, 19, 10, 0)}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reminders.parse_when(text, now=NOW), want)
        for bad in ('soon', 'tomorrow 25', '', 'in 2 weeks'):
            with self.subTest(bad=bad), self.assertRaises(reminders.ReminderError):
                reminders.parse_when(bad, now=NOW)

    def test_when_text(self):
        self.assertEqual(reminders.when_text(NOW + 600, now=NOW), '17:40')
        self.assertEqual(reminders.when_text(reminders.parse_when('tomorrow 10:00', now=NOW), now=NOW), 'tomorrow 10:00')
        self.assertEqual(reminders.when_text(NOW + 3 * 86400, now=NOW), 'Sat 21 Nov 17:30')


class HttpTests(Base):
    def call(self, action, body):
        handler = Handler(f'{reminders.PREFIX}/{action}')
        with mock.patch.object(reminders.time, 'time', return_value=NOW):
            reminders.serve(handler, body)
        return handler.sent

    def test_routes(self):
        self.assertTrue(reminders.handles('/mobile/api/reminders'))
        self.assertTrue(reminders.handles('/mobile/api/reminders/snooze?x=1'))
        self.assertFalse(reminders.handles('/mobile/api/reminders/other'))
        self.assertFalse(reminders.handles('/mobile/api/reminders/'))

    def test_add_list_and_errors(self):
        status, body = self.call('add', {'text': 'Call', 'due': NOW + 60, 'link': {'pane': '%3'}})
        self.assertEqual((status, body['reminder']['by'], body['reminder']['id']), (200, 'you', 'R1'))
        handler = Handler(reminders.PREFIX)
        reminders.get(handler)
        self.assertEqual([r['id'] for r in handler.sent[1]['reminders']], ['R1'])
        self.assertNotIn('notified', handler.sent[1]['reminders'][0])
        self.assertEqual(self.call('add', {'text': 'Call', 'due': NOW - 60})[0], 400)
        self.assertEqual(self.call('done', {'id': 'R7', 'occ': 1})[0], 404)
        self.assertEqual(self.call('snooze', {'id': 'R1', 'occ': 1, 'until': NOW + 600})[1]['reminder']['snoozes'], 1)
        status, body = self.call('delete', {'id': 'R1'})
        self.assertEqual((status, body), (200, {'reminder': None}))

    def test_add_accepts_spoken_time_and_preserves_explicit_due(self):
        date = datetime(2026, 11, 24, 10, tzinfo=reminders.ZONE).timestamp()
        for when, expected in (('2026-11-24 10:00', date), ('2026-11-24', date), ('in 2h', NOW + 7200)):
            with self.subTest(when=when):
                status, body = self.call('add', {'text': 'Call', 'when': when})
                self.assertEqual(status, 200)
                self.assertEqual(body['reminder']['due'], expected)
        status, body = self.call('add', {'text': 'Call', 'due': NOW + 600, 'when': 'later'})
        self.assertEqual(status, 200)
        self.assertEqual(body['reminder']['due'], NOW + 600)

    def test_add_rejects_invalid_or_past_spoken_time(self):
        self.assertEqual(self.call('add', {'text': 'Call', 'when': 'later'}), (400, {
            'error': 'Cannot read the time "later". Use "2026-11-19 10:00", "tomorrow 10:00" or "in 2h".'}))
        self.assertEqual(self.call('add', {'text': 'Call', 'when': '2026-11-17 10:00'}),
                         (400, {'error': 'Pick a time in the future.'}))
        self.assertEqual(self.call('add', {'text': 'Call', 'when': 17}), (400, {'error': 'Pick a time.'}))
        self.assertFalse((self.folder / 'reminders.json').exists())

    def test_restore_requires_an_id_from_recently_deleted(self):
        _, body = self.call('add', {'text': 'Call', 'due': NOW + 60})
        record = body['reminder']
        self.call('delete', {'id': record['id']})
        self.assertEqual(self.call('restore', {'reminder': record})[0], 400)
        self.assertEqual(self.call('restore', {'id': 'R99'}),
                         (404, {'error': 'That reminder is no longer in Recently deleted.'}))
        self.assertEqual(self.call('restore', {'id': record['id']}), (200, body))

    def test_done_and_snooze_require_an_integer_occurrence(self):
        self.call('add', {'text': 'Call', 'due': NOW + 60})
        before = self.stored()
        for action, extra in (('done', {}), ('snooze', {'for': 3600})):
            for occurrence in ({}, {'occ': None}, {'occ': '1'}, {'occ': True}, {'occ': 1.0}):
                with self.subTest(action=action, occurrence=occurrence):
                    self.assertEqual(self.call(action, {'id': 'R1', **extra, **occurrence}),
                                     (400, {'error': 'Include the reminder occurrence (occ) as an integer.'}))
                    self.assertEqual(self.stored(), before)

    def test_notification_actions_reject_a_stale_occurrence(self):
        self.call('add', {'text': 'Call', 'due': NOW + 60})
        status, body = self.call('snooze', {'id': 'R1', 'occ': 1, 'for': 3600})
        record = body['reminder']
        self.assertEqual((status, record['occ'], record['due']), (200, 2, NOW + 3600))
        before = self.stored()
        for action, extra in (('done', {}), ('snooze', {'for': 3600})):
            with self.subTest(action=action):
                self.assertEqual(self.call(action, {'id': 'R1', 'occ': 1, **extra}),
                                 (409, {'error': 'This reminder changed after that notice. Open Reminders to see it.'}))
                self.assertEqual(self.stored(), before)
        status, body = self.call('done', {'id': 'R1', 'occ': 2})
        self.assertEqual((status, body['reminder']['state']), (200, 'done'))

    def test_server_wiring(self):
        source = (Path(__file__).with_name('server.py')).read_text()
        self.assertIn('elif reminders.handles(path):\n                reminders.get(self)', source)
        self.assertIn('not reminders.handles(self.path)', source)
        self.assertIn('if reminders.handles(self.path):\n                reminders.serve(self, body)', source)
        self.assertLess(source.index('ThreadingHTTPServer((host, port), Handler)'), source.index('REMINDERS.start()'))


class DeliveryTests(Base):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name)
        for patcher in (mock.patch.object(paths, 'CONFIG', self.config),
                        mock.patch.object(urgent_ping, '_REMINDERS', deque())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stderr=b''))

    def mock_web_push(self, stack, sender, error=RuntimeError):
        import server
        push = server.push
        store = self.config / 'push.json'
        for patcher in (
            mock.patch.object(push, 'STORE', store),
            mock.patch.object(push, 'delivery_settings', return_value={}),
            mock.patch.object(push, 'webpush', sender),
            mock.patch.object(push, 'WebPushException', error, create=True),
            mock.patch.object(push, 'vapid_keys', return_value=('pem', None)),
            mock.patch.object(push, 'Vapid02', mock.Mock(), create=True),
        ):
            stack.enter_context(patcher)
        return {'push': server.REMINDERS.channels['push']}, store

    def remind(self, **record):
        return urgent_ping.remind(dict({'id': 'R1', 'occ': 1, 'text': 'Check usage'}, **record),
                                  clock=lambda: 1000, runner=self.runner)

    def test_ping_has_its_own_limit_link_and_no_reply_key(self):
        self.assertTrue(self.remind())  # not set up: nothing to retry
        self.runner.assert_not_called()
        (self.config / 'urgent-ping.json').write_text(json.dumps(
            {'command': ['ping', '{title}', '{question}', '{url}', '{pane}'], 'base_url': 'https://tether.example/'}))
        self.assertTrue(self.remind(late=True))
        self.assertEqual(self.runner.call_args.args[0],
                         ['ping', 'Tether: Reminder (late)', 'Check usage', 'https://tether.example/chat/?go=reminders', ''])
        for _ in range(19):
            self.assertTrue(self.remind())
        self.assertFalse(self.remind())  # 21st in an hour: the tick tries again later
        (self.config / 'urgent-ping.json').write_text(json.dumps({'enabled': False, 'command': ['ping']}))
        self.assertTrue(self.remind())  # pings off: nothing to retry

    def test_ping_returns_the_command_result_and_counts_only_runs(self):
        (self.config / 'urgent-ping.json').write_text(json.dumps({'command': ['ping']}))
        self.runner.side_effect = [OSError('cannot start'), subprocess.TimeoutExpired('ping', 60),
                                   SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.remind())
            self.assertEqual(len(urgent_ping._REMINDERS), 0)
            self.assertTrue(self.remind())
            self.assertEqual(len(urgent_ping._REMINDERS), 1)
            self.assertFalse(self.remind())
            self.assertTrue(self.remind())
        self.assertEqual(len(urgent_ping._REMINDERS), 3)
        self.assertEqual(self.runner.call_count, 4)
        self.assertEqual({call.kwargs['env']['TETHER_PING_KEY'] for call in self.runner.call_args_list}, {'R1:1'})

    def test_ping_timeout_marks_occurrence_sent_with_its_key(self):
        (self.config / 'urgent-ping.json').write_text(json.dumps({'command': ['ping']}))
        rid = self.add(due=NOW + 60)['id']
        reminders.change(rid, 'snooze', {'occ': 1, 'for': 60}, now=NOW + 1)
        self.runner.side_effect = subprocess.TimeoutExpired('ping', 60)
        channels = {'ping': lambda record: urgent_ping.remind(record, clock=lambda: 1000, runner=self.runner)}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(reminders.tick(channels, now=NOW + 61), [(rid, 'ping')])
            self.assertEqual(reminders.tick(channels, now=NOW + 91), [])
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.kwargs['env']['TETHER_PING_KEY'], f'{rid}:2')
        self.assertEqual(err.getvalue().strip(), 'Reminder ping timed out; not retried to avoid a duplicate')
        record = self.stored()['reminders'][0]
        self.assertEqual((record['sent'], record['failed'], record['tries']), (['ping'], [], {}))

    def test_server_delivery_follows_the_urgent_setting(self):
        import server
        self.assertIs(server.REMINDERS.channels['ping'], urgent_ping.remind)
        endpoint = 'https://example.test/device'

        def send_all(_payload, _urgent, *, endpoints, on_delivered, **_):
            self.assertEqual(endpoints, {endpoint})
            on_delivered(endpoint)
            return 1

        channels = {'push': server.REMINDERS.channels['push']}
        for mode, pushes, silent in (('sound', 1, False), ('silent', 1, True), ('off', 0, None)):
            with self.subTest(mode=mode), \
                    mock.patch.object(server.push, 'delivery_settings', return_value={'urgent': mode}), \
                    mock.patch.object(server.push, 'load_strict', return_value={endpoint: {}}), \
                    mock.patch.object(server.push, 'send_all', side_effect=send_all) as send:
                rid = self.add(due=NOW + 60)['id']
                self.assertEqual(reminders.tick(channels, now=NOW + 60), [(rid, 'push')])
                self.assertEqual(send.call_count, pushes)
                if pushes:
                    self.assertEqual(send.call_args.args[0]['silent'], silent)
                    self.assertTrue(send.call_args.args[1])

    def test_done_before_push_audience_is_saved_skips_the_attempt(self):
        import server
        rid = self.add(due=NOW + 60)['id']

        def subscriptions(_store):
            reminders.change(rid, 'done', {'occ': 1}, now=NOW + 60)
            return {'https://example.test/device': {}}

        with mock.patch.object(server.push, 'delivery_settings', return_value={}), \
                mock.patch.object(server.push, 'load_strict', side_effect=subscriptions), \
                mock.patch.object(server.push, 'send_all') as send:
            channels = {'push': server.REMINDERS.channels['push']}
            self.assertEqual(reminders.tick(channels, now=NOW + 60), [])
            send.assert_not_called()
        record = self.stored()['reminders'][0]
        self.assertEqual((record['sent'], record['tries']), ([], {}))
        self.assertNotIn('push', record)

    def test_push_store_read_errors_retry_without_saving_an_audience(self):
        endpoint = 'https://example.test/device'
        started = NOW + 60
        read_text = Path.read_text
        for invalid in ('corrupt JSON', '[]', None):
            with self.subTest(invalid=invalid), contextlib.ExitStack() as stack:
                sender = mock.Mock()
                channels, store = self.mock_web_push(stack, sender)
                rid = self.add(due=started)['id']
                good = json.dumps({endpoint: {'subscription': {'endpoint': endpoint}}})
                store.write_text(invalid if invalid is not None else good)

                def read(path, *args, **kwargs):
                    if path == store and invalid is None:
                        raise PermissionError('cannot read subscriptions')
                    return read_text(path, *args, **kwargs)

                with mock.patch.object(type(store), 'read_text', read):
                    self.assertEqual(reminders.tick(channels, now=started), [])
                sender.assert_not_called()
                record = self.stored()['reminders'][-1]
                self.assertNotIn('push', record)
                self.assertEqual((record['sent'], record['retry_at']), ([], {'push': started + 30}))
                store.write_text(good)
                self.assertEqual(reminders.tick(channels, now=started + 30), [(rid, 'push')])
                sender.assert_called_once()
                self.assertEqual(sender.call_args.kwargs['subscription_info']['endpoint'], endpoint)

    def test_push_store_read_error_after_send_keeps_the_audience_for_retry(self):
        endpoint = 'https://example.test/device'
        good = json.dumps({endpoint: {'subscription': {'endpoint': endpoint}}})
        with contextlib.ExitStack() as stack:
            sender = mock.Mock()
            channels, store = self.mock_web_push(stack, sender)
            rid = self.add(due=NOW + 60)['id']
            store.write_text(good)

            def fail(**_):
                store.write_text('corrupt JSON')
                raise OSError('temporary push failure')

            sender.side_effect = fail
            self.assertEqual(reminders.tick(channels, now=NOW + 60), [])
            record = self.stored()['reminders'][0]
            self.assertEqual((record['sent'], record['tries']), ([], {'push': 1}))
            self.assertEqual(record['push'], {'audience': [reminders._push_id(endpoint)], 'to': []})
            sender.side_effect = None
            store.write_text(good)
            self.assertEqual(reminders.tick(channels, now=NOW + 90), [(rid, 'push')])
            self.assertEqual(sender.call_count, 2)

    def test_push_stops_remaining_endpoints_after_done_snooze_or_delete(self):
        import server
        first, second = 'https://example.test/first', 'https://example.test/second'
        started = NOW + 60
        send_all = server.push.send_all
        for action, body in (('done', {'occ': 1}), ('snooze', {'occ': 1, 'for': 60}), ('delete', {})):
            with self.subTest(action=action), contextlib.ExitStack() as stack:
                sender = mock.Mock()
                channels, store = self.mock_web_push(stack, sender)
                store.write_text(json.dumps({endpoint: {'subscription': {'endpoint': endpoint}}
                                             for endpoint in (first, second)}))
                rid = self.add(due=started)['id']

                def send_with_change(*args, on_delivered, **kwargs):
                    def accept_then_change(endpoint):
                        on_delivered(endpoint)
                        if endpoint == first:
                            reminders.change(rid, action, body, now=started)

                    return send_all(*args, on_delivered=accept_then_change, **kwargs)

                with mock.patch.object(server.push, 'send_all', send_with_change):
                    self.assertEqual(reminders.tick(channels, now=started), [])
                self.assertEqual([c.kwargs['subscription_info']['endpoint'] for c in sender.call_args_list], [first])
                records = self.stored()['deleted' if action == 'delete' else 'reminders']
                record = next(record for record in records if record['id'] == rid)
                self.assertEqual((record['sent'], record['tries'], record['sending']), ([], {}, {}))
                if action == 'snooze':
                    self.assertNotIn('push', record)
                    continue
                self.assertEqual(record['push']['to'], [reminders._push_id(first)])
                if action == 'done':
                    reminders.change(rid, 'reopen', now=started)
                else:
                    reminders.restore(rid, now=started)
                sender.reset_mock()
                self.assertEqual(reminders.tick(channels, now=started), [(rid, 'push')])
                sender.assert_called_once()
                self.assertEqual(sender.call_args.kwargs['subscription_info']['endpoint'], second)

    def test_web_push_lost_answer_is_not_sent_again_but_unsent_request_retries(self):
        # The push service can accept a request and then lose its answer (read timeout, peer closed the connection).
        # A retry would show the reminder twice. An error that proves the request never left must retry.
        # The errors copy how requests wraps urllib3 and http.client errors (names, nesting in args).
        import server
        request_error = type('RequestException', (OSError,), {})
        connection_error = type('ConnectionError', (request_error,), {})
        inner = lambda name: type(name, (Exception,), {})('detail')
        cases = (
            ('read timeout', type('ReadTimeout', (request_error,), {})('lost'), False),
            ('peer closed after the request', connection_error(type('ProtocolError', (Exception,), {})(
                'Connection aborted.', inner('RemoteDisconnected'))), False),
            ('connect timeout', type('ConnectTimeout', (connection_error,), {})('slow'), True),
            ('refused', connection_error(type('MaxRetryError', (Exception,), {})(inner('NewConnectionError'))), True),
        )
        started = NOW + 60
        for label, error, again in cases:
            with self.subTest(label), contextlib.ExitStack() as stack:
                sender = mock.Mock(side_effect=[error, None])
                channels, store = self.mock_web_push(stack, sender)
                endpoint = 'https://example.test/' + label.replace(' ', '-')
                store.write_text(json.dumps({endpoint: {'subscription': {'endpoint': endpoint}}}))
                rid = self.add(due=started)['id']
                stack.callback(lambda rid=rid: reminders.change(rid, 'delete'))  # the next case starts with nothing due
                self.assertEqual(reminders.tick(channels, now=started), [] if again else [(rid, 'push')])
                second = reminders.tick(channels, now=started + 3600)
                self.assertEqual(sender.call_count, 2 if again else 1)
                self.assertEqual(second, [(rid, 'push')] if again else [])
                record = next(r for r in self.stored()['reminders'] if r['id'] == rid)
                self.assertIn('push', record['sent'])
                self.assertEqual(record['push']['to'], [reminders._push_id(endpoint)])

    def test_web_push_retries_only_unreached_members_of_the_first_audience(self):
        import server

        class PushError(Exception):
            def __init__(self, status):
                self.response = SimpleNamespace(status_code=status)

        first, second, new = [f'https://example.test/{name}' for name in ('first', 'second', 'new')]

        def subscribe(*endpoints):
            push.save({endpoint: {'subscription': {'endpoint': endpoint}} for endpoint in endpoints}, store)

        def short_id(endpoint):
            return hashlib.sha256(endpoint.encode()).hexdigest()[:16]

        for outcome in ('retry', 'new device', 'unsubscribed', 'gone', 'crash'):
            with self.subTest(outcome=outcome), contextlib.ExitStack() as stack:
                rid = self.add(due=NOW + 60)['id']
                sender = mock.Mock(side_effect=[None, {
                    'gone': PushError(410), 'crash': SystemExit(),
                }.get(outcome, PushError(503))])
                channels, store = self.mock_web_push(stack, sender, error=PushError)
                push = server.push
                subscribe(first, second)
                started = NOW + 60
                if outcome == 'crash':
                    with self.assertRaises(SystemExit):
                        reminders.tick(channels, now=started)
                else:
                    initial = [(rid, 'push')] if outcome == 'gone' else []
                    self.assertEqual(reminders.tick(channels, now=started), initial)
                record = self.stored()['reminders'][-1]
                self.assertCountEqual(record['push']['audience'], [short_id(first), short_id(second)])
                self.assertEqual(record['push']['to'], [short_id(first)])
                self.assertEqual([c.kwargs['subscription_info']['endpoint'] for c in sender.call_args_list],
                                 [first, second])

                if outcome in ('crash', 'new device'):
                    subscribe(first, second, new)
                elif outcome == 'unsubscribed':
                    push.unsubscribe(second, store)
                elif outcome == 'gone':
                    self.assertNotIn(second, push.load(store))
                sender.reset_mock(side_effect=True)
                sender.return_value = None
                completed = [] if outcome == 'gone' else [(rid, 'push')]
                retry_at = started + (120 if outcome == 'crash' else 30)
                self.assertEqual(reminders.tick(channels, now=retry_at), completed)
                expected = [second] if outcome in ('retry', 'new device', 'crash') else []
                self.assertEqual([c.kwargs['subscription_info']['endpoint'] for c in sender.call_args_list], expected)
                self.assertEqual(self.stored()['reminders'][-1]['sent'], ['push'])

                if outcome == 'new device':
                    reminders.change(rid, 'snooze', {'occ': 1, 'for': 60}, now=started + 30)
                    self.assertNotIn('push', self.stored()['reminders'][-1])
                    sender.reset_mock()
                    self.assertEqual(reminders.tick(channels, now=started + 90), [(rid, 'push')])
                    self.assertEqual([c.kwargs['subscription_info']['endpoint'] for c in sender.call_args_list],
                                     [first, second, new])


class CliTests(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(agent_reminder_cli.agent_backlog_cli, 'project_name', return_value='tether'), \
                mock.patch.object(agent_reminder_cli.agent_backlog_cli, 'guess_agent', return_value='claude'), \
                mock.patch.dict(os.environ, {'TMUX_PANE': '%5', 'CLAUDE_CODE_SESSION_ID': 'sess-1'}):
            code = agent_reminder_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_add_list_snooze_done_delete(self):
        code, out, _ = self.run_cli('add', 'Fable check', '--at', '2027-11-19 10:00', '--pane', '%5', '--item', 'b224')
        self.assertEqual(code, 0)
        self.assertIn('R1  2027-11-19 10:00 IST (2027-11-19T04:30:00Z)  scheduled  Fable check', out)
        record = self.stored()['reminders'][0]
        self.assertEqual(record['link']['pane'], '%5')
        self.assertEqual((record['link']['item'], record['link']['project']), ('B224', 'tether'))
        self.assertTrue(record['by'])
        self.assertIn('R1', self.run_cli('list')[1])
        self.assertEqual(self.run_cli('snooze', 'R1', '--until', 'in 2h')[0], 0)
        self.assertIn('Done: R1', self.run_cli('done', 'r1')[1])
        self.assertEqual(self.run_cli('list')[1].strip(), 'No reminders.')
        self.assertIn('done', self.run_cli('list', '--all')[1])
        self.assertEqual(self.run_cli('delete', 'R1')[1].strip(), 'Deleted R1.')

    def test_errors_go_to_stderr(self):
        code, out, err = self.run_cli('add', 'x', '--at', '2000-01-01 10:00')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('Pick a time in the future', err)
        self.assertEqual(self.run_cli('done', 'R9')[0], 1)
        self.assertEqual(self.run_cli('add', 'x', '--at', 'soon')[0], 1)

    def test_invalid_clock_values_and_dates_report_a_cli_error(self):
        for when in ('tomorrow 25:00', '23:60', '2026-13-01 10:00', 'tomorrow 10:99', '24:15', '2027-02-29 10:00'):
            with self.subTest(when=when):
                code, out, err = self.run_cli('add', 'x', '--at', when)
                self.assertEqual((code, out), (1, ''))
                self.assertIn('Cannot read the time', err)

    def test_no_project(self):
        self.run_cli('add', 'x', '--at', 'in 1h', '--no-project')
        self.assertIsNone(self.stored()['reminders'][0]['link'])


class RepeatTests(Base):
    @staticmethod
    def ist(day, hour=10, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()

    def complete(self, item, now):
        return reminders.change(item['id'], 'done', {'occ': item['occ']}, now=now)

    def following(self, done, now):
        return next(item for item in reminders.listing(now=now)['reminders'] if item['id'] == done['next_id'])

    def test_daily_done_before_due_moves_the_rule_to_a_fresh_copy(self):
        anchor = self.ist(19)
        link = {'pane': '%5', 'item': 'B227'}
        item = self.add('Read the report', due=anchor, by='codex', link=link, repeat='day')
        self.assertEqual(item['repeat'], {'every': 'day', 'anchor': anchor, 'zone': 'Asia/Kolkata'})
        self.assertIsNone(item['next_id'])
        now = self.ist(19, 9)
        done = self.complete(item, now)
        upcoming = self.following(done, now)
        self.assertEqual((done['state'], done['repeat'], done['next_id']), ('done', None, 'R2'))
        self.assertNotEqual(item['id'], upcoming['id'])
        self.assertEqual((upcoming['text'], upcoming['link'], upcoming['by']), (item['text'], link, 'codex'))
        self.assertEqual((upcoming['created'], upcoming['due']), (now, self.ist(20)))
        self.assertEqual(upcoming['repeat'], {**item['repeat'], 'anchor': self.ist(20), 'origin': anchor})
        self.assertEqual((upcoming['state'], upcoming['occ'], upcoming['snoozes']), ('scheduled', 1, 0))

    def test_weekday_moves_from_friday_to_monday(self):
        item = self.add(due=self.ist(20), repeat='weekday')
        self.assertEqual(self.following(self.complete(item, self.ist(20)), self.ist(20))['due'], self.ist(23))

    def test_weekly_moves_to_the_same_weekday(self):
        item = self.add(due=self.ist(19), repeat='week')
        self.assertEqual(self.following(self.complete(item, self.ist(19)), self.ist(19))['due'], self.ist(26))

    def test_weekday_keeps_a_saturday_first_due(self):
        anchor = self.ist(21)
        item = self.add(due=anchor, repeat='weekday')
        sent = []
        reminders.tick({'push': lambda item: sent.append(item['due']) or True}, now=anchor)
        self.assertEqual(sent, [anchor])
        done = self.complete(item, anchor)
        upcoming = self.following(done, anchor)
        self.assertEqual(upcoming['due'], self.ist(23))
        stored = next(record for record in self.stored()['reminders'] if record['id'] == upcoming['id'])
        self.assertEqual([stored[key] for key in ('sent', 'tries', 'retry_at', 'sending', 'failed', 'fired_at', 'late')],
                         [[], {}, {}, {}, [], None, False])

    def test_catch_up_creates_only_one_strictly_future_copy(self):
        anchor = self.ist(9)
        item = reminders.add('Late report', anchor, now=self.ist(8), repeat='day')
        now = self.ist(19)
        count = len(reminders.listing(now=now)['reminders'])
        done = self.complete(item, now)
        self.assertEqual(len(reminders.listing(now=now)['reminders']), count + 1)
        self.assertEqual(self.following(done, now)['due'], self.ist(20))
        weekly = reminders.add('Old weekly report', anchor, now=self.ist(8), repeat='week')
        self.assertEqual(self.following(self.complete(weekly, now), now)['due'], self.ist(23))

    def test_snooze_keeps_the_anchor_for_short_and_long_snoozes(self):
        anchor = self.ist(19)
        for day, next_day in ((19, 20), (21, 22)):
            with self.subTest(day=day):
                item = self.add(due=anchor, repeat='day')
                until, now = self.ist(day, 18), self.ist(day, 18, 5)
                snoozed = reminders.change(item['id'], 'snooze', {'occ': item['occ'], 'until': until}, now=anchor)
                self.assertEqual((snoozed['due'], snoozed['repeat']['anchor']), (until, anchor))
                self.assertEqual(self.following(self.complete(snoozed, now), now)['due'], self.ist(next_day))

    def test_done_is_idempotent_and_reopened_copy_is_one_off(self):
        item = self.add(due=self.ist(19), repeat='day')
        now = self.ist(19, 9)
        done = self.complete(item, now)
        stored = self.stored()
        self.assertEqual(self.complete(item, now + 1), done)
        self.assertEqual(self.stored(), stored)
        reopened = reminders.change(item['id'], 'reopen', now=now + 2)
        self.assertIsNone(reopened['repeat'])
        self.complete(reopened, now + 3)
        self.assertEqual(len(self.stored()['reminders']), 2)

    def test_delete_and_restore_preserve_the_open_rule(self):
        item = self.add(due=self.ist(19), repeat='day')
        now = self.ist(19)
        reminders.change(item['id'], 'delete', now=now)
        listed = reminders.listing(now=now)
        self.assertEqual(listed['reminders'], [])
        self.assertEqual(listed['deleted'][0]['repeat'], item['repeat'])
        self.assertEqual(reminders.restore(item['id'], now=now), item)
        done = self.complete(item, now)
        reminders.change(done['id'], 'delete', now=now)
        self.assertEqual(self.following(done, now)['due'], self.ist(20))

    def test_rule_only_edit_keeps_schedule_and_delivery_state(self):
        anchor = self.ist(19)
        item = self.add(due=anchor, repeat='day')
        snoozed = reminders.change(item['id'], 'snooze', {'occ': 1, 'for': 3600}, now=anchor)
        reminders.tick({'push': lambda _: False, 'ping': lambda _: True}, now=snoozed['due'])
        before = self.stored()['reminders'][0]
        edited = reminders.change(item['id'], 'edit', {'repeat': 'week'}, now=snoozed['due'])
        rule = {'every': 'week', 'anchor': anchor, 'zone': 'Asia/Kolkata'}
        self.assertEqual(edited['repeat'], rule)
        self.assertEqual(self.stored()['reminders'][0], {**before, 'repeat': rule})

    def test_edit_due_reanchors_and_stop_repeating_creates_no_copy(self):
        item = self.add(due=self.ist(19))
        rid = item['id']
        edited = reminders.change(rid, 'edit', {'repeat': 'day'}, now=NOW)
        self.assertEqual(edited['repeat']['anchor'], self.ist(19))
        edited = reminders.change(rid, 'edit', {'due': self.ist(20)}, now=NOW)
        self.assertEqual(edited['repeat']['anchor'], self.ist(20))
        edited = reminders.change(rid, 'edit', {'due': self.ist(21), 'repeat': 'weekday'}, now=NOW)
        self.assertEqual(edited['repeat'], {'every': 'weekday', 'anchor': self.ist(21), 'zone': 'Asia/Kolkata'})
        stopped = reminders.change(rid, 'edit', {'repeat': None}, now=NOW)
        self.assertIsNone(stopped['repeat'])
        self.assertIsNone(self.complete(stopped, NOW)['next_id'])
        self.assertEqual(len(self.stored()['reminders']), 1)

    def test_repeat_edits_on_done_and_bad_rules_are_rejected(self):
        for rule in ('month', 'never', True, []):
            with self.subTest(rule=rule):
                with self.assertRaises(reminders.ReminderError) as error:
                    self.add(repeat=rule)
                self.assertEqual((error.exception.status, str(error.exception)),
                                 (400, 'Pick a repeat: never, day, weekday or week.'))
        item = self.add()
        with self.assertRaises(reminders.ReminderError) as error:
            reminders.change(item['id'], 'edit', {'repeat': 'month'}, now=NOW)
        self.assertEqual(error.exception.status, 400)
        self.complete(item, NOW)
        for rule in ('day', None):
            with self.subTest(rule=rule):
                with self.assertRaises(reminders.ReminderError) as error:
                    reminders.change(item['id'], 'edit', {'repeat': rule}, now=NOW)
                self.assertEqual((error.exception.status, str(error.exception)),
                                 (409, 'That reminder is done. Reopen it first.'))

    def test_v1_upgrade_keeps_all_fields_and_clears_invalid_rules(self):
        self.folder.mkdir()
        source = self.folder / 'reminders.json'
        full = {
            'id': 'R2', 'text': 'Keep delivery state', 'due': self.ist(19), 'created': NOW - 600, 'by': 'claude',
            'state': 'due', 'fired_at': NOW, 'late': True, 'snoozes': 2, 'done_at': None, 'link': {'pane': '%5'},
            'occ': 3, 'sent': ['ping'], 'tries': {'push': 2}, 'retry_at': {'push': NOW + 60},
            'sending': {'push': {'until': NOW + 120}}, 'failed': ['ping'],
            'push': {'audience': ['a', 'b'], 'to': ['a']},
        }
        records = [dict(full, id='R1', text='Keep this too'), full]
        deleted = [dict(full, id='R3', deleted_at=NOW)]
        source.write_text(json.dumps({'version': 1, 'next': 4, 'reminders': records, 'deleted': deleted}))
        listed = reminders.listing(now=NOW)
        private = {'sent', 'tries', 'retry_at', 'sending', 'push'}
        for stored, item in zip(records + deleted, listed['reminders'] + listed['deleted'], strict=True):
            self.assertEqual(item, {key: value for key, value in stored.items() if key not in private}
                             | {'repeat': None, 'next_id': None})
        self.add()
        saved = self.stored()
        self.assertEqual((saved['version'], saved['next']), (2, 5))
        for original, migrated in zip(records + deleted, saved['reminders'][:2] + saved['deleted'], strict=True):
            self.assertIsNone(migrated.pop('repeat'))
            self.assertEqual(json.dumps(migrated), json.dumps(original))

        valid = {'every': 'day', 'anchor': self.ist(19), 'zone': 'Asia/Kolkata'}
        rules = [{'every': 'month'}, 'day', {**valid, 'anchor': True}, {**valid, 'anchor': float('inf')},
                 {**valid, 'anchor': float('nan')}, {**valid, 'zone': 5}]
        saved['reminders'] = [dict(full, id=f'R{i}', repeat=rule) for i, rule in enumerate(rules, 10)]
        source.write_text(json.dumps(saved))
        self.assertEqual([item['repeat'] for item in reminders.listing(now=NOW)['reminders']], [None] * len(rules))

    def test_wall_clock_survives_dst(self):
        zone = ZoneInfo('America/New_York')
        anchor = datetime(2026, 10, 31, 9, tzinfo=zone).timestamp()
        expected = datetime(2026, 11, 1, 9, tzinfo=zone).timestamp()
        with mock.patch.object(reminders, 'ZONE', zone):
            item = reminders.add('Across DST', anchor, now=anchor - 3600, repeat='day')
            self.assertEqual(item['repeat']['zone'], zone.key)
            self.assertEqual(self.following(self.complete(item, anchor), anchor)['due'], expected)

    def test_wall_clock_survives_a_skipped_hour(self):
        zone = ZoneInfo('America/New_York')
        at = lambda day, hour=2: datetime(2026, 3, day, hour, 30, tzinfo=zone).timestamp()
        for every, first, gap, after in (('day', 7, 8, 9), ('week', 1, 8, 15)):
            with self.subTest(every=every), mock.patch.object(reminders, 'ZONE', zone):
                item = reminders.add('Across spring-forward', at(first), now=at(first) - 3600, repeat=every)
                shifted = self.following(self.complete(item, at(first)), at(first))
                self.assertEqual(shifted['due'], at(gap, 3))
                # A rule edit on the moved copy keeps the series wall time.
                shifted = reminders.change(shifted['id'], 'edit', {'repeat': every}, now=at(first))
                self.assertEqual(self.following(self.complete(shifted, at(gap, 4)), at(gap, 4))['due'], at(after))

    def test_unknown_stored_zone_falls_back_to_default(self):
        anchor = self.ist(19)
        item = self.add(due=anchor, repeat='day')
        stored = self.stored()
        stored['reminders'][0]['repeat']['zone'] = 'Unknown/Zone'
        (self.folder / 'reminders.json').write_text(json.dumps(stored))
        self.assertEqual(self.following(self.complete(item, anchor), anchor)['due'], self.ist(20))

    def test_http_add_and_edit_round_trip(self):
        anchor = self.ist(19)

        def call(action, body):
            handler = Handler(f'{reminders.PREFIX}/{action}')
            reminders.serve(handler, body)
            status, data = handler.sent
            self.assertEqual(status, 200)
            return data['reminder']

        with mock.patch.object(reminders.time, 'time', return_value=NOW):
            item = call('add', {'text': 'Read the report', 'due': anchor, 'repeat': 'weekday'})
            self.assertEqual(item['repeat'], {'every': 'weekday', 'anchor': anchor, 'zone': 'Asia/Kolkata'})
            item = call('edit', {'id': item['id'], 'repeat': 'week'})
            self.assertEqual(item['repeat'], {'every': 'week', 'anchor': anchor, 'zone': 'Asia/Kolkata'})
            self.assertIsNone(call('edit', {'id': item['id'], 'repeat': None})['repeat'])

    def test_cli_add_list_and_done_show_the_rule(self):
        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(agent_reminder_cli.main(argv), 0)
            return out.getvalue()

        with mock.patch.object(reminders.time, 'time', return_value=NOW), \
                mock.patch.object(agent_reminder_cli.agent_backlog_cli, 'guess_agent', return_value='claude'):
            run('add', 'Read the report', '--at', '2026-11-19 10:00', '--every', 'weekday', '--no-project')
            self.assertEqual(self.stored()['reminders'][0]['repeat']['every'], 'weekday')
            self.assertIn('scheduled  every weekday  Read the report', run('list'))
            self.assertIn('Next: R2  2026-11-20 10:00 IST', run('done', 'R1'))
            self.assertIn('Next: R2  2026-11-20 10:00 IST', run('done', 'R1'))
            self.assertEqual(len(self.stored()['reminders']), 2)
            run('add', 'Weekly report', '--at', '2026-11-19 10:00', '--every', 'week', '--no-project')
            self.assertIn('scheduled  every week (Thu)  Weekly report', run('list'))


if __name__ == '__main__':
    unittest.main()
