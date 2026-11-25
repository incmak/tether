const assert = require('node:assert/strict')
const { mkdtempSync, rmSync } = require('node:fs')
const { join } = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const fixture = `
  import React, { useEffect, useState } from 'react';
  import { createRoot } from 'react-dom/client';
  import { BrowserSheet } from '/chat/src/AgentBrowser.tsx';
  import { setPlace } from '/chat/src/backNav.ts';
  import '/chat/src/index.css';
  import '/chat/src/App.css';
  setPlace({ screen: 'chats' }, { push: false });
  const tab = { id: 'fixture', preset: 'desktop', width: 360, height: 600, title: 'Fixture page',
    url: 'https://example.invalid/', mode: 'private', touch: false };
  const query = new URLSearchParams(location.search);
  const mode = query.get('panel');
  if (mode === 'pairing') localStorage.setItem('tether-browser-which', 'chrome');
  const api = async (path, body) => {
    const url = new URL(path, location.href);
    if (url.pathname.endsWith('/browser/tabs')) {
      if (mode === 'loading') await new Promise(() => {});
      if (mode === 'failure') throw new Error('Fixture browser failed');
      const size = url.searchParams.get('live_size');
      if (size) window.liveSizes.push(size);
      return { tabs: mode === 'pairing' ? [] : [mode === 'watching' ? { ...tab, mode: 'agent', controller: 'agent' } : tab],
        presets: [], ...(mode === 'live' ? { live: '/fixture-live/', live_port: Number(location.port), live_run: 'fixture' } : {}) };
    }
    if (path === 'browser/device') return { ...tab, ...body, id: tab.id };
    return { bookmarks: [], devices: [], agents: [], asks: [], requests: [], grants: [], ended: [] };
  };
  window.liveSizes = [];
  function Fixture() {
    const [open, setOpen] = useState(false);
    useEffect(() => {
      const close = () => setOpen(false);
      window.addEventListener('close-fixture', close);
      return () => window.removeEventListener('close-fixture', close);
    }, []);
    return open ? React.createElement(BrowserSheet, { api, pane: { id: '%1' }, onClose: () => setOpen(false) })
      : React.createElement('button', { onClick: () => setOpen(true) }, 'Open fixture');
  }
  createRoot(document.querySelector('main')).render(React.createElement(Fixture));
`

function mockFullscreen(mode) {
  window.fullscreenCalls = []
  if (mode === 'native') return
  let element = null
  const change = value => {
    element = value
    document.dispatchEvent(new Event('fullscreenchange'))
  }
  window.systemExit = () => change(null)
  Object.defineProperties(document, {
    fullscreenEnabled: { value: true },
    fullscreenElement: { get: () => element },
    exitFullscreen: { value: async () => { window.fullscreenCalls.push('exit'); change(null) } },
  })
  Element.prototype.requestFullscreen = function (options) {
    window.fullscreenCalls.push([this.tagName, options, navigator.userActivation.isActive])
    if (mode === 'reject') return Promise.reject(new Error('Fullscreen denied'))
    if (mode === 'pending') return new Promise(resolve => {
      window.finishFullscreen = () => { change(this); resolve() }
    })
    return Promise.resolve().then(() => change(this))
  }
}

const cornerKey = 'tether-browser-full-corner'
const button = (page, name) => page.getByRole('button', { name, exact: true })
const sheet = page => page.locator('.browser-sheet[open]')
const pill = page => page.getByRole('group', { name: 'Full screen controls' })
const layer = (page, number) => page.waitForFunction(number => history.state?.layer === number, number)
const corner = (page, value) => page.locator(`.browser-full-controls[data-corner="${value}"]`).waitFor()
const enters = async page => {
  await button(page, 'Full screen').tap()
  await button(page, 'Exit full screen').waitFor()
  await layer(page, 2)
}
const exits = async page => {
  await button(page, 'Full screen').waitFor()
  await layer(page, 1)
  assert.equal(await button(page, 'Full screen').evaluate(el => el === document.activeElement), true)
}

async function dragButton(page, control, dx, dy) {
  const { x, y, width, height } = await button(page, control).boundingBox()
  await page.mouse.move(x + width / 2, y + height / 2)
  await page.mouse.down()
  await page.mouse.move(x + width / 2 + dx, y + height / 2 + dy)
  assert.notEqual(await pill(page).evaluate(el => el.style.transform), '')
  await page.mouse.up()
}

async function main() {
  const { createServer } = await import('vite')
  const cacheDir = mkdtempSync(join(__dirname, 'node_modules', '.vite-fullscreen-'))
  let browser, server
  try {
    server = await createServer({ root: __dirname, cacheDir,
      optimizeDeps: { entries: [], include: ['react', 'react-dom', 'react-dom/client', 'lucide-react'] },
      server: { host: '127.0.0.1', port: 0 }, plugins: [{
      name: 'fullscreen-fixture',
      resolveId(id) { if (id.endsWith('/fullscreen-fixture.js')) return '\0fullscreen-fixture.js' },
      load(id) { if (id === '\0fullscreen-fixture.js') return fixture },
      configureServer(vite) {
        vite.middlewares.use('/chat/fullscreen-fixture/', async (req, res) => {
          const html = await vite.transformIndexHtml('/chat/fullscreen-fixture/', `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content"><main></main><script type="module" src="/chat/fullscreen-fixture.js"></script>`)
          res.setHeader('Content-Type', 'text/html'); res.end(html)
        })
      },
    }] })
    await server.listen()
    const url = `http://127.0.0.1:${server.httpServer.address().port}/chat/fullscreen-fixture/`
    browser = await getBrowser({ local: true })
    let passed = 0
    async function scenario(name, test, { panel = 'normal', mode = 'accept', saved, frame } = {}) {
      const context = await browser.newContext({ viewport: { width: 360, height: 780 }, hasTouch: true, serviceWorkers: 'block' })
      try {
        const page = await context.newPage()
        const errors = []
        page.on('pageerror', error => errors.push(error.message))
        page.setDefaultTimeout(8000)
        await page.addInitScript(mockFullscreen, mode)
        if (saved) await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [cornerKey, saved])
        await page.route('**/mobile/api/**', route => route.fulfill({ json: {} }))
        await page.routeWebSocket('**/mobile/api/**', socket => socket.close())
        await page.route('**/fixture-live/', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><p>Live fixture</p><script>
          const report = () => parent.postMessage({ tether: 'live-resize', width: innerWidth, height: innerHeight }, location.origin);
          addEventListener('resize', report);
          report();
        </script>` }))
        if (frame) {
          // One still frame on the fallback stream, so the panel can take notes on it.
          const jpeg = await page.evaluate(() => {
            const canvas = Object.assign(document.createElement('canvas'), { width: 360, height: 600 })
            canvas.getContext('2d').fillRect(0, 0, 360, 600)
            return canvas.toDataURL('image/jpeg').split(',')[1]
          })
          await page.route('**/mobile/api/browser/stream?*', route => route.fulfill({ contentType: 'text/event-stream',
            body: `retry: 60000\nevent: frame\ndata: ${JSON.stringify({ seq: 1, width: 360, height: 600, jpeg })}\n\n` }))
        }
        await page.goto(`${url}?panel=${panel}`)
        await button(page, 'Open fixture').tap()
        await layer(page, 1)
        await test(page)
        assert.deepEqual(errors, [])
        passed++
        console.log(`ok ${name}`)
      } finally { await context.close() }
    }

    await scenario('enter and exit focus, corner cycling, drag snap, keyboard portal and status', async page => {
      await page.getByRole('tab', { name: /Fixture page/ }).waitFor()
      assert.equal(await button(page, 'Full screen').getAttribute('title'), 'Full screen')
      await enters(page)
      assert.deepEqual(await page.evaluate(() => window.fullscreenCalls), [['DIV', { navigationUI: 'hide' }, true]])
      assert.equal(await button(page, 'Exit full screen').evaluate(el => el === document.activeElement), true)
      assert.equal(await sheet(page).locator('.browser-sheet-head').isVisible(), false)
      await corner(page, 'bl')
      for (const next of ['tl', 'tr', 'br', 'bl']) {
        await button(page, 'Move these buttons').tap()
        await corner(page, next)
      }
      for (const control of await pill(page).getByRole('button').all()) {
        const box = await control.boundingBox()
        assert(box.width >= 44 && box.height >= 44)
      }
      await dragButton(page, 'Move these buttons', 240, -680)
      await corner(page, 'tr')
      await dragButton(page, 'Type into the page', -190, 650)
      await corner(page, 'bl')
      assert.equal(await page.evaluate(() => document.activeElement.className === 'browser-keys'), false)
      assert.equal(await page.evaluate(key => localStorage.getItem(key), cornerKey), 'bl')
      assert.equal(await sheet(page).locator('.browser-status').isVisible(), false)
      await pill(page).getByRole('button', { name: 'Type into the page' }).tap()
      await sheet(page).getByRole('status').filter({ hasText: 'Keys go to the page' }).waitFor()
      assert.equal(await page.evaluate(() => document.activeElement.className), 'browser-keys')
      await button(page, 'Move these buttons').tap()
      await button(page, 'Exit full screen').tap()
      await exits(page)
      assert.deepEqual(await page.evaluate(() => window.fullscreenCalls.at(-1)), 'exit')
      await enters(page)
      await corner(page, 'tl')
    }, { saved: 'invalid' })

    for (const [panel, text] of [['loading', 'Starting the browser…'], ['failure', 'Fixture browser failed'], ['pairing', 'Connect your Chrome']]) {
      await scenario(`full-screen controls in ${panel}`, async page => {
        await sheet(page).getByText(text, { exact: true }).waitFor()
        await enters(page)
        await sheet(page).getByText(text, { exact: true }).waitFor()
        assert.equal(await pill(page).getByRole('button').count(), 2)
        await button(page, 'Move these buttons').tap()
        await corner(page, 'tl')
        await button(page, 'Exit full screen').tap()
        await exits(page)
      }, { panel })
    }

    await scenario('rejection keeps CSS fullscreen, Escape exits, Back exits full screen then closes the sheet', async page => {
      await enters(page)
      assert.deepEqual(await sheet(page).boundingBox(), { x: 0, y: 0, width: 360, height: 780 })
      await page.keyboard.press('Escape')
      await exits(page)
      await enters(page)
      await page.evaluate(() => history.back())
      await exits(page)
      await page.evaluate(() => history.back())
      await layer(page, 0)
      await button(page, 'Open fixture').waitFor()
    }, { mode: 'reject' })

    await scenario('system fullscreen exit and external sheet unmount clean up history', async page => {
      await enters(page)
      await page.evaluate(() => window.systemExit())
      await exits(page)
      await enters(page)
      await page.evaluate(() => window.dispatchEvent(new Event('close-fixture')))
      await button(page, 'Open fixture').waitFor()
      await layer(page, 0)
      assert.equal(await page.evaluate(() => document.fullscreenElement), null)
    })

    await scenario('a fullscreen request completing after unmount exits immediately', async page => {
      await enters(page)
      await page.evaluate(() => window.dispatchEvent(new Event('close-fixture')))
      await layer(page, 0)
      await page.evaluate(() => window.finishFullscreen())
      await page.waitForFunction(() => document.fullscreenElement === null && window.fullscreenCalls.at(-1) === 'exit')
    }, { mode: 'pending' })

    await scenario('watching keeps keyboard out of the pill', async page => {
      await sheet(page).getByText(/uses this tab\. You watch\./).waitFor()
      await enters(page)
      assert.equal(await pill(page).getByRole('button').count(), 2)
      assert.equal(await sheet(page).locator('.browser-agent').isVisible(), false)
    }, { panel: 'watching' })

    await scenario('the Notes toolbar stays in full screen so notes can be sent or closed', async page => {
      await button(page, 'Add notes to this frame').tap()
      const notes = sheet(page).getByRole('toolbar', { name: 'Notes' })
      await notes.waitFor()
      await enters(page)
      assert.equal(await notes.isVisible(), true)
      assert.equal(await sheet(page).locator('.browser-bar:not(.browser-notes)').first().isVisible(), false)
      await notes.getByRole('button', { name: 'Close notes' }).tap()
      await notes.waitFor({ state: 'detached' })
      assert.equal(await pill(page).isVisible(), true)
    }, { frame: true })

    await scenario('native fullscreen reserves a Back step and the live stage refits', async page => {
      const frame = sheet(page).locator('.browser-live')
      await frame.contentFrame().getByText('Live fixture').waitFor()
      const before = await frame.boundingBox()
      await enters(page)
      await page.waitForFunction(() => document.fullscreenElement === document.querySelector('.browser-sheet-body'))
      const full = await frame.boundingBox()
      assert(full.height > before.height)
      assert.equal(await page.evaluate(() => document.elementFromPoint(innerWidth / 2, innerHeight / 2)?.className), 'browser-live')
      await page.waitForFunction(size => window.liveSizes.includes(size), `${full.width}x${full.height}`)
      await page.setViewportSize({ width: 360, height: 430 })
      await page.waitForFunction(() => window.liveSizes.includes('360x430'))
      await page.evaluate(() => history.back())
      await exits(page)
      await page.waitForFunction(() => document.fullscreenElement === null)
    }, { panel: 'live', mode: 'native' })
    console.log(`${passed} browser fullscreen scenarios passed`)
  } finally {
    try {
      await browser?.close()
    } finally {
      try { await server?.close() } finally { rmSync(cacheDir, { recursive: true, force: true }) }
    }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
