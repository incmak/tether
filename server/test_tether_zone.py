import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import advisor_log
import agent_audit_cli
import backlog
import prefs_sync
import server
import tether_zone
import work_triage


class TetherZoneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(prefs_sync, 'FILE', self.root / 'cache' / 'prefs.json'))
        self.enterContext(mock.patch.dict(os.environ, {'MOBILE_TMUX_CACHE': str(self.root / 'cache'),
                                                       'MOBILE_TMUX_CONFIG': str(self.root / 'config'),
                                                       'TETHER_REMINDER_ZONE': ''}))

    def test_default_when_the_file_is_missing(self):
        with mock.patch.object(prefs_sync, 'load') as load:
            self.assertEqual(tether_zone.name(), 'Asia/Kolkata')
            load.assert_not_called()

    def test_preference_wins_over_the_legacy_environment(self):
        prefs_sync.save({'timeZone': 'America/New_York'})
        with mock.patch.dict(os.environ, {'TETHER_REMINDER_ZONE': 'Pacific/Auckland'}):
            self.assertEqual(tether_zone.zone().key, 'America/New_York')

    def test_legacy_environment_is_used_when_the_preference_is_unset(self):
        prefs_sync.save({'theme': 'dark'})
        with mock.patch.dict(os.environ, {'TETHER_REMINDER_ZONE': 'Pacific/Auckland'}):
            self.assertEqual(tether_zone.zone().key, 'Pacific/Auckland')

    def test_invalid_preference_and_environment_are_ignored(self):
        prefs_sync.FILE.parent.mkdir()
        for prefs in ({'timeZone': '../etc'}, {'timeZone': 42}, ['malformed prefs']):
            with self.subTest(prefs=prefs):
                prefs_sync.FILE.write_text(json.dumps({'prefs': prefs}))
                with mock.patch.dict(os.environ, {'TETHER_REMINDER_ZONE': 'Pacific/Auckland'}):
                    self.assertEqual(tether_zone.name(), 'Pacific/Auckland')
                with mock.patch.dict(os.environ, {'TETHER_REMINDER_ZONE': 'Not/AZone'}):
                    self.assertEqual(tether_zone.name(), 'Asia/Kolkata')

    def test_cache_reuses_the_read_and_detects_a_same_size_save(self):
        prefs_sync.save({'timeZone': 'America/New_York', 'drafts': {'%1': {'text': 'Draft', 'at': 10}}})
        os.utime(prefs_sync.FILE, ns=(1_000_000_000, 1_000_000_000))
        size = prefs_sync.FILE.stat().st_size
        with mock.patch.object(prefs_sync, 'load', wraps=prefs_sync.load) as load:
            self.assertEqual(tether_zone.name(), 'America/New_York')
            self.assertEqual(tether_zone.name(), 'America/New_York')
            load.assert_called_once_with()
        prefs_sync.save({'timeZone': 'Pacific/Auckland'})
        self.assertEqual(prefs_sync.FILE.stat().st_size, size)
        self.assertEqual(tether_zone.zone().key, 'Pacific/Auckland')
        prefs_sync.FILE.unlink()
        self.assertEqual(tether_zone.name(), 'Asia/Kolkata')

    def test_get_prefs_adds_the_effective_zone_and_keeps_existing_keys(self):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/prefs'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        with mock.patch.dict(os.environ, {'TETHER_REMINDER_ZONE': 'Pacific/Auckland'}):
            handler.do_GET()
            handler.respond.assert_called_once_with(
                200, {'prefs': {'task_auto_close': False}, 'version': 0, 'zone': 'Pacific/Auckland'})
            prefs_sync.save({'timeZone': 'America/New_York', 'theme': 'dark'})
            handler.respond.reset_mock()
            handler.do_GET()
            handler.respond.assert_called_once_with(
                200, {'prefs': {'task_auto_close': False, 'timeZone': 'America/New_York', 'theme': 'dark'},
                      'version': 1, 'zone': 'America/New_York'})

    def test_stamp_round_trip_across_dst_and_current_zone_rule(self):
        prefs_sync.save({'timeZone': 'America/New_York'})
        for day, hour, offset in ((7, 15, -5), (8, 14, -4)):
            with self.subTest(day=day):
                at = datetime(2026, 3, day, hour, tzinfo=timezone.utc).timestamp()
                with mock.patch.object(tether_zone.time, 'time', return_value=at):
                    self.assertEqual(tether_zone.now().utcoffset().total_seconds(), offset * 3600)
                    shown = tether_zone.stamp()
                self.assertEqual(shown, f'2026-03-{day:02d} 10:00')
                self.assertEqual(tether_zone.parse(shown), at)
        prefs_sync.save({'timeZone': 'Pacific/Auckland'})
        self.assertEqual(tether_zone.parse(shown), datetime(2026, 3, 7, 21, tzinfo=timezone.utc).timestamp())

    def test_post_prefs_returns_effective_zone(self):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = 'POST', '/mobile/api/prefs'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        for version, chosen in enumerate(('America/New_York', 'Asia/Calcutta'), 1):
            with self.subTest(chosen=chosen):
                handler.respond.reset_mock()
                body = json.dumps({'changes': {'timeZone': chosen}}).encode()
                handler.rfile = io.BytesIO(body)
                handler.headers = {'Host': 'local', 'Origin': 'http://local', 'Content-Length': str(len(body)),
                                   'X-Codex-Mobile': '1'}
                handler.do_POST()
                handler.respond.assert_called_once_with(
                    200, {'prefs': {'task_auto_close': False, 'timeZone': chosen}, 'version': version, 'zone': chosen})

    def test_repeated_hour_stamp_reads_as_the_later_occurrence(self):
        prefs_sync.save({'timeZone': 'America/New_York'})
        at = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc).timestamp()
        first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp()
        shown = tether_zone.stamp(at)
        self.assertEqual(shown, '2026-11-01 01:30')
        self.assertEqual(tether_zone.stamp(first), shown)
        self.assertEqual(tether_zone.parse(shown), at)

    def test_stamp_parsing_handles_half_hour_folds_gaps_and_unambiguous_times(self):
        for zone, stamp, expected in (
            ('Australia/Lord_Howe', '2026-04-05 01:45', '2026-04-04T15:15:00Z'),
            ('Australia/Lord_Howe', '2026-10-04 02:15', '2026-10-03T15:45:00Z'),
            ('America/New_York', '2026-03-08 02:30', '2026-03-08T07:30:00Z'),
            ('America/New_York', '2026-11-01 02:30', '2026-11-01T07:30:00Z'),
        ):
            with self.subTest(zone=zone, stamp=stamp):
                prefs_sync.save({'timeZone': zone})
                self.assertEqual(tether_zone.parse(stamp), datetime.fromisoformat(expected).timestamp())

    def test_exact_stamp_round_trips(self):
        rows = json.loads((Path(__file__).parent / 'fixtures' / 'zone_exact_stamps.json').read_text())
        for row in rows:
            with self.subTest(instant=row['instant']):
                prefs_sync.save({'timeZone': row['zone']})
                at = datetime.fromisoformat(row['instant']).timestamp()
                self.assertEqual(tether_zone.stamp_exact(at), row['stamp'])
                self.assertEqual(tether_zone.parse(tether_zone.stamp_exact(at)), at)

    def test_explicit_offset_ignores_the_current_zone(self):
        prefs_sync.save({'timeZone': 'Pacific/Auckland'})
        for stamp, instant in (('2026-11-01 01:30 -0400', '2026-11-01T05:30:00Z'),
                               ('2026-04-05 01:45 +10:30', '2026-04-04T15:15:00Z')):
            with self.subTest(stamp=stamp):
                self.assertEqual(tether_zone.parse(stamp), datetime.fromisoformat(instant).timestamp())

    def test_backlog_exact_done_stamp_readers(self):
        for stamp, instant in (('2026-11-01 01:30 -0400', '2026-11-01T05:30:00Z'),
                               ('2026-04-05 01:45 +10:30', '2026-04-04T15:15:00Z')):
            at = datetime.fromisoformat(instant).timestamp()
            block = ['- [x] Task #B1', f'  - Done: {stamp} · finished (codex · pane %12)']
            with self.subTest(stamp=stamp, reader='_done_time'):
                self.assertEqual(backlog._done_time(block), at)
            with self.subTest(stamp=stamp, reader='_card'):
                card = backlog._card(block, 0, 'Inbox', 0, at)
                self.assertIsNotNone(card)
                self.assertEqual((card['doneAt'], card['done']), (at, 'finished (codex · pane %12)'))
            with self.subTest(stamp=stamp, reader='_finished'):
                text = '\n'.join(block)
                self.assertEqual(backlog._finished(text, True, keep_since=at)[0], [])
                self.assertEqual(backlog._finished(text, True, keep_since=at + 1)[0], [('Inbox', block)])
            with self.subTest(stamp=stamp, reader='_pane_hint'):
                self.assertEqual(backlog._pane_hint('backlog.done', block), '%12')

    def test_exact_offsets_in_triage_ledger_and_advisor_markdown(self):
        for stamp, instant in (('2026-11-01 01:30 -0400', '2026-11-01T05:30:00Z'),
                               ('2026-04-05 01:45 +10:30', '2026-04-04T15:15:00Z')):
            at = datetime.fromisoformat(instant).timestamp()
            with self.subTest(stamp=stamp, reader='triage'):
                rows = work_triage.parse(f'- {stamp} | T-1 | fixing | result | ref: pl#9')
                self.assertEqual(rows, [(stamp, 'T-1', 'fixing', 'result', 'pl#9', at)])
            with self.subTest(stamp=stamp, reader='advisor'):
                entry = advisor_log._legacy(f'- {stamp} [decided] Q: Question -> A: Answer (claude opus)')
                self.assertIsNotNone(entry)
                self.assertEqual(entry['time'], at)

    def test_backlog_and_triage_write_offsets_and_advisor_keeps_its_display_stamp(self):
        at = datetime(2026, 11, 19, 1, 42, 38, tzinfo=timezone.utc).timestamp()
        minute = at - 38
        for zone, shown, offset in (('America/New_York', '2026-11-18 20:42', '-0500'),
                                    ('Pacific/Auckland', '2026-11-19 14:42', '+1300')):
            exact = f'{shown} {offset}'
            with self.subTest(zone=zone):
                prefs_sync.save({'timeZone': zone})
                with mock.patch.object(tether_zone.time, 'time', return_value=at):
                    done = backlog._done_line('finished', '')
                entry = advisor_log.make('Question', {'status': 'decided', 'answer': 'Answer'}, 'claude', 'opus', now=at)
                line = advisor_log.line(entry)
                self.assertTrue(line.startswith(f'- {shown} [decided]'))
                self.assertEqual(entry['time'], at)
                self.assertEqual(advisor_log._legacy(line)['time'], minute)
                self.assertEqual(work_triage.parse_time(shown), minute)
            with self.subTest(zone=zone, writer='backlog'):
                self.assertIn(f'Done: {exact} ·', done)
                self.assertEqual(backlog._done_time(['- [x] Task #B1', done]), minute)
            with self.subTest(zone=zone, writer='triage'):
                self.assertEqual(work_triage.stamp(at), exact)
                self.assertEqual(work_triage.parse_time(exact), minute)

    def test_day_start_uses_the_first_instant_at_a_fold_or_gap(self):
        for zone, day, instant in (('America/Havana', '2026-11-01', '2026-11-01T04:00:00Z'),
                                    ('America/Havana', '2026-03-08', '2026-03-08T05:00:00Z'),
                                    ('Asia/Kathmandu', '1986-01-01', '1985-12-31T18:30:00Z')):
            with self.subTest(zone=zone, day=day):
                prefs_sync.save({'timeZone': zone})
                at = datetime.fromisoformat(instant).timestamp()
                self.assertEqual(tether_zone.day_start(day), at)
                self.assertEqual(tether_zone.day_start(datetime.fromisoformat(day).date()), at)

    def test_advisor_full_log_matches_its_markdown_after_a_zone_change(self):
        prefs_sync.save({'timeZone': 'America/New_York'})
        entry = advisor_log.make('Question', {'status': 'decided', 'answer': 'Answer'}, 'claude', 'opus', now=1_700_000_000)
        advisor_log.append(self.root, entry)
        prefs_sync.save({'timeZone': 'Pacific/Auckland'})
        rows = advisor_log.listing(self.root)
        self.assertEqual([(row['id'], row['time']) for row in rows], [(entry['id'], entry['time'])])

    def test_audit_date_filter_uses_the_same_midnight_as_display(self):
        prefs_sync.save({'timeZone': 'America/New_York'})
        midnight = agent_audit_cli.local_day('2026-11-19')
        self.assertEqual(midnight, datetime(2026, 11, 19, 5, tzinfo=timezone.utc).timestamp())
        self.assertEqual(tether_zone.stamp(midnight), '2026-11-19 00:00')
