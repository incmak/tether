// The action deck: the asks that wait on you, one card at a time. Each card shows the tab, the question and the
// right answer control: a reply box for a reply ask, or "Open tab" for a prompt that waits in the terminal
// (approval, plan, question). It opens from the Waiting on you list, and by itself for a new urgent ask.
import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, CircleAlert, ExternalLink, ListChecks, Send, X } from 'lucide-react'
import type { Api, Pane } from './api'
import { errorText } from './api'
import { askAction } from './attention'
import type { Waiting } from './attention'
import { sendToTab } from './backlogSend'
import { withApproval } from './approval'
import { useModal } from './hooks'
import { age } from './needs'
import { useNow } from './attention'
import { chatTitle } from './Sidebar'
import type { Session } from './Sidebar'
import type { ReviewEntry } from './Attention'
import './ActionDeck.css'

const KIND: Record<string, string> = { reply: 'Asks you', approval: 'Needs approval', plan: 'Plan to approve', question: 'Asks a question' }

/** Urgent asks first, then oldest first. */
export const deckOrder = (items: Waiting[]) => [...items].sort((a, b) => Number(!!b.ask.urgent) - Number(!!a.ask.urgent) || a.ask.since - b.ask.since)

export function ActionDeck({ api, items, sessions, review, onOpen, onClose, onUpdate }: { api: Api; items: Waiting[]; sessions: Session[]; review?: ReviewEntry; onOpen: (pane: Pane) => void; onClose: () => void; onUpdate: () => void }) {
  const modal = useModal(true, onClose)
  const now = useNow()
  // The deck keeps the order it opened with, so a card does not jump while you type. New asks join at the end.
  const [first] = useState(() => deckOrder(items).map(item => item.pane.id))
  const [done, setDone] = useState<string[]>([])
  const [index, setIndex] = useState(0)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const queue = [...first, ...deckOrder(items).map(item => item.pane.id).filter(id => !first.includes(id))].filter(id => !done.includes(id))
  const live = queue.map(id => items.find(item => item.pane.id === id)).filter((item): item is Waiting => !!item)
  const at = Math.min(index, Math.max(0, live.length - 1))
  const card = live[at]

  if (!card) {
    return <dialog {...modal} className="sheet action-deck" aria-labelledby="deck-title">
      <div className="sheet-heading deck-head"><h2 id="deck-title">Waiting on you</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button></div>
      <div className="deck-empty" role="status"><Check size={22} aria-hidden="true" /><p>{status || 'Nothing waits on you.'}</p>
        {review && <p className="deck-hint">Lessons are ready to review: {review.label}.</p>}</div>
      <div className="deck-actions">
        {review && <button type="button" className="secondary" onClick={() => { onClose(); review.onOpen() }}><ListChecks size={15} aria-hidden="true" />Review lessons</button>}
        <button type="button" className="primary" onClick={onClose}>Done</button></div>
    </dialog>
  }

  const { pane, ask } = card
  const session = sessions.find(item => item.id === pane.tmux_session)
  const name = chatTitle(pane, session)
  const next = () => { setStatus(''); setIndex(at + 1 < live.length ? at + 1 : 0) }
  async function run(work: () => Promise<unknown>, result: string) {
    setBusy(true); setStatus('')
    try { const said = await work(); setStatus(typeof said === 'string' ? said : result); onUpdate(); setDone(current => [...current, pane.id]) }
    catch (error) { setStatus(errorText(error)) }
    finally { setBusy(false) }
  }
  const send = (text: string, remember: boolean) => void run(async () => {
    await sendToTab(api, pane, text)
    if (!remember) return
    // The reply went out, so the card is done even when the save fails; say so instead of offering a resend.
    try { await withApproval(api, 'autonomy/decisions', 'decision-save', { pane: pane.id, question: ask.next || '', answer: text }) }
    catch (error) { return `Sent to ${name}. Tether did not save the answer: ${errorText(error)}` }
  }, remember ? `Sent to ${name}. Saved for this project.` : `Sent to ${name}.`)
  const handled = () => void run(() => askAction(api, pane, 'handled'), `${name}: handled.`)

  return <dialog {...modal} className={ask.urgent ? 'sheet action-deck urgent' : 'sheet action-deck'} aria-labelledby="deck-title">
    <div className="sheet-heading deck-head">
      <h2 id="deck-title">Waiting on you <span className="deck-count">{at + 1} of {live.length}</span></h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button>
    </div>
    <article className="deck-card" aria-live="polite">
      <p className="deck-meta">
        {ask.urgent && <span className="deck-urgent"><CircleAlert size={14} aria-hidden="true" />Urgent</span>}
        <span>{KIND[ask.kind] ?? 'Waits on you'}</span><span aria-hidden="true">·</span><span>waited {age(ask.since, now)}</span>
      </p>
      <h3 className="deck-tab">{name}</h3>
      <p className="deck-question">{ask.next || 'Open the tab to see what it needs.'}</p>
      {ask.kind === 'reply'
        ? <ReplyBox key={`${pane.id} ${ask.since}`} name={name} busy={busy} onSend={send} />
        : <p className="deck-hint">This prompt waits in the tab. Open the tab to answer it.</p>}
    </article>
    {status && <p className="deck-status" role="status">{status}</p>}
    <div className="deck-actions">
      <button type="button" className="secondary" disabled={busy} onClick={() => { onClose(); onOpen(pane) }}><ExternalLink size={15} aria-hidden="true" />Open tab</button>
      <button type="button" className="secondary" disabled={busy} onClick={handled}><Check size={15} aria-hidden="true" />Handled</button>
      <button type="button" className="secondary" disabled={busy || live.length < 2} onClick={next}>Skip<ArrowRight size={15} aria-hidden="true" /></button>
    </div>
  </dialog>
}

/** The answer box. Keyed by the ask, so each card starts empty. Ctrl/Cmd+Enter sends. */
function ReplyBox({ name, busy, onSend }: { name: string; busy: boolean; onSend: (text: string, remember: boolean) => void }) {
  const [text, setText] = useState('')
  const [remember, setRemember] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)
  // The dialog's showModal() runs after this mounts and moves focus to the first button; take it back a frame later.
  useEffect(() => { const frame = requestAnimationFrame(() => box.current?.focus()); return () => cancelAnimationFrame(frame) }, [])
  const send = () => { if (text.trim()) onSend(text.trim(), remember) }
  return <form className="deck-reply" onSubmit={event => { event.preventDefault(); send() }}>
    <label htmlFor="deck-text" className="visually-hidden">Your answer to {name}</label>
    <textarea ref={box} id="deck-text" rows={3} value={text} disabled={busy} placeholder="Your answer"
      onChange={event => setText(event.target.value)}
      onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send() } }} />
    <div className="deck-reply-row">
      <label className="deck-remember"><input type="checkbox" checked={remember} disabled={busy} onChange={event => setRemember(event.target.checked)} />Remember for this project</label>
      <button type="submit" className="primary" disabled={busy || !text.trim()}><Send size={15} aria-hidden="true" />Send</button>
    </div>
  </form>
}

/** Opens the deck by itself once for each urgent ask it has not shown yet, also on page load. */
export function useUrgentDeck(items: Waiting[], enabled: boolean) {
  const [open, setOpen] = useState(false)
  const [seen, setSeen] = useState<string[]>([])
  const fresh = items.filter(item => item.ask.urgent).map(item => `${item.pane.id} ${item.ask.since}`).filter(key => !seen.includes(key))
  if (fresh.length) { // adjust state while rendering (React's pattern for state derived from props)
    setSeen([...seen, ...fresh])
    if (enabled) setOpen(true)
  }
  return [open, setOpen] as const
}
