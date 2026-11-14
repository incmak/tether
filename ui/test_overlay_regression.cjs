// Run against an existing build. Every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-11/overlay-regression/shots')
const floating = '.status-stack, .attach-menu, .mode-menu, .notice-stack, .jump-button, .composer, .live-chips, .prompt-suggestion, .agent-bubble, .agent-pop'
const states = ['heads-open', 'heads-tasks', 'approval', 'queue', 'attach-sheet', 'mode-menu', 'jump', 'toast']
const sizes = [[390, 844], [820, 1180], [1280, 900]]
const detail = Array.from({ length: 18 }, (_, i) => `- Check ${i + 1}: Review the overlay text and each available option before the next step. Keep this card readable while its long details scroll beneath the pinned chip row.`)
const messages = Array.from({ length: 60 }, (_, i) => ({
  id: `message-${i}`, role: i % 2 ? 'assistant' : 'user',
  text: `History item ${i + 1}. A fixture conversation with enough text to scroll.\n\nReview the interface and record the result.`,
  time: '2026-11-11T08:00:00Z',
}))

function fixtures(state) {
  const approval = state === 'approval' ? {
    id: 'overlay-approval', actionable: true,
    text: detail.join('\n\n'),
    options: [1, 2, 3, 4].map(id => ({ id, text: `Permission option ${id}` })),
  } : null
  const pane = { id: '%1', tmux_session: '$1', label: 'Overlay test', agent: 'claude',
    dir: '/srv/project', state: approval ? 'Approval' : state === 'queue' ? 'Working' : 'Idle',
    writable: true, thread: 'overlay-thread', approval }
  return {
    sessions: { sessions: [pane], default_thread: pane.thread },
    chat: { messages, busy: state === 'queue', thread: pane.thread, agent: pane.agent,
      writable: true, session: pane.id, earlier: 0, limit: 100, state: pane.state, approval },
    footer: { observed: true, model: 'Claude', mode: 'default',
      suggestion: 'Review the next step',
      heads_up: { text: 'Heads up · Overlay review', state: 'explained', detail,
        options: [{ key: '1', label: 'Understood' }, { key: '0', label: 'Dismiss' }] },
      ...(state === 'heads-tasks' ? { goal: 'Review overlays', tasks: ['1 shell', '1 monitor'] } : {}),
      notices: state === 'toast' ? ['Fixture notice: Review the overlay result.'] : [],
    },
    prefs: { prefs: {}, version: 1 },
    'projects/marks': {},
    'projects/backlog': { text: '' },
    'side-shells': { shells: [] },
    update: { current: '0.0.0', latest: '0.0.0', available: false },
    host: { name: 'Test server', office: false },
    catalog: { commands: [], plugins: [], mcp: [], note: '' },
    stream: {},
    'codex-runs': { pane: '%1', running: 0, runs: [] }, 'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
    'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
    'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
    usage: { claude: null, codex: null },
    subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  }
}

// Runs in the browser. Clipping removes text and controls outside scrollports,
// but deliberately does not remove content behind sticky siblings or popovers.
function inspectOverlays(selector) {
  const textErrors = [], controlErrors = []
  // An open menu (the + sheet and its click-catching backdrop, the Mode menu) covers the page on purpose, and the page
  // under it is inert until it closes. Only the menu's own text and controls must be on top.
  const menus = [...document.querySelectorAll('.attach-layer, .mode-menu')]
  const underMenu = (el, top) => !!top && menus.some(menu => menu.contains(top) && !menu.contains(el))
  let textLines = 0, controls = 0
  const roots = [...document.querySelectorAll(selector)]
  const describe = el => {
    const parts = []
    for (let at = el; at && parts.length < 3; at = at.parentElement) {
      parts.push(at.tagName.toLowerCase() + [...at.classList].map(c => `.${c}`).join(''))
    }
    return parts.join(' < ')
  }
  const visible = el => {
    for (let at = el; at; at = at.parentElement) {
      const s = getComputedStyle(at)
      if (at.hidden || s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false
    }
    return true
  }
  const clip = (rect, el) => {
    let left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right)
    let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom)
    for (let at = el.parentElement; at; at = at.parentElement) {
      const s = getComputedStyle(at), r = at.getBoundingClientRect()
      if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) {
        left = Math.max(left, r.left + at.clientLeft)
        right = Math.min(right, r.left + at.clientLeft + at.clientWidth)
      }
      if (/(auto|scroll|hidden|clip)/.test(s.overflowY)) {
        top = Math.max(top, r.top + at.clientTop)
        bottom = Math.min(bottom, r.top + at.clientTop + at.clientHeight)
      }
    }
    return right > left && bottom > top ? { left, right, top, bottom } : null
  }
  const opaque = el => {
    // Computed colors can use rgba(r,g,b,a), rgb(r g b / a), or color(srgb ... / a).
    // Images alone do not qualify, including gradients with transparent stops.
    const color = getComputedStyle(el).backgroundColor
    if (color === 'transparent') return false
    const alpha = color.includes('/') ? color.split('/')[1].replace(')', '').trim()
      : /^rgba\(/.test(color) ? color.slice(5, -1).split(',')[3]?.trim() : '1'
    return (alpha?.endsWith('%') ? parseFloat(alpha) / 100 : Number(alpha)) >= 0.95
  }
  const nodes = new Set()
  for (const root of roots) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    while (walker.nextNode()) nodes.add(walker.currentNode)
  }
  for (const node of nodes) {
    const parent = node.parentElement
    if (!node.textContent.trim() || !visible(parent)) continue
    const range = document.createRange()
    range.selectNodeContents(node)
    for (const rect of range.getClientRects()) {
      if (rect.width <= 2 || rect.height <= 2) continue
      const r = clip(rect, parent), x = (rect.left + rect.right) / 2, y = (rect.top + rect.bottom) / 2
      if (!r || x < r.left || x > r.right || y < r.top || y > r.bottom) continue
      textLines++
      const top = document.elementFromPoint(x, y)
      if (top && (top === parent || top.contains(parent) || parent.contains(top))) continue
      if (underMenu(parent, top)) continue
      let common = parent
      while (common && !common.contains(top)) common = common.parentElement
      let covered = false
      for (let at = top; at && at !== common; at = at.parentElement) {
        if (opaque(at)) { covered = true; break }
      }
      if (!covered) textErrors.push(`text "${node.textContent.trim().slice(0, 40)}" shows through ${top ? describe(top) : 'null'}`)
    }
  }
  const controlSet = new Set()
  const controlSelector = 'button, [role=menuitem], [role=separator][tabindex], a[href], textarea, input'
  for (const root of roots) {
    if (root.matches(controlSelector)) controlSet.add(root)
    for (const control of root.querySelectorAll(controlSelector)) controlSet.add(control)
  }
  for (const control of controlSet) {
    if (!visible(control)) continue // Disabled controls still count.
    const r = clip(control.getBoundingClientRect(), control)
    if (!r) continue
    controls++
    const top = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2)
    if (top !== control && !control.contains(top) && !underMenu(control, top)) {
      const label = control.getAttribute('aria-label') || control.textContent.trim() || control.tagName
      controlErrors.push(`control "${label.slice(0, 80)}" blocked by ${top ? describe(top) : 'null'}`)
    }
  }
  return { textErrors: [...new Set(textErrors)], controlErrors: [...new Set(controlErrors)],
    textLines, controls, overflow: document.documentElement.scrollWidth > innerWidth }
}

// Runs in the browser. Bubbles must stay clickable and clear of neighbouring controls.
function inspectBubbles() {
  const errors = []
  const visible = el => {
    for (let at = el; at; at = at.parentElement) {
      const s = getComputedStyle(at)
      if (at.hidden || s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false
    }
    return true
  }
  const describe = el => {
    const parts = []
    for (let at = el; at && parts.length < 3; at = at.parentElement) {
      parts.push(at.tagName.toLowerCase() + [...at.classList].map(c => `.${c}`).join(''))
    }
    return parts.join(' < ')
  }
  const bounds = r => `(${Math.round(r.left)}-${Math.round(r.right)}, ${Math.round(r.top)}-${Math.round(r.bottom)})`
  const targets = []
  for (const el of document.querySelectorAll('.status-stack, .composer')) {
    if (!visible(el)) continue
    const r = el.getBoundingClientRect()
    if (el.matches('.composer')) { targets.push({ name: 'composer', r }); continue }
    if (!el.querySelector(':scope > .status-content:not([hidden])')) continue
    targets.push({ name: 'status card', r })
    if (el.offsetWidth - el.clientWidth - el.clientLeft * 2 > 0) {
      targets.push({ name: 'status card scrollbar', r: {
        left: r.left + el.clientLeft + el.clientWidth, right: r.right - el.clientLeft,
        top: r.top, bottom: r.bottom,
      } })
    }
  }
  const menu = document.querySelector('.attach-layer:popover-open, .mode-menu')
  const bubbles = [...document.querySelectorAll('.agent-bubble')].filter(visible)
  for (const bubble of bubbles) {
    const label = bubble.getAttribute('aria-label'), r = bubble.getBoundingClientRect()
    if (!menu) {
      const top = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2)
      if (top !== bubble && !bubble.contains(top)) {
        errors.push(`bubble "${label}" hit test returns ${top ? describe(top) : 'null'}`)
      }
    }
    for (const target of targets) {
      const t = target.r
      if (Math.min(r.right, t.right) - Math.max(r.left, t.left) > 0.5 &&
          Math.min(r.bottom, t.bottom) - Math.max(r.top, t.top) > 0.5) {
        errors.push(`bubble "${label}" ${bounds(r)} overlaps ${target.name} ${bounds(t)}`)
      }
    }
  }
  return { bubbles: bubbles.length, errors }
}

async function main() {
  let browser, page, server, passed = 0, failed = 0, cases = 0
  const endpoints = new Set(), unexpected = new Set(), pageErrors = []
  const triggered = new Set(), bubbleCases = new Set()
  const check = (name, errors) => {
    if (errors.length) { failed++; console.log(`FAIL ${name}: ${errors.join('; ')}`) }
    else { passed++; console.log(`ok ${name}`) }
  }
  try {
    await fs.access(path.join(DIST, 'index.html'))
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      // This server has no proxy or API implementation, even if a browser route is missed.
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html')
        : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (req.method !== 'GET' || url.pathname.endsWith('/sw.js') || !url.pathname.startsWith('/chat/') || !file.startsWith(DIST + path.sep)) {
        res.writeHead(404); res.end(); return
      }
      try {
        const target = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : file
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(target)
        res.writeHead(200, { 'Content-Type': mime[path.extname(target)] || 'application/octet-stream', 'Cache-Control': 'no-store' })
        res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    let fixture = fixtures('heads-open')
    async function openPage() {
      const page = await context.newPage()
      page.setDefaultTimeout(10000)
      page.on('pageerror', error => pageErrors.push(error.message))
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
      await page.routeWebSocket('**/*', socket => socket.close())
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        // The random origin has no existing worker; never let this page install one.
        if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
        if (url.pathname.includes('/api/')) {
          const endpoint = url.pathname.split('/api/')[1]
          endpoints.add(`${request.method()} ${url.pathname}`)
          if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
          if (!(endpoint in fixture)) unexpected.add(endpoint)
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' }) // Use fixture polling, without SSE retries.
          return route.fulfill({ json: fixture[endpoint] || {} })
        }
        if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
        unexpected.add(`${url.origin}${url.pathname}`)
        return route.abort('blockedbyclient')
      })
      await page.addInitScript(() => {
        // Service-worker fetches bypass page.route. Keep all fetches in this page.
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
        const query = new URL(location.href).searchParams
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light',
          voiceOff: true, voiceSound: false, sidePins: [{ pane: '%1', note: 'Overlay fixture' }],
          look: { layout: 'classic', surface: query.get('look') === 'glass' ? 'glass' : 'flat', backdrop: 'ember' },
        }))
        if (query.get('state') === 'queue') localStorage.setItem('tether.pending', JSON.stringify({ '%1': [1, 2].map(id => ({
          id, text: `Queued fixture message ${id}`, key: `Queued fixture message ${id}`, seen: 0,
          at: Date.now(), thread: 'overlay-thread',
        })) }))
      })
      return page
    }
    for (const state of states) for (const [width, height] of sizes) for (const look of ['plain', 'glass']) {
      const name = `${state}-${width}-${look}`, errorStart = pageErrors.length
      fixture = fixtures(state)
      cases++
      try {
        // Each case gets a fresh page to contain stray history.back() from the previous case.
        if (page) await page.close().catch(() => {})
        page = await openPage()
        await page.setViewportSize({ width, height })
        await page.goto(`${origin}/chat/?session=%251&state=${state}&look=${look}`, { waitUntil: 'load' })
        await page.waitForSelector('.composer textarea:enabled')
        await page.waitForSelector('.heads-up-detail', { state: 'attached' })
        await page.waitForFunction(glass => (document.querySelector('.app-shell')?.dataset.surface === 'glass') === glass, look === 'glass')
        await page.waitForFunction(() => document.querySelector('.chat-scroll')?.scrollHeight > innerHeight)
        if (state === 'queue') {
          await page.getByRole('button', { name: '2 queued', exact: true }).click()
          await page.waitForFunction(() => [...document.querySelectorAll('.status-content:not([hidden]) .pending-message')].length === 2)
        } else if (state === 'attach-sheet') {
          await page.getByRole('button', { name: 'Add photos and files', exact: true }).click()
          await page.waitForSelector('.attach-layer:popover-open .attach-menu')
        } else if (state === 'mode-menu') {
          await page.getByRole('button', { name: /^Mode:/ }).click()
          await page.waitForSelector('.mode-menu')
        } else if (state === 'jump') {
          await page.locator('.chat-scroll').first().evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')) })
          await page.waitForSelector('.jump-button')
        } else if (state === 'toast') {
          await page.waitForSelector('.notice-stack')
        }
        if (['heads-open', 'heads-tasks', 'approval'].includes(state)) {
          await page.waitForFunction(() => { const el = document.querySelector('.status-stack'); return el.scrollHeight > el.clientHeight })
          await page.locator('.status-stack').evaluate(el => { el.scrollTop = (el.scrollHeight - el.clientHeight) / 2 })
          await page.waitForFunction(() => { const el = document.querySelector('.status-stack'); return Math.abs(el.scrollTop - (el.scrollHeight - el.clientHeight) / 2) <= 1 && el.scrollTop > 0 })
        }
        if (state === 'approval') await page.waitForSelector('.status-chip[aria-expanded=true][aria-controls$="-action"]')
        await page.waitForSelector('.agent-bubble:not(.side-pin-bubble)')
        await page.waitForSelector('.side-pin-bubble')
        bubbleCases.add(name)
        await page.evaluate(() => document.fonts.ready)
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
        await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
        triggered.add(state)
        const result = await page.evaluate(inspectOverlays, floating)
        check(`${name} A text (${result.textLines} lines)`, result.textLines ? result.textErrors : ['No visible floating text'])
        check(`${name} B controls (${result.controls})`, result.controls ? result.controlErrors : ['No visible floating controls'])
        check(`${name} C page overflow`, result.overflow ? ['documentElement.scrollWidth exceeds innerWidth'] : [])
        const { bubbles: n, errors } = await page.evaluate(inspectBubbles)
        check(`${name} D bubbles (${n})`, n ? errors : ['No visible bubbles'])
        if (pageErrors.length > errorStart) check(`${name} runtime`, pageErrors.slice(errorStart))
      } catch (error) {
        await page.screenshot({ path: path.join(SHOTS, `${name}.png`) }).catch(() => {})
        check(`${name} setup`, [error.message.split('\n')[0]])
      }
    }
    check('fixture coverage', [...unexpected].map(endpoint => `Unmocked request: ${endpoint}`))
    console.log(`Fixture endpoints: ${[...endpoints].sort().join(', ')}`)
    const missing = states.filter(state => !triggered.has(state))
    console.log(`States not triggered: ${missing.length ? missing.join(', ') + ' (setup failed; see setup checks)' : 'none'}. Right-edge bubbles: agent and side-pin triggered in ${bubbleCases.size}/${cases} cases with both bubbles.`)
  } catch (error) { check('runner', [error.message]) }
  finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {}) // Disconnect CDP; never close the shared context.
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`overlay regression: ${failed ? 'FAIL' : 'PASS'} (${cases} cases, ${passed} checks passed, ${failed} checks failed)`)
    if (failed) process.exitCode = 1
  }
}

main().catch(error => { console.error(`FAIL overlay regression: ${error.message}`); process.exitCode = 1 })
