// Which agent-added backlog items the person has not seen yet. The sidebar and Home show them; opening the backlog
// marks them seen. "Seen" is the highest item ID shown in the open sheet, kept per browser.
import { useCallback, useEffect, useState } from 'react'
import type { Api } from './api'
import { parseBacklog } from './backlogItems'
import type { Item } from './backlogItems'
import { usePolling } from './hooks'

export const BACKLOG_PATH = 'projects/backlog'
const KEY = 'tether.backlog.seen'
const EVENT = 'tether-backlog-seen'
const POLL_MS = 30000

const number = (item: Item) => Number(item.ref?.slice(1) ?? 0)
export const highestRef = (items: Item[]) => items.reduce((most, item) => Math.max(most, number(item)), 0)

function readSeen(): number | null {
  try { const value = localStorage.getItem(KEY); return value === null ? null : Number(value) || 0 } catch { return 0 }
}

/** The open sheet showed every item up to this ID. */
export function markSeen(highest: number) {
  const seen = readSeen()
  if (seen !== null && seen >= highest) return
  try { localStorage.setItem(KEY, String(highest)) } catch { /* private mode: nothing is marked new */ }
  window.dispatchEvent(new Event(EVENT))
}

/** Open items, and the open items that agents added after the person last looked. On the first visit nothing is new. */
export function useBacklogNews(api: Api) {
  const fetcher = useCallback((signal: AbortSignal) => api<{ text: string }>(BACKLOG_PATH, undefined, signal, undefined, undefined, 'low'), [api])  // a badge: background
  const { data } = usePolling(fetcher, POLL_MS)
  const [seen, setSeen] = useState(readSeen)
  useEffect(() => {
    const changed = () => setSeen(readSeen())
    window.addEventListener(EVENT, changed)
    window.addEventListener('storage', changed)
    return () => { window.removeEventListener(EVENT, changed); window.removeEventListener('storage', changed) }
  }, [])
  const items = data ? parseBacklog(data.text).filter((row): row is Item => row.kind === 'item' && !row.done) : []
  useEffect(() => { if (data && seen === null) markSeen(highestRef(items)) })
  const fresh = seen === null ? [] : items.filter(item => item.source && number(item) > seen)
  return { open: items.length, fresh }
}
