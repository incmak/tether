// Backlog items on the work board (src/WorkBacklog.tsx, server work_backlog.py): cards in Ready, Building and Done,
// a cap per column, the Backlog filter, the drawer's moves, a 409, Open in Backlog, focus after the drawer, and an
// unreadable backlog with and without projects.
// Real browser against a build: DIST (default dist). Run: node test_work_backlog_ui.cjs
const { strict: assert } = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS
const ORIGIN = 'http://tether.test'
const NOW = Date.now() / 1000
const fixtures = {
  sessions: { sessions: [], default_thread: '' },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '- [ ] First backlog item #B1\n', version: 'v', limit: 200000, next_id: 2 },
  'side-shells': { shells: [] }, update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, usage: { claude: null, codex: null },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
}
const card = (n, stage, extra = {}) => ({
  id: `B${n}`, project: 'Backlog', area: n % 2 ? 'Inbox' : 'tether', title: `Backlog item number ${n} with a title long enough to wrap on a phone`,
  rank: n, stage, source: 'claude · chat a', doing: stage === 'building' ? 'Mak' : null, done: stage === 'done' ? 'in 1234abc' : null,
  doneAt: stage === 'done' ? NOW - 3600 : null, details: ['why it matters'], version: 1000 + n, ...extra,
})
const board = (error, bare) => ({
  revision: 1, stages: [], jira: { connected: false }, feeds: {},
  projects: bare ? [] : [{ name: 'shop', prefix: 'T', folder: '', triage: '', work: '', feed: '', feedTarget: '', feedBy: 'tether', jira: '', qaStatus: '', jiraUser: '' }],
  items: bare ? [] : [{ id: 'T-001', project: 'shop', title: 'Login fails', stage: 'ready', qa: false, ask: null, askFrom: null, refs: [], tabs: [], history: [], source: '', created: NOW, updated: NOW, doneAt: null, jira: null, version: 3 }],
  backlog: error ? { items: [], error } : { items: [...Array.from({ length: 12 }, (_, i) => card(i + 1, 'ready')), card(20, 'building'), card(21, 'done'), card(22, 'done', { doneAt: NOW - 8 * 86400 })], error: '' },
})
const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }

async function open(browser, viewport, options = {}) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block' })
  const posts = [], gets = [], errors = []
  await context.route(`${ORIGIN}/**`, route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.startsWith('/mobile/api/')) {
      const name = url.pathname.slice('/mobile/api/'.length)
      if (request.method() === 'POST') {
        const body = request.postDataJSON()
        if (name !== 'work/stage') return route.fulfill({ json: {} })
        posts.push({ name, body })
        if (options.conflict) return route.fulfill({ status: 409, json: { error: 'B1 changed since you loaded it. Reload and try again.' } })
        return route.fulfill({ json: { item: card(Number(body.item.slice(1)), body.stage, { version: 5 }) } })
      }
      gets.push(name)
      if (name === 'work/board') {
        const json = board(options.error, options.bare)
        // vanish: after the first move, B1 leaves the backlog (moved to DONE.md, or deleted)
        if (options.vanish && posts.length) json.backlog.items = json.backlog.items.filter(entry => entry.id !== 'B1')
        return route.fulfill({ json })
      }
      return name === 'stream' ? route.fulfill({ status: 204 }) : route.fulfill({ json: fixtures[name] ?? {} })
    }
    if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404 })
    const file = path.join(DIST, url.pathname.slice('/chat/'.length))
    const served = fs.existsSync(file) && fs.statSync(file).isFile() ? file : path.join(DIST, 'index.html')
    return route.fulfill({ status: 200, contentType: types[path.extname(served)] ?? 'application/octet-stream', body: fs.readFileSync(served) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${ORIGIN}/chat/?go=board`)
  await page.locator('.work-sheet').waitFor()
  return { context, page, posts, gets, errors }
}

const column = (page, name) => page.locator('.work-col').filter({ has: page.getByRole('heading', { name: new RegExp(`^${name}`) }) })

async function main() {
  const browser = await getBrowser()
  const results = []
  const check = async (name, fn) => {
    try { await fn(); results.push(`ok ${name}`) } catch (error) { results.push(`FAIL ${name}: ${error.message.split('\n')[0]}`); process.exitCode = 1 }
  }
  try {
    await check('cards, cap and Show more', async () => {
      const { context, page, errors } = await open(browser, { width: 1280, height: 900 })
      const ready = column(page, 'Ready')
      await ready.locator('[data-item="B1"]').waitFor()
      assert.equal(await ready.locator('.work-bcard-backlog').count(), 5)
      assert.equal(await ready.locator('[data-item="T-001"]').count(), 1)
      assert.equal(await ready.locator('.badge').first().textContent(), '13')
      await ready.getByRole('button', { name: /Show 7 more backlog items/ }).click()
      assert.equal(await ready.locator('.work-bcard-backlog').count(), 12)
      await ready.getByRole('button', { name: /Show fewer/ }).click()
      assert.equal(await ready.locator('.work-bcard-backlog').count(), 5)
      assert.equal(await column(page, 'Building').locator('[data-item="B20"]').count(), 1)
      assert.equal(await column(page, 'Done').locator('[data-item="B21"]').count(), 1)
      assert.equal(await page.locator('[data-item="B22"]').count(), 0, 'a Done item older than a week is off the board')
      await page.getByRole('button', { name: 'Backlog', exact: true }).click()
      assert.equal(await page.locator('[data-item="T-001"]').count(), 0)
      assert.equal(await page.locator('[data-item="B1"]').count(), 1)
      assert.deepEqual(errors, [])
      await context.close()
    })
    await check('drawer moves an item and opens the Backlog', async () => {
      const { context, page, posts, gets } = await open(browser, { width: 400, height: 860 })
      await page.locator('[data-item="B1"]').click()
      const drawer = page.locator('.work-drawer')
      await drawer.getByRole('heading', { name: /Backlog item number 1/ }).waitFor()
      assert.ok(await drawer.getByText('why it matters').isVisible())
      assert.equal(await drawer.getByRole('button', { name: 'Mark done' }).evaluate(el => el === document.activeElement), true)
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'backlog-drawer-400.png') })
      await drawer.getByRole('button', { name: 'Mark done' }).click()
      await drawer.getByText('B1 is Done.').waitFor()
      assert.deepEqual(posts[0], { name: 'work/stage', body: { item: 'B1', stage: 'done', version: 1001 } })
      await drawer.getByRole('button', { name: 'Open in Backlog' }).click()
      await page.locator('.work-sheet').waitFor({ state: 'detached' })
      await page.waitForFunction(() => document.querySelector('dialog[open]'))
      assert.ok(gets.includes('projects/backlog'), 'the Backlog page loads')
      await context.close()
    })
    await check('a changed item locks the drawer', async () => {
      const { context, page } = await open(browser, { width: 1024, height: 800 }, { conflict: true })
      await page.locator('[data-item="B20"]').click()
      const drawer = page.locator('.work-drawer')
      await drawer.getByRole('button', { name: 'Back to Ready' }).click()
      await drawer.getByText('B20 changed in the backlog. Read it again, then try again.').waitFor()
      assert.equal(await drawer.getByRole('button', { name: 'Mark done' }).isDisabled(), true)
      await context.close()
    })
    await check('closing the drawer gives focus back to its card', async () => {
      const { context, page } = await open(browser, { width: 320, height: 800 })
      await page.locator('[data-item="B1"]').focus()
      await page.keyboard.press('Enter')
      await page.locator('.work-drawer').getByRole('button', { name: 'Mark done' }).waitFor()
      await page.keyboard.press('Escape')
      await page.locator('.work-drawer').waitFor({ state: 'detached' })
      await page.waitForFunction(() => document.activeElement?.getAttribute('data-item') === 'B1')
      await context.close()
    })
    await check('a drawer whose item leaves the board gives focus to the view switch', async () => {
      const { context, page } = await open(browser, { width: 320, height: 800 }, { vanish: true })
      await page.locator('[data-item="B1"]').click()
      await page.locator('.work-drawer').getByRole('button', { name: 'Mark done' }).click()
      await page.locator('.work-drawer').waitFor({ state: 'detached' })
      await page.waitForFunction(() => document.activeElement?.matches('.work-views [aria-pressed="true"]'))
      await context.close()
    })
    await check('an unreadable backlog shows with no projects', async () => {
      const { context, page } = await open(browser, { width: 400, height: 800 }, { error: 'The backlog is over 200,000 characters.', bare: true })
      await page.getByText('Could not load the backlog. The backlog is over 200,000 characters.').waitFor()
      assert.ok(await page.getByText('No projects yet.', { exact: false }).isVisible())
      await context.close()
    })
    await check('an unreadable backlog leaves the rest of the board', async () => {
      const { context, page } = await open(browser, { width: 768, height: 900 }, { error: 'The backlog is over 200,000 characters.' })
      await page.getByText('Could not load the backlog. The backlog is over 200,000 characters.').waitFor()
      assert.equal(await page.locator('[data-item="T-001"]').count(), 1)
      await context.close()
    })
    for (const width of [320, 400, 768, 1024, 1280]) {
      await check(`no page overflow at ${width}px`, async () => {
        const { context, page } = await open(browser, { width, height: 860 })
        await page.locator('[data-item="B1"]').waitFor()
        // Wide layouts scroll the columns sideways inside the sheet; the page itself never scrolls sideways.
        const wide = await page.evaluate(() => ({
          page: document.documentElement.scrollWidth > innerWidth,
          cards: [...document.querySelectorAll('.work-bcard-backlog, .work-more')].filter(el => {
            const box = el.getBoundingClientRect(), col = el.closest('.work-col').getBoundingClientRect()
            return box.left < col.left - 1 || box.right > col.right + 1
          }).length,
        }))
        assert.deepEqual(wide, { page: false, cards: 0 })
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `backlog-board-${width}.png`) })
        await context.close()
      })
    }
  } finally {
    await browser.close()
  }
  console.log(results.join('\n'))
}
main().catch(error => { console.error(error); process.exit(1) })
