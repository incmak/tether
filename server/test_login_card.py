"""The /login menu in a narrow pane, the sign-in link screen, and saved accounts from ccswap (with a fake runner)."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import subprocess
import unittest

import saved_accounts
import terminal_input

LOGIN_MENU = '''
 Select login method:

 ❯ 1. Claude account
      with
      subscription ·
      Pro, Max, Team,
      or Enterprise
   2. Anthropic
      Console account
      · API usage
      billing
   3. 3rd-party
      platform ·
      Amazon Bedrock,
      Microsoft
      Foundry, Google
      Vertex AI

 Esc to cancel
'''

URL = ('https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88e'
       'd-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%'
       '2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key&state=i6Gm09FKik_3i')
SIGN_IN = f'''
  Browser didn't open? Use the url below to sign in
  (Copied to tmux buffer · select the URL manually if paste fails)

{URL[:86]}
{URL[86:172]}
{URL[172:]}

  Hold Shift (Option in iTerm2, Fn in Terminal.app) while selecting to use
  your terminal's native copy


  Paste code here if prompted >

  Esc to cancel
'''


def seen(entry, screen):
    terminal_input._attention_seen.clear()
    terminal_input.attention(entry, screen, False, now=0)
    return terminal_input.attention(entry, screen, False, now=10)


class LoginScreens(unittest.TestCase):
    def test_wrapped_menu_with_middle_dot_is_a_menu(self):
        found = seen({'id': '%91', 'agent': 'claude'}, LOGIN_MENU)
        self.assertEqual(found['reason'], 'menu')
        self.assertFalse(found['menu_verified'])
        self.assertNotIn('options', found)

    def test_spinner_below_a_sent_numbered_message_is_still_chat(self):
        screen = ' ❯ 1. Do what is compliant\n\n· Thinking…\n'
        self.assertFalse(terminal_input.cursor_in_menu(screen.splitlines(), '❯'))

    def test_sign_in_screen_gives_the_whole_url(self):
        found = seen({'id': '%92', 'agent': 'claude'}, SIGN_IN)
        self.assertEqual(found['reason'], 'prompt')
        self.assertEqual(found['login'], {'url': URL, 'code': True})

    def test_other_links_are_not_sign_in(self):
        self.assertIsNone(terminal_input.login_link(['https://example.com/oauth/x', '', 'Esc to cancel']))


class SavedAccounts(unittest.TestCase):
    DATA = {'claude': {'accounts': [
        {'number': 1, 'email': 'a@x', 'organizationName': 'A', 'active': True, 'usageStatus': 'ok', 'usage': {'sevenDay': {'pct': 21.0, 'resetsAt': '2026-11-06T13:30:00Z'}}},
        {'number': 2, 'email': 'b@x', 'organizationName': 'B', 'active': False, 'usageStatus': 'ok', 'usage': {'fiveHour': {'pct': 0.0}, 'sevenDay': {'pct': 99.0}}}]},
        'codex': {'accounts': [{'number': 1, 'email': 'a@x', 'planType': 'pro', 'active': True}]}}

    def runner(self, calls, code=0):
        def run(command, **_):
            calls.append(command[1:])
            return subprocess.CompletedProcess(command, code if command[-2:] != ['list', '--json'] else 0, json.dumps(self.DATA), 'boom')
        return run

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        self.old = saved_accounts.binary
        saved_accounts.binary = lambda: '/fake/ccswap'
        # A switch records its time in the cache folder: keep it out of the real one.
        self.cache = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(saved_accounts.paths, 'CACHE', Path(self.cache.name))
        self.cache_patch.start()

    def tearDown(self):
        saved_accounts.binary = self.old
        self.cache_patch.stop()
        self.cache.cleanup()

    def test_listing_keeps_usage_only(self):
        out = saved_accounts.listing(self.runner([]))
        self.assertEqual(out['claude'][1]['windows'], [{'label': '5h', 'percent': 0.0, 'resets_at': None}, {'label': '7d', 'percent': 99.0, 'resets_at': None}])
        self.assertEqual(out['codex'][0]['plan'], 'pro')

    def test_switch_runs_the_right_command(self):
        calls = []
        saved_accounts.switch('claude', 2, self.runner(calls))
        self.assertIn(['switch', '2'], calls)
        calls.clear()
        saved_accounts.switch('codex', 1, self.runner(calls))
        self.assertIn(['codex', 'switch', '1'], calls)

    def test_switch_rejects_unknown_and_bad_input(self):
        for agent, number in (('claude', 7), ('claude', '2'), ('claude', True), ('gemini', 1)):
            with self.assertRaises(ValueError):
                saved_accounts.switch(agent, number, self.runner([]))

    def test_switch_failure_reports_the_reason(self):
        with self.assertRaisesRegex(ValueError, 'boom'):
            saved_accounts.switch('claude', 2, self.runner([], code=1))


class LoginRegressions(unittest.TestCase):
    def test_codex_device_login_without_a_key_hint_has_a_copyable_code(self):
        screen = ('Sign in with ChatGPT using device authorization:\n'
                  '1. Open this link in your browser\nhttps://auth.openai.com/codex/device\n'
                  '2. Enter this one-time code\nABCD-1234\nWaiting for authorization…')
        found = seen({'id': '%fixture-device', 'agent': 'codex'}, screen)
        self.assertEqual(found['login'], {'url': 'https://auth.openai.com/codex/device', 'code': False, 'user_code': 'ABCD-1234'})

    def test_long_login_screen_keeps_url_above_last_thirty_rows(self):
        found = seen({'id': '%fixture-tall', 'agent': 'claude'}, SIGN_IN.replace('  Paste code', '\n' * 35 + '  Paste code'))
        self.assertEqual(found['login']['url'], URL)

    def test_login_link_rejects_userinfo_and_bad_ports(self):
        for url in ('https://evil@claude.com/oauth/authorize', 'https://claude.com:bad/oauth/authorize',
                    'https://claude.com:1234/oauth/authorize', 'https://claude.com.evil.test/oauth/authorize'):
            with self.subTest(url=url):
                self.assertIsNone(terminal_input.login_link([url, 'Paste code here if prompted >']))

    def test_old_login_url_does_not_hide_the_current_input(self):
        self.assertNotIn('login', seen({'id': '%fixture-old', 'agent': 'claude'}, SIGN_IN + '\n❯ ready\nEsc to cancel'))

    def test_passive_account_snapshot_never_runs_ccswap(self):
        from unittest.mock import patch
        with patch.object(saved_accounts, '_snapshot', None), patch.object(saved_accounts.subprocess, 'run', side_effect=AssertionError('passive command')):
            self.assertEqual(saved_accounts.snapshot()['claude'], [])

    def test_native_code_is_bound_to_login_and_preserves_drafts(self):
        from unittest.mock import patch
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, SIGN_IN)
        entry = {'id': '%fixture', 'agent': 'claude'}
        with patch.object(terminal_input, 'verify'):
            terminal_input.type_text(entry, '-literal-code', enter=True, login_url=URL, run=run)
            self.assertEqual(calls[-2][1:], ['send-keys', '-t', '%fixture', '-l', '--', '-literal-code'])
            self.assertEqual(calls[-1][1:], ['send-keys', '-t', '%fixture', 'Enter'])
            for screen in ('❯ existing draft', SIGN_IN.replace('prompted >', 'prompted > existing'),
                           SIGN_IN + '\n' + SIGN_IN.replace('prompted >', 'prompted > existing')):
                calls.clear()
                with patch.object(terminal_input, 'capture', return_value=screen), self.assertRaises(RuntimeError):
                    terminal_input.type_text(entry, 'code', enter=True, login_url=URL, run=run)
                self.assertEqual(calls, [])

    def test_resized_login_recovers_the_latest_link_without_reading_history_at_a_normal_prompt(self):
        from unittest.mock import Mock
        entry = {'id': '%clipped', 'agent': 'claude'}
        current = 'rest-of-a-clipped-link\nPaste code here if prompted >'
        history = 'https://claude.com/cai/oauth/authorize?state=old\n\n' + SIGN_IN
        run = Mock(return_value=subprocess.CompletedProcess([], 0, history))
        recovered = terminal_input.login_capture(entry, current, run)
        terminal_input._attention_seen.clear()
        terminal_input.attention(entry, current, False, now=0, login_screen=recovered)
        found = terminal_input.attention(entry, current, False, now=10, login_screen=recovered)
        self.assertEqual(found['login']['url'], URL)
        self.assertIn('-J', run.call_args.args[0])
        run.reset_mock()
        self.assertEqual(terminal_input.login_capture(entry, '❯ existing draft', run), '❯ existing draft')
        run.assert_not_called()

    def test_native_login_reports_only_explicit_outcomes(self):
        entry = {'id': '%outcome', 'agent': 'claude', 'process_start': 1, 'thread': 'one'}
        self.assertIsNone(terminal_input.login_progress(entry, SIGN_IN, now=0))
        result = terminal_input.login_progress(entry, 'Login successful. Press Enter to continue.', now=1)
        self.assertEqual(result, {'phase': 'done', 'message': 'Signed in.'})
        self.assertIsNone(terminal_input.login_progress(dict(entry, thread='two'), '❯ ', now=2))
        entry['id'] = '%failure'
        terminal_input.login_progress(entry, SIGN_IN, now=0)
        result = terminal_input.login_progress(entry, 'Authentication failed. Try again.', now=1)
        self.assertEqual(result['phase'], 'failed')
        entry['id'] = '%closed'
        terminal_input.login_progress(entry, SIGN_IN, now=0)
        result = terminal_input.login_progress(entry, '❯ ', now=1)
        self.assertEqual(result['phase'], 'unknown')
        self.assertNotIn(URL, repr(terminal_input._login_progress))


if __name__ == '__main__':
    unittest.main()
