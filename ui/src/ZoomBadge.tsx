import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ZoomIn } from 'lucide-react'
import './ZoomBadge.css'

// Shows the page zoom while a pinch has left the page zoomed in, so a cut-off edge has a visible cause.
// position: fixed sticks to the layout viewport, which a pinch zoom does not move. The badge follows the
// visual viewport instead and scales by 1/zoom, so it stays in the bottom right corner at its normal size.
const SHOWN_FROM = 1.01
const META = 'meta[name="viewport"]'

const zoomPercent = (scale: number) => Math.round(scale * 100)

// Pans and the keyboard move the visual viewport without a new zoom, so each viewport event places the badge again.
function place(el: HTMLElement | null) {
  const view = window.visualViewport
  if (!view || !el || !el.matches(':popover-open')) return
  el.style.transform = `translate(${view.offsetLeft + view.width}px, ${view.offsetTop + view.height}px) scale(${1 / view.scale}) translate(-100%, -100%) translate(var(--zoom-badge-dx), var(--zoom-badge-dy))`
}

export function ZoomBadge() {
  const [scale, setScale] = useState(() => window.visualViewport?.scale ?? 1)
  const [stuckAt, setStuckAt] = useState(0)  // the zoom at which the browser ignored a reset (iOS Safari, Firefox, forced zoom)
  const badge = useRef<HTMLButtonElement>(null)
  const zoomed = scale >= SHOWN_FROM
  const stuck = zoomed && stuckAt === scale  // a new pinch offers the reset again

  const restore = useRef<(() => void) | null>(null)  // a pending reset: puts the original viewport meta back

  useEffect(() => {
    const view = window.visualViewport
    if (!view) return
    let frame = 0
    const read = () => { frame = 0; setScale(view.scale); place(badge.current) }
    const queue = () => { if (!frame) frame = requestAnimationFrame(read) }
    view.addEventListener('resize', queue)
    view.addEventListener('scroll', queue)
    return () => {
      cancelAnimationFrame(frame); view.removeEventListener('resize', queue); view.removeEventListener('scroll', queue)
      restore.current?.()
    }
  }, [])

  useLayoutEffect(() => {
    const view = window.visualViewport, el = badge.current
    if (!view || !el) return
    if (!zoomed) { if (el.matches(':popover-open')) el.hidePopover(); return }
    // The top layer keeps the badge above open sheets and dialogs.
    if (!el.matches(':popover-open')) el.showPopover()
    place(el)
  })

  // Chromium clamps the zoom when the viewport meta gains maximum-scale=1. Restore the meta after that,
  // so pinch zoom keeps working.
  // A second tap while a reset runs does nothing, so the clamp can never become the saved original.
  const reset = () => {
    const meta = document.querySelector<HTMLMetaElement>(META)
    const view = window.visualViewport
    if (!meta || !view || restore.current) return
    const original = meta.content
    const timer = window.setTimeout(() => {
      restore.current?.()
      if (view.scale >= SHOWN_FROM) setStuckAt(view.scale)
    }, 300)
    restore.current = () => { window.clearTimeout(timer); meta.content = original; restore.current = null }
    meta.content = `${original}, maximum-scale=1`
  }

  const percent = zoomPercent(scale)
  return <button ref={badge} type="button" popover="manual" className="zoom-badge"
    aria-label={stuck ? `Zoomed to ${percent}%. Pinch out to return to 100%.` : `Zoomed to ${percent}%. Reset to 100%.`}
    title={stuck ? 'Pinch out to return to 100%' : 'Reset zoom to 100%'} onClick={reset}>
    <ZoomIn size={16} aria-hidden="true" />
    <span>{percent}%</span>
    {stuck && <small>Pinch out</small>}
  </button>
}
