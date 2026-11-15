// The sidebar's Catch up view and the Sessions tree's counts: which tabs wait on you, grouped by goal, and which are quiet.
// A goal is a stack, or the session when the tab is in no stack. Pure rules, so test_catch_up.cjs can load them.
import type { GroupInfo, Pane } from './api'
import { isDone, needsYou } from './paneState'
import { groupOf } from './groups'

/** The fields of a sidebar session that these rules read. */
export interface CatchSession { id: string; name: string; panes: Pane[]; groups?: GroupInfo[] }
export interface Goal { key: string; name: string; session: string; stack: boolean; panes: Pane[]; since: number }

/**
 * The tab waits on you now. With Attention mode on, a pane's ask decides (a snoozed ask waits later);
 * a pane with no ask still waits while its agent asks for approval or answers. Shells never wait.
 */
export function waitsOnYou(pane: Pane, now: number, attention: boolean): boolean {
  if (pane.agent === 'shell') return false
  if (attention && pane.ask) return !(pane.ask.snoozed_until && pane.ask.snoozed_until > now)
  return needsYou(pane.state)
}

/** When the tab started to wait: its ask, else its state change. */
export const waitedSince = (pane: Pane) => pane.ask?.since ?? pane.since ?? 0

/** The tabs that wait on you, grouped by goal. Goals and tabs go oldest first. */
export function catchUpGoals(sessions: CatchSession[], now: number, attention: boolean): Goal[] {
  const byKey = new Map<string, Goal>()
  for (const session of sessions) for (const pane of session.panes) {
    if (!waitsOnYou(pane, now, attention)) continue
    const stack = groupOf(pane, session.groups)
    const key = `${session.id}/${stack}`
    const goal = byKey.get(key) ?? { key, session: session.name, stack: !!stack, name: stack ? session.groups!.find(group => group.id === stack)!.name : session.name, panes: [], since: Infinity }
    goal.panes.push(pane)
    goal.since = Math.min(goal.since, waitedSince(pane))
    byKey.set(key, goal)
  }
  const goals = [...byKey.values()]
  for (const goal of goals) goal.panes.sort((a, b) => waitedSince(a) - waitedSince(b))
  return goals.sort((a, b) => a.since - b.since || a.name.localeCompare(b.name))
}

/** Agent turns that ended after you last looked and do not wait on you. Newest first. */
export function finishedPanes(sessions: CatchSession[], seen: Record<string, number>, selected: string | undefined, now: number, attention: boolean): Pane[] {
  return sessions.flatMap(session => session.panes)
    .filter(pane => pane.agent !== 'shell' && isDone(pane, seen, selected) && !waitsOnYou(pane, now, attention))
    .sort((a, b) => (b.finished ?? 0) - (a.finished ?? 0))
}

/**
 * A quiet tab has nothing for you: it does not wait on you, work, run background tasks or hold a turn you have
 * not read, and it is not the open tab. "Active only" folds quiet tabs and sessions away.
 */
export function isQuiet(pane: Pane, seen: Record<string, number>, selected: string | undefined, now: number, attention: boolean): boolean {
  if (pane.id === selected || waitsOnYou(pane, now, attention)) return false
  if (pane.state === 'Working' || needsYou(pane.state) || pane.tasks?.length) return false
  return !isDone(pane, seen, selected)
}

/** How many tabs wait on you: the count on an area, session or stack row. */
export const waitCount = (panes: Pane[], now: number, attention: boolean) => panes.filter(pane => waitsOnYou(pane, now, attention)).length
