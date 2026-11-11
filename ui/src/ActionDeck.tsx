// The action deck: the asks that wait on you, one card at a time. Each card shows the tab, the whole prompt and the
// right answer control: a reply box for a reply ask, or the Home card's answer buttons for a prompt that waits in the
// terminal (approval, plan, question). On a wide screen a list of every ask sits beside the card.
// It opens from the Waiting on you list, and by itself for a new urgent ask.
import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, CircleAlert, ExternalLink, Send, X } from 'lucide-react'
import type { Api, Choice, Pane } from './api'
import { errorText } from './api'
import { AnswerActions } from './answers'
import { askAction } from './attention'
import type { Waiting } from './attention'
import { sendToTab } from './backlogSend'
import { answerName, askLines, askText, deckItem, deckKey, deckOrder, hasAnswers, isTyping, planOptions, sendPlanChoice } from './deck'
import { useModal } from './hooks'
import { age } from './needs'
import { useNow } from './attention'
import { agentNames, chatTitle } from './Sidebar'
import type { Session } from './Sidebar'
import { answerError, sendAnswer } from './useAnswers'
import './ActionDeck.css'

const KIND: Record<string, string> = { reply: 'Asks you', approval: 'Needs approval', plan: 'Plan to approve', question: 'Asks a question' }
/** A key pressed this soon after a card appears was meant for the card before it (milliseconds). */
const SETTLE = 400

export function ActionDeck({ api, items, sessions, onOpen, onClose, onUpdate }: { api: Api; items: Waiting[]; sessions: Session[]; onOpen: (pane: Pane) => void; onClose: () => void; onUpdate: () => void }) {
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
  const title = (pane: Pane) => chatTitle(pane, sessions.find(item => item.id === pane.tmux_session))
  // A prompt in the terminal is answered from the pane's state now, so the buttons match the text on the card.
  const item = card && card.ask.kind !== 'reply' ? deckItem(card.pane) : undefined
  const next = () => { setStatus(''); setIndex(at + 1 < live.length ? at + 1 : 0) }

  async function run(pane: Pane, work: () => Promise<unknown>, result: string) {
    setBusy(true); setStatus('')
    try { await work(); setStatus(result); onUpdate(); setDone(current => [...current, pane.id]) }
    catch (error) { setStatus(answerError(error)); onUpdate() }
    finally { setBusy(false) }
  }
  // The same checked routes as the Home card (useAnswers.ts). A sent answer cannot be taken back, so there is no undo.
  const answer = (pane: Pane, option: Choice) => { if (item && !busy) void run(pane, () => sendAnswer(api, item, option), `${answerName(item, option)} sent to ${title(pane)}.`) }

  // Y, A and N answer a checked approval, 1-9 a question's menu, J shows the next card. A plan has no answer keys.
  // Not while you type, and not right after a card appears.
  const cardKey = card ? `${card.pane.id} ${card.ask.since}` : ''
  const shown = useRef(0)
  useEffect(() => { shown.current = Date.now() }, [cardKey])
  const keys = useRef<(event: KeyboardEvent) => void>(() => {})
  useEffect(() => {
    keys.current = event => {
      if (!card || busy || event.repeat || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || isTyping(event.target as HTMLElement | null)) return
      const action = deckKey(event.code, item)
      if (!action) return
      event.preventDefault()
      if (action === 'next') { if (live.length > 1) next() }
      else if (Date.now() - shown.current >= SETTLE) answer(card.pane, action)
    }
  })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => keys.current(event)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (!card) {
    return <dialog {...modal} className="sheet action-deck" aria-labelledby="deck-title">
      <div className="sheet-heading deck-head"><h2 id="deck-title">Waiting on you</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button></div>
      <div className="deck-empty" role="status"><Check size={22} aria-hidden="true" /><p>{status || 'Nothing waits on you.'}</p></div>
      <div className="deck-actions"><button type="button" className="primary" onClick={onClose}>Done</button></div>
    </dialog>
  }

  const { pane, ask } = card
  const name = title(pane)
  const send = (text: string) => void run(pane, () => sendToTab(api, pane, text), `Sent to ${name}.`)
  const handled = () => void run(pane, () => askAction(api, pane, 'handled'), `${name}: handled.`)
  const text = ask.kind === 'reply' ? '' : askText(pane)
  const answers = hasAnswers(item)
  const plans = planOptions(pane, item)
  const choosePlan = (option: Choice) => { if (!busy) void run(pane, () => sendPlanChoice(api, pane, option), `${answerName(item!, option)} sent to ${name}.`) }

  return <dialog {...modal} className={ask.urgent ? 'sheet action-deck has-list urgent' : 'sheet action-deck has-list'} aria-labelledby="deck-title">
    <div className="sheet-heading deck-head">
      <h2 id="deck-title">Waiting on you <span className="deck-count">{at + 1} of {live.length}</span></h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button>
    </div>
    <div className="deck-body">
      <ul className="deck-list" aria-label="Asks that wait on you">
        {live.map((row, position) => {
          const session = sessions.find(found => found.id === row.pane.tmux_session)
          const rowName = title(row.pane)
          const source = [agentNames[row.pane.agent], session && session.name !== rowName ? session.name : ''].filter(Boolean).join(' · ')
          return <li key={row.pane.id}>
            <button type="button" className="deck-row" aria-current={position === at ? 'true' : undefined} disabled={busy} onClick={() => { setStatus(''); setIndex(position) }}>
              <span className="deck-row-top">
                {row.ask.urgent && <CircleAlert size={13} className="deck-row-urgent" aria-label="Urgent" />}
                <span className="deck-row-kind">{KIND[row.ask.kind] ?? 'Waits on you'}</span>
                <span className="deck-row-age">{age(row.ask.since, now)}</span>
              </span>
              <span className="deck-row-title">{rowName}</span>
              <span className="deck-row-source">{source}</span>
            </button>
          </li>
        })}
      </ul>
      <div className="deck-main">
        <article className="deck-card" aria-live="polite">
          <p className="deck-meta">
            {ask.urgent && <span className="deck-urgent"><CircleAlert size={14} aria-hidden="true" />Urgent</span>}
            <span>{KIND[ask.kind] ?? 'Waits on you'}</span><span aria-hidden="true">·</span><span>{agentNames[pane.agent]}</span><span aria-hidden="true">·</span><span>waited {age(ask.since, now)}</span>
          </p>
          <h3 className="deck-tab">{name}</h3>
          <p className="deck-question">{ask.next || 'Open the tab to see what it needs.'}</p>
          {text && text !== ask.next && <pre className="deck-text" tabIndex={0} aria-label={`What ${name} asks`}>
            {askLines(text, item?.kind ?? ask.kind).map((line, row) => <span key={row} className={line.mark || undefined}>{line.text}{'\n'}</span>)}
          </pre>}
          {ask.kind === 'reply'
            ? <ReplyBox key={cardKey} name={name} busy={busy} onSend={send} />
            : plans.length
              ? <div className="inbox-actions deck-plan" role="group" aria-label={`Answer ${name}`}>
                {plans.map(option => <button type="button" key={option.id} disabled={busy} onClick={() => choosePlan(option)}>{option.text}</button>)}
              </div>
              : answers && item
              ? <AnswerActions api={api} item={item} label={name} waiting={busy} onAnswer={(_, option) => answer(pane, option)} onFail={(_, error) => setStatus(errorText(error))} />
              : <p className="deck-hint">This prompt waits in the tab. Open the tab to answer it.</p>}
        </article>
        {status && <p className="deck-status" role="status">{status}</p>}
        <div className="deck-actions">
          <button type="button" className="secondary" disabled={busy} onClick={() => { onClose(); onOpen(pane) }}><ExternalLink size={15} aria-hidden="true" />Open tab</button>
          <button type="button" className="secondary" disabled={busy} onClick={handled}><Check size={15} aria-hidden="true" />Handled</button>
          <button type="button" className="secondary" disabled={busy || live.length < 2} onClick={next}>Skip<kbd>J</kbd><ArrowRight size={15} aria-hidden="true" /></button>
        </div>
      </div>
    </div>
  </dialog>
}

/** The answer box. Keyed by the ask, so each card starts empty. Ctrl/Cmd+Enter sends. */
function ReplyBox({ name, busy, onSend }: { name: string; busy: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState('')
  const box = useRef<HTMLTextAreaElement>(null)
  // The dialog's showModal() runs after this mounts and moves focus to the first button; take it back a frame later.
  useEffect(() => { const frame = requestAnimationFrame(() => box.current?.focus()); return () => cancelAnimationFrame(frame) }, [])
  const send = () => { if (text.trim()) onSend(text.trim()) }
  return <form className="deck-reply" onSubmit={event => { event.preventDefault(); send() }}>
    <label htmlFor="deck-text" className="visually-hidden">Your answer to {name}</label>
    <textarea ref={box} id="deck-text" rows={3} value={text} disabled={busy} placeholder="Your answer"
      onChange={event => setText(event.target.value)}
      onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send() } }} />
    <button type="submit" className="primary" disabled={busy || !text.trim()}><Send size={15} aria-hidden="true" />Send</button>
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
