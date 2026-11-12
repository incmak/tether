const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const exportsObject = {}
runInNewContext(ts.transpileModule(readFileSync('src/fridaySessions.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText, { exports: exportsObject })
// Cross-realm results become plain objects for assertions.
const list = (...args) => JSON.parse(JSON.stringify(exportsObject.fridayChatList(...args)))
const sessions = [{ id: '$9', name: 'Triage' }, { id: '$16', name: 'Admin Work' }]
const chats = [
  { session: '$9', chat: 'Review', agent: 'Claude', state: 'idle', busy: false },
  { session: '$9', chat: 'Fix', agent: 'Codex', state: 'working', busy: true },
  { session: '$16', chat: 'Mail', agent: 'Antigravity', state: 'needs you', busy: false },
]
const full = list(chats, sessions, 'Review')
assert.deepEqual(full, {
  target: 'Review',
  chats: ['Review, Claude, idle, in Triage', 'Fix, Codex, working, in Triage',
    'Mail, Antigravity, needs you, in Admin Work'],
  sessions: ['Triage: 2 chats, 1 busy', 'Admin Work: 1 chats, 0 busy'],
})
for (const wanted of ['Triage', 'triage', 'Treeage', 'tri-age', 'Tri']) {
  assert.deepEqual(list(chats, sessions, '', wanted), { session: 'Triage', chats: full.chats.slice(0, 2) })
}
for (const wanted of ['admin work', 'ADMIN WORK', 'admin-work', 'Work']) {
  assert.deepEqual(list(chats, sessions, '', wanted), { session: 'Admin Work', chats: full.chats.slice(2) })
}
for (const wanted of ['zzzzzzzzzz', '', '!!!']) {
  assert.deepEqual(list(chats, sessions, '', wanted), {
    error: `No session called ${wanted}. Sessions: Triage, Admin Work`,
  })
}
assert.deepEqual(list([], [], '', 'Missing'), { error: 'No session called Missing. Sessions: ' })
assert.deepEqual(list([], [], ''), { target: '', chats: [], sessions: [] })
assert.deepEqual(list(chats.slice(0, 1), [{ id: '$9', name: '' }], ''), {
  target: '', chats: ['Review, Claude, idle, in $9'], sessions: ['$9: 1 chats, 0 busy'],
})
assert.deepEqual(list(chats.slice(0, 1), [], '', '$9'), {
  session: '$9', chats: ['Review, Claude, idle, in $9'],
})
// Prefixes outrank substrings, then edit distance picks the closest candidate.
assert.equal(list([], [{ id: 'a', name: 'Preflight' }, { id: 'b', name: 'Flight' }], '', 'Flig').session, 'Flight')
assert.equal(list([], [{ id: 'a', name: 'Triage' }, { id: 'b', name: 'Storage' }], '', 'Treeage').session, 'Triage')
// Forty percent is inclusive; a larger distance must fail.
assert.equal(list([], [{ id: 'a', name: 'abcde' }], '', 'abxye').session, 'abcde')
assert.deepEqual(list([], [{ id: 'a', name: 'abcde' }], '', 'abxyz'), {
  error: 'No session called abxyz. Sessions: abcde',
})
const { soundKey } = exportsObject
assert.equal(soundKey('theatre'), soundKey('Tether'))
assert.notEqual(soundKey('Friday test'), soundKey('Tether'))
assert.equal(soundKey('Philly'), 'fl')
assert.equal(soundKey('Ckick'), 'k')
assert.equal(soundKey('Awhyy'), 'a')
assert.equal(soundKey('!!!'), '')
const spokenSessions = [
  { id: 't', name: 'Tether' }, { id: 'f', name: 'Friday test' }, { id: 'a', name: 'Admin Work' },
]
const spokenChats = [{ session: 't', chat: 'Fix', agent: 'Codex', state: 'idle', busy: false }]
for (const wanted of ['theatre', 'theater']) {
  assert.deepEqual(list(spokenChats, spokenSessions, '', wanted), {
    session: 'Tether', chats: ['Fix, Codex, idle, in Tether'],
  })
}
const ambiguous = [...spokenSessions, { id: 'other', name: 'Totoro' }]
assert.equal(soundKey('Totoro'), soundKey('theatre'))
assert.deepEqual(list([], ambiguous, '', 'theatre'), {
  error: 'No session called theatre. Sessions: Tether, Friday test, Admin Work, Totoro',
})
assert.equal(list([], ambiguous, '', 'Tether').session, 'Tether', 'Existing matches win despite phonetic ambiguity')
console.log('Friday sessions: names, counts, case-insensitive filtering, normalized and closest matches, threshold, errors and ID fallback pass')
