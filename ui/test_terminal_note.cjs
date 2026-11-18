// Run against an existing build. Every request stays in the browser fixture.
const assert = require('node:assert/strict')
const http = require('node:http')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const dist = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const thread = 'terminal-note-fixture'
const pane = { id: '%45', tmux_session: '$1', label: 'Terminal note fixture', pane_name: 'Terminal note fixture',
  agent: 'claude', dir: '/fixture', state: 'Idle', writable: true, thread, approval: null }
const record = (id, text, role = 'user') => ({ id, role, text, time: new Date().toISOString() })

async function main() {
  const server = http.createServer((req, res) => { res.writeHead(404); res.end() })
  const browser = await getBrowser({ local: true })
  const context = await browser.newContext({ viewport: { width: 320, height: 740 } })
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    const page = await context.newPage()
    page.setDefaultTimeout(8000)
    const errors = [], writes = []
    page.on('pageerror', error => errors.push(error.message))
    let chat, holdChat
    await page.addInitScript(() => {
      navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
      if (!new URLSearchParams(location.search).has('idle-stream')) return
      // An unchanged idle stream sends a version acknowledgment and no new chat data.
      window.EventSource = class {
        constructor(url) { this.version = new URL(url, location.href).searchParams.get('have') }
        addEventListener(name, handler) {
          if (name === 'same') queueMicrotask(() => handler({ data: JSON.stringify({ version: this.version }) }))
        }
        close() {}
      }
    })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== origin) return route.abort('blockedbyclient')
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Seed fixture</title>' })
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404 })
      if (url.pathname === '/terminal/live/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Fixture terminal</title>' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204 })
        if (['message', 'key', 'unqueue'].includes(endpoint)) {
          writes.push([endpoint, request.postDataJSON()])
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'chat') {
          if (holdChat) await holdChat()
          return route.fulfill({ json: chat })
        }
        const responses = {
          sessions: { sessions: [{ ...pane, agent: chat.agent, thread: chat.thread, state: chat.state }], default_thread: chat.thread },
          prefs: { prefs: {}, version: 1 }, 'work/board': { revision: 1, projects: [], items: [], stages: [] },
          'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
          undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
          update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test fixture', office: false },
          catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: pane.id, running: 0, runs: [] },
          subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
          'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
          'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
        }
        return route.fulfill({ json: responses[endpoint] ?? {} })
      }
      if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404 })
      const file = url.pathname === '/chat/' ? path.join(dist, 'index.html') : path.resolve(dist, '.' + url.pathname.slice('/chat'.length))
      assert.ok(file.startsWith(dist + path.sep))
      return route.fulfill({ path: file })
    })

    const composer = page.locator('.composer textarea:enabled')
    const chip = page.getByRole('button', { name: /^(\/\S+|\d+ commands) ran in the terminal$/ })
    const queue = page.getByRole('button', { name: '1 queued', exact: true })
    const note = page.locator('.terminal-note')
    const live = page.locator('.chat-panel > .visually-hidden[role="status"]')
    const readPending = () => page.evaluate(id => JSON.parse(localStorage.getItem('tether.pending') || '{}')[id], pane.id)
    const cleared = () => page.waitForFunction(id => !JSON.parse(localStorage.getItem('tether.pending') || '{}')[id], pane.id)
    const open = async ({ text = '/context', item = {}, data = {}, idleStream = false, age = 10000 } = {}) => {
      chat = { version: 'fixture', messages: [], busy: false, thread, agent: pane.agent, writable: true,
        session: pane.id, earlier: 0, limit: 40, state: pane.state, approval: null, ...data }
      await page.goto(origin)
      await page.evaluate(({ id, text, thread, item, age }) => {
        localStorage.setItem('tether.pending', JSON.stringify({ [id]: [{ id: 1, text, key: text, seen: 0, thread, at: Date.now() - age, ...item }] }))
        localStorage.setItem('tmux-chat.prefs.synced', '1')
        localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: id, voiceOff: true, voiceSound: false }))
      }, { id: pane.id, text, thread, item, age })
      await page.goto(`${origin}/chat/?session=%2545${idleStream ? '&idle-stream' : ''}`)
      await composer.waitFor()
    }

    await open()
    assert.equal(await chip.textContent(), '/context ran in the terminal', 'The collapsed chip names the command')
    await chip.click()
    assert.equal(await queue.count(), 0)
    assert.match(await note.innerText(), /Ran in the terminal\. No chat output\./)
    assert.equal(await live.textContent(), '/context ran in the terminal. It left no chat output.')
    assert.deepEqual((await readPending())[0].terminal, { lastId: null })
    const openTerminal = note.getByRole('button', { name: /^Open terminal to see \/context/ })
    const dismiss = command => note.getByRole('button', { name: `Dismiss note: ${command} ran in the terminal` })
    for (const button of [openTerminal, dismiss('/context')]) {
      const box = await button.boundingBox()
      assert.ok(box && box.width >= 24 && box.height >= 24 && box.x >= 0 && box.x + box.width <= 320, 'Note controls fit at 320 px and have a 24 px target')
    }
    assert.equal(await note.evaluate(element => getComputedStyle(element).opacity), '1')
    await openTerminal.press('Enter')
    await page.getByRole('button', { name: 'Back to chat', exact: true, pressed: true }).waitFor()
    await page.waitForFunction(() => document.activeElement?.matches('.terminal-toggle[aria-pressed="true"]'))
    await page.getByTitle('Live terminal', { exact: true }).waitFor()
    await cleared()
    assert.equal(await live.textContent(), '')
    console.log('Claude /context has its own status item, announces the result, and opens the terminal with focus on Back to chat')

    await page.setViewportSize({ width: 1280, height: 900 })
    const unrelatedCards = [
      ...['info', 'menu'].map(reason => ({ name: reason, data: { attention: { id: 'a', reason, text: 'Fixture card' } } })),
      { name: 'approval', data: { approval: { id: 'a', text: 'Fixture approval', actionable: false } } },
      { name: 'plan', data: { plan: { id: 'p', text: 'Fixture plan' } } },
    ]
    const clears = [
      { name: 'transcript record', data: { messages: [record('sent', '/context')] } },
      { name: 'panel opened while working', data: { state: 'Working', attention: { id: 'a', reason: 'panel', text: 'Fixture card' } } },
      ...unrelatedCards.map(card => ({ name: `idle ${card.name}`, data: card.data })),
      { name: 'thread change', text: '/clear', data: { thread: 'new-thread' } },
      { name: 'first thread of the pane', text: '/clear', item: { thread: null }, data: { thread: 'first-thread' } },
      ...['Unknown', 'Unavailable', 'Approval'].map(state => ({ name: `${state} state`, data: { state } })),
      { name: 'idle side question', text: '/btw What now?' },
      { name: 'answered side question while working', text: '/btw What now?', data: { state: 'Working',
        messages: [{ ...record('side', 'Fixture side answer', 'assistant'), kind: 'side', title: 'What now?' }] } },
      { name: 'expired note', item: { terminal: { lastId: null } }, age: 31 * 60 * 1000 },
    ]
    for (const scenario of clears) {
      await open(scenario)
      await cleared()
      assert.equal(await chip.count(), 0, scenario.name)
    }
    // A card that the command did not open (an approval, a menu) must not clear a command still queued behind the work.
    // A bare /btw is not a side question; it keeps the normal queue rule while the agent works, also after 10 s.
    for (const scenario of [{ data: { state: 'Working' } }, { text: 'Normal prompt' }, { text: '/btw', data: { state: 'Working' }, age: 11000 }, ...unrelatedCards.map(card => ({ data: { ...card.data, state: 'Working' } }))]) {
      await open(scenario)
      await queue.waitFor()
      assert.equal(await chip.count(), 0)
      assert.equal((await readPending())[0].terminal, undefined)
    }
    await open({ item: { failed: 'Offline', offline: true } })
    await page.getByRole('button', { name: '1 not sent', exact: true }).waitFor()
    assert.equal(await chip.count(), 0)
    assert.equal((await readPending())[0].failed, 'Offline')
    console.log('Records, cards, thread changes, /btw, and expiry clear silently. Working commands, failed sends, and normal prompts keep their queue behavior')

    await open({ text: '/status fast', data: { agent: 'codex' } })
    assert.equal(await chip.textContent(), '/status ran in the terminal')
    await chip.click()
    assert.equal(await queue.count(), 0)
    await note.getByRole('button', { name: 'Open terminal to see /status', exact: true }).waitFor()
    assert.match(await note.innerText(), /\/status fast/)

    await open()
    await chip.click()
    await dismiss('/context').press('Enter')
    await chip.waitFor({ state: 'detached' })
    await cleared()
    assert.deepEqual(writes, [], 'Opening and dismissing a note send no keys and do not unqueue')

    const tail = record('tail', 'Previous reply', 'assistant')
    for (const messages of [[tail, record('next', 'New reply', 'assistant')], [record('late', '/context'), tail]]) {
      await open({ data: { messages: [tail] } })
      await chip.waitFor()
      assert.deepEqual((await readPending())[0].terminal, { lastId: tail.id })
      chat = { ...chat, messages }
      await chip.waitFor({ state: 'detached' })
      await cleared()
    }
    await open()
    await chip.waitFor()
    await page.reload()
    await chip.waitFor()
    await composer.fill('Next prompt')
    await page.getByRole('button', { name: 'Send message', exact: true }).click()
    await chip.waitFor({ state: 'detached' })
    await queue.waitFor()
    assert.deepEqual((await readPending()).map(item => item.text), ['Next prompt'])
    assert.deepEqual(writes, [['message', { session: pane.id, thread, message: 'Next prompt' }]])
    console.log('Codex /status fast also shows a note. Dismiss, new chat activity, a late record, and a new send remove notes')

    const start = Date.now()
    await page.clock.install({ time: start })
    await page.clock.pauseAt(start + 1000)
    await open({ idleStream: true, age: 1000 })
    await queue.waitFor()
    const freshRead = Promise.withResolvers(), freshResponse = Promise.withResolvers()
    let freshReads = 0
    holdChat = () => { freshReads++; freshRead.resolve(); return freshResponse.promise }
    try {
      const request = page.waitForRequest(req => new URL(req.url()).pathname === '/mobile/api/chat')
      await page.clock.runFor(8000)
      await request
      await freshRead.promise
      assert.equal(await chip.count(), 0, 'Old idle data must not mark a command')
      assert.equal(freshReads, 1, 'Recheck an idle stream with a single fresh read')
    } finally {
      holdChat = undefined
      freshResponse.resolve()
    }
    await chip.waitFor()
    await page.clock.fastForward(30 * 60 * 1000)
    await chip.waitFor({ state: 'detached' })
    await cleared()
    assert.deepEqual(errors, [], 'No uncaught browser exceptions')
    console.log('An unchanged idle stream needs one fresh read. A marked note expires at the 30-minute cap. No page errors')
  } finally {
    await context.close()
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
