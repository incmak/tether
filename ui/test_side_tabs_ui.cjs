// Fixture-only regression for the Agents and Pinned tabs and the Friday bubble.
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, prefsStore } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const AGENTS = '.agent-bubble:not(.side-pin-bubble)'
const PINS = '.side-pin-bubble'
const FRIDAY = '.live-bubble-hit'
const LIVE_PLACE = 'tether.live-window.v1'
const controls = [
  { name: 'Agents', selector: AGENTS, opened: '.agent-pop:not(.side-pin-pop)', resting: AGENTS + '.docked' },
  { name: 'Pinned', selector: PINS, opened: '.side-pin-pop', resting: PINS + '.docked' },
  { name: 'Friday', selector: FRIDAY, opened: '.live-window', resting: '.live-bubble:not(.moving)' },
]

const panes = ['One', 'Two', 'Three', 'Four'].map((label, i) => ({
  id: '%' + (i + 1), tmux_session: '$1', label, pane_name: label, agent: 'claude', dir: '/srv/project',
  state: i % 2 ? 'Idle' : 'Working', writable: true, thread: 't' + (i + 1), approval: null,
}))
const messages = Array.from({ length: 40 }, (_, i) => ({
  id: 'm' + i, role: i % 2 ? 'assistant' : 'user',
  text: `Message ${i}. Some long text so the chat scrolls and wraps and wraps.`,
  time: '2026-11-11T08:00:00Z',
}))
const fixture = {
  sessions: { sessions: panes, default_thread: 't1' },
  chat: { messages, busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Claude', mode: 'default', notices: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  undo: { entries: [] }, 'browser/agents': { enabled: false, available: false, agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  usage: { claude: null, codex: null }, reminders: { reminders: [], deleted: [], now: 0 },
  feedback: { ratings: {} }, subagents: { main: {}, agents: [], tasks: [] },
}

async function openPage(page, origin, side) {
  page.setDefaultTimeout(10000)
  await page.routeWebSocket('**/*', socket => socket.close())
  const prefs = prefsStore()
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== origin) return route.abort()
    if (url.pathname.startsWith('/mobile/api/')) {
      const endpoint = url.pathname.slice('/mobile/api/'.length)
      if (endpoint === 'prefs') return route.fulfill({ json: request.method() === 'GET' ? prefs.get() : prefs.post(request.postDataJSON()) })
      if (endpoint === 'stream') return route.fulfill({ status: 204 })
      if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
      return route.fulfill({ json: fixture[endpoint] || {} })
    }
    return serveLocal(route)
  })
  await page.addInitScript(side => {
    localStorage.setItem('tmux-chat.prefs.synced', '1')
    localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({
      selected: '%1', theme: 'light', voiceOff: true, sidePins: [{ pane: '%2' }, { pane: '%3' }],
      look: { layout: 'classic', surface: 'flat', backdrop: 'ember' },
    }))
    localStorage.setItem('tether.agent-bubble.v1', JSON.stringify({ side, y: 0.55 }))
    localStorage.setItem('tether.pin-bubble.v1', JSON.stringify({ side: 'right', y: 0.68 }))
    navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Fixture microphone off', 'NotAllowedError') }
  }, side)
  await page.goto(`${origin}/chat/?session=%251`)
}

async function rest(page, selector) {
  await page.locator(selector).waitFor()
  await page.waitForFunction(sel => {
    const el = document.querySelector(sel)
    return el && !el.getAnimations().some(animation => animation.pending || animation.playState === 'running')
  }, selector)
}

async function centre(page, selector) {
  const box = await page.locator(selector).boundingBox()
  assert.ok(box, `Missing box for ${selector}`)
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 }
}

async function assertHeld(page, selector, point, duration, label) {
  const result = await page.locator(selector).evaluate((el, { point, duration }) => new Promise(resolve => {
    const until = performance.now() + duration
    let drift = 0, hit = true
    function frame(now) {
      const box = el.getBoundingClientRect()
      drift = Math.max(drift, Math.hypot(box.x + box.width / 2 - point.x, box.y + box.height / 2 - point.y))
      hit &&= el.contains(document.elementFromPoint(point.x, point.y))
      if (now < until) requestAnimationFrame(frame)
      else resolve({ drift, hit })
    }
    requestAnimationFrame(frame)
  }), { point, duration })
  assert.ok(result.drift <= 2, `${label}: centre moved ${result.drift.toFixed(2)} px`)
  assert.ok(result.hit, `${label}: pointer left the control`)
}

async function drag(page, cdp, selector, dx, label) {
  const from = await centre(page, selector)
  const to = { x: from.x + dx, y: from.y - 80 }
  if (cdp) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [from] })
    for (let i = 1; i <= 8; i++) {
      const point = { x: from.x + dx * i / 8, y: from.y - 80 * i / 8 }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point] })
    }
  } else {
    await page.mouse.move(from.x, from.y)
    await page.mouse.down()
    await page.mouse.move(to.x, to.y, { steps: 8 })
  }
  await page.waitForSelector(selector === FRIDAY ? '.live-bubble.moving' : selector + '.dragging')
  const at = await centre(page, selector)
  const distance = Math.hypot(at.x - to.x, at.y - to.y)
  assert.ok(distance <= 30, `${label}: drag centre is ${distance.toFixed(2)} px from the pointer`)
  if (cdp) await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  else await page.mouse.up()
  assert.equal(await page.evaluate(() => getSelection().toString()), '', `${label}: drag selected text`)
}

async function fridayKeyboardMove(page, label) {
  const before = await centre(page, FRIDAY)
  await page.locator(FRIDAY).press('ArrowUp')
  await rest(page, '.live-bubble')
  const after = await centre(page, FRIDAY)
  assert.ok(Math.hypot(after.x - before.x, after.y - before.y + 16) <= 2, `${label}: ArrowUp must move the bubble 16 px up`)
}

async function fridayPop(page, label) {
  const stored = await page.evaluate(key => JSON.parse(localStorage.getItem(key)).bubble, LIVE_PLACE)
  // Start sampling before shrink so the first painted bubble frame is included.
  await page.evaluate(() => {
    window.popFrames = new Promise(resolve => {
      const frames = []
      let start
      function frame(now) {
        const el = document.querySelector('.live-bubble')
        if (el) {
          start ??= now
          const { x, y, width, height } = el.getBoundingClientRect()
          frames.push({ x, y, width, height })
        }
        if (start === undefined || now - start < 460) requestAnimationFrame(frame)
        else resolve(frames)
      }
      requestAnimationFrame(frame)
    })
  })
  await page.getByRole('button', { name: 'Shrink Friday to a bubble' }).click()
  await rest(page, '.live-bubble')
  const frames = await page.evaluate(() => window.popFrames)
  assert.ok(frames.length > 1, `${label}: expected multiple pop frames`)
  const final = frames.at(-1)
  assert.ok(Math.hypot(final.x - stored.x, final.y - stored.y) <= 2, `${label}: bubble did not return to its stored place`)
  const centreOf = box => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 })
  const end = centreOf(final)
  const drift = Math.max(...frames.map(frame => {
    const at = centreOf(frame)
    return Math.hypot(at.x - end.x, at.y - end.y)
  }))
  assert.ok(drift <= 8, `${label}: a pop frame was ${drift.toFixed(2)} px from the final centre`)
}

async function checkControl(page, cdp, { selector, opened, resting }, side, label) {
  const friday = selector === FRIDAY
  const shrink = page.getByRole('button', { name: 'Shrink Friday to a bubble' })
  if (friday) {
    if (cdp) {
      await page.getByRole('button', { name: /^More/ }).click()
      await page.getByRole('menuitem', { name: 'Friday', exact: true }).click()
    } else await page.getByRole('button', { name: 'Friday Live mode' }).click()
    await rest(page, opened)
    await shrink.click()
    await page.mouse.move(1, 1)
  }
  await rest(page, resting)
  if (!friday) assert.equal(await page.locator(selector).evaluate((el, side) => el.classList.contains(side), side), true, label)
  const start = await centre(page, selector)
  await page.mouse.move(start.x, start.y)
  await assertHeld(page, selector, start, 600, `${label} hover`)
  await page.mouse.down()
  await assertHeld(page, selector, start, 300, `${label} press`)
  await page.mouse.up()
  await rest(page, opened)
  if (friday) await shrink.click()
  else {
    // The tab keeps its place while open, so a second press on the same spot closes it.
    const open = await centre(page, selector)
    assert.ok(Math.hypot(open.x - start.x, open.y - start.y) <= 2, `${label}: the tab moved ${Math.hypot(open.x - start.x, open.y - start.y).toFixed(2)} px when it opened`)
    await page.mouse.click(start.x, start.y)
    await page.waitForFunction(sel => !document.querySelector(sel), opened, { timeout: 2000 })
  }
  await page.mouse.move(1, 1)
  await rest(page, resting)

  await drag(page, cdp, selector, side === 'left' ? 120 : -120, label)
  await rest(page, resting)
  assert.equal(await page.locator(opened).count(), 0, `${label}: drag opened the control`)
  if (friday) {
    await page.mouse.move(1, 1)
    await fridayKeyboardMove(page, label)
  }
  const released = await centre(page, selector)
  await page.mouse.click(released.x, released.y)
  await rest(page, opened)
  if (friday) {
    await page.mouse.move(1, 1)
    await fridayPop(page, label)
    await fridayGrabWhileOpening(page, label)
  }
}

// A grab during the bloom must start from where the window shows, not from the corner.
async function fridayGrabWhileOpening(page, label) {
  await rest(page, '.live-bubble')
  await page.locator(FRIDAY).click()
  await rest(page, '.live-window')
  const target = await page.locator('.live-window').boundingBox()
  await page.getByRole('button', { name: 'Shrink Friday to a bubble' }).click()
  await rest(page, '.live-bubble')
  await page.mouse.move(1, 1)
  await page.locator(FRIDAY).click()
  // Hold the bloom at 200 of 300 ms, so the press lands while it runs, whatever the machine speed.
  await page.waitForFunction(() => document.querySelector('.live-window')?.getAnimations().length > 0)
  await page.evaluate(() => { for (const motion of document.querySelector('.live-window').getAnimations()) { motion.pause(); motion.currentTime = 200 } })
  const grip = await page.locator('.live-grip').boundingBox()
  const x = grip.x + grip.width / 2, y = grip.y + grip.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  assert.ok(await page.evaluate(() => document.querySelector('.live-window').getAnimations().some(motion => motion.playState === 'paused')), `${label}: the bloom ended before the grab`)
  await page.evaluate(() => { for (const motion of document.querySelector('.live-window').getAnimations()) motion.play() })
  await page.mouse.move(x - 45, y + 30, { steps: 5 })
  await page.mouse.up()
  await rest(page, '.live-window')
  const after = await page.locator('.live-window').boundingBox()
  const off = Math.hypot(after.x - target.x, after.y - target.y)
  assert.ok(off >= 20 && off <= 120, `${label}: a grab during the bloom moved the window ${off.toFixed(0)} px (from ${target.x},${target.y} to ${after.x},${after.y})`)
}

async function main() {
  await fs.access(path.join(DIST, 'index.html'))
  const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1')
    try {
      if (req.method !== 'GET' || !url.pathname.startsWith('/chat/') || url.pathname === '/chat/sw.js') throw new Error()
      const file = path.resolve(DIST, url.pathname === '/chat/' ? 'index.html' : '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!file.startsWith(DIST + path.sep)) throw new Error()
      const body = await fs.readFile(file)
      res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' })
      res.end(body)
    } catch { res.writeHead(404); res.end() }
  })
  let browser
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser({ local: true })
    for (const [width, height, touch] of [[1280, 900, false], [390, 844, true]]) {
      const cases = controls.map(control => [control, 'right'])
      if (!touch) cases.push([controls[0], 'left'])
      for (const [control, side] of cases) {
        const context = await browser.newContext({ viewport: { width, height }, hasTouch: touch, isMobile: touch, serviceWorkers: 'block', reducedMotion: 'no-preference' })
        try {
          const page = await context.newPage()
          const cdp = touch ? await context.newCDPSession(page) : null
          const label = `${width}x${height} ${control.name} (${side})`
          await openPage(page, origin, side)
          await checkControl(page, cdp, control, side, label)
          console.log(`PASS ${label}`)
        } finally { await context.close() }
      }
    }
  } finally {
    await browser?.close()
    if (server.listening) await new Promise(resolve => server.close(resolve))
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
