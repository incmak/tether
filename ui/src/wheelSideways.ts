// A vertical scroll (a mouse wheel; a trackpad swipe up or down) over a horizontal strip of controls (tab bars,
// toolbars, breadcrumbs, thumbnails) scrolls that strip sideways: down or away moves it to the right. A sideways swipe
// keeps the browser's native scroll. Touch screens send no wheel events. The chat header's stack row and tab strip use
// stripScroll.ts instead (it also stops Back and Forward there).
//
// A scroll that starts elsewhere stays there: when the page or the chat scrolls a strip under a resting pointer, the
// next wheel events of that run still scroll the page. A run is a series of wheel events less than QUIET_MS apart,
// anywhere in the document; the strip takes only a run that starts on it. Browsers latch nested scrolling the same
// way. Chromium also makes the later events of a run uncancelable when its first event was not cancelled, so the
// decision has to hold for the whole run.
//
// Use it on chrome strips only, not on strips inside reading content (chat images, tables, code): there a wheel must
// scroll the content. Attach with the ref callback: <div ref={wheelSideways}>.

import { NOTCH, QUIET_MS } from './stripScroll'

const LINE = 16, PAGE = 400

interface Run { at: number; owner: Element | null }
let previous: Run | null = null
let current: Run | null = null
let watching = false

/** Notes every wheel event in the document, before any strip sees it. Passive: it never delays a scroll. */
function watch() {
  if (watching) return
  watching = true
  addEventListener('wheel', event => { previous = current; current = { at: event.timeStamp, owner: null } }, { capture: true, passive: true })
}

export interface WheelInput { deltaX: number; deltaY: number; deltaMode: number; ctrlKey: boolean; timeStamp: number }

/** The px to scroll the strip by, or null to let the browser have the event. Exported for tests. */
export function sidewaysDelta(event: WheelInput, strip: Element, last: Run | null, overflows: boolean): number | null {
  if (event.ctrlKey) return null  // a pinch zoom
  const scale = event.deltaMode === 1 ? LINE : event.deltaMode === 2 ? PAGE : 1
  const dx = event.deltaX * scale, dy = event.deltaY * scale
  // A run in progress: the strip keeps it only if the run started on the strip. Each event then moves the strip by its
  // larger delta, so a trackpad swipe whose first event leans vertical still scrolls sideways.
  if (last && event.timeStamp - last.at <= QUIET_MS) return last.owner === strip ? Math.abs(dx) > Math.abs(dy) ? dx : dy : null
  if (Math.abs(dx) >= Math.abs(dy) || !overflows) return null
  return dy
}

const glides = new WeakMap<Element, { left: number; at: number }>()

/** Scrolls strip by dx. A mouse-wheel notch glides; a run of notches adds to where the last one was going, not to where
 * the strip is mid-animation. Small (trackpad) deltas follow 1:1. */
export function scrollSideways(strip: Element, dx: number, at: number) {
  const glide = Math.abs(dx) >= NOTCH && !matchMedia('(prefers-reduced-motion: reduce)').matches
  if (!glide) { glides.delete(strip); strip.scrollBy({ left: dx, behavior: 'instant' }); return }
  const last = glides.get(strip)
  const from = last && at - last.at < 400 ? last.left : strip.scrollLeft
  const left = Math.max(0, Math.min(strip.scrollWidth - strip.clientWidth, from + dx))
  glides.set(strip, { left, at })
  strip.scrollTo({ left, behavior: 'smooth' })
}

function overflows(strip: Element) {
  return strip.scrollWidth - strip.clientWidth > 0.5 && /auto|scroll/.test(getComputedStyle(strip).overflowX)
}

/** React 19 ref callback: <div ref={wheelSideways}>. Returns the cleanup. */
export function wheelSideways(strip: HTMLElement | null) {
  if (!strip) return
  watch()
  const wheel = (event: WheelEvent) => {
    if (!event.cancelable) return
    const dx = sidewaysDelta(event, strip, previous, overflows(strip))
    if (dx === null) return
    if (current) current.owner = strip
    event.preventDefault()
    scrollSideways(strip, dx, event.timeStamp)
  }
  strip.addEventListener('wheel', wheel, { passive: false })
  return () => strip.removeEventListener('wheel', wheel)
}
