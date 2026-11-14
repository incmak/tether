"""Codex hooks use isolated cache/config files and mocked terminal reads."""
from datetime import datetime, timezone
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import claude_hooks as hooks
import autonomy
import paths
import server
import terminal_input as terminal


class CodexHooksTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.transcript = self.root / 'rollout.jsonl'
        self.transcript.write_text('')
        self.entry = {'id': '%fixture', 'agent': 'codex', 'thread': 'codex-session',
                      'transcript': str(self.transcript)}
        for context in (patch.dict(os.environ, MOBILE_TMUX_CACHE=str(self.root)),
                        patch.object(paths, 'CACHE', self.root),
                        patch.object(paths, 'CODEX_HOOKS', self.root / '.codex/hooks.json')):
            context.start()
            self.addCleanup(context.stop)
        terminal._attention_seen.clear()

    def feed(self, event, now=100, **extra):
        hooks.capture(dict(session_id=self.entry['thread'], hook_event_name=event, **extra), now, agent='codex')

    def live(self, now=110):
        return hooks.codex_live(self.entry, now)

    def state(self):
        return self.root / 'hooks' / (self.entry['thread'] + '.json')

    def tool(self, now=101, identity='tool-1'):
        self.feed('PreToolUse', now, tool_name='Bash', tool_use_id=identity, tool_input={'command': 'echo hi'})

    def permission(self, now=102):
        self.feed('PermissionRequest', now, tool_name='Bash', tool_input={'command': 'echo hi', 'description': 'Allow this command'})

    def test_event_sequence(self):
        self.assertIsNone(self.live())
        self.feed('SessionStart')
        self.assertFalse(self.live()['busy'])
        self.assertEqual(json.loads(self.state().read_text())['agent'], 'codex')
        self.feed('UserPromptSubmit')
        self.assertTrue(self.live()['busy'])
        self.tool()
        self.assertEqual(self.live()['tool'], {'title': 'Bash', 'command': 'echo hi', 'started': 101})
        self.permission()
        self.assertTrue(self.live()['waiting'])
        self.assertEqual(self.live()['approval'], {'tool': 'Bash', 'text': 'Allow this command', 'at': 102})
        self.feed('PostToolUse', 103, tool_use_id='tool-1')
        self.assertTrue(self.live()['busy'])
        self.assertFalse(self.live()['waiting'])
        self.assertIsNone(self.live()['approval'])
        self.assertIsNone(self.live()['tool'])
        for event in ('Interrupt', 'Stop'):
            self.feed('UserPromptSubmit', 104)
            self.feed(event, 105)
            self.assertFalse(self.live()['busy'], event)
            self.assertFalse(self.live()['waiting'], event)
            self.assertIsNone(self.live()['tool'], event)
        self.feed('SessionEnd', 106)
        self.assertIsNone(self.live())

    def test_approval_clear_events(self):
        for event in ('PostToolUse', 'Stop', 'Interrupt', 'UserPromptSubmit', 'SessionStart', 'SessionEnd'):
            with self.subTest(event=event):
                self.feed('UserPromptSubmit')
                self.tool()
                self.permission()
                self.feed(event, 103)
                self.assertNotIn('approval', json.loads(self.state().read_text()))
                self.state().unlink()

    def test_approval_survives_same_tool_but_not_next_tool(self):
        self.tool()
        self.permission()
        self.tool(103)
        self.assertIsNotNone(self.live()['approval'])
        self.tool(104, 'tool-2')
        self.assertIsNone(self.live()['approval'])

    def test_approval_command_fallback_and_short_text(self):
        self.feed('PermissionRequest', tool_name='  Bash  ', tool_input={'command': 'echo\n' + 'a' * 200})
        self.assertEqual(self.live()['approval']['tool'], 'Bash')
        self.assertEqual(self.live()['approval']['text'], ('echo ' + 'a' * 200)[:160])

    def test_wrong_session_agent_and_clock_skew_fall_back(self):
        self.feed('UserPromptSubmit')
        original = json.loads(self.state().read_text())
        for changed in ({'session_id': 'wrong'}, {'agent': 'claude'}, {'reported_at': 120},
                        {'started': 120}, {'reported_at': float('nan')}, {'ended': True}):
            self.state().write_text(json.dumps(dict(original, **changed)))
            self.assertIsNone(self.live(), changed)

    def test_busy_without_tools_expires_but_active_tool_does_not(self):
        self.feed('UserPromptSubmit')
        self.assertIsNone(self.live(101 + hooks.TURN_MAX_AGE))
        self.tool()
        self.assertTrue(self.live(101 + hooks.TURN_MAX_AGE)['busy'])

    def rollout(self, kind, stamp=102, **extra):
        return json.dumps({'type': 'event_msg', 'timestamp': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                           'payload': {'type': kind}, **extra}) + '\n'

    def test_newer_lifecycle_invalidates_even_active_tools(self):
        self.feed('UserPromptSubmit')
        self.tool()
        for event in ('task_started', 'task_complete', 'turn_aborted'):
            self.transcript.write_text(self.rollout(event, 103))
            self.assertIsNone(self.live(), event)
        self.transcript.write_text(self.rollout('task_complete', 102))
        self.assertIsNotNone(self.live(), 'one second write lag is allowed')
        self.transcript.write_text(self.rollout('token_count', 103))
        self.assertIsNotNone(self.live())

    def test_rollout_tail_is_bounded_and_ignores_malformed_lines(self):
        self.feed('UserPromptSubmit')
        self.transcript.write_text(self.rollout('task_complete') + 'x' * 262144 + '\ninvalid json\n')
        self.assertIsNotNone(self.live())
        with self.transcript.open('a') as stream:
            stream.write(self.rollout('task_complete'))
        self.assertIsNone(self.live())

    def status(self, screen):
        run = Mock(side_effect=AssertionError('status must not touch tmux'))
        with patch.object(terminal, 'capture_quota_notices'), patch.object(terminal, 'attention', return_value=None), \
                patch.object(terminal.prompt_actions, 'panel', return_value=None):
            return terminal._live_status(self.entry, run, screen)

    def test_hooks_idle_overrides_screen_and_transcript(self):
        self.feed('Stop')
        with patch.object(hooks.time, 'time', return_value=110), \
                patch.object(terminal, 'transcript_busy', side_effect=AssertionError('hooks must win')):
            self.assertEqual(self.status('Working (8s)\n› ')['state'], 'Idle')

    def test_waiting_without_screen_options_shows_read_only_card(self):
        self.tool()
        self.permission()
        with patch.object(hooks.time, 'time', return_value=110):
            result = self.status('\n'.join('row-%02d' % row for row in range(30)))
        self.assertEqual(result['state'], 'Approval')
        card = result['approval']
        self.assertIsNone(card['id'])
        self.assertFalse(card['actionable'])
        for text in ('Codex is waiting for your approval.', 'Bash', 'Allow this command', 'row-10', 'row-29'):
            self.assertIn(text, card['text'])
        self.assertNotIn('row-09', card['text'])

    def test_working_screen_after_approval_overrides_waiting(self):
        self.tool()
        self.permission()
        with patch.object(hooks.time, 'time', return_value=110):
            self.assertEqual(self.status('Working (8s • esc to interrupt)\n› ')['state'], 'Working')

    def test_claude_mode_ignores_interrupt(self):
        hooks.capture({'session_id': 'abc-1', 'hook_event_name': 'Interrupt'}, 100)
        self.assertFalse((Path(self.root) / "hooks/abc-1.json").exists())

    def test_screen_approval_keeps_keys_before_reading_hooks(self):
        pending = {'id': 'screen-id', 'text': 'Allow?', 'options': [{'key': '1', 'label': 'Yes'}]}
        with patch.object(terminal, 'approval', return_value=pending), \
                patch.object(hooks, 'codex_live', side_effect=AssertionError('screen options win')):
            self.assertEqual(self.status('approval screen')['approval'], pending)

    def test_no_hooks_preserves_screen_and_transcript_fallback(self):
        self.assertEqual(self.status('Working (8s)\n› ')['state'], 'Working')
        self.assertEqual(self.status('› ')['state'], 'Idle')
        self.transcript.write_text(self.rollout('task_started'))
        self.assertEqual(self.status('› ')['state'], 'Working')

    def footer(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/mobile/api/footer?session=%25fixture'
        handler.deny = lambda: False
        replies = []
        handler.respond = lambda status, data: replies.append((status, data))
        with patch.object(server, 'choose', return_value=self.entry), \
                patch.object(server.history, 'read', return_value={'usage': {}, 'model': 'test'}), \
                patch.object(server, 'codex_metrics', return_value={}), \
                patch.object(server.footer, 'codex', return_value={'context': {'percent': 10}}), \
                patch.object(server.time, 'time', return_value=170):
            handler.do_GET()
        self.assertEqual(replies[0][0], 200)
        return replies[0][1]

    def test_footer_tools_waiting_idle_and_fallback(self):
        self.assertEqual(self.footer(), {'context': {'percent': 10}})
        self.feed('UserPromptSubmit')
        self.tool()
        result = self.footer()
        self.assertEqual(result['spinner'], {'verb': 'Working…', 'detail': '1m 10s'})
        self.assertEqual(result['running'], {'title': 'Bash', 'command': 'echo hi', 'elapsed': '1m 9s'})
        for event in ('PermissionRequest', 'Stop'):
            self.feed(event, 102)
            self.assertIsNone(self.footer()['spinner'])
            self.assertIsNone(self.footer()['running'])

    def test_installer_preserves_groups_mode_and_one_backup(self):
        path = paths.CODEX_HOOKS
        path.parent.mkdir()
        groups = [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command, 'timeout': 20}]
                   } for command in ('superset hook', 'charter hook')]
        original = (json.dumps({'hooks': {'Stop': groups}, 'other': {'enabled': True}}) + '\n').encode()
        path.write_bytes(original)
        path.chmod(0o640)
        self.assertEqual(hooks.install_codex(), list(hooks.CODEX_EVENTS))
        installed = json.loads(path.read_text())
        self.assertEqual(installed['hooks']['Stop'][:2], groups)
        self.assertEqual(installed['other'], {'enabled': True})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        backup = Path(str(path) + '.before-tether')
        self.assertEqual(backup.read_bytes(), original)
        backup_stat = backup.stat()
        first_write = path.read_bytes()
        self.assertEqual(hooks.install_codex(), [])
        self.assertEqual(path.read_bytes(), first_write)
        self.assertEqual(backup.stat().st_mtime_ns, backup_stat.st_mtime_ns)
        self.assertEqual(backup.read_bytes(), original)
        data = json.loads(first_write)
        del data['hooks']['Interrupt']
        path.write_text(json.dumps(data))
        self.assertEqual(hooks.install_codex(), ['Interrupt'])
        self.assertEqual(backup.read_bytes(), original)
        for event in hooks.CODEX_EVENTS:
            handler = installed['hooks'][event][-1]['hooks'][0]
            self.assertEqual(shlex.split(handler['command']),
                             [sys.executable, '-I', str(Path(hooks.__file__).resolve()), 'codex'])
            self.assertEqual(handler['timeout'], 3 if event == 'Interrupt' else 5)
            self.assertNotIn('trusted_hash', handler)

    def test_installer_refuses_malformed_file_without_write(self):
        path = paths.CODEX_HOOKS
        path.parent.mkdir()
        for content in ('{', '[]', '{"hooks":{"Stop":{}}}'):
            path.write_text(content)
            with self.assertRaises(ValueError):
                hooks.install_codex()
            self.assertEqual(path.read_text(), content)
            self.assertFalse(Path(str(path) + '.before-tether').exists())

    def test_cli_installs_only_in_overridden_path(self):
        env = dict(os.environ, MOBILE_TMUX_CODEX_HOOKS=str(paths.CODEX_HOOKS))
        for expected in ('Added Codex hooks:', 'Codex hooks already installed.'):
            run = subprocess.run([sys.executable, hooks.__file__, 'install-codex'], env=env,
                                 text=True, capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn(expected, run.stdout)
            self.assertEqual(run.stderr, '')
        self.assertEqual(set(json.loads(paths.CODEX_HOOKS.read_text())['hooks']), set(hooks.CODEX_EVENTS))

    def test_codex_main_only_prompt_prints_rules_under_isolated_python(self):
        for event in hooks.CODEX_EVENTS:
            run = subprocess.run([sys.executable, '-I', hooks.__file__, 'codex'],
                                 input=json.dumps({'session_id': self.entry['thread'], 'hook_event_name': event}),
                                 env=dict(os.environ, TETHER_CDP_URL='http://localhost:9222',
                                          HOME=str(self.root), MOBILE_TMUX_HOME=str(self.root),
                                          MOBILE_TMUX_CONFIG=str(self.root / 'config'),
                                          TETHER_HOME=str(self.root / 'tether'), CODEX_HOME=str(self.root / 'codex'),
                                          MOBILE_TMUX_TMUX='/bin/false'),
                                 text=True, capture_output=True, timeout=5)
            self.assertEqual((run.returncode, run.stderr), (0, ''), event)
            if event == 'UserPromptSubmit':
                output = json.loads(run.stdout)['hookSpecificOutput']
                self.assertEqual(output['hookEventName'], event)
                self.assertIn('Autonomy level 2', output['additionalContext'])
                self.assertNotIn('browser', output['additionalContext'].lower())
            else:
                self.assertEqual(run.stdout, '', event)
        self.assertEqual(json.loads(self.state().read_text())['agent'], 'codex')

    def test_codex_prompt_rules_follow_capture_and_exceptions_are_quiet(self):
        payload = {'session_id': self.entry['thread'], 'hook_event_name': 'UserPromptSubmit'}
        order = []
        with patch.object(sys, 'argv', [hooks.__file__, 'codex']), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(hooks, 'capture', side_effect=lambda *a, **k: order.append('capture')), \
                patch.object(autonomy, 'hook_output', side_effect=lambda *a, **k: order.append('rules') or '{}') as output, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            hooks.main()
        self.assertEqual(order, ['capture', 'rules'])
        output.assert_called_once_with(payload, agent='codex')
        self.assertEqual(stdout.getvalue(), '{}\n')
        for raw in (json.dumps(payload), 'null', '[]', '{bad',
                    json.dumps(dict(payload, session_id='../invalid'))):
            with patch.object(sys, 'argv', [hooks.__file__, 'codex']), \
                    patch.object(sys, 'stdin', io.StringIO(raw)), \
                    patch.object(hooks, 'capture'), \
                    patch.object(autonomy, 'hook_output', side_effect=RuntimeError('broken')), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                hooks.main()
            self.assertEqual(stdout.getvalue(), '')
        with patch.object(sys, 'argv', [hooks.__file__, 'codex']), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                patch.object(hooks, 'capture', side_effect=RuntimeError('broken')), \
                patch.object(autonomy, 'hook_output') as output, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            hooks.main()
        self.assertEqual(stdout.getvalue(), '')
        output.assert_not_called()


if __name__ == '__main__':
    unittest.main()
