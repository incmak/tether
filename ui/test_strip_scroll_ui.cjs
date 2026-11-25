// Strip scroll (stripScroll.ts): real wheel events (CDP) over a chat's stack row or tab strip, or 10 px above them, scroll
// that strip and never switch tabs. The title row cancels the swipe (no Back/Forward). The chat, also just below the tab
// strip, has no listener and keeps B81.
// With the setting off and on the phone, nothing changes. Every API request stays in this page's fixtures; no terminal
// is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('header-scroll')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
// Many tabs with long names in ten stacks: both strips overflow at 1280 px. %1 to %12 are in "Ship"; the rest spread.
const names = ['Deploy fix', 'Docs pass for the release notes', 'Review the parser change', 'Automation tether', 'BriskBot daily', 'Voice worker on CPU', 'Backlog detail pane', 'Header strips scroll', 'Sidebar fuzzy search', 'Release notes draft', 'Usage limits page', 'Agent map layout']
const panes = names.map((name, i) => pane(`%${i + 1}`, name, { group: 'g0' }))
const stacks = ['Ship', 'Automation tether', 'BriskBot website', 'Voice and dictation', 'Backlog and planning', 'Release work', 'Design system', 'Research notes', 'Ops and deploys', 'Customer follow-ups']
const many = {
  sessions: [...panes, ...stacks.slice(1).map((name, i) => pane(`%${i + 20}`, `${name} tab`, { group: `g${i + 1}` })), pane('%40', 'Loose end')],
  default_thread: 't1', groups: { $1: stacks.map((name, i) => ({ id: `g${i}`, name, project: '' })) },
}
const few = { sessions: [pane('%1', 'Deploy fix'), pane('%2', 'Docs pass'), pane('%3', 'Review', { agent: 'codex' })], default_thread: 't1' }
let sessions = many
const fixture = {
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
        if (endpoint === 'sessions') return route.fulfill({ json: sessions })
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
      // Bubble phase on window: runs after the panel's listener, so defaultPrevented shows its decision.
      window.__wheels = []
      addEventListener('wheel', event => window.__wheels.push(event.defaultPrevented), { passive: true })
    })
    const selected = () => page.locator('[role="tab"][aria-selected="true"]').first().getAttribute('data-pane')
    const current = () => page.locator('.group-tab[aria-current="true"] .group-name').first().textContent()
    const finger = [2, 6, 14, 26, 38, 44, 40, 30, 26, 22, 18, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1]
    const total = finger.reduce((sum, dx) => sum + dx, 0)
    // One swipe: the finger part and its momentum, then a pause so the next swipe is a new gesture.
    const swipe = async (sign, at, dy = 0) => {
      await page.mouse.move(at.x, at.y)
      await page.evaluate(() => { window.__wheels = [] })
      for (const dx of finger) await page.mouse.wheel(sign * dx, dy)
      await page.waitForTimeout(260)
      return page.evaluate(() => window.__wheels)
    }
    const box = selector => page.locator(selector).first().boundingBox()
    const scrolled = selector => page.locator(selector).first().evaluate(element => element.scrollLeft)
    const setScroll = (selector, left) => page.locator(selector).first().evaluate((element, value) => { element.scrollLeft = value === 'end' ? element.scrollWidth : value }, left)
    const open = async (width, query = '') => {
      await page.setViewportSize({ width, height: 800 })
      await page.goto(`${origin}/chat/?session=%252${query}`, { waitUntil: 'load' })
      await page.locator('[role="tab"][data-pane="%2"][aria-selected="true"]').waitFor()
    }
    const TABS = '.chat-panel .pane-tabs', ROW = '.chat-panel .group-row'
    const all = list => list.length === finger.length && list.every(Boolean)
    const none = list => list.length > 0 && list.every(value => !value)

    await open(1280)
    await page.locator(ROW).waitFor()
    check('desktop layout', await page.evaluate(() => matchMedia('(min-width: 900px) and (hover: hover) and (pointer: fine)').matches))
    check('both strips overflow at 1280 px', await page.evaluate(sel => sel.every(s => { const e = document.querySelector(s); return e.scrollWidth > e.clientWidth + 100 }), [TABS, ROW]))
    check('the strips stop at their ends (overscroll-behavior-x: contain)', await page.evaluate(sel => sel.every(s => getComputedStyle(document.querySelector(s)).overscrollBehaviorX === 'contain'), [TABS, ROW]))
    let tabs = await box(TABS), row = await box(ROW)
    const x = 640
    // 1. Over the tab strip: it scrolls natively; no tab switch.
    let wheels = await swipe(1, { x, y: tabs.y + tabs.height / 2 })
    let left = await scrolled(TABS)
    check('tab strip: a swipe left scrolls it', left > 100, String(left))
    check('tab strip: no tab switch, events go to the native scroll', await selected() === '%2' && none(wheels), `${await selected()} ${JSON.stringify(wheels)}`)
    await page.screenshot({ path: path.join(SHOTS, 'strip-scroll-tabs-1280.png') })
    wheels = await swipe(-1, { x, y: tabs.y + tabs.height / 2 })
    check('tab strip: a swipe right scrolls it back', await scrolled(TABS) < left && await selected() === '%2', String(await scrolled(TABS)))
    // 2. Over the stack row: it scrolls; the stack and tab stay.
    const stack = await current()
    wheels = await swipe(1, { x, y: row.y + row.height / 2 })
    check('stack row: a swipe left scrolls it, no stack or tab switch', await scrolled(ROW) > 100 && await current() === stack && await selected() === '%2', `${await scrolled(ROW)} ${await current()}`)
    await page.screenshot({ path: path.join(SHOTS, 'strip-scroll-stacks-1280.png') })
    // 3. The 10 px band above the stack row (over the title row) scrolls the stack row. Below the tab strip lies the chat:
    // it has no listener (its scroll never waits), so a swipe there passes (B81).
    await setScroll(TABS, 0); await setScroll(ROW, 0)
    wheels = await swipe(1, { x, y: row.y - 8 })
    check('band above the stack row: scrolls the stack row 1:1, cancelled (no Back)', Math.abs(await scrolled(ROW) - total) <= 1 && all(wheels) && await scrolled(TABS) === 0 && await selected() === '%2', `${await scrolled(ROW)} ${JSON.stringify(wheels)}`)
    wheels = await swipe(1, { x, y: tabs.y + tabs.height + 4 })
    check('just below the tab strip: the chat, not cancelled (B81), no strip scroll', none(wheels) && await scrolled(TABS) === 0, JSON.stringify(wheels))
    check('the chat has no wheel listener of the header swipe', await page.evaluate(() => {
      const scroll = document.querySelector('.chat-scroll'), event = new WheelEvent('wheel', { deltaX: 40, bubbles: true, cancelable: true })
      scroll.dispatchEvent(event)
      return !event.defaultPrevented
    }))
    // Without stacks the band above the tab strip lies over the title row.
    sessions = few
    await open(1280)
    await page.evaluate(s => { const e = document.querySelector(s); for (let i = 0; i < 12; i++) e.append(e.querySelector('[role="tab"]').cloneNode(true)) }, TABS)
    tabs = await box(TABS)
    wheels = await swipe(1, { x, y: tabs.y - 6 })
    check('no stacks, band above the tab strip: scrolls the tab strip, cancelled', await scrolled(TABS) > 100 && all(wheels), `${await scrolled(TABS)} ${JSON.stringify(wheels)}`)
    sessions = many
    await open(1280)
    tabs = await box(TABS); row = await box(ROW)
    // 4. At the strip's end: the swipe stops there, cancelled.
    await setScroll(TABS, 'end')
    const end = await scrolled(TABS)
    wheels = await swipe(1, { x, y: tabs.y + tabs.height / 2 })
    check('at the end of the tab strip: no Back (cancelled), no tab switch', all(wheels) && await scrolled(TABS) === end && await selected() === '%2', JSON.stringify(wheels))
    await setScroll(ROW, 0)
    wheels = await swipe(-1, { x, y: row.y + row.height / 2 })
    check('at the start of the stack row: no Forward (cancelled)', all(wheels) && await scrolled(ROW) === 0, JSON.stringify(wheels))
    // 5. The title row outside the zone: cancelled, nothing moves. The chat: B81.
    const title = await box('.chat-header .chat-title')
    wheels = await swipe(1, { x: title.x + title.width / 2, y: title.y + title.height / 2 })
    check('title row: no Back/Forward (cancelled), no tab switch', all(wheels) && await selected() === '%2', JSON.stringify(wheels))
    await page.screenshot({ path: path.join(SHOTS, 'strip-scroll-title-1280.png') })
    wheels = await swipe(1, await box('.chat-scroll').then(b => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 })))
    check('chat: the swipe passes to the browser (B81 Back/Forward)', none(wheels), JSON.stringify(wheels))
    // 6. A mouse wheel (vertical) over a strip scrolls it sideways and glides; no tab switch. Up scrolls back.
    const notches = async (dy, count, at) => {
      await page.mouse.move(at.x, at.y)
      await page.evaluate(() => { window.__wheels = [] })
      for (let i = 0; i < count; i++) await page.mouse.wheel(0, dy)
      await page.waitForTimeout(600)
      return page.evaluate(() => window.__wheels)
    }
    await setScroll(TABS, 0)
    wheels = await notches(100, 3, { x, y: tabs.y + tabs.height / 2 })
    check('mouse wheel down over the tab strip: scrolls it right, cancelled, no tab switch', await scrolled(TABS) >= 250 && wheels.every(Boolean) && await selected() === '%2', `${await scrolled(TABS)} ${JSON.stringify(wheels)}`)
    const right = await scrolled(TABS)
    await notches(-100, 1, { x, y: tabs.y + tabs.height / 2 })
    check('mouse wheel up over the tab strip: scrolls it back left', Math.abs(await scrolled(TABS) - (right - 100)) <= 2, `${right} -> ${await scrolled(TABS)}`)
    await setScroll(ROW, 0)
    await notches(100, 2, { x, y: row.y + row.height / 2 })
    check('mouse wheel over the stack row: scrolls it right', await scrolled(ROW) >= 150, String(await scrolled(ROW)))
    await setScroll(TABS, 0)
    wheels = await notches(100, 1, { x: title.x + title.width / 2, y: title.y + title.height / 2 })
    check('mouse wheel over the title row: passes, no strip scroll', wheels.length > 0 && wheels.every(v => !v) && await scrolled(TABS) === 0, JSON.stringify(wheels))
    // 7. A strip that does not overflow (three tabs, no stacks): a swipe does nothing, cancelled.
    sessions = few
    await open(1280)
    tabs = await box(TABS)
    check('few tabs: the strip does not overflow', await page.evaluate(s => { const e = document.querySelector(s); return e.scrollWidth <= e.clientWidth }, TABS))
    wheels = await swipe(1, { x, y: tabs.y + tabs.height / 2 })
    check('strip without overflow: no Back (cancelled), no tab switch', all(wheels) && await selected() === '%2', JSON.stringify(wheels))
    sessions = many
    // 8. The setting off: the header behaves like the rest of the page.
    await open(1280, '&off')
    tabs = await box(TABS)
    check('setting off: no overscroll contain on the strips', await page.evaluate(s => getComputedStyle(document.querySelector(s)).overscrollBehaviorX, TABS) === 'auto')
    await setScroll(TABS, 'end')
    wheels = await swipe(1, { x, y: tabs.y + tabs.height / 2 })
    check('setting off: at the strip end the swipe passes (Back/Forward)', none(wheels) && await selected() === '%2', JSON.stringify(wheels))
    wheels = await swipe(1, { x, y: tabs.y + tabs.height + 8 })
    check('setting off: the band passes too', none(wheels), JSON.stringify(wheels))
    // 9. The setting: on by default, a labelled switch in Settings › Sidebar & chat that works with the keyboard at 320 px.
    await open(1280)
    await page.locator('button[aria-label="Settings"]').first().click()
    await page.getByLabel('Search settings').fill('swipe')
    const toggle = page.getByRole('switch', { name: /Swipe on the tab bar scrolls it/ })
    check('settings: the switch is on by default', await toggle.isChecked())
    await page.setViewportSize({ width: 320, height: 700 })
    const size = await toggle.boundingBox()
    check('settings 320 px: the switch is visible, at least 24 px, no horizontal scroll', size && size.width >= 24 && size.height >= 24 && size.x + size.width <= 320 && !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), JSON.stringify(size))
    await page.screenshot({ path: path.join(SHOTS, 'settings-320.png') })
    await toggle.focus()
    await page.keyboard.press('Space')
    check('settings: Space turns it off and saves it on this device (same key: headerSwipe)', !(await toggle.isChecked()) && await page.evaluate(() => JSON.parse(localStorage.getItem('tmux-chat.prefs.v1')).headerSwipe === false))
    await page.keyboard.press('Escape')
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.locator('.chat-header .chat-title').first().waitFor()
    const t2 = await box('.chat-header .chat-title')
    wheels = await swipe(1, { x: t2.x + t2.width / 2, y: t2.y + t2.height / 2 })
    check('settings: after the switch is off, the title-row swipe passes through', none(wheels), JSON.stringify(wheels))
    // 10. Phone width: no change.
    await open(390)
    const t3 = await box('.chat-header .chat-title')
    wheels = await swipe(1, { x: t3.x + t3.width / 2, y: t3.y + t3.height / 2 })
    check('phone width: no change', await selected() === '%2' && none(wheels), JSON.stringify(wheels))
    // 11. B147: a press on the add button, or a gap of the strip, is not a tab drag. The chat panel
    // around the strip also has data-pane; a move after the press must not drag the whole panel.
    sessions = few
    await open(1280)
    const add = await box('.pane-tabs .tab-add')
    const panelMoved = () => page.evaluate(() => { const p = document.querySelector('.chat-panel'); return p.style.transform || getComputedStyle(p).transform !== 'none' ? p.style.transform || getComputedStyle(p).transform : '' })
    await page.mouse.move(add.x + add.width / 2, add.y + add.height / 2)
    await page.mouse.down()
    await page.mouse.move(add.x + add.width / 2 - 40, add.y + add.height / 2, { steps: 4 })
    const pressMove = await panelMoved()
    await page.mouse.up()
    check('press on + and move 40 px: the chat panel does not move', !pressMove && !(await page.locator('.pane-tabs .dragging').count()), pressMove)
    const strip = await box(TABS)
    const gap = { x: Math.min(add.x + add.width + 40, strip.x + strip.width - 4), y: add.y + add.height / 2 }
    const gapOk = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.matches('.pane-tabs'), gap)
    await page.mouse.move(gap.x, gap.y)
    await page.mouse.down()
    await page.mouse.move(gap.x - 60, gap.y, { steps: 4 })
    const gapMove = await panelMoved()
    await page.mouse.up()
    check('press on an empty part of the strip and move 60 px: the chat panel does not move', gapOk && !gapMove, `${gapOk} ${gapMove}`)
    await page.mouse.move(add.x + add.width / 2, add.y + add.height / 2)
    await page.mouse.down()
    await page.mouse.move(add.x + add.width / 2 - 10, add.y + add.height / 2, { steps: 2 })
    await page.mouse.move(add.x + add.width / 2, add.y + add.height / 2, { steps: 2 })
    await page.mouse.up()
    check('press on +, small move, release on +: the new-task popover opens', await page.locator('.pane-tabs .tab-add').getAttribute('aria-expanded') === 'true')
    await page.keyboard.press('Escape')
    sessions = many
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`strip scroll: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
