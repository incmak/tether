import { useEffect } from 'react'
import { WORKSPACE_QUERY } from './workspace'

// UI usage (server/ui_usage.py): which controls people click, which menus and dialogs they open, and how many clicks
// that took. The log stays on this server. It names a control by the menu, dialog or labelled part of the page it is
// in and by its accessible name. It never sends typed text, and in lists of items (tabs, sessions, messages, files)
// it sends no visible text: only the aria-label or title, or "(item)". The server keeps only the words of the UI code
// from each name, so "Restore <tab name>" is stored as "Restore …".
export type UsageEvent = { k: 'click' | 'seen' | 'open'; r: string; l: string; c: string; d: number; n: number; j: string; w: 'phone' | 'wide'; t: number; g: string }
/** Settings sends this, with the new generation as detail, after it clears the log: the page drops its queue. */
export const USAGE_CLEARED = 'tether-ui-usage-cleared'

const CONTROL = 'button, a[href], summary, select, input[type="checkbox"], input[type="radio"], [role="button"], [role="link"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="tab"], [role="option"], [role="switch"], [role="checkbox"], [role="radio"], [role="treeitem"]'
// Menus, dialogs and popovers. Each one adds a level of depth.
const LAYER = 'dialog[open], [role="dialog"], [role="alertdialog"], [role="menu"], [popover]'
// A row in a list of the person's own things. Its visible text is theirs, so it is never sent.
const ITEM = '[data-ui-item], [data-id], [data-pane]:not(.chat-panel), [data-msg-id], [data-path], [data-item], [data-backlog-row], [data-key], .messages'
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

const MAX_ROW_NODES = 500
const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
// The texts each row shows, kept until the row changes. Rows are small; MAX_ROW_NODES bounds a long one.
let rowTexts = new WeakMap<Element, RegExp | null>()
function shownText(item: Element) {
  if (rowTexts.has(item)) return rowTexts.get(item)!
  const shown = new Set<string>()
  const walker = document.createTreeWalker(item, NodeFilter.SHOW_TEXT)
  let visited = 0
  for (let node = walker.nextNode(); node && visited < MAX_ROW_NODES; node = walker.nextNode(), visited++) {
    const text = (node.textContent ?? '').replace(/\s+/g, ' ').trim()
    if (/\p{L}/u.test(text)) shown.add(text)
  }
  // Longest first, whole words only: the row "A" makes "Restore A" -> "Restore …" but leaves "Add" alone.
  const pattern = shown.size ? new RegExp(`(?<![\\p{L}\\p{N}])(?:${[...shown].sort((a, b) => b.length - a.length).map(escape).join('|')})(?![\\p{L}\\p{N}])`, 'gu') : null
  rowTexts.set(item, pattern)
  return pattern
}

/** Replace each text that the row shows (a tab title, a file name) with "…": "Restore Fix login" -> "Restore …". */
export function withoutRowText(label: string, item: Element) {
  const pattern = shownText(item)
  return (pattern ? label.replace(pattern, '…') : label).replace(/…(?:[^\p{L}\p{N}]*…)+/gu, '…')
}

/** The control's name. In a list item, only a name the code sets (aria-label or title), never the row's text. */
export function labelOf(element: Element) {
  const fixed = element.getAttribute('data-ui') || element.getAttribute('aria-label') || labelledBy(element) || element.getAttribute('title')
  const item = element.closest(ITEM)
  if (item) return clean(withoutRowText(fixed ?? '', item.matches('.messages') ? element.closest('[data-msg-id]') ?? element : item)) || '(item)'
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

/** On screen now: in the viewport, and on top at the middle of its visible part (not under a dialog, not scrolled away
 * inside a list). A checkbox hidden under its styled label counts when the label is on top. */
export function onScreen(element: Element) {
  if (!visible(element)) return false
  const box = element.getBoundingClientRect()
  const left = Math.max(box.left, 0), right = Math.min(box.right, window.innerWidth)
  const top = Math.max(box.top, 0), bottom = Math.min(box.bottom, window.innerHeight)
  if (right <= left || bottom <= top) return false
  const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2)
  if (!hit) return false
  if (element.contains(hit)) return true
  return element instanceof HTMLInputElement && [...(element.labels ?? [])].some(label => label.contains(hit))
}

/** The menus and dialogs open now, one per menu: a popover that only wraps a menu counts once, as the menu. */
export function openLayers() {
  const layers = [...document.querySelectorAll(LAYER)].filter(layer => (!layer.matches('[popover]') || layer.matches(':popover-open')) && visible(layer))
  return layers.filter(outer => !layers.some(inner => inner !== outer && outer.contains(inner) && (outer.textContent ?? '').trim() === (inner.textContent ?? '').trim()))
}

/** gen: the log generation the page knows now, '' before the first answer. Nothing is logged without one, so an event
 * never carries a generation newer than the log it happened in. */
export function startUsageLog(send: (events: UsageEvent[]) => void, wide: () => boolean, gen: () => string): () => void {
  const queue: UsageEvent[] = []
  let visit = ''
  let lastInput = 0
  let lastClick = 0
  let lastRegion = ''
  let chain = 0
  let seen = new Set<string>()
  // The menus and dialogs open now, in the order they opened, and the clicks it took to open each one.
  let stack: Element[] = []
  const reached = new WeakMap<Element, number>()
  let scanTimer: ReturnType<typeof setTimeout> | undefined
  let idle: number | undefined
  let hiddenAt = 0
  let stopped = false

  const newVisit = () => {
    visit = Math.random().toString(36).slice(2, 12)
    seen = new Set()
    chain = 0
  }
  const add = (event: Omit<UsageEvent, 'j' | 'w' | 't' | 'g'>) => {
    const g = gen()
    if (!g) return
    queue.push({ ...event, j: visit, w: wide() ? 'wide' : 'phone', t: Date.now(), g })
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

  // Update the stack of open menus and dialogs. A new one gets an 'open' event with the clicks that reached it.
  const sync = () => {
    const layers = openLayers()
    stack = stack.filter(layer => layers.includes(layer))
    const byKeyboard = Date.now() - lastClick > KEYBOARD
    for (const layer of layers) {
      if (stack.includes(layer)) continue
      stack.push(layer)
      reached.set(layer, byKeyboard ? 0 : chain)
      add({ k: 'open', r: byKeyboard ? 'Keyboard' : lastRegion, l: layerName(layer), c: '', d: stack.length, n: byKeyboard ? 0 : chain })
    }
  }
  /** The open menu or dialog the element is in, and its depth: 1 for the first one open, 2 for one opened from it. */
  const layerOf = (element: Element) => {
    for (let index = stack.length - 1; index >= 0; index--) if (stack[index].contains(element)) return { layer: stack[index], depth: index + 1 }
    return element.closest(LAYER) ? { layer: null, depth: Math.max(1, stack.length) } : { layer: null, depth: 0 }
  }

  // Menus and dialogs that opened since the last scan, and the controls now on screen that this visit has not seen.
  const scan = () => {
    scanTimer = idle = undefined
    if (stopped || document.visibilityState !== 'visible') return
    if (!gen()) { later(1000); return }  // wait for the generation, or the seen controls would be marked but not sent
    sync()
    rowTexts = new WeakMap()
    const top = stack[stack.length - 1]
    const controls = top ? top.querySelectorAll(CONTROL) : document.querySelectorAll(CONTROL)
    for (const control of controls) {
      if (seen.size >= MAX_SEEN) break
      if (!top && control.closest(LAYER)) continue
      if (control.matches(':disabled') || !onScreen(control)) continue
      const region = regionOf(control), label = labelOf(control), kind = kindOf(control)
      const key = `${region}\n${label}\n${kind}`
      if (seen.has(key)) continue
      seen.add(key)
      add({ k: 'seen', r: region, l: label, c: kind, d: layerOf(control).depth, n: 0 })
    }
  }
  const cancel = () => {
    clearTimeout(scanTimer)
    if (idle !== undefined && 'cancelIdleCallback' in window) window.cancelIdleCallback(idle)
    scanTimer = idle = undefined
  }
  const later = (delay = SCAN_DELAY) => {
    cancel()
    scanTimer = setTimeout(() => {
      scanTimer = undefined
      if ('requestIdleCallback' in window) idle = window.requestIdleCallback(scan, { timeout: 1000 })
      else scan()
    }, delay)
  }

  const onClick = (event: MouseEvent) => {
    const control = event.target instanceof Element ? event.target.closest(CONTROL) : null
    if (!control) return
    touch()
    sync()
    rowTexts = new WeakMap()
    const { layer, depth } = layerOf(control)
    // A click on the page starts a chain. A click in a menu or dialog continues from the clicks that opened it, so
    // closing a submenu and opening it again counts the same each time.
    chain = depth === 0 ? 1 : (layer ? reached.get(layer) ?? chain : chain) + 1
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
  const onScroll = () => { if (!scanTimer && idle === undefined) later(1000) }
  // Events from before the clear carry the old generation, so the server would drop them; do not send them.
  const onCleared = () => { queue.length = 0; newVisit(); later() }

  newVisit()
  lastInput = Date.now()
  later(2000)
  const timer = setInterval(flush, SEND_EVERY)
  document.addEventListener('click', onClick, { capture: true, passive: true })
  document.addEventListener('keydown', onKey, { capture: true, passive: true })
  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('pagehide', onHide)
  document.addEventListener('scroll', onScroll, { capture: true, passive: true })
  window.addEventListener(USAGE_CLEARED, onCleared)
  return () => {
    stopped = true
    flush()
    clearInterval(timer)
    cancel()
    document.removeEventListener('scroll', onScroll, { capture: true })
    window.removeEventListener(USAGE_CLEARED, onCleared)
    document.removeEventListener('click', onClick, { capture: true })
    document.removeEventListener('keydown', onKey, { capture: true })
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('pagehide', onHide)
  }
}

// The server's log generation. It changes when the log is cleared; the server keeps only events with the current one.
let generation = ''
if (typeof window !== 'undefined') {
  window.addEventListener(USAGE_CLEARED, event => { const detail = (event as CustomEvent).detail; generation = typeof detail === 'string' ? detail : '' })
}

function post(events: UsageEvent[]) {
  try {
    void fetch('/mobile/api/ui-usage', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ events, now: Date.now() }),
    }).then(response => response.ok ? response.json() : null)
      .then((body: { gen?: unknown } | null) => { if (typeof body?.gen === 'string') generation = body.gen })
      .catch(() => { /* usage is best effort */ })
  } catch { /* usage must never interrupt the app */ }
}

/** Log UI usage while on is true (the 'uiUsage' preference, on unless turned off in Settings). */
export function useUsageLog(on: boolean) {
  useEffect(() => {
    if (!on) return
    const media = window.matchMedia(WORKSPACE_QUERY)
    if (!generation) post([])  // ask for the generation; the log starts once it is known
    return startUsageLog(post, () => media.matches, () => generation)
  }, [on])
}
