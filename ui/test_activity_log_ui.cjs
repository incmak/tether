// Settings › Activity (ActivityLog.tsx) and the level Undo toast (LevelUndo.tsx), against an existing build. Every API
// request stays in this page's fixtures; no terminal and no real setting is touched.
// Run: cd ui && npm run test:ui (or node test_activity_log_ui.cjs after a build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-18/activity-log/shots')
const sizes = [[320, 640], [1280, 900]]
const now = Math.floor(Date.now() / 1000)
const GROUP = { setting: 'settings', passkey: 'settings', tab: 'tabs', review: 'reviews', backlog: 'backlog', lesson: 'lessons', advisor: 'advisor', approval: 'approvals' }
const id = (at, n) => `${String(Math.round(at * 1000)).padStart(16, '0')}-${n.toString(16).padStart(6, '0')}`
const ui = { source: 'ui', device: 'Chrome on Mac' }
const agent = { source: 'agent-cli', tab: 'Audit Log with a very long tab name that has to wrap on a phone', pane: '%12' }
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
    && (!params.get('actor') || Object.values(entry.actor).some(value => value.toLowerCase().includes(params.get('actor').toLowerCase())))
    && (since === null || entry.at >= since) && (until === null || entry.at < until)
    && (!before || entry.id < before)
  const found = all.filter(match).sort((a, b) => b.id.localeCompare(a.id)).slice(0, limit + 1)
  const page = found.slice(0, limit)
  const result = { entries: page, next: found.length > limit ? page[page.length - 1].id : null, retention_days: 90 }
  if (params.get('facets') === '1') result.facets = {
    kinds: [], projects: [...new Set(all.map(entry => entry.project).filter(Boolean))].sort(),
    actors: [...new Set(all.map(entry => entry.actor.tab || entry.actor.device).filter(Boolean))].sort(),
  }
  return result
}
const levels = [0, 1, 2, 3, 4, 5].map(level => ({ level, name: `Level ${level}`, summary: `What level ${level} does.` }))
const codex = global => ({ levels, global, level: global, source: 'global', project: null, runs: { today: 0, running: 0, panes: [] }, run: { model: 'gpt-6.2-sol', effort: 'high', fast: true }, models: [] })
const pane = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Activity test', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const fixture = {
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  sessions: { sessions: [pane], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] }, subagents: { main: null, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'push/config': { enabled: false },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], unexpected = new Set(), asks = [], posts = []
  let mode = 'rows'  // rows | empty | error | slow
  let all = entries(), level = 3
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
        if (request.method() !== 'GET' && endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (request.method() === 'POST' && endpoint === 'ui-usage') return route.fulfill({ json: { gen: '' } })
        if (endpoint === 'audit') {
          if (request.method() !== 'GET') { unexpected.add(`${request.method()} audit`); return route.fulfill({ status: 405, json: { error: 'read only' } }) }
          asks.push(url.searchParams)
          if (mode === 'error') return route.fulfill({ status: 500, json: { error: 'Tether could not read the audit log.' } })
          if (mode === 'slow') await new Promise(resolve => setTimeout(resolve, 1500))
          return route.fulfill({ json: query(mode === 'empty' ? [] : all, url.searchParams) })
        }
        if (endpoint === 'codex-level') {
          if (request.method() === 'POST') {
            const body = request.postDataJSON()
            posts.push({ body, client: request.headers()['x-tether-client'] })
            // Like server/codex_level.py with server/audit.py: one record per change, "Undo: " for the toast's undo.
            if (typeof body.level === 'number' && body.level !== level) {
              const at = Date.now() / 1000
              all = [{ id: id(at, 900 + posts.length), at, kind: 'setting.codex_level', summary: `${body.undo ? 'Undo: ' : ''}Codex level ${level} → ${body.level}`, old: level, new: body.level, undo: body.undo || undefined, actor: ui }, ...all]
              level = body.level
            }
          }
          return route.fulfill({ json: codex(level) })
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
      all = entries(); level = 3; posts.length = 0

      // Loading, then rows: newest first, the actor in words, old → new, the Undo mark.
      mode = 'slow'
      await open(width)
      await section.waitFor()
      check(`${at} loading skeleton announces itself`, (await section.getByText('Loading the activity log').count()) > 0)
      mode = 'rows'
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
      check(`${at} an agent row names the tab and the project`, /Audit Log with a very long tab name[\s\S]*Tether/.test(await rows.nth(4).innerText()))
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
      check(`${at} the end says how long Tether keeps it`, await section.getByText('That is all. Tether keeps 90 days.').isVisible())

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
      // Date range: From today sends since = the start of today, local time.
      await section.locator('.audit-dates input').first().fill(new Date().toLocaleDateString('en-CA'))
      await page.waitForFunction(() => document.querySelectorAll('section.activity-log .audit-row').length === 5)
      const midnight = new Date(); midnight.setHours(0, 0, 0, 0)
      check(`${at} From sends the start of the local day`, asks[asks.length - 1].get('since') === String(midnight.getTime() / 1000))
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
      check(`${at} Undo sends the old level with undo: true`, posts[1]?.body.level === 3 && posts[1].body.undo === true && posts[1].client === 'web', JSON.stringify(posts[1]))
      check(`${at} the level is back to 3`, await page.locator('input[name="codex-level-global"][value="3"]').isChecked())
      check(`${at} the done toast has no second Undo`, await toast.getByRole('button', { name: 'Undo' }).count() === 0)
      // The log shows both records, the undo marked.
      await page.getByPlaceholder(/Search settings/).fill('activity log')
      await section.locator('.audit-row').first().waitFor()
      const top = await rows.evaluateAll(els => els.slice(0, 2).map(el => el.innerText))
      check(`${at} the log shows the change and its undo`, /Undo: Codex level 1 → 3[\s\S]*Chrome on Mac/.test(top[0]) && /Codex level 3 → 1/.test(top[1]), top.join(' | '))
    }
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
    check('no writes or requests outside the fixtures', !unexpected.size, [...unexpected].join(', '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser && !process.env.TETHER_CDP_URL) await browser.close().catch(() => {})
    if (server) server.close()
  }
  console.log(failed ? `${failed} failed` : 'Activity log: states, filters, Load more, layout, contrast and the level Undo toast pass')
  console.log(`Screenshots: ${SHOTS}`)
  process.exit(failed ? 1 : 0)
}
main().catch(error => { console.error(error); process.exit(1) })
