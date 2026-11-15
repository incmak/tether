// UI usage (Settings › About & system › UI usage, src/uiUsage.ts, server/ui_usage.py) against an existing build: the
// page logs clicks, opened dialogs and seen controls without tab names or paths, the report renders at 320 px and
// 1280 px, the switch saves the pref, and Clear asks first. Every API request stays in this page's fixtures.
// Run: cd ui && npm run test:ui (or node test_ui_usage_ui.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/ui-usage/shots')
const sizes = [[320, 720], [1280, 900]]
const paneA = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Fix login', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const paneB = { id: '%2', tmux_session: '$1', label: 'project:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/docs', state: 'Idle', writable: true, thread: 't2', approval: null }
const fixture = {
  sessions: { sessions: [paneA, paneB], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { projects: [], items: [] },
}
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
let prefs = {}, version = 1
const row = (label, region, depth, clicks, seen, rate, steps = null) => ({ region, label, kind: 'button', depth, clicks, seen, rate, steps })
const report = {
  enabled: true, days: 30, device: '', visits: 42, clicks: 610, controls: 133, ready: true,
  top: [row('New tab', 'Tether', 0, 120, 42, 0.9, 1), row('Settings', 'Tether', 0, 40, 42, 0.5, 1)],
  demote: [row('Broadcast to every tab at once, with a long label that wraps', 'Header', 0, 0, 40, 0)],
  promote: [row('Shell', 'New tab', 2, 33, 30, 0.6, 3)],
  unused: [row('Export', 'More', 1, 0, 9, 0)],
  views: [{ view: 'Settings', visits: 20, steps: 1, most: 2, keyboard: 3, from: 'Tether' }, { view: 'Autonomy level', visits: 4, steps: 4, most: 5, keyboard: 0, from: 'Settings' }],
}

async function main() {
  let browser, page, server, failed = 0
  const posts = [], pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime0 = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime0[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('console', message => { if (message.type() === 'error') pageErrors.push(message.text()) })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') {
          const body = request.postDataJSON(); posts.push({ endpoint, body })
          if (endpoint === 'prefs') {
            prefs = { ...prefs, ...body.changes }; version++
            return route.fulfill({ json: { prefs, version } })
          }
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs, version } })
        if (endpoint.startsWith('ui-usage')) return route.fulfill({ json: report })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // Serve the build from here: the shared browser may not reach this process's loopback port.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try { return route.fulfill({ body: await fs.readFile(file), contentType: mime[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const usage = () => posts.filter(post => post.endpoint === 'ui-usage').flatMap(post => post.body.events)
    for (const [width, height] of sizes) {
      const at = `${width}px`
      prefs = { selected: '%1' }; version++
      posts.length = 0
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      const chat = page.locator('.chat-panel[data-pane="%1"]').first()
      await chat.waitFor()
      // Open Settings by clicks, then UI usage.
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByRole('button', { name: /About & system/ }).first().click()
      const toggle = page.getByRole('switch', { name: /Log UI usage/ })
      await toggle.waitFor()
      check(`${at} on by default`, await toggle.isChecked())
      await page.getByRole('heading', { name: 'Rarely used on the main screen' }).waitFor()
      const section = page.locator('.ui-usage')
      check(`${at} lists render`, await section.getByText('Shell', { exact: true }).isVisible() && await section.locator('.ui-usage-deep').count() === 1 && await section.getByText('Export', { exact: true }).isVisible())
      check(`${at} no page overflow`, !(await overflow()))
      await page.getByRole('heading', { name: 'UI usage' }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `report-${width}.png`) })
      await page.getByRole('heading', { name: 'Clicks to reach each view' }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `report-views-${width}.png`) })
      await page.waitForTimeout(900)
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')))
      await page.waitForTimeout(200)
      const events = usage()
      const clicks = events.filter(event => event.k === 'click')
      // Phone: Settings sits in the sessions drawer (a layer), one click from the page.
      check(`${at} logs the Settings click`, clicks.some(event => event.l === 'Settings' && (width < 800 ? event.d === 1 && event.n === 2 : event.d === 0 && event.n === 1)), JSON.stringify(clicks))
      check(`${at} logs the click in Settings at depth 1`, clicks.some(event => /About/.test(event.l) && event.d === 1 && event.n >= 2), JSON.stringify(clicks))
      check(`${at} logs Settings opening`, events.some(event => event.k === 'open' && event.l === 'Settings' && event.n >= 1), JSON.stringify(events.filter(event => event.k === 'open')))
      check(`${at} logs seen controls`, events.filter(event => event.k === 'seen').length >= 3, JSON.stringify(events.filter(event => event.k !== 'click').map(event => [event.k, event.r, event.l, event.d])))
      check(`${at} device`, events.every(event => event.w === (width < 800 ? 'phone' : 'wide')), JSON.stringify([...new Set(events.map(event => event.w))]))
      const text = JSON.stringify(events)
      check(`${at} no tab names or paths`, !/Fix login|Docs pass|\/srv\//.test(text), text.match(/.{40}(Fix login|Docs pass|\/srv\/).{40}/)?.[0])
      // Clear asks first.
      await page.getByRole('button', { name: 'Clear the log' }).click()
      await page.getByRole('button', { name: 'Remove' }).click()
      await page.getByText('The UI usage log is empty now.').waitFor()
      check(`${at} clear posts`, posts.some(post => post.endpoint === 'ui-usage/clear'))
      // Turning the switch off saves the pref and stops the log.
      await toggle.click()
      await page.waitForTimeout(400)
      check(`${at} pref saved`, posts.some(post => post.endpoint === 'prefs' && post.body.changes.uiUsage === false), JSON.stringify(posts.map(post => post.endpoint)))
      const before = usage().length
      await page.getByRole('button', { name: 'Clear the log' }).click()
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')))
      await page.waitForTimeout(200)
      check(`${at} off logs nothing`, usage().length === before, String(usage().length - before))
      await page.keyboard.press('Escape')
    }
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
  } finally {
    await page?.close().catch(() => {})
    server?.close()
    await browser?.close?.().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exitCode = failed ? 1 : 0
}
main()
