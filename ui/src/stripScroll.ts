// Desktop: a two-finger trackpad swipe over a chat's stack row or tab strip scrolls that strip (Settings › Sessions &
// chat). It never switches tabs and never goes Back or Forward. Elsewhere on the page the swipe stays the browser's
// Back and Forward (B81: the root lets x overscroll through, index.css).
//
// The chat panel has one non-passive wheel listener; this module decides what each wheel event does:
// - Over a strip that can still scroll that way, the browser scrolls it natively (with its momentum). At the strip's
//   end, overscroll-behavior-x: contain (App.css, .chat-panel[data-strip-scroll]) stops the scroll there instead of chaining to the root.
// - BAND px above or below a strip, outside it, the listener scrolls the nearest strip by the event's delta and
//   cancels the event. A trackpad sends its momentum as wheel events, so this follows the fingers 1:1.
// - At a strip's end, over a strip that does not overflow, and over the rest of the header: cancel, so no Back.
// Chromium sends only the first wheel event of a scroll sequence as cancelable when it is not cancelled, so the
// decision is made once per gesture and held until QUIET_MS without wheel events.

/** The extra hit zone above and below each strip, in CSS px. */
export const BAND = 10
export const QUIET_MS = 160
const LINE = 16, PAGE = 400

/** The strips that scroll, children of the chat panel: the stack row and the tab strip (inside .group-strip, which is
 * display: contents, when there are stacks). */
export const STRIPS = ':scope > .group-row, :scope > .pane-tabs, :scope > .group-strip > .pane-tabs'
/** The header parts where a swipe outside the strips' zone does nothing (no Back/Forward). */
export const HEADER = '.chat-header, .group-row, .group-strip, .pane-tabs'
/** Targets that keep the wheel: the header's open menu and dialogs. */
export const SKIP = '.keys-menu, [role="menu"], [role="dialog"], dialog'

export interface WheelLike { deltaX: number; deltaY: number; deltaMode: number; ctrlKey: boolean; timeStamp: number; clientX: number; clientY: number }
export interface StripBox { top: number; bottom: number; left: number; right: number; scrollLeft: number; scrollWidth: number; clientWidth: number }
/** What is under the pointer. native: the target sits in an element that can scroll by dx itself (a strip or any
 * other horizontal scroller). header: the target is in the chat header, the stack row or the tab strip. */
export interface Scene { strips: StripBox[]; native: boolean; header: boolean }
/** pass: let the browser have it. block: preventDefault. scroll: preventDefault and scroll strips[strip] by dx. */
export type Plan = { do: 'pass' } | { do: 'block' } | { do: 'scroll'; strip: number; dx: number }

/** The strip whose rect, grown by BAND on each side, holds the point; the nearer one by y when two zones overlap. */
export function nearestStrip(strips: StripBox[], x: number, y: number): number {
  let best = -1, bestGap = Infinity
  strips.forEach((box, index) => {
    if (box.bottom <= box.top || x < box.left - BAND || x > box.right + BAND) return
    const gap = Math.max(0, box.top - y, y - box.bottom)
    if (gap <= BAND && gap < bestGap) { best = index; bestGap = gap }
  })
  return best
}

/** Room to scroll by dx, in px (a fraction of a pixel counts as none: scrollLeft can be fractional). */
export function room(box: StripBox, dx: number) {
  return dx > 0 ? box.scrollWidth - box.clientWidth - box.scrollLeft > 0.5 : dx < 0 ? box.scrollLeft > 0.5 : false
}

export function createStripWheel() {
  let last = -Infinity
  let held: Plan | null = null  // this gesture's decision
  return function wheel(event: WheelLike, look: (dx: number) => Scene): Plan {
    if (event.ctrlKey) return { do: 'pass' }  // a pinch zoom
    const scale = event.deltaMode === 1 ? LINE : event.deltaMode === 2 ? PAGE : 1
    const dx = event.deltaX * scale, dy = event.deltaY * scale
    const fresh = event.timeStamp - last > QUIET_MS
    if (!fresh && held) {
      last = event.timeStamp
      return held.do === 'scroll' ? { ...held, dx } : held
    }
    // A gesture is ours only when it starts sideways; a vertical scroll (a mouse wheel too) passes through.
    if (Math.abs(dx) <= Math.abs(dy)) { held = null; return { do: 'pass' } }
    last = event.timeStamp
    const scene = look(dx)
    const strip = nearestStrip(scene.strips, event.clientX, event.clientY)
    if (scene.native) held = { do: 'pass' }
    else if (strip >= 0) held = room(scene.strips[strip], dx) ? { do: 'scroll', strip, dx } : { do: 'block' }
    else held = scene.header ? { do: 'block' } : { do: 'pass' }
    return held
  }
}

/** Can an ancestor of target, up to and with stop, scroll by dx on its own? */
export function scrollsItself(target: Element | null, stop: Element, dx: number) {
  for (let node = target; node && stop.contains(node); node = node.parentElement) {
    if (node.scrollWidth > node.clientWidth && /auto|scroll/.test(getComputedStyle(node).overflowX) && room(boxOf(node), dx)) return true
    if (node === stop) break
  }
  return false
}

export function boxOf(element: Element): StripBox {
  const rect = element.getBoundingClientRect()
  return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, scrollLeft: element.scrollLeft, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }
}
