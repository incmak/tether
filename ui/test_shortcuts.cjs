// The keyboard shortcuts list (shortcutList.ts): the ⌘/ sheet and Settings › Keyboard shortcuts read it, and its filter finds a key by its action.
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const modules = {}
const load = (file, name) => {
  const output = { exports: {} }
  runInNewContext(ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, { exports: output.exports, require: wanted => modules[wanted] ?? require(wanted) })
  modules[name] = output.exports
  return output.exports
}
load('src/keys.ts', './keys')
load('src/navKeys.ts', './navKeys')
const list = load('src/shortcutList.ts', './shortcutList')
const plain = value => JSON.parse(JSON.stringify(value))
const labels = groups => plain(groups).map(group => [group.name, group.items.map(([label]) => label)])

const names = list.SHORTCUT_GROUPS.map(group => group.name)
assert.deepEqual(plain(names), ['Move', 'Search', 'Backlog', 'Layout', 'Act'])
const every = list.SHORTCUT_GROUPS.flatMap(group => group.items)
assert.equal(new Set(list.SHORTCUT_GROUPS.flatMap(group => group.items.map(([label]) => `${group.name}/${label}`))).size, every.length, 'No label twice in one group: React keys stay unique')
assert.ok(every.some(([label, key]) => label === 'Keyboard shortcuts' && key === '⌘/'), 'The list names its own key, ⌘/')
assert.ok(!every.some(([, key]) => key === '⌘?'), '⌘? stays with the Mac Help menu')

assert.equal(list.filterShortcuts(list.SHORTCUT_GROUPS, '').length, 5, 'No filter: every group')
assert.equal(list.filterShortcuts(list.SHORTCUT_GROUPS, '   ').length, 5, 'Spaces only: every group')
assert.deepEqual(labels(list.filterShortcuts(list.SHORTCUT_GROUPS, 'split')), [['Layout', ['Split right']]], 'Finds a key by its action')
assert.deepEqual(labels(list.filterShortcuts(list.SHORTCUT_GROUPS, 'PANES commands')), [['Search', ['Search panes, commands and the backlog']]], 'Every word, any case')
assert.equal(labels(list.filterShortcuts(list.SHORTCUT_GROUPS, 'layout')).find(([name]) => name === 'Layout')[1].length, list.SHORTCUT_GROUPS.find(group => group.name === 'Layout').items.length, 'A group name finds the whole group')
assert.ok(labels(list.filterShortcuts(list.SHORTCUT_GROUPS, '⌘/')).some(([, items]) => items.includes('Keyboard shortcuts')), 'A key finds its row')
assert.ok(labels(list.filterShortcuts(list.SHORTCUT_GROUPS, 'détails')).some(([name]) => name === 'Backlog'), 'Accents do not matter')
assert.deepEqual(plain(list.filterShortcuts(list.SHORTCUT_GROUPS, 'zzz nothing')), [], 'No match: no groups')
console.log('Shortcuts: groups, ⌘/ row and filter passed')
