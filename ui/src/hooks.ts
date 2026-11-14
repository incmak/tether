import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { errorText } from './api'
import { useBackStep } from './backNav'

// One request at a time. Abort obsolete requests and pause polling in hidden tabs.
// `initial` shows saved data at once (no loading state) while the first fetch runs.
/** streamPath: an SSE path that pushes new data. As a function, it gets the data on screen when the stream opens,
 *  so the server can skip sending that data again (B63). */
export function usePolling<T>(fetcher: (signal: AbortSignal) => Promise<T>, interval: number, streamPath?: string | ((shown: T | undefined) => string), initial?: T) {
  const [data, setData] = useState<T | undefined>(initial)
  const shown = useRef<T | undefined>(initial)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(initial === undefined)
  const active = useRef<AbortController | null>(null)
  const refresh = useCallback(async () => {
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    try {
      const result = await fetcher(controller.signal)
      if (!controller.signal.aborted) { shown.current = result; setData(result); setError('') }
    } catch (error) {
      if (!controller.signal.aborted) setError(errorText(error))
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }, [fetcher])
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>
    let stopped = false, received = 0
    let stream: EventSource | null = null
    const connect = () => {
      if (!streamPath || !('EventSource' in window) || document.hidden || stopped) return
      stream = new EventSource(`/mobile/api/${typeof streamPath === 'function' ? streamPath(shown.current) : streamPath}`)
      stream.onmessage = event => {
        try {
          const result = JSON.parse(event.data) as T
          active.current?.abort()  // an older poll must not overwrite a pushed update
          received = Date.now(); shown.current = result; setData(result); setError(''); setLoading(false)
        } catch { received = 0 }
      }
      // The server did not send the data again because the screen already shows that version. EventSource reconnects
      // with its first URL, so `have` can be older than the screen: on a mismatch, close and let tick() fetch and reopen.
      const current = stream
      current.addEventListener('same', event => {
        let version: unknown
        try { version = (JSON.parse((event as MessageEvent<string>).data) as { version?: unknown }).version } catch { version = undefined }
        if (!version || version !== (shown.current as { version?: unknown } | undefined)?.version) {
          current.close(); if (stream === current) stream = null
          received = 0
          return
        }
        active.current?.abort()  // an older fallback poll must not bring back an error banner
        received = Date.now(); setError(''); setLoading(false)
      })
      stream.addEventListener('idle', () => { if (received) received = Date.now() })
      stream.addEventListener('unavailable', () => { received = 0 })
      stream.onerror = () => { received = 0 }
    }
    const tick = async () => {
      if (stopped || document.hidden) return
      if (!received || Date.now() - received > 25000) {
        if (received) { stream?.close(); stream = null; received = 0 }
        // Fetch first, then open the stream with what is on screen: its first event is then a short `same`,
        // not the whole payload a second time on the same slow link.
        await refresh()
        if (!stream) connect()
      }
      if (!stopped) timer = setTimeout(tick, interval)
    }
    const visibility = () => {
      clearTimeout(timer); stream?.close(); stream = null; received = 0
      if (document.hidden) active.current?.abort()
      else void tick()
    }
    void tick()
    document.addEventListener('visibilitychange', visibility)
    return () => {
      stopped = true
      clearTimeout(timer); stream?.close()
      active.current?.abort()
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [refresh, interval, streamPath])
  return { data, error, loading, refresh }
}

export function useModal(open: boolean, close: () => void, drawer?: { onInterrupt: () => void }) {
  useBackStep(open, close)
  const ref = useRef<HTMLDialogElement>(null)
  const opened = useRef(0)
  const horizontal = !!drawer
  const latest = useRef({ close, interrupt: drawer?.onInterrupt })
  useLayoutEffect(() => { latest.current = { close, interrupt: drawer?.onInterrupt } })
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const suppressClick = useRef(0)
  const start = useRef<{ x: number; y: number; offset: number; last: number; at: number; speed: number; locked: boolean } | null>(null)
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
  const position = useCallback((dialog: HTMLDialogElement) => {
    const transform = getComputedStyle(dialog).transform
    const matrix = transform === 'none' ? null : new DOMMatrix(transform)
    return (horizontal ? matrix?.m41 : matrix?.m42) ?? 0
  }, [horizontal])
  const move = useCallback((dialog: HTMLDialogElement, offset: number, ms = 0) => {
    const size = horizontal ? dialog.offsetWidth : dialog.offsetHeight
    dialog.style.transition = ms ? `transform ${ms}ms cubic-bezier(.32,.72,0,1)` : 'none'
    dialog.style.transform = `translate${horizontal ? 'X' : 'Y'}(${offset}px)`
    dialog.style.setProperty('--sheet-duration', `${ms}ms`)
    dialog.style.setProperty('--sheet-progress', String(Math.max(0, Math.min(1, (horizontal ? -offset : offset) / (size || 1)))))
  }, [horizontal])
  const reset = useCallback((dialog: HTMLDialogElement) => {
    clearTimeout(timer.current); timer.current = undefined; start.current = null
    dialog.style.removeProperty('transform'); dialog.style.removeProperty('transition')
    dialog.style.removeProperty('--sheet-progress'); dialog.style.removeProperty('--sheet-duration')
  }, [])
  const finish = useCallback((dismiss: boolean, speed = 0, notify = true) => {
    const dialog = ref.current
    if (!dialog?.open) return
    clearTimeout(timer.current)
    const from = position(dialog), size = horizontal ? dialog.offsetWidth : dialog.offsetHeight
    const to = dismiss ? (horizontal ? -size : size) : 0
    const ms = reduced() || Math.abs(to - from) < 1 ? 0 : dismiss ? Math.round(Math.max(140, Math.min(320, Math.abs(to - from) / Math.max(Math.abs(speed), 1.2)))) : 220
    move(dialog, from)
    // Commit the caught position before changing the transition's destination.
    void dialog.offsetWidth
    move(dialog, to, ms)
    const done = () => {
      timer.current = undefined
      if (!dismiss) { reset(dialog); return }
      if (!notify) { dialog.close(); reset(dialog); return }
      latest.current.close()
      // A guarded close can refuse dismissal while a request is pending. Give React time to close the dialog
      // before the sheet comes back, so a normal close does not flash the sheet upward.
      timer.current = setTimeout(() => {
        timer.current = undefined
        if (!dialog.open) { reset(dialog); return }
        const back = reduced() ? 0 : 220
        move(dialog, 0, back)
        if (back) timer.current = setTimeout(() => reset(dialog), back)
        else reset(dialog)
      }, 80)
    }
    if (ms) timer.current = setTimeout(done, ms)
    else done()
  }, [horizontal, move, position, reset])
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open) {
      if (!dialog.open) { reset(dialog); dialog.showModal(); opened.current = Date.now() }
      else if (!start.current && timer.current !== undefined) finish(false)
    } else if (horizontal && dialog.open) finish(true, 0, false)
    else { dialog.close(); reset(dialog) }
  }, [open, horizontal, finish, reset])
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    const cancel = () => { if (start.current) { start.current = null; finish(false) } }
    const moveTouch = (event: TouchEvent) => {
      const from = start.current
      if (!from) return
      // An active row hold owns the touch before it reaches the drawer.
      if (horizontal && event.defaultPrevented) { cancel(); return }
      if (event.touches.length !== 1) { cancel(); return }
      const touch = event.touches[0], dx = touch.clientX - from.x, dy = touch.clientY - from.y
      const delta = horizontal ? dx : dy, across = horizontal ? dy : dx
      if (!from.locked) {
        // Claim a downward pull at once: if Chrome starts a native scroll first, later moves cannot be cancelled.
        if (!horizontal && delta > 0 && delta >= Math.abs(across)) event.preventDefault()
        if (Math.max(Math.abs(delta), Math.abs(across)) < 10) return
        if (Math.abs(delta) <= Math.abs(across) * 1.2 || (!horizontal && delta <= 0)) { cancel(); return }
        from.locked = true
      }
      event.preventDefault()
      const now = performance.now(), point = horizontal ? touch.clientX : touch.clientY
      if (now > from.at) from.speed = from.speed * 0.4 + (point - from.last) / (now - from.at) * 0.6
      from.last = point; from.at = now
      const offset = from.offset + delta
      move(dialog, (horizontal ? offset > 0 : offset < 0) ? offset * 0.25 : offset)
      suppressClick.current = Date.now() + 400
    }
    const closed = () => reset(dialog)
    dialog.addEventListener('touchmove', moveTouch, { passive: false })
    dialog.addEventListener('close', closed)
    return () => {
      clearTimeout(timer.current); timer.current = undefined
      dialog.removeEventListener('touchmove', moveTouch)
      dialog.removeEventListener('close', closed)
    }
  }, [horizontal, move, finish, reset])
  return {
    ref,
    // React passes cancel up its tree; stop it so Escape closes only the top dialog, not a dialog that holds it.
    onCancel: (event: React.SyntheticEvent) => { event.preventDefault(); event.stopPropagation(); close() },
    // A tap on the dimmed backdrop lands on the dialog itself, outside its box.
    onClick: (event: React.MouseEvent<HTMLDialogElement>) => {
      const box = event.currentTarget.getBoundingClientRect()
      if (Date.now() - opened.current > 400 && event.target === event.currentTarget && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) close()
    },
    onClickCapture: (event: React.MouseEvent<HTMLDialogElement>) => {
      if (Date.now() < suppressClick.current) { event.preventDefault(); event.stopPropagation() }
    },
    onTouchStart: (event: React.TouchEvent<HTMLDialogElement>) => {
      if (event.touches.length !== 1) { if (start.current) finish(false); start.current = null; return }
      start.current = null
      const dialog = event.currentTarget
      if (!horizontal && (!dialog.classList.contains('sheet') || dialog.classList.contains('popover') || matchMedia('(min-width: 600px)').matches)) return
      for (let node = event.target as HTMLElement | null; !horizontal && node && node !== dialog.parentElement; node = node.parentElement) if (node.scrollTop > 0) return
      const touch = event.touches[0], box = dialog.getBoundingClientRect()
      if (touch.clientX < box.left || touch.clientX > box.right || touch.clientY < box.top || touch.clientY > box.bottom) return
      const offset = position(dialog), interrupted = timer.current !== undefined
      clearTimeout(timer.current); timer.current = undefined
      move(dialog, offset)
      start.current = { x: touch.clientX, y: touch.clientY, offset: (horizontal ? offset > 0 : offset < 0) ? offset * 4 : offset, last: horizontal ? touch.clientX : touch.clientY, at: performance.now(), speed: 0, locked: false }
      if (interrupted && horizontal) latest.current.interrupt?.()
    },
    onTouchEnd: () => {
      const from = start.current; start.current = null
      if (!from) return
      const dialog = ref.current
      if (!dialog) return
      const offset = position(dialog), size = horizontal ? dialog.offsetWidth : dialog.offsetHeight
      const distance = horizontal ? -offset : offset
      // A pause before release is not a fling.
      const speed = performance.now() - from.at > 100 || reduced() ? 0 : from.speed
      finish(from.locked && (distance > size * 0.4 || (horizontal ? speed < -0.4 : speed > 0.5)), speed)
      if (from.locked) suppressClick.current = Date.now() + 400
    },
    onTouchCancel: () => { if (start.current) { start.current = null; finish(false) } },
  }
}

/** A screen point a menu opens from: the pointer for a right-click, or the button that opened it. */
export type Anchor = { x: number; y: number }
export const anchorOf = (event: React.MouseEvent): Anchor => {
  if (event.clientX || event.clientY) return { x: event.clientX, y: event.clientY }
  const box = event.currentTarget.getBoundingClientRect()  // keyboard: no pointer position
  return { x: box.left, y: box.bottom + 4 }
}

/**
 * Show a dialog as a popover next to `at`, inside the window, instead of as a sheet.
 * It moves again when its content changes size (for example a rename form). Arrow keys move between its buttons.
 */
export function usePopover(ref: RefObject<HTMLDialogElement | null>, at: Anchor | undefined, close: () => void) {
  useLayoutEffect(() => {
    const box = ref.current
    if (!box || !at) return
    const place = () => {
      const width = box.offsetWidth, height = box.offsetHeight
      box.style.left = `${Math.max(8, Math.min(at.x, innerWidth - width - 8))}px`
      box.style.top = `${at.y + height > innerHeight - 8 ? Math.max(8, at.y - height) : at.y}px`
    }
    place()
    const observer = new ResizeObserver(place)
    observer.observe(box)
    // Focus the menu itself, so no item shows a focus ring until the arrow keys move to it.
    const timer = setTimeout(() => { if (!box.querySelector('input')) box.focus() })
    return () => { observer.disconnect(); clearTimeout(timer) }
  }, [ref, at])
  if (!at) return {}
  return {
    tabIndex: -1,
    onKeyDown: (event: React.KeyboardEvent<HTMLDialogElement>) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
      const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('.action-list button:not(:disabled)')]
      if (!items.length) return
      event.preventDefault()
      const at = items.indexOf(document.activeElement as HTMLButtonElement)
      items[(at + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus()
    },
    // A right-click outside the menu closes it, like a click does.
    onContextMenu: (event: React.MouseEvent<HTMLDialogElement>) => {
      if (event.target !== event.currentTarget) return
      event.preventDefault(); close()
    },
  }
}

/** A row shows its full name as a tooltip only when the name is cut off. */
export const clipTitle = (event: React.MouseEvent<HTMLElement>) => {
  const name = event.currentTarget.querySelector<HTMLElement>('.session-title, .pane-child-name, .pane-tab-name')
  event.currentTarget.title = name && name.scrollWidth > name.clientWidth ? name.textContent ?? '' : ''
}
