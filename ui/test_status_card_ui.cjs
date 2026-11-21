// The coordinator's "[Tether status]" prompt as a status card (StatusCard.tsx), and the other prompts Tether or Claude Code
// type into a tab (coordinator rules, an interrupted turn), against an existing build. Every API request stays in this
// page's fixtures; no terminal is touched. Run: cd ui && npm run test:ui (or node test_status_card_ui.cjs after a build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, shotsDir } = require('./browser_test_helper.cjs')
const { status, review } = require('../server/fixtures/injected_prompts/browser.json')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS && shotsDir('status-card')
const sizes = [[320, 720], [360, 800], [1280, 900]]
const [header, row] = status.split('\n')
const LONG = row.split(' finished: ')[1]
const pastedStatus = [header.replace('4 tab updates.', '1 tab update.'), '- `Sample guide` (%10) finished'].join('\n')
const rules = '[Tether] The user made this tab the coordinator of this session in Tether.\nYou are the coordinator of this Tether session.'
const quoted = 'Why did you write "[Tether status] 1 tab update." in the middle of this?'
const sharedPrompt = 'Try the sample filter'
const realPrompt = 'Clear the sample filter'
const now = new Date().toISOString()
const tabs = ['%7', '%8', '%9', '%55'].map(pane => ({ pane, session: '$1', thread: 't' + pane }))
const user = (id, text, meta) => ({ id, role: 'user', text, time: now, ...meta })
const messages = [
  user('u1', rules, { source: 'tether', prompt: 'rules' }),
  user('pasted-rules', rules),
  { id: 'a1', role: 'assistant', text: 'I am the coordinator now.', time: now },
  ...Object.entries(review).flatMap(([kind, text]) => [
    user('review-' + kind, text, { source: 'tether', prompt: 'review' }),
    user('pasted-' + kind, text),
  ]),
  user('short-tabs', pastedStatus, { source: 'tether', prompt: 'status', tabs: [tabs[1], tabs[2]] }),
  user('s2', pastedStatus.replace('1 tab update.', '2 tab updates.'), { source: 'tether', prompt: 'status' }),
  user('q1', quoted),
  user('pasted-status', pastedStatus),
  user('s1', status, { source: 'tether', prompt: 'status', tabs }),
  { id: 'a2', role: 'assistant', text: 'The export needs your answer.', time: now },
  user('i1', '[Request interrupted by user]'),
  user('legacy-status', pastedStatus, { legacy: true }),
  user('legacy-approved', review.approved, { legacy: true }),
  user('legacy-feedback', review.feedback, { legacy: true }),
  user('legacy-request', review.request, { legacy: true }),
  user('pill-pasted', sharedPrompt),
  { id: 'pill-spacer', role: 'assistant', text: 'The filter matched the sample.\n\nThe list has one item.' },
  user('pill-real', realPrompt),
  user('pill-marked', sharedPrompt, { source: 'tether', prompt: 'review' }),
  { id: 'pill-tail', role: 'assistant', text: Array.from({ length: 28 }, (_, n) => `Sample result ${n + 1}`).join('\n\n') },
]
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't' + id, approval: null, ...extra })
const panes = [
  pane('%1', 'Coordinator'),
  pane('%7', 'Settings speed with a long tab name'),
  pane('%18', 'Renamed after the report', { thread: 't%8' }),
  pane('%9', 'Sample export', { thread: 'different' }),
  pane('%55', 'Gone tab', { tmux_session: '$2' }),
]
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
    if (SHOTS) await fs.mkdir(SHOTS, { recursive: true })
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
        if (endpoint === 'search') {
          const text = url.searchParams.get('q')
          const matches = messages.flatMap((message, index) => message.text === text
            ? [{ id: message.id, role: message.role, source: message.source, snippet: message.text, from_end: messages.length - index }] : []).reverse()
          return route.fulfill({ json: { matches, total: matches.length, max_limit: 2000 } })
        }
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
    const message = id => live(`[data-msg-id="${id}"]`)
    const card = live('.status-card[data-msg-id="s1"]')
    const shot = name => SHOTS && page.screenshot({ path: path.join(SHOTS, name) })
    const open = async () => {
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await card.waitFor()
    }
    const showMineBarAt = async id => {
      const scroll = live('section.chat-panel[data-pane="%1"] .chat-scroll')
      await scroll.evaluate(async (node, id) => {
        node.scrollTop = node.scrollHeight
        node.dispatchEvent(new Event('scroll'))
        await new Promise(requestAnimationFrame)
        const target = node.querySelector(`[data-msg-id="${id}"]`)
        node.scrollTop += target.getBoundingClientRect().top - node.getBoundingClientRect().top - 28
        node.dispatchEvent(new Event('scroll'))
        await new Promise(requestAnimationFrame)
      }, id)
      return live('.mine-bar.shown .mine-bar-text').innerText()
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
    const TEXT = '.status-summary span, .status-toggle time, .status-when, .status-name, .status-state, .status-text, .notice-line span'
    const low = list => list.filter(item => item.ratio < 4.5).map(item => `${item.text} ${item.ratio}`).join('; ')

    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      await open()

      // Folded first: one line with the count and what waits on the person, in the attention style; the rows are hidden.
      const toggle = card.locator('.status-toggle')
      const summary = await toggle.innerText()
      check(`${at} starts folded`, await toggle.getAttribute('aria-expanded') === 'false' && !(await card.locator('ul').isVisible()))
      check(`${at} folded line names the count`, (/Tether status/.test(summary) ? summary.includes('Tether status · 4 tab updates') : summary.includes('4 updates')), summary)
      check(`${at} folded line says what waits`, (/Tether status/.test(summary) ? /1\s*waits on you/ : /1\s*waits/).test(summary), summary)
      check(`${at} folded line has the attention style`, await card.evaluate(node => node.classList.contains('has-needs'))
        && await toggle.evaluate(node => getComputedStyle(node).backgroundColor !== 'rgba(0, 0, 0, 0)'))
      // One line at every width: the summary is a single row of text, and nothing in it is cut off.
      const line = await card.locator('.status-summary').evaluate(node => ({
        height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight) || 13 * 1.6,
        cut: node.scrollWidth > node.clientWidth + 1 }))
      check(`${at} folded summary is one line`, line.height < line.line * 1.5 && !line.cut, JSON.stringify(line))
      check(`${at} the button name keeps the full words`, /4 tab updates, 1 waits on you, 1 asks you, 1 failed/.test(await toggle.getAttribute('aria-label')))
      const shortCard = message('short-tabs')
      if (await shortCard.locator('.status-toggle').count()) await shortCard.locator('.status-toggle').click()
      await card.scrollIntoViewIfNeeded()
      await shot(`card-${width}-folded.png`)
      await toggle.click()
      check(`${at} tap opens the rows`, await toggle.getAttribute('aria-expanded') === 'true' && await card.locator('ul').isVisible())
      check(`${at} the toggle controls the list`, await card.locator('.status-rows').getAttribute('id') === await toggle.getAttribute('aria-controls'))
      const rows = card.locator('.status-row')
      const order = await rows.evaluateAll(list => list.map(row => row.className.replace('status-row ', '')))
      check(`${at} waits-on-you first, then asks, failed, finished`, order.join(',') === 'needs,asks,failed,done', order.join(','))
      const words = await card.locator('.status-state').allInnerTexts()
      check(`${at} state words`, words.join(',') === 'Waits on you,Asks you,Failed,Finished', words.join(','))
      const shown = await card.innerText()
      check(`${at} no pane ids or boilerplate`, !/%\d|Tell the user|coordinator rules|agent_coordinator_cli/.test(shown), shown)
      check(`${at} the waiting row has the attention wash`, await rows.first().evaluate(row => getComputedStyle(row).backgroundColor !== 'rgba(0, 0, 0, 0)'))
      check(`${at} the waiting row shows its question`, (await rows.first().innerText()).includes('Keep CSV or move to JSON?'))

      check(`${at} the same chat remains a link after a new pane id and name`, await card.getByRole('button', { name: 'Open the tab Sample sign-in' }).count() === 1)
      check(`${at} the old pane id and name with a different thread display plain text`, await card.getByRole('button', { name: 'Open the tab Sample export' }).count() === 0)
      check(`${at} the same name and thread in another session display plain text`, await card.getByRole('button', { name: 'Open the tab Gone tab' }).count() === 0)
      check(`${at} a list with the wrong row count has no links`, await message('short-tabs').getByRole('button', { name: /Open the tab/ }).count() === 0)

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
      await shot(`card-${width}.png`)
      await more.click()
      check(`${at} expand shows the whole summary`, await more.getAttribute('aria-expanded') === 'true' && (await done.boundingBox()).height > tall.height)
      check(`${at} the expanded text is the region it controls`, await live(`[id="${await more.getAttribute('aria-controls')}"]`).innerText() === LONG)
      await shot(`card-${width}-open.png`)
      await more.click()
      check(`${at} collapse again`, await more.getAttribute('aria-expanded') === 'false')

      // The other prompts: rules fold into one row, an interrupted turn is a quiet line, a quote stays the person's bubble.
      check(`${at} rules are a folded row`, (await live('[data-msg-id="u1"]').evaluate(node => node.tagName + ' ' + node.textContent)).startsWith('DETAILS Coordinator rules'))
      check(`${at} interrupted is a notice line`, (await live('[data-msg-id="i1"]').innerText()).trim() === 'You stopped the turn')
      check(`${at} a quote of the header stays a bubble`, await live('[data-msg-id="q1"]').evaluate(node => node.classList.contains('user-message')))
      check(`${at} a status with the wrong count stays a bubble`, await live('[data-msg-id="s2"]').evaluate(node => node.classList.contains('user-message')))
      for (const kind of Object.keys(review)) {
        check(`${at} marked ${kind} is a review card`, await message('review-' + kind).evaluate(node => node.classList.contains('review-message')))
        check(`${at} pasted ${kind} stays a bubble`, await message('pasted-' + kind).evaluate(node => node.classList.contains('user-message')))
      }
      check(`${at} pasted rules stay a bubble`, await message('pasted-rules').evaluate(node => node.classList.contains('user-message')))
      check(`${at} pasted status stays a bubble`, await message('pasted-status').evaluate(node => node.classList.contains('user-message')))
      check(`${at} legacy review request is a card`, await message('legacy-request').evaluate(node => node.classList.contains('review-message')))
      check(`${at} legacy review feedback is a card`, await message('legacy-feedback').evaluate(node => node.classList.contains('review-message')))
      check(`${at} legacy status stays a bubble`, await message('legacy-status').evaluate(node => node.classList.contains('user-message')))
      check(`${at} legacy approval stays a bubble`, await message('legacy-approved').evaluate(node => node.classList.contains('user-message')))

      check(`${at} no sideways scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
        const bad = low(await contrast(TEXT))
        check(`${at} ${theme} text contrast at least 4.5:1`, !bad, bad)
        await card.scrollIntoViewIfNeeded()
        await shot(`card-${width}-${theme}.png`)
      }
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })

      const composer = live('form.composer textarea')
      await composer.press('ArrowUp')
      check(`${at} Up skips the marked prompt`, await composer.inputValue() === realPrompt)
      await composer.press('ArrowUp')
      check(`${at} Up recalls the identical unmarked paste`, await composer.inputValue() === sharedPrompt)
      await composer.press('ArrowUp')
      check(`${at} Up recalls the legacy review request`, await composer.inputValue() === review.request)
      await composer.fill('')

      check(`${at} the You pill skips the marked prompt`, await showMineBarAt('pill-marked') === realPrompt)
      await shot(`you-pill-${width}.png`)
      check(`${at} the You pill shows the identical unmarked paste`, await showMineBarAt('pill-pasted') === sharedPrompt)
      check(`${at} the You pill keeps the legacy review card`, (await showMineBarAt('legacy-request')).startsWith('[Tether review] The Codex tab'))

      await live('button.search-toggle').click()
      await live('input[type="search"][aria-label="Words to find"]').fill(sharedPrompt)
      const searchLabels = live('.search-results small')
      await searchLabels.nth(1).waitFor()
      check(`${at} search labels the marked prompt Tether and the identical paste You`,
        (await searchLabels.allInnerTexts()).join(',') === 'Tether,You')
      await shot(`search-${width}.png`)
      await live('dialog.search-sheet button[aria-label="Close"]').click()

      // The link opens that tab.
      await card.getByRole('button', { name: 'Open the tab Sample sign-in' }).press('Enter')
      await live('section.chat-panel[data-pane="%18"]').waitFor()
      check(`${at} the link opens the tab`, true)
    }

    // Every width, not only the shot widths: the folded line never clips, in its long or its short form.
    await page.setViewportSize({ width: 320, height: 720 })
    await open()
    const clipped = []
    for (let width = 320; width <= 1600; width += 10) {
      await page.setViewportSize({ width, height: 900 })
      const fit = await card.locator('.status-summary').evaluate(node => ({
        cut: node.scrollWidth > node.clientWidth + 1,
        tall: node.getBoundingClientRect().height >= parseFloat(getComputedStyle(node).lineHeight) * 1.5 }))
      if (fit.cut || fit.tall) clipped.push(width)
    }
    check('the folded line fits from 320 to 1600 px', clipped.length === 0, clipped.join(', '))
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
