// Drag files from the desktop onto a chat (chatDrop.ts): the panel shows a drop target, the composer uploads the files
// and shows their chips. A terminal tab refuses with a reason. A drop outside a chat, a text drag and a drag that starts
// on the page never upload and never open the file in the tab. Build ui/dist (or set DIST) first.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const SHOTS = shotsDir('chat-drop')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const chat = (session, agent = 'claude') => ({ messages: [], busy: false, thread: `t${session.slice(1)}`, agent, writable: true, session, earlier: 0, limit: 100, state: 'Idle', approval: null })
const fixture = {
  sessions: { sessions: [pane('%1', 'Deploy fix'), pane('%2', 'Docs pass'), pane('%3', 'Build shell', { agent: 'shell' })], default_thread: 't1' },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], uploads = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer((req, res) => { res.writeHead(404); res.end() })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    // The shared browser runs the fixture checks. CHAT_DROP_LOCAL=1 uses local headless Chromium instead and adds a
    // real drag (CDP) with a file from this host's disk, which the shared browser's container cannot reach.
    const local = process.env.CHAT_DROP_LOCAL === '1'
    browser = await getBrowser({ local })
    const context = local ? await browser.newContext() : browser.contexts()[0]
    if (!context || (!local && context.pages().length >= 3)) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    page.on('console', message => { if (message.type() === 'error' && !/Failed to load resource|EventSource's response has a MIME type/.test(message.text())) pageErrors.push(message.text().slice(0, 600)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint === 'upload') {
          const headers = request.headers(), name = decodeURIComponent(headers['x-filename'])
          uploads.push({ name, session: headers['x-session'], thread: headers['x-thread'], body: request.postData() })
          return route.fulfill({ json: { path: `/srv/uploads/${name}`, name, size: 2 } })
        }
        if (endpoint.startsWith('chat')) { const id = url.searchParams.get('session') || '%1'; return route.fulfill({ json: chat(id, id === '%3' ? 'shell' : 'claude') }) }
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (endpoint.startsWith('files')) return route.fulfill({ json: { path: '/srv/project', entries: [] } })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
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
    const open = async (width, height, session) => {
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/?session=${encodeURIComponent(session)}`, { waitUntil: 'load' })
      await page.locator('.chat-panel .composer textarea:not([disabled])').first().waitFor({ state: 'visible' })
      await page.waitForTimeout(300)
    }
    // Fire one drag step at the element that matches selector, with files (name, type, text) or only text.
    // Returns whether the page cancelled the event, which is what stops the browser from opening the file.
    const drag = (selector, type, files, text, x = 200, y = 200) => page.evaluate(({ selector, type, files, text, x, y }) => {
      const target = document.querySelector(selector)
      if (!target) throw new Error(`No ${selector}`)
      const data = new DataTransfer()
      for (const [name, kind, body] of files) data.items.add(new File([body], name, { type: kind }))
      if (text) data.setData('text/plain', text)
      const event = new DragEvent(type, { dataTransfer: data, bubbles: true, cancelable: true, clientX: x, clientY: y })
      target.dispatchEvent(event)
      return { cancelled: event.defaultPrevented, effect: data.dropEffect }
    }, { selector, type, files, text, x, y })
    const overlay = page.locator('.chat-drop')
    const chips = page.locator('.composer .attachments .chip')
    const NOTES = [['notes.txt', 'text/plain', 'hi']]

    for (const [width, height, scheme] of [[1280, 800, 'light'], [320, 640, 'dark']]) {
      await page.emulateMedia({ colorScheme: scheme })
      uploads.length = 0
      await open(width, height, '%1')
      const at = `${width}px`
      const href = page.url()

      // Hover: the drop target names the chat. The drop: one upload for this pane and thread, then a chip.
      let result = await drag('.chat-panel .chat-scroll', 'dragover', NOTES)
      check(`${at} dragover is taken`, result.cancelled, JSON.stringify(result))
      await overlay.waitFor({ state: 'visible' })
      check(`${at} drop target names the chat`, /Drop to attach to Deploy fix/.test(await overlay.textContent()), await overlay.textContent())
      const box = await overlay.boundingBox(), panel = await page.locator('.chat-panel').first().boundingBox()
      check(`${at} drop target stays inside the chat panel`, box && panel && box.x >= panel.x && box.x + box.width <= panel.x + panel.width + 0.5 && box.width <= width, JSON.stringify({ box, panel }))
      await page.screenshot({ path: path.join(SHOTS, `drop-${width}-${scheme}.png`) })
      result = await drag('.chat-panel .chat-scroll', 'drop', NOTES)
      check(`${at} drop is taken`, result.cancelled)
      await chips.first().waitFor({ state: 'visible' })
      check(`${at} drop target hides after the drop`, !(await overlay.count()))
      check(`${at} one upload to this pane and thread`, uploads.length === 1 && uploads[0].name === 'notes.txt' && uploads[0].session === '%1' && uploads[0].thread === 't1' && uploads[0].body === 'hi', JSON.stringify(uploads))
      check(`${at} chip shows the file`, (await chips.allTextContents()).join('|').includes('notes.txt'))

      // Several files in one drop, on the message box itself: all upload, in order. The box gets no file path text.
      await drag('.composer textarea', 'dragover', [])
      result = await drag('.composer textarea', 'drop', [['spec.pdf', 'application/pdf', '%PDF'], ['data.csv', 'text/csv', 'a,b']])
      check(`${at} drop on the message box is taken`, result.cancelled)
      await page.waitForFunction(() => document.querySelectorAll('.composer .attachments .chip').length === 3)
      check(`${at} files upload in drop order`, uploads.map(u => u.name).join(',') === 'notes.txt,spec.pdf,data.csv', uploads.map(u => u.name).join(','))
      check(`${at} message box text is unchanged`, (await page.locator('.composer textarea').inputValue()) === '')
      await page.screenshot({ path: path.join(SHOTS, `chips-${width}-${scheme}.png`) })

      // A text drag is not a file drop: no target, and the page leaves it to the browser (text goes into the box).
      result = await drag('.chat-panel .chat-scroll', 'dragover', [], 'plain words')
      check(`${at} text drag shows no drop target`, !result.cancelled && !(await overlay.count()))

      // A drag that starts on the page (an image, a tab) is marked at dragstart, so it is not a desktop file drop.
      const marked = await page.evaluate(() => { const data = new DataTransfer(); document.querySelector('.chat-header').dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: data })); data.items.add(new File(['x'], 'shot.png', { type: 'image/png' })); const over = new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: data }); document.querySelector('.chat-panel .chat-scroll').dispatchEvent(over); return { types: [...data.types], cancelled: over.defaultPrevented } })
      check(`${at} drag from the page shows no drop target`, marked.types.includes('application/x-tether-page-drag') && !marked.cancelled && !(await overlay.count()), JSON.stringify(marked))

      // A drag that stops with no dragleave (a cancelled drop) does not leave the target up.
      await drag('.chat-panel .chat-scroll', 'dragover', NOTES)
      await overlay.waitFor({ state: 'visible' })
      await page.waitForTimeout(700)
      check(`${at} target stays through a 700 ms dragover gap`, (await overlay.count()) === 1)
      await overlay.waitFor({ state: 'detached', timeout: 3000 }).then(() => check(`${at} target goes when dragover stops`, true), () => check(`${at} target goes when dragover stops`, false))

      // A drop zone inside the chat that takes the drag hides the chat's target.
      await page.evaluate(() => { const zone = document.createElement('div'); zone.id = 'nested-zone'; zone.style.cssText = 'position:absolute;inset:0'; zone.addEventListener('dragover', e => e.preventDefault()); zone.addEventListener('drop', e => e.preventDefault()); document.querySelector('.chat-panel .chat-scroll').append(zone) })
      await drag('.chat-panel .chat-scroll', 'dragover', NOTES)
      await overlay.waitFor({ state: 'visible' })
      await drag('#nested-zone', 'dragover', NOTES)
      check(`${at} nested drop zone hides the chat target`, !(await overlay.count()))
      const kept = uploads.length
      await drag('#nested-zone', 'drop', NOTES)
      check(`${at} nested drop zone keeps the files`, uploads.length === kept && !(await overlay.count()))
      await page.evaluate(() => document.getElementById('nested-zone')?.remove())

      // A file dropped outside any chat does not open in the tab and does not upload.
      const before = uploads.length
      result = await drag('body', 'drop', NOTES)
      check(`${at} drop outside a chat is cancelled`, result.cancelled)
      check(`${at} drop outside a chat does not upload`, uploads.length === before && page.url() === href)

      // A terminal tab refuses with a reason; the cursor shows no drop.
      await open(width, height, '%3')
      result = await drag('.chat-panel', 'dragover', NOTES)
      await overlay.waitFor({ state: 'visible' })
      check(`${at} terminal tab refuses with a reason`, /terminal tab does not take files/.test(await overlay.textContent()), await overlay.textContent())
      await page.screenshot({ path: path.join(SHOTS, `refused-${width}-${scheme}.png`) })
      // Leaving with no relatedTarget (Safari): a point inside keeps the target, a point outside the window hides it.
      const mid = await page.locator('.chat-panel').first().boundingBox()
      await drag('.chat-panel', 'dragover', NOTES)
      await drag('.chat-panel', 'dragleave', NOTES, '', mid.x + mid.width / 2, mid.y + mid.height / 2)
      check(`${at} dragleave inside the panel keeps the target`, (await overlay.count()) === 1)
      await drag('.chat-panel', 'dragleave', NOTES, '', -5, -5)
      check(`${at} drop target leaves with the drag`, !(await overlay.count()))
      result = await drag('.chat-panel', 'drop', NOTES)
      check(`${at} terminal tab drop uploads nothing`, uploads.length === before && result.cancelled)
    }
    if (local) {
    // A real drag from the browser's drag system (CDP), with a file from disk: the page sets the copy cursor, the drop
    // uploads the file's bytes, and a drop on the sidebar neither uploads nor opens the file in the tab.
    const real = path.join(SHOTS, 'real-drop.txt')
    await fs.writeFile(real, 'real bytes')
    await page.emulateMedia({ colorScheme: 'light' })
    uploads.length = 0
    await open(1280, 800, '%1')
    const cdp = await page.context().newCDPSession(page)
    const centre = async selector => { const b = await page.locator(selector).first().boundingBox(); return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) } }
    const data = { items: [], files: [real], dragOperationsMask: 1 }
    const step = (type, at) => cdp.send('Input.dispatchDragEvent', { type, x: at.x, y: at.y, data })
    let spot = await centre('.chat-panel .chat-scroll')
    await step('dragEnter', spot); await step('dragOver', spot)
    await overlay.waitFor({ state: 'visible' })
    check('real drag shows the drop target', /Drop to attach to Deploy fix/.test(await overlay.textContent()))
    await step('drop', spot)
    await chips.first().waitFor({ state: 'visible' })
    check('real drop uploads the file bytes', uploads.length === 1 && uploads[0].name === 'real-drop.txt' && uploads[0].body === 'real bytes', JSON.stringify(uploads))
    const href = page.url()
    spot = await centre('.sidebar')
    await step('dragEnter', spot); await step('dragOver', spot); await step('drop', spot)
    await page.waitForTimeout(500)
    check('real drop on the sidebar keeps Tether open', page.url() === href && uploads.length === 1, page.url())
    await cdp.detach()
    } else console.log('skip real CDP drag: set CHAT_DROP_LOCAL=1')
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } finally {
    await page?.close().catch(() => {})
    await browser?.close?.().catch?.(() => {})
    server?.close()
  }
  console.log(failed ? `chat drop UI: ${failed} failed` : 'chat drop UI: all checks pass')
  if (failed) process.exitCode = 1
}
main().catch(error => { console.error(error); process.exitCode = 1 })
