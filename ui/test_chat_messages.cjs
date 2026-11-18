// Chat data from the server is not trusted. Check the shared transcript reader (api.ts) and Live's lastReply,
// which uses it, against transcripts that are missing, the wrong type, or hold bad entries.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const api = {}
runInNewContext(compile(readFileSync('src/api.ts', 'utf8')), { exports: api, require: () => new Proxy({}, { get: (_, key) => key }) })
const { chatMessages, cleanChat } = api

// The real lastReply from Live.tsx, run with the real chatMessages.
const live = ts.createSourceFile('Live.tsx', readFileSync('src/Live.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const declaration = live.statements.find(node => ts.isVariableStatement(node)
  && node.declarationList.declarations.some(item => item.name.getText(live) === 'lastReply'))
assert.ok(declaration, 'Live.tsx declares lastReply')
const scope = { chatMessages }
runInNewContext(compile(declaration.getText(live)) + '\nthis.lastReply = lastReply', scope)
const { lastReply } = scope
// The sandbox builds arrays and objects with its own prototypes; compare values as JSON.
const same = (actual, expected, label) => assert.equal(JSON.stringify(actual), JSON.stringify(expected), label)

const reply = { id: 'r1', role: 'assistant', text: 'Done.' }
const ask = { id: 'u1', role: 'user', text: 'Run it' }
const bad = [null, 5, 'text', { role: 'user', text: 5 }, { role: 'assistant' }, { text: 'no role' }]

for (const messages of [undefined, null, {}, '', 7, { length: 1, 0: reply }]) {
  const label = JSON.stringify(messages) ?? 'missing'
  same(chatMessages({ messages }), [], label)
  same(cleanChat({ messages, state: 'Idle' }), { messages: [], state: 'Idle' }, label)
  assert.equal(lastReply({ messages }), null, label)
}
same(chatMessages(undefined), [])
assert.equal(cleanChat(undefined), undefined)
console.log('A transcript that is missing or not a list reads as empty, and Live reports no reply')

const mixed = [...bad, ask, reply, ...bad]
same(chatMessages({ messages: mixed }), [ask, reply])
same(cleanChat({ messages: mixed }).messages, [ask, reply])
assert.equal(lastReply({ messages: mixed }), reply)
assert.equal(lastReply({ messages: [reply, ...bad] }), reply, 'bad entries after the reply are skipped')
console.log('Bad entries drop out, and Live still finds the last reply')

const valid = [ask, reply]
const data = { messages: valid, state: 'Idle' }
assert.equal(chatMessages(data), valid, 'A valid list comes back as the same array')
assert.equal(cleanChat(data), data, 'Valid data comes back as the same object')
console.log('Valid chat data keeps its identity, so memos and cache checks still match')

// Nested fields: the artifact reader (preview.ts artifactsOf) walks tools and members, so a bad shape there must drop
// out too. The reviewer's case: an actions entry with tools = { length: 1 }.
const preview = {}
runInNewContext(compile(readFileSync('src/preview.ts', 'utf8')), { exports: preview, require: () => ({ createContext: () => ({}) }) })
const artifact = { url: 'https://claude.ai/artifact/abc', path: '/w/page.html', title: 'Page' }
const tool = { id: 't1', name: 'Artifact', title: 'Publish', is_error: false, status: 'complete', artifact }
const nested = [
  { id: 'a1', role: 'assistant', text: '', kind: 'actions', tools: { length: 1 } },
  { id: 'a2', role: 'assistant', text: '', kind: 'actions', tools: [null, 5, { id: 't2' }, { ...tool, id: 't3', artifact: { url: 7 }, description: 9, started: [] }, tool] },
  { id: 'a3', role: 'assistant', text: '', members: [null, { role: 'user', text: 1 }, { id: 'm1', role: 'assistant', text: '', tools: 'x' }] },
  { id: 'a4', role: 'assistant', text: 'x', members: { length: 1 }, watches: [1], title: 5, cycles: 'two', time: null },
]
assert.throws(() => preview.artifactsOf(nested), 'the raw data crashes the artifact reader')
const cleaned = chatMessages({ messages: nested })
same(cleaned.map(message => message.id), ['a1', 'a2', 'a3', 'a4'])
assert.equal(cleaned[0].tools, undefined, 'a tool list that is not a list drops out')
same(cleaned[1].tools.map(item => item.id), ['t3', 't1'], 'bad tools drop out')
same(Object.keys(cleaned[1].tools[0]), ['id', 'name', 'title', 'is_error', 'status'], 'a bad artifact and bad fields drop out of their tool')
same(cleaned[2].members, [{ id: 'm1', role: 'assistant', text: '' }], 'bad members drop out, and their bad fields too')
same(cleaned[3], { id: 'a4', role: 'assistant', text: 'x' }, 'wrong-type fields drop out')
same([...preview.artifactsOf(cleaned).keys()], [artifact.url])
assert.equal(nested[1].tools.length, 5, 'the input is not changed')
const good = [{ id: 'g', role: 'assistant', text: '', tools: [tool], members: [{ ...ask }], watches: ['w'] }]
assert.equal(chatMessages({ messages: good }), good, 'valid nested data keeps its identity')
console.log('Bad tools, artifacts, members and fields drop out, and the artifact reader still runs')

// A rejected entry must count as a change even when it is the only bad one: null cleans to null, and a hole in a
// sparse list is skipped by map and every. Each case has exactly one bad entry beside valid ones.
const holes = [reply]; holes[2] = ask
for (const [label, list, ids] of [['a lone null message', [reply, null], ['r1']], ['a lone undefined message', [undefined, reply], ['r1']],
  ['a hole in the list', holes, ['r1', 'u1']]]) {
  const clean = chatMessages({ messages: list })
  same(clean.map(message => message.id), ids, label)
  assert.equal(lastReply({ messages: list }), reply, label)
  assert.doesNotThrow(() => preview.artifactsOf(clean), label)
}
const lone = chatMessages({ messages: [{ id: 'n1', role: 'assistant', text: '', tools: [null, tool], members: [null, { ...reply }] }] })
same(lone[0].tools.map(item => item.id), ['t1'], 'a lone null tool drops out')
same(lone[0].members.map(item => item.id), ['r1'], 'a lone null member drops out')
same([...preview.artifactsOf(lone).keys()], [artifact.url])
console.log('A lone null or a hole drops out of messages, tools and members')
