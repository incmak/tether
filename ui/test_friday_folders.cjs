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
const { fridayChatFolder } = load('fridayFolders', { './fridaySessions': load('fridaySessions') })
const { fridayHomePane } = load('fridayNames', { './Sidebar': {
  sessionName: pane => pane.label.includes(':') ? pane.label.slice(0, pane.label.lastIndexOf(':')) : pane.label,
} })
const folder = (panes, target, project) => JSON.parse(JSON.stringify(fridayChatFolder(panes, target, project, 'Friday chats')))
const panes = ['~/apps/tether', '~/apps/acme', '~'].map(dir => ({ dir }))
for (const project of ['tether', 'TETHER', 'teather', 'teth', 'ether', ' tether ']) {
  assert.deepEqual(folder(panes, undefined, project), { dir: '~/apps/tether', project: 'tether' })
}
assert.deepEqual(folder(panes, undefined, 'Ac me'), { dir: '~/apps/acme', project: 'acme' })
assert.deepEqual(folder(panes, { dir: '~/apps/acme' }), { dir: '~/apps/acme', project: 'acme' })
for (const dir of ['~', '~/']) {
  for (const label of ['Friday chats', 'Other']) {
    assert.match(folder([...panes, { dir }], { dir, tmux_session: '$1', label }).error, /^Which project\?/)
  }
}
assert.deepEqual(folder(panes, undefined, 'zebra'), { error: 'Which project? Known projects: tether, acme' })
for (const path of ['~/apps/new', '/projects/new']) {
  assert.deepEqual(folder(panes, undefined, path), { dir: path, project: 'new' })
}
assert.deepEqual(folder(panes, panes[1], '  '), { dir: panes[1].dir, project: 'acme' })
assert.match(folder(panes, panes[1], 'zebra').error, /^Which project\?/)
assert.match(folder([], undefined).error, /^Which project\?/)
const duplicate = [...panes, { dir: '/work/tether' }]
assert.match(folder(duplicate, undefined, 'tether').error, /^Which project\?/)
assert.equal(folder([...duplicate, { dir: '/work/tether' }], undefined, 'tether').dir, '/work/tether')
assert.match(folder([...duplicate, { dir: '~/apps/tether-ui' }], undefined, 'teth').error, /^Which project\?/)
assert.equal(folder([...panes, { dir: '~/apps/tether-ui' }], undefined, 'tether').dir, '~/apps/tether')
assert.equal(folder([...panes, { dir: '~/apps/my-tether' }], undefined, 'teth').dir, '~/apps/tether')
assert.deepEqual(folder([{ dir: '/abcde' }], undefined, 'abxye'), { dir: '/abcde', project: 'abcde' })
assert.match(folder([{ dir: '/abcde' }], undefined, 'abxyz').error, /^Which project\?/)
const many = Array.from({ length: 10 }, (_, i) => ({ dir: `/projects/p${i}` }))
assert.equal(folder([...many, many[9], { dir: '' }], undefined, 'zebra').error,
  'Which project? Known projects: p9, p0, p1, p2, p3, p4, p5, p6')
console.log('Friday folders: project matching, target fallback, home exclusion, paths, ambiguity, counts and errors pass')

// Exercise Live's actual create method and tool handler with API calls mocked.
const liveSource = ts.createSourceFile('Live.tsx', readFileSync('src/Live.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let createMethod, newChatHandler
function visit(node) {
  if (ts.isMethodDeclaration(node) && node.name.getText(liveSource) === 'create') createMethod = node
  if (ts.isPropertyAssignment(node) && node.name.getText(liveSource) === 'new_chat') newChatHandler = node.initializer
  ts.forEachChild(node, visit)
}
visit(liveSource)
assert.ok(createMethod && newChatHandler)
async function testLive(hasSession, project, target) {
  const calls = []
  let created = false
  const api = async (path, body) => {
    calls.push({ path, body })
    if (path === 'sessions') return { sessions: hasSession
      ? [{ id: '%1', tmux_session: '$1', label: 'Friday chats' }]
      : created ? [{ id: '%2', tmux_session: '$2', label: 'Friday chats' }] : [] }
    if (path === 'split') return { pane: '%2' }
    if (path === 'tmux-sessions') {
      created = true
      return { session: '$2', name: 'Friday chats' }
    }
    return {}
  }
  const context = {
    fridayChatFolder, fridayHomePane, live: { current: { panes, target, api } },
    FRIDAY_SESSION: 'Friday chats', SESSION_NAME: /^[A-Za-z0-9 _-]+$/,
    setTargetId: () => {}, flow: { current: {} }, wait: async () => assert.fail('Unexpected wait'),
  }
  runInNewContext(ts.transpileModule(
    `globalThis.create = ({${createMethod.getText(liveSource)}}).create; globalThis.newChat = ${newChatHandler.getText(liveSource)}`, {
      compilerOptions: { target: ts.ScriptTarget.ES2020 },
    }).outputText, context)
  context.flow.current.create = context.create
  if (project === 'zebra') {
    await assert.rejects(context.newChat({ name: 'New chat', project }), /Which project\?/)
    assert.deepEqual(calls, [])
    return
  }
  const result = await context.newChat({ name: 'New chat', project })
  assert.deepEqual(JSON.parse(JSON.stringify(result)), { ok: true, chat: 'New chat', folder: '~/apps/tether' })
  const write = calls.find(call => call.path === (hasSession ? 'split' : 'tmux-sessions'))
  assert.equal(write.body.cwd, '~/apps/tether')
  assert.deepEqual(calls.slice(-2).map(({ path, body }) => [path, { ...body }]), [
    ['pane-name', { pane: '%2', name: 'New chat', keep_session: true }],
    ['start', { session: '%2', thread: null, agent: 'claude' }],
  ])
}
Promise.all([
  testLive(true, 'tether'), testLive(false, 'tether'),
  testLive(true, undefined, panes[0]), testLive(true, 'zebra'),
]).then(() => console.log('Friday creation: both paths pass cwd, preserve the session name, and reject unknown projects before API calls'))
  .catch(error => { console.error(error); process.exitCode = 1 })
// A parent folder wins over its subfolders; a repo with nested folders.
const nested = ['~/acme-v2', '~/acme-v2/acme-release-bot',
  '~/acme-v2/acme-be-v2', '~/apps/tether'].map(dir => ({ dir }))
assert.equal(folder(nested, undefined, 'Acme').dir, '~/acme-v2')
assert.equal(folder(nested, undefined, 'release bot').dir, '~/acme-v2/acme-release-bot')
