// Claude slash-command dialogs (/mcp, /config, /usage ...) as cards over the message box.
// The dialog data comes from server/claude_dialog.py reading the server's own fixtures, so the test covers the real shape.
// Run against an existing build. Every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || ''
const SERVER = path.join(__dirname, '..', 'server')
const NAMES = (process.env.ONLY || 'mcp,config,usage,effort,resume,status,help,plugin,stats').split(',')
const DIALOGS = JSON.parse(execFileSync('python3', ['-c', `
import json, sys
sys.path.insert(0, sys.argv[1])
import claude_dialog
print(json.dumps({name: claude_dialog.parse(open(sys.argv[1] + '/fixtures/claude_dialog_' + name + '.ansi').read()) for name in sys.argv[2:]}))
`, SERVER, ...NAMES], { encoding: 'utf8', maxBuffer: 1 << 24 }))

function fixtures(name) {
  const dialog = DIALOGS[name]
  const attention = { id: 'dialog-' + dialog.title, reason: 'dialog', requiresResponse: false, text: dialog.text, dialog }
  const pane = { id: '%1', tmux_session: '$1', label: 'Dialog test', agent: 'claude', dir: '/srv/project', state: 'Idle',
    writable: true, thread: 'dialog-thread', approval: null }
  const messages = [{ id: 'm1', role: 'user', text: 'Start the work.', time: '2026-11-15T08:00:00Z' },
    { id: 'm2', role: 'assistant', text: 'Work started.', time: '2026-11-15T08:00:05Z' }]
  return {
    sessions: { sessions: [pane], default_thread: pane.thread },
    chat: { messages, busy: false, thread: pane.thread, agent: pane.agent, writable: true, session: pane.id, earlier: 0, limit: 100,
      state: 'Idle', approval: null, attention },
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
    for (const state of NAMES) for (const width of [320, 390, 1280]) {
      const name = `${state}-${width}`, errors = [], sent = [], pageErrors = []
      const fixture = fixtures(state)
      if (page) await page.close().catch(() => {})
      page = await context.newPage()
      page.setDefaultTimeout(10000)
      page.on('pageerror', error => pageErrors.push(error.message))
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
      await page.routeWebSocket('**/*', socket => socket.close())
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
        if (url.pathname.includes('/api/')) {
          const endpoint = url.pathname.split('/api/')[1]
          if (request.method() !== 'GET') {
            sent.push({ endpoint, ...request.postDataJSON() })
            return route.fulfill({ json: { ok: true, accepted: true } })
          }
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
          return route.fulfill({ json: fixture[endpoint] || {} })
        }
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
        return route.abort('blockedbyclient')
      })
      await page.addInitScript(() => {
        if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false, sidePins: [], look: { layout: 'classic', surface: 'flat', backdrop: 'ember' } }))
      })
      try {
        await page.setViewportSize({ width, height: 800 })
        await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
        await page.waitForSelector('.composer textarea')
        const card = page.locator('.dialog-dock')
        await card.waitFor()
        const dialog = DIALOGS[state]
        const actions = () => sent.filter(item => item.endpoint === 'dialog').map(item => [item.action, item.key ?? item.label ?? item.tab ?? item.value ?? item.text].join(':'))
        if (!(await card.locator('.choice-head').innerText()).includes(dialog.title)) errors.push('wrong card title')
        if ((await page.locator('body').innerText()).includes('needs attention')) errors.push('the raw fallback title shows')
        if (/Esc to (cancel|close)/.test(await card.innerText())) errors.push('the terminal key hints show as text')
        if (dialog.tabs && await card.getByRole('tab').count() !== dialog.tabs.length) errors.push('tabs are missing')
        if (dialog.tabs && await card.getByRole('tab', { selected: true }).innerText() !== dialog.tab) errors.push('wrong tab is selected')
        if (state === 'mcp') {
          if (await card.locator('.dlg-list button').count() !== 9) errors.push('want 9 server rows')
          if (await card.locator('.dlg-list [aria-current="true"]').count() !== 1) errors.push('want one marked row')
          if (await card.locator('.dlg-group').count() !== 3) errors.push('want 3 group rows')
          await card.locator('.dlg-list button', { hasText: 'scrut' }).click()
          await card.getByRole('button', { name: /^Confirm/ }).click()
          await card.getByRole('button', { name: 'Move down' }).click()
          await card.getByRole('button', { name: /^Cancel/ }).click()
          await page.waitForTimeout(200)
          if (actions().join() !== 'row:scrut,key:Enter,key:Down,key:Escape') errors.push(`actions sent: ${actions().join()}`)
          if (sent.some(item => item.endpoint === 'dialog' && item.title !== 'Manage MCP servers')) errors.push('an action has no dialog title')
          if (sent.find(item => item.action === 'row').row !== 2) errors.push('wrong row index')
        } else if (state === 'config') {
          await card.getByRole('tab', { name: 'Usage' }).click()
          await card.getByRole('searchbox').fill('compact')
          await card.getByRole('button', { name: 'Search', exact: true }).click()
          await card.getByRole('button', { name: /^Clear/ }).click()
          await page.waitForTimeout(200)
          if (actions().join() !== 'tab:Usage,filter:compact,key:Escape') errors.push(`actions sent: ${actions().join()} ${JSON.stringify(sent)} box=${await card.getByRole('searchbox').inputValue()}`)
          if (await card.getByRole('button', { name: 'Type' }).count()) errors.push('"Type" shows as a key button')
        } else if (state === 'usage') {
          if (await card.locator('progress').count() !== 3) errors.push('want 3 usage meters')
          if (await card.locator('pre').count()) errors.push('usage shows as terminal text')
          if (!await card.locator('.dlg-facts dt', { hasText: 'Total cost' }).count()) errors.push('facts are missing')
        } else if (state === 'effort') {
          if (await card.getByRole('radio').count() !== 5) errors.push('want 5 levels')
          if (await card.getByRole('radio', { checked: true }).innerText() !== 'medium') errors.push('wrong level is marked')
          await card.getByRole('radio', { name: 'high', exact: true }).click()
          await card.getByRole('button', { name: /^This session only/ }).click()
          await page.waitForTimeout(200)
          if (actions().join() !== 'scale:high,key:s') errors.push(`actions sent: ${actions().join()}`)
          if (await card.getByRole('button', { name: /^(←|→|Left|Right)$/ }).count()) errors.push('arrow buttons repeat the level control')
        } else if (state === 'resume') {
          if (!await card.getByRole('searchbox').isEnabled()) errors.push('the search box is off')
          if (!(await card.locator('.dlg-list button').first().innerText()).includes('ago')) errors.push('rows have no detail line')
          if (!await card.getByRole('button', { name: /^Show all projects/ }).count()) errors.push('no button for Ctrl+A')
        } else if (state === 'status') {
          if (await card.locator('.dlg-facts div').count() < 10) errors.push('facts are missing')
          if (await card.locator('pre').count()) errors.push('status shows as terminal text')
          if (await card.getByRole('button').filter({ hasNotText: /Cancel|Status|Config|Usage|Stats/ }).count()) errors.push('unexpected buttons')
        } else if (state === 'help') {
          if (await card.locator('pre').count() !== 1) errors.push('the shortcut table is missing')
        } else if (state === 'plugin') {
          if (await card.getByRole('tab').count() !== 5) errors.push('want 5 tabs')
          if (!await card.getByRole('button', { name: /^Toggle/ }).count()) errors.push('no toggle button')
        } else if (state === 'stats') {
          if (!await card.getByRole('button', { name: /^Close/ }).count()) errors.push('a dialog with no key hints has no Close button')
        }
        for (const button of await card.getByRole('button').all()) {
          const size = await button.boundingBox()
          if (size && size.height < 24) errors.push(`button ${(await button.innerText()) || 'icon'} is ${Math.round(size.height)} px high`)
        }
        const wide = await card.evaluate(node => [...node.querySelectorAll('*')].filter(item => !item.closest('pre, .perm-tabs') && item.scrollWidth > item.clientWidth + 1 && getComputedStyle(item).overflowX === 'visible').length)
        if (wide) errors.push(`${wide} elements are cut off sideways`)
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
        if (overflow > 0) errors.push(`the page scrolls sideways by ${overflow} px`)
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
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
