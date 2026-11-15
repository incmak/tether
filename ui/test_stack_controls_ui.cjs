// Stack controls: drag a stack to reorder it, Move left in the stack menu, Suggest stacks with tabs that go to
// Unstacked, and the New stack popover with its folder picker. Every API request stays in this page's fixtures.
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/create-popovers/shots')
const pane = (id, name, group) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...(group ? { group } : {}) })
const stacks = [{ id: 'g00000001', name: 'Alpha', project: '' }, { id: 'g00000002', name: 'Beta', project: '' }, { id: 'g00000003', name: 'Gamma', project: '' }]
const fixture = {
  sessions: { sessions: [pane('%1', 'Loose tab'), pane('%2', 'Login fix', 'g00000001'), pane('%3', 'Login review', 'g00000002'), pane('%4', 'Docs pass', 'g00000003')], default_thread: 't2', groups: { $1: stacks } },
  chat: { messages: [], busy: false, thread: 't2', agent: 'claude', writable: true, session: '%2', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%2', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'files/places': { pinned: [], sessions: [{ session: '%2', cwd: '/srv/project' }, { session: '%3', cwd: '/srv/api' }], disk: null },
  'files/list': { path: '/srv', entries: [{ name: 'project', dir: true, size: null, modified: null, kind: 'folder' }, { name: 'api', dir: true, size: null, modified: null, kind: 'folder' }, { name: 'notes.txt', dir: false, size: 3, modified: null, kind: 'text' }], truncated: false },
  'suggest-groups': { groups: [{ name: 'Login', sessions: ['%2', '%3'] }], unstacked: ['%1', '%4'], source: 'llm' },
  'suggest-name': { names: ['Login work', 'Auth'], source: 'llm' }, 'files/mkdir': { name: 'fresh' },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  'groups/reorder': { groups: stacks }, 'groups/arrange': { groups: [stacks[0]] },
}

async function main() {
  let browser, page, failed = 0
  const pageErrors = [], posts = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  const posted = endpoint => posts.filter(item => item.endpoint === endpoint).map(item => item.body)
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    const origin = 'http://tether.test'
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('console', message => { if (message.type() === 'error' && process.env.DEBUG) console.log('console:', message.text().slice(0, 1500)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (route.request().method() === 'POST') posts.push({ endpoint, body: route.request().postDataJSON() })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'files/list') return route.fulfill({ json: { ...fixture['files/list'], path: url.searchParams.get('path') || '/srv' } })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        // The shared browser may not reach this sandbox's ports, so the page routes serve the build.
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try { return route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false, mainModel: 'none' }))
    })
    const stack = name => page.locator('.group-row .group-tab', { hasText: name }).first()
    const noSideScroll = () => page.evaluate(() => document.scrollingElement.scrollWidth <= innerWidth + 1)

    // Wide screen: the stack row.
    await page.setViewportSize({ width: 1280, height: 860 })
    await page.goto(`${origin}/chat/?session=%252`, { waitUntil: 'load' })
    await stack('Gamma').waitFor({ state: 'visible' }).catch(async error => { console.log((await page.locator('body').innerText()).slice(0, 600)); console.log(pageErrors.join('\n')); throw error })
    check('1280: the stack row shows Unstacked and three stacks', await page.locator('.group-row .group-tab').count() === 4)
    check('1280: stacks can be dragged, Unstacked cannot', await stack('Gamma').getAttribute('draggable') === 'true' && await stack('Unstacked').getAttribute('draggable') === 'false')
    const alpha = await stack('Alpha').boundingBox()
    // HTML5 drag events with one DataTransfer, sent in the order Chromium sends them. Native mouse drags do not
    // reach the page in every shared-browser mode, so the test sends the events the browser would send.
    await page.evaluate(({ x, y }) => {
      const tabs = [...document.querySelectorAll('.group-row .group-tab')]
      const from = tabs.find(tab => tab.textContent.includes('Gamma')), to = tabs.find(tab => tab.textContent.includes('Alpha'))
      const data = new DataTransfer()
      const send = (target, type, at) => target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: data, clientX: at.x, clientY: at.y }))
      const box = from.getBoundingClientRect()
      send(from, 'dragstart', { x: box.x + box.width / 2, y: box.y + box.height / 2 })
      return new Promise(resolve => requestAnimationFrame(() => {
        send(to, 'dragenter', { x, y }); send(to, 'dragover', { x, y })
        requestAnimationFrame(() => { send(to, 'drop', { x, y }); requestAnimationFrame(() => { send(from, 'dragend', { x, y }); resolve() }) })
      }))
    }, { x: alpha.x + 4, y: alpha.y + alpha.height / 2 })
    await page.waitForTimeout(300)
    check('1280: a drag saves the new stack order', JSON.stringify(posted('groups/reorder').at(-1)) === JSON.stringify({ session: '$1', groups: ['g00000003', 'g00000001', 'g00000002'] }), JSON.stringify(posted('groups/reorder')))
    const shown = await page.locator('.group-row .group-tab .group-name').allTextContents()
    check('1280: the row shows the new order at once', shown.join() === 'Unstacked,Gamma,Alpha,Beta', shown.join())

    // The stack menu: a one-tap reorder (WCAG 2.5.7) and a new tab in the stack.
    await stack('Beta').click({ button: 'right' })
    const menu = page.locator('dialog[open]', { has: page.locator('#group-menu-title') })
    await menu.waitFor({ state: 'visible' })
    check('1280: the stack menu has New tab here and Move left/right', await menu.getByRole('button', { name: 'New tab here' }).isVisible() && await menu.getByRole('button', { name: 'Move right' }).isDisabled())
    await page.screenshot({ path: path.join(SHOTS, 'stack-menu-1280.png') })
    await menu.getByRole('button', { name: 'Move left' }).click()
    await page.waitForTimeout(200)
    check('1280: Move left starts from the order the row shows', JSON.stringify(posted('groups/reorder').at(-1)?.groups) === JSON.stringify(['g00000003', 'g00000002', 'g00000001']), JSON.stringify(posted('groups/reorder').at(-1)))

    // New stack: a popover with a name, a folder picker and a first task.
    await page.locator('.group-row .group-add').click()
    const create = page.locator('dialog[open]').last()
    await create.getByLabel('Stack name').waitFor({ state: 'visible' })
    const box = await create.boundingBox()
    check('1280: New stack opens as a popover inside the window', box && box.width < 600 && box.x >= 0 && box.x + box.width <= 1280, JSON.stringify(box))
    await create.locator('.create-folder-row').click()
    await create.locator('.create-folders button', { hasText: 'api' }).waitFor({ state: 'visible' })
    check('1280: the folder picker lists folders only', await create.locator('.create-folders li').count() === 2)
    check('1280: the folder picker shows recent folders', await create.locator('.create-recent button').count() >= 1)
    await page.screenshot({ path: path.join(SHOTS, 'new-stack-folder-1280.png') })
    await create.locator('.create-folders button', { hasText: 'api' }).click()
    await create.getByRole('button', { name: /^Use / }).click()
    check('1280: the chosen folder shows in the row', (await create.locator('.create-folder-path').textContent()).endsWith('/api'))
    // New folder sits inside the New stack form: Enter makes the folder and does not submit New stack.
    await create.getByLabel('Stack name').fill('Fresh work')
    const before = posts.length
    await create.locator('.create-folder-row').click()
    await create.getByRole('button', { name: 'New folder' }).click()
    await create.getByLabel('New folder name').fill('fresh')
    await create.getByLabel('New folder name').press('Enter')
    await create.locator('.create-crumbs [aria-current="location"]', { hasText: 'fresh' }).waitFor({ state: 'visible' })
    const sent = posts.slice(before).map(item => item.endpoint)
    check('1280: Enter in New folder makes the folder only', sent.join() === 'files/mkdir' && await create.isVisible(), sent.join())
    await page.keyboard.press('Escape')
    await page.keyboard.press('Escape')

    // Two stacks and no loose tab: the stacks can still be dragged.
    const full = fixture.sessions
    fixture.sessions = { ...full, sessions: full.sessions.filter(item => item.group && item.group !== 'g00000003'), groups: { $1: stacks.slice(0, 2) } }
    await page.goto(`${origin}/chat/?session=%252`, { waitUntil: 'load' })
    await stack('Beta').waitFor({ state: 'visible' })
    check('1280: two stacks with an empty Unstacked can be dragged', await stack('Alpha').getAttribute('draggable') === 'true' && await stack('Beta').getAttribute('draggable') === 'true')
    fixture.sessions = full

    // Phone: the stack sheet, Suggest stacks with tabs that go back to Unstacked.
    for (const width of [390, 320]) {
      posts.length = 0
      await page.setViewportSize({ width, height: 760 })
      await page.goto(`${origin}/chat/?session=%252`, { waitUntil: 'load' })
      await page.locator('.group-switch').click()
      const sheet = page.locator('dialog[open]', { has: page.locator('#group-switch-title') })
      await sheet.getByRole('button', { name: 'Suggest stacks…' }).click()
      await sheet.getByRole('button', { name: 'Suggest stacks', exact: true }).click()
      await sheet.getByRole('button', { name: 'Apply stacks' }).waitFor({ state: 'visible' })
      const rows = await sheet.locator('.suggest-list li').allTextContents()
      check(`${width}: the suggestion lists the stack and the tabs that go to Unstacked`, rows.length === 2 && rows[0].startsWith('Login') && rows[1].startsWith('Unstacked') && rows[1].includes('Docs pass') && !rows[1].includes('Loose tab'), rows.join(' | '))
      check(`${width}: no side scroll with the sheet open`, await noSideScroll())
      await page.screenshot({ path: path.join(SHOTS, `suggest-stacks-${width}.png`) })
      await sheet.getByRole('button', { name: 'Apply stacks' }).click()
      await page.waitForTimeout(200)
      const body = posted('groups/arrange').at(-1)
      check(`${width}: Apply sends the stacks and the tabs for Unstacked`, JSON.stringify(body) === JSON.stringify({ session: '$1', stacks: [{ name: 'Login', panes: ['%2', '%3'] }], unstack: ['%4'] }), JSON.stringify(body))
      check(`${width}: the suggestion went to the tabs kind`, posted('suggest-groups').at(-1)?.kind === 'tabs')
    }
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
  } finally {
    await page?.close().catch(() => {})
    if (browser && !process.env.TETHER_CDP_URL) await browser.close().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'Stack controls: all pass')
  process.exit(failed ? 1 : 0)
}

main()
