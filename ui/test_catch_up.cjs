const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const compilerOptions = { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS }
const transpile = code => ts.transpileModule(code, { compilerOptions }).outputText
// Load the real Catch up rules (src/catchUp.ts) and the modules they use. No component runs.
function load(file) {
  const exports = {}
  const deps = { './paneState': 'src/paneState.ts', './groups': 'src/groups.ts' }
  runInNewContext(transpile(readFileSync(file, 'utf8')), { exports, localStorage: { getItem: () => null, setItem: () => {} }, require: name => deps[name] ? load(deps[name]) : {} })
  return exports
}
const c = load('src/catchUp.ts')
const now = 10_000
const ask = (since, extra = {}) => ({ pane: '', thread: null, since, next: 'Pick one', kind: 'reply', opened: false, snoozed_until: null, ...extra })
const pane = (id, session, extra = {}) => ({ id, tmux_session: session, label: id, agent: 'claude', state: 'Idle', ...extra })
const ids = list => Array.from(list, item => item.id)
const plain = value => JSON.parse(JSON.stringify(value))

// 1. What waits on you: an ask (not snoozed), or an approval without an ask. Shells never wait.
assert.equal(c.waitsOnYou(pane('%1', '$1', { ask: ask(100) }), now, true), true)
assert.equal(c.waitsOnYou(pane('%1', '$1', { ask: ask(100, { snoozed_until: now + 60 }), state: 'Approval' }), now, true), false, 'a snoozed ask waits later, even on an approval')
assert.equal(c.waitsOnYou(pane('%1', '$1', { ask: ask(100, { snoozed_until: now - 60 }) }), now, true), true, 'an ended snooze waits again')
assert.equal(c.waitsOnYou(pane('%1', '$1', { state: 'Approval' }), now, true), true)
assert.equal(c.waitsOnYou(pane('%1', '$1', { state: 'Input needed' }), now, false), true)
assert.equal(c.waitsOnYou(pane('%1', '$1', { ask: ask(100) }), now, false), false, 'with Attention mode off, only the state counts')
assert.equal(c.waitsOnYou(pane('%1', '$1', { agent: 'shell', state: 'Approval' }), now, true), false)

// 2. Goals: a stack, or the session for tabs in no stack. Oldest goal first, oldest tab first.
const stacks = [{ id: 'g1', name: 'Release', project: '' }, { id: 'g2', name: 'Quiet stack', project: '' }]
const sessions = [
  { id: '$1', name: 'Tether', groups: stacks, panes: [
    pane('%1', '$1', { group: 'g1', ask: ask(500) }), pane('%2', '$1', { group: 'g1', ask: ask(300) }),
    pane('%3', '$1', { ask: ask(900) }), pane('%4', '$1', { group: 'g2' }), pane('%5', '$1', { group: 'gone', state: 'Approval', since: 50 })] },
  { id: '$2', name: 'BriskBot', panes: [pane('%6', '$2', { ask: ask(700) }), pane('%7', '$2', { state: 'Working' })] },
]
const goals = c.catchUpGoals(sessions, now, true)
assert.deepEqual(plain(goals.map(goal => goal.name)), ['Tether', 'Release', 'BriskBot'], 'the oldest goal first; a tab in a removed stack counts as no stack')
assert.deepEqual(ids(goals[0].panes), ['%5', '%3'])
assert.deepEqual(ids(goals[1].panes), ['%2', '%1'], 'the oldest tab first in a goal')
assert.equal(goals[1].stack, true); assert.equal(goals[1].session, 'Tether'); assert.equal(goals[0].stack, false)
assert.deepEqual(plain(c.catchUpGoals([{ id: '$3', name: 'Calm', panes: [pane('%9', '$3')] }], now, true)), [], 'nothing waits: no goals')

// 3. Finished since you looked: not seen, not waiting, not open. Newest first.
const done = [{ id: '$1', name: 'A', panes: [
  pane('%1', '$1', { finished: 200 }), pane('%2', '$1', { finished: 400 }), pane('%3', '$1', { finished: 300 }),
  pane('%4', '$1', { finished: 500, ask: ask(500) }), pane('%5', '$1', { finished: 600 })] }]
assert.deepEqual(ids(c.finishedPanes(done, { '%3': 300 }, '%5', now, true)), ['%2', '%1'], 'seen, waiting and open tabs leave the list')

// 4. Quiet: nothing for you. A working, waiting, unread, background or open tab is not quiet.
const seen = { '%2': 900 }
assert.equal(c.isQuiet(pane('%1', '$1'), seen, undefined, now, true), true)
assert.equal(c.isQuiet(pane('%2', '$1', { finished: 800 }), seen, undefined, now, true), true, 'a read turn is quiet')
assert.equal(c.isQuiet(pane('%3', '$1', { finished: 800 }), seen, undefined, now, true), false, 'an unread turn is not quiet')
assert.equal(c.isQuiet(pane('%1', '$1', { state: 'Working' }), seen, undefined, now, true), false)
assert.equal(c.isQuiet(pane('%1', '$1', { tasks: ['1 shell'] }), seen, undefined, now, true), false)
assert.equal(c.isQuiet(pane('%1', '$1', { ask: ask(100) }), seen, undefined, now, true), false)
assert.equal(c.isQuiet(pane('%1', '$1', { escalated: true }), seen, undefined, now, true), false, 'an escalated helper waits even in Idle')
assert.equal(c.isQuiet(pane('%1', '$1', { ask: ask(100, { snoozed_until: now + 60 }) }), seen, undefined, now, true), true, 'a snoozed ask is quiet until it wakes')
assert.equal(c.isQuiet(pane('%1', '$1'), seen, '%1', now, true), false, 'the open tab always shows')

// 5. The counts on rows.
assert.equal(c.waitCount(sessions.flatMap(session => session.panes), now, true), 5)
assert.equal(c.waitCount(sessions[1].panes, now, true), 1)
console.log('test_catch_up: ok')
