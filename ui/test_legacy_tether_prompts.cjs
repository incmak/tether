// Run from ui/: node test_legacy_tether_prompts.cjs [SHOTS_DIR]. The legacy chat (/mobile/chat/, server/index.html + server/app.js).
// A prompt Tether sent (the server marks it source 'tether' from a send receipt) is never shown as your words:
// it folds to one labeled row. A plain prompt, and a review-like text without a receipt, stay your bubbles.
const { strict: assert } = require('node:assert')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const server = path.resolve(__dirname, '../server')
const shots = process.argv[2]
const origin = 'http://127.0.0.1:9878'
const pane = { id: '%21', tmux_session: '$1', label: 'Legacy', dir: '/fixture', agent: 'claude', state: 'Idle',
  writable: true, thread: 'legacy-thread', approval: null }
const user = (id, text, extra = {}) => ({ id: String(id), role: 'user', text, ...extra })
const messages = [
  user(1, 'Please fix the login page.'),
  user(2, '[Tether status] 2 tab updates\n- `B12 fix` finished: Done.\n- `B13 ui` waits on the user: Pick one.',
    { source: 'tether', prompt: 'status', tabs: [] }),
  user(3, '[Tether review] Feedback from the reviewer tab:\n\nReview blocked.', { source: 'tether', prompt: 'review' }),
  user(4, 'You coordinate this session. Rules follow.', { source: 'tether', prompt: 'rules' }),
  user(5, 'A Tether prompt of a kind this page does not know.', { source: 'tether', prompt: 'future-kind' }),
  user(8, 'A Tether prompt whose kind names an inherited property.', { source: 'tether', prompt: 'constructor' }),
  user(9, 'Another kind that names an inherited property.', { source: 'tether', prompt: 'toString' }),
  // Pasted by the person: same words as a relay, but no receipt, so it stays theirs.
  user(6, '[Tether review] Feedback from the reviewer tab:\n\nPasted by hand.'),
  { id: '7', role: 'assistant', text: 'On it.' },
]
const folded = [
  ['2', 'Tether status update', 'B12 fix'],
  ['3', 'Review message from Tether', 'Review blocked.'],
  ['4', 'Coordinator rules from Tether', 'Rules follow.'],
  ['5', 'Sent by Tether', 'does not know'],
  ['8', 'Sent by Tether', 'whose kind names'],
  ['9', 'Sent by Tether', 'Another kind'],
]

const state = { messages, busy: false }
// The page polls the chat every 2 s while its stream is down (the mock has no stream), so a state change shows within one poll.
const settle = (page, check, arg) => page.waitForFunction(check, arg, { timeout: 8000 })

async function main() {
  const browser = await getBrowser({ local: true })
  try {
    const page = await browser.contexts()[0].newPage()
    page.setDefaultTimeout(4000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== origin) return route.abort('blockedbyclient')
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        if (request.method() !== 'GET') return route.fulfill({ json: { accepted: true, delivery: 'queued' } })  // never a real write
        if (endpoint === 'sessions') return route.fulfill({ json: { sessions: [pane], default_thread: pane.thread } })
        if (endpoint === 'chat') return route.fulfill({ json: { ...pane, session: pane.id, messages: state.messages, busy: state.busy, earlier: 0 } })
        return route.fulfill({ json: {} })
      }
      if (url.pathname === '/mobile/chat/') return route.fulfill({ contentType: 'text/html', body: await fs.readFile(path.join(server, 'index.html')) })
      if (url.pathname === '/mobile/app.js') return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(server, 'app.js')) })
      if (url.pathname.startsWith('/mobile/vendor/')) {
        return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(server, 'vendor', path.basename(url.pathname))) })
      }
      return route.fulfill({ status: 204, body: '' })
    })

    for (const width of [320, 360, 1280]) {
      await page.setViewportSize({ width, height: 900 })
      await page.goto(`${origin}/mobile/chat/?session=%2521`)
      await page.waitForFunction(() => document.querySelectorAll('#chat article').length >= 9)
      // Your own words: user bubbles with the "> " label, in order.
      const bubbles = await page.locator('#chat article.message.user').evaluateAll(nodes => nodes.map(node => node.textContent))
      assert.equal(bubbles.length, 2, `${width} px: only the two prompts the person wrote are user bubbles`)
      assert.match(bubbles[0], /fix the login page/)
      assert.match(bubbles[1], /Pasted by hand/)
      for (const [id, label, body] of folded) {
        const row = page.locator('#chat article.tether-sent').filter({ hasText: label }).filter({ has: page.locator('summary', { hasText: new RegExp(`^${label}$`) }) }).filter({ hasText: body })
        assert.equal(await row.filter({ hasText: body }).count(), 1, `${width} px: one "${label}" row for id ${id}`)
        const details = row.locator('details')
        assert.equal(await details.evaluate(node => node.open), false, `${width} px: "${label}" starts folded`)
        assert.equal(await row.locator('.label').count(), 0, `${width} px: "${label}" has no "> " user label`)
        await row.locator('summary').click()
        assert.match(await row.locator('.carried-summary').innerText(), new RegExp(body), `${width} px: "${label}" opens to the full text`)
        const box = await row.boundingBox()
        assert.ok(box.x >= 0 && box.x + box.width <= width, `${width} px: "${label}" fits (id ${id})`)
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${width} px: no side scroll`)
      if (shots) {
        await fs.mkdir(shots, { recursive: true })
        await page.screenshot({ path: path.join(shots, `legacy-tether-${width}.png`), fullPage: true })
      }
    }
    // Behavior after the first render, at phone width.
    await page.setViewportSize({ width: 320, height: 900 })
    const userText = id => page.locator('#chat article.message.user:not(.pending-message)').filter({ hasText: id })

    // 1. A receipt that binds after the first render: same id and text, now marked as Tether's. The bubble becomes a row.
    state.messages = [user(1, 'Please fix the login page.'), user(2, 'Late-bound review relay.')]
    await page.goto(`${origin}/mobile/chat/?session=%2521`)
    await settle(page, () => [...document.querySelectorAll('#chat article.message.user')].some(node => node.textContent.includes('Late-bound')))
    state.messages = [state.messages[0], user(2, 'Late-bound review relay.', { source: 'tether', prompt: 'review' })]
    await settle(page, () => [...document.querySelectorAll('#chat article.tether-sent')].some(node => node.textContent.includes('Review message from Tether')))
    assert.equal(await userText('Late-bound').count(), 0, 'a receipt bound after the first render turns the bubble into a Tether row')

    // 2. A Tether prompt with the same words never confirms your pending send. Your own echo does.
    const words = '[Tether review] Feedback from the reviewer tab:\n\nPasted by hand.'
    await page.locator('#message').fill(words)
    await page.locator('#send').click()
    await settle(page, () => document.querySelector('#chat .pending-message') !== null)
    state.messages = [...state.messages, user(10, words, { source: 'tether', prompt: 'review' })]
    await settle(page, () => document.querySelectorAll('#chat article.tether-sent').length === 2)
    assert.equal(await page.locator('#chat .pending-message').count(), 1, 'a Tether prompt with your words keeps your send pending')
    state.messages = [...state.messages, user(11, words)]
    await settle(page, () => document.querySelector('#chat .pending-message') === null)
    assert.equal(await userText('Pasted by hand').count(), 1, 'your own echo confirms the send')

    // 3. The working indicator and Stop follow the latest prompt, also when Tether sent it.
    state.messages = [user(1, 'Please fix the login page.'), user(12, 'A status update.', { source: 'tether', prompt: 'status' })]
    state.busy = true
    await page.goto(`${origin}/mobile/chat/?session=%2521`)
    await settle(page, () => document.getElementById('working')?.hidden === false)   // #working attaches on the first chat render
    assert.match(await page.evaluate(() => document.getElementById('working').previousElementSibling.textContent), /Tether status update/,
      'the working row sits under the newest prompt, a Tether row')
    // A folded Tether prompt whose text renders HTML with row classes never pulls Stop inside its closed disclosure.
    for (const example of ['message user', 'message tether-sent']) {
      state.messages = [user(1, 'Please fix the login page.'),
        user(13, `Review: an example.\n\n<div class="${example}">example row</div>`, { source: 'tether', prompt: 'review' })]
      await page.goto(`${origin}/mobile/chat/?session=%2521`)
      await settle(page, () => document.getElementById('working')?.hidden === false)   // #working attaches on the first chat render
      const place = await page.evaluate(() => { const node = document.getElementById('working'); return { row: node.parentElement.id, after: node.previousElementSibling?.textContent } })
      assert.equal(place.row, 'chat', `"${example}" in a message: the working row stays a chat row`)
      assert.match(place.after, /Review message from Tether/, `"${example}" in a message: the working row sits under that prompt`)
      assert.ok(await page.locator('#working button').isVisible(), `"${example}" in a message: Stop stays visible`)
    }
    assert.deepEqual(errors, [])
    console.log('Legacy chat: Tether prompts fold to labeled rows (also when the receipt binds late, and for inherited-name kinds); your own and pasted prompts stay user bubbles at 320, 360 and 1280 px; a Tether echo never confirms your send; the working row follows the newest prompt.')
  } finally { await browser.close() }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
