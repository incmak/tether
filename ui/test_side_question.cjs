// Claude's /btw side question: the card over the message box and the copy kept in the chat.
// Run against an existing build. Every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || ''
const QUESTION = 'give a markdown table of 3 linux commands with a one line use, then a bullet list of 2 tips, then a 3 line bash code block'
const ANSWER = '| Command | Use |\n|---|---|\n| `ls -la` | List all files, including hidden ones, with permissions and sizes. |\n| `grep -rn "text" .` | Find every line that contains "text" in the current folder and below. |\n\n- Press `Tab` to autocomplete file names.\n\n```bash\nls -la ~/Documents\ngrep -rn "TODO" ~/apps/a-very-long-folder-name/with/more/folders/inside/it/server\n```\n'
const DRAWN = '┌────────┬─────────┐\n│ Command │ Use │\n└────────┴─────────┘\n- Press Tab to autocomplete file names.'

function fixtures(state) {
  const attention = state === 'saved' ? null : { id: 'side-1', reason: 'side', requiresResponse: false, question: QUESTION,
    text: state === 'answering' ? '' : state === 'drawn' ? DRAWN : ANSWER, pending: state === 'answering',
    markdown: state === 'answer', fork: state === 'answer' }
  const pane = { id: '%1', tmux_session: '$1', label: 'Side test', agent: 'claude', dir: '/srv/project', state: 'Idle',
    writable: true, thread: 'side-thread', approval: null }
  const messages = [{ id: 'm1', role: 'user', text: 'Start the work.', time: '2026-11-14T08:00:00Z' },
    { id: 'm2', role: 'assistant', text: 'Work started.', time: '2026-11-14T08:00:05Z' },
    ...(state === 'saved' ? [{ id: 'terminal:side:abc', role: 'assistant', kind: 'side', title: QUESTION, text: ANSWER, time: '2026-11-14T08:01:00Z' }] : [])]
  return {
    sessions: { sessions: [pane], default_thread: pane.thread },
    chat: { messages, busy: false, thread: pane.thread, agent: pane.agent, writable: true, session: pane.id, earlier: 0, limit: 100,
      state: 'Idle', approval: null, attention },
    footer: { observed: true, model: 'Claude', mode: 'default', notices: [] },
    prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
    update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
    catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {},
    'codex-runs': { pane: '%1', running: 0, runs: [] }, 'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
    'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
    'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
    'work/board': { revision: 1, projects: [], items: [], stages: [] },
    usage: { claude: null, codex: null }, subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  }
}

async function main() {
  let browser, page, server, passed = 0, failed = 0
  const check = (name, errors) => {
    if (errors.length) { failed++; console.log(`FAIL ${name}: ${errors.join('; ')}`) } else { passed++; console.log(`ok ${name}`) }
  }
  try {
    await fs.access(path.join(DIST, 'index.html'))
    if (SHOTS) await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (req.method !== 'GET' || url.pathname.endsWith('/sw.js') || !url.pathname.startsWith('/chat/') || !file.startsWith(DIST)) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' })
        res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    for (const state of ['answering', 'answer', 'drawn', 'saved']) for (const width of [320, 390, 1280]) {
      const name = `${state}-${width}`, errors = [], keys = [], pageErrors = []
      const fixture = fixtures(state)
      if (page) await page.close().catch(() => {})
      page = await context.newPage()
      page.setDefaultTimeout(10000)
      page.on('pageerror', error => pageErrors.push(error.message))
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
      await page.routeWebSocket('**/*', socket => socket.close())
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
        if (url.pathname.includes('/api/')) {
          const endpoint = url.pathname.split('/api/')[1]
          if (request.method() !== 'GET') {
            if (endpoint === 'key') keys.push(request.postDataJSON().key)
            return route.fulfill({ json: { ok: true, accepted: true } })
          }
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
          return route.fulfill({ json: fixture[endpoint] || {} })
        }
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
        return route.abort('blockedbyclient')
      })
      await page.addInitScript(() => {
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false, sidePins: [], look: { layout: 'classic', surface: 'flat', backdrop: 'ember' } }))
      })
      try {
        await page.setViewportSize({ width, height: 800 })
        await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
        await page.waitForSelector('.composer textarea')
        if (state === 'saved') {
          const saved = page.locator('.side-message')
          await saved.waitFor()
          if (await page.locator('.side-dock').count()) errors.push('a card shows with no overlay open')
          if (!(await saved.locator('summary').innerText()).includes('Side question')) errors.push('the saved message has no title')
          if (!(await saved.locator('.side-asked').innerText()).includes('3 linux commands')) errors.push('the saved message has no question')
          if (!await saved.locator('table').count()) errors.push('the saved answer has no table')
        } else {
          const card = page.locator('.side-dock')
          await card.waitFor()
          if (!(await card.locator('.choice-head').innerText()).includes('Side question')) errors.push('wrong card title')
          if (!(await card.locator('.side-question').innerText()).includes('3 line bash code block')) errors.push('the question is cut short')
          if ((await card.innerText()).includes('needs attention')) errors.push('the raw fallback title shows')
          const fork = card.getByRole('button', { name: 'Continue in a subagent' })
          if (state === 'answering') {
            if (!(await card.getByRole('status').innerText()).includes('Answering')) errors.push('no waiting state')
            if (await fork.count() || await card.getByRole('button', { name: 'Copy answer' }).count()) errors.push('answer actions show before the answer')
          } else if (state === 'answer') {
            if (await card.locator('.side-answer table').count() !== 1) errors.push('the Markdown table is not a table')
            if (await card.locator('.side-answer pre').count() !== 1) errors.push('the code block is missing')
            if (await card.locator('pre.panel-pre').count()) errors.push('raw terminal text shows')
            await fork.click()
            if (!await card.getByRole('button', { name: 'Copy answer' }).count()) errors.push('no copy button')
          } else {
            if (!(await card.locator('pre').first().innerText()).includes('Command')) errors.push('the drawn text is missing')
            if (await fork.count()) errors.push('fork shows when the server did not offer it')
          }
          await card.getByRole('button', { name: 'Close', exact: true }).click()
          await page.waitForTimeout(150)
          const want = state === 'answer' ? ['f', 'Escape'] : ['Escape']
          if (keys.join() !== want.join()) errors.push(`keys sent: ${keys.join() || 'none'}, want ${want.join()}`)
          for (const button of await card.getByRole('button').all()) {
            const box = await button.boundingBox()
            if (box && box.height < 24) errors.push(`button ${(await button.innerText()) || 'icon'} is ${Math.round(box.height)} px high`)
          }
        }
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
        if (overflow > 0) errors.push(`the page scrolls sideways by ${overflow} px`)
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
      } catch (error) { errors.push(error.message.split('\n')[0]) }
      check(name, [...errors, ...pageErrors.map(text => `page error: ${text}`)])
    }
  } catch (error) { failed++; console.log(`FAIL setup: ${error.message}`) } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) await new Promise(resolve => server.close(resolve))
  }
  console.log(`${passed} passed, ${failed} failed`)
  process.exit(failed ? 1 : 0)
}

main()
