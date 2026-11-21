// Reminders (server/reminders.py): Tether delivers reminders at their time on the urgent channels
// (push, urgent ping, Waiting on you). Times are UTC epoch seconds on the wire and show in Tether's time zone.
import { useCallback, useEffect } from 'react'
import { parse as parseDate } from 'chrono-node'
import type { Api } from './api'
import type { MenuOption } from './SelectMenu'
import { usePolling } from './hooks'
import { offsetMinutes, tetherZone, zoneLabel, zoneParts, zonedTime, zonedTimes } from './zone'

export type ReminderState = 'scheduled' | 'due' | 'done'
export type Repeat = 'day' | 'weekday' | 'week' | 'month' | 'year'
export type RepeatUnit = 'day' | 'week' | 'month' | 'year'
/** interval: every N units (1 when absent). day: month and year rules, 1–31 or -1 for the last day; a day the month lacks
 *  moves to its last day. month: year rules, 1–12. The server sets day and month; they outlive a clamped anchor. */
export interface RepeatRule { every: Repeat; interval?: number; day?: number; month?: number; anchor: number; zone: string; origin?: number }
export interface ReminderLink { pane?: string; thread?: string; project?: string; item?: string }
export interface Reminder {
  id: string; text: string; due: number; created: number; by: string; state: ReminderState
  fired_at: number | null; late: boolean; snoozes: number; done_at: number | null; link: ReminderLink | null
  /** The schedule's number: it changes on each snooze or new time, so an old notice cannot act on a new schedule. */
  occ?: number
  /** Channels that gave up after their retries. */
  failed?: string[]
  repeat?: RepeatRule | null
  next_id?: string | null
  /** The task app this reminder mirrors (server/task_sources.py). Tether never sends it as a Teams ping. */
  source?: { key: string; label: string } | null
}
/** A deleted reminder, kept for 30 days in Recently deleted. */
export interface Deleted extends Reminder { deleted_at: number }
export interface Listing { reminders: Reminder[]; deleted?: Deleted[]; now: number }
export type Action = 'add' | 'edit' | 'done' | 'reopen' | 'snooze' | 'delete' | 'restore'

export const PATH = 'reminders'
export const TEXT_LIMIT = 500
const DONE_SHOWN = 20
const POLL_MS = 15000
/** Others (the sheet, the sidebar) refresh at once after a change, instead of after the next poll. */
const EVENT = 'tether-reminders-changed'

/** Keep the zone on stored rules. An unknown stored zone uses Tether's current zone. */
function parts(at: number, zone = tetherZone()) {
  try { return zoneParts(at * 1000, zone) } catch (error) {
    if (!(error instanceof RangeError) || zone === tetherZone()) throw error
    return zoneParts(at * 1000)
  }
}
/** Epoch seconds of a wall time in Tether's zone. Day overflow rolls into the next month. */
export const zoneTime = (year: number, month: number, date: number, hour: number, minute = 0) => zonedTime(year, month, date, hour, minute) / 1000
const dayNumber = ({ year, month, date }: ReturnType<typeof parts>) => Date.UTC(year, month, date) / 86400000

const pad = (value: number) => String(value).padStart(2, '0')
const clock = ({ hour, minute }: ReturnType<typeof parts>) => `${pad(hour)}:${pad(minute)}`
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** The largest N for "every N" per unit; each keeps one step within the server's five years ahead. */
export const INTERVAL_MAX: Record<RepeatUnit, number> = { day: 365, week: 52, month: 60, year: 5 }
const UNIT_NAMES: Record<RepeatUnit, [string, string]> = { day: ['day', 'days'], week: ['week', 'weeks'], month: ['month', 'months'], year: ['year', 'years'] }
export const unitName = (unit: RepeatUnit, count: number) => UNIT_NAMES[unit][count === 1 ? 0 : 1]

/** 1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st, 22nd, 23rd, 31st. */
export function ordinal(value: number) {
  const tens = value % 100, ones = value % 10
  return value + (tens >= 11 && tens <= 13 ? 'th' : ones === 1 ? 'st' : ones === 2 ? 'nd' : ones === 3 ? 'rd' : 'th')
}

/** The calendar facts a rule repeats on: from the rule's own day and month when it has them, else from a date in
 *  `zone` (the kept rule's zone or Tether's current zone for a new rule). */
interface Target { weekday: number; day: number; month: number }
function target(at: number, rule?: RepeatRule | null, zone = rule?.zone): Target {
  const p = parts(at, zone)
  const keeps = rule && (rule.every === 'month' || rule.every === 'year') && typeof rule.day === 'number'
  return { weekday: p.weekday, day: keeps ? rule.day! : p.date, month: keeps && rule.every === 'year' && typeof rule.month === 'number' ? rule.month - 1 : p.month }
}
const lastDay = (at: number, zone?: string) => { const p = parts(at, zone); return new Date(Date.UTC(p.year, p.month + 1, 0)).getUTCDate() }

function describe(every: Repeat, interval: number, on: Target) {
  const each = interval > 1 ? `Every ${interval} ${unitName(every === 'weekday' ? 'day' : every, interval)}` : ''
  if (every === 'day') return each || 'Every day'
  if (every === 'weekday') return 'Every weekday'
  if (every === 'week') return each ? `${each} on ${WEEKDAYS[on.weekday]}` : `Every ${WEEKDAYS[on.weekday]}`
  if (every === 'month') return `${each || 'Every month'} on the ${on.day === -1 ? 'last day' : ordinal(on.day)}`
  return `${each || 'Every year'} on ${on.day === -1 ? 'the last day of' : on.day} ${MONTHS[on.month]}`
}

export function repeatLabel(rule: RepeatRule | null | undefined): string {
  if (!rule) return ''
  return describe(rule.every, rule.interval ?? 1, target(rule.anchor, rule))
}

/** What the Repeat picker holds. last: the last day of every month. custom: every `interval` `unit`s;
 *  lastDay: a custom monthly rule falls on the last day of the month. */
export type RepeatPick = 'never' | Repeat | 'last' | 'custom'
export interface RepeatChoice { pick: RepeatPick; unit: RepeatUnit; interval: number; lastDay: boolean }
export const NO_REPEAT: RepeatChoice = { pick: 'never', unit: 'week', interval: 2, lastDay: false }
/** The choice falls on the last day of the month. */
export const onLastDay = (choice: RepeatChoice) => choice.pick === 'last' || (choice.pick === 'custom' && choice.unit === 'month' && choice.lastDay === true)

/** The picker's value for a stored rule. */
export function choiceOf(rule: RepeatRule | null | undefined): RepeatChoice {
  if (!rule) return NO_REPEAT
  const interval = rule.interval ?? 1
  if (interval > 1 && rule.every !== 'weekday') return { pick: 'custom', unit: rule.every, interval, lastDay: rule.every === 'month' && rule.day === -1 }
  if (rule.every === 'month' && rule.day === -1) return { ...NO_REPEAT, pick: 'last' }
  return { ...NO_REPEAT, pick: rule.every }
}

/** A custom N is a whole number from 1 to the unit's limit; the other picks are always valid. */
export const choiceValid = (choice: RepeatChoice) => choice.pick !== 'custom'
  || (Number.isInteger(choice.interval) && choice.interval >= 1 && choice.interval <= INTERVAL_MAX[choice.unit])

/** The `repeat` the server takes: null, a rule name (every 1), or { every, interval?, last_day? }. */
export type RepeatBody = null | Repeat | { every: RepeatUnit; interval?: number; last_day?: true }
export function repeatBody(choice: RepeatChoice): RepeatBody {
  if (choice.pick === 'never') return null
  if (onLastDay(choice)) return choice.pick === 'custom' && choice.interval > 1
    ? { every: 'month', interval: choice.interval, last_day: true } : { every: 'month', last_day: true }
  if (choice.pick !== 'custom') return choice.pick as Repeat
  return choice.interval === 1 ? choice.unit : { every: choice.unit, interval: choice.interval }
}
export const sameChoice = (a: RepeatChoice, b: RepeatChoice) => JSON.stringify(repeatBody(a)) === JSON.stringify(repeatBody(b))

/** The rule a choice makes on a date, in words: "Every 2 weeks on Thursday". rule: the kept rule (an edit that keeps its time). */
export function choiceLabel(choice: RepeatChoice, at: number, rule?: RepeatRule | null): string {
  const body = repeatBody(choice)
  if (body === null) return ''
  const every = typeof body === 'string' ? body : body.every
  const last = onLastDay(choice)
  const on = target(at, keptFor(every, last, rule), rule?.zone)
  if (last) on.day = -1
  return describe(every, typeof body === 'object' ? body.interval ?? 1 : 1, on)
}

/** The kept rule whose own day and month a new rule takes, as the server does: the same unit and the same last-day
 *  setting. Any other change takes the day from the date. */
const keptFor = (every: Repeat, last: boolean, rule?: RepeatRule | null) =>
  rule && rule.every === every && (rule.day === -1) === last ? rule : null

/** The picker's choices. at: the date the rule starts from; rule: the kept rule, whose own day wins over a clamped date. */
export function repeatOptions(at: number | null, rule?: RepeatRule | null, current?: RepeatPick): MenuOption[] {
  const zone = rule?.zone
  const on = at === null ? null : target(at, keptFor('month', false, rule), zone)
  const yearly = at === null ? null : target(at, keptFor('year', false, rule), zone)
  const shorter = on && on.day >= 29 ? `, or the last day` : ''
  const options: MenuOption[] = [
    { value: 'never', label: 'Never' },
    { value: 'day', label: 'Every day' },
    { value: 'weekday', label: 'Every weekday', detail: 'Mon–Fri' },
    { value: 'week', label: 'Every week', detail: on ? `On ${WEEKDAYS[on.weekday]}s` : 'Same day each week' },
    { value: 'month', label: 'Every month', detail: on ? `On the ${ordinal(on.day)}${shorter}` : 'Same date each month' },
  ]
  // The last day differs from "the same date" only for a date that ends a month shorter than 31 days (30 Nov, 28 Feb).
  if (current === 'last' || (at !== null && on && on.day === parts(at, zone).date && on.day === lastDay(at, zone) && on.day < 31)) {
    options.push({ value: 'last', label: 'Last day of every month', detail: 'The 28th to the 31st, as the month ends' })
  }
  options.push(
    { value: 'year', label: 'Every year', detail: yearly ? `On ${yearly.day} ${MONTHS[yearly.month]}${yearly.day === 29 && yearly.month === 1 ? '; 28 Feb in other years' : ''}` : 'Same date each year' },
    { value: 'custom', label: 'Custom', detail: 'Every N days, weeks, months or years' },
  )
  return options
}

/** "Today 10:00", "Tomorrow 10:00", "Yesterday 18:00", "Wed 25 Nov 10:00", "Wed 25 Nov 2027 10:00". */
export function whenLabel(at: number, now: number) {
  const p = parts(at), today = parts(now)
  const days = dayNumber(p) - dayNumber(today)
  if (days === 0) return `Today ${clock(p)}`
  if (days === 1) return `Tomorrow ${clock(p)}`
  if (days === -1) return `Yesterday ${clock(p)}`
  const year = today.year === p.year ? '' : ` ${p.year}`
  return `${DAYS[p.weekday]} ${p.date} ${MONTHS[p.month]}${year} ${clock(p)}`
}

/** The full date and time with the zone, for titles and screen readers. */
export function fullLabel(at: number) {
  const p = parts(at)
  return `${DAYS[p.weekday]} ${p.date} ${MONTHS[p.month]} ${p.year}, ${clock(p)} ${zoneLabel(tetherZone(), at * 1000)}`
}

/** typed: the When text a pick fills in, so the field stays the one source of the time. */
export interface Pick { label: string; at: number; typed: string }

const tomorrowAt = (now: number, hour: number) => { const p = parts(now); return zoneTime(p.year, p.month, p.date + 1, hour) }

/** Quick picks for a new reminder (research: Slack, Apple Reminders, Things). "This evening" only before 17:00. */
export function quickPicks(now: number): Pick[] {
  const p = parts(now)
  const picks: Pick[] = [{ label: 'In 1 h', at: Math.round(now / 60) * 60 + 3600, typed: 'in 1 hour' }]
  if (p.hour < 17) picks.push({ label: 'This evening 18:00', at: zoneTime(p.year, p.month, p.date, 18), typed: 'today 18:00' })
  picks.push({ label: 'Tomorrow 10:00', at: tomorrowAt(now, 10), typed: 'tomorrow 10:00' })
  return picks
}

/** Snooze choices, from a notification or a row. */
export function snoozePicks(now: number): Pick[] {
  return [{ label: '10 min', at: now + 600, typed: 'in 10 minutes' }, { label: '1 h', at: now + 3600, typed: 'in 1 hour' }, { label: 'Tomorrow 10:00', at: tomorrowAt(now, 10), typed: 'tomorrow 10:00' }]
}

const PART_OF_DAY = /\b(tonight|evening|morning|afternoon|noon|midnight|night)\b/i

/** A typed time ("tomorrow 10am", "in 2 h", "fri 18:00", "3 dec 15:00") in Tether's zone.
 *  A day without a time means 09:00, as in Slack and Apple Reminders. null: not a time. */
export function parseWhen(text: string, now: number): number | null {
  const typed = text.trim()
  if (!typed) return null
  // An elapsed time is plain arithmetic. Chrono adds it on the device's calendar, which is off by an hour across a
  // device DST change, so read it here, as the server does ("in 90m", "in 2h", "in 1d").
  const elapsed = /^in\s+(\d{1,5})\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?)\.?$/i.exec(typed)
  if (elapsed) return now + Number(elapsed[1]) * ({ m: 60, h: 3600, d: 86400 } as Record<string, number>)[elapsed[2][0].toLowerCase()]
  const instant = new Date(now * 1000)
  // Chrono accepts offsets and abbreviations. Use the current offset for its reference date.
  const found = parseDate(typed, { instant, timezone: offsetMinutes(tetherZone(), instant.getTime()) }, { forwardDate: true })[0]
  // Text around the time ("call mom tomorrow") belongs in the reminder text, not here.
  if (!found || found.text.length < typed.replace(/[\s.,]+$/, '').length - 1) return null
  const time = found.start
  const year = time.get('year')!, month = time.get('month')! - 1, date = time.get('day')!
  if (!time.isCertain('hour') && !PART_OF_DAY.test(typed) && !/\bin\s+\d/i.test(typed)) return zoneTime(year, month, date, 9)
  // Explicit offsets and elapsed times already identify an instant.
  if (time.isCertain('timezoneOffset')) return Math.floor(found.date().getTime() / 1000)
  const hour = time.get('hour')!, minute = time.get('minute') ?? 0, second = time.get('second') ?? 0
  if (time.isCertain('hour') && !time.isCertain('day') && !time.isCertain('weekday') && !time.isCertain('month')) {
    // Chrono's reference offset cannot resolve a bare clock time across an offset change.
    const today = parts(now)
    const upcoming = zonedTimes(today.year, today.month, today.date, hour, minute)
      .map(at => at / 1000 + second).find(at => at > now)
    return upcoming ?? (zoneTime(today.year, today.month, today.date + 1, hour, minute) + second)
  }
  // Re-read wall times in the zone at the target date, where its offset may differ.
  return zoneTime(year, month, date, hour, minute) + second
}

export interface Groups { due: Reminder[]; upcoming: Reminder[]; done: Reminder[] }

/** Due (overdue first: the oldest due time first), upcoming (soonest first), done (the newest 20). */
export function groupReminders(list: Reminder[]): Groups {
  const by = (key: (item: Reminder) => number) => (a: Reminder, b: Reminder) => key(a) - key(b)
  return {
    due: list.filter(item => item.state === 'due').sort(by(item => item.due)),
    upcoming: list.filter(item => item.state === 'scheduled').sort(by(item => item.due)),
    done: list.filter(item => item.state === 'done').sort(by(item => -(item.done_at ?? 0))).slice(0, DONE_SHOWN),
  }
}

/** Ask the sheet and the sidebar to load the list again. */
export const announce = () => window.dispatchEvent(new Event(EVENT))

/** POST one action. The server answers with the reminder (null after delete). */
export async function act(api: Api, action: Action, body: Record<string, unknown>) {
  const result = await api<{ reminder: Reminder | null }>(`${PATH}/${action}`, body)
  announce()
  return result.reminder
}

/** The list, polled every 15 s while visible and loaded again after any change in this page. */
export function useReminders(api: Api, priority: RequestPriority = 'auto') {
  const fetcher = useCallback((signal: AbortSignal) => api<Listing>(PATH, undefined, signal, undefined, undefined, priority), [api, priority])
  const polled = usePolling(fetcher, POLL_MS)
  const { refresh } = polled
  useEffect(() => {
    const changed = () => void refresh()
    window.addEventListener(EVENT, changed)
    return () => window.removeEventListener(EVENT, changed)
  }, [refresh])
  return polled
}
