// List search against a real UI build. Run npm run build in ui/ first, or set DIST.
// The fixture intercepts every request, including writes, so it never reaches a terminal.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const ORIGIN = 'http://tether.test'
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json' }

function pane(id, session, title, agent) {
  return { id: `%${id}`, tmux_session: `$${session}`, label: `Session ${session}:0.${id}`,
    pane_name: title, agent, dir: `/fixtures/project-${session}`, state: 'Idle', writable: true, thread: `thread-${id}`, approval: null }
}

const tabs = Array.from({ length: 42 }, (_, index) => {
  const id = index + 1
  const titles = { 1: 'Current work', 4: 'Review plans', 7: 'Review release', 13: 'Folding atlas', 26: 'Review docs' }
  return pane(id, Math.ceil(id / 7), titles[id] || `Task ${id}`, id % 2 ? 'claude' : 'codex')
})
const fixtures = {
  chat: { messages: [], busy: false, thread: 'thread-1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: false, notices: [] },
  subagents: { main: {}, agents: [], tasks: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' },
  'codex-runs': { pane: '', running: 0, runs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  'projects/backlog': { text: '' },
  'projects/marks': {},
  'side-shells': { shells: [] },
  host: { name: 'Test server', office: false },
  update: { current: '0.0.0', latest: '0.0.0', available: false },
  usage: { claude: null, codex: null },
  'browser/agents': { enabled: false, available: false, agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  prefs: { prefs: {}, version: 1 },
}

async function installFixtures(context, sessions, posts) {
  await context.routeWebSocket('**/*', socket => socket.close())
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== ORIGIN) return route.abort('blockedbyclient')
    if (url.pathname.startsWith('/mobile/api/')) {
      const endpoint = url.pathname.slice('/mobile/api/'.length)
      if (endpoint === 'review-link' && request.method() === 'POST') {
        posts.push(request.postDataJSON())
        return route.fulfill({ json: { ok: true } })
      }
      if (endpoint === 'sessions') return route.fulfill({ json: { sessions: sessions(), default_thread: 'thread-1' } })
      if (endpoint === 'stream') return route.fulfill({ status: 204 })
      return route.fulfill({ json: fixtures[endpoint] ?? {} })
    }
    if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404 })
    const file = path.resolve(DIST, url.pathname.slice('/chat/'.length) || 'index.html')
    if (!file.startsWith(DIST + path.sep)) return route.fulfill({ status: 404 })
    const body = await fs.readFile(file).catch(() => null)
    return body ? route.fulfill({ body, contentType: TYPES[path.extname(file)] || 'application/octet-stream' }) : route.fulfill({ status: 404 })
  })
  await context.addInitScript(() => {
    localStorage.setItem('tmux-chat.prefs.synced', '1')
    localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ voiceOff: true, voiceSound: false, sideMode: 'sessions' }))
  })
}

async function main() {
  const browser = await getBrowser({ local: true })
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, serviceWorkers: 'block', reducedMotion: 'reduce' })
    const posts = []
    let sessions = tabs
    await installFixtures(context, () => sessions, posts)
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const open = () => page.goto(`${ORIGIN}/chat/?session=%251`)
    const dialog = page.locator('dialog.popover[aria-labelledby="pane-title"]')
    async function pickFromTabMenu(name) {
      await page.locator('.pane-tabs [role="tab"][data-pane="%1"]').click({ button: 'right' })
      await dialog.getByRole('button', { name, exact: true }).click()
    }
    const focusIs = selector => page.waitForFunction(target => document.activeElement?.matches(target), selector)
    const openReview = async () => {
      await pickFromTabMenu('Link a review…')
      await focusIs('.review-picker input[type="search"]')
    }
    const findTab = dialog.getByRole('searchbox', { name: 'Find a tab' })
    const rows = dialog.locator('.review-picker-list button')
    const titles = () => rows.locator('.review-name').allTextContents()
    const inOrder = [...tabs.slice(1, 7).reverse(), ...tabs.slice(7).reverse()].map(tab => tab.pane_name)

    await open()
    assert.equal(await page.evaluate(() => matchMedia('(pointer: fine)').matches), true)
    await openReview()
    assert.deepEqual(await titles(), inOrder, 'current session first, then highest pane number first in each set')
    await findTab.press('Enter')
    assert.equal(await dialog.isVisible(), true, 'Enter with an empty query keeps the picker open')

    await findTab.fill('review')
    await dialog.getByRole('status').filter({ hasText: /^3 tabs found$/ }).waitFor()
    assert.deepEqual(await titles(), ['Review release', 'Review plans', 'Review docs'])
    await findTab.press('ArrowDown')
    await focusIs('.review-picker-list button:first-child')
    await page.keyboard.press('ArrowUp')
    await focusIs('.review-picker input[type="search"]')

    await findTab.press('Escape')
    assert.equal(await findTab.inputValue(), '')
    assert.equal(await dialog.isVisible(), true, 'Escape clears text before closing the picker')
    await findTab.press('Escape')
    await dialog.waitFor({ state: 'detached' })

    await openReview()
    await findTab.fill('no such title')
    await dialog.getByText(/^No tab matches “no such title”\./).waitFor()
    assert.equal(await rows.count(), 0)
    await dialog.getByRole('button', { name: 'Clear search' }).click()
    await focusIs('.review-picker input[type="search"]')
    assert.deepEqual(await titles(), inOrder)
    assert.deepEqual(posts, [], 'an empty query never sends a review-link POST')

    await findTab.fill('review')
    const linked = page.waitForResponse(response => response.url() === `${ORIGIN}/mobile/api/review-link` && response.request().method() === 'POST')
    await findTab.press('Enter')
    assert.ok((await linked).ok())
    await dialog.waitFor({ state: 'detached' })
    assert.deepEqual(posts, [{ worker: '%7', reviewer: '%1' }], 'a nonempty query picks the first result once')
    console.log('ok review picker focus, order, filtering, status, arrows, Enter, Escape, and Clear search')

    // Six sessions cover the review picker. Add three so the move picker has eight destinations.
    sessions = [...tabs, ...[7, 8, 9].map(number => pane(40 + number, number, `Extra task ${number}`, 'codex'))]
    await open()
    await pickFromTabMenu('Move to session…')
    const findSession = dialog.getByRole('searchbox', { name: 'Find a session' })
    await findSession.fill('folding atlas')
    await dialog.getByRole('button', { name: 'Session 2 7 panes', exact: true }).waitFor()
    assert.equal(await dialog.locator('.action-list button:not([data-search-skip])').count(), 1, 'a tab title finds only its session')
    await findSession.fill('UnbrokenSearchTerm'.repeat(20))
    await dialog.getByText(/^No session matches/).waitFor()
    assert.ok(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'a long unmatched query wraps inside the picker')
    await dialog.getByRole('button', { name: 'New session', exact: true }).waitFor()
    await findSession.press('ArrowDown')
    assert.equal(await findSession.evaluate(input => input === document.activeElement), true, 'arrows skip commands when there are no destinations')
    assert.deepEqual(errors, [])
    console.log('ok move picker filters by a tab title and skips commands when there are no results')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
