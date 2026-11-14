// Tab names in agent replies open the tab: name matching and the remark plugin. Run: node test_tab_links.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/tabLinks.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const m = {}
runInNewContext(code, { exports: m })

const panes = [
  { id: '%1', tmux_session: 'a', pane_name: 'B69 review' },
  { id: '%2', tmux_session: 'a', agent_name: 'Advisor: Triage' },
  { id: '%3', tmux_session: 'a', pane_name: 'notes' },
  { id: '%4', tmux_session: 'b', pane_name: 'notes' },
  { id: '%5', tmux_session: 'b' },
  { id: '%6', tmux_session: 'c', pane_name: 'fix' },
  { id: '%7', tmux_session: 'd', pane_name: 'fix' },
]
const here = panes[2]
const id = text => m.tabTarget(text, panes, here)?.id ?? null

assert.equal(id('B69 review'), '%1')
assert.equal(id(' b69 REVIEW '), '%1', 'case and spaces do not matter')
assert.equal(id('B69 review tab'), '%1', 'a trailing "tab" is fine')
assert.equal(id('Advisor: Triage'), '%2', 'the agent name counts')
assert.equal(id('%5'), '%5', 'a pane id')
assert.equal(id('%99'), null, 'a closed tab')
assert.equal(id('notes'), '%4', 'the current tab never links to itself')
assert.equal(m.tabTarget('notes', panes, panes[0])?.id, '%3', 'a shared name picks the tab in this session')
assert.equal(id('fix'), null, 'a shared name in other sessions is ambiguous')
assert.equal(id('Claude'), null, 'the bare agent name is not a tab name')
assert.equal(id(''), null)
assert.equal(id('x'.repeat(90)), null)

// The plugin: quotes stay outside, pane ids become candidates, links and code stay as they are.
const tree = { type: 'root', children: [{ type: 'paragraph', children: [
  { type: 'text', value: 'Open "B69 review" or “Advisor: Triage”, then %5. 100% sure, a%5b.' },
  { type: 'link', url: 'x', children: [{ type: 'text', value: '"B69 review"' }] },
  { type: 'inlineCode', value: '"B69 review"' },
] }] }
m.remarkTabs()(tree)
const parts = tree.children[0].children
const shown = parts.map(part => part.type === 'text' ? part.value : part.type === 'link' && part.data ? `[${part.data.hProperties['data-tab']}]` : `<${part.type}>`).join('')
assert.equal(shown, 'Open "[B69 review]" or “[Advisor: Triage]”, then [%5]. 100% sure, a%5b.<link><inlineCode>')
assert.equal(parts[1].children[0].value, 'B69 review')

const plain = { type: 'root', children: [{ type: 'paragraph', children: [{ type: 'text', value: 'nothing here' }] }] }
m.remarkTabs()(plain)
assert.deepEqual(plain.children[0].children, [{ type: 'text', value: 'nothing here' }])
console.log('tab links: ok')
