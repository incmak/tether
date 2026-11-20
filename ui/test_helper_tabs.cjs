// Run: node test_helper_tabs.cjs — the helper tab filter, folds and nesting (src/helperTabs.ts, B400).
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/helperTabs.ts', 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
const loaded = {}
runInNewContext(code, { exports: loaded, require: name => { assert.equal(name, 'react'); return { createContext: value => ({ value }) } } })
const { needsMak, alwaysShown, withShown, hidesHelper, foldTarget, helperView, nestHelpers, nestDepth, helperCount } = loaded

const tab = (id, more = {}) => ({ id, tmux_session: '$1', dir: '/w', thread: `t${id}`, state: 'Idle', agent: 'claude', ...more })
const on = { hideHelpers: true, shownHelpers: {} }
const plain = value => JSON.parse(JSON.stringify(value))

// A helper hides only while the setting is on, it is idle, not open, and not always shown.
const owner = tab('%1'), reviewer = tab('%2', { helper: true, owner: '%1' })
assert.equal(hidesHelper(reviewer, on, '%1'), true)
assert.equal(hidesHelper(reviewer, { ...on, hideHelpers: false }, '%1'), false, 'setting off shows every tab')
assert.equal(hidesHelper(owner, on, null), false, 'a tab without helper never hides')
assert.equal(hidesHelper(reviewer, on, '%2'), false, 'the open tab stays shown')

// It shows again while it needs you: a prompt, an ask (Needs you: yes, Urgent) or an escalation.
for (const more of [{ state: 'Approval' }, { state: 'Input needed' }, { ask: { id: 'a1' } }, { escalated: true }]) {
  assert.equal(needsMak(tab('%3', more)), true, JSON.stringify(more))
  assert.equal(hidesHelper(tab('%3', { helper: true, ...more }), on, null), false, JSON.stringify(more))
}
assert.equal(needsMak(tab('%3', { state: 'Working' })), false)

// "Always show this tab" binds the pane id to its conversation and folder, so a reused pane id does not inherit it.
const shown = withShown({}, reviewer, true, new Set(['%2']))
assert.deepEqual(plain(shown), { '%2': { thread: 't%2', dir: '/w' } })
assert.equal(hidesHelper(reviewer, { ...on, shownHelpers: shown }, null), false)
assert.equal(alwaysShown({ ...reviewer, thread: 'other' }, shown), false, 'a new conversation in a reused pane id')
assert.equal(alwaysShown({ ...reviewer, dir: '/elsewhere' }, shown), false)
assert.deepEqual(plain(withShown(shown, reviewer, false, new Set(['%2']))), {}, 'turning it off removes the entry')
assert.deepEqual(plain(withShown({ '%9': { thread: 'x', dir: '/w' } }, reviewer, true, new Set(['%2']))), { '%2': { thread: 't%2', dir: '/w' } }, 'closed tabs drop out')

// A hidden helper folds under its owner, or under the first shown tab up the owner chain.
const worker = tab('%4', { helper: true, owner: '%1' }), sub = tab('%5', { helper: true, owner: '%4' })
const byId = new Map([owner, worker, sub].map(item => [item.id, item]))
assert.equal(foldTarget(sub, byId, new Set(['%1'])), '%1')
assert.equal(foldTarget(sub, byId, new Set(['%1', '%4'])), '%4')
assert.equal(foldTarget(tab('%6', { owner: '%6' }), byId, new Set()), undefined, 'a loop ends')

// helperView: hidden ids and folds by session; an opened fold shows its helpers; the open tab stays out of the fold.
let view = helperView([owner, reviewer, worker, sub], on, '%1', () => false, () => false)
assert.deepEqual([...view.hidden].sort(), ['%2', '%4', '%5'])
assert.deepEqual(plain(view.bySession.get('$1')), [{ key: '%1', open: false, panes: [reviewer, worker, sub].map(plain) }])
view = helperView([owner, reviewer, worker, sub], on, '%1', () => false, key => key === '%1')
assert.deepEqual([...view.hidden], [])
assert.equal(view.bySession.get('$1')[0].open, true)
view = helperView([owner, reviewer, worker, sub], on, '%4', () => false, () => false)
assert.deepEqual([...view.hidden].sort(), ['%2', '%5'])
assert.deepEqual(plain(view.bySession.get('$1').map(fold => fold.key).sort()), ['%1', '%4'], 'the open helper keeps its own helpers')

// Archived tabs stay with the archive; a helper whose owner is gone folds under its session.
view = helperView([owner, reviewer], on, null, item => item.id === '%2', () => false)
assert.deepEqual([...view.hidden], [])
const orphan = tab('%7', { helper: true })
view = helperView([owner, orphan], on, null, () => false, () => false)
assert.deepEqual(plain(view.bySession.get('$1').map(fold => fold.key)), ['@$1'])

// A session never loses all its tabs to the filter.
view = helperView([tab('%8', { helper: true, tmux_session: '$2' })], on, null, () => false, () => false)
assert.equal(view.hidden.size, 0)

// A stack never loses all its tabs: a stack with only a hidden helper keeps it shown (it would have no fold to draw).
view = helperView([owner, tab('%10', { helper: true, group: 'g2' })], on, null, () => false, () => false)
assert.equal(view.hidden.size, 0)
view = helperView([owner, tab('%11', { group: 'g2' }), tab('%10', { helper: true, group: 'g2' })], on, null, () => false, () => false)
assert.deepEqual([...view.hidden], ['%10'])

// An owner in another session: the helper folds under its own session, where the fold can be drawn.
view = helperView([owner, tab('%12', { tmux_session: '$3' }), tab('%13', { helper: true, owner: '%1', tmux_session: '$3' })], on, null, () => false, () => false)
assert.deepEqual(plain(view.bySession.get('$3').map(fold => fold.key)), ['@$3'])
assert.equal(view.bySession.get('$1'), undefined)

// Nesting: helpers right after their owner, depth first, whatever the tab order; the rest keep their order.
const order = [tab('%5', { owner: '%4' }), tab('%9'), tab('%4', { owner: '%1' }), tab('%1'), tab('%2', { owner: '%1' })]
assert.deepEqual(plain(nestHelpers(order).map(item => item.id)), ['%9', '%1', '%4', '%5', '%2'])
const loop = [tab('%a', { owner: '%b' }), tab('%b', { owner: '%a' })]
assert.deepEqual(plain(nestHelpers(loop).map(item => item.id)), ['%a', '%b'], 'an owner loop keeps every tab')
const listed = new Map(order.map(item => [item.id, item]))
assert.equal(nestDepth(listed.get('%5'), listed), 2)
assert.equal(nestDepth(listed.get('%9'), listed), 0)
assert.equal(helperCount(1), '1 helper')
assert.equal(helperCount(3), '3 helpers')

console.log('helper tabs: ok')
