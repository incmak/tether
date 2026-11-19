// Todos from Mak's task apps (server/task_sources.py): Microsoft To Do and Planner, read only. Tether mirrors open
// tasks, can turn a task's own reminder into a Tether reminder (opt-in per source), and copies a task to the backlog
// on request. Times are UTC epoch seconds on the wire; dates without a time are 'YYYY-MM-DD'.
import { useCallback } from 'react'
import type { Api } from './api'
import { usePolling } from './hooks'

export type SourceId = 'todo' | 'planner'
export type SourceState = 'ok' | 'off' | 'error' | 'syncing'
export interface TaskSource {
  id: SourceId; label: string; enabled: boolean; alerts: boolean; state: SourceState
  /** Why the source is off or failed, in plain words. */
  why?: string
  /** The command that turns the source on (a reconnect with the tasks permission). */
  command?: string
  syncedAt: number | null; complete: boolean; count: number
}
export interface TaskDue { date: string; at: number | null }
export interface ExternalTask {
  key: string; source: SourceId; list: string; title: string
  due: TaskDue | null; reminderAt: number | null; importance?: string; link: string; modified?: number
  /** The app gave a time zone Tether could not read, so the time may be off. */
  timeUncertain?: boolean
  /** The backlog item this task was copied to. */
  backlog: string | null
  /** The Tether reminder linked to this task's own reminder time. */
  reminder: string | null
}
export interface TaskListing { sources: TaskSource[]; tasks: ExternalTask[] }

const POLL_MS = 60000
const OFFSET = 330 * 60  // IST, seconds east of UTC

/** The IST day number of an instant, and of a 'YYYY-MM-DD' date (a date names a calendar day, not an instant). */
const dayOfInstant = (at: number) => Math.floor((at + OFFSET) / 86400)
const dayOfDate = (date: string) => {
  const [y, m, d] = date.split('-').map(Number)
  return Math.floor(Date.UTC(y, m - 1, d) / 86400000)
}
export const dueDay = (due: TaskDue) => due.at !== null ? dayOfInstant(due.at) : dayOfDate(due.date)

export interface TaskGroups { overdue: ExternalTask[]; today: ExternalTask[]; upcoming: ExternalTask[]; someday: ExternalTask[] }

/** Overdue, Today, Upcoming (soonest first) and No due date (by title), like To Do's own Planned view. */
export function groupTasks(tasks: ExternalTask[], now: number): TaskGroups {
  const today = dayOfInstant(now)
  const groups: TaskGroups = { overdue: [], today: [], upcoming: [], someday: [] }
  for (const task of tasks) {
    if (!task.due) groups.someday.push(task)
    else {
      const day = dueDay(task.due)
      // A due time today that has passed is overdue; a due date of today is not, until the day ends.
      if (day < today || (day === today && task.due.at !== null && task.due.at < now)) groups.overdue.push(task)
      else if (day === today) groups.today.push(task)
      else groups.upcoming.push(task)
    }
  }
  const byDue = (a: ExternalTask, b: ExternalTask) => (dueDay(a.due!) - dueDay(b.due!)) || ((a.due!.at ?? 0) - (b.due!.at ?? 0)) || a.title.localeCompare(b.title)
  groups.overdue.sort(byDue); groups.today.sort(byDue); groups.upcoming.sort(byDue)
  groups.someday.sort((a, b) => a.title.localeCompare(b.title))
  return groups
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const pad = (value: number) => String(value).padStart(2, '0')

/** "Today", "Tomorrow", "Yesterday", "Wed 25 Nov", with the IST time when the task has one. */
export function dueLabel(due: TaskDue, now: number) {
  const days = dueDay(due) - dayOfInstant(now)
  const day = new Date(dueDay(due) * 86400000)
  const name = days === 0 ? 'Today' : days === 1 ? 'Tomorrow' : days === -1 ? 'Yesterday'
    : `${DAYS[day.getUTCDay()]} ${day.getUTCDate()} ${MONTHS[day.getUTCMonth()]}${day.getUTCFullYear() === new Date((now + OFFSET) * 1000).getUTCFullYear() ? '' : ` ${day.getUTCFullYear()}`}`
  if (due.at === null) return name
  const time = new Date((due.at + OFFSET) * 1000)
  return `${name} ${pad(time.getUTCHours())}:${pad(time.getUTCMinutes())}`
}

/** "just now", "4 min ago", "2 h ago", "3 days ago". */
export function agoLabel(at: number, now: number) {
  const s = Math.max(0, now - at)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  const d = Math.floor(s / 86400)
  return `${d} day${d === 1 ? '' : 's'} ago`
}

export const settings = (api: Api, source: SourceId, change: { enabled?: boolean; alerts?: boolean }) =>
  api<TaskListing>('tasks/settings', { source, ...change })
export const syncNow = (api: Api) => api<TaskListing>('tasks/sync', {})
export const toBacklog = (api: Api, key: string) => api<{ id: string; created: boolean }>('tasks/backlog', { key })

/** The mirror, polled every minute while visible. The server itself syncs every 5 min. */
export function useTasks(api: Api) {
  const fetcher = useCallback((signal: AbortSignal) => api<TaskListing>('tasks', undefined, signal), [api])
  return usePolling(fetcher, POLL_MS)
}
