// Installed app name and icon (Settings › Appearance, src/AppIdentity.tsx, server/app_identity.py) against an existing
// build: the setting loads, previews the name and colour, saves both, renders at 320 px and 1280 px without overflow,
// and names the save in a status line. The sidebar brand shows the saved name and icon, and follows a save at once. Every API request stays in this page's fixtures.
// Run: cd ui && npm run test:ui (or DIST=<build> node test_app_identity_ui.cjs).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('app-identity')
const sizes = [[320, 720], [1280, 900]]
const paneA = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Fix login', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null }
const paneB = { id: '%2', tmux_session: '$1', label: 'project:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/docs', state: 'Idle', writable: true, thread: 't2', approval: null }
const fixture = {
  sessions: { sessions: [paneA, paneB], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { projects: [], items: [] },
}
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
let prefs = {}, version = 1
const COLORS = [['clay', 'Clay', '#C96442'], ['blue', 'Blue', '#2F6BD8'], ['green', 'Green', '#2E7D4F']].map(([id, label, hex]) => ({ id, label, hex }))
const BLANK = { name: 'Tether', color: 'clay', icon: 'color', art: null, draft: null, job: null }
let identity = { ...BLANK }
// The name the server writes into index.html, as server/app_identity.py page() does; null serves the build as it is.
let served = null

async function main() {
  let browser, page, server, failed = 0
  const posts = [], pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime0 = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime0[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('console', message => { if (message.type() === 'error') pageErrors.push(message.text()) })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') {
          if (endpoint === 'app-identity/upload') {
            posts.push({ endpoint, kind: request.headers()['content-type'], size: request.postDataBuffer()?.length })
            identity = { ...identity, draft: 'd2' }
            return route.fulfill({ json: { ...identity, colors: COLORS } })
          }
          const body = request.postDataJSON(); posts.push({ endpoint, body })
          if (endpoint === 'app-identity/generate') return route.fulfill({ status: 202, json: { job: 'j1' } })
          if (endpoint === 'prefs') {
            prefs = { ...prefs, ...body.changes }; version++
            return route.fulfill({ json: { prefs, version } })
          }
          if (endpoint === 'app-identity') {
            if (body.useDraft) identity = { ...identity, icon: 'custom', art: identity.draft, draft: null }
            else if (body.discardDraft) identity = { ...identity, draft: null }
            else identity = { ...identity, name: body.name, color: body.color, icon: body.icon }
            return route.fulfill({ json: { ...identity, colors: COLORS } })
          }
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs, version } })
        if (endpoint === 'app-identity') return route.fulfill({ json: { ...identity, colors: COLORS } })
        if (endpoint === 'app-identity/generate') { identity = { ...identity, draft: 'd1' }; return route.fulfill({ json: { state: 'done' } }) }
        if (endpoint.startsWith('app-identity/preview/')) return route.fulfill({ body: await fs.readFile(path.join(DIST, 'icon-192.png')), contentType: 'image/png' })
        if (endpoint === 'activity') return route.fulfill({ json: { enabled: false, agents: false, zone: 'UTC', since: null, store: '', days: [], projects: [], publish: { folder: '', file: '', busy: false, pending: false, published: null, last: null } } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // Serve the build from here: the shared browser may not reach this process's loopback port.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try {
          let body = await fs.readFile(file)
          if (served && file === path.join(DIST, 'index.html')) body = body.toString().replace('<title>Tether</title>', `<title>${served}</title><meta name="apple-mobile-web-app-title" content="${served}" />`)
          return route.fulfill({ body, contentType: mime[path.extname(file)] || 'application/octet-stream' })
        } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    for (const [width, height] of sizes) {
      const at = `${width}px`
      prefs = { selected: '%1' }; version++
      identity = { ...BLANK }
      posts.length = 0
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      const brand = page.locator('.sidebar-brand:visible')
      check(`${at} sidebar shows the default name and mark`, (await brand.textContent()) === 'Tether' && await brand.locator('img').getAttribute('src') === '/chat/icon.svg')
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByRole('searchbox', { name: 'Search settings' }).fill('home screen icon')
      const name = page.getByRole('textbox', { name: 'App name' })
      await name.waitFor()
      check(`${at} loads the saved name`, await name.inputValue() === 'Tether')
      const save = page.getByRole('button', { name: 'Save app name and icon' })
      check(`${at} save waits for a change`, await save.isDisabled())
      await name.fill('  Work   box ')
      await page.locator('.preferences-colors label').filter({ hasText: 'Blue' }).click()
      const preview = page.locator('.app-identity-preview')
      check(`${at} preview shows the name`, (await preview.textContent()) === 'Work box')
      check(`${at} preview shows the colour`, await preview.locator('rect').getAttribute('fill') === '#2F6BD8')
      check(`${at} no page overflow`, !(await overflow()))
      await page.locator('.app-identity').scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `app-identity-${width}.png`) })
      await save.click()
      await page.getByRole('status').filter({ hasText: 'Saved.' }).waitFor()
      const sent = posts.find(post => post.endpoint === 'app-identity')
      check(`${at} posts the trimmed name and colour`, sent && sent.body.name === 'Work box' && sent.body.color === 'blue', JSON.stringify(sent))
      check(`${at} title follows`, await page.title() === 'Work box')
      const sidebarBrand = page.locator('.sidebar-brand')
      check(`${at} sidebar name follows the save`, (await sidebarBrand.first().textContent()) === 'Work box')
      const firstIcon = await sidebarBrand.first().locator('img').getAttribute('src')
      check(`${at} sidebar icon loads again`, /^\/chat\/icon\.svg\?v=\d+$/.test(firstIcon || ''), firstIcon)
      check(`${at} tab icon loads again`, await page.locator('link[rel="icon"]').getAttribute('href') === firstIcon)
      check(`${at} save turns off after saving`, await save.isDisabled())
      await name.fill('')
      await page.locator('.preferences-colors label').filter({ hasText: 'Green' }).click()
      await save.click()
      check(`${at} empty name is refused here`, await page.getByRole('alert').textContent() === 'Type a name.' && posts.filter(post => post.endpoint === 'app-identity').length === 1)
      await page.screenshot({ path: path.join(SHOTS, `app-identity-error-${width}.png`) })
      // Own picture: make one with AI, use it, then upload another and discard it.
      await name.fill('Work box')
      await page.getByRole('radio', { name: /My own picture/ }).check()
      check(`${at} empty state names both ways`, await page.getByText('No picture yet.').isVisible())
      const make = page.getByRole('button', { name: 'Make with AI' })
      check(`${at} make waits for a description`, await make.isDisabled())
      await page.getByRole('textbox', { name: 'Describe a picture for AI' }).fill('a lighthouse at night')
      await make.click()
      check(`${at} shows progress`, await page.getByRole('status').filter({ hasText: 'about 1 minute' }).isVisible())
      const draft = page.getByRole('img', { name: 'New icon with the Tether badge' })
      await draft.waitFor({ timeout: 15000 })
      check(`${at} sends the description`, posts.some(post => post.endpoint === 'app-identity/generate' && post.body.prompt === 'a lighthouse at night'))
      check(`${at} draft loads`, await draft.evaluate(image => image.complete && image.naturalWidth > 0))
      check(`${at} no page overflow with a draft`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `app-identity-draft-${width}.png`) })
      await page.getByRole('button', { name: 'Use this icon' }).click()
      await page.getByRole('status').filter({ hasText: 'Your picture is now the app icon' }).waitFor()
      check(`${at} preview shows the picture`, await preview.locator('img').count() === 1)
      check(`${at} sidebar icon follows the new picture`, await page.locator('.sidebar-brand img').first().getAttribute('src') !== firstIcon)
      check(`${at} use posts useDraft only`, posts.some(post => post.endpoint === 'app-identity' && post.body.useDraft === true && post.body.draft === 'd1' && !('name' in post.body)))
      await page.locator('.app-identity-own input[type=file]').setInputFiles({ name: 'me.png', mimeType: 'image/png', buffer: await fs.readFile(path.join(DIST, 'icon-192.png')) })
      await draft.waitFor()
      check(`${at} upload sends the image bytes`, posts.some(post => post.endpoint === 'app-identity/upload' && post.kind === 'image/png' && post.size > 100))
      await page.getByRole('button', { name: 'Discard' }).click()
      await draft.waitFor({ state: 'detached' })
      check(`${at} discard removes the draft`, !identity.draft && posts.some(post => post.endpoint === 'app-identity' && post.body.discardDraft === true && post.body.draft === 'd2'))
      await page.screenshot({ path: path.join(SHOTS, `app-identity-own-${width}.png`) })
      await page.keyboard.press('Escape'); await page.keyboard.press('Escape')
    }
    // A saved name that the server wrote into the page: the sidebar shows it from the start. The longest name (30
    // characters) ends in an ellipsis at 320 px and leaves the header buttons whole.
    for (const [width, height] of sizes) {
      const at = `${width}px`
      served = 'Wwwwwwwwwwwwwwwwwwwwwwwwwwwwww'
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      const brand = page.locator('.sidebar-brand:visible')
      check(`${at} sidebar shows the served name`, (await brand.textContent()) === served)
      const fit = await page.evaluate(() => {
        const name = [...document.querySelectorAll('.sidebar-brand span')].find(item => item.getClientRects().length)
        const top = name.closest('.sidebar-top'), last = top.querySelector('.sidebar-top-actions').lastElementChild
        return { cut: name.scrollWidth > name.clientWidth, inside: last.getBoundingClientRect().right <= top.getBoundingClientRect().right + 0.5, title: name.title }
      })
      check(`${at} a long name ends in an ellipsis`, width > 800 || fit.cut, JSON.stringify(fit))
      check(`${at} header buttons stay whole`, fit.inside && fit.title === served, JSON.stringify(fit))
      check(`${at} no page overflow with a long name`, !(await overflow()))
      await page.locator('.sidebar-top:visible').screenshot({ path: path.join(SHOTS, `sidebar-brand-${width}.png`) })
    }
    served = null
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
  } finally {
    await page?.close().catch(() => {})
    server?.close()
    await browser?.close?.().catch(() => {})
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exitCode = failed ? 1 : 0
}
main()
