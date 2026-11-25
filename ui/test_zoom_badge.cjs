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
          const badge = run.page.getByRole('button', { name: 'Zoomed to 150%. Reset to 100%.' })
          // Wait on the badge state in the page, not on a polled role locator: under load each locator retry costs
          // seconds, so a timed locator fails while the badge is already right. The default timeout only catches a hang.
          await run.page.waitForFunction(() => { const el = document.querySelector('.zoom-badge'); return el.matches(':popover-open') && el.getAttribute('aria-label') === 'Zoomed to 150%. Reset to 100%.' })
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
      await check(`${label}: a tap on the badge returns to 100% or says to pinch out`, async () => {
        const run = await open(browser, viewport, theme)
        try {
          await zoom(run, 1.3)
          // A double tap must not save the temporary clamp as the original meta.
          await run.page.locator('.zoom-badge').tap()
          await run.page.locator('.zoom-badge').tap().catch(() => {})
          // The reset ends when the meta is back and the badge shows the result: hidden at 100%, or Pinch out.
          // Frames and the reset timer can run late on a loaded host, so wait for that state, not for a fixed time.
          await run.page.waitForFunction(() => {
            const el = document.querySelector('.zoom-badge')
            return !document.querySelector('meta[name="viewport"]').content.includes('maximum-scale')
              && (!el.matches(':popover-open') || el.getAttribute('aria-label').includes('Pinch out'))
          }).catch(() => {})
          const end = await run.page.evaluate(() => {
            const el = document.querySelector('.zoom-badge')
            return { scale: visualViewport.scale, shown: el.matches(':popover-open'), label: el.getAttribute('aria-label'), meta: document.querySelector('meta[name="viewport"]').content }
          })
          if (end.scale < 1.01) assert.equal(end.shown, false, 'badge hides at 100%')
          else assert.deepEqual([end.shown, end.label], [true, 'Zoomed to 130%. Pinch out to return to 100%.'], 'badge says Pinch out')
          results.push(`   ${label}: reset ${end.scale < 1.01 ? 'returned to 100%' : `kept ${end.scale}, badge says Pinch out`}`)
          assert.equal(end.meta.includes('maximum-scale'), false, 'meta restored')
        } finally { await run.context.close() }
      })
    }
  } finally { await browser.close() }
  console.log(results.join('\n'))
  console.log(`zoom badge: ${process.exitCode ? 'FAIL' : 'PASS'}`)
}
main().catch(error => { console.error(error); process.exitCode = 1 })
