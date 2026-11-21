const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(name, imports = {}, globals = {}) {
  const exports = {}
  runInNewContext(ts.transpileModule(readFileSync(`src/${name}.ts`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, { ...globals, exports, require: name => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`)
    return imports[name]
  } })
  return exports
}

const zone = load('zone', { react: {} })
const api = load('api', { './zone': zone })
const imports = { react: {}, './api': api, './layouts': { DEFAULT_LOOK: {} }, './zone': zone }
const pendingKey = 'tmux-chat.prefs.pending'
function storage() {
  const items = new Map()
  return {
    getItem: key => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: key => items.delete(key),
  }
}
const localStorage = storage()
const { rejectedKeep, loadPending, savePending } = load('prefs', imports, { localStorage })
const plain = value => JSON.parse(JSON.stringify(value))
savePending({ order: ['a'], timeZone: 'America/New_York' })
assert.deepEqual(plain(loadPending()), { order: ['a'], timeZone: 'America/New_York' })
savePending({})
assert.equal(localStorage.getItem(pendingKey), null)
for (const value of ['{"unknown":true}', '{broken', 'null', '[]', '42', '"text"']) {
  localStorage.setItem(pendingKey, value)
  assert.deepEqual(plain(loadPending()), {})
}
localStorage.setItem(pendingKey, '{"order":["a"],"unknown":true}')
assert.deepEqual(plain(loadPending()), { order: ['a'] })
savePending({ order: ['a'], unknown: true })
assert.deepEqual(JSON.parse(localStorage.getItem(pendingKey)), { order: ['a'] })
const blocked = load('prefs', imports, { localStorage: {
  getItem: () => { throw new Error('Blocked') },
  setItem: () => { throw new Error('Blocked') },
  removeItem: () => { throw new Error('Blocked') },
} })
assert.deepEqual(plain(blocked.loadPending()), {})
assert.doesNotThrow(() => blocked.savePending({ order: ['a'] }))
assert.doesNotThrow(() => blocked.savePending({}))
console.log('Prefs pending: round trip, removal, invalid data and blocked storage pass')
const changes = Object.freeze({ waitingOrder: 'newest', theme: 'dark' })
const kept = error => {
  const result = rejectedKeep(changes, error)
  return result === null ? null : JSON.parse(JSON.stringify(result))
}

assert.equal(kept(new Error('Network unavailable')), null)
assert.equal(kept({ status: 400, message: 'Invalid preference: waitingOrder.' }), null)
for (const status of [401, 403, 408, 429, 500, 503, 599]) {
  assert.equal(kept(new api.ApiError(status, 'Try again')), null, `Retry status ${status}`)
}
assert.deepEqual(kept(new api.ApiError(400, 'Invalid preference: waitingOrder.')), { theme: 'dark' })
for (const status of [400, 404, 409, 422]) {
  assert.deepEqual(kept(new api.ApiError(status, 'Rejected body')), {}, `Drop status ${status}`)
}
assert.deepEqual(kept(new api.ApiError(400, 'Invalid preference: absent.')), {})
assert.deepEqual(kept(new api.ApiError(400, 'Invalid preference: waitingOrder')), {})
assert.deepEqual(changes, { waitingOrder: 'newest', theme: 'dark' })
console.log('Prefs flush: retryable errors, rejected keys, rejected batches and unchanged input pass')

// Exercise the actual flush function with requests and timers mocked; no React renderer or network.
const source = ts.createSourceFile('prefs.ts', readFileSync('src/prefs.ts', 'utf8'), ts.ScriptTarget.Latest, true)
let flush
function visit(node) {
  if (ts.isFunctionExpression(node) && node.name?.text === 'flush') flush = node
  ts.forEachChild(node, visit)
}
visit(source)
assert.ok(flush)
async function testFlush(error, newer, expected, retries, warns) {
  const pending = { current: { ...changes } }
  const localStorage = storage()
  const { loadPending, savePending } = load('prefs', imports, { localStorage })
  savePending(pending.current)
  const busy = { current: false }
  const sending = { current: {} }
  const persist = () => savePending({ ...sending.current, ...pending.current })
  const calls = []
  const timers = []
  const warnings = []
  const context = {
    pending, busy, sending, persist, timer: { current: 0 }, rejectedKeep, savePending, ApiError: api.ApiError,
    request: async (path, body) => {
      calls.push({ path, body: JSON.parse(JSON.stringify(body)) })
      // The sent batch stays on disk until the server answers, so closing the page mid-request loses nothing.
      assert.deepEqual(plain(loadPending()), plain(changes))
      pending.current = { ...newer }
      persist()
      assert.deepEqual(plain(loadPending()), plain({ ...changes, ...newer }))
      if (error) throw error
      return { prefs: { ...changes }, version: 1 }
    },
    apply: stored => { assert.equal(error, null); assert.equal(stored.version, 1) },
    console: { warn: (...args) => warnings.push(args) },
    window: { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return 1 } },
  }
  runInNewContext(ts.transpileModule(`globalThis.flush = ${flush.getText(source)}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2020 },
  }).outputText, context)
  await context.flush()
  assert.deepEqual(calls, [{ path: 'prefs', body: { changes } }])
  assert.deepEqual(JSON.parse(JSON.stringify(pending.current)), expected)
  assert.deepEqual(plain(loadPending()), expected)
  if (!Object.keys(expected).length) assert.equal(localStorage.getItem(pendingKey), null)
  assert.equal(busy.current, false)
  assert.equal(timers.length, retries)
  if (retries) assert.equal(timers[0].delay, 2000)
  assert.equal(warnings.length, warns)
  if (warns) assert.deepEqual(warnings[0].slice(1), [error.status, error.message])
}
Promise.all([
  testFlush(null, {}, {}, 0, 0),
  testFlush(new api.ApiError(400, 'Invalid preference: waitingOrder.'), {}, { theme: 'dark' }, 1, 1),
  testFlush(new api.ApiError(400, 'Invalid preference: waitingOrder.'),
    { waitingOrder: 'oldest', theme: 'light' }, { waitingOrder: 'oldest', theme: 'light' }, 1, 1),
  testFlush(new api.ApiError(400, 'Rejected body'), {}, {}, 0, 1),
  testFlush(new Error('Offline'), { theme: 'light' }, { waitingOrder: 'newest', theme: 'light' }, 1, 0),
]).then(() => console.log('Prefs flush: actual catch preserves newer changes, schedules retries and warns once per rejected batch'))
  .catch(error => { console.error(error); process.exitCode = 1 })
