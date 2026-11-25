"""The Codex provider switch: new Codex panes start with `codex -p foundry` only when Foundry is chosen; the plan is the default."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_provider
import terminal_input as terminal
from own_time import own_time

PROFILE = 'model = "gpt-6.1-sol"\nmodel_provider = "azure"\nmodel_reasoning_effort = "medium"\n'
CONFIG = '[model_providers.azure]\nname = "Azure"\nbase_url = "https://example.invalid"\n\n[model_providers.azure.auth]\ncommand = "%s"\nargs = ["never-run"]\n'


class CodexProviderTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        patcher = patch.object(codex_provider, 'FILE', self.root / 'cache' / 'codex_provider.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_up_foundry(self, command='/bin/true'):
        (self.home / 'foundry.config.toml').write_text(PROFILE)
        (self.home / 'config.toml').write_text(CONFIG % command)

    def launch(self, agent):
        """Start an agent through the same steps as /mobile/api/start, with a fake tmux runner. Returns the typed command."""
        typed = []
        def run(argv, **kwargs):
            if 'load-buffer' in argv:
                typed.append(kwargs['input'].decode())
            return subprocess.CompletedProcess(argv, 0, stdout='')
        profile = codex_provider.launch_profile(self.home) if agent == 'codex' else None
        command = 'codex' + (' -p ' + profile if profile else '') if agent == 'codex' else agent
        with patch.object(terminal, 'shell_prompt', side_effect=['$', '$', '$ ' + command]), own_time(terminal, sleep=Mock()):
            terminal.start_agent({'id': '%1', 'agent': 'shell'}, agent, run=run, profile=profile)
        return typed[0]

    def test_plan_is_the_default(self):
        self.set_up_foundry()
        self.assertEqual(codex_provider.status(self.home)['provider'], 'plan')
        self.assertEqual(self.launch('codex'), 'codex')

    def test_foundry_only_when_chosen(self):
        self.set_up_foundry()
        codex_provider.choose('foundry', self.home)
        self.assertEqual(self.launch('codex'), 'codex -p foundry')
        self.assertEqual(self.launch('claude'), 'claude')  # Claude never gets a Codex profile
        codex_provider.choose('plan', self.home)
        self.assertEqual(self.launch('codex'), 'codex')

    def test_readiness_reads_structure_only(self):
        self.assertFalse(hasattr(codex_provider, 'subprocess'))  # it cannot run the key command
        self.assertEqual(codex_provider.readiness(self.home)[:2], (False, 'Foundry is not set up on the server yet. ~/.codex/foundry.config.toml is missing.'))
        # The Mac setup: a macOS Keychain command that Linux does not have.
        self.set_up_foundry(command='security')
        with patch.object(codex_provider.shutil, 'which', return_value=None):
            ready, hint, model = codex_provider.readiness(self.home)
        self.assertFalse(ready)
        self.assertIn('(security) does not run on Linux', hint)
        self.assertEqual(model, 'gpt-6.1-sol')
        self.set_up_foundry(command='/bin/true')
        self.assertEqual(codex_provider.readiness(self.home), (True, None, 'gpt-6.1-sol'))
        (self.home / 'config.toml').write_text('[model_providers.azure]\nenv_key = "AZURE_KEY"\n')
        self.assertTrue(codex_provider.readiness(self.home)[0])
        (self.home / 'config.toml').write_text('model_provider = "openai"\n')
        self.assertFalse(codex_provider.readiness(self.home)[0])

    def test_foundry_cannot_be_chosen_or_started_when_not_ready(self):
        with self.assertRaises(ValueError):
            codex_provider.choose('foundry', self.home)
        with self.assertRaises(ValueError):
            codex_provider.choose('azure', self.home)
        self.assertEqual(codex_provider.chosen(), 'plan')
        self.set_up_foundry()
        codex_provider.choose('foundry', self.home)
        (self.home / 'foundry.config.toml').unlink()  # the setup broke after Foundry was chosen
        with self.assertRaises(RuntimeError) as caught:
            self.launch('codex')
        self.assertIn('Switch Codex provider to ChatGPT plan', str(caught.exception))
        self.assertEqual(codex_provider.choose('plan', self.home)['provider'], 'plan')  # going back always works

    def test_start_agent_refuses_bad_profiles(self):
        for agent, profile in (('claude', 'foundry'), ('codex', 'x; rm -rf ~'), ('codex', '')):
            with patch.object(terminal, 'shell_prompt') as prompt, self.assertRaises(ValueError):
                terminal.start_agent({'id': '%1', 'agent': 'shell'}, agent, run=None, profile=profile)
            prompt.assert_not_called()

    def test_profile_from_running_command(self):
        self.assertEqual(codex_provider.profile_from_args(['-p', 'foundry']), 'foundry')
        self.assertEqual(codex_provider.profile_from_args(['--profile=foundry-astra', 'resume']), 'foundry-astra')
        self.assertEqual(codex_provider.profile_from_args(['--profile', 'foundry']), 'foundry')
        self.assertIsNone(codex_provider.profile_from_args(['resume', 'abc']))
        self.assertIsNone(codex_provider.profile_from_args(['-p', '$(x)']))

    def test_fork_keeps_the_source_provider(self):
        thread = '0123abcd-0123-4567-89ab-0123456789ab'
        for profile, expected in ((None, f'codex fork {thread}'), ('foundry', f'codex fork -p foundry {thread}')):
            typed = []
            def run(argv, **kwargs):
                if '-l' in argv:
                    typed.append(argv[-1])
                output = ('@3\t/source\n' if '#{window_id}\t#{pane_current_path}\t#{@group}' in argv
                          else 'latest\t120\t40\n' if argv[1] == 'display-message' else '%9\n')
                return subprocess.CompletedProcess(argv, 0, stdout=output)
            entry = {'id': '%1', 'agent': 'codex', 'thread': thread, 'tmux_session': '$1', 'codex_profile': profile}
            terminal.fork_chat(entry, [entry], '/tmp', run=run)
            self.assertEqual(typed, [expected])


class DefaultProviderTest(CodexProviderTest):
    def test_switch_sets_only_the_top_level_provider_line(self):
        self.set_up_foundry()
        config = self.home / 'config.toml'
        config.write_text('model = "gpt-6.1-sol"\nmodel_provider = "openai"\n\n' + config.read_text() + '\n[profiles.x]\nmodel_provider = "openai"\n')
        config.chmod(0o600)
        before = config.read_text()
        codex_provider.choose('foundry', self.home)
        after = config.read_text()
        self.assertEqual(after, before.replace('model_provider = "openai"\n\n', 'model_provider = "azure"\n\n', 1))
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / 'config.toml.bak-tether').read_text(), before)
        self.assertTrue(codex_provider.status(self.home)['everywhere'])
        codex_provider.choose('plan', self.home)
        self.assertEqual(config.read_text(), before)

    def test_missing_top_level_line_is_added(self):
        self.set_up_foundry()
        codex_provider.choose('foundry', self.home)
        self.assertTrue((self.home / 'config.toml').read_text().startswith('model_provider = "azure"\n[model_providers.azure]'))
        self.assertEqual(codex_provider.default_provider(self.home), 'azure')


if __name__ == '__main__':
    unittest.main()
