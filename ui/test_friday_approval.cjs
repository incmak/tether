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
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  })
  const exports = {}
  runInNewContext(outputText, { exports, require: name => imports[name] ?? {} }, { filename: file })
  return exports
}

const paneState = load('paneState.ts')
const sidebar = load('Sidebar.tsx', { react: { lazy: () => {} }, './paneState': paneState })
const names = load('fridayNames.ts', { './Sidebar': sidebar })
const attention = load('attention.ts', { './paneState': paneState })
const needs = load('needs.ts', { './Sidebar': sidebar, './paneState': paneState })
const { homeGroups } = load('homeGroups.ts', { './needs': needs, './paneState': paneState })
const { approvalTarget, chats, chatList } = load('Live.tsx', {
  './Sidebar': sidebar, './fridayNames': names, './fridaySessions': load('fridaySessions.ts'),
  './speech': load('speech.ts'),
})

const plain = value => JSON.parse(JSON.stringify(value))
const now = 1000
const pane = (id, pane_name, fields = {}) => ({
  id, pane_name, label: `Studio:${id.slice(1)}.0`, tmux_session: '$1', agent: 'codex',
  state: 'Idle', approval: null, ...fields,
})
const ask = (id, fields = {}) => ({
  pane: id, thread: null, since: 800, next: 'Choose a palette', kind: 'question', opened: false,
  snoozed_until: null, ...fields,
})
const question = pane('%1', 'Map', { state: 'Input needed', needs: { kind: 'question', text: 'Which route?' } })
const prompt = pane('%2', 'Catalog', {
  state: 'Approval', approval: { id: 'proof', options: [{ id: 1, text: 'Yes' }], text: 'Publish the proof?' },
})
const palette = pane('%3', 'Palette', { ask: ask('%3') })
const fixture = [question, prompt, palette,
  pane('%4', 'Frame', { ask: ask('%4', { later: { at: now, by: 'you' } }) }),
  pane('%5', 'Clay', { ask: ask('%5', { snoozed_until: now + 1 }) }),
  pane('%6', 'Console', { agent: 'shell', ask: ask('%6') }),
  pane('%7', 'Paper'),
]

function homeAttention(panes, enabled, hidden = () => false) {
  const sessions = sidebar.groupSessions(panes, hidden)
  const { needs } = homeGroups(panes, sessions, {})
  const asks = enabled ? attention.waitingAsks(sessions.flatMap(session => session.panes), now) : []
  return { asks, count: new Set([...needs, ...asks].map(item => item.pane.id)).size }
}
const agentsOf = panes => panes.filter(pane => pane.agent !== 'shell')

test('Friday reports the same waiting asks and total as Home only in Attention mode', () => {
  const agents = agentsOf(fixture)
  const on = homeAttention(fixture, true)
  const off = homeAttention(fixture, false)
  const previous = {
    target: 'Map', busy: [], idle: 4,
    needs: [{ chat: 'Map', needs: 'Which route?' }, { chat: 'Catalog', needs: 'Publish the proof?' }],
  }
  assert.equal(on.count, 3)
  assert.deepEqual(plain(chats(agents, question, on)), {
    ...previous, idle: 3, waiting: [{ chat: 'Palette', kind: 'question', text: 'Choose a palette' }], waiting_count: on.count,
  })
  assert.deepEqual(plain(chats(agents, question, off)), { ...previous, waiting: [], waiting_count: off.count })
  const previousList = {
    target: 'Map', chats: ['Map, Codex, needs you, in Studio', 'Catalog, Codex, needs you, in Studio',
      ...['Palette', 'Frame', 'Clay', 'Paper'].map(name => `${name}, Codex, idle, in Studio`)],
    sessions: ['Studio: 6 chats, 0 busy'],
  }
  const currentList = plain(chatList(agents, fixture, question, on))
  assert.deepEqual(currentList, { ...previousList, chats: previousList.chats.map(line => line.startsWith('Palette,')
    ? 'Palette, Codex, waiting on you (question: Choose a palette), in Studio' : line) })
  assert.deepEqual(plain(chatList(agents, fixture, question, off)), previousList)
  assert.deepEqual(plain(chatList(agents, fixture, question, on, 'Studio')), { session: 'Studio', chats: currentList.chats })
})

test('Friday keeps prompt priority, orders visible asks by age and keeps waiting chats out of busy', () => {
  const both = { ...question, ask: ask('%1', { next: 'A different question' }) }
  const oldest = pane('%8', 'Ink', { state: 'Working', ask: ask('%8', { since: 100, kind: 'reply', next: '' }) })
  const hidden = pane('%9', 'Archive', { ask: ask('%9') })
  const longAsk = { ...palette, ask: ask('%3', { next: 'Choose a palette. '.repeat(10).trim() }) }
  const panes = [both, longAsk, hidden, oldest]
  const agents = agentsOf(panes)
  const home = homeAttention(panes, true, pane => pane.id === hidden.id)
  const state = plain(chats(agents, undefined, home))
  assert.equal(state.waiting_count, home.count)
  assert.equal(home.count, 3)
  assert.deepEqual(state.needs, [{ chat: 'Map', needs: 'Which route?' }])
  assert.deepEqual(state.waiting, [
    { chat: 'Ink', kind: 'reply', text: '' },
    { chat: 'Palette', kind: 'question', text: 'Choose a palette. '.repeat(7) + 'Choose a…' },
  ])
  assert.deepEqual(state.busy, [])
  assert.equal(state.idle, 1)
  const list = plain(chatList(agents, panes, undefined, home))
  assert.equal(list.chats[0], 'Map, Codex, needs you, in Studio')
  assert.equal(list.chats[2], 'Archive, Codex, idle, in Studio')
  assert.equal(list.chats[3], 'Ink, Codex, waiting on you (reply), in Studio')
  assert.deepEqual(list.sessions, ['Studio: 4 chats, 0 busy'])
})

test('an ask that waits on you does not authorize a named or unnamed approval', () => {
  const askOnly = { ...palette, ask: ask('%3', { kind: 'approval', next: 'Approve the proof?' }) }
  assert.equal(approvalTarget([askOnly], 'Palette').error, 'No chat called Palette is waiting for approval. Waiting: none.')
  assert.equal(approvalTarget([askOnly], '').error, 'No chat is waiting for approval.')
  assert.equal(approvalTarget([askOnly, prompt], 'Palette').error,
    'No chat called Palette is waiting for approval. Waiting: Catalog.')
})

const waiting = (pane_name, id = '%1') => ({
  id, pane_name, agent: 'codex', approval: { id: `approval-${id}`, options: [{ id: '1', text: 'Yes' }] },
})
const reports = waiting('Weekly-reporting')
const invoices = waiting('Invoices', '%2')
const idle = { ...invoices, approval: null }

test('an unmatched name refuses even when only one chat is waiting', () => {
  assert.equal(approvalTarget([reports, idle], 'Invoices').error,
    'No chat called Invoices is waiting for approval. Waiting: Weekly-reporting.')
})

test('an exact spoken title selects the waiting chat with that title', () => {
  assert.equal(approvalTarget([invoices, reports], 'the WEEKLY reporting chat'), reports)
})

test('an unnamed approval with two waiting chats asks which one', () => {
  assert.match(approvalTarget([reports, invoices], '').error, /Weekly-reporting, Invoices\. Ask which one\./)
})

test('an unnamed approval selects the only waiting chat', () => {
  assert.equal(approvalTarget([idle, reports], ''), reports)
})

test('a substring or agent kind cannot identify a chat', () => {
  for (const chat of ['weekly', 'codex']) {
    assert.equal(approvalTarget([reports], chat).error,
      `No chat called ${chat} is waiting for approval. Waiting: Weekly-reporting.`)
  }
})

test('two matching spoken titles ask which one and list only matches', () => {
  const other = waiting('Weekly reporting', '%3')
  assert.match(approvalTarget([reports, invoices, other], 'weekly reporting').error,
    /Weekly-reporting, Weekly reporting\. Ask which one\./)
})

for (const [chat, shortTitle] of [['Auth session', 'Auth'], ['The Hub', 'Hub'], ['Friday chat', 'Friday']]) {
  test(`"${chat}" is ambiguous when both the full and stripped titles are waiting`, () => {
    const agents = [waiting(shortTitle), waiting(chat, '%2')]
    assert.equal(approvalTarget(agents, chat).error,
      `Multiple chats are waiting for approval: ${shortTitle}, ${chat}. Ask which one.`)
  })
}

for (const title of ['Auth session', 'Review tab']) {
  test(`"${title}" selects a chat with that full title`, () => {
    const pane = waiting(title)
    assert.equal(approvalTarget([pane], title), pane)
  })
}

test('"the auth chat" selects Auth when only the stripped title matches', () => {
  const pane = waiting('Auth')
  assert.equal(approvalTarget([pane], 'the auth chat'), pane)
})

test('no waiting chats produce errors for named and unnamed approvals', () => {
  const noOptions = { ...reports, approval: { id: 'approval', options: [] } }
  assert.equal(approvalTarget([idle, noOptions], '').error, 'No chat is waiting for approval.')
  assert.equal(approvalTarget([idle, noOptions], 'Invoices').error,
    'No chat called Invoices is waiting for approval. Waiting: none.')
})
