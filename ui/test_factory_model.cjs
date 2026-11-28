// The Factory view's phase rules (src/factoryModel.ts): which phase each task is in, why, and the spatial focus
// that arrow keys and TV remotes use. Pure functions, no browser.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')

const load = (file, deps = {}, extra = '') => {
  const mod = new module.constructor(file)
  mod.require = name => deps[name] ?? require(name)
  mod._compile(ts.transpileModule(readFileSync(file, 'utf8') + extra, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, file)
  return mod.exports
}
const paneState = load('src/paneState.ts')
const items = load('src/backlogItems.ts')
const zone = load('src/zone.ts')
const m = load('src/factoryModel.ts', { './paneState': paneState, './zone': zone })

test('a date-only deploy line keeps its written day in New York', () => {
  const dateOnly = { day: '2026-11-21', at: Date.parse('2026-11-20T18:30Z') / 1000, timed: false }
  zone.setTetherZone('America/New_York')
  try {
    assert.equal(zone.dayKey(dateOnly.at * 1000), '2026-11-20')
    const writtenDay = new Intl.DateTimeFormat(undefined, { timeZone: 'UTC' }).format(Date.UTC(2026, 10, 21))
    assert.equal(zone.fmtDay(dateOnly.day), writtenDay)
  } finally {
    zone.setTetherZone('Asia/Kolkata')
  }
})

zone.setTetherZone('Asia/Kolkata')
const NOW = Date.parse('2026-11-17T06:30:00Z')
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
const exactStamps = JSON.parse(readFileSync('../server/fixtures/zone_exact_stamps.json', 'utf8'))

for (const { zone, instant, stamp } of exactStamps) {
  test(`Factory Done time round-trips ${zone} ${stamp}`, () => {
    const [row] = items.parseBacklog(`- [x] Shipped #B15\n  - Done: ${stamp} · shipped`)
    assert.equal(m.doneAt(row, zone), Date.parse(instant))
    assert.equal(m.doneAt(row, 'Pacific/Auckland'), Date.parse(instant))
  })
}

for (const { zone, instant, stamp } of [exactStamps[0], exactStamps[3]]) {
  test(`Factory expires the first occurrence of an exact Done stamp after 24 hours in ${zone}`, () => {
    const rows = items.parseBacklog(`- [x] Shipped #B15\n  - Done: ${stamp} · shipped`)
    const expiry = Date.parse(instant) + 24 * 60 * 60 * 1000
    const taskAt = now => byId(m.buildFloor({ panes: [], rows, data: null, now, zone }), 'B15')
    assert.equal(taskAt(expiry)?.phase, 'done', stamp)
    assert.equal(taskAt(expiry + 1000), undefined, stamp)
  })
}

test('Factory accepts a colon in the offset of a Done stamp', () => {
  const [row] = items.parseBacklog('- [x] Shipped #B15\n  - Done: 2026-04-05 01:45 +10:30 · shipped')
  assert.equal(m.doneAt(row, 'Australia/Lord_Howe'), Date.parse('2026-04-04T15:15:00Z'))
})

test('Factory retains a repeated-hour Done stamp for 24 hours from its later instant', () => {
  const rows = items.parseBacklog('## tether\n- [x] Shipped #B15\n  - Done: 2026-11-01 01:30 · shipped')
  const taskAt = iso => byId(m.buildFloor({ panes: [], rows, data: null, now: Date.parse(iso), zone: 'America/New_York' }), 'B15')
  assert.equal(taskAt('2026-11-02T06:00:00Z')?.phase, 'done')
  assert.equal(taskAt('2026-11-02T06:30:01Z'), undefined)
})

test('Factory retains an unambiguous Done stamp for 24 hours from its only instant', () => {
  const rows = items.parseBacklog('## tether\n- [x] Shipped #B15\n  - Done: 2026-11-01 02:30 · shipped')
  const taskAt = iso => byId(m.buildFloor({ panes: [], rows, data: null, now: Date.parse(iso), zone: 'America/New_York' }), 'B15')
  assert.equal(taskAt('2026-11-02T07:30:00Z')?.phase, 'done')
  assert.equal(taskAt('2026-11-02T07:30:01Z'), undefined)
})

test('Factory counts an untimed deploy through the end of its written day', () => {
  const rows = items.parseBacklog('## tether\n- [x] Shipped #B15')
  const line = { day: '2026-11-21', at: Date.parse('2026-11-20T18:30Z') / 1000, timed: false, text: 'deploy', refs: ['B15'] }
  const taskAt = iso => {
    const now = Date.parse(iso)
    return byId(m.buildFloor({ panes: [], rows, data: { now: now / 1000, tools: {}, deployed: [line], commits: [] }, now, zone: 'America/New_York' }), 'B15')
  }
  assert.equal(taskAt('2026-11-22T23:00:00Z')?.phase, 'done')
  assert.equal(taskAt('2026-11-23T05:00:00Z'), undefined)
})

test('Factory uses the end of a 25-hour deploy day for an open item', () => {
  const rows = items.parseBacklog('## tether\n- [ ] Shipping #B15\n  - Doing: claude · pane %1')
  const line = { day: '2026-11-01', at: Date.parse('2026-10-31T18:30Z') / 1000, timed: false, text: 'deploy', refs: ['B15'] }
  const taskAt = iso => {
    const now = Date.parse(iso)
    return byId(m.buildFloor({ panes: [pane('%1')], rows, data: { now: now / 1000, tools: {}, deployed: [line], commits: [] },
      now, zone: 'America/New_York' }), 'B15')
  }
  assert.equal(taskAt('2026-11-03T04:30:00Z')?.phase, 'deploy')
  assert.equal(taskAt('2026-11-03T05:00:00Z')?.phase, 'build')
})

test('Factory proof renders an untimed deploy from an older backend without day', () => {
  const { TaskLevel } = load('src/Factory.tsx', {
    './backlogItems': items, './backlogNews': {}, './backNav': {}, './paneState': paneState,
    './factoryModel': m, './zone': zone, './Factory.css': {},
  }, '\nexport { TaskLevel }')
  const line = { at: Date.parse('2026-11-20T18:30Z') / 1000, timed: false, text: 'old backend deploy', refs: ['B15'] }
  const task = byId(floor([], {}, { deployed: [line] }), 'B15')
  assert.ok(task)
  const html = renderToStaticMarkup(React.createElement(TaskLevel, { task, history: [], now: NOW }))
  assert.ok(html.includes(`<b>DEPLOYED.md</b> ${zone.fmtDate(line.at * 1000)}: old backend deploy`))
  const modern = { ...line, day: '2026-11-21', text: 'date-only deploy' }
  const dated = renderToStaticMarkup(React.createElement(TaskLevel, { task: { ...task, deploys: [modern] }, history: [], now: NOW }))
  assert.ok(dated.includes(`<b>DEPLOYED.md</b> ${zone.fmtDay(modern.day)}: date-only deploy`))
})

const members = task => task.agents.map(agent => [agent.pane.id, agent.role])
const visiblePanes = result => result.tasks.flatMap(task => task.agents.map(agent => agent.pane.id)).sort()

test('the same Done stamp and deployment clock follow Kolkata and New York independently of device TZ', () => {
  const item = items.parseBacklog(backlog).find(row => row.ref === 'B15')
  assert.ok(item)
  const deployed = Date.parse('2026-11-17T03:00:00Z')
  try {
    assert.equal(m.doneAt(item), deployed)
    assert.equal(m.clock(deployed), '08:30')
    zone.setTetherZone('America/New_York')
    assert.equal(m.doneAt(item), Date.parse('2026-11-17T13:30:00Z'))
    assert.equal(m.clock(deployed), '22:00')
  } finally {
    zone.setTetherZone('Asia/Kolkata')
  }
})

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
  assert.equal(at([], { agent: 'antigravity' }).why, none.why)
  assert.equal(at([tool('run_command', 'python3 -m unittest -q')], { agent: 'antigravity' }).phase, 'verify')
}

// Tool order from server/fixtures/antigravity_1.5.19/transcript.jsonl.
const agyTools = [
  tool('read_file', '.'),
  tool('read_file', 'calc.py'),
  tool('update_steps', ''),
  tool('run_command', 'ls -la /workspace/agyrun'),
  tool('run_command', 'grep -rn "beta" /workspace/agyrun'),
  tool('read_file', '/workspace/agyrun/calc.py'),
  tool('update_steps', ''),
  tool('edit_file', '/workspace/agyrun/calc.py'),
  tool('write_file', '/workspace/agyrun/hello.txt'),
  tool('update_steps', ''),
  tool('run_command', "cd /workspace/agyrun && python3 -c 'import calc; print(calc.add(2,3))'"),
  tool('update_steps', ''),
]

test('Antigravity fixture through step 6 is Spec', () => {
  assert.equal(m.phaseOfTools(agyTools.slice(0, 6)).phase, 'spec')
})

test('Antigravity fixture edits put the task in Build through step 11', () => {
  const afterEdits = m.phaseOfTools(agyTools.slice(0, 9))
  assert.equal(afterEdits.phase, 'build')
  assert.equal(afterEdits.why, 'edits files: write_file /workspace/agyrun/hello.txt')
  assert.equal(m.phaseOfTools(agyTools.slice(0, 8)).why, 'edits files: edit_file /workspace/agyrun/calc.py')
})

test('Antigravity fixture remains in Build after the Python smoke check', () => {
  const result = m.phaseOfTools(agyTools)
  assert.equal(result.phase, 'build')
  assert.equal(result.why, 'edits files: write_file /workspace/agyrun/hello.txt')
})

test('Antigravity write alone is Build with file edit evidence', () => {
  assert.deepEqual(m.phaseOfTools([tool('write_file', 'hello.txt')]), {
    phase: 'build', why: 'edits files: write_file hello.txt',
  })
})

test('Antigravity viewer and search calls are Spec', () => {
  assert.equal(m.phaseOfTools([tool('view_file', 'calc.py'), tool('grep_search', 'beta')]).phase, 'spec')
})

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

test('a missing or shell parent leaves the child alone', () => {
  const child = pane('%2', { parent: '%1', state: 'Working' })
  for (const panes of [[child], [pane('%1', { agent: 'shell' }), child]]) {
    const result = floor(panes)
    assert.deepEqual(members(byId(result, '%2')), [['%2', 'worker']])
    assert.deepEqual(visiblePanes(result), ['%2'])
  }
})

test('direct item links win over parent links for every item', () => {
  const result = floor([
    pane('%30', { pane_name: 'B12 root', state: 'Working' }),
    pane('%2', { pane_name: 'Factory View', parent: '%30', state: 'Working' }),
    pane('%3', { brief: { name: 'B20 worker' }, parent: '%30', state: 'Working' }),
    pane('%5', { parent: '%30', state: 'Working' }),
  ])
  assert.deepEqual(members(byId(result, 'B191')), [['%2', 'worker']])
  assert.deepEqual(members(byId(result, 'B20')), [['%3', 'worker']])
  assert.deepEqual(members(byId(result, 'B12')), [['%30', 'main'], ['%5', 'worker']])
  assert.deepEqual(visiblePanes(result), ['%2', '%3', '%30', '%5'])

  const started = floor([pane('%1', { state: 'Working' }), pane('%4', { parent: '%1', state: 'Working' })])
  assert.deepEqual(members(byId(started, 'B191')), [['%1', 'main'], ['%4', 'worker']], "an item's Doing tab that started a worker is main")

  const doing = floor([pane('%1', { parent: '%2' }), pane('%2', { pane_name: 'B12 root' })])
  assert.deepEqual(members(byId(doing, 'B191')), [['%1', 'worker']])
  assert.deepEqual(members(byId(doing, 'B12')), [['%2', 'worker']])

  const repeated = floor([pane('%3', { pane_name: 'B12 work', brief: { name: 'B20 work' } })])
  assert.deepEqual(members(byId(repeated, 'B12')), [['%3', 'worker']])
  assert.equal(byId(repeated, 'B20').phase, 'inbox')
})

test('a different backlog ref keeps a child and its descendants separate', () => {
  for (const name of [{ pane_name: 'B15 follow-up' }, { brief: { name: 'B16 follow-up' } }]) {
    const result = floor([
      pane('%30', { pane_name: 'B12 root', state: 'Working' }),
      pane('%3', { parent: '%2', state: 'Working' }),
      pane('%2', { ...name, parent: '%30', state: 'Working' }),
    ])
    assert.deepEqual(members(byId(result, 'B12')), [['%30', 'worker']])
    assert.deepEqual(members(byId(result, '%2')), [['%2', 'main'], ['%3', 'worker']])
  }

  const same = floor([
    pane('%30', { pane_name: 'B15 follow-up' }),
    pane('%2', { brief: { name: 'B15 helper' }, parent: '%30', state: 'Working' }),
  ])
  assert.deepEqual(members(byId(same, '%30')), [['%30', 'main'], ['%2', 'worker']])
})

test('an item includes all descendants of its linked Claude and Codex tabs', () => {
  const result = floor([
    pane('%6', { pane_name: 'Advisor', parent: '%4', role: 'advisor' }),
    pane('%5', { pane_name: 'Claude grandchild', parent: '%4', state: 'Working' }),
    pane('%4', { agent: 'codex', pane_name: 'Codex child', parent: '%2', reviewers: ['%7'] }),
    pane('%2', { pane_name: 'Factory View' }),
    pane('%7', { pane_name: 'Reviewer', parent: '%8' }),
    pane('%8', { pane_name: 'Elsewhere', state: 'Working' }),
  ])
  const task = byId(result, 'B191')
  assert.deepEqual(members(task).sort(), [['%2', 'main'], ['%4', 'worker'], ['%5', 'worker'], ['%6', 'advisor'], ['%7', 'reviewer']])
  assert.match(task.why, /Codex child was started by Factory View/)
  assert.match(task.why, /Claude grandchild was started by Codex child/)
  assert.match(task.why, /Reviewer reviews Codex child/)
  assert.deepEqual(visiblePanes(result), ['%2', '%4', '%5', '%6', '%7', '%8'])
})

test('an unclaimed Antigravity root groups its Claude workers, advisors and reviewers', () => {
  const result = floor([
    pane('%31', { pane_name: 'Claude child', parent: '%30', reviewers_paused: ['%33'], state: 'Working' }),
    pane('%30', { pane_name: 'Antigravity root', agent: 'antigravity' }),
    pane('%32', { parent: '%31', pane_name: 'Advisor', role: 'advisor' }),
    pane('%33', { pane_name: 'Reviewer' }),
  ])
  const task = byId(result, '%30')
  assert.deepEqual(members(task), [['%30', 'main'], ['%31', 'worker'], ['%32', 'advisor'], ['%33', 'reviewer']])
  assert.match(task.why, /linked: Claude child was started by Antigravity root/)
  assert.match(task.why, /Advisor was started by Claude child/)
  assert.match(task.why, /Reviewer reviews Claude child/)
  assert.deepEqual(visiblePanes(result), ['%30', '%31', '%32', '%33'])
})

test('review-it follows the reviewed tab and gives each pane one task', () => {
  const caller = pane('%30', { pane_name: 'Caller', state: 'Working' })
  const reviewed = pane('%31', { pane_name: 'New worker', parent: '%30', reviewers: ['%30'] })
  const standalone = floor([caller, reviewed])
  assert.deepEqual(members(byId(standalone, '%31')), [['%31', 'worker'], ['%30', 'reviewer']])
  assert.deepEqual(visiblePanes(standalone), ['%30', '%31'])

  const linked = floor([{ ...caller, id: '%1' }, { ...reviewed, parent: '%1', reviewers: ['%1'], pane_name: 'B20 work' }])
  assert.deepEqual(members(byId(linked, 'B20')), [['%31', 'worker'], ['%1', 'reviewer']])
  assert.equal(byId(linked, 'B191').phase, 'inbox')
  assert.deepEqual(visiblePanes(linked), ['%1', '%31'])

  const inherited = floor([{ ...caller, id: '%1' }, { ...reviewed, parent: '%1', reviewers: ['%1'] }])
  assert.deepEqual(members(byId(inherited, 'B191')), [['%1', 'reviewer'], ['%31', 'worker']])

  const repeated = floor([caller, { ...caller, tmux_session: '$2' }, reviewed])
  assert.deepEqual(visiblePanes(repeated), ['%30', '%31'])
})

test('a parent cycle shows each tab once', () => {
  const a = pane('%30', { parent: '%31', state: 'Working' })
  const b = pane('%31', { parent: '%30', state: 'Working' })
  const child = pane('%32', { parent: '%31', state: 'Working' })
  for (const panes of [[a, b, child], [child, b, a]]) {
    const result = floor(panes)
    assert.equal(result.tasks.filter(task => task.id.startsWith('%')).length, 1)
    assert.deepEqual(visiblePanes(result), ['%30', '%31', '%32'])
  }
})

test('busy state, last activity and parking use all members', () => {
  const root = pane('%30', { finished: NOW / 1000 - 2 * 3600 })
  const child = pane('%31', { parent: '%30', state: 'Working' })
  const result = floor([root, child])
  assert.equal(byId(result, '%30').phase, 'build')
  assert.equal(byId(result, '%30').last, NOW)
  assert.equal(result.parked, 0)

  const recent = floor([{ ...root, finished: 0 }, { ...child, state: 'Idle', finished: NOW / 1000 - 3600 }])
  assert.equal(byId(recent, '%30').phase, 'done')
  assert.equal(byId(recent, '%30').last, NOW - 3600000)
  assert.equal(recent.parked, 0)

  const parked = floor([{ ...root, finished: 0 }, { ...child, state: 'Idle' }])
  assert.equal(byId(parked, '%30'), undefined)
  assert.equal(parked.parked, 1)
})

test('advisors never lead the phase even when their tools are newest', () => {
  const tools = { '%30': [tool('Edit', 'x', 10)], '%31': [tool('Bash', 'python3 scripts/deploy-ui.py /tmp/b ui/dist')] }
  const advisor = pane('%31', { parent: '%30', role: 'advisor', state: 'Working' })
  const result = floor([pane('%30'), advisor], tools)
  assert.deepEqual(members(byId(result, '%30')), [['%30', 'worker'], ['%31', 'advisor']])
  assert.equal(byId(result, '%30').phase, 'build')
  assert.equal(byId(floor([advisor], tools), '%31').phase, 'deploy', 'an advisor alone reads its own steps')
})

// Report only evidence this screen has for a phase, and keep it until the server confirms it.
{
  const history = { B1: [{ phase: 'build', at: 1000 }, { phase: 'verify', at: 2000 }], B2: [{ phase: 'done', at: 3000 }] }
  const build = { id: 'B1', phase: 'build' }, verify = { ...build, phase: 'verify' }
  const spec = { id: '%4', phase: 'spec' }, deploy = { id: 'B2', phase: 'deploy' }
  const buildMove = { task: 'B1', phase: 'build' }
  const seen = new Set(['B1', 'B2'])
  const moves = (now, previous, observed, tasks, pending = new Map()) =>
    m.historyMoves(history, now, previous, observed, tasks, pending)

  assert.deepEqual(moves(4000, null, new Set(), [build, spec]).moves, [{ task: '%4', phase: 'spec' }],
    'first observation reports the missing task and leaves the recent entry alone')
  assert.deepEqual(moves(4000, new Map([['B1', 'verify'], ['B2', 'deploy']]), seen, [build, deploy]).moves, [buildMove],
    'own move reports the changed task and leaves the unchanged task alone')
  assert.deepEqual(moves(62001, new Map(), new Set(), [build, deploy]).moves, [buildMove],
    'catch-up reports an old entry for a task new to this screen')
  assert.deepEqual(moves(62000, null, new Set(), [build]).moves, [],
    'catch-up waits until the last entry is older than 60 seconds')
  assert.deepEqual(moves(62001, new Map(), seen, [build]).moves, [],
    'catch-up cannot run when a previously seen task returns to the floor')

  const pending = new Map([['B1', 'build']])
  const retry = moves(5000, new Map([['B1', 'build']]), seen, [build], pending)
  assert.deepEqual(retry, { moves: [buildMove], pending }, 'an unchanged floor retries a pending move after failure')
  const saved = moves(5000, new Map([['B1', 'verify']]), seen, [verify], new Map([['B1', 'verify']]))
  assert.deepEqual(saved, { moves: [], pending: new Map() }, 'the server confirms the pending phase')
  assert.deepEqual(moves(5000, new Map([['B1', 'build']]), seen, [build], saved.pending).moves, [],
    'a later disagreement cannot revive a confirmed report')
  const review = { ...build, phase: 'review' }
  assert.deepEqual(moves(5000, new Map([['B1', 'build']]), seen, [review], pending),
    { moves: [{ task: 'B1', phase: 'review' }], pending: new Map([['B1', 'review']]) },
    'a newer own move replaces a pending move')
  assert.deepEqual(moves(5000, new Map([['B1', 'build']]), seen, [], pending).pending, new Map(),
    'a task that leaves the floor loses its pending move')
  assert.deepEqual(pending, new Map([['B1', 'build']]), 'the decision leaves its input pending map intact')
  assert.equal(m.phaseSince(history, verify), 2000)
  assert.equal(m.phaseSince(history, build), 0)
}

// A second screen arrives just after the first screen reports a move and never gets a fresh backlog.
{
  const history = { B12: [{ phase: 'build', at: NOW - 600000 }] }
  const screen = phase => ({ tasks: [{ id: 'B12', phase }], previous: null, seen: new Set(), pending: new Map() })
  const fresh = screen('done'), stale = screen('build')
  for (let poll = 0; poll < 25; poll++) {
    for (const view of [fresh, stale]) {
      const now = NOW + poll * 5000
      const { moves, pending } = m.historyMoves(history, now, view.previous, view.seen, view.tasks, view.pending)
      for (const { task, phase } of moves) history[task].push({ phase, at: now })
      view.pending = pending
      view.previous = new Map(view.tasks.map(task => [task.id, task.phase]))
      for (const task of view.tasks) view.seen.add(task.id)
    }
  }
  assert.deepEqual(history.B12.slice(1), [{ phase: 'done', at: NOW }],
    '25 Factory polls add exactly one Done entry despite a stale second screen')
}

// The worktree sweep (B210): each finding goes to its tab's agent and task; a tab with none has an empty list.
{
  const lines = [
    { pane: '%2', name: 'Factory View', branch: 'factory-view', kind: 'behind', text: '4 commits behind main. Rebase by hand.' },
    { pane: '%2', name: 'Factory View', branch: 'factory-view', kind: 'idle', text: 'Idle for 3 h.' },
    { pane: '%9', name: 'Closed', branch: 'x', kind: 'checks', text: '1 check fails on abc1234: test.' },
  ]
  const result = floor([pane('%1', { pane_name: 'Automation' }), pane('%2', { pane_name: 'Factory View', state: 'Working' })], {}, { sweep: { at: NOW / 1000, every: 1800, lines } })
  const task = result.tasks.find(entry => entry.agents.some(agent => agent.pane.id === '%2'))
  assert.deepEqual(task.sweep.map(line => line.kind), ['behind', 'idle'])
  assert.deepEqual(task.agents.find(agent => agent.pane.id === '%2').sweep.length, 2)
  assert.ok(result.tasks.filter(entry => entry !== task).every(entry => entry.sweep.length === 0))
  assert.ok(floor([pane('%2')]).tasks.every(entry => entry.sweep.length === 0))   // an older server sends no sweep
  assert.deepEqual(Object.keys(m.SWEEP_NAMES), ['behind', 'checks', 'idle'])
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
