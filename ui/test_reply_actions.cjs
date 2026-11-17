// Reply actions and the phone composer (ReplyFeedback.tsx, Messages.tsx, Composer.tsx): Copy, Share, Read aloud,
// thumbs up and thumbs down under the last reply; the one-row phone composer; the "Add to message" sheet.
// Every API request stays in this page's fixtures; no terminal is touched.
const http = require('node:http'), fs = require('node:fs/promises'), path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')
const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
const pane = (id, name) => ({ id, tmux_session: '$1', label: `project:0.${id.slice(1)}`, pane_name: name, agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: `t${id.slice(1)}`, approval: null, ask: null })
const reply = 'The numbers add up across all the statements: the balance sheet balances, totals foot, and figures that appear in more than one place match.\n\nTwo things to know when you open it:\n\n- **Table of contents:** it is a Word field, so it starts empty.\n- **Page breaks:** a few tables run onto the next page.'
const fixture = {
  sessions: { sessions: [pane('%1', 'Deploy fix'), pane('%2', 'Docs pass')], default_thread: 't1' },
  chat: { messages: [{ id: 'u1', role: 'user', text: 'Make the SEC-style doc', time: new Date().toISOString() }, { id: 'a1', role: 'assistant', text: reply, time: new Date().toISOString() }], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: true, available: true, reason: '', agents: [], asks: [] }, 'browser/tabs': { tabs: [] },
  'work/board': { revision: 1, projects: [], items: [], stages: [] },
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}
let failed = 0
const check = (name, ok, detail = '') => { console.log(`${ok ? 'ok' : 'FAIL'} ${name}${ok ? '' : ' ' + detail}`); if (!ok) failed++ }
;(async () => {
  const server = http.createServer((q, r) => { r.writeHead(404); r.end() }); await new Promise(r => server.listen(0, '127.0.0.1', r))
  const origin = `http://localhost:${server.address().port}`
  const browser = await getBrowser(); const context = await browser.newContext({ viewport: { width: 390, height: 844 } }); const page = await context.newPage()
  const posts = []
  let ratings = { a1: { rating: 'up', note: '' } }
  const ratingsT2 = { a1: { rating: 'up', note: '' } }
  // A held save: the test releases it with a status after it changes the conversation.
  let gate = null, failing = false
  const gets = []
  const hold = () => { let release; const promise = new Promise(resolve => { release = resolve }); gate = { promise, release }; return gate }
  try {
    await page.routeWebSocket('**/*', s => s.close())
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.startsWith('/mobile/api/')) {
        const ep = url.pathname.slice(12)
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON(); posts.push({ ep, body })
          if (ep === 'feedback' && gate) { const held = gate; gate = null; const status = await held.promise; if (status !== 200) return route.fulfill({ status, json: { error: 'Disk full.' } }) }
          if (ep === 'feedback' && failing) return route.fulfill({ status: 500, json: { error: 'Disk full.' } })
          if (ep === 'feedback' && body.message === 'fail') return route.fulfill({ status: 500, json: { error: 'Disk full.' } })
          return route.fulfill({ json: ep === 'feedback' ? { saved: body.rating ? { rating: body.rating, note: body.note } : null } : {} })
        }
        if (ep === 'stream') return route.fulfill({ status: 204, body: '' })
        if (ep.startsWith('chat')) return route.fulfill({ json: url.searchParams.get('session') === '%2' ? { ...fixture.chat, thread: 't2', session: '%2' } : fixture.chat })
        if (ep === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (ep === 'feedback') gets.push(url.searchParams.get('thread'))
        if (ep === 'feedback') return route.fulfill({ json: { ratings: url.searchParams.get('thread') === 't2' ? ratingsT2 : ratings } })
        return route.fulfill({ json: fixture[ep] || {} })
      }
      if (url.pathname.startsWith('/chat/')) {
        const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.join(DIST, decodeURIComponent(url.pathname.slice(5)))
        try { return await route.fulfill({ body: await fs.readFile(file), contentType: MIME[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
      }
      return route.abort()
    })
    await page.addInitScript(() => {
      localStorage.clear(); localStorage.setItem('tmux-chat.prefs.synced', '1'); localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'system', voiceOff: true, voiceSound: false }))
      // A desktop browser with no share sheet: Share copies the text. Record what was copied.
      delete Navigator.prototype.share
      window.__copied = []
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__copied.push(text) } } })
    })
    await page.goto(`${origin}/chat/?session=%251`); await page.locator('.phone-tabbar').waitFor()
    const actions = page.locator('.chat-page .assistant-message .message-actions').last()
    await actions.waitFor()

    // Order and names, as in the Claude app.
    const names = await actions.locator('button').evaluateAll(list => list.map(item => item.getAttribute('aria-label')))
    check('the reply actions are Copy, Share, Read aloud, Good, Bad', JSON.stringify(names.filter(name => name !== 'Read aloud')) === JSON.stringify(['Copy message', 'Share message (copies the text)', 'Good response', 'Bad response']), JSON.stringify(names))
    const sizes = await actions.locator('button').evaluateAll(list => list.map(item => { const box = item.getBoundingClientRect(); return [box.width, box.height] }))
    check('each reply action is at least 24 by 24 (WCAG 2.5.8)', sizes.every(([w, h]) => w >= 24 && h >= 24), JSON.stringify(sizes))

    // Saved ratings load: a1 was rated up.
    const up = actions.getByRole('button', { name: 'Good response' }), down = actions.getByRole('button', { name: 'Bad response' })
    await page.waitForFunction(() => document.querySelector('.assistant-message:last-of-type [aria-label="Good response"]')?.getAttribute('aria-pressed') === 'true' || [...document.querySelectorAll('[aria-label="Good response"]')].some(item => item.getAttribute('aria-pressed') === 'true'), null, { timeout: 5000 }).catch(() => {})
    check('a saved thumbs up shows as pressed', await up.getAttribute('aria-pressed') === 'true')

    // Thumbs up again removes it.
    posts.length = 0
    await up.click(); await page.waitForTimeout(200)
    check('a second tap removes the thumbs up', posts.some(p => p.ep === 'feedback' && p.body.message === 'a1' && p.body.rating === '' && p.body.thread === 't1'), JSON.stringify(posts))
    check('the thumbs up is no longer pressed', await up.getAttribute('aria-pressed') === 'false')

    // Share without a share sheet copies the text.
    await actions.getByRole('button', { name: /^Share message/ }).click(); await page.waitForTimeout(200)
    check('Share copies the reply when there is no share sheet', (await page.evaluate(() => window.__copied)).includes(fixture.chat.messages[1].text))
    check('Share confirms the copy', await actions.getByRole('button', { name: 'Copied to share' }).count() === 1)
    await actions.getByRole('button', { name: 'Copy message' }).click(); await page.waitForTimeout(200)
    check('Copy copies the reply', (await page.evaluate(() => window.__copied)).length === 2)

    // Thumbs down: the sheet, then Save only.
    posts.length = 0
    await down.click()
    const sheet = page.getByRole('dialog', { name: 'What went wrong?' })
    await sheet.waitFor()
    check('thumbs down opens "What went wrong?"', await sheet.isVisible())
    await sheet.getByRole('button', { name: 'Too long' }).click()
    check('a reason chip toggles on', await sheet.getByRole('button', { name: 'Too long' }).getAttribute('aria-pressed') === 'true')
    await sheet.getByLabel('Details (optional)').fill('Cut it to three lines.')
    await sheet.getByRole('button', { name: 'Save only' }).click()
    await sheet.waitFor({ state: 'detached' })
    const saved = posts.find(p => p.ep === 'feedback')
    check('Save only saves the rating with the reason and note', saved && saved.body.rating === 'down' && saved.body.note === 'Too long. Cut it to three lines.' && saved.body.excerpt.startsWith('The numbers add up'), JSON.stringify(posts))
    check('Save only sends no message to the agent', !posts.some(p => p.ep === 'message'))
    check('the thumbs down shows as pressed', await down.getAttribute('aria-pressed') === 'true')

    // Thumbs down again removes it; then "Tell the agent" sends the note as a message.
    await down.click(); await page.waitForTimeout(200)
    check('a second tap removes the thumbs down', await down.getAttribute('aria-pressed') === 'false')
    posts.length = 0
    await down.click(); await sheet.waitFor()
    await sheet.getByRole('button', { name: 'Not correct' }).click()
    await sheet.getByRole('button', { name: 'Tell the agent' }).click()
    await sheet.waitFor({ state: 'detached' })
    const sent = posts.find(p => p.ep === 'message')
    check('Tell the agent saves the rating', posts.some(p => p.ep === 'feedback' && p.body.rating === 'down' && p.body.note === 'Not correct'), JSON.stringify(posts))
    check('Tell the agent sends the feedback to this tab and thread', sent && sent.body.session === '%1' && sent.body.thread === 't1' && /^Feedback on your reply that starts "The numbers add up/.test(sent.body.message) && sent.body.message.includes('Not correct'), JSON.stringify(sent))

    // Compact composer: one row while idle and empty; focus opens the full box with the pills.
    await page.locator('.chat-scroll').first().click({ position: { x: 20, y: 150 } }); await page.waitForTimeout(200)
    const form = page.locator('form.composer')
    check('the idle phone composer is one pill row', await form.evaluate(el => el.classList.contains('compact') && el.getBoundingClientRect().height <= 60), String(await form.evaluate(el => el.getBoundingClientRect().height)))
    check('the idle row hides the model pill', await page.locator('.composer-pills').isHidden())
    await page.locator('.composer textarea').focus(); await page.waitForTimeout(150)
    check('focus opens the full composer with the model pill', !(await form.evaluate(el => el.classList.contains('compact'))) && await page.locator('.composer-pills').isVisible())
    await page.locator('.composer textarea').fill('draft text')
    await page.locator('.chat-scroll').first().click({ position: { x: 20, y: 150 } }); await page.waitForTimeout(150)
    check('a draft keeps the full composer after blur', !(await form.evaluate(el => el.classList.contains('compact'))))
    await page.locator('.composer textarea').fill('')
    await page.locator('.chat-scroll').first().click({ position: { x: 20, y: 150 } }); await page.waitForTimeout(150)

    // The + sheet: header, three cards, grouped rows; Close closes it.
    await page.locator('button.attach').click()
    const menu = page.locator('.attach-menu'); await menu.waitFor()
    check('the sheet has the "Add to message" title', (await menu.locator('.attach-head h2').textContent()) === 'Add to message')
    check('the sheet cards are Camera, Photos, Files', JSON.stringify(await menu.locator('.attach-tiles button').allTextContents()) === JSON.stringify(['Camera', 'Photos', 'Files']))
    const rows = await menu.locator('.attach-group > button').evaluateAll(list => list.map(item => item.getBoundingClientRect().height))
    check('the sheet rows are in groups and at least 44 high', rows.length >= 4 && rows.every(h => h >= 44), JSON.stringify(rows))
    await menu.locator('.attach-head button').click()
    check('Close closes the sheet', await page.locator('.attach-menu').count() === 0)

    // Closing the sheet with Escape or Close puts focus back on +; a tap outside then collapses the empty box.
    for (const how of ['Escape', 'Close']) {
      await page.locator('button.attach').click(); await page.locator('.attach-menu').waitFor()
      if (how === 'Escape') { await page.locator('.attach-menu button:visible').first().focus(); await page.keyboard.press('Escape') } else await page.locator('.attach-head button').click()
      await page.waitForTimeout(150)
      check(`${how} returns focus to +`, await page.evaluate(() => document.activeElement?.classList.contains('attach')))
      await page.locator('.chat-scroll').first().click({ position: { x: 20, y: 150 } }); await page.waitForTimeout(150)
      check(`after ${how}, a tap outside collapses the empty box`, await form.evaluate(el => el.classList.contains('compact')))
    }
    // A tap on the backdrop closes the sheet and collapses the box at once.
    await page.locator('button.attach').click(); await page.locator('.attach-menu').waitFor()
    await page.mouse.click(200, 120); await page.waitForTimeout(200)
    check('a tap outside the sheet closes it and collapses the box', await page.locator('.attach-menu').count() === 0 && await form.evaluate(el => el.classList.contains('compact')))

    // A slow save, then a change of conversation: the old failure must not undo the new conversation's rating.
    const tab = name => page.getByRole('tab', { name: new RegExp(name) }).first()
    const live = () => page.locator('.chat-page .assistant-message .message-actions').last()
    let held = hold()
    await live().getByRole('button', { name: 'Good response' }).click()
    await tab('Docs pass').click()
    await page.waitForFunction(() => [...document.querySelectorAll('.chat-page .assistant-message .message-actions [aria-label="Good response"]')].some(item => item.getAttribute('aria-pressed') === 'true' && item.checkVisibility()), null, { timeout: 8000 }).catch(() => {})
    check('the other conversation shows its own thumbs up', await live().getByRole('button', { name: 'Good response' }).getAttribute('aria-pressed') === 'true')
    held.release(500); await page.waitForTimeout(400)
    check('a late failure in the first conversation leaves the other conversation unchanged', await live().getByRole('button', { name: 'Good response' }).getAttribute('aria-pressed') === 'true')

    // A slow "Tell the agent", then a change of conversation: nothing is sent to the other conversation.
    await tab('Deploy fix').click(); await page.waitForTimeout(600)
    posts.length = 0
    held = hold()
    await live().getByRole('button', { name: 'Bad response' }).click()
    const sheet2 = page.getByRole('dialog', { name: 'What went wrong?' })
    await sheet2.getByRole('button', { name: 'Too long' }).click()
    await sheet2.getByRole('button', { name: 'Tell the agent' }).click()
    // The sheet is modal, so the switch comes from outside it (a back gesture, a closed pane): a script click.
    await tab('Docs pass').evaluate(el => el.click()); await page.waitForTimeout(600)
    held.release(200); await page.waitForTimeout(600)
    check('the held save goes to the first conversation', posts.some(p => p.ep === 'feedback' && p.body.thread === 't1' && p.body.rating === 'down'), JSON.stringify(posts))
    check('after a tab switch, a late "Tell the agent" still goes to the rated tab and conversation', posts.filter(p => p.ep === 'message').every(p => p.body.session === '%1' && p.body.thread === 't1'), JSON.stringify(posts.filter(p => p.ep === 'message')))
    await page.keyboard.press('Escape')

    // A new conversation in the same tab (as after /clear) while the save waits: nothing is sent to it.
    await tab('Deploy fix').click(); await page.waitForTimeout(600)
    await live().getByRole('button', { name: 'Bad response' }).click()
    if (await page.getByRole('dialog', { name: 'What went wrong?' }).count() === 0) await live().getByRole('button', { name: 'Bad response' }).click()
    posts.length = 0; gets.length = 0
    held = hold()
    const sheet3 = page.getByRole('dialog', { name: 'What went wrong?' })
    await sheet3.getByRole('button', { name: 'Not correct' }).click()
    await sheet3.getByRole('button', { name: 'Tell the agent' }).click()
    const oldThread = fixture.chat.thread
    fixture.chat.thread = 't9'
    await page.waitForFunction(() => true); for (let wait = 0; wait < 40 && !gets.includes('t9'); wait++) await page.waitForTimeout(250)
    check('the tab moved to the new conversation', gets.includes('t9'), JSON.stringify(gets))
    held.release(200); await page.waitForTimeout(600)
    check('a late "Tell the agent" sends nothing to a new conversation in the same tab', !posts.some(p => p.ep === 'message'), JSON.stringify(posts.filter(p => p.ep === 'message')))
    check('it says the message was not sent', /not sent/.test(await sheet3.locator('.error').textContent().catch(() => '')))
    fixture.chat.thread = oldThread
    await page.keyboard.press('Escape')

    // Two changes to one reply overlap and both fail: the reply shows what the server holds, not the first unsaved change.
    const overlap = async (name, start) => {
      failing = true
      const gateNow = hold()
      await live().getByRole('button', { name: 'Good response' }).click()
      await live().getByRole('button', { name: 'Bad response' }).click()
      const dialog = page.getByRole('dialog', { name: 'What went wrong?' })
      await dialog.getByRole('button', { name: 'Save only' }).click()
      await page.waitForTimeout(200)
      gateNow.release(500); await page.waitForTimeout(600)
      check(`${name}: the sheet says the rating was not saved`, (await dialog.locator('.error').textContent().catch(() => '')).length > 0)
      await page.keyboard.press('Escape'); await page.waitForTimeout(200)
      const pressed = async label => await live().getByRole('button', { name: label }).getAttribute('aria-pressed') === 'true'
      check(`${name}: the reply goes back to ${start || 'no rating'}`, await pressed('Good response') === (start === 'thumbs up') && !(await pressed('Bad response')))
      failing = false
    }
    ratings = {}
    await page.goto(`${origin}/chat/?session=%251`); await live().waitFor(); await page.waitForTimeout(400)
    await overlap('unrated reply', '')
    await tab('Docs pass').click(); await page.waitForTimeout(800)
    check('the second conversation starts with a saved thumbs up', await live().getByRole('button', { name: 'Good response' }).getAttribute('aria-pressed') === 'true')
    await overlap('reply with a saved thumbs up', 'thumbs up')

    // A rating that cannot save goes back and says why.
    fixture.chat.messages[1].id = 'fail'
    ratings = {}
    await page.goto(`${origin}/chat/?session=%251`); await page.locator('.chat-page .assistant-message .message-actions').last().waitFor(); await page.waitForTimeout(300)
    const fresh = page.locator('.chat-page .assistant-message .message-actions').last()
    await fresh.getByRole('button', { name: 'Good response' }).click(); await page.waitForTimeout(300)
    check('a failed save says it was not saved', /Not saved/.test(await fresh.locator('.rate-error').textContent().catch(() => '')))
    check('a failed save leaves the thumbs up off', await fresh.getByRole('button', { name: 'Good response' }).getAttribute('aria-pressed') === 'false')
  } catch (error) { console.log('FAIL', error.stack); failed++ }
  finally { await context.close(); server.close() }
  console.log(failed ? `${failed} failed` : 'reply actions: all checks pass')
  process.exit(failed ? 1 : 0)
})()
