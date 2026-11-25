const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(name, imports = {}) {
  const exports = {}
  runInNewContext(ts.transpileModule(readFileSync(`src/${name}.ts`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, { exports, require: name => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`)
    return imports[name]
  } })
  return exports
}

const { findPane, sessionChoice, findSession, fridayHomePane } = load('fridayNames', { './Sidebar': {
  paneTitle: pane => pane.title,
  sessionName: pane => pane.label.includes(':') ? pane.label.slice(0, pane.label.lastIndexOf(':')) : pane.label,
} })
const chats = [
  { title: 'Reports Draft', agent: 'codex', tmux_session: '$1', label: 'Weekly Reporting:0.0' },
  { title: 'Reports', agent: 'claude', tmux_session: '$1', label: 'Weekly Reporting:0.1' },
  { title: 'New landing page', agent: 'antigravity', tmux_session: '$2', label: 'docs-site' },
]

assert.equal(findPane(chats, 'the Reports chat'), chats[1])
assert.equal(findPane(chats, 'draft'), chats[0])
assert.equal(findPane(chats, 'Claude'), chats[1])
assert.equal(findPane(chats, 'docs site'), chats[2])
assert.equal(findPane(chats, '$2'), undefined)
const theDocs = { ...chats[2], label: 'the docs' }
assert.equal(findPane([theDocs], 'the docs session'), theDocs)
assert.equal(findPane(chats, 'Weekly Reporting'), undefined)
assert.equal(findPane(chats, ''), undefined)
assert.equal(sessionChoice(chats, 'the weekly-reporting session').error,
  'Weekly Reporting is a tmux session with 2 chats: Reports Draft, Reports. Ask which one.')
assert.equal(sessionChoice(chats, 'Docs site'), null)
const review = { ...chats[2], label: 'Review tab' }
const shortReview = { ...chats[0], label: 'Review' }
assert.equal(findPane([shortReview, review], 'Review tab'), review)

const sessions = ['Backend tasks', 'Backend', 'docs-site', 'Docs Lab', 'Backend lab'].map((name, index) => ({
  id: `$${index}`, name, panes: [{ tmux_session: `$${index}` }],
}))
assert.equal(findSession(sessions, 'Backend'), sessions[1])
assert.equal(findSession(sessions, 'backend'), sessions[1])
assert.equal(findSession(sessions, 'tasks'), sessions[0])
assert.equal(findSession(sessions, 'docs site'), sessions[2])
assert.equal(findSession(sessions, 'the docs-site session'), sessions[2])
assert.equal(findSession(sessions, 'docs').error, 'Several sessions match: docs-site, Docs Lab. Ask which one.')
assert.equal(findSession(sessions, 'lab').error, 'Several sessions match: Docs Lab, Backend lab. Ask which one.')
assert.equal(findSession(sessions, 'unrelated'), undefined)
assert.equal(findSession(sessions, '$2'), undefined)
assert.equal(findSession(sessions, ' --- '), undefined)
console.log('Friday names: chat title, agent, session, exact and partial matches, ambiguity, and no match pass')

const home = 'Friday chats'
const cwd = '/projects/docs'
const pane = (id, tmux_session, label) => ({ id, tmux_session, label })

function setup(...lists) {
  const calls = []
  const waits = []
  const api = async (path, body) => {
    calls.push([path, body])
    if (path === 'sessions') return { sessions: lists.shift() ?? [] }
    if (path === 'tmux-sessions') return { session: '$8', name: home }
    if (path === 'split') return { pane: '%9' }
    assert.fail(`Unexpected path: ${path}`)
  }
  return { calls, waits, open: () => fridayHomePane(api, home, cwd, async ms => { waits.push(ms) }) }
}

for (const existing of [[pane('%3', '$7', home)], [pane('%3', '$7', `${home}:0.0`), pane('%4', '$7', `${home}:0.1`)]]) {
  test(`Friday splits its existing home with ${existing.length} pane(s)`, async () => {
    const { calls, waits, open } = setup(existing)
    const result = await open()
    assert.deepEqual({ ...result }, { pane: '%9', created: false })
    assert.deepEqual(calls.map(([path, body]) => [path, body && { ...body }]), [
      ['sessions', undefined], ['split', { session: '%3', direction: 'right', cwd }],
    ])
    assert.deepEqual(waits, [])
  })
}

test('Friday creates a home when only similarly named sessions exist', async () => {
  const { calls, open } = setup(
    [pane('%4', '$4', 'Friday chats test'), pane('%5', '$5', 'Friday chats2')],
    [pane('%8', '$8', home)],
  )
  const result = await open()
  assert.deepEqual({ ...result }, { pane: '%8', created: true })
  assert.deepEqual(calls.map(([path]) => path), ['sessions', 'tmux-sessions', 'sessions'])
})

test('Friday waits for the new home by its session ID', async () => {
  const wrong = pane('%3', '$7', home)
  const { calls, waits, open } = setup([], [wrong], [wrong, pane('%8', '$8', `${home}:0.0`)])
  const result = await open()
  assert.deepEqual({ ...result }, { pane: '%8', created: true })
  assert.deepEqual(calls.map(([path, body]) => [path, body && { ...body }]), [
    ['sessions', undefined], ['tmux-sessions', { name: home, cwd }], ['sessions', undefined], ['sessions', undefined],
  ])
  assert.deepEqual(waits, [300])
})

test('Friday reports a new home whose pane never appears', async () => {
  const { calls, waits, open } = setup([])
  await assert.rejects(open, { message: 'The session Friday chats started, but its pane is not visible yet.' })
  assert.equal(calls.filter(([path]) => path === 'sessions').length, 21)
  assert.equal(calls.filter(([path]) => path === 'tmux-sessions').length, 1)
  assert.deepEqual(waits, Array(20).fill(300))
})
