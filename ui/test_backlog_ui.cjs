// The backlog page, its search and filters, the ⌥B keys, and backlog results in search (⌥Space), against an existing build.
// Every API request stays in this page's fixtures. Run: npm run build -- --outDir <dir>, then DIST=<dir> node test_backlog_ui.cjs
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal, shotsDir } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = shotsDir('backlog-detail')
const backlog = [
  '- [ ] Write the release notes #B3',
  '',
  '## tether',
  '- [ ] Fix flaky share test (codex · pane %1) #B7',
  '  - teardown races the HTTP worker',
  '- [ ] Add backlog search (claude · pane %2) #B12',
  '  - Doing: claude · pane %9',
  '- [x] Ship gzip (claude) #B5',
  '  - Done: fixed in abc123',
  '',
  '## brisk',
  '- [ ] Zebra item #B9',
  '  - [x] find a zebra',
  '  - [ ] draw the stripes',
  '- [ ] alpha item #B10',
  '',
].join('\n')
let saved = { text: backlog, version: 'v1', limit: 50000 }
const fixture = {
  sessions: { sessions: [], default_thread: null },
  footer: { observed: false, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '', running: 0, runs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], posts = []
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
        if (endpoint === 'projects/backlog') {
          if (request.method() === 'POST') { const body = request.postDataJSON(); posts.push(body); saved = { ...saved, text: body.text, version: `v${posts.length + 1}` } }
          return route.fulfill({ json: saved })
        }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      if (!sessionStorage.getItem('kept')) { localStorage.clear(); sessionStorage.setItem('kept', '1') }
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    // Loading bones reuse the row classes (Backlog.tsx BacklogBones); only loaded rows carry data-backlog-key.
    const titles = scope => scope.locator('.backlog-row[data-backlog-key] .backlog-text-line').allInnerTexts()

    for (const [width, height] of [[1280, 860], [390, 844], [320, 640]]) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      if (width !== 1280) await page.evaluate(() => localStorage.removeItem('tether.backlogView'))
      await page.goto(`${origin}/chat/?go=backlog`, { waitUntil: 'load' })
      const sheet = page.locator('dialog.backlog-page')
      await sheet.locator('.backlog-row[data-backlog-key]').first().waitFor()
      check(`${at} ?go=backlog opens the page`, await sheet.isVisible())
      check(`${at} page fills the width`, (await sheet.boundingBox()).width >= Math.min(width - 48, 1440) - 1)
      check(`${at} plain list keeps the saved order`, JSON.stringify(await titles(sheet)) === JSON.stringify(['Write the release notes', 'Fix flaky share test', 'Add backlog search', 'Zebra item', 'alpha item']))
      check(`${at} views show counts`, (await sheet.getByRole('button', { name: /In progress/ }).innerText()).includes('1'))
      check(`${at} no page overflow`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `page-${width}.png`) })

      // Search: every word, any case, details too. A match is marked.
      const search = sheet.getByRole('searchbox', { name: 'Search the backlog' })
      await search.fill('TEARDOWN')
      check(`${at} search reads details`, JSON.stringify(await titles(sheet)) === JSON.stringify(['Fix flaky share test']))
      check(`${at} drag is off while searching`, !(await sheet.locator('.backlog-grip').count()))
      await search.fill('flaky')
      check(`${at} title match is marked`, (await sheet.locator('mark').first().innerText()) === 'flaky')
      check(`${at} status says what is shown`, (await sheet.locator('.backlog-status').innerText()).startsWith('1 shown · 5 open'))
      await search.fill('flkay shre')
      check(`${at} typos find the item (the sidebar's matcher)`, JSON.stringify(await titles(sheet)) === JSON.stringify(['Fix flaky share test']))
      await search.press('Escape')
      check(`${at} Escape clears the search, the page stays`, (await search.inputValue()) === '' && await sheet.isVisible())
      await search.fill('nothing like this')
      check(`${at} empty result offers Clear filters`, await sheet.getByText('Nothing matches').isVisible())
      await sheet.locator('.backlog-empty').getByRole('button', { name: 'Clear filters' }).click()
      check(`${at} Clear filters brings the list back`, (await titles(sheet)).length === 5)

      // Views and projects.
      await sheet.getByRole('button', { name: /^Done/ }).first().click()
      check(`${at} Done view`, JSON.stringify(await titles(sheet)) === JSON.stringify(['Ship gzip']))
      await sheet.getByRole('button', { name: /^Open/ }).first().click()
      await sheet.getByRole('button', { name: /^brisk/ }).click()
      check(`${at} project filter`, JSON.stringify(await titles(sheet)) === JSON.stringify(['Zebra item', 'alpha item']))
      check(`${at} reorder inside one project`, (await sheet.locator('.backlog-grip').count()) === 2)
      await sheet.getByRole('button', { name: 'Order: Manual order' }).click()
      await page.getByRole('option', { name: 'A to Z' }).click()
      check(`${at} A to Z`, JSON.stringify(await titles(sheet)) === JSON.stringify(['alpha item', 'Zebra item']))
      await sheet.getByRole('button', { name: /^All projects/ }).click()
      await sheet.getByRole('button', { name: 'Added by: Anyone' }).click()
      await page.getByRole('option', { name: /^Codex/ }).click()
      check(`${at} added by Codex`, JSON.stringify(await titles(sheet)) === JSON.stringify(['Fix flaky share test']))
      check(`${at} no page overflow (filtered)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `page-filtered-${width}.png`) })
      await page.keyboard.press('Escape')
      await sheet.waitFor({ state: 'detached' })
    }

    // The order and "Added by" stay per browser; the search does not.
    await page.setViewportSize({ width: 1280, height: 860 })
    await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
    await page.waitForTimeout(400)
    await page.keyboard.press('Alt+KeyB')
    const small = page.locator('dialog.backlog-sheet:not(.backlog-page)')
    await small.locator('.backlog-row[data-backlog-key]').first().waitFor()
    check('⌥B opens the sheet', await small.isVisible())
    check('the sheet keeps A to Z and Codex', JSON.stringify(await titles(small)) === JSON.stringify(['Fix flaky share test']))
    await small.getByRole('button', { name: 'Added by: Codex' }).click()
    await page.getByRole('option', { name: /^Anyone/ }).click()
    await small.getByRole('button', { name: 'Order: A to Z' }).click()
    await page.getByRole('option', { name: 'Manual order' }).click()
    check('sheet back to the saved order', (await titles(small))[0] === 'Write the release notes')
    await page.keyboard.press('/')
    check('"/" goes to the search', await small.getByRole('searchbox').evaluate(node => node === document.activeElement))
    await page.screenshot({ path: path.join(SHOTS, 'sheet-1280.png') })
    await page.keyboard.type('release')
    await small.getByRole('textbox', { name: 'New backlog item' }).first().fill('half typed')
    await small.getByRole('button', { name: 'Open full page' }).click()
    const grown = page.locator('dialog.backlog-page')
    await grown.waitFor()
    check('the full page keeps the search and the typed text', (await grown.getByRole('searchbox').inputValue()) === 'release' && (await grown.getByRole('textbox', { name: 'New backlog item' }).first().inputValue()) === 'half typed')
    await grown.getByRole('textbox', { name: 'New backlog item' }).first().fill('')
    await page.keyboard.press('Escape')
    await page.locator('dialog.backlog-sheet').waitFor({ state: 'detached' })
    await page.keyboard.press('Alt+Shift+KeyB')
    await page.locator('dialog.backlog-page').waitFor()
    check('⌥⇧B opens the page', true)
    await page.keyboard.press('Escape')
    await page.locator('dialog.backlog-sheet').waitFor({ state: 'detached' })

    // ⌥Space: search from anywhere, backlog items included. A saved "Added by" must not hide the item picked here.
    await page.evaluate(() => localStorage.setItem('tether.backlogView', JSON.stringify({ sort: 'manual', who: 'codex' })))
    await page.keyboard.press('Control+KeyK')
    await page.locator('dialog.command-menu').waitFor()
    check('⌘K opens search on Home', true)
    await page.keyboard.press('Control+KeyK')
    await page.locator('dialog.command-menu').waitFor({ state: 'detached' })
    await page.keyboard.press('Alt+Space')
    const menu = page.locator('dialog.command-menu')
    await menu.waitFor()
    check('⌥Space opens search', await menu.getByRole('combobox').evaluate(node => node === document.activeElement))
    await page.keyboard.type('gzip')
    await menu.locator('#command-list').getByText('Ship gzip').waitFor()
    check('search finds a done backlog item', (await menu.locator('.command-row', { hasText: 'Ship gzip' }).innerText()).includes('B5'))
    check('search offers the whole backlog', await menu.getByText('Search the backlog for “gzip”').isVisible())
    await page.screenshot({ path: path.join(SHOTS, 'search-1280.png') })
    await page.keyboard.press('Enter')
    const found = page.locator('dialog.backlog-page')
    await found.locator('.backlog-row[data-backlog-key]').first().waitFor()
    check('Enter opens the item on the page, in Done too', JSON.stringify(await titles(found)) === JSON.stringify(['Ship gzip']) && (await found.getByRole('searchbox').inputValue()) === 'B5')
    await found.getByRole('textbox', { name: 'New backlog item' }).fill('half typed')
    await found.getByRole('button', { name: 'Show as a sheet' }).click()
    const shrunk = page.locator('dialog.backlog-sheet:not(.backlog-page)')
    await shrunk.waitFor()
    check('the sheet keeps the search from the menu and the typed text', (await shrunk.getByRole('searchbox').inputValue()) === 'B5' && (await shrunk.getByRole('textbox', { name: 'New backlog item' }).inputValue()) === 'half typed')
    await shrunk.getByRole('textbox', { name: 'New backlog item' }).fill('')
    await shrunk.getByRole('button', { name: 'Open full page' }).click()
    await found.waitFor()
    await page.keyboard.press('Escape')
    await page.keyboard.press('Escape')
    await found.waitFor({ state: 'detached' })
    await page.keyboard.press('Alt+Space')
    await menu.waitFor()
    await page.keyboard.press('Alt+Space')
    await menu.waitFor({ state: 'detached' })
    check('⌥Space again closes search', true)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.keyboard.press('Alt+Space')
    await menu.waitFor()
    await page.keyboard.type('zebra')
    await menu.getByText('Zebra item').waitFor()
    check('390px search fits', !(await overflow()) && (await menu.boundingBox()).width <= 390)
    await page.screenshot({ path: path.join(SHOTS, 'search-390.png') })
    check('no saves from filtering', !posts.length, JSON.stringify(posts))
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'detached' })

    // B76: an item's details beside the list (1200px and wider) or over it. Esc or Back closes them and gives the cursor back to the row.
    for (const [width, height] of [[1280, 860], [390, 844], [320, 640]]) {
      const at = `${width}px`, beside = width >= 1200
      await page.setViewportSize({ width, height })
      await page.evaluate(() => localStorage.removeItem('tether.backlogView'))
      await page.goto(`${origin}/chat/?go=backlog`, { waitUntil: 'load' })
      const sheet = page.locator('dialog.backlog-page')
      await sheet.locator('.backlog-row[data-backlog-key]').first().waitFor()
      // A CSS locator: under the narrow overlay the list is hidden, and role queries skip hidden rows.
      const zebra = sheet.locator('button.backlog-open', { hasText: /^Zebra item$/ })
      await zebra.click()
      const pane = sheet.locator('#backlog-detail')
      await pane.waitFor()
      check(`${at} the title opens the details`, (await pane.locator('#backlog-detail-title').innerText()) === 'Zebra item' && (await zebra.getAttribute('aria-current')) === 'true')
      check(`${at} details ${beside ? 'beside' : 'over'} the list`, beside
        ? (await pane.boundingBox()).x >= (await sheet.locator('.backlog-main').boundingBox()).x + 300 && !(await pane.evaluate(node => node.classList.contains('overlay')))
        : await pane.evaluate(node => node.classList.contains('overlay')) && await sheet.locator('.backlog-main').evaluate(node => node.inert) && await sheet.locator('.backlog-nav').evaluate(node => node.inert))
      check(`${at} narrow: the cursor goes to the title`, beside || await pane.locator('#backlog-detail-title').evaluate(node => node === document.activeElement))
      check(`${at} steps show progress`, /\d of 2/.test(await pane.locator('#backlog-detail-steps').innerText()))
      check(`${at} project and ID`, (await pane.locator('.backlog-detail-bar').innerText()).includes('brisk') && await pane.getByRole('button', { name: 'Copy ID B9' }).isVisible())
      check(`${at} no page overflow (details)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `detail-${width}.png`) })
      if (beside) {
        await page.keyboard.press('j')
        check(`${at} J shows the next item`, (await pane.locator('#backlog-detail-title').innerText()) === 'alpha item')
        await page.keyboard.press('k')
        await page.keyboard.press('ArrowUp')
        check(`${at} K and ↑ go back`, (await pane.locator('#backlog-detail-title').innerText()) === 'Add backlog search'
          && await sheet.getByRole('button', { name: 'Add backlog search', exact: true }).evaluate(node => node === document.activeElement))
        await page.keyboard.press('ArrowDown')
        const saves = posts.length
        const [stepSave] = await Promise.all([
          page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/api/projects/backlog')),
          pane.getByRole('checkbox', { name: 'draw the stripes' }).check(),
        ])
        await pane.getByText('2 of 2').waitFor()
        check(`${at} a step saves from the details`, stepSave.ok() && posts.length === saves + 1 && posts.at(-1).text.includes('  - [x] draw the stripes'))
        // An agent renames the item during a Reword: Tether asks before it writes over the new title.
        const agentSaves = text => { saved = { ...saved, text, version: `agent${Date.now()}` } }
        await pane.getByRole('button', { name: 'Reword' }).click()
        await pane.getByRole('textbox', { name: 'Reword Zebra item' }).fill('Zebra mine')
        agentSaves(saved.text.replace('Zebra item #B9', 'Zebra theirs #B9'))
        await page.waitForTimeout(3600)
        await page.keyboard.press('Enter')
        const clash = pane.getByRole('alertdialog')
        check(`${at} a Reword over an agent's rename asks first`, await clash.isVisible() && (await clash.innerText()).includes('Zebra theirs') && !posts.some(post => post.text.includes('Zebra mine')))
        await clash.getByRole('button', { name: 'Keep theirs' }).click()
        check(`${at} Keep theirs keeps the agent's title`, (await pane.locator('#backlog-detail-title').innerText()) === 'Zebra theirs')
        // The item goes during a Reword: the words stay, with a way to add them back.
        await pane.getByRole('button', { name: 'Reword' }).click()
        await pane.getByRole('textbox', { name: 'Reword Zebra theirs' }).fill('Zebra kept')
        agentSaves(saved.text.replace(/- \[ \] Zebra theirs #B9\n(  - .*\n)*/, ''))
        await pane.getByText('This item is gone').waitFor({ timeout: 6000 })
        check(`${at} a gone item keeps the new wording`, (await pane.innerText()).includes('Zebra kept'))
        const [addSave] = await Promise.all([
          page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/api/projects/backlog')),
          pane.getByRole('button', { name: 'Add as a new item' }).click(),
        ])
        await page.waitForFunction(() => document.querySelector('#backlog-detail .backlog-detail-kept') === null)
        check(`${at} Add as a new item saves the words`, addSave.ok() && posts.at(-1).text.includes('- [ ] Zebra kept'))
        agentSaves(backlog)
        await pane.locator('#backlog-detail-title', { hasText: /^Zebra item$/ }).waitFor({ timeout: 6000 })
        await zebra.focus()
        // ⌘/ over the page, then Esc: the cursor goes back to the row, so the next Esc closes only the details.
        await page.keyboard.press('Control+Slash')
        await page.locator('dialog.shortcuts-sheet').waitFor()
        await page.keyboard.press('Escape')
        await page.locator('dialog.shortcuts-sheet').waitFor({ state: 'detached' })
        await page.waitForTimeout(100)
        check(`${at} closing the shortcuts gives the cursor back to the row`, await zebra.evaluate(node => node === document.activeElement))
        await page.keyboard.press('Escape')
      } else if (width === 390) {
        // The details cover the list here, so Undo and save errors show in the pane.
        await pane.getByRole('button', { name: 'Delete' }).click()
        await pane.getByText('This item is gone').waitFor()
        const undo = pane.getByRole('button', { name: 'Undo' })
        check(`${at} Undo shows over the list`, await undo.isVisible())
        await page.screenshot({ path: path.join(SHOTS, `detail-undo-${width}.png`) })
        await undo.click()
        await pane.locator('#backlog-detail-title', { hasText: /^Zebra item$/ }).waitFor()
        check(`${at} Undo brings the item back`, true)
        await pane.getByRole('button', { name: 'Back to the list' }).click()
      }
      else await page.keyboard.press('Escape')
      await pane.waitFor({ state: 'detached' })
      // closeDetail() moves the cursor in the next animation frame, after the pane leaves; a slow runner can check first.
      const refocused = await page.waitForFunction(node => node === document.activeElement, await zebra.elementHandle(), { timeout: 2000 }).then(() => true, () => false)
      check(`${at} closing gives the cursor back to the row`, refocused)
      check(`${at} the page stays open`, await sheet.isVisible())
      await page.keyboard.press('Escape')
      await sheet.waitFor({ state: 'detached' })
    }

    // ⌘/ (Ctrl+/ off a Mac) and ? open the keyboard shortcuts on any screen. ⌘/ also works over a sheet.
    await page.setViewportSize({ width: 1280, height: 860 })
    await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
    await page.waitForTimeout(400)
    const keys = page.locator('dialog.shortcuts-sheet')
    await page.keyboard.press('Control+Slash')
    await keys.waitFor()
    check('⌘/ opens the shortcuts on Home', await keys.getByRole('heading', { name: 'Keyboard shortcuts' }).isVisible())
    await keys.getByRole('searchbox', { name: 'Find a shortcut' }).fill('split')
    check('the filter finds a key by its action', JSON.stringify(await keys.locator('dt').allInnerTexts()) === JSON.stringify(['Split right']))
    await keys.getByRole('searchbox', { name: 'Find a shortcut' }).fill('zzz')
    check('no match says so', await keys.getByText('No shortcut matches “zzz”.').isVisible())
    await page.keyboard.press('Escape')
    check('Esc clears the filter first', (await keys.getByRole('searchbox').inputValue()) === '' && await keys.isVisible())
    await page.screenshot({ path: path.join(SHOTS, 'shortcuts-1280.png') })
    await page.keyboard.press('Control+Slash')
    await keys.waitFor({ state: 'detached' })
    check('⌘/ again closes them', true)
    await page.keyboard.press('?')
    await keys.waitFor()
    check('? opens them when no text box has focus', true)
    await page.keyboard.press('Escape')
    await page.keyboard.press('Escape')
    await keys.waitFor({ state: 'detached' })
    await page.keyboard.press('Alt+Shift+KeyB')
    await page.locator('dialog.backlog-page .backlog-row[data-backlog-key]').first().waitFor()
    await page.keyboard.press('Control+Slash')
    await keys.waitFor()
    check('⌘/ works over the backlog page', true)
    await page.keyboard.press('Escape')
    await keys.waitFor({ state: 'detached' })
    check('Esc closes only the shortcuts', await page.locator('dialog.backlog-page').isVisible())
    await page.keyboard.press('Escape')
    await page.locator('dialog.backlog-page').waitFor({ state: 'detached' })

    for (const [width, height] of [[390, 844], [320, 640]]) {
      await page.setViewportSize({ width, height })
      await page.keyboard.press('Control+Slash')
      await keys.waitFor()
      check(`${width}px shortcuts fit`, !(await overflow()) && (await keys.boundingBox()).width <= width)
      await page.screenshot({ path: path.join(SHOTS, `shortcuts-${width}.png`) })
      await page.keyboard.press('Control+Slash')
      await keys.waitFor({ state: 'detached' })
    }

    // Settings › Keyboard shortcuts lists the same keys, and its search finds one.
    await page.setViewportSize({ width: 1280, height: 860 })
    await page.getByRole('button', { name: 'Settings', exact: true }).first().click()
    const settings = page.locator('dialog.preferences')
    await settings.waitFor()
    await settings.getByRole('button', { name: /Keyboard shortcuts/ }).click()
    check('Settings has a Keyboard shortcuts section', await settings.getByRole('heading', { name: 'Backlog', exact: true }).isVisible() && await settings.getByText('Split right').isVisible())
    await page.screenshot({ path: path.join(SHOTS, 'settings-shortcuts-1280.png') })
    await settings.getByRole('searchbox', { name: 'Search settings' }).fill('grid of four')
    await settings.getByRole('searchbox', { name: 'Search settings' }).focus()
    await page.keyboard.press('Control+Slash')
    await keys.waitFor()
    await page.keyboard.press('Escape')
    await keys.waitFor({ state: 'detached' })
    await page.waitForTimeout(100)
    check('closing the shortcuts over Settings gives the cursor back', await settings.getByRole('searchbox', { name: 'Search settings' }).evaluate(node => node === document.activeElement))
    check('a Settings search finds a key', await settings.locator('dt', { hasText: 'Grid of four' }).isVisible() && (await settings.locator('.shortcuts-rows').count()) === 1)
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`backlog ui: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
