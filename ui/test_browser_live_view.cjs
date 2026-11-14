// Fixture-only BrowserPanel test for the Selkies live view. No Tether server, container, shared browser or tmux.
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const http = require('node:http')
const { resolve } = require('node:path')
const { chromium } = require('playwright-core')
const ts = require('typescript')

async function main() {
  const { createServer } = await import('vite')
  const evidence = resolve(__dirname, '../.work-notes/2026-11-14-selkies-live')
  mkdirSync(evidence, { recursive: true })
  let browser, server, liveServer, closedPort
  // The live view has its own origin: a second server on another port stands in for Tether's live listener.
  liveServer = http.createServer((req, res) => {
    if (!req.url.startsWith('/browser/live/')) { res.statusCode = 404; res.end(); return }
    res.setHeader('Content-Type', 'text/html')
    res.end('<!doctype html><title>Selkies fixture</title><body style="margin:0;background:#dee9fb;font:20px sans-serif"><p style="margin:24px">Live video fixture</p></body>')
  })
  await new Promise(done => liveServer.listen(0, '127.0.0.1', done))
  const livePort = liveServer.address().port
  { const probe = http.createServer(); await new Promise(done => probe.listen(0, '127.0.0.1', done)); closedPort = probe.address().port; await new Promise(done => probe.close(done)) }
  const fixture = `
    import React from 'react'; import {createRoot} from 'react-dom/client';
    import '/chat/src/index.css';
    import {BrowserPanel} from '/chat/src/BrowserPanel.tsx';
    import '/chat/src/App.css';
    const presets = [{id:'desktop',label:'Desktop',width:1280,height:800,touch:false},{id:'pixel',label:'Phone',width:412,height:915,touch:true}];
    window.tab = {id:'fixture', title:'Fixture tab', url:'https://example.com/', mode:'private', preset:'desktop', width:1280, height:800, touch:false, ...(window.fixtureTab || {})};
    window.run = 'run-1'; window.livePort = window.livePort || ${livePort};
    const api = async (path, body) => {
      window.calls.push({path, body});
      if (path.startsWith('browser/tabs')) return {tabs:[window.tab], presets, nav:{back:true,forward:false}, live:'/browser/live/', live_port:window.livePort, live_run:window.run};
      if (path === 'browser/device') { window.tab = {...window.tab, preset:body.preset, touch:body.preset !== 'desktop'}; return window.tab }
      return {ok:true};
    };
    createRoot(document.getElementById('root')).render(<BrowserPanel api={api} pane={{id:'%fixture'}} folder={null} active={true}/>);
  `
  try {
    server = await createServer({ root: __dirname, optimizeDeps: { entries: [] }, server: { host: '127.0.0.1', port: 0 }, plugins: [{
      name: 'live-fixture',
      resolveId(id) { if (id.endsWith('/live-fixture.jsx')) return '\0live-fixture.jsx' },
      load(id) { if (id === '\0live-fixture.jsx') return ts.transpileModule(fixture, {
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
        fileName: 'live-fixture.tsx',
      }).outputText },
      configureServer(vite) {
        vite.middlewares.use('/chat/live-view-fixture', async (req, res, next) => {
          if (req.url !== '/') return next()
          const html = await vite.transformIndexHtml('/chat/live-view-fixture/', `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"/><style>html,body,#root{margin:0;width:100%;height:100%;overflow:hidden}</style></head><body><div id="root"></div><script type="module" src="/chat/live-fixture.jsx"></script></body></html>`)
          res.setHeader('Content-Type', 'text/html'); res.end(html)
        })
      },
    }] })
    await server.listen()
    browser = await chromium.launch({ executablePath: process.env.MOBILE_TMUX_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] })
    for (const [width, height, touch, agent, down] of [[1280, 900, false], [320, 700, true], [768, 1024, true, true], [1280, 900, false, false, true]]) {
      const context = await browser.newContext({ viewport: { width, height }, hasTouch: touch, serviceWorkers: 'block' })
      const page = await context.newPage()
      page.setDefaultTimeout(15000)
      const errors = []
      page.on('pageerror', error => errors.push(error.message))
      let liveLoads = 0
      page.on('framenavigated', item => { if (item !== page.mainFrame() && item.url().includes('/browser/live/')) liveLoads++ })
      // Block all API requests, so a test can never reach a real Tether server.
      await page.route('**/mobile/**', route => route.fulfill({ json: { name: 'fixture' } }))
      if (agent) await page.addInitScript(() => { window.fixtureTab = { mode: 'agent', controller: 'agent', preset: 'desktop' } })
      if (down) await page.addInitScript(port => { window.livePort = port }, closedPort)
      await page.addInitScript(() => {
        window.calls = []; window.sockets = []; window.sources = []
        const NativeWebSocket = window.WebSocket
        window.WebSocket = class {
          static OPEN = 1
          readyState = 1
          constructor(url, protocols) {
            if (!String(url).includes('/mobile/api/browser/live?')) return new NativeWebSocket(url, protocols)
            this.url = url; window.sockets.push(this); setTimeout(() => this.onopen?.(), 0)
          }
          send(data) { window.calls.push({socket: JSON.parse(data)}) }
          close() { this.closed = true; this.readyState = 3 }
        }
        window.EventSource = class extends EventTarget {
          static CLOSED = 2
          readyState = 1
          constructor(url) { super(); this.url = url; window.sources.push(this); setTimeout(() => this.onopen?.(), 0) }
          close() { this.closed = true; this.readyState = 2 }
        }
        window.emitFrame = () => {
          const image = document.createElement('canvas'); image.width = 640; image.height = 400
          const ctx = image.getContext('2d'); ctx.fillStyle = '#fbe9de'; ctx.fillRect(0, 0, 640, 400)
          ctx.fillStyle = '#603d29'; ctx.font = '20px sans-serif'; ctx.fillText('Still picture for notes', 180, 220)
          window.sources.at(-1).dispatchEvent(new MessageEvent('frame', {data: JSON.stringify({seq: 1, width: 1280, height: 800, scrollOffsetX: 0, scrollOffsetY: 0, jpeg: image.toDataURL('image/jpeg').split(',')[1]})}))
        }
      })
      await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/chat/live-view-fixture/`, { timeout: 60000 })
      if (down) {
        // The live port does not answer (tailscale serve not set up): say so, and use the picture stream.
        await page.getByRole('status').filter({ hasText: `Live video is off: port ${closedPort} did not answer. Showing pictures instead.` }).waitFor()
        await page.waitForFunction(() => window.sockets.some(socket => !socket.closed))
        assert.equal(await page.locator('iframe').count(), 0)
        assert(await page.locator('.browser-screen').isVisible())
        await page.screenshot({ path: resolve(evidence, 'live-down.png') })
        assert.deepEqual(errors.filter(text => !/Failed to fetch/.test(text)), [])
        console.log('Browser live view: an unreachable live port falls back to the picture stream and says why')
        await context.close()
        continue
      }
      const frame = page.locator('iframe.browser-live')
      try { await frame.waitFor() } catch (error) { console.error(errors, await page.locator('body').innerText()); throw error }

      // A Desktop tab shows the live video page and opens no picture stream.
      assert.equal(await frame.getAttribute('title'), 'Page: Fixture tab')
      assert.equal(await frame.getAttribute('src'), `http://127.0.0.1:${livePort}/browser/live/`)  // its own origin
      assert.equal(await frame.getAttribute('sandbox'), 'allow-scripts allow-same-origin')  // no top navigation, no popups
      assert.equal(await frame.getAttribute('allow'), 'clipboard-read; clipboard-write')
      await page.frameLocator('iframe.browser-live').getByText('Live video fixture').waitFor()
      await page.locator('.browser-wait').waitFor({ state: 'detached' })
      assert.equal(await page.evaluate(() => window.sockets.length + window.sources.length), 0, 'no picture stream')
      assert(!(await page.locator('.browser-screen').isVisible()), 'canvas hidden')
      const box = await frame.boundingBox(), stage = await page.locator('.browser-stage').boundingBox()
      assert(Math.abs(box.width - stage.width) < 1 && Math.abs(box.height - stage.height) < 1, 'live view fills the stage')
      await page.waitForFunction(() => window.calls.some(call => call.path === 'browser/show' && call.body.tab === 'fixture'))
      // No stream brings tab titles and history, so the panel polls for them, named by its tab.
      await page.waitForFunction(() => window.calls.filter(call => call.path === 'browser/tabs?tab=fixture').length >= 2, null, { timeout: 6000 })
      assert(await page.getByRole('button', { name: 'Back', exact: true }).isEnabled() === !agent)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `no overflow at ${width}px`)
      await page.screenshot({ path: resolve(evidence, `live-${width}${agent ? '-agent' : ''}.png`) })

      if (agent) {
        // An agent controls this tab: the person watches, and the cover keeps clicks out of the page.
        const cover = page.locator('.browser-live-block')
        await cover.waitFor()
        const coverBox = await cover.boundingBox()
        assert(Math.abs(coverBox.width - box.width) < 1 && Math.abs(coverBox.height - box.height) < 1)
        assert.equal(await page.evaluate(([x, y]) => document.elementFromPoint(x, y).className, [box.x + box.width / 2, box.y + box.height / 2]), 'browser-live-block')
        assert.deepEqual(errors, [])
        console.log('Browser live view: agent tab is watch-only')
        await context.close()
        continue
      }

      // A new container run loads the page again.
      const loads = liveLoads
      await page.evaluate(() => { window.run = 'run-2' })
      await page.waitForFunction(() => window.calls.filter(call => call.path === 'browser/tabs?tab=fixture').length >= 3, null, { timeout: 6000 })
      for (let wait = 0; wait < 50 && liveLoads === loads; wait++) await page.waitForTimeout(100)
      assert.equal(liveLoads, loads + 1, 'a new run reloads the live view once')
      assert.equal(await frame.count(), 1)

      // Notes take one still picture from the screencast, then draw on it.
      const notes = page.getByRole('button', { name: 'Add notes to this frame', exact: true })
      assert(await notes.isEnabled())
      await notes.click()
      await page.waitForFunction(() => window.sources.some(source => source.url.includes('/browser/stream?tab=fixture')))
      await page.getByRole('status').filter({ hasText: 'Taking a picture of the page' }).waitFor()
      await page.evaluate(() => window.emitFrame())
      await page.getByRole('toolbar', { name: 'Notes', exact: true }).waitFor()
      assert.equal(await page.evaluate(() => window.sources.at(-1).closed), true, 'one picture only')
      assert.equal(await page.evaluate(() => document.querySelector('.browser-canvas').width), 640)
      assert(await page.locator('.browser-screen').isVisible())
      assert(!(await frame.isVisible()), 'live view hidden while noting')
      await page.screenshot({ path: resolve(evidence, `notes-${width}.png`) })
      await page.getByRole('button', { name: 'Close notes', exact: true }).click()
      await frame.waitFor({ state: 'visible' })

      // A failed picture says so and leaves the live view in place.
      await notes.click()
      await page.waitForFunction(() => window.sources.filter(source => source.url.includes('/browser/stream?')).length === 2)
      await page.evaluate(() => { const source = window.sources.at(-1); source.readyState = 2; source.dispatchEvent(new Event('error')) })
      await page.getByRole('status').filter({ hasText: 'Could not take a picture of the page. Try again.' }).waitFor()
      assert(await frame.isVisible())

      // A phone tab needs Chrome's device emulation, so it uses the picture stream again.
      await page.getByRole('button', { name: 'Device: Desktop', exact: true }).click()
      await page.getByRole('menuitemradio', { name: /Phone/ }).click()
      await page.waitForFunction(() => window.sockets.some(socket => !socket.closed))
      await frame.waitFor({ state: 'detached' })
      assert(await page.locator('.browser-screen').isVisible())
      assert.deepEqual(errors, [])
      console.log(`Browser live view: ${width}px, live page, poll, show, reload on new run, notes and phone stream pass`)
      await context.close()
    }
  } finally {
    await browser?.close()
    await server?.close()
    liveServer?.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
