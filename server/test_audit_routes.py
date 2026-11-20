"""Audit API and permission decisions with a mocked terminal."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import audit
import server
import terminal_input


class AuditRouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(audit.paths, 'CONFIG', self.folder))

    def handler(self, method, path, body=None):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = path, method
        handler.client_address = ('127.0.0.1', 1234)
        handler.respond = Mock()
        data = json.dumps(body).encode() if body is not None else b''
        origin = '127.0.0.1:1234'
        handler.headers = {'Host': origin, 'Origin': f'http://{origin}', 'X-Codex-Mobile': '1',
                           'X-Tether-Client': 'web', 'User-Agent': 'Chrome/1 (Macintosh)',
                           'Content-Length': str(len(data))}
        handler.rfile = io.BytesIO(data)
        return handler

    def get(self, path, signed_in=True):
        handler = self.handler('GET', audit.PATH + path)
        with patch.object(server.auth, 'signed_in', return_value=signed_in), \
                patch.object(server.auth, 'tailscale_signed_in', return_value=False):
            handler.do_GET()
        return handler.respond.call_args.args

    def test_get_passes_filters_and_returns_requested_facets(self):
        with patch.object(audit, 'query', return_value={'entries': [], 'next': None}) as query, \
                patch.object(audit, 'facets', return_value={'kinds': [], 'projects': [], 'actors': []}) as facets:
            status, body = self.get('?q=level&kind=settings,review.link&project=Tether&actor=Chrome'
                                    '&since=1.25&until=99&limit=2&before=0000000000000100-abcdef&facets=1')
        self.assertEqual(status, 200)
        query.assert_called_once_with(text='level', kinds=['settings', 'review.link'], project='Tether',
                                      actor='Chrome', since=1.25, until=99.0, limit=2,
                                      before='0000000000000100-abcdef')
        facets.assert_called_once_with()
        self.assertEqual(body, {'entries': [], 'next': None, 'retention_days': 90,
                                'facets': {'kinds': [], 'projects': [], 'actors': []}})
        self.assertEqual(self.get(''), (200, {'entries': [], 'next': None, 'retention_days': 90}))

    def test_get_requires_auth_and_reports_bad_numbers_and_disk_errors(self):
        with patch.object(audit, 'query') as query:
            self.assertEqual(self.get('', signed_in=False), (401, {'error': 'Sign in required.'}))
            for parameter in ('limit=one', 'since=bad', 'until=nan'):
                with self.subTest(parameter=parameter):
                    status, body = self.get('?' + parameter)
                    self.assertEqual(status, 400)
                    self.assertTrue(body['error'])
            query.assert_not_called()
            query.side_effect = OSError('internal path')
            self.assertEqual(self.get(''), (500, {'error': 'Tether could not read the audit log.'}))

    def test_permission_decisions_use_verified_option_and_store_no_prompt_data(self):
        config = {'id': '%12', 'thread': 'chat', 'agent': 'codex', 'pane_name': 'Audit Log', 'writable': True}
        for choice, kind, action in ((2, 'approval.approve', 'approve'), (3, 'approval.deny', 'deny')):
            with self.subTest(kind=kind):
                screen = '\n'.join(['Command: sensitive prompt data', 'Would you like to run the following command?',
                                    '  1. Yes, proceed (y)',
                                    ('›' if choice == 2 else ' ') + " 2. Yes, and don't ask again",
                                    ('›' if choice == 3 else ' ') + ' 3. No, and tell Codex what to do differently'])
                identifier = terminal_input.approval(screen)['id']
                payload = {'session': '%12', 'thread': 'chat', 'approval': identifier, 'choice': choice,
                           'action': 'reject' if choice == 2 else 'approve'}
                handler = self.handler('POST', '/mobile/api/approval', payload)
                handler.deny = Mock(return_value=False)
                with patch.object(server, 'choose', return_value=config), patch.object(terminal_input, 'verify'), \
                        patch.object(terminal_input, 'capture', return_value=screen), \
                        patch.object(server.attention.STORE, 'clear'), \
                        patch.object(server, 'respond_approval', side_effect=lambda *args, **kwargs:
                                     terminal_input.respond_approval(*args, **kwargs, run=terminal)):
                    terminal = Mock()
                    for approval in (None, 'stale'):
                        stale = self.handler('POST', '/mobile/api/approval', dict(payload, approval=approval))
                        stale.deny = Mock(return_value=False)
                        stale.do_POST()
                        self.assertEqual(stale.respond.call_args.args[0], 503)
                        terminal.assert_not_called()
                    handler.do_POST()
                    handler.respond.assert_called_once_with(200, {'accepted': True})
                    self.assertEqual(terminal.call_args.args[0][-1], 'Enter')
                entry, = audit.query(kinds=[kind])['entries']
                self.assertEqual(entry['detail'], {'tab': 'Audit Log', 'action': action})
                self.assertEqual(entry['actor'], {'source': 'ui', 'device': 'Chrome on Mac'})
                self.assertNotIn(identifier, json.dumps(entry))
                self.assertNotIn('sensitive prompt data', json.dumps(entry))
