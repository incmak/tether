"""Private tmux servers and checks for their test lifecycle."""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import ast
import os
from pathlib import Path
import re
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


class PrivateTmux:
    def __init__(self, folder, env=None):
        self.folder = folder
        self.socket = os.path.join(folder, 'tmux.sock')
        self.env = _tmux_env({**os.environ, **(env or {}), 'TMUX_TMPDIR': folder})

    def run(self, argv, **kwargs):
        kwargs.setdefault('env', self.env)
        if os.path.basename(argv[0]) == 'tmux':
            argv = [argv[0], '-S', self.socket, '-f', '/dev/null', *argv[1:]]
            kwargs['env'] = _tmux_env(kwargs['env'])
            kwargs['env']['TMUX_TMPDIR'] = self.folder
        return subprocess.run(argv, **kwargs)

    def tmux(self, *args):
        return self.run([paths.TMUX, *args], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)


FOLDER_PREFIX = 'ttx-'  # Every private test server folder starts with this, so a later run can find leftovers.
OWNER = 'owner'  # "pid start" of the test process that made the folder.
_reaped = False


def _owner_alive(text):
    try:
        pid, start = text.split()
        return str(procinfo.start_ticks(int(pid))) == start
    except (OSError, ValueError, IndexError):
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
        if _owner_alive(owner):
            continue
        stop_server(str(folder / 'tmux.sock'))
        shutil.rmtree(folder, ignore_errors=True)


def private_tmux(case, prefix, env=None):
    global _reaped
    if not _reaped:
        _reaped = True
        reap_dead_runs()
    # Short paths fit in a Unix socket address.
    folder = tempfile.mkdtemp(prefix=FOLDER_PREFIX + prefix, dir='/tmp')
    case.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    Path(folder, OWNER).write_text(f'{os.getpid()} {procinfo.start_ticks(os.getpid())}')
    tmux = PrivateTmux(folder, env)
    case.addCleanup(stop_server, tmux.socket)
    return tmux


def _matches_socket(pid, socket):
    try:
        argv = procinfo.argv(pid)
    except OSError:  # The process exited or belongs to another user.
        return False
    return bool(argv and os.path.basename(argv[0]) == b'tmux' and socket in argv[1:])


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
    env = _tmux_env({**os.environ, 'TMUX_TMPDIR': os.path.dirname(socket)})
    try:
        subprocess.run([paths.TMUX, '-S', socket, '-f', '/dev/null', 'kill-server'],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def stop_server(socket):
    socket = os.fspath(socket)
    _request_stop(socket)
    for pid in server_pids(socket):
        if _matches_socket(pid, os.fsencode(socket)):
            try:
                os.kill(pid, signal.SIGKILL)
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
    def test_next_run_stops_the_server_of_a_killed_run(self):
        """SIGTERM or SIGKILL from `timeout` skips every cleanup; the next run must stop that server."""
        root = tempfile.mkdtemp(prefix='ttr', dir='/tmp')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        # The child owns the folder like private_tmux does, starts a server, and dies without cleanups.
        code = ('import os, subprocess, sys\n'
                'sys.path.insert(0, sys.argv[3])\n'
                'import procinfo\n'
                'folder, owner = sys.argv[1], sys.argv[2]\n'
                "owner = owner or f'{os.getpid()} {procinfo.start_ticks(os.getpid())}'\n"
                "open(os.path.join(folder, 'owner'), 'w').write(owner)\n"
                "subprocess.run([sys.argv[4], '-S', os.path.join(folder, 'tmux.sock'), '-f', '/dev/null',\n"
                "                'new-session', '-d', '/bin/sh'], check=True, timeout=5, stdin=subprocess.DEVNULL)\n"
                'os.kill(os.getpid(), 9)\n')
        dead = os.path.join(root, FOLDER_PREFIX + 'dead')
        live = os.path.join(root, FOLDER_PREFIX + 'live')
        for folder, owner in ((dead, ''), (live, f'{os.getpid()} {procinfo.start_ticks(os.getpid())}')):
            os.mkdir(folder)
            self.addCleanup(stop_server, os.path.join(folder, 'tmux.sock'))
            child = subprocess.run([sys.executable, '-c', code, folder, owner, str(Path(__file__).parent), paths.TMUX],
                                   env=_tmux_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        self.assertTrue(server_pids(os.path.join(dead, 'tmux.sock')))
        live_pids = server_pids(os.path.join(live, 'tmux.sock'))
        self.assertTrue(live_pids)
        reap_dead_runs(root)
        self.assertEqual(server_pids(os.path.join(dead, 'tmux.sock')), [])
        self.assertFalse(os.path.exists(dead))
        self.assertEqual(server_pids(os.path.join(live, 'tmux.sock')), live_pids)  # A live run keeps its server.

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
