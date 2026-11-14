// The sidebar search over sessions and tabs (sessionSearch.ts).
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const output = { exports: {} }
runInNewContext(ts.transpileModule(readFileSync('src/sessionSearch.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: output.exports })
const { matchSessions, searchText, searchWords } = output.exports
const plain = map => Object.fromEntries([...map].map(([id, tabs]) => [id, tabs === 'all' ? 'all' : [...tabs]]))

const entries = [
  { id: 'tether', text: searchText('Tether', '/srv/apps/tether', 'Work'), tabs: [
    { id: '%1', text: searchText('Voice mode latency', 'Claude', 'Voice stack') },
    { id: '%2', text: searchText('Backlog search', 'Codex', null, 'b77-search') },
  ] },
  { id: 'blender', text: searchText('Blender', '/srv/blender'), tabs: [{ id: '%3', text: searchText('Render', 'Claude') }] },
]

// A tab title finds its session, and only that tab shows.
assert.deepEqual(plain(matchSessions(entries, searchWords('voice'))), { tether: ['%1'] })
// Case does not matter.
assert.deepEqual(plain(matchSessions(entries, searchWords('VOICE'))), { tether: ['%1'] })
// A session name or its custom group shows every tab.
assert.deepEqual(plain(matchSessions(entries, searchWords('tether'))), { tether: 'all' })
assert.deepEqual(plain(matchSessions(entries, searchWords('work'))), { tether: 'all' })
// Words in any order, split over session and tab.
assert.deepEqual(plain(matchSessions(entries, searchWords('latency tether'))), { tether: ['%1'] })
// Stack name and worktree branch.
assert.deepEqual(plain(matchSessions(entries, searchWords('voice stack'))), { tether: ['%1'] })
assert.deepEqual(plain(matchSessions(entries, searchWords('b77'))), { tether: ['%2'] })
// The agent of every tab matches: the session shows whole.
assert.deepEqual(plain(matchSessions(entries, searchWords('claude'))), { tether: ['%1'], blender: 'all' })
// Every word must match.
assert.deepEqual(plain(matchSessions(entries, searchWords('voice render'))), {})
assert.deepEqual([...searchWords('  a   B ')], ['a', 'b'])

// Speed: 200 sessions with 10 tabs each, 1000 queries.
const big = Array.from({ length: 200 }, (_, s) => ({ id: `s${s}`, text: searchText(`Session ${s}`, `/srv/p${s}`), tabs: Array.from({ length: 10 }, (_, t) => ({ id: `%${s}-${t}`, text: searchText(`Tab ${t} of ${s}`, 'Claude') })) }))
const start = performance.now()
for (let i = 0; i < 1000; i++) matchSessions(big, searchWords(`tab ${i % 10}`))
const each = (performance.now() - start) / 1000
assert.ok(each < 5, `one search took ${each.toFixed(2)} ms`)
console.log(`session search ok (${each.toFixed(3)} ms per search over 2000 tabs)`)
