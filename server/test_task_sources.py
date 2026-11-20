import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import agent_feed_cli
import backlog
import graph_client
import paths
import reminders
import server
import task_sources

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc).timestamp()
LISTS = '/me/todo/lists'
LIST = LISTS + '/personal/tasks'
PLANNER = '/me/planner/tasks'
TENANT = '11111111-2222-3333-4444-555555555555'
CLIENT = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
CONFIG = {'tenant': TENANT, 'client': CLIENT, 'refresh': 'private-refresh', 'account': 'test@example.com'}


def when(at, zone='UTC'):
    return {'dateTime': datetime.fromtimestamp(at, timezone.utc).replace(tzinfo=None).isoformat(), 'timeZone': zone}


def todo(task_id, at=None, **fields):
    return {'id': task_id, 'title': task_id, 'status': 'notStarted',
            'isReminderOn': at is not None, 'reminderDateTime': when(at) if at is not None else None, **fields}


def page(*rows, next_link=None):
    return {'value': list(rows), **({'@odata.nextLink': graph_client.GRAPH + next_link} if next_link else {})}


class TaskTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(paths, 'TETHER', root / 'tether'))
        self.enterContext(mock.patch.object(paths, 'CONFIG', root / 'config'))
        self.folder = root / 'task-data'
        self.enterContext(mock.patch.dict(os.environ, {'TETHER_TASK_SOURCES': str(self.folder),
                                                       'TETHER_REMINDERS': str(root / 'reminders')}))
        self.enterContext(mock.patch.object(task_sources, 'time', SimpleNamespace(time=lambda: NOW)))
        self.enterContext(mock.patch.dict(task_sources._PLAN_NAMES, clear=True))
        self.sync_now = self.enterContext(mock.patch.object(task_sources, 'sync_now'))
        self.calls = []
        self.pages = {LISTS: page({'id': 'personal', 'displayName': 'Personal'}), LIST: page(), PLANNER: page()}
        self.real_client = graph_client.Client
        client = mock.Mock(spec=graph_client.Client)
        client.get.side_effect = self.get_page
        self.client = self.enterContext(mock.patch.object(graph_client, 'Client', return_value=client))
        self.grant('tasks')

    def grant(self, *kinds):
        graph_client.save(dict(CONFIG, scopes=graph_client.scopes_for(kinds)))

    def get_page(self, url):
        self.calls.append(url)
        parsed = urlsplit(url)
        key = parsed.path.removeprefix('/v1.0')
        value = self.pages.get(url, self.pages.get(key))
        if value is None:
            raise AssertionError(f'Unexpected GET {url}')
        if isinstance(value, Exception):
            raise value
        return value

    def poll(self, *items):
        self.pages[LIST] = page(*items)
        task_sources.sync()
        return task_sources.listing()

    @staticmethod
    def active():
        return reminders.listing(NOW)['reminders']

    def alert(self):
        task_sources.settings_for('todo', alerts=True)

    def test_todo_reads_multiple_lists_and_pages_without_storing_bodies(self):
        self.pages[LISTS] = page({'id': 'personal', 'displayName': 'First'}, next_link='/lists-2')
        self.pages['/lists-2'] = page({'id': 'shared/id', 'displayName': 'Second List'})
        self.pages[LIST] = page(todo('middle', dueDateTime=when(NOW + 3600)), next_link='/tasks-2')
        self.pages['/tasks-2'] = page(todo('late', dueDateTime=when(NOW - 600)),
                                      todo('done', status='completed'))
        self.pages[LISTS + '/shared%2Fid/tasks'] = page(
            todo('other', title=' x\n  ' + 'y' * 200, body={'content': 'do not mirror this'},
                 notes='do not mirror this', importance='high', lastModifiedDateTime=when(NOW)['dateTime']))
        task_sources.sync()
        shown = task_sources.listing()
        self.assertEqual([row['key'] for row in shown['tasks']], ['todo:late', 'todo:middle', 'todo:other'])
        self.assertEqual(shown['tasks'][2]['title'], 'x ' + 'y' * 198)
        self.assertEqual((shown['tasks'][2]['list'], shown['tasks'][2]['importance'], shown['tasks'][2]['modified']),
                         ('Second List', 'high', NOW))
        query = parse_qs(urlsplit(self.calls[1]).query)
        self.assertEqual(query['$filter'], ["status ne 'completed'"])
        self.assertNotIn('body', query['$select'][0])
        self.assertNotIn('do not mirror this', (self.folder / 'mirror.json').read_text())
        self.assertEqual((self.folder / 'mirror.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual([(row['complete'], row['count']) for row in shown['sources']], [(True, 3), (True, 0)])
        self.assertEqual(shown['tasks'][0]['link'], task_sources.TODO_HOME)

    def test_query_fallback_still_filters_locally(self):
        first = task_sources._query(LIST, filter=task_sources.TODO_FILTER, select=task_sources.TODO_FIELDS)
        fallback = task_sources._query(LIST, select=task_sources.TODO_FIELDS)
        self.pages[first] = graph_client.GraphError('Unsupported query', 400)
        self.pages[fallback] = page(todo('open'), todo('done', status='completed'))
        shown = self.poll()
        self.assertEqual([task['key'] for task in shown['tasks']], ['todo:open'])
        self.assertEqual(self.calls[1:3], [first, fallback])

    def test_rejected_todo_projection_never_reads_unprojected_rows(self):
        before = self.poll(todo('keep'))['tasks']
        for path, fields in ((LIST, task_sources.TODO_FIELDS), (LISTS, 'id,displayName')):
            with self.subTest(path=path):
                self.calls.clear()
                rejected = graph_client.GraphError('Unsupported projection', 501)
                projected = {task_sources._query(path, select=fields): rejected}
                if path == LIST:
                    projected[task_sources._query(path, filter=task_sources.TODO_FILTER, select=fields)] = rejected
                with mock.patch.dict(self.pages, projected):
                    after = self.poll(todo('unprojected', body={'content': 'private'}))
                self.assertEqual(after['tasks'], before)
                self.assertEqual(after['sources'][0]['state'], 'error')
                self.assertFalse(after['sources'][0]['complete'])
                self.assertTrue(all('$select' in parse_qs(urlsplit(url).query)
                                    for url in self.calls if url.startswith(LISTS)))

    def test_overdue_timed_task_precedes_date_only_due_today(self):
        shown = self.poll(todo('z', title='Z last'), todo('tomorrow', dueDateTime='2026-11-20'),
                          todo('today', dueDateTime='2026-11-19'), todo('a', title='a first'),
                          todo('later', dueDateTime=when(NOW + 60)), todo('overdue', dueDateTime=when(NOW - 60)))
        self.assertEqual([row['key'] for row in shown['tasks']],
                         [f'todo:{key}' for key in ('overdue', 'today', 'later', 'tomorrow', 'a', 'z')])

    def test_enough_pages_to_hit_cap_with_next_link_preserves_old_state(self):
        self.alert()
        before = self.poll(todo('keep', NOW + 3600))
        self.pages[LIST] = page(todo('new', NOW + 4000), next_link='/page/2')
        for number in range(2, graph_client.PAGE_LIMIT + 1):
            self.pages[f'/page/{number}'] = page(next_link=f'/page/{number + 1}')
        task_sources.sync()
        after = task_sources.listing()
        self.assertEqual(after['tasks'], before['tasks'])
        self.assertEqual(after['sources'][0]['syncedAt'], before['sources'][0]['syncedAt'])
        self.assertFalse(after['sources'][0]['complete'])
        self.assertEqual(after['sources'][0]['state'], 'error')
        self.assertNotIn(graph_client.GRAPH + f'/page/{graph_client.PAGE_LIMIT + 1}', self.calls)
        self.assertEqual([(r['text'], r['state']) for r in self.active()], [('keep', 'scheduled')])
        self.pages[f'/page/{graph_client.PAGE_LIMIT}'] = page()
        task_sources.sync()
        self.assertTrue(task_sources.listing()['sources'][0]['complete'])
        self.assertEqual([r['state'] for r in self.active()], ['done', 'scheduled'])

    def test_error_on_later_page_is_local_to_source(self):
        self.alert()
        before = self.poll(todo('keep', NOW + 3600))
        self.pages[LIST] = page(next_link='/broken')
        self.pages['/broken'] = graph_client.GraphError('request diagnostic with private data', 503)
        self.pages[PLANNER] = page({'id': 'new', 'planId': 'p', 'title': 'Planner task', 'percentComplete': 0})
        self.pages['/planner/plans/p'] = {'title': 'Plan'}
        task_sources.sync()
        after = task_sources.listing()
        self.assertEqual(after['tasks'][0], before['tasks'][0])
        self.assertEqual([row['state'] for row in after['sources']], ['error', 'ok'])
        self.assertNotIn('private data', (self.folder / 'mirror.json').read_text())
        self.assertEqual(self.active()[0]['state'], 'scheduled')

    def test_completed_upstream_closes_open_reminder(self):
        self.alert()
        self.poll(todo('keep', NOW + 3600), todo('other', NOW + 3600))
        after = self.poll(todo('keep', NOW + 3600, status='completed'), todo('other', NOW + 3600))
        self.assertEqual([row['key'] for row in after['tasks']], ['todo:other'])
        self.assertEqual([r['state'] for r in self.active()], ['done', 'scheduled'])

    def test_unchanged_upstream_time_keeps_snooze_done_and_delete(self):
        self.alert()
        items = [todo(key, NOW + 3600) for key in ('snoozed', 'done', 'deleted')]
        self.poll(*items)
        first, second, third = [r['id'] for r in self.active()]
        reminders.change(first, 'snooze', {'occ': 1, 'until': NOW + 1800}, NOW)
        reminders.change(second, 'done', {'occ': 1}, NOW)
        reminders.change(third, 'delete', now=NOW)
        before = reminders.listing(NOW)
        self.poll(*items)
        self.assertEqual(reminders.listing(NOW), before)

    def test_changed_time_reschedules_with_new_occurrence_or_replaces_deleted(self):
        self.alert()
        self.poll(todo('task', NOW + 3600), todo('deleted', NOW + 3600))
        first, second = [r['id'] for r in self.active()]
        reminders.change(first, 'done', {'occ': 1}, NOW)
        reminders.change(second, 'delete', now=NOW)
        self.poll(todo('task', NOW + 7200), todo('deleted', NOW + 1800))
        r1, r2 = self.active()
        self.assertEqual((r1['id'], r1['due'], r1['state'], r1['occ'], r1['done_at']),
                         (first, NOW + 7200, 'scheduled', 2, None))
        self.assertEqual((r2['id'], r2['due'], r2['occ']), ('R3', NOW + 1800, 1))
        with self.assertRaises(reminders.ReminderError) as stale:
            reminders.change(first, 'done', {'occ': 1}, NOW)
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(reminders.ReminderError):
            reminders.restore(second, NOW)

    def test_restored_future_time_reopens_after_past_or_disabled_time(self):
        self.alert()
        original = NOW + 3600
        for intermediate in (NOW - 60, None):
            with self.subTest(intermediate=intermediate):
                self.poll(todo('done', original), todo('open', original))
                done = self.active()[0]
                reminders.change(done['id'], 'done', {'occ': done['occ']}, NOW)
                before = self.active()
                self.poll(todo('done', intermediate), todo('open', intermediate), todo('new', intermediate))
                self.assertEqual(self.active(), before)
                self.poll(todo('done', original), todo('open', original))
                self.assertEqual([(r['id'], r['due'], r['state'], r['occ']) for r in self.active()],
                                 [(r['id'], original, 'scheduled', r['occ'] + 1) for r in before])

    def test_retried_mirror_write_reuses_reminder(self):
        self.alert()
        self.pages[LIST] = page(todo('task', NOW + 3600))
        with mock.patch.object(task_sources, '_save', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                task_sources.sync()
        before = self.active()
        self.assertEqual(len(before), 1)
        after = self.poll(todo('task', NOW + 3600))
        self.assertEqual(after['tasks'][0]['reminder'], before[0]['id'])
        self.assertEqual(self.active(), before)

    def test_first_sync_and_reconnect_skip_past_and_due_only(self):
        self.alert()
        tasks = [todo('past', NOW - 10), todo('due-only', dueDateTime=when(NOW + 3600)),
                 todo('reminder-off', NOW + 7200, isReminderOn=False)]
        self.poll(*tasks)
        self.assertEqual(self.active(), [])
        self.grant('mail')
        self.poll(todo('another-past', NOW - 1))
        self.grant('mail', 'tasks')
        self.poll(todo('another-past', NOW - 1))
        self.assertEqual(self.active(), [])

    def test_alerts_off_does_not_create_or_close(self):
        self.poll(todo('no-alert', NOW + 3600))
        self.assertEqual(self.active(), [])
        self.alert()
        self.poll(todo('keep', NOW + 3600))
        task_sources.settings_for('todo', alerts=False)
        self.poll(todo('no-alert', NOW + 3600))
        self.assertEqual([(r['text'], r['state']) for r in self.active()], [('keep', 'scheduled')])

    def test_moving_list_with_new_id_closes_old_and_only_adds_future(self):
        self.alert()
        at = NOW + 3600
        self.poll(todo('old', at, title='Task'), todo('past-old', at, title='Past task'))
        self.pages[LISTS] = page({'id': 'shared', 'displayName': 'Shared'})
        self.pages[LISTS + '/shared/tasks'] = page(todo('new', at, title='Task'),
                                                  todo('past-new', NOW - 1, title='Past task'))
        moved = self.poll()
        self.assertEqual({row['key']: row['reminder'] for row in moved['tasks']},
                         {'todo:new': 'R3', 'todo:past-new': None})
        self.assertEqual([(r['source']['key'], r['state']) for r in self.active()],
                         [('todo:old', 'done'), ('todo:past-old', 'done'), ('todo:new', 'scheduled')])

    def test_move_overlap_keeps_both_links_until_a_complete_poll_closes_the_old_key(self):
        self.alert()
        at = NOW + 3600
        old = todo('old', at, title='Task')
        self.poll(old)
        self.pages[LISTS] = page({'id': 'personal'}, {'id': 'shared'})
        self.pages[LISTS + '/shared/tasks'] = page(todo('new', at, title='Task'))
        shown = self.poll(old)
        self.assertEqual({row['key']: row['reminder'] for row in shown['tasks']},
                         {'todo:old': 'R1', 'todo:new': 'R2'})

        self.pages[LISTS + '/shared/tasks'] = page(next_link='/busy')
        self.pages['/busy'] = graph_client.GraphError('Busy', 429)
        self.assertEqual(self.poll()['tasks'], shown['tasks'])
        self.assertEqual([r['state'] for r in self.active()], ['scheduled', 'scheduled'])

        self.pages[LISTS + '/shared/tasks'] = page(todo('new', at, title='Task'))
        self.assertEqual(self.poll()['tasks'][0]['reminder'], 'R2')
        self.assertEqual([(r['source']['key'], r['state']) for r in self.active()],
                         [('todo:old', 'done'), ('todo:new', 'scheduled')])

    def test_two_known_tasks_keep_separate_reminders_when_times_match(self):
        self.alert()
        first, second = NOW + 3600, NOW + 7200
        self.pages[LISTS] = page({'id': 'personal'}, {'id': 'shared'})
        self.pages[LISTS + '/shared/tasks'] = page(todo('second', second, title='Task'))
        self.poll(todo('first', first, title='Task'))
        one, two = self.active()
        reminders.change(two['id'], 'done', {'occ': 1}, NOW)
        self.pages[LISTS + '/shared/tasks'] = page(todo('second', first, title='Task'))
        shown = self.poll(todo('first', first, title='Task'))
        self.assertEqual({row['key']: row['reminder'] for row in shown['tasks']},
                         {'todo:first': one['id'], 'todo:second': two['id']})
        record = self.active()[1]
        self.assertEqual((record['id'], record['due'], record['state'], record['occ']),
                         (two['id'], first, 'scheduled', 2))

    def test_distinct_tasks_with_same_title_and_time_get_separate_reminders(self):
        self.alert()
        at = NOW + 3600
        original = todo('old', at, title='Task')
        self.poll(original)
        for task_id, same_list in (('local', True), ('shared', False)):
            with self.subTest(same_list=same_list):
                new = todo(task_id, at, title='Task')
                if same_list:
                    shown = self.poll(original, new)
                else:
                    self.pages[LISTS] = page({'id': 'personal'}, {'id': 'shared'})
                    self.pages[LISTS + '/shared/tasks'] = page(new)
                    shown = self.poll(original)
                links = {row['key']: row['reminder'] for row in shown['tasks']}
                self.assertEqual(links['todo:old'], 'R1')
                self.assertTrue(links[f'todo:{task_id}'])
                self.assertNotEqual(links[f'todo:{task_id}'], 'R1')
                self.assertEqual({r['source']['key']: r['id'] for r in self.active() if r['state'] == 'scheduled'},
                                 links)

    def test_new_task_keeps_its_reminder_when_matching_task_is_closed_locally_and_upstream(self):
        self.alert()
        at = NOW + 3600
        self.pages[LISTS] = page({'id': 'personal'}, {'id': 'shared'})
        self.pages[LISTS + '/shared/tasks'] = page()
        for action in ('done', 'delete'):
            with self.subTest(action=action):
                old = todo(f'{action}-old', at, title=action)
                new = todo(f'{action}-new', at, title=action)
                old_id = self.poll(old)['tasks'][0]['reminder']
                reminders.change(old_id, action, {'occ': 1}, now=NOW)
                self.pages[LISTS + '/shared/tasks'] = page(new)
                self.poll(old)
                shown = self.poll(dict(old, status='completed'))
                new_id = shown['tasks'][0]['reminder']
                self.assertNotEqual(new_id, old_id)
                self.assertEqual([(r['id'], r['source']['key']) for r in self.active() if r['state'] == 'scheduled'],
                                 [(new_id, f"todo:{new['id']}")])
                self.pages[LISTS + '/shared/tasks'] = page()

    def test_restore_after_a_time_change_uses_the_open_reminder_for_the_key(self):
        self.alert()
        at = NOW + 3600
        first = self.poll(todo('task', at))['tasks'][0]['reminder']
        reminders.change(first, 'delete', now=NOW)
        second = self.poll(todo('task', at + 60))['tasks'][0]['reminder']
        self.assertNotEqual(first, second)
        with self.assertRaises(reminders.ReminderError) as conflict:
            reminders.restore(first, now=NOW)
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual([(r['id'], r['state']) for r in self.active()], [(second, 'scheduled')])

        reminders.change(second, 'delete', now=NOW)
        self.assertEqual(reminders.restore(first, now=NOW)['state'], 'scheduled')
        shown = self.poll(todo('task', at + 60))
        self.assertEqual(shown['tasks'][0]['reminder'], first)
        self.assertEqual([(r['id'], r['state'], r['due']) for r in self.active()], [(first, 'scheduled', at)])
        reminders.change(first, 'snooze', {'occ': 1, 'until': at + 120}, now=NOW)
        self.assertEqual(self.poll(todo('task', at))['tasks'][0]['reminder'], first)
        self.assertEqual([(r['id'], r['state'], r['due']) for r in self.active()], [(first, 'scheduled', at)])

    def test_a_closed_linked_reminder_stays_linked_over_a_newer_closed_one(self):
        # Review round 4: creation time outranked the saved link, so the link moved to an older history record.
        for close_second in ('done', 'delete'):
            with self.subTest(second=close_second):
                key = f'task-{close_second}'
                clock = iter(NOW + step for step in range(100))
                self.enterContext(mock.patch.object(task_sources, 'time', SimpleNamespace(time=lambda: next(clock))))
                self.alert()
                at = NOW + 3600
                first = self.poll(todo(key, at))['tasks'][0]['reminder']
                reminders.change(first, 'delete', now=NOW)
                second = self.poll(todo(key, at + 60))['tasks'][0]['reminder']
                occ = next(r['occ'] for r in self.active() if r['id'] == second)
                if close_second == 'done':
                    reminders.change(second, 'done', {'occ': occ}, now=NOW)
                else:
                    reminders.change(second, 'delete', now=NOW)
                reminders.restore(first, now=NOW)
                self.assertEqual(self.poll(todo(key, at + 60))['tasks'][0]['reminder'], first)
                occ = next(r['occ'] for r in self.active() if r['id'] == first)
                reminders.change(first, 'done', {'occ': occ}, now=NOW)
                self.assertEqual(self.poll(todo(key, at + 60))['tasks'][0]['reminder'], first)
                self.assertEqual(self.poll(todo(key, at + 120))['tasks'][0]['reminder'], first)
                mine = [(r['id'], r['state'], r['due']) for r in self.active() if r['state'] != 'done']
                self.assertEqual(mine, [(first, 'scheduled', at + 120)])
                self.pages[LIST] = page()

    def test_a_live_reminder_beats_a_stale_link_to_a_deleted_one(self):
        # Review round 5: the mirror write failed after sync made R2, so the saved link still named deleted R1.
        for close_new in ('done', 'snooze-then-done'):
            with self.subTest(new=close_new):
                key = f'stale-{close_new}'
                clock = iter(NOW + step for step in range(100))
                self.enterContext(mock.patch.object(task_sources, 'time', SimpleNamespace(time=lambda: next(clock))))
                self.alert()
                at = NOW + 3600
                first = self.poll(todo(key, at))['tasks'][0]['reminder']
                reminders.change(first, 'delete', now=NOW)
                real_save = task_sources._save
                failed = []

                def fail_mirror_once(name, data):
                    if name == 'mirror.json' and not failed:
                        failed.append(name)
                        raise OSError('disk full')
                    return real_save(name, data)
                with mock.patch.object(task_sources, '_save', fail_mirror_once):
                    with self.assertRaises(OSError):
                        self.poll(todo(key, at + 60))
                second = next(r['id'] for r in self.active() if r['id'] != first and r['state'] != 'done'
                              and r['due'] == at + 60)
                occ = next(r['occ'] for r in self.active() if r['id'] == second)
                if close_new == 'snooze-then-done':
                    reminders.change(second, 'snooze', {'occ': occ, 'until': at + 90}, now=NOW)
                    occ = next(r['occ'] for r in self.active() if r['id'] == second)
                reminders.change(second, 'done', {'occ': occ}, now=NOW)
                self.assertEqual(self.poll(todo(key, at + 60))['tasks'][0]['reminder'], second)
                self.assertEqual(self.poll(todo(key, at + 120))['tasks'][0]['reminder'], second)
                mine = [(r['id'], r['state'], r['due']) for r in self.active() if r['state'] != 'done']
                self.assertEqual(mine, [(second, 'scheduled', at + 120)])
                self.pages[LIST] = page()

    def test_planner_reads_assigned_open_tasks_and_caches_plan_names(self):
        task_sources.settings_for('planner', alerts=True)
        self.pages[PLANNER] = page({'id': 'a/b', 'planId': 'plan-5', 'title': 'First', 'percentComplete': 0,
                                    'dueDateTime': '2026-11-19T21:00:00Z'}, next_link='/planner-next')
        self.pages['/planner-next'] = page({'id': '2', 'planId': 'plan-5', 'title': 'Second', 'percentComplete': 50},
                                           {'id': '3', 'planId': 'done', 'percentComplete': 100})
        self.pages['/planner/plans/plan-5'] = {'title': 'Plan Five'}
        first = self.poll()
        self.assertEqual([row['list'] for row in first['tasks']], ['Plan Five', 'Plan Five'])
        self.assertEqual(first['tasks'][0]['due']['date'], '2026-11-20')
        self.assertEqual(first['tasks'][0]['link'],
                         'https://planner.cloud.microsoft/webui/v1/plan/plan-5/view/board/task/a%2Fb')
        self.poll()
        self.assertEqual(self.calls.count('/planner/plans/plan-5?$select=title'), 1)
        self.assertEqual(self.active(), [])

    def test_planner_plan_without_access_keeps_other_tasks(self):
        self.pages[PLANNER] = page({'id': '1', 'planId': 'gone', 'title': 'Old plan task', 'percentComplete': 0},
                                   {'id': '2', 'planId': 'ok', 'title': 'Live task', 'percentComplete': 0})
        self.pages['/planner/plans/gone'] = graph_client.GraphError('Forbidden', 403)
        self.pages['/planner/plans/ok'] = {'title': 'Launch'}
        listed = self.poll()
        planner = next(source for source in listed['sources'] if source['id'] == 'planner')
        self.assertEqual(planner['state'], 'ok')
        self.assertEqual(sorted((row['title'], row['list']) for row in listed['tasks'] if row['source'] == 'planner'),
                         [('Live task', 'Launch'), ('Old plan task', task_sources.UNNAMED_PLAN)])
        self.pages['/planner/plans/ok'] = graph_client.GraphError('Server busy', 503)
        task_sources._PLAN_NAMES.clear()
        self.assertEqual(next(s for s in self.poll()['sources'] if s['id'] == 'planner')['state'], 'error')

    def test_unexpected_error_does_not_end_the_poller(self):
        with mock.patch.object(task_sources, 'sync', side_effect=KeyError('private title')), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            task_sources._tick()
        self.assertIn('KeyError', err.getvalue())
        self.assertNotIn('private title', err.getvalue())

    def test_missing_scope_lists_exact_reconnect_command(self):
        self.grant('mail', 'teams')
        task_sources.sync()
        self.client.assert_not_called()
        for row in task_sources.listing()['sources']:
            self.assertEqual(row['state'], 'off')
            self.assertEqual(row['command'], 'tether graph connect --tenant <tenant id> --client <app id> '
                                             '--feeds mail,teams,tasks')
        self.grant('teams')
        self.assertTrue(task_sources.listing()['sources'][0]['command'].endswith('--feeds teams,tasks'))
        graph_client.remove()
        self.assertTrue(task_sources.listing()['sources'][0]['command'].endswith('--feeds tasks'))

    def test_first_failed_poll_reports_error_without_exposing_another_accounts_tasks(self):
        self.alert()
        at = NOW + 3600
        self.pages[PLANNER] = page({'id': 'reused', 'planId': 'plan', 'title': 'Old Planner task'})
        self.pages['/planner/plans/plan'] = {'title': 'Plan'}
        self.poll(todo('reused', at, title='Old To Do task'), todo('uncopied'))
        old_copy = task_sources.to_backlog('todo:reused')['id']
        old_reminder = self.active()[0]
        other = dict(CONFIG, account='other@example.com', scopes=graph_client.scopes_for(['tasks']))
        for connected in (False, True):
            with self.subTest(connected=connected):
                if connected:
                    graph_client.save(other)
                    self.pages[LISTS] = self.pages[PLANNER] = graph_client.GraphError('Busy', 429)
                    task_sources.sync()
                else:
                    graph_client.remove()
                shown = task_sources.listing()
                self.assertEqual(shown['tasks'], [])
                self.assertEqual([(s['state'], s['syncedAt'], s['count']) for s in shown['sources']],
                                 [('error' if connected else 'off', None, 0)] * 2)
                if connected:
                    self.assertTrue(all(s['why'] == 'Microsoft could not sync this source (HTTP 429).'
                                        for s in shown['sources']))
                for key in ('todo:reused', 'todo:uncopied', 'planner:reused'):
                    with self.assertRaises(task_sources.TaskError) as missing:
                        task_sources.to_backlog(key)
                    self.assertEqual(missing.exception.status, 404)
        self.pages[LISTS] = page({'id': 'personal'})
        self.pages[PLANNER] = page({'id': 'reused', 'planId': 'plan', 'title': 'New Planner task'})
        shown = self.poll(todo('reused', at, title='New To Do task'))
        indexed = {row['source']: row for row in shown['tasks']}
        self.assertEqual(indexed['planner']['title'], 'New Planner task')
        self.assertEqual(indexed['todo']['title'], 'New To Do task')
        self.assertIsNone(indexed['todo']['backlog'])
        self.assertNotEqual(indexed['todo']['reminder'], old_reminder['id'])
        self.assertEqual(self.active()[0], old_reminder)
        new_copy = task_sources.to_backlog('todo:reused')
        self.assertTrue(new_copy['created'])
        self.assertNotEqual(new_copy['id'], old_copy)
        self.assertIn('Old To Do task', backlog.show(old_copy))

    def test_account_round_trip_preserves_backlog_copies_and_reminder_links(self):
        self.alert()
        at = NOW + 3600
        items = [todo('reused', at, title='First account'), todo('other', at)]
        original = {row['key']: row['reminder'] for row in self.poll(*items)['tasks']}
        copies = {key: task_sources.to_backlog(key)['id'] for key in original}
        graph_client.save(dict(CONFIG, account='other@example.com', scopes=graph_client.scopes_for(['tasks'])))
        self.poll(todo('reused', at, title='Second account'))
        self.assertTrue(task_sources.to_backlog('todo:reused')['created'])
        self.grant('tasks')
        returned = {row['key']: row for row in self.poll(*items)['tasks']}
        for key, copy_id in copies.items():
            with self.subTest(key=key):
                self.assertEqual(task_sources.to_backlog(key), {'id': copy_id, 'created': False})
                self.assertEqual(returned[key]['backlog'], copy_id)
                self.assertEqual(returned[key]['reminder'], original[key])
        self.assertEqual(len(backlog.board_items()), 3)
        self.assertEqual(len(self.active()), 3)

    def test_unowned_snapshot_is_hidden_until_replaced(self):
        self.poll(todo('legacy'))
        saved = task_sources._account_snapshots(task_sources._mirror(), task_sources._account(CONFIG))
        task_sources._save('mirror.json', saved)
        self.assertEqual(task_sources.listing()['tasks'], [])
        with self.assertRaises(task_sources.TaskError) as missing:
            task_sources.to_backlog('todo:legacy')
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual([row['key'] for row in self.poll(todo('current'))['tasks']], ['todo:current'])

    def test_owned_legacy_snapshot_keeps_links_after_a_different_account_syncs(self):
        self.alert()
        task = todo('legacy', NOW + 3600)
        self.poll(task)
        copy = task_sources.to_backlog('todo:legacy')['id']
        account = task_sources._account(CONFIG)
        saved = task_sources._account_snapshots(task_sources._mirror(), account)
        task_sources._save('mirror.json', {source: dict(snapshot, account=list(account))
                                          for source, snapshot in saved.items()})
        graph_client.save(dict(CONFIG, account='other@example.com', scopes=graph_client.scopes_for(['tasks'])))
        self.poll(todo('other', NOW + 3600))
        self.grant('tasks')
        restored = self.poll(task)['tasks'][0]
        self.assertEqual((restored['backlog'], restored['reminder']), (copy, 'R1'))
        self.assertEqual(task_sources.to_backlog('todo:legacy'), {'id': copy, 'created': False})

    def test_in_flight_client_does_not_use_another_accounts_token(self):
        config, _ = graph_client.load()
        other = dict(config, account='other@example.com', refresh='other-refresh')
        graph_client.save(other)
        open_url = mock.Mock(side_effect=AssertionError('The client must reject the new account before a request.'))
        client = self.real_client(config, open_url=open_url)
        with self.assertRaises(graph_client.GraphError) as changed:
            client.get(LISTS)
        self.assertEqual(changed.exception.status, 401)
        open_url.assert_not_called()

    def test_windows_iana_utc_and_unknown_zones(self):
        items = [todo(f't{index}', NOW + 3600, dueDateTime={'dateTime': '2026-11-19T20:00:00', 'timeZone': zone})
                 for index, zone in enumerate(('India Standard Time', 'Asia/Kolkata', 'UTC', 'Unknown Standard Time'))]
        items.append(todo('date', dueDateTime={'dateTime': '2026-11-20', 'timeZone': 'India Standard Time'}))
        indexed = {row['key']: row for row in self.poll(*items)['tasks']}
        expected = datetime(2026, 11, 19, 20, tzinfo=timezone.utc).timestamp()
        for key in ('todo:t0', 'todo:t1'):
            self.assertEqual(indexed[key]['due'], {'date': '2026-11-19', 'at': expected - 19800})
        for key in ('todo:t2', 'todo:t3'):
            self.assertEqual(indexed[key]['due'], {'date': '2026-11-20', 'at': expected})
        self.assertTrue(indexed['todo:t3']['timeUncertain'])
        self.assertNotIn('timeUncertain', indexed['todo:t2'])
        self.assertEqual(indexed['todo:date']['due'], {'date': '2026-11-20', 'at': None})

    def test_backlog_copy_is_at_most_once_and_keeps_title_literal(self):
        backlog.add('Existing item')
        self.poll(todo('task', title='Call about #B1'), todo('second'))
        self.assertEqual(task_sources.to_backlog('todo:task'), {'id': 'B2', 'created': True})
        self.assertEqual(task_sources.to_backlog('todo:task', project='Other'), {'id': 'B2', 'created': False})
        self.assertEqual(backlog.show('B2').splitlines(),
                         ['- [ ] Call about #B1 #B2', f'  - {task_sources.TODO_HOME}',
                          '  - From Microsoft To Do, list Personal'])
        self.assertEqual(task_sources.to_backlog('todo:second', project='Another project')['id'], 'B3')
        self.assertIn('## Another project', backlog.read()['text'])
        self.poll()
        self.assertEqual(task_sources.to_backlog('todo:task'), {'id': 'B2', 'created': False})

    def test_backlog_copy_recovers_if_mirror_write_fails_after_backlog_save(self):
        self.poll(todo('one'), todo('two'))
        real_save = task_sources._save

        def fail_after_backlog(name, data):
            if task_sources._account_snapshots(data, task_sources._account(CONFIG))['todo']['backlog']:
                raise OSError('mirror save failed')
            real_save(name, data)

        with mock.patch.object(task_sources, '_save', side_effect=fail_after_backlog):
            with self.assertRaises(OSError):
                task_sources.to_backlog('todo:one')
        self.assertEqual(task_sources.to_backlog('todo:one'), {'id': 'B1', 'created': False})
        self.assertEqual(task_sources.to_backlog('todo:two'), {'id': 'B2', 'created': True})
        self.assertEqual(len(backlog.board_items()), 2)

    def test_only_one_poller_can_use_the_store(self):
        with task_sources._lock('.sync'):
            self.assertEqual(task_sources.listing()['sources'][0]['state'], 'syncing')
            task_sources.sync()
            self.client.assert_not_called()
        self.poll()
        self.assertEqual(task_sources.listing()['sources'][0]['state'], 'ok')

    def test_http_dispatch_and_auth_for_each_task_write(self):
        def handler(action='', body=None, allow=True, origin='https://test.example', header='1'):
            data = json.dumps(body or {}).encode()
            class Handler:
                _get_without_share = server.Handler._get_without_share
                _post_without_share = server.Handler._post_without_share
            value = Handler()
            value.path = task_sources.PREFIX + action
            value.deny = mock.Mock(return_value=not allow)
            value.headers = {'Origin': origin, 'Host': 'test.example', 'X-Codex-Mobile': header,
                             'Content-Length': str(len(data))}
            value.respond, value.rfile = mock.Mock(), io.BytesIO(data)
            return value

        anonymous = handler(allow=False)
        server.Handler.do_GET(anonymous)
        anonymous.deny.assert_called_once()
        anonymous.respond.assert_not_called()
        self.poll(todo('task'))
        fetched = handler()
        server.Handler.do_GET(fetched)
        fetched.respond.assert_called_once_with(200, task_sources.listing())
        for action in ('/settings', '/sync', '/backlog'):
            blocked = handler(action, origin='https://elsewhere.example')
            server.Handler.do_POST(blocked)
            self.assertEqual(blocked.respond.call_args.args[0], 403)
            blocked = handler(action, header='')
            server.Handler.do_POST(blocked)
            self.assertEqual(blocked.respond.call_args.args[0], 403)
        self.sync_now.assert_not_called()
        posted = handler('/sync')
        server.Handler.do_POST(posted)
        self.sync_now.assert_called_once()
        posted.respond.assert_called_once_with(200, task_sources.listing())
        for action, data, code in (
            ('/settings', {'source': 'todo', 'alerts': 'yes'}, 400),
            ('/settings', {'source': 'planner', 'enabled': False}, 200),
            ('/backlog', {'key': 'todo:unknown'}, 404),
            ('/backlog', {'key': 'todo:task', 'project': 'x\n## New'}, 400),
            ('/backlog', {'key': 'todo:task'}, 200),
        ):
            with self.subTest(action=action, data=data):
                posted = handler(action, data)
                server.Handler.do_POST(posted)
                self.assertEqual(posted.respond.call_args.args[0], code)
                if code >= 400:
                    self.assertEqual(set(posted.respond.call_args.args[1]), {'error'})
        self.assertEqual(task_sources.listing()['sources'][1]['state'], 'off')
        self.assertEqual((self.folder / 'settings.json').stat().st_mode & 0o777, 0o600)

    def test_cli_shows_counts_without_titles_and_runs_sync(self):
        self.poll(todo('task', title='Do not print this title'))
        self.calls.clear()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(task_sources.main(['status']), 0)
            self.assertEqual(self.calls, [])
            self.assertEqual(task_sources.main(['sync']), 0)
            self.assertTrue(self.calls)
        self.assertIn('Microsoft To Do: ok (1 task)', out.getvalue())
        self.assertNotIn('Do not print this title', out.getvalue())

    def test_tasks_scopes_and_cli_validation(self):
        for kinds in ('tasks', 'mail,tasks', 'tasks, teams', 'teams,mail,tasks'):
            scopes = graph_client.scopes_for(agent_feed_cli._kinds(kinds))
            self.assertIn('Tasks.Read', scopes)
            self.assertEqual('Mail.ReadBasic' in scopes, 'mail' in kinds)
            self.assertEqual('ChannelMessage.Read.All' in scopes, 'teams' in kinds)
        with self.assertRaisesRegex(ValueError, 'mail, teams, and tasks'):
            agent_feed_cli._kinds('mail,invalid')
        self.assertTrue(graph_client.status()['tasks'])

    def test_connect_adds_tasks_without_dropping_existing_grants(self):
        self.grant('mail', 'teams')
        args = SimpleNamespace(tenant=TENANT, client=CLIENT, feeds='tasks', port=0)
        listener = mock.Mock(port=8765)
        graph_answer = {'access_token': 'access-secret', 'refresh_token': 'new-refresh',
                        'scope': ' '.join(graph_client.scopes_for({'mail', 'teams', 'tasks'}))}
        self.client.return_value.get.side_effect = graph_client.GraphError('Account denied', 403)
        with mock.patch.object(graph_client, 'redeem', return_value=graph_answer) as redeem, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(agent_feed_cli.connect(args, listen=lambda _port: listener), 1)
            self.assertEqual(graph_client.load()[0]['refresh'], CONFIG['refresh'])
            self.client.return_value.get.side_effect = None
            self.client.return_value.get.return_value = {'userPrincipalName': CONFIG['account']}
            self.assertEqual(agent_feed_cli.connect(args, listen=lambda _port: listener), 0)
            self.assertEqual(redeem.call_args.args[-1], graph_client.scopes_for({'mail', 'teams', 'tasks'}))
        self.assertEqual([graph_client.status()[kind] for kind in ('mail', 'teams', 'tasks')], [True, True, True])
