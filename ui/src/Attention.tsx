// Attention mode (server/attention.py): the sidebar "Waiting on you" box, the header "Open the oldest" button (a pill
// with a bottom sheet on a phone), the Handled, Remind me tomorrow and Later chips under the last reply, the "Handled for
// you" log with Later and Undo (B335), and the daily summary and stale ask settings.
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ArchiveRestore, ArrowDownWideNarrow, ArrowUpNarrowWide, BellRing, Check, Clock, GalleryVerticalEnd, Hand, ListChecks, MoonStar, RotateCw, Sparkles, Undo2, X } from 'lucide-react'
import type { WaitOrder } from './prefs'
import type { Api, Ask, Pane } from './api'
import { ApiError, errorText } from './api'
import type { Reminder } from './reminders'
import { whenLabel } from './reminders'
import { fmtDate, fmtTime, tetherZone, tetherZoneConfirmed, useTetherZone, useTetherZoneConfirmed } from './zone'
import { askAction, confirmStaleRule, LATE, splitToday, staleRuleQueue, stillWait, useNow } from './attention'
import type { AskAction, AttentionState, StaleRuleResult, AutoEntry, AutoLog, Digest, Waiting } from './attention'
import { useModal } from './hooks'
import { age } from './needs'
import { chatTitle } from './Sidebar'
import type { Session } from './Sidebar'
import './attention.css'

const seenText = (ask: Ask) => ask.opened ? 'Opened, not answered' : 'Not opened'
const DONE: Record<AskAction, string> = { handled: 'Handled.', snooze: 'Snoozed until tomorrow.', opened: '', later: 'Moved to Later.', back: 'Back in the queue.' }
const clock = (at: number) => fmtTime(at * 1000, { hour: '2-digit', minute: '2-digit' })
const ago = (at: number, now: number) => { const text = age(at, now); return text === 'now' ? 'just now' : `${text} ago` }
const dayText = (at: number) => fmtDate(at * 1000, { day: 'numeric', month: 'short' })
/** "In Later since 16 Nov, moved by Tether" */
const laterText = (later: NonNullable<Ask['later']>, now: number) =>
  `In Later ${now - later.at < 86400 ? `for ${age(later.at, now)}` : `since ${dayText(later.at)}`}, moved by ${later.by === 'rule' ? 'Tether' : 'you'}`

/** The sort of a Waiting on you list. items stay oldest first everywhere else ("Open the oldest" uses items[0]). */
export interface WaitSort { order: WaitOrder; onOrder: (order: WaitOrder) => void }
/** The lessons review entry: one quiet row for all projects, after the tabs. */
export interface ReviewEntry { label: string; onOpen: () => void }

/** Due reminders: one row each, after the tabs. A row opens the Reminders sheet. */
export interface DueReminders { items: Reminder[]; onOpen: () => void }

export function ReminderRows({ due, now, className, list = false }: { due: DueReminders; now: number; className: string; list?: boolean }) {
  useTetherZone()
  const catchup = className.startsWith('catchup')
  return <>{due.items.map(item => {
    const when = `${item.late ? 'Late, was due' : 'Due'} ${whenLabel(item.due, now)}`
    return <button key={item.id} type="button" className={className} title={item.text} aria-label={`Reminder: ${item.text}. ${when}`} onClick={due.onOpen}>
      <BellRing size={14} className={catchup ? 'catchup-icon' : 'waiting-review-icon'} aria-hidden="true" />
      <span className={catchup ? 'catchup-name' : 'waiting-name'}>{item.text}</span>
      {!list && <span className="waiting-seen">{when}</span>}
    </button>
  })}</>
}

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
export function WaitingBox({ items, sessions, selected, limit = 3, variant = 'box', sort, review, reminder, onOpen, onDeck }: { items: Waiting[]; sessions: Session[]; selected?: string; limit?: number; variant?: 'box' | 'list'; sort?: WaitSort; review?: ReviewEntry; reminder?: DueReminders; onOpen: (pane: Pane) => void; onDeck?: () => void }) {
  const now = useNow()
  const id = useId()
  const [all, setAll] = useState(false)
  if (!items.length && !review && !reminder) return null
  const list = variant === 'list'
  const sorted = ordered(items, sort?.order)
  const shown = limit && !all ? sorted.slice(0, limit) : sorted
  return <section className={list ? 'waiting-box waiting-list' : 'waiting-box'} aria-labelledby={id}>
    <h2 className="waiting-title" id={id}><Hand size={list ? 13 : 14} aria-hidden="true" />Waiting on you<span className="waiting-count">{list ? '· ' : ''}{items.length + (review ? 1 : 0) + (reminder?.items.length ?? 0)}</span>{sort && items.length > 1 && <SortButton {...sort} />}{onDeck && items.length > 0 && (list
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
    {reminder && <ReminderRows due={reminder} now={now} className="waiting-row waiting-review" list={list} />}
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

/** Under the last reply of a tab that waits on you. A visible, focused page marks the ask opened (it cancels the Teams ping). onDone gets the confirmation. */
export function AskChips({ api, pane, ask, onDone, onUpdate }: { api: Api; pane: Pane; ask: Ask; onDone: (text: string) => void; onUpdate: () => void }) {
  const now = useNow()
  const [busy, setBusy] = useState(false)
  const marked = useRef('')
  useEffect(() => {
    if (ask.opened) {
      marked.current = ''
      return
    }
    const key = JSON.stringify([pane.id, pane.thread, ask.id, ask.since, ask.next, ask.urgent])
    const markOpened = () => {
      if (document.visibilityState !== 'visible' || !document.hasFocus() || marked.current === key) return
      marked.current = key
      askAction(api, pane, 'opened', ask).catch(error => {
        if (error instanceof ApiError && error.status === 409) return
        if (marked.current === key) marked.current = ''
      })
    }
    document.addEventListener('visibilitychange', markOpened)
    window.addEventListener('focus', markOpened)
    markOpened()
    return () => {
      document.removeEventListener('visibilitychange', markOpened)
      window.removeEventListener('focus', markOpened)
    }
  }, [api, pane, ask])
  const act = async (action: AskAction, from?: HTMLButtonElement) => {
    if (busy) return
    const chat = from?.closest('.chat-panel')
    setBusy(true)
    try {
      const result = await askAction(api, pane, action, ask)
      onDone(`${DONE[action]} ${stillWait(result.waiting)}`)
      chat?.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus()
      onUpdate()
    } catch (error) {
      onDone(errorText(error))
    } finally {
      setBusy(false)
    }
  }
  if (ask.later) return <div className="ask-chips" role="group" aria-label="This tab's ask is in Later">
    <button type="button" className="ask-chip primary" aria-disabled={busy} onClick={event => void act('back', event.currentTarget)}><ArchiveRestore size={15} aria-hidden="true" />Back to the queue</button>
    <button type="button" className="ask-chip" aria-disabled={busy} onClick={event => void act('handled', event.currentTarget)}><Check size={15} aria-hidden="true" />Handled, no reply needed</button>
    <span className="ask-state">{laterText(ask.later, now)} · asked {ago(ask.asked_at ?? ask.since, now)}</span>
  </div>
  return <div className="ask-chips" role="group" aria-label="This tab waits on you">
    <button type="button" className="ask-chip primary" disabled={busy} onClick={() => void act('handled')}><Check size={15} aria-hidden="true" />Handled, no reply needed</button>
    <button type="button" className="ask-chip" disabled={busy} onClick={() => void act('snooze')}><Clock size={15} aria-hidden="true" />Remind me tomorrow</button>
    <button type="button" className="ask-chip" disabled={busy} onClick={() => void act('later')}><MoonStar size={15} aria-hidden="true" />Later</button>
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

/** The text of one log line's task: the tab name the rule saw. */
const entryTab = (entry: AutoEntry) => String(entry.detail?.tab ?? entry.detail?.pane ?? '')

/** Home header: "Handled for you N" (today's moves by Tether's rules, not undone). It opens the log with Later and Undo. */
export function HandledButton({ api, later, sessions, refreshKey, onOpen, onUpdate }: { api: Api; later: Waiting[]; sessions: Session[]; refreshKey: string; onOpen: (pane: Pane) => void; onUpdate: () => void }) {
  const [count, setCount] = useState<number | null>(null)
  const [open, setOpen] = useState(false)
  const load = useCallback((signal?: AbortSignal) => api<AttentionState>('attention', undefined, signal).then(data => setCount(data.handled_today)).catch(() => { /* the count is a hint; the log shows errors */ }), [api])
  // Again when the tabs change (a rule moved an ask, or you put one back) and each minute for a new day.
  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    const timer = setInterval(() => void load(controller.signal), 60000)
    return () => { controller.abort(); clearInterval(timer) }
  }, [load, refreshKey])
  return <>
    {count !== null && <button type="button" className="ghost home-handled" aria-haspopup="dialog" onClick={() => setOpen(true)}
      aria-label={`Handled for you today: ${count}. ${later.length} in Later. Show the log`}>
      <Sparkles size={15} aria-hidden="true" />Handled for you<b className="home-handled-count">{count}</b>
    </button>}
    {open && <HandledSheet api={api} later={later} sessions={sessions} onOpen={pane => { setOpen(false); onOpen(pane) }}
      onChanged={() => { void load(); onUpdate() }} onClose={() => setOpen(false)} />}
  </>
}

const laterKey = ({ pane, ask }: Waiting) => `later:${ask.id ?? pane.id}`

/** The automation log as a dialog: what Tether did by itself (with Undo), and the asks in Later (with Back to the queue). */
function HandledSheet({ api, later, sessions, onOpen, onChanged, onClose }: { api: Api; later: Waiting[]; sessions: Session[]; onOpen: (pane: Pane) => void; onChanged: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const heading = useRef<HTMLHeadingElement>(null)
  const laterHeading = useRef<HTMLHeadingElement>(null)
  const laterButtons = useRef(new Map<string, HTMLButtonElement>())
  const focusLater = useRef<{ id: string; neighbors: string[] } | null>(null)
  const focusUndo = useRef('')
  const now = useNow()
  // Reload when the zone changes locally and when the server confirms it.
  const zone = useTetherZone()
  const confirmed = useTetherZoneConfirmed()
  const [loaded, setLoaded] = useState<AutoLog | null>(null)
  const [failure, setFailure] = useState<{ api: Api; zone: string; confirmed: boolean; text: string } | null>(null)
  const latestRequest = useRef(0)
  const log = loaded?.zone === zone ? loaded : null
  const currentFailure = failure?.api === api && failure.zone === zone && failure.confirmed === confirmed ? failure : null
  if (failure && !currentFailure) setFailure(null)
  const error = currentFailure?.text ?? ''
  const [busy, setBusy] = useState('')
  const [rowError, setRowError] = useState<{ id: string; text: string } | null>(null)
  const [status, setStatus] = useState('')
  const load = useCallback((signal?: AbortSignal) => {
    const request = ++latestRequest.current
    const requestedZone = tetherZone()
    const requestedConfirmation = tetherZoneConfirmed()
    const isCurrent = () => request === latestRequest.current && !signal?.aborted
      && tetherZone() === requestedZone && tetherZoneConfirmed() === requestedConfirmation
    return api<AutoLog>('attention/log', undefined, signal).then(data => {
      if (!isCurrent()) return
      setLoaded(data)
      setFailure(null)
    }).catch(reason => {
      if (isCurrent()) setFailure({ api, zone: requestedZone, confirmed: requestedConfirmation, text: errorText(reason) })
    })
  }, [api])
  const reload = () => {
    setFailure(null)
    return load()
  }
  useLayoutEffect(() => {
    const pending = focusLater.current
    if (!pending || later.some(item => laterKey(item) === pending.id)) return
    focusLater.current = null
    const neighbor = pending.neighbors.map(id => laterButtons.current.get(id)).find(Boolean)
    const target = neighbor ?? laterHeading.current ?? heading.current
    target?.focus()
  })
  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort() }, [load, zone, confirmed])
  const back = async (id: string, write: () => Promise<unknown>, done: string, source: { undo: AutoEntry } | { index: number }) => {
    if (busy) return
    setBusy(id); setRowError(null)
    try {
      await write()
      if ('undo' in source) {
        focusUndo.current = id
        setLoaded(current => current && {
          ...current,
          entries: current.entries.map(row => row.id === id ? { ...row, undone: true, undo: null } : row),
          handled_today: source.undo.at >= current.today_start ? Math.max(0, current.handled_today - 1) : current.handled_today,
        })
      } else {
        const neighbors = [...later.slice(source.index + 1), ...later.slice(0, source.index).reverse()].map(laterKey)
        focusLater.current = { id, neighbors }
      }
      setStatus(done)
      onChanged()
      await reload()
    } catch (reason) {
      setRowError({ id, text: errorText(reason) })
    } finally {
      setBusy('')
    }
  }
  const nameOf = (pane: Pane) => chatTitle(pane, sessions.find(item => item.id === pane.tmux_session))
  const { today, older } = log ? splitToday(log.entries, log.today_start) : { today: [], older: [] }
  const line = (entry: AutoEntry, isToday: boolean) => {
    const tab = entryTab(entry), rule = String(entry.detail?.rule ?? '')
    const target = entry.undo
    return <li key={entry.id} className="handled-line">
      <time dateTime={new Date(entry.at * 1000).toISOString()}>{isToday ? clock(entry.at) : `${dayText(entry.at)} ${clock(entry.at)}`}</time>
      <span className="handled-text">{tab && <b>{tab}</b>}<span>{entry.summary}</span>{rule && <small>{rule}</small>}</span>
      {target ? <button type="button" className="secondary sm handled-undo" aria-disabled={!!busy}
        aria-label={`Undo: ${entry.summary}${tab ? ` on ${tab}` : ''}`} onClick={() => void back(entry.id, () => api('attention/back', target), `Put the ask on ${tab || 'the tab'} back in the queue.`, { undo: entry })}>
        <Undo2 size={14} aria-hidden="true" />{busy === entry.id ? 'Undoing…' : 'Undo'}</button>
        : entry.undone ? <span className="handled-undone" tabIndex={-1} ref={node => {
          if (node && focusUndo.current === entry.id) {
            node.focus()
            focusUndo.current = ''
          }
        }}>Undone</span> : entry.failed ? <span className="handled-undone">Not moved</span> : null}
      {rowError?.id === entry.id && <p className="error handled-row-error" role="alert">{rowError.text}</p>}
    </li>
  }
  return <dialog {...modal} className="sheet settings-sheet handled-sheet" aria-labelledby="handled-title">
    <div className="sheet-heading">
      <h2 id="handled-title" ref={heading} tabIndex={-1}>Handled for you</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
    </div>
    <p className="sheet-sub sheet-note">Tether moves an ask to Later when it waited over 24 hours on an idle tab. Undo puts it back.</p>
    <p className="visually-hidden" role="status">{status}</p>
    <section className="handled-section" aria-labelledby="handled-later">
      <h3 id="handled-later" ref={laterHeading} tabIndex={-1}>Later <span className="handled-count">{later.length}</span></h3>
      {later.length ? <ul className="handled-list">{later.map((item, index) => {
        const { pane, ask } = item
        const name = nameOf(pane), id = laterKey(item)
        return <li key={pane.id} className="handled-later-row">
          <button type="button" className="handled-open" onClick={() => onOpen(pane)} aria-label={`Open ${name}. ${ask.next}`}>
            <b>{name}</b>{ask.next && <span className="handled-next">{ask.next}</span>}
            {ask.later && <small>{laterText(ask.later, now)}</small>}
          </button>
          <button type="button" className="secondary sm" aria-disabled={!!busy || !ask.id} aria-label={`Back to the queue: ${name}`}
            ref={node => { if (node) laterButtons.current.set(id, node); else laterButtons.current.delete(id) }}
            onClick={() => { if (ask.id) void back(id, () => askAction(api, pane, 'back', ask), `${name} is back in the queue.`, { index }) }}>
            <ArchiveRestore size={14} aria-hidden="true" />{busy === id ? 'Moving…' : 'Back to the queue'}</button>
          {rowError?.id === id && <p className="error handled-row-error" role="alert">{rowError.text}</p>}
        </li>
      })}</ul> : <p className="handled-empty">Nothing in Later.</p>}
    </section>
    <section className="handled-section" aria-labelledby="handled-log">
      <h3 id="handled-log">Today <span className="handled-count">{log ? log.handled_today : ''}</span></h3>
      {!log && !error && <p className="handled-empty" role="status">Loading the log…</p>}
      {error && <div className="handled-error" role="alert"><p className="error">Could not load the log. {error}</p>
        <button type="button" className="secondary sm" onClick={() => void reload()}><RotateCw size={14} aria-hidden="true" />Retry</button></div>}
      {log && (today.length ? <ol className="handled-list">{today.map(entry => line(entry, true))}</ol> : <p className="handled-empty">Nothing was handled for you today.</p>)}
    </section>
    {older.length > 0 && <section className="handled-section" aria-labelledby="handled-older">
      <h3 id="handled-older">Last 7 days</h3>
      <ol className="handled-list">{older.map(entry => line(entry, false))}</ol>
    </section>}
    <button type="button" className="secondary waiting-close" onClick={onClose}><X size={16} aria-hidden="true" />Close</button>
  </dialog>
}

/** Settings: the stale ask rule, on by default. It is a server setting. */
export function StaleRuleSetting({ api }: { api: Api }) {
  const [{ shown: stale }, setStale] = useState<{ confirmed: boolean | null; shown: boolean | null }>(
    () => ({ confirmed: staleRuleQueue.confirmed, shown: staleRuleQueue.busy ? staleRuleQueue.wanted : null }))
  const [error, setError] = useState('')
  useEffect(() => {
    const listener = (result: StaleRuleResult) => {
      setStale(current => confirmStaleRule(current, result.confirmed, result.last))
      if (result.last) setError(result.error)
    }
    staleRuleQueue.listener = listener
    const controller = new AbortController()
    const accept = staleRuleQueue.startRead()
    api<AttentionState>('attention', undefined, controller.signal).then(data => {
      const value = data.rules?.stale ?? true
      if (!controller.signal.aborted && accept(value)) setStale(state => confirmStaleRule(state, value, true))
    }).catch(reason => { if (!controller.signal.aborted && accept(null)) setError(errorText(reason)) })
    return () => {
      controller.abort()
      if (staleRuleQueue.listener === listener) staleRuleQueue.listener = null
    }
  }, [api])
  const save = (next: boolean) => {
    setStale(current => ({ ...current, shown: next })); setError('')
    void staleRuleQueue.set(next,
      async value => (await api<{ rules: { stale: boolean } }>('attention/rules', { stale: value })).rules.stale,
      async () => (await api<AttentionState>('attention')).rules.stale,
      errorText)
  }
  return <div className="preferences-field attention-digest">
    <label className="switch-row"><span><strong>Move stale asks to Later</strong><small>An ask that waited over 24 hours on an idle tab leaves the queue. Undo it in Handled for you on Home.</small></span>
      <input type="checkbox" role="switch" checked={!!stale} disabled={stale === null} onChange={event => void save(event.target.checked)} /></label>
    {error && <p className="digest-error" role="alert">{error}</p>}
    <span className="preferences-scope">This server</span>
  </div>
}
