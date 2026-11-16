// Responsive chat surfaces, overlays and narrow panels. Build ui/dist before this fixture-only sweep.
// Every API request stays in this page's fixtures: no real tab closes.
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')
const os = require('node:os')
const esbuild = require('esbuild')
const { existsSync } = require('node:fs')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const pane = (id, name, session = '$1', extra = {}) => ({ id, tmux_session: session, label: `${session === '$1' ? 'project' : 'api'}:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const fixture = {
  sessions: { sessions: [pane('%1', 'Loose tab'), pane('%2', 'Login fix'), pane('%3', 'Login review', '$1', { state: 'Working' }), pane('%4', 'Docs pass'), pane('%5', 'Api server', '$2')], default_thread: 't2', groups: {} },
  chat: { messages: [], busy: false, thread: 't2', agent: 'claude', writable: true, session: '%2', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%2', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'suggest-name': { names: [], source: 'llm' },
  'groups/create': { group: { id: 'g00000009', name: 'Login', project: '' } },
}


const SHOTS = path.resolve(__dirname, '../.release-work/2026-11-16/responsive/chat/shots', (process.env.PHASE || 'after') + (process.env.COARSE ? '-touch' : ''))
const LONG = 'long_name_'.repeat(9).slice(0, 80)
const ALT = '界'.repeat(80)
const part = process.env.PART || 'all'
const checkOnly = process.env.PHASE === 'before'
const viewports = process.env.COARSE ? [[320, 640], [390, 844], [740, 360]] : [[320, 640], [390, 844], [740, 360], [768, 1024], [1024, 768], [1440, 900]]
fixture.sessions.sessions = Array.from({ length: 123 }, (_, i) => pane(`%${i + 1}`, `${i ? LONG : ALT}${i}`))
fixture.sessions.default_thread = 't2'
fixture.chat.messages = [
  { id: 'u1', role: 'user', text: LONG },
  { id: 'a1', role: 'assistant', text: `# Response\n\n${LONG}\n\n\`\`\`${LONG}\n${LONG.repeat(5)}\n\`\`\`\n\n| File | Result |\n| --- | --- |\n| ${LONG} | ${ALT} |\n| ${ALT} | \`${LONG}\` |\n\n![Wide image](/mobile/api/test-image)` },
  { id: 'tool1', role: 'assistant', kind: 'actions', text: LONG, count: 123, tools: [{ id: 't1', name: 'Read', title: LONG, description: ALT, is_error: false, status: 'complete' }] },
  { id: 'notice', role: 'assistant', kind: 'notice', title: LONG, text: LONG.repeat(4) },
]
fixture.catalog.commands = [{ name: 'help', description: LONG, kind: 'builtin' }]
fixture.files = { files: [LONG + '.txt', ALT + '.md'] }
fixture['model-options'] = { agent: 'claude', models: [{ id: 'opus', label: LONG, current: true }, { id: 'other', label: ALT, current: false }], efforts: ['low', 'medium', 'high'], current_effort: 'high' }
fixture['tool'] = { id: 't1', name: 'Read', title: LONG, is_error: false, status: 'complete', detail: LONG.repeat(10), format: 'text', truncated: false }
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="600"><rect width="1200" height="600" fill="#607d8b"/><circle cx="600" cy="300" r="200" fill="#efc56e"/></svg>'

async function main() {
  let browser, server, temp, context
  let failures = 0
  const errors = []
  const check = (name, ok, why = '') => { console.log(`${ok ? 'ok' : 'FAIL'} ${name}${why ? ': ' + why : ''}`); if (!ok) failures++ }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    // Bundle only the test entry. All product CSS comes from the production Vite build.
    temp = await fs.mkdtemp(path.join(os.tmpdir(), 'responsive-chat-'))
    const entry = path.join(__dirname, 'responsive_chat/fixture.tsx')
    const hasHarness = await fs.stat(entry).then(() => true, () => false)
    if (!hasHarness) throw new Error('Missing responsive_chat/fixture.tsx')
    if (hasHarness) await esbuild.build({ entryPoints: [entry], bundle: true, minify: true, outfile: path.join(temp, 'fixture.js'), format: 'esm', plugins: [{ name: 'exact-case', setup(build) { build.onResolve({ filter: /^\./ }, args => { for (const ext of ['.ts', '.tsx']) { const file = path.resolve(args.resolveDir, args.path + ext); if (existsSync(file)) return { path: file } } }) } }], jsx: 'automatic', loader: { '.css': 'empty' }, define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'silent' })
    const entryHtml = await fs.readFile(path.join(DIST, 'index.html'), 'utf8')
    const primaryCss = [...entryHtml.matchAll(/href="([^\"]+\.css)"/g)].map(m => m[1])
    const css = primaryCss
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://localhost')
      if (url.pathname === '/chat/fixture-frame.html') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html><body>Fixture preview</body></html>'); return }
      if (url.pathname === '/fixture') { res.setHeader('Content-Type', 'text/html'); res.end(`<!doctype html><html><head>${css.map(h => `<link rel="stylesheet" href="${h}">`).join('')}</head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>`); return }
      const file = url.pathname === '/fixture.js' ? path.join(temp, 'fixture.js') : url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (url.pathname !== '/fixture.js' && (!url.pathname.startsWith('/chat/') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep)))) { res.writeHead(404); res.end(); return }
      try { res.setHeader('Content-Type', MIME[path.extname(file)] || 'application/octet-stream'); res.end(await fs.readFile(file)) } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    let page
    const startBrowser = async () => {
    if (!browser) browser = await getBrowser({ local: true })
    // Playwright1.56's serviceWorkers:block init script throws in opaque preview frames.
    // The guarded bootstrap below and the /sw.js404 route disable registration instead.
    context = await browser.newContext({ reducedMotion: 'reduce', hasTouch: !!process.env.COARSE })
    page = await context.newPage()
    page.setDefaultTimeout(7000)
    page.on('pageerror', e => errors.push(e.stack))
    await context.routeWebSocket('**/*', socket => socket.close())
    await context.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length).split('?')[0]
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint === 'test-image') return route.fulfill({ contentType: 'image/svg+xml', body: SVG })
        if (endpoint === 'upload') return route.fulfill({ json: { path: '/uploaded/file.txt', name: LONG + '.txt' } })
        if (endpoint === 'message') return route.fulfill({ status: 400, json: { error: LONG.repeat(10), draft: true } })
        if (endpoint === 'chat') return route.fulfill({ json: fixture.chat })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin) return route.continue()
      return route.abort('blockedbyclient')
    })
    await context.addInitScript(() => {
      if (window.top !== window || !['/chat/', '/fixture'].includes(location.pathname)) return
      // Opaque preview frames deliberately deny service workers and storage. This is test bootstrap only.
      try { navigator.serviceWorker.register = () => Promise.reject(new Error('fixture')) } catch { /* sandboxed frame */ }
      try {
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, mainModel: 'none', sideMode: 'sessions' }))
      } catch { /* sandboxed frame */ }
    })
    }
    await startBrowser()
    const shot = async name => { const options = { path: path.join(SHOTS, `${name}.png`) }; try { await page.screenshot(options) } catch { await page.waitForTimeout(100); await page.screenshot(options) } }
    
    const audit = async (name, selector, { scroll = true, bounds = true } = {}) => {
      check(`${name}: rendered`, await page.locator(selector).count() > 0)
      const bad = await page.locator(selector).evaluateAll((roots, opts) => roots.flatMap(root => {
        const r = root.getBoundingClientRect(), out = []
        if (!r.width || !r.height) return out
        if (opts.bounds && (r.left < -1 || r.right > innerWidth + 1 || r.top < -1 || r.bottom > innerHeight + 1)) out.push(`${root.className} outside viewport [${r.x},${r.y},${r.width},${r.height}]`)
        const style = getComputedStyle(root)
        const ellipsis = /^(block|inline-block|list-item|flow-root|table-cell)$/.test(style.display) && style.textOverflow === 'ellipsis' && style.overflowX === 'hidden' && style.whiteSpace === 'nowrap'
        if (opts.scroll && !ellipsis && root.scrollWidth > root.clientWidth + 1) out.push(`${root.className} overflows ${root.scrollWidth}/${root.clientWidth}`)
        return out
      }), { scroll, bounds })
      check(name, bad.length === 0, bad.join('; '))
    }
    const renew = async (restart = false) => {
      if (restart) { await context.close(); await startBrowser() }
      else { await page.close(); page = await context.newPage(); page.setDefaultTimeout(7000); page.on('pageerror', e => errors.push(e.stack)) }
    }
    const controls = async (name, selector) => {
      const bad = await page.locator(selector).evaluateAll(roots => roots.flatMap(root => {
        const buttons = [...root.querySelectorAll('button, summary, [role="tab"]')].filter(el => {
          const box = el.getBoundingClientRect()
          return box.width && box.height && getComputedStyle(el).visibility !== 'hidden' && !el.classList.contains('tab-close')
        })
        const out = []
        for (const el of buttons) {
          const r = el.getBoundingClientRect()
          if (r.width < 23.5 || r.height < 23.5) out.push(`${el.className} target ${r.width}x${r.height}`)
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
          let node
          while ((node = walker.nextNode())) {
            if (!node.textContent.trim()) continue
            let parent = node.parentElement, shortened = false
            let container = parent
            while (container && /^(inline|contents)$/.test(getComputedStyle(container).display)) container = container.parentElement
            while (parent && el.contains(parent)) {
              const style = getComputedStyle(parent)
              if ((parent === container && /^(block|inline-block|list-item|flow-root|table-cell)$/.test(style.display) && style.textOverflow === 'ellipsis' && style.overflowX === 'hidden' && style.whiteSpace === 'nowrap') || Number(style.webkitLineClamp) > 0) shortened = true
              if (parent === el) break
              parent = parent.parentElement
            }
            if (shortened) continue
            const range = document.createRange(); range.selectNodeContents(node)
            if ([...range.getClientRects()].some(text => text.left < r.left - 1 || text.right > r.right + 1)) out.push(`${el.className} clips text ${node.textContent.slice(0, 30)}`)
          }
        }
        buttons.forEach((a, i) => buttons.slice(i + 1).forEach(b => {
          if (a.contains(b) || b.contains(a)) return
          const visible = el => {
            const rect = el.getBoundingClientRect(), r = { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom }
            for (let parent = el.parentElement; parent; parent = parent.parentElement) {
              const style = getComputedStyle(parent), clip = parent.getBoundingClientRect()
              if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) { r.left = Math.max(r.left, clip.left); r.right = Math.min(r.right, clip.right) }
              if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) { r.top = Math.max(r.top, clip.top); r.bottom = Math.min(r.bottom, clip.bottom) }
            }
            return r
          }
          const x = visible(a), y = visible(b)
          if (Math.min(x.right, y.right) - Math.max(x.left, y.left) > 1 && Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top) > 1) out.push(`${a.className} overlaps ${b.className}`)
        }))
        return out
      }))
      check(name, bad.length === 0, bad.join('; '))
    }
    const shortChat = async name => {
      const result = await page.locator('.chat-panel').first().evaluate(panel => {
        const thread = panel.querySelector('.thread'), input = panel.querySelector('.composer textarea'), send = panel.querySelector('.send-button'), tabs = document.querySelector('.phone-tabbar')
        const floor = tabs && tabs.getBoundingClientRect().height ? tabs.getBoundingClientRect().top : innerHeight
        const reachable = el => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= floor + 1 && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }
        return { room: thread.clientHeight >= panel.clientHeight * .25 - 1, input: reachable(input), send: reachable(send), inputHeight: input.clientHeight }
      })
      check(`${name}: messages retain a quarter of the chat`, result.room)
      check(`${name}: textarea and Send stay reachable`, result.input && result.send)
    }
    const open = async (w, h) => {
      await renew(true)
      await page.setViewportSize({ width: w, height: h })
      await page.goto(`${origin}/chat/?session=%252`)
      await page.locator('.composer textarea').first().waitFor()
      await page.waitForTimeout(200)
    }
    for (const [w, h] of ['all', 'app'].includes(part) ? viewports : []) {
      await open(w, h)
      if (w === 740) {
        const originalName = fixture.sessions.sessions[1].pane_name
        fixture.sessions.sessions[1].pane_name = ALT
        await open(w, h)
        check('740: second long placeholder stays one row', await page.locator('.composer textarea').first().evaluate(el => el.clientHeight <= 30))
        await shortChat('740 Unicode name')
        await shot('740-empty-unicode')
        fixture.sessions.sessions[1].pane_name = originalName
        await open(w, h)
      }
      await audit(`${w}: page fits`, 'html')
      check(`${w}: empty input ignores long placeholder`, await page.locator('.composer textarea').first().evaluate(el => el.clientHeight <= 30))
      if (h <= 500) await shortChat(`${w} empty`)
      await audit(`${w}: composer controls fit`, '.composer-controls', { bounds: false })
      if (w >= 390) check(`${w}: mode label has room`, await page.locator('.mode-container .pill-text').first().evaluate(el => el.scrollWidth <= el.clientWidth + 1))
      // Scrollable code and tables retain their own scrollbars; their outer boxes stay in the message column.
      await page.locator('.chat-scroll').first().evaluate(el => { el.scrollTop = 0 })
      await page.locator('.code-header').first().scrollIntoViewIfNeeded()
      await audit(`${w}: code header fits`, '.code-header', { bounds: false })
      check(`${w}: code language stays one line`, await page.locator('.code-header > span').first().evaluate(el => el.offsetHeight <= 24))
      await shot(`${w}-code`)
      await page.locator('.assistant-message table').first().scrollIntoViewIfNeeded()
      check(`${w}: table values stay in their cells`, await page.locator('.assistant-message table td, .assistant-message table th').evaluateAll(cells => cells.every(cell => cell.scrollWidth <= cell.clientWidth + 1)))
      await shot(`${w}-table`)
      await page.locator('.assistant-message table').first().evaluate(el => { el.scrollLeft = el.scrollWidth })
      check(`${w}: last table column can be read`, await page.locator('.assistant-message table').first().evaluate(el => { const r = el.getBoundingClientRect(), last = el.querySelector('tbody tr:last-child td:last-child').getBoundingClientRect(); return last.right <= r.right + 1 && last.left >= r.left - 1 }))
      await shot(`${w}-table-right`)
      await page.locator('.tool-toggle').first().scrollIntoViewIfNeeded()
      await page.locator('.tool-toggle').first().click()
      await shot(`${w}-tools`)
      await page.locator('.assistant-message img').first().scrollIntoViewIfNeeded()
      await shot(`${w}-message-image`)
      await audit(`${w}: notice summary fits`, '.notice-row summary', { bounds: false })
      await controls(`${w}: chat controls have room`, '.chat-header, .composer')
      await shot(`${w}-chat`)
      const keys = page.locator('.keys-container > button').first()
      await keys.click()
      await page.waitForTimeout(220)
      await audit(`${w}: keys menu fits`, '.keys-menu')
      await shot(`${w}-keys`)
      await page.keyboard.press('Escape')
      await page.getByRole('button', { name: 'Model and effort', exact: true }).click()
      await page.locator('.model-list').waitFor()
      await audit(`${w}: model picker fits`, '.model-sheet')
      await audit(`${w}: model list fits`, '.model-list', { bounds: false })
      await controls(`${w}: model options fit`, '.model-list')
      await shot(`${w}-models`)
      await page.keyboard.press('Escape')
      const mode = page.getByRole('button', { name: /^Mode:/ }).first()
      await mode.click()
      await audit(`${w}: mode menu fits`, '.mode-menu')
      await shot(`${w}-mode`)
      check(`${w}: last mode option is reachable`, await page.locator('.mode-menu button').last().evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
      if (checkOnly) { await page.locator('.mode-menu [aria-checked=true]').focus(); await page.keyboard.press('Enter') } else await page.locator('.mode-menu [aria-checked=true]').click()
      for (const trigger of ['/', '@']) {
        await page.locator('.composer textarea').first().fill(trigger)
        await page.locator('.suggest').waitFor()
        await audit(`${w}: ${trigger} suggestions fit`, '.suggest')
        check(`${w}: ${trigger} suggestion is reachable`, await page.locator('.suggest button').first().evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
        await shot(`${w}-${trigger === '/' ? 'commands' : 'mentions'}`)
        await page.locator('.composer textarea').first().fill('')
      }
      await page.locator('.composer input[type=file]').first().setInputFiles({ name: LONG + '.txt', mimeType: 'text/plain', buffer: Buffer.from('fixture attachment') })
      await page.getByRole('button', { name: 'Remove ' + LONG + '.txt', exact: true }).waitFor()
      await audit(`${w}: attached filename fits`, '.attachments .chip', { bounds: false })
      await shot(`${w}-attachment`)
      await page.getByRole('button', { name: 'Remove ' + LONG + '.txt', exact: true }).click()
      await page.locator('.composer .attach').first().click()
      await page.waitForTimeout(220)
      await audit(`${w}: attach menu fits`, '.attach-menu')
      await shot(`${w}-attach`)
      await page.locator('.attach-menu button').first().focus()
      await page.keyboard.press('Escape')
      // Multi-selection uses the real tab controls. Scrolling the strip is intentional.
      const tabs = page.locator('.pane-tabs [role=tab]')
      await tabs.nth(2).click({ modifiers: ['ControlOrMeta'] })
      await audit(`${w}: selection bar fits`, '.tab-select-bar')
      await controls(`${w}: selection controls have room`, '.tab-select-bar')
      await shot(`${w}-selection`)
      await tabs.last().click({ modifiers: ['Shift'] })
      await audit(`${w}: three-digit selection fits`, '.tab-select-bar')
      await shot(`${w}-selection-many`)
      await page.keyboard.press('Escape')
      // A second worst-case input exercises the same chip rule as a long file name.
      await page.locator('.composer textarea').first().evaluate((el, text) => {
        const data = new DataTransfer(); data.setData('text/plain', text)
        el.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: data }))
      }, `${LONG}\n`.repeat(30))
      await audit(`${w}: collapsed paste fits`, '.paste-chip', { bounds: false })
      await shot(`${w}-paste`)
      if (h <= 500) await shortChat(`${w} paste closed`)
      check(`${w}: paste toggle is reachable`, await page.locator('.paste-toggle').evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
      if (checkOnly) { await page.locator('.paste-toggle').focus(); await page.keyboard.press('Enter') } else await page.locator('.paste-toggle').click()
      await audit(`${w}: expanded paste fits`, '.paste-chip', { bounds: false })
      await shot(`${w}-paste-open`)
      if (h <= 500) { await shortChat(`${w} paste open`); await page.locator('.composer textarea').first().fill(LONG.repeat(3) + '\n' + ALT); await shortChat(`${w} long draft`); await shot(`${w}-long-draft`) }
      if (checkOnly) { await page.getByRole('button', { name: 'Send message', exact: true }).focus(); await page.keyboard.press('Enter') } else await page.getByRole('button', { name: 'Send message', exact: true }).click()
      await page.locator('.composer-error').waitFor()
      await audit(`${w}: composer error fits`, '.composer-error', { bounds: false })
      await shot(`${w}-composer-error`)
      if (!checkOnly) { await page.waitForTimeout(50); await page.locator('.composer-error').focus(); await page.keyboard.press('End'); await page.waitForTimeout(250); check(`${w}: composer error scrolls by keyboard`, await page.locator('.composer-error').evaluate(el => el.scrollHeight <= el.clientHeight + 1 || el.scrollTop > 0)) }
      if (h <= 500) await shortChat(`${w} error`)
      // Also exercise the cards in the real chat status stack, where their available height differs.
      for (const kind of ['picker', 'menu', 'bridge']) {
        fixture.chat.attention = { id: `fixture-${kind}`, reason: kind === 'picker' ? 'picker' : 'menu', title: LONG, text: LONG, question: LONG,
          items: [{ text: LONG, selected: true }, { text: ALT, selected: false }],
          options: kind === 'menu' ? [{ id: 1, text: LONG, detail: ALT, selected: true }, { id: 2, text: ALT, detail: LONG }] : undefined,
          choice: { selection: 'multi', focus: 0, filter: LONG, tabs: [{ label: LONG, active: true }], items: [{ id: 1, text: LONG, detail: ALT, selected: true, checked: true }], more_above: 123, more_below: 99, hints: [] } }
        if (kind === 'bridge') fixture.chat.attention.askBridge = { id: 'live-ask', nonce: 'fixture', expires: Math.floor(Date.now() / 1000) + 3600, questions: [{ header: LONG, question: ALT, options: [{ label: LONG, description: ALT }, { label: ALT, description: LONG }] }] }
        await open(w, h)
        await page.locator('.status-content:not([hidden]) .choice-dock').waitFor()
        await audit(`${w}: live ${kind} dock fits`, '.status-stack')
        await controls(`${w}: status controls keep separate hit targets`, '.status-stack')
        if (process.env.COARSE && h <= 500) check(`${w}: status resize target remains44px`, await page.locator('.status-grip').evaluate(el => el.clientHeight >= 44 && el.clientWidth >= 44))
        check(`${w}: live ${kind} leaves Send reachable`, await page.locator('.send-button').first().evaluate(el => { const r = el.getBoundingClientRect(), tabs = document.querySelector('.phone-tabbar'), floor = tabs?.getBoundingClientRect().height ? tabs.getBoundingClientRect().top : innerHeight; return r.bottom <= floor + 1 && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
        const grip = page.locator('.status-grip')
        await grip.focus(); await page.keyboard.press('ArrowUp')
        check(`${w}: status resize key still works`, Number(await grip.getAttribute('aria-valuenow')) > 0)
        const chip = page.locator('.status-chip[aria-expanded=true]').first()
        check(`${w}: live ${kind} chip remains reachable`, await chip.evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
        check(`${w}: live ${kind} scrolls within the stack`, await page.locator('.status-stack').evaluate(el => getComputedStyle(el).overflowY === 'auto'))
        if (h <= 500) check(`${w}: live ${kind} draft cap is one line`, await page.locator('.composer textarea').first().evaluate(el => getComputedStyle(el).maxHeight === '28px'))
        if (kind === 'bridge') {
          await page.locator('.ask-form-option input').first().focus(); await page.keyboard.press('Space')
          await page.waitForFunction(() => !document.querySelector('.ask-form-actions button[type=submit]').disabled)
          for (let step = 0; step < 8 && !(await page.locator('.ask-form-actions button[type=submit]').evaluate(el => el === document.activeElement)); step++) await page.keyboard.press('Tab')
          check(`${w}: question Submit reachable by keyboard`, await page.locator('.ask-form-actions button[type=submit]').evaluate(el => { const r = el.getBoundingClientRect(); const stack = el.closest('.status-stack'), rail = stack.querySelector('.status-rail').getBoundingClientRect(); return el === document.activeElement && r.top >= rail.bottom - 1 && r.bottom <= stack.getBoundingClientRect().bottom + 1 && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
        }
        await shot(`${w}-live-${kind}`)
        delete fixture.chat.attention
      }
    }
    if (['all', 'app'].includes(part) && !process.env.COARSE) {
    // The desktop layout allows a chat panel to shrink below phone width.
    await open(1440, 900)
    await page.locator('.chat-panel').first().evaluate(el => { el.style.width = '180px'; el.style.flex = 'none' })
    check('180px: title has space', await page.locator('.chat-title h1').first().evaluate(el => el.clientWidth >= 40))
    check('180px: model label has space', await page.locator('.composer-pills .pill-text').first().evaluate(el => el.clientWidth >= 24))
    await audit('180px: header controls fit', '.chat-header', { bounds: false })
    await audit('180px: composer controls fit', '.composer-controls', { bounds: false })
    await controls('180px: chat controls have room', '.chat-header, .composer')
    await shot('180-chat')
    }
    if (hasHarness && ['all', 'overlays'].includes(part)) {
      for (const [w, h] of viewports) {
        await renew(true)
        for (const surface of ['choice', 'ask', 'deck', 'image', 'preview', 'notices', 'pins', 'toast', 'attention', 'picker', 'app-toast', 'pane-menu', 'pane-popover', 'session-menu', 'organize'].filter(surface => !process.env.SURFACE || process.env.SURFACE === surface)) {
          await renew(true)
          await page.setViewportSize({ width: w, height: h })
          await page.goto(`${origin}/fixture?surface=${surface}`)
          await page.waitForTimeout(250)
          // Surface-specific interactions are below; this catches overflow even within clipped app roots.
          // Check heading text separately: the Close button deliberately expands its44px hit area into the dialog padding.
          await audit(`${w}: ${surface} document fits`, 'html')
          const bodySelectors = { choice: '.choice-head, .choice-tabs, .choice-options button', ask: '.ask-form, .ask-form-question legend', deck: '.action-deck', image: '.viewer-head', preview: '.preview-head', notices: '.notice-card', pins: '.side-pin-bubble', toast: '[data-fixture-action="toast-tab"]', attention: '.waiting-box', picker: '.choice-head, .picker-card', 'app-toast': '.app-toast', 'pane-menu': '.sheet-heading h2, .sheet-sub, .action-list', 'pane-popover': '.sheet-heading h2, .sheet-sub, .action-list', 'session-menu': '.sheet-heading h2, .sheet-sub, .action-list', organize: '.organize-body' }
          await audit(`${w}: ${surface} content fits`, bodySelectors[surface], { bounds: !['choice', 'ask', 'notices', 'attention', 'picker', 'pane-menu', 'pane-popover', 'session-menu', 'organize'].includes(surface), scroll: surface !== 'pins' })
          await controls(`${w}: ${surface} controls have room`, '[data-fixture-surface]')
          if (surface === 'choice') check(`${w}: choice rows contain their text`, await page.locator('.choice-options button').evaluateAll(buttons => buttons.every(button => button.scrollHeight <= button.clientHeight + 1)))
          if (surface === 'image') {
            await page.getByRole('button', { name: 'Zoom in', exact: true }).click()
            await shot(`${w}-image-zoom`)
            await page.getByRole('button', { name: 'Zoom out', exact: true }).click()
            await page.keyboard.press('ArrowRight')
          }
          if (surface === 'deck') {
            await page.getByRole('button', { name: 'Handled', exact: true }).click()
            await audit(`${w}: deck error wraps`, '.deck-status', { bounds: false })
            await page.getByRole('button', { name: 'Skip', exact: true }).scrollIntoViewIfNeeded()
            await audit(`${w}: deck last action reachable`, '.deck-actions')
          }
          if (surface === 'notices') {
            await page.getByRole('button', { name: 'Show all', exact: true }).click()
            await audit(`${w}: expanded notice fits`, '.notice-card', { bounds: false })
          }
          if (surface === 'attention') {
            await page.locator('.oldest-button.compact').click()
            await page.locator('dialog.waiting-sheet[open]').waitFor()
            await audit(`${w}: waiting dialog fits`, 'dialog.waiting-sheet[open]')
            await shot(`${w}-waiting-dialog`)
            await page.keyboard.press('Escape')
          }
          if (surface === 'toast') {
            await page.locator('[data-fixture-action="toast-tab"]').click()
            await page.locator('.quick-toast').waitFor()
            await audit(`${w}: toast fits`, '.quick-toast')
          }
          if (surface === 'pins') {
            await page.locator('.side-pin-bubble').click()
            await page.locator('.side-pin-pop').waitFor()
            await page.waitForTimeout(250)
            await audit(`${w}: pins popover fits`, '.side-pin-pop')
            await shot(`${w}-pins-list`)
            await page.locator('.side-pin-action').first().click()
            await audit(`${w}: pin reply fits`, '.side-pin-quick', { bounds: false })
            await shot(`${w}-pins-reply`)
            await page.getByRole('button', { name: 'Edit', exact: true }).click()
            await audit(`${w}: pin edit rows fit`, '.side-pin-edit-row', { bounds: false })
            await shot(`${w}-pins-edit`)
            await page.getByRole('button', { name: 'Done', exact: true }).click()
            await page.getByRole('button', { name: 'Pin a chat', exact: true }).click()
            await audit(`${w}: pin picker fits`, '.side-pin-picker-row', { bounds: false })
          }
          if (surface === 'organize') { await page.getByRole('button', { name: 'Suggest groups', exact: true }).click(); await page.locator('.suggest-list').waitFor(); await audit(`${w}: suggested group names fit`, '.suggest-list', { bounds: false }); await audit(`${w}: organize dialog stays inside viewport`, 'dialog[open]'); const last = page.locator('.suggest-list li').last(); await last.scrollIntoViewIfNeeded(); check(`${w}: final suggestion is reachable`, await last.evaluate(el => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) })) }
          if (surface === 'pane-menu' || surface === 'pane-popover' || surface === 'session-menu') { await audit(`${w}: pane dialog stays inside viewport`, 'dialog[open]'); await audit(`${w}: pane title fits`, '.sheet-heading h2', { bounds: false }); await controls(`${w}: pane menu labels fit`, '.action-list') }
          await shot(`${w}-${surface}`)
          if (surface === 'choice' || surface === 'picker') {
            const lastKey = page.locator('.choice-keys button').last()
            await lastKey.focus()
            check(`${w}: ${surface} final key remains reachable`, await lastKey.evaluate(el => { const r = el.getBoundingClientRect(); return r.bottom <= innerHeight + 1 && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }))
            await shot(`${w}-${surface}-controls`)
          }
          if (surface === 'image') {
            await page.goto(`${origin}/fixture?surface=image&state=error`)
            await page.waitForTimeout(250)
            await audit(`${w}: image error text fits`, '.viewer-error')
            await shot(`${w}-image-error-latin`)
            if (!checkOnly) { await page.locator('.viewer-error').focus(); await page.keyboard.press('End'); await page.waitForTimeout(250); check(`${w}: image error scrolls by keyboard`, await page.locator('.viewer-error').evaluate(el => el.scrollHeight <= el.clientHeight + 1 || el.scrollTop > 0)) }
          }
        }
      }
    }
    if (hasHarness && ['all', 'states'].includes(part)) for (const [w, h] of [viewports[0], viewports[2], ...(!process.env.COARSE ? [viewports[5]] : [])]) {
      for (const surface of ['choice', 'ask', 'deck', 'preview', 'image', 'toast']) {
        await renew(true)
        for (const state of ['error', 'loading', 'empty', ...(surface === 'toast' ? ['suggested'] : [])]) {
          await renew()
          if (surface === 'image' && state !== 'error') continue
          await page.setViewportSize({ width: w, height: h })
          await page.goto(`${origin}/fixture?surface=${surface}&state=${state}&word=界`)
          await page.waitForTimeout(250)
          if (surface === 'toast') { await page.locator('[data-fixture-action="toast-tab"]').click(); if (state === 'suggested') await page.locator('.quick-toast-actions button', { hasText: 'Move to' }).waitFor() }
          if (surface === 'ask' && state === 'error') { await page.locator('.ask-form-option').first().click(); await page.getByRole('button', { name: 'Send answer', exact: true }).click(); await page.locator('.ask-form [role=alert]').waitFor() }
          if (surface === 'deck' && state !== 'empty') await page.getByRole('button', { name: 'Handled', exact: true }).click()
          const selector = { choice: '.choice-dock', ask: '.ask-form', deck: '.action-deck', preview: '.preview-drawer', image: '.image-viewer', toast: '.quick-toast' }[surface]
          await audit(`${w}: ${surface} ${state} fits with second input`, selector, { bounds: !['choice', 'ask'].includes(surface) })
          if (surface === 'toast' && state === 'error') { await page.getByRole('button', { name: 'Dismiss', exact: true }).scrollIntoViewIfNeeded(); await controls(`${w}: toast error controls reachable`, '.quick-toast-actions') }
          await shot(`${w}-${surface}-${state}`)
        }
      }
    }
    check('no browser errors' , errors.length === 0, errors.join('\n'))
  } finally {
    await context?.close()
    await browser?.close()
    if (server) await new Promise(resolve => server.close(resolve))
    if (temp) await fs.rm(temp, { recursive: true, force: true })
  }
  console.log(`${failures} responsive failures${checkOnly ? ' (baseline)' : ''}`)
  if (failures && !checkOnly) process.exitCode = 1
}
main().catch(error => { console.error(error); process.exitCode = 1 })
