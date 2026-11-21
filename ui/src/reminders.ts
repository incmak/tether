// Reminders (server/reminders.py): Tether delivers reminders at their time on the urgent channels
// (push, urgent ping, Waiting on you). Times are UTC epoch seconds on the wire and show in India Standard Time.
// IST has no daylight saving time, so a fixed +05:30 offset converts both ways.
import { useCallback, useEffect } from 'react'
import { parse as parseDate } from 'chrono-node'
import type { Api } from './api'
import type { MenuOption } from './SelectMenu'
import { usePolling } from './hooks'

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
export const ZONE = 'Asia/Kolkata'
export const ZONE_LABEL = 'IST'
const OFFSET = 330 * 60  // IST, seconds east of UTC
const DONE_SHOWN = 20
const POLL_MS = 15000
/** Others (the sheet, the sidebar) refresh at once after a change, instead of after the next poll. */
const EVENT = 'tether-reminders-changed'

/** The IST calendar parts of an instant. */
function parts(at: number) {
  const day = new Date((at + OFFSET) * 1000)
  return { year: day.getUTCFullYear(), month: day.getUTCMonth(), date: day.getUTCDate(), hour: day.getUTCHours(), minute: day.getUTCMinutes(), weekday: day.getUTCDay() }
}
/** The instant of an IST wall time. Day overflow (date 32) rolls into the next month. */
export const istTime = (year: number, month: number, date: number, hour: number, minute = 0) => Date.UTC(year, month, date, hour, minute) / 1000 - OFFSET
const dayNumber = (at: number) => Math.floor((at + OFFSET) / 86400)

const pad = (value: number) => String(value).padStart(2, '0')
const clock = (at: number) => { const p = parts(at); return `${pad(p.hour)}:${pad(p.minute)}` }
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

/** The calendar facts a rule repeats on: from the rule's own day and month when it has them, else from a date. */
interface Target { weekday: number; day: number; month: number }
function target(at: number, rule?: RepeatRule | null): Target {
  const p = parts(at)
  const keeps = rule && (rule.every === 'month' || rule.every === 'year') && typeof rule.day === 'number'
  return { weekday: p.weekday, day: keeps ? rule.day! : p.date, month: keeps && rule.every === 'year' && typeof rule.month === 'number' ? rule.month - 1 : p.month }
}
const lastDay = (at: number) => { const p = parts(at); return new Date(Date.UTC(p.year, p.month + 1, 0)).getUTCDate() }

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

/** What the Repeat picker holds. last: the last day of every month. custom: every `interval` `unit`s. */
export type RepeatPick = 'never' | Repeat | 'last' | 'custom'
export interface RepeatChoice { pick: RepeatPick; unit: RepeatUnit; interval: number }
export const NO_REPEAT: RepeatChoice = { pick: 'never', unit: 'week', interval: 2 }

/** The picker's value for a stored rule. */
export function choiceOf(rule: RepeatRule | null | undefined): RepeatChoice {
  if (!rule) return NO_REPEAT
  const interval = rule.interval ?? 1
  if (interval > 1 && rule.every !== 'weekday') return { pick: 'custom', unit: rule.every, interval }
  if (rule.every === 'month' && rule.day === -1) return { ...NO_REPEAT, pick: 'last' }
  return { ...NO_REPEAT, pick: rule.every }
}

/** A custom N is a whole number from 1 to the unit's limit; the other picks are always valid. */
export const choiceValid = (choice: RepeatChoice) => choice.pick !== 'custom'
  || (Number.isInteger(choice.interval) && choice.interval >= 1 && choice.interval <= INTERVAL_MAX[choice.unit])

/** The `repeat` the server takes: null, a rule name (every 1), or { every, interval } / { every: 'month', last_day }. */
export function repeatBody(choice: RepeatChoice): null | Repeat | { every: RepeatUnit; interval: number } | { every: 'month'; last_day: true } {
  if (choice.pick === 'never') return null
  if (choice.pick === 'last') return { every: 'month', last_day: true }
  if (choice.pick !== 'custom') return choice.pick
  return choice.interval === 1 ? choice.unit : { every: choice.unit, interval: choice.interval }
}
export const sameChoice = (a: RepeatChoice, b: RepeatChoice) => JSON.stringify(repeatBody(a)) === JSON.stringify(repeatBody(b))

/** The rule a choice makes on a date, in words: "Every 2 weeks on Thursday". rule: the kept rule (an edit that keeps its time). */
export function choiceLabel(choice: RepeatChoice, at: number, rule?: RepeatRule | null): string {
  const body = repeatBody(choice)
  if (body === null) return ''
  const every = typeof body === 'string' ? body : body.every
  const kept = rule && rule.every === every && (rule.day === -1) === (choice.pick === 'last') ? rule : null
  const on = target(at, kept)
  if (choice.pick === 'last') on.day = -1
  return describe(every, typeof body === 'object' && 'interval' in body ? body.interval : 1, on)
}

/** The picker's choices. at: the date the rule starts from; rule: the kept rule, whose own day wins over a clamped date. */
export function repeatOptions(at: number | null, rule?: RepeatRule | null, current?: RepeatPick): MenuOption[] {
  const on = at === null ? null : target(at, rule && (rule.every === 'month' || rule.every === 'year') && rule.day !== -1 ? rule : null)
  const shorter = on && on.day >= 29 ? `, or the last day` : ''
  const options: MenuOption[] = [
    { value: 'never', label: 'Never' },
    { value: 'day', label: 'Every day' },
    { value: 'weekday', label: 'Every weekday', detail: 'Mon–Fri' },
    { value: 'week', label: 'Every week', detail: on ? `On ${WEEKDAYS[on.weekday]}s` : 'Same day each week' },
    { value: 'month', label: 'Every month', detail: on ? `On the ${ordinal(on.day)}${shorter}` : 'Same date each month' },
  ]
  // The last day differs from "the same date" only for a date that ends a month shorter than 31 days (30 Nov, 28 Feb).
  if (current === 'last' || (at !== null && on && on.day === parts(at).date && on.day === lastDay(at) && on.day < 31)) {
    options.push({ value: 'last', label: 'Last day of every month', detail: 'The 28th to the 31st, as the month ends' })
  }
  options.push(
    { value: 'year', label: 'Every year', detail: on ? `On ${on.day} ${MONTHS[on.month]}${on.day === 29 && on.month === 1 ? '; 28 Feb in other years' : ''}` : 'Same date each year' },
    { value: 'custom', label: 'Custom', detail: 'Every N days, weeks, months or years' },
  )
  return options
}

/** "Today 10:00", "Tomorrow 10:00", "Yesterday 18:00", "Wed 25 Nov 10:00", "Wed 25 Nov 2027 10:00". */
export function whenLabel(at: number, now: number) {
  const days = dayNumber(at) - dayNumber(now)
  if (days === 0) return `Today ${clock(at)}`
  if (days === 1) return `Tomorrow ${clock(at)}`
  if (days === -1) return `Yesterday ${clock(at)}`
  const p = parts(at), year = parts(now).year === p.year ? '' : ` ${p.year}`
  return `${DAYS[p.weekday]} ${p.date} ${MONTHS[p.month]}${year} ${clock(at)}`
}

/** The full date and time with the zone, for titles and screen readers. */
export function fullLabel(at: number) {
  const p = parts(at)
  return `${DAYS[p.weekday]} ${p.date} ${MONTHS[p.month]} ${p.year}, ${clock(at)} ${ZONE_LABEL}`
}

/** typed: the When text a pick fills in, so the field stays the one source of the time. */
export interface Pick { label: string; at: number; typed: string }

const tomorrowAt = (now: number, hour: number) => { const p = parts(now); return istTime(p.year, p.month, p.date + 1, hour) }

/** Quick picks for a new reminder (research: Slack, Apple Reminders, Things). "This evening" only before 17:00. */
export function quickPicks(now: number): Pick[] {
  const p = parts(now)
  const picks: Pick[] = [{ label: 'In 1 h', at: Math.round(now / 60) * 60 + 3600, typed: 'in 1 hour' }]
  if (p.hour < 17) picks.push({ label: 'This evening 18:00', at: istTime(p.year, p.month, p.date, 18), typed: 'today 18:00' })
  picks.push({ label: 'Tomorrow 10:00', at: tomorrowAt(now, 10), typed: 'tomorrow 10:00' })
  return picks
}

/** Snooze choices, from a notification or a row. */
export function snoozePicks(now: number): Pick[] {
  return [{ label: '10 min', at: now + 600, typed: 'in 10 minutes' }, { label: '1 h', at: now + 3600, typed: 'in 1 hour' }, { label: 'Tomorrow 10:00', at: tomorrowAt(now, 10), typed: 'tomorrow 10:00' }]
}

const PART_OF_DAY = /\b(tonight|evening|morning|afternoon|noon|midnight|night)\b/i

/** A typed time ("tomorrow 10am", "in 2 h", "fri 18:00", "3 dec 15:00") in IST, the next such time when it is ambiguous.
 *  A day without a time means 09:00, as in Slack and Apple Reminders. null: not a time. */
export function parseWhen(text: string, now: number): number | null {
  const typed = text.trim()
  if (!typed) return null
  const found = parseDate(typed, { instant: new Date(now * 1000), timezone: OFFSET / 60 }, { forwardDate: true })[0]
  // Text around the time ("call mom tomorrow") belongs in the reminder text, not here.
  if (!found || found.text.length < typed.replace(/[\s.,]+$/, '').length - 1) return null
  let at = Math.floor(found.date().getTime() / 1000)
  if (!found.start.isCertain('hour') && !PART_OF_DAY.test(typed) && !/\bin\s+\d/i.test(typed)) {
    const p = parts(at)
    at = istTime(p.year, p.month, p.date, 9)
  }
  return at
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
