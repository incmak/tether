// Run from ui/: DIST=<fresh build> node test_menu_verification_ui.cjs.
const { strict: assert } = require('node:assert')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const dist = path.resolve(process.env.DIST || 'dist')
const origin = 'http://127.0.0.1:9876'
const pane = { id: '%12', tmux_session: '$1', label: 'Menu test', dir: '/fixture', agent: 'claude',
  state: 'Input needed', writable: true, thread: 'menu-thread', approval: null }
const question = 'Which screenshot format should the report use?'
const revision = 'a'.repeat(64)
const listedRevision = 'b'.repeat(64)
const options = [{ id: 1, text: 'PNG', selected: true }, { id: 2, text: 'WebP' }]
const multiQuestion = 'Which features?'
const submitRefusal = 'The question changed or is no longer open. Open the terminal to review it.'
const multiOptions = [
  { id: 1, text: 'Search', detail: 'Find' },
  { id: 2, text: 'Export', detail: 'Save' },
  { id: 3, text: 'Sync', detail: 'Share' },
  { id: 4, text: 'Type something', free: true },
  { id: 5, text: 'Chat about this', selected: true },
]
const multiCurrent = { index: 1, total: 3, header: 'Features', multi: true, question: multiQuestion,
  options: multiOptions.map(option => ({ ...option, checked: option.id === 2 })), tabs: [], keys: [] }
const approvalPane = { ...pane, id: '%13', thread: 'approval-thread', state: 'Approval', attention: null,
  approval: { id: 'approval-1', actionable: true, text: 'Run the report?', options: [{ id: 1, text: 'Yes' }] } }
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2' }
const replies = {
  footer: { observed: true, model: 'Opus', mode: 'default', notices: [] },
  host: { name: 'Test server', office: false },
  update: { current: '0.0.0', latest: '0.0.0', available: false },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'side-shells': { shells: [] },
  'codex-runs': { pane: pane.id, running: 0, runs: [] },
  'projects/backlog': { text: '' },
  'work/board': { projects: [], items: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  usage: { claude: null, codex: null },
}

async function main() {
  const browser = await getBrowser({ local: true })
  try {
    const context = browser.contexts()[0]
    const page = await context.newPage()
    page.setDefaultTimeout(4000)
    const errors = [], writes = []
    let verified = false
    let multi = false
    let submitResponse
    page.on('pageerror', error => errors.push(error.message))
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== origin) return route.abort('blockedbyclient')
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (request.method() !== 'GET') {
          const body = request.postDataJSON()
          writes.push({ endpoint, body })
          if (endpoint === 'pick' && body.then === 'Enter') {
            await submitResponse
            return route.fulfill({ status: 409, json: { error: submitRefusal } })
          }
          return route.fulfill({ json: { accepted: true } })
        }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        if (endpoint === 'sessions') return route.fulfill({ json: { sessions: [{ ...pane, attention: {
          id: 'listed-menu', reason: 'menu', question, options, menu_verified: listedRevision,
        } }, approvalPane], default_thread: pane.thread } })
        if (endpoint === 'chat') return route.fulfill({ json: { ...pane, session: pane.id, messages: [], busy: false, earlier: 0,
          attention: multi
            ? { id: 'multi-1', reason: 'menu', text: multiQuestion, question: multiQuestion,
              menu_verified: verified ? revision : undefined, options: multiOptions, ask_current: multiCurrent }
            : { id: 'menu-1', reason: 'menu', text: question, question, menu_verified: verified ? revision : false,
              ...(verified ? { options } : {}) } } })
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: { uiUsage: false }, version: 1 } })
        return route.fulfill({ json: replies[endpoint] ?? {} })
      }
      if (url.pathname === '/terminal/live/') return route.fulfill({ contentType: 'text/html', body: '<p>Mocked terminal</p>' })
      const asset = url.pathname === '/chat/' ? 'index.html' : url.pathname.slice('/chat/'.length)
      if (!url.pathname.startsWith('/chat/') || asset === 'sw.js' || path.resolve(dist, asset) !== path.join(dist, asset)) return route.abort('blockedbyclient')
      try { return route.fulfill({ body: await fs.readFile(path.join(dist, asset)), contentType: mime[path.extname(asset)] }) }
      catch { return route.fulfill({ status: 404, body: '' }) }
    })
    await page.addInitScript(() => {
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%12', voiceOff: true, uiUsage: false }))
    })

    for (const width of [320, 1280]) {
      await page.setViewportSize({ width, height: 800 })
      const dock = page.locator('.status-content:not([hidden]) .choice-dock')
      for (verified of [false, true]) {
        writes.length = 0
        await page.goto(`${origin}/chat/?session=%2512`)
        await dock.getByText(question, { exact: true }).waitFor()
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width)
        if (verified) {
          assert.equal(await dock.getByText('Answer this in the terminal.', { exact: true }).count(), 0)
          const chose = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/mobile/api/menu')
          await dock.getByRole('button', { name: /PNG/ }).click()
          assert.deepEqual((await chose).postDataJSON(), { session: pane.id, thread: pane.thread, choice: 1, label: 'PNG', revision })
        } else {
          await dock.getByText('Answer this in the terminal.', { exact: true }).waitFor()
          const open = dock.getByRole('button', { name: 'Open terminal', exact: true })
          assert.equal(await dock.getByRole('button').count(), 1)
          assert(await open.evaluate(node => {
            const rect = node.getBoundingClientRect()
            return rect.left >= 0 && rect.right <= innerWidth && rect.height >= 24
          }))
          await page.locator('.status-grip').focus()
          for (let step = 0; step < 10 && !await open.evaluate(node => node === document.activeElement); step++) {
            await page.keyboard.press('Tab')
          }
          assert(await open.evaluate(node => node === document.activeElement))
          await page.keyboard.press('Enter')
          const frame = page.getByTitle('Live terminal')
          await frame.waitFor()
          assert.equal(new URL(await frame.getAttribute('src'), origin).searchParams.get('arg'), 'z%12')
          await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Back to chat')
          await page.getByRole('button', { name: 'Back to chat' }).click()
          await dock.getByText('Answer this in the terminal.', { exact: true }).waitFor()
          assert.deepEqual(writes.filter(({ endpoint }) => ['menu', 'key', 'pick', 'message'].includes(endpoint)), [])
        }
      }
      multi = true
      await page.goto(`${origin}/chat/?session=%2512`)
      await dock.getByText(multiQuestion, { exact: true }).waitFor()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width)
      assert(await page.locator('.composer textarea').isDisabled())
      assert.equal(await dock.getByRole('button', { name: /Type your own answer|Chat about this/ }).count(), 0)
      for (const [button, pick] of [
        [/Search/, { row: 0, labels: ['Search\nFind', 'Export\nSave', 'Sync\nShare', 'Type something', 'Chat about this'], then: 'Space' }],
        [/Done \(Enter\)/, { labels: ['Export\nSave'], then: 'Enter' }],
      ]) {
        const submitting = pick.then === 'Enter'
        const gate = submitting ? Promise.withResolvers() : null
        if (gate) submitResponse = gate.promise
        const picked = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/mobile/api/pick')
        const control = dock.getByRole('button', { name: button })
        try {
          if (submitting) await control.and(page.locator('button:enabled')).press('Enter')
          else await control.click()
          assert.deepEqual((await picked).postDataJSON(), {
            session: pane.id, thread: pane.thread, ...pick, heading: multiQuestion, revision,
          })
          if (submitting) await control.and(page.locator('button:disabled')).waitFor()
        } finally {
          gate?.resolve()
        }
      }
      await page.getByText(submitRefusal, { exact: true }).waitFor()
      verified = false
      await page.goto(`${origin}/chat/?session=%2512`)
      await dock.getByText('Answer this in the terminal.', { exact: true }).waitFor()
      assert.equal(await dock.getByRole('button').count(), 1)
      assert(await dock.getByRole('button', { name: 'Open terminal', exact: true }).isVisible())
      multi = false
    }
    const toggle = page.getByRole('button', { name: /^Needs you,/ })
    if (await toggle.getAttribute('aria-pressed') === 'false') await toggle.click()
    const inbox = page.getByRole('complementary', { name: 'Needs you' })
    const row = inbox.getByRole('button', { name: /^Question: / })
    for (const option of options) {
      const chose = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/mobile/api/menu')
      await row.press(String(option.id))
      assert.deepEqual((await chose).postDataJSON(), {
        session: pane.id, thread: pane.thread, choice: option.id, label: option.text, revision: listedRevision,
      })
      await inbox.getByRole('button', { name: new RegExp(option.text) }).and(inbox.locator('button:not([disabled])')).waitFor()
    }
    const approved = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/mobile/api/approval')
    await inbox.getByRole('button', { name: /^Needs permission: / }).press('y')
    assert.deepEqual((await approved).postDataJSON(), {
      session: approvalPane.id, thread: approvalPane.thread, approval: approvalPane.approval.id, choice: 1,
    })
    assert.deepEqual(errors, [])
    console.log('Dock menus, multi-select tick and Done, and the terminal fallback pass at 320 px and 1280 px. Inbox digits and Y send the listed identities.')
  } finally { await browser.close() }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
