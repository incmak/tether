// Cold first load of /chat/ against a build, served by the real server code. All API traffic uses fixtures.
// Budgets: BUDGET_DESKTOP_MS (2000), BUDGET_PHONE_MS (4000, Fast 4G: 150 ms RTT, 9 Mbps), BUDGET_ENTRY_GZIP_KB (600), BUDGET_CSS_GZIP_KB (100), RUNS (3).
const fs = require('node:fs/promises')
const path = require('node:path')
const os = require('node:os')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { gzipSync } = require('node:zlib')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const median = values => { const sorted = [...values].sort((a, b) => a - b), mid = sorted.length >> 1; return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2 }
const number = (name, fallback) => {
  const value = Number(process.env[name] ?? fallback)
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be positive`)
  return value
}
const fixtures = {
  sessions: { sessions: [{ id: '%1', tmux_session: '$1', label: 'Cold load test', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 'fixture-thread', approval: null }], default_thread: 'fixture-thread' },
  chat: { messages: Array.from({ length: 60 }, (_, i) => ({ id: `message-${i}`, role: i % 2 ? 'assistant' : 'user', text: `History item ${i + 1}. A fixture conversation with enough text to scroll.\n\nReview the interface and record the result.`, time: '2026-11-11T08:00:00Z' })), busy: false, thread: 'fixture-thread', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Claude', mode: 'default', suggestion: 'Review the next step', heads_up: { text: 'Heads up · Overlay review', state: 'explained', detail: Array.from({ length: 18 }, (_, i) => `- Check ${i + 1}: Review the overlay text and each available option before the next step. Keep this card readable while its long details scroll beneath the pinned chip row.`), options: [{ key: '1', label: 'Understood' }, { key: '0', label: 'Dismiss' }] }, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },  // Home and the sidebar read the work board
}
const python = `
import json, sys
from http.server import ThreadingHTTPServer
# Import reads attention state; the child environment points it at fixture storage.
def guard(event, args):
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise RuntimeError('Fixture forbids subprocesses')
sys.addaudithook(guard)
import server
with open(sys.argv[1]) as source:
    fixtures = json.load(source)
class Handler(server.Handler):
    keep_sessions = True
    def log_message(self, *args): pass
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path.startswith('/chat/'):
            self.serve_chat_ui(path)
        elif path.startswith('/mobile/api/'):
            endpoint = path[len('/mobile/api/'):]
            self.respond(204, b'') if endpoint == 'stream' else self.respond(200, fixtures.get(endpoint, {}))
        else:
            self.respond(404, {})
    def do_POST(self): self.respond(200, {'ok': True})
    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST
http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print(http.server_port, flush=True)
http.serve_forever()
`

// A shared download queue caps all connections together. Timers and sockets belong to this proxy.
async function throttle(target) {
  const sockets = new Set(), timers = new Set(), half = 75, bytesPerMs = 9e6 / 8 / 1000
  let linkFree = 0
  const later = (at, fn) => { const timer = setTimeout(() => { timers.delete(timer); fn() }, Math.max(0, at - performance.now())); timers.add(timer) }
  const proxy = net.createServer(client => {
    const up = net.connect(target, '127.0.0.1'), handshake = performance.now() + 150
    let uploaded = handshake, downloaded = 0
    for (const socket of [client, up]) { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) }
    const end = () => { client.destroy(); up.destroy() }
    client.on('data', data => { uploaded = Math.max(performance.now(), handshake) + half; later(uploaded, () => { if (!up.destroyed) up.write(data) }) })
    up.on('data', data => {
      linkFree = Math.max(linkFree, performance.now()) + data.length / bytesPerMs
      downloaded = linkFree + half
      later(downloaded, () => { if (!client.destroyed) client.write(data) })
    })
    up.on('end', () => later(downloaded + 1, () => client.end()))
    client.on('end', () => later(uploaded + 1, () => up.end()))
    up.on('error', end); client.on('error', end)
  })
  await new Promise((resolve, reject) => { proxy.once('error', reject); proxy.listen(0, '127.0.0.1', resolve) })
  return { port: proxy.address().port, close: async () => {
    for (const timer of timers) clearTimeout(timer)
    for (const socket of sockets) socket.destroy()
    await new Promise(resolve => proxy.close(resolve))
  } }
}

function init() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
  localStorage.clear()
  localStorage.setItem('tmux-chat.prefs.synced', '1')
  localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false,
    sidePins: [{ pane: '%1', note: 'Overlay fixture' }], look: { layout: 'classic', surface: 'flat', backdrop: 'ember' } }))
  const metrics = window.coldLoad = { fcp: null, composer: null, ready: null }
  const visible = el => {
    if (!el.offsetParent || getComputedStyle(el).visibility === 'hidden') return false
    const rect = el.getBoundingClientRect()
    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth
  }
  const poll = () => {
    metrics.fcp ??= performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null
    if (metrics.composer === null && [...document.querySelectorAll('.composer')].some(el => el.offsetParent)) metrics.composer = performance.now()
    if (metrics.ready === null && [...document.querySelectorAll('[data-msg-id], .assistant-message')].some(visible)) metrics.ready = performance.now()
    if (Object.values(metrics).some(value => value === null)) requestAnimationFrame(poll)
  }
  requestAnimationFrame(poll)
}

async function run(browser, port, viewport, entry) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block' }), errors = []
  try {
    await context.addInitScript(init)
    const page = await context.newPage(), tasks = []
    let bytes = 0, gzip = false
    page.on('pageerror', error => errors.push(error.stack))
    page.on('response', response => {
      if (new URL(response.url()).pathname.startsWith('/chat/assets/')) tasks.push((async () => {
        if (!response.ok()) throw new Error(`Asset HTTP ${response.status()}: ${response.url()}`)
        const headers = await response.allHeaders()
        if (new URL(response.url()).pathname === entry) gzip = ['br', 'gzip'].includes(headers['content-encoding'])
        await response.finished()
        bytes += headers['content-length'] !== undefined ? Number(headers['content-length']) : (await response.request().sizes()).responseBodySize
      })().then(() => null, error => error.message))
    })
    await page.goto(`http://127.0.0.1:${port}/chat/?session=%251`, { waitUntil: 'load', timeout: 30000 })
    if (errors.length) throw new Error(errors.join('; '))
    await Promise.race([page.waitForFunction(() => window.coldLoad && Object.values(window.coldLoad).every(value => value !== null), null, { timeout: 30000 }),
      new Promise((_, reject) => page.once('pageerror', reject))])
    if (await page.locator('.app-shell.phone-home, .app-shell.screen-home').count()) throw new Error('Home dashboard opened instead of chat')
    const metrics = await page.evaluate(() => window.coldLoad)
    errors.push(...(await Promise.all(tasks)).filter(Boolean))
    if (errors.length) throw new Error(errors.join('; '))
    return { ...metrics, bytes, gzip }
  } catch (error) {
    const detail = await context.pages()[0]?.evaluate(() => ({ metrics: window.coldLoad, shell: document.querySelector('.app-shell')?.className, text: document.body.innerText.slice(0, 400) })).catch(() => null)
    throw new Error(`${error.message.split('\n')[0]} ${JSON.stringify(detail)} ${errors.join('; ').replaceAll('\n', ' ')}`)
  } finally { await context.close() }
}

async function main() {
  let browser, child, proxy, temp, failed = false
  const check = (ok, message) => { console.log(`${ok ? 'ok' : 'FAIL'} ${message}`); if (!ok) failed = true }
  const stopChild = () => { if (child?.pid && child.exitCode === null) child.kill('SIGKILL') }
  const signal = () => { stopChild(); process.exit(1) }
  process.once('exit', stopChild); process.once('SIGINT', signal); process.once('SIGTERM', signal)
  try {
    const runs = number('RUNS', 3)
    if (!Number.isInteger(runs)) throw new Error('RUNS must be an integer')
    const html = await fs.readFile(path.join(DIST, 'index.html'), 'utf8')
    const entry = html.match(/<script\b[^>]*\bsrc=["']([^"']+\.js)["']/i)?.[1]
    const css = [...html.matchAll(/<link\b[^>]*\bhref=["']([^"']+\.css)["']/gi)].map(match => match[1])
    if (!entry || !css.length) throw new Error('Entry JS or CSS missing from index.html')
    for (const [name, assets, budget] of [['entry-js', [entry], number('BUDGET_ENTRY_GZIP_KB', 600)], ['entry-css', css, number('BUDGET_CSS_GZIP_KB', 100)]]) {
      let bytes = 0
      for (const asset of assets) bytes += gzipSync(await fs.readFile(path.join(DIST, asset.replace(/^\/chat\//, ''))), { level: 6 }).length
      check(bytes <= budget * 1024, `${name} gzipKB=${(bytes / 1024).toFixed(1)} budgetKB=${budget}`)
    }
    temp = await fs.mkdtemp(path.join(os.tmpdir(), 'tether-cold-load-'))
    const fixtureFile = path.join(temp, 'fixtures.json')
    await fs.writeFile(fixtureFile, JSON.stringify(fixtures))
    child = spawn('python3', ['-u', '-c', python, fixtureFile], { cwd: path.join(__dirname, '../server'), env: { ...process.env,
      PYTHONDONTWRITEBYTECODE: '1', TMUX_CHAT_DIST: DIST, MOBILE_TMUX_HOME: temp, MOBILE_TMUX_CONFIG: temp, MOBILE_TMUX_CACHE: temp,
      TETHER_HOME: temp, TETHER_ATTENTION: path.join(temp, 'attention.json'),
    }, stdio: ['ignore', 'pipe', 'pipe'] })
    let stderr = ''
    child.stderr.on('data', data => { stderr = (stderr + data).slice(-4000) })
    const port = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture server startup timed out')), 10000)
      const finish = (error, value) => { clearTimeout(timer); error ? reject(error) : resolve(value) }
      child.once('error', error => finish(error))
      child.once('exit', code => finish(new Error(`Fixture server exited ${code}: ${stderr}`)))
      let stdout = ''
      child.stdout.on('data', data => { stdout += data; if (/^\d+\n/.test(stdout)) finish(null, Number(stdout.split('\n')[0])) })
    })
    proxy = await throttle(port)
    browser = await getBrowser({ local: true })
    for (const [name, viewport, target, budget] of [
      ['desktop', { width: 1280, height: 900 }, port, number('BUDGET_DESKTOP_MS', 2000)],
      ['phone', { width: 390, height: 844 }, proxy.port, number('BUDGET_PHONE_MS', 4000)],
    ]) {
      const results = []
      for (let i = 0; i < runs; i++) {
        try { results.push(await run(browser, target, viewport, entry)) }
        catch (error) { failed = true; console.log(`FAIL ${name} run=${i + 1}: ${error.message.split('\n')[0]}`); results.push({ ready: Infinity, fcp: Infinity, composer: Infinity, bytes: 0, gzip: false }) }
      }
      const value = key => median(results.map(result => result[key]))
      // Tailscale Serve passes assets through raw, so the server's own compression (prebuilt Brotli since B26, else gzip)
      // is what keeps 4G loads fast.
      check(results.every(result => result.gzip), `${name} entry JS sent compressed (br or gzip)`)
      check(value('ready') <= budget, `${name} median=${Math.round(value('ready'))} runs=[${results.map(result => Math.round(result.ready)).join(',')}] fcp=${Math.round(value('fcp'))} composer=${Math.round(value('composer'))} assetsKB=${(value('bytes') / 1024).toFixed(1)} gzip=${results.every(result => result.gzip) ? 'yes' : 'no'}`)
    }
  } catch (error) { check(false, `runner: ${error.message}`) }
  finally {
    stopChild()
    if (browser) await browser.close().catch(() => {})
    if (proxy) await proxy.close()
    if (child?.pid && child.exitCode === null && child.signalCode === null) await new Promise(resolve => child.once('exit', resolve))
    if (temp) await fs.rm(temp, { recursive: true, force: true })
    process.removeListener('exit', stopChild); process.removeListener('SIGINT', signal); process.removeListener('SIGTERM', signal)
    console.log(`cold load: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}
main().catch(error => { console.error(`FAIL runner: ${error.message}\ncold load: FAIL`); process.exitCode = 1 })
