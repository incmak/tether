// Real grouping and shared answers with isolated data and fake API calls. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const compile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText
function load(file, imports = {}, globals = {}) {
  const exports = {}
  runInNewContext(compile(readFileSync(`src/${file}`, 'utf8')), {
    exports, Error, ...globals,
    require(name) {
      if (name in imports) return imports[name]
      if (['react', 'react/jsx-runtime'].includes(name)) return require(name)
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  return exports
}
const plain = value => JSON.parse(JSON.stringify(value))
const paneState = load('paneState.ts')
const agentNames = { claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', shell: 'Shell' }
const needs = load('needs.ts', { './Sidebar': { agentNames }, './paneState': paneState })
const { homeGroups, cardTime, hasAgents } = load('homeGroups.ts', { './needs': needs, './paneState': paneState })
const pane = (id, extra = {}) => ({ id, tmux_session: 'work', agent: 'codex', state: 'Idle', thread: `thread-${id}`, ...extra })
const session = panes => ({ id: 'work', name: 'Work', panes })
const ids = cards => Array.from(cards, card => card.pane.id)
const approval = pane('approval', { state: 'Approval', since: 30, approval: { id: 'permit', actionable: true, text: 'Allow?', options: [{ id: 1, text: 'Yes' }, { id: 2, text: 'No' }] } })
const question = pane('question', { agent: 'antigravity', state: 'Input needed', since: 10, needs: { kind: 'question', text: 'Choose' } })
const noTime = pane('no-time', { state: 'Input needed' })
const tie = pane('a-tie', { state: 'Approval', since: 30 })
const working = pane('working', { state: 'Working', since: 5, tasks: ['build', 'test'] })
const workTie = pane('a-working', { state: 'Working', since: 5 })
const workNoTime = pane('working-no-time', { state: 'Working' })
const browser = pane('browser', { state: 'Working', since: 1 })
const done = pane('done', { finished: 50 })
const recent = pane('recent', { agent: 'claude', finished: 60 })
const doneTie = pane('a-done', { finished: 50 })
const shell = pane('shell', { agent: 'shell', state: 'Approval', finished: 99 })
const shellWorking = pane('shell-working', { agent: 'shell', state: 'Working' })
const shellDone = pane('shell-done', { agent: 'shell', finished: 100 })
const archived = pane('archived', { state: 'Input needed', since: 20, tmux_session: 'hidden' })
const archivedWorking = pane('archived-working', { state: 'Working' })
const archivedDone = pane('archived-done', { finished: 100 })
const visible = [approval, question, noTime, tie, working, workTie, workNoTime, browser, done, recent, doneTie, shell, shellWorking, shellDone]
const all = [...visible, archived, archivedWorking, archivedDone]
const sessions = [session(visible)]
const asks = [{ id: 'ask', pane: browser.id, text: 'Sign in', at: 15, tab: 'tab' }, { id: 'shell-ask', pane: shell.id, at: 1 }, { id: 'gone', pane: 'gone', at: 1 }]
let groups = homeGroups(all, sessions, {}, undefined, asks)
assert.deepEqual(ids(groups.needs), ['question', 'browser', 'archived', 'a-tie', 'approval', 'no-time'])
assert.deepEqual(ids(groups.working), ['a-working', 'working', 'working-no-time'])
assert.deepEqual(ids(groups.done), ['recent', 'a-done', 'done'])
assert.equal(groups.working.find(card => card.pane.id === 'working').text, 'build, test')
assert.equal(groups.working[0].text, '')
assert.equal(groups.done[0].text, 'Claude finished its turn')
assert.equal(groups.needs.find(card => card.pane.id === 'archived').session, undefined)
assert.equal(groups.working[0].session, sessions[0])
assert.equal(groups.done[0].session, sessions[0])
assert.equal(groups.needs[0].session, sessions[0])
for (const seen of [50, 51]) assert(!ids(homeGroups(all, sessions, { done: seen }, undefined, asks).done).includes('done'))
assert(ids(homeGroups(all, sessions, { done: 49 }, undefined, asks).done).includes('done'))
assert(!ids(homeGroups(all, sessions, {}, done.id, asks).done).includes('done'))
assert(ids(homeGroups(all, sessions, {}, undefined, asks).done).includes('done'))
groups = homeGroups(all.filter(item => item.id !== browser.id), [session(visible.filter(item => item.id !== browser.id))], {}, undefined, asks)
assert(!Object.values(groups).flat().some(card => card.pane.id === browser.id))
assert.deepEqual(plain(homeGroups([], [], {}, undefined, asks)), { needs: [], working: [], done: [] })
assert.equal(hasAgents([]), false)
assert.equal(hasAgents([shell]), false)
assert.equal(hasAgents([shell, done]), true)
const stoppedPane = pane('stopped', { state: 'Stopped', since: 7, finished: 90 })
const stoppedAsk = pane('stopped-ask', { state: 'Stopped' })
const stoppedOnly = homeGroups([stoppedPane, stoppedAsk, working], [session([stoppedPane, stoppedAsk, working])], {}, undefined, [{ id: 'ask-2', pane: stoppedAsk.id, text: 'Sign in', at: 3 }])
assert.deepEqual(ids(stoppedOnly.needs), ['stopped-ask', 'stopped'])
assert.equal(stoppedOnly.needs[1].kind, 'stopped')
assert.equal(needs.KIND_LABEL[stoppedOnly.needs[1].kind], 'Stopped')
assert.equal(stoppedOnly.needs[1].session.id, 'work')
assert.deepEqual(ids(stoppedOnly.working), ['working'])
assert.deepEqual(ids(stoppedOnly.done), [])
console.log('Home grouping: groups, shells, order, missing times, seen/selected, browser asks, archives, removed panes and sessions pass')

const now = 1000000
const time = (seconds, kind = 'working') => cardTime({ kind, at: now - seconds }, now)
for (const [seconds, expected] of [[0, '0s'], [42, '42s'], [59, '59s'], [60, '1m 00s'], [185, '3m 05s'], [3599, '59m 59s'], [3600, '1h 00m'], [4020, '1h 07m'], [86399, '23h 59m'], [86400, '1d 0h'], [183600, '2d 3h']]) assert.equal(time(seconds), expected)
for (const [seconds, expected] of [[0, 'Just now'], [59, 'Just now'], [60, '1m ago'], [240, '4m ago'], [3599, '59m ago'], [3600, '1h ago'], [10800, '3h ago'], [86399, '23h ago'], [86400, '1d ago'], [172800, '2d ago']]) assert.equal(time(seconds, 'done'), expected)
for (const kind of ['done', 'working', 'approval', 'browser']) {
  for (const at of [null, undefined, 0]) assert.equal(cardTime({ kind, at }, now), '')
  assert.equal(cardTime({ kind, at: now + 100 }, now), kind === 'done' ? 'Just now' : '0s')
}
console.log('Card time: formats, 60s/1h/24h boundaries, missing times and clock skew pass')

const apiModule = load('api.ts', { './zone': load('zone.ts') })
const browserCalls = []
const browserModule = {
  watchTab: (...args) => browserCalls.push(args),
  changeAgentBrowser: async (...args) => { browserCalls.push(args); if (browserModule.error) throw browserModule.error },
}
const imports = { './api': apiModule, './AgentBrowser': browserModule }
const { sendAnswer, answerError, optionForKey, isAnswerKey } = load('useAnswers.ts', imports)
const { AnswerActions } = load('answers.tsx', imports)
const approvalItem = needs.inboxItems([approval], {})[0]
const menuItem = { pane: question, kind: 'question', text: 'Pick', at: 10, menu: [{ id: 1, text: 'First' }, { id: 2, text: 'Second' }, { id: 3, text: 'Third' }] }
const stale = new apiModule.ApiError(409, 'Browser changed')
assert.equal(answerError(stale), 'This pane changed. Open it to check.')
assert.equal(answerError(new Error('Offline')), 'Offline')
assert.equal(answerError(new apiModule.ApiError(500, 'Server failed')), 'Server failed')
assert.equal(answerError(null), apiModule.errorText(null))
assert.equal(optionForKey('KeyY', approvalItem), approvalItem.yes)
assert.equal(optionForKey('KeyN', approvalItem), approvalItem.no)
for (let digit = 1; digit <= 3; digit++) for (const prefix of ['Digit', 'Numpad']) {
  assert.equal(optionForKey(prefix + digit, menuItem), menuItem.menu[digit - 1])
  assert.equal(isAnswerKey(prefix + digit), true)
}
for (const code of ['KeyY', 'KeyN']) { assert.equal(isAnswerKey(code), true); assert.equal(optionForKey(code, menuItem), undefined) }
for (const code of ['KeyA', 'Digit0', 'Digit4', 'Numpad4', 'ArrowDown', 'Enter', 'y']) {
  assert.equal(isAnswerKey(code), false)
  assert.equal(optionForKey(code, approvalItem), undefined)
}
const props = { api() {}, label: 'Work', waiting: false, onAnswer() {}, onFail() {} }
let html = renderToStaticMarkup(React.createElement(AnswerActions, { ...props, item: approvalItem }))
assert(html.includes('aria-label="Answer Work"'))
assert(html.includes('title="Yes"'))
assert(html.includes('Yes <kbd>Y</kbd>'))
assert(html.includes('No <kbd>N</kbd>'))
html = renderToStaticMarkup(React.createElement(AnswerActions, { ...props, item: menuItem }))
assert(html.includes('aria-label="Answer Work"'))
for (const option of menuItem.menu) assert(html.includes(`<kbd>${option.id}</kbd><span>${option.text}</span>`))
html = renderToStaticMarkup(React.createElement(AnswerActions, { ...props, item: approvalItem, waiting: true }))
assert.equal((html.match(/disabled=""/g) || []).length, 2)
assert.equal(renderToStaticMarkup(React.createElement(AnswerActions, { ...props, item: { pane: done, kind: 'done' } })), '')
console.log('Shared answers: errors, key mapping, approval/menu markup, labels, disabled state and no-actions pass')

// Minimal hooks harness verifies state and focus without a browser or terminal.
function harness() {
  let cursor = 0, effects = []
  const slots = []
  const hooks = {
    useState(initial) {
      const id = cursor++
      if (!(id in slots)) slots[id] = typeof initial === 'function' ? initial() : initial
      return [slots[id], value => { slots[id] = typeof value === 'function' ? value(slots[id]) : value }]
    },
    useRef(initial) { const id = cursor++; return slots[id] ??= { current: initial } },
    useEffect(fn, deps) {
      const id = cursor++, old = slots[id]
      if (!old || deps.some((dep, at) => dep !== old[at])) { slots[id] = deps; effects.push(fn) }
    },
  }
  return { hooks, render(fn, props) { cursor = 0; const result = fn(props); const pending = effects; effects = []; pending.forEach(fn => fn()); return result } }
}
async function main() {
  const calls = []
  const api = async (path, body) => { calls.push([path, plain(body)]) }
  await sendAnswer(api, approvalItem, approvalItem.yes)
  await sendAnswer(api, menuItem, menuItem.menu[1])
  assert.deepEqual(calls, [
    ['approval', { session: approval.id, thread: approval.thread, approval: 'permit', choice: 1 }],
    ['menu', { session: question.id, thread: question.thread, choice: 2, label: 'Second' }],
  ])
  const browserItem = { pane: browser, kind: 'browser', ask: 'ask', tab: 'tab' }
  const failures = []
  const browserTree = AnswerActions({ ...props, api, item: browserItem, onFail: (...args) => failures.push(args) })
  const browserButtons = browserTree.props.children[0].props.children
  browserButtons[0].props.onClick()
  browserModule.error = stale
  browserButtons[1].props.onClick()
  await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(browserCalls[0], ['tab', browser.id])
  assert.equal(browserCalls[1][0], api)
  assert.equal(browserCalls[1][1], 'browser/ask/dismiss')
  assert.deepEqual(plain(browserCalls[1][2]), { id: 'ask' })
  assert.deepEqual(failures, [[browser.id, stale]])

  const h = harness(), document = { body: {} }, focused = []
  document.activeElement = document.body
  const shared = load('useAnswers.ts', { ...imports, react: h.hooks }, { document })
  let updates = 0, reject, resolve
  const hookProps = { api: () => new Promise((ok, fail) => { resolve = ok; reject = fail }), items: [approvalItem, menuItem], active: true, onUpdate: () => updates++, focusRow: id => focused.push(id) }
  const render = () => h.render(shared.useAnswers, hookProps)
  let result = render()
  const event = (code, extra = {}) => ({ code, prevented: false, stopped: false, preventDefault() { this.prevented = true }, stopPropagation() { this.stopped = true }, ...extra })
  for (const modifier of ['ctrlKey', 'metaKey', 'altKey', 'shiftKey']) {
    const key = event('KeyY', { [modifier]: true }); result.onAnswerKey(key, approvalItem); assert.equal(key.prevented, false)
  }
  const unavailable = event('KeyY'); result.onAnswerKey(unavailable, menuItem)
  assert(unavailable.prevented && unavailable.stopped)
  const first = result.answer(approvalItem, approvalItem.yes)
  result = render(); assert.equal(result.busy, approval.id)
  await result.answer(menuItem, menuItem.menu[0]) // Busy answers must not replace the pending request.
  resolve(); await first
  assert.equal(updates, 1)
  hookProps.items = [menuItem]; result = render()
  assert.deepEqual(focused, [question.id])
  result = render(); assert.equal(result.busy, null)
  const failed = result.answer(menuItem, menuItem.menu[0]); reject(stale); await failed
  result = render(); assert.equal(result.errors[question.id], 'This pane changed. Open it to check.')
  assert.equal(updates, 2)
  result.fail(browser.id, stale); result = render()
  assert.equal(result.errors[browser.id], 'Browser changed', 'Browser dismissal retains its original error text')
  hookProps.items = []; render(); result = render(); assert.deepEqual(plain(result.errors), {})
  console.log('Shared answers: fake approval/menu POSTs, browser controls, modifiers, key suppression, busy guard, focus, errors and pruning pass')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
