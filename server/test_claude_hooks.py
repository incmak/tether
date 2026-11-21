"""Hook capture uses isolated cache files; footer reads never touch tmux."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import claude_hooks
import footer
import paths
import server
from agent_browser_cli import note
from own_time import own_time


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.transcript = self.cache / 'chat.jsonl'
        self.transcript.write_text('')
        self.config = {'agent': 'claude', 'thread': 'abc123', 'transcript': str(self.transcript)}
        self.screen = footer.parse('─' * 80 + '\n❯\n' + '─' * 80 + '\n⏸ plan mode on')
        self.screen.update(spinner={'verb': 'Old…', 'detail': '9s'}, running={'title': 'Old', 'command': 'old', 'elapsed': '9s'})
        self.env = patch.dict(os.environ, MOBILE_TMUX_CACHE=str(self.cache))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path_patch = patch.object(paths, 'CACHE', self.cache)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def feed(self, event, now=100, **extra):
        claude_hooks.capture(dict(session_id='abc123', hook_event_name=event, **extra), now)

    def read(self, now=110):
        return claude_hooks.overlay(self.config, self.screen, now)

    def test_turn_tool_and_mode_contract(self):
        self.screen.update(spinner=None, running=None)
        self.feed('UserPromptSubmit', permission_mode='acceptEdits', effort={'level': 'xhigh'})
        self.feed('PreToolUse', 101, tool_name='Bash', tool_use_id='tool-1', tool_input={'command': 'npm test'})
        result = self.read()
        self.assertEqual(set(result), set(self.screen))
        self.assertEqual(footer.mode_key(result['mode']), 'accept')
        self.assertEqual(result['effort'], 'xhigh')
        self.assertEqual(result['spinner'], {'verb': 'Working…', 'detail': '10s'})
        self.assertEqual(result['running'], {'title': 'Bash', 'command': 'npm test', 'elapsed': '9s'})
        self.assertEqual(result['suggestion'], self.screen['suggestion'])
        self.feed('PostToolUse', 111, tool_name='Bash', tool_use_id='tool-1')
        self.assertIsNone(self.read(112)['running'])
        self.assertIsNotNone(self.read(112)['spinner'])
        self.feed('Stop', 113)
        self.assertIsNone(self.read(114)['spinner'])

    def test_busy_hook_keeps_screen_spinner(self):
        self.screen['spinner'] = {'verb': 'Misting…', 'detail': '1m 8s · ↓ 3.1k tokens'}
        self.feed('UserPromptSubmit')
        self.assertEqual(self.read()['spinner'], self.screen['spinner'])

    def test_idle_hook_clears_screen_spinner(self):
        self.feed('Stop')
        self.assertIsNone(self.read()['spinner'])

    def test_tool_description_becomes_running_title(self):
        self.screen['running'] = None
        for tool_name in ('Bash', 'Agent'):
            with self.subTest(tool_name=tool_name):
                self.feed('PreToolUse', tool_name=tool_name, tool_use_id='one',
                          tool_input={'command': 'ls', 'description': '  List\nfiles  '})
                stored = json.loads((self.cache / 'hooks/abc123.json').read_text())
                self.assertEqual(stored['tools']['one']['description'], 'List files')
                self.assertEqual(self.read()['running'],
                                 {'title': 'List files', 'command': 'ls', 'elapsed': '10s'})

    def test_tool_without_description_uses_tool_name(self):
        self.screen['running'] = None
        self.feed('PreToolUse', tool_name='Bash', tool_use_id='one', tool_input={'command': 'ls'})
        stored = json.loads((self.cache / 'hooks/abc123.json').read_text())
        self.assertEqual(stored['tools']['one']['description'], '')
        self.assertEqual(self.read()['running'], {'title': 'Bash', 'command': 'ls', 'elapsed': '10s'})

    def test_screen_running_title_wins_but_hook_command_and_elapsed_remain(self):
        self.screen['running'] = {'title': 'Exact terminal text', 'command': 'screen command', 'elapsed': '99s'}
        self.feed('PreToolUse', tool_name='Bash', tool_input={'command': 'ls', 'description': 'List files'})
        self.assertEqual(self.read()['running'],
                         {'title': 'Exact terminal text', 'command': 'ls', 'elapsed': '10s'})

    def test_empty_screen_title_falls_back_to_hook_description(self):
        self.screen['running']['title'] = ''
        self.feed('PreToolUse', tool_name='Agent', tool_input={'description': 'Find files'})
        self.assertEqual(self.read()['running'],
                         {'title': 'Find files', 'command': 'Find files', 'elapsed': '10s'})

    def test_old_tool_state_without_description_uses_tool_name(self):
        self.screen['running'] = None
        self.feed('PreToolUse', tool_name='Bash', tool_use_id='one', tool_input={'command': 'ls'})
        target = self.cache / 'hooks/abc123.json'
        state = json.loads(target.read_text())
        del state['tools']['one']['description']
        target.write_text(json.dumps(state))
        self.assertEqual(self.read()['running'], {'title': 'Bash', 'command': 'ls', 'elapsed': '10s'})

    def test_failures_waiting_and_idle(self):
        self.feed('UserPromptSubmit')
        self.feed('PreToolUse', 101, tool_name='Read', tool_use_id='one', tool_input={'file_path': '/example/file'})
        self.feed('PermissionRequest', 102, tool_name='Read')
        self.assertIsNone(self.read()['spinner'])
        self.assertIsNone(self.read()['running'])
        self.feed('PermissionDenied', 103, tool_name='Read')
        self.assertIsNotNone(self.read()['spinner'])
        self.assertIsNone(self.read()['running'])
        self.feed('Notification', 104, notification_type='permission_prompt')
        self.assertIsNone(self.read()['spinner'])
        self.feed('PostToolUseFailure', 105, tool_name='Read', tool_use_id='one')
        self.assertIsNotNone(self.read()['spinner'])
        self.feed('StopFailure', 106, error='rate_limit')
        self.assertIsNone(self.read()['spinner'])
        self.feed('UserPromptSubmit', 107)
        self.feed('Notification', 108, notification_type='idle_prompt')
        self.assertIsNone(self.read()['spinner'])

    def test_denied_block_facts_preserve_existing_notice(self):
        self.feed('UserPromptSubmit', 100)
        self.feed('PermissionDenied', 101, tool_name='Bash',
                  tool_input={'command': 'curl x', 'description': 'Fetch page'},
                  reason='Auto mode: [Bot Check Circumvention]')
        result = self.read()
        self.assertEqual(result['blocked'], {
            'tool': 'Bash', 'description': 'Fetch page', 'command': 'curl x',
            'reason': 'Bot Check Circumvention', 'at': 101})
        self.assertEqual(result['notices'], [
            'Bash(curl x) blocked by auto mode · /permissions · Auto mode: '
            'Auto mode: [Bot Check Circumvention]'])
        state = json.loads((self.cache / 'hooks/abc123.json').read_text())
        self.assertEqual(state['denied']['description'], 'Fetch page')
        self.assertEqual(state['denied']['command'], 'curl x')
        self.assertNotIn('blocked', self.read(101 + claude_hooks.TURN_MAX_AGE + 1))
        self.feed('UserPromptSubmit', 111)
        self.assertNotIn('blocked', self.read(112))

    def test_old_denied_state_defaults_missing_block_facts_to_empty(self):
        self.feed('PermissionDenied', tool_name='Read', tool_input={'file_path': '/example/file'},
                  reason='[File access]')
        target = self.cache / 'hooks/abc123.json'
        state = json.loads(target.read_text())
        state['denied'].pop('description', None)
        state['denied'].pop('command', None)
        target.write_text(json.dumps(state))
        self.assertEqual(self.read()['blocked'], {
            'tool': 'Read', 'description': '', 'command': '', 'reason': 'File access', 'at': 100})

    def test_block_command_fallbacks_and_short_limit(self):
        for tool, command in [({'command': 'curl x', 'url': 'https://example.test'}, 'curl x'),
                              ({'url': 'https://example.test', 'file_path': '/example/file'}, 'https://example.test'),
                              ({'file_path': '/example/file'}, '/example/file'), ({}, ''),
                              ({'command': 'x' * 200}, 'x' * 160)]:
            with self.subTest(tool=tool):
                self.feed('PermissionDenied', tool_name='Tool', tool_input=tool, reason='Auto mode: [Rule]')
                blocked = self.read()['blocked']
                self.assertEqual(blocked['command'], command)
                self.assertEqual(blocked['description'], '')
                self.assertEqual(blocked['reason'], 'Rule')

    def test_subagent_does_not_change_main_context_or_end_turn(self):
        self.feed('UserPromptSubmit', permission_mode='plan', effort={'level': 'high'})
        self.feed('SubagentStart', 101, agent_id='agent-one', agent_type='Explore')
        self.feed('PreToolUse', 102, agent_id='agent-one', permission_mode='bypassPermissions', effort={'level': 'low'}, tool_name='Bash')
        self.assertEqual(self.read()['effort'], 'high')
        self.assertEqual(footer.mode_key(self.read()['mode']), 'plan')
        self.assertEqual(self.read()['agents'], [{'name': 'Explore', 'task': '', 'detail': '9s', 'main': False}])
        self.feed('Stop', 103)
        self.assertEqual(len(self.read()['agents']), 1)
        self.feed('SubagentStop', 102, agent_id='agent-one')  # async removal may arrive after the parent's Stop
        self.assertEqual(self.read()['agents'], [])
        self.feed('UserPromptSubmit', 105)
        self.feed('Stop', 106, agent_id='agent-two')
        self.assertIsNotNone(self.read()['spinner'])

    def test_overlapping_tools_complete_by_identity(self):
        self.screen['running'] = None
        self.feed('PreToolUse', tool_name='Bash', tool_use_id='one')
        self.feed('PreToolUse', 101, tool_name='Read', tool_use_id='two')
        self.feed('PostToolUse', 102, tool_use_id='one')
        self.assertEqual(self.read()['running']['title'], 'Read')
        self.feed('PostToolUseFailure', 103, tool_use_id='two')
        self.assertIsNone(self.read()['running'])

    def test_stop_captures_tasks_and_crons_with_short_labels(self):
        tasks = [
            {'id': 's1', 'type': 'shell', 'status': 'running', 'description': 'Watch logs', 'command': 'tail -f logs'},
            {'id': 's2', 'type': 'shell', 'status': 'running', 'command': 'npm test'},
            {'id': 'm1', 'type': 'monitor', 'status': 'running', 'server': 'build', 'tool': 'watch'},
            {'id': 'a1', 'type': 'subagent', 'status': 'running', 'agent_type': 'Explore'},
            {'id': 'w1', 'type': 'workflow', 'name': 'Build'},
        ]
        crons = [{'id': 'c1', 'schedule': '* * * * *', 'recurring': False, 'prompt': 'Check build'}]
        self.feed('Stop', background_tasks=tasks, session_crons=crons)
        stored = json.loads((self.cache / 'hooks/abc123.json').read_text())
        self.assertEqual(stored['session_crons'], crons)
        for task in tasks:
            self.assertEqual(stored['background_tasks'][task['id']], dict(task, started=100))
        result = self.read()
        self.assertEqual(result['tasks'], ['2 shells', '1 monitor', '1 agent', '1 workflow'])
        self.assertEqual(result['task_details'][0], dict(tasks[0], started=100))
        self.assertEqual(result['task_details'][2], {'id': 'm1', 'type': 'monitor', 'status': 'running', 'started': 100})
        self.assertEqual(self.screen['tasks'], [])

    def test_stop_empty_list_clears_tasks_and_missing_list_preserves_them(self):
        self.screen['tasks'] = ['2 shells']
        self.feed('Stop', background_tasks=[{'id': 'one', 'type': 'shell'}], session_crons=[{'id': 'cron'}])
        self.feed('StopFailure', 101)
        self.assertEqual(self.read()['tasks'], ['1 shell'])
        self.feed('Stop', 102, background_tasks=[], session_crons=[])
        self.assertEqual(self.read()['tasks'], [])
        self.assertEqual(self.read()['task_details'], [])
        self.assertEqual(json.loads((self.cache / 'hooks/abc123.json').read_text())['session_crons'], [])
        self.feed('SessionStart', 103)
        self.assertEqual(self.read()['tasks'], self.screen['tasks'])
        self.assertNotIn('task_details', self.read())

    def test_background_shell_start_is_replaced_and_keeps_elapsed_origin(self):
        command = 'printf "line one\\nline two" ' + 'x' * 300
        self.feed('PreToolUse', tool_name='Bash', tool_use_id='one',
                  tool_input={'run_in_background': True, 'command': command, 'description': 'Watch build'})
        self.feed('PostToolUse', 101, tool_use_id='one')
        detail = self.read()['task_details'][0]
        self.assertEqual(detail, {'id': 'pending:one', 'type': 'shell', 'status': 'running',
                                  'command': command, 'description': 'Watch build', 'started': 100})
        self.feed('Stop', 102, background_tasks=[{'id': 'real-one', 'type': 'shell', 'command': command, 'status': 'running'}])
        self.assertEqual(self.read()['task_details'], [{'id': 'real-one', 'type': 'shell', 'command': command,
                                                       'status': 'running', 'started': 100}])
        self.feed('Stop', 103, background_tasks=[])
        self.assertEqual(self.read()['task_details'], [])

    def test_only_explicit_background_bash_is_captured(self):
        for tool, background in [('Bash', False), ('Bash', 'true'), ('Read', True)]:
            self.feed('PreToolUse', tool_name=tool, tool_input={'run_in_background': background})
            self.assertNotIn('task_details', self.read())

    def test_subagent_snapshot_is_parent_scoped_and_ordered(self):
        self.feed('UserPromptSubmit')
        self.feed('PreToolUse', 101, agent_id='one', tool_name='Bash', tool_use_id='bg',
                  tool_input={'run_in_background': True, 'command': 'npm test'})
        self.assertEqual(self.read()['tasks'], ['1 shell'])
        self.feed('SubagentStop', 102, agent_id='one', background_tasks=[{'id': 'm', 'type': 'monitor'}], session_crons=[])
        self.assertEqual(self.read()['tasks'], ['1 monitor'])
        self.assertIsNotNone(self.read()['spinner'])
        self.feed('StopFailure', 103, background_tasks=[], session_crons=[])
        self.feed('SubagentStop', 102, agent_id='two', background_tasks=[{'id': 'old', 'type': 'shell'}])
        self.assertEqual(self.read()['task_details'], [])

    def test_old_task_data_falls_back_but_next_prompt_keeps_it(self):
        self.screen['tasks'] = ['2 shells']
        self.feed('Stop', background_tasks=[{'id': 'one', 'type': 'monitor'}])
        self.assertEqual(self.read()['tasks'], ['1 monitor'])
        self.feed('PostModelSwitch', 1900, effort={'level': 'max'})
        self.assertEqual(self.read(1901)['tasks'], self.screen['tasks'])
        self.assertNotIn('task_details', self.read(1901))
        self.transcript.write_text(json.dumps({'type': 'user', 'timestamp': '1970-01-01T00:01:45Z',
                                               'message': {'content': 'Next prompt'}}) + '\n')
        # The Stop list stays true during the next turn, until the next Stop.
        self.assertEqual(self.read()['tasks'], ['1 monitor'])

    def test_freshness_and_non_claude_fallback(self):
        self.assertEqual(self.read(), self.screen)
        self.feed('UserPromptSubmit')
        self.assertEqual(self.read(1901), self.screen)
        self.assertEqual(self.read(99), self.screen)
        self.assertEqual(claude_hooks.overlay(dict(self.config, agent='codex'), self.screen, 110), self.screen)
        self.assertEqual(claude_hooks.overlay(dict(self.config, thread='other'), self.screen, 110), self.screen)
        self.feed('SessionEnd', 105)
        self.assertEqual(self.read(), self.screen)
        self.feed('SessionStart', 106)
        self.assertIsNone(self.read()['spinner'])

    def test_newer_transcript_invalidates_missing_hooks_but_not_streaming(self):
        self.screen['running'] = None
        self.feed('UserPromptSubmit', permission_mode='acceptEdits', effort={'level': 'max'})
        self.feed('PreToolUse', 101, tool_name='Bash', tool_use_id='one')
        record = {'type': 'assistant', 'timestamp': '1970-01-01T00:01:45Z',
                  'message': {'role': 'assistant', 'content': [], 'stop_reason': 'tool_use'}}
        self.transcript.write_text(json.dumps(record) + '\n')
        self.assertEqual(self.read(3700)['running']['title'], 'Bash')
        record['message']['stop_reason'] = 'end_turn'
        self.transcript.write_text(json.dumps(record) + '\n')
        result = self.read(3700)
        self.assertEqual(result['spinner'], self.screen['spinner'])
        self.assertEqual(result['running'], self.screen['running'])
        self.assertEqual(footer.mode_key(result['mode']), 'accept')
        self.assertEqual(result['effort'], 'max')
        record.update(type='user', message={'role': 'user', 'content': 'next prompt'})
        self.transcript.write_text(json.dumps(record) + '\n')
        self.assertEqual(self.read(3700)['running'], self.screen['running'])

    def test_compact_summary_does_not_invalidate_the_running_turn(self):
        for flag in ('isCompactSummary', 'isAutoCompactSummary'):
            for origin in ('human', 'task_notification'):
                with self.subTest(flag=flag, turnOrigin=origin):
                    self.screen['running'] = None
                    self.feed('UserPromptSubmit')
                    self.feed('PreToolUse', 101, tool_name='Bash', tool_use_id='one')
                    summary = {'type': 'user', flag: True, 'isVisibleInTranscriptOnly': True, 'turnOrigin': origin,
                               'timestamp': '1970-01-01T00:01:45Z',
                               'message': {'role': 'user', 'content': 'This session is being continued.'}}
                    self.transcript.write_text(json.dumps(summary) + '\n')
                    self.assertEqual(self.read(3700)['running']['title'], 'Bash')

    def test_delayed_settings_cannot_revive_or_overwrite_newer_state(self):
        self.feed('UserPromptSubmit', permission_mode='plan')
        self.feed('Stop', 102, permission_mode='auto')
        self.feed('PostModelSwitch', 101, permission_mode='default', effort={'level': 'low'})
        self.assertEqual(footer.mode_key(self.read()['mode']), 'auto')
        self.assertIsNone(self.read()['spinner'])
        self.feed('PostModelSwitch', 109, effort={'level': 'max'})
        self.assertEqual(self.read()['effort'], 'max')
        result = self.read(1903)  # settings never refresh lifecycle age
        self.assertEqual(result['spinner'], self.screen['spinner'])
        self.assertEqual(result['effort'], 'max')
        self.feed('UserPromptSubmit', 100)
        self.assertIsNone(self.read()['spinner'])

    def test_idle_settings_have_no_age_limit_but_require_live_identity(self):
        self.feed('Stop', permission_mode='acceptEdits', effort={'level': 'high'})
        result = self.read(86500)
        self.assertEqual(footer.mode_key(result['mode']), 'accept')
        self.assertEqual(result['effort'], 'high')
        self.assertEqual(result['spinner'], self.screen['spinner'])
        self.assertEqual(claude_hooks.overlay(dict(self.config, ambiguous=True), self.screen, 86500), self.screen)
        target = self.cache / 'hooks/abc123.json'
        state = json.loads(target.read_text())
        state['session_id'] = 'other'
        target.write_text(json.dumps(state))
        self.assertEqual(self.read(86500), self.screen)
        self.feed('SessionEnd', 102, permission_mode='auto')
        self.assertEqual(self.read(86500), self.screen)

    def test_later_confident_screen_mode_handles_shift_tab_without_a_hook(self):
        self.feed('Stop', permission_mode='acceptEdits', effort={'level': 'max'})
        for read_at in (None, 99, 100):
            result = claude_hooks.overlay(self.config, self.screen, 3700, screen_read_at=read_at)
            self.assertEqual(footer.mode_key(result['mode']), 'accept')
        result = claude_hooks.overlay(self.config, self.screen, 3700, screen_read_at=3699)
        self.assertEqual(result['mode'], self.screen['mode'])
        self.assertEqual(result['effort'], 'max')
        clipped = footer.parse('permission dialog without a footer')
        result = claude_hooks.overlay(self.config, clipped, 3700, screen_read_at=3699)
        self.assertEqual(footer.mode_key(result['mode']), 'accept')
        self.feed('PostModelSwitch', 3700, permission_mode='auto')
        result = claude_hooks.overlay(self.config, self.screen, 3701, screen_read_at=3699)
        self.assertEqual(footer.mode_key(result['mode']), 'auto')

    def test_each_main_event_with_permission_mode_updates_settings(self):
        for event in claude_hooks.EVENTS:
            if event in ('SubagentStart', 'SubagentStop'):
                continue  # subagent context cannot replace parent settings
            state = {}
            claude_hooks.update(state, {'hook_event_name': event, 'permission_mode': 'auto'}, 100)
            self.assertEqual(state['mode'], claude_hooks.MODES['auto'], event)
        self.feed('Stop', permission_mode='plan')
        self.feed('Notification', 102, notification_type='other', permission_mode='acceptEdits')
        self.assertEqual(footer.mode_key(self.read()['mode']), 'accept')
        self.assertIsNone(self.read()['spinner'])

    def test_open_tool_has_no_age_limit_and_post_tool_restores_turn_limit(self):
        self.screen['running'] = None
        self.feed('UserPromptSubmit')
        self.feed('PreToolUse', 101, tool_name='Bash', tool_use_id='one', tool_input={'command': 'test suite'})
        for now in (701, 3701):
            result = self.read(now)
            self.assertIsNotNone(result['spinner'])
            self.assertEqual(result['running']['title'], 'Bash')
            self.assertEqual(result['running']['elapsed'], claude_hooks.elapsed(101, now))
        self.feed('PostToolUse', 3702, tool_use_id='one')
        self.assertIsNone(self.read(3703)['running'])
        self.assertEqual(self.read(5503)['running'], self.screen['running'])

    def test_slow_reply_without_open_tool_has_bounded_freshness(self):
        self.feed('UserPromptSubmit', effort={'level': 'high'})
        for now in (401, 1000, 1900):
            self.assertIsNotNone(self.read(now)['spinner'])
            self.assertIsNone(self.read(now)['running'])
        result = self.read(1901)
        self.assertEqual(result['spinner'], self.screen['spinner'])
        self.assertEqual(result['running'], self.screen['running'])
        self.assertEqual(result['effort'], 'high')

    def test_hook_subagents_replace_screen_agent_rows(self):
        self.screen = footer.parse('─' * 80 + '\n❯\n' + '─' * 80 +
                                   '\n  ● main\n  ◯ general-purpose  Find the bug  47m 3s')
        self.feed('Stop', background_tasks=[{'id': 'a1', 'type': 'subagent', 'status': 'running', 'description': 'Find the bug'}])
        result = self.read()
        self.assertEqual(result['tasks'], ['1 agent'])
        self.assertEqual([row['name'] for row in result['agents']], ['main'])

    def test_screen_agents_survive_empty_hooks_at_any_age(self):
        self.screen = footer.parse('─' * 80 + '\n❯\n' + '─' * 80 +
                                   '\n  ● main\n  ◯ general-purpose  Find the bug  47m 3s')
        self.assertEqual(len(self.screen['agents']), 2)
        self.feed('Stop')
        for now in (110, 401, 3700):
            self.assertEqual(self.read(now)['agents'], self.screen['agents'])
        self.feed('SubagentStart', 101, agent_id='one', agent_type='Explore')
        self.feed('SubagentStart', 102, agent_id='two', agent_type='general-purpose')
        result = self.read(3700)
        self.assertEqual(result['agents'][:2], self.screen['agents'])
        self.assertEqual([row['name'] for row in result['agents']], ['main', 'general-purpose', 'Explore'])
        self.feed('SubagentStop', 103, agent_id='one')
        self.assertEqual(self.read(3700)['agents'], self.screen['agents'])
        self.feed('SubagentStart', 104, agent_id='three', agent_type='Explore')
        self.feed('SubagentStart', 105, agent_id='four', agent_type='Explore')
        self.assertEqual([row['name'] for row in self.read(3700)['agents']],
                         ['main', 'general-purpose', 'Explore', 'Explore'])

    def test_set_mode_keeps_instant_screen_reads(self):
        self.feed('Stop', permission_mode='plan')
        screen = dict(self.screen, mode='')
        presses = []
        def press():
            presses.append('BTab')
            screen['mode'] = '⏵⏵ accept edits on'
        with patch.object(claude_hooks, 'overlay', side_effect=AssertionError('mode control must read the screen')):
            self.assertEqual(footer.set_mode(lambda: screen, press, 'accept', sleep=lambda _: None), {'mode': 'accept'})
        self.assertEqual(presses, ['BTab'])

    def test_all_modes_match_existing_ui_keys(self):
        for mode, key in [('default', 'default'), ('dontAsk', 'default'), ('acceptEdits', 'accept'),
                          ('plan', 'plan'), ('auto', 'auto'), ('bypassPermissions', 'bypass')]:
            self.feed('UserPromptSubmit', permission_mode=mode)
            self.assertEqual(footer.mode_key(self.read()['mode']), key)

    def test_script_is_quiet_private_and_fail_open(self):
        script = str(Path(claude_hooks.__file__))
        for payload in ['invalid JSON', 'null', '[]', '{"session_id":"../escape","hook_event_name":"Stop"}',
                        json.dumps({'session_id': 'abc123', 'hook_event_name': 'UserPromptSubmit'})]:
            run = subprocess.run([sys.executable, script], input=payload, text=True, capture_output=True, timeout=2)
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, '', ''))
        target = self.cache / 'hooks/abc123.json'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(target.parent.glob('*.tmp')), [])
        self.assertIsNotNone(self.read(time.time())['spinner'])
        blocked = self.cache / 'blocked'
        blocked.write_text('not a directory')
        with patch.dict(os.environ, MOBILE_TMUX_CACHE=str(blocked)):
            run = subprocess.run([sys.executable, script], input=json.dumps({'session_id': 'abc123', 'hook_event_name': 'Stop'}),
                                 text=True, capture_output=True, timeout=2)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, '', ''))
        target.write_text('broken')
        self.assertEqual(self.read(), self.screen)

    def test_shared_browser_context_only_after_session_start_capture(self):
        for event in claude_hooks.EVENTS:
            for enabled in (False, True):
                with self.subTest(event=event, enabled=enabled):
                    env = dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x' if enabled else '')
                    payload = {'session_id': 'abc123', 'hook_event_name': event}
                    run = subprocess.run([sys.executable, claude_hooks.__file__], input=json.dumps(payload),
                                         text=True, capture_output=True, env=env, timeout=2)
                    self.assertEqual((run.returncode, run.stderr), (0, ''))
                    if event == 'SessionStart' and enabled:
                        self.assertEqual(len(run.stdout.splitlines()), 1)
                        self.assertEqual(json.loads(run.stdout), {'hookSpecificOutput': {
                            'hookEventName': 'SessionStart', 'additionalContext': note()}})
                        state = json.loads((self.cache / 'hooks/abc123.json').read_text())
                        self.assertFalse(state['busy'])
                    else:
                        self.assertEqual(run.stdout, '')

    def test_shared_browser_hook_errors_are_quiet(self):
        blocked = self.cache / 'blocked'
        blocked.write_text('not a directory')
        for payload in ('invalid JSON', 'null', '[]',
                        '{"session_id":"../bad","hook_event_name":"SessionStart"}',
                        '{"session_id":"abc123","hook_event_name":"SessionStart"}'):
            run = subprocess.run([sys.executable, claude_hooks.__file__], input=payload, text=True, capture_output=True,
                                 env=dict(os.environ, TETHER_CDP_URL='ws://127.0.0.1:1/cdp/x',
                                          MOBILE_TMUX_CACHE=str(blocked)), timeout=2)
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, '', ''))

    def test_footer_endpoint_uses_hook_script_samples(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/mobile/api/footer?session=%25test'
        results = []
        handler.deny = lambda: False
        handler.respond = lambda status, data: results.append((status, data))
        def request():
            with patch.object(server, 'choose', return_value=self.config), patch.object(server, 'capture', return_value=''):
                handler.do_GET()
            self.assertEqual(results[-1][0], 200)
            return results[-1][1]
        def sample(event, **extra):
            payload = dict(session_id='abc123', transcript_path=str(self.transcript), cwd='/example/project',
                           permission_mode='default', hook_event_name=event, **extra)
            run = subprocess.run([sys.executable, claude_hooks.__file__], input=json.dumps(payload),
                                 text=True, capture_output=True, timeout=2)
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, '', ''))
        self.assertIsNone(request()['spinner'])
        sample('UserPromptSubmit', prompt='Write a factorial function')
        self.assertIsNotNone(request()['spinner'])
        sample('PreToolUse', tool_name='Bash', tool_use_id='tool-1', tool_input={'command': 'npm test'}, effort={'level': 'high'})
        self.assertEqual(request()['running']['command'], 'npm test')
        self.assertEqual(request()['effort'], 'high')
        sample('PermissionRequest', tool_name='Bash', tool_input={'command': 'npm test'})
        self.assertIsNone(request()['spinner'])
        sample('PostToolUse', tool_name='Bash', tool_use_id='tool-1', tool_input={'command': 'npm test'}, tool_response={'success': True})
        self.assertIsNotNone(request()['spinner'])
        self.assertIsNone(request()['running'])
        sample('Stop', stop_hook_active=False, last_assistant_message='Done')
        self.assertIsNone(request()['spinner'])


    def test_footer_endpoint_dates_capture_before_overlay(self):
        self.feed('Stop', permission_mode='acceptEdits')
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/mobile/api/footer?session=%25test'
        results = []
        handler.deny = lambda: False
        handler.respond = lambda status, data: results.append((status, data))
        screen = '─' * 80 + '\n❯\n' + '─' * 80 + '\n⏸ plan mode on'
        for captured, expected in ((99, 'accept'), (109, 'plan')):
            with patch.object(server, 'choose', return_value=self.config), patch.object(server, 'capture', return_value=screen), \
                    own_time(server, time=Mock(side_effect=[captured, 110])):
                handler.do_GET()
            self.assertEqual(results[-1][0], 200)
            self.assertEqual(footer.mode_key(results[-1][1]['mode']), expected)


if __name__ == '__main__':
    unittest.main()
