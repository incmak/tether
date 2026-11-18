// Real component contracts with controlled hooks and buffers. No terminal writes.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const jsx = require('react/jsx-runtime')
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve() }
function harness() {
  let cursor = 0, pending = []
  const slots = []
  const hooks = {
    useState(initial) {
      const id = cursor++
      if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial
      return [slots[id], value => { slots[id] = typeof value === 'function' ? value(slots[id]) : value }]
    },
    useRef(initial) { const id = cursor++; return slots[id] ??= { current: initial } },
    useId() { const id = cursor++; return slots[id] ??= `fixture-${id}` },
    useEffect(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old.deps[at])) pending.push(() => { old?.cleanup?.(); slots[id] = { deps, cleanup: fn() } })
    },
    useCallback(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old.deps[at])) slots[id] = { fn, deps }
      return slots[id].fn
    },
  }
  return { hooks, render(fn, props) { cursor = 0; const tree = fn(props); const effects = pending; pending = []; effects.forEach(fn => fn()); return tree } }
}
const nodes = tree => !tree || typeof tree !== 'object' ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)]
const text = tree => typeof tree === 'string' ? tree : tree && typeof tree === 'object' ? [tree.props?.children].flat(Infinity).map(text).join('') : ''
const find = (tree, predicate) => { const found = nodes(tree).find(predicate); assert.ok(found, 'Expected control not found'); return found }
const button = (tree, label) => find(tree, n => n.type === 'button' && text(n) === label)
const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText
function load(file, imports, globals = {}, suffix = '') {
  const exports = {}
  runInNewContext(compile(readFileSync(file, 'utf8') + suffix), {
    exports, require: name => name === 'react/jsx-runtime' ? jsx : imports[name] ?? new Proxy({}, { get: (_, key) => key }), ...globals,
  })
  return exports
}
async function main() {
  const writes = [], h = harness()
  let picked = null
  const selectionListeners = new Set()
  const component = load('src/Terminal.tsx', { react: h.hooks, './terminalRows': load('src/terminalRows.ts', {}), './clipboard': { copyText: async value => writes.push(value) } }, {
    localStorage: { getItem: () => null }, fetch: async () => ({ ok: true }),
    window: { getSelection: () => picked },
    document: {
      addEventListener: (type, listener) => { assert.equal(type, 'selectionchange'); selectionListeners.add(listener) },
      removeEventListener: (type, listener) => { assert.equal(type, 'selectionchange'); selectionListeners.delete(listener) },
    },
  }).LiveTerminal
  const props = { paneId: '%fixture', onChat() {}, shell: true }
  let tree = h.render(component, props); await flush(); tree = h.render(component, props)
  let lines = [
    { isWrapped: false, translateToString: () => 'https://example.test/long-' },
    { isWrapped: true, translateToString: () => 'link' },
    { isWrapped: false, translateToString: () => 'second line' },
  ]
  const selectControl = button(tree, 'Select text')
  const iframe = find(tree, n => n.type === 'iframe')
  iframe.props.ref.current = { contentWindow: { term: { rows: 3, cols: 80, buffer: { active: { viewportY: 10, getLine: row => lines[row - 10] } } } } }
  selectControl.props.onClick(); tree = h.render(component, props)
  const field = find(tree, n => n.type === 'pre')
  const snapshot = 'https://example.test/long-link\nsecond line'
  assert.equal(field.props.children.props.text, snapshot)
  assert.equal(field.props.contentEditable, undefined)
  assert.equal(find(tree, n => n.type === 'iframe').props.inert, true)
  const selectionNode = {}
  field.props.ref.current = { contains: node => node === selectionNode }
  picked = { isCollapsed: false, anchorNode: selectionNode, focusNode: selectionNode, toString: () => snapshot.slice(0, 29) }
  selectionListeners.forEach(listener => listener()); tree = h.render(component, props)
  await button(tree, 'Copy selection').props.onClick(); await flush()
  assert.equal(writes[0], snapshot.slice(0, 29))
  field.props.onPointerUp({ pointerType: 'mouse' }); await flush()
  assert.equal(writes.length, 2)
  lines = [{ isWrapped: false, translateToString: () => 'new output' }]
  tree = h.render(component, props)
  assert.equal(find(tree, n => n.type === 'pre').props.children.props.text, snapshot)
  picked.isCollapsed = true
  selectionListeners.forEach(listener => listener()); tree = h.render(component, props)
  await button(tree, 'Copy all').props.onClick(); await flush()
  assert.equal(writes[2], snapshot)
  button(tree, 'Back to terminal').props.onClick(); tree = h.render(component, props)
  assert.equal(nodes(tree).some(n => n.type === 'pre'), false)
  assert.equal(selectionListeners.size, 0)
  assert.equal(find(tree, n => n.type === 'iframe').props.inert, false)
  assert.equal(find(tree, n => n.type === 'iframe').key, iframe.key)
  console.log('PASS terminal viewport, wrapped links, frozen selection, mouse/phone copy controls and return')

  const ch = harness(), typed = []
  const rovingRows = load('src/rovingRows.ts', { react: ch.hooks })
  const dock = load('src/Dock.tsx', { react: ch.hooks, './rovingRows': rovingRows }).ChoiceDock
  const url = 'https://claude.com/cai/oauth/authorize?state=' + 'a'.repeat(600)
  const dockProps = { approval: null, attention: { id: 'login', reason: 'prompt', login: { url, code: true } }, agent: 'Claude', disabled: false,
    onType: async (...args) => { typed.push(args); return true }, onChoose() {}, onKey() {} }
  tree = ch.render(dock, dockProps)
  find(tree, n => n.type === 'input').props.onChange({ target: { value: '  code-value  ' } })
  tree = ch.render(dock, dockProps)
  find(tree, n => n.type === 'form').props.onSubmit({ preventDefault() {} }); await flush()
  assert.deepEqual(typed, [['code-value', url]])
  tree = ch.render(dock, dockProps)
  assert.equal(find(tree, n => n.type === 'input').props.value, '')
  assert.ok(text(tree).includes('Code sent.'))
  console.log('PASS native login sends code with login identity and shows submission status')

  const ah = harness(), calls = []
  const accountApi = async (path, body) => { calls.push([path, body]); return { available: true, claude: [], codex: [] } }
  const saved = load('src/AccountLogin.tsx', { react: ah.hooks, './host': { useHost: () => 'test host' } }, {}, '\nexport { SavedList }').SavedList
  const accountProps = { agent: 'claude', api: accountApi, onList() {}, onDone() {} }
  tree = ah.render(saved, accountProps); await flush(); tree = ah.render(saved, accountProps)
  assert.deepEqual(calls, [['accounts', undefined]])
  button(tree, 'Load saved accounts').props.onClick(); await flush()
  assert.equal(calls[1][0], 'accounts/refresh')
  assert.equal(calls[1][1].agent, 'claude')
  console.log('PASS saved account loading requires an explicit action')

  const ownedHooks = harness(), accountCalls = []
  let pollState = { phase: 'idle', url: null, user_code: null, message: '', account: null }, done = 0
  const owned = load('src/AccountLogin.tsx', { react: ownedHooks.hooks,
    './host': { useHost: () => 'test host' }, './api': { errorText: error => error.message },
    './hooks': { usePolling: () => ({ data: pollState, refresh: async () => {} }) },
  }).AccountLogin
  const ownedProps = { agent: 'claude', onDone: () => done++, api: async (path, body) => {
    accountCalls.push([path, body])
    return { ...pollState, phase: path === 'login/start' ? 'waiting_code' : 'waiting_browser', url }
  } }
  tree = ownedHooks.render(owned, ownedProps); tree = ownedHooks.render(owned, ownedProps)
  assert.equal(accountCalls.length, 0)
  button(tree, 'Switch account').props.onClick(); tree = ownedHooks.render(owned, ownedProps)
  find(tree, n => n.type === 'form').props.onSubmit({ preventDefault() {} }); await flush()
  tree = ownedHooks.render(owned, ownedProps)
  assert.equal(accountCalls[0][0], 'login/start')
  find(tree, n => n.type === 'input' && n.props.id === 'claude-login-code').props.onChange({ target: { value: 'code-value' } })
  tree = ownedHooks.render(owned, ownedProps)
  find(tree, n => n.type === 'form').props.onSubmit({ preventDefault() {} }); await flush()
  assert.equal(accountCalls[1][0], 'login/code')
  assert.equal(accountCalls[1][1].code, 'code-value')
  pollState = { ...pollState, phase: 'done', message: 'Signed in.' }
  ownedHooks.render(owned, ownedProps); tree = ownedHooks.render(owned, ownedProps)
  assert.ok(text(tree).includes('Signed in.')); assert.equal(done, 1)
  pollState = { ...pollState, phase: 'failed', message: 'Sign-in failed. Try again.' }
  ownedHooks.render(owned, ownedProps); tree = ownedHooks.render(owned, ownedProps)
  assert.ok(text(tree).includes('Sign-in failed.'))
  console.log('PASS account flow starts explicitly, submits code and displays success/failure')

  const quickHooks = harness(), quickCalls = [], Dock = () => null
  quickHooks.hooks.useLayoutEffect = quickHooks.hooks.useEffect
  const quick = load('src/QuickReply.tsx', { react: quickHooks.hooks,
    './api': { ApiError: class ApiError extends Error {}, errorText: error => error.message, chatMessages: data => Array.isArray(data?.messages) ? data.messages : [] },
    './hooks': { usePolling: () => ({ data: { attention: dockProps.attention, messages: [], thread: 'conversation', writable: true }, refresh: async () => {} }) },
    './Dock': { ChoiceDock: Dock }, './Sidebar': { paneTitle: () => 'Fixture', agoSeconds: () => 'now', agentNames: { claude: 'Claude' } },
  }).QuickReply
  const quickProps = { pane: { id: '%fixture', agent: 'claude' }, api: async (...args) => quickCalls.push(args), focus: false, onSuccess() {} }
  tree = quickHooks.render(quick, quickProps)
  await find(tree, n => n.type === Dock).props.onType('code-value', url); await flush()
  assert.equal(quickCalls[0][0], 'type')
  assert.equal(quickCalls[0][1].login_url, url)
  assert.equal(quickCalls[0][1].session, '%fixture')
  assert.equal(quickCalls[0][1].thread, 'conversation')
  assert.equal(quickCalls[0][1].enter, true)
  console.log('PASS pinned quick reply keeps the same pane, conversation and login identity')

  const link = load('src/LoginLink.tsx', {}).LoginLink
  tree = link({ url })
  assert.equal(find(tree, n => n.type === 'a' && n.props.className === 'login-url').props.children, url)
  assert.equal(find(tree, n => n.props.label === 'Copy link').props.text, url)
  console.log('PASS full URL is visible, tappable and copied unchanged')

  const copied = []
  const native = load('src/clipboard.ts', {}, { navigator: { clipboard: { writeText: async value => copied.push(value) } } }).copyText
  await native(url); assert.equal(copied[0], url)
  let removed = false, restored = false, selected = false
  class Element { focus() { restored = true } }
  const fallback = load('src/clipboard.ts', {}, { navigator: {}, HTMLElement: Element, document: {
    activeElement: new Element(), body: { append() {} }, execCommand: command => command === 'copy',
    createElement: () => ({ style: {}, select() { selected = true }, remove() { removed = true } }),
  } }).copyText
  await fallback(url); assert.ok(removed && restored && selected)
  console.log('PASS Clipboard API and HTTP native-copy fallback')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
