// A failed lazy chunk or a render error must never leave a blank page (src/staleChunk.ts, src/CrashScreen.tsx).
// Real browser against a build: DIST (default dist). Run: node test_crash_screen.cjs
const { strict: assert } = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const ORIGIN = 'http://tether.test'
const fixtures = {
  sessions: { sessions: [{ id: '%1', tmux_session: '$1', label: 'Crash test', pane_icon: 'squirrel', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 'fixture-thread', approval: null }], default_thread: 'fixture-thread' },
  chat: { messages: Array.from({ length: 60 }, (_, i) => ({ id: `message-${i}`, role: i % 2 ? 'assistant' : 'user', text: `History item ${i + 1}. A fixture conversation with enough text to scroll.\n\nReview the interface and record the result.`, time: '2026-11-11T08:00:00Z' })), busy: false, thread: 'fixture-thread', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Claude', mode: 'default', suggestion: 'Review the next step', heads_up: { text: 'Heads up · Overlay review', state: 'explained', detail: Array.from({ length: 18 }, (_, i) => `- Check ${i + 1}: Review the overlay text and each available option before the next step. Keep this card readable while its long details scroll beneath the pinned chip row.`), options: [{ key: '1', label: 'Understood' }, { key: '0', label: 'Dismiss' }] }, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}
const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }

// fail(url, count): true when this request should get a 404, to stand in for a chunk a deploy removed.
async function open(browser, viewport, theme, fail) {
  const context = await browser.newContext({ viewport, colorScheme: theme, serviceWorkers: 'block' })
  const chunks = [], errors = []
  let navigations = 0
  await context.route(`${ORIGIN}/**`, route => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/mobile/api/')) {
      const name = url.pathname.slice('/mobile/api/'.length)
      return name === 'stream' ? route.fulfill({ status: 204 }) : route.fulfill({ json: fixtures[name] ?? {} })
    }
    if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404 })
    const file = path.join(DIST, url.pathname.slice('/chat/'.length))
    const isFile = fs.existsSync(file) && fs.statSync(file).isFile()
    if (url.pathname.startsWith('/chat/assets/') && url.pathname.endsWith('.js')) {
      chunks.push(url.pathname)
      if (fail(url.pathname)) return route.fulfill({ status: 404, body: '' })
    }
    if (!isFile) navigations++
    const served = isFile ? file : path.join(DIST, 'index.html')
    return route.fulfill({ status: 200, contentType: types[path.extname(served)] ?? 'application/octet-stream', body: fs.readFileSync(served) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  return { context, page, chunks, errors, navigations: () => navigations }
}

const SHOTS = process.env.SHOTS
const stopped = page => page.getByRole('alert').filter({ hasText: 'Tether stopped' })

async function main() {
  const browser = await getBrowser()
  const results = []
  const check = async (name, fn) => {
    try { await fn(); results.push(`ok ${name}`) } catch (error) { results.push(`FAIL ${name}: ${error.message.split('\n')[0]}`); process.exitCode = 1 }
  }
  try {
    // The session icon loads lucide's dynamic chunk, as in a real tab with @chat_icon.
    await check('a chunk missing once: one reload, then the app opens', async () => {
      let misses = 0
      const run = await open(browser, { width: 1280, height: 900 }, 'light', chunk => /\/dynamic-[^/]+\.js$/.test(chunk) && misses++ === 0)
      try {
        await run.page.goto(`${ORIGIN}/chat/`)
        await run.page.getByText('Crash test').first().waitFor({ timeout: 20000 })
        await run.page.waitForLoadState('networkidle')
        assert.equal(misses, 2, 'chunk asked for again after the reload')
        assert.equal(run.navigations(), 2, 'exactly one reload')
        assert.equal(await stopped(run.page).count(), 0)
      } finally { await run.context.close() }
    })
    for (const [label, viewport, theme] of [['desktop light', { width: 1280, height: 900 }, 'light'], ['phone 320 dark', { width: 320, height: 640 }, 'dark']]) {
      await check(`a chunk missing for good (${label}): Tether stopped with Reload and details, no reload loop`, async () => {
        const run = await open(browser, viewport, theme, chunk => /\/dynamic-[^/]+\.js$/.test(chunk))
        const logged = []
        run.page.on('console', message => { if (message.type() === 'error' && message.text().startsWith('Tether stopped:')) logged.push(message.text()) })
        try {
          await run.page.goto(`${ORIGIN}/chat/`)
          const alert = stopped(run.page)
          await alert.waitFor({ timeout: 20000 })
          await run.page.waitForTimeout(1500)
          assert.equal(run.navigations(), 2, 'one automatic reload only')
          assert.ok(await alert.getByRole('button', { name: 'Reload' }).isVisible())
          assert.ok(await alert.getByRole('button', { name: 'Copy details' }).isVisible())
          assert.match(await alert.locator('pre').textContent(), /dynamic-[^/]+\.js[\s\S]*Page: http:\/\/tether\.test\/chat\//)
          assert.equal(logged.length > 0, true, 'error logged to the console')
          const overflow = await run.page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
          assert.ok(overflow <= 0, `no sideways scroll (${overflow}px)`)
          if (SHOTS) {
            await alert.locator('summary').click()
            await run.page.screenshot({ path: path.join(SHOTS, `crash-${label.replaceAll(' ', '-')}.png`), fullPage: true })
          }
          await alert.getByRole('button', { name: 'Reload' }).click()
          await alert.waitFor({ state: 'detached', timeout: 10000 }).catch(() => {})
          await stopped(run.page).waitFor({ timeout: 20000 })
          assert.equal(run.navigations(), 3, 'Reload loads the page again')
        } finally { await run.context.close() }
      })
    }
  } finally { await browser.close() }
  console.log(results.join('\n'))
  console.log(`crash screen: ${process.exitCode ? 'FAIL' : 'PASS'}`)
}
main().catch(error => { console.error(error); process.exitCode = 1 })
