#!/usr/bin/env node
// Boot a test Tether (server/dev/verify_server.py: the real server over fixture data, never the live one), open
// named surfaces of the built UI at 320 px and 1280 px, check each, and save the screenshots for the Verified stamp.
//   node ui/tools/verify.cjs --surface settings,backlog      built-in surfaces: see --list
//   node ui/tools/verify.cjs --recipe my_surface.cjs         your own surface: see .claude/skills/verify/SKILL.md
// Options: --dist DIR (a build to reuse; default: build the current sources into a temporary folder), --name LABEL
// (shots folder label; default: the branch), --shots NEW_DIR, --widths 320,1280, --local (own headless Chromium).
const fs = require('node:fs')
const http = require('node:http')
const { createHash } = require('node:crypto')
const os = require('node:os')
const path = require('node:path')
const { spawn, execFileSync } = require('node:child_process')
const { getBrowser } = require('../browser_test_helper.cjs')

const UI = path.resolve(__dirname, '..'), REPO = path.resolve(UI, '..')
const HEIGHTS = { 320: 640, 1280: 800 }
// The test Tether starts no browser (it would be a second Chromium or the live container), so these two say so.
const EXPECTED = { 'browser/agents': 503, 'browser/signed/access': 503 }
const API = '/mobile/api/'
const REDIRECTS = new Set([301, 302, 303, 307, 308])

// A surface: `open` brings it on screen from the loaded chat page, `landmark` is a selector that only this surface
// shows. A shot counts only when the landmark is visible and exposed. ctx: { width, phone, pane, thread, url }.
const SURFACES = {
  chat: {
    about: 'The fixture chat and the composer',
    open: async () => {},
    landmark: '.app-shell .composer-area form.composer',
    ready: async page => { await page.locator('.chat-page').getByText('The mock conversation stays with this test.').first().waitFor() },
  },
  'chat-advisor': {
    about: 'An expanded advisor answer and an advisor error',
    open: async page => {
      const rows = page.locator('.chat-page .advisor-call')
      await rows.filter({ hasText: '1m 15s · Fable 5.2' }).locator('summary').press('Enter')
      await rows.filter({ has: page.locator('.tool-error') }).scrollIntoViewIfNeeded()
    },
    landmark: '.chat-page .advisor-call[open] .advisor-call-body',
  },
  'chat-review': {
    about: 'A review request card, opened: the commit, the reply format and the rules',
    open: async page => {
      const card = page.locator('.chat-page details.review-message').first()
      await card.locator('summary').press('Enter')
      await card.locator('.review-meta code').first().scrollIntoViewIfNeeded()
    },
    landmark: '.chat-page details.review-message[open] .review-meta code >> nth=0',
  },
  settings: {
    about: 'The Settings dialog',
    open: async (page, ctx) => {
      if (ctx.phone) await openPhoneMenu(page)
      await page.getByRole('button', { name: 'Settings', exact: true }).first().click()
    },
    landmark: 'dialog.preferences[open] #settings-title',
  },
  backlog: {
    about: 'The backlog, with the fixture items',
    open: async (page, ctx) => {
      if (ctx.phone) await openPhoneMenu(page)
      await page.getByRole('button', { name: /^Backlog/ }).first().click()
    },
    landmark: 'dialog.backlog-sheet[open] .backlog-text-line:text-is("Fix the flaky share test")',
  },
}

async function openPhoneMenu(page) {
  const settings = page.getByRole('button', { name: 'Settings', exact: true }).first()
  if (await settings.isVisible().catch(() => false)) return
  await page.getByRole('button', { name: /^(Open sessions(?:\.|$)|(?:Sessions|Menu|Open menu|Home)$)/ }).first().click()
}

function parseArgs(argv) {
  const args = { surface: [], recipe: [], widths: [320, 1280] }
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i], value = () => { if (i + 1 >= argv.length) throw new Error(`${flag} needs a value`); return argv[++i] }
    if (flag === '--surface') args.surface.push(...value().split(',').filter(Boolean))
    else if (flag === '--recipe') args.recipe.push(value())
    else if (flag === '--dist') args.dist = path.resolve(value())
    else if (flag === '--shots') args.shots = path.resolve(value())
    else if (flag === '--name') args.name = value()
    else if (flag === '--widths') args.widths = value().split(',').map(Number)
    else if (flag === '--local') args.local = true
    else if (flag === '--list') args.list = true
    else if (flag === '--help' || flag === '-h') args.help = true
    else throw new Error(`Unknown option ${flag}. Run with --help.`)
  }
  if (args.name !== undefined && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(args.name)) throw new Error('--name: use letters, digits, dot, dash or underscore')
  if (!args.widths.length || args.widths.some(width => !Number.isInteger(width) || width < 320 || width > 3840)) throw new Error('--widths: whole numbers from 320 to 3840')
  args.widths = [...new Set(args.widths)]
  return args
}

function loadSurfaces(args) {
  const surfaces = { ...SURFACES }, chosen = [...args.surface]
  for (const file of args.recipe) {
    const loaded = require(path.resolve(file))
    for (const [name, surface] of Object.entries(loaded)) {
      if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name)) throw new Error(`${file}: surface name "${name}" must be lower-case letters, digits and dashes`)
      if (typeof surface?.open !== 'function' || typeof surface.landmark !== 'string' || !surface.landmark) throw new Error(`${file}: surface "${name}" needs open(page, ctx) and a landmark selector`)
      if (surface.expect !== undefined && (!surface.expect || typeof surface.expect !== 'object' || Array.isArray(surface.expect)
          || Object.entries(surface.expect).some(([key, status]) => !/^[a-z0-9-]+(?:\/[a-z0-9-]+)*$/.test(key) || !Number.isInteger(status) || status < 100 || status > 599))) {
        throw new Error(file + ': surface "' + name + '" needs expect entries like { "projects/backlog": 500 }')
      }
      surfaces[name] = surface
      chosen.push(name)
    }
  }
  const unknown = chosen.filter(name => !surfaces[name])
  if (unknown.length) throw new Error(`Unknown surface: ${unknown.join(', ')}. Built-in: ${Object.keys(SURFACES).join(', ')}. Add your own with --recipe.`)
  return { surfaces, chosen: [...new Set(chosen)] }
}

const git = (...args) => execFileSync('git', ['-C', REPO, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()

function folderName(label, now) {
  const two = value => String(value).padStart(2, '0')
  const day = [now.getFullYear(), two(now.getMonth() + 1), two(now.getDate())].join('-')
  const time = [now.getHours(), now.getMinutes(), now.getSeconds()].map(two).join('')
  return path.join('.release-work', day, 'verify', label + '-' + time)
}

function shotsFolder(args) {
  const branch = git('rev-parse', '--abbrev-ref', 'HEAD').replace(/[^A-Za-z0-9._-]+/g, '-')
  const label = args.name || (branch === 'HEAD' ? git('rev-parse', '--short', 'HEAD') : branch)
  const start = Date.now()
  for (let offset = 0; ; offset += 1000) {
    // Under the main checkout, so the shots outlive a worktree.
    const folder = args.shots || path.join(path.dirname(git('rev-parse', '--path-format=absolute', '--git-common-dir')), folderName(label, new Date(start + offset)))
    fs.mkdirSync(path.dirname(folder), { recursive: true })
    try {
      fs.mkdirSync(folder)
      return folder
    } catch (error) {
      if (args.shots || error.code !== 'EEXIST') throw error
    }
  }
}

function sourceTreeId(head, files, readFile) {
  const tree = createHash('sha256')
  for (const file of [...files].sort(Buffer.compare)) {
    const content = readFile(file)
    const hash = content === null ? 'missing' : createHash('sha256').update(content).digest('hex')
    tree.update(file).update('\0').update(hash).update('\0')
  }
  return head + '+' + tree.digest('hex')
}

function treeId() {
  const head = git('rev-parse', 'HEAD')
  const listed = execFileSync('git', ['-C', REPO, 'ls-files', '-co', '--exclude-standard', '-z', '--', 'ui', 'server'],
    { maxBuffer: 64 * 1024 * 1024 })
  const files = []
  for (let start = 0, end; (end = listed.indexOf(0, start)) !== -1; start = end + 1) {
    files.push(listed.subarray(start, end))
  }
  const prefix = Buffer.from(REPO + path.sep)
  return sourceTreeId(head, files, file => {
    try { return fs.readFileSync(Buffer.concat([prefix, file])) }
    catch (error) {
      // Git still lists a tracked file that is missing from the working tree.
      if (['ENOENT', 'ENOTDIR', 'EISDIR'].includes(error.code)) return null
      throw error
    }
  })
}

function stampReason(built, widths, sourcesChanged) {
  if (sourcesChanged) return 'sources changed during the run'
  if (!built) return 'used --dist'
  if (!widths.includes(320) || !widths.includes(1280)) return 'the run needs both 320 px and 1280 px'
  return ''
}

function bounded(promise, ms, message) {
  let timer
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms) }),
  ]).finally(() => clearTimeout(timer))
}

function unlessCancelled(promise, signal) {
  if (!signal) return promise
  let abort
  return Promise.race([promise, new Promise((_, reject) => {
    abort = () => reject(signal.reason)
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
  })]).finally(() => signal.removeEventListener('abort', abort))
}

function managedChild(command, args, options) {
  const child = spawn(command, args, options)
  let stderr = ''
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-8000) })
  child.stdin?.on('error', () => {})  // The child may already have closed its end.
  const done = new Promise(resolve => {
    child.once('error', error => resolve({ error }))
    child.once('close', (code, signal) => resolve({ code, signal }))
  })
  return { child, done, diagnostic: () => stderr.trim() }
}

async function build(temp, signal) {
  const dist = path.join(temp, 'dist')
  console.log('Build: current sources into a temporary folder')
  if (!fs.existsSync(path.join(UI, 'node_modules'))) throw new Error('ui/node_modules is missing. Run: cd ui && npm ci')
  const { child, done, diagnostic } = managedChild('npm', ['run', 'build', '--', '--outDir', dist, '--emptyOutDir'],
    { cwd: UI, detached: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let stdout = '', finished = false
  child.stdout.on('data', data => { stdout = (stdout + data).slice(-8000) })
  try {
    const result = await unlessCancelled(bounded(done, 600000, 'The UI build timed out.'), signal)
    finished = true
    if (result.code !== 0) throw new Error('The UI build failed:\n' +
      (result.error?.message || (stdout + diagnostic()).trim().split('\n').slice(-30).join('\n')))
    return dist
  } finally {
    if (!finished && child.pid) {
      const kill = name => { try { process.kill(-child.pid, name) } catch (error) { if (error.code !== 'ESRCH') throw error } }
      kill('SIGTERM')
      const timer = setTimeout(() => kill('SIGKILL'), 2000)
      try { await bounded(done, 4000, 'The build process did not close.') } finally { clearTimeout(timer) }
    }
  }
}

function startServer(dist) {
  const { child, done, diagnostic } = managedChild('python3',
    ['-u', path.join(REPO, 'server/dev/verify_server.py'), '--dist', dist, '--until-stdin-closes'],
    { stdio: ['pipe', 'pipe', 'pipe'] })
  const ready = new Promise((resolve, reject) => {
    let stdout = ''
    child.stdout.on('data', function read(data) {
      stdout += data
      if (!stdout.includes('\n')) return
      child.stdout.off('data', read)
      child.stdout.resume()
      try { resolve(JSON.parse(stdout.split('\n')[0])) } catch { reject(new Error('The test server sent an invalid ready line.')) }
    })
    done.then(({ error, code, signal }) => reject(new Error('The test server stopped (' +
      (error?.message || signal || 'exit ' + code) + '). ' + diagnostic())))
  })
  const stop = async () => {
    child.stdin.end()
    const timer = setTimeout(() => child.kill('SIGTERM'), 8000)
    try {
      const { error, code, signal } = await bounded(done, 20000, 'The test server did not shut down.')
      if (code !== 0) throw new Error('The test server shut down with ' +
        (error?.message || signal || 'exit ' + code) + (diagnostic() ? ': ' + diagnostic() : ''))
    } finally { clearTimeout(timer) }
  }
  return { ready, stop, diagnostic }
}

function answerProblem(method, pathname, status, expected = {}, blocked = false) {
  const request = method + ' ' + pathname
  if (status === 403 && blocked) return request + ' blocked by the test Tether (403). Check this route, then add it to ALLOWED in server/dev/verify_server.py or stub it with page.route in the recipe.'
  if (REDIRECTS.has(status)) return request + ' redirects (' + status + '): check sign-in and the URL'
  if (pathname.startsWith(API) && status >= 400 && expected[pathname.slice(API.length)] !== status) return request + ' answered ' + status
  return ''
}

function observeAnswers(expect = {}) {
  const expected = { ...EXPECTED, ...expect }
  const seen = new Set()
  const problems = []
  return {
    problems,
    answer(method, pathname, status, blocked) {
      const problem = answerProblem(method, pathname, status, expected, blocked)
      if (problem) problems.push(problem)
      else if (expect[pathname.slice(API.length)] === status && pathname.startsWith(API)) seen.add(pathname.slice(API.length))
    },
    finish() {
      for (const [endpoint, status] of Object.entries(expect)) {
        if (!seen.has(endpoint)) problems.push('expected ' + API + endpoint + ' to answer ' + status + ', but that status never arrived')
      }
    },
  }
}

// The relay talks to the test server directly. Node's global agent can follow HTTP_PROXY (NODE_USE_ENV_PROXY), which
// would send the request and its sign-in cookie to that proxy.
const DIRECT = new http.Agent()

async function loopbackRequest(url, request, cookie, signal) {
  const headers = { ...await request.allHeaders(), cookie, 'accept-encoding': 'identity' }
  for (const name of ['connection', 'keep-alive', 'proxy-authorization', 'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade']) delete headers[name]
  return new Promise((resolve, reject) => {
    const upstream = http.request(url, { method: request.method(), headers, signal, agent: DIRECT }, response => {
      const chunks = []
      response.on('data', chunk => chunks.push(chunk))
      response.on('error', reject)
      response.on('end', () => {
        const headers = { ...response.headers }
        for (const name of ['connection', 'keep-alive', 'transfer-encoding', 'set-cookie', 'trailer', 'upgrade']) delete headers[name]
        resolve({ status: response.statusCode, headers, body: Buffer.concat(chunks) })
      })
    })
    const timer = setTimeout(() => upstream.destroy(new Error('timeout')), 30000)
    upstream.once('close', () => clearTimeout(timer))
    upstream.on('error', reject)
    upstream.end(request.postDataBuffer())
  })
}

async function routeToServer(context, info, existing, reportFailure) {
  const origin = new URL(info.url).origin
  const cookie = info.cookie.name + '=' + info.cookie.value
  const controller = new AbortController()
  let active = true
  const handle = async route => {
    const request = route.request()
    let page
    try { page = request.frame().page() } catch { /* A popup's first request can precede its frame. */ }
    if (existing.has(page)) return route.fallback()
    const url = new URL(request.url())
    if (url.origin !== origin) return route.abort('blockedbyclient')
    if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404 })
    // A stream never ends. Let the UI poll the real handler.
    if (/^\/mobile\/api\/(?:[a-z-]+\/)?stream$/.test(url.pathname)) return route.fulfill({ status: 204 })
    try {
      const response = await loopbackRequest(url, request, cookie, controller.signal)
      if (REDIRECTS.has(response.status)) {
        reportFailure(page, request.method() + ' ' + url.pathname + ' redirects to ' + (response.headers.location || '(no Location)'))
        return await route.fulfill({ status: 502, contentType: 'text/plain', body: 'The fixture relay refused a redirect.' })
      }
      await route.fulfill(response)
    } catch {
      if (active) reportFailure(page, request.method() + ' ' + url.pathname + ' failed on the fixture connection')
      await route.abort().catch(() => {})
    }
  }
  // The installed Playwright has no unrouteWebSocket. Disable its matcher before disconnecting.
  try {
    await context.routeWebSocket(() => active, socket => socket.close())
    await context.route('**/*', handle)
  } catch (error) {
    active = false
    controller.abort()
    throw error
  }
  return async () => {
    active = false
    controller.abort()
    // A browser that is already gone has no routes left to remove.
    await context.unroute('**/*', handle).catch(error => { if (!/has been closed/.test(error.message)) throw error })
  }
}

function init(pane) {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('The verify run turns off service workers'))
  localStorage.clear()
  localStorage.setItem('tmux-chat.prefs.synced', '1')
  localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: pane, theme: 'light', voiceOff: true, voiceSound: false }))
}

// Runs inside the page. Report what the window cuts and note full-width sideways scrollers.
function cutByEdge() {
  const view = document.documentElement.clientWidth
  const cut = [], notes = []
  const within = rect => rect.left >= -1 && rect.right <= view + 1
  const scroller = style => style.overflowX === 'auto' || style.overflowX === 'scroll'
  const describe = el => '<' + el.tagName.toLowerCase() + (el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : '') + '>'
  for (const element of document.body.querySelectorAll('*')) {
    const rect = element.getBoundingClientRect()
    if (rect.width < 1 || rect.height < 1 || rect.bottom <= 0 || rect.top >= innerHeight) continue
    if (element.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }) === false) continue
    if (scroller(getComputedStyle(element)) && rect.width >= view - 1 && within(rect) && element.scrollWidth > element.clientWidth + 1) {
      notes.push(describe(element) + ' scrolls sideways (' + element.scrollWidth + ' px of ' + element.clientWidth + ' px)')
    }
    if (cut.length >= 5 || within(rect) || rect.left >= view || rect.right <= 0) continue
    let clipped = false, escapes = false
    for (let child = element; child.parentElement && child.parentElement !== document.body; child = child.parentElement) {
      const box = child.parentElement, style = getComputedStyle(box), area = box.getBoundingClientRect()
      const position = getComputedStyle(child).position
      if ((position === 'absolute' || position === 'fixed') && !box.contains(child.offsetParent)) escapes = true
      if (escapes || style.overflowX === 'visible') continue
      const narrow = area.width < view - 1
      if ((scroller(style) && (narrow || within(area))) || style.textOverflow === 'ellipsis' || (within(area) && narrow)) {
        clipped = true
        break
      }
    }
    if (!clipped && !cut.some(([parent]) => parent.contains(element))) {
      cut.push([element, describe(element) + ' spans ' + Math.round(rect.left) + ' to ' + Math.round(rect.right) + ' px of ' + view])
    }
  }
  return { cut: cut.map(([, line]) => line), notes }
}

function landmarkCover(landmark) {
  const box = landmark.getBoundingClientRect()
  const left = Math.max(0, box.left), top = Math.max(0, box.top)
  const right = Math.min(document.documentElement.clientWidth, box.right)
  const bottom = Math.min(document.documentElement.clientHeight, box.bottom)
  for (const fraction of [0.5, 0.25]) {
    const hit = document.elementFromPoint(left + (right - left) * fraction, top + (bottom - top) * fraction)
    if (landmark.contains(hit)) continue
    let cover = hit
    if (hit?.contains(landmark)) {
      cover = [...hit.querySelectorAll('dialog[open], [role="dialog"], [aria-modal="true"]')].find(dialog =>
        !dialog.contains(landmark) && dialog.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }) !== false)
      if (!cover) continue
    }
    if (!cover) return '<no element>'
    const classes = [...cover.classList].slice(0, 2)
    return '<' + cover.localName + (classes.length ? '.' + classes.join('.') : '') + '>'
  }
  return ''
}

async function useContext(context, info) {
  const existing = new Set(context.pages())
  const pages = new Set()
  const pageNavigation = new Map()
  const observedRequests = new WeakMap()
  const requestShots = new WeakMap()
  const origin = new URL(info.url).origin
  let shot, opening, order = 0
  const setAwaiting = (request, awaiting) => {
    const observed = observedRequests.get(request)
    const navigation = observed?.mainNavigation && pageNavigation.get(observed.page)
    if (navigation) navigation.awaiting = awaiting
  }
  const events = {
    page(page) {
      if (page.isClosed() || existing.has(page)) return
      pages.add(page)
      const navigation = { awaiting: false, commit: 0, stop }
      const pageEvents = {
        framenavigated(frame) {
          if (frame !== page.mainFrame() || !navigation.awaiting) return
          navigation.awaiting = false
          navigation.commit = ++order
        },
        domcontentloaded() {
          if (!navigation.commit) return
          // A replaced document may never report request completion, so stop waiting for its requests.
          if (shot) {
            for (const request of shot.pending) {
              const observed = observedRequests.get(request)
              if (observed?.page === page && observed.order < navigation.commit) shot.pending.delete(request)
            }
          }
          navigation.commit = 0
        },
        close: stop,
      }
      function stop() {
        for (const [event, handler] of Object.entries(pageEvents)) page.off(event, handler)
        pageNavigation.delete(page)
        pages.delete(page)
      }
      pageNavigation.set(page, navigation)
      for (const [event, handler] of Object.entries(pageEvents)) page.on(event, handler)
    },
    weberror(error) {
      const page = error.page()
      if (shot && !shot.closing && page && !page.isClosed() && !existing.has(page)) {
        shot.problems.push('page error: ' + error.error().message.split('\n')[0])
      }
    },
    request(request) {
      let frame
      try { frame = request.frame() } catch { /* A popup's first navigation can precede its frame. Worker requests have no page. */ }
      const page = frame?.page()
      const isNavigation = request.isNavigationRequest()
      const mainNavigation = isNavigation && page && frame === page.mainFrame()
      observedRequests.set(request, { page, order: ++order, mainNavigation })
      setAwaiting(request, false)
      if (!shot || shot.closing) return
      const url = new URL(request.url())
      if (url.origin !== origin) return
      if (existing.has(page) || (!page && (!isNavigation || request.serviceWorker()))) return
      requestShots.set(request, shot)
      shot.requests.add(request.method() + ' ' + url.pathname)
      if (url.pathname.startsWith(API)) shot.pending.add(request)
    },
    requestfinished(request) {
      requestShots.get(request)?.pending.delete(request)
    },
    requestfailed(request) {
      setAwaiting(request, false)
      const current = requestShots.get(request)
      const pending = current?.pending.delete(request)
      // The page cancels a poll when a newer one starts (ERR_ABORTED). That is not a failed answer.
      if (pending && !current.closing && !/ERR_ABORTED/.test(request.failure()?.errorText || '')) {
        current.problems.push(request.method() + ' ' + new URL(request.url()).pathname + ' did not finish')
      }
    },
    response(response) {
      const request = response.request()
      setAwaiting(request, true)
      const current = requestShots.get(request)
      if (current && !current.closing) {
        current.answer(request.method(), new URL(request.url()).pathname, response.status(), response.headers()['x-tether-verify-blocked'] === '1')
      }
    },
  }
  for (const [event, handler] of Object.entries(events)) context.on(event, handler)
  const stopObserving = () => {
    for (const [event, handler] of Object.entries(events)) context.off(event, handler)
    for (const navigation of pageNavigation.values()) navigation.stop()
  }
  let unroute
  try {
    unroute = await routeToServer(context, info, existing, (page, problem) => {
      if (shot && !shot.closing && (!page || pages.has(page))) shot.problems.push(problem)
    })
  } catch (error) {
    stopObserving()
    throw error
  }
  return {
    newPage() {
      opening = context.newPage().finally(() => { opening = undefined })
      return opening
    },
    measure(expect) {
      shot = { ...observeAnswers(expect), pending: new Set(), requests: new Set(), closing: false }
      return shot
    },
    async closePages() {
      if (shot) shot.closing = true
      await bounded((async () => {
        if (opening) await opening.catch(() => {})
        while (pages.size) await Promise.all([...pages].map(async page => {
          await page.close().catch(error => { if (!/has been closed/.test(error.message)) throw error })
          pageNavigation.get(page)?.stop()
        }))
      })(), 5000, 'The verify pages did not close.')
    },
    async stop() {
      if (shot) shot.closing = true
      try { await this.closePages() }
      finally {
        stopObserving()
        await bounded(unroute(), 5000, 'The verify routes did not close.')
      }
    },
  }
}

async function waitForApi(shot, signal, afterCapture = false) {
  const deadline = Date.now() + 5000
  while (shot.pending.size && Date.now() < deadline) {
    signal?.throwIfAborted()
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  if (shot.pending.size) {
    const pending = [...new Set([...shot.pending].map(request => request.method() + ' ' + new URL(request.url()).pathname))].join(', ')
    throw new Error(afterCapture ? pending + ' did not finish before capture ended' : 'API still in flight after 5 s: ' + pending)
  }
}

async function shoot(run, info, name, surface, width, folder, signal) {
  const shot = run.measure(surface.expect)
  const file = path.join(folder, name + '-' + width + '.png')
  const notes = []
  let page, captured = false
  try {
    const height = HEIGHTS[width] || 800
    page = await run.newPage()
    signal?.throwIfAborted()
    page.setDefaultTimeout(15000)
    await page.setViewportSize({ width, height })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.addInitScript(init, info.pane)
    const ctx = { width, phone: width < 768, pane: info.pane, thread: info.thread, url: info.url }
    const checkPage = async () => {
      if (await page.locator('.signin, form[action="/mobile/login"]').count()) throw new Error('the sign-in page opened')
      if (await page.locator('.crash-actions').count()) throw new Error('the crash screen is showing')
    }
    const landmark = page.locator(surface.landmark).first()
    await page.goto(info.url + '?session=' + encodeURIComponent(info.pane), { waitUntil: 'load', timeout: 30000 })
    await checkPage()
    await page.locator('.app-shell').first().waitFor({ state: 'visible' })
    await surface.open(page, ctx)
    await landmark.waitFor({ state: 'visible' }).catch(() => { throw new Error('landmark did not show: ' + surface.landmark) })
    if (surface.ready) await surface.ready(page, ctx)
    await bounded(page.evaluate(() => document.fonts.ready), 5000, 'The page fonts did not settle.')
    await page.waitForTimeout(400)
    await waitForApi(shot, signal)
    await checkPage()
    const box = await landmark.boundingBox({ timeout: 1000 }).catch(() => null)
    const visible = box && await landmark.evaluate(
      el => el.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }) !== false, undefined, { timeout: 1000 },
    ).catch(() => false)
    if (!box || !visible || box.x < -1 || box.y < -1
        || box.x + box.width > width + 1 || box.y + box.height > height + 1) {
      throw new Error('landmark is outside the viewport or hidden at capture: ' + surface.landmark)
    }
    const size = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, view: document.documentElement.clientWidth }))
    if (size.content > size.view) shot.problems.push('sideways scroll: the page is ' + size.content + ' px wide in a ' + size.view + ' px window')
    const edges = await page.evaluate(cutByEdge)
    notes.push(...edges.notes)
    if (edges.cut.length) shot.problems.push('cut off by the screen edge: ' + edges.cut.join(', '))
    const cover = await landmark.evaluate(landmarkCover, undefined, { timeout: 1000 })
    if (cover) throw new Error('landmark is covered by ' + cover)
    await page.screenshot({ path: file, timeout: 5000 })
    captured = true
    await waitForApi(shot, signal, true)
  } catch (error) {
    if (signal?.aborted) throw error
    shot.problems.push(error.message.split('\n')[0])
    if (page && !captured) await page.screenshot({ path: file, timeout: 2000 }).catch(() => {})  // Keep a failed shot for diagnosis.
  } finally {
    shot.closing = true
    if (!signal?.aborted) await run.closePages()
  }
  shot.finish()
  const problems = [...new Set(shot.problems)]
  return {
    surface: name, width, file: path.basename(file), ok: problems.length === 0, problems, notes,
    requests: [...shot.requests].sort(),
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (args.help) { console.log(fs.readFileSync(__filename, 'utf8').split('\n').slice(1, 7).map(line => line.replace(/^\/\/ ?/, '')).join('\n')); return }
  if (args.list) { for (const [name, surface] of Object.entries(SURFACES)) console.log(name.padEnd(10) + ' ' + surface.about); return }
  const { surfaces, chosen } = loadSurfaces(args)
  if (!chosen.length) throw new Error('Name what to check with --surface or --recipe. See --list.')
  const tree = treeId(), folder = shotsFolder(args), built = !args.dist
  const results = [], cleanupErrors = []
  let temp, server, browser, ownContext, run, info, treeAfter, failure, exit = 0
  const cancel = new AbortController()
  const stop = code => {
    if (!cancel.signal.aborted) {
      exit = code
      cancel.abort(new Error('Verification cancelled.'))
    }
  }
  const interrupt = () => stop(130), terminate = () => stop(143)
  process.on('SIGINT', interrupt)
  process.on('SIGTERM', terminate)
  async function clean(action) {
    try { await action() } catch (error) { cleanupErrors.push(error.message.split('\n')[0]) }
  }
  let leaks = [], blocked = []
  const readLogs = () => {
    if (!info) return
    const read = file => {
      const log = path.join(info.root, file)
      return fs.existsSync(log) ? fs.readFileSync(log, 'utf8').split('\n').filter(Boolean) : []
    }
    leaks = read('leaks.log')
    blocked = [...new Set(read('blocked.log'))]
  }
  try {
    if (built) temp = fs.mkdtempSync(path.join(os.tmpdir(), 'tether-verify-'))
    const dist = args.dist || await build(temp, cancel.signal)
    cancel.signal.throwIfAborted()
    if (!fs.existsSync(path.join(dist, 'index.html'))) throw new Error('No built UI in ' + dist)
    server = startServer(dist)
    info = await unlessCancelled(bounded(server.ready, 60000, 'The test server did not start in 60 s.'), cancel.signal)
    console.log('Test Tether: ' + info.url + ' (fixture data in ' + info.root + ')')
    browser = await getBrowser({ local: Boolean(args.local) })
    cancel.signal.throwIfAborted()
    if (args.local) ownContext = await browser.newContext({ serviceWorkers: 'block' })
    const context = ownContext || browser.contexts()[0]
    if (!context) throw new Error('The shared browser has no open context.')
    run = await useContext(context, info)
    for (const name of chosen) for (const width of args.widths) {
      cancel.signal.throwIfAborted()
      if (!args.local && context.pages().length >= 3) throw new Error('The shared browser already has three tabs. Close one, or run with --local.')
      const result = await unlessCancelled(shoot(run, info, name, surfaces[name], width, folder, cancel.signal), cancel.signal)
      results.push(result)
      console.log((result.ok ? 'ok   ' : 'FAIL ') + name + ' ' + width + ' px' + (result.ok ? '' : ': ' + result.problems.join('; ')))
      for (const note of result.notes) console.log('note: ' + name + ' ' + width + ' px ' + note)
    }
    treeAfter = treeId()
  } catch (error) {
    if (!exit) failure = error.message
  } finally {
    if (run) await clean(() => run.stop())
    if (ownContext) await clean(() => bounded(ownContext.close(), 5000, 'The verify context did not close.'))
    if (browser) await clean(() => bounded(browser.close(), 5000, 'The browser connection did not close.'))
    await clean(readLogs)
    if (server) await clean(() => server.stop())
    if (temp) await clean(() => fs.rmSync(temp, { recursive: true, force: true }))
    process.off('SIGINT', interrupt)
    process.off('SIGTERM', terminate)
  }
  const passed = !exit && !failure && !cleanupErrors.length && !leaks.length && results.length === chosen.length * args.widths.length && results.every(result => result.ok)
  if (failure) console.log('FAIL ' + failure + (!info && server?.diagnostic() && !failure.includes(server.diagnostic()) ? '\n' + server.diagnostic() : ''))
  for (const error of cleanupErrors) console.log('FAIL cleanup: ' + error)
  for (const leak of leaks) console.log('FAIL the test server refused: ' + leak)
  for (const entry of blocked) console.log('note: the test server refused ' + entry)
  fs.writeFileSync(path.join(folder, 'manifest.json'), JSON.stringify({
    passed, built, tree, treeAfter, at: new Date().toISOString(), widths: args.widths, results, leaks, blocked, failure, cleanupErrors, cancelled: Boolean(exit),
  }, null, 2) + '\n')
  console.log('Shots: ' + folder)
  if (!passed) {
    console.log(exit ? 'verify: cancelled.' : 'verify: FAIL. Fix the problems above, then run again.')
    process.exitCode = exit || 1
    return
  }
  const reason = stampReason(built, args.widths, tree !== treeAfter)
  const main = path.dirname(git('rev-parse', '--path-format=absolute', '--git-common-dir'))
  const shown = folder.startsWith(main + path.sep) ? path.relative(main, folder) : folder
  console.log(reason ? 'verify: PASS (no stamp: ' + reason + ')' : 'verify: PASS. Stamp item: screenshots(' + results.length + ' ' + shown + ')')
}

if (require.main === module) main().catch(error => { console.error('verify: FAIL. ' + error.message); process.exitCode = 1 })
module.exports = { loopbackRequest, parseArgs, loadSurfaces, answerProblem, observeAnswers, folderName, shotsFolder, sourceTreeId, stampReason, startServer, useContext, shoot, SURFACES }
