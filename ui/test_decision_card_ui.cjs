// Decision cards on Home (src/WorkItems.tsx): a tap anywhere on the card body opens the item's tab, the chip opens it
// too, an answer button only answers, a pane id that another chat now has is skipped, and a card whose tab is closed opens the item's details instead.
// Real browser against a build: DIST (default dist). Run: node test_decision_card_ui.cjs
const { strict: assert } = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS
const ORIGIN = 'http://tether.test'
const NOW = Date.now() / 1000
const pane = (id, name) => ({ id, tmux_session: '$1', label: `Triage:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/shop', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null })
const tab = (id, name) => ({ pane: id, role: 'triage', chat: '', name, session: 'Triage', stack: '', at: NOW - 60 })
const item = (id, title, tabs, asked = {}) => ({
  id, project: 'shop', title, stage: 'needs', qa: false, askFrom: 'triage', refs: [], tabs, history: [], source: '', created: NOW - 3600,
  updated: NOW - 60, doneAt: null, jira: null, version: 3, ask: { id: `a-${id}`, q: `Question for ${title}`, options: ['Go ahead', 'Hold'], pane: '', chat: '', at: NOW - 600, ...asked },
})
const fixtures = {
  sessions: { sessions: [pane('%1', 'Deploy fix'), pane('%195', 'exhibits-carry-over · needs decision')], default_thread: '' },
  chat: { messages: [], busy: false, thread: 't195', agent: 'claude', writable: true, session: '%195', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, usage: { claude: null, codex: null },
  'codex-runs': { pane: '%195', running: 0, runs: [] }, feedback: { ratings: {} },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  'work/board': {
    revision: 1, stages: [], jira: { connected: false }, feeds: {}, backlog: { items: [], error: '' },
    projects: [{ name: 'shop', prefix: 'T', folder: '', triage: 'Triage', work: '', feeds: [], jira: '', qaStatus: '', jiraUser: '' }],
    // T-020 asked from a pane id that another tab (%1, another chat) now has: the card follows the chat that asked.
    items: [item('T-020', 'exhibits-carry-over', [tab('%195', 'exhibits-carry-over · needs decision')], { pane: '%1', chat: 't195' }),
      item('T-012', 'word-import-export-diff', [tab('', 'word-import-export-diff · needs decision')])],
  },
}
const types = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.webmanifest': 'application/manifest+json' }

const contexts = []
async function open(browser, viewport) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block', hasTouch: viewport.width < 500 })
  contexts.push(context)
  const gets = [], errors = []
  await context.route(`${ORIGIN}/**`, route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.startsWith('/mobile/api/')) {
      const name = url.pathname.slice('/mobile/api/'.length)
      if (request.method() === 'POST') return route.fulfill({ json: name === 'work/answer' ? { sent: false, why: 'test' } : {} })
      gets.push(`${name}${url.search}`)
      return name === 'stream' ? route.fulfill({ status: 204 }) : route.fulfill({ json: fixtures[name] ?? {} })
    }
    if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404 })
    const file = path.join(DIST, url.pathname.slice('/chat/'.length))
    const served = fs.existsSync(file) && fs.statSync(file).isFile() ? file : path.join(DIST, 'index.html')
    return route.fulfill({ status: 200, contentType: types[path.extname(served)] ?? 'application/octet-stream', body: fs.readFileSync(served) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${ORIGIN}/chat/`)
  await page.locator('#home-group-decisions .work-card').first().waitFor()
  return { context, page, gets, errors }
}

// The title button's ::after covers the card body, so a real tap lands on it: click by position, as a finger does.
const tapOn = async (page, locator) => {
  const box = await locator.boundingBox()
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
}
// Opened: Home gives way to the chat, and the chat watches the tab (Home also loads short previews of tabs).
const openLinkedTab = async (page, id, action) => {
  await Promise.all([
    page.waitForRequest(request => {
      const { pathname, searchParams } = new URL(request.url())
      return request.method() === 'GET' && pathname.endsWith('/api/chat') && searchParams.get('session') === id && searchParams.get('watch') === '1'
    }),
    action(),
  ])
  await page.locator('#home-group-decisions').waitFor({ state: 'detached', timeout: 5000 })
}

async function main() {
  const browser = await getBrowser()
  const results = []
  const check = async (name, fn) => {
    try { await fn(); results.push(`ok ${name}`) } catch (error) { results.push(`FAIL ${name}: ${error.message.split('\n')[0]}`); process.exitCode = 1 }
    finally { await Promise.all(contexts.splice(0).map(context => context.close().catch(() => {}))) }
  }
  try {
    for (const viewport of [{ width: 320, height: 760 }, { width: 412, height: 915 }, { width: 1280, height: 860 }]) {
      const at = `${viewport.width}px`
      await check(`${at}: a tap on the question opens the linked tab`, async () => {
        const { context, page, gets, errors } = await open(browser, viewport)
        const card = page.locator('#home-group-decisions .work-card[data-item="T-020"]')
        const button = card.getByRole('button', { name: 'exhibits-carry-over, open its tab', exact: true })
        assert.equal(await button.count(), 1, `aria: ${await card.ariaSnapshot()}`)
        assert.equal(await card.getAttribute('aria-labelledby').then(id => page.locator(`[id="${id}"]`).innerText()), 'exhibits-carry-over')
        if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `decision-card-${viewport.width}.png`), fullPage: true })
        await openLinkedTab(page, '%195', () => tapOn(page, card.locator('.work-q')))
        assert.deepEqual(errors, [], errors.join(" | ") + ' :: unmocked ' + [...new Set(gets.map(g => g.split('?')[0]).filter(g => !(g in fixtures)))].join(','))
        await context.close()
      })
      await check(`${at}: the top row opens the tab, an answer button only answers`, async () => {
        const { context, page, gets } = await open(browser, viewport)
        const card = page.locator('#home-group-decisions .work-card[data-item="T-020"]')
        const before = gets.length
        const [answer] = await Promise.all([
          page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/api/work/answer')),
          card.getByRole('button', { name: 'Hold' }).click(),
        ])
        assert.ok(answer.ok(), 'Hold did not answer')
        assert.equal(await page.locator('#home-group-decisions').count(), 1, 'Hold left Home')
        assert.ok(!gets.slice(before).some(entry => entry.startsWith('chat?session=%25195') && entry.includes('watch=1')), `Hold opened the tab: ${gets.slice(before).join(', ')}`)
        await context.close()
        const second = await open(browser, viewport)
        await openLinkedTab(second.page, '%195', () => tapOn(second.page, second.page.locator('#home-group-decisions .work-card[data-item="T-020"] .work-id')))
        await second.context.close()
      })
      await check(`${at}: a card whose tab is closed opens the item details`, async () => {
        const { context, page } = await open(browser, viewport)
        const card = page.locator('#home-group-decisions .work-card[data-item="T-012"]')
        assert.equal(await card.locator('.work-tab').isDisabled(), true)
        assert.equal(await card.getByRole('button', { name: 'word-import-export-diff, show details', exact: true }).count(), 1)
        await tapOn(page, card.locator('.work-q'))
        await page.locator('.work-sheet').waitFor()
        await page.getByRole('dialog').filter({ hasText: 'T-012' }).last().waitFor()
        await context.close()
      })
      await check(`${at}: keyboard Enter on the title opens the tab`, async () => {
        const { context, page } = await open(browser, viewport)
        await page.locator('#home-group-decisions .work-card[data-item="T-020"] .work-open').focus()
        await openLinkedTab(page, '%195', () => page.keyboard.press('Enter'))
        await context.close()
      })
    }
  } finally {
    await browser.close()
  }
  console.log(results.join('\n'))
}

main().catch(error => { console.error(error); process.exit(1) })
