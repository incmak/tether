// Real Web Locks, BroadcastChannel and named pop-ups in local Chromium. All Tether APIs use fixtures.
const assert = require('node:assert/strict')
const { readFile } = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const { getBrowser, prefsStore } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json' }
const LOCK = 'tether-browser-window'
const COOKIE = 'tether_fixture=browser-popout'
const AWAY = 'The browser is open in its own window.'
const BLOCKED = 'Your browser blocked the new window. Allow pop-ups for this site to open the browser in its own window.'
const NO_CHAT = 'Open Tether to send this to a chat.'
const REFUSED = 'This pane does not take files. Choose a Claude or Codex pane, then send again.'
const PANE = '%1'
const OTHER_PANE = '%2'
const pane = (id, name) => ({
  id, pane_name: name, label: 'fixture:0.' + id.slice(1), tmux_session: '$1', agent: 'claude', dir: '/fixture',
  state: 'Idle', writable: true, thread: id.slice(1), approval: null, ask: null,
})
const sessions = { sessions: [pane(PANE, 'Fixture chat'), pane(OTHER_PANE, 'Second chat')], default_thread: '1' }
const device = { id: 'desktop', label: 'Desktop', width: 640, height: 400, touch: false }
const tab = { id: 'tab-1', preset: 'desktop', title: 'Fixture tab', url: 'https://example.invalid/', mode: 'private', width: 640, height: 400, touch: false }
const fixtures = {
  sessions, host: { name: 'Fixture host' },
  'browser/tabs': { live: null, tabs: [tab, { ...tab, id: 'tab-2', title: 'Watched tab' }], presets: [device] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  'projects/backlog': { text: '' },
  'work/board': { projects: [], items: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'codex-runs': { runs: [] },
  chat: { messages: [], busy: false, thread: '1', agent: 'claude', writable: true, session: PANE, earlier: 0 },
}
const openButton = page => page.getByRole('button', { name: 'Open the browser in its own window', exact: true })
const dockToggle = page => page.getByRole('group', { name: 'Panels', exact: true }).getByRole('button', { name: 'Browser', exact: true })
const dockPanel = page => page.locator('section.panel[aria-label="Browser"]')
const away = page => page.getByText(AWAY, { exact: true })
const returned = page => dockPanel(page).locator('.browser-panel').waitFor()
const status = (page, text) => page.getByRole('status').filter({ hasText: text }).waitFor()

// Install before the app script in the first popup document, even if it loads before Playwright attaches.
function pictureStream() {
  if (new URLSearchParams(location.search).get('browser') !== 'window') return
  window.WebSocket = undefined
  const canvas = document.createElement('canvas')
  canvas.width = 640; canvas.height = 400
  const draw = canvas.getContext('2d')
  draw.fillStyle = '#dde5f4'
  draw.fillRect(0, 0, 640, 400)
  const jpeg = canvas.toDataURL('image/jpeg').split(',')[1]
  window.EventSource = class extends EventTarget {
    readyState = 1
    constructor(url) {
      super()
      if (url.includes('/browser/stream?')) queueMicrotask(() => {
        if (this.readyState === 2) return
        this.dispatchEvent(new MessageEvent('frame', { data: JSON.stringify({ seq: 1, width: 640, height: 400, jpeg }) }))
      })
    }
    close() { this.readyState = 2 }
  }
}

async function run() {
  let server, browser
  const requests = []
  let prefs
  try {
    const html = await readFile(path.join(DIST, 'index.html'), 'utf8').catch(() => { throw new Error('Run npm run build in ui/ first.') })
    const index = html.replace('<head>', `<head><script>(${pictureStream.toString()})()</script>`)
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      if (url.pathname === '/chat/' || url.pathname.startsWith('/mobile/api/browser/')) {
        requests.push({ url: req.url, cookie: req.headers.cookie })
      }
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        const denied = req.headers.cookie !== COOKIE && endpoint.startsWith('browser/')
        if (endpoint === 'stream' || endpoint.endsWith('/stream')) { res.writeHead(204).end(); return }
        res.writeHead(denied ? 401 : 200, { 'Content-Type': 'application/json' })
        if (endpoint === 'prefs' || endpoint === 'browser/device') {
          const chunks = []
          for await (const chunk of req) chunks.push(chunk)
          const body = req.method === 'POST' ? JSON.parse(Buffer.concat(chunks)) : null
          const data = endpoint === 'prefs' ? body ? prefs.post(body) : prefs.get()
            : fixtures['browser/tabs'].tabs.find(item => item.id === body?.tab)
          res.end(JSON.stringify(data ?? {}))
        } else res.end(JSON.stringify(denied ? { error: 'Sign in' } : fixtures[endpoint] ?? {}))
        return
      }
      if (url.pathname === '/chat/') { res.writeHead(200, { 'Content-Type': 'text/html' }).end(index); return }
      const file = path.resolve(DIST, '.' + url.pathname.slice('/chat'.length))
      if (url.pathname.startsWith('/chat/') && file.startsWith(DIST + path.sep)) {
        const content = await readFile(file).catch(() => null)
        if (content) { res.writeHead(200, { 'Content-Type': mime[path.extname(file)] ?? 'application/octet-stream' }).end(content); return }
      }
      res.writeHead(404).end()
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = 'http://127.0.0.1:' + server.address().port
    browser = await getBrowser({ local: true })
    async function scenario(name, test, { signedIn = true, width = 1280 } = {}) {
      prefs = prefsStore()
      const context = await browser.newContext({ viewport: { width, height: 800 }, serviceWorkers: 'block' })
      context.setDefaultTimeout(15_000)
      const errors = []
      context.on('page', page => page.on('pageerror', error => errors.push(error.stack ?? error.message)))
      try {
        if (signedIn) await context.addCookies([{ name: 'tether_fixture', value: 'browser-popout', url: origin, httpOnly: true, sameSite: 'Strict' }])
        await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
        await context.addInitScript(() => {
          if (!location.protocol.startsWith('http')) return
          localStorage.setItem('tmux-chat.prefs.synced', '1')
          if (!localStorage.getItem('tmux-chat.prefs.v1')) {
            localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'dark', voiceOff: true, voiceSound: false }))
          }
          window.receivedFiles = []
          window.takeFiles = true
          window.addEventListener('tether-attach', event => {
            event.stopImmediatePropagation() // Record the dispatch. Never upload.
            const { pane, files } = event.detail
            window.receivedFiles.push({ pane, files: files.map(file => ({ name: file.name, type: file.type, size: file.size })) })
            if (window.takeFiles) event.preventDefault()
          })
        })
        const main = async (id = PANE, page) => {
          page ??= await context.newPage()
          await page.goto(origin + '/chat/?session=' + encodeURIComponent(id))
          try { await page.locator('.chat-panel').first().waitFor() } catch (error) {
            console.error(await page.locator('body').innerText(), await page.locator('.crash-details pre').allTextContents(), errors)
            throw error
          }
          return page
        }
        const openDock = async page => {
          await dockToggle(page).waitFor()
          if (await dockToggle(page).getAttribute('aria-pressed') === 'false') await dockToggle(page).click()
        }
        const pop = async page => {
          const [popup] = await Promise.all([page.waitForEvent('popup'), openButton(page).click()])
          await popup.waitForURL(origin + '/chat/?browser=window')
          await popup.locator('.browser-popout-page .browser-panel').waitFor()
          await away(page).waitFor()
          return popup
        }
        const send = async popup => {
          await popup.bringToFront()
          await popup.getByRole('button', { name: 'Add notes to this frame' }).click()
          await popup.getByRole('button', { name: 'Send to chat' }).click()
        }
        await test({ context, main, openDock, pop, send })
        assert.deepEqual(errors, [])
        console.log('Browser pop-out OK: ' + name)
      } finally { await context.close() }
    }

    await scenario('a–d: named window, shared cookie, reload, second main, lock release', async ({ context, main, openDock, pop }) => {
      const first = await main()
      await openDock(first)
      const popup = await pop(first)
      assert.equal(await popup.title(), 'Browser · Tether')
      assert.equal(await popup.evaluate(() => name), 'tether-browser')
      assert(!await popup.locator('.app-shell').count())
      assert(!await popup.getByRole('button', { name: 'Open the browser in its own window' }).count())
      assert.equal(await first.locator('.browser-panel, iframe.browser-live').count(), 0)
      await popup.getByText('Fixture chat', { exact: true }).waitFor()
      await popup.waitForFunction(() => document.documentElement.dataset.theme === 'dark')
      assert(requests.some(req => req.url === '/chat/?browser=window' && req.cookie === COOKIE))
      assert(requests.some(req => req.url === '/mobile/api/browser/tabs' && req.cookie === COOKIE))
      await first.reload()
      await away(first).waitFor()

      const second = await main(OTHER_PANE)
      await openDock(second)
      await away(second).waitFor()
      assert.equal(await second.locator('.browser-panel, iframe.browser-live').count(), 0)
      const show = second.getByRole('button', { name: 'Show window' })
      await second.bringToFront()
      await show.press('Tab')
      await second.keyboard.press('Shift+Tab')
      assert(await show.evaluate(button => button.matches(':focus-visible') && getComputedStyle(button).outlineStyle === 'solid'))
      await popup.evaluate(() => {
        const focus = window.focus.bind(window)
        window.focusCalls = 0
        window.focus = () => { window.focusCalls++; focus() }
      })
      await show.press('Enter')
      await popup.waitForFunction(() => window.focusCalls > 0, undefined, { polling: 50 })
      assert.equal(context.pages().length, 3)
      assert.equal(popup.url(), origin + '/chat/?browser=window')

      await popup.close()
      await Promise.all([returned(first), returned(second)])
    })

    await scenario('e: Put back reopens a closed dock. Put it back here also closes the pop-out', async ({ main, openDock, pop }) => {
      const page = await main()
      await openDock(page)
      let popup = await pop(page)
      await popup.getByText('Fixture chat', { exact: true }).waitFor()
      await dockPanel(page).getByRole('button', { name: 'Close browser', exact: true }).click()
      await dockPanel(page).waitFor({ state: 'detached' })
      await Promise.all([popup.waitForEvent('close'), popup.getByRole('button', { name: 'Put back in Tether' }).click()])
      await returned(page)

      popup = await pop(page)
      await Promise.all([popup.waitForEvent('close'), page.getByRole('button', { name: 'Put it back here' }).click()])
      await returned(page)
    })

    await scenario('f: blocked window falls back to float; detached float can open a real window', async ({ context, main, openDock }) => {
      const page = await main()
      await openDock(page)
      await page.evaluate(() => {
        window.nativeOpen = window.open
        window.open = (...args) => { window.openArgs = args; return null }
      })
      const panel = await dockPanel(page).locator('.browser-panel').boundingBox()
      await openButton(page).click()
      await page.locator('.browser-window .browser-panel').waitFor()
      await status(page, BLOCKED)
      assert.equal(context.pages().length, 1)
      assert.deepEqual(await page.evaluate(() => window.openArgs), [
        '/chat/?browser=window', 'tether-browser',
        `popup,width=${Math.max(480, Math.round(panel.width))},height=${Math.max(600, Math.round(panel.height))}`,
      ])
      await page.evaluate(() => { window.open = window.nativeOpen })
      const [popup] = await Promise.all([
        page.waitForEvent('popup'), page.getByRole('button', { name: 'Open in its own window', exact: true }).click(),
      ])
      await popup.locator('.browser-panel').waitFor()
      await away(page).waitFor()
      await page.locator('.browser-window').waitFor({ state: 'detached' })
      await popup.close()
      await page.locator('.browser-window .browser-panel').waitFor()
      assert.equal(await page.evaluate(() => localStorage.getItem('tether-browser-floating')), 'true')
    })

    await scenario('g: touch keeps the in-page float', async ({ context, main, openDock }) => {
      await context.addInitScript(() => {
        Object.defineProperty(navigator, 'maxTouchPoints', { value: 5 })
        window.open = () => { throw new Error('Touch pop-out tried to open a new window') }
      })
      const page = await main()
      await openDock(page)
      await openButton(page).click()
      await page.locator('.browser-window .browser-panel').waitFor()
      assert.equal(context.pages().length, 1)
      assert.equal(await page.getByRole('button', { name: 'Open in its own window', exact: true }).count(), 0)
    })

    await scenario('g: phone keeps the browser sheet without a pop-out button', async ({ main }) => {
      const page = await main()
      await watch(page)
      await page.locator('.browser-sheet[open] .browser-panel').waitFor()
      assert.equal(await openButton(page).count(), 0)
    }, { width: 320 })

    await scenario('h–i: attach to the focused main pane, refusal, and Watch with the dock closed', async ({ main, openDock, pop, send }) => {
      const first = await main()
      await openDock(first)
      const popup = await pop(first)
      const second = await main(OTHER_PANE)
      // Drive the window event explicitly. Headless Chromium marks both OS windows as focused.
      await second.evaluate(() => window.dispatchEvent(new Event('focus')))
      await popup.getByText('Second chat', { exact: true }).waitFor()
      await send(popup)
      await status(popup, 'Image added to the message box.')
      const [attachment] = await second.evaluate(() => window.receivedFiles)
      assert.equal(attachment.pane, OTHER_PANE)
      assert.match(attachment.files[0].name, /^browser-.*\.png$/)
      assert.equal(attachment.files[0].type, 'image/png')
      assert(attachment.files[0].size > 0)
      assert.deepEqual(await first.evaluate(() => window.receivedFiles), [])
      await second.evaluate(() => { window.takeFiles = false })
      await send(popup)
      await status(popup, REFUSED)
      await popup.getByRole('button', { name: 'Close notes' }).click()

      if (await dockToggle(second).getAttribute('aria-pressed') === 'true') {
        await dockPanel(second).getByRole('button', { name: 'Close browser', exact: true }).click()
        await dockPanel(second).waitFor({ state: 'detached' })
      }
      await popup.evaluate(() => {
        window.forwardedWatches = []
        window.addEventListener('tether-browser-watch', event => window.forwardedWatches.push(event.detail))
      })
      await watch(second)
      await popup.waitForFunction(() => window.forwardedWatches.some(item => item.tab === 'tab-2'), undefined, { polling: 50 })
      await popup.getByRole('tab', { name: 'Watched tab', selected: true }).waitFor()
      assert.equal(await dockToggle(second).getAttribute('aria-pressed'), 'false')
      assert.equal(await second.locator('.browser-sheet, .browser-panel').count(), 0)
    })

    await scenario('h: no target and no answer errors at 320 px', async ({ context, send }) => {
      const popup = await context.newPage()
      await popup.goto(origin + '/chat/?browser=window')
      await popup.getByText('No Tether window', { exact: true }).waitFor()
      await send(popup)
      await status(popup, NO_CHAT)
      assert.equal(await popup.evaluate(() => document.documentElement.scrollWidth), 320)

      await popup.evaluate(lock => {
        window.unresponsiveMain = new BroadcastChannel(lock)
        window.unresponsiveMain.onmessage = event => { if (event.data.type === 'attach') window.attachSent = true }
        window.unresponsiveMain.postMessage({ type: 'target', id: 'gone', pane: '%1', label: 'Unresponsive chat', focusedAt: 1, theme: 'light' })
      }, LOCK)
      await popup.getByText('Unresponsive chat', { exact: true }).waitFor()
      await popup.getByRole('button', { name: 'Close notes' }).click()
      await popup.clock.install()
      await send(popup)
      await popup.waitForFunction(() => window.attachSent)
      await popup.clock.fastForward(5000)
      await status(popup, NO_CHAT)
    }, { width: 320 })

    await scenario('no hello: fake clock returns to the dock after 10 s', async ({ main, openDock }) => {
      const page = await main()
      await openDock(page)
      await page.clock.install()
      await page.evaluate(() => { window.open = () => ({ close() { window.stubClosed = true } }) })
      await openButton(page).click()
      await away(page).waitFor()
      await page.clock.fastForward(10_000)
      await returned(page)
      await status(page, 'The browser window did not open.')
      assert.equal(await page.evaluate(() => window.stubClosed), true)
    })

    await scenario('API 401: the pop-out shows the sign-in instruction', async ({ context }) => {
      const popup = await context.newPage()
      await popup.goto(origin + '/chat/?browser=window')
      await popup.getByRole('alert').filter({ hasText: 'Sign in to Tether in the main window, then reload this window.' }).waitFor()
    }, { signedIn: false })
  } finally {
    await browser?.close()
    if (server) await new Promise(resolve => server.close(resolve))
  }
}

function watch(page) {
  return page.evaluate(() => window.dispatchEvent(new CustomEvent('tether-browser-watch', {
    detail: { tab: 'tab-2', pane: '%2', browser: 'test' },
  })))
}

run().catch(error => { console.error(error); process.exitCode = 1 })
