const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
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

const { findPane, sessionChoice, findSession } = load('fridayNames', { './Sidebar': { paneTitle: pane => pane.title } })
const chats = [
  { title: 'Reports Draft', agent: 'codex', tmux_session: 'Weekly Reporting' },
  { title: 'Reports', agent: 'claude', tmux_session: 'Weekly Reporting' },
  { title: 'New landing page', agent: 'antigravity', tmux_session: 'docs-site' },
]

assert.equal(findPane(chats, 'the Reports chat'), chats[1])
assert.equal(findPane(chats, 'draft'), chats[0])
assert.equal(findPane(chats, 'Claude'), chats[1])
assert.equal(findPane(chats, 'docs site'), chats[2])
assert.equal(findPane(chats, 'Weekly Reporting'), undefined)
assert.equal(findPane(chats, ''), undefined)
assert.equal(sessionChoice(chats, 'the weekly-reporting session').error,
  'Weekly Reporting is a tmux session with 2 chats: Reports Draft, Reports. Ask which one.')
assert.equal(sessionChoice(chats, 'Docs site'), null)

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
