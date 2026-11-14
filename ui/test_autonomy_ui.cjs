// Autonomy settings and the action deck against an existing build. Every API request stays in this page's fixtures; no terminal is touched.
// Run: cd ui && npm run test:ui (or node test_agent_map.cjs against an existing build).
const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const DIST = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const SHOTS = process.env.SHOTS || path.resolve(__dirname, '../.release-work/2026-11-12/guided-setup/shots')
const sizes = [[390, 844], [1280, 900]]
const now = Math.floor(Date.now() / 1000)
const ask = (pane, extra) => ({ pane, thread: null, since: now - 600, next: '', kind: 'reply', opened: false, snoozed_until: null, urgent: false, ...extra })
const paneA = { id: '%1', tmux_session: '$1', label: 'project:0.0', pane_name: 'Deploy fix', agent: 'claude', dir: '/srv/project', state: 'Idle', writable: true, thread: 't1', approval: null,
  ask: ask('%1', { since: now - 120, urgent: true, next: 'The release script needs the signing key. Which key should I use: staging or production?' }) }
const paneB = { id: '%2', tmux_session: '$1', label: 'project:0.1', pane_name: 'Docs pass', agent: 'codex', dir: '/srv/project', state: 'Idle', writable: true, thread: 't2', approval: null,
  ask: ask('%2', { since: now - 3600, kind: 'approval', next: 'Run npm install?' }) }
const levels = [['Check-in', 'Confirms the plan first.'], ['Guided', 'Confirms the plan, then works.'], ['Standard', 'Asks when a decision is material.'],
  ['Trusted', 'Uses sensible defaults.'], ['Autopilot', 'Does not ask about normal decisions.'], ['Full auto', 'Runs the task alone. Asks only when blocked; urgent asks reach the user at once.']]
  .map(([name, summary], level) => ({ level, name, summary }))
const auto = { agent: 'auto', model: 'auto', effort: 'auto' }
const advisorDefault = () => ({ main: { agent: 'claude', model: 'latest', effort: 'high' }, second: { agent: 'codex', model: 'latest', effort: 'high' } })
let autonomy = { levels, global: 2, level: 2, source: 'global', project: null, reviewers: { critical: auto, normal: auto, light: auto }, tiers: ['critical', 'normal', 'light'],
  models: { claude: [{ slug: 'opus', name: 'Opus' }, { slug: 'claude-fable-5-2', name: 'Fable 5.2' }], codex: [{ slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol', efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'] }, { slug: 'gpt-6-luna', name: 'GPT-6 Luna', efforts: ['low', 'medium'] }] },
  efforts: ['low', 'medium', 'high', 'xhigh', 'max'], advisor: advisorDefault(), latest: { claude: 'Fable 5.2', codex: 'GPT-6.1 Sol' },
  catalog: {
    claude: { latest: { launch: 'fable', slug: 'claude-fable-5-2', name: 'Fable 5.2' }, models: [{ slug: 'claude-fable-5-2', name: 'Fable 5.2', efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }, { slug: 'claude-mythos-5-2', name: 'Mythos 5.2', family: 'mythos', available: false, access: 'no-access', efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }, { slug: 'claude-opus-5-5', name: 'Opus 5.5', efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }], new_families: [], source: 'claude-code', changed: { from: 'Fable 5.1', to: 'Fable 5.2', at: now - 3600 } },
    codex: { latest: { launch: 'gpt-6.1-sol', slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol' }, models: [{ slug: 'gpt-6.1-sol', name: 'GPT-6.1 Sol', efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'] }, { slug: 'gpt-6-luna', name: 'GPT-6 Luna', efforts: ['low', 'medium'] }], new_families: [], source: 'codex', changed: null } },
  learning: { enabled: true, global: [{ id: 'aaaa1111', text: 'When you change UI, check it at 390 px before you report done.', kind: 'avoid', source: 'user', agent: 'codex', hits: 3, created: now - 86400 * 3, last_hit: now - 3600, stuck: true, status: 'confirmed' }],
    review: { due: true, reason: 'disputed', new: 1, disputed: 1, total: 3 } } }
const disputedLesson = { id: 'bbbb2222', text: 'When a test is slow, skip it in CI and report done.', kind: 'keep', source: 'user', agent: 'claude', hits: 1, created: now - 86400 * 5, last_hit: now - 86400 * 5,
  status: 'trial', quote: 'perfect, just skip that slow test', tab: 'Build fix', disputes: [{ pane: '%12', reason: 'The CI docs say skipped tests hide failures; the project rule asks for all tests.', at: now - 600 }], disputed: true, project: 'e89e1e21b318', project_name: 'Tether',
  flags: [{ type: 'conflict', with: 'cccc3333' }] }
const staleLesson = { id: 'cccc3333', text: 'When a test is slow, run it in CI and do not skip it.', kind: 'avoid', source: 'user', agent: 'codex', hits: 1, created: now - 86400 * 40, last_hit: now - 86400 * 40,
  status: 'trial', quote: 'never skip tests', tab: 'Tests', disputes: [], disputed: false, project: 'e89e1e21b318', project_name: 'Tether', flags: [{ type: 'conflict', with: 'bbbb2222' }] }
let reviewFail = false
// The advisor check: 'off' answers like a server with the advisor turned off.
let checkMode = 'ok'
const checkDone = { state: 'done', started: now - 300, finished: now - 60, label: 'Claude, Fable 5.2 (latest), high effort', count: 3, skipped: 0, error: '',
  advice: {
    bbbb2222: { verdict: 'remove', confidence: 'high', reason: 'The project notes ask for every test in CI, and a later correction says never skip tests.', text: '' },
    cccc3333: { verdict: 'edit', confidence: 'medium', reason: 'It holds, but the notes allow a separate slow-test job.', text: 'When a test is slow, move it to the slow CI job; never skip it.' },
  } }
let reviewState = { due: true, reason: 'disputed', new: 1, disputed: 1, total: 3, last_review: now - 86400 * 20, snoozed_until: 0,
  groups: [{ key: 'disputed', lessons: [disputedLesson] }, { key: 'stale', lessons: [staleLesson] }, { key: 'other', lessons: [{ ...autonomy.learning.global[0], project: null, project_name: null, flags: [] }] }],
  check: { state: 'idle' } }
const fixture = {
  sessions: { sessions: [paneA, paneB], default_thread: 't1' },
  chat: { messages: [], busy: false, thread: 't1', agent: 'claude', writable: true, session: '%1', earlier: 0, limit: 100, state: 'Idle', approval: null },
  footer: { observed: true, model: 'Opus 5.5', mode: 'default', notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  prefs: { prefs: {}, version: 1 }, 'projects/marks': {}, 'projects/backlog': { text: '' }, 'side-shells': { shells: [] },
  update: { current: '0.0.0', latest: '0.0.0', available: false }, host: { name: 'Test server', office: false },
  catalog: { commands: [], plugins: [], mcp: [], note: '' }, stream: {}, 'codex-runs': { pane: '%1', running: 0, runs: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] }, usage: { claude: null, codex: null },
}

const setupFresh = () => ({
  hook: { state: 'missing', path: '/srv/u/.claude/settings.json', entry: { type: 'command', command: '/usr/bin/python3 /srv/tether/server/autonomy.py hook', timeout: 5 } },
  ping: { state: 'off', preset: null, fields: {}, base_url: '', presets: [{ id: 'briskbot', name: 'BriskBot (Microsoft Teams)', available: true }] },
})
let setup = setupFresh()
// Passkeys: a fake navigator.credentials stands in for the authenticator (Tether's CDP proxy refuses the WebAuthn domain).
// It records the options it gets. server/test_approval.py checks the real signatures.
let keys = []
const b64 = bytes => Buffer.from(bytes).toString('base64url')
const approved = body => !!body.approval?.challenge_id && !!body.approval?.credential?.response?.signature
const needsKey = () => ({ status: 403, json: keys.length ? { error: 'Approve with your passkey.', approval: 'required' } : { error: 'Add a passkey in Settings › Setup first.', approval: 'no-passkey' } })

async function main() {
  let browser, page, server, failed = 0
  const posts = [], pageErrors = [], unexpected = new Set()
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
    const origin = `http://localhost:${server.address().port}`  // a secure context with a valid WebAuthn rp id
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
      if (url.pathname.endsWith('/sw.js')) return route.fulfill({ status: 404, body: '' })
      if (url.pathname.includes('/api/')) {
        const endpoint = url.pathname.split('/api/')[1]
        if (request.method() !== 'GET') {
          const body = request.postDataJSON(); posts.push({ endpoint, body })
          if (endpoint === 'passkeys') {
            if (body.op === 'options') return route.fulfill({ json: { challenge_id: `c${posts.length}`, publicKey: body.kind === 'create'
              ? { challenge: b64(Buffer.alloc(32, 1)), rp: { id: 'localhost', name: 'Tether' }, user: { id: b64(Buffer.alloc(16, 2)), name: 'tether', displayName: 'Tether' },
                  pubKeyCredParams: [{ type: 'public-key', alg: -7 }], authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' }, attestation: 'none', timeout: 60000,
                  excludeCredentials: keys.map(key => ({ type: 'public-key', id: key.id })) }
              : { challenge: b64(Buffer.alloc(32, 3)), rpId: 'localhost', userVerification: 'required', timeout: 60000, allowCredentials: keys.map(key => ({ type: 'public-key', id: key.id })) } } })
            if (body.op === 'enroll') {
              if (keys.length && !approved(body)) return route.fulfill(needsKey())
              keys = [...keys, { id: body.credential.id, name: body.name, created: new Date().toISOString(), last_used: null }]
            }
            if (body.op === 'remove') { if (!approved(body)) return route.fulfill(needsKey()); keys = keys.filter(key => key.id !== body.id) }
            return route.fulfill({ json: { passkeys: keys, here: keys.length } })
          }
          const gated = (endpoint === 'autonomy/setup' && ['hook', 'ping'].includes(body.action)) || (endpoint === 'autonomy/decisions' && !body.delete)
            || (endpoint === 'autonomy' && body.level !== undefined && !body.project && body.level > autonomy.global)
          if (gated && !approved(body)) return route.fulfill(needsKey())
          if (endpoint === 'autonomy') { autonomy = { ...autonomy, ...(body.level !== undefined && !body.project ? { global: body.level, level: body.level } : {}), ...(body.reviewers ? { reviewers: body.reviewers } : {}), ...(body.advisor ? { advisor: body.advisor } : {}) }; return route.fulfill({ json: autonomy }) }
          if (endpoint === 'autonomy/setup') {
            if (body.action === 'hook') setup = { ...setup, hook: { ...setup.hook, state: 'ok' } }
            if (body.action === 'ping') setup = { ...setup, ping: { ...setup.ping, state: 'on', preset: body.preset, fields: body.fields, base_url: body.base_url } }
            return route.fulfill({ json: body.action === 'ping-test' ? { ...setup, test: { ok: true, code: 0, detail: '' } } : setup })
          }
          if (endpoint === 'autonomy/review' && body.action === 'check') {
            if (checkMode === 'off') return route.fulfill({ status: 400, json: { error: 'Turn on the advisor in Settings › Agents first.' } })
            if (checkMode === 'busy') {
              reviewState = { ...reviewState, check: { ...reviewState.check, state: 'running', started: now + 5, finished: null, count: 3 } }
              return route.fulfill({ status: 409, json: { error: 'The advisor is already checking the lessons.' } })
            }
            reviewState = { ...reviewState, check: { ...reviewState.check, state: 'running', started: now, finished: null, count: 3, error: '' } }
            return route.fulfill({ json: reviewState })
          }
          if (endpoint === 'autonomy/review') { reviewState = { ...reviewState, due: false }; return route.fulfill({ json: reviewState }) }
          if (endpoint === 'autonomy/lessons') {
            const kept = body.keep || body.keep_global
            if (kept) reviewState = { ...reviewState, groups: reviewState.groups.map(group => ({ ...group, lessons: group.lessons.map(item => item.id === kept ? { ...item, status: 'confirmed', disputes: [], disputed: false } : item) })) }
            const learning = { ...autonomy.learning, ...(typeof body.enabled === 'boolean' ? { enabled: body.enabled } : {}), global: autonomy.learning.global.filter(item => item.id !== body.remove_global) }
            autonomy = { ...autonomy, learning }; return route.fulfill({ json: autonomy })
          }
          if (endpoint === 'autonomy/decisions') return route.fulfill({ json: { entry: { id: 'abcd1234', date: '2026-11-12', question: body.question, answer: body.answer }, project: 'e89e1e21b318' } })
          if (endpoint.startsWith('attention/')) return route.fulfill({ json: { ask: null, waiting: 1 } })
          return route.fulfill({ json: { ok: true } })
        }
        if (endpoint === 'autonomy/setup') return route.fulfill({ json: setup })
        if (endpoint === 'passkeys') return route.fulfill({ json: { passkeys: keys, here: keys.length } })
        if (endpoint === 'autonomy/review') return reviewFail ? route.fulfill({ status: 500, json: { error: 'Tether could not read the lessons.' } }) : route.fulfill({ json: reviewState })
        if (endpoint.startsWith('autonomy')) return route.fulfill({ json: autonomy })
        if (endpoint.startsWith('codex-level')) return route.fulfill({ json: { level: 0, global: 0, levels: [], project: null, models: [] } })
        if (endpoint.startsWith('chat')) return route.fulfill({ json: fixture.chat })
        if (!(endpoint in fixture)) unexpected.add(endpoint)
        if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
        return route.fulfill({ json: fixture[endpoint] || {} })
      }
      if (url.origin === origin && url.pathname.startsWith('/chat/')) return route.continue()
      unexpected.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    })
    await page.addInitScript(() => {
      if ('serviceWorker' in navigator) navigator.serviceWorker.register = () => Promise.reject(new Error('off'))
      localStorage.clear()
      const calls = window.__webauthn = []
      const buffer = text => new TextEncoder().encode(text).buffer
      const credential = (kind, response) => ({ id: 'cred-1', rawId: buffer('cred-1'), type: 'public-key', response })
      const record = (kind, publicKey) => calls.push({ kind, uv: publicKey.userVerification || publicKey.authenticatorSelection?.userVerification,
        challenge: publicKey.challenge instanceof Uint8Array ? publicKey.challenge.length : -1, ids: (publicKey.allowCredentials || publicKey.excludeCredentials || []).map(item => item.id instanceof Uint8Array) })
      Object.defineProperty(navigator, 'credentials', { configurable: true, value: {
        create: async ({ publicKey }) => { record('create', publicKey); return credential('create', { clientDataJSON: buffer('{"type":"webauthn.create"}'), attestationObject: buffer('att') }) },
        get: async ({ publicKey }) => { record('get', publicKey); return credential('get', { clientDataJSON: buffer('{"type":"webauthn.get"}'), authenticatorData: buffer('auth'), signature: buffer('sig'), userHandle: null }) },
      } })
      localStorage.setItem('tmux-chat.prefs.synced', '1')
      localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ theme: 'light', voiceOff: true, voiceSound: false }))
    })
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    for (const [width, height] of sizes) {
      const at = `${width}px`
      autonomy = { ...autonomy, global: 2, level: 2, reviewers: { critical: auto, normal: auto, light: auto }, advisor: advisorDefault() }
      setup = setupFresh()
      keys = []
      const first = posts.length
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/chat/`, { waitUntil: 'load' })
      // The urgent ask opens the deck by itself.
      const deck = page.locator('dialog.action-deck')
      await deck.waitFor()
      check(`${at} deck opens for urgent ask`, true)
      check(`${at} urgent card first`, (await deck.locator('.deck-tab').innerText()).trim() === 'Deploy fix' && await deck.getByText('Urgent', { exact: true }).isVisible())
      check(`${at} count`, (await deck.locator('.deck-count').innerText()).trim() === '1 of 2')
      await page.waitForTimeout(100)
      check(`${at} reply box focused`, await deck.locator('textarea').evaluate(node => node === document.activeElement))
      check(`${at} no page overflow (deck)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `deck-urgent-${width}.png`) })
      check(`${at} remember is off by default`, !(await deck.getByLabel('Remember for this project').isChecked()))
      await deck.locator('textarea').fill('Use staging.')
      await deck.getByLabel('Remember for this project').check()
      await deck.locator('textarea').focus()
      const before = posts.length
      await page.keyboard.press('Control+Enter')
      await deck.getByText('Run npm install?').waitFor()
      const saved = posts.slice(before).filter(post => post.endpoint === 'autonomy/decisions')
      check(`${at} remember asks to save the answer for the pane`, saved.length === 1 && saved[0].body.pane === '%1' && saved[0].body.answer === 'Use staging.' && !!saved[0].body.question, JSON.stringify(saved))
      check(`${at} no passkey: not saved, says what to do`, await deck.getByText('Tether did not save the answer: Add a passkey', { exact: false }).isVisible() && !posts.slice(before).some(post => post.endpoint === 'passkeys'))
      const sent = posts.slice(before).find(post => post.endpoint === 'message')
      check(`${at} Ctrl+Enter sends to the tab`, sent && sent.body.session === '%1' && sent.body.message === 'Use staging.', JSON.stringify(posts.slice(before)))
      check(`${at} next card: approval hint`, await deck.getByText('This prompt waits in the tab.').isVisible() && await deck.locator('textarea').count() === 0)
      await page.screenshot({ path: path.join(SHOTS, `deck-approval-${width}.png`) })
      await deck.getByRole('button', { name: 'Handled' }).click()
      await deck.getByText('Docs pass: handled.').waitFor()
      check(`${at} Handled posts`, posts.some(post => post.endpoint === 'attention/handled' && post.body.session === '%2'))
      await deck.getByRole('button', { name: 'Done' }).click()
      await deck.waitFor({ state: 'hidden' })
      check(`${at} Done closes`, true)
      // Settings: autonomy level and reviewers.
      if (width < 800) await page.getByRole('button', { name: /menu|sessions|open the list/i }).first().click().catch(() => {})
      await page.getByRole('button', { name: 'Settings' }).first().click()
      const search = page.getByPlaceholder(/Search settings/)
      // Setup: add a passkey, then each step shows its change and applies it only after a passkey approval.
      if (await search.count()) await search.fill('setup')
      const review = page.getByRole('button', { name: 'Review change' })
      await review.waitFor()
      check(`${at} hook not applied before approval`, !posts.slice(first).some(post => post.endpoint === 'autonomy/setup'))
      await review.click()
      check(`${at} approve disabled without a passkey`, await page.getByRole('button', { name: 'Approve and apply' }).isDisabled() && await page.getByText('Add a passkey above first.').first().isVisible())
      await page.screenshot({ path: path.join(SHOTS, `settings-setup-nokey-${width}.png`) })
      await page.getByRole('button', { name: 'Add passkey' }).click()
      await page.getByText('Passkey added.', { exact: false }).waitFor()
      check(`${at} passkey enrolled`, keys.length === 1 && keys[0].id === 'cred-1', JSON.stringify(keys))
      check(`${at} passkey count shows`, await page.getByText('1 passkey approves changes').isVisible())
      check(`${at} hook preview shows the command`, (await page.locator('.setup-review pre').innerText()).includes('autonomy.py hook'))
      check(`${at} no page overflow (setup review)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-setup-review-${width}.png`) })
      await page.getByRole('button', { name: 'Approve and apply' }).click()
      await page.getByText('Ready. Each Claude prompt gets').waitFor()
      const hookPosts = posts.slice(first).filter(post => post.endpoint === 'autonomy/setup' && post.body.action === 'hook')
      check(`${at} hook applies only with a passkey approval`, hookPosts.length === 2 && !hookPosts[0].body.approval && approved(hookPosts[1].body), JSON.stringify(hookPosts.map(post => !!post.body.approval)))
      await page.getByRole('button', { name: 'Set up' }).click()
      const approvePing = page.getByRole('button', { name: 'Approve and turn on' })
      check(`${at} ping approve disabled without mention`, await approvePing.isDisabled())
      await page.locator('#ping-mention').fill('sam')
      check(`${at} ping preview names the mention`, await page.getByText('@sam', { exact: false }).first().isVisible())
      check(`${at} no page overflow (ping form)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-setup-ping-${width}.png`) })
      await approvePing.click()
      await page.getByRole('button', { name: 'Send test' }).waitFor()
      const ping = posts.slice(first).find(post => post.endpoint === 'autonomy/setup' && post.body.action === 'ping' && post.body.approval)
      check(`${at} ping posts fields and origin`, ping && ping.body.fields.mention === 'sam' && ping.body.fields.space === 'team' && ping.body.base_url === origin, JSON.stringify(ping))
      await page.getByRole('button', { name: 'Send test' }).click()
      await page.getByText('Test sent. Check Teams.').waitFor()
      check(`${at} send test`, true)
      await page.screenshot({ path: path.join(SHOTS, `settings-setup-on-${width}.png`) })
      if (await search.count()) await search.fill('autonomy')
      const level = page.getByRole('radio', { name: /Full auto/ }).first()
      await level.waitFor()
      check(`${at} six named levels`, await page.locator('fieldset.autonomy-levels').first().locator('.autonomy-level').count() === 6)
      check(`${at} level 2 checked`, await page.getByRole('radio', { name: /Standard/ }).first().isChecked())
      check(`${at} no page overflow (autonomy)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-autonomy-${width}.png`), fullPage: false })
      await level.check({ force: true })
      for (let tries = 0; tries < 50 && autonomy.global !== 5; tries++) await page.waitForTimeout(100)
      check(`${at} level raise carries a passkey approval`, posts.slice(first).some(post => post.endpoint === 'autonomy' && post.body.level === 5 && approved(post.body)) && autonomy.global === 5, JSON.stringify(posts.slice(first).filter(post => post.endpoint === 'autonomy' || post.endpoint === 'passkeys').map(post => [post.endpoint, post.body.op || post.body.level, !!post.body.approval])))
      // Advisor and reviewers: one list of rows; each row opens one sheet with agent, model and effort.
      const advisorPosts = () => posts.filter(post => post.endpoint === 'autonomy' && post.body.advisor).map(post => post.body.advisor)
      const reviewerPosts = () => posts.filter(post => post.endpoint === 'autonomy' && post.body.reviewers).map(post => post.body.reviewers)
      const settle = async (list, mark) => { for (let tries = 0; tries < 30 && list().length === mark; tries++) await page.waitForTimeout(100); return list().at(-1) }
      const row = name => page.locator('.helper-row', { hasText: name })
      const sheet = page.locator('dialog.helper-sheet')
      if (await search.count()) await search.fill('reviewers')
      await row('Critical tasks').waitFor()
      if (await search.count()) await search.fill('advisor')
      const advisorRow = row('Advisor').first()
      await advisorRow.waitFor()
      await advisorRow.scrollIntoViewIfNeeded()
      check(`${at} five helper rows in two groups`, await page.locator('.helper-row').count() === 5 && await page.getByRole('heading', { name: 'Before they ask you' }).isVisible() && await page.getByRole('heading', { name: 'After they finish' }).isVisible())
      check(`${at} advisor row names the concrete model`, (await advisorRow.innerText()).includes('Claude · Latest (Fable 5.2) · high effort'), await advisorRow.innerText())
      check(`${at} second opinion row: Codex latest`, (await row('Second opinion').innerText()).includes('Codex · Latest (GPT-6.1 Sol)'))
      check(`${at} access notice names Mythos`, await page.getByText('Mythos 5.2: your account has no access.', { exact: false }).isVisible())
      const rechecks = () => posts.filter(post => post.endpoint === 'autonomy' && post.body.catalog)
      let mark0 = posts.length
      const recheckMark = rechecks().length
      await page.getByRole('button', { name: 'Check access again' }).click()
      await settle(rechecks, recheckMark)
      check(`${at} check access posts recheck`, posts.slice(mark0).some(post => post.body.catalog === 'recheck'))
      check(`${at} release notice shows`, await page.getByText('Fable 5.2 replaced Fable 5.1').isVisible())
      check(`${at} no page overflow (helpers)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-helpers-${width}.png`) })
      await advisorRow.click()
      await sheet.waitFor()
      check(`${at} sheet: latest names model and is checked`, await sheet.getByRole('radio', { name: /Latest: Fable 5.2/ }).isChecked())
      check(`${at} sheet: pinned versions, no bare aliases`, await sheet.getByRole('radio', { name: /^Opus 5.5/ }).count() === 1 && await sheet.getByRole('radio', { name: /^Opus Stays/ }).count() === 0)
      check(`${at} sheet: model without access is disabled and says why`, await sheet.getByRole('radio', { name: /^Mythos 5.2/ }).isDisabled() && await sheet.getByText('Your account has no access.', { exact: false }).isVisible())
      check(`${at} no page overflow (advisor sheet)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-advisor-sheet-${width}.png`) })
      let mark = advisorPosts().length
      await sheet.getByRole('radio', { name: 'Codex', exact: true }).check({ force: true })
      let last = await settle(advisorPosts, mark)
      check(`${at} codex advisor uses latest and keeps effort`, last && last.main.agent === 'codex' && last.main.model === 'latest' && last.main.effort === 'high' && last.second.agent === 'codex', JSON.stringify(last))
      await sheet.getByRole('radio', { name: /Latest: GPT-6.1 Sol/ }).waitFor()
      check(`${at} codex efforts include ultra`, await sheet.getByRole('radio', { name: 'Ultra', exact: true }).count() === 1)
      mark = advisorPosts().length
      await sheet.getByRole('radio', { name: 'Ultra', exact: true }).check({ force: true })
      last = await settle(advisorPosts, mark)
      mark = advisorPosts().length
      await sheet.getByText('GPT-6 Luna', { exact: true }).click()
      last = await settle(advisorPosts, mark)
      check(`${at} effort the model lacks resets to auto`, last && last.main.model === 'gpt-6-luna' && last.main.effort === 'auto', JSON.stringify(last))
      // Keyboard: arrow keys move between agent radios.
      await sheet.getByRole('radio', { name: 'Codex', exact: true }).focus()
      mark = advisorPosts().length
      await page.keyboard.press('ArrowRight')
      last = await settle(advisorPosts, mark)
      check(`${at} keyboard: arrow picks Off`, last && last.main.agent === 'none' && last.second.agent === 'none', JSON.stringify(last))
      check(`${at} off hides model and effort`, await sheet.getByRole('group', { name: 'Model' }).count() === 0 && await sheet.getByRole('group', { name: 'Effort' }).count() === 0)
      await sheet.getByRole('button', { name: 'Done' }).click()
      await sheet.waitFor({ state: 'hidden' })
      check(`${at} focus returns to the row`, await advisorRow.evaluate(node => node === document.activeElement))
      check(`${at} off: rows say agents ask you`, (await advisorRow.innerText()).includes('Off. Agents ask you') && await row('Second opinion').isDisabled())
      mark = advisorPosts().length
      await advisorRow.click()
      await sheet.getByRole('radio', { name: 'Claude', exact: true }).check({ force: true })
      last = await settle(advisorPosts, mark)
      check(`${at} advisor back on restores second opinion`, last && last.main.agent === 'claude' && last.second.agent === 'codex' && last.second.model === 'latest', JSON.stringify(last))
      await page.keyboard.press('Escape')
      await sheet.waitFor({ state: 'hidden' })
      check(`${at} advisor changes need no passkey`, !posts.some(post => post.endpoint === 'autonomy' && post.body.advisor && post.body.approval))
      if (await search.count()) await search.fill('reviewers')
      await row('Critical tasks').click()
      await sheet.waitFor()
      check(`${at} reviewer sheet starts on Auto`, await sheet.getByRole('radio', { name: 'Auto', exact: true }).first().isChecked())
      mark = reviewerPosts().length
      await sheet.getByRole('radio', { name: 'Codex', exact: true }).check({ force: true })
      last = await settle(reviewerPosts, mark)
      check(`${at} reviewer codex uses latest, high`, last && last.critical.agent === 'codex' && last.critical.model === 'latest' && last.critical.effort === 'high', JSON.stringify(last))
      check(`${at} reviewer offers latest model`, await sheet.getByRole('radio', { name: /Latest: GPT-6.1 Sol/ }).isChecked())
      await page.screenshot({ path: path.join(SHOTS, `settings-reviewer-sheet-${width}.png`) })
      await sheet.getByRole('button', { name: 'Done' }).click()
      await sheet.waitFor({ state: 'hidden' })
      if (await search.count()) await search.fill('learning')
      const learnSwitch = page.getByRole('switch', { name: /Learn from your feedback/ })
      await learnSwitch.waitFor()
      await learnSwitch.scrollIntoViewIfNeeded()
      check(`${at} learning is on by default`, await learnSwitch.isChecked())
      check(`${at} global lesson shows kind and did-not-stick flag`, await page.locator('.lesson').first().getByText('Avoid', { exact: true }).isVisible() && await page.getByText('Did not stick').first().isVisible())
      check(`${at} no page overflow (learning)`, !(await overflow()))
      await page.screenshot({ path: path.join(SHOTS, `settings-learning-${width}.png`) })
      mark0 = posts.length
      await learnSwitch.click()
      await page.waitForTimeout(300)
      check(`${at} switch posts enabled false`, posts.slice(mark0).some(post => post.endpoint === 'autonomy/lessons' && post.body.enabled === false))
      await learnSwitch.click()
      await page.waitForTimeout(300)
      reviewState = { ...reviewState, due: true, check: { state: 'idle' } }
      await page.getByRole('button', { name: 'Review', exact: true }).click()
      const reviewSheet = page.getByRole('dialog', { name: 'Review lessons' })
      await reviewSheet.waitFor()
      await reviewSheet.getByText('Questioned by an agent').waitFor()
      check(`${at} review shows the reason`, await reviewSheet.getByText('An agent questioned a lesson.').isVisible())
      check(`${at} review groups: disputed, stale, other`, await reviewSheet.getByRole('heading', { name: /Seen once, not repeated in 30 days/ }).isVisible() && await reviewSheet.getByRole('heading', { name: /Other lessons/ }).isVisible())
      check(`${at} review shows the agent reason and your words`, await reviewSheet.getByText(/skipped tests hide failures/).isVisible() && await reviewSheet.getByText('You said: “perfect, just skip that slow test”').isVisible())
      check(`${at} review flags a possible conflict`, await reviewSheet.getByText(/^May conflict with/).first().isVisible())
      check(`${at} review offers snooze when due`, await reviewSheet.getByRole('button', { name: 'Remind me in 3 days' }).isVisible())
      check(`${at} no overflow in the review`, await reviewSheet.evaluate(sheet => [...sheet.querySelectorAll('*')].every(node => node.scrollWidth <= node.clientWidth + 1 || getComputedStyle(node).overflowX !== 'visible' || node.getBoundingClientRect().right <= sheet.getBoundingClientRect().right + 1)))
      await page.screenshot({ path: path.join(SHOTS, `lessons-review-${width}.png`) })
      // The advisor check: off shows the reason with a retry; on, it runs, then each lesson shows a suggestion.
      checkMode = 'off'
      mark0 = posts.length
      await reviewSheet.getByRole('button', { name: 'Ask the advisor to check these' }).click()
      await reviewSheet.getByText('Turn on the advisor in Settings › Agents first.').waitFor()
      check(`${at} check posts action check`, posts.slice(mark0).some(post => post.endpoint === 'autonomy/review' && post.body.action === 'check'))
      checkMode = 'ok'
      await reviewSheet.getByRole('button', { name: 'Try again' }).click()
      await reviewSheet.getByText(/The advisor is checking 3 lessons/).waitFor()
      check(`${at} running check hides the button and the error`, !(await reviewSheet.getByRole('button', { name: /Ask the advisor|Try again|Check again/ }).count()) && !(await reviewSheet.getByText('Turn on the advisor').count()))
      await page.screenshot({ path: path.join(SHOTS, `lessons-check-running-${width}.png`) })
      reviewState = { ...reviewState, check: checkDone }
      await reviewSheet.getByText(/Advisor checked 3 lessons/).waitFor({ timeout: 8000 })
      check(`${at} done check names the advisor`, await reviewSheet.getByText(/Claude, Fable 5.2 \(latest\), high effort/).isVisible())
      check(`${at} advice shows verdict, confidence and reason`, await reviewSheet.getByText('Advisor suggests: Remove').isVisible() && await reviewSheet.getByText(/high confidence\. The project notes ask/).isVisible())
      check(`${at} nothing applied by the check`, !posts.slice(mark0).some(post => post.endpoint === 'autonomy/lessons'))
      check(`${at} no overflow with advice`, await reviewSheet.evaluate(sheet => [...sheet.querySelectorAll('*')].every(node => node.scrollWidth <= node.clientWidth + 1 || getComputedStyle(node).overflowX !== 'visible' || node.getBoundingClientRect().right <= sheet.getBoundingClientRect().right + 1)))
      await reviewSheet.getByText('Advisor suggests: Fix').scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(SHOTS, `lessons-check-done-${width}.png`) })
      await reviewSheet.getByRole('button', { name: 'Use this wording' }).click()
      check(`${at} use this wording fills the edit form`, (await reviewSheet.getByLabel('Lesson text').inputValue()) === checkDone.advice.cccc3333.text)
      check(`${at} the edit is not saved by itself`, !posts.slice(mark0).some(post => post.endpoint === 'autonomy/lessons'))
      await reviewSheet.getByRole('button', { name: 'Cancel' }).click()
      check(`${at} done check offers Check again`, await reviewSheet.getByRole('button', { name: 'Check again' }).isVisible())
      // Another device starts a check first: this one gets 409, then the error goes once the check runs and ends.
      checkMode = 'busy'
      await reviewSheet.getByRole('button', { name: 'Check again' }).click()
      await reviewSheet.getByText(/The advisor is checking 3 lessons/).waitFor()
      check(`${at} a busy start shows the running check, not the error`, !(await reviewSheet.getByText('The advisor is already checking the lessons.').count()))
      reviewState = { ...reviewState, check: { ...checkDone, started: now + 5 } }
      await reviewSheet.getByText(/Advisor checked 3 lessons/).waitFor({ timeout: 8000 })
      check(`${at} the busy error does not come back after the check`, !(await reviewSheet.getByText('The advisor is already checking the lessons.').count()))
      checkMode = 'ok'
      mark0 = posts.length
      await reviewSheet.getByRole('button', { name: 'Keep' }).first().click()
      await reviewSheet.getByText('Kept').waitFor()
      check(`${at} keep posts project and id`, posts.slice(mark0).some(post => post.endpoint === 'autonomy/lessons' && post.body.keep === 'bbbb2222' && post.body.project === 'e89e1e21b318'))
      // An agent questions the kept lesson again and the next load fails: the error stays with a retry, and the lesson
      // offers Keep again instead of the stale "Kept".
      reviewState = { ...reviewState, groups: reviewState.groups.map(group => ({ ...group, lessons: group.lessons.map(item => item.id === 'bbbb2222' ? disputedLesson : item) })) }
      reviewFail = true
      await reviewSheet.getByRole('button', { name: `Remove the lesson: ${staleLesson.text}` }).click()
      await reviewSheet.getByRole('alert').waitFor()
      check(`${at} review keeps a failed reload visible`, (await reviewSheet.getByRole('alert').innerText()).includes('could not read the lessons'))
      reviewFail = false
      await reviewSheet.getByRole('button', { name: 'Try again' }).click()
      await reviewSheet.getByRole('alert').waitFor({ state: 'detached' })
      check(`${at} a questioned lesson offers Keep again`, await reviewSheet.getByRole('button', { name: 'Keep' }).first().isVisible() && !(await reviewSheet.getByText('Kept').isVisible()))
      await reviewSheet.getByRole('button', { name: 'Finish review' }).click()
      await reviewSheet.waitFor({ state: 'hidden' })
      check(`${at} finish posts done`, posts.slice(mark0).some(post => post.endpoint === 'autonomy/review' && post.body.action === 'done'))
      const calls = await page.evaluate(() => window.__webauthn)
      check(`${at} every passkey call asks for user verification with byte challenges`, calls.length >= 4 && calls.every(call => call.uv === 'required' && call.challenge === 32 && call.ids.every(Boolean)), JSON.stringify(calls))
      await page.keyboard.press('Escape').catch(() => {})
    }
    // The digest link (?go=attention) opens the lesson review when no tab waits.
    const sessions = fixture.sessions
    fixture.sessions = { sessions: [], default_thread: 't1' }
    reviewState = { ...reviewState, due: true }
    await page.goto(`${origin}/chat/?go=attention`, { waitUntil: 'load' })
    const fromDigest = page.getByRole('dialog', { name: 'Review lessons' })
    await fromDigest.waitFor({ timeout: 5000 }).catch(() => {})
    check('digest link opens the lesson review', await fromDigest.isVisible())
    fixture.sessions = sessions
    check('no unmocked requests', !unexpected.size, [...unexpected].join(', '))
    check('no page errors', !pageErrors.length, pageErrors.join('; '))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(SHOTS, 'error.png') }).catch(() => {})
    check('runner', false, error.message.split('\n')[0] + ' | ' + pageErrors.join('; '))
  } finally {
    if (page) await page.close().catch(() => {})
    if (browser) await browser.close().catch(() => {})
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
    console.log(`autonomy ui: ${failed ? 'FAIL' : 'PASS'}`)
    if (failed) process.exitCode = 1
  }
}

main()
