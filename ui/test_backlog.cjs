// The backlog's Markdown rules: every change keeps the other lines exactly as they were.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const output = { exports: {} }
runInNewContext(ts.transpileModule(readFileSync('src/backlogItems.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: output.exports })
const b = output.exports
const plain = value => JSON.parse(JSON.stringify(value))

const file = '- [ ] Mine\n  - why\n  - [x] sub\n- [x] Old\n\nfree text\n\n## tether\n- [ ] Fix (codex · tmux $3 · pane %12 · chat 019a)\n'
const rows = plain(b.parseBacklog(file))
assert.deepEqual(rows.map(row => row.kind), ['item', 'item', 'note', 'note', 'item'])
assert.equal(rows[0].details.length, 2)
assert.equal(rows[0].details[1].done, true)
assert.equal(rows[2].heading, false)
assert.equal(rows[3].heading, true)
assert.equal(rows[3].text, 'tether')
assert.equal(rows[4].text, 'Fix')
assert.deepEqual(rows[4].source, { agent: 'codex', tmux: '$3', pane: '%12', chat: '019a' })
assert.equal(plain(b.parseBacklog('- [ ] (codex)'))[0].text, '(codex)', 'A whole line in brackets is the item')
const twins = plain(b.parseBacklog('- [ ] Same\n- [ ] Same\n'))
assert.notEqual(twins[0].id, twins[1].id, 'Equal lines keep their own row')
assert.deepEqual(plain(b.parseBacklog('* [X] Star'))[0].done, true)

assert.equal(b.addItem(' New\nline ')(''), '- [ ] New line\n')
assert.equal(b.addItem('New')('- [ ] A\n'), '- [ ] A\n- [ ] New\n')
assert.equal(b.addItem('New')('- [ ] A'), '- [ ] A\n- [ ] New\n')
assert.equal(b.addItem('New')('- [ ] A\n\n## x\n- [ ] B\n'), '- [ ] A\n- [ ] New\n\n## x\n- [ ] B\n')
assert.equal(b.addItem('New')('## x\n- [ ] B\n'), '- [ ] New\n\n## x\n- [ ] B\n')

assert.equal(b.setDone('- [ ] Mine', 0, true)(file), file.replace('- [ ] Mine', '- [x] Mine'))
assert.equal(b.setDone('  - [x] sub', 2, false)(file), file.replace('  - [x] sub', '  - [ ] sub'))
assert.equal(b.setDone('- [ ] Gone', 0, true)(file), null, 'A line that is gone is not guessed')
assert.equal(b.setDone('- [ ] Same', 1, true)('- [ ] Same\n- [ ] Same\n'), '- [ ] Same\n- [x] Same\n', 'The nearest twin changes')
assert.equal(b.setDone('- [ ] Mine', 0, true)('- [ ] Agent\n' + file), '- [ ] Agent\n' + file.replace('- [ ] Mine', '- [x] Mine'), 'The line is found after a shift')

const fix = plain(b.parseBacklog(file))[4]
assert.equal(b.rewordItem(fix, ' Fixed\nit ')(file), file.replace('- [ ] Fix (codex', '- [ ] Fixed it (codex'), 'Rewording keeps who added it')

const removed = b.blockOf(file, '- [ ] Mine', 0)
assert.deepEqual(plain(removed.lines), ['- [ ] Mine', '  - why', '  - [x] sub'])
const without = b.removeItem('- [ ] Mine', 0)(file)
assert.equal(without, file.replace('- [ ] Mine\n  - why\n  - [x] sub\n', ''), 'Details go with their item')
assert.equal(b.restoreItem(removed)(without), file, 'Undo puts it back')
const fixBlock = b.blockOf(file, fix.raw, fix.line)
assert.equal(b.restoreItem(fixBlock)(b.removeItem(fix.raw, fix.line)(file)), file, 'Undo after its heading')

const moving = '- [ ] A\n  - a1\n- [ ] B\n- [ ] C\n'
assert.equal(b.moveItem('- [ ] A', 0, '- [ ] C', 3, true)(moving), '- [ ] B\n- [ ] C\n- [ ] A\n  - a1\n', 'Down, with its details')
assert.equal(b.moveItem('- [ ] C', 3, '- [ ] A', 0, false)(moving), '- [ ] C\n- [ ] A\n  - a1\n- [ ] B\n', 'Up')
assert.equal(b.moveItem('- [ ] C', 3, '- [ ] A', 0, true)(moving), '- [ ] A\n  - a1\n- [ ] C\n- [ ] B\n', 'After a block')
assert.equal(b.moveItem('- [ ] A', 0, '- [ ] A', 0, true)(moving), moving, 'Onto itself')
assert.equal(b.moveItem('- [ ] A', 0, '- [ ] Gone', 0, true)(moving), null)
assert.equal(b.moveItem('- [ ] A', 0, '- [ ] Z', 3, false)('- [ ] A\n\n## x\n- [ ] Z\n'), '\n## x\n- [ ] A\n- [ ] Z\n', 'Into another group')

const groups = plain(b.openGroups(b.parseBacklog('- [ ] In\n\n## a\n- [x] Done\n\n## b\n- [ ] Open\n')))
assert.deepEqual(groups.map(group => group.heading && group.heading.text), [null, 'b'], 'A group with nothing open is hidden')
console.log('Backlog: parsing, add, tick, reword, delete, undo, move and groups passed')
