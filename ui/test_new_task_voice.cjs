// Real dictation and task handlers with fake audio and API calls. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
function load(file, imports, globals = {}) {
  const exports = {}
  runInNewContext(ts.transpileModule(readFileSync(`src/${file}`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, {
    exports, Error, Blob, AbortController, DOMException, ...globals,
    require(name) {
      if (name in imports) return imports[name]
      if (['react/jsx-runtime', 'lucide-react'].includes(name)) return require(name)
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  return exports
}
function harness() {
  let cursor = 0, effects = []
  const slots = [], cleanups = []
  const hooks = {
    useState(initial) {
      const id = cursor++
      if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial
      return [slots[id], value => { slots[id] = typeof value === 'function' ? value(slots[id]) : value }]
    },
    useRef(initial) { const id = cursor++; return slots[id] ??= { current: initial } },
    useMemo(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old.deps[at])) slots[id] = { deps, value: fn() }
      return slots[id].value
    },
    useEffect(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old[at])) {
        slots[id] = deps; effects.push(() => { cleanups[id]?.(); cleanups[id] = fn() })
      }
    },
  }
  hooks.useLayoutEffect = hooks.useEffect
  return {
    hooks,
    render(fn) { cursor = 0; const result = fn(); const pending = effects; effects = []; pending.forEach(fn => fn()); return result },
    unmount() { cleanups.forEach(fn => fn?.()) },
  }
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
const deferred = () => { let resolve; const promise = new Promise(ok => { resolve = ok }); return { promise, resolve } }
function environment(label = '') {
  let now = 0, nextTimer = 1
  const timers = new Map(), records = [], calls = [], streams = []
  const response = text => ({ ok: true, headers: { get: () => 'application/json' }, json: async () => ({ text }) })
  const env = { calls, records, streams, timers, result: '', focus: 0, chimes: [], spoken: 0, fine: true }
  const stream = () => {
    const track = { label, stops: 0, stop() { this.stops++ } }
    const value = { track, getAudioTracks: () => [track], getTracks: () => [track] }
    streams.push(value); return value
  }
  class Recorder {
    static isTypeSupported() { return true }
    constructor(stream, options) { this.stream = stream; this.mimeType = options.mimeType; this.state = 'inactive'; records.push(this) }
    start() { this.state = 'recording' }
    data() { this.ondataavailable?.({ data: new Blob(['audio']) }) }
    stop() { assert.notEqual(this.state, 'inactive'); this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) }
  }
  const setTimer = (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id }
  env.globals = {
    Date: { now: () => now }, crypto: { randomUUID: () => 'fake-recording' }, MediaRecorder: Recorder,
    navigator: { mediaDevices: { getUserMedia: async () => env.acquire ? env.acquire.promise : stream() } },
    window: { setInterval: setTimer, setTimeout: setTimer, clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id) },
    matchMedia: () => ({ matches: env.fine }), localStorage: { getItem() {}, setItem() {} },
    fetch: async (path, options) => {
      calls.push({ path, options })
      if (options.headers['X-Live']) return response('rough preview')
      if (env.failure) throw env.failure
      if (env.delayed) return new Promise((resolve, reject) => {
        env.delayed.promise.then(text => resolve(response(text)))
        options.signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')))
      })
      return response(env.result)
    },
  }
  env.advance = ms => { now += ms }
  env.stream = stream
  return env
}
function setup(env, options = {}) {
  const h = harness()
  let text = options.text || '', done = 0
  const { useDictation } = load('useDictation.ts', {
    react: h.hooks, './api': { errorText: error => error.message },
    './sound': { prepareChime: () => env.chimes.push('prepare'), playChime: async () => env.chimes.push('play') },
    './speech': { stopSpeaking: () => env.spoken++ },
    './voiceLog': { reportVoice() {} },
  }, env.globals)
  const props = { text, setText: next => { text = next }, chime: false, onHandsFreeDone: () => done++, onFinished: () => env.focus++, ...options }
  return { h, useDictation, render: () => h.render(() => useDictation({ ...props, text })), text: () => text, done: () => done }
}
function nodes(tree, predicate) {
  if (Array.isArray(tree)) return tree.flatMap(child => nodes(child, predicate))
  if (!tree || typeof tree !== 'object') return []
  return [...(predicate(tree) ? [tree] : []), ...nodes(tree.props?.children, predicate)]
}
const find = (tree, predicate) => { const found = nodes(tree, predicate); assert.equal(found.length, 1); return found[0] }
const byLabel = label => node => node.props?.['aria-label'] === label
const formOf = tree => find(tree, node => node.type === 'form')
const buttonText = text => node => node.type === 'button' && node.props.children === text
const key = extra => ({ code: 'KeyV', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, prevented: false, preventDefault() { this.prevented = true }, ...extra })
function taskSetup(env) {
  const s = setup(env), requests = [], created = []
  const { NewTask } = load('NewTask.tsx', {
    react: s.h.hooks, './api': { errorText: error => error.message },
    './hooks': { useModal: () => ({}), usePopover: () => ({}) }, './Composer': { uploadFile() { throw new Error('Unexpected upload') } },
    './ChatPicker': { ChatPicker: () => null }, './Sidebar': { AgentIcon: () => null, agentNames: { claude: 'Claude', codex: 'Codex' } },
    './Worktree': { WorktreeField: () => null }, './Skeleton': { Spinner: () => null },
    './keys': { keyLabel: () => 'Alt+V' }, './useDictation': { useDictation: s.useDictation }, './NewTask.css': {},
  }, env.globals)
  const props = {
    api: async (path, body) => {
      if (path === 'task-models') return { claude: [], codex: [] }
      assert.equal(path, 'delegate')
      requests.push({ path, body }); return { pane: 'fake-new' }
    },
    pane: { id: 'fake-source', agent: 'codex', thread: null }, at: { x: 0, y: 0 }, micOk: true, chime: false,
    onCreated: id => created.push(id), onClose() {}, onEmpty() {}, onOpen() {}, onMoved() {},
  }
  const render = () => {
    const tree = s.h.render(() => NewTask(props))
    for (const node of nodes(tree, byLabel('Task'))) node.props.ref.current = { focus: () => env.focus++ }
    return tree
  }
  return { ...s, props, render, requests, created }
}
async function record(env, s) {
  await s.render().toggle(); env.advance(1000); env.records.at(-1).data()
}
async function main() {
  for (const file of ['NewTask.tsx', 'Composer.tsx']) {
    const source = readFileSync(`src/${file}`, 'utf8')
    assert.match(source, /import \{ useDictation \} from '\.\/useDictation'/)
    assert(!source.includes('getUserMedia'), `${file} must share recording logic`)
  }
  for (const label of ['USB Headset', '']) {
    const env = environment(label), s = setup(env)
    await record(env, s)
    const hook = s.render(), waiting = hook.finalText()
    assert(hook.listening && hook.active())
    hook.stop(); assert.equal(await waiting, '')
    const expected = label ? `No speech heard on ${label}. Check the mic or choose another input, then try again.` : 'No speech heard. Tap the mic and speak.'
    assert.equal(s.render().error, expected)
    assert.equal(s.done(), 0); assert.equal(env.focus, 1); assert.equal(env.timers.size, 0)
    assert.equal(env.streams[0].track.stops, 1)
  }
  console.log('Dictation: named and unnamed silent microphone errors, no auto-send, track and timer cleanup pass')

  const env = environment(), s = setup(env, { text: 'Existing draft ', chime: true })
  env.result = 'final words'
  await record(env, s)
  const waiting = s.render().finalText(); s.render().stop()
  assert.equal(await waiting, 'Existing draft final words')
  assert.equal(s.text(), 'Existing draft final words'); assert.deepEqual(env.chimes, ['prepare', 'play']); assert.equal(env.spoken, 1)
  assert.equal(s.render().finalText(), null)
  env.delayed = deferred()
  await s.render().toggle(); env.advance(2500); env.records.at(-1).data(); await flush(); s.render()
  const cancelled = s.render().finalText(); s.render().stop(); await flush()
  assert(s.render().transcribing); s.render().cancelConverting()
  assert.equal(await cancelled, 'Existing draft final words rough preview')
  assert.equal(s.render().error, '')
  console.log('Dictation: final text, append, chime, stop-speaking, live preview and conversion cancellation pass')

  for (const kind of ['speech', 'empty', 'failure', 'silence']) {
    const env = environment()
    let level = 0.1, closed = 0
    env.globals.window.AudioContext = class {
      createAnalyser() { return { fftSize: 1024, getFloatTimeDomainData: samples => samples.fill(level) } }
      createMediaStreamSource() { return { connect() {} } }
      async close() { closed++ }
    }
    env.result = kind === 'speech' ? 'final speech' : ''
    if (kind === 'failure') env.failure = new Error('Offline')
    const s = setup(env, { handsFree: true, text: 'Base' })
    await record(env, s)
    const pause = [...env.timers.values()].find(timer => timer.ms === 100).fn
    if (kind !== 'silence') pause()
    level = 0; env.advance(kind === 'silence' ? 8001 : 1901); pause(); await flush()
    assert.equal(s.done(), kind === 'speech' ? 1 : 0)
    assert.equal(s.text(), kind === 'speech' ? 'Base final speech' : 'Base')
    assert.equal(closed, 1); assert.equal(env.timers.size, 0)
  }
  console.log('Dictation: hands-free pause sends only successful new speech; empty, failed and silent recordings never auto-send')

  const closedEnv = environment(), closed = setup(closedEnv)
  await record(closedEnv, closed); closed.h.unmount(); await flush()
  assert.equal(closedEnv.streams[0].track.stops, 1); assert.equal(closedEnv.timers.size, 0); assert.equal(closedEnv.calls.length, 0)
  const lateEnv = environment(); lateEnv.acquire = deferred()
  const late = setup(lateEnv), starting = late.render().toggle()
  late.h.unmount(); lateEnv.acquire.resolve(lateEnv.stream()); await starting
  assert.equal(lateEnv.streams[0].track.stops, 1); assert.equal(lateEnv.records.length, 0)
  const convertingEnv = environment(), converting = setup(convertingEnv)
  convertingEnv.delayed = deferred()
  await record(convertingEnv, converting); converting.render().stop(); await flush()
  assert(converting.render().transcribing)
  converting.h.unmount(); await flush()
  // A chat composer unmounts on a tab switch: the accurate text must still reach the saved draft.
  assert.equal(convertingEnv.calls[0].options.signal.aborted, false)
  convertingEnv.delayed.resolve('accurate'); await flush()
  assert.equal(convertingEnv.timers.size, 0); assert.equal(converting.text(), 'accurate')
  console.log('Dictation: close discards recording, releases timers, and stops a late microphone stream pass')

  for (const shortcut of [false, true]) {
    const env = environment(), s = taskSetup(env)
    env.delayed = deferred()
    let tree = s.render()
    find(tree, byLabel('Start voice input')).props.onClick(); await flush()
    env.advance(1000); env.records[0].data(); tree = s.render()
    assert.equal(find(tree, byLabel('Task')).props.readOnly, true)
    assert.equal(find(tree, byLabel('Task')).props.disabled, undefined)
    assert.equal(find(tree, node => node.props?.className === 'primary task-create').props.disabled, false)
    if (shortcut) find(tree, byLabel('Task')).props.onKeyDown(key({ code: 'Enter', key: 'Enter', altKey: false, ctrlKey: true }))
    else formOf(tree).props.onSubmit({ preventDefault() {} })
    formOf(tree).props.onSubmit({ preventDefault() {} }); tree = s.render(); await flush()
    tree = s.render()
    assert.equal(s.requests.length, 0)
    assert.equal(find(tree, node => node.props?.className === 'primary task-create').props.disabled, true)
    assert.equal(find(tree, byLabel('Stop converting and keep the live text')).props.disabled, true)
    env.delayed.resolve('accurate task'); await flush()
    assert.equal(s.requests.length, 1); assert.equal(s.requests[0].path, 'delegate')
    assert.equal(s.requests[0].body.message, 'accurate task'); assert.deepEqual(s.created, ['fake-new'])
  }
  console.log('New task: Create and Ctrl+Enter wait for final text, set pending immediately, and block duplicate submissions pass')

  const emptyEnv = environment('Silent mic'), empty = taskSetup(emptyEnv)
  find(empty.render(), byLabel('Start voice input')).props.onClick(); await flush(); emptyEnv.advance(1000); emptyEnv.records[0].data()
  await formOf(empty.render()).props.onSubmit({ preventDefault() {} })
  let tree = empty.render()
  assert.equal(find(tree, node => node.props?.role === 'alert').props.children, 'Type a task first.')
  assert.equal(find(tree, node => node.props?.className === 'primary task-create').props.disabled, false)
  assert.equal(empty.requests.length, 0); assert.equal(empty.created.length, 0)
  find(tree, byLabel('Task')).props.onChange({ target: { value: 'typed task' } }); tree = empty.render()
  assert.equal(nodes(tree, node => node.props?.role === 'alert').length, 0)
  console.log('New task: empty final text keeps popover open, clears pending, and typing clears both errors pass')

  const keysEnv = environment(), keys = taskSetup(keysEnv)
  tree = keys.render()
  for (const modifier of ['ctrlKey', 'metaKey', 'shiftKey']) {
    const event = key({ [modifier]: true }); formOf(tree).props.onKeyDown(event); assert.equal(event.prevented, false)
  }
  keys.props.micOk = false; tree = keys.render()
  const hidden = key(); formOf(tree).props.onKeyDown(hidden); assert.equal(hidden.prevented, false)
  assert.equal(nodes(tree, byLabel('Start voice input')).length, 0)
  keys.props.micOk = true; tree = keys.render()
  const altV = key(); formOf(tree).props.onKeyDown(altV); await flush(); assert(altV.prevented)
  assert.equal(keysEnv.records.length, 1)
  keysEnv.advance(1000); keysEnv.records[0].data(); tree = keys.render()
  find(tree, buttonText('Open existing')).props.onClick(); await flush()
  assert.equal(keysEnv.records[0].state, 'inactive'); assert.equal(nodes(keys.render(), node => node.type === 'form').length, 0)
  assert.equal(keys.requests.length, 0)
  // Alt+V during conversion cancels it, like the composer's button. It must not start a second recording.
  const busyEnv = environment(), busy = taskSetup(busyEnv); busyEnv.delayed = deferred()
  find(busy.render(), byLabel('Start voice input')).props.onClick(); await flush(); busyEnv.advance(1000); busyEnv.records[0].data()
  find(busy.render(), byLabel('Stop recording')).props.onClick(); await flush()
  const cancelKey = key(); formOf(busy.render()).props.onKeyDown(cancelKey); await flush()
  assert(cancelKey.prevented); assert.equal(busyEnv.records.length, 1); assert.equal(busyEnv.calls.at(-1).options.signal.aborted, true)
  for (const fine of [true, false]) {
    const env = environment(), task = taskSetup(env); env.fine = fine; env.result = 'dictated'
    find(task.render(), byLabel('Start voice input')).props.onClick(); await flush(); env.advance(1000); env.records[0].data()
    find(task.render(), byLabel('Stop recording')).props.onClick(); await flush()
    assert.equal(env.focus, fine ? 1 : 0); assert.equal(task.requests.length, 0)
  }
  console.log('New task: Alt+V, modifier guards, mic preference, Open existing stop and desktop-only focus pass')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
