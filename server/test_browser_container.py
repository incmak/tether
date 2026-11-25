import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import base64
import concurrent.futures
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import browser
import browser_container
from test_browser import FakeChrome


class Runner:
    """Records docker commands; answers `inspect` with `running`."""

    def __init__(self, run_code=0, running=False):
        self.calls, self.run_code, self.is_running = [], run_code, running

    def __call__(self, args, timeout=60):
        self.calls.append(args)
        if args[:2] == ['docker', 'inspect']:
            return SimpleNamespace(returncode=0, stdout='true\n' if self.is_running else 'false\n', stderr='')
        if args[:2] == ['docker', 'run']:
            self.is_running = self.run_code == 0
            return SimpleNamespace(returncode=self.run_code, stdout='', stderr='' if self.run_code == 0 else 'no such image')
        if args[:2] == ['docker', 'stop']:
            self.is_running = False
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def ran(self, verb):
        return [call for call in self.calls if call[:2] == ['docker', verb]]


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = Runner()
        self.container = browser_container.Container('t', root=Path(self.tmp.name) / 'c', runner=self.runner)
        patcher = mock.patch.object(browser_container, 'docker_path', return_value='/usr/bin/docker')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_command_publishes_no_port_and_keeps_the_sandbox_on(self):
        self.container.start()
        args = self.runner.ran('run')[0]
        joined = ' '.join(args)
        self.assertNotIn('-p', args)
        self.assertNotIn('--publish', joined)
        self.assertNotIn('--cpu-shares', joined)
        self.assertNotIn('--privileged', joined)
        self.assertNotIn('SYS_ADMIN', joined)
        self.assertIn('seccomp=' + str(browser_container.FILES / 'seccomp-chromium.json'), args)
        self.assertIn('no-new-privileges=true', args)
        for setting in ('HARDEN_DESKTOP=true', 'RESTART_APP=true', 'SELKIES_UI_SHOW_SIDEBAR=false',
                        'SUBFOLDER=' + browser_container.LIVE_PREFIX, 'SELKIES_MICROPHONE_ENABLED=false'):
            self.assertIn(setting, args)
        self.assertEqual(args[-1], browser_container.IMAGE)
        self.assertNotIn('FRAME_ANCESTORS', joined)  # Tether sets frame-ancestors on the live origin itself
        self.assertIn('@sha256:', browser_container.IMAGE)  # pinned, not :latest

    def test_both_browsers_start_with_sharp_text_and_allow_picture_changes(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                runner = Runner()
                container = browser_container.Container('t', root=self.container.root, persistent=persistent, runner=runner)
                container.start()
                args = runner.ran('run')[0]
                self.assertIn('SELKIES_VIDEO_FULLCOLOR=true', args)
                self.assertIn('SELKIES_FRAMERATE=60,8-240', args)
                self.assertIn('SELKIES_VIDEO_BITRATE=8000,100-1000000', args)

    def test_selkies_listens_only_on_its_socket_with_login_and_origin_checks(self):
        # B89: the image turns Selkies' login off, allows any Origin and puts it on TCP 8082 behind nginx.
        self.container.start()
        args = self.runner.ran('run')[0]
        env = {arg.split('=', 1)[0]: arg.split('=', 1)[1] for prev, arg in zip(args, args[1:]) if prev == '-e'}
        self.assertEqual(env['SELKIES_UNIX_SOCKET'], '/sock/web.sock')
        self.assertEqual(self.container.web_sock.name, 'web.sock')
        self.assertEqual(env['SELKIES_ENABLE_BASIC_AUTH'], 'true')
        self.assertEqual(env['SELKIES_ALLOWED_ORIGINS'], '')  # empty: Selkies' default, same-origin only
        self.assertEqual(env['CUSTOM_USER'], 'tether')  # web_auth() logs in as this user
        self.assertEqual(env['PASSWORD'], self.container.password)
        self.assertNotIn('SELKIES_PUBLIC', env)
        self.assertNotIn('SELKIES_ADDR', env)
        self.assertNotIn('CUSTOM_WS_PORT', env)
        self.assertIn('SELKIES_WEB_ROOT', env)
        mounts = [arg for prev, arg in zip(args, args[1:]) if prev == '-v']
        self.assertIn('%s:/defaults/default.conf:ro' % (browser_container.FILES / 'nginx-default.conf'), mounts)
        self.assertFalse(any('/custom-services.d' in mount for mount in mounts))  # no TCP relay to nginx any more

    def test_nginx_stub_listens_on_no_tcp_port(self):
        text = (browser_container.FILES / 'nginx-default.conf').read_text()
        self.assertNotIn('#', text)  # the image's init-nginx deletes every '#' when PASSWORD is set
        listens = [line.split(None, 1)[1].rstrip(';').strip() for line in text.splitlines()
                   if line.strip().startswith('listen ')]
        self.assertTrue(listens)
        self.assertTrue(all(value.startswith('unix:') for value in listens), listens)
        self.assertNotIn('proxy_pass', text)
        self.assertFalse((browser_container.FILES / 'sock_relay.py').exists())

    def test_live_page_hands_wheel_turns_to_the_panel(self):
        # Selkies sends whole wheel clicks (a trackpad scrolled about 12 times too far); tether-wheel.js sends the
        # turns to the panel instead. init-nginx rebuilds the web root from the dashboard folder, so mount them there.
        self.container.start()
        args = self.runner.ran('run')[0]
        mounts = [arg for prev, arg in zip(args, args[1:]) if prev == '-v']
        files, dashboard = browser_container.FILES, browser_container.DASHBOARD
        self.assertIn('%s:%s/index.html:ro' % (files / 'dashboard-index.html', dashboard), mounts)
        self.assertIn('%s:%s/tether-wheel.js:ro' % (files / 'tether-wheel.js', dashboard), mounts)
        page = (files / 'dashboard-index.html').read_text()
        self.assertIn('<script src="tether-wheel.js"></script>', page)
        self.assertLess(page.index('tether-wheel.js'), page.index('type="module"'))  # runs before the Selkies client
        script = (files / 'tether-wheel.js').read_text()
        self.assertIn("tether: 'wheel'", script)
        self.assertIn('capture: true', script)

    def test_each_start_gets_a_new_password_and_the_web_auth_matches_it(self):
        self.assertIsNone(self.container.web_auth())
        self.container.start()
        first = self.container.password
        self.assertGreaterEqual(len(first), 24)
        self.assertIn('PASSWORD=' + first, self.runner.ran('run')[0])
        self.assertEqual(base64.b64decode(self.container.web_auth().split()[1]).decode(), 'tether:' + first)
        self.container.stop()
        self.assertIsNone(self.container.web_auth())
        run = self.container.run_id
        self.container.start()
        self.assertNotEqual(self.container.password, first)
        self.assertNotEqual(self.container.run_id, run)  # the panel reloads its live view

    def test_socket_folder_is_private(self):
        self.container.start()
        self.assertEqual(self.container.sock_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.container.root.stat().st_mode & 0o777, 0o700)

    def test_a_running_container_is_reused_and_a_stale_one_replaced(self):
        self.container.start()
        self.container.start()
        self.assertEqual(len(self.runner.ran('run')), 1)
        fresh = browser_container.Container('t', root=self.container.root, runner=self.runner)  # Tether restarted
        fresh.start()  # it does not know the old password, so it replaces the container
        self.assertEqual(len(self.runner.ran('run')), 2)
        self.assertEqual(len(self.runner.ran('rm')), 2)

    def test_failed_start_reports_and_leaves_no_password(self):
        self.runner.run_code = 125
        with self.assertRaisesRegex(browser_container.ContainerError, 'no such image'):
            self.container.start()
        self.assertIsNone(self.container.web_auth())

    def test_start_waits_for_docker_to_release_the_container_name(self):
        def removing(args, timeout):
            if args[:2] == ['docker', 'run'] and not self.runner.ran('run'):
                self.runner.calls.append(args)
                return SimpleNamespace(returncode=125, stderr=(
                    f'Conflict. The container name "/{self.container.name}" is already in use'))
            return self.runner(args, timeout=timeout)

        self.container.runner = removing
        with mock.patch.object(browser_container.time, 'sleep'):
            self.container.start()
        first, second = self.runner.ran('run')
        self.assertEqual(first, second)
        self.assertIsNotNone(self.container.web_auth())

    def test_persistent_container_mounts_its_profile(self):
        container = browser_container.Container('s', root=Path(self.tmp.name) / 's', runner=self.runner, persistent=True)
        container.start()
        args = self.runner.ran('run')[-1]
        self.assertIn('%s:/config' % (Path(self.tmp.name) / 's' / 'config'), args)
        self.assertNotIn('/config', ' '.join(self.runner.ran('run')[0]) if len(self.runner.ran('run')) > 1 else '')

    def test_launch_stops_the_container_when_the_socket_never_answers(self):
        with mock.patch.object(browser_container, 'START_TIMEOUT', 0.3):
            with self.assertRaises(browser_container.ContainerError):
                self.container.launch()
        self.assertEqual(len(self.runner.ran('stop')), 1)

    def test_launch_returns_two_fds_on_the_cdp_socket(self):
        self.container.start()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.container.cdp_sock))
        server.listen(1)
        self.addCleanup(server.close)
        pid, read_fd, write_fd = self.container.launch()
        peer, _ = server.accept()
        self.assertIsNone(pid)
        self.assertNotEqual(read_fd, write_fd)  # Connection closes each one
        import os
        os.write(write_fd, b'{"id":1}\0')
        self.assertEqual(peer.recv(64), b'{"id":1}\0')
        peer.sendall(b'{"id":1,"result":{}}\0')
        self.assertEqual(os.read(read_fd, 64), b'{"id":1,"result":{}}\0')
        os.close(read_fd)
        os.close(write_fd)
        peer.close()

    def test_default_needs_docker_and_the_pulled_image(self):
        with mock.patch.object(browser_container, '_run', return_value=SimpleNamespace(returncode=1)), \
                mock.patch.dict('os.environ', {'MOBILE_TMUX_BROWSER_SELKIES': '1'}):
            self.assertIsNone(browser_container.default('test'))
        with mock.patch.object(browser_container, '_run', return_value=SimpleNamespace(returncode=0)), \
                mock.patch.dict('os.environ', {'MOBILE_TMUX_BROWSER_SELKIES': '1'}):
            self.assertIsInstance(browser_container.default('test'), browser_container.Container)
            with mock.patch.dict('os.environ', {'MOBILE_TMUX_BROWSER_SELKIES': '0'}):
                self.assertIsNone(browser_container.default('test'))
        with mock.patch.object(browser_container, 'docker_path', return_value=None):
            self.assertIsNone(browser_container.default('test'))

    def test_signed_default_has_its_own_port_and_private_persistent_config(self):
        root = Path(self.tmp.name)
        with mock.patch.object(browser_container.paths, 'CACHE', root), \
                mock.patch.object(browser_container, '_run', self.runner), \
                mock.patch.dict('os.environ', MOBILE_TMUX_BROWSER_SELKIES='1'):
            signed = browser_container.default('signed')
            test = browser_container.default('test')
        self.assertNotEqual(signed.live_port, test.live_port)
        self.assertEqual(signed.live_port, browser_container.SIGNED_LIVE_PORT)
        self.assertEqual(test.live_port, browser_container.LIVE_PORT)
        signed.runner = test.runner = self.runner
        test.start()
        signed.start()
        config = root / 'selkies' / 'signed' / 'config'
        self.assertEqual(config.stat().st_mode & 0o777, 0o700)
        test_args, signed_args = self.runner.ran('run')
        self.assertIn('--cpu-shares=16384', test_args)
        self.assertIn('--cpu-shares=16384', signed_args)
        self.assertIn(f'{config}:/config', signed_args)
        self.assertFalse(any(arg.endswith(':/config') for arg in test_args))


    def test_import_starts_no_process_and_the_server_start_picks_the_container(self):
        # The UI fixture servers import server.py under an audit hook that forbids subprocesses (test_cold_load.cjs).
        import os, subprocess, sys
        with tempfile.TemporaryDirectory() as folder:
            docker = Path(folder) / 'docker'
            docker.write_text('#!/bin/sh\nexit 0\n')
            docker.chmod(0o755)
            code = ("import sys\n"
                    "def guard(event, args):\n"
                    "    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'): raise RuntimeError('spawned')\n"
                    "sys.addaudithook(guard)\n"
                    "import browser\n"
                    "print(browser.BROWSER.container)\n")
            env = dict(os.environ, PATH=folder + os.pathsep + os.environ.get('PATH', ''), MOBILE_TMUX_BROWSER_SELKIES='1')
            result = subprocess.run([sys.executable, '-c', code], cwd=Path(browser.__file__).parent, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, 'None'), result.stderr[-2000:])
        container = browser_container.Container('t', root=Path(self.tmp.name) / 'u', runner=Runner())
        chrome = browser.Browser()
        chrome.use_container(container)
        self.assertIs(chrome.container, container)
        self.assertEqual((chrome.chrome_finder(), chrome.launcher), ('container', container.launch))
        chrome.pid = 1
        with self.assertRaises(browser.BrowserError):
            chrome.use_container(container)  # never swap the launcher of a running browser
        chrome.pid = None

class WindowChrome(FakeChrome):
    window_state = 'normal'
    screen_size = None
    window_size = None

    def answer(self, message):
        if message['method'] == 'Browser.getWindowForTarget':
            bounds = {'windowState': self.window_state}
            if self.window_size:
                bounds.update(zip(('width', 'height'), self.window_size))
            return {'windowId': 7, 'bounds': bounds}
        if message['method'] == 'Emulation.getScreenInfos' and self.screen_size:
            width, height = self.screen_size
            return {'screenInfos': [{'isPrimary': True, 'width': width, 'height': height, 'devicePixelRatio': 2}]}
        if message['method'] == 'Browser.setWindowBounds':
            self.window_state = message['params']['bounds']['windowState']
        if message['method'] == 'Page.captureScreenshot':
            return {'data': 'jpeg'}
        return super().answer(message)

    def emit(self, message):
        super().emit(message)
        last = self.commands[-1] if self.commands else {}
        if last.get('method') == 'Browser.close' and last['id'] == message.get('id'):
            self.close()


class FakeContainer:
    def __init__(self, fake, *, persistent=False):
        self.fake, self.stopped = fake, 0
        self.live_port = browser_container.LIVE_PORT
        self.run_id = 'fixture-run'
        self.persistent = persistent

    def launch(self, *_args, **_kwargs):
        return self.fake.launcher(None, None)

    def stop(self):
        self.stopped += 1


class BrowserInContainerTests(unittest.TestCase):
    def setUp(self):
        self.fake = WindowChrome()
        self.addCleanup(self.fake.close)  # runs after stop(): the reader blocked in os.read needs EOF (B149)
        self.container = FakeContainer(self.fake)
        self.browser = browser.Browser(container=self.container)
        self.addCleanup(self.browser.stop)

    def test_tabs_open_fullscreen_and_the_desktop_page_fills_the_window(self):
        state = self.browser.list_tabs()
        self.assertEqual(state['live'], browser_container.LIVE_PREFIX)
        self.assertEqual(state['live_port'], browser_container.LIVE_PORT)
        self.browser.new_tab('https://a.test/')
        bounds = self.fake.sent('Browser.setWindowBounds')
        self.assertTrue(bounds)
        self.assertTrue(all(c['params']['bounds'] == {'windowState': 'fullscreen'} for c in bounds))
        self.assertTrue(self.fake.sent('Emulation.clearDeviceMetricsOverride'))
        self.assertFalse([c for c in self.fake.sent('Emulation.setDeviceMetricsOverride') if not c['params'].get('mobile')])

    def test_live_presets_keep_exact_layout_width_and_emulate_touch(self):
        self.browser.list_tabs()
        target = self.browser.order[-1]
        for preset, width, height, dpr in (('phone', 412, 915, 2.625), ('iphone', 393, 852, 3), ('tablet', 820, 1180, 2)):
            with self.subTest(preset=preset):
                row = self.browser.set_device(target, preset)
                self.assertEqual((row['width'], row['height'], row['touch']), (width, height, True))
                metrics = self.fake.sent('Emulation.setDeviceMetricsOverride')[-1]
                self.assertEqual(metrics['params'], {
                    'width': width, 'height': height, 'deviceScaleFactor': dpr, 'mobile': False,
                    'screenWidth': width, 'screenHeight': height})
                self.assertEqual(self.fake.sent('Emulation.setEmitTouchEventsForMouse')[-1]['params'],
                                 {'enabled': True, 'configuration': 'mobile'})
                scrollbar = self.fake.sent('Emulation.setScrollbarsHidden')[-1]
                self.assertEqual(scrollbar['params'], {'hidden': True})
                self.assertLess(scrollbar['id'], metrics['id'])
        self.browser.set_device(target, 'desktop')
        clears = self.fake.sent('Emulation.clearDeviceMetricsOverride')
        self.assertEqual(len(clears), 2)
        self.assertEqual(self.fake.sent('Emulation.setEmitTouchEventsForMouse')[-1]['params'], {'enabled': False})
        scrollbar = self.fake.sent('Emulation.setScrollbarsHidden')[-1]
        self.assertEqual(scrollbar['params'], {'hidden': False})
        self.assertLess(scrollbar['id'], clears[-1]['id'])

    def test_desktop_fit_tracks_the_real_screen_when_chromium_clamps_the_window(self):
        self.browser.list_tabs()
        tab = self.browser.tab(self.browser.order[-1])
        self.fake.screen_size = (470, 1260)
        self.fake.window_size = (500, 1260)
        self.browser.refresh(tab)
        self.browser.refresh(tab)
        self.fake.screen_size = (320, 560)
        self.fake.window_size = (500, 560)
        self.browser.refresh(tab)
        self.assertEqual([call['params'] for call in self.fake.sent('Emulation.setDeviceMetricsOverride')], [
            {'width': 470, 'height': 1260, 'deviceScaleFactor': 0, 'mobile': False},
            {'width': 320, 'height': 560, 'deviceScaleFactor': 0, 'mobile': False},
        ])
        self.fake.screen_size = self.fake.window_size = (640, 560)
        self.browser.refresh(tab)
        self.assertEqual(len(self.fake.sent('Emulation.clearDeviceMetricsOverride')), 2)

    def test_desktop_fit_waits_for_chromiums_display_after_selkies_resizes(self):
        self.browser.list_tabs()
        tab = self.browser.tab(self.browser.order[-1])
        self.fake.screen_size = (470, 572)
        self.fake.window_size = (500, 572)
        self.browser.refresh(tab)
        self.assertEqual(tab.fullscreen_viewport, (470, 572))

        self.fake.window_size = (1190, 614)
        self.assertIs(self.browser._fit_live_desktop(tab, (2380, 1228)), False)
        self.assertEqual(tab.fullscreen_viewport, (470, 572))

        self.fake.screen_size = (1190, 614)
        self.browser.refresh(tab, live_size=(2380, 1228))
        self.assertIsNone(tab.fullscreen_viewport)
        self.assertEqual(len(self.fake.sent('Emulation.clearDeviceMetricsOverride')), 2)

        self.fake.screen_size = (470, 1260)
        self.fake.window_size = (500, 1260)
        self.browser.refresh(tab, live_size=(940, 2520))
        self.assertEqual(tab.fullscreen_viewport, (470, 1260))

    def test_resize_observation_supersedes_an_older_poll_even_without_a_metrics_write(self):
        tab = self.browser.tab(self.browser.list_tabs()['tabs'][0]['id'])
        for screen, window, viewport in (
            ((1190, 614), (1190, 614), None),
            ((320, 560), (500, 560), (320, 560)),
        ):
            with self.subTest(screen=screen):
                self.fake.screen_size, self.fake.window_size = screen, window
                self.browser.refresh(tab)
                self.assertEqual(tab.fullscreen_viewport, viewport)

                self.fake.screen_size = (470, 572)
                self.fake.window_size = (500, 572)
                read = threading.Event()
                resume = threading.Event()
                call = self.browser.conn.call

                def pause_poll(method, *args, **kwargs):
                    result = call(method, *args, **kwargs)
                    if method == 'Browser.getWindowForTarget' and not read.is_set():
                        read.set()
                        if not resume.wait(3):
                            raise AssertionError('The resize waited for the poll geometry read.')
                    return result

                with mock.patch.object(self.browser.conn, 'call', side_effect=pause_poll):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        poll = executor.submit(self.browser.refresh, tab)
                        try:
                            self.assertTrue(read.wait(3))
                            self.fake.screen_size, self.fake.window_size = screen, window
                            self.browser.refresh(tab, live_size=(screen[0] * 2, screen[1] * 2))
                        finally:
                            resume.set()
                        poll.result(timeout=3)
                self.assertEqual(tab.fullscreen_viewport, viewport)

    def test_fit_rechecks_geometry_when_requests_read_it_out_of_start_order(self):
        tab = self.browser.tab(self.browser.list_tabs()['tabs'][0]['id'])
        self.fake.screen_size = (470, 572)
        self.fake.window_size = (500, 572)
        resize_started, poll_read = threading.Event(), threading.Event()
        read_resize, finish_poll = threading.Event(), threading.Event()
        call = self.browser.conn.call

        def pause_reads(method, *args, **kwargs):
            if method == 'Emulation.getScreenInfos' and not resize_started.is_set():
                resize_started.set()
                if not read_resize.wait(3):
                    raise AssertionError('The poll waited for the resize geometry read.')
            result = call(method, *args, **kwargs)
            if method == 'Browser.getWindowForTarget' and not poll_read.is_set():
                poll_read.set()
                if not finish_poll.wait(3):
                    raise AssertionError('The resize waited for the poll geometry read.')
            return result

        with mock.patch.object(self.browser.conn, 'call', side_effect=pause_reads):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                resize = executor.submit(self.browser.refresh, tab, live_size=(2380, 1228))
                try:
                    self.assertTrue(resize_started.wait(3))
                    poll = executor.submit(self.browser.refresh, tab)
                    self.assertTrue(poll_read.wait(3))
                    self.fake.screen_size = self.fake.window_size = (1190, 614)
                    read_resize.set()
                    resize.result(timeout=3)
                finally:
                    read_resize.set()
                    finish_poll.set()
                poll.result(timeout=3)
        self.assertIsNone(tab.fullscreen_viewport)
        self.assertFalse(self.fake.sent('Emulation.setDeviceMetricsOverride'))

    def test_signed_container_reports_its_port_and_uses_saved_tabs(self):
        fake = WindowChrome()
        self.addCleanup(fake.close)
        container = FakeContainer(fake, persistent=True)
        container.live_port = browser_container.SIGNED_LIVE_PORT
        container.run_id = 'signed-run'
        signed = browser.Browser(signed=True, container=container)
        state = signed.list_tabs()
        try:
            self.assertEqual((state['live_port'], state['live_run']), (browser_container.SIGNED_LIVE_PORT, 'signed-run'))
            test = self.browser.list_tabs()
            self.assertEqual((test['live_port'], test['live_run']), (browser_container.LIVE_PORT, 'fixture-run'))
            self.assertEqual(state['tabs'][0]['mode'], 'saved')
            self.assertFalse(fake.sent('Target.createBrowserContext'))
            self.assertEqual(fake.sent('Browser.setDownloadBehavior')[0]['params'], {'behavior': 'deny'})
        finally:
            signed.stop()
        self.assertTrue(fake.sent('Browser.close'))

    def test_show_brings_the_tab_to_the_front(self):
        self.browser.list_tabs()
        target = self.browser.order[-1]
        self.assertEqual(self.browser.show(target), {'ok': True})
        self.assertEqual(self.fake.sent('Target.activateTarget')[-1]['params'], {'targetId': target})
        self.assertTrue(self.fake.sent('Page.bringToFront'))
        with self.assertRaises(browser.BrowserError):
            self.browser.show('gone')

    def test_show_clears_fit_only_in_whole_window_view(self):
        signed = browser.Browser(signed=True, container=self.container)
        self.addCleanup(signed.stop)
        first = signed.list_tabs()['tabs'][0]['id']
        second = signed.new_tab()['id']
        self.fake.screen_size = (470, 1260)
        self.fake.window_size = (500, 1260)
        for target in (first, second):
            signed.show(target)
            signed.refresh(signed.tab(target))
        signed.show(first)
        self.assertEqual(signed.tab(first).fullscreen_viewport, (470, 1260))
        self.assertEqual(signed.tab(second).fullscreen_viewport, (470, 1260))
        signed.set_window_view(first, True)
        self.assertIsNone(signed.tab(first).fullscreen_viewport)
        signed.show(second)
        self.assertIsNone(signed.tab(second).fullscreen_viewport)
        self.assertEqual(self.fake.sent('Emulation.clearDeviceMetricsOverride')[-1]['sessionId'],
                         signed.tab(second).session)

    def test_fit_drops_stale_geometry_after_a_window_view_round_trip(self):
        signed = browser.Browser(signed=True, container=self.container)
        self.addCleanup(signed.stop)
        tab = signed.tab(signed.list_tabs()['tabs'][0]['id'])
        self.fake.screen_size = (470, 1260)
        self.fake.window_size = (500, 1260)
        read = threading.Event()
        changed = threading.Event()
        call = signed.conn.call

        def pause_read(method, *args, **kwargs):
            result = call(method, *args, **kwargs)
            if method == 'Emulation.getScreenInfos':
                read.set()
                if not changed.wait(3):
                    raise AssertionError('The mode change waited for the geometry read.')
            return result

        with mock.patch.object(signed.conn, 'call', side_effect=pause_read):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                fit = executor.submit(signed.refresh, tab)
                try:
                    self.assertTrue(read.wait(3))
                    signed.set_window_view(tab.target, True)
                    signed.set_window_view(tab.target, False)
                finally:
                    changed.set()
                fit.result(timeout=3)
        self.assertIsNone(tab.fullscreen_viewport)
        self.assertFalse(self.fake.sent('Emulation.setDeviceMetricsOverride'))
        signed.refresh(tab)
        self.assertEqual(tab.fullscreen_viewport, (470, 1260))

    def test_tabs_are_never_minimized(self):
        self.browser.list_tabs()
        tab = self.browser.tabs[self.browser.order[-1]]
        self.browser._park(tab)
        self.assertFalse(tab.parked)
        self.assertFalse([c for c in self.fake.sent('Browser.setWindowBounds') if c['params']['bounds'].get('windowState') == 'minimized'])

    def test_stop_ends_the_container_without_closing_chrome_first(self):
        self.browser.list_tabs()
        self.browser.stop()
        self.assertEqual(self.container.stopped, 1)
        self.assertFalse(self.fake.sent('Browser.close'))
        self.assertFalse(self.browser.running())


class LocalBrowserUnchangedTests(unittest.TestCase):
    def test_no_live_key_and_no_fullscreen_without_a_container(self):
        fake = WindowChrome()
        self.addCleanup(fake.close)
        local = browser.Browser(launcher=fake.launcher, chrome_finder=lambda: '/fake/chrome', profile=Path('/nonexistent/p'))
        self.addCleanup(local.stop)
        self.assertNotIn('live', local.list_tabs())
        self.assertFalse(fake.sent('Browser.setWindowBounds'))
        self.assertFalse(fake.sent('Emulation.clearDeviceMetricsOverride'))


if __name__ == '__main__':
    unittest.main()
