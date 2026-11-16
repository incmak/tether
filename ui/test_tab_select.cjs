// Multi-select of tabs (src/tabSelection.ts): Ctrl/Cmd+click toggles, Shift+click picks a range, closed tabs drop out,
// and the bulk close sends each tab's chat so the server can refuse a stale one.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const output = {}
const api = { errorText: error => error.message }
runInNewContext(ts.transpileModule(readFileSync('src/tabSelection.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: output, require: name => { assert.equal(name, './api'); return api } })
const { pickTab, keepLive, pickMode, closeTabs, stamp, choose, NO_SELECTION } = output
const order = ['a', 'b', 'c', 'd', 'e']
const ids = selection => [...selection.ids]

let picked = pickTab(NO_SELECTION, 'c', 'toggle', order, 'a')
assert.deepEqual(ids(picked), ['a', 'c'], 'First Ctrl+click keeps the open tab and adds the clicked one')
picked = pickTab(picked, 'e', 'toggle', order, 'a')
assert.deepEqual(ids(picked), ['a', 'c', 'e'], 'Next Ctrl+click adds')
picked = pickTab(picked, 'c', 'toggle', order, 'a')
assert.deepEqual(ids(picked), ['a', 'e'], 'Ctrl+click on a chosen tab removes it')
assert.deepEqual(ids(pickTab(NO_SELECTION, 'a', 'toggle', order, 'a')), ['a'], 'Ctrl+click on the open tab chooses only it')
assert.deepEqual(ids(pickTab(NO_SELECTION, 'd', 'range', order, 'b')), ['b', 'c', 'd'], 'Shift+click picks from the open tab')
assert.deepEqual(ids(pickTab(NO_SELECTION, 'a', 'range', order, 'c')), ['a', 'b', 'c'], 'Shift+click to the left picks the range backwards')
const anchored = pickTab(NO_SELECTION, 'b', 'toggle', order, 'b')
assert.deepEqual(ids(pickTab(pickTab(anchored, 'e', 'range', order), 'c', 'range', order)), ['b', 'c'], 'A second Shift+click resizes the range from the same anchor')
assert.deepEqual(ids(pickTab(NO_SELECTION, 'd', 'range', order, 'gone')), ['d'], 'Shift+click without a known anchor picks the tab only')
assert.deepEqual(ids(pickTab({ ids: ['x'], anchor: 'x' }, 'c', 'range', order)), ['c'], 'An anchor from the other list starts again')
console.log('Tab select: toggle, range, anchor pass')

const chats = entries => new Map(entries)
const all = chats(order.map(id => [id, `t-${id}`]))
const live = stamp({ ids: ['a', 'c'], anchor: 'c' }, all)
assert.deepEqual({ ...live.threads }, { a: 't-a', c: 't-c' }, 'Choosing a tab records its chat')
assert.deepEqual({ ...stamp({ ...live, ids: ['a', 'c', 'd'] }, chats([['a', 'new'], ['c', 't-c'], ['d', 't-d']])).threads }, { a: 't-a', c: 't-c', d: 't-d' }, 'A later pick keeps the chats already recorded')
assert.equal(keepLive(live, all), live, 'Nothing changed: the same object, no render')
assert.deepEqual(JSON.parse(JSON.stringify(keepLive(live, chats([['a', 't-a']])))), { ids: ['a'], anchor: null, threads: { a: 't-a' } }, 'A closed tab drops out with its anchor')
assert.deepEqual([...keepLive(live, chats([['a', 't-a'], ['c', 'other']])).ids], ['a'], 'A tab that now holds another chat drops out')
let made = choose(choose(NO_SELECTION, all, 'b', 'toggle', order, 'a'), all, 'c', 'toggle', order, 'a')
assert.deepEqual({ ...made.threads }, { a: 't-a', b: 't-b', c: 't-c' }, 'Each pick records the chats of the chosen tabs')
const replaced = chats(order.map(id => [id, id === 'a' ? 'other' : `t-${id}`]))
made = choose(made, replaced, 'd', 'toggle', order, 'b')
assert.deepEqual([...made.ids], ['b', 'c', 'd'], 'A later pick drops a tab whose chat changed; it does not come back with the new chat')
assert.deepEqual({ ...made.threads }, { b: 't-b', c: 't-c', d: 't-d' }, 'Tabs that stay chosen keep their first chat')
made = choose(made, replaced, 'a', 'toggle', order, 'b')
assert.equal(made.threads.a, 'other', 'Choosing that tab again records its new chat')
const ranged = choose(stamp({ ids: ['b'], anchor: 'b' }, all), chats(order.map(id => [id, id === 'b' ? 'other' : `t-${id}`])), 'd', 'range', order, 'b')
assert.deepEqual({ ...ranged.threads }, { b: 'other', c: 't-c', d: 't-d' }, 'A range after a change starts from the live chats')
assert.deepEqual([...keepLive(stamp({ ids: ['e'], anchor: 'e' }, chats([['e', null]])), chats([['e', 't-e']])).ids], [], 'A tab with no chat that starts one drops out too')
assert.equal(pickMode({ shiftKey: true, ctrlKey: false, metaKey: false }, false), 'range')
assert.equal(pickMode({ shiftKey: false, ctrlKey: true, metaKey: false }, false), 'toggle')
assert.equal(pickMode({ shiftKey: false, ctrlKey: false, metaKey: true }, false), 'toggle')
assert.equal(pickMode({ shiftKey: false, ctrlKey: false, metaKey: false }, true), 'toggle', 'A tap while choosing picks')
assert.equal(pickMode({ shiftKey: false, ctrlKey: false, metaKey: false }, false), null, 'A plain click opens the tab')
console.log('Tab select: closed tabs and click modes pass')

;(async () => {
  const calls = []
  const fake = async (path, body, _signal, method) => { calls.push({ path, body, method }); if (body.pane === 'b') throw new Error('That tab now holds another chat. Reload and try again.') }
  const result = await closeTabs(fake, [{ id: 'a', thread: 't-a' }, { id: 'b', thread: 't-b' }, { id: 'c', thread: null }])
  assert.deepEqual(calls.map(call => [call.path, call.method, call.body.pane, call.body.thread]), [['session', 'DELETE', 'a', 't-a'], ['session', 'DELETE', 'b', 't-b'], ['session', 'DELETE', 'c', null]], 'One DELETE per tab, each with the chat it showed')
  assert.deepEqual([...result.closed], ['a', 'c'], 'The others still close')
  assert.deepEqual([...result.failed].map(item => [item.pane.id, item.error]), [['b', 'That tab now holds another chat. Reload and try again.']], 'A refused tab reports why')
  calls.length = 0
  await closeTabs(fake, [{ id: 'a', thread: 'newer' }], { a: 't-a' })
  assert.deepEqual(calls.map(call => call.body.thread), ['t-a'], 'The close names the chat seen when the tab was chosen, not a newer one')
  console.log('Tab select: bulk close per tab with stale-chat refusal passes')
})().catch(error => { console.error(error); process.exit(1) })
