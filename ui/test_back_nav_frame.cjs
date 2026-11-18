// B33: a plain sheet on the entry a frame overlay left must not call history.back() into the frame's own entries.
// Without the Navigation API the page cannot skip those entries, so the sheet's close leaves the entry, like the frame's
// close did. The top page hides window.navigation; the framed Tether at 127.0.0.1 adds its own history entry.
// Run: cd ui && DIST=<build> node test_back_nav_frame.cjs (default ui/dist).
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const paneA = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Fix login', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const paneB = { id: '%2', tmux_session: '$1', label: 'project:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/docs', state: 'Idle', writable: true, thread: 't2', approval: null }
const fixture = {
  sessions: { sessions: [paneA, paneB], default_thread: 't1' },
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
const HOME = 'http://localhost:7801', OTHER = 'http://127.0.0.1:7802', SILENT = 'http://localhost:7803'
const names = { [HOME]: 'Studio', [OTHER]: 'Mac' }
const hosts = { [HOME]: [{ name: 'Mac', url: OTHER }, { name: 'Old', url: SILENT }], [OTHER]: [{ name: 'Studio', url: HOME }, { name: 'Elsewhere', url: 'http://localhost:7809' }] }

async function main() {
  let browser, page, failed = 0
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('popup', popup => void popup.close())
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin === SILENT) return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Old</title><p>Old Tether</p>' })
      if (url.origin !== HOME && url.origin !== OTHER) return route.abort('blockedbyclient')
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        const prefs = { selected: '%1', hosts: hosts[url.origin] }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs, version: 1 } })
        if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
        if (endpoint === 'host') return route.fulfill({ json: { name: names[url.origin], office: false } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404, body: '' })
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
      try { return route.fulfill({ body: await fs.readFile(file), contentType: mime[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
      if (window !== top) return
      // A browser without the Navigation API (Safari before 26.2). Count the page's own history.back() calls.
      Object.defineProperty(window, 'navigation', { value: undefined, configurable: true })
      window.backs = 0
      window.pops = 0
      addEventListener('popstate', () => window.pops++)
      const back = History.prototype.back
      History.prototype.back = function () { window.backs++; return back.call(this) }
    })
    const view = page.locator('dialog.host-view')
    const shortcuts = page.locator('dialog.shortcuts-sheet')
    const topState = () => page.evaluate(() => ({ layer: history.state?.layer, frame: history.state?.frame, length: history.length, backs: window.backs }))
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto(`${HOME}/chat/`, { waitUntil: 'load' })
    await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
    check('the top page has no Navigation API', await page.evaluate(() => window.navigation === undefined))
    await page.locator('.host-switch:visible').first().click()
    await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ }).click()
    await view.waitFor()
    await page.frameLocator('iframe.host-view-frame').locator('.chat-panel[data-pane="%1"]').first().waitFor()
    const framed = page.frames().find(frame => frame.url().startsWith(OTHER))
    const before = await page.evaluate(() => history.length)
    await framed.evaluate(() => history.pushState({ framed: 1 }, '', location.href))
    await page.waitForFunction(length => history.length === length + 1, before)
    const left = await topState()
    check('the frame entry carries the frame flag', left.layer === 1 && left.frame === true, JSON.stringify(left))

    await page.keyboard.press('Escape')
    await view.waitFor({ state: 'detached' })
    const closed = await topState()
    check('the frame close does not Back into the frame', closed.backs === 0 && closed.layer === 1, JSON.stringify(closed))

    // The reviewer's sequence: a plain sheet reuses the entry the frame left, then closes.
    await page.keyboard.press('Control+Slash')
    await shortcuts.waitFor()
    const reused = await topState()
    check('the sheet reuses the frame entry', reused.layer === 1 && reused.frame === true && reused.length === closed.length, JSON.stringify(reused))
    await page.keyboard.press('Control+Slash')
    await shortcuts.waitFor({ state: 'detached' })
    const after = await topState()
    check('the sheet close does not Back into the frame', after.backs === 0 && after.length === closed.length, JSON.stringify(after))

    // Later overlay writes are not blocked: the next sheet opens and closes on its own entry after the user's Back.
    // User Backs first step through the closed frame's own entries (the framed Tether's and the one pushed above); they
    // fire no popstate here. Browser traversals run in order, so the press count is exact. Three presses at most.
    let presses = 0
    while (presses < 3 && await page.evaluate(() => window.pops === 0)) { await page.goBack().catch(() => {}); presses++ }
    await page.waitForFunction(() => window.pops > 0 && (history.state?.layer ?? 0) === 0)
    check('user Back leaves the frame entry without an extra history.back', await page.evaluate(() => window.backs === 0))
    console.log(`user Back presses to layer 0: ${presses}`)
    await page.keyboard.press('Control+Slash')
    await shortcuts.waitFor()
    await page.waitForFunction(() => history.state?.layer === 1 && !history.state?.frame)
    const backs = await page.evaluate(() => window.backs)
    await page.keyboard.press('Control+Slash')
    await shortcuts.waitFor({ state: 'detached' })
    await page.waitForFunction(() => (history.state?.layer ?? 0) === 0)
    check('a later sheet closes with one Back', await page.evaluate(() => window.backs) === backs + 1)
    check('still on this Tether', new URL(page.url()).origin === HOME, page.url())
    check('no page errors', pageErrors.length === 0, pageErrors.join('\n'))
  } catch (error) { failed++; console.log(`FAIL ${error.stack}`) } finally {
    await page?.close().catch(() => {})
    await browser?.close?.().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exitCode = failed ? 1 : 0
}
main()
