// Todos tab of the Reminders sheet in Chromium against a scratch build: DIST=<dir> node ui/test_todos_ui.cjs
// Every API response is a fixture. Only the local static fixture server receives HTTP requests.
const assert = require('node:assert/strict')
const { readFile } = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const { test } = require('node:test')
const { getBrowser, serveLocal } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const NOW = Date.parse('2026-11-19T06:30:00Z') / 1000  // 12:00 IST
const API = '/mobile/api/'
const PANE = { id: '%3', tmux_session: '$1', pane_name: 'Deploy check', label: 'project:0.3', agent: 'claude',
  dir: '/srv/project', state: 'Idle', writable: true, thread: 't3', approval: null, ask: null }
const fixtures = {
  chat: { messages: [], busy: false, thread: PANE.thread, agent: PANE.agent, writable: true, session: PANE.id, earlier: 0, limit: 100, state: 'Idle' },
  footer: { observed: false, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Fixture', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: PANE.id, running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  reminders: { reminders: [{ id: 'R41', text: 'Pay electricity bill', due: NOW + 3600, created: NOW - 7200, by: 'you', state: 'scheduled',
    fired_at: null, late: false, snoozes: 0, done_at: null, link: null, occ: 1, repeat: null, next_id: null,
    source: { key: 'todo:2', label: 'Microsoft To Do' } }], deleted: [], now: NOW },
}
const COMMAND = 'tether graph connect --tenant <tenant id> --client <app id> --feeds mail,tasks'
const source = (id, label, extra = {}) => ({ id, label, enabled: true, alerts: false, state: 'ok', syncedAt: NOW - 240, complete: true, count: 0, ...extra })
const task = (key, title, due, extra = {}) => ({ key, source: key.split(':')[0], list: 'Tasks', title, due, reminderAt: null,
  importance: 'normal', link: 'https://to-do.office.com/tasks/', backlog: null, reminder: null, ...extra })
const LIVE = () => ({
  sources: [source('todo', 'Microsoft To Do', { alerts: true, count: 3 }), source('planner', 'Planner', { count: 1 })],
  tasks: [
    task('todo:1', 'Renew the passport', { date: '2026-11-17', at: null }, { importance: 'high' }),
    task('todo:2', 'Pay electricity bill', { date: '2026-11-19', at: null }, { reminder: 'R41' }),
    task('planner:3', 'Book venue', { date: '2026-11-25', at: null }, { list: 'Launch plan', link: 'https://planner.cloud.microsoft/' }),
    task('todo:5', 'Call the bank', { date: '2026-11-26', at: Date.parse('2026-11-26T04:30:00Z') / 1000 }, { timeUncertain: true }),
    task('todo:4', 'Read a book', null),
  ],
})
const OFF = {
  sources: ['todo', 'planner'].map(id => source(id, id === 'todo' ? 'Microsoft To Do' : 'Planner',
    { state: 'off', syncedAt: null, complete: false, why: 'Tether has no permission to read tasks.', command: COMMAND })),
  tasks: [],
}

const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }

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

async function open(t, browser, origin, viewport, { listing = LIVE(), failFirst = false } = {}) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block', reducedMotion: 'reduce' })
  const errors = [], posts = []
  let reads = 0, closing = false, nextId = 500
  t.after(async () => { closing = true; await context.close(); assert.deepEqual(errors, [], 'uncaught page errors') })
  await context.routeWebSocket('**/*', socket => socket.close())
  const respond = async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== origin) return route.abort('blockedbyclient')
    if (url.pathname.startsWith(API)) {
      const endpoint = url.pathname.slice(API.length)
      if (endpoint === 'tasks') {
        if (++reads === 1 && failFirst) return route.fulfill({ status: 503, json: { error: 'Tasks fixture unavailable' } })
        return route.fulfill({ json: listing })
      }
      if (endpoint.startsWith('tasks/')) {
        const body = request.postDataJSON()
        posts.push({ endpoint, body })
        if (endpoint === 'tasks/backlog') {
          const item = listing.tasks.find(entry => entry.key === body.key)
          if (!item) return route.fulfill({ status: 404, json: { error: 'No such task' } })
          const created = !item.backlog
          item.backlog ||= `B${nextId++}`
          return route.fulfill({ json: { id: item.backlog, created } })
        }
        if (endpoint === 'tasks/settings') Object.assign(listing.sources.find(entry => entry.id === body.source), body, { id: body.source })
        return route.fulfill({ json: listing })
      }
      if (endpoint === 'stream') return route.fulfill({ status: 204 })
      if (endpoint === 'sessions') return route.fulfill({ json: { sessions: [PANE], default_thread: PANE.thread } })
      return route.fulfill({ json: fixtures[endpoint] ?? {} })
    }
    if (url.pathname.startsWith('/chat/') && !url.pathname.endsWith('/sw.js')) return serveLocal(route)
    return route.abort('blockedbyclient')
  }
  await context.route('**/*', async route => {
    try { await respond(route) } catch (error) { if (!closing) throw error }
  })
  await context.addInitScript(selected => {
    localStorage.setItem('tmux-chat.prefs.synced', '1')
    localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected }))
  }, PANE.id)
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  page.on('pageerror', error => errors.push(error.message))
  await page.clock.setFixedTime(new Date(NOW * 1000))
  await page.goto(`${origin}/chat/?go=reminders`)
  const sheet = page.getByRole('dialog', { name: 'Reminders', exact: true })
  await sheet.waitFor()
  return { page, sheet, posts }
}

const button = (scope, name) => scope.getByRole('button', { name, exact: true })
const tab = (sheet, name) => sheet.getByRole('tab', { name: new RegExp(`^${name}`) })
async function toTodos(page, sheet) {
  await tab(sheet, 'Reminders').focus()
  await page.keyboard.press('ArrowRight')
  const todos = tab(sheet, 'Todos')
  assert.equal(await todos.getAttribute('aria-selected'), 'true')
  assert.equal(await todos.evaluate(node => node === document.activeElement), true, 'ArrowRight moves focus to Todos')
  assert.equal(await tab(sheet, 'Reminders').getAttribute('tabindex'), '-1')
}

test('Todos in Chromium', async t => {
  const origin = await fixtureServer(t)
  const browser = await getBrowser()
  t.after(() => browser.close())

  for (const [width, height] of [[360, 780], [1280, 800]]) {
    const viewport = { width, height }
    await t.test(`${width}px tabs, groups, fold and counts`, async t => {
      const { page, sheet } = await open(t, browser, origin, viewport)
      await toTodos(page, sheet)
      await sheet.getByText('Pay electricity bill').waitFor()
      assert.match(await tab(sheet, 'Todos').innerText(), /5/)
      await sheet.getByText('Thu 26 Nov 10:00 (time zone unclear)').waitFor()
      const headings = await sheet.getByRole('tabpanel').locator('h3').allTextContents()
      assert.deepEqual(headings.map(text => text.replace(/\s+\d+$/, '').trim()), ['Overdue', 'Today', 'Upcoming', 'No due date', 'Task apps'])
      const fold = button(sheet, 'No due date 1')
      assert.equal(await fold.getAttribute('aria-expanded'), 'false')
      assert.equal(await sheet.getByText('Read a book').count(), 0)
      await fold.click()
      await sheet.getByText('Read a book').waitFor()
      await sheet.getByRole('img', { name: 'Important' }).waitFor()
      const link = sheet.getByRole('link', { name: 'Open in Planner: Book venue' })
      await link.focus()
      assert.equal(await link.getAttribute('target'), '_blank')
      assert.match(await link.getAttribute('rel'), /noopener/)
      await page.keyboard.press('ArrowLeft')  // focus is on a link in the panel: the arrow does not switch tabs
      assert.equal(await tab(sheet, 'Todos').getAttribute('aria-selected'), 'true')
      await tab(sheet, 'Todos').focus()
      await page.keyboard.press('ArrowLeft')
      assert.equal(await tab(sheet, 'Reminders').getAttribute('aria-selected'), 'true')
      await sheet.getByRole('textbox', { name: 'Remind me to' }).waitFor()
      await sheet.getByText('From Microsoft To Do').waitFor()
    })

    await t.test(`${width}px add to backlog, sync now and the alerts switch post once each`, async t => {
      const { page, sheet, posts } = await open(t, browser, origin, viewport)
      await toTodos(page, sheet)
      await button(sheet, 'Add to backlog: Book venue').click()
      await sheet.getByRole('button', { name: 'B500' }).or(sheet.getByText('B500')).first().waitFor()
      assert.equal(await button(sheet, 'Add to backlog: Book venue').count(), 0)
      await button(sheet, 'Sync now').click()
      const alerts = sheet.getByRole('switch', { name: /^Alerts/ })
      assert.equal(await alerts.isChecked(), true)
      await alerts.click()
      await page.waitForFunction(() => !document.querySelector('[role=switch]:disabled'))
      assert.equal(await alerts.isChecked(), false)
      assert.deepEqual(posts, [
        { endpoint: 'tasks/backlog', body: { key: 'planner:3' } },
        { endpoint: 'tasks/sync', body: {} },
        { endpoint: 'tasks/settings', body: { source: 'todo', alerts: false } },
      ])
    })

    await t.test(`${width}px off state shows the connect command and no Sync now`, async t => {
      const { page, sheet } = await open(t, browser, origin, viewport, { listing: OFF })
      await toTodos(page, sheet)
      await sheet.getByText(/^Connect a task app below/).waitFor()
      assert.equal(await sheet.locator('code', { hasText: COMMAND }).count(), 2)
      assert.equal(await button(sheet, 'Sync now').count(), 0)
      assert.equal(await sheet.getByRole('switch', { name: /^Alerts/ }).isChecked(), false)
    })

    await t.test(`${width}px a GET error offers Retry`, async t => {
      const { page, sheet } = await open(t, browser, origin, viewport, { failFirst: true })
      await toTodos(page, sheet)
      const error = sheet.getByRole('alert').filter({ hasText: 'Cannot load todos: Tasks fixture unavailable' })
      await error.waitFor()
      await button(error, 'Retry').click()
      await sheet.getByText('Pay electricity bill').waitFor()
    })
  }
})
