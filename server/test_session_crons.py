"""Session cron jobs: names, schedules in words, next runs, and scheduled turns in the chat."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import agents
import claude_hooks
import paths
import session_crons
import tether_zone


def local(*parts):
    return datetime.datetime(*parts).timestamp()


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.zone = patch.dict(os.environ, TZ='UTC')
        self.zone.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(self.zone.stop)

    def test_next_run_for_common_shapes(self):
        now = local(2026, 11, 17, 12, 15, 30)  # a Tuesday
        cases = {
            '13,43 * * * *': (2026, 11, 17, 12, 43),
            '*/5 * * * *': (2026, 11, 17, 12, 20),
            '* * * * *': (2026, 11, 17, 12, 16),
            '0 9 * * 1-5': (2026, 11, 18, 9, 0),
            '0 9 * * 0': (2026, 11, 22, 9, 0),
            '0 9 * * 7': (2026, 11, 22, 9, 0),
            '30 */6 * * *': (2026, 11, 17, 12, 30),
            '0 0 29 2 *': (2028, 2, 29, 0, 0),
            '15 12 17 11 *': (2027, 11, 17, 12, 15),  # this minute has started: the next one is a year away
        }
        for schedule, expected in cases.items():
            with self.subTest(schedule):
                self.assertEqual(session_crons.next_run(schedule, now), local(*expected))

    def test_both_day_fields_limited_means_either(self):
        # Standard cron: the 1st of the month OR any Friday.
        self.assertEqual(session_crons.next_run('0 8 1 * 5', local(2026, 11, 17, 12, 0)), local(2026, 11, 20, 8, 0))

    def test_invalid_schedules_have_no_next_run_and_keep_their_text(self):
        for schedule in ('bad', '* * * *', '61 * * * *', '* * * * 8', '5-1 * * * *', '*/0 * * * *', '@hourly', None):
            with self.subTest(schedule):
                self.assertIsNone(session_crons.next_run(schedule, 0))
                self.assertEqual(session_crons.describe(schedule), schedule or '')

    def test_schedule_in_words(self):
        self.assertEqual(session_crons.describe('13,43 * * * *'), 'Every hour at :13 and :43')
        self.assertEqual(session_crons.describe('7 * * * *'), 'Every hour at :07')
        self.assertEqual(session_crons.describe('*/10 * * * *'), 'Every 10 minutes')
        self.assertEqual(session_crons.describe('* * * * *'), 'Every minute')
        self.assertEqual(session_crons.describe('0 9 * * *'), 'Every day at 09:00')
        self.assertEqual(session_crons.describe('0 9 * * 1-5'), 'Weekdays at 09:00')
        self.assertEqual(session_crons.describe('30 17 * * 5'), 'Every Friday at 17:30')
        self.assertEqual(session_crons.describe('0 */2 * * *'), 'Every 2 hours at :00')
        self.assertEqual(session_crons.describe('0 9 1 * *'), '0 9 1 * *')

    def test_schedule_says_server_time_when_offsets_differ(self):
        self.enterContext(patch.dict(os.environ, TZ='Asia/Kolkata'))
        time.tzset()
        # setUp restores TZ and resets the system time zone after this test.
        self.enterContext(patch.object(tether_zone, 'zone', return_value=ZoneInfo('America/New_York')))
        now = local(2026, 11, 21, 8, 0)
        [row] = session_crons.summary([{'id': 'c1', 'schedule': '0 9 * * *'}], now)
        self.assertEqual(row['every'], 'Every day at 09:00 (server time, IST)')
        self.assertEqual(row['next'], local(2026, 11, 21, 9, 0))

    def test_schedule_compares_offsets_at_the_next_run(self):
        self.enterContext(patch.dict(os.environ, TZ='America/Panama'))
        time.tzset()
        self.enterContext(patch.object(tether_zone, 'zone', return_value=ZoneInfo('America/New_York')))
        # New York has a different offset when we check and the same offset at the next run.
        now = local(2026, 10, 31, 12, 0)
        [row] = session_crons.summary([{'id': 'c1', 'schedule': '0 9 * * *'}], now)
        self.assertEqual(row['every'], 'Every day at 09:00')
        self.assertEqual(row['next'], local(2026, 11, 1, 9, 0))

    def test_label_is_the_start_of_the_prompt(self):
        self.assertEqual(session_crons.label('Compliance Internal watcher v2 (post only as BriskBot). Keep'), 'Compliance Internal watcher v2')
        self.assertEqual(session_crons.label('Morning summary.\nThen more'), 'Morning summary')
        self.assertEqual(session_crons.label('Check the deploy: run x'), 'Check the deploy')
        self.assertEqual(session_crons.label('Bump v1.2 and test'), 'Bump v1.2 and test')
        self.assertEqual(len(session_crons.label('x' * 200)), 60)
        self.assertEqual(session_crons.label(None), '')


class HookCronTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cache = Path(folder.name)
        for patcher in (patch.object(paths, 'CACHE', self.cache), patch.dict(os.environ, MOBILE_TMUX_CACHE=str(self.cache))):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.cache / 'chat.jsonl').write_text('')
        self.config = {'agent': 'claude', 'thread': 'abc123', 'transcript': str(self.cache / 'chat.jsonl')}
        self.job = {'id': 'c1', 'schedule': '*/5 * * * *', 'recurring': True, 'prompt': 'Check the build (CI). Then reply'}

    def feed(self, event, now, **extra):
        claude_hooks.capture(dict(session_id='abc123', hook_event_name=event, **extra), now)

    def test_overlay_and_tab_list_show_jobs_long_after_the_stop(self):
        stop = local(2026, 11, 17, 12, 0)
        host_zone = datetime.datetime.fromtimestamp(stop).astimezone().tzinfo
        self.enterContext(patch.object(tether_zone, 'zone', return_value=host_zone))
        self.feed('Stop', stop, session_crons=[self.job])
        later = stop + 3 * 3600 + 60  # far past the 30 minute task window: a job is still scheduled
        expected = {'id': 'c1', 'label': 'Check the build', 'schedule': '*/5 * * * *', 'every': 'Every 5 minutes',
                    'recurring': True, 'next': stop + 3 * 3600 + 300}
        self.assertEqual(claude_hooks.overlay(self.config, {}, later)['crons'], [expected])
        self.assertEqual(claude_hooks.crons(self.config, later), [expected])
        self.feed('Stop', later + 10, session_crons=[])
        self.assertEqual(claude_hooks.overlay(self.config, {}, later + 20)['crons'], [])
        self.assertEqual(claude_hooks.crons(self.config, later + 20), [])

    def test_no_jobs_for_ended_cleared_or_foreign_sessions(self):
        self.feed('Stop', 100, session_crons=[self.job])
        self.assertEqual(len(claude_hooks.crons(self.config, 110)), 1)
        self.assertEqual(claude_hooks.crons(dict(self.config, thread='other'), 110), [])
        self.assertEqual(claude_hooks.crons(dict(self.config, agent='codex'), 110), [])
        self.assertEqual(claude_hooks.crons(dict(self.config, thread='../x'), 110), [])
        # Compaction keeps the jobs; a new start (startup, resume, clear) drops them.
        self.feed('SessionStart', 115, source='compact')
        self.assertEqual(len(claude_hooks.crons(self.config, 118)), 1)
        self.feed('SessionStart', 120, source='startup')
        self.assertEqual(claude_hooks.crons(self.config, 130), [])
        self.assertNotIn('crons', claude_hooks.overlay(self.config, {}, 130))
        self.feed('Stop', 140, session_crons=[self.job])
        self.feed('SessionEnd', 150)
        self.assertEqual(claude_hooks.crons(self.config, 160), [])


class ClockChangeTests(unittest.TestCase):
    def setUp(self):
        self.zone = patch.dict(os.environ, TZ='America/New_York')
        self.zone.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(self.zone.stop)

    def test_repeated_hour_never_gives_a_past_run(self):
        now = datetime.datetime(2026, 11, 1, 1, 15, 30, fold=1).timestamp()  # the second 01:15, after clocks go back
        self.assertEqual(session_crons.next_run('*/5 * * * *', now), now - 30 + 300)
        first = datetime.datetime(2026, 11, 1, 1, 55, 30).timestamp()  # the first 01:55: 01:00 comes round again
        self.assertEqual(session_crons.next_run('0 * * * *', first), first - 30 + 300)

    def test_skipped_hour_does_not_run(self):
        now = datetime.datetime(2026, 3, 8, 1, 58).timestamp()  # 02:00-02:59 does not happen that night
        self.assertEqual(session_crons.next_run('*/5 * * * *', now), datetime.datetime(2026, 3, 8, 3, 0).timestamp())
        self.assertEqual(session_crons.next_run('30 2 * * *', now), datetime.datetime(2026, 3, 9, 2, 30).timestamp())


class ScheduledTurnTests(unittest.TestCase):
    def snapshot(self, records):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'chat.jsonl'
            path.write_text(''.join(json.dumps(record) + '\n' for record in records))
            return agents.claude_snapshot({'id': 'fake', 'thread': 'fake', 'agent': 'claude', 'transcript': str(path)}, lambda text: text)

    @staticmethod
    def fire(job, at, prompt='Inbox watcher (read only). Check mail'):
        return {'type': 'user', 'isMeta': True, 'promptSource': 'system', 'wakeupSource': 'schedule_wakeup', 'scheduledTaskId': job,
                'scheduledFireId': 'f-' + at, 'turnOrigin': 'scheduled', 'timestamp': at, 'message': {'role': 'user', 'content': prompt}}

    @staticmethod
    def say(text, at):
        return {'type': 'assistant', 'timestamp': at, 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}

    def test_fired_turns_are_marked_and_keep_their_last_result(self):
        records = [
            {'type': 'user', 'timestamp': 't0', 'message': {'role': 'user', 'content': 'Start the watcher'}},
            self.say('Started.', 't1'),
            self.fire('c1', 't2'),
            {'type': 'user', 'timestamp': 't3', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]}},
            self.say('Checking.', 't4'),
            self.say('Nothing new.\n\nNeeds you: no', 't5'),
            {'type': 'user', 'timestamp': 't6', 'message': {'role': 'user', 'content': 'Thanks, now fix the bug'}},
            self.say('Fixed the bug.', 't7'),
            self.fire('c2', 't8', [{'type': 'text', 'text': 'Deploy check'}]),
        ]
        data = self.snapshot(records)
        notices = [message for message in data['messages'] if message.get('title') == 'Scheduled run']
        self.assertEqual([(notice['id'], notice['cron'], notice['text'], notice['quiet'], notice['time']) for notice in notices],
                         [('2', 'c1', 'Inbox watcher', 'schedule', 't2'), ('8', 'c2', 'Deploy check', 'schedule', 't8')])
        self.assertFalse(any(message.get('text', '').startswith('Inbox watcher (read only)') for message in data['messages']))
        # The person's own prompt ends the scheduled turn: "Fixed the bug." is not the job's result.
        self.assertEqual(data['scheduled'], {'c1': {'at': 't2', 'message': '2', 'result': 'Nothing new.\n\nNeeds you: no'},
                                             'c2': {'at': 't8', 'message': '8', 'result': ''}})

    def test_queued_prompt_ends_the_scheduled_turn_but_a_compact_summary_does_not(self):
        records = [
            self.fire('c1', 't0'), self.say('Checking.', 't1'),
            {'type': 'user', 'isCompactSummary': True, 'timestamp': 't2', 'message': {'role': 'user', 'content': 'This session is being continued...'}},
            self.say('No new mail.', 't3'),
            {'type': 'attachment', 'timestamp': 't4', 'attachment': {'type': 'queued_command', 'prompt': '<task-notification><summary>Done</summary></task-notification>'}},
            self.say('Still nothing.', 't5'),
            {'type': 'attachment', 'timestamp': 't6', 'attachment': {'type': 'queued_command', 'prompt': 'Fix my bug'}},
            self.say('Fixed your bug.', 't7'),
        ]
        data = self.snapshot(records)
        self.assertEqual(data['scheduled'], {'c1': {'at': 't0', 'message': '0', 'result': 'Still nothing.'}})
        self.assertTrue(any(message.get('text') == 'Fix my bug' for message in data['messages']))

    def test_a_later_fire_replaces_the_last_run_and_sidechains_are_ignored(self):
        records = [self.fire('c1', 't0'), self.say('First.', 't1'), self.fire('c1', 't2'),
                   dict(self.fire('c9', 't3'), isSidechain=True), self.say('Second.', 't4')]
        data = self.snapshot(records)
        self.assertEqual(data['scheduled'], {'c1': {'at': 't2', 'message': '2', 'result': 'Second.'}})
        self.assertEqual(sum(message.get('title') == 'Scheduled run' for message in data['messages']), 2)


if __name__ == '__main__':
    unittest.main()
