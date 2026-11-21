// Search commands with no workspace, plus the original workspace order.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { getBrowser } = require('./browser_test_helper.cjs')

const dist = path.resolve(process.env.DIST || path.join(__dirname, 'dist'))
const shots = process.env.SHOTS
const origin = 'http://localhost:43177'
const pane = { id: '%1', tmux_session: '$1', label: 'example:0.1', agent: 'claude', dir: '/srv/example', state: 'Idle', thread: 't1', writable: true, approval: null, ask: null }
const fixture = {
  chat: { messages: [], agent: pane.agent, thread: pane.thread, session: pane.id, writable: true, state: pane.state, busy: false, earlier: 0, limit: 100, approval: null },
  footer: { observed: false, notices: [] },
  subagents: { main: { model: null, context: null, title: null }, agents: [], tasks: [] },
  catalog: { commands: [], plugins: [], mcp: [], note: '' },
  'codex-runs': { pane: pane.id, running: 0, runs: [] },
  'side-shells': { shells: [] },
  'browser/agents': { enabled: false, available: false, agents: [], asks: [] },
  'browser/signed/access': { browser: 'signed', requests: [], grants: [], ended: [] },
  prefs: { prefs: {}, version: 1 },
  'projects/backlog': { text: '', version: 'v1', limit: 50000 },
  'work/board': { projects: [], items: [] },
  reminders: { reminders: [] },
  undo: { entries: [], next_undo_label: null, next_redo_label: null },
  usage: { claude: null, codex: null },
}
const live = 'Friday Live mode: talk hands-free'
// Each row is [label, key hint]; '' means the row shows no key hint.
// Captured on the unmodified app on a non-Mac platform (the init script pins it).
const globalCommands = [
  ['New session', 'Alt N'], ['Keyboard shortcuts', 'Ctrl /'], ['Undo history', ''], ['Plan usage', ''],
  ['Backlog', 'Alt B'], ['Backlog page', 'Alt Shift B'], ['Reminders', ''], ['Work board', ''],
  ['Agents page', ''], ['Factory view', ''], [live, ''],
]
// On the Files page, Home gets its slot hint and New session loses its own.
const filesCommands = [
  ['Home', 'Alt H'], ['New session', ''], ['Keyboard shortcuts', 'Ctrl /'], ['Undo history', ''], ['Plan usage', ''],
  ['Backlog', 'Alt B'], ['Backlog page', 'Alt Shift B'], ['Reminders', ''], ['Work board', ''],
  ['Agents page', ''], ['Factory view', ''], [live, ''],
]
// Captured with the selected Claude pane below.
const workspaceCommands = [
  ['Home', 'Alt H'], ['Split right', 'Ctrl \\'], ['Grid of four', 'Ctrl Shift \\'], ['Terminal view of the focused pane', 'Alt T'],
  ['Show the terminal panel', 'Ctrl .'], ['Show the changes panel', ''], ['Show the browser panel', ''], ['Show the files panel', ''],
  ['Side chat: fork this chat beside it', 'Ctrl ;'], ['Show background tasks', ''],
  ['New session', 'Alt N'], ['Hide the sidebar', 'Ctrl B'], ['Back', 'Alt ←'], ['Keyboard shortcuts', 'Ctrl /'], ['Undo history', ''],
  ['Make a layout', 'Alt L'], ['Restore the default layout', 'Alt Shift L'], ['Plan usage', ''], ['Backlog', 'Alt B'],
  ['Backlog page', 'Alt Shift B'], ['Reminders', ''], ['Work board', ''], ['Agents page', ''], ['Factory view', ''],
  ['Message all sessions', ''], [live, ''],
]
// The phone shows no key hints on any row.
const phone = rows => rows.map(([name]) => [name, ''])

async function main() {
  const browser = await getBrowser({ local: true })
  const errors = []
  const failures = []
  try {
    if (shots) await fs.mkdir(shots, { recursive: true })
    for (const [width, height, workspace] of [[390, 844, false], [1280, 844, false], [1280, 844, true]]) {
      const label = `${width}px ${workspace ? 'workspace' : 'no pane'}`
      const context = await browser.newContext({ viewport: { width, height }, hasTouch: width < 900, serviceWorkers: 'block' })
      const page = await context.newPage()
      try {
        page.setDefaultTimeout(5000)
        page.on('pageerror', error => errors.push(error.message))
        await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' })
        await page.routeWebSocket('**/*', socket => socket.close())
        await page.route('**/*', async route => {
          const url = new URL(route.request().url())
          if (url.origin !== origin) return route.abort('blockedbyclient')
          if (url.pathname.startsWith('/mobile/api/')) {
            const endpoint = url.pathname.slice('/mobile/api/'.length)
            if (endpoint === 'stream') return route.fulfill({ status: 204 })
            if (endpoint === 'sessions') return route.fulfill({ json: { sessions: workspace ? [pane] : [], default_thread: null } })
            return route.fulfill({ json: fixture[endpoint] ?? {} })
          }
          if (url.pathname.startsWith('/chat/')) {
            const rel = url.pathname.slice('/chat/'.length) || 'index.html'
            return route.fulfill({ path: path.join(dist, rel) })
          }
          return route.fulfill({ status: 404 })
        })
        await page.addInitScript(selected => {
          // Key hints read Ctrl and Alt on every host, also on a Mac.
          Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'Linux x86_64' })
          localStorage.setItem('tmux-chat.prefs.synced', '1')
          localStorage.setItem('tmux-chat.prefs.v1', JSON.stringify({ selected, theme: 'light', voiceOff: true, voiceSound: false }))
          // Test opening Live mode without starting capture on the test machine.
          navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Test microphone is off', 'NotAllowedError') }
        }, workspace ? pane.id : null)
        const menu = page.getByRole('dialog', { name: 'Search', exact: true })
        const search = menu.getByRole('combobox')
        const command = name => menu.getByRole('option').filter({
          has: page.locator('.command-label').filter({ hasText: new RegExp('^' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$') }),
        })
        // [label, key hint] for each row. The phone link may open search without a Back step, so a no-pane list leaves Back out.
        const rows = async () => (await menu.locator('.command-row').evaluateAll(list => list.map(row => [
          row.querySelector('.command-label').textContent, row.querySelector('kbd')?.textContent ?? '',
        ]))).filter(([name]) => workspace || name !== 'Back')
        const openSearch = async () => {
          await page.keyboard.press('Alt+Space')
          await search.waitFor()
        }
        // The search link works on a touch phone without a physical keyboard.
        await page.goto(`${origin}/chat/${width < 900 ? '?go=search' : ''}`)
        if (workspace) {
          await page.waitForFunction(id => history.state?.place?.pane === id, pane.id)
          await page.getByRole('button', { name: 'Command menu' }).click()
        } else {
          await page.locator(width < 900 ? '.phone-home' : '.screen-home').waitFor()
          if (width >= 900) await openSearch()
        }
        await search.waitFor()
        // The desktop button adds a Back step when it opens search. The phone link may open without activation.
        if (workspace) await command('Back').waitFor()
        assert.equal(await search.evaluate(input => input === document.activeElement), true)
        const shown = await rows()
        if (workspace) assert.deepEqual(shown.slice(0, shown.findIndex(([name]) => name === 'example')), workspaceCommands)
        else assert.deepEqual(shown, width < 900 ? phone(globalCommands) : globalCommands)
        if (!workspace) {
          assert.equal(await menu.locator('.command-list').evaluate(list => list.scrollWidth <= list.clientWidth), true)
          if (shots) await page.screenshot({ path: path.join(shots, `global-commands-${width}.png`) })
          for (const [name, target, close] of [
            ['Backlog', '.backlog-sheet:not(.backlog-page)[open]'],
            ['Backlog page', '.backlog-page[open]'],
            ['Reminders', '.reminders-sheet[open]'],
            ['New session', '.task-pop[open]'],
            ['Keyboard shortcuts', '.shortcuts-sheet[open]'],
            ['Undo history', '.undo-history[open]'],
            ['Plan usage', '[aria-labelledby="usage-title"][open]'],
            ['Work board', '.work-sheet[open]'],
            ['Agents page', '.agents-sheet[open]'],
            ['Factory view', '.factory[open]', 'Close the Factory view'],
            [live, '[role="dialog"][aria-label="Friday Live mode"]', 'End Live mode'],
          ]) {
            await search.fill(name)
            await command(name).click()
            const surface = page.locator(target)
            await surface.waitFor()
            if (name === 'Backlog' && width === 390 && shots) {
              await page.screenshot({ path: path.join(shots, 'global-commands-backlog-390.png') })
            }
            if (name === 'Keyboard shortcuts') {
              await surface.getByRole('searchbox').fill('backlog')
              await surface.getByText('Backlog as a full page', { exact: true }).waitFor()
              await surface.getByRole('searchbox').clear()
            }
            if (close) await surface.getByRole('button', { name: close }).click()
            else await page.keyboard.press('Escape')
            await surface.waitFor({ state: 'hidden' })
            await openSearch()
          }
          await page.keyboard.press('Escape')
          await menu.waitFor({ state: 'detached' })
          await page.getByRole('button', { name: 'Files', exact: true }).click()
          await page.locator(width < 900 ? '.phone-files' : '.screen-files').waitFor()
          await openSearch()
          assert.deepEqual(await rows(), width < 900 ? phone(filesCommands) : filesCommands)
          await search.fill('Home')
          await page.keyboard.press('Enter')
          await page.locator(width < 900 ? '.phone-home' : '.screen-home').waitFor()
        }
        console.log(`ok ${label}`)
      } catch (error) {
        failures.push(error)
        console.error(`FAIL ${label}\n${error.stack}`)
        if (shots) await page.screenshot({ path: path.join(shots, `global-commands-error-${width}-${workspace}.png`) }).catch(() => {})
      } finally {
        await context.close()
      }
    }
    assert.deepEqual(errors, [], 'No uncaught page errors')
    if (failures.length) process.exitCode = 1
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
