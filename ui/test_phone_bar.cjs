// The phone bottom bar (PhoneTabs.tsx): Home, Chats, Files, Terminals and More. The More menu holds Share, Browser and
// the assistant. No tab is ever disabled: with no chat open, Terminals shows an empty state. Checks the five items, the menu (opens, each page opens, current state, the browser dot), keyboard and
// screen reader use, 44 px targets, 320 and 390 px, light and dark, and reduced motion.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const SHOTS = shotsDir('phone-bar-5')
const pane = (id, name, extra) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null, ...extra })
const BROWSER_IDLE = { enabled: true, available: true, reason: '', agents: [], asks: [] }
const fixture = {
  sessions: { sessions: [pane('%1', 'Deploy fix'), pane('%2', 'Docs pass')], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': BROWSER_IDLE, 'browser/tabs': { tabs: [{ id: 'T1', url: 'https://example.com/', title: 'Example' }] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

async function main() {
  let browser, context, page, server, failed = 0, failSessions = false
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    // Holds a free port for the page's origin. The route below serves every request.
    server = http.createServer((req, res) => { res.writeHead(404); res.end() })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://localhost:${server.address().port}`
    browser = await getBrowser()
    context = await browser.newContext({ hasTouch: true })
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    // The fixtures answer every API path with JSON, so the browser sheet's event stream is refused: not a page fault.
    page.on('console', message => { if (message.type() === 'error' && !/Failed to load resource|EventSource's response has a MIME type/.test(message.text())) pageErrors.push(message.text().slice(0, 600)) })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (endpoint === 'sessions' && failSessions) return route.fulfill({ status: 500, json: { error: 'Server is down.' } })
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (endpoint.startsWith('share/list')) return route.fulfill({ json: { files: [], next: null, usage: { used: 0, quota: 1e9, count: 0 } } })
        if (endpoint === 'share/uploads') return route.fulfill({ json: { uploads: [] } })
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
    const open = async (width, height, query = '?session=%251') => {
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/${query}`, { waitUntil: 'load' })
      await page.locator('.phone-tabbar').waitFor({ state: 'visible' })
      await page.waitForTimeout(300)
    }
    const bar = page.locator('.phone-tabbar')
    const tab = name => bar.locator(':scope > button').filter({ hasText: new RegExp(`^${name}$`) })
    const more = bar.locator(':scope > button.phone-tab-more')
    const menu = page.locator('.phone-more-menu')
    const row = name => menu.getByRole('menuitem', { name })
    const focused = () => page.evaluate(() => document.activeElement?.textContent?.trim() ?? '')
    const tap = target => target.tap()

    for (const [width, scheme] of [[390, 'dark'], [320, 'light']]) {
      const at = `${width}px ${scheme}`
      fixture['browser/agents'] = BROWSER_IDLE
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: width === 320 ? 'reduce' : 'no-preference' })
      await open(width, 740)

      // The bar: five items, each a 44 px target, no label cut off.
      const items = await page.evaluate(() => [...document.querySelectorAll('.phone-tabbar > button')].map(button => {
        const label = button.querySelector(':scope > span:last-child'), box = button.getBoundingClientRect(), text = label.getBoundingClientRect()
        return { text: button.textContent.trim(), width: box.width, height: box.height, left: box.left, right: box.right, disabled: button.disabled, clipped: label.scrollWidth > label.clientWidth + 1 || text.left < box.left - 1 || text.right > box.right + 1 }
      }))
      check(`${at}: the bar is Home, Chats, Files, Terminals, More`, items.map(item => item.text).join(',') === 'Home,Chats,Files,Terminals,More', JSON.stringify(items))
      check(`${at}: each bar item is at least 44 by 44`, items.every(item => item.width >= 44 && item.height >= 44), JSON.stringify(items))
      check(`${at}: the bar fits and no label is cut off`, items.every(item => !item.clipped && item.left >= 0 && item.right <= width + 0.5), JSON.stringify(items))
      check(`${at}: no horizontal page scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      check(`${at}: Terminals is enabled with a chat open`, items[3] && !items[3].disabled)
      check(`${at}: Chats is current, More is not`, await tab('Chats').getAttribute('aria-current') === 'page' && await more.getAttribute('aria-current') === null)
      check(`${at}: More is a closed menu button with no dot`, await more.getAttribute('aria-haspopup') === 'menu' && await more.getAttribute('aria-expanded') === 'false' && await more.getAttribute('aria-label') === null && await more.locator('.phone-tab-dot').count() === 0)
      await page.screenshot({ path: path.join(SHOTS, `bar-${width}-${scheme}.png`) })

      const input = page.locator('.chat-page .composer textarea')
      await tap(input)
      check(`${at}: the bar stays visible on focus alone`, await bar.isVisible())
      await page.setViewportSize({ width, height: 440 })
      await page.locator('html[data-keyboard]').waitFor()
      check(`${at}: the bar hides while the keyboard is open`, await bar.isHidden())
      await page.setViewportSize({ width, height: 740 })
      await bar.waitFor()
      await input.evaluate(el => el.blur())

      // More opens a menu with the three pages.
      await tap(more)
      await menu.waitFor({ state: 'visible' })
      const names = await menu.getByRole('menuitem').allTextContents()
      check(`${at}: More opens a menu with Share, Browser and the assistant`, names.length === 3 && names[0] === 'Share' && names[1] === 'Browser' && names[2].length > 0, JSON.stringify(names))
      check(`${at}: the menu is named and More shows it as open`, await menu.getAttribute('aria-label') === 'More' && await more.getAttribute('aria-expanded') === 'true' && await more.getAttribute('aria-controls') === await menu.getAttribute('id'))
      check(`${at}: focus moves to the first row`, await focused() === 'Share', await focused())
      const rows = await menu.getByRole('menuitem').evaluateAll(list => list.map(item => { const box = item.getBoundingClientRect(); return { width: box.width, height: box.height, left: box.left, right: box.right, icon: !!item.querySelector('svg'), clipped: item.scrollWidth > item.clientWidth + 1 } }))
      check(`${at}: each row has an icon, is at least 44 by 44 and fits the screen`, rows.every(item => item.icon && item.width >= 44 && item.height >= 44 && item.left >= 0 && item.right <= width && !item.clipped), JSON.stringify(rows))
      const menuBox = await menu.boundingBox(), barBox = await bar.boundingBox()
      check(`${at}: the menu is above the bar and on screen`, menuBox.y >= 0 && menuBox.y + menuBox.height <= barBox.y && menuBox.x >= 0 && menuBox.x + menuBox.width <= width, JSON.stringify(menuBox))
      check(`${at}: the menu is on top where it shows`, await menu.evaluate(element => { const box = element.getBoundingClientRect(); return element.contains(document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)) }))
      const motion = await menu.evaluate(element => getComputedStyle(element).animationName)
      check(`${at}: the menu ${width === 320 ? 'does not move with reduced motion' : 'has a short entrance'}`, width === 320 ? motion === 'none' : motion === 'phone-more-in', motion)
      await page.waitForTimeout(200)
      await page.screenshot({ path: path.join(SHOTS, `more-${width}-${scheme}.png`) })

      // Keyboard: arrows, Home, End, Escape, Enter, Tab.
      await page.keyboard.press('ArrowDown'); const second = await focused()
      await page.keyboard.press('End'); const last = await focused()
      await page.keyboard.press('ArrowDown'); const wrapped = await focused()
      await page.keyboard.press('ArrowUp'); const back = await focused()
      await page.keyboard.press('Home'); const first = await focused()
      check(`${at}: the arrow keys, Home and End move through the rows`, second === 'Browser' && last === names[2] && wrapped === 'Share' && back === names[2] && first === 'Share', [second, last, wrapped, back, first].join(','))
      await page.keyboard.press('Escape')
      await menu.waitFor({ state: 'detached' })
      check(`${at}: Escape closes the menu and focus returns to More`, await focused() === 'More' && await more.getAttribute('aria-expanded') === 'false', await focused())
      check(`${at}: Escape leaves the chat in place`, await tab('Chats').getAttribute('aria-current') === 'page')
      await page.keyboard.press('Enter')
      await menu.waitFor({ state: 'visible' })
      check(`${at}: Enter on More opens the menu`, await focused() === 'Share')
      await page.keyboard.press('Shift+Tab')
      await menu.waitFor({ state: 'detached' })
      check(`${at}: Shift+Tab closes the menu and moves to the tab before More`, await focused() === 'Terminals', await focused())

      // A tap outside closes the menu and does nothing else.
      await tap(more)
      await menu.waitFor({ state: 'visible' })
      await page.mouse.click(width / 2, 200)
      await menu.waitFor({ state: 'detached' })
      check(`${at}: a tap outside closes the menu only`, await tab('Chats').getAttribute('aria-current') === 'page' && await page.locator('dialog[open]').count() === 0)
      // A bar tab closes the menu and opens its screen.
      await tap(more)
      await menu.waitFor({ state: 'visible' })
      await tap(tab('Files'))
      check(`${at}: a bar tab closes the menu and opens its screen`, await menu.count() === 0 && await tab('Files').getAttribute('aria-current') === 'page')

      // Share: two taps, and More is current.
      await tap(more)
      await tap(row('Share'))
      await page.locator('.sh-head').waitFor({ state: 'visible' })
      check(`${at}: Share opens in two taps and the menu closes`, await menu.count() === 0)
      check(`${at}: More is the one current item on Share`, await more.getAttribute('aria-current') === 'page' && await bar.locator(':scope > button[aria-current="page"]').count() === 1)
      check(`${at}: focus is on More after Share opens`, await focused() === 'More', await focused())
      await tap(more)
      await menu.waitFor({ state: 'visible' })
      check(`${at}: the Share row is current in the menu`, await row('Share').getAttribute('aria-current') === 'page' && await row('Browser').getAttribute('aria-current') === null)
      await page.screenshot({ path: path.join(SHOTS, `more-share-${width}-${scheme}.png`) })
      await page.keyboard.press('Escape')
      await tap(tab('Chats'))
      check(`${at}: Chats opens again and More is not current`, await tab('Chats').getAttribute('aria-current') === 'page' && await more.getAttribute('aria-current') === null)

      // Browser: opens the browser sheet.
      await tap(more)
      await tap(row('Browser'))
      await page.locator('dialog.browser-sheet[open]').waitFor({ state: 'visible' })
      check(`${at}: Browser opens the browser in two taps`, await menu.count() === 0)
      await page.locator('dialog.browser-sheet').evaluate(dialog => dialog.close())
      await page.locator('dialog.browser-sheet[open]').waitFor({ state: 'detached' })

      // The assistant opens.
      await tap(more)
      await tap(menu.getByRole('menuitem').nth(2))
      await page.locator('.live, .lazy-float').first().waitFor({ state: 'attached' })
      check(`${at}: the assistant opens in two taps`, await menu.count() === 0)
    }

    // The browser's state: busy is in the names and on the Browser row; asking adds a dot on More.
    await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' })
    fixture['browser/agents'] = { ...BROWSER_IDLE, agents: [{ pane: '%2', tabs: ['T1'], since: 1 }] }
    await open(390, 740)
    await page.waitForFunction(() => document.querySelector('.phone-tab-more')?.getAttribute('aria-label'))
    check('busy: More names the state and shows no dot', await more.getAttribute('aria-label') === 'More, Browser: agents are using it' && await more.locator('.phone-tab-dot').count() === 0, await more.getAttribute('aria-label'))
    await tap(more)
    await menu.waitFor({ state: 'visible' })
    check('busy: the Browser row names the state, with a plain dot and text', await row('Browser, agents are using it').count() === 1 && await menu.locator('.phone-tab-browser .phone-tab-dot:not(.asking)').count() === 1 && await menu.locator('.phone-tab-browser small').textContent() === 'Agents are using it')
    await page.keyboard.press('Escape')
    fixture['browser/agents'] = { ...BROWSER_IDLE, agents: [{ pane: '%2', tabs: ['T1'], since: 1 }], asks: [{ id: 'a1', pane: '%2', text: 'Sign in, please', at: Date.now() / 1000 }] }
    await page.waitForFunction(() => document.querySelector('.phone-tab-more .phone-tab-dot.asking'), null, { timeout: 15000 })
    check('asking: More shows a dot and names the state', await more.getAttribute('aria-label') === 'More, Browser: an agent needs you' && await more.locator('.phone-tab-dot.asking').count() === 1, await more.getAttribute('aria-label'))
    check('asking: the visible label is still More', (await more.textContent()).trim() === 'More')
    await tap(more)
    await menu.waitFor({ state: 'visible' })
    check('asking: the Browser row names the state, with a dot and text', await row('Browser, an agent needs you').count() === 1 && await menu.locator('.phone-tab-browser .phone-tab-dot.asking').count() === 1 && await menu.locator('.phone-tab-browser small').textContent() === 'An agent needs you')
    const askRow = await menu.locator('.phone-tab-browser').evaluate(item => ({ clipped: item.scrollWidth > item.clientWidth + 1, right: item.getBoundingClientRect().right }))
    check('asking: the Browser row text is not cut off', !askRow.clipped && askRow.right <= 390, JSON.stringify(askRow))
    await page.screenshot({ path: path.join(SHOTS, 'more-asking-390-light.png') })
    await page.setViewportSize({ width: 320, height: 740 })
    await page.waitForTimeout(200)
    const narrow = await menu.evaluate(element => { const box = element.getBoundingClientRect(), item = element.querySelector('.phone-tab-browser'); return { left: box.left, right: box.right, clipped: item.scrollWidth > item.clientWidth + 1 } })
    check('asking, 320px: the menu fits and the Browser row is not cut off', narrow.left >= 0 && narrow.right <= 320 && !narrow.clipped, JSON.stringify(narrow))
    await page.screenshot({ path: path.join(SHOTS, 'more-asking-320-light.png') })
    await page.keyboard.press('Escape')

    // No chat open: Terminals stays enabled and shows an empty state. The menu has no Browser row.
    fixture['browser/agents'] = BROWSER_IDLE
    const sessions = fixture.sessions
    const blank = page.locator('.home-blank')
    for (const [width, scheme] of [[390, 'light'], [390, 'dark'], [320, 'light'], [320, 'dark']]) {
      const at = `no chat, ${width}px ${scheme}`
      fixture.sessions = { sessions: [], default_thread: null }
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' })
      await open(width, 740, '')
      check(`${at}: Terminals is enabled`, await tab('Terminals').isEnabled() && await bar.locator(':scope > button:disabled').count() === 0)
      await tap(tab('Terminals'))
      await blank.getByRole('heading', { name: 'No chat is open' }).waitFor({ state: 'visible' })
      check(`${at}: Terminals is the current item`, await tab('Terminals').getAttribute('aria-current') === 'page' && await bar.locator(':scope > button[aria-current="page"]').count() === 1)
      check(`${at}: one line says why it is empty`, (await blank.locator('p').allTextContents()).join('|') === 'Terminals shows the terminal of the open chat.', (await blank.locator('p').allTextContents()).join('|'))
      const choose = blank.getByRole('button', { name: 'Choose a chat' })
      const box = await choose.boundingBox(), barTop = (await bar.boundingBox()).y
      check(`${at}: the Choose a chat button is at least 44 high, on screen and above the bar`, box && box.height >= 44 && box.x >= 0 && box.x + box.width <= width && box.y + box.height <= barTop, JSON.stringify(box))
      check(`${at}: no horizontal page scroll`, !(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)))
      await page.screenshot({ path: path.join(SHOTS, `terminals-empty-${width}-${scheme}.png`) })
      await tap(choose)
      await page.locator('dialog.drawer[open]').waitFor({ state: 'visible' })
      check(`${at}: Choose a chat opens the sessions list`, await page.locator('dialog.drawer[open]').getAttribute('aria-label') === 'Sessions')
      await page.keyboard.press('Escape')
      await page.locator('dialog.drawer[open]').waitFor({ state: 'detached' })
      check(`${at}: the empty state stays after the list closes`, await blank.getByRole('heading', { name: 'No chat is open' }).count() === 1)
      if (width === 390 && scheme === 'light') {
        await tap(more)
        await menu.waitFor({ state: 'visible' })
        const few = await menu.getByRole('menuitem').allTextContents()
        check('no chat: the menu has Share and the assistant, no Browser', few.length === 2 && few[0] === 'Share' && !few.includes('Browser'), JSON.stringify(few))
        await page.screenshot({ path: path.join(SHOTS, 'more-no-chat-390-light.png') })
        await page.keyboard.press('Escape')
        await tap(tab('Home'))
        check('no chat: Home opens from the empty Terminals screen', await tab('Home').getAttribute('aria-current') === 'page' && await blank.getByRole('heading', { name: 'No chat is open' }).count() === 0)
      }
    }

    // The session list did not load: the Terminals screen says so and offers Retry. Retry then shows the chat's terminal.
    failSessions = true
    await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' })
    await open(390, 740, '')
    await tap(tab('Terminals'))
    await blank.getByRole('heading', { name: 'Could not load sessions' }).waitFor({ state: 'visible', timeout: 30000 })
    check('error: Terminals shows the load error with Retry', await blank.getByRole('button', { name: /retry|try again/i }).count() === 1, await blank.textContent())
    await page.screenshot({ path: path.join(SHOTS, 'terminals-error-390-light.png') })
    failSessions = false
    fixture.sessions = sessions
    await tap(blank.getByRole('button', { name: /retry|try again/i }))
    await page.locator('.chat-panel .terminal-toggle[aria-pressed="true"]').waitFor({ state: 'visible', timeout: 20000 })
    check('a chat that arrives on the empty Terminals screen opens in its terminal view', await tab('Terminals').getAttribute('aria-current') === 'page' && await blank.count() === 0)
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (context) await context.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`phone bar: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
