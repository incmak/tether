// Undo and redo (Undo.tsx): the toast after a step, ⌘Z / ⌘⇧Z (Ctrl off a Mac) with real key presses, the keys staying
// with text fields, the confirm dialog for critical steps, refusals, the rail and phone Undo buttons, and History.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-15/undo/shots')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const fixture = {
  sessions: { sessions: [pane('%1', 'Deploy fix'), pane('%2', 'Docs pass')], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

// A small model of server/undo.py: entries newest last.
const now = () => Date.now() / 1000
let entries, posts, armed = false
function reset() {
  posts = []
  entries = [
    { id: 'u000000000001', at: now() - 600, who: { by: 'agent', tab: 'Helper' }, label: 'Started tab `Helper`', state: 'done', redo: false, undone_at: null, critical: true, why: null },
    { id: 'u000000000002', at: now() - 60, who: { by: 'user' }, label: 'Moved tab `Docs pass` to stack `API`', state: 'done', redo: false, undone_at: null, why: null },
  ]
}
const nextOf = direction => direction === 'undo'
  ? [...entries].reverse().find(entry => entry.state === 'done')
  : [...entries].filter(entry => entry.state === 'undone' && entry.redo).sort((a, b) => b.undone_at - a.undone_at)[0]
function history() {
  const undo = nextOf('undo'), redo = nextOf('redo')
  return { entries: [...entries].reverse().map(({ critical, refuse, ...entry }) => ({ ...entry, why: refuse || null })), next_undo: undo?.id ?? null, next_redo: redo?.id ?? null, next_undo_label: undo?.label ?? null, next_redo_label: redo?.label ?? null }
}
let effectsVersion = 1  // the server's confirm token changes when the effects change
const tokenOf = entry => `${entry.id}-v${effectsVersion}`
function runUndo(body) {
  posts.push(body)
  const entry = body.id ? entries.find(item => item.id === body.id) : nextOf(body.action)
  if (!entry) return { status: 409, json: { error: body.action === 'undo' ? 'Nothing to undo.' : 'Nothing to redo.' } }
  if (entry.refuse) return { status: 409, json: { error: entry.refuse } }
  if (entry.critical && body.confirmed !== tokenOf(entry)) return { status: 200, json: { status: 'confirm', id: entry.id, label: entry.label, direction: body.action, token: tokenOf(entry), effects: ['Closes tab `Helper`. Claude in it stops.', 'It is working now. The running turn stops and its unfinished work is lost.'] } }
  if (body.action === 'undo') Object.assign(entry, { state: 'undone', redo: true, undone_at: now() })
  else Object.assign(entry, { state: 'done', redo: false, undone_at: null })
  return { status: 200, json: { status: 'done', id: entry.id, label: entry.label, direction: body.action } }
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
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
    page.on('console', message => { if (message.type() === 'error' && !/Failed to load resource/.test(message.text())) pageErrors.push(message.text().slice(0, 600)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    let prefsVersion = 1
    await page.route('**/*', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'undo' && request.method() === 'GET') return route.fulfill({ json: history() })
        if (endpoint === 'undo') { const answer = runUndo(request.postDataJSON()); return route.fulfill({ status: answer.status, json: answer.json }) }
        if (endpoint === 'prefs' && request.method() === 'POST' && armed) {
          armed = false
          // A reorder the server recorded: it names the step in the answer, as undo_api.headers does.
          const step = { id: 'u000000000003', at: now(), who: { by: 'user' }, label: 'Reordered tabs `a` & “b”', state: 'done', redo: false, undone_at: null, why: null }
          if (!entries.some(entry => entry.id === step.id)) entries.push(step)
          return route.fulfill({ json: { prefs: {}, version: ++prefsVersion }, headers: { 'X-Tether-Undo': step.id, 'X-Tether-Undo-Label': encodeURIComponent(step.label) } })
        }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: prefsVersion } })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      // The shared browser may not reach this machine's ports: serve the build from here.
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try { return await route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
    })
    const mac = await page.evaluate(() => /Mac|iPhone|iPad/.test(navigator.platform)).catch(() => false)
    const mod = mac ? 'Meta' : 'Control'
    const toast = page.locator('.undo-toast')
    const region = page.locator('.undo-toast-region')
    const open = async (width, height) => {
      reset()
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      await page.locator('form.composer textarea').first().waitFor({ state: 'visible' })
      await page.waitForTimeout(300)
    }
    const away = async () => { await page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur()) }

    // Desktop.
    await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' })
    await open(1280, 900)
    check('toast region is a polite live region', await region.getAttribute('aria-live') === 'polite' && await region.getAttribute('role') === 'status')
    const rail = page.locator('.app-rail [aria-label^="Undo"]')
    check('rail shows Undo with the next step', (await rail.getAttribute('aria-label')) === 'Undo: Moved tab `Docs pass` to stack `API`', await rail.getAttribute('aria-label'))

    // A step from a request: typing saves a draft (prefs), whose answer names a step. The toast offers Undo.
    const box = page.locator('form.composer textarea').first()
    armed = true
    await box.click()
    await page.keyboard.type('half a prompt')
    await toast.waitFor({ state: 'visible' })
    check('a recorded step shows its toast with Undo', (await toast.textContent()).includes('Reordered tabs `a` & “b”') && await toast.getByRole('button', { name: 'Undo', exact: true }).count() === 1, await toast.textContent())

    // In the composer, the key undoes text, not a Tether step.
    await page.keyboard.press(`${mod}+z`)
    await page.waitForTimeout(300)
    check('⌘Z in the composer leaves Tether steps alone', posts.length === 0, JSON.stringify(posts))

    // Out of the text field, ⌘Z undoes the newest step at once (not critical) and offers Redo.
    await away()
    await page.keyboard.press(`${mod}+z`)
    await page.waitForFunction(() => document.querySelector('.undo-toast')?.textContent?.includes('Undid'))
    check('⌘Z undoes the newest step', posts.at(-1)?.action === 'undo' && !posts.at(-1)?.id && entries.find(entry => entry.id === 'u000000000003').state === 'undone', JSON.stringify(posts))
    check('the toast says Undid and offers Redo', (await toast.textContent()).includes('Undid: Reordered tabs') && await toast.getByRole('button', { name: 'Redo', exact: true }).count() === 1)
    await page.keyboard.press(`${mod}+Shift+z`)
    await page.waitForFunction(() => document.querySelector('.undo-toast')?.textContent?.includes('Redid'))
    check('⌘⇧Z redoes it', posts.at(-1)?.action === 'redo' && entries.find(entry => entry.id === 'u000000000003').state === 'done')

    // The toast's own Undo button runs that exact step.
    await toast.getByRole('button', { name: 'Undo', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.undo-toast')?.textContent?.includes('Undid'))
    check('the toast Undo targets its step', posts.at(-1)?.id === 'u000000000003', JSON.stringify(posts.at(-1)))

    // A critical step asks first, names the effect, and keeps focus inside until closed.
    entries = entries.filter(entry => entry.id !== 'u000000000003')
    entries.find(entry => entry.id === 'u000000000002').state = 'undone'
    const before = posts.length
    await away()
    await page.keyboard.press(`${mod}+z`)
    const dialog = page.locator('dialog.undo-confirm')
    await dialog.waitFor({ state: 'visible' })
    check('critical step opens an alert dialog', await dialog.getAttribute('role') === 'alertdialog' && (await dialog.locator('h2').textContent()).includes('Undo “Started tab `Helper`”?'))
    check('the dialog names the effects', (await dialog.textContent()).includes('Claude in it stops') && (await dialog.textContent()).includes('unfinished work is lost'))
    check('focus starts on Cancel', await page.evaluate(() => document.activeElement?.textContent) === 'Cancel')
    for (let i = 0; i < 4; i++) await page.keyboard.press('Tab')
    check('focus stays in the dialog', await page.evaluate(() => !!document.activeElement?.closest('dialog.undo-confirm')))
    await page.keyboard.press(`${mod}+z`)
    check('⌘Z inside the dialog does nothing more', posts.length === before + 1)
    await page.keyboard.press('Escape')
    await dialog.waitFor({ state: 'detached' })
    check('Escape cancels without running', entries[0].state === 'done' && posts.length === before + 1)
    await page.screenshot({ path: path.join(SHOTS, 'undo-desktop-toast.png') })
    await page.keyboard.press(`${mod}+z`)
    await dialog.waitFor({ state: 'visible' })
    await page.screenshot({ path: path.join(SHOTS, 'undo-desktop-confirm.png') })
    effectsVersion++  // the effects changed while the dialog was open: the yes no longer matches
    await dialog.getByRole('button', { name: 'Undo', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('dialog.undo-confirm .undo-changed'))
    check('a yes to old effects asks again and says so', entries[0].state === 'done' && typeof posts.at(-1)?.confirmed === 'string')
    await dialog.getByRole('button', { name: 'Undo', exact: true }).click()
    await dialog.waitFor({ state: 'detached' })
    check('Undo in the dialog sends the token of the effects shown', posts.at(-1)?.confirmed === tokenOf(entries[0]) && entries[0].state === 'undone', JSON.stringify(posts.at(-1)))

    // Nothing left: a refusal, said in the toast.
    await away()
    await page.keyboard.press(`${mod}+z`)
    await page.waitForFunction(() => document.querySelector('.undo-toast')?.textContent?.includes('Nothing to undo'))
    check('a refusal shows its reason', true)
    const sent = posts.length
    await page.evaluate(() => { const box = document.createElement('div'); box.id = 'fake-confirm'; box.setAttribute('role', 'alertdialog'); document.body.append(box) })
    await page.keyboard.press(`${mod}+z`)
    await page.evaluate(() => document.getElementById('fake-confirm')?.remove())
    check('⌘Z does nothing while a confirm box (alertdialog) is open', posts.length === sent)

    // The rail Redo button, then History with a refused older step.
    await page.locator('.app-rail [aria-label^="Redo"]').click()
    await dialog.waitFor({ state: 'visible' })
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    Object.assign(entries[1], { state: 'done', refuse: 'Tab `Docs pass` moved to another stack since.' })
    await page.locator('.sidebar [aria-label="Undo history"]').first().click()
    const sheet = page.locator('dialog.undo-history')
    await sheet.waitFor({ state: 'visible' })
    await sheet.locator('.undo-list li').first().waitFor()
    const rows = await sheet.locator('.undo-list li').allTextContents()
    check('History lists steps newest first with who', rows.length === 2 && rows[0].includes('Docs pass') && rows[1].includes('Agent Helper'), rows.join(' | '))
    check('a refused step says why and its button is off', rows[0].includes('moved to another stack since') && await sheet.locator('.undo-list li').first().getByRole('button').isDisabled())
    await page.screenshot({ path: path.join(SHOTS, 'undo-desktop-history.png') })
    await page.keyboard.press('Escape')
    await sheet.waitFor({ state: 'detached' })

    // The shortcuts sheet lists the keys.
    await away()
    await page.keyboard.press(`${mod}+/`)
    const shortcuts = await page.locator('dialog.shortcuts-sheet').textContent()
    check('shortcuts sheet lists undo and redo', shortcuts.includes('Undo the last tab, session or stack step') && shortcuts.includes('Redo it'))
    await page.keyboard.press('Escape')

    // Phone, 390 and 320 wide, dark: the tab bar Undo button.
    for (const [width, scheme] of [[390, 'dark'], [320, 'light']]) {
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' })
      await open(width, 740)
      const undoTab = page.locator('.phone-tabbar .phone-tab-undo')
      await page.waitForFunction(() => document.querySelector('.phone-tabbar .phone-tab-undo')?.getAttribute('aria-disabled') === 'false')
      const size = await undoTab.boundingBox()
      check(`${width}px: tab bar Undo is at least 24 by 24`, size && size.width >= 24 && size.height >= 24, JSON.stringify(size))
      check(`${width}px: no horizontal page scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      const count = posts.length
      await undoTab.tap().catch(() => undoTab.click())
      await page.waitForFunction(() => document.querySelector('.undo-toast')?.textContent?.includes('Undid'))
      check(`${width}px: tapping Undo undoes the newest step`, posts.length === count + 1 && entries[1].state === 'undone')
      const box2 = await toast.boundingBox()
      check(`${width}px: the toast fits the screen`, box2 && box2.x >= 0 && box2.x + box2.width <= width, JSON.stringify(box2))
      await page.screenshot({ path: path.join(SHOTS, `undo-phone-${width}-${scheme}.png`) })
      await toast.getByRole('button', { name: 'Undo history' }).click()
      await page.locator('dialog.undo-history .undo-list li').first().waitFor()
      check(`${width}px: History opens from the toast`, await page.locator('dialog.undo-history .undo-list li').count() === 2)
      await page.screenshot({ path: path.join(SHOTS, `undo-phone-${width}-history.png`) })
      await page.locator('dialog.undo-history').getByRole('button', { name: 'Close' }).click()
    }
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`undo: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
