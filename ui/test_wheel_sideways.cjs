// wheelSideways.ts: the decision for a strip of controls (tab bars, toolbars, breadcrumbs). Real TS, no DOM.
// The browser proof is in test_work_backlog_ui.cjs.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const js = source => ts.transpileModule(readFileSync(source, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const strip = {}
runInNewContext(js('src/stripScroll.ts'), { exports: strip, Math, Infinity })
const mod = {}
runInNewContext(js('src/wheelSideways.ts'), { exports: mod, Math, Infinity, WeakMap, require: name => name === './stripScroll' ? strip : {} })
const { sidewaysDelta } = mod
const { QUIET_MS } = strip

const el = {}, other = {}
const wheel = (dx, dy, at, over = {}) => ({ deltaX: dx, deltaY: dy, deltaMode: 0, ctrlKey: false, timeStamp: at, ...over })

// A run that starts on the strip: a vertical wheel scrolls it by the vertical delta; up scrolls back.
assert.equal(sidewaysDelta(wheel(0, 100, 1000), el, null, true), 100)
assert.equal(sidewaysDelta(wheel(0, -100, 1000), el, { at: 0, owner: null }, true), -100)
assert.equal(sidewaysDelta(wheel(0, 3, 1000, { deltaMode: 1 }), el, null, true), 48, 'Firefox: 3 lines')
// Sideways, a pinch, or a strip that does not overflow: the browser keeps it.
assert.equal(sidewaysDelta(wheel(30, 4, 1000), el, null, true), null)
assert.equal(sidewaysDelta(wheel(0, 100, 1000, { ctrlKey: true }), el, null, true), null)
assert.equal(sidewaysDelta(wheel(0, 100, 1000), el, null, false), null)
// A run in progress that this strip took: each event moves it by its larger delta (a trackpad swipe that turns sideways).
assert.equal(sidewaysDelta(wheel(-20, 2, 1016), el, { at: 1000, owner: el }, true), -20)
assert.equal(sidewaysDelta(wheel(1, 6, 1016), el, { at: 1000, owner: el }, true), 6)
// A run that started elsewhere (the page, another strip, a native sideways scroll): never taken while it lasts.
assert.equal(sidewaysDelta(wheel(0, 100, 1000 + QUIET_MS), el, { at: 1000, owner: null }, true), null)
assert.equal(sidewaysDelta(wheel(0, 100, 1050), el, { at: 1000, owner: other }, true), null)
// After a pause longer than QUIET_MS, a new run.
assert.equal(sidewaysDelta(wheel(0, 100, 1001 + QUIET_MS), el, { at: 1000, owner: null }, true), 100)

console.log('wheel sideways: PASS')
