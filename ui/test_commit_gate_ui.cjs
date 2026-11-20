// The commit gate in Settings › Agents (src/CommitGate.tsx, server/commit_gate.py) against an existing build: the
// skeleton while it loads, the load error, the empty log, the numbers, a mode change, a failed save, the keyboard, and
// 320 px and 1280 px without overflow, in light and dark. Every API request stays in this page's fixtures.
// Run: cd ui && npm run test:ui (or DIST=<build> node test_commit_gate_ui.cjs).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('commit-gate')
const sizes = [[320, 720], [1280, 900]]
const pane = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Fix login', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const fixture = {
  sessions: { sessions: [pane], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { projects: [], items: [] },
}
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const modes = [['off', 'Off', 'Commits are not checked.'], ['observe', 'Observe', 'Each Claude commit is checked and logged. No commit is blocked.'],
  ['enforce', 'Enforce', 'A Claude commit with no valid "Verified:" line is blocked. After 2 blocks in one session, the next one passes.']].map(([mode, name, summary]) => ({ mode, name, summary }))
const none = { days: 7, commits: 0, passed: 0, would_block: 0, blocked: 0, let_through: 0, unknown: 0, stamped: 0, unstamped: 0, false_blocks: 0, rate: null }
const busy = { days: 7, commits: 1240, passed: 1100, would_block: 90, blocked: 6, let_through: 2, unknown: 42, stamped: 1101, unstamped: 97, false_blocks: 3, rate: 0.0027 }
let gate, load, saveFails, loadGate

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
        const mime0 = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime0[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
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
    page.on('console', message => { if (message.type() === 'error' && !message.text().includes('Failed to load resource')) pageErrors.push(message.text()) })  // the 500 and 403 here are on purpose
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') {
          if (endpoint === 'commit-gate') {
            const body = request.postDataJSON()
            if (saveFails) return route.fulfill({ status: 403, json: { error: 'Reload the page and try again.' } })
            gate = { ...gate, global: body.mode, mode: body.mode }
            if (body.mode === 'enforce') load = 'fail'
            return route.fulfill({ json: gate })
          }
          if (endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'commit-gate') {
          if (load === 'wait') await loadGate
          if (load === 'fail') return route.fulfill({ status: 500, json: { error: 'The server could not read the commit gate.' } })
          return route.fulfill({ json: gate })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // Serve the build from here: the shared browser may not reach this process's loopback port.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try {
          const body = await fs.readFile(file)
          return route.fulfill({ body, contentType: mime[path.extname(file)] || 'application/octet-stream' })
        } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const section = page.locator('section[aria-labelledby="settings-commit-gate"]')
    async function open(width, height, scheme) {
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: scheme })
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByRole('searchbox', { name: 'Search settings' }).fill('commit gate')
      await section.waitFor()
    }
    const inside = () => section.evaluate(node => { const box = node.getBoundingClientRect(); return [...node.querySelectorAll('*')].every(item => { const part = item.getBoundingClientRect(); return part.width === 0 || (part.left >= box.left - 1 && part.right <= box.right + 1) }) })
    for (const [width, height] of sizes) {
      const at = `${width}px`
      // Loading, then a load error.
      gate = { modes, global: 'observe', mode: 'observe', source: 'global', project: null, stats: none, max_blocks: 2 }
      let release
      loadGate = new Promise(resolve => { release = resolve })
      load = 'wait'; saveFails = false
      await open(width, height, 'light')
      await section.locator('.skeleton[aria-busy="true"]').waitFor()
      check(`${at} loading announces itself`, (await section.getByRole('status').first().innerText()).includes('Loading the commit gate'))
      check(`${at} loading shows a skeleton and no controls`, await section.getByRole('radio').count() === 0)
      await section.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-loading-${width}.png`) })
      load = 'fail'; release()
      await section.getByRole('alert').filter({ hasText: 'The server could not read the commit gate.' }).waitFor()
      check(`${at} load error names the cause`, await section.getByRole('radio').count() === 0)
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-error-${width}.png`) })

      // The empty log, in Observe (the default).
      load = 'ok'
      await open(width, height, 'light')
      const observe = section.getByRole('radio', { name: 'Observe' }), enforce = section.getByRole('radio', { name: 'Enforce' })
      await observe.waitFor()
      check(`${at} three modes in one group named Mode`, await section.getByRole('group', { name: 'Mode' }).getByRole('radio').count() === 3)
      check(`${at} Observe is the default`, await observe.isChecked())
      check(`${at} empty log says so`, (await section.getByRole('status').textContent()).includes('No commits checked yet.'))
      check(`${at} empty: no page overflow`, !(await overflow()) && await inside())
      await section.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-empty-${width}.png`) })

      gate = { ...gate, stats: null }
      await open(width, height, 'light')
      await section.getByRole('status').filter({ hasText: 'Commit gate stats are temporarily unavailable.' }).waitFor()
      check(`${at} unavailable stats keep the mode controls`, await observe.isChecked() && await observe.isEnabled())
      check(`${at} unavailable stats: no page overflow`, !(await overflow()) && await inside())
      await section.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-unavailable-${width}.png`) })

      // The numbers, a mode change by keyboard, and a failed save.
      gate = { ...gate, stats: busy }
      await open(width, height, 'light')
      await observe.waitFor()
      const status = await section.getByRole('status').textContent()
      check(`${at} shows the counts`, status.includes('1,240 commits checked · 98 with no valid line · 6 blocked, 2 let through · 42 not readable'), status)
      check(`${at} shows the false-block rate`, status.includes('False-block rate: 0.3% (3 of 1,101 commits with a valid line).'), status)
      await observe.focus()
      const [request] = await Promise.all([
        page.waitForRequest(req => req.method() === 'POST' && new URL(req.url()).pathname.endsWith('/api/commit-gate')),
        page.keyboard.press('ArrowRight'),
      ])
      const sent = request.postDataJSON()
      check(`${at} arrow key posts the mode`, JSON.stringify(sent) === '{"mode":"enforce"}', JSON.stringify(sent))
      await section.locator('.codex-level-note').filter({ hasText: 'Enforce.' }).waitFor()
      check(`${at} Enforce is checked and keeps focus`, await enforce.isChecked() && await enforce.evaluate(node => node === document.activeElement))
      await section.getByRole('alert').filter({ hasText: 'The server could not read the commit gate.' }).waitFor()
      check(`${at} failed refresh keeps the saved mode`, await enforce.isChecked())
      load = 'ok'
      const ring = await enforce.evaluate(node => getComputedStyle(node.nextElementSibling).outlineStyle)
      check(`${at} focus ring shows`, ring === 'solid', ring)
      const target = await section.locator('.segmented label').first().boundingBox()
      check(`${at} each mode is a 24 px target or larger`, target.height >= 24 && target.width >= 24, JSON.stringify(target))
      check(`${at} numbers: no page overflow`, !(await overflow()) && await inside())
      await section.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-enforce-${width}.png`) })
      saveFails = true
      await section.locator('.segmented label').filter({ hasText: 'Off' }).click()
      await section.getByRole('alert').filter({ hasText: 'Reload the page and try again.' }).waitFor()
      check(`${at} failed save keeps the saved mode`, await enforce.isChecked())
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-save-error-${width}.png`) })

    }
    // Dark, with the numbers and Enforce.
    await page.addInitScript(() => localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'dark', voiceOff: true, voiceSound: false, selected: '%1' })))
    for (const [width, height] of sizes) {
      const at = `${width}px`
      saveFails = false
      await open(width, height, 'dark')
      await section.getByRole('radio', { name: 'Enforce' }).waitFor()
      check(`${at} dark theme is on`, await page.evaluate(() => { const [r, g, b] = getComputedStyle(document.body).backgroundColor.match(/\d+/g).map(Number); return r + g + b < 200 }))
      await page.evaluate(() => document.fonts.ready)
      check(`${at} dark: no page overflow`, !(await overflow()) && await inside())
      const contrast = await section.evaluate(node => {
        const parse = color => { const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1; const context = canvas.getContext('2d', { willReadFrequently: true }); context.fillStyle = color; context.fillRect(0, 0, 1, 1); return [...context.getImageData(0, 0, 1, 1).data] }
        const light = ([r, g, b]) => { const part = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4 }; return .2126 * part(r) + .7152 * part(g) + .0722 * part(b) }
        const behind = item => { for (let at = item; at; at = at.parentElement) { const color = parse(getComputedStyle(at).backgroundColor); if (color[3] === 255) return color } return parse(getComputedStyle(document.body).backgroundColor) }
        return [...node.querySelectorAll('h3, legend, .segmented span, .setting-note, .switch-row strong, .switch-row small')].map(item => {
          const a = light(parse(getComputedStyle(item).color)), b = light(behind(item))
          return { text: item.textContent.slice(0, 24), ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) }
        }).filter(item => item.ratio < 4.5)
      })
      check(`${at} dark: text contrast is 4.5:1 or more`, contrast.length === 0, JSON.stringify(contrast))
      await section.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `commit-gate-dark-${width}.png`) })
    }
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
  } finally {
    await page?.close().catch(() => {})
    server?.close()
    await browser?.close?.().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exitCode = failed ? 1 : 0
}
main()
