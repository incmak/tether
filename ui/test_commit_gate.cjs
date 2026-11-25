// The commit gate's status text and both sections (src/commitGateText.ts, src/CommitGate.tsx), rendered without a browser.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const ts = require('typescript')
const compile = text => ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText
const text = {}
runInNewContext(compile(readFileSync('src/commitGateText.ts', 'utf8')), { exports: text })
const none = { days: 7, commits: 0, passed: 0, would_block: 0, blocked: 0, let_through: 0, unknown: 0, stamped: 0, unstamped: 0, false_blocks: 0, rate: null }

assert.deepEqual({ ...text.gateSummary(none, 'observe') }, { seen: 'No commits checked yet.', rate: '' })
assert.deepEqual({ ...text.gateSummary(none, 'off') }, { seen: 'The gate is off. No commit is checked.', rate: '' })
assert.deepEqual({ ...text.gateSummary({ ...none, commits: 1, would_block: 1, unstamped: 1 }, 'observe') },
  { seen: '1 commit checked · 1 with no valid line', rate: 'No false-block rate yet. It needs a checked commit that has a valid line.' })
assert.deepEqual({ ...text.gateSummary({ ...none, commits: 1240, passed: 1100, would_block: 90, blocked: 6, let_through: 2, unknown: 42, stamped: 1101, unstamped: 97, false_blocks: 3, rate: 0.0027 }, 'enforce') },
  { seen: '1,240 commits checked · 98 with no valid line · 6 blocked, 2 let through · 42 not readable', rate: 'False-block rate: 0.3% (3 of 1,101 commits with a valid line).' })
assert.equal(text.gateSummary({ ...none, commits: 1, passed: 1, stamped: 1, rate: 0 }, 'observe').rate, 'False-block rate: 0% (0 of 1 commit with a valid line).')

const modes = [['off', 'Off', 'Commits are not checked.'], ['observe', 'Observe', 'Each Claude commit is checked and logged. No commit is blocked.'], ['enforce', 'Enforce', 'A Claude commit with no valid line is blocked.']].map(([mode, name, summary]) => ({ mode, name, summary }))
let polled, hookState, hookIndex, fetchPoll
const gate = {}
runInNewContext(compile(readFileSync('src/CommitGate.tsx', 'utf8') + '\nexport { useCommitGate }'), {
  exports: gate,
  require(name) {
    if (name === 'react') return {
      ...React,
      useState(initial) {
        if (!hookState) return React.useState(initial)
        const index = hookIndex++
        if (!(index in hookState)) hookState[index] = initial
        return [hookState[index], value => { hookState[index] = value }]
      },
      useRef(initial) {
        if (!hookState) return React.useRef(initial)
        const index = hookIndex++
        if (!(index in hookState)) hookState[index] = { current: initial }
        return hookState[index]
      },
      useCallback: (callback, deps) => hookState ? callback : React.useCallback(callback, deps),
    }
    if (name === 'react/jsx-runtime') return require(name)
    if (name === './commitGateText') return text
    if (name === './api') return { errorText: error => String(error) }
    if (name === './hooks') return { usePolling: fetcher => { fetchPoll = fetcher; return polled } }
    if (name === './SettingBones') { const bone = kind => props => React.createElement('i', { 'data-bone': kind, 'data-label': props.label }, props.children); return { NoteBone: bone('note'), StepperBone: bone('stepper'), SwitchBone: bone('switch'), SettingSkeleton: bone('skeleton') } }
    return {}
  },
})
const render = (component, props = {}) => renderToStaticMarkup(React.createElement(component, { api() {}, ...props }))
const checked = (html, value) => (html.match(/<input[^>]*>/g) || []).some(tag => tag.includes(`value="${value}"`) && tag.includes('checked=""'))
const switched = html => (html.match(/<input[^>]*>/g) || []).some(tag => tag.includes('role="switch"') && tag.includes('checked=""'))
function switchIds(html) {
  const row = html.match(/<label class="switch-row"><span><strong id="([^"]+)">Use a project mode<\/strong><small id="([^"]+)">[^<]+<\/small><\/span>(<input[^>]*>)/)
  assert(row, 'the switch keeps its label and description in the row')
  const [, name, description, input] = row
  assert(input.includes(`aria-labelledby="${name}"`) && input.includes(`aria-describedby="${description}"`))
  assert.notEqual(name, description)
  return [name, description]
}
const state = extra => ({ modes, global: 'observe', mode: 'observe', source: 'global', project: null, stats: none, max_blocks: 2, ...extra })
const pollData = extra => ({ state: state(extra), number: 1 })

polled = { data: undefined, error: '', refresh() {} }
for (const html of [render(gate.GlobalCommitGate), render(gate.ProjectCommitGate, { project: 'abcdef012345' })]) {
  assert(html.includes('data-label="Loading the commit gate"') && !html.includes('type="radio"'), 'loading shows the skeleton, no controls')
}
polled = { data: undefined, error: 'Cannot reach the server.', refresh() {} }
assert(render(gate.GlobalCommitGate).includes('<p class="setting-note" role="alert">Cannot reach the server.</p>'), 'a failed first load says why')

polled = { data: pollData(), error: '', refresh() {} }
let html = render(gate.GlobalCommitGate)
assert.equal((html.match(/type="radio"/g) || []).length, 3)
assert(checked(html, 'observe') && !checked(html, 'enforce'))
assert(html.includes('<legend>Mode</legend>') && html.includes('<strong>Observe.</strong> Each Claude commit is checked and logged.'))
assert(html.includes('role="status"') && html.includes('<strong>Last 7 days</strong><small>No commits checked yet.</small>'))
polled = { data: pollData({ stats: null }), error: '', refresh() {} }
for (const section of [render(gate.GlobalCommitGate), render(gate.ProjectCommitGate, { project: 'abcdef012345' })]) {
  assert.match(section, /role="status"><span><small>Commit gate stats are temporarily unavailable\.<\/small>/)
  assert(checked(section, 'observe'), 'unavailable stats keep the mode controls')
}
polled = { data: pollData(), error: 'Reload the page and try again.', refresh() {} }
html = render(gate.GlobalCommitGate)
assert(html.includes('<p class="error" role="alert">Reload the page and try again.</p>') && html.includes('type="radio"'), 'a failed save keeps the controls')

polled = { data: pollData({ global: 'observe', mode: 'observe', project: null }), error: '', refresh() {} }
html = render(gate.ProjectCommitGate, { project: 'abcdef012345' })
assert(html.includes('Claude here uses the global mode Observe.') && /<fieldset[^>]*disabled=""/.test(html), 'no override: the mode choice is off')
assert(!switched(html))
const overrideIds = switchIds(html)
polled = { data: pollData({ mode: 'enforce', source: 'project', project: { override: true, mode: 'enforce' }, stats: { ...none, commits: 3, passed: 2, blocked: 1, stamped: 2, rate: 0 } }), error: '', refresh() {} }
html = render(gate.ProjectCommitGate, { project: 'abcdef012345' })
assert(html.includes('Claude here uses Enforce, not the global mode Observe.') && switched(html) && !/<fieldset[^>]*disabled=""/.test(html))
assert.deepEqual(switchIds(html), overrideIds, 'the switch name and description references stay stable')
assert(checked(html, 'enforce') && html.includes('<strong>Last 7 days, this project</strong>'))
assert(html.includes('3 commits checked · 1 with no valid line · 1 blocked, 0 let through<br/>False-block rate: 0% (0 of 2 commits with a valid line).'))
async function testSaveSurvivesFailedRefresh() {
  hookState = []
  const saved = state({ global: 'enforce', mode: 'enforce' })
  const older = Promise.withResolvers()
  const post = Promise.withResolvers()
  const off = state({ global: 'off', mode: 'off' })
  const gets = [() => state(), () => older.promise, () => { throw Error('GET failed') }, () => off]
  const poll = async () => { polled.data = await fetchPoll(new AbortController().signal) }
  let refreshes = 0
  polled = { data: undefined, error: '', async refresh() { refreshes++; await poll() } }
  const api = async (path, body) => {
    assert.equal(path, 'commit-gate')
    if (!body) return gets.shift()()
    assert.deepEqual(body, { mode: 'enforce' })
    return post.promise
  }
  const read = () => { hookIndex = 0; return gate.useCommitGate(api) }
  read()
  await poll()
  const saving = read().save({ mode: 'enforce' })
  const oldPoll = poll()
  older.resolve(state())
  await oldPoll
  assert.equal(read().data.global, 'observe', 'the older GET finished during the POST')
  post.resolve(saved)
  await saving
  assert.equal(refreshes, 1)
  assert.equal(read().data, saved, 'the response to the POST supplies the saved mode')
  assert.equal(read().error, '', 'a rejected refresh is not a save error')
  polled.error = 'GET failed'
  assert.equal(read().data, saved, 'a polling error keeps the saved mode')
  assert.equal(read().error, 'GET failed')
  await poll()
  assert.equal(read().data, off, 'a GET started after the POST replaces the saved response')
}

testSaveSurvivesFailedRefresh().then(() => {
  console.log('Commit gate: status text, loading, errors, saved mode after refresh failure, and project switch labels pass')
}).catch(error => { console.error(error); process.exitCode = 1 })
