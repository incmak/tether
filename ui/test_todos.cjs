// Todos (src/tasks.ts): grouping by local day and due labels. Run from the repo root: node ui/test_todos.cjs
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const { test } = require('node:test')
const ts = require('typescript')

function loadSource(name, resolve = require) {
  const file = path.join(__dirname, 'src', name)
  const mod = new module.constructor(file)
  mod.require = resolve
  mod._compile(ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, file)
  return mod.exports
}
function load(zoneName) {
  const zone = loadSource('zone.ts')
  zone.setTetherZone(zoneName)
  return loadSource('tasks.ts', name => name === './zone' ? zone : name === './hooks' ? {} : require(name))
}
const { groupTasks, dueLabel, agoLabel } = load('Asia/Kolkata')
const seconds = iso => Date.parse(iso) / 1000
// 19 Nov 2026 23:00 IST (17:30Z): late in the IST day.
const NOW = seconds('2026-11-19T17:30:00Z')
const task = (key, due, title = key) => ({ key, source: 'todo', list: 'Tasks', title, due, reminderAt: null, link: '', backlog: null, reminder: null })

test('groups by IST day: a date-only task due today is not overdue until the day ends', () => {
  const tasks = [
    task('yesterday', { date: '2026-11-18', at: null }),
    task('today-date', { date: '2026-11-19', at: null }),
    task('today-past-time', { date: '2026-11-19', at: seconds('2026-11-19T10:00:00Z') }),
    task('today-later', { date: '2026-11-19', at: seconds('2026-11-19T18:00:00Z') }),
    // 20 Nov 00:30 IST is 19 Nov 19:00 UTC: tomorrow in IST.
    task('tomorrow-ist', { date: '2026-11-20', at: seconds('2026-11-19T19:00:00Z') }),
    task('later', { date: '2026-12-01', at: null }),
    task('b-none', null), task('a-none', null),
  ]
  const groups = groupTasks(tasks, NOW)
  assert.deepEqual(groups.overdue.map(t => t.key), ['yesterday', 'today-past-time'])
  assert.deepEqual(groups.today.map(t => t.key), ['today-date', 'today-later'])
  assert.deepEqual(groups.upcoming.map(t => t.key), ['tomorrow-ist', 'later'])
  assert.deepEqual(groups.someday.map(t => t.key), ['a-none', 'b-none'])
})

test('due labels', () => {
  assert.equal(dueLabel({ date: '2026-11-19', at: null }, NOW), 'Today')
  assert.equal(dueLabel({ date: '2026-11-20', at: null }, NOW), 'Tomorrow')
  assert.equal(dueLabel({ date: '2026-11-18', at: null }, NOW), 'Yesterday')
  assert.equal(dueLabel({ date: '2026-11-25', at: null }, NOW), 'Wed 25 Nov')
  assert.equal(dueLabel({ date: '2027-01-05', at: null }, NOW), 'Tue 5 Jan 2027')
  assert.equal(dueLabel({ date: '2026-11-20', at: seconds('2026-11-19T19:00:00Z') }, NOW), 'Tomorrow 00:30')
})

test('grouping uses New York day boundaries and keeps date-only tasks on their named day', () => {
  const { groupTasks, dueLabel } = load('America/New_York')
  const now = seconds('2026-11-20T04:00:00Z') // 19 Nov 23:00 EST
  const tasks = [
    task('yesterday', { date: '2026-11-18', at: null }),
    task('today-date', { date: '2026-11-19', at: null }),
    task('today-past-time', { date: '2026-11-19', at: seconds('2026-11-19T23:00:00Z') }),
    task('today-later', { date: '2026-11-20', at: seconds('2026-11-20T04:30:00Z') }),
    task('tomorrow', { date: '2026-11-20', at: seconds('2026-11-20T05:30:00Z') }),
    task('later', { date: '2026-12-01', at: null }),
    task('b-none', null), task('a-none', null),
  ]
  const groups = groupTasks(tasks, now)
  assert.deepEqual(groups.overdue.map(t => t.key), ['yesterday', 'today-past-time'])
  assert.deepEqual(groups.today.map(t => t.key), ['today-date', 'today-later'])
  assert.deepEqual(groups.upcoming.map(t => t.key), ['tomorrow', 'later'])
  assert.deepEqual(groups.someday.map(t => t.key), ['a-none', 'b-none'])
  assert.equal(dueLabel(tasks[1].due, now), 'Today')
  assert.equal(dueLabel(tasks[3].due, now), 'Today 23:30')
  assert.equal(dueLabel(tasks[4].due, now), 'Tomorrow 00:30')
  assert.equal(dueLabel(tasks[5].due, now), 'Tue 1 Dec')
  assert.equal(dueLabel({ date: '2026-12-29', at: null }, seconds('2027-01-01T02:00:00Z')), 'Tue 29 Dec')
})

test('due labels use calendar days across the New York DST change', () => {
  const { groupTasks, dueLabel } = load('America/New_York')
  const now = seconds('2026-03-08T05:05:00Z')
  const due = { date: '2026-03-09', at: seconds('2026-03-09T04:00:00Z') }
  assert.equal(dueLabel(due, now), 'Tomorrow 00:00')
  assert.deepEqual(groupTasks([task('midnight', due)], now).upcoming.map(t => t.key), ['midnight'])
})

test('ago labels', () => {
  assert.equal(agoLabel(NOW - 5, NOW), 'just now')
  assert.equal(agoLabel(NOW - 240, NOW), '4 min ago')
  assert.equal(agoLabel(NOW - 7300, NOW), '2 h ago')
  assert.equal(agoLabel(NOW - 86400, NOW), '1 day ago')
})
