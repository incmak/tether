// Exercise Chat's real footer code with controlled responses and timers. No terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const source = ts.createSourceFile('Chat.tsx', readFileSync('src/Chat.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const chat = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'Chat')
const declaration = (statements, name) => statements.find(node => ts.isVariableStatement(node)
  && node.declarationList.declarations.some(item => item.name.getText(source) === name)).getText(source)
const effect = chat.body.statements.find(node => ts.isExpressionStatement(node)
  && node.getText(source).includes('footerReads.current.requested')).getText(source)
const onKey = chat.body.statements.find(node => ts.isFunctionDeclaration(node) && node.name.text === 'headsUpKey').getText(source)
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const flush = async () => { for (let at = 0; at < 8; at++) await Promise.resolve() }
const good = { observed: true, goal: '10h', tasks: ['1 monitor'], notices: ['2% until auto-compact'], status: 'Opus', heads_up: { state: 'explained' } }
const blank = { observed: true, goal: '', tasks: [], notices: [], status: '', heads_up: null }

async function main() {
  const calls = [], intervals = new Map(), timers = new Map(), polls = new Map(), writes = []
  let nextTimer = 0, cleanup, shown = good, failKey = false, pendingKey
  const context = {
    pane: { id: '%fixture', thread: 'thread-1' }, data: { thread: 'thread-1' }, activePane: { agent: 'claude' }, state: 'Idle',
    document: { visibilityState: 'visible' }, footerId: '',
    readFooter: { current: () => {} }, footerReads: { current: { requested: 0, applied: 0 } }, footerReread: { current: null }, cardError: { current: false },
    api: path => new Promise((resolve, reject) => calls.push({ path, resolve, reject })),
    sendKey: async (key, card) => { writes.push({ key, card }); return pendingKey ? await pendingKey : !failKey },
    useEffect: fn => { cleanup = fn() }, setFooter: result => { shown = result.value },
    setInterval: fn => { intervals.set(++nextTimer, fn); return nextTimer }, clearInterval: id => intervals.delete(id),
    setTimeout: (fn, delay) => {
      if (delay === 700) timers.set(++nextTimer, fn)
      else { assert.ok(delay === 1000 || delay === 5000, `poll delay ${delay}`); polls.set(++nextTimer, { fn, delay }) }
      return nextTimer
    },
    clearTimeout: id => { timers.delete(id); polls.delete(id) },
  }
  runInNewContext(compile([
    declaration(source.statements, 'footerCache'), declaration(source.statements, 'footerKey'), declaration(source.statements, 'cacheFooter'),
    'globalThis.cache = footerCache; globalThis.key = footerKey;',
    `globalThis.start = () => { ${effect} };`, onKey, 'globalThis.press = headsUpKey;',
    `globalThis.select = (pane, data, claudeFooter, activePane) => { ${declaration(chat.body.statements, 'footerId')} ${declaration(chat.body.statements, 'footer')} return footer; };`,
  ].join('\n')), context)
  context.footerId = context.key('%fixture', 'thread-1')
  context.start()
  context.readFooter.current()
  calls[1].resolve(good); await flush()
  calls[0].resolve(blank); await flush()
  assert.equal(shown, good); assert.equal(context.cache.get(context.footerId), good)
  console.log('UI B1: older responses cannot overwrite state or cache')

  context.readFooter.current(); calls[2].resolve({ ...blank, observed: false }); await flush()
  const kept = { ...good, heads_up: null, suggestion: '' }
  const same = (a, b) => assert.equal(JSON.stringify(a), JSON.stringify(b))
  same(shown, kept); same(context.cache.get(context.footerId), kept)
  context.readFooter.current(); calls[3].reject(new Error('offline')); await flush()
  same(shown, kept)
  context.readFooter.current(); calls[4].resolve(blank); await flush()
  assert.equal(shown, blank); assert.equal(context.cache.get(context.footerId), blank)
  console.log('UI B2: unobserved and rejected reads preserve state; observed absence clears it')

  await context.press('1')
  assert.equal(writes[0].card, true); assert.equal(timers.size, 1)
  const retry = timers.values().next().value
  timers.clear(); retry()
  assert.equal(calls.length, 7)
  await context.press('1'); assert.equal(timers.size, 1)
  cleanup()
  assert.equal(timers.size, 0); assert.equal(intervals.size, 0); assert.equal(polls.size, 0)
  calls[7].resolve(good); await flush()
  assert.equal(shown, blank); assert.equal(context.cache.get(context.footerId), blank)
  context.readFooter.current(); assert.equal(calls.length, 8)
  console.log('UI B1: key retry fires at 700 ms; cleanup cancels timers and ignores pending reads')

  const oldState = { key: context.footerId, value: good }
  context.data = { thread: 'thread-2' }
  const newKey = context.key('%fixture', 'thread-2')
  assert.notEqual(newKey, context.footerId)
  assert.equal(JSON.stringify(context.select(context.pane, context.data, oldState, context.activePane)), '{}')
  const other = { observed: true, status: 'Other conversation' }
  context.cache.set(newKey, other)
  assert.equal(context.select(context.pane, context.data, oldState, context.activePane), other)
  assert.equal(context.select(context.pane, null, oldState, context.activePane), good)
  context.footerId = newKey; context.start()
  calls[8].resolve(other); await flush(); assert.equal(shown, other)
  pendingKey = new Promise(resolve => { context.resolveKey = resolve })
  const lateKey = context.press('1'); cleanup(); context.resolveKey(true); await lateKey
  assert.equal(timers.size, 0); assert.equal(polls.size, 0); assert.equal(calls.length, 9)
  console.log('UI B3: cache and rendered footer isolate conversations, including effect changes and late key completion')

  context.state = 'Working'; context.start()
  assert.equal(calls.length, 10); assert.equal(polls.size, 0)  // no timer while the first read is on the wire
  calls[9].resolve(good); await flush()
  assert.equal(polls.size, 1); const [[firstId, first]] = polls; assert.equal(first.delay, 1000)
  polls.delete(firstId); first.fn(); assert.equal(calls.length, 11); assert.equal(polls.size, 0)
  await flush(); assert.equal(polls.size, 0)  // a slow read never lets a second one start
  calls[10].reject(new Error('offline')); await flush()
  assert.equal(polls.size, 1)  // a failed read still schedules the next one
  const [[againId, again]] = polls; polls.delete(againId); again.fn(); assert.equal(calls.length, 12)
  cleanup(); calls[11].resolve(good); await flush()
  assert.equal(polls.size, 0); assert.equal(intervals.size, 0)
  context.state = 'Idle'
  console.log('UI B64: the footer poll waits for each read to end, then waits 1 s (Working) before the next; failures continue; cleanup stops it')

  const apiOutput = { exports: {} }
  let ok = true, valid = false, bodyError
  runInNewContext(compile(readFileSync('src/api.ts', 'utf8')), {
    exports: apiOutput.exports, AbortSignal,
    fetch: async () => ({ ok, status: ok ? 200 : 503, json: async () => { if (bodyError) throw bodyError; if (!valid) throw new SyntaxError('invalid JSON'); return { status: 'OK' } } }),
  })
  await assert.rejects(apiOutput.exports.request('footer?session=%25fixture'), { name: 'SyntaxError' })
  assert.equal(JSON.stringify(await apiOutput.exports.request('other-read')), '{}')
  assert.equal(JSON.stringify(await apiOutput.exports.request('key', { key: '1' })), '{}')
  ok = false
  await assert.rejects(apiOutput.exports.request('footer'), error => error.status === 503)
  ok = true; valid = true
  assert.equal((await apiOutput.exports.request('footer')).status, 'OK')
  bodyError = Object.assign(new Error('signal timed out'), { name: 'TimeoutError' })
  await assert.rejects(apiOutput.exports.request('codex-runs?session=%251'), { name: 'TimeoutError' })
  bodyError = Object.assign(new Error('signal aborted'), { name: 'AbortError' })
  await assert.rejects(apiOutput.exports.request('codex-runs?session=%251'), { name: 'AbortError' })
  bodyError = undefined
  console.log('UI B4: invalid successful footer JSON and timed out or aborted body reads reject; other callers and HTTP errors keep their behavior')
  assert.equal(intervals.size, 0); assert.equal(timers.size, 0); assert.equal(polls.size, 0)
}
main().catch(error => { console.error(error); process.exitCode = 1 })
