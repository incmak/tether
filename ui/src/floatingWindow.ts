import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

export type FloatMode = 'window' | 'bubble' | 'full'
export type Rect = { x: number; y: number; w: number; h: number }
export type Edge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
type Handlers = Pick<React.HTMLAttributes<HTMLElement>, 'onPointerDown' | 'onPointerMove' | 'onPointerUp' | 'onPointerCancel' | 'onKeyDown'>
export type Point = { x: number; y: number }
export type Size = { w: number; h: number }
type Options = { minW: number; minH: number; bubble: Size; initial: () => Rect; initialMode?: FloatMode }
type Place = { mode: FloatMode; rect: Rect; bubble: Point }
type Drag = { id: number; target: HTMLElement; element: HTMLElement; vx: number; vy: number; lastX: number; lastY: number; time: number; caught: boolean; kind: 'move' | 'resize' | 'bubble'; edge?: Edge; x: number; y: number; rect: Rect; bubble: Point; moved: boolean }

const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
const translate = (point: Point) => `translate3d(${point.x}px, ${point.y}px, 0)`
const position = (element: HTMLElement): Point => {
  const matrix = new DOMMatrix(getComputedStyle(element).transform)
  return { x: matrix.m41, y: matrix.m42 }
}
const elastic = (point: Point, fitted: Point): Point => ({ x: fitted.x + (point.x - fitted.x) * 0.25, y: fitted.y + (point.y - fitted.y) * 0.25 })
const MARGIN = 8
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max))
const bounds = () => ({ w: window.visualViewport?.width ?? window.innerWidth, h: window.visualViewport?.height ?? window.innerHeight })
const fitRect = (rect: Rect, minW: number, minH: number): Rect => {
  const b = bounds()
  const w = clamp(rect.w, minW, b.w - 2 * MARGIN)
  const h = clamp(rect.h, minH, b.h - 2 * MARGIN)
  return { x: clamp(rect.x, MARGIN, b.w - MARGIN - w), y: clamp(rect.y, MARGIN, b.h - MARGIN - h), w, h }
}
const fitBubble = (point: Point, size: Size): Point => {
  const b = bounds()
  return { x: clamp(point.x, MARGIN, b.w - MARGIN - size.w), y: clamp(point.y, MARGIN, b.h - MARGIN - size.h) }
}
const isPoint = (value: unknown): value is Point => {
  if (!value || typeof value !== 'object') return false
  const p = value as Point
  return Number.isFinite(p.x) && Number.isFinite(p.y)
}
const isRect = (value: unknown): value is Rect => {
  if (!isPoint(value)) return false
  const r = value as Rect
  return Number.isFinite(r.w) && r.w > 0 && Number.isFinite(r.h) && r.h > 0
}
const isMode = (value: unknown): value is FloatMode => value === 'window' || value === 'bubble' || value === 'full'
const loadPlace = (storageKey: string, options: Options): Place => {
  const b = bounds()
  let rect = options.initial()
  let bubble = { x: b.w - MARGIN - options.bubble.w, y: b.h - 88 - options.bubble.h }
  let mode = options.initialMode ?? 'window'
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(storageKey) ?? 'null')
    if (saved && typeof saved === 'object') {
      const place = saved as Partial<Place>
      if (isRect(place.rect)) rect = place.rect
      if (isPoint(place.bubble)) bubble = place.bubble
      if (isMode(place.mode)) mode = options.initialMode ?? place.mode
    }
  } catch { /* storage blocked */ }
  return { mode, rect: fitRect(rect, options.minW, options.minH), bubble: fitBubble(bubble, options.bubble) }
}
const savePlace = (storageKey: string, place: Place) => { try { localStorage.setItem(storageKey, JSON.stringify(place)) } catch { /* storage blocked */ } }

const resizeRect = (rect: Rect, edge: Edge, dx: number, dy: number, minW: number, minH: number): Rect => {
  const b = bounds()
  let { x, y, w, h } = fitRect(rect, minW, minH)
  if (edge.includes('w')) {
    const right = x + w
    x = clamp(x + dx, MARGIN, right - minW)
    w = right - x
  }
  if (edge.includes('e')) w = clamp(w + dx, minW, b.w - MARGIN - x)
  if (edge.includes('n')) {
    const bottom = y + h
    y = clamp(y + dy, MARGIN, bottom - minH)
    h = bottom - y
  }
  if (edge.includes('s')) h = clamp(h + dy, minH, b.h - MARGIN - y)
  return fitRect({ x, y, w, h }, minW, minH)
}
const keyStep = (event: React.KeyboardEvent, shift = true): Point | null => {
  const step = shift && event.shiftKey ? 64 : 16
  switch (event.key) {
    case 'ArrowLeft': return { x: -step, y: 0 }
    case 'ArrowRight': return { x: step, y: 0 }
    case 'ArrowUp': return { x: 0, y: -step }
    case 'ArrowDown': return { x: 0, y: step }
    default: return null
  }
}

export function useFloatingWindow(storageKey: string, options: Options): {
  ref: React.RefCallback<HTMLDivElement>
  mode: FloatMode
  setMode: (mode: FloatMode) => void
  rect: Rect
  bubble: Point
  moving: boolean
  windowStyle: React.CSSProperties
  bubbleStyle: React.CSSProperties
  move: Handlers
  resize: (edge: Edge) => Handlers
  bubbleHandlers: Handlers & { onClick: (e: React.MouseEvent) => void }
} {
  const { minW, minH, bubble: size } = options
  const element = useRef<HTMLDivElement>(null)
  const ref = useCallback((el: HTMLDivElement | null) => { element.current = el }, [])
  const { w: bubbleW, h: bubbleH } = size
  const [place, setPlace] = useState(() => loadPlace(storageKey, options))
  const current = useRef(place)
  const [moving, setMoving] = useState(false)
  const drag = useRef<Drag | null>(null)
  const flight = useRef<{ timer: number; element: HTMLElement; kind: Drag['kind'] } | null>(null)
  const dragged = useRef(false)  // swallow the click after a bubble drag

  const interrupt = () => {
    const f = flight.current
    if (!f) return
    const point = position(f.element)
    window.clearTimeout(f.timer)
    flight.current = null
    f.element.style.transition = 'none'
    f.element.style.transform = translate(point)
    const p = current.current
    current.current = f.kind === 'bubble' ? { ...p, bubble: point } : { ...p, rect: { ...p.rect, ...point } }
  }
  const update = (next: Place, commit = false) => {
    current.current = next
    setPlace(next)
    if (commit) savePlace(storageKey, next)
  }
  const setMode = (mode: FloatMode) => {
    interrupt()
    setMoving(false)
    if (element.current) element.current.style.transition = ''
    const p = current.current
    update({ ...p, mode, rect: fitRect(p.rect, minW, minH), bubble: fitBubble(p.bubble, size) }, true)
  }

  useEffect(() => {
    const fit = () => {
      const f = flight.current
      if (f) { window.clearTimeout(f.timer); f.element.style.transition = ''; flight.current = null }
      const d = drag.current
      if (d) { try { d.target.releasePointerCapture(d.id) } catch { /* pointer already gone */ }; drag.current = null }
      setMoving(false)
      const p = current.current
      const next = { ...p, rect: fitRect(p.rect, minW, minH), bubble: fitBubble(p.bubble, { w: bubbleW, h: bubbleH }) }
      const el = element.current
      if (el && p.mode !== 'full') {
        el.style.transition = 'none'
        el.style.transform = translate(p.mode === 'bubble' ? next.bubble : next.rect)
        if (p.mode === 'window') { el.style.width = `${next.rect.w}px`; el.style.height = `${next.rect.h}px` }
      }
      current.current = next
      setPlace(next)
    }
    fit()
    const viewport = window.visualViewport
    window.addEventListener('resize', fit)
    viewport?.addEventListener('resize', fit)
    return () => {
      window.removeEventListener('resize', fit)
      viewport?.removeEventListener('resize', fit)
    }
  }, [minW, minH, bubbleW, bubbleH])

  useEffect(() => () => {
    const d = drag.current
    if (d) {
      try { d.target.releasePointerCapture(d.id) } catch { /* pointer already gone */ }
    }
    drag.current = null
    if (flight.current) { window.clearTimeout(flight.current.timer); flight.current = null }
  }, [])

  const down = (event: React.PointerEvent<HTMLElement>, kind: Drag['kind'], edge?: Edge) => {
    if (event.button !== 0 || drag.current) return
    if (kind === 'move' && event.target instanceof Element) {
      const target = event.target
      if (target.closest('button, a, input, select, textarea, [role="menu"], [data-no-drag]') && !target.closest('[data-float-grip]')) return
    }
    if (kind === 'resize') { event.preventDefault(); event.stopPropagation() }
    // A move captures only once it is a real drag: an earlier capture would take the click from whatever sits under the pointer.
    if (kind !== 'move') try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* pointer already gone */ }
    if (kind === 'bubble') dragged.current = false
    const interrupted = !!flight.current
    interrupt()
    const el = element.current
    if (!el) return
    const point = position(el)
    el.style.transition = 'none'
    el.style.transform = translate(point)
    const p = current.current
    const next = kind === 'bubble' ? { ...p, bubble: point } : { ...p, rect: { ...p.rect, ...point } }
    current.current = next
    const fitted = kind === 'bubble' ? fitBubble(point, size) : fitRect(next.rect, minW, minH)
    const raw = { x: fitted.x + (point.x - fitted.x) * 4, y: fitted.y + (point.y - fitted.y) * 4 }
    drag.current = { id: event.pointerId, target: event.currentTarget, element: el, kind, edge, x: event.clientX, y: event.clientY, rect: { ...next.rect, ...(kind === 'move' ? raw : point) }, bubble: kind === 'bubble' ? raw : next.bubble, moved: kind === 'resize', caught: interrupted, vx: 0, vy: 0, lastX: event.clientX, lastY: event.clientY, time: event.timeStamp }
    if (interrupted) {
      if (kind === 'move') try { event.currentTarget.setPointerCapture(event.pointerId) } catch { /* pointer already gone */ }
      setMoving(true)
    }
    if (kind === 'resize') setMoving(true)
  }
  const pointerMove = (event: React.PointerEvent<HTMLElement>) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    const dx = event.clientX - d.x
    const dy = event.clientY - d.y
    if (!d.moved && Math.hypot(dx, dy) < 6) return
    if (!d.moved && d.kind === 'move') try { d.target.setPointerCapture(d.id) } catch { /* pointer already gone */ }
    if (!d.moved) { d.moved = true; setMoving(true) }
    if (event.clientX !== d.lastX || event.clientY !== d.lastY) {
      const dt = Math.max(1, event.timeStamp - d.time)
      d.vx = (dt > 100 ? 0 : d.vx * 0.4) + (event.clientX - d.lastX) / dt * 0.6
      d.vy = (dt > 100 ? 0 : d.vy * 0.4) + (event.clientY - d.lastY) / dt * 0.6
      d.lastX = event.clientX; d.lastY = event.clientY; d.time = event.timeStamp
    }
    const p = current.current
    let next: Place
    if (d.kind === 'bubble') {
      const point = { x: d.bubble.x + dx, y: d.bubble.y + dy }
      next = { ...p, bubble: elastic(point, fitBubble(point, size)) }
    } else if (d.kind === 'resize' && d.edge) next = { ...p, rect: resizeRect(d.rect, d.edge, dx, dy, minW, minH) }
    else {
      const rect = { ...d.rect, x: d.rect.x + dx, y: d.rect.y + dy }
      next = { ...p, rect: { ...rect, ...elastic(rect, fitRect(rect, minW, minH)) } }
    }
    current.current = next
    d.element.style.transform = translate(d.kind === 'bubble' ? next.bubble : next.rect)
    if (d.kind === 'resize') { d.element.style.width = `${next.rect.w}px`; d.element.style.height = `${next.rect.h}px` }
  }
  const finish = (event: React.PointerEvent<HTMLElement>, cancel = false) => {
    const d = drag.current
    if (!d || d.id !== event.pointerId) return
    if (!cancel) pointerMove(event)
    drag.current = null
    try { d.target.releasePointerCapture(d.id) } catch { /* pointer already gone */ }
    if (!d.moved && !d.caught) { d.element.style.transition = ''; return }
    const p = current.current
    const carry = !cancel && !reduced() && event.timeStamp - d.time <= 100 && d.kind !== 'resize'
    const vx = carry ? d.vx : 0, vy = carry ? d.vy : 0
    const from = d.kind === 'bubble' ? p.bubble : p.rect
    let next = p
    if (d.kind === 'bubble') {
      dragged.current = true
      const point = fitBubble({ x: from.x + vx * 180, y: from.y + vy * 180 }, size)
      const b = bounds(), right = Math.max(MARGIN, b.w - MARGIN - size.w)
      next = { ...p, bubble: { ...point, x: point.x - MARGIN < right - point.x ? MARGIN : right } }
    } else next = { ...p, rect: fitRect({ ...p.rect, x: from.x + vx * 120, y: from.y + vy * 120 }, minW, minH) }
    const to = d.kind === 'bubble' ? next.bubble : next.rect
    const distance = Math.hypot(to.x - from.x, to.y - from.y), speed = Math.hypot(vx, vy)
    const ms = reduced() || distance < 0.5 ? 0 : clamp(distance / Math.max(0.5, speed), 200, 380)
    const curve = d.kind === 'bubble' && speed > 0.5 ? 'cubic-bezier(.34,1.3,.64,1)' : 'cubic-bezier(.32,.72,0,1)'
    // Flush the tracked position before the browser sees the release target.
    void d.element.offsetWidth
    d.element.style.transition = ms ? `transform ${ms}ms ${curve}` : 'none'
    d.element.style.transform = translate(to)
    current.current = next
    const done = () => {
      flight.current = null
      d.element.style.transition = ''
      update(next, true)
      setMoving(false)
    }
    if (ms) flight.current = { element: d.element, kind: d.kind, timer: window.setTimeout(done, ms) }
    else done()
  }
  const handlers = (kind: Drag['kind'], edge?: Edge): Handlers => ({
    onPointerDown: event => down(event, kind, edge),
    onPointerMove: pointerMove,
    onPointerUp: event => finish(event),
    onPointerCancel: event => finish(event, true),
    onKeyDown: event => {
      const step = keyStep(event, kind !== 'bubble')
      if (!step) return
      if (kind === 'resize' && (!edge || (step.x !== 0 && !/[ew]/.test(edge)) || (step.y !== 0 && !/[ns]/.test(edge)))) return
      event.preventDefault()
      interrupt()
      setMoving(false)
      const el = element.current
      if (el) el.style.transition = reduced() ? 'none' : 'transform 220ms cubic-bezier(.32,.72,0,1)'
      const p = current.current
      if (kind === 'bubble') update({ ...p, bubble: fitBubble({ x: p.bubble.x + step.x, y: p.bubble.y + step.y }, size) }, true)
      else if (kind === 'resize' && edge) update({ ...p, rect: resizeRect(p.rect, edge, step.x, step.y, minW, minH) }, true)
      else update({ ...p, rect: fitRect({ ...p.rect, x: p.rect.x + step.x, y: p.rect.y + step.y }, minW, minH) }, true)
    },
  })

  useLayoutEffect(() => {
    const el = element.current
    if (!el || (!drag.current && !flight.current)) return
    const p = current.current
    el.style.transform = translate(p.mode === 'bubble' ? p.bubble : p.rect)
    if (p.mode === 'window') { el.style.width = `${p.rect.w}px`; el.style.height = `${p.rect.h}px` }
  })

  const { mode, rect, bubble } = place
  return {
    ref, mode, setMode, rect, bubble, moving,
    windowStyle: mode === 'full' ? {} : { transform: translate(rect), width: rect.w, height: rect.h },
    bubbleStyle: { transform: translate(bubble), width: size.w, height: size.h },
    move: handlers('move'),
    resize: edge => handlers('resize', edge),
    bubbleHandlers: {
      ...handlers('bubble'),
      onClick: event => {
        if (moving || dragged.current) { dragged.current = false; event.preventDefault(); return }
        setMode('window')
      },
    },
  }
}
