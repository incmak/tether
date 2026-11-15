// The search menu's "Start a new tab" rows: where a tab can go, and the server calls that pick a place and start it.
// The server (quick_tab.py) checks every place again against live tmux before it starts anything.
import type { Api } from './api'
import type { Session } from './Sidebar'

/** A session and one of its stacks. group "" is the session's tabs that are in no stack. */
export interface Place { session: string; group: string; sessionName: string; groupName: string }
/** auto: Tether picks the place for the query. default: the triage stack of the General session. */
export type Choice = { kind: 'auto' } | { kind: 'default' } | { kind: 'place'; place: Place }
export interface Started { pane: string; name: string; agent: string; place: Place }
export interface Routed { place: Place | null; source: 'ai' | 'default'; why?: string }

export const DEFAULT_SESSION = 'General'
export const DEFAULT_STACK = 'Triage'
/** On Enter, wait this long for the routing answer, then start the tab in the default place. */
export const ROUTE_WAIT_MS = 3000

export const placeName = (place: Place) => place.groupName ? `${place.sessionName} · ${place.groupName}` : place.sessionName

/** Every session, then each of its stacks. Values: "$3|" for a session's unstacked tabs, "$3|g1a2b3c4d" for a stack. */
export function places(sessions: Session[]): Place[] {
  return sessions.flatMap(session => [
    { session: session.id, group: '', sessionName: session.name, groupName: '' },
    ...(session.groups ?? []).map(group => ({ session: session.id, group: group.id, sessionName: session.name, groupName: group.name })),
  ])
}
export const placeValue = (place: Place) => `${place.session}|${place.group}`

/** request: one id per Enter, so a retried request starts no second tab. */
export function choiceBody(message: string, choice: Choice, request: string) {
  if (choice.kind === 'place') return { request, message, session: choice.place.session, group: choice.place.group }
  return { request, message, default: true }
}

export const requestId = () => typeof crypto !== 'undefined' && 'randomUUID' in crypto
  ? crypto.randomUUID()
  : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`

export const route = (api: Api, query: string, agent: string, signal?: AbortSignal) =>
  api<Routed>('quick-tab/route', { query, agent }, signal, undefined, 15000)

export const startTab = (api: Api, message: string, choice: Choice, request: string) =>
  api<Started>('quick-tab/start', choiceBody(message, choice, request), undefined, undefined, 20000)

/** Put a tab that startTab made into another session or stack. Its agent keeps running in its folder. */
export const moveTab = (api: Api, pane: string, place: Place) =>
  api<{ pane: string; place: Place }>('quick-tab/move', { pane, session: place.session, group: place.group }, undefined, undefined, 15000)

/** Same place: same session and same stack. */
export const samePlace = (a: Place, b: Place) => a.session === b.session && a.group === b.group
