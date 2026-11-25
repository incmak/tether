// Render the real Codex goal sheet and Chat controls, without terminal writes.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const icons = require('lucide-react')
const compile = text => ts.transpileModule(text, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText
const zone = {}
runInNewContext(compile(readFileSync('src/zone.ts', 'utf8')), { exports: zone, require })
const api = {}
runInNewContext(compile(readFileSync('src/api.ts', 'utf8')), { exports: api, require: name => name === './zone' ? zone : require(name) })
let state
const dock = {}
runInNewContext(compile(readFileSync('src/Dock.tsx', 'utf8')), {
  exports: dock,
  require(name) {
    if (['react', 'react/jsx-runtime', 'lucide-react'].includes(name)) return require(name)
    if (name === './api') return api
    if (name === './zone') return zone
    if (name === './hooks') return { useModal: () => ({}), usePolling: () => ({ data: state, error: '', refresh() {} }) }
    return {}
  },
})
for (const [status, label] of Object.entries({ active: 'Active', paused: 'Paused', blocked: 'Blocked', usage_limited: 'Usage limit reached', budget_limited: 'Budget limit reached', complete: 'Complete' })) {
  state = { goal: { condition: 'Finish <cleanup> & tests', status, met: status === 'complete', tokens_used: 1200, token_budget: 100000, time_used_seconds: 65 }, tasks: [] }
  const html = renderToStaticMarkup(React.createElement(dock.LiveSheet, { api() {}, pane: { id: '%fixture' }, show: 'goal', onClose() {}, onTerminal() {} }))
  assert(html.includes(label))
  assert(html.includes('Finish &lt;cleanup&gt; &amp; tests'))
  assert(html.includes('1,200 / 100,000'))
  assert(html.includes('1 min 5 sec'))
  assert(!html.includes('>Stop</button>'))
}
state = { goal: { condition: 'Claude condition', met: false }, tasks: [] }
assert(renderToStaticMarkup(React.createElement(dock.LiveSheet, { pane: { id: '%fixture' }, show: 'goal' })).includes('Not met yet'))
console.log('Goal sheet: every Codex status, objective, budget, elapsed time, escaping, and Claude fallback pass')

const source = ts.createSourceFile('Chat.tsx', readFileSync('src/Chat.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let usage, goal, folder
function visit(node) {
  if (ts.isJsxElement(node) && node.openingElement.tagName.getText(source) === 'button') {
    const text = node.getText(source)
    if (text.includes('aria-label="Usage and credits"')) usage = text
    if (text.includes("setLiveSheet('goal')")) goal = text
  }
  if (ts.isJsxElement(node) && node.openingElement.tagName.getText(source) === 'bdi' && node.getText(source).includes('className="header-folder"')) folder = node.getText(source)
  ts.forEachChild(node, visit)
}
visit(source)
const context = { require, exports: {}, Gauge: icons.Gauge, Target: icons.Target, ChevronRight: icons.ChevronRight, goalStatusLabel: api.goalStatusLabel }
runInNewContext(compile(`globalThis.usage = (setUsageSheet) => (${usage}); globalThis.goal = (footer, setLiveSheet) => (${goal});`), context)
let shown
const usageButton = context.usage(value => { shown = value })
assert(renderToStaticMarkup(usageButton).includes('Usage'))
usageButton.props.onClick(); assert.equal(shown, true)
const goalButton = context.goal({ goal: 'blocked', goal_status: 'blocked' }, value => { shown = value })
assert(renderToStaticMarkup(goalButton).includes('Goal · Blocked'))
goalButton.props.onClick(); assert.equal(shown, 'goal')
assert(!source.text.includes('className="footer-row codex-usage"'))
console.log('Chat: goal control opens details; compact Usage control opens usage; separate usage rows removed')

const folderContext = { require, exports: {} }
runInNewContext(compile(`globalThis.folder = (activePane) => (${folder});`), folderContext)
for (const [path, label] of [['~/apps/tether', 'tether'], ['/', '/'], ['~', '~'], ['~/apps/a & b/', 'a &amp; b']]) {
  const html = renderToStaticMarkup(folderContext.folder({ dir: path }))
  assert(html.includes(`>${label}</bdi>`))
  assert(html.includes('aria-label="Directory: '))
}
console.log('Header: current folder label, full-path accessible name, root, home and escaping pass')
