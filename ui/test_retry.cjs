// Real TSX with controlled hooks and fake time. No DOM or network requests.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const jsx = require('react/jsx-runtime')
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve() }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const nodes = tree => !tree || typeof tree !== 'object' ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)]
const text = tree => typeof tree === 'string' ? tree : tree && typeof tree === 'object' ? [tree.props?.children].flat(Infinity).map(text).join('') : ''
const find = (tree, type) => { const node = nodes(tree).find(n => n.type === type); assert.ok(node, `Missing ${type}`); return node }
function harness() {
  let cursor = 0, pending = [], dirty = false
  const slots = []
  const hooks = {
    useState(initial) {
      const id = cursor++
      if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial
      return [slots[id], value => { const next = typeof value === 'function' ? value(slots[id]) : value; if (!Object.is(next, slots[id])) dirty = true; slots[id] = next }]
    },
    useRef(initial) { const id = cursor++; return slots[id] ??= { current: initial } },
    useId() { return `retry-test-${cursor++}` },
    useEffect(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => !Object.is(dep, old.deps[at]))) pending.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: fn() } })
    },
  }
  hooks.useLayoutEffect = hooks.useEffect
  return { hooks, render(fn, props) {
    let tree, turns = 0
    do {
      assert.ok(turns++ < 20, 'Render loop')
      cursor = 0; dirty = false; pending = []
      tree = fn(props)
      const effects = pending; pending = []; effects.forEach(effect => effect())
    } while (dirty)
    return tree
  }, unmount() { slots.forEach(slot => slot?.cleanup?.()) } }
}
function load(file, imports = {}, globals = {}, suffix = '') {
  const exports = {}
  const source = ts.transpileModule(readFileSync(`src/${file}`, 'utf8') + suffix, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  runInNewContext(source, {
    exports, Error, TypeError, ReferenceError, RangeError, SyntaxError, EvalError, URIError, DOMException, AbortController, AbortSignal, console,
    fetch() { throw Error('Network forbidden') },
    require(name) {
      if (name === 'react/jsx-runtime') return jsx
      if (name in imports) return imports[name]
      throw Error(`Unexpected import: ${name}`)
    }, ...globals,
  })
  return exports
}
const { errorText, ApiError, NetworkError } = load('api.ts')
const logged = []
const realConsoleError = console.error
console.error = (...args) => { logged.push(args) }
try {
  for (const [error, expected] of [
    [new DOMException('signal timed out', 'TimeoutError'), 'Tether took too long to answer.'],
    [new NetworkError(new TypeError('Failed to fetch')), 'Tether cannot reach the server. Check the connection.'],
    [new Error('A useful error'), 'A useful error'],
  ]) { assert.equal(errorText(error), expected); assert.doesNotMatch(errorText(error), /signal timed out/i) }
  assert.equal(logged.length, 0, 'timeouts, network failures and planned errors are not logged as bugs')

  // A code TypeError is a bug in Tether, never a network failure: even when its text mentions fetch.
  for (const bug of [new TypeError("Cannot read properties of undefined (reading 'panes')"), new TypeError('Failed to fetch'), new ReferenceError('x is not defined')]) {
    const text = errorText(bug)
    assert.doesNotMatch(text, /cannot reach/i, `${bug.name}: ${bug.message}`)
    assert.match(text, /^Something went wrong in Tether\./)
    assert.ok(text.includes(bug.message))
  }
  assert.equal(logged.length, 3, 'each code error goes to console.error')
} finally { console.error = realConsoleError }

// reach(): only a rejection from fetch() itself becomes a NetworkError. Timeouts keep their name.
async function reachTests() {
  const failing = load('api.ts', {}, { fetch: async () => { throw new TypeError('Failed to fetch') } })
  await assert.rejects(failing.reach('/x'), error => error instanceof failing.NetworkError && error.cause instanceof TypeError)
  const timing = load('api.ts', {}, { fetch: async () => { throw new DOMException('signal timed out', 'TimeoutError') } })
  await assert.rejects(timing.reach('/x'), error => error.name === 'TimeoutError')
  const offline = load('api.ts', {}, { fetch: async () => { throw new TypeError('Failed to fetch') } })
  await assert.rejects(offline.request('sessions'), error => offline.errorText(error) === 'Tether cannot reach the server. Check the connection.')
}

async function main() {
  await reachTests()
  let now = 10000
  const timers = [], h = harness()
  const Spinner = () => null, RotateCw = () => null
  const { RetryButton, MIN_BUSY } = load('Retry.tsx', {
    react: h.hooks, 'lucide-react': { RotateCw }, './Skeleton': { Spinner }, './Retry.css': {},
  }, { Date: class extends Date { static now() { return now } }, setTimeout(fn, delay) { timers.push({ fn, at: now + delay }) } })
  assert.equal(MIN_BUSY, 600)
  const advance = async ms => { now += ms; for (const timer of timers.splice(0)) { if (timer.at <= now) timer.fn(); else timers.push(timer) }; await flush() }
  const request = deferred()
  let calls = 0
  const props = { onRetry: () => { calls++; return request.promise } }
  let tree = h.render(RetryButton, props)
  assert.match(find(tree, 'button').props.className, /link-button/)
  find(tree, 'button').props.onClick()
  tree = h.render(RetryButton, props)
  assert.equal(calls, 1)
  assert.equal(find(tree, 'button').props.disabled, true)
  assert.equal(find(tree, 'button').props['aria-busy'], true)
  assert.equal(text(tree), 'Trying again…')
  assert.ok(nodes(tree).some(n => n.type === Spinner))
  find(tree, 'button').props.onClick(); assert.equal(calls, 1)
  request.reject(Error('Still down')); await flush()
  await advance(MIN_BUSY - 1)
  tree = h.render(RetryButton, props); assert.equal(find(tree, 'button').props.disabled, true)
  await advance(1)
  tree = h.render(RetryButton, props)
  assert.equal(find(tree, 'button').props.disabled, false)
  assert.match(text(tree), /Still failing\. Tried at /)
  assert.ok(nodes(tree).some(n => n.props?.role === 'status'))

  const slow = deferred(), slowProps = { onRetry: () => slow.promise }
  tree = h.render(RetryButton, slowProps); find(tree, 'button').props.onClick()
  await advance(MIN_BUSY + 1)
  tree = h.render(RetryButton, slowProps); assert.equal(find(tree, 'button').props.disabled, true)
  slow.resolve(); await flush()
  tree = h.render(RetryButton, slowProps); assert.match(text(tree), /Still failing/)

  h.unmount()
  const busyHost = harness()
  const BusyRetry = load('Retry.tsx', { react: busyHost.hooks, 'lucide-react': { RotateCw }, './Skeleton': { Spinner }, './Retry.css': {} }, { Date }).RetryButton
  const hostProps = { onRetry() {}, busy: false, label: 'Retry', className: 'secondary' }
  tree = busyHost.render(BusyRetry, hostProps)
  assert.doesNotMatch(text(tree), /Still failing/)
  tree = busyHost.render(BusyRetry, { ...hostProps, busy: true })
  assert.equal(find(tree, 'button').props.disabled, true); assert.equal(text(tree), 'Trying again…')
  tree = busyHost.render(BusyRetry, hostProps)
  assert.match(find(tree, 'button').props.className, /secondary/); assert.match(text(tree), /^RetryStill failing/)
  busyHost.unmount()

  // Failed load -> mounted, busy retry -> failure -> successful load removes retry.
  const retryStub = () => null
  async function hostCycle(file, exported, props, imports, globals = {}, suffix = '') {
    const host = harness(), requests = []
    const api = () => { const next = deferred(); requests.push(next); return next.promise }
    const component = load(file, { react: host.hooks, './Retry': { RetryButton: retryStub },
      './api': { errorText, ApiError }, ...imports }, { ...globals, fetch: api }, suffix)[exported]
    const hostProps = { ...props, api }
    let view = host.render(component, hostProps)
    if (exported === 'ToolRow') { find(view, 'button').props.onClick(); view = host.render(component, hostProps) }
    requests[0].reject(Error('First failure')); await flush()
    view = host.render(component, hostProps)
    find(view, retryStub).props.onRetry(); view = host.render(component, hostProps)
    assert.equal(find(view, retryStub).props.busy, true)
    assert.match(text(view), /First failure|live terminal is not running/)
    requests[1].reject(Error('Second failure')); await flush()
    view = host.render(component, hostProps)
    assert.equal(find(view, retryStub).props.busy, false)
    find(view, retryStub).props.onRetry(); view = host.render(component, hostProps)
    assert.equal(find(view, retryStub).props.busy, true)
    requests[2].resolve(file === 'ChangesPanel.tsx' ? { diff: '', note: 'No changes' } : file === 'TerminalPanel.tsx' ? { pane: '%fake' } : file === 'ToolGroup.tsx' ? { detail: 'Loaded', status: 'complete' } : { ok: true })
    await flush(); view = host.render(component, hostProps)
    assert.equal(nodes(view).some(n => n.type === retryStub), false)
    host.unmount()
  }
  await hostCycle('ChangesPanel.tsx', 'FileDiffView', { query: 'fixture', id: 'diff', label: 'file' }, {
    './Skeleton': { CodeBones: 'loading', SkeletonRows: 'loading' }, './skeletonCounts': { rememberCount() {}, rememberedCount: n => n }, 'lucide-react': {}, './panelProps': {},
  }, {}, '\nexport { FileDiffView }')
  await hostCycle('ToolGroup.tsx', 'ToolRow', { tool: { id: 'tool', name: 'Read', title: '', status: 'complete' }, session: 'fixture', onOpen() {} }, {
    './Skeleton': { Loading: 'loading' }, 'lucide-react': {}, './ImageViewer': {}, './Notices': {}, './toolSummary': { toolSeconds: () => null, toolKind: () => 'read' },
  }, {}, '\nexport { ToolRow }')
  await hostCycle('TerminalPanel.tsx', 'TerminalPanel', { pane: { id: '%fake' }, item: { id: 'fixture' }, onItem() {} }, { './Terminal': { LiveTerminal: 'terminal' } })
  await hostCycle('Terminal.tsx', 'LiveTerminal', { paneId: '%fake', onChat() {} }, {
    'lucide-react': { SquareTerminal: 'icon' }, './Messages': {}, './clipboard': {}, './terminalRows': {}, './terminalMouse': {},
  })
  console.log('retry tests passed: errorText (network vs code errors), reach, immediate feedback, minimum duration, pending promise, busy transition, mounted host retries (mocked hooks; no DOM or network)')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
