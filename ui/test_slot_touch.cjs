// Serve the build in ui/dist (or DIST) through request routes. Every API request uses fixtures.
const { strict: assert } = require('node:assert')
const { mkdirSync } = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const origin = 'http://127.0.0.1:5142'
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const panes = ['Alpha', 'Beta', 'Gamma'].map((name, i) => ({ id: `%${i + 1}`, tmux_session: '$fixture',
  label: 'Gesture fixture', pane_name: name, agent: 'claude', state: 'Idle', dir: '/srv/fixture', thread: `thread-${i}`, writable: true }))

async function main() {
  const browser = await getBrowser()
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, hasTouch: true, serviceWorkers: 'block' })
  try {
    const page = await context.newPage(), errors = [], writes = []
    page.setDefaultTimeout(15000)
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') { writes.push(endpoint); return route.fulfill({ json: { ok: true } }) }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        const pane = panes.find(item => item.id === url.searchParams.get('session')) || panes[0]
        const fixtures = {
          sessions: { sessions: panes, default_thread: panes[0].thread },
          chat: { messages: [], earlier: 0, limit: 40, session: pane.id, thread: pane.thread, agent: pane.agent, state: 'Idle', writable: true },
          footer: { observed: true }, prefs: { prefs: {}, version: 1 }, host: { name: 'Fixture' },
          'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
          catalog: { commands: [], plugins: [], mcp: [] }, usage: { claude: null, codex: null },
          subagents: { main: {}, agents: [], tasks: [] }, 'codex-runs': { runs: [] },
          'browser/agents': { enabled: false, agents: [], asks: [] },
          'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
        }
        return route.fulfill({ json: fixtures[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/') && request.method() === 'GET') {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (file !== path.join(DIST, 'index.html') && !file.startsWith(DIST + path.sep)) return route.fulfill({ status: 404, body: '' })
        return route.fulfill({ path: file, headers: { 'Cache-Control': 'no-store' } }).catch(() => route.fulfill({ status: 404, body: '' }))
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', voiceOff: true, voiceSound: false,
        sidePins: [{ pane: '%1' }, { pane: '%2' }],
        layout: { desktop: { count: 3, panes: ['%1', '%2', '%3'], sidebar: false, inbox: false } },
      }))
      // Model a tablet with a trackpad. Keep the real width and reduced-motion queries.
      const media = window.matchMedia.bind(window)
      window.matchMedia = query => media(query === '(min-width: 900px) and (hover: hover) and (pointer: fine)' ? '(min-width: 900px)' : query)
      navigator.vibrate = ms => { window.vibrated = ms; return true }
      navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
    })
    await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'domcontentloaded', timeout: 45000 })
    await page.locator('.slot').nth(2).waitFor()
    // The shared browser blocks raw CDP sessions. Dispatch touch events on their original target.
    const input = (type, points = []) => page.evaluate(({ type, points }) => {
      const previous = window.fixtureTouches || []
      const touches = points.map((p, id) => new Touch({ identifier: id, target: previous[id]?.target || document.elementFromPoint(p.x, p.y), clientX: p.x, clientY: p.y }))
      const target = touches[0]?.target || previous[0]?.target
      if (type === 'touchStart' && !previous.length) target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', pointerId: 1 }))
      const event = new TouchEvent(type.toLowerCase(), { bubbles: true, cancelable: true, touches, targetTouches: touches, changedTouches: touches.length ? touches : previous })
      target.dispatchEvent(event)
      window.fixtureTouches = touches
      return event.defaultPrevented
    }, { type, points })
    const point = async selector => {
      const box = await page.locator(selector).boundingBox()
      assert(box, selector)
      return { x: box.x + box.width / 2, y: box.y + box.height / 2 }
    }
    const title = index => `.slot[data-slot="${index}"] .chat-title`
    const labels = () => page.locator('.slot .chat-title h1').allTextContents()
    const shot = async name => {
      if (!process.env.SHOTS) return
      mkdirSync(process.env.SHOTS, { recursive: true })
      await page.screenshot({ path: path.join(process.env.SHOTS, `${name}.png`) })
    }
    const clean = async () => {
      assert.equal(await page.locator('.touch-ghost, [data-carried], [data-touch-drop]').count(), 0)
    }
    const lift = async () => {
      const from = await point(title(0))
      await input('touchStart', [from])
      await page.locator('.touch-ghost').waitFor()
      assert.equal(await page.evaluate(() => window.vibrated), 12)
      assert.equal(await page.locator('.slot[data-slot="0"][data-carried]').count(), 1)
      assert.equal(await page.locator('.touch-ghost').evaluate(el => el.parentElement === document.body && getComputedStyle(el).transitionDuration === '0s'), true)
      return from
    }

    const before = await labels()
    await lift()
    const to = await point('.slot[data-slot="1"]')
    assert.equal(await input('touchMove', [to]), true, 'A lifted move prevents scrolling')
    assert.equal(await page.locator('.slot[data-slot="1"][data-touch-drop]').count(), 1)
    assert.equal(await page.locator('.slot[data-slot="0"][data-touch-drop]').count(), 0)
    const position = await page.locator('.touch-ghost').evaluate(el => { const m = new DOMMatrix(getComputedStyle(el).transform); return { x: m.m41, y: m.m42 } })
    assert(Math.abs(position.x - to.x) < 1 && Math.abs(position.y - to.y) < 1, 'Ghost tracks the finger')
    await shot('wide-carry')
    await input('touchEnd')
    await page.waitForFunction(first => document.querySelector('.slot .chat-title h1').textContent !== first, before[0])
    assert.deepEqual(await labels(), [before[1], before[0], before[2]])
    await clean()
    assert.equal(await page.locator('dialog[open]').count(), 0, 'Release does not open details')
    console.log('Touch: lift, direct tracking, hint, swap and click suppression pass')

    const swapped = await labels()
    await lift(); await input('touchEnd'); await clean()
    await page.locator(title(0)).dispatchEvent('click', { detail: 1 })
    assert.equal(await page.locator('dialog[open]').count(), 0, 'The click after a lift is suppressed')
    assert.deepEqual(await labels(), swapped, 'A stationary lift does nothing')
    await lift(); await input('touchMove', [to]); await input('touchCancel'); await clean()
    assert.deepEqual(await labels(), swapped, 'Cancel does not swap')
    await lift(); await input('touchMove', [to]); await input('touchMove', [{ x: 3, y: 3 }])
    assert.equal(await page.locator('[data-touch-drop]').count(), 0)
    await input('touchEnd'); await clean()
    assert.deepEqual(await labels(), swapped, 'An outside release does nothing')
    const held = await lift()
    await input('touchStart', [held, { x: held.x + 30, y: held.y + 30 }]); await clean()
    await input('touchEnd')
    await clean()
    assert.deepEqual(await labels(), swapped, 'Second finger cancels')
    const pending = await point(title(0))
    await input('touchStart', [pending]); await input('touchStart', [pending, { x: 3, y: 3 }])
    await page.waitForTimeout(450); await clean(); await input('touchEnd')
    console.log('Touch: stationary release, outside release, cancel and second finger pass')

    const start = await point(title(0))
    await input('touchStart', [start]); await input('touchMove', [{ x: start.x, y: start.y + 20 }])
    await page.waitForTimeout(450); await clean(); await input('touchCancel')
    await input('touchStart', [await point('.slot[data-slot="0"] .icon-button[aria-label="Close this slot (Alt+X)"]')])
    await page.waitForTimeout(450); await clean(); await input('touchCancel')
    console.log('Touch: early scroll and header button do not lift')

    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 1024, height: 768 })
    await lift(); await input('touchMove', [await point('.slot[data-slot="1"]')]); await shot('tablet-carry'); await input('touchCancel'); await clean()
    await lift(); await page.setViewportSize({ width: 390, height: 844 }); await clean(); await input('touchCancel')
    const phoneTitle = await point('.chat-header .chat-title')
    await input('touchStart', [phoneTitle])
    await input('touchMove', [{ x: phoneTitle.x - 160, y: phoneTitle.y }]); await input('touchEnd')
    await clean()
    await page.waitForFunction(() => document.querySelector('.chat-title h1').textContent === 'Alpha')
    console.log('Touch: reduced motion, tablet size, unmount cleanup and pinned-header swipe pass')

    await page.setViewportSize({ width: 1280, height: 900 })
    await page.locator('.slot').nth(2).waitFor()
    const mouseBefore = await labels()
    await page.locator(title(0)).dragTo(page.locator('.slot[data-slot="1"] .chat-page .chat-scroll'))
    await page.waitForFunction(first => document.querySelector('.slot .chat-title h1').textContent !== first, mouseBefore[0])
    assert.deepEqual(await labels(), [mouseBefore[1], mouseBefore[0], mouseBefore[2]])
    await page.locator(title(0)).focus(); await page.keyboard.press('Enter')
    await page.locator('dialog[open]').waitFor()
    assert.equal(writes.some(endpoint => endpoint !== 'prefs'), false, `Unexpected write: ${writes}`)
    assert.deepEqual(errors, [])
    console.log('Mouse slot drag and keyboard details pass; no terminal writes or page errors')
  } finally {
    await context.close()
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
