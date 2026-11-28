import unittest_isolated  # noqa: F401
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import catalog


class AntigravityCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'home'
        self.gemini = self.home / '.gemini'
        self.workspace = self.root / 'repo'
        self.cwd = self.workspace / 'nested'
        self.cwd.mkdir(parents=True)
        self.write(self.workspace / '.git', 'gitdir: linked-worktree\n')
        self.enterContext(mock.patch.multiple(catalog.paths, HOME=self.home, ANTIGRAVITY=self.gemini / 'antigravity-cli'))

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def config(self, path, data):
        return self.write(path, json.dumps(data))

    def skill(self, root, folder, description, name=None):
        metadata = f'name: {name}\n' if name else ''
        return self.write(root / folder / 'SKILL.md', f'---\n{metadata}description: {description}\n---\n')

    def read(self, cwd=None):
        return catalog.catalog('antigravity', cwd or self.cwd)

    def commands(self, cwd=None):
        return {command['name']: command for command in self.read(cwd)['commands']}

    def test_cli_commands_have_aliases_and_argument_hints(self):
        commands = self.commands()
        self.assertEqual(commands['add-dir']['hint'], '<path>')
        self.assertEqual(commands['open']['hint'], '<path>')
        self.assertEqual(commands['codesearch']['aliases'], ['cs', 'search'])
        self.assertEqual(commands['resume']['aliases'], ['switch', 'conversation'])
        self.assertEqual(commands['projects']['kind'], 'builtin')
        self.assertEqual(commands['thinking']['kind'], 'builtin')
        self.assertEqual(commands['record']['kind'], 'builtin')
        self.assertEqual(commands['antigravity-guide']['kind'], 'skill')
        self.assertEqual(commands['run-workflow']['kind'], 'skill')
        self.assertNotIn('migrate-workflows', commands)
        self.assertEqual(catalog.catalog('shell', self.cwd)['commands'], [])
        self.assertEqual(len(self.read()['commands']), len(commands))

    def test_workspace_scope_compatibility_folders_and_precedence(self):
        root = self.workspace / '.agents' / 'skills'
        child = self.cwd / '.agents' / 'skills'
        cross_tool = self.home / '.agents' / 'skills'
        self.skill(root, 'collision', 'Repository root')
        self.skill(child, 'collision', 'Nested workspace')
        self.skill(cross_tool, 'collision', 'Cross-tool home')
        self.skill(cross_tool, 'cross-tool', 'Cross-tool home')
        self.skill(child, 'report', 'Workspace before global')
        self.skill(self.gemini / 'config' / 'skills', 'report', 'Global Gemini')
        self.skill(child, 'local', 'Workspace')
        self.skill(self.gemini / 'skills', 'local', 'Gemini CLI legacy')
        self.skill(self.gemini / 'antigravity-cli' / 'skills', 'local', 'Antigravity CLI')
        self.skill(root, 'help', 'A skill with the name of a built-in')
        self.skill(self.root / '.agents' / 'skills', 'outside-root', 'Outside the Git root')
        self.skill(child, 'group/nested-skill', 'Too deep for the standard scan')
        self.skill(self.workspace / 'sibling' / '.agents' / 'skills', 'other', 'Outside the selected workspace')
        for directory, skill in (('.agent', 'legacy'), ('_agents', 'plural'), ('_agent', 'singular')):
            self.skill(self.cwd / directory / 'skills', skill, f'From {directory}')
        personal = self.workspace / 'people' / 'test-user' / '.agents' / 'skills'
        self.skill(personal, 'report', 'Personal workspace')
        self.config(self.workspace / '.gemini' / 'config.json', {'personal_customization_dir': 'people/{user}'})

        with mock.patch.dict(os.environ, USER='test-user'):
            commands = self.commands()
        expected = {'collision': 'Repository root', 'report': 'Personal workspace', 'local': 'Antigravity CLI',
                    'cross-tool': 'Cross-tool home', 'legacy': 'From .agent',
                    'plural': 'From _agents', 'singular': 'From _agent'}
        self.assertEqual({name: commands[name]['description'] for name in expected}, expected)
        self.assertEqual(commands['help']['kind'], 'builtin')
        self.assertFalse({'outside-root', 'nested-skill', 'other'} & commands.keys())
        # A directory outside Git uses its own customizations only.
        non_repo = self.root / 'non-repo' / 'nested'
        self.skill(non_repo / '.agents' / 'skills', 'standalone', 'Only this folder')
        # The test harness can run under a Git marker in the host's temporary directory.
        with mock.patch.object(Path, 'exists', return_value=False):
            commands = self.commands(non_repo)
        self.assertIn('standalone', commands)
        self.assertNotIn('outside-root', commands)
        self.assertNotIn('legacy', commands)

    def test_manifests_resolve_nested_paths_and_inheritance(self):
        tools = self.workspace / 'tools'
        self.skill(tools, 'db/migrate', 'Update the database', name='migrate-data')
        self.skill(tools, 'unused', 'Unselected sibling')
        shared = self.root / 'shared'
        self.skill(shared / 'skills', 'build', '>\n  Build an artifact.\n  Verify the result.')
        self.skill(shared / 'skills', 'archive', 'Excluded by inherited selection')
        local_manifest = self.cwd / '.agents' / 'skills.json'
        shared_manifest = shared / '.agents' / 'skills.json'
        direct = self.skill(tools, 'checks/check', 'Run the direct check')
        self.config(shared_manifest, {'entries': [{'path': 'skills'}], 'inherits': [{'path': str(local_manifest)}]})
        self.config(local_manifest, {
            'entries': [{'path': 'tools', 'include_only': ['db/migrate']}, {'path': str(direct)}],
            'inherits': [{'path': str(shared_manifest), 'include_only': ['build']}],
            'exclude': ['hidden', 'local-exception'],
        })
        self.skill(self.gemini / 'config' / 'skills', 'hidden', 'Hidden by project')
        self.skill(self.gemini / 'antigravity-cli' / 'skills', 'local-exception', 'CLI-specific skills survive excludes')
        self.config(self.gemini / 'config' / 'skills.json', {'entries': [{'path': 'extra'}]})
        self.skill(self.gemini / 'config' / 'extra', 'global-extra', 'From the global manifest')

        commands = self.commands()
        self.assertEqual(commands['build']['description'], 'Build an artifact. Verify the result.')
        self.assertEqual(commands['migrate-data']['description'], 'Update the database')
        self.assertIn('check', commands)
        self.assertIn('global-extra', commands)
        self.assertIn('local-exception', commands)
        self.assertFalse({'unused', 'archive', 'hidden'} & commands.keys())

    def test_manifest_tilde_paths_are_relative_to_the_workspace(self):
        self.skill(self.home / 'extra', 'from-home', 'Outside the workspace')
        self.skill(self.cwd / '~' / 'extra', 'literal-tilde', 'In a folder named tilde')
        self.config(self.cwd / '.agents' / 'skills.json', {'entries': [{'path': '~/extra'}]})

        commands = self.commands()
        self.assertIn('literal-tilde', commands)
        self.assertNotIn('from-home', commands)

    def test_manifest_filters_match_literal_names_and_paths(self):
        self.skill(self.cwd / '.agents' / 'skills', 'outer-one', 'Top-level exclude is literal')
        self.skill(self.cwd / 'extra', 'experimental-one', 'Entry exclude is literal')
        self.skill(self.cwd / 'extra', 'excluded', 'Exact exclude removes this skill')
        self.skill(self.cwd / 'selected', 'match-one', 'Pattern does not include this skill')
        self.skill(self.cwd / 'selected', 'chosen', 'Exact include loads this skill')
        self.config(self.cwd / '.agents' / 'skills.json', {
            'entries': [
                {'path': 'extra', 'exclude': ['experimental-.*', 'excluded']},
                {'path': 'selected', 'include_only': ['match-.*', 'chosen']},
            ],
            'exclude': ['outer-.*'],
        })

        commands = self.commands()
        self.assertTrue({'outer-one', 'experimental-one', 'chosen'} <= commands.keys())
        self.assertFalse({'excluded', 'match-one'} & commands.keys())

    def test_plugins_and_mcp_read_public_metadata_only(self):
        local = self.cwd / '.agents' / 'plugins' / 'kit-folder'
        installed = self.gemini / 'config' / 'plugins' / 'global-kit'
        off = self.gemini / 'config' / 'plugins' / 'off-kit'
        global_config = self.gemini / 'config'
        self.config(local / '.antigravity-plugin' / 'plugin.json', {'name': 'different-name'})
        self.config(local / 'plugin.json', {'name': 'shadowed', 'disabled': True})
        self.config(installed / 'plugin.json', {'disabled': True})
        self.config(off / 'plugin.json', {})
        self.skill(local / 'skills', 'check', 'Check the project')
        self.skill(installed / 'skills', 'check', 'Check global settings')
        self.skill(off / 'skills', 'check', 'Check the disabled plugin')
        self.config(global_config / 'config.json', {'plugins': {'global-kit': {'enabled': True}, 'off-kit': {'enabled': False}}})
        custom = self.workspace / 'tools' / 'extra-plugin'
        self.config(custom / '.gemini-plugin' / 'plugin.json', {})
        self.skill(custom / 'skills', 'format', 'Format the project')
        self.config(self.workspace / '.agents' / 'plugins.json', {'entries': [{'path': 'tools/extra-plugin'}]})
        secret = 'PRIVATE_TEST_VALUE'
        server = {'command': 'fixture-program', 'env': {'TOKEN': secret}, 'headers': {'Authorization': secret}}
        self.config(local / 'mcp_config.json', {'mcpServers': {'tasks': server}})
        self.config(off / 'mcp_config.json', {'mcpServers': {'tasks': server}})
        self.config(self.cwd / '.agents' / 'mcp_config.json', {'mcpServers': {'project': server}})
        self.config(global_config / 'mcp_config.json', {'mcpServers': {'global': server}})

        with mock.patch('subprocess.Popen', side_effect=AssertionError('Catalog must never run a program')):
            result = self.read()
        self.assertEqual({plugin['name']: plugin['enabled'] for plugin in result['plugins']},
                         {'kit-folder': True, 'global-kit': True, 'off-kit': False, 'extra-plugin': True})
        commands = {command['name'] for command in result['commands']}
        self.assertTrue({'kit-folder:check', 'global-kit:check', 'extra-plugin:format'} <= commands)
        self.assertNotIn('off-kit:check', commands)
        self.assertEqual({config['name']: config['source'] for config in result['mcp']},
                         {'global': 'global', 'project': 'workspace', 'kit-folder_tasks': 'plugin kit-folder'})
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('fixture-program', json.dumps(result))

    def test_builtin_plugin_contributes_its_metadata(self):
        plugin = self.gemini / 'antigravity-cli' / 'builtin' / 'plugins' / 'swarm'
        self.config(plugin / 'plugin.json', {})
        self.skill(plugin / 'skills', 'guide', 'Read the swarm guide')
        result = self.read()
        self.assertEqual(result['plugins'], [{'name': 'swarm', 'source': 'built-in', 'enabled': True}])
        self.assertIn('swarm:guide', {command['name'] for command in result['commands']})

    def test_plugin_manifest_can_disable_a_plugin_without_a_preference(self):
        plugin = self.cwd / '.agents' / 'plugins' / 'off-kit'
        self.config(plugin / 'plugin.json', {'disabled': True})
        self.skill(plugin / 'skills', 'check', 'Check the workspace')
        self.config(plugin / 'mcp_config.json', {'mcpServers': {'tasks': {}}})
        result = self.read()
        self.assertEqual(result['plugins'], [{'name': 'off-kit', 'source': 'workspace', 'enabled': False}])
        self.assertNotIn('off-kit:check', {command['name'] for command in result['commands']})
        self.assertEqual(result['mcp'], [])

    def test_plugin_manifest_excludes_default_workspace_and_global_plugins(self):
        workspace = self.cwd / '.agents'
        self.config(workspace / 'plugins' / 'workspace-kit' / 'plugin.json', {})
        self.config(self.gemini / 'config' / 'plugins' / 'global-kit' / 'plugin.json', {})
        self.config(workspace / 'plugins' / 'keep-kit' / 'plugin.json', {})
        self.config(workspace / 'plugins.json', {'exclude': ['workspace-kit', 'global-kit']})
        self.assertEqual(self.read()['plugins'], [{'name': 'keep-kit', 'source': 'workspace', 'enabled': True}])

    def test_manifests_over_the_byte_limit_are_ignored(self):
        self.skill(self.cwd / 'extra', 'listed', 'Load from the manifest')
        manifest = self.cwd / '.agents' / 'skills.json'
        for text in ('text', 'café'):
            with self.subTest(text=text):
                data = json.dumps({'entries': [{'path': 'extra'}], 'description': text}, ensure_ascii=False)
                at_limit = data + ' ' * (1024 * 1024 - len(data.encode('utf-8')))
                self.write(manifest, at_limit)
                self.assertIn('listed', self.commands())
                self.write(manifest, at_limit + ' ')
                self.assertNotIn('listed', self.commands())

    def test_malformed_metadata_keeps_the_rest_of_the_catalog(self):
        agents = self.cwd / '.agents'
        self.config(agents / 'skills.json', {'entries': [False, {}, {'path': 'bad\0path'}, {'path': []}], 'inherits': 1})
        self.config(agents / 'mcp_config.json', {'mcpServers': ['not-a-map']})
        self.config(agents / 'plugins' / 'broken' / 'plugin.json', ['not-an-object'])
        skill = self.skill(agents / 'skills', 'metadata', '|-\n  Preserve the description.\n  Ignore nested metadata.')
        skill.write_text(skill.read_text().replace('\n---\n', '\nmetadata:\n  name: wrong\n---\n'), encoding='utf-8')
        self.write(agents / 'skills' / 'incomplete' / 'SKILL.md', 'Invalid skill metadata')
        self.assertEqual(self.commands()['metadata']['description'], 'Preserve the description. Ignore nested metadata.')
        self.assertNotIn('incomplete', self.commands())
        self.assertEqual(self.read()['mcp'], [])
        self.assertEqual(self.read()['plugins'], [])
