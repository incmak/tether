// Settings navigation and search against an existing build and fixture APIs.
// Run from ui/ with node test_settings_nav_ui.cjs.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const ORIGIN = 'http://localhost:7813'
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json' }
const pane = { id: '%1', tmux_session: '$1', label: 'test:0.0', pane_name: 'Test chat', agent: 'claude', dir: '/srv/test', state: 'Idle', writable: true, thread: 't1', approval: null }
const fixture = {
  sessions: { sessions: [pane], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' },
  'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  usage: { claude: null, codex: null },
  prefs: { prefs: { selected: '%1', voiceOff: true }, version: 1, zone: 'UTC' },
  host: { name: 'Test server', office: false },
  update: { current: '0.0.0', latest: '0.0.0', available: false },
  'work/board': { projects: [], items: [] },
}

async function main() {
  const browser = await getBrowser()
  let page
  const errors = []
  try {
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => errors.push(error.stack))
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const { origin, pathname } = new URL(route.request().url())
      if (origin !== ORIGIN) return route.abort('blockedbyclient')
      if (pathname.startsWith('/mobile/api/')) {
        const endpoint = pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204 })
        return route.fulfill({ json: fixture[endpoint] || (endpoint.startsWith('chat') ? fixture.chat : {}) })
      }
      if (!pathname.startsWith('/chat/') || pathname.endsWith('/sw.js')) return route.fulfill({ status: 404 })
      const file = path.resolve(DIST, pathname.slice('/chat/'.length) || 'index.html')
      if (!file.startsWith(DIST + path.sep)) return route.fulfill({ status: 404 })
      const body = await fs.readFile(file).catch(error => {
        if (error.code !== 'ENOENT') throw error
        return null
      })
      if (!body) return route.fulfill({ status: 404 })
      return route.fulfill({ body, contentType: MIME[path.extname(file)] || 'application/octet-stream' })
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })

    const settings = page.getByRole('dialog', { name: 'Settings', exact: true })
    const heading = settings.locator('.preferences-content-heading h3')
    const openSettings = async width => {
      await page.setViewportSize({ width, height: 800 })
      await page.goto(`${ORIGIN}/chat/`)
      await page.locator('.chat-panel[data-pane="%1"]').first().waitFor()
      if (width < 800) await page.getByRole('button', { name: 'Open sessions', exact: true }).click()
      await page.getByRole('button', { name: 'Settings', exact: true }).click()
      await settings.waitFor()
    }

    await openSettings(1280)
    const aside = settings.locator('aside').filter({ has: page.locator('nav.preferences-nav') })
    const nav = aside.locator('nav.preferences-nav')
    const general = nav.getByRole('button', { name: 'General', exact: true })
    await heading.filter({ hasText: /^General$/ }).waitFor()
    assert.equal(await general.getAttribute('aria-current'), 'page')
    assert.deepEqual(await nav.locator('h3.preferences-section:visible').allTextContents(), ['Tether', 'Agents', 'More'])
    assert.equal(await nav.getByRole('button').count(), 14)
    const dimensions = await aside.evaluate(el => ({ scroll: el.scrollHeight, client: el.clientHeight, top: el.scrollTop }))
    assert(dimensions.scroll <= dimensions.client && dimensions.top === 0, JSON.stringify(dimensions))
    console.log('ok 1280x800: General opens, and all 14 buttons fit under three section labels')

    const search = settings.getByRole('searchbox', { name: 'Search settings' })
    const folder = settings.getByRole('region', { name: 'Default folder', exact: true })
    for (const [query, title, clear] of [
      ['folder', 'General', () => search.press('Escape')],
      ['default folder', 'Sidebar & chat', () => settings.getByRole('button', { name: 'Clear search', exact: true }).click()],
    ]) {
      const active = nav.getByRole('button', { name: title, exact: true })
      if (title !== 'General') await active.click()
      await search.fill(query)
      await settings.getByRole('status').filter({ hasText: `Results for “${query}”` }).waitFor()
      await folder.getByRole('button', { name: 'General', exact: true }).waitFor()
      await clear()
      await heading.and(settings.getByRole('heading', { name: title, exact: true })).waitFor()
      assert.equal(await search.inputValue(), '')
      assert.equal(await active.getAttribute('aria-current'), 'page')
    }
    console.log('ok search: folder and default folder find the General result, and Escape and Clear restore the page')

    // A search result button leaves the screen when you choose it: focus goes to the new page's title (WCAG 2.4.3).
    const focusedHeading = title => page.waitForFunction(name => document.activeElement?.matches('.preferences-content-heading h3') && document.activeElement.textContent === name, title)
    await nav.getByRole('button', { name: 'Voice', exact: true }).click()
    await search.fill('default folder')
    await folder.getByRole('button', { name: 'General', exact: true }).press('Enter')
    await focusedHeading('General')
    assert.equal(await search.inputValue(), '')
    console.log('ok 1280px: Enter on a search result focuses the page title')

    // A resize that hides the focused control moves focus to the page title, not to the page body.
    await nav.getByRole('button', { name: 'Voice', exact: true }).press('Enter')
    await heading.and(settings.getByRole('heading', { name: 'Voice', exact: true })).waitFor()
    await page.setViewportSize({ width: 320, height: 800 })
    await focusedHeading('Voice')
    console.log('ok resize 1280 -> 320 px: focus moves from the hidden nav to the page title')

    await openSettings(320)
    const index = settings.locator('.preferences-index nav.preferences-nav')
    await index.waitFor()
    assert.deepEqual(await index.locator('h3.preferences-section:visible').allTextContents(), ['Tether', 'Agents', 'More'])
    const rows = await index.getByRole('button').all()
    assert.equal(rows.length, 14)
    for (const row of rows) {
      const description = row.locator('small')
      assert(await description.isVisible() && (await description.textContent()).trim(), 'Each index row shows a description')
    }
    assert.equal(await heading.count(), 0)
    const voice = index.locator('[data-group="voice"]')
    await voice.click()
    await page.waitForFunction(() => document.activeElement?.matches('.preferences-content-heading h3') && document.activeElement.textContent === 'Voice')
    assert.equal(await heading.getAttribute('tabindex'), '-1')
    await settings.getByRole('button', { name: 'All settings', exact: true }).click()
    await page.waitForFunction(() => document.activeElement?.matches('.preferences-index [data-group="voice"]'))
    console.log('ok 320px: 14 described index rows, Voice heading focus, and focus return to the Voice row')
    await voice.click()
    await focusedHeading('Voice')
    await search.fill('microphone')
    await settings.getByRole('region', { name: 'Voice button', exact: true }).getByRole('button', { name: 'Voice', exact: true }).press('Enter')
    await focusedHeading('Voice')
    await page.keyboard.press('Tab')
    assert(await page.evaluate(() => !!document.activeElement?.closest('.preferences-content') && !document.activeElement.matches('input[type="search"]')), 'Tab after the title stays in the page')
    console.log('ok 320px: Enter on a search result focuses the page title, and Tab moves into the page')

    // A deep link on a phone opens on the page and focuses its title.
    await page.goto(`${ORIGIN}/chat/?go=notifications`)
    await focusedHeading('Notifications')
    console.log('ok 320px: ?go=notifications focuses the Notifications title')

    await openSettings(320)
    await index.locator('[data-group="voice"]').focus()
    await page.setViewportSize({ width: 1280, height: 800 })
    await focusedHeading('General')
    console.log('ok resize 320 -> 1280 px: focus moves from the hidden index row to the page title')
    assert.deepEqual(errors, [])
    console.log('all passed')
  } catch (error) {
    if (errors.length) console.error(errors.join('\n'))
    throw error
  } finally {
    await page?.close()
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
