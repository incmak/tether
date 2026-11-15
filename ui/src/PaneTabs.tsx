import { createPortal, flushSync } from 'react-dom'
import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react'
import { useWords } from './words'
import { Eye, Lightbulb, Plus } from 'lucide-react'
import type { Api, Pane } from './api'
import { NewTask } from './NewTask'
import { AgentIcon, needsYou, paneTitle, shownIndex } from './Sidebar'
import { WorktreeMark, worktreeNotice } from './Worktree'
import { anchorOf, clipTitle } from './hooks'
import type { Anchor } from './hooks'
import type { Session } from './Sidebar'
import { HOLD_MS } from './gesture'

// Put panes in the saved order. Panes not in the list (new splits) keep tmux order at the end.
export function orderPanes(panes: Pane[], order: string[] | undefined) {
  if (!order?.length) return panes
  const rank = (pane: Pane) => { const at = order.indexOf(pane.id); return at < 0 ? Number.MAX_SAFE_INTEGER : at }
  return panes.map((pane, at) => ({ pane, at })).sort((a, b) => rank(a.pane) - rank(b.pane) || a.at - b.at).map(item => item.pane)
}

// Replace only the visible positions. Hidden tabs keep their place for restore.
export function reorderPanes(panes: Pane[], order: string[] | undefined, ids: string[]): string[] {
  const shown = new Set(ids)
  let at = 0
  return orderPanes(panes, order).map(pane => shown.has(pane.id) ? ids[at++] : pane.id)
}

interface Props {
  session: Session
  pane: Pane
  onOpenPane: (pane: Pane) => void
  onPaneActions: (id: string, at?: Anchor) => void
  onSplit: (direction: 'right') => void
  onReorder: (ids: string[]) => void
  /** Desktop: a tab dropped on a group in the group row ([data-group-drop]) moves into it. */
  onDropGroup?: (pane: string, group: string) => void
  api: Api
  onUpdate: () => void
  micOk: boolean
  chime: boolean
  onNotice: (text: string) => void
}
// One shared pill marks the active tab. A side swipe on the chat slides it toward the next tab with the finger.
const SLIDE_CURVE = 'cubic-bezier(.2,.8,.2,1)'
const scrollBySession = new Map<string, number>()  // the strip remounts with each pane: keep its scroll place
const swipeScroll = new WeakMap<HTMLElement, number>()  // strip scroll when the swipe started
const scrollTween = new WeakMap<HTMLElement, number>()
interface TabPosition { left: number; top: number; width: number; height: number }
const swipePositions = new WeakMap<HTMLElement, { fromId: string; toId: string | null; from: TabPosition; to: TabPosition }>()
interface TabSnapshot { session: string; active: string; scroll: number; tabs: Map<string, TabPosition>; pill?: TabPosition }
// Chat remounts when its pane changes. Retain geometry so closing the active tab can still animate.
const closedTabPositions = new Map<string, TabSnapshot>()
const positionOf = (element: HTMLElement): TabPosition => ({ left: element.offsetLeft, top: element.offsetTop, width: element.offsetWidth, height: element.offsetHeight })
const tabTransform = (position: TabPosition, dx = 0) => `translate(${position.left + dx}px, ${position.top}px)`

/** Place the pill `progress` (0..1) of the way from the active tab to the tab `toId`. `ms` animates the move. */
export function movePill(strip: HTMLElement | null, toId: string | null, progress: number, ms = 0) {
  const finishing = ms > 0
  if (ms && matchMedia('(prefers-reduced-motion: reduce)').matches) ms = 0
  const pill = strip?.querySelector<HTMLElement>('.pane-tab-pill')
  const from = strip?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
  if (!strip || !pill || !from) return
  const to = toId ? strip.querySelector<HTMLElement>(`[data-pane="${CSS.escape(toId)}"]`) : null
  const p = to ? Math.min(1, Math.max(0, progress)) : 0
  const fromId = from.dataset.pane ?? ''
  let positions = swipePositions.get(strip)
  if (!positions || positions.fromId !== fromId || positions.toId !== toId) {
    const start = positionOf(from)
    positions = { fromId, toId, from: start, to: to ? positionOf(to) : start }
    swipePositions.set(strip, positions)
  }
  const { from: start, to: end } = positions
  if (!p || finishing) swipePositions.delete(strip)
  pill.style.transition = ms ? `transform ${ms}ms cubic-bezier(.32,.72,0,1), width ${ms}ms cubic-bezier(.32,.72,0,1)` : 'none'
  pill.style.transform = `translate(${start.left + (end.left - start.left) * p}px, ${start.top}px)`
  pill.style.width = `${start.width + (end.width - start.width) * p}px`
  pill.style.height = `${start.height}px`
  // Scroll the strip with the same progress, so a target tab off screen comes into view.
  cancelAnimationFrame(scrollTween.get(strip) ?? 0)
  let base = swipeScroll.get(strip)
  if (!p) { if (base !== undefined) { swipeScroll.delete(strip); scrollTo(strip, base, ms) } return }
  if (base === undefined) { base = strip.scrollLeft; swipeScroll.set(strip, base) }
  const pad = 12, view = strip.clientWidth
  const goal = end.left - pad < base ? end.left - pad : end.left + end.width + pad > base + view ? end.left + end.width + pad - view : base
  scrollTo(strip, base + (goal - base) * p, ms)
}
// A scroll that follows the same curve and time as the pill (smooth scrolling has its own timing).
function scrollTo(strip: HTMLElement, left: number, ms: number) {
  if (!ms) { strip.scrollLeft = left; return }
  const start = strip.scrollLeft, began = performance.now()
  const step = (now: number) => {
    const t = Math.min(1, (now - began) / ms), eased = 1 - Math.pow(1 - t, 3)
    strip.scrollLeft = start + (left - start) * eased
    if (t < 1) scrollTween.set(strip, requestAnimationFrame(step))
  }
  scrollTween.set(strip, requestAnimationFrame(step))
}

interface Drag { id: string; tab: HTMLElement; startX: number; startY: number; startLeft: number; scroll: number; x: number; lastAt: number; speed: number; lifted: boolean; moved: boolean; touch: boolean; timer: number; centers: { id: string; x: number }[] }
const REORDER_CURVE = 'cubic-bezier(.32,.72,0,1)'

// Tabs for the panes of one session. Reorder: long-press a tab, then drag it (a mouse drags at once).
// A long press without a drag opens the pane actions, as before. Only Tether's order changes; tmux panes stay put.
export const PaneTabs = forwardRef<HTMLDivElement, Props>(function PaneTabs({ session, pane, onOpenPane, onPaneActions, onSplit, onReorder, onDropGroup, api, onUpdate, micOk, chime, onNotice }, ref) {
  const strip = useRef<HTMLDivElement>(null)
  const words = useWords()
  useImperativeHandle(ref, () => strip.current!)
  const drag = useRef<Drag | null>(null)
  const suppressClick = useRef(false)
  const [preview, setPreviewState] = useState<string[] | null>(null)
  const previewNow = useRef<string[] | null>(null)
  const setPreview = (next: string[] | null) => { previewNow.current = next; setPreviewState(next) }
  const [dragging, setDragging] = useState<string | null>(null)
  const [taskAt, setTaskAt] = useState<Anchor | null>(null)
  const [fresh, setFresh] = useState<string | null>(null)  // the tab a task just opened: it lands with a glow
  useEffect(() => { if (!fresh) return; const timer = window.setTimeout(() => setFresh(null), 1600); return () => window.clearTimeout(timer) }, [fresh])
  const ids = session.panes.map(item => item.id)
  const order = ids.join()
  const positions = useRef<TabSnapshot | null>(null)
  // Before paint: restore the strip scroll and put the pill on the active tab. Tab sizes change with names and state dots.
  useLayoutEffect(() => {
    const box = strip.current
    if (!box) return
    box.scrollLeft = scrollBySession.get(session.id) ?? 0
    const tabIds = order.split(',')
    const before = positions.current ?? [...closedTabPositions.values()].reverse().find(snapshot => snapshot.session === session.id && !tabIds.includes(snapshot.active))
    const closing = before && [...before.tabs.keys()].some(id => id !== '+' && !tabIds.includes(id))
    for (const [id, snapshot] of closedTabPositions) {
      if (snapshot.session === session.id && !tabIds.includes(snapshot.active)) closedTabPositions.delete(id)
    }
    if (closing) {
      // Reveal the neighbor only as far as needed. Preserve the strip's place for inactive closes.
      const active = box.querySelector<HTMLElement>('[aria-selected="true"]')
      if (active) box.scrollLeft = Math.max(0, Math.min(box.scrollLeft, active.offsetLeft - 12, Math.max(0, box.scrollWidth - box.clientWidth)))
      if (active && active.offsetLeft + active.offsetWidth + 12 > box.scrollLeft + box.clientWidth) box.scrollLeft = active.offsetLeft + active.offsetWidth + 12 - box.clientWidth
    }
    const capture = () => {
      const tabs = new Map([...box.querySelectorAll<HTMLElement>('[data-pane]')].map(tab => [tab.dataset.pane!, positionOf(tab)]))
      const add = box.querySelector<HTMLElement>('.tab-add')
      if (add) tabs.set('+', positionOf(add))
      positions.current = { session: session.id, active: pane.id, scroll: box.scrollLeft, tabs, pill: tabs.get(pane.id) }
    }
    const save = () => { scrollBySession.set(session.id, box.scrollLeft); capture() }
    const place = () => { if (swipeScroll.get(box) === undefined) movePill(box, null, 0) }
    place()
    const animations: Animation[] = []
    if (closing && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      const timing = { duration: 220, easing: SLIDE_CURVE }
      const dx = box.scrollLeft - before.scroll
      for (const tab of box.querySelectorAll<HTMLElement>('[data-pane], .tab-add')) {
        const old = before.tabs.get(tab.dataset.pane ?? '+')
        if (old) animations.push(tab.animate([{ transform: `translate(${old.left - tab.offsetLeft + dx}px, ${old.top - tab.offsetTop}px)` }, { transform: 'translate(0, 0)' }], timing))
      }
      const pill = box.querySelector<HTMLElement>('.pane-tab-pill')
      const active = box.querySelector<HTMLElement>('[aria-selected="true"]')
      if (pill && active && before.pill) {
        const end = positionOf(active)
        animations.push(pill.animate([
          { transform: tabTransform(before.pill, dx), width: `${before.pill.width}px`, height: `${before.pill.height}px` },
          { transform: tabTransform(end), width: `${end.width}px`, height: `${end.height}px` },
        ], timing))
      }
    }
    capture()
    if (closing && !tabIds.includes(before.active)) box.querySelector<HTMLElement>('[aria-selected="true"]')?.focus({ preventScroll: true })
    scrollBySession.set(session.id, box.scrollLeft)
    const observer = new ResizeObserver(place)
    observer.observe(box)  // A layout change can move the tabs (Zed centres them) without resizing any of them.
    for (const tab of box.querySelectorAll('[role="tab"]')) observer.observe(tab)
    box.addEventListener('scroll', save, { passive: true })
    return () => {
      observer.disconnect(); box.removeEventListener('scroll', save)
      animations.forEach(animation => animation.cancel())
      if (positions.current) {
        closedTabPositions.delete(pane.id)
        closedTabPositions.set(pane.id, positions.current)
        if (closedTabPositions.size > 40) closedTabPositions.delete(closedTabPositions.keys().next().value!)
      }
      cancelAnimationFrame(scrollTween.get(box) ?? 0)
    }
  }, [session.id, pane.id, order])
  const latest = useRef({ ids, onReorder, onPaneActions, onDropGroup })
  latest.current = { ids, onReorder, onPaneActions, onDropGroup }

  useEffect(() => {
    const box = strip.current
    if (!box) return
    const tabOf = (target: EventTarget | null) => (target as Element | null)?.closest<HTMLElement>('[data-pane]')
    const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
    const timers = new Map<HTMLElement, number>()
    let clickTimer = 0
    const stop = (tab: HTMLElement) => {
      const transform = getComputedStyle(tab).transform
      const x = transform === 'none' ? 0 : new DOMMatrix(transform).m41
      window.clearTimeout(timers.get(tab)); timers.delete(tab)
      tab.style.transition = 'none'
      tab.style.transform = `translateX(${x}px)`
      return x
    }
    const settle = (tab: HTMLElement, held = false) => {
      const clear = () => {
        tab.style.transition = ''; tab.style.transform = ''
        timers.delete(tab)
        if (held) setDragging(now => now === tab.dataset.pane ? null : now)
      }
      window.clearTimeout(timers.get(tab))
      if (reduced()) { clear(); return }
      void tab.offsetWidth
      tab.style.transition = `transform 200ms ${REORDER_CURVE}`
      tab.style.transform = 'translateX(0px)'
      timers.set(tab, window.setTimeout(clear, 200))
    }
    // Measure visual positions so another crossing can interrupt a FLIP in flight.
    const rearrange = (state: Drag, update: () => void) => {
      const tabs = [...box.querySelectorAll<HTMLElement>('[data-pane]')]
      const scroll = box.scrollLeft
      const left = box.getBoundingClientRect().left - scroll
      const before = new Map(tabs.map(tab => [tab, tab.getBoundingClientRect().left - left]))
      flushSync(update)
      box.scrollLeft = scroll
      for (const tab of tabs) {
        if (!tab.isConnected) continue
        window.clearTimeout(timers.get(tab)); timers.delete(tab)
        tab.style.transition = 'none'
        const dx = before.get(tab)! - tab.offsetLeft
        tab.style.transform = `translateX(${dx}px)`
        if (tab !== state.tab) settle(tab)
      }
    }
    const begin = (tab: HTMLElement, x: number, y: number, touch: boolean): Drag => ({
      id: tab.dataset.pane!, tab, startX: x, startY: y, startLeft: tab.offsetLeft + stop(tab), scroll: box.scrollLeft,
      x, lastAt: performance.now(), speed: 0, lifted: false, moved: false, touch, timer: 0, centers: [],
    })
    // Tab centers in strip coordinates, so a scrolled strip still maps the finger to the right slot.
    const lift = (state: Drag) => {
      state.centers = [...box.querySelectorAll<HTMLElement>('[data-pane]')].map(tab => ({ id: tab.dataset.pane!, x: tab.offsetLeft + tab.offsetWidth / 2 }))
      state.lifted = true
      if (state.touch) navigator.vibrate?.(15)
      setDragging(state.id)
      setPreview(latest.current.ids)
    }
    const move = (state: Drag, x: number, carry = 0) => {
      const rect = box.getBoundingClientRect()
      if (x < rect.left + 32) box.scrollLeft -= 8
      else if (x > rect.right - 32) box.scrollLeft += 8
      const tabs = [...box.querySelectorAll<HTMLElement>('[data-pane]')]
      const min = Math.min(...tabs.map(tab => tab.offsetLeft))
      const max = Math.max(...tabs.map(tab => tab.offsetLeft + tab.offsetWidth)) - state.tab.offsetWidth
      const left = state.startLeft + x - state.startX + box.scrollLeft - state.scroll
      const bound = Math.max(min, Math.min(max, left))
      state.tab.style.transition = 'none'
      state.tab.style.transform = `translateX(${bound + (left - bound) * .25 - state.tab.offsetLeft}px)`
      if (!state.moved) return
      const at = x + carry - rect.left + box.scrollLeft
      const others = state.centers.filter(item => item.id !== state.id)
      const slot = others.filter(item => item.x < at).length
      const next = others.map(item => item.id)
      next.splice(slot, 0, state.id)
      if (previewNow.current?.join() !== next.join()) rearrange(state, () => setPreview(next))
    }
    const track = (state: Drag, x: number) => {
      const now = performance.now()
      if (now > state.lastAt) state.speed = state.speed * .4 + (x - state.x) / (now - state.lastAt) * .6
      state.x = x; state.lastAt = now
      move(state, x)
    }
    const finish = (commit: boolean) => {
      const state = drag.current
      drag.current = null
      if (!state) return
      window.clearTimeout(state.timer)
      if (commit && state.moved && !reduced() && performance.now() - state.lastAt < 80 && Math.abs(state.speed) > .4 && Math.sign(state.speed) === Math.sign(state.x - state.startX)) {
        const direction = Math.sign(state.speed)
        const neighbor = state.centers[state.centers.findIndex(item => item.id === state.id) + direction]
        const edge = neighbor ? neighbor.x + box.getBoundingClientRect().left - box.scrollLeft + direction : state.x
        const projected = state.x + state.speed * 100
        move(state, state.x, (direction > 0 ? Math.max(edge, projected) : Math.min(edge, projected)) - state.x)
      }
      if (state.lifted) {
        suppressClick.current = true
        window.clearTimeout(clickTimer)
        clickTimer = window.setTimeout(() => { suppressClick.current = false }, 400)
        const order = previewNow.current
        rearrange(state, () => {
          if (commit && state.moved && order && order.join() !== latest.current.ids.join()) latest.current.onReorder(order)
          setPreview(null)
        })
        if (commit && !state.moved && state.touch) latest.current.onPaneActions(state.id)
      }
      settle(state.tab, true)
    }
    const touchStart = (event: TouchEvent) => {
      const tab = tabOf(event.target)
      if (!tab || event.touches.length > 1) return
      const touch = event.touches[0]
      const state = begin(tab, touch.clientX, touch.clientY, true)
      state.timer = window.setTimeout(() => { if (drag.current === state) lift(state) }, HOLD_MS)
      drag.current = state
    }
    const touchMove = (event: TouchEvent) => {
      const state = drag.current
      if (!state) return
      const touch = event.touches[0]
      if (!state.lifted) {
        // The finger moved before the hold time: this is a scroll of the strip, not a drag.
        if (Math.hypot(touch.clientX - state.startX, touch.clientY - state.startY) > 10) finish(false)
        return
      }
      event.preventDefault()
      if (Math.abs(touch.clientX - state.startX) > 6) state.moved = true
      track(state, touch.clientX)
    }
    const touchEnd = (event: TouchEvent) => { if (drag.current?.lifted) event.preventDefault(); finish(event.type === 'touchend') }
    const mouseDown = (event: MouseEvent) => {
      const tab = tabOf(event.target)
      if (!tab || event.button !== 0) return
      drag.current = begin(tab, event.clientX, event.clientY, false)
    }
    // A group in the group row under the pointer, while a tab is dragged (desktop only).
    const dropAt = (x: number, y: number) => latest.current.onDropGroup ? document.elementFromPoint(x, y)?.closest<HTMLElement>('[data-group-drop]') ?? null : null
    let over: HTMLElement | null = null
    const hover = (target: HTMLElement | null) => { if (over === target) return; over?.classList.remove('drop-over'); over = target; over?.classList.add('drop-over') }
    const mouseMove = (event: MouseEvent) => {
      const state = drag.current
      if (!state || state.touch) return
      if (!state.lifted) { if (Math.abs(event.clientX - state.startX) < 6) return; lift(state) }
      state.moved = true
      hover(dropAt(event.clientX, event.clientY))
      track(state, event.clientX)
    }
    const mouseUp = (event: MouseEvent) => {
      const state = drag.current
      if (!state || state.touch) return
      const target = state.lifted ? dropAt(event.clientX, event.clientY) : null
      hover(null)
      if (target) { finish(false); latest.current.onDropGroup?.(state.id, target.dataset.groupDrop ?? '') }
      else finish(true)
    }
    box.addEventListener('touchstart', touchStart, { passive: true })
    box.addEventListener('touchmove', touchMove, { passive: false })
    box.addEventListener('touchend', touchEnd, { passive: false })
    box.addEventListener('touchcancel', touchEnd)
    box.addEventListener('mousedown', mouseDown)
    window.addEventListener('mousemove', mouseMove)
    window.addEventListener('mouseup', mouseUp)
    return () => {
      if (drag.current) window.clearTimeout(drag.current.timer)
      window.clearTimeout(clickTimer)
      for (const [tab, timer] of timers) { window.clearTimeout(timer); tab.style.transition = ''; tab.style.transform = '' }
      if (drag.current) { drag.current.tab.style.transition = ''; drag.current.tab.style.transform = '' }
      drag.current = null
      hover(null)
      box.removeEventListener('touchstart', touchStart)
      box.removeEventListener('touchmove', touchMove)
      box.removeEventListener('touchend', touchEnd)
      box.removeEventListener('touchcancel', touchEnd)
      box.removeEventListener('mousedown', mouseDown)
      window.removeEventListener('mousemove', mouseMove)
      window.removeEventListener('mouseup', mouseUp)
    }
  }, [])

  const byId = new Map(session.panes.map(item => [item.id, item]))
  const shown = preview ? preview.map(id => byId.get(id)).filter((item): item is Pane => !!item) : session.panes
  // While tabs are reordered, each tab draws its own highlight again.
  return <div className={preview ? 'pane-tabs' : 'pane-tabs has-pill'} ref={strip} role="tablist" aria-label={`Panes of ${session.name}`}>
    <span className="pane-tab-pill" aria-hidden="true" />
    {shown.map(item => <button key={item.id} data-pane={item.id} role="tab" aria-selected={item.id === pane.id} className={[item.id === pane.id ? 'active' : '', dragging === item.id ? 'dragging' : '', fresh === item.id ? 'task-new' : '', item.role === 'advisor' ? 'advisor-tab' : '', item.role === 'advisor' && item.state === 'Working' ? 'advising' : ''].filter(Boolean).join(' ')}
      onClick={() => { if (!suppressClick.current) onOpenPane(item) }}
      // A touch long press is handled above (drag, or actions on release). A right-click still opens the actions.
      onMouseEnter={clipTitle} onContextMenu={event => { event.preventDefault(); if (!drag.current?.touch && !suppressClick.current) onPaneActions(item.id, anchorOf(event)) }}>
      {item.role === 'advisor' ? <><AgentIcon agent={item.agent} size={14} /><Lightbulb size={13} className="tab-advisor" aria-hidden="true" /></> : <AgentIcon agent={item.agent} icon={item.pane_icon} size={14} />}<span className="pane-tab-name">{paneTitle(item)}</span>{shownIndex(item) && <span className="pane-index">{shownIndex(item)}</span>}{item.worktree && <WorktreeMark branch={item.worktree} />}{!!item.reviewers?.length && <Eye size={12} className="tab-reviewed" aria-label={`Reviewed by ${item.reviewers.map(id => { const other = byId.get(id); return other ? paneTitle(other) : 'another tab' }).join(', ')}`} />}
      {item.state === 'Working' || needsYou(item.state) ? <span className={`state-dot ${item.state === 'Working' ? 'working' : 'approval'}`} aria-label={item.state === 'Working' ? item.role === 'advisor' ? 'Advising' : 'Working' : 'Needs you'} /> : item.state === 'Stopped' ? <span className="state-dot" aria-label="Stopped" /> : null}
    </button>)}
    <button className="tab-add" aria-label={`${words.newRight} with a task`} title={`${words.newRight} with a task`} aria-haspopup="dialog" aria-expanded={!!taskAt}
      onClick={event => { const box = event.currentTarget.getBoundingClientRect(); setTaskAt({ x: box.right - 360, y: box.bottom + 6 }) }}><Plus size={16} /></button>
    {taskAt && createPortal(<NewTask api={api} pane={pane} at={taskAt} micOk={micOk} chime={chime} onClose={() => setTaskAt(null)} onEmpty={() => { setTaskAt(null); onSplit('right') }}
      onCreated={(id, made) => { setTaskAt(null); setFresh(id); onUpdate(); if (made) onNotice(worktreeNotice(made)) }}
      onOpen={item => { setTaskAt(null); setFresh(item.id); onUpdate(); onOpenPane(item) }}
      onMoved={item => { setTaskAt(null); setFresh(item.id); onUpdate(); onOpenPane(item) }} />, document.body)}
  </div>
})
