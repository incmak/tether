const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const m = {}
runInNewContext(ts.transpileModule(readFileSync('src/liveHistory.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText, { exports: m })
const plain = value => JSON.parse(JSON.stringify(value))
const empty = { said: '', reply: { who: 'Friday', text: '' } }

// First phrase: nothing to keep yet.
let step = plain(m.heard(empty, 'Hi'))
assert.deepEqual(step, { hero: { said: 'Hi', reply: { who: 'Friday', text: '' } }, keep: [] })
// A streaming reply grows in place.
step = plain(m.answered(step.hero, { who: 'Friday', text: 'Hel' }))
assert.deepEqual(step.keep, [])
step = plain(m.answered(step.hero, { who: 'Friday', text: 'Hello there.' }))
assert.deepEqual(step, { hero: { said: 'Hi', reply: { who: 'Friday', text: 'Hello there.' } }, keep: [] })
// The next phrase moves the whole exchange up, person first.
const next = plain(m.heard(step.hero, 'Check B318'))
assert.deepEqual(next.keep, [{ who: 'You', text: 'Hi', you: true }, { who: 'Friday', text: 'Hello there.', you: false }])
assert.deepEqual(next.hero, { said: 'Check B318', reply: { who: 'Friday', text: '' } })
// A different reply (new text or another speaker) keeps the old exchange and clears the phrase.
const other = plain(m.answered(step.hero, { who: 'Review', text: 'Done.' }))
assert.equal(other.keep.length, 2)
assert.deepEqual(other.hero, { said: '', reply: { who: 'Review', text: 'Done.' } })
const restart = plain(m.answered(step.hero, { who: 'Friday', text: 'Sorry, again.' }))
assert.equal(restart.keep.length, 2)
// A fresh reply that starts with the old words is a new entry, not growth (review of d5c7001).
const done = { said: 'Run it', reply: { who: 'Friday', text: 'Done.' } }
const second = plain(m.answered(done, { who: 'Friday', text: 'Done. The tests passed.' }, true))
assert.deepEqual(second.keep.map(line => line.text), ['Run it', 'Done.'])
assert.deepEqual(second.hero.reply.text, 'Done. The tests passed.')
// A fresh reply with the same words is a second entry, not a lost one.
assert.deepEqual(plain(m.answered(done, { who: 'Friday', text: 'Done.' }, true)).keep.map(line => line.text), ['Run it', 'Done.'])
// The same words again inside one stream (not fresh) stay one reply.
assert.equal(plain(m.answered(done, { who: 'Friday', text: 'Done.' })).keep.length, 0)
// Without fresh, a longer version of the same reply still grows in place.
assert.equal(plain(m.answered(done, { who: 'Friday', text: 'Done. The tests passed.' })).keep.length, 0)
// Blank lines are not kept.
assert.deepEqual(plain(m.heard({ said: '  ', reply: { who: 'Friday', text: 'Yes.' } }, 'x').keep),
  [{ who: 'Friday', text: 'Yes.', you: false }])
// remember: fresh ids, cap, and no copy when nothing is kept.
let id = 0
const past = []
assert.equal(m.remember(past, [], () => ++id), past)
let many = past
for (let i = 0; i < 150; i++) many = m.remember(many, [{ who: 'You', text: `a${i}`, you: true }, { who: 'F', text: `b${i}`, you: false }], () => ++id)
assert.equal(many.length, m.PAST_MAX)
assert.equal(many[many.length - 1].text, 'b149')
assert.equal(many[0].text, 'a50')
assert.equal(new Set(many.map(line => line.id)).size, m.PAST_MAX)
console.log('liveHistory ok')
