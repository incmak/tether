"""Device handoff through the panel route and an isolated real Playwright bridge."""
import base64
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest

import browser
import browser_api
import browser_cdp
from test_browser import FakeHandler


def playwright_modules():
    candidates = [Path(__file__).resolve().parent.parent / 'ui/node_modules']
    candidates += [Path(p) for p in os.environ.get('NODE_PATH', '').split(os.pathsep) if p]
    return next((str(p) for p in candidates if (p / 'playwright-core').is_dir()), None)


def jpeg_size(encoded):
    data = base64.b64decode(encoded)
    offset = 2
    while offset < len(data):
        marker = data[offset + 1]
        length = struct.unpack_from('!H', data, offset + 2)[0]
        if marker in (0xc0, 0xc1, 0xc2):
            height, width = struct.unpack_from('!HH', data, offset + 5)
            return [width, height]
        offset += 2 + length
    raise AssertionError('JPEG has no dimensions')


CLIENT = r'''
const { chromium } = require('playwright-core');
const readline = require('readline');
(async () => {
  const connection = await chromium.connectOverCDP(process.env.E2E_CDP_URL);
  const context = await connection.newContext();
  const page = await context.newPage();
  await page.goto('data:text/html,<meta name="viewport" content="width=device-width,initial-scale=1"><h1>Device handoff</h1>');
  const report = async () => ({width: await page.evaluate(() => innerWidth), viewport: page.viewportSize()});
  console.log(JSON.stringify(await report()));
  for await (const line of readline.createInterface({input: process.stdin})) {
    const action = JSON.parse(line);
    try {
      if (action.viewport) await page.setViewportSize(action.viewport);
      if (action.reload) await page.reload();
      let shot;
      if (action.screenshot) {
        const png = await page.screenshot();
        shot = [png.readUInt32BE(16), png.readUInt32BE(20)];
      }
      console.log(JSON.stringify({...await report(), shot}));
    } catch (error) { console.log(JSON.stringify({error: error.message})); }
  }
  await connection.close();
})().catch(error => { console.error(error); process.exit(1); });
'''


@unittest.skipUnless(os.environ.get('TETHER_REAL_BROWSER_TEST') == '1',
                     'Set TETHER_REAL_BROWSER_TEST=1 to run the real Chromium test')
@unittest.skipUnless(browser.find_chrome() and shutil.which('node') and playwright_modules(),
                     'Chromium, node, and ui/npm ci are required')
class RealBridgeDeviceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='tether-device-test-')
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.real = browser.Browser(profile=root / 'browser/profile')
        self.proxy = browser_cdp.Proxy(self.real, root / 'config/agent-browser.json',
                                      env_runner=lambda *a, **k: None, pane_finder=lambda *a: None)
        self.addCleanup(self.real.stop)
        self.addCleanup(self.proxy.stop)
        # Report launch failures in setUp. Browser.getVersion still has Tether's 30 s deadline.
        self.real.ensure()
        self.proxy.start()
        self.node = subprocess.Popen(['node', '-e', CLIENT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     env=dict(os.environ, NODE_PATH=playwright_modules(), E2E_CDP_URL=self.proxy.url))
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self.read_lines, daemon=True, name='device-test-node-output')
        self.reader.start()
        self.addCleanup(self.stop_node)
        self.initial = self.receive()
        deadline = time.monotonic() + 5
        while not any(t.mode == 'agent' for t in self.real.tabs.values()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.tab = next(t for t in self.real.tabs.values() if t.mode == 'agent')
        self.real.watch(self.tab.target, 640, 960)

    def read_lines(self):
        for line in self.node.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def stop_node(self):
        if self.node.poll() is None:
            self.node.stdin.close()
            try:
                self.node.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.node.kill()
                self.node.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.node.stdin, self.node.stdout):
            stream.close()

    def receive(self):
        line = self.lines.get(timeout=30)
        self.assertIsNotNone(line, 'Playwright exited early')
        try:
            return json.loads(line)
        except ValueError:
            self.fail('Unexpected Playwright output: ' + line)

    def agent(self, **action):
        self.node.stdin.write(json.dumps(action) + '\n')
        self.node.stdin.flush()
        return self.receive()

    def device(self, preset, **size):
        handler = FakeHandler('/mobile/api/browser/device', dict(tab=self.tab.target, preset=preset, **size))
        browser_api.post(handler, self.real, self.proxy)
        return handler.replies[0]

    def snapshot(self, label):
        previous = self.tab.seq
        if self.tab.frame:
            self.real.ack(self.tab, self.tab.frame)
        self.real.conn.call('Runtime.evaluate', {'expression': 'document.body.style.background = document.body.style.background === "red" ? "blue" : "red"'}, self.tab.session)
        deadline = time.monotonic() + 5
        frame = self.real.wait_frame(self.tab, previous, 3)
        while frame is not None and not self.real.heal(self.tab, frame) and time.monotonic() < deadline:
            self.real.ack(self.tab, frame)
            frame = self.real.wait_frame(self.tab, frame[0], .5)
        self.assertIsNotNone(frame)
        self.real.ack(self.tab, frame)
        width = self.real.conn.call('Runtime.evaluate', {'expression': 'innerWidth', 'returnByValue': True}, self.tab.session)['result']['value']
        result = {'label': label, 'innerWidth': width, 'frame': [frame[1].get('deviceWidth'), frame[1].get('deviceHeight')],
                  'jpeg': jpeg_size(frame[2]), 'row': self.tab.row()}
        print(json.dumps(result), flush=True)
        return result

    def test_phone_route_and_agent_handoff(self):
        before = self.snapshot('agent_before')
        self.assertEqual(self.initial['width'], 1280)
        self.assertEqual(before['innerWidth'], 1280)
        response = self.device('phone')
        print(json.dumps({'label': 'phone_response', 'response': response}), flush=True)
        after = self.snapshot('user_after_phone')
        self.assertEqual(response[0], 200)
        self.assertEqual(after['innerWidth'], 412)
        self.assertEqual(after['frame'], [412, 915])
        self.assertAlmostEqual(after['jpeg'][0] / after['jpeg'][1], 412 / 915, delta=.01)
        self.assertEqual(after['row']['controller'], 'user')
        self.assertEqual(self.device('agent')[0], 200)
        restored = self.snapshot('agent_restored')
        self.assertEqual(restored['innerWidth'], 1280)
        self.assertEqual(self.agent(screenshot=True)['shot'], [1280, 720])

    def test_custom_viewport_reload_desktop_fit_and_agent_screenshot(self):
        self.assertEqual(self.agent(viewport={'width': 900, 'height': 600})['width'], 900)
        self.assertEqual(self.snapshot('agent_custom')['frame'], [900, 600])
        self.assertEqual(self.device('phone')[0], 200)
        self.assertEqual(self.agent(reload=True)['width'], 412)
        phone = self.snapshot('user_phone_after_reload')
        self.assertEqual(phone['frame'], [412, 915])
        self.assertEqual(phone['innerWidth'], 412)
        self.assertAlmostEqual(phone['jpeg'][0] / phone['jpeg'][1], 412 / 915, delta=.01)
        response = self.device('desktop', width=500, height=700)
        self.assertEqual(response[0], 200)
        desktop = self.snapshot('user_desktop_fit')
        self.assertEqual(desktop['frame'], [500, 700])
        self.assertEqual(desktop['innerWidth'], 500)
        self.assertEqual(self.device('agent')[0], 200)
        restored = self.snapshot('agent_custom_restored')
        self.assertEqual(restored['frame'], [900, 600])
        self.assertEqual(restored['innerWidth'], 900)
        self.assertEqual(self.agent(screenshot=True)['shot'], [900, 600])
        self.assertEqual(self.agent(viewport={'width': 1000, 'height': 700})['width'], 1000)
        self.assertEqual(self.agent(screenshot=True)['shot'], [1000, 700])
