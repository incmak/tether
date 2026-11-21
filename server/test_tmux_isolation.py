"""Private tmux servers and checks for their test lifecycle."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import ast
from io import StringIO
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import paths
import procinfo

HERE = os.path.dirname(os.path.abspath(__file__))


def _tmux_env(env=None):
    result = {**(os.environ if env is None else env)}
    result.pop('TMUX', None)
    result.pop('TMUX_PANE', None)
    return result


class PrivateTmux:
    def __init__(self, folder, env=None):
        self.folder = os.fspath(folder)
        self.socket = os.path.join(self.folder, 'tmux.sock')
        # A None value removes the variable. The server keeps the env it starts with.
        merged = {**os.environ, **(env or {}), 'TMUX_TMPDIR': self.folder}
        self.env = _tmux_env({key: value for key, value in merged.items() if value is not None})

    def run(self, argv, **kwargs):
        kwargs.setdefault('env', self.env)
        if argv and (argv[0] == paths.TMUX or os.path.basename(argv[0]) == 'tmux'):  # paths.TMUX may be an override
            argv = [argv[0], '-N', '-S', self.socket, '-f', '/dev/null', *argv[1:]]
            kwargs['env'] = _tmux_env(kwargs['env'])
            kwargs['env']['TMUX_TMPDIR'] = self.folder
        return subprocess.run(argv, **kwargs)

    def tmux(self, *args):
        return self.run([paths.TMUX, *args], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)


def private_tmux(case, prefix, env=None):
    # Short paths fit in a Unix socket address.
    tmux = PrivateTmux(tempfile.mkdtemp(prefix=prefix, dir='/tmp'), env)
    tmux.pid = watch_request('start', tmux.folder, env=tmux.env)['pid']
    case.addCleanup(watch_request, 'stop', tmux.folder)
    return tmux


# A detached watcher owns each server as a child. The request pipe closes when the test process exits,
# even if the runner is killed or has not been reaped.
_watch = None
IMPORT_WATCH = ("import sys, types\n"
                "sys.modules['unittest_isolated'] = types.ModuleType('unittest_isolated')\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "import test_tmux_isolation as w\n")
WATCH = IMPORT_WATCH + "w.watch(sys.stdin, sys.stdout)\n"
DETACH = ("import subprocess, sys\n"
          "subprocess.Popen(sys.argv[1:], start_new_session=True)\n")
START_TIMEOUT = 10
TERM_TIMEOUT = 5


def _close_watch_in_child():
    global _watch
    if _watch is not None:
        for pipe in _watch:
            pipe.close()
        _watch = None


os.register_at_fork(after_in_child=_close_watch_in_child)


def watch_request(op, folder, **fields):
    global _watch
    if _watch is None:
        request_read, request_write = os.pipe()
        reply_read, reply_write = os.pipe()
        with os.fdopen(request_read) as reader, os.fdopen(reply_write, 'w') as writer:
            try:
                # Wait for the middle process. The watcher leaves the runner's process group.
                subprocess.run([sys.executable, '-c', DETACH, sys.executable, '-c', WATCH, HERE],
                               stdin=reader, stdout=writer, timeout=10, check=True)
            except BaseException:
                os.close(request_write)
                os.close(reply_read)
                raise
        _watch = os.fdopen(request_write, 'w'), os.fdopen(reply_read)
    requests, replies = _watch
    requests.write(json.dumps({'op': op, 'folder': os.fspath(folder), **fields}) + '\n')
    requests.flush()
    reply = replies.readline()
    if not reply:
        raise RuntimeError(f'Private tmux watcher exited during {op} {folder}')
    result = json.loads(reply)
    if not result['ok']:
        raise RuntimeError(f'Private tmux watcher could not {op} {folder}: {result["error"]}')
    return result


def _wait_for_server(child, path):
    deadline = time.monotonic() + START_TIMEOUT
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        while (remaining := deadline - time.monotonic()) > 0:
            if child.poll() is not None:
                raise RuntimeError(f'tmux exited before listening (exit {child.returncode})')
            client.settimeout(remaining)
            try:
                client.connect(path)
                return
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(min(0.02, remaining))  # Poll for a listener or a child exit.
    raise TimeoutError(f'tmux did not listen on {path}')


def _stop_tmux_child(child):
    # Popen only signals this child while it remains unreaped. No socket or process lookup chooses a PID.
    child.terminate()
    try:
        child.wait(timeout=TERM_TIMEOUT)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def watch(requests, replies):
    folders = {}
    children = {}
    errors = (OSError, RuntimeError, ValueError, TypeError, KeyError)

    def reap(folder):
        if folder in children:
            _stop_tmux_child(children[folder])
            del children[folder]

    def remove(folder):
        try:
            current = os.lstat(folder)
        except FileNotFoundError:
            pass
        else:
            if not os.path.samestat(current, folders[folder]):
                raise RuntimeError(f'folder was replaced: {folder}')
            shutil.rmtree(folder)
        del folders[folder]

    try:
        for line in requests:
            try:
                request = json.loads(line)
                op, folder = request['op'], request['folder']
                if op == 'stop':
                    if folder not in folders:
                        raise RuntimeError('folder is not owned')
                    reap(folder)
                    remove(folder)
                    reply = {'ok': True}
                elif op in ('own', 'start'):
                    info = os.lstat(folder)
                    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                        raise RuntimeError('expected a private folder owned by the test user')
                    if folder in folders and not os.path.samestat(info, folders[folder]):
                        raise RuntimeError('folder was replaced')
                    if op == 'start' and (folder in children or os.listdir(folder)):
                        raise RuntimeError('expected an empty folder for a new tmux server')
                    folders[folder] = info
                    reply = {'ok': True}
                    if op == 'start':
                        path = os.path.join(folder, 'tmux.sock')
                        try:
                            env = _tmux_env(request['env'])
                            env['TMUX_TMPDIR'] = folder
                            child = subprocess.Popen([paths.TMUX, '-D', '-S', path, '-f', '/dev/null'],
                                                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL)
                            children[folder] = child
                            _wait_for_server(child, path)
                        except errors:
                            reap(folder)
                            remove(folder)
                            raise
                        reply['pid'] = child.pid
                else:
                    raise ValueError(f'unknown operation: {op}')
            except errors as exc:
                reply = {'ok': False, 'error': str(exc)}
            try:
                replies.write(json.dumps(reply) + '\n')
                replies.flush()
            except BrokenPipeError:
                # The runner can disappear before it reads the reply.
                break
    finally:
        for folder in list(children):
            try:
                reap(folder)
            except Exception as exc:
                print(f'Private tmux watcher could not stop the child in {folder}: {exc}', file=sys.stderr)
        for folder in list(folders):
            if folder not in children:
                try:
                    remove(folder)
                except Exception as exc:
                    print(f'Private tmux watcher could not remove {folder}: {exc}', file=sys.stderr)


def server_pids(path):
    """Match the foreground tmux argv in /proc for test assertions."""
    wanted = [os.fsencode(paths.TMUX), b'-D', b'-S', os.fsencode(path), b'-f', b'/dev/null', b'']
    found = []
    for pid in procinfo.pids():
        try:
            if procinfo.argv(pid) == wanted:
                found.append(pid)
        except OSError:
            pass
    return sorted(found)


def _tokens(text):
    return [a or b or c for a, b, c in re.findall(r"""'([^']*)'|"([^"]*)"|([^\s'"(),;]+)""", text)]


def _signatures(tokens):
    """Yield the index before each private-client signature: -S X -f /dev/null, or -f /dev/null -S X."""
    for i, token in enumerate(tokens):
        if token == '-S' and tokens[i + 2:i + 4] == ['-f', '/dev/null']:
            yield i - 1
        elif token == '-f' and tokens[i + 1:i + 3] == ['/dev/null', '-S']:
            yield i - 1


def unguarded_clients(source, filename='<test>'):
    """Lines with a tmux call on a private socket that lacks -N (or -D, the watcher start)."""
    tree = ast.parse(source, filename=filename)
    parts = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            tokens = [item.value if isinstance(item, ast.Constant) else '{}' for item in node.elts]
        elif isinstance(node, ast.JoinedStr):
            tokens = _tokens(''.join(part.value if isinstance(part, ast.Constant) else ' {} ' for part in node.values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in parts:
            tokens = _tokens(node.value)
        else:
            continue
        if any(before < 0 or tokens[before] not in ('-N', '-D') for before in _signatures(tokens)):
            lines.append(node.lineno)
    return lines


class IsolationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_failed_setup_stops_each_fixture_server(self):
        from test_groups import TmuxTests
        from test_pane_zoom import IsolatedZoom
        from test_project_launch import WrapperTests
        from test_side_shells import SideShellTests
        from test_tmux_merge import TmuxMergeTests
        from test_tmux_names import TmuxNameHookTests
        from test_undo import TmuxBase

        for fixture in (TmuxBase, TmuxTests, SideShellTests, TmuxNameHookTests,
                        TmuxMergeTests, WrapperTests, IsolatedZoom):
            with self.subTest(fixture=fixture.__name__):
                class FailedSetup(fixture):
                    def setUp(case):
                        try:
                            super().setUp()
                        finally:
                            if hasattr(case, 'private'):
                                self.addCleanup(self.stop_if_left, case.private)
                        self.assertTrue(server_pids(case.private.socket), 'The fixture must start a server.')
                        raise RuntimeError('setup failed after tmux started')

                    def test_guard(case):
                        self.fail('The test ran after setUp failed.')

                case = FailedSetup('test_guard')
                result = case.run(unittest.TestResult())
                self.assertEqual(len(result.errors), 1, result.errors)
                self.assertIn('RuntimeError: setup failed after tmux started', result.errors[0][1])
                self.assertEqual(server_pids(case.private.socket), [])
                self.assertFalse(os.path.exists(case.private.folder))

    def stop_if_left(self, private):
        if os.path.exists(private.folder):
            watch_request('stop', private.folder)

    def folder(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ttz', dir='/tmp')))

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_stop_uses_the_child_even_when_the_socket_is_replaced(self):
        decoy = private_tmux(self, 'ttz')
        decoy.tmux('new-session', '-d', '-s', 'decoy', '/bin/sh').check_returncode()
        for link in (os.link, os.symlink):
            with self.subTest(link=link.__name__):
                case = unittest.TestCase()
                target = private_tmux(case, 'ttz')
                self.addCleanup(self.stop_if_left, target)
                self.assertEqual(server_pids(target.socket), [target.pid])
                os.unlink(target.socket)
                link(decoy.socket, target.socket)
                self.assertTrue(case.doCleanups())
                self.assert_stopped([(target.folder, target.pid)])
                self.assertEqual(server_pids(decoy.socket), [decoy.pid])
                self.assertEqual(decoy.tmux('display-message', '-p', '#{pid}').stdout.strip(), str(decoy.pid))

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_stop_reaps_a_server_that_already_exited(self):
        case = unittest.TestCase()
        target = private_tmux(case, 'ttz')
        self.addCleanup(self.stop_if_left, target)
        target.tmux('kill-server').check_returncode()
        deadline = time.monotonic() + 5
        while procinfo.stat_fields(target.pid)[0] != 'Z':
            self.assertLess(time.monotonic(), deadline, 'tmux did not exit after kill-server.')
            time.sleep(.02)
        self.assertTrue(case.doCleanups())
        self.assert_stopped([(target.folder, target.pid)])

    def test_stop_escalates_when_sigterm_is_ignored(self):
        child = subprocess.Popen([sys.executable, '-c',
                                  'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                                  'print("ready", flush=True); signal.pause()'], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.kill)
        self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'Child did not install its signal handler.')
        self.assertEqual(child.stdout.readline(), 'ready\n')
        folder = self.folder()
        requests = StringIO(''.join(json.dumps({'op': op, 'folder': str(folder), 'env': {}}) + '\n'
                                    for op in ('start', 'stop')))
        replies = StringIO()
        with mock.patch(f'{__name__}.subprocess.Popen', return_value=child), \
                mock.patch(f'{__name__}._wait_for_server'), mock.patch(f'{__name__}.TERM_TIMEOUT', .05):
            watch(requests, replies)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual([json.loads(line) for line in replies.getvalue().splitlines()],
                         [{'ok': True, 'pid': child.pid}, {'ok': True}])
        self.assertFalse(folder.exists())

    def test_stop_rejects_an_unknown_folder(self):
        folder = self.folder()
        marker = folder / 'keep'
        marker.touch()
        requests = StringIO(json.dumps({'op': 'stop', 'folder': str(folder)}) + '\n')
        replies = StringIO()
        watch(requests, replies)
        self.assertEqual(json.loads(replies.getvalue()), {'ok': False, 'error': 'folder is not owned'})
        self.assertTrue(marker.is_file())

    def test_a_tmux_override_still_gets_the_private_socket_and_no_config(self):
        private = PrivateTmux('/tmp/ttx-example')
        for executable in ('/opt/bin/tmux-3.4', '/usr/bin/tmux'):
            with self.subTest(executable=executable), mock.patch.object(paths, 'TMUX', '/opt/bin/tmux-3.4'), \
                    mock.patch(f'{__name__}.subprocess.run') as run:
                private.run([executable, 'new-session', '-d'])
                self.assertEqual(run.call_args.args[0],
                                 [executable, '-N', '-S', private.socket, '-f', '/dev/null', 'new-session', '-d'])

    def assert_stopped(self, servers, removed=()):
        folders = [folder for folder, _ in servers] + list(removed)
        pids = [pid for _, pid in servers]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(os.path.exists(f'/proc/{pid}') for pid in pids) and not any(map(os.path.exists, folders)):
                break
            time.sleep(.02)
        self.assertEqual([pid for pid in pids if os.path.exists(f'/proc/{pid}')], [], 'The watcher must reap its child.')
        self.assertEqual([folder for folder in folders if os.path.exists(folder)], [])

    def run_killed(self, code, *args, pass_fds=()):
        with (self.folder() / 'stderr').open('w+') as log:
            child = subprocess.run([sys.executable, '-c', code, HERE, *map(os.fspath, args)], env=_tmux_env(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                   start_new_session=True, pass_fds=pass_fds, timeout=30)
            log.seek(0)
            if child.returncode == 77:
                self.skipTest(log.read())
            self.assertEqual(child.returncode, -signal.SIGKILL, log.read())

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_watcher_stops_servers_of_a_process_group_killed_by_timeout(self):
        record = self.folder() / 'servers'
        code = IMPORT_WATCH + """
import json, os, signal, unittest
from pathlib import Path
private = w.private_tmux(unittest.TestCase(), 'tkid')
Path(sys.argv[2]).write_text(json.dumps([[private.folder, private.pid]]))
os.killpg(0, signal.SIGKILL)
"""
        self.run_killed(code, record)
        self.assert_stopped(json.loads(record.read_text()))

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_watcher_kills_a_stopped_server_after_the_runner_is_killed(self):
        record = self.folder() / 'servers'
        code = IMPORT_WATCH + """
import json, os, signal, time, unittest
from pathlib import Path
private = w.private_tmux(unittest.TestCase(), 'tkid')
os.kill(private.pid, signal.SIGSTOP)
deadline = time.monotonic() + 5
while w.procinfo.stat_fields(private.pid)[0] != 'T':
    if time.monotonic() >= deadline:
        raise RuntimeError('tmux did not stop')
    time.sleep(.02)
Path(sys.argv[2]).write_text(json.dumps([[private.folder, private.pid]]))
os.killpg(0, signal.SIGKILL)
"""
        self.run_killed(code, record)
        self.assert_stopped(json.loads(record.read_text()))

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_watcher_acts_when_the_runner_exits_even_unreaped(self):
        record = self.folder() / 'servers'
        code = IMPORT_WATCH + """
import json, os, unittest
from pathlib import Path
private = w.private_tmux(unittest.TestCase(), 'tkid')
Path(sys.argv[2]).write_text(json.dumps([[private.folder, private.pid]]))
os._exit(0)
"""
        child = subprocess.Popen([sys.executable, '-c', code, HERE, str(record)], env=_tmux_env(),
                                 stdin=subprocess.DEVNULL)
        self.addCleanup(child.wait, 20)
        deadline = time.monotonic() + 20
        while procinfo.stat_fields(child.pid)[0] != 'Z' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(procinfo.stat_fields(child.pid)[0], 'Z')
        self.assert_stopped(json.loads(record.read_text()))
        self.assertEqual(procinfo.stat_fields(child.pid)[0], 'Z')

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_watcher_stops_the_verifier_tmux_when_boot_is_killed_before_ready(self):
        record = self.folder() / 'servers'
        code = IMPORT_WATCH + r"""
import json, os, select, signal, unittest
from pathlib import Path
import test_verify_server
case = test_verify_server.Boot('test_serves_fixtures_only_and_cleans_up_on_signal')
try:
    case.setUp()
except unittest.SkipTest as exc:
    print(exc, file=sys.stderr)
    sys.exit(77)
# The detached HTTP child waits for its test-owned control socket before it serves.
gate = '''
import subprocess, sys
sys.path.insert(0, sys.argv.pop(1))
fd = int(sys.argv.pop(1))
import verify_server
popen = subprocess.Popen
before_ready = (
    "import os, runpy, sys; fd = int(sys.argv.pop(1)); sys.argv.pop(0); "
    "print('before serve', flush=True); "
    "os.read(fd, 1) and runpy.run_path(sys.argv[0], run_name='__main__')"
)

def held(args, **kwargs):
    if '--serve' in args:
        return popen([*args[:2], '-c', before_ready, str(fd), *args[2:]], pass_fds=(fd,), **kwargs)
    return popen(args, **kwargs)

subprocess.Popen = held
sys.exit(verify_server.main())
'''
fd = int(sys.argv[3])
process, root = case.launch(('-c', gate, str(Path(sys.argv[1]) / 'dev'), str(fd)), pass_fds=(fd,))
if not select.select([process.stdout], [], [], 15)[0]:
    raise RuntimeError('HTTP child did not reach its readiness gate')
if process.stdout.readline() != b'before serve\n':
    raise RuntimeError(process.stderr.read().decode())
servers = []
for path in (case.decoy, root / 'tmux.sock'):
    pid, = w.server_pids(path)
    servers.append([str(path.parent), pid])
Path(sys.argv[2]).write_text(json.dumps([servers, str(case.dist)]))
os.killpg(0, signal.SIGKILL)
"""
        control, child_end = socket.socketpair()
        self.addCleanup(self.stop_verifier_child, control)
        with child_end:
            fd = child_end.fileno()
            self.run_killed(code, record, str(fd), pass_fds=(fd,))
        servers, dist = json.loads(record.read_text())
        self.assert_stopped(servers, removed=[dist])

    def stop_verifier_child(self, control):
        with control:
            control.shutdown(socket.SHUT_WR)
            control.settimeout(5)
            self.assertEqual(control.recv(1), b'')

    def test_private_tmux_clients_cannot_start_a_server(self):
        # This file holds the guard samples; a mock test checks the argv of PrivateTmux.run.
        found = [f'{file.name}:{line}' for file in sorted(Path(__file__).parent.glob('test_*.py'))
                 if file.name != Path(__file__).name
                 for line in unguarded_clients(file.read_text(encoding='utf-8'), str(file))]
        self.assertEqual(found, [], 'Add -N so a test client cannot start an unwatched tmux server.')

    def test_client_guard_checks_each_tmux_call(self):
        samples = {
            "subprocess.run([tmux, '-S', sock, '-f', '/dev/null', 'ls'])": [1],
            "subprocess.run([tmux, '-N', '-f', '/dev/null', '-S', sock, 'ls'])": [],
            "subprocess.run([tmux, '-D', '-S', sock, '-f', '/dev/null'])": [],
            'code = """run([a, "-S", s, "-f", "/dev/null"])"""': [1],
            "code = 'tmux -N -S s -f /dev/null ls; tmux -S s -f /dev/null ls'": [1],
            "cmd = f'{tmux} -S {sock} -f /dev/null ls'": [1],
            "cmd = f'{tmux} -N -S {sock} -f /dev/null ls'": [],
            "subprocess.run(['curl', '-S', '-o', '/dev/null', url])": [],
            "script = '[ \"$1\" = -S ] && [ \"$3\" = -f ] && [ \"$4\" = /dev/null ]'": [],
        }
        for source, lines in samples.items():
            with self.subTest(source=source):
                self.assertEqual(unguarded_clients(source), lines)

    def test_real_tmux_tests_use_cleanup_instead_of_teardown_or_finally(self):
        for file in sorted(Path(__file__).parent.glob('test_*.py')):
            if file.name == Path(__file__).name:
                continue
            source = file.read_text(encoding='utf-8')
            if 'new-session' not in source:
                continue
            tree = ast.parse(source, filename=str(file))
            calls = (node for node in ast.walk(tree) if isinstance(node, ast.Call))
            if not any(ast.unparse(call.func) == 'private_tmux'
                       or (ast.unparse(call.func) == 'shutil.which' and call.args
                           and ast.unparse(call.args[0]) in ("'tmux'", 'paths.TMUX'))
                       for call in calls):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == 'tearDown':
                    cleanup = node.body
                elif isinstance(node, (ast.Try, ast.TryStar)):
                    cleanup = node.finalbody
                else:
                    continue
                for statement in cleanup:
                    for child in ast.walk(statement):
                        if isinstance(child, ast.Constant) and isinstance(child.value, str) and 'kill-server' in child.value:
                            self.fail(f'{file.name}:{child.lineno}: register tmux cleanup before setup can fail')
