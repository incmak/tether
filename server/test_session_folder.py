"""New session routes honor the shared folder without changing resume or split routes."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import agents
import prefs_sync
import server


class SessionFolderRoutes(unittest.TestCase):
    def request(self, route, payload=None):
        handler = object.__new__(server.Handler)
        handler.path = route
        body = json.dumps(payload if payload is not None else {'name': 'test-session'}).encode()
        handler.headers = {'Origin': 'http://localhost', 'Host': 'localhost',
                           'X-Codex-Mobile': '1', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.deny = Mock(return_value=False)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def test_both_new_session_routes_use_the_saved_folder(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(prefs_sync, 'FILE', Path(folder) / 'prefs.json'), \
                patch.object(server, 'create_session', return_value='%99') as create, \
                patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '$99\n')):
            prefs_sync.save({'sessionFolder': folder})
            for route, status in (('/mobile/api/session', 200), ('/mobile/api/tmux-sessions', 201)):
                with self.subTest(route=route):
                    self.assertEqual(self.request(route)[0], status)
                    create.assert_called_with('test-session', folder)

    def test_missing_saved_folder_refuses_creation(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(prefs_sync, 'FILE', Path(folder) / 'prefs.json'), \
                patch.object(server, 'create_session') as create:
            target = Path(folder) / 'project'
            target.mkdir()
            prefs_sync.save({'sessionFolder': str(target)})
            target.rmdir()
            for route in ('/mobile/api/session', '/mobile/api/tmux-sessions'):
                status, body = self.request(route)
                self.assertEqual(status, 400)
                self.assertIn('default session folder', body['error'])
            create.assert_not_called()

    def test_delegate_worktree_reviewer_refusal_creates_no_branch_folder_or_window(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / 'project'
            def git(*args):
                return subprocess.run(['git', '-C', folder, *args], check=True,
                                      capture_output=True, text=True, timeout=10).stdout
            git('init', '-b', 'main', str(repo))
            git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '--allow-empty', '-m', 'First')
            before = git('-C', str(repo), 'branch', '--list')
            thread = '0f8fad5b-d9cb-469f-a165-70867728950e'
            source = dict(id='%12', agent='codex', thread=thread, writable=True,
                          transcript=str(repo / 'chat.jsonl'))
            with patch.object(server, 'sessions', return_value=[source]), \
                    patch.object(server, 'agent_folder', return_value=str(repo)), \
                    patch.object(server.subprocess, 'run') as run:
                self.assertEqual(self.request('/mobile/api/delegate',
                                              dict(session='%12', thread=thread, agent='codex', message='',
                                                   review='new', worktree='feature/reviewer')),
                                 (400, {'error': "A reviewer tab stays in this tab's folder, so it can see the work. Turn off the new worktree or the new tab review."}))
                run.assert_not_called()
            self.assertEqual(git('-C', str(repo), 'branch', '--list'), before)
            self.assertFalse((Path(folder) / 'project-wt').exists())

    def test_worktree_session_uses_new_path_and_saves_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            made = {'branch': 'feature/session', 'path': folder}
            def make(body, base):
                self.assertFalse(server.SEND_LOCK.locked())
                self.assertEqual(base, folder)
                return made
            def create(name, cwd):
                self.assertTrue(server.SEND_LOCK.locked())
                self.assertEqual((name, cwd), ('test-session', folder))
                return '%99'
            def run(args, **kwargs):
                self.assertEqual(server.SEND_LOCK.locked(), args[1] != 'has-session')
                return subprocess.CompletedProcess(args, 1 if args[1] == 'has-session' else 0, '$99\n', '')
            with patch.object(prefs_sync, 'session_folder', return_value=folder), \
                    patch.object(server.worktrees, 'from_body', side_effect=make), \
                    patch.object(server, 'create_session', side_effect=create), \
                    patch.object(server.subprocess, 'run', side_effect=run) as commands:
                self.assertEqual(self.request('/mobile/api/tmux-sessions',
                                              {'name': 'test-session', 'worktree': made['branch']}),
                                 (201, {'session': '$99', 'name': 'test-session', 'worktree': made}))
            self.assertEqual([call.args[0][1:] for call in commands.call_args_list], [
                ['has-session', '-t', '=test-session'],
                ['set-option', '-p', '-t', '%99', '@worktree', made['branch']],
                ['display-message', '-p', '-t', '%99', '#{session_id}']])

    def test_worktree_session_refuses_bad_or_taken_name_before_creation(self):
        with patch.object(prefs_sync, 'session_folder', return_value='/Users/test/project'), \
                patch.object(server.worktrees, 'from_body') as make, \
                patch.object(server, 'create_session') as create, \
                patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertEqual(self.request('/mobile/api/tmux-sessions', {'name': 'bad.name', 'worktree': 'feature/foo'}),
                             (400, {'error': server.NAME_HELP}))
            run.assert_not_called()
            self.assertEqual(self.request('/mobile/api/tmux-sessions', {'name': 'taken', 'worktree': 'feature/foo'}),
                             (400, {'error': 'A session with that name already exists.'}))
            make.assert_not_called()
            create.assert_not_called()

    def test_worktree_session_creation_refusal_message_reaches_reply(self):
        with patch.object(prefs_sync, 'session_folder', return_value='/Users/test/project'), \
                patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                patch.object(server.worktrees, 'from_body', side_effect=ValueError('Branch already exists.')), \
                patch.object(server, 'create_session') as create:
            self.assertEqual(self.request('/mobile/api/tmux-sessions', {'name': 'new', 'worktree': 'feature/foo'}),
                             (400, {'error': 'Branch already exists.'}))
            create.assert_not_called()

    def test_worktree_session_survives_tmux_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            made = {'branch': 'feature/session', 'path': folder}
            def run(args, **kwargs):
                if args[1] == 'has-session':
                    return subprocess.CompletedProcess(args, 1, '', '')
                raise subprocess.CalledProcessError(1, args)
            with patch.object(prefs_sync, 'session_folder', return_value=folder), \
                    patch.object(server.worktrees, 'from_body', return_value=made), \
                    patch.object(server, 'create_session', return_value='%99'), \
                    patch.object(server.subprocess, 'run', side_effect=run) as commands:
                status, reply = self.request('/mobile/api/tmux-sessions', {'name': 'new', 'worktree': made['branch']})
            self.assertEqual(status, 503)
            self.assertIn(folder, reply['error'])
            self.assertTrue(Path(folder).is_dir())
            self.assertFalse(any('remove' in call.args[0] for call in commands.call_args_list))


class WorktreeInfoRoutes(unittest.TestCase):
    def request(self, query='', denied=False):
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/worktree-info' + query
        handler.deny = Mock(return_value=denied)
        handler.respond = Mock()
        handler.do_GET()
        handler.deny.assert_called_once_with()
        return handler.respond.call_args.args if handler.respond.called else None

    def test_saved_folder_and_session_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = {'main': folder, 'base': folder + '-wt'}
            with patch.object(prefs_sync, 'session_folder', return_value=folder) as saved, \
                    patch.object(server, 'choose', return_value={'id': '%99'}) as choose, \
                    patch.object(server, 'agent_folder', return_value=folder) as agent_folder, \
                    patch.object(server.worktrees, 'info', return_value=repo) as info:
                self.assertEqual(self.request(), (200, {'repo': True, 'base': server.home_path(repo['base'])}))
                saved.assert_called_once_with()
                choose.assert_not_called()
                self.assertEqual(self.request('?session=%2599'),
                                 (200, {'repo': True, 'base': server.home_path(repo['base'])}))
                choose.assert_called_once_with('%99')
                agent_folder.assert_called_once_with({'id': '%99'})
                self.assertEqual(info.call_count, 2)
                info.assert_called_with(folder)

    def test_missing_session_folder_falls_back_and_base_hides_home(self):
        with patch.object(server, 'choose', return_value={'id': '%99'}), \
                patch.object(server, 'agent_folder', return_value='/missing/project'), \
                patch.object(server.Path, 'home', return_value=Path('/Users/test')), \
                patch.object(server.worktrees, 'info', return_value={'base': '/Users/test/project-wt'}) as info:
            self.assertEqual(self.request('?session=%2599'), (200, {'repo': True, 'base': '~/project-wt'}))
            info.assert_called_once_with(str(server.paths.HOME))

    def test_nonrepo_and_folder_lookup_refusals_return_false(self):
        with patch.object(prefs_sync, 'session_folder', return_value='/Users/test/project'), \
                patch.object(server.worktrees, 'info', return_value=None):
            self.assertEqual(self.request(), (200, {'repo': False}))
        with patch.object(prefs_sync, 'session_folder', side_effect=ValueError('missing')), \
                patch.object(server.worktrees, 'info') as info:
            self.assertEqual(self.request(), (200, {'repo': False}))
            info.assert_not_called()
        with patch.object(server, 'choose', side_effect=ValueError('session ended')), \
                patch.object(server.worktrees, 'info') as info:
            self.assertEqual(self.request('?session=%2599'), (200, {'repo': False}))
            info.assert_not_called()

    def test_login_is_required(self):
        with patch.object(prefs_sync, 'session_folder') as folder, patch.object(server.worktrees, 'info') as info:
            self.assertIsNone(self.request(denied=True))
            folder.assert_not_called()
            info.assert_not_called()


class WorktreeDiscovery(unittest.TestCase):
    def test_old_and_new_lines_keep_icons_names_positions_and_zoom(self):
        with tempfile.TemporaryDirectory() as folder:
            lines = '\n'.join([
                '%1\t$1\trepo\t1.0\t101\t/work\t123\tOld tab\tterminal\t',
                '%2\t$1\trepo\t9.0\t102\t/work-wt/feature-foo\t456\tNew tab\tcode\t feature/foo \t',
                '%3\t$1\trepo\t2.0\t103\t/work\t\t\t\t\t%2',
                '%4\t$1\trepo\t3.0\t104\t/work\t\t\t\t' + 'x' * 201 + '\t'])
            run = Mock(return_value=subprocess.CompletedProcess([], 0, lines, ''))
            with patch.object(agents, 'HOME', Path(folder)), \
                    patch.object(agents.pane_view, 'zoom_repair_all'), \
                    patch.object(agents.procinfo, 'table', return_value={}), \
                    patch.object(agents, 'process_start', return_value='1'), \
                    patch.object(agents, 'codex_database'):
                entries = agents.discover({}, run)
        self.assertEqual([entry['id'] for entry in entries], ['%1', '%2', '%4'])
        self.assertEqual((entries[0]['worktree'], entries[0]['pane_name'], entries[0]['pane_icon']),
                         ('', 'Old tab', 'terminal'))
        self.assertEqual((entries[1]['worktree'], entries[1]['pane_name'], entries[1]['pane_icon'],
                          entries[1]['position'], entries[1]['cwd'], entries[1]['pane_named_at']),
                         ('feature/foo', 'New tab', 'code', '2.0', '/work-wt/feature-foo', 456))
        self.assertEqual(len(entries[2]['worktree']), 200)
        self.assertIn('#{@chat_icon}\t#{@worktree}\t#{@group}\t#{@zoom_for}', run.call_args.args[0][-1])

    def test_role_field_marks_advisor_tabs_and_keeps_zoom(self):
        with tempfile.TemporaryDirectory() as folder:
            lines = '\n'.join([
                '%1\t$1\trepo\t1.0\t101\t/work\t123\tAdvisor: Fix\tlightbulb\t\t\t\tadvisor',
                '%2\t$1\trepo\t2.0\t102\t/work\t123\tAdvisor 2: Fix\tlightbulb\t\t\t\tsecond-advisor',
                '%3\t$1\trepo\t3.0\t103\t/work\t123\tTab\tbug\t\t\t\tboss',
                '%4\t$1\trepo\t4.0\t104\t/work\t\t\t\t\t\t%1\tadvisor',
                '%5\t$1\trepo\t5.0\t105\t/work\t123\tOld\tbug\t\t\t'])
            run = Mock(return_value=subprocess.CompletedProcess([], 0, lines, ''))
            with patch.object(agents, 'HOME', Path(folder)), \
                    patch.object(agents.pane_view, 'zoom_repair_all'), \
                    patch.object(agents.procinfo, 'table', return_value={}), \
                    patch.object(agents, 'process_start', return_value='1'), \
                    patch.object(agents, 'codex_database'):
                entries = agents.discover({}, run)
        self.assertEqual([(entry['id'], entry.get('role'), entry['pane_name'], entry['cwd']) for entry in entries],
                         [('%1', 'advisor', 'Advisor: Fix', '/work'), ('%2', 'advisor', 'Advisor 2: Fix', '/work'),
                          ('%3', None, 'Tab', '/work'), ('%5', None, 'Old', '/work')])
        self.assertTrue(run.call_args.args[0][-1].endswith('#{@zoom_for}\t#{@tether_role}'))

    def test_group_field_is_read_and_old_lines_have_none(self):
        with tempfile.TemporaryDirectory() as folder:
            lines = '\n'.join([
                '%1\t$1\trepo\t1.0\t101\t/work\t123\tTab\tterminal\tfeature/foo\tg1a2b3c4d\t',
                '%2\t$1\trepo\t2.0\t102\t/work\t\t\t\tfeature/bar\t',
                '%3\t$1\trepo\t3.0\t103\t/work\t\t\t\t\tg1a2b3c4d\t%1'])
            run = Mock(return_value=subprocess.CompletedProcess([], 0, lines, ''))
            with patch.object(agents, 'HOME', Path(folder)), \
                    patch.object(agents.pane_view, 'zoom_repair_all'), \
                    patch.object(agents.procinfo, 'table', return_value={}), \
                    patch.object(agents, 'process_start', return_value='1'), \
                    patch.object(agents, 'codex_database'):
                entries = agents.discover({}, run)
        self.assertEqual([(e['id'], e['group'], e['worktree'], e['pane_icon']) for e in entries],
                         [('%1', 'g1a2b3c4d', 'feature/foo', 'terminal'), ('%2', '', 'feature/bar', '')])

    def test_session_row_copies_worktree(self):
        with patch.object(server, 'shared_status', return_value={}), \
                patch.object(server, 'tab_folder', return_value='/Users/test/project'), \
                patch.object(server, 'timeline', return_value={}), \
                patch.object(server.needs, 'summary', return_value=''):
            self.assertEqual(server.session_row({'id': '%1', 'worktree': 'feature/foo'})['worktree'], 'feature/foo')
