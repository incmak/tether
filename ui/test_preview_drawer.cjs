// The preview drawer: Claude artifact cards, artifact links, HTML file chips and a plain localhost link in a chat.
// Run against an existing build (DIST). Every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || ''
const REMOTE = 'https://claude.ai/artifact/AbCdEf123456'
const ARTIFACT = { url: REMOTE, path: '/tmp/claude-1000/x/scratchpad/plan.html', title: 'Launch plan with a long title that has to wrap or cut' }
const PAGE = '<!doctype html><meta charset="utf-8"><title>Page</title><body style="margin:0"><h1 id="made">Made by an agent</h1></body>'

function fixtures() {
  const pane = { id: '%1', tmux_session: '$1', label: 'Preview test', agent: 'claude', dir: '/srv/project', state: 'Idle',
    writable: true, thread: 'preview-thread', approval: null }
  const messages = [{ id: 'm1', role: 'user', text: 'Make the plan page.', time: '2026-11-15T08:00:00Z' },
    { id: 'm2', role: 'assistant', kind: 'actions', text: 'Artifact', count: 1, time: '2026-11-15T08:00:05Z',
      tools: [{ id: 't1', name: 'Artifact', title: 'Publish plan.html', is_error: false, status: 'complete', artifact: ARTIFACT }] },
    { id: 'm3', role: 'assistant', time: '2026-11-15T08:00:09Z',
      text: `Done. Read [the plan](${REMOTE}), open \`site/index.html\` or \`site/broken.html\`, or run [the dev server](http://localhost:5173/).` }]
  return {
    sessions: { sessions: [pane], default_thread: pane.thread },
    chat: { messages, busy: false, thread: pane.thread, agent: pane.agent, writable: true, session: pane.id, earlier: 0, limit: 100,
      state: 'Idle', approval: null, attention: null },
    footer: { observed: true, model: 'Claude', mode: 'default', notices: [] },
    prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
    update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
    catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {},
    'codex-runs': { pane: '%1', running: 0, runs: [] }, 'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
    'browser/agents': { enabled: false, available: false, reason: 'Fixture browser off', agents: [], asks: [] },
    'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
    'work/board': { revision: 1, projects: [], items: [], stages: [] },
    usage: { claude: null, codex: null }, subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  }
}

async function main() {
  let browser, page, server, passed = 0, failed = 0
  const check = (name, errors) => {
    if (errors.length) { failed++; console.log(`FAIL ${name}: ${errors.join('; ')}`) } else { passed++; console.log(`ok ${name}`) }
  }
  try {
    await fs.access(path.join(DIST, 'index.html'))
    if (SHOTS) await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (req.method !== 'GET' || url.pathname.endsWith('/sw.js') || !url.pathname.startsWith('/chat/') || !file.startsWith(DIST)) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' })
        res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    for (const width of [320, 390, 768, 1280]) {
      const name = `drawer-${width}`, errors = [], posts = [], pageErrors = []
      const fixture = fixtures()
      if (page) await page.close().catch(() => {})
      page = await context.newPage()
      page.setDefaultTimeout(10000)
      // Playwright's own per-frame script reads navigator.serviceWorker, which throws in the sandboxed preview frame.
      page.on('pageerror', error => { if (!/Service worker is disabled because the context is sandboxed/.test(error.message)) pageErrors.push(error.message) })
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
      await page.routeWebSocket('**/*', socket => socket.close())
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
        if (url.pathname.startsWith('/mobile/preview/')) return route.fulfill({ status: 200, contentType: 'text/html', body: PAGE, headers: { 'Content-Security-Policy': 'sandbox allow-scripts' } })
        if (url.pathname.includes('/api/')) {
          const endpoint = url.pathname.split('/api/')[1]
          if (request.method() !== 'GET') {
            const body = request.postDataJSON() || {}
            posts.push({ endpoint, body })
            if (endpoint === 'file-check') return route.fulfill({ json: { results: Object.fromEntries(body.paths.map(item => [item, true])) } })
            if (endpoint === 'preview') return body.path === 'site/broken.html'
              ? route.fulfill({ status: 404, json: { error: 'That file is not there.' } })
              : route.fulfill({ json: { url: `/mobile/preview/token123/${body.path.split('/').pop()}`, name: 'x', path: body.path } })
            if (endpoint === 'browser/new') return route.fulfill({ json: { id: 'tab9', url: body.url, title: '' } })
            return route.fulfill({ json: { ok: true, accepted: true } })
          }
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
          return route.fulfill({ json: fixture[endpoint] || {} })
        }
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
        return route.abort('blockedbyclient')
      })
      await page.addInitScript(() => {
        if (window !== window.top) return // The sandboxed preview frame has no storage or service workers.
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false, sidePins: [], look: { layout: 'classic', surface: 'flat', backdrop: 'ember' } }))
      })
      try {
        const height = 800
        await page.setViewportSize({ width, height })
        await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
        await page.waitForSelector('.composer textarea')
        const drawer = page.locator('dialog.preview-drawer')
        const frameShows = async () => {
          await drawer.locator('iframe').waitFor()
          await page.frameLocator('dialog.preview-drawer iframe').locator('#made').waitFor()
          await drawer.locator('.preview-loading').waitFor({ state: 'detached' })
        }
        const closeDrawer = async () => {
          await page.keyboard.press('Escape')
          await page.waitForFunction(() => !document.querySelector('dialog.preview-drawer')?.open)
        }

        // 1. The artifact card under the tool group opens the local copy, and links to claude.ai.
        const card = page.locator('.tool-artifacts li')
        await card.waitFor()
        if (await card.count() !== 1) errors.push(`${await card.count()} artifact cards, want 1`)
        if (await card.getByRole('link', { name: /on claude\.ai/ }).getAttribute('href') !== REMOTE) errors.push('the card has no claude.ai link')
        const opener = card.locator('button.artifact-main')
        const cardBox = await card.boundingBox()
        if (!cardBox || cardBox.height < 44) errors.push(`the card is ${cardBox && Math.round(cardBox.height)} px high`)
        await opener.click()
        await frameShows()
        const preview = posts.find(post => post.endpoint === 'preview')
        if (preview?.body.path !== ARTIFACT.path || preview?.body.session !== '%1') errors.push(`preview asked for ${JSON.stringify(preview?.body)}`)
        if (!(await drawer.locator('h2').innerText()).startsWith('Launch plan')) errors.push('wrong drawer title')
        if (!(await drawer.locator('.preview-sub').innerText()).includes('Claude artifact')) errors.push('no artifact subtitle')
        if (await drawer.getByRole('link', { name: 'Open on claude.ai' }).getAttribute('href') !== REMOTE) errors.push('the drawer does not open claude.ai')
        if (await drawer.locator('iframe').getAttribute('sandbox') === null || (await drawer.locator('iframe').getAttribute('sandbox')).includes('allow-same-origin')) errors.push('the frame is not sandboxed')

        // 2. Geometry: a phone sheet leaves 5-10% of the chat in view; a wide screen gets a full-screen modal.
        let box = await drawer.boundingBox()
        if (width < 600) {
          const gap = box.y / height
          if (gap < 0.05 || gap > 0.1) errors.push(`the sheet starts at ${Math.round(gap * 100)}% of the height`)
          if (Math.round(box.width) !== width) errors.push(`the sheet is ${Math.round(box.width)} px wide`)
          if (await drawer.getByRole('button', { name: 'Phone width' }).isVisible()) errors.push('phone width shows on a phone')
          const title = await drawer.locator('h2').boundingBox()
          if (title.width < width * 0.6) errors.push(`the title gets ${Math.round(title.width)} px`)
        } else {
          if (Math.round(box.x) !== 0 || Math.round(box.y) !== 0 || Math.round(box.height) !== height || Math.round(box.width) !== width) errors.push(`the modal box is ${JSON.stringify(box)}`)
          if (await drawer.getByRole('button', { name: 'Full screen' }).isVisible()) errors.push('full screen shows on a wide screen')
          await drawer.getByRole('button', { name: 'Phone width' }).click()
          const frameBox = await drawer.locator('iframe').boundingBox()
          if (Math.round(frameBox.width) > 390) errors.push(`phone width frame is ${Math.round(frameBox.width)} px`)
          await drawer.getByRole('button', { name: 'Phone width' }).click()
        }
        // 3. Phone: full screen covers the whole view; a second press restores it.
        const full = drawer.getByRole('button', { name: 'Full screen' })
        if (width < 600) {
        await full.click()
        box = await drawer.boundingBox()
        if (Math.round(box.y) !== 0 || Math.round(box.height) !== height || Math.round(box.width) !== width) errors.push(`full screen box is ${JSON.stringify(box)}`)
        if (await full.getAttribute('aria-pressed') !== 'true') errors.push('full screen is not pressed')
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}-full.png`) })
        await full.click()
        }
        for (const button of await drawer.locator('.preview-head :is(button, a)').all()) {
          if (!await button.isVisible()) continue
          const size = await button.boundingBox()
          if (size.height < 24 || size.width < 24) errors.push(`a drawer control is ${Math.round(size.width)}x${Math.round(size.height)} px`)
          if (!await button.getAttribute('aria-label')) errors.push('a drawer control has no name')
        }
        const headBox = await drawer.locator('.preview-head').boundingBox()
        if (Math.round(headBox.width) > width) errors.push('the drawer header is wider than the view')
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
        await closeDrawer()
        if (!await opener.evaluate(node => node === document.activeElement)) errors.push('focus does not return to the card')

        // 4. The claude.ai link in the reply opens the same local copy.
        await page.getByRole('button', { name: /^Preview Launch plan/ }).click()
        await frameShows()
        await closeDrawer()

        // 5. An HTML file chip opens the file by its path; a refused file shows the reason and a retry.
        await page.getByRole('button', { name: 'Open site/index.html' }).click()
        await frameShows()
        if (await drawer.locator('h2').innerText() !== 'index.html') errors.push('the file title is wrong')
        if (!(await drawer.getByRole('link', { name: 'Open in browser' }).getAttribute('href')).includes('/mobile/preview/token123/index.html')) errors.push('Open in browser has the wrong link')
        // View source reads the preview link (it also works for scratchpad pages) and offers no edit.
        await drawer.getByRole('button', { name: 'View source' }).click()
        const source = page.locator('dialog[open]').filter({ hasText: 'Made by an agent' }).filter({ hasNot: page.locator('iframe') })
        await source.waitFor({ timeout: 5000 }).catch(() => errors.push('View source does not show the HTML'))
        if (await source.getByRole('button', { name: 'Edit' }).count()) errors.push('the page source can be edited')
        await page.keyboard.press('Escape')
        await source.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => errors.push('the source viewer does not close'))
        await closeDrawer()
        await page.getByRole('button', { name: 'Open site/broken.html' }).click()
        const alert = drawer.getByRole('alert')
        await alert.waitFor()
        if (!(await alert.innerText()).includes('That file is not there.')) errors.push('the error has no reason')
        if (!await alert.getByRole('button', { name: 'Try again' }).count()) errors.push('the error has no retry')
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}-error.png`) })
        await closeDrawer()

        // 6. A localhost link stays a plain link: the shared browser cannot reach the host's loopback (port proxy: backlog).
        const local = page.getByRole('link', { name: 'the dev server' })
        if (await local.getAttribute('href') !== 'http://localhost:5173/' || await local.getAttribute('target') !== '_blank') errors.push('the localhost link changed')

        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
        if (overflow > 0) errors.push(`the page scrolls sideways by ${overflow} px`)
      } catch (error) { errors.push(error.message.split('\n')[0]) }
      check(name, [...errors, ...pageErrors.map(text => `page error: ${text}`)])
    }
  } catch (error) { failed++; console.log(`FAIL setup: ${error.message}`) } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) await new Promise(resolve => server.close(resolve))
  }
  console.log(`${passed} passed, ${failed} failed`)
  process.exit(failed ? 1 : 0)
}

main()
