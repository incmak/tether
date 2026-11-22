// Several feeds per work-board project (src/WorkForms.tsx, server work_items.py and work_feeds.py): the feed rows,
// Add feed and Remove with focus, the empty state, each row's status (ok, failed read, Teams off, agent), the save body,
// a save error, and no sideways scroll at 320, 768 and 1280 px.
// Real browser against a build: DIST (default dist). Run: node test_work_feeds_ui.cjs
const { strict: assert } = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')
const { getBrowser, prefsStore } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS
const ORIGIN = 'http://tether.test'
const NOW = Math.floor(Date.now() / 1000)
const fixtures = {
  sessions: { sessions: [], default_thread: '' },
  'projects/marks': {}, 'projects/backlog': { text: '', version: 'v', limit: 200000, next_id: 1 },
  'side-shells': { shells: [] }, update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, usage: { claude: null, codex: null },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
}
const FEEDS = [
  { id: 'f1', kind: 'github', target: 'acme/shop', by: 'tether', since: NOW - 86400 },
  { id: 'a1b2c3', kind: 'email', target: 'Support', by: 'tether', since: NOW - 3600 },
  { id: 'd4e5f6', kind: 'teams', target: 'Shop/Bugs', by: 'tether', since: NOW - 3600 },
  { id: 'aa11bb', kind: 'github', target: 'acme/app', by: 'agent', since: NOW - 3600 },
]
const state = (feed, status, extra = {}) => ({ id: feed.id, kind: feed.kind, target: feed.target, by: feed.by, status, at: NOW - 60, error: '', errors: 0, items: 0, ...extra })
const board = feeds => ({
  revision: 7, stages: [], jira: { connected: false }, backlog: { items: [], error: '' },
  graph: { connected: true, account: 'me@example.com', mail: true, teams: false },
  projects: [{ name: 'shop', prefix: 'T', folder: '/work/shop', triage: '', work: '', feeds, jira: '', qaStatus: '', jiraUser: '' }],
  feeds: { shop: [state(FEEDS[0], 'ok', { items: 3 }), state(FEEDS[1], 'error', { error: 'Microsoft Graph answered 503.', errors: 2 }), state(FEEDS[2], 'off', { error: 'Teams channels need admin consent.' })] },
  items: [],
})
const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }

const contexts = []
async function open(browser, viewport, options = {}) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block' })
  contexts.push(context)
  const posts = [], errors = []
  const prefs = prefsStore()
  await context.route(`${ORIGIN}/**`, route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.startsWith('/mobile/api/')) {
      const name = url.pathname.slice('/mobile/api/'.length)
      if (name === 'prefs') return route.fulfill({ json: request.method() === 'GET' ? prefs.get() : prefs.post(request.postDataJSON()) })
      if (request.method() === 'POST') {
        if (name !== 'work/project') return route.fulfill({ json: {} })
        posts.push(request.postDataJSON())
        if (options.refuse) return route.fulfill({ status: 400, json: { error: 'Feed 2 reads the same place as an earlier feed.' } })
        return route.fulfill({ json: { revision: 8, projects: board(FEEDS).projects } })
      }
      if (name === 'work/board') return route.fulfill({ json: board(options.feeds ?? FEEDS) })
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
  await page.getByRole('button', { name: 'Settings of shop' }).click()
  const form = page.locator('dialog.work-form-sheet')
  await form.waitFor()
  return { context, page, form, posts, errors }
}

const feed = (form, n) => form.locator('.work-feed').nth(n - 1)
const note = async (form, n) => (await feed(form, n).locator('.work-feed-note').textContent()).trim()

async function main() {
  const browser = await getBrowser()
  const results = []
  const check = async (name, fn) => {
    try { await fn(); results.push(`ok ${name}`) } catch (error) { results.push(`FAIL ${name}: ${error.message.split('\n').slice(0, 12).join(' ')}`); process.exitCode = 1 }
    // A failed check leaves its tab open; the shared browser allows only 3.
    for (const context of contexts.splice(0)) await context.close().catch(() => {})
  }
  try {
    await check('each feed shows its own status', async () => {
      const { context, form, errors } = await open(browser, { width: 1280, height: 900 })
      assert.equal(await form.locator('.work-feed').count(), 4)
      assert.equal(await form.getByRole('group', { name: 'Feed 4' }).count(), 1)
      assert.match(await note(form, 1), /^Last read at .+\. 3 items added so far\.$/)
      assert.match(await note(form, 2), /^The last read failed at .+: Microsoft Graph answered 503\.$/)
      assert.match(await note(form, 3), /admin consent for ChannelMessage\.Read\.All/)
      assert.match(await note(form, 4), /Tether does not read this feed/)
      assert.deepEqual(await form.locator('.work-feed-note.failed').evaluateAll(els => els.map(el => el.closest('.work-feed') && [...el.closest('.work-feeds').children].indexOf(el.closest('.work-feed')) + 1)), [2, 3])
      // Each target field names its status note, so a screen reader reads the error with the field.
      const described = await feed(form, 2).locator('input:not([type])').evaluate(el => el.getAttribute('aria-describedby').split(' ').map(id => document.getElementById(id)?.textContent ?? ''))
      assert.ok(described.some(text => text.includes('503')))
      assert.deepEqual(errors, [])
      await context.close()
    })
    await check('Add feed and Remove move focus and save the list', async () => {
      const { context, page, form, posts } = await open(browser, { width: 768, height: 900 })
      await form.getByRole('button', { name: 'Remove feed 2' }).click()
      assert.equal(await form.locator('.work-feed').count(), 3)
      await page.waitForFunction(() => document.activeElement?.textContent === 'Add feed')
      await form.getByRole('button', { name: 'Add feed' }).click()
      assert.equal(await form.locator('.work-feed').count(), 4)
      const added = feed(form, 4)
      await page.waitForFunction(() => document.activeElement?.matches('.work-feed:last-child select'))
      assert.equal(await added.locator('select').inputValue(), 'email', 'Add feed picks a source not used yet')
      assert.match(await note(form, 4), /starts with messages that come after you save/)
      await added.locator('input:not([type])').fill('  Orders  ')
      // Changing a saved feed says its start moves to the save.
      await feed(form, 1).locator('input:not([type])').fill('acme/store')
      assert.match(await note(form, 1), /^Changed:/)
      await form.getByRole('button', { name: /^Save/ }).click()
      await page.locator('dialog.work-form-sheet').waitFor({ state: 'detached' })
      assert.equal(posts.length, 1)
      assert.deepEqual(posts[0].project.feeds, [
        { id: 'f1', kind: 'github', target: 'acme/store', by: 'tether' },
        { id: 'd4e5f6', kind: 'teams', target: 'Shop/Bugs', by: 'tether' },
        { id: 'aa11bb', kind: 'github', target: 'acme/app', by: 'agent' },
        { kind: 'email', target: 'Orders', by: 'tether' },
      ])
      assert.equal(posts[0].revision, 7)
      assert.equal('feed' in posts[0].project || 'feedTarget' in posts[0].project, false, 'the old single-feed fields are gone')
      await context.close()
    })
    await check('no feeds shows the empty state, and the agent box works', async () => {
      const { context, page, form, posts } = await open(browser, { width: 320, height: 800 }, { feeds: [] })
      await form.getByText('No feeds. Add items by hand with New item, or add a feed.').waitFor()
      await form.getByRole('button', { name: 'Add feed' }).click()
      assert.equal(await form.locator('.work-feeds-empty').count(), 0)
      assert.equal(await feed(form, 1).locator('select').inputValue(), 'github')
      await feed(form, 1).locator('input:not([type])').fill('acme/shop')
      await feed(form, 1).getByRole('checkbox').check()
      assert.match(await note(form, 1), /Tether does not read this feed/)
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, 'feeds-added-320.png'), fullPage: true })
      await form.getByRole('button', { name: 'Remove feed 1' }).click()
      await form.getByText('No feeds.', { exact: false }).waitFor()
      await form.getByRole('button', { name: /^Save/ }).click()
      await page.locator('dialog.work-form-sheet').waitFor({ state: 'detached' })
      assert.deepEqual(posts[0].project.feeds, [])
      await context.close()
    })
    await check('a refused save keeps the rows and shows the reason', async () => {
      const { context, page, form } = await open(browser, { width: 768, height: 900 }, { refuse: true })
      await form.getByRole('button', { name: /^Save/ }).click()
      await form.getByText('Feed 2 reads the same place as an earlier feed.').waitFor()
      assert.equal(await form.locator('.work-feed').count(), 4)
      assert.ok(await page.locator('dialog.work-form-sheet').isVisible())
      await context.close()
    })
    await check('Add feed stops at 8', async () => {
      const { context, form } = await open(browser, { width: 1280, height: 900 })
      for (let i = 0; i < 4; i++) await form.getByRole('button', { name: 'Add feed' }).click()
      assert.equal(await form.locator('.work-feed').count(), 8)
      assert.equal(await form.getByRole('button', { name: 'Add feed' }).isDisabled(), true)
      await form.getByText('A project reads at most 8 feeds.').waitFor()
      await context.close()
    })
    for (const width of [320, 768, 1280]) {
      await check(`feed rows fit at ${width}px`, async () => {
        const { context, page, form } = await open(browser, { width, height: 900 })
        const wide = await page.evaluate(() => ({
          page: document.documentElement.scrollWidth > innerWidth,
          rows: [...document.querySelectorAll('.work-feed fieldset > *, .work-feed-fields > *')].filter(el => {
            const box = el.getBoundingClientRect(), row = el.closest('.work-feed').getBoundingClientRect()
            return box.left < row.left - 1 || box.right > row.right + 1
          }).map(el => el.className || el.tagName),
        }))
        assert.deepEqual(wide, { page: false, rows: [] })
        // Remove is a real target: 24 px is the WCAG 2.2 AA minimum.
        const box = await form.getByRole('button', { name: 'Remove feed 1' }).boundingBox()
        assert.ok(box.height >= 24 && box.width >= 24)
        if (SHOTS) {
          await feed(form, 1).scrollIntoViewIfNeeded()
          await page.screenshot({ path: path.join(SHOTS, `feeds-form-${width}.png`) })
        }
        await context.close()
      })
    }
  } finally {
    await browser.close()
  }
  console.log(results.join('\n'))
}
main().catch(error => { console.error(error); process.exit(1) })
