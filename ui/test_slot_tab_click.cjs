// A tab clicked inside a split slot opens in that slot; the split stays.
// Serve the build in ui/dist (or DIST) through request routes. Every API request uses fixtures.
const { strict: assert } = require('node:assert')
const { mkdirSync } = require('node:fs')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const origin = 'http://127.0.0.1:5142'
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
// Two sessions. The split shows Alpha (session A) and Gamma (session B, placed by the user as a guest).
const panes = [['Alpha', '$a'], ['Beta', '$a'], ['Gamma', '$b'], ['Delta', '$b']].map(([name, session], i) => ({ id: `%${i + 1}`, tmux_session: session,
  label: session === '$a' ? 'Session A' : 'Session B', pane_name: name, agent: 'claude', state: 'Idle', dir: '/srv/fixture', thread: `thread-${i}`, writable: true }))

async function main() {
  const browser = await getBrowser()
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' })
  try {
    const page = await context.newPage(), errors = [], writes = []
    page.setDefaultTimeout(15000)
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') { writes.push(endpoint); return route.fulfill({ json: { ok: true } }) }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        const pane = panes.find(item => item.id === url.searchParams.get('session')) || panes[0]
        const fixtures = {
          sessions: { sessions: panes, default_thread: panes[0].thread },
          chat: { messages: [], earlier: 0, limit: 40, session: pane.id, thread: pane.thread, agent: pane.agent, state: 'Idle', writable: true },
          footer: { observed: true }, prefs: { prefs: {}, version: 1 }, host: { name: 'Fixture' },
          'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
          catalog: { commands: [], plugins: [], mcp: [] }, usage: { claude: null, codex: null },
          subagents: { main: {}, agents: [], tasks: [] }, 'codex-runs': { runs: [] },
          'browser/agents': { enabled: false, agents: [], asks: [] },
          'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
          'work/board': { revision: 1, projects: [], items: [], stages: [] },  // Home and the sidebar read the work board
        }
        return route.fulfill({ json: fixtures[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/') && request.method() === 'GET') {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
        if (file !== path.join(DIST, 'index.html') && !file.startsWith(DIST + path.sep)) return route.fulfill({ status: 404, body: '' })
        return route.fulfill({ path: file, headers: { 'Cache-Control': 'no-store' } }).catch(() => route.fulfill({ status: 404, body: '' }))
      }
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', voiceOff: true, voiceSound: false,
        layout: { desktop: { sidebar: false, inbox: false }, active: '$a', desks: { $a: { count: 2, panes: ['%1', '%3'], guests: ['%3'], focus: 0 } } },
      }))
      // Model a tablet with a trackpad. Keep the real width and reduced-motion queries.
      const media = window.matchMedia.bind(window)
      window.matchMedia = query => media(query === '(min-width: 900px) and (hover: hover) and (pointer: fine)' ? '(min-width: 900px)' : query)
      navigator.vibrate = ms => { window.vibrated = ms; return true }
      navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
    })
    await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'domcontentloaded', timeout: 45000 })
    await page.locator('.slot').nth(1).waitFor()
    const labels = () => page.locator('.slot .chat-title h1').allInnerTexts()
    const tab = (slot, name) => page.locator(`.slot[data-slot="${slot}"] .pane-tabs [role="tab"]`).filter({ hasText: name })
    const settle = want => page.waitForFunction(want => [...document.querySelectorAll('.slot .chat-title h1')].map(el => el.innerText).join('|') === want.join('|'), want)
    assert.deepEqual(await labels(), ['Alpha', 'Gamma'])

    // The guest slot: a sibling tab of another session opens in that slot, not as its own full-width layout.
    await tab(1, 'Delta').click()
    await settle(['Alpha', 'Delta'])
    assert.equal(await page.locator('.slot').count(), 2, 'The split stays')
    assert.equal(await page.locator('.slot[data-slot="1"].focused').count(), 1, 'The clicked slot has the focus')
    console.log('Split: a tab of the guest session opens in its slot')

    // The first slot: a tab of the layout session opens in that slot.
    await tab(0, 'Beta').click()
    await settle(['Beta', 'Delta'])
    assert.equal(await page.locator('.slot[data-slot="0"].focused').count(), 1)
    console.log('Split: a tab of the layout session opens in its slot')

    // Back to the guest slot: its first tab opens there again, and the first slot keeps Beta.
    await tab(1, 'Gamma').click()
    await settle(['Beta', 'Gamma'])
    assert.equal(await page.locator('.slot').count(), 2)
    console.log('Split: the slots stay after each tab click')
    assert.deepEqual(errors, [])
  } finally {
    await context.close()
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
