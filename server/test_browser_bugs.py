"""p27 regressions. No live services or profiles.

Only the CDP wire and clock are mocked. Browser, stream, device, and grant
methods below are the production methods. No worker or timer is started.
"""
import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import browser
import browser_api
import mac_browser
import signed_access
from test_browser import FakeHandler


def panel_browser(*, signed=False):
    instance = browser.Browser(signed=signed, profile=Path('/unused-p27-profile'),
                               chrome_finder=lambda: '/unused-chrome')
    instance.conn = Mock(closed=False)
    instance.conn.call.return_value = {}
    tab = browser.Tab('T1', 'S1')
    instance.tabs[tab.target] = instance.by_session[tab.session] = tab
    instance.order.append(tab.target)
    return instance, tab


class StreamRegressions(unittest.TestCase):
    def test_failed_screencast_start_does_not_leak_viewer(self):
        instance, tab = panel_browser()

        def call(method, *args, **kwargs):
            if method == 'Page.startScreencast':
                raise browser.BrowserError('Renderer detached during start.')
            return {}

        instance.conn.call.side_effect = call
        handler = FakeHandler(browser_api.PREFIX + 'stream?tab=T1&w=800&h=600')
        browser_api.get(handler, instance)
        self.assertEqual(handler.replies[0][0], 503)
        self.assertEqual((tab.viewers, tab.viewer_sizes, tab.casting), (0, [], None),
                         'A rejected stream must release its viewer and cast reservation.')
        instance.conn.call.side_effect = None
        instance.watch('T1', 800, 600)
        self.assertEqual((tab.viewers, tab.casting), (1, (800, 600)))

    def test_failed_larger_viewer_restores_existing_cast(self):
        instance, tab = panel_browser()
        instance.watch('T1', 800, 600)

        def call(method, params=None, *args, **kwargs):
            if method == 'Page.startScreencast' and params['maxWidth'] == 1200:
                raise browser.BrowserError('New cast rejected.')
            return {}

        instance.conn.call.side_effect = call
        with self.assertRaisesRegex(browser.BrowserError, 'New cast rejected'):
            instance.watch('T1', 1200, 900)
        self.assertEqual((tab.viewers, tab.viewer_sizes, tab.casting),
                         (1, [((800, 600), None)], (800, 600)))

    def test_failed_shared_cast_restore_preserves_viewer_without_false_cast(self):
        instance, tab = panel_browser()
        instance.watch('T1', 800, 600)

        def call(method, *args, **kwargs):
            if method == 'Page.startScreencast':
                raise browser.BrowserError('Renderer detached.')
            return {}

        instance.conn.call.side_effect = call
        with self.assertRaises(browser.BrowserError):
            instance.watch('T1', 1200, 900)
        self.assertEqual((tab.viewers, tab.viewer_sizes, tab.casting),
                         (1, [((800, 600), None)], None))

    def test_failed_bring_to_front_keeps_existing_viewer(self):
        instance, tab = panel_browser(signed=True)
        instance.watch('T1', 800, 600)
        instance.conn.call.side_effect = browser.BrowserError('Cannot activate tab.')
        with self.assertRaises(browser.BrowserError):
            instance.watch('T1', 800, 600)
        self.assertEqual((tab.viewers, tab.viewer_sizes, tab.casting),
                         (1, [((800, 600), None)], (800, 600)))

    def test_reopened_stream_gets_fresh_frame_after_unwatched_interval(self):
        instance, tab = panel_browser()
        instance.watch('T1', 800, 600)
        tab.seq = 1
        tab.frame = (1, {'deviceWidth': 1280, 'deviceHeight': 800}, 'old-page', 7)
        instance.unwatch(tab, (800, 600))
        instance.conn.call.return_value = {'data': 'current-page'}
        # The page changes while no viewer exists. A static page may not emit
        # a screencast frame; first_frame must use its fresh screenshot fallback.
        instance.watch('T1', 800, 600)
        frame = instance.first_frame(tab, wait=0)
        self.assertEqual(frame[2], 'current-page',
                         'Reopening a stream must not replay its previous cast frame.')

    def test_reopened_stream_uses_new_cast_frame_and_second_viewer_keeps_it(self):
        instance, tab = panel_browser()
        instance.watch('T1', 800, 600)
        instance.unwatch(tab, (800, 600))

        def call(method, *args, **kwargs):
            if method == 'Page.startScreencast':
                instance._event('Page.screencastFrame', {
                    'metadata': {'deviceWidth': 1280, 'deviceHeight': 800},
                    'data': 'new-cast', 'sessionId': 8}, tab.session)
            return {}

        instance.conn.call.side_effect = call
        instance.watch('T1', 800, 600)
        self.assertEqual(instance.first_frame(tab, wait=0)[2], 'new-cast')
        instance.watch('T1', 800, 600)
        self.assertEqual(instance.first_frame(tab, wait=0)[2], 'new-cast')
        self.assertNotIn('Page.captureScreenshot', [c.args[0] for c in instance.conn.call.call_args_list])

    def test_height_only_resize_rejects_old_frame(self):
        instance, tab = panel_browser()
        instance.set_device('T1', 'desktop', 800, 600)
        old = (1, {'deviceWidth': 800, 'deviceHeight': 900}, 'old-tall-page', 7)
        self.assertFalse(instance.heal(tab, old),
                         'An old height maps vertical clicks to the wrong page position.')

    def test_frame_requires_both_viewport_dimensions(self):
        instance, tab = panel_browser()
        instance.set_device('T1', 'desktop', 800, 600)
        for width, height, accepted in ((800, 600, True), (900, 600, False), (800, 900, False),
                                         (800, None, False), (None, 600, False)):
            for cast in (0, 7):
                with self.subTest(width=width, height=height, cast=cast):
                    frame = (1, {'deviceWidth': width, 'deviceHeight': height}, 'page', cast)
                    self.assertEqual(instance.heal(tab, frame), accepted)

    def test_same_url_history_change_updates_back_button(self):
        instance, tab = panel_browser()
        now = [100.0]
        history = {'currentIndex': 0, 'entries': [{'id': 1}]}

        def call(method, *args, **kwargs):
            return dict(history) if method == 'Page.getNavigationHistory' else {}

        def wait_frame(*args):
            now[0] += 1.0
            history.update(currentIndex=1, entries=[{'id': 1}, {'id': 2}])
            # history.pushState can add an entry without changing URL or title.
            instance._event('Page.navigatedWithinDocument',
                            {'url': tab.url, 'navigationType': 'historyApi'}, tab.session)
            return None

        clock = SimpleNamespace(monotonic=lambda: now[0],
                                sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        instance.conn.call.side_effect = call
        sink = io.BytesIO()
        with patch.object(browser_api, 'time', clock), \
                patch.object(browser_api, 'STREAM_LIFETIME', 2.5), \
                patch.object(instance, 'first_frame', return_value=None), \
                patch.object(instance, 'wait_frame', side_effect=wait_frame):
            browser_api.stream(Mock(wfile=sink), instance, 'T1', 800, 600)
        states = [json.loads(event.split(b'data: ', 1)[1])
                  for event in sink.getvalue().split(b'\n\n') if event.startswith(b'event: tabs')]
        self.assertFalse(states[0]['nav']['back'])
        self.assertTrue(states[-1]['nav']['back'],
                        'A new history entry must enable Back even if URL and title stay equal.')


class ParkRegressions(unittest.TestCase):
    """Lag: headless Chrome drew every unwatched tab with animations at 2x in software (87% CPU for 2 chat tabs)."""

    def calls(self, instance):
        def call(method, params=None, *args, **kwargs):
            return {'windowId': 7} if method == 'Browser.getWindowForTarget' else {}
        instance.conn.call.side_effect = call
        return lambda: [(c.args[0], (c.args[1] if len(c.args) > 1 else {}).get('bounds', {}).get('windowState'))
                        for c in instance.conn.call.call_args_list
                        if c.args[0] in ('Browser.setWindowBounds', 'Page.startScreencast')]

    def test_unwatched_user_tab_is_minimized_and_restored_before_cast(self):
        instance, tab = panel_browser()
        seen = self.calls(instance)
        with patch.object(browser, 'PARK_SECONDS', 0.01):
            instance.watch('T1', 800, 600)
            instance.unwatch(tab, (800, 600))
            deadline = time.monotonic() + 2
            while not tab.parked and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertTrue(tab.parked)
        self.assertEqual(seen()[-1], ('Browser.setWindowBounds', 'minimized'))
        instance.conn.call.reset_mock()
        instance.watch('T1', 800, 600)
        self.assertFalse(tab.parked)
        self.assertEqual(seen(), [('Browser.setWindowBounds', 'normal'), ('Page.startScreencast', None)])

    def test_watched_agent_and_signed_tabs_stay_visible(self):
        instance, tab = panel_browser()
        seen = self.calls(instance)
        instance.watch('T1', 800, 600)
        instance._park(tab)
        agent = browser.Tab('A1', 'SA', mode='agent')
        instance.tabs['A1'] = agent
        instance._park(agent)
        signed, signed_tab = panel_browser(signed=True)
        signed_seen = self.calls(signed)
        signed._park(signed_tab)
        self.assertEqual((tab.parked, agent.parked, signed_tab.parked), (False, False, False))
        self.assertNotIn('minimized', [state for _, state in seen() + signed_seen()])


class DeviceRegressions(unittest.TestCase):
    def test_chrome_desktop_preset_clears_previous_phone_user_agent(self):
        broker = mac_browser.Broker(store=Path('/unused-p27-device-store'))
        instance = broker.browser
        # MacBrowser.ensure does not populate Browser.user_agent. Represent a
        # connected relay without reading a device store or sending commands.
        broker.connections['fake'] = SimpleNamespace(closed=False)
        instance.conn = Mock(closed=False)
        instance.conn.call.return_value = {}
        tab = browser.Tab('T1', 'S1', mode='agent')
        instance.tabs[tab.target] = instance.by_session[tab.session] = tab
        instance.order.append(tab.target)
        instance.set_device('T1', 'phone')
        instance.set_device('T1', 'desktop', 800, 600)
        overrides = [call.args[1]['userAgent'] for call in instance.conn.call.call_args_list
                     if call.args[0] == 'Emulation.setUserAgentOverride']
        self.assertTrue(overrides)
        self.assertEqual(overrides[-1], '',
                         'Desktop must clear the phone UA on the extension-owned tab.')


class ChromeGrantRegressions(unittest.TestCase):
    def test_chrome_initial_address_survives_grant_and_refresh(self):
        broker = mac_browser.Broker(store=Path('/unused-p27-device-store'))
        access = mac_browser.MacAccess(broker)
        url = 'https://example.test/requested-page'
        device = Mock(closed=False, ended=set())
        device.request.return_value = {'type': 'granted', 'tab': 7, 'target': 'T7',
                                       'expires': (access.clock() + 60) * 1000}
        broker.connections['fake'] = device
        row = {'id': 'grant', 'pane': '%12', 'agent': 'codex', 'reason': 'Isolated test',
               'minutes': 1, 'url': url, 'created': access.clock(), 'browser': 'chrome'}
        access.pending['grant'] = {'row': row, 'event': threading.Event(), 'result': {},
                                    'deadline': time.monotonic() + 10, 'binding': device, 'identity': None, 'auto': False}
        # worker.js navigates to the requested URL before its granted reply.
        # Mock the reply and timer, without a real extension, listener, or worker.
        with patch.object(access, '_listen'), patch.object(signed_access.threading, 'Timer'):
            access.allow('grant')
        self.assertEqual(device.request.call_args_list[0].args[0]['url'], url)
        tab = broker.browser.tab('T7')
        broker.browser.refresh(tab)
        self.assertEqual(tab.url, url,
                         'The initial address must not remain about:blank after periodic refresh.')

    def test_chrome_grant_reads_actual_metadata_and_refreshes_after_navigation(self):
        broker = mac_browser.Broker(store=Path('/unused-p31-device-store'))
        access = mac_browser.MacAccess(broker)
        device = Mock(closed=False, ended=set())
        broker.connections['fake'] = device
        requested = 'https://example.test/requested-page'
        current = {'id': 1, 'url': 'https://example.test/redirect', 'title': 'Redirect title'}

        def request(message, *args, **kwargs):
            if message['type'] == 'grant':
                return {'type': 'granted', 'tab': 7, 'target': 'T7',
                        'expires': (access.clock() + 60) * 1000}
            if message['method'] == 'Page.getNavigationHistory':
                self.assertEqual(message['tab'], 7)
                self.assertEqual(message['grant'], 'grant')
                return {'result': {'currentIndex': 1, 'entries': [
                    {'id': 0, 'url': 'about:blank', 'title': ''}, dict(current)]}}
            return {'result': {}}

        device.request.side_effect = request
        row = {'id': 'grant', 'pane': '%12', 'agent': 'codex', 'reason': 'Isolated test',
               'minutes': 1, 'url': requested, 'created': access.clock(), 'browser': 'chrome'}
        access.pending['grant'] = {'row': row, 'event': threading.Event(), 'result': {},
                                   'deadline': time.monotonic() + 10, 'binding': device, 'identity': None, 'auto': False}
        with patch.object(access, '_listen'), patch.object(signed_access.threading, 'Timer'):
            access.allow('grant')
        tab = broker.browser.tab('T7')
        self.assertEqual((tab.url, tab.title), (current['url'], current['title']))
        self.assertIn('Page', broker.browser.conn.enabled[tab.session])
        version = broker.browser.version
        broker.browser.conn.event(device, {'tab': 7, 'grant': 'grant',
                                          'method': 'Page.navigatedWithinDocument', 'params': {'url': tab.url}})
        self.assertGreater(broker.browser.version, version)
        current.update(url='https://example.test/next', title='Next title')
        broker.browser.refresh(tab)
        self.assertEqual((tab.url, tab.title), (current['url'], current['title']))
        # A failed metadata read must preserve the last real address and title.
        device.request.side_effect = browser.BrowserError('Disconnected.')
        broker.browser.refresh(tab)
        self.assertEqual((tab.url, tab.title), (current['url'], current['title']))


if __name__ == '__main__':
    unittest.main()
