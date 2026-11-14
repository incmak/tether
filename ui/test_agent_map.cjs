// Agent map check against an existing build. Every API request stays in this page's fixtures; no terminal is touched.
// Run: cd ui && npm run test:ui (or node test_agent_map.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-11/agent-map/shots')
const sizes = [[390, 844], [1280, 900]]
const now = Date.now()
const iso = seconds => new Date(now - seconds * 1000).toISOString()
const agent = (id, description, state, extra = {}) => ({ id, description, type: 'general-purpose', model: 'claude-sonnet-5-5', effort: 'medium',
  background: true, state, started: iso(400), ended: state === 'running' ? null : iso(60), seconds: 340, tokens: 87012, tools: 2, depth: 1,
  parent: null, summary: null, ...extra })
const agents = [
  agent('a1', 'Research remote browser products and compare their pricing pages', 'finished'),
  agent('a2', 'Audit the chat composer', 'running', { model: 'claude-haiku-5-5', seconds: 95, tokens: 1200 }),
  agent('a3', 'Check the deploy script', 'stopped', { summary: 'Agent "Check the deploy script" was stopped by user' }),
  agent('a4', 'Read the hooks folder', 'finished', { parent: 'a2', depth: 2, tools: 0 }),
]
const task = (id, kind, description, command, state, extra = {}) => ({ id, kind, description, command, state, started: iso(128), ended: state === 'running' ? null : iso(10),
  seconds: 128, parent: null, summary: null, ...extra })
const tasks = [
  task('b1', 'shell', 'Run the server tests', 'cd server && python3 -m unittest discover -q', 'running'),
  task('b2', 'monitor', null, 'tail -f build.log | grep --line-buffered ERROR', 'stopped', { summary: '[killed]', parent: 'a2', seconds: 21 }),
]
const taskOutput = id => ({ ...tasks.find(item => item.id === id), output: Array.from({ length: 80 }, (_, i) => `line ${i + 1}`).join('\n'), output_cut: true })
const tools = [{ kind: 'tool', id: 't1', name: 'Bash', title: 'ls server', is_error: false, status: 'complete' },
  { kind: 'tool', id: 't2', name: 'Read', title: 'server/subagents.py', is_error: true, status: 'complete' }]
const detail = id => ({ ...agents.find(item => item.id === id), prompt: 'Find the three best remote browser products.\n\nReport prices.',
  steps: [{ kind: 'text', text: 'Starting with the server folder.' }, ...tools], report: id === 'a1' ? 'Three products fit. Browserbase is the cheapest at the volume you need.' : null })
const messages = Array.from({ length: 12 }, (_, i) => ({ id: `m${i}`, role: i % 2 ? 'assistant' : 'user', text: `Message ${i + 1}.`, time: '2026-11-11T08:00:00Z' }))
const pane = { id: '%1', tmux_session: '$1', label: 'Agent map test', agent: 'claude', dir: '/srv/project', state: 'Working', writable: true, thread: 'map-thread', approval: null }
const fixture = {
  sessions: { sessions: [pane], default_thread: pane.thread },
  chat: { messages, busy: true, thread: pane.thread, agent: 'claude', writable: true, session: pane.id, earlier: 0, limit: 100, state: 'Working', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: 'claude-opus-5-5', context: 91452, title: 'Build the agent map' }, agents, tasks },
  'subagent-tool': { id: 't1', name: 'Bash', title: 'ls server', is_error: false, status: 'complete', detail: '$ ls server\nserver.py\nsubagents.py', format: 'text', truncated: false, images: 0 },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] }, 'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

async function main() {
  let browser, page, server, failed = 0
  const posts = [], pageErrors = [], unexpected = new Set()
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
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') { posts.push({ endpoint, body: request.postDataJSON() }); return route.fulfill({ json: { ok: true, state: 'stopped' } }) }
        if (endpoint === 'subagent-task') return route.fulfill({ json: taskOutput(url.searchParams.get('id')) })
        if (endpoint === 'subagent') return route.fulfill({ json: detail(url.searchParams.get('id')) })
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
    const onTop = selector => page.evaluate(selector => [...document.querySelectorAll(selector)].filter(el => el.getClientRects().length).every(el => {
      const r = el.getBoundingClientRect(), top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      return top && (el === top || el.contains(top))
    }), selector)
    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      const pill = page.getByRole('button', { name: /1 agent running\. Open the agent map/ })
      await pill.waitFor()
      check(`${at} pill shows running count`, (await pill.textContent()).trim() === '1agent')
      await page.screenshot({ path: path.join(SHOTS, `pill-${width}.png`) })
      await pill.click()
      const map = page.locator('dialog.agent-map')
      await map.waitFor()
      check(`${at} map main node`, await map.getByText('Build the agent map').isVisible() && await map.getByText('Opus 5.5 · 91.5k tokens in context').isVisible())
      check(`${at} nested child under its parent`, await map.locator('.agent-children .agent-children').getByText('Read the hooks folder').isVisible())
      check(`${at} map heading counts states`, (await map.locator('.sheet-sub').first().innerText()).trim() === '4 agents · 1 running · 1 stopped · 2 done')
      check(`${at} running agent listed first`, (await map.locator('.agent-tree > .agent-branch > .agent-children > li').first().innerText()).startsWith('Audit the chat composer'))
      const word = async (name, text) => {
        const row = map.locator('.agent-node', { hasText: name }).first()
        const el = row.locator('.agent-state-word')
        return (await el.innerText()).trim() === text && await row.locator('.agent-state svg, .agent-state i').count() === 1
      }
      check(`${at} rows say Running / Done / Stopped`, await word('Audit the chat composer', 'Running') && await word('Research remote browser', 'Done') && await word('Check the deploy script', 'Stopped'))
      const colors = await map.evaluate(node => ['running', 'finished', 'stopped'].map(state => getComputedStyle(node.querySelector(`.agent-state-word.${state}`)).color))
      check(`${at} states have distinct colors`, new Set(colors).size === 3, colors.join(' | '))
      check(`${at} map rows reachable`, await onTop('.agent-map .agent-node'))
      check(`${at} no page overflow (map)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `map-${width}.png`) })
      await map.getByRole('button', { name: /Research remote browser/ }).click()
      await map.locator('.agent-report').waitFor()
      check(`${at} finished detail facts`, (await map.locator('.agent-facts').innerText()).includes('Done · general-purpose · Sonnet 5.5 · medium · background'))
      check(`${at} spawned by the main agent`, await map.getByText('Spawned by the main agent').isVisible())
      check(`${at} time and tokens line`, (await map.locator('.agent-numbers').innerText()).trim() === '5m 40s · 87.0k tokens')
      check(`${at} report is plain text`, (await map.locator('.agent-report').innerText()).startsWith('Three products fit.'))
      check(`${at} finished has no Stop`, await map.getByRole('button', { name: 'Stop agent' }).count() === 0)
      await map.getByText('Tool calls (2)').click()
      check(`${at} file tool shows the file name`, (await map.locator('.tool-inline code').innerText()).trim() === 'subagents.py')
      await map.getByText('ls server').click()
      await map.locator('.agent-tools pre').getByText('server.py').waitFor()
      check(`${at} tool row opens subagent tool details`, true)
      check(`${at} tool rows reachable`, await onTop('.agent-tools .tool-toggle'))
      await page.screenshot({ path: path.join(SHOTS, `detail-finished-${width}.png`) })
      await map.getByRole('button', { name: 'Open transcript' }).click()
      await map.locator('.agent-steps').getByText('Starting with the server folder.').waitFor()
      check(`${at} transcript view`, await map.getByRole('heading', { name: 'Transcript' }).isVisible())
      await page.screenshot({ path: path.join(SHOTS, `transcript-${width}.png`) })
      await map.getByRole('button', { name: 'Back to the agent' , exact: true }).click()
      await map.locator('.agent-report').waitFor()
      await map.getByRole('button', { name: 'Back to the agent map' }).click()
      await map.getByRole('button', { name: /Read the hooks folder/ }).click()
      await map.getByText('Spawned by Audit the chat composer').waitFor()
      check(`${at} nested agent names its parent`, true)
      await map.getByRole('button', { name: 'Back to the agent map' }).click()
      await map.getByRole('button', { name: /Check the deploy script/ }).click()
      await map.getByText('was stopped by user').waitFor()
      check(`${at} stopped detail shows summary`, true)
      await map.getByRole('button', { name: 'Back to the agent map' }).click()
      await map.getByRole('button', { name: /Audit the chat composer/ }).click()
      const stop = map.getByRole('button', { name: 'Stop agent' })
      await stop.waitFor()
      check(`${at} Stop reachable`, await onTop('.agent-stop'))
      check(`${at} no page overflow (detail)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `detail-running-${width}.png`) })
      const before = posts.length
      await stop.click()
      await page.waitForTimeout(300)
      const sent = posts.slice(before).find(post => post.endpoint === 'subagent-stop')
      check(`${at} Stop posts session and id`, sent && sent.body.session === '%1' && sent.body.id === 'a2', JSON.stringify(posts.slice(before)))
      // Background tasks: listed under the tree; a task opens its command and output; Stop task posts its id.
      await map.getByRole('button', { name: 'Back to the agent map' }).click()
      check(`${at} tasks heading`, (await map.locator('#agent-tasks-title').innerText()).trim() === '2 background tasks · 1 running · 1 stopped')
      check(`${at} task row facts`, await map.locator('.agent-tasks').getByText('Running · shell · 2m 08s').isVisible() && await map.locator('.agent-tasks').getByText('Stopped · monitor · 21s').isVisible())
      check(`${at} task without description shows its command`, await map.locator('.agent-tasks strong.mono').getByText('tail -f build.log').isVisible())
      check(`${at} task rows reachable`, await onTop('.agent-tasks .agent-node'))
      await page.screenshot({ path: path.join(SHOTS, `map-tasks-${width}.png`) })
      await map.getByRole('button', { name: /Run the server tests/ }).click()
      const out = map.getByLabel('Output')
      await out.waitFor()
      check(`${at} task facts`, (await map.locator('.agent-facts').innerText()).startsWith('Running · shell · background'))
      check(`${at} task command shown`, await map.locator('pre.agent-text').first().innerText() === 'cd server && python3 -m unittest discover -q')
      check(`${at} output starts at its end`, await out.evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight < 2))
      check(`${at} Stop task reachable`, await onTop('.agent-stop'))
      check(`${at} no page overflow (task)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `task-running-${width}.png`) })
      const beforeTask = posts.length
      await map.getByRole('button', { name: 'Stop task' }).click()
      await page.waitForTimeout(300)
      const sentTask = posts.slice(beforeTask).find(post => post.endpoint === 'subagent-stop')
      check(`${at} Stop task posts its id`, sentTask && sentTask.body.id === 'b1', JSON.stringify(posts.slice(beforeTask)))
      await map.getByRole('button', { name: 'Back to the agent map' }).click()
      await map.getByRole('button', { name: /tail -f build.log/ }).click()
      await map.getByText('Started by Audit the chat composer').waitFor()
      check(`${at} stopped task: summary, no Stop`, await map.getByText('[killed]').isVisible() && await map.getByRole('button', { name: 'Stop task' }).count() === 0)
      await page.keyboard.press('Escape')
      await map.waitFor({ state: 'hidden' })
      check(`${at} Esc closes the map`, true)
    }
    check('no unmocked requests', !unexpected.size, [...unexpected].join(', '))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`agent map: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
