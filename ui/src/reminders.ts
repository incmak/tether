// Reminders (server/reminders.py): one-off reminders that Tether delivers at their time on the urgent channels
// (push, urgent ping, Waiting on you). Times are UTC epoch seconds on the wire and show in India Standard Time.
// IST has no daylight saving time, so a fixed +05:30 offset converts both ways.
import { useCallback, useEffect } from 'react'
import { parse as parseDate } from 'chrono-node'
import type { Api } from './api'
import { usePolling } from './hooks'

export type ReminderState = 'scheduled' | 'due' | 'done'
export interface ReminderLink { pane?: string; thread?: string; project?: string; item?: string }
export interface Reminder {
  id: string; text: string; due: number; created: number; by: string; state: ReminderState
  fired_at: number | null; late: boolean; snoozes: number; done_at: number | null; link: ReminderLink | null
  /** The schedule's number: it changes on each snooze or new time, so an old notice cannot act on a new schedule. */
  occ?: number
  /** Channels that gave up after their retries. */
  failed?: string[]
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
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

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
