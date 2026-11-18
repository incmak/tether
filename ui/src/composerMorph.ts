import { useEffect, useLayoutEffect, useRef } from 'react'
import type { RefObject } from 'react'

// The phone composer morphs between the one-row pill and the full box, as the Claude app does.
// The layout changes at once, so the chat above never reflows frame by frame. Only paint-cheap things move:
// - the box outline (a ::before behind the content) grows or shrinks from the old size, anchored at the bottom;
// - the text field, + and the mic/send group slide from where they were (transforms);
// - controls that were hidden fade in; until they show they take no taps or focus (inert).
// - the whole box slides from its old place when the tab bar under it hides or comes back.
// Part positions are kept relative to the box's bottom edge. The morph flag is a data attribute, not a class,
// because React rewrites the class list when `compact` changes.

const EXPAND = 300, COLLAPSE = 240
const EASE = 'cubic-bezier(0.32, 0.72, 0, 1)'
const PARTS = { field: 'textarea', attach: '.attach-container', end: '.composer-controls > div:last-child' } as const
type Part = keyof typeof PARTS
interface Place { left: number; right: number; bottom: number }
interface Snapshot { bottom: number; height: number; radius: number; parts: Partial<Record<Part, Place>>; shown: Set<Element> }

const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

function snapshot(box: HTMLElement): Snapshot {
  const outer = box.getBoundingClientRect()
  const parts: Snapshot['parts'] = {}
  for (const [name, selector] of Object.entries(PARTS) as [Part, string][]) {
    const element = box.querySelector(selector)
    if (!element) continue
    const rect = element.getBoundingClientRect()
    if (rect.width) parts[name] = { left: rect.left - outer.left, right: outer.right - rect.right, bottom: outer.bottom - rect.bottom }
  }
  const shown = new Set<Element>()
  for (const control of box.querySelectorAll('.composer-controls button, .composer-controls .pill, .composer-status')) if ((control as HTMLElement).offsetParent) shown.add(control)
  const radius = Math.min(parseFloat(getComputedStyle(box).borderTopLeftRadius) || 0, outer.height / 2)
  return { bottom: outer.bottom, height: outer.height, radius, parts, shown }
}

// The shape on screen during a morph: the outline's animated size, and each part's settled place plus its animated shift.
const shift = (element: Element | null) => {
  const transform = element ? getComputedStyle(element).transform : 'none'
  const matrix = new DOMMatrixReadOnly(transform === 'none' ? undefined : transform)
  return { x: matrix.m41, y: matrix.m42 }
}

function live(box: HTMLElement, settled: Snapshot): Snapshot {
  const outline = getComputedStyle(box, '::before')
  const height = parseFloat(outline.height), radius = parseFloat(outline.borderTopLeftRadius)
  const parts: Snapshot['parts'] = {}
  for (const [name, place] of Object.entries(settled.parts) as [Part, Place][]) {
    const { x, y } = shift(box.querySelector(PARTS[name]))
    parts[name] = { left: place.left + x, right: place.right - x, bottom: place.bottom - y }
  }
  return { bottom: settled.bottom + shift(box).y, height: Number.isFinite(height) ? height : settled.height, radius: Number.isFinite(radius) ? radius : settled.radius, parts, shown: settled.shown }
}

// End a morph: the real outline comes back in one frame (no color transition from the transparent morph border),
// and controls that were fading in take taps and focus again.
function handBack(box: HTMLElement) {
  for (const control of box.querySelectorAll('[data-morph-in]')) { control.removeAttribute('data-morph-in'); control.removeAttribute('inert') }
  box.style.transition = 'none'
  box.removeAttribute('data-morph')
  void box.offsetWidth
  box.style.transition = ''
}

export function useComposerMorph(form: RefObject<HTMLFormElement | null>, compact: boolean) {
  const last = useRef<Snapshot | null>(null)
  const was = useRef(compact)
  const running = useRef<Animation[]>([])
  // Keep the last settled shape. A ResizeObserver runs after layout, so at the next change it still holds the old shape.
  // The box can also move without a resize (the keyboard, the tab bar), so a viewport resize and a press take a new
  // snapshot too. The press comes before focus hides the tab bar.
  useEffect(() => {
    const box = form.current
    if (!box || typeof ResizeObserver === 'undefined') return
    const refresh = () => { if (!running.current.length) last.current = snapshot(box) }
    const observer = new ResizeObserver(refresh)
    observer.observe(box)
    const view = window.visualViewport
    box.addEventListener('pointerdown', refresh, true)
    window.addEventListener('resize', refresh)
    view?.addEventListener('resize', refresh)
    last.current = snapshot(box)
    return () => {
      observer.disconnect()
      box.removeEventListener('pointerdown', refresh, true)
      window.removeEventListener('resize', refresh)
      view?.removeEventListener('resize', refresh)
    }
  }, [form])
  useLayoutEffect(() => {
    const box = form.current
    if (was.current === compact || !box) return
    was.current = compact
    // A change in the middle of a morph starts from what is on screen now, not from the last settled shape.
    const before = running.current.length && last.current ? live(box, last.current) : last.current
    for (const animation of running.current) animation.cancel()
    running.current = []
    if (box.hasAttribute('data-morph')) handBack(box)
    const after = snapshot(box)
    last.current = after
    if (!before || reduced() || typeof box.animate !== 'function' || Math.abs(before.height - after.height) < 2) return
    const timing: KeyframeAnimationOptions = { duration: compact ? COLLAPSE : EXPAND, easing: EASE }
    const animations: Animation[] = []
    box.setAttribute('data-morph', '')
    const dy = before.bottom - after.bottom
    if (Math.abs(dy) >= 0.5) animations.push(box.animate({ transform: [`translateY(${dy}px)`, 'none'] }, timing))
    animations.push(box.animate({ height: [`${before.height}px`, `${after.height}px`], borderRadius: [`${before.radius}px`, `${after.radius}px`] }, { ...timing, pseudoElement: '::before' }))
    for (const name of Object.keys(PARTS) as Part[]) {
      const from = before.parts[name], to = after.parts[name], element = box.querySelector<HTMLElement>(PARTS[name])
      if (!from || !to || !element) continue
      const x = name === 'end' ? to.right - from.right : from.left - to.left, y = to.bottom - from.bottom
      if (Math.abs(x) < 0.5 && Math.abs(y) < 0.5) continue
      animations.push(element.animate({ transform: [`translate(${x}px, ${y}px)`, 'translate(0, 0)'] }, timing))
    }
    // Controls the old shape did not show come in after the box has started to open. They take no taps until they show.
    for (const control of box.querySelectorAll<HTMLElement>('.composer-controls button, .composer-controls .pill, .composer-status')) {
      if (before.shown.has(control) || !control.offsetParent) continue
      control.setAttribute('data-morph-in', '')
      control.setAttribute('inert', '')
      const fade = control.animate({ opacity: [0, 1] }, { duration: timing.duration as number * 0.7, delay: compact ? 0 : 60, easing: 'ease-out', fill: 'backwards' })
      void fade.finished.then(() => { control.removeAttribute('data-morph-in'); control.removeAttribute('inert') }, () => {})
      animations.push(fade)
    }
    running.current = animations
    void Promise.allSettled(animations.map(animation => animation.finished)).then(() => {
      if (running.current !== animations) return
      running.current = []
      handBack(box)
      last.current = snapshot(box)
    })
  }, [form, compact])
}
