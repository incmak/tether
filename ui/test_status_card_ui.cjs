// The coordinator's "[Tether status]" prompt as a status card (StatusCard.tsx), and the other prompts Tether or Claude Code
// type into a tab (coordinator rules, an interrupted turn), against an existing build. Every API request stays in this
// page's fixtures; no terminal is touched. Run: cd ui && npm run test:ui (or node test_status_card_ui.cjs after a build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('status-card')
const sizes = [[320, 720], [1280, 900]]
const CLI = 'python3 /opt/tether/server/agent_coordinator_cli.py'
const head = (count, mode = 'all') => `[Tether status] ${count} tab update${count === 1 ? '' : 's'}. Tell the user what changed and what needs them, in one or two lines per tab. `
  + `Reports are set to "${mode}". If you do not have the coordinator rules, run \`${CLI} claim\`.`
const LONG = 'B12 is done: the commit a1b2c3d is on main and deployed. The settings page now loads in 300 ms on a phone over 3G, and the review approved the change with no open findings.'
const status = [head(4),
  '- `B12 settings speed with a long tab name` (%7) finished: ' + LONG,
  '- `B13 login` (%8) failed: the server tests failed at test_auth.py:42.',
  '- `B14 export` (%9) waits on the user: question: Keep CSV or move to JSON?',
  '- `Gone tab` (%55) finished and asks the user: Should I deploy?'].join('\n')
const rules = '[Tether] The user made this tab the coordinator of this session in Tether.\nYou are the coordinator of this Tether session.'
const quoted = 'Why did you write "[Tether status] 1 tab update." in the middle of this?'
const now = new Date().toISOString()
const messages = [
  { id: 'u1', role: 'user', text: rules, time: now },
  { id: 'a1', role: 'assistant', text: 'I am the coordinator now.', time: now },
  { id: 's1', role: 'user', text: status, time: now },
  { id: 'a2', role: 'assistant', text: 'B14 needs you.', time: now },
  { id: 'i1', role: 'user', text: '[Request interrupted by user]', time: now },
  { id: 'q1', role: 'user', text: quoted, time: now },
  // One tab fewer than the header says: the parser refuses, so the text stays a bubble.
  { id: 's2', role: 'user', text: [head(2), '- `B15 docs` (%10) finished'].join('\n'), time: now },
]
const pane = (id, name) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't' + id, approval: null })
// %9 is now another tab: tmux reused the id. The name check keeps the link off it.
const panes = [pane('%1', 'Coordinator'), pane('%7', 'B12 settings speed with a long tab name'), pane('%8', 'B13 login'), pane('%9', 'Something else')]
const fixture = {
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  sessions: { sessions: panes, default_thread: 't%1' },
  chat: { messages, busy: false, thread: 't%1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
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
  const pageErrors = [], writes = []
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
        if (request.method() !== 'GET') { writes.push(`${request.method()} ${endpoint}`); return route.fulfill({ json: {} }) }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false }))
    })
    // The chat also keeps an inert copy (it is not shown to anyone): look only at the live one.
    const live = selector => page.locator(`${selector}:not([inert] *)`)
    const card = live('.status-card[data-msg-id="s1"]')
    const open = async () => {
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await card.waitFor()
    }
    // WCAG 2.2 1.4.3: text against the background it sits on (every ancestor layer composited).
    const contrast = selector => page.evaluate(selector => {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      const paint = layers => {
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1, 1)
        for (const color of layers) { ctx.fillStyle = '#fff'; ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1) }
        return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3)
      }
      const lum = rgb => { const c = rgb.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] }
      return [...document.querySelectorAll(selector)].filter(el => el.getClientRects().length && !el.closest('[inert]')).map(el => {
        const layers = []
        for (let at = el; at; at = at.parentElement) layers.unshift(getComputedStyle(at).backgroundColor)
        const back = paint(layers), text = paint([...layers, getComputedStyle(el).color])
        const [hi, lo] = [lum(back), lum(text)].sort((a, b) => b - a)
        return { text: el.textContent.trim().slice(0, 24), ratio: Math.round((hi + 0.05) / (lo + 0.05) * 100) / 100 }
      })
    }, selector)
    const TEXT = '.status-title span, .status-title time, .status-name, .status-state, .status-text, .notice-line span'
    const low = list => list.filter(item => item.ratio < 4.5).map(item => `${item.text} ${item.ratio}`).join('; ')

    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      await open()

      // The card: a header with the count, then one row per tab, what needs the person first. No pane id, no boilerplate.
      check(`${at} header names the count`, (await card.locator('.status-title').innerText()).includes('Tether status · 4 tabs'))
      const rows = card.locator('.status-row')
      const order = await rows.evaluateAll(list => list.map(row => row.className.replace('status-row ', '')))
      check(`${at} waits-on-you first, then asks, failed, finished`, order.join(',') === 'needs,asks,failed,done', order.join(','))
      const words = await card.locator('.status-state').allInnerTexts()
      check(`${at} state words`, words.join(',') === 'Waits on you,Asks you,Failed,Finished', words.join(','))
      const shown = await card.innerText()
      check(`${at} no pane ids or boilerplate`, !/%\d|Tell the user|coordinator rules|agent_coordinator_cli/.test(shown), shown)
      check(`${at} the waiting row has the attention wash`, await rows.first().evaluate(row => getComputedStyle(row).backgroundColor !== 'rgba(0, 0, 0, 0)'))
      check(`${at} the waiting row shows its question`, (await rows.first().innerText()).includes('Keep CSV or move to JSON?'))

      // Links: a live tab with the same name opens; a reused id with another name, and a gone tab, stay plain text.
      check(`${at} B13 is a link`, await card.getByRole('button', { name: 'Open the tab B13 login' }).count() === 1)
      check(`${at} a reused pane id is not a link`, await card.getByRole('button', { name: 'Open the tab B14 export' }).count() === 0)
      check(`${at} a gone tab is not a link`, await card.getByRole('button', { name: 'Open the tab Gone tab' }).count() === 0)

      // Expand: the long summary is cut to one line; its button shows the rest and says so.
      const done = rows.nth(3)
      const more = done.getByRole('button', { name: /summary/ })
      check(`${at} a cut summary has an expand button`, await more.count() === 1)
      check(`${at} the expand button starts collapsed`, await more.getAttribute('aria-expanded') === 'false')
      const tall = await done.boundingBox()
      if (width === 320) {
        const box = await more.boundingBox()
        check(`${at} the expand button is at least 24 px`, box.width >= 24 && box.height >= 24, JSON.stringify(box))
      }
      await card.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `card-${width}.png`) })
      await more.click()
      check(`${at} expand shows the whole summary`, await more.getAttribute('aria-expanded') === 'true' && (await done.boundingBox()).height > tall.height)
      check(`${at} the expanded text is the region it controls`, await live(`[id="${await more.getAttribute('aria-controls')}"]`).innerText() === LONG)
      await page.screenshot({ path: path.join(SHOTS, `card-${width}-open.png`) })
      await more.click()
      check(`${at} collapse again`, await more.getAttribute('aria-expanded') === 'false')

      // The other prompts: rules fold into one row, an interrupted turn is a quiet line, a quote stays the person's bubble.
      check(`${at} rules are a folded row`, (await live('[data-msg-id="u1"]').evaluate(node => node.tagName + ' ' + node.textContent)).startsWith('DETAILS Coordinator rules'))
      check(`${at} interrupted is a notice line`, (await live('[data-msg-id="i1"]').innerText()).trim() === 'You stopped the turn')
      check(`${at} a quote of the header stays a bubble`, await live('[data-msg-id="q1"]').evaluate(node => node.classList.contains('user-message')))
      check(`${at} a status with the wrong count stays a bubble`, await live('[data-msg-id="s2"]').evaluate(node => node.classList.contains('user-message')))

      check(`${at} no sideways scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
        const bad = low(await contrast(TEXT))
        check(`${at} ${theme} text contrast at least 4.5:1`, !bad, bad)
        await card.scrollIntoViewIfNeeded()
        await page.screenshot({ path: path.join(SHOTS, `card-${width}-${theme}.png`) })
      }
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })

      // The link opens that tab.
      await card.getByRole('button', { name: 'Open the tab B13 login' }).click()
      await page.locator('section.chat-panel[data-pane="%8"]').waitFor()
      check(`${at} the link opens the tab`, true)
    }
    check('no page errors', pageErrors.length === 0, pageErrors.join('; '))
    check('no writes beyond prefs', writes.every(item => /prefs|ui-usage|seen|read/.test(item)), writes.join(', '))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack || error}`)
  } finally {
    await page?.close().catch(() => {})
    await browser?.close().catch(() => {})
    server?.close()
  }
  console.log(failed ? `${failed} failed` : 'all ok')
  process.exitCode = failed ? 1 : 0
}
main()
