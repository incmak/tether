// Fixture-only BrowserPanel test. No Tether server, shared browser, Xvfb or terminal input.
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const { resolve } = require('node:path')
const { chromium } = require('playwright-core')
const ts = require('typescript')

async function main() {
  const { createServer } = await import('vite')
  const evidence = resolve(__dirname, '../.work-notes/2026-11-14-window-view')
  mkdirSync(evidence, { recursive: true })
  let browser, server
  const fixture = `
    import React from 'react'; import {createRoot} from 'react-dom/client';
    import '/chat/src/index.css';
    import {BrowserPanel} from '/chat/src/BrowserPanel.tsx';
    import '/chat/src/App.css';
    const tab = {id:'fixture', title:'Fixture tab', url:'about:blank', mode:'saved', preset:'desktop', width:1280, height:800, touch:false};
    const api = async (path, body) => {
      window.calls.push({path, body});
      if (path.startsWith('browser/tabs')) return {tabs:[tab], presets:[{id:'desktop',label:'Desktop',width:1280,height:800}], nav:{back:true,forward:true}};
      if (path === 'browser/device') return {...tab, width:body.width, height:body.height};
      return {ok:true};
    };
    createRoot(document.getElementById('root')).render(<BrowserPanel api={api} pane={{id:'%fixture'}} folder={null} active={true}/>);
  `
  try {
    server = await createServer({ root: __dirname, optimizeDeps: { entries: [] }, server: { host: '127.0.0.1', port: 0 }, plugins: [{
      name: 'window-fixture',
      resolveId(id) { if (id.endsWith('/window-fixture.jsx')) return '\0window-fixture.jsx' },
      load(id) { if (id === '\0window-fixture.jsx') return ts.transpileModule(fixture, {
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
        fileName: 'window-fixture.tsx',
      }).outputText },
      configureServer(vite) {
        vite.middlewares.use('/chat/window-fixture', async (req, res, next) => {
          if (req.url !== '/') return next()
          const html = await vite.transformIndexHtml('/chat/window-fixture/', `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"/><style>html,body,#root{margin:0;width:100%;height:100%;overflow:hidden}</style></head><body><div id="root"></div><script type="module" src="/chat/window-fixture.jsx"></script></body></html>`)
          res.setHeader('Content-Type', 'text/html'); res.end(html)
        })
      },
    }] })
    await server.listen()
    browser = await chromium.launch({ executablePath: process.env.MOBILE_TMUX_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] })
    for (const [width, height, touch] of [[1280, 900, false], [768, 1024, true], [320, 700, true]]) {
      const context = await browser.newContext({ viewport: { width, height }, hasTouch: touch, serviceWorkers: 'block' })
      const page = await context.newPage()
      page.setDefaultTimeout(15000)
      const errors = []
      page.on('pageerror', error => errors.push(error.message))
      // Block all API requests, so a test can never reach the configured live proxy.
      await page.route('**/mobile/**', route => route.fulfill({ json: { name: 'fixture' } }))
      await page.addInitScript(() => {
        localStorage.setItem('tether-browser-which', 'signed')
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
          const ctx = image.getContext('2d'); ctx.fillStyle = '#dee9fb'; ctx.fillRect(0,0,640,400)
          ctx.fillStyle = '#293d60'; ctx.fillRect(0,0,640,48); ctx.fillStyle = 'white'; ctx.font='20px sans-serif'; ctx.fillText('Chrome toolbar · fixture',18,31)
          ctx.fillStyle = '#293d60'; ctx.fillText('Whole-window test frame',180,220)
          window.sources.at(-1).dispatchEvent(new MessageEvent('frame', {data: JSON.stringify({seq:1,width:2560,height:1600,top:0,browser:'signed',window:true,jpeg:image.toDataURL('image/jpeg').split(',')[1]})}))
        }
      })
      await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/chat/window-fixture/`, { timeout: 60000 })
      const toggle = page.getByRole('button', {name:'Show the whole Chrome window', exact:true})
      try { await toggle.waitFor() } catch (error) { console.error(errors, await page.locator('body').innerText()); throw error }
      await page.getByRole('button', {name:'Dialog not showing? Show the whole window', exact:true}).click()
      await page.waitForFunction(() => window.sources.some(source => source.url.includes('/browser/window?')))
      assert.equal(await toggle.getAttribute('aria-pressed'), 'true')
      assert.equal(await page.evaluate(() => window.sockets.filter(socket => !socket.closed).length), 0)
      const streamUrl = await page.evaluate(() => window.sources.at(-1).url)
      assert.match(streamUrl, /window\?browser=signed&w=\d+&h=\d+&sid=/)
      assert(!streamUrl.includes('tab='))
      for (const name of ['Back', 'Forward', 'Reload', 'Add notes to this frame', 'Device: Desktop']) assert(await page.getByRole('button', {name, exact:true}).isDisabled(), name)
      assert(await page.getByRole('textbox', {name:'Address', exact:true}).isDisabled())
      assert.equal(await page.getByRole('tab', {name:'Fixture tab'}).count(), 1)
      await page.evaluate(() => window.emitFrame())
      await page.waitForFunction(() => document.querySelector('.browser-canvas').width === 640)
      const canvas = page.locator('.browser-canvas'), box = await canvas.boundingBox()
      assert(Math.abs(box.width / box.height - 1.6) < .02, '16:10 canvas')
      assert(box.x >= 0 && box.x + box.width <= width + 1)
      const stage = await page.locator('.browser-stage').boundingBox()
      assert(Math.abs(box.x + box.width / 2 - (stage.x + stage.width / 2)) < 2, 'horizontal centering')
      assert(Math.abs(box.y + box.height / 2 - (stage.y + stage.height / 2)) < 2, 'vertical letterboxing')
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
      await page.waitForFunction(() => window.calls.some(call => call.path === 'browser/window/input' && call.body.events.some(event => event.type === 'down')))
      const click = await page.evaluate(() => window.calls.find(call => call.path === 'browser/window/input' && call.body.events.some(event => event.type === 'down')).body)
      assert.equal(click.browser, 'signed'); assert(!('tab' in click))
      const pixel = Math.ceil(2560 / box.width)
      assert(click.events.some(event => event.type === 'down' && Math.abs(event.x - 1280) <= pixel && Math.abs(event.y - 800) <= pixel), 'display coordinates')
      if (touch) await page.getByRole('button', {name:'Type into the page',exact:true}).click()
      else await page.locator('.browser-keys').focus()
      await page.keyboard.press('Control+l'); await page.keyboard.press('Enter')
      await page.locator('.browser-keys').evaluate(box => { box.value = '_touch text'; box.dispatchEvent(new InputEvent('input', {bubbles:true,inputType:'insertText',data:'touch text'})) })
      await page.waitForFunction(() => window.calls.some(call => call.path === 'browser/window/input' && call.body.events.some(event => event.type === 'text' && event.text === 'touch text')))
      assert.equal(await page.evaluate(() => window.calls.filter(call => call.path === 'browser/input' || call.socket?.type === 'input').length), 0)
      await page.screenshot({ path: resolve(evidence, `window-${width}.png`) })
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      assert(!overflow, `no overflow at ${width}px`)
      await page.evaluate(() => window.sources.at(-1).dispatchEvent(new MessageEvent('error', {data:JSON.stringify({error:'Fixture capture error'})})))
      await page.getByRole('status').filter({hasText:'Fixture capture error'}).waitFor()
      // Hold a special key, then blur and toggle before its queued release gets a display frame.
      await page.locator('.browser-keys').focus()
      await page.keyboard.down('ArrowLeft')
      await page.waitForFunction(() => window.calls.some(call => call.path === 'browser/window/input' && call.body.events.some(event => event.type === 'keydown' && event.key === 'ArrowLeft')))
      const beforeRelease = await page.evaluate(() => window.calls.length)
      await toggle.evaluate(button => {
        window.savedRAF = window.requestAnimationFrame; window.pausedRAF = []
        window.requestAnimationFrame = callback => { window.pausedRAF.push(callback); return 1 }
        button.focus(); button.click()
      })
      await page.waitForFunction(start => window.calls.slice(start).some(call => call.path === 'browser/window/input' && call.body.events.some(event => event.type === 'keyup' && event.key === 'ArrowLeft')), beforeRelease)
      await page.evaluate(() => { window.requestAnimationFrame = window.savedRAF; window.pausedRAF.forEach(callback => requestAnimationFrame(callback)) })
      await page.keyboard.up('ArrowLeft')
      assert.equal(await toggle.getAttribute('aria-pressed'), 'false')
      // The pressed button remains keyboard operable, and toggling returns to the live page stream.
      await toggle.focus(); await page.keyboard.press('Space')
      assert.equal(await toggle.getAttribute('aria-pressed'), 'true')
      await page.keyboard.press('Space')
      assert.equal(await toggle.getAttribute('aria-pressed'), 'false')
      await page.waitForFunction(() => window.sockets.some(socket => !socket.closed))
      assert(await page.getByRole('button', {name:'Reload',exact:true}).isEnabled())
      // BrowserView remounts on browser changes and resets whole-window mode.
      await toggle.click()
      await page.getByRole('button', {name:'Browser: Signed in',exact:true}).click()
      await page.getByRole('menuitemradio', {name:/Test browser/}).click()
      assert.equal(await page.getByRole('button', {name:'Show the whole Chrome window',exact:true}).count(), 0)
      await page.getByRole('button', {name:'Browser: Test',exact:true}).click()
      await page.getByRole('menuitemradio', {name:/Signed in browser/}).click()
      assert.equal(await toggle.getAttribute('aria-pressed'), 'false')
      assert.deepEqual(errors, [])
      console.log(`Browser window: ${width}px, streams, input, keyboard, controls, letterboxing, errors and reset pass`)
      await context.close()
    }
  } finally {
    await browser?.close()
    await server?.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
