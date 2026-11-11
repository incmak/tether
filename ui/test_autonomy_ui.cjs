// Autonomy settings and the action deck against an existing build. Every API request stays in this page's fixtures; no terminal is touched.
// Run: cd ui && npm run test:ui (or node test_agent_map.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-11/autonomy/shots')
const sizes = [[390, 844], [1280, 900]]
const now = Math.floor(Date.now() / 1000)
const ask = (pane, extra) => ({ pane, thread: null, since: now - 600, next: '', kind: 'reply', opened: false, snoozed_until: null, urgent: false, ...extra })
const paneA = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Deploy fix', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null,
  ask: ask('%1', { since: now - 120, urgent: true, next: 'The release script needs the signing key. Which key should I use: staging or production?' }) }
const paneB = { id: '%2', tmux_session: '$1', label: 'project:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/project', state: 'Idle', writable: true, thread: 't2', approval: null,
  ask: ask('%2', { since: now - 3600, kind: 'approval', next: 'Run npm install?' }) }
// A real permission prompt: the pane's state carries the request, so the deck can answer it in place.
const paneC = { id: '%3', tmux_session: '$1', label: 'project:0.2', pane_name: 'Build check', agent: 'claude', dir: '/srv/project', state: 'Approval', writable: true, thread: 't3', since: now - 900,
  needs: { kind: 'approval', text: 'Edit file · src/build.js' },
  approval: { id: 'permit-1', actionable: true, text: 'Edit file\nsrc/build.js\n\n 12 -const retries = 1\n 12 +const retries = 3\n 13  run(retries)',
    options: [{ id: 1, text: 'Yes' }, { id: 2, text: 'Yes, allow all edits during this session (shift+tab)' }, { id: 3, text: 'No, and tell Claude what to do differently (esc)' }] },
  ask: ask('%3', { since: now - 900, kind: 'approval', next: 'Edit src/build.js?' }) }
const levels = [['Check-in', 'Confirms the plan first.'], ['Guided', 'Confirms the plan, then works.'], ['Standard', 'Asks when a decision is material.'],
  ['Trusted', 'Uses sensible defaults.'], ['Autopilot', 'Does not ask about normal decisions.'], ['Full auto', 'Runs the task alone. Asks only when blocked; urgent asks reach the user at once.']]
  .map(([name, summary], level) => ({ level, name, summary }))
const auto = { agent: 'auto', model: 'auto', effort: 'auto' }
let autonomy = { levels, global: 2, level: 2, source: 'global', project: null, reviewers: { critical: auto, normal: auto, light: auto }, tiers: ['critical', 'normal', 'light'],
  models: { claude: [{ slug: 'opus', name: 'Opus' }, { slug: 'claude-fable-5-2', name: 'Fable 5.2' }], codex: [{ slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol', efforts: ['low', 'medium', 'high', 'xhigh'] }] },
  efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }
const fixture = {
  sessions: { sessions: [paneA, paneB, paneC], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

async function main() {
  let browser, page, server, failed = 0
  const posts = [], pageErrors = [], unexpected = new Set()
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
    page.on('pageerror', error => pageErrors.push(error.stack))
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') {
          const body = request.postDataJSON(); posts.push({ endpoint, body })
          if (endpoint === 'autonomy') { autonomy = { ...autonomy, ...(body.level !== undefined && !body.project ? { global: body.level, level: body.level } : {}), ...(body.reviewers ? { reviewers: body.reviewers } : {}) }; return route.fulfill({ json: autonomy }) }
          if (endpoint.startsWith('attention/')) return route.fulfill({ json: { ask: null, waiting: 1 } })
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint.startsWith('autonomy')) return route.fulfill({ json: autonomy })
        if (endpoint.startsWith('codex-level')) return route.fulfill({ json: { level: 0, global: 0, levels: [], project: null, models: [] } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (!(endpoint in fixture)) unexpected.add(endpoint)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      unexpected.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    for (const [width, height] of sizes) {
      const at = `${width}px`
      autonomy = { ...autonomy, global: 2, level: 2, reviewers: { critical: auto, normal: auto, light: auto } }
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      // The urgent ask opens the deck by itself.
      const deck = page.locator('dialog.action-deck')
      await deck.waitFor()
      check(`${at} deck opens for urgent ask`, true)
      check(`${at} urgent card first`, (await deck.locator('.deck-tab').innerText()).trim() === 'Deploy fix' && await deck.getByText('Urgent', { exact: true }).isVisible())
      check(`${at} count`, (await deck.locator('.deck-count').innerText()).trim() === '1 of 3')
      await page.waitForTimeout(100)
      check(`${at} reply box focused`, await deck.locator('textarea').evaluate(node => node === document.activeElement))
      check(`${at} no page overflow (deck)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `deck-urgent-${width}.png`) })
      // On a wide screen the list of every ask sits beside the card: urgent first, then oldest first. A phone keeps one card.
      const rows = deck.locator('.deck-row')
      if (width >= 900) {
        check(`${at} list order`, (await rows.locator('.deck-row-title').allInnerTexts()).join('|') === 'Deploy fix|Docs pass|Build check' && await rows.first().getAttribute('aria-current') === 'true')
        check(`${at} list row names kind and agent`, /Needs approval/.test(await rows.nth(2).innerText()) && /Claude/.test(await rows.nth(2).innerText()))
      } else check(`${at} no list on a phone`, !(await deck.locator('.deck-list').isVisible()))
      // Keys are off while you type: J and Y stay in the reply box.
      const before = posts.length
      await deck.locator('textarea').pressSequentially('jy')
      check(`${at} keys stay in the reply box`, await deck.locator('textarea').inputValue() === 'jy' && (await deck.locator('.deck-tab').innerText()).trim() === 'Deploy fix' && posts.length === before)
      await deck.locator('textarea').fill('Use staging.')
      await page.keyboard.press('Control+Enter')
      await deck.getByText('Run npm install?').waitFor()
      const sent = posts.slice(before).find(post => post.endpoint === 'message')
      check(`${at} Ctrl+Enter sends to the tab`, sent && sent.body.session === '%1' && sent.body.message === 'Use staging.', JSON.stringify(posts.slice(before)))
      check(`${at} next card: approval hint`, await deck.getByText('This prompt waits in the tab.').isVisible() && await deck.locator('textarea').count() === 0)
      check(`${at} no answer buttons without a live prompt`, await deck.locator('.inbox-actions').count() === 0)
      await page.keyboard.press('y')
      check(`${at} Y does nothing without a live prompt`, !posts.slice(before).some(post => post.endpoint === 'approval'))
      // The next card holds a real permission prompt: J (or its list row) shows it, with the full text and the answer buttons.
      if (width >= 900) await rows.nth(1).click(); else await page.keyboard.press('j')
      await deck.getByText('Edit src/build.js?').waitFor()
      const actions = deck.locator('.deck-card .inbox-actions button')
      check(`${at} approval buttons`, (await actions.allInnerTexts()).map(text => text.replace(/\s+/g, ' ').trim().replace(/ [YAN]$/, '')).join('|') === 'Yes|Yes, allow all edits during this session (shift+tab)|No', (await actions.allInnerTexts()).join('|'))
      // Key hints show on the buttons at desktop width, as on Home cards; a phone has no keyboard, so they hide.
      const hints = (await deck.locator('.deck-card .inbox-actions kbd:visible').allInnerTexts()).join('')
      check(`${at} key hints`, hints === (width >= 600 ? 'YAN' : '') && await deck.locator('.deck-actions kbd').isVisible() === (width >= 600), hints)
      check(`${at} full text with diff marks`, (await deck.locator('.deck-text .remove').innerText()).includes('retries = 1') && (await deck.locator('.deck-text .add').innerText()).includes('retries = 3') && (await deck.locator('.deck-text').innerText()).includes('src/build.js'))
      check(`${at} open tab, handled and skip stay`, await deck.getByRole('button', { name: 'Open tab' }).isVisible() && await deck.getByRole('button', { name: 'Handled' }).isVisible() && await deck.getByRole('button', { name: /Skip/ }).isVisible())
      check(`${at} no page overflow (approval card)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `deck-approval-${width}.png`) })
      await page.keyboard.press('Control+y')
      await page.waitForTimeout(450)
      check(`${at} Ctrl+Y sends nothing`, !posts.slice(before).some(post => post.endpoint === 'approval'))
      await page.keyboard.press('y')
      await deck.getByText('Yes sent to Build check.').waitFor()
      const approved = posts.slice(before).filter(post => post.endpoint === 'approval')
      check(`${at} Y sends one checked approval`, approved.length === 1 && approved[0].body.session === '%3' && approved[0].body.approval === 'permit-1' && approved[0].body.choice === 1 && approved[0].body.thread === 't3', JSON.stringify(approved))
      check(`${at} moves to the next card, no undo`, (await deck.locator('.deck-tab').innerText()).trim() === 'Docs pass' && await deck.getByText(/undo/i).count() === 0)
      await deck.getByRole('button', { name: 'Handled' }).click()
      await deck.getByText('Docs pass: handled.').waitFor()
      check(`${at} Handled posts`, posts.some(post => post.endpoint === 'attention/handled' && post.body.session === '%2'))
      await deck.getByRole('button', { name: 'Done' }).click()
      await deck.waitFor({ state: 'hidden' })
      check(`${at} Done closes`, true)
      // Settings: autonomy level and reviewers.
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      const search = page.getByPlaceholder(/Search settings/)
      if (await search.count()) await search.fill('autonomy')
      const level = page.getByRole('radio', { name: /Full auto/ }).first()
      await level.waitFor()
      check(`${at} six named levels`, await page.locator('fieldset.autonomy-levels').first().locator('.autonomy-level').count() === 6)
      check(`${at} level 2 checked`, await page.getByRole('radio', { name: /Standard/ }).first().isChecked())
      check(`${at} no page overflow (autonomy)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-autonomy-${width}.png`), fullPage: false })
      await level.check({ force: true })
      await page.waitForTimeout(300)
      check(`${at} level posts`, posts.some(post => post.endpoint === 'autonomy' && post.body.level === 5))
      if (await search.count()) await search.fill('reviewers')
      const agent = page.locator('#reviewer-critical-agent')
      await agent.waitFor()
      await agent.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `settings-reviewers-${width}.png`) })
      check(`${at} no page overflow (reviewers)`, !(await overflow()))
      await page.keyboard.press('Escape').catch(() => {})
    }
    check('no unmocked requests', !unexpected.size, [...unexpected].join(', '))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`autonomy ui: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
