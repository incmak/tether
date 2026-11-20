"""Task-tree undo uses the history store and a mock tmux runner."""
import unittest_isolated  # noqa: F401
import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import auto_resume
import helper_tabs
import paths
import prefs_sync
import undo
import undo_store


def thread(number):
    return f'00000000-0000-0000-0000-{number:012d}'


def helper(owner, owner_thread, *, root=None, root_thread=None, moved=False):
    record = {'kind': 'helper', 'owner_pane': owner, 'owner_thread': owner_thread,
              'home_pane': owner, 'home_thread': owner_thread,
              'root_pane': root or owner, 'root_thread': root_thread or owner_thread, 'sent_at': 0}
    if moved:
        record['moved_by'] = 'user'
    return record


class TaskTreeUndoTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(temporary)
        self.folder = temporary
        self.enterContext(patch.object(paths, 'CACHE', root / 'cache'))
        self.enterContext(patch.object(paths, 'TETHER', root / 'tether'))
        self.enterContext(patch.object(prefs_sync, 'FILE', root / 'prefs.json'))
        self.enterContext(patch.object(undo_store, '_server', return_value='1:2'))
        self.enterContext(patch.object(undo_store, '_ENABLED', True))
        self.entries = {}
        self.records = {}
        self.server_id = '1:2'
        self.runner = Mock(side_effect=self.tmux)
        self.fresh = Mock(side_effect=lambda: list(self.entries.values()))
        self.env = undo.Env(run=self.runner, entries=lambda: self.entries.values(), fresh=self.fresh)

    def entry(self, pane, conversation, name='', session='$1'):
        return {'id': pane, 'thread': thread(conversation), 'tmux_session': session,
                'pane_name': name or f'Tab {pane}', 'agent': 'codex', 'codex_binding': 'open'}

    def put(self, entry, record=None):
        self.entries[entry['id']] = entry
        self.records[entry['id']] = record

    def tmux(self, command, **_):
        if command[1:4] == ['display-message', '-p', '#{pid}:#{start_time}']:
            if self.server_id is None:
                return CompletedProcess(command, 1, stdout='', stderr='no server running')
            return CompletedProcess(command, 0, stdout=f'{self.server_id}\n')
        if command[1:3] == ['list-panes', '-a']:
            if self.server_id is None:
                return CompletedProcess(command, 1)
            lines = ['\t'.join((pane, '', '', json.dumps(self.records[pane]) if self.records[pane] else '',
                                entry['tmux_session'], '', '', ''))
                     for pane, entry in self.entries.items()]
            return CompletedProcess(command, 0, stdout='\n'.join(lines))
        if command[1:4] == ['set-option', '-p', '-t']:
            pane = command[4]
            if command[5:] == ['-u', helper_tabs.OPTION]:
                self.records[pane] = None
            else:
                self.assertEqual(command[5], helper_tabs.OPTION)
                self.records[pane] = json.loads(command[6])
            return CompletedProcess(command, 0)
        self.fail(f'Unexpected tmux command {command!r}')

    def test_move_undo_clears_an_option_that_did_not_exist_and_redo_restores_it(self):
        self.put(self.entry('%1', 1))
        child = self.entry('%2', 2)
        new = helper('%1', thread(1), moved=True)
        self.put(child, new)
        with undo_store.acting('user'), undo_store.step():
            undo.record_task_tree_move(child, None, new)
        saved, = undo_store.entries()
        self.assertEqual(saved['who'], {'by': 'user'})
        self.assertEqual(len(saved['actions']), 1)
        self.assertEqual(undo.run('undo', saved['id'], self.env)['state'], 'undone')
        self.assertIsNone(self.records['%2'])
        self.assertEqual(undo.run('redo', saved['id'], self.env)['state'], 'done')
        self.assertEqual(self.records['%2'], new)

    def test_move_refuses_a_changed_conversation_or_placement(self):
        self.put(self.entry('%1', 1))
        child = self.entry('%2', 2)
        new = helper('%1', thread(1), moved=True)
        self.put(child, new)
        undo.record_task_tree_move(child, None, new)
        self.env.entries = lambda: [child]  # the list the page saw is stale
        self.put(self.entry('%2', 3), new)
        with self.assertRaisesRegex(undo.Refused, 'changed conversations'):
            undo.run('undo', None, self.env)
        self.assertTrue(self.fresh.called)
        self.put(child, dict(new, sent_at=1))
        with self.assertRaisesRegex(undo.Refused, 'changed task placement'):
            undo.run('undo', None, self.env)
        self.assertEqual(self.records['%2']['sent_at'], 1)
        self.assertFalse(any(call.args[0][1] == 'set-option' for call in self.runner.call_args_list))

    def test_undo_move_refuses_a_stale_home_or_a_new_loop(self):
        child = self.entry('%2', 2)
        old = helper('%1', thread(1), moved=True)
        detached = {'kind': 'own', 'owner_pane': '%1', 'owner_thread': thread(1), 'sent_at': 0}
        self.put(child, detached)
        self.put(self.entry('%1', 3))
        undo.record_task_tree_move(child, old, detached)
        with self.assertRaisesRegex(undo.Refused, 'task for tab %2 closed or changed conversations'):
            undo.run('undo', None, self.env)
        self.put(self.entry('%1', 1), helper('%2', thread(2), moved=True))
        with self.assertRaisesRegex(undo.Refused, 'task loop'):
            undo.run('undo', None, self.env)
        self.assertEqual(self.records['%2'], detached)

    def test_move_redo_refuses_a_tab_that_started_its_own_helper(self):
        self.put(self.entry('%1', 1))
        child = self.entry('%2', 2)
        new = helper('%1', thread(1), moved=True)
        self.put(child, new)
        undo.record_task_tree_move(child, None, new)
        undo.run('undo', None, self.env)
        self.put(self.entry('%3', 3), helper('%2', thread(2)))
        with self.assertRaisesRegex(undo.Refused, 'now has helpers'):
            undo.run('redo', None, self.env)
        self.assertIsNone(self.records['%2'])

    def capture(self, entry):
        pane = entry['id']
        return {'pane': pane, 'agent': entry['agent'], 'thread': entry['thread'], 'name': entry['pane_name'],
                'session': 'task', 'session_id': entry['tmux_session'], 'folder': self.folder,
                'options': {}, 'session_options': {}, 'order': [int(pane[1:]), 0], 'server': self.server_id}

    def closed_tree(self, *, kept=True, root_record=None):
        self.root = self.entry('%1', 1, 'Main')
        self.worker = self.entry('%2', 2, 'Worker')
        self.review = self.entry('%3', 3, 'Review')
        self.review_record = helper('%2', thread(2), root='%1', root_thread=thread(1))
        self.worker_record = helper('%1', thread(1))
        self.detached = {'kind': 'own', 'owner_pane': '%2', 'owner_thread': thread(2),
                         'moved_by': 'user', 'sent_at': 0}
        self.next_pane = 9
        self.killed = []
        self.reopen_anchor = []
        self.enterContext(patch.object(undo, 'note_released', side_effect=lambda tab, _: tab))
        self.enterContext(patch.object(undo, '_reopen_check'))
        self.enterContext(patch.object(undo, '_check_reopen_target'))
        self.enterContext(patch.object(undo, '_check_released'))
        self.enterContext(patch.object(undo, '_restack_released'))
        self.enterContext(patch.object(undo, '_need_pane', side_effect=lambda pane, _: (self.entries[pane]['tmux_session'], '', '')))
        self.enterContext(patch.object(undo, '_target_session', return_value='$1' if kept else None))
        self.enterContext(patch.object(undo, '_reopen', side_effect=self.reopen))
        self.enterContext(patch.object(undo, '_close', side_effect=self.close))
        self.enterContext(patch.object(undo, 'capture', side_effect=lambda pane, run, entry: self.capture(entry)))
        closed = [(self.capture(self.worker), self.worker_record), (self.capture(self.root), root_record)]
        kept_changes = []
        if kept:
            self.put(self.review, self.detached)
            kept_changes.append((self.review, self.review_record, self.detached))
        undo.record_task_tree_close(closed, kept_changes, self.runner)

    def reopen(self, tab, _, after=None):
        if self.server_id is None:
            self.server_id = '2:3'
        self.next_pane += 1
        pane = f'%{self.next_pane}'
        session = self.entries[after]['tmux_session'] if after else '$1' if self.entries else '$2'
        self.put(self.entry(pane, int(tab['thread'][-12:]), tab['name'], session))
        self.reopen_anchor.append(after)
        return pane

    def close(self, pane, env):
        self.killed.append(pane)
        del self.entries[pane], self.records[pane]
        env.forget()

    def act(self, direction):
        plan = undo.run(direction, None, self.env)
        self.assertEqual(plan['status'], 'confirm')
        return undo.run(direction, plan['id'], self.env, plan['token'])

    def test_one_close_step_reopens_the_tree_and_redo_uses_reopened_ids(self):
        self.closed_tree()
        saved, = undo_store.entries()
        self.assertEqual(saved['label'], 'Closed task `Main`')
        self.assertEqual(len(saved['actions']), 1)
        self.assertEqual(self.act('undo')['state'], 'undone')
        self.assertEqual(self.records['%11'], helper('%10', thread(1)))
        self.assertEqual(self.records['%3'], helper('%11', thread(2), root='%10', root_thread=thread(1)))
        self.assertEqual(self.act('redo')['state'], 'done')
        self.assertEqual(self.killed, ['%11', '%10'])
        self.assertEqual(self.records['%3'], dict(self.detached, owner_pane='%11'))
        self.assertEqual(self.act('undo')['state'], 'undone')
        self.assertEqual(self.records['%3'], helper('%13', thread(2), root='%12', root_thread=thread(1)))

    def test_close_of_a_whole_session_reopens_children_in_the_new_session(self):
        self.closed_tree(kept=False)
        self.act('undo')
        self.assertEqual(self.reopen_anchor, [None, '%10'])
        self.assertEqual({entry['tmux_session'] for entry in self.entries.values()}, {'$2'})
        self.assertEqual(self.records['%11'], helper('%10', thread(1)))

    def test_close_of_the_last_session_can_restart_tmux(self):
        self.closed_tree(kept=False)
        self.server_id = None
        self.assertEqual(self.act('undo')['state'], 'undone')
        self.assertEqual(self.records['%11'], helper('%10', thread(1)))
        self.assertEqual(self.act('redo')['state'], 'done')

    def test_a_new_tmux_server_blocks_reuse_of_the_old_pane_ids(self):
        self.closed_tree(kept=False)
        self.server_id = '2:3'
        with self.assertRaisesRegex(undo.Refused, 'tmux restarted'):
            undo.run('undo', None, self.env)
        self.assertEqual(self.entries, {})

    def test_offline_undo_refuses_an_unbound_external_opener(self):
        self.closed_tree(kept=False, root_record={'kind': 'own', 'owner_pane': '%10', 'sent_at': 0})
        self.server_id = None
        with self.assertRaisesRegex(undo.Refused, 'cannot confirm an old relationship'):
            undo.run('undo', None, self.env)
        self.assertEqual(self.entries, {})

    def test_close_undo_refuses_when_a_kept_child_changed_conversations(self):
        self.closed_tree()
        self.put(self.entry('%3', 33), self.detached)
        with self.assertRaisesRegex(undo.Refused, 'Tab %3 changed conversations'):
            undo.run('undo', None, self.env)
        self.assertEqual(self.reopen_anchor, [])

    def test_close_redo_refuses_when_a_new_child_joined(self):
        self.closed_tree()
        self.act('undo')
        self.put(self.entry('%15', 15), helper('%10', thread(1)))
        with self.assertRaisesRegex(undo.Refused, 'Tab %15 joined the task'):
            undo.run('redo', None, self.env)
        self.assertEqual(self.killed, [])

    def test_partial_redo_can_resume_without_closing_a_tab_twice(self):
        self.closed_tree()
        self.act('undo')
        original_close = self.close

        def interrupted(pane, env):
            if pane == '%10':
                raise OSError('The last close failed.')
            original_close(pane, env)

        with patch.object(undo, '_close', side_effect=interrupted):
            with self.assertRaisesRegex(undo.Refused, 'Only part'):
                self.act('redo')
        self.assertEqual(self.killed, ['%11'])
        self.assertEqual(self.records['%3'], dict(self.detached, owner_pane='%11'))
        self.assertEqual(self.act('redo')['state'], 'done')
        self.assertEqual(self.killed, ['%11', '%10'])
        self.assertEqual(self.act('undo')['state'], 'undone')

    def test_failed_reopen_rolls_back_the_other_reopened_tab(self):
        self.closed_tree()
        original_reopen = self.reopen

        def interrupted(tab, env, after=None):
            if tab['name'] == 'Worker':
                raise OSError('Could not reopen Worker.')
            return original_reopen(tab, env, after)

        with patch.object(undo, '_reopen', side_effect=interrupted):
            with self.assertRaisesRegex(undo.Refused, 'Could not reopen Worker'):
                self.act('undo')
        self.assertEqual(self.killed, ['%10'])
        self.assertEqual(self.records, {'%3': self.detached})
        self.assertEqual(self.act('undo')['state'], 'undone')

    def test_capture_checks_the_saved_conversation_against_the_live_tab(self):
        entry = self.entry('%2', 2)
        fields = ['Task', '$1', os.fspath(self.folder), '1', '0'] + [''] * (
            len(auto_resume.OPTIONS) + len(auto_resume.SESSION_OPTIONS))
        for old_thread, agent in ((thread(1), 'codex'), (thread(2), 'claude')):
            with self.subTest(thread=old_thread, agent=agent):
                saved = {'thread': old_thread, 'agent': agent, 'folder': '/stale'}
                with patch.object(auto_resume, 'saved_tab', return_value=saved), patch.object(
                        undo, '_show', return_value=fields):
                    tab = undo.capture('%2', self.runner, entry)
                self.assertEqual(tab['thread'], thread(2))
                self.assertEqual(tab['agent'], 'codex')
                self.assertEqual(tab['folder'], self.folder)

    def test_saved_default_chat_is_not_a_live_tab_that_blocks_undo(self):
        tab = self.capture(self.entry('%2', 2))
        env = undo.Env(run=self.runner, entries=lambda: [{'id': 'saved', 'thread': thread(2)}])
        with patch('history.find_chat', return_value={'id': thread(2)}):
            undo._reopen_check(tab, env)

    def test_reopen_retains_an_explicit_placement_in_unstacked(self):
        tab = self.capture(self.entry('%2', 2))
        tab['options'] = {'@group_by': 'user'}
        self.assertEqual(undo._options_for(tab, existing=True)['options'], {'@group_by': 'user'})


if __name__ == '__main__':
    unittest.main()
