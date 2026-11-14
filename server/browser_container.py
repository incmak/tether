"""The panel browser inside a Selkies container (lscr.io/linuxserver/chromium).

Selkies streams the real Chromium window to the panel as H.264 (sharp text, 30 fps on this box). Tether keeps its
own tab bar and address bar and drives Chromium over the DevTools pipe, which the container serves on a Unix socket
(selkies/cdp_pipe.py). The Selkies web client reaches the panel only through Tether's sign-in: the container
publishes no port, and Tether relays /browser/live/ to a second Unix socket (selkies/sock_relay.py).

Security (B79):
- Chromium runs with its sandbox on. The seccomp file is Docker's default profile plus clone/unshare/setns, which the
  namespace sandbox needs; the container keeps Docker's default capabilities (no SYS_ADMIN).
- HARDEN_DESKTOP turns off sudo, terminals, xdg-open, file transfer and Selkies commands in the container.
- The container's nginx listens on the Docker bridge. A random password per start (basic auth) keeps other local
  users and containers out; only Tether's relay sends it.
- The container sends the live view's page, and a process in it can replace the relay. So Tether serves the live
  view on its own port (LIVE_PORT), never on its main origin: a hostile page there cannot read Tether's pages or call
  its APIs (they check Origin). Tether strips cookies and identity headers before it forwards a request, drops
  Set-Cookie and Clear-Site-Data from the answer (cookies span all ports), and allows only its main origin to frame
  the view. The panel's iframe is sandboxed: no top navigation, no popups.
- The socket folder is mode 700 and owned by Tether's user (PUID/PGID). Anyone in the docker group can reach the
  container anyway, so the socket file adds no new access for them.
"""
import base64
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import paths

IMAGE = 'lscr.io/linuxserver/chromium@sha256:ff1b8235e20367c3b660d31892e14428687df75e2d62e274b7d8d40cf5537108'
FILES = Path(__file__).resolve().parent / 'selkies'
START_TIMEOUT = 90  # first start pulls nothing (pinned image is local) but Selkies and Chromium take 10-20 s
LIVE_PREFIX = '/browser/live/'
# Tether serves the live view on this port only (server.BrowserLiveOriginHandler), so it has its own origin. Behind
# tailscale serve, publish it too: tailscale serve --bg --https=7698 http://127.0.0.1:7698
LIVE_PORT = int(os.environ.get('MOBILE_TMUX_BROWSER_LIVE_PORT', '7698'))


class ContainerError(RuntimeError):
    pass


def docker_path():
    return shutil.which('docker')


def _run(args, timeout=60):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


class Container:
    """One Selkies container for one panel browser. Thread-safe; Browser calls launch() and stop()."""

    def __init__(self, name, *, persistent=False, root=None, runner=_run, image=IMAGE, files=FILES):
        self.name = 'tether-selkies-' + name
        self.root = Path(root) if root is not None else paths.CACHE / 'selkies' / name
        self.persistent = persistent
        self.runner, self.image, self.files = runner, image, files
        self.lock = threading.RLock()
        self.password = None
        self.run_id = None  # new for each docker run; the panel reloads its live view when it changes

    # paths ------------------------------------------------------------------------------------------------------
    @property
    def sock_dir(self):
        return self.root / 'sock'

    @property
    def cdp_sock(self):
        return self.sock_dir / 'cdp.sock'

    @property
    def web_sock(self):
        return self.sock_dir / 'web.sock'

    def available(self):
        return bool(docker_path()) and self.files.is_dir()

    # life cycle -------------------------------------------------------------------------------------------------
    def running(self):
        result = self.runner(['docker', 'inspect', '-f', '{{.State.Running}}', self.name], timeout=15)
        return result.returncode == 0 and result.stdout.strip() == 'true'

    def run_args(self):
        """The docker run command. No published port; the sockets are the only way in."""
        uid, gid = os.getuid(), os.getgid()
        args = ['docker', 'run', '-d', '--rm', '--name', self.name,
                '--security-opt', 'seccomp=' + str(self.files / 'seccomp-chromium.json'),
                '--security-opt', 'no-new-privileges=true',
                '--shm-size=1g', '--pids-limit=2048', '--memory=4g',
                '-e', 'PUID=%d' % uid, '-e', 'PGID=%d' % gid, '-e', 'TZ=' + (os.environ.get('TZ') or 'Etc/UTC'),
                '-e', 'CUSTOM_USER=tether', '-e', 'PASSWORD=' + self.password,
                '-e', 'SUBFOLDER=' + LIVE_PREFIX,  # Tether sets frame-ancestors itself (server.live_head)
                '-e', 'HARDEN_DESKTOP=true', '-e', 'RESTART_APP=true',
                '-e', 'SELKIES_UI_SHOW_SIDEBAR=false', '-e', 'SELKIES_UI_SHOW_CORE_BUTTONS=false',
                '-e', 'SELKIES_AUDIO_ENABLED=false', '-e', 'SELKIES_MICROPHONE_ENABLED=false',
                '-e', 'SELKIES_WEBCAM_ENABLED=false', '-e', 'NO_GAMEPAD=true', '-e', 'SELKIES_SECOND_SCREEN=false',
                '-e', 'SELKIES_ENABLE_BINARY_CLIPBOARD=false',
                '-v', '%s:/tether:ro' % self.files,
                '-v', '%s:/custom-services.d:ro' % (self.files / 'services'),
                '-v', '%s:/usr/bin/wrapped-chromium:ro' % (self.files / 'wrapped-chromium'),
                '-v', '%s:/sock' % self.sock_dir]
        if self.persistent:
            args += ['-v', '%s:/config' % (self.root / 'config')]
        return args + [self.image]

    def start(self):
        with self.lock:
            if not docker_path():
                raise ContainerError('Docker is not installed. The panel browser needs it.')
            for folder in [self.root, self.sock_dir] + ([self.root / 'config'] if self.persistent else []):
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                folder.chmod(0o700)
            if self.password and self.running():
                return
            self._remove()
            for stale in (self.cdp_sock, self.web_sock):
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass
            self.password = secrets.token_urlsafe(24)
            self.run_id = secrets.token_hex(6)
            result = self.runner(self.run_args(), timeout=120)
            if result.returncode != 0:
                self.password = None
                raise ContainerError('The browser container did not start: %s' % (result.stderr.strip()[-300:] or 'docker run failed'))

    def _remove(self):
        result = self.runner(['docker', 'rm', '-f', self.name], timeout=30)
        return result.returncode == 0

    def stop(self):
        with self.lock:
            self.password = None
            self.runner(['docker', 'stop', '-t', '5', self.name], timeout=30)

    def connect(self, path, timeout):
        """A connected Unix socket to `path`, waiting for the container to create it."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(str(path))
                return client
            except OSError as exc:
                client.close()
                last = exc
            time.sleep(0.25)
        raise ContainerError('The browser container did not answer (%s).' % (last or 'no socket'))

    def launch(self, *_args, **_kwargs):
        """Browser's launcher hook: returns (pid, read_fd, write_fd). There is no local pid; stop() ends the container."""
        self.start()
        try:
            client = self.connect(self.cdp_sock, START_TIMEOUT)
        except ContainerError:
            self.stop()  # the watchdog only restarts Chromium after it saw it once; a fresh container tries again
            raise
        read_fd = client.detach()
        return None, read_fd, os.dup(read_fd)

    def web_auth(self):
        with self.lock:
            if not self.password:
                return None
            return 'Basic ' + base64.b64encode(('tether:' + self.password).encode()).decode()


def default(name):
    """The container for a panel browser, or None to keep the local Chromium: no Docker, the image is not pulled, or
    MOBILE_TMUX_BROWSER_SELKIES=0."""
    if os.environ.get('MOBILE_TMUX_BROWSER_SELKIES', '1') == '0' or not docker_path() or not FILES.is_dir():
        return None
    try:
        result = _run(['docker', 'image', 'inspect', '-f', '{{.Id}}', IMAGE], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return Container(name) if result.returncode == 0 else None
