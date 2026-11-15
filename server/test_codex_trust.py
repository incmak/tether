"""Trust a folder for Codex (codex_trust.py). Files live in temporary folders; no real Codex config is read."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tomllib
import unittest
from unittest import mock

import approval
import codex_provider
import codex_trust
import paths
import project_launch
from test_approval import HAS_CRYPTO, Handler, PasskeyCase

KEEP = '''# my Codex settings
model = "gpt-test"   # keep this comment

[projects."/Users/old/mac"]
trust_level = "trusted"

[mcp_servers.docs]
command = "docs"  # and this one
'''


def compact(record):
    return json.dumps(record, separators=(',', ':'))  # Codex writes rollout lines without spaces


class Base(PasskeyCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop('CODEX_HOME', None)
        self.enterContext(mock.patch.object(project_launch, '_managed', return_value=False))
        self.enterContext(mock.patch.object(codex_provider, 'launch_profile', return_value=None))
        self.home = paths.CODEX
        self.config = self.home / 'config.toml'
        self.repo = self.root / 'work' / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
        self.entries = {}

    def write(self, text, mode=0o600):
        self.home.mkdir(parents=True, exist_ok=True)
        self.config.write_text(text)
        self.config.chmod(mode)

    def call(self, body):
        handler = Handler(codex_trust.PATH, body)
        codex_trust.post(handler, choose=lambda pane: self.entries[pane], agent_folder=lambda entry: entry['cwd'],
                         chat_folder=lambda path: path, session_folder=lambda: str(self.repo))
        return handler


class Edit(Base):
    def test_existing_table_keeps_every_other_line_and_comment(self):
        text = KEEP + '\n[projects."/workspace/app"]\n# why\ntrust_level = "untrusted"  # old\nsandbox_note = 1\n'
        self.write(text)
        codex_trust.write_trust('/workspace/app')
        result = self.config.read_text()
        self.assertEqual(result, text.replace('trust_level = "untrusted"  # old', 'trust_level = "trusted"'))
        self.assertEqual(tomllib.loads(result)['projects']['/Users/old/mac'], {'trust_level': 'trusted'})

    def test_table_without_trust_level_gets_the_line_after_its_header(self):
        self.write(KEEP + '[projects."/workspace/app"]\nnote = "x"\n')
        codex_trust.write_trust('/workspace/app')
        self.assertEqual(self.config.read_text(), KEEP + '[projects."/workspace/app"]\ntrust_level = "trusted"\nnote = "x"\n')

    def test_new_table_is_appended(self):
        self.write(KEEP.rstrip('\n'))
        codex_trust.write_trust('/workspace/app')
        self.assertEqual(self.config.read_text(), KEEP + '\n[projects."/workspace/app"]\ntrust_level = "trusted"\n')

    def test_missing_file_is_created_private(self):
        codex_trust.write_trust('/workspace/app')
        self.assertEqual(self.config.read_text(), '[projects."/workspace/app"]\ntrust_level = "trusted"\n')
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertFalse((self.home / codex_trust.BACKUP).exists())

    def test_path_with_quotes_spaces_and_backslash(self):
        key = '/workspace/my "odd" folder\\x'
        self.write(KEEP)
        codex_trust.write_trust(key)
        self.assertEqual(tomllib.loads(self.config.read_text())['projects'][key], {'trust_level': 'trusted'})
        codex_trust.write_trust(key)  # a second trust finds the quoted header and changes nothing
        self.assertEqual(self.config.read_text().count('trust_level = "trusted"'), 2)

    def test_mode_kept_and_backup_made_once(self):
        self.write(KEEP, mode=0o640)
        codex_trust.write_trust('/workspace/a')
        codex_trust.write_trust('/workspace/b')
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        backup = self.home / codex_trust.BACKUP
        self.assertEqual((backup.read_text(), stat.S_IMODE(backup.stat().st_mode)), (KEEP, 0o640))
        self.assertEqual(sorted(path.name for path in self.home.iterdir()),
                         ['.config.toml.tether-lock', 'config.toml', codex_trust.BACKUP])

    def test_symlinked_config_is_refused(self):
        real = self.root / 'elsewhere.toml'
        real.write_text(KEEP)
        self.home.mkdir(parents=True)
        self.config.symlink_to(real)
        with self.assertRaisesRegex(codex_trust.TrustError, 'symbolic link'):
            codex_trust.write_trust('/workspace/app')
        self.assertEqual(real.read_text(), KEEP)
        self.assertTrue(self.config.is_symlink())

    def test_forms_it_cannot_edit_exactly_are_refused_unchanged(self):
        for text in ('projects = { "/workspace/app" = { trust_level = "untrusted" } }\n[mcp_servers.docs]\ncommand = "d"\n',
                     'projects."/workspace/app".trust_level = "untrusted"\n' + KEEP,
                     KEEP + '[projects]\n"/workspace/app" = { trust_level = "untrusted" }\n',
                     KEEP + '[projects."/workspace/app"]\ntrust_level = """\n[x]\n"""\n',
                     'not toml = = 1\n'):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(codex_trust.TrustError):
                    codex_trust.write_trust('/workspace/app')
                self.assertEqual(self.config.read_text(), text)

    def test_change_during_write_is_refused(self):
        self.write(KEEP)
        real = codex_trust.edit

        def racing(text, key):
            self.config.write_text(KEEP + '# Codex wrote this\n')
            return real(text, key)
        with mock.patch.object(codex_trust, 'edit', racing), self.assertRaisesRegex(codex_trust.TrustError, 'changed'):
            codex_trust.write_trust('/workspace/app')
        self.assertEqual(self.config.read_text(), KEEP + '# Codex wrote this\n')
        self.assertFalse([path for path in self.home.iterdir() if path.name.startswith('.config.toml.tether-')
                          and path.name != '.config.toml.tether-lock'])


class Api(Base):
    def new(self, **extra):
        return {'op': 'status', 'mode': 'new', **extra}

    def test_status_names_the_canonical_repository_root(self):
        self.write(KEEP)
        (self.repo / 'sub').mkdir()
        link = self.root / 'link'
        link.symlink_to(self.repo)
        handler = self.call(self.new(cwd=str(link / 'sub')))
        self.assertEqual(handler.body, {'state': 'untrusted', 'folder': str(link / 'sub'), 'target': str(self.repo)})

    def test_status_uses_an_existing_entry_for_the_folder(self):
        self.write(KEEP + f'[projects.{project_launch.toml_string(str(self.repo))}]\nnote = 1\n')
        self.assertEqual(self.call(self.new()).body['target'], str(self.repo))
        self.write(KEEP + f'[projects.{project_launch.toml_string(str(self.repo))}]\ntrust_level = "untrusted"\n')
        self.assertEqual(self.call(self.new()).body, {'state': 'trusted', 'folder': str(self.repo), 'target': None})

    def test_status_other_when_trust_would_not_help(self):
        self.write('sandbox_mode = "read-only"\n' + KEEP)
        self.assertEqual(self.call(self.new()).body['state'], 'other')
        self.write(KEEP)
        with mock.patch.object(codex_provider, 'launch_profile', return_value='foundry'):
            self.assertEqual(self.call(self.new()).body['state'], 'other')

    def test_unapproved_trust_writes_nothing(self):
        self.write(KEEP)
        body = {'op': 'trust', 'mode': 'new', 'target': str(self.repo)}
        for extra in ({}, {'approval': {'challenge_id': 'x', 'credential': {}}}):
            handler = self.call({**body, **extra})
            self.assertEqual(handler.status, 403)
            self.assertIn(handler.body['approval'], ('no-passkey', 'required'))
            self.assertEqual(self.config.read_text(), KEEP)
            self.assertFalse((self.home / codex_trust.BACKUP).exists())

    def test_target_mismatch_is_refused_before_approval(self):
        self.write(KEEP)
        with mock.patch.object(approval, 'require') as require:
            handler = self.call({'op': 'trust', 'mode': 'new', 'target': '/workspace/other'})
        self.assertEqual(handler.status, 409)
        require.assert_not_called()
        self.assertEqual(self.config.read_text(), KEEP)

    def test_approved_trust_writes_and_binds_the_payload(self):
        self.write(KEEP)
        body = {'op': 'trust', 'mode': 'new', 'target': str(self.repo)}
        with mock.patch.object(approval, 'require') as require:
            handler = self.call({**body, 'approval': 'proof'})
        require.assert_called_once_with(mock.ANY, 'codex-trust', body, 'proof')
        self.assertEqual((handler.status, handler.body['state']), (200, 'trusted'))
        self.assertEqual(self.config.read_text(), KEEP + f'\n[projects.{project_launch.toml_string(str(self.repo))}]\n'
                                                         'trust_level = "trusted"\n')
        self.assertEqual(self.call(body).status, 409)  # already trusted

    @unittest.skipUnless(HAS_CRYPTO, 'cryptography is not installed')
    def test_passkey_assertion_for_another_target_is_refused(self):
        self.write(KEEP)
        self.add()
        body = {'op': 'trust', 'mode': 'new', 'target': str(self.repo)}
        wrong = self.assertion('codex-trust', {**body, 'target': '/workspace/other'})
        self.assertEqual(self.call({**body, 'approval': wrong}).status, 403)
        self.assertEqual(self.config.read_text(), KEEP)
        handler = self.call({**body, 'approval': self.assertion('codex-trust', body)})
        self.assertEqual((handler.status, handler.body['state']), (200, 'trusted'), handler.body)

    def test_running_tab_read_only_then_restart(self):
        self.write(KEEP)
        transcript = self.root / 'rollout.jsonl'
        turn = {'type': 'turn_context', 'payload': {'cwd': str(self.repo), 'sandbox_policy': {'type': 'read-only'}}}
        transcript.write_text(compact({'type': 'session_meta', 'payload': {}}) + '\n' + compact(turn) + '\n')
        self.entries['%5'] = {'agent': 'codex', 'owner_pid': 42, 'transcript': str(transcript)}
        body = {'op': 'status', 'mode': 'running', 'session': '%5'}
        with mock.patch.object(codex_trust.procinfo, 'argv', return_value=[b'codex', b'-m', b'gpt-test', b'']), \
                mock.patch.object(codex_trust.procinfo, 'cwd', return_value=str(self.root)):
            self.assertEqual(self.call(body).body, {'state': 'untrusted', 'folder': str(self.repo), 'target': str(self.repo)})
            with mock.patch.object(approval, 'require'):
                handler = self.call({**body, 'op': 'trust', 'target': str(self.repo)})
            self.assertEqual((handler.status, handler.body.get('state')), (200, 'restart'), handler.body)
            self.assertEqual(self.call(body).body['state'], 'restart')
            turn['payload']['sandbox_policy']['type'] = 'workspace-write'
            with transcript.open('a') as stream:
                stream.write(compact(turn) + '\n')
            self.assertEqual(self.call(body).body['state'], 'trusted')
        self.entries['%6'] = {'agent': 'claude', 'owner_pid': 43}
        self.assertEqual(self.call({**body, 'session': '%6'}).status, 409)

    @unittest.skipUnless(shutil.which('git'), 'git is not installed')
    def test_linked_worktree_trusts_the_main_checkout(self):
        shutil.rmtree(self.repo)
        self.repo.mkdir(parents=True)
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', '-c', 'init.defaultBranch=main']
        subprocess.run([*git, 'init', '-q', str(self.repo)], check=True)
        subprocess.run([*git, '-C', str(self.repo), 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
        linked = self.root / 'work' / 'linked'
        subprocess.run([*git, '-C', str(self.repo), 'worktree', 'add', '-q', '-b', 'b', str(linked)], check=True)
        self.write(KEEP)
        self.assertEqual(self.call(self.new(cwd=str(linked))).body['target'], str(self.repo))
        self.assertEqual(self.call(self.new(worktree=True)).body['target'], str(self.repo))


if __name__ == '__main__':
    unittest.main()
