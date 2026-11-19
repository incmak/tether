import type { Pane } from './api'
import { paneTitle, type Session } from './Sidebar'

/** Speech has no hyphens: "weekly reporting" must match "weekly-reporting". */
export const spokenForm = (text: string) => text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()
export const spokenName = (wanted: string) => spokenForm(wanted).replace(/^the /, '').replace(/ (pane|chat|tab|session)$/, '')
export const inSession = (agents: Pane[], name: string) => agents.filter(pane => spokenForm(pane.tmux_session) === name)

/** A chat by its spoken name: exact title first, then a part of it, then the agent name, then a tmux session with one chat. */
export const findPane = (agents: Pane[], wanted: string) => {
  const name = spokenName(wanted)
  if (!name) return undefined
  const session = inSession(agents, name)
  return agents.find(pane => spokenForm(paneTitle(pane)) === name) ?? agents.find(pane => spokenForm(paneTitle(pane)).includes(name) || pane.agent === name)
    ?? (session.length === 1 ? session[0] : undefined)
}

/** A name that means a tmux session with several chats: Friday must ask which one, and must not guess. */
export const sessionChoice = (agents: Pane[], wanted: string) => {
  const name = spokenName(wanted)
  const session = name && !findPane(agents, wanted) ? inSession(agents, name) : []
  return session.length ? { error: `${session[0].tmux_session} is a tmux session with ${session.length} chats: ${session.map(paneTitle).join(', ')}. Ask which one.` } : null
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
