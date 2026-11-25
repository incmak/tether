"""Loopback-only CDP bridge. Each program owns contexts on Tether's existing Chromium pipe."""
import atexit
import base64
import errno
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import select
import signal
import socket
import ssl
import struct
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit

import browser
import paths
import procinfo

LOG = logging.getLogger(__name__)
# 29000-29999 is below Linux (32768+) and macOS (49152+) ephemeral ranges, so outgoing connections never take it.
PORT_BASE = 29000
MAX_MESSAGE = 32 * 1024 * 1024
MAX_QUEUED_BYTES = 2 * MAX_MESSAGE
MAX_CLIENTS = 8
TAB_LIMIT = 3
ENV_NAMES = ('PLAYWRIGHT_MCP_CDP_ENDPOINT', 'TETHER_CDP_URL')
TAB_ERROR = 'Tab limit (3) for this agent. Close a tab first.'
CONFIG = paths.CONFIG / 'agent-browser.json'


def _read_text(path):
    return procinfo.read_text(path)


def _pane_list(timeout):
    return subprocess.check_output([paths.TMUX, 'list-panes', '-a', '-F', '#{pane_id} #{pane_pid}'],
                                   text=True, timeout=timeout)


def _darwin_peer_pane(peer_port, server_port, pane_reader, clock, uid):
    deadline = clock() + 2
    uid = os.getuid() if uid is None else uid
    try:
        if clock() >= deadline:
            return None
        pid = procinfo.socket_owner(peer_port, server_port, uid)
        if not pid or clock() >= deadline:
            return None
        panes = {}
        for line in pane_reader(max(.01, deadline - clock())).splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r'%\d+', parts[0]):
                panes[int(parts[1])] = parts[0]
        visited = set()
        while pid > 0 and pid not in visited and clock() < deadline:
            if pid in panes:
                return panes[pid]
            visited.add(pid)
            pid = procinfo.parent(pid)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None


def peer_pane(peer_port, server_port, *, read_text=_read_text, list_dir=procinfo.list_dir,
              read_link=procinfo.read_link, uid_of=procinfo.uid_of,
              pane_reader=_pane_list, clock=time.monotonic, uid=None):
    """Resolve the client's socket inode, then its process ancestors, within two seconds.
    All /proc and tmux readers are injectable; no process or terminal is changed."""
    if procinfo.DARWIN:
        return _darwin_peer_pane(peer_port, server_port, pane_reader, clock, uid)
    deadline = clock() + 2
    uid = os.getuid() if uid is None else uid
    try:
        inode = None
        for table in (procinfo.ROOT + '/net/tcp', procinfo.ROOT + '/net/tcp6'):
            if clock() >= deadline:
                return None
            try:
                rows = read_text(table).splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                if clock() >= deadline:
                    return None
                fields = row.split()
                if len(fields) > 9 and fields[3] == '01':
                    local = int(fields[1].rsplit(':', 1)[1], 16)
                    remote = int(fields[2].rsplit(':', 1)[1], 16)
                    if local == peer_port and remote == server_port and int(fields[7]) == uid:
                        inode = fields[9]
                        break
            if inode:
                break
        if not inode:
            return None
        pid = None
        for name in list_dir(procinfo.ROOT):
            if clock() >= deadline:
                return None
            if not name.isdigit():
                continue
            folder = procinfo.ROOT + '/' + name
            try:
                if uid_of(folder) != uid:
                    continue
                for fd in list_dir(folder + '/fd'):
                    if clock() >= deadline:
                        return None
                    try:
                        if read_link(folder + '/fd/' + fd) == 'socket:[%s]' % inode:
                            pid = int(name)
                            break
                    except OSError:
                        continue
            except OSError:
                continue
            if pid:
                break
        if not pid or clock() >= deadline:
            return None
        panes = {}
        for line in pane_reader(max(.01, deadline - clock())).splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r'%\d+', parts[0]):
                panes[int(parts[1])] = parts[0]
        visited = set()
        while pid > 0 and pid not in visited and clock() < deadline:
            if pid in panes:
                return panes[pid]
            visited.add(pid)
            # comm can contain spaces and parentheses; ppid follows the final ') state'.
            pid = int(read_text(procinfo.ROOT + '/%d/stat' % pid).rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None


def load_config(path):
    try:
        config = json.loads(path.read_text())
        if (type(config.get('enabled')) is not bool or type(config.get('port')) is not int
                or not 0 < config['port'] < 65536 or not isinstance(config.get('token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', config['token'])):
            raise ValueError('Invalid agent browser config.')
        path.parent.chmod(0o700)
        path.chmod(0o600)
        if not PORT_BASE <= config['port'] < PORT_BASE + 1000:
            config['port'] = PORT_BASE + secrets.randbelow(1000)
            save_config(path, config)
        return config
    except FileNotFoundError:
        config = {'enabled': True, 'port': PORT_BASE + secrets.randbelow(1000), 'token': secrets.token_urlsafe(32)}
        save_config(path, config)
        return config


def save_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(config, handle)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class WebSocketError(Exception):
    def __init__(self, code):
        self.code = code


def _exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def frame(opcode, data=b''):
    size = len(data)
    header = bytes([0x80 | opcode])
    if size < 126:
        header += bytes([size])
    elif size < 65536:
        header += b'\x7e' + struct.pack('!H', size)
    else:
        header += b'\x7f' + struct.pack('!Q', size)
    return header + data


def websocket_messages(stream, send, limit=MAX_MESSAGE):
    """Read masked RFC 6455 text messages. Control frames may interrupt fragments."""
    parts, total, fragmented = [], 0, False
    while True:
        first, second = _exact(stream, 2)
        final, opcode = bool(first & 0x80), first & 0x0f
        if first & 0x70 or not second & 0x80:
            raise WebSocketError(1002)
        size = second & 0x7f
        if size == 126:
            size = struct.unpack('!H', _exact(stream, 2))[0]
            if size < 126:
                raise WebSocketError(1002)
        elif size == 127:
            size = struct.unpack('!Q', _exact(stream, 8))[0]
            if size < 65536 or size >> 63:
                raise WebSocketError(1002)
        if opcode >= 8:
            if not final or size > 125 or opcode not in (8, 9, 10):
                raise WebSocketError(1002)
        elif opcode not in (0, 1) or (opcode == 0) != fragmented:
            raise WebSocketError(1002 if opcode != 2 else 1003)
        elif total + size > limit:
            raise WebSocketError(1009)
        mask = _exact(stream, 4)
        data = _exact(stream, size)
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        if opcode == 8:
            if len(data) == 1:
                raise WebSocketError(1002)
            if data:
                code = struct.unpack('!H', data[:2])[0]
                if code < 1000 or code >= 5000 or code in (1004, 1005, 1006, 1015) or 1016 <= code < 3000:
                    raise WebSocketError(1002)
                try:
                    data[2:].decode('utf-8')
                except UnicodeDecodeError:
                    raise WebSocketError(1007) from None
            send(8, data)
            return
        if opcode == 9:
            send(10, data)
        elif opcode < 8:
            parts.append(data)
            total += size
            fragmented = not final
            if final:
                try:
                    yield b''.join(parts).decode('utf-8')
                except UnicodeDecodeError:
                    raise WebSocketError(1007) from None
                parts, total = [], 0


class Transport:
    """A writer thread keeps slow programs off the shared pipe's reader thread."""
    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        self.out = queue.Queue()
        self.pending_bytes = 0
        self.closed = False
        self.writer = threading.Thread(target=self._write, daemon=True, name='agent-cdp-writer')
        self.writer.start()

    def send(self, opcode, data=b''):
        if self.closed:
            return
        data = frame(opcode, data)
        with self.lock:
            if self.closed:
                return
            if not self.pending_bytes or self.pending_bytes + len(data) <= MAX_QUEUED_BYTES:
                self.pending_bytes += len(data)
                self.out.put_nowait(data)
                return
        self.abort()

    def message(self, message):
        self.send(1, json.dumps(message).encode())

    def close(self, code=1000, reason=''):
        text = reason.encode('utf-8')[:123].decode('utf-8', errors='ignore').encode('utf-8')
        self.send(8, struct.pack('!H', code) + text)

    def abort(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _write(self):
        try:
            while not self.closed:
                try:
                    data = self.out.get(timeout=.5)
                except queue.Empty:
                    continue
                remaining = memoryview(data)
                deadline = time.monotonic() + 30
                while remaining and not self.closed:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([], [self.sock], [], left)[1]:
                        raise TimeoutError('WebSocket write timed out.')
                    try:
                        # TLS sockets reject MSG_DONTWAIT. Their existing read/write timeout
                        # bounds sends; raw loopback sockets retain nonblocking writes.
                        written = (self.sock.send(remaining) if isinstance(self.sock, ssl.SSLSocket)
                                   else self.sock.send(remaining, socket.MSG_DONTWAIT))
                    except BlockingIOError:
                        continue
                    if not written:
                        raise OSError('WebSocket closed.')
                    remaining = remaining[written:]
                with self.lock:
                    self.pending_bytes -= len(data)
                if data[0] & 0x0f == 8:
                    break
        except OSError:
            pass
        finally:
            self.abort()


SCOPED = {'Browser.setDownloadBehavior', 'Browser.grantPermissions', 'Browser.resetPermissions',
          'Storage.getCookies', 'Storage.setCookies', 'Storage.clearCookies'}
TARGET_COMMANDS = {'Target.attachToTarget', 'Target.closeTarget', 'Target.activateTarget', 'Target.getTargetInfo'}
AS_IS = {'Browser.getVersion', 'Browser.getWindowForTarget', 'Browser.setWindowBounds', 'Browser.getWindowBounds',
         'Target.setAutoAttach', 'Target.setDiscoverTargets', 'SystemInfo.getInfo', 'Runtime.runIfWaitingForDebugger'}
SWALLOWED = {'Browser.close', 'Browser.crash', 'Browser.crashGpuProcess'}


class AgentClient:
    def __init__(self, proxy, transport, pane):
        self.proxy, self.transport, self.pane = proxy, transport, pane
        self.since = time.time()
        self.lock = threading.RLock()
        self.conn = None
        self.root = self.default = None
        self.contexts = set()
        self.created_contexts = set()
        self.sessions = {}  # child session -> target id
        self.emulation = {}  # target -> successful viewport commands; guarded by Browser.lock
        self.targets = {}  # live owned targets
        self.known_targets = set()  # retain ids until disconnect to filter destruction events
        self.pending_pages = 0
        self.closed = False

    def open(self):
        self.proxy.browser.ensure()
        available, reason = self.proxy.availability()
        if not available:
            raise browser.BrowserError(reason)
        self.conn = self.proxy.browser.conn
        self.root = self.conn.call('Target.attachToBrowserTarget')['sessionId']
        self.conn.register_sink(self.root, self.event)
        self.default = self.conn.call('Target.createBrowserContext', {'disposeOnDetach': True}, self.root)['browserContextId']
        with self.lock:
            self.contexts.add(self.default)

    def error(self, message, text, code=-32000):
        reply = {'id': message.get('id'), 'error': {'code': code, 'message': text}}
        if message.get('sessionId'):
            reply['sessionId'] = message['sessionId']
        self.transport.message(reply)

    def handle(self, message):
        if (not isinstance(message, dict) or type(message.get('id')) is not int
                or not isinstance(message.get('method'), str) or not isinstance(message.get('params', {}), dict)):
            self.error(message if isinstance(message, dict) else {}, 'Invalid CDP request.', -32600)
            return
        method = message['method']
        params = dict(message.get('params') or {})
        session = message.get('sessionId')
        with self.lock:
            if self.closed:
                return
            if session and session not in self.sessions:
                self.error(message, 'That session belongs to another agent.')
                return
            root = not session
            if root:
                if method in SWALLOWED:
                    self.transport.message({'id': message['id'], 'result': {}})
                    return
                if method == 'Target.attachToBrowserTarget':
                    self.error(message, 'Not allowed through Tether.', -32601)
                    return
                if method == 'Target.createTarget':
                    context = params.setdefault('browserContextId', self.default)
                    if context not in self.contexts:
                        self.error(message, 'That context belongs to another agent.')
                        return
                    pages = sum(info.get('type') == 'page' for info in self.targets.values())
                    if pages + self.pending_pages >= TAB_LIMIT:
                        self.error(message, TAB_ERROR)
                        return
                    self.pending_pages += 1
                elif method in SCOPED or method == 'Target.disposeBrowserContext':
                    if method in SCOPED:
                        params.setdefault('browserContextId', self.default)
                    if params.get('browserContextId') not in self.contexts:
                        self.error(message, 'That context belongs to another agent.')
                        return
                elif method in TARGET_COMMANDS:
                    # Playwright uses the browser session's implicit target as an auto-attach barrier.
                    implicit_browser = method == 'Target.getTargetInfo' and 'targetId' not in params
                    if not implicit_browser and params.get('targetId') not in self.targets:
                        self.error(message, 'That target belongs to another agent.')
                        return
                    if method == 'Target.attachToTarget' and params.get('flatten') is not True:
                        self.error(message, 'Use flattened sessions through Tether.')
                        return
                elif method not in AS_IS | {'Target.getTargets', 'Target.getBrowserContexts', 'Target.createBrowserContext'}:
                    self.proxy.log_denied(method)
                    self.error(message, 'Not allowed through Tether.', -32601)
                    return
        target = self.sessions.get(session)
        slot = browser.AGENT_EMULATION.get(method) if session else None
        live_page = session and self.proxy.browser.container is not None and self.targets.get(target, {}).get('type') == 'page'
        if slot or (live_page and 'metrics' not in self.emulation.get(target, {})):
            # Serialize these commands with panel handoffs. Never wait with self.lock held: the reader uses it.
            with self.proxy.browser.lock:
                tab = self.proxy.browser.tabs.get(target)
                user_controls_viewport = tab is not None and tab.controller == 'user'
                if slot and user_controls_viewport:
                    self.error(message, "The user controls this tab's viewport. Return control to the agent first.")
                    return
                try:
                    saved = self.emulation.get(target, {})
                    if live_page and not user_controls_viewport and slot != 'metrics' and 'metrics' not in saved:
                        # Initialize before the agent can inspect the page or take its first screenshot.
                        self.conn.call(*browser.LIVE_AGENT_METRICS, session=session)
                        self.emulation.setdefault(target, {})['metrics'] = browser.AGENT_DEFAULTS['metrics']
                    if slot:
                        if live_page and method == 'Emulation.clearDeviceMetricsOverride':
                            result = browser.reset_live_agent_metrics(self.conn, session)
                        else:
                            result = self.conn.call(method, params, session)
                        self.emulation.setdefault(target, {})[slot] = (method, params)
                        self.transport.message({'id': message['id'], 'sessionId': session, 'result': result})
                except browser.BrowserError as exc:
                    self.error(message, str(exc))
                    return
            if slot:
                return
        outgoing = dict(message, params=params, sessionId=session or self.root)
        completed = threading.Event() if root and method == 'Target.createTarget' else None
        def answered(reply):
            try:
                self.reply(message, params, root, reply)
            finally:
                if completed:
                    completed.set()
        try:
            self.conn.forward(outgoing, answered)
            # Serialize page creation per client so discovery events and pending commands cannot double-count a tab.
            if completed and not completed.wait(browser.CALL_TIMEOUT):
                self.transport.close(1011)
        except browser.BrowserError:
            if root and method == 'Target.createTarget':
                with self.lock:
                    self.pending_pages -= 1
            self.transport.close(1011)

    def reply(self, original, params, root, reply):
        method = original['method']
        result = dict(reply.get('result') or {})
        with self.lock:
            if root and method == 'Target.createTarget':
                self.pending_pages -= 1
            if 'error' not in reply and root:
                if method == 'Target.createBrowserContext':
                    context = result['browserContextId']
                    self.contexts.add(context)
                    self.created_contexts.add(context)
                    if self.closed:
                        self.conn.send('Target.disposeBrowserContext', {'browserContextId': context}, self.root)
                elif method == 'Target.getBrowserContexts':
                    result['browserContextIds'] = [c for c in result.get('browserContextIds', []) if c in self.created_contexts]
                elif method == 'Target.getTargets':
                    infos = [info for info in result.get('targetInfos', []) if info.get('browserContextId') in self.contexts]
                    for info in infos:
                        self.remember(info)
                    result['targetInfos'] = infos
                elif method == 'Target.createTarget':
                    self.remember({'targetId': result['targetId'], 'type': 'page', 'browserContextId': params['browserContextId']})
                elif method == 'Target.attachToTarget':
                    self.sessions[result['sessionId']] = params['targetId']
                    self.conn.register_sink(result['sessionId'], self.event)
                elif method == 'Target.disposeBrowserContext':
                    context = params['browserContextId']
                    self.contexts.discard(context)
                    self.created_contexts.discard(context)
            if self.closed:
                return
        outgoing = {'id': original['id']}
        outgoing.update(error=reply['error']) if 'error' in reply else outgoing.update(result=result)
        if original.get('sessionId'):
            outgoing['sessionId'] = original['sessionId']
        self.transport.message(outgoing)

    def remember(self, info):
        target = info['targetId']
        previous = self.targets.get(target, {})
        self.targets[target] = dict(previous, **info)
        self.known_targets.add(target)

    def target_event(self, method, params):
        """Global discovery updates ownership before browser-level sessions see those events."""
        with self.lock:
            if self.closed:
                return
            info = params.get('targetInfo') or {}
            target = info.get('targetId') or params.get('targetId')
            if method in ('Target.targetCreated', 'Target.targetInfoChanged') and info.get('browserContextId') in self.contexts:
                is_new = target not in self.targets
                if is_new and info.get('type') == 'page' and sum(t.get('type') == 'page' for t in self.targets.values()) >= TAB_LIMIT:
                    self.conn.send('Target.closeTarget', {'targetId': target}, self.root)
                    return
                self.remember(info)
                if method == 'Target.targetCreated' and info.get('type') == 'page':
                    threading.Thread(target=self.proxy.browser.adopt_agent, args=(info, self),
                                     daemon=True, name='agent-tab-adopt').start()
            elif method == 'Target.targetDestroyed':
                self.targets.pop(target, None)

    def event(self, message):
        params = message.get('params') or {}
        method = message['method']
        parent = message.get('sessionId')
        with self.lock:
            if self.closed:
                return
            if method == 'Target.attachedToTarget':
                info = params.get('targetInfo') or {}
                child = params.get('sessionId')
                over_limit = (info.get('type') == 'page' and info.get('targetId') not in self.targets
                              and sum(t.get('type') == 'page' for t in self.targets.values()) >= TAB_LIMIT)
                if info.get('browserContextId') not in self.contexts or over_limit:
                    if params.get('waitingForDebugger'):
                        self.conn.send('Runtime.runIfWaitingForDebugger', session=child)
                    self.conn.send('Target.detachFromTarget', {'sessionId': child}, parent)
                    if over_limit:
                        self.conn.send('Target.closeTarget', {'targetId': info['targetId']}, self.root)
                    return
                self.remember(info)
                self.sessions[child] = info['targetId']
                self.conn.register_sink(child, self.event)
            elif method in ('Target.targetCreated', 'Target.targetInfoChanged'):
                info = params.get('targetInfo') or {}
                self.target_event(method, params)
                if info.get('browserContextId') not in self.contexts or info.get('targetId') not in self.targets:
                    return
            elif method == 'Target.targetDestroyed':
                if params.get('targetId') not in self.known_targets:
                    return
                self.targets.pop(params['targetId'], None)
            elif method == 'Target.detachedFromTarget':
                child = params.get('sessionId')
                if child not in self.sessions:
                    return
                self.sessions.pop(child)
                self.conn.unregister_sink(child)
            outgoing = dict(message)
            if parent == self.root:
                outgoing.pop('sessionId', None)
        self.transport.message(outgoing)

    def cleanup(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            contexts = list(self.contexts)
            sessions = [self.root, *self.sessions]
        if self.conn:
            for session in sessions:
                self.conn.unregister_sink(session)
            for context in contexts:
                try:
                    self.conn.call('Target.disposeBrowserContext', {'browserContextId': context}, self.root, timeout=3)
                except browser.BrowserError:
                    pass
            if self.root:
                try:
                    self.conn.call('Target.detachFromTarget', {'sessionId': self.root}, timeout=3)
                except browser.BrowserError:
                    pass
        with self.proxy.lock:
            self.proxy.clients.discard(self)


class Proxy:
    def __init__(self, the_browser=None, config_path=CONFIG, notifier=lambda *args, **kwargs: None,
                 env_runner=subprocess.run, pane_finder=peer_pane):
        self.browser = the_browser or browser.BROWSER
        if self.browser.signed:
            raise ValueError('Agents cannot access the signed-in browser.')
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.notifier, self.env_runner, self.pane_finder = notifier, env_runner, pane_finder
        self.lock = threading.RLock()
        self.clients = set()
        self.asks = []
        self.denied = set()
        self.major = None
        self.httpd = self.thread = None
        self.stopped = False
        self.signed_access = None
        self.browser.agent_proxy = self
        if self.browser.product:
            self.version_checked(self.browser.product)

    @property
    def url(self):
        return 'ws://127.0.0.1:%d/cdp/%s' % (self.config['port'], self.config['token'])

    def availability(self):
        if not self.config['enabled']:
            return False, 'Agent browser is disabled.'
        if not self.browser.chrome_finder():
            return False, 'No Chromium found.'
        if self.major is not None and self.major < 120:
            return False, 'Chromium 120 or newer is required.'
        return True, ''

    def version_checked(self, product):
        found = re.search(r'/(\d+)', product)
        self.major = int(found.group(1)) if found else 0
        self.sync_env()

    def sync_env(self, clear=False):
        available = not clear and self.availability()[0]
        for name in ENV_NAMES:
            command = [paths.TMUX, 'set-environment', '-g', name, self.url] if available else [paths.TMUX, 'set-environment', '-gu', name]
            try:
                self.env_runner(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            except (OSError, subprocess.SubprocessError):
                LOG.warning('Could not update tmux environment for %s', name)

    def start(self, shutdown_hooks=False):
        start_port = self.config['port']
        for n in range(1000):
            port = PORT_BASE + (start_port - PORT_BASE + n) % 1000
            try:
                self.httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:  # retry occupied ports, never sandbox/permission errors
                    raise
        if self.httpd is None:
            raise OSError('No free agent browser port.')
        self.httpd.daemon_threads = True
        self.httpd.proxy = self
        self.config['port'] = self.httpd.server_port
        save_config(self.config_path, self.config)
        LOG.info('Agent CDP proxy on 127.0.0.1:%d', self.config['port'])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name='agent-cdp-http')
        self.thread.start()
        self.sync_env()
        if shutdown_hooks:
            atexit.register(self.stop)
            previous = signal.getsignal(signal.SIGTERM)
            def terminate(signum, frame):
                self.stop()
                if callable(previous):
                    previous(signum, frame)
                elif previous != signal.SIG_IGN:
                    raise SystemExit(128 + signum)
            signal.signal(signal.SIGTERM, terminate)
        return self

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.signed_access:
            self.signed_access.stop()
        self.sync_env(clear=True)
        self.browser_stopped()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)

    def busy(self):
        with self.lock:
            return bool(self.clients)

    def owns_context(self, context):
        with self.lock:
            return any(context in client.contexts for client in self.clients if not client.closed)

    def browser_stopped(self):
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            if client.transport:
                client.transport.close(1011)

    def target_event(self, method, params):
        if method not in ('Target.targetCreated', 'Target.targetInfoChanged', 'Target.targetDestroyed'):
            return
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.target_event(method, params)

    def log_denied(self, method):
        with self.lock:
            if method not in self.denied:
                self.denied.add(method)
                LOG.warning('CDP method not allowed through Tether: %s', method)

    def state(self):
        available, reason = self.availability()
        with self.lock:
            clients, asks = list(self.clients), list(self.asks)
        agents = []
        for client in clients:
            with client.lock:
                if not client.closed:
                    agents.append({'pane': client.pane, 'tabs': [target for target, info in client.targets.items()
                                                               if info.get('type') == 'page'], 'since': client.since})
        return {'enabled': self.config['enabled'], 'available': available, 'reason': reason,
                'agents': sorted(agents, key=lambda agent: agent['since']), 'asks': asks}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean.')
        with self.lock:
            self.config['enabled'] = enabled
            save_config(self.config_path, self.config)
        self.sync_env()
        if not enabled:
            self.browser_stopped()
        return self.state()

    def ask(self, text, pane):
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError('Ask text must contain 1 to 300 characters.')
        if pane is not None and (not isinstance(pane, str) or not re.fullmatch(r'%\d+', pane)):
            raise ValueError('Invalid pane.')
        ask = {'id': secrets.token_urlsafe(12), 'pane': pane, 'text': text, 'at': time.time()}
        with self.lock:
            self.asks = [entry for entry in self.asks if entry['pane'] != pane][-19:] + [ask]
        payload = {'title': 'Agent needs you in the browser', 'body': text, 'tag': 'browser-ask-' + (pane or 'unknown'),
                   'session': pane, 'priority': 'urgent', 'url': '/mobile/' + ('#session=' + quote(pane, safe='') if pane else '')}
        self.notifier(payload, urgent=True)
        return {'id': ask['id']}

    def dismiss(self, ident):
        if not isinstance(ident, str):
            raise ValueError('Choose an ask.')
        with self.lock:
            self.asks = [ask for ask in self.asks if ask['id'] != ident]
        return self.state()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # never log the capability token in a request path

    def parse_request(self):
        # Apply capability and rebinding checks even to unsupported HTTP methods.
        return super().parse_request() and self.route() is not None

    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def route(self):
        parts = urlsplit(self.path).path.split('/')
        token = parts[2] if len(parts) >= 3 else ''
        proxy = self.server.proxy
        if not hmac.compare_digest(token.encode(), proxy.config['token'].encode()):
            self.respond(404, {})
            return None
        if self.headers.get_all('Origin') is not None:
            self.respond(403, {})
            return None
        hosts = self.headers.get_all('Host', [])
        port = proxy.config['port']
        if len(hosts) != 1 or hosts[0] not in ('127.0.0.1:%d' % port, 'localhost:%d' % port):
            self.respond(403, {})
            return None
        return '/' + '/'.join([parts[1], *parts[3:]])

    def do_GET(self):
        route = self.route()
        if route is None:
            return
        proxy = self.server.proxy
        if route not in ('/health', '/cdp', '/cdp/json/version', '/cdp/json/version/'):
            self.respond(404, {})
            return
        available, reason = proxy.availability()
        if route == '/health':
            self.respond(200 if available else 503, {'ok': True} if available else {'ok': False, 'reason': reason})
        elif not available:
            self.respond(503, {'ok': False, 'reason': reason})
        elif route != '/cdp':
            self.respond(200, {'Browser': proxy.browser.product or 'Chrome', 'webSocketDebuggerUrl': proxy.url})
        else:
            self.upgrade(proxy)

    def upgrade(self, proxy):
        key = self.headers.get('Sec-WebSocket-Key', '')
        try:
            valid_key = len(base64.b64decode(key, validate=True)) == 16
        except ValueError:
            valid_key = False
        if (self.headers.get('Upgrade', '').lower() != 'websocket'
                or 'upgrade' not in [part.strip().lower() for part in self.headers.get('Connection', '').split(',')]
                or self.headers.get('Sec-WebSocket-Version') != '13' or not valid_key):
            self.respond(400, {})
            return
        with proxy.lock:
            if len(proxy.clients) >= MAX_CLIENTS:
                self.respond(503, {})
                return
            client = self.create_client(proxy)
            proxy.clients.add(client)  # reserve a slot before Chromium starts
        transport = None
        try:
            if client.pane is None:
                client.pane = proxy.pane_finder(self.client_address[1], proxy.config['port'])
            client.open()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            self.connection.settimeout(None)
            transport = client.transport = Transport(self.connection)
            if client.closed:
                raise browser.BrowserError('That connection has ended.')
            for raw in websocket_messages(self.rfile, transport.send):
                try:
                    message = json.loads(raw)
                except ValueError:
                    raise WebSocketError(1007) from None
                client.handle(message)
        except browser.BrowserError as exc:
            if transport:
                transport.close(1011)
            else:
                self.respond(503, {'ok': False, 'reason': str(exc)})
        except WebSocketError as exc:
            transport.close(exc.code)
        except (EOFError, OSError):
            pass
        finally:
            client.cleanup()
            if transport:
                transport.close()
                transport.writer.join(timeout=2)
                transport.abort()

    def create_client(self, proxy):
        return AgentClient(proxy, None, None)

    def do_POST(self):
        route = self.route()
        if route is None:
            return
        if route not in ('/ask', '/ask-signed'):
            self.respond(404, {})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                self.respond(413, {})
                return
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Invalid request.')
            # The pane that owns the socket wins over the pane the agent names, so an agent cannot ask as another tab.
            # The named pane is used only when the lookup finds nothing (for example on another host).
            pane = self.server.proxy.pane_finder(self.client_address[1], self.server.proxy.config['port']) or body.get('pane')
            if route == '/ask-signed':
                access = self.server.proxy.signed_access
                if access is None:
                    raise ValueError('Signed browser access is unavailable.')
                self.respond(200, access.ask(body.get('text'), pane, body.get('minutes', 15), body.get('url'),
                                            **({'browser': body['browser']} if 'browser' in body else {})))
            else:
                self.respond(200, self.server.proxy.ask(body.get('text'), pane))
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {'error': str(exc)})


PROXY = None


def start(notifier, agent_finder=lambda pane: 'agent', identity_finder=lambda pane: None):
    global PROXY
    import signed_access
    import mac_browser
    PROXY = Proxy(notifier=notifier).start(shutdown_hooks=True)
    PROXY.signed_access = mac_browser.RoutedAccess(
        signed_access.Access(notifier=notifier, agent_finder=agent_finder, identity_finder=identity_finder),
        mac_browser.MacAccess(mac_browser.BROKER, notifier=notifier, agent_finder=agent_finder, identity_finder=identity_finder))
    return PROXY
