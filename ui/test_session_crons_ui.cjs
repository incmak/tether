// Session cron jobs (B198) in a real browser: the tab's chip, the "Scheduled jobs" sheet, the sidebar mark, and
// scheduled runs in the chat. Run against an existing build; every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const assert = require('node:assert/strict')
const { getBrowser, serveLocal, prefsStore } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const now = Math.floor(Date.now() / 1000)
const iso = seconds => new Date(seconds * 1000).toISOString()
const job = { id: 'c1', label: 'Inbox watcher', every: 'Every hour at :13 and :43' }
const fire = (id, at) => ({ id, role: 'assistant', kind: 'notice', title: 'Scheduled run', text: job.label, quiet: 'schedule', watch: job.label, cron: job.id, time: iso(at) })
const tools = (id, at, name = 'Bash') => ({ id, role: 'assistant', kind: 'actions', text: '', time: iso(at), tools: [{ id: id + '-t', name, title: 'Check the inbox', is_error: false, status: 'complete' }] })
const say = (id, at, text) => ({ id, role: 'assistant', text, time: iso(at) })
const messages = [
  { id: 'u1', role: 'user', text: 'Start the inbox watcher', time: iso(now - 7200) },
  say('a1', now - 7190, 'Started.'),
  fire('f1', now - 5400), tools('t1', now - 5395), say('r1', now - 5390, 'No new messages.\n\nNeeds you: no'),
  fire('f2', now - 3600), tools('t2', now - 3595), say('r2', now - 3590, 'No new messages.\n\nNeeds you: no'),
  fire('f3', now - 1800), tools('t3', now - 1795, 'mcp__teams__send'), say('r3', now - 1790, 'Posted the reply to Faiz in the thread.\n\nNeeds you: no'),
]
const pane = { id: '%1', tmux_session: '$1', label: 'Cron test', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true,
  thread: 'cron-thread', crons: [{ ...job, next: now + 600 }] }
const fixture = {
  sessions: { sessions: [pane], default_thread: pane.thread },
  chat: { messages, busy: false, thread: pane.thread, agent: pane.agent, writable: true, session: pane.id, earlier: 0, limit: 100, state: 'Idle',
    scheduled: { c1: { at: iso(now - 1800), message: 'f3', result: 'Posted the reply to Faiz in the thread.\n\nNeeds you: no' } } },
  footer: { observed: true, model: 'Claude', mode: 'default', notices: [],
    crons: [{ ...job, next: now + 600, schedule: '13,43 * * * *', recurring: true }] },
  live: {},
  'projects/marks': {}, 'projects/backlog': { text: '' },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {},
  'codex-runs': { pane: '%1', running: 0, runs: [] }, undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  usage: { claude: null, codex: null }, subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
}

async function main() {
  let server, browser, page
  const pageErrors = []
  try {
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (req.method !== 'GET' || url.pathname.endsWith('/sw.js') || !url.pathname.startsWith('/chat/') || !file.startsWith(DIST)) { res.writeHead(404); res.end(); return }
      const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
      try { const bytes = await fs.readFile(file); res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes) }
      catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0] || await browser.newContext({ serviceWorkers: 'block' })
    for (const [width, height] of [[320, 640], [390, 844], [820, 1180], [1280, 900]]) {
      if (page) await page.close().catch(() => {})
      page = await context.newPage()
      page.setDefaultTimeout(10000)
      page.on('pageerror', error => pageErrors.push(`${width}: ${error.message}`))
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
      await page.routeWebSocket('**/*', socket => socket.close())
      const prefs = prefsStore()
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
        if (url.pathname.includes('/api/')) {
          const endpoint = url.pathname.split('/api/')[1]
          if (endpoint === 'prefs') return route.fulfill({ json: request.method() === 'GET' ? prefs.get() : prefs.post(request.postDataJSON()) })
          if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
          return route.fulfill({ json: fixture[endpoint] || {} })
        }
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
        return route.abort('blockedbyclient')
      })
      await page.addInitScript(() => {
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false }))
      })
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await page.waitForSelector('.watch-row')

      // Scheduled runs: identical results share one line; the run that posted keeps its own line, in its own words.
      const lines = await page.locator('.watch-row .watch-what').allTextContents()
      assert.deepEqual(lines, ['Inbox watcher · 2 runs · No new messages.', 'Inbox watcher · Posted the reply to Faiz in the thread.'], `${width}: run lines`)
      assert.equal(await page.getByText(/nothing to report/i).count(), 0, `${width}: no quiet claim`)
      await page.locator('.watch-row summary').nth(1).click()
      await page.waitForSelector('.watch-row[open] .watch-reply')

      // The chip names the job; Enter opens the sheet with the schedule, the next run and the last result.
      const chip = page.getByRole('button', { name: /Inbox watcher/ }).filter({ has: page.locator('.live-chip-text') })
      await chip.focus()
      await page.keyboard.press('Enter')
      const sheet = page.getByRole('dialog', { name: 'Scheduled jobs' })
      await sheet.waitFor()
      const text = await sheet.innerText()
      for (const want of ['Inbox watcher', 'Every hour at :13 and :43', 'Next run about', 'Posted the reply to Faiz']) assert.ok(text.includes(want), `${width}: sheet shows "${want}": ${text}`)
      const box = await sheet.boundingBox()
      assert.ok(box && box.x >= 0 && box.x + box.width <= width + 0.5, `${width}: sheet fits the width`)
      await page.keyboard.press('Escape')
      await sheet.waitFor({ state: 'hidden' })

      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      assert.equal(overflow, false, `${width}: no page scroll sideways`)
      await page.screenshot({ path: path.join(process.env.SHOTS || '/tmp', `crons-${width}.png`) })
      if (width >= 1280) {
        await page.getByRole('button', { name: 'Sessions', exact: true }).or(page.getByRole('tab', { name: 'Sessions', exact: true })).first().click()
        const mark = page.locator('.cron-mark').first()
        assert.match(await mark.getAttribute('title'), /^Scheduled: Inbox watcher/, `${width}: sidebar mark`)
        assert.match(await mark.locator('.visually-hidden').textContent(), /scheduled: Inbox watcher/i, `${width}: mark text for screen readers`)
      }
    }
    assert.deepEqual(pageErrors, [])
    console.log('session crons ui: chip, sheet, sidebar mark and scheduled-run lines pass at 320, 390, 820 and 1280 px')
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
  }
}

main().catch(error => { console.error(`FAIL session crons ui: ${error.message}`); process.exitCode = 1 })
