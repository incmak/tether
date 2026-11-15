// Trust a folder for Codex (B82) against an existing build. Every API request stays in this page's fixtures; no terminal
// or config file is touched. Run: cd ui && npm run build && node test_codex_trust_ui.cjs
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/b82-codex-trust/shots')
const sizes = [[320, 700], [1280, 900]]
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const FOLDER = '/workspace/app/packages/web'
const TARGET = '/workspace/app'
const codexPane = { id: '%2', tmux_session: '$1', label: 'app:0.0', pane_name: 'Docs pass', agent: 'codex', dir: FOLDER, state: 'Idle', writable: true, thread: 't2', approval: null }
const claudePane = { ...codexPane, id: '%1', label: 'app:0.1', pane_name: 'Plan', agent: 'claude', thread: 't1' }
const fixture = {
  sessions: { sessions: [claudePane, codexPane], default_thread: 't2' },
  chat: { messages: [], busy: false, thread: 't2', agent: 'codex', writable: true, session: '%2', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'gpt-test', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%2', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  'task-models': { claude: [], codex: [] }, worktrees: { repository: true, main: TARGET, base: '/workspace/app-wt' },
}
const b64 = bytes => Buffer.from(bytes).toString('base64url')
const approved = body => !!body.approval?.challenge_id && !!body.approval?.credential?.response?.signature
// The server state: 'untrusted' until an approved trust; newFails makes the new-tab trust fail once.
let trusted = false, newFails = false, slow = false

async function main() {
  let browser, page, server, failed = 0
  const posts = [], pageErrors = [], unexpected = new Set()
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime = MIME || { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`  // a secure context with a valid WebAuthn rp id
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('console', message => { if (message.type() === 'error' && !message.text().startsWith('Failed to load resource')) pageErrors.push(message.text()) })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') {
          const body = request.postDataJSON(); posts.push({ endpoint, body })
          if (endpoint === 'passkeys' && body.op === 'options') return route.fulfill({ json: { challenge_id: `c${posts.length}`,
            publicKey: { challenge: b64(Buffer.alloc(32, 3)), rpId: 'localhost', userVerification: 'required', timeout: 60000, allowCredentials: [{ type: 'public-key', id: b64(Buffer.from('cred-1')) }] } } })
          if (endpoint === 'codex-trust') {
            const running = body.mode === 'running'
            const status = trusted ? { state: running ? 'restart' : 'trusted', folder: FOLDER, target: null } : { state: 'untrusted', folder: body.worktree ? TARGET : FOLDER, target: TARGET }
            if (body.op === 'status') return route.fulfill({ json: status })
            if (!approved(body)) return route.fulfill({ status: 403, json: { error: 'Approve with your passkey.', approval: 'required' } })
            if (slow) await new Promise(resolve => setTimeout(resolve, 700))
            if (!running && newFails) { newFails = false; return route.fulfill({ status: 409, json: { error: 'Codex config.toml changed while Tether wrote it. Try again.' } }) }
            trusted = true
            return route.fulfill({ json: { state: running ? 'restart' : 'trusted', folder: FOLDER, target: null } })
          }
          if (endpoint === 'task-models') return route.fulfill({ json: fixture['task-models'] })
          if (endpoint.startsWith('worktrees')) return route.fulfill({ json: fixture.worktrees })
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint.startsWith('codex-level')) return route.fulfill({ json: { level: 0, global: 0, levels: [], project: null, models: [] } })
        if (endpoint.startsWith('autonomy')) return route.fulfill({ json: {} })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint.startsWith('codex-runs')) return route.fulfill({ json: fixture['codex-runs'] })
        if (endpoint.startsWith('worktrees')) return route.fulfill({ json: fixture.worktrees })
        if (endpoint.startsWith('worktree-info')) return route.fulfill({ json: { repo: true, base: 'main' } })
        if (!(endpoint in fixture)) unexpected.add(endpoint)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        // Serve the build from the page's routes: the shared browser may not reach this host's loopback port.
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try { return route.fulfill({ status: 200, contentType: MIME[path.extname(file)] || 'application/octet-stream', body: await fs.readFile(file) }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      unexpected.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      const buffer = text => new TextEncoder().encode(text).buffer
      Object.defineProperty(navigator, 'credentials', { configurable: true, value: {
        get: async () => ({ id: 'cred-1', rawId: buffer('cred-1'), type: 'public-key', response: { clientDataJSON: buffer('{"type":"webauthn.get"}'), authenticatorData: buffer('auth'), signature: buffer('sig'), userHandle: null } }),
      } })
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const trustPosts = from => posts.slice(from).filter(post => post.endpoint === 'codex-trust' && post.body.op === 'trust')
    for (const [width, height] of sizes) {
      const at = `${width}px`
      trusted = false; newFails = false; slow = false
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%252`, { waitUntil: 'load' })

      // A running Codex tab that is read-only because its folder is not trusted.
      const chip = page.getByRole('button', { name: 'Codex read-only' })
      await chip.waitFor()
      if (await chip.getAttribute('aria-expanded') !== 'true') await chip.click()
      const card = page.locator('.codex-trust')
      await card.waitFor()
      check(`${at} running: reason`, await card.getByText('Codex can only read files in this folder.').isVisible())
      check(`${at} running: names the folder to trust`, (await card.locator('.codex-trust-path').innerText()).trim() === TARGET)
      check(`${at} running: says it covers the repository`, await card.getByText(/whole repository, its worktrees included/).isVisible())
      check(`${at} running: group has a name`, await card.getAttribute('role') === 'group' && !!(await card.getAttribute('aria-labelledby')))
      check(`${at} running: no page overflow`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `running-untrusted-${width}.png`) })
      const button = card.getByRole('button', { name: 'Trust folder' })
      const box = await button.boundingBox()
      check(`${at} running: button is at least 24 px`, box && box.width >= 24 && box.height >= 24, JSON.stringify(box))
      // Keyboard: focus the button and press Enter.
      slow = true
      const first = posts.length
      await button.focus()
      await page.keyboard.press('Enter')
      const busy = card.getByRole('button', { name: /Trusting/ })
      await busy.waitFor()
      check(`${at} running: busy state disables the button`, await busy.isDisabled() && await busy.getAttribute('aria-busy') === 'true')
      await page.screenshot({ path: path.join(SHOTS, `running-busy-${width}.png`) })
      await page.getByText('Folder trusted. This Codex tab still runs read-only.').waitFor()
      const sent = trustPosts(first)
      check(`${at} running: first try has no approval, second has a passkey`, sent.length === 2 && !sent[0].body.approval && approved(sent[1].body), JSON.stringify(sent.map(post => !!post.body.approval)))
      check(`${at} running: binds tab and target`, sent.every(post => post.body.mode === 'running' && post.body.session === '%2' && post.body.target === TARGET))
      check(`${at} running: says what to do next`, await page.getByText(/type \/quit in this tab/).isVisible())
      check(`${at} running: restart message is a status`, await page.locator('.codex-trust[role="status"]').count() === 1)
      check(`${at} running: no page overflow after trust`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `running-restart-${width}.png`) })

      // A new Codex tab from the + popover, in a new worktree. The first trust fails; the error shows; the retry works.
      trusted = false; newFails = true; slow = false
      await page.getByRole('button', { name: /with a task$/ }).click()
      const dialog = page.locator('dialog').filter({ has: page.getByRole('radiogroup', { name: 'Agent' }) })
      await dialog.waitFor()
      check(`${at} new: no card for Claude`, await dialog.locator('.codex-trust').count() === 0)
      await dialog.getByRole('radio', { name: /Codex/ }).click()
      const newCard = dialog.locator('.codex-trust')
      await newCard.waitFor()
      check(`${at} new: reason`, await newCard.getByText('Codex can only read files in this folder.').isVisible())
      check(`${at} new: no page overflow`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `new-task-untrusted-${width}.png`) })
      const before = posts.length
      await newCard.getByRole('button', { name: 'Trust folder' }).click()
      const alert = newCard.getByRole('alert')
      await alert.waitFor()
      check(`${at} new: error is shown`, (await alert.innerText()).includes('changed while Tether wrote it'))
      check(`${at} new: button usable again after an error`, await newCard.getByRole('button', { name: 'Trust folder' }).isEnabled())
      await page.screenshot({ path: path.join(SHOTS, `new-task-error-${width}.png`) })
      await newCard.getByRole('button', { name: 'Trust folder' }).click()
      await dialog.getByText('The new Codex tab can edit files here.').waitFor()
      const newSent = trustPosts(before)
      check(`${at} new: binds the source tab`, newSent.length >= 2 && newSent.every(post => post.body.mode === 'new' && post.body.session === '%2' && post.body.target === TARGET))
      await page.screenshot({ path: path.join(SHOTS, `new-task-trusted-${width}.png`) })
      await page.keyboard.press('Escape')
    }
    check('no page errors', pageErrors.length === 0, pageErrors.join('\n'))
    check('no requests outside the fixtures', unexpected.size === 0, [...unexpected].join(', '))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`); if (pageErrors.length) console.log(pageErrors.join('\n'))
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
  } finally {
    if (page) await page.close().catch(() => {})
    if (server) server.close()
  }
  console.log(failed ? `${failed} failed` : 'all passed')
  process.exit(failed ? 1 : 0)
}

main()
