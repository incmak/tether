// Fixture-only MacPairing test (B232): the extension zip download step, its error states, keyboard use and layout.
// No Tether server: Vite renders the component, and every /mobile/ request is answered by this page's routes.
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const { resolve } = require('node:path')
const ts = require('typescript')
const { getBrowser } = require('./browser_test_helper.cjs')

const ZIP = '/mobile/api/browser/extension/download'
const NAME = 'tether-extension-1.0.0.zip'
const SHOTS = process.env.SHOTS || ''

async function main() {
  const { createServer } = await import('vite')
  if (SHOTS) mkdirSync(SHOTS, { recursive: true })
  let browser, server
  const fixture = `
    import React from 'react'; import {createRoot} from 'react-dom/client';
    import '/chat/src/index.css'; import '/chat/src/App.css'; import '/chat/src/BrowserPanel.css';
    import {MacPairing} from '/chat/src/MacPairing.tsx';
    const devices = window.fixtureConnected ? [{id:'d1', name:'Mak’s MacBook Pro', connected:true, lastSeen:0, chrome:'Mozilla/5.0 Chrome/142.0'}] : [];
    const api = async path => {
      if (path === 'browser/extension/devices') return devices;
      if (path === 'browser/extension/pair') return {code:'A1B2C3D4E5F6A7B8', expires: Date.now() / 1000 + 300};
      return {};
    };
    createRoot(document.getElementById('root')).render(<div className="browser-panel"><MacPairing api={api} connected={!!window.fixtureConnected}/></div>);
  `
  try {
    server = await createServer({ root: __dirname, logLevel: 'error', optimizeDeps: { entries: [] }, server: { host: '127.0.0.1', port: 0 }, plugins: [{
      name: 'pairing-fixture',
      resolveId(id) { if (id.endsWith('/pairing-fixture.jsx')) return '\0pairing-fixture.jsx' },
      load(id) {
        if (id === '\0pairing-fixture.jsx') return ts.transpileModule(fixture, {
          compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX }, fileName: 'pairing-fixture.tsx',
        }).outputText
      },
      configureServer(vite) {
        vite.middlewares.use('/chat/pairing-fixture', async (req, res, next) => {
          if (req.url !== '/') return next()
          const html = await vite.transformIndexHtml('/chat/pairing-fixture/', '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"/><style>html,body,#root{margin:0;width:100%;height:100%}</style></head><body><div id="root"></div><script type="module" src="/chat/pairing-fixture.jsx"></script></body></html>')
          res.setHeader('Content-Type', 'text/html'); res.end(html)
        })
      },
    }] })
    await server.listen()
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`
    // Local Chromium: the shared browser cannot reach this host's loopback Vite server.
    browser = await getBrowser({ local: true })

    // answer: what the download route returns for this page.
    async function open({ width, scheme = 'light', connected = false, answer = 'zip' }) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true, serviceWorkers: 'block', colorScheme: scheme })
      const page = await context.newPage()
      page.setDefaultTimeout(15000)
      const errors = [], hits = []
      page.on('pageerror', error => errors.push(error.message))
      await page.route('**/mobile/**', route => route.fulfill({ json: {} }))
      let release
      const held = new Promise(done => { release = done })
      await page.route('**' + ZIP, async route => {
        hits.push(route.request().url())
        if (answer === 'hold') await held
        if (answer === '401') return route.fulfill({ status: 401, json: { error: 'Sign in required.' } })
        if (answer === '500') return route.fulfill({ status: 500, json: { error: 'Could not build the extension zip. No manifest.json.' } })
        if (answer === 'html') return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>Sign in</title>' })
        return route.fulfill({ status: 200, body: Buffer.from('PK\x05\x06' + '\0'.repeat(18), 'binary'), headers: {
          'Content-Type': 'application/zip', 'Cache-Control': 'no-store',
          'Content-Disposition': `attachment; filename="${NAME}"; filename*=UTF-8''${NAME}` } })
      })
      if (connected) await page.addInitScript(() => { window.fixtureConnected = true })
      await page.goto(origin + '/chat/pairing-fixture/')
      await page.locator('.mac-pairing h3').waitFor()
      return { context, page, errors, hits, release }
    }
    async function noOverflow(page, label) {
      const wide = await page.evaluate(() => [...document.querySelectorAll('.mac-pairing *')].filter(node => {
        const box = node.getBoundingClientRect(); return box.width && box.right > document.documentElement.clientWidth + 0.5
      }).map(node => node.className || node.tagName))
      assert.deepEqual(wide, [], label + ': nothing may run past the right edge')
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), label + ': no page scroll')
    }
    const shot = (page, name) => SHOTS ? page.screenshot({ path: resolve(SHOTS, name + '.png'), fullPage: true }) : null

    // 1. The steps, at each width and both themes: a real link in step 1, the folder wording, no overflow.
    for (const width of [320, 390, 1280]) for (const scheme of ['light', 'dark']) {
      const { context, page, errors } = await open({ width, scheme })
      const link = page.getByRole('link', { name: 'Download extension (.zip)' })
      assert.equal(await link.getAttribute('href'), ZIP)
      assert.equal(await link.getAttribute('download'), '')
      const steps = await page.locator('.mac-pairing-steps li').allInnerTexts()
      assert.equal(steps.length, 6)
      assert.match(steps[0], /download the Tether extension/)
      assert.match(steps[1], /Unzip it\. Keep the tether-extension folder/)
      assert.match(steps[2], /chrome:\/\/extensions.*Developer mode/)
      assert.match(steps[3], /Load unpacked and pick the tether-extension folder, not the \.zip file/)
      const box = await link.boundingBox()
      assert.ok(box.height >= 40, `link height ${box.height}`)
      await noOverflow(page, `${width} ${scheme}`)
      await shot(page, `steps-${width}-${scheme}`)
      assert.deepEqual(errors, [])
      await context.close()
    }

    // 2. Keyboard: Tab reaches the link with a visible focus ring; Enter saves the zip under the server's name.
    for (const width of [320, 1280]) {
      const { context, page, errors, hits } = await open({ width })
      const link = page.getByRole('link', { name: 'Download extension (.zip)' })
      await page.keyboard.press('Tab')
      assert.ok(await link.evaluate(node => node === document.activeElement), 'the first Tab stop is the download link')
      assert.ok(await link.evaluate(node => node.matches(':focus-visible') && getComputedStyle(node).outlineStyle !== 'none'), 'focus ring shows')
      await shot(page, `focus-${width}`)
      const [download] = await Promise.all([page.waitForEvent('download'), page.keyboard.press('Enter')])
      assert.equal(download.suggestedFilename(), NAME)
      await page.getByRole('status').filter({ hasText: `Downloaded ${NAME}.` }).waitFor()
      assert.equal(hits.length, 1, 'one fetch, no second navigation download')
      await shot(page, `downloaded-${width}`)
      assert.deepEqual(errors, [])
      await context.close()
    }

    // 3. While the zip loads: busy label, and a second click sends no second request.
    {
      const { context, page, hits, release } = await open({ width: 390, answer: 'hold' })
      await page.getByRole('link', { name: 'Download extension (.zip)' }).click()
      const busy = page.locator('.mac-pairing-download[aria-busy="true"]')
      await busy.filter({ hasText: 'Downloading…' }).waitFor()
      await busy.dispatchEvent('click')  // aria-disabled: a Playwright click would wait for it to enable
      await shot(page, 'downloading-390')
      release()
      await page.getByRole('status').filter({ hasText: 'Downloaded' }).waitFor()
      assert.equal(hits.length, 1)
      await context.close()
    }

    // 4. Errors: signed out, a server failure, and a 200 sign-in page from a proxy show an alert in the panel and save nothing.
    for (const [answer, text] of [['401', 'Your sign-in ended. Reload Tether, sign in, and download again.'], ['500', 'Could not build the extension zip. No manifest.json.'], ['html', 'The server did not send a zip file. Reload Tether, sign in, and download again.']]) {
      for (const width of [320, 1280]) {
        const { context, page, errors } = await open({ width, answer })
        let saved = false
        page.on('download', () => { saved = true })
        await page.getByRole('link', { name: 'Download extension (.zip)' }).click()
        await page.getByRole('alert').filter({ hasText: text }).waitFor()
        assert.equal(saved, false)
        assert.ok(await page.getByRole('link', { name: 'Download extension (.zip)' }).isVisible(), 'the link stays for a retry')
        await noOverflow(page, `error ${answer} ${width}`)
        await shot(page, `error-${answer}-${width}`)
        assert.deepEqual(errors, [])
        await context.close()
      }
    }

    // 5. Connected: no steps, but the download stays reachable for another computer or an update.
    for (const width of [320, 1280]) for (const scheme of ['light', 'dark']) {
      const { context, page } = await open({ width, scheme, connected: true })
      await page.getByText('Your Chrome is connected').waitFor()
      assert.equal(await page.locator('.mac-pairing-steps').count(), 0)
      const update = page.locator('.mac-pairing-update')
      assert.match(await update.innerText(), /For another computer or an update:[\s\S]*replace the files in the tether-extension folder, then click Reload/)
      const [download] = await Promise.all([page.waitForEvent('download'), update.getByRole('link', { name: 'Download extension (.zip)' }).click()])
      assert.equal(download.suggestedFilename(), NAME)
      await noOverflow(page, `connected ${width} ${scheme}`)
      await shot(page, `connected-${width}-${scheme}`)
      await context.close()
    }
    console.log('ok test_mac_pairing')
  } finally {
    if (browser) await browser.close().catch(() => {})
    if (server) await server.close().catch(() => {})
  }
}

main().catch(error => { console.error(error); process.exit(1) })
