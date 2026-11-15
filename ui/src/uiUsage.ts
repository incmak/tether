import { useEffect } from 'react'
import { WORKSPACE_QUERY } from './workspace'

// UI usage (server/ui_usage.py): which controls people click, which menus and dialogs they open, and how many clicks
// that took. The log stays on this server. It names a control by the menu, dialog or labelled part of the page it is
// in and by its accessible name. It never sends typed text, and in lists of items (tabs, sessions, messages, files)
// it sends no visible text: only a fixed name such as "Copy", or "(item)".
export type UsageEvent = { k: 'click' | 'seen' | 'open'; r: string; l: string; c: string; d: number; n: number; j: string; w: 'phone' | 'wide' }

const CONTROL = 'button, a[href], summary, select, input[type="checkbox"], input[type="radio"], [role="button"], [role="link"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="tab"], [role="option"], [role="switch"], [role="checkbox"], [role="radio"], [role="treeitem"]'
// Menus, dialogs and popovers. Each one adds a level of depth.
const LAYER = 'dialog[open], [role="dialog"], [role="alertdialog"], [role="menu"], [popover]'
// A row in a list of the person's own things. Its visible text is theirs, so it is never sent.
const ITEM = '[data-ui-item], [data-id], [data-pane], [data-msg-id], [data-path], [data-item], [data-backlog-row], [data-key], .messages'
const REGION = 'nav[aria-label], aside[aria-label], [role="toolbar"][aria-label], [role="tablist"][aria-label], [role="region"][aria-label], section[aria-label], section[aria-labelledby], header, footer'
const SEND_EVERY = 15_000
const BATCH = 100
const NEW_VISIT = 10 * 60_000  // a pause this long starts a new visit
const SCAN_DELAY = 500  // after a click, wait for a menu or dialog to open
const MAX_SEEN = 400  // controls reported as seen per visit
const KEYBOARD = 1500  // a view that opens this long after the last click opened by keyboard

const clean = (text: string | null | undefined, size = 48) => (text ?? '').replace(/\s+/g, ' ').trim().replace(/\d+/g, '#').slice(0, size)

function labelledBy(element: Element) {
  const ids = element.getAttribute('aria-labelledby')
  return ids ? ids.split(/\s+/).map(id => document.getElementById(id)?.textContent ?? '').join(' ') : ''
}

/** A menu or dialog's name: its label, or its first heading. */
export function layerName(layer: Element) {
  const heading = layer.querySelector('h1, h2, h3, legend')
  return clean(layer.getAttribute('aria-label') || labelledBy(layer) || heading?.textContent || layer.getAttribute('data-ui')) || (layer.matches('[role="menu"]') ? 'Menu' : 'Dialog')
}

function regionOf(element: Element) {
  const layer = element.closest(LAYER)
  if (layer) return layerName(layer)
  const region = element.closest(REGION)
  if (!region) return 'Page'
  return clean(region.getAttribute('aria-label') || labelledBy(region)) || (region.tagName === 'HEADER' ? 'Header' : region.tagName === 'FOOTER' ? 'Footer' : 'Page')
}

function kindOf(element: Element) {
  const role = element.getAttribute('role')
  if (role) return role
  if (element instanceof HTMLInputElement) return element.type
  return element.tagName === 'A' ? 'link' : element.tagName.toLowerCase()
}

/** The control's name. In a list item, only a name the code sets (aria-label or title), never the row's text. */
export function labelOf(element: Element) {
  const fixed = element.getAttribute('data-ui') || element.getAttribute('aria-label') || labelledBy(element) || element.getAttribute('title')
  if (element.closest(ITEM)) return clean(fixed) || '(item)'
  if (fixed) return clean(fixed)
  if (element instanceof HTMLInputElement || element instanceof HTMLSelectElement) {
    const label = element.labels?.[0]
    return clean(label?.querySelector('strong')?.textContent || label?.textContent || element.name) || kindOf(element)
  }
  const strong = element.querySelector('strong')
  return clean(strong?.textContent || element.textContent) || clean(element.querySelector('svg[class*="lucide-"]')?.getAttribute('class')?.match(/lucide-([a-z0-9-]+)/)?.[1]) || '(icon)'
}

const visible = (element: Element) => typeof (element as HTMLElement).checkVisibility === 'function'
  ? (element as HTMLElement).checkVisibility() : element.getClientRects().length > 0

function openLayers() {
  return [...document.querySelectorAll(LAYER)].filter(layer => (!layer.matches('[popover]') || layer.matches(':popover-open')) && visible(layer))
}

/** 0 on the page; in a menu or dialog, the number of menus and dialogs open now (a menu opened from a dialog is 2). */
function depthOf(element: Element, layers: Element[]) {
  return element.closest(LAYER) ? Math.max(1, layers.length) : 0
}

export function startUsageLog(send: (events: UsageEvent[]) => void, wide: () => boolean): () => void {
  const queue: UsageEvent[] = []
  let visit = ''
  let lastInput = 0
  let lastClick = 0
  let lastRegion = ''
  let chain = 0
  let seen = new Set<string>()
  let open = new WeakSet<Element>()
  let scanTimer: ReturnType<typeof setTimeout> | undefined
  let hiddenAt = 0

  const newVisit = () => {
    visit = Math.random().toString(36).slice(2, 12)
    seen = new Set()
    chain = 0
  }
  const add = (event: Omit<UsageEvent, 'j' | 'w'>) => {
    queue.push({ ...event, j: visit, w: wide() ? 'wide' : 'phone' })
    if (queue.length >= BATCH) flush()
  }
  const flush = () => {
    if (!queue.length) return
    const batch = queue.splice(0, BATCH)
    send(batch)
    if (queue.length) flush()
  }
  const touch = () => {
    const now = Date.now()
    if (!visit || now - lastInput > NEW_VISIT) newVisit()
    lastInput = now
  }

  // Menus and dialogs that opened since the last scan, and the controls now on screen that this visit has not seen.
  const scan = () => {
    scanTimer = undefined
    if (document.visibilityState !== 'visible') return
    const layers = openLayers()
    const byKeyboard = Date.now() - lastClick > KEYBOARD
    for (const layer of layers) {
      if (open.has(layer)) continue
      open.add(layer)
      add({ k: 'open', r: byKeyboard ? 'Keyboard' : lastRegion, l: layerName(layer), c: '', d: layers.length, n: byKeyboard ? 0 : chain })
    }
    const now = new WeakSet(layers)
    open = now
    const top = layers[layers.length - 1]
    const controls = top ? top.querySelectorAll(CONTROL) : document.querySelectorAll(CONTROL)
    for (const control of controls) {
      if (seen.size >= MAX_SEEN) break
      if (!top && control.closest(LAYER)) continue
      if (control.matches(':disabled') || !visible(control)) continue
      const region = regionOf(control), label = labelOf(control), kind = kindOf(control)
      const key = `${region}\n${label}\n${kind}`
      if (seen.has(key)) continue
      seen.add(key)
      add({ k: 'seen', r: region, l: label, c: kind, d: depthOf(control, layers), n: 0 })
    }
  }
  const later = (delay = SCAN_DELAY) => {
    clearTimeout(scanTimer)
    scanTimer = setTimeout(() => {
      if ('requestIdleCallback' in window) window.requestIdleCallback(scan, { timeout: 1000 })
      else scan()
    }, delay)
  }

  const onClick = (event: MouseEvent) => {
    const control = event.target instanceof Element ? event.target.closest(CONTROL) : null
    if (!control) return
    touch()
    const layers = openLayers()
    const depth = depthOf(control, layers)
    chain = depth === 0 ? 1 : chain + 1
    lastClick = Date.now()
    lastRegion = regionOf(control)
    add({ k: 'click', r: lastRegion, l: labelOf(control), c: kindOf(control), d: depth, n: chain })
    later()
  }
  const onKey = () => { touch(); later(800) }
  const onVisibility = () => {
    if (document.visibilityState === 'hidden') { hiddenAt = Date.now(); flush(); return }
    if (hiddenAt && Date.now() - hiddenAt > NEW_VISIT) newVisit()
    lastInput = Date.now()
    later(2000)
  }
  const onHide = () => flush()

  newVisit()
  lastInput = Date.now()
  later(2000)
  const timer = setInterval(flush, SEND_EVERY)
  document.addEventListener('click', onClick, { capture: true, passive: true })
  document.addEventListener('keydown', onKey, { capture: true, passive: true })
  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('pagehide', onHide)
  return () => {
    flush()
    clearInterval(timer)
    clearTimeout(scanTimer)
    document.removeEventListener('click', onClick, { capture: true })
    document.removeEventListener('keydown', onKey, { capture: true })
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('pagehide', onHide)
  }
}

function post(events: UsageEvent[]) {
  try {
    void fetch('/mobile/api/ui-usage', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ events }),
    }).catch(() => { /* usage is best effort */ })
  } catch { /* usage must never interrupt the app */ }
}

/** Log UI usage while on is true (the 'uiUsage' preference, on unless turned off in Settings). */
export function useUsageLog(on: boolean) {
  useEffect(() => {
    if (!on) return
    const media = window.matchMedia(WORKSPACE_QUERY)
    return startUsageLog(post, () => media.matches)
  }, [on])
}
