#!/usr/bin/env python3
"""A test Tether for UI checks: the real request handler over fixture data, on a free loopback port.

    python3 server/dev/verify_server.py --dist BUILD_DIR

The launcher usually owns the fixture root and private tmux. With --root, the test
watcher owns them. The child serves the real handler behind the route and program
allowlists and the private tmux wrapper.
The audit hook is a safety net for Tether's own code. It provides no sandbox for
hostile code in the server process (https://docs.python.org/3.12/library/sys.html#sys.addaudithook).
Production workers do not start.
"""
import argparse
import fcntl
from hmac import compare_digest
import json
import os
from pathlib import Path
import pwd
import secrets
import select
import shutil
import signal
import site
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from weakref import WeakValueDictionary

SERVER = Path(__file__).resolve().parents[1]
TEMP = Path('/tmp' if Path('/tmp').is_dir() else tempfile.gettempdir()).resolve()
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
MARKER = '.verify-root'
KEEP = ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TERM', 'USER', 'LOGNAME')
SESSION = '11111111-2222-4333-8444-555555555555'
CHAT_LINE = 'The mock conversation stays with this test.'
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
OFF = ('Off in the test Tether. To allow it, add the route to ALLOWED in '
       'server/dev/verify_server.py after you check that it cannot reach the host.')

ALLOWED = {
    'GET': {
        # Fixture panes, transcripts, captured screen, reply ratings and tab details.
        'sessions', 'chat', 'footer', 'screen', 'feedback', 'tab-brief', 'tmux-sessions',
        # Every chat page asks for these. They read the fixture transcript and home.
        # The catalog's `claude -p` probe is refused by the program allowlist; the catalog then lists files only.
        'subagents', 'catalog',
        # Sidebar and workspace state from the fixture stores. Usage is cached-only.
        'work/board', 'projects/marks', 'projects/project', 'projects/backlog', 'undo', 'autonomy/review', 'usage',
        # Settings and local capability summaries. Update checks are seeded off; the browser never starts.
        'update', 'prefs', 'layouts', 'app-identity', 'push/config', 'shortcuts',
        'browser/agents', 'browser/signed/access', 'ui-usage',
    },
    # Edits to the fixture's preferences, backlog, Factory history and UI usage log.
    'POST': {'prefs', 'projects/backlog', 'factory/history', 'ui-usage'},
}
def factory_fixture(_pane):
    import factory_history
    return {'now': int(time.time()), 'tools': {}, 'deployed': [], 'commits': [], 'history': factory_history.history()}


FIXTURES = {
    'codex-runs': lambda pane: {'pane': pane, 'running': 0, 'runs': []},
    'host': lambda _pane: {'name': 'Fixture', 'resources': False, 'terminal': False, 'office': False},
    'reminders': lambda _pane: {'reminders': [], 'deleted': [], 'now': time.time()},
    # No task app is connected in the fixture. A recipe stubs the listing to show tasks.
    'tasks': lambda _pane: {'sources': [], 'tasks': []},
    'factory': factory_fixture,
}
# The UI calls these routes, but the fixture server leaves them off. This list
# documents the decision for the source scan. The request gate stays default-deny.
# {} marks an ID in a reviewed path shape. It never grants runtime access.
REFUSED = {
    'GET': {
        # These inspect host services, credentials, processes, or live account data.
        'accounts', 'browser/extension/devices', 'browser/extension/download',
        'browser/live', 'browser/snapshot', 'browser/stream', 'browser/tabs',
        'dev-preview', 'foundry-spend', 'friday-notes', 'host-stats', 'live/agent', 'live/brief',
        'login/state', 'passkeys', 'upkeep/ops', 'upkeep/ops/note', 'upkeep/prune', 'usage?live=1',
        # These browse or expose files outside the fixture chat transcript.
        'file', 'files', 'files/list', 'files/places', 'files/preview', 'files/raw',
        'files/search', 'files/size', 'files/stat', 'files/zip', 'office/copy',
        'repo/changes', 'repo/diff', 'repo/tree', 'worktree-info',
        # The fixture has no Share store, generated assets, or attached media.
        'app-identity/generate', 'app-identity/preview/art.png', 'app-identity/preview/draft.png',
        'backgrounds', 'backgrounds/generate', 'icon/{}.svg', 'layouts/generate', 'message-image',
        'share/list', 'share/raw/{}', 'share/thumb/{}', 'share/uploads', 'share/uploads/{}', 'tool-image',
        # Optional panels and detail views have no fixture contract. The verify
        # browser relay answers the chat stream with 204 so the UI polls instead.
        'activity', 'activity/export', 'attention', 'attention/log', 'audit', 'auto-merge', 'autonomy', 'autonomy/setup',
        'browser/bookmarks', 'claude-advisor', 'codex-level', 'codex-provider', 'commit-gate', 'groups/context',
        'history', 'history/hidden', 'live', 'projects/advisor-entry', 'projects/advisor-log',
        'projects/note', 'search', 'side-shells', 'stream', 'subagent', 'subagent-task', 'subagent-tool', 'tool',
        'task-tree/close-plan',
    },
    # These can close a pane or session.
    'DELETE': {'session', 'tmux-sessions'},
    # WebRTC signaling connects to the live voice worker.
    'PATCH': {'live/offer'},
    'POST': {
        # These can type into, start, stop, or reconfigure a terminal or agent.
        'approval', 'ask-answer', 'ask-engage', 'attention/back', 'attention/digest', 'attention/handled',
        'attention/later', 'attention/opened', 'attention/rules', 'attention/snooze', 'auto-resume', 'broadcast', 'clear-draft',
        'continue', 'continue-preview', 'coordinator-role', 'delegate', 'dialog', 'fork', 'groups/close', 'key',
        'live/summary',
        'menu', 'merge', 'message', 'mode', 'model', 'model-options', 'pane-icon', 'pane-icon/draw',
        'pane-name', 'pick', 'projects/backlog-start', 'prompt-action', 'quick-tab/move',
        'quick-tab/route', 'quick-tab/start', 'rename', 'restart-tab', 'resume', 'review-link', 'review-stop',
        'side-shell', 'side-shell/end', 'split', 'start', 'stop-task', 'subagent-stop',
        'suggest-groups', 'suggest-name', 'tab-brief', 'task-models', 'tmux-sessions', 'type', 'unqueue',
        'task-tree/answer', 'task-tree/close', 'task-tree/done', 'task-tree/move',
        # These access live browsers, host services, login flows, or external workers.
        'accounts/refresh', 'accounts/switch', 'browser/ack', 'browser/agents', 'browser/agents/setup',
        'browser/ask/dismiss', 'browser/close', 'browser/device', 'browser/extension/pair',
        'browser/extension/revoke', 'browser/history', 'browser/input', 'browser/navigate',
        'browser/new', 'browser/show', 'browser/signed/allow', 'browser/signed/deny',
        'browser/signed/dismiss', 'browser/signed/end', 'browser/signed/policy', 'browser/window-view',
        'dev-preview', 'dev-preview/release', 'dev-preview/stop', 'live/offer', 'login/cancel',
        'login/code', 'login/start', 'preview', 'push/level', 'push/settings', 'push/subscribe',
        'push/test', 'push/unsubscribe', 'transcribe', 'tts', 'update', 'upkeep/ops', 'upkeep/prune',
        # These act on files, uploads, or generated assets with no fixture.
        'app-identity', 'app-identity/generate', 'app-identity/upload', 'backgrounds/delete',
        'backgrounds/generate', 'backgrounds/upload', 'file-check', 'files/copy', 'files/delete',
        'files/duplicate', 'files/mkdir', 'files/move', 'files/rename', 'files/save', 'files/secrets',
        'files/upload', 'layouts/delete', 'layouts/generate', 'layouts/save', 'office/open',
        'share/delete', 'share/rename', 'share/restore', 'share/thumb/{}', 'share/to-chat',
        'share/upload', 'share/uploads', 'share/uploads/{}', 'share/uploads/{}/cancel', 'upload',
        # These change state outside the small set of fixture writes in ALLOWED.
        'activity', 'activity/publish', 'auto-merge', 'autonomy', 'autonomy/decisions', 'autonomy/lessons',
        'autonomy/review', 'autonomy/setup', 'browser/bookmarks/add', 'browser/bookmarks/remove',
        'claude-advisor', 'codex-level', 'codex-provider',
        'codex-trust', 'commit-gate', 'feedback', 'friday-notes/hold', 'friday-notes/drop',
        'friday-notes/draft', 'friday-notes/draft-edit', 'friday-notes/draft-send',
        'friday-notes/draft-close', 'friday-notes/draft-asked',
        'groups/arrange', 'groups/create', 'groups/move',
        'groups/remove', 'groups/rename', 'groups/reorder', 'groups/ungroup', 'history/hide', 'passkeys',
        'projects/backlog-add', 'projects/budget', 'projects/instructions', 'projects/note',
        'projects/note-delete', 'projects/pin', 'projects/seen', 'projects/unpin',
        'reminders/add', 'reminders/delete', 'reminders/done', 'reminders/edit', 'reminders/reopen',
        'reminders/restore', 'reminders/snooze', 'tasks/backlog', 'tasks/settings', 'tasks/sync',
        'ui-usage/clear', 'undo', 'voice-log',
        'work/answer', 'work/fix', 'work/item', 'work/jira', 'work/project', 'work/qa', 'work/stage', 'work/triage',
    },
}
STATIC_PREFIX = '/chat/'
API_PREFIX = '/mobile/api/'


def allowed_route(method, target):
    path = target.partition('?')[0]
    if method in ('GET', 'HEAD') and path.startswith(STATIC_PREFIX):
        return True
    if not path.startswith(API_PREFIX) or path[len(API_PREFIX):] not in ALLOWED.get(method, ()):
        return False
    return path != API_PREFIX + 'usage' or '1' not in parse_qs(urlsplit(target).query).get('live', [])


def fixture_handler(base, root, pane):
    class VerifyHandler(base):
        def parse_request(self):
            # BaseHTTPRequestHandler dispatches every method only after this returns true.
            if not super().parse_request():
                return False
            path = self.path.partition('?')[0]
            if self.command == 'GET' and path.startswith(API_PREFIX):
                fixture = FIXTURES.get(path[len(API_PREFIX):])
                if fixture is not None:
                    if not self.deny():
                        self.respond(200, fixture(pane))
                    return False
            if allowed_route(self.command, self.path):
                return True
            with (root / 'blocked.log').open('a') as log:
                log.write(f'route {self.command} {path}\n')
            self.close_connection = True
            data = json.dumps({'error': OFF}).encode()
            self.send_response(403)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Tether-Verify-Blocked', '1')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)
            return False

        def do_HEAD(self):
            output, end_headers = self.wfile, self.end_headers

            def headers_only():
                end_headers()
                self.wfile = SimpleNamespace(write=lambda _data: None)

            self.end_headers = headers_only
            try:
                self.serve_chat_ui(self.path.partition('?')[0])
            finally:
                self.wfile = output
                del self.end_headers

    return VerifyHandler


def clean_env(root, dist, base=None):
    """Pass only platform settings and fixture paths to the child."""
    base = os.environ if base is None else base
    env = {name: base[name] for name in KEEP if name in base}
    env.setdefault('TERM', 'xterm-256color')
    root = Path(root)
    home = str(root / 'home')
    env.update(HOME=home, MOBILE_TMUX_HOME=home, TMPDIR=str(root / 'tmp'), SHELL='/bin/sh',
               PATH=str(root / 'bin') + os.pathsep + env.get('PATH', os.defpath),
               MOBILE_TMUX_TMUX=str(root / 'bin/tmux'), MOBILE_TMUX_TTYD_PORT='0',
               TMUX_CHAT_DIST=str(dist), MOBILE_TMUX_AUTH='token', MOBILE_TMUX_TAILSCALE='0',
               PYTHONDONTWRITEBYTECODE='1')
    return env


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def check_root_location(root, option):
    if (root == REAL_HOME or root in REAL_HOME.parents or root.parent != TEMP
            or not root.name.startswith('tv-') or len(str(root)) > 60):
        raise SystemExit(f"verify_server: {option} needs the launcher's private temporary root.")


def check_root(root, marker):
    root = Path(root).resolve()
    check_root_location(root, '--serve')
    saved = root / MARKER
    if not marker or saved.is_symlink() or not saved.is_file() or not compare_digest(saved.read_bytes(), marker.encode()):
        raise SystemExit("verify_server: --serve needs the launcher's matching root marker.")
    return root


def check_supplied_root(root):
    root = Path(root)
    check_root_location(root, '--root')
    try:
        info = root.lstat()
        if (stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                and info.st_uid == os.getuid()):
            # The watcher's foreground server binds its socket before the launcher starts.
            entries = {entry.name for entry in root.iterdir()}
            if not entries:
                return root
            if entries == {'tmux.sock'}:
                socket_info = (root / 'tmux.sock').lstat()
                if stat.S_ISSOCK(socket_info.st_mode) and socket_info.st_uid == info.st_uid:
                    return root
    except OSError:
        pass
    raise SystemExit('verify_server: --root needs a private 0700 directory owned by this user, '
                     'empty except for the prestarted tmux socket.')


def isolation_errors(root, env=None):
    """Check the locations before loading the application or using the wrapper."""
    env = os.environ if env is None else env
    sys.path.insert(0, str(SERVER))
    import paths
    errors = [f'{name} is set' for name in ('TMUX', 'TMUX_PANE', 'TMUX_TMPDIR', 'XDG_RUNTIME_DIR',
                                           'DBUS_SESSION_BUS_ADDRESS') if env.get(name)]
    places = {name: getattr(paths, name) for name in ('HOME', 'CACHE', 'TETHER', 'SHARE', 'CONFIG', 'ATTENTION', 'LAYOUTS',
                                                      'BACKGROUNDS', 'CLAUDE', 'CODEX', 'CODEX_HOOKS', 'ANTIGRAVITY')}
    places['Path.home()'] = Path.home()
    places['TMPDIR'] = env.get('TMPDIR') or '/'
    errors += [f'{name} = {place} is outside {root}' for name, place in places.items() if not inside(place, root)]
    wrapper = root / 'bin/tmux'
    if (env.get('MOBILE_TMUX_TMUX') != str(wrapper) or paths.TMUX != str(wrapper)
            or wrapper.resolve() != wrapper or shutil.which('tmux', path=env.get('PATH', '')) != str(wrapper)):
        errors.append('MOBILE_TMUX_TMUX and PATH must select the fixture wrapper')
    return errors


# Each allowed operation must remain read-only even with options or an expanded tmux format.
TMUX_WRAPPER = r"""
import os
import re
import sys

FLAGS = {
    'list-panes': ('as', 'Fft', ()),
    'list-windows': ('a', 'Fft', ()),
    'list-sessions': ('', 'Ff', ()),
    'list-clients': ('', 'Ft', ()),
    'display-message': ('p', 'tF', ('-p',)),
    'capture-pane': ('aCeJpPqN', 'tSE', ('-p',)),
    'show-options': ('Agpqsvw', 't', ()),
}

def allowed(args):
    if not args or any('#(' in arg or arg.endswith(';') or
                       arg.startswith(('-S', '-L')) and not (args[0] == 'capture-pane' and arg == '-S')
                       for arg in args):
        return False
    command, *options = args
    if command == 'set-option':
        # Discovery publishes agent pane IDs. Opening Project may assign its fixture session an ID.
        if options[:2] == ['-g', '@tether_agent_panes'] and len(options) == 3:
            return re.fullmatch(r'(?:%\d+(?:,%\d+)*)?', options[-1]) is not None
        return (len(options) == 4 and options[0] == '-t' and re.fullmatch(r'\$\d+', options[1])
                and options[2] == '@project' and re.fullmatch(r'[0-9a-f]{12}', options[3]))
    if command not in FLAGS:
        return False
    switches, values, required = FLAGS[command]
    remaining = iter(options)
    seen = set()
    positional = []
    for option in remaining:
        if len(option) == 2 and option[0] == '-' and option[1] in values:
            value = next(remaining, None)
            if value is None or (option in ('-S', '-E') and not re.fullmatch(r'-?\d+|-', value)):
                return False
        elif option.startswith('-') and len(option) > 1 and all(flag in switches for flag in option[1:]):
            seen.update('-' + flag for flag in option[1:])
        elif not option.startswith('-'):
            positional.append(option)
        else:
            return False
    return set(required) <= seen and len(positional) <= int(command in ('display-message', 'show-options'))

args = sys.argv[1:]
if not allowed(args):
    command = args[0] if args and re.fullmatch(r'[a-zA-Z0-9_-]+', args[0]) else '(invalid command)'
    with open(LOG, 'a') as log:
        log.write(f'tmux {command}\n')
    sys.exit(f'verify_server: tmux wrapper refused {command}; only fixture reads and named metadata writes are allowed')
os.execv(REAL_TMUX, [REAL_TMUX, *CLIENT_OPTIONS, '-S', SOCKET, '-f', '/dev/null', *args])
"""


def write_wrapper(root, tmux, *, client_options=()):
    wrapper = root / 'bin/tmux'
    wrapper.parent.mkdir()
    # A shell trampoline also works when the Python install path has spaces (including on macOS).
    import shlex
    source = (f'REAL_TMUX = {tmux!r}\nCLIENT_OPTIONS = {client_options!r}\nSOCKET = {str(root / "tmux.sock")!r}\n'
              f'LOG = {str(root / "blocked.log")!r}\n' + TMUX_WRAPPER)
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -B -c {shlex.quote(source)} "$@"\n')
    wrapper.chmod(0o700)


def guard(root, dist):
    """Install CPython audit checks before importing the server."""
    root = Path(root).resolve()
    private = (REAL_HOME, '/tmp', '/var/tmp', '/private/tmp', '/private/var/folders', '/run/user')
    restricted = {Path(folder) for folder in private} | {Path(os.path.realpath(folder)) for folder in private}
    readable = tuple(Path(os.path.realpath(folder)) for folder in
                     (root, SERVER.parent, dist, sys.prefix, sys.base_prefix, sys.exec_prefix,
                      sys.base_exec_prefix, *site.getsitepackages(), site.getusersitepackages()))
    # Fixture discovery reads PIDs, parent PIDs and names via ps -axo with ucomm on macOS
    # and comm on Linux. It reads no arguments or environment and keeps only the fixture pane tree.
    # The wrapper pins tmux to one socket.
    wrapper = str(root / 'bin/tmux')
    ps = shutil.which('ps', path=os.defpath)
    programs = {'tmux': wrapper, wrapper: wrapper}
    if ps:
        resolved_ps = os.path.realpath(ps)
        programs.update({'ps': resolved_ps, ps: resolved_ps, resolved_ps: resolved_ps})

    children = WeakValueDictionary()

    class OwnedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.verify_group = kwargs.get('start_new_session') or kwargs.get('process_group') == 0
            children[self.pid] = self

    def record(name):
        return (root / name).open('a', encoding='utf-8', buffering=1)

    leaks, blocked = record('leaks.log'), record('blocked.log')

    def refuse(what, log=leaks):
        log.write(what.replace('\n', r'\n').replace('\r', r'\r') + '\n')
        raise PermissionError('verify_server: refused ' + what)

    def directory(fd):
        try:
            opened = os.fstat(fd)
            if stat.S_ISDIR(opened.st_mode):
                if sys.platform == 'darwin':
                    folder = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0])
                else:
                    folder = os.readlink(f'/proc/self/fd/{fd}')
                if os.path.isabs(folder) and os.path.samestat(opened, os.stat(folder)):
                    return folder
        except (AttributeError, OSError, OverflowError):
            pass
        refuse(f'unresolved directory descriptor {fd}')

    original_open = os.open
    open_context = threading.local()

    def checked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return original_open(path, flags, mode)
        name = os.fspath(path)
        folder = directory(dir_fd)
        # Keep the real descriptor-relative open; the audit event itself omits dir_fd.
        open_context.target = os.path.join(os.fsencode(folder) if isinstance(name, bytes) else folder, name)
        try:
            return original_open(name, flags, mode, dir_fd=dir_fd)
        finally:
            del open_context.target

    def path_of(target):
        if target is None:
            target = '.'
        if isinstance(target, (str, bytes, os.PathLike)):
            return Path(os.path.realpath(os.fsdecode(target)))
        return None  # an already open file descriptor

    def among(path, folders):
        return any(path == folder or folder in path.parents for folder in folders)

    def read(target):
        path = path_of(target)
        if path is not None and not among(path, readable):
            shown = os.fsdecode(target or '.')
            if among(path, restricted) or among(Path(os.path.abspath(shown)), restricted):
                refuse(f'read {shown}')

    def write(target, event, dir_fd=None):
        path = path_of(target)
        if event == 'write' and dir_fd is None and path == Path(os.devnull):
            return  # subprocess.DEVNULL: a write that keeps nothing
        if dir_fd not in (None, -1) or (path is not None and not among(path, (root,))):
            refuse(f'{event} {target}')

    def program(executable, env=None, cwd=None):
        if not isinstance(executable, (str, bytes, os.PathLike)):
            refuse('run (non-path executable)', blocked)
        name = os.fsdecode(executable)
        expected = programs.get(name)
        if expected and (cwd is None or among(path_of(cwd), readable)):
            search = (os.environ if env is None else env).get('PATH', os.defpath)
            found = shutil.which(name, path=search)
            # Keep the fixture environment and match the executable, including symlinks in system paths.
            if found and os.path.realpath(found) == expected and (env is None or env == os.environ):
                return
        refuse(f'run {os.path.basename(name)}', blocked)

    writes = {'os.remove', 'os.rmdir', 'os.mkdir', 'os.truncate', 'os.chmod', 'os.chown',
              'os.chflags', 'os.utime', 'os.setxattr', 'os.removexattr', 'shutil.rmtree',
              'shutil.chown', 'shutil.copymode', 'shutil.copystat'}
    dir_fds = {'os.remove': 1, 'os.rmdir': 1, 'os.mkdir': 2, 'os.chmod': 2, 'os.chown': 3,
               'os.utime': 3, 'shutil.rmtree': 1}
    outbound = {'socket.connect', 'socket.sendto', 'socket.sendmsg', 'socket.getaddrinfo',
                'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'}

    def hook(event, args):
        if event == 'open':
            target, _, flags = args
            target = getattr(open_context, 'target', target)
            if flags & WRITE_FLAGS:
                write(target, 'write')
            else:
                read(target)
        elif event in ('os.listdir', 'os.scandir'):
            target = args[0]
            read(directory(target) if isinstance(target, int) else target)
        elif event in writes:
            target, index = (args[1], None) if event in ('shutil.copymode', 'shutil.copystat') else (args[0], dir_fds.get(event))
            write(target, event, args[index] if index is not None else None)
        elif event in ('os.rename', 'os.link', 'os.symlink', 'shutil.move', 'shutil.copyfile'):
            write(args[1], event, args[-1] if event.startswith('os.') else None)
            if event in ('os.rename', 'os.link', 'shutil.move'):
                write(args[0], event, args[2] if event.startswith('os.') else None)
        elif event == 'subprocess.Popen':
            executable, _, cwd, env = args
            program(executable, env, cwd)
        elif event in ('os.exec', 'os.posix_spawn', 'os.system', 'os.fork', 'os.forkpty', 'os.spawn'):
            refuse(f'run {event}', blocked)
        elif event in outbound:
            refuse(event, blocked)
        elif event in ('os.kill', 'os.killpg'):
            pid, _ = args
            child = children.get(pid) if pid > 0 else None
            # Popen holds its wait lock across waitpid and stores the result before releasing it.
            if child is None or child.poll() is not None or (event == 'os.killpg' and not child.verify_group):
                refuse(f'{event} {pid}', blocked)

    os.open = checked_open
    if hasattr(subprocess, '_USE_POSIX_SPAWN'):
        subprocess._USE_POSIX_SPAWN = False
    subprocess.Popen = OwnedPopen
    sys.addaudithook(hook)


REVIEW_COMMIT = '3f9c2a7b41d6e8f05a1c9b2d7e4f6a8c0b1d2e3f'


def seed(root, pane, pane_pid, session):
    """Fixture data, written with the server's own modules: one Claude chat on the fixture pane and a backlog."""
    import backlog
    import factory_history
    import injected_prompts
    import paths
    work = str(Path(root) / 'work')
    registry = paths.CLAUDE / 'sessions' / f'{pane_pid}.json'
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({'pid': pane_pid, 'sessionId': SESSION, 'cwd': work, 'kind': 'interactive', 'entrypoint': 'cli',
                                    'version': 'fixture', 'tmux': f'Fixture:@0.{pane}', 'status': 'idle',
                                    'name': 'Fixture chat', 'nameSource': 'user', 'nameSince': 1}))
    transcript = paths.CLAUDE / 'projects' / ''.join(c if c.isalnum() else '-' for c in work) / f'{SESSION}.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    # A review request as server/review_links.py sends it to a reviewer tab (the chat-review surface).
    review = ("[Tether review] The Codex tab \u201cFixture worker\u201d (%7) finished a turn. "
              "Review its work: check the changes and the claims, then list what is wrong or missing. "
              "Start your reply with APPROVED if nothing is. Tether sends your reply to that tab. Review id: 5e1f0c2a. "
              f"Commit under review: {REVIEW_COMMIT} in /work/tether. "
              "Read the review rules first: `git -C /work/tether show origin/main:REVIEW.md`. "
              f"An approval counts only for this commit: start it with `APPROVED {REVIEW_COMMIT}` (the full hash). "
              "This diff changes the UI (1 file(s), for example ui/src/Messages.tsx). Do not approve without before and "
              "after screenshots at 320 px and 1280 px for each changed screen: the worker's reply must name the folder "
              "that holds them. Open the screenshots and check them against the claims. "
              "Its last reply:\n\nThe review card now shows the commit and the reply format.\n\n"
              "Transcript: /work/.codex/sessions/fixture.jsonl")
    entry = {'agent': 'claude', 'transcript': transcript}
    with injected_prompts.track_send(entry, review, 'review') as delivery, transcript.open('w') as stream:
        turns = [('user', delivery),
                 ('user', 'Check the Settings page at phone width.'),
                 ('assistant', 'The Settings page fits a 320 px screen. Each control keeps a 44 px touch target.'),
                 ('user', 'Where does this conversation go?'),
                 ('assistant', CHAT_LINE)]
        for index, (role, text) in enumerate(turns):
            stream.write(json.dumps({'type': role, 'uuid': f'00000000-0000-4000-8000-{index:012d}', 'sessionId': SESSION, 'cwd': work,
                                     'timestamp': f'2026-01-05T09:{index:02d}:00.000Z',
                                     'message': {'role': role, 'content': text if role == 'user' else [{'type': 'text', 'text': text}],
                                                 **({'model': 'claude-fixture', 'stop_reason': 'end_turn'} if role == 'assistant' else {})}}) + '\n')
        advisor = SERVER / 'fixtures' / 'claude_2.1.327_advisor'
        for name in ('answered.jsonl', 'error.jsonl'):
            stream.write((advisor / name).read_text().rstrip('\n') + '\n')
    backlog.add('Write the release notes')
    inbox = backlog.add('Fix the flaky share test', source='codex', project='tether', details=['The teardown races the HTTP worker'])
    done = backlog.add('Add backlog search', source='claude', project='tether')
    backlog.add('Draw the new icon set with a very long title that must wrap on a narrow phone screen without a sideways scroll', project='design')
    backlog.mark_done('Add backlog search', project='tether', result='Search added')
    inbox_id, done_id = (line.rsplit('#', 1)[1] for line in (inbox, done))
    factory_history.record({'moves': [{'task': inbox_id, 'phase': 'inbox'},
                                      {'task': done_id, 'phase': 'build'}, {'task': done_id, 'phase': 'done'}]})


def use_fixture_folders(root):
    """The Settings default-folder field goes to prefs_sync.session_folder, which asks the filesystem about any path
    the page sends. The test server replaces it. Rule: the path that is checked is the path that is looked up, as
    one exact string, with no second parser between the check and the lookup."""
    import paths
    import prefs_sync
    root = os.path.normpath(str(root))
    text_error = ValueError('Use an absolute folder path or a path that starts with ~/.')

    def session_folder(value=None):
        if value is None:
            value = prefs_sync.load()['prefs'].get('sessionFolder', '')  # saved as typed: it gets the same checks
        if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise text_error
        text = value.strip()
        if text in ('', '~'):
            text = str(paths.HOME)
        elif text.startswith('~/'):
            text = os.path.join(str(paths.HOME), text[2:])
        if not os.path.isabs(text):
            raise text_error
        wanted = os.path.normpath(text)
        if wanted != root and not wanted.startswith(root + os.sep):
            raise ValueError('The test Tether accepts a default folder inside its fixture root only.')
        # Each part below the root must be a real folder, not a link: nothing here follows a path out of the root.
        part = root
        for name in wanted[len(root):].split(os.sep):
            if name:
                part = os.path.join(part, name)
                # One lstat per part: a separate islink and isdir could follow a link swapped in between them.
                try:
                    is_folder = stat.S_ISDIR(os.lstat(part).st_mode)
                except OSError:
                    is_folder = False
                if not is_folder:
                    raise ValueError('The default session folder is unavailable. Choose an existing folder in the fixture root.')
        return wanted

    prefs_sync.session_folder = session_folder


def use_fixture_processes(pane_pid):
    import procinfo
    from agents import descendants

    def table(run=subprocess.run):
        # On macOS, comm reads the argument record even though it prints just the name.
        name_column = 'ucomm' if procinfo.DARWIN else 'comm'
        snapshot = run(['ps', '-axo', f'pid=,ppid=,{name_column}='],
                       capture_output=True, text=True, timeout=5, check=True)
        processes = {}
        for line in snapshot.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3:
                pid, ppid, name = fields
                processes[int(pid)] = (int(ppid), name)
        if pane_pid not in processes:
            return {}
        return {pid: processes[pid] for pid in descendants(pane_pid, processes)}

    procinfo.table = table
    # Task discovery and Antigravity can enumerate PIDs separately from session discovery.
    procinfo.pids = lambda proc=None: list(table())


def serve(root, dist):
    errors = isolation_errors(root)
    if errors:
        raise SystemExit('verify_server: not isolated, so it did not start:\n  ' + '\n  '.join(errors))
    guard(root, dist)
    wrapper = str(root / 'bin/tmux')
    listed = subprocess.run([wrapper, 'list-panes', '-a', '-F', '#{pane_id}\t#{pane_pid}\t#{session_id}'],
                            capture_output=True, text=True, timeout=10, check=True)
    pane, pane_pid, session = listed.stdout.splitlines()[0].split('\t')
    seed(root, pane, int(pane_pid), session)
    use_fixture_processes(int(pane_pid))
    use_fixture_folders(root)
    import auth
    import server
    from http.server import ThreadingHTTPServer
    from socketserver import TCPServer

    class FixtureHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind does a reverse DNS lookup even for a numeric address.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

    with FixtureHTTPServer(('127.0.0.1', 0), fixture_handler(server.Handler, root, pane)) as http:
        auth.token()
        print(json.dumps({'port': http.server_port, 'url': f'http://127.0.0.1:{http.server_port}/chat/', 'root': str(root),
                          'cookie': {'name': auth.COOKIE, 'value': auth.session_value()}, 'pane': pane, 'session': session,
                          'thread': SESSION}), flush=True)
        http.serve_forever()


def tmux_command(tmux, root, env, *args, client_options=()):
    return subprocess.run([tmux, *client_options, '-S', str(root / 'tmux.sock'), '-f', '/dev/null', *args],
                          env=env, capture_output=True, text=True, timeout=5, start_new_session=True)


def stop_tmux(tmux, root, env):
    socket_path = root / 'tmux.sock'
    # "server exited unexpectedly": the probe reached the server while kill-server was ending it. It is gone too.
    absent = {f'no server running on {socket_path}',
              f'error connecting to {socket_path} (No such file or directory)',
              'server exited unexpectedly'}
    errors = []
    for command in ('kill-server', 'list-sessions'):
        try:
            result = tmux_command(tmux, root, {**env, 'LC_ALL': 'C'}, command)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'private tmux {command} failed: {exc}')
            continue
        detail = result.stderr.strip()
        if result.returncode == 1 and detail in absent:
            continue
        if command == 'list-sessions' and result.returncode == 0:
            errors.append(f'private tmux still answers on {socket_path}')
        elif result.returncode != 0:
            errors.append(f'private tmux {command} failed: {detail or f"exit {result.returncode}"}')
    return errors


def check_dist(dist):
    def walk_error(error):
        raise error

    if dist.is_symlink():
        raise SystemExit(f'verify_server: build contains a symlink: {dist}')
    dist = dist.resolve()
    if not dist.is_dir():
        return dist
    for folder, dirs, files in os.walk(dist, onerror=walk_error):
        for name in (*dirs, *files):
            entry = Path(folder) / name
            if entry.is_symlink():
                raise SystemExit(f'verify_server: build contains a symlink: {entry}')
    return dist


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--dist', help='the built UI to serve at /chat/ (npm run build -- --outDir DIR)')
    parser.add_argument('--until-stdin-closes', action='store_true', help='stop when stdin closes')
    parser.add_argument('--root', type=Path, help='use a private fixture root and tmux server owned by the test watcher')
    parser.add_argument('--serve', metavar='ROOT', help=argparse.SUPPRESS)
    args = parser.parse_args()
    dist = Path(args.dist or SERVER.parent / 'ui/dist')
    if args.serve:
        dist = dist.resolve()
        root = check_root(args.serve, os.environ.pop('TETHER_VERIFY_MARKER', ''))
        if os.environ.get('TMUX_CHAT_DIST') != str(dist):
            raise SystemExit('verify_server: --serve needs the launcher build path.')
        return serve(root, dist)

    dist = check_dist(dist)
    tmux = shutil.which('tmux')
    if not tmux:
        raise SystemExit('verify_server: tmux is not installed.')
    tmux = str(Path(tmux).resolve())
    if not (dist / 'index.html').is_file():
        raise SystemExit(f'verify_server: no built UI in {dist}. Run: cd ui && npm run build -- --outDir DIR, then pass --dist DIR.')

    # A signal only sets a flag. A resource created by an in-progress call is still registered before cleanup.
    cancelled = False

    def stop(*_):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    root = child = env = None
    owns_root = args.root is None
    tmux_started = False
    try:
        root = Path(tempfile.mkdtemp(prefix='tv-', dir=TEMP)) if owns_root else check_supplied_root(args.root)
        client_options = () if owns_root else ('-N',)
        marker = secrets.token_hex(32)
        (root / MARKER).write_text(marker)
        check_root(root, marker)
        env = clean_env(root, dist)
        write_wrapper(root, tmux, client_options=client_options)
        for folder in ('work', 'tmp', 'home/.config/tether'):
            (root / folder).mkdir(parents=True)
        # The normal GET update route may schedule an external release check.
        (root / 'home/.config/tether/install.json').write_text('{"updates":"off"}\n')
        if cancelled:
            return 0
        tmux_started = owns_root  # Only the standalone launcher can start a server.
        started = tmux_command(tmux, root, env, 'new-session', '-d', '-s', 'Fixture', '-x', '120', '-y', '40',
                               '-c', str(root / 'work'), 'exec sleep 86400', ';',
                               'set-option', '-w', 'window-size', 'manual', ';',
                               'set-option', '-w', '@tether_window_size', '1', ';',
                               'resize-window', '-x', '120', '-y', '40', client_options=client_options)
        if started.returncode:
            raise SystemExit('verify_server: tmux did not start: ' + started.stderr.strip())
        if cancelled:
            return 0
        child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '--serve', str(root),
                                  '--dist', str(dist)], env={**env, 'TETHER_VERIFY_MARKER': marker}, cwd=SERVER,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        while child.poll() is None and not cancelled:
            if args.until_stdin_closes:
                if select.select([sys.stdin], [], [], 0.2)[0] and not os.read(sys.stdin.fileno(), 4096):
                    break
            else:
                try:
                    return child.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
        return child.returncode or 0
    finally:
        errors = []
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            if child is not None and child.poll() is None:
                errors.append('server child did not exit')
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'could not stop server child: {exc}')
        if tmux_started:
            errors.extend(stop_tmux(tmux, root, env))
        if root is not None and owns_root:
            try:
                shutil.rmtree(root)
            except OSError as exc:
                errors.append(f'could not remove fixture root: {exc}')
        if errors:
            previous = sys.exc_info()[1]
            if previous is not None:
                print(previous, file=sys.stderr)
            for error in errors:
                print('verify_server: cleanup: ' + error, file=sys.stderr)
            raise SystemExit(3)


if __name__ == '__main__':
    sys.exit(main())
