import type { Api, Pane, SessionsData } from './api'
import { paneTitle, sessionName, type Session } from './Sidebar'

/** Speech has no hyphens: "weekly reporting" must match "weekly-reporting". */
export const spokenForm = (text: string) => text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()
export const spokenName = (wanted: string) => spokenForm(wanted).replace(/^the /, '').replace(/ (pane|chat|tab|session)$/, '')
export const inSession = (agents: Pane[], name: string) => agents.filter(pane => {
  const title = sessionName(pane)
  return spokenForm(title) === name || spokenName(title) === name
})

export function sessionPanes(agents: Pane[], wanted: string) {
  const name = spokenName(wanted)
  if (!name) return []
  const full = spokenForm(wanted)
  const exact = agents.filter(pane => spokenForm(sessionName(pane)) === full)
  if (exact.length) return exact
  const matches = inSession(agents, name)
  const literal = matches.filter(pane => spokenForm(sessionName(pane)) === name)
  return literal.length ? literal : matches
}

/** Reuse Friday's home by name. After creation, wait for the session ID the API returned. */
export async function fridayHomePane(api: Api, home: string, cwd: string, wait: (ms: number) => Promise<void>) {
  const list = (await api<SessionsData>('sessions')).sessions
  const found = list.find(pane => sessionName(pane) === home)
  if (found) {
    const { pane } = await api<{ pane: string }>('split', { session: found.id, direction: 'right', cwd })
    return { pane, created: false }
  }
  const made = await api<{ session: string; name: string }>('tmux-sessions', { name: home, cwd })
  for (let tries = 0; tries < 20; tries++) {
    const first = (await api<SessionsData>('sessions')).sessions.find(pane => pane.tmux_session === made.session)
    if (first) return { pane: first.id, created: true }
    await wait(300)
  }
  throw new Error(`The session ${made.name} started, but its pane is not visible yet.`)
}

/** A chat by its spoken name: exact title first, then a part of it, then the agent name, then a tmux session with one chat. */
export const findPane = (agents: Pane[], wanted: string) => {
  const name = spokenName(wanted)
  if (!name) return undefined
  const session = sessionPanes(agents, wanted)
  return agents.find(pane => spokenForm(paneTitle(pane)) === name) ?? agents.find(pane => spokenForm(paneTitle(pane)).includes(name) || pane.agent === name)
    ?? (session.length === 1 ? session[0] : undefined)
}

/** A name that means a tmux session with several chats: Friday must ask which one, and must not guess. */
export const sessionChoice = (agents: Pane[], wanted: string) => {
  const session = !findPane(agents, wanted) ? sessionPanes(agents, wanted) : []
  return session.length ? { error: `${sessionName(session[0])} is a tmux session with ${session.length} chats: ${session.map(paneTitle).join(', ')}. Ask which one.` } : null
}

/** Match the sidebar name that list_chats reports. Require a unique partial match. */
export function findSession(sessions: Session[], wanted: string): Session | { error: string } | undefined {
  const name = spokenForm(wanted)
  if (!name) return undefined
  const exact = sessions.find(session => spokenForm(session.name) === name)
  if (exact) return exact
  const matches = sessions.filter(session => {
    const candidate = spokenForm(session.name)
    return candidate && (candidate.includes(name) || name.includes(candidate))
  })
  if (matches.length > 1) return { error: `Several sessions match: ${matches.map(session => session.name).join(', ')}. Ask which one.` }
  return matches[0]
}
