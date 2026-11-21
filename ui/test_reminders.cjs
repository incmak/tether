// Reminders' time, grouping, and push actions. Run from the repo root: node ui/test_reminders.cjs
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')
const ts = require('typescript')

const seconds = iso => Date.parse(iso) / 1000
const NOW = seconds('2026-11-18T06:30:00Z')
const TOMORROW = seconds('2026-11-19T04:30:00Z')
const parseCases = [
  ['tomorrow 10:00', TOMORROW], ['tomorrow 10am', TOMORROW], ['2026-11-19 10:00', TOMORROW],
  ['in 2 h', NOW + 7200], ['10:00', TOMORROW], ['tomorrow', seconds('2026-11-19T03:30:00Z')],
  ['tonight', seconds('2026-11-18T16:30:00Z')],
  ['soon', null], ['', null], ['call mom tomorrow', null],
]

function loadReminders() {
  const file = path.join(__dirname, 'src/reminders.ts')
  const mod = new module.constructor(file)
  // The polling hook is not called. Load the real chrono-node package via its CJS exports.
  mod.require = name => name === './hooks' ? {} : require(name)
  mod._compile(ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, file)
  return mod.exports
}

if (process.argv[2] === '--parse-in-zone') {
  const { parseWhen } = loadReminders()
  assert.equal(new Date(NOW * 1000).getTimezoneOffset(), Number(process.argv[3]))
  for (const [input, expected] of parseCases) assert.equal(parseWhen(input, NOW), expected, JSON.stringify(input))
  return
}

for (const [zone, offset] of [['UTC', 0], ['America/New_York', 300]]) {
  test(`parseWhen in ${zone}`, () => {
    // Each process loads Chrono after its time zone is set, with its own module cache.
    execFileSync(process.execPath, [__filename, '--parse-in-zone', String(offset)], { env: { ...process.env, TZ: zone }, stdio: 'inherit' })
  })
}

test('labels use the IST day and include the year only when needed', () => {
  const { whenLabel, fullLabel } = loadReminders()
  const labels = [
    ['2026-11-18T04:30:00Z', 'Today 10:00'], ['2026-11-19T04:30:00Z', 'Tomorrow 10:00'],
    ['2026-11-17T12:30:00Z', 'Yesterday 18:00'], ['2026-11-25T04:30:00Z', 'Wed 25 Nov 10:00'],
    ['2027-11-25T04:30:00Z', 'Thu 25 Nov 2027 10:00'], ['2026-11-18T19:00:00Z', 'Tomorrow 00:30'],
  ]
  for (const [iso, label] of labels) assert.equal(whenLabel(seconds(iso), NOW), label)
  assert.equal(fullLabel(TOMORROW), 'Thu 19 Nov 2026, 10:00 IST')
})

test('repeat labels use the anchor weekday in IST', () => {
  const { repeatLabel } = loadReminders()
  const rule = { anchor: seconds('2026-11-18T19:00:00Z'), zone: 'Asia/Kolkata' }
  assert.equal(repeatLabel(null), '')
  assert.equal(repeatLabel(undefined), '')
  assert.equal(repeatLabel({ ...rule, every: 'day' }), 'Every day')
  assert.equal(repeatLabel({ ...rule, every: 'weekday' }), 'Every weekday')
  assert.equal(repeatLabel({ ...rule, every: 'week' }), 'Every Thursday')
  assert.equal(repeatLabel({ ...rule, every: 'week', anchor: seconds('2026-11-23T04:30:00Z') }), 'Every Monday')
})

test('repeat labels name N, the month day and the year date from the rule, not a clamped anchor', () => {
  const { repeatLabel } = loadReminders()
  const feb28 = seconds('2027-02-28T04:30:00Z'), zone = 'Asia/Kolkata'
  assert.equal(repeatLabel({ every: 'day', interval: 3, anchor: feb28, zone }), 'Every 3 days')
  assert.equal(repeatLabel({ every: 'week', interval: 2, anchor: feb28, zone }), 'Every 2 weeks on Sunday')
  assert.equal(repeatLabel({ every: 'month', day: 31, anchor: feb28, zone }), 'Every month on the 31st')
  assert.equal(repeatLabel({ every: 'month', day: -1, anchor: feb28, zone }), 'Every month on the last day')
  assert.equal(repeatLabel({ every: 'month', interval: 3, day: 22, anchor: feb28, zone }), 'Every 3 months on the 22nd')
  assert.equal(repeatLabel({ every: 'year', day: 29, month: 2, anchor: feb28, zone }), 'Every year on 29 Feb')
  assert.equal(repeatLabel({ every: 'year', interval: 2, day: 21, month: 11, anchor: seconds('2026-11-21T04:30:00Z'), zone }), 'Every 2 years on 21 Nov')
  // A rule from before day and month existed falls back to its anchor.
  assert.equal(repeatLabel({ every: 'month', anchor: seconds('2026-11-13T04:30:00Z'), zone }), 'Every month on the 13th')
})

test('ordinals', () => {
  const { ordinal } = loadReminders()
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 30, 31].map(ordinal), ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '30th', '31st'])
})

test('repeat options name the date; the last-day option shows only for a short month end', () => {
  const { repeatOptions } = loadReminders()
  const values = at => repeatOptions(at).map(option => option.value)
  const detail = (at, value, rule) => repeatOptions(at, rule).find(option => option.value === value)?.detail
  assert.deepEqual(values(null), ['never', 'day', 'weekday', 'week', 'month', 'year', 'custom'])
  assert.equal(detail(null, 'week'), 'Same day each week')
  assert.equal(detail(TOMORROW, 'week'), 'On Thursdays')
  assert.equal(detail(TOMORROW, 'month'), 'On the 19th')
  assert.equal(detail(TOMORROW, 'year'), 'On 19 Nov')
  const nov30 = seconds('2026-11-30T04:30:00Z'), dec31 = seconds('2026-12-31T04:30:00Z'), feb29 = seconds('2028-02-29T04:30:00Z')
  assert.deepEqual(values(nov30), ['never', 'day', 'weekday', 'week', 'month', 'last', 'year', 'custom'])
  assert.equal(detail(nov30, 'month'), 'On the 30th, or the last day')
  assert.deepEqual(values(dec31), values(null), 'the 31st already is the last day')
  assert.equal(detail(feb29, 'year'), "On 29 Feb; 28 Feb in other years")
  // An edit that keeps a clamped 28 Feb copy of a 31st series names the 31st, and offers no last-day choice.
  const rule = { every: 'month', day: 31, anchor: seconds('2027-02-28T04:30:00Z'), zone: 'Asia/Kolkata' }
  assert.equal(detail(rule.anchor, 'month', rule), 'On the 31st, or the last day')
  assert.equal(repeatOptions(rule.anchor, rule).some(option => option.value === 'last'), false)
  assert.equal(repeatOptions(TOMORROW, null, 'last').some(option => option.value === 'last'), true, 'a last-day rule keeps its option')
})

test('repeat choices round-trip to the request body and validate N per unit', () => {
  const { choiceOf, repeatBody, choiceValid, sameChoice, choiceLabel, NO_REPEAT } = loadReminders()
  const anchor = TOMORROW, zone = 'Asia/Kolkata'
  assert.equal(repeatBody(choiceOf(null)), null)
  assert.equal(repeatBody(choiceOf({ every: 'week', anchor, zone })), 'week')
  assert.deepEqual(repeatBody(choiceOf({ every: 'month', day: -1, anchor, zone })), { every: 'month', last_day: true })
  assert.deepEqual(choiceOf({ every: 'month', interval: 3, day: 19, anchor, zone }), { pick: 'custom', unit: 'month', interval: 3 })
  assert.deepEqual(repeatBody({ pick: 'custom', unit: 'week', interval: 2 }), { every: 'week', interval: 2 })
  assert.equal(repeatBody({ pick: 'custom', unit: 'day', interval: 1 }), 'day', 'every 1 day is the plain rule')
  assert.equal(sameChoice({ pick: 'custom', unit: 'year', interval: 1 }, { ...NO_REPEAT, pick: 'year' }), true)
  for (const [unit, max] of [['day', 365], ['week', 52], ['month', 60], ['year', 5]]) {
    assert.equal(choiceValid({ pick: 'custom', unit, interval: max }), true, unit)
    assert.equal(choiceValid({ pick: 'custom', unit, interval: max + 1 }), false, unit)
  }
  for (const interval of [0, -1, 1.5, NaN]) assert.equal(choiceValid({ pick: 'custom', unit: 'day', interval }), false, String(interval))
  assert.equal(choiceValid({ pick: 'never', unit: 'day', interval: NaN }), true, 'a hidden N does not block')
  assert.equal(choiceLabel({ pick: 'custom', unit: 'week', interval: 2 }, TOMORROW), 'Every 2 weeks on Thursday')
  assert.equal(choiceLabel({ pick: 'custom', unit: 'month', interval: 3 }, seconds('2027-02-28T04:30:00Z'), { every: 'month', day: 31, anchor, zone }), 'Every 3 months on the 31st')
})

test('quick picks respect the evening cutoff and round-trip across month end', () => {
  const { quickPicks, parseWhen } = loadReminders()
  const late = seconds('2026-11-18T12:00:00Z')
  const monthEnd = seconds('2026-11-30T06:30:43Z')
  assert.ok(quickPicks(NOW).some(pick => pick.label === 'This evening 18:00'))
  assert.ok(!quickPicks(late).some(pick => pick.label === 'This evening 18:00'))
  assert.equal(quickPicks(monthEnd).find(pick => pick.label === 'Tomorrow 10:00')?.at, seconds('2026-12-01T04:30:00Z'))
  for (const now of [NOW, late, monthEnd]) {
    for (const { label, at, typed } of quickPicks(now)) {
      const parsed = parseWhen(typed, now)
      if (label === 'In 1 h') assert.ok(parsed !== null && Math.abs(parsed - at) <= 60, `${typed} at ${now}`)
      else assert.equal(parsed, at, `${typed} at ${now}`)
    }
  }
})

test('snooze offers 10 minutes, an hour, and 10:00 IST tomorrow', () => {
  const { snoozePicks } = loadReminders()
  assert.deepEqual(snoozePicks(NOW).map(({ label, at }) => [label, at]), [
    ['10 min', NOW + 600], ['1 h', NOW + 3600], ['Tomorrow 10:00', TOMORROW],
  ])
})

test('group order and newest 20 done', () => {
  const { groupReminders } = loadReminders()
  const item = (text, state, due, done_at = null) => ({ id: text, text, state, due, done_at })
  const older = item('Older due', 'due', NOW - 120)
  const newer = item('Newer due', 'due', NOW - 60)
  const soon = item('Soon', 'scheduled', NOW + 60)
  const later = item('Later', 'scheduled', NOW + 120)
  const done = Array.from({ length: 22 }, (_, i) => item(`Done ${i}`, 'done', NOW - i - 100, NOW + i))
  const groups = groupReminders([later, newer, ...done, soon, older])
  assert.deepEqual(groups.due, [older, newer])
  assert.deepEqual(groups.upcoming, [soon, later])
  assert.equal(groups.done.length, 20)
  assert.deepEqual(groups.done, done.slice(2).reverse())
})

test('service worker sends the occurrence and opens Reminders from a stale notice', async () => {
  const handlers = new Map(), shown = [], calls = [], opened = []
  const active = new Map()
  const stale = 'This reminder changed after that notice. Open Reminders to see it.'
  let status = 200
  let release
  const self = {
    addEventListener: (name, fn) => handlers.set(name, fn),
    registration: {
      getNotifications: async () => [],
      showNotification: async (title, options) => {
        const notice = { title, ...structuredClone(options) }
        shown.push(notice)
        active.set(notice.tag, notice)
      },
    },
    clients: { matchAll: async () => [], openWindow: async url => opened.push(url) },
  }
  const file = path.join(__dirname, 'public/sw.js')
  vm.runInNewContext(readFileSync(file, 'utf8'), {
    self, Date: { now: () => assert.fail('Snooze must use the server clock') },
    fetch: async (url, options) => {
      calls.push({ url, ...structuredClone(options), body: JSON.parse(options.body) })
      if (status === 409) await new Promise(resolve => { release = resolve })
      return { status, ok: status === 200, json: async () => ({ error: status === 409 ? stale : 'Reminder R1 was deleted.' }) }
    },
  }, { filename: file })
  const fire = async (name, event) => {
    let work
    handlers.get(name)({ ...event, waitUntil: promise => { work = promise } })
    assert.ok(work, `${name} extends the event lifetime`)
    await work
  }
  const push = occ => fire('push', { data: { json: () => ({ title: 'Reminder', tag: 'reminder-R1', reminder: 'R1', occ }) } })
  await push(2)
  assert.equal(shown.length, 1)
  const [notification] = shown
  assert.equal(notification.data.reminder, 'R1')
  assert.equal(notification.data.occ, 2)
  assert.deepEqual(notification.actions, [
    { action: 'reminder-done', title: 'Done' }, { action: 'reminder-snooze', title: 'Snooze 1 h' },
  ])
  const click = action => fire('notificationclick', { action, notification: { ...notification, close() {} } })
  await click('reminder-done')
  await click('reminder-snooze')
  const post = (action, body) => ({
    url: `/mobile/api/reminders/${action}`, body, method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
  })
  assert.deepEqual(calls, [post('done', { id: 'R1', occ: 2 }), post('snooze', { id: 'R1', occ: 2, for: 3600 })])
  assert.equal(shown.length, 1, 'successful actions do not show an error')
  status = 404
  await click('reminder-done')
  assert.deepEqual(calls[2], post('done', { id: 'R1', occ: 2 }))
  assert.equal(shown[1]?.title, 'Reminder: not done')
  assert.match(shown[1].body, /Reminder R1 was deleted\./)
  assert.deepEqual(shown[1].data, { go: 'reminders' })
  status = 409
  const staleAction = click('reminder-snooze')
  await push(3)
  release()
  await staleAction
  assert.deepEqual(calls[3], post('snooze', { id: 'R1', occ: 2, for: 3600 }))
  const newest = active.get('reminder-R1')
  assert.equal(newest.data.occ, 3)
  assert.deepEqual(newest.actions, notification.actions)
  const error = active.get('reminder-R1-action')
  assert.equal(error.title, 'Reminder changed')
  assert.equal(error.body, stale)
  assert.deepEqual(error.data, { go: 'reminders' })
  assert.equal(shown[1].tag, 'reminder-R1-action')
  assert.deepEqual(opened, [], 'actions do not open a tab')
  await fire('notificationclick', { action: '', notification: { ...error, close() {} } })
  assert.deepEqual(opened, ['/chat/?go=reminders'])
})
