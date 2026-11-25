"""Validate shipped integrations without installing or changing services."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    ROOT / 'integrations/claude/tether/skills/setup/SKILL.md',
    ROOT / 'integrations/codex/skills/tether-setup/SKILL.md',
)


class IntegrationTests(unittest.TestCase):
    def test_marketplace_resolves_plugin_manifest(self):
        marketplace = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text())
        self.assertEqual(marketplace['name'], 'tether')
        self.assertTrue(marketplace['owner']['name'])
        self.assertEqual(len(marketplace['plugins']), 1)
        entry = marketplace['plugins'][0]
        self.assertEqual(entry['name'], 'tether')
        self.assertEqual(entry['source'], './integrations/claude/tether')
        plugin_path = ROOT / entry['source']
        self.assertTrue(plugin_path.is_dir())
        plugin = json.loads((plugin_path / '.claude-plugin/plugin.json').read_text())
        self.assertEqual(plugin['name'], entry['name'])
        self.assertTrue(plugin['description'])
        version_file = ROOT / 'VERSION'
        version = version_file.read_text().strip() if version_file.is_file() else '0.1.0'
        self.assertEqual(plugin['version'], version)
        self.assertEqual(plugin['license'], 'MIT')
        self.assertEqual(plugin['repository'], 'https://github.com/incmak/tether')
        self.assertTrue((plugin_path / 'commands/tether-setup.md').is_file())

    def test_skills_have_required_frontmatter(self):
        for path in SKILLS:
            with self.subTest(skill=path.relative_to(ROOT)):
                text = path.read_text()
                frontmatter = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)', text, re.DOTALL)
                self.assertIsNotNone(frontmatter, 'Skill must start with YAML frontmatter')
                for field in ('name', 'description'):
                    value = re.search(rf'^{field}:\s*(\S[^\r\n]*)$', frontmatter[1], re.MULTILINE)
                    self.assertIsNotNone(value, f'Missing or empty {field}')
                    self.assertNotIn(value[1].strip(), ('""', "''"))

    def test_documented_flags_exist(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'install.py'), '--help'],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=10,
        )
        help_flags = set(re.findall(r'(?<![\w-])--[a-z][a-z-]*\b', result.stdout))
        for script, args in ((ROOT / 'tether', ['install', '--help']),
                             (ROOT / 'tether', ['migrate', '--help']),
                             (ROOT / 'integrations/claude/tether/skills/setup/bootstrap.py', ['--help'])):
            result = subprocess.run([sys.executable, str(script), *args], capture_output=True,
                                    text=True, check=True, timeout=10)
            help_flags.update(re.findall(r'(?<![\w-])--[a-z][a-z-]*\b', result.stdout))
        for path in SKILLS:
            with self.subTest(skill=path.relative_to(ROOT)):
                flags = set(re.findall(r'(?<![\w-])--[a-z][a-z-]*\b', path.read_text()))
                self.assertTrue(flags, 'Skill must document installer flags')
                self.assertFalse(flags - help_flags, f'Unsupported documented flags: {sorted(flags - help_flags)}')

    def test_packaged_bootstraps_match(self):
        self.assertEqual(SKILLS[0].with_name('bootstrap.py').read_bytes(),
                         SKILLS[1].with_name('bootstrap.py').read_bytes())

    def test_tether_tools_lists_agent_commands(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tether'), 'tools'],
                                capture_output=True, text=True, check=True, timeout=10)
        for filename, purpose in (
            ('agent_reminder_cli.py', "Set and manage reminders for Mak from an agent's tab."),
            ('agent_backlog_cli.py', "Read and change the shared backlog from an agent's tab."),
        ):
            with self.subTest(filename=filename):
                self.assertIn(f'{filename}  {purpose}\n', result.stdout)
                self.assertIn(str(ROOT / 'server' / filename), result.stdout)
        for script in (ROOT / 'server').glob('agent_*_cli.py'):
            self.assertIn(str(script), result.stdout)

    def test_tether_help_points_to_tools_and_reminders(self):
        for args, expected in ((['--help'], ('tether tools', 'tether remind')),
                               (['remind', '--help'], ('Set and manage reminders',))):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ROOT / 'tether'), *args],
                                        capture_output=True, text=True, check=True, timeout=10)
                for text in expected:
                    self.assertIn(text, result.stdout)


if __name__ == '__main__':
    unittest.main()
