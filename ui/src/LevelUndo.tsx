import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'

// The Undo toast after a Codex or autonomy level change (B56). It looks and times out like the page's undo toast
// (Undo.tsx), but it stays while it offers Undo, and it renders inside the open sheet: the Settings sheet is a modal dialog, and the page toast sits
// under it. Undo sends the old level again through the same route with {undo: true}, so a raise still asks for the
// passkey (withApproval) and the activity log records the undo.
const TOAST_MS = 8000
interface Offer { text: string; undone?: string; run?: () => Promise<boolean>; at: number }

export function useLevelUndo(): { offer: (text: string, undone: string, run: () => Promise<boolean>) => void; toast: ReactNode } {
  const [shown, setShown] = useState<Offer | null>(null)
  const [held, setHeld] = useState(false)
  const [busy, setBusy] = useState(false)
  // A toast with an Undo button stays until it is dismissed or replaced (WCAG 2.2.1: no time limit on the action).
  // The "Undid" note has no action, so it goes after 8 s, or later while the pointer or focus is on it.
  useEffect(() => {
    if (!shown || shown.run || held || busy) return
    const timer = setTimeout(() => setShown(null), TOAST_MS)
    return () => clearTimeout(timer)
  }, [shown, held, busy])
  async function undo(offer: Offer) {
    if (!offer.run) return
    setBusy(true)
    try { if (await offer.run()) setShown({ text: offer.undone ?? 'Undone', at: Date.now() }); else setShown(null) }
    finally { setBusy(false); setHeld(false) }
  }
  const toast = <div className="undo-toast-region" role="status" aria-live="polite" aria-atomic="true">
    {shown && <div key={shown.at} className="toast undo-toast" onPointerEnter={() => setHeld(true)} onPointerLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setHeld(false) }}>
      <span className="undo-toast-text">{shown.text}</span>
      {shown.run && <button type="button" className="undo-toast-action" disabled={busy} onClick={() => void undo(shown)}>Undo</button>}
      <button type="button" className="icon-button undo-toast-close" aria-label="Dismiss" onClick={() => { setHeld(false); setShown(null) }}><X size={16} aria-hidden="true" /></button>
    </div>}
  </div>
  return { offer: (text, undone, run) => setShown({ text, undone, run, at: Date.now() }), toast }
}
