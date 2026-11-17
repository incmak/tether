// The Factory view's phase rules (src/factoryModel.ts): which phase each task is in, why, and the spatial focus
// that arrow keys and TV remotes use. Pure functions, no browser.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const ts = require('typescript')

const load = (file, deps = {}) => {
  const mod = new module.constructor(file)
  mod.require = name => deps[name] ?? require(name)
  mod._compile(ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, file)
  return mod.exports
}
const paneState = load('src/paneState.ts')
const items = load('src/backlogItems.ts')
const m = load('src/factoryModel.ts', { './paneState': paneState })

const NOW = new Date(2026, 10, 17, 12, 0).getTime()
const iso = minutesAgo => new Date(NOW - minutesAgo * 60000).toISOString()
const pane = (id, extra = {}) => ({ id, tmux_session: '$1', label: `s:${id}`, agent: 'claude', dir: '/srv/app', state: 'Idle', writable: true, thread: id, approval: null, pane_name: '', ...extra })
const tool = (name, title, minutesAgo = 1) => ({ name, title, at: iso(minutesAgo) })
const backlog = [
  '## tether',
  '- [ ] Factory view (claude · pane %1) #B191',
  '  - Doing: claude · pane %1',
  '  - Worker tab: Factory View.',
  '- [ ] Dark mode #B12',
  '- [ ] Closed tab item #B13',
  '  - Doing: claude · pane %99',
  '- [ ] Deploying item #B14',
  '- [x] Shipped today #B15',
  '  - Done: 2026-11-17 08:30 · abc1234 shipped',
  '- [x] Shipped last week #B16',
  '  - Done: 2026-11-09 08:30 · old',
  '## other',
  '- [ ] Reviewed item #B20',
].join('\n')

function floor(panes, tools = {}, extra = {}) {
  return m.buildFloor({ panes, rows: items.parseBacklog(backlog), data: { now: NOW / 1000, tools, deployed: [], commits: [], ...extra }, now: NOW })
}
const byId = (result, id) => result.tasks.find(task => task.id === id)

// Inbox, Done and links from the item.
{
  const result = floor([pane('%1', { pane_name: 'Automation' }), pane('%2', { pane_name: 'Factory View', state: 'Working' })], { '%2': [tool('Edit', 'src/Factory.tsx')] })
  assert.equal(byId(result, 'B12').phase, 'inbox')
  assert.match(byId(result, 'B12').why, /no tab works on it/)
  assert.equal(byId(result, 'B13').phase, 'inbox')
  assert.match(byId(result, 'B13').why, /Doing tab %99 is closed/)
  assert.equal(byId(result, 'B15').phase, 'done')
  assert.equal(byId(result, 'B16'), undefined, 'an item done over 24 h ago leaves the floor')
  const factory = byId(result, 'B191')
  assert.equal(factory.phase, 'build')
  assert.deepEqual(factory.agents.map(agent => [agent.name, agent.role]), [['Automation', 'main'], ['Factory View', 'worker']])
  assert.match(factory.why, /edits files: Edit src\/Factory\.tsx/)
  assert.match(factory.why, /the item's notes name tab Factory View/)
  assert.equal(result.tasks.filter(task => task.agents.some(agent => agent.pane.id === '%2')).length, 1, 'a linked tab is not also a task of its own')
}

// Spec, Verify, Review and Deploy from the latest steps.
{
  const at = (tools, extra) => byId(floor([pane('%5', { pane_name: 'B20 work', state: 'Working', ...extra })], { '%5': tools }), 'B20')
  assert.equal(at([tool('Read', 'a.ts'), tool('Grep', 'foo'), tool('Bash', 'cd ui && grep -n x src/a.ts')]).phase, 'spec')
  assert.equal(at([tool('Edit', 'a.ts'), tool('Bash', 'cd server && python3 -m unittest -q')]).phase, 'verify')
  assert.equal(at([tool('Edit', 'a.ts'), tool('Bash', 'cd ui && npx tsc -b')]).phase, 'verify')
  assert.equal(at([tool('Edit', 'a.ts'), tool('Bash', 'codex-lane -- -m x "review"')]).phase, 'review')
  assert.equal(at([tool('Edit', 'a.ts'), tool('Bash', 'python3 scripts/deploy-ui.py /tmp/b ui/dist')]).phase, 'deploy')
  assert.equal(at([tool('Read', 'a.ts'), tool('Edit', 'a.ts'), tool('Bash', 'npm run build')]).phase, 'build')
  const none = at([])
  assert.equal(none.phase, 'build')
  assert.match(none.why, /guessed: B20 work is open, no step read/)
  assert.match(at([], { agent: 'antigravity' }).why, /Tether reads no Antigravity steps/)
}

// A working reviewer puts the task in Review; a DEPLOYED.md line puts an open item in Deploy.
{
  const result = floor([pane('%6', { pane_name: 'B20 work', reviewers: ['%7'] }), pane('%7', { agent: 'codex', state: 'Working' })], { '%6': [tool('Edit', 'x')] })
  const task = byId(result, 'B20')
  assert.equal(task.phase, 'review')
  assert.deepEqual(task.agents.map(agent => agent.role), ['worker', 'reviewer'])
  assert.equal(result.tasks.some(entry => entry.id === '%7'), false)
  const deployed = floor([pane('%8', { pane_name: 'B14 ship', state: 'Working' })], { '%8': [tool('Edit', 'x')] }, { deployed: [{ at: NOW / 1000 - 600, timed: true, commit: 'abc1234', text: 'abc1234 B14 shipped', refs: ['B14'] }] })
  assert.equal(byId(deployed, 'B14').phase, 'deploy')
  assert.equal(byId(deployed, 'B14').deploys.length, 1)
}

// Review findings: a closed reviewer does not crash; a reviewer named for the item stays a reviewer; the newest step leads, not pane order.
{
  const gone = floor([pane('%6', { pane_name: 'B20 work', reviewers: ['%2'] })], { '%6': [tool('Edit', 'x')] })
  assert.equal(byId(gone, 'B20').agents.length, 1)
  const named = floor([pane('%6', { pane_name: 'B20 work', reviewers: ['%7'] }), pane('%7', { pane_name: 'B20 review', agent: 'codex', state: 'Working' })], { '%6': [tool('Edit', 'x')] })
  assert.equal(byId(named, 'B20').phase, 'review')
  assert.deepEqual(byId(named, 'B20').agents.map(agent => agent.role), ['worker', 'reviewer'])
  const a = pane('%8', { pane_name: 'B20 a', state: 'Working' }), b = pane('%9', { pane_name: 'B20 b', state: 'Working' })
  const tools = { '%8': [tool('Edit', 'x', 10)], '%9': [tool('Bash', 'npx tsc -b', 2)] }
  assert.equal(byId(floor([a, b], tools), 'B20').phase, 'verify')
  assert.equal(byId(floor([b, a], tools), 'B20').phase, 'verify', 'pane order does not change the phase')
}

// Tabs with no item: own task; idle ones from today are Done, older ones parked; waits and stuck.
{
  const result = floor([
    pane('%30', { pane_name: 'Fresh idle', finished: NOW / 1000 - 3600 }),
    pane('%31', { pane_name: 'Old idle' }),
    pane('%32', { pane_name: 'Asks', state: 'Approval', needs: { kind: 'approval', text: 'Allow rm?' } }),
    pane('%33', { pane_name: 'Quiet', ask: { pane: '%33', thread: 't', since: 1, next: 0, kind: 'question', opened: false, snoozed_until: null, urgent: false } }),
    pane('%34', { pane_name: 'B20 slow' }),
    pane('%40', { agent: 'shell' }),
  ], { '%31': [tool('Read', 'x', 3000)], '%32': [tool('Bash', 'rm -rf build')], '%34': [tool('Edit', 'x', 90)] })
  assert.equal(byId(result, '%30').phase, 'done')
  assert.equal(byId(result, '%31'), undefined)
  assert.equal(result.parked, 1)
  assert.equal(byId(result, '%32').waits, true)
  assert.equal(byId(result, '%32').agents[0].state, 'prompt')
  assert.equal(byId(result, '%32').agents[0].step, 'Allow rm?')
  assert.equal(byId(result, '%33').waits, true)
  assert.equal(byId(result, '%33').agents[0].state, 'waits')
  const slow = byId(result, 'B20')
  assert.equal(slow.stuck, true, 'an open task whose tabs have not moved for 90 min is stuck')
  assert.equal(result.tasks.some(task => task.id === '%40'), false, 'shell tabs are not agents')
}

// History: the first time each phase is seen; a change is reported once.
{
  const task = { id: 'B1', phase: 'build' }
  const first = m.recordHistory({}, [task], 1000)
  assert.deepEqual(first.history.B1, [{ phase: 'build', at: 1000 }])
  assert.equal(first.moved.length, 0)
  const same = m.recordHistory(first.history, [task], 2000)
  assert.equal(same.history.B1.length, 1)
  const moved = m.recordHistory(same.history, [{ id: 'B1', phase: 'verify' }], 3000)
  assert.deepEqual(moved.history.B1.map(entry => entry.phase), ['build', 'verify'])
  assert.equal(moved.moved[0].id, 'B1')
  assert.equal(m.phaseSince(moved.history, { id: 'B1' }), 3000)
  // A poll with no tasks keeps the history; entries older than a week go.
  const empty = m.recordHistory(moved.history, [], 4000)
  assert.deepEqual(empty.history, moved.history)
  const old = m.recordHistory({ B2: [{ phase: 'done', at: 0 }], B1: moved.history.B1 }, [], 8 * 24 * 3600000)
  assert.deepEqual(Object.keys(old.history), [])
  assert.deepEqual(Object.keys(m.recordHistory({ B2: [{ phase: 'done', at: 0 }] }, [], 6 * 24 * 3600000).history), ['B2'])
}

// Spatial focus: arrows go to the nearest item in that direction; nothing beyond the edge.
{
  const box = (x, y) => ({ x, y, width: 100, height: 40 })
  const grid = [box(0, 0), box(120, 0), box(240, 0), box(0, 60), box(120, 60), box(0, 120)]
  assert.equal(m.nearest(grid[0], grid, 'right'), 1)
  assert.equal(m.nearest(grid[1], grid, 'down'), 4)
  assert.equal(m.nearest(grid[4], grid, 'left'), 3)
  assert.equal(m.nearest(grid[3], grid, 'down'), 5)
  assert.equal(m.nearest(grid[0], grid, 'up'), -1)
  assert.equal(m.nearest(grid[2], grid, 'right'), -1)
  // Down prefers the item straight below over a nearer one far to the side.
  assert.equal(m.nearest(box(0, 0), [box(0, 0), box(300, 50), box(0, 90)], 'down'), 2)
}

console.log('factory model: ok')
