// The Session details drawer: its sections, hidden empty rows, the AI description (write, edit, clear), rename,
// and its links to Files, the backlog, plan usage and the model picker. Every API request uses fixtures.
// Run: npm run build -- --outDir <dir>, then DIST=<dir> node test_session_details_ui.cjs. Request routes serve the build.
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/session-brief/shots')
const now = () => Math.floor(Date.now() / 1000)
const pane = { id: '%1', tmux_session: '$fixture', label: 'work:0.1', pane_name: 'Files search', agent: 'claude', state: 'Idle', dir: '/srv/fixture/app', thread: 'thread-1', writable: true }
const AI = { name: 'Files search', description: 'Adds a search box to the Files panel and ranks matches by path.', description_by: 'ai', state: 'done' }
let state

function fixture(endpoint, url) {
  const agent = state.agent || pane.agent
  const footer = { observed: true, model: agent === 'codex' ? 'GPT-6.2-Sol' : 'Opus 5.5', effort: 'high', tier: state.tier, mode: 'default', context: { percent: 42 }, goal: state.goal ? 'active' : undefined, tasks: state.tasks, task_details: [], ide: state.ide }
  const fixtures = {
    sessions: { sessions: [{ ...pane, agent, thread: state.thread, finished: now() - 300, brief: state.brief }], default_thread: state.thread },
    chat: { messages: [], earlier: 0, limit: 40, session: pane.id, thread: state.thread, agent, state: 'Idle', writable: true, footer: agent === 'codex' ? footer : undefined },
    footer,
    prefs: { prefs: {}, version: 1 }, host: { name: 'Fixture' }, 'projects/marks': {}, 'side-shells': { shells: [] },
    'projects/backlog': { text: '## tether\n- [ ] Add search to Files (claude · pane %1 · chat thread-1) #B4\n', version: 'v1', limit: 50000 },
    catalog: { commands: [], plugins: [], mcp: [] }, usage: { claude: null, codex: null }, subagents: { main: {}, agents: [], tasks: [] }, 'codex-runs': { runs: [] },
    'browser/agents': { enabled: false, agents: [], asks: [] }, 'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
    'model-options': { agent: 'claude', models: [{ id: 'opus', label: 'Opus 5.5', current: true, effort: true }], efforts: ['high'], current_effort: 'high' },
    'files/list': { path: url.searchParams.get('path') || '/', parent: '/srv/fixture', entries: [], truncated: false }, 'files/places': { places: [] },
    'work/board': { revision: 1, projects: [], items: [], stages: [] }, update: { current: '0.0.0', latest: '0.0.0', available: false },
  }
  if (endpoint === 'tab-brief') {
    state.gets.push(url.search)
    if (state.fail) return { status: 500, json: { error: 'Disk read failed.' } }
    return { json: { brief: state.brief, enabled: state.enabled, backlog: state.links } }
  }
  return { json: fixtures[endpoint] || {} }
}

async function main() {
  let browser, page, failed = 0
  const errors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  // The newest tab-brief post: the page also posts other things (ui-usage) at any time.
  const briefPost = () => state.posts.findLast(post => post.endpoint === 'tab-brief')
  const reset = (extra = {}) => { state = { brief: { ...AI, at: now() - 120 }, enabled: true, links: [{ id: 'B4', title: 'Add search to Files', state: 'doing', open: true }], goal: true, tasks: ['1 shell'], ide: undefined, fail: false, posts: [], gets: [], thread: pane.thread, ...extra } }
  reset()
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    const origin = 'http://127.0.0.1:5143'
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => errors.push(error.stack))
    page.on('console', message => { if (message.type() === 'error' && !/status of (404|500)/.test(message.text())) errors.push(message.text().slice(0, 300)) })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (request.method() === 'POST') {
          const body = request.postDataJSON() || {}
          state.posts.push({ endpoint, body })
          if (endpoint === 'tab-brief' && body.action === 'save') state.brief = { ...state.brief, description: body.description || undefined, description_by: 'user' }
          if (endpoint === 'tab-brief' && body.action === 'write') state.brief = { name: 'Files search box', description: 'Builds search for the Files panel.', description_by: 'ai', state: 'done', at: now() }
          if (endpoint === 'tab-brief') return route.fulfill({ json: { brief: state.brief } })
          return route.fulfill({ json: endpoint === 'model-options' ? fixture(endpoint, url).json : { ok: true } })
        }
        const { status = 200, json } = fixture(endpoint, url)
        return route.fulfill({ status, json })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/') && request.method() === 'GET') {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (file !== path.join(DIST, 'index.html') && !file.startsWith(DIST + path.sep)) return route.fulfill({ status: 404, body: '' })
        return route.fulfill({ path: file, headers: { 'Cache-Control': 'no-store' } }).catch(() => route.fulfill({ status: 404, body: '' }))
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', selected: '%1', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const drawer = page.locator('dialog.session-details')
    const open = async () => {
      await page.locator('button.chat-title').first().click()
      await drawer.waitFor()
      await drawer.locator('.sd-description, .sd-placeholder').first().waitFor()
    }
    const close = async () => { await page.keyboard.press('Escape'); await drawer.waitFor({ state: 'detached' }) }
    const sections = () => drawer.locator('.sd-section h3').allInnerTexts()

    for (const [width, height] of [[390, 844], [1280, 860], [320, 640]]) {
      const at = `${width}px`
      reset()
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await open()
      await drawer.getByText(AI.description).waitFor()
      check(`${at} the description from the server shows`, await drawer.getByText(AI.description).isVisible())
      check(`${at} the five filled sections show, in order`, JSON.stringify((await sections()).map(text => text.toLowerCase())) === JSON.stringify(['overview', 'where', 'agent', 'activity', 'tools']), JSON.stringify(await sections()))
      check(`${at} an empty IDE row is hidden`, !(await drawer.getByText('IDE context').count()))
      check(`${at} sans-serif title`, !/serif/.test((await drawer.locator('h2').evaluate(node => getComputedStyle(node).fontFamily)).replace('sans-serif', '')))
      check(`${at} the name came from AI`, await drawer.getByText('Named by AI').isVisible())
      check(`${at} no page overflow`, !(await overflow()))
      check(`${at} the drawer fits the screen`, await drawer.evaluate(node => node.scrollWidth <= node.clientWidth + 1))
      const small = await drawer.locator('button:visible').evaluateAll(nodes => nodes.filter(node => { const box = node.getBoundingClientRect(); return box.height < 43.5 || box.width < 43.5 }).map(node => node.getAttribute('aria-label') || node.textContent))
      check(`${at} every button is at least 44 px`, !small.length, JSON.stringify(small))
      const box = await drawer.boundingBox()
      if (width === 1280) check('1280px a full-height drawer on the right', Math.abs(box.x + box.width - 1280) < 2 && box.height >= height - 2, JSON.stringify(box))
      else check(`${at} a sheet from the bottom`, Math.abs(box.y + box.height - height) < 2 && box.width <= width, JSON.stringify(box))
      await page.screenshot({ path: path.join(SHOTS, `details-${width}.png`) })
      if (width !== 390) { await close(); continue }

      // Edit, then clear, the description.
      await drawer.getByRole('button', { name: 'Edit' }).click()
      const field = drawer.getByRole('textbox', { name: 'Description' })
      check('edit puts the focus in the field', await field.evaluate(node => node === document.activeElement))
      await field.fill('My own words.')
      await drawer.getByRole('button', { name: 'Save', exact: true }).click()
      await drawer.getByText('My own words.').waitFor()
      check('save posts the text', JSON.stringify(briefPost()) === JSON.stringify({ endpoint: 'tab-brief', body: { pane: '%1', thread: 'thread-1', action: 'save', description: 'My own words.' } }))
      // The author line follows the saved brief; it can render a moment after the text.
      check('a saved text says who wrote it', await drawer.getByText('Written by you').waitFor({ timeout: 5000 }).then(() => true, () => false))
      await drawer.getByRole('button', { name: 'Clear' }).click()
      await drawer.getByText('No description yet.').waitFor()
      check('clear posts an empty text', briefPost()?.body.description === '')
      // Write it again with AI.
      await drawer.getByRole('button', { name: 'Write with AI' }).click()
      await drawer.getByText('Builds search for the Files panel.').waitFor()
      check('write posts the write action', state.posts.some(post => post.endpoint === 'tab-brief' && post.body.action === 'write'), JSON.stringify(state.posts.map(post => post.endpoint)))
      // Rename.
      const renameButton = drawer.getByRole('button', { name: 'Rename this tab' })
      const focusedOnRename = () => renameButton.evaluate(node => node === document.activeElement)
      await renameButton.click()
      check('rename mode keeps the dialog name', /^Session details: /.test(await drawer.getAttribute('aria-label') || ''), await drawer.getAttribute('aria-label'))
      await drawer.getByRole('button', { name: 'Cancel' }).click()
      await renameButton.waitFor()
      check('cancel returns the focus to Rename', await focusedOnRename())
      await renameButton.click()
      await drawer.getByRole('textbox', { name: 'Tab name' }).fill('Search work')
      await drawer.getByRole('button', { name: 'Save name' }).click()
      await renameButton.waitFor()
      check('rename posts pane-name', state.posts.some(post => JSON.stringify(post) === JSON.stringify({ endpoint: 'pane-name', body: { pane: '%1', name: 'Search work' } })), JSON.stringify(state.posts.slice(-3)))
      check('save returns the focus to Rename', await focusedOnRename())
      // A new chat in the same pane: a draft from the old chat must not save to the new one.
      await drawer.getByRole('button', { name: 'Edit' }).click()
      await drawer.getByRole('textbox', { name: 'Description' }).fill('Draft for the old chat')
      state.thread = 'thread-2'
      await drawer.getByRole('textbox', { name: 'Description' }).waitFor({ state: 'detached', timeout: 20000 })
      check('a new chat drops the old draft', !state.posts.some(post => post.body.description === 'Draft for the old chat'))
      await page.waitForFunction(() => !document.querySelector('.session-details textarea'))
      state.thread = pane.thread
      await close(); await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' }); await open()
      // Copy the pane id: no crash, the button answers.
      await drawer.getByRole('button', { name: 'Copy pane' }).click()
      // copyText() is async; the label changes when it settles and resets after 1.5 s.
      check('copy answers', await drawer.locator('.sd-copy[aria-label="Copied"], .sd-copy[aria-label="Could not copy"]').waitFor({ timeout: 1000 }).then(() => true, () => false))
      // Keyboard: Tab moves through the drawer and the focus shows.
      await page.keyboard.press('Tab')
      check('keyboard focus stays in the drawer', await drawer.evaluate(node => node.contains(document.activeElement)))
      // Links out of the drawer.
      await drawer.getByRole('button', { name: /^Backlog B4/ }).click()
      const backlog = page.locator('dialog.backlog-page')
      await backlog.waitFor()
      check('a backlog row opens the backlog page on that item', (await backlog.getByRole('searchbox').inputValue()) === 'B4' && await drawer.count() === 0)
      await backlog.getByRole('button', { name: /^Close/ }).first().click(); await backlog.waitFor({ state: 'detached' })
      await open()
      await drawer.getByRole('button', { name: /^Plan usage/ }).click()
      await drawer.waitFor({ state: 'detached' })
      check('plan usage opens its sheet', await page.getByRole('dialog').filter({ hasText: /usage/i }).count() > 0)
      await page.keyboard.press('Escape')
      await open()
      await drawer.getByRole('button', { name: /^Goal/ }).click()
      await drawer.waitFor({ state: 'detached' })
      check('goal opens the goal sheet', await page.locator('dialog.live-sheet').isVisible())
      await page.keyboard.press('Escape')
      await open()
      await drawer.getByRole('button', { name: /^Model and effort/ }).click()
      await drawer.waitFor({ state: 'detached' })
      await page.getByRole('dialog').filter({ hasText: 'Opus 5.5' }).first().waitFor()
      check('model opens the model picker', true)
      await page.keyboard.press('Escape')
      await open()
      await drawer.getByRole('button', { name: /^Folder/ }).click()
      await drawer.waitFor({ state: 'detached' })
      await page.locator('section.xp').waitFor()
      check('folder closes the drawer and opens Files', await page.locator('.xp-title').innerText() === 'app', await page.locator('.xp-title').innerText())
    }

    // Off: no AI button, the note says why. Empty rows and sections stay hidden.
    reset({ enabled: false, brief: null, links: [], goal: false, tasks: [] })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
    await open()
    await drawer.getByText('No description yet.').waitFor()
    check('off: no Write with AI button', !(await drawer.getByRole('button', { name: /with AI/ }).count()))
    check('off: the note says why', await drawer.getByText(/Local only/).isVisible())
    check('off: Activity hides without activity', !(await drawer.getByRole('heading', { name: 'Activity' }).count()) || await drawer.getByText('Last reply').isVisible())
    check('off: the goal row hides without a goal', !(await drawer.getByRole('button', { name: /^Goal/ }).count()))
    await page.screenshot({ path: path.join(SHOTS, 'details-off-390.png') })
    await close()

    // A failed load: an error with Try again, and the rest of the drawer still works.
    reset({ fail: true, brief: null })
    await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
    await page.locator('button.chat-title').first().click()
    await drawer.getByText(/Could not load the description/).waitFor()
    check('error: the message names the cause', await drawer.getByText(/Disk read failed/).isVisible())
    state.fail = false; state.brief = { ...AI, at: now() }
    await drawer.getByRole('button', { name: 'Try again' }).click()
    await drawer.getByText(AI.description).waitFor()
    check('error: Try again loads the description', await drawer.getByText(AI.description).isVisible())
    await page.screenshot({ path: path.join(SHOTS, 'details-retry-390.png') })
    await close()

    // A brief that is being written: the drawer says so and looks again.
    reset({ brief: { state: 'running', at: now() } })
    await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
    await open()
    check('running: the drawer says it writes', await drawer.getByText('Writing a description…').isVisible())
    check('running: the AI button waits', await drawer.getByRole('button', { name: 'Writing…' }).isDisabled())
    state.brief = { ...AI, at: now() }
    await drawer.getByText(AI.description).waitFor({ timeout: 8000 })
    check('running: the finished brief shows without a reload', true)
    await close()

    for (const [width, height, tier, label] of [[320, 640, 'fast', 'Fast'], [1280, 860, 'ultrafast', 'Ultrafast'], [1280, 860, undefined, '']]) {
      reset({ agent: 'codex', tier })
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      const pillLabel = 'Model and effort' + (label ? `. ${label} mode on` : '')
      const pill = page.locator('.composer-pills button').filter({ hasText: 'GPT-6.2-Sol' })
      await pill.waitFor({ state: 'attached' })
      await page.locator('.composer textarea').focus()
      check(`${width}px ${tier ?? 'default'}: accessible model pill`, await pill.getAttribute('aria-label') === pillLabel)
      check(`${width}px ${tier ?? 'default'}: tier icon`, await pill.locator('.pill-tier svg[aria-hidden="true"]').isVisible() === Boolean(tier))
      const word = pill.locator('.pill-tier-word')
      check(`${width}px ${tier ?? 'default'}: tier word`, await word.isVisible() === Boolean(tier && width > 320))
      if (tier) check(`${width}px ${tier}: correct tier word`, await word.textContent() === tier)
      if (tier === 'fast') {
        await pill.press('Enter')
        const picker = page.locator('dialog.model-sheet[open]')
        await picker.waitFor()
        await page.keyboard.press('Escape')
        await picker.waitFor({ state: 'detached' })
        check('320px fast: keyboard opens the model picker', true)
      }
      const expected = 'GPT-6.2-Sol · high' + (tier ? ` · ${tier}` : '')
      await open()
      const row = drawer.getByRole('button', { name: /^Model and effort/ }).locator('.sd-row-value')
      check(`${width}px ${tier ?? 'default'}: details model and tier`, await row.textContent() === expected)
      await drawer.getByRole('button', { name: /^Usage and credits/ }).click()
      const usage = page.getByRole('dialog', { name: 'Usage', exact: true })
      check(`${width}px ${tier ?? 'default'}: usage model and tier`, await usage.locator('.sheet-sub').textContent() === expected)
      await page.keyboard.press('Escape')
    }

    check('no page errors', !errors.length, errors.join('\n'))
  } catch (error) {
    failed++; console.log(`FAIL ${error.stack}`)
  } finally {
    await page?.close().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exit(failed ? 1 : 0)
}
main()
