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

const sidebar = load('Sidebar.tsx', { react: { lazy: () => {} } })
const { approvalTarget } = load('Live.tsx', { './Sidebar': sidebar })

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
