// Claude Code advisor warnings: Settings and the tab's Model sheet warn only for a problem the server reports.
// A Fable advisor with no reported problem shows no warning (B220), also with a Fable main model.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('advisor-consent')
const FABLE = 'Claude consults Fable at the same points. On some plans, Fable calls bill to usage credits. On those plans, Claude Code leaves the Fable advisor off until you give a one-time consent with /model fable. /model fable also makes Fable the main model for new sessions; switch back with /model opus. Tether never accepts the consent for you.'
const NO_FLAGS = { code: 'no-flags', text: 'DISABLE_TELEMETRY turns off the feature-flag fetch that the advisor needs, so Claude Code starts without it.' }
const owner = { id: '%1', tmux_session: '$1', label: 'project:0.1', pane_name: 'Deploy fix', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null, ask: null }
const advisorState = problems => ({
  models: [{ model: 'off', name: 'Off', summary: 'Tether adds no advisor.' }, { model: 'opus', name: 'Opus', summary: 'Claude consults Opus.' }, { model: 'fable', name: 'Fable', summary: FABLE }],
  global: 'fable', model: 'fable', source: 'global', project: null, user_setting: '', problems: { opus: problems, fable: problems },
})
const footer = (model, problems) => ({ observed: true, model, mode: 'default', notices: [], advisor: { model: 'fable', name: 'Fable', source: 'tether', problems } })
const fixture = {
  sessions: { sessions: [owner], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: footer('Fable 5.2', []), 'claude-advisor': advisorState([]),
  'model-options': { agent: 'claude', models: [{ id: 'fable', label: 'Fable 5.2', current: true, effort: true }], efforts: ['high'], current_effort: 'high' },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
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
    page.on('pageerror', error => pageErrors.push(error.stack))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        // An endpoint outside this test answers like a server error: an empty object would break other settings.
        return fixture[endpoint] ? route.fulfill({ json: fixture[endpoint] }) : route.fulfill({ status: 404, json: { error: 'Not in this test.' } })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
    })
    const noScroll = () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
    // [main model, problems the server reports, warning expected]
    const cases = [['Fable 5.2', [], false], ['Opus 5.5', [], false], ['Fable 5.2', [NO_FLAGS], true]]
    for (const [width, height] of [[320, 700], [1280, 900]]) {
      await page.setViewportSize({ width, height })
      for (const [main, problems, warned] of cases) {
        const at = `${width}px, main ${main}, ${problems.length ? 'a reported problem' : 'no reported problem'}:`
        fixture.footer = footer(main, problems); fixture['claude-advisor'] = advisorState(problems)
        await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
        // The model pill and the Model sheet of the tab.
        const pill = page.locator('button.pill[aria-label^="Model and effort"]').first()
        // The pill shows the model name once the footer has loaded; read the label only then.
        await pill.filter({ hasText: main }).waitFor({ state: 'attached' })
        const label = await pill.getAttribute('aria-label')
        check(`${at} model pill label`, label === (warned ? 'Model and effort. Advisor Fable is not attached' : 'Model and effort'), label)
        check(`${at} model pill warning icon`, await pill.locator('.pill-warning').count() === (warned ? 1 : 0))
        // The phone composer shows the pills after it opens; else the tab's details list the same row.
        if (!(await pill.isVisible())) await page.locator('.composer textarea, .composer [contenteditable="true"]').first().click()
        if (await pill.isVisible()) { console.log(`   ${at} opened from the pill`); await pill.click() }
        else {
          console.log(`   ${at} opened from the tab details`)
          await page.locator('button.chat-title').first().click()
          await page.locator('dialog.session-details').getByRole('button', { name: /^Model and effort/ }).click()
        }
        const sheet = page.getByRole('dialog').filter({ hasText: 'Applies to this session only.' }).first()
        await sheet.waitFor()
        const sheetText = await sheet.innerText()
        check(`${at} Model sheet advisor state`, warned
          ? await sheet.locator('.provider-warning').count() === 1 && sheetText.includes('Advisor: Fable, not attached') && sheetText.includes(NO_FLAGS.text)
          : await sheet.locator('.provider-warning').count() === 0 && sheetText.includes('Advisor: Fable, set for this tab. If your plan still needs a usage-credits consent, Claude Code shows a notice in this tab and works without the advisor.') && !sheetText.includes('Claude consults it'), sheetText.slice(0, 300))
        check(`${at} Model sheet does not say "not attached" without a problem, and gives no /model command`, (warned || !/not attached/i.test(sheetText)) && !sheetText.includes('/model'), sheetText.slice(0, 300))
        check(`${at} Model sheet: no horizontal page scroll`, await noScroll())
        await page.screenshot({ path: path.join(SHOTS, `model-sheet-${width}-${main.split(' ')[0].toLowerCase()}-${warned ? 'problem' : 'clean'}.png`) })
        await page.keyboard.press('Escape')
        await sheet.waitFor({ state: 'detached' })
        // Settings > Claude Code advisor.
        if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
        await page.getByRole('button', { name: 'Settings' }).first().click()
        const search = page.getByPlaceholder(/Search settings/)
        if (await search.count()) await search.fill('Claude Code advisor')
        const group = page.locator('section.settings-group[aria-labelledby="settings-claude-advisor"]')
        await group.getByRole('radio', { name: 'Fable' }).waitFor()
        await group.scrollIntoViewIfNeeded()
        const text = await group.innerText()
        check(`${at} Settings: Fable is chosen`, await group.getByRole('radio', { name: 'Fable' }).isChecked())
        check(`${at} Settings warning`, warned
          ? await group.locator('.provider-warning').count() === 1 && text.includes('The advisor will not attach in new tabs:') && text.includes(NO_FLAGS.text)
          : await group.locator('.provider-warning').count() === 0 && !/not attach/i.test(text), text.slice(0, 400))
        check(`${at} Settings: the Fable note names the consent command and what else it changes`, text.includes('one-time consent with /model fable') && text.includes('/model fable also makes Fable the main model for new sessions; switch back with /model opus') && text.includes('Tether never accepts the consent for you.'), text.slice(0, 400))
        const box = await group.evaluate(node => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, wide: node.scrollWidth > node.clientWidth } })
        check(`${at} Settings: the group fits the screen`, box.left >= 0 && box.right <= width && !box.wide && await noScroll(), JSON.stringify(box))
        await page.screenshot({ path: path.join(SHOTS, `settings-${width}-${main.split(' ')[0].toLowerCase()}-${warned ? 'problem' : 'clean'}.png`) })
      }
    }
    // An Opus advisor with no reported problem: the same "set for this tab" wording, and no Fable consent text.
    fixture.footer = { ...footer('Opus 5.5', []), advisor: { model: 'opus', name: 'Opus', source: 'tether', problems: [] } }
    await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
    const opusPill = page.locator('button.pill[aria-label^="Model and effort"]').first()
    await opusPill.filter({ hasText: 'Opus 5.5' }).waitFor({ state: 'visible' })
    await opusPill.click()
    const opusSheet = page.getByRole('dialog').filter({ hasText: 'Applies to this session only.' }).first()
    await opusSheet.waitFor()
    const opusText = await opusSheet.innerText()
    check('Opus advisor: Model sheet advisor state', await opusSheet.locator('.provider-warning').count() === 0 && opusText.includes('Advisor: Opus, set for this tab. Claude consults it before big steps.') && !/consent/i.test(opusText), opusText.slice(0, 300))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`claude advisor: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
