"""A stack close checks the displayed task placement before any terminal write."""
import unittest_isolated  # noqa: F401
from contextlib import nullcontext
import json
import subprocess
import unittest
from unittest.mock import patch

import groups
import groups_api
from test_groups import Handler
from test_task_tree import FakeTmux, chat, owner


BUGS = 'g00000001'
TREE = 'g00000002'


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
                          **({'panes': [{'pane': item['id'], 'thread': item['thread']} for item in entries]}
                             if route == 'close' else {})})
        groups_api.post(handler, self.run, sessions=lambda: self.entries)
        return handler.status, handler.body

    def test_b446_close_preserves_helpers_listed_in_another_stack(self):
        self.assertEqual(self.post('close', BUGS, self.root)[0], 200)
        self.assertEqual(list(self.run.rows), ['%1', '%82', '%85'])
        self.assertEqual([stack['id'] for stack in self.run.stacks], [TREE])

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

    def test_remove_uses_effective_membership_instead_of_saved_helper_labels(self):
        self.run.rows.pop(self.root['id'])
        self.assertEqual(self.post('remove', BUGS), (200, {'ok': True}))
        self.assertEqual(self.post('remove', TREE)[0], 409)
