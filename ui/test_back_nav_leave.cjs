// B33: a close must leave a pending document navigation alone. All API responses are fixtures.
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const TIMEOUT = 10000
const pane = { id: '%1', tmux_session: '$1', label: 'Back nav test', agent: 'claude', dir: '/srv/project',
  state: 'Idle', writable: true, thread: 'back-nav-test', approval: null }
const fixture = {
  sessions: { sessions: [pane], default_thread: pane.thread },
  chat: { messages: [], busy: false, thread: pane.thread, agent: pane.agent, writable: true,
    session: pane.id, earlier: 0, limit: 100, state: pane.state, approval: null },
  footer: { observed: true, model: 'Claude', mode: 'default', notices: [] },
  prefs: { prefs: {}, version: 1 },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false },
  host: { name: 'Test server', office: false },
  activity: { enabled: false, days: [], projects: [], publish: { folder: '', file: '', busy: false, pending: false, published: null, last: null } },
  'ui-usage': { visits: 0, clicks: 0, controls: 0, ready: false, top: [], views: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' },
  'codex-runs': { pane: pane.id, running: 0, runs: [] },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'autonomy/review': { due: false, groups: [] },
  'browser/agents': { enabled: false, available: false, agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  usage: { claude: null, codex: null },
  reminders: { reminders: [], deleted: [], now: 0 },
  feedback: { ratings: {} },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
}
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }

async function serveBuild(req, res) {
  const { pathname } = new URL(req.url, 'http://127.0.0.1')
  if (req.method === 'GET' && pathname === '/away') {
    res.writeHead(200, { 'Content-Type': 'text/html' }).end('<!doctype html><title>Navigation destination</title>')
    return
  }
  const file = pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + pathname.slice('/chat'.length))
  if (req.method === 'GET' && pathname.startsWith('/chat/') && !pathname.endsWith('/sw.js') && file.startsWith(DIST + path.sep)) {
    try {
      const body = await fs.readFile(file)
      res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' })
      res.end(body)
      return
    } catch { /* A missing asset gets a 404. */ }
  }
  res.writeHead(404).end()
}

async function openChat(page, url) {
  await page.goto(url)
  await page.waitForSelector('.composer textarea:enabled')
  await page.evaluate(() => { window.backNavLeaveMarker = true })
}

async function openSheet(page, layer = 1) {
  await page.getByRole('button', { name: 'Add photos and files', exact: true }).click()
  await page.waitForSelector('.attach-layer:popover-open .attach-menu')
  if (layer !== null) await page.waitForFunction(layer => history.state?.layer === layer, layer)
}

async function closeSheet(page) {
  await page.locator('.attach-backdrop').click({ position: { x: 20, y: 20 } })
  await page.waitForSelector('.attach-layer', { state: 'detached' })
}

async function escapeSheet(page) {
  await page.getByRole('menuitem', { name: 'Close', exact: true }).press('Escape')
  await page.waitForSelector('.attach-layer', { state: 'detached' })
  await page.waitForFunction(() => history.state?.layer === 0)
}

const stateBeforeClose = 'B33 state before close: '
const shortcuts = 'dialog.shortcuts-sheet'

function waitForConsole(page, message) {
  return page.waitForEvent('console', { predicate: event => event.text() === message }).catch(error => error)
}

async function checked(promise) {
  const result = await promise
  if (result instanceof Error) throw result
  return result
}

async function holdDocument(page, url, overlay, openedState, action) {
  const { promise, resolve } = Promise.withResolvers()
  // This matches only the next document request. Its response stays pending through the close.
  await page.route(reqUrl => reqUrl.href === url, async route => {
    await promise
    await serveLocal(route)
  }, { times: 1 })
  // Watch the old DOM before navigation starts. The browser can pause animation frames while it waits.
  const closed = waitForConsole(page, 'B33 overlay closed')
  await page.evaluate(({ overlay, stateBeforeClose }) => {
    const element = document.querySelector(overlay)
    if (!element) throw new Error(`Expected ${overlay} to be open`)
    // Playwright can wait for a new execution context during reload. Read from the old page on the close input.
    const recordState = event => {
      if (event.key !== 'Escape' && !event.target.closest?.('.attach-backdrop')) return
      console.log(stateBeforeClose + JSON.stringify(history.state, (_, value) => value === undefined ? { undefined: true } : value))
    }
    window.addEventListener('keydown', recordState, true)
    window.addEventListener('click', recordState, true)
    const observer = new MutationObserver(() => {
      if (element.isConnected) return
      observer.disconnect()
      window.removeEventListener('keydown', recordState, true)
      window.removeEventListener('click', recordState, true)
      console.log('B33 overlay closed')
    })
    observer.observe(document.body, { childList: true, subtree: true })
  }, { overlay, stateBeforeClose })
  const beforeClose = page.waitForEvent('console', { predicate: event => event.text().startsWith(stateBeforeClose) }).catch(error => error)
  const request = page.waitForRequest(request => request.isNavigationRequest() && request.url() === url).catch(error => error)
  try {
    await action(async () => {
      const result = await request
      if (result instanceof Error) throw result
    })
    const recorded = await beforeClose
    if (recorded instanceof Error) throw recorded
    const expected = JSON.stringify(openedState, (_, value) => value === undefined ? { undefined: true } : value)
    assert.deepEqual(JSON.parse(recorded.text().slice(stateBeforeClose.length)), JSON.parse(expected),
      "the pending navigation still has the open overlay's history entry")
    const result = await closed
    if (result instanceof Error) throw result
  } finally {
    resolve()
  }
}

async function assertNewDocument(page, url, type) {
  assert.equal(page.url(), url)
  const result = await page.evaluate(() => ({ marker: window.backNavLeaveMarker, type: performance.getEntriesByType('navigation')[0].type }))
  assert.equal(result.marker, undefined, 'the old document is still loaded')
  assert.equal(result.type, type)
}

async function navigateWithSheet(page, url, { rendererInitiated = false, replace = false } = {}) {
  if (rendererInitiated) {
    await page.addInitScript(() => {
      const add = window.addEventListener
      window.addEventListener = function (type, ...args) {
        if (type !== 'beforeunload') add.call(this, type, ...args)
      }
    })
  }
  await openChat(page, url)
  await openSheet(page)
  if (replace) {
    await page.evaluate(() => {
      window.addEventListener('keydown', event => {
        if (event.code !== 'F8') return
        history.replaceState(history.state, '')
        console.log('B33 replaced state')
      }, { once: true, capture: true })
    })
  }
  const openedState = await page.evaluate(() => history.state)
  const backdrop = await page.locator('.attach-backdrop').boundingBox()
  const otherUrl = new URL('/away', url).href
  let outcome
  await holdDocument(page, otherUrl, '.attach-layer', openedState, async requested => {
    if (rendererInitiated) {
      const failed = page.waitForEvent('requestfailed', { predicate: request => request.isNavigationRequest() && request.url() === otherUrl })
        .then(request => { throw new Error(`Navigation failed: ${request.failure().errorText}`) })
      outcome = Promise.race([page.waitForURL(otherUrl), failed]).catch(error => error)
      await page.evaluate(target => { location.href = target }, otherUrl)
    } else outcome = page.goto(otherUrl).catch(error => error)
    await requested()
    if (replace) {
      // The pending navigation can keep Playwright waiting for a new execution context.
      const replaced = waitForConsole(page, 'B33 replaced state')
      await page.keyboard.press('F8')
      await checked(replaced)
    }
    // Locator clicks wait for navigation. Send the pointer to the old page while its response is held.
    await page.mouse.click(backdrop.x + 20, backdrop.y + 20)
  })
  const result = await outcome
  if (result instanceof Error) throw result
  await assertNewDocument(page, otherUrl, 'navigate')
}

async function pushDuringLeave(page, url) {
  await page.addInitScript(() => {
    const push = history.pushState
    history.pushState = function (...args) {
      push.apply(this, args)
      if (history.state?.layer === 2) console.log('B33 pushed layer 2')
    }
    const observer = new MutationObserver(() => {
      if (!document.querySelector('dialog.shortcuts-sheet[open]')) return
      observer.disconnect()
      const closed = new MutationObserver(() => {
        if (document.querySelector('dialog.shortcuts-sheet')) return
        closed.disconnect()
        console.log('B33 shortcuts closed')
      })
      closed.observe(document.body, { childList: true, subtree: true })
    })
    observer.observe(document, { childList: true, subtree: true })
  })
  await openChat(page, url)
  await openSheet(page)
  const openedState = await page.evaluate(() => ({ ...history.state, layer: 2, index: history.state.index + 1 }))
  const backdrop = await page.locator('.attach-backdrop').boundingBox()
  const otherUrl = new URL('/away', url).href
  const pushed = waitForConsole(page, 'B33 pushed layer 2')
  const closed = waitForConsole(page, 'B33 shortcuts closed')
  let outcome
  await holdDocument(page, otherUrl, '.attach-layer', openedState, async requested => {
    outcome = page.goto(otherUrl).catch(error => error)
    await requested()
    await page.keyboard.press('Control+Slash')
    await checked(pushed)
    await page.keyboard.press('Control+Slash')
    await checked(closed)
    await page.mouse.click(backdrop.x + 20, backdrop.y + 20)
  })
  await checked(outcome)
  await assertNewDocument(page, otherUrl, 'navigate')
}

const cases = [
  ['Settings Escape during reload', { width: 1280, height: 900 }, async (page, url) => {
    await openChat(page, url)
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    const settings = page.getByRole('dialog', { name: 'Settings', exact: true })
    await page.waitForFunction(() => history.state?.layer === 1)
    const openedState = await page.evaluate(() => history.state)
    await settings.getByRole('button', { name: 'About & updates', exact: true }).click()
    url = page.url()
    const finished = page.waitForEvent('requestfinished', { predicate: request => request.isNavigationRequest() && request.url() === url })
    const failed = page.waitForEvent('requestfailed', { predicate: request => request.isNavigationRequest() && request.url() === url })
      .then(request => { throw new Error(`Reload failed: ${request.failure().errorText}`) })
    const outcome = Promise.race([finished, failed]).catch(error => error)
    await holdDocument(page, url, 'dialog.preferences', openedState, async requested => {
      await settings.getByRole('button', { name: 'Reload', exact: true }).click({ noWaitAfter: true })
      await requested()
      await page.keyboard.press('Escape')
    })
    const result = await outcome
    if (result instanceof Error) throw result
    await page.waitForLoadState('load')
    await assertNewDocument(page, url, 'reload')
  }],
  ['Attach backdrop during address-bar navigation', { width: 390, height: 844 }, navigateWithSheet],
  ['Attach backdrop during renderer navigation without beforeunload', { width: 390, height: 844 },
    (page, url) => navigateWithSheet(page, url, { rendererInitiated: true })],
  ['Attach backdrop after replaceState during renderer navigation', { width: 390, height: 844 },
    (page, url) => navigateWithSheet(page, url, { rendererInitiated: true, replace: true })],
  ['Push and close a second overlay during address-bar navigation', { width: 390, height: 844 }, pushDuringLeave],
  ['Nested attach cycles keep history stable after a 204', { width: 390, height: 844 }, async (page, url) => {
    await page.addInitScript(() => {
      window.backNavNavigateErrors = 0
      window.navigation?.addEventListener('navigateerror', () => {
        window.backNavNavigateErrors++
        console.log('B33 navigateerror')
      })
    })
    await openChat(page, url)
    const openBoth = async layer => {
      await openSheet(page, layer)
      await page.keyboard.press('Control+Slash')
      await page.waitForSelector(shortcuts + '[open]')
      await page.waitForFunction(() => history.state?.layer === 2)
    }
    const closeBoth = async () => {
      await page.keyboard.press('Control+Slash')
      await page.waitForSelector(shortcuts, { state: 'detached' })
      await closeSheet(page)
    }
    await openBoth()
    const entries = await page.evaluate(() => history.length)
    const errorsBefore = await page.evaluate(() => window.backNavNavigateErrors)
    const noContent = new URL('/chat/no-content', url).href
    const response = page.waitForResponse(noContent).catch(error => error)
    const finished = Promise.race(['requestfinished', 'requestfailed'].map(event => page.waitForEvent(event, {
      predicate: request => request.isNavigationRequest() && request.url() === noContent,
    }))).catch(error => error)
    await page.evaluate(target => { location.href = target }, noContent)
    const result = await response
    if (result instanceof Error) throw result
    assert.equal(result.status(), 204)
    const request = await finished
    if (request instanceof Error) throw request
    const errors = await page.evaluate(() => window.backNavNavigateErrors) - errorsBefore
    console.log(`204: navigateerror ${errors ? `fired (${errors})` : 'did not fire'}`)
    await closeBoth()
    for (let cycle = 1; cycle <= 3; cycle++) {
      await openBoth(null)
      await closeBoth()
      assert.equal(await page.evaluate(() => history.length), entries, `history grew on cycle ${cycle}`)
    }
    assert.equal(await page.evaluate(() => history.state?.layer), 2, 'the closes leave the layer-2 entry in place')
    await page.goBack({ waitUntil: 'commit' })
    await page.waitForFunction(() => history.state?.layer === 0)
    assert.equal(await page.evaluate(() => window.backNavLeaveMarker), true, 'Back stays in the original document')
    await openSheet(page)
    await escapeSheet(page)
    assert.ok(await page.evaluate(() => history.length) <= entries)
    assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Add photos and files')
    await page.waitForSelector('.composer textarea:enabled')
  }],
  ['Back clears the leave mark after the user stops a navigation', { width: 390, height: 844 }, async (page, url) => {
    // Chromium fires no event when a navigation ends without a new page (a 204, or Stop). The mark stays until Back.
    await page.addInitScript(() => {
      window.backNavNavigateErrors = 0
      window.navigation?.addEventListener('navigateerror', () => { window.backNavNavigateErrors++ })
    })
    await openChat(page, url)
    const otherUrl = new URL('/away', url).href
    const { promise, resolve } = Promise.withResolvers()
    await page.route(reqUrl => reqUrl.href === otherUrl, async route => {
      await promise
      await route.abort().catch(() => {})
    }, { times: 1 })
    await page.evaluate(() => {
      window.addEventListener('keydown', event => {
        if (event.code !== 'F9') return
        window.stop()
        console.log('B33 stopped')
      }, { once: true, capture: true })
    })
    const request = page.waitForRequest(request => request.isNavigationRequest() && request.url() === otherUrl).catch(error => error)
    const stopped = waitForConsole(page, 'B33 stopped')
    try {
      await page.evaluate(target => { location.href = target }, otherUrl)
      const result = await request
      if (result instanceof Error) throw result
      await page.keyboard.press('F9')
      await checked(stopped)
    } finally {
      resolve()
    }
    assert.equal(await page.evaluate(() => window.backNavLeaveMarker), true, 'Stop keeps the original document')
    const errors = await page.evaluate(() => window.backNavNavigateErrors)
    console.log(`stop: navigateerror ${errors ? `fired (${errors})` : 'did not fire'}`)
    // The navigate event marked the page as leaving. A close now leaves its entry in place.
    await openSheet(page)
    await closeSheet(page)
    assert.equal(await page.evaluate(() => history.state?.layer), 1, 'while marked, the close leaves its entry')
    // One Back clears the mark (backNav.ts popstate listener). It lands on the view the user already sees.
    await page.goBack({ waitUntil: 'commit' })
    await page.waitForFunction(() => history.state?.layer === 0)
    assert.equal(await page.evaluate(() => window.backNavLeaveMarker), true, 'Back stays in the original document')
    // After that Back a close steps back again.
    await openSheet(page)
    await escapeSheet(page)
    return `navigateerror ${errors ? 'fired' : 'did not fire'} on Stop`
  }],
  ['bfcache', { width: 390, height: 844 }, async (page, url) => {
    await openChat(page, url)
    await openSheet(page)
    const originalUrl = page.url()
    await page.goto(new URL('/away', url).href)
    await page.goBack({ waitUntil: 'commit' })
    assert.equal(page.url(), originalUrl)
    await page.waitForFunction(() => typeof window.backNavRestored === 'boolean')
    if (!await page.evaluate(() => window.backNavRestored)) return 'unverified (not restored)'
    await escapeSheet(page)
    return 'pageshow.persisted = true'
  }],
]

async function main() {
  await fs.access(path.join(DIST, 'index.html'))
  const server = http.createServer(serveBuild)
  let browser
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    const url = `${origin}/chat/?session=%251`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context) throw new Error('Browser has no context')
    const selected = process.argv.includes('--pending-only') ? cases.slice(0, 3) : cases
    let passed = 0, failed = 0, unverified = 0
    for (const [name, viewport, run] of selected) {
      if (context.pages().length >= 3) throw new Error('Shared browser already has three tabs')
      const page = await context.newPage()
      try {
        page.setDefaultTimeout(TIMEOUT)
        page.setDefaultNavigationTimeout(TIMEOUT)
        await page.setViewportSize(viewport)
        await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
        await page.routeWebSocket('**/*', socket => socket.close())
        await page.route('**/*', async route => {
          const { pathname, origin: requestOrigin } = new URL(route.request().url())
          if (requestOrigin !== origin) return route.abort('blockedbyclient')
          if (pathname.includes('/api/')) {
            const endpoint = pathname.split('/api/')[1]
            if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
            return route.fulfill({ json: fixture[endpoint] ?? {} })
          }
          if (pathname === '/chat/no-content') return route.fulfill({ status: 204, body: '' })
          if (pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
          return serveLocal(route)
        })
        await page.addInitScript(() => {
          if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
          localStorage.setItem('tmux-chat.prefs.synced', '1')
          localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true,
            look: { layout: 'classic', surface: 'flat' } }))
          window.addEventListener('pageshow', event => { window.backNavRestored = event.persisted })
        })
        const result = await run(page, url)
        if (result?.startsWith('unverified')) { unverified++; console.log(`${name}: ${result}`) }
        else { passed++; console.log(`ok ${name}${result ? ` (${result})` : ''}`) }
      } catch (error) {
        failed++
        console.error(`FAIL ${name}: ${error.message}`)
      } finally {
        await page.close()
      }
    }
    console.log(`back nav leave: ${failed ? 'FAIL' : 'PASS'} (${passed} passed, ${failed} failed, ${unverified} unverified)`)
    if (failed) process.exitCode = 1
  } finally {
    if (browser) await browser.close().catch(() => {}) // Disconnect CDP without closing the shared context.
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1 })
