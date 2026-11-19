import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { AlarmClock, BellRing, Check, ClipboardList, Pencil, Repeat as RepeatIcon, RotateCcw, SquareTerminal, Trash2, Undo2, X } from 'lucide-react'
import type { Api } from './api'
import { errorText } from './api'
import type { Pane } from './api'
import { useModal } from './hooks'
import { Bone } from './Skeleton'
import { SelectMenu } from './SelectMenu'
import { chatTitle } from './Sidebar'
import { act, fullLabel, groupReminders, parseWhen, quickPicks, repeatLabel, repeatOptions, snoozePicks, TEXT_LIMIT, whenLabel, ZONE_LABEL } from './reminders'
import type { Listing, Reminder, Repeat, useReminders } from './reminders'
import './Reminders.css'

type Polled = ReturnType<typeof useReminders>
const UNDO_MS = 8000
const nowSeconds = () => Date.now() / 1000
const WHO: Record<string, string> = { you: '', codex: 'Codex', claude: 'Claude', antigravity: 'Antigravity' }
const byLabel = (by: string) => by === 'you' ? '' : `Set by ${WHO[by.split(':')[0]] || by}`

/** What the When field holds: the typed text, and the instant of a quick pick until the text changes. A pick keeps its
 *  instant, so "In 1 h" does not move while the sheet is open, and "Tomorrow 10:00" does not move past midnight. */
export interface When { text: string; at?: number }
const EMPTY: When = { text: '' }
const whenAt = (when: When, now: number) => when.at ?? parseWhen(when.text, now)

/** The "When" field: typed text, its parsed time below it, and quick picks that fill it. */
function WhenField({ id, when, onChange, now, picks = true }: { id: string; when: When; onChange: (when: When) => void; now: number; picks?: boolean }) {
  const value = when.text
  const at = whenAt(when, now)
  const hint = `${id}-hint`
  return <div className="reminder-when">
    <label htmlFor={id}>When <span className="reminder-zone">({ZONE_LABEL})</span></label>
    <input id={id} type="text" value={value} autoComplete="off" enterKeyHint="done" placeholder="tomorrow 10:00, in 2 h, fri 18:00"
      aria-describedby={hint} aria-invalid={!!value.trim() && at === null} onChange={event => onChange({ text: event.target.value })} />
    <p id={hint} className={value.trim() && at === null ? 'reminder-parsed bad' : 'reminder-parsed'} aria-live="polite">
      {!value.trim() ? 'Type a time, or pick one.' : at === null ? 'Not a time. Try "tomorrow 10:00" or "in 2 h".' : at <= now ? 'That time has passed.' : fullLabel(at)}
    </p>
    {picks && <div className="reminder-picks" role="group" aria-label="Quick times">
      {quickPicks(now).map(pick => <button key={pick.label} type="button" className="secondary sm reminder-pick" aria-pressed={when.at !== undefined && value === pick.typed} onClick={() => onChange({ text: pick.typed, at: pick.at })}>{pick.label}</button>)}
    </div>}
  </div>
}

function RepeatField({ id, value, at, onChange }: { id: string; value: 'never' | Repeat; at: number | null; onChange: (value: 'never' | Repeat) => void }) {
  return <div className="reminder-repeat-field">
    <span className="reminder-field-label" id={`${id}-label`}>Repeat</span>
    <SelectMenu id={id} value={value} label="Repeat" minWidth={200} className="sm-field reminder-repeat-select"
      icon={<RepeatIcon size={14} aria-hidden="true" />} options={repeatOptions(at)} onChange={next => onChange(next as 'never' | Repeat)} />
  </div>
}

/** One reminder: text, time, links and actions. Editing swaps the row for a small form. */
function Row({ item, now, panes, api, onOpenPane, onOpenItem, onDelete, onError }: {
  item: Reminder; now: number; panes: Pane[]; api: Api; onOpenPane?: (pane: Pane) => void; onOpenItem?: (ref: string) => void
  onDelete: (item: Reminder) => void; onError: (text: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(item.text)
  const [when, setWhen] = useState<When>(EMPTY)
  // null until the user picks: the picker then follows the latest polled rule, and Save leaves the rule alone.
  const [repeatDraft, setRepeatDraft] = useState<'never' | Repeat | null>(null)
  const repeat = repeatDraft ?? item.repeat?.every ?? 'never'
  const [busy, setBusy] = useState(false)
  const formId = useId()
  const run = async (work: () => Promise<unknown>) => {
    setBusy(true)
    try { await work(); return true } catch (error) { onError(errorText(error)); return false } finally { setBusy(false) }
  }
  // A tab can switch to another conversation. With a stored thread, follow that conversation (it can move tabs);
  // when no tab shows it any more, the link is unavailable rather than the tab's new conversation.
  const link = item.link
  const pane = !link?.pane ? undefined : link.thread ? panes.find(entry => entry.thread === link.thread) : panes.find(entry => entry.id === link.pane)
  const done = item.state === 'done'
  if (editing) {
    const typed = !!when.text.trim()
    const at = typed ? whenAt(when, now) : null
    const save = async (event: FormEvent) => {
      event.preventDefault()
      const body: Record<string, unknown> = { id: item.id }
      if (text.trim() !== item.text) body.text = text.trim()
      if (at !== null) body.due = at
      if (repeatDraft !== null && repeatDraft !== (item.repeat?.every ?? 'never')) body.repeat = repeatDraft === 'never' ? null : repeatDraft
      if (Object.keys(body).length === 1 || await run(() => act(api, 'edit', body))) setEditing(false)
    }
    return <li className="reminder-row editing">
      <form onSubmit={save} aria-label={`Edit reminder: ${item.text}`}>
        <label htmlFor={`${formId}-text`}>Reminder</label>
        <textarea id={`${formId}-text`} rows={2} maxLength={TEXT_LIMIT} value={text} onChange={event => setText(event.target.value)} autoFocus />
        <WhenField id={`${formId}-when`} when={when} onChange={setWhen} now={now} picks={false} />
        <p className="reminder-keep">{typed ? '' : `Leave When empty to keep ${whenLabel(item.due, now)}.`}</p>
        {/* A kept due (blank or re-typed) can be a snoozed time; the server keeps the rule's own occurrence then. */}
        <RepeatField id={`${formId}-repeat`} value={repeat} at={at !== null && at !== item.due ? at : item.repeat?.anchor ?? item.due} onChange={setRepeatDraft} />
        {item.repeat && (repeat !== 'never'
          ? <button type="button" className="link-button reminder-stop-repeat" onClick={() => setRepeatDraft('never')}>Stop repeating</button>
          : <p className="reminder-keep">Repeating stops when you save.</p>)}
        <div className="sheet-buttons">
          <button type="button" className="secondary" onClick={() => { setEditing(false); setText(item.text); setWhen(EMPTY) }}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || !text.trim() || (typed && (at === null || at <= now))}>Save</button>
        </div>
      </form>
    </li>
  }
  const snooze = (value: string) => {
    const index = Number(value), pick = snoozePicks(nowSeconds())[index]
    // 10 min and 1 h count from the server's clock; "Tomorrow 10:00" is a fixed time.
    if (pick) void run(() => act(api, 'snooze', { id: item.id, occ: item.occ, ...(index < 2 ? { for: index ? 3600 : 600 } : { until: pick.at }) }))
  }
  const who = byLabel(item.by)
  const label = repeatLabel(item.repeat)
  return <li className={`reminder-row ${item.state}`}>
    <div className="reminder-main">
      <p className="reminder-text">{item.text}</p>
      <p className="reminder-meta">
        <time dateTime={new Date(item.due * 1000).toISOString()} title={fullLabel(item.due)} aria-label={fullLabel(item.due)}>{whenLabel(item.due, now)}</time>
        {label && <span className="reminder-repeat" title={`Repeats: ${label}`}><RepeatIcon size={12} aria-hidden="true" /><span className="visually-hidden">Repeats: </span>{label}</span>}
        {item.state === 'due' && item.late && <span className="reminder-late">Sent late</span>}
        {item.snoozes > 0 && !done && <span>Snoozed {item.snoozes}×</span>}
        {who && <span>{who}</span>}
      </p>
      {(item.link?.pane || item.link?.item || item.link?.project) && <p className="reminder-links">
        {item.link?.pane && (pane && onOpenPane
          ? <button type="button" className="link-button" onClick={() => onOpenPane(pane)}><SquareTerminal size={13} aria-hidden="true" />{chatTitle(pane)}</button>
          : <span title="No open tab shows this conversation"><SquareTerminal size={13} aria-hidden="true" />Conversation closed</span>)}
        {item.link?.item && (onOpenItem
          ? <button type="button" className="link-button" onClick={() => onOpenItem(item.link!.item!)}><ClipboardList size={13} aria-hidden="true" />{item.link.item}</button>
          : <span>{item.link.item}</span>)}
        {item.link?.project && <span className="reminder-project">{item.link.project}</span>}
      </p>}
    </div>
    <div className="reminder-actions">
      {done
        ? <button type="button" className="icon-button" aria-label={`Reopen: ${item.text}`} title="Reopen" disabled={busy} onClick={() => void run(() => act(api, 'reopen', { id: item.id }))}><RotateCcw size={16} /></button>
        : <>
          <button type="button" className="icon-button reminder-done" aria-label={`Done: ${item.text}`} title="Done" disabled={busy} onClick={() => void run(() => act(api, 'done', { id: item.id, occ: item.occ }))}><Check size={17} /></button>
          <SelectMenu value="" label="Snooze" triggerLabel="" triggerTitle={item.text} className="icon-button reminder-snooze" minWidth={180} disabled={busy}
            icon={<AlarmClock size={16} aria-hidden="true" />} onChange={snooze}
            options={snoozePicks(now).map((pick, index) => ({ value: String(index), label: pick.label, detail: whenLabel(pick.at, now) }))} />
          <button type="button" className="icon-button" aria-label={`Edit: ${item.text}`} title="Edit" disabled={busy} onClick={() => { setText(item.text); setWhen(EMPTY); setRepeatDraft(null); setEditing(true) }}><Pencil size={15} /></button>
        </>}
      <button type="button" className="icon-button" aria-label={`Delete: ${item.text}`} title="Delete" disabled={busy} onClick={() => onDelete(item)}><Trash2 size={15} /></button>
    </div>
  </li>
}

function Section<T extends Reminder>({ title, items, children, folded = false, note }: { title: string; items: T[]; children: (item: T) => React.ReactNode; folded?: boolean; note?: string }) {
  const id = useId()
  const [open, setOpen] = useState(!folded)
  if (!items.length) return null
  return <section className="reminder-section" aria-labelledby={id}>
    <h3 id={id}>{folded
      ? <button type="button" className="reminder-fold" aria-expanded={open} onClick={() => setOpen(!open)}>{title} <span className="reminder-count">{items.length}</span></button>
      : <>{title} <span className="reminder-count">{items.length}</span></>}</h3>
    {open && note && <p className="reminder-deleted-note">{note}</p>}
    {open && <ul>{items.map(children)}</ul>}
  </section>
}

/** Reminders: add one, and see what is due, upcoming and done. Times show in IST. */
export function RemindersSheet({ api, polled, panes, current, onOpenPane, onOpenItem, onClose }: {
  api: Api; polled: Polled; panes: Pane[]; current?: Pane; onOpenPane?: (pane: Pane) => void; onOpenItem?: (ref: string) => void; onClose: () => void
}) {
  const modal = useModal(true, onClose, undefined, { view: 'reminders' })
  const { data, error, loading, refresh } = polled
  const [now, setNow] = useState(nowSeconds)
  useEffect(() => { const timer = setInterval(() => setNow(nowSeconds()), 15000); return () => clearInterval(timer) }, [])
  const [text, setText] = useState('')
  const [when, setWhen] = useState<When>(EMPTY)
  const [repeat, setRepeat] = useState<'never' | Repeat>('never')
  const [linkTab, setLinkTab] = useState(false)
  const [saving, setSaving] = useState(false)
  const [problem, setProblem] = useState('')
  const [undo, setUndo] = useState<null | { label: string; back: () => Promise<unknown> }>(null)
  const textRef = useRef<HTMLTextAreaElement>(null)
  const ids = useId()
  useEffect(() => {
    if (!undo) return
    const timer = window.setTimeout(() => setUndo(null), UNDO_MS)
    return () => window.clearTimeout(timer)
  }, [undo])
  const groups = useMemo(() => groupReminders(data?.reminders ?? []), [data])
  const at = whenAt(when, now)
  const ready = !!text.trim() && at !== null && at > now
  const add = async (event: FormEvent) => {
    event.preventDefault()
    if (!ready || saving) return
    setSaving(true); setProblem('')
    try {
      const link = linkTab && current ? { pane: current.id, thread: current.thread || undefined } : undefined
      await act(api, 'add', { text: text.trim(), due: at, link, repeat: repeat === 'never' ? null : repeat })
      setText(''); setWhen(EMPTY); setRepeat('never'); setLinkTab(false)
      textRef.current?.focus()
    } catch (error) { setProblem(errorText(error)) } finally { setSaving(false) }
  }
  const remove = async (item: Reminder) => {
    setProblem('')
    try {
      await act(api, 'delete', { id: item.id })
      setUndo({ label: 'Reminder deleted.', back: () => act(api, 'restore', { id: item.id }) })
    } catch (error) { setProblem(errorText(error)) }
  }
  const restore = async (id: string) => {
    setProblem('')
    try { await act(api, 'restore', { id }) } catch (error) { setProblem(errorText(error)) }
  }
  const deleted = useMemo(() => [...(data?.deleted ?? [])].sort((a, b) => b.deleted_at - a.deleted_at), [data])
  const row = (item: Reminder) => <Row key={item.id} item={item} now={now} panes={panes} api={api} onDelete={item => void remove(item)} onError={setProblem}
    onOpenPane={onOpenPane && (pane => { onClose(); onOpenPane(pane) })} onOpenItem={onOpenItem && (ref => { onClose(); onOpenItem(ref) })} />
  const list: Listing | undefined = data
  return <dialog {...modal} className="sheet settings-sheet reminders-sheet" aria-labelledby={`${ids}-title`}>
    <div className="sheet-heading">
      <h2 id={`${ids}-title`}>Reminders</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
    </div>
    <p className="sheet-sub sheet-note">Tether notifies you at the time, like an urgent ask. Times are in {ZONE_LABEL}.</p>
    <form className="reminder-add" onSubmit={add} aria-label="New reminder">
      <label htmlFor={`${ids}-text`}>Remind me to</label>
      <textarea id={`${ids}-text`} ref={textRef} rows={2} maxLength={TEXT_LIMIT} value={text} placeholder="Check the deploy"
        onChange={event => setText(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void add(event) }} />
      <WhenField id={`${ids}-when`} when={when} onChange={setWhen} now={now} />
      <RepeatField id={`${ids}-repeat`} value={repeat} at={at} onChange={setRepeat} />
      {current && <label className="reminder-link-tab"><input type="checkbox" checked={linkTab} onChange={event => setLinkTab(event.target.checked)} />
        <span>Link to this tab: {chatTitle(current)}</span></label>}
      <button type="submit" className="primary reminder-submit" disabled={!ready || saving}><BellRing size={15} aria-hidden="true" />{saving ? 'Adding…' : 'Add reminder'}</button>
    </form>
    {problem && <p className="error reminder-error" role="alert">{problem}</p>}
    <div className="reminder-lists" aria-busy={loading && !list}>
      {loading && !list && <ul className="reminder-skeleton" aria-label="Loading reminders">{[0, 1, 2].map(key => <li key={key}><Bone w="70%" h={14} /><Bone w="35%" h={12} /></li>)}</ul>}
      {error && !list && <div className="reminder-load-error" role="alert"><p>Cannot load reminders: {error}</p><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
      {error && list && <p className="reminder-stale" role="status">Not up to date: {error}</p>}
      {list && !list.reminders.length && !deleted.length && <p className="reminder-empty">No reminders yet. Add one above, or ask an agent: “remind me tomorrow at 10 to check the deploy”.</p>}
      <Section title="Due" items={groups.due}>{row}</Section>
      <Section title="Upcoming" items={groups.upcoming}>{row}</Section>
      <Section title="Done" items={groups.done} folded>{row}</Section>
      <Section title="Recently deleted" items={deleted} folded note="Kept for 30 days.">{item => <li key={item.id} className="reminder-row deleted">
        <div className="reminder-main"><p className="reminder-text">{item.text}</p>
          <p className="reminder-meta"><time dateTime={new Date(item.due * 1000).toISOString()} title={fullLabel(item.due)} aria-label={fullLabel(item.due)}>{whenLabel(item.due, now)}</time></p></div>
        <div className="reminder-actions"><button type="button" className="icon-button" aria-label={`Restore: ${item.text}`} title="Restore" onClick={() => void restore(item.id)}><Undo2 size={16} /></button></div>
      </li>}</Section>
    </div>
    {undo && <div className="backlog-undo reminder-undo" role="status"><span>{undo.label} It stays in Recently deleted for 30 days.</span>
      <button type="button" className="link-button" onClick={() => { const back = undo.back; setUndo(null); back().catch(error => setProblem(errorText(error))) }}>Undo</button>
      <button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setUndo(null)}><X size={14} /></button></div>}
  </dialog>
}

/** The sidebar's way in, under Backlog: how many reminders are due now. */
export function RemindersButton({ due, onOpen }: { due: number; onOpen: () => void }) {
  return <button type="button" className="sidebar-backlog sidebar-reminders" aria-label={due ? `Reminders, ${due} due` : 'Reminders'} onClick={onOpen}>
    <BellRing size={16} aria-hidden="true" /><span>Reminders</span>
    {due > 0 && <span className="sidebar-backlog-new">{due} due</span>}
  </button>
}

