// Run against an existing build. Every request stays in the browser fixture.
const assert = require('node:assert/strict')
const http = require('node:http')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const dist = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const thread = 'codex-fixture'
const image = '/fixture/.cache/mobile-tmux/uploads/8a1502c4-image.png'
const prompt = 'What is the progress? I do not see any work on it here.'
const draft = `${image} ${prompt}`
const pane = { id: '%45', tmux_session: '$1', label: 'Queue fixture', pane_name: 'Queue fixture',
  agent: 'codex', dir: '/fixture', state: 'Idle', writable: true, thread, approval: null }
const otherPane = { ...pane, id: '%46', thread: 'other-pane' }
const record = (id, text, role = 'user') => ({ id, role, text, time: new Date().toISOString() })

async function main() {
  const server = http.createServer((req, res) => { res.writeHead(404); res.end() })
  const browser = await getBrowser({ local: true })
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    const page = await context.newPage()
    page.setDefaultTimeout(8000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const sends = []
    let messages = [record('image', image), record('reply', 'Here is what the screenshot shows.', 'assistant')]
    let retryMessages
    let currentThread = thread
    let currentAgent = 'codex'
    const stored = { '%45': [{ id: 1, text: draft, key: draft.slice(0, 80), seen: 0, thread }],
      '%46': [{ id: 2, text: draft, key: draft.slice(0, 80), seen: 0, thread: otherPane.thread }] }
    await page.addInitScript(initial => {
      navigator.serviceWorker.register = () => Promise.reject(new Error('Fixture disables service workers'))
      if (sessionStorage.getItem('queue-fixture')) return
      sessionStorage.setItem('queue-fixture', 'yes')
      Object.values(initial).flat().forEach(item => { item.at = Date.now() })
      localStorage.setItem('tether.pending', JSON.stringify(initial))
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%45', voiceOff: true, voiceSound: false }))
    }, stored)
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== origin) return route.abort('blockedbyclient')
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404 })
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (endpoint === 'stream') return route.fulfill({ status: 204 })
        if (endpoint === 'message') {
          const sent = request.postDataJSON()
          sends.push(sent)
          // A broken transcript stays broken: the server answers the send but the chat still has no list.
          if (Array.isArray(messages)) messages = [...messages, record('retry', sent.message)]
          return route.fulfill({ json: { ok: true } })
        }
        const responses = {
          sessions: { sessions: [{ ...pane, agent: currentAgent, thread: currentThread }, otherPane], default_thread: currentThread },
          chat: { messages: retryMessages && !url.searchParams.has('preview') ? retryMessages : messages,
            busy: false, thread: currentThread, agent: currentAgent, writable: true,
            session: pane.id, earlier: 0, limit: 100, state: pane.state, approval: null },
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

    const open = async () => {
      await page.goto(`${origin}/chat/?session=%2545`, { waitUntil: 'load' })
      await page.locator('.composer textarea:enabled').waitFor().catch(async error => {
        throw new Error(`Composer did not load. ${errors.join(' ')} ${(await page.locator('body').textContent()).slice(0, 400)}`, { cause: error })
      })
    }
    const replacePending = async items => page.evaluate(items => localStorage.setItem('tether.pending', JSON.stringify(items)), items)
    const readPending = async () => page.evaluate(() => JSON.parse(localStorage.getItem('tether.pending') || '{}'))

    await open()
    const failure = page.getByRole('button', { name: '1 not sent', exact: true })
    await failure.waitFor({ timeout: 3000 }).catch(async error => {
      const queued = page.getByRole('button', { name: '1 queued', exact: true })
      if (await queued.isVisible()) {
        await queued.click()
        assert.doesNotMatch(await page.locator('.pending-message').textContent(), /Sending…/, 'The partial send must leave Sending and preserve the unsent text for recovery')
      }
      throw error
    })
    await failure.click()
    const card = page.locator('.failed-message')
    assert.match(await card.textContent(), /Image delivered, but the message text was not sent/)
    assert.match(await card.textContent(), new RegExp(prompt.replace(/[?.]/g, '\\$&')))
    assert.equal((await readPending())['%45'][0].text, prompt)
    assert.equal((await readPending())['%46'][0].text, draft)
    await open()
    await failure.click()
    await card.getByRole('button', { name: 'Retry' }).click()
    await failure.waitFor({ state: 'detached' })
    await page.waitForFunction(() => !JSON.parse(localStorage.getItem('tether.pending') || '{}')['%45'])
    assert.deepEqual(sends, [{ session: '%45', thread, message: prompt }])

    const queued = { ...stored['%45'][0], at: Date.now() }
    const cases = [
      { name: 'complete text with attachment', messages: [record('both', `${image}\n${prompt}`)], clears: true },
      { name: 'unrelated image never clears a send', messages: [record('other', `${image.replace('8a1502c4', '12345678')}\n${prompt}`)] },
      { name: 'the same path under another thread cannot clear it', messages: [record('other-thread', `${image}\n${prompt}`)], thread: 'other-thread' },
      { name: 'no new transcript record', messages: [record('old', `${image}\n${prompt}`)], seen: 1 },
      { name: 'expired partial delivery preserves the unsent text', messages: [record('aged-image', image)], expired: true, recovers: true },
      { name: 'expired pending send without evidence expires', messages: [], expired: true, clears: true },
      { name: 'expired non-Codex pending send expires', messages: [record('claude-image', image)], agent: 'claude', expired: true, clears: true },
    ]
    for (const scenario of cases) {
      messages = scenario.messages
      currentThread = scenario.thread ?? thread
      currentAgent = scenario.agent ?? 'codex'
      const at = Date.now() - (scenario.expired ? 31 * 60 * 1000 : 0)
      await replacePending({ '%45': [{ ...queued, at, seen: scenario.seen ?? 0 }] })
      await open()
      if (scenario.clears) await page.waitForFunction(() => !JSON.parse(localStorage.getItem('tether.pending') || '{}')['%45'])
      else if (scenario.recovers) {
        await failure.waitFor({ timeout: 3000 })
        assert.equal((await readPending())['%45'][0].text, prompt, scenario.name)
      } else await page.getByRole('button', { name: '1 queued', exact: true }).waitFor({ timeout: 3000 })
      assert.equal(!!(await readPending())['%45'], !scenario.clears, scenario.name)
    }

    messages = []
    currentAgent = 'codex'
    await replacePending({ '%45': [{ ...queued, failed: 'Offline', offline: true }] })
    await open()
    await failure.click()
    retryMessages = [record('lost-ack', `${image}\n${prompt}`)]
    await card.getByRole('button', { name: 'Retry' }).click()
    await failure.waitFor({ state: 'detached' })
    assert.equal(sends.length, 1, 'When the first response was lost, retry must check for delivery before posting again')
    // Chat data is not trusted. A Codex image send must still go out when the transcript is not a list, or has bad entries.
    for (const [name, broken, clears] of [['an object', {}, false], ['bad entries', [null, { role: 'user', text: 5 }], true]]) {
      const before = sends.length
      messages = broken
      retryMessages = undefined
      await replacePending({})
      await open()
      await page.locator('.composer textarea').fill(draft)
      await page.getByRole('button', { name: 'Send message', exact: true }).click()
      await page.waitForFunction(() => document.querySelector('.composer textarea')?.value === '')
      assert.equal(sends.length, before + 1, `${name}: the send must POST`)
      assert.deepEqual(sends.at(-1), { session: '%45', thread, message: draft }, name)
      if (clears) {
        // The bad entries drop out; the new record lands in a valid list, so the send leaves the queue.
        await page.waitForFunction(() => !JSON.parse(localStorage.getItem('tether.pending') || '{}')['%45'])
      } else {
        // No list to check against: the send stays queued, not failed.
        await page.getByRole('button', { name: '1 queued', exact: true }).waitFor()
        const [item] = (await readPending())['%45']
        assert.equal(item.text, draft, name)
        assert.equal(item.failed, undefined, name)
      }
      assert.equal(await page.getByText('Tether stopped').count(), 0, name)
    }
    console.log('A Codex image send posts and keeps the right queue when the transcript is an object or has bad entries')
    assert.deepEqual(errors, [], 'No uncaught browser exceptions')
    console.log('Pending Codex sends preserve unsent text, retry only missing content, and reconcile within the original pane and thread')
  } finally {
    await context.close()
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
