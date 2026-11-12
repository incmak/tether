import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import catalog


def listing(names, content, initial=True):
    return json.dumps({'type': 'attachment', 'attachment': {'type': 'skill_listing', 'names': names, 'content': content, 'isInitial': initial}}) + '\n'


class TranscriptSkillTests(unittest.TestCase):
    def setUp(self):
        catalog._listings.clear()

    def test_reads_bundled_claude_ai_and_plugin_skills(self):
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder) / 't.jsonl'
            transcript.write_text('{"type":"user"}\n' + listing(
                ['simplify', 'anthropic-skills:docx', 'atlassian:triage-issue'],
                '- simplify: Review the changed code\n- anthropic-skills:docx: Word files: read and write\n- atlassian:triage-issue: Triage bugs'))
            found = catalog.transcript_skills(str(transcript))
            self.assertEqual(found, {'simplify': 'Review the changed code', 'anthropic-skills:docx': 'Word files: read and write',
                                     'atlassian:triage-issue': 'Triage bugs'})
            # A later listing adds to the set; a half-written line waits for the next call.
            with transcript.open('a') as handle:
                handle.write(listing(['loop'], '- loop: Run on an interval', initial=False) + '{"type":"skill_li')
            self.assertIn('loop', catalog.transcript_skills(str(transcript)))
            # A new initial listing (resume, /clear) replaces the old set.
            with transcript.open('a') as handle:
                handle.write('stin"}\n' + listing(['pr'], '- pr: Write a PR body'))
            self.assertEqual(catalog.transcript_skills(str(transcript)), {'pr': 'Write a PR body'})

    def test_missing_transcript_gives_nothing(self):
        self.assertEqual(catalog.transcript_skills(None), {})
        self.assertEqual(catalog.transcript_skills('/nonexistent/t.jsonl'), {})


class ClaudeCatalogTests(unittest.TestCase):
    def test_probe_hint_falls_back_to_docs(self):
        docs = [
            {'name': 'add-dir', 'description': 'Add a directory', 'aliases': [], 'hint': '<path>', 'kind': 'builtin'},
            {'name': 'advisor', 'description': 'Choose advisor', 'aliases': [], 'hint': '[model|off]', 'kind': 'builtin'},
        ]
        with mock.patch.object(catalog, 'claude_builtins', return_value=docs):
            result = catalog.merged_commands({'commands': [
                {'name': 'add-dir', 'description': '', 'hint': ''},
            ]})
            self.assertEqual([item['hint'] for item in result], ['<path>', '[model|off]'])
            result = catalog.merged_commands({'commands': [
                {'name': 'add-dir', 'description': '', 'hint': '[directory]'},
            ]})
            self.assertEqual(result[0]['hint'], '[directory]')

    def test_docs_hint_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'commands.json'
            path.write_text(json.dumps({'commands': [
                {'name': 'add-dir', 'description': 'Add a directory', 'hint': '<path>'},
                {'name': 'status', 'description': 'Show status'},
            ]}))
            with mock.patch.object(catalog, 'COMMANDS_FILE', path):
                self.assertEqual([item['hint'] for item in catalog.claude_builtins()], ['<path>', ''])

    def test_plugin_prefix_and_synced_plugins_and_transcript(self):
        catalog._listings.clear()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            claude, home = root / '.claude', root / 'home'
            plugin = claude / 'plugins' / 'cache' / 'market' / 'atlassian-mcp' / 'v1'
            (plugin / '.claude-plugin').mkdir(parents=True)
            (plugin / '.claude-plugin' / 'plugin.json').write_text('{"name": "atlassian"}')
            (plugin / 'skills' / 'triage').mkdir(parents=True)
            (plugin / 'skills' / 'triage' / 'SKILL.md').write_text('---\nname: triage-issue\ndescription: Triage bugs\n---\n')
            (claude / 'settings.json').write_text('{"enabledPlugins": {"atlassian-mcp@market": true}}')
            synced = claude / 'plugins' / 'synced' / 'org_acct' / 'engineering~g2'
            (synced / '.claude-plugin').mkdir(parents=True)
            (synced / '.claude-plugin' / 'plugin.json').write_text('{"name": "engineering"}')
            (synced / 'skills' / 'standup').mkdir(parents=True)
            (synced / 'skills' / 'standup' / 'SKILL.md').write_text('---\nname: standup\ndescription: Write a standup\n---\n')
            home.mkdir()
            (home / '.claude.json').write_text('{"oauthAccount": {"organizationUuid": "org", "accountUuid": "acct"}}')
            transcript = root / 't.jsonl'
            transcript.write_text(listing(['atlassian:triage-issue', 'simplify'], '- atlassian:triage-issue: Triage bugs\n- simplify: Clean up'))
            with mock.patch.object(catalog.paths, 'CLAUDE', claude), mock.patch.object(catalog, 'HOME', home), \
                    mock.patch.object(catalog.claude_probe, 'commands', return_value=None):
                result = catalog.catalog('claude', None, str(transcript))
            names = [item['name'] for item in result['commands']]
            self.assertIn('atlassian:triage-issue', names)
            self.assertNotIn('atlassian-mcp:triage-issue', names)
            self.assertIn('engineering:standup', names)
            self.assertIn('simplify', names)
            self.assertIn('fast', names)
            status = next(item for item in result['commands'] if item['name'] == 'clear')
            self.assertIn('new', status['aliases'])
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(result['plugins'][0]['name'], 'atlassian')


class DocsParserTests(unittest.TestCase):
    def test_argument_hints(self):
        import update_claude_commands as update
        page = (
            '| `/add-dir <path>` | Add a directory. |\n'
            '| `/advisor [model\\|off]` | Choose an advisor. |\n'
            '| `/status` | Show status. |\n')
        self.assertEqual([item['hint'] for item in update.parse(page)], ['<path>', '[model|off]', ''])

    def test_rows_aliases_and_skills(self):
        import update_claude_commands as update
        page = (
            '| Command | Purpose |\n| :-- | :-- |\n'
            '| `/clear [name]` | Start a new conversation with empty context. Pass a name. Aliases: `/reset`, `/new` |\n'
            '| `/loop [interval] [prompt]` | **[Skill](https://x/skills.md#bundled-skills).** Run a prompt repeatedly. Alias: `/proactive` |\n'
            '| `/status` | Open the [Settings](https://x) interface, showing version, model, account, and connectivity. More text |\n')
        self.assertEqual(update.parse(page), [
            {'name': 'clear', 'description': 'Start a new conversation with empty context.', 'aliases': ['reset', 'new'], 'hint': '[name]', 'kind': 'builtin'},
            {'name': 'loop', 'description': 'Run a prompt repeatedly.', 'aliases': ['proactive'], 'hint': '[interval] [prompt]', 'kind': 'skill'},
            {'name': 'status', 'description': 'Open the Settings interface, showing version, model, account, and connectivity.', 'aliases': [], 'hint': '', 'kind': 'builtin'},
        ])


class DiskHintTests(unittest.TestCase):
    def test_front_matter_and_disk_hints(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            skill = root / 'skill' / 'SKILL.md'
            skill.parent.mkdir()
            skill.write_text('---\nname: skill\nargument-hint: "[file]"\n---\n')
            command = root / 'command.md'
            command.write_text("---\nargument-hint: '<path>'\n---\n")
            self.assertEqual(catalog.front_matter(skill)['argument-hint'], '[file]')
            self.assertEqual(catalog.skills_in(root)[0]['hint'], '[file]')
            self.assertEqual(catalog.commands_in(root)[0]['hint'], '<path>')
            command.write_text('---\nargument-hint: >\n---\n')
            self.assertEqual(catalog.commands_in(root)[0]['hint'], '')


class CodexCatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.home = self.root / 'home'
        self.codex = self.home / '.codex'
        self.admin = self.root / 'etc' / 'codex'
        for target, name, value in [(catalog.paths, 'CODEX', self.codex),
                                    (catalog, 'HOME', self.home), (catalog, 'CODEX_ADMIN', self.admin)]:
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def skill(self, folder, name, description='A skill', directory=None):
        path = folder / (directory or name) / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nname: %s\ndescription: %s\n---\n' % (name, description))

    def test_builtins(self):
        items = catalog.codex_catalog(None)['commands']
        commands = {item['name']: item for item in items}
        for name in ('permissions', 'skills', 'goal', 'stop'):
            self.assertIn(name, commands)
        for name in ('approvals', 'undo', 'test-approval', 'debug-m-drop', 'debug-m-update', 'rollout', 'app'):
            self.assertNotIn(name, commands)
        self.assertEqual(commands['stop']['aliases'], ['clean'])
        self.assertEqual(commands['pwd']['aliases'], ['cwd'])
        self.assertEqual(commands['pets']['aliases'], ['pet'])
        self.assertEqual(commands['review']['hint'], '[instructions]')
        self.assertEqual(commands['skills']['hint'], '')
        self.assertEqual(len(items), 60)
        self.assertEqual([item['name'] for item in items[:3]], ['model', 'presets', 'daybreak'])
        for item in items:
            self.assertEqual(set(item), {'name', 'description', 'aliases', 'hint', 'kind'})
            self.assertTrue(not item['hint'] or (item['hint'].startswith('[') and item['hint'].endswith(']')))

    def test_skill_roots_and_precedence(self):
        repo = self.root / 'repo'
        cwd = repo / 'sub'
        (repo / '.git').mkdir(parents=True)
        folders = [repo / '.agents' / 'skills', cwd / '.agents' / 'skills', cwd / '.codex' / 'skills',
                   self.codex / 'skills', self.home / '.agents' / 'skills',
                   self.codex / 'skills' / '.system', self.admin / 'skills']
        for index, folder in enumerate(folders):
            self.skill(folder, 'skill-%d' % index)
            self.skill(folder, 'shared', 'layer-%d' % index, 'different-folder')
        self.skill(self.root / '.agents' / 'skills', 'outside')
        self.skill(folders[0] / 'nested', 'too-deep')
        items = catalog.codex_catalog(str(cwd))['commands']
        skills = [item for item in items if item['kind'] == 'skill']
        self.assertEqual([item['name'] for item in skills], ['$shared', '$skill-0'] + ['$skill-%d' % i for i in range(1, 7)])
        self.assertEqual(skills[0]['description'], 'layer-0')

    def test_no_git_ancestor_uses_only_cwd(self):
        cwd = self.root / 'sub'
        self.skill(self.root / '.agents' / 'skills', 'parent')
        self.skill(cwd / '.agents' / 'skills', 'local')
        # The host's temp folder may itself have a .git marker.
        with mock.patch.object(Path, 'exists', return_value=False):
            items = catalog.codex_catalog(str(cwd))['commands']
        self.assertEqual([item['name'] for item in items if item['kind'] == 'skill'], ['$local'])


if __name__ == '__main__':
    unittest.main()
