import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Columns2, Columns3, LayoutGrid, PanelLeft, Rows2, Search, Square, X } from 'lucide-react'
import type { Pane } from './api'
import type { DesktopLayout } from './prefs'
import { useModal } from './hooks'
import { agentNames, paneTitle, shownIndex } from './Sidebar'
import { profileSuffix } from './provider'
import type { Session } from './Sidebar'
import { DRAG_CHAT, DRAG_PANE, DRAG_SESSION, DRAG_SLOT, clampRow, columnBounds, equalBounds, moveBound, splitSlot, tracks } from './workspace'
import type { Side } from './workspace'

const LAYOUTS = [
  { count: 1, stack: false, label: 'Show 1 pane', icon: Square },
  { count: 2, stack: false, label: 'Show 2 side by side (⌘\\ adds one)', icon: Columns2 },
  { count: 2, stack: true, label: 'Show 2, one above the other', icon: Rows2 },
  { count: 3, stack: false, label: 'Show 3 side by side', icon: Columns3 },
  { count: 4, stack: false, label: 'Show 4 in a grid (⌘⇧\\)', icon: LayoutGrid },
] as const

const PAD = 8, GAP = 8

interface WorkspaceProps {
  layout: DesktopLayout
  slots: Pane[]
  focus: number
  zoomed: boolean
  onCount: (count: 1 | 2 | 3 | 4, stack: boolean) => void
  onFocus: (index: number) => void
  onLayout: (patch: Partial<DesktopLayout>) => void
  renderSlot: (pane: Pane, index: number, shown: number) => ReactNode
  tools?: ReactNode
  /** The panel buttons at the right end of the toolbar, and the panel column beside the slots (PanelColumn.tsx). */
  panelTools?: ReactNode
  panels?: ReactNode
  /** Drops from the sidebar (a pane or a whole session) and from another slot's header. A side means "next to this slot". */
  onDropPane: (id: string, index: number, side: Side | null) => void
  onDropSession: (id: string) => void
  /** A past chat from the sidebar: it opens as a new tab, then shows here. */
  onDropChat: (data: string, index: number, side: Side | null) => void
  onSwap: (from: number, to: number) => void
}

type Drop = { index: number; mode: 'replace' | 'swap' | 'session' | Side }
// How close to an edge (as a share of the slot) a drop must be to land beside the slot instead of in it.
const EDGE = 0.28

/** Desktop workspace: a toolbar and a grid of slots with dividers you can drag, or move with the arrow keys. */
export function Workspace({ layout, slots, focus, zoomed, onCount, onFocus, onLayout, renderSlot, tools, panelTools, panels, onDropPane, onDropSession, onDropChat, onSwap }: WorkspaceProps) {
  const shown = zoomed ? slots.slice(focus, focus + 1) : slots
  const count = shown.length
  const stacked = count === 2 && !!layout.stack
  const columns = count === 4 ? 2 : stacked ? 1 : count
  const rows = count === 4 || stacked ? 2 : 1
  // While a divider moves, the sizes live here. They go to prefs once, when the drag ends.
  const [draft, setDraft] = useState<{ cols?: number[]; row?: number } | null>(null)
  const draftRef = useRef(draft)
  const frame = useRef(0)
  const pending = useRef<(() => void) | null>(null)
  const [drag, setDrag] = useState<string | null>(null)
  useEffect(() => () => cancelAnimationFrame(frame.current), [])
  const grid = useRef<HTMLDivElement>(null)
  const bounds = draft?.cols ?? columnBounds(layout, count)
  const row = draft?.row ?? clampRow(layout.row)
  const saveCols = (next: number[]) => onLayout({ cols: { ...layout.cols, [count]: next } })
  const [drop, setDrop] = useState<Drop | null>(null)
  // Where a drag would land: a slot header swaps; a pane replaces the slot, or adds a slot when held over its right edge.
  const dropAt = (event: React.DragEvent<HTMLElement>, index: number): Drop | null => {
    const types = event.dataTransfer.types
    if (types.includes(DRAG_SLOT)) return { index, mode: 'swap' }
    if (types.includes(DRAG_SESSION)) return { index, mode: 'session' }
    if (!types.includes(DRAG_PANE) && !types.includes(DRAG_CHAT)) return null
    if (zoomed) return { index, mode: 'replace' }
    // The pane being dragged is unknown until the drop, so test each edge with a new pane. Nearest allowed edge wins.
    const box = event.currentTarget.getBoundingClientRect()
    const x = (event.clientX - box.left) / box.width, y = (event.clientY - box.top) / box.height
    const ids = slots.map(item => item.id)
    const edges = ([['left', x], ['right', 1 - x], ['top', y], ['bottom', 1 - y]] as [Side, number][])
      .filter(([side, gap]) => gap < EDGE && splitSlot(ids, stacked, index, side, '\u0000'))
      .sort((a, b) => a[1] - b[1])
    return { index, mode: edges[0]?.[0] ?? 'replace' }
  }
  const dropProps = (index: number) => ({
    onDragOver: (event: React.DragEvent<HTMLElement>) => {
      const next = dropAt(event, index)
      if (!next) return
      event.preventDefault()
      event.dataTransfer.dropEffect = next.mode === 'swap' ? 'move' : 'copy'
      if (drop?.index !== next.index || drop.mode !== next.mode) setDrop(next)
    },
    onDragLeave: (event: React.DragEvent<HTMLElement>) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDrop(null) },
    onDrop: (event: React.DragEvent<HTMLElement>) => {
      const next = dropAt(event, index)
      setDrop(null)
      if (!next) return
      event.preventDefault()
      const data = event.dataTransfer
      if (next.mode === 'swap') { const from = Number(data.getData(DRAG_SLOT)); if (Number.isInteger(from) && from !== index) onSwap(from, index) }
      else if (next.mode === 'session') onDropSession(data.getData(DRAG_SESSION))
      else if (data.types.includes(DRAG_CHAT)) onDropChat(data.getData(DRAG_CHAT), index, next.mode === 'replace' ? null : next.mode)
      else onDropPane(data.getData(DRAG_PANE), index, next.mode === 'replace' ? null : next.mode)
    },
  })
  const hint = (mode: Drop['mode']) => ({ swap: 'Swap slots', session: 'Show all panes of this session', replace: 'Show here', left: 'Open on the left', right: 'Open on the right', top: 'Open above', bottom: 'Open below' })[mode]

  const divider = (kind: 'col' | 'row', j: number, share: number, cells: number) => {
    const vertical = kind === 'col'
    const at = `calc(${PAD}px + (100% - ${2 * PAD + (cells - 1) * GAP}px) * ${share.toFixed(4)} + ${j * GAP - 2}px)`
    const id = kind + j
    const place = (event: Pick<React.PointerEvent, 'clientX' | 'clientY'>) => {
      const box = grid.current!.getBoundingClientRect()
      const along = vertical ? event.clientX - box.left : event.clientY - box.top
      const total = (vertical ? box.width : box.height) - 2 * PAD - (cells - 1) * GAP
      const value = (along - PAD - j * GAP - GAP / 2) / total
      // eslint-disable-next-line react-hooks/refs -- place runs from pointer handlers and animation frames, not during render.
      draftRef.current = vertical ? { ...draftRef.current, cols: moveBound(draftRef.current?.cols ?? bounds, j, value) } : { ...draftRef.current, row: clampRow(value) }
      setDraft(draftRef.current)
    }
    const set = (value: number) => vertical ? saveCols(moveBound(bounds, j, value)) : onLayout({ row: clampRow(value) })
    const reset = () => vertical ? saveCols(equalBounds(count)) : onLayout({ row: 0.5 })
    return <div key={id} role="separator" tabIndex={0} className={`slot-divider ${vertical ? 'vertical' : 'horizontal'} ${drag === id ? 'dragging' : ''}`}
      aria-orientation={vertical ? 'vertical' : 'horizontal'} aria-label={vertical ? `Resize panes ${j + 1} and ${j + 2}` : 'Resize rows'}
      aria-valuemin={15} aria-valuemax={85} aria-valuenow={Math.round(share * 100)} title="Drag to resize. Double-click to make equal."
      style={vertical ? { left: at, top: PAD, bottom: PAD } : { top: at, left: PAD, right: PAD }}
      onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.focus(); event.preventDefault(); setDrag(id) }}
      onPointerMove={event => { if (drag !== id) return
        const point = { clientX: event.clientX, clientY: event.clientY }
        // eslint-disable-next-line react-hooks/refs -- Pointer move handler, not render.
        pending.current = () => place(point)
        if (!frame.current) frame.current = requestAnimationFrame(() => { frame.current = 0; pending.current?.(); pending.current = null })
      }}
      onPointerUp={() => { if (drag !== id) return; cancelAnimationFrame(frame.current); frame.current = 0; pending.current?.(); pending.current = null; setDrag(null); const done = draftRef.current; if (done?.cols) saveCols(done.cols); if (done?.row !== undefined) onLayout({ row: done.row }); draftRef.current = null; setDraft(null) }}
      onPointerCancel={() => { cancelAnimationFrame(frame.current); frame.current = 0; pending.current = null; draftRef.current = null; setDrag(null); setDraft(null) }}
      onDoubleClick={reset}
      onKeyDown={event => {
        const back = vertical ? 'ArrowLeft' : 'ArrowUp', forward = vertical ? 'ArrowRight' : 'ArrowDown'
        const step = event.shiftKey ? 0.1 : 0.02
        if (event.key === back || event.key === forward) { event.preventDefault(); set(share + (event.key === forward ? step : -step)) }
        else if (event.key === 'Enter') { event.preventDefault(); reset() }
      }}><span aria-hidden="true" /></div>
  }

  return <div className="workspace">
    <div className="workspace-bar" role="toolbar" aria-label="Workspace">
      <button type="button" className="icon-button" aria-label="Sidebar (⌘B)" title="Sidebar (⌘B)" aria-pressed={layout.sidebar} onClick={() => onLayout({ sidebar: !layout.sidebar })}><PanelLeft size={18} /></button>
      <div className="layout-switch" role="group" aria-label="Layout">
        {LAYOUTS.map(({ count: value, stack, label, icon: Icon }) => <button key={label} type="button" aria-label={label} title={label} aria-pressed={!zoomed && layout.count === value && (value !== 2 || !!layout.stack === stack)} onClick={() => onCount(value, stack)}><Icon size={16} /></button>)}
      </div>
      <span className="workspace-spacer" />
      {tools}
      {panelTools}
    </div>
    <div className="workspace-body">
    <div ref={grid} className={count > 1 ? 'slots multi' : 'slots single'} style={{ gridTemplateColumns: columns > 1 ? tracks(bounds) : undefined, gridTemplateRows: rows > 1 ? tracks([row]) : undefined }}>
      {shown.map((pane, at) => { const index = zoomed ? focus : at; return <section key={pane.id} data-slot={index} className={index === focus ? 'slot focused' : 'slot'} aria-label={`Slot ${index + 1}`}
        onPointerDownCapture={() => { if (index !== focus) onFocus(index) }} onFocusCapture={() => { if (index !== focus) onFocus(index) }} {...dropProps(index)}>
        {renderSlot(pane, index, count)}
        <div className="drop-hint touch-drop-hint" aria-hidden="true" />
        {drop?.index === index && <div className={`drop-hint ${drop.mode}`} aria-hidden="true"><span>{hint(drop.mode)}</span></div>}
      </section> })}
      {columns > 1 && bounds.map((share, j) => divider('col', j, share, columns))}
      {rows > 1 && divider('row', 0, row, 2)}
    </div>
    {panels}
    </div>
  </div>
}

interface SlotPickerProps { index: number; sessions: Session[]; slots: string[]; onPane: (id: string) => void; onSession: (session: Session) => void; onClose: () => void }

/** Choose what one slot shows: a single pane, or every pane of a session across the slots. */
export function SlotPicker({ index, sessions, slots, onPane, onSession, onClose }: SlotPickerProps) {
  const modal = useModal(true, onClose)
  const [query, setQuery] = useState('')
  const words = query.trim().toLowerCase()
  const match = (text: string) => !words || text.toLowerCase().includes(words)
  const label = (pane: Pane) => `${[paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')} · ${agentNames[pane.agent]}${profileSuffix(pane)}`
  const where = (id: string) => { const at = slots.indexOf(id); return at < 0 ? null : <kbd className="slot-key" aria-label={`In slot ${at + 1}`}>⌥{at + 1}</kbd> }
  const shown = sessions.filter(session => match(session.name) || session.panes.some(pane => match(label(pane))))
  return <dialog {...modal} className="sheet slot-picker" aria-labelledby="slot-picker-title">
    <div className="sheet-heading"><h2 id="slot-picker-title">Show in slot {index + 1}</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="sheet-sub">Pick a pane for this slot, or a whole session to fill the slots with its panes.</p>
    <label className="sidebar-search picker-search"><Search size={15} aria-hidden="true" /><input type="search" autoFocus placeholder="Search" aria-label="Search panes and sessions" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <ul className="picker-list">
      {shown.map(session => session.panes.length > 1
        ? <li key={session.id}>
            <button type="button" className="picker-session" onClick={() => onSession(session)}><span className="picker-name">{session.name}</span><span className="picker-hint">Show all {Math.min(4, session.panes.length)} panes{session.panes.length > 4 ? ` of ${session.panes.length}` : ''}</span></button>
            <ul>{session.panes.filter(pane => match(session.name) || match(label(pane))).map(pane => <li key={pane.id}><button type="button" className="picker-pane" onClick={() => onPane(pane.id)}><span className="picker-name">{label(pane)}</span>{where(pane.id)}</button></li>)}</ul>
          </li>
        : <li key={session.id}><button type="button" className="picker-pane single" onClick={() => onPane(session.panes[0].id)}><span className="picker-name">{session.name}</span><span className="picker-hint">{agentNames[session.panes[0].agent]}</span>{where(session.panes[0].id)}</button></li>)}
      {!shown.length && <li className="group-empty">Nothing matches.</li>}
    </ul>
  </dialog>
}
