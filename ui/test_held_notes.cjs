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

const { chipLabel, dropNotes } = load('fridayNotes.ts', { './api': { errorText: error => error.message } })
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

test('Friday needs a confirmed topic ID before it can call the drop API', async () => {
  const request = () => assert.fail('The drop API must not be called without an ID')
  for (const args of [{ topic: 'API' }, { topic: 'API', id: 2, note: 1 }]) {
    const result = await dropNotes(request, args)
    assert.equal(result.error, 'Not deleted. Friday must read it back and hear yes first.')
  }
})

test('Friday sends the confirmed ID and uses the exact text for a note drop', async () => {
  const calls = []
  const request = (path, body) => {
    calls.push([path, { ...body }])
    return { dropped: body.note === undefined ? 'topic' : 'note', topic: 'API', left: 0 }
  }
  assert.equal((await dropNotes(request, { topic: 'unused', id: 'T2', note: 1, text: 'Keep the route.' })).ok, true)
  assert.equal((await dropNotes(request, { id: 'T2', count: 2 })).ok, true)
  assert.deepEqual(calls, [
    ['friday-notes/drop', { id: 'T2', note: 1, text: 'Keep the route.' }],
    ['friday-notes/drop', { id: 'T2', count: 2 }],
  ])
})
