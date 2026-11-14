import base64
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


class WindowChrome(FakeChrome):
    def answer(self, message):
        if message['method'] == 'Browser.getWindowForTarget':
            return {'windowId': 7}
        return super().answer(message)


class FakeContainer:
    def __init__(self, fake):
        self.fake, self.stopped = fake, 0

    def launch(self, *_args, **_kwargs):
        return self.fake.launcher(None, None)

    def stop(self):
        self.stopped += 1


class BrowserInContainerTests(unittest.TestCase):
    def setUp(self):
        self.fake = WindowChrome()
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

    def test_phone_preset_still_emulates(self):
        self.browser.list_tabs()
        target = self.browser.order[-1]
        self.browser.set_device(target, 'phone')
        self.assertTrue([c for c in self.fake.sent('Emulation.setDeviceMetricsOverride') if c['params'].get('mobile')])

    def test_show_brings_the_tab_to_the_front(self):
        self.browser.list_tabs()
        target = self.browser.order[-1]
        self.assertEqual(self.browser.show(target), {'ok': True})
        self.assertEqual(self.fake.sent('Target.activateTarget')[-1]['params'], {'targetId': target})
        self.assertTrue(self.fake.sent('Page.bringToFront'))
        with self.assertRaises(browser.BrowserError):
            self.browser.show('gone')

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
        local = browser.Browser(launcher=fake.launcher, chrome_finder=lambda: '/fake/chrome', profile=Path('/nonexistent/p'))
        self.addCleanup(local.stop)
        self.assertNotIn('live', local.list_tabs())
        self.assertFalse(fake.sent('Browser.setWindowBounds'))
        self.assertFalse(fake.sent('Emulation.clearDeviceMetricsOverride'))


if __name__ == '__main__':
    unittest.main()
