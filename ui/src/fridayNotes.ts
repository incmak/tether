// Friday's tools for notes saved between calls.
import { useCallback, useEffect, useRef, useState } from 'react'
import { errorText, type Api } from './api'

const PATH = 'friday-notes'
export const NOTES_PATH = PATH

interface Topic {
  title: string
  chat: string
  notes: { text: string }[]
}

function numbered(text: string, index: number) {
  const characters = Array.from(`${index + 1}. ${text}`)
  return characters.length > 300 ? characters.slice(0, 299).join('').trimEnd() + '…' : characters.join('')
}

export async function holdNotes(api: Api, { topic, notes, chat }: Record<string, unknown>) {
  try {
    const saved = await api<Topic>(`${PATH}/hold`, { topic, notes, chat })
    return { ok: true, topic: saved.title, added: Array.isArray(notes) ? notes.length : 0, total: saved.notes.length }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export async function readNotes(api: Api, { topic }: Record<string, unknown>) {
  try {
    const wanted = typeof topic === 'string' ? topic.trim() : ''
    const path = wanted ? `${PATH}?${new URLSearchParams({ topic: wanted })}` : PATH
    const { topics } = await api<{ topics: Topic[] }>(path)
    if (wanted) {
      const saved = topics[0]
      if (!saved) return { error: `No topic matches ${wanted}.` }
      const count = saved.notes.length
      const notes = saved.notes.slice(0, 20).map(({ text }, index) => numbered(text, index))
      return { topic: saved.title, chat: saved.chat, count, notes, ...(count > notes.length && { more: count - notes.length }) }
    }
    return { count: topics.length, topics: topics.slice(0, 10).map(({ title, notes, chat }) =>
      `${title} (${notes.length} ${notes.length === 1 ? 'note' : 'notes'}${chat ? `, for ${chat}` : ''})`) }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export async function dropNotes(api: Api, { topic, note }: Record<string, unknown>) {
  try {
    const result = await api<{ dropped: 'topic' | 'note'; topic: string; left: number }>(`${PATH}/drop`, { topic, note })
    return { ok: true, ...result }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export interface HeldTopic {
  id: string
  title: string
  chat: string
  notes: { text: string; at: number }[]
  state: 'open' | 'sent'
}

/** Open topics only; the chip refreshes after Friday's note tools and when the page shows again. */
export function useHeldNotes(api: Api) {
  const [topics, setTopics] = useState<HeldTopic[]>([])
  const live = useRef(true)
  // The chip keeps the last list when a read fails; the sheet shows errors from its own actions.
  const refresh = useCallback(() => api<{ topics: HeldTopic[] }>(PATH)
    .then(({ topics: all }) => { if (live.current) setTopics(all.filter(topic => topic.state === 'open' && topic.notes.length)) })
    .catch(() => undefined), [api])
  useEffect(() => {
    live.current = true
    api<{ topics: HeldTopic[] }>(PATH)
      .then(({ topics: all }) => { if (live.current) setTopics(all.filter(topic => topic.state === 'open' && topic.notes.length)) })
      .catch(() => undefined)
    const show = () => { if (document.visibilityState === 'visible') void refresh() }
    document.addEventListener('visibilitychange', show)
    return () => { live.current = false; document.removeEventListener('visibilitychange', show) }
  }, [api, refresh])
  return { topics, refresh }
}

export function chipLabel(topics: HeldTopic[]) {
  const count = topics.reduce((sum, topic) => sum + topic.notes.length, 0)
  return topics.length === 1 ? { lead: `Held for ${topics[0].title}`, count: String(count) }
    : { lead: `Held: ${topics.length} topics`, count: String(count) }
}
