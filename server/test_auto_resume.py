"""Auto-resume uses an isolated cache and fake tmux, never real terminals."""
import importlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import auto_resume
import paths
import server

THREAD = '0f8fad5b-d9cb-469f-a165-70867728950e'
GROUPS = '[{"id": "g1a2b3c4d", "name": "Docs", "project": ""}]'


class FakeTmux:
    def __init__(self):
        self.calls = []
        self.server = '10:100'
        self.panes = {}
        self.next_id = 10
        self.before = lambda argv: None
        self.screen = 'resume error'

    def add(self, entry, **options):
        self.panes[entry['id']] = dict(pane_id=entry['id'], session_id=entry['tmux_session'],
                                      session_name=entry['label'].split(':')[0], window_id='@' + entry['id'][1:],
                                      pane_current_path=entry['cwd'], window_index=entry['position'].split('.')[0],
                                      pane_index=entry['position'].split('.')[1], **options)

    def lose(self, identifier=None):
        self.server = identifier
        self.panes.clear()

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.before(argv)
        command = argv[1]
        output = ''
        code = 0
        error = ''
        if command == 'display-message':
            if not self.server:
                code, error = 1, 'no server running'
            elif argv[-1] == '#{pid}:#{start_time}':
                output = self.server
            elif argv[-1] == '#{window-size}\t#{window_width}\t#{window_height}':
                output = 'latest\t120\t40'
            else:
                target = argv[argv.index('-t') + 1]
                row = (next((row for row in self.panes.values() if row['session_id'] == target[:-1]), None)
                       if target.endswith(':') else self.panes.get(target))
                if row is None:
                    code, error = 1, 'no target'
                else:
                    output = re.sub(r'#\{([^}]+)\}', lambda match: row.get(match[1], ''), argv[-1])
        elif command == 'list-panes':
            rows = list(self.panes.values())
            if '-s' in argv:
                target = argv[argv.index('-t') + 1].lstrip('=').rstrip(':')
                rows = [row for row in rows if target in (row['session_name'], row['session_id'])]
            fmt = argv[argv.index('-F') + 1]
            output = '\n'.join(re.sub(r'#\{([^}]+)\}', lambda match: row.get(match[1], ''), fmt) for row in rows)
            if not rows:
                code, error = 1, 'no panes'
        elif command in ('new-session', 'new-window'):
            self.next_id += 1
            pane = '%' + str(self.next_id)
            folder = argv[argv.index('-c') + 1]
            if command == 'new-session':
                session_id, name = '$' + str(self.next_id), argv[argv.index('-s') + 1]
                self.server = self.server or '20:200'
                position = '0.0'
            else:
                window = argv[argv.index('-t') + 1]
                row = next(row for row in self.panes.values() if row['window_id'] == window)
                session_id, name = row['session_id'], row['session_name']
                position = str(int(row['window_index']) + 1) + '.0'
            self.add(dict(id=pane, tmux_session=session_id, label=name, cwd=folder, position=position))
            output = pane
        elif command == 'set-option' and '-p' in argv:
            self.panes[argv[argv.index('-t') + 1]][argv[-2]] = argv[-1]
        elif command == 'capture-pane':
            output = self.screen
        elif command == 'kill-pane':
            self.panes.pop(argv[-1], None)
        elif command == 'list-sessions':
            output = '\n'.join(sorted({row['session_id'] for row in self.panes.values()}))
        elif command == 'kill-session':
            self.panes = {pane: row for pane, row in self.panes.items() if row['session_id'] != argv[-1]}
        if code and kwargs.get('check'):
            raise subprocess.CalledProcessError(code, argv, output, error)
        return subprocess.CompletedProcess(argv, code, output, error)


class AutoResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.enterContext(patch.object(paths, 'CACHE', self.cache))
        self.enterContext(patch.object(auto_resume, '_tombstones', {}))
        self.enterContext(patch.object(auto_resume, '_cache', (None, {})))
        self.enterContext(patch.object(paths, 'SCOPE_TMUX', False))
        self.prefs = {}
        self.enterContext(patch.object(auto_resume.prefs_sync, 'load', side_effect=lambda: {'prefs': self.prefs}))
        self.find_chat = self.enterContext(patch.object(auto_resume.history, 'find_chat', return_value={'cwd': '/Users/test'}))
        self.isdir = self.enterContext(patch.object(auto_resume.os.path, 'isdir', return_value=True))
        self.tmux = FakeTmux()
        self.entry = dict(id='%1', agent='claude', thread=THREAD, writable=True,
                          tmux_session='$1', label='work', position='0.0', cwd='/Users/test',
                          transcript='/Users/test/chat.jsonl', pane_name='Task', pane_icon='')
        self.entries = [self.entry]
        self.clock = 1000
        self.tmux.add(self.entry)
        self.states = {}
        self.sleeps = []
        self.changed = Mock()

    def tick(self):
        auto_resume.tick(entries=lambda: self.entries, folder_of=lambda e: e['cwd'],
                         status_of=lambda e: {'state': self.states.get(e['id'], 'Idle')}, display_dir=lambda e: e['cwd'],
                         run=self.tmux, now=lambda: self.clock, sleep=self.sleeps.append, on_change=self.changed)

    def saved(self):
        return json.loads((self.cache / 'open-tabs.json').read_text())

    def test_closed_tab_stays_gone_after_loss_and_stale_discovery(self):
        self.tick()
        self.assertIn(THREAD, self.saved()['tabs'])
        auto_resume.forget_pane('%1', run=self.tmux, now=lambda: self.clock)
        self.assertEqual(self.saved()['tabs'], {})
        self.tick()  # A stale discovery result must not undo the close.
        self.assertEqual(self.saved()['tabs'], {})
        self.tmux.server = None
        self.entries = []
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})
        self.assertFalse(any('send-keys' in call for call in self.tmux.calls))

    def add(self, number, **fields):
        entry = dict(self.entry, id='%' + str(number), thread=f'{number:08x}-d9cb-469f-a165-70867728950e', **fields)
        self.entries.append(entry)
        self.tmux.add(entry)
        return entry

    def sends(self):
        return [call for call in self.tmux.calls if call[1] == 'send-keys']

    def restart(self, identifier=None):
        self.tmux.lose(identifier)
        self.entries = []
        self.tick()

    def store(self, saved):
        (self.cache / 'open-tabs.json').write_text(json.dumps(saved))

    def test_forget_session_removes_all_its_tabs_and_tombstones_stale_rows(self):
        other = self.add(2, label='work:1.0', position='1.0')
        kept = self.add(3, label='other', tmux_session='$2')
        self.tick()
        auto_resume.forget_session('$1', run=self.tmux, now=lambda: self.clock)
        self.tick()
        self.assertEqual(set(self.saved()['tabs']), {kept['thread']})
        self.assertNotIn(other['thread'], self.saved()['tabs'])
        self.assertIn('%2', auto_resume._tombstones)

    def test_failed_kill_can_reappear_after_tombstone_ttl(self):
        self.tick()
        auto_resume.forget_pane('%1', run=self.tmux, now=lambda: self.clock)
        self.clock += 29
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})
        self.clock += 1
        self.tick()
        self.assertIn(THREAD, self.saved()['tabs'])

    def test_forget_new_session_id_removes_waiting_tabs_with_old_or_empty_ids(self):
        self.tick()
        saved = self.saved()
        for old_id in ('$old', ''):
            with self.subTest(session_id=old_id):
                saved['tabs'][THREAD].update(state='pending', pane='', session_id=old_id)
                self.store(saved)
                self.tmux.lose('30:300')
                live = dict(self.entry, id='%99', tmux_session='$99', thread=None)
                self.tmux.add(live)
                self.entries = [live]
                def before(argv):
                    self.assertIn('$99', auto_resume._tombstones)
                self.tmux.before = before
                auto_resume.forget_session('$99', run=self.tmux, now=lambda: self.clock)
                self.tmux.before = lambda argv: None
                self.assertEqual(self.saved()['tabs'], {})
                self.tick()
                self.assertEqual(self.sends(), [])

    def test_forget_session_removes_waiting_siblings_before_lookup_failure(self):
        waiting = self.add(2)
        self.tick()
        saved = self.saved()
        saved['tabs'][waiting['thread']].update(state='pending', pane='', session_id='')
        self.store(saved)
        auto_resume.forget_session('$1', run=Mock(side_effect=subprocess.TimeoutExpired('tmux', 5)))
        self.assertEqual(self.saved()['tabs'], {})

    def test_failed_session_name_lookup_does_not_remove_waiting_tabs(self):
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD].update(state='pending', pane='', session_id='')
        self.store(saved)
        run = Mock(return_value=subprocess.CompletedProcess([], 1, 'work', 'no session'))
        auto_resume.forget_session('$99', run=run, now=lambda: self.clock)
        self.assertIn(THREAD, self.saved()['tabs'])

    def test_empty_session_name_lookup_keeps_waiting_tab_with_empty_name(self):
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD].update(state='pending', pane='', session_id='', session='')
        self.store(saved)
        run = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        auto_resume.forget_session('$99', run=run, now=lambda: self.clock)
        self.assertIn(THREAD, self.saved()['tabs'])

    def test_close_of_reused_session_id_keeps_waiting_tabs_of_another_session(self):
        self.tick()
        with patch.object(auto_resume, 'launch', return_value=False):
            self.restart()
        self.assertEqual(self.saved()['tabs'][THREAD]['session_id'], '')
        self.tmux.server = '30:300'
        other = dict(self.entry, id='%99', label='other', tmux_session='$1', thread=None)
        self.tmux.add(other)
        self.entries = [other]
        auto_resume.forget_session('$1', run=self.tmux, now=lambda: self.clock)
        self.assertIn(THREAD, self.saved()['tabs'])
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')

    def test_missing_pane_is_closed_after_grace_and_stays_closed_on_loss(self):
        self.tick()
        self.entries = []
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['missing_since'], self.clock)
        self.clock += auto_resume.CLOSE_AFTER
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})
        self.restart()
        self.assertEqual(self.sends(), [])

    def test_missing_pane_that_returns_within_grace_stays(self):
        self.tick()
        self.entries = []
        self.tick()
        self.clock += auto_resume.CLOSE_AFTER - 1
        self.entries = [self.entry]
        self.tick()
        self.assertIsNone(self.saved()['tabs'][THREAD]['missing_since'])

    def test_missing_pane_already_closed_before_server_loss_is_dropped(self):
        self.tick()
        self.entries = []
        self.tick()
        self.clock += auto_resume.CLOSE_AFTER + 1
        self.restart()
        self.assertEqual(self.saved()['tabs'], {})
        self.assertEqual(self.sends(), [])

    def test_archive_tab_and_session_excludes_until_restored(self):
        for prefs in ({'archivedTabs': {'%1': {'dir': '/Users/test', 'thread': THREAD}}},
                      {'archivedTabs': {'%1': {'dir': '/Users/test', 'thread': None}}},
                      {'archived': {'$1': 'work'}}):
            with self.subTest(prefs=prefs):
                self.tick()
                self.prefs.update(prefs)
                self.tick()
                self.assertEqual(self.saved()['tabs'], {})
                self.prefs.clear()
                self.tick()
                self.assertIn(THREAD, self.saved()['tabs'])
                self.prefs.update(prefs)
                self.tick()
                self.restart()
                self.assertEqual(self.sends(), [])
                self.entries = [self.entry]
                self.tmux.server = '30:300'
                self.tmux.add(self.entry)
                self.prefs.clear()

    def test_archive_match_uses_display_dir_thread_and_session_name(self):
        self.entry.update(label='work:2.0', position='2.0')
        for archived in ({'dir': '~/elsewhere', 'thread': THREAD},
                         {'dir': '/Users/test', 'thread': 'different'}):
            self.prefs['archivedTabs'] = {'%1': archived}
            self.tick()
            self.assertIn(THREAD, self.saved()['tabs'])
        self.prefs = {'archived': {'$1': 'work'}}
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})

    def test_archive_removes_waiting_tabs_only_for_a_matching_live_session(self):
        other = self.add(2, label='other', tmux_session='$2')
        self.tick()
        saved = self.saved()
        for tab in saved['tabs'].values():
            tab.update(state='pending', pane='', session_id='')
        for archived, removed in (({'$99': 'work'}, True), ({'$old': 'work'}, False),
                                  ({'$99': 'other'}, False)):
            with self.subTest(archived=archived):
                self.store(saved)
                self.tmux.lose('30:300')
                self.tmux.calls.clear()
                # An archived session can have only an ineligible shell pane.
                live = dict(self.entry, id='%99', tmux_session='$99', agent='shell', thread=None, writable=False)
                self.tmux.add(live)
                self.entries = [live]
                self.prefs['archived'] = archived
                self.tick()
                tabs = self.saved()['tabs']
                self.assertEqual(THREAD not in tabs, removed)
                self.assertEqual(tabs[other['thread']]['state'], 'resuming')
                self.assertEqual(sum(THREAD in call[-1] for call in self.sends()), 0 if removed else 1)
                self.tick()
                self.assertEqual(sum(THREAD in call[-1] for call in self.sends()), 0 if removed else 1)

    def test_restart_preserves_order_options_profile_and_only_sends_command_enter(self):
        self.entry.update(label='zeta:3.0', position='3.0')
        self.tmux.add(self.entry)
        second = self.add(2, label='zeta:1.2', position='1.2', agent='codex', codex_profile='foundry')
        first = self.add(3, label='alpha', tmux_session='$3', position='4.0')
        self.tmux.panes['%2'].update({'@chat_name': 'Saved', '@chat_icon': 'star', '@chat_icon_by': 'user',
                                     '@chat_name_at': '123', '@project': 'project-1', '@project_rev': 'rev',
                                     '@worktree': 'tree', '@group': 'g1a2b3c4d',
                                     '@groups': GROUPS})
        self.tick()
        saved_options = self.saved()['tabs'][second['thread']]['options']
        self.restart()
        commands = [call[-1] for call in self.sends()[::2]]
        self.assertEqual(commands, ['claude --resume ' + first['thread'],
                                    'codex -p foundry resume ' + second['thread'], 'claude --resume ' + THREAD])
        self.assertEqual(len(self.sends()), 6)
        for command, enter in zip(self.sends()[::2], self.sends()[1::2]):
            self.assertEqual(command[1:4], ['send-keys', '-t', enter[3]])
            self.assertEqual(command[4], '-l')
            self.assertEqual(enter[4:], ['Enter'])
        created = [call[1] for call in self.tmux.calls if call[1] in ('new-session', 'new-window')]
        self.assertEqual(created, ['new-session', 'new-session', 'new-window'])
        tab = self.saved()['tabs'][second['thread']]
        for opt, value in saved_options.items():
            self.assertIn([paths.TMUX, 'set-option', '-p', '-t', tab['pane'], opt, value], self.tmux.calls)
        self.assertIn([paths.TMUX, 'set-option', '-t', tab['session_id'], '@project', 'project-1'], self.tmux.calls)
        self.assertEqual(saved_options['@group'], 'g1a2b3c4d')
        self.assertEqual(self.tmux.panes[tab['pane']]['@group'], 'g1a2b3c4d')
        self.assertIn([paths.TMUX, 'set-option', '-t', tab['session_id'], '@groups', GROUPS], self.tmux.calls)
        self.assertEqual(tab['profile'], 'foundry')
        self.assertEqual(self.sleeps, [auto_resume.GAP, auto_resume.GAP])
        self.changed.assert_called_once_with()

    def test_restart_preserves_worktree_branch_and_folder_after_resume(self):
        folder = '/Users/test/worktrees/feature-x'
        self.entry['cwd'] = folder
        self.tmux.add(self.entry, **{'@worktree': 'feature/x'})
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['options']['@worktree'], 'feature/x')
        self.assertEqual(self.saved()['tabs'][THREAD]['folder'], folder)

        self.restart()
        tab = self.saved()['tabs'][THREAD]
        pane = tab['pane']
        self.assertNotEqual(pane, self.entry['id'])
        self.assertEqual(tab['options']['@worktree'], 'feature/x')
        self.assertEqual(tab['folder'], folder)
        self.assertEqual(self.tmux.panes[pane]['pane_current_path'], folder)
        self.assertEqual(self.tmux.panes[pane]['@worktree'], 'feature/x')
        self.assertEqual(self.sends(), [
            [paths.TMUX, 'send-keys', '-t', pane, '-l', 'claude --resume ' + THREAD],
            [paths.TMUX, 'send-keys', '-t', pane, 'Enter'],
        ])

    def test_existing_session_options_are_set_before_keys_and_name_before_rename(self):
        self.tmux.panes['%1']['@project'] = 'project-1'
        self.tmux.panes['%1']['@chat_icon'] = 'star'
        self.tick()
        self.entries = []
        self.tmux.server = '30:300'
        self.tick()
        pane = self.saved()['tabs'][THREAD]['pane']
        calls = self.tmux.calls
        name = calls.index([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_name', 'Task'])
        icon = calls.index([paths.TMUX, 'set-option', '-p', '-t', pane, '@chat_icon', 'star'])
        rename = calls.index([paths.TMUX, 'rename-window', '-t', pane, 'Task'])
        send = calls.index(self.sends()[0])
        self.assertLess(name, rename)
        self.assertLess(name, icon)
        self.assertLess(rename, send)
        session_id = self.saved()['tabs'][THREAD]['session_id']
        project = calls.index([paths.TMUX, 'set-option', '-t', session_id, '@project', 'project-1'])
        self.assertLess(project, send)
        self.assertEqual([call[1] for call in calls].count('new-window'), 1)

    def test_same_server_after_tether_reload_does_not_resume(self):
        self.tick()
        importlib.reload(auto_resume)
        self.tick()
        self.assertEqual(self.sends(), [])
        self.entries = []
        self.tick()
        self.clock += auto_resume.CLOSE_AFTER
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})

    def test_thread_already_open_elsewhere_is_not_launched(self):
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD]['state'] = 'pending'
        self.store(saved)
        self.entries = [dict(self.entry, id='%9')]
        auto_resume.launch(entries=lambda: self.entries, run=self.tmux, now=lambda: self.clock, sleep=self.sleeps.append)
        self.assertEqual(self.saved()['tabs'][THREAD]['pane'], '%9')
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'open')
        self.assertEqual(self.sends(), [])

    def test_new_server_id_saved_and_second_tick_does_not_launch_again(self):
        self.tick()
        self.restart()
        self.assertEqual(self.saved()['server'], '20:200')
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')

    def test_resuming_saved_before_send_and_reload_cannot_launch_twice(self):
        self.tick()
        seen = []
        def before(argv):
            self.assertFalse(auto_resume._lock.locked())
            if argv[1] == 'send-keys':
                seen.append(self.saved()['tabs'][THREAD]['state'])
                importlib.reload(auto_resume)
        self.tmux.before = before
        self.restart()
        self.assertEqual(seen, ['resuming', 'resuming'])
        self.tick()
        self.assertEqual(len(self.sends()), 2)

    def test_missing_folder_fails_one_tab_persists_and_retry_launches_once(self):
        other = self.add(2, cwd='/Users/test/other')
        self.tick()
        self.isdir.side_effect = lambda folder: folder != '/Users/test'
        self.restart()
        tab = self.saved()['tabs'][THREAD]
        self.assertEqual((tab['state'], tab['error']), ('failed', 'Folder not found: /Users/test'))
        self.assertEqual(self.saved()['tabs'][other['thread']]['state'], 'resuming')
        self.tick()
        notice = auto_resume.notice()
        self.assertEqual(notice['failed'], 1)
        self.assertEqual(notice['tabs'][0]['thread'], THREAD)
        self.assertEqual(set(notice['tabs'][0]), {'thread', 'agent', 'name', 'folder', 'error', 'state'})
        self.isdir.side_effect = None
        self.assertEqual(auto_resume.retry(), 1)
        self.assertTrue(auto_resume._wake.is_set())
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        self.tick()
        self.assertEqual(len(self.sends()), 4)

    def test_run_exception_on_one_tab_does_not_stop_others(self):
        self.add(2, label='other', tmux_session='$2')
        self.tick()
        def fail(argv):
            if argv[1] == 'new-session' and argv[argv.index('-s') + 1] == 'other':
                raise OSError('launch failed')
        self.tmux.before = fail
        self.restart()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(auto_resume.notice()['tabs'][0]['error'], 'launch failed')

    def test_timeout_shell_reads_error_and_kills_pane(self):
        self.tick()
        self.restart()
        tab = self.saved()['tabs'][THREAD]
        self.entries = [dict(self.entry, id=tab['pane'], agent='shell', writable=False, thread=None)]
        self.tmux.screen = '\n earlier\n\n  launcher failed: ' + 'x' * 200 + '\n\n'
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        failed = self.saved()['tabs'][THREAD]
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['error'], ('launcher failed: ' + 'x' * 200)[:160])
        self.assertNotIn(tab['pane'], self.tmux.panes)

    def test_pending_agent_gets_five_verify_periods_without_kill(self):
        self.tick()
        self.restart()
        pane = self.saved()['tabs'][THREAD]['pane']
        self.entries = [dict(self.entry, id=pane, thread='pending-123', pending_conversation=True)]
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')
        self.clock += 4 * auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'failed')
        self.assertIn(pane, self.tmux.panes)
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        tab = self.saved()['tabs'][THREAD]
        self.assertEqual(tab['state'], 'resuming')
        self.assertEqual(tab['pane'], pane)
        self.assertEqual(tab['started'], self.clock)
        self.assertEqual(auto_resume.notice()['failed'], 0)
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual([call[1] for call in self.tmux.calls].count('new-session'), 1)
        self.assertFalse(any(call[1] == 'new-window' for call in self.tmux.calls))
        self.clock += 5 * auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(len(self.sends()), 2)
        self.assertIn(pane, self.tmux.panes)

    def test_retry_rechecks_a_live_agent_then_can_fail_again_or_resume_an_ended_pane(self):
        self.tick()
        self.restart()
        pane = self.saved()['tabs'][THREAD]['pane']
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual([call[1] for call in self.tmux.calls].count('new-session'), 1)
        self.assertFalse(any(call[1] == 'new-window' for call in self.tmux.calls))
        tab = self.saved()['tabs'][THREAD]
        self.assertEqual(tab['pane'], pane)
        self.assertEqual(tab['state'], 'resuming')
        self.assertEqual(tab['started'], self.clock)
        self.assertEqual(tab['error'], '')
        self.assertEqual(auto_resume.notice()['failed'], 0)
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(len(self.sends()), 2)
        self.tmux.add(dict(self.entry, id='%99', tmux_session='$99', label='other'))
        self.tmux.panes.pop(pane)
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        self.assertEqual(len(self.sends()), 4)
        self.assertNotEqual(self.saved()['tabs'][THREAD]['pane'], pane)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')
        pane = self.saved()['tabs'][THREAD]['pane']
        saved = self.saved()
        saved['tabs'][THREAD]['state'] = 'failed'
        self.store(saved)
        self.entries = [dict(self.entry, id=pane, agent='shell', thread=None, writable=False)]
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        self.assertEqual(len(self.sends()), 6)
        self.assertNotEqual(self.saved()['tabs'][THREAD]['pane'], pane)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')

    def test_missing_resuming_pane_fails_without_relaunch(self):
        self.tick()
        self.restart()
        self.tmux.panes.clear()
        # A live server can have another unrelated pane.
        self.tmux.add(dict(self.entry, id='%90', thread=None))
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'failed')
        self.assertEqual(len(self.sends()), 2)

    def test_conversation_not_found_fails(self):
        self.tick()
        self.find_chat.return_value = None
        self.restart()
        self.assertEqual(auto_resume.notice()['tabs'][0]['error'], 'Conversation not found.')
        self.assertEqual(self.sends(), [])

    def test_discovery_error_during_launch_waits_for_the_next_tick(self):
        # Discovery reads /proc, and a process can end during that walk. Tmux loss ends many processes.
        self.tick()
        self.tmux.lose()
        self.entries = []
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise FileNotFoundError('/proc/1/stat')
            return self.entries
        ticks = dict(folder_of=lambda e: e['cwd'], status_of=lambda e: {'state': 'Idle'}, display_dir=lambda e: e['cwd'],
                     run=self.tmux, now=lambda: self.clock, sleep=self.sleeps.append, on_change=self.changed)
        auto_resume.tick(entries=flaky, **ticks)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'pending')
        self.assertEqual(auto_resume.notice()['failed'], 0)
        self.assertEqual(self.sends(), [])
        auto_resume.tick(entries=flaky, **ticks)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')
        self.assertEqual(len(self.sends()), 2)

    def test_saved_transcript_counts_when_the_chat_list_hides_the_conversation(self):
        # The Past chats list leaves out `codex exec` threads, but such a thread can be open in a tab.
        transcript = self.cache / 'chat.jsonl'
        transcript.write_text('{}\n')
        self.entry['transcript'] = str(transcript)
        self.tick()
        self.find_chat.return_value = None
        self.restart()
        self.assertEqual(auto_resume.notice()['tabs'], [])
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.saved()['tabs'][THREAD]['offset'], transcript.stat().st_size)

    def test_preference_off_clears_loss_and_turning_on_does_not_restore(self):
        self.tick()
        self.prefs['autoResume'] = False
        self.restart()
        self.assertEqual(self.saved()['tabs'], {})
        self.prefs['autoResume'] = True
        self.tick()
        self.assertEqual(self.sends(), [])

    def test_corrupt_snapshots_are_empty_repaired_atomically_and_private(self):
        for value in ('bad JSON', '[]', '{"version":1,"tabs":[]}',
                      '{"version":1,"server":null,"tabs":{"bad":{},"other":null}}',
                      '{"version":1,"server":[],"tabs":{}}', '\udcff'):
            with self.subTest(value=repr(value)):
                path = self.cache / 'open-tabs.json'
                path.write_bytes(value.encode('utf-8', errors='surrogateescape'))
                self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})
                self.tick()
                self.assertIn(THREAD, self.saved()['tabs'])
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(list(self.cache.glob('*.tmp')), [])

    def test_malformed_individual_rows_drop_without_losing_good_tab(self):
        self.tick()
        saved = self.saved()
        saved['tabs']['bad'] = []
        for number, change in enumerate(({'order': ['0', 1]}, {'started': 'bad'}, {'options': []},
                                         {'session_options': {'evil': 'value'}}, {'working': 1}, {'offset': -1},
                                         {'started': float('nan')}, {'missing_since': float('inf')}), 2):
            thread = f'{number:08x}-d9cb-469f-a165-70867728950e'
            saved['tabs'][thread] = dict(saved['tabs'][THREAD], thread=thread, **change)
        self.store(saved)
        with auto_resume._lock:
            self.assertEqual(set(auto_resume._read()['tabs']), {THREAD})

    def test_limit_twenty_then_automatically_resume_five_with_nineteen_gaps(self):
        for number in range(2, 26):
            self.add(number, label=f'work:{number}.0', position=f'{number}.0')
        self.tick()
        self.restart()
        self.assertEqual(len(self.sends()), 40)
        self.assertEqual(self.sleeps, [auto_resume.GAP] * 19)
        self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})
        waiting = [tab for tab in self.saved()['tabs'].values() if tab['state'] == 'pending']
        self.assertEqual(len(waiting), 5)
        self.assertTrue(all(tab['error'] == '' for tab in waiting))
        self.tick()
        self.assertEqual(len(self.sends()), 40)
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(len(self.sends()), 50)
        self.tick()
        self.assertEqual(len(self.sends()), 50)

    def test_forty_four_tabs_resume_in_three_passes_once_each(self):
        for number in range(2, 45):
            self.add(number, label=f'work:{number}.0', position=f'{number}.0',
                     agent='codex' if number % 2 == 0 else 'claude')
        originals = {entry['thread']: entry for entry in self.entries}
        self.tick()
        self.restart()
        for total, gaps, waiting in ((20, 19, 24), (40, 38, 4), (44, 41, 0)):
            self.assertEqual(len(self.sends()), total * 2)
            self.assertEqual(self.sleeps, [auto_resume.GAP] * gaps)
            tabs = self.saved()['tabs']
            pending = [tab for tab in tabs.values() if tab['state'] == 'pending']
            self.assertEqual(len(pending), waiting)
            self.assertTrue(all(tab['error'] == '' for tab in pending))
            self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})
            self.clock += auto_resume.VERIFY_SECS - 1
            self.tick()
            self.assertEqual(len(self.sends()), total * 2)
            self.entries = [dict(originals[thread], id=tab['pane'], tmux_session=tab['session_id'])
                            for thread, tab in tabs.items() if tab['state'] != 'pending']
            self.tick()
        self.assertEqual(len(self.sends()), 88)
        self.assertTrue(all(tab['state'] == 'open' for tab in self.saved()['tabs'].values()))
        for thread in originals:
            self.assertEqual(sum(thread in call[-1] for call in self.sends()), 1)

    def test_stuck_resuming_tab_blocks_at_fifty_nine_seconds_but_not_sixty(self):
        for number in range(2, 22):
            self.add(number, position=f'{number}.0')
        originals = {entry['thread']: entry for entry in self.entries}
        self.tick()
        self.restart()
        tabs = self.saved()['tabs']
        stuck = tabs[THREAD]
        self.entries = [dict(originals[thread], id=tab['pane'], tmux_session=tab['session_id'])
                        for thread, tab in tabs.items() if tab['state'] == 'resuming' and thread != THREAD]
        self.entries.append(dict(self.entry, id=stuck['pane'], thread='pending-123', pending_conversation=True))
        self.clock += 59
        self.tick()
        self.assertEqual(len(self.sends()), 40)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')
        self.clock += 1
        self.tick()
        self.assertEqual(len(self.sends()), 42)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')
        self.assertIn(stuck['pane'], self.tmux.panes)

    def test_waiting_tabs_are_not_notices_or_dismissed_and_still_resume(self):
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD].update(state='pending', pane='', session_id='')
        self.store(saved)
        self.entries = []
        self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})
        self.assertEqual(auto_resume.dismiss(), 0)
        self.assertEqual(auto_resume.retry(), 0)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'pending')
        self.tick()
        self.assertEqual(len(self.sends()), 2)

    def test_notice_and_dismiss_touch_only_failures_among_waiting_tabs(self):
        failed = self.add(2)
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD].update(state='pending', pane='', session_id='')
        saved['tabs'][failed['thread']].update(state='failed', error='failed')
        self.store(saved)
        notice = auto_resume.notice()
        self.assertEqual(notice['failed'], 1)
        self.assertEqual([tab['thread'] for tab in notice['tabs']], [failed['thread']])
        self.assertEqual(auto_resume.dismiss(), 1)
        self.assertEqual(set(self.saved()['tabs']), {THREAD})
        self.entries = []
        self.tick()
        self.assertEqual(len(self.sends()), 2)

    def test_retry_twenty_five_failures_continues_without_another_retry(self):
        for number in range(2, 26):
            self.add(number, position=f'{number}.0')
        originals = {entry['thread']: entry for entry in self.entries}
        self.tick()
        saved = self.saved()
        for tab in saved['tabs'].values():
            tab.update(state='failed', error='failed', pane='', session_id='')
        self.store(saved)
        self.entries = []
        self.assertEqual(auto_resume.retry(), 25)
        self.tick()
        self.assertEqual(len(self.sends()), 40)
        self.assertEqual(self.sleeps, [auto_resume.GAP] * 19)
        self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})
        self.clock += 59
        self.tick()
        self.assertEqual(len(self.sends()), 40)
        tabs = self.saved()['tabs']
        self.entries = [dict(originals[thread], id=tab['pane'], tmux_session=tab['session_id'])
                        for thread, tab in tabs.items() if tab['state'] == 'resuming']
        self.tick()
        self.assertEqual(len(self.sends()), 50)
        self.assertEqual(self.sleeps, [auto_resume.GAP] * 23)
        self.tick()
        self.assertEqual(len(self.sends()), 50)

    def test_retry_obeys_the_gate_of_an_existing_batch(self):
        for number in range(2, 22):
            self.add(number, position=f'{number}.0')
        self.tick()
        self.restart()
        saved = self.saved()
        waiting = next(tab for tab in saved['tabs'].values() if not tab['pane'])
        waiting.update(state='failed', error='failed')
        self.store(saved)
        self.assertEqual(auto_resume.retry(), 1)
        self.clock += 59
        self.tick()
        self.assertEqual(len(self.sends()), 40)
        self.clock += 1
        self.tick()
        self.assertEqual(len(self.sends()), 42)

    def test_old_deferred_snapshot_loads_pending_and_resumes_without_retry(self):
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD].update(state='deferred', error='Old limit message', pane='')
        self.store(saved)
        self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})
        with auto_resume._lock:
            tab = auto_resume._read()['tabs'][THREAD]
        self.assertEqual((tab['state'], tab['error']), ('pending', ''))
        self.assertEqual(auto_resume.dismiss(), 0)
        self.assertEqual(auto_resume.retry(), 0)
        self.restart()
        self.assertEqual(len(self.sends()), 2)

    def test_preference_off_clears_waiting_tabs_before_the_next_batch(self):
        for number in range(2, 26):
            self.add(number)
        self.tick()
        self.restart()
        self.assertEqual(len(self.sends()), 40)
        self.prefs['autoResume'] = False
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})
        self.clock += auto_resume.VERIFY_SECS
        self.prefs['autoResume'] = True
        self.tick()
        self.assertEqual(len(self.sends()), 40)
        self.tick()
        self.assertEqual(len(self.sends()), 40)

    def test_unknown_tmux_leaves_snapshot_and_entries_untouched(self):
        self.tick()
        original = (self.cache / 'open-tabs.json').read_bytes()
        for response in (subprocess.TimeoutExpired('tmux', 5), OSError('unavailable'),
                         subprocess.CompletedProcess([], 1, '', 'permission denied'),
                         subprocess.CompletedProcess([], 0, 'not-an-id', '')):
            with self.subTest(response=response), patch.object(auto_resume, '_entries') as entries:
                run = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                auto_resume.tick(entries=[], folder_of=Mock(), status_of=Mock(), display_dir=Mock(), run=run)
                self.assertEqual((self.cache / 'open-tabs.json').read_bytes(), original)
                entries.assert_not_called()

    def test_both_no_server_error_forms_mean_loss(self):
        for text in ('no server running on /Users/test/tmux', 'error connecting to /Users/test/tmux (No such file or directory)'):
            run = Mock(return_value=subprocess.CompletedProcess([], 1, '', text))
            self.assertEqual(auto_resume.server_id(run=run), ('none', None))

    def test_discovery_race_to_different_server_skips_sync_and_launch(self):
        self.tick()
        original = self.saved()
        def entries():
            self.tmux.server = '40:400'
            return []
        auto_resume.tick(entries=entries, folder_of=Mock(), status_of=Mock(), display_dir=Mock(),
                         run=self.tmux, now=lambda: self.clock, sleep=self.sleeps.append)
        self.assertEqual(self.saved(), original)
        self.assertEqual(self.sends(), [])

    def test_close_during_discovery_blocks_the_stale_result(self):
        self.tick()
        def entries():
            auto_resume.forget_pane('%1', run=self.tmux, now=lambda: self.clock)
            return [self.entry]
        auto_resume.tick(entries=entries, folder_of=lambda e: e['cwd'], status_of=lambda e: {'state': 'Idle'},
                         display_dir=lambda e: e['cwd'], run=self.tmux, now=lambda: self.clock, sleep=self.sleeps.append)
        self.assertEqual(self.saved()['tabs'], {})

    def test_stopped_overlay_real_turns_and_non_turn_records(self):
        for agent, non_turns, turn in (
                ('claude', [{'type': 'user', 'isMeta': True}, {'type': 'assistant'}, {'type': 'session-owner'}],
                 {'type': 'user', 'message': {'role': 'user', 'content': 'next'}}),
                ('codex', [{'type': 'session_meta'}, {'type': 'event_msg', 'payload': {'type': 'token_count'}},
                           {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user'}}],
                 {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'next'}})):
            with self.subTest(agent=agent):
                self.store({'version': 1, 'server': '10:100', 'tabs': {}})
                transcript = self.cache / (agent + '.jsonl')
                transcript.write_text(json.dumps(turn) + '\n')
                self.entry.update(agent=agent, transcript=str(transcript))
                self.entries = [self.entry]
                self.tmux.server = '10:100'
                self.tmux.add(self.entry)
                self.states['%1'] = 'Working'
                self.tick()
                self.restart()
                tab = self.saved()['tabs'][THREAD]
                self.assertTrue(tab['stopped'])
                self.assertEqual(tab['offset'], transcript.stat().st_size)
                working = {'state': 'Working', 'approval': None}
                self.assertEqual(auto_resume.overlay(self.entry, working)['state'], 'Stopped')
                for state in ('Approval', 'Input needed'):
                    prompt = {'state': state}
                    self.assertIs(auto_resume.overlay(self.entry, prompt), prompt)
                with transcript.open('a') as stream:
                    for record in non_turns:
                        stream.write(json.dumps(record) + '\n')
                self.assertEqual(auto_resume.overlay(self.entry, working)['state'], 'Stopped')
                with transcript.open('a') as stream:
                    stream.write(json.dumps(turn) + '\n')
                self.assertIs(auto_resume.overlay(self.entry, working), working)
                self.assertFalse(self.saved()['tabs'][THREAD]['stopped'])

    def test_killed_claude_tool_result_keeps_stopped_until_user_prompt(self):
        transcript = self.cache / 'killed-tool.jsonl'
        transcript.write_text('')
        self.entry['transcript'] = str(transcript)
        self.states['%1'] = 'Working'
        self.tick()
        self.restart()
        tab = self.saved()['tabs'][THREAD]
        self.assertTrue(tab['stopped'])
        with transcript.open('a') as stream:
            stream.write('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"Exit code 137","is_error":true,"tool_use_id":"toolu_x"}]},"sourceToolAssistantUUID":"a","toolUseResult":"Error: Exit code 137"}\n')
            stream.write(json.dumps({'type': 'attachment'}) + '\n')
            stream.write(json.dumps({'type': 'cost-state'}) + '\n')
        idle = {'state': 'Idle'}
        self.assertEqual(auto_resume.overlay(self.entry, idle)['state'], 'Stopped')
        self.assertFalse(auto_resume.new_turn(agent='claude', transcript=transcript, offset=tab['offset']))
        self.assertTrue(self.saved()['tabs'][THREAD]['stopped'])
        with transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'continue'}}) + '\n')
        self.assertTrue(auto_resume.new_turn(agent='claude', transcript=transcript, offset=tab['offset']))
        self.assertIs(auto_resume.overlay(self.entry, idle), idle)
        self.assertFalse(self.saved()['tabs'][THREAD]['stopped'])

    def test_claude_new_turn_requires_user_prompt(self):
        path = self.cache / 'prompts.jsonl'
        tool = {'type': 'tool_result', 'content': 'Exit code 137'}
        text = {'type': 'text', 'text': 'continue'}
        image = {'type': 'image', 'source': {'type': 'base64', 'data': 'aGVsbG8='}}
        cases = [
            ('string', {'message': {'content': 'continue'}}, True),
            ('blank', {'message': {'content': ' \t\n'}}, False),
            ('empty string', {'message': {'content': ''}}, False),
            ('missing message', {}, False),
            ('null message', {'message': None}, False),
            ('missing content', {'message': {}}, False),
            ('null content', {'message': {'content': None}}, False),
            ('number content', {'message': {'content': 1}}, False),
            ('object content', {'message': {'content': text}}, False),
            ('empty list', {'message': {'content': []}}, False),
            ('tool only', {'message': {'content': [tool]}}, False),
            ('multiple tools', {'message': {'content': [tool, tool]}}, False),
            ('text', {'message': {'content': [text]}}, True),
            ('image', {'message': {'content': [image]}}, True),
            ('tool and text', {'message': {'content': [tool, text]}}, True),
            ('tool and image', {'message': {'content': [tool, image]}}, True),
            ('assistant', {'type': 'assistant', 'message': {'content': 'continue'}}, False),
        ]
        for flag in ('isMeta', 'isSidechain', 'isCompactSummary', 'isVisibleInTranscriptOnly'):
            cases.append((flag, {flag: True, 'message': {'content': 'continue'}}, False))
            cases.append((flag + ' false', {flag: False, 'message': {'content': 'continue'}}, True))
        for name, fields, expected in cases:
            with self.subTest(case=name):
                path.write_text(json.dumps({'type': 'user', **fields}) + '\n')
                self.assertEqual(auto_resume.new_turn(agent='claude', transcript=path, offset=0), expected)

    def test_idle_tab_is_not_overlaid_and_cache_does_not_parse_per_call(self):
        self.tick()
        idle = {'state': 'Idle'}
        with patch.object(auto_resume, '_read', wraps=auto_resume._read) as read:
            for _ in range(5):
                self.assertIs(auto_resume.overlay(self.entry, idle), idle)
            self.assertEqual(read.call_count, 1)

    def test_stopped_survives_sync_until_a_real_turn(self):
        self.states['%1'] = 'Working'
        self.tick()
        self.restart()
        pane = self.saved()['tabs'][THREAD]['pane']
        self.entries = [dict(self.entry, id=pane)]
        self.states[pane] = 'Working'
        self.tick()
        tab = self.saved()['tabs'][THREAD]
        self.assertEqual(tab['state'], 'open')
        self.assertTrue(tab['stopped'])
        self.assertFalse(tab['working'])

    def test_stopped_survives_a_second_loss_without_a_new_turn(self):
        self.states['%1'] = 'Working'
        self.tick()
        for _ in range(2):
            self.restart()
            pane = self.saved()['tabs'][THREAD]['pane']
            self.entries = [dict(self.entry, id=pane)]
            self.states[pane] = 'Working'
            self.tick()
            tab = self.saved()['tabs'][THREAD]
            self.assertEqual(tab['state'], 'open')
            self.assertTrue(tab['stopped'])
            self.assertFalse(tab['working'])
            self.assertEqual(auto_resume.overlay(self.entries[0], {'state': 'Working'})['state'], 'Stopped')

    def test_new_turn_reads_whole_lines_only_and_caps_at_four_mib(self):
        path = self.cache / 'turns.jsonl'
        record = json.dumps({'type': 'user', 'message': {'content': 'next'}})
        path.write_text(record)
        self.assertFalse(auto_resume.new_turn(agent='claude', transcript=path, offset=0))
        path.write_text(record + '\n')
        self.assertFalse(auto_resume.new_turn(agent='claude', transcript=path, offset=2))
        self.assertFalse(auto_resume.new_turn(agent='claude', transcript=path, offset=100))
        path.write_bytes(b'x' * (4 * 1024 * 1024) + b'\n' + record.encode() + b'\n')
        self.assertFalse(auto_resume.new_turn(agent='claude', transcript=path, offset=0))
        path.write_text('bad\n[]\nnull\n' + record + '\n')
        self.assertTrue(auto_resume.new_turn(agent='claude', transcript=path, offset=0))

    def test_only_supported_writable_uuid_conversations_are_saved(self):
        self.entries = [dict(self.entry, **fields) for fields in (
            {'agent': 'antigravity'}, {'agent': 'shell'}, {'writable': False}, {'pending_conversation': True},
            {'thread': 'pending-123'}, {'thread': None}, {'label': 'tether-shells'})]
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})

    def test_order_uses_discovered_position_for_a_zoomed_pane(self):
        self.entry['position'] = '2.1'
        self.tmux.panes['%1'].update(window_index='99', pane_index='0')
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['order'], [2, 1])

    def test_dismiss_removes_failures_permanently(self):
        self.tick()
        self.isdir.return_value = False
        self.restart()
        self.assertEqual(auto_resume.dismiss(), 1)
        self.assertEqual(auto_resume.dismiss(), 0)
        self.assertEqual(auto_resume.notice()['failed'], 0)
        self.isdir.return_value = True
        self.tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.saved()['tabs'], {})

    def test_forget_mid_launch_does_not_reinsert_tab(self):
        self.tick()
        def forget(argv):
            if argv[1] == 'display-message' and argv[-1] == '#{session_id}':
                auto_resume.forget_pane(argv[argv.index('-t') + 1], run=self.tmux, now=lambda: self.clock)
        self.tmux.before = forget
        self.restart()
        self.assertEqual(self.saved()['tabs'], {})

    def test_send_failure_after_new_window_cleans_up_shell(self):
        self.tick()
        def fail(argv):
            if argv[1] == 'send-keys':
                raise subprocess.CalledProcessError(1, argv, stderr='failed')
        self.tmux.before = fail
        self.entries = []
        self.tmux.server = '30:300'
        self.tick()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(set(self.tmux.panes), {'%1'})
        self.assertEqual([call[1] for call in self.tmux.calls].count('kill-pane'), 1)

    def test_new_session_options_failure_keeps_pane_and_retry_does_not_resume_twice(self):
        self.tick()
        with patch.object(auto_resume, '_options', side_effect=OSError('options failed')):
            self.restart()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(auto_resume.notice()['tabs'][0]['error'], 'options failed')
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual([call[1] for call in self.tmux.calls].count('new-session'), 1)
        self.assertFalse(any(call[1] == 'new-window' for call in self.tmux.calls))
        tab = self.saved()['tabs'][THREAD]
        self.assertEqual(tab['state'], 'resuming')
        self.assertIn(tab['pane'], self.tmux.panes)

    def test_failed_waits_for_retry_across_another_loss(self):
        self.tick()
        saved = self.saved()
        saved['tabs'][THREAD].update(state='failed', error='failed')
        self.store(saved)
        self.restart()
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'failed')
        self.assertEqual(self.sends(), [])

        # A new server can reuse the failed tab's former pane id.
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        pane = self.saved()['tabs'][THREAD]['pane']
        self.clock += auto_resume.VERIFY_SECS
        self.tick()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.tmux.lose('30:300')
        self.tmux.add(dict(self.entry, id=pane, tmux_session='$99', label='other'))
        self.entries = [dict(self.entry, id=pane, tmux_session='$99', label='other',
                             thread='pending-other', pending_conversation=True)]
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['pane'], '')
        self.assertEqual(auto_resume.retry(), 1)
        self.tick()
        tab = self.saved()['tabs'][THREAD]
        self.assertEqual(tab['state'], 'resuming')
        self.assertNotEqual(tab['pane'], pane)
        self.assertEqual(len(self.sends()), 4)

    def test_loss_clears_panes_and_session_ids_in_all_states_and_forget_keeps_unrelated_tabs(self):
        for number in range(2, 6):
            self.add(number)
        self.tick()
        saved = self.saved()
        for tab, state in zip(saved['tabs'].values(), ('open', 'pending', 'resuming', 'failed', 'pending')):
            tab['state'] = state
        self.store(saved)
        self.tmux.lose()
        self.entries = []
        with patch.object(auto_resume, 'launch', return_value=False):
            self.tick()
        self.assertTrue(all(tab['pane'] == '' for tab in self.saved()['tabs'].values()))
        self.assertTrue(all(tab['session_id'] == '' for tab in self.saved()['tabs'].values()))
        auto_resume.forget_session('$other', run=self.tmux, now=lambda: self.clock)
        auto_resume.forget_pane('', run=self.tmux, now=lambda: self.clock)
        self.assertEqual(len(self.saved()['tabs']), 5)

    def test_idle_tick_does_not_write_and_failed_atomic_write_leaves_no_temp(self):
        self.tick()
        with patch.object(auto_resume, '_write', wraps=auto_resume._write) as write:
            self.tick()
            write.assert_not_called()
        with patch.object(auto_resume.os, 'replace', side_effect=OSError('disk')):
            auto_resume.forget_pane('%1', run=self.tmux, now=lambda: self.clock)
        self.assertEqual(list(self.cache.glob('*.tmp')), [])

    def test_forget_swallows_disk_and_subprocess_failures(self):
        self.tick()
        with patch.object(auto_resume, '_write', side_effect=OSError('disk')):
            auto_resume.forget_pane('%1', run=self.tmux)
        auto_resume.forget_session('$1', run=Mock(side_effect=subprocess.TimeoutExpired('tmux', 5)))
        self.assertEqual(self.saved()['tabs'], {})

    def test_bad_session_name_fails_without_a_command(self):
        self.entry['label'] = 'bad.name'
        self.tmux.add(self.entry)
        self.tick()
        self.restart()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.assertEqual(self.sends(), [])

    def test_snapshot_read_error_is_empty(self):
        self.tick()
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            self.assertEqual(auto_resume.notice(), {'failed': 0, 'tabs': []})

    def test_saved_row_with_no_pane_does_not_count_as_open(self):
        self.tick()
        self.tmux.lose()
        self.entries = [dict(self.entry, id='saved', label='Last chat', agent='codex', writable=False)]
        self.tick()
        self.assertTrue(any(call[1] == 'new-session' for call in self.tmux.calls))
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.saved()['tabs'][THREAD]['state'], 'resuming')

    def test_unknown_folder_keeps_the_tab_and_fails_at_resume(self):
        self.entry['cwd'] = None
        self.tick()
        self.assertEqual(self.saved()['tabs'][THREAD]['folder'], '')
        self.isdir.return_value = False
        self.restart()
        self.assertEqual(auto_resume.notice()['failed'], 1)

    def test_turning_the_preference_off_clears_the_notice_and_a_retry(self):
        self.tick()
        self.isdir.return_value = False
        self.restart()
        self.assertEqual(auto_resume.notice()['failed'], 1)
        self.prefs['autoResume'] = False
        auto_resume.retry()
        self.tick()
        self.assertEqual(self.saved()['tabs'], {})
        self.assertEqual(auto_resume.notice()['failed'], 0)

    def test_loss_off_removes_all_states(self):
        for number in range(2, 6):
            self.add(number)
        self.tick()
        saved = self.saved()
        for tab, state in zip(list(saved['tabs'].values())[1:], ('pending', 'resuming', 'pending', 'failed')):
            tab['state'] = state
        self.store(saved)
        self.prefs['autoResume'] = False
        self.restart()
        self.assertEqual(self.saved()['tabs'], {})

    def handler(self, *, method='POST', payload=None, path='/mobile/api/auto-resume', headers=None, denied=False):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = method, path
        encoded = json.dumps(payload or {}).encode()
        handler.rfile, handler.wfile = io.BytesIO(encoded), io.BytesIO()
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1',
                           'Content-Length': str(len(encoded)), **(headers or {})}
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        return handler

    def test_sessions_route_carries_the_notice_only_when_a_tab_failed(self):
        self.tick()
        def listing():
            handler = self.handler(method='GET', path='/mobile/api/sessions')
            with patch.object(server, 'sessions', return_value=[]), \
                    patch.object(server, 'settings', return_value={'thread': None}):
                server.LISTING_CACHE.update(generation=None)  # a new list on each call, not the one-second shared one
                handler.do_GET()
            return json.loads(handler.respond.call_args.args[1])
        self.assertNotIn('auto_resume', listing())
        self.isdir.return_value = False
        self.restart()
        self.assertEqual(listing()['auto_resume'], auto_resume.notice())
        self.assertEqual(listing()['auto_resume']['failed'], 1)

    def test_notice_retry_dismiss_routes_and_bad_action(self):
        self.tick()
        self.isdir.return_value = False
        self.restart()
        handler = self.handler(method='GET')
        handler.do_GET()
        self.assertEqual(handler.respond.call_args.args, (200, auto_resume.notice()))
        handler = self.handler(payload={'action': 'wrong'})
        handler.do_POST()
        self.assertEqual(handler.respond.call_args.args, (400, {'error': 'Choose retry or dismiss.'}))
        handler = self.handler(payload={'action': 'retry'})
        handler.do_POST()
        self.assertEqual(handler.respond.call_args.args, (200, {'count': 1}))
        saved = self.saved()
        saved['tabs'][THREAD]['state'] = 'failed'
        self.store(saved)
        handler = self.handler(payload={'action': 'dismiss'})
        handler.do_POST()
        self.assertEqual(handler.respond.call_args.args, (200, {'count': 1}))

    def test_routes_keep_auth_origin_and_write_header_checks(self):
        with patch.object(server.auto_resume, 'retry') as retry, patch.object(server.auto_resume, 'notice') as notice:
            handler = self.handler(method='GET', denied=True)
            handler.do_GET()
            notice.assert_not_called()
            handler = self.handler(payload={'action': 'retry'}, denied=True)
            handler.do_POST()
            retry.assert_not_called()
            for headers in ({'Origin': 'http://evil.test'}, {'X-Codex-Mobile': ''}):
                handler = self.handler(payload={'action': 'retry'}, headers=headers)
                handler.do_POST()
                self.assertEqual(handler.respond.call_args.args[0], 403)
                retry.assert_not_called()

    def test_successful_post_invalidates_session_cache(self):
        handler = self.handler()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch.object(server, 'forget_sessions') as forget:
            server.Handler.respond(handler, 200, {'count': 1})
        forget.assert_called_once_with()

    def test_delete_forgets_pane_and_session_before_kill(self):
        for path, payload, pane in (('/mobile/api/session', {'pane': '%1'}, True),
                                    ('/mobile/api/tmux-sessions', {'session': '$1'}, False)):
            with self.subTest(path=path):
                self.entries = [self.entry]
                self.tmux.add(self.entry)
                self.tick()
                def before(argv):
                    if argv[1] in ('kill-pane', 'kill-session'):
                        self.assertEqual(self.saved()['tabs'], {})
                self.tmux.before = before
                handler = self.handler(method='DELETE', path=path, payload=payload)
                with patch.object(server.subprocess, 'run', self.tmux):
                    handler.do_DELETE()
                self.assertEqual(handler.respond.call_args.args[0], 200)
                self.tmux.before = lambda argv: None
                auto_resume._tombstones.clear()

    def test_shared_status_applies_overlay_only_to_fresh_status_and_chat_agrees(self):
        self.states['%1'] = 'Working'
        self.tick()
        self.restart()
        with patch.object(server, 'STATUS_CACHE', {}), \
                patch.object(server, 'live_status', return_value={'state': 'Working'}) as status, \
                patch.object(server, 'track'):
            self.assertEqual(server.shared_status(self.entry)['state'], 'Stopped')
            self.assertEqual(server.shared_status(self.entry)['state'], 'Stopped')
            status.assert_called_once_with(self.entry)
        with patch.object(server, 'live_status', return_value={'state': 'Working'}), \
                patch.object(server, 'snapshot', return_value={}), \
                patch.object(server.continuations, 'decorate', side_effect=lambda entry, data: data):
            self.assertEqual(server.chat_payload(self.entry)['state'], 'Stopped')
