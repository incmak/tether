"""Private tmux servers and checks for their test lifecycle."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import ast
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import paths
import procinfo


def _tmux_env(env=None):
    result = {**(os.environ if env is None else env)}
    result.pop('TMUX', None)
    result.pop('TMUX_PANE', None)
    return result


HERE = os.path.dirname(os.path.abspath(__file__))


class PrivateTmux:
    def __init__(self, folder, env=None):
        self.folder = folder
        self.socket = os.path.join(folder, 'tmux.sock')
        self.env = _tmux_env({**os.environ, **(env or {}), 'TMUX_TMPDIR': folder})

    def run(self, argv, **kwargs):
        kwargs.setdefault('env', self.env)
        if argv and (argv[0] == paths.TMUX or os.path.basename(argv[0]) == 'tmux'):  # paths.TMUX may be an override
            argv = [argv[0], '-S', self.socket, '-f', '/dev/null', *argv[1:]]
            kwargs['env'] = _tmux_env(kwargs['env'])
            kwargs['env']['TMUX_TMPDIR'] = self.folder
        return subprocess.run(argv, **kwargs)

    def tmux(self, *args):
        return self.run([paths.TMUX, *args], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)


FOLDER_PREFIX = 'ttx-'  # Every private test server folder starts with this, so a later run can find leftovers.
OWNER = 'owner'  # "pid start" of the test process that made the folder.
_started = False


def _identity(pid):
    return f'{pid} {procinfo.start_ticks(pid)}'


def _owner_state(text):
    """True: the owner runs. False: it is gone. None: not a whole record (being written, or not ours)."""
    try:
        pid, start = text.split()
        pid = int(pid)
    except ValueError:
        return None
    try:
        return str(procinfo.start_ticks(pid)) == start
    except (OSError, IndexError):
        return False


def reap_dead_runs(root='/tmp'):
    """Stop the servers of test runs that died before their cleanups ran (SIGKILL, timeout, crash)."""
    for folder in Path(root).glob(FOLDER_PREFIX + '*'):
        try:
            if folder.is_symlink() or not folder.is_dir() or folder.stat().st_uid != os.getuid():
                continue
            owner = (folder / OWNER).read_text()
        except OSError:
            continue  # Another run may be making or removing it.
        if _owner_state(owner) is not False:
            continue
        stop_server(str(folder / 'tmux.sock'))
        shutil.rmtree(folder, ignore_errors=True)


def watch(pid, start):
    """Run detached: wait until the test process ends, then stop the servers its cleanups did not stop."""
    owner = f'{pid} {start}'
    if hasattr(os, 'pidfd_open'):
        try:
            handle = os.pidfd_open(pid)
        except ProcessLookupError:
            handle = None
        if handle is not None and _owner_state(owner):  # Checked after the open, so the handle is the owner.
            select.select([handle], [], [])
    while _owner_state(owner):  # No pidfd (macOS): check once a second.
        time.sleep(1)
    reap_dead_runs()


WATCH = ("import sys, types\n"
         "sys.modules['unittest_isolated'] = types.ModuleType('unittest_isolated')  # no isolate() here\n"
         "sys.path.insert(0, sys.argv[1])\n"
         "import test_tmux_isolation\n"
         "test_tmux_isolation.watch(int(sys.argv[2]), sys.argv[3])\n")
DETACH = ("import subprocess, sys\n"
          "subprocess.Popen(sys.argv[1:], start_new_session=True, stdin=subprocess.DEVNULL,\n"
          "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")


def _start_watch():
    """A process outside the runner's group stops this run's servers if `timeout` or a crash skips the cleanups."""
    pid, start = _identity(os.getpid()).split()
    subprocess.run([sys.executable, '-c', DETACH, sys.executable, '-c', WATCH, HERE, pid, start],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True)


def private_tmux(case, prefix, env=None):
    global _started
    if not _started:
        _started = True
        reap_dead_runs()
        _start_watch()
    # Short paths fit in a Unix socket address.
    folder = tempfile.mkdtemp(prefix=FOLDER_PREFIX + prefix, dir='/tmp')
    case.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    draft = Path(folder, OWNER + '.new')
    draft.write_text(_identity(os.getpid()))
    os.replace(draft, Path(folder, OWNER))  # A reader sees no record or a whole one.
    tmux = PrivateTmux(folder, env)
    case.addCleanup(stop_server, tmux.socket)
    return tmux


def _matches_socket(pid, socket):
    """A tmux process started with exactly `-S socket` (not a mention of the path elsewhere)."""
    try:
        argv = procinfo.argv(pid)
    except OSError:  # The process exited or belongs to another user.
        return False
    if not argv or not os.path.basename(argv[0]).startswith(b'tmux'):
        return False
    return any(flag == b'-S' and value == socket for flag, value in zip(argv[1:], argv[2:]))


def server_pids(socket):
    socket = os.fspath(socket)
    if Path('/proc').is_dir():
        candidates = procinfo.pids()
    else:
        found = subprocess.run(['pgrep', '-f', '--', re.escape(socket)], capture_output=True, text=True, timeout=1)
        if found.returncode == 1:
            return []
        found.check_returncode()
        candidates = [int(pid) for pid in found.stdout.split()]
    return sorted(pid for pid in candidates if _matches_socket(pid, os.fsencode(socket)))


def _request_stop(socket):
    if os.path.islink(socket):
        return  # A link could lead to another server, such as the user's.
    env = _tmux_env({**os.environ, 'TMUX_TMPDIR': os.path.dirname(socket)})
    try:
        subprocess.run([paths.TMUX, '-S', socket, '-f', '/dev/null', 'kill-server'],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _kill(pid, socket):
    """SIGKILL pid only while it still is the server of socket. A pidfd pins the process, so a reused pid is safe."""
    if not hasattr(os, 'pidfd_open'):
        if _matches_socket(pid, socket):
            os.kill(pid, signal.SIGKILL)
        return
    handle = os.pidfd_open(pid)
    try:
        if _matches_socket(pid, socket):  # Checked after the open: the handle is this process.
            signal.pidfd_send_signal(handle, signal.SIGKILL)
    finally:
        os.close(handle)


def stop_server(socket):
    socket = os.fspath(socket)
    _request_stop(socket)
    for pid in server_pids(socket):
        try:
            _kill(pid, os.fsencode(socket))
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 5
    while pids := server_pids(socket):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f'Private tmux still runs on {socket}: {pids}')
        time.sleep(min(.02, remaining))


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
                                self.addCleanup(stop_server, case.private.socket)
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

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_stop_kills_only_its_server_when_kill_server_does_nothing(self):
        target = private_tmux(self, 'tstop-')
        decoy = private_tmux(self, 'tother-')
        for tmux in (target, decoy):
            tmux.tmux('new-session', '-d', '/bin/sh').check_returncode()
            self.assertTrue(server_pids(tmux.socket))
        decoy_pids = server_pids(decoy.socket)
        with mock.patch(f'{__name__}._request_stop'):
            stop_server(target.socket)
        self.assertEqual(server_pids(target.socket), [])
        self.assertEqual(server_pids(decoy.socket), decoy_pids)

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_watch_stops_the_server_when_the_runner_is_killed(self):
        """SIGTERM or SIGKILL from `timeout` skips every cleanup; the run's watch process stops the server."""
        out = tempfile.mkdtemp(prefix='ttw', dir='/tmp')
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        code = WATCH.split('test_tmux_isolation.watch')[0] + (
            'import os, unittest\n'
            'case = unittest.TestCase()\n'
            "private = test_tmux_isolation.private_tmux(case, 'kid')\n"
            "private.tmux('new-session', '-d', '/bin/sh').check_returncode()\n"
            "open(sys.argv[2], 'w').write(private.socket)\n"
            'os.kill(os.getpid(), 9)\n')
        record = os.path.join(out, 'socket')
        child = subprocess.run([sys.executable, '-c', code, HERE, record], env=_tmux_env(),
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        socket = Path(record).read_text()
        self.addCleanup(stop_server, socket)  # Only if the watch fails.
        deadline = time.monotonic() + 10
        while (server_pids(socket) or os.path.exists(os.path.dirname(socket))) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(server_pids(socket), [])
        self.assertFalse(os.path.exists(os.path.dirname(socket)))

    @unittest.skipUnless(shutil.which(paths.TMUX), 'tmux is not installed')
    def test_reap_stops_only_dead_owners(self):
        root = tempfile.mkdtemp(prefix='ttr', dir='/tmp')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        servers = {}
        for name, owner in (('dead', '1 0'), ('live', _identity(os.getpid())), ('partial', ''), ('linked', '1 0')):
            folder = os.path.join(root, FOLDER_PREFIX + name)
            os.mkdir(folder)
            Path(folder, OWNER).write_text(owner)
            if name == 'linked':  # A dead owner whose socket name leads to another server.
                os.symlink(servers['live'], os.path.join(folder, 'tmux.sock'))
                continue
            servers[name] = os.path.join(folder, 'tmux.sock')
            self.addCleanup(stop_server, servers[name])
            started = subprocess.run([paths.TMUX, '-S', servers[name], '-f', '/dev/null', 'new-session', '-d', '/bin/sh'],
                                     env=_tmux_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            self.assertEqual(started.returncode, 0, started.stderr)
        before = {name: server_pids(socket) for name, socket in servers.items()}
        self.assertTrue(all(before.values()))
        reap_dead_runs(root)
        self.assertEqual(server_pids(servers['dead']), [])
        self.assertFalse(os.path.exists(os.path.dirname(servers['dead'])))
        self.assertEqual(server_pids(servers['live']), before['live'])  # A live run keeps its server,
        self.assertEqual(server_pids(servers['partial']), before['partial'])  # a half-written owner is not "dead",
        self.assertTrue(os.path.exists(os.path.join(root, FOLDER_PREFIX + 'partial')))  # and a link is never followed.

    def test_only_the_socket_flag_marks_a_server(self):
        target = os.path.join(tempfile.mkdtemp(prefix='ttm', dir='/tmp'), 'tmux.sock')
        self.addCleanup(shutil.rmtree, os.path.dirname(target), ignore_errors=True)
        # argv is "tmux -c 'sleep 60; :' <target>": a tmux name that mentions the path, not as -S.
        other = subprocess.Popen(['tmux', '-c', 'sleep 60; :', target], executable='/bin/sh', stdin=subprocess.DEVNULL)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        deadline = time.monotonic() + 5
        while os.fsencode(target) not in procinfo.argv(other.pid) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn(os.fsencode(target), procinfo.argv(other.pid))
        self.assertEqual(server_pids(target), [])
        with mock.patch(f'{__name__}._request_stop'):
            stop_server(target)
        self.assertIsNone(other.poll())

    def test_a_tmux_override_still_gets_the_private_socket_and_no_config(self):
        private = PrivateTmux('/tmp/ttx-example')
        with mock.patch.object(paths, 'TMUX', '/opt/bin/tmux-3.4'), \
                mock.patch(f'{__name__}.subprocess.run') as run:
            private.tmux('new-session', '-d')
        self.assertEqual(run.call_args.args[0][:5], ['/opt/bin/tmux-3.4', '-S', private.socket, '-f', '/dev/null'])

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
