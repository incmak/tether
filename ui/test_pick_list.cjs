// Pickers and numbered menus are clickable: a click on a row picks it, the arrow keys move the focus between rows,
// and "Show more" rows scroll the list. No ↑/↓ buttons. Every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, prefsStore } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const PICKER = { id: 'picker-1', reason: 'picker', title: 'Rewind', subtitle: 'Restore the code and the chat to the point before…', text: 'Rewind',
  items: [{ text: 'Fix the login bug', selected: false }, { text: 'Add the tests', selected: true }, { text: '(current)', selected: false }], more_above: 4, more_below: 0 }
const MULTI = { ...PICKER, id: 'picker-2', title: 'Select tools',
  choice: { selection: 'multi', focus: 0, filter: null, tabs: null, items: [{ id: 1, text: 'Read', selected: true, checked: true }, { id: 2, text: 'Write', selected: false, checked: false }], more_above: 0, more_below: 0, hints: [] },
  items: [{ text: 'Read', selected: true }, { text: 'Write', selected: false }], more_above: 0 }
const MENU = { id: 'menu-1', reason: 'menu', menu_verified: 'a'.repeat(64), question: 'Select Model and Effort', text: 'Select Model and Effort',
  options: [{ id: 1, text: 'GPT-6.2-Sol (current)', detail: 'Latest workhorse model.', selected: true }, { id: 2, text: 'GPT-6.2-Astra', detail: 'Most capable model.', selected: false }, { id: 3, text: 'GPT-6-Luna', detail: 'Fast model.', selected: false }],
  choice: { selection: 'single', focus: 0, filter: null, tabs: null, more_above: 0, more_below: -1, hints: ['enter select', 'esc back'] } }
// A Claude numbered list with boxes: a click on a row ticks it (Space), and Done sends Enter.
const TICKS = { id: 'menu-2', reason: 'menu', menu_verified: 'b'.repeat(64), question: 'Choose features?', text: 'Choose features?',
  options: [{ id: 1, text: '☐ Alpha', detail: '', selected: true }, { id: 2, text: '☐ Beta', detail: '', selected: false }, { id: 3, text: '☐ Gamma', detail: '', selected: false }],
  choice: { selection: 'multi', focus: 0, filter: null, tabs: null, items: [{ id: 1, text: 'Alpha', selected: true, checked: true }, { id: 2, text: 'Beta', selected: false, checked: false }, { id: 3, text: 'Gamma', selected: false, checked: false }], more_above: 0, more_below: 0, hints: ['Space to toggle', 'Enter to confirm', 'Esc to cancel'] } }
const CASES = { picker: PICKER, multi: MULTI, menu: MENU, ticks: TICKS, unverified: { ...TICKS, menu_verified: false } }
// The list name each pick must carry: the picker title, or the question of a numbered menu.
const HEADINGS = { picker: 'Rewind', multi: 'Select tools', menu: 'Select Model and Effort', ticks: 'Choose features?' }

function fixtures(attention, agent) {
  const pane = { id: '%1', tmux_session: '$1', label: 'Pick test', agent, dir: '/srv/project', state: 'Idle', writable: true, thread: 'pick-thread', approval: null }
  return {
    sessions: { sessions: [pane], default_thread: pane.thread },
    chat: { messages: [{ id: 'm1', role: 'user', text: 'Start.', time: '2026-11-17T08:00:00Z' }], busy: false, thread: pane.thread, agent, writable: true, session: pane.id, earlier: 0, limit: 100, state: 'Idle', approval: null, attention },
    footer: { observed: true, model: agent, mode: 'default', notices: [] },
    'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
    update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
    catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {},
    'codex-runs': { pane: '%1', running: 0, runs: [] }, 'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
    'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
    'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
    'work/board': { revision: 1, projects: [], items: [], stages: [] },
    usage: { claude: null, codex: null }, subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  }
}

async function main() {
  let browser, page, server, passed = 0, failed = 0
  try {
    await fs.access(path.join(DIST, 'index.html'))
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (req.method !== 'GET' || url.pathname.endsWith('/sw.js') || !url.pathname.startsWith('/chat/') || !file.startsWith(DIST)) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' })
        res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    for (const state of Object.keys(CASES)) for (const width of [320, 1280]) {
      const name = `${state}-${width}`, errors = [], sent = [], pageErrors = []
      const fixture = fixtures(structuredClone(CASES[state]), state === 'menu' ? 'codex' : 'claude')
      if (page) await page.close().catch(() => {})
      page = await context.newPage()
      page.setDefaultTimeout(10000)
      page.on('pageerror', error => pageErrors.push(error.message))
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
      await page.routeWebSocket('**/*', socket => socket.close())
      const prefs = prefsStore()
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
        if (url.pathname.includes('/api/')) {
          const endpoint = url.pathname.split('/api/')[1]
          if (endpoint === 'prefs') return route.fulfill({ json: request.method() === 'GET' ? prefs.get() : prefs.post(request.postDataJSON()) })
          if (request.method() !== 'GET') { sent.push({ endpoint, ...request.postDataJSON() }); return route.fulfill({ json: { ok: true, accepted: true } }) }
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
          return route.fulfill({ json: fixture[endpoint] || {} })
        }
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
        return route.abort('blockedbyclient')
      })
      await page.addInitScript(() => {
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false, sidePins: [], look: { layout: 'classic', surface: 'flat', backdrop: 'ember' } }))
      })
      try {
        await page.setViewportSize({ width, height: 800 })
        await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
        await page.waitForSelector('.composer textarea')
        const dock = page.locator('.choice-dock').first()
        await dock.waitFor()
        const choiceWrites = () => sent.filter(item => ['pick', 'key', 'keys', 'menu'].includes(item.endpoint))
        const clickPick = async button => {
          const reply = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/mobile/api/pick')
          const [response] = await Promise.all([reply, button.click()])
          return response.request().postDataJSON()
        }
        const picks = () => sent.filter(item => item.endpoint === 'pick').map(item => !Array.isArray(item.labels) ? `bad labels ${JSON.stringify(item)}`
          : item.heading !== HEADINGS[state] ? `bad heading ${JSON.stringify(item.heading)}` : `${item.row}:${item.labels[item.row].replace('\n', '|')}:${item.then}:${item.labels.length}`)
        const noKeys = () => { if (sent.some(item => item.endpoint === 'key')) errors.push(`Done used the unchecked key route: ${JSON.stringify(sent)}`) }
        // One Tab stop per list; the arrow keys move the focus and the Tab stop with it, and send nothing.
        const roving = async rowSelector => {
          if (await dock.locator(`${rowSelector}[tabindex="0"]`).count() !== 1) errors.push('want one Tab stop in the list')
          await dock.locator(`${rowSelector}[tabindex="0"]`).focus()
          const start = await page.evaluate(() => document.activeElement?.textContent ?? '')
          await page.keyboard.press('ArrowDown')
          const now = await page.evaluate(() => ({ text: document.activeElement?.textContent ?? '', tab: document.activeElement?.getAttribute('tabindex') }))
          if (now.text === start) errors.push('ArrowDown did not move the focus')
          if (now.tab !== '0' || await dock.locator(`${rowSelector}[tabindex="0"]`).count() !== 1) errors.push('the Tab stop did not follow the focus')
          if (choiceWrites().length) errors.push(`moving the focus sent ${JSON.stringify(choiceWrites())}`)
        }
        // The agent moves its mark after the user moved the focus: still one Tab stop, on the row with the focus.
        const markMoves = async rowSelector => {
          const list = fixture.chat.attention.items ?? fixture.chat.attention.options
          const focused = await page.evaluate(() => document.activeElement?.getAttribute('data-row'))
          const old = list.findIndex(row => row.selected)
          const next = list.findIndex((_, at) => at !== old && `row-${at}` !== focused)
          if (next < 0) return
          list.forEach((row, at) => { row.selected = at === next })
          if (fixture.chat.attention.choice?.items) fixture.chat.attention.choice.items.forEach((row, at) => { row.selected = at === next })
          await page.waitForFunction(([selector, row]) => document.querySelector(`${selector}[data-row="${row}"]`)?.classList.contains('current'), [rowSelector, `row-${next}`], { timeout: 8000 })
          const stops = await dock.locator(`${rowSelector}[tabindex="0"]`).evaluateAll(nodes => nodes.map(node => node.getAttribute('data-row')))
          if (stops.length !== 1 || stops[0] !== focused) errors.push(`after a mark change want one Tab stop on ${focused}, got ${JSON.stringify(stops)}`)
        }
        if (await dock.getByRole('button', { name: /^(↑|↓|Up|Down)$/ }).count()) errors.push('arrow buttons show')
        if (state === 'picker' || state === 'multi') {
          const rows = dock.locator('.pick-list button[data-row]')
          await roving('.pick-list button[data-row]')
          await markMoves('.pick-list button[data-row]')
          if (state === 'picker') {
            await clickPick(rows.filter({ hasText: 'Fix the login bug' }))
            await clickPick(dock.getByRole('button', { name: /Show 4 more above/ }))
            if (picks().join() !== '0:Fix the login bug:Enter:3,0:Fix the login bug:Up:3') errors.push(`picks: ${picks().join()}`)
          } else {
            await clickPick(rows.filter({ hasText: 'Write' }))
            await clickPick(dock.getByRole('button', { name: /^Done/ }))
            if (picks().join() !== '1:Write:Space:2,0:Read:Enter:2') errors.push(`picks: ${picks().join()}`)
            noKeys()
          }
        } else if (state === 'unverified') {
          await dock.getByText('Answer this in the terminal.', { exact: true }).waitFor()
          const terminal = dock.getByRole('button', { name: 'Open terminal', exact: true })
          await terminal.focus()
          if (await dock.getByRole('button').count() !== 1 || !await terminal.evaluate(node => node === document.activeElement)) errors.push('want one focusable Open terminal button')
          if (choiceWrites().length) errors.push(`unverified dock sent ${JSON.stringify(choiceWrites())}`)
        } else {
          if (await dock.locator('.choice-options button').count() !== 3) errors.push('want 3 option buttons')
          if (await dock.locator('.choice-options [role="listitem"]').count()) errors.push('option buttons lost their button role')
          await roving('.choice-options button[data-row]')
          await markMoves('.choice-options button[data-row]')
          if (state === 'menu') {
            await clickPick(dock.getByRole('button', { name: /Show more below/ }))
            if (picks().join() !== '2:GPT-6-Luna|Fast model.:Down:3') errors.push(`picks: ${picks().join()}`)
          } else {
            const tick = await clickPick(dock.locator('.choice-options button').filter({ hasText: 'Beta' }))
            if (choiceWrites().some(item => item.endpoint === 'menu')) errors.push(`a tick chose a numbered option: ${JSON.stringify(choiceWrites())}`)
            const submit = await clickPick(dock.getByRole('button', { name: /^Done/ }))
            if (tick.heading !== HEADINGS.ticks || tick.revision !== TICKS.menu_verified || tick.row !== 1 || tick.ticked !== false || tick.then !== 'Space'
                || JSON.stringify(tick.labels) !== JSON.stringify(['☐ Alpha', '☐ Beta', '☐ Gamma'])) errors.push(`bad tick: ${JSON.stringify(tick)}`)
            if (submit.heading !== HEADINGS.ticks || submit.revision !== TICKS.menu_verified || 'row' in submit || 'ticked' in submit || submit.then !== 'Enter'
                || JSON.stringify(submit.labels) !== JSON.stringify(['☐ Alpha'])) errors.push(`bad submit: ${JSON.stringify(submit)}`)
            noKeys()
          }
        }
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
        if (overflow > 0) errors.push(`the page scrolls sideways by ${overflow} px`)
        const short = await dock.evaluate(node => [...node.querySelectorAll('button')].filter(b => { const r = b.getBoundingClientRect(); return r.height > 0 && r.height < 24 }).map(b => b.innerText))
        if (short.length) errors.push(`buttons under 24 px: ${short.join(', ')}`)
      } catch (error) { errors.push(error.message.split('\n')[0]) }
      const all = [...errors, ...pageErrors.map(text => `page error: ${text}`)]
      if (all.length) { failed++; console.log(`FAIL ${name}: ${all.join('; ')}`) } else { passed++; console.log(`ok ${name}`) }
    }
  } catch (error) { failed++; console.log(`FAIL setup: ${error.message}`) } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) await new Promise(resolve => server.close(resolve))
  }
  console.log(`${passed} passed, ${failed} failed`)
  if (failed) process.exitCode = 1
}
main()
