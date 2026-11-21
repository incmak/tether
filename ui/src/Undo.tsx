import { useCallback, useEffect, useRef, useState } from 'react'
import { History, Redo2, Undo2, X } from 'lucide-react'
import { UNDO_EVENT, errorText } from './api'
import type { Api, RestartOffer, SessionsData, UndoStep } from './api'
import { useModal } from './hooks'
import { IS_MAC } from './keys'
import { PREFS_PULL_EVENT } from './prefs'
import { UNDO_RUN_EVENT, setUndoNext } from './undoState'

/** Undo and redo of Tether steps (server/undo.py): tab, session and stack changes, from this page, another device or an agent.
 *  ⌘Z / ⌘⇧Z (Ctrl off a Mac) act on the newest step, but never in a text field, the composer, the terminal or an open sheet:
 *  there the keys edit text. A toast after each step offers Undo; History lists older steps. Steps that start or stop an
 *  agent, or touch another session, ask first in a dialog that names the effect. */

type Direction = 'undo' | 'redo'
interface Done { status: 'done'; id: string; label: string; direction: Direction; warnings?: string[] }
interface Ask { status: 'confirm'; id: string; label: string; direction: Direction; effects: string[]; token: string; changed?: boolean }
// A real chat id (server/terminal_input.py UUID); a new chat has none until its first message.
const CHAT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export interface UndoEntry {
  id: string; at: number; who: { by: 'user' | 'tether' | 'agent'; tab?: string }; label: string
  state: 'done' | 'undone'; redo: boolean; undone_at: number | null; why?: string | null
}
interface HistoryData { entries: UndoEntry[]; next_undo: string | null; next_redo: string | null; next_undo_label: string | null; next_redo_label: string | null }
interface Action { label: string; run: () => void }
/** restart: the toast asks for a tab restart. It stays until the person acts or dismisses it, and lays out on two rows. */
interface Toast { text: string; action?: Action; second?: Action; at: number; restart?: boolean }
interface Restarted { started: boolean; warning?: string }

/** The toast text after a move to a session with another project (spec-move-chats §5). */
function restartText(offer: RestartOffer) {
  return `Moved to ${offer.session}. Restart this tab to load project ${offer.name}.`
}

/** The direction for a key press, or null when the key belongs to text, the terminal or a sheet. */
export function undoKey(event: KeyboardEvent): Direction | null {
  if (event.altKey || event.code !== 'KeyZ') return null
  if (!(IS_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey)) return null
  const target = event.target instanceof Element ? event.target : null
  if (target && ((target as HTMLElement).isContentEditable || target.closest('input, textarea, select, [contenteditable], .xterm, .composer, dialog, [role="dialog"], [role="alertdialog"]'))) return null
  // A modal owns the keys: an open <dialog>, a confirm box (alertdialog) or a dialog marked aria-modal. A non-modal one
  // (the browser window) only owns them while focus is inside it (above).
  if (document.querySelector('dialog[open], [role="alertdialog"], [role="dialog"][aria-modal="true"]')) return null
  return event.shiftKey ? 'redo' : 'undo'
}

const TOAST_MS = 8000
const whoText = (who: UndoEntry['who']) => who.by === 'user' ? 'You' : who.by === 'agent' ? (who.tab ? `Agent ${who.tab}` : 'An agent') : 'Tether'
const timeText = (at: number) => {
  const minutes = Math.round((Date.now() / 1000 - at) / 60)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  return new Date(at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** Give focus back to the control that had it before a sheet opened (the sheets unmount without dialog.close()). */
function useReturnFocus() {
  const [opener] = useState(() => document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null)
  useEffect(() => () => { requestAnimationFrame(() => {
    if (document.activeElement && document.activeElement !== document.body) return
    if (opener?.isConnected && !opener.closest('[inert]')) opener.focus()
  }) }, [opener])
}

function Confirm({ ask, busy, onYes, onClose }: { ask: Ask; busy: boolean; onYes: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  useReturnFocus()
  const verb = ask.direction === 'undo' ? 'Undo' : 'Redo'
  return <dialog {...modal} className="sheet undo-confirm" role="alertdialog" aria-labelledby="undo-confirm-title" aria-describedby={ask.changed ? 'undo-confirm-changed undo-confirm-effects' : 'undo-confirm-effects'}>
    <div className="sheet-heading"><h2 id="undo-confirm-title">{verb} “{ask.label}”?</h2></div>
    {ask.changed && <p id="undo-confirm-changed" className="hint undo-changed">What this does changed since you looked. Check it again.</p>}
    <ul id="undo-confirm-effects" className="undo-effects">{ask.effects.map(line => <li key={line}>{line}</li>)}</ul>
    <div className="sheet-buttons">
      <button type="button" className="secondary" autoFocus disabled={busy} onClick={onClose}>Cancel</button>
      <button type="button" className="primary" disabled={busy} onClick={onYes}>{busy ? `${verb === 'Undo' ? 'Undoing' : 'Redoing'}…` : verb}</button>
    </div>
  </dialog>
}

function HistorySheet({ api, busy, onRun, onClose, version }: { api: Api; busy: boolean; onRun: (direction: Direction, id: string) => void; onClose: () => void; version: number }) {
  const modal = useModal(true, onClose)
  useReturnFocus()
  const [data, setData] = useState<HistoryData | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const stop = new AbortController()
    api<HistoryData>('undo', undefined, stop.signal).then(found => { setData(found); setError('') }, failure => { if (!stop.signal.aborted) setError(errorText(failure)) })
    return () => stop.abort()
  }, [api, version])
  return <dialog {...modal} className="sheet undo-history" aria-labelledby="undo-history-title">
    <div className="sheet-heading"><h2 id="undo-history-title">Undo history</h2><button type="button" className="icon-button" aria-label="Close" autoFocus onClick={onClose}><X /></button></div>
    <p className="hint">Tab, session and stack steps from the last 24 hours, newest first. {IS_MAC ? '⌘Z undoes and ⌘⇧Z redoes' : 'Ctrl+Z undoes and Ctrl+Shift+Z redoes'} the newest one.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {!data && !error && <p className="hint" role="status">Loading…</p>}
    {data && !data.entries.length && <p className="hint" role="status">No steps yet. Moving, closing or adding a tab shows here.</p>}
    {data && data.entries.length > 0 && <ol className="undo-list">{data.entries.map(entry => {
      const direction: Direction | null = entry.state === 'done' ? 'undo' : entry.redo ? 'redo' : null
      return <li key={entry.id} className={entry.state === 'undone' ? 'undone' : undefined}>
        <div className="undo-row-text">
          <span className="undo-row-label">{entry.label}</span>
          <small>{whoText(entry.who)} · {timeText(entry.at)}{entry.state === 'undone' ? ' · undone' : ''}</small>
          {direction && entry.why && <small className="undo-why">{entry.why}</small>}
        </div>
        {direction && <button type="button" className="secondary" disabled={busy || !!entry.why}
          aria-label={`${direction === 'undo' ? 'Undo' : 'Redo'} ${entry.label}`} onClick={() => onRun(direction, entry.id)}>
          {direction === 'undo' ? <Undo2 size={15} aria-hidden="true" /> : <Redo2 size={15} aria-hidden="true" />}{direction === 'undo' ? 'Undo' : 'Redo'}
        </button>}
      </li>
    })}</ol>}
  </dialog>
}

/** The toast, the keys, the confirm dialog and the History sheet. onChanged: reload the session list after a step ran. */
export function UndoCenter({ api, onChanged, history, onHistory }: { api: Api; onChanged: () => void; history: boolean; onHistory: (open: boolean) => void }) {
  const [toast, setToast] = useState<Toast | null>(null)
  const [ask, setAsk] = useState<Ask | null>(null)
  const [busy, setBusy] = useState(false)
  const [version, setVersion] = useState(0)
  const running = useRef(false)
  // The toast's Redo or Undo runs the same function again; a ref, because run cannot name itself.
  const again = useRef<(direction: Direction, id?: string) => Promise<void>>(async () => {})
  const latest = useRef({ api, onChanged })
  useEffect(() => { latest.current = { api, onChanged } })

  /** confirmed: the token of the confirm answer the person said yes to. The server asks again when the effects changed. */
  const run = useCallback(async (direction: Direction, id?: string, confirmed?: string) => {
    if (running.current) return
    running.current = true
    setBusy(true)
    try {
      const result = await latest.current.api<Done | Ask>('undo', { action: direction, ...(id ? { id } : {}), ...(confirmed ? { confirmed } : {}) })
      if (result.status === 'confirm') { setAsk({ ...result, changed: confirmed !== undefined }); return }
      setAsk(null)
      const back: Direction = direction === 'undo' ? 'redo' : 'undo'
      // A step that ran with a leftover (an extra shell tab tmux did not close) says so in the toast.
      const warned = result.warnings?.length ? `. ${result.warnings.join(' ')}` : ''
      setToast({ text: `${direction === 'undo' ? 'Undid' : 'Redid'}: ${result.label}${warned}`, at: Date.now(),
        action: { label: back === 'redo' ? 'Redo' : 'Undo', run: () => void again.current(back, result.id) } })
      latest.current.onChanged()
      window.dispatchEvent(new Event(PREFS_PULL_EVENT))
    } catch (error) {
      setAsk(null)
      setToast({ text: errorText(error), at: Date.now() })
    } finally {
      running.current = false
      setBusy(false)
      setVersion(value => value + 1)
    }
  }, [])
  useEffect(() => { again.current = run }, [run])

  const retry = useRef<(offer: RestartOffer) => Promise<void>>(async () => {})
  const restart = useCallback(async (offer: RestartOffer) => {
    if (running.current) return
    running.current = true
    setBusy(true)
    const again = { label: 'Restart', run: () => void retry.current(offer) }
    try {
      // A new chat gets its id with its first message, after the move: read the tab's chat now. Until then
      // the session list holds a "pending-…" placeholder, which is not a chat id.
      let thread = offer.thread
      if (!thread || !CHAT_ID.test(thread)) {
        const tab = (await latest.current.api<SessionsData>('sessions')).sessions.find(item => item.id === offer.pane)
        if (!tab) { setToast({ text: 'That tab has closed.', at: Date.now() }); return }
        thread = tab.thread
      }
      if (!thread || !CHAT_ID.test(thread)) {
        setToast({ text: `Send this tab's first message, then restart it to load project ${offer.name}.`, at: Date.now(), action: again, restart: true })
        return
      }
      const result = await latest.current.api<Restarted>('restart-tab', { pane: offer.pane, thread, project: offer.project, session: offer.session },
        undefined, undefined, 20000)
      setToast({ text: result.started ? `Restarted. The tab now loads project ${offer.name}.` : result.warning || 'The tab restarted. Open it to check.', at: Date.now() })
      latest.current.onChanged()
    } catch (error) {
      // A refusal (busy agent, unsent draft) keeps the offer, so the person can try again once the tab is ready.
      setToast({ text: errorText(error), at: Date.now(), action: again, restart: true })
    } finally {
      running.current = false
      setBusy(false)
    }
  }, [])
  useEffect(() => { retry.current = restart }, [restart])

  // A step this page (or a request it made) recorded: offer Undo for it.
  useEffect(() => {
    const onStep = (event: Event) => {
      const step = (event as CustomEvent<UndoStep>).detail
      const undo = step.id ? { label: 'Undo', run: () => void run('undo', step.id) } : undefined
      const offer = step.restart
      if (offer) {
        setToast({ text: restartText(offer), at: Date.now(), action: { label: 'Restart', run: () => void restart(offer) }, second: undo, restart: true })
      } else if (undo) setToast({ text: step.label, at: Date.now(), action: undo })
      setVersion(value => value + 1)
    }
    window.addEventListener(UNDO_EVENT, onStep)
    return () => window.removeEventListener(UNDO_EVENT, onStep)
  }, [run, restart])

  // The Undo and Redo buttons (Rail, the chat header's ⋯ menu) and the command menu send their press here.
  useEffect(() => {
    const onRun = (event: Event) => { const direction = (event as CustomEvent<Direction>).detail; if (direction === 'undo' || direction === 'redo') void run(direction) }
    window.addEventListener(UNDO_RUN_EVENT, onRun)
    return () => window.removeEventListener(UNDO_RUN_EVENT, onRun)
  }, [run])

  // What the buttons would act on. Agents and other devices add steps too, so read it again now and then.
  useEffect(() => {
    let stop = new AbortController()
    const read = () => {
      stop.abort()
      stop = new AbortController()
      latest.current.api<HistoryData>('undo?brief=1', undefined, stop.signal, undefined, 10000, 'low')
        .then(found => setUndoNext({ undo: found.next_undo_label, redo: found.next_redo_label }), () => { /* offline: keep the last state */ })
    }
    read()
    const visible = () => { if (document.visibilityState === 'visible') read() }
    const every = window.setInterval(visible, 20000)
    document.addEventListener('visibilitychange', visible)
    window.addEventListener('focus', visible)
    return () => { stop.abort(); window.clearInterval(every); document.removeEventListener('visibilitychange', visible); window.removeEventListener('focus', visible) }
  }, [version])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      const direction = undoKey(event)
      if (!direction) return
      event.preventDefault()
      void run(direction)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [run])

  // The toast stays while the pointer or focus is on it (WCAG 2.2.1: enough time to reach its button).
  const [held, setHeld] = useState(false)
  useEffect(() => {
    if (!toast || held || toast.restart) return
    const timer = setTimeout(() => setToast(null), TOAST_MS)
    return () => clearTimeout(timer)
  }, [toast, held])

  return <>
    <div className="undo-toast-region" role="status" aria-live="polite" aria-atomic="true">
      {toast && <div key={toast.at} className={toast.restart ? 'toast undo-toast undo-toast-restart' : 'toast undo-toast'} onPointerEnter={() => setHeld(true)} onPointerLeave={() => setHeld(false)}
        onFocus={() => setHeld(true)} onBlur={() => setHeld(false)}>
        <span className="undo-toast-text">{toast.text}</span>
        {toast.action && <button type="button" className="undo-toast-action" disabled={busy} onClick={() => { const action = toast.action!; setHeld(false); action.run() }}>{busy && toast.action.label === 'Restart' ? 'Restarting…' : toast.action.label}</button>}
        {toast.second && <button type="button" className="undo-toast-action" disabled={busy} onClick={() => { const action = toast.second!; setHeld(false); action.run() }}>{toast.second.label}</button>}
        <button type="button" className="undo-toast-more" aria-label="Undo history" title="Undo history" onClick={() => { setHeld(false); setToast(null); onHistory(true) }}><History size={16} aria-hidden="true" /></button>
        <button type="button" className="icon-button undo-toast-close" aria-label="Dismiss" onClick={() => { setHeld(false); setToast(null) }}><X size={16} aria-hidden="true" /></button>
      </div>}
    </div>
    {ask && <Confirm ask={ask} busy={busy} onClose={() => setAsk(null)} onYes={() => void run(ask.direction, ask.id, ask.token)} />}
    {history && !ask && <HistorySheet api={api} busy={busy} version={version} onClose={() => onHistory(false)} onRun={(direction, id) => void run(direction, id)} />}
  </>
}
