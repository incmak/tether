// Thumbs up and thumbs down under an agent's reply, saved by the server (server/feedback.py).
// Thumbs down opens "What went wrong?": the note is saved, and "Tell the agent" also sends it to the tab as a message,
// so the agent can fix the reply and keep the lesson.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { ThumbsDown, ThumbsUp, X } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'

export type Rating = 'up' | 'down'
interface Saved { rating: Rating; note: string }
interface Feedback {
  ratings: Record<string, Saved>
  rate: (message: string, rating: Rating | '', note?: string, excerpt?: string) => Promise<void>
  /** Sends a message to the agent; false when the tab does not take messages now. */
  tell?: (text: string) => Promise<boolean>
}

const FeedbackContext = createContext<Feedback | null>(null)
export const ReplyFeedbackProvider = FeedbackContext.Provider
const REASONS = ['Not correct', 'Did not follow my request', 'Too long', 'Unsafe or risky']
const EXCERPT = 200

/** Loads and saves the ratings of one conversation. No thread (a new tab, a shell): no ratings.
 *  `tell(text, thread)` must refuse when `thread` is no longer the open conversation: a slow save must not send the note elsewhere. */
export function useReplyFeedback(api: Api, thread: string | null | undefined, tell?: (text: string, thread: string) => Promise<boolean>): Feedback | null {
  // The ratings carry their thread, so a late load or rollback for another conversation changes nothing.
  const [state, setState] = useState<{ thread: string | null; ratings: Record<string, Saved> }>({ thread: null, ratings: {} })
  // Per reply ("thread\nmessage"): the newest change, the server-confirmed rating, and the queue that sends saves in order.
  const latest = useRef(new Map<string, number>())
  const confirmed = useRef(new Map<string, Saved | undefined>())
  const queue = useRef(new Map<string, Promise<unknown>>())
  useEffect(() => {
    if (!thread) return
    const control = new AbortController()
    api<{ ratings: Record<string, Saved> }>(`feedback?thread=${encodeURIComponent(thread)}`, undefined, control.signal)
      .then(result => {
        const ratings = result.ratings ?? {}
        for (const key of [...confirmed.current.keys()]) if (key.startsWith(`${thread}\n`)) confirmed.current.delete(key)
        for (const [message, saved] of Object.entries(ratings)) confirmed.current.set(`${thread}\n${message}`, saved)
        setState({ thread, ratings })
      }, () => { if (!control.signal.aborted) setState({ thread, ratings: {} }) })
    return () => control.abort()
  }, [api, thread])
  const rate = useCallback(async (message: string, rating: Rating | '', note = '', excerpt = '') => {
    if (!thread) return
    const key = `${thread}\n${message}`, token = (latest.current.get(key) ?? 0) + 1
    latest.current.set(key, token)
    const show = (saved: Saved | undefined) => setState(current => {
      if (current.thread !== thread) return current
      const ratings = { ...current.ratings }
      if (saved) ratings[message] = saved; else delete ratings[message]
      return { thread, ratings }
    })
    show(rating ? { rating, note } : undefined)
    const send = (queue.current.get(key) ?? Promise.resolve()).catch(() => {}).then(() => api('feedback', { thread, message, rating, note, excerpt: excerpt.slice(0, EXCERPT) }))
    queue.current.set(key, send)
    try {
      await send
      confirmed.current.set(key, rating ? { rating, note } : undefined)
    } catch (error) {
      // Show what the server holds, unless a newer change to this reply is still on its way.
      if (latest.current.get(key) === token) show(confirmed.current.get(key))
      throw error
    } finally { if (queue.current.get(key) === send) queue.current.delete(key) }
  }, [api, thread])
  const tellHere = useMemo(() => tell && thread ? (text: string) => tell(text, thread) : undefined, [tell, thread])
  const ratings = state.ratings
  return useMemo(() => thread && state.thread === thread ? { ratings, rate, tell: tellHere } : null, [thread, state.thread, ratings, rate, tellHere])
}

export function RateButtons({ id, text }: { id: string; text: string }) {
  const feedback = useContext(FeedbackContext)
  // The open sheet keeps the conversation it was opened for, even if the tab moves to another one meanwhile.
  const [asking, setAsking] = useState<Feedback | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { if (!error) return; const timer = setTimeout(() => setError(''), 4000); return () => clearTimeout(timer) }, [error])
  if (!feedback && !asking) return null
  const current = feedback?.ratings[id]?.rating
  const set = (rating: Rating | '') => feedback?.rate(id, rating, '', text).catch(failure => setError(`Not saved: ${errorText(failure)}`))
  // One tree shape in every state: the open sheet must not remount (and lose its state) when the buttons go.
  return <>
    {feedback && <>
      <button type="button" className="icon-button" aria-label="Good response" title="Good response" aria-pressed={current === 'up'} data-rated={current === 'up' || undefined}
        onClick={() => void set(current === 'up' ? '' : 'up')}><ThumbsUp size={16} fill={current === 'up' ? 'currentColor' : 'none'} /></button>
      <button type="button" className="icon-button" aria-label="Bad response" title="Bad response" aria-pressed={current === 'down'} data-rated={current === 'down' || undefined}
        onClick={() => { if (current === 'down') void set(''); else setAsking(feedback) }}><ThumbsDown size={16} fill={current === 'down' ? 'currentColor' : 'none'} /></button>
      {error && <span className="rate-error" role="alert">{error}</span>}
    </>}
    {asking && <WhatWentWrong id={id} text={text} feedback={asking} onClose={() => setAsking(null)} />}
  </>
}

function WhatWentWrong({ id, text, feedback, onClose }: { id: string; text: string; feedback: Feedback; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [reasons, setReasons] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<'' | 'save' | 'tell'>('')
  const [error, setError] = useState('')
  const summary = [reasons.join(', '), note.trim()].filter(Boolean).join('. ')
  async function submit(tell: boolean) {
    setBusy(tell ? 'tell' : 'save'); setError('')
    try {
      await feedback.rate(id, 'down', summary, text)
      if (tell && feedback.tell) {
        const quote = text.replace(/\s+/g, ' ').trim().slice(0, EXCERPT)
        const sent = await feedback.tell(`Feedback on your reply that starts "${quote}${text.length > EXCERPT ? '…' : ''}": ${summary || 'This reply was not good.'} Please fix it.`)
        if (!sent) { setError('The rating is saved, but the message was not sent. Send it from the message box.'); setBusy(''); return }
      }
      onClose()
    } catch (failure) { setError(errorText(failure)); setBusy('') }
  }
  return <dialog {...modal} className="sheet feedback-sheet" aria-labelledby="feedback-title">
    <div className="sheet-heading"><h2 id="feedback-title">What went wrong?</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <div className="feedback-reasons" role="group" aria-label="Reasons">
      {REASONS.map(reason => <button key={reason} type="button" className="feedback-chip" aria-pressed={reasons.includes(reason)} disabled={!!busy}
        onClick={() => setReasons(current => current.includes(reason) ? current.filter(item => item !== reason) : [...current, reason])}>{reason}</button>)}
    </div>
    <label htmlFor="feedback-note" className="feedback-label">Details (optional)</label>
    <textarea id="feedback-note" rows={3} maxLength={1500} value={note} disabled={!!busy} placeholder="What should the reply have said or done?" onChange={event => setNote(event.target.value)} />
    {error && <p className="error" role="alert">{error}</p>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" disabled={!!busy} onClick={() => void submit(false)}>{busy === 'save' ? 'Saving…' : 'Save only'}</button>
      {feedback.tell && <button type="button" className="primary" disabled={!!busy} onClick={() => void submit(true)}>{busy === 'tell' ? 'Sending…' : 'Tell the agent'}</button>}
    </div>
  </dialog>
}
