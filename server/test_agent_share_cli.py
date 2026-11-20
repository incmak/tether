import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from share_store import Store


class ShareCLITests(unittest.TestCase):
    def test_put_list_stat_path_and_errors(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            env = dict(os.environ, TETHER_HOME=str(root / 'home'), TETHER_SHARE=str(root / 'share'),
                       TETHER_SHARE_QUOTA_BYTES='100', TETHER_SHARE_MAX_BYTES='50')
            env.pop('TETHER_AGENT', None)
            source = root / 'source.txt'
            source.write_bytes(b'from agent')
            def run(*args, code=0):
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('agent_share_cli.py')), *args],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stderr + result.stdout)
                self.assertEqual(result.stderr, '')
                self.assertEqual(len(result.stdout.splitlines()), 1)
                return json.loads(result.stdout)
            file = run('put', str(source), '--name', '../../renamed.txt')['file']
            self.assertEqual(file['name'], 'renamed.txt')
            self.assertEqual(file['source'], 'agent:agent')
            self.assertEqual(run('list', '--kind', 'doc', '--limit', '1')['files'], [file])
            self.assertEqual(run('stat', file['id'])['file'], file)
            export = Path(run('path', file['id'])['path'])
            self.assertEqual(export.read_bytes(), b'from agent')
            self.assertEqual(export.stat().st_mode & 0o777, 0o600)
            codex = run('put', str(source), '--from', 'codex')['file']
            self.assertEqual(codex['source'], 'agent:codex')
            browser = Store(root / 'share').put(io.BytesIO(b'browser'), 7, 'browser.txt')
            self.assertEqual({f['id'] for f in run('list', '--source', 'agent')['files']}, {file['id'], codex['id']})
            self.assertEqual(run('list', '--source', 'you')['files'], [browser])
            env['TETHER_AGENT'] = 'claude'
            self.assertEqual(run('put', str(source))['file']['source'], 'agent:claude')
            self.assertEqual(run('put', str(source), '--from', 'codex')['file']['source'], 'agent:codex')
            for name in ('', 'bad/name', 'a' * 65):
                self.assertEqual(run('put', str(source), '--from', name, code=1), {'error': 'Choose a valid source.'})
            env['TETHER_AGENT'] = 'bad/name'
            self.assertEqual(run('put', str(source), code=1), {'error': 'Choose a valid source.'})
            self.assertEqual(run('list', '--source', 'bad', code=1), {'error': 'Choose a valid source.'})
            self.assertIn('error', run('stat', 'missing', code=1))
            source.write_bytes(b'x' * 51)
            self.assertIn('error', run('put', str(source), code=1))
            self.assertIn('error', run('put', str(root), code=1))
