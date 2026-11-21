"""Restart validation and tmux command ordering, without starting agents."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import paths
import restart_tab
import server
import terminal_input
from test_tmux_isolation import private_tmux


THREAD = '12345678-1234-1234-1234-123456789abc'
REQUEST = {'pane': '%7', 'thread': THREAD}
ROUTE = '/mobile/api/restart-tab'


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        transcript = self.folder / 'chat.jsonl'
        transcript.touch()
        self.entry = {'id': '%7', 'thread': THREAD, 'tmux_session': '$3', 'pane_name': 'Review',
                      'agent': 'claude', 'owner_pid': 4321, 'transcript': str(transcript)}
        self.events = Mock()
        self.events.sessions.return_value = [self.entry]
        self.events.run.side_effect = self.tmux
        self.status = {'state': 'Idle', 'approval': None}
        self.probes = ['sh', 'claude']
        self.default_command = ''
        self.project_mark = '$3\t111111111111:0\t\tNew project\n'
        self.fallback = self.folder / 'pane folder'
        self.fallback.mkdir()
        self.events.cwd.return_value = str(self.folder)
        self.events.saved_tab.return_value = None
        self.events.launch_profile.return_value = None
        self.enterContext(patch.object(restart_tab.procinfo, 'cwd', self.events.cwd))
        self.enterContext(patch.object(restart_tab.audit, 'record', self.events.audit))
        self.enterContext(patch.object(restart_tab.auto_resume, 'saved_tab', self.events.saved_tab))
        self.enterContext(patch.object(restart_tab.codex_provider, 'launch_profile', self.events.launch_profile))
        self.enterContext(patch.object(terminal_input, 'verify', self.events.verify))
        self.enterContext(patch.object(terminal_input, 'live_status', self.events.live_status))
        self.events.live_status.return_value = self.status
        self.prompt = self.enterContext(patch.object(terminal_input, 'empty_prompt', wraps=terminal_input.empty_prompt))
        self.screen = '❯\n────────────────\n'
        self.clock = 0
        self.enterContext(patch.object(restart_tab, 'monotonic', side_effect=lambda: self.clock))

    def tmux(self, argv, **kwargs):
        if argv[1] == 'capture-pane':
            output = self.screen
        elif argv[1] == 'display-message':
            fmt = argv[-1]
            if fmt == '#{pane_current_command}':
                output = (self.probes.pop(0) if self.probes else 'sh') + '\n'
            elif fmt == '#{pane_current_path}':
                output = str(self.fallback) + '\n'
            elif fmt == '#{default-command}':
                output = self.default_command + '\n'
            elif fmt == '#{default-shell}':
                output = '/bin/sh\n'
            elif fmt.startswith('#{pane_width}'):
                output = '30\t6\t30\t6\twork\tclaude\tmanual\n'
            else:
                self.assertEqual(fmt, '#{session_id}\t#{@project_rev}\t#{@project}\t#{session_name}')
                output = self.project_mark
        else:
            output = ''
        return subprocess.CompletedProcess(argv, 0, output, '')

    def sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, .1)
        self.clock += seconds

    def restart(self, body=REQUEST):
        return restart_tab.restart(body, sessions=self.events.sessions, forget_sessions=self.events.forget,
                                   run=self.events.run, sleep=self.sleep)

    def writes(self):
        return [call.args[0] for call in self.events.run.call_args_list
                if call.args[0][1] not in ('capture-pane', 'display-message')]

    def assert_refused(self, message, status=409):
        with self.assertRaises(restart_tab.Refused) as raised:
            self.restart()
        self.assertEqual((raised.exception.status, str(raised.exception)), (status, message))
        self.assertEqual(self.writes(), [])
        self.events.audit.assert_not_called()

    def test_invalid_requests_are_refused_before_discovery(self):
        for body in ({}, dict(REQUEST, pane='%7;'), dict(REQUEST, pane=7), dict(REQUEST, thread='another chat')):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.restart(body)
        self.assertEqual(self.events.mock_calls, [])

    def test_missing_pane(self):
        self.events.sessions.return_value = [dict(self.entry, id='%8')]
        self.assert_refused('That tab has closed.', 404)
        self.assertEqual([item[0] for item in self.events.mock_calls], ['forget', 'sessions'])

    def test_unsupported_agent_and_stale_or_pending_thread(self):
        for fields, message in [({'agent': 'antigravity'}, 'Only Claude and Codex tabs can restart.'),
                                ({'thread': 'other'}, 'That tab now holds another chat. Reload and try again.'),
                                ({'pending_conversation': True}, 'That tab now holds another chat. Reload and try again.')]:
            with self.subTest(fields=fields), patch.dict(self.entry, fields):
                self.assert_refused(message)
        self.events.run.assert_not_called()

    def test_already_current_project(self):
        self.project_mark = '$3\t111111111111:0\t111111111111\tSame project\n'
        self.assert_refused("This tab already uses its session's project.")
        self.events.verify.assert_not_called()

    def test_lost_process_or_conversation_binding(self):
        for error in (terminal_input.ConversationChanged('The agent restarted.'), FileNotFoundError('Registry gone.')):
            with self.subTest(error=error):
                self.events.verify.side_effect = error
                self.assert_refused(str(error))
        self.events.live_status.assert_not_called()

    def test_busy_agent_and_pending_input_or_jobs(self):
        for fields, message in [({'state': 'Working'}, 'Wait until the agent finishes its turn.'),
                                ({'state': 'Unknown'}, 'Wait until the agent finishes its turn.'),
                                ({'state': 'Approval', 'approval': {'id': '123'}}, 'Answer the agent first.'),
                                ({'attention': {'kind': 'question'}}, 'Answer the agent first.'),
                                ({'tasks': [{'kind': 'shell'}]}, 'Stop its background tasks first: a restart ends them.'),
                                ({'crons': [{'id': 'job'}]}, 'This tab has scheduled jobs. A restart ends them.')]:
            with self.subTest(fields=fields), patch.dict(self.status, fields):
                self.assert_refused(message)
        self.prompt.assert_not_called()

    def test_draft_is_preserved(self):
        self.screen = '❯ unsent text\n────────────────\n'
        self.assert_refused('There is an unsent draft in the agent input. Clear it, then send again.')

    def test_hidden_prompt_does_not_resize_or_dismiss_panels(self):
        self.screen = '/usage\n'
        with patch.object(terminal_input.window_size, 'apply') as resize, patch.object(terminal_input, 'close_info_panel') as close:
            self.assert_refused(terminal_input.SIZE_HELP)
            resize.assert_not_called()
            with patch.object(terminal_input, 'small_pane_reason', return_value=None):
                self.assert_refused('Cannot find the Claude input box on screen. Open the pane in Terminals to see what it shows.')
            close.assert_not_called()

    def test_transcript_must_be_a_file(self):
        for path in (None, self.folder / 'missing', self.folder):
            with self.subTest(path=path), patch.dict(self.entry, transcript=str(path) if path is not None else None):
                self.assert_refused("Tether cannot find this chat's history file.")
        self.events.cwd.assert_not_called()

    def test_claude_restarts_in_process_folder_then_confirms_start(self):
        self.assertEqual(self.restart(), dict(REQUEST, agent='claude', started=True))
        self.assertEqual(self.writes(), [[paths.TMUX, 'respawn-pane', '-k', '-t', '%7', '-c', str(self.folder), '/bin/sh', '-l'],
                                        [paths.TMUX, 'send-keys', '-t', '%7', '-l', 'claude --resume ' + THREAD],
                                        [paths.TMUX, 'send-keys', '-t', '%7', 'Enter']])
        self.events.audit.assert_called_once_with('tab.restart', 'Restarted tab Review', pane='%7')
        audit_index = next(i for i, call in enumerate(self.events.mock_calls) if call[0] == 'audit')
        self.assertEqual(self.events.mock_calls[audit_index + 1].args[0], self.writes()[0])
        self.assertEqual(self.clock, .1)
        self.events.saved_tab.assert_not_called()

    def test_codex_uses_saved_profile_or_current_default(self):
        for saved, agent_command, profile in [({'profile': 'foundry'}, 'node', 'foundry'),
                                              ({'profile': None}, 'codex', None), (None, 'codex', 'foundry')]:
            with self.subTest(saved=saved):
                self.events.reset_mock()
                self.entry['agent'] = 'codex'
                self.screen = '›\n\n'
                self.probes = [agent_command]
                self.events.cwd.side_effect = FileNotFoundError
                self.events.saved_tab.return_value = saved
                self.events.launch_profile.return_value = 'foundry'
                self.assertEqual(self.restart(), dict(REQUEST, agent='codex', started=True))
                self.assertEqual(self.writes()[0][6], str(self.fallback))
                command = 'codex' + (' -p ' + profile if profile else '') + ' resume ' + THREAD
                self.assertEqual(self.writes()[1][-1], command)
                self.assertEqual(self.events.launch_profile.call_count, int(saved is None))

    def test_respawn_uses_the_configured_default_command(self):
        self.default_command = 'exec /bin/sh -i;'
        self.assertTrue(self.restart()['started'])
        self.assertEqual(self.writes()[0][7:], [r'exec /bin/sh -i\;'])

    def test_startup_timeout_does_not_retry_or_kill_the_pane(self):
        self.probes = ['sh', 'node']  # A Codex process name does not confirm Claude startup.
        self.assertEqual(self.restart(), dict(REQUEST, agent='claude', started=False, warning=
                         'The tab restarted, but Claude did not start yet. Open the tab to check.'))
        self.assertEqual(self.clock, 10)
        self.assertEqual([argv[1] for argv in self.writes()], ['respawn-pane', 'send-keys', 'send-keys'])


class RestartRouteTests(unittest.TestCase):
    def post(self, headers, body=REQUEST, path=ROUTE):
        payload = json.dumps(body).encode()
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command = path, 'POST'
        handler.client_address = ('127.0.0.1', 0)
        handler.headers = {'Host': 'phone.test', 'Content-Length': str(len(payload)), **headers}
        handler.rfile = BytesIO(payload)
        handler.respond = Mock()
        handler.do_POST()
        return handler.respond.call_args.args

    def setUp(self):
        self.enterContext(patch.object(server.auth, 'MODE', 'token'))
        self.enterContext(patch.object(server.auth, 'token', return_value='test-token'))
        self.headers = {'Cookie': server.auth.COOKIE + '=' + server.auth.session_value(),
                        'Origin': 'https://phone.test', 'X-Codex-Mobile': '1'}

    def test_restart_requires_cookie_origin_and_write_header(self):
        with patch.object(restart_tab, 'restart') as restart:
            for header, value, status in [('Cookie', '', 401), ('Origin', 'https://other.test', 403),
                                          ('X-Codex-Mobile', '', 403)]:
                with self.subTest(header=header):
                    self.assertEqual(self.post(dict(self.headers, **{header: value}))[0], status)
            restart.assert_not_called()

    def test_route_reports_validation_and_refusals(self):
        self.assertEqual(self.post(self.headers, dict(REQUEST, thread='wrong')), (400, {'error': 'Choose a valid chat id.'}))
        with patch.object(server, 'discover', return_value=[]) as discover:
            self.assertEqual(self.post(self.headers), (404, {'error': 'That tab has closed.'}))
            self.assertEqual(discover.call_args.kwargs, {'repair_zoom': False})

    def test_route_restarts_under_send_lock(self):
        reply = dict(REQUEST, agent='claude', started=True)

        def restart(body, **kwargs):
            self.assertTrue(server.SEND_LOCK.locked())
            self.assertEqual(body, REQUEST)
            return reply

        with patch.object(restart_tab, 'restart', side_effect=restart):
            self.assertEqual(self.post(self.headers), (200, reply))
        self.assertIn(ROUTE, server.TMUX_WRITES)

    def test_move_reply_binds_the_restart_offer_to_the_tab_chat(self):
        # The toast's Restart sends pane and thread back to restart-tab; a chat before its first message has no thread.
        moved = lambda: {'session': '$2', 'name': 'Beta', 'pane': '%7', 'restart': {'project': 'beta', 'name': 'Beta site'}}
        body = {'kind': 'pane', 'source': '%7', 'target': '$2'}
        for entry, thread in [({'id': '%7', 'thread': REQUEST['thread']}, REQUEST['thread']),
                              ({'id': '%7', 'thread': REQUEST['thread'], 'pending_conversation': True}, None), (None, None)]:
            with self.subTest(entry=entry), patch.object(server.undo, 'record_merge', return_value=lambda result: None), \
                    patch.object(server.tmux_merge, 'merge', side_effect=lambda *a, **k: moved()), \
                    patch.object(server, 'sessions', return_value=[entry] if entry else []):
                status, reply = self.post(self.headers, body, '/mobile/api/merge')[:2]
                self.assertEqual(status, 200)
                self.assertEqual(reply['restart'], {'project': 'beta', 'name': 'Beta site', 'pane': '%7', 'session': 'Beta', 'thread': thread})
        with patch.object(server.undo, 'record_merge', return_value=lambda result: None), \
                patch.object(server.tmux_merge, 'merge', return_value={'session': '$2', 'name': 'Beta', 'pane': '%7'}), \
                patch.object(server, 'sessions') as listed:
            self.assertNotIn('restart', self.post(self.headers, body, '/mobile/api/merge')[1])
            listed.assert_not_called()


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required')
class RealTmuxRestartTests(unittest.TestCase):
    def test_respawn_replaces_the_old_command_and_keeps_the_last_pane(self):
        tmux = private_tmux(self, 'trestart-')
        folder = Path(tmux.folder) / 'new folder-#{pane_id};'
        folder.mkdir()
        created = tmux.tmux('new-session', '-d', '-s', 'work', '-P', '-F', '#{pane_id}', '/bin/sleep', '120')
        created.check_returncode()
        pane = created.stdout.strip()
        tmux.tmux('set-option', '-t', 'work', 'default-shell', '/bin/sh').check_returncode()
        session = tmux.tmux('display-message', '-p', '-t', pane, '#{session_id}').stdout.strip()
        options = {'@chat_name': 'Review', '@project_rev': '111111111111:0', '@group': 'g12345678'}
        for key, value in options.items():
            tmux.tmux('set-option', '-p', '-t', pane, key, value).check_returncode()
        transcript = folder / 'chat.jsonl'
        transcript.touch()
        audit_folder = Path(tmux.folder) / 'audit'
        entry = {'id': pane, 'thread': THREAD, 'tmux_session': session, 'agent': 'claude',
                 'transcript': str(transcript), 'owner_pid': 123, 'pane_name': 'Review'}

        def started(run, pane, commands, sleep):
            tmux.run([paths.TMUX, 'wait-for', 'restart-ready'], check=True, capture_output=True, timeout=5)
            return True

        with patch.object(restart_tab.procinfo, 'cwd', return_value=str(folder)), \
                patch.object(restart_tab.audit, '_folder', return_value=audit_folder), patch.object(terminal_input, 'verify'), \
                patch.object(terminal_input, 'live_status', return_value={'state': 'Idle'}), \
                patch.object(terminal_input, 'empty_prompt'), patch.object(restart_tab, '_started', side_effect=started), \
                patch.object(terminal_input, 'resume_command', return_value='pwd > restart.cwd; tmux wait-for -S restart-ready'):
            reply = restart_tab.restart(dict(REQUEST, pane=pane), sessions=lambda: [entry],
                                        forget_sessions=lambda: None, run=tmux.run)
        self.assertTrue(reply['started'])
        self.assertEqual((folder / 'restart.cwd').read_text().strip(), str(folder))
        audit_entry = json.loads(next(audit_folder.glob('*.jsonl')).read_text())
        self.assertEqual((audit_entry['kind'], audit_entry['summary'], audit_entry['detail']),
                         ('tab.restart', 'Restarted tab Review', {'pane': pane}))
        self.assertEqual(tmux.tmux('list-panes', '-s', '-t', session, '-F', '#{pane_id}').stdout.splitlines(), [pane])
        for key, value in options.items():
            self.assertEqual(tmux.tmux('display-message', '-p', '-t', pane, '#{' + key + '}').stdout.strip(), value)
