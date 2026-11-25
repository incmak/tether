// B331: foreignQueued keeps the Claude queue entries that this message box did not send. Real TS, no DOM.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const ts = require('typescript')

const source = ts.transpileModule(readFileSync('src/queueMatch.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const module_ = { exports: {} }
new Function('module', 'exports', source)(module_, module_.exports)
const { foreignQueued } = module_.exports

const entry = text => ({ text, time: null })
const local = text => ({ text, key: text.replace(/\s+/g, ' ').trim().slice(0, 80) })
const shown = (entries, mine) => foreignQueued(entries.map(entry), mine.map(local)).map(item => item.text)

// A local text inside a longer entry does not hide that entry (the review case).
assert.deepEqual(shown(['Do not run the tests until Friday', 'run the tests'], ['run the tests']), ['Do not run the tests until Friday'])
// A short local text never takes a longer entry, even when it is the only entry (round 2 review cases).
assert.deepEqual(shown(['run the tests after Friday'], ['run the tests']), ['run the tests after Friday'])
assert.deepEqual(shown(['check the logs for production'], ['check the logs']), ['check the logs for production'])
// Two prompts that share their first 80 characters: the exact text decides.
const head = 'x'.repeat(80)
assert.deepEqual(shown([`${head} first`, `${head} second`], [`${head} second`]), [`${head} first`])
// Each local bubble hides one entry only.
assert.deepEqual(shown(['again', 'again'], ['again']), ['again'])
assert.deepEqual(shown(['again', 'again'], ['again', 'again']), [])
// Whitespace and "! ls" vs "!ls" count as the same text.
assert.deepEqual(shown(['! ls  -la', 'other'], ['!ls -la']), ['other'])
// Two long prompts that share their first 80 characters: only the same text hides an entry (round 3 review cases).
const start = 'Please check the deploy log for the share page and the files page, then list every error '
assert.deepEqual(shown([`${start}from Friday`], [`${start}from this tab`]), [`${start}from Friday`])
assert.deepEqual(shown([`${start}a`, `${start}b`], [`${start}b`]), [`${start}a`])
// Nothing local: every entry shows. Nothing queued: nothing shows.
assert.deepEqual(shown(['a', 'b'], []), ['a', 'b'])
assert.deepEqual(shown([], ['a']), [])
// Extra fields such as id pass through.
assert.deepEqual(foreignQueued([{ id: 7, text: 'a', time: 't' }], []), [{ id: 7, text: 'a', time: 't' }])

console.log('ok test_queue_match')
