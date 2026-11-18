// Render the built chat with mock responses and exercise the native advisor disclosure in Chromium.
const { strict: assert } = require('node:assert')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const ORIGIN = 'http://advisor.test'
const START = '2026-11-18T03:17:30.715Z'
const END = '2026-11-18T03:18:46.091Z'
const CIPHERTEXT = 'REDACTED_TEST_CIPHERTEXT'
const row = (id, status, data = {}) => ({
  id, role: 'assistant', kind: 'advisor', text: 'Asked the advisor', time: START,
  advisor: { id: 'call-' + id, status, started: START, ...data },
})
const scheduled = { id: 'schedule', role: 'assistant', kind: 'notice', title: 'Scheduled run', quiet: 'schedule', watch: 'Fixture job', text: 'Fixture job', time: START }
const answered = row('answered', 'answered', { model: 'claude-fable-5-2', ended: END, encrypted_content: CIPHERTEXT })
const failed = row('failed', 'error', { model: 'claude-opus-5-5-20261118', error: 'too_many_requests' })
const unknown = row('unknown', 'unknown', { model: 'custom-advisor', result_type: 'new_reply' })
const timeless = row('timeless', 'answered')
const fallback = { ...row('fallback', 'pending'), advisor: undefined }
const pending = row('pending', 'pending', { model: 'claude-fable-5-2' })
const pane = { id: '%1', tmux_session: '$1', label: 'fixture:0.1', pane_name: 'Advisor calls', agent: 'claude', dir: '/fixture', writable: true, thread: 't1' }
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json' }

async function main() {
  const browser = await getBrowser({ local: true })
  try {
    const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 800 } })
    const page = await context.newPage()
    page.setDefaultTimeout(7000)
    const errors = []
    page.on('pageerror', error => errors.push(error.stack))
    let state = 'Working'
    let messages = [scheduled, answered, { id: 'reply', role: 'assistant', text: 'Fixture reply.' }, failed, unknown, timeless,
      fallback, pending, { id: 'notice', role: 'assistant', kind: 'notice', title: 'Fixture notice', text: 'Working' }]
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== ORIGIN) return route.abort()
      if (url.pathname.startsWith('/mobile/api/')) {
        const endpoint = url.pathname.slice('/mobile/api/'.length)
        const fixture = {
          sessions: { sessions: [{ ...pane, state }], default_thread: pane.thread },
          chat: { ...pane, session: pane.id, state, busy: state === 'Working', messages, earlier: 0, limit: 100 },
          footer: { observed: true, notices: [] },
          search: { total: 1, max_limit: 100, matches: [{ id: answered.id, role: answered.role, kind: answered.kind, from_end: 8, snippet: answered.text }] },
          catalog: { commands: [], plugins: [], mcp: [] },
          subagents: { agents: [], tasks: [] }, 'side-shells': { shells: [] },
          'browser/agents': { enabled: false, available: false, agents: [], asks: [] },
          'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
          'codex-runs': { pane: pane.id, running: 0, runs: [] },
          prefs: { prefs: { theme: 'system', voiceOff: true }, version: 1 }, 'projects/backlog': { text: '' },
          'work/board': { revision: 1, items: [], stages: [], projects: [] },
        }
        if (endpoint === 'stream') return route.fulfill({ status: 204 })
        return route.fulfill({ json: fixture[endpoint] ?? {} })
      }
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404 })
      const file = path.join(DIST, url.pathname.slice('/chat/'.length) || 'index.html')
      const body = await fs.readFile(file).catch(() => null)
      return body ? route.fulfill({ body, contentType: mime[path.extname(file)] ?? 'application/octet-stream' }) : route.fulfill({ status: 404 })
    })
    const load = () => page.goto(ORIGIN + '/chat/?session=%251')
    const item = id => page.locator(`.chat-page [data-msg-id="${id}"]`)
    const summary = id => item(id).locator('summary')
    const meta = id => item(id).locator('.advisor-call-meta').textContent()
    await load()

    await page.locator('.chat-page .watch-row > summary').click()
    assert.equal(await summary('answered').locator('.advisor-call-what').textContent(), 'Asked the advisor')
    assert.equal(await meta('answered'), '1m 15s · Fable 5.2')
    assert.equal(await summary('answered').locator('time').getAttribute('datetime'), START)
    await summary('answered').press('Enter')
    assert.equal(await item('answered').evaluate(node => node.open), true)
    assert.equal(await summary('answered').evaluate(node => getComputedStyle(node).outlineStyle), 'solid')
    assert.equal(await item('answered').locator('.advisor-call-body p').first().textContent(),
      'Claude sent its conversation to the advisor (Fable 5.2). The advice came back after 1m 15s.')
    assert.match(await item('answered').locator('.advisor-call-body').textContent(), /keeps the advice text encrypted/)
    assert.equal(await page.getByText(CIPHERTEXT, { exact: false }).count(), 0)
    assert.doesNotMatch(await page.locator('.chat-page').innerHTML(), /REDACTED_TEST_CIPHERTEXT|encrypted_content/)

    assert.equal(await meta('failed'), 'No answer · Opus 5.5')
    assert.equal(await item('failed').locator('.tool-error').textContent(), 'No answer')
    await summary('failed').press('Space')
    assert.equal(await item('failed').locator('.advisor-call-body p').textContent(),
      'The advisor (Opus 5.5) did not answer: too many requests. Claude went on without advice.')
    assert.equal(await meta('unknown'), 'Reply received · custom-advisor')
    assert.match(await item('unknown').locator('.advisor-call-body p').textContent(), /\(new_reply\)\.$/)
    assert.equal(await meta('timeless'), '')
    assert.equal(await item('timeless').locator('.advisor-call-body p').first().textContent(), 'Claude sent its conversation to the advisor.')
    assert.equal(await item('fallback').getAttribute('class'), 'notice-line')
    assert.equal(await item('fallback').textContent(), 'Asked the advisor')
    assert.equal(await meta('pending'), 'Waiting for advice… · Fable 5.2')
    assert.equal(await item('pending').locator('.advisor-call-body p').textContent(), 'Claude is waiting for the advisor to answer.')

    await page.setViewportSize({ width: 320, height: 640 })
    await page.waitForFunction(() => innerWidth === 320)
    for (const id of ['pending', 'failed']) {
      const layout = await summary(id).evaluate(node => {
        const meta = node.querySelector('.advisor-call-meta')
        const bounds = node.getBoundingClientRect()
        const trailing = [...node.querySelectorAll('time, .chevron')].map(child => child.getBoundingClientRect().toJSON())
        const what = node.querySelector('.advisor-call-what').getBoundingClientRect()
        return { textWidth: meta.scrollWidth, metaWidth: meta.clientWidth, below: meta.getBoundingClientRect().top >= what.bottom - 1, bounds: bounds.toJSON(), trailing }
      })
      // At 320 px the state and model sit on their own line and show in full.
      assert.ok(layout.below && layout.textWidth <= layout.metaWidth && layout.metaWidth > 13, JSON.stringify(layout))
      assert.ok(layout.trailing.every(rect => rect.width > 0 && rect.left >= layout.bounds.left && rect.right <= layout.bounds.right), JSON.stringify(layout))
    }
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme })
      const contrast = await summary('failed').evaluate(node => {
        const luminance = color => color.match(/\d+(?:\.\d+)?/g).slice(0, 3).map(value => {
          const channel = Number(value) / 255
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
        }).reduce((sum, value, at) => sum + value * [0.2126, 0.7152, 0.0722][at], 0)
        let backdrop = node
        while (getComputedStyle(backdrop).backgroundColor === 'rgba(0, 0, 0, 0)') backdrop = backdrop.parentElement
        const background = luminance(getComputedStyle(backdrop).backgroundColor)
        return ['.advisor-call-meta', '.tool-error', 'time'].map(selector => {
          const foreground = luminance(getComputedStyle(node.querySelector(selector)).color)
          return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
        })
      })
      assert.ok(contrast.every(ratio => ratio >= 4.5), `${colorScheme} text contrast: ${contrast}`)
    }
    await page.setViewportSize({ width: 1280, height: 800 })

    messages = [...messages, { id: 'later', role: 'assistant', text: 'More work.' }]
    await load()
    assert.equal(await meta('pending'), 'No reply recorded · Fable 5.2')
    messages = messages.slice(0, -1)
    state = 'Idle'
    await load()
    assert.equal(await meta('pending'), 'No reply recorded · Fable 5.2')
    assert.match(await item('pending').locator('.advisor-call-body p').textContent(), /The transcript has no answer for this call\./)

    await page.getByRole('button', { name: 'Search this chat' }).click()
    await page.getByRole('searchbox', { name: 'Words to find' }).fill('advisor')
    assert.match(await page.locator('.search-results small').textContent(), /^Advisor/)
    // The answered call sits inside a closed scheduled-run fold. Picking it opens the fold and shows the row.
    assert.equal(await page.locator('.chat-page .watch-row').evaluate(node => node.open), false)
    await page.locator('.search-results button').first().click()
    await item('answered').waitFor({ state: 'visible' })
    assert.equal(await page.locator('.chat-page .watch-row').evaluate(node => node.open), true)

    // An advisor call last in an idle turn does not take the reply's actions away.
    await page.keyboard.press('Escape')
    messages = [{ id: 'ask', role: 'user', text: 'Fixture question.' }, { id: 'done', role: 'assistant', text: 'Fixture answer.' }, row('last', 'answered', { ended: END })]
    await load()
    await item('done').locator('.message-actions').waitFor()
    assert.deepEqual(errors, [])
    console.log('Advisor chat renders all states, opens by keyboard, keeps ciphertext out, and labels search results')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
