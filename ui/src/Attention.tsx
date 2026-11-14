// Attention mode (server/attention.py): the sidebar "Waiting on you" box, the header "Open the oldest" button (a pill
// with a bottom sheet on a phone), the Handled and Remind me tomorrow chips under the last reply, and the daily summary setting.
import { useEffect, useId, useRef, useState } from 'react'
import { ArrowDownWideNarrow, ArrowUpNarrowWide, Check, Clock, GalleryVerticalEnd, Hand, ListChecks, X } from 'lucide-react'
import type { WaitOrder } from './prefs'
import type { Api, Ask, Pane } from './api'
import { errorText } from './api'
import { askAction, LATE, stillWait, useNow } from './attention'
import type { AskAction, Digest, Waiting } from './attention'
import { useModal } from './hooks'
import { age } from './needs'
import { chatTitle } from './Sidebar'
import type { Session } from './Sidebar'
import './attention.css'

const seenText = (ask: Ask) => ask.opened ? 'Opened, not answered' : 'Not opened'

/** The sort of a Waiting on you list. items stay oldest first everywhere else ("Open the oldest" uses items[0]). */
export interface WaitSort { order: WaitOrder; onOrder: (order: WaitOrder) => void }
/** The lessons review entry: one quiet row for all projects, after the tabs. */
export interface ReviewEntry { label: string; onOpen: () => void }

export const ordered = (items: Waiting[], order: WaitOrder = 'oldest') => order === 'newest' ? [...items].reverse() : items

/** The sort button: one icon that shows the current order and switches it. */
export function SortButton({ order, onOrder, className = '' }: WaitSort & { className?: string }) {
  const next: WaitOrder = order === 'oldest' ? 'newest' : 'oldest'
  const label = `Sorted ${order} first. Sort ${next} first`
  return <button type="button" className={`icon-button waiting-sort ${className}`.trim()} aria-label={label} title={label} onClick={() => onOrder(next)}>
    {order === 'oldest' ? <ArrowUpNarrowWide size={15} aria-hidden="true" /> : <ArrowDownWideNarrow size={15} aria-hidden="true" />}
  </button>
}

/** The tabs that wait on you, oldest first. limit: rows before "+N more" (0 shows all). No asks: no box.
 *  variant "list": the sidebar form, a plain section inside the session list's scroll area, one line of text per row. */
export function WaitingBox({ items, sessions, selected, limit = 3, variant = 'box', sort, review, onOpen, onDeck }: { items: Waiting[]; sessions: Session[]; selected?: string; limit?: number; variant?: 'box' | 'list'; sort?: WaitSort; review?: ReviewEntry; onOpen: (pane: Pane) => void; onDeck?: () => void }) {
  const now = useNow()
  const id = useId()
  const [all, setAll] = useState(false)
  if (!items.length && !review) return null
  const list = variant === 'list'
  const sorted = ordered(items, sort?.order)
  const shown = limit && !all ? sorted.slice(0, limit) : sorted
  return <section className={list ? 'waiting-box waiting-list' : 'waiting-box'} aria-labelledby={id}>
    <h2 className="waiting-title" id={id}><Hand size={list ? 13 : 14} aria-hidden="true" />Waiting on you<span className="waiting-count">{list ? '· ' : ''}{items.length + (review ? 1 : 0)}</span>{sort && items.length > 1 && <SortButton {...sort} />}{onDeck && items.length > 0 && (list
      ? <button type="button" className="icon-button waiting-sort" aria-label="Answer one by one" title="Answer one by one" onClick={onDeck}><GalleryVerticalEnd size={15} aria-hidden="true" /></button>
      : <button type="button" className="waiting-deck-button" onClick={onDeck}>One by one</button>)}</h2>
    {shown.map(({ pane, ask }) => {
      const session = sessions.find(item => item.id === pane.tmux_session)
      const name = chatTitle(pane, session), waited = age(ask.since, now)
      const seen = `${session && session.name !== name ? `${session.name} · ` : ''}${seenText(ask)}`
      return <button key={pane.id} type="button" className="waiting-row" aria-current={selected === pane.id ? 'page' : undefined} title={list ? seen : undefined}
        aria-label={`${ask.urgent ? 'Urgent: ' : ''}${name}, waited ${waited}, ${seenText(ask).toLowerCase()}`} onClick={() => onOpen(pane)}>
        <span className={`${ask.opened ? 'waiting-dot opened' : 'waiting-dot'}${ask.urgent ? ' urgent' : ''}`} aria-hidden="true" />
        <span className="waiting-name">{name}</span>
        <span className={now - ask.since >= LATE ? 'waiting-age late' : 'waiting-age'}>{waited}</span>
        {ask.next && <span className="waiting-next">{ask.next}</span>}
        {!list && <span className="waiting-seen">{seen}</span>}
      </button>
    })}
    {review && <button type="button" className="waiting-row waiting-review" title={list ? review.label : undefined} aria-label={`Review lessons: ${review.label}`} onClick={review.onOpen}>
      <ListChecks size={14} className="waiting-review-icon" aria-hidden="true" />
      <span className="waiting-name">Review lessons</span>
      {!list && <span className="waiting-seen">{review.label}</span>}
    </button>}
    {!!limit && items.length > limit && <button type="button" className="waiting-more" aria-expanded={all} onClick={() => setAll(!all)}>{all ? 'Show fewer' : `+${items.length - limit} more`}</button>}
  </section>
}

/** Desktop: "Open the oldest" opens the tab that waited longest. compact (phone): a pill that opens the full list. */
export function OldestButton({ items, sessions, selected, compact = false, sort, onOpen, onDeck }: { items: Waiting[]; sessions: Session[]; selected?: string; compact?: boolean; sort?: WaitSort; onOpen: (pane: Pane) => void; onDeck?: () => void }) {
  const [sheet, setSheet] = useState(false)
  if (!items.length) return null
  const count = items.length
  return <>
    <button type="button" className={compact ? 'oldest-button compact' : 'oldest-button'} aria-haspopup={compact ? 'dialog' : undefined}
      aria-label={compact ? `Waiting on you: ${count} ${count === 1 ? 'tab' : 'tabs'}. Show the list` : `Open the oldest waiting tab, ${count} waiting`}
      title={compact ? 'Waiting on you' : 'Open the tab that waited longest'} onClick={() => compact ? setSheet(true) : onOpen(items[0].pane)}>
      <Hand size={16} aria-hidden="true" />{!compact && <span className="oldest-label">Open the oldest</span>}<span className="oldest-count" aria-hidden="true">{count > 99 ? '99+' : count}</span>
    </button>
    {sheet && <WaitingSheet items={items} sessions={sessions} selected={selected} sort={sort} onOpen={onOpen} onDeck={onDeck && (() => { setSheet(false); onDeck() })} onClose={() => setSheet(false)} />}
  </>
}

function WaitingSheet({ items, sessions, selected, sort, onOpen, onDeck, onClose }: { items: Waiting[]; sessions: Session[]; selected?: string; sort?: WaitSort; onOpen: (pane: Pane) => void; onDeck?: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const { ref } = modal
  useEffect(() => { ref.current?.querySelector<HTMLButtonElement>('.waiting-row')?.focus() }, [ref])
  return <dialog {...modal} className="sheet waiting-sheet" aria-label="Waiting on you">
    <span className="waiting-grab" aria-hidden="true" />
    <WaitingBox items={items} sessions={sessions} selected={selected} limit={0} sort={sort} onDeck={onDeck} onOpen={pane => { onClose(); onOpen(pane) }} />
    <button type="button" className="secondary waiting-close" onClick={onClose}><X size={16} aria-hidden="true" />Close</button>
  </dialog>
}

/** Under the last reply of a tab that waits on you. Showing them marks the ask opened. onDone gets the confirmation. */
export function AskChips({ api, pane, ask, onDone, onUpdate }: { api: Api; pane: Pane; ask: Ask; onDone: (text: string) => void; onUpdate: () => void }) {
  const now = useNow()
  const [busy, setBusy] = useState(false)
  const marked = useRef('')
  useEffect(() => {
    const key = `${pane.id} ${pane.thread} ${ask.since}`
    if (ask.opened || marked.current === key) return
    marked.current = key
    askAction(api, pane, 'opened').catch(() => { marked.current = '' })
  }, [api, pane, ask.opened, ask.since])
  const act = async (action: AskAction) => {
    setBusy(true)
    try {
      const result = await askAction(api, pane, action)
      onDone(`${action === 'handled' ? 'Handled.' : 'Snoozed until tomorrow.'} ${stillWait(result.waiting)}`)
      onUpdate()
    } catch (error) {
      onDone(errorText(error))
    } finally {
      setBusy(false)
    }
  }
  return <div className="ask-chips" role="group" aria-label="This tab waits on you">
    <button type="button" className="ask-chip primary" disabled={busy} onClick={() => void act('handled')}><Check size={15} aria-hidden="true" />Handled, no reply needed</button>
    <button type="button" className="ask-chip" disabled={busy} onClick={() => void act('snooze')}><Clock size={15} aria-hidden="true" />Remind me tomorrow</button>
    <span className="ask-state">Waiting {age(ask.since, now)} · {seenText(ask)}</span>
  </div>
}

const HOURS = Array.from({ length: 24 }, (_, hour) => hour)

/** Settings: the daily summary push ("5 tabs wait on you, oldest 3 days."), off by default. It is a server setting. */
export function DigestSettings({ api }: { api: Api }) {
  const [digest, setDigest] = useState<Digest | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api<{ digest: Digest }>('attention', undefined, controller.signal).then(data => setDigest(data.digest)).catch(reason => { if (!controller.signal.aborted) setError(errorText(reason)) })
    return () => controller.abort()
  }, [api])
  const save = async (next: Digest) => {
    const before = digest
    setDigest(next); setError('')
    try { setDigest((await api<{ digest: Digest }>('attention/digest', next)).digest) } catch (reason) { setDigest(before); setError(errorText(reason)) }
  }
  return <div className="preferences-field attention-digest">
    <label className="switch-row"><span><strong>Daily summary</strong><small>Send one notification a day with the number of waiting tabs and the oldest wait.</small></span>
      <input type="checkbox" role="switch" checked={!!digest?.enabled} disabled={!digest} onChange={event => digest && void save({ ...digest, enabled: event.target.checked })} /></label>
    <label className="digest-hour">Send at
      <select value={digest?.hour ?? 9} disabled={!digest?.enabled} onChange={event => digest && void save({ ...digest, hour: Number(event.target.value) })}>
        {HOURS.map(hour => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00</option>)}
      </select></label>
    {error && <p className="digest-error" role="alert">{error}</p>}
    <span className="preferences-scope">This server</span>
  </div>
}
