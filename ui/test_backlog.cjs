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
// IDs and status: the same rules as server/backlog.py (test_backlog.py uses the same text).
const unnumbered = '- [ ] One #B7\n- [ ] Two\n  - [ ] sub step\n\n## p\n* [x] Three\n'
const numbered = '- [ ] One #B7\n- [ ] Two #B8\n  - [ ] sub step\n\n## p\n* [x] Three #B9\n'
assert.equal(b.withIds(unnumbered), numbered)
assert.equal(b.withIds(numbered), numbered, 'IDs stay')
assert.equal(b.withIds('- [ ] A  \n'), '- [ ] A #B1\n')
const tracked = '- [ ] Fix (codex · pane %12 · chat 019a) #B12\n  - Doing: claude · tmux $3 · pane %40 · chat abcd\n  - why\n- [x] Old #B7\n  - Done: fixed in 1234abc (claude · chat ef)\n'
const [open, old] = plain(b.parseBacklog(tracked))
assert.equal(open.text, 'Fix')
assert.equal(open.ref, 'B12')
assert.deepEqual(open.source, { agent: 'codex', pane: '%12', chat: '019a' })
assert.deepEqual([open.status.kind, open.status.source], ['doing', { agent: 'claude', tmux: '$3', pane: '%40', chat: 'abcd' }])
assert.deepEqual(open.details.map(detail => detail.text), ['why'], 'A status line is not a detail')
assert.deepEqual([old.ref, old.status.kind, old.status.text, old.status.source], ['B7', 'done', 'fixed in 1234abc', { agent: 'claude', chat: 'ef' }])
assert.equal(plain(b.parseBacklog('- [ ] #B5'))[0].ref, undefined, 'A bare ID is the text')
assert.equal(b.rewordItem(open, 'Fix it')(tracked), tracked.replace('- [ ] Fix (', '- [ ] Fix it ('), 'Rewording keeps the ID')
assert.equal(b.startItem(open.raw, 0, 'claude · pane %41')(tracked), tracked.replace('pane %40 · chat abcd', 'pane %41').replace('claude · tmux $3 · ', 'claude · '), 'A new send replaces the old')
assert.equal(b.startItem('- [x] Old #B7', 3, 'codex')(tracked), tracked.replace('  - Done: fixed in 1234abc (claude · chat ef)\n', '  - Doing: codex\n'))

// Filters: Mine has no source, From agents has one, New is what the sidebar counts. Counts are of open items.
const mixed = [
  '- [ ] Mine one #B1',
  '- [ ] Agent one (claude · tmux $3 · pane %14 · chat 4f1c9a2e) #B2',
  '- [ ] Bare agent (codex) #B3',
  '- [ ] Unknown agent (gemini · pane %2) #B4',
  '- [ ] Tag (claude · chat ab) in the middle #B5',
  '- [ ] Capital (Claude · chat ab) #B6',
  '- [ ] Nested (see (claude · chat ab)) #B7',
  '- [ ] (claude · chat ab) #B8',
  '- [ ] No ID yet (codex · pane %9)',
  '- [x] Done by agent (claude · chat cd) #B9',
  '- [x] Done by me #B10',
  '',
  '## later',
  'a free line',
  '- [ ] Late agent (codex · chat ef) #B11',
  '- [ ] Odd keys (claude · window 4 · chat) #B12',
  '',
].join('\n')
const all = b.parseBacklog(mixed).filter(row => row.kind === 'item')
const names = list => plain(list.map(item => item.text))
assert.deepEqual(names(b.filterItems(all, 'all', 0)), names(all), 'All shows every item')
assert.deepEqual(names(b.filterItems(all, 'agents', 0)), ['Agent one', 'Bare agent', 'No ID yet', 'Done by agent', 'Late agent', 'Odd keys'])
assert.deepEqual(names(b.filterItems(all, 'mine', 0)), ['Mine one', 'Unknown agent (gemini · pane %2)', 'Tag (claude · chat ab) in the middle', 'Capital (Claude · chat ab)', 'Nested (see (claude · chat ab))', '(claude · chat ab)', 'Done by me'],
  'Only a claude or codex tag at the end of the line is a source')
assert.deepEqual(plain(all.find(item => item.text === 'Odd keys').source), { agent: 'claude' }, 'Unknown parts of a tag are dropped; the item is still from an agent')
assert.deepEqual(names(b.filterItems(all, 'new', 2)), ['Bare agent', 'Late agent', 'Odd keys'], 'New: open, from an agent, above the last ID seen. No ID is not new')
assert.deepEqual(names(b.filterItems(all, 'new', null)), [], 'On a first visit nothing is new')
assert.deepEqual(names(b.filterItems(all, 'new', 12)), [])
assert.deepEqual(names(b.freshItems(all, 2)), names(b.filterItems(all, 'new', 2)), 'New is the sidebar count')
assert.deepEqual(plain(b.filterCounts(all, 2)), { all: 11, mine: 6, agents: 5, new: 3 }, 'Counts leave done items out')
assert.deepEqual(plain(b.filterCounts(all, null)), { all: 11, mine: 6, agents: 5, new: 0 })
assert.deepEqual(plain(b.filterCounts([], 0)), { all: 0, mine: 0, agents: 0, new: 0 })
assert.equal(b.highestRef(all), 12)
assert.deepEqual(['mine', 'agents', 'new', 'all', 'New', '', null, 7].map(value => b.asFilter(value)), ['mine', 'agents', 'new', 'all', 'all', 'all', 'all', 'all'], 'A stored filter that is not known falls back to All')
console.log('Backlog: parsing, add, tick, reword, delete, undo, move, groups, IDs, status and filters passed')
