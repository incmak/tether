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

export async function dropNotes(api: Api, { id, note, text, count, texts }: Record<string, unknown>) {
  if (typeof id !== 'string') return { error: 'Not deleted. Friday must read it back and hear yes first.' }
  try {
    const result = await api<{ dropped: 'topic' | 'note'; topic: string; left: number }>(
      `${PATH}/drop`, note === undefined ? { id, count, texts } : { id, note, text })
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
  draft: {
    state: 'drafting' | 'ready' | 'failed' | 'closed'
    error: string | null
    chat: { pane: string; thread: string; name: string; gone?: true } | null
    asked_close: boolean
    revisions: { rev: number; text: string; skill: string | null; summary: string; at: number }[]
  } | null
}

async function findTopic(api: Api, topic: unknown, scope: 'open' | 'draft') {
  if (typeof topic !== 'string' || !topic.trim()) throw new Error('Provide a topic name.')
  const { topics } = await api<{ topics: HeldTopic[] }>(`${PATH}?${new URLSearchParams({ topic, scope })}`)
  return topics[0]
}

type DraftStarted = { state: 'drafting'; chat: string }

export async function makePrompt(api: Api, { topic }: Record<string, unknown>, watch: (id: string) => void) {
  try {
    const saved = await findTopic(api, topic, 'open')
    const result = await api<DraftStarted>(`${PATH}/draft`, { id: saved.id })
    watch(saved.id)
    return { ok: true, topic: saved.title, ...result }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export async function editPrompt(api: Api, { topic, change }: Record<string, unknown>, watch: (id: string) => void) {
  try {
    if (typeof change !== 'string' || !change.trim() || Array.from(change).length > 1000) {
      return { error: 'Give a change of 1 to 1000 characters.' }
    }
    const saved = await findTopic(api, topic, 'open')
    const revisions = saved.draft?.revisions ?? []
    const latest = revisions[revisions.length - 1]
    if (saved.draft?.state !== 'ready' || !latest) {
      return { error: `There is no finished prompt for ${saved.title} yet.` }
    }
    const result = await api<DraftStarted>(`${PATH}/draft-edit`, { id: saved.id, rev: latest.rev, edit: change })
    watch(saved.id)
    return { ok: true, topic: saved.title, ...result }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export async function sendPrompt(api: Api, { id, rev, pane, thread }: Record<string, unknown>) {
  if (typeof id !== 'string' || !id || typeof rev !== 'number' || !Number.isInteger(rev) || rev < 1 ||
      typeof pane !== 'string' || !pane || typeof thread !== 'string' || !thread) {
    return { error: 'Not sent. Friday must read it back and hear yes first.' }
  }
  try {
    const result = await api<{ chat: string; rev: number; length: number; ask_close: boolean }>(
      `${PATH}/draft-send`, { id, rev, pane, thread })
    const asked = result.ask_close && await api<{ ask_close: boolean }>(`${PATH}/draft-asked`, { id }).catch(() => null)
    return { ok: true, chat: result.chat, rev: result.rev, length: result.length, ask_close: !!asked && asked.ask_close }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export async function closeDraft(api: Api, { topic }: Record<string, unknown>) {
  try {
    const saved = await findTopic(api, topic, 'draft')
    const chat = saved.draft?.chat
    if (!chat) return { error: `There is no draft chat for ${saved.title}.` }
    await api(`${PATH}/draft-close`, { id: saved.id, pane: chat.pane, thread: chat.thread })
    return { ok: true }
  } catch (error) {
    return { error: errorText(error) }
  }
}

export function createDraftWatch(api: Api, announce: (text: string, show?: { id: string; rev: number }) => void) {
  const watches = new Map<string, () => void>()
  return {
    start(id: string) {
      if (watches.has(id)) return
      const abort = new AbortController()
      const stop = () => {
        abort.abort()
        clearTimeout(timer)
        clearTimeout(deadline)
        watches.delete(id)
      }
      const poll = async () => {
        try {
          const { topics } = await api<{ topics: HeldTopic[] }>(PATH, undefined, abort.signal, undefined, undefined, 'low')
          if (abort.signal.aborted) return
          const topic = topics.find(topic => topic.id === id)
          const draft = topic?.draft
          if (draft?.state === 'drafting') {
            timer = setTimeout(poll, 3000)
            return
          }
          stop()
          if (!topic || !draft) return
          const latest = draft.revisions[draft.revisions.length - 1]
          if (draft.state === 'ready' && latest) {
            const summary = latest.summary ? ` ${latest.summary}` : ''
            announce(`The prompt for ${topic.title} is ready, version ${latest.rev}.${summary} Say send it to a chat, read all of it, or a change.`,
              { id, rev: latest.rev })
          } else if (draft.state === 'failed') {
            announce(`The prompt for ${topic.title} failed.${draft.error ? ` ${draft.error}` : ''}`)
          }
        } catch {
          if (!abort.signal.aborted) stop()
        }
      }
      let timer = setTimeout(poll, 3000)
      const deadline = setTimeout(stop, 7 * 60 * 1000)
      watches.set(id, stop)
    },
    clear() {
      for (const stop of watches.values()) stop()
    },
  }
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
