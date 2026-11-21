// The Factory view (src/Factory.tsx) against a build, with fixture data only. It checks the keyboard zoom path
// (arrows, Enter, Esc, Backspace), the breadcrumb, wheel and touch zoom, a token that moves phase, the live region, the
// loading, empty and error states, and no sideways scroll from 320 px. With SHOTS set (or by default) it saves
// screenshots of all four levels at 390, 1280, 1920 and 3840 px wide, light and dark.
// Run: cd ui && npm run test:ui (or DIST=<build> node test_factory_ui.cjs).
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || ''
const ORIGIN = 'http://localhost:7811'
const NOW = Date.now()
const iso = minutes => new Date(NOW - minutes * 60000).toISOString()
const tool = (name, title, minutes) => ({ name, title, at: iso(minutes) })
const pane = (id, name, extra = {}) => ({ id, tmux_session: '$1', label: `tether:${id}`, pane_name: name, agent: 'claude', dir: '/srv/tether', state: 'Idle', writable: true, thread: id, approval: null, ...extra })

const panes = [
  pane('%1', 'Automation', { state: 'Working' }),
  pane('%2', 'Factory View', { state: 'Working', reviewers: ['%3'] }),
  pane('%3', 'Factory review', { agent: 'codex', state: 'Idle' }),
  pane('%4', 'B71 Part 2', { state: 'Working' }),
  pane('%5', 'Dev servers', { state: 'Approval', needs: { kind: 'approval', text: 'Allow: kill 4123?' } }),
  pane('%6', 'BriskBot people', { dir: '/srv/briskbot' }),
  pane('%7', 'Docs pass', { agent: 'codex', state: 'Working' }),
  pane('%8', 'Release notes', { finished: NOW / 1000 - 3 * 3600 }),
  pane('%9', 'Login fix', { agent: 'antigravity', state: 'Working' }),
  pane('%11', 'Factory helper', { agent: 'codex', parent: '%2' }),
  pane('%12', 'Factory advisor', { role: 'advisor', parent: '%11' }),
]
const backlog = [
  '## tether',
  '- [ ] Factory view (claude · pane %1) #B191',
  '  - Doing: claude · pane %1',
  '  - Worker tab: Factory View.',
  '- [ ] Unified task list, part 2 #B71',
  '  - Doing: claude · pane %4',
  '- [ ] Dev servers manager #B190',
  '  - Doing: claude · pane %5',
  '- [ ] Touch pinch zoom on the board #B192',
  '- [ ] Export the backlog as CSV #B193',
  '- [ ] Quiet hours for notifications #B194',
  '- [x] Launch: pass project folders as additionalDirectories #B188',
  `  - Done: ${new Date(NOW - 2 * 3600000).toISOString().slice(0, 16).replace('T', ' ')} · 5d59421 deployed`,
  '## briskbot',
  '- [ ] BriskBot per person #B168',
  '  - Doing: claude · pane %6',
].join('\n')
const factoryData = (moved = false) => ({
  now: NOW / 1000,
  tools: {
    '%1': [tool('Bash', 'agent_tab_cli.py wait --pane %2', 2)],
    '%2': [tool('Read', 'src/Factory.tsx', 9), tool('Edit', 'src/Factory.tsx', 6), tool('Bash', 'cd ui && npx tsc -b', 1)],
    '%3': [tool('Read', 'src/factoryModel.ts', 50)],
    '%4': moved ? [tool('Edit', 'src/Tasks.tsx', 3), tool('Bash', 'python3 scripts/deploy-ui.py /tmp/b ui/dist', 0)] : [tool('Read', 'src/Tasks.tsx', 8), tool('Edit', 'src/Tasks.tsx', 3)],
    '%5': [tool('Bash', 'kill 4123', 1)],
    '%6': [tool('Edit', 'bot/people.py', 95)],
    '%7': [tool('Read', 'README.md', 4), tool('Grep', 'deploy', 3)],
    '%8': [tool('Edit', 'RELEASES.md', 190)],
  },
  deployed: [{ at: Math.round(NOW / 1000 - 2 * 3600), timed: true, commit: '5d59421', text: '5d59421 B188 new Claude tabs no longer stop', refs: ['B188'] }],
  sweep: { at: Math.round(NOW / 1000 - 12 * 60), every: 1800, lines: [
    { pane: '%4', name: 'B71 Part 2', branch: 'b71-part2', kind: 'behind', text: '6 commits behind main. Rebase by hand.' },
    { pane: '%4', name: 'B71 Part 2', branch: 'b71-part2', kind: 'checks', text: '2 checks fail on 4c1e9a0: test, lint.' },
    { pane: '%8', name: 'Release notes', branch: 'release-notes-with-a-long-branch-name-that-must-wrap', kind: 'idle', text: 'Idle for 3 h.' },
  ] },
  commits: [
    { hash: '5d59421', at: Math.round(NOW / 1000 - 2.2 * 3600), subject: 'Launch: pass project folders as additionalDirectories (B188)', refs: ['B188'], repo: 'tether' },
    { hash: '4c1e9a0', at: Math.round(NOW / 1000 - 40 * 60), subject: 'Tasks: one list for agent tasks and backlog (B71)', refs: ['B71'], repo: 'tether' },
  ],
})
const screenText = 'Tasks.tsx  ▸ Edit  src/Tasks.tsx\n\n  ✓ 14 tests pass\n\n⏺ Next: run the deploy check.\n\n────────────────────────────\n> \n────────────────────────────\n  ⏵⏵ accept edits on · 31% context'
const base = {
  chat: { messages: [], busy: false, thread: '%1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [], context: { percent: 31 } },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'side-shells': { shells: [] }, update: { current: '0.0.0', latest: '0.0.0', available: false },
  host: { name: 'Studio', office: false }, catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null }, 'work/board': { projects: [], items: [] },
}
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const touchViewports = [{ width: 320, height: 640 }, { width: 768, height: 1024 }]
const readCrumbs = page => page.locator('.factory-crumbs li').allInnerTexts()
const zoomSettled = page => page.waitForFunction(() => !document.documentElement.matches(':active-view-transition'))

async function midpoint(item) {
  await item.scrollIntoViewIfNeeded()
  const { x, y, width, height } = await item.boundingBox()
  return { x: Math.round(x + width / 2), y: Math.round(y + height / 2) }
}

async function dragTouches(cdp, paths) {
  const dispatch = (type, touchPoints) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints })
  const points = fraction => paths.map(([from, to], id) => ({
    id,
    x: from.x + (to.x - from.x) * fraction,
    y: from.y + (to.y - from.y) * fraction,
  }))
  await dispatch('touchStart', points(0))
  for (let step = 1; step <= 6; step++) await dispatch('touchMove', points(step / 6))
  await dispatch('touchEnd', [])
}

async function main() {
  let browser, page, failed = 0
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  // What the fake server answers. Tests change these between steps.
  const history = Object.fromEntries(Object.entries({
    B191: 'verify', B190: 'build', B192: 'inbox', B193: 'inbox', B194: 'inbox', B188: 'done', B168: 'build',
    '%7': 'spec', '%8': 'done', '%9': 'build',
  }).map(([id, phase]) => [id, [{ phase, at: NOW - 60000 }]]))
  history.B71 = [{ phase: 'inbox', at: NOW - 26 * 3600000 }, { phase: 'build', at: NOW - 2 * 3600000 }]
  const state = { panes, moved: false, factory: 'ok', factoryPoll: 0, rejectHistory: false, reports: [], history }
  const reportsDeploy = body => body.moves.some(move => move.task === 'B71' && move.phase === 'deploy')
  const deployReports = () => state.reports.filter(({ body }) => reportsDeploy(body))
  let sessionGate, historyRetryGate, factoryGate
  const setupPage = async page => {
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    await page.emulateMedia({ reducedMotion: 'no-preference', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== ORIGIN) return route.abort('blockedbyclient')
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: { selected: '%1' }, version: 1 } })
        if (endpoint === 'factory/history' && request.method() === 'POST') {
          const body = request.postDataJSON()
          state.reports.push({ body, poll: state.factoryPoll, header: request.headers()['x-codex-mobile'] })
          if (reportsDeploy(body)) {
            if (state.rejectHistory) {
              state.rejectHistory = false
              return route.fulfill({ status: 503, json: { error: 'Phase history is unavailable' } })
            }
            await historyRetryGate
          }
          for (const { task, phase } of body.moves) {
            const entries = state.history[task] ??= []
            if (entries.at(-1)?.phase !== phase) entries.push({ phase, at: NOW })
          }
          return route.fulfill({ json: { ok: true } })
        }
        if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
        if (endpoint === 'sessions') {
          await sessionGate
          return route.fulfill({ json: { sessions: state.panes, default_thread: '%1' } })
        }
        if (endpoint === 'factory') {
          await factoryGate
          if (state.factory === 'down') return route.fulfill({ status: 502, json: { error: 'Bad gateway' } })
          state.factoryPoll++
          const facts = state.factory === 'empty' ? { now: NOW / 1000, tools: {}, deployed: [], commits: [] } : factoryData(state.moved)
          return route.fulfill({ json: { ...facts, history: state.history } })
        }
        if (endpoint === 'projects/backlog') return route.fulfill({ json: { text: state.factory === 'empty' ? '' : backlog } })
        if (endpoint === 'screen') return route.fulfill({ json: { text: screenText, session: url.searchParams.get('session'), agent: 'claude' } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: base.chat })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: base[endpoint] || {} })
      }
      if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404, body: '' })
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
      try { return route.fulfill({ body: await fs.readFile(file), contentType: mime[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
    })
    // Old browser history conflicts with the server history and should be removed on first open.
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      if (sessionStorage.getItem('factory-test-seeded')) return
      sessionStorage.setItem('factory-test-seeded', '1')
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
      localStorage.setItem('tether-factory-history', JSON.stringify({ B71: [{ phase: 'done', at: Date.now() }] }))
    })
  }
  try {
    if (SHOTS) await fs.mkdir(SHOTS, { recursive: true })
    // The shared browser proxy does not allow per-page CDP sessions.
    browser = await getBrowser({ local: true })
    page = await browser.contexts()[0].newPage()
    await setupPage(page)

    const view = page.locator('dialog.factory')
    const crumbs = () => readCrumbs(page)
    const focused = () => page.evaluate(() => { const el = document.activeElement; return el ? (el.getAttribute('data-key') || el.textContent.trim().slice(0, 40)) : '' })
    const station = phase => page.locator(`.factory-station.phase-${phase}`)
    const overflow = () => page.evaluate(() => { const body = document.querySelector('.factory-body'); return document.documentElement.scrollWidth > innerWidth || (body && body.scrollWidth > body.clientWidth + 1) })

    // Loading, then the floor from the command URL.
    await page.setViewportSize({ width: 1280, height: 800 })
    let showFactory
    factoryGate = new Promise(resolve => { showFactory = resolve })
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await view.locator('.factory-loading').waitFor()
    check('loading state shows', await view.locator('.factory-loading').isVisible())
    showFactory()
    await station('build').waitFor()
    check('seven stations, left to right', JSON.stringify(await page.locator('.factory-station-name').allInnerTexts()) === JSON.stringify(['Inbox', 'Spec', 'Build', 'Review', 'Verify', 'Deploy', 'Done']), JSON.stringify(await page.locator('.factory-station-name').allInnerTexts()))
    check('B191 is in Verify: its worker last ran tsc', (await station('verify').innerText()).includes('B191'), await station('verify').innerText())
    check('B190 waits for you: word and shape', (await station('build').locator('.factory-token.is-waits').innerText()).includes('Waits for you'))
    check('B168 is stuck: word and dashed border', await page.evaluate(() => { const token = [...document.querySelectorAll('.factory-token.is-stuck')].find(el => el.textContent.includes('B168')); return !!token && getComputedStyle(token).borderStyle === 'dashed' && /Stuck/.test(token.textContent) }))
    check('B188 is Done', (await station('done').innerText()).includes('B188'))
    check('counters', /1\s*wait for you/.test(await page.locator('.factory-counts').innerText()) && /1\s*stuck/.test(await page.locator('.factory-counts').innerText()), await page.locator('.factory-counts').innerText())
    check('breadcrumb on the floor', JSON.stringify(await crumbs()) === '["Factory"]')
    check('focus starts on the first station', await focused() === 'phase:inbox', await focused())

    // Keyboard: arrows move to the nearest item; Enter zooms in; Backspace and Esc zoom out to where we were.
    await page.keyboard.press('ArrowRight'); await page.keyboard.press('ArrowRight')
    check('ArrowRight moves to the next station', await focused() === 'phase:build', await focused())
    await page.keyboard.press('ArrowDown')
    const token = await focused()
    check('ArrowDown moves into the Build tokens', token.startsWith('task:'), token)
    await page.keyboard.press('ArrowUp')
    check('ArrowUp goes back to the station head', await focused() === 'phase:build', await focused())
    await page.keyboard.press('Enter')
    await page.locator('.factory-phase').waitFor()
    check('Enter on a station zooms to the phase', JSON.stringify(await crumbs()) === '["Factory","Build"]', JSON.stringify(await crumbs()))
    check('focus goes to the first task of the phase', (await focused()).startsWith('task:'), await focused())
    await page.keyboard.press('Escape')
    await page.locator('.factory-floor').waitFor()
    check('Esc zooms out to the floor, focus on the station', await focused() === 'phase:build', await focused())
    check('Esc does not close the dialog', await view.evaluate(box => box.open))

    await page.locator('[data-key="task:B71"]').focus()
    await page.keyboard.press('Enter')
    await page.locator('.factory-task').waitFor()
    check('Enter on a token zooms to the task', JSON.stringify(await crumbs()) === '["Factory","Build","B71"]', JSON.stringify(await crumbs()))
    check('task view says why the phase is a guess', /Phase guessed from:/.test(await page.locator('.factory-why').innerText()))
    const timeline = page.locator('.factory-timeline')
    check('task view shows the server timeline', JSON.stringify(await timeline.locator('b').allInnerTexts()) === '["Inbox","Build"]')
    check('the old browser history key is removed', await page.evaluate(() => localStorage.getItem('tether-factory-history')) === null)
    check('task view shows the commit', (await page.locator('.factory-task').innerText()).includes('4c1e9a0'))
    await page.keyboard.press('Enter')
    await page.locator('.factory-agent').waitFor()
    check('Enter on an agent zooms to the agent', JSON.stringify(await crumbs()) === '["Factory","Build","B71","B71 Part 2"]', JSON.stringify(await crumbs()))
    await page.locator('.factory-screen').waitFor()
    check('agent view shows the live screen read-only', (await page.locator('.factory-screen').innerText()).includes('14 tests pass') && !(await page.locator('.factory-agent textarea, .factory-agent input').count()))
    check('agent view shows context %', (await page.locator('.factory-facts').innerText()).includes('31% used'))
    check('agent view lists recent steps', (await page.locator('.factory-agent .factory-list').innerText()).includes('src/Tasks.tsx'))
    check('Open tab button is there', await page.getByRole('button', { name: 'Open tab' }).isVisible())
    {
      // WCAG 1.4.3: the Open tab label keeps its own colour on the accent fill (the base button rule must not win).
      const ratio = await page.getByRole('button', { name: 'Open tab' }).evaluate(el => {
        const rgb = value => value.match(/[\d.]+/g).slice(0, 3).map(Number)
        const lum = ([r, g, b]) => [r, g, b].map(c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0)
        const style = getComputedStyle(el); const a = lum(rgb(style.color)), b = lum(rgb(style.backgroundColor))
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
      })
      check('Open tab text contrast is at least 4.5:1', ratio >= 4.5, String(ratio))
      // TV remote: arrows reach the Tour button in the bar.
      await page.getByRole('button', { name: 'Open tab' }).focus()
      for (let step = 0; step < 4; step++) await page.keyboard.press('ArrowUp')
      for (let step = 0; step < 8; step++) await page.keyboard.press('ArrowRight')
      check('arrows reach the Tour button', await page.evaluate(() => document.activeElement?.classList.contains('factory-tour')), await page.evaluate(() => document.activeElement?.className))
      await page.getByRole('button', { name: 'Open tab' }).focus()
    }
    await page.keyboard.press('Backspace')
    await page.locator('.factory-task').waitFor()
    check('Backspace zooms out one level, focus on the agent', await focused() === 'agent:%4', await focused())
    await page.locator('.factory-crumbs button', { hasText: 'Factory' }).click()
    await page.locator('.factory-floor').waitFor()
    check('a breadcrumb button zooms out to that level', JSON.stringify(await crumbs()) === '["Factory"]')

    // Wheel: ctrl+wheel up on a token zooms in, down zooms out.
    await zoomSettled(page)  // during the zoom's view transition, the pointer hits the root
    const box = await page.locator('[data-key="task:B190"]').boundingBox()
    await page.mouse.move(box.x + 20, box.y + 10)
    await page.keyboard.down('Control'); await page.mouse.wheel(0, -100); await page.keyboard.up('Control')
    await page.locator('.factory-task').waitFor()
    const zoomedAt = await page.evaluate(() => Date.now())
    check('pinch or ctrl+wheel zooms in on the item under the pointer', JSON.stringify(await crumbs()) === '["Factory","Build","B190"]', JSON.stringify(await crumbs()))
    await zoomSettled(page)
    // Factory.tsx drops a wheel within 450 ms of the last one, by the page clock.
    await page.waitForFunction(at => Date.now() - at >= 450, zoomedAt)
    await page.keyboard.down('Control'); await page.mouse.wheel(0, 100); await page.keyboard.up('Control')
    await page.locator('.factory-phase').waitFor()
    check('ctrl+wheel down zooms out one level, to the phase', JSON.stringify(await crumbs()) === '["Factory","Build"]', JSON.stringify(await crumbs()))
    await page.keyboard.press('Escape')
    await page.locator('.factory-floor').waitFor()

    // Touch pinch at phone and tablet sizes.
    {
      const viewport = page.viewportSize()
      const pointers = (center, steps, pointerType = 'touch') => page.evaluate(({ center, steps, pointerType }) => {
        for (const [type, offsets] of steps) {
          offsets.forEach((offset, index) => {
            if (offset === null) return
            const clientX = center.x + offset, clientY = center.y
            const target = document.elementFromPoint(clientX, clientY)
            if (!target?.closest('.factory')) throw new Error(`Touch point outside Factory: ${clientX}, ${clientY}`)
            target.dispatchEvent(new PointerEvent(`pointer${type}`, {
              bubbles: true, pointerType, pointerId: 11 + index, isPrimary: index === 0, clientX, clientY,
            }))
          })
        }
      }, { center, steps, pointerType })
      const zoom = async (center, start, finish, pointerType) => {
        await pointers(center, [['down', start], ['move', finish], ['up', finish]], pointerType)
        await zoomSettled(page)
      }

      for (const { width, height } of touchViewports) {
        await zoomSettled(page)
        await page.setViewportSize({ width, height })
        const center = await midpoint(page.locator('[data-key="task:B190"]'))
        check(`${width}: Factory allows one-finger pan on both axes`, await view.evaluate(el => getComputedStyle(el).touchAction) === 'pan-x pan-y')
        const prevented = await view.evaluate(el => {
          const touches = [11, 12].map(identifier => new Touch({ identifier, target: el }))
          return [touches, touches.slice(0, 1)].map(points => {
            const event = new TouchEvent('touchmove', { cancelable: true, touches: points })
            el.dispatchEvent(event)
            return event.defaultPrevented
          })
        })
        check(`${width}: two-touch move is canceled and one-touch move is allowed`, prevented[0] && !prevented[1], JSON.stringify(prevented))

        await zoom(center, [-30, 30], [-45, 45])
        check(`${width}: a spread of less than 40 px does not zoom`, JSON.stringify(await crumbs()) === '["Factory"]')
        await zoom(center, [-80, 80], [-103, 103])
        check(`${width}: a spread below the distance ratio does not zoom`, JSON.stringify(await crumbs()) === '["Factory"]')
        await zoom(center, [-30], [-85])
        check(`${width}: one moving finger does not zoom`, JSON.stringify(await crumbs()) === '["Factory"]')
        for (const kind of ['mouse', 'pen']) {
          await zoom(center, [-30, 30], [-60, 60], kind)
          check(`${width}: ${kind} pointers do not pinch zoom`, JSON.stringify(await crumbs()) === '["Factory"]')
        }
        await zoom({ x: Math.floor(width / 2), y: 20 }, [-30, 30], [-60, 60])
        check(`${width}: a spread with no item under the start midpoint does not zoom`, JSON.stringify(await crumbs()) === '["Factory"]')
        await zoom(center, [-60, 60], [-30, 30])
        check(`${width}: squeezing at the floor leaves Factory open`, await page.locator('dialog.factory[open]').isVisible() && JSON.stringify(await crumbs()) === '["Factory"]')

        // A finger that ends outside the dialog (the browser sends pointercancel to <html>) leaves no stale id.
        await page.evaluate(({ x, y }) => {
          document.elementFromPoint(x, y).dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', pointerId: 31, isPrimary: true, clientX: x, clientY: y }))
          document.documentElement.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerType: 'touch', pointerId: 31, isPrimary: true, clientX: x, clientY: y }))
        }, center)
        await pointers(center, [['down', [-30, 30]], ['move', [-60, 60]]])
        await page.locator('.factory-task').waitFor()
        await zoomSettled(page)
        check(`${width}: spreading two fingers on B190 zooms to the task`, JSON.stringify(await crumbs()) === '["Factory","Build","B190"]')
        const agentCenter = await midpoint(page.locator('[data-key="agent:%5"]'))
        await pointers(agentCenter, [['move', [-90, 90]], ['move', [-120, 120]], ['move', [-5, 5]], ['up', [-5, 5]]])
        await zoomSettled(page)
        check(`${width}: continuing or reversing the same pinch only zooms once`, JSON.stringify(await crumbs()) === '["Factory","Build","B190"]')

        await zoom(agentCenter, [-50, 50], [-36, 36])
        check(`${width}: a squeeze of less than 40 px does not zoom`, JSON.stringify(await crumbs()) === '["Factory","Build","B190"]')
        await zoom(agentCenter, [-100, 100], [-77, 77])
        check(`${width}: a squeeze above the distance ratio does not zoom`, JSON.stringify(await crumbs()) === '["Factory","Build","B190"]')
        await pointers(agentCenter, [
          ['down', [-30, 30]], ['cancel', [null, 30]], ['down', [null, 90]],
          ['move', [-30, 30]], ['up', [-30, 30]],
        ])
        await page.locator('.factory-phase').waitFor()
        check(`${width}: a new squeeze after pointercancel zooms out to the phase`, JSON.stringify(await crumbs()) === '["Factory","Build"]')
        await page.keyboard.press('Escape')
        await page.locator('.factory-floor').waitFor()
      }
      await zoomSettled(page)
      await page.setViewportSize(viewport)
    }

    // CDP sends trusted touches through Chromium's gesture recognition and scrolling.
    for (const viewport of touchViewports) {
      const { width } = viewport
      const context = await browser.newContext({ viewport, hasTouch: true, isMobile: true, serviceWorkers: 'block' })
      try {
        const touchPage = await context.newPage()
        await setupPage(touchPage)
        const cdp = await context.newCDPSession(touchPage)
        await touchPage.goto(`${ORIGIN}/chat/?go=factory`)
        const pinch = (center, from, to) => dragTouches(cdp, [-1, 1].map(side => [
          { x: center.x + side * from, y: center.y },
          { x: center.x + side * to, y: center.y },
        ]))
        const viewportScale = () => touchPage.evaluate(() => window.visualViewport.scale)

        await pinch(await midpoint(touchPage.locator('[data-key="task:B190"]')), 30, 65)
        await touchPage.locator('.factory-task').waitFor()
        await zoomSettled(touchPage)
        check(`${width}: real spread on B190 zooms to the task`, JSON.stringify(await readCrumbs(touchPage)) === '["Factory","Build","B190"]')
        check(`${width}: viewport scale stays at 1 after real spread`, await viewportScale() === 1)

        await pinch(await midpoint(touchPage.locator('[data-key="agent:%5"]')), 65, 30)
        await touchPage.locator('.factory-phase').waitFor()
        await zoomSettled(touchPage)
        check(`${width}: real squeeze zooms out to the phase`, JSON.stringify(await readCrumbs(touchPage)) === '["Factory","Build"]')
        check(`${width}: viewport scale stays at 1 after real squeeze`, await viewportScale() === 1)

        const body = touchPage.locator('.factory-body')
        if (!await body.evaluate(el => el.scrollHeight > el.clientHeight)) {
          // Leave 120 px for the body so the Build fixture overflows.
          const { y } = await body.boundingBox()
          await touchPage.setViewportSize({ width, height: Math.ceil(y) + 120 })
        }
        await touchPage.waitForFunction(() => {
          const el = document.querySelector('.factory-body')
          return el.scrollHeight > el.clientHeight
        })
        await body.evaluate(el => { el.scrollTop = 0 })
        const { x, y } = await midpoint(body)
        await dragTouches(cdp, [[{ x, y: y + 40 }, { x, y: y - 40 }]])
        await touchPage.waitForFunction(() => document.querySelector('.factory-body').scrollTop > 0)
        check(`${width}: one real finger scrolls the body and keeps the phase`, JSON.stringify(await readCrumbs(touchPage)) === '["Factory","Build"]')
      } finally {
        await context.close()
      }
    }

    // A task changes phase while its history write fails. A fresh floor prompts another attempt.
    const deployReport = (status, count = 1) => page.waitForResponse(response => response.url().endsWith('/factory/history') &&
      response.status() === status && reportsDeploy(response.request().postDataJSON()) && deployReports().length >= count,
    { timeout: 12000 })
    let saveRetry
    historyRetryGate = new Promise(resolve => { saveRetry = resolve })
    state.rejectHistory = true
    const rejected = deployReport(503)
    state.moved = true
    await station('deploy').locator('[data-token="B71"]').waitFor({ timeout: 12000 })
    // The token renders in Deploy one render before the effect marks it moved: wait for the mark, do not read it at once.
    const marked = await station('deploy').locator('[data-token="B71"].is-moved').waitFor({ timeout: 5000 }).then(() => true, () => false)
    check('B71 moves to Deploy on the next poll', marked)
    // Each poll (and each clock tick) rebuilds the floor with the same phases: the mark stays until a phase changes.
    const nextPoll = page.waitForResponse(response => response.url().endsWith('/api/factory') && response.ok(), { timeout: 12000 })
    await nextPoll
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    check('a poll with no phase change keeps the moved mark', await station('deploy').locator('[data-token="B71"].is-moved').count() === 1)
    await rejected
    await station('deploy').locator('[data-key="task:B71"]').click()
    await timeline.waitFor()
    const afterFailure = await timeline.locator('b').allInnerTexts()
    check('a failed write leaves the displayed server timeline intact', JSON.stringify(afterFailure) === '["Inbox","Build"]', JSON.stringify(afterFailure))
    const accepted = deployReport(200, 2)
    saveRetry()
    await accepted
    const reports = deployReports()
    check('a fresh floor retries with just the phase move and the write header', reports.length === 2 &&
      reports[1].poll > reports[0].poll && reports.every(({ body, header }) =>
        JSON.stringify(body) === '{"moves":[{"task":"B71","phase":"deploy"}]}' && header === '1'), JSON.stringify(reports))
    await timeline.getByText('Deploy', { exact: true }).waitFor({ timeout: 12000 })
    check('the next poll shows the phase saved by the server', JSON.stringify(await timeline.locator('b').allInnerTexts()) === '["Inbox","Build","Deploy"]')
    await page.waitForFunction(() => /B71 .* moved to Deploy/.test(document.querySelector('.factory [aria-live="polite"]')?.textContent || ''), null, { timeout: 20000 })
    check('the live region announces the move', true)
    state.moved = false
    await page.locator('.factory-crumbs button', { hasText: 'Factory' }).click()
    await page.locator('.factory-floor').waitFor()

    // Esc on the floor closes the view.
    await page.keyboard.press('Escape')
    await view.waitFor({ state: 'detached' })
    check('Esc on the floor closes the view', !(await view.count()))

    // The command menu opens it, behind no setting.
    const beforeReopen = state.reports.length
    await page.keyboard.press('Alt+Space')
    await page.keyboard.type('Factory')
    await page.locator('.command-menu').getByText('Factory view', { exact: true }).first().click()
    await station('inbox').waitFor()
    check('command menu entry opens the view', await view.evaluate(box => box.open))
    await station('build').locator('[data-token="B71"]').waitFor()
    await page.waitForResponse(response => response.url().endsWith('/api/factory') && response.ok())
    check('a stale floor leaves a recently saved phase alone', !state.reports.slice(beforeReopen).some(({ body }) =>
      body.moves.some(move => move.task === 'B71' && move.phase === 'build')))

    // Error with stale data, then the server with no data at all, then the empty floor.
    state.factory = 'down'
    await page.locator('.factory-stale').waitFor({ timeout: 12000 })
    check('server down: the old data stays, with its age', /does not answer.*Shown data is from \d\d:\d\d\.?/.test(await page.locator('.factory-stale').innerText()), await page.locator('.factory-stale').innerText())
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'state-error-stale-1280-light.png') })
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await page.locator('.factory-problem').waitFor({ timeout: 15000 })
    check('server down at start: the error state', /The server does not answer/.test(await page.locator('.factory-problem').innerText()))
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'state-error-1280-light.png') })
    state.factory = 'empty'; state.panes = []
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await page.locator('.factory-problem', { hasText: 'No work on the floor' }).waitFor({ timeout: 15000 })
    check('no agents and no items: the empty state', true)
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'state-empty-1280-light.png') })
    state.factory = 'ok'; state.panes = panes

    // Session data can arrive after Factory and the backlog. Only report phases from the complete floor.
    const beforeOpen = state.reports.length
    state.history.B71 = [{ phase: 'build', at: NOW - 60001 }]
    state.moved = true
    let showSessions
    sessionGate = new Promise(resolve => { showSessions = resolve })
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await station('inbox').locator('[data-token="B71"]').waitFor()
    const afterSessions = deployReport(200)
    showSessions()
    await afterSessions
    check('only the complete floor reports a phase for B71', !state.reports.slice(beforeOpen).some(({ body }) =>
      body.moves.some(move => move.task === 'B71' && move.phase === 'inbox')))
    state.moved = false

    // Every width from 320 px: no sideways scroll at any level; targets stay 24 px or more.
    for (const [width, height] of [[320, 640], [390, 844], [1280, 800], [1920, 1080], [3840, 2160]]) {
      await page.setViewportSize({ width, height })
      await page.goto(`${ORIGIN}/chat/?go=factory`)
      await station('build').waitFor()
      check(`${width}: no sideways scroll on the floor`, !(await overflow()))
      const small = await page.evaluate(() => [...document.querySelectorAll('.factory button')].filter(el => el.getClientRects().length).map(el => el.getBoundingClientRect()).filter(r => r.width < 24 || r.height < 24).length)
      check(`${width}: every button is 24 px or more`, small === 0, `${small} small`)
      if (width === 320 || width === 1280) {
        await station('verify').locator('[data-key="task:B191"]').click()
        await page.locator('.factory-task').waitFor()
        await zoomSettled(page)
        const roles = await page.locator('.factory-agents .factory-card-meta').allInnerTexts()
        check(`${width}: B191 shows main, worker and advisor roles for its linked agents`,
          JSON.stringify(roles.map(text => text.split(' · ')[0])) === '["main","worker","reviewer","worker","advisor"]', JSON.stringify(roles))
        check(`${width}: the linked worker and advisor fit in the task view`, !(await overflow()))
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `parent-links-${width}.png`) })
        await page.locator('[data-key="agent:%11"]').focus()
        await page.keyboard.press('Enter')
        await page.locator('.factory-agent').waitFor()
        check(`${width}: Enter opens the linked worker`, (await crumbs()).at(-1) === 'Factory helper')
        await page.locator('.factory-crumbs button', { hasText: 'Factory', exact: true }).first().click()
        await page.locator('.factory-floor').waitFor()
        await zoomSettled(page)
      }
      await page.locator('[data-key="task:B71"]').click()
      await page.locator('.factory-task').waitFor()
      await zoomSettled(page)
      await page.locator('.factory-agents button').first().click()
      await page.locator('.factory-screen').waitFor()
      check(`${width}: no sideways scroll on the agent level`, !(await overflow()))
    }

    // Screenshots: four levels at four widths, light and dark.
    if (SHOTS) {
      for (const scheme of ['light', 'dark']) {
        await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' })
        await page.evaluate(theme => { const prefs = JSON.parse(localStorage.getItem('tmux-chat.prefs.v1')); prefs.theme = theme; localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify(prefs)) }, scheme)
        for (const [width, height] of [[390, 844], [1280, 800], [1920, 1080], [3840, 2160]]) {
          await page.setViewportSize({ width, height })
          await page.goto(`${ORIGIN}/chat/?go=factory`)
          await station('build').waitFor()
          await page.waitForTimeout(300)
          const shot = name => page.screenshot({ path: path.join(SHOTS, `${name}-${width}-${scheme}.png`) })
          await shot('1-floor')
          await page.locator('[data-key="phase:build"]').click(); await page.locator('.factory-phase').waitFor(); await page.waitForTimeout(300); await shot('2-phase')
          await page.locator('[data-key="task:B71"]').click().catch(async () => { await page.keyboard.press('Escape'); await page.locator('[data-key="task:B71"]').click() })
          await page.locator('.factory-task').waitFor(); await page.waitForTimeout(300); await shot('3-task')
          await page.locator('.factory-agents button').first().click(); await page.locator('.factory-screen').waitFor(); await page.waitForTimeout(300); await shot('4-agent')
        }
      }
      console.log(`shots: ${SHOTS}`)
    }
    // An urgent ask does not open the Waiting on you deck over the Factory view; it opens when the view closes.
    await page.setViewportSize({ width: 1280, height: 800 })
    state.panes = [...panes, pane('%10', 'Asks now', { ask: { pane: '%10', thread: '%10', since: NOW / 1000 - 60, next: 0, kind: 'question', opened: false, snoozed_until: null, urgent: true } })]
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await station('build').waitFor()
    await page.waitForTimeout(1500)
    check('urgent ask: no deck over the Factory view', !(await page.locator('#deck-title').isVisible()))
    await page.keyboard.press('Escape')
    await page.locator('#deck-title').waitFor({ timeout: 10000 })
    check('urgent ask: the deck opens after the view closes', true)
    state.panes = panes

    // Ambient tour, on a fake clock: after 60 s with no input it shows the task that waits for you; a key stops it.
    await page.setViewportSize({ width: 1920, height: 1080 })
    await page.evaluate(() => localStorage.setItem('tether-factory-tour', 'true'))
    await page.clock.install()
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await station('build').waitFor()
    check('Tour toggle is on and pressed', await page.locator('.factory-tour').getAttribute('aria-pressed') === 'true')
    await page.clock.runFor(30000)
    check('no tour before 60 s idle', JSON.stringify(await crumbs()) === '["Factory"]', JSON.stringify(await crumbs()))
    await page.clock.runFor(35000)
    await page.locator('.factory-touring').waitFor()
    check('after 60 s idle the tour shows the task that waits for you', JSON.stringify(await crumbs()) === '["Factory","Build","B190"]', JSON.stringify(await crumbs()))
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'state-tour-1920-dark.png') })
    await page.keyboard.press('Shift')
    await page.locator('.factory-touring').waitFor({ state: 'detached' })
    check('any key stops the tour', true)
    // The worktree sweep (B210): a strip on the floor, flags on the task, the lines in the task.
    await page.clock.runFor(1000)
    await page.keyboard.press('Shift')
    for (const [width, height] of [[1280, 800], [320, 640]]) {
      await page.setViewportSize({ width, height })
      await page.goto(`${ORIGIN}/chat/?go=factory`)
      await station('build').waitFor()
      const strip = page.locator('details.factory-sweep')
      check(`${width}: the sweep strip names the count and is closed`, /Worktree sweep\s+3 findings/.test(await strip.locator('summary').innerText()) && !(await strip.getAttribute('open') !== null), await strip.locator('summary').innerText())
      await strip.locator('summary').focus()
      await page.keyboard.press('Enter')
      check(`${width}: Enter opens the strip with one line per finding`, await strip.locator('.factory-list li').count() === 3)
      check(`${width}: a line names the tab, the finding and the branch`, (await strip.locator('.factory-list li').first().innerText()).replace(/\s+/g, ' ').includes('B71 Part 2: Behind main 6 commits behind main. Rebase by hand. b71-part2'))
      check(`${width}: the strip says Tether does not rebase`, (await strip.innerText()).includes('It does not rebase or change a branch.'))
      check(`${width}: no sideways scroll with the strip open`, !(await overflow()))
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `sweep-floor-${width}.png`) })
      const token = page.locator('.factory-token', { hasText: 'B71' }).first()
      check(`${width}: the task token carries both sweep flags`, JSON.stringify(await token.locator('.factory-flag.is-sweep').allInnerTexts()) === '["Behind main","Checks fail"]', JSON.stringify(await token.locator('.factory-flag.is-sweep').allInnerTexts()))
      await token.click()
      await page.locator('#factory-sweep').waitFor()
      check(`${width}: the task lists its sweep lines`, await page.locator('.factory-task section[aria-labelledby="factory-sweep"] li').count() === 2)
      check(`${width}: no sideways scroll in the task`, !(await overflow()))
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `sweep-task-${width}.png`) })
    }
    state.factory = 'empty'
    await page.goto(`${ORIGIN}/chat/?go=factory`)
    await page.locator('.factory-floor, .factory-problem').first().waitFor()
    check('a server with no sweep data shows no strip', await page.locator('details.factory-sweep').count() === 0)
    state.factory = 'ok'
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
    if (pageErrors.length) console.log(pageErrors.join('\n'))
  } finally {
    await page?.close().catch(() => {})
    await browser?.close().catch(() => {})
  }
  console.log(failed ? `factory ui: ${failed} failed` : 'factory ui: ok')
  process.exitCode = failed ? 1 : 0
}
main()
