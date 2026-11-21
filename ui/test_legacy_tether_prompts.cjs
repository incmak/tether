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
  // Pasted by the person: same words as a relay, but no receipt, so it stays theirs.
  user(6, '[Tether review] Feedback from the reviewer tab:\n\nPasted by hand.'),
  { id: '7', role: 'assistant', text: 'On it.' },
]
const folded = [
  ['2', 'Tether status update', 'B12 fix'],
  ['3', 'Review message from Tether', 'Review blocked.'],
  ['4', 'Coordinator rules from Tether', 'Rules follow.'],
  ['5', 'Sent by Tether', 'does not know'],
]

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
        if (request.method() !== 'GET') return route.fulfill({ json: { accepted: true } })  // never a real write
        if (endpoint === 'sessions') return route.fulfill({ json: { sessions: [pane], default_thread: pane.thread } })
        if (endpoint === 'chat') return route.fulfill({ json: { ...pane, session: pane.id, messages, busy: false, earlier: 0 } })
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
      await page.waitForFunction(() => document.querySelectorAll('#chat article').length >= 7)
      // Your own words: user bubbles with the "> " label, in order.
      const bubbles = await page.locator('#chat article.message.user').evaluateAll(nodes => nodes.map(node => node.textContent))
      assert.equal(bubbles.length, 2, `${width} px: only the two prompts the person wrote are user bubbles`)
      assert.match(bubbles[0], /fix the login page/)
      assert.match(bubbles[1], /Pasted by hand/)
      for (const [id, label, body] of folded) {
        const row = page.locator('#chat article.tether-sent').filter({ hasText: label })
        assert.equal(await row.count(), 1, `${width} px: one "${label}" row`)
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
    assert.deepEqual(errors, [])
    console.log('Legacy chat: Tether status, review, rules and unknown-kind prompts fold to labeled rows; your own and pasted prompts stay user bubbles, at 320, 360 and 1280 px.')
  } finally { await browser.close() }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
