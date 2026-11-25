// Run from ui/: node test_helper_tabs.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(name, dependencies = {}) {
  const code = ts.transpileModule(readFileSync(`src/${name}.ts`, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const exports = {}
  runInNewContext(code, { exports, require: name => { assert.ok(name in dependencies, name); return dependencies[name] } })
  return exports
}
const helpers = load('helperTabs')
const groups = load('groups', { './paneState': load('paneState') })
const { taskOf, taskChildren, taskState, canMoveInto, moveIntoTask } = load('taskTree', { './groups': groups, './helperTabs': helpers })
const tab = (id, more = {}) => ({ id, tmux_session: '$1', dir: '/w', thread: `t${id}`, state: 'Idle', agent: 'claude', ...more })

for (const more of [{ state: 'Approval' }, { state: 'Input needed' }, { ask: { next: 'Select a test' } }, { escalated: true }]) {
  assert.equal(helpers.needsMak(tab('%1', more)), true, JSON.stringify(more))
  assert.equal(taskState(tab('%2', { state: 'Working' }), [tab('%1', more)]), 'needs')
}
assert.equal(helpers.needsMak(tab('%1', { state: 'Working' })), false)
assert.equal(helpers.helperCount(1), '1 helper')
assert.equal(helpers.helperCount(4), '4 helpers')
assert.equal(helpers.fromLabel('B292 review'), 'B292')
assert.equal(helpers.fromLabel('B446'), 'B446')
assert.equal(helpers.fromLabel('B12: fix'), 'B12')
assert.equal(helpers.fromLabel('tether main'), 'tether main')
assert.equal(helpers.fromLabel('B12x tab'), 'B12x tab')
assert.equal(helpers.fromLabel('b12 tab'), 'b12 tab')
assert.equal(helpers.fromLabel('  B7 x'), 'B7')

const root = tab('%1', { group: 'g1' })
const child = tab('%2', { task: '%1', group: 'g2', task_role: 'Worker' })
const nested = tab('%3', { task: '%1', task_depth: 2, task_role: "Worker's review" })
const separate = tab('%4', { owner: '%1', helper: true, group: 'g1', from: { pane: '%1', name: 'Root', live: true } })
const elsewhere = tab('%5', { group: 'g2' })
const panes = [child, root, nested, separate, elsewhere]
const stacks = [{ id: 'g1', name: 'First' }, { id: 'g2', name: 'Second' }]
assert.equal(taskOf(child, panes), root)
assert.equal(taskOf(nested, panes), root)
assert.equal(taskOf(separate, panes), separate)
assert.deepEqual(Array.from(taskChildren(root, panes)), [child, nested])
assert.deepEqual(Array.from(groups.groupPanes(panes, stacks, child)), [child, root, nested, separate])
assert.deepEqual(Array.from(groups.groupPanes(panes, stacks, elsewhere)), [elsewhere])
assert.deepEqual(Array.from(groups.groupPanes(panes, stacks, tab('%9', { group: 'g2' }))), [elsewhere])
assert.equal(canMoveInto(child, separate, panes, stacks), true)
assert.equal(canMoveInto(child, root, panes, stacks), false, 'already in this task')
assert.equal(canMoveInto(child, elsewhere, panes, stacks), false, 'different stack')
assert.equal(canMoveInto(child, tab('%6', { group: 'g1', tmux_session: '$2' }), panes, stacks), false, 'different session')
assert.equal(canMoveInto(root, separate, panes, stacks), false, 'a task with helpers cannot become a child')
assert.equal(canMoveInto(separate, child, panes, stacks), false, 'a child cannot become a task target')
assert.equal(taskState({ ...root, done: true }), 'done')
assert.equal(taskState({ ...root, done: true }, [tab('%2', { escalated: true })]), 'needs')
const mockedApi = (name, body) => ({ name, body: JSON.parse(JSON.stringify(body)) })
assert.deepEqual(moveIntoTask(mockedApi, child, separate).body, { pane: '%2', thread: 't%2', into: '%4', into_thread: 't%4' })
assert.deepEqual(moveIntoTask(mockedApi, child, null).body, { pane: '%2', thread: 't%2', into: null, into_thread: null })
console.log('task tree model: ok')
