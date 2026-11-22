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
const rejectedKey = 'tmux-chat.prefs.rejected'
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
    SYNCED: Object.keys(changes),
    forgetRejections() {},
    restoreRejected() {},
    setZoneRejection() {},
    requestStored: async body => {
      calls.push(plain(body))
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
  assert.deepEqual(calls, [{ changes }])
  assert.deepEqual(JSON.parse(JSON.stringify(pending.current)), expected)
  assert.deepEqual(plain(loadPending()), expected)
  if (!Object.keys(expected).length) assert.equal(localStorage.getItem(pendingKey), null)
  assert.equal(busy.current, false)
  assert.equal(timers.length, retries)
  if (retries) assert.equal(timers[0].delay, 2000)
  assert.equal(warnings.length, warns)
  if (warns) assert.deepEqual(warnings[0].slice(1), [error.status, error.message])
}

function prefsHarness(initial, get, post, localStorage = storage()) {
  localStorage.setItem('tmux-chat.prefs.synced', '1')
  if (initial) localStorage.setItem(prefsKey, JSON.stringify(initial))
  const slots = []
  let slot = 0
  let pull
  let timer, renderError
  const layouts = []
  const react = {
    useState: init => {
      const i = slot++
      if (!(i in slots)) slots[i] = typeof init === 'function' ? init() : init
      return [slots[i], update => {
        try { slots[i] = typeof update === 'function' ? update(slots[i]) : update }
        catch (error) { renderError = error }  // React can run a state updater during render, outside the caller's try.
      }]
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
    Object, localStorage, document: { addEventListener() {} },  // Mock API bodies come from this realm.
    window: {
      setInterval() {}, addEventListener() {}, clearTimeout() { timer = undefined },
      setTimeout: (callback, delay) => { timer = { callback, delay } },
    },
  })
  const render = () => {
    if (renderError) throw renderError
    slot = 0
    const result = usePrefs()
    for (const effect of layouts.splice(0)) effect()
    return result
  }
  return {
    localStorage,
    render,
    pull: async () => {
      render()
      pull()
      await new Promise(setImmediate)
      return render()
    },
    flush: () => {
      const { callback } = timer
      timer = undefined
      return callback()
    },
    retryDelay: () => timer?.delay,
    saved: () => JSON.parse(localStorage.getItem(prefsKey)),
    queued: () => localStorage.getItem(pendingKey),
  }
}

const zonePrefs = (timeZone, get, post) => prefsHarness({ timeZone }, get, post)

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

test('prefs retries a malformed POST success and keeps the local choice', async () => {
  const stored = { prefs: { timeZone: 'America/New_York' }, version: 1, zone: 'America/New_York' }
  const choice = 'Pacific/Auckland'
  const batches = []
  const prefs = zonePrefs(stored.zone, stored, changes => {
    batches.push(changes)
    if (batches.length === 1) return { ok: true, accepted: true }
    return { prefs: changes, version: 2, zone: choice }
  })
  const [, update] = await prefs.pull()
  update(() => ({ timeZone: choice }))
  await assert.doesNotReject(prefs.flush)
  const [current, , rejection] = prefs.render()
  assert.equal(rejection, undefined)
  assert.equal(current.timeZone, choice)
  assert.equal(zone.tetherZone(), choice)
  assert.deepEqual(JSON.parse(prefs.queued()), { timeZone: choice })
  assert.equal(prefs.retryDelay(), 2000)

  await prefs.flush()
  assert.deepEqual(batches, [{ timeZone: choice }, { timeZone: choice }])
  assert.equal(prefs.queued(), null)
  assert.equal(prefs.retryDelay(), undefined)
})

test('prefs ignores a malformed GET success and keeps the local choice', async () => {
  const choice = 'America/New_York'
  const prefs = zonePrefs(choice, {})
  const [before] = prefs.render()
  const [after] = await prefs.pull()
  assert.equal(after, before)
  assert.deepEqual(prefs.saved(), { timeZone: choice })
  assert.equal(zone.tetherZone(), choice)
  assert.equal(prefs.queued(), null)
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
    if (changes.timeZone === attempted) throw new api.ApiError(400, 'Invalid preference: timeZone.')
    return { prefs: { ...stored.prefs, ...changes }, version: batches.length, zone: changes.timeZone ?? stored.zone }
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
  assert.equal(prefs.render()[2], attempted)
  update(() => ({ timeZone: 'Europe/London' }))
  await prefs.flush()
  assert.equal(prefs.render()[2], undefined)
  assert.equal(zone.tetherZone(), 'Europe/London')
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

test('prefs keeps all 20,001 characters of a rejected draft and drops the write', async () => {
  const drafts = { '%1': { text: 'Last synced draft', at: 1 } }
  const stored = { prefs: { drafts }, version: 1 }
  const text = 'x'.repeat(20_001)
  const prefs = prefsHarness({}, stored, changes => {
    assert.equal(changes.drafts['%1'].text, text)
    throw new api.ApiError(400, 'Invalid preference: drafts.')
  })
  const [, update] = await prefs.pull()
  update(() => ({ drafts: { '%1': { text, at: 2 } } }))
  await prefs.flush()
  assert.equal(prefs.render()[0].drafts['%1'].text === text, true, 'all draft text stays in the composer')
  assert.equal(prefs.saved().drafts['%1'].text === text, true, 'all draft text stays in local storage')
  assert.equal(prefs.queued(), null)

  const reloaded = prefsHarness(undefined, stored, undefined, prefs.localStorage)
  const [current] = await reloaded.pull()
  assert.equal(current.drafts['%1'].text === text, true, 'reload keeps all the text after a server pull')
  assert.equal(reloaded.queued(), null, 'pull does not requeue the rejected draft')
})

test('prefs keeps a rejected pin note when another key syncs and sends a later edit', async () => {
  const serverPins = [{ pane: '%1', note: 'Last synced note' }]
  const localPins = [{ pane: '%1', note: 'x'.repeat(61) }]
  const editedPins = [{ pane: '%1', note: 'Shorter note' }]
  const stored = { prefs: { sidePins: serverPins, theme: 'light' }, version: 1 }
  const batches = []
  const prefs = prefsHarness({}, stored, changes => {
    batches.push(changes)
    if (changes.sidePins?.[0].note.length > 60) throw new api.ApiError(400, 'Invalid preference: sidePins.')
    return { prefs: { ...stored.prefs, ...changes }, version: batches.length }
  })
  const [, update] = await prefs.pull()
  update(() => ({ sidePins: localPins, theme: 'dark' }))
  await prefs.flush()
  assert.deepEqual(prefs.saved().sidePins, localPins)
  assert.deepEqual(JSON.parse(prefs.queued()), { theme: 'dark' })
  await prefs.flush()
  assert.deepEqual(plain(prefs.render()[0].sidePins), localPins)
  assert.deepEqual(prefs.saved().sidePins, localPins)
  assert.equal(prefs.queued(), null)
  update(() => ({ sidePins: editedPins }))
  await prefs.flush()
  assert.deepEqual(batches, [{ sidePins: localPins, theme: 'dark' }, { theme: 'dark' }, { sidePins: editedPins }])
  assert.deepEqual(prefs.saved().sidePins, editedPins)
  assert.equal(prefs.queued(), null)
})

function reloadZone(attempted, get) {
  const localStorage = storage()
  const changes = { timeZone: attempted }
  localStorage.setItem(pendingKey, JSON.stringify(changes))
  const calls = []
  const prefs = prefsHarness(changes, () => {
    calls.push('GET')
    return get()
  }, posted => {
    calls.push('POST')
    assert.deepEqual(posted, changes)
    throw new api.ApiError(400, 'Invalid preference: timeZone.')
  }, localStorage)
  return { prefs, calls }
}

test('prefs restores Auckland after the reload POST is rejected, before polling', async () => {
  const attempted = 'America/Coyhaique'
  const { prefs, calls } = reloadZone(attempted, () => ({
    prefs: { timeZone: 'Pacific/Auckland', theme: 'dark' }, version: 1, zone: 'Pacific/Auckland',
  }))
  const [current, , rejectedZone] = await prefs.pull()
  assert.equal(zone.tetherZone(), 'Pacific/Auckland')
  assert.equal(current.timeZone, 'Pacific/Auckland')
  assert.equal(current.theme, 'dark')
  assert.equal(prefs.saved().timeZone, 'Pacific/Auckland')
  assert.equal(prefs.queued(), null)
  assert.deepEqual(calls, ['POST', 'GET'])
  assert.match(renderTimeZoneSetting(rejectedZone), /Tether could not use America\/Coyhaique on the server\. It still uses Pacific\/Auckland\./)
})

test('prefs keeps the attempted zone when the recovery GET fails and reports the zone after the next pull', async () => {
  const attempted = 'America/Coyhaique'
  let online = false
  const { prefs, calls } = reloadZone(attempted, () => {
    if (!online) throw new Error('Offline')
    return { prefs: {}, version: 1, zone: 'Pacific/Auckland' }
  })
  const [current, , rejectedZone] = await prefs.pull()
  assert.equal(zone.tetherZone(), attempted)
  assert.equal(current.timeZone, attempted)
  assert.equal(prefs.saved().timeZone, attempted)
  assert.equal(prefs.queued(), null)
  assert.deepEqual(calls, ['POST', 'GET'])
  assert.match(renderTimeZoneSetting(rejectedZone), /Tether could not use America\/Coyhaique on the server\. It will use the server’s zone once it reconnects\./)

  online = true
  const [restored, , rejectionAfterPull] = await prefs.pull()
  assert.equal(restored.timeZone, undefined)
  assert.equal(zone.tetherZone(), 'Pacific/Auckland')
  assert.equal(prefs.saved().timeZone, undefined)
  assert.deepEqual(calls, ['POST', 'GET', 'GET'])
  assert.match(renderTimeZoneSetting(rejectionAfterPull), /Tether could not use America\/Coyhaique on the server\. It still uses Pacific\/Auckland\./)
})

function offline() { throw new Error('Offline') }

test('prefs displays the rejected zone after a second reload while GET still fails', async () => {
  const attempted = 'America/Coyhaique'
  const { prefs, calls } = reloadZone(attempted, offline)
  await prefs.pull()
  assert.deepEqual(calls, ['POST', 'GET'])
  const reloaded = prefsHarness(undefined, offline, undefined, prefs.localStorage)
  const [current, , rejectedZone] = await reloaded.pull()
  assert.equal(current.timeZone, attempted)
  assert.match(renderTimeZoneSetting(rejectedZone), /Tether could not use America\/Coyhaique on the server\. It will use the server’s zone once it reconnects\./)
})

test('prefs restores Auckland after a second reload and keeps the note for this visit', async () => {
  const attempted = 'America/New_York'
  const stored = { prefs: { timeZone: 'Pacific/Auckland' }, version: 1, zone: 'Pacific/Auckland' }
  const { prefs } = reloadZone(attempted, offline)
  await prefs.pull()
  let get = offline
  const reloaded = prefsHarness(undefined, () => get(), undefined, prefs.localStorage)
  await reloaded.pull()
  get = () => stored
  const [restored, , rejectedZone] = await reloaded.pull()
  assert.equal(restored.timeZone, 'Pacific/Auckland')
  assert.equal(reloaded.saved().timeZone, 'Pacific/Auckland')
  assert.equal(reloaded.localStorage.getItem(rejectedKey), null)
  assert.match(renderTimeZoneSetting(rejectedZone), /Tether could not use America\/New York on the server\. It still uses Pacific\/Auckland\./)
  assert.equal(prefsHarness(undefined, stored, undefined, prefs.localStorage).render()[2], undefined)
})

test('prefs clears the zone marker as soon as a new zone is sent', async () => {
  const { prefs } = reloadZone('America/Coyhaique', offline)
  await prefs.pull()
  assert.notEqual(prefs.localStorage.getItem(rejectedKey), null)
  const reloaded = prefsHarness(undefined, offline, changes => {
    assert.deepEqual(changes, { timeZone: 'Europe/London' })
    return offline()
  }, prefs.localStorage)
  const [, update, previousNote] = await reloaded.pull()
  update(() => ({ timeZone: 'Europe/London' }))
  await reloaded.flush()
  assert.deepEqual([reloaded.render()[2], reloaded.localStorage.getItem(rejectedKey)], [undefined, null])
  assert.equal(previousNote, 'America/Coyhaique')
  assert.deepEqual(JSON.parse(reloaded.queued()), { timeZone: 'Europe/London' })
})

test('prefs reads legacy rejection markers and preserves rejected local content', async () => {
  const localStorage = storage()
  localStorage.setItem(rejectedKey, JSON.stringify(['sidePins', 'timeZone']))
  const sidePins = [{ pane: '%1', note: 'Rejected local note' }]
  const stored = { prefs: { sidePins: [], timeZone: 'Pacific/Auckland' }, version: 1, zone: 'Pacific/Auckland' }
  const prefs = prefsHarness({ sidePins, timeZone: 'America/Coyhaique' }, stored, undefined, localStorage)
  const [restored] = await prefs.pull()
  assert.equal(restored.timeZone, 'Pacific/Auckland')
  assert.deepEqual(plain(restored.sidePins), sidePins)
  const reloaded = prefsHarness(undefined, stored, undefined, localStorage)
  const [current] = await reloaded.pull()
  assert.deepEqual(plain(current.sidePins), sidePins)
})

function renderTimeZoneSetting(rejectedZone) {
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const setting = { exports: {} }
  const imports = {
    react: React, 'react/jsx-runtime': require('react/jsx-runtime'),
    './SelectMenu': { SelectMenu: () => React.createElement('button', {}, 'Time zone menu') },
    './zone': { ...zone, useTetherZone: zone.tetherZone },
  }
  runInNewContext(ts.transpileModule(readFileSync('src/TimeZoneSetting.tsx', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, { exports: setting.exports, require: name => imports[name] })
  return renderToStaticMarkup(React.createElement(setting.exports.TimeZoneSetting, { onChange() {}, rejectedZone }))
}

test('Time zone settings renders a live status below the menu after rejection', () => {
  zone.setTetherZone('Pacific/Auckland')
  const html = renderTimeZoneSetting('America/Coyhaique')
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
