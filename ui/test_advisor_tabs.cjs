// Advisor chips in the helper row, and an advisor moved out as a separate task.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('advisor-names')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const owner = pane('%1', 'Deploy fix')
const child = { task: owner.id, task_depth: 1 }
const advising = pane('%2', 'Advisor: Deploy fix', { ...child, task_role: 'Advisor', role: 'advisor', state: 'Working' })
const second = pane('%3', 'Advisor 2: Deploy fix', { ...child, task_role: 'Second advisor', role: 'advisor', agent: 'codex' })
const helper = pane('%4', 'Docs pass', { ...child, task_role: 'Worker', state: 'Working' })
const separate = pane('%5', 'Advisor: Rollout', { role: 'advisor', state: 'Working', from: { pane: owner.id, name: owner.pane_name } })
const fixture = {
  sessions: { sessions: [owner, advising, second, helper, separate], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },  // Home and the sidebar read the work board
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = []
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
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
    })
    const chips = page.getByRole('tablist', { name: 'Tabs in Deploy fix' })
    const chip = id => chips.locator(`[role="tab"][data-chip="${id}"]`)
    for (const [width, height, scheme, motion] of [[1280, 900, 'light', 'no-preference'], [390, 844, 'dark', 'no-preference'], [320, 700, 'light', 'reduce']]) {
      const where = `${width}px ${scheme} ${motion}`
      await page.setViewportSize({ width, height })
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: motion })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await chip('%2').waitFor({ state: 'visible' })
      const names = await chips.locator('[role="tab"] > span:first-of-type').allTextContents()
      check(`${where}: each advisor chip has its own name`, names.includes('Advisor: Deploy fix') && names.includes('Advisor 2: Deploy fix'), names.join(' | '))
      check(`${where}: advisors in the helper row have no top-level tab`, await page.locator('.pane-tabs [role="tab"][data-pane="%2"], .pane-tabs [role="tab"][data-pane="%3"]').count() === 0)
      check(`${where}: advisor chips show the advisor icon`, await chip('%2').locator('.lucide-lightbulb').count() === 1 && await chip('%3').locator('.lucide-lightbulb').count() === 1)
      check(`${where}: other chips have no advisor icon`, await chip('%1').locator('.lucide-lightbulb').count() === 0 && await chip('%4').locator('.lucide-lightbulb').count() === 0)
      const style = tab => tab.evaluate(node => { const s = getComputedStyle(node); return { glow: node.classList.contains('advising'), animation: s.animationName, shadow: s.boxShadow } })
      const working = await style(chip('%2')), idle = await style(chip('%3')), plain = await style(chip('%4'))
      // B474, concept D: the glow was decoration. The working advisor chip keeps its state class and dot, with no shadow or pulse.
      check(`${where}: the working advisor chip has no glow`, working.glow && working.shadow === 'none' && working.animation === 'none', JSON.stringify(working))
      check(`${where}: an idle advisor and a working helper do not glow`, !idle.glow && idle.shadow === 'none' && !plain.glow && plain.shadow === 'none', JSON.stringify([idle, plain]))
      check(`${where}: the working dot says Advising`, await chip('%2').locator('.task-dot[aria-label="Advising"]').count() === 1 && await chip('%4').locator('.task-dot[aria-label="Working"]').count() === 1)
      check(`${where}: the advisor chip name includes its role and status`, await chips.getByRole('tab', { name: 'Advisor: Deploy fix, Advisor, Advising', exact: true }).count() === 1)
      if (width === 1280) {
        const moved = page.locator('.pane-tabs [role="tab"][data-pane="%5"]')
        const movedStyle = await style(moved)
        check('an advisor moved out keeps the advisor icon, pulse, and dot label', await moved.locator('.lucide-lightbulb').count() === 1
          && movedStyle.glow && movedStyle.shadow !== 'none' && movedStyle.animation === 'advise-glow'
          && await moved.locator('.task-dot[aria-label="Advising"]').count() === 1)
      }
      check(`${where}: no horizontal page scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      await chip('%2').scrollIntoViewIfNeeded().catch(() => {})
      await page.screenshot({ path: path.join(SHOTS, `advisor-tabs-${width}-${scheme}-${motion}.png`) })
    }
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`advisor tabs: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
