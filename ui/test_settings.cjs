const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const source = ts.transpileModule(readFileSync('src/settingsIndex.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const output = {}
runInNewContext(source, { exports: output, require })
const match = output.matchesSetting
const folder = { id: 'folder', group: 'general', title: 'Default folder', keywords: 'working directory path home new session start projects' }
const mic = { id: 'microphone', group: 'voice', title: 'Voice button', keywords: 'mic microphone dictate' }
const colors = { id: 'colors', group: 'appearance', title: 'Colors', keywords: 'Moss Rosé palette' }
assert(match(folder, '  DEFAULT   directory  '))
assert(match(folder, 'folder'))
assert(match(folder, 'default folder'))
assert(!match(folder, 'directory microphone'))
assert(match(mic, 'microphone'))
assert(!match(mic, 'notifications'), 'Category descriptions must not return unrelated controls')
assert(match(colors, 'rose'), 'Search accepts unaccented palette names')
assert(match(colors, 'appearance'))
assert(!match(colors, 'no-such-setting'))
assert.equal(output.SETTINGS_GROUPS.length, 14)
assert.deepEqual([...new Set(output.SETTINGS_GROUPS.map(group => group.section))], ['Tether', 'Agents', 'More'])
for (const { id, section, description } of output.SETTINGS_GROUPS) {
  assert(section?.trim(), `${id} has a section`)
  assert(description?.trim(), `${id} has a description`)
}
const activity = { id: 'activity-log', group: 'activity', title: 'Activity log', keywords: 'audit history' }
assert(match(activity, 'activity log'))
assert(!match(activity, 'voice alerts'))
const keys = { id: 'keys-layout', group: 'shortcuts', title: 'Layout keys', keywords: 'keyboard shortcut Split right ⌘\\' }
assert(match(keys, 'keyboard split'), 'A key is found by its action')
assert(match(keys, 'shortcuts'), 'The Keyboard shortcuts category name finds its keys')
console.log('Settings search: case, whitespace, multiword intersection, aliases, categories, accents and unrelated-result exclusion pass')
