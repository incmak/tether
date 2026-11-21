"""Task tree rules and HTTP integration with a fake tmux and controlled clocks."""
import unittest_isolated  # noqa: F401
from contextlib import nullcontext
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import agent_tabs
import backlog
import helper_close
import helper_tabs
import paths
import server
import task_tree
import task_tree_api
import task_tree_close
import undo_store
from undo import next_target


def chat(number, **changes):
    return dict({'id': f'%{number}', 'thread': f'{number:08d}-1111-4111-8111-111111111111',
                 'agent': 'codex', 'codex_binding': 'open', 'tmux_session': '$1', 'writable': True,
                 'pane_name': f'Tab {number}'}, **changes)


def owner(entry, **changes):
    return dict({'owner_pane': entry['id'], 'owner_thread': entry['thread'], 'sent_at': 1}, **changes)


class FakeTmux:
    def __init__(self, entries):
        self.rows = {entry['id']: {'session_id': entry['tmux_session']} for entry in entries}
        self.writes = []
        self.on_kill = lambda pane: None

    def __call__(self, command, **_):
        if command[1] == 'list-panes':
            fields = [name[2:-1] for name in command[-1].split('\t')]
            data = ''.join('\t'.join(pane if key == 'pane_id' else opts.get(key, '') for key in fields) + '\n'
                           for pane, opts in self.rows.items())
            return subprocess.CompletedProcess(command, 0, stdout=data)
        if command[1] == 'display-message' and command[-1] == '#{pane_id}\t#{@tether_done}':
            pane = command[4]
            return subprocess.CompletedProcess(command, 0, stdout=f'{pane}\t{self.rows[pane].get(task_tree.DONE, "")}\n')
        if command[1] == 'display-message' and command[-1] == '#{pane_id}\t#{@tether_helper}':
            pane = command[4]
            row = self.rows.get(pane)
            return subprocess.CompletedProcess(command, 0 if row else 1,
                                               stdout=f'{pane}\t{row.get(helper_tabs.OPTION, "")}\n' if row else '')
        if command[1] == 'display-message' and command[-1] == helper_close.CLOSE_FORMAT:
            pane = command[4]
            row = self.rows.get(pane)
            identity = f'10:20\t{pane}\t{row["session_id"]}\t{row.get("pane_pid", "")}\n' if row else ''
            return subprocess.CompletedProcess(command, 0 if row else 1, stdout=identity)
        if command[1:] == ['display-message', '-p', '#{pid}:#{start_time}']:
            return subprocess.CompletedProcess(command, 0, stdout='10:20\n')
        self.writes.append(command[1:])
        if command[1] == 'set-option':
            for part in ' '.join(command[1:]).split(' ; '):
                _, _, _, pane, key, rest = part.split(' ', 5)
                if key == '-u':
                    self.rows[pane].pop(rest, None)
                else:
                    self.rows[pane][key] = rest
            return subprocess.CompletedProcess(command, 0)
        if command[1] == 'kill-pane':
            self.rows.pop(command[-1])
            self.on_kill(command[-1])
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(command)

    def set_record(self, pane, record, role='', grouped=''):
        self.rows[pane].update({'@tether_helper': json.dumps(record), '@group_by': grouped, '@tether_role': role})


class ClassificationTests(unittest.TestCase):
    def test_start_classification_checks_title_and_first_line_only(self):
        opener = chat(1, pane_name='B446 task')
        owned = owner(opener, kind='helper')
        legacy = owner(opener)
        for record, role, name, message, expected in (
                (owned, '', 'B999 work', 'B446', ('separate', None)),
                (owned, '', 'Worker', 'B123\nB446', ('separate', None)),
                (owned, '', 'B446', 'B446\nB123', ('child', '%1')),
                (owned, '', 'b123', 'Work\nB123', ('child', '%1')),
                (owner(opener, kind='own'), '', 'B446', '', ('separate', None)),
                (owned, 'reviewer', 'B999', '', ('child', '%1')),
                (owned, 'advisor', '', 'B999', ('child', '%1')),
                (owned, 'second-advisor', '', 'B999', ('child', '%1')),
                (legacy, '', 'Worker', '', ('separate', None)),
                (legacy, '', 'B446', '', ('child', '%1')),
                (dict(owned, moved_by='user', home_pane='%2'), '', 'B999', '', ('child', '%2')),
                (dict(owned, group_by='user'), 'reviewer', 'B446', '', ('separate', None)),
                (dict(owned, kind='own', moved_by='user'), 'reviewer', 'B446', '', ('separate', None))):
            with self.subTest(record=record, role=role, name=name, message=message):
                self.assertEqual(task_tree.classify(record, role, name, message, opener), expected)
        self.assertEqual(task_tree.classify(owned, '', 'B20', '', chat(3)), ('separate', None))

    def test_agent_start_keeps_opener_authority_but_drops_placement_for_handoff(self):
        opener = chat(1, pane_name='B446 Root')
        run = Mock()
        separate = agent_tabs._owner(opener, opener, [opener], run, name='B447 Handoff')
        self.assertEqual((separate['kind'], separate['owner_pane'], separate['opener_name']),
                         ('own', '%1', 'B446 Root'))
        self.assertIsNone(separate['home_pane'])
        run.assert_not_called()

    def test_listing_flattens_grandchildren_and_keeps_legacy_workers_apart(self):
        root, legacy, worker, review, placed = chat(1), chat(2), chat(3), chat(4), chat(5)
        entries = [root, legacy, worker, review, placed]
        run = FakeTmux(entries)
        run.set_record('%2', owner(root))
        run.set_record('%3', owner(root, kind='helper'))
        run.set_record('%4', owner(worker), role='reviewer')
        run.set_record('%5', owner(root, kind='helper'), role='advisor', grouped='user')
        fields = helper_tabs.listing_fields(run, entries)
        self.assertEqual(fields['%2']['from'], {'pane': '%1', 'name': 'Tab 1', 'live': True})
        self.assertEqual(fields['%2']['owner'], '%1')
        self.assertNotIn('task', fields['%2'])
        self.assertEqual((fields['%4']['task'], fields['%4']['task_role'], fields['%4']['task_depth']),
                         ('%1', "Worker's review", 2))
        self.assertNotIn('task', fields['%5'])
        self.assertNotIn('helper', fields['%5'])


class Routes(unittest.TestCase):
    def setUp(self):
        self.entries = [chat(1, pane_name='B446 Task'), chat(2), chat(3), chat(4)]
        self.root, self.worker, self.review, self.separate = self.entries
        self.run = FakeTmux(self.entries)
        self.run.set_record('%2', owner(self.root, kind='helper'))
        self.run.set_record('%3', owner(self.worker), role='reviewer')
        self.states = {}
        self.asks = {}
        self.escalated = set()
        self.lock = threading.Lock()
        self.audit = self.enterContext(patch.object(task_tree_api.audit, 'record'))
        self.undo = self.enterContext(patch.object(task_tree_api.undo, 'record_task_tree_move'))
        self.enterContext(patch.object(task_tree_api.undo, 'note_released'))
        self.enterContext(patch.object(helper_close.auto_resume, 'server_id', return_value=('up', '10:20')))
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.trace = self.folder / 'chat.jsonl'
        self.trace.write_text('{}\n')
        self.enterContext(patch.object(task_tree_api.undo, 'capture', side_effect=self.capture))
        self.enterContext(patch.object(helper_close.auto_resume, 'forget_pane'))
        self.enterContext(patch.object(helper_close.groups, 'closing', return_value=nullcontext()))
        self.enterContext(patch.object(helper_close.review_links, 'close'))

    def capture(self, pane, run, entry):
        return dict(pane=pane, thread=entry['thread'], agent=entry['agent'], folder=str(self.folder),
                    transcript=str(self.trace), server='10:20', name=entry['pane_name'], session='Fixture')

    def tree(self):
        return task_tree_api.Tree(self.entries, self.run, lambda item: {'state': self.states.get(item['id'], 'Idle')},
                                  lambda pane, _: self.asks.get(pane), lambda _: self.escalated,
                                  fresh_entries=lambda: self.entries)

    def request(self, action, payload, headers=None, deny=False):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = 'POST', task_tree_api.PREFIX + action
        body = json.dumps(payload).encode()
        handler.rfile = io.BytesIO(body)
        handler.headers = {'Host': 'local', 'Origin': 'http://local', 'Content-Length': str(len(body)),
                           'X-Codex-Mobile': '1', **(headers or {})}
        handler.deny = Mock(return_value=deny)
        handler.respond = Mock()
        with patch.object(server, 'live_task_tree', side_effect=self.tree):
            handler.do_POST()
        return handler.respond.call_args.args if handler.respond.called else None

    def move(self, source, target):
        return self.request('move', dict(pane=source['id'], thread=source['thread'],
                                        into=target['id'] if target else None,
                                        into_thread=target['thread'] if target else None))

    def test_writes_require_cookie_origin_and_header(self):
        payload = {'pane': '%1', 'thread': self.root['thread'], 'done': True}
        self.assertIsNone(self.request('done', payload, deny=True))
        for headers in ({'Origin': 'https://elsewhere'}, {'X-Codex-Mobile': ''}):
            self.assertEqual(self.request('done', payload, headers=headers)[0], 403)
        self.assertEqual(self.run.writes, [])

    def test_move_in_out_and_changed_owner_cleanup(self):
        self.assertEqual(self.move(self.worker, None), (200, {'ok': True}))
        self.assertEqual(self.tree().options['%2'].record['kind'], 'own')
        self.assertEqual(self.move(self.worker, self.separate)[0], 400)  # it still has a review
        self.assertEqual(self.move(self.review, None)[0], 200)
        self.assertEqual(self.move(self.worker, self.separate)[0], 200)
        record = self.tree().options['%2'].record
        self.assertEqual((record['owner_pane'], record['home_pane'], record['moved_by']), ('%1', '%4', 'user'))
        self.assertEqual(self.undo.call_count, 3)
        closer = helper_close.Closer()
        before = {'%2': (record, ''), '%4': (None, '')}
        entries = {'%2': self.worker, '%4': self.separate}
        self.assertIsNone(closer._reason('%2', record, '', entries, before, {}, {'%2': 70}))
        self.assertEqual(closer._reason('%2', record, '', entries, {'%2': (record, '')}, {}, {'%2': 70})[0],
                         'owner-gone')

    def test_move_refuses_stale_target_or_source_and_wrong_placement(self):
        body = dict(pane='%4', thread=self.separate['thread'], into='%1', into_thread=self.root['thread'])
        for changes in (dict(thread=self.worker['thread']), dict(into_thread=self.worker['thread'])):
            self.assertEqual(self.request('move', dict(body, **changes))[0], 409)
        self.assertEqual(self.move(self.separate, self.worker)[0], 400)
        self.assertEqual(self.move(self.separate, self.separate)[0], 400)
        self.root['tmux_session'] = self.run.rows['%1']['session_id'] = '$2'
        self.assertEqual(self.move(self.separate, self.root)[0], 400)
        self.root['agent'] = 'shell'
        self.assertEqual(self.move(self.separate, self.root)[0], 400)
        self.assertEqual(self.run.writes, [])

    def test_move_refuses_a_coordinator_in_every_mode(self):
        for mode in ('all', 'needs', 'off'):
            with self.subTest(mode=mode):
                self.separate['coordinator'] = mode
                result = self.move(self.separate, self.root)
                self.assertEqual(result[0], 400)
                self.assertIn('coordinator', result[1]['error'])
        self.assertEqual(self.run.writes, [])
        del self.separate['coordinator']
        self.assertEqual(self.move(self.separate, self.root)[0], 200)

    def test_existing_loop_cannot_take_another_child(self):
        self.run.set_record('%1', owner(self.worker, kind='helper'))
        self.assertEqual(self.move(self.separate, self.root)[0], 400)
        self.assertEqual(self.run.writes, [])

    def test_mark_reopen_a_new_conversation_and_user_message(self):
        body = {'pane': '%1', 'thread': self.root['thread']}
        self.assertEqual(self.request('done', dict(body, done=False)), (200, {'ok': True}))
        root, idle = self.root, {'state': 'Idle'}
        status = lambda: self.tree().options['%1']
        self.assertFalse(task_tree.is_done(root, idle, [], status(), {'B446'}))
        self.assertEqual(self.request('done', dict(body, done=True)), (200, {'ok': True}))
        self.assertTrue(task_tree.is_done(root, idle, [], status(), set()))
        self.assertFalse(task_tree.is_done(root, idle, [], status(), {'B446'}, lambda _: float(status().done_at) + 1))
        with patch.object(server, 'terminal_submit'), patch.object(server.subprocess, 'run', self.run), \
                patch.dict(server.TASK_USER_AT, clear=True), patch.dict(server.TASK_SEND_AT, clear=True):
            server.submit_locked('Revisit this', self.root, user=True)
        self.assertEqual(status().done, '')
        self.assertFalse(task_tree.is_done(root, idle, [], status(), {'B446'}))
        root['thread'] = chat(99)['thread']
        self.assertTrue(task_tree.is_done(root, idle, [], status(), {'B446'}))
        self.assertEqual(self.request('done', dict(body, done=True))[0], 409)
        task_tree_close.clear_obsolete_marks(self.tree(), self.lock, lambda _: 0)
        self.assertEqual(status().reopened, '')

    def close_request(self, also=()):
        plan = task_tree_api.close_plan('%1', self.tree())
        return dict(pane='%1', thread=self.root['thread'], plan=plan.get('plan'),
                    close_idle=[child['pane'] for child in plan['idle'] if child.get('undo', True)], close_also=list(also))

    def test_close_refuses_changes_to_the_shown_set(self):
        self.states['%3'] = 'Working'
        data = self.close_request()
        for change in ('went idle', 'joined'):
            with self.subTest(change=change):
                if change == 'went idle':
                    self.states['%3'] = 'Idle'
                else:
                    self.states['%3'] = 'Working'
                    late = chat(9)
                    self.entries.append(late)
                    self.run.rows['%9'] = {'session_id': '$1'}
                    self.run.set_record('%9', owner(self.root, kind='helper'))
                status, result = self.request('close', data)
                self.assertEqual((status, result), (409, {'error': 'The helpers changed. Review the list again.'}))
                self.assertEqual(self.run.writes, [])

    def test_close_plan_and_close_detach_before_kill(self):
        self.states['%3'] = 'Working'
        self.asks['%3'] = {'kind': 'reply'}
        plan = task_tree_api.close_plan('%1', self.tree())
        self.assertEqual(plan['task'], {'pane': '%1', 'name': 'B446 Task', 'state': 'idle', 'undo': True})
        self.assertEqual(plan['idle'][0]['pane'], '%2')
        self.assertEqual((plan['working'][0]['role'], plan['working'][0]['state']), ("Worker's review", 'needs'))
        with patch.object(server, 'SEND_LOCK', self.lock):
            result, body = self.request('close', self.close_request())
        self.assertEqual((result, body), (200, {'ok': True, 'closed': ['%2', '%1'], 'kept': ['%3']}))
        self.assertEqual([write[0] for write in self.run.writes], ['set-option', 'kill-pane', 'kill-pane'])
        record = self.tree().options['%3'].record
        self.assertEqual((record['kind'], record['moved_by']), ('own', 'user'))
        self.assertNotIn('home_pane', record)
        self.assertIsNone(helper_close.Closer()._reason('%3', record, 'reviewer', {}, {}, {}, {'%3': 70}))

    def test_close_checks_all_selected_threads_before_mutation(self):
        self.states['%3'] = 'Working'
        data = self.close_request(('%3',))
        thread = self.review['thread']
        self.review['thread'] = self.worker['thread']
        self.assertEqual(self.request('close', data)[0], 409)
        self.assertEqual(self.run.writes, [])
        self.review['thread'] = thread
        self.assertEqual(self.request('close', data)[1]['closed'], ['%3', '%2', '%1'])

    def test_close_stops_if_an_implicit_idle_helper_starts_work(self):
        def start_during_capture(pane, run, entry):
            self.states['%2'] = 'Working'
            return self.capture(pane, run, entry)
        task_tree_api.undo.capture.side_effect = start_during_capture
        data = self.close_request()
        self.assertEqual(self.request('close', data)[0], 409)
        self.assertEqual(self.run.writes, [])

    def check_after_checkpoint(self, pane, change, closed):
        checkpoint = undo_store.checkpoint

        def change_after_checkpoint(identifier, label, actions, **kwargs):
            saved = checkpoint(identifier, label, actions, **kwargs)
            if actions and actions[0]['kind'] == 'task-tree.close':
                member = actions[0]['closed'][-1]
                if member.get('pending') and member['tab']['pane'] == pane:
                    change()
            return saved

        with patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'), \
                patch.object(undo_store, 'checkpoint', side_effect=change_after_checkpoint):
            earlier = undo_store.record('prefs.order', 'Earlier step', sub=None, old=['a'], new=['b'])
            undo_store.edit(lambda found: found[0].update(state='undone', redo=True))
            before = undo_store.entries()
            status, body = self.request('close', self.close_request())
            old, *steps = undo_store.entries()
            if closed:
                self.assertIsNone(next_target('redo'))
            else:
                self.assertEqual([old], before)
                self.assertEqual(next_target('redo'), earlier['id'])
        self.assertEqual(status, 409)
        self.assertEqual(body['error'], task_tree_api.CLOSE_CHANGED)
        self.assertEqual(body['closed'], closed)
        self.assertIn(pane, self.run.rows)
        self.assertIn('%1', self.run.rows)
        if closed:
            step, = steps
            self.assertEqual(body['undo'], step['id'])
            self.assertEqual([item['tab']['pane'] for item in step['actions'][0]['closed']], ['%3'])
        else:
            self.assertEqual(steps, [])

    def test_close_rechecks_state_after_the_undo_checkpoint(self):
        self.check_after_checkpoint('%3', lambda: self.states.update({'%3': 'Working'}), [])

    def test_close_refuses_a_reused_pane_id_after_the_first_kill(self):
        def reuse_id():
            self.entries[1] = dict(self.worker, pane_pid=123, pane_start='new')

        self.check_after_checkpoint('%2', reuse_id, ['Tab 3'])

    def test_close_asks_for_explicit_selection_of_a_child_without_undo(self):
        self.review['agent'] = 'antigravity'
        plan = task_tree_api.close_plan('%1', self.tree())
        self.assertIs(plan['task']['undo'], True)
        self.assertIs(next(item for item in plan['idle'] if item['pane'] == '%3')['undo'], False)
        body = self.close_request()
        self.assertEqual(self.request('close', dict(body, close_idle=['%2', '%3']))[0], 409)
        self.assertEqual(self.run.writes, [])
        with patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'):
            status, result = self.request('close', dict(body, close_idle=['%2'], close_also=['%3']))
            step, = undo_store.entries()
        self.assertEqual(status, 200)
        self.assertEqual(result['closed_without_undo'], ['Tab 3'])
        self.assertEqual(result['undo'], step['id'])
        self.assertEqual([item['tab']['pane'] for item in step['actions'][0]['closed']], ['%2', '%1'])

    def test_close_without_undo_keeps_unselected_child(self):
        self.review['agent'] = 'antigravity'
        request = dict(self.close_request(), close_idle=['%2'])
        status, result = self.request('close', request)
        self.assertEqual(status, 200)
        self.assertEqual(result['kept'], ['%3'])
        self.assertEqual(self.tree().options['%3'].record['kind'], 'own')

    def test_close_undo_capability_is_bound_to_the_plan(self):
        data = self.close_request()
        self.trace.unlink()
        self.assertEqual(self.request('close', data)[0], 409)
        self.assertEqual(self.run.writes, [])

    def test_antigravity_root_closes_without_undo_and_restores_eligible_children(self):
        self.root['agent'] = 'antigravity'
        self.assertIs(task_tree_api.close_plan('%1', self.tree())['task']['undo'], False)
        with patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'):
            status, result = self.request('close', self.close_request())
            step, = undo_store.entries()
        self.assertEqual(status, 200)
        self.assertEqual(result['closed_without_undo'], ['B446 Task'])
        self.assertEqual(result['undo'], step['id'])
        members = step['actions'][0]['closed']
        self.assertEqual([item['tab']['pane'] for item in members], ['%3', '%2'])
        self.assertEqual(members[1]['record']['kind'], 'own')

    def test_auto_close_keeps_task_if_any_tab_has_no_undo(self):
        clock = [0]
        closer = task_tree_close.Closer(now=lambda: clock[0])

        def sweep():
            return closer.observe(self.tree(), self.tree, enabled=lambda: True, lock=self.lock,
                                  activity=lambda _: 1, viewed=lambda _: (False, None), draft=lambda _: False,
                                  changed=Mock(), user_prompt_at=lambda _: 0)

        with patch.object(task_tree, 'done_items', return_value={'B446'}):
            self.review['agent'] = 'antigravity'
            self.assertEqual(sweep(), [])
            clock[0] += task_tree.CLOSE_AFTER
            self.assertEqual(sweep(), [])
            self.review['agent'] = 'codex'
            self.assertEqual(sweep(), [])
            self.trace.unlink()  # all agents are eligible, but their Undo is now unavailable
            clock[0] += task_tree.CLOSE_AFTER
            self.assertEqual(sweep(), [])
        self.assertEqual(self.run.writes, [])

    def close_with_first_kill_timeout(self, automatic):
        def lost_acknowledgment(pane):
            steps = undo_store.entries()
            member = steps[0]['actions'][0]['closed'][-1]
            self.assertEqual((member['tab']['pane'], member['pending']), (pane, True))
            raise subprocess.TimeoutExpired(['kill-pane'], 5)

        self.run.on_kill = lost_acknowledgment
        with patch.object(undo_store, '_ENABLED', True), patch.object(undo_store, '_server', return_value='10:20'), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'), \
                patch.object(task_tree, 'done_items', return_value={'B446'}):
            if automatic:
                clock = [0]
                closer = task_tree_close.Closer(now=lambda: clock[0])
                def sweep():
                    return closer.observe(self.tree(), self.tree, enabled=lambda: True, lock=self.lock,
                                          activity=lambda _: 1, viewed=lambda _: (False, None), draft=lambda _: False,
                                          changed=Mock(), user_prompt_at=lambda _: 0)
                self.assertEqual(sweep(), [])
                clock[0] = task_tree.CLOSE_AFTER
                result = sweep()
            else:
                plan = self.close_request()
                status, result = self.request('close', plan)
                self.assertEqual(status, 503)
            steps = undo_store.entries()
        self.assertEqual(len(steps), 1)
        action = steps[0]['actions'][0]
        expected = '%3' if automatic else '%2'
        self.assertEqual([member['tab']['pane'] for member in action['closed']], [expected])
        self.assertNotIn('pending', action['closed'][0])
        if automatic:
            self.assertEqual(result, [expected])
        else:
            self.assertEqual(result['undo'], steps[0]['id'])
            self.assertEqual(result['closed'], ['Tab 2'])
            self.assertEqual([change['pane'] for change in action['kept']], ['%3'])
        self.assertNotIn(expected, self.run.rows)
        self.assertIn('%1', self.run.rows)

    def test_partial_manual_close_journals_the_first_kill_and_detached_helper(self):
        self.states['%3'] = 'Working'
        self.close_with_first_kill_timeout(automatic=False)

    def test_partial_auto_close_journals_the_first_kill(self):
        self.close_with_first_kill_timeout(automatic=True)

    def test_partial_stale_close_reports_only_the_tabs_that_closed(self):
        close = helper_close.close_pane

        def lost_identity(pane, run, **kwargs):
            if pane == '%2':
                raise helper_tabs.Stale('The helper changed.')
            close(pane, run, **kwargs)

        with patch.object(helper_close, 'close_pane', side_effect=lost_identity), \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'):
            status, body = self.request('close', self.close_request())
            step, = undo_store.entries()
        self.assertEqual(status, 409)
        self.assertEqual(body['closed'], ['Tab 3'])
        self.assertEqual(body['undo'], step['id'])
        self.assertEqual([member['tab']['pane'] for member in step['actions'][0]['closed']], ['%3'])
        self.assertEqual(set(self.run.rows), {'%1', '%2', '%4'})

    def test_auto_close_refuses_a_helper_that_begins_work_during_capture(self):
        clock = [0]
        closer = task_tree_close.Closer(now=lambda: clock[0])
        def sweep():
            return closer.observe(self.tree(), self.tree, enabled=lambda: True, lock=self.lock,
                                  activity=lambda _: 1, viewed=lambda _: (False, None), draft=lambda _: False,
                                  changed=Mock(), user_prompt_at=lambda _: 0)
        with patch.object(task_tree, 'done_items', return_value={'B446'}):
            self.assertEqual(sweep(), [])
            def start_during_capture(pane, run, entry):
                self.states['%2'] = 'Working'
                return self.capture(pane, run, entry)
            task_tree_api.undo.capture.side_effect = start_during_capture
            clock[0] = task_tree.CLOSE_AFTER
            self.assertEqual(sweep(), [])
        self.assertEqual(self.run.writes, [])

    def test_auto_close_rechecks_state_after_the_undo_checkpoint(self):
        clock = [0]
        closer = task_tree_close.Closer(now=lambda: clock[0])

        def sweep():
            return closer.observe(self.tree(), self.tree, enabled=lambda: True, lock=self.lock,
                                  activity=lambda _: 1, viewed=lambda _: (False, None), draft=lambda _: False,
                                  changed=Mock(), user_prompt_at=lambda _: 0)

        checkpoint = undo_store.checkpoint

        def ask_after_checkpoint(identifier, label, actions, **kwargs):
            saved = checkpoint(identifier, label, actions, **kwargs)
            if actions:
                self.asks['%3'] = {'kind': 'reply'}
            return saved

        with patch.object(task_tree, 'done_items', return_value={'B446'}), \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'), \
                patch.object(undo_store, 'checkpoint', side_effect=ask_after_checkpoint):
            self.assertEqual(sweep(), [])
            clock[0] = task_tree.CLOSE_AFTER
            self.assertEqual(sweep(), [])
            self.assertEqual(undo_store.entries(), [])
        self.assertEqual(self.run.writes, [])

    def check_auto_close_activity_after_checkpoint(self, change, target, expected):
        clock = [0]
        closer = task_tree_close.Closer(now=lambda: clock[0])
        views, drafts, activity, user_prompts = {}, set(), {}, {}

        def sweep():
            return closer.observe(self.tree(), self.tree, enabled=lambda: True, lock=self.lock,
                                  activity=lambda entry: activity.get(entry['id'], 1),
                                  viewed=lambda pane: views.get(pane, (False, None)),
                                  draft=lambda entry: entry['id'] in drafts, changed=Mock(),
                                  user_prompt_at=lambda entry: user_prompts.get(entry['id'], 0))

        checkpoint = undo_store.checkpoint

        def change_after_checkpoint(identifier, label, actions, **kwargs):
            saved = checkpoint(identifier, label, actions, **kwargs)
            if actions and actions[0]['closed'][-1].get('pending'):
                if actions[0]['closed'][-1]['tab']['pane'] == target:
                    change(views, drafts, activity, user_prompts)
            return saved

        with patch.object(task_tree, 'done_items', return_value={'B446'}), \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'), \
                patch.object(undo_store, 'checkpoint', side_effect=change_after_checkpoint):
            self.assertEqual(sweep(), [])
            clock[0] = task_tree.CLOSE_AFTER
            self.assertEqual(sweep(), expected)
            steps = undo_store.entries()
            self.assertEqual([member['tab']['pane'] for step in steps
                              for member in step['actions'][0]['closed']], expected)
        self.assertEqual([write[-1] for write in self.run.writes], expected)

    def test_auto_close_rechecks_view_after_first_checkpoint(self):
        self.check_auto_close_activity_after_checkpoint(
            lambda views, *_: views.update({'%3': (True, 1)}), '%3', [])

    def test_auto_close_rechecks_draft_after_first_checkpoint(self):
        self.check_auto_close_activity_after_checkpoint(
            lambda _, drafts, *_rest: drafts.add('%3'), '%3', [])

    def test_auto_close_keeps_undo_for_the_helper_closed_before_a_visit(self):
        self.check_auto_close_activity_after_checkpoint(
            lambda views, *_: views.update({'%2': (False, 1)}), '%2', ['%3'])

    def test_auto_close_rechecks_activity_and_user_prompts(self):
        for field in (2, 3):
            with self.subTest(field=field):
                self.run.writes.clear()
                self.check_auto_close_activity_after_checkpoint(
                    lambda *changes: changes[field].update({'%1': 2}), '%3', [])

    def test_manual_task_close_allows_a_view_opened_after_confirmation(self):
        checkpoint = undo_store.checkpoint

        def begin_view(identifier, label, actions, **kwargs):
            server.WATCHING['%3'] = 1
            return checkpoint(identifier, label, actions, **kwargs)

        with patch.object(server, 'WATCHING', {}), \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=self.folder / 'undo.json'), \
                patch.object(undo_store, 'checkpoint', side_effect=begin_view):
            status, body = self.request('close', self.close_request())
        self.assertEqual(status, 200)
        self.assertEqual(body['closed'], ['%3', '%2', '%1'])

    def test_unseen_starting_child_blocks_close_and_done(self):
        late = chat(9)
        self.run.rows['%9'] = {'session_id': '$1'}
        self.run.set_record('%9', owner(self.worker, kind='helper'))
        tree = self.tree()
        self.assertEqual(task_tree.pending_roots(tree.options, tree.entries, tree.fields), {'%1'})
        data = self.close_request()
        self.assertEqual(self.request('close', data)[0], 409)
        self.assertEqual(self.run.writes, [])
        self.entries.append(late)
        self.assertEqual(self.request('close', data)[0], 409)
        self.assertEqual(self.request('close', self.close_request())[0], 200)

    def test_auto_close_needs_one_continuous_day_with_no_view_or_turn(self):
        clock, enabled = [0], [False]
        activity, views = {'%1': 1, '%2': 1, '%3': 1, '%4': 1}, {}
        closer = task_tree_close.Closer(now=lambda: clock[0])
        with patch.object(task_tree, 'done_items', return_value={'B446'}):
            def sweep(at):
                clock[0] = at
                return closer.observe(self.tree(), self.tree, enabled=lambda: enabled[0], lock=self.lock,
                                      activity=lambda entry: activity.get(entry['id']),
                                      viewed=lambda pane: (False, views.get(pane)),
                                      draft=lambda _: False, changed=Mock(), user_prompt_at=lambda _: 0)

            day = task_tree.CLOSE_AFTER
            self.assertEqual(sweep(0), [])
            self.assertEqual(sweep(day), [])
            enabled[0] = True
            self.assertEqual(sweep(day + 1), [])
            activity['%2'] = 2  # a short turn finished between the last two polls
            self.assertEqual(sweep(2 * day + 1), [])
            views['%3'] = 2 * day + 2  # a brief view ended before the next poll
            self.assertEqual(sweep(3 * day + 1), [])
            self.assertEqual(sweep(4 * day), [])
            self.assertEqual(sweep(4 * day + 1), ['%3', '%2', '%1'])
        self.assertEqual([write[-1] for write in self.run.writes], ['%3', '%2', '%1'])


class DoneRuleTests(unittest.TestCase):
    def test_archived_backlog_ids_and_non_idle_children(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(backlog, 'folder', return_value=Path(folder)):
            (Path(folder) / 'BACKLOG.md').write_text('## tether\n- [x] Done #B446\n- [ ] Open #B18\n')
            (Path(folder) / 'DONE.md').write_text('## tether\n- [X] Archived #B123\n- [x] Old #B18\n')
            ids = task_tree.done_items()
            self.assertEqual(ids, {'B446', 'B123'})
            idle = {'state': 'Idle'}
            for name in ('B446 Task', 'B123 Archived'):
                self.assertTrue(task_tree.is_done(chat(1, pane_name=name), idle, [idle], None, ids))
            for child in ({'state': 'Working'}, {'state': 'Approval'}, {'state': 'Input needed'},
                          {'state': 'Idle', 'ask': {'kind': 'reply'}}, {'state': 'Idle', 'escalated': True}):
                self.assertFalse(task_tree.is_done(chat(1, pane_name='B446'), idle, [child], None, ids))
            for title in ('b446', 'B446 B123', 'B446 B446', 'B18', 'Looks done'):
                self.assertFalse(task_tree.is_done(chat(1, pane_name=title), idle, [], None, ids))
        with patch.object(backlog, 'done_ids', side_effect=OSError('unreadable')):
            root = chat(3, pane_name='B446')
            self.assertEqual(task_tree.done_items(), set())
            self.assertTrue(task_tree.is_done(root, idle, [], helper_tabs.PaneOptions(None, done=root['thread']), set()))

    def test_both_options_persist_across_resume(self):
        self.assertIn(task_tree.DONE, server.auto_resume.OPTIONS)
        self.assertIn(task_tree.REOPENED, server.auto_resume.OPTIONS)

    def test_server_settings_accept_only_a_boolean_auto_close(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server.prefs_sync, 'FILE', Path(folder) / 'prefs.json'):
            self.assertFalse(server.prefs_sync.load()['prefs'].get('task_auto_close', False))
            self.assertTrue(server.prefs_sync.save({'task_auto_close': True})['prefs']['task_auto_close'])
            self.assertTrue(server.prefs_sync.load()['prefs']['task_auto_close'])
            with self.assertRaises(ValueError):
                server.prefs_sync.save({'task_auto_close': 1})

    def test_unfinished_user_prompt_excludes_tether_messages(self):
        root = chat(1)
        def row(message, timestamp):
            return {'type': 'event_msg', 'timestamp': timestamp,
                    'payload': {'type': 'user_message', 'message': message}}
        with patch.object(server, 'notification_records', return_value=[
                row('Mak types here', '2026-11-21T00:00:00Z'),
                row('Agent sends here', '2026-11-21T00:01:00Z')]), patch.dict(
                    server.TASK_AUTOMATED, {('%1', root['thread']): (
                        (helper_tabs.digest('Agent sends here'), 1795219259, 1795219261),)}, clear=True):
            self.assertEqual(server.task_user_prompt_at(root), 1795219200)
            server.notification_records.return_value += [row('Agent sends here', '2026-11-21T00:02:00Z')]
            self.assertEqual(server.task_user_prompt_at(root), 1795219320)
