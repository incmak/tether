"""Test the verify server against decoys and a fresh private tmux."""
import errno
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'dev'))
import verify_server

SCRIPT = str(HERE / 'dev/verify_server.py')
TMUX = shutil.which('tmux')


class Configuration(unittest.TestCase):
    def test_live_names_do_not_pass(self):
        live = dict(PATH='/usr/bin', HOME='/outside/home', TMUX='/some/socket', TMUX_PANE='%5',
                    TMUX_TMPDIR='/outside', TETHER_HOME='/outside', TETHER_CDP_URL='ws://127.0.0.1:1/',
                    MOBILE_TMUX_TMUX='/outside/bin/tmux', MOBILE_TMUX_CONFIG='/outside/config',
                    XDG_RUNTIME_DIR='/outside/run', CODEX_HOME='/outside/codex')
        env = verify_server.clean_env('/tmp/tv-x', '/tmp/dist', base=live)
        self.assertEqual(env['HOME'], '/tmp/tv-x/home')
        self.assertEqual(env['MOBILE_TMUX_HOME'], env['HOME'])
        self.assertEqual(env['TMPDIR'], '/tmp/tv-x/tmp')
        self.assertEqual(env['MOBILE_TMUX_AUTH'], 'token')
        self.assertEqual(env['MOBILE_TMUX_TMUX'], '/tmp/tv-x/bin/tmux')
        self.assertEqual(env['PATH'], '/tmp/tv-x/bin:/usr/bin')
        self.assertEqual(set(live) & set(env), {'PATH', 'HOME', 'MOBILE_TMUX_TMUX'})

    def test_child_requires_private_marked_root(self):
        with tempfile.TemporaryDirectory(prefix='tv-', dir=verify_server.TEMP) as folder:
            root = Path(folder)
            for bad in (Path('/'), verify_server.REAL_HOME, verify_server.REAL_HOME.parent, root / 'nested', root):
                with self.subTest(root=bad):
                    run = subprocess.run([sys.executable, SCRIPT, '--serve', str(bad)],
                                         capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn('--serve needs', run.stderr)
            (root / verify_server.MARKER).write_text('fixture marker')
            self.assertEqual(verify_server.check_root(root, 'fixture marker'), root)
            with self.assertRaises(SystemExit):
                verify_server.check_root(root, 'wrong marker')
            dist = root / 'dist'
            env = verify_server.clean_env(root, dist)
            env['TETHER_VERIFY_MARKER'] = 'fixture marker'
            env['MOBILE_TMUX_TMUX'] = '/usr/bin/tmux'
            run = subprocess.run([sys.executable, SCRIPT, '--serve', folder, '--dist', str(dist)],
                                 env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('MOBILE_TMUX_TMUX and PATH must select the fixture wrapper', run.stderr)

    def test_signals_while_root_and_tmux_are_being_created(self):
        script = r'''
import os, signal, sys, tempfile
from pathlib import Path
import verify_server as v

when, signum, dist = sys.argv[1:]
signum = int(signum)
tracked = []
def temporary(*args, **kwargs):
    root = original(*args, **kwargs)
    tracked.append(Path(root))
    if when == 'root':
        os.kill(os.getpid(), signum)
    return root
original = tempfile.mkdtemp
tempfile.mkdtemp = temporary
commands = []
def tmux(executable, root, env, *args):
    assert root == tracked[0]
    commands.append(args[0])
    if args[0] == 'new-session':
        (root / 'tmux.sock').touch()
        os.kill(os.getpid(), signum)
    if args[0] == 'list-sessions':
        return v.SimpleNamespace(returncode=1, stderr=f'no server running on {root / "tmux.sock"}')
    return v.SimpleNamespace(returncode=0, stderr='')
v.tmux_command = tmux
v.shutil.which = lambda name: '/fixture/tmux'
sys.argv = ['verify_server.py', '--dist', dist]
assert v.main() == 0
assert commands == ([] if when == 'root' else ['new-session', 'kill-server', 'list-sessions']), commands
assert not tracked[0].exists()
'''
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'index.html').touch()
            for when, signum in (('root', signal.SIGINT), ('tmux', signal.SIGTERM)):
                with self.subTest(when=when):
                    done = subprocess.run([sys.executable, '-c', script, when, str(signum), folder],
                                          cwd=HERE / 'dev', capture_output=True, text=True, timeout=10)
                    self.assertEqual(done.returncode, 0, done.stderr)

    def test_launcher_refuses_symlinks_in_build(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder) / 'dist'
            dist.mkdir()
            (dist / 'index.html').touch()
            outside = Path(folder) / 'outside'
            outside.mkdir()
            assets = dist / 'assets'
            assets.mkdir()
            for link, target in ((dist / 'index.html.br', outside / 'missing'), (assets / 'nested', outside)):
                with self.subTest(link=link):
                    link.symlink_to(target)
                    try:
                        done = subprocess.run([sys.executable, SCRIPT, '--dist', str(dist)],
                                              capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(done.returncode, 0)
                        self.assertIn(f'build contains a symlink: {dist.resolve() / link.relative_to(dist)}', done.stderr)
                    finally:
                        link.unlink()

    def test_launcher_fails_when_private_tmux_still_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            (dist / 'index.html').touch()
            tmux = dist / 'tmux'
            tmux.write_text(r'''#!/bin/sh
[ "$1" = -S ] && [ "$3" = -f ] && [ "$4" = /dev/null ] || exit 8
printf '%s %s\n' "$5" "$2" >> "$0.log"
case "$5" in
    new-session) kill -TERM "$PPID" ;;
    kill-server) exit 0 ;;
    list-sessions) echo 'Fixture: 1 windows' ;;
    *) exit 8 ;;
esac
''')
            tmux.chmod(0o700)
            env = {**os.environ, 'PATH': folder + os.pathsep + os.defpath}
            done = subprocess.run([sys.executable, SCRIPT, '--dist', folder],
                                  env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(done.returncode, 3, done.stderr)
            steps = (dist / 'tmux.log').read_text().splitlines()
            commands, sockets = zip(*(step.split(' ', 1) for step in steps))
            self.assertEqual(commands, ('new-session', 'kill-server', 'list-sessions'))
            self.assertEqual(len(set(sockets)), 1)
            self.assertIn('private tmux still answers on ' + sockets[0], done.stderr)
            self.assertFalse(Path(sockets[0]).parent.exists())


BLOCKED_REQUESTS = [
    ('GET', '/terminal/live/socket', {'Upgrade': 'websocket', 'Connection': 'Upgrade'}),
    ('GET', '/mobile/api/live/agent', {}),
    ('PATCH', '/mobile/api/live/offer', {}),
    ('GET', '/mobile/api/browser/extension/connect', {'Connection': 'Upgrade', 'Upgrade': 'websocket'}),
    ('POST', '/mobile/api/browser/extension/exchange', {}),
    ('GET', '/mobile/api/browser/live', {'Connection': 'Upgrade', 'Upgrade': 'websocket'}),
    ('GET', '/browser/live/', {}),
    ('GET', '/office/', {}),
    ('HEAD', '/mobile/api/share/file', {}),
    ('PUT', '/mobile/api/upload', {}),
    ('DELETE', '/mobile/api/tmux-sessions', {}),
    ('OPTIONS', '/mobile/api/chat', {}),
    ('POST', '/mobile/api/dev-preview/stop', {}),
    ('POST', '/mobile/api/preview', {}),
    ('GET', '/mobile/preview/synthetic/index.html', {}),
    ('GET', '/mobile/api/usage?%6cive=1', {}),
    ('POST', '/mobile/api/start', {}),
    ('POST', '/mobile/api/login/start', {}),
    ('POST', '/mobile/api/reminders/add', {}),
    ('POST', '/mobile/api/tmux-sessions', {}),
]


class RouteGate(unittest.TestCase):
    def test_rejects_before_method_dispatch(self):
        class Base(BaseHTTPRequestHandler):
            def do_GET(self):
                self.serve_chat_ui(self.path)

            def serve_chat_ui(self, path):
                self.send_response(200)
                self.send_header('Content-Length', '7')
                self.end_headers()
                self.wfile.write(b'fixture')

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            handler = verify_server.fixture_handler(Base, root, '%0')

            def send(method, path, headers):
                request = object.__new__(handler)
                request.client_address = ('127.0.0.1', 1)
                wire = f'{method} {path} HTTP/1.1\r\nHost: fixture\r\n'
                wire += ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n'
                request.rfile, request.wfile = BytesIO(wire.encode()), BytesIO()
                request.handle_one_request()
                head, body = request.wfile.getvalue().split(b'\r\n\r\n', 1)
                return int(head.split()[1]), head, body

            for method, route, headers in BLOCKED_REQUESTS:
                with self.subTest(method=method, route=route):
                    status, head, body = send(method, route, headers)
                    self.assertEqual(status, 403)
                    self.assertIn(b'X-Tether-Verify-Blocked: 1', head)
                    self.assertEqual(body, b'' if method == 'HEAD' else json.dumps({'error': verify_server.OFF}).encode())
            self.assertEqual((root / 'blocked.log').read_text().splitlines(),
                             [f'route {method} {route.split("?")[0]}' for method, route, _ in BLOCKED_REQUESTS])
            self.assertEqual(send('GET', '/chat/fixture.js', {})[2], b'fixture')
            status, head, body = send('HEAD', '/chat/fixture.js', {})
            self.assertEqual(status, 200)
            self.assertIn(b'Content-Length: 7', head)
            self.assertEqual(body, b'')

    def test_fixture_routes_bypass_real_handler(self):
        script = r'''
from io import BytesIO
import json
import os
from pathlib import Path
from unittest.mock import patch
import server
import verify_server

root = Path(os.environ['HOME']).parent
handler = verify_server.fixture_handler(server.Handler, root, '%17')
with patch.object(server.Handler, 'do_GET', side_effect=AssertionError('real route ran')), \
     patch.object(verify_server.time, 'time', return_value=1234):
    for path, expected in (
        ('codex-runs?session=%259', {'pane': '%17', 'running': 0, 'runs': []}),
        ('host', {'name': 'Fixture', 'resources': False, 'terminal': False, 'office': False}),
        ('reminders', {'reminders': [], 'deleted': [], 'now': 1234}),
    ):
        assert not verify_server.allowed_route('GET', '/mobile/api/' + path)
        for cookie, expected_status in (('', 401), (server.auth.session_value(), 200)):
            request = object.__new__(handler)
            request.client_address = ('127.0.0.1', 1)
            wire = (f'GET /mobile/api/{path} HTTP/1.1\r\nHost: fixture\r\n'
                    f'Cookie: {server.auth.COOKIE}={cookie}\r\n\r\n')
            request.rfile, request.wfile = BytesIO(wire.encode()), BytesIO()
            request.handle_one_request()
            head, body = request.wfile.getvalue().split(b'\r\n\r\n', 1)
            assert int(head.split()[1]) == expected_status, (head, body)
            if expected_status == 200:
                assert json.loads(body) == expected, body
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'home').mkdir()
            env = verify_server.clean_env(root, root / 'dist')
            env['PYTHONPATH'] = str(HERE / 'dev')
            done = subprocess.run([sys.executable, '-c', script], cwd=HERE, env=env,
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(done.returncode, 0, done.stderr)


PROCESS_ROWS = '''\
105 104 grandchild
101 1 decoy
102 101 decoy-child
104 103 child
103 100 sleep
100 1 tmux
'''


class FixtureProcesses(unittest.TestCase):
    def test_linux_process_table_keeps_only_pane_tree(self):
        import procinfo

        run = Mock(return_value=SimpleNamespace(stdout=PROCESS_ROWS))
        with (patch.object(procinfo, 'DARWIN', False), patch.object(procinfo, 'table'),
              patch.object(procinfo, 'pids'), patch.object(verify_server.subprocess, 'run', run)):
            verify_server.use_fixture_processes(103)
            self.assertEqual(procinfo.table(run), {103: (100, 'sleep'), 104: (103, 'child'), 105: (104, 'grandchild')})
            self.assertEqual(set(procinfo.pids()), {103, 104, 105})
            run.return_value.stdout = PROCESS_ROWS.replace('103 100 sleep\n', '')
            self.assertEqual(procinfo.table(run), {})
        self.assertTrue(all(call.args[0] == ['ps', '-axo', 'pid=,ppid=,comm='] for call in run.call_args_list))

    def test_darwin_sessions_reads_only_fixture_processes(self):
        script = r'''
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import auth, procinfo, verify_server

root = Path(os.environ['HOME']).parent
process_rows = sys.argv[1]
parents = {int(pid): {'ppid': int(parent)} for pid, parent, _ in (line.split() for line in process_rows.splitlines())}
allowed = {103, 104, 105}

def run(command, **kwargs):
    output = ''
    if command[0] == 'ps':
        assert command == ['ps', '-axo', 'pid=,ppid=,ucomm='], command
        output = process_rows
    elif command[1] == 'list-panes':
        if '#{pane_current_path}' in command[-1]:
            output = f'%17\t$0\tFixture\t0.0\t103\t{root / "work"}' + '\t' * 7 + '\n'
        else:
            output = '%17\t103\t$0\n'
    return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

class InMemoryServer(ThreadingHTTPServer):
    def __init__(self, address, handler):
        self.server_port = 32123
        self.handler = handler

    def server_close(self):
        pass

    def serve_forever(self):
        request = object.__new__(self.handler)
        request.client_address = ('127.0.0.1', 1)
        wire = ('GET /mobile/api/sessions HTTP/1.1\r\nHost: fixture\r\n'
                f'Cookie: {auth.COOKIE}={auth.session_value()}\r\n\r\n')
        request.rfile, request.wfile = BytesIO(wire.encode()), BytesIO()
        request.handle_one_request()
        head, body = request.wfile.getvalue().split(b'\r\n\r\n', 1)
        assert int(head.split()[1]) == 200, (head, body)
        sessions = json.loads(body)['sessions']
        assert [(s['id'], s['agent'], s['thread']) for s in sessions] == [('%17', 'claude', verify_server.SESSION)], sessions

with (patch('http.server.ThreadingHTTPServer', InMemoryServer),
      patch.object(verify_server, 'isolation_errors', return_value=[]), patch.object(verify_server, 'guard'),
      patch.object(subprocess, 'run', side_effect=run), patch.object(procinfo, 'DARWIN', True),
      patch.object(procinfo, '_darwin_snapshot', return_value=parents),
      patch.object(procinfo, 'start_ticks', return_value='1767603600.000000') as started,
      patch.object(procinfo, 'argv', return_value=[b'sleep']) as argv):
    verify_server.serve(root, root / 'dist')
    assert started.called
    for reader in (argv, started):
        assert {call.args[0] for call in reader.call_args_list} <= allowed, reader.call_args_list
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'home').mkdir()
            env = verify_server.clean_env(root, root / 'dist')
            env['PYTHONPATH'] = str(HERE / 'dev')
            done = subprocess.run([sys.executable, '-c', script, PROCESS_ROWS], cwd=HERE, env=env,
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(done.returncode, 0, done.stderr)


GUARDED = r'''
import os, signal, socket, subprocess, sys
from pathlib import Path
import verify_server

root, outside, dist = map(Path, sys.argv[1:4])
outside_fd = os.open(outside, os.O_RDONLY | os.O_DIRECTORY)
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
verify_server.guard(root, dist)
def refused(key, action):
    try:
        action()
    except PermissionError as exc:
        assert 'verify_server: refused ' in str(exc), exc
        return
    raise AssertionError('allowed: ' + key)

(root / 'ok.txt').write_text('fixture')
assert (dist / 'index.html').read_text() == 'build'
assert (verify_server.SERVER / 'server.py').open().read(1)
decoy = outside / 'keep.txt'
link = root / 'link.txt'
for mode in ('w', 'a', 'r'):
    refused('symlink ' + mode, lambda: link.open(mode))
refused('open', decoy.read_text)
refused('outside alias', (outside / 'device-link').read_bytes)
refused('listdir', lambda: os.listdir(outside))
refused('scandir', lambda: os.scandir(outside))
refused('dir_fd open read', lambda: os.open('keep.txt', os.O_RDONLY, dir_fd=outside_fd))
refused('dir_fd open write', lambda: os.open(b'dir-fd-created', os.O_CREAT | os.O_WRONLY, dir_fd=outside_fd))
refused('dir_fd listdir', lambda: os.listdir(outside_fd))
refused('dir_fd scandir', lambda: os.scandir(outside_fd))
os.close(outside_fd)
refused('unresolved dir_fd', lambda: os.open('keep.txt', os.O_RDONLY, dir_fd=outside_fd))
fd = os.open(Path('ok.txt'), os.O_RDONLY, dir_fd=root_fd)
with os.fdopen(fd) as fixture:
    assert fixture.read() == 'fixture'
fd = os.open(b'created-in-fixture', os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=root_fd)
os.close(fd)
assert 'created-in-fixture' in os.listdir(root_fd)
with os.scandir(root_fd) as entries:
    assert any(entry.name == 'created-in-fixture' for entry in entries)
refused('dir_fd remove', lambda: os.remove('ok.txt', dir_fd=root_fd))
os.close(root_fd)
refused('mkdir outside', lambda: (outside / 'new').mkdir())
refused('rename outside', lambda: (root / 'ok.txt').rename(decoy))
refused('remove outside', decoy.unlink)
refused('popen', lambda: subprocess.run(['true']))
refused('popen executable override', lambda: subprocess.run(['ps'], executable='true'))
refused('shell', lambda: subprocess.run('ps', shell=True))
refused('exec', lambda: os.execv('/bin/true', ['ps']))
ps = verify_server.shutil.which('ps', path=os.defpath)
refused('spawn with file action', lambda: os.posix_spawn(ps, [ps], os.environ,
        file_actions=[(os.POSIX_SPAWN_OPEN, 1, str(outside / 'spawn-created'), os.O_CREAT | os.O_WRONLY, 0o600)]))
assert not getattr(subprocess, '_USE_POSIX_SPAWN', False)
refused('system', lambda: os.system('ps'))
refused('fork', os.fork)
refused('non-child kill', lambda: os.kill(int(sys.argv[4]), signal.SIGTERM))
refused('non-child killpg', lambda: os.killpg(int(sys.argv[4]), signal.SIGTERM))
# AF_UNIX creation works without a listening socket. PermissionError must come from this guard.
with socket.socket(socket.AF_UNIX) as sock:
    try:
        sock.connect(str(outside / 'missing.sock'))
    except PermissionError as exc:
        assert 'verify_server: refused socket.connect' in str(exc), exc
    else:
        raise AssertionError('allowed connect')
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
    refused('datagram send', lambda: sock.sendto(b'decoy', str(outside / 'missing.sock')))
refused('resolver lookup', lambda: socket.getaddrinfo('127.0.0.1', 0, flags=socket.AI_NUMERICHOST))
subprocess.run(['ps'], check=True, capture_output=True)
try:
    subprocess.run(['ps'], timeout=0, check=True, capture_output=True)
except subprocess.TimeoutExpired:
    pass
else:
    raise AssertionError('zero-timeout subprocess did not time out')
'''


class Folders(unittest.TestCase):
    def test_default_folder_lookups_never_leave_the_fixture_root(self):
        script = r"""
import os, sys
from pathlib import Path
import verify_server
root, decoy = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(verify_server.SERVER))
import paths, prefs_sync
verify_server.use_fixture_folders(root)
OUTSIDE = 'The test Tether accepts a default folder inside its fixture root only.'
GONE = 'The default session folder is unavailable. Choose an existing folder in the fixture root.'
TEXT = 'Use an absolute folder path or a path that starts with ~/.'
os.symlink(decoy, root / 'work' / 'link')          # a link that leads out
os.symlink(decoy + '/missing', root / 'work' / 'dead')
os.symlink(decoy, root / 'work' / 'ws')            # next to a real folder whose name differs by one space
(root / 'work' / 'ws ').mkdir()
(root / 'work' / 'file').write_text('x')           # a file is not a folder
asked, followed = [], []
real_stat, real_lstat = os.stat, os.lstat
os.stat = lambda path, *a, **k: (asked.append(os.fspath(path)), followed.append(os.fspath(path)), real_stat(path, *a, **k))[2]
os.lstat = lambda path, *a, **k: (asked.append(os.fspath(path)), real_lstat(path, *a, **k))[1]
def answer(value):
    try:
        return prefs_sync.session_folder(value)
    except ValueError as error:
        return str(error)
there, missing = os.path.basename(decoy), 'tv-no-such-folder'
cases = [
    (decoy, OUTSIDE), (decoy + '/missing', OUTSIDE), (str(root) + '-sibling', OUTSIDE),
    (str(root / 'work' / '..' / '..' / 'elsewhere'), OUTSIDE), ('~/../../' + decoy.lstrip('/'), OUTSIDE),
    # A link below the root is refused without following it: a live and a dead target answer the same.
    (str(root / 'work' / 'link'), GONE), (str(root / 'work' / 'dead'), GONE), (str(root / 'work' / 'link' / 'below'), GONE),
    (str(root / 'work' / 'ws'), GONE), (str(root / 'work' / 'ws') + '/.', GONE),
    # The name with the space is the real folder. The checked string is the string that is looked up.
    (str(root / 'work' / 'ws ') + '/.', str(root / 'work' / 'ws ')),
    (str(root / 'work' / 'none'), GONE), (str(root / 'work' / 'file'), GONE), (str(root / 'work' / 'file' / 'below'), GONE),
    # A path that leaves the root and returns is looked up by its normalized form, whatever exists outside.
    (f'{root}/../{there}/../{root.name}/work', str(root / 'work')),
    (f'{root}/../{missing}/../{root.name}/work', str(root / 'work')),
    (str(root / 'work'), str(root / 'work')), ('', str(paths.HOME)), ('~', str(paths.HOME)), ('~/', str(paths.HOME)),
    ('work', TEXT), (7, TEXT), (['x'], TEXT), ('a\x00b', TEXT),
]
for value, expected in cases:
    assert answer(value) == expected, (value, answer(value), expected)
# The stored default is saved as typed. A later lookup of it, and later invalid saves, get the same checks.
for text in (f'{root}/../{there}/../{root.name}/work', f'{root}/../{missing}/../{root.name}/work'):
    prefs_sync.save({'sessionFolder': text})
    assert answer(None) == str(root / 'work')
    for bad in (None, 7, ['x'], decoy, str(root / 'work' / 'link')):
        try:
            prefs_sync.save({'sessionFolder': bad})
            raise SystemExit(f'saved {bad!r}')
        except ValueError:
            pass
    assert prefs_sync.load()['prefs']['sessionFolder'] == text
# Every filesystem question the folder checks asked was about a path below the root, and never through a link.
below = [path for path in asked if isinstance(path, str)]
config = str(paths.CONFIG)
folder_questions = [path for path in below if not path.startswith(config)]
assert folder_questions, 'the spies saw no lookup'
bad = [path for path in folder_questions if not (path + os.sep).startswith(str(root) + os.sep)
       or any(part in path.split(os.sep) for part in ('..',))
       or path.startswith((str(root / 'work' / 'link') + os.sep, str(root / 'work' / 'ws') + os.sep, str(root / 'work' / 'dead') + os.sep))]
assert bad == [], bad
# One lstat per part (B254): a stat that follows links could see a link swapped in after the lstat.
followed_below = [path for path in followed if isinstance(path, str) and (path + os.sep).startswith(str(root / 'work') + os.sep)]
assert followed_below == [], followed_below
print('ok')
"""
        with tempfile.TemporaryDirectory(prefix='tv-') as root, tempfile.TemporaryDirectory(prefix='tv-decoy-') as decoy:
            for folder in ('home', 'work', 'tmp'):
                (Path(root) / folder).mkdir()
            env = verify_server.clean_env(root, root)
            done = subprocess.run([sys.executable, '-c', script, root, decoy], env={**env, 'PYTHONPATH': str(HERE / 'dev')},
                                  cwd=str(HERE), capture_output=True, text=True, timeout=60)
            self.assertEqual((done.returncode, done.stdout.strip()), (0, 'ok'), done.stderr)


class Shutdown(unittest.TestCase):
    def test_a_server_that_exits_during_the_probe_counts_as_gone(self):
        """Under load, list-sessions can reach the private server while kill-server ends it."""
        original = verify_server.tmux_command
        self.addCleanup(setattr, verify_server, 'tmux_command', original)
        root = Path('/tmp/tv-shutdown-test')
        answers = {'gone': (1, 'server exited unexpectedly'), 'absent': (1, f'no server running on {root / "tmux.sock"}'),
                   'alive': (0, ''), 'other': (1, 'lost server')}
        for name, expected in (('gone', []), ('absent', []), ('alive', [f'private tmux still answers on {root / "tmux.sock"}']),
                               ('other', ['private tmux list-sessions failed: lost server'])):
            with self.subTest(name):
                code, text = answers[name]
                verify_server.tmux_command = lambda tmux, where, env, command: SimpleNamespace(
                    returncode=0 if command == 'kill-server' else code, stderr='' if command == 'kill-server' else text)
                self.assertEqual(verify_server.stop_tmux('tmux', root, {}), expected)


class Guard(unittest.TestCase):
    def test_reads_writes_executables_connects_and_signals(self):
        with (tempfile.TemporaryDirectory(dir=verify_server.TEMP) as folder,
              tempfile.TemporaryDirectory(dir=verify_server.TEMP) as other,
              tempfile.TemporaryDirectory(dir=verify_server.TEMP) as build,
              subprocess.Popen(['sleep', '60'], start_new_session=True) as decoy):
            try:
                root, outside, dist = map(Path, (folder, other, build))
                keep = outside / 'keep.txt'
                keep.write_text('decoy data')
                (outside / 'device-link').symlink_to(os.devnull)
                (dist / 'index.html').write_text('build')
                (root / 'link.txt').symlink_to(keep)
                done = subprocess.run([sys.executable, '-c', GUARDED, folder, other, build, str(decoy.pid)],
                                      env=verify_server.clean_env(root, dist), cwd=HERE / 'dev',
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIsNone(decoy.poll())
                self.assertEqual(keep.read_text(), 'decoy data')
                self.assertFalse((outside / 'dir-fd-created').exists())
                self.assertFalse((outside / 'spawn-created').exists())
                leaks = (root / 'leaks.log').read_text()
                self.assertIn('write ' + str(root / 'link.txt'), leaks)
                self.assertIn('read ' + str(outside), leaks)
                blocked = (root / 'blocked.log').read_text()
                for event in ('run true', 'run sh', 'run os.fork', 'run os.system', 'run os.posix_spawn',
                              'socket.connect', 'socket.sendto', 'socket.getaddrinfo',
                              'os.kill ' + str(decoy.pid), 'os.killpg ' + str(decoy.pid)):
                    self.assertIn(event, blocked)
            finally:
                decoy.terminate()
                decoy.wait(timeout=5)


WRAPPER_REFUSALS = [
    ('new-window',), ('send-keys', '-t', '%0', 'hello'), ('kill-server',),
    ('-S', '/not/a/socket', 'list-sessions'), ('-L', 'other', 'list-sessions'),
    ('list-sessions', '-F', '#(echo should-not-run)'),
    ('list-sessions', '-F', '#{session_id};', 'kill-server'), ('capture-pane', '-t', '%0'),
    ('set-option', '-g', '@not_allowed', '1'),
]


@unittest.skipUnless(TMUX, 'tmux is not installed')
class Wrapper(unittest.TestCase):
    def test_refuses_commands_before_contacting_a_socket(self):
        with tempfile.TemporaryDirectory(dir=verify_server.TEMP) as folder:
            root = Path(folder)
            verify_server.write_wrapper(root, TMUX)
            for args in WRAPPER_REFUSALS:
                with self.subTest(args=args):
                    run = subprocess.run([str(root / 'bin/tmux'), *args], capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn('tmux wrapper refused', run.stderr)
            self.assertEqual((root / 'blocked.log').read_text().splitlines(),
                             ['tmux ' + args[0] for args in WRAPPER_REFUSALS])


def require_loopback(case):
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            case.skipTest('not run (sandbox): loopback socket: ' + str(exc))
        raise


# Separate processes and an isolated environment keep the tests away from live Tether state.
DECOYS = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, signal, subprocess, threading, urllib.error, urllib.request
from pathlib import Path
import server, verify_server

root = Path(os.environ['HOME']).parent
decoy_file = root.parent / 'decoy-scratch' / 'example' / 'index.html'
decoy_file.parent.mkdir(parents=True)
decoy_file.write_text('decoy preview contents')
server.previews.PREVIEWS = server.previews.Previews(scratch=lambda: decoy_file.parent.parent)
preview = server.previews.PREVIEWS.issue(str(decoy_file), root)['url']
decoy = subprocess.Popen(['sleep', '60'])
hits = []
class Decoy(BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self, *_):
        pass

def stop(*args, **kwargs):
    os.kill(decoy.pid, signal.SIGTERM)
    return {8888: 'stopped'}

server.dev_previews.PREVIEWS.stop = stop
try:
    with (ThreadingHTTPServer(('127.0.0.1', 0), Decoy) as outside,
          ThreadingHTTPServer(('127.0.0.1', 0), verify_server.fixture_handler(server.Handler, root, '%0')) as fixture):
        server.TTYD_PORT = outside.server_port
        server.friday_proxy.URL = f'http://127.0.0.1:{outside.server_port}'
        threads = []
        for listener in (outside, fixture):
            thread = threading.Thread(target=listener.serve_forever)
            thread.start()
            threads.append(thread)
        try:
            def check(path, method='GET', payload=None, extra=None):
                origin = f'http://127.0.0.1:{fixture.server_port}'
                cookie = server.auth.COOKIE + '=' + server.auth.session_value()
                headers = {'Origin': origin, 'Cookie': cookie, 'X-Codex-Mobile': '1', 'Content-Type': 'application/json'}
                headers.update(extra or {})
                request = urllib.request.Request(origin + path, payload, headers, method=method)
                try:
                    with urllib.request.urlopen(request, timeout=5) as reply:
                        raise AssertionError((path, reply.status))
                except urllib.error.HTTPError as reply:
                    with reply:
                        assert reply.code == 403, (path, reply.code)
                        assert json.load(reply) == {'error': verify_server.OFF}
            check('/terminal/live/')
            check('/terminal/live/websocket', extra={'Upgrade': 'websocket', 'Connection': 'Upgrade'})
            check('/mobile/api/live/agent')
            check('/mobile/api/live/offer', 'PATCH', b'{}')
            check('/mobile/api/dev-preview/stop', 'POST', b'{"ports":[8888]}')
            check('/mobile/api/preview', 'POST', json.dumps({'path': str(decoy_file)}).encode())
            check(preview)
            assert not hits, hits
            assert decoy.poll() is None
        finally:
            for listener in (outside, fixture):
                listener.shutdown()
            for thread in threads:
                thread.join()
finally:
    if decoy.poll() is None:
        decoy.terminate()
    decoy.wait(timeout=5)
'''


class ProxyDecoys(unittest.TestCase):
    def test_real_handler_does_not_reach_decoys(self):
        require_loopback(self)
        with tempfile.TemporaryDirectory(dir=verify_server.TEMP) as folder:
            root = Path(folder) / 'fixture'
            (root / 'home').mkdir(parents=True)
            env = verify_server.clean_env(root, root / 'dist')
            env['PYTHONPATH'] = str(HERE / 'dev')
            done = subprocess.run([sys.executable, '-c', DECOYS], cwd=HERE, env=env,
                                  capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr)


@unittest.skipUnless(TMUX, 'tmux is not installed')
class Boot(unittest.TestCase):
    def setUp(self):
        require_loopback(self)
        self.dist = Path(self.enterContext(tempfile.TemporaryDirectory(dir=verify_server.TEMP)))
        (self.dist / 'index.html').write_text('<!doctype html><title>Fixture build</title>')
        self.decoy = self.dist / 'other.sock'
        self.env = verify_server.clean_env(self.dist, self.dist)
        started = self.tmux(self.decoy, 'new-session', '-d', '-s', 'Decoy', 'exec sleep 60')
        if 'Operation not permitted' in started.stderr or 'Permission denied' in started.stderr:
            self.skipTest('not run (sandbox): private tmux socket: ' + started.stderr.strip())
        started.check_returncode()
        self.addCleanup(lambda: self.tmux(self.decoy, 'kill-server').check_returncode())

    def tmux(self, sock, *args):
        return subprocess.run([TMUX, '-S', str(sock), '-f', '/dev/null', *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def start(self):
        process = subprocess.Popen([sys.executable, SCRIPT, '--dist', str(self.dist), '--until-stdin-closes'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)

        def stop():
            if process.poll() is None:
                process.terminate()
            try:
                output = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                output = process.communicate(timeout=5)
            return output[1].decode()

        self.addCleanup(stop)
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            data = bytearray()
            import time
            deadline = time.monotonic() + 60
            while b'\n' not in data and (remaining := deadline - time.monotonic()) > 0 and ready.select(remaining):
                block = os.read(process.stdout.fileno(), 4096)
                if not block:
                    break
                data.extend(block)
        self.assertIn(b'\n', data, stop() if b'\n' not in data else '')
        return process, json.loads(data.splitlines()[0])

    def request(self, info, path, method='GET', body=None, cookie=True):
        origin = f'http://127.0.0.1:{info["port"]}'
        headers = {'Origin': origin, 'X-Codex-Mobile': '1', 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = f'{info["cookie"]["name"]}={info["cookie"]["value"]}'
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(origin + path, headers=headers, method=method, data=data)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def test_serves_fixtures_only_and_cleans_up_on_signal(self):
        process, info = self.start()
        root = Path(info['root'])
        self.assertEqual(root.parent, verify_server.TEMP)
        status, body = self.request(info, '/mobile/api/sessions')
        self.assertEqual(status, 200, body)
        sessions = json.loads(body)['sessions']
        self.assertEqual([(entry['id'], entry['label'], entry['agent'], entry['thread']) for entry in sessions],
                         [(info['pane'], 'Fixture', 'claude', verify_server.SESSION)])
        self.assertTrue(sessions[0]['dir'].startswith(str(root)))
        status, body = self.request(info, '/mobile/api/chat?session=' + info['pane'].replace('%', '%25'))
        self.assertEqual(status, 200, body)
        messages = json.loads(body)['messages']
        self.assertEqual([(message['role'], message.get('kind')) for message in messages], [
            ('user', None), ('user', None), ('assistant', None), ('user', None), ('assistant', None),
            ('assistant', 'advisor'), ('assistant', 'actions'), ('assistant', 'advisor'),
        ])
        self.assertEqual([message['advisor']['status'] for message in messages if message.get('kind') == 'advisor'],
                         ['answered', 'error'])
        self.assertNotIn(b'encrypted_content', body)
        self.assertIn(verify_server.CHAT_LINE.encode(), body)
        self.assertIn(f'start it with `APPROVED {verify_server.REVIEW_COMMIT}`'.encode(), body)  # the chat-review surface
        for title in (b'Write the release notes', b'Fix the flaky share test', b'Add backlog search', b'Draw the new icon set'):
            self.assertNotIn(title, body)
        status, body = self.request(info, '/mobile/api/projects/backlog')
        self.assertEqual(status, 200, body)
        self.assertIn('Fix the flaky share test', json.loads(body)['text'])
        self.assertEqual(self.request(info, '/mobile/api/sessions', cookie=False)[0], 401)
        self.assertEqual(json.loads(self.request(info, '/mobile/api/codex-runs')[1]),
                         {'pane': info['pane'], 'running': 0, 'runs': []})
        self.assertIn(b'Fixture build', self.request(info, '/chat/')[1])
        self.assertEqual(self.request(info, '/chat/', 'HEAD'), (200, b''))
        self.assertEqual(self.request(info, '/mobile/api/prefs', 'POST', {'changes': {'theme': 'dark'}})[0], 200)
        self.assertEqual(json.loads(self.request(info, '/mobile/api/update')[1])['mode'], 'off')
        self.assertEqual((root / 'leaks.log').read_text(), '')
        self.assertEqual((root / 'blocked.log').read_text(), '')
        sock = root / 'tmux.sock'
        panes = self.tmux(sock, 'list-panes', '-a', '-F', '#{pane_pid}')
        panes.check_returncode()
        pid = int(panes.stdout.strip())
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=15), 0)
        self.assertFalse(root.exists())
        self.assertNotEqual(self.tmux(sock, 'list-sessions').returncode, 0)
        # A session killed with its private server can briefly leave a zombie until its parent reaps it.
        shown = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=5)
        self.assertTrue(shown.returncode == 1 or shown.returncode == 0 and shown.stdout.startswith('Z'), shown.stdout)

    def test_actions_and_socket_overrides_never_change_private_or_decoy_tmux(self):
        _, info = self.start()
        root = Path(info['root'])
        private = root / 'tmux.sock'

        def windows(sock):
            result = self.tmux(sock, 'list-windows', '-a', '-F', '#{session_name}:#{window_id}')
            result.check_returncode()
            return result.stdout.splitlines()

        before, other = windows(private), windows(self.decoy)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(other), 1)
        self.assertTrue(before[0].startswith('Fixture:'))
        self.assertTrue(other[0].startswith('Decoy:'))
        actions = {
            'start': {'session': info['pane'], 'agent': 'claude'},
            'login/start': {'agent': 'claude', 'method': 'claudeai'},
            'tmux-sessions': {'name': 'Worktree', 'worktree': 'fixture-decoy'},
            'agent-tab': {'session': info['pane'], 'agent': 'codex'},
        }
        for path, data in actions.items():
            with self.subTest(path=path):
                status, body = self.request(info, '/mobile/api/' + path, 'POST', data)
                self.assertEqual(status, 403, body)
        wrapper = root / 'bin/tmux'
        for args in (*WRAPPER_REFUSALS, ('-S', str(self.decoy), 'kill-server'), ('-L', self.decoy.name, 'kill-server')):
            result = subprocess.run([str(wrapper), *args], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('tmux wrapper refused', result.stderr)
        shown = subprocess.run([str(wrapper), 'display-message', '-p', '#{socket_path}'],
                               capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(shown.stdout.strip(), str(private))
        self.assertEqual(windows(private), before)
        self.assertEqual(windows(self.decoy), other)


if __name__ == '__main__':
    unittest.main()
