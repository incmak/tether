// usePanelPoll: a slow load is not aborted by the next tick; a stuck load is. Mocked hooks, fake timers; no DOM or network.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

let now = 0, timers = [], effects = []
const react = { useRef: value => ({ current: value }), useEffect: fn => effects.push(fn), useCallback: fn => fn, useState: value => [value, () => {}] }
const window = {
  setTimeout: (fn, ms) => { timers.push({ fn, at: now + ms }); return timers.length },
  clearTimeout: id => { if (timers[id - 1]) timers[id - 1].fn = null },
}
const document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} }
const mod = {}
runInNewContext(ts.transpileModule(readFileSync('src/panelProps.tsx', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText, {
  exports: mod, window, document, AbortController, Date: { now: () => now }, Promise,
  require: name => name === 'react' ? react : name === 'react/jsx-runtime' ? require('react/jsx-runtime') : {},
})
const advance = async ms => {
  const end = now + ms
  for (;;) {
    const next = timers.filter(timer => timer.fn && timer.at <= end).sort((a, b) => a.at - b.at)[0]
    if (!next) break
    now = next.at; const fn = next.fn; next.fn = null; fn(); await Promise.resolve()
  }
  now = end
  for (let i = 0; i < 5; i += 1) await Promise.resolve()
}

async function main() {
  const calls = []
  const load = signal => { const call = { signal }; calls.push(call); return new Promise(resolve => { call.resolve = resolve }) }
  mod.usePanelPoll(load, 3000, true)
  effects.forEach(effect => effect())
  assert.equal(calls.length, 1, 'The first load starts at once')
  await advance(9000)
  assert.equal(calls.length, 1, 'Ticks wait while a slow load is in flight')
  assert.equal(calls[0].signal.aborted, false, 'A slow load is not aborted')
  calls[0].resolve(); await advance(0)
  await advance(3000)
  assert.equal(calls.length, 2, 'The next tick after the load ends starts a new one')
  calls[1].resolve(); await advance(0); await advance(3000)
  assert.equal(calls.length, 3)
  await advance(33000)
  assert.equal(calls[2].signal.aborted, true, 'A stuck load is aborted')
  assert.equal(calls.length, 4, 'and a new one starts')
  console.log('panel poll tests passed: slow load kept, next tick after it ends, stuck load replaced')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
