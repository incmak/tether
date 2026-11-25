import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { ArrowUp, History } from 'lucide-react'
import type { ChatMessage } from './api'
import { fmtDateTime, zoneParts } from './zone'

// The bar covers the top of the chat: a message that starts under it counts as out of view.
export const MINE_BAR_SPACE = 52
const HIDE_MS = 2500

const oneLine = (text: string) => text.replace(/\s+/g, ' ').trim()

/**
 * A bar at the top of the chat that names your own message nearest above what you see now.
 * A tap goes to that message; the bar then names the one before it, and so on.
 * At the oldest loaded message it offers to load earlier ones.
 */
export function MineBar({ view, messages, earlier, onReveal, onEarlier }: {
  view: RefObject<HTMLDivElement | null>
  messages: ChatMessage[]
  earlier: number
  onReveal: (id: string) => void
  onEarlier: () => void
}) {
  const mine = useMemo(() => new Map(messages.filter(item => item.role === 'user' && !item.kind && item.source !== 'tether').map(item => [item.id, item])), [messages])
  const [above, setAbove] = useState<string | null>(null)
  // The bar comes only while you scroll back. It goes when you scroll down, reach the end, or stop for HIDE_MS.
  const [shown, setShown] = useState(false)
  const held = useRef(false)  // pointer over it or focus in it: keep it
  const hideTimer = useRef(0)
  useEffect(() => {
    const element = view.current
    if (!element) return
    let frame = 0
    let last = element.scrollTop
    const measure = () => {
      frame = 0
      const line = element.getBoundingClientRect().top + MINE_BAR_SPACE - 8
      let found: string | null = null
      for (const node of element.querySelectorAll<HTMLElement>('[data-msg-id]')) {
        const id = node.dataset.msgId
        const message = id ? mine.get(id) : undefined
        if (!message || !(node.classList.contains('user-message') || (message.legacy && node.classList.contains('review-message')))) continue
        if (node.getBoundingClientRect().top < line) found = message.id
        else break
      }
      setAbove(found)
    }
    const hideLater = () => {
      window.clearTimeout(hideTimer.current)
      hideTimer.current = window.setTimeout(() => { if (!held.current) setShown(false) }, HIDE_MS)
    }
    const onScroll = () => {
      const top = element.scrollTop
      const atEnd = element.scrollHeight - top - element.clientHeight < 48
      // Compare with the last point where the direction was clear: smooth scroll moves in steps under 4px.
      if (atEnd || top > last + 4) { if (!held.current) setShown(false); last = top }
      else if (top < last - 4) { setShown(true); hideLater(); last = top }
      if (!frame) frame = requestAnimationFrame(measure)
    }
    frame = requestAnimationFrame(measure)
    element.addEventListener('scroll', onScroll, { passive: true })
    return () => { element.removeEventListener('scroll', onScroll); if (frame) cancelAnimationFrame(frame); window.clearTimeout(hideTimer.current) }
  }, [view, mine])
  const hold = (on: boolean) => {
    held.current = on
    if (on) window.clearTimeout(hideTimer.current)
    else hideTimer.current = window.setTimeout(() => setShown(false), HIDE_MS)
  }
  const message = above ? mine.get(above) : undefined
  if (!message && (!mine.size || !earlier)) return null
  const text = message ? oneLine(message.text) || (message.images ? 'Image' : 'Message') : ''
  return <div className={`mine-bar ${shown ? 'shown' : ''}`} inert={!shown} onPointerEnter={() => hold(true)} onPointerLeave={() => hold(false)} onFocus={() => hold(true)} onBlur={() => hold(false)}>
    {message
      ? <button type="button" title={text} aria-label={`Go to your message: ${text}`} onClick={() => onReveal(message.id)}>
        <ArrowUp size={14} aria-hidden="true" /><span className="mine-bar-who">You</span><span className="mine-bar-text">{text}</span>
      </button>
      : <button type="button" onClick={onEarlier}><History size={14} aria-hidden="true" /><span className="mine-bar-text">Load earlier messages · {earlier}</span></button>}
  </div>
}

/** "8 Nov, 1:59 PM"; the year shows only when it is not this year. */
export function startedLabel(iso: string | null | undefined) {
  if (!iso) return null
  const date = Date.parse(iso)
  if (Number.isNaN(date)) return null
  const year = zoneParts(date).year !== zoneParts(Date.now()).year
  return fmtDateTime(date, { day: 'numeric', month: 'short', ...year ? { year: 'numeric' } : {}, hour: 'numeric', minute: '2-digit' })
}
