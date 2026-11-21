"""A stack close checks the displayed task placement before any terminal write."""
import unittest_isolated  # noqa: F401
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import groups
import groups_api
import undo
import undo_store
from test_groups import Handler as GroupHandler
from test_task_tree import FakeTmux, chat, owner


BUGS = 'g00000001'
TREE = 'g00000002'


class Handler(GroupHandler):
    def respond(self, status, body, headers=None):
        self.response_headers = headers or {}
        super().respond(status, body)


class StackTmux(FakeTmux):
    def __init__(self, entries):
        super().__init__(entries)
        self.stacks = [{'id': BUGS, 'name': 'Tether bugs', 'project': ''},
                       {'id': TREE, 'name': 'Task tree', 'project': ''}]
        for entry in entries:
            self.rows[entry['id']]['@group'] = entry['group']

    def __call__(self, command, **kwargs):
        if command[-1] == '#{session_id}\t#{session_name}\t#{@groups}':
            return subprocess.CompletedProcess(command, 0, stdout='$1\tTether\t' + json.dumps(self.stacks) + '\n')
        if command[1:4] == ['set-option', '-t', '$1'] and command[-2] == '@groups':
            self.stacks = json.loads(command[-1])
            self.writes.append(command[1:])
            return subprocess.CompletedProcess(command, 0)
        return super().__call__(command, **kwargs)


class StackCloseTests(unittest.TestCase):
    def setUp(self):
        self.root = chat(88, pane_name='B401 Sidebar fix', group=BUGS)
        self.other = chat(1, pane_name='B446 Task tree', group=TREE)
        self.worker = chat(82, pane_name='B446 server', group=BUGS)
        self.review = chat(85, pane_name='B446 server review', group=BUGS)
        self.entries = [self.root, self.other, self.worker, self.review]
        self.run = StackTmux(self.entries)
        self.run.set_record(self.worker['id'], owner(self.other, kind='helper'))
        self.run.set_record(self.review['id'], owner(self.worker), role='reviewer')
        self.enterContext(patch.object(groups, 'starting', return_value=False))
        self.enterContext(patch.object(groups, 'closing', return_value=nullcontext()))
        self.enterContext(patch('helper_close.auto_resume.forget_pane'))
        self.enterContext(patch('helper_close.review_links.close'))
        self.enterContext(patch('audit.record'))

    def post(self, route, group, *entries):
        handler = Handler(groups_api.PREFIX + route, {'session': '$1', 'group': group,
                          **({'panes': [{'pane': item['id'], 'thread': item['thread'],
                                         'bucket': item.get('bucket', 'idle')} for item in entries]}
                             if route == 'close' else {})})
        groups_api.post(handler, self.run, sessions=lambda: self.entries,
                        status=lambda entry: {'state': entry.get('state', 'Idle')})
        return handler.status, handler.body

    def check_first_kill_error(self, outcome):
        first = self.review['id']
        attempt, staged = [], []

        def failed_kill(command, **kwargs):
            if command[1] == 'list-panes' and outcome == 'unknown' and attempt:
                raise subprocess.TimeoutExpired(command, 5)
            if command[1] != 'kill-pane':
                return self.run(command, **kwargs)
            attempt.append(command[-1])
            staged.extend(undo_store.entries())
            if outcome == 'open':
                raise subprocess.CalledProcessError(1, command)
            self.run(command, **kwargs)
            raise subprocess.TimeoutExpired(command, 5)

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=Path(folder) / 'history.json'):
            saved = dict(pane=first, name=self.review['pane_name'], session='Tether', session_id='$1',
                         server='10:20', agent='codex', thread=self.review['thread'], folder=folder,
                         transcript=str(Path(folder) / 'chat.jsonl'), options={}, session_options={})
            Path(saved['transcript']).touch()

            handler = Handler(groups_api.PREFIX + 'close', {'session': '$1', 'group': TREE,
                              'panes': [{'pane': item['id'], 'thread': item['thread'], 'bucket': 'idle'}
                                        for item in (self.other, self.worker, self.review)]})
            with patch('undo.capture_safe', return_value=saved):
                groups_api.post(handler, failed_kill, sessions=lambda: self.entries,
                                status=lambda _: {'state': 'Idle'})
            self.assertEqual(handler.status, 503)
            self.assertEqual(len(staged), 1, 'Undo must be on disk before tmux is called')
            self.assertTrue(staged[0]['actions'][0]['pending'])
            self.assertEqual(handler.body['closed'], [self.review['pane_name']] if outcome == 'closed' else [])
            self.assertEqual(attempt, [first])
            self.assertIn(self.worker['id'], self.run.rows)
            self.assertIn(self.other['id'], self.run.rows)
            self.assertIn(TREE, [stack['id'] for stack in self.run.stacks])
            if outcome == 'open':
                self.assertIn(first, self.run.rows)
                self.assertEqual(undo_store.entries(), [])
                self.assertNotIn('undo', handler.body)
                return
            step, = undo_store.entries()
            self.assertEqual(handler.body['undo'], step['id'])
            self.assertEqual(step['actions'][0]['kind'], 'tab.close')
            self.assertEqual(step['actions'][0].get('pending', False), outcome == 'unknown')
            if outcome == 'unknown':
                self.assertEqual(handler.body['unknown'], [self.review['pane_name']])
            # Apply the real Undo step, with the terminal reopen itself mocked.
            env = undo.Env(self.run, lambda: [entry for entry in self.entries if entry['id'] in self.run.rows])
            with patch.object(undo, '_target_session', return_value=None), \
                    patch.object(undo, '_restack_released'), \
                    patch.object(undo, '_reopen', return_value='%100') as reopen:
                preview = undo.run('undo', step['id'], env)
                self.assertEqual(undo.run('undo', step['id'], env, confirmed=preview['token'])['status'], 'done')
                self.assertEqual(reopen.call_args.args[0]['thread'], self.review['thread'])

    def test_close_timeout_after_kill_keeps_undo_and_stops_the_stack(self):
        self.check_first_kill_error('closed')

    def test_close_error_before_kill_discards_undo_and_stops_the_stack(self):
        self.check_first_kill_error('open')

    def test_close_timeout_with_unreadable_tmux_keeps_pending_undo(self):
        self.check_first_kill_error('unknown')

    def test_stale_stack_close_after_checkpoint_preserves_redo(self):
        checkpoint = undo_store.checkpoint
        saved = {'pane': self.root['id'], 'name': self.root['pane_name'], 'session': 'Tether'}

        def stage(identifier, label, actions, **kwargs):
            step = checkpoint(identifier, label, actions, **kwargs)
            if actions:
                self.root['state'] = 'Working'
            return step

        with (tempfile.TemporaryDirectory() as folder, patch.object(undo_store, '_ENABLED', True),
              patch.object(undo_store, '_path', return_value=Path(folder) / 'history.json'),
              patch.object(undo_store, 'checkpoint', side_effect=stage),
              patch('undo.capture_safe', return_value=saved), undo_store.acting('user')):
            earlier = undo_store.record('prefs.order', 'Earlier step', sub=None, old=['a'], new=['b'])
            undo_store.edit(lambda found: found[0].update(state='undone', redo=True))
            before = undo_store.entries()
            self.assertEqual(self.post('close', BUGS, self.root)[0], 409)
            self.assertEqual(undo_store.entries(), before)
            self.assertEqual(undo.next_target('redo'), earlier['id'])
            self.assertEqual(self.run.writes, [])

    def test_b446_close_preserves_helpers_listed_in_another_stack(self):
        self.assertEqual(self.post('close', BUGS, self.root)[0], 200)
        self.assertEqual(list(self.run.rows), ['%1', '%82', '%85'])
        self.assertEqual([stack['id'] for stack in self.run.stacks], [TREE])

    def test_stack_can_close_a_shell_without_agent_status(self):
        self.root.update(agent='shell', thread=None, state='Unavailable')
        self.assertEqual(self.post('close', BUGS, self.root)[0], 200)
        self.assertNotIn(self.root['id'], self.run.rows)

    def test_stale_members_or_conversations_refuse_before_any_write(self):
        for entries in ([self.other, self.worker], [self.other, self.worker, dict(self.review, thread=None)]):
            with self.subTest(entries=entries):
                self.assertEqual(self.post('close', TREE, *entries)[0], 409)
                self.assertEqual(self.run.writes, [])

    def test_a_helper_starting_in_another_saved_stack_blocks_the_task_stack_close(self):
        late = chat(89, group=BUGS)
        self.run.rows[late['id']] = {'session_id': '$1', '@group': BUGS}
        self.run.set_record(late['id'], owner(self.worker, kind='helper'))
        self.assertEqual(self.post('close', TREE, self.other, self.worker, self.review)[0], 409)
        self.assertEqual(self.run.writes, [])

    def test_close_includes_helpers_labelled_in_another_stack_and_closes_them_first(self):
        self.assertEqual(self.post('close', TREE, self.other, self.worker, self.review)[0], 200)
        self.assertEqual(list(self.run.rows), ['%88'])
        self.assertEqual([call[-1] for call in self.run.writes if call[0] == 'kill-pane'], ['%85', '%82', '%1'])

    def test_close_rechecks_the_next_conversation_after_each_kill(self):
        def first_kill(pane):
            if pane == self.review['id']:
                self.entries[2] = dict(self.worker, thread=self.root['thread'])

        self.run.on_kill = first_kill
        saved = {'pane': '%85', 'name': 'B446 server review', 'session': 'Tether'}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(undo_store, '_ENABLED', True), \
                patch.object(undo_store, '_path', return_value=Path(folder) / 'history.json'), \
                patch('undo.capture_safe', return_value=saved), patch('undo.note_released'):
            status, body = self.post('close', TREE, self.other, self.worker, self.review)
            step, = undo_store.entries()[:1]
        self.assertEqual(status, 409)
        self.assertEqual(body, {'error': 'The helpers changed. Review the list again.',
                                'closed': ['B446 server review'], 'undo': step['id']})
        self.assertEqual([call[-1] for call in self.run.writes if call[0] == 'kill-pane'], ['%85'])
        self.assertIn(TREE, [stack['id'] for stack in self.run.stacks])

    def test_close_rechecks_a_working_pane_that_became_idle_during_undo_capture(self):
        self.root['state'] = 'Working'

        def finish_during_capture(*_):
            self.root['state'] = 'Idle'

        with patch('undo.capture_safe', side_effect=finish_during_capture):
            status, body = self.post('close', BUGS, dict(self.root, bucket='working'))
        self.assertEqual((status, body), (409, {'error': 'The helpers changed. Review the list again.', 'closed': []}))
        self.assertEqual(self.run.writes, [])

    def test_remove_uses_effective_membership_instead_of_saved_helper_labels(self):
        self.run.rows.pop(self.root['id'])
        self.assertEqual(self.post('remove', BUGS), (200, {'ok': True}))
        self.assertEqual(self.post('remove', TREE)[0], 409)
