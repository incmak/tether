// Header swipe (tabSwipe.ts): real wheel events (CDP) over the header step one tab and are cancelled, so the browser
// does not go Back or Forward. Over the chat, with the setting off, and on the phone, the wheel passes through.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/header-swipe/shots')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const first = pane('%1', 'Deploy fix'), second = pane('%2', 'Docs pass'), third = pane('%3', 'Review', { agent: 'codex' })
const fixture = {
  sessions: { sessions: [first, second, third], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null }, 'work/board': { projects: [], items: [] },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
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
    if (process.env.DEBUG) page.on('console', message => { if (message.type() === 'error') console.log('console:', message.text().slice(0, 600)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // The shared browser may run on another host: serve dist from here, not through the local server.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        try { return route.fulfill({ status: 200, contentType: mime[path.extname(file)] || 'application/octet-stream', body: await fs.readFile(file) }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      const off = new URL(location.href).searchParams.has('off')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false, ...off ? { headerSwipe: false } : {} }))
      // Bubble phase on window: runs after the header's own listener, so defaultPrevented shows its decision.
      window.__wheels = []
      addEventListener('wheel', event => { if (event.deltaX) window.__wheels.push(event.defaultPrevented) }, { passive: true })
    })
    const selected = () => page.locator('[role="tab"][aria-selected="true"]').first().getAttribute('data-pane')
    const finger = [2, 6, 14, 26, 38, 44, 40, 30, 26, 22, 18, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1]
    // One swipe: the finger part and its momentum, then a pause so the next swipe is a new gesture.
    const swipe = async (sign, at) => {
      await page.mouse.move(at.x, at.y)
      await page.evaluate(() => { window.__wheels = [] })
      for (const dx of finger) await page.mouse.wheel(sign * dx, 0)
      await page.waitForTimeout(260)
      return page.evaluate(() => window.__wheels)
    }
    const centre = async selector => { const box = await page.locator(selector).first().boundingBox(); return { x: box.x + box.width / 2, y: box.y + box.height / 2 } }
    const open = async (width, query = '') => {
      await page.setViewportSize({ width, height: 800 })
      await page.goto(`${origin}/chat/?session=%252${query}`, { waitUntil: 'load' })
      await page.locator('[role="tab"][data-pane="%2"][aria-selected="true"]').waitFor()
    }
    for (const motion of ['no-preference', 'reduce']) {
      await page.emulateMedia({ reducedMotion: motion })
      await open(1280)
      check(`${motion}: desktop layout`, await page.evaluate(() => matchMedia('(min-width: 900px) and (hover: hover) and (pointer: fine)').matches))
      let wheels = await swipe(1, await centre('.chat-header .chat-title'))
      check(`${motion}: a swipe left over the header opens the next tab, one step`, await selected() === '%3', await selected())
      check(`${motion}: every header wheel event is cancelled (no browser Back/Forward)`, wheels.length === finger.length && wheels.every(Boolean), JSON.stringify(wheels))
      await page.screenshot({ path: path.join(SHOTS, `header-swipe-1280-${motion}.png`) })
      wheels = await swipe(-1, await centre('.pane-tabs [data-pane="%3"]'))
      check(`${motion}: a swipe right over the tab strip opens the previous tab`, await selected() === '%2', await selected())
      await swipe(-1, await centre('.pane-tabs'))
      check(`${motion}: a second swipe is a second step`, await selected() === '%1', await selected())
      wheels = await swipe(-1, await centre('.pane-tabs'))
      check(`${motion}: at the first tab the swipe stays put and is still cancelled`, await selected() === '%1' && wheels.every(Boolean), JSON.stringify(wheels))
      wheels = await swipe(1, await centre('.chat-scroll'))
      check(`${motion}: over the chat the swipe passes through to the browser (B81 Back/Forward)`, await selected() === '%1' && wheels.length > 0 && wheels.every(value => !value), JSON.stringify(wheels))
      await page.mouse.move(640, 400)
      await page.evaluate(() => { window.__wheels = [] })
      await page.mouse.wheel(0, 120)
      check(`${motion}: a vertical wheel over the header is not taken`, await selected() === '%1')
    }
    await open(1280, '&off')
    let wheels = await swipe(1, await centre('.chat-header .chat-title'))
    check('setting off: the header swipe passes through (Back/Forward)', await selected() === '%2' && wheels.length > 0 && wheels.every(value => !value), JSON.stringify(wheels))
    // The setting: on by default, a labelled switch in Settings › Sessions & chat that works with the keyboard at 320 px.
    await open(1280)
    await page.locator('button[aria-label="Settings"]').first().click()
    await page.getByLabel('Search settings').fill('swipe')
    const toggle = page.getByRole('switch', { name: /Swipe on the tab bar switches tabs/ })
    check('settings: the switch is on by default', await toggle.isChecked())
    await page.setViewportSize({ width: 320, height: 700 })
    const size = await toggle.boundingBox()
    check('settings 320 px: the switch is visible, at least 24 px, no horizontal scroll', size && size.width >= 24 && size.height >= 24 && size.x + size.width <= 320 && !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), JSON.stringify(size))
    await page.screenshot({ path: path.join(SHOTS, 'settings-320.png') })
    await toggle.focus()
    await page.keyboard.press('Space')
    check('settings: Space turns it off and saves it on this device', !(await toggle.isChecked()) && await page.evaluate(() => JSON.parse(localStorage.getItem('tmux-chat.prefs.v1')).headerSwipe === false))
    await page.keyboard.press('Escape')
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.locator('.chat-header .chat-title').first().waitFor()
    wheels = await swipe(1, await centre('.chat-header .chat-title'))
    check('settings: after the switch is off, the header swipe passes through', await selected() === '%2' && wheels.length > 0 && wheels.every(value => !value), JSON.stringify(wheels))
    await open(390)
    wheels = await swipe(1, await centre('.chat-header .chat-title'))
    check('phone width: no change', await selected() === '%2' && wheels.every(value => !value), JSON.stringify(wheels))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`header swipe: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
