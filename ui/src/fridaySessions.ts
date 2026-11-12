import type { AgentChatList } from './fridayAgent'

interface Session { id: string; name: string }
interface Chat { session: string; chat: string; agent: string; state: string; busy: boolean }

export const normalize = (name: string) => name.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '')

export function soundKey(name: string): string {
  const key = normalize(name).replace(/th/g, 't').replace(/ph/g, 'f').replace(/ck/g, 'k')
  return (key.slice(0, 1) + key.slice(1).replace(/[aeiouyhw]/g, '')).replace(/(.)\1+/g, '$1')
}

export function distance(left: string, right: string): number {
  let row = Array.from({ length: right.length + 1 }, (_, index) => index)
  for (let i = 0; i < left.length; i++) {
    const next = [i + 1]
    for (let j = 0; j < right.length; j++) {
      next.push(Math.min(next[j] + 1, row[j + 1] + 1, row[j] + Number(left[i] !== right[j])))
    }
    row = next
  }
  return row[right.length]
}

/** Match spoken session names, then return only that session's chats. No terminal access. */
export function fridayChatList(chats: Chat[], sessions: Session[], target: string, wanted?: string):
  AgentChatList | { session: string; chats: string[] } | { error: string } {
  const names = new Map(sessions.map(session => [session.id, session.name || session.id]))
  for (const chat of chats) if (!names.has(chat.session)) names.set(chat.session, chat.session)
  const entries = [...names].map(([id, name]) => ({ id, name, normalized: normalize(name) }))
  const line = (chat: Chat) => `${chat.chat}, ${chat.agent}, ${chat.state}, in ${names.get(chat.session)}`
  if (wanted !== undefined) {
    const query = normalize(wanted)
    const exact = entries.find(entry => entry.name.toLowerCase() === wanted.toLowerCase())
    const ranked = query ? entries.filter(entry => entry.normalized).map(entry => ({
      ...entry,
      rank: entry.normalized.startsWith(query) || query.startsWith(entry.normalized) ? 0
        : entry.normalized.includes(query) || query.includes(entry.normalized) ? 1 : 2,
      distance: distance(query, entry.normalized),
    })).sort((a, b) => a.rank - b.rank || a.distance - b.distance) : []
    const closest = ranked[0]
    let match = exact ?? (closest && (closest.rank < 2 || closest.distance <= Math.max(query.length, closest.normalized.length) * 0.4) ? closest : undefined)
    const key = soundKey(wanted)
    if (!match && key) {
      const phonetic = entries.filter(entry => soundKey(entry.name) === key)
      if (phonetic.length === 1) match = phonetic[0]
    }
    if (!match) return { error: `No session called ${wanted}. Sessions: ${entries.map(entry => entry.name).join(', ')}` }
    return { session: match.name, chats: chats.filter(chat => chat.session === match.id).map(line) }
  }
  return {
    target,
    chats: chats.map(line),
    sessions: entries.map(entry => {
      const members = chats.filter(chat => chat.session === entry.id)
      return `${entry.name}: ${members.length} chats, ${members.filter(chat => chat.busy).length} busy`
    }),
  }
}
