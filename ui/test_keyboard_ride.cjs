// One viewport resize stands in for the Android IME. All API calls use fixtures.
const http = require('node:http'), fs = require('node:fs/promises'), path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || '/tmp'
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }
const phone = { width: 390, height: 844 }
const keyboard = { ...phone, height: 544 }
const pane = { id: '%1', tmux_session: '$1', label: 'project:0.1', pane_name: 'Keyboard ride', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1' }
const fixtures = {
  sessions: { sessions: [pane], default_thread: 't1' },
  prefs: { prefs: {}, version: 1 },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] }, 'browser/tabs': { tabs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

let failed = 0
function check(name, ok, evidence) {
  if (!ok) failed++
  console.log(`${ok ? 'ok' : 'FAIL'} ${name}${evidence === undefined ? '' : ' ' + JSON.stringify(evidence)}`)
}

async function open(context, origin, count) {
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => { if (response.status() >= 400 && !response.url().endsWith('/sw.js')) errors.push(`${response.status()} ${response.url()}`) })
  const messages = Array.from({ length: count }, (_, i) => ({ id: `m${i}`, role: i % 2 ? 'assistant' : 'user', text: i % 2 ? 'The numbers match across all statements.\n\n- The balance sheet balances.\n- The cash flows reconcile.' : 'Can you check the statements?' }))
  await page.routeWebSocket('**/*', socket => socket.close())
  await page.route('**/*', async route => {
    const { pathname } = new URL(route.request().url())
    if (pathname.startsWith('/mobile/api/')) {
      const endpoint = pathname.slice(12)
      if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
      if (endpoint === 'chat') return route.fulfill({ json: { messages, busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: count, state: 'Idle', approval: null } })
      return route.fulfill({ json: fixtures[endpoint] || {} })
    }
    if (pathname.startsWith('/chat/') && !pathname.endsWith('/sw.js')) {
      const file = path.resolve(DIST, '.' + decodeURIComponent(pathname.slice(5)), ...(pathname === '/chat/' ? ['index.html'] : []))
      if (file.startsWith(DIST + path.sep)) {
        try { return await route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { /* Missing fixture asset. */ }
      }
    }
    return route.fulfill({ status: 404, body: '' })
  })
  await page.addInitScript(() => {
    localStorage.setItem('tmux-chat.prefs.synced', '1')
    localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true }))
  })
  await page.goto(`${origin}/chat/?session=%251`)
  await page.locator('.phone-tabbar').waitFor().catch(async error => { throw new Error([...errors, await page.locator('body').innerText(), error.message].join('\n')) })
  await page.waitForFunction(n => {
    const scroller = document.querySelector('.chat-page .chat-scroll')
    return scroller?.querySelectorAll('[data-msg-id]').length === n && scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 2
  }, count)
  await page.evaluate(() => {
    const shell = document.querySelector('main.app-shell')
    const box = shell.querySelector('.chat-page .composer')
    const bar = shell.querySelector('.phone-tabbar')
    const viewport = shell.querySelector('.chat-page .chat-scroll')
    const message = [...shell.querySelectorAll('.chat-page .messages [data-msg-id]')].at(-1)
    let resizedAt
    addEventListener('resize', () => { resizedAt = performance.now() })
    window.readRide = (frameTime = document.timeline.currentTime) => {
      const t = performance.now()
      const rect = box.getBoundingClientRect()
      const animation = shell.getAnimations().find(a => a.effect.getKeyframes().some(key => key.bottom !== undefined))
      return { t, frameTime, resizedAt, start: animation?.startTime ?? null, h: innerHeight,
        bottom: rect.bottom, top: rect.top, message: message.getBoundingClientRect().bottom,
        scrollTop: viewport.scrollTop,
        display: getComputedStyle(bar).display, keyboard: document.documentElement.hasAttribute('data-keyboard'),
        riding: !!animation }
    }
    // The chat's observer pins the scroll before paint, after the rAF callbacks.
    const afterScroll = new ResizeObserver(() => {
      const frame = window.rideFrames?.at(-1)
      if (frame?.frameTime !== document.timeline.currentTime) return
      frame.message = message.getBoundingClientRect().bottom
      frame.scrollTop = viewport.scrollTop
    })
    afterScroll.observe(viewport)
    window.recordRide = () => {
      const frames = window.rideFrames = [window.readRide()]
      const tick = t => {
        if (window.rideFrames !== frames) return
        frames.push(window.readRide(t))
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    }
  })
  return page
}

const read = page => page.evaluate(() => window.readRide())
const record = page => page.evaluate(() => window.recordRide())
const waitResize = (page, height) => page.waitForFunction(h => window.rideFrames.some(f => f.h === h), height)
const settle = (page, height) => page.waitForFunction(h => {
  const f = window.rideFrames.at(-1)
  return f.h === h && !f.riding
}, height)
const finish = page => page.evaluate(() => {
  const frames = window.rideFrames
  window.rideFrames = null
  return frames
})
// Time is the x coordinate of cubic-bezier(.2, 0, 0, 1). Solve x before reading y.
function curve(elapsed) {
  const x = Math.min(1, Math.max(0, elapsed / 285))
  if (x === 0 || x === 1) return x
  let low = 0, high = 1
  for (let i = 0; i < 16; i++) {
    const u = (low + high) / 2
    if (0.6 * (1 - u) ** 2 * u + u ** 3 < x) low = u
    else high = u
  }
  const u = (low + high) / 2
  return u ** 2 * (3 - 2 * u)
}

const rideStart = frames => frames.find(f => f.start !== null)?.start ?? frames[0].frameTime
const rideBottom = (from, to, elapsed) => from + (to - from) * curve(elapsed)
function trackRide(frames, from, to) {
  const start = rideStart(frames)
  const direction = Math.sign(to - from)
  let previous = from, maxError = 0, backwards = 0
  for (const f of frames) {
    // WAAPI samples on the frame clock even when this callback runs later.
    maxError = Math.max(maxError, Math.abs(f.bottom - rideBottom(from, to, f.frameTime - start)))
    backwards = Math.max(backwards, (previous - f.bottom) * direction)
    previous = f.bottom
  }
  const last = frames.at(-1), elapsed = last.t - (frames[0].resizedAt ?? frames[0].t)
  return {
    ok: maxError <= 20 && backwards <= 2 && elapsed <= 400 && Math.abs(last.bottom - to) < 1,
    maxError, backwards, elapsed, frames: frames.length,
  }
}

async function functional(page) {
  const field = page.locator('.chat-page .composer textarea')
  const idle = await read(page)
  await record(page)
  await field.tap()
  const tapped = await read(page)
  const tapping = [...await finish(page), tapped]
  check('1. tapping keeps the tabs and the old composer bottom', tapping.every(f => !f.keyboard && f.display !== 'none' && Math.abs(f.bottom - idle.bottom) < 1), { idle: idle.bottom, focused: tapped.bottom })

  // Measure the gap after the focus morph and its scroll adjustment. The resize below tests the keyboard ride.
  await page.waitForFunction(() => {
    const composer = document.querySelector('.chat-page .composer')
    const scroller = document.querySelector('.chat-page .chat-scroll')
    return !composer.hasAttribute('data-morph') && scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 2
  })
  const focused = await read(page)
  await record(page)
  await page.setViewportSize(keyboard)
  await settle(page, keyboard.height)
  const opening = (await finish(page)).filter(f => f.h === keyboard.height)
  const openBottom = keyboard.height - 14
  const openingTrack = trackRide(opening, focused.bottom, openBottom)
  check('2. the keyboard hides the tabs and the composer rides upward', openingTrack.ok && opening.every(f => f.keyboard && f.display === 'none'),
    { ...openingTrack, rise: focused.bottom - opening.at(-1).bottom, firstStep: opening[0].bottom - focused.bottom })
  // Allow the chat's normal padding and the last message's margin. The gap must stay constant.
  const gap = Math.max(...opening.map(f => Math.abs(f.top - f.message - (focused.top - focused.message))))
  check('3. the last message stays attached to the composer', opening.every(f => f.message <= f.top + 2) && gap <= 2, { maxGapChange: gap })
  await page.screenshot({ path: path.join(SHOTS, 'keyboard-open.png') })

  // A tap outside blurs first; Chrome grows the page a moment later. Until then nothing may move.
  const open = await read(page)
  await record(page)
  await field.evaluate(el => el.blur())
  await page.waitForFunction(() => window.rideFrames.length >= 4)
  await page.setViewportSize(phone)
  await settle(page, phone.height)
  const blurring = await finish(page)
  const waiting = blurring.filter(f => f.h === keyboard.height), growing = blurring.filter(f => f.h === phone.height)
  check('3b. a blur before the resize keeps the composer still, then it rides down', waiting.length >= 3 && waiting.every(f => f.keyboard && f.display === 'none' && Math.abs(f.bottom - open.bottom) < 1)
    && growing.every((f, i) => i === 0 || f.bottom >= growing[i - 1].bottom - 2) && growing.at(-1).display !== 'none',
    { waiting: waiting.length, moved: Math.max(...waiting.map(f => Math.abs(f.bottom - open.bottom))) })
  await field.focus()
  // A blur that leaves the page small (split screen, no keyboard) gives the tab bar back after a moment.
  await page.setViewportSize(keyboard)
  await page.waitForFunction(() => document.documentElement.hasAttribute('data-keyboard'))
  await field.evaluate(el => el.blur())
  const cleared = await page.waitForFunction(() => !document.documentElement.hasAttribute('data-keyboard') && getComputedStyle(document.querySelector('.phone-tabbar')).display !== 'none', null, { timeout: 2000 }).then(() => true, () => false)
  check('3c. a blur that leaves the page small gives the tab bar back', cleared)
  await page.setViewportSize(phone)
  await page.waitForFunction(h => innerHeight === h && !document.querySelector('main.app-shell').getAnimations().length, phone.height)
  await field.focus()
  await record(page)
  await page.setViewportSize(keyboard)
  await waitResize(page, keyboard.height)
  await page.waitForFunction(h => {
    const from = window.rideFrames.find(f => f.h === h)
    return window.rideFrames.at(-1).t - from.t >= 90
  }, keyboard.height)
  await page.setViewportSize(phone)
  await settle(page, phone.height)
  const interrupt = await finish(page)
  const reversal = interrupt.findIndex((f, i) => f.h === phone.height && interrupt[i - 1]?.h === keyboard.height)
  const interrupted = interrupt.slice(0, reversal).filter(f => f.h === keyboard.height), closing = interrupt.slice(reversal)
  const before = interrupted.at(-1)
  const interruptedAt = closing[0].frameTime - rideStart(interrupted)
  const closeFrom = rideBottom(idle.bottom, openBottom, interruptedAt)
  const closingTrack = trackRide(closing, closeFrom, idle.bottom)
  check('4. closing mid-ride reverses from the on-screen position', before.riding && closingTrack.ok && closing.every(f => !f.keyboard && f.display !== 'none'),
    { ...closingTrack, interruptedAt, reversalStep: closing[0].bottom - before.bottom, ridingBeforeClose: before.riding })
  await page.screenshot({ path: path.join(SHOTS, 'keyboard-closed.png') })

  await field.evaluate(el => el.blur())
  await page.locator('.chat-page .chat-scroll').evaluate(el => { el.scrollTop = 200 })
  await page.getByRole('button', { name: 'Jump to latest message' }).waitFor()
  const reading = await read(page)
  await record(page)
  await page.setViewportSize(keyboard)
  await settle(page, keyboard.height)
  const split = (await finish(page)).filter(f => f.h === keyboard.height)
  const scrollDrift = Math.max(...split.map(f => Math.abs(f.scrollTop - reading.scrollTop)))
  check('5. a height drop without editable focus keeps the tabs and preserves manual scroll', split.every(f => !f.keyboard && f.display !== 'none') && scrollDrift <= 2, { scrollDrift })
  await page.setViewportSize(phone)
  await field.focus()
  await record(page)
  const rotated = { width: 740, height: 360 }
  await page.setViewportSize(rotated)
  await settle(page, rotated.height)
  const rotation = (await finish(page)).filter(f => f.h === rotated.height)
  check('6. a new width establishes its own baseline', rotation.every(f => !f.keyboard && f.display !== 'none'))
  // Rotate with the keyboard open: the new width has no baseline yet, so the screen height stands in.
  await page.setViewportSize(phone)
  await page.waitForFunction(h => innerHeight === h && !document.querySelector('main.app-shell').getAnimations().length, phone.height)
  await field.focus()
  await page.setViewportSize(keyboard)
  await page.waitForFunction(() => document.documentElement.hasAttribute('data-keyboard'))
  // Playwright makes the screen as small as the viewport; a phone in landscape keeps its full screen.
  await page.evaluate(() => Object.defineProperty(screen, 'availHeight', { get: () => 390 }))
  const turn = async height => {
    const previous = (await read(page)).resizedAt
    await page.setViewportSize({ width: 700, height })
    // The dimensions can change before the app's resize handler updates the keyboard state.
    await page.waitForFunction(({ height, previous }) =>
      innerWidth === 700 && innerHeight === height && window.readRide().resizedAt > previous, { height, previous })
  }
  await turn(200)
  const turnedOpen = await read(page), availHeight = await page.evaluate(() => screen.availHeight)
  await turn(360)
  const turnedClosed = await read(page)
  // Only measured heights become the baseline: 225 is a keyboard against 390 (the screen), not against 360 (measured).
  await turn(225)
  const measured = !(await read(page)).keyboard
  check('6b. rotating with the keyboard open keeps it open, and closing it gives the tabs back', turnedOpen.keyboard && turnedOpen.display === 'none' && !turnedClosed.keyboard && turnedClosed.display !== 'none' && measured,
    { availHeight, measured, open: turnedOpen.keyboard, closed: turnedClosed.keyboard })
}

async function main() {
  let browser, perfBrowser
  const server = http.createServer((req, res) => { res.writeHead(404); res.end() })
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser()
    const touch = { viewport: phone, hasTouch: true, isMobile: true }
    for (const [scenario, options, count] of [
      ['functional', touch, 24],
      ['reduced', { ...touch, reducedMotion: 'reduce' }, 24],
      ['fine', { viewport: phone }, 24],
      ['performance', touch, 160],
    ]) {
      // The shared Tether proxy does not allow raw CDP sessions for CPU throttling.
      const runner = scenario === 'performance' ? (perfBrowser = await getBrowser({ local: true })) : browser
      const context = await runner.newContext({ ...options, serviceWorkers: 'block' })
      try {
        const page = await open(context, origin, count)
        const field = page.locator('.chat-page .composer textarea')
        if (scenario === 'functional') await functional(page)
        if (scenario === 'reduced') {
          await field.tap()
          await record(page)
          await page.setViewportSize(keyboard)
          await waitResize(page, keyboard.height)
          const first = (await finish(page)).find(f => f.h === keyboard.height)
          check('7. reduced motion places the composer on the first frame', first.keyboard && !first.riding && Math.abs(first.bottom - (keyboard.height - 14)) < 1, first)
        }
        if (scenario === 'fine') {
          await field.focus()
          const frame = await read(page)
          check('8. a narrow mouse viewport hides the tabs on focus', await page.evaluate(() => matchMedia('(pointer: fine)').matches) && !frame.keyboard && frame.display === 'none', frame)
        }
        if (scenario === 'performance') {
          await field.tap()
          await page.waitForFunction(() => !document.querySelector('.chat-page .composer').hasAttribute('data-morph'))
          const cdp = await context.newCDPSession(page)
          await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 })
          // Measure frame delivery without forcing per-frame layout in the recorder.
          await page.evaluate(() => {
            window.rideIntervals = new Promise(resolve => {
              let resized, previous
              const intervals = []
              addEventListener('resize', () => { resized = performance.now() }, { once: true })
              const tick = t => {
                if (resized !== undefined && previous !== undefined) intervals.push(t - previous)
                previous = t
                if (resized !== undefined && t - resized >= 285) resolve(intervals)
                else requestAnimationFrame(tick)
              }
              requestAnimationFrame(tick)
            })
          })
          await page.setViewportSize(keyboard)
          const intervals = await page.evaluate(() => window.rideIntervals)
          console.log(`performance: ${count} messages, CPU 4x, ${intervals.length} intervals, max ${Math.max(...intervals).toFixed(1)} ms, ${intervals.filter(ms => ms > 33).length} > 33 ms`)
          await cdp.detach()
        }
      } catch (error) { check(scenario, false, error.stack) } finally { await context.close() }
    }
  } catch (error) { check('harness', false, error.stack) } finally {
    await browser?.close().catch(() => {})
    await perfBrowser?.close().catch(() => {})
    server.close()
  }
  console.log(failed ? `keyboard ride: ${failed} failed` : 'keyboard ride: all checks pass')
  process.exitCode = failed ? 1 : 0
}

main()
