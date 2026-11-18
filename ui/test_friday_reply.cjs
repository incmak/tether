const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(file, imports = {}) {
  const exports = {}
  runInNewContext(ts.transpileModule(readFileSync(join(__dirname, 'src', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, { exports, require: name => imports[name] })
  return exports
}

const { replyPart, REPLY_PART } = load('fridayReply.ts')
const { speakable } = load('speech.ts', { react: {} })
const fixture = readFileSync(join(__dirname, 'fixtures', 'factory-floor-reply.md'), 'utf8')
const spoken = speakable(fixture)
// Convert the cross-realm result to a plain object for assertions.
const part = (...args) => ({ ...replyPart(...args) })
const realReply = part(spoken)
assert.equal(REPLY_PART, 6000)
assert.ok(spoken.length > 1500)
assert.deepEqual(realReply, { text: spoken, total_chars: spoken.length, offset: 0, truncated: false })
for (let n = 1; n <= 5; n++) assert.ok(realReply.text.includes(`Q${n} - `))

const long = 'alpha beta gamma '.repeat(883).slice(0, 15000)
const first = part(long)
const second = part(long, first.next_offset)
const third = part(long, second.next_offset)
assert.deepEqual([first.truncated, second.truncated, third.truncated], [true, true, false])
assert.equal(third.next_offset, undefined)
assert.equal(first.text + second.text + third.text, long)
assert.deepEqual([first.offset, second.offset, third.offset], [0, first.text.length, first.text.length + second.text.length])
for (const item of [first, second, third]) {
  assert.ok(item.text.length <= REPLY_PART)
  assert.equal(item.total_chars, 15000)
}

assert.deepEqual(part('alpha beta', 0, 8), {
  text: 'alpha ', total_chars: 10, offset: 0, truncated: true, next_offset: 6,
})
assert.equal(part('alpha beta', 0, 5).text, 'alpha')
assert.deepEqual(part('a😀b', 0, 2), { text: 'a', total_chars: 4, offset: 0, truncated: true, next_offset: 1 })
assert.equal(part('a😀b', 1).text, '😀b')
assert.equal(part('a😀b', 2).text, '😀b')

for (const offset of [-1, NaN, -Infinity]) {
  assert.deepEqual(part('abc', offset), { text: 'abc', total_chars: 3, offset: 0, truncated: false })
}
for (const offset of [3, 99, Infinity]) {
  assert.deepEqual(part('abc', offset), { text: '', total_chars: 3, offset: 3, truncated: false })
}
assert.deepEqual(part(''), { text: '', total_chars: 0, offset: 0, truncated: false })
console.log('Friday reply: factory-floor questions, three-part round trip, word and surrogate boundaries, offsets and empty text pass')
