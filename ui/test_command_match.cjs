// Slash command checks before send: unknown names, "did you mean", and which commands run on Enter. Run: node test_command_match.cjs
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const code = ts.transpileModule(readFileSync('src/commandMatch.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const m = {}
runInNewContext(code, { exports: m })

const commands = [
  { name: 'clear', description: 'Start a new conversation', kind: 'builtin', aliases: ['reset', 'new'], hint: '' },
  { name: 'add-dir', description: 'Add a working directory', kind: 'builtin', hint: '<path>' },
  { name: 'compact', description: 'Free context', kind: 'builtin', hint: '[instructions]' },
  { name: 'improve-ui', description: 'Polish the interface', kind: 'skill' },
  { name: 'improve', description: 'Improve the code', kind: 'skill' },
  { name: '$review-skill', description: 'A Codex skill', kind: 'skill' },
]

// The bug: /imporove went to Claude, which ran its closest command.
assert.equal(m.unknownCommand('/imporove', 'claude', commands), true)
assert.equal(m.unknownCommand('/imporove fix the header', 'claude', commands), true)
assert.equal(m.unknownCommand('/improve', 'claude', commands), false)
assert.equal(m.unknownCommand('/IMPROVE now', 'claude', commands), false)
assert.equal(m.unknownCommand('/new', 'claude', commands), false, 'aliases are commands')
assert.equal(m.unknownCommand('/impro', 'claude', commands), true, 'a partial name would run the top prefix match')
console.log('commands: unknown and partial names are caught; names and aliases pass')

// Not commands, or nothing to check against.
assert.equal(m.unknownCommand('fix /imporove', 'claude', commands), false)
assert.equal(m.unknownCommand('/usr/share/file.txt', 'claude', commands), false)
assert.equal(m.unknownCommand('/imporove', 'claude', []), false, 'no catalog: send as before')
assert.equal(m.unknownCommand('/imporove', 'antigravity', commands), false)
assert.equal(m.unknownCommand('/imporove', 'shell', commands), false)
assert.equal(m.unknownCommand('/ls', 'codex', commands), false, 'Codex /ls becomes !ls on the server')
assert.equal(m.unknownCommand('/ls', 'claude', commands), true)
assert.equal(m.unknownCommand('/mcp__github__pr', 'claude', commands), false, 'MCP prompts are not in the list')
console.log('commands: paths, shells, other agents, Codex /ls and MCP prompts are left alone')

assert.deepEqual(m.closest('imporove', commands).map(item => item.name), ['improve'])
assert.deepEqual(m.closest('impro', commands).map(item => item.name), ['improve-ui', 'improve'])
assert.deepEqual(m.closest('cmpact', commands).map(item => item.name), ['compact'])
assert.deepEqual(m.closest('zzzzzz', commands), [])
assert(!m.closest('review', commands).some(item => item.name.startsWith('$')), 'Codex $skills are not slash commands')
console.log('commands: "did you mean" lists close names and small typos')

assert.equal(m.needsMore(commands[1]), true, '<path> is required: Enter fills the box')
assert.equal(m.needsMore(commands[2]), false, '[instructions] is optional: Enter runs')
assert.equal(m.needsMore(commands[0]), false)
assert.equal(m.needsMore(commands[3]), false)
console.log('commands: Enter runs unless a required argument is missing')
