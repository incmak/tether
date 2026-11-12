"""Synthetic WebAuthn ceremonies; all files and processes are isolated."""
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import approval
import codex_level
import paths
import server


def cbor(value):
    def head(major, size):
        if size < 24:
            return bytes([major * 32 + size])
        length = next(length for length in (1, 2, 4, 8) if size < 1 << (length * 8))
        return bytes([major * 32 + {1: 24, 2: 25, 4: 26, 8: 27}[length]]) + size.to_bytes(length, 'big')
    if value is None or type(value) is bool:
        return {None: b'\xf6', True: b'\xf5', False: b'\xf4'}[value]
    if isinstance(value, int):
        return head(0 if value >= 0 else 1, value if value >= 0 else -1 - value)
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, list):
        return head(4, len(value)) + b''.join(cbor(item) for item in value)
    return head(5, len(value)) + b''.join(cbor(key) + cbor(item) for key, item in value.items())


class Handler:
    def __init__(self, path=approval.PATH, body=None, host='tether.test:443', origin='https://tether.test:443'):
        raw = json.dumps(body).encode() if body is not None else b''
        self.path = path
        self.headers = {'Host': host, 'Origin': origin, 'X-Codex-Mobile': '1', 'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.status = self.body = None

    def respond(self, status, body):
        self.status, self.body = status, body


class PasskeyCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (
                *(mock.patch.object(paths, name, self.root / name.lower())
                  for name in ('CONFIG', 'TETHER', 'CLAUDE', 'CODEX')),
                mock.patch.object(approval, '_CHALLENGES', {}),
                mock.patch.object(approval.push, 'available', return_value=False),
                mock.patch.object(codex_level, 'models', return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.handler = Handler()
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.identifier = b'first-passkey'
        self.count = 1

    def registration(self, name='Phone', identifier=None, host='tether.test', flags=69, cose=None,
                     origin=None, fmt='none'):
        identifier = self.identifier if identifier is None else identifier
        opts = approval.options(self.handler, 'create', 'passkey-add', {'name': name})
        numbers = self.key.public_key().public_numbers()
        if cose is None:
            cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')}
        client = json.dumps({'type': 'webauthn.create', 'challenge': opts['publicKey']['challenge'],
                             'origin': origin or self.handler.headers['Origin']}).encode()
        raw = (hashlib.sha256(host.encode()).digest() + bytes([flags]) + (1).to_bytes(4, 'big')
               + b'\x00' * 16 + len(identifier).to_bytes(2, 'big') + identifier + cbor(cose))
        return {'op': 'enroll', 'name': name, 'challenge_id': opts['challenge_id'], 'credential': {
            'id': approval._b64(identifier), 'rawId': approval._b64(identifier), 'type': 'public-key',
            'response': {'clientDataJSON': approval._b64(client),
                         'attestationObject': approval._b64(cbor({'fmt': fmt, 'authData': raw, 'attStmt': {}}))}}}

    def add(self, **kwargs):
        body = self.registration(**kwargs)
        handler = Handler(body=body)
        approval.post(handler)
        self.assertEqual((handler.status, handler.body.get('here')), (200, 1), handler.body)
        return body

    def assertion(self, action='setup-hook', payload=None, count=None, identifier=None, key=None,
                  host='tether.test', origin=None, flags=5, client_type='webauthn.get'):
        payload = {'action': 'hook'} if payload is None else payload
        opts = approval.options(self.handler, 'get', action, payload)
        self.count += 1
        count = self.count if count is None else count
        raw = hashlib.sha256(host.encode()).digest() + bytes([flags]) + count.to_bytes(4, 'big')
        client = json.dumps({'type': client_type, 'origin': origin or self.handler.headers['Origin'],
                             'challenge': opts['publicKey']['challenge']}).encode()
        signature = (key or self.key).sign(raw + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        identifier = approval._b64(self.identifier if identifier is None else identifier)
        return {'challenge_id': opts['challenge_id'], 'credential': {
            'id': identifier, 'rawId': identifier, 'type': 'public-key', 'response': {
                'clientDataJSON': approval._b64(client), 'authenticatorData': approval._b64(raw),
                'signature': approval._b64(signature)}}}

    def require(self, value, action='setup-hook', payload=None):
        return approval.require(self.handler, action, {'action': 'hook'} if payload is None else payload, value)


class Ceremonies(PasskeyCase):
    def test_enroll_status_private_store_and_stable_user(self):
        self.assertEqual(approval.status('tether.test'), {'passkeys': [], 'here': 0})
        self.assertFalse(paths.CONFIG.exists())
        self.add()
        file = paths.CONFIG / 'passkeys.json'
        data = json.loads(file.read_bytes())
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(len(approval._unb64(data['user_id'])), 16)
        row = data['passkeys'][0]
        self.assertEqual((row['name'], row['rp_id'], row['sign_count'], row['last_used']), ('Phone', 'tether.test', 1, None))
        key = serialization.load_der_public_key(approval._unb64(row['public_key']))
        self.assertEqual(key.public_numbers(), self.key.public_key().public_numbers())
        opts = approval.options(self.handler, 'create', 'passkey-add', {'name': 'Next'})['publicKey']
        self.assertEqual(opts['user']['id'], data['user_id'])
        self.assertEqual(opts['rp'], {'id': 'tether.test', 'name': 'Tether'})
        self.assertEqual(opts['excludeCredentials'], [{'id': row['id'], 'type': 'public-key'}])
        self.assertEqual(opts['attestation'], 'none')
        self.assertEqual(opts['authenticatorSelection'], {'residentKey': 'preferred', 'userVerification': 'required'})
        self.assertEqual(approval.status('elsewhere.test')['here'], 0)
        self.assertNotIn('public_key', approval.status('tether.test')['passkeys'][0])

    def test_registration_rejects_wrong_origin_rp_flags_key_and_fmt(self):
        for kwargs in ({'origin': 'https://evil.test'}, {'host': 'evil.test'}, {'flags': 65},
                       {'flags': 68}, {'flags': 5}, {'cose': {1: 3, 3: -257, -1: b'RSA'}}, {'fmt': 'u2f'}):
            with self.subTest(kwargs=kwargs):
                handler = Handler(body=self.registration(**kwargs))
                approval.post(handler)
                self.assertEqual(handler.status, 403, handler.body)
                self.assertEqual(approval.status('tether.test')['here'], 0)

    def test_registration_rejects_bad_cbor_and_oversize(self):
        for raw in (b'\xff', b'\xa1\x63fmt', b'\x9f\xff', b'x' * 4097):
            with self.subTest(raw=raw[:10]):
                body = self.registration()
                body['credential']['response']['attestationObject'] = approval._b64(raw)
                handler = Handler(body=body)
                approval.post(handler)
                self.assertEqual(handler.status, 403)
                self.assertEqual(approval.status('tether.test')['here'], 0)

    def test_cbor_subset_depth_and_lengths(self):
        values = [0, -1000, 2**40, b'bytes', 'text', [1, None, False, True], {1: -7}]
        for value in values:
            self.assertEqual(approval._cbor(cbor(value)), value)
        bad = [b'\x9f\xff', b'\xc0\x00', b'\xf9\x00\x00', b'\xf7', b'\x1c', b'\x01\x02',
               b'\x58\x05hi', b'\x81' * 9 + b'\x00', b'\xa2\x01\x00\x01\x01', b'\x61\xff',
               b'\x5b' + (2**40).to_bytes(8, 'big')]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                approval._cbor(raw)

    def test_second_enroll_needs_valid_approval_and_remove_needs_approval(self):
        self.add()
        second = self.registration(name='Laptop', identifier=b'other')
        handler = Handler(body=second)
        approval.post(handler)
        self.assertEqual((handler.status, handler.body), (403, {'error': approval.APPROVE, 'approval': 'required'}))
        second['approval'] = self.assertion('passkey-add', {'name': 'Laptop'})
        handler = Handler(body=second)
        approval.post(handler)
        self.assertEqual((handler.status, handler.body['here']), (200, 2))
        payload = {'id': approval._b64(b'other')}
        handler = Handler(body={'op': 'remove', **payload})
        approval.post(handler)
        self.assertEqual(handler.status, 403)
        self.assertEqual(approval.status('tether.test')['here'], 2)
        proof = self.assertion('passkey-remove', payload)
        handler = Handler(body={'op': 'remove', **payload, 'approval': proof})
        approval.post(handler)
        self.assertEqual((handler.status, handler.body['here']), (200, 1))

    def test_assertion_updates_counter_and_last_used_and_replay_fails(self):
        self.add()
        value = self.assertion()
        self.require(value)
        row = json.loads((paths.CONFIG / 'passkeys.json').read_bytes())['passkeys'][0]
        self.assertEqual(row['sign_count'], 2)
        self.assertIsNotNone(row['last_used'])
        with self.assertRaises(approval.ApprovalError):
            self.require(value)

    def test_challenge_binds_kind_action_payload_host_origin_and_expires(self):
        self.add()
        for action, payload in (('setup-ping', {'action': 'hook'}), ('setup-hook', {'action': 'ping'})):
            with self.subTest(action=action, payload=payload), self.assertRaises(approval.ApprovalError):
                self.require(self.assertion(), action, payload)
        value = self.assertion()
        with mock.patch.object(approval.time, 'monotonic', return_value=approval._CHALLENGES[value['challenge_id']]['time'] + 120):
            with self.assertRaises(approval.ApprovalError):
                self.require(value)
        value = self.assertion()
        approval._CHALLENGES[value['challenge_id']]['kind'] = 'create'
        with self.assertRaises(approval.ApprovalError):
            self.require(value)
        value = self.assertion()
        with self.assertRaises(approval.ApprovalError):
            approval.require(Handler(origin='http://tether.test:443'), 'setup-hook', {'action': 'hook'}, value)
        with self.assertRaisesRegex(approval.ApprovalError, approval.NO_PASSKEY):
            approval.require(Handler(host='other.test', origin='https://other.test'), 'setup-hook', {'action': 'hook'}, value)

    def test_assertion_rejects_wrong_signature_count_origin_rp_uv_id_and_type(self):
        self.add()
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        for kwargs in ({'key': wrong_key}, {'count': 1}, {'host': 'evil.test'}, {'origin': 'https://evil.test'},
                       {'flags': 1}, {'flags': 4}, {'identifier': b'unknown'}, {'client_type': 'webauthn.create'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(approval.ApprovalError):
                self.require(self.assertion(**kwargs))
        self.require(self.assertion(count=5))
        with self.assertRaises(approval.ApprovalError):
            self.require(self.assertion(count=4))

    def test_zero_counter_authenticators_and_canonical_payload(self):
        self.add()
        self.require(self.assertion(count=0))
        self.require(self.assertion(count=0))
        payload = {'nested': {'b': 2, 'a': 1}, 'action': 'hook'}
        proof = self.assertion(payload=payload)
        self.require(proof, payload={'action': 'hook', 'nested': {'a': 1, 'b': 2}})

    def test_missing_malformed_approval_and_identifiers(self):
        with self.assertRaisesRegex(approval.ApprovalError, approval.NO_PASSKEY):
            self.require(None)
        self.assertFalse(paths.CONFIG.exists())
        self.add()
        for value in (None, [], {}, 'bad', {'challenge_id': [], 'credential': {}}):
            with self.subTest(value=value), self.assertRaisesRegex(approval.ApprovalError, approval.APPROVE):
                self.require(value)
        for field, value in (('rawId', 'eA'), ('type', 'password'), ('response', {})):
            proof = self.assertion()
            proof['credential'][field] = value
            with self.assertRaises(approval.ApprovalError):
                self.require(proof)

    def test_context_https_loopback_lowercase_and_host_ports(self):
        for host, origin, expected in (('LOCALHOST:8000', 'http://LOCALHOST:8000', 'localhost'),
                                      ('[::1]:8000', 'http://[::1]:8000', '::1'),
                                      ('127.0.0.1:8000', 'http://127.0.0.1:8000', '127.0.0.1'),
                                      ('TETHER.TEST:8443', 'https://TETHER.TEST:8443', 'tether.test')):
            opts = approval.options(Handler(host=host, origin=origin), 'create', 'passkey-add', {'name': 'Phone'})
            self.assertEqual(opts['publicKey']['rp']['id'], expected)
        for host, origin in (('tether.test', 'http://tether.test'), ('tether.test', 'https://evil.test'),
                             ('tether.test', ''), ('tether.test', 'https://tether.test/path')):
            with self.assertRaises(ValueError):
                approval.options(Handler(host=host, origin=origin), 'create', 'passkey-add', {})

    def test_challenge_cap_get_options_and_expiry_cleanup(self):
        self.add()
        opts = approval.options(self.handler, 'get', 'setup-hook', {'action': 'hook'})
        public = opts['publicKey']
        self.assertEqual(public['userVerification'], 'required')
        self.assertEqual(public['timeout'], 60000)
        self.assertEqual(len(approval._unb64(public['challenge'])), 32)
        self.assertEqual(public['allowCredentials'], [{'id': approval._b64(self.identifier), 'type': 'public-key'}])
        for _ in range(51):
            approval.options(self.handler, 'get', 'setup-hook', {})
        self.assertEqual(len(approval._CHALLENGES), 50)
        self.assertNotIn(opts['challenge_id'], approval._CHALLENGES)
        with mock.patch.object(approval.time, 'monotonic', return_value=10**12):
            approval.options(self.handler, 'get', 'setup-hook', {})
        self.assertEqual(len(approval._CHALLENGES), 1)

    def test_name_limit_max_passkeys_and_atomic_write_failure(self):
        for name in ('', 'a' * 41, 'bad\nname', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                approval.enroll(self.handler, name, {})
        self.add()
        old = (paths.CONFIG / 'passkeys.json').read_bytes()
        with mock.patch.object(approval.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.require(self.assertion())
        self.assertEqual((paths.CONFIG / 'passkeys.json').read_bytes(), old)
        self.assertEqual([p.name for p in paths.CONFIG.iterdir()], ['passkeys.json'])
        data = json.loads(old)
        data['passkeys'] = [dict(data['passkeys'][0], id=approval._b64(bytes([i]))) for i in range(10)]
        approval._write(data)
        with self.assertRaises(ValueError):
            approval.options(self.handler, 'create', 'passkey-add', {'name': 'Eleven'})
        with self.assertRaises(ValueError):
            approval.enroll(self.handler, 'Eleven', {})

    def test_new_host_is_not_a_second_first_use(self):
        # An agent can send any Host header, so a host without a passkey must not get a free first passkey.
        self.add()
        self.handler = Handler(host='other.test', origin='https://other.test')
        body = self.registration(host='other.test', identifier=b'other-rp')
        handler = Handler(body=body, host='other.test', origin='https://other.test')
        approval.post(handler)
        self.assertEqual((handler.status, handler.body), (409, {'error': approval.OTHER_HOST}))
        self.assertEqual(approval.status('other.test'), {'passkeys': approval.status('tether.test')['passkeys'], 'here': 0})
        self.assertEqual(len(approval.status('tether.test')['passkeys']), 1)

    def test_packed_attestation_is_accepted(self):
        self.handler = Handler(host='other.test', origin='https://other.test')
        body = self.registration(host='other.test', identifier=b'packed-rp', fmt='packed')
        handler = Handler(body=body, host='other.test', origin='https://other.test')
        approval.post(handler)
        self.assertEqual((handler.status, handler.body['here']), (200, 1))

    def test_push_notice_and_failure_are_optional(self):
        keyfile = self.root / 'push-keys.json'
        keyfile.touch()
        with mock.patch.object(approval.push, 'available', return_value=True), \
                mock.patch.object(approval.push, 'KEYS', keyfile), \
                mock.patch.object(approval.push, 'send_all') as send:
            self.add()
            self.assertEqual(send.call_args.args[0]['body'], 'A passkey was added to Tether: Phone')
            send.side_effect = OSError('offline')
            identifier = approval._b64(self.identifier)
            approval.remove(self.handler, identifier, self.assertion('passkey-remove', {'id': identifier}))
            self.assertEqual(send.call_args.args[0]['body'], 'A passkey was removed from Tether: Phone')
            self.assertEqual(approval.status('tether.test')['here'], 0)

    def test_http_validation_and_body_limit(self):
        for body in ([], {}, {'op': 'unknown'}, {'op': 'options', 'kind': 'create', 'action': 'setup-hook', 'payload': {}},
                     {'op': 'options', 'kind': 'get', 'action': 'other', 'payload': {}},
                     {'op': 'options', 'kind': 'get', 'action': 'setup-hook', 'payload': []}):
            handler = Handler(body=body)
            approval.post(handler)
            self.assertEqual(handler.status, 400, handler.body)
        for size in ('0', '-1', '16385'):
            handler = Handler(body={})
            handler.headers['Content-Length'] = size
            approval.post(handler)
            self.assertEqual(handler.status, 413)
        handler = Handler(body={'op': 'options', 'kind': 'get', 'action': 'setup-hook', 'payload': {}})
        approval.post(handler)
        self.assertEqual((handler.status, handler.body['approval']), (403, 'no-passkey'))

    def test_direct_enroll_api_and_registration_replay(self):
        body = self.registration(name=' ')
        approval.enroll(self.handler, body['name'], body['credential'])
        self.assertEqual(approval.status('tether.test')['here'], 1)
        body['approval'] = self.assertion('passkey-add', {'name': body['name']})
        handler = Handler(body=body)
        approval.post(handler)
        self.assertEqual(handler.status, 403)
        self.assertEqual(approval.status('tether.test')['here'], 1)

    def test_server_keeps_terminal_approval_and_rejects_oversize_passkey_body(self):
        handler = object.__new__(server.Handler)
        handler.path, handler.command = '/mobile/api/approval', 'POST'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        body = {'session': '%12', 'thread': 'chat', 'approval': 'terminal-prompt', 'choice': 1}
        request = Handler(body=body)
        handler.headers, handler.rfile = request.headers, request.rfile
        config = {'id': '%12', 'thread': 'chat', 'writable': True, 'agent': 'claude'}
        with mock.patch.object(server, 'choose', return_value=config), \
                mock.patch.object(server, 'respond_approval') as terminal, \
                mock.patch.object(server.attention.STORE, 'clear') as clear, \
                mock.patch.object(approval, 'post') as post:
            handler.do_POST()
            terminal.assert_called_once_with(config, 'terminal-prompt', None, choice=1, feedback=None)
            clear.assert_called_once_with('%12', 'chat')
            post.assert_not_called()
            handler.respond.assert_called_with(200, {'accepted': True})
        handler.path = approval.PATH
        handler.headers = dict(request.headers, **{'Content-Length': '16385'})
        with mock.patch.object(approval, 'post') as post:
            handler.do_POST()
            handler.respond.assert_called_with(413, {'error': 'Message is too large.'})
            post.assert_not_called()

    def test_server_dispatch_guards_and_keeps_terminal_approval(self):
        handler = object.__new__(server.Handler)
        handler.path = approval.PATH
        handler.command = 'POST'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        body = {'op': 'options', 'kind': 'create', 'action': 'passkey-add', 'payload': {'name': 'Phone'}}
        with mock.patch.object(approval, 'post') as post:
            request = Handler(body=body)
            handler.headers, handler.rfile = request.headers, request.rfile
            handler.do_POST()
            post.assert_called_once_with(handler, body)
            post.reset_mock()
            for change in ({'Origin': 'https://evil.test'}, {'X-Codex-Mobile': ''}):
                handler.headers = dict(request.headers, **change)
                handler.do_POST()
                handler.respond.assert_called_with(403, {'error': 'Reload the page and try again.'})
            handler.headers = request.headers
            handler.deny.return_value = True
            handler.do_POST()
            post.assert_not_called()
        handler.command = 'GET'
        handler.deny.return_value = False
        with mock.patch.object(approval, 'get') as get:
            handler.do_GET()
            get.assert_called_once_with(handler)
            get.reset_mock()
            handler.deny.return_value = True
            handler.do_GET()
            get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
