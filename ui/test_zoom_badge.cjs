// A pinch zoom shows a badge with the zoom percent in the bottom right corner; a tap on it returns to 100%
// (src/ZoomBadge.tsx). Real browser against a build: DIST (default dist). Run: node test_zoom_badge.cjs
const { strict: assert } = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const ORIGIN = 'http://tether.test'
const SHOTS = process.env.SHOTS
const pane = { id: '%1', tmux_session: '$1', label: 'Zoom test', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 'zoom-thread', approval: null }
const fixtures = {
  sessions: { sessions: [pane], default_thread: pane.thread },
  chat: { messages: Array.from({ length: 30 }, (_, i) => ({ id: `message-${i}`, role: i % 2 ? 'assistant' : 'user', text: `History item ${i + 1}. A fixture conversation with enough text to scroll.`, time: '2026-11-11T08:00:00Z' })), busy: false, thread: pane.thread, agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Claude', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}
const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }

async function open(browser, viewport, theme) {
  const context = await browser.newContext({ viewport, colorScheme: theme, isMobile: true, hasTouch: true, deviceScaleFactor: 3, serviceWorkers: 'block' })
  await context.route(`${ORIGIN}/**`, route => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/mobile/api/')) {
      const name = url.pathname.slice('/mobile/api/'.length)
      return name === 'stream' ? route.fulfill({ status: 204 }) : route.fulfill({ json: fixtures[name] ?? {} })
    }
    if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404 })
    const file = path.join(DIST, url.pathname.slice('/chat/'.length))
    const served = fs.existsSync(file) && fs.statSync(file).isFile() ? file : path.join(DIST, 'index.html')
    return route.fulfill({ status: 200, contentType: types[path.extname(served)] ?? 'application/octet-stream', body: fs.readFileSync(served) })
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${ORIGIN}/chat/`)
  await page.waitForSelector('.zoom-badge', { state: 'attached', timeout: 20000 })
  await page.waitForLoadState('networkidle')
  const cdp = await context.newCDPSession(page)
  return { context, page, cdp, errors }
}

// The badge box in visual viewport CSS px (what the user sees), from its layout box and the zoom.
const PLACEMENT = () => {
  const el = document.querySelector('.zoom-badge'), view = visualViewport
  const r = el.getBoundingClientRect()
  const toView = (x, y) => [(x - view.offsetLeft) * view.scale, (y - view.offsetTop) * view.scale]
  const [left, top] = toView(r.left, r.top), [right, bottom] = toView(r.right, r.bottom)
  return { left, top, right, bottom, viewWidth: view.width * view.scale, viewHeight: view.height * view.scale, open: el.matches(':popover-open') }
}
const placement = page => page.evaluate(PLACEMENT)
// What the badge says and whether it shows, with the zoom and the viewport meta.
const BADGE_STATE = () => {
  const el = document.querySelector('.zoom-badge')
  return { scale: visualViewport.scale, shown: el.matches(':popover-open'), label: el.getAttribute('aria-label'), meta: document.querySelector('meta[name="viewport"]').content }
}
const badgeState = page => page.evaluate(BADGE_STATE)
// Waits in the page until test(BADGE_STATE(), arg) holds. The default timeout only catches a hang, so on a timeout
// it returns and the assertion that follows reports the state the badge stopped in.
const settle = (page, test, arg) => page.waitForFunction(`(${test})((${BADGE_STATE})(), ${JSON.stringify(arg)})`).catch(() => {})

async function zoom(run, scale) {
  await run.cdp.send('Emulation.setPageScaleFactor', { pageScaleFactor: scale })
  await run.page.waitForFunction(s => Math.abs(visualViewport.scale - s) < 0.005, scale)
}

async function main() {
  // Pinch zoom needs a CDP session, which the shared browser's proxy does not allow: use a local browser.
  const browser = await getBrowser({ local: true })
  const results = []
  const check = async (name, fn) => {
    try { await fn(); results.push(`ok ${name}`) } catch (error) { results.push(`FAIL ${name}: ${error.message.split('\n')[0]}`); process.exitCode = 1 }
  }
  try {
    for (const [label, viewport, theme] of [['phone 320 dark', { width: 320, height: 640 }, 'dark'], ['phone 412 light', { width: 412, height: 915 }, 'light']]) {
      await check(`${label}: no badge at 100%`, async () => {
        const run = await open(browser, viewport, theme)
        try {
          await run.page.waitForTimeout(300)
          assert.equal(await run.page.locator('.zoom-badge').isVisible(), false)
        } finally { await run.context.close() }
      })
      await check(`${label}: pinch zoom shows the percent in the bottom right corner, at normal size`, async () => {
        const run = await open(browser, viewport, theme)
        try {
          await zoom(run, 1.5)
          const name = 'Zoomed to 150%. Reset to 100%.'
          // Wait on the badge state in the page, not on a polled role locator: under load each locator retry costs
          // seconds, so a timed locator fails while the badge is already right.
          await settle(run.page, (s, name) => s.shown && s.label === name, name)
          const zoomed = await badgeState(run.page)
          assert.deepEqual([zoomed.shown, zoomed.label], [true, name], `the badge opens with the 150% name (${JSON.stringify(zoomed)})`)
          const badge = run.page.getByRole('button', { name })
          assert.equal(await badge.count(), 1, 'the badge has the 150% name')
          assert.equal((await badge.textContent()).trim(), '150%')
          const box = await placement(run.page)
          assert.ok(box.open, 'badge is in the top layer')
          assert.ok(Math.abs(box.viewWidth - box.right - 12) < 2, `12px from the right edge (${box.viewWidth - box.right})`)
          assert.ok(Math.abs(box.viewHeight - box.bottom - 68) < 2, `68px from the bottom edge (${box.viewHeight - box.bottom})`)
          assert.ok(box.bottom - box.top >= 43.5, `44px tall on screen (${box.bottom - box.top})`)
          assert.ok(box.left > 0, 'fully on screen')
          // The keyboard or browser bars change the viewport height at the same zoom: the badge moves with the edge.
          await run.page.setViewportSize({ width: viewport.width, height: viewport.height - 200 })
          await run.page.waitForFunction(h => Math.abs(visualViewport.height * visualViewport.scale - h) < 2, viewport.height - 200)
          // The badge moves in the animation frame after the viewport resize event, which a loaded host can delay
          // past any fixed wait: wait for the move itself, with the default timeout as a hang guard. On timeout the
          // assertion below reports where the badge stopped.
          await run.page.waitForFunction(`(p => Math.abs(p.viewHeight - p.bottom - 68) < 2)((${PLACEMENT})())`).catch(() => {})
          const moved = await placement(run.page)
          assert.ok(Math.abs(moved.viewHeight - moved.bottom - 68) < 2, `stays 68px from the new bottom edge (${moved.viewHeight - moved.bottom})`)
          await run.page.setViewportSize(viewport)
          if (SHOTS) await run.page.screenshot({ path: path.join(SHOTS, `zoom-badge-${label.replaceAll(' ', '-')}.png`) })
          assert.deepEqual(run.errors, [])
        } finally { await run.context.close() }
      })
      // The reset ends when the original meta is back and the badge shows the result: hidden at 100%, or Pinch out
      // above it. Frames and the reset timer run late on a loaded host, so each check waits for that state.
      const done = (s, want) => s.meta === want.original
        && (s.scale < 1.01 ? !s.shown : s.shown && s.label === `Zoomed to ${want.percent}%. Pinch out to return to 100%.`)
      const ended = async (run, original, percent, step) => {
        const want = { original, percent }
        await settle(run.page, done, want)
        const end = await badgeState(run.page)
        assert.ok(done(end, want), `${step} ends with the original meta and a matching badge (${JSON.stringify(end)}, original meta ${original})`)
        results.push(`   ${label}: ${step} ${end.scale < 1.01 ? 'returned to 100%' : `kept ${end.scale}, badge says Pinch out`}`)
      }
      await check(`${label}: a tap on the badge returns to 100% or says to pinch out`, async () => {
        const run = await open(browser, viewport, theme)
        try {
          const original = (await badgeState(run.page)).meta
          // A real tap: hit testing and touch input must reach the badge.
          await zoom(run, 1.3)
          await run.page.locator('.zoom-badge').tap()
          await ended(run, original, 130, 'tap')
        } finally { await run.context.close() }
      })
      // Its own page: after a reset to 100%, a new emulated zoom in the same page can stay at 100% under load.
      await check(`${label}: a double tap keeps the original viewport meta`, async () => {
        const run = await open(browser, viewport, theme)
        try {
          const original = (await badgeState(run.page)).meta
          await zoom(run, 1.3)
          // A double tap must not save the temporary clamp as the original meta. Both clicks run in one page task,
          // so the second always meets the active clamp, however slow the host.
          const clamped = await run.page.evaluate(() => {
            const el = document.querySelector('.zoom-badge')
            el.click()
            const during = document.querySelector('meta[name="viewport"]').content
            el.click()
            return during
          })
          assert.ok(clamped.includes('maximum-scale=1'), `the first tap clamps the zoom (${clamped})`)
          await ended(run, original, 130, 'double tap')
        } finally { await run.context.close() }
      })
    }
  } finally { await browser.close() }
  console.log(results.join('\n'))
  console.log(`zoom badge: ${process.exitCode ? 'FAIL' : 'PASS'}`)
}
main().catch(error => { console.error(error); process.exitCode = 1 })
