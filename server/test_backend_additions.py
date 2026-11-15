"""Backend routes use in-memory requests and mocked tmux, never live panes."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.parse import urlencode

import history
import chat_hidden
import paths
import server
import terminal_input

CHAT = '12345678-1234-1234-1234-123456789abc'
OTHER = '87654321-1234-1234-1234-123456789abc'


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.enterContext(patch.object(server.prefs_sync, 'FILE', self.root / 'prefs.json'))
        self.project = self.root / 'projects' / 'project'
        self.project.mkdir(parents=True)
        self.patch = patch.object(paths, 'CLAUDE', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.enterContext(patch.object(paths, 'CODEX', self.root / 'codex'))
        self.enterContext(patch.object(paths, 'HOME', self.root))
        history._previews.clear()
        self.addCleanup(history._previews.clear)
        self.run = self.enterContext(patch.object(server.subprocess, 'run'))
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout='%13\n', stderr='')
        self.entries = self.enterContext(patch.object(server, 'sessions', return_value=[{'id': '%12', 'thread': CHAT, 'writable': True}]))
        self.verify = self.enterContext(patch.object(server, 'verify'))

    def transcript(self, records, identifier=CHAT):
        path = self.project / (identifier + '.jsonl')
        path.write_text('\n'.join(json.dumps(record) for record in records) + '\n')
        return path

    def codex_database(self, rows, columns=None, version=5):
        paths.CODEX.mkdir(exist_ok=True)
        database = paths.CODEX / f'state_{version}.sqlite'
        columns = columns or {'id': 'TEXT', 'name': 'TEXT', 'title': 'TEXT', 'first_user_message': 'TEXT',
                              'cwd': 'TEXT', 'updated_at_ms': 'INT', 'archived': 'INT',
                              'source': 'TEXT', 'has_user_event': 'INT'}
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE threads (' + ', '.join(f'{key} {kind}' for key, kind in columns.items()) + ')')
            for row in rows:
                values = dict(id=CHAT, title='Codex chat', cwd=str(self.root), updated_at_ms=1000, archived=0, source='cli', has_user_event=0)
                values.update(row)
                connection.execute('INSERT INTO threads VALUES (' + ','.join('?' for _ in columns) + ')',
                                   [values.get(key) for key in columns])
        return database

    def request(self, route, body=None, method='POST', headers=None, denied=False):
        handler = object.__new__(server.Handler)
        handler.path = route
        handler.command = method
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload)
        handler.headers = {'Host': 'phone.test', 'Origin': 'http://phone.test',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(payload))}
        if headers:
            handler.headers.update(headers)
        handler.respond = Mock()
        handler.deny = Mock(return_value=denied)
        getattr(handler, 'do_' + method)()
        return handler.respond.call_args.args if handler.respond.called else None

    def test_control_c_uses_existing_guards_and_tmux_key(self):
        self.assertEqual(self.request('/mobile/api/key', {'session': '%12', 'thread': CHAT, 'key': 'C-c'})[0], 200)
        self.verify.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], [paths.TMUX, 'send-keys', '-t', '%12', 'C-c'])
        self.assertEqual(self.request('/mobile/api/key', {'session': '%12', 'key': 'C-c'})[0], 409)
        self.assertEqual(self.request('/mobile/api/key', {'session': '%12', 'thread': CHAT, 'key': 'C-z'})[0], 400)

    def test_stop_escape_returns_restored_text_under_send_lock(self):
        import terminal_input as terminal
        entry = {'id': '%12', 'agent': 'claude', 'thread': CHAT, 'writable': True}
        self.entries.return_value = [entry]
        body = {'session': '%12', 'thread': CHAT, 'key': 'Escape', 'stop': True}

        def stop(config):
            self.assertIs(config, entry)
            self.assertTrue(server.SEND_LOCK.locked())
            self.verify.assert_called_once_with(entry)
            return {'restored': 'write about rivers'}

        with patch.object(terminal, 'stop_turn', side_effect=stop) as restore:
            self.assertEqual(self.request('/mobile/api/key', body),
                             (200, {'accepted': True, 'restored': 'write about rivers'}))
            restore.assert_called_once_with(entry)
            self.run.assert_not_called()
            self.assertEqual(self.request('/mobile/api/key', {**body, 'thread': OTHER})[0], 409)
            self.assertEqual(self.request('/mobile/api/key', body, headers={'X-Codex-Mobile': ''})[0], 403)
            self.assertEqual(restore.call_count, 1)

    def test_stop_restoration_requires_claude_escape_and_literal_true(self):
        import terminal_input as terminal
        entry = {'id': '%12', 'agent': 'claude', 'thread': CHAT, 'writable': True}
        self.entries.return_value = [entry]
        with patch.object(terminal, 'stop_turn') as restore, \
                patch.object(terminal, 'drop_double_escape', return_value=False):
            for agent, key, flag in (('claude', 'Escape', 1), ('claude', 'Escape', False),
                                     ('claude', 'C-c', True), ('codex', 'Escape', True),
                                     ('antigravity', 'Escape', True)):
                entry['agent'] = agent
                body = {'session': '%12', 'thread': CHAT, 'key': key, 'stop': flag}
                self.assertEqual(self.request('/mobile/api/key', body), (200, {'accepted': True}))
                self.assertEqual(self.run.call_args.args[0][-1], key)
            restore.assert_not_called()

    def test_double_escape_guard_uses_a_fake_clock_and_keeps_response_contract(self):
        import terminal_input as terminal
        entry = {'id': '%esc-guard', 'agent': 'claude', 'thread': CHAT, 'writable': True}
        self.entries.return_value = [entry]
        body = {'session': entry['id'], 'thread': CHAT, 'key': 'Escape'}
        screen = ['──────────\n❯ \n──────────\n']
        clock = [100.0]
        with patch.dict(terminal._escape_seen, clear=True), \
                patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(terminal, 'capture', side_effect=lambda *args, **kwargs: screen[0]), \
                patch('builtins.print') as log:
            def press():
                self.run.reset_mock()
                self.assertEqual(self.request('/mobile/api/key', body), (200, {'accepted': True}))
                return [call.args[0][-1] for call in self.run.call_args_list if 'send-keys' in call.args[0]]

            self.assertEqual(press(), ['Escape'])
            clock[0] += .5
            self.assertEqual(press(), [])
            log.assert_called_once()
            self.assertIn('Escape', log.call_args.args[0])
            clock[0] += 1.0
            self.assertEqual(press(), ['Escape'])
            clock[0] += .2
            screen[0] = '──────────\n❯ Keep this draft\n──────────\n'
            self.assertEqual(press(), ['Escape'])
            clock[0] += .2
            screen[0] = (Path(__file__).parent / 'test_fixtures/pickers/claude_rewind_150x39.txt').read_text()
            self.assertEqual(press(), ['Escape'])  # Esc must still cancel an open picker.
            clock[0] += .2
            screen[0] = 'No input box on screen'
            self.assertEqual(press(), ['Escape'])
            clock[0] += .2
            screen[0] = '──────────\n❯ \n──────────\n'
            entry['agent'] = 'codex'
            self.assertEqual(press(), ['Escape'])
            log.assert_called_once()

    def test_chat_folder_validates_and_resolves(self):
        self.assertEqual(server.chat_folder('~'), str(self.root.resolve()))
        self.assertEqual(server.chat_folder('~/projects/project'), str(self.project.resolve()))
        link = self.root / 'link'
        link.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(server.chat_folder(str(link)), str(self.project.resolve()))
        file = self.root / 'file'
        file.touch()
        for value in (None, 7, '', 'projects/project', '~someone', '~/missing',
                      str(file), '/' + 'a' * 1024, '~/projects/\nproject',
                      '~/projects/\x00project', '~/projects/\x7fproject', '~/projects/\x85project'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^Folder not found[.]$'):
                server.chat_folder(value)

    def test_split_passes_explicit_cwd(self):
        with patch.object(server, 'new_window_after', return_value='%13') as new_window:
            self.assertEqual(self.request('/mobile/api/split', {
                'session': '%12', 'direction': 'right', 'cwd': '~/projects/project',
            }), (201, {'pane': '%13'}))
        new_window.assert_called_once_with('%12', self.run, cwd=str(self.project.resolve()), group=None)

    def test_tmux_session_passes_explicit_cwd(self):
        with patch.object(server, 'create_session', return_value='%13') as create:
            status, _ = self.request('/mobile/api/tmux-sessions', {
                'name': 'Friday chats', 'cwd': '~/projects/project',
            })
        self.assertEqual(status, 201)
        create.assert_called_once_with('Friday chats', str(self.project.resolve()))

    def test_tmux_session_without_cwd_keeps_session_folder(self):
        with patch.object(server, 'create_session', return_value='%13') as create:
            with patch.object(server.prefs_sync, 'session_folder', return_value=str(self.project)):
                status, _ = self.request('/mobile/api/tmux-sessions', {'name': 'Friday chats'})
        self.assertEqual(status, 201)
        create.assert_called_once_with('Friday chats', str(self.project))

    def test_cwd_routes_reject_invalid_folders_before_tmux(self):
        for route, body in (('/mobile/api/split', {'session': '%12', 'direction': 'right'}),
                            ('/mobile/api/tmux-sessions', {'name': 'Friday chats'})):
            for cwd in (None, 'relative', '~/missing', '~\n'):
                with self.subTest(route=route, cwd=cwd):
                    self.run.reset_mock()
                    self.assertEqual(self.request(route, {**body, 'cwd': cwd}),
                                     (400, {'error': 'Folder not found.'}))
                    self.run.assert_not_called()
        self.assertEqual(self.request('/mobile/api/tmux-sessions', {
            'name': 'Friday chats', 'cwd': '~', 'worktree': 'branch',
        }), (400, {'error': 'Use cwd or worktree, not both.'}))
        self.run.assert_not_called()

    def test_split_directions_do_not_require_thread_or_writable(self):
        self.entries.return_value = [{'id': '%12', 'thread': CHAT, 'writable': False}]
        for direction in ('right', 'down'):
            self.run.reset_mock()
            def run(command, **kwargs):
                self.assertTrue(server.SEND_LOCK.locked())
                output = ('@7\t/a folder\n' if '#{window_id}\t#{pane_current_path}\t#{@group}' in command
                          else 'manual\t160\t48\n' if '#{window-size}\t#{window_width}\t#{window_height}' in command
                          else '%13\n')
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
            self.run.side_effect = run
            self.assertEqual(self.request('/mobile/api/split', {'session': '%12', 'direction': direction}), (201, {'pane': '%13'}))
            self.assertEqual([call.args[0] for call in self.run.call_args_list], [
                [paths.TMUX, 'display-message', '-p', '-t', '%12', '#{window_id}\t#{pane_current_path}\t#{@group}'],
                [paths.TMUX, 'display-message', '-p', '-t', '@7', '#{window-size}\t#{window_width}\t#{window_height}'],
                [paths.TMUX, 'new-window', '-a', '-t', '@7', '-c', '/a folder', '-P', '-F', '#{pane_id}'],
                [paths.TMUX, 'resize-window', '-t', '%13', '-x', '160', '-y', '48'],
            ])
        self.verify.assert_not_called()

    def test_pane_name_sets_and_clears_a_tmux_pane_option(self):
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': ' build logs '}), (200, {'pane': '%12', 'name': 'build logs'}))
        command = self.run.call_args_list[0].args[0]
        self.assertEqual(command[:7], [paths.TMUX, 'set-option', '-p', '-t', '%12', '@chat_name', 'build logs'])
        # The pane is alone in its tmux session, so the session takes the same name.
        self.assertEqual(self.run.call_args.args[0], [paths.TMUX, 'rename-session', '-t', '%12', 'build logs'])
        self.assertEqual(command[7:13], [';', 'set-option', '-p', '-t', '%12', '@chat_name_at'])
        self.assertTrue(command[13].isdigit())
        # The user named it: Tether never renames this tab (no AI name either), and the name names its stack (auto_stacks).
        self.assertEqual(command[14:], [';', 'set-option', '-p', '-t', '%12', '@chat_name_by', 'user', ';', 'set-option', '-p', '-t', '%12', '-u', '@chat_name_auto'])
        self.run.reset_mock()
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': ''})[0], 200)
        self.assertEqual(self.run.call_args_list[0].args[0], [paths.TMUX, 'set-option', '-p', '-t', '%12', '-u', '@chat_name', ';',
                                                        'set-option', '-p', '-t', '%12', '-u', '@chat_name_at', ';',
                                                        'set-option', '-p', '-t', '%12', '@chat_name_by', 'cleared', ';', 'set-option', '-p', '-t', '%12', '-u', '@chat_name_auto'])
        self.run.reset_mock()
        for body in ({'pane': '$1', 'name': 'x'}, {'pane': '%12', 'name': 'x' * 41}, {'pane': '%12', 'name': 'a\nb'}, {'pane': '%12'}):
            self.assertEqual(self.request('/mobile/api/pane-name', body)[0], 400)
        self.run.assert_not_called()

    def test_pane_icon_sets_and_clears_a_tmux_pane_option(self):
        self.run.reset_mock()
        status, _ = self.request('/mobile/api/pane-icon', {'pane': '%3', 'icon': 'bug'})
        self.assertEqual(status, 200)
        command = self.run.call_args_list[0].args[0]
        self.assertEqual(command[1:7], ['set-option', '-p', '-t', '%3', '@chat_icon', 'bug'])
        self.assertIn('@chat_icon_by', command)
        self.run.reset_mock()
        status, _ = self.request('/mobile/api/pane-icon', {'pane': '%3', 'icon': ''})
        self.assertEqual(status, 200)
        self.assertIn('-u', self.run.call_args_list[0].args[0])
        status, _ = self.request('/mobile/api/pane-icon', {'pane': '%3', 'icon': 'Bad Icon!'})
        self.assertEqual(status, 400)

    def test_claude_draws_a_pane_icon_and_the_ui_can_load_it(self):
        with patch.object(server.pane_icons, 'draw', return_value='dabc') as draw:
            self.run.reset_mock()
            self.assertEqual(self.request('/mobile/api/pane-icon/draw', {'pane': '%3', 'label': 'api', 'hint': 'a plug'}), (200, {'pane': '%3', 'icon': 'svg-dabc'}))
            draw.assert_called_once_with('api', 'a plug')
            self.assertIn('svg-dabc', self.run.call_args_list[0].args[0])
        with patch.object(server.pane_icons, 'draw', side_effect=RuntimeError('Claude did not return a usable icon. Try again.')):
            self.assertEqual(self.request('/mobile/api/pane-icon/draw', {'pane': '%3', 'label': 'api'})[:2], (503, {'error': 'Claude did not return a usable icon. Try again.', 'draft': False}))
        with patch.object(server.pane_icons, 'read', return_value='<svg/>'):
            self.assertEqual(self.request('/mobile/api/icon/dabc.svg', method='GET'), (200, '<svg/>', 'image/svg+xml', 'max-age=300'))
        with patch.object(server.pane_icons, 'read', return_value=None):
            self.assertEqual(self.request('/mobile/api/icon/nope.svg', method='GET')[0], 404)

    def test_pane_name_shows_in_tmux_window_or_border(self):
        def panes(count):
            def run(command, **kwargs):
                return Mock(returncode=0, stdout=f'{count}\n' if '#{window_panes}' in command else '')
            return run
        commands = lambda: [call.args[0][1:] for call in self.run.call_args_list]
        self.run.side_effect = panes(1)
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': 'api'})[0], 200)
        self.assertIn(['rename-window', '-t', '%12', 'api', ';', 'set-option', '-w', '-t', '%12', 'automatic-rename', 'off'], commands())
        self.run.reset_mock()
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': ''})[0], 200)
        self.assertIn(['set-option', '-w', '-t', '%12', '-u', 'automatic-rename'], commands())
        self.run.reset_mock()
        self.run.side_effect = panes(2)
        self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': 'api'})[0], 200)
        self.assertIn(['set-option', '-w', '-t', '%12', 'pane-border-status', 'top', ';', 'set-option', '-w', '-t', '%12',
                       'pane-border-format', server.BORDER_FORMAT], commands())
        self.assertFalse(any(command[0] == 'rename-window' for command in commands()))  # several panes: the window keeps its name

    def test_one_name_per_pane(self):
        claude = {'id': '%12', 'tmux_session': '$1', 'label': 'web:0.0', 'agent': 'claude', 'writable': True, 'thread': CHAT}
        self.entries.return_value = [claude, {'id': '%13', 'tmux_session': '$1', 'label': 'web:0.1', 'agent': 'shell'}]
        with patch.object(server, 'rename_agent', return_value='') as agent:
            self.assertEqual(self.request('/mobile/api/pane-name', {'pane': '%12', 'name': 'api'}), (200, {'pane': '%12', 'name': 'api'}))
            agent.assert_called_once_with(claude, 'api')
            self.assertFalse(any(call.args[0][1] == 'rename-session' for call in self.run.call_args_list))  # two panes: the session keeps its name
            agent.reset_mock()
            self.run.reset_mock()
            self.assertEqual(self.request('/mobile/api/rename', {'pane': '%12', 'name': 'shop'}), (200, {'session': '%12', 'name': 'shop'}))
            agent.assert_not_called()  # a session name with two panes is not the agent's name
            self.entries.return_value = [claude]
            agent.return_value = 'Claude is busy, so its own name did not change. Rename again when it is idle.'
            self.run.reset_mock()
            status, body = self.request('/mobile/api/rename', {'pane': '%12', 'name': 'shop'})
            self.assertEqual((status, body['note'][:15]), (200, 'Claude is busy,'))
            agent.assert_called_once_with(claude, 'shop')
            self.assertEqual(self.run.call_args.args[0][1:7], ['set-option', '-p', '-t', '%12', '@chat_name', 'shop'])

    def test_usage_summary_reads_claude_and_codex_reports(self):
        import agents
        metrics = self.root / 'usage'
        metrics.mkdir()
        (metrics / 'a.json').write_text(json.dumps({'reported_at': 50, 'rate_limits': {
            'five_hour': {'used_percentage': 12, 'resets_at': 200}, 'seven_day': {'used_percentage': 40, 'resets_at': 50}}}))
        rates = self.root / 'codex_rates.json'
        rates.write_text(json.dumps({'windows': [{'label': '168h', 'percent': 48.0, 'resets_at': 300}], 'reported_at': 'T'}))
        with patch.object(agents, 'METRICS', metrics), patch.object(server, 'RATES_FILE', rates), \
                patch.object(server.saved_accounts, 'switched_at', return_value=0):  # not this machine's account switches
            report = server.usage_summary(now=100)
        self.assertEqual(report['claude'], {'reported_at': 50, 'windows': [
            {'label': '5h', 'percent': 12, 'resets_at': 200}, {'label': '7d', 'percent': 0, 'resets_at': None}]})
        self.assertEqual(report['codex'], {'reported_at': 'T', 'windows': [{'label': '7d', 'percent': 48.0, 'resets_at': 300}]})
        with patch.object(agents, 'METRICS', self.root / 'none'), patch.object(server, 'RATES_FILE', self.root / 'none.json'), \
                patch.object(server, 'recent_rates', return_value=([], None)):
            empty = server.usage_summary(now=100)
        self.assertEqual((empty['claude'], empty['codex']), (None, None))

    def test_split_rejects_bad_input_and_reports_tmux_errors(self):
        for body in ({}, {'session': '%12'}, {'session': '%12', 'direction': 'left'}, {'session': '$1', 'direction': 'right'},
                     {'session': '%99', 'direction': 'right'}, {'session': 12, 'direction': 'down'}):
            self.assertEqual(self.request('/mobile/api/split', body)[0], 400)
        self.run.assert_not_called()
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='no space for new pane')
        status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})
        self.assertEqual((status, body['error']), (503, 'no space for new pane'))
        self.run.side_effect = subprocess.TimeoutExpired('tmux', 5)
        self.assertEqual(self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})[0], 503)

    def test_split_keeps_fallback_error_and_pane_confirmation(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='')
        status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'right'})
        self.assertEqual((status, body['error']), (503, 'Could not split the pane.'))
        for output in ('', '@13\n', '%13\n%14\n'):
            with self.subTest(output=output):
                self.run.side_effect = [
                    subprocess.CompletedProcess([], 0, stdout='@7\t/work\n', stderr=''),
                    subprocess.CompletedProcess([], 0, stdout='latest\t120\t40\n', stderr=''),
                    subprocess.CompletedProcess([], 0, stdout=output, stderr=''),
                ]
                status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'right'})
                self.assertEqual((status, body['error']), (503, 'Pane creation could not be confirmed.'))

    def test_split_rejects_unconfirmed_source_window(self):
        for output in ('', '%12\t/work\n', '@7', '@7x\t/work\n', '@7\t\n'):
            with self.subTest(output=output):
                self.run.reset_mock()
                self.run.return_value = subprocess.CompletedProcess([], 0, stdout=output, stderr='')
                status, body = self.request('/mobile/api/split', {'session': '%12', 'direction': 'down'})
                self.assertEqual((status, body['error']), (503, 'Could not split the pane.'))
                self.assertEqual(self.run.call_count, 1)

    def test_new_post_routes_use_auth_origin_and_header_guards(self):
        for route in ('/mobile/api/split', '/mobile/api/resume'):
            self.assertIsNone(self.request(route, {}, denied=True))
            for headers in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                self.assertEqual(self.request(route, {}, headers=headers)[0], 403)
        self.run.assert_not_called()
        self.entries.assert_not_called()

    def test_history_titles_cwd_live_order_and_limit(self):
        self.enterContext(patch.object(server.Path, 'home', return_value=Path('/Users/test')))
        older = self.transcript([{'type': 'user', 'cwd': '/Users/test', 'message': {'content': 'first'}},
                                 {'type': 'custom-title', 'customTitle': 'Old'},
                                 {'type': 'summary', 'summary': 'Summary'},
                                 {'type': 'ai-title', 'aiTitle': 'Newest'}])
        os.utime(older, (1, 1))
        self.transcript([{'type': 'user', 'message': {'content': '<command-name>ignore</command-name>\n hello   world'}}], OTHER)
        status, body = self.request('/mobile/api/history', method='GET')
        self.assertEqual(status, 200)
        chats = body['chats']
        self.assertEqual([chat['id'] for chat in chats], [OTHER, CHAT])
        self.assertEqual(chats[0]['title'], 'hello world')
        self.assertIsNone(chats[0]['dir'])
        self.assertFalse(chats[0]['live'])
        self.assertEqual((chats[1]['title'], chats[1]['dir'], chats[1]['live']), ('Newest', '~', True))
        self.assertTrue(chats[1]['time'].endswith('+00:00'))
        self.assertEqual(len(self.request('/mobile/api/history?limit=1', method='GET')[1]['chats']), 1)
        for limit in ('0', '101', 'nope'):
            self.assertEqual(self.request('/mobile/api/history?limit=' + limit, method='GET')[0], 400)
        self.assertIsNone(self.request('/mobile/api/history', method='GET', denied=True))

    def test_history_bounded_reads_cache_and_invalidation(self):
        path = self.transcript([{'type': 'user', 'cwd': '/old', 'message': {'content': 'First'}}])
        with path.open('ab') as stream:
            stream.write(b'x' * 200000 + b'\n{bad json}\nnull\n{"type": []}\n')
            stream.write(json.dumps({'type': 'custom-title', 'customTitle': 'Tail title', 'cwd': '/new'}).encode() + b'\n')
        with patch('builtins.open', wraps=open) as opened:
            self.assertEqual(history.recent_chats()[0]['title'], 'Tail title')
            self.assertEqual(history.recent_chats()[0]['cwd'], '/new')
            opened.assert_called_once()
        with patch('builtins.open', side_effect=AssertionError('cached files must not be read')):
            self.assertEqual(history.recent_chats()[0]['title'], 'Tail title')
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'ai-title', 'aiTitle': 'Updated'}) + '\n')
        self.assertEqual(history.find_chat(CHAT)['title'], 'Updated')
        # Enforce byte bounds, including on large files.
        real_open = open
        reads = []
        class Reader:
            def __init__(self, file):
                self.file = file
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def seek(self, *args):
                return self.file.seek(*args)
            def read(self, size):
                reads.append(size)
                return self.file.read(size)
        history._previews.clear()
        with patch('builtins.open', side_effect=lambda *args: Reader(real_open(*args))):
            history.recent_chats()
        self.assertEqual(reads, [65536, 65536])

    def test_history_skips_subfolders_and_non_chat_files(self):
        sub = self.project / 'subagents'
        sub.mkdir()
        (sub / (CHAT + '.jsonl')).write_text('{}\n')
        (self.project / 'notes.jsonl').write_text('{}\n')
        self.assertEqual(history.recent_chats(), [])
        self.assertIsNone(history.find_chat(CHAT))
        path = self.transcript([{'type': 'user', 'isMeta': True, 'message': {'content': 'Skip'}},
                                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'Skip'}]}},
                                {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'word ' * 30}]}}])
        self.assertEqual(len(history.recent_chats()[0]['title']), 80)
        path.unlink()
        self.assertIsNone(history.find_chat(CHAT))

    def test_resume_uses_transcript_cwd_literal_text_enter_and_session_id(self):
        self.entries.return_value = []
        self.transcript([{'cwd': str(self.root)}])
        self.run.side_effect = [subprocess.CompletedProcess([], 0, stdout='%13\n', stderr=''),
                                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                                subprocess.CompletedProcess([], 0, stdout='$7\n')]
        self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'past-chat'}),
                         (201, {'session': '$7', 'pane': '%13', 'name': 'past-chat'}))
        commands = [call.args[0] for call in self.run.call_args_list]
        self.assertEqual(commands, [[paths.TMUX, 'new-session', '-d', '-s', 'past-chat', '-c', str(self.root), '-P', '-F', '#{pane_id}'],
                                    [paths.TMUX, 'send-keys', '-t', '%13', '-l', 'claude --resume ' + CHAT],
                                    [paths.TMUX, 'send-keys', '-t', '%13', 'Enter'],
                                    [paths.TMUX, 'display-message', '-p', '-t', '%13', '#{session_id}']])
        self.entries.assert_called_once()

    def test_resume_validation_fallback_duplicate_and_failures(self):
        self.entries.return_value = []
        for identifier in ('../bad', '', None, 123):
            self.assertEqual(self.request('/mobile/api/resume', {'id': identifier, 'name': 'ok'})[0], 400)
        self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'ok'})[0], 404)
        self.transcript([{'cwd': str(self.root / 'missing')}])
        for name in ('../bad', '-bad', '', 'x' * 65, None):
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': name})[0], 400)
        self.run.assert_not_called()
        for error, status in [('duplicate session: ok', 409), ('no server running', 503)]:
            self.run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr=error)
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'ok'})[0], status)
            self.assertEqual(self.run.call_args.args[0][6], str(paths.HOME))
        self.run.side_effect = subprocess.TimeoutExpired('tmux', 5)
        self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'name': 'ok'})[0], 503)

    def test_codex_history_interactive_rows_titles_and_search_text(self):
        ids = [f'00000000-0000-0000-0000-{number:012x}' for number in range(10)]
        self.codex_database([
            {'id': ids[0], 'name': '  My\n name  ', 'title': 'ignored', 'first_user_message': 'search ' * 60},
            {'id': ids[1], 'source': 'vscode', 'name': ' ', 'title': ' A\t title '},
            {'id': ids[2], 'title': None, 'first_user_message': ' first\n user ' + 'x' * 90},
            {'id': ids[3], 'title': 'Plain title'},
            {'id': ids[4], 'source': 'exec'},
            {'id': ids[5], 'source': 'subagent'},
            {'id': ids[6], 'source': '{"subagent":{"thread_spawn":{}}}'},
            {'id': ids[7], 'archived': 1},
            {'id': ids[8], 'title': None},  # an empty thread has no text at all
            {'id': ids[9], 'source': 'other'},
        ])
        self.transcript([{'type': 'user', 'message': {'content': 'Claude text'}}])
        chats = history.all_chats()
        codex = [chat for chat in chats if chat['agent'] == 'codex']
        self.assertEqual([chat['id'] for chat in codex], ids[:4])
        self.assertEqual([chat['title'] for chat in codex], ['My name', 'A title', ('first user ' + 'x' * 90)[:80], 'Plain title'])
        self.assertEqual(codex[0]['text'], ('search ' * 60)[:300])
        self.assertEqual(codex[0]['time'], '1970-01-01T00:00:01+00:00')
        self.assertEqual(codex[0]['cwd'], str(self.root))
        self.assertEqual(history.find_chat(ids[0], agent='codex'), codex[0])
        for identifier in ids[4:]:
            self.assertIsNone(history.find_chat(identifier, agent='codex'))
        self.assertEqual(chats[0]['agent'], 'claude')
        self.assertEqual(chats[0]['text'], 'Claude text')
        self.run.assert_not_called()

    def test_codex_history_missing_unreadable_and_older_schemas(self):
        self.assertEqual(history.all_chats(), [])
        self.assertIsNone(history.find_chat(CHAT, agent='codex'))
        columns = {'id': 'TEXT', 'cwd': 'TEXT', 'title': 'TEXT', 'updated_at_ms': 'INT',
                   'archived': 'INT', 'source': 'TEXT'}
        self.codex_database([{'title': 'Old title'}, {'id': OTHER, 'source': 'exec'}], columns, version=9)
        self.assertEqual([chat['title'] for chat in history.all_chats()], ['Old title'])
        # Choose the newest schema number, as agents.codex_database does.
        self.codex_database([{'name': 'New title'}], version=10)
        self.assertEqual(history.find_chat(CHAT, agent='codex')['title'], 'New title')
        self.codex_database([{}], {'id': 'TEXT', 'cwd': 'TEXT'}, version=11)
        chat = history.find_chat(CHAT, agent='codex')
        self.assertEqual((chat['title'], chat['text'], chat['time']), (CHAT, '', None))
        self.codex_database([{}], {'cwd': 'TEXT'}, version=12)
        self.assertEqual(history.all_chats(), [])
        (paths.CODEX / 'state_13.sqlite').write_bytes(b'not a database')
        self.assertEqual(history.all_chats(), [])
        self.assertIsNone(history.find_chat(CHAT, agent='codex'))
        with patch.object(history.sqlite3, 'connect', side_effect=sqlite3.OperationalError('unreadable')):
            self.assertEqual(history.all_chats(), [])

    def test_all_claude_chats_and_long_first_user_text(self):
        for number in range(35):
            identifier = f'00000000-0000-0000-0000-{number:012x}'
            self.transcript([{'type': 'user', 'isSidechain': True, 'message': {'content': 'skip'}},
                             {'type': 'user', 'message': {'content': 'a' * 90 + ' hidden ' + 'b' * 240}}], identifier)
        self.assertEqual(len(history.all_chats()), 35)
        self.assertEqual(len(history.recent_chats()), 30)
        chat = history.all_chats()[0]
        self.assertEqual(chat['title'], 'a' * 80)
        self.assertEqual(len(chat['text']), 300)
        self.assertIn('hidden', chat['text'])
        self.assertEqual(len(history.search_chats(history.all_chats(), q='hidden')), 35)

    def test_search_chats_terms_agent_state_and_displayed_directory(self):
        chats = [dict(id=CHAT, agent='claude', title='Build API', text='Hidden NEEDLE', cwd='/Users/test/app', time='2026-01-01'),
                 dict(id=OTHER, agent='codex', title='Build tests', text='needle', cwd='/Users/test/other', time='2026-02-01'),
                 dict(id='third', agent='codex', title='API', text='', cwd=None, time=None)]
        self.assertEqual(history.search_chats(chats), [chats[1], chats[0], chats[2]])
        self.assertEqual(history.search_chats(chats, q='  BUILD\tneedle APP '), [chats[0]])
        self.assertEqual(history.search_chats(chats, q='build absent'), [])
        self.assertEqual(history.search_chats(chats, agent='codex'), [chats[1], chats[2]])
        self.assertEqual(history.search_chats(chats, state='open', live={OTHER}), [chats[1]])
        self.assertEqual(history.search_chats(chats, state='closed', live={OTHER}), [chats[0], chats[2]])
        self.assertEqual(history.search_chats(chats, dir='~/app', display_dir=lambda cwd: server.home_path(cwd)), [])
        with patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            self.assertEqual(history.search_chats(chats, dir='~/app', display_dir=server.home_path), [chats[0]])

    def test_history_route_paging_counts_and_directory_filters(self):
        chats = [dict(id=CHAT, agent='claude', title='API one', text='secret', cwd='/Users/test/a', time='2026-04-01'),
                 dict(id=OTHER, agent='codex', title='API two', text='secret', cwd='/Users/test/a', time='2026-03-01'),
                 dict(id='third', agent='codex', title='API three', text='secret', cwd='/Users/test/b', time='2026-02-01'),
                 dict(id='fourth', agent='claude', title='API four', text='secret', cwd=None, time='2026-01-01'),
                 dict(id='fifth', agent='claude', title='Unrelated', text='', cwd='/Users/test/z', time='2026-05-01')]
        with patch.object(history, 'all_chats', return_value=chats), \
                patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            def get(**query):
                status, body = self.request('/mobile/api/history?' + urlencode(query), method='GET')
                self.assertEqual(status, 200)
                return body
            body = get(q='api', limit=2)
            self.assertEqual((body['total'], body['next']), (4, 2))
            self.assertEqual(body['counts'], {'all': 4, 'claude': 2, 'codex': 2})
            self.assertEqual(body['dirs'], [{'dir': '~/a', 'count': 2, 'temporary': False}, {'dir': '~/b', 'count': 1, 'temporary': False}])
            self.assertEqual(body['chats'][0], dict(id=CHAT, agent='claude', title='API one', dir='~/a',
                                                  time='2026-04-01', live=True, pane='%12'))
            self.assertIsNone(body['chats'][1]['pane'])
            self.assertFalse(body['chats'][1]['live'])
            self.assertTrue(all(set(chat) == {'id', 'agent', 'title', 'dir', 'time', 'live', 'pane'} for chat in body['chats']))
            page = get(q='api', limit=2, offset=body['next'])
            self.assertEqual((page['total'], page['next']), (4, None))
            self.assertEqual([chat['id'] for chat in page['chats']], ['third', 'fourth'])
            self.assertEqual(get(offset=99)['chats'], [])
            body = get(q='api', agent='codex', dir='~/b')
            self.assertEqual([chat['id'] for chat in body['chats']], ['third'])
            self.assertEqual(body['counts'], {'all': 1, 'claude': 0, 'codex': 1})
            self.assertEqual(body['dirs'], [{'dir': '~/a', 'count': 1, 'temporary': False}, {'dir': '~/b', 'count': 1, 'temporary': False}])
            body = get(q='api', agent='codex', dir='~/a', state='closed')
            self.assertEqual(body['counts'], {'all': 1, 'claude': 0, 'codex': 1})
            self.assertEqual(body['dirs'], [{'dir': '~/a', 'count': 1, 'temporary': False}, {'dir': '~/b', 'count': 1, 'temporary': False}])
            body = get(q='api', agent='codex', state='open')
            self.assertEqual(body['chats'], [])
            self.assertEqual(body['counts'], {'all': 1, 'claude': 1, 'codex': 0})
            self.assertEqual(body['dirs'], [])
            self.assertEqual(get(q='secret')['total'], 4)
        self.run.assert_not_called()

    def test_history_route_bad_parameters_query_cap_and_uncapped_directories(self):
        for query in ('limit=', 'limit=0', 'limit=101', 'limit=x', 'offset=-1', 'offset=1.2',
                      'offset=', 'offset=x', 'agent=', 'agent=exec', 'state=', 'state=live'):
            with self.subTest(query=query):
                status, body = self.request('/mobile/api/history?' + query, method='GET')
                self.assertEqual(status, 400)
                self.assertIn('error', body)
        chats = [dict(id=str(number), agent='claude', title='x' * 200, text='', cwd=f'/app/{number:02}', time='')
                 for number in range(35)]
        with patch.object(history, 'all_chats', return_value=chats):
            _, body = self.request('/mobile/api/history?' + urlencode({'q': 'x' * 200 + 'missing'}), method='GET')
        self.assertEqual(body['total'], 35)
        self.assertEqual(len(body['chats']), 30)
        self.assertEqual(len(body['dirs']), 35)
        self.assertEqual(body['dirs'][0], {'dir': '/app/00', 'count': 1, 'temporary': False})
        self.run.assert_not_called()

    def hidden_history(self):
        status, body = self.request('/mobile/api/history', method='GET')
        self.assertEqual(status, 200)
        return body

    def hide(self, **body):
        self.assertEqual(self.request('/mobile/api/history/hide', body), (200, {'ok': True}))

    def test_history_default_hides_temporary_chats_from_all_lists(self):
        chats = [dict(id=CHAT, agent='claude', title='Keep', cwd='/Users/test/app', time=None),
                 dict(id=OTHER, agent='codex', title='Temporary', cwd='/tmp/work', time=None)]
        self.entries.return_value = []
        self.assertFalse(server.prefs_sync.FILE.with_name('chat-hidden.json').exists())
        with patch.object(history, 'all_chats', return_value=chats), \
                patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            body = self.hidden_history()
            self.assertEqual([chat['id'] for chat in body['chats']], [CHAT])
            self.assertEqual(body['dirs'], [{'dir': '~/app', 'count': 1, 'temporary': False}])
            self.assertEqual(body['counts'], {'all': 1, 'claude': 1, 'codex': 0})
            self.assertEqual((body['temporary'], body['hiddenCount']), ('hidden', 1))
            _, filtered = self.request('/mobile/api/history?agent=claude&q=Keep', method='GET')
            self.assertEqual(filtered['hiddenCount'], 1)
        self.run.assert_not_called()

    def test_history_default_without_temporary_chats_has_no_hidden_count(self):
        chats = [dict(id=CHAT, agent='claude', title='Keep', cwd='/Users/test/app', time=None)]
        self.entries.return_value = []
        self.assertFalse(server.prefs_sync.FILE.with_name('chat-hidden.json').exists())
        with patch.object(history, 'all_chats', return_value=chats):
            body = self.hidden_history()
        self.assertEqual((body['temporary'], body['hiddenCount']), ('hidden', 0))
        self.assertEqual([chat['id'] for chat in body['chats']], [CHAT])
        self.run.assert_not_called()

    def test_history_saved_collapsed_mode_keeps_temporary_chats_listed(self):
        chats = [dict(id=CHAT, agent='claude', title='Temporary', cwd='/tmp/work', time=None)]
        self.entries.return_value = []
        file = server.prefs_sync.FILE.with_name('chat-hidden.json')
        saved = json.dumps({'temporary': 'collapsed'})
        file.write_text(saved)
        with patch.object(history, 'all_chats', return_value=chats):
            body = self.hidden_history()
        self.assertEqual((body['temporary'], body['hiddenCount']), ('collapsed', 0))
        self.assertEqual([chat['id'] for chat in body['chats']], [CHAT])
        self.assertEqual(body['dirs'], [{'dir': '/tmp/work', 'count': 1, 'temporary': True}])
        self.assertEqual(file.read_text(), saved)
        self.run.assert_not_called()

    def test_history_default_keeps_live_temporary_chats_listed(self):
        chats = [dict(id=CHAT, agent='claude', title='Live', cwd='/tmp/work', time=None)]
        self.assertFalse(server.prefs_sync.FILE.with_name('chat-hidden.json').exists())
        with patch.object(history, 'all_chats', return_value=chats):
            body = self.hidden_history()
        self.assertEqual((body['temporary'], body['hiddenCount']), ('hidden', 1))
        self.assertEqual([chat['id'] for chat in body['chats']], [CHAT])
        self.assertTrue(body['chats'][0]['live'])
        self.assertEqual(body['dirs'], [{'dir': '/tmp/work', 'count': 1, 'temporary': True}])
        self.assertEqual(body['counts'], {'all': 1, 'claude': 1, 'codex': 0})
        self.run.assert_not_called()

    def test_hide_chat_updates_all_history_fields_and_preserves_transcript(self):
        transcript = self.transcript([{'type': 'user', 'cwd': '/Users/test/app',
                                       'message': {'content': 'Keep these bytes'}}])
        before = transcript.read_bytes()
        self.entries.return_value = []
        initial = self.hidden_history()
        self.assertEqual(initial['total'], 1)
        self.hide(kind='chat', agent='claude', id=CHAT, hidden=True)
        hidden = self.hidden_history()
        self.assertEqual((hidden['chats'], hidden['total'], hidden['next'], hidden['dirs']), ([], 0, None, []))
        self.assertEqual(hidden['counts'], {'all': 0, 'claude': 0, 'codex': 0})
        self.assertEqual(hidden['hiddenCount'], 1)
        self.assertEqual(transcript.read_bytes(), before)
        state_file = server.prefs_sync.FILE.with_name('chat-hidden.json')
        self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
        self.hide(kind='chat', agent='claude', id=CHAT, hidden=False)
        self.assertEqual(self.hidden_history(), initial)
        self.run.assert_not_called()

    def test_live_chats_remain_visible_for_every_hide_kind(self):
        chats = [dict(id=CHAT, agent='claude', title='Live', cwd='/tmp/live', time=None)]
        with patch.object(history, 'all_chats', return_value=chats):
            self.hide(kind='chat', agent='claude', id=CHAT, hidden=True)
            self.hide(kind='dir', dir='/tmp/live', hidden=True)
            self.hide(kind='temporary', mode='hidden')
            body = self.hidden_history()
        self.assertEqual(body['total'], 1)
        self.assertTrue(body['chats'][0]['live'])
        self.assertEqual(body['hiddenCount'], 3)
        self.assertEqual(body['dirs'], [{'dir': '/tmp/live', 'count': 1, 'temporary': True}])

    def test_hide_folder_and_temporary_filter_before_search_and_paging(self):
        self.hide(kind='temporary', mode='collapsed')
        chats = [dict(id=CHAT, agent='claude', title='Folder', cwd='/Users/test/app', time=None),
                 dict(id=OTHER, agent='codex', title='Temporary', cwd='/tmp/work', time=None),
                 dict(id='third', agent='codex', title='Keep', cwd='/tmpfoo', time=None)]
        self.entries.return_value = []
        with patch.object(history, 'all_chats', return_value=chats), \
                patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            initial = self.hidden_history()
            self.assertEqual(initial['temporary'], 'collapsed')
            self.assertEqual(initial['hiddenCount'], 0)
            self.assertEqual({item['dir']: item['temporary'] for item in initial['dirs']},
                             {'~/app': False, '/tmp/work': True, '/tmpfoo': False})
            self.hide(kind='dir', dir='~/app', hidden=True)
            body = self.hidden_history()
            self.assertEqual(body['total'], 2)
            self.assertEqual(body['counts'], {'all': 2, 'claude': 0, 'codex': 2})
            self.assertNotIn('~/app', [item['dir'] for item in body['dirs']])
            self.hide(kind='temporary', mode='hidden')
            body = self.hidden_history()
            self.assertEqual([chat['id'] for chat in body['chats']], ['third'])
            self.assertEqual((body['total'], body['next'], body['hiddenCount']), (1, None, 2))
            self.assertEqual(body['counts'], {'all': 1, 'claude': 0, 'codex': 1})
            self.assertEqual(body['dirs'], [{'dir': '/tmpfoo', 'count': 1, 'temporary': False}])
            _, filtered = self.request('/mobile/api/history?agent=claude&dir=~/app', method='GET')
            self.assertEqual((filtered['total'], filtered['counts']['all'], filtered['dirs']), (0, 0, []))
            self.hide(kind='dir', dir='~/app', hidden=False)
            for mode in ('open', 'collapsed'):
                self.hide(kind='temporary', mode=mode)
                restored = self.hidden_history()
                self.assertEqual(restored['total'], 3)
                self.assertEqual(restored['temporary'], mode)

    def test_hidden_route_lists_existing_chats_and_folders_newest_first(self):
        chat_hidden.set_temporary('collapsed')
        chats = [dict(id=CHAT, agent='claude', title='One', cwd='/Users/test/a', time='2026-01-01'),
                 dict(id=OTHER, agent='codex', title='Two', cwd='/tmp/work', time=None)]
        stale = '00000000-0000-0000-0000-000000000000'
        with patch.object(chat_hidden.time, 'time', side_effect=[1, 2, 3, 4, 5]):
            chat_hidden.set_chat('claude', CHAT, True)
            chat_hidden.set_chat('codex', OTHER, True)
            chat_hidden.set_chat('claude', stale, True)
            chat_hidden.set_dir('/missing', True)
            chat_hidden.set_dir('~/a', True)
        with patch.object(history, 'all_chats', return_value=chats), \
                patch.object(server.Path, 'home', return_value=Path('/Users/test')):
            status, body = self.request('/mobile/api/history/hidden', method='GET')
            self.assertEqual(self.hidden_history()['hiddenCount'], 4)
        self.assertEqual(status, 200)
        self.assertEqual(body, {'temporary': 'collapsed', 'temporaryCount': 1,
                               'dirs': [{'dir': '~/a', 'count': 1}, {'dir': '/missing', 'count': 0}],
                               'chats': [dict(id=OTHER, agent='codex', title='Two', dir='/tmp/work', time=None),
                                         dict(id=CHAT, agent='claude', title='One', dir='~/a', time='2026-01-01')]})
        self.assertIn('claude:' + stale, chat_hidden.load()['chats'])

    def test_hide_rejects_bad_input_and_uses_write_guards(self):
        invalid = [None, [], {}, {'kind': 'unknown'},
                   {'kind': 'chat', 'agent': 'exec', 'id': CHAT, 'hidden': True},
                   {'kind': 'chat', 'agent': [], 'id': CHAT, 'hidden': True},
                   {'kind': 'chat', 'agent': 'claude', 'id': CHAT + 'x', 'hidden': True},
                   {'kind': 'chat', 'agent': 'claude', 'id': None, 'hidden': True},
                   {'kind': 'chat', 'agent': 'codex', 'id': CHAT, 'hidden': 1},
                   {'kind': 'dir', 'dir': '', 'hidden': True},
                   {'kind': 'dir', 'dir': ' ' , 'hidden': True},
                   {'kind': 'dir', 'dir': None, 'hidden': True},
                   {'kind': 'dir', 'dir': 'x' * 4097, 'hidden': True},
                   {'kind': 'dir', 'dir': '~/app', 'hidden': 'false'},
                   {'kind': 'temporary'}, {'kind': 'temporary', 'mode': 'invalid'},
                   {'kind': 'temporary', 'mode': []}]
        for body in invalid:
            with self.subTest(body=body):
                status, response = self.request('/mobile/api/history/hide', body)
                self.assertEqual(status, 400)
                self.assertIsInstance(response['error'], str)
        valid = dict(kind='chat', agent='claude', id=CHAT, hidden=True)
        for headers in ({'Origin': ''}, {'Origin': 'http://other.test'}, {'X-Codex-Mobile': ''}):
            self.assertEqual(self.request('/mobile/api/history/hide', valid, headers=headers)[0], 403)
        self.assertIsNone(self.request('/mobile/api/history/hide', valid, denied=True))
        self.assertEqual(chat_hidden.load()['chats'], {})
        self.assertIsNone(self.request('/mobile/api/history/hidden', method='GET', denied=True))

    def test_resume_clears_only_individual_hidden_chat_after_success(self):
        self.entries.return_value = []
        self.codex_database([{}])
        self.hide(kind='chat', agent='codex', id=CHAT, hidden=True)
        self.hide(kind='chat', agent='claude', id=CHAT, hidden=True)
        with patch.object(server.codex_provider, 'launch_profile', return_value=None):
            self.run.side_effect = subprocess.CalledProcessError(1, 'tmux')
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', 'name': 'past'})[0], 503)
            self.assertIn('codex:' + CHAT, chat_hidden.load()['chats'])
            self.run.side_effect = None
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', 'name': 'past'})[0], 201)
        self.assertNotIn('codex:' + CHAT, chat_hidden.load()['chats'])
        self.assertIn('claude:' + CHAT, chat_hidden.load()['chats'])

    def test_hidden_state_defaults_limits_and_temporary_boundaries(self):
        defaults = {'chats': {}, 'dirs': {}, 'temporary': 'hidden'}
        self.assertEqual(chat_hidden.load(), defaults)
        file = server.prefs_sync.FILE.with_name('chat-hidden.json')
        for content in ('broken', '[]', 'null', '{"chats": [], "dirs": null, "temporary": "bad"}'):
            file.write_text(content)
            self.assertEqual(chat_hidden.load(), defaults)
        chats = {f'claude:00000000-0000-0000-0000-{number:012x}': number for number in range(5001)}
        dirs = {f'/Users/test/{number}': number for number in range(501)}
        file.write_text(json.dumps(dict(chats=chats, dirs=dirs)))
        state = chat_hidden.load()
        self.assertEqual((len(state['chats']), len(state['dirs'])), (5000, 500))
        self.assertNotIn(next(iter(chats)), state['chats'])
        self.assertNotIn('/Users/test/0', state['dirs'])
        chat_hidden.set_chat('codex', CHAT, True)
        chat_hidden.set_dir('/Users/test/new', True)
        state = chat_hidden.load()
        self.assertEqual((len(state['chats']), len(state['dirs'])), (5000, 500))
        self.assertIn('codex:' + CHAT, state['chats'])
        self.assertIn('/Users/test/new', state['dirs'])
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        with patch.object(chat_hidden.tempfile, 'gettempdir', return_value='/Users/test/custom-temp'):
            for root in ('/tmp', '/private/tmp', '/var/folders', '/private/var/folders', '/Users/test/custom-temp'):
                self.assertTrue(chat_hidden.is_temporary(root))
                self.assertTrue(chat_hidden.is_temporary(root + '/child'))
                self.assertFalse(chat_hidden.is_temporary(root + 'foo'))
            for cwd in (None, '', 'tmp/child', '/Users/test/app', '/tmp/../Users/test'):
                self.assertFalse(chat_hidden.is_temporary(cwd))

    def test_resume_codex_with_and_without_profile(self):
        self.entries.return_value = []
        self.codex_database([{}])
        for profile in (None, 'foundry'):
            with self.subTest(profile=profile), patch.object(server.codex_provider, 'launch_profile', return_value=profile) as launch:
                self.run.reset_mock()
                self.run.side_effect = [subprocess.CompletedProcess([], 0, stdout='%13\n', stderr=''),
                                        subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                                        subprocess.CompletedProcess([], 0, stdout='$7\n')]
                self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', 'name': 'past-chat'}),
                                 (201, {'session': '$7', 'pane': '%13', 'name': 'past-chat'}))
                command = 'codex' + (' -p foundry' if profile else '') + ' resume ' + CHAT
                self.assertEqual(self.run.call_args_list[1].args[0], [paths.TMUX, 'send-keys', '-t', '%13', '-l', command])
                launch.assert_called_once_with()

    def test_resume_after_opens_next_window_names_and_launches_under_lock(self):
        self.entries.return_value = [{'id': '%12', 'thread': OTHER}]
        title = 'A chat title ' * 8
        self.codex_database([{'name': title}])
        commands = []
        outputs = ['@7\t/old\n', 'manual\t120\t40\n', '%13\n', '', '', '', '', '$7\n']
        def run(command, **kwargs):
            self.assertTrue(server.SEND_LOCK.locked())
            commands.append(command)
            return subprocess.CompletedProcess([], 0, stdout=outputs[len(commands) - 1], stderr='')
        self.run.side_effect = run
        with patch.object(server.codex_provider, 'launch_profile', return_value='foundry'):
            self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', 'after': '%12'}),
                             (201, {'session': '$7', 'pane': '%13', 'name': title[:60]}))
        self.assertEqual(commands[:4], [
            [paths.TMUX, 'display-message', '-p', '-t', '%12', '#{window_id}\t#{pane_current_path}\t#{@group}'],
            [paths.TMUX, 'display-message', '-p', '-t', '@7', '#{window-size}\t#{window_width}\t#{window_height}'],
            [paths.TMUX, 'new-window', '-a', '-t', '@7', '-c', str(self.root), '-P', '-F', '#{pane_id}'],
            [paths.TMUX, 'resize-window', '-t', '%13', '-x', '120', '-y', '40']])
        self.assertEqual(commands[4][:13], [paths.TMUX, 'set-option', '-p', '-t', '%13', '@chat_name', title[:60], ';',
                                            'set-option', '-p', '-t', '%13', '@chat_name_at'])
        self.assertTrue(commands[4][13].isdigit())
        self.assertEqual(commands[5:], [
            [paths.TMUX, 'send-keys', '-t', '%13', '-l', 'codex -p foundry resume ' + CHAT],
            [paths.TMUX, 'send-keys', '-t', '%13', 'Enter'],
            [paths.TMUX, 'display-message', '-p', '-t', '%13', '#{session_id}']])

    def test_resume_targets_agent_unknown_pane_and_live_conflicts(self):
        self.transcript([{'type': 'user', 'message': {'content': 'Title'}}])
        for body in ({'id': CHAT}, {'id': CHAT, 'name': 'new', 'after': '%12'},
                     {'id': CHAT, 'agent': 'exec', 'name': 'new'}, {'id': CHAT, 'agent': None, 'name': 'new'}):
            self.assertEqual(self.request('/mobile/api/resume', body)[0], 400)
        for agent in ('claude', 'codex'):
            if agent == 'codex':
                self.codex_database([{}])
            for target in ({'name': 'new'}, {'after': '%12'}):
                self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': agent, **target}),
                                 (409, {'error': 'That chat is already open.', 'pane': '%12'}))
        self.entries.return_value = [{'id': '%12', 'thread': OTHER}]
        for pane in ('%99', '$1', None, 12, ['%12']):
            status, body = self.request('/mobile/api/resume', {'id': CHAT, 'after': pane})
            self.assertEqual(status, 400)
            self.assertIn('live session list', body['error'])
        self.run.assert_not_called()

    def test_resume_rejects_bad_command_values_before_creating_a_target(self):
        for identifier, agent, profile in ((CHAT + ';echo', 'claude', None), (None, 'codex', None),
                                            (CHAT, 'exec', None), (CHAT, 'codex', 'bad;profile'),
                                            (CHAT, 'codex', ''), (CHAT, 'codex', 123)):
            with self.assertRaises(ValueError):
                terminal_input.resume_session('new', str(self.root), identifier, self.run, agent=agent, profile=profile)
        self.codex_database([{}])
        self.entries.return_value = [{'id': '%12', 'thread': OTHER}]
        with patch.object(server.codex_provider, 'launch_profile', return_value='bad;profile'):
            for target in ({'name': 'new'}, {'after': '%12'}):
                self.assertEqual(self.request('/mobile/api/resume', {'id': CHAT, 'agent': 'codex', **target})[0], 400)
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
