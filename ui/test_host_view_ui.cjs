// Other hosts inside the app (src/HostView.tsx, src/HostMenu.tsx) against an existing build. Two origins serve the
// same build: "localhost" is this Tether and "127.0.0.1" is the other one. A tap on the other host opens it in a frame
// inside this page, not in a browser tab; Back comes home; the framed Tether's own host menu can send you home.
// In the frame, Settings › Notifications offers the server's own window, where the browser lets it ask (B187).
// Every API request stays in this page's fixtures.
// Run: cd ui && npm run test:ui (or DIST=<build> node test_host_view_ui.cjs).
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-17/host-view/shots')
const sizes = [[320, 720], [1280, 900]]
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
// SILENT is an old server: a page that never says it started.
const hosts = { [HOME]: [{ name: 'Mac', url: OTHER }, { name: 'Old', url: SILENT }], [OTHER]: [{ name: 'Studio', url: HOME }, { name: 'Elsewhere', url: 'http://localhost:7809' }] }

async function main() {
  let browser, page, failed = 0
  const pageErrors = [], popups = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('popup', popup => { popups.push(popup.url()); void popup.close() })
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
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const inert = () => page.evaluate(() => !!document.querySelector('dialog.host-view[open]'))
    const view = page.locator('dialog.host-view')
    const openMenu = async (width, where = page) => {
      const visible = where.locator('.host-switch:visible')
      if (width < 800 && !(await visible.count())) await where.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await visible.first().click()
    }
    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      await page.goto(`${HOME}/chat/`, { waitUntil: 'load' })
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      await openMenu(width)
      const link = page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ })
      check(`${at} link keeps the address for a new tab`, await link.getAttribute('href') === `${OTHER}/chat/`)
      await link.click()
      await view.waitFor()
      check(`${at} opens inside the app, no browser tab`, popups.length === 0, popups.join(' '))
      check(`${at} frame shows the other host`, await view.locator('iframe').getAttribute('src') === `${OTHER}/chat/`)
      const frame = page.frameLocator('iframe.host-view-frame')
      await frame.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      check(`${at} the other Tether runs in the frame`, (await frame.locator('.host-switch-name').first().textContent()) === 'Mac')
      check(`${at} view is a modal dialog`, await inert())
      check(`${at} Back has focus`, await page.evaluate(() => document.activeElement?.classList.contains('host-view-back')))
      check(`${at} no slow-load note`, await page.locator('.host-view-note').count() === 0)
      check(`${at} no page overflow`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `host-view-${width}.png`) })
      await view.getByRole('button', { name: 'Studio' }).click()
      await view.waitFor({ state: 'detached' })
      check(`${at} Back comes home`, !(await inert()))
      await page.waitForFunction(() => document.activeElement?.classList.contains('host-switch'))
      check(`${at} focus returns to the host button`, true)
      // Review finding: open, close, choose another pane, open again, then Back. The view closes and the pane stays.
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) { await page.keyboard.press('Escape'); await page.waitForFunction(() => !document.querySelector('dialog.drawer[open]')) }
      await page.getByRole('tab', { name: /Docs pass/ }).first().click()
      await page.waitForFunction(() => /Docs pass/.test(document.title) || !!document.querySelector('.chat-panel[data-pane="%2"]'))
      await openMenu(width)
      await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ }).click()
      await view.waitFor()
      await frame.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      for (let step = 0; step < 6 && await inert(); step++) await page.goBack().catch(() => {})
      await view.waitFor({ state: 'detached' })
      check(`${at} Back closes the view and keeps the chosen pane`, await page.locator('.chat-panel[data-pane="%2"]').count() > 0)
      // On a phone the drawer was open under the view: one Back closes it first.
      if (width < 800) { await page.goBack().catch(() => {}); await page.waitForFunction(() => !document.querySelector('dialog.drawer[open]')) }
      await page.goBack().catch(() => {})
      await page.waitForTimeout(400)
      check(`${at} the next Back goes to the earlier pane, no dead step`, !(await inert()) && await page.locator('.chat-panel[data-pane="%1"]').count() > 0, await page.evaluate(() => JSON.stringify([history.length, history.state, [...document.querySelectorAll('.chat-panel')].map(e => e.dataset.pane), !!document.querySelector('dialog.drawer[open]')])))

      await openMenu(width)
      await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ }).click()
      await view.waitFor()
      // Back steps through the framed Tether's own entries first, then closes the view.
      for (let step = 0; step < 6 && await inert(); step++) await page.goBack().catch(() => {})
      await view.waitFor({ state: 'detached' })
      check(`${at} browser Back closes the view`, !(await inert()))
      check(`${at} still on this Tether`, new URL(page.url()).origin === HOME, page.url())

      await openMenu(width)
      await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ }).click()
      await view.waitFor()
      await frame.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      await openMenu(width, frame)
      await frame.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Studio/ }).click()
      await view.waitFor({ state: 'detached' })
      check(`${at} the framed host menu sends you home`, !(await inert()) && popups.length === 0, popups.join(' '))

      // A host that the framed Tether lists but this one does not: the request is ignored.
      await openMenu(width)
      await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ }).click()
      await view.waitFor()
      await frame.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      await openMenu(width, frame)
      await frame.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Elsewhere/ }).click()
      await page.waitForTimeout(500)
      check(`${at} an unknown host from the frame stays shut`, await view.locator('iframe').getAttribute('src') === `${OTHER}/chat/` && popups.length === 0)
      await view.getByRole('button', { name: 'Back to Studio' }).click()
      await view.waitFor({ state: 'detached' })
    }
    // Notifications in the frame: the browser refuses the permission prompt there, so Settings offers the server's own
    // window, opened at its notification setting.
    // A long host name must wrap inside the hint and its link at 320 px.
    names[OTHER] = 'workstation-with-a-long-hostname'
    for (const [width, height] of sizes) {
      const at = `${width}px framed`
      await page.setViewportSize({ width, height })
      await page.goto(`${HOME}/chat/`, { waitUntil: 'load' })
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      await openMenu(width)
      await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Mac/ }).click()
      await view.waitFor()
      const frame = page.frameLocator('iframe.host-view-frame')
      await frame.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) await frame.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await frame.getByRole('button', { name: 'Settings' }).first().click()
      await frame.getByRole('searchbox', { name: 'Search settings' }).fill('push notifications')
      const hint = frame.locator('.framed-notifications')
      await hint.waitFor()
      check(`${at} names the server's own window`, (await hint.textContent()).includes(`Turn on notifications in ${names[OTHER]}'s own window`))
      const own = hint.getByRole('link', { name: `Open ${names[OTHER]} in the browser` })
      check(`${at} link opens the notification setting in a new window`, await own.getAttribute('href') === `${OTHER}/chat/?go=notifications` && await own.getAttribute('target') === '_blank')
      check(`${at} no switch that cannot work`, await frame.getByRole('switch', { name: /Push notifications/ }).count() === 0)
      check(`${at} no page overflow`, !(await overflow()) && await page.frames()[1].evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      const box = await own.boundingBox()
      check(`${at} the link fits the screen`, box && box.x >= 0 && box.x + box.width <= width, JSON.stringify(box))
      await page.screenshot({ path: path.join(SHOTS, `host-view-notifications-${width}.png`) })
      await view.getByRole('button', { name: 'Back to Studio' }).click()
      await view.waitFor({ state: 'detached' })
    }
    // The server's own window, opened from that link: Settings shows the notification switch.
    await page.goto(`${HOME}/chat/?go=notifications`, { waitUntil: 'load' })
    const push = page.getByRole('switch', { name: /Push notifications/ })
    await push.waitFor()
    check('?go=notifications opens the notification setting', await push.isVisible() && new URL(page.url()).search === '')
    await page.keyboard.press('Escape')
    await page.goto(`${HOME}/chat/`, { waitUntil: 'load' })
    await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
    // An old server never says it started: after 10 s a note offers the browser.
    await openMenu(1280)
    await page.getByRole('group', { name: 'Tether hosts' }).getByRole('link', { name: /Old/ }).click()
    await view.waitFor()
    await page.locator('.host-view-note').waitFor({ timeout: 13000 })
    check('a silent host gets the browser note', (await page.locator('.host-view-note a').getAttribute('href')) === `${SILENT}/chat/`)
    await page.keyboard.press('Escape')
    await view.waitFor({ state: 'detached' })
    check('Escape comes home', !(await inert()))
    check('no page errors', pageErrors.length === 0, pageErrors.join('\n'))
  } catch (error) { failed++; console.log(`FAIL ${error.stack}`) } finally {
    await page?.close().catch(() => {})
    await browser?.close?.().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exitCode = failed ? 1 : 0
}
main()
