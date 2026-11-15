// A two-finger trackpad swipe over a chat's header switches to the previous or next tab (desktop only).
// Elsewhere the same swipe stays the browser's Back and Forward (B81). The header listens to wheel events
// (non-passive, so preventDefault keeps the browser from starting its history swipe) and this module decides.
//
// One swipe is one tab step. Wheel deltaX adds up until it passes THRESHOLD; then the gesture locks. Momentum keeps
// sending smaller and smaller deltas after the fingers lift, and the lock eats them. The lock ends after QUIET_MS with
// no wheel events, or earlier when a new swipe shows: the deltas turn the other way (REVERSE px in a row), or rise again after they fell to a
// third of their peak (macOS stops the momentum when the fingers touch again, and the new swipe starts small).
// Direction follows the fingers, as on the phone (a swipe to the left shows the next tab): a positive deltaX (fingers
// move left with natural scrolling) is +1, the next tab.

export const THRESHOLD = 40
export const QUIET_MS = 160
/** A turn the other way during the lock must add up to this before it counts as a new swipe: one or two pixels the
 * other way are finger noise in the momentum, not a swipe back. */
export const REVERSE = 12
const LINE = 16, PAGE = 400

export interface WheelLike { deltaX: number; deltaY: number; deltaMode: number; ctrlKey: boolean; timeStamp: number }
/** handled: call preventDefault (no browser Back or Forward, no strip scroll). step: -1 previous tab, 1 next, 0 none. */
export interface SwipeResult { handled: boolean; step: -1 | 0 | 1 }

export function createTabSwipe() {
  let last = -Infinity
  let sum = 0
  let locked = 0  // the direction of the step this gesture made, or 0
  let peak = 0, low = 0  // since the lock: the largest |deltaX|, and the smallest one after it
  let back = 0  // since the lock: the deltas against it, in a row
  let active = false
  function wheel(event: WheelLike): SwipeResult {
    if (event.ctrlKey) return { handled: false, step: 0 }  // a pinch zoom
    const scale = event.deltaMode === 1 ? LINE : event.deltaMode === 2 ? PAGE : 1
    const dx = event.deltaX * scale, dy = event.deltaY * scale
    if (event.timeStamp - last > QUIET_MS) { active = false; sum = 0; locked = 0; back = 0 }
    // A gesture starts only when it goes sideways; a vertical scroll over the header passes through.
    if (!active && Math.abs(dx) <= Math.abs(dy)) return { handled: false, step: 0 }
    active = true
    last = event.timeStamp
    if (!dx) return { handled: true, step: 0 }
    const size = Math.abs(dx), sign = dx > 0 ? 1 : -1
    if (locked) {
      if (sign !== locked) {
        back += dx
        if (Math.abs(back) < REVERSE) return { handled: true, step: 0 }
        locked = 0; sum = back - dx; back = 0  // a swipe back: it starts from what it moved so far
      } else {
        back = 0
        if (size >= peak) { peak = size; low = size } else low = Math.min(low, size)
        if (!(low * 3 <= peak && size > low * 4 + 8)) return { handled: true, step: 0 }
        locked = 0; sum = 0  // the deltas rise again: a new swipe
      }
    }
    sum = Math.sign(sum) === -sign ? dx : sum + dx
    if (Math.abs(sum) < THRESHOLD) return { handled: true, step: 0 }
    locked = sign; peak = low = size; sum = 0; back = 0
    return { handled: true, step: sign }
  }
  /** The step drew its tab at `at` (a performance.now() time). A remount can hold the page for longer than QUIET_MS:
   * the quiet time counts from then, so the rest of the swipe does not look like a new one. */
  function hold(at: number) { if (locked) last = Math.max(last, at) }
  return Object.assign(wheel, { hold })
}

/** One gesture state for the page: a step remounts the chat (and its header), and the rest of the swipe must still
 * land on the lock. Blink sends the next wheel event to the new header under the pointer (mouse_wheel_event_manager.cc). */
export const tabSwipe = createTabSwipe()

/** The parts of a chat panel that switch tabs: the header row, the stack row and the tab strip. With stacks the strip
 * sits in .group-strip, which is display: contents. A listener there has no box, so Chromium would not wait for it
 * and preventDefault would not stop Back: the zone takes its children. */
export const SWIPE_ZONE = ':scope > .chat-header, :scope > .group-row, :scope > .group-strip > *, :scope > .pane-tabs'
/** Parents whose children come and go with the tabs: the panel, and .group-strip. */
export const SWIPE_PARENTS = ':scope > .group-strip'
/** Targets inside the zone that keep the wheel: the header's open menu. */
export const SWIPE_SKIP = '.keys-menu, [role="menu"], [role="dialog"], dialog'

let tabStep: { from: string; at: number } | null = null
/** A header swipe left the tab `from`: the tab strip scrolls the new tab into view and slides its pill from there. */
export function markTabStep(from: string) { tabStep = { from, at: performance.now() } }
/** The fresh step, if any (under a second old), once: a later redraw of the strip is not a step. */
export function takeTabStep() {
  const step = tabStep && performance.now() - tabStep.at < 1000 ? tabStep : null
  tabStep = null
  return step
}
