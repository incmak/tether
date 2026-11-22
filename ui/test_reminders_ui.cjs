// Chromium against a scratch build: DIST=<dir> node ui/test_reminders_ui.cjs
// Every API response is a fixture. Only the local static fixture server receives HTTP requests.
const assert = require('node:assert/strict')
const { readFile } = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const { test } = require('node:test')
const { getBrowser, serveLocal, prefsStore } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const NOW = Date.parse('2026-11-18T06:30:00Z') / 1000
const TOMORROW = Date.parse('2026-11-19T04:30:00Z') / 1000
const KOLKATA = 'Asia/Kolkata'
const KOLKATA_LABEL = new Intl.DateTimeFormat('en-US', { timeZone: KOLKATA, timeZoneName: 'short' })
  .formatToParts(NOW * 1000).find(part => part.type === 'timeZoneName').value
const API = '/mobile/api/'
const MISSING_OCC = 'Include the reminder occurrence (occ) as an integer.'
const STALE_OCC = 'This reminder changed after that notice. Open Reminders to see it.'
const PANE = { id: '%3', tmux_session: '$1', pane_name: 'Deploy check', label: 'project:0.3', agent: 'claude',
  dir: '/srv/project', state: 'Idle', writable: true, thread: 't3', approval: null, ask: null }
const fixtures = {
  chat: { messages: [], busy: false, thread: PANE.thread, agent: PANE.agent, writable: true, session: PANE.id, earlier: 0, limit: 100, state: 'Idle' },
  footer: { observed: false, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Fixture', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: PANE.id, running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
}
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }

function reminder(id, text, extra = {}) {
  return { id, text, due: TOMORROW, created: NOW - 7200, by: 'you', state: 'scheduled', fired_at: null,
    late: false, snoozes: 0, done_at: null, link: null, occ: 1, repeat: null, next_id: null, ...extra }
}

const repeatRule = (every, anchor) => ({ every, anchor, zone: 'Asia/Kolkata' })
/** The rule the server stores for a request's `repeat`: a rule name or { every, interval?, last_day? }. */
function ruleFrom(repeat, anchor) {
  if (!repeat) return null
  const spec = typeof repeat === 'string' ? { every: repeat } : repeat
  const date = new Date((anchor + 19800) * 1000)
  const rule = repeatRule(spec.every, anchor)
  if (spec.interval > 1) rule.interval = spec.interval
  if (spec.every === 'month' || spec.every === 'year') rule.day = spec.last_day ? -1 : date.getUTCDate()
  if (spec.every === 'year') rule.month = date.getUTCMonth() + 1
  return rule
}

async function fixtureServer(t) {
  await readFile(path.join(DIST, 'index.html'))
  const server = http.createServer(async (request, response) => {
    try {
      const { pathname } = new URL(request.url, 'http://localhost')
      if (request.method !== 'GET' || !pathname.startsWith('/chat/')) throw new Error('Only static chat files are served')
      const file = path.resolve(DIST, decodeURIComponent(pathname.slice('/chat/'.length)) || 'index.html')
      if (!file.startsWith(DIST + path.sep)) throw new Error('Outside the scratch build')
      const body = await readFile(file)
      response.writeHead(200, { 'Content-Type': mime[path.extname(file)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' })
      response.end(body)
    } catch { response.writeHead(404).end() }
  })
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections() }))
  return `http://127.0.0.1:${server.address().port}`
}

async function open(t, browser, origin, viewport, {
  records = [], panes = [PANE], selected = PANE.id, layout = 'islands', theme = 'light', now = NOW,
  loadGate, failFirst = false, go = true, zone = KOLKATA, holdPrefsSave = false,
} = {}) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block', reducedMotion: 'reduce', timezoneId: 'UTC' })
  const prefs = prefsStore({ zone })
  const errors = []
  const live = new Map(records.map(item => [item.id, item]))
  const deleted = new Map()
  let nextId = 10, reads = 0, closing = false
  let releasePrefsGet, releasePrefsSave
  const prefsPostStarted = new Promise(resolve => { releasePrefsGet = resolve })
  const prefsSaveReleased = new Promise(resolve => { releasePrefsSave = resolve })
  t.after(async () => {
    closing = true
    releasePrefsGet()
    releasePrefsSave()
    await context.close()
    assert.deepEqual(errors, [], 'uncaught page errors')
  })
  await context.routeWebSocket('**/*', socket => socket.close())
  const respond = async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== origin) return route.abort('blockedbyclient')
    if (url.pathname.startsWith(API)) {
      const endpoint = url.pathname.slice(API.length)
      if (endpoint === 'prefs') {
        const read = request.method() === 'GET'
        if (holdPrefsSave) {
          // Return the server zone while the local seen stamp is in flight.
          if (read) await prefsPostStarted
          else {
            releasePrefsGet()
            await prefsSaveReleased
          }
        }
        return route.fulfill({ json: read ? prefs.get() : prefs.post(request.postDataJSON()) })
      }
      if (endpoint === 'reminders') {
        const first = ++reads === 1
        if (first && loadGate) await loadGate
        if (first && failFirst) return route.fulfill({ status: 503, json: { error: 'Reminders fixture unavailable' } })
        return route.fulfill({ json: { reminders: [...live.values()], deleted: [...deleted.values()], now: NOW } })
      }
      if (endpoint.startsWith('reminders/')) {
        const action = endpoint.slice('reminders/'.length)
        const body = request.postDataJSON()
        const old = live.get(body.id)
        const reject = (status, error) => route.fulfill({ status, json: { error } })
        if (action === 'done' || action === 'snooze') {
          if (!Number.isInteger(body.occ)) return reject(400, MISSING_OCC)
          if (old && body.occ !== old.occ) return reject(409, STALE_OCC)
        }
        if (!old && action !== 'add' && action !== 'restore') {
          return reject(404, `No reminder ${body.id}. It may have been deleted.`)
        }
        let item
        switch (action) {
          case 'add': {
            item = reminder(`R${nextId++}`, body.text, {
              due: body.due, link: body.link ?? null, repeat: ruleFrom(body.repeat, body.due),
            })
            break
          }
          case 'done': {
            item = old.state === 'done' ? old : { ...old, state: 'done', done_at: NOW }
            if (item !== old && old.repeat) {
              const due = old.repeat.anchor + 86400
              const next = reminder(`R${nextId++}`, old.text, { due, created: NOW, by: old.by, link: old.link,
                repeat: { ...old.repeat, anchor: due } })
              live.set(next.id, next)
              item = { ...item, repeat: null, next_id: next.id }
            }
            break
          }
          case 'reopen': item = { ...old, state: 'due', done_at: null }; break
          case 'snooze': {
            if (old.state === 'done') return reject(409, 'That reminder is done. Reopen it first.')
            const due = 'for' in body ? NOW + body.for : body.until
            item = { ...old, state: 'scheduled', due, fired_at: null, late: false, occ: old.occ + 1, snoozes: old.snoozes + 1 }
            break
          }
          case 'edit': {
            const changedDue = 'due' in body && body.due !== old.due
            item = { ...old, ...('text' in body && { text: body.text }), ...(changedDue && { due: body.due, occ: old.occ + 1 }) }
            if ('repeat' in body) item.repeat = ruleFrom(body.repeat, old.repeat?.anchor ?? old.due)
            if (changedDue && item.repeat) item.repeat = { ...item.repeat, anchor: item.due }
            break
          }
          case 'delete': {
            live.delete(old.id)
            deleted.set(old.id, { ...old, deleted_at: NOW })
            item = null
            break
          }
          case 'restore': {
            if (old) { item = old; break }
            const record = deleted.get(body.id)
            if (!record) return reject(404, 'That reminder is no longer in Recently deleted.')
            item = { ...record }
            delete item.deleted_at
            deleted.delete(body.id)
            break
          }
          default: return reject(404, 'Unknown reminder action')
        }
        if (item) live.set(item.id, item)
        return route.fulfill({ json: { reminder: item } })
      }
      if (endpoint === 'stream') return route.fulfill({ status: 204 })
      if (endpoint === 'sessions') return route.fulfill({ json: { sessions: panes, default_thread: panes[0]?.thread ?? null } })
      if (endpoint === 'chat') {
        const pane = panes.find(item => item.id === url.searchParams.get('session')) ?? panes[0]
        return route.fulfill({ json: { ...fixtures.chat, session: pane?.id, thread: pane?.thread } })
      }
      return route.fulfill({ json: fixtures[endpoint] ?? {} })
    }
    if (url.pathname.startsWith('/chat/') && !url.pathname.endsWith('/sw.js')) return serveLocal(route)
    return route.abort('blockedbyclient')
  }
  await context.route('**/*', async route => {
    try { await respond(route) } catch (error) {
      // The app prefetches assets. Closing the context can cancel those requests.
      if (!closing) throw error
    }
  })
  await context.addInitScript(({ selected, layout, theme }) => {
    localStorage.setItem('tmux-chat.prefs.synced', '1')
    localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme, voiceOff: true, voiceSound: false,
      selected, look: { layout, surface: 'flat', backdrop: 'ember' } }))
  }, { selected, layout, theme })
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  page.on('pageerror', error => errors.push(error.message))
  await page.clock.setFixedTime(new Date(now * 1000))
  await page.goto(`${origin}/chat/${go ? '?go=reminders' : ''}`)
  const sheet = page.getByRole('dialog', { name: 'Reminders', exact: true })
  return { page, sheet, releasePrefsSave }
}

async function post(page, action, trigger, status = 200) {
  const [response] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === `${API}reminders/${action}`, { timeout: 5000 }),
    trigger(),
  ])
  const request = response.request()
  assert.equal(request.method(), 'POST')
  assert.equal(request.headers()['x-codex-mobile'], '1')
  assert.equal(response.status(), status)
  return request.postDataJSON()
}

function fixtureWrite(page, action, body) {
  return page.evaluate(async ({ url, body }) => {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' }, body: JSON.stringify(body),
    })
    return { status: response.status, data: await response.json() }
  }, { url: `${API}reminders/${action}`, body })
}

const button = (scope, name) => scope.getByRole('button', { name, exact: true })
const late = reminder('R1', 'Send the deploy note', { state: 'due', due: NOW - 3600, fired_at: NOW - 60, late: true, occ: 4 })
const upcoming = reminder('R2', 'Read the report', { link: { project: 'project' } })
const done = reminder('R3', 'File the receipt', { state: 'done', due: NOW - 7200, done_at: NOW - 3600 })
const due = reminder('R4', 'Check the queue', { state: 'due', due: NOW - 60, fired_at: NOW - 60, occ: 2 })

test('Reminders in Chromium', async t => {
  const origin = await fixtureServer(t)
  const browser = await getBrowser()
  t.after(() => browser.close())
  const empty = sheet => sheet.getByText(/^No reminders yet\./)

  for (const [width, height] of [[360, 780], [1280, 800]]) {
    const viewport = { width, height }
    await t.test(`${width}px deep link, loading, GET error, Retry and empty state`, async t => {
      let release
      const loadGate = new Promise(resolve => { release = resolve })
      const { sheet } = await open(t, browser, origin, viewport, { loadGate, failFirst: true })
      const skeleton = sheet.getByRole('list', { name: 'Loading reminders' })
      try { await skeleton.waitFor(); assert.equal(await skeleton.getByRole('listitem').count(), 3) } finally { release() }
      const error = sheet.getByRole('alert').filter({ hasText: 'Cannot load reminders: Reminders fixture unavailable' })
      await error.waitFor()
      assert.equal(await skeleton.count(), 0)
      await button(error, 'Retry').click()
      await empty(sheet).waitFor()
      assert.equal(await error.count(), 0)
    })

    await t.test(`${width}px validates When and adds reminders with and without a tab link`, async t => {
      const { page, sheet } = await open(t, browser, origin, viewport)
      await empty(sheet).waitFor()
      const text = sheet.getByRole('textbox', { name: 'Remind me to' })
      const when = sheet.getByRole('textbox', { name: `When (${KOLKATA_LABEL})` })
      const add = button(sheet, 'Add reminder')
      const link = sheet.getByRole('checkbox', { name: 'Link to this tab: Deploy check' })
      await text.fill('  Send the invoice  ')
      await when.fill('soon')
      await sheet.getByText('Not a time. Try "tomorrow 10:00" or "in 2 h".', { exact: true }).waitFor()
      assert.ok(await add.isDisabled())
      assert.equal(await when.getAttribute('aria-invalid'), 'true')
      await button(sheet, 'Tomorrow 10:00').click()
      await sheet.getByText(`Thu 19 Nov 2026, 10:00 ${KOLKATA_LABEL}`, { exact: true }).waitFor()
      assert.equal(await when.inputValue(), 'tomorrow 10:00')
      assert.deepEqual(await post(page, 'add', () => add.click()), { text: 'Send the invoice', due: TOMORROW, repeat: null })
      await sheet.getByText('Send the invoice', { exact: true }).waitFor()
      await text.fill('Check the deployment')
      await button(sheet, 'Tomorrow 10:00').click()
      await link.check()
      assert.deepEqual(await post(page, 'add', () => add.click()), { text: 'Check the deployment', due: TOMORROW, link: { pane: PANE.id, thread: PANE.thread }, repeat: null })
      const linked = sheet.getByRole('listitem').filter({ hasText: 'Check the deployment' })
      await button(linked, 'Deploy check').click()
      await sheet.waitFor({ state: 'hidden' })
    })

    await t.test(`${width}px section order, Sent late, and all row actions`, async t => {
      const { page, sheet } = await open(t, browser, origin, viewport, { records: [upcoming, done, late] })
      const dueSection = sheet.getByRole('region', { name: 'Due 1', exact: true })
      const upcomingSection = sheet.getByRole('region', { name: /^Upcoming / })
      await dueSection.waitFor()
      assert.deepEqual(await sheet.getByRole('heading', { level: 3 }).allTextContents(), ['Due 1', 'Upcoming 1', 'Done 1'])
      await dueSection.getByText('Sent late', { exact: true }).waitFor()
      const fold = button(sheet, 'Done 1')
      assert.equal(await fold.getAttribute('aria-expanded'), 'false')
      assert.equal(await sheet.getByText(done.text, { exact: true }).count(), 0)
      await fold.click()
      await sheet.getByText(done.text, { exact: true }).waitFor()
      assert.equal(await fold.getAttribute('aria-expanded'), 'true')
      assert.deepEqual(await post(page, 'done', () => button(dueSection, `Done: ${late.text}`).click()), { id: late.id, occ: late.occ })
      await dueSection.waitFor({ state: 'hidden' })
      assert.deepEqual(await post(page, 'reopen', () => button(sheet, `Reopen: ${late.text}`).click()), { id: late.id })
      await dueSection.getByText(late.text, { exact: true }).waitFor()

      // Two edits in a row: the second edit changes only the text, so it must not send the first edit's time again.
      const drafted = 'Draft the report'
      await button(sheet, `Edit: ${upcoming.text}`).click()
      const first = sheet.getByRole('form', { name: `Edit reminder: ${upcoming.text}` })
      await first.getByRole('textbox', { name: 'Reminder', exact: true }).fill(drafted)
      await first.getByRole('textbox', { name: /^When/ }).fill('in 2 h')
      const firstBody = await post(page, 'edit', () => button(first, 'Save').click())
      assert.deepEqual(Object.keys(firstBody).sort(), ['due', 'id', 'text'])
      assert.equal(firstBody.text, drafted)
      const updated = 'Finish the report'
      await button(sheet, `Edit: ${drafted}`).click()
      const edit = sheet.getByRole('form', { name: `Edit reminder: ${drafted}` })
      assert.equal(await edit.getByRole('textbox', { name: 'Reminder', exact: true }).inputValue(), drafted)
      assert.equal(await edit.getByRole('textbox', { name: /^When/ }).inputValue(), '')
      await edit.getByRole('textbox', { name: 'Reminder', exact: true }).fill(updated)
      assert.deepEqual(await post(page, 'edit', () => button(edit, 'Save').click()), { id: upcoming.id, text: updated })
      const remove = () => button(upcomingSection, `Delete: ${updated}`).click()
      const toast = sheet.getByRole('status').filter({ hasText: 'It stays in Recently deleted for 30 days.' })
      const deletedFold = button(sheet, 'Recently deleted 1')
      const deletedSection = sheet.getByRole('region', { name: 'Recently deleted 1', exact: true })
      const restore = button(deletedSection, `Restore: ${updated}`)

      assert.deepEqual(await post(page, 'delete', remove), { id: upcoming.id })
      await deletedFold.waitFor()
      assert.equal(await deletedFold.getAttribute('aria-expanded'), 'false')
      assert.equal(await restore.count(), 0)
      await toast.waitFor()
      assert.deepEqual(await post(page, 'restore', () => button(toast, 'Undo').click()), { id: upcoming.id })
      await upcomingSection.getByText(updated, { exact: true }).waitFor()
      await deletedFold.waitFor({ state: 'hidden' })

      assert.deepEqual(await post(page, 'delete', remove), { id: upcoming.id })
      await deletedFold.click()
      await restore.waitFor()
      assert.equal(await deletedFold.getAttribute('aria-expanded'), 'true')
      assert.deepEqual(await post(page, 'restore', () => restore.click()), { id: upcoming.id })
      await upcomingSection.getByText(updated, { exact: true }).waitFor()
      await deletedFold.waitFor({ state: 'hidden' })
    })

    await t.test(`${width}px opens from the sidebar and counts both due reminders in Catch up`, async t => {
      const { page, sheet } = await open(t, browser, origin, viewport, { records: [late, due], go: false })
      const side = width === 360 ? page.getByRole('dialog', { name: 'Sessions' }) : page.locator('aside.sidebar')
      const showSidebar = async () => {
        if (width === 360) await page.getByRole('button', { name: /^Open sessions/ }).click()
      }
      await showSidebar()
      const entry = button(side, 'Reminders, 2 due')
      await entry.getByText('2 due', { exact: true }).waitFor()
      await entry.click()
      await sheet.waitFor()
      await button(sheet, 'Close').click()
      await showSidebar()
      const names = [`Reminder: ${late.text}. Late, was due Today 11:00`, `Reminder: ${due.text}. Due Today 11:59`]
      const catchUp = side.getByRole('region', { name: /^Needs you/ })
      await button(catchUp, names[0]).waitFor()
      assert.equal(await catchUp.getByRole('button', { name: /^Reminder: / }).count(), 2)
      assert.equal((await catchUp.locator('.catchup-count').textContent()).trim(), '· 2')
      await button(catchUp, names[1]).click()
      await sheet.waitFor()
      if (width === 1280) {
        await button(sheet, 'Close').click()
        const waiting = page.locator('.waiting-box')
        await waiting.getByRole('heading', { name: /^Waiting on you/ }).waitFor()
        assert.equal((await waiting.locator('.waiting-count').textContent()).trim(), '2')
        assert.equal(await waiting.getByRole('button', { name: /^Reminder: / }).count(), 2)
        await button(waiting, names[1]).waitFor()
        await button(waiting, names[0]).click()
        await sheet.waitFor()
      }
    })

    for (const [action, name, extra] of [['done', 'Done', {}], ['snooze', 'Snooze 1 h', { for: 3600 }]]) {
      await t.test(`${width}px Home without agents posts ${name} and focuses the blank state heading`, async t => {
        const { page } = await open(t, browser, origin, viewport, { records: [late], panes: [], selected: null, go: false })
        const home = page.getByRole('region', { name: 'Home', exact: true })
        await home.getByRole('heading', { name: '1 reminder due', level: 1, exact: true }).waitFor()
        const waiting = home.getByRole('region', { name: 'Waiting on you', exact: true })
        const card = waiting.getByRole('group', { name: `Reminder: ${late.text}`, exact: true })
        await button(card, 'Done').waitFor()
        await button(card, 'Snooze 1 h').waitFor()
        await button(card, name).focus()
        assert.deepEqual(await post(page, action, () => page.keyboard.press('Enter')), { id: late.id, occ: late.occ, ...extra })
        await card.waitFor({ state: 'detached' })
        const blank = page.getByRole('heading', { name: 'Space for your next task.', level: 1, exact: true })
        await blank.and(page.locator(':focus')).waitFor()
      })
    }
  }

  await t.test('1280px Ctrl+K offers Reminders in the workspace', async t => {
    const { page, sheet } = await open(t, browser, origin, { width: 1280, height: 800 }, { go: false })
    await page.getByRole('heading', { name: PANE.pane_name, exact: true }).waitFor()
    await page.keyboard.press('Control+k')
    const menu = page.locator('dialog.command-menu')
    await menu.getByRole('combobox').fill('reminders')
    await menu.getByRole('option', { name: 'Reminders', exact: true }).click()
    await sheet.waitFor()
  })

  const phone = { width: 360, height: 780 }

  await t.test('adds a weekday reminder using the Repeat picker keyboard controls', async t => {
    const { page, sheet } = await open(t, browser, origin, { width: 1280, height: 800 })
    await sheet.getByRole('textbox', { name: 'Remind me to' }).fill('Check weekday reports')
    await button(sheet, 'Tomorrow 10:00').click()
    await button(sheet, 'Repeat: Never').focus()
    for (const key of ['Enter', 'ArrowDown', 'ArrowDown', 'Enter']) await page.keyboard.press(key)
    await button(sheet, 'Repeat: Every weekday').and(page.locator('[aria-expanded="false"]')).waitFor()
    assert.deepEqual(await post(page, 'add', () => button(sheet, 'Add reminder').click()), {
      text: 'Check weekday reports', due: TOMORROW, repeat: 'weekday',
    })
    const row = sheet.getByRole('listitem').filter({ hasText: 'Check weekday reports' })
    const label = row.locator('.reminder-repeat')
    await label.waitFor()
    assert.match(await label.ariaSnapshot(), /Repeats:\s+Every weekday/)
    assert.equal(await label.evaluate(element => element.lastChild.textContent), 'Every weekday')
    await button(sheet, 'Repeat: Never').waitFor()
  })

  await t.test('Custom repeat: every N units with the rule in words, and a bad N blocks Add', async t => {
    const { page, sheet } = await open(t, browser, origin, { width: 320, height: 700 })
    await sheet.getByRole('textbox', { name: 'Remind me to' }).fill('Pay the quarterly tax')
    await button(sheet, 'Tomorrow 10:00').click()
    await button(sheet, 'Repeat: Never').click()
    await sheet.getByRole('listbox', { name: 'Repeat' }).getByRole('option', { name: /^Custom/ }).click()
    const every = sheet.getByRole('spinbutton', { name: 'Repeat every' })
    assert.equal(await every.inputValue(), '2')
    const hint = sheet.locator('.reminder-every-hint')
    assert.equal(await hint.textContent(), 'Every 2 weeks on Thursday')
    await every.fill('0')
    assert.equal(await hint.textContent(), 'Type a number from 1 to 52.')
    assert.equal(await every.getAttribute('aria-invalid'), 'true')
    assert.equal(await button(sheet, 'Add reminder').isDisabled(), true)
    await every.fill('3')
    await button(sheet, 'Repeat unit: weeks').click()
    await sheet.getByRole('listbox', { name: 'Repeat unit' }).getByRole('option', { name: 'months' }).click()
    assert.equal(await hint.textContent(), 'Every 3 months on the 19th')
    assert.deepEqual(await post(page, 'add', () => button(sheet, 'Add reminder').click()), {
      text: 'Pay the quarterly tax', due: TOMORROW, repeat: { every: 'month', interval: 3 },
    })
    const label = sheet.getByRole('listitem').filter({ hasText: 'Pay the quarterly tax' }).locator('.reminder-repeat')
    await label.waitFor()
    assert.match(await label.ariaSnapshot(), /Repeats:\s+Every 3 months on the 19th/)
    await button(sheet, 'Repeat: Never').waitFor()
    assert.equal(await every.count(), 0, 'the form resets to Never')
  })

  await t.test('a 30 Nov reminder offers the last day of every month', async t => {
    const { page, sheet } = await open(t, browser, origin, phone)
    await sheet.getByRole('textbox', { name: 'Remind me to' }).fill('Send the rent')
    await sheet.getByRole('textbox', { name: /^When/ }).fill('2026-11-30 10:00')
    await button(sheet, 'Repeat: Never').click()
    const list = sheet.getByRole('listbox', { name: 'Repeat' })
    assert.match(await list.getByRole('option', { name: /^Every month/ }).textContent(), /On the 30th, or the last day/)
    await list.getByRole('option', { name: /^Last day of every month/ }).click()
    const due = Date.UTC(2026, 10, 30, 4, 30) / 1000
    assert.deepEqual(await post(page, 'add', () => button(sheet, 'Add reminder').click()), {
      text: 'Send the rent', due, repeat: { every: 'month', last_day: true },
    })
    const label = sheet.getByRole('listitem').filter({ hasText: 'Send the rent' }).locator('.reminder-repeat')
    await label.waitFor()
    assert.match(await label.ariaSnapshot(), /Every month on the last day/)
  })

  await t.test('Edit of a 28 Feb copy of a 31st series keeps the 31st and saves a custom interval', async t => {
    const feb28 = Date.UTC(2027, 1, 28, 4, 30) / 1000
    const monthly = reminder('R1', 'File the invoices', { due: feb28, repeat: { ...repeatRule('month', feb28), day: 31 } })
    const { page, sheet } = await open(t, browser, origin, phone, { records: [monthly] })
    const row = sheet.getByRole('listitem').filter({ hasText: monthly.text })
    assert.match(await row.locator('.reminder-repeat').ariaSnapshot(), /Every month on the 31st/)
    await button(sheet, `Edit: ${monthly.text}`).click()
    const edit = sheet.getByRole('form', { name: `Edit reminder: ${monthly.text}` })
    await button(edit, 'Repeat: Every month').click()
    const list = sheet.getByRole('listbox', { name: 'Repeat' })
    assert.match(await list.getByRole('option', { name: /^Every month/ }).textContent(), /On the 31st/)
    assert.equal(await list.getByRole('option', { name: /^Last day/ }).count(), 0)
    await list.getByRole('option', { name: /^Custom/ }).click()
    await button(edit, 'Repeat unit: weeks').click()
    await sheet.getByRole('listbox', { name: 'Repeat unit' }).getByRole('option', { name: 'months' }).click()
    assert.equal(await edit.locator('.reminder-every-hint').textContent(), 'Every 2 months on the 31st')
    assert.deepEqual(await post(page, 'edit', () => button(edit, 'Save').click()), { id: monthly.id, repeat: { every: 'month', interval: 2 } })
  })

  await t.test('Edit of an every-3-months last-day rule keeps the last day when N changes', async t => {
    const feb28 = Date.UTC(2027, 1, 28, 4, 30) / 1000
    const quarterly = reminder('R1', 'Close the books', { due: feb28, repeat: { ...repeatRule('month', feb28), interval: 3, day: -1 } })
    const { page, sheet } = await open(t, browser, origin, phone, { records: [quarterly] })
    await button(sheet, `Edit: ${quarterly.text}`).click()
    const edit = sheet.getByRole('form', { name: `Edit reminder: ${quarterly.text}` })
    const last = edit.getByRole('checkbox', { name: 'On the last day of the month' })
    assert.equal(await last.isChecked(), true)
    await edit.getByRole('spinbutton', { name: 'Repeat every' }).fill('4')
    assert.equal(await edit.locator('.reminder-every-hint').textContent(), 'Every 4 months on the last day')
    assert.deepEqual(await post(page, 'edit', () => button(edit, 'Save').click()),
      { id: quarterly.id, repeat: { every: 'month', interval: 4, last_day: true } })
    // Unchecked, the rule falls on the copy's date; another unit hides the checkbox.
    await button(sheet, `Edit: ${quarterly.text}`).click()
    await last.uncheck()
    assert.equal(await edit.locator('.reminder-every-hint').textContent(), 'Every 4 months on the 28th')
    await button(edit, 'Repeat unit: months').click()
    await sheet.getByRole('listbox', { name: 'Repeat unit' }).getByRole('option', { name: 'weeks' }).click()
    assert.equal(await last.count(), 0)
  })

  await t.test('weekly label uses the anchor even when the copy was snoozed', async t => {
    const weekly = reminder('R1', 'Review weekly reports', { due: TOMORROW + 86400, repeat: repeatRule('week', TOMORROW) })
    const { sheet } = await open(t, browser, origin, phone, { records: [weekly] })
    const row = sheet.getByRole('listitem').filter({ hasText: weekly.text })
    const label = row.locator('.reminder-repeat')
    await label.waitFor()
    assert.match(await label.ariaSnapshot(), /Repeats:\s+Every Thursday/)
  })

  await t.test('Stop repeating saves a rule-only edit and removes the label', async t => {
    const daily = reminder('R1', 'Review reports', { repeat: repeatRule('day', TOMORROW) })
    const { page, sheet } = await open(t, browser, origin, phone, { records: [daily] })
    await button(sheet, `Edit: ${daily.text}`).click()
    const edit = sheet.getByRole('form', { name: `Edit reminder: ${daily.text}` })
    await button(edit, 'Stop repeating').click()
    await edit.getByText('Repeating stops when you save.', { exact: true }).waitFor()
    assert.deepEqual(await post(page, 'edit', () => button(edit, 'Save').click()), { id: daily.id, repeat: null })
    const row = sheet.getByRole('listitem').filter({ hasText: daily.text })
    await row.locator('.reminder-text').waitFor()
    assert.equal(await row.locator('.reminder-text').textContent(), daily.text)
    await row.locator('.reminder-repeat').waitFor({ state: 'detached' })
    assert.equal(await row.locator('.reminder-repeat').count(), 0)
  })

  await t.test('Edit of a snoozed weekly copy names the series weekday in the Repeat menu, blank or re-typed', async t => {
    const weekly = reminder('R1', 'Review weekly reports', { due: TOMORROW + 86400, snoozes: 1, repeat: repeatRule('week', TOMORROW) })
    const { sheet } = await open(t, browser, origin, phone, { records: [weekly] })
    await button(sheet, `Edit: ${weekly.text}`).click()
    const edit = sheet.getByRole('form', { name: `Edit reminder: ${weekly.text}` })
    await button(edit, 'Repeat: Every week').click()
    const list = sheet.getByRole('listbox', { name: 'Repeat' })
    await list.waitFor()
    assert.match(await list.getByRole('option', { name: /^Every week(?!day)/ }).textContent(), /On Thursdays/)
    await list.press('Escape')
    // Re-typing the current (snoozed) due keeps the series day; a new time moves it.
    const when = edit.getByRole('textbox', { name: /^When/ })
    for (const [typed, day] of [['fri 10:00', 'Thursdays'], ['sat 10:00', 'Saturdays']]) {
      await when.fill(typed)
      await button(edit, 'Repeat: Every week').click()
      await list.waitFor()
      assert.match(await list.getByRole('option', { name: /^Every week(?!day)/ }).textContent(), new RegExp(`On ${day}`))
      await list.press('Escape')
    }
  })

  await t.test('a text-only edit keeps a repeat change that another tab made', async t => {
    const daily = reminder('R1', 'Review reports', { repeat: repeatRule('day', TOMORROW) })
    const { page, sheet } = await open(t, browser, origin, phone, { records: [daily] })
    await button(sheet, `Edit: ${daily.text}`).click()
    const edit = sheet.getByRole('form', { name: `Edit reminder: ${daily.text}` })
    await button(edit, 'Repeat: Every day').waitFor()
    // Another tab stops the rule; this tab's next load shows it while the edit stays open.
    assert.equal((await fixtureWrite(page, 'edit', { id: daily.id, repeat: null })).status, 200)
    await page.evaluate(() => window.dispatchEvent(new Event('tether-reminders-changed')))
    await button(edit, 'Repeat: Never').waitFor()
    await edit.getByRole('textbox', { name: 'Reminder' }).fill('Review the reports')
    assert.deepEqual(await post(page, 'edit', () => button(edit, 'Save').click()), { id: daily.id, text: 'Review the reports' })
    const row = sheet.getByRole('listitem').filter({ hasText: 'Review the reports' })
    await row.locator('.reminder-text').waitFor()
    assert.equal(await row.locator('.reminder-repeat').count(), 0)
  })

  await t.test('Done moves the repeat label to the next Upcoming copy', async t => {
    const daily = { ...late, repeat: repeatRule('day', late.due) }
    const { page, sheet } = await open(t, browser, origin, phone, { records: [daily] })
    assert.deepEqual(await post(page, 'done', () => button(sheet, `Done: ${daily.text}`).click()), { id: daily.id, occ: daily.occ })
    const upcoming = sheet.getByRole('region', { name: 'Upcoming 1', exact: true })
    const label = upcoming.locator('.reminder-repeat')
    await label.waitFor()
    assert.match(await label.ariaSnapshot(), /Repeats:\s+Every day/)
    await button(sheet, 'Done 1').click()
    const finished = sheet.getByRole('region', { name: 'Done 1', exact: true }).getByRole('listitem')
    await finished.locator('.reminder-text').waitFor()
    assert.equal(await finished.locator('.reminder-text').textContent(), daily.text)
    assert.equal(await finished.locator('.reminder-repeat').count(), 0)
  })

  await t.test('320px add form fits and the Repeat trigger names its selected value', async t => {
    const { sheet } = await open(t, browser, origin, { width: 320, height: 780 })
    const form = sheet.getByRole('form', { name: 'New reminder' })
    const repeat = button(form, 'Repeat: Never')
    await repeat.focus()
    await repeat.press('ArrowDown')
    const list = sheet.getByRole('listbox', { name: 'Repeat' })
    await list.waitFor()
    assert.deepEqual(await list.getByRole('option').evaluateAll(options => options.map(option => option.querySelector('.sm-label').textContent)),
      ['Never', 'Every day', 'Every weekday', 'Every week', 'Every month', 'Every year', 'Custom'])
    const box = await list.boundingBox()
    assert.ok(box.x >= 0 && box.x + box.width <= 320, `the Repeat menu fits 320 px: ${JSON.stringify(box)}`)
    await list.press('Escape')
    const layout = await form.evaluate(form => ({
      viewport: innerWidth, page: document.documentElement.scrollWidth, form: form.clientWidth, content: form.scrollWidth,
    }))
    assert.equal(layout.viewport, 320)
    assert.ok(layout.page <= layout.viewport)
    assert.ok(layout.content <= layout.form)
  })

  for (const [label, clickTime, expected, eveningBefore] of [
    ['In 1 h', Date.parse('2026-11-18T11:29:00Z') / 1000, Date.parse('2026-11-18T12:29:00Z') / 1000, true],
    ['Tomorrow 10:00', Date.parse('2026-11-18T18:29:00Z') / 1000, TOMORROW, false],
  ]) {
    await t.test(`${label} keeps its picked instant across the sheet clock refresh`, async t => {
      const { page, sheet } = await open(t, browser, origin, phone, { now: clickTime })
      const evening = button(sheet, 'This evening 18:00')
      await sheet.getByRole('textbox', { name: 'Remind me to' }).fill('Keep the time I picked')
      assert.equal(await evening.isVisible(), eveningBefore)
      await button(sheet, label).click()
      await page.clock.setFixedTime(new Date((clickTime + 120) * 1000))
      await page.clock.fastForward(120000)
      // Crossing 17:00 or midnight changes the evening pick only after the sheet reads the new time.
      await evening.waitFor({ state: eveningBefore ? 'hidden' : 'visible' })
      assert.deepEqual(await post(page, 'add', () => button(sheet, 'Add reminder').click()), {
        text: 'Keep the time I picked', due: expected, repeat: null,
      })
    })
  }

  await t.test('sheet snooze sends durations for 10 min and 1 h and an instant for Tomorrow', async t => {
    const next = reminder('R5', 'Review the log', { state: 'due', due: NOW - 300, occ: 7 })
    const { page, sheet } = await open(t, browser, origin, phone, { records: [late, due, next] })
    const upcoming = sheet.getByRole('region', { name: /^Upcoming / })
    for (const [item, name, timing, at] of [
      [late, /^10 min\b/, { for: 600 }, NOW + 600],
      [due, /^1 h\b/, { for: 3600 }, NOW + 3600],
      [next, /^Tomorrow 10:00\b/, { until: TOMORROW }, TOMORROW],
    ]) {
      await button(sheet, `Snooze: ${item.text}`).click()
      const option = page.getByRole('listbox', { name: 'Snooze' }).getByRole('option', { name })
      assert.deepEqual(await post(page, 'snooze', () => option.click()), { id: item.id, occ: item.occ, ...timing })
      const row = upcoming.getByRole('listitem').filter({ hasText: item.text })
      const datetime = new Date(at * 1000).toISOString()
      await row.locator(`time[datetime="${datetime}"]`).waitFor()
    }
  })

  await t.test('Done and Snooze require occ, and a stale Done shows the 409 message', async t => {
    const { page, sheet } = await open(t, browser, origin, phone, { records: [late] })
    const doneButton = button(sheet, `Done: ${late.text}`)
    await doneButton.waitFor()
    for (const [action, extra] of [['done', {}], ['snooze', { for: 3600 }]]) {
      assert.deepEqual(await fixtureWrite(page, action, { id: late.id, ...extra }), { status: 400, data: { error: MISSING_OCC } })
    }
    // A write from another client changes the occurrence without refreshing this sheet.
    const snooze = { id: late.id, occ: late.occ, for: 3600 }
    const updated = await fixtureWrite(page, 'snooze', snooze)
    assert.equal(updated.status, 200)
    assert.equal(updated.data.reminder.occ, late.occ + 1)
    assert.deepEqual(await fixtureWrite(page, 'snooze', snooze), { status: 409, data: { error: STALE_OCC } })
    assert.deepEqual(await post(page, 'done', () => doneButton.click(), 409), { id: late.id, occ: late.occ })
    await sheet.getByRole('alert').getByText(STALE_OCC, { exact: true }).waitFor()
  })

  const linked = { ...upcoming, link: { pane: PANE.id, thread: PANE.thread } }
  const replaced = { ...PANE, thread: 'replacement' }
  const moved = { ...PANE, id: '%8', pane_name: 'Moved conversation', label: 'project:0.8' }
  await t.test('a linked reminder opens the matching thread in its new pane', async t => {
    const { page, sheet } = await open(t, browser, origin, phone, { records: [linked], panes: [replaced, moved] })
    const row = sheet.getByRole('listitem').filter({ hasText: linked.text })
    await button(row, moved.pane_name).waitFor()
    assert.equal(await button(row, PANE.pane_name).count(), 0)
    await button(row, moved.pane_name).click()
    await sheet.waitFor({ state: 'hidden' })
    await page.getByRole('heading', { name: moved.pane_name, exact: true }).waitFor()
  })

  await t.test('a linked reminder has no tab link when its thread has closed', async t => {
    const { sheet } = await open(t, browser, origin, phone, { records: [linked], panes: [replaced] })
    const row = sheet.getByRole('listitem').filter({ hasText: linked.text })
    await row.getByText('Conversation closed', { exact: true }).waitFor()
    assert.equal(await row.locator('.reminder-links').getByRole('button').count(), 0)
  })

  for (const width of [320, 1280]) {
    await t.test(`${width}px uses the synced zone to schedule a typed time after a DST change`, async t => {
      const now = Date.parse('2026-03-07T16:00:00Z') / 1000
      const due = Date.parse('2026-03-08T14:00:00Z') / 1000
      const { page, sheet, releasePrefsSave } = await open(t, browser, origin, { width, height: 800 }, {
        now, zone: 'America/New_York', holdPrefsSave: width === 320,
      })
      await sheet.getByText(/Times are in EST \(America\/New_York\)/).waitFor()
      releasePrefsSave()
      await sheet.getByRole('textbox', { name: 'Remind me to' }).fill('Check the rollout')
      await sheet.getByRole('textbox', { name: 'When (EST)' }).fill('tomorrow 10:00')
      await sheet.getByText('Sun 8 Mar 2026, 10:00 EDT', { exact: true }).waitFor()
      assert.deepEqual(await post(page, 'add', () => button(sheet, 'Add reminder').click()), { text: 'Check the rollout', due, repeat: null })
      await sheet.getByRole('listitem').filter({ hasText: 'Check the rollout' }).getByText('Tomorrow 10:00').waitFor()
      assert.ok(await sheet.evaluate(dialog => dialog.scrollWidth <= dialog.clientWidth), 'zone guidance fits the sheet')
    })
  }

  await t.test('Home keeps the IST time after two seconds and moves focus to the next card after Done', async t => {
    const working = { ...PANE, state: 'Working', since: NOW - 10 }
    const { page, sheet } = await open(t, browser, origin, phone, { records: [late], panes: [working], selected: null, go: false })
    const waiting = page.getByRole('region', { name: 'Waiting on you', exact: true })
    const openReminder = button(waiting, `Reminder: ${late.text}. Late, was due Today 11:00. Open Reminders`)
    const next = page.getByRole('region', { name: 'Working', exact: true }).getByRole('button', { name: /Deploy check/ })
    const agentTimer = next.locator('.home-time')
    const time = openReminder.locator('time')
    assert.equal(await agentTimer.textContent(), '10s')
    assert.equal(await time.textContent(), 'Today 11:00')
    await page.clock.setFixedTime(new Date((NOW + 2) * 1000))
    await page.clock.runFor(2000)
    await agentTimer.and(page.getByText('12s', { exact: true })).waitFor()
    assert.equal(await time.textContent(), 'Today 11:00')
    assert.equal(await time.getAttribute('title'), `Wed 18 Nov 2026, 11:00 ${KOLKATA_LABEL}`)
    await openReminder.click()
    await sheet.waitFor()
    await button(sheet, 'Close').click()
    const actions = waiting.getByRole('group', { name: `Reminder: ${late.text}`, exact: true })
    await button(actions, 'Done').focus()
    assert.deepEqual(await post(page, 'done', () => page.keyboard.press('Enter')), { id: late.id, occ: late.occ })
    await openReminder.waitFor({ state: 'detached' })
    await next.and(page.locator(':focus')).waitFor()
  })

  for (const theme of ['light', 'dark']) {
    await t.test(`When input border meets 3:1 contrast in ${theme} theme`, async t => {
      const { page, sheet } = await open(t, browser, origin, phone, { layout: 'classic', theme })
      await page.locator(`html[data-theme="${theme}"]`).waitFor({ state: 'attached' })
      const result = await sheet.getByRole('textbox', { name: `When (${KOLKATA_LABEL})` }).evaluate(input => {
        const border = getComputedStyle(input).borderTopColor
        const background = getComputedStyle(input.closest('dialog')).backgroundColor
        // Canvas converts computed colors such as color(srgb ...) to sRGB channels.
        const canvas = document.createElement('canvas')
        canvas.width = canvas.height = 1
        const pixel = canvas.getContext('2d', { willReadFrequently: true })
        const luminance = () => {
          const [r, g, b] = Array.from(pixel.getImageData(0, 0, 1, 1).data).slice(0, 3).map(channel => {
            const value = channel / 255
            return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
          })
          return 0.2126 * r + 0.7152 * g + 0.0722 * b
        }
        pixel.fillStyle = background
        pixel.fillRect(0, 0, 1, 1)
        const bg = luminance()
        pixel.fillStyle = border
        pixel.fillRect(0, 0, 1, 1)
        const fg = luminance()
        return { border, background, ratio: (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05) }
      })
      assert.ok(result.ratio >= 3, `${theme} When border ${result.border} against sheet ${result.background}: ${result.ratio.toFixed(2)}:1`)
    })
  }

  await t.test('320px fits a 500-character reminder and names every row action', async t => {
    const long = { ...late, text: 'X'.repeat(500) }
    const { page, sheet } = await open(t, browser, origin, { width: 320, height: 780 }, { records: [long, done] })
    await sheet.getByText(long.text, { exact: true }).waitFor()
    await button(sheet, 'Done 1').click()
    const buttons = sheet.locator('.reminder-actions button')
    await button(sheet, `Reopen: ${done.text}`).waitFor()
    assert.equal(await buttons.count(), 6)
    for (const action of ['Done', 'Snooze', 'Edit', 'Delete']) assert.equal(await buttons.and(button(sheet, `${action}: ${long.text}`)).count(), 1)
    for (const action of ['Reopen', 'Delete']) assert.equal(await buttons.and(button(sheet, `${action}: ${done.text}`)).count(), 1)
    await button(sheet, `Delete: ${done.text}`).click()
    await button(sheet, 'Recently deleted 1').click()
    const restore = buttons.and(button(sheet, `Restore: ${done.text}`))
    await restore.waitFor()
    assert.equal(await restore.count(), 1)
    const { scrollWidth, sheetWidth, sheetScroll, width } = await sheet.evaluate(element => ({
      scrollWidth: document.scrollingElement.scrollWidth, sheetWidth: element.clientWidth, sheetScroll: element.scrollWidth, width: innerWidth,
    }))
    assert.equal(width, 320)
    assert.ok(scrollWidth <= width, `document scroll width ${scrollWidth} exceeds viewport ${width}`)
    assert.ok(sheetScroll <= sheetWidth, `sheet scroll width ${sheetScroll} exceeds sheet width ${sheetWidth}`)
  })
})
