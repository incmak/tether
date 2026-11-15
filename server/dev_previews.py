"""Preview a local dev server (http://localhost:5173/) from the phone, through a pool of ports.

An agent starts a dev server on this machine and posts a localhost link. The phone cannot open it. Tether forwards
the dev server on one port of a pool (7700-7709 by default); each pool port is its own origin once it is published:

    tailscale serve --bg --https=7700 http://127.0.0.1:7700

Flow: POST /mobile/api/dev-preview {port} on the main origin (cookie, Origin and X-Codex-Mobile checked there) maps
the dev port to a pool port (a lease) and returns two links with one-time tokens. The pool port trades a token for
its own cookie and redirects to the clean URL. Every other request needs that cookie, else it gets a 401 page.

Which dev ports: TCP listeners above 1024 on a loopback or wildcard address that Tether's own user owns, and never a
port on the deny list (Tether's own ports, debuggers, databases).

Security:
- The dev page runs in its own origin (another port), so it cannot read Tether's pages or call its APIs: those check
  Origin, and the browser shows no cross-origin answer to the page.
- Cookies span all ports of a host (RFC 6265, 8.5). Tether's session cookie and the preview cookies are HttpOnly, so
  a dev page's scripts cannot read them, and a script cannot replace an HttpOnly cookie. The proxy removes them from
  each request before the dev server sees it, and drops a Set-Cookie from the dev server that names one of them.
  A dev page can still fill the cookie jar until the browser drops Tether's cookie: that signs the person out, it
  does not let the page in. The session value is an HMAC of the access token, so a planted cookie is never valid.
- The dev server sees a plain localhost request: Host, Origin and Referer name localhost:<port>, and the proxy
  headers (X-Forwarded-*, Tailscale-*) stay here. Its host and CSRF checks then pass as they do on the machine.
- The preview cookie is SameSite=Strict, so another site cannot send signed-in requests to the dev server. Tailscale
  identity does not sign in here for the same reason: Serve adds it to every request, also one from another site.
- Ports of one host are one site, so the browser sends a preview port's cookie with a request from a page on
  another preview port. The proxy therefore checks where a request comes from (allowed_source): only the page's own
  origin may write, open a WebSocket or load a part of the page; another origin may only navigate to it.
- A dev port keeps its pool port when it can (stable_port), so an origin's storage stays with one dev server, as on
  plain localhost. When a browser signs in to a pool port that last showed it another dev server, the answer clears
  that origin's storage and cache (Clear-Site-Data) and the connections of the old lease end. A page of the old dev
  server that is still open in a browser without "executionContexts" support stays open until it is reloaded.
- After the proxy connects, it reads the owner of that very connection from the socket table and sends nothing when
  it is not Tether's user: another user cannot take a port that a dev server left.
- The proxy rebuilds the request and response framing (one Content-Length or chunked, never both) and refuses
  malformed heads, so no request can mean one thing to the proxy and another to the dev server (RFC 9112, 6.3).
- Connections, WebSockets, header time and write time are limited (Guard), so stalled clients cannot hold workers.
- Only Tether's main origin (and the dev page itself) may frame the page.
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import auth

DEFAULT_PORTS = '7700-7709'
MAX_POOL = 32
IDLE = 12 * 3600          # a lease ends after this long without a request
GONE = 10 * 60            # ... or this long after its dev server stopped listening (a restart keeps the lease)
REUSE_AFTER = 5 * 60      # a full pool takes the lease that is idle the longest, when it is idle at least this long
TICKET_LIFE = 120         # the link for the frame; the "Open in browser" link lives as long as the lease
MAX_TICKETS = 20
SCAN_TTL = 2.0            # how long one read of the listener table serves the proxy
OPEN_PATH = '/__tether_preview__/open'
COOKIE_PREFIX = 'tether_preview_'
# Never forwarded, whoever owns them: remote shells, debuggers (they run code), databases and caches.
DENIED_PORTS = frozenset({22, 2375, 2376, 3306, 5432, 6379, 9200, 9222, 9229, 11211, 27017})
MARKER_SUFFIX = '_app'    # tether_preview_<pool>_app: which dev server this browser last saw on the pool port
MARKER_LIFE = 365 * 86400
CLEAR = '"cache", "storage", "executionContexts"'  # never "cookies": cookies span all ports, Tether's session too
HEAD_LIMIT = 65536
HEAD_WAIT = 300           # a dev server may compile for a while before it answers; an absolute limit for its head
IDLE_STREAM = 900         # a plain answer that sends nothing for this long is cut; WebSockets have no idle limit
MAX_CONNECTIONS = 256     # open connections on all pool ports together
MAX_SOCKETS = 64          # WebSockets (and other upgrades) among them
ACCEPT_WAIT = 5           # how long a new connection waits for a free slot before it is closed
HEAD_DEADLINE = 20        # the whole request head must arrive in this time, however slowly it drips
WRITE_TIMEOUT = 60        # a peer that takes no bytes for this long loses the connection
LEASE_CHECK = 5           # an open WebSocket ends this soon after its lease ends
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-connection', 'proxy-authorization', 'expect', 'host', 'cookie',
               'origin', 'referer', 'forwarded', 'content-length', 'transfer-encoding', 'te', 'trailer', 'upgrade'}
LOCAL_NAMES = ('localhost', '127.0.0.1', '0.0.0.0', '[::1]')
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
BAD_VALUE = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')
STATUS_LINE = re.compile(rb'HTTP/1\.[01] ([1-5][0-9][0-9])(?: [\t\x20-\x7e\x80-\xff]*)?')
FIELD_LINE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\t\x20-\x7e\x80-\xff]*")
CHUNK_SIZE = re.compile(rb'([0-9A-Fa-f]{1,8})(?:[ \t]*;[\t\x20-\x7e\x80-\xff]*)?')
LENGTH = re.compile(r'[0-9]{1,15}')
HOST_VALUE = re.compile(r'[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::[0-9]{1,5})?')


class Refused(ValueError):
    """Tether does not forward this port. `code` tells the page which state to show."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_ports(text):
    """The pool from MOBILE_TMUX_DEV_PREVIEW_PORTS: '7700-7709', '7700,7705' or a mix. '', '0' or 'off' turns it off."""
    text = (text or '').strip().lower()
    if text in ('', '0', 'off', 'none'):
        return []
    ports = []
    for part in text.split(','):
        first, dash, last = part.strip().partition('-')
        try:
            low = int(first)
            high = int(last) if dash else low
        except ValueError:
            raise ValueError('MOBILE_TMUX_DEV_PREVIEW_PORTS must look like 7700-7709.') from None
        if not 1024 < low <= high <= 65535:
            raise ValueError('MOBILE_TMUX_DEV_PREVIEW_PORTS must name ports from 1025 to 65535.')
        ports.extend(range(low, high + 1))
        if len(ports) > MAX_POOL:
            raise ValueError('MOBILE_TMUX_DEV_PREVIEW_PORTS may name at most %d ports.' % MAX_POOL)
    return sorted(set(ports))


def pool_ports(environ=os.environ):
    return parse_ports(environ.get('MOBILE_TMUX_DEV_PREVIEW_PORTS', DEFAULT_PORTS))


# listeners ---------------------------------------------------------------------------------------------------------
def _proc_address(text):
    """An address from /proc/net/tcp or tcp6: the kernel prints each 32-bit word in the machine's byte order."""
    raw = bytes.fromhex(text)
    if len(raw) not in (4, 16):
        raise ValueError(text)
    if sys.byteorder == 'little':
        raw = b''.join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4))
    return ipaddress.ip_address(raw)


def parse_proc_net(text):
    """Listening sockets from /proc/net/tcp or tcp6: [{'address', 'port', 'uid', 'inode'}]. Other lines are skipped."""
    found = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != '0A':  # 0A: TCP_LISTEN
            continue
        try:
            address, _, port = fields[1].rpartition(':')
            found.append({'address': _proc_address(address), 'port': int(port, 16), 'uid': int(fields[7]),
                          'inode': int(fields[9])})
        except ValueError:
            continue
    return found


def parse_lsof(text, uid):
    """Listening sockets from `lsof -nP -iTCP -sTCP:LISTEN -a -u UID -Fpcn` (macOS has no /proc)."""
    found, pid, name = [], None, ''
    for line in text.splitlines():
        kind, value = line[:1], line[1:]
        if kind == 'p':
            pid, name = value, ''
        elif kind == 'c':
            name = value
        elif kind == 'n':
            host, _, port = value.rpartition(':')
            try:
                address = ipaddress.ip_address('0.0.0.0' if host == '*' else host.strip('[]'))
                found.append({'address': address, 'port': int(port), 'uid': uid, 'inode': 0, 'pid': pid, 'name': name})
            except ValueError:
                continue
    return found


def read_listeners():
    """Every listening TCP socket this system shows. Linux: all users, from /proc. macOS: this user's, from lsof."""
    if sys.platform == 'darwin':
        uid = os.getuid()
        try:
            result = subprocess.run(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-a', '-u', str(uid), '-Fpcn'],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            raise Refused('unsupported', 'Tether cannot list the dev servers on this machine (lsof did not run).') from None
        return parse_lsof(result.stdout, uid)
    found = []
    for name in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(name, encoding='ascii', errors='replace') as stream:
                found += parse_proc_net(stream.read())
        except FileNotFoundError:
            continue  # no IPv6 on this machine
        except OSError:
            raise Refused('unsupported', 'Tether cannot list the dev servers on this machine.') from None
    return found


def _plain(address):
    mapped = getattr(address, 'ipv4_mapped', None)
    return mapped if mapped is not None else address


def connection_uid(sock, tries=4, pause=0.02, root='/proc/net'):
    """The uid that owns the other end of a connected loopback socket, from the kernel's socket table (Linux). None
    when the table does not show the connection. This names the very connection, so no other listener can stand in.
    A connection that waits for accept() may show no owner yet on some kernels: look again a few times."""
    local, peer = sock.getsockname()[:2], sock.getpeername()[:2]
    ends = (ipaddress.ip_address(peer[0]), peer[1], ipaddress.ip_address(local[0]), local[1])
    ends = (_plain(ends[0]), ends[1], _plain(ends[2]), ends[3])
    suffix, found = ':%04X' % peer[1], None
    for attempt in range(tries):
        for name in ('tcp', 'tcp6'):
            try:
                with open('%s/%s' % (root, name), encoding='ascii', errors='replace') as stream:
                    lines = stream.read().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10 or fields[3] == '0A' or not fields[1].endswith(suffix):
                    continue
                try:
                    here, there = fields[1].rpartition(':'), fields[2].rpartition(':')
                    row = (_plain(_proc_address(here[0])), int(here[2], 16), _plain(_proc_address(there[0])), int(there[2], 16))
                    if row == ends:
                        found = (int(fields[7]), int(fields[9]))
                except ValueError:
                    continue
        if found is not None and found[1]:
            break  # accepted: the owner is final
        if attempt + 1 < tries:
            time.sleep(pause)
    return found[0] if found is not None else None


def connect_address(address):
    """Where the proxy reaches a listener on `address`, or None when a localhost link would not reach it either."""
    mapped = getattr(address, 'ipv4_mapped', None)
    if mapped is not None:
        address = mapped
    if address.is_loopback:
        return str(address)
    if address.is_unspecified:
        return '127.0.0.1' if address.version == 4 else '::1'
    return None


def process_names(inodes, uid, root='/proc', limit=20000):
    """{inode: {'name', 'folder'}} for this user's listening sockets (Linux): the command and its folder's name."""
    wanted = {'socket:[%d]' % inode: inode for inode in inodes if inode}
    found = {}
    if not wanted:
        return found
    try:
        pids = [name for name in os.listdir(root) if name.isdigit()]
    except OSError:
        return found
    looked = 0
    for pid in pids:
        base = '%s/%s' % (root, pid)
        try:
            if os.stat(base).st_uid != uid:
                continue
            links = os.listdir(base + '/fd')
        except OSError:
            continue
        for link in links:
            looked += 1
            if looked > limit:
                return found
            try:
                inode = wanted.get(os.readlink('%s/fd/%s' % (base, link)))
            except OSError:
                continue
            if inode is None or inode in found:
                continue
            entry = {'name': '', 'folder': ''}
            try:
                with open(base + '/comm', encoding='utf-8', errors='replace') as stream:
                    entry['name'] = stream.read().strip()[:40]
                entry['folder'] = os.path.basename(os.readlink(base + '/cwd'))[:60]
            except OSError:
                pass
            found[inode] = entry
        if len(found) == len(wanted):
            break
    return found


# leases ------------------------------------------------------------------------------------------------------------
class Lease:
    def __init__(self, pool_port, dev_port, now):
        self.pool_port = pool_port
        self.dev_port = dev_port
        self.secret = secrets.token_urlsafe(32)
        self.used = now
        self.gone = None      # when the dev server was first seen not listening
        self.tickets = {}     # one-time token -> the time it ends
        self.hint = None      # 'next': Next.js refused a dev request


class DevPreviews:
    """The pool: which dev port each pool port forwards, and who may use it. Thread-safe."""

    def __init__(self, ports=(), denied=(), clock=time.time, listeners=read_listeners, uid=None, names=process_names,
                 peer_uid=connection_uid if sys.platform.startswith('linux') else None):
        self.peer_uid = peer_uid  # None: no socket table on this system (macOS); verify() reads the listeners again
        self.ports = list(ports)
        self.denied = set(DENIED_PORTS) | set(denied) | set(self.ports)
        self.clock = clock
        self.read = listeners
        self.uid = os.getuid() if uid is None else uid
        self.names = names
        self.lock = threading.Lock()
        self.leases = {}  # pool port -> Lease
        self.scan = None  # (monotonic time, listeners)

    # which ports -----------------------------------------------------------------------------------------------
    def listeners(self, fresh=False):
        now = time.monotonic()
        with self.lock:
            scan = self.scan
        if fresh or scan is None or now - scan[0] > SCAN_TTL:
            scan = (now, self.read())
            with self.lock:
                self.scan = scan
        return scan[1]

    def check_port(self, port):
        if type(port) is not int or not 0 < port <= 65535:
            raise Refused('bad-port', 'That is not a port number.')
        if port <= 1024:
            raise Refused('denied', 'Tether forwards ports above 1024 only.')
        if port in self.denied:
            raise Refused('denied', 'Tether does not forward port %d.' % port)

    def target(self, port, fresh=False):
        """The address the proxy connects to for dev port `port`. Refused when the port may not be forwarded now."""
        self.check_port(port)
        other = False
        best = None
        for entry in self.listeners(fresh):
            if entry['port'] != port:
                continue
            address = connect_address(entry['address'])
            if entry['uid'] != self.uid or address is None:
                other = True
                continue
            rank = (entry['address'].is_unspecified, ':' in address)
            if best is None or rank < best[0]:
                best = (rank, address)
        if best is not None:
            return best[1]
        if other:
            raise Refused('denied', 'Port %d belongs to another user or does not listen on localhost.' % port)
        raise Refused('not-listening', 'Nothing listens on port %d. Start the dev server, then try again.' % port)

    def verify(self, sock, port, expected=None):
        """True when the connected socket `sock` reaches a dev server of Tether's own user. Called after connect and
        before the first byte: between the listener check and connect, another user may have taken the port."""
        try:
            if self.peer_uid is not None:
                return self.peer_uid(sock) == self.uid
            return self.target(port, fresh=True) == expected
        except (Refused, OSError, ValueError):
            return False

    def stable_port(self, dev_port, free):
        """The pool port for a dev port, from the free ones: always the same one when it is free (also after a
        restart), so the origin and its storage stay with one dev server, as they do on localhost:<port>."""
        wanted = self.ports[dev_port % len(self.ports)]
        return wanted if wanted in free else free[0]

    def alive(self, lease):
        """False once the lease ended or its pool port went to another dev server: its open connections must end."""
        with self.lock:
            return self.leases.get(lease.pool_port) is lease and self.clock() - lease.used <= IDLE

    def servers(self):
        """This user's dev servers that Tether can forward: [{'port', 'name', 'folder'}], by port."""
        found = {}
        for entry in self.listeners(fresh=True):
            port = entry['port']
            if (entry['uid'] != self.uid or port <= 1024 or port in self.denied or port in found
                    or connect_address(entry['address']) is None):
                continue
            found[port] = entry
        names = self.names([entry['inode'] for entry in found.values()], self.uid) if sys.platform != 'darwin' else {}
        return [{'port': port, 'name': names.get(entry['inode'], {}).get('name') or entry.get('name', ''),
                 'folder': names.get(entry['inode'], {}).get('folder', '')}
                for port, entry in sorted(found.items())]

    # leases ----------------------------------------------------------------------------------------------------
    def _sweep(self, now):
        for port, lease in list(self.leases.items()):
            if now - lease.used > IDLE or (lease.gone is not None and now - lease.gone > GONE):
                del self.leases[port]

    def open(self, dev_port, path='/'):
        """Forward `dev_port` and return the links: {'port', 'dev_port', 'frame', 'browser'}. Refused with a reason."""
        if not self.ports:
            raise Refused('off', 'Dev server previews are turned off on this server.')
        self.target(dev_port, fresh=True)
        path = safe_path(path)
        now = self.clock()
        with self.lock:
            self._sweep(now)
            lease = next((item for item in self.leases.values() if item.dev_port == dev_port), None)
            if lease is None:
                free = [port for port in self.ports if port not in self.leases]
                if not free:
                    free = self._reusable(now)
                if not free:
                    raise Refused('pool-full', 'All %d preview ports are in use. Stop a dev server you do not need, '
                                               'or wait %d minutes.' % (len(self.ports), REUSE_AFTER // 60))
                # A new Lease has a new secret: the cookie and the connections of the port's old lease end here.
                port = self.stable_port(dev_port, free)
                lease = self.leases[port] = Lease(port, dev_port, now)
            lease.used, lease.gone = now, None
            lease.tickets = {key: end for key, end in lease.tickets.items() if end > now}
            while len(lease.tickets) > MAX_TICKETS - 2:
                del lease.tickets[min(lease.tickets, key=lease.tickets.get)]
            frame, browser = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            lease.tickets[frame] = now + TICKET_LIFE
            lease.tickets[browser] = now + IDLE
            after = '&next=' + quote(path, safe='')
            return {'port': lease.pool_port, 'dev_port': dev_port, 'frame': OPEN_PATH + '?token=' + frame + after,
                    'browser': OPEN_PATH + '?token=' + browser + after}

    def _reusable(self, now):
        """A full pool: first the leases whose dev server is gone, then the one that is idle the longest."""
        try:
            listening = {entry['port'] for entry in self.read()}
        except Refused:
            listening = None
        if listening is not None:
            dead = [port for port, lease in self.leases.items() if lease.dev_port not in listening]
            if dead:
                return dead
        port, lease = min(self.leases.items(), key=lambda item: item[1].used)
        return [port] if now - lease.used >= REUSE_AFTER else []

    def redeem(self, pool_port, token):
        """The lease for a one-time token, which is spent now. None for an unknown, used or old token."""
        now = self.clock()
        with self.lock:
            self._sweep(now)
            lease = self.leases.get(pool_port)
            if lease is None or not token:
                return None
            for known in list(lease.tickets):
                if secrets.compare_digest(known.encode(), token.encode()):
                    end = lease.tickets.pop(known)
                    if end > now:
                        lease.used = now
                        return lease
            return None

    def authorize(self, pool_port, cookies):
        """The lease when a Cookie header carries this pool port's cookie. Each request keeps the lease alive."""
        now = self.clock()
        with self.lock:
            self._sweep(now)
            lease = self.leases.get(pool_port)
            if lease is None:
                return None
            if any(secrets.compare_digest(value.encode('utf-8', 'replace'), lease.secret.encode())
                   for value in cookie_values(cookies, COOKIE_PREFIX + str(pool_port))):
                lease.used = now
                return lease
            return None

    def mark(self, lease, listening):
        with self.lock:
            lease.gone = None if listening else lease.gone if lease.gone is not None else self.clock()

    def state(self):
        """What the page shows next to a preview: [{'port', 'dev_port', 'hint'}]."""
        with self.lock:
            self._sweep(self.clock())
            return [{'port': lease.pool_port, 'dev_port': lease.dev_port, 'hint': lease.hint}
                    for lease in self.leases.values()]


def cookie_values(cookies, name):
    """Every value that the Cookie headers `cookies` give for `name`."""
    return [value for text in cookies or () for key, _, value in (part.strip().partition('=') for part in text.split(';'))
            if key == name]


def marker_key():
    return auth.token().encode()


def marker(pool_port, dev_port):
    """The value of the cookie that records which dev server a browser last saw on a pool port. It is signed with the
    access token: a dev page on another port cannot plant one to keep the storage of an old dev server in place."""
    text = '%d:%d' % (pool_port, dev_port)
    return '%d.%s' % (dev_port, hmac.new(marker_key(), b'dev-preview-app:' + text.encode(), hashlib.sha256).hexdigest()[:32])


def allowed_source(method, upgrading, headers, public):
    """True when the browser says this request comes from the forwarded page itself, or is a plain navigation to it.
    Ports of one host are one site: the browser sends a pool port's SameSite=Strict cookie with a request from a page
    on another pool port. So the cookie alone does not show who asks. Origin decides when it is there; else the
    browser's fetch metadata. A write or a WebSocket with neither is refused."""
    origin, site = headers.get('Origin'), (headers.get('Sec-Fetch-Site') or '').lower()
    if origin is not None:
        return origin == public
    if site in ('same-origin', 'none'):
        return True
    safe = method in ('GET', 'HEAD') and not upgrading
    if site:  # same-site or cross-site: another origin may open the page, never load a part of it
        return safe and (headers.get('Sec-Fetch-Mode') or '').lower() == 'navigate' \
            and (headers.get('Sec-Fetch-Dest') or '').lower() in ('document', 'iframe')
    return safe  # an old browser or a tool that sends no fetch metadata: a read without Origin is not a CORS read


def request_framing(headers, version):
    """('none' | 'length' | 'chunked', length) for a request. ValueError for a head that two servers could read in
    two ways: a parse defect, a control character, two different lengths, a length with chunked, another coding."""
    if headers.defects:
        raise ValueError('defect')
    for name, value in headers.items():
        if not TOKEN.fullmatch(name) or BAD_VALUE.search(value):
            raise ValueError('header')
    lengths = [value.strip() for value in headers.get_all('Content-Length') or ()]
    codings = [value.strip().lower() for value in headers.get_all('Transfer-Encoding') or ()]
    if codings:
        if lengths or codings != ['chunked'] or version != 'HTTP/1.1':
            raise ValueError('coding')
        return 'chunked', None
    if lengths:
        if len(set(lengths)) != 1 or not LENGTH.fullmatch(lengths[0]):
            raise ValueError('length')
        return 'length', int(lengths[0])
    return 'none', 0


def response_framing(head, code, method, upgrading):
    """('upgrade' | 'none' | 'length' | 'chunked' | 'close', length) for a response head, or None when it is ambiguous."""
    lengths, codings = [], []
    for line in head.split(b'\r\n')[1:]:
        name, _, value = line.partition(b':')
        if name.lower() == b'content-length':
            lengths.append(value.strip().decode('latin-1'))
        elif name.lower() == b'transfer-encoding':
            codings.append(value.strip().lower())
    if (lengths and codings) or (codings and codings != [b'chunked']) or len(set(lengths)) > 1 \
            or (lengths and not LENGTH.fullmatch(lengths[0])):
        return None
    length = int(lengths[0]) if lengths else None
    if code == 101:
        return ('upgrade', None) if upgrading and not lengths and not codings else None
    if method == 'HEAD' or code in (204, 304):
        return 'none', length
    if codings:
        return 'chunked', None
    return ('length', length) if lengths else ('close', None)


def safe_path(path):
    """A path on the dev server for the redirect after sign-in: it starts with one '/', so it never names another site."""
    if not isinstance(path, str) or not path.startswith('/') or path[1:2] in ('/', '\\') or len(path) > 2048 \
            or any(ord(letter) < 0x21 or ord(letter) == 0x7f for letter in path):
        return '/'
    return path


def reserved_cookie(name):
    """True for a cookie that belongs to Tether: its session, or a preview port's key. A dev server never gets or sets one."""
    name = name.strip().lower()
    return name == auth.COOKIE.lower() or name.startswith(COOKIE_PREFIX)


def dev_cookies(values):
    """The Cookie header for the dev server: the browser's cookies without Tether's. None when nothing is left."""
    kept = [part.strip() for text in values or () for part in text.split(';')
            if part.strip() and not reserved_cookie(part.partition('=')[0])]
    return '; '.join(kept) or None


def set_cookie(value):
    """A Set-Cookie value from the dev server for the public origin, or None to drop it. The Domain attribute goes:
    'Domain=localhost' does not match the public host, and a host-only cookie is what the dev server means."""
    parts = value.split(';')
    if reserved_cookie(parts[0].partition('=')[0]):
        return None
    return ';'.join([parts[0]] + [part for part in parts[1:] if part.strip().lower().partition('=')[0].strip() != 'domain'])


def to_local(value, public, local):
    """An Origin or Referer value as the dev server would see it on the machine. Other sites stay as they are."""
    if value == public or value.startswith(public + '/'):
        return local + value[len(public):]
    return value


def to_public(value, public, port):
    """A Location or CORS value from the dev server that names itself, for the public origin."""
    for scheme in ('http://', 'https://'):
        for name in LOCAL_NAMES:
            prefix = '%s%s:%d' % (scheme, name, port)
            if value.lower().startswith(prefix) and value[len(prefix):len(prefix) + 1] in ('', '/', '?', '#'):
                return public + value[len(prefix):]
    return value


def response_code(head):
    """The status code of a response head (no blank line). None when a part is malformed: a bare CR or LF, a folded
    line or a bad field name. A browser reads some of these forms, so a header could pass the filter unseen."""
    lines = head.split(b'\r\n')
    match = STATUS_LINE.fullmatch(lines[0])
    if not match or not all(FIELD_LINE.fullmatch(line) for line in lines[1:]):
        return None
    return int(match.group(1))


def frame_ancestors(host, main_port):
    """Who may frame a forwarded page: the page itself and Tether's main origin on the same host."""
    name = urlsplit('//' + host).hostname or ''
    if not re.fullmatch(r'[a-z0-9.-]{1,253}', name):
        return "'self'"
    return "'self' https://%s http://%s:%d https://%s:%d" % (name, name, main_port, name, main_port)


PAGE = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '%(extra)s<title>%(title)s</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
        'font:16px/1.5 system-ui,sans-serif;color:#333;background:#fff}main{max-width:26rem;padding:1.5rem;text-align:center}'
        'h1{font-size:1.1rem;margin:0 0 .5rem}p{margin:0}@media(prefers-color-scheme:dark){body{color:#ddd;background:#1a1a1a}}'
        '</style><main><h1>%(title)s</h1><p%(role)s>%(text)s</p></main>%(script)s</html>')
# The drawer listens for this message and shows its own state with a retry (PreviewDrawer.tsx). It carries no secret.
TELL_PARENT = '<script>if(parent!==window)parent.postMessage({tetherPreview:"%s"},"*")</script>'


def page(title, text, state=None, refresh=None):
    return (PAGE % {'title': title, 'text': text, 'role': ' role="status"' if refresh else '',
                    'extra': '<meta http-equiv="refresh" content="%d">' % refresh if refresh else '',
                    'script': TELL_PARENT % state if state else ''}).encode()


# the proxy ---------------------------------------------------------------------------------------------------------
class Guard:
    """Limits for all pool ports together, so stalled or hostile clients cannot hold every worker: how many
    connections and WebSockets are open, and how long a request head may take from its first byte to its last."""

    def __init__(self, connections=MAX_CONNECTIONS, sockets=MAX_SOCKETS, accept=ACCEPT_WAIT, head=HEAD_DEADLINE,
                 write=WRITE_TIMEOUT, upstream_head=HEAD_WAIT, idle=IDLE_STREAM, lease_check=LEASE_CHECK):
        self.connections = threading.BoundedSemaphore(connections)
        self.sockets = threading.BoundedSemaphore(sockets)
        self.accept, self.head, self.write = accept, head, write
        self.upstream_head, self.idle, self.lease_check = upstream_head, idle, lease_check
        self.lock = threading.Lock()
        self.deadlines = {}  # id(socket) -> (socket, monotonic deadline)
        self.thread = None

    def watch(self, sock):
        """Start the header deadline for a new connection. A per-read timeout does not stop a head that drips."""
        with self.lock:
            self.deadlines[id(sock)] = (sock, time.monotonic() + self.head)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='dev-preview-guard', daemon=True)
                self.thread.start()

    def unwatch(self, sock):
        with self.lock:
            self.deadlines.pop(id(sock), None)

    def _run(self):
        while True:
            time.sleep(min(0.25, self.head / 4))
            now = time.monotonic()
            with self.lock:
                late = [key for key, (_, deadline) in self.deadlines.items() if deadline <= now]
                socks = [self.deadlines.pop(key)[0] for key in late]
            for sock in socks:
                try:
                    sock.shutdown(socket.SHUT_RDWR)  # the handler's read ends; its thread closes the socket
                except OSError:
                    pass


class Reader:
    """Reads a response body from the dev server, after the bytes that came with the head."""

    def __init__(self, sock, data):
        self.sock, self.data = sock, data

    def some(self, limit=65536):
        if not self.data:
            self.data = self.sock.recv(65536)
            if not self.data:
                raise EOFError
        piece, self.data = self.data[:limit], self.data[limit:]
        return piece

    def line(self, limit=1024):
        while b'\r\n' not in self.data:
            if len(self.data) > limit:
                raise ValueError('long line')
            more = self.sock.recv(65536)
            if not more:
                raise EOFError
            self.data += more
        line, self.data = self.data.split(b'\r\n', 1)
        if len(line) > limit:
            raise ValueError('long line')
        return line


class HeadRecorder:
    """The client's stream, keeping the raw lines of the request head. The standard parser splits a line at a bare CR
    and joins folded lines; the proxy checks the lines as they came (raw_head_ok), so it never forwards a head that
    it read in a more lenient way than the dev server would."""

    def __init__(self, stream):
        self.stream, self.lines, self.recording = stream, [], True

    def readline(self, *args):
        line = self.stream.readline(*args)
        if self.recording and len(self.lines) < 256:
            self.lines.append(line)
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


REQUEST_LINE = re.compile(rb'[A-Z]{1,16} [\x21-\x7e]{1,8192} HTTP/1\.[01]')


def raw_head_ok(lines):
    """True when each raw line of a request head is one well-formed line: CRLF at the end, no bare CR, no control
    character, no folded line, a valid field name."""
    if len(lines) < 2 or len(lines) >= 256 or lines[-1] != b'\r\n':
        return False
    if not all(line.endswith(b'\r\n') for line in lines) or not REQUEST_LINE.fullmatch(lines[0][:-2]):
        return False
    return all(FIELD_LINE.fullmatch(line[:-2]) for line in lines[1:-1])


class PoolServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # one page of a dev server asks for many modules at once
    previews = None
    guard = None
    main_port = 0

    def process_request(self, request, client_address):
        # A full house: wait a short time for a slot (a page load comes in bursts), then close the new connection.
        if not self.guard.connections.acquire(timeout=self.guard.accept):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.guard.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.guard.unwatch(request)
            self.guard.connections.release()


class PoolHandler(BaseHTTPRequestHandler):
    """One pool port. It serves the sign-in link and the forwarded dev server, and nothing of Tether."""
    protocol_version = 'HTTP/1.1'  # so the base class answers Expect: 100-continue
    timeout = 60

    def setup(self):
        super().setup()
        self.rfile = HeadRecorder(self.rfile)
        self.server.guard.watch(self.connection)

    def log_message(self, *_args):
        pass

    def version_string(self):
        return 'Tether'

    def answer(self, status, body, headers=()):
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src "
                             "'unsafe-inline'; frame-ancestors " + frame_ancestors(self.headers.get('Host', ''), self.server.main_port))
            self.send_header('Connection', 'close')
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except OSError:
            pass

    def expired(self):
        self.answer(401, page('This preview has ended', 'Open it again from Tether.', state='expired'))

    def public_origin(self):
        host = self.headers.get('Host', '')
        if not HOST_VALUE.fullmatch(host):
            return None
        return ('https://' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http://') + host

    def sign_in(self):
        """GET OPEN_PATH?token=...&next=/path: trade a one-time token for this port's cookie, then go to the page."""
        previews, port = self.server.previews, self.server.server_port
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        cookies = self.headers.get_all('Cookie')
        lease = previews.redeem(port, query.get('token', [''])[0]) if self.command == 'GET' else None
        # A used link still opens the page for a browser that holds the cookie (Reload, a second tap).
        lease = lease or previews.authorize(port, cookies)
        if lease is None:
            self.expired()
            return
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        cookie = '%s%d=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s' % (COOKIE_PREFIX, port, lease.secret, IDLE, secure)
        # This browser last saw another dev server on this origin (or none that Tether knows of): the storage and
        # cache there belong to that server. Clear them before the first page of this one loads.
        name, mark = '%s%d%s' % (COOKIE_PREFIX, port, MARKER_SUFFIX), marker(port, lease.dev_port)
        fresh = not any(secrets.compare_digest(value.encode('utf-8', 'replace'), mark.encode()) for value in cookie_values(cookies, name))
        self.close_connection = True
        try:
            self.send_response(302)
            self.send_header('Location', safe_path(query.get('next', ['/'])[0]))
            self.send_header('Set-Cookie', cookie)
            if fresh:
                self.send_header('Set-Cookie', '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s' % (name, mark, MARKER_LIFE, secure))
                self.send_header('Clear-Site-Data', CLEAR)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
        except OSError:
            pass

    def relay(self):
        self.close_connection = True
        previews, guard, port = self.server.previews, self.server.guard, self.server.server_port
        guard.unwatch(self.connection)  # the whole head is here
        self.rfile.recording = False
        if not re.fullmatch(r'/[\x21-\x7e]*', self.path) or not raw_head_ok(self.rfile.lines):
            self.answer(400, page('Bad request', 'Tether cannot forward this request: its path or its headers are malformed.'))
            return
        if self.path.split('?', 1)[0] == OPEN_PATH:
            self.sign_in()
            return
        lease = previews.authorize(port, self.headers.get_all('Cookie'))
        public = self.public_origin()
        if lease is None or public is None:
            self.expired()
            return
        upgrading = 'upgrade' in self.headers.get('Connection', '').lower() and bool(self.headers.get('Upgrade'))
        if not allowed_source(self.command, upgrading, self.headers, public):
            self.answer(403, page('Tether refused this request', 'It comes from another page. Only the previewed page '
                                  'may send data to its dev server.'))
            return
        try:
            framing = request_framing(self.headers, self.request_version)
            if upgrading and framing[0] != 'none':
                raise ValueError('upgrade with a body')
        except ValueError:
            self.answer(400, page('Bad request', 'Tether cannot forward this request: its headers are malformed or '
                                  'give the body length in two ways.'))
            return
        try:
            address = previews.target(lease.dev_port)
        except Refused as exc:
            previews.mark(lease, False)
            if exc.code == 'not-listening':
                self.answer(502, page('The dev server is not running', 'Nothing listens on port %d. This page loads '
                                      'again when the dev server is back.' % lease.dev_port, state='down', refresh=3))
            else:
                self.answer(502, page('Tether cannot forward this port', str(exc), state='down'))
            return
        if upgrading and not guard.sockets.acquire(blocking=False):
            self.answer(503, page('Too many live connections', 'Close a preview that you do not use, then reload.'))
            return
        try:
            upstream = socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM)
            with upstream:
                upstream.settimeout(5)
                try:
                    upstream.connect((address, lease.dev_port))
                except OSError:
                    previews.mark(lease, False)
                    self.answer(502, page('The dev server did not answer', 'Port %d refused the connection. This page '
                                          'loads again in a moment.' % lease.dev_port, state='down', refresh=3))
                    return
                # The listener was checked a moment ago (and that answer may be cached). This connection is checked now.
                if not previews.verify(upstream, lease.dev_port, address):
                    previews.mark(lease, False)
                    self.answer(502, page('Tether cannot forward this port', 'Port %d now belongs to another program or '
                                          'user. Start the dev server again.' % lease.dev_port, state='down'))
                    return
                previews.mark(lease, True)
                self.forward(upstream, lease, public, upgrading, framing)
        finally:
            if upgrading:
                guard.sockets.release()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = relay

    def request_head(self, lease, public, upgrading, framing):
        """The request as the dev server would get it on the machine. Built from the parsed headers, with the body
        length stated once, so the dev server reads the same message that the proxy checked."""
        local = 'http://localhost:%d' % lease.dev_port
        lines = ['%s %s HTTP/1.1' % (self.command, self.path), 'Host: localhost:%d' % lease.dev_port]
        for name, value in self.headers.items():
            low = name.lower()
            if low in ('origin', 'referer'):
                lines.append('%s: %s' % (name, to_local(value, public, local)))
            elif low not in HOP_HEADERS and not low.startswith(('x-forwarded-', 'tailscale-')):
                lines.append('%s: %s' % (name, value))
        cookies = dev_cookies(self.headers.get_all('Cookie'))
        if cookies:
            lines.append('Cookie: ' + cookies)
        if framing[0] == 'chunked':
            lines.append('Transfer-Encoding: chunked')
        elif self.headers.get('Content-Length') is not None:
            lines.append('Content-Length: %d' % framing[1])
        if upgrading:
            lines += ['Upgrade: ' + self.headers.get('Upgrade'), 'Connection: Upgrade']
        else:
            lines.append('Connection: close')
        return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1')

    def response_head(self, head, code, lease, public, framing):
        """The dev server's response head for the public origin: its own links and cookies fitted to that origin, the
        body length stated once, and nothing that acts on Tether's cookies or picks who frames the page."""
        upgraded = code == 101
        lines = head.decode('latin-1').split('\r\n')
        kept = [lines[0]]
        for line in lines[1:]:
            name, _, value = line.partition(':')
            low, value = name.lower(), value.strip()
            if low in ('clear-site-data', 'x-frame-options', 'set-cookie2', 'content-length', 'transfer-encoding', 'trailer') \
                    or (not upgraded and low in ('connection', 'keep-alive')):
                continue  # Clear-Site-Data "cookies" clears every port of the host, Tether's session too
            if low == 'set-cookie':
                value = set_cookie(value)
                if value is None:
                    continue
            elif low in ('location', 'content-location', 'access-control-allow-origin'):
                value = to_public(value, public, lease.dev_port)
            kept.append('%s: %s' % (name, value))
        if framing[0] == 'chunked' and self.request_version == 'HTTP/1.1':
            kept.append('Transfer-Encoding: chunked')
        elif framing[1] is not None:
            kept.append('Content-Length: %d' % framing[1])
        if not upgraded:
            kept.append('Connection: close')
            kept.append('Content-Security-Policy: frame-ancestors ' + frame_ancestors(self.headers.get('Host', ''), self.server.main_port))
        # Next.js answers 403 to a dev request (/_next/) from an origin it does not allow. The drawer then shows the setting.
        if code == 403 and '/_next/' in self.path:
            lease.hint = 'next'
        return ('\r\n'.join(kept) + '\r\n\r\n').encode('latin-1')

    def send_body(self, upstream, framing):
        """Stream the request body to the dev server. The headers may have left body bytes in rfile, so read there.
        A chunked body is read chunk by chunk and written again in the proxy's own framing, without extensions or trailers."""
        if framing[0] == 'length':
            self._copy(upstream, framing[1])
        elif framing[0] == 'chunked':
            while True:
                line = self.rfile.readline(1026)
                match = CHUNK_SIZE.fullmatch(line[:-2]) if line.endswith(b'\r\n') else None
                if match is None:
                    raise ValueError('chunk size')
                size = int(match.group(1), 16)
                if size == 0:
                    for _ in range(64):  # trailers end with a blank line; they are not forwarded
                        line = self.rfile.readline(8194)
                        if line == b'\r\n':
                            upstream.sendall(b'0\r\n\r\n')
                            return
                        if not line.endswith(b'\r\n') or not FIELD_LINE.fullmatch(line[:-2]):
                            break
                    raise ValueError('trailer')
                upstream.sendall(b'%x\r\n' % size)
                self._copy(upstream, size)
                if self.rfile.read(2) != b'\r\n':
                    raise ValueError('chunk end')
                upstream.sendall(b'\r\n')

    def _copy(self, upstream, left):
        while left:
            data = self.rfile.read(min(left, 65536))
            if not data:
                raise ValueError('short body')
            upstream.sendall(data)
            left -= len(data)

    def send_response_body(self, reader, client, framing):
        """Stream the answer's body. Chunks are read strictly and written again as the proxy's own chunks (or plain,
        for an HTTP/1.0 client), piece by piece as they arrive, so nothing waits for the whole body."""
        kind, left = framing
        if kind == 'length':
            while left:
                piece = reader.some(left)
                left -= len(piece)
                client.sendall(piece)
        elif kind == 'close':
            while True:
                client.sendall(reader.some())
        elif kind == 'chunked':
            rechunk = self.request_version == 'HTTP/1.1'
            while True:
                match = CHUNK_SIZE.fullmatch(reader.line())
                if match is None:
                    raise ValueError('chunk size')
                left = int(match.group(1), 16)
                if left == 0:
                    for _ in range(64):
                        line = reader.line(8192)
                        if line == b'':
                            if rechunk:
                                client.sendall(b'0\r\n\r\n')
                            return
                        if not FIELD_LINE.fullmatch(line):
                            break
                    raise ValueError('trailer')
                while left:
                    piece = reader.some(left)
                    left -= len(piece)
                    client.sendall(b'%x\r\n%s\r\n' % (len(piece), piece) if rechunk else piece)
                if reader.line(0) != b'':
                    raise ValueError('chunk end')

    def forward(self, upstream, lease, public, upgrading, framing):
        guard, previews, client = self.server.guard, self.server.previews, self.connection
        try:
            upstream.settimeout(guard.write)
            upstream.sendall(self.request_head(lease, public, upgrading, framing))
            self.send_body(upstream, framing)
        except (ValueError, OSError):
            return
        # Interim answers (100, 103) are dropped. Only a 101 switch keeps the client's side open. The head has one
        # deadline from now, however slowly it comes.
        data, deadline = b'', time.monotonic() + guard.upstream_head
        try:
            while True:
                while b'\r\n\r\n' not in data:
                    left = deadline - time.monotonic()
                    if left <= 0 or len(data) > HEAD_LIMIT:
                        raise ValueError('no head')
                    upstream.settimeout(left)
                    chunk = upstream.recv(65536)
                    if not chunk:
                        raise ValueError('no head')
                    data += chunk
                head, data = data.split(b'\r\n\r\n', 1)
                code = response_code(head) if len(head) <= HEAD_LIMIT else None
                if code is None:
                    raise ValueError('malformed head')
                if not (100 <= code < 200 and code != 101):
                    break
            answer = response_framing(head, code, self.command, upgrading)
            if answer is None:
                raise ValueError('ambiguous length')
        except (ValueError, OSError):
            self.answer(502, page('The dev server sent no usable answer', 'Port %d closed the connection, took too long, '
                                  'or sent a malformed answer.' % lease.dev_port, state='down'))
            return
        try:
            # Both sockets keep a timeout, so a peer that stops reading cannot block a write for ever.
            client.settimeout(guard.write)
            for sock in (client, upstream):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            client.sendall(self.response_head(head, code, lease, public, answer))
            if answer[0] != 'upgrade':
                upstream.settimeout(guard.idle)
                self.send_response_body(Reader(upstream, data), client, answer)
                return
            upstream.settimeout(guard.write)
            if data:
                client.sendall(data)
            pair = {client: upstream, upstream: client}
            while previews.alive(lease):  # the lease ended or its port went to another dev server: hang up
                ready, _, _ = select.select(list(pair), [], [], guard.lease_check)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    pair[source].sendall(data)
        except (ValueError, EOFError, OSError):
            return


def serve_commands(ports):
    """The deploy step that gives each pool port its own HTTPS origin. Tether never runs it."""
    return ['tailscale serve --bg --https=%d http://127.0.0.1:%d' % (port, port) for port in ports]


PREVIEWS = DevPreviews()


def start(host, main_port, denied=(), previews=None, ports=None, log=print, guard=None):
    """Listen on each pool port. A port that is taken leaves the pool. Returns the servers (tests close them)."""
    previews = previews or PREVIEWS
    guard = guard or Guard()
    try:
        wanted = pool_ports() if ports is None else list(ports)
    except ValueError as exc:
        log('Dev server previews are off: %s' % exc)
        wanted = []
    servers = []
    for port in wanted:
        try:
            server = PoolServer((host, port), PoolHandler)
        except OSError:
            continue
        server.previews, server.main_port, server.guard = previews, main_port, guard
        threading.Thread(target=server.serve_forever, name='dev-preview-%d' % server.server_port, daemon=True).start()
        servers.append(server)
    bound = [server.server_port for server in servers]
    with previews.lock:
        previews.ports = bound
        previews.denied = set(DENIED_PORTS) | set(denied) | set(bound) | {main_port}
    if bound:
        log('Dev server previews listen on %s:%d-%d. Behind tailscale serve, publish each port, for example: %s'
            % (host, bound[0], bound[-1], serve_commands(bound[:1])[0]))
    return servers
