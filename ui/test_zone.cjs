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
  DEFAULT_ZONE, dayKey, fmtDate, fmtDateTime, fmtDay, fmtTime, offsetLabel, pickZone,
  sameDay, setTetherZone, startOfDay, tetherZone, zonedTime, zoneLabel, zoneList, zoneParts,
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

test('a server-confirmed zone this browser cannot format leaves the active zone in place', () => {
  setTetherZone('America/New_York')
  // The server's tzdata can know a name this browser's ICU does not. Times must keep a zone, not read "Invalid Date".
  setTetherZone('Not/AZone', true)
  assert.equal(tetherZone(), 'America/New_York')
  assert.notEqual(fmtTime(Date.UTC(2026, 10, 22, 12, 0)), 'Invalid Date')
  setTetherZone('Etc/Not_A_Zone', true)
  assert.equal(tetherZone(), 'America/New_York')
  // A confirmed old name that has a renamed copy still applies and formats.
  setTetherZone('Asia/Calcutta', true)
  assert.equal(tetherZone(), 'Asia/Calcutta')
  assert.equal(fmtTime(Date.UTC(2026, 10, 22, 12, 0), { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }), '17:30')
  setTetherZone('America/New_York')
})

test('zone selection skips invalid preferences and preserves renamed zones', () => {
  assert.equal(pickZone('../etc', 'Pacific/Auckland'), 'Pacific/Auckland')
  assert.equal(pickZone('Not/AZone', 'Bad/Too'), DEFAULT_ZONE)
  assert.equal(pickZone('Asia/Calcutta', 'Pacific/Auckland'), 'Asia/Kolkata')
  assert.equal(pickZone('America/New_York', 'UTC'), 'America/New_York')
})

for (const bad of [NaN, Infinity, -Infinity, 1e20, -1e20]) {
  test(`date formatters handle invalid epoch ${bad}`, () => {
    const zone = 'America/New_York'
    setTetherZone(zone)
    assert.ok(Object.values(zoneParts(bad)).every(Number.isNaN))
    assert.ok(dayKey(bad).includes('NaN'))
    for (const format of [fmtTime, fmtDate, fmtDateTime]) {
      assert.equal(format(bad), 'Invalid Date')
      assert.equal(format(new Date(bad)), 'Invalid Date')
    }
    for (const label of [offsetLabel, zoneLabel]) assert.match(label(zone, bad), /^(?:Invalid Date|America\/New_York)$/)
  })
}

for (const day of [undefined, '', 'garbage', '2026-13-45', '2026-02-29']) {
  test(`fmtDay handles invalid calendar day ${JSON.stringify(day)}`, () => {
    assert.match(fmtDay(day), /^(?:Invalid Date)?$/)
  })
}
