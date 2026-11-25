import { RotateCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Spinner } from './Skeleton'
import './Retry.css'
import { fmtTime, useTetherZone } from './zone'

// One Retry for the whole UI. A press must always show that something happened:
// 1. The button sinks at once (:active).
// 2. While the retry runs: a spinner, "Trying again…", disabled. It runs at least MIN_BUSY, so a fast failure still reads as a try.
// 3. The host hides the error when the retry works, and this button goes with it.
//    If the button is still here after a try, the retry failed too: a short note says so, with the time.
// `onRetry` may return a promise (it is awaited). A host that reloads by state (an attempt counter) passes `busy` instead.

export const MIN_BUSY = 600

export function clock(at: number) {
  return fmtTime(at, { hour: 'numeric', minute: '2-digit', second: '2-digit' })
}

export function RetryButton({ onRetry, busy, label = 'Try again', className = 'link-button', note = true }: {
  onRetry: () => unknown
  busy?: boolean
  label?: string
  className?: string
  note?: boolean
}) {
  useTetherZone()
  const [running, setRunning] = useState(false)
  const [failedAt, setFailedAt] = useState<number | null>(null)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  // A host-driven try: busy went from true to false and the error is still here.
  const wasBusy = useRef(!!busy)
  useEffect(() => {
    if (wasBusy.current && !busy) setFailedAt(Date.now())
    wasBusy.current = !!busy
  }, [busy])
  const shown = running || !!busy
  const run = async () => {
    if (shown) return
    setRunning(true)
    const started = Date.now()
    try { await onRetry() } catch { /* The host shows its own error. */ }
    const rest = MIN_BUSY - (Date.now() - started)
    if (rest > 0) await new Promise(done => setTimeout(done, rest))
    if (!alive.current) return
    setRunning(false)
    if (busy === undefined) setFailedAt(Date.now())
  }
  return <span className="retry">
    <button type="button" className={`retry-button ${className}`} disabled={shown} aria-busy={shown} onClick={() => void run()}>
      {shown ? <Spinner /> : <RotateCw size={14} aria-hidden="true" />}{shown ? 'Trying again…' : label}
    </button>
    {note && failedAt !== null && !shown && <span className="retry-note" role="status">Still failing. Tried at {clock(failedAt)}.</span>}
  </span>
}
