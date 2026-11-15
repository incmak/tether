// The one search of the sidebar and the ⌘K menu (search.ts): fields, fuzzy words, sessions and tabs, speed.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const ts = require('typescript')

// Compile in this realm, as in the browser: arrays from another vm realm make loops slow and the speed check wrong.
const search = new module.constructor()
search._compile(ts.transpileModule(readFileSync('src/search.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, 'search.js')
const output = { exports: search.exports }
const { field, matchSessions, scoreWords, searchWords, wordScore } = output.exports
const plain = map => Object.fromEntries([...map].map(([id, match]) => [id, match.tabs === 'all' ? 'all' : [...match.tabs]]))
const find = query => plain(matchSessions(entries, searchWords(query)))

// Sessions: name (2) and folder with custom group (1). Tabs: title (2) and agent, stack, worktree (1).
const session = (name, dir, group) => [field(2, name), field(1, dir, group)]
const tab = (title, agent, stack, worktree) => [field(2, title), field(1, agent, stack, worktree)]
const entries = [
  { id: 'tether', fields: session('Tether', '/srv/apps/tether', 'Work'), tabs: [
    { id: '%1', fields: tab('Voice mode latency', 'Claude', 'Voice stack') },
    { id: '%2', fields: tab('Backlog search', 'Codex', null, 'b77-search') },
  ] },
  { id: 'blender', fields: session('Blender', '/srv/blender'), tabs: [{ id: '%3', fields: tab('Render', 'Claude') }] },
]

// A tab title finds its session, and only that tab shows. Case does not matter.
assert.deepEqual(find('voice'), { tether: ['%1'] })
assert.deepEqual(find('VOICE'), { tether: ['%1'] })
// A session name or its custom group shows every tab.
assert.deepEqual(find('tether'), { tether: 'all' })
assert.deepEqual(find('work'), { tether: 'all' })
// Words in any order, split over session and tab; stacks and worktrees match.
assert.deepEqual(find('latency tether'), { tether: ['%1'] })
assert.deepEqual(find('voice stack'), { tether: ['%1'] })
assert.deepEqual(find('b77'), { tether: ['%2'] })
// An agent name matches the tabs it runs in.
assert.deepEqual(find('claude'), { tether: ['%1'], blender: 'all' })
// Every word must match.
assert.deepEqual(find('voice render'), {})
assert.deepEqual([...searchWords('  a   B ')], ['a', 'b'])

// Fuzzy: initials, close letters from a word start, one typo.
assert.deepEqual(find('vml'), { tether: ['%1'] })
assert.deepEqual(find('bklg'), { tether: ['%2'] })
assert.deepEqual(find('vioce'), { tether: ['%1'] })
assert.deepEqual(find('lateny'), { tether: ['%1'] })
assert.deepEqual(find('blendr'), { blender: 'all' })
// A typo in the first letter, or an extra letter before it, still counts.
assert.equal(wordScore(field(1, 'Voice mode'), 'boice'), 1)
assert.equal(wordScore(field(1, 'Voice mode'), 'xvoice'), 1)
assert.equal(wordScore(field(1, 'Voice mode'), 'ovice'), 1)
// A later match at a word start beats the first one inside a word.
assert.equal(wordScore(field(1, 'revoice voice'), 'voice'), 4)
assert.equal(wordScore(field(1, 'revoice'), 'voice'), 3)
// No match from letters scattered over a long text, and no typo match for short words.
assert.equal(wordScore(field(1, '/srv/projects/apps/mobile-tmux/server'), 'pass'), null)
assert.equal(wordScore(field(1, 'Render'), 'rxn'), null)
assert.equal(wordScore(field(1, 'Tether'), 'tez'), null)
// Ranking: start of the text > start of a word > inside a word > initials and close letters > typo.
const voice = field(1, 'Voice mode latency')
assert.deepEqual(['voice', 'mode', 'oice', 'vml', 'vce', 'vioce'].map(word => wordScore(voice, word)), [5, 4, 3, 3, 2, 1])
// A name counts double, so a title match ranks above a folder match.
assert.ok(scoreWords([field(2, 'Search')], ['search']) > scoreWords([field(1, '/srv/search')], ['search']))
const ranked = matchSessions(entries, searchWords('render'))
assert.equal(ranked.get('blender').tabScores.get('%3'), 10)

// Speed: 200 sessions with 10 tabs each. One search must take well under a frame.
const words = ['voice', 'mode', 'latency', 'backlog', 'render', 'review', 'deploy', 'search', 'tether', 'claude']
const big = Array.from({ length: 200 }, (_, s) => ({ id: `s${s}`, fields: session(`Session ${s} ${words[s % 10]}`, `/srv/projects/p${s}/apps`), tabs: Array.from({ length: 10 }, (_, t) => ({ id: `%${s}-${t}`, fields: tab(`Tab ${t} ${words[(s + t) % 10]} work`, 'Claude', `stack ${t}`, `b${s}-${t}`) })) }))
const queries = ['tab 3', 'vioce', 'zzzz', 'dply', 'vml', 'session 1 review', 'qqqq wwww']
for (const query of queries) for (let i = 0; i < 10; i++) matchSessions(big, searchWords(query))
// The median of 5 rounds per query: other work on the machine makes single rounds noisy.
const times = queries.map(query => {
  const rounds = Array.from({ length: 5 }, () => {
    const start = performance.now()
    for (let i = 0; i < 10; i++) matchSessions(big, searchWords(query))
    return (performance.now() - start) / 10
  }).sort((a, b) => a - b)
  return [query, rounds[2]]
})
const worst = Math.max(...times.map(([, ms]) => ms))
assert.ok(worst < 5, `a search over 2,000 tabs took ${worst.toFixed(2)} ms: ${JSON.stringify(times)}`)
console.log(`search ok: ${times.map(([query, ms]) => `"${query}" ${ms.toFixed(2)} ms`).join(', ')} (2,000 tabs, median of 5)`)
