const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(file, imports = {}) {
  const source = readFileSync(join(__dirname, 'src', file), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    fileName: file,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  })
  const exports = {}
  runInNewContext(outputText, { exports, require: name => imports[name] ?? {} }, { filename: file })
  return exports
}

const { chipLabel } = load('fridayNotes.ts')
const docs = { id: 'T1', title: 'Docs', chat: '', state: 'open', notes: [{ text: 'Keep this.', at: 100 }] }
const api = {
  id: 'T2', title: 'API', chat: '', state: 'open',
  notes: [{ text: 'Use port 8080.', at: 101 }, { text: 'Keep the route.', at: 102 }],
}

test('one topic shows its title and note count', () => {
  const { lead, count } = chipLabel([api])
  assert.equal(lead, 'Held for API')
  assert.equal(count, '2')
})

test('two topics show the topic count and the sum of their notes', () => {
  const { lead, count } = chipLabel([docs, api])
  assert.equal(lead, 'Held: 2 topics')
  assert.equal(count, '3')
})
