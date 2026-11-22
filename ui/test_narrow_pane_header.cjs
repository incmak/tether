// B378 workspace header interactions against an existing build. All requests use fixtures.
const assert = require('node:assert/strict')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const dist = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const origin = 'http://127.0.0.1:5378'
const session = '$fixture'
const panes = ['Alpha', 'Beta', 'Gamma', 'Delta'].map((name, index) => ({
  id: `%${index + 1}`, tmux_session: session, label: 'Header fixture', pane_name: name,
  agent: 'claude', dir: '/fixture', state: 'Idle', writable: true, thread: `thread-${index}`, approval: null,
}))
const selected = panes[0]
const chatFailure = 'Fixture conversation unavailable'
const savedPrefs = { prefs: {}, version: 1 }

async function main() {
  const browser = await getBrowser({ local: true })
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, hasTouch: false, serviceWorkers: 'block' })
    const page = await context.newPage()
    page.setDefaultTimeout(8000)
    assert.ok(await page.evaluate(() => matchMedia('(min-width: 900px) and (hover: hover) and (pointer: fine)').matches),
      'The browser must match the real desktop workspace media query at 1280×800')

    const errors = [], writes = []
    let failChat = false
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== origin) return route.abort('blockedbyclient')
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Seed fixture</title>' })
      if (url.pathname === '/terminal/live/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Fixture terminal</title>' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (request.method() !== 'GET') {
          writes.push({ endpoint, method: request.method(), body: request.postData() })
          return route.fulfill({ json: endpoint === 'prefs' ? savedPrefs : { ok: true } })
        }
        if (endpoint === 'stream') return route.fulfill({ status: 204 })
        const pane = panes.find(item => item.id === url.searchParams.get('session')) ?? selected
        if (endpoint === 'chat') {
          if (failChat && pane === selected) return route.fulfill({ status: 500, json: { error: chatFailure } })
          return route.fulfill({ json: { version: 'fixture', messages: [], busy: false, session: pane.id,
            thread: pane.thread, agent: pane.agent, state: pane.state, writable: pane.writable, earlier: 0, limit: 40, approval: null } })
        }
        const responses = {
          sessions: { sessions: panes, default_thread: selected.thread }, prefs: savedPrefs,
          footer: { observed: true }, host: { name: 'Fixture' }, 'projects/marks': {}, 'projects/backlog': { text: '' },
          'side-shells': { shells: [] }, 'work/board': { revision: 1, projects: [], items: [], stages: [] },
          undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
          catalog: { commands: [], plugins: [], mcp: [] }, usage: { claude: null, codex: null },
          subagents: { main: {}, agents: [], tasks: [] }, 'codex-runs': { runs: [] },
          'browser/agents': { enabled: false, available: false, agents: [], asks: [] },
          'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
        }
        return route.fulfill({ json: responses[endpoint] ?? {} })
      }
      if (!url.pathname.startsWith('/chat/') || request.method() !== 'GET' || url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404 })
      const file = url.pathname === '/chat/' ? path.join(dist, 'index.html') : path.resolve(dist, '.' + url.pathname.slice('/chat'.length))
      assert.ok(file.startsWith(dist + path.sep))
      return route.fulfill({ path: file })
    })

    const slot = '.workspace .slot[data-slot="0"]'
    const panel = page.locator(`${slot} > .chat-panel`)
    const header = panel.locator('.chat-header')
    const tools = {
      search: header.locator('.search-toggle'), terminal: header.locator('.terminal-toggle'), pin: header.locator('.side-pin-toggle'),
      badge: header.locator('.slot-badge'), swap: header.locator('.slot-pick'), zoom: header.locator('.slot-zoom'),
    }
    const keys = header.locator('.keys-container > button')
    const menu = header.locator('.keys-menu')
    const menuItem = name => menu.getByRole('button', { name, exact: true })
    const picker = page.getByRole('dialog', { name: 'Show in slot 1', exact: true })
    const liveTerminal = panel.getByTitle('Live terminal', { exact: true })
    const focused = locator => locator.evaluate(element => document.activeElement === element)
    const open = async (count, { cols = {}, row = 0.5, error = false } = {}) => {
      failChat = error
      const prefs = { selected: selected.id, voiceOff: true, voiceSound: false, uiUsage: false,
        layout: { desktop: { sidebar: false, inbox: false }, active: session,
          desks: { [session]: { count, cols, row, panes: panes.slice(0, count).map(pane => pane.id), focus: 0 } } },
      }
      await page.goto(origin)
      await page.evaluate(prefs => {
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify(prefs))
      }, prefs)
      await page.goto(`${origin}/chat/?session=${encodeURIComponent(selected.id)}`)
      await panel.waitFor()
      assert.equal(await page.locator('.workspace .slot').count(), count, 'The fixture has the requested number of slots')
      if (error) await panel.getByRole('alert').filter({ hasText: chatFailure }).waitFor()
      else await header.locator('.keys-container > button:enabled').waitFor()
    }
    const widthIn = async (scenario, min, max = Infinity) => {
      const width = await panel.evaluate(element => element.getBoundingClientRect().width)
      assert.ok(width > min && width <= max, `${scenario}: .chat-panel is ${width}px wide, expected > ${min}px and <= ${max}px`)
      return width.toFixed(1)
    }
    const show = async (scenario, expected) => {
      for (const [name, visible] of Object.entries(expected)) {
        const display = await tools[name].evaluate(element => getComputedStyle(element).display)
        assert.equal(display !== 'none', visible, `${scenario}: ${name} must be ${visible ? 'visible' : 'hidden'} (display: ${display})`)
      }
    }
    const assertMenuInViewport = async scenario => {
      const fit = await menu.evaluate(element => {
        const { left, right, top, bottom } = element.getBoundingClientRect()
        return { open: element.matches(':popover-open'), left, right, top, bottom, width: innerWidth, height: innerHeight }
      })
      assert.ok(fit.open, `${scenario}: the … menu must be :popover-open`)
      assert.ok(fit.left >= 0 && fit.top >= 0 && fit.right <= fit.width && fit.bottom <= fit.height,
        `${scenario}: all menu edges must be inside the viewport: ${JSON.stringify(fit)}`)
    }
    const scrollToMenuButton = async button => {
      const result = await button.evaluate(element => {
        const menu = element.closest('.keys-menu')
        const list = menu.getBoundingClientRect(), before = element.getBoundingClientRect()
        // Scroll the menu itself so scrolling an overflow-hidden grid slot cannot mask clipping.
        menu.scrollBy({ top: (before.top + before.bottom - list.top - list.bottom) / 2, behavior: 'instant' })
        const { left, right, top, bottom } = element.getBoundingClientRect()
        const x = (left + right) / 2, y = (top + bottom) / 2
        const inside = left >= 0 && right <= innerWidth && top >= 0 && bottom <= innerHeight &&
          left >= list.left && right <= list.right && top >= list.top && bottom <= list.bottom
        return { label: element.textContent.trim(), inside, hit: element.contains(document.elementFromPoint(x, y)), x, y }
      })
      assert.ok(result.inside, `“${result.label}” must scroll fully into view`)
      assert.ok(result.hit, `“${result.label}” must be hit-testable at (${result.x}, ${result.y})`)
      return result
    }
    const activateMenuItem = async name => {
      await keys.focus()
      await page.keyboard.press('Enter')
      const item = menuItem(name)
      await item.waitFor()
      const tabStops = await menu.getByRole('button', { disabled: false }).count()
      for (let index = 0; index < tabStops && !await focused(item); index++) await page.keyboard.press('Tab')
      assert.ok(await focused(item), `Tab must reach “${name}” from the … button`)
      await page.keyboard.press('Enter')
    }
    const closePickerWithEscape = async () => {
      await picker.waitFor()
      await page.keyboard.press('Escape')
      await picker.waitFor({ state: 'hidden' })
    }

    await open(3)
    const narrow = await widthIn('Search from the menu', 320, 420)
    await show('Loaded narrow slot', { search: false, terminal: false, pin: false })
    await keys.click()
    // Each visible menu row is on top where it shows: no tab-strip × paints over the menu (header z-index).
    const covered = await menu.evaluate(element => [...element.querySelectorAll('button')].filter(button => getComputedStyle(button).display !== 'none').map(button => {
      const box = button.getBoundingClientRect(), list = element.getBoundingClientRect()
      if (box.bottom > list.bottom || box.top < list.top) return null
      return [0.1, 0.5, 0.9].some(x => { const hit = document.elementFromPoint(box.left + box.width * x, box.top + box.height / 2); return !hit || !button.contains(hit) }) ? button.textContent : null
    }).filter(Boolean))
    assert.deepEqual(covered, [], 'Nothing paints over the open … menu')
    await menuItem('Search this chat').click()
    const dialog = panel.getByRole('dialog', { name: 'Search this chat' })
    await dialog.waitFor()
    await menu.waitFor({ state: 'hidden' })
    await page.keyboard.press('Escape')
    await dialog.waitFor({ state: 'hidden' })
    assert.ok(await focused(keys), 'Escape from Search this chat must return focus to the … button')
    console.log(`ok search dialog opens and Escape restores menu button focus (${narrow}px slot)`)

    await widthIn('Terminal from the menu', 320, 420)
    await keys.click()
    await menuItem('Open terminal').click()
    await liveTerminal.waitFor()
    const back = header.getByRole('button', { name: 'Back to chat', exact: true, pressed: true })
    await back.waitFor()
    await page.waitForFunction(slot => document.activeElement?.matches(`${slot} .terminal-toggle[aria-pressed="true"]`), slot)
    await menu.waitFor({ state: 'hidden' })
    await keys.click()
    await menuItem('Back to chat').click()
    await liveTerminal.waitFor({ state: 'detached' })
    await menu.waitFor({ state: 'hidden' })
    assert.ok(await focused(keys), 'Returning to chat must restore focus to the … button')
    console.log(`ok terminal opens with focus on the pressed header toggle and closes with focus on the … button (${narrow}px slot)`)

    await widthIn('Keyboard activation', 393, 395)
    const [pinResponse] = await Promise.all([
      page.waitForResponse(response => {
        const request = response.request()
        return new URL(response.url()).pathname === '/mobile/api/prefs' && request.method() === 'POST' &&
          Object.hasOwn(request.postDataJSON().changes ?? {}, 'sidePins')
      }),
      activateMenuItem('Pin to side tab'),
    ])
    await menu.waitFor({ state: 'hidden' })
    const focusAfterPin = await focused(keys)
    assert.deepEqual(pinResponse.request().postDataJSON().changes.sidePins, [{ pane: selected.id }],
      'Pin to side tab must save the selected pane in the mocked prefs request')

    await activateMenuItem('Open terminal')
    await liveTerminal.waitFor()
    await page.waitForFunction(slot => document.activeElement?.matches(`${slot} .terminal-toggle[aria-pressed="true"]`), slot)
    assert.ok(await focused(back), 'Opening the terminal with Enter must focus the pressed terminal toggle')
    await page.keyboard.press('Enter')
    await liveTerminal.waitFor({ state: 'detached' })
    await show('Keyboard Back to chat', { terminal: false })
    const focusAfterTerminal = await focused(keys)
    assert.deepEqual({ pin: focusAfterPin, backToChat: focusAfterTerminal }, { pin: true, backToChat: true },
      'Pin to side tab and the pressed Back to chat toggle must each restore focus to the … button')
    // At this width the picker control is in the header. It folds into the menu at 320px or less.
    await tools.swap.focus()
    await page.keyboard.press('Enter')
    await closePickerWithEscape()
    console.log(`ok Enter pins the pane and toggles the terminal with focus return (${narrow}px slot)`)

    // The composer unmounts when the shortcut opens the terminal. The last focus was outside the header.
    const message = panel.locator('form.composer textarea')
    await keys.focus()
    await message.focus()
    assert.ok(await focused(message) && !await menu.isVisible(), 'Start the shortcut with focus in the message box and the menu closed')
    await page.keyboard.press('Alt+t')
    await liveTerminal.waitFor()
    await message.waitFor({ state: 'detached' })
    assert.ok(!await focused(keys), 'Alt+T from the message box must leave focus away from the … button')
    console.log('ok Alt+T from the message box does not move focus to the … button')

    await page.setViewportSize({ width: 1280, height: 500 })
    await open(4, { cols: { 4: [0.25] }, row: 0.15 }) // MIN_SHARE in workspace.ts
    await widthIn('Short grid slot', 0, 320)
    assert.equal(await page.getByRole('separator', { name: 'Resize rows' }).getAttribute('aria-valuenow'), '15',
      'The short grid starts with the minimum top-row share')
    await keys.click()
    await menu.waitFor()
    for (const button of await menu.getByRole('button').all()) await scrollToMenuButton(button)
    await assertMenuInViewport('Short grid row')
    const zoom = await scrollToMenuButton(menuItem('Zoom this slot Alt+Z'))
    await page.mouse.click(zoom.x, zoom.y)
    const showAllSlots = header.getByRole('button', { name: 'Show all slots (Alt+Z)', exact: true })
    await showAllSlots.waitFor()
    assert.equal(await page.locator('.workspace .slot').count(), 1, 'A mouse click in the short-row menu zooms the selected slot')
    await showAllSlots.click()
    await page.getByRole('separator', { name: 'Resize rows' }).waitFor()
    await keys.click()
    await menu.waitFor()
    const beforeResize = await menu.boundingBox()
    await page.setViewportSize({ width: 1100, height: 460 })
    // The left column gets narrower, so the right-aligned menu must move with its button.
    await page.waitForFunction(({ slot, previousLeft }) => {
      const box = document.querySelector(`${slot} .keys-menu`)?.getBoundingClientRect()
      return box && box.left < previousLeft - 1
    }, { slot, previousLeft: beforeResize.x })
    await assertMenuInViewport('Resized short grid row')
    const anchor = await keys.boundingBox(), resized = await menu.boundingBox()
    assert.ok(Math.abs(resized.y - (anchor.y + anchor.height)) <= 8 || Math.abs(resized.y + resized.height - anchor.y) <= 8,
      `The menu must stay within 8px above or below its … button after resize: ${JSON.stringify({ anchor, resized })}`)
    console.log('ok all short-row menu buttons receive hits, a mouse click zooms, and resize preserves the anchor')

    await page.setViewportSize({ width: 1280, height: 800 })
    // Four slots form a 2×2 grid. Set an unequal column split to reach the smallest tier at the same viewport.
    await open(4, { cols: { 4: [0.25] } })
    const smallest = await widthIn('Slot actions from the menu', 0, 320)
    await show('Smallest slot', { badge: false, swap: false, zoom: false })
    await activateMenuItem('Show another pane here')
    await closePickerWithEscape()
    console.log(`ok Enter opens the slot picker and Escape closes it (${smallest}px slot)`)

    await keys.click()
    await menuItem('Show another pane here').waitFor()
    await assertMenuInViewport('Smallest grid slot')
    await scrollToMenuButton(menu.getByRole('button').last())
    await menuItem('Zoom this slot Alt+Z').click()
    await showAllSlots.waitFor()
    assert.equal(await page.locator('.workspace .slot').count(), 1, 'Zoom only shows the selected slot')
    const zoomed = await widthIn('Zoomed slot', 420)
    await show('Zoomed slot', { search: true })
    assert.ok(await focused(keys), 'Zoom must restore focus to the … button')
    console.log(`ok slot actions fold at ${smallest}px and Zoom restores the header controls at ${zoomed}px`)

    await open(3, { error: true })
    const failed = await widthIn('Failed chat request', 320, 420)
    await show('Failed chat request', { search: true, terminal: true })
    assert.ok(await keys.isDisabled(), 'The … button must be disabled when chat data is unavailable')
    assert.ok(await tools.search.isDisabled(), 'Search needs chat data')
    await tools.terminal.click()
    await liveTerminal.waitFor()
    console.log(`ok HTTP 500 keeps the search and terminal controls visible and the terminal opens (${failed}px slot)`)

    await open(2)
    const wide = await widthIn('Wide slot', 420)
    await show('Wide slot', { search: true, terminal: true })
    await keys.click()
    await menu.waitFor()
    assert.deepEqual(await menu.locator('.keys-fold, .keys-fold-slot').evaluateAll(items => items.map(item => getComputedStyle(item).display)),
      ['none', 'none', 'none', 'none'], 'The wide menu hides all four folded items')
    console.log(`ok a wide slot shows its header controls and hides the fold items (${wide}px slot)`)

    assert.deepEqual(writes.filter(write => write.endpoint !== 'prefs'), [], 'Header actions send no API writes except local fixture preference sync')
    assert.deepEqual(errors, [], 'No uncaught browser exceptions')
    console.log('9 scenarios passed. No terminal writes or uncaught browser exceptions.')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
