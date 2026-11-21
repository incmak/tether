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
import injected_prompts
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


def append_prompt(entry, text='Please revisit this', timestamp='2026-11-21T00:02:00Z'):
    if entry['agent'] == 'claude':
        record = {'type': 'user', 'uuid': entry['thread'], 'message': {'role': 'user', 'content': text}}
    else:
        record = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}}
    with entry['transcript'].open('a') as stream:
        stream.write(json.dumps(dict(record, timestamp=timestamp)) + '\n')


def fail_next_receipt_read(entry):
    read = injected_prompts._read
    target = injected_prompts._file(entry['transcript'])
    failed = False

    def read_once(path):
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise OSError('receipt unavailable')
        return read(path)

    return patch.object(injected_prompts, '_read', side_effect=read_once)


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
        if command[1] == 'display-message' and command[-1] == '#{pane_id}\t#{@tether_role}':
            pane = command[4]
            row = self.rows.get(pane)
            return subprocess.CompletedProcess(command, 0 if row else 1,
                                               stdout=f'{pane}\t{row.get("@tether_role", "")}\n' if row else '')
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

    def mark_for_unreadable_prompt(self, agent):
        self.root.update(agent=agent, registry='verified', transcript=self.folder / f'{agent}.jsonl')
        self.root['transcript'].write_text('')
        self.run.rows['%1'].update({task_tree.DONE: self.root['thread'], task_tree.DONE_AT: '1795219260'})
        self.run.writes.clear()

    def test_auto_close_drops_timer_when_later_prompt_receipt_is_unreadable(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent), patch.object(task_tree, 'done_items', return_value=set()):
                self.mark_for_unreadable_prompt(agent)
                clock = [0]
                closer = task_tree_close.Closer(now=lambda: clock[0])

                def sweep():
                    return closer.observe(self.tree(), self.tree, enabled=lambda: True, lock=self.lock,
                                          activity=lambda _: 1, viewed=lambda _: (False, None), draft=lambda _: False,
                                          changed=Mock(), user_prompt_at=server.task_user_prompt_at)

                self.assertEqual(sweep(), [])
                self.assertIn('%1', closer.timers)
                append_prompt(self.root)
                clock[0] = task_tree.CLOSE_AFTER
                with fail_next_receipt_read(self.root):
                    self.assertEqual(sweep(), [])
                self.assertEqual(closer.timers, {})
                self.assertEqual(sweep(), [])
                self.assertEqual(closer.timers, {})
                self.assertEqual(self.run.writes, [])

    def test_auto_close_waits_while_a_prompt_after_done_has_unknown_provenance(self):
        copied = 'Please revisit this\n\n[Tether id 0123456789abcdef]'
        for agent in ('claude', 'codex'):
            for when, closes in (('2026-11-21T00:02:00Z', False), ('2026-11-21T00:00:30Z', True)):
                with self.subTest(agent=agent, after_done=not closes), \
                        patch.object(task_tree, 'done_items', return_value=set()):
                    self.mark_for_unreadable_prompt(agent)
                    append_prompt(self.root, copied, when)
                    clock = [0]
                    closer = task_tree_close.Closer(now=lambda: clock[0])

                    def sweep():
                        tree = self.tree()
                        task_tree_close.clear_obsolete_marks(tree, self.lock, server.task_user_prompt_at)
                        return closer.observe(tree, self.tree, enabled=lambda: True, lock=self.lock,
                                              activity=lambda _: 1, viewed=lambda _: (False, None),
                                              draft=lambda _: False, changed=Mock(),
                                              user_prompt_at=server.task_user_prompt_at)

                    sweep()
                    # An unknown prompt after Done neither keeps the task Done nor clears its mark.
                    self.assertEqual('%1' in closer.timers, closes)
                    if not closes:
                        clock[0] = task_tree.CLOSE_AFTER
                        self.assertEqual(sweep(), [])
                        self.assertEqual(self.run.writes, [])
                        self.assertEqual(self.run.rows['%1'][task_tree.DONE], self.root['thread'])

    def test_unknown_prompt_before_done_keeps_mark_and_known_prompt_after_done_ends_it(self):
        copied = 'Relay\n\n[Tether id 0123456789abcdef]'
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                self.mark_for_unreadable_prompt(agent)
                append_prompt(self.root, copied, '2026-11-21T00:00:30Z')
                options = self.tree().options['%1']
                self.assertTrue(task_tree.manual_mark(self.root, options, server.task_user_prompt_at))
                append_prompt(self.root, copied, '2026-11-21T00:02:00Z')
                with self.assertRaises(injected_prompts.ReceiptsUnreadable):
                    task_tree.manual_mark(self.root, options, server.task_user_prompt_at)
                append_prompt(self.root, 'A real reply', '2026-11-21T00:03:00Z')
                self.assertFalse(task_tree.manual_mark(self.root, options, server.task_user_prompt_at))

    def test_unknown_prompt_does_not_hide_a_confirmed_reopen(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent), patch.object(task_tree, 'done_items', return_value=set()):
                self.mark_for_unreadable_prompt(agent)
                append_prompt(self.root, 'A real reply', '2026-11-21T00:02:00Z')
                append_prompt(self.root, 'Relay\n\n[Tether id 0123456789abcdef]', '2026-11-21T00:03:00Z')
                with self.assertRaises(injected_prompts.ProvenanceUnknown) as caught:
                    server.task_user_prompt_at(self.root)
                self.assertEqual((caught.exception.at, caught.exception.user_at), (1795219380, 1795219320))
                tree = self.tree()
                self.assertFalse(task_tree.manual_mark(self.root, tree.options['%1'], server.task_user_prompt_at))
                self.assertEqual(task_tree_close.clear_obsolete_marks(tree, self.lock, server.task_user_prompt_at), set())
                self.assertTrue(self.run.writes)  # the obsolete Done mark is cleared

    def test_auto_close_refuses_unreadable_helper_at_final_validation(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent), patch.object(task_tree, 'done_items', return_value={'B446'}), \
                    patch.object(task_tree_api, 'prepare_close', side_effect=helper_tabs.Stale('Stop before capture')) as prepare:
                self.worker.update(agent=agent, registry='verified', transcript=self.folder / f'{agent}.jsonl')
                self.worker['transcript'].write_text('')
                clock = [0]
                closer = task_tree_close.Closer(now=lambda: clock[0])
                pending = False

                def open_once(path, *args, **kwargs):
                    nonlocal pending
                    if pending and path == self.worker['transcript']:
                        pending = False
                        raise OSError('helper transcript unavailable')
                    return open(path, *args, **kwargs)

                def fresh():
                    nonlocal pending
                    pending = True
                    append_prompt(self.worker, 'Relay\n\n[Tether id 0123456789abcdef]')
                    return self.tree()

                def sweep():
                    return closer.observe(self.tree(), fresh, enabled=lambda: True, lock=self.lock,
                                          activity=lambda _: 1, viewed=lambda _: (False, None), draft=lambda _: False,
                                          changed=Mock(), user_prompt_at=server.task_user_prompt_at)

                with patch.object(server, 'open', open_once, create=True):
                    self.assertEqual(sweep(), [])
                    self.assertIn('%1', closer.timers)
                    clock[0] = task_tree.CLOSE_AFTER
                    self.assertEqual(sweep(), [])
                prepare.assert_not_called()
                self.assertEqual(closer.timers, {})
                self.assertEqual(self.run.writes, [])

    def test_clear_obsolete_marks_waits_for_readable_prompt(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                self.mark_for_unreadable_prompt(agent)
                append_prompt(self.root)
                self.run.rows['%1'][task_tree.REOPENED] = self.separate['thread']
                before = self.tree().options['%1']
                with fail_next_receipt_read(self.root), patch.object(task_tree, 'set_marks', wraps=task_tree.set_marks) as write:
                    task_tree_close.clear_obsolete_marks(self.tree(), self.lock, server.task_user_prompt_at)
                write.assert_not_called()
                self.assertEqual(self.tree().options['%1'], before)
                task_tree_close.clear_obsolete_marks(self.tree(), self.lock, server.task_user_prompt_at)
                after = self.tree().options['%1']
                self.assertFalse(after.done or after.done_at or after.reopened)

    def test_task_close_sweep_defers_pane_after_cleanup_read_failure(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                self.mark_for_unreadable_prompt(agent)
                append_prompt(self.root, timestamp='2026-11-21T00:00:00Z')
                clock = [0]
                closer = task_tree_close.Closer(now=lambda: clock[0])
                with patch.multiple(server, TASK_CLOSER=closer, live_task_tree=lambda *_: self.tree(),
                                    task_activity=lambda _: 1, task_view=lambda _: (False, None), SEND_LOCK=self.lock), \
                        patch.object(server.prefs_sync, 'load', return_value={'prefs': {'task_auto_close': True}}), \
                        patch.object(task_tree, 'done_items', return_value=set()):
                    server.task_close_sweep(self.entries)
                    self.assertIn('%1', closer.timers)
                    clock[0] = task_tree.CLOSE_AFTER
                    with fail_next_receipt_read(self.root), patch.object(server, 'prompt_draft', return_value=('', '')), \
                            patch.object(task_tree_api, 'prepare_close', side_effect=helper_tabs.Stale('Stop before capture')) as prepare:
                        server.task_close_sweep(self.entries)
                    prepare.assert_not_called()
                    self.assertEqual(closer.timers, {})
                    self.assertEqual(self.run.rows['%1'][task_tree.DONE], self.root['thread'])
                    server.task_close_sweep(self.entries)
                    self.assertEqual(closer.timers['%1'][0], clock[0])

    def test_sessions_listing_returns_row_without_done_when_receipts_are_unreadable(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                self.mark_for_unreadable_prompt(agent)
                append_prompt(self.root)
                handler = object.__new__(server.Handler)
                handler.command, handler.path, handler.headers = 'GET', '/mobile/api/sessions', {}
                handler.deny = Mock(return_value=False)
                handler.respond = Mock()
                with patch.multiple(server, sessions=lambda: self.entries, LISTING_CACHE={'generation': None},
                                    session_rows=lambda entries: [dict(id=entry['id'], state='Idle') for entry in entries]), \
                        patch.object(server.subprocess, 'run', self.run), \
                        patch.object(task_tree, 'done_items', return_value={'B446'}), fail_next_receipt_read(self.root):
                    handler.do_GET()
                status, body = handler.respond.call_args.args
                self.assertEqual(status, 200)
                row = json.loads(body)['sessions'][0]
                self.assertEqual(row['id'], '%1')
                self.assertFalse(row.get('done') or row.get('done_mark'))


class DoneRuleTests(unittest.TestCase):
    def test_latest_task_user_prompt_propagates_receipt_and_transcript_read_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            for agent in ('claude', 'codex'):
                with self.subTest(agent=agent):
                    root = chat(1, agent=agent, transcript=Path(folder) / f'{agent}.jsonl')
                    append_prompt(root)
                    append_prompt(root, 'Copied delivery\n\n[Tether id 0123456789abcdef]', '2026-11-21T00:03:00Z')
                    with self.subTest(error='receipt'), fail_next_receipt_read(root), \
                            self.assertRaises(injected_prompts.ReceiptsUnreadable):
                        server.task_user_prompt_at(root)
                    for error in (PermissionError('transcript unavailable'), FileNotFoundError('transcript missing')):
                        with self.subTest(error=type(error).__name__), patch.object(server, 'open', side_effect=[error], create=True), \
                                self.assertRaises(injected_prompts.ReceiptsUnreadable):
                            server.task_user_prompt_at(root)
                    # Readable, but the copied delivery's sender is unknown: never skipped as "not the user".
                    with self.assertRaises(injected_prompts.ProvenanceUnknown) as caught:
                        server.task_user_prompt_at(root)
                    self.assertEqual((caught.exception.at, caught.exception.user_at), (1795219380, 1795219320))

    def test_is_done_does_not_fall_back_to_backlog_on_unreadable_manual_mark(self):
        with tempfile.TemporaryDirectory() as folder:
            for agent in ('claude', 'codex'):
                with self.subTest(agent=agent):
                    root = chat(1, agent=agent, registry='verified', pane_name='B446 Task',
                                transcript=Path(folder) / f'{agent}.jsonl')
                    append_prompt(root)
                    mark = helper_tabs.PaneOptions(None, done=root['thread'], done_at='1795219260')
                    with fail_next_receipt_read(root):
                        self.assertFalse(task_tree.is_done(root, {'state': 'Idle'}, [], mark, {'B446'},
                                                           server.task_user_prompt_at))

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
