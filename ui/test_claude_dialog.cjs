// Claude slash-command dialogs (/mcp, /config, /usage ...) as cards over the message box.
// The dialog data comes from server/claude_dialog.py reading the server's own fixtures, so the test covers the real shape.
// Run against an existing build. Every API request stays in this page's fixtures.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const { getBrowser, serveLocal } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || ''
const SERVER = path.join(__dirname, '..', 'server')
const NAMES = (process.env.ONLY || 'mcp,config,config_list,privacy,usage,effort,resume,status,help,help_narrow,plugin,stats,stats_body,stats_models,stats_narrow').split(',')
const DIALOGS = JSON.parse(execFileSync('python3', ['-c', `
import json, sys
sys.path.insert(0, sys.argv[1])
import claude_dialog
print(json.dumps({name: claude_dialog.parse(open(sys.argv[1] + '/fixtures/claude_dialog_' + name + '.ansi').read()) for name in sys.argv[2:]}))
`, SERVER, ...NAMES], { encoding: 'utf8', maxBuffer: 1 << 24 }))
// The marked row of each list holds its Tab stop on the first render, wherever the mark is: also first and last.
const marked = (name, pick) => {
  const dialog = structuredClone(DIALOGS[name])
  for (const block of dialog.blocks) if (block.type === 'list') {
    const rows = block.items.filter(item => 'text' in item)
    rows.forEach((item, at) => { item.selected = at === pick(rows.length) })
  }
  return dialog
}
if (DIALOGS.mcp) { DIALOGS.mark_first = marked('mcp', () => 0); DIALOGS.mark_last = marked('mcp', count => count - 1); NAMES.push('mark_first', 'mark_last') }

function fixtures(name) {
  const dialog = DIALOGS[name]
  const attention = { id: 'dialog-' + dialog.title, reason: 'dialog', requiresResponse: false, text: dialog.text, dialog }
  const pane = { id: '%1', tmux_session: '$1', label: 'Dialog test', agent: 'claude', dir: '/srv/project', state: 'Idle',
    writable: true, thread: 'dialog-thread', approval: null }
  const messages = [{ id: 'm1', role: 'user', text: 'Start the work.', time: '2026-11-15T08:00:00Z' },
    { id: 'm2', role: 'assistant', text: 'Work started.', time: '2026-11-15T08:00:05Z' },
    { id: 'm3', role: 'user', text: '/model', time: '2026-11-15T08:01:00Z' },
    { id: 'm4', role: 'assistant', kind: 'terminal', text: 'Set model to `Fable 5.2` and saved as your default for new sessions', time: '2026-11-15T08:01:01Z' },
    { id: 'm5', role: 'assistant', kind: 'terminal', text: 'Context Usage\n  38.2k/1m tokens (4%)\n  Free space: 928.8k', time: '2026-11-15T08:01:02Z' }]
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
      // The stored prefs, as server/prefs_sync.py keeps them: a save applies its changes and raises the version.
      let prefs = { ...fixture.prefs, zone: 'UTC' }
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
            const body = request.postDataJSON()
            sent.push({ endpoint, ...body })
            // A prefs save answers with the stored prefs, as the server does. Another body makes the app retry or stop.
            if (endpoint === 'prefs') {
              prefs = { prefs: { ...prefs.prefs, ...body.changes }, version: prefs.version + 1, zone: prefs.zone }
              return route.fulfill({ json: prefs })
            }
            return route.fulfill({ json: { ok: true, accepted: true } })
          }
          if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
          return route.fulfill({ json: endpoint === 'prefs' ? prefs : fixture[endpoint] || {} })
        }
        if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
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
        // Before any click: one Tab stop in each list, on its marked row when it has one.
        for (const list of await card.locator('.dlg-list').all()) {
          const stops = await list.locator('button[tabindex="0"]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-current') === 'true'))
          const hasMark = await list.locator('button[aria-current="true"]').count() > 0
          if (stops.length !== 1 || hasMark && !stops[0]) errors.push(`want one Tab stop on the marked row, got ${JSON.stringify(stops)}`)
        }
        const actionName = item => [item.action, item.key ?? item.label ?? item.tab ?? item.value ?? item.text].join(':')
        const actions = () => sent.filter(item => item.endpoint === 'dialog').map(actionName)
        const clickAction = (button, expected) => Promise.all([page.waitForResponse(response => {
          const request = response.request()
          return request.method() === 'POST' && new URL(response.url()).pathname === '/mobile/api/dialog'
            && actionName(request.postDataJSON()) === expected
        }), button.click()])
        const pres = await card.locator('pre').count()
        if (!(await card.locator('.choice-head').innerText()).includes(dialog.title)) errors.push('wrong card title')
        if ((await page.locator('body').innerText()).includes('needs attention')) errors.push('the raw fallback title shows')
        if (/Esc to (cancel|close)/.test(await card.innerText())) errors.push('the terminal key hints show as text')
        if (dialog.tabs && await card.locator('.perm-tabs').getByRole('tab').count() !== dialog.tabs.length) errors.push('tabs are missing')
        if (dialog.tabs && await card.locator('.perm-tabs').getByRole('tab', { selected: true }).innerText() !== dialog.tab) errors.push('wrong tab is selected')
        if (state === 'mcp') {
          if (await card.locator('.dlg-list button').count() !== 9) errors.push('want 9 server rows')
          if (await card.locator('.dlg-list [aria-current="true"]').count() !== 1) errors.push('want one marked row')
          if (await card.locator('.dlg-group').count() !== 3) errors.push('want 3 group rows')
          await clickAction(card.locator('.dlg-list button', { hasText: 'scrut' }), 'row:scrut')
          await clickAction(card.getByRole('button', { name: /^Confirm/ }), 'key:Enter')
          if (await card.getByRole('button', { name: /^(Move up|Move down|↑|↓)$/ }).count()) errors.push('arrow buttons show next to clickable rows')
          // The keyboard moves the focus between rows without a key to the terminal; Tab reaches the list once.
          const marked = card.locator('.dlg-list button[aria-current="true"]')
          if (await card.locator('.dlg-list button[tabindex="0"]').count() !== 1) errors.push('want one Tab stop in the list')
          await marked.focus()
          // Prefs saves (seen, layout) and UI usage batches go out on their own timers. Neither reaches the terminal.
          const toTerminal = () => sent.filter(item => !['prefs', 'ui-usage'].includes(item.endpoint))
          const before = toTerminal().length
          const focused = () => page.evaluate(() => document.activeElement?.textContent ?? '')
          const start = await focused()
          await page.keyboard.press('ArrowDown')
          if (await focused() === start) errors.push('ArrowDown did not move the focus')
          await page.keyboard.press('End')
          const last = await focused()
          await page.keyboard.press('Home')
          if (await focused() === last) errors.push('Home did not move the focus')
          if (toTerminal().length !== before) errors.push(`moving the focus sent a request: ${JSON.stringify(toTerminal().slice(before))}`)
          // A click on the marked row runs its action, as Enter does in the terminal.
          if (!(await marked.innerText()).includes('Confirm')) errors.push('the marked row does not name its action')
          await clickAction(marked, 'key:Enter')
          await clickAction(card.getByRole('button', { name: /^Cancel/ }), 'key:Escape')
          if (actions().join() !== 'row:scrut,key:Enter,key:Enter,key:Escape') errors.push(`actions sent: ${actions().join()}`)
          if (sent.some(item => item.endpoint === 'dialog' && (item.title !== 'Manage MCP servers' || item.mode !== dialog.mode))) errors.push('an action has no dialog title or mode')
          if (sent.find(item => item.action === 'row').row !== 2) errors.push('wrong row index')
        } else if (state === 'config') {
          await clickAction(card.getByRole('tab', { name: 'Usage' }), 'tab:Usage')
          await card.getByRole('searchbox').fill('compact')
          await clickAction(card.getByRole('button', { name: 'Search', exact: true }), 'filter:compact')
          await clickAction(card.getByRole('button', { name: /^Clear/ }), 'key:Escape')
          if (actions().join() !== 'tab:Usage,filter:compact,key:Escape') errors.push(`actions sent: ${actions().join()} ${JSON.stringify(sent)} box=${await card.getByRole('searchbox').inputValue()}`)
          if (await card.getByRole('button', { name: 'Type' }).count()) errors.push('"Type" shows as a key button')
        } else if (state === 'config_list') {
          // Enter flips the marked setting at once, so the button names the setting.
          if (!await card.getByRole('button', { name: /^Change “Auto-compact”/ }).count()) errors.push('the Change button does not name the setting')
          // "Show more" is one checked action: the server moves the mark to the last row, then presses Down.
          await card.locator('.dlg-more button').last().click()
          await page.waitForTimeout(200)
          const more = sent.filter(item => item.endpoint === 'dialog')
          const last = await card.locator('li:not(.dlg-more) > button[data-row]').last().innerText()
          if (more.length !== 1 || more[0].action !== 'row' || more[0].then !== 'Down' || !more[0].label || !last.includes(more[0].label)) errors.push(`Show more sent ${JSON.stringify(more)}; last row ${last}`)
          const result = page.locator('.command-result')
          if (await result.locator('code').innerText() !== 'Fable 5.2') errors.push('a one-line command result does not show its code mark')
          if ((await result.innerText()).includes('`')) errors.push('a command result shows raw backticks')
          if (await page.locator('pre.terminal-snapshot').count() !== 1) errors.push('want one terminal box: the multi-line result only')
        } else if (state === 'privacy') {
          if (!await card.getByRole('button', { name: /^Toggle “Help improve our AI models”/ }).count()) errors.push('the Toggle button does not name the setting')
        } else if (state === 'usage') {
          if (await card.locator('progress').count() !== 3) errors.push('want 3 usage meters')
          if (await card.locator('pre').count()) errors.push('usage shows as terminal text')
          if (!await card.locator('.dlg-facts dt', { hasText: 'Total cost' }).count()) errors.push('facts are missing')
        } else if (state === 'effort') {
          if (await card.getByRole('radio').count() !== 5) errors.push('want 5 levels')
          if (await card.getByRole('radio', { checked: true }).innerText() !== 'medium') errors.push('wrong level is marked')
          await card.getByRole('radio', { name: 'high', exact: true }).click()
          await page.waitForTimeout(300)
          if (!await page.evaluate(() => document.activeElement?.getAttribute('role') === 'radio')) errors.push('the level button lost the keyboard focus')
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
        } else if (state === 'help' || state === 'help_narrow') {
          if (pres) errors.push('the shortcut table shows as terminal text')
          if (await card.locator('.dlg-keys div').count() !== 17) errors.push('want 17 shortcuts')
          if (!(await card.locator('.dlg-keys div').nth(8).innerText()).includes('toggle tasks')) errors.push('shortcuts are out of order')
        } else if (state === 'plugin') {
          if (await card.getByRole('tab').count() !== 5) errors.push('want 5 tabs')
          if (!await card.getByRole('button', { name: /^Toggle/ }).count()) errors.push('no toggle button')
        } else if (state === 'stats' || state === 'stats_body' || state === 'stats_narrow') {
          if (!await card.getByRole('button', { name: /^Close/ }).count()) errors.push('a dialog with no Esc hint has no Close button')
          if (pres) errors.push('the activity chart shows as terminal text')
          const cells = await card.locator('.dlg-heat-grid i').count()
          if (cells < 7 * 38) errors.push(`the activity chart has ${cells} squares`)
          const chart = await card.locator('.dlg-heat').boundingBox(), panel = await card.boundingBox()
          if (chart.x < panel.x || chart.x + chart.width > panel.x + panel.width) errors.push('the activity chart is wider than the card')
          const square = await card.locator('.dlg-heat-grid i').first().boundingBox()
          if (square.width < 2.5) errors.push(`chart squares are ${square.width.toFixed(1)} px wide`)
          if (await card.getByRole('button', { name: /^(↑|↓)$/ }).count()) errors.push('focus arrows show as buttons')
          const months = await card.getByRole('list', { name: 'Activity by month' }).getByRole('listitem').allInnerTexts()
          if (months.length < 9 || !months.some(line => /: [1-9]\d* active days?, busiest day (light|medium|high|very high)/.test(line))) errors.push(`the chart has no text alternative: ${months.slice(-2).join(' / ')}`)
          await card.getByRole('radio', { name: 'Last 30 days' }).click()
          await card.getByRole('tab', { name: 'Models' }).click()
          await page.waitForTimeout(200)
          if (actions().join() !== 'option:Last 30 days,subtab:Models') errors.push(`actions sent: ${actions().join()}`)
          if (state === 'stats_body' && !await card.getByRole('button', { name: /^Cycle dates/ }).count()) errors.push('no button for the "r" key')
        } else if (state === 'stats_models') {
          if (await card.locator('.dlg-entries li').count() !== 4) errors.push('want 4 model entries')
          if (pres !== 1) errors.push('want the tokens chart as the one terminal drawing')
          if (await card.getByRole('tab', { selected: true }).last().innerText() !== 'Models') errors.push('the Models view is not marked')
        }
        // One round trip: the shared browser is remote, so a call per button made each case take seconds.
        const short = await card.evaluate(node => [...node.querySelectorAll('button, input:is([type="button"], [type="submit"], [type="reset"], [type="image"]), [role="button"], [role="radio"], [role="tab"]')]
          .filter(button => { const box = button.getBoundingClientRect(); return box.width > 0 && box.height > 0 && box.height < 24 && !button.closest('[hidden], [aria-hidden="true"]') })
          .map(button => `button ${button.innerText || button.value || 'icon'} is ${Math.round(button.getBoundingClientRect().height)} px high`))
        errors.push(...short)
        const wide = await card.evaluate(node => [...node.querySelectorAll('*')].filter(item => !item.closest('pre, .perm-tabs, .dlg-sr') && item.scrollWidth > item.clientWidth + 1 && getComputedStyle(item).overflowX === 'visible').map(item => `${item.tagName.toLowerCase()}.${[...item.classList].join('.')} "${item.textContent.slice(0, 40)}"`))
        if (wide.length) errors.push(`${wide.length} elements are cut off sideways: ${wide.join('; ')}`)
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
