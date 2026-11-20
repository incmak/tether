"""Setup tests use temporary config files, fixture processes, and fake API writes."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import agent_browser_cli
import agent_browser_setup as setup
import browser_api
import paths
import server
from test_browser import FakeHandler

VAR = setup.VARIABLE
ENDPOINT = 'ws://127.0.0.1:47123/cdp/fixture-token'


class ConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.claude = self.root / '.claude' / 'settings.json'
        self.codex = self.root / '.codex' / 'config.toml'

    def write(self, path, text):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return text

    def status(self):
        return setup.status(self.claude, self.codex)

    def assert_no_write(self, path, original, fix):
        with self.assertRaises(ValueError) as raised:
            fix(path)
        self.assertEqual(path.read_text(), original)
        self.assertFalse(Path(str(path) + '.tether-bak').exists())
        self.assertEqual(list(path.parent.iterdir()), [path])
        return raised.exception

    def test_missing_agent_folders_and_configs(self):
        self.assertEqual(self.status(), {'claude': 'none', 'codex': 'none'})
        self.claude.parent.mkdir()
        self.assertEqual(self.status()['claude'], 'missing')
        setup.fix_claude(self.claude)
        self.assertEqual(self.status()['claude'], 'ok')
        self.assertEqual(self.claude.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(self.claude) + '.tether-bak').exists())
        setup.fix_codex(self.codex)
        self.assertFalse(self.codex.exists())

    def test_default_paths_and_codex_home_override(self):
        with patch.object(paths, 'CLAUDE', self.claude.parent), patch.object(paths, 'CODEX', self.codex.parent), \
                patch.dict(os.environ, CODEX_HOME=''):
            self.assertEqual(setup.claude_settings_path(), self.claude)
            self.assertEqual(setup.codex_config_path(), self.codex)
            setup.fix_claude()
            self.assertEqual(setup.status()['claude'], 'ok')
            with patch.dict(os.environ, CODEX_HOME=str(self.root / 'custom')):
                self.assertEqual(setup.codex_config_path(), self.root / 'custom/config.toml')

    def test_claude_preserves_other_keys_and_hooks_and_quotes_command(self):
        old = {'theme': 'dark', 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'stop'}]}],
               'SessionStart': [{'matcher': 'resume', 'extra': True, 'hooks': [{'type': 'command', 'command': 'other'}]}]}}
        self.write(self.claude, json.dumps(old))
        with patch.object(setup.sys, 'executable', "/python path/user's python"):
            setup.fix_claude(self.claude)
            self.assertEqual(self.status()['claude'], 'ok')
        new = json.loads(self.claude.read_text())
        group = new['hooks']['SessionStart'].pop()
        self.assertEqual(new, old)
        self.assertEqual(set(group), {'hooks'})
        hook = group['hooks'][0]
        self.assertEqual((hook['type'], hook['timeout']), ('command', 10))
        self.assertEqual(shlex.split(hook['command']), ["/python path/user's python",
                         str(Path(agent_browser_cli.__file__).resolve()), 'hook'])

    def test_claude_existing_install_hook_is_ok_and_does_not_write(self):
        original = self.write(self.claude, json.dumps({'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': "python3 '/install path/server/claude_hooks.py'", 'timeout': 1}]}]}}))
        self.assertEqual(self.status()['claude'], 'ok')
        setup.fix_claude(self.claude)
        self.assertEqual(self.claude.read_text(), original)
        self.assertFalse(Path(str(self.claude) + '.tether-bak').exists())

    def test_claude_other_events_and_echo_are_not_session_start_hooks(self):
        for event, command in (('Stop', 'python3 /repo/server/claude_hooks.py'),
                               ('SessionStart', 'echo /repo/server/claude_hooks.py'),
                               ('SessionStart', 'python3 /repo/server/agent_browser_cli.py status')):
            with self.subTest(event=event, command=command):
                self.write(self.claude, json.dumps({'hooks': {event: [{'hooks': [
                    {'type': 'command', 'command': command}]}]}}))
                self.assertEqual(self.status()['claude'], 'missing')

    def test_claude_invalid_json_is_manual_and_untouched(self):
        for original in ('{broken', '{"value": NaN}', '{"value": Infinity}'):
            with self.subTest(original=original):
                self.write(self.claude, original)
                self.assertEqual(self.status()['claude'], 'manual')
                self.assertEqual(self.status()['claude_reason'], 'Claude settings contain invalid JSON.')
                self.assert_no_write(self.claude, original, setup.fix_claude)

    def test_claude_invalid_hook_shapes_are_manual(self):
        for value in ([], {'hooks': []}, {'hooks': {'SessionStart': {}}},
                      {'hooks': {'SessionStart': [{'hooks': [None]}]}}):
            with self.subTest(value=value):
                original = self.write(self.claude, json.dumps(value))
                self.assertEqual(self.status()['claude'], 'manual')
                self.assert_no_write(self.claude, original, setup.fix_claude)

    def test_claude_backup_once_mode_kept_and_fix_idempotent(self):
        original = self.write(self.claude, '{"theme": "dark"}\n')
        self.claude.chmod(0o640)
        setup.fix_claude(self.claude)
        backup = Path(str(self.claude) + '.tether-bak')
        self.assertEqual(backup.read_text(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.claude.stat().st_mode & 0o777, 0o640)
        fixed = self.claude.read_bytes()
        setup.fix_claude(self.claude)
        self.assertEqual(self.claude.read_bytes(), fixed)
        self.write(self.claude, '{"theme": "light"}\n')
        setup.fix_claude(self.claude)
        self.assertEqual(backup.read_text(), original)

    def test_codex_no_playwright_server_is_none(self):
        original = self.write(self.codex, '[mcp_servers.other]\ncommand = "other"\n'
                              '[mcp_servers.http]\nurl = "https://example.test/playwright"\n')
        self.assertEqual(self.status()['codex'], 'none')
        setup.fix_codex(self.codex)
        self.assertEqual(self.codex.read_text(), original)
        self.assertFalse(Path(str(self.codex) + '.tether-bak').exists())

    def test_codex_insert_after_last_key_preserves_comments_and_other_tables(self):
        prefix = '# global\nmodel = "example"\n\n[mcp_servers.browser]\ncommand = "npx" # keep\n'
        keys = '# args comment\nargs = [\n  "@Playwright/mcp",\n] # last key\n'
        suffix = '# trailing comment with [brackets]\n\n[mcp_servers.other]\ncommand = "other"\n'
        original = self.write(self.codex, prefix + keys + suffix)
        self.assertEqual(self.status()['codex'], 'missing')
        setup.fix_codex(self.codex)
        self.assertEqual(self.codex.read_text(), prefix + keys + f'env_vars = ["{VAR}"]\n' + suffix)
        self.assertEqual(Path(str(self.codex) + '.tether-bak').read_text(), original)
        self.assertEqual(self.status()['codex'], 'ok')

    def test_codex_one_line_array_appended_preserves_comments_and_entries(self):
        for array, expected in (('[]', f'["{VAR}"]'),
                                ('["USER_TOKEN"]', f'["USER_TOKEN", "{VAR}"]'),
                                ('["USER_TOKEN", ]', f'["USER_TOKEN",  "{VAR}"]'),
                                ('[{name = "REMOTE_TOKEN", source = "remote"}]',
                                 f'[{{name = "REMOTE_TOKEN", source = "remote"}}, "{VAR}"]'),
                                ('["a]b"]', f'["a]b", "{VAR}"]')):
            with self.subTest(array=array):
                prefix = '[mcp_servers.browser]\ncommand = "playwright-mcp"\nenv_vars = '
                suffix = ' # keep this ] too\n'
                self.write(self.codex, prefix + array + suffix)
                setup.fix_codex(self.codex)
                self.assertEqual(self.codex.read_text(), prefix + expected + suffix)

    def test_codex_uneditable_forms_are_manual_and_do_not_write(self):
        for original in ('[mcp_servers.browser]\ncommand = "playwright"\nenv_vars = [\n"A",\n]\n',
                         'mcp_servers = {browser = {command = "playwright"}}\n',
                         '[mcp_servers]\nbrowser = {command = "playwright"}\n',
                         '[mcp_servers]\nbrowser.command = "playwright"\n',
                         '[mcp_servers.browser]\ncommand = "playwright"\nenv_vars = "A"\n',
                         f'[mcp_servers.browser]\ncommand = "playwright"\nenv_vars = "{VAR}"\n'):
            with self.subTest(original=original):
                self.write(self.codex, original)
                self.assertEqual(self.status()['codex'], 'manual')
                self.assertEqual(self.status()['codex_reason'], setup.CODEX_REASON)
                self.assert_no_write(self.codex, original, setup.fix_codex)

    def test_codex_quoted_name_and_multiple_servers(self):
        original = self.write(self.codex, '[mcp_servers."browser.tool"]\ncommand = "npx"\nargs = ["@playwright/mcp"]\n'
                              "[mcp_servers.'second tool']\ncommand = 'PLAYWRIGHT'\nenv_vars = ['A']\n")
        setup.fix_codex(self.codex)
        self.assertEqual(self.codex.read_text(), original.replace('[mcp_servers.\'second tool\']',
                         f'env_vars = ["{VAR}"]\n[mcp_servers.\'second tool\']').replace("['A']", f"['A', \"{VAR}\"]"))
        self.assertEqual(self.status()['codex'], 'ok')

    def test_codex_already_ok_with_plain_and_local_source_vars(self):
        for entry in (f'"{VAR}"', f'{{name = "{VAR}", source = "local"}}', f'{{name = "{VAR}"}}'):
            with self.subTest(entry=entry):
                original = self.write(self.codex, '[mcp_servers.browser]\ncommand = "playwright"\n'
                                      f'env_vars = [{entry}]\n')
                self.assertEqual(self.status()['codex'], 'ok')
                setup.fix_codex(self.codex)
                self.assertEqual(self.codex.read_text(), original)
                self.assertFalse(Path(str(self.codex) + '.tether-bak').exists())

    def test_codex_remote_source_needs_local_env(self):
        self.write(self.codex, '[mcp_servers.browser]\ncommand = "playwright"\n'
                   f'env_vars = [{{name = "{VAR}", source = "remote"}}]\n')
        self.assertEqual(self.status()['codex'], 'missing')
        setup.fix_codex(self.codex)
        self.assertEqual(setup.tomllib.loads(self.codex.read_text())['mcp_servers']['browser']['env_vars'],
                         [{'name': VAR, 'source': 'remote'}, VAR])

    def test_codex_parse_check_failure_does_not_write(self):
        original = self.write(self.codex, '[mcp_servers.browser]\ncommand = "playwright"\n')
        loads = setup.tomllib.loads
        def tampered_parse(text):
            result = loads(text)
            if f'env_vars = ["{VAR}"]' in text:
                result['model'] = 'unrelated change'
            return result
        with patch.object(setup.tomllib, 'loads', side_effect=tampered_parse) as parse:
            error = self.assert_no_write(self.codex, original, setup.fix_codex)
        self.assertEqual(str(error), 'Codex settings could not be changed safely.')
        self.assertEqual(parse.call_count, 3)

    def test_codex_all_edits_rejected_if_one_server_is_manual(self):
        original = self.write(self.codex, '[mcp_servers.good]\ncommand = "playwright"\n'
                              '[mcp_servers.bad]\ncommand = "playwright"\nenv_vars = [\n]\n')
        self.assert_no_write(self.codex, original, setup.fix_codex)

    def test_codex_invalid_toml_and_old_python_are_manual(self):
        original = self.write(self.codex, '[broken')
        self.assertEqual(self.status()['codex'], 'manual')
        self.assert_no_write(self.codex, original, setup.fix_codex)
        original = self.write(self.codex, '[mcp_servers.browser]\ncommand = "playwright"\n')
        with patch.object(setup, 'tomllib', None):
            self.assertEqual(self.status()['codex'], 'manual')
            self.assertEqual(self.status()['codex_reason'], 'Codex setup requires Python 3.11 or newer.')
            self.assert_no_write(self.codex, original, setup.fix_codex)

    def test_codex_multiline_strings_do_not_look_like_headers(self):
        for closing in ('"""', '""""', '"""""'):
            with self.subTest(closing=closing):
                original = self.write(self.codex, '[mcp_servers.browser]\ncommand = "playwright"\n'
                                      'description = """\n[mcp_servers.fake]\nx = "y"\n' + closing + '\n')
                setup.fix_codex(self.codex)
                self.assertEqual(self.codex.read_text(), original + f'env_vars = ["{VAR}"]\n')

    def test_codex_crlf_no_final_newline_mode_and_backup_once(self):
        original = b'[mcp_servers.browser]\r\ncommand = "playwright"'
        self.codex.parent.mkdir()
        self.codex.write_bytes(original)
        self.codex.chmod(0o640)
        setup.fix_codex(self.codex)
        fixed = original + f'\r\nenv_vars = ["{VAR}"]\r\n'.encode()
        self.assertEqual(self.codex.read_bytes(), fixed)
        backup = Path(str(self.codex) + '.tether-bak')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.codex.stat().st_mode & 0o777, 0o640)
        setup.fix_codex(self.codex)
        self.assertEqual(self.codex.read_bytes(), fixed)
        self.codex.write_bytes(original)
        setup.fix_codex(self.codex)
        self.assertEqual(backup.read_bytes(), original)

    def test_failed_atomic_replace_keeps_original_and_cleans_temp(self):
        original = self.write(self.claude, '{}')
        with patch.object(setup.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                setup.fix_claude(self.claude)
        self.assertEqual(self.claude.read_text(), original)
        self.assertEqual(sorted(p.name for p in self.claude.parent.iterdir()), ['settings.json', 'settings.json.tether-bak'])


class StaleTests(unittest.TestCase):
    def setUp(self):
        self.parents = {10: 1, 11: 10, 12: 11, 20: 1, 21: 20, 30: 1}
        self.commands = {10: b'/bin/zsh\0', 11: b'node\0/tools/claude\0', 12: b'/native/codex\0',
                         20: b'/bin/zsh\0', 21: b'/native/codex\0', 30: b'/bin/zsh\0'}
        self.environ = {11: b'OTHER=1\0', 12: b'OTHER=1\0', 21: ('TETHER_CDP_URL=' + ENDPOINT).encode() + b'\0'}
        self.now = 0
        self.cache = {}
        self.panes = Mock(return_value='%12 10\n%13 20\n%14 30\n')
        self.text = Mock(side_effect=lambda path: f'{path.split("/")[2]} (name with ) chars) S '
                         f'{self.parents[int(path.split("/")[2])]} 0')
        self.binary = Mock(side_effect=self.read_bytes)
        self.uid = Mock(return_value=1000)
        self.list_dir = Mock(return_value=[str(pid) for pid in self.parents] + ['net'])
        self.clock = lambda: self.now

    def read_bytes(self, path):
        pid = int(path.split('/')[2])
        return (self.commands if path.endswith('cmdline') else self.environ)[pid]

    def scan(self, endpoint=ENDPOINT):
        return setup.stale_panes(endpoint, read_text=self.text, read_bytes=self.binary, list_dir=self.list_dir,
                                 uid_of=self.uid, pane_reader=self.panes, clock=self.clock, uid=1000, cache=self.cache)

    def test_claude_without_env_stale_codex_with_env_and_shell_not_stale(self):
        self.assertEqual(self.scan(), ['%12'])
        self.assertEqual(self.text.call_count, len(self.parents))
        self.assertNotIn('/proc/12/cmdline', [call.args[0] for call in self.binary.call_args_list])

    def test_matching_env_on_claude_skips_nested_native_agent(self):
        self.environ[11] = ('TETHER_CDP_URL=' + ENDPOINT).encode() + b'\0'
        self.assertEqual(self.scan(), [])

    def test_topmost_match_ignores_deeper_agent_in_sibling_tree(self):
        self.parents.update({13: 10, 14: 13})
        self.commands.update({13: b'/bin/zsh\0', 14: b'/native/codex\0'})
        self.environ[11] = ('TETHER_CDP_URL=' + ENDPOINT).encode()
        self.environ[14] = b''
        self.list_dir.return_value = [str(pid) for pid in self.parents]
        self.assertEqual(self.scan(), [])
        self.assertNotIn('/proc/14/environ', [call.args[0] for call in self.binary.call_args_list])

    def test_wrong_endpoint_is_stale(self):
        self.assertEqual(self.scan(ENDPOINT + '-new'), ['%12', '%13'])

    def test_node_codex_wrapper_is_topmost_and_root_agent_counts(self):
        self.commands[11] = b'/usr/bin/node\0/tools/codex\0'
        self.commands[30] = b'codex\0resume\0'
        self.environ[30] = b''
        self.assertEqual(self.scan(), ['%12', '%14'])

    def test_other_users_and_unreadable_processes_are_skipped(self):
        self.uid.side_effect = lambda path: 2000 if path == '/proc/11' else 1000
        self.assertEqual(self.scan(), [])
        self.cache.clear()
        self.uid.side_effect = None
        def read(path):
            if path in ('/proc/11/environ', '/proc/21/cmdline'):
                raise PermissionError
            return self.read_bytes(path)
        self.binary.side_effect = read
        self.assertEqual(self.scan(), [])

    def test_cache_ten_seconds_and_endpoint_invalidation(self):
        self.assertEqual(self.scan(), ['%12'])
        self.environ[11] = ('TETHER_CDP_URL=' + ENDPOINT).encode()
        self.now = 9.9
        self.assertEqual(self.scan(), ['%12'])
        self.assertEqual(self.panes.call_count, 1)
        self.now = 10
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.panes.call_count, 2)
        self.assertEqual(self.scan(ENDPOINT + '-new'), ['%12', '%13'])

    def test_scan_caps_at_two_seconds_and_tmux_errors_return_empty(self):
        def expire(path):
            self.now = 2
            return 1000
        self.uid.side_effect = expire
        self.assertEqual(self.scan(), [])
        self.text.assert_not_called()
        self.assertLessEqual(self.panes.call_args.args[0], 2)
        self.cache.clear()
        self.panes.side_effect = subprocess.TimeoutExpired('tmux', 2)
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.scan(''), [])


class HookTests(unittest.TestCase):
    def test_hook_with_and_without_environment(self):
        for endpoint in ('', ENDPOINT):
            with self.subTest(endpoint=endpoint):
                run = subprocess.run([sys.executable, agent_browser_cli.__file__, 'hook'], input='not even JSON',
                                     capture_output=True, text=True, timeout=5,
                                     env=dict(os.environ, TETHER_CDP_URL=endpoint))
                self.assertEqual((run.returncode, run.stderr), (0, ''))
                if endpoint:
                    self.assertEqual(len(run.stdout.splitlines()), 1)
                    self.assertEqual(json.loads(run.stdout), {'hookSpecificOutput': {
                        'hookEventName': 'SessionStart', 'additionalContext': agent_browser_cli.note()}})
                else:
                    self.assertEqual(run.stdout, '')

    def test_hook_errors_are_silent_and_successful(self):
        for target in ('stdin', 'note'):
            with self.subTest(target=target), patch.dict(os.environ, TETHER_CDP_URL=ENDPOINT), \
                    patch.object(agent_browser_cli.sys, 'stdin', io.StringIO('{}')), patch('builtins.print') as output:
                if target == 'stdin':
                    with patch.object(agent_browser_cli.sys.stdin, 'read', side_effect=OSError):
                        self.assertEqual(agent_browser_cli.main(['hook']), 0)
                else:
                    with patch.object(agent_browser_cli, 'note', side_effect=RuntimeError):
                        self.assertEqual(agent_browser_cli.main(['hook']), 0)
                output.assert_not_called()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.state = {'enabled': True, 'available': True, 'reason': '', 'agents': [], 'asks': []}
        self.proxy = Mock(url=ENDPOINT)
        self.proxy.state.side_effect = lambda: dict(self.state)
        self.proxy.set_enabled.side_effect = lambda enabled: dict(self.state, enabled=enabled)
        self.proxy.dismiss.side_effect = lambda ident: dict(self.state)
        self.setup = Mock()
        self.setup.status.return_value = {'claude': 'missing', 'codex': 'ok'}
        self.setup.stale_panes.return_value = ['%12']

    def post(self, name='agents/setup', body=None):
        handler = FakeHandler(browser_api.PREFIX + name, {'agent': 'claude'} if body is None else body)
        browser_api.post(handler, the_proxy=self.proxy, the_setup=self.setup)
        return handler.replies[0]

    def test_setup_route_fixes_only_selected_agent_and_returns_state(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                self.setup.reset_mock()
                code, result = self.post(body={'agent': agent})
                self.assertEqual(code, 200)
                getattr(self.setup, 'fix_' + agent).assert_called_once_with()
                getattr(self.setup, 'fix_' + ('codex' if agent == 'claude' else 'claude')).assert_not_called()
                self.assertEqual(result, dict(self.state, setup=self.setup.status.return_value, stale=['%12']))
                self.setup.stale_panes.assert_called_once_with(ENDPOINT)

    def test_all_agent_responses_gain_setup_and_stale(self):
        handler = FakeHandler(browser_api.PREFIX + 'agents')
        browser_api.get(handler, the_proxy=self.proxy, the_setup=self.setup)
        replies = [handler.replies[0], self.post('agents', {'enabled': True}), self.post('ask/dismiss', {'id': 'fixture'})]
        for code, result in replies:
            self.assertEqual(code, 200)
            self.assertEqual(result['setup'], self.setup.status.return_value)
            self.assertEqual(result['stale'], ['%12'])

    def test_disabled_or_unavailable_does_not_scan(self):
        for field in ('enabled', 'available'):
            with self.subTest(field=field):
                self.state[field] = False
                self.assertEqual(self.post()[1]['stale'], [])
                self.state[field] = True
        self.setup.stale_panes.assert_not_called()
        self.assertEqual(self.post('agents', {'enabled': False})[1]['stale'], [])
        self.setup.stale_panes.assert_not_called()

    def test_setup_errors_are_400_with_plain_sentences(self):
        for error, message in ((ValueError(setup.CODEX_REASON), setup.CODEX_REASON),
                               (OSError('private path must not appear'), 'Could not update the agent settings.')):
            with self.subTest(error=error):
                self.setup.fix_claude.side_effect = error
                self.assertEqual(self.post(), (400, {'error': message}))
        for agent in ('other', None, ['claude']):
            self.assertEqual(self.post(body={'agent': agent}), (400, {'error': 'Choose Claude or Codex.'}))

    def test_unknown_routes_never_run_setup(self):
        for name in ('agents/setup/extra', 'agents/restart', 'setup'):
            self.assertEqual(self.post(name), (404, {'error': 'Not found'}))
        self.setup.fix_claude.assert_not_called()
        self.setup.fix_codex.assert_not_called()

    def test_server_route_retains_auth_origin_and_write_header(self):
        for failure in (None, 'auth', 'Origin', 'X-Codex-Mobile'):
            with self.subTest(failure=failure):
                handler = server.Handler.__new__(server.Handler)
                handler.path = browser_api.PREFIX + 'agents/setup'
                handler.headers = {'Host': 'phone.test', 'Origin': 'https://phone.test', 'X-Codex-Mobile': '1'}
                handler.deny = Mock(return_value=failure == 'auth')
                handler.respond = Mock()
                if failure in handler.headers:
                    handler.headers[failure] = ''
                with patch.object(browser_api, 'post') as post:
                    server.Handler.do_POST(handler)
                if failure is None:
                    post.assert_called_once_with(handler)
                else:
                    post.assert_not_called()
                    if failure != 'auth':
                        handler.respond.assert_called_once_with(403, {'error': 'Reload the page and try again.'})


if __name__ == '__main__':
    unittest.main()
