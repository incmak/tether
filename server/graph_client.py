"""Microsoft Graph for the work board feeds (Teams channels, an email folder), with Mak's own Entra app.

Delegated access only: Tether reads what the signed-in account can read. `tether graph connect --tenant <id>
--client <app id>` signs in with the authorization code flow and PKCE (a temporary redirect to 127.0.0.1) and saves the connection in paths.private_file('graph.json'),
mode 600: tenant, app id, the refresh token, the scopes and the account name. The tokens stay in this module: they
never go to the board, an item event, a log line or a command line. The server only reads the file, and writes it
back when Microsoft rotates the refresh token.

Requests: https only, to login.microsoftonline.com and graph.microsoft.com, no redirects, a 10 s timeout each and a
total deadline per poll (the jira_client watchdog). Only GETs go to Graph."""
import base64
import contextlib
import fcntl
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paths
from jira_client import MAX_BODY, TIMEOUT, _Cut, opener

FILE_NAME = 'graph.json'
LOCK_NAME = 'graph.lock'  # one writer of graph.json at a time, across the server and the command line
DEADLINE = 90
LOGIN = 'https://login.microsoftonline.com'
GRAPH = 'https://graph.microsoft.com/v1.0'
TENANT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9-]{1,63})+')
CLIENT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SCOPES = {'mail': ['Mail.ReadBasic'],  # subject, sender, time, links: no body, no admin consent
          'teams': ['Team.ReadBasic.All', 'Channel.ReadBasic.All', 'ChannelMessage.Read.All']}
BASE_SCOPES = ['offline_access', 'User.Read']
NOT_CONNECTED = 'Microsoft 365 is not connected. Run: tether graph connect --tenant <tenant id> --client <app id>'
EXPIRED = 'The Microsoft sign-in expired or was revoked. Run: tether graph connect'
LATE = 'Microsoft took too long. The feed tries again later.'
_LOCK = threading.Lock()   # one refresh at a time: Microsoft rotates the refresh token
_ACCESS = {}               # (tenant, client, refresh token) -> (access token, expiry)


class GraphError(Exception):
    def __init__(self, text, status=0):
        super().__init__(text)
        self.status = status


def config_path():
    return paths.private_file(FILE_NAME)


@contextlib.contextmanager
def locked():
    """Hold the file lock for a read, change and write of graph.json. The server's refresh, connect and disconnect
    take it, so a disconnect cannot be undone by a refresh that read the file before it."""
    path = paths.private_file(LOCK_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def scopes_for(kinds):
    return BASE_SCOPES + [scope for kind in ('mail', 'teams') if kind in kinds for scope in SCOPES[kind]]


def check(tenant, client):
    tenant, client = (tenant or '').strip().lower(), (client or '').strip().lower()
    if not TENANT.fullmatch(tenant):
        raise ValueError('Give the directory (tenant) id of your Entra app, or your domain, like contoso.com.')
    if not CLIENT.fullmatch(client):
        raise ValueError('Give the application (client) id of your Entra app.')
    return tenant, client


def load():
    """(config, why). config is None, and why says what to do, when the file is missing, open to others or broken."""
    path = config_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, NOT_CONNECTED
    except OSError as exc:
        return None, f'Cannot read {path.name}: {exc.strerror or exc}.'
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None, f'{path.name} must be a plain file of yours. Run: tether graph connect'
    if info.st_mode & 0o077:
        return None, f'{path.name} must be readable by you only. Run: chmod 600 {path}'
    try:
        config = json.loads(path.read_text())
        config['tenant'], config['client'] = check(config.get('tenant'), config.get('client'))
    except (OSError, ValueError, AttributeError, TypeError):
        return None, f'{path.name} is broken. Run: tether graph connect'
    if not isinstance(config.get('refresh'), str) or not config['refresh'] or not isinstance(config.get('scopes'), list):
        return None, f'{path.name} is incomplete. Run: tether graph connect'
    return config, ''


def save(config):
    """Write the connection atomically, mode 600 from the start."""
    with locked():
        _write(config)


def update(fields):
    """Add fields (the account name) to the saved connection. False when it is gone."""
    with locked():
        config, _ = load()
        if not config:
            return False
        _write({**config, **fields})
        return True


def _write(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def remove():
    with locked():
        config_path().unlink(missing_ok=True)
        _ACCESS.clear()


def status():
    """What the board shows: connected or not, the account, and which feeds the scopes allow. Never a token."""
    config, why = load()
    if not config:
        return {'connected': False, 'why': why}
    granted = {s.casefold() for s in config.get('granted') or config['scopes']}
    return {'connected': True, 'account': config.get('name') or config.get('account') or '',
            'mail': 'mail.readbasic' in granted or 'mail.read' in granted, 'teams': 'channelmessage.read.all' in granted}


def _error(raw, code):
    try:
        data = json.loads(raw or b'{}')
    except ValueError:
        data = {}
    if isinstance(data.get('error'), dict):  # Graph: {"error": {"code", "message"}}
        found = data['error'].get('message') or data['error'].get('code')
    else:  # login: {"error": "invalid_grant", "error_description": "AADSTS…"}
        found = data.get('error_description') or data.get('error')
    return str(found).split('\r\n')[0] if found else f'HTTP {code}'


class Client:
    """One poll's calls. deadline: seconds for all of them together. open_url, clock: for tests."""

    def __init__(self, config, open_url=None, clock=time.monotonic, deadline=DEADLINE, wall=time.time):
        self.config = config
        self.open = open_url or opener().open
        self.clock, self.wall = clock, wall
        self.end = clock() + deadline
        self._secrets = {config['refresh']}

    def _clean(self, text):
        for secret in sorted((s for s in self._secrets if s), key=len, reverse=True):
            text = text.replace(secret, '[hidden]')
        return text

    def _call(self, url, data=None, headers=None):
        """(json, status) of one request. Errors carry the status and no secret."""
        left = self.end - self.clock()
        if left <= 0:
            raise GraphError(LATE)
        body = None if data is None else urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(url, data=body, method='POST' if body else 'GET',
                                         headers={'Accept': 'application/json', **(headers or {})})
        request.cut = _Cut()
        watchdog = threading.Timer(left, request.cut.cut)
        watchdog.daemon = True
        watchdog.start()
        try:
            with self.open(request, timeout=min(TIMEOUT, left)) as response:
                raw, code = response.read(MAX_BODY + 1), response.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(4096) if exc.fp else b''
            except OSError:
                raw = b''
            finally:
                exc.close()
            if request.cut.done:
                raise GraphError(LATE) from None
            raise GraphError(self._clean(_error(raw, exc.code))[:300], exc.code) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if request.cut.done:
                raise GraphError(LATE) from None
            raise GraphError(self._clean(f'Microsoft did not answer ({getattr(exc, "reason", exc)}).')[:200]) from None
        except Exception as exc:  # http.client.HTTPException and the like: the connection broke mid-answer
            raise GraphError(self._clean(f'Microsoft did not answer ({type(exc).__name__}).')) from None
        finally:
            watchdog.cancel()
            request.cut.close()
        if request.cut.done or self.clock() > self.end:
            raise GraphError(LATE, code)
        if len(raw) > MAX_BODY:
            raise GraphError('The Microsoft answer was too large.', code)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise GraphError('The Microsoft answer was not JSON.', code) from None

    def token(self):
        """A live access token, refreshed with the saved refresh token (saved again when Microsoft rotates it)."""
        config = self.config
        with _LOCK, locked():
            fresh, why = load()  # another thread may have rotated the refresh token since this poll read it
            if not fresh:  # disconnected, or broken: never write the old connection back
                _ACCESS.clear()
                raise GraphError(why or NOT_CONNECTED, 401)
            if fresh['tenant'] != config['tenant'] or fresh['client'] != config['client']:
                raise GraphError(EXPIRED, 401)  # connected again to another app since this poll began
            config = self.config = fresh
            self._secrets.add(config['refresh'])
            key = (config['tenant'], config['client'], config['refresh'])
            cached = _ACCESS.get(key)
            if cached and cached[1] > self.wall() + 120:
                self._secrets.add(cached[0])
                return cached[0]
            try:
                data = self._call(f'{LOGIN}/{config["tenant"]}/oauth2/v2.0/token', {
                    'client_id': config['client'], 'grant_type': 'refresh_token', 'refresh_token': config['refresh'],
                    'scope': ' '.join(config['scopes'])})
            except GraphError as exc:
                if exc.status in (400, 401):  # invalid_grant: revoked, expired, or consent withdrawn
                    raise GraphError(EXPIRED, 401) from None
                raise
            access = data.get('access_token')
            if not isinstance(access, str) or not access:
                raise GraphError('Microsoft gave no access token.')
            self._secrets.add(access)
            rotated = data.get('refresh_token')
            if isinstance(rotated, str) and rotated and rotated != config['refresh']:
                self._secrets.add(rotated)
                config = self.config = dict(config, refresh=rotated)
                _write(config)
            _ACCESS.clear()
            _ACCESS[(config['tenant'], config['client'], config['refresh'])] = (
                access, self.wall() + max(60, int(data.get('expires_in') or 3600)))
            return access

    def get(self, path_or_link):
        """GET a Graph path (/me/...) or an @odata.nextLink that Graph gave."""
        url = path_or_link if path_or_link.startswith(GRAPH + '/') else GRAPH + path_or_link
        if not url.startswith(GRAPH + '/') or '\\' in url or '@' in urllib.parse.urlsplit(url).netloc:
            raise GraphError('Graph gave a link to another host.')
        if re.search(r'[\s\x00-\x1f\x7f]', url):  # a space or a control character: the caller did not encode it
            raise GraphError('Tether built a Graph address with a space in it. Nothing was sent.')
        try:
            return self._call(url, headers={'Authorization': 'Bearer ' + self.token()})
        except GraphError as exc:
            if exc.status == 401:
                _ACCESS.clear()
                raise GraphError(EXPIRED, 401) from None
            if exc.status == 403:
                raise GraphError(f'Microsoft refused: {exc}. Check the app permissions and admin consent.', 403) from None
            raise

    def pages(self, path, limit=5):
        """The items of up to limit pages."""
        out, link = [], path
        for _ in range(limit):
            data = self.get(link)
            out += [v for v in data.get('value') or [] if isinstance(v, dict)]
            link = data.get('@odata.nextLink')
            if not isinstance(link, str) or not link:
                break
        return out


def pkce():
    """(verifier, challenge): RFC 7636 S256. The verifier stays in this process; only the challenge leaves it."""
    verifier = secrets.token_urlsafe(72)  # 96 characters of A-Z a-z 0-9 - _
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode()
    return verifier, challenge


def redirect_uri(port):
    """The app registers http://localhost (Mobile and desktop applications); Entra ignores the port of a localhost
    redirect when it matches one."""
    return f'http://localhost:{port}'


def authorize_url(tenant, client, scopes, port, state, challenge):
    return f'{LOGIN}/{tenant}/oauth2/v2.0/authorize?' + urllib.parse.urlencode({
        'client_id': client, 'response_type': 'code', 'redirect_uri': redirect_uri(port), 'response_mode': 'query',
        'scope': ' '.join(scopes), 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'prompt': 'select_account'}, quote_via=urllib.parse.quote)


PAGE = ('<!doctype html><meta charset="utf-8"><title>Tether</title>'
        '<p style="font:16px system-ui;margin:2em">{}</p>')


class Listener:
    """The temporary redirect target: one HTTP server on 127.0.0.1:port for one sign-in answer. Each request runs on
    its own daemon thread with a 5 s socket timeout, so an idle or slow client never holds up the wait; the wait
    ends at its own deadline. Other requests, and answers whose state is not this sign-in's, get a 400 and the wait
    goes on. Nothing it receives is logged."""

    def __init__(self, port):
        found, done, lock = self.found, self.done, self.lock = {}, threading.Event(), threading.Lock()

        class Answer(http.server.BaseHTTPRequestHandler):
            timeout = 5  # seconds per socket read

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                state = (query.get('state') or [''])[0]
                with lock:
                    wanted = found.get('state', '')
                    ours = (bool(wanted) and parts.path in ('', '/') and not done.is_set()
                            and hmac.compare_digest(state.encode(), wanted.encode()))
                    if ours and query.get('code'):
                        found['code'] = query['code'][0]
                    elif ours:
                        found['error'] = ' '.join(filter(None, [(query.get('error') or [''])[0],
                                                                (query.get('error_description') or [''])[0]]))
                    if ours:
                        done.set()
                if not ours:
                    return self._page(400, 'This is not the answer to the sign-in that Tether is waiting for.')
                if 'code' in found:
                    return self._page(200, 'Signed in. You can close this tab and go back to the terminal.')
                return self._page(400, 'The sign-in did not finish. The terminal says why.')

            def _page(self, code, text):
                body = PAGE.format(html.escape(text)).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):  # the query holds the code
                pass

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False  # a slow client's thread never delays the end of the wait

        try:
            self.server = Server(('127.0.0.1', port), Answer)
        except OSError as exc:
            raise GraphError(f'Tether cannot listen on 127.0.0.1:{port} ({exc.strerror or exc}). '
                             'Choose another port with --port.') from None
        self.port = self.server.server_address[1]

    def wait(self, state, timeout=300):
        """The code of the answer with this state. Raises GraphError on a refusal or after timeout seconds."""
        with self.lock:
            self.found.clear()
            self.found['state'] = state
        loop = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True)
        loop.start()
        try:
            answered = self.done.wait(timeout)
        finally:
            self.server.shutdown()
            self.server.server_close()
        with self.lock:
            if not answered:
                raise GraphError('No sign-in answer came in time. Run tether graph connect again.')
            if 'error' in self.found:
                raise GraphError(f'Microsoft refused the sign-in: {self.found["error"][:300] or "no reason given"}.')
            return self.found['code']


def redeem(tenant, client, code, verifier, port, scopes, open_url=None):
    """Swap the code for tokens. A public client: no secret, the PKCE verifier proves the sign-in is ours."""
    caller = Client({'tenant': tenant, 'client': client, 'refresh': ''}, open_url=open_url, deadline=30)
    caller._secrets.update({code, verifier})
    return caller._call(f'{LOGIN}/{tenant}/oauth2/v2.0/token', {
        'client_id': client, 'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri(port),
        'code_verifier': verifier, 'scope': ' '.join(scopes)})
