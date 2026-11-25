import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'

// The Undo toast after a Codex or autonomy level change (B56). It looks and times out like the page's undo toast
// (Undo.tsx), but it stays while it offers Undo, and it renders inside the open sheet: the Settings sheet is a modal dialog, and the page toast sits
// under it. Undo sends the old level again through the same route with {undo: true}, so a raise still asks for the
// passkey (withApproval) and the activity log records the undo. One sheet shows one toast: LevelUndoProvider holds it, so
// a new change replaces the last offer, and the toast stays when the setting that made it leaves the screen.
const TOAST_MS = 8000
/** `run` sends the undo. It gives null when the undo is done, or the reason it failed. */
type OfferFn = (text: string, undone: string, run: () => Promise<string | null>) => void
interface Toast { offer: OfferFn; busy: boolean }
const Shared = createContext<Toast | null>(null)
interface Offer { text: string; undone?: string; run?: () => Promise<string | null>; failed?: boolean; at: number }

function useUndoToast(): Toast & { toast: ReactNode } {
  const [shown, setShown] = useState<Offer | null>(null)
  const [held, setHeld] = useState(false)
  // The offer whose Undo is on its way. The level controls in the sheet stay off until it ends, so no new change
  // can cross it, and its result goes only to its own toast.
  const [busy, setBusy] = useState<Offer | null>(null)
  // A toast with an Undo button, or one that says the undo failed, stays until it is dismissed or replaced (WCAG 2.2.1).
  // The "Undid" note has no action, so it goes after 8 s, or later while the pointer or focus is on it.
  useEffect(() => {
    if (!shown || shown.run || shown.failed || held) return
    const timer = setTimeout(() => setShown(current => current === shown ? null : current), TOAST_MS)
    return () => clearTimeout(timer)
  }, [shown, held])
  async function undo(offer: Offer) {
    if (!offer.run || busy) return
    setBusy(offer)
    let failure: string | null
    try { failure = await offer.run() } catch (error) { failure = error instanceof Error ? error.message : String(error) }
    const result: Offer = failure === null ? { text: offer.undone ?? 'Undone', at: Date.now() } : { text: `Could not undo. ${failure}`, failed: true, at: Date.now() }
    // Dismissed or replaced while the undo ran: leave the newer toast alone.
    setShown(current => current === offer ? result : current)
    setBusy(null); setHeld(false)
  }
  const toast = <div className="undo-toast-region" role="status" aria-live="polite" aria-atomic="true">
    {shown && <div key={shown.at} className={shown.failed ? 'toast undo-toast undo-toast-failed' : 'toast undo-toast'} onPointerEnter={() => setHeld(true)} onPointerLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setHeld(false) }}>
      <span className="undo-toast-text" role={shown.failed ? 'alert' : undefined}>{shown.text}</span>
      {shown.run && <button type="button" className="undo-toast-action" disabled={!!busy} onClick={() => void undo(shown)}>{busy === shown ? 'Undoing…' : 'Undo'}</button>}
      <button type="button" className="icon-button undo-toast-close" aria-label="Dismiss" onClick={() => { setHeld(false); setShown(null) }}><X size={16} aria-hidden="true" /></button>
    </div>}
  </div>
  return { offer: (text, undone, run) => { if (!busy) setShown({ text, undone, run, at: Date.now() }) }, busy: !!busy, toast }
}

/** One Undo toast for everything inside: wrap a sheet's content in it. */
export function LevelUndoProvider({ children }: { children: ReactNode }) {
  const { offer, busy, toast } = useUndoToast()
  return <Shared.Provider value={{ offer, busy }}>{children}{toast}</Shared.Provider>
}

/** The sheet's shared toast; a setting outside a provider gets its own. `busy` is true while an Undo runs. */
export function useLevelUndo(): Toast & { toast: ReactNode } {
  const shared = useContext(Shared)
  const own = useUndoToast()
  return shared ? { ...shared, toast: null } : own
}
