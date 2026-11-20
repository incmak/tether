// Run from ui/: DIST=<build> node test_task_tree_ui.cjs. Every request goes through a fixture.
const { strict: assert } = require('node:assert')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const taskName = 'B446 Task tree'
const reviewName = 'B446 review'
const workerName = 'B446 server'
const separateName = 'B447 Settings'
const doneName = 'B300 Cleanup'
const settingsLabel = 'Close done tasks after 24 hours untouched'
const replyDigest = 'a'.repeat(64)

function fixture(id = '%1', thread = 'root-thread') {
  const make = (id, name, fields) => ({
    id, pane_name: name, agent: 'claude', dir: '/fixture/tether', tmux_session: '$1', state: 'Idle', writable: true,
    label: `Tether:0.${id.slice(1)}`, group: 'tree', thread: `thread-${id}`, approval: null, ...fields,
  })
  const child = { task: id, task_depth: 1 }
  return {
    sessions: [
      make(id, taskName, { thread, state: 'Working' }),
      make('%81', separateName, { helper: true, owner: id, from: { pane: id, name: 'B446', live: true } }),
      make('%82', workerName, { ...child, group: 'bugs', task_role: 'Worker', state: 'Working' }),
      make('%83', 'B446 advisor', { ...child, task_role: 'Advisor' }),
      make('%84', 'B446 second advisor', { ...child, task_role: 'Second advisor' }),
      make('%85', 'B446 server review', { ...child, task_depth: 2, task_role: "Worker's review" }),
      make('%86', reviewName, { ...child, task_role: 'Review', state: 'Approval', since: 200,
        approval: { id: 'approval-digest', text: 'Apply the task tree changes?', actionable: true, options: [{ id: 1, text: 'Yes, apply' }, { id: 2, text: 'No' }] } }),
      make('%87', doneName, { done: true, done_mark: true }),
      make('%88', 'B401 Sidebar fix', { group: 'bugs' }),
    ],
    default_thread: thread,
    groups: { $1: [{ id: 'tree', name: 'Task tree', project: '' }, { id: 'bugs', name: 'Tether bugs', project: '' }] },
  }
}

function plan(listing) {
  const [root] = listing.sessions
  const children = listing.sessions.filter(pane => pane.task === root.id).map(pane => ({
    pane: pane.id, thread: pane.thread, name: pane.pane_name, role: pane.task_role,
    state: pane.state === 'Approval' ? 'needs' : pane.state === 'Working' ? 'working' : 'idle',
  }))
  return { task: { pane: root.id, name: root.pane_name }, idle: children.filter(child => child.state === 'idle'), working: children.filter(child => child.state !== 'idle') }
}

const apiFixture = {
  footer: { observed: true, model: 'Opus', mode: 'default', notices: [] },
  host: { name: 'Test server', office: false },
  update: { current: '0.0.0', latest: '0.0.0', available: false },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  'browser/agents': { enabled: false, available: false, reason: 'off', agents: [], asks: [] },
  'side-shells': { shells: [] },
  'codex-runs': { pane: '%1', running: 0, runs: [] },
  'projects/backlog': { text: '' },
  'work/board': { projects: [], items: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  usage: { claude: null, codex: null },
}
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' }
async function main() {
  const browser = await getBrowser({ local: true })
  const context = browser.contexts()[0]
  const page = await context.newPage()
  page.setDefaultTimeout(8000)
  const listing = fixture()
  const writes = []
  const errors = []
  let setting = false
  let staleAnswer = true
  const dist = path.resolve(process.env.DIST || 'dist')
  const origin = 'http://127.0.0.1:9876'
  page.on('pageerror', error => errors.push(error.stack))
  await page.routeWebSocket('**/*', socket => socket.close())
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (url.origin !== origin) return route.abort('blockedbyclient')
    if (url.pathname.startsWith('/mobile/api/')) {
      const endpoint = url.pathname.slice('/mobile/api/'.length)
      const pane = listing.sessions.find(item => item.id === url.searchParams.get('session')) ?? listing.sessions[0]
      if (req.method() !== 'GET') {
        const body = req.postDataJSON()
        writes.push({ endpoint, body })
        if (endpoint === 'task-tree/answer' && staleAnswer) return route.fulfill({ status: 409, json: { error: 'The helper’s question changed.' } })
        if (endpoint === 'prefs' && typeof body.changes.task_auto_close === 'boolean') setting = body.changes.task_auto_close
        if (endpoint !== 'prefs') return route.fulfill({ json: { ok: true, accepted: true } })
      }
      if (endpoint === 'stream') return route.fulfill({ status: 204, body: '' })
      if (endpoint === 'sessions') return route.fulfill({ json: listing })
      if (endpoint === 'prefs') return route.fulfill({ json: { prefs: { task_auto_close: setting, uiUsage: false }, version: setting ? 2 : 1 } })
      if (endpoint === 'task-tree/close-plan') return route.fulfill({ json: plan(listing) })
      if (endpoint === 'chat') return route.fulfill({ json: { messages: [], busy: false, thread: pane.thread,
        agent: pane.agent, writable: true, session: pane.id, earlier: 0, limit: 100, state: pane.state, approval: null } })
      return route.fulfill({ json: apiFixture[endpoint] ?? {} })
    }
    const asset = url.pathname === '/chat/' ? 'index.html' : url.pathname.slice('/chat/'.length)
    if (!url.pathname.startsWith('/chat/') || asset === 'sw.js' || path.resolve(dist, asset) !== path.join(dist, asset)) return route.abort('blockedbyclient')
    try { return route.fulfill({ body: await fs.readFile(path.join(dist, asset)), contentType: mime[path.extname(asset)] }) }
    catch { return route.fulfill({ status: 404, body: '' }) }
  })
  await page.addInitScript(() => {
    localStorage.setItem('tmux-chat.prefs.synced', '1')
    localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected: '%1', sideMode: 'sessions', voiceOff: true, uiUsage: false }))
  })
  const open = async (id = '%1', width = 1280, reducedMotion = 'reduce') => {
    await page.emulateMedia({ reducedMotion })
    await page.setViewportSize({ width, height: 800 })
    await page.goto(`${origin}/chat/?session=${encodeURIComponent(id)}`)
    await page.locator(`.chat-panel[data-pane="${id}"] .task-chips`).waitFor()
  }
  const panel = page.locator('.chat-panel')
  const tasks = () => panel.getByRole('tablist', { name: /^Tasks in/ })
  const chips = () => panel.getByRole('tablist', { name: `Tabs in ${taskName}` })
  const side = () => page.locator('.sidebar')
  const write = (endpoint) => writes.filter(item => item.endpoint === endpoint).at(-1)?.body
  const assertFocus = async (locator) => assert(await locator.evaluate(node => document.activeElement === node))
  const assertHelperLayout = async (sidebar) => {
    const group = sidebar.locator('.task-fold.open .task-kids')
    const needs = group.getByRole('button', { name: /B446 review, Review, needs you/ })
    await needs.waitFor()
    assert.equal(await sidebar.locator('.task-branch .task-row.needs').count(), 1)
    assert.equal(await needs.locator('..').evaluate(row => row.classList.contains('needs')), true)
    assert.deepEqual(await group.evaluate(node => {
      const style = getComputedStyle(node)
      return [style.backgroundColor, style.borderLeftWidth]
    }), ['rgba(0, 0, 0, 0)', '1px'])
    for (const helper of ['B446 second advisor', 'B446 server review']) {
      const row = group.getByRole('button', { name: new RegExp(`^${helper},`) })
      assert(await row.evaluate(button => {
        const name = button.querySelector('.pane-child-name')
        const role = button.querySelector('.task-role')
        return name.clientWidth > role.clientWidth && getComputedStyle(role).textOverflow === 'ellipsis'
      }), `${helper}: the name has more room than the role`)
    }
  }
  const assertCloseLayout = async (dialog, count) => {
    const idle = dialog.getByRole('list', { name: 'Close with the task' })
    await idle.getByText('B446 advisor', { exact: false }).waitFor()
    const working = dialog.getByRole('group', { name: 'Still working (stay open as separate tasks)' })
    const checkboxes = await working.getByRole('checkbox').all()
    assert.equal(checkboxes.length, count)
    for (const checkbox of checkboxes) {
      assert.equal(await checkbox.isChecked(), false)
      const box = await checkbox.boundingBox()
      assert(box.width >= 24 && box.height >= 24, 'Each checkbox has a 24 × 24 px target')
    }
    assert.equal(await dialog.evaluate(node => getComputedStyle(node).paddingInlineStart), '16px')
    assert(await dialog.getByRole('heading').evaluate(node => {
      const title = getComputedStyle(node)
      return (title.fontFamily.includes('sans-serif') || title.fontFamily === 'system-ui') && title.fontSize === '13.5px'
    }), 'The heading uses the compact UI type')
  }
  const waitChoice = (id) => page.locator(`.chat-panel[data-pane="${id}"] .task-chips [data-chip="${id}"][aria-selected=true]`).waitFor()
  const waitWrite = (endpoint) => page.waitForResponse(response => new URL(response.url()).pathname === '/mobile/api/' + endpoint && response.request().method() === 'POST')
  try {
    await open()
    assert.equal(await tasks().getByRole('tab').count(), 3, 'Only the three roots in this stack')
    const parent = tasks().getByRole('tab', { name: /B446 Task tree, Task, needs you, 5 helpers/ })
    assert.equal(await parent.getAttribute('aria-selected'), 'true')
    assert.equal(await parent.getAttribute('aria-controls'), await panel.getByRole('tabpanel').getAttribute('id'))
    assert.equal(await tasks().getByRole('tab', { name: /B447 Settings, from B446/ }).count(), 1, 'Legacy helper flag does not hide a separate task')
    assert.equal(await tasks().locator('[tabindex="0"]').count(), 1)
    await parent.focus()
    await parent.press('ArrowRight')
    await assertFocus(tasks().getByRole('tab', { name: /B447 Settings/ }))
    const main = chips().getByRole('tab', { name: /Main/ })
    const review = chips().getByRole('tab', { name: /B446 review, Review, needs you/ })
    await main.focus()
    await main.press('ArrowLeft')
    await assertFocus(review)
    assert.equal(await chips().locator('[tabindex="0"]').count(), 1)
    await review.press('Home')
    await assertFocus(main)
    await main.press('End')
    await review.press('Enter')
    await waitChoice('%86')
    assert.equal(await parent.getAttribute('aria-selected'), 'true')
    await page.waitForFunction(() => document.activeElement?.getAttribute('data-chip') === '%86')
    await chips().getByRole('tab', { name: /Main/ }).click()

    const fold = side().getByRole('button', { name: `Fold helpers of ${taskName}` })
    await fold.waitFor()
    assert.equal(await fold.getAttribute('aria-expanded'), 'true')
    const foldId = await fold.getAttribute('aria-controls')
    const childFold = side().locator(`[id="${foldId}"]`)
    assert.equal(await childFold.evaluate(node => getComputedStyle(node).transitionDuration), '0s')
    assert.equal(await childFold.getByRole('button', { name: /B446 server review, Worker's review, idle/ }).count(), 1)
    await assertHelperLayout(side())
    await fold.click()
    assert.equal(await childFold.getAttribute('inert'), '')
    assert.equal(await side().getByRole('img', { name: 'B446 advisor, Advisor, idle' }).getAttribute('title'), 'B446 advisor, Advisor, idle')
    assert.equal(await side().getByRole('img', { name: 'B446 review, Review, needs you' }).count(), 1)
    const done = side().getByRole('button', { name: 'Done 1' })
    assert.equal(await done.getAttribute('aria-expanded'), 'false')
    const selectedRow = id => side().locator(`button[data-select-pane="${id}"][aria-current="page"]`)
    await selectedRow('%1').focus()
    await page.keyboard.press('Alt+ArrowDown')
    await assertFocus(selectedRow('%81'))
    await page.keyboard.press('Alt+ArrowDown')
    await assertFocus(selectedRow('%88'))
    await page.keyboard.press('Alt+ArrowUp')
    await assertFocus(selectedRow('%81'))
    await page.keyboard.press('Alt+ArrowUp')
    await assertFocus(selectedRow('%1'))
    await done.click()
    await side().getByRole('button', { name: /B300 Cleanup, Task, done/ }).waitFor()

    const card = page.getByRole('region', { name: 'B446 review needs you' })
    await Promise.all([waitWrite('approval'), card.getByRole('button', { name: 'Yes, apply' }).click()])
    assert.deepEqual(write('approval'), { session: '%86', thread: 'thread-%86', approval: 'approval-digest', choice: 1 })
    const worker = listing.sessions.find(pane => pane.id === '%82')
    Object.assign(worker, { state: 'Idle', ask: { next: 'Which test should I run?', since: 220, kind: 'reply' }, answer_digest: replyDigest })
    await open()
    const areas = page.locator('.task-answer')
    assert.match(await areas.first().textContent(), /B446 server needs you/)
    const answer = page.getByRole('textbox', { name: 'Answer B446 server' })
    await answer.fill('Run the small test')
    await Promise.all([waitWrite('task-tree/answer'), answer.press('Enter')])
    assert.deepEqual(write('task-tree/answer'), { pane: '%82', thread: 'thread-%82', digest: replyDigest, text: 'Run the small test' })
    await page.getByRole('alert').getByText('The helper’s question changed.').waitFor()
    assert.equal(await answer.inputValue(), 'Run the small test')
    staleAnswer = false
    await Promise.all([waitWrite('task-tree/answer'), answer.press('Enter')])
    await page.getByText('Sent to B446 server.').waitFor()

    const dialog = page.locator('dialog[open].sheet').last()
    await side().getByRole('button', { name: `Actions for ${separateName}` }).click()
    await dialog.getByRole('button', { name: 'Move into task…' }).click()
    assert.equal(await dialog.getByRole('button', { name: /B401 Sidebar fix/ }).count(), 0)
    await Promise.all([waitWrite('task-tree/move'), dialog.getByRole('button', { name: taskName }).click()])
    assert.deepEqual(write('task-tree/move'), { pane: '%81', thread: 'thread-%81', into: '%1', into_thread: 'root-thread' })
    await dialog.waitFor({ state: 'hidden' })
    await side().getByRole('button', { name: `Actions for ${reviewName}` }).click()
    await Promise.all([waitWrite('task-tree/move'), dialog.getByRole('button', { name: 'Move out of task' }).click()])
    assert.deepEqual(write('task-tree/move'), { pane: '%86', thread: 'thread-%86', into: null, into_thread: null })
    await dialog.waitFor({ state: 'hidden' })
    const rootRow = side().getByRole('button', { name: /B446 Task tree, Task/ })
    await Promise.all([waitWrite('task-tree/move'), side().getByRole('button', { name: /B447 Settings, from B446, Task/ }).dragTo(rootRow)])
    assert.deepEqual(write('task-tree/move'), { pane: '%81', thread: 'thread-%81', into: '%1', into_thread: 'root-thread' })

    await side().getByRole('button', { name: `Actions for ${taskName}` }).click()
    await Promise.all([waitWrite('task-tree/done'), dialog.getByRole('button', { name: 'Mark done' }).click()])
    assert.deepEqual(write('task-tree/done'), { pane: '%1', thread: 'root-thread', done: true })
    await dialog.waitFor({ state: 'hidden' })
    await side().getByRole('button', { name: 'Done 1' }).click()
    await side().getByRole('button', { name: `Actions for ${doneName}` }).click()
    await Promise.all([waitWrite('task-tree/done'), dialog.getByRole('button', { name: 'Reopen' }).click()])
    assert.deepEqual(write('task-tree/done'), { pane: '%87', thread: 'thread-%87', done: false })
    await dialog.waitFor({ state: 'hidden' })
    await panel.locator('[data-close-for="%1"]').click()
    const close = page.getByRole('dialog', { name: 'Close B446 Task tree?' })
    await assertCloseLayout(close, 1)
    const closeReview = close.getByRole('checkbox', { name: /B446 review/ })
    await closeReview.press('Space')
    await assertFocus(closeReview)
    assert.equal(await closeReview.isChecked(), true)
    await Promise.all([waitWrite('task-tree/close'), close.getByRole('button', { name: 'Close', exact: true }).last().click()])
    assert.deepEqual(write('task-tree/close'), { pane: '%1', thread: 'root-thread', close_also: [{ pane: '%86', thread: 'thread-%86' }] })
    assert.equal(writes.filter(item => item.endpoint === 'session').length, 0, 'A parent close never takes the legacy DELETE route')
    await close.waitFor({ state: 'hidden' })

    await side().getByRole('button', { name: 'Settings', exact: true }).click()
    await page.getByRole('searchbox', { name: 'Search settings' }).fill(settingsLabel)
    const toggle = page.getByRole('switch', { name: new RegExp(settingsLabel) })
    assert.equal(await toggle.isChecked(), false)
    await Promise.all([waitWrite('prefs'), toggle.click()])
    await page.waitForFunction(() => document.querySelector('dialog.preferences input[role=switch]')?.checked)
    assert.deepEqual(write('prefs'), { changes: { task_auto_close: true } })

    await page.getByRole('button', { name: 'Close settings' }).click()
    await page.getByRole('button', { name: /^Tether bugs, 1 tab/ }).click()
    await tasks().getByRole('tab', { name: /B401 Sidebar fix, Task, idle/ }).waitFor()
    assert.equal(await tasks().getByRole('tab').count(), 1, 'A single-task stack has a task row')

    delete worker.ask
    delete worker.answer_digest
    worker.state = 'Working'
    await open('%1', 320)
    assert.equal(await tasks().count(), 0)
    assert.equal(await panel.locator('.task-phone-stack').textContent(), 'Task tree')
    assert(await chips().evaluate(node => node.scrollLeft > 0), 'Initial phone view scrolls to a waiting helper')
    await page.getByRole('button', { name: /^Open sessions/ }).click()
    const drawer = page.getByRole('dialog', { name: 'Sessions' })
    await assertHelperLayout(drawer)
    await drawer.getByRole('button', { name: `Close ${taskName}`, exact: true }).click()
    await assertCloseLayout(close, 2)
    await close.getByRole('button', { name: 'Cancel' }).click()
    await close.waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: /^Open sessions/ }).click()
    await drawer.getByRole('button', { name: /B446 review, Review, needs you/ }).waitFor()
    await drawer.getByRole('button', { name: /B446 review, Review, needs you/ }).click()
    await waitChoice('%86')
    assert.equal(await panel.locator('h1').innerText(), taskName)

    const reviewPane = listing.sessions.find(pane => pane.id === '%86')
    reviewPane.state = 'Idle'
    reviewPane.approval = null
    await open('%1', 320, 'no-preference')
    assert.equal(await chips().evaluate(node => node.scrollLeft), 0, 'Main stays in view when all helpers are quiet')
    Object.assign(listing.sessions.find(pane => pane.id === '%85'), { escalated: true, since: 230 })
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    await page.waitForFunction(() => {
      const chip = document.querySelector('.task-chips [data-chip="%85"]')
      const strip = chip?.parentElement
      if (!chip || !strip || !chip.getAttribute('aria-label').includes('needs you')) return false
      const tab = chip.getBoundingClientRect(), box = strip.getBoundingClientRect()
      return tab.left >= box.left && tab.right <= box.right
    })

    // Active only keeps the task and its working or escalated helpers in view.
    listing.sessions[0].state = 'Idle'
    await open()
    await side().getByRole('button', { name: 'Active only' }).click()
    await panel.getByRole('button', { name: /^Tether bugs, 1 tab/ }).click()
    await selectedRow('%88').waitFor()
    assert.equal(await side().locator('button[data-select-pane="%82"]').isVisible(), true)
    assert.equal(await side().getByRole('button', { name: /B446 server review, Worker's review, needs you/ }).isVisible(), true)
    assert.equal(await side().locator('button[data-select-pane="%83"]').count(), 0)
    await side().getByRole('button', { name: 'Show 5 quiet tabs' }).click()
    assert.equal(await side().locator('button[data-select-pane="%83"]').isVisible(), true)
    assert.deepEqual(errors, [])
    console.log('Task tree: root tabs, roving focus, dots and folds, child answers, move menu and drag, Done, close plan, setting and 320 px navigation pass')
  } catch (error) {
    if (errors.length) console.error(errors)
    if (process.env.DEBUG_TASK_TREE) console.error((await page.locator('.crash-details pre, body').last().textContent()).slice(0, 2500))
    throw error
  } finally {
    await browser.close()
  }
}

if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1 })
module.exports = { fixture, plan, settingsLabel }
