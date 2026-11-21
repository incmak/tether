const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const compilerOptions = { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS }
const transpile = code => ts.transpileModule(code, { compilerOptions }).outputText
// B335: load the real Later rules (src/attention.ts, src/catchUp.ts). No component runs.
function load(file) {
  const exports = {}
  const deps = { './paneState': 'src/paneState.ts', './groups': 'src/groups.ts' }
  runInNewContext(transpile(readFileSync(file, 'utf8')), { exports, localStorage: { getItem: () => null, setItem: () => {} }, require: name => deps[name] ? load(deps[name]) : name === 'react' ? { useEffect() {}, useState() { return [] } } : {} })
  return exports
}
const a = load('src/attention.ts')
const c = load('src/catchUp.ts')
const now = 100_000
const ask = (extra = {}) => ({ pane: '', thread: null, since: 10, asked_at: 10, id: 'a1b2c3d4e5f6', next: 'Pick one', kind: 'reply', opened: true, snoozed_until: null, later: null, ...extra })
const pane = (id, extra = {}) => ({ id, tmux_session: '$1', label: id, agent: 'claude', state: 'Idle', thread: 't-' + id, ...extra })
const ids = list => Array.from(list, item => item.pane.id)

// 1. An ask in Later leaves the queue, the deck and Catch up. It does not expire.
const later = pane('%2', { ask: ask({ id: 'bbbbbbbbbbbb', later: { at: now - 60, by: 'rule', entry: 'rule-move' } }) })
const older = pane('%3', { ask: ask({ id: 'cccccccccccc', later: { at: now - 7200, by: 'you' } }) })
const open = pane('%1', { ask: ask() })
assert.deepEqual(ids(a.waitingAsks([later, open, older], now)), ['%1'])
assert.equal(a.visibleAsk(later, now), null, 'a Later ask is not the visible ask')
assert.equal(c.waitsOnYou(later, now, true), false, 'a Later ask does not wait on you')
assert.equal(c.waitsOnYou(open, now, true), true)
assert.deepEqual(ids(a.waitingAsks([later], now + 365 * 86400)), [], 'Later never expires')

// 2. laterAsks: only Later asks, newest move first, never shells.
assert.deepEqual(ids(a.laterAsks([older, open, later, pane('%4', { agent: 'shell', ask: ask({ later: { at: now, by: 'you' } }) })])), ['%2', '%3'])

// 3. askAction sends the occurrence id the page saw, and nothing when the ask has none.
const calls = []
const api = (path, body) => { calls.push([path, JSON.parse(JSON.stringify(body))]); return Promise.resolve({ ask: null, waiting: 0 }) }
a.askAction(api, later, 'back', later.ask)
a.askAction(api, older, 'back', older.ask)
a.askAction(api, later, 'handled', later.ask)
a.askAction(api, open, 'opened', open.ask)
a.askAction(api, open, 'later', ask({ id: undefined }))
a.askAction(api, open, 'handled')
assert.deepEqual(calls, [
  ['attention/back', { session: '%2', thread: 't-%2', ask: 'bbbbbbbbbbbb', entry: 'rule-move' }],
  ['attention/back', { session: '%3', thread: 't-%3', ask: 'cccccccccccc' }],
  ['attention/handled', { session: '%2', thread: 't-%2', ask: 'bbbbbbbbbbbb' }],
  ['attention/opened', { session: '%1', thread: 't-%1', ask: 'a1b2c3d4e5f6' }],
  ['attention/later', { session: '%1', thread: 't-%1' }],
  ['attention/handled', { session: '%1', thread: 't-%1' }],
])

// 4. Today starts at the server's midnight, including rows at that exact second.
const rows = [{ id: 'new', at: now + 60 }, { id: 'midnight', at: now }, { id: 'old', at: now - 1 }]
for (const [start, today, older] of [
  [now, ['new', 'midnight'], ['old']],
  [now + 1, ['new'], ['midnight', 'old']],
]) {
  const split = a.splitToday(rows, start)
  assert.deepEqual(Array.from(split.today, row => row.id), today)
  assert.deepEqual(Array.from(split.older, row => row.id), older)
}
console.log('ok attention later')
