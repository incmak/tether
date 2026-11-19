// Motion for the Friday window: the window folds into its bubble and the bubble blooms back into the window.
// Only transform, translate, opacity and clip-path move. Nothing waits for an animation to show content: every
// animation runs without fill, so if it never runs the element sits in its normal, visible state.
// The element's position lives in the CSS translate property. Keyframes with an absolute transform set translate
// to none, so the position is not applied twice.
import type { Point, Size } from './floatingWindow'

const EASE_OUT = 'cubic-bezier(0.23, 1, 0.32, 1)'
const EASE_DRAWER = 'cubic-bezier(0.32, 0.72, 0, 1)'
const EASE_IN_OUT = 'cubic-bezier(0.65, 0, 0.35, 1)'

/** An underdamped spring as a CSS linear() curve. zeta 0.55 overshoots about 12%, then settles by the end. */
const spring = (zeta: number, points = 40) => {
  const w = 6.4 / zeta
  const wd = w * Math.sqrt(1 - zeta * zeta)
  const at = (t: number) => 1 - Math.exp(-zeta * w * t) * (Math.cos(wd * t) + (zeta * w / wd) * Math.sin(wd * t))
  const values = Array.from({ length: points + 1 }, (_, i) => i === points ? 1 : +at(i / points).toFixed(4))
  return `linear(${values.join(', ')})`
}
const canSpring = typeof CSS !== 'undefined' && CSS.supports?.('animation-timing-function', 'linear(0, 1)')
const SPRING_POP = canSpring ? spring(0.55) : EASE_OUT
const SPRING_SNAP = canSpring ? spring(0.7) : EASE_OUT

export const reducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
const can = (el: HTMLElement | null): el is HTMLElement => !!el && typeof el.animate === 'function'

/** The window's box at the bubble: scaled to cover the bubble's box and cut to its capsule shape. */
const atBubble = (box: DOMRect, bubble: Point, size: Size) => {
  const s = Math.max(size.w / box.width, size.h / box.height)
  const x = bubble.x + size.w / 2 - s * box.width / 2
  const y = bubble.y + size.h / 2 - s * box.height / 2
  const insetY = (box.height - size.h / s) / 2, insetX = (box.width - size.w / s) / 2
  return { translate: 'none', transform: `translate(${x}px, ${y}px) scale(${s})`, clipPath: `inset(${insetY}px ${insetX}px round ${size.h / s / 2}px)` }
}
const atBox = (el: HTMLElement, box: DOMRect) => ({ translate: 'none', transform: `translate(${box.left}px, ${box.top}px) scale(1)`, clipPath: `inset(0px 0px round ${parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0}px)` })

/** Window or full screen folds into a capsule and slides into the bubble's place. */
export function foldToBubble(el: HTMLElement | null, bubble: Point, size: Size): Animation | null {
  if (!can(el) || reducedMotion()) return null
  const box = el.getBoundingClientRect()
  const from = atBox(el, box), to = atBubble(box, bubble, size)
  return el.animate([
    { ...from, transformOrigin: '0 0', opacity: 1 },
    { translate: 'none', opacity: 1, offset: 0.55 },
    { ...to, transformOrigin: '0 0', opacity: 0.5 },
  ], { duration: 260, easing: EASE_IN_OUT, fill: 'forwards' })
}

/** The bubble arrives with a small spring, so it reads as the same object the window turned into. */
export function popBubble(el: HTMLElement | null, folded: boolean) {
  if (!can(el)) return
  if (reducedMotion()) { el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 150, easing: 'ease' }); return }
  el.animate([{ scale: folded ? 0.92 : 0.85, opacity: folded ? 0.5 : 0 }, { scale: 1, opacity: 1 }], { duration: 420, easing: SPRING_POP })
}

/** The bubble blooms out into the window (or full screen) from where it sits. */
export function bloomFromBubble(el: HTMLElement | null, bubble: Point, size: Size) {
  if (!can(el)) return
  if (reducedMotion()) { el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 150, easing: 'ease' }); return }
  const box = el.getBoundingClientRect()
  const from = atBubble(box, bubble, size), to = atBox(el, box)
  el.animate([
    { ...from, transformOrigin: '0 0', opacity: 0.5 },
    { translate: 'none', opacity: 1, offset: 0.35 },
    { ...to, transformOrigin: '0 0', opacity: 1 },
  ], { duration: 300, easing: EASE_DRAWER })
}

/** First open: Friday grows out of the button that opened it, so the window reads as coming from there. */
export function openFrom(el: HTMLElement | null, from: { left: number; top: number; width: number; height: number }) {
  if (!can(el)) return
  if (reducedMotion()) { el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 150, easing: 'ease' }); return }
  const size = Math.max(24, Math.min(from.width, from.height))
  const box = el.getBoundingClientRect()
  const start = atBubble(box, { x: from.left + (from.width - size) / 2, y: from.top + (from.height - size) / 2 }, { w: size, h: size })
  el.animate([
    { ...start, transformOrigin: '0 0', opacity: 0 },
    { translate: 'none', opacity: 1, offset: 0.3 },
    { ...atBox(el, box), transformOrigin: '0 0', opacity: 1 },
  ], { duration: 340, easing: EASE_DRAWER })
}

/** Window to full screen and back: a short settle, centered, no travel. */
export function settle(el: HTMLElement | null, opening = false) {
  if (!can(el)) return
  if (reducedMotion()) { el.animate([{ opacity: 0.4 }, { opacity: 1 }], { duration: 150, easing: 'ease' }); return }
  el.animate(opening
    ? [{ scale: 0.96, transform: 'translateY(10px)', opacity: 0 }, { scale: 1, transform: 'none', opacity: 1 }]
    : [{ scale: 0.97, opacity: 0.6 }, { scale: 1, opacity: 1 }], { duration: opening ? 260 : 200, easing: EASE_OUT })
}

/** After a drag the bubble snaps to the side. It travels there on a spring instead of jumping. */
export function snapBubble(el: HTMLElement | null, from: Point, to: Point) {
  const dx = from.x - to.x, dy = from.y - to.y
  if (!can(el) || reducedMotion() || Math.hypot(dx, dy) < 24) return
  el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], { duration: 460, easing: SPRING_SNAP })
}
