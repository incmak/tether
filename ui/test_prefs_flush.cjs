const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
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
const prefsKey = 'tmux-chat.prefs.v1'
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
    restoreRejected() {},
    setZoneRejection() {},
    request: async (path, body) => {
      calls.push({ path, body: JSON.parse(JSON.stringify(body)) })
      // The sent batch stays on disk until the server answers, so closing the page mid-request loses nothing.
      assert.deepEqual(plain(loadPending()), plain(changes))
      pending.current = { ...newer }
      persist()
      assert.deepEqual(plain(loadPending()), plain({ ...changes, ...newer }))
      if (error) throw error
      return { prefs: { ...changes }, version: 1, zone: 'Asia/Kolkata' }
    },
    apply: stored => { assert.equal(error, null); assert.equal(stored.version, 1) },
    rememberZone: stored => { assert.equal(error, null); assert.equal(stored.zone, 'Asia/Kolkata') },
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

function zonePrefs(initial, get, post) {
  const localStorage = storage()
  localStorage.setItem('tmux-chat.prefs.synced', '1')
  localStorage.setItem(prefsKey, JSON.stringify({ timeZone: initial }))
  const slots = []
  let slot = 0
  let pull
  let flush
  const layouts = []
  const react = {
    useState: init => {
      const i = slot++
      if (!(i in slots)) slots[i] = typeof init === 'function' ? init() : init
      return [slots[i], update => { slots[i] = typeof update === 'function' ? update(slots[i]) : update }]
    },
    useRef: current => (slots[slot++] ??= { current }),
    useCallback: callback => callback,
    useEffect: effect => { pull ??= effect },
    useLayoutEffect: effect => { layouts.push(effect) },
  }
  const { usePrefs } = load('prefs', { ...imports, react, './api': {
    ...api, request: async (path, body) => {
      assert.equal(path, 'prefs')
      return body ? post(plain(body.changes)) : typeof get === 'function' ? get() : get
    },
  } }, {
    localStorage, document: { addEventListener() {} },
    window: { setInterval() {}, addEventListener() {}, clearTimeout() {}, setTimeout: callback => { flush = callback } },
  })
  const render = () => {
    slot = 0
    const result = usePrefs()
    for (const effect of layouts.splice(0)) effect()
    return result
  }
  return {
    render,
    pull: async () => {
      render()
      pull()
      await new Promise(setImmediate)
      return render()
    },
    flush: () => flush(),
    saved: () => JSON.parse(localStorage.getItem(prefsKey)),
  }
}

test('prefs shows the server zone for a confirmed fixed-offset preference', async () => {
  const prefs = zonePrefs('+05:30', { prefs: { timeZone: '+05:30' }, version: 1, zone: 'Pacific/Auckland' })
  await prefs.pull()
  assert.equal(zone.tetherZone(), 'Pacific/Auckland')
})

test('prefs follows a server alias, previews a local change, and confirms it from POST', async () => {
  const prefs = zonePrefs(undefined, { prefs: {}, version: 1, zone: 'Asia/Calcutta' }, changes => {
    assert.deepEqual(changes, { timeZone: '+05:30' })
    return { prefs: changes, version: 2, zone: 'Pacific/Auckland' }
  })
  const [, update] = await prefs.pull()
  assert.match(zone.tetherZone(), /^Asia\/(?:Calcutta|Kolkata)$/)
  update(() => ({ timeZone: '+05:30' }))
  prefs.render()
  assert.equal(zone.tetherZone(), zone.pickZone('+05:30', 'Asia/Calcutta'))
  await prefs.flush()
  prefs.render()
  assert.equal(zone.tetherZone(), 'Pacific/Auckland')
})

test('prefs keeps a formattable zone when the server confirms one this browser cannot format', async () => {
  const prefs = zonePrefs('America/New_York', { prefs: { timeZone: 'America/New_York' }, version: 1, zone: 'Not/AZone' })
  await prefs.pull()
  assert.equal(zone.tetherZone(), 'America/New_York')
  assert.notEqual(zone.fmtTime(Date.UTC(2026, 10, 22, 12, 0)), 'Invalid Date')
})

test('prefs still uses the saved choice when an older backend omits zone', async () => {
  const prefs = zonePrefs(undefined, { prefs: { timeZone: 'America/New_York' }, version: 1 })
  await prefs.pull()
  assert.equal(zone.tetherZone(), 'America/New_York')
})

test('prefs keeps POST confirmation when an older GET arrives late', async () => {
  const old = { prefs: { timeZone: 'Asia/Calcutta' }, version: 1, zone: 'Asia/Calcutta' }
  let finishPull
  const delayed = new Promise(resolve => { finishPull = resolve })
  let pulls = 0
  const prefs = zonePrefs(undefined, () => pulls++ ? delayed : old, changes => (
    { prefs: changes, version: 2, zone: 'Pacific/Auckland' }
  ))
  const [, update] = await prefs.pull()
  await prefs.pull()
  update(() => ({ timeZone: 'Pacific/Auckland' }))
  prefs.render()
  await prefs.flush()
  finishPull(old)
  await new Promise(setImmediate)
  prefs.render()
  assert.equal(zone.tetherZone(), 'Pacific/Auckland')
})

test('prefs restores the server default after a rejected zone and a same-version GET', async () => {
  const stored = { prefs: {}, version: 1, zone: 'Pacific/Auckland' }
  const attempted = 'America/Coyhaique'
  const prefs = zonePrefs(undefined, stored, () => { throw new api.ApiError(400, 'Invalid preference: timeZone.') })
  const [, update] = await prefs.pull()
  update(() => ({ timeZone: attempted }))
  prefs.render()
  assert.equal(zone.tetherZone(), attempted)
  await prefs.flush()
  const [restored, , rejectedZone] = await prefs.pull()
  assert.equal(zone.tetherZone(), stored.zone)
  assert.equal(restored.timeZone, undefined)
  assert.equal(prefs.saved().timeZone, undefined)
  assert.equal(rejectedZone, attempted)
})

test('prefs restores the previous zone and saves a valid key from the rejected batch', async () => {
  const stored = { prefs: { timeZone: 'America/New_York', theme: 'light' }, version: 1, zone: 'America/New_York' }
  const batches = []
  const attempted = 'America/Coyhaique'
  const prefs = zonePrefs(undefined, stored, changes => {
    batches.push(changes)
    if ('timeZone' in changes) throw new api.ApiError(400, 'Invalid preference: timeZone.')
    return { ...stored, prefs: { ...stored.prefs, ...changes }, version: 2 }
  })
  const [, update] = await prefs.pull()
  update(() => ({ timeZone: attempted, theme: 'dark' }))
  await prefs.flush()
  const [restored, , rejectedZone] = prefs.render()
  assert.equal(restored.timeZone, stored.prefs.timeZone)
  assert.equal(restored.theme, 'dark')
  assert.equal(rejectedZone, attempted)
  await prefs.flush()
  assert.deepEqual(batches, [{ timeZone: attempted, theme: 'dark' }, { theme: 'dark' }])
  assert.equal(prefs.render()[0].theme, 'dark')
})

test('prefs restores another rejected key and keeps a valid time zone from that batch', async () => {
  const stored = { prefs: { timeZone: 'America/New_York', theme: 'light' }, version: 1, zone: 'America/New_York' }
  const batches = []
  const choice = 'Pacific/Auckland'
  const prefs = zonePrefs(undefined, stored, changes => {
    batches.push(changes)
    if ('theme' in changes) throw new api.ApiError(400, 'Invalid preference: theme.')
    return { prefs: { ...stored.prefs, ...changes }, version: 2, zone: choice }
  })
  const [, update] = await prefs.pull()
  update(() => ({ timeZone: choice, theme: 'dark' }))
  await prefs.flush()
  const [restored, , rejectedZone] = prefs.render()
  assert.equal(restored.timeZone, choice)
  assert.equal(restored.theme, 'light')
  assert.equal(prefs.saved().timeZone, choice)
  assert.equal(rejectedZone, undefined)
  await prefs.flush()
  assert.deepEqual(batches, [{ timeZone: choice, theme: 'dark' }, { timeZone: choice }])
  prefs.render()
  assert.equal(zone.tetherZone(), choice)
})

test('Time zone settings renders a live status below the menu after rejection', () => {
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const setting = { exports: {} }
  const imports = {
    react: React, 'react/jsx-runtime': require('react/jsx-runtime'),
    './SelectMenu': { SelectMenu: () => React.createElement('button', {}, 'Time zone menu') },
    './zone': { ...zone, useTetherZone: () => 'Pacific/Auckland' },
  }
  runInNewContext(ts.transpileModule(readFileSync('src/TimeZoneSetting.tsx', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, { exports: setting.exports, require: name => imports[name] })
  const html = renderToStaticMarkup(React.createElement(setting.exports.TimeZoneSetting, {
    onChange() {}, rejectedZone: 'America/Coyhaique',
  }))
  assert.match(html, /<\/button><div role="status"><p[^>]*>Tether could not use America\/Coyhaique on the server\. It still uses Pacific\/Auckland\.<\/p><\/div>/)
})

Promise.all([
  testFlush(null, {}, {}, 0, 0),
  testFlush(new api.ApiError(400, 'Invalid preference: waitingOrder.'), {}, { theme: 'dark' }, 1, 1),
  testFlush(new api.ApiError(400, 'Invalid preference: waitingOrder.'),
    { waitingOrder: 'oldest', theme: 'light' }, { waitingOrder: 'oldest', theme: 'light' }, 1, 1),
  testFlush(new api.ApiError(400, 'Rejected body'), {}, {}, 0, 1),
  testFlush(new Error('Offline'), { theme: 'light' }, { waitingOrder: 'newest', theme: 'light' }, 1, 0),
]).then(() => console.log('Prefs flush: actual catch preserves newer changes, schedules retries and warns once per rejected batch'))
  .catch(error => { console.error(error); process.exitCode = 1 })
