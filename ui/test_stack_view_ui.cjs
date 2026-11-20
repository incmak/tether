// Stack view: the stack row and the tab strip keep the open stack and tab in view, and the sidebar shows a session's
// stacks like the chat header. Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('stack-view')
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
  const stackWrites = []
  let allowStackClose = false
  const staleStack = 'The stack’s tabs changed. Open the stack menu again to review them.'
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
        if (['groups/close', 'groups/remove'].includes(endpoint) || (endpoint === 'session' && route.request().method() === 'DELETE')) {
          stackWrites.push({ endpoint, ...route.request().postDataJSON() })
          if (endpoint === 'groups/remove' || endpoint === 'groups/close' && !allowStackClose) {
            return route.fulfill({ status: 409, json: { error: staleStack } })
          }
          return route.fulfill({ json: { ok: true } })
        }
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
      if (performance.getEntriesByType('navigation')[0]?.type === 'reload') return
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      const off = new URL(location.href).searchParams.has('off')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false, sideMode: 'sessions', ...off ? { headerSwipe: false } : {} }))
    })
    const TABS = '.chat-panel .pane-tabs', ROW = '.chat-panel .group-row', SIDE = '.sidebar .session-groups'
    const open = async (id, width = 1280, height = 800) => {
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=${encodeURIComponent(id)}`, { waitUntil: 'load' })
      await page.locator('.chat-panel .group-row, .chat-panel .group-switch').locator('visible=true').first().waitFor()  // the stack row (desktop) or switcher (phone)
      await page.waitForFunction(id => { const head = document.querySelector('.chat-panel'); return head && (document.querySelector(`[data-pane="${id}"][aria-selected="true"]`) || document.querySelector('.sidebar [aria-current="page"]')?.textContent?.length) }, id)
      await page.waitForTimeout(150)
    }
    // The item lies fully inside its scroll box (1 px for rounding).
    const inView = (box, item) => page.evaluate(([box, item]) => {
      const strip = document.querySelector(box), element = strip && strip.querySelector(item)
      if (!element) return `no ${item}`
      const a = strip.getBoundingClientRect(), b = element.getBoundingClientRect()
      return b.left >= a.left - 1 && b.right <= a.right + 1 && b.top >= a.top - 1 && b.bottom <= a.bottom + 1 ? true : `item ${Math.round(b.left)}..${Math.round(b.right)} x ${Math.round(b.top)}..${Math.round(b.bottom)}, box ${Math.round(a.left)}..${Math.round(a.right)} x ${Math.round(a.top)}..${Math.round(a.bottom)}`
    }, [box, item])
    const scrolled = selector => page.locator(selector).first().evaluate(element => Math.round(element.scrollLeft))
    const current = () => page.locator('.group-tab[aria-current="true"] .group-name').first().textContent()

    // 1. The reported case: scroll the long stack row to its end, click the last stack. The row must not jump to the left.
    await open('%2')
    await page.locator(ROW).evaluate(element => { element.scrollLeft = element.scrollWidth })
    const end = await scrolled(ROW)
    await page.locator(`${ROW} .group-tab`, { hasText: 'Customer follow-ups' }).click()
    await page.locator('[role="tab"][data-pane="%28"][aria-selected="true"], .chat-panel .group-tab[aria-current="true"]').first().waitFor()
    await page.waitForTimeout(200)
    check('a click on the last stack opens it', await current() === 'Customer follow-ups', await current())
    const after = await scrolled(ROW)
    check('the stack row keeps its place after the click (no jump to the left)', Math.abs(after - end) <= 1, `before ${end}, after ${after}`)
    check('the open stack is fully in view', await inView(ROW, '.group-tab[aria-current="true"]') === true, String(await inView(ROW, '.group-tab[aria-current="true"]')))
    await page.screenshot({ path: path.join(SHOTS, '1280-last-stack.png') })

    // 2. A second case of the same kind: a stack in the middle, from a row scrolled to the start.
    await page.locator(ROW).evaluate(element => { element.scrollLeft = 0 })
    await page.locator(`${ROW} .group-tab`, { hasText: 'Ship' }).click()
    await page.locator('.chat-panel .group-tab[aria-current="true"]', { hasText: 'Ship' }).waitFor()
    const start = await scrolled(ROW)
    const middle = page.locator(`${ROW} .group-tab`, { hasText: 'Backlog and planning' })
    // Clear of the 24 px edge fade (revealLeft): scrollIntoViewIfNeeded can leave it under the fade, which counts as not in view.
    await middle.evaluate(element => element.scrollIntoView({ inline: 'center', block: 'nearest' }))
    const before = await scrolled(ROW)
    await middle.click()
    await page.locator('.chat-panel .group-tab[aria-current="true"]', { hasText: 'Backlog and planning' }).waitFor()
    await page.waitForTimeout(200)
    check('a click on a visible middle stack keeps the row still', Math.abs(await scrolled(ROW) - before) <= 1, `start ${start}, before ${before}, after ${await scrolled(ROW)}`)

    // 3. Opening a tab from outside the row (a link, the sidebar, a key) brings its stack into view.
    await open('%28')
    check('a direct open of a tab in the last stack shows that stack', await inView(ROW, '.group-tab[aria-current="true"]') === true, String(await inView(ROW, '.group-tab[aria-current="true"]')))
    await open('%12')
    check('a direct open of the last tab of a long stack shows that tab in the tab strip', await inView(TABS, '[role="tab"][aria-selected="true"]') === true, String(await inView(TABS, '[role="tab"][aria-selected="true"]')))
    check('and its stack in the stack row', await inView(ROW, '.group-tab[aria-current="true"]') === true)

    // 3b. A narrower window keeps the open stack and tab in view (1500 -> 1000 px, and 1280 -> 1000 px).
    for (const [wide, id] of [[1500, '%28'], [1280, '%12']]) {
      await open(id, wide)
      await page.setViewportSize({ width: 1000, height: 800 })
      await page.waitForTimeout(250)
      check(`${wide} -> 1000 px keeps the open stack in view (${id})`, await inView(ROW, '.group-tab[aria-current="true"]') === true, String(await inView(ROW, '.group-tab[aria-current="true"]')))
      if (id === '%12') check(`${wide} -> 1000 px keeps the open tab in view`, await inView(TABS, '[role="tab"][aria-selected="true"]') === true, String(await inView(TABS, '[role="tab"][aria-selected="true"]')))
    }

    // 4. Each stack keeps its own tab strip place: a scroll in Ship does not move another stack's strip.
    await page.locator(TABS).evaluate(element => { element.scrollLeft = 0 })
    await page.locator(`${ROW} .group-tab`, { hasText: 'Release work' }).click()
    await page.locator('.chat-panel .group-tab[aria-current="true"]', { hasText: 'Release work' }).waitFor()
    await page.locator(`${ROW} .group-tab`, { hasText: 'Ship' }).click()
    await page.locator('.chat-panel .group-tab[aria-current="true"]', { hasText: 'Ship' }).waitFor()
    await page.waitForTimeout(150)
    check('back in Ship, the open tab is still in view', await inView(TABS, '[role="tab"][aria-selected="true"]') === true, String(await inView(TABS, '[role="tab"][aria-selected="true"]')))

    // 5. The sidebar shows the session's stacks like the header: same order, each stack's tabs under it.
    await open('%28', 1280, 700)
    const sideStacks = await page.locator(`${SIDE} .pane-stack-head .pane-child-name`).allTextContents()
    const rowStacks = await page.locator(`${ROW} .group-tab .group-name`).allTextContents()
    check('the sidebar lists the stacks in the header order', JSON.stringify(sideStacks) === JSON.stringify(rowStacks), `${sideStacks} / ${rowStacks}`)
    const stack = (name, side = SIDE) => page.locator(`${side} .pane-stack`, { has: page.locator('.pane-stack-head .pane-child-name', { hasText: name }) })
    const ship = stack('Ship')
    const shipFold = ship.locator('.pane-stack-fold')
    const shipTabs = await ship.locator('button[data-select-pane]').count()
    check('each stack holds its own tabs (Ship: 12)', shipTabs === 12, String(shipTabs))
    const marked = await page.locator(`${SIDE} .pane-stack-head.current .pane-child-name`).allTextContents()
    const openRow = await page.locator(`${SIDE} .pane-stack`, { has: page.locator('[aria-current="page"]') }).locator('.pane-stack-head .pane-child-name').allTextContents()
    check('the open tab is marked in its stack', JSON.stringify(marked) === '["Customer follow-ups"]' && JSON.stringify(openRow) === '["Customer follow-ups"]', `${marked} / ${openRow}`)
    check('the sidebar list scrolls the open tab into view', await inView(SIDE, '.pane-stack-tabs [aria-current="page"]') === true, String(await inView(SIDE, '.pane-stack-tabs [aria-current="page"]')))
    await page.screenshot({ path: path.join(SHOTS, '1280-sidebar-stacks.png') })

    const openControls = await page.locator(`${SIDE} .pane-stack`).evaluateAll(stacks => {
      const ids = new Set()
      return stacks.every(stack => {
        const fold = stack.querySelector('.pane-stack-fold')
        const id = fold?.getAttribute('aria-controls')
        const box = fold?.getBoundingClientRect()
        const tabs = stack.querySelector('ul.pane-stack-tabs')
        if (!id || /\s/.test(id) || ids.has(id) || !tabs || document.getElementById(id) !== tabs) return false
        ids.add(id)
        return fold.getAttribute('aria-expanded') === 'true' && box.width >= 24 && box.height >= 24
      })
    })
    check('every stack starts open with a unique list ID and a fold target of at least 24 px', sideStacks.length > 0 && openControls)
    await ship.locator('.pane-stack-head').focus()
    await page.keyboard.press('Tab')
    check('keyboard focus outlines the fold button', await shipFold.evaluate(button => button.matches(':focus-visible') && getComputedStyle(button).outlineWidth === '2px'))
    await page.keyboard.press('Space')
    await ship.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('folding Ship removes its tab rows and keeps its heading dot', await ship.locator('button[data-select-pane]').count() === 0
      && await ship.locator('.pane-stack-head .state-dot').isVisible() && await page.locator(`[id="${await shipFold.getAttribute('aria-controls')}"]`).evaluate(list => list.hidden && list.children.length === 0)
      && (await shipFold.getAttribute('aria-label')).startsWith('Show tabs of stack Ship'))
    check('Release work stays open', await stack('Release work').locator('.pane-stack-fold').getAttribute('aria-expanded') === 'true'
      && await stack('Release work').locator('button[data-select-pane]').count() === 1)

    await page.reload({ waitUntil: 'load' })
    await ship.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('Ship stays folded after reload', await ship.locator('button[data-select-pane]').count() === 0)
    const customer = stack('Customer follow-ups')
    const customerFold = customer.locator('.pane-stack-fold')
    check('the selected stack has a disabled fold button', await customerFold.getAttribute('aria-disabled') === 'true'
      && await customerFold.getAttribute('aria-label') === 'Tabs of stack Customer follow-ups stay shown while one of them is open')
    await customerFold.click({ force: true })
    check('clicking the disabled fold button keeps the selected tab visible', await customerFold.getAttribute('aria-expanded') === 'true'
      && await customer.locator('button[data-select-pane][aria-current="page"]').isVisible())

    const unstacked = stack('Unstacked')
    await unstacked.locator('.pane-stack-fold').click()
    await unstacked.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('Unstacked folds independently and removes Loose end', await unstacked.locator('button[data-select-pane]').count() === 0)

    await page.mouse.move(640, 350)
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme })
      await shipFold.evaluate(button => button.scrollIntoView({ block: 'center' }))
      await page.screenshot({ animations: 'disabled', path: path.join(SHOTS, `1280-folded-${colorScheme}.png`) })
    }
    await page.setViewportSize({ width: 320, height: 700 })
    await page.locator('.mobile-menu').locator('visible=true').first().click()
    const phoneShip = stack('Ship', '.drawer[open]')
    const phoneFold = phoneShip.locator('.pane-stack-fold[aria-expanded="false"]')
    await phoneFold.waitFor()
    const phoneBox = await phoneFold.boundingBox()
    check('320 px: the drawer shows the folded stack and its fold target fits', phoneBox.width >= 24 && phoneBox.height >= 24
      && await page.evaluate(() => { const box = document.querySelector('.drawer .sidebar-inner'); return box.scrollWidth <= box.clientWidth + 1 && document.documentElement.scrollWidth <= innerWidth }))
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme })
      await phoneFold.evaluate(button => button.scrollIntoView({ block: 'center' }))
      await page.screenshot({ animations: 'disabled', path: path.join(SHOTS, `320-folded-${colorScheme}.png`) })
    }
    await page.keyboard.press('Escape')
    await page.locator('.drawer[open]').waitFor({ state: 'hidden' })
    await page.setViewportSize({ width: 1280, height: 700 })
    await page.emulateMedia({ colorScheme: null })

    const search = page.locator('.sidebar input[aria-label="Search sessions"]')
    await search.fill('Docs pass for the release notes')
    await page.locator(`${SIDE} button[data-select-pane="%2"]`).waitFor()
    check('search shows matching tabs from folded stacks without fold controls', await page.locator(`${SIDE} .pane-stack-fold`).count() === 0
      && await page.locator(`${SIDE} button[data-select-pane="%2"]`).isVisible())
    await search.fill('')
    await ship.locator('.pane-stack-head').click()
    await ship.locator('button[data-select-pane][aria-current="page"]').waitFor()
    check('opening a folded stack from its heading forces its tabs open', await shipFold.getAttribute('aria-disabled') === 'true'
      && await shipFold.getAttribute('aria-expanded') === 'true')
    await customer.locator('.pane-stack-head').click()
    await ship.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('leaving Ship restores its saved fold state', await ship.locator('button[data-select-pane]').count() === 0)
    // Focus survives when a folded stack closes again under it.
    await ship.locator('.pane-stack-head').click()
    await ship.locator('button[data-select-pane][aria-current="page"]').waitFor()
    await ship.locator('button[data-select-pane]').last().click()
    await page.keyboard.press('Alt+ArrowDown')
    await ship.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('Alt+Down out of a folded stack moves focus to the new tab row', await page.evaluate(() => document.activeElement?.matches('button[data-select-pane][aria-current="page"]')))
    await ship.locator('.pane-stack-head').click()
    await ship.locator('button[data-select-pane][aria-current="page"]').waitFor()
    await ship.locator('button[data-select-pane]').last().focus()
    await customer.locator('.pane-stack-head').evaluate(head => head.click())
    await ship.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('a selection change from elsewhere moves focus to the refolded stack\'s fold button', await shipFold.evaluate(button => document.activeElement === button))
    // Focus that left on its own is not pulled back: blur the fold button, then make the list render again.
    await shipFold.evaluate(button => button.blur())
    const unstackedFold = unstacked.locator('.pane-stack-fold')
    await unstackedFold.evaluate(button => button.click())
    await unstacked.locator('.pane-stack-fold[aria-expanded="true"]').waitFor()
    const stayedOnBody = await page.evaluate(() => document.activeElement === document.body)
    await unstackedFold.evaluate(button => button.click())
    await unstacked.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('a later render does not pull focus back to a fold button', stayedOnBody && await page.evaluate(() => document.activeElement === document.body))
    // A row the user left earlier does not count when a later shortcut removes it.
    await ship.locator('.pane-stack-head').click()
    await ship.locator('button[data-select-pane][aria-current="page"]').waitFor()
    await ship.locator('button[data-select-pane]').last().click()
    await ship.locator('button[data-select-pane]').last().evaluate(row => row.blur())
    await page.keyboard.press('Alt+ArrowDown')
    await ship.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('Alt+Down with focus outside the sidebar leaves focus outside', await page.evaluate(() => document.activeElement === document.body))
    await shipFold.click()
    await unstacked.locator('.pane-stack-fold').click()
    await ship.locator('button[data-select-pane="%12"]').click()
    await ship.locator('button[data-select-pane="%12"][aria-current="page"]').waitFor()
    await page.keyboard.press('Alt+ArrowDown')
    await page.locator(`${SIDE} button[data-select-pane="%20"][aria-current="page"]`).waitFor()
    check('Alt+Down crosses a stack boundary to the next tab without folding a stack', await page.locator(`${SIDE} .pane-stack-fold[aria-expanded="false"]`).count() === 0)

    // A stack heading opens that stack, like a stack in the header.
    await page.locator(`${SIDE} .pane-stack-head`, { hasText: 'Voice and dictation' }).click()
    await page.locator('.chat-panel .group-tab[aria-current="true"]', { hasText: 'Voice and dictation' }).waitFor()
    check('a sidebar stack heading opens its stack', await current() === 'Voice and dictation')
    // Alt+Down steps through tab rows only, never onto a heading.
    const rows = await page.evaluate(() => [...document.querySelectorAll('.sidebar .session-groups .pane-children button[data-select-pane]')].length)
    check('keyboard rows leave out the stack headings', rows === 22, String(rows))

    // 6. Phone width: no horizontal page scroll; the sidebar drawer is not part of this check.
    await open('%28', 390, 800)
    check('no horizontal page scroll at 390 px', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))

    sessions = { default_thread: 't88', groups: { $1: [{ id: 'g00000001', name: 'Tether bugs', project: '' },
      { id: 'g00000002', name: 'Task tree', project: '' }] }, sessions: [
      pane('%88', 'B401 Sidebar fix', { group: 'g00000001' }),
      pane('%1', 'B446 Task tree', { group: 'g00000002' }),
      pane('%82', 'B446 server', { group: 'g00000001', task: '%1', task_depth: 1, task_role: 'Worker' }),
    ] }
    await open('%88')
    await page.getByRole('button', { name: 'Tether bugs, 1 tab', exact: true }).click({ button: 'right' })
    const stackDialog = page.getByRole('dialog', { name: 'Tether bugs' })
    await stackDialog.getByRole('button', { name: 'Close all tabs…' }).click()
    await stackDialog.getByRole('button', { name: 'Close 1 tab' }).click()
    await stackDialog.getByRole('alert').getByText(staleStack).waitFor()
    check('a refused stack close sends no individual Deletes', JSON.stringify(stackWrites) ===
      JSON.stringify([{ endpoint: 'groups/close', session: '$1', group: 'g00000001', panes: [{ pane: '%88', thread: 't88' }] }]),
    JSON.stringify(stackWrites))

    allowStackClose = true
    await open('%88', 320, 800)
    await page.getByRole('button', { name: /^Stack: Tether bugs/ }).click()
    await page.getByRole('button', { name: 'Actions for Task tree' }).click()
    const treeDialog = page.getByRole('dialog', { name: 'Task tree' })
    await treeDialog.getByRole('button', { name: 'Close all tabs…' }).click()
    await treeDialog.getByRole('button', { name: 'Close 2 tabs' }).click()
    await treeDialog.waitFor({ state: 'hidden' })
    check('320 px: stack close includes a child displayed with its task', JSON.stringify(stackWrites.at(-1)) ===
      JSON.stringify({ endpoint: 'groups/close', session: '$1', group: 'g00000002',
        panes: [{ pane: '%1', thread: 't1' }, { pane: '%82', thread: 't82' }] }), JSON.stringify(stackWrites))
    check('no page errors', pageErrors.length === 0, pageErrors.join('\n').slice(0, 800))
  } catch (error) {
    failed++
    console.log('FAIL', error.stack)
  } finally {
    await page?.close().catch(() => {})
    await browser?.close?.().catch?.(() => {})
    server?.close()
  }
  if (failed) { console.log(`${failed} failed`); process.exit(1) }
  console.log('test_stack_view_ui: ok')
}
main()
