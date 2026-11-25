// One viewport resize stands in for the Android IME. All API calls use fixtures.
const http = require('node:http'), fs = require('node:fs/promises'), path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || '/tmp'
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }
const phone = { width: 390, height: 844 }
const keyboard = { ...phone, height: 544 }
const pane = { id: '%1', tmux_session: '$1', label: 'project:0.1', pane_name: 'Keyboard state', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1' }
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
    const field = box.querySelector('textarea')
    window.readKeyboard = () => {
      const rect = box.getBoundingClientRect()
      return { width: innerWidth, height: innerHeight, bottom: rect.bottom, top: rect.top,
        message: message.getBoundingClientRect().bottom, scrollTop: viewport.scrollTop,
        display: getComputedStyle(bar).display, keyboard: document.documentElement.hasAttribute('data-keyboard'),
        focused: document.activeElement === field, animations: shell.getAnimations().length }
    }
    // The chat pins scroll in its ResizeObserver after rAF and before paint. Read the gap from that same frame.
    const afterScroll = new ResizeObserver(() => {
      const frame = window.keyboardFrames?.at(-1)
      if (!frame || frame.time !== document.timeline.currentTime) return
      frame.message = message.getBoundingClientRect().bottom
      frame.scrollTop = viewport.scrollTop
    })
    afterScroll.observe(viewport)
    window.recordKeyboardFrames = () => {
      const frames = window.keyboardFrames = []
      const tick = time => {
        if (window.keyboardFrames !== frames) return
        frames.push({ ...window.readKeyboard(), time })
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    }
  })
  return page
}

const read = page => page.evaluate(() => window.readKeyboard())
const record = page => page.evaluate(() => window.recordKeyboardFrames())
const waitResize = (page, { width, height }) => page.waitForFunction(({ width, height }) =>
  window.keyboardFrames.some(f => f.width === width && f.height === height), { width, height })
const finish = page => page.evaluate(() => {
  const frames = window.keyboardFrames
  window.keyboardFrames = null
  return frames
})
const resize = async (page, viewport) => {
  await record(page)
  await page.setViewportSize(viewport)
  await waitResize(page, viewport)
  return (await finish(page)).find(f => f.width === viewport.width && f.height === viewport.height)
}
const waitForMorph = page => page.waitForFunction(() => !document.querySelector('.chat-page .composer').hasAttribute('data-morph'))

async function functional(page) {
  const field = page.locator('.chat-page .composer textarea')
  const idle = await read(page)
  await record(page)
  await field.tap()
  const tapped = await read(page)
  const tapping = [...await finish(page), tapped]
  check('1. tapping keeps the tabs and the old composer bottom', tapping.every(f => !f.keyboard && f.display !== 'none' && Math.abs(f.bottom - idle.bottom) < 1), { idle: idle.bottom, focused: tapped.bottom })

  // Sample the gap after the focus morph and its scroll adjustment.
  await page.waitForFunction(() => {
    const composer = document.querySelector('.chat-page .composer')
    const scroller = document.querySelector('.chat-page .chat-scroll')
    return !composer.hasAttribute('data-morph') && scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 2
  })
  const focused = await read(page)
  const first = await resize(page, keyboard), openBottom = keyboard.height - 14
  check('2. the first resized frame places the composer at its final bottom, hides the tabs and has no shell animation',
    Math.abs(first.bottom - openBottom) <= 1 && first.keyboard && first.display === 'none' && first.animations === 0,
    { actual: first.bottom, expected: openBottom, tabs: first.display, keyboard: first.keyboard, animations: first.animations })
  const gap = first.top - first.message, gapChange = Math.abs(gap - (focused.top - focused.message))
  check('3. the last message stays attached on that same frame', gap >= -2 && gapChange <= 2, { gap, gapChange })
  await page.screenshot({ path: path.join(SHOTS, 'keyboard-open.png') })

  // A tap outside blurs first. The composer bottom and tabs must wait for Chrome to grow the page.
  const open = await read(page)
  await record(page)
  await field.evaluate(el => el.blur())
  await page.waitForFunction(() => window.keyboardFrames.some(f => !f.focused))
  await page.setViewportSize(phone)
  await waitResize(page, phone)
  const blurring = await finish(page)
  const waiting = blurring.filter(f => f.height === keyboard.height && !f.focused), grown = blurring.find(f => f.height === phone.height)
  const moved = Math.max(...waiting.map(f => Math.abs(f.bottom - open.bottom)))
  check('3b. blur keeps the composer bottom still until resize, then puts it back with the tabs',
    moved <= 1 && waiting.every(f => f.keyboard && f.display === 'none') && !grown.keyboard && grown.display !== 'none' && Math.abs(grown.bottom - idle.bottom) <= 1,
    { waiting: waiting.length, moved, bottom: grown.bottom, expected: idle.bottom, tabs: grown.display })
  await waitForMorph(page)
  await page.screenshot({ path: path.join(SHOTS, 'keyboard-closed.png') })

  await field.focus()
  // A blur that leaves the page small (split screen, no keyboard) gives the tab bar back after a moment.
  await page.setViewportSize(keyboard)
  await page.waitForFunction(() => document.documentElement.hasAttribute('data-keyboard'))
  await field.evaluate(el => el.blur())
  const cleared = await page.waitForFunction(() => !document.documentElement.hasAttribute('data-keyboard') && getComputedStyle(document.querySelector('.phone-tabbar')).display !== 'none', null, { timeout: 2000 }).then(() => true, () => false)
  check('3c. a blur that leaves the page small gives the tab bar back', cleared)
  await record(page)
  await field.focus()
  await page.waitForFunction(() => window.keyboardFrames.some(f => f.focused))
  const refocusing = await finish(page)
  check('3d. refocusing after a blur at the split height keeps the keyboard closed', refocusing.every(f => !f.keyboard),
    { frames: refocusing.length, keyboardFrames: refocusing.filter(f => f.keyboard).length })
  await resize(page, phone)
  await field.evaluate(el => el.blur())
  await waitForMorph(page)
  await page.locator('.chat-page .chat-scroll').evaluate(el => { el.scrollTop = 200 })
  await page.getByRole('button', { name: 'Jump to latest message' }).waitFor()
  const reading = await read(page)
  const split = await resize(page, keyboard)
  const scrollDrift = Math.abs(split.scrollTop - reading.scrollTop)
  check('5. a height drop without editable focus keeps the tabs and preserves manual scroll', !split.keyboard && split.display !== 'none' && scrollDrift <= 2, { scrollDrift })
  await record(page)
  await field.tap()
  const splitTapped = await read(page)
  await page.waitForFunction(() => window.keyboardFrames.some(f => f.focused))
  const splitTapping = [...await finish(page), splitTapped]
  check('5b. tapping the split composer keeps the keyboard closed, tabs visible and bottom still',
    splitTapping.every(f => !f.keyboard && f.display !== 'none' && Math.abs(f.bottom - split.bottom) <= 1),
    { expectedBottom: split.bottom, focusedBottom: splitTapped.bottom, keyboardFrames: splitTapping.filter(f => f.keyboard).length })
  const splitOpen = await resize(page, { ...keyboard, height: 294 })
  check('5c. a keyboard-size drop from the focused split opens the keyboard and hides the tabs',
    splitTapped.focused && !splitTapped.keyboard && splitOpen.focused && splitOpen.keyboard && splitOpen.display === 'none',
    { before: splitTapped.keyboard, after: splitOpen.keyboard, tabs: splitOpen.display })
  await resize(page, keyboard)
  await field.evaluate(el => el.blur())
  await resize(page, phone)
  await field.focus()
  const rotation = await resize(page, { width: 740, height: 360 })
  check('6. a new width establishes its own baseline', !rotation.keyboard && rotation.display !== 'none')

  // Rotate with the keyboard open. Playwright shrinks the screen with the viewport, so give the new width a full screen height.
  await resize(page, phone)
  await field.focus()
  await resize(page, keyboard)
  await page.evaluate(() => Object.defineProperty(screen, 'availHeight', { get: () => 390 }))
  const turn = height => resize(page, { width: 700, height })
  const turnedOpen = await turn(200)
  const turnedClosed = await turn(360)
  // Only measured heights become the baseline: 225 is a keyboard against 390 (the screen), but not against 360 (measured).
  const measured = !(await turn(225)).keyboard
  check('6b. rotating with the keyboard open keeps it open, and closing it gives the tabs back', turnedOpen.keyboard && turnedOpen.display === 'none' && !turnedClosed.keyboard && turnedClosed.display !== 'none' && measured,
    { measured, open: turnedOpen.keyboard, closed: turnedClosed.keyboard })
}

async function performanceRun(page, context, mode, count) {
  const field = page.locator('.chat-page .composer textarea')
  if (mode === 'resize') {
    await field.tap()
    await waitForMorph(page)
  }
  const cdp = await context.newCDPSession(page)
  try {
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 })
    // Record delivery without forcing layout on every frame.
    await page.evaluate(mode => new Promise(ready => {
      window.keyboardIntervals = new Promise(resolve => {
        let start, end, previous
        const intervals = []
        if (mode === 'resize') addEventListener('resize', () => { start = performance.now() }, { once: true })
        else {
          const box = document.querySelector('.chat-page .composer')
          box.addEventListener('focusin', () => { start = performance.now() }, { once: true })
          const observer = new MutationObserver(() => {
            if (start === undefined || box.hasAttribute('data-morph')) return
            end = performance.now()
            observer.disconnect()
          })
          observer.observe(box, { attributes: true, attributeFilter: ['data-morph'] })
        }
        const tick = time => {
          if (previous === undefined) ready()
          if (start !== undefined && previous !== undefined) intervals.push(time - previous)
          previous = time
          if (mode === 'resize' ? start !== undefined && time - start >= 1000 : end !== undefined && time >= end) resolve(intervals)
          else requestAnimationFrame(tick)
        }
        requestAnimationFrame(tick)
      })
    }), mode)
    if (mode === 'resize') await page.setViewportSize(keyboard)
    else await field.tap()
    await page.waitForFunction(() => window.keyboardIntervals.then(intervals => intervals.length > 0))
    const intervals = await page.evaluate(() => window.keyboardIntervals)
    console.log(`${mode}: ${count} messages, CPU 4x, ${intervals.length} intervals, max ${Math.max(...intervals).toFixed(1)} ms, ${intervals.filter(ms => ms > 33).length} > 33 ms`)
    console.log('intervals (ms):', intervals.map(ms => ms.toFixed(1)).join(', '))
  } finally {
    await cdp.detach()
  }
}

async function main() {
  let browser
  const perf = process.argv[2]
  const server = http.createServer((req, res) => { res.writeHead(404); res.end() })
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    // CPU throttling needs a local CDP session.
    browser = await getBrowser({ local: !!perf })
    const touch = { viewport: phone, hasTouch: true, isMobile: true }
    const scenarios = perf ? [[perf, touch, 160]] : [['functional', touch, 24], ['fine', { viewport: phone }, 24]]
    for (const [scenario, options, count] of scenarios) {
      const context = await browser.newContext({ ...options, serviceWorkers: 'block' })
      try {
        const page = await open(context, origin, count)
        if (scenario === 'functional') await functional(page)
        else if (scenario === 'fine') {
          await page.locator('.chat-page .composer textarea').focus()
          const frame = await read(page)
          check('8. a narrow mouse viewport hides the tabs on focus', await page.evaluate(() => matchMedia('(pointer: fine)').matches) && !frame.keyboard && frame.display === 'none', frame)
        } else if (scenario === 'resize' || scenario === 'morph') await performanceRun(page, context, scenario, count)
        else throw new Error('Expected resize or morph for a performance run')
      } catch (error) { check(scenario, false, error.stack) } finally { await context.close() }
    }
  } catch (error) { check('harness', false, error.stack) } finally {
    await browser?.close().catch(() => {})
    server.close()
  }
  if (!perf || failed) console.log(failed ? `keyboard state: ${failed} failed` : 'keyboard state: all checks pass')
  process.exitCode = failed ? 1 : 0
}

main()
