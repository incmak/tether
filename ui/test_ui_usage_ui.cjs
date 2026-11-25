// UI usage (Settings › Usage tracking › UI usage, src/uiUsage.ts, server/ui_usage.py) against an existing build: the
// page logs clicks, opened dialogs and seen controls without names or paths, the report renders at 320 px and 1280 px,
// the switch saves the pref, and Clear asks first. Every API request stays in this page's fixtures.
// Run: cd ui && npm run test:ui (or node test_ui_usage_ui.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('ui-usage')
const sizes = [[320, 720], [1280, 900]]
const paneA = { id: '%1', tmux_session: '$1', label: 'acme-site:0.0', pane_name: 'Fix login', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const paneB = { id: '%2', tmux_session: '$1', label: 'acme-site:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/docs', state: 'Idle', writable: true, thread: 't2', approval: null }
const nameLeaks = events => events.filter(event => /Fix login|Docs pass|acme-site|\/srv\//.test(JSON.stringify(event)))
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
  // Answered ui-usage POSTs per page document (X-Usage-Doc, set by the init script). Each answer wakes the waiters, so
  // a test can wait until every POST the current document sent is recorded. A late POST from an earlier document
  // (the pagehide flush on navigation) counts for that document only.
  const usageAnswered = new Map()
  let usageWaiters = []
  const nextUsageAnswer = () => new Promise(resolve => usageWaiters.push(resolve))
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
    await page.clock.install()
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
          const body = request.postDataJSON(), doc = request.headers()['x-usage-doc']; posts.push({ endpoint, body, doc })
          if (endpoint === 'prefs') {
            prefs = { ...prefs, ...body.changes }; version++
            return route.fulfill({ json: { prefs, version } })
          }
          if (endpoint === 'ui-usage') {
            await route.fulfill({ json: { kept: body.events.length, enabled: true, gen: '1' } })
            usageAnswered.set(doc, (usageAnswered.get(doc) ?? 0) + 1)
            for (const wake of usageWaiters.splice(0)) wake()
            return
          }
          if (endpoint === 'ui-usage/clear') return route.fulfill({ json: { cleared: true, gen: '2' } })
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs, version } })
        if (endpoint.startsWith('ui-usage')) return route.fulfill({ json: report })
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
      window.usagePosts = 0
      window.usageDoc = Math.random().toString(36).slice(2)
      const fetchPage = window.fetch
      window.fetch = (input, init) => {
        // Tag every API write with this document, so the test reads only what the current page sent.
        if (typeof input === 'string' && input.startsWith('/mobile/api/') && init?.method && init.method !== 'GET') {
          if (input === '/mobile/api/ui-usage') window.usagePosts++
          const headers = new Headers(init.headers)
          headers.set('X-Usage-Doc', window.usageDoc)
          init = { ...init, headers }
        }
        return fetchPage(input, init)
      }
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    // The document the checks read. Each page load gets a new id (window.usageDoc); a late POST from an earlier
    // document (held, or the pagehide flush on navigation) keeps its own id, so no read below sees it.
    let doc = null
    const oldDocs = new Set()
    const usage = () => posts.filter(post => post.endpoint === 'ui-usage' && post.doc === doc).flatMap(post => post.body.events)
    const waitForPost = (endpoint, match) => page.waitForResponse(response => {
      const request = response.request()
      return new URL(response.url()).pathname === '/mobile/api/' + endpoint && request.method() === 'POST'
        && match(request.postDataJSON(), request.headers()['x-usage-doc'])
    })
    const flush = () => page.evaluate(() => window.dispatchEvent(new Event('pagehide')))
    // Wait until the current document has sent an event that matches and every ui-usage POST it has sent so far is
    // answered and recorded. The page counts POSTs when it calls fetch, so a held or slow POST keeps this waiting.
    // Same 10 s deadline as the page's other waits; the error says what was missing.
    const recorded = async (matches, what) => {
      const deadline = Date.now() + 10_000
      let state = ''
      for (;;) {
        const answered = nextUsageAnswer()
        const { current, sent } = await page.evaluate(() => ({ current: window.usageDoc, sent: window.usagePosts }))
        if (current !== doc) throw new Error(`The page reloaded: document ${current}, the checks read ${doc}`)
        const done = usageAnswered.get(doc) ?? 0
        const found = usage().some(matches)
        if (done >= sent && found) return
        state = `${found ? 'found' : 'no'} ${what}; ${done} of ${sent} ui-usage POSTs answered`
        const left = deadline - Date.now()
        if (left <= 0) throw new Error(`Timed out waiting for the usage log: ${state}`)
        let timer
        await Promise.race([answered, new Promise(resolve => { timer = setTimeout(resolve, left) })])
        clearTimeout(timer)
      }
    }
    // Force a scan of what is on screen now, then wait for its events. One scan logs every control on screen in one
    // pass, and the flush sends them all at once, so `marker` (a control there in every build) marks the whole scan.
    const scan = async (marker, what) => {
      await page.keyboard.press('Shift')
      await page.clock.runFor(1800) // 800 ms input debounce and the 1000 ms idle deadline, in virtual time.
      await flush()
      await recorded(marker, what)
    }
    const scanVisible = async (control, label) => {
      const matches = event => event.k === 'seen' && event.r === 'Settings' && event.l === label
      await control.scrollIntoViewIfNeeded()
      if (!usage().some(matches)) await scan(matches, `seen ${label}`)
    }
    for (const [width, height] of sizes) {
      const at = `${width}px`
      prefs = { selected: '%1' }; version++
      if (doc) oldDocs.add(doc)
      await page.setViewportSize({ width, height })
      // The new page's first (empty) ui-usage POST; an earlier document's late POST does not match.
      const generation = waitForPost('ui-usage', (body, from) => body.events.length === 0 && !!from && !oldDocs.has(from))
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      const firstFrom = (await generation).request().headers()['x-usage-doc']
      doc = await page.evaluate(() => window.usageDoc)
      if (firstFrom !== doc) throw new Error(`${at}: the first ui-usage POST came from document ${firstFrom}, the page is ${doc}`)
      const chat = page.locator('.chat-panel[data-pane="%1"]').first()
      await chat.waitFor()
      const mainSeen = event => event.k === 'seen' && event.d === 0
      await scan(event => mainSeen(event) && event.c === 'tab', 'seen tab on the main screen')
      const strip = chat.locator('.pane-tabs')
      const stripVisible = await strip.isVisible()
      const mainEvents = usage().filter(mainSeen)
      if (stripVisible) check(`${at} logs Session tabs on the main screen`, mainEvents.some(event => event.c === 'tab' && event.r === 'Session tabs'))
      if (await strip.locator('.tab-close.current').isVisible()) {
        check(`${at} logs Close tab on the main screen`, mainEvents.some(event => event.l === 'Close tab'))
      } else {
        check(`${at} tab strip has no visible close button`, !stripVisible || (await strip.locator('.tab-close').count()) === 0)
      }
      // Open Settings by clicks, then UI usage.
      if (width === 320) {
        await chat.getByRole('button', { name: /^Open sessions/ }).click()
        const drawer = page.getByRole('dialog', { name: 'Sessions', exact: true })
        // The drawer opens on Catch up; the tab rows are in the Sessions view.
        await drawer.getByRole('group', { name: 'Sidebar view' }).getByRole('button', { name: 'Sessions', exact: true }).click()
        await drawer.locator('.row-close').first().waitFor()
        const drawerSeen = event => event.k === 'seen' && event.d === 1 && event.r === 'Sessions'
        // A task row of the Sessions view: Catch up has none, and every build logs it.
        await scan(event => drawerSeen(event) && event.c === 'button' && /, Task, idle$/.test(event.l), 'seen task row in the Sessions drawer')
        const seen = usage().filter(drawerSeen)
        check(`${at} logs redacted sidebar close and tab rows`, seen.some(event => event.l === 'Close …') && seen.some(event => event.l === '…, Task, idle'))
        const leaks = nameLeaks(seen)
        check(`${at} sidebar scan has no tab or session names or paths`, !leaks.length, JSON.stringify(leaks))
      }
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByRole('navigation', { name: 'Settings categories' }).getByRole('button', { name: /^Usage tracking/ }).click()
      const toggle = page.getByRole('switch', { name: /Log UI usage/ })
      await toggle.waitFor()
      check(`${at} on by default`, await toggle.isChecked())
      await page.getByRole('heading', { name: 'Rarely used on the main screen' }).waitFor()
      const section = page.locator('.ui-usage')
      check(`${at} lists render`, await section.getByText('Shell', { exact: true }).isVisible() && await section.locator('.ui-usage-deep').count() === 1 && await section.getByText('Export', { exact: true }).isVisible())
      check(`${at} no page overflow`, !(await overflow()))
      await page.getByRole('heading', { name: 'UI usage' }).scrollIntoViewIfNeeded()
      await scanVisible(toggle, 'Log UI usage')
      await page.screenshot({ path: path.join(SHOTS, `report-${width}.png`) })
      await page.getByRole('heading', { name: 'Clicks to reach each view' }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `report-views-${width}.png`) })
      const clear = page.getByRole('button', { name: 'Clear the log' })
      await scanVisible(clear, 'Clear the log')
      const events = usage()
      const clicks = events.filter(event => event.k === 'click')
      // Phone: Settings sits in the sessions drawer (a layer), one click from the page.
      check(`${at} logs the Settings click`, clicks.some(event => event.l === 'Settings' && (width < 800 ? event.d === 1 && event.n === 2 : event.d === 0 && event.n === 1)), JSON.stringify(clicks))
      check(`${at} logs the click in Settings at depth 1`, clicks.some(event => event.l === 'Usage tracking' && event.d === 1 && event.n >= 2), JSON.stringify(clicks))
      check(`${at} logs Settings opening`, events.some(event => event.k === 'open' && event.l === 'Settings' && event.n >= 1), JSON.stringify(events.filter(event => event.k === 'open')))
      const seen = events.filter(event => event.k === 'seen' && event.r === 'Settings').map(event => event.l)
      check(`${at} logs seen controls`, seen.includes('Log UI usage') && seen.includes('Clear the log'), JSON.stringify(seen))
      check(`${at} device`, events.every(event => event.w === (width < 800 ? 'phone' : 'wide')), JSON.stringify([...new Set(events.map(event => event.w))]))
      const leaks = nameLeaks(events)
      check(`${at} no tab or session names or paths`, !leaks.length, JSON.stringify(leaks))
      // Clear asks first.
      await clear.click()
      await page.getByRole('button', { name: 'Remove' }).click()
      await page.getByText('The UI usage log is empty now.').waitFor()
      check(`${at} clear posts`, posts.some(post => post.endpoint === 'ui-usage/clear' && post.doc === doc))
      // Turning the switch off saves the pref and stops the log.
      const saveOff = waitForPost('prefs', (body, from) => from === doc && body.changes.uiUsage === false)
      await toggle.click()
      check(`${at} pref saved`, (await saveOff).ok())
      const before = await page.evaluate(() => window.usagePosts)
      await page.getByRole('button', { name: 'Clear the log' }).click()
      await flush()
      const after = await page.evaluate(() => window.usagePosts)
      check(`${at} off logs nothing`, after === before, String(after - before))
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
