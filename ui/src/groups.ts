import type { GroupInfo, Pane } from './api'
import { isDone, needsYou } from './paneState'

// Groups: named sets of tabs inside one tmux session (server/groups.py). A tab's `group` is a group id;
// a tab with no id, or an id the session does not list, is in Unstacked. Sessions with no groups do not change.

/** A place to switch between: Unstacked (id '') or a group. */
export interface Place { id: string; name: string; panes: Pane[] }
export type GroupState = 'needs' | 'done' | 'working' | 'idle'

/** The group id of a tab in its session: '' for Unstacked. */
export function groupOf(pane: Pane, groups: GroupInfo[] | undefined): string {
  return pane.group && groups?.some(group => group.id === pane.group) ? pane.group : ''
}

/** Unstacked (only while it holds a tab) and every group, in order. Empty for a session with no groups. */
export function placesOf(panes: Pane[], groups: GroupInfo[] | undefined): Place[] {
  if (!groups?.length) return []
  const roots = new Map(panes.filter(pane => !pane.task).map(pane => [pane.id, pane]))
  const place = (pane: Pane) => groupOf(roots.get(pane.task ?? '') ?? pane, groups)
  const main = panes.filter(pane => !place(pane))
  return [...(main.length ? [{ id: '', name: 'Unstacked', panes: main }] : []),
    ...groups.map(group => ({ id: group.id, name: group.name, panes: panes.filter(pane => place(pane) === group.id) }))]
}

export function placeName(id: string, groups: GroupInfo[] | undefined) {
  return id ? groups?.find(group => group.id === id)?.name ?? 'Unstacked' : 'Unstacked'
}

/** The tabs a chat's strip and swipe use: the open tab's group, or the whole session when it has no groups. */
export function groupPanes(panes: Pane[], groups: GroupInfo[] | undefined, pane: Pane): Pane[] {
  if (!groups?.length) return panes
  const places = placesOf(panes, groups)
  return (places.find(place => place.panes.some(item => item.id === pane.id))
    ?? places.find(place => place.id === groupOf(pane, groups)))?.panes ?? []
}

/**
 * The scroll place of a sideways strip that shows one item fully, with `gap` px to spare. It moves the strip as little
 * as it can: an item already in view keeps the strip still. An item wider than the strip shows from its start.
 */
export function revealLeft(scroll: number, view: number, total: number, start: number, width: number, gap = 12): number {
  const most = Math.max(0, total - view)
  let next = scroll
  if (start + width + gap > next + view) next = start + width + gap - view
  if (start - gap < next) next = start - gap
  return Math.max(0, Math.min(most, next))
}

/** The scroll key of a strip that shows one stack of a session. */
export function stripKey(session: string, group: string) { return `${session}/${group}` }

/** The most urgent state of a set of tabs: needs you, then done, then working. */
export function topState(panes: Pane[], seen: Record<string, number>, selected?: string): GroupState {
  if (panes.some(pane => needsYou(pane.state))) return 'needs'
  if (panes.some(pane => isDone(pane, seen, selected))) return 'done'
  if (panes.some(pane => pane.state === 'Working')) return 'working'
  return 'idle'
}

/** Tabs that wait on you in the other places: the phone switcher's badge. */
export function waitingElsewhere(places: Place[], current: string) {
  return places.filter(place => place.id !== current).reduce((sum, place) => sum + place.panes.filter(pane => needsYou(pane.state)).length, 0)
}

/** The next place in a direction, with wrap. Undefined when there is nowhere else to go. */
export function stepPlace(places: Place[], current: string, dir: 1 | -1): Place | undefined {
  if (places.length < 2) return undefined
  const at = Math.max(0, places.findIndex(place => place.id === current))
  return places[(at + dir + places.length) % places.length]
}

/** The checks the server makes (server/groups.py): trimmed, 1 to 40 characters, no control characters, not "Unstacked" or "Main". */
export function groupNameError(name: string, groups: GroupInfo[] | undefined, except?: string): string {
  const value = name.trim()
  if (!value) return 'Type a name.'
  if (value.length > 40) return 'Use 40 characters or fewer.'
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f-\u009f]/.test(value)) return 'Use letters, numbers and spaces.'
  if (['unstacked', 'main'].includes(value.toLowerCase())) return 'Unstacked and Main are kept for the tabs in no stack.'
  if (groups?.some(group => group.id !== except && group.name.toLowerCase() === value.toLowerCase())) return 'A stack with that name exists.'
  return ''
}

// Each group remembers its last tab on this device (not synced: each device keeps its own place).
const LAST_KEY = 'tether.groupLastTab'
const lastKey = (session: string, group: string) => `${session}/${group || 'main'}`
function readLast(): Record<string, string> {
  try { const value = JSON.parse(localStorage.getItem(LAST_KEY) ?? '{}'); return value && typeof value === 'object' ? value : {} } catch { return {} }
}
export function rememberTab(session: string, group: string, pane: string) {
  try {
    const last = readLast()
    if (last[lastKey(session, group)] === pane) return
    last[lastKey(session, group)] = pane
    const keys = Object.keys(last)
    for (const key of keys.slice(0, Math.max(0, keys.length - 200))) delete last[key]
    localStorage.setItem(LAST_KEY, JSON.stringify(last))
  } catch { /* private mode: the first tab opens instead */ }
}
/** The tab to open in a place: the one last open here on this device, else the first. */
export function tabFor(session: string, place: Place): Pane | undefined {
  const id = readLast()[lastKey(session, place.id)]
  return place.panes.find(pane => pane.id === id) ?? place.panes[0]
}
