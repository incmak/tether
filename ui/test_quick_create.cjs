// The search menu's create rows: quickTab.ts (places, request bodies) and quickBacklog.ts (add with ID, 409 retry, undo).
// Real TS with stubbed imports and a fake API. No DOM or browser.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

class ApiError extends Error { constructor(status, message) { super(message); this.status = status } }
const loaded = {}
function load(file) {
  if (loaded[file]) return loaded[file]
  const module = { exports: {} }
  loaded[file] = module.exports
  const stubs = { './api': { ApiError }, './backlogNews': { BACKLOG_PATH: 'projects/backlog' } }
  const code = ts.transpileModule(readFileSync(`src/${file}.ts`, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  runInNewContext(code, { exports: module.exports, module, require: name => stubs[name] ?? load(name.slice(2)), crypto: globalThis.crypto })
  return module.exports
}
const quick = load('quickTab')
const backlog = load('quickBacklog')
const plain = value => JSON.parse(JSON.stringify(value))

async function main() {
  // Places: each session, then its stacks. Values keep the session and stack ids apart.
  const sessions = [{ id: '$1', name: 'work', groups: [{ id: 'g1', name: 'Billing' }] }, { id: '$2', name: 'site' }]
  const found = plain(quick.places(sessions))
  assert.deepEqual(found.map(quick.placeValue), ['$1|', '$1|g1', '$2|'])
  assert.deepEqual(found.map(quick.placeName), ['work', 'work · Billing', 'site'])
  assert.ok(quick.samePlace(found[1], { ...found[1], groupName: 'renamed' }))
  assert.ok(!quick.samePlace(found[0], found[1]))

  // The start body: a request id always, a checked place or the default flag.
  assert.deepEqual(plain(quick.choiceBody('Fix', { kind: 'place', place: found[1] }, 'req-1')), { request: 'req-1', message: 'Fix', session: '$1', group: 'g1' })
  assert.deepEqual(plain(quick.choiceBody('Fix', { kind: 'default' }, 'req-2')), { request: 'req-2', message: 'Fix', default: true })
  assert.match(quick.requestId(), /^[A-Za-z0-9_-]{8,64}$/, 'The server accepts the request id')
  assert.notEqual(quick.requestId(), quick.requestId())

  // Calls go to the quick-tab endpoints with the right bodies.
  const calls = []
  const api = async (path, body, signal, method, timeout) => { calls.push({ path, body: plain(body ?? null), timeout }); return { ok: true } }
  await quick.route(api, 'Fix', 'auto')
  await quick.startTab(api, 'Fix', { kind: 'default' }, 'req-3')
  await quick.moveTab(api, '%50', found[2])
  assert.deepEqual(calls.map(call => call.path), ['quick-tab/route', 'quick-tab/start', 'quick-tab/move'])
  assert.deepEqual(calls[0].body, { query: 'Fix', agent: 'auto' })
  assert.deepEqual(calls[2].body, { pane: '%50', session: '$2', group: '' })
  assert.ok(calls[0].timeout > quick.ROUTE_WAIT_MS, 'The route call outlives the 3 s wait, so a late answer can offer Move')

  // Backlog: the new item's ID, a retry after someone else saved first, and an undo of that exact line.
  let file = { text: '- [ ] Old #B1\n', version: 'v1', limit: 100000, next_id: 2 }
  let conflict = true
  const saves = []
  const store = async (path, body) => {
    assert.equal(path, 'projects/backlog')
    if (!body) return { ...file }
    saves.push(body.text)
    if (conflict) {
      conflict = false
      file = { text: '- [ ] Old #B1\n- [ ] Theirs #B2\n', version: 'v2', limit: 100000, next_id: 3 }
      throw new ApiError(409, 'changed')
    }
    if (body.version !== file.version) throw new ApiError(409, 'changed')
    const next = Math.max(file.next_id, ...[...body.text.matchAll(/#B(\d+)/g)].map(match => Number(match[1]) + 1))
    file = { text: body.text, version: `${file.version}+`, limit: file.limit, next_id: next }
    return { ...file }
  }
  const added = await backlog.addToBacklog(store, '  Write the docs  ')
  assert.equal(added.ref, 'B3', 'The ID after the other save')
  assert.match(file.text, /- \[ \] Theirs #B2\n- \[ \] Write the docs #B3\n$/)
  assert.equal(saves.length, 2, 'One retry after the 409')
  await added.undo()
  assert.equal(file.text, '- [ ] Old #B1\n- [ ] Theirs #B2\n', 'Undo removes only the new line')
  await added.undo()  // already gone: nothing to do
  assert.equal(file.text, '- [ ] Old #B1\n- [ ] Theirs #B2\n')

  // Undo refuses when the item changed after it was added.
  const again = await backlog.addToBacklog(store, 'Edit me')
  file = { ...file, text: file.text.replace('Edit me', 'Edited') }
  await assert.rejects(again.undo(), /changed since it was added/)
  assert.match(file.text, /Edited/)

  // Another client adds the same text while this save runs: Undo removes this save's item, never theirs.
  file = { text: '- [ ] Old #B1\n', version: 'w1', limit: 100000, next_id: 2 }
  let raced = false
  const racing = async (path, body) => {
    if (body && !raced) {
      raced = true
      file = { text: '- [ ] Old #B1\n- [ ] Same #B2\n', version: 'w2', limit: 100000, next_id: 3 }
    }
    return store(path, body)
  }
  conflict = false
  const mine = await backlog.addToBacklog(racing, 'Same')
  assert.equal(mine.ref, 'B3', 'Its own item, not the equal one another client saved first')
  await mine.undo()
  assert.equal(file.text, '- [ ] Old #B1\n- [ ] Same #B2\n', 'Undo leaves the other client item')

  // addedItem finds only a new open line with the same text.
  assert.equal(backlog.addedItem('- [ ] A #B1\n', '- [ ] A #B1\n- [ ] A #B2\n', 'A').ref, 'B2')
  assert.equal(backlog.addedItem('- [ ] A #B1\n', '- [ ] A #B1\n', 'A'), undefined)
  console.log('quick create: all checks passed')
}
main().catch(error => { console.error(error); process.exit(1) })
