// Run from ui/: node test_legacy_menu_card.cjs. The legacy chat (/mobile/chat/, server/index.html + server/app.js).
// A card answer names the question it answers: it posts /menu with the verified revision, never a bare /key digit.
// An unverified question, or a multi-select question, shows no card and no raw keys: only Open terminal.
const { strict: assert } = require('node:assert')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const server = path.resolve(__dirname, '../server')
const origin = 'http://127.0.0.1:9877'
const question = 'Which screenshot format should the report use?'
const revision = 'c'.repeat(64)
const options = [{ id: 1, text: 'PNG', selected: true }, { id: 2, text: 'WebP' }, { id: 3, text: 'Type something.', free: true }]
const pane = { id: '%21', tmux_session: '$1', label: 'Legacy', dir: '/fixture', agent: 'claude', state: 'Input needed',
  writable: true, thread: 'legacy-thread', approval: null }
const multi = { index: 0, total: 1, header: 'Features', multi: true, question, options, tabs: [], keys: [] }
const cases = [
  { name: 'verified', attention: { menu_verified: revision }, card: true },
  { name: 'unverified', attention: { menu_verified: false }, card: false },
  { name: 'multi-select', attention: { menu_verified: revision, ask_current: multi }, card: false },
]

const attentionFor = current => ({ id: 'legacy-menu', reason: 'menu', text: `${question}\n❯ 1. PNG\n  2. WebP\n  3. Type something.`,
  question, options, ...current.attention })

async function main() {
  const browser = await getBrowser({ local: true })
  try {
    const page = await browser.contexts()[0].newPage()
    page.setDefaultTimeout(4000)
    const errors = [], writes = []
    let current
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== origin) return route.abort('blockedbyclient')
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (request.method() !== 'GET') {
          writes.push({ endpoint, body: request.postDataJSON() })
          return route.fulfill({ json: { accepted: true } })
        }
        const attention = attentionFor(current)
        if (endpoint === 'sessions') return route.fulfill({ json: { sessions: [{ ...pane, attention }], default_thread: pane.thread } })
        if (endpoint === 'chat') return route.fulfill({ json: { ...pane, session: pane.id, messages: [], busy: false, earlier: 0, attention } })
        return route.fulfill({ json: {} })
      }
      if (url.pathname === '/mobile/chat/') return route.fulfill({ contentType: 'text/html', body: await fs.readFile(path.join(server, 'index.html')) })
      if (url.pathname === '/mobile/app.js') return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(server, 'app.js')) })
      if (url.pathname.startsWith('/mobile/vendor/')) {
        return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(server, 'vendor', path.basename(url.pathname))) })
      }
      return route.fulfill({ status: 204, body: '' })
    })

    for (const width of [320, 1280]) {
      await page.setViewportSize({ width, height: 800 })
      for (current of cases) {
        writes.length = 0
        await page.goto(`${origin}/mobile/chat/?session=%2521`)
        const panel = page.locator('#attention')
        await panel.waitFor()
        const card = page.locator('#attention-card')
        await page.waitForFunction(() => document.querySelector('#attention-text')?.textContent.includes('PNG'))
        assert.equal(await card.isVisible(), current.card, `${current.name} card at ${width} px`)
        if (current.card) {
          const buttons = card.locator('.opts button')
          assert.equal(await buttons.count(), 2, 'the free row is not a card answer')
          const posted = page.waitForRequest(request => request.method() === 'POST')
          await buttons.filter({ hasText: 'WebP' }).click()
          const request = await posted
          assert.equal(new URL(request.url()).pathname, '/mobile/api/menu')
          assert.deepEqual(request.postDataJSON(), { session: pane.id, thread: pane.thread, choice: 2, label: 'WebP', revision })
        } else {
          // No raw key on a question nobody verified: the interactive terminal instead.
          assert.equal(await page.locator('#attention-keys button:visible').count(), 0, `${current.name}: no raw keys`)
          assert.equal(await page.locator('#attention-type').isVisible(), false, `${current.name}: no typing`)
          const open = page.getByRole('button', { name: 'Open terminal', exact: true })
          await open.click()
          const frame = page.locator('#model-frame')
          await page.waitForFunction(() => document.querySelector('#model-frame')?.getAttribute('src')?.includes('/terminal/live/'))
          assert.equal(new URL(await frame.getAttribute('src'), origin).searchParams.get('arg'), pane.tmux_session)
          await page.locator('#close-model').click()
        }
        assert.equal(writes.filter(({ endpoint }) => ['key', 'type'].includes(endpoint)).length, 0)
      }
    }
    // One page, no reload: the controls follow each new attention value and do not keep the last state.
    const yesNo = { name: 'yes-no', attention: { id: 'legacy-yes-no', reason: 'permission', text: 'Allow the edit? (y/n)',
      question: undefined, options: [], menu_verified: false } }
    await page.goto(`${origin}/mobile/chat/?session=%2521`)
    await page.locator('#attention').waitFor()
    const visible = selector => page.locator(selector).isVisible()
    for (const [step, terminal, card, typing, keys] of [
      [cases[1], true, false, false, []],
      [cases[0], false, true, true, ['Escape']],
      [cases[2], true, false, false, []],
      [yesNo, false, false, true, ['Up', 'Down', 'Enter', 'Escape', 'Tab', 'y', 'n']],
    ]) {
      current = step
      await page.evaluate(value => updateAttention(value), attentionFor(step))
      assert.equal(await visible('#attention-terminal'), terminal, `${step.name}: Open terminal`)
      assert.equal(await visible('#attention-card'), card, `${step.name}: card`)
      assert.equal(await visible('#attention-type'), typing, `${step.name}: typing`)
      assert.deepEqual(await page.locator('#attention-keys button:visible').evaluateAll(nodes => nodes.map(node => node.dataset.key)),
        keys, `${step.name}: keys`)
    }
    assert.deepEqual(errors, [])
    console.log('Legacy card: verified one-choice answers post /menu with the revision; unverified and multi-select offer only the terminal, at 320 px and 1280 px.')
  } finally { await browser.close() }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
