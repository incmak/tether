"""Jira Cloud REST v3 for the work board, with Mak's own API token.

The connection lives in paths.private_file('jira.json'), mode 600: base URL, email, token, and the connected
account's accountId and name (from /myself at connect). The token stays in this module: it never goes to the board,
an item event, a log line or a command line. `tether jira connect` writes the file; the server only reads it.

Requests: https only, to *.atlassian.net or the api.atlassian.com gateway (scoped tokens), no redirects (urllib
would send the Authorization header on), a 10 s timeout each and a total deadline per job. At the deadline a
watchdog shuts the request's socket down, so no slow header, body or error read outlasts it. A GET is tried again on a
timeout, 429 or 5xx; a POST never is (work_jira reads the status again instead)."""
import base64
import http.client
import json
import os
import re
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paths

FILE_NAME = 'jira.json'
TIMEOUT = 10
DEADLINE = 60
MAX_BODY = 2_000_000
GETS = 3  # one try and two more
SITE = re.compile(r'[a-z0-9][a-z0-9-]*\.atlassian\.net')
GATEWAY = re.compile(r'/ex/jira/[0-9a-f-]{8,64}')
TICKET = re.compile(r'[A-Z][A-Z0-9]{0,19}-[1-9][0-9]{0,9}')
NOT_CONNECTED = 'Jira is not connected. Run: tether jira connect'
GUARD = ('Moves wait for a Jira guard: add the Only Assignee condition to the Done and QA transitions in the Jira '
         'workflow, then run: tether jira connect --assignee-guard')
LATE = 'Jira took too long. Try again.'
EXPIRED = 'The Jira token expired or was revoked. Run: tether jira connect'


class JiraError(Exception):
    """A Jira call failed. status: the HTTP status, 0 when Jira did not answer."""

    def __init__(self, text, status=0, wait=0):
        super().__init__(text)
        self.status, self.wait = status, wait


def config_path():
    return paths.private_file(FILE_NAME)


def check_base(url):
    """The API base URL, cleaned: https://<site>.atlassian.net or https://api.atlassian.com/ex/jira/<cloud id>."""
    parts = urllib.parse.urlsplit((url or '').strip())
    host = (parts.hostname or '').lower()
    path = parts.path.rstrip('/')
    if (parts.scheme != 'https' or parts.username or parts.password or parts.port or parts.query or parts.fragment
            or not ((SITE.fullmatch(host) and not path) or (host == 'api.atlassian.com' and GATEWAY.fullmatch(path)))):
        raise ValueError('Give https://<site>.atlassian.net, or https://api.atlassian.com/ex/jira/<cloud id> '
                         'for a token with scopes.')
    return f'https://{host}{path}'


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
        return None, f'{path.name} must be a plain file of yours. Run: tether jira connect'
    if info.st_mode & 0o077:
        return None, f'{path.name} must be readable by you only. Run: chmod 600 {path}'
    try:
        config = json.loads(path.read_text())
        config['base'] = check_base(config.get('base'))
    except (OSError, ValueError, AttributeError, TypeError):
        return None, f'{path.name} is broken. Run: tether jira connect'
    if not all(isinstance(config.get(key), str) and config[key] for key in ('email', 'token', 'accountId')):
        return None, f'{path.name} is incomplete. Run: tether jira connect'
    return config, ''


def save(config):
    """Write the connection atomically, mode 600 from the start."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
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
    config_path().unlink(missing_ok=True)


def status():
    """What the board shows: connected or not, the site and the account name. Never the token or the email."""
    config, why = load()
    if not config:
        return {'connected': False, 'why': why}
    site = urllib.parse.urlsplit(config['base'])
    return {'connected': True, 'site': site.hostname + (site.path if site.hostname == 'api.atlassian.com' else ''),
            'account': config.get('name') or config['accountId'], 'guard': config.get('guard') is True,
            **({} if config.get('guard') is True else {'why': GUARD})}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib then raises HTTPError with the 3xx code


class _Cut:
    """The sockets of one call. cut() at the deadline shuts them down: a blocked read then ends at once.

    It keeps a dup of each socket: TLS wrapping detaches the plain socket object, but a dup still names the same
    connection, and shutdown acts on the connection, so it also ends a TLS handshake or a TLS read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.socks, self.done = [], False

    def add(self, sock):
        copy = sock.dup()
        with self.lock:
            self.socks.append(copy)
            late = self.done
        if late:
            _shut(copy)

    def cut(self):
        with self.lock:
            self.done = True
            socks = list(self.socks)
        for sock in socks:
            _shut(sock)

    def close(self):
        with self.lock:
            socks, self.socks = self.socks, []
        for sock in socks:
            sock.close()


def _shut(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _Https(urllib.request.HTTPSHandler):
    """Gives each connection's socket to the request's _Cut, so the deadline can end the call."""

    def https_open(self, req):
        cut = getattr(req, 'cut', None)

        def connection(host, **kwargs):
            made = http.client.HTTPSConnection(host, **kwargs)
            if cut is not None:
                plain = made._create_connection

                def create(*args, **kw):
                    sock = plain(*args, **kw)
                    cut.add(sock)
                    return sock
                made._create_connection = create
            return made
        return self.do_open(connection, req, context=self._context)


def opener():
    return urllib.request.build_opener(_NoRedirect, _Https)


def _message(raw, code):
    try:
        data = json.loads(raw or b'{}')
        found = list(data.get('errorMessages') or []) + [str(v) for v in (data.get('errors') or {}).values()]
    except (ValueError, AttributeError):
        found = []
    text = '; '.join(m for m in found if isinstance(m, str))  # the caller hides secrets, then shortens
    return text or f'HTTP {code}'


class Client:
    """One job's calls. deadline: seconds for all of them together."""

    def __init__(self, config, open_url=None, sleep=time.sleep, clock=time.monotonic, deadline=DEADLINE):
        self.base = check_base(config['base'])
        pair = f'{config["email"]}:{config["token"]}'.encode()
        self._auth = 'Basic ' + base64.b64encode(pair).decode()
        self._secrets = sorted({s for s in (config['token'], self._auth, self._auth.split(' ', 1)[1]) if s}, key=len, reverse=True)
        self.open = open_url or opener().open
        self.sleep, self.clock = sleep, clock
        self.end = clock() + deadline

    def _clean(self, text):
        """Jira and the network can echo what was sent: no error text keeps the token or the Authorization value."""
        for secret in self._secrets:
            text = text.replace(secret, '[hidden]')
        return text

    def _call(self, method, path, body=None):
        left = self.end - self.clock()
        if left <= 0:
            raise JiraError(LATE)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            'Authorization': self._auth, 'Accept': 'application/json', 'Content-Type': 'application/json'})
        request.cut = _Cut()
        watchdog = threading.Timer(left, request.cut.cut)
        watchdog.daemon = True
        watchdog.start()
        try:
            raw, code = self._send(request, min(TIMEOUT, left))
        except JiraError:
            if request.cut.done:
                raise JiraError(LATE) from None
            raise
        finally:
            watchdog.cancel()
            request.cut.close()
        if request.cut.done or self.clock() > self.end:  # the answer came after the deadline
            raise JiraError(LATE, code)
        if len(raw) > MAX_BODY:
            raise JiraError('The Jira answer was too large.', code)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise JiraError('The Jira answer was not JSON.', code) from None

    def _send(self, request, timeout):
        """(body, status). Every failure becomes a JiraError with no secret in its text."""
        try:
            with self.open(request, timeout=timeout) as response:
                return response.read(MAX_BODY + 1), response.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(4096) if exc.fp else b''
            except (OSError, http.client.HTTPException):
                raw = b''
            finally:
                exc.close()
            if exc.code == 401:
                raise JiraError(EXPIRED, 401) from None
            wait = (exc.headers.get('Retry-After') or '') if exc.headers else ''
            raise JiraError(self._clean(_message(raw, exc.code))[:400], exc.code,
                            int(wait) if wait.isdigit() else 0) from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            reason = getattr(exc, 'reason', exc)
            raise JiraError(self._clean(f'Jira did not answer ({reason}).')[:200]) from None

    def get(self, path):
        for attempt in range(GETS):
            try:
                return self._call('GET', path)
            except JiraError as exc:
                if attempt == GETS - 1 or not (exc.status == 0 or exc.status == 429 or exc.status >= 500):
                    raise
                pause = min(exc.wait or 2 ** attempt, 10)
                if self.clock() + pause > self.end:
                    raise
                self.sleep(pause)

    def post(self, path, body):
        return self._call('POST', path, body)  # never tried again: the move may have happened

    def myself(self):
        return self.get('/rest/api/3/myself') or {}

    @staticmethod
    def _key(key):
        if not TICKET.fullmatch(key or ''):
            raise JiraError(f'{key} is not a Jira ticket key.')
        return key

    def issue(self, key):
        return self.get(f'/rest/api/3/issue/{self._key(key)}?fields=assignee,status') or {}

    def transitions(self, key):
        return (self.get(f'/rest/api/3/issue/{self._key(key)}/transitions') or {}).get('transitions') or []

    def transition(self, key, transition_id):
        self.post(f'/rest/api/3/issue/{self._key(key)}/transitions', {'transition': {'id': str(transition_id)}})
