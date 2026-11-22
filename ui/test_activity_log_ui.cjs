// Settings › Activity (ActivityLog.tsx) and the level Undo toast (LevelUndo.tsx), against an existing build. Every API
// request stays in this page's fixtures; no terminal and no real setting is touched.
// Run: cd ui && npm run test:ui (or node test_activity_log_ui.cjs after a build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, shotsDir, prefsStore } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('activity-log')
const sizes = [[320, 640], [1280, 900]]
const now = Math.floor(Date.now() / 1000)
const GROUP = { setting: 'settings', passkey: 'settings', tab: 'tabs', review: 'reviews', backlog: 'backlog', lesson: 'lessons', advisor: 'advisor', approval: 'approvals' }
const id = (at, n) => `${String(Math.round(at * 1000)).padStart(16, '0')}-${n.toString(16).padStart(6, '0')}`
const ui = { source: 'ui', device: 'Chrome on Mac' }
const agent = { source: 'agent-cli', tab: 'Audit Log with a very long tab name that has to wrap on a phone', pane: '%12' }
const agentLabels = new Map([['claude', 'Claude tab'], ['codex', 'Codex tab'], ['antigravity', 'Antigravity tab']])
const actorName = actor => actor.tab || actor.device
  || ((actor.pane || ['agent-cli', 'hook'].includes(actor.source)) ? agentLabels.get(actor.agent) || 'An agent tab' : actor.source)
// 65 entries: 5 named ones, then filler, so Load more has a second page. Newest first.
function entries() {
  const named = [
    { kind: 'setting.codex_level', summary: 'Undo: Codex level 1 → 3', old: 1, new: 3, undo: true, actor: ui },
    { kind: 'setting.codex_level', summary: 'Codex level 3 → 1', old: 3, new: 1, actor: ui },
    { kind: 'setting.autonomy_level', summary: 'Autonomy level for Tether: 4 → 5', old: 4, new: 5, project: 'Tether', actor: { source: 'ui', device: 'Safari on iPhone' } },
    { kind: 'setting.reviewers', summary: 'Reviewers changed: normal tier', old: 'Fable · high', new: 'Opus · medium', actor: ui },
    { kind: 'backlog.start', summary: 'B56 started: Activity log: who changed what, with a long title that wraps', project: 'Tether', actor: agent },
  ]
  const list = named.map((entry, n) => ({ ...entry, at: now - 60 - n * 600 }))
  for (let n = 0; n < 60; n++) list.push({ kind: n % 2 ? 'tab.start' : 'review.verdict', summary: n % 2 ? `Started tab Worker ${n} (claude)` : `Review verdict: APPROVED ${n}`, project: n % 3 ? 'Tether' : 'Other', actor: agent, at: now - 86400 * (1 + Math.floor(n / 10)) - n })
  // A review verdict comes from the server, named by the reviewer tab (review_links.py).
  Object.assign(list[5], { summary: 'Approved by reviewer', actor: { source: 'server', pane: '%40', tab: 'B56 review' } })
  Object.assign(list[6], { summary: 'Started an unnamed tab', actor: { source: 'agent-cli', pane: '%305' } })
  Object.assign(list[7], { summary: 'Closed an unnamed Claude tab', actor: { source: 'hook', pane: '%7', agent: 'claude' } })
  return list.map((entry, n) => ({ id: id(entry.at, n), ...entry }))
}
// Like server/audit.py query(): newest first, filters, a cursor and limit+1.
function query(all, params) {
  const words = (params.get('q') || '').toLowerCase().split(/\s+/).filter(Boolean)
  const kinds = (params.get('kind') || '').split(',').filter(Boolean)
  const limit = Number(params.get('limit') || 50), before = params.get('before')
  const since = params.get('since') ? Number(params.get('since')) : null, until = params.get('until') ? Number(params.get('until')) : null
  const match = entry => (!words.length || words.every(word => JSON.stringify(entry).toLowerCase().includes(word)))
    && (!kinds.length || kinds.some(kind => entry.kind === kind || GROUP[entry.kind.split('.')[0]] === kind))
    && (!params.get('project') || entry.project === params.get('project'))
    && (!params.get('actor') || [...Object.values(entry.actor), actorName(entry.actor)]
      .some(value => value.toLowerCase().includes(params.get('actor').toLowerCase())))
    && (since === null || entry.at >= since) && (until === null || entry.at < until)
    && (!before || entry.id < before)
  const found = all.filter(match).sort((a, b) => b.id.localeCompare(a.id)).slice(0, limit + 1)
  const page = found.slice(0, limit)
  const result = { entries: page, next: found.length > limit ? page[page.length - 1].id : null, retention_days: 90 }
  if (params.get('facets') === '1') result.facets = {
    kinds: [], projects: [...new Set(all.map(entry => entry.project).filter(Boolean))].sort(),
    actors: [...new Set(all.map(entry => actorName(entry.actor)))].sort(),
  }
  return result
}
const levels = [0, 1, 2, 3, 4, 5].map(level => ({ level, name: `Level ${level}`, summary: `What level ${level} does.` }))
const codex = global => ({ levels, global, level: global, source: 'global', project: null, runs: { today: 0, running: 0, panes: [] }, run: { model: 'gpt-6.2-sol', effort: 'high', fast: true }, models: [] })
const autonomyState = global => ({ levels, global, level: global, source: 'global', project: null, decisions: [] })
const pane = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Activity test', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const fixture = {
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  sessions: { sessions: [pane], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] }, subagents: { main: null, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'push/config': { enabled: false },
  // The Project sheet (Project.tsx) for the project Undo check.
  'projects/project': { id: 'a1b2c3d4e5f6', name: 'web', revision: 1, budget: 10000, budget_min: 1000, budget_max: 10000, usable_budget_max: 9000,
    instructions: '', pins: [], notes: [], preview: 'Built-in guidance.\n', chars: 0, fixed_chars: 19, folder: '/srv/projects/a1b2c3d4e5f6', session_name: 'web' },
  'projects/advisor-log': { entries: [], keep: 200 },
  'claude-advisor': { models: [{ model: 'off', name: 'Off', summary: 'Tether adds no advisor.' }], global: 'off', model: 'off', source: 'global', project: { override: false, model: 'off' }, user_setting: null, problems: {} },
  'commit-gate': { modes: [{ mode: 'observe', name: 'Observe', summary: 'Report commits that would be blocked.' }], global: 'observe', mode: 'observe', source: 'global', project: { override: false, mode: 'observe' }, stats: { days: 7, commits: 0, passed: 0, would_block: 0, blocked: 0, let_through: 0, unknown: 0, stamped: 0, unstamped: 0, false_blocks: 0, rate: null }, max_blocks: 3 },
}

async function main() {
  let browser, page, server, failed = 0, finished = false
  const pageErrors = [], unexpected = new Set(), asks = [], posts = []
  let mode = 'rows'  // rows | empty | error
  let all = entries(), level = 3, rev = 0, autonomy = 4, projectCodex = { override: false, level: 3 }
  let prefs = prefsStore({ zone: 'America/New_York' })
  // A request the test holds: `seen` resolves when it arrives, and it is answered only on `release()`, or with
  // release({ status, json }) instead. The test waits on these events, never on a timer.
  const gates = []
  const hold = match => {
    let arrived, open
    const seen = new Promise(resolve => { arrived = resolve }), opened = new Promise(resolve => { open = resolve })
    gates.push({ match, arrived, opened })
    // A request that never comes fails the run after 10 s, like a Playwright wait; it does not hang it.
    const late = new Promise((resolve, reject) => { setTimeout(() => reject(new Error('A held request did not come within 10 s')), 10000).unref() })
    return { seen: Promise.race([seen, late]), release: reply => open(reply || null) }
  }
  const gated = async (request, url) => {
    const at = gates.findIndex(gate => gate.match(request, url))
    if (at < 0) return null
    const [gate] = gates.splice(at, 1)
    gate.arrived()
    return gate.opened
  }
  const isAudit = (url, test = () => true) => url.pathname.endsWith('/api/audit') && test(url.searchParams)
  const isUndo = (name, request) => request.method() === 'POST' && request.url().includes(`/api/${name}`) && !!request.postDataJSON()?.undo
  // Lets the page finish the answer it just got: two frames after the request ended.
  const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      // No keep-alive: Node closes an idle socket after 5 s, and route.fetch could reuse it at that moment (ECONNRESET).
      res.setHeader('Connection', 'close')
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
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (endpoint === 'prefs') return route.fulfill({ json: request.method() === 'GET' ? prefs.get() : prefs.post(request.postDataJSON()) })
        if (request.method() === 'POST' && endpoint === 'ui-usage') return route.fulfill({ json: { gen: '' } })
        if (endpoint === 'audit') {
          if (request.method() !== 'GET') { unexpected.add(`${request.method()} audit`); return route.fulfill({ status: 405, json: { error: 'read only' } }) }
          asks.push(url.searchParams)
          const reply = await gated(request, url)
          if (reply) return route.fulfill(reply)
          if (mode === 'error') return route.fulfill({ status: 500, json: { error: 'Tether could not read the audit log.' } })
          return route.fulfill({ json: query(mode === 'empty' ? [] : all, url.searchParams) })
        }
        if (endpoint === 'codex-level') {
          if (request.method() === 'POST') {
            const body = request.postDataJSON()
            posts.push({ body, client: request.headers()['x-tether-client'] })
            const reply = await gated(request, url)
            if (reply) return route.fulfill(reply)
            // Like server/codex_level.py: an Undo must send the revision of its change, or the server refuses it.
            if (body.undo && body.revision !== `r${rev}`) return route.fulfill({ status: 409, json: { error: 'The level changed after this change. Undo would overwrite the newer value, so Tether did not change it.' } })
            // With server/audit.py: one record per change, "Undo: " for the toast's undo.
            if (body.project) {
              const old = { ...projectCodex }
              projectCodex = { override: body.override, level: body.level }
              rev++
              return route.fulfill({ json: { ...codex(level), source: projectCodex.override ? 'project' : 'global', project: projectCodex, change: { old, new: { ...projectCodex }, revision: `r${rev}` } } })
            }
            const old = level
            if (typeof body.level === 'number' && body.level !== level) {
              const at = Date.now() / 1000
              all = [{ id: id(at, 900 + posts.length), at, kind: 'setting.codex_level', summary: `${body.undo ? 'Undo: ' : ''}Codex level ${level} → ${body.level}`, old: level, new: body.level, undo: body.undo || undefined, actor: ui }, ...all]
              level = body.level
            }
            rev++
            return route.fulfill({ json: { ...codex(level), change: { old, new: level, revision: `r${rev}` } } })
          }
          return route.fulfill({ json: url.searchParams.get('project') ? { ...codex(level), project: projectCodex } : codex(level) })
        }
        if (endpoint === 'autonomy') {
          if (request.method() === 'POST') {
            const body = request.postDataJSON()
            posts.push({ body, client: request.headers()['x-tether-client'], endpoint })
            const reply = await gated(request, url)
            if (reply) return route.fulfill(reply)
            const old = autonomy
            autonomy = body.level
            return route.fulfill({ json: { ...autonomyState(autonomy), change: { old, new: autonomy, revision: 'a1' } } })
          }
          return route.fulfill({ json: autonomyState(autonomy) })
        }
        if (request.method() !== 'GET') { unexpected.add(`POST ${endpoint}`); return route.fulfill({ status: 500, json: { error: 'no writes here' } }) }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })  // the other Settings sections load their own data; this test does not check them
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
      unexpected.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const section = page.locator('section.activity-log')
    const open = async (width, query = 'activity log') => {
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByPlaceholder(/Search settings/).fill(query)
    }
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const shot = async (name, target = section) => {
      await target.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
      await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
      await page.screenshot({ path: path.join(SHOTS, `${name}-dark.png`) })
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
    }
    // WCAG 2.2 1.4.3: each text against the background it sits on (every ancestor layer composited), 4.5:1.
    const contrast = (root, selector) => root.evaluate((node, selector) => {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      const paint = layers => {
        ctx.globalCompositeOperation = 'source-over'; ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1, 1)
        for (const color of layers) { ctx.fillStyle = '#fff'; ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1) }
        return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3)
      }
      const lum = rgb => { const c = rgb.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] }
      return [...node.querySelectorAll(selector)].filter(el => el.getClientRects().length).map(el => {
        const layers = []
        for (let at = el; at; at = at.parentElement) layers.unshift(getComputedStyle(at).backgroundColor)
        const back = paint(layers), text = paint([...layers, getComputedStyle(el).color])
        const [hi, lo] = [lum(back), lum(text)].sort((a, b) => b - a)
        return { text: el.textContent.trim().slice(0, 24), ratio: Math.round((hi + 0.05) / (lo + 0.05) * 100) / 100 }
      })
    }, selector)
    const TEXT = '.audit-time, .audit-summary, .audit-change .audit-old, .audit-change b, .audit-meta span, .audit-undo, .audit-day h4, .setting-note, .audit-dates label span, .sm-trigger span, button'
    const low = list => list.filter(item => item.ratio < 4.5).map(item => `${item.text} ${item.ratio}`).join('; ')

    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      all = entries(); level = 3; autonomy = 4; posts.length = 0

      // Loading, then rows: newest first, the actor in words, old → new, the Undo mark.
      const initial = hold((request, url) => isAudit(url))
      await open(width)
      await initial.seen
      check(`${at} loading skeleton announces itself`, (await section.getByText('Loading the activity log').count()) > 0)
      initial.release()
      await section.locator('.audit-row').first().waitFor()
      const rows = section.locator('.audit-row')
      check(`${at} first page has 50 rows`, await rows.count() === 50, String(await rows.count()))
      const first = await rows.nth(0).innerText()
      check(`${at} newest first, with the Undo mark`, /Undo\s*Undo: Codex level 1 → 3/.test(first), first)
      check(`${at} a row names the device`, first.includes('Chrome on Mac'), first)
      check(`${at} the summary carries old → new once`, (first.match(/1 → 3/g) || []).length === 1 && await rows.nth(0).locator('.audit-change').count() === 0, first)
      const plain = rows.filter({ hasText: 'Reviewers changed' })
      check(`${at} a row whose summary has no values shows old → new`, (await plain.locator('.audit-old').innerText()) === 'Fable · high' && (await plain.locator('.audit-change b').innerText()) === 'Opus · medium')
      check(`${at} screen readers hear "Changed from … to …"`, (await plain.locator('.audit-change .visually-hidden').allTextContents()).join('|') === 'Changed from | to ')
      check(`${at} screen readers hear the old value too`, await plain.locator('.audit-old').getAttribute('aria-hidden') === null)
      check(`${at} a review verdict names the reviewer tab`, /Approved by reviewer[\s\S]*B56 review/.test(await rows.filter({ hasText: 'Approved by reviewer' }).innerText()))
      check(`${at} an agent row names the tab and the project`, /Audit Log with a very long tab name[\s\S]*Tether/.test(await rows.nth(4).innerText()))
      const unnamed = rows.filter({ hasText: 'Started an unnamed tab' }).locator('.audit-actor')
      const claude = rows.filter({ hasText: 'Closed an unnamed Claude tab' }).locator('.audit-actor')
      check(`${at} a legacy pane actor has a generic label`, await unnamed.innerText() === 'An agent tab')
      check(`${at} an agent actor has a named label and keeps the hook suffix`, await claude.innerText() === 'Claude tab · hook')
      check(`${at} rows sit under day headings`, (await section.locator('.audit-day h4').first().innerText()).toLowerCase() === 'today')
      check(`${at} a row has a machine time`, !!(await rows.nth(0).locator('time').getAttribute('datetime')))
      const shown = await section.locator('.visually-hidden').evaluateAll(els => els.filter(el => el.getBoundingClientRect().width > 1).map(el => el.textContent))
      check(`${at} screen-reader text is not on screen`, !shown.length, shown.join(' | '))
      check(`${at} no page overflow (rows)`, !(await overflow()))
      const wide = await section.evaluate(node => [...node.querySelectorAll('*')].filter(el => el.getBoundingClientRect().right > innerWidth + 1).length)
      check(`${at} nothing runs past the screen`, wide === 0, String(wide))
      const targets = await section.locator('button, input, .sm-trigger').evaluateAll(els => els.filter(el => el.getClientRects().length).map(el => Math.round(el.getBoundingClientRect().height)))
      check(`${at} targets at least 24 px tall`, targets.length > 5 && targets.every(h => h >= 24), targets.join(','))
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
        const found = await contrast(section, TEXT)
        check(`${at} ${theme} text contrast at least 4.5:1`, found.length > 30 && !low(found), low(found) || `${found.length} texts`)
      }
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
      await shot(`rows-${width}`)

      // Load more adds the older page under it, with no repeats, then says it is the end.
      await section.getByRole('button', { name: 'Load more' }).click()
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 65)
      const ids = await rows.evaluateAll(els => els.map(el => el.querySelector('.audit-summary').textContent))
      check(`${at} Load more: 65 rows, no repeats`, new Set(ids).size === 65, String(ids.length))
      check(`${at} Load more sends the cursor`, asks.some(params => params.get('before')))
      // B56 review: a refresh after Load more must not drop the rows that slid below the first page.
      const total = all.length
      all = [0, 1, 2].map(n => ({ id: id(Date.now() / 1000 + n, 800 + n), at: Date.now() / 1000 + n, kind: 'tab.start', summary: `Fresh tab ${n}`, actor: ui })).reverse().concat(all)
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      await page.waitForFunction(count => document.querySelectorAll('section.activity-log .audit-row').length === count, total + 3)
      const merged = await rows.evaluateAll(els => els.map(el => el.querySelector('.audit-summary').textContent))
      check(`${at} a refresh after Load more keeps every row once, newest first`, new Set(merged).size === total + 3 && merged[0] === 'Fresh tab 2', `${merged.length} ${merged[0]}`)
      // Three refreshes in a row, each with new rows: every row stays, once.
      for (let round = 1; round <= 3; round++) {
        all = [{ id: id(Date.now() / 1000 + 10 + round, 820 + round), at: Date.now() / 1000 + 10 + round, kind: 'tab.start', summary: `Round ${['one', 'two', 'three'][round - 1]}`, actor: ui }, ...all]
        await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
        await page.waitForFunction(count => document.querySelectorAll('section.activity-log .audit-row').length === count, total + 3 + round)
      }
      const rounds = await rows.evaluateAll(els => els.map(el => el.querySelector('.audit-summary').textContent))
      check(`${at} three refreshes keep every row once`, new Set(rounds).size === total + 6 && rounds[0] === 'Round three', `${rounds.length} ${rounds[0]}`)
      check(`${at} the end says how long Tether keeps it`, await section.getByText('That is all. Tether keeps 90 days.').isVisible())
      // More than a page of new rows in one refresh: the new first page does not reach the list, so the sheet starts
      // again at the newest entry, with Load more, and no gap between the two.
      const burst = Array.from({ length: 55 }, (_, n) => ({ id: id(Date.now() / 1000 + 100 + n, 1000 + n), at: Date.now() / 1000 + 100 + n, kind: 'tab.start', summary: `Burst ${n}`, actor: ui }))
      all = [...burst.reverse(), ...all]
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      await section.locator('.audit-row').first().filter({ hasText: 'Burst 54' }).waitFor()
      const after = await rows.evaluateAll(els => els.map(el => el.querySelector('.audit-summary').textContent))
      check(`${at} a refresh with a gap starts again at the newest page`, after.length === 50 && after[0] === 'Burst 54' && after[49] === 'Burst 5', `${after.length} ${after[0]} ${after[after.length - 1]}`)
      check(`${at} after a gap, Load more is back`, await section.getByRole('button', { name: 'Load more' }).isVisible())
      // B56 review 3: a Load more still on its way when a refresh resets the list adds nothing. Twice: from the first
      // page, and from a list that already has two pages.
      const search = section.getByRole('searchbox', { name: 'Search the activity log' })
      const loadMore = section.locator('.audit-more')
      const summaries = () => rows.evaluateAll(els => els.map(el => el.querySelector('.audit-summary').textContent))
      const refreshNow = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      for (const pages of [1, 2]) {
        if (pages === 2) {
          await loadMore.click()
          await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 100)
        }
        const held = hold((request, url) => isAudit(url, params => !!params.get('before')))
        await loadMore.click()
        await held.seen
        const stamp = Date.now() / 1000 + 300 + pages * 100
        all = [...Array.from({ length: 55 }, (_, n) => ({ id: id(stamp + n, 3000 + pages * 100 + n), at: stamp + n, kind: 'tab.start', summary: `Gap ${pages}-${n}`, actor: ui })).reverse(), ...all]
        await refreshNow()
        await rows.first().filter({ hasText: `Gap ${pages}-54` }).waitFor()
        const ended = page.waitForEvent('requestfinished', request => request.url().includes('before='))
        held.release()
        await ended; await settle()
        const kept = await summaries()
        check(`${at} a Load more from ${pages} page(s) that ends after a reset adds nothing`, kept.length === 50 && kept[0] === `Gap ${pages}-54` && kept[49] === `Gap ${pages}-5`, `${kept.length} ${kept[0]} ${kept[kept.length - 1]}`)
        check(`${at} after that, Load more works (${pages})`, await loadMore.innerText() === 'Load more' && await loadMore.isEnabled())
      }
      // A failed Load more shows its error only under its own list. (a) It fails, then the filter changes.
      const failure = { status: 500, json: { error: 'Tether could not read the older page.' } }
      const moreAlert = section.getByRole('alert').filter({ hasText: 'Tether could not read the older page.' })
      let heldPage = hold((request, url) => isAudit(url, params => !!params.get('before')))
      await loadMore.click()
      await heldPage.seen
      heldPage.release(failure)
      await moreAlert.waitFor()
      check(`${at} a failed Load more says why and offers Try again`, (await moreAlert.innerText()).includes('Could not load more. Tether could not read the older page.') && await loadMore.innerText() === 'Try again')
      await search.fill('Round two')
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)
      check(`${at} the Load more error does not follow to the next filter`, await moreAlert.count() === 0)
      // (b) The filter changes while the page is on its way, then the page fails.
      await search.fill('')
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
      heldPage = hold((request, url) => isAudit(url, params => !!params.get('before')))
      await loadMore.click()
      await heldPage.seen
      await search.fill('Round two')
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)
      const failedLate = page.waitForEvent('requestfinished', request => request.url().includes('before='))
      heldPage.release(failure)
      await failedLate; await settle()
      check(`${at} a page that fails after a filter change shows no error`, await moreAlert.count() === 0)
      await search.fill('')
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
      // B56 review 4: a page asked before a filter change and back has the same query and cursor as the page asked
      // after it. Only the newer one may change the list, whether the older one answers with a page or an error.
      for (const [label, old] of [['a page', { json: { entries: [{ ...all[60], id: id(1, 999999), at: 1, summary: 'Old page' }], next: null, retention_days: 90 } }], ['an error', failure]]) {
        const first = hold((request, url) => isAudit(url, params => !!params.get('before')))
        await loadMore.click()
        await first.seen
        await search.fill('Round two')
        await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)
        await search.fill('')
        await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
        const second = hold((request, url) => isAudit(url, params => !!params.get('before')))
        await loadMore.click()
        await second.seen
        const firstEnded = page.waitForEvent('requestfinished', request => request.url().includes('before='))
        first.release(old)
        await firstEnded; await settle()
        const secondEnded = page.waitForEvent('requestfinished', request => request.url().includes('before='))
        second.release()
        await secondEnded; await settle()
        const shown = await summaries()
        check(`${at} after a filter change and back, ${label} for the older Load more does not replace the newer one`,
          shown.length === 100 && !shown.includes('Old page') && await moreAlert.count() === 0 && await loadMore.innerText() === 'Load more', `${shown.length} ${shown[shown.length - 1]}`)
        await search.fill('Round two')
        await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)
        await search.fill('')
        await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
      }
      // A filter whose reply is slow: the old list and its Load more must not show under the new filter.
      const slow = hold((request, url) => isAudit(url, params => params.get('q') === 'Round two'))
      await section.getByRole('searchbox', { name: 'Search the activity log' }).fill('Round two')
      await slow.seen
      await page.waitForFunction(() => document.querySelector('section.activity-log .skeleton, section.activity-log [aria-busy="true"]') || !document.querySelector('section.activity-log .audit-row'))
      check(`${at} while a filter loads, no old rows and no Load more`, await rows.count() === 0 && await section.getByRole('button', { name: 'Load more' }).count() === 0 && await section.locator('.audit-end').count() === 0)
      slow.release()
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)
      check(`${at} the slow filter shows only its match`, (await rows.nth(0).innerText()).includes('Round two'))
      await section.getByRole('searchbox', { name: 'Search the activity log' }).fill('')
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
      all = all.filter(entry => !/^(Fresh tab|Round|Burst|Gap) /.test(entry.summary))

      // Search: one request after typing stops, and it starts again at the newest entry.
      const before = asks.length
      await section.getByRole('searchbox', { name: 'Search the activity log' }).fill('autonomy')
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)
      check(`${at} search filters the list`, (await rows.nth(0).innerText()).includes('Autonomy level for Tether: 4 → 5'))
      check(`${at} typing sends one request, not one per key`, asks.slice(before).filter(params => params.get('q')).length === 1, String(asks.length - before))
      check(`${at} the end says it is the matching activity`, await section.getByText('End of the matching activity.').isVisible())

      // Type filter (keyboard): a group name goes to the server as kind.
      await section.getByRole('searchbox', { name: 'Search the activity log' }).fill('')
      const type = section.getByRole('button', { name: /^Type:/ })
      await type.focus(); await page.keyboard.press('Enter')
      await page.getByRole('option', { name: 'Backlog' }).click()
      await section.locator('.audit-row', { hasText: 'B56 started' }).waitFor()
      check(`${at} Type filter sends kind=backlog`, asks[asks.length - 1].get('kind') === 'backlog')
      check(`${at} Type filter shows only backlog`, await rows.count() === 1)
      // Agent filter from the facets.
      const who = section.getByRole('button', { name: /^Agent or device:/ })
      await who.click()
      await page.getByRole('option', { name: 'Safari on iPhone' }).click()
      await section.getByText('No matching activity').waitFor()
      check(`${at} two filters with no match show No matching activity`, await section.getByText('Nothing in the last 90 days matches these filters.').isVisible())
      await shot(`no-results-${width}`)
      await section.locator('.audit-state').getByRole('button', { name: 'Clear filters' }).click()
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
      check(`${at} Clear filters brings back every row`, !asks[asks.length - 1].get('kind') && !asks[asks.length - 1].get('actor'))
      await who.focus()
      await page.keyboard.press('Enter')
      await page.getByRole('option', { name: 'An agent tab', exact: true }).click()
      await unnamed.waitFor()
      check(`${at} the unnamed agent facet returns its row`, await rows.count() === 1
        && asks[asks.length - 1].get('actor') === 'An agent tab')
      await section.locator('.audit-tools').getByRole('button', { name: 'Clear filters' }).click()
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 50)
      // A date filter covers a whole day in Tether, including the 23-hour spring DST day.
      const from = section.locator('.audit-dates input').first(), to = section.locator('.audit-dates input').last()
      const dateRange = (start, end) => page.waitForRequest(request => isAudit(new URL(request.url()), params => (
        Number(params.get('since')) === Date.parse(start) / 1000 && Number(params.get('until')) === Date.parse(end) / 1000)))
      const ranges = (start, end) => dateRange(`2026-03-08T${start}Z`, `2026-03-09T${end}Z`)
      const pullPrefs = () => page.evaluate(() => window.dispatchEvent(new Event('tether-prefs-pull')))
      const spring = ranges('05:00:00', '04:00:00')
      await from.fill('2026-03-08')
      await to.fill('2026-03-08')
      await spring
      check(`${at} the server's zone covers the 23-hour day`, true)
      prefs.post({ changes: { timeZone: 'Asia/Kolkata' } })
      const kolkata = dateRange('2026-03-07T18:30:00Z', '2026-03-08T18:30:00Z')
      await pullPrefs()
      await kolkata
      check(`${at} the synced override updates the open date filter`, true)
      prefs.post({ changes: { timeZone: undefined } })
      const restored = ranges('05:00:00', '04:00:00')
      await pullPrefs()
      await restored
      prefs = prefsStore({ ...prefs.get(), zone: 'UTC' })
      const utc = ranges('00:00:00', '00:00:00')
      await pullPrefs()
      await utc
      check(`${at} the server default updates without a prefs version change`, true)
      prefs = prefsStore({ ...prefs.get(), zone: 'America/New_York' })
      const resetZone = ranges('05:00:00', '04:00:00')
      await pullPrefs()
      await resetZone
      await section.locator('.audit-tools').getByRole('button', { name: 'Clear filters' }).click()

      // Empty and error states.
      mode = 'empty'
      await open(width)
      await section.getByText('Nothing recorded yet').waitFor()
      check(`${at} empty state says what gets recorded`, await section.getByText('It never records messages, typed text or secrets.', { exact: false }).isVisible())
      check(`${at} no page overflow (empty)`, !(await overflow()))
      await shot(`empty-${width}`)
      mode = 'error'
      await open(width)
      await section.getByRole('alert').waitFor()
      check(`${at} error says what failed`, (await section.getByRole('alert').innerText()).includes('Tether could not read the audit log.'))
      await shot(`error-${width}`)
      mode = 'rows'
      await section.getByRole('button', { name: 'Retry' }).click()
      await section.locator('.audit-row').first().waitFor()
      check(`${at} Retry loads the rows`, await rows.count() === 50)
      // A failed refresh keeps the rows under an error; the next filter must not show that error while it loads.
      mode = 'error'
      await refreshNow()
      await section.getByRole('alert').filter({ hasText: 'Could not refresh' }).waitFor()
      const nextFilter = hold((request, url) => isAudit(url, params => params.get('q') === 'autonomy'))
      await search.fill('autonomy')
      await nextFilter.seen
      check(`${at} a refresh error does not show under the next filter`, await section.getByRole('alert').count() === 0)
      mode = 'rows'
      nextFilter.release()
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 1)

      // The Undo toast after a Codex level change, inside the open Settings sheet. Undo sends the old level with undo: true.
      await open(width, 'codex level')
      await page.locator('input[name="codex-level-global"][value="1"]').waitFor({ state: 'attached' })
      await page.locator('label', { has: page.locator('input[name="codex-level-global"][value="1"]') }).click()
      const toast = page.locator('dialog[open] .undo-toast')
      await toast.waitFor()
      check(`${at} the toast names the change`, (await toast.innerText()).includes('Codex level 3 → 1'))
      check(`${at} the toast is inside the open dialog and on screen`, await toast.evaluate(el => { const r = el.getBoundingClientRect(); const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!el.closest('dialog[open]') && !!hit && el.contains(hit) }))
      check(`${at} the change sends X-Tether-Client: web`, posts[0]?.client === 'web' && posts[0].body.level === 1 && !posts[0].body.undo, JSON.stringify(posts))
      check(`${at} no page overflow (toast)`, !(await overflow()))
      const toastTargets = await toast.locator('button').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().height)))
      check(`${at} toast buttons at least 24 px`, toastTargets.every(h => h >= 24), toastTargets.join(','))
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
        const found = await contrast(toast, 'span, button')
        check(`${at} ${theme} toast contrast at least 4.5:1`, found.length >= 2 && !low(found), low(found) || `${found.length} texts`)
      }
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
      await shot(`toast-${width}`, toast)
      await toast.getByRole('button', { name: 'Undo' }).click()
      await toast.getByText('Undid: Codex level 1 → 3').waitFor()
      check(`${at} Undo sends the old level with undo: true and the revision of its change`, posts[1]?.body.level === 3 && posts[1].body.undo === true && posts[1].body.revision === `r${rev - 1}` && posts[1].client === 'web', JSON.stringify(posts[1]))
      check(`${at} the level is back to 3`, await page.locator('input[name="codex-level-global"][value="3"]').isChecked())
      check(`${at} the done toast has no second Undo`, await toast.getByRole('button', { name: 'Undo' }).count() === 0)
      // The log shows both records, the undo marked.
      await page.getByPlaceholder(/Search settings/).fill('activity log')
      await section.locator('.audit-row').first().waitFor()
      const top = await rows.evaluateAll(els => els.slice(0, 2).map(el => el.innerText))
      check(`${at} the log shows the change and its undo`, /Undo: Codex level 1 → 3[\s\S]*Chrome on Mac/.test(top[0]) && /Codex level 3 → 1/.test(top[1]), top.join(' | '))

      // The server holds 4 while this tab still shows 3: the toast and its Undo use the server's 4.
      await open(width, 'codex level')
      await page.locator('input[name="codex-level-global"][value="3"]').waitFor({ state: 'attached' })
      level = 4; rev++
      await page.locator('label', { has: page.locator('input[name="codex-level-global"][value="1"]') }).click()
      await toast.getByText('Codex level 4 → 1').waitFor()
      check(`${at} the toast names the value the server replaced`, (await toast.innerText()).includes('Codex level 4 → 1'))
      // Another device writes 1 → 2 → 1. The value matches again, but the Undo is stale: the server refuses it.
      level = 2; rev++; level = 1; rev++
      const sent = posts.length
      await toast.getByRole('button', { name: 'Undo' }).click()
      await page.locator('dialog[open] section[aria-labelledby="settings-codex"] .error').waitFor()
      check(`${at} a stale Undo sends the old revision and is refused`, posts[sent]?.body.level === 4 && posts[sent].body.revision === `r${rev - 2}` && level === 1, JSON.stringify(posts[sent]))
      check(`${at} a refused Undo says why`, (await page.locator('dialog[open] section[aria-labelledby="settings-codex"] .error').innerText()).includes('The level changed after this change'))
      check(`${at} a refused Undo says why in its toast`, (await toast.getByRole('alert').innerText()).includes('Could not undo. The level changed after this change') && await toast.getByRole('button', { name: 'Undo' }).count() === 0)
      await shot(`toast-failed-${width}`, toast)
      await toast.getByRole('button', { name: 'Dismiss' }).click()

      // One sheet, one toast: an autonomy change, then a Codex change, leave only the Codex offer.
      await open(width, 'level')
      await page.locator('input[name="autonomy-global"][value="3"]').waitFor({ state: 'attached' })
      await page.locator('label', { has: page.locator('input[name="autonomy-global"][value="3"]') }).click()
      await page.locator('dialog[open] .undo-toast', { hasText: 'Autonomy level 4 → 3' }).waitFor()
      await page.locator('label', { has: page.locator('input[name="codex-level-global"][value="2"]') }).click()
      await page.locator('dialog[open] .undo-toast', { hasText: 'Codex level 1 → 2' }).waitFor()
      check(`${at} a second change replaces the first offer: one toast`, await page.locator('dialog[open] .undo-toast').count() === 1)
      level = 3; rev++

      // B56 review 3: while an Undo is on its way, the level controls wait, so no newer change can cross it.
      const codexInput = value => page.locator(`input[name="codex-level-global"][value="${value}"]`)
      const codexPick = value => page.locator('label', { has: codexInput(value) }).click()
      await open(width, 'level')
      await page.locator('input[name="codex-level-global"][value="3"]:checked').waitFor({ state: 'attached' })
      await codexPick(1)
      await toast.getByText('Codex level 3 → 1').waitFor()
      let heldUndo = hold(request => isUndo('codex-level', request))
      await toast.getByRole('button', { name: 'Undo' }).click()
      await heldUndo.seen
      check(`${at} while an Undo runs, the Codex and autonomy levels wait`, await codexInput(2).isDisabled() && await page.locator('input[name="autonomy-global"][value="2"]').isDisabled())
      check(`${at} the running Undo says so`, await toast.getByRole('button', { name: 'Undoing…' }).isDisabled())
      heldUndo.release()
      await toast.getByText('Undid: Codex level 1 → 3').waitFor()
      check(`${at} after the Undo, the levels work again`, await codexInput(2).isEnabled() && await page.locator('input[name="autonomy-global"][value="2"]').isEnabled())
      // Dismissed while its Undo runs: the answer does not bring the toast back.
      await codexPick(1)
      await toast.getByText('Codex level 3 → 1').waitFor()
      heldUndo = hold(request => isUndo('codex-level', request))
      await toast.getByRole('button', { name: 'Undo' }).click()
      await heldUndo.seen
      await toast.getByRole('button', { name: 'Dismiss' }).click()
      const undoEnded = page.waitForEvent('requestfinished', request => isUndo('codex-level', request))
      heldUndo.release()
      await undoEnded; await settle()
      check(`${at} an Undo dismissed while it runs shows no toast after`, await page.locator('dialog[open] .undo-toast').count() === 0 && level === 3)
      // The setting left the screen (search moved to the log). A refused Undo still says why; a good one still says Undid.
      await codexPick(1)
      await toast.getByText('Codex level 3 → 1').waitFor()
      await page.getByPlaceholder(/Search settings/).fill('activity log')
      await section.locator('.audit-row').first().waitFor()
      level = 2; rev++  // another device
      await toast.getByRole('button', { name: 'Undo' }).click()
      await toast.getByRole('alert').waitFor()
      check(`${at} away from the setting, a refused Undo says why`, (await toast.getByRole('alert').innerText()).includes('Could not undo. The level changed after this change') && await page.locator('dialog[open] section[aria-labelledby="settings-codex"]').count() === 0)
      await toast.getByRole('button', { name: 'Dismiss' }).click()
      await page.getByPlaceholder(/Search settings/).fill('level')
      await page.locator('input[name="codex-level-global"][value="2"]:checked').waitFor({ state: 'attached' })
      await codexPick(1)
      await toast.getByText('Codex level 2 → 1').waitFor()
      await page.getByPlaceholder(/Search settings/).fill('activity log')
      await section.locator('.audit-row').first().waitFor()
      await toast.getByRole('button', { name: 'Undo' }).click()
      await toast.getByText('Undid: Codex level 1 → 2').waitFor()
      check(`${at} away from the setting, a good Undo says Undid`, level === 2)
      // An autonomy Undo that the server refuses for another reason: the toast says that reason.
      await page.getByPlaceholder(/Search settings/).fill('level')
      await page.locator('input[name="autonomy-global"][value="3"]:checked').waitFor({ state: 'attached' })
      await page.locator('label', { has: page.locator('input[name="autonomy-global"][value="2"]') }).click()
      await toast.getByText('Autonomy level 3 → 2').waitFor()
      heldUndo = hold(request => isUndo('autonomy', request))
      await toast.getByRole('button', { name: 'Undo' }).click()
      await heldUndo.seen
      heldUndo.release({ status: 500, json: { error: 'Tether could not save the autonomy level.' } })
      await toast.getByRole('alert').waitFor()
      check(`${at} a failed autonomy Undo says why in its toast`, (await toast.getByRole('alert').innerText()).includes('Could not undo. Tether could not save the autonomy level.'))
      await toast.getByRole('button', { name: 'Dismiss' }).click()

      // The Project sheet: a project Codex level from "global" offers Undo back to global (override off).
      projectCodex = { override: false, level: 3 }
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await page.getByRole('button', { name: /^Session, .* and keys$/ }).first().click()
      await page.getByRole('button', { name: 'Session options' }).click()
      await page.getByRole('button', { name: 'Project…' }).click()
      const sheet = page.locator('dialog.project-sheet')
      await sheet.locator('section[aria-labelledby="project-codex"] input[role="switch"]').waitFor()
      const projectSwitch = sheet.locator('section[aria-labelledby="project-codex"] input[role="switch"]')
      const projectPick = value => sheet.locator('label', { has: page.locator(`input[name="codex-level-project"][value="${value}"]`) }).click()
      const projectToast = sheet.locator('.undo-toast')
      // Twice: the server still had the project on "global" (another device turned it off), and a plain project level.
      for (const [server, from, pick, old] of [[false, 'global', 1, { override: false, level: 3 }], [true, '3', 2, { override: true, level: 3 }]]) {
        await projectSwitch.check()
        await page.waitForFunction(() => !document.querySelector('dialog.project-sheet input[name="codex-level-project"][value="1"]')?.matches(':disabled'))
        projectCodex = { override: server, level: 3 }
        await projectPick(pick)
        await projectToast.getByText(`Project Codex level ${from} → ${pick}`).waitFor()
        check(`${at} the project toast is in the Project sheet and on screen (${from})`, await projectToast.evaluate(el => { const r = el.getBoundingClientRect(); const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!el.closest('dialog.project-sheet') && !!hit && el.contains(hit) }))
        if (!server) await shot(`project-toast-${width}`, projectToast)
        await projectToast.getByRole('button', { name: 'Undo' }).click()
        await projectToast.getByText(`Undid: project Codex level ${pick} → ${from}`).waitFor()
        const projectUndo = posts[posts.length - 1].body
        check(`${at} the project Undo sends back ${from}`, projectUndo.project === 'a1b2c3d4e5f6' && projectUndo.override === old.override && projectUndo.level === old.level && projectUndo.undo === true && projectUndo.revision === `r${rev - 1}`
          && projectCodex.override === old.override && projectCodex.level === old.level, JSON.stringify(projectUndo))
        await projectToast.getByRole('button', { name: 'Dismiss' }).click()
        if (server) await projectSwitch.uncheck()
        await page.waitForFunction(() => document.querySelector('dialog.project-sheet input[name="codex-level-project"][value="1"]')?.matches(':disabled'))
      }
      level = 3; rev++
    }
    // WCAG 2.2.1: the toast that offers Undo has no time limit. A fake clock moves 60 s on; it runs last, so no other
    // step sees the fake clock. The "Undid" note goes after 8 s unless hovered or focused.
    await page.clock.install()
    await open(sizes[sizes.length - 1][0], 'codex level')
    await page.locator('label', { has: page.locator('input[name="codex-level-global"][value="1"]') }).click()
    const offer = page.locator('dialog[open] .undo-toast')
    await offer.getByRole('button', { name: 'Undo' }).waitFor()
    await page.clock.fastForward(60000)
    check('the Undo toast stays after 60 s', await offer.getByRole('button', { name: 'Undo' }).isVisible())
    await offer.getByRole('button', { name: 'Undo' }).click()
    // The confirmation can open directly under the pointer left by the Undo click.
    await page.mouse.move(0, 0)
    await offer.getByText('Undid: Codex level 1 → 3').waitFor()
    await offer.hover()
    await settle()
    await page.clock.fastForward(9000)
    check('the Undid note stays while hovered', await offer.isVisible())
    await page.mouse.move(0, 0)
    await settle()
    await page.clock.fastForward(9000)
    await offer.waitFor({ state: 'detached' })
    check('the Undid note goes after 8 s', true)
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
    check('no writes or requests outside the fixtures', !unexpected.size, [...unexpected].join(', '))
    finished = true
  } finally {
    if (pageErrors.length) console.log(`page errors: ${pageErrors.join('; ')}`)
    if (page && (!finished || failed)) await page.screenshot({ path: path.join(SHOTS, 'last-failure.png') }).catch(() => {})
    if (page) await page.close().catch(() => {})
    if (browser && !process.env.TETHER_CDP_URL) await browser.close().catch(() => {})
    if (server) server.close()
  }
  console.log(failed ? `${failed} failed` : 'Activity log: states, filters, Load more, layout, contrast and the level Undo toast pass')
  console.log(`Screenshots: ${SHOTS}`)
  process.exit(failed ? 1 : 0)
}
main().catch(error => { console.error(error); process.exit(1) })
