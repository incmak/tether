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

// Finding 5: every successful write confirms a value. Only the newest write changes the switch.
function testStaleRuleMixedResults() {
  const optimistic = { confirmed: true, shown: true }
  const olderSuccess = a.confirmStaleRule(optimistic, false, false)
  assert.deepEqual({ ...olderSuccess }, { confirmed: false, shown: true })
  assert.deepEqual({ ...a.confirmStaleRule(olderSuccess, null, true) }, { confirmed: false, shown: false })
  assert.deepEqual({ ...a.confirmStaleRule(olderSuccess, true, true) }, { confirmed: true, shown: true })

  const newerSuccess = a.confirmStaleRule({ confirmed: false, shown: false }, true, true)
  assert.deepEqual({ ...a.confirmStaleRule(newerSuccess, false, false) }, { confirmed: false, shown: true })
}
testStaleRuleMixedResults()

// Review 4: one write at a time across dialog mounts; only the newest result sets the error.
async function testStaleRuleQueue() {
  const q = new a.StaleRuleQueue()
  const sent = [], results = [], held = []
  let server = true
  const write = value => { sent.push(value); return new Promise((resolve, reject) => held.push({ value, resolve, reject })) }
  const read = async () => server
  q.listener = result => results.push(result)
  const first = q.set(false, write, read, () => 'Could not save. Try again.')
  q.listener = null                                  // Settings closes while the write runs
  const generation = q.generation
  q.set(false, write, read, () => 'x')
  q.set(true, write, read, () => 'x')                // reopened: two more choices
  assert.equal(q.busy, true)
  assert.equal(q.wanted, true)
  assert.notEqual(q.generation, generation)
  assert.deepEqual(sent, [false])                    // nothing overtakes the held write
  q.listener = result => results.push(result)
  held[0].reject(new Error('503'))
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(sent, [false, true])              // only the newest queued choice goes next
  server = true
  held[1].resolve(true)
  await first
  assert.deepEqual(results.map(r => ({ ...r })), [
    { confirmed: true, error: 'Could not save. Try again.', last: false },  // confirms only; the view ignores it
    { confirmed: true, error: '', last: true },                              // the newest write clears the error
  ])
  assert.equal(q.busy, false)
  assert.equal(q.wanted, null)

  // A failed newest write shows its error and the server value read after it.
  const q2 = new a.StaleRuleQueue(), seen = []
  q2.listener = result => seen.push({ ...result })
  await q2.set(false, async () => { throw new Error('503') }, async () => true, () => 'Could not save. Try again.')
  assert.deepEqual(seen, [{ confirmed: true, error: 'Could not save. Try again.', last: true }])
  // Review 5: a read that starts during a write never counts, even after the write ends.
  const q3 = new a.StaleRuleQueue()
  let release
  const writing = q3.set(false, () => new Promise(resolve => { release = resolve }), async () => true, () => 'x')
  const duringWrite = q3.startRead()
  release(false)
  await writing
  assert.equal(q3.confirmed, false)
  assert.equal(duringWrite(true), false)
  assert.equal(q3.confirmed, false)
  // Review 6: a failed read reports whether it is current and never changes the confirmed value.
  assert.equal(duringWrite(null), false)
  const failedIdle = q3.startRead()
  assert.equal(failedIdle(null), true)
  assert.equal(q3.confirmed, false)
  const idleRead = q3.startRead()
  assert.equal(idleRead(true), true)
  assert.equal(q3.confirmed, true)
  // Review 5: the confirmed value outlives the dialog, so a failed write and a failed read fall back to it.
  const q4 = new a.StaleRuleQueue(), last = []
  q4.startRead()(true)
  q4.listener = result => last.push({ ...result })
  await q4.set(false, async () => { throw new Error('503') }, async () => { throw new Error('503') }, () => 'Could not save. Try again.')
  assert.deepEqual(last, [{ confirmed: true, error: 'Could not save. Try again.', last: true }])
  assert.deepEqual({ ...a.confirmStaleRule({ confirmed: q4.confirmed, shown: false }, last[0].confirmed, true) }, { confirmed: true, shown: true })
  // A failed read keeps what the switch shows when nothing was confirmed yet.
  assert.deepEqual({ ...a.confirmStaleRule({ confirmed: null, shown: true }, null, true) }, { confirmed: null, shown: true })
}
testStaleRuleQueue().then(() => console.log('ok attention later'), error => { console.error(error); process.exitCode = 1 })
