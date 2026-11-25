"""Claude notice hooks use fixture payloads and an isolated cache, never tmux."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import claude_hooks
import paths
import quota_notices


class ClaudeNoticeHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.entry = {'agent': 'claude', 'thread': 'notice-session'}
        for override in (patch.dict(os.environ, MOBILE_TMUX_CACHE=str(self.cache)),
                         patch.object(paths, 'CACHE', self.cache),
                         patch.object(quota_notices, 'CACHE', self.cache / 'notices')):
            override.start()
            self.addCleanup(override.stop)
        self.feed('UserPromptSubmit', 100)

    def feed(self, event, now=101, **extra):
        claude_hooks.capture(dict(session_id=self.entry['thread'], hook_event_name=event, **extra), now)

    def deny(self, now=101, **extra):
        self.feed('PermissionDenied', now, tool_name='Bash', tool_use_id='one',
                  tool_input={'command': 'git push --force', 'description': 'Push changes'},
                  reason='[Data Exfiltration]', **extra)

    def overlay(self, now=110, **screen):
        return claude_hooks.overlay(self.entry, screen, now)

    def test_permission_denied_notice_matches_ui_regex(self):
        self.feed('PreToolUse', 100.5, tool_name='Bash', tool_use_id='one')
        self.feed('PermissionRequest', 100.6)
        self.deny()
        notice = self.overlay()['notices'][0]
        self.assertEqual(notice, 'Bash(git push --force) blocked by auto mode · /permissions'
                         ' · Auto mode: [Data Exfiltration]')
        # The two parsing expressions from ui/src/noticeText.ts, kept verbatim.
        match = re.match(r'^(.+?) blocked by auto mode(?:\s*·\s*\/permissions)?'
                         r'(?:\s*·\s*auto mode:\s*(.*))?$', notice, re.I)
        call = re.match(r'^(\w+)\((.+)\)$', match[1])
        self.assertEqual(call.groups(), ('Bash', 'git push --force'))
        self.assertEqual(match[2], '[Data Exfiltration]')
        self.assertIsNotNone(self.overlay()['spinner'])
        self.assertIsNone(self.overlay()['running'])

    def test_screen_notice_is_replaced_without_mutating_screen(self):
        self.deny()
        screen = ['Keep this notice', 'bash blocked by AUTO MODE · /permissions']
        result = self.overlay(notices=screen)
        self.assertEqual(len(result['notices']), 2)
        self.assertEqual(result['notices'][0], screen[0])
        self.assertIn('Bash(git push --force)', result['notices'][1])
        self.assertEqual(screen[1], 'bash blocked by AUTO MODE · /permissions')

    def test_notice_includes_subagent_snapshot_return_path(self):
        self.deny()
        self.feed('Stop', 102, background_tasks=[{'id': 'agent-one', 'type': 'subagent'}])
        self.assertIn('git push --force', self.overlay()['notices'][0])
        self.assertEqual(self.overlay()['tasks'], ['1 agent'])

    def test_new_turn_and_session_events_clear_denial(self):
        for index, event in enumerate(('UserPromptSubmit', 'SessionStart', 'SessionEnd')):
            with self.subTest(event=event):
                now = 100 + index * 10
                self.feed('UserPromptSubmit', now)
                self.deny(now + 1)
                self.assertIn('notices', self.overlay(now + 2))
                self.feed(event, now + 2)
                self.assertNotIn('denied', json.loads((self.cache / 'hooks/notice-session.json').read_text()))
                self.assertNotIn('notices', self.overlay(now + 3))

    def test_subagent_and_out_of_order_denials_are_ignored(self):
        self.deny(agent_id='agent-one')
        self.assertNotIn('notices', self.overlay())
        self.feed('UserPromptSubmit', 102)
        self.deny()
        self.assertNotIn('notices', self.overlay())

    def test_notice_age_is_bounded_even_with_newer_hook_events(self):
        self.deny()
        self.assertIn('notices', self.overlay(101 + claude_hooks.TURN_MAX_AGE))
        self.feed('PostModelSwitch', 1902)
        self.assertNotIn('notices', self.overlay(1902))

    def test_denial_must_be_newer_than_last_prompt(self):
        self.feed('PermissionDenied', 100, tool_name='Read')
        self.assertNotIn('notices', self.overlay())

    def test_detail_priority_shortening_and_incomplete_notice(self):
        values = dict(command='git  push\n--force', file_path='/example/file',
                      pattern='*.py', query='query', description='description')
        for key in list(values):
            with self.subTest(key=key):
                self.feed('PermissionDenied', tool_name='Read', tool_input=values, reason='Classifier unavailable')
                self.assertIn('Read(' + claude_hooks.short(values[key]) + ')', self.overlay()['notices'][0])
                del values[key]
        self.feed('PermissionDenied', tool_name='Read', tool_input={'query': 'x' * 200}, reason='y' * 200)
        stored = json.loads((self.cache / 'hooks/notice-session.json').read_text())['denied']
        self.assertEqual((len(stored['detail']), len(stored['reason'])), (160, 160))
        for extra, expected in (({'tool_input': None, 'reason': 'Classifier unavailable'},
                                 'Bash blocked by auto mode · /permissions · Auto mode: Classifier unavailable'),
                                ({'tool_input': {'command': 'git push'}, 'reason': ''},
                                 'Bash(git push) blocked by auto mode · /permissions')):
            self.feed('PermissionDenied', tool_name='Bash', **extra)
            self.assertEqual(self.overlay()['notices'], [expected])

    def test_quota_events_are_saved_once_and_merged(self):
        for error in ('rate_limit', 'billing_error', 'account_on_hold'):
            with self.subTest(error=error):
                self.entry['thread'] = error.replace('_', '-')
                text = 'API Error: Rate limit reached'
                for _ in range(2):
                    self.feed('StopFailure', error=error, last_assistant_message=text, error_details='429 Too Many Requests')
                notices = quota_notices.saved(self.entry)
                self.assertEqual(len(notices), 1)
                self.assertEqual(notices[0]['text'], text + '\n429 Too Many Requests')
                self.assertEqual(notices[0]['id'], 'quota:' + quota_notices.identity(notices[0]['text']))
                self.assertEqual(quota_notices.merge(self.entry, []), notices)
                self.assertEqual(quota_notices.merge(self.entry, notices), notices)

    def test_overloaded_is_not_a_quota_notice(self):
        self.feed('StopFailure', error='overloaded', last_assistant_message='API Error: overloaded')
        self.assertEqual(quota_notices.saved(self.entry), [])

    def test_fallback_text_and_repeated_details(self):
        for message in (None, '', '   ', 5):
            self.feed('StopFailure', error='rate_limit', last_assistant_message=message,
                      error_details='Usage limit reached.')
        self.assertEqual([notice['text'] for notice in quota_notices.saved(self.entry)], ['Usage limit reached.'])

    def test_quota_text_is_preserved_and_equal_details_are_not_repeated(self):
        text = ' API Error: billing problem\nPlease check your account. '
        self.feed('StopFailure', error='billing_error', last_assistant_message=text, error_details=text)
        self.assertEqual(quota_notices.saved(self.entry)[0]['text'], text)

    def test_hook_process_uses_cache_override_without_server_state(self):
        payload = dict(session_id=self.entry['thread'], hook_event_name='StopFailure', error='rate_limit',
                       last_assistant_message="You've hit your session limit · resets tomorrow")
        for _ in range(2):
            run = subprocess.run([sys.executable, claude_hooks.__file__], input=json.dumps(payload),
                                 env=dict(os.environ), text=True, capture_output=True, timeout=5)
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, '', ''))
        notices = quota_notices.saved(self.entry)
        self.assertEqual(len(notices), 1)
        quota_notices.capture(self.entry, '⎿ ' + payload['last_assistant_message'])
        self.assertEqual(quota_notices.saved(self.entry), notices)
        target = self.cache / 'notices/claude/notice-session.json'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(target.parent.glob('*.tmp')), [])

    def test_quota_storage_errors_do_not_prevent_lifecycle_capture(self):
        with patch.object(quota_notices, 'record', side_effect=OSError('Unavailable')):
            self.feed('StopFailure', error='rate_limit')
        state = json.loads((self.cache / 'hooks/notice-session.json').read_text())
        self.assertFalse(state['busy'])


if __name__ == '__main__':
    unittest.main()
