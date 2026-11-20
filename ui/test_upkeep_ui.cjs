// Upkeep (src/Upkeep.tsx) against a build, with fixture data only: the clean-up sheet (B211) and the daily ops report
// in Settings (B212). It checks loading, empty and error states, that each removal is one tap on one item, Undo, the
// candidate Add and Dismiss, the postmortem sheet, keyboard reach, and no sideways scroll at 320 and 1280 px.
// Run: cd ui && npm run test:ui (or DIST=<build> SHOTS=<folder> node test_upkeep_ui.cjs).
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || ''
const ORIGIN = 'http://localhost:7812'
const NOW = Date.now() / 1000
const DAY = 86400
const lesson = (id, text, extra = {}) => ({ id, mark: `m-${id}`, key: `lesson:aaaaaaaaaaaa:${id}`, text, kind: 'avoid', source: 'user', agent: 'claude', hits: 1, created: NOW - 60 * DAY, last_hit: NOW - 60 * DAY,
  proposed_global: false, status: 'trial', quote: '', tab: 'Deploy guard', first_pane: '%4', disputes: [], reviewed_at: null, stuck: false, disputed: false, project: 'aaaaaaaaaaaa', project_name: 'tether', ...extra })
const note = (name, title, days) => ({ mark: `m-${name}`, key: `note:aaaaaaaaaaaa:${name}`, project: 'aaaaaaaaaaaa', project_name: 'tether', name, title, size: 900, mtime: (NOW - days * DAY) * 1000 })
const fresh = () => ({
  prune: { last_run: NOW - 2 * DAY, next_run: NOW + 5 * DAY, stale_days: 30, note_days: 30, undo_days: 7, skipped: [], removed: [],
    lessons: [lesson('11111111', 'When a deploy script prints a warning, read the whole warning before the next step.'),
      lesson('22222222', 'When a long unbroken path such as /srv/projects/a-very-long-folder-name/and-another-one/file.tsx appears, wrap it.', { project: null, project_name: null, key: 'lesson:global:22222222', kind: 'keep' })],
    notes: [note('brief-slow-network.md', 'Brief: slow network fixes', 64), note('gesture-audit.md', 'Gesture audit', 41)] },
  ops: { enabled: false, hour: 7, model: 'Claude Haiku, low effort', tabs: 3, busy: false, skipped: [],
    runs: [{ day: '2026-11-17', project: 'aaaaaaaaaaaa', project_name: 'tether', status: 'started', why: '', pane: '%41', at: NOW - 3600, note: true, problems: 4 },
      { day: '2026-11-16', project: 'aaaaaaaaaaaa', project_name: 'tether', status: 'quiet', why: 'Every commit and deploy was verified. No tab was needed.', pane: '', at: NOW - DAY, note: true, problems: 0 },
      { day: '2026-11-15', project: 'bbbbbbbbbbbb', project_name: 'briskbot', status: 'failed', why: 'The session has no open tab now.', pane: '', at: NOW - 2 * DAY, note: false, problems: 2 }],
    candidates: [{ id: 'c1c1c1c1', text: 'When a commit has no stamp, write Verified: none with the reason.', kind: 'avoid', quote: '3 commits on 17 Nov had no Verified line', tab: 'Ops report 2026-11-17', at: NOW - 3000, project: 'aaaaaaaaaaaa', project_name: 'tether' },
      { id: 'c2c2c2c2', text: 'When a review pauses at the round limit, split the change before the next round.', kind: 'avoid', quote: '', tab: 'Ops report 2026-11-17', at: NOW - 2900, project: 'aaaaaaaaaaaa', project_name: 'tether' }] },
})
const noteText = '# Postmortem 2026-11-17: tether\n\n## Numbers\n- Commits: 14 (11 verified, 2 "Verified: none", 1 no stamp)\n- Deploys: 5 (5 verified)\n\n## Commits without verification\n- `4c1e9a0` 11:20 Tasks: one list (no Verified stamp; tether)\n\n## What to learn\n- One commit had no stamp: the commit was made from a second terminal that has no hook.\n'
const base = {
  chat: { messages: [], busy: false, thread: '%1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] }, subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] }, update: { current: '0.0.0', latest: '0.0.0', available: false },
  host: { name: 'Studio', office: false }, catalog: { commands: [], plugins: [], mcp: [], note: '' }, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null }, 'work/board': { projects: [], items: [] },
}
const panes = [{ id: '%1', tmux_session: '$1', label: 'tether:%1', pane_name: 'Automation', agent: 'claude', dir: '/srv/tether', state: 'Idle', writable: true, thread: '%1', approval: null }]
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }

async function main() {
  let browser, page, failed = 0
  const pageErrors = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  const state = { ...fresh(), posts: [], mode: 'ok', delay: 0 }
  // The fake server keeps the lists, like prune.py and ops_agent.py do.
  function post(endpoint, body) {
    state.posts.push({ endpoint, body })
    if (state.mode === 'refuse') return { status: 409, json: { error: 'This project has 60 lessons. Remove one first, then undo again.' } }
    if (endpoint === 'upkeep/prune') {
      const data = state.prune
      if (body.action === 'remove' || body.action === 'keep') {
        const list = body.kind === 'lesson' ? data.lessons : data.notes
        const index = list.findIndex(item => (body.kind === 'lesson' ? item.id : item.name) === body.id)
        if (index < 0) return { status: 404, json: { error: 'That item is no longer in the clean-up list.' } }
        const [item] = list.splice(index, 1)
        if (body.action === 'remove') data.removed.unshift({ token: `t-${body.id}`, kind: body.kind, at: NOW, until: NOW + 7 * DAY, project_name: item.project_name || 'All projects', text: item.text || item.title, name: item.name || '', item })
      } else if (body.action === 'restore') data.removed = data.removed.filter(item => item.token !== body.token)
      else if (body.action === 'check') data.last_run = NOW
      return { json: data }
    }
    const data = state.ops
    if ('enabled' in body) data.enabled = body.enabled
    else if (body.action === 'run') data.busy = true
    else data.candidates = data.candidates.filter(item => item.id !== body.id)
    return { json: data }
  }
  try {
    if (SHOTS) await fs.mkdir(SHOTS, { recursive: true })
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.stack))
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== ORIGIN) return route.abort('blockedbyclient')
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (endpoint === 'prefs') return route.fulfill({ json: { prefs: { selected: '%1' }, version: 1 } })
        if (endpoint.startsWith('upkeep/')) {
          if (state.delay) await new Promise(resolve => setTimeout(resolve, state.delay))
          if (state.mode === 'down') return route.fulfill({ status: 500, json: { error: 'Tether could not read or write its files.' } })
          if (request.method() !== 'GET') return route.fulfill(post(endpoint, request.postDataJSON()))
          if (endpoint === 'upkeep/ops/note') return route.fulfill({ json: { project: 'aaaaaaaaaaaa', day: url.searchParams.get('day'), path: '~/.tether/projects/aaaaaaaaaaaa/notes/postmortems/2026-11-17.md', text: noteText } })
          return route.fulfill({ json: state[endpoint.split('/')[1]] })
        }
        if (request.method() !== 'GET') return route.fulfill({ json: { ok: true } })
        if (endpoint === 'sessions') return route.fulfill({ json: { sessions: panes, default_thread: '%1' } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: base.chat })
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: base[endpoint] || {} })
      }
      if (!url.pathname.startsWith('/chat/')) return route.fulfill({ status: 404, body: '' })
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) return route.fulfill({ status: 404, body: '' })
      try { return route.fulfill({ body: await fs.readFile(file), contentType: mime[path.extname(file)] || 'application/octet-stream' }) } catch { return route.fulfill({ status: 404, body: '' }) }
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      if (sessionStorage.getItem('upkeep-test-seeded')) return
      sessionStorage.setItem('upkeep-test-seeded', '1')
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false, selected: '%1' }))
    })
    const overflow = scope => page.evaluate(selector => { const el = document.querySelector(selector); return document.documentElement.scrollWidth > innerWidth || (!!el && el.scrollWidth > el.clientWidth + 1) }, scope)
    const small = scope => page.evaluate(selector => [...document.querySelectorAll(`${selector} button`)].filter(el => el.getClientRects().length).map(el => el.getBoundingClientRect()).filter(box => box.width < 24 || box.height < 24).length, scope)
    const reset = () => Object.assign(state, fresh(), { posts: [], mode: 'ok', delay: 0 })
    const sheet = page.locator('dialog.upkeep-sheet[open]')

    for (const [width, height] of [[1280, 800], [320, 640]]) {
      const at = `${width}:`
      reset()
      await page.setViewportSize({ width, height })

      // ---- the clean-up sheet, from its notification link ----
      state.delay = 4000
      await page.goto(`${ORIGIN}/chat/?go=cleanup`)
      await sheet.locator('.skeleton[aria-busy="true"]').waitFor()
      check(`${at} clean-up shows a loading state that names itself`, (await sheet.locator('.skeleton [role="status"]').textContent()) === 'Loading the clean-up list')
      state.delay = 0
      await sheet.getByRole('heading', { name: /Lessons that never repeated/ }).waitFor()
      check(`${at} lists 2 lessons and 2 notes`, await sheet.locator('.review-lesson').count() === 4)
      check(`${at} says nothing goes without a tap`, (await sheet.innerText()).includes('It removes nothing itself: each Remove button removes one item, and you can undo it for 7 days.'))
      check(`${at} nothing was sent on open`, state.posts.length === 0)
      check(`${at} no sideways scroll`, !(await overflow('dialog.upkeep-sheet .review-body')))
      check(`${at} targets are at least 24 px`, await small('dialog.upkeep-sheet') === 0)
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `cleanup-list-${width}.png`) })

      // One tap removes one item, and only that item.
      await sheet.getByRole('button', { name: /^Remove the lesson: When a deploy script/ }).click()
      await sheet.getByRole('heading', { name: /^Removed/ }).waitFor()
      check(`${at} one tap sends one remove for that lesson`, JSON.stringify(state.posts) === JSON.stringify([{ endpoint: 'upkeep/prune', body: { action: 'remove', kind: 'lesson', project: 'aaaaaaaaaaaa', id: '11111111', mark: 'm-11111111' } }]), JSON.stringify(state.posts))
      check(`${at} the other items stay`, await sheet.getByRole('button', { name: /^Remove the / }).count() === 3)
      check(`${at} the removed row says how long undo works`, (await sheet.locator('section[aria-labelledby="cleanup-removed"]').innerText()).includes('undo for 7 more days'))
      // Keyboard: the note's Remove button works with Enter.
      await sheet.getByRole('button', { name: 'Remove the note: Gesture audit' }).focus()
      await page.keyboard.press('Enter')
      await sheet.getByRole('button', { name: 'Undo the removal of: Gesture audit' }).waitFor()
      check(`${at} Enter on Remove sends the note with the version the list showed`, JSON.stringify(state.posts[1].body) === JSON.stringify({ action: 'remove', kind: 'note', project: 'aaaaaaaaaaaa', id: 'gesture-audit.md', mark: 'm-gesture-audit.md' }))
      await sheet.getByRole('button', { name: /^Keep the lesson: When a long unbroken/ }).click()
      await sheet.getByRole('heading', { name: /Lessons that never repeated/ }).waitFor({ state: 'detached' })
      check(`${at} Keep on a lesson for all projects sends project null`, JSON.stringify(state.posts[2].body) === JSON.stringify({ action: 'keep', kind: 'lesson', project: null, id: '22222222' }))
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `cleanup-removed-${width}.png`) })
      // An undo the server refuses says why and keeps the row.
      state.mode = 'refuse'
      await sheet.getByRole('button', { name: /^Undo the removal of: When a deploy script/ }).click()
      await sheet.getByRole('alert').getByText('This project has 60 lessons. Remove one first, then undo again.').waitFor()
      check(`${at} a refused undo keeps the row`, await sheet.getByRole('button', { name: /^Undo the removal of/ }).count() === 2)
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `cleanup-error-${width}.png`) })
      state.mode = 'ok'
      await sheet.getByRole('button', { name: /^Undo the removal of: When a deploy script/ }).click()
      await sheet.getByRole('button', { name: /^Undo the removal of: When a deploy script/ }).waitFor({ state: 'detached' })
      check(`${at} Undo sends the token`, JSON.stringify(state.posts.at(-1).body) === JSON.stringify({ action: 'restore', token: 't-11111111' }))
      await sheet.getByRole('button', { name: 'Undo the removal of: Gesture audit' }).click()
      await sheet.getByRole('button', { name: 'Remove the note: Brief: slow network fixes' }).click()
      await sheet.getByRole('button', { name: 'Undo the removal of: Brief: slow network fixes' }).click()
      await sheet.getByText(/Nothing is stale\. Last check:/).waitFor()
      check(`${at} the empty state names the last check`, true)
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `cleanup-empty-${width}.png`) })
      const [request] = await Promise.all([
        page.waitForRequest(req => req.method() === 'POST' && new URL(req.url()).pathname.endsWith('/api/upkeep/prune')),
        sheet.getByRole('button', { name: 'Check now' }).click(),
      ])
      check(`${at} Check now sends check`, JSON.stringify(request.postDataJSON()) === JSON.stringify({ action: 'check' }))
      await page.keyboard.press('Escape')
      await sheet.waitFor({ state: 'detached' })
      check(`${at} Escape closes the sheet`, true)

      // The server is down: an error with a retry, not an empty list.
      state.mode = 'down'
      await page.goto(`${ORIGIN}/chat/?go=cleanup`)
      await sheet.getByRole('alert').getByText('Tether could not read or write its files.').waitFor()
      check(`${at} a failed load shows the error and no "nothing is stale"`, !(await sheet.innerText()).includes('Nothing is stale'))
      state.mode = 'ok'
      reset()
      await sheet.getByRole('button', { name: 'Try again' }).click()
      await sheet.getByRole('heading', { name: /Lessons that never repeated/ }).waitFor()
      check(`${at} Try again loads the list`, true)

      // ---- Settings › Agents: the entry and the daily ops report ----
      await page.goto(`${ORIGIN}/chat/?go=notifications`)
      const settings = page.locator('dialog.preferences[open]')
      await settings.getByPlaceholder(/Search settings/).fill('clean up')
      const entry = settings.locator('[data-setting="cleanup"]')
      await entry.getByText('2 lessons and 2 notes look stale').waitFor()
      check(`${at} Settings names the counts`, true)
      await entry.getByRole('button', { name: 'Review' }).click()
      await sheet.getByRole('heading', { name: 'Clean up lessons and notes' }).waitFor()
      await sheet.getByRole('button', { name: 'Done' }).click()
      await sheet.waitFor({ state: 'detached' })
      check(`${at} Review opens the sheet and Done closes it`, true)

      await settings.getByPlaceholder(/Search settings/).fill('ops report')
      const ops = settings.locator('[data-setting="ops-report"]')
      const toggle = ops.getByRole('switch', { name: /Write a postmortem each day/ })
      await toggle.waitFor()
      check(`${at} the report is off by default`, !(await toggle.isChecked()))
      check(`${at} the switch says what it costs`, (await ops.innerText()).includes('This uses tokens each day: at most 3 tabs, and none on a day with no problem.') && (await ops.innerText()).includes('Claude Haiku, low effort'))
      await toggle.focus()
      await page.keyboard.press('Space')
      await page.waitForFunction(() => document.querySelector('[data-setting="ops-report"] input[role="switch"]')?.checked)
      check(`${at} Space turns it on`, JSON.stringify(state.posts.at(-1)) === JSON.stringify({ endpoint: 'upkeep/ops', body: { enabled: true } }))
      const flag = await ops.locator('.upkeep-flag').first().evaluate(el => { const lum = value => { const [r, g, b] = value.match(/[\d.]+/g).slice(0, 3).map(n => { const c = n / 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }); return 0.2126 * r + 0.7152 * g + 0.0722 * b }; const style = getComputedStyle(el); const a = lum(style.color), b = lum(style.backgroundColor); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) })
      check(`${at} the Proposed label has 4.5:1 contrast`, flag >= 4.5, String(flag))
      check(`${at} 2 proposed lessons and 3 runs`, await ops.locator('ul[aria-labelledby="ops-candidates"] li').count() === 2 && await ops.locator('ul[aria-labelledby="ops-runs"] li').count() === 3)
      check(`${at} a failed run says why`, (await ops.locator('ul[aria-labelledby="ops-runs"]').innerText()).includes('Not started. The session has no open tab now.'))
      check(`${at} no sideways scroll in Settings`, !(await overflow('dialog.preferences .preferences-content')))
      check(`${at} Settings targets are at least 24 px`, await small('[data-setting="ops-report"]') === 0)
      if (SHOTS) { await ops.scrollIntoViewIfNeeded(); await page.screenshot({ path: path.join(SHOTS, `ops-settings-${width}.png`) }) }
      await ops.getByRole('button', { name: /^Add the lesson: When a commit has no stamp/ }).click()
      await ops.getByRole('button', { name: /^Add the lesson: When a commit has no stamp/ }).waitFor({ state: 'detached' })
      check(`${at} Add sends accept for that candidate`, JSON.stringify(state.posts.at(-1).body) === JSON.stringify({ action: 'accept', project: 'aaaaaaaaaaaa', id: 'c1c1c1c1' }))
      await ops.getByRole('button', { name: /^Dismiss the proposed lesson/ }).click()
      await ops.getByText('None. Agents do not get a proposed lesson until you add it.').waitFor()
      check(`${at} Dismiss sends dismiss, then the empty state`, JSON.stringify(state.posts.at(-1).body) === JSON.stringify({ action: 'dismiss', project: 'aaaaaaaaaaaa', id: 'c2c2c2c2' }))
      await ops.getByRole('button', { name: 'Run now' }).click()
      await ops.getByRole('button', { name: 'Running…' }).waitFor()
      check(`${at} Run now sends run and shows Running`, JSON.stringify(state.posts.at(-1).body) === JSON.stringify({ action: 'run' }) && await ops.getByRole('button', { name: 'Running…' }).isDisabled())
      await ops.getByRole('button', { name: 'Read the postmortem of 2026-11-17, tether' }).click()
      const post_ = page.locator('dialog.upkeep-sheet[open]')
      await post_.locator('pre.upkeep-note').waitFor()
      check(`${at} the postmortem shows the note and its path`, (await post_.innerText()).includes('## What to learn') && (await post_.innerText()).includes('notes/postmortems/2026-11-17.md'))
      check(`${at} no sideways scroll in the postmortem`, !(await overflow('dialog.upkeep-sheet .review-body')))
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `ops-postmortem-${width}.png`) })
      await page.keyboard.press('Escape')
      await post_.waitFor({ state: 'detached' })
      check(`${at} Escape closes the postmortem, Settings stays`, await settings.isVisible())

      // Dark theme and the ops error state.
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'dark' })
      state.mode = 'down'
      await page.goto(`${ORIGIN}/chat/?go=notifications`)
      await settings.getByPlaceholder(/Search settings/).fill('ops report')
      await ops.getByRole('alert').getByText('Tether could not read or write its files.').waitFor()
      check(`${at} the ops report shows a load error with a retry`, await ops.getByRole('button', { name: 'Try again' }).isVisible())
      state.mode = 'ok'
      await ops.getByRole('button', { name: 'Try again' }).click()
      await toggle.waitFor()
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `ops-settings-${width}-dark.png`) })
      await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    }
    check('no page errors', !pageErrors.length, pageErrors.join('\n'))
  } catch (error) {
    failed++
    console.log(`FAIL ${error.stack}`)
    if (pageErrors.length) console.log(pageErrors.join('\n'))
  } finally {
    await page?.close().catch(() => {})
    await browser?.close().catch(() => {})
  }
  console.log(failed ? `upkeep ui: ${failed} failed` : 'upkeep ui: ok')
  process.exitCode = failed ? 1 : 0
}
main()
