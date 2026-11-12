import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useBackStep } from './backNav'

type Point = { x: number; y: number }
type Shape = { transform: string; width: number; height: number; borderRadius: string | number }
type Drag = { id: number; x: number; y: number; dx: number; dy: number; moved: boolean; caught: boolean; point: Point; vx: number; vy: number; lastX: number; lastY: number; time: number }
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
const translate = (point: Point) => `translate3d(${point.x}px, ${point.y}px, 0)`
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max))
const elastic = (value: number, max: number) => { const fitted = clamp(value, 0, max); return fitted + (value - fitted) * 0.25 }
const SIZE = 52
const TAB_W = 26
const TAB_H = 48
const EDGE = 12
export type Place = { side: 'left' | 'right'; y: number }  // y: share (0..1) of the free height, so a rotate or resize keeps it on screen

const loadPlace = (storageKey: string, fallback: Place): Place => {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) ?? 'null') as Place | null
    if (saved && (saved.side === 'left' || saved.side === 'right') && saved.y >= 0 && saved.y <= 1) return saved
  } catch { /* storage blocked */ }
  return fallback
}
const savePlace = (storageKey: string, place: Place) => { try { localStorage.setItem(storageKey, JSON.stringify(place)) } catch { /* storage blocked */ } }

// Free area: inside the safe area, and above the phone tab bar.
// A focused text box hides the tab bar (display: none, top 0). Keep its last height, so the keyboard
// does not throw the bubble to the top and leave it there.
let tabbarHeight = 0
function bounds() {
  const tabs = document.querySelector<HTMLElement>('.phone-tabbar')
  if (tabs && tabs.offsetHeight) tabbarHeight = tabs.offsetHeight
  const bottom = window.innerHeight - (tabs ? tabbarHeight : 0)
  return { left: EDGE, right: window.innerWidth - SIZE - EDGE, top: EDGE + 48, bottom: Math.max(EDGE + 48, bottom - SIZE - EDGE) }
}
const tabShape = (place: Place, point: Point): Shape => {
  const vw = document.documentElement.clientWidth || window.innerWidth
  return { transform: translate({ x: place.side === 'left' ? 0 : vw - TAB_W, y: point.y + (SIZE - TAB_H) / 2 }), width: TAB_W, height: TAB_H, borderRadius: place.side === 'left' ? `0 ${TAB_H / 2}px ${TAB_H / 2}px 0` : `${TAB_H / 2}px 0 0 ${TAB_H / 2}px` }
}
const freeShape = (point: Point): Shape => ({ transform: translate(point), width: SIZE, height: SIZE, borderRadius: SIZE / 2 })
const toPoint = (place: Place) => { const b = bounds(); return { x: place.side === 'left' ? b.left : b.right, y: b.top + place.y * (b.bottom - b.top) } }

export function useDockedBubble(storageKey: string, fallback: Place, onEscape?: () => boolean) {
  const [place, setPlace] = useState(() => loadPlace(storageKey, fallback))
  const settled = useRef(place)
  const [point, setPoint] = useState(() => toPoint(place))
  const [dragging, setDragging] = useState(false)
  const [open, setOpen] = useState(false)
  const [closing, setClosing] = useState(false)
  const drag = useRef<Drag | null>(null)
  const visual = useRef<Shape | null>(null)
  const flight = useRef(0)
  const dragged = useRef(false)  // the click after a drag must not open the popover
  const button = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)

  const timer = useRef(0)
  const escape = useRef(onEscape)
  useEffect(() => { escape.current = onEscape })
  const docked = !open && !dragging

  useEffect(() => {
    const fit = () => {
      if (flight.current) window.clearTimeout(flight.current)
      flight.current = 0
      const d = drag.current
      drag.current = null
      if (d) { try { button.current?.releasePointerCapture(d.id) } catch { /* pointer already gone */ } }
      visual.current = null
      if (button.current) { button.current.style.transition = ''; button.current.style.scale = '' }
      setDragging(false)
      setPoint(toPoint(settled.current))
    }
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [])

  const close = useCallback((focus = true) => {
    if (!open || closing) return
    setClosing(true)
    timer.current = window.setTimeout(() => { setOpen(false); setClosing(false); if (focus) button.current?.focus() }, 140)
  }, [open, closing])
  useBackStep(open, close)

  useEffect(() => {
    if (!open) return
    pop.current?.focus()
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { if (escape.current) event.preventDefault(); event.stopPropagation(); if (!escape.current?.()) close() } }
    const outside = (event: PointerEvent) => { const target = event.target as Node; if (!pop.current?.contains(target) && !button.current?.contains(target)) close(false) }
    window.addEventListener('keydown', key, true)
    window.addEventListener('pointerdown', outside, true)
    return () => { window.removeEventListener('keydown', key, true); window.removeEventListener('pointerdown', outside, true) }
  }, [open, close])

  const down = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || drag.current) return
    const el = event.currentTarget
    const caught = !!flight.current
    const computed = getComputedStyle(el), matrix = new DOMMatrix(computed.transform)
    const at = { x: matrix.m41, y: matrix.m42 }
    const width = computed.width, height = computed.height, borderRadius = computed.borderRadius
    const raw = { x: clamp(at.x, 0, window.innerWidth - SIZE), y: clamp(at.y, 0, window.innerHeight - SIZE) }
    raw.x += (at.x - raw.x) * 4; raw.y += (at.y - raw.y) * 4
    window.clearTimeout(flight.current); flight.current = 0
    el.style.transition = 'none'
    el.style.transform = translate(at)
    if (caught) {
      visual.current = { transform: translate(at), width: parseFloat(width), height: parseFloat(height), borderRadius }
      el.style.width = width; el.style.height = height; el.style.borderRadius = borderRadius
    }
    try { el.setPointerCapture(event.pointerId) } catch { /* pointer already gone */ }
    dragged.current = false
    // A docked tab centres under the finger. A caught release keeps its current offset.
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, dx: docked && !caught ? SIZE / 2 : event.clientX - raw.x, dy: docked && !caught ? SIZE / 2 : event.clientY - raw.y, moved: false, caught, point: at, vx: 0, vy: 0, lastX: event.clientX, lastY: event.clientY, time: event.timeStamp }
  }
  const move = (event: React.PointerEvent<HTMLButtonElement>) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    if (!d.moved && Math.hypot(event.clientX - d.x, event.clientY - d.y) < 6) return
    if (!d.moved) { d.moved = true; setDragging(true); if (open) close(false) }
    if (event.clientX !== d.lastX || event.clientY !== d.lastY) {
      const dt = Math.max(1, event.timeStamp - d.time)
      d.vx = (dt > 100 ? 0 : d.vx * 0.4) + (event.clientX - d.lastX) / dt * 0.6
      d.vy = (dt > 100 ? 0 : d.vy * 0.4) + (event.clientY - d.lastY) / dt * 0.6
      d.lastX = event.clientX; d.lastY = event.clientY; d.time = event.timeStamp
    }
    d.point = { x: elastic(event.clientX - d.dx, window.innerWidth - SIZE), y: elastic(event.clientY - d.dy, window.innerHeight - SIZE) }
    visual.current = freeShape(d.point)
    const el = event.currentTarget
    el.style.transition = 'none'
    el.style.scale = ''
    el.style.transform = translate(d.point)
    el.style.width = `${SIZE}px`; el.style.height = `${SIZE}px`; el.style.borderRadius = `${SIZE / 2}px`
  }
  const up = (event: React.PointerEvent<HTMLButtonElement>) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    if (event.type !== 'pointercancel') move(event)
    drag.current = null
    const el = event.currentTarget
    try { el.releasePointerCapture(d.id) } catch { /* pointer already gone */ }
    if (!d.moved && !d.caught) { el.style.transition = ''; return }  // a tap toggles the popover
    dragged.current = true
    const carry = event.type !== 'pointercancel' && !reduced() && event.timeStamp - d.time <= 100
    const vx = carry ? d.vx : 0, vy = carry ? d.vy : 0
    const projected = { x: d.point.x + vx * 180, y: d.point.y + vy * 180 }
    const b = bounds(), y = clamp(projected.y, b.top, b.bottom)
    const next: Place = { side: projected.x + SIZE / 2 < window.innerWidth / 2 ? 'left' : 'right', y: b.bottom > b.top ? (y - b.top) / (b.bottom - b.top) : 0 }
    const target = toPoint(next), shape = tabShape(next, target)
    const speed = Math.hypot(vx, vy)
    const targetX = next.side === 'left' ? 0 : (document.documentElement.clientWidth || window.innerWidth) - TAB_W
    const distance = Math.hypot(targetX - d.point.x, target.y + (SIZE - TAB_H) / 2 - d.point.y)
    const ms = reduced() ? 0 : clamp(distance / Math.max(0.5, speed), 200, 380)
    const curve = speed > 0.5 ? 'cubic-bezier(.34,1.3,.64,1)' : 'cubic-bezier(.32,.72,0,1)'
    visual.current = shape
    void el.offsetWidth
    el.style.transition = ms ? `transform ${ms}ms ${curve}, width ${ms}ms cubic-bezier(.32,.72,0,1), height ${ms}ms cubic-bezier(.32,.72,0,1), border-radius ${ms}ms cubic-bezier(.32,.72,0,1), scale ${ms}ms cubic-bezier(.32,.72,0,1)` : 'none'
    el.style.transform = shape.transform
    el.style.width = `${TAB_W}px`; el.style.height = `${TAB_H}px`; el.style.borderRadius = String(shape.borderRadius); el.style.scale = '1'
    const done = () => {
      flight.current = 0
      visual.current = null
      el.style.transition = ''; el.style.scale = ''
      setDragging(false)
      setPoint(target)
      settled.current = next
      setPlace(next)
      savePlace(storageKey, next)
    }
    if (ms) flight.current = window.setTimeout(done, ms)
    else done()
  }
  const click = (event: React.MouseEvent, beforeOpen?: () => void) => {
    if (dragging || dragged.current) { dragged.current = false; event.preventDefault(); return }
    if (open) close(); else { beforeOpen?.(); window.dispatchEvent(new CustomEvent('tether:bubble-open', { detail: storageKey })); setOpen(true) }
  }

  // The popover opens on the bubble's side, above it in the lower half of the screen and below it in the upper half.
  const above = point.y + SIZE / 2 > window.innerHeight / 2
  const width = Math.min(340, window.innerWidth - 2 * EDGE)
  const left = place.side === 'left' ? EDGE : window.innerWidth - width - EDGE
  const popStyle: React.CSSProperties = {
    left, width,
    ...(above ? { bottom: window.innerHeight - point.y + 10 } : { top: point.y + SIZE + 10 }),
    maxHeight: above ? point.y - 10 - EDGE : window.innerHeight - point.y - SIZE - 10 - EDGE,
    transformOrigin: `${place.side === 'left' ? 'left' : 'right'} ${above ? 'bottom' : 'top'}`,
  }
  const shape = docked ? tabShape(place, point) : freeShape(point)
  useLayoutEffect(() => {
    // Keep React's shape changes from resetting an active drag or release.
    const el = button.current, shape = visual.current
    if (!el || !shape) return
    el.style.transform = shape.transform
    el.style.width = `${shape.width}px`; el.style.height = `${shape.height}px`
    el.style.borderRadius = typeof shape.borderRadius === 'number' ? `${shape.borderRadius}px` : shape.borderRadius
  })

  useEffect(() => {
    // A second bubble takes over without returning focus to the first one.
    const other = (event: Event) => {
      if ((event as CustomEvent<string>).detail === storageKey) return
      window.clearTimeout(timer.current)
      setOpen(false); setClosing(false)
    }
    window.addEventListener('tether:bubble-open', other)
    const el = button.current
    return () => {
      window.clearTimeout(timer.current); window.clearTimeout(flight.current)
      const d = drag.current
      if (d) { try { el?.releasePointerCapture(d.id) } catch { /* pointer already gone */ } }
      window.removeEventListener('tether:bubble-open', other)
    }
  }, [storageKey])

  return { point, place, docked, dragging, open, closing, button, pop, shape, popStyle, close, down, move, up, click }
}
