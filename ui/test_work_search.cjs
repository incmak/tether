// The work board's search (work.ts workScore): the shared matcher from search.ts, with the ID rule. Run: node test_work_search.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const ts = require('typescript')

// Compile in this realm (module._compile), with stubs for the React hooks that work.ts also exports.
const stubs = { react: { useCallback: fn => fn }, './hooks': { usePolling: () => ({}) } }
const load = (file, extra = {}) => {
  const mod = new module.constructor(file)
  mod.require = name => extra[name] ?? stubs[name] ?? require(name)
  mod._compile(ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, file)
  return mod.exports
}
const search = load('src/search.ts')
const work = load('src/work.ts', { './search': search })
const words = search.searchWords

const item = (id, title, extra = {}) => ({ id, project: 'shop', title, stage: 'ready', qa: false, ask: null, askFrom: null, refs: [], tabs: [], history: [], source: '', created: 0, updated: 0, doneAt: null, jira: null, version: 1, ...extra })
const card = (id, title, extra = {}) => ({ id, project: 'Backlog', area: 'tether', title, rank: 1, stage: 'ready', source: 'claude', doing: null, done: null, doneAt: null, details: [], version: 1, ...extra })
const score = (entry, query) => work.workScore(entry, words(query))

const login = item('T-21', 'Login fails on Safari', {
  refs: ['B12'], tabs: [{ pane: '%1', role: 'fix', chat: '', name: 'Voice fix', session: 'tether', stack: 'Search' }],
  ask: { id: 'a', q: 'Should the retry keep the old token?', options: ['Keep it'], pane: '%1', chat: '', at: 0 },
  history: [{ at: 0, by: 'Mak', text: 'Moved to Ready after the teardown fix' }], jira: { tickets: ['SHOP-77'] },
})
const voice = card('B12', 'Voice mode latency', { details: ['the worker restarts after a timeout'] })

assert.equal(score(login, ''), 0, 'No query: everything shows')
assert.ok(score(login, 'lgin') !== null, 'One typo in the title')
assert.ok(score(voice, 'vml') !== null, 'Title initials')
assert.ok(score(login, 'shop safari') !== null, 'Project and title, any order')
assert.ok(score(login, 'voice fix') !== null, 'A linked tab name')
assert.ok(score(login, 'shop-77') !== null, 'A Jira ticket')
assert.ok(score(login, 'token') !== null, 'The question, whole word')
assert.equal(score(login, 'tokn'), null, 'The question gives no typo matches')
assert.ok(score(voice, 'timeout') !== null, 'A backlog note, whole word')
assert.equal(score(voice, 'timout'), null, 'A backlog note gives no typo matches')
assert.ok(score(voice, 'backlog') !== null, 'A backlog card matches "backlog"')
assert.ok(score(login, 'safari') > score(login, 'teardown'), 'A title match ranks above a history match')

assert.equal(score(login, 't-21'), 10, 'An ID alone finds that item')
assert.equal(score(item('T-210', 'Other'), 't-21'), null, 'not T-210')
assert.equal(score(voice, 'b-12'), 10, 'B-12 is B12')
assert.equal(score(card('B120', 'Other'), 'b12'), null, 'not B120')
assert.equal(score(login, 'b12'), 5, 'An item that links to the ID shows too, below it')
assert.ok(score(voice, 'b12 voice') !== null, 'An ID in a longer query is a plain word')

// Speed: 500 items and 500 cards, two typo words, after warm-up.
const many = [...Array.from({ length: 500 }, (_, i) => item(`T-${i}`, `Item ${i} about deploy logs and share tests`, { history: [{ at: 0, by: 'x', text: 'the worker restarts after a timeout' }] })),
  ...Array.from({ length: 500 }, (_, i) => card(`B${i}`, `Card ${i} about share tests`, { details: ['a long note about the deploy'] }))]
const times = []
for (let round = 0; round < 35; round++) {
  const start = performance.now(); const query = words('shrae tsets'); for (const entry of many) work.workScore(entry, query)
  if (round >= 20) times.push(performance.now() - start)
}
const median = times.sort((a, b) => a - b)[7]
assert.ok(median < 10, `work search took ${median.toFixed(1)} ms`)
console.log(`Work search: fuzzy fields, exact notes, IDs and ranking passed (${median.toFixed(1)} ms over 1,000 entries)`)
