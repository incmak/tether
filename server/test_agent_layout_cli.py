import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import agent_layout_cli as cli
import auth
import custom_layouts as layouts
from test_custom_layouts import GOOD, changed
from own_time import own_time


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.file = Path(folder) / 'cockpit.json'
        self.file.write_text(json.dumps(GOOD))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.client = self.enterContext(patch.object(cli, 'Client')).return_value

    def test_manifest_and_validate_are_local(self):
        self.assertEqual(cli.main(['manifest']), 0)
        self.assertEqual(json.loads(self.output.getvalue()), layouts.MANIFEST)
        self.output.seek(0)
        self.output.truncate()
        self.assertEqual(cli.main(['validate', str(self.file)]), 0)
        self.assertEqual(self.output.getvalue(), 'OK\n')
        cli.Client.assert_not_called()
        self.file.write_text('{bad')
        self.assertEqual(cli.main(['validate', str(self.file)]), 1)
        self.assertIn('not valid JSON', self.errors.getvalue())

    def test_save_overwrite_and_list(self):
        self.client.request.return_value = {'layout': layouts.validate(GOOD, 'cockpit'), 'file': 'cockpit.json'}
        self.assertEqual(cli.main(['save', str(self.file), '--overwrite']), 0)
        self.assertEqual(self.client.request.call_args.args[0], '/save')
        self.assertTrue(self.client.request.call_args.args[1]['overwrite'])
        self.client.request.return_value = {'layouts': [dict(changed(id='custom'), file='custom.json')], 'errors': []}
        self.output.seek(0)
        self.output.truncate()
        self.assertEqual(cli.main(['list']), 0)
        self.assertIn('classic\tClassic\t(built-in)', self.output.getvalue())
        self.assertIn('custom\tCockpit\tcustom.json', self.output.getvalue())

    def test_generate_polls_and_optional_save(self):
        clean = layouts.validate(GOOD, 'cockpit')
        self.client.request.side_effect = [{'job': 'job-1'}, {'state': 'running'}, {'state': 'done', 'layout': clean}, {'layout': clean}]
        with own_time(cli, sleep=Mock()) as sleep:
            self.assertEqual(cli.main(['generate', 'like Telegram', '--save']), 0)
        sleep.assert_called_once_with(1.5)
        self.assertEqual(json.loads(self.output.getvalue()), clean)
        self.assertEqual(self.client.request.call_args.args, ('/save', {'layout': clean, 'overwrite': False}))
        self.client.request.side_effect = [{'job': 'job-2'}, {'state': 'done', 'layout': clean}]
        self.assertEqual(cli.main(['generate', 'quiet']), 0)
        self.assertEqual(self.client.request.call_args.args, ('/generate?job=job-2',))

    def test_failed_generation_and_deadline_are_reported(self):
        self.client.request.side_effect = [{'job': 'job-1'}, {'state': 'failed', 'error': 'Bad contrast.'}]
        self.assertEqual(cli.main(['generate', 'quiet']), 1)
        self.assertIn('Bad contrast.', self.errors.getvalue())
        self.client.request.side_effect = [{'job': 'job-2'}]
        with own_time(cli, monotonic=Mock(side_effect=[0, 421])):
            self.assertEqual(cli.main(['generate', 'quiet']), 1)
        self.assertIn('took too long', self.errors.getvalue())


class ClientTests(unittest.TestCase):
    def test_client_uses_owner_cookie_origin_header_and_port(self):
        with patch.dict(cli.os.environ, {'MOBILE_TMUX_PORT': '8080'}), \
                patch.object(auth, 'TOKEN_FILE') as token, patch.object(cli, 'build_opener') as opener:
            token.read_text.return_value = 'fixture-secret'
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
            client = cli.Client()
            self.assertEqual(client.request('/save', {'layout': {}}), {'ok': True})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:8080/mobile/api/layouts/save')
            self.assertEqual(request.get_header('Origin'), 'http://127.0.0.1:8080')
            self.assertEqual(request.get_header('X-codex-mobile'), '1')
            self.assertEqual(request.get_header('Cookie'), auth.COOKIE + '=' + auth.session_value('fixture-secret'))
            self.assertEqual(request.get_method(), 'POST')
            response = HTTPError(request.full_url, 409, 'conflict', {}, io.BytesIO(b'{"error":"Already exists."}'))
            opener.return_value.open.side_effect = response
            with self.assertRaisesRegex(ValueError, 'Already exists'):
                client.request('/save', {})
