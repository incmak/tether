// Render the real task sheet with controlled footer and transcript data.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const compile = file => ts.transpileModule(readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText
const zone = {}
runInNewContext(compile('src/zone.ts'), { exports: zone, require })

let state = { goal: null, tasks: [{ id: 'legacy', kind: 'shell', title: 'Old task', command: 'npm test', state: 'running' }] }
let error = ''
const output = { exports: {} }
runInNewContext(compile('src/Dock.tsx'), {
  exports: output.exports,
  require(name) {
    if (name === 'react' || name === 'react/jsx-runtime' || name === 'lucide-react') return require(name)
    if (name === './hooks') return { useModal: () => ({}), usePolling: () => ({ data: state, error, refresh: async () => {} }) }
    if (name === './Notices') return { Elapsed: ({ since }) => React.createElement('span', { 'data-since': since }, '10s') }
    if (name === './zone') return zone
    return {}
  },
})
const render = taskDetails => renderToStaticMarkup(React.createElement(output.exports.LiveSheet, {
  api: async () => { throw new Error('Unexpected API write') }, pane: { id: '%fixture' }, show: 'tasks',
  taskDetails, onClose() {}, onTerminal() {},
}))
const description = 'Watch <build> & logs'
const command = 'printf "one\\ntwo" ' + 'x'.repeat(1000)
let html = render([{ id: 's1', type: 'shell', status: 'running', description, command, started: 100 }])
assert(html.includes('Watch &lt;build&gt; &amp; logs'))
assert(html.includes('class="live-command"'))
assert(html.includes('x'.repeat(1000)))
assert(html.includes('shell · running'))
assert(html.includes('data-since="100000"'))
assert(!html.includes('Old task'))
assert(!html.includes('>Stop</button>'), 'Hook IDs must not use transcript stop controls')
state = null; error = 'offline'
html = render([{ id: 'm1', type: 'monitor', status: 'running' }])
assert(html.includes('monitor · running'))
assert(!html.includes('Could not load'))
assert(!html.includes('Loading'))
html = render([])
assert(html.includes('No background tasks'))
const started = Date.parse('2026-11-19T01:00:00Z') / 1000
state = { goal: null, tasks: [{ id: 'legacy', kind: 'shell', title: 'Old task', command: 'npm test', state: 'running', started }] }
error = ''
for (const timeZone of ['Asia/Kolkata', 'America/New_York']) {
  zone.setTetherZone(timeZone)
  html = render(undefined)
  const label = new Date(started * 1000).toLocaleString(undefined, { timeZone })
  assert(html.includes('Old task'))
  assert(html.includes('>Stop</button>'))
  assert(html.includes(`started ${label}`), `Legacy start time uses ${timeZone}`)
}
console.log('Task sheet: hook details, escaped command, elapsed, empty list, API failure, legacy fallback and start time passed')
