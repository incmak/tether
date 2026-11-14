"""Panel browsers driven over private Chrome DevTools pipes.

The browser starts on first use with `--remote-debugging-pipe`: commands go in on the child's fd 3 and replies and
events come out on fd 4, one JSON message per NUL byte. No TCP debug port opens, and fds 3/4 are Unix sockets so
other processes cannot open them through /proc.
It keeps its own profile under the cache folder (logins survive restarts) and stops after IDLE_SECONDS of no use.
Pages use Page.startScreencast. Frames are acknowledged on receipt and replace one newest-frame slot per tab;
network viewers never hold up capture. See .work-notes/2026-11-02/panels/browser-research.md for sources."""
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import threading
import time
from urllib.parse import urlsplit

import paths
import procinfo
from browser_display import VirtualDisplay

IDLE_SECONDS = 15 * 60
CALL_TIMEOUT = 15
MAX_TABS = 12
PROFILE = paths.CACHE / 'browser' / 'profile'  # both folders are mode 700
SIGNED_PROFILE = paths.CACHE / 'signed-browser' / 'profile'
MODES = ('private', 'saved')
DESKTOP = (1280, 800)
# JPEG quality of streamed frames. At 60, text on a 2x screen showed blocks. Desktop tabs render at 2x (PRESETS) for the same reason;
# the stream is still capped at the panel size in device pixels.
QUALITY = 80
# Seconds after the last viewer leaves before a user tab's window is minimized (see Browser._park).
PARK_SECONDS = 5

IPHONE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
             'Version/18.0 Mobile/15E148 Safari/604.1')
IPAD_UA = ('Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
           'Version/18.0 Mobile/15E148 Safari/604.1')
PRESETS = {
    'desktop': {'label': 'Desktop', 'width': DESKTOP[0], 'height': DESKTOP[1], 'scale': 2, 'mobile': False},
    'pixel': {'label': 'Pixel 8', 'width': 412, 'height': 915, 'scale': 2.625, 'mobile': True, 'ua': 'android'},
    'iphone': {'label': 'iPhone 15', 'width': 393, 'height': 852, 'scale': 3, 'mobile': True, 'ua': IPHONE_UA},
    'tablet': {'label': 'iPad Air', 'width': 820, 'height': 1180, 'scale': 2, 'mobile': True, 'ua': IPAD_UA},
}

# Defaults undo panel overrides when an agent never set that property. AgentClient records successful commands
# by target under Browser.lock, so a control handoff can replay the exact parameters, including clear requests.
AGENT_EMULATION = {
    'Emulation.setDeviceMetricsOverride': 'metrics',
    'Emulation.clearDeviceMetricsOverride': 'metrics',
    'Emulation.setTouchEmulationEnabled': 'touch',
    'Emulation.setEmitTouchEventsForMouse': 'mouse',
    'Emulation.setUserAgentOverride': 'ua',
}
AGENT_DEFAULTS = {
    'metrics': ('Emulation.clearDeviceMetricsOverride', {}),
    'touch': ('Emulation.setTouchEmulationEnabled', {'enabled': False}),
    'mouse': ('Emulation.setEmitTouchEventsForMouse', {'enabled': False}),
    'ua': ('Emulation.setUserAgentOverride', {'userAgent': ''}),
}


class BrowserError(RuntimeError):
    """A problem worth showing to the person (no browser installed, page gone, bad URL)."""


def find_chrome():
    """The Chromium binary: $MOBILE_TMUX_CHROME, one on PATH, or the newest Playwright download."""
    explicit = os.environ.get('MOBILE_TMUX_CHROME')
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome'):
        found = shutil.which(name)
        if found:
            return found
    if procinfo.DARWIN:
        for root in ('/Applications', paths.HOME / 'Applications'):
            for app, binary in (('Chromium', 'Chromium'), ('Google Chrome', 'Google Chrome')):
                path = str(Path(root) / (app + '.app') / 'Contents/MacOS' / binary)
                if os.access(path, os.X_OK):
                    return path
    builds = glob.glob(str(paths.HOME / '.cache/ms-playwright/chromium-*/chrome-linux*/chrome'))
    if procinfo.DARWIN:
        for app in ('Chromium', 'Google Chrome for Testing'):
            builds += glob.glob(str(paths.HOME / 'Library/Caches/ms-playwright' /
                                    ('chromium-*/chrome-mac*/%s.app/Contents/MacOS/%s' % (app, app))))
    builds = sorted(builds,
                    key=lambda path: int(re.search(r'chromium-(\d+)', path).group(1)), reverse=True)
    return next((path for path in builds if os.access(path, os.X_OK)), None)


def find_signed_chrome():
    """The signed-in browser always uses branded Chrome, never the agent's override."""
    if procinfo.DARWIN:
        for root in ('/Applications', paths.HOME / 'Applications'):
            path = str(Path(root) / 'Google Chrome.app/Contents/MacOS/Google Chrome')
            if os.access(path, os.X_OK):
                return path
        return None
    return '/usr/bin/google-chrome' if os.access('/usr/bin/google-chrome', os.X_OK) else None


def needs_no_sandbox():
    """True when the kernel blocks the user-namespace sandbox (Ubuntu 23.10+ AppArmor) and no SUID sandbox is set up.
    Chromium aborts at start in that case; Playwright and Puppeteer pass --no-sandbox there too."""
    if os.environ.get('MOBILE_TMUX_BROWSER_SANDBOX') in ('0', '1'):
        return os.environ['MOBILE_TMUX_BROWSER_SANDBOX'] == '0'
    if os.environ.get('CHROME_DEVEL_SANDBOX'):
        return False
    try:
        with open('/proc/sys/kernel/apparmor_restrict_unprivileged_userns') as handle:
            return handle.read().strip() == '1'
    except OSError:
        return False


SETTINGS_URL = re.compile(r'chrome://settings(/[A-Za-z0-9/_-]*)?([?#][A-Za-z0-9=&%_.-]*)?', re.I)


def normalize_url(text, settings=False):
    """The URL to open, or ValueError. Only http, https and about:blank. A bare port or host:port means local http.
    settings: also chrome://settings pages. Only the signed-in browser's own panel passes it (Mak); agents never do."""
    text = str(text or '').strip()
    if not text or text == 'about:blank':
        return 'about:blank'
    if settings and SETTINGS_URL.fullmatch(text):
        return 'chrome://settings' + text[len('chrome://settings'):]
    if len(text) > 4096 or any(ord(char) < 32 for char in text):
        raise ValueError('That address is not valid.')
    if re.fullmatch(r'\d{2,5}([/?#].*)?', text):  # "8765" or "8765/path": a local port, not the IP 0.0.34.61
        return 'http://127.0.0.1:' + text
    if re.fullmatch(r'(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:\d{1,5})?(/.*)?', text, re.I):
        return 'http://' + re.sub(r'^(localhost|0\.0\.0\.0)', '127.0.0.1', text, flags=re.I)
    scheme = text.split(':', 1)[0].lower() if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', text) else ''
    if scheme and scheme not in ('http', 'https') and not re.match(r'^[^/:]+:\d', text):
        raise ValueError('Only http and https addresses can be opened.')
    if not scheme or re.match(r'^[^/:]+:\d', text):
        text = ('http://' if re.match(r'^[^/]*:\d', text) else 'https://') + text
    parts = urlsplit(text)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('That address is not valid.')
    return text


def mobile_android_ua(desktop_ua):
    version = re.search(r'Chrome/([\d.]+)', desktop_ua or '')
    return ('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/%s Mobile Safari/537.36'
            % (version.group(1) if version else '147.0.0.0'))


class Connection:
    """CDP over a pair of file descriptors: NUL-separated JSON. A reader thread routes replies by id and events
    to `on_event(method, params, session_id)`."""

    def __init__(self, read_fd, write_fd, on_event, on_close=None):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.on_event, self.on_close = on_event, on_close
        self.lock = threading.Lock()
        self.next_id = 0
        self.waiting = {}
        self.sinks = {}
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True, name='browser-cdp-reader')
        self.reader.start()

    def call(self, method, params=None, session=None, timeout=CALL_TIMEOUT):
        """Send one command and wait for its result. Raises BrowserError on a protocol error or a closed browser."""
        slot = {'event': threading.Event()}
        def reply(message):
            slot.update(message)
            slot['event'].set()
        message = {'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        ident = self.forward(message, reply)
        if not slot['event'].wait(timeout):
            with self.lock:
                self.waiting.pop(ident, None)
            raise BrowserError('The browser did not answer (%s).' % method)
        if 'error' in slot:
            raise BrowserError(str(slot['error'].get('message') or 'The browser refused %s.' % method))
        return slot.get('result') or {}

    def forward(self, message, reply):
        """Assign a pipe-global id and deliver the raw reply to a callback on the reader thread.
        Callbacks and session sinks must not wait for CDP replies or take Browser.lock."""
        with self.lock:
            if self.closed:
                raise BrowserError('The browser stopped.')
            self.next_id += 1
            ident = self.next_id
            self.waiting[ident] = reply
            message = dict(message, id=ident)
            data = json.dumps(message).encode() + b'\0'
            try:
                while data:
                    data = data[os.write(self.write_fd, data):]
            except OSError:
                self.waiting.pop(ident, None)
                raise BrowserError('The browser stopped.')
        return ident

    def register_sink(self, session, sink):
        # Dict operations are atomic, as for waiting.pop below. The reader must never wait on a pipe writer.
        self.sinks[session] = sink

    def unregister_sink(self, session):
        self.sinks.pop(session, None)

    def send(self, method, params=None, session=None):
        """Fire and forget (screencast acks, input moves): errors are ignored."""
        threading.Thread(target=self._quiet_call, args=(method, params, session), daemon=True).start()

    def _quiet_call(self, method, params, session):
        try:
            self.call(method, params, session, timeout=5)
        except BrowserError:
            pass

    def _read(self):
        buffer = b''
        try:
            while True:
                chunk = os.read(self.read_fd, 1 << 20)
                if not chunk:
                    break
                buffer += chunk
                while b'\0' in buffer:
                    raw, buffer = buffer.split(b'\0', 1)
                    self._dispatch(raw)
        except OSError:
            pass
        finally:
            self._shut()

    def _dispatch(self, raw):
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if 'id' in message:
            reply = self.waiting.pop(message['id'], None)
            if reply is not None:
                try:
                    reply(message)
                except Exception:
                    pass
        elif 'method' in message:
            try:
                sink = self.sinks.get(message.get('sessionId'))
                if sink:
                    sink(message)
                self.on_event(message['method'], message.get('params') or {}, message.get('sessionId'))
            except Exception:  # an event handler bug must not kill the reader
                pass

    def _shut(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            pending, self.waiting = self.waiting, {}
            self.sinks = {}
        for ident, reply in pending.items():
            try:
                reply({'id': ident, 'error': {'code': -32000, 'message': 'The browser stopped.'}})
            except Exception:
                pass
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.on_close:
            self.on_close()


def _high_fd(fd):
    """Move a pipe end above 9 (close-on-exec) so dup2 onto 3 and 4 never collides with it."""
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


def _channel():
    """One direction of the DevTools channel as (read end, write end). A Unix socket pair, not os.pipe(): another process
    of the same user can open a pipe through /proc/<pid>/fd/N and inject commands, but opening a socket that way fails."""
    one, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return _high_fd(one.detach()), _high_fd(other.detach())


def launch(chrome, profile, extra=(), *, headed=False, env=None):
    """Start Chromium in its own process group with the DevTools pipe on fds 3 and 4. Returns (pid, read_fd, write_fd)."""
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    for folder in (profile.parent, profile):  # mkdir's mode does not apply to folders that already exist
        folder.chmod(0o700)
    args = [chrome, *(['--disable-blink-features=AutomationControlled', '--window-position=0,0'] if headed else ['--headless=new']),
            '--remote-debugging-pipe', '--user-data-dir=' + str(profile),
            '--no-first-run', '--no-default-browser-check', '--disable-sync', '--mute-audio', '--password-store=basic',
            '--window-size=%d,%d' % DESKTOP, '--force-device-scale-factor=2', *extra, 'about:blank']
    if needs_no_sandbox():
        args.insert(2, '--no-sandbox')
    to_child_r, to_child_w = _channel()
    from_child_r, from_child_w = _channel()
    actions = [(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
               (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
               (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
               (os.POSIX_SPAWN_DUP2, to_child_r, 3),
               (os.POSIX_SPAWN_DUP2, from_child_w, 4)]
    try:
        pid = os.posix_spawn(chrome, args, dict(os.environ, **(env or {})), file_actions=actions, setsid=True)
    except OSError:
        os.close(from_child_r)
        os.close(to_child_w)
        raise
    finally:
        os.close(to_child_r)
        os.close(from_child_w)
    return pid, from_child_r, to_child_w


class Tab:
    def __init__(self, target, session, mode='saved', context=None, owner=None):
        self.target, self.session = target, session
        self.mode, self.context = mode, context  # private tabs own an in-memory browser context
        self.owner = owner
        self.controller = 'agent' if mode == 'agent' else 'user'
        self.agent_client = None
        self.grant = None
        self.url, self.title = 'about:blank', ''
        self.preset = 'desktop'
        self.size = DESKTOP
        self.frame = None  # (seq, metadata, base64 jpeg)
        self.seq = 0
        self.acked = 0
        self.viewers = 0
        self.casting = None  # (max width, max height) while a screencast runs
        self.viewer_sizes = []
        self.last_input = 0.0
        self.changed = threading.Condition()
        self.healed = 0.0  # when the emulation was last applied again (see Browser.heal)
        self.parked = False  # the tab's window is minimized because nobody watches it

    def row(self):
        preset = PRESETS[self.preset]
        width, height = self.size if self.preset == 'desktop' else (preset['width'], preset['height'])
        row = {'id': self.target, 'url': self.url, 'title': self.title, 'preset': self.preset, 'mode': self.mode,
               'width': width, 'height': height, 'touch': preset['mobile']}
        if self.mode == 'agent':
            row['owner'] = self.owner or {'pane': None}
            row['controller'] = self.controller
            if self.grant:
                row['grant'] = self.grant
        return row


class Browser:
    """One panel browser. Public methods start it when needed and are safe to call from any thread."""

    def __init__(self, launcher=launch, chrome_finder=None, profile=None, clock=time.monotonic,
                 *, signed=False, display=None):
        self.launcher, self.clock = launcher, clock
        self.chrome_finder = chrome_finder or (find_signed_chrome if signed else find_chrome)
        self.profile = profile if profile is not None else (SIGNED_PROFILE if signed else PROFILE)
        self.signed = signed
        self.display = (display or VirtualDisplay()) if signed else None
        self.default_mode = 'saved' if signed else 'private'
        self.lock = threading.RLock()
        self.conn = None
        self.pid = None
        self.tabs = {}
        self.order = []
        self.by_session = {}
        self.version = 0
        self.user_agent = ''
        self.product = ''
        self.agent_proxy = None
        self.used = clock()
        self.watcher = None
        self.idle_stop = threading.Event()

    # life cycle ------------------------------------------------------------------------------------------------
    def running(self):
        return self.conn is not None and not self.conn.closed

    def touch(self):
        self.used = self.clock()

    def ensure(self):
        with self.lock:
            self.touch()
            if self.running():
                return
            if self.pid or self.conn:
                self.stop()
            self._reset()
            chrome = self.chrome_finder()
            if not chrome:
                raise BrowserError('Google Chrome is not installed.' if self.signed else
                                   'No Chromium found. Install one or set MOBILE_TMUX_CHROME.')
            try:
                if self.signed:
                    env = self.display.start()
                    pid, read_fd, write_fd = self.launcher(chrome, self.profile, headed=True, env=env)
                else:
                    pid, read_fd, write_fd = self.launcher(chrome, self.profile)
                self.pid = pid
                self.conn = Connection(read_fd, write_fd, self._event, self._closed)
                version = self.conn.call('Browser.getVersion', timeout=30)
                self.product = str(version.get('product') or '')
                if self.agent_proxy:
                    self.agent_proxy.version_checked(self.product)
                self.user_agent = str(version.get('userAgent') or '').replace('HeadlessChrome', 'Chrome')
                self.conn.call('Browser.setDownloadBehavior', {'behavior': 'deny'})
                self.conn.call('Target.setDiscoverTargets', {'discover': True})
                # The start-up about:blank page stays hidden: it keeps the browser alive when every tab is closed.
                self._create('about:blank', self.default_mode)
            except (BrowserError, OSError, RuntimeError) as exc:
                self.stop()
                raise BrowserError(str(exc)) from exc
            if self.watcher is None or not self.watcher.is_alive() or self.idle_stop.is_set():
                self.idle_stop = threading.Event()
                self.watcher = threading.Thread(target=self._watch, args=(self.idle_stop,), daemon=True, name='browser-idle')
                self.watcher.start()

    def stop(self):
        """Close the browser this server started (its own process group only)."""
        with self.lock:
            self.idle_stop.set()
            if self.agent_proxy:
                self.agent_proxy.browser_stopped()
            conn, pid = self.conn, self.pid
            self.conn = self.pid = None
            if conn and not conn.closed:
                try:
                    conn.call('Browser.close', timeout=3)
                except BrowserError:
                    pass
            if pid:
                _reap(pid)
            if conn:
                conn._shut()
            if self.display:
                self.display.stop()
            self._reset()

    def _reset(self):
        for tab in self.tabs.values():
            with tab.changed:
                tab.changed.notify_all()
        self.tabs, self.order, self.by_session = {}, [], {}
        self._bump()

    def _closed(self):
        if self.agent_proxy:
            self.agent_proxy.browser_stopped()
        threading.Thread(target=self._after_close, daemon=True).start()

    def _after_close(self):
        with self.lock:
            if self.conn is not None and self.conn.closed:
                pid, self.pid, self.conn = self.pid, None, None
                if pid:
                    _reap(pid)
                self.idle_stop.set()
                if self.display:
                    self.display.stop()
                self._reset()

    def _watch(self, stopped):
        while not stopped.wait(30):
            with self.lock:
                if stopped.is_set() or not self.running():
                    return
                busy = any(tab.viewers for tab in self.tabs.values()) or bool(self.agent_proxy and self.agent_proxy.busy())
                for tab in list(self.tabs.values()):
                    self._park(tab)  # tabs that no viewer ever opened
                if busy:
                    self.touch()
                elif self.clock() - self.used > IDLE_SECONDS:
                    self.stop()
                    return

    # tabs -----------------------------------------------------------------------------------------------------
    def _bump(self):
        self.version += 1

    def _adopt(self, target, info=None, mode='saved', context=None, owner=None):
        if self.signed and (mode != 'saved' or context):
            raise BrowserError('The signed-in browser accepts saved user tabs only.')
        session = self.conn.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        tab = Tab(target, session, mode, context, owner)
        if info:
            tab.url, tab.title = info.get('url') or tab.url, info.get('title') or ''
        self.tabs[target], self.by_session[session] = tab, tab
        self.order.append(target)
        self.conn.call('Page.enable', session=session)
        self._apply_preset(tab, 'desktop')
        if mode == 'agent':
            self._agent_size(tab)
        self._bump()
        return tab

    def _create(self, url, mode):
        """A new tab. A private tab gets its own browser context: cookies and storage live in memory only, nothing is
        written to the profile folder, and all of it is gone when the tab closes (disposeOnDetach also drops it if
        this server's pipe closes)."""
        params = {'url': 'about:blank'}
        context = None
        if mode == 'private':
            context = self.conn.call('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
            params['browserContextId'] = context
        try:
            target = self.conn.call('Target.createTarget', params)['targetId']
        except BrowserError:
            if context:
                self.conn.send('Target.disposeBrowserContext', {'browserContextId': context})
            raise
        tab = self._adopt(target, mode=mode, context=context)
        if url != 'about:blank':
            self._navigate(tab, url)
        return tab

    def tab(self, target):
        tab = self.tabs.get(target)
        if tab is None:
            raise BrowserError('That tab is closed.')
        return tab

    def state(self):
        with self.lock:
            state = {'running': self.running(), 'version': self.version,
                    'tabs': [self.tabs[target].row() for target in self.order if target in self.tabs],
                    'presets': [{'id': key, 'label': value['label'], 'width': value['width'], 'height': value['height'],
                                 'touch': value['mobile']} for key, value in PRESETS.items()]}
            if self.signed:
                state.update(getattr(self.agent_proxy, 'panel_state', self.agent_proxy.state)()
                             if self.agent_proxy else {'requests': [], 'grants': []})
            return state

    def list_tabs(self):
        self.ensure()
        with self.lock:
            if not self.order:  # every tab closed itself (window.close): offer a fresh one
                self._create('about:blank', self.default_mode)
        return self.state()

    def new_tab(self, url='about:blank', mode=None):
        url = normalize_url(url, settings=self.signed)
        mode = self.default_mode if mode is None else mode
        if mode not in MODES:
            raise ValueError('Unknown tab mode.')
        if self.signed and mode != 'saved':
            raise ValueError('The signed-in browser accepts saved tabs only.')
        self.ensure()
        with self.lock:
            if self._user_tab_count() >= MAX_TABS:
                raise BrowserError('Close a tab first (the limit is %d).' % MAX_TABS)
            tab = self._create(url, mode)
            return tab.row()

    def close_tab(self, target):
        self.ensure()
        with self.lock:
            tab = self.tab(target)
            self.conn.call('Target.closeTarget', {'targetId': target})
            self._forget(tab)
            if not self.order:
                self._create('about:blank', self.default_mode)
            return self.state()

    def _forget(self, tab):
        self.tabs.pop(tab.target, None)
        self.by_session.pop(tab.session, None)
        if tab.mode != 'agent' and tab.context and self.running() and not any(other.context == tab.context for other in self.tabs.values()):
            self.conn.send('Target.disposeBrowserContext', {'browserContextId': tab.context})  # drop its cookies now
        if tab.target in self.order:
            self.order.remove(tab.target)
        with tab.changed:
            tab.changed.notify_all()
        self._bump()

    def _forget_locked(self, tab):
        with self.lock:
            if self.tabs.get(tab.target) is tab:
                self._forget(tab)

    def navigate(self, target, url):
        url = normalize_url(url, settings=self.signed)
        self.ensure()
        tab = self.tab(target)
        return self._navigate(tab, url)

    def _navigate(self, tab, url):
        result = self.conn.call('Page.navigate', {'url': url}, tab.session, timeout=30)
        tab.url = url
        self._bump()
        error = result.get('errorText')
        return dict(tab.row(), error=error) if error else tab.row()

    def history(self, target, action):
        self.ensure()
        tab = self.tab(target)
        if action == 'reload':
            self.conn.call('Page.reload', {}, tab.session)
            return tab.row()
        if action not in ('back', 'forward'):
            raise ValueError('Unknown action.')
        found = self.conn.call('Page.getNavigationHistory', session=tab.session)
        index = found.get('currentIndex', 0) + (-1 if action == 'back' else 1)
        entries = found.get('entries') or []
        if 0 <= index < len(entries):
            self.conn.call('Page.navigateToHistoryEntry', {'entryId': entries[index]['id']}, tab.session)
        return tab.row()

    def refresh(self, tab):
        """Read the tab's title and URL again. After a navigation that swaps the renderer (about:blank to a site),
        Chromium 147 sends no targetInfoChanged for later title changes, so the stream asks about once a second."""
        try:
            info = self.conn.call('Target.getTargetInfo', {'targetId': tab.target}, timeout=5).get('targetInfo') or {}
        except (BrowserError, AttributeError):
            return
        if info.get('targetId') == tab.target and tab.target in self.tabs and (tab.url, tab.title) != (info.get('url') or tab.url, info.get('title') or ''):
            tab.url, tab.title = info.get('url') or tab.url, info.get('title') or ''
            self._bump()

    def nav_state(self, target):
        tab = self.tab(target)
        found = self.conn.call('Page.getNavigationHistory', session=tab.session)
        index, count = found.get('currentIndex', 0), len(found.get('entries') or [])
        return {'back': index > 0, 'forward': index < count - 1}

    # device presets ---------------------------------------------------------------------------------------------
    def set_device(self, target, preset, width=None, height=None):
        """A panel preset takes viewport control; 'agent' restores the agent's saved emulation on an agent tab."""
        preset = 'pixel' if preset == 'phone' else preset
        if preset not in PRESETS and preset != 'agent':
            raise ValueError('Unknown device.')
        self.ensure()
        with self.lock:
            tab = self.tab(target)
            if preset == 'agent':
                if tab.mode != 'agent':
                    raise ValueError('That tab has no agent.')
                if tab.controller == 'user':
                    saved = tab.agent_client.emulation.get(target, {}) if tab.agent_client else {}
                    for slot, default in AGENT_DEFAULTS.items():
                        method, params = saved.get(slot, default)
                        self.conn.call(method, params, tab.session)
                    tab.controller, tab.preset = 'agent', 'desktop'
                    self._agent_size(tab)
            else:
                tab.controller = 'user'
                self._apply_preset(tab, preset, width, height)
            with tab.changed:
                tab.frame = None  # a frame at the old size would map clicks wrongly; first_frame takes a new one
            self._restart_cast(tab)
            self._bump()
            return tab.row()

    def _apply_preset(self, tab, preset, width=None, height=None):
        if tab.controller == 'agent':
            return
        spec = PRESETS[preset]
        session = tab.session
        if preset == 'desktop':
            width = _clamp(width, 320, 2560, DESKTOP[0])
            height = _clamp(height, 240, 1600, DESKTOP[1])
            tab.size = (width, height)
            self.conn.call('Emulation.setDeviceMetricsOverride',
                           {'width': width, 'height': height, 'deviceScaleFactor': spec['scale'], 'mobile': False}, session)
            self.conn.call('Emulation.setTouchEmulationEnabled', {'enabled': False}, session)
            self.conn.call('Emulation.setEmitTouchEventsForMouse', {'enabled': False}, session)
            # An empty override restores the native UA, including on relayed tabs without a cached desktop UA.
            self.conn.call('Emulation.setUserAgentOverride', {'userAgent': '' if self.signed else self.user_agent}, session)
        else:
            self.conn.call('Emulation.setDeviceMetricsOverride',
                           {'width': spec['width'], 'height': spec['height'], 'deviceScaleFactor': spec['scale'],
                            'mobile': True, 'screenWidth': spec['width'], 'screenHeight': spec['height']}, session)
            self.conn.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5}, session)
            # Not setEmitTouchEventsForMouse: under it, headless Chromium 147 never answers Input.dispatchMouseEvent.
            # Browser.input sends touch events for phone and tablet tabs instead.
            self.conn.call('Emulation.setEmitTouchEventsForMouse', {'enabled': False}, session)
            agent = mobile_android_ua(self.user_agent) if spec['ua'] == 'android' else spec['ua']
            self.conn.call('Emulation.setUserAgentOverride', {'userAgent': agent}, session)
        tab.preset = preset

    # input -----------------------------------------------------------------------------------------------------
    def input(self, target, events):
        """Replay a batch of panel events. Coordinates are CSS pixels of the page viewport."""
        if not isinstance(events, list) or len(events) > 200:
            raise ValueError('Invalid input.')
        self.ensure()
        tab = self.tab(target)
        tab.last_input = self.clock()
        touch = PRESETS[tab.preset]['mobile']
        for event in events:
            method, params = input_command(event)
            if touch:
                method, params = as_touch(method, params)
                if method is None:
                    continue
            self.conn.call(method, params, tab.session, timeout=5)
        return {'ok': True}

    # screencast ------------------------------------------------------------------------------------------------
    def window_watch(self, width, height):
        if not self.signed:
            raise BrowserError('The whole-window view is only for the signed-in browser.')
        with self.lock:
            self.ensure()
            try:
                return self.display.watch(width, height)
            except RuntimeError as exc:
                raise BrowserError(str(exc)) from exc

    def window_unwatch(self):
        if not self.signed:
            raise BrowserError('The whole-window view is only for the signed-in browser.')
        with self.lock:  # no ensure(): a closing stream must never start Chrome again
            try:
                return self.display.unwatch()
            except RuntimeError as exc:
                raise BrowserError(str(exc)) from exc

    def window_input(self, events):
        if not self.signed:
            raise BrowserError('The whole-window view is only for the signed-in browser.')
        with self.lock:
            self.ensure()
            self.touch()
            try:
                return self.display.input(events)
            except RuntimeError as exc:
                raise BrowserError(str(exc)) from exc

    def watch(self, target, max_width, max_height, css_size=None):
        """Register a viewer; start the screencast when it is the first. Returns the Tab."""
        self.ensure()
        with self.lock:
            tab = self.tab(target)
            size = (_clamp(max_width, 160, 2560, 1280), _clamp(max_height, 160, 2560, 1280))
            if css_size is not None:
                if len(css_size) != 2 or any(type(v) is not int or not 1 <= v <= 8192 for v in css_size):
                    raise ValueError('Invalid viewer CSS size.')
                css_size = tuple(css_size)
            tab.viewer_sizes.append((size, css_size))
            tab.viewers = len(tab.viewer_sizes)
            previous_cast = tab.casting
            try:
                self._unpark(tab)
                if tab.viewers == 1:
                    with tab.changed:
                        tab.frame = None  # a previous viewing interval cannot supply this viewer's first frame
                if self.signed:  # headed Chrome paints only the front tab
                    self.conn.call('Page.bringToFront', session=tab.session)
                size = self._cast_size(tab)
                if tab.casting != size:
                    self._start_cast(tab, size)
            except Exception:
                tab.viewer_sizes.pop()  # only this registration; existing viewers still own the tab
                tab.viewers = len(tab.viewer_sizes)
                if tab.viewers and previous_cast and tab.casting != previous_cast:
                    try:
                        self._start_cast(tab, previous_cast)
                    except BrowserError:
                        pass  # a detached renderer cannot restore the previous cast
                raise
            return tab

    def unwatch(self, tab, size=None, css_size=None):
        with self.lock:
            if size is not None:
                size = (_clamp(size[0], 160, 2560, 1280), _clamp(size[1], 160, 2560, 1280))
            viewer = (size, tuple(css_size) if css_size is not None else None)
            if tab.viewer_sizes:
                tab.viewer_sizes.remove(viewer if viewer in tab.viewer_sizes else tab.viewer_sizes[-1])
            tab.viewers = len(tab.viewer_sizes)
            self.touch()
            if not tab.viewers and tab.casting and self.running() and tab.target in self.tabs:
                tab.casting = None
                self.conn.send('Page.stopScreencast', session=tab.session)
            if not tab.viewers and self.running() and tab.target in self.tabs:
                timer = threading.Timer(PARK_SECONDS, self._park_later, args=(tab,))
                timer.daemon = True
                timer.start()
            elif tab.viewers and tab.casting and self.running() and tab.target in self.tabs:
                size = self._cast_size(tab)
                if tab.casting != size:
                    self._start_cast(tab, size)

    def _park(self, tab):
        """Minimize the window of a user tab that nobody watches. Headless Chrome treats every tab as visible, so a
        page with endless CSS animations kept the software compositor drawing full 2x frames: two Tether chat tabs
        used 87% CPU, and 7% when minimized. Agent tabs stay visible: Playwright waits on requestAnimationFrame,
        which a hidden page does not run. The headed signed browser paints only its front tab already."""
        if (tab.parked or tab.viewers or tab.mode == 'agent' or self.signed or not self.running()
                or self.tabs.get(tab.target) is not tab):
            return
        try:
            window = self.conn.call('Browser.getWindowForTarget', {'targetId': tab.target})['windowId']
            self.conn.call('Browser.setWindowBounds', {'windowId': window, 'bounds': {'windowState': 'minimized'}})
            tab.parked = True
        except (BrowserError, KeyError, TypeError):
            pass  # a tab that closed meanwhile; a later sweep tries again

    def _park_later(self, tab):
        with self.lock:
            self._park(tab)

    def _unpark(self, tab):
        if not tab.parked:
            return
        try:
            window = self.conn.call('Browser.getWindowForTarget', {'targetId': tab.target})['windowId']
            self.conn.call('Browser.setWindowBounds', {'windowId': window, 'bounds': {'windowState': 'normal'}})
            tab.parked = False
        except (BrowserError, KeyError, TypeError):
            pass  # a minimized tab still sends screencast frames

    def _cast_size(self, tab):
        return tuple(max(s[i] for s, _ in tab.viewer_sizes) for i in (0, 1))

    def _start_cast(self, tab, size):
        if tab.controller == 'agent':
            self._agent_size(tab)
        if tab.casting:  # headed Chrome refuses a second start ("Screencast is already active"); headless allows it
            self.conn.call('Page.stopScreencast', session=tab.session)
            tab.casting = None
        self.conn.call('Page.startScreencast', {'format': 'jpeg', 'quality': QUALITY, 'maxWidth': size[0], 'maxHeight': size[1],
                                                'everyNthFrame': 1, 'maxFramesInFlight': 6}, tab.session)
        tab.casting = size

    def _restart_cast(self, tab):
        if tab.casting:
            self.conn.call('Page.stopScreencast', session=tab.session)
            size, tab.casting = tab.casting, None
            # Flush the resized compositor before streaming. Otherwise Chromium can label a desktop-shaped JPEG
            # with the new phone dimensions, even though the page already lays out at the phone width.
            self.conn.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': QUALITY}, tab.session)
            self._start_cast(tab, size)

    def ack(self, tab, frame):
        """Tell Chromium a frame reached the server. Every frame of one screencast carries the
        same sessionId (it counts screencasts, not frames), so dedupe on our own frame number."""
        seq, session = frame[0], frame[3]
        with tab.changed:
            if seq <= tab.acked or not session:  # session 0: our screenshot, not a screencast frame
                return
            tab.acked = seq
        if self.running():
            self.conn.send('Page.screencastFrameAck', {'sessionId': session}, tab.session)

    def wait_frame(self, tab, after, timeout):
        """The newest frame newer than `after` (seq), or None after the timeout or when the tab closed."""
        deadline = time.monotonic() + timeout
        with tab.changed:
            while (tab.frame is None or tab.frame[0] <= after) and tab.target in self.tabs:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                tab.changed.wait(left)
            return tab.frame if tab.target in self.tabs else None

    def heal(self, tab, frame):
        """True when the frame has the tab's viewport size. Otherwise apply the device again and return False.
        Chromium 147: after a reload or navigation under phone emulation, the screencast captures the whole window
        (1280 wide) while the page still lays out at 412. Applying the preset again restores the emulated capture."""
        if tab.controller == 'agent':
            metadata = frame[1] or {}
            if metadata.get('deviceWidth') and metadata.get('deviceHeight'):
                size = (metadata['deviceWidth'], metadata['deviceHeight'])
                if tab.size != size:
                    tab.size = size
                    self._bump()
            return True
        metadata = frame[1] or {}
        row = tab.row()
        if all(metadata.get(field) and abs(metadata[field] - row[dimension]) <= 1
               for field, dimension in (('deviceWidth', 'width'), ('deviceHeight', 'height'))):
            return True
        now = self.clock()
        if now - tab.healed >= 1.0:
            tab.healed = now
            with self.lock:
                if tab.target in self.tabs:
                    self._apply_preset(tab, tab.preset, *tab.size)
                    self._restart_cast(tab)
        return False

    def first_frame(self, tab, wait=1.0):
        """The current frame for a new viewer. Chromium sometimes misses the first paint after startScreencast and then
        sends nothing until the page changes, so a still page would stay blank: take one screenshot instead."""
        frame = self.wait_frame(tab, 0, wait)
        if frame is not None or tab.target not in self.tabs:
            return frame
        if tab.controller == 'agent':
            self._agent_size(tab)
        return self._snapshot(tab)

    def _snapshot(self, tab):
        seq = tab.seq
        row = tab.row()  # the emulated viewport; layout metrics can report the window size under phone emulation
        shot = self.conn.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': QUALITY}, tab.session).get('data') or ''
        with tab.changed:
            if tab.seq == seq and shot:
                tab.seq += 1
                tab.frame = (tab.seq, {'deviceWidth': row['width'], 'deviceHeight': row['height'], 'offsetTop': 0}, shot, 0)
                tab.changed.notify_all()
            return tab.frame

    def _event(self, method, params, session):
        if self.agent_proxy and not session:
            self.agent_proxy.target_event(method, params)
        tab = self.by_session.get(session) if session else None
        if method == 'Page.screencastFrame' and tab is not None:
            with tab.changed:
                tab.seq += 1
                tab.frame = (tab.seq, params.get('metadata') or {}, params.get('data') or '', params.get('sessionId', 0))
                self.ack(tab, tab.frame)
                tab.changed.notify_all()
        elif method == 'Page.navigatedWithinDocument' and tab is not None:
            self._bump()  # pushState can change Back/Forward without changing the URL or title
        elif method == 'Target.targetInfoChanged':
            info = params.get('targetInfo') or {}
            known = self.tabs.get(info.get('targetId'))
            if known is not None and (known.url, known.title) != (info.get('url'), info.get('title')):
                known.url, known.title = info.get('url') or known.url, info.get('title') or ''
                self._bump()
        elif method == 'Target.targetDestroyed':
            known = self.tabs.get(params.get('targetId'))
            if known is not None:  # never take self.lock on the reader thread: a caller holding it waits on us
                threading.Thread(target=self._forget_locked, args=(known,), daemon=True).start()
        elif method == 'Target.targetCreated':
            info = params.get('targetInfo') or {}
            # A page opened by window.open or target=_blank: show it as a tab.
            if (info.get('type') == 'page' and info.get('openerId') and info['targetId'] not in self.tabs
                    and not (self.agent_proxy and self.agent_proxy.owns_context(info.get('browserContextId')))):
                threading.Thread(target=self._adopt_popup, args=(info,), daemon=True).start()

    def _adopt_popup(self, info):
        with self.lock:
            if self.running() and info['targetId'] not in self.tabs and self._user_tab_count() < MAX_TABS:
                context = info.get('browserContextId')
                private = context and any(tab.context == context for tab in self.tabs.values())
                try:  # a popup shares its opener's cookies, so it keeps the opener's mode
                    self._adopt(info['targetId'], info, 'private' if private else 'saved', context if private else None)
                except BrowserError:
                    pass

    def _user_tab_count(self):
        return sum(tab.mode != 'agent' for tab in self.tabs.values())

    def _agent_size(self, tab):
        metrics = self.conn.call('Page.getLayoutMetrics', session=tab.session)
        viewport = metrics.get('cssVisualViewport') or metrics.get('cssLayoutViewport') or {}
        if viewport.get('clientWidth') and viewport.get('clientHeight'):
            tab.size = (viewport['clientWidth'], viewport['clientHeight'])

    def adopt_agent(self, info, client):
        """Attach a panel session on a worker; the agent retains its own viewport and context."""
        if self.signed:
            return
        with self.lock:
            if (self.running() and not client.closed and info['targetId'] not in self.tabs
                    and info['targetId'] in client.targets):
                try:
                    tab = self._adopt(info['targetId'], info, 'agent', info.get('browserContextId'), {'pane': client.pane})
                    tab.agent_client = client
                except BrowserError:
                    pass


KEY_CODES = {'Enter': 13, 'Backspace': 8, 'Tab': 9, 'Escape': 27, 'Delete': 46, 'ArrowLeft': 37, 'ArrowUp': 38,
             'ArrowRight': 39, 'ArrowDown': 40, 'Home': 36, 'End': 35, 'PageUp': 33, 'PageDown': 34, ' ': 32}
CODE_VK = {
    **KEY_CODES,
    **{'Key' + chr(value): value for value in range(65, 91)},
    **{'Digit' + str(value): 48 + value for value in range(10)},
    **{'Numpad' + str(value): 96 + value for value in range(10)},
    'Space': 32, 'Semicolon': 186, 'Equal': 187, 'Comma': 188, 'Minus': 189, 'Period': 190,
    'Slash': 191, 'Backquote': 192, 'BracketLeft': 219, 'Backslash': 220, 'BracketRight': 221,
    'Quote': 222, 'IntlBackslash': 226, 'NumpadMultiply': 106, 'NumpadAdd': 107, 'NumpadSubtract': 109,
    'NumpadDecimal': 110, 'NumpadDivide': 111, 'NumpadEnter': 13,
}
PUNCTUATION_VK = {char: value for chars, value in (
    (';:', 186), ('=+', 187), (',<', 188), ('-_', 189), ('.>', 190), ('/?', 191), ('`~', 192),
    ('[{', 219), ('\\|', 220), (']}', 221), ("'\"", 222),
) for char in chars}
BUTTONS = {'left': 1, 'right': 2, 'middle': 4, 'none': 0}


def _windows_virtual_key_code(key, code):
    if code in CODE_VK:
        return CODE_VK[code]
    if key in KEY_CODES:
        return KEY_CODES[key]
    if len(key) == 1 and ('a' <= key <= 'z' or 'A' <= key <= 'Z' or '0' <= key <= '9'):
        return ord(key.upper())
    return PUNCTUATION_VK.get(key, 0)


def as_touch(method, params):
    """On a phone or tablet tab, a left press, drag and release is one finger. Hover moves are dropped (a finger
    cannot hover). Other buttons, wheel and keys stay as they are."""
    if method != 'Input.dispatchMouseEvent' or params['type'] == 'mouseWheel' or params.get('button') not in ('left', 'none'):
        return method, params
    kind = params['type']
    if kind == 'mouseMoved' and not params.get('buttons', 0) & 1:
        return None, None
    point = {'x': params['x'], 'y': params['y']}
    return 'Input.dispatchTouchEvent', {'type': {'mousePressed': 'touchStart', 'mouseMoved': 'touchMove', 'mouseReleased': 'touchEnd'}[kind],
                                        'touchPoints': [] if kind == 'mouseReleased' else [point], 'modifiers': params['modifiers']}


def input_command(event):
    """One panel event as a CDP Input command (method, params). Raises ValueError on bad input."""
    if not isinstance(event, dict):
        raise ValueError('Invalid input.')
    kind = event.get('type')
    modifiers = _clamp(event.get('modifiers'), 0, 15, 0)
    if kind in ('down', 'up', 'move'):
        button = event.get('button') if event.get('button') in BUTTONS else ('left' if kind != 'move' else 'none')
        params = {'type': {'down': 'mousePressed', 'up': 'mouseReleased', 'move': 'mouseMoved'}[kind],
                  'x': _number(event.get('x')), 'y': _number(event.get('y')), 'modifiers': modifiers, 'button': button,
                  'buttons': _clamp(event.get('buttons'), 0, 31, BUTTONS[button] if kind == 'down' else 0)}
        if kind != 'move':
            params['clickCount'] = _clamp(event.get('clicks'), 1, 3, 1)
        return 'Input.dispatchMouseEvent', params
    if kind == 'wheel':
        return 'Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': _number(event.get('x')), 'y': _number(event.get('y')),
                                            'deltaX': _number(event.get('dx'), 10000), 'deltaY': _number(event.get('dy'), 10000),
                                            'modifiers': modifiers}
    if kind in ('keydown', 'keyup'):
        key = str(event.get('key') or '')[:32]
        code = str(event.get('code') or '')[:32]
        if not key:
            raise ValueError('Invalid key.')
        text = key if len(key) == 1 and not modifiers & 6 else ('\r' if key == 'Enter' and kind == 'keydown' else '')
        params = {'type': 'keyDown' if text and kind == 'keydown' else ('rawKeyDown' if kind == 'keydown' else 'keyUp'),
                  'key': key, 'code': code, 'modifiers': modifiers,
                  'windowsVirtualKeyCode': _windows_virtual_key_code(key, code)}
        if text and kind == 'keydown':
            params['text'] = params['unmodifiedText'] = text
        commands = [command for command in event.get('commands') or [] if command in ('selectAll', 'copy', 'cut', 'paste', 'undo', 'redo')]
        if commands and kind == 'keydown':
            params['commands'] = commands
        return 'Input.dispatchKeyEvent', params
    if kind == 'text':
        text = event.get('text')
        if not isinstance(text, str) or not text or len(text) > 20000:
            raise ValueError('Invalid text.')
        return 'Input.insertText', {'text': text}
    raise ValueError('Invalid input.')


def _number(value, limit=100000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError('Invalid coordinate.')
    return max(-limit, min(limit, float(value)))


def _clamp(value, low, high, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(max(low, min(high, value)))


def _reap(pid):
    """Stop the process group this module started, politely, then by force."""
    for sig, wait in ((None, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        if sig is not None:
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done = pid
            if done:
                try:
                    os.killpg(pid, signal.SIGKILL)  # helpers left in the group after the browser exited
                except (ProcessLookupError, PermissionError):
                    pass
                return
            time.sleep(.05)


def frame_event(frame, browser_id='test'):
    """One SSE 'frame' message: metadata plus the JPEG as base64 (the pipe already carries it that way)."""
    browser_id = 'chrome' if browser_id == 'mac' else browser_id
    seq, metadata, data, _ = frame
    head = json.dumps({'seq': seq, 'width': metadata.get('deviceWidth'), 'height': metadata.get('deviceHeight'),
                       'top': metadata.get('offsetTop', 0),
                       **{key: metadata[key] for key in ('scrollOffsetX', 'scrollOffsetY', 'pageScaleFactor') if key in metadata},
                       **({'browser': browser_id} if browser_id in ('signed', 'chrome') else {})})
    return ('event: frame\ndata: ' + head[:-1] + ',"jpeg":"' + data + '"}\n\n').encode()


BROWSER = Browser()
SIGNED_BROWSER = Browser(chrome_finder=find_signed_chrome, profile=SIGNED_PROFILE, signed=True)


def stop_all():
    SIGNED_BROWSER.stop()
    BROWSER.stop()
