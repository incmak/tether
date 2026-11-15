// Search from anywhere: the #search link the extension opens, and the search menu's last rows — add the query to the
// backlog (with Undo) and start a new tab for it (Tether's place, the default after 3 s with a later Move, the picker).
// Every API request stays in this page's fixtures. Run: npm run build -- --outDir <dir>, then DIST=<dir> node test_quick_create_ui.cjs
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/search-anywhere/shots')
const ids = { tether: '$1', blender: '$2' }
const pane = (id, session, name, extra = {}) => ({ id, tmux_session: ids[session], label: `${session}:0.${id.slice(1)}`, ask: null, agent: 'claude', dir: `/srv/${session}`, state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, pane_name: name, ...extra })
const panes = [pane('%1', 'tether', 'Voice mode latency', { group: 'g1' }), pane('%2', 'tether', 'Release notes'), pane('%4', 'blender', 'Render farm')]
const fixture = {
  sessions: { default_thread: null, sessions: panes, groups: { $1: [{ id: 'g1', name: 'Audio stack', project: '' }] } },
  footer: { observed: false, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '', running: 0, runs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}
const audio = { session: '$1', group: 'g1', sessionName: 'tether', groupName: 'Audio stack' }
const triage = { session: '$9', group: 'gt', sessionName: 'General', groupName: 'Triage' }

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], calls = []
  let saved = { text: '- [ ] Write the release notes #B3\n', version: 'v1', limit: 50000, next_id: 4 }
  // Each test sets how the quick-tab endpoints answer.
  let routeAnswer = { place: audio, source: 'ai' }, routeDelay = 0, startFails = 0
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
    page.on('console', message => { if (message.type() === 'error' && !/503/.test(message.text())) pageErrors.push(message.text().slice(0, 300)) })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        const body = request.method() === 'POST' ? request.postDataJSON() : null
        if (endpoint.startsWith('quick-tab/') || (endpoint === 'projects/backlog' && body)) calls.push({ endpoint, body, at: Date.now() })
        if (endpoint === 'projects/backlog') {
          if (body) {
            const next = Math.max(saved.next_id, ...[...body.text.matchAll(/#B(\d+)/g)].map(match => Number(match[1]) + 1))
            saved = { ...saved, text: body.text, version: `v${calls.length + 1}`, next_id: next }
          }
          return route.fulfill({ json: saved })
        }
        if (endpoint === 'quick-tab/route') {
          if (routeDelay) await new Promise(resolve => setTimeout(resolve, routeDelay))
          return route.fulfill({ json: routeAnswer }).catch(() => {})
        }
        if (endpoint === 'quick-tab/start') {
          if (startFails > 0) { startFails--; return route.fulfill({ status: 503, json: { error: 'tmux did not answer.' } }) }
          return route.fulfill({ status: 201, json: { pane: '%50', name: body.message, agent: 'claude', place: body.default ? triage : { ...audio, ...(body.group ? {} : { group: '', groupName: '' }), session: body.session, sessionName: body.session === '$2' ? 'blender' : 'tether' } } })
        }
        if (endpoint === 'quick-tab/move') return route.fulfill({ json: { pane: body.pane, place: body.group === 'g1' ? audio : { session: body.session, group: body.group, sessionName: body.session === '$2' ? 'blender' : 'tether', groupName: '' } } })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      if (!sessionStorage.getItem('kept')) { localStorage.clear(); sessionStorage.setItem('kept', '1') }
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const menu = page.locator('dialog.command-menu')
    const toast = page.locator('.quick-toast')
    const active = () => menu.locator('.command-row.active .command-label').innerText()
    // Open search, type, and put the cursor on a row of the "New" group with the keyboard only.
    const searchFor = async (query, row) => {
      await page.keyboard.press('Alt+Space')
      await menu.waitFor()
      await page.keyboard.type(query)
      await menu.getByText(`Add “${query}” to the backlog`).waitFor()
      await page.keyboard.press('Control+End')
      for (let step = 0; step < 3 && (await active()) !== row; step++) await page.keyboard.press('ArrowUp')
      return (await active()) === row
    }

    await page.setViewportSize({ width: 1280, height: 860 })
    await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
    await page.locator('.sidebar, main').first().waitFor()

    // The extension's link: #search opens the menu on any screen and leaves no history step or hash behind.
    const before = await page.evaluate(() => history.length)
    await page.evaluate(() => { location.hash = 'search' })
    await menu.waitFor()
    check('#search opens search', await menu.getByRole('combobox').evaluate(node => node === document.activeElement))
    check('#search leaves no hash', (await page.evaluate(() => location.hash)) === '')
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'detached' })
    await page.evaluate(() => { location.hash = 'search' })
    await menu.waitFor()
    check('#search works again', true)
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'detached' })
    check('#search keeps Back usable', (await page.evaluate(() => history.length)) <= before + 2)

    // No match: the menu says so and still offers the three "New" rows last.
    check('search reaches "Add to the backlog" by keyboard', await searchFor('Write the onboarding doc', 'Add “Write the onboarding doc” to the backlog'))
    check('no match says so', await menu.getByText('Nothing matches “Write the onboarding doc”.').isVisible())
    const labels = await menu.locator('.command-row .command-label').allInnerTexts()
    check('the New rows come last, in order', JSON.stringify(labels.slice(-3)) === JSON.stringify(['Add “Write the onboarding doc” to the backlog', 'Start a new tab for “Write the onboarding doc”', 'Start a new tab for “Write the onboarding doc” in…']), JSON.stringify(labels))
    await page.screenshot({ path: path.join(SHOTS, 'new-rows-1280.png') })

    // Add to the backlog: the toast names the ID, Undo removes exactly that line.
    await page.keyboard.press('Enter')
    await menu.waitFor({ state: 'detached' })
    await toast.getByText('Added B4 to the backlog.').waitFor()
    check('backlog item saved', /- \[ \] Write the onboarding doc #B4/.test(saved.text), saved.text)
    check('backlog toast is a status', (await toast.locator('[role=status]').count()) === 1)
    await page.screenshot({ path: path.join(SHOTS, 'toast-backlog-1280.png') })
    await toast.getByRole('button', { name: 'Undo' }).click()
    await toast.getByText('Removed B4 from the backlog.').waitFor()
    check('Undo removes the item', !/onboarding/.test(saved.text), saved.text)
    await toast.getByRole('button', { name: 'Dismiss' }).click()
    await toast.waitFor({ state: 'detached' })

    // Start a new tab, Tether chooses in time: the tab starts in that place. Only the query goes to route.
    calls.length = 0
    check('search reaches "Start a new tab" by keyboard', await searchFor('Fix the mic echo', 'Start a new tab for “Fix the mic echo”'))
    await page.keyboard.press('Enter')
    await toast.getByText('Started “Fix the mic echo” in tether · Audio stack.').waitFor()
    const route = calls.find(call => call.endpoint === 'quick-tab/route'), start = calls.find(call => call.endpoint === 'quick-tab/start')
    check('route gets only the query and the model setting', JSON.stringify(route.body) === JSON.stringify({ query: 'Fix the mic echo', agent: 'auto' }), JSON.stringify(route.body))
    check('start goes to the chosen place with a request id', start.body.session === '$1' && start.body.group === 'g1' && /^[A-Za-z0-9_-]{8,64}$/.test(start.body.request) && start.body.message === 'Fix the mic echo', JSON.stringify(start.body))
    check('the toast offers Move', await toast.getByRole('button', { name: /Move/ }).isVisible())
    await page.screenshot({ path: path.join(SHOTS, 'toast-tab-1280.png') })
    await toast.getByRole('button', { name: 'Dismiss' }).click()

    // Tether answers late: the tab starts in General · Triage after 3 s, then the toast offers the later place.
    calls.length = 0
    routeDelay = 4500
    await searchFor('Tune the reverb', 'Start a new tab for “Tune the reverb”')
    await page.keyboard.press('Enter')
    await toast.getByText('Choosing a place for “Tune the reverb”…').waitFor()
    await toast.getByText('Started “Tune the reverb” in General · Triage.').waitFor()
    const waited = calls.find(call => call.endpoint === 'quick-tab/start').at - calls.find(call => call.endpoint === 'quick-tab/route').at
    check('the default start waits about 3 s', waited >= 2900 && waited < 4400, `${waited} ms`)
    check('the default start has no place', calls.find(call => call.endpoint === 'quick-tab/start').body.default === true)
    check('the toast says why', await toast.getByText('Tether took too long to choose, so the tab started in the default place.').isVisible())
    await toast.getByRole('button', { name: 'Move to tether · Audio stack' }).waitFor()
    await page.screenshot({ path: path.join(SHOTS, 'toast-late-1280.png') })
    await toast.getByRole('button', { name: 'Move to tether · Audio stack' }).click()
    await toast.getByText('Moved “Tune the reverb” to tether · Audio stack.').waitFor()
    const move = calls.find(call => call.endpoint === 'quick-tab/move')
    check('Move sends the tab and the place', JSON.stringify(move.body) === JSON.stringify({ pane: '%50', session: '$1', group: 'g1' }), JSON.stringify(move.body))
    check('the toast says the agent keeps its folder', await toast.getByText('The agent keeps its folder.').isVisible())
    await toast.getByRole('button', { name: 'Dismiss' }).click()
    routeDelay = 0

    // The picker: Tab on the row opens it, the choice skips the AI.
    calls.length = 0
    await searchFor('Bake the lights', 'Start a new tab for “Bake the lights”')
    await page.keyboard.press('Tab')
    const listbox = page.getByRole('listbox', { name: /Place for the new tab/ })
    await listbox.waitFor()
    check('Tab opens the place picker', await listbox.isVisible())
    check('the picker lists the default and each stack', await page.getByRole('option', { name: /General · Triage/ }).isVisible() && await page.getByRole('option', { name: /Audio stack/ }).isVisible())
    await page.screenshot({ path: path.join(SHOTS, 'picker-1280.png') })
    await page.getByRole('option', { name: /Tabs in no stack.*blender/ }).click()
    await toast.getByText('Started “Bake the lights” in blender.').waitFor()
    check('the picked place skips the AI', !calls.some(call => call.endpoint === 'quick-tab/route') && calls[0].body.session === '$2' && calls[0].body.group === '', JSON.stringify(calls))
    await toast.getByRole('button', { name: 'Dismiss' }).click()

    // → at the end of the text opens the picker too; Escape returns to the search field.
    await searchFor('Check audio', 'Start a new tab for “Check audio”')
    await page.keyboard.press('ArrowRight')
    await listbox.waitFor()
    check('→ opens the place picker', true)
    await page.keyboard.press('Escape')
    await listbox.waitFor({ state: 'detached' })
    check('Escape closes only the picker', await menu.isVisible())
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'detached' })

    // A failed start: an alert with Try again, and the retry sends the same request id.
    calls.length = 0
    startFails = 1
    await searchFor('Fix the build', 'Start a new tab for “Fix the build”')
    await page.keyboard.press('Enter')
    await toast.getByText(/The tab did not start\. tmux did not answer\./).waitFor()
    check('a failed start is an alert', (await toast.locator('[role=alert]').count()) === 1)
    await toast.getByRole('button', { name: 'Try again' }).click()
    await toast.getByText('Started “Fix the build” in tether · Audio stack.').waitFor()
    const starts = calls.filter(call => call.endpoint === 'quick-tab/start')
    check('the retry reuses the request id', starts.length === 2 && starts[0].body.request === starts[1].body.request)
    await toast.getByRole('button', { name: 'Dismiss' }).click()

    // Narrow screens: rows, picker and toast fit without page overflow.
    for (const [width, height] of [[390, 844], [320, 640]]) {
      await page.setViewportSize({ width, height })
      const long = 'A long task name that keeps going past the edge of a small phone screen'
      await searchFor(long, `Start a new tab for “${long}”`)
      check(`${width}px menu fits`, !(await overflow()) && (await menu.boundingBox()).width <= width)
      await page.keyboard.press('Tab')
      await listbox.waitFor()
      const box = await listbox.boundingBox()
      check(`${width}px picker fits`, box.x >= 0 && box.x + box.width <= width + 0.5, JSON.stringify(box))
      await page.screenshot({ path: path.join(SHOTS, `picker-${width}.png`) })
      await page.keyboard.press('Escape')
      await listbox.waitFor({ state: 'detached' })
      await page.keyboard.press('Enter')
      await toast.getByText(/^Started/).waitFor()
      const toastBox = await toast.boundingBox()
      check(`${width}px toast fits`, !(await overflow()) && toastBox.x >= 0 && toastBox.x + toastBox.width <= width + 0.5, JSON.stringify(toastBox))
      await page.screenshot({ path: path.join(SHOTS, `toast-${width}.png`) })
      await toast.getByRole('button', { name: 'Dismiss' }).click()
    }
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`quick create ui: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
