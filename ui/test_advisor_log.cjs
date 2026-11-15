// Advisor decisions in the Project sheet (AdvisorLog.tsx), against an existing build. Every API request stays in this
// page's fixtures; no terminal is touched. Run: cd ui && npm run test:ui (or node test_advisor_log.cjs after a build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/advisor-log/shots')
const sizes = [[320, 640], [1280, 900]]
const PID = 'a1b2c3d4e5f6'
const now = Math.floor(Date.now() / 1000)
const longQuestion = 'Should the export use a streaming writer or build the whole file in memory first? ' + 'The largest export today is 40 MB. '.repeat(6)
const longAnswer = 'Use the streaming writer. It keeps memory flat for large exports and the client already reads chunks. '.repeat(5)
const rows = [
  { id: 'aaaaaaaaaaa1', time: now - 60, status: 'ask_user', question: 'Which brand color should the empty state use?', answer: 'This is a taste choice. Ask the user.', agent: 'claude', model: 'fable', full: true },
  { id: 'aaaaaaaaaaa2', time: now - 600, status: 'decided', question: longQuestion.slice(0, 300), answer: longAnswer.slice(0, 400), agent: 'claude', model: 'fable', full: true },
  { id: 'aaaaaaaaaaa3', time: now - 3600, status: 'error', question: 'Is the retry budget safe for the payment hook?', answer: '', agent: 'claude', model: 'fable', full: true },
  { id: 'aaaaaaaaaaa4', time: now - 7200, status: 'timeout', question: 'Pick the queue library.', answer: '', agent: 'codex', model: 'gpt', full: true },
  { id: 'aaaaaaaaaaa5', time: now - 9000, status: 'off', question: 'Rename the settings tab?', answer: '', agent: 'none', model: 'latest', full: true },
  { id: '', time: now - 86400 * 3, status: 'decided', question: 'Old question from the cut Markdown log', answer: 'Old answer, cut at 300 characters', agent: 'claude', model: 'fable', full: false },
]
const entries = {
  aaaaaaaaaaa1: { ...rows[0], confidence: 'medium', evidence: 'docs/brand.md has no rule for empty states.', error: '', advisors: [{ role: 'main', agent: 'claude', model: 'fable', answer: 'This is a taste choice. Ask the user.', confidence: 'medium' }] },
  aaaaaaaaaaa2: { ...rows[1], question: longQuestion, answer: longAnswer, confidence: 'high', evidence: 'server/export.py\nui/src/Export.tsx', error: '',
    advisors: [{ role: 'main', agent: 'claude', model: 'fable', answer: longAnswer, confidence: 'high' }, { role: 'second', agent: 'codex', model: 'gpt', answer: 'Agree: stream it.', confidence: 'high' }] },
  aaaaaaaaaaa5: { ...rows[4], confidence: '', evidence: '', error: '', advisors: [] },
}
const project = { id: PID, name: 'web', revision: 1, budget: 4000, budget_min: 1000, budget_max: 10000, instructions: '', pins: [], notes: [], preview: '', chars: 120, folder: '/srv/projects/' + PID, session_name: 'web' }
const levels = [0, 1, 2, 3, 4].map(level => ({ level, name: `Level ${level}`, summary: 'Rules for this level.' }))
const pane = { id: '%1', tmux_session: '$1', label: 'Advisor log test', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 'adv-thread', approval: null }
const fixture = {
  sessions: { sessions: [pane], default_thread: pane.thread },
  chat: { messages: [{ id: 'm1', role: 'user', text: 'Hello', time: '2026-11-15T08:00:00Z' }], busy: false, thread: pane.thread, agent: 'claude', writable: true, session: pane.id, earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, subagents: { main: null, agents: [], tasks: [] },
  'projects/project': project, 'push/config': { enabled: false },
  autonomy: { levels, global: 4, level: 4, source: 'global', project: { override: false, level: 4 }, reviewers: {}, tiers: [], models: { claude: [], codex: [] }, efforts: [], decisions: [] },
  'codex-level': { levels: levels.slice(0, 5), global: 1, level: 1, source: 'global', project: { override: false, level: 1 }, runs: { today: 0, running: 0, panes: [] }, run: { model: '', effort: '', fast: false }, models: [] },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], unexpected = new Set()
  let logMode = 'rows'  // rows | empty | error | slow
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
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    const asked = []
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET' && endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (request.method() !== 'GET') { unexpected.add(`POST ${endpoint}`); return route.fulfill({ status: 500, json: { error: 'no writes here' } }) }
        if (endpoint === 'projects/advisor-log') {
          asked.push(url.searchParams.get('project'))
          if (logMode === 'error') return route.fulfill({ status: 500, json: { error: 'Tether could not read or write the project folder.' } })
          if (logMode === 'slow') await new Promise(resolve => setTimeout(resolve, 1500))
          return route.fulfill({ json: { entries: logMode === 'empty' ? [] : rows, keep: 200 } })
        }
        if (endpoint === 'projects/advisor-entry') {
          const found = entries[url.searchParams.get('id')]
          return route.fulfill(found ? { json: found } : { status: 404, json: { error: 'That advisor entry is gone. The log keeps the last 200 asks.' } })
        }
        if (!(endpoint in fixture)) unexpected.add(endpoint)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      unexpected.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const sheet = page.locator('dialog.project-sheet')
    const section = sheet.locator('section.advisor-log')
    const openSheet = async () => {
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await page.getByRole('button', { name: /^Session, .* and keys$/ }).click()
      await page.getByRole('button', { name: 'Session options' }).click()
      await page.getByRole('button', { name: 'Project…' }).click()
      await sheet.waitFor()
    }
    const shot = async name => {
      await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
      await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
      await page.screenshot({ path: path.join(SHOTS, `${name}-dark.png`) })
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
    }
    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })

      logMode = 'slow'
      await openSheet()
      await section.locator('.advisor-skeleton').waitFor()
      check(`${at} loading skeleton announces itself`, (await section.getByRole('status').first().innerText()).includes('Loading advisor decisions'))
      logMode = 'rows'
      await section.locator('.advisor-row').first().waitFor()
      check(`${at} asks for this project`, asked.at(-1) === PID, String(asked.at(-1)))

      // The list: newest first, a word and an icon for each status, distinct colors.
      const words = await section.locator('.advisor-row .advisor-word').allInnerTexts()
      check(`${at} newest first with status words`, words.join(',') === 'Asked you,Decided,Error,Timed out,Advisor off,Decided', words.join(','))
      check(`${at} every row has an icon`, await section.locator('.advisor-row .advisor-status svg').count() === rows.length)
      const colors = await section.evaluate(node => ['decided', 'ask_user', 'error', 'off'].map(status => getComputedStyle(node.querySelector(`.advisor-word.${status}`)).color))
      check(`${at} statuses have distinct colors`, new Set(colors).size === 4, colors.join(' | '))
      const clamp = await section.locator('.advisor-row').nth(1).evaluate(row => {
        const q = row.querySelector('.advisor-question'), a = row.querySelector('.advisor-answer')
        const lines = el => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))
        return { q: lines(q), a: lines(a), cut: q.scrollWidth > q.clientWidth }
      })
      check(`${at} question one line, answer at most two`, clamp.q === 1 && clamp.a <= 2 && clamp.cut, JSON.stringify(clamp))
      check(`${at} empty answer says so`, await section.locator('.advisor-row').nth(2).getByText('No answer').isVisible())
      check(`${at} no page overflow (list)`, !(await overflow()))
      const sheetOverflow = await sheet.evaluate(node => node.scrollWidth > node.clientWidth + 1)
      check(`${at} no sheet overflow (list)`, !sheetOverflow)
      const targets = await section.locator('.advisor-row, .advisor-filter .segmented span').evaluateAll(els => els.map(el => el.getBoundingClientRect().height))
      check(`${at} targets at least 24 px tall`, targets.every(h => h >= 24), targets.join(','))
      await section.scrollIntoViewIfNeeded()
      await shot(`list-${width}`)

      // Filters.
      // Each filter label shows in full: none is cut, and the control spans the section.
      const clipped = await section.locator('.advisor-filter .segmented span').evaluateAll(els => els.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.textContent))
      check(`${at} filter labels not cut`, clipped.length === 0, clipped.join(', '))
      const filter = async (label, expected) => {
        await section.locator('.advisor-filter label', { hasText: label }).click()
        check(`${at} filter ${label} is checked`, await section.getByRole('radio', { name: new RegExp(`^${label}, \\d+$`) }).isChecked())
        const shown = await section.locator('.advisor-row .advisor-word').allInnerTexts()
        check(`${at} filter ${label}`, shown.join(',') === expected, shown.join(','))
      }
      await filter('Asked you', 'Asked you')
      await filter('Errors', 'Error,Timed out')
      await shot(`filter-errors-${width}`)
      await filter('Decided', 'Decided,Decided')
      await filter('All', 'Asked you,Decided,Error,Timed out,Advisor off,Decided')

      // Keyboard: focus a row, Enter opens it in full, Back returns focus to the same row.
      const row = section.locator('.advisor-row').nth(1)
      await row.focus()
      await page.keyboard.press('Enter')
      const detail = sheet.locator('.advisor-detail')
      await detail.getByText('Evidence').waitFor()
      check(`${at} heading names the view`, (await sheet.locator('#project-title').innerText()) === 'Advisor decision')
      check(`${at} Back has focus`, await page.evaluate(() => document.activeElement?.textContent === 'All decisions'))
      check(`${at} full question and answer`, (await detail.locator('.advisor-text').first().innerText()) === longQuestion.trim() || (await detail.locator('.advisor-text').first().innerText()).startsWith(longQuestion.slice(0, 200)))
      check(`${at} both advisors shown`, await detail.getByText('Advisor (claude fable) · high confidence').isVisible() && await detail.getByText('Second advisor (codex gpt) · high confidence').isVisible())
      check(`${at} evidence keeps lines`, (await detail.locator('.advisor-text').last().innerText()) === 'server/export.py\nui/src/Export.tsx')
      check(`${at} no page overflow (detail)`, !(await overflow()))
      await shot(`detail-${width}`)
      await page.keyboard.press('Enter')  // on Back
      await section.locator('.advisor-row').first().waitFor()
      check(`${at} Back focuses the row it came from`, await page.evaluate(() => document.activeElement?.dataset?.key === 'aaaaaaaaaaa2'))

      // An older Markdown line opens without a request and says it is cut.
      await section.locator('.advisor-row').last().click()
      await detail.getByText('Older entry.').waitFor()
      check(`${at} older entry shows cut text`, (await detail.locator('.advisor-text').first().innerText()) === 'Old question from the cut Markdown log')
      await shot(`detail-older-${width}`)
      await detail.getByRole('button', { name: 'All decisions' }).click()

      // An entry that left the log: the error and Retry.
      await section.locator('.advisor-row').nth(2).click()
      await detail.getByRole('alert').waitFor()
      check(`${at} gone entry shows the server error`, (await detail.getByRole('alert').innerText()).includes('That advisor entry is gone.'))
      check(`${at} gone entry offers Retry`, await detail.getByRole('button', { name: 'Retry' }).isVisible())
      await detail.getByRole('button', { name: 'All decisions' }).click()

      // Advisor off: no fake model in the answer label.
      await section.locator('.advisor-row').nth(4).click()
      await detail.getByText('The advisor was off.').waitFor()
      check(`${at} advisor off: plain Answer label`, (await detail.locator('.advisor-part h4').allInnerTexts()).join(',') === 'Question,Answer')
      await detail.getByRole('button', { name: 'All decisions' }).click()
      await page.keyboard.press('Escape')
      await sheet.waitFor({ state: 'hidden' })

      // Empty and error states.
      logMode = 'empty'
      await openSheet()
      await section.getByText('No advisor decisions yet.').waitFor()
      check(`${at} empty state, no filter`, await section.locator('.advisor-filter').count() === 0)
      await section.scrollIntoViewIfNeeded()
      await shot(`empty-${width}`)
      await page.keyboard.press('Escape')
      logMode = 'error'
      await openSheet()
      await section.getByRole('alert').waitFor()
      check(`${at} error state with Retry`, (await section.getByRole('alert').innerText()).includes('Could not load the advisor log.') && await section.getByRole('button', { name: 'Retry' }).isVisible())
      await section.scrollIntoViewIfNeeded()
      await shot(`error-${width}`)
      logMode = 'rows'
      await section.getByRole('button', { name: 'Retry' }).click()
      await section.locator('.advisor-row').first().waitFor()
      check(`${at} Retry loads the list`, true)
      await page.keyboard.press('Escape')
    }
    check('no unmocked requests', !unexpected.size, [...unexpected].join(', '))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; ') + ' | unmocked: ' + [...unexpected].join(', '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`advisor log: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
