// Sidebar views: Catch up (what waits on you, by goal) and the Sessions tree with need-you counts and Active only.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('sidebar-catchup')
const NOW = Math.floor(Date.now() / 1000)
const ask = (pane, ago, extra = {}) => ({ pane, thread: null, since: NOW - ago, next: `Next for ${pane}`, kind: 'reply', opened: false, snoozed_until: null, ...extra })
const pane = (id, session, name, label, extra) => ({ id, tmux_session: session, label, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const busy = {
  sessions: [
    pane('%1', '$1', 'Release notes', 'tether:0.1', { group: 'g1', ask: ask('%1', 3 * 3600) }),
    pane('%2', '$1', 'Deploy check', 'tether:0.2', { group: 'g1', ask: ask('%2', 1800, { urgent: true, opened: true }) }),
    pane('%3', '$1', 'Sidebar build', 'tether:0.3', { state: 'Working', since: NOW - 300 }),
    pane('%4', '$1', 'Old idea', 'tether:0.4', { group: 'g2' }),
    pane('%5', '$1', 'Older idea', 'tether:0.5', { group: 'g2' }),
    pane('%6', '$1', 'Voice fix', 'tether:0.6', { finished: NOW - 600 }),
    pane('%10', '$2', 'BriskBot', 'briskbot', { state: 'Approval', since: NOW - 2 * 3600 }),
    pane('%20', '$3', 'Notes one', 'notes:0.0', {}), pane('%21', '$3', 'Notes two', 'notes:0.1', {}),
    pane('%30', '$4', 'Scratch', 'scratch', {}),
  ],
  default_thread: 't3', groups: { $1: [{ id: 'g1', name: 'Release', project: '' }, { id: 'g2', name: 'Quiet stack', project: '' }] },
}
const calm = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%30', '$4', 'Scratch', 'scratch', {})], default_thread: 't3' }
let sessions = busy
const fixture = {
  chat: { messages: [], busy: false, thread: 't3', agent: 'claude', writable: true, session: '%3', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%3', running: 0, runs: [] },
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
      // A reload keeps what the page stored; a fresh open starts from these prefs.
      const asked = new URL(location.href).searchParams.get('prefs')
      if (asked === null || sessionStorage.getItem('kept') === asked) return
      sessionStorage.setItem('kept', asked)
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false, ...JSON.parse(asked) }))
    })
    const SIDE = '.sidebar'
    // The urgent ask opens the answer deck by itself; close it to reach the sidebar.
    const closeDeck = async () => { if (await page.locator('dialog.action-deck[open]').count()) { await page.keyboard.press('Escape'); await page.locator('dialog.action-deck[open]').waitFor({ state: 'detached' }).catch(() => {}) } }
    // keepDeck: leave a deck that opened by itself, so a test can see it.
    const open = async (id, width = 1280, height = 800, prefs = {}, { keepDeck = false } = {}) => {
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=${encodeURIComponent(id)}&prefs=${encodeURIComponent(JSON.stringify(prefs))}`, { waitUntil: 'load' })
      await page.locator('.chat-panel').first().waitFor()
      await page.waitForTimeout(300)
      if (!keepDeck) await closeDeck()
    }
    const texts = selector => page.locator(selector).allTextContents()
    const pressed = name => page.locator(`${SIDE} .side-mode-switch button`, { hasText: name }).getAttribute('aria-pressed')

    // 1. Catch up is the first view: what waits on you, grouped by goal, oldest first.
    await open('%3')
    check('Catch up is on by default', await pressed('Catch up') === 'true' && await pressed('Sessions') === 'false')
    check('the switch shows how many tabs wait (3)', (await page.locator(`${SIDE} .side-mode-count`).textContent()).replace(/\D/g, '') === '3')
    const goals = await texts(`${SIDE} .catchup-goal-name`)
    check('goals: the stack, then the session, oldest first', JSON.stringify(goals) === '["Release","briskbot"]', JSON.stringify(goals))
    check('a stack goal names its session', (await page.locator(`${SIDE} .catchup-goal-session`).first().textContent()).includes('tether'))
    const release = await page.locator(`${SIDE} .catchup-goal`, { hasText: 'Release' }).locator('.catchup-name').allTextContents()
    check('tabs in a goal go oldest first', JSON.stringify(release) === '["Release notes","Deploy check"]', JSON.stringify(release))
    check('an urgent ask has the urgent mark', await page.locator(`${SIDE} .catchup-row`, { hasText: 'Deploy check' }).locator('.catchup-dot.urgent').count() === 1)
    check('the ask shows its next line', await page.locator(`${SIDE} .catchup-row`, { hasText: 'Release notes' }).locator('.catchup-next').textContent() === 'Next for %1')
    check('an approval without an ask still shows', await page.locator(`${SIDE} .catchup-row`, { hasText: 'BriskBot' }).locator('.catchup-next').textContent() === 'Approval')
    check('the finished turn shows', JSON.stringify(await page.locator(`${SIDE} .catchup-part`, { has: page.locator('h2', { hasText: 'Finished' }) }).locator('.catchup-name').allTextContents()) === '["Voice fix"]')
    await page.screenshot({ path: path.join(SHOTS, '1280-catch-up.png') })
    // Newest first flips goals and tabs.
    await page.locator(`${SIDE} .catchup-head .waiting-sort`).first().click()
    check('newest first puts the newest goal first', JSON.stringify(await texts(`${SIDE} .catchup-goal-name`)) === '["briskbot","Release"]', JSON.stringify(await texts(`${SIDE} .catchup-goal-name`)))
    await page.locator(`${SIDE} .catchup-head .waiting-sort`).first().click()
    // Working tabs wait behind one button.
    const toggle = page.locator(`${SIDE} .catchup-toggle`)
    check('working tabs fold behind "Show 1 working"', (await toggle.textContent()).includes('Show 1 working') && await toggle.getAttribute('aria-expanded') === 'false')
    await toggle.click()
    check('Show lists the working tab', await page.locator(`${SIDE} .catchup .running-list`, { hasText: 'Sidebar build' }).count() === 1 && await toggle.getAttribute('aria-expanded') === 'true')
    // A row opens its tab.
    await page.locator(`${SIDE} .catchup-row`, { hasText: 'BriskBot' }).click()
    await page.waitForFunction(() => document.querySelector('.sidebar .catchup-row[aria-current="page"]')?.textContent?.includes('BriskBot'))
    check('a Catch up row opens its tab', true)
    // Alt+Down steps through the Catch up rows.
    await page.keyboard.press('Alt+ArrowDown')
    const currentRow = (name, selector = '.catchup-row') => page.locator(`${SIDE} ${selector}[aria-current="page"]`, { hasText: name })
    const title = () => page.locator('.chat-title h1').first().innerText()
    await currentRow('Voice fix').waitFor()
    // Voice fix is the next row; it stays in its place while it is open.
    check('Alt+Down opens the next Catch up row', await title() === 'Voice fix' && await currentRow('Voice fix').count() === 1, await title())
    await page.keyboard.press('Alt+ArrowUp')
    await currentRow('BriskBot').waitFor()
    check('Alt+Up goes back to the row before', await title() === 'BriskBot', await title())
    // Voice fix was read when you left it, so Alt+Down goes on to the working list.
    await page.keyboard.press('Alt+ArrowDown')
    await currentRow('Sidebar build', '.catchup .running-list button').waitFor()
    check('the read row does not come back', await page.locator(`${SIDE} .catchup-row`, { hasText: 'Voice fix' }).count() === 0)
    check('Alt+Down skips the read row and reaches the working list', await title() === 'Sidebar build' && await page.locator(`${SIDE} .catchup-row`, { hasText: 'Voice fix' }).count() === 0, await title())
    // Mark all as read empties Finished. (New prefs in the address start from fresh storage, so the turn is unread again.)
    await open('%3', 1280, 800, { sideMode: 'catchup' })
    await page.locator(`${SIDE} [aria-label="Mark all finished as read"]`).click()
    check('Mark all as read clears Finished', await page.locator(`${SIDE} h2`, { hasText: 'Finished' }).count() === 0)

    // 2. Sessions: the tree, with need-you counts on group, session and stack rows.
    await page.locator(`${SIDE} .side-mode-switch button`, { hasText: 'Sessions' }).click()
    check('Sessions is pressed', await pressed('Sessions') === 'true')
    check('no lifted "Needs you" group', await page.locator(`${SIDE} .group-toggle`, { hasText: 'Needs you' }).count() === 0)
    const groupCount = await page.locator(`${SIDE} .group-toggle .rollup`).first().textContent()
    check('the group row counts the tabs that wait (3)', groupCount.startsWith('3'), groupCount)
    const tether = page.locator(`${SIDE} .session-list > li[data-id="$1"]`)
    check('the session row counts its waiting tabs (2)', (await tether.locator('.session-main .rollup').textContent()).startsWith('2'))
    check('a session with waiting tabs is open', await tether.locator('.pane-children').count() === 1)
    check('the stack row counts its waiting tabs (2)', await tether.locator('.pane-stack-head', { hasText: 'Release' }).locator('.rollup').textContent() === '2')
    const releaseNotes = tether.getByRole('button', { name: 'Release notes, Task, needs you', exact: true })
    check('a tab whose ask waits has the Needs you dot', await releaseNotes.locator('.task-dot.needs').isVisible())
    check('the stack row says it to a screen reader', (await tether.locator('.pane-stack-head', { hasText: 'Release' }).getAttribute('aria-label')).includes('2 need you'))
    const releaseStack = tether.locator('.pane-stack', { has: page.locator('.pane-stack-head', { hasText: 'Release' }) })
    await releaseStack.locator('.pane-stack-fold').click()
    await releaseStack.locator('.pane-stack-fold[aria-expanded="false"]').waitFor()
    check('the folded Release stack still shows its waiting count (2)', await releaseStack.locator('.pane-stack-head .rollup').textContent() === '2'
      && await releaseStack.locator('button[data-select-pane]').count() === 0)
    await releaseStack.locator('.pane-stack-fold').click()
    await releaseStack.locator('.pane-stack-fold[aria-expanded="true"]').waitFor()
    check('a quiet session with several tabs starts folded', await page.locator(`${SIDE} .session-list > li[data-id="$3"] .pane-children`).count() === 0)
    await page.locator(`${SIDE} .session-list > li[data-id="$3"] .session-main`).click()
    check('its row opens it', await page.locator(`${SIDE} .session-list > li[data-id="$3"] .pane-children`).count() === 1)
    await page.locator(`${SIDE} .session-list > li[data-id="$3"] .session-main`).click()
    check('and folds it again', await page.locator(`${SIDE} .session-list > li[data-id="$3"] .pane-children`).count() === 0)
    await page.screenshot({ path: path.join(SHOTS, '1280-sessions.png') })

    // 3. Active only folds quiet sessions and quiet tabs.
    const active = page.locator(`${SIDE} .side-active`)
    await active.click()
    check('Active only is pressed', await active.getAttribute('aria-pressed') === 'true')
    check('quiet sessions fold into one row', await page.locator(`${SIDE} .session-list > li[data-id="$3"], ${SIDE} .session-list > li[data-id="$4"]`).count() === 0
      && (await page.locator(`${SIDE} .group > .quiet-more`).textContent()) === 'Show 2 quiet sessions')
    check('quiet tabs fold, and a stack of only quiet tabs hides', await tether.locator('.pane-stack-head', { hasText: 'Quiet stack' }).count() === 0
      && (await tether.locator('.quiet-more').textContent()) === 'Show 3 quiet tabs', await tether.locator('.quiet-more').textContent().catch(e => e.message))
    check('the read tab folds, the open and waiting tabs stay', await tether.locator('.pane-children button', { hasText: 'Voice fix' }).count() === 0 && await tether.locator('.pane-children button', { hasText: 'Sidebar build' }).count() === 1 && await tether.locator('.pane-children button', { hasText: 'Deploy check' }).count() === 1)
    await page.screenshot({ path: path.join(SHOTS, '1280-active-only.png') })
    await tether.locator('.quiet-more').click()
    check('Show brings the quiet tabs back', await tether.locator('.pane-stack-head', { hasText: 'Quiet stack' }).count() === 1 && await tether.locator('.quiet-more').getAttribute('aria-expanded') === 'true')
    await page.locator(`${SIDE} .group > .quiet-more`).click()
    check('Show brings the quiet sessions back', await page.locator(`${SIDE} .session-list > li[data-id="$4"]`).count() === 1)
    const rows = await page.evaluate(() => [...document.querySelectorAll('.sidebar .session-groups .pane-children button[data-select-pane]')].length)
    check('keyboard rows count every shown tab', rows === 6, String(rows))

    // 4. The view stays after a reload (this device).
    await page.reload({ waitUntil: 'load' })
    await page.locator(`${SIDE} .side-mode-switch`).waitFor()
    await page.waitForTimeout(300)
    await closeDeck()
    check('Sessions and Active only stay after a reload', await pressed('Sessions') === 'true' && await page.locator(`${SIDE} .side-active`).getAttribute('aria-pressed') === 'true')

    // 5. A search always shows the tree, without the switch.
    await page.locator(`${SIDE} .side-mode-switch button`, { hasText: 'Catch up' }).click()
    await page.locator(`${SIDE} input[aria-label="Search sessions"]`).fill('scratch')
    check('a search hides the switch and shows matching sessions', await page.locator(`${SIDE} .side-modes`).count() === 0 && await page.locator(`${SIDE} .session-list > li[data-id="$4"]`).count() === 1 && await page.locator(`${SIDE} .catchup`).count() === 0)
    await page.locator(`${SIDE} input[aria-label="Search sessions"]`).fill('')
    check('clearing the search goes back to Catch up', await page.locator(`${SIDE} .catchup`).count() === 1)

    // 6. Nothing waits: the empty state.
    sessions = calm
    await open('%3', 1280, 800)
    check('nothing waits: "You are caught up."', (await page.locator(`${SIDE} .catchup-empty`).textContent()).includes('You are caught up.') && await page.locator(`${SIDE} .side-mode-count`).count() === 0)
    // Seven finished turns: five show, and the open sixth one shows too.
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), ...[1, 2, 3, 4, 5, 6, 7].map(n => pane(`%4${n}`, '$5', `Done ${n}`, `done:0.${n}`, { finished: NOW - n * 60 }))], default_thread: 't3' }
    await open('%3', 1280, 800, { sideMode: 'catchup', attention: true })
    const more = page.locator(`${SIDE} .catchup-more`)
    check('five finished rows show, the rest behind "+2 more"', (await texts(`${SIDE} .catchup-row`)).length === 5 && await more.textContent() === '+2 more')
    await more.click()
    await page.locator(`${SIDE} .catchup-row`, { hasText: 'Done 6' }).click()
    await more.click()
    await page.waitForTimeout(200)
    check('the open sixth row still shows when folded', await page.locator(`${SIDE} .catchup-row[aria-current="page"]`, { hasText: 'Done 6' }).count() === 1 && await more.textContent() === '+1 more', await more.textContent())

    // Six finished turns with the sixth open: all six show, with no "+0 more".
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), ...[1, 2, 3, 4, 5, 6].map(n => pane(`%4${n}`, '$5', `Done ${n}`, `done:0.${n}`, { finished: NOW - n * 60 }))], default_thread: 't3' }
    await open('%3', 1280, 800, { sideMode: 'catchup', attention: true, n: 6 })
    await page.locator(`${SIDE} .catchup-more`).click()
    await page.locator(`${SIDE} .catchup-row`, { hasText: 'Done 6' }).click()
    await page.waitForTimeout(200)
    check('six rows with the sixth open: all show, no more button', (await texts(`${SIDE} .catchup-row`)).length === 6 && await page.locator(`${SIDE} .catchup-more`).count() === 0)

    // An approval with no ask (B134): the row opens the tab, and the deck shows it as a card that opens the tab.
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%10', '$2', 'BriskBot', 'briskbot', { state: 'Approval', since: NOW - 60 })], default_thread: 't3' }
    for (const [width, height] of [[1280, 800], [320, 700]]) {
      await open('%3', width, height, { sideMode: 'catchup', attention: true })
      if (width === 320) { await page.locator('.mobile-menu').locator('visible=true').first().click(); await page.locator('.drawer .catchup').waitFor() }
      const side = width === 320 ? '.drawer' : SIDE
      check(`${width} px: a wait with no ask offers "Answer one by one"`, await page.locator(`${side} .catchup-row`, { hasText: 'BriskBot' }).count() === 1 && await page.locator(`${side} [aria-label="Answer one by one"]`).count() === 1)
      await page.locator(`${side} [aria-label="Answer one by one"]`).click()
      const deck = page.locator('dialog.action-deck[open]')
      await deck.waitFor()
      check(`${width} px: the deck card names the tab and the prompt`, (await deck.locator('.deck-tab').textContent()).includes('BriskBot') && (await deck.locator('.deck-question').textContent()) === 'A permission prompt waits in this tab. Open the tab to answer it.' && await deck.locator('.deck-hint').count() === 0 && (await deck.locator('.deck-meta').textContent()).includes('Needs approval'))
      check(`${width} px: the card has Open tab and no Handled or reply box`, await deck.getByRole('button', { name: 'Open tab' }).count() === 1 && await deck.getByRole('button', { name: 'Handled' }).count() === 0 && await deck.locator('textarea').count() === 0)
      check(`${width} px: the deck fits`, await deck.evaluate(box => box.scrollWidth <= box.clientWidth + 1 && document.documentElement.scrollWidth <= innerWidth))
      await page.screenshot({ path: path.join(SHOTS, `${width}-deck-state-only.png`), animations: 'disabled' })
      await deck.getByRole('button', { name: 'Open tab' }).click()
      await deck.waitFor({ state: 'detached' })
      check(`${width} px: Open tab closes the deck and opens the tab`, await page.evaluate(() => location.hash + location.search).then(where => where.includes('%10') || where.includes('%2510')) || (await page.locator('[aria-current="page"]', { hasText: 'BriskBot' }).count()) > 0)
    }

    // Review fixes: an archived tab gets no card; an undated wait sorts first and shows no age; Escape gives focus back.
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}),
      pane('%10', '$2', 'BriskBot', 'briskbot', { state: 'Approval', since: NOW - 60 }),
      pane('%11', '$2', 'Old helper', 'briskbot:0.2', { state: 'Approval', since: NOW - 30 }),
      pane('%12', '$2', 'Undated', 'briskbot:0.3', { state: 'Approval', since: null })], default_thread: 't3' }
    await open('%3', 1280, 800, { sideMode: 'catchup', attention: true, archivedTabs: { '%11': { dir: '/srv/project', thread: 't11', name: 'Old helper' } } })
    const deckRows = await page.locator(`${SIDE} .catchup-row`).count()
    const opener = page.locator(`${SIDE} [aria-label="Answer one by one"]`)
    await opener.focus()
    await page.keyboard.press('Enter')
    const stateDeck = page.locator('dialog.action-deck[open]')
    await stateDeck.waitFor()
    check('the deck has one card for each Catch up row, no archived tab', (await stateDeck.locator('.deck-count').textContent()) === `1 of ${deckRows}` && deckRows === 2, `${deckRows} rows, ${await stateDeck.locator('.deck-count').textContent()}`)
    check('an undated wait comes first and shows no age', (await stateDeck.locator('.deck-tab').textContent()).includes('Undated') && !(await stateDeck.locator('.deck-meta').textContent()).includes('waited'))
    await page.keyboard.press('Escape')
    await stateDeck.waitFor({ state: 'detached' })
    const refocused = await opener.evaluate(node => new Promise(done => { const end = Date.now() + 3000; const poll = () => document.activeElement === node ? done(true) : Date.now() > end ? done(false) : requestAnimationFrame(poll); poll() }))
    check('Escape gives focus back to "Answer one by one"', refocused)

    // An urgent ask in an archived tab does not open the deck by itself.
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%13', '$2', 'Hidden urgent', 'briskbot:0.4', { state: 'Approval', since: NOW - 60, ask: ask('%13', 60, { kind: 'approval', urgent: true }) })], default_thread: 't3' }
    // Control: the same urgent ask in a visible tab opens it, so the check below can see a deck that opened.
    await open('%3', 1280, 800, { sideMode: 'catchup', attention: true }, { keepDeck: true })
    check('a visible urgent ask opens the deck by itself', await page.locator('dialog.action-deck[open]').count() === 1)
    await open('%3', 1280, 800, { sideMode: 'catchup', attention: true, archivedTabs: { '%13': { dir: '/srv/project', thread: 't13', name: 'Hidden urgent' } } }, { keepDeck: true })
    await page.locator(`${SIDE} .catchup`).waitFor()
    check('an archived urgent ask does not open the deck', await page.locator('dialog.action-deck[open]').count() === 0)

    // Handled ends the ask's card; when the tab still waits by its state, its state card shows next.
    let handledAsk = false
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%10', '$2', 'BriskBot', 'briskbot', { state: 'Approval', since: NOW - 60, ask: ask('%10', 60, { kind: 'approval' }) })], default_thread: 't3' }
    await open('%3', 1280, 800, { sideMode: 'catchup', attention: true })
    await page.locator(`${SIDE} [aria-label="Answer one by one"]`).click()
    await stateDeck.waitFor()
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%10', '$2', 'BriskBot', 'briskbot', { state: 'Approval', since: NOW - 60 })], default_thread: 't3' }
    await stateDeck.getByRole('button', { name: 'Handled' }).click()
    handledAsk = await stateDeck.locator('.deck-question', { hasText: 'A permission prompt waits in this tab.' }).waitFor({ timeout: 5000 }).then(() => true, () => false)
    check('after Handled, the state card of the same tab shows', handledAsk && await stateDeck.getByRole('button', { name: 'Handled' }).count() === 0)
    await page.keyboard.press('Escape')
    sessions = busy

    // 7. Phone, 320 px: the drawer shows both views with no sideways scroll.
    await open('%3', 320, 700, { sideMode: 'catchup' })
    await page.locator('.mobile-menu').locator('visible=true').first().click()
    await page.locator('.drawer .catchup').waitFor()
    const fits = () => page.evaluate(() => { const box = document.querySelector('.drawer .sidebar-inner'); return box.scrollWidth <= box.clientWidth + 1 && document.documentElement.scrollWidth <= innerWidth })
    check('320 px: Catch up fits', await fits())
    await page.screenshot({ path: path.join(SHOTS, '320-catch-up.png') })
    await page.locator('.drawer .side-mode-switch button', { hasText: 'Sessions' }).click()
    await page.locator('.drawer .side-active').click()
    check('320 px: Sessions with Active only fits', await fits())
    await page.screenshot({ path: path.join(SHOTS, '320-sessions.png') })

    // 8. A narrow sidebar with a two-digit count: the switch, Collapse all and Active only never overlap or clip.
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), ...Array.from({ length: 30 }, (_, i) => pane(`%${100 + i}`, '$1', `Ask ${i}`, `tether:1.${i}`, { ask: ask(`%${100 + i}`, 60 + i) }))], default_thread: 't3' }
    for (const width of [180, 210, 248, 320]) {
      await open('%3', 1280, 800, { sideMode: 'sessions' })
      await page.evaluate(w => document.querySelector('.app-shell').style.setProperty('--lw-sessions', `${w}px`), width)
      await page.locator(`${SIDE} .side-fold`).waitFor()
      const bad = await page.evaluate(() => {
        const row = document.querySelector('.sidebar .side-modes'), side = document.querySelector('.sidebar').getBoundingClientRect()
        const parts = [...row.querySelectorAll('.side-mode-switch button, .side-fold, .side-active')]
        const out = []
        for (const el of parts) { const r = el.getBoundingClientRect(); if (el.scrollWidth > el.clientWidth + 1) out.push(`${el.textContent || el.className} clips`); if (r.left < side.left || r.right > side.right) out.push(`${el.textContent || el.className} leaves the sidebar`) }
        parts.forEach((a, i) => parts.slice(i + 1).forEach(b => { const x = a.getBoundingClientRect(), y = b.getBoundingClientRect(); if (Math.min(x.right, y.right) - Math.max(x.left, y.left) > 0.5 && Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top) > 0.5) out.push(`${a.textContent || a.className} overlaps ${b.textContent || b.className}`) }))
        return out
      })
      check(`${width} px sidebar: the mode row fits with a two-digit count`, bad.length === 0, bad.join('; '))
      await page.screenshot({ path: path.join(SHOTS, `side-modes-${width}.png`), clip: { x: 0, y: 0, width: Math.min(width + 80, 1280), height: 260 } })
    }
    sessions = busy

    // A snooze that ends: Catch up and the deck read one clock, so both show the tab after the same tick (320 px).
    // It runs last: the fake clock stays on this page.
    await page.clock.install({ time: NOW * 1000 })
    sessions = { sessions: [pane('%3', '$1', 'Sidebar build', 'tether', {}), pane('%14', '$2', 'Snoozed tab', 'briskbot:0.5', { state: 'Approval', since: NOW - 60, ask: ask('%14', 60, { kind: 'approval', snoozed_until: NOW + 20 }) })], default_thread: 't3' }
    await open('%3', 320, 700, { sideMode: 'catchup', attention: true })
    // The snooze ends 20 s in; the app clock next ticks 30 s in. A drawer opened at 25 s must not read a newer clock
    // than the deck: it shows no row until the tick, and then both show the tab.
    await page.clock.runFor(25_000)
    await page.locator('.mobile-menu').locator('visible=true').first().click()
    await page.locator('.drawer .catchup').waitFor()
    check('320 px: before the tick, Catch up shows no row and no deck for the snoozed tab', await page.locator('.drawer .catchup-row', { hasText: 'Snoozed tab' }).count() === 0 && await page.locator('.drawer [aria-label="Answer one by one"]').count() === 0)
    await page.clock.runFor(10_000)  // the app clock ticks, past the snooze end
    await page.locator('.drawer .catchup-row', { hasText: 'Snoozed tab' }).waitFor()
    await page.locator('.drawer [aria-label="Answer one by one"]').click()
    const snoozeDeck = page.locator('dialog.action-deck[open]')
    await snoozeDeck.waitFor()
    check('320 px: after the snooze ends, Catch up and the deck agree', (await snoozeDeck.locator('.deck-count').textContent()) === '1 of 1' && (await snoozeDeck.locator('.deck-tab').textContent()).includes('Snoozed tab'))
    await page.keyboard.press('Escape')

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
  console.log('test_sidebar_views_ui: ok')
}
main()
