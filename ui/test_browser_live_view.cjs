// Fixture-only BrowserPanel test for the Selkies live view. No Tether server, container, shared browser or tmux.
const assert = require('node:assert/strict')
const { mkdirSync, readFileSync } = require('node:fs')
const http = require('node:http')
const { resolve } = require('node:path')
const { chromium } = require('playwright-core')
const ts = require('typescript')

// While watching, Tab never reaches the live view and typing changes nothing in it.
async function keysStayOut(page, frame) {
  await page.getByRole('button', { name: 'Take over', exact: true }).focus()
  for (let step = 0; step < 40; step++) {
    await page.keyboard.press('Tab')
    assert.notEqual(await page.evaluate(() => document.activeElement.className), 'browser-live', `Tab ${step + 1} reached the live view`)
    await page.keyboard.type('x')
  }
  assert.equal(await frame.getAttribute('inert'), '')
  assert.equal(await page.frameLocator('iframe.browser-live').locator('input').inputValue(), '')
}

// Wheel turns over the live view: the panel sends them to the tab with the exact pixels (Selkies sends whole clicks).
function wheelCalls(page) {
  return page.evaluate(() => window.calls.filter(call => call.path === 'browser/input').flatMap(call => call.body.events).filter(event => event.type === 'wheel'))
}
async function wheelOver(page, box, steps) {
  await page.mouse.move(box.x + box.width * 0.25, box.y + box.height * 0.5)
  for (let step = 0; step < steps; step++) { await page.mouse.wheel(0, 3); await page.waitForTimeout(16) }
  await page.waitForTimeout(400)
}

async function main() {
  const { createServer } = await import('vite')
  const evidence = process.env.TETHER_BROWSER_TEST_SHOTS || resolve(__dirname, '../.work-notes/2026-11-14-selkies-live')
  mkdirSync(evidence, { recursive: true })
  let browser, server, liveServer, closedPort
  // The live view has its own origin: a second server on another port stands in for Tether's live listener.
  liveServer = http.createServer((req, res) => {
    if (!req.url.startsWith('/browser/live/')) { res.statusCode = 404; res.end(); return }
    if (req.url === '/browser/live/tether-wheel.js') { res.setHeader('Content-Type', 'text/javascript'); res.end(readFileSync(resolve(__dirname, '../server/selkies/tether-wheel.js'))); return }
    res.setHeader('Content-Type', 'text/html')
    res.end('<!doctype html><script src="tether-wheel.js"></script><title>Selkies fixture</title><body style="margin:0;background:#dee9fb;font:20px sans-serif"><p style="margin:24px">Live video fixture</p><input aria-label="Fixture input" style="margin:24px">'
      + '<script>window.selkiesWheels = 0; window.addEventListener("wheel", () => window.selkiesWheels++)</script></body>')
  })
  await new Promise(done => liveServer.listen(0, '127.0.0.1', done))
  const livePort = liveServer.address().port
  { const probe = http.createServer(); await new Promise(done => probe.listen(0, '127.0.0.1', done)); closedPort = probe.address().port; await new Promise(done => probe.close(done)) }
  const fixture = `
    import React from 'react'; import {createRoot} from 'react-dom/client';
    import '/chat/src/index.css';
    import {BrowserPanel} from '/chat/src/BrowserPanel.tsx';
    import '/chat/src/App.css';
    const presets = [{id:'desktop',label:'Desktop',width:1280,height:800,touch:false},{id:'pixel',label:'Phone',width:412,height:915,touch:true},{id:'tablet',label:'Tablet',width:820,height:1180,touch:true}];
    window.tab = {id:'fixture', title:'Fixture tab', url:'https://example.com/', mode:'private', preset:'desktop', width:1280, height:800, touch:false, ...(window.fixtureTab || {})};
    const tablet = {...window.tab, ...presets[2], id:'tablet', preset:'tablet', title:'Tablet tab', mode:'private', controller:'user'};
    window.run = 'run-1'; window.livePort = window.livePort || ${livePort};
    const api = async (path, body) => {
      window.calls.push({path, body});
      if (path.startsWith('browser/tabs')) return {tabs:[window.tab, tablet], presets, nav:{back:true,forward:false}, live:'/browser/live/', live_port:window.livePort, live_run:window.run, window_view:window.wholeWindow ?? false};
      if (path === 'browser/window-view') {
        window.wholeWindow = body.enabled;
        return {window_view:window.wholeWindow};
      }
      if (path === 'browser/show' && window.holdShow === body.tab) return new Promise(resolve => {
        window.releaseShow = () => resolve({ok:true});
      });
      if (path.startsWith('browser/snapshot?')) return new Promise((resolve,reject) => {
        window.finishSnapshot = ok => ok ? resolve({jpeg:window.snapshotJpeg()}) : reject(new Error('Fixture capture failed'));
      });
      if (path === 'browser/device' && body.preset === 'agent') { window.tab = {...window.tab, controller:'agent'}; return window.tab }
      if (path === 'browser/device') {
        const {width,height,touch} = presets.find(preset => preset.id === body.preset)
        window.tab = {...window.tab, preset:body.preset, width, height, touch};
        return window.tab
      }
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
    for (const [width, height, touch, agent, down, giveBack, signed] of [
      [1280, 900, false], [320, 700, true], [768, 1024, true, true], [1280, 900, false, false, true], [320, 700, true, false, true],
      [1280, 900, false, false, false, true], [1280, 900, false, false, false, false, true], [320, 700, true, false, false, false, true],
    ]) {
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
      if (giveBack) await page.addInitScript(() => { window.fixtureTab = { mode: 'agent', controller: 'user', preset: 'desktop' } })
      if (down) await page.addInitScript(port => { window.livePort = port }, closedPort)
      if (signed) await page.addInitScript(() => {
        localStorage.setItem('tether-browser-which', 'signed')
        window.fixtureTab = { mode: 'saved' }
      })
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
        window.snapshotJpeg = () => {
          const image = document.createElement('canvas'); image.width = 640; image.height = 400
          const ctx = image.getContext('2d'); ctx.fillStyle = '#fbe9de'; ctx.fillRect(0, 0, 640, 400)
          ctx.fillStyle = '#603d29'; ctx.font = '20px sans-serif'; ctx.fillText('Still picture for notes', 180, 220)
          return image.toDataURL('image/jpeg').split(',')[1]
        }
      })
      await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/chat/live-view-fixture/`, { timeout: 60000 })
      if (down) {
        const alert = page.getByRole('alert').filter({ hasText: `Could not connect to live video on port ${closedPort}.` })
        await alert.waitFor()
        assert.equal(await page.evaluate(() => window.sockets.length + window.sources.length), 0)
        assert.equal(await page.locator('iframe').count(), 0)
        assert.equal(await page.locator('.browser-screen').isVisible(), false)
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
        const retry = page.getByRole('button', { name: 'Retry', exact: true })
        const box = await retry.boundingBox()
        assert(box.width >= 24 && box.height >= 24 && box.x >= 0 && box.x + box.width <= width)
        await retry.focus()
        assert(await retry.evaluate(button => button.matches(':focus-visible')))
        await page.screenshot({ path: resolve(evidence, `live-down-${width}.png`) })
        await page.route(`http://127.0.0.1:${closedPort}/browser/live/`, route => route.fulfill({
          contentType: 'text/html', body: '<!doctype html><p>Live video recovered</p>',
        }))
        await page.keyboard.press('Enter')
        await page.frameLocator('iframe.browser-live').getByText('Live video recovered').waitFor()
        await alert.waitFor({ state: 'detached' })
        assert.equal(await page.evaluate(() => document.activeElement.className), 'browser-stage')
        assert.equal(await page.evaluate(() => window.sockets.length + window.sources.length), 0)
        assert.deepEqual(errors.filter(text => !/Failed to fetch/.test(text)), [])
        console.log(`Browser live view: ${width}px, unreachable live port shows keyboard-accessible Retry and recovers without a picture stream`)
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
      await page.waitForFunction(() => window.calls.filter(call => call.path.startsWith('browser/tabs?tab=fixture')).length >= 2, null, { timeout: 6000 })
      assert(await page.getByRole('button', { name: 'Back', exact: true }).isEnabled() === !agent)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `no overflow at ${width}px`)
      await page.screenshot({ path: resolve(evidence, `live-${width}${agent ? '-agent' : signed ? '-signed' : ''}.png`) })

      if (signed) {
        const toggle = page.getByRole('button', { name: 'Show the whole Chrome window' })
        const notes = page.getByRole('button', { name: 'Add notes to this frame' })
        await page.getByRole('button', { name: 'Device: Desktop' }).click()
        await page.getByRole('menuitemradio', { name: /Phone/ }).click()
        await page.locator('iframe.browser-live-preset').waitFor()
        const frameLoads = liveLoads
        await toggle.press('Enter')
        await page.getByRole('button', { name: 'Show the whole Chrome window', pressed: true }).waitFor()
        assert.equal(await frame.getAttribute('class'), 'browser-live')
        assert(await notes.isDisabled())
        assert.equal(await notes.getAttribute('aria-description'), 'Notes are available in page view.')
        await page.screenshot({ path: resolve(evidence, `window-${width}.png`) })
        await wheelOver(page, await frame.boundingBox(), 1)
        assert.equal(await page.frameLocator('iframe.browser-live').locator('body').evaluate(() => window.selkiesWheels), 1)
        assert.deepEqual(await wheelCalls(page), [])
        const input = page.frameLocator('iframe.browser-live').getByRole('textbox', { name: 'Fixture input' })
        await input.fill('Direct input')
        assert.equal(await input.inputValue(), 'Direct input')
        await toggle.click()
        await page.getByRole('button', { name: 'Show the whole Chrome window', pressed: false }).waitFor()
        await page.locator('iframe.browser-live-preset').waitFor()
        assert(await notes.isEnabled())
        assert.equal(liveLoads, frameLoads, 'the whole-window toggle keeps the same iframe')
        assert.equal(await page.evaluate(() => window.sockets.length + window.sources.length), 0, 'no picture stream')
        assert.deepEqual(await page.evaluate(() => window.calls.filter(call => call.path === 'browser/window-view').map(call => call.body)), [
          { tab: 'fixture', enabled: true, browser: 'signed' }, { tab: 'fixture', enabled: false, browser: 'signed' },
        ])
        assert.deepEqual(errors, [])
        console.log(`Browser live view: ${width}px, whole window switches on and off in the same iframe`)
        await context.close()
        continue
      }

      if (!agent && !giveBack && !touch) {
        // Trackpad turns: 20 events of 3 px scroll the tab by 60 px in total, at the pointer's page point.
        await wheelOver(page, box, 20)
        const wheels = await wheelCalls(page)
        assert(wheels.length >= 1, 'wheel turns reach the tab')
        assert.equal(wheels.reduce((sum, event) => sum + event.dy, 0), 60)
        assert(wheels.every(event => event.x === Math.round(box.width / 4) && event.y === Math.round(box.height / 2) && event.dx === 0 && event.modifiers === 0), JSON.stringify(wheels))
        assert.equal(await page.frameLocator('iframe.browser-live').locator('body').evaluate(() => window.selkiesWheels), 0, 'Selkies does not see the turns')
        // Only the live frame may send them: a message from the panel's own page, or a malformed one, does nothing.
        const before = (await wheelCalls(page)).length
        await page.evaluate(() => window.postMessage({ tether: 'wheel', x: 0.5, y: 0.5, dx: 0, dy: 500 }, '*'))
        await page.frameLocator('iframe.browser-live').locator('body').evaluate(() => window.parent.postMessage({ tether: 'wheel', x: 'a', y: null, dx: 1e9, dy: NaN, alt: 'yes' }, '*'))
        await page.waitForTimeout(300)
        const after = await wheelCalls(page)
        assert.equal(after.length, before + 1)
        assert.deepEqual(after.at(-1), { type: 'wheel', x: 0, y: 0, dx: 10000, dy: 0, modifiers: 0 })
        // Pinch zoom (ctrl) stays with Selkies.
        await page.frameLocator('iframe.browser-live').locator('body').evaluate(body => body.dispatchEvent(new WheelEvent('wheel', { deltaY: 3, ctrlKey: true, bubbles: true, cancelable: true })))
        await page.waitForTimeout(300)
        assert.equal((await wheelCalls(page)).length, before + 1)
        assert.equal(await page.frameLocator('iframe.browser-live').locator('body').evaluate(() => window.selkiesWheels), 1)
        console.log('Browser live view: wheel turns scroll the tab by the exact pixels')
      }

      if (agent) {
        // An agent controls this tab: the person watches, and the cover keeps clicks out of the page.
        const cover = page.locator('.browser-live-block')
        await cover.waitFor()
        const coverBox = await cover.boundingBox()
        assert(Math.abs(coverBox.width - box.width) < 1 && Math.abs(coverBox.height - box.height) < 1)
        assert.equal(await page.evaluate(([x, y]) => document.elementFromPoint(x, y).className, [box.x + box.width / 2, box.y + box.height / 2]), 'browser-live-block')
        await keysStayOut(page, frame)
        await page.frameLocator('iframe.browser-live').locator('body').evaluate(() => window.parent.postMessage({ tether: 'wheel', x: 0.5, y: 0.5, dx: 0, dy: 50 }, '*'))
        await page.waitForTimeout(300)
        assert.deepEqual(await wheelCalls(page), [], 'no scrolling while an agent controls the tab')
        assert.deepEqual(errors, [])
        console.log('Browser live view: agent tab is watch-only')
        await context.close()
        continue
      }

      if (giveBack) {
        // The person controls an agent tab and types into the live view, then gives it back: the keys stop at once.
        const input = page.frameLocator('iframe.browser-live').locator('input')
        await input.click()
        assert.equal(await page.evaluate(() => document.activeElement.className), 'browser-live')
        await page.getByRole('button', { name: 'Give back', exact: true }).click()
        await page.getByRole('button', { name: 'Take over', exact: true }).waitFor()
        assert.notEqual(await page.evaluate(() => document.activeElement.className), 'browser-live', 'focus left the live view')
        await page.keyboard.type('late')
        assert.equal(await input.inputValue(), '', 'no keys after Give back')
        await keysStayOut(page, frame)
        assert.deepEqual(errors, [])
        console.log('Browser live view: Give back stops keys at once')
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

      // Notes take one CDP screenshot, then draw on it.
      const notes = page.getByRole('button', { name: 'Add notes to this frame', exact: true })
      // Notes wait for the reloaded live view's load event, so wait on the condition, not one read.
      await page.waitForFunction(() => !document.querySelector('[aria-label="Add notes to this frame"]').disabled)
      await notes.click()
      await page.waitForFunction(() => window.calls.some(call => call.path === 'browser/snapshot?tab=fixture'))
      await page.getByRole('status').filter({ hasText: 'Taking a picture of the page' }).waitFor()
      await page.evaluate(() => window.finishSnapshot(true))
      await page.getByRole('toolbar', { name: 'Notes', exact: true }).waitFor()
      assert.equal(await page.evaluate(() => window.sources.length), 0, 'no EventSource for Notes')
      assert.equal(await page.evaluate(() => document.querySelector('.browser-canvas').width), 640)
      assert(await page.locator('.browser-screen').isVisible())
      assert(!(await frame.isVisible()), 'live view hidden while noting')
      await page.screenshot({ path: resolve(evidence, `notes-${width}.png`) })
      await page.getByRole('button', { name: 'Close notes', exact: true }).click()
      await frame.waitFor({ state: 'visible' })

      // A failed picture says so and leaves the live view in place.
      await notes.click()
      await page.waitForFunction(() => window.calls.filter(call => call.path === 'browser/snapshot?tab=fixture').length === 2)
      await page.evaluate(() => window.finishSnapshot(false))
      await page.getByRole('status').filter({ hasText: 'Could not take a picture of the page. Try again.' }).waitFor()
      assert(await frame.isVisible())

      // A preset resizes the iframe's viewport and scales the picture to fit the stage.
      const assertPreset = async (width, height) => {
        await page.waitForFunction(([width, height]) => {
          const frame = document.querySelector('iframe.browser-live')
          return frame?.clientWidth === width && frame.clientHeight === height
        }, [width, height])
        const stage = await page.locator('.browser-stage').boundingBox()
        const box = await frame.boundingBox()
        const scale = Math.min(1, stage.width / width, stage.height / height)
        assert(Math.abs(box.width - width * scale) < 1 && Math.abs(box.height - height * scale) < 1)
        assert(Math.abs(box.x - (stage.x + (stage.width - box.width) / 2)) < 1)
        assert(Math.abs(box.y - (stage.y + (stage.height - box.height) / 2)) < 1)
        assert.deepEqual(await page.frameLocator('iframe.browser-live').locator('body').evaluate(() => [innerWidth, innerHeight]), [width, height])
        // Scaled down, the live page samples its canvas smoothly so thin text keeps its strokes (tether-wheel.js).
        const smooth = () => [...document.querySelectorAll('style')].some(style => style.textContent.includes('image-rendering: auto'))
        await page.frameLocator('iframe.browser-live').locator('body').evaluate((_, [want, source]) =>
          new Promise((done, fail) => { const until = Date.now() + 3000; const check = () => (new Function('return (' + source + ')()')() === want ? done() : Date.now() > until ? fail(new Error('smooth ' + !want)) : setTimeout(check, 50)); check() }),
        [scale < 1, smooth.toString()])
        return box
      }
      await page.getByRole('button', { name: 'Device: Desktop', exact: true }).click()
      await page.getByRole('menuitemradio', { name: /Phone/ }).click()
      const phone = await assertPreset(412, 915)
      await page.screenshot({ path: resolve(evidence, `phone-${width}.png`) })
      await notes.click()
      await page.waitForFunction(() => window.calls.filter(call => call.path === 'browser/snapshot?tab=fixture').length === 3)
      await page.evaluate(() => window.finishSnapshot(true))
      await page.getByRole('toolbar', { name: 'Notes', exact: true }).waitFor()
      await page.getByRole('button', { name: 'Close notes', exact: true }).click()
      await frame.waitFor({ state: 'visible' })
      await wheelOver(page, phone, 1)
      const [turn] = (await wheelCalls(page)).slice(-1)
      assert(Math.abs(turn.x - 103) <= 1 && Math.abs(turn.y - 458) <= 1 && turn.dy === 3, JSON.stringify(turn))
      await page.evaluate(() => { window.holdShow = 'tablet' })
      const previousShows = await page.evaluate(() => window.calls.filter(call => call.path === 'browser/show' && call.body.tab === 'fixture').length)
      await page.getByRole('tab', { name: /Tablet tab/ }).click()
      await assertPreset(820, 1180)
      await page.waitForFunction(() => typeof window.releaseShow === 'function')
      await page.getByRole('tab', { name: /Fixture tab/ }).click()
      await assertPreset(412, 915)
      assert.equal(await page.evaluate(() => window.calls.filter(call => call.path === 'browser/show' && call.body.tab === 'fixture').length), previousShows)
      await page.evaluate(() => window.releaseShow())
      await page.waitForFunction(previous => window.calls.filter(call => call.path === 'browser/show' && call.body.tab === 'fixture').length > previous, previousShows)
      await page.getByRole('button', { name: 'Device: Phone', exact: true }).click()
      await page.getByRole('menuitemradio', { name: /Desktop/ }).click()
      await page.waitForFunction(() => !document.querySelector('iframe.browser-live').classList.contains('browser-live-preset'))
      assert.equal(await page.locator('.browser-screen').isVisible(), false)
      assert.equal(await page.evaluate(() => window.sockets.length), 0)
      assert.equal(liveLoads, loads + 1, 'preset and tab changes keep the existing iframe')
      assert.deepEqual(errors, [])
      console.log(`Browser live view: ${width}px, live page, poll, show, reload, notes, phone and tablet sizes pass`)
      await context.close()
    }
  } finally {
    await browser?.close()
    await server?.close()
    liveServer?.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
