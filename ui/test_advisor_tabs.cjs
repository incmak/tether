// Advisor tabs in the pane tab strip: a distinct name per tab, the advisor icon, and the glow while the advisor works.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-14/advisor-names/shots')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const owner = pane('%1', 'Deploy fix')
const advising = pane('%2', 'Advisor: Deploy fix', { role: 'advisor', state: 'Working' })
const second = pane('%3', 'Advisor 2: Deploy fix', { role: 'advisor', agent: 'codex' })
const helper = pane('%4', 'Docs pass', { state: 'Working' })
const fixture = {
  sessions: { sessions: [owner, advising, second, helper], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
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
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
    })
    const tab = id => page.locator(`[role="tab"][data-pane="${id}"]`).first()
    for (const [width, height, scheme, motion] of [[1280, 900, 'light', 'no-preference'], [390, 844, 'dark', 'no-preference'], [320, 700, 'light', 'reduce']]) {
      const where = `${width}px ${scheme} ${motion}`
      await page.setViewportSize({ width, height })
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: motion })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await tab('%2').waitFor({ state: 'visible' })
      await tab('%2').waitFor({ state: 'attached' })
      const names = await page.locator('[role="tab"][data-pane] .pane-tab-name').allTextContents()
      check(`${where}: each advisor tab has its own name`, names.includes('Advisor: Deploy fix') && names.includes('Advisor 2: Deploy fix'), names.join(' | '))
      check(`${where}: advisor tabs show the advisor icon`, await tab('%2').locator('.tab-advisor').count() === 1 && await tab('%3').locator('.tab-advisor').count() === 1)
      check(`${where}: other tabs have no advisor icon`, await tab('%1').locator('.tab-advisor').count() === 0 && await tab('%4').locator('.tab-advisor').count() === 0)
      const style = id => tab(id).evaluate(node => { const s = getComputedStyle(node); return { glow: node.classList.contains('advising'), animation: s.animationName, shadow: s.boxShadow } })
      const working = await style('%2'), idle = await style('%3'), plain = await style('%4')
      check(`${where}: the working advisor glows`, working.glow && working.shadow !== 'none', JSON.stringify(working))
      check(`${where}: glow ${motion === 'reduce' ? 'is static' : 'pulses'}`, motion === 'reduce' ? working.animation === 'none' : working.animation === 'advise-glow', working.animation)
      check(`${where}: an idle advisor and a working helper do not glow`, !idle.glow && idle.shadow === 'none' && !plain.glow && plain.shadow === 'none', JSON.stringify([idle, plain]))
      check(`${where}: the working dot says Advising`, await tab('%2').locator('[aria-label="Advising"]').count() === 1 && await tab('%4').locator('[aria-label="Working"]').count() === 1)
      check(`${where}: no horizontal page scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      await tab('%2').scrollIntoViewIfNeeded().catch(() => {})
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
