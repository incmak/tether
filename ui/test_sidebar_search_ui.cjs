// The sidebar search finds sessions by tab title, stack, branch and custom group, and shows only the matching tabs.
// Every API request stays in this page's fixtures. Run: npm run build -- --outDir <dir>, then DIST=<dir> node test_sidebar_search_ui.cjs
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-14/sidebar-search/shots')
const ids = { tether: '$1', blender: '$2' }
const pane = (id, session, name, extra = {}) => ({ id, tmux_session: ids[session], label: `${session}:0.${id.slice(1)}`, ask: null, agent: 'claude', dir: `/srv/${session}`, state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, pane_name: name, ...extra })
const fixture = {
  sessions: { default_thread: null, sessions: [
    pane('%1', 'tether', 'Voice mode latency', { group: 'g1' }),
    pane('%2', 'tether', 'Backlog search', { agent: 'codex', worktree: 'b77-search' }),
    pane('%3', 'tether', 'Release notes'),
    pane('%4', 'blender', 'Render farm'),
  ], groups: { $1: [{ id: 'g1', name: 'Audio stack', project: '' }] } },
  footer: { observed: false, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '', running: 0, runs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'projects/backlog': { text: '', version: 'v1', limit: 50000 },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], history = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack)); page.on('console', message => { if (message.type() === 'error') pageErrors.push(message.text().slice(0, 300)) })
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (endpoint.startsWith('history')) { history.push(url.searchParams.get('q') || ''); return route.fulfill({ json: { chats: [], total: 0, next: null, counts: { all: 0, claude: 0, codex: 0 }, dirs: [] } }) }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      if (!sessionStorage.getItem('kept')) { localStorage.clear(); sessionStorage.setItem('kept', '1') }
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    let root
    const tabs = () => root.locator('.session-groups .pane-children .pane-child-name').allInnerTexts()
    const sessionTitles = () => root.locator('.session-groups .session-title').allInnerTexts()
    for (const [width, height] of [[1280, 860], [390, 844], [320, 640]]) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      await page.evaluate(() => localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false })))
      await page.reload({ waitUntil: 'load' })
      root = page.locator(width < 800 ? 'dialog.drawer' : 'aside.sidebar')
      if (width < 800) await page.getByRole('button', { name: 'Open sessions' }).first().click()
      const search = root.getByRole('searchbox', { name: 'Search sessions' })
      await search.waitFor()
      await root.locator('.session-groups .session-title').first().waitFor()
      await search.fill('voice')
      check(`${at} tab title finds its session`, JSON.stringify(await sessionTitles()) === JSON.stringify(['tether']), JSON.stringify(await sessionTitles()))
      check(`${at} only the matching tab shows`, (await tabs()).length === 1 && (await tabs())[0].startsWith('Voice mode latency'), JSON.stringify(await tabs()))
      await search.fill('AUDIO')
      check(`${at} stack name matches`, (await tabs()).length === 1 && (await tabs())[0].startsWith('Voice mode latency'), JSON.stringify(await tabs()))
      await search.fill('b77')
      check(`${at} worktree branch matches`, (await tabs()).length === 1 && (await tabs())[0].startsWith('Backlog search'), JSON.stringify(await tabs()))
      await search.fill('tether release')
      check(`${at} words split over session and tab`, (await tabs()).length === 1 && (await tabs())[0].startsWith('Release notes'), JSON.stringify(await tabs()))
      await search.fill('tether')
      check(`${at} session name shows every tab`, (await tabs()).length === 3, JSON.stringify(await tabs()))
      await search.fill('nothing like this')
      check(`${at} empty result says no match`, await root.getByText('No sessions or tabs match.').first().isVisible())
      check(`${at} no page overflow`, !(await overflow()))
      await search.fill('voice')
      await page.screenshot({ path: path.join(SHOTS, `voice-${width}.png`) })
      // Enter opens the first matching tab.
      await search.press('Enter')
      await page.waitForFunction(() => /[?&#]/.test(location.href) || document.querySelector('[aria-current="page"]'), null, { timeout: 3000 }).catch(() => {})
      check(`${at} Enter opens the voice tab`, await page.evaluate(() => [...document.querySelectorAll('[aria-current="page"]')].some(el => el.textContent.includes('Voice')) || location.href.includes('%251') || location.href.includes('%1')), await page.evaluate(() => location.href))
    }
    // Typing a word asks the server for past chats once, after the pause, not per letter.
    history.length = 0
    await page.getByRole('button', { name: 'Open sessions' }).first().click()
    const search = root.getByRole('searchbox', { name: 'Search sessions' })
    await search.fill('')
    await search.pressSequentially('latency', { delay: 30 })
    await page.waitForTimeout(600)
    const typed = history.filter(q => q && q !== 'voice')
    check('past chats wait for a pause in typing', typed.length <= 2 && typed.at(-1) === 'latency', JSON.stringify(history))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`sidebar search ui: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
