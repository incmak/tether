// The backlog's search, filters and sort (backlogView.ts). They change only what is shown, never the file.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const load = file => {
  const output = { exports: {} }
  runInNewContext(ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, { exports: output.exports, require: name => name === './backlogItems' ? items : require(name) })
  return output.exports
}
const items = load('src/backlogItems.ts')
const v = load('src/backlogView.ts')
const plain = value => JSON.parse(JSON.stringify(value))

const file = [
  '- [ ] Write blog post #B3',
  '- [x] Old inbox thing #B1',
  '',
  '## tether',
  '- [ ] Fix flaky share test (codex · pane %1) #B7',
  '  - teardown races the HTTP worker',
  '- [ ] Add backlog search (claude · pane %2) #B12',
  '  - Doing: claude · pane %9',
  '- [x] Ship gzip (claude) #B5',
  '  - Done: fixed in abc123',
  '',
  '## brisk',
  '- [ ] Zebra item #B9',
  '- [ ] alpha item #B10',
  '',
].join('\n')
const rows = items.parseBacklog(file)
const view = over => ({ ...v.DEFAULT_VIEW, ...over })
const refs = groups => plain(groups).map(group => [group.name, group.items.map(item => item.ref)])

assert.deepEqual(refs(v.viewGroups(rows, view())), [['', ['B3']], ['tether', ['B7', 'B12']], ['brisk', ['B9', 'B10']]], 'Open items in file order, by project')
assert.deepEqual(refs(v.viewGroups(rows, view({ status: 'done' }))), [['', ['B1']], ['tether', ['B5']]], 'Done only')
assert.deepEqual(refs(v.viewGroups(rows, view({ status: 'doing' }))), [['tether', ['B12']]], 'In progress only')
assert.deepEqual(refs(v.viewGroups(rows, view({ status: 'all', project: 'tether' }))), [['tether', ['B7', 'B12', 'B5']]], 'One project, every status')
assert.deepEqual(refs(v.viewGroups(rows, view({ project: '' }))), [['', ['B3']]], 'The inbox is the empty project name')
assert.deepEqual(refs(v.viewGroups(rows, view({ who: 'me' }))), [['', ['B3']], ['brisk', ['B9', 'B10']]], 'Me: items with no agent tag')
assert.deepEqual(refs(v.viewGroups(rows, view({ who: 'codex' }))), [['tether', ['B7']]], 'Added by Codex')

assert.deepEqual(refs(v.viewGroups(rows, view({ query: 'teardown' }))), [['tether', ['B7']]], 'Search reads the details')
assert.deepEqual(refs(v.viewGroups(rows, view({ query: 'FLAKY test' }))), [['tether', ['B7']]], 'Every word, any case')
assert.deepEqual(refs(v.viewGroups(rows, view({ query: 'b1', status: 'all' }))), [['', ['B1']]], 'An ID finds that item only, not B10 or B12')
assert.deepEqual(refs(v.viewGroups(rows, view({ query: 'brisk' }))), [['brisk', ['B9', 'B10']]], 'Search reads the project name')
assert.deepEqual(refs(v.viewGroups(rows, view({ query: 'abc123', status: 'all' }))), [['tether', ['B5']]], 'Search reads the result line')
assert.deepEqual(refs(v.viewGroups(rows, view({ query: 'nothing matches' }))), [], 'No match: no groups')

assert.deepEqual(refs(v.viewGroups(rows, view({ sort: 'newest' }))), [['', ['B3']], ['tether', ['B12', 'B7']], ['brisk', ['B10', 'B9']]], 'Newest first inside each project')
assert.deepEqual(refs(v.viewGroups(rows, view({ sort: 'oldest', project: 'tether' }))), [['tether', ['B7', 'B12']]])
assert.deepEqual(refs(v.viewGroups(rows, view({ sort: 'az', project: 'brisk' }))), [['brisk', ['B10', 'B9']]], 'A to Z ignores case')
assert.equal(items.parseBacklog(file).map(row => row.raw).join('\n'), rows.map(row => row.raw).join('\n'), 'Sorting does not change the rows')

assert.deepEqual(plain(v.projectCounts(rows)), [
  { name: '', open: 1, doing: 0, done: 1 },
  { name: 'tether', open: 2, doing: 1, done: 1 },
  { name: 'brisk', open: 2, doing: 0, done: 0 },
])
assert.deepEqual(plain(v.projectCounts(items.parseBacklog('## only\n- [ ] A #B1\n'))), [{ name: '', open: 0, doing: 0, done: 0 }, { name: 'only', open: 1, doing: 0, done: 0 }], 'The inbox stays even when empty')

assert.equal(v.canReorder(view()), true)
assert.equal(v.canReorder(view({ sort: 'newest' })), false)
assert.equal(v.canReorder(view({ query: 'x' })), false)
assert.equal(v.canReorder(view({ who: 'me' })), false)
assert.equal(v.canReorder(view({ status: 'all' })), false, 'Done rows in the list: no drag')
assert.equal(v.canReorder(view({ project: 'tether' })), true, 'Moving inside one project keeps the saved order')
assert.equal(v.isFiltered(view()), false)
assert.equal(v.isFiltered(view({ sort: 'az' })), false, 'An order is not a filter')
assert.equal(v.isFiltered(view({ who: 'claude' })), true)

assert.deepEqual(plain(v.highlight('Fix flaky share test', 'SHARE fix')), [['Fix', true], [' flaky ', false], ['share', true], [' test', false]])
assert.deepEqual(plain(v.highlight('a.b c', '.')), [['a', false], ['.', true], ['b c', false]], 'Query text is not a pattern')
assert.deepEqual(plain(v.highlight('Plain', '')), [['Plain', false]])

const fix = rows.find(row => row.ref === 'B7')
const search = rows.find(row => row.ref === 'B12')
assert.ok(v.queryScore(search, 'search') > v.queryScore(fix, 'teardown'), 'A title match ranks above a detail match')
assert.equal(v.queryScore(fix, 'zzz'), null)
console.log('Backlog view: filters, search, IDs, sort, counts, reorder rule and highlight passed')
