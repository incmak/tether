import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import type { Screen } from './Rail'

// Tether's own history steps. Each entry Tether writes carries its place (pane and screen), how many overlays were
// open (layer), and its index in Tether's run of entries. Index 0 is the first Tether entry: Back from there would
// leave the app, so the in-app Back stops there. view names an overlay that Forward can open again (setViewOpener).
export type Place = { pane?: string; screen: Screen }
type NavState = { tether: 1; layer: number; place?: Place; index?: number; view?: string }
type Overlay = { id: number; close: () => void; layer: number; noEntry: boolean }

const overlays: Overlay[] = []
let pendingBacks = 0
const queue: (() => void)[] = []
let placeHandler: ((place: Place) => void) | null = null
let viewOpener: ((view: string) => boolean) | null = null
let reopening: { view: string; layer: number } | null = null
let currentPlace: Place | null = null
let nextId = 0
let listening = false
let index = 0
let top = 0
// A Forward that steps over closed overlays and finds no live entry after them comes back to where it started.
// Those entries are dead, so Forward turns off there.
let skip: { start: number; bounced: boolean } | null = null
const watchers = new Set<() => void>()
// In-app Back and Forward requests (negative: Back). A traversal is async, so a second click or key press before its
// popstate must wait and then check the bounds again; else two quick Backs from index 1 would leave Tether.
let wanted = 0
let moving = false
let movingTimer: ReturnType<typeof setTimeout> | undefined

const state = () => (typeof history === 'undefined' ? null : history.state) as Partial<NavState> | null
const equal = (a: Place | null | undefined, b: Place | null) => a?.pane === b?.pane && a?.screen === b?.screen
const activated = () => navigator.userActivation?.isActive !== false
const TOP_KEY = 'tether-nav-top'

function saveTop() {
  try { sessionStorage.setItem(TOP_KEY, String(top)) } catch { /* Private mode: Forward is lost after a reload only. */ }
}
function readTop() {
  try { return Number(sessionStorage.getItem(TOP_KEY)) || 0 } catch { return 0 }
}

let snapshot = { back: false, forward: false }
function changed() {
  const next = { back: index > 0, forward: index < top }
  if (next.back === snapshot.back && next.forward === snapshot.forward) return
  snapshot = next
  watchers.forEach(watch => watch())
}

function push(next: NavState) {
  index = (state()?.index ?? index) + 1
  top = index
  saveTop()
  history.pushState({ ...next, index }, '')
  changed()
}
function replace(next: NavState) {
  history.replaceState({ ...next, index: state()?.index ?? index }, '')
}

function traverse(step: -1 | 1) {
  moving = true
  clearTimeout(movingTimer)
  // A traversal that never ends in popstate (the browser refused it) must not block later steps.
  movingTimer = setTimeout(() => { moving = false; flush(); drain() }, 1500)
  if (step < 0) history.back()
  else history.forward()
}

function drain() {
  if (moving || pendingBacks > 0) return
  if (wanted < 0 && index > 0) { wanted++; traverse(-1) }
  else if (wanted > 0 && index < top) { wanted--; traverse(1) }
  else wanted = 0
}

// History writes wait while a Back or a traversal is in flight, so that they act on the entry it lands on.
function run(fn: () => void) {
  if (pendingBacks > 0 || moving) queue.push(fn)
  else fn()
}

function flush() {
  while (!moving && pendingBacks === 0 && queue.length) queue.shift()!()
}

function back() {
  run(() => { pendingBacks++; history.back() })
}

function listen() {
  if (listening) return
  listening = true
  index = state()?.index ?? 0
  top = Math.max(index, readTop())
  window.addEventListener('popstate', event => {
    moving = false
    clearTimeout(movingTimer)
    onPop(event)
    flush()
    drain()
  })
  function onPop(event: PopStateEvent) {
    const next = event.state as Partial<NavState> | null
    const from = index
    reopening = null
    index = next?.index ?? 0
    changed()
    if (pendingBacks > 0) {
      pendingBacks--
      return
    }
    const layer = next?.layer ?? 0
    // Remove first, so the close callback cannot go back a second time.
    while (overlays.length > layer) overlays.pop()!.close()
    if (layer > overlays.length) {
      // Forward into a view that Tether can open again: open it on this entry.
      if (index > from && layer === overlays.length + 1 && next?.view && viewOpener) {
        reopening = { view: next.view, layer }
        if (viewOpener(next.view)) return
        reopening = null
      }
      // An overlay that is closed now: step over its entry in the same direction.
      if (index > from && !skip) skip = { start: from, bounced: false }
      if (index > from && index < top && !skip?.bounced) traverse(1)
      else {
        if (skip && index > from) skip.bounced = true
        traverse(-1)
      }
      return
    }
    if (skip) {
      if (skip.bounced && index === skip.start) { top = index; saveTop(); changed() }
      skip = null
    }
    if (layer === 0 && next?.place && !equal(next.place, currentPlace)) {
      currentPlace = next.place
      placeHandler?.(next.place)
    }
  }
}

/** keepForward: when there are Forward steps, add no entry, so opening this overlay (the command menu) keeps them. */
export function openOverlay(close: () => void, view?: string, keepForward = false): number {
  listen()
  const overlay: Overlay = { id: ++nextId, close, layer: overlays.length + 1, noEntry: true }
  const count = overlays.length
  overlays.push(overlay)
  // Forward opened this view again: its entry is already there.
  if (view && reopening?.view === view && reopening.layer === overlay.layer && state()?.layer === overlay.layer) {
    reopening = null
    overlay.noEntry = false
    return overlay.id
  }
  if (keepForward && index < top) return overlay.id
  if (activated()) run(() => {
    if (!overlays.includes(overlay)) return
    const next: NavState = { ...state(), tether: 1, layer: overlay.layer, place: currentPlace ?? undefined, view }
    if ((state()?.layer ?? 0) > count) replace(next)
    else push(next)
    overlay.noEntry = false
  })
  return overlay.id
}

export function closeOverlay(id: number) {
  const at = overlays.findIndex(overlay => overlay.id === id)
  if (at < 0) return
  const [overlay] = overlays.splice(at, 1)
  run(() => {
    if (!overlay.noEntry && state()?.layer === overlay.layer) back()
  })
}

export function setPlace(place: Place, { push: wantPush }: { push: boolean }) {
  listen()
  if (equal(place, currentPlace)) return
  const first = currentPlace === null
  const canPush = wantPush && overlays.length === 0 && activated()
  currentPlace = place
  run(() => {
    const previous = state()
    const next: NavState = { ...previous, tether: 1, layer: 0, place, view: undefined }
    if (first) replace(next)
    else if (canPush) {
      if ((previous?.layer ?? 0) > overlays.length) replace(next)
      else push(next)
    } else if ((previous?.layer ?? 0) === 0) replace(next)
  })
}

export function setPlaceHandler(fn: ((place: Place) => void) | null) {
  listen()
  placeHandler = fn
}

/** fn opens the named view and returns true, or returns false when it cannot (Forward then steps over it). */
export function setViewOpener(fn: ((view: string) => boolean) | null) {
  listen()
  viewOpener = fn
}

/** In-app Back and Forward. They never leave Tether: Back stops at Tether's first entry. */
// They wait for a pending close (an overlay that goes back on its own entry), so they step from where it lands.
export function goBack() {
  listen()
  wanted--
  run(drain)
}
export function goForward() {
  listen()
  wanted++
  run(drain)
}

export function useNavAvailability() {
  return useSyncExternalStore(watch => { listen(); changed(); watchers.add(watch); return () => watchers.delete(watch) }, () => snapshot, () => snapshot)
}

export type StepOptions = { view?: string; keepForward?: boolean }

export function useBackStep(open: boolean, close: () => void, options?: StepOptions) {
  const latest = useRef(close)
  useLayoutEffect(() => { latest.current = close }, [close])
  const step = useRef(options)
  useLayoutEffect(() => { step.current = options })
  useEffect(() => {
    if (!open) return
    const id = openOverlay(() => latest.current(), step.current?.view, step.current?.keepForward)
    return () => closeOverlay(id)
  }, [open])
}
