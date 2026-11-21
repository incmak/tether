const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { test } = require('node:test')
const ts = require('typescript')

const mod = new module.constructor('src/zone.ts')
mod.require = require
mod._compile(ts.transpileModule(readFileSync('src/zone.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, 'src/zone.ts')
const {
  DEFAULT_ZONE, dayKey, fmtDate, fmtDateTime, fmtTime, offsetLabel, pickZone,
  sameDay, setTetherZone, startOfDay, tetherZone, zonedTime, zoneList, zoneParts,
} = mod.exports

test('New York wall times round trip across both DST transitions', () => {
  setTetherZone('America/New_York')
  for (const [month, date, hour, minute, iso, actualHour] of [
    [2, 8, 1, 59, '2026-03-08T06:59:00Z', 1],
    [2, 8, 2, 30, '2026-03-08T07:30:00Z', 3],
    [2, 8, 3, 30, '2026-03-08T07:30:00Z', 3],
    [10, 1, 0, 59, '2026-11-01T04:59:00Z', 0],
    [10, 1, 1, 30, '2026-11-01T05:30:00Z', 1],
    [10, 1, 2, 0, '2026-11-01T07:00:00Z', 2],
  ]) {
    const at = zonedTime(2026, month, date, hour, minute)
    assert.equal(at, Date.parse(iso))
    assert.deepEqual(zoneParts(at), { year: 2026, month, date, weekday: 0, hour: actualHour, minute })
  }
  assert.deepEqual(zoneParts(Date.parse('2026-11-01T06:30:00Z')), zoneParts(Date.parse('2026-11-01T05:30:00Z')))
  const day = startOfDay(Date.parse('2026-03-08T20:00:00Z'))
  assert.equal(day, Date.parse('2026-03-08T05:00:00Z'))
  assert.equal(zonedTime(2026, 2, 9, 0, 0) - day, 23 * 3600000)
  assert.equal(zonedTime(2026, 10, 2, 0, 0) - zonedTime(2026, 10, 1, 0, 0), 25 * 3600000)
})

test('Chatham uses its quarter-hour offset on either side of its DST changes', () => {
  setTetherZone('Pacific/Chatham')
  for (const [month, date, hour, minute, iso, actualHour] of [
    [3, 5, 1, 30, '2026-04-04T11:45:00Z', 1],
    [3, 5, 3, 15, '2026-04-04T13:30:00Z', 3],
    [3, 5, 3, 45, '2026-04-04T15:00:00Z', 3],
    [8, 27, 2, 30, '2026-09-26T13:45:00Z', 2],
    [8, 27, 3, 15, '2026-09-26T14:30:00Z', 4],
  ]) {
    const at = zonedTime(2026, month, date, hour, minute)
    assert.equal(at, Date.parse(iso))
    assert.deepEqual(zoneParts(at), { year: 2026, month, date, weekday: 0, hour: actualHour, minute })
  }
  assert.equal(offsetLabel('Pacific/Chatham', Date.parse('2026-07-01T00:00:00Z')), 'GMT+12:45')
  assert.equal(offsetLabel('Pacific/Chatham', Date.parse('2026-01-01T00:00:00Z')), 'GMT+13:45')
})

test('day keys and formatted times follow the selected zone', () => {
  const before = Date.parse('2026-11-17T18:00:00Z')
  const after = Date.parse('2026-11-17T19:00:00Z')
  setTetherZone(DEFAULT_ZONE)
  assert.equal(dayKey(after), '2026-11-18')
  assert.equal(sameDay(before, after), false)
  assert.equal(offsetLabel(DEFAULT_ZONE, after), 'GMT+5:30')
  setTetherZone('America/New_York')
  assert.equal(dayKey(after), '2026-11-17')
  assert.equal(sameDay(before, after), true)
  for (const [format, options] of [
    [fmtDate, { day: 'numeric', month: 'short' }],
    [fmtTime, { hour: '2-digit', minute: '2-digit' }],
    [fmtDateTime, { dateStyle: 'medium', timeStyle: 'short' }],
  ]) {
    const expected = new Intl.DateTimeFormat(undefined, { ...options, timeZone: tetherZone() }).format(after)
    assert.equal(format(after, options), expected)
    assert.equal(format(new Date(after), options), expected)
  }
})

test('invalid settings leave the active zone in place and the list includes UTC', () => {
  setTetherZone('America/New_York')
  setTetherZone('Not/AZone')
  assert.equal(tetherZone(), 'America/New_York')
  assert.ok(zoneList().includes('UTC'))
})

test('zone selection skips invalid preferences and preserves renamed zones', () => {
  assert.equal(pickZone('../etc', 'Pacific/Auckland'), 'Pacific/Auckland')
  assert.equal(pickZone('Not/AZone', 'Bad/Too'), DEFAULT_ZONE)
  assert.equal(pickZone('Asia/Calcutta', 'Pacific/Auckland'), 'Asia/Kolkata')
  assert.equal(pickZone('America/New_York', 'UTC'), 'America/New_York')
})

test('a bad timestamp gives NaN parts and "Invalid Date", not a thrown RangeError', () => {
  setTetherZone('America/New_York')
  for (const bad of [NaN, Infinity, -Infinity]) {
    const parts = zoneParts(bad)
    assert.ok(Number.isNaN(parts.year) && Number.isNaN(parts.hour) && parts.weekday === -1)
    assert.equal(dayKey(bad).includes('NaN'), true)
    assert.equal(fmtDate(bad), 'Invalid Date')
  }
})
