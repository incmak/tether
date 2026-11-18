"""Release tests use temporary homes, fake systemctl, and random HTTP ports."""
import contextlib
import hashlib
import http.client
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import updates
REAL_PATH = os.environ.get('PATH', '')
import test_install as install_tests

installer = install_tests.installer

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('tether_cli', str(ROOT / 'tether'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        with updates._LOCK:
            updates._RUNNING = False
            updates._ATTEMPTED.clear()

    def test_version_comparison(self):
        self.assertLess(updates.version('v0.9.0'), updates.version('1.0.0'))
        self.assertLess(updates.version('1.0.0-rc2'), updates.version('1.0.0-rc10'))
        self.assertLess(updates.version('1.0.0-rc10'), updates.version('1.0.0'))
        for invalid in ('../1.0.0', 'v1', '1.0.0/foo', '1.0.0;echo x'):
            with self.assertRaises(ValueError):
                updates.version(invalid)

    def test_daily_cache_and_quiet_failure(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp, mock.patch.object(updates.paths, 'CACHE', Path(temp)), \
                mock.patch.object(updates, 'mode', return_value='notify'), \
                mock.patch.object(updates, 'fetch_release', return_value={
                    'tag_name': 'v0.2.0', 'html_url': 'https://example.invalid/release', 'body': 'Changes'}) as fetch:
            with mock.patch.object(updates.threading, 'Thread') as thread:
                first = updates.payload()
                self.assertIsNone(first['latest'])
                self.assertTrue(first['checking'])
                self.assertTrue(updates.payload()['checking'])
                thread.assert_called_once()
                args = thread.call_args.kwargs['args']
                updates.refresh(*args)
                cached = updates.payload()
                self.assertTrue(cached['available'])
                self.assertFalse(cached['checking'])
                self.assertEqual(cached['notes'], 'Changes')
                thread.assert_called_once()
            fetch.assert_called_once()
            file = Path(temp) / 'updates.json'
            cache = json.loads(file.read_text())
            cache['checked_at'] = time.time() - updates.DAY - 1
            file.write_text(json.dumps(cache))
            updates._ATTEMPTED.clear()
            with mock.patch.object(updates, 'fetch_release', side_effect=OSError('offline')):
                updates.refresh(file, cache)
            self.assertEqual(updates.read_cache(file)['latest'], 'v0.2.0')
            self.assertGreater(updates.read_cache(file)['checked_at'], cache['checked_at'])
            with mock.patch.object(updates.threading, 'Thread') as thread:
                self.assertFalse(updates.payload()['checking'])
                thread.assert_not_called()

    def test_request_returns_while_network_waits(self):
        from tempfile import TemporaryDirectory
        started, finish = threading.Event(), threading.Event()
        workers = []
        real_thread = threading.Thread

        def fetch():
            started.set()
            finish.wait(2)
            raise OSError('offline')

        def thread(**kwargs):
            worker = real_thread(**kwargs)
            workers.append(worker)
            return worker

        with TemporaryDirectory() as temp, mock.patch.object(updates.paths, 'CACHE', Path(temp)), \
                mock.patch.object(updates, 'mode', return_value='notify'), \
                mock.patch.object(updates, 'fetch_release', side_effect=fetch), \
                mock.patch.object(updates.threading, 'Thread', side_effect=thread):
            try:
                self.assertTrue(updates.payload()['checking'])
                self.assertTrue(started.wait(1))
                self.assertFalse(finish.is_set())
                updates.payload()
                self.assertEqual(len(workers), 1)
            finally:
                finish.set()
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())

    def test_off_never_checks(self):
        with mock.patch.object(updates, 'mode', return_value='off'), \
                mock.patch.object(updates.threading, 'Thread') as thread:
            self.assertEqual(updates.payload()['mode'], 'off')
            self.assertFalse(updates.payload()['checking'])
            thread.assert_not_called()

    def test_route_payload_and_auth(self):
        import server
        handler = object.__new__(server.Handler)
        handler.path = '/mobile/api/update'
        handler.deny = mock.Mock(return_value=False)
        handler.respond = mock.Mock()
        expected = dict(current='0.1.0', latest='v0.2.0', available=True, url='https://example.invalid',
                        notes='Changes', checked_at=1, mode='notify', checking=False)
        with mock.patch.object(updates, 'payload', return_value=expected) as payload:
            handler.do_GET()
            handler.respond.assert_called_once_with(200, expected)
            handler.deny.return_value = True
            handler.do_GET()
            payload.assert_called_once()

    def test_release_actions_are_pinned(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        actions = [line.strip() for line in workflow.splitlines() if 'uses:' in line]
        self.assertEqual(len(actions), 4)
        for action in actions:
            self.assertRegex(action, r'^- uses: actions/[a-z-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$')

    def test_banner_refetches_only_checking_and_stops(self):
        # Run the real component with fake React, requests, storage, and timers.
        node = shutil.which('node')
        if not node or not (ROOT / 'ui/node_modules/typescript').is_dir():
            self.skipTest('UI dependencies and Node are required for the banner test')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('./ui/node_modules/typescript');
const source = fs.readFileSync('./ui/src/UpdateBanner.tsx', 'utf8');
const code = ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX
}}).outputText;
async function scenario(payloads, expectedCalls, disabledStorage = false) {
  let calls = 0, reloadCalls = 0, subscribe, snapshot;
  const timers = [], listeners = [];
  const storage = new Map();
  const context = {exports: {}, setTimeout: (fn, ms) => {
    assert.equal(ms, 15000); timers.push(fn);
  }, localStorage: {
    getItem: key => { if (disabledStorage) throw Error('disabled'); return storage.get(key) ?? null; },
    setItem: (key, value) => { if (disabledStorage) throw Error('disabled'); storage.set(key, value); }
  }, require: name => {
    if (name === 'react') return {useSyncExternalStore: (sub, get) => {
      subscribe = sub; snapshot = get; return get();
    }};
    if (name === './api') return {errorText: error => error.message, request: async path => {
      assert.equal(path, 'update'); const data = payloads[Math.min(calls++, payloads.length - 1)];
      if (data instanceof Error) throw data;
      return data;
    }};
    if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})};
    if (name === 'lucide-react') return {X: 'X'};
    if (name === './Skeleton') return {Spinner: 'Spinner'};
    if (name === './backNav') return {reloadPage: () => { reloadCalls++; }};
    throw Error(name);
  }};
  vm.runInNewContext(code, context);
  const banner = context.exports.UpdateBanner;
  banner();
  const unsubscribe = subscribe(() => listeners.push(snapshot()));
  subscribe(() => {}); // Multiple mounts still share one request.
  const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
  await flush();
  let limit = 5;
  while (timers.length && limit--) { timers.shift()(); await flush(); }
  assert.equal(timers.length, 0);
  assert.equal(calls, expectedCalls);
  if (snapshot()) {
    const view = banner();
    assert.equal(view.props.children[0].props.children.join(''), 'Update available: v0.2.0');
    view.props.children[1].props.onClick();
    assert.equal(snapshot(), null);
    assert.equal(banner(), null);
  }
  unsubscribe(); subscribe(() => {}); await flush();
  assert.equal(calls, expectedCalls); // Resubscription never restarts discovery.
  assert.equal(reloadCalls, 0);
}
const pending = {latest: null, available: false, url: '', mode: 'notify', checking: true};
const complete = {latest: 'v0.2.0', available: true, url: 'https://example.invalid', mode: 'notify', checking: false};
(async () => {
  await scenario([pending, complete], 2);
  await scenario([pending], 4);
  await scenario([complete], 1);
  await scenario([complete], 1, true);
  await scenario([{...pending, mode: 'off'}], 1);
  await scenario([Error('offline')], 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, '-'], input=script, text=True, cwd=ROOT, timeout=30,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class UpdateRunTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.cache = self.home / 'cache'
        self.root = self.home / '.local/share/tether/releases/0.1.0'
        self.root.mkdir(parents=True)
        (self.root / 'VERSION').write_text('0.1.0\n')
        self.state = self.home / 'install.json'
        self.state.write_text('{}')
        for patcher in (mock.patch.object(updates.paths, 'HOME', self.home),
                        mock.patch.object(updates.paths, 'CACHE', self.cache),
                        mock.patch.object(updates, 'ROOT', self.root),
                        mock.patch.object(updates, 'install_file', return_value=self.state),
                        mock.patch.object(updates.subprocess, 'run'),
                        mock.patch.object(updates.subprocess, 'Popen')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cache.mkdir()
        (self.cache / 'updates.json').write_text(json.dumps(dict(latest='v0.2.0', checked_at=time.time())))

    def running(self, **overrides):
        return dict(dict(state='running', target='0.2.0', error='', started_at=time.time()), **overrides)

    def refused(self, status, message, target='0.2.0'):
        with mock.patch.object(updates, 'launch') as launch:
            with self.assertRaises(updates.UpdateRefused) as caught:
                updates.start(target)
            self.assertEqual((caught.exception.status, str(caught.exception)), (status, message))
            launch.assert_not_called()

    def test_payload_preserves_keys_and_adds_run_and_capability(self):
        data = self.running()
        updates.write_run(data)
        payload = updates.payload()
        self.assertEqual(set(payload), {'current', 'latest', 'available', 'url', 'notes', 'checked_at',
                                       'mode', 'checking', 'can_update', 'run'})
        self.assertTrue(payload['can_update'])
        self.assertEqual(payload['run'], data)

    def test_only_managed_release_can_update(self):
        self.assertTrue(updates.can_update())
        for marker in ('directory', 'file', 'broken-link'):
            with self.subTest(marker=marker):
                git = self.root / '.git'
                if marker == 'directory':
                    git.mkdir()
                elif marker == 'file':
                    git.write_text('gitdir: elsewhere')
                else:
                    git.symlink_to(self.home / 'missing')
                self.assertFalse(updates.can_update())
                git.rmdir() if git.is_dir() else git.unlink()
        with mock.patch.object(updates, 'ROOT', ROOT):
            self.assertFalse(updates.can_update())
        with mock.patch.object(updates.paths, 'HOME', self.home / 'elsewhere'):
            self.assertFalse(updates.can_update())
        (self.root / 'VERSION').write_text('0.0.9')
        self.assertFalse(updates.can_update())
        (self.root / 'VERSION').write_text('0.1.0')
        for content in ('broken', 'null', '[]'):
            self.state.write_text(content)
            self.assertFalse(updates.can_update())
        self.state.unlink()
        self.assertFalse(updates.can_update())

    def test_run_file_is_private_atomic_and_validated(self):
        self.assertEqual(updates.run_file(), self.cache / 'update-run.json')
        self.assertIsNone(updates.read_run())
        data = self.running()
        updates.write_run(data)
        self.assertEqual(updates.read_run(), data)
        self.assertEqual(updates.run_file().stat().st_mode & 0o777, 0o600)
        updates.write_run(self.running(state='done'))
        self.assertEqual(updates.read_run()['state'], 'done')
        self.assertEqual(list(self.cache.glob('.update-run-*')), [])
        before = updates.run_file().read_bytes()
        with mock.patch.object(updates.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                updates.write_run(data)
        self.assertEqual(updates.run_file().read_bytes(), before)
        self.assertEqual(list(self.cache.glob('.update-run-*')), [])
        for invalid in ('{', 'null', '[]', '{}', json.dumps(self.running(state='unknown')),
                        json.dumps(self.running(target='../0.2.0')), json.dumps(self.running(error=None)),
                        json.dumps(self.running(started_at='now')), json.dumps(self.running(started_at=True)),
                        json.dumps(self.running(started_at=float('nan')))):
            with self.subTest(invalid=invalid):
                updates.run_file().write_text(invalid)
                self.assertIsNone(updates.read_run())

    def test_stale_running_reads_failed_and_can_retry(self):
        data = self.running(started_at=time.time() - 901)
        updates.write_run(data)
        self.assertEqual(updates.read_run(), dict(data, state='failed',
                         error='The update stopped without a result. Run tether update on the host.'))
        with mock.patch.object(updates, 'launch'):
            self.assertEqual(updates.start('0.2.0')['state'], 'running')

    def test_each_refusal(self):
        with mock.patch.object(updates, 'mode', return_value='off'):
            self.refused(409, 'Updates are disabled.')
        with mock.patch.object(updates, 'can_update', return_value=False):
            self.refused(409, 'This install updates with git pull.')
        for target in (None, 2, {}, ['0.2.0']):
            self.refused(400, 'Choose a release version.', target)
        for target in ('garbage', '../0.2.0', '0.3.0', '0.1.0'):
            self.refused(409, 'That update is no longer available.', target)
        for latest in ('v0.1.0', 'v0.0.9', None):
            (self.cache / 'updates.json').write_text(json.dumps(dict(latest=latest, checked_at=time.time())))
            self.refused(409, 'That update is no longer available.', latest or '0.2.0')

    def test_double_start_and_discovery_lock_is_free_during_launch(self):
        def launch(target):
            self.assertEqual(target, '0.2.0')
            self.assertEqual(updates.read_run()['state'], 'running')
            self.assertTrue(updates._LOCK.acquire(blocking=False))
            updates._LOCK.release()
        with mock.patch.object(updates, 'launch', side_effect=launch) as launch_mock:
            data = updates.start('v0.2.0')
            self.assertEqual(data, updates.read_run())
            self.refused(409, 'An update is already running.')
            launch_mock.assert_called_once_with('0.2.0')

    def test_concurrent_start_launches_only_once(self):
        barrier = threading.Barrier(3, timeout=3)
        results = []
        def start():
            barrier.wait()
            try:
                results.append(updates.start('0.2.0')['state'])
            except updates.UpdateRefused as error:
                results.append(error.status)
        workers = [threading.Thread(target=start) for _ in range(2)]
        with mock.patch.object(updates, 'launch') as launch:
            try:
                for worker in workers:
                    worker.start()
                barrier.wait()
            finally:
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
            self.assertCountEqual(results, ['running', 409])
            launch.assert_called_once()

    def test_launch_failure_records_failed(self):
        with mock.patch.object(updates, 'launch', side_effect=OSError('launch failed')):
            with self.assertRaises(updates.UpdateRefused) as caught:
                updates.start('0.2.0')
        self.assertEqual((caught.exception.status, str(caught.exception)), (500, 'launch failed'))
        self.assertEqual(updates.read_run()['state'], 'failed')
        self.assertEqual(updates.read_run()['error'], 'launch failed')

    def test_systemd_launch_environment_and_failure(self):
        env = dict(PATH='/tools with spaces', TETHER_INSTALL_STATE='/state with spaces', TETHER_REPO='test/repo',
                   MOBILE_TMUX_CACHE='/cache', XDG_RUNTIME_DIR='/runtime', UNRELATED='private')
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(updates.sys, 'platform', 'linux'), \
                mock.patch.object(updates.paths, 'SYSTEMD_RUN', '/tools/systemd-run'), \
                mock.patch.object(updates.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '', '')
            updates.launch('0.2.0')
            argv = run.call_args.args[0]
            self.assertEqual(argv[:5], ['/tools/systemd-run', '--user', '--collect', '--quiet',
                                       '--unit=tether-self-update'])
            self.assertNotIn('--scope', argv)
            for key, value in env.items():
                if key != 'UNRELATED':
                    self.assertIn('--setenv=' + key + '=' + value, argv)
            self.assertNotIn('--setenv=UNRELATED=private', argv)
            self.assertEqual(argv[-7:], [updates.sys.executable, str(self.root / 'tether'), 'update',
                                        '--yes', '--to', '0.2.0', '--report'])
            self.assertEqual(run.call_args.kwargs, dict(timeout=10, capture_output=True, text=True, env=env))
            run.return_value = subprocess.CompletedProcess([], 1, '', 'unit failed\n')
            with self.assertRaisesRegex(RuntimeError, 'unit failed'):
                updates.launch('0.2.0')
            run.side_effect = subprocess.TimeoutExpired(argv, 10)
            with self.assertRaises(updates.UpdateRefused):
                updates.start('0.2.0')
            self.assertEqual(updates.read_run()['state'], 'failed')

    def test_detached_fallbacks_and_reaper(self):
        cases = [('darwin', {}, '/systemd-run'), ('linux', {'TERMUX_VERSION': '1'}, '/systemd-run'),
                 ('linux', {'PREFIX': '/data/data/com.termux/files/usr'}, '/systemd-run'), ('linux', {}, None)]
        for platform, env, systemd in cases:
            with self.subTest(platform=platform, env=env), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(updates.sys, 'platform', platform), \
                    mock.patch.object(updates.paths, 'SYSTEMD_RUN', systemd), \
                    mock.patch.object(updates.subprocess, 'Popen') as popen, \
                    mock.patch.object(updates.threading, 'Thread') as thread, \
                    mock.patch.object(updates.subprocess, 'run') as run:
                updates.launch('0.2.0')
                run.assert_not_called()
                options = popen.call_args.kwargs
                self.assertTrue(options['start_new_session'])
                self.assertEqual(options['stdin'], subprocess.DEVNULL)
                self.assertEqual(options['env'], env)
                self.assertEqual(options['stdout'].name, str(self.cache / 'update-run.log'))
                self.assertIs(options['stderr'], options['stdout'])
                self.assertTrue(options['stdout'].closed)
                thread.assert_called_once_with(target=popen.return_value.wait, daemon=True)
                thread.return_value.start.assert_called_once()
                thread.call_args.kwargs['target']()
                popen.return_value.wait.assert_called_once()


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.object(updates.paths, 'CACHE', Path(temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = dict(state='running', target='0.2.0', error='', started_at=time.time())
        updates.write_run(self.data)

    def test_report_reuses_main_and_forwards_stderr(self):
        main = cli.main
        def update(argv):
            self.assertEqual(argv, ['update', '--yes', '--to', '0.2.0'])
            print('first warning', file=cli.sys.stderr)
            os.write(2, b'child failure\n\n')
            return 1
        with mock.patch.object(cli, 'main', side_effect=update), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(['update', '--yes', '--to', '0.2.0', '--report']), 1)
        self.assertEqual(output.getvalue(), 'first warning\nchild failure\n\n')
        self.assertEqual(updates.read_run(), dict(self.data, state='failed', error='child failure'))

    def test_report_success_and_default_error(self):
        for code, state, error in ((0, 'done', ''), (1, 'failed', 'The update failed.')):
            with self.subTest(code=code), mock.patch.object(cli, 'main', return_value=code) as main:
                self.assertEqual(cli.report_update(['update']), code)
                main.assert_called_once_with(['update'])
            self.assertEqual(updates.read_run(), dict(self.data, state=state, error=error))

    def test_report_without_existing_run(self):
        updates.run_file().unlink()
        with mock.patch.object(cli, 'main', return_value=0):
            self.assertEqual(cli.report_update(['update', '--to', 'v0.2.0']), 0)
        data = updates.read_run()
        self.assertEqual(data['state'], 'done')
        self.assertEqual(data['target'], '0.2.0')
        self.assertGreaterEqual(data['started_at'], self.data['started_at'])

    def test_report_with_real_stderr_descriptor(self):
        def update(argv):
            print('reported warning', file=cli.sys.stderr)
            os.write(2, b'reported failure\n')
            return 1
        with mock.patch.object(cli, 'main', side_effect=update), \
                mock.patch.object(cli.sys, 'stderr', cli.sys.__stderr__):
            self.assertEqual(cli.report_update(['update']), 1)
        self.assertEqual(updates.read_run()['error'], 'reported failure')

    def test_report_records_all_exceptions_and_restores_sigterm(self):
        previous = signal.getsignal(signal.SIGTERM)
        for error in (RuntimeError('unexpected'), SystemExit(0), KeyboardInterrupt()):
            with self.subTest(error=error), mock.patch.object(cli, 'main', side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(type(error)):
                    cli.report_update(['update'])
                self.assertEqual(updates.read_run()['state'], 'failed')
                self.assertEqual(updates.read_run()['target'], self.data['target'])
                self.assertEqual(updates.read_run()['started_at'], self.data['started_at'])
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_sigterm_records_failure(self):
        def terminate(argv):
            os.kill(os.getpid(), signal.SIGTERM)
        with mock.patch.object(cli, 'main', side_effect=terminate), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.report_update(['update'])
        self.assertEqual(updates.read_run(), dict(self.data, state='failed',
                                                error='The update was terminated (SIGTERM).'))

    def test_parser_failure_keeps_last_stderr_line(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                cli.main(['update', '--report', '--unknown-option'])
        self.assertEqual(updates.read_run()['state'], 'failed')
        self.assertEqual(updates.read_run()['error'], output.getvalue().strip().splitlines()[-1])
        self.assertIn('unrecognized arguments', updates.read_run()['error'])


class UpdateEndpointTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        patcher = mock.patch.object(server.auth, 'MODE', 'proxy')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_http)

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())

    def post(self, body, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        try:
            connection.request('POST', '/mobile/api/update', json.dumps(body),
                               dict({'Content-Type': 'application/json'}, **headers))
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def headers(self):
        return {'Origin': f'http://127.0.0.1:{self.http.server_port}', 'X-Codex-Mobile': '1'}

    def test_post_success_and_refusal(self):
        data = dict(state='running', target='0.2.0', error='', started_at=time.time())
        with mock.patch.object(updates, 'start', return_value=data) as start:
            self.assertEqual(self.post({'version': '0.2.0'}, **self.headers()), (202, {'run': data}))
            start.assert_called_once_with('0.2.0')
        for status in (400, 409, 500):
            with mock.patch.object(updates, 'start', side_effect=updates.UpdateRefused(status, 'refused')):
                self.assertEqual(self.post({'version': None}, **self.headers()), (status, {'error': 'refused'}))

    def test_post_requires_sign_in_header_and_same_origin(self):
        with mock.patch.object(updates, 'start') as start:
            self.assertEqual(self.post({'version': '0.2.0'}, Origin=self.headers()['Origin'])[0], 403)
            self.assertEqual(self.post({'version': '0.2.0'}, **{'Origin': 'http://foreign.invalid',
                                                                           'X-Codex-Mobile': '1'})[0], 403)
            with mock.patch.object(self.server.auth, 'MODE', 'token'):
                self.assertEqual(self.post({'version': '0.2.0'}, **self.headers())[0], 401)
            start.assert_not_called()


class BannerRunTests(unittest.TestCase):
    def test_update_now_waits_through_the_restart(self):
        node = shutil.which('node')
        if not node or not (ROOT / 'ui/node_modules/typescript').is_dir():
            self.skipTest('UI dependencies and Node are required for the banner test')
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('./ui/node_modules/typescript');
const code = ts.transpileModule(fs.readFileSync('./ui/src/UpdateBanner.tsx', 'utf8'), {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX
}}).outputText;
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const text = node => node == null || node === false ? '' : typeof node !== 'object' ? String(node)
  : Array.isArray(node) ? node.map(text).join('') : text(node.props?.children);
const find = (node, name) => {
  if (!node || typeof node !== 'object') return null;
  if (Array.isArray(node)) { for (const item of node) { const hit = find(item, name); if (hit) return hit; } return null; }
  if (node.type === 'button' && text(node) === name) return node;
  return find(node.props?.children, name);
};
const idle = {current: '0.1.0', latest: 'v0.2.0', available: true, url: 'https://example.invalid', mode: 'notify', checking: false, can_update: true, run: null};
async function scenario(replies, postError) {
  let snapshot, subscribe; const timers = [], posts = [], reloads = [];
  const context = {exports: {}, Date, setTimeout: fn => timers.push(fn), localStorage: {getItem: () => null, setItem() {}},
    require: name => {
      if (name === 'react') return {useSyncExternalStore: (sub, get) => { subscribe = sub; snapshot = get; return get(); }};
      if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})};
      if (name === 'lucide-react') return {X: 'X'};
      if (name === './Skeleton') return {Spinner: 'Spinner'};
      if (name === './backNav') return {reloadPage: () => { reloads.push('reload'); }};
      if (name === './api') return {errorText: error => error.message, request: async (path, body) => {
        if (body) { posts.push(body); if (postError) throw Error(postError); return {}; }
        const reply = replies.length > 1 ? replies.shift() : replies[0];
        if (reply instanceof Error) throw reply;
        return reply;
      }};
      throw Error(name);
    }};
  vm.runInNewContext(code, context);
  const banner = context.exports.UpdateBanner;
  banner(); subscribe(() => {}); await flush();
  const tick = async () => { timers.shift()(); await flush(); };
  return {banner, tick, posts, timers, reloads};
}
(async () => {
  let run = await scenario([idle]);
  assert.equal(find(run.banner(), 'Reload'), null);
  find(run.banner(), 'Update now').props.onClick(); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(run.posts)), [{version: '0.2.0'}]);
  assert.match(text(run.banner()), /Updating to v0\.2\.0/);
  run = await scenario([idle, Error('restarting'), {...idle, run: {state: 'running', target: '0.2.0', error: ''}}, {...idle, current: '0.2.0', available: false}]);
  find(run.banner(), 'Update now').props.onClick(); await flush();
  await run.tick(); assert.match(text(run.banner()), /Updating/); // A failed request during the restart keeps waiting.
  await run.tick(); assert.match(text(run.banner()), /Updating/);
  await run.tick(); assert.equal(run.timers.length, 0);
  const reload = find(run.banner(), 'Reload');
  assert.ok(reload);
  assert.equal(run.reloads.length, 0);
  reload.props.onClick();
  assert.equal(run.reloads.length, 1);
  run = await scenario([idle, {...idle, run: {state: 'failed', target: '0.2.0', error: 'Went back.'}}]);
  find(run.banner(), 'Update now').props.onClick(); await flush();
  await run.tick(); assert.match(text(run.banner()), /Update failed\. Went back\./); assert.ok(find(run.banner(), 'Try again'));
  run = await scenario([idle], 'Refused.');
  find(run.banner(), 'Update now').props.onClick(); await flush();
  assert.match(text(run.banner()), /Update failed\. Refused\./);
  run = await scenario([{...idle, can_update: false}]);
  assert.equal(find(run.banner(), 'Update now'), null); // A git checkout keeps the link only.
  assert.match(text(run.banner()), /Update available/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, '-'], input=script, text=True, cwd=ROOT, timeout=30, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ReleaseTests(unittest.TestCase):
    setUp = install_tests.InstallTests.setUp
    plan = install_tests.InstallTests.plan
    apply = install_tests.InstallTests.apply
    calls = install_tests.InstallTests.calls

    def prepare(self, mode='notify'):
        self.root = self.home / '.local/share/tether'
        self.releases = self.root / 'releases'
        self.old = self.releases / '0.1.0'
        self.old.mkdir(parents=True)
        (self.old / 'VERSION').write_text('0.1.0\n')
        (self.old / 'ui/dist').mkdir(parents=True)
        (self.old / 'ui/dist/index.html').write_text('old')
        (self.old / 'install.py').write_bytes((ROOT / 'install.py').read_bytes())
        self.link = self.root / 'current'
        self.link.symlink_to(self.old)
        plan = self.plan('--updates', mode)
        plan['repo'] = str(self.link)
        self.apply(plan)
        self.state = self.home / '.config/tether/install.json'
        patch = mock.patch.dict(os.environ, TETHER_INSTALL_STATE=str(self.state))
        patch.start()
        self.addCleanup(patch.stop)
        self.asset_dir = Path(self.temp.name) / 'assets'
        self.asset_dir.mkdir()

        class Quiet(SimpleHTTPRequestHandler):
            def __init__(handler, *args, **kwargs):
                super().__init__(*args, directory=str(self.asset_dir), **kwargs)

            def log_message(handler, *args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_http)
        self.base = f'http://127.0.0.1:{self.http.server_port}'
        self.release = self.bundle()

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())

    def bundle(self, unsafe=None):
        archive = self.asset_dir / 'tether-0.2.0.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, content in [('VERSION', b'0.2.0\n'), ('install.py', (ROOT / 'install.py').read_bytes()),
                                  ('ui/dist/index.html', b'new'), ('tether', (ROOT / 'tether').read_bytes()),
                                  ('server/updates.py', (ROOT / 'server/updates.py').read_bytes()),
                                  ('server/paths.py', (ROOT / 'server/paths.py').read_bytes()),
                                  ('integrations/claude/tether/skills/setup/bootstrap.py',
                                   (ROOT / 'integrations/claude/tether/skills/setup/bootstrap.py').read_bytes())]:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o755 if name == 'tether' else 0o644
                tar.addfile(info, io.BytesIO(content))
            if unsafe:
                info = tarfile.TarInfo(unsafe)
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.asset_dir / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n')
        return dict(tag_name='v0.2.0', html_url='https://example.invalid/release', assets=[
            dict(name=archive.name, browser_download_url=self.base + '/' + archive.name),
            dict(name='SHA256SUMS', browser_download_url=self.base + '/SHA256SUMS')])

    def update(self, *args):
        with mock.patch.object(updates, 'fetch_release', return_value=self.release), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(['update', '--yes', *args])

    def prepare_report(self):
        self.prepare()
        patcher = mock.patch.object(updates.paths, 'CACHE', self.home / 'cache')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.report = dict(state='running', target='0.2.0', error='', started_at=time.time())
        updates.write_run(self.report)

    def test_report_real_update_done(self):
        self.prepare_report()
        with mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.update('--report'), 0)
        self.assertEqual(updates.read_run(), dict(self.report, state='done'))
        self.assertEqual(self.link.resolve(), self.releases / '0.2.0')

    def test_report_real_update_failed(self):
        self.prepare_report()
        (self.asset_dir / 'SHA256SUMS').write_text('0' * 64 + '  tether-0.2.0.tar.gz\n')
        self.assertEqual(self.update('--report'), 1)
        self.assertEqual(updates.read_run()['state'], 'failed')
        self.assertEqual(updates.read_run()['error'], 'Checksum mismatch; install refused')
        self.assertEqual(self.link.resolve(), self.old)

    def test_report_rollback_is_failed(self):
        self.prepare_report()
        with mock.patch.object(cli, 'healthy', return_value=False):
            self.assertEqual(self.update('--report'), 1)
        self.assertEqual(updates.read_run(), dict(self.report, state='failed',
                                                error='Tether went back to v0.1.0. Run tether update on the host to see why.'))
        self.assertEqual(self.link.resolve(), self.old)

    def test_checksum_mismatch_refused(self):
        self.prepare()
        (self.asset_dir / 'SHA256SUMS').write_text('0' * 64 + '  tether-0.2.0.tar.gz\n')
        with mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update(), 1)
            run.assert_not_called()
        self.assertEqual(self.link.resolve(), self.old)

    def test_tar_traversal_absolute_and_links_refused(self):
        self.prepare()
        for name in ('../escape', '/absolute', './nested/../../escape'):
            self.release = self.bundle(name)
            self.assertEqual(self.update(), 1)
            self.assertEqual(self.link.resolve(), self.old)
        archive = self.asset_dir / 'link.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo('link')
            info.type = tarfile.SYMTYPE
            info.linkname = '../escape'
            tar.addfile(info)
        with self.assertRaises(ValueError):
            cli.extract(archive, self.asset_dir / 'out')

    def test_successful_switch_saved_choices_and_two_releases(self):
        self.prepare('auto')
        (self.releases / '0.0.1').mkdir()
        (self.releases / '0.0.1/VERSION').write_text('0.0.1\n')
        with mock.patch.object(cli, 'healthy', return_value=True) as health:
            self.assertEqual(self.update(), 0)
            health.assert_called_once_with(self.port, host='127.0.0.1')
        self.assertEqual(self.link.resolve(), self.releases / '0.2.0')
        self.assertEqual(sorted(p.name for p in self.releases.iterdir()), ['0.1.0', '0.2.0'])
        state = json.loads(self.state.read_text())
        self.assertEqual(state['repo'], str(self.link))
        self.assertEqual(state['updates'], 'auto')
        unit = (self.home / '.config/systemd/user/tether.service').read_text()
        self.assertIn(str(self.link / 'server/server.py'), unit)
        self.assertIn(['systemctl', '--user', 'restart', 'tether.service'], self.calls())
        with mock.patch.object(cli, 'healthy', return_value=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['rollback']), 0)
        self.assertEqual(self.link.resolve(), self.old)

    def test_cleanup_deletes_only_owned_releases(self):
        self.prepare()
        keep = ['notes', '0.0.2', '0.0.3', '0.0.4', '0.0.5']
        for name in keep + ['0.0.1']:
            directory = self.releases / name
            directory.mkdir()
            if name != '0.0.2':
                (directory / 'VERSION').write_text(name if name != '0.0.3' else '0.9.0')
        (self.releases / '0.0.4/.git').mkdir()
        (self.releases / '0.0.5/.git').write_text('gitdir: elsewhere')
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'VERSION').write_text('0.0.6')
        (self.releases / '0.0.6').symlink_to(outside)
        (self.releases / '0.0.7').write_text('unrelated file')
        with mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.update(), 0)
        self.assertFalse((self.releases / '0.0.1').exists())
        for name in keep + ['0.0.6', '0.0.7', '0.1.0', '0.2.0']:
            self.assertTrue((self.releases / name).exists(), name)
        self.assertTrue(outside.exists())

    def test_failed_copy_removes_partial_and_retry_succeeds(self):
        self.prepare()
        copytree = cli.shutil.copytree

        def fail_copy(source, target):
            self.assertEqual(target, self.releases / '.0.2.0.partial')
            target.mkdir()
            (target / 'VERSION').write_text('partial')
            raise OSError('copy failed')

        with mock.patch.object(cli.shutil, 'copytree', side_effect=fail_copy), mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update(), 1)
            run.assert_not_called()
        self.assertFalse((self.releases / '.0.2.0.partial').exists())
        self.assertFalse((self.releases / '0.2.0').exists())
        with mock.patch.object(cli.shutil, 'copytree', wraps=copytree), mock.patch.object(cli, 'healthy', return_value=True):
            self.assertEqual(self.update(), 0)
        self.assertFalse((self.releases / '.0.2.0.partial').exists())

    def test_failed_rename_removes_partial(self):
        self.prepare()
        with mock.patch.object(cli.Path, 'rename', side_effect=OSError('rename failed')), mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update(), 1)
            run.assert_not_called()
        self.assertFalse((self.releases / '.0.2.0.partial').exists())
        self.assertFalse((self.releases / '0.2.0').exists())

    def test_override_state_rolls_back_home_units(self):
        self.prepare()
        original = self.state
        self.state = self.home / 'custom/state/install.json'
        self.state.parent.mkdir(parents=True)
        self.state.write_bytes(original.read_bytes())
        original.unlink()
        units = self.home / '.config/systemd/user'
        before = {path.name: path.read_bytes() for path in units.iterdir()}
        state_before = self.state.read_bytes()
        with mock.patch.dict(os.environ, TETHER_INSTALL_STATE=str(self.state)), \
                mock.patch.object(cli, 'healthy', return_value=False):
            self.assertEqual(self.update(), 1)
        self.assertEqual(self.state.read_bytes(), state_before)
        self.assertEqual({path.name: path.read_bytes() for path in units.iterdir()}, before)
        self.assertFalse((self.home / 'custom/systemd/user').exists())
        self.assertEqual(self.link.resolve(), self.old)

    def test_failed_health_rolls_back_state_and_units(self):
        self.prepare()
        backup = cli.snapshot(self.state)
        with mock.patch.object(cli, 'healthy', return_value=False):
            self.assertEqual(self.update(), 1)
        self.assertEqual(self.link.resolve(), self.old)
        self.assertEqual(cli.snapshot(self.state), backup)
        self.assertFalse((self.releases / '0.2.0').exists())
        restarts = [call for call in self.calls() if call[:3] == ['systemctl', '--user', 'restart']]
        self.assertGreaterEqual(len(restarts), 3)

    def test_installer_failure_rolls_back(self):
        self.prepare()
        backup = cli.snapshot(self.state)
        real_run = cli.run

        def fail_install(command):
            if '--apply' in command:
                self.state.write_text('{}')
                raise RuntimeError('installer failed')
            real_run(command)

        with mock.patch.object(cli, 'run', side_effect=fail_install):
            self.assertEqual(self.update(), 1)
        self.assertEqual(cli.snapshot(self.state), backup)
        self.assertEqual(self.link.resolve(), self.old)

    def test_git_checkout_refusal(self):
        self.prepare()
        state = json.loads(self.state.read_text())
        state['repo'] = str(ROOT)
        self.state.write_text(json.dumps(state))
        with mock.patch.object(updates, 'fetch_release') as fetch, mock.patch.object(cli, 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['update', '--check']), 1)
            self.assertIn('this is a git checkout: run git pull, then python3 install.py --apply', output.getvalue())
            fetch.assert_not_called()
            run.assert_not_called()

    def test_git_marker_counts_when_git_refuses_the_folder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.git').write_text('gitdir: /missing/worktree\n')
            with mock.patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 128, '', 'fatal')):
                self.assertTrue(cli.git_checkout(Path(folder)))

    def test_release_inside_a_home_git_repo_is_not_a_checkout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, PATH=REAL_PATH):  # setUp puts fake tools first
                subprocess.run(['git', 'init', '-q', home], check=True)
                release = Path(home) / '.local/share/tether/releases/0.1.0'
                release.mkdir(parents=True)
                self.assertFalse(cli.git_checkout(release))
                self.assertTrue(cli.git_checkout(Path(home)))

    def test_check_does_not_download_or_restart(self):
        self.prepare()
        with mock.patch.object(cli, 'download') as download, mock.patch.object(cli, 'run') as run:
            self.assertEqual(self.update('--check', '--to', 'v0.2.0'), 0)
            download.assert_not_called()
            run.assert_not_called()

    def test_auto_respects_saved_mode(self):
        self.prepare()
        with mock.patch.object(updates, 'fetch_release') as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['update', '--auto']), 0)
            fetch.assert_not_called()

    def test_reconfigure_release_keeps_current_unit_paths(self):
        self.prepare()
        args = installer.parser().parse_args(['--plan', '--updates', 'auto', '--no-build-ui'])
        plan = installer.make_plan(args, repo=self.old, home=self.home)
        self.assertEqual(plan['repo'], str(self.link))
        self.assertIn(str(self.link / 'server/server.py'), plan['units']['tether.service'])
        self.assertIn(str(self.link / 'tether'), plan['units']['tether-update.service'])

    def test_timer_only_in_auto_mode(self):
        for mode in ('notify', 'off', 'auto'):
            plan = self.plan('--updates', mode)
            self.assertEqual('tether-update.timer' in plan['units'], mode == 'auto')
            self.assertEqual('tether-update.service' in plan['units'], mode == 'auto')
            self.assertEqual(plan['options']['updates'], mode)
            if mode == 'auto':
                self.assertIn('RandomizedDelaySec=1h', plan['units']['tether-update.timer'])
                command = next(line.removeprefix('ExecStart=') for line in
                               plan['units']['tether-update.service'].splitlines() if line.startswith('ExecStart='))
                self.assertEqual(shlex.split(command)[-2:], ['update', '--auto'])
                self.assertNotIn(['systemctl', '--user', 'enable', '--now', 'tether-update.service'], plan['commands'])
                self.apply(plan)
                self.assertEqual(self.plan()['options']['updates'], 'auto')
        plan = self.plan('--updates', 'off')
        self.assertIn('tether-update.timer', plan['remove_units'])
        self.apply(plan)
        self.assertFalse((self.home / '.config/systemd/user/tether-update.timer').exists())

    def test_health_on_random_port_and_timeout(self):
        self.prepare()
        (self.asset_dir / 'chat').mkdir()
        (self.asset_dir / 'chat/index.html').write_text('healthy')
        self.assertTrue(cli.healthy(self.http.server_port, seconds=1))
        with mock.patch.object(cli, 'urlopen', side_effect=OSError('offline')):
            self.assertFalse(cli.healthy(self.http.server_port, seconds=.01))

    def test_health_probes_configured_host(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        for host, address in [('0.0.0.0', '127.0.0.1'), ('localhost', '127.0.0.1'),
                              ('::', '127.0.0.1'), ('127.0.0.2', '127.0.0.2'),
                              ('2001:db8::1', '[2001:db8::1]')]:
            with self.subTest(host=host), mock.patch.object(cli, 'urlopen', return_value=response) as fetch:
                self.assertTrue(cli.healthy(12345, host=host))
                self.assertEqual(fetch.call_args.args[0], f'http://{address}:12345/chat/')
        self.prepare()
        state = json.loads(self.state.read_text())
        state['options']['host'] = '127.0.0.2'
        self.state.write_text(json.dumps(state))
        with mock.patch.object(cli, 'healthy', return_value=True) as health:
            self.assertEqual(self.update(), 0)
            health.assert_called_once_with(self.port, host='127.0.0.2')


if __name__ == '__main__':
    unittest.main()
