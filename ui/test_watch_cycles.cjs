// Quiet watch cycles. Run: node test_watch_cycles.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/watchCycles.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const moduleExports = {}
runInNewContext(code, { exports: moduleExports, require: () => ({}) })
const { foldWatchCycles } = moduleExports

const quiet = (id, watch = 'CI-card') => ({ id, role: 'assistant', kind: 'notice', quiet: 'watch', watch, text: 'Watch expired', time: id })
const rearm = (id, tool = {}) => ({ id, role: 'assistant', kind: 'actions', text: '', time: id, tools: [{ id: 'monitor', name: 'Monitor', status: 'complete', is_error: false, ...tool }] })
const reply = (id, text = 'The watch expired, so I started it again.\n\nNeeds you: no') => ({ id, role: 'assistant', text, time: id })
const user = { id: 'user', role: 'user', text: 'Next task', time: 'user' }

const run = [quiet('q1'), rearm('r1'), reply('a1'), quiet('q2'), rearm('r2'), reply('a2')]
const messages = [...run, user]
const snapshot = JSON.stringify(messages)
const folded = foldWatchCycles(messages)
assert.equal(folded.length, 2)
assert.deepEqual({ ...folded[0], members: [...folded[0].members], watches: [...folded[0].watches] }, {
  id: 'watch:q1', role: 'assistant', kind: 'watch', text: '', time: 'a2',
  members: run, watches: ['CI-card'], cycles: 2, rearmed: true,
})
assert.equal(folded[1], user)
assert.equal(JSON.stringify(messages), snapshot)
run.forEach((message, index) => assert.equal(folded[0].members[index], message))

const needsYou = reply('yes', 'Needs you: yes')
const needsResult = foldWatchCycles([run[0], run[1], needsYou])
assert.equal(needsResult[0].members.length, 2)
assert.equal(needsResult[1], needsYou)

const invalid = [
  { ...quiet('notice'), quiet: undefined },
  rearm('bash', { name: 'Bash' }),
  rearm('pending', { status: 'pending' }),
  rearm('error', { is_error: true }),
  { ...rearm('empty'), tools: [] },
  { ...rearm('missing'), tools: undefined },
  { ...rearm('mixed'), tools: [...rearm('monitor').tools, ...rearm('bash', { name: 'Bash' }).tools] },
]
assert.equal(foldWatchCycles(invalid), invalid)
for (const message of invalid) {
  const result = foldWatchCycles([quiet('q'), message])
  assert.equal(result.length, 2)
  assert.equal(result[0].members.length, 1)
  assert.equal(result[0].rearmed, false)
  assert.equal(result[1], message)
}

const twoWatches = foldWatchCycles([quiet('x', 'X'), quiet('y', 'Y'), rearm('r'), reply('a')])[0]
assert.deepEqual([...twoWatches.watches], ['X', 'Y'])
assert.equal(twoWatches.cycles, 2)

const plain = [rearm('r'), reply('a'), user]
assert.equal(foldWatchCycles(plain), plain)
const empty = []
assert.equal(foldWatchCycles(empty), empty)

for (const boundary of [reply('question', 'Restart it?'), reply('long', 'x'.repeat(401)), user]) {
  const result = foldWatchCycles([quiet('q'), boundary])
  assert.equal(result[0].members.length, 1)
  assert.equal(result[1], boundary)
}
for (const boundary of [reply('second'), rearm('second')]) {
  const result = foldWatchCycles([quiet('q'), reply('first'), boundary, quiet('next')])
  assert.equal(result.length, 3)
  assert.equal(result[0].members.length, 2)
  assert.equal(result[1], boundary)
  assert.equal(result[2].kind, 'watch')
}
assert.equal(foldWatchCycles([quiet('q'), reply('limit', '  ' + 'x'.repeat(400) + '  ')])[0].members.length, 2)
assert.equal(foldWatchCycles([quiet('q'), reply('case', 'NEEDS YOU:   YES')])[0].members.length, 1)
assert.deepEqual([...foldWatchCycles([quiet('x', 'X'), quiet('y', 'Y'), quiet('x2', 'X')])[0].watches], ['X', 'Y'])


// Scheduled runs (session cron jobs): any finished tools and short replies fold under the run's notice.
const fire = (id, watch = 'Inbox watcher', cron = 'c1') => ({ id, role: 'assistant', kind: 'notice', title: 'Scheduled run', quiet: 'schedule', watch, cron, text: watch, time: id })
const work = (id, tool = {}) => rearm(id, { name: 'Bash', ...tool })
const quietRun = [fire('s1'), work('w1'), reply('b1', 'Checking.'), work('w2', { name: 'Read' }), reply('b2', 'Nothing new.\n\nNeeds you: no')]
const scheduled = foldWatchCycles([...quietRun, fire('s2'), reply('b3', 'Nothing new.'), user])
assert.equal(scheduled.length, 2)
assert.equal(scheduled[0].kind, 'watch')
assert.equal(scheduled[0].schedule, true)
assert.equal(scheduled[0].reported, false)
assert.equal(scheduled[0].cycles, 2)
assert.equal(scheduled[0].members.length, 7)
assert.deepEqual([...scheduled[0].watches], ['Inbox watcher'])
assert.equal(scheduled[0].result, 'Nothing new.')
assert.equal(scheduled[1], user)

// A run that acted says so in its own words; it never merges into runs that found nothing, so its line shows it.
const acted = foldWatchCycles([...quietRun, fire('s2'), work('post', { name: 'mcp__teams__send' }), reply('b4', 'Posted the reply.\n\nNeeds you: no'), fire('s3'), reply('b5', 'Nothing new.')])
assert.deepEqual(acted.map(item => [item.cycles, item.result]), [[1, 'Nothing new.'], [1, 'Posted the reply.'], [1, 'Nothing new.']])
assert.ok(acted.every(item => item.kind === 'watch' && item.schedule))
// A run with no reply at all has no result to show.
assert.equal(foldWatchCycles([fire('s1'), work('w1'), user])[0].result, '')
// Two jobs never share one line.
assert.equal(foldWatchCycles([fire('s1'), reply('a', 'Nothing new.'), fire('s2', 'Deploy check', 'c2'), reply('b', 'Nothing new.')]).length, 2)
// Two jobs with the same label are still two jobs.
assert.equal(foldWatchCycles([fire('s1'), reply('a', 'Nothing new.'), fire('s2', 'Inbox watcher', 'c2'), reply('b', 'Nothing new.')]).length, 2)

// A run that reports something keeps the report outside the fold, and the fold does not claim the run was quiet.
const report = reply('long', 'Two new messages need an answer.\n\nNeeds you: yes')
const reported = foldWatchCycles([fire('s1'), work('w1'), report])
assert.equal(reported.length, 2)
assert.equal(reported[0].reported, true)
assert.equal(reported[0].members.length, 2)
assert.equal(reported[1], report)

// A run with nothing folded but its notice keeps the plain notice.
const bare = foldWatchCycles([fire('s1'), report])
assert.equal(bare.length, 2)
assert.equal(bare[0].kind, 'notice')

// A failed tool ends the fold; watch cycles and scheduled runs never share one fold.
const failed = work('bad', { is_error: true })
assert.equal(foldWatchCycles([fire('s1'), failed])[1], failed)
const mixed = foldWatchCycles([quiet('q1'), fire('s1'), reply('b1', 'Nothing new.')])
assert.equal(mixed.length, 2)
assert.equal(mixed[0].schedule, undefined)
assert.equal(mixed[1].schedule, true)
// A watch cycle still accepts only Monitor re-arms.
assert.equal(foldWatchCycles([quiet('q'), work('w')]).length, 2)

console.log('watch cycles: quiet runs and scheduled runs fold; boundaries, labels, identity, and input purity pass')
