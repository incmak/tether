// Installed app name and icon (Settings › Appearance, src/AppIdentity.tsx, server/app_identity.py) against an existing
// build: the setting loads, previews the name and colour, saves both, renders at 320 px and 1280 px without overflow,
// and names the save in a status line. Every API request stays in this page's fixtures.
// Run: cd ui && npm run test:ui (or DIST=<build> node test_app_identity_ui.cjs).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-16/app-identity/shots')
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
const COLORS = [['clay', 'Clay', '#C96442'], ['blue', 'Blue', '#2F6BD8'], ['green', 'Green', '#2E7D4F']].map(([id, label, hex]) => ({ id, label, hex }))
let identity = { name: 'Tether', color: 'clay' }

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
          if (endpoint === 'app-identity') {
            identity = { name: body.name, color: body.color }
            return route.fulfill({ json: { ...identity, colors: COLORS } })
          }
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs, version } })
        if (endpoint === 'app-identity') return route.fulfill({ json: { ...identity, colors: COLORS } })
        if (endpoint === 'activity') return route.fulfill({ json: { enabled: false, agents: false, zone: 'UTC', since: null, store: '', days: [], projects: [], publish: { folder: '', file: '', busy: false, pending: false, published: null, last: null } } })
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
    for (const [width, height] of sizes) {
      const at = `${width}px`
      prefs = { selected: '%1' }; version++
      identity = { name: 'Tether', color: 'clay' }
      posts.length = 0
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByRole('searchbox', { name: 'Search settings' }).fill('home screen icon')
      const name = page.getByRole('textbox', { name: 'App name' })
      await name.waitFor()
      check(`${at} loads the saved name`, await name.inputValue() === 'Tether')
      const save = page.getByRole('button', { name: 'Save app name and icon' })
      check(`${at} save waits for a change`, await save.isDisabled())
      await name.fill('  Work   box ')
      await page.getByRole('radio', { name: 'Blue' }).check()
      const preview = page.locator('.app-identity-preview')
      check(`${at} preview shows the name`, (await preview.textContent()) === 'Work box')
      check(`${at} preview shows the colour`, await preview.locator('rect').getAttribute('fill') === '#2F6BD8')
      check(`${at} no page overflow`, !(await overflow()))
      await page.locator('.app-identity').scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `app-identity-${width}.png`) })
      await save.click()
      await page.getByRole('status').filter({ hasText: 'Saved.' }).waitFor()
      const sent = posts.find(post => post.endpoint === 'app-identity')
      check(`${at} posts the trimmed name and colour`, sent && sent.body.name === 'Work box' && sent.body.color === 'blue', JSON.stringify(sent))
      check(`${at} title follows`, await page.title() === 'Work box')
      check(`${at} save turns off after saving`, await save.isDisabled())
      await name.fill('')
      await page.getByRole('radio', { name: 'Green' }).check()
      await save.click()
      check(`${at} empty name is refused here`, await page.getByRole('alert').textContent() === 'Type a name.' && posts.filter(post => post.endpoint === 'app-identity').length === 1)
      await page.screenshot({ path: path.join(SHOTS, `app-identity-error-${width}.png`) })
      await page.keyboard.press('Escape'); await page.keyboard.press('Escape')
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
