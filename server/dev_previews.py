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
- A pool port belongs to one dev port until the person releases it (the list is a file in Tether's config folder,
  so it holds after a lease ends and after a restart). Tether never moves a pool port to another dev server by
  itself: a page of the old dev server that is still open in a browser would act with the new server's cookie,
  and clearing storage does not close that page. A full pool is an error that names the assigned dev ports; the
  person releases one from the drawer (POST /mobile/api/dev-preview/release) after they close its tabs. As a
  second line, the first sign-in of a browser on a pool port that last showed it another dev server clears that
  origin's storage and cache (Clear-Site-Data), and the connections of a released lease end.
- No service worker can exist on a preview origin. A worker outlives its pages and its dev server: registered by
  one app, it would intercept the sign-in link of the next app on a released port, before Clear-Site-Data runs.
  A browser sends "Service-Worker: script" with every fetch of a worker script (install and update); the proxy
  refuses those requests and removes Service-Worker-Allowed from answers.
- After the proxy connects, it reads the owner of that very connection from the socket table and sends nothing when
  it is not Tether's user, or when the owner cannot be read: another user cannot take a port that a dev server
  left. The socket table is Linux's /proc/net/tcp; on another system Tether does not forward dev servers.
- The proxy rebuilds the request and response framing (one Content-Length or chunked, never both) and refuses
  malformed heads, so no request can mean one thing to the proxy and another to the dev server (RFC 9112, 6.3).
- Connections, WebSockets, header time and write time are limited (Guard), so stalled clients cannot hold workers.
- Only Tether's main origin (and the dev page itself) may frame the page.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import auth
import paths
import prefs_sync

DEFAULT_PORTS = '7700-7709'
MAX_POOL = 32
IDLE = 12 * 3600          # a lease ends after this long without a request
GONE = 10 * 60            # ... or this long after its dev server stopped listening (a restart keeps the lease)
STATE_FILE = 'dev-preview-ports.json'  # which dev port each pool port belongs to, until the person releases it
TICKET_LIFE = 120         # the link for the frame; the "Open in browser" link lives as long as the lease
MAX_TICKETS = 20
SCAN_TTL = 2.0            # how long one read of the listener table serves the proxy
ACCEPT_LOOK = 3.0         # how long the proxy waits for a dev server to accept a connection before it checks the owner
STOP_WAIT = 3.0           # how long a stop request waits for the dev servers to end
MAX_STOP = 64             # ports in one stop request
OPEN_PATH = '/__tether_preview__/open'
COOKIE_PREFIX = 'tether_preview_'
# Never forwarded, whoever owns them: remote shells, debuggers (they run code), databases and caches.
DENIED_PORTS = frozenset({22, 2375, 2376, 3306, 5432, 6379, 9200, 9222, 9229, 11211, 27017})
MARKER_SUFFIX = '_app'    # tether_preview_<pool>_app: which dev server this browser last saw on the pool port
MARKER_LIFE = 365 * 86400
CLEAR = '"cache", "storage", "executionContexts"'  # never "cookies": cookies span all ports, Tether's session too
HEAD_LIMIT = 65536        # bytes of a request head or an answer head
HEAD_WAIT = 300           # a dev server may compile for a while before it answers; an absolute limit for its head
IDLE_STREAM = 900         # a plain answer that sends nothing for this long is cut; WebSockets have no idle limit
MAX_CONNECTIONS = 256     # open connections on all pool ports together
MAX_SOCKETS = 64          # WebSockets (and other upgrades) among them
ACCEPT_WAIT = 5           # how long a new connection waits for a free slot before it is closed
HEAD_DEADLINE = 20        # the whole request head must arrive in this time, however slowly it drips
WRITE_TIMEOUT = 60        # a peer that takes no bytes for this long loses the connection
LINGER = 2                # after Tether's own answer, how long it reads and drops what the client still sends
LINGER_IDLE = 0.5         # ... and how long it waits for more bytes before it closes (like nginx lingering_timeout)
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


UNSUPPORTED = ('Dev server previews need a Linux server: on this system Tether cannot check who owns a dev '
               "server's connection, so it does not forward it.")


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


def read_listeners():
    """Every listening TCP socket, of all users, from the kernel's socket table (Linux)."""
    if not sys.platform.startswith('linux'):
        raise Refused('unsupported', UNSUPPORTED)
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
    found = connection_owner(sock, tries, pause, root)
    return found[0] if found is not None else None


def connection_inode(sock, wait=ACCEPT_LOOK, root='/proc/net'):
    """The inode of the dev server's end of a connected loopback socket once the dev server accepted it, else 0.
    A busy dev server may accept late: look for up to `wait` seconds."""
    found = connection_owner(sock, max(1, int(wait / 0.05)), 0.05, root)
    return found[1] if found is not None else 0


def connection_owner(sock, tries=4, pause=0.02, root='/proc/net'):
    """(uid, inode) of the other end of a connected loopback socket, or None. The inode is 0 until it is accepted."""
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
    return found


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


AGENTS = frozenset({'claude', 'codex'})  # agent CLIs: their sockets are not dev servers, and Tether never stops them


def _read(path):
    with open(path, 'rb') as stream:
        return stream.read(65536)


def cgroup_kind(text):
    """'service' when a cgroup record puts the process in a systemd service (or below one), None when it does not,
    'unknown' when the records cannot be read. cgroup v2 has one line, '0::/path'; v1 and hybrid have several, and each
    is checked. Below a user manager (user@1000.service) the units are that user's own: a program started in a terminal
    or tmux sits in a .scope there, Tether's helpers in their .service."""
    paths = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(':', 2)
        if len(parts) != 3 or not parts[2].startswith('/'):
            return 'unknown'
        paths.append(parts[2])
    if not paths:
        return 'unknown'
    for path in paths:
        units = [unit for unit in path.split('/') if unit]
        managers = [index for index, unit in enumerate(units) if unit.startswith('user@') and unit.endswith('.service')]
        if managers:
            units = units[managers[-1] + 1:]
        if any(unit.endswith('.service') for unit in units):
            return 'service'
    return None


def _process_command(base):
    command = _read(base + '/cmdline').split(b'\0')
    comm = _read(base + '/comm').decode('utf-8', 'replace').strip()
    names = {comm} | {os.path.basename(part.decode('utf-8', 'replace')) for part in command[:2] if part}
    return comm, bool(names & AGENTS)


def process_kind(pid, root='/proc', own_pid=None):
    """What the process `pid` is, for the dev server list (Linux): 'dev' for a program the person or an agent
    started, else 'tether' (Tether or a program it started), 'service' (a systemd service, such as Tether's voice
    and model helpers), 'agent' (Claude Code, Codex, or a helper that an agent started itself) or 'unknown' (Tether
    cannot tell). Only 'dev' is listed, forwarded or stopped: the other kinds fail closed.
    Below an agent, a dev server and an MCP server both descend from it (B192). Tether checks the direct child of
    the nearest agent ancestor: a tool run has CLAUDE_PID equal to that agent (Claude Code's shell) or
    CODEX_TOOL_CALL_ID (Codex), or, with no marker, is a shell. Anything else, such as an MCP server or the browser
    it starts, is the agent's own helper. Tether trusts the processes of its own user not to disguise themselves:
    they could signal any of this user's processes without Tether. The rule guards against mistakes, such as
    stopping Tether's own helpers or an agent's tools."""
    own_pid = os.getpid() if own_pid is None else own_pid
    base = '%s/%d' % (root, pid)
    parent_line = re.compile(r'^PPid:[ \t]*([0-9]+)[ \t]*$', re.M)
    try:
        status = _read(base + '/status').decode('utf-8', 'replace')
        parent = int(parent_line.search(status).group(1))
        group = cgroup_kind(_read(base + '/cgroup').decode('utf-8', 'replace'))
        comm, is_agent = _process_command(base)
        if own_pid and own_pid in (pid, parent):
            return 'tether'
        if group:
            return group
        if is_agent:
            return 'agent'

        child, child_comm = pid, comm
        for _ in range(128):
            if parent <= 1:
                return 'dev'
            base = '%s/%d' % (root, parent)
            comm, is_agent = _process_command(base)
            if is_agent:
                break
            child, child_comm = parent, comm
            status = _read(base + '/status').decode('utf-8', 'replace')
            parent = int(parent_line.search(status).group(1))
        else:
            return 'dev' if parent <= 1 else 'unknown'
    except (OSError, AttributeError, ValueError):
        return 'unknown'

    try:
        with open('%s/%d/environ' % (root, child), 'rb') as stream:
            environ = dict(entry.split(b'=', 1) for entry in stream.read().split(b'\0') if b'=' in entry)
    except OSError:
        environ = {}
    if b'CLAUDE_PID' in environ:
        return 'dev' if environ[b'CLAUDE_PID'] == str(parent).encode() else 'agent'
    if environ.get(b'CODEX_TOOL_CALL_ID') or child_comm in {'sh', 'bash', 'zsh', 'dash', 'fish', 'ksh'}:
        return 'dev'
    return 'agent'


def socket_holders(inodes, uid, root='/proc', limit=50000):
    """({inode: [pid, ...]}, complete): this user's processes that hold each socket (Linux). A socket may have several
    holders, for example a reloader and its worker. complete is False when the scan stopped early or could not read a
    process of this user: a holder may be missing then, so the caller must not trust the result.
    A non-dumpable process of this user (sshd, gpg-agent, sd-pam: always there) hides its descriptors. Counting each
    as a gap would hide every dev server, so it counts only when it may have inherited the socket: it descends from
    a holder, or it is in the session of a holder (workers that a supervisor forked, then PR_SET_DUMPABLE 0). Such
    a socket is left out, as if no holder were visible, and every caller refuses a socket without a visible holder.
    So a dev server leaves the list while ssh (non-dumpable) runs in its terminal session. Not covered: a process
    that left the session (setsid) after it inherited the socket, and a descriptor sent over a Unix socket to an
    unrelated non-dumpable process (SCM_RIGHTS). Same-uid programs can do that on purpose only; this rule guards
    against mistakes, not against this user's own programs (see process_kind)."""
    wanted = {'socket:[%d]' % inode: inode for inode in inodes if inode}
    found = {}
    if not wanted:
        return found, True
    try:
        pids = [int(name) for name in os.listdir(root) if name.isdigit()]
    except OSError:
        return found, False
    looked, hidden = 0, []
    for pid in pids:
        base = '%s/%d' % (root, pid)
        try:
            if os.stat(base).st_uid != uid:
                continue
            links = os.listdir(base + '/fd')
        except FileNotFoundError:
            continue  # it ended
        except PermissionError:
            hidden.append(pid)  # non-dumpable: see the docstring
            continue
        except OSError:
            return found, False
        for link in links:
            looked += 1
            if looked > limit:
                return found, False
            try:
                inode = wanted.get(os.readlink('%s/fd/%s' % (base, link)))
            except OSError:
                continue  # the descriptor closed
            if inode is not None and pid not in found.setdefault(inode, []):
                found[inode].append(pid)
    sessions = {}
    for inode, holders in found.items():
        for holder in holders:
            sessions.setdefault(_stat(holder, root)[1], set()).add(inode)
    sessions.pop(None, None)
    for pid in hidden:
        session = _stat(pid, root)[1]
        ancestors = _ancestors(pid, root)
        for inode in [inode for inode, holders in found.items()
                      if ancestors.intersection(holders) or inode in sessions.get(session, ())]:
            del found[inode]  # it may hold the socket too
    return found, True


def _stat(pid, root='/proc'):
    """(parent pid, session id) from /proc/PID/stat, readable for non-dumpable processes too; (None, None) if gone."""
    try:
        text = _read('%s/%d/stat' % (root, pid))
        fields = text[text.rindex(b')') + 1:].split()
        return int(fields[1]), int(fields[3])
    except (OSError, ValueError, IndexError):
        return None, None


def _ancestors(pid, root='/proc', depth=128):
    """The pids above `pid` up to init."""
    seen = set()
    while pid and pid > 1 and len(seen) < depth:
        pid = _stat(pid, root)[0]
        if pid is not None:
            seen.add(pid)
    return seen


def holds(pid, inode, root='/proc'):
    """True when the process `pid` holds the socket `inode` now."""
    want = 'socket:[%d]' % inode
    try:
        for link in os.listdir('%s/%d/fd' % (root, pid)):
            try:
                if os.readlink('%s/%d/fd/%s' % (root, pid, link)) == want:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def process_names(inodes, uid, root='/proc', own_pid=None, *, kind=None):
    """{inode: {'name', 'folder', 'pids', 'kind'}} for this user's listening sockets (Linux): the command, its folder's
    name, the processes that hold the socket and their kind (process_kind). The kind is 'dev' only when every holder
    is a dev server and the scan saw every process. A socket without a visible holder is left out."""
    classify = kind if kind is not None else lambda pid: process_kind(pid, root, own_pid)
    holders, complete = socket_holders(inodes, uid, root)
    found = {}
    for inode, pids in holders.items():
        kinds = {classify(pid) for pid in pids}
        socket_kind = kinds.pop() if len(kinds) == 1 and complete else 'unknown'
        entry = {'name': '', 'folder': '', 'pids': sorted(pids), 'kind': socket_kind}
        base = '%s/%d' % (root, min(pids))
        try:
            command = _read(base + '/cmdline').split(b'\0')[0].decode('utf-8', 'replace')
            entry['name'] = (os.path.basename(command) or _read(base + '/comm').decode('utf-8', 'replace').strip())[:40]
            entry['folder'] = os.path.basename(os.readlink(base + '/cwd'))[:60]
        except OSError:
            pass
        found[inode] = entry
    return found


def stop_process(pid, inode, force=False, kind=process_kind, root='/proc'):
    """Signal `pid` with SIGTERM (or SIGKILL): 'sent', 'gone' (it no longer holds the socket `inode`) or 'refused' (it
    is no longer a dev server). The pidfd pins this very process first; then Tether checks that it still holds the
    socket and is still a dev server, and only then signals it. A pid that went to another program before the pin
    fails the socket check: socket inodes are not shared between processes that did not pass them on (Linux 5.3 or
    later; elsewhere Tether stops nothing)."""
    import signal as signals
    if not hasattr(os, 'pidfd_open') or not hasattr(signals, 'pidfd_send_signal'):
        raise OSError('pidfd is not available')
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return 'gone'
    try:
        if not holds(pid, inode, root):
            return 'gone'
        if kind(pid) != 'dev':
            return 'refused'
        try:
            signals.pidfd_send_signal(descriptor, signals.SIGKILL if force else signals.SIGTERM)
        except ProcessLookupError:
            return 'gone'
        return 'sent'
    finally:
        os.close(descriptor)


# leases ------------------------------------------------------------------------------------------------------------
def server_id(entries):
    """The id of the program behind a port: the inodes of its listening sockets. A restart makes new sockets."""
    return ','.join(sorted(str(entry['inode']) for entry in entries))


class Lease:
    def __init__(self, pool_port, dev_port, now):
        self.pool_port = pool_port
        self.dev_port = dev_port
        self.secret = secrets.token_urlsafe(32)
        self.used = now
        self.gone = None      # when the dev server was first seen not listening
        self.tickets = {}     # one-time token -> the time it ends
        self.hint = None      # 'next': Next.js refused a dev request
        self.worker = False   # the page tried to register a service worker, which previews refuse


class DevPreviews:
    """The pool: which dev port each pool port forwards, and who may use it. Thread-safe."""

    def __init__(self, ports=(), denied=(), clock=time.time, listeners=read_listeners, uid=None, names=process_names,
                 peer_uid=connection_uid if sys.platform.startswith('linux') else None, store=None, signal=None, kind=None,
                 peer_inode=connection_inode):
        self.peer_uid = peer_uid  # None: no socket table on this system. Nothing is forwarded then (verify, open).
        self.store = store        # the file that keeps the assignments; None keeps them in memory (tests)
        self.assigned = None      # pool port -> dev port, read from the file at first use
        self.ports = list(ports)
        self.denied = set(DENIED_PORTS) | set(denied) | set(self.ports)
        self.clock = clock
        self.read = listeners
        self.uid = os.getuid() if uid is None else uid
        self.names = names        # (inodes, uid) -> {inode: {'name', 'folder', 'pids', 'kind'}}: process_names
        self.peer_inode = peer_inode  # sock -> the dev server's end of this connection, once accepted (0: not yet)
        self.kind = kind or process_kind  # pid -> 'dev', 'tether', 'service', 'agent' or 'unknown'
        # (pid, inode, force) -> 'sent', 'gone' or 'refused': stop_process
        self.signal = signal or (lambda pid, inode, force: stop_process(pid, inode, force, kind=self.kind))
        self.stopping = threading.Lock()  # one stop request at a time: each one scans /proc and waits
        self.owners = {}          # inode -> (monotonic time, process_names entry): the proxy asks often
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

    def owner(self, inodes, fresh=False):
        """{inode: process_names entry} for these sockets, from a short cache unless `fresh`."""
        now = time.monotonic()
        with self.lock:
            known = {inode: self.owners[inode][1] for inode in inodes
                     if not fresh and inode in self.owners and now - self.owners[inode][0] <= SCAN_TTL}
        missing = [inode for inode in inodes if inode not in known]
        if missing:
            try:
                found = self.names(missing, self.uid)
            except OSError:
                found = {}
            with self.lock:
                if len(self.owners) > 4096:
                    self.owners.clear()
                for inode in missing:
                    entry = found.get(inode, {'name': '', 'folder': '', 'pids': [], 'kind': 'unknown'})
                    self.owners[inode] = (now, entry)
                    known[inode] = entry
        return known

    def ours(self, port, fresh=False, listening=None):
        """This user's listeners on `port` that a localhost link reaches, and whether another user's listener or a
        listener on another address shares the port."""
        mine, other = [], False
        for entry in self.listeners(fresh) if listening is None else listening:
            if entry['port'] != port:
                continue
            if entry['uid'] != self.uid or connect_address(entry['address']) is None:
                other = True
            else:
                mine.append(entry)
        return mine, other

    def target(self, port, fresh=False):
        """The address the proxy connects to for dev port `port`. Refused when the port may not be forwarded now:
        only a dev server is forwarded, never Tether, its services or an agent CLI (process_kind)."""
        self.check_port(port)
        mine, other = self.ours(port, fresh)
        if mine:
            owners = self.owner([entry['inode'] for entry in mine], fresh)
            if any(owners[entry['inode']].get('kind') != 'dev' for entry in mine):
                raise Refused('denied', 'Port %d is not a dev server: Tether, its services and agents are never '
                                        'forwarded.' % port)
        best = None
        for entry in mine:
            address = connect_address(entry['address'])
            rank = (entry['address'].is_unspecified, ':' in address)
            if best is None or rank < best[0]:
                best = (rank, address)
        if best is not None:
            return best[1]
        if other:
            raise Refused('denied', 'Port %d belongs to another user or does not listen on localhost.' % port)
        raise Refused('not-listening', 'Nothing listens on port %d. Start the dev server, then try again.' % port)

    def verify(self, sock):
        """'ours' when the connected socket `sock` reaches a dev server of Tether's own user, 'other' when it does
        not, 'unknown' when the owner of this very connection cannot be read. Called after connect and before the
        first byte: between the listener check and connect, another user may have taken the port. Only 'ours'
        is forwarded; a second look at the listeners would not name this connection."""
        if self.peer_uid is None:
            return 'unknown'
        try:
            owner = self.peer_uid(sock)
        except (OSError, ValueError):
            return 'unknown'
        if owner is None:
            return 'unknown'
        if owner != self.uid:
            return 'other'
        try:
            inode = self.peer_inode(sock)
        except (OSError, ValueError):
            return 'unknown'
        if not inode:
            return 'unknown'  # never accepted: Tether cannot see which process would read the bytes
        return 'ours' if self.connected_dev(inode) else 'other'

    def connected_dev(self, inode):
        """True when the processes that hold the dev server's end of a connection (socket `inode`) are dev servers.
        This names the very connection: a listener that closed and a new one on the port cannot stand in for it.
        A socket without a visible holder, or with a hidden one (see socket_holders), or an incomplete scan, is refused."""
        holders, complete = socket_holders([inode], self.uid)  # every holder, so no cached one stands in (~35 ms)
        live = holders.get(inode, []) if complete else []
        return bool(live) and all(self.kind(pid) == 'dev' for pid in live)

    # assignments -----------------------------------------------------------------------------------------------
    def _assignments(self):
        """{pool port: dev port}, read once. Call with the lock held. Refused when the file is there but unreadable:
        without the list, Tether cannot know which dev server an origin belongs to, so it assigns nothing."""
        if self.assigned is None:
            found = {}
            if self.store is not None:
                try:
                    data = json.loads(self.store.read_text())['ports']
                    found = {int(pool): dev for pool, dev in data.items()}
                    if not all(type(dev) is int and 1024 < dev <= 65535 and 1024 < pool <= 65535 for pool, dev in found.items()) \
                            or len(set(found.values())) != len(found):
                        raise ValueError('ports')
                except FileNotFoundError:
                    found = {}
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    raise Refused('state', 'Tether cannot read its list of preview ports (%s in its config folder). '
                                           'Repair or remove that file.' % self.store.name) from None
            self.assigned = found
        return self.assigned

    def _save(self, assigned):
        """Write the assignments, then use them. Call with the lock held. Nothing changes when the write fails."""
        if self.store is not None:
            try:
                self.store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                draft = self.store.with_name(self.store.name + '.new')
                descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump({'ports': {str(pool): dev for pool, dev in sorted(assigned.items())}}, stream)
                os.replace(draft, self.store)
            except OSError:
                raise Refused('state', 'Tether cannot save its list of preview ports. Check the disk and its config folder.') from None
        self.assigned = assigned

    def assignments(self):
        """For the page: {'assigned': [{'port', 'dev_port'}], 'free': how many pool ports have no dev port}."""
        with self.lock:
            assigned = self._assignments()
            rows = [{'port': port, 'dev_port': assigned[port]} for port in self.ports if port in assigned]
            return {'assigned': rows, 'free': len(self.ports) - len(rows)}

    def release(self, dev_port):
        """The person gives up the pool port of `dev_port`: its lease, cookie and open connections end, and the pool
        port is free for another dev server. Only a request the person makes does this (server.py)."""
        if type(dev_port) is not int:
            raise Refused('bad-port', 'That is not a port number.')
        with self.lock:
            assigned = self._assignments()
            pool = next((port for port, dev in assigned.items() if dev == dev_port), None)
            if pool is None:
                raise Refused('not-assigned', 'Port %d has no preview port.' % dev_port)
            self._save({port: dev for port, dev in assigned.items() if port != pool})
            self.leases.pop(pool, None)
        return pool

    def alive(self, lease):
        """False once the lease ended or the person released its pool port: its open connections must end."""
        with self.lock:
            return self.leases.get(lease.pool_port) is lease and self.clock() - lease.used <= IDLE

    def servers(self):
        """This user's dev servers that Tether can forward and stop: [{'port', 'id', 'name', 'folder'}], by port.
        Tether, its services, agent CLIs and listeners without a visible process are left out (process_kind). `id`
        names this program's sockets: a new program on the same port gets a new id (see stop, `expect`)."""
        ports = {}
        for entry in self.listeners(fresh=True):
            port = entry['port']
            if port <= 1024 or port in self.denied:
                continue
            ports.setdefault(port, []).append(entry)
        owners = self.owner([entry['inode'] for entries in ports.values() for entry in entries], fresh=True)
        found = []
        for port, entries in sorted(ports.items()):
            if any(entry['uid'] != self.uid or connect_address(entry['address']) is None
                   or owners[entry['inode']].get('kind') != 'dev' for entry in entries):
                continue
            first = owners[entries[0]['inode']]
            found.append({'port': port, 'id': server_id(entries), 'name': first.get('name', ''),
                          'folder': first.get('folder', '')})
        return found

    def stop(self, ports, expect, force=False, wait=STOP_WAIT):
        """The person stops dev servers: {port: 'stopped' | 'still-running' | 'refused' | 'not-found' | 'changed'}.
        `expect` maps each port to the id that the list showed (servers); every port needs one. When another program
        listens there now, the port answers 'changed' and nothing is signalled, so a stop never reaches a program
        the person did not see. Each process
        that holds the port's socket gets SIGTERM (SIGKILL when `force`), only when it is a dev server now
        (process_kind, checked again on the pinned process: stop_process). A stopped port also gives back its preview
        port. One request at a time; it scans /proc once for all ports and ends `wait` seconds after it starts."""
        if (type(ports) is not list or not 0 < len(ports) <= MAX_STOP
                or any(type(port) is not int or not 0 < port <= 65535 for port in ports)):
            raise Refused('bad-port', 'Name 1 to %d port numbers.' % MAX_STOP)
        if (type(expect) is not dict or set(expect) != set(ports)
                or any(type(value) is not str or len(value) > 4096 for value in expect.values())):
            raise Refused('bad-port', 'Name the id that the list showed for each port.')
        if self.peer_uid is None:
            raise Refused('unsupported', UNSUPPORTED)
        if not self.stopping.acquire(blocking=False):
            raise Refused('busy', 'Tether is stopping other dev servers. Try again in a moment.')
        try:
            deadline = time.monotonic() + wait
            listening = self.listeners(fresh=True)
            result, found = {}, {}
            for port in dict.fromkeys(ports):
                try:
                    self.check_port(port)
                except Refused:
                    result[port] = 'refused'
                    continue
                mine, other = self.ours(port, listening=listening)
                if other:
                    result[port] = 'refused'
                elif not mine:
                    result[port] = 'not-found'
                elif expect[port] != server_id(mine):
                    result[port] = 'changed'
                else:
                    found[port] = mine
            owners = self.owner([entry['inode'] for mine in found.values() for entry in mine], fresh=True)
            sent = {}
            for port, mine in found.items():
                if any(owners[entry['inode']].get('kind') != 'dev' or not owners[entry['inode']].get('pids')
                       for entry in mine):
                    result[port] = 'refused'
                    continue
                answers = set()
                try:
                    for entry in mine:
                        for pid in owners[entry['inode']]['pids']:
                            answers.add(self.signal(pid, entry['inode'], force))
                except OSError:
                    answers.add('refused')  # no pidfd here, or no permission: Tether stops nothing it cannot pin
                if 'refused' in answers:
                    result[port] = 'refused'
                    continue
                result[port] = 'still-running'
                sent[port] = {entry['inode'] for entry in mine}
            while sent:
                alive = {entry['inode'] for entry in self.listeners(fresh=True)}
                for port, inodes in sent.items():
                    if result[port] == 'still-running' and not inodes & alive:
                        result[port] = 'stopped'
                if all(result[port] == 'stopped' for port in sent) or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        finally:
            self.stopping.release()
        for port in sent:
            if result[port] == 'stopped':
                try:
                    self.release(port)
                except Refused:
                    pass  # no preview port, or the list cannot be saved: the server is stopped all the same
        return {port: result[port] for port in dict.fromkeys(ports)}  # in the order of the request

    # leases ----------------------------------------------------------------------------------------------------
    def _sweep(self, now):
        for port, lease in list(self.leases.items()):
            if now - lease.used > IDLE or (lease.gone is not None and now - lease.gone > GONE):
                del self.leases[port]

    def open(self, dev_port, path='/'):
        """Forward `dev_port` and return the links: {'port', 'dev_port', 'frame', 'browser'}. Refused with a reason."""
        if not self.ports:
            raise Refused('off', 'Dev server previews are turned off on this server.')
        if self.peer_uid is None:
            raise Refused('unsupported', UNSUPPORTED)
        self.target(dev_port, fresh=True)
        path = safe_path(path)
        now = self.clock()
        with self.lock:
            self._sweep(now)
            assigned = self._assignments()
            pool = next((port for port in self.ports if assigned.get(port) == dev_port), None)
            if pool is None:
                # The first free pool port. A pool port never moves to another dev port here: only release() frees it.
                pool = next((port for port in self.ports if port not in assigned), None)
                if pool is None:
                    used = ', '.join('localhost:%d' % assigned[port] for port in self.ports)
                    raise Refused('pool-full', 'All %d preview ports are in use (%s). Release one to preview port %d.'
                                  % (len(self.ports), used, dev_port))
                self._save({**assigned, pool: dev_port})
            lease = self.leases.get(pool)
            if lease is None or lease.dev_port != dev_port:
                lease = self.leases[pool] = Lease(pool, dev_port, now)
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
            return [{'port': lease.pool_port, 'dev_port': lease.dev_port, 'hint': lease.hint, 'worker': lease.worker}
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
    """Who may frame a forwarded page: the page itself, Tether's main origin on the same host, and the host menu's other
    Tether servers (one of them may show this Tether in a frame, and CSP checks every ancestor)."""
    name = urlsplit('//' + host).hostname or ''
    if not re.fullmatch(r'[a-z0-9.-]{1,253}', name):
        return "'self'"
    return ' '.join(["'self' https://%s http://%s:%d https://%s:%d" % (name, name, main_port, name, main_port)] + prefs_sync.host_origins())


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
        """Runs while a connection waits for its head; watch() starts it again for the next one."""
        while True:
            time.sleep(min(0.25, self.head / 4))
            now = time.monotonic()
            with self.lock:
                late = [key for key, (_, deadline) in self.deadlines.items() if deadline <= now]
                socks = [self.deadlines.pop(key)[0] for key in late]
                done = not self.deadlines
                if done:
                    self.thread = None
            for sock in socks:
                try:
                    sock.shutdown(socket.SHUT_RDWR)  # the handler's read ends; its thread closes the socket
                except OSError:
                    pass
            if done:
                return


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

    def __init__(self, stream, limit=HEAD_LIMIT):
        self.stream, self.lines, self.recording = stream, [], True
        self.left, self.over = limit, False

    def readline(self, size=65537):
        if not self.recording:
            return self.stream.readline(size)
        if self.over:
            return b'\r\n'
        # Never read more of the head than the limit allows: the limit holds while the head arrives, before any
        # parsing and before the sign-in check. Past it, the parser gets an end of head and relay() answers 431.
        line = self.stream.readline(min(size, self.left + 1))
        self.left -= len(line)
        if self.left < 0:
            self.over = True
            return b'\r\n'
        if len(self.lines) < 256:
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


def linger(sock, seconds=LINGER, idle=LINGER_IDLE):
    """A lingering close (RFC 9112, 9.6): end our side, then read and drop what the client still sends until it
    closes, sends nothing for `idle` seconds, or `seconds` pass. A close with unread input makes the kernel send a
    reset, and a reset can drop the answer before the client reads it."""
    try:
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(min(left, idle))
            if not sock.recv(65536):
                return
    except OSError:  # a timeout too
        pass


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

    linger = False

    def setup(self):
        super().setup()
        self.rfile = HeadRecorder(self.rfile)
        self.server.guard.watch(self.connection)

    def finish(self):
        super().finish()
        if self.linger:
            linger(self.connection)

    def log_message(self, *_args):
        pass

    def version_string(self):
        return 'Tether'

    def send_error(self, code, message=None, explain=None):
        # The base class's own answers to a head it cannot parse (400, 414, 431 for too many headers, 505).
        self.server.guard.unwatch(self.connection)  # the answer is on its way: the head deadline no longer applies
        self.linger = True
        super().send_error(code, message, explain)

    def answer(self, status, body, headers=()):
        self.close_connection = True
        self.linger = True  # the client may still be sending the head or a body that Tether did not read
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
        self.linger = True  # a body sent with the sign-in link is never read
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
        if self.rfile.over:
            self.answer(431, page('Request header too large', 'Tether forwards request headers of up to %d KB.' % (HEAD_LIMIT // 1024)))
            return
        if not re.fullmatch(r'/[\x21-\x7e]*', self.path) or not raw_head_ok(self.rfile.lines):
            self.answer(400, page('Bad request', 'Tether cannot forward this request: its path or its headers are malformed.'))
            return
        if self.headers.get('Service-Worker') is not None:
            # A fetch of a service worker script, for any path (the sign-in link too) and before a token is spent.
            lease = previews.authorize(port, self.headers.get_all('Cookie'))
            if lease is not None:
                lease.worker = True
            self.answer(403, page('Service workers are off in previews', 'A service worker would stay on this address '
                                  'after its dev server is gone. The page works without it.'))
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
                owner = previews.verify(upstream)
                if owner != 'ours':
                    previews.mark(lease, False)
                    self.answer(502, page('Tether cannot forward this port', 'Port %d now belongs to another program or '
                                          'user. Start the dev server again.' % lease.dev_port if owner == 'other' else
                                          'Tether cannot check who owns the connection to port %d, so it does not '
                                          'forward it.' % lease.dev_port, state='down'))
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
            if low in ('clear-site-data', 'x-frame-options', 'set-cookie2', 'content-length', 'transfer-encoding', 'trailer',
                       'service-worker-allowed') \
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


PREVIEWS = DevPreviews(store=paths.private_file(STATE_FILE))


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
