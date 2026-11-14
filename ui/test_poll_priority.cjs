// B67: background polls ask for low fetch priority, and the agent browser poll waits for each answer before it schedules the next.
// Real TS/TSX with stubbed imports and fake time. No DOM or network requests.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve() }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const icons = new Proxy({}, { get: () => () => null })

function load(file, imports = {}, globals = {}) {
  const exports = {}
  const source = ts.transpileModule(readFileSync(`src/${file}`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  runInNewContext(source, {
    exports, Error, TypeError, DOMException, AbortController, AbortSignal, Promise, Object, Array, JSON, console,
    fetch() { throw Error('Network forbidden') },
    require(name) {
      if (name === 'react/jsx-runtime') return require('react/jsx-runtime')
      if (name === 'lucide-react') return icons
      if (name.endsWith('.css')) return {}
      if (name in imports) return imports[name]
      throw Error(`Unexpected import: ${name}`)
    }, ...globals,
  })
  return exports
}

// Fake timers shared by the window stubs below.
let now = 0, timers = []
const fakeWindow = {
  setTimeout: (fn, ms) => { timers.push({ fn, at: now + ms }); return timers.length },
  clearTimeout: id => { if (timers[id - 1]) timers[id - 1].fn = null },
  setInterval: (fn, ms) => { const timer = { fn, at: now + ms, every: ms }; timers.push(timer); return timers.length },
  clearInterval: id => { if (timers[id - 1]) timers[id - 1].fn = null },
  addEventListener() {}, removeEventListener() {},
}
const advance = async ms => {
  const end = now + ms
  for (;;) {
    const next = timers.filter(timer => timer.fn && timer.at <= end).sort((a, b) => a.at - b.at)[0]
    if (!next) break
    now = next.at; const fn = next.fn
    if (next.every) next.at += next.every; else next.fn = null
    fn(); await flush()
  }
  now = end; await flush()
}

async function requestPriority() {
  const seen = []
  const { request } = load('api.ts', {}, { fetch: async (url, init) => { seen.push({ url, init }); return { ok: true, status: 200, json: async () => ({}) } } })
  await request('browser/agents', undefined, undefined, undefined, undefined, 'low')
  await request('sessions')
  await request('send', { text: 'x' })
  assert.equal(seen[0].init.priority, 'low', 'A background poll asks for low priority')
  assert.equal('priority' in seen[1].init, false, 'A plain GET keeps the default priority')
  assert.equal('priority' in seen[2].init, false, 'A write keeps the default priority')
}

async function agentBrowserChain() {
  now = 0; timers = []
  let subscribe = null
  const react = { useEffect() {}, useRef: value => ({ current: value }), useState: value => [value, () => {}], useSyncExternalStore: (sub, get) => { subscribe = sub; return get() } }
  const mod = load('AgentBrowser.tsx', { react, './api': { errorText: String }, './BrowserPanel': {}, './backNav': {} },
    { window: fakeWindow, document: { visibilityState: 'visible' }, clearTimeout: fakeWindow.clearTimeout, clearInterval: fakeWindow.clearInterval })
  const calls = []
  const api = (path, body, signal, method, timeout, priority) => {
    const call = { path, priority, ...deferred() }; calls.push(call); return call.promise
  }
  mod.useAgentBrowser(api)
  const stop = subscribe(() => {})
  await flush()
  assert.deepEqual(calls.map(c => [c.path, c.priority]), [['browser/agents', 'low']], 'The first poll starts at once, at low priority')
  await advance(20000)
  assert.equal(calls.length, 1, 'No new poll starts while the slow answer is still out')
  calls[0].resolve({ agents: [{ tabs: ['t1'] }] }); await flush()
  assert.deepEqual(calls.slice(1).map(c => [c.path, c.priority]), [['browser/signed/access', 'low']], 'The signed access check is low priority too')
  calls[1].resolve({}); await flush()
  assert.deepEqual(calls.slice(2).map(c => [c.path, c.priority]), [['browser/tabs', 'low']], 'Tab titles are low priority too')
  calls[2].resolve({ tabs: [] }); await flush()
  await advance(3999)
  assert.equal(calls.length, 3, 'The next poll waits the full interval after the last answer')
  await advance(1)
  assert.equal(calls.length, 4, 'The next poll starts 4 s after the last answer')
  stop()
  calls[3].resolve({ agents: [] }); await flush()
  calls[4]?.resolve({}); await flush()
  const before = calls.length
  await advance(30000)
  assert.equal(calls.length, before, 'No poll runs after the last listener leaves, even when one was in flight')
  // A user action still goes out at the default priority.
  const user = []
  await mod.changeAgentBrowser((path, body, signal, method, timeout, priority) => { user.push([path, priority]); return Promise.resolve(path === 'browser/agents' ? { agents: [{ tabs: ['t1'] }] } : { tabs: [] }) }, 'browser/agents', { enabled: true })
  assert.deepEqual(user, [['browser/agents', undefined], ['browser/tabs', undefined]], 'User actions keep the default priority')
}

async function backlogBadge() {
  let fetcher = null
  const react = { useCallback: fn => fn, useEffect() {}, useState: init => [typeof init === 'function' ? init() : init, () => {}] }
  const mod = load('backlogNews.ts', { react, './backlogItems': { parseBacklog: () => [] }, './hooks': { usePolling: fn => { fetcher = fn; return {} } } },
    { localStorage: { getItem: () => null, setItem() {} }, window: fakeWindow })
  const seen = []
  mod.useBacklogNews((path, body, signal, method, timeout, priority) => { seen.push([path, priority]); return Promise.resolve({ text: '' }) })
  await fetcher(new AbortController().signal)
  assert.deepEqual(seen, [['projects/backlog', 'low']], 'The backlog badge poll is low priority')
}

async function projectMarks() {
  const effects = []
  const react = { useCallback: fn => fn, useEffect: fn => effects.push(fn), useState: init => [init, () => {}] }
  const stub = {}
  const mod = load('Project.tsx', { react, './Skeleton': stub, './VoiceField': stub, './SelectMenu': stub, './api': { ApiError: Error, errorText: String }, './hooks': stub, './CodexLevel': stub, './Autonomy': stub },
    { setInterval: () => 1, clearInterval() {} })
  const seen = []
  mod.useProjectMarks((path, body, signal, method, timeout, priority) => { seen.push([path, priority]); return Promise.resolve({}) }, true)
  effects.forEach(effect => effect())
  assert.deepEqual(seen, [['projects/marks', 'low']], 'The sidebar marks poll is low priority')
}

async function shellList() {
  now = 0; timers = []
  const effects = []
  const react = { useCallback: fn => fn, useEffect: fn => effects.push(fn), useState: init => [init, () => {}] }
  const seen = []
  const sideShells = { SHELLS_CHANGED: 'x', endShell() {}, endShells() {}, shellName: String, shellsChanged() {}, listShells: (api, priority) => { seen.push(priority); return Promise.resolve([]) } }
  const mod = load('ShellList.tsx', { react, './api': { errorText: String }, './sideShells': sideShells },
    { window: fakeWindow, document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} }, setInterval: fakeWindow.setInterval, clearInterval: fakeWindow.clearInterval })
  mod.ShellList({ api: () => {}, open: true, onToggle() {}, onEnded() {} })
  effects.forEach(effect => effect())
  assert.deepEqual(seen, [undefined], 'Opening the group loads at the default priority')
  await advance(5000)
  assert.deepEqual(seen, [undefined, 'low'], 'The timed refresh of a shown list is low priority')
}

;(async () => {
  await requestPriority()
  await agentBrowserChain()
  await backlogBadge()
  await projectMarks()
  await shellList()
  console.log('Poll priority (B67): low for background polls, default for user actions; agent browser polls never stack')
})().catch(error => { console.error(error); process.exit(1) })
