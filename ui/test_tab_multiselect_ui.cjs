// Tab × close, multi-select with the action bar, and Collapse all / Expand all in the sidebar.
// Every API request stays in this page's fixtures: no real tab closes.
const fs = require('node:fs/promises')
const path = require('node:path')
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

async function main() {
  let browser, page, failed = 0
  const pageErrors = [], writes = []
  let refuse = null  // a pane id the fake server refuses to close, as a stale chat
  let failMove = false  // the next groups/move fails once
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  const deletes = () => writes.filter(item => item.method === 'DELETE').map(item => item.body)
  try {
    const origin = 'http://tether.test'
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
        const endpoint = url.pathname.slice('/mobile/api/'.length), method = route.request().method()
        if (method !== 'GET') {
          const body = route.request().postDataJSON()
          writes.push({ endpoint, method, body })
          if (endpoint === 'groups/move' && failMove) { failMove = false; return route.fulfill({ status: 500, json: { error: 'Move failed.' } }) }
          if (method === 'DELETE' && body?.pane === refuse) return route.fulfill({ status: 409, json: { error: 'That tab now holds another chat. Reload and try again.' } })
        }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
        try { return route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      if (!sessionStorage.getItem('test-prefs-set')) {
        localStorage.clear()
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false, mainModel: 'none', sideMode: 'sessions' }))
        sessionStorage.setItem('test-prefs-set', '1')
      }
    })
    const strip = page.locator('.chat-panel .pane-tabs, .pane-tabs').first()
    const tab = name => strip.locator('[role=tab]', { hasText: name })
    const closeOf = name => strip.getByRole('button', { name: `Close ${name}` })
    const opacity = locator => locator.evaluate(el => Number(getComputedStyle(el).opacity))
    const picked = () => strip.locator('[role=tab].picked').count()
    const bar = page.getByRole('toolbar', { name: 'Selected tabs' })
    const noSideScroll = () => page.evaluate(() => document.scrollingElement.scrollWidth <= innerWidth + 1)
    const openDialog = () => page.locator('dialog[open]')

    await page.setViewportSize({ width: 1280, height: 860 })
    await page.goto(`${origin}/chat/?session=%252`, { waitUntil: 'load' })
    await tab('Docs pass').waitFor({ state: 'visible' }).catch(async error => { console.log((await page.locator('body').innerText()).slice(0, 600)); console.log(pageErrors.join('\n')); throw error })

    // 1. The × on each tab.
    check('each tab has a × named Close <tab name>', await strip.locator('.tab-close').count() === 4 && await closeOf('Login review').count() === 1)
    check('the open tab shows its ×; another tab hides it until hover', await opacity(closeOf('Login fix')) === 1 && await opacity(closeOf('Docs pass')) === 0, JSON.stringify([await opacity(closeOf('Login fix')), await opacity(closeOf('Docs pass')), await strip.evaluate(el => el.className), await page.evaluate(() => matchMedia('(hover: none)').matches)]))
    await tab('Docs pass').hover(); await page.waitForTimeout(200)
    check('hover shows the ×', await opacity(closeOf('Docs pass')) === 1)
    const box = await closeOf('Docs pass').boundingBox()
    check('the × is at least 24x24 (WCAG 2.5.8)', box.width >= 24 && box.height >= 24, JSON.stringify(box))
    const tabBox = await tab('Docs pass').boundingBox()
    check('the × sits inside its tab', box.x >= tabBox.x && box.x + box.width <= tabBox.x + tabBox.width + 1, JSON.stringify({ box, tabBox }))
    await closeOf('Docs pass').click()
    await openDialog().waitFor()
    check('× opens the close question of the tab menu', await openDialog().getByText('Close this tab?').count() === 1 && await openDialog().getByRole('heading', { name: 'Docs pass' }).count() === 1)
    check('× did not switch the tab or start a drag', await tab('Login fix').getAttribute('aria-selected') === 'true' && await strip.locator('.dragging').count() === 0 && await tab('Docs pass').getAttribute('aria-selected') === 'false')
    check('no close before the answer', deletes().length === 0)
    await openDialog().getByRole('button', { name: 'Cancel' }).click()
    await openDialog().waitFor({ state: 'detached' })
    check('Cancel closes the question, nothing closes', deletes().length === 0)
    await tab('Docs pass').hover(); await closeOf('Docs pass').click()
    await openDialog().getByRole('button', { name: 'Close tab' }).click()
    await openDialog().waitFor({ state: 'detached' })
    check('the answer closes that tab with its chat', JSON.stringify(deletes()) === JSON.stringify([{ pane: '%4', thread: 't4' }]), JSON.stringify(deletes()))

    // Delete on a focused tab asks the same question.
    await tab('Loose tab').focus(); await page.keyboard.press('Delete')
    await openDialog().waitFor()
    check('Delete on a focused tab asks to close it', await openDialog().getByRole('heading', { name: 'Loose tab' }).count() === 1)
    await page.keyboard.press('Escape'); await openDialog().waitFor({ state: 'detached' })

    // 2. Multi-select in the strip.
    await tab('Login review').click({ modifiers: ['ControlOrMeta'] })
    check('Ctrl/Cmd+click keeps the open tab and adds the clicked one', await picked() === 2 && await tab('Login fix').getAttribute('aria-selected') === 'true')
    check('the bar shows the count', await bar.isVisible() && (await bar.innerText()).includes('2 tabs selected'))
    check('a chosen tab says so to screen readers', await strip.getByRole('tab', { name: /Login review.*selected/ }).count() === 1)
    await tab('Docs pass').click({ modifiers: ['Shift'] })
    check('Shift+click picks the range from the last pick', await picked() === 2 && await tab('Login review').evaluate(el => el.classList.contains('picked')) && await tab('Docs pass').evaluate(el => el.classList.contains('picked')))
    await tab('Loose tab').click({ modifiers: ['Shift'] })
    check('Shift+click to the left picks the range backwards', await picked() === 3)
    await page.keyboard.press('Escape')
    check('Esc clears the choice', await picked() === 0 && !(await bar.isVisible()))

    // Bulk close: one question naming the count, busy tabs and the per-tab close route.
    await tab('Login review').click({ modifiers: ['ControlOrMeta'] })
    await tab('Docs pass').click({ modifiers: ['ControlOrMeta'] })
    await bar.getByRole('button', { name: 'Close…' }).click()
    await openDialog().waitFor()
    const ask = await openDialog().innerText()
    check('one question names the count', ask.includes('Close 3 tabs?'), ask)
    check('the question names a busy tab', ask.includes('working') && ask.includes('1 tab still runs'), ask)
    const before = deletes().length
    await openDialog().getByRole('button', { name: 'Close 3 tabs' }).click()
    await openDialog().waitFor({ state: 'detached' })
    check('each tab closes with its own chat check', JSON.stringify(deletes().slice(before)) === JSON.stringify([{ pane: '%2', thread: 't2' }, { pane: '%3', thread: 't3' }, { pane: '%4', thread: 't4' }]), JSON.stringify(deletes().slice(before)))
    check('the choice clears after the close', await picked() === 0)

    // A stale tab: the server refuses it, the others close, it stays chosen.
    refuse = '%3'
    await tab('Login review').click({ modifiers: ['ControlOrMeta'] })
    await bar.getByRole('button', { name: 'Close…' }).click()
    await openDialog().getByRole('button', { name: 'Close 2 tabs' }).click()
    await openDialog().getByText('1 tab did not close.').waitFor()
    check('a refused tab shows why', (await openDialog().innerText()).includes('now holds another chat'))
    await openDialog().getByRole('button', { name: 'OK' }).click()
    check('the refused tab stays chosen, with the bar', await picked() === 1 && await tab('Login review').evaluate(el => el.classList.contains('picked')) && (await bar.innerText()).includes('1 tab selected'))
    refuse = null
    await page.keyboard.press('Escape')
    check('Esc clears a single chosen tab too', await picked() === 0 && !(await bar.isVisible()))

    // Group: one session → existing stack code, a new stack with both tabs.
    await tab('Docs pass').click({ modifiers: ['ControlOrMeta'] })
    await bar.getByRole('button', { name: 'Group…' }).click()
    await openDialog().getByRole('button', { name: 'New stack with these 2 tabs' }).click()
    await openDialog().getByLabel('Stack name').fill('Login')
    failMove = true
    await openDialog().getByRole('button', { name: 'Make stack' }).click()
    await openDialog().getByText('Move failed.').waitFor()
    check('a failed move offers to move the rest, not a new stack', await openDialog().getByRole('button', { name: 'Move the rest' }).count() === 1 && await openDialog().getByLabel('Stack name').isDisabled())
    await openDialog().getByRole('button', { name: 'Move the rest' }).click()
    await openDialog().waitFor({ state: 'detached' })
    const grouped = writes.filter(item => item.endpoint.startsWith('groups/')).map(item => [item.endpoint, item.body.pane, item.body.group ?? null])
    check('Group makes the stack once and the retry moves only the rest', JSON.stringify(grouped) === JSON.stringify([['groups/create', '%2', null], ['groups/move', '%4', 'g00000009'], ['groups/move', '%4', 'g00000009']]), JSON.stringify(grouped))

    // 1+2 in the sidebar.
    const nav = page.locator('.sidebar .session-groups')
    const row = name => nav.locator('.pane-children li.pane-row', { hasText: name })
    await row('Docs pass').waitFor()
    check('each chat row has a × named Close <name>', await nav.getByRole('button', { name: 'Close Docs pass' }).count() === 1 && await nav.getByRole('button', { name: 'Close api' }).count() === 1)
    await row('Docs pass').hover()
    check('row hover shows the ×', await opacity(nav.getByRole('button', { name: 'Close Docs pass' })) === 1)
        await nav.getByRole('button', { name: 'Close Docs pass' }).click()
    await openDialog().waitFor()
    check('row × opens the same close question and does not switch chat', await openDialog().getByText('Close this tab?').count() === 1 && await tab('Login fix').getAttribute('aria-selected') === 'true')
    await openDialog().getByRole('button', { name: 'Cancel' }).click(); await openDialog().waitFor({ state: 'detached' })
    await row('Docs pass').locator('button').first().click({ modifiers: ['ControlOrMeta'] })
    await nav.locator('.session-row', { has: page.getByRole('button', { name: 'Close api' }) }).locator('.session-main').click({ modifiers: ['ControlOrMeta'] })
    check('Ctrl/Cmd+click picks rows across sessions', await page.locator('.picked').count() >= 3 && (await bar.innerText()).includes('3 tabs selected'))
    await bar.getByRole('button', { name: 'Group…' }).click({ force: true })  // aria-disabled: it stays focusable and says why
    check('Group across sessions explains instead of opening', await openDialog().count() === 0 && (await bar.innerText()).includes('Choose tabs from one session'))
    await bar.getByRole('button', { name: 'Clear selection' }).click()
    check('Clear ends the choice', !(await bar.isVisible()) && await page.locator('.picked').count() === 0)

    // 3. Collapse all / Expand all.
    const fold = page.getByRole('button', { name: /^(Collapse|Expand) all sessions$/ })
    check('Collapse all shows while a session is open', await fold.getAttribute('aria-label') === 'Collapse all sessions')
    await fold.click()
    check('Collapse all folds the sessions', await nav.locator('.pane-children').count() === 0 && await fold.getAttribute('aria-label') === 'Expand all sessions')
    await page.reload({ waitUntil: 'load' }); await tab('Docs pass').waitFor()
    check('the folded state persists after a reload', await nav.locator('.pane-children').count() === 0 && await fold.getAttribute('aria-label') === 'Expand all sessions')
    await fold.click()
    check('Expand all opens them again', await nav.locator('.pane-children').count() === 1 && await fold.getAttribute('aria-label') === 'Collapse all sessions')

    // Touch: Select in the tab menu starts choosing; a tap picks.
    await tab('Loose tab').click({ button: 'right' })
    await openDialog().getByRole('button', { name: 'Select tabs' }).click()
    check('Select starts choosing with that tab', (await bar.innerText()).includes('1 tab selected') && await picked() === 1)
    await tab('Docs pass').click()
    check('while choosing, a tap picks instead of opening', await picked() === 2 && await tab('Login fix').getAttribute('aria-selected') === 'true')
    await bar.getByRole('button', { name: 'Done selecting' }).click()
    check('Done ends choosing', !(await bar.isVisible()))

    // Narrow screens: no sideways scroll, the bar fits.
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 760 })
      await page.goto(`${origin}/chat/?session=%252`, { waitUntil: 'load' })
      await tab('Login fix').waitFor()
      await tab('Login fix').click({ button: 'right' }).catch(() => {})
      const menu = openDialog().getByRole('button', { name: 'Select tabs' })
      if (await menu.count()) await menu.click()
      else { await page.keyboard.press('Escape') }
      await bar.waitFor().catch(() => {})
      const barBox = await bar.boundingBox()
      check(`${width}: the bar fits`, !!barBox && barBox.x >= 0 && barBox.x + barBox.width <= width, JSON.stringify(barBox))
      check(`${width}: no sideways scroll`, await noSideScroll())
      await page.keyboard.press('Escape')
    }
    check('no page errors', pageErrors.length === 0, pageErrors.join('\n'))
  } finally {
    await page?.close().catch(() => {})
    if (browser && !process.env.TETHER_CDP_URL) await browser.close().catch(() => {})
  }
  if (failed) { console.log(`${failed} failed`); process.exit(1) }
  console.log('Tab close, multi-select and collapse all pass')
}
main().catch(error => { console.error(error); process.exit(1) })
