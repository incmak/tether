// Auto-merge in Settings › Agents (AutoMerge.tsx), against an existing build. Every API request stays in this page's
// fixtures; no terminal and no repository is touched. Run: cd ui && npm run test:ui (or node test_auto_merge_ui.cjs after a build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser, serveLocal } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-18/auto-merge/shots')
const sizes = [[320, 640], [1280, 900]]
const now = Math.floor(Date.now() / 1000)
const ROOT = '/srv/project'
const sha = letter => letter.repeat(40)
const passing = [
  { key: 'approval', ok: true, text: 'The reviewer approved commit aaaaaaaaaaaa.' },
  { key: 'branch', ok: true, text: 'Worker branch settings-copy, with no uncommitted changes.' },
  { key: 'rules', ok: true, text: 'The areas come from docs/auto-merge.json on origin/main.' },
  { key: 'stamp', ok: true, text: 'The stamp names tests(server 412 ok, ui 91/93). The review is the approval above.' },
  { key: 'scope', ok: true, text: 'Every file is in an area within its trust limit: UI 42 lines.' },
  { key: 'checklist', ok: true, text: 'The diff hits no reliability checklist row.' },
  { key: 'merge', ok: true, text: 'The commit merges into origin/main with no conflict.' },
]
const failing = passing.map(item => item.key === 'scope' ? { key: 'scope', ok: false, text: 'Files outside the auto-merge areas: server/a_very_long_module_name_that_must_wrap_on_a_phone_screen.py, server/auth.py.' } : item)
const decision = (extra) => ({ at: now - 300, mode: 'observe', outcome: 'would_merge', repo: 'project', root: ROOT, branch: 'settings-copy', head: sha('a'), target: 'origin/main',
  name: 'Settings copy', subject: 'Settings: shorter notes', checks: passing, areas: { ui: 42 }, files: 3, merge: null, error: null, note: null, ...extra })
const decisions = [
  decision({}),
  decision({ at: now - 4000, outcome: 'wait', name: 'Auth refactor with a very long tab name that has to wrap', branch: 'auth-refactor', head: sha('b'), checks: failing }),
  decision({ at: now - 90000, mode: 'enforce', outcome: 'merged', name: 'Test fixtures', branch: 'fixtures', head: sha('c'), merge: sha('d') }),
  decision({ at: now - 200000, mode: 'enforce', outcome: 'failed', name: null, branch: 'retry-copy', head: sha('e'), error: 'git push failed: remote rejected.' }),
]
const modes = [['off', 'Off', 'Tether does not check branches.'], ['observe', 'Observe', 'Tether records what it would merge. It merges nothing.'],
  ['enforce', 'Enforce', 'Tether merges a branch into the main branch when every check passes.']].map(([mode, name, summary]) => ({ mode, name, summary }))
const area = (name, label, extra) => ({ name, label, rung: 1, limit: 60, streak: 2, need: 5, top: false, merges: 2, reverts: 0, last_revert: null, ...extra })
const full = () => ({ mode: 'observe', since: 0, modes, decisions, rungs: [0, 60, 150, 300], rules_file: 'docs/auto-merge.json',
  trust: [{ root: ROOT, repo: 'project', target: 'origin/main', error: '', areas: [area('tests', 'Tests', { rung: 3, limit: 300, top: true, streak: 1, merges: 11 }), area('ui', 'UI', {}), area('docs', 'Docs', { rung: 0, limit: 0, streak: 0, merges: 1, reverts: 1 })] },
    { root: '/srv/other', repo: 'other', areas: [], error: 'origin/main has no valid docs/auto-merge.json. Nothing auto-merges in this repository.' }] })
const pane = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Auto-merge test', agent: 'claude', dir: ROOT, state: 'Idle', writable: true, thread: 't1', approval: null }
const fixture = {
  undo: { entries: [], next_undo: null, next_redo: null, next_undo_label: null, next_redo_label: null },
  sessions: { sessions: [pane], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] }, subagents: { main: null, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'autonomy/review': { due: false, reason: null, new: 0, disputed: 0, total: 0, last_review: 0, snoozed_until: 0, groups: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
  'work/board': { revision: 1, projects: [], items: [], stages: [] }, 'push/config': { enabled: false },
}

async function main() {
  let browser, page, server, failed = 0
  const pageErrors = [], unexpected = new Set()
  let mode = 'rows'  // rows | empty | error | slow
  let state = full(), postFails = false, hasKey = true
  const approved = body => !!body.approval?.challenge_id && !!body.approval?.credential?.response?.signature
  const b64 = bytes => Buffer.from(bytes).toString('base64url')
  const posts = [], passkeyAsks = []
  const check = (name, ok, why = '') => { if (ok) console.log(`ok ${name}`); else { failed++; console.log(`FAIL ${name} ${why}`) } }
  try {
    await fs.mkdir(SHOTS, { recursive: true })
    server = http.createServer(async (req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const file = url.pathname === '/chat/' ? path.join(DIST, 'index.html') : path.resolve(DIST, '.' + decodeURIComponent(url.pathname.slice('/chat'.length)))
      if (!url.pathname.startsWith('/chat/') || url.pathname.endsWith('/sw.js') || !(file === path.join(DIST, 'index.html') || file.startsWith(DIST + path.sep))) { res.writeHead(404); res.end(); return }
      try {
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json' }
        const bytes = await fs.readFile(file)
        res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }); res.end(bytes)
      } catch { res.writeHead(404); res.end() }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await getBrowser()
    const context = browser.contexts()[0]
    if (!context || context.pages().length >= 3) throw new Error('Shared browser has no context or already has three tabs')
    page = await context.newPage()
    page.setDefaultTimeout(10000)
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
    await page.routeWebSocket('**/*', socket => socket.close())
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET' && endpoint === 'prefs') return route.fulfill({ json: { prefs: {}, version: 1 } })
        if (request.method() === 'POST' && endpoint === 'ui-usage') return route.fulfill({ json: { gen: '' } })
        if (endpoint === 'auto-merge') {
          if (request.method() === 'POST') {
            const body = request.postDataJSON()
            posts.push(body)
            if (postFails) return route.fulfill({ status: 500, json: { error: 'Could not save the auto-merge setting.' } })
            // Like the server: Enforce and Restore need a passkey approval; Off and Observe do not.
            if ((body.mode === 'enforce' || body.restore) && !approved(body)) return route.fulfill({ status: 403, json: hasKey ? { error: 'Approve with your passkey.', approval: 'required' } : { error: 'Add a passkey in Settings › Setup first.', approval: 'no-passkey' } })
            if (body.mode) state = { ...state, mode: body.mode, since: now, paused: false }
            if (body.restore) state = { ...state, trust: state.trust.map(repo => ({ ...repo, areas: repo.areas.map(item => repo.root === body.restore.root && item.name === body.restore.area ? { ...item, rung: 1, limit: 60 } : item) })) }
            return route.fulfill({ json: state })
          }
          if (mode === 'error') return route.fulfill({ status: 500, json: { error: 'Tether could not read the auto-merge state.' } })
          if (mode === 'slow') await new Promise(resolve => setTimeout(resolve, 1500))
          return route.fulfill({ json: mode === 'empty' ? { ...state, decisions: [], trust: [] } : state })
        }
        if (endpoint === 'passkeys' && request.method() === 'POST') {
          const body = request.postDataJSON()
          passkeyAsks.push(body)
          return route.fulfill({ json: { challenge_id: 'challenge-1', publicKey: { challenge: b64(Buffer.alloc(32, 3)), rpId: 'localhost', userVerification: 'required', timeout: 60000, allowCredentials: [{ type: 'public-key', id: b64('cred-1') }] } } })
        }
        if (request.method() !== 'GET') { unexpected.add(`POST ${endpoint}`); return route.fulfill({ status: 500, json: { error: 'no writes here' } }) }
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })  // the other Settings sections load their own data; this test does not check them
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return serveLocal(route)
      unexpected.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      // A fake authenticator: the shared browser's CDP proxy refuses the WebAuthn domain. server/test_approval.py checks real signatures.
      const buffer = text => new TextEncoder().encode(text).buffer
      Object.defineProperty(navigator, 'credentials', { configurable: true, value: {
        get: async () => ({ id: 'cred-1', rawId: buffer('cred-1'), type: 'public-key', response: { clientDataJSON: buffer('{"type":"webauthn.get"}'), authenticatorData: buffer('auth'), signature: buffer('sig'), userHandle: null } }),
      } })
      localStorage.clear()
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const section = page.locator('section.auto-merge')
    const open = async width => {
      await page.goto(`${origin}/chat/?session=%251`, { waitUntil: 'load' })
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      await page.getByPlaceholder(/Search settings/).fill('auto-merge')
      await section.waitFor()
    }
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    const shot = async name => {
      await section.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
      await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
      await page.screenshot({ path: path.join(SHOTS, `${name}-dark.png`) })
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
    }
    // WCAG 2.2 1.4.3: each text against the background it sits on (every ancestor layer composited), 4.5:1.
    const contrast = selector => section.evaluate((node, selector) => {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      const paint = layers => {
        ctx.globalCompositeOperation = 'source-over'; ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1, 1)
        for (const color of layers) { ctx.fillStyle = '#fff'; ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1) }
        return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3)
      }
      const lum = rgb => { const c = rgb.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] }
      return [...node.querySelectorAll(selector)].filter(el => el.getClientRects().length).map(el => {
        const layers = []
        for (let at = el; at; at = at.parentElement) layers.unshift(getComputedStyle(at).backgroundColor)
        const back = paint(layers), text = paint([...layers, getComputedStyle(el).color])
        const [hi, lo] = [lum(back), lum(text)].sort((a, b) => b - a)
        return { text: el.textContent.trim().slice(0, 24), ratio: Math.round((hi + 0.05) / (lo + 0.05) * 100) / 100 }
      })
    }, selector)
    const TEXT = '.auto-merge-outcome, .auto-merge-what strong, .auto-merge-what small, .auto-merge-areas strong, .auto-merge-areas small, .auto-merge-checks span, .setting-note, .hint, .auto-merge-heading, .auto-merge-confirm p, .auto-merge-repo-name small, .segmented span, button'
    const low = list => list.filter(item => item.ratio < 4.5).map(item => `${item.text} ${item.ratio}`).join('; ')

    for (const [width, height] of sizes) {
      const at = `${width}px`
      await page.setViewportSize({ width, height })
      state = full(); posts.length = 0; passkeyAsks.length = 0; postFails = false; hasKey = true

      mode = 'slow'
      await open(width)
      check(`${at} loading skeleton announces itself`, (await section.getByRole('status').first().innerText()).includes('Loading auto-merge'))
      mode = 'rows'
      await section.locator('.auto-merge-decision').first().waitFor()

      // The mode: Observe by default, with its meaning.
      check(`${at} Observe is checked`, await section.getByRole('radio', { name: 'Observe' }).isChecked())
      check(`${at} the mode note says it merges nothing`, (await section.locator('#auto-merge-note').innerText()).includes('It merges nothing.'))

      // Trust by area: the limit, the count to the next step, the top step and zero trust.
      const areas = await section.locator('.auto-merge-areas > li').allInnerTexts()
      check(`${at} three areas`, areas.length === 3, String(areas.length))
      check(`${at} top step says so`, /Tests[\s\S]*Up to 300 changed lines in one merge\. This is the highest step\./.test(areas[0]), areas[0])
      check(`${at} next step and count`, /UI[\s\S]*Up to 60 changed lines in one merge\. 2 of 5 clean merges to the next step \(150 lines\)\./.test(areas[1]), areas[1])
      check(`${at} zero trust says who merges`, /Docs[\s\S]*Trust is at zero after a revert\. You merge this area until you restore it\./.test(areas[2]), areas[2])
      check(`${at} counts use singular and plural`, areas[2].includes('1 merge by Tether · 1 revert') && areas[0].includes('11 merges by Tether · 0 reverts'), areas[2])
      check(`${at} only the zero-trust area has Restore`, await section.getByRole('button', { name: /Restore trust/ }).count() === 1)
      check(`${at} a repository without rules says so`, await section.getByText('origin/main has no valid docs/auto-merge.json. Nothing auto-merges in this repository.').isVisible())

      // Decisions: a word for each outcome, the first failed check in the row, all checks on open.
      const words = await section.locator('.auto-merge-outcome').allInnerTexts()
      check(`${at} outcome words, newest first`, words.join(',') === 'Would merge,Waits for you,Merged,Merge failed', words.join(','))
      const rows = section.locator('.auto-merge-decision')
      check(`${at} a waiting row shows why`, (await rows.nth(1).locator('summary').innerText()).includes('Files outside the auto-merge areas'))
      check(`${at} a merged row says it is not deployed`, (await rows.nth(2).locator('summary').innerText()).includes('Merge commit dddddddddddd on origin/main. Not deployed.'))
      check(`${at} a failed row shows the error`, (await rows.nth(3).locator('summary').innerText()).includes('git push failed: remote rejected.'))
      check(`${at} a row with no tab name uses the branch`, (await rows.nth(3).locator('strong').innerText()) === 'retry-copy')
      check(`${at} checks are closed at first`, !(await rows.nth(1).locator('.auto-merge-checks').isVisible()))
      await rows.nth(1).locator('summary').focus()
      await page.keyboard.press('Enter')
      const marks = await rows.nth(1).locator('.auto-merge-checks li > span:first-child').allInnerTexts()
      check(`${at} Enter opens the checks, with a word for each`, marks.join(',') === 'Pass,Pass,Pass,Pass,Wait,Pass,Pass', marks.join(','))
      await rows.nth(2).locator('summary').click()
      check(`${at} a merge shows how to undo it`, await rows.nth(2).getByText('git revert -m 1 dddddddddddd').isVisible())
      check(`${at} no page overflow (rows)`, !(await overflow()))
      const wide = await section.evaluate(node => [...node.querySelectorAll('*')].filter(el => el.getBoundingClientRect().right > innerWidth + 1).length)
      check(`${at} nothing runs past the screen`, wide === 0, String(wide))
      const targets = await section.locator('summary, button, .segmented label').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().height)))
      check(`${at} targets at least 24 px tall`, targets.every(h => h >= 24), targets.join(','))
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
        const found = await contrast(TEXT)
        check(`${at} ${theme} text contrast at least 4.5:1`, found.length > 30 && !low(found), low(found) || `${found.length} texts`)
      }
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
      await shot(`rows-${width}`)

      // Enforce asks first. Cancel sends nothing.
      await section.locator('.segmented label', { hasText: 'Enforce' }).click()
      const confirm = section.getByRole('group', { name: 'Turn on Enforce?' })
      await confirm.waitFor()
      check(`${at} Enforce asks before it saves`, posts.length === 0)
      check(`${at} the confirm text names the push, the undo and the deploy`, /never a forced push[\s\S]*git revert -m 1[\s\S]*A merge is not a deploy/.test(await confirm.innerText()))
      check(`${at} focus is on the confirm button`, await page.evaluate(() => document.activeElement?.textContent === 'Turn on Enforce'))
      check(`${at} no page overflow (confirm)`, !(await overflow()))
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
        const found = await contrast('.auto-merge-confirm p, .auto-merge-confirm strong, .auto-merge-confirm button, .auto-merge-confirm code')
        check(`${at} ${theme} confirm contrast at least 4.5:1`, found.length === 5 && !low(found), low(found) || `${found.length} texts`)
      }
      await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
      await shot(`confirm-${width}`)
      // The confirmation is an inline group, not a modal: Tab may leave it and it stays open; Escape and Cancel close it.
      await page.keyboard.press('Tab')
      check(`${at} the confirmation stays open when Tab leaves it`, await confirm.isVisible() && await page.evaluate(() => !document.activeElement?.closest('.auto-merge-confirm')))
      await confirm.getByRole('button', { name: 'Turn on Enforce' }).focus()
      await page.keyboard.press('Escape')
      await confirm.waitFor({ state: 'detached' })
      check(`${at} Escape cancels and Settings stays open`, posts.length === 0 && await section.isVisible() && await section.getByRole('radio', { name: 'Observe' }).isChecked())
      await section.locator('.segmented label', { hasText: 'Enforce' }).click()
      await confirm.getByRole('button', { name: 'Cancel' }).click()
      check(`${at} Cancel keeps Observe and sends nothing`, posts.length === 0 && await section.getByRole('radio', { name: 'Observe' }).isChecked())
      // Without a passkey nothing changes, and the message says what to do.
      hasKey = false
      await section.locator('.segmented label', { hasText: 'Enforce' }).click()
      await confirm.getByRole('button', { name: 'Turn on Enforce' }).click()
      await section.getByRole('alert').getByText('Add a passkey in Settings › Setup first.').waitFor()
      check(`${at} no passkey: Observe stays`, state.mode === 'observe' && await section.getByRole('radio', { name: 'Observe' }).isChecked() && passkeyAsks.length === 0)
      hasKey = true; posts.length = 0
      await section.locator('.segmented label', { hasText: 'Enforce' }).click()
      await confirm.getByRole('button', { name: 'Turn on Enforce' }).click()
      await section.getByText('Tether merges a branch into the main branch when every check passes.').waitFor()
      check(`${at} Enforce is saved only with a passkey approval`, posts.length === 2 && !approved(posts[0]) && approved(posts[1]) && posts[1].mode === 'enforce' && state.mode === 'enforce'
        && await section.getByRole('radio', { name: 'Enforce' }).isChecked(), JSON.stringify(posts.map(post => [post.mode, !!post.approval])))
      check(`${at} the passkey check is bound to this action`, passkeyAsks.length === 1 && passkeyAsks[0].action === 'auto-merge' && passkeyAsks[0].payload?.mode === 'enforce', JSON.stringify(passkeyAsks))

      // Off and Observe save at once. A failed save says so and keeps the old mode.
      await section.locator('.segmented label', { hasText: 'Off' }).click()
      await section.getByText('Tether does not check branches.').waitFor()
      check(`${at} Off saves at once, with no passkey`, JSON.stringify(posts.at(-1)) === '{"mode":"off"}' && passkeyAsks.length === 1)
      postFails = true
      await section.locator('.segmented label', { hasText: 'Observe' }).click()
      await section.getByRole('alert').getByText('Could not save the auto-merge setting.').waitFor()
      check(`${at} a failed save keeps the old mode`, await section.getByRole('radio', { name: 'Off' }).isChecked())
      postFails = false

      // Restore trust names its area for a screen reader and sends the area.
      const restore = section.getByRole('button', { name: 'Restore trust for Docs in project' })
      await restore.click()
      await restore.waitFor({ state: 'detached' })
      check(`${at} Restore sends the repository and the area, with a passkey approval`, JSON.stringify(posts.at(-1).restore) === JSON.stringify({ root: ROOT, area: 'docs' }) && approved(posts.at(-1)), JSON.stringify(posts.at(-1)))

      // After a restart the server holds no grant: Enforce is paused and the page says so.
      state = { ...state, mode: 'observe', paused: true }
      await open(width)
      await section.getByRole('status').getByText('Enforce is paused.').waitFor()
      check(`${at} paused Enforce shows Observe`, await section.getByRole('radio', { name: 'Observe' }).isChecked())
      await shot(`paused-${width}`)
      state = { ...state, paused: false }

      // Empty: no repository with rules, no decisions.
      mode = 'empty'
      await open(width)
      await section.getByText('No decisions yet. One appears here when a reviewer approves a commit of a worker branch.').waitFor()
      check(`${at} empty trust says what is missing`, (await section.locator('.hint').first().innerText()).includes('docs/auto-merge.json'))
      check(`${at} no page overflow (empty)`, !(await overflow()))
      await shot(`empty-${width}`)

      // Error: the load fails.
      mode = 'error'
      await open(width)
      await section.getByRole('alert').getByText('Tether could not read the auto-merge state.').waitFor()
      check(`${at} a failed load shows no controls`, await section.getByRole('radio').count() === 0)
      await shot(`error-${width}`)
    }
    check('no unmocked requests', !unexpected.size, [...unexpected].join(', '))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; ') + ' | unmocked: ' + [...unexpected].join(', '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`auto-merge ui: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
