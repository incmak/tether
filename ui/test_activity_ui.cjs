// Activity time (Settings › About & system) and the activity ping against an existing build. Every API request stays
// in this page's fixtures; no terminal is touched and nothing is published.
// Run: cd ui && npm run test:ui (or node test_activity_ui.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/activity-time/shots')
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
const publishOff = { folder: '', file: 'tether-activity.json', busy: false, pending: false, published: null, last: null }
const sampleDays = Array.from({ length: 14 }, (_, i) => ({ day: `2026-11-${String(2 + i).padStart(2, '0')}`, you: i * 1500, agents: i * 3000 }))
const sampleProjects = [{ name: 'a-project-with-a-very-long-name-that-must-not-overflow-the-settings-sheet', you: 7200, agents: 36000 }, { name: 'tether', you: 3600, agents: 0 }]
let activity = { enabled: false, agents: false, zone: 'Asia/Kolkata', since: null, store: '~/.tether/activity', days: [], projects: [], publish: publishOff }

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
    // The refused publish folder answers 400 on purpose; the browser logs that response.
    page.on('console', message => { if (message.type() === 'error' && !message.text().includes('status of 400')) pageErrors.push(message.text()) })
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
            activity = { ...activity, enabled: prefs.activity === true, agents: prefs.activityAgents === true }
            return route.fulfill({ json: { prefs, version } })
          }
          if (endpoint === 'activity') return route.fulfill({ json: { sent: true, reason: '' } })
          if (endpoint === 'activity/publish') {
            if (body.folder === '~/not-git') return route.fulfill({ status: 400, json: { error: 'The folder must be the top folder of a git checkout.' } })
            if ('folder' in body) activity = { ...activity, publish: { ...activity.publish, folder: body.folder } }
            return route.fulfill({ json: { ...activity, started: body.now === true } })
          }
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs, version } })
        if (endpoint === 'activity') return route.fulfill({ json: activity })
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
    const pings = () => posts.filter(post => post.endpoint === 'activity')
    for (const [width, height] of sizes) {
      const at = `${width}px`
      prefs = { selected: '%1' }; version++
      activity = { ...activity, enabled: false, agents: false, since: null, days: [], projects: [], publish: publishOff }
      posts.length = 0
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      const chat = page.locator('.chat-panel[data-pane="%1"]').first()
      await chat.waitFor().catch(async error => { await page.screenshot({ path: path.join(SHOTS, `debug-${width}.png`) }); console.log(pageErrors.join('\n---\n').slice(0, 3000)); throw error })
      await chat.click({ position: { x: 20, y: 120 } })
      await page.waitForTimeout(200)
      check(`${at} off by default: no ping`, pings().length === 0, JSON.stringify(pings()))

      // Settings: the section, its default state and the agent switch that waits for the first one.
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      const search = page.getByPlaceholder(/Search settings/)
      if (await search.count()) await search.fill('activity')
      const keep = page.getByRole('switch', { name: /Keep activity time/ })
      const agents = page.getByRole('switch', { name: /Count agent work/ })
      await keep.waitFor()
      check(`${at} keep switch off`, !(await keep.isChecked()))
      check(`${at} agent switch disabled until keep is on`, await agents.isDisabled())
      await page.getByText('Off. Nothing is counted.').waitFor()
      check(`${at} no page overflow (off)`, !(await overflow()))
      await keep.click()
      await page.waitForFunction(() => document.querySelector('[role="switch"]:checked') !== null)
      check(`${at} agent switch enabled`, !(await agents.isDisabled()))
      await page.waitForTimeout(500)
      check(`${at} pref saved`, posts.some(post => post.endpoint === 'prefs' && post.body.changes.activity === true), JSON.stringify(posts.map(post => post.endpoint)))
      // Clicks inside Settings name no tab, so they send no ping.
      await keep.focus()
      await page.keyboard.press('Tab')
      check(`${at} settings input sends no ping`, pings().length === 0, JSON.stringify(pings()))

      // Data: today's total, a 14-day table and top projects, with long names that must not overflow.
      await agents.click()
      activity = { ...activity, since: 1, days: sampleDays, projects: sampleProjects }
      await page.getByText('Today: 5 h 25 m').waitFor({ timeout: 20000 })
      check(`${at} today shown with agents`, await page.getByText('agents 10 h 50 m').isVisible())
      check(`${at} 14 day rows`, await page.locator('.activity-table').first().locator('tbody tr').count() === 14)
      check(`${at} day table has headers`, await page.getByRole('columnheader', { name: 'Agents' }).first().isVisible())
      check(`${at} top projects`, await page.getByRole('rowheader', { name: 'tether' }).isVisible())
      check(`${at} no page overflow (data)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-data-${width}.png`), fullPage: false })

      // Publish folder: a refusal shows as an alert; a saved folder offers Publish now; a failed publish shows.
      const field = page.getByLabel('Publish folder')
      await field.fill('~/not-git')
      await page.getByRole('button', { name: 'Save folder' }).click()
      await page.getByRole('alert').filter({ hasText: 'top folder of a git checkout' }).waitFor()
      check(`${at} bad folder refused`, true)
      await field.fill('~/stats-activity')
      await page.getByRole('button', { name: 'Save folder' }).click()
      await page.getByText('Saved. Tether publishes every 6 hours').waitFor()
      const now = page.getByRole('button', { name: 'Publish now' })
      await now.click()
      check(`${at} publish now posted`, posts.some(post => post.endpoint === 'activity/publish' && post.body.now === true))
      activity = { ...activity, publish: { ...activity.publish, last: { at: 1, ok: false, error: 'git push failed: fatal: Authentication failed' } } }
      await page.getByRole('alert').filter({ hasText: 'Authentication failed' }).waitFor({ timeout: 20000 })
      check(`${at} publish error shown`, true)
      check(`${at} no page overflow (publish)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-publish-${width}.png`) })

      // Back in the chat: input in the chat panel pings for that tab, once a minute at most.
      await page.keyboard.press('Escape')
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      await chat.waitFor()
      await chat.click({ position: { x: 20, y: 120 } })
      await page.waitForTimeout(300)
      await chat.click({ position: { x: 20, y: 140 } })
      await page.waitForTimeout(300)
      check(`${at} chat input pings its tab once`, pings().length === 1 && pings()[0].body.pane === '%1' && typeof pings()[0].body.zone === 'string', JSON.stringify(pings()))
      // A click on another tab's button counts for that tab, not for the tab that was open before the click.
      const other = page.locator('[role="tab"][data-pane="%2"]').first()
      if (await other.isVisible()) {
        await other.click()
        await page.waitForTimeout(300)
        check(`${at} tab click pings the clicked tab`, pings().length === 2 && pings()[1].body.pane === '%2', JSON.stringify(pings()))
      }
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
