import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { KeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ExternalLink, GripHorizontal, PanelRightOpen, X } from 'lucide-react'
import { ATTACH_REFUSED, BrowserPanel } from './BrowserPanel'
import type { Api, BrowserId, Pane } from './api'
import { sendToComposer } from './chatDrop'
import type { PanelBodyProps } from './panels'
import './BrowserWindow.css'

const FLOAT_KEY = 'tether-browser-floating'
const RECT_KEY = 'tether-browser-window'
export const BROWSER_WINDOW = 'tether-browser-window'
const WINDOW_NAME = 'tether-browser'
const BLOCKED = 'Your browser blocked the new window. Allow pop-ups for this site to open the browser in its own window.'
const DID_NOT_OPEN = 'The browser window did not open.'
const MIN = { width: 360, height: 320 }
type Rect = { x: number; y: number; width: number; height: number }
type Placement = 'dock' | 'float' | 'window'
type Theme = 'light' | 'dark' | 'system'
type Labels = { id: string; label: string }[]
export type BrowserWatch = { tab: string; pane: string; browser?: BrowserId }
type Target = { id: string; pane: string | null; label: string; focusedAt: number; theme: Theme }
export type BrowserWindowMessage =
  | { type: 'hello' | 'focus' }
  | { type: 'dock'; to?: string }
  | ({ type: 'target' } & Target)
  | { type: 'labels'; labels: Labels }
  | ({ type: 'watch' } & BrowserWatch)
  | { type: 'attach'; req: string; to: string; pane: string; files: File[] }
  | { type: 'attached'; req: string; ok: boolean; error?: string }

function load<T>(key: string, fallback: T): T {
  try { const text = localStorage.getItem(key); return text ? JSON.parse(text) as T : fallback } catch { return fallback }
}
function save(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode */ }
}
// Keep the whole window on screen, so its title bar and resize corner stay in reach.
function clamp(rect: Rect): Rect {
  const width = Math.min(Math.max(MIN.width, rect.width), innerWidth - 16)
  const height = Math.min(Math.max(MIN.height, rect.height), innerHeight - 16)
  return { width, height, x: Math.min(Math.max(8, rect.x), innerWidth - width - 8), y: Math.min(Math.max(8, rect.y), innerHeight - height - 8) }
}
const defaultRect = (): Rect => clamp({ width: 480, height: Math.min(760, innerHeight - 96), x: innerWidth - 480 - 24, y: 72 })

const storedPlacement = () => load(FLOAT_KEY, false) ? 'float' : 'dock'
const supportsWindow = () => !!navigator.locks && typeof BroadcastChannel !== 'undefined'
const pointerDesktop = () => navigator.maxTouchPoints === 0
let state: { placement: Placement; ready: boolean; status: string } = { placement: storedPlacement(), ready: !supportsWindow(), status: '' }
const listeners = new Set<() => void>()
function show(placement: Placement, status = '') {
  state = { placement, status, ready: true }
  listeners.forEach(listener => listener())
}
function placeHere(placement: 'dock' | 'float', status = '') {
  save(FLOAT_KEY, placement === 'float')
  show(placement, status)
}
const usePlacement = () => useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener) } }, () => state)

type MainWindow = { pane: string | null; label: string; labels: Labels; theme: Theme; onDock: () => void }
let controls: { open: (size: DOMRect) => void; focus: () => void; dock: () => void; watch: (detail: BrowserWatch) => void } | null = null

function connectMainWindow(current: () => MainWindow) {
  const id = crypto.randomUUID()
  const channel = new BroadcastChannel(BROWSER_WINDOW)
  const lifetime = new AbortController()
  const post = (message: BrowserWindowMessage) => channel.postMessage(message)
  const focusTime = () => performance.timeOrigin + performance.now()
  let focusedAt = document.hasFocus() ? focusTime() : 0
  let popup: Window | null = null
  let opening: number | undefined
  let waiting = false
  let pendingWatch: BrowserWatch | undefined
  const target = () => {
    const { pane, label, theme } = current()
    post({ type: 'target', id, pane, label, focusedAt, theme })
  }
  const labels = () => post({ type: 'labels', labels: current().labels })
  const onFocus = () => { focusedAt = focusTime(); target() }
  const forgetTarget = () => post({ type: 'target', id, pane: null, label: '', focusedAt: -1, theme: current().theme })

  function waitForClose() {
    if (waiting) return
    waiting = true
    void navigator.locks.request(BROWSER_WINDOW, { signal: lifetime.signal }, () => {
      waiting = false
      popup = null
      show(storedPlacement())
    }).catch(error => {
      if (!lifetime.signal.aborted) {
        console.error('Cannot watch the browser window:', error)
        waiting = false
        show(storedPlacement())
      }
    })
  }
  function dock(to?: string) {
    save(FLOAT_KEY, false)
    // Keep the main view unmounted until the pop-out releases its lock.
    if (state.placement !== 'window') show('dock')
    if (to === id) current().onDock()
  }
  channel.onmessage = (event: MessageEvent<BrowserWindowMessage>) => {
    const message = event.data
    if (message.type === 'hello') {
      window.clearTimeout(opening)
      opening = undefined
      show('window')
      waitForClose()
      target()
      labels()
      if (pendingWatch) { post({ type: 'watch', ...pendingWatch }); pendingWatch = undefined }
    } else if (message.type === 'dock') {
      dock(message.to)
    } else if (message.type === 'attach' && message.to === id) {
      const ok = message.pane === current().pane && sendToComposer(message.pane, message.files)
      post({ type: 'attached', req: message.req, ok, error: ok ? undefined : ATTACH_REFUSED })
    }
  }
  window.addEventListener('focus', onFocus, { signal: lifetime.signal })
  window.addEventListener('pagehide', forgetTarget, { signal: lifetime.signal })
  window.addEventListener('pageshow', target, { signal: lifetime.signal })
  target()
  labels()
  void navigator.locks.query().then(({ held }) => {
    if (lifetime.signal.aborted || opening !== undefined || waiting) return
    if (held?.some(lock => lock.name === BROWSER_WINDOW)) {
      show('window')
      waitForClose()
    } else show(storedPlacement())
  }, () => { if (!lifetime.signal.aborted && !state.ready) show(storedPlacement()) })

  controls = {
    open(size) {
      if (state.placement === 'window') { controls?.focus(); return }
      focusedAt = focusTime()
      const width = Math.max(480, Math.round(size.width)), height = Math.max(600, Math.round(size.height))
      popup = window.open('/chat/?browser=window', WINDOW_NAME, `popup,width=${width},height=${height}`)
      if (!popup) { placeHere('float', BLOCKED); return }
      show('window')
      opening = window.setTimeout(() => {
        opening = undefined
        popup?.close()
        popup = null
        pendingWatch = undefined
        placeHere('dock', DID_NOT_OPEN)
      }, 10_000)
    },
    focus() {
      if (popup && !popup.closed) popup.focus()
      // A separate Chrome tab can create an empty window when it looks up a name it did not open.
      else post({ type: 'focus' })
    },
    dock() {
      post({ type: 'dock', to: id })
      dock(id)
    },
    watch(detail) {
      if (opening !== undefined) pendingWatch = detail
      else post({ type: 'watch', ...detail })
      if (popup && !popup.closed) popup.focus()
    },
  }
  return {
    target, labels,
    close() {
      lifetime.abort()
      window.clearTimeout(opening)
      channel.close()
      controls = null
    },
  }
}

// eslint-disable-next-line react-refresh/only-export-components -- App and BrowserDock share placement in this module.
export function useBrowserWindow(main: MainWindow) {
  const latest = useRef(main)
  const link = useRef<ReturnType<typeof connectMainWindow>>(null)
  useLayoutEffect(() => { latest.current = main })
  useEffect(() => {
    if (!supportsWindow()) return
    const connection = connectMainWindow(() => latest.current)
    link.current = connection
    return () => { connection.close(); link.current = null }
  }, [])
  useEffect(() => { link.current?.target() }, [main.pane, main.label, main.theme])
  useEffect(() => { link.current?.labels() }, [main.labels])
  return usePlacement().placement === 'window'
}

// eslint-disable-next-line react-refresh/only-export-components -- App forwards its Watch event to the same channel.
export function forwardBrowserWatch(detail: BrowserWatch) {
  if (state.placement !== 'window' || !controls) return false
  controls.watch(detail)
  return true
}
function popOut(element: Element | null | undefined) {
  if (pointerDesktop() && element && controls) controls.open(element.getBoundingClientRect())
  else placeHere('float')
}
function WindowStatus({ status }: { status: string }) {
  return status ? <p className="browser-placement-status" role="status">{status}</p> : null
}

/** The Browser panel body: the browser, or a note when it is popped out into its own window. */
export function BrowserDock(props: PanelBodyProps) {
  const { placement, ready, status } = usePlacement()
  if (!ready) return null
  if (placement === 'dock') return <>
    <WindowStatus status={status} />
    <BrowserPanel {...props} onPopOut={event => popOut(event.currentTarget.closest('.browser-panel'))} />
  </>
  if (placement === 'window') return <div className="panel-empty browser-away">
    <p>The browser is open in its own window.</p>
    <button type="button" className="browser-text-button" onClick={() => controls?.focus()}>Show window</button>
    <button type="button" className="browser-text-button" onClick={() => controls?.dock()}>Put it back here</button>
  </div>
  return <div className="panel-empty browser-away">
    <p>The browser is floating in Tether.</p>
    <button type="button" className="browser-text-button" onClick={() => placeHere('dock')}>Put it back here</button>
  </div>
}

/** The popped-out browser window. The app mounts it once, beside the workspace.
 * onDock: open the side Browser panel if it is closed. onClose: close the side Browser panel if it is open. */
export function BrowserFloat({ api, pane, onDock, onClose }: { api: Api; pane: Pane; onDock: () => void; onClose: () => void }) {
  const { placement, ready, status } = usePlacement()
  if (!ready || placement !== 'float') return null
  return createPortal(<BrowserWindow onDock={() => { placeHere('dock'); onDock() }} onClose={() => { placeHere('dock'); onClose() }}>
    <WindowStatus status={status} />
    <BrowserPanel api={api} pane={pane} folder={pane.dir || null} active />
  </BrowserWindow>, document.body)
}

function BrowserWindow({ onDock, onClose, children }: { onDock: () => void; onClose: () => void; children: ReactNode }) {
  const [rect, setRect] = useState<Rect>(() => clamp(load(RECT_KEY, defaultRect())))
  const windowRef = useRef<HTMLElement>(null)
  const frame = useRef(0)
  const rectRef = useRef(rect)
  useEffect(() => { rectRef.current = rect }, [rect])
  useEffect(() => () => cancelAnimationFrame(frame.current), [])
  const drag = useRef<{ kind: 'move' | 'size'; x: number; y: number; start: Rect } | null>(null)
  useEffect(() => {
    const onResize = () => setRect(old => clamp(old))
    addEventListener('resize', onResize)
    return () => removeEventListener('resize', onResize)
  }, [])

  const start = (kind: 'move' | 'size', event: ReactPointerEvent) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest('button:not(.browser-window-grip)')) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    const el = windowRef.current
    if (el) {
      const matrix = new DOMMatrix(getComputedStyle(el).transform)
      rectRef.current = { ...rectRef.current, x: matrix.m41, y: matrix.m42 }
      el.style.transition = 'none'
      el.style.transform = `translate3d(${matrix.m41}px, ${matrix.m42}px, 0)`
    }
    drag.current = { kind, x: event.clientX, y: event.clientY, start: rectRef.current }
  }
  const move = (event: ReactPointerEvent) => {
    const now = drag.current
    if (!now) return
    const dx = event.clientX - now.x, dy = event.clientY - now.y
    const { start } = now
    const raw = now.kind === 'move' ? { ...start, x: start.x + dx, y: start.y + dy }
      : { ...start, width: start.width + dx, height: start.height + dy }
    const bounded = clamp(raw)
    const elastic = (value: number, edge: number) => edge + (value - edge) * 0.25
    rectRef.current = now.kind === 'move' ? { ...start, x: elastic(raw.x, bounded.x), y: elastic(raw.y, bounded.y) }
      : { ...start, width: elastic(raw.width, Math.min(bounded.width, innerWidth - start.x - 8)), height: elastic(raw.height, Math.min(bounded.height, innerHeight - start.y - 8)) }
    if (!frame.current) frame.current = requestAnimationFrame(() => {
      frame.current = 0
      const el = windowRef.current, next = rectRef.current
      if (el) { el.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`; el.style.width = `${next.width}px`; el.style.height = `${next.height}px` }
    })
  }
  const end = () => {
    if (!drag.current) return
    cancelAnimationFrame(frame.current); frame.current = 0
    const current = rectRef.current
    const next = clamp(drag.current.kind === 'size' ? { ...current, width: Math.min(current.width, innerWidth - current.x - 8), height: Math.min(current.height, innerHeight - current.y - 8) } : current)
    drag.current = null
    if (windowRef.current) {
      windowRef.current.style.transition = `transform ${matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 200}ms cubic-bezier(.32,.72,0,1)`
      windowRef.current.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`
      windowRef.current.style.width = `${next.width}px`; windowRef.current.style.height = `${next.height}px`
    }
    rectRef.current = next; setRect(next); save(RECT_KEY, next)
  }
  // Keyboard: arrows move the window from its grip; with Shift they resize it.
  const onKey = (event: KeyboardEvent) => {
    const step = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] }[event.key]
    if (!step) return
    event.preventDefault()
    if (windowRef.current) windowRef.current.style.transition = 'none'
    const old = rectRef.current
    const next = clamp(event.shiftKey ? { ...old, width: old.width + step[0], height: old.height + step[1] } : { ...old, x: old.x + step[0], y: old.y + step[1] })
    setRect(next); save(RECT_KEY, next)
  }

  return <section ref={windowRef} className="browser-window" role="dialog" aria-label="Browser" style={{ left: 0, top: 0, transform: `translate3d(${rect.x}px, ${rect.y}px, 0)`, width: rect.width, height: rect.height }}>
    <header className="browser-window-head" onPointerDown={event => start('move', event)} onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
      <button type="button" className="browser-window-grip" aria-label="Move the browser window. Arrow keys move it, Shift with arrows resizes it." onKeyDown={onKey}><GripHorizontal size={16} /></button>
      <strong>Browser</strong>
      {pointerDesktop() && <button type="button" className="browser-icon" aria-label="Open in its own window" title="Open in its own window" onClick={() => popOut(windowRef.current)}><ExternalLink size={17} /></button>}
      <button type="button" className="browser-icon" aria-label="Put the browser back in the side column" title="Put back in the side column" onClick={onDock}><PanelRightOpen size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Close the browser window" title="Close" onClick={onClose}><X size={17} /></button>
    </header>
    {children}
    <div className="browser-window-size" aria-hidden="true" onPointerDown={event => start('size', event)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} />
  </section>
}
