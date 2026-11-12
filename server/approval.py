"""User-verified WebAuthn approval for requests that raise agent power."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import auth
import paths
import push

PATH = '/mobile/api/passkeys'
ACTIONS = ('setup-hook', 'setup-ping', 'decision-save', 'level-raise', 'passkey-add', 'passkey-remove')
NO_PASSKEY = 'Add a passkey in Settings › Setup first.'
APPROVE = 'Approve with your passkey.'
OTHER_HOST = 'Add more passkeys from the Tether address where your first passkey works.'
_LOCK = threading.RLock()
_CHALLENGES = {}


class ApprovalError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Invalid base64url.')
    result = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    if _b64(result) != value:
        raise ValueError('Invalid base64url.')
    return result


def _read():
    try:
        data = json.loads((paths.CONFIG / 'passkeys.json').read_bytes())
    except FileNotFoundError:
        return {'passkeys': []}
    if not isinstance(data, dict) or not isinstance(data.get('passkeys'), list):
        raise ValueError('Invalid passkey store.')
    return data


def _write(data):
    paths.CONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.passkeys-', dir=paths.CONFIG)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.write('\n')
        os.replace(name, paths.CONFIG / 'passkeys.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def rp_id(handler):
    host = urlsplit('//' + handler.headers.get('Host', '')).hostname
    if not host:
        raise ValueError('Invalid host.')
    return host.lower()


def _context(handler):
    host = rp_id(handler)
    origin = handler.headers.get('Origin', '')
    parts = urlsplit(origin)
    if (not auth.same_origin(origin, handler.headers.get('Host', ''))
            or parts.username is not None or parts.password is not None
            or parts.path or parts.query or parts.fragment
            or not (parts.scheme == 'https' or (parts.scheme == 'http' and auth.is_loopback(host)))):
        raise ValueError('Passkeys need HTTPS or a loopback host.')
    return host, origin


def status(rp_id):
    with _LOCK:
        rows = _read()['passkeys']
        return {'passkeys': [{key: row[key] for key in ('id', 'name', 'created', 'last_used')} for row in rows],
                'here': sum(row['rp_id'] == rp_id for row in rows)}


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).digest()


def options(handler, kind, action, payload):
    if (kind not in ('get', 'create') or action not in ACTIONS or not isinstance(payload, dict)
            or (kind == 'create' and action != 'passkey-add')):
        raise ValueError('Choose a passkey action.')
    host, origin = _context(handler)
    with _LOCK:
        data = _read()
        credentials = [{'id': row['id'], 'type': 'public-key'} for row in data['passkeys'] if row['rp_id'] == host]
        if kind == 'get' and not credentials:
            raise ApprovalError(403, NO_PASSKEY)
        public = {'challenge': _b64(secrets.token_bytes(32)), 'timeout': 60000}
        if kind == 'get':
            public.update(rpId=host, allowCredentials=credentials, userVerification='required')
        else:
            if len(data['passkeys']) >= 10:
                raise ValueError('Tether allows at most 10 passkeys.')
            if 'user_id' not in data:
                data['user_id'] = _b64(secrets.token_bytes(16))
                _write(data)
            public.update(rp={'id': host, 'name': 'Tether'},
                          user={'id': data['user_id'], 'name': 'tether', 'displayName': 'Tether'},
                          pubKeyCredParams=[{'type': 'public-key', 'alg': -7}],
                          authenticatorSelection={'residentKey': 'preferred', 'userVerification': 'required'},
                          excludeCredentials=credentials, attestation='none')
        now = time.monotonic()
        for key in list(_CHALLENGES):
            if now - _CHALLENGES[key]['time'] >= 120:
                del _CHALLENGES[key]
        while len(_CHALLENGES) >= 50:
            del _CHALLENGES[next(iter(_CHALLENGES))]
        identifier = secrets.token_urlsafe(24)
        _CHALLENGES[identifier] = dict(kind=kind, action=action, digest=_digest(payload),
                                      host=host, origin=origin, challenge=public['challenge'], time=now)
        return {'challenge_id': identifier, 'publicKey': public}


class _CBOR:
    """The definite-length CBOR subset used by WebAuthn and COSE."""
    def __init__(self, data):
        if not isinstance(data, bytes) or len(data) > 4096:
            raise ValueError('Invalid CBOR size.')
        self.data, self.pos = data, 0

    def take(self, size):
        end = self.pos + size
        if end > len(self.data):
            raise ValueError('Truncated CBOR.')
        result, self.pos = self.data[self.pos:end], end
        return result

    def value(self, depth=0):
        if depth > 8:
            raise ValueError('CBOR is too deep.')
        head = self.take(1)[0]
        major, info = head >> 5, head & 31
        if major == 7:
            if info not in (20, 21, 22):
                raise ValueError('Unsupported CBOR simple value.')
            return {20: False, 21: True, 22: None}[info]
        if major > 5 or info >= 28:
            raise ValueError('Unsupported CBOR type.')
        size = info if info < 24 else int.from_bytes(self.take(1 << (info - 24)), 'big')
        if major in (0, 1):
            return size if major == 0 else -1 - size
        if size > 4096:
            raise ValueError('Invalid CBOR length.')
        if major in (2, 3):
            raw = self.take(size)
            return raw if major == 2 else raw.decode('utf-8')
        if major == 4:
            return [self.value(depth + 1) for _ in range(size)]
        result = {}
        for _ in range(size):
            key, value = self.value(depth + 1), self.value(depth + 1)
            if not isinstance(key, (str, bytes, int)) or key in result:
                raise ValueError('Invalid CBOR map key.')
            result[key] = value
        return result


def _cbor(data):
    decoder = _CBOR(data)
    result = decoder.value()
    if decoder.pos != len(data):
        raise ValueError('Trailing CBOR.')
    return result


def _client(handler, kind, action, payload, identifier, credential):
    # Pop before verification: even a failed ceremony cannot reuse its challenge.
    pending = _CHALLENGES.pop(identifier, None) if isinstance(identifier, str) else None
    host, origin = _context(handler)
    if (pending is None or time.monotonic() - pending['time'] >= 120
            or any(pending[key] != value for key, value in (
                ('kind', kind), ('action', action), ('digest', _digest(payload)), ('host', host), ('origin', origin)))):
        raise ValueError('Invalid challenge.')
    if not isinstance(credential, dict) or credential.get('type') != 'public-key':
        raise ValueError('Invalid credential.')
    identifier = _unb64(credential['id'])
    if identifier != _unb64(credential['rawId']):
        raise ValueError('Credential identifiers differ.')
    response = credential['response']
    raw = _unb64(response['clientDataJSON'])
    client = json.loads(raw)
    expected_type = 'webauthn.create' if kind == 'create' else 'webauthn.get'
    if (client['type'] != expected_type or client['challenge'] != pending['challenge']
            or client['origin'] != origin or client.get('crossOrigin', False) is not False):
        raise ValueError('Invalid client data.')
    return host, identifier, response, raw


def _auth_data(raw, host):
    if len(raw) < 37 or raw[:32] != hashlib.sha256(host.encode()).digest() or raw[32] & 5 != 5:
        raise ValueError('Invalid authenticator data.')
    return int.from_bytes(raw[33:37], 'big')


def _require(handler, action, payload, approval, data):
    host = rp_id(handler)
    rows = [row for row in data['passkeys'] if row['rp_id'] == host]
    if not rows:
        raise ApprovalError(403, NO_PASSKEY)
    try:
        host, identifier, response, client = _client(handler, 'get', action, payload,
                                                    approval['challenge_id'], approval['credential'])
        row = next(row for row in rows if _unb64(row['id']) == identifier)
        raw = _unb64(response['authenticatorData'])
        count = _auth_data(raw, host)
        key = serialization.load_der_public_key(_unb64(row['public_key']))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError('Invalid key.')
        key.verify(_unb64(response['signature']), raw + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if row['sign_count'] > 0 and count > 0 and count <= row['sign_count']:
            raise ValueError('Invalid signature counter.')
    except (ValueError, KeyError, TypeError, StopIteration, InvalidSignature):
        raise ApprovalError(403, APPROVE) from None
    row.update(sign_count=count, last_used=_now())
    _write(data)


def require(handler, action, payload, approval):
    with _LOCK:
        _require(handler, action, payload, approval, _read())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _notice(verb, name):
    try:
        # Do not generate push keys when push is not configured.
        if push.available() and push.KEYS.exists():
            push.send_all({'title': 'Tether', 'body': f'A passkey was {verb} to Tether: {name}'
                           if verb == 'added' else f'A passkey was removed from Tether: {name}',
                           'tag': 'passkeys', 'url': '/chat/'})
    except Exception:  # A push failure must not undo a passkey change.
        pass


def enroll(handler, name, credential, approval=None):
    if not isinstance(name, str) or not 1 <= len(name) <= 40 or not name.isprintable():
        raise ValueError('Use 1–40 printable characters for the passkey name.')
    with _LOCK:
        host, _ = _context(handler)
        data = _read()
        if len(data['passkeys']) >= 10:
            raise ValueError('Tether allows at most 10 passkeys.')
        # Trust on first use applies once for the whole store, not once per host name: an agent can send any
        # Host header, so a new host must not be a new first passkey.
        if data['passkeys']:
            if not any(row['rp_id'] == host for row in data['passkeys']):
                raise ApprovalError(409, OTHER_HOST)
            _require(handler, 'passkey-add', {'name': name}, approval, data)
        try:
            # Direct Python callers can supply the browser credential alone. HTTP also supplies its challenge id.
            if not isinstance(credential, dict):
                raise ValueError('Invalid credential.')
            challenge_id = credential.get('challenge_id')
            if challenge_id is None:
                client = json.loads(_unb64(credential['response']['clientDataJSON']))
                challenge_id = next((key for key, pending in _CHALLENGES.items()
                                     if pending['challenge'] == client['challenge']), None)
            host, identifier, response, _ = _client(handler, 'create', 'passkey-add', {'name': name},
                                                    challenge_id, credential)
            attestation = _cbor(_unb64(response['attestationObject']))
            if attestation['fmt'] not in ('none', 'packed'):
                raise ValueError('Unsupported attestation.')
            raw = attestation['authData']
            count = _auth_data(raw, host)
            if raw[32] & 64 == 0 or len(raw) < 55:
                raise ValueError('Missing attested credential.')
            size = int.from_bytes(raw[53:55], 'big')
            if not size or raw[55:55 + size] != identifier:
                raise ValueError('Invalid credential identifier.')
            decoder = _CBOR(raw[55 + size:])
            cose = decoder.value()
            if (not isinstance(cose, dict) or any(type(cose.get(k)) is not int or cose[k] != v
                                                for k, v in ((1, 2), (3, -7), (-1, 1)))
                    or any(not isinstance(cose.get(k), bytes) or len(cose[k]) != 32 for k in (-2, -3))):
                raise ValueError('Use a P-256 ES256 passkey.')
            if raw[32] & 128:
                decoder.value()  # Optional authenticator extensions must also be definite CBOR.
            if decoder.pos != len(decoder.data):
                raise ValueError('Trailing authenticator data.')
            key = ec.EllipticCurvePublicNumbers(int.from_bytes(cose[-2], 'big'),
                                               int.from_bytes(cose[-3], 'big'), ec.SECP256R1()).public_key()
            if any(row['id'] == _b64(identifier) and row['rp_id'] == host for row in data['passkeys']):
                raise ValueError('This passkey already exists.')
        except (ValueError, KeyError, TypeError, IndexError):
            raise ApprovalError(403, APPROVE) from None
        data['passkeys'].append({'id': _b64(identifier), 'public_key': _b64(key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)),
            'sign_count': count, 'name': name, 'created': _now(), 'last_used': None, 'rp_id': host})
        _write(data)
    _notice('added', name)


def remove(handler, passkey_id, approval):
    with _LOCK:
        data = _read()
        _require(handler, 'passkey-remove', {'id': passkey_id}, approval, data)
        row = next((row for row in data['passkeys'] if row['id'] == passkey_id), None)
        if row is None:
            raise ValueError('Choose a passkey.')
        data['passkeys'].remove(row)
        _write(data)
    _notice('removed', row['name'])


def handles(path):
    return path.split('?', 1)[0] == PATH


def error(handler, exc):
    result = {'error': str(exc)}
    if exc.status == 403:
        result['approval'] = 'no-passkey' if str(exc) == NO_PASSKEY else 'required'
    handler.respond(exc.status, result)


def get(handler):
    try:
        handler.respond(200, status(rp_id(handler)))
    except ValueError as exc:
        handler.respond(400, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Tether could not read the passkeys.'})


def post(handler, body=None):
    try:
        if body is None:
            size = int(handler.headers.get('Content-Length', '0') or 0)
            if not 0 < size <= 16384:
                raise ApprovalError(413, 'Invalid request.')
            body = json.loads(handler.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError('Invalid request.')
        op = body.get('op')
        if op == 'options':
            handler.respond(200, options(handler, body.get('kind'), body.get('action'), body.get('payload')))
            return
        if op == 'enroll':
            credential = body.get('credential')
            if not isinstance(credential, dict) or not isinstance(body.get('challenge_id'), str):
                raise ValueError('Invalid credential.')
            enroll(handler, body.get('name'), dict(credential, challenge_id=body.get('challenge_id')), body.get('approval'))
        elif op == 'remove':
            remove(handler, body.get('id'), body.get('approval'))
        else:
            raise ValueError('Choose a passkey operation.')
        handler.respond(200, status(rp_id(handler)))
    except ApprovalError as exc:
        error(handler, exc)
    except (ValueError, UnicodeError) as exc:
        handler.respond(400, {'error': str(exc)})
    except OSError:
        handler.respond(500, {'error': 'Tether could not save the passkeys.'})
