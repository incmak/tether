import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode, UIEvent } from 'react'
import { ArchiveRestore, Check, ChevronDown, ChevronLeft, ChevronRight, EyeOff, Folder, Search, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, HiddenData, HistoryDir, TemporaryMode } from './api'
import './SelectMenu.css'

export interface MenuOption {
  value: string; label: string; detail?: string; count?: number; group?: string; search?: string
  /** Do this instead of picking the row. The menu stays open. */
  run?: () => void
  lead?: ReactNode
  /** A small button at the end of the row. The Delete key also runs it. */
  trailing?: { label: string; icon: ReactNode; run: () => void }
  /** Only while searching: a row of a collapsed group. */
  onlySearch?: boolean
  /** Not while searching: a row that opens or collapses something. */
  noSearch?: boolean
  /** Move this row to the top of the list after `run`, so the rows it opened are in view. */
  reveal?: boolean
  /** Show the trailing button on every row, also with a pointer. */
  pinTrailing?: boolean
}

const SEARCH_FROM = 9  // A shorter list is quick to read.
const GAP = 4, EDGE = 8, MAX_HEIGHT = 440

/**
 * The app's dropdown, in place of a native select. A long list gets a search field.
 * A sheet's swipe, click and key handlers must not see what happens inside the panel.
 */
export function SelectMenu({ id, value, options, onChange, label, icon, className = 'sm-field', disabled, minWidth = 220, searchLabel = 'Search', emptyText = 'Nothing matches.', triggerLabel, triggerTitle, onClosed, openOnMount = false }: {
  id?: string; value: string; options: MenuOption[]; onChange: (value: string) => void; label?: string; icon?: ReactNode
  className?: string; disabled?: boolean; minWidth?: number; searchLabel?: string; emptyText?: string; triggerLabel?: string; triggerTitle?: string; onClosed?: () => void
  /** Open the list as soon as the trigger appears, for a picker that a key or row opened. */
  openOnMount?: boolean
}) {
  const [open, setOpen] = useState(openOnMount)
  const [query, setQuery] = useState('')
  const [activeValue, setActiveValue] = useState(value)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const listId = useId()
  const selected = options.find(option => option.value === value)
  const searchable = options.length >= SEARCH_FROM
  const shown = useMemo(() => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
    if (!terms.length) return options.filter(option => !option.onlySearch)
    return options.filter(option => { if (option.noSearch) return false; const text = `${option.label} ${option.detail ?? ''} ${option.search ?? ''}`.toLowerCase(); return terms.every(term => text.includes(term)) })
  }, [options, query])
  const active = shown.find(option => option.value === activeValue) ?? shown[0]
  const optionId = (index: number) => `${listId}-${index}`
  const activeId = active ? optionId(shown.indexOf(active)) : undefined

  const show = () => { setQuery(''); setActiveValue(value); setOpen(true) }
  const close = (focus: boolean) => { setOpen(false); onClosed?.(); if (focus) trigger.current?.focus() }
  const pick = (next: string) => { close(true); if (next !== value) onChange(next) }
  // The clicked row or button can leave the list. Keep focus in the panel, so the keys still work.
  const stay = () => requestAnimationFrame(() => { if (searchable && matchMedia('(pointer: fine)').matches) field.current?.focus(); else if (!panel.current?.contains(document.activeElement)) list.current?.focus() })
  const choose = (option: MenuOption, index: number) => {
    if (!option.run) return pick(option.value)
    option.run(); stay()
    if (option.reveal) requestAnimationFrame(() => document.getElementById(optionId(index))?.scrollIntoView({ block: 'start' }))
  }
  const remove = (option: MenuOption) => { option.trailing?.run(); stay() }

  useLayoutEffect(() => {
    const box = panel.current, button = trigger.current
    if (!open || !box || !button) return
    box.showPopover?.()
    const place = () => {
      const anchor = button.getBoundingClientRect(), view = window.visualViewport
      const top = view?.offsetTop ?? 0, height = view?.height ?? innerHeight
      const below = top + height - anchor.bottom - GAP - EDGE, above = anchor.top - top - GAP - EDGE
      const up = below < 240 && above > below
      const width = Math.min(Math.max(anchor.width, minWidth), innerWidth - 2 * EDGE)
      box.style.width = `${width}px`
      box.style.left = `${Math.max(EDGE, Math.min(anchor.left, innerWidth - width - EDGE))}px`
      box.style.maxHeight = `${Math.min(up ? above : below, MAX_HEIGHT)}px`
      box.style.top = up ? 'auto' : `${anchor.bottom + GAP}px`
      box.style.bottom = up ? `${innerHeight - anchor.top + GAP}px` : 'auto'
    }
    place()
    const scrolled = (event: Event) => { if (!(event.target instanceof Node && box.contains(event.target))) place() }
    window.addEventListener('resize', place)
    window.addEventListener('scroll', scrolled, true)
    window.visualViewport?.addEventListener('resize', place)
    window.visualViewport?.addEventListener('scroll', place)
    // A phone keeps its keyboard closed until the person taps the search field.
    if (searchable && matchMedia('(pointer: fine)').matches) field.current?.focus()
    else list.current?.focus()
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', scrolled, true)
      window.visualViewport?.removeEventListener('resize', place)
      window.visualViewport?.removeEventListener('scroll', place)
    }
    // Place the panel once per opening. Later size changes come through the listeners.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      const node = event.target as Node
      if (!panel.current?.contains(node) && !trigger.current?.contains(node)) close(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  useLayoutEffect(() => {
    if (open && activeId) document.getElementById(activeId)?.scrollIntoView({ block: 'nearest' })
  }, [open, activeId])

  const keys = (event: KeyboardEvent) => {
    const at = active ? shown.indexOf(active) : -1
    const move = (index: number) => setActiveValue(shown[index]?.value ?? activeValue)
    switch (event.key) {
      case 'ArrowDown': move(Math.min(at + 1, shown.length - 1)); break
      case 'ArrowUp': move(Math.max(at - 1, 0)); break
      case 'Home': if (event.target === field.current) return; move(0); break
      case 'End': if (event.target === field.current) return; move(shown.length - 1); break
      case 'Enter': if (active) choose(active, shown.indexOf(active)); break
      case 'Delete': if (!active?.trailing || (event.target === field.current && query)) return; remove(active); break
      case 'Escape': close(true); break
      case 'Tab': close(true); event.stopPropagation(); return
      default: return
    }
    event.preventDefault()
    event.stopPropagation()
  }
  const keep = (event: UIEvent) => event.stopPropagation()

  return <>
    <button ref={trigger} id={id} type="button" className={`sm-trigger ${className}`} disabled={disabled} title={triggerTitle ?? selected?.search ?? selected?.detail}
      aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined} aria-label={label ? `${label}: ${triggerTitle ?? triggerLabel ?? selected?.search ?? selected?.label ?? ''}` : undefined}
      onClick={() => open ? close(false) : show()}
      onKeyDown={event => { if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); event.stopPropagation(); show() } }}>
      {icon}<span>{triggerLabel ?? selected?.label ?? value}</span><ChevronDown className="sm-chevron" size={14} aria-hidden="true" />
    </button>
    {open && <div ref={panel} className="sm-panel" popover="manual" onKeyDown={keys} onClick={keep} onContextMenu={keep} onTouchStart={keep} onTouchMove={keep} onTouchEnd={keep}>
      {searchable && <label className="sm-search"><Search size={14} aria-hidden="true" />
        <input ref={field} type="search" role="combobox" value={query} placeholder={searchLabel} aria-label={searchLabel} aria-expanded="true" aria-controls={listId} aria-autocomplete="list" aria-activedescendant={activeId} autoComplete="off" onChange={event => setQuery(event.target.value)} />
      </label>}
      <div ref={list} id={listId} className="sm-list" role="listbox" tabIndex={-1} aria-label={label ?? searchLabel} aria-activedescendant={activeId}>
        {shown.map((option, index) => <div key={option.value} role="presentation">
          {option.group && option.group !== shown[index - 1]?.group && <div className="sm-group" role="presentation">{option.group}</div>}
          <div className={`sm-row ${option === active ? 'active' : ''} ${option.pinTrailing ? 'pin' : ''}`} role="presentation" onPointerMove={() => setActiveValue(option.value)}>
          <div id={optionId(index)} role="option" aria-selected={!option.run && option.value === value} aria-keyshortcuts={option.trailing ? 'Delete' : undefined} title={option.search ?? option.detail} className="sm-option" onClick={() => choose(option, index)}>
            <span className="sm-check">{option.lead ?? (!option.run && option.value === value && <Check size={15} aria-hidden="true" />)}</span>
            <span className="sm-text"><span className="sm-label">{option.label}</span>{option.detail && <bdi className="sm-detail" dir="ltr">{option.detail}</bdi>}</span>
            {option.count !== undefined && <span className="sm-count">{option.count}</span>}
          </div>
          {option.trailing && <button type="button" className="sm-trailing" tabIndex={-1} aria-label={option.trailing.label} title={option.trailing.label} onClick={() => remove(option)}>{option.trailing.icon}</button>}
          </div>
        </div>)}
        {!shown.length && <p className="sm-none" role="status">{emptyText}</p>}
      </div>
    </div>}
  </>
}

const TEMPORARY = /^(\/tmp|\/private\/tmp|\/var\/folders|\/private\/var\/folders)(\/|$)/

/** "~/apps/tether" gives the name "tether" and the parent "~/apps". */
function folderParts(path: string) {
  const trimmed = path.length > 1 ? path.replace(/\/+$/, '') : path
  const cut = trimmed.lastIndexOf('/')
  if (cut < 0) return { name: trimmed, parent: '' }
  if (cut === 0) return { name: trimmed.slice(1) || '/', parent: trimmed === '/' ? '' : '/' }
  return { name: trimmed.slice(cut + 1), parent: trimmed.slice(0, cut) }
}

/** Filter by folder. Real project folders come first. Temporary agent folders collapse, and any folder can leave the lists. */
export function FolderMenu({ api, value, folders, mode = 'collapsed', hiddenCount = 0, onChange, onChanged, onError, onRemoved }: {
  api: Api; value: string; folders: HistoryDir[]; mode?: TemporaryMode; hiddenCount?: number
  onChange: (dir: string) => void; onChanged: () => void; onError: (message: string) => void
  onRemoved?: (text: string, undo: () => Promise<unknown>) => void
}) {
  const [view, setView] = useState<'folders' | 'hidden'>('folders')
  const viewRef = useRef(view)
  useLayoutEffect(() => { viewRef.current = view }, [view])
  const [hidden, setHidden] = useState<HiddenData>()
  const [localMode, setLocalMode] = useState<TemporaryMode>()
  // The server answer replaces the instant local state, and it carries changes from other devices.
  const [previousMode, setPreviousMode] = useState(mode)
  if (previousMode !== mode) { setPreviousMode(mode); setLocalMode(undefined) }
  const shownMode = localMode ?? mode
  const loadHidden = () => api<HiddenData>('history/hidden').then(setHidden, failure => onError(errorText(failure)))
  const post = async (body: object) => {
    try { await api('history/hide', body); onChanged(); if (viewRef.current === 'hidden') await loadHidden() }
    catch (failure) { setLocalMode(undefined); onError(errorText(failure)) }
  }
  const setMode = (next: TemporaryMode) => { setLocalMode(next); void post({ kind: 'temporary', mode: next }) }
  const hideDir = (dir: string) => { if (dir === value) onChange(''); onRemoved?.('Folder removed from the lists', () => post({ kind: 'dir', dir, hidden: false })); void post({ kind: 'dir', dir, hidden: true }) }
  const openHidden = () => { setHidden(undefined); setView('hidden'); void loadHidden() }
  const options = useMemo<MenuOption[]>(() => {
    if (view === 'hidden') {
      const restore = (run: () => void, label: string) => ({ run, pinTrailing: true, trailing: { label, icon: <ArchiveRestore size={14} aria-hidden="true" />, run } })
      const rows: MenuOption[] = [{ value: '#back', label: 'Hidden', noSearch: true, run: () => setView('folders'), lead: <ChevronLeft size={15} aria-hidden="true" /> }]
      // eslint-disable-next-line react-hooks/refs -- restore only stores this handler; the latest-view ref is read after the user restores temporary folders.
      if (hidden?.temporary === 'hidden' && hidden.temporaryCount) rows.push({ value: '#temporary', label: 'Temporary folders', detail: `${hidden.temporaryCount} chats`, group: 'Folders', ...restore(() => setMode('collapsed'), 'Show temporary folders') })
      for (const item of hidden?.dirs ?? []) {
        const { name, parent } = folderParts(item.dir)
        // eslint-disable-next-line react-hooks/refs -- restore only stores this handler; the latest-view ref is read after the user restores a folder.
        rows.push({ value: `#dir:${item.dir}`, label: name, detail: parent || undefined, count: item.count, search: item.dir, group: 'Folders', ...restore(() => void post({ kind: 'dir', dir: item.dir, hidden: false }), `Show ${item.dir}`) })
      }
      // eslint-disable-next-line react-hooks/refs -- restore only stores this handler; the latest-view ref is read after the user restores a chat.
      for (const chat of hidden?.chats ?? []) rows.push({ value: `#chat:${chat.agent}:${chat.id}`, label: chat.title || 'Untitled chat', detail: chat.dir ?? undefined, group: 'Chats', ...restore(() => void post({ kind: 'chat', agent: chat.agent, id: chat.id, hidden: false }), `Show ${chat.title || 'this chat'}`) })
      return rows
    }
    const remove = <X size={14} aria-hidden="true" />
    const row = ({ dir, count }: HistoryDir, group: string, onlySearch = false): MenuOption => {
      const { name, parent } = folderParts(dir)
      return { value: dir, label: name, detail: parent || undefined, count: count || undefined, group, search: dir, onlySearch, trailing: { label: `Remove ${dir} from lists`, icon: remove, run: () => hideDir(dir) } }
    }
    const known = folders.some(item => item.dir === value) || !value ? folders : [...folders, { dir: value, count: 0 }]
    const isTemporary = (item: HistoryDir) => item.temporary ?? TEMPORARY.test(item.dir)
    const temporary = known.filter(isTemporary)
    const collapsed = shownMode !== 'open'
    const rows: MenuOption[] = [{ value: '', label: 'All folders' }, ...known.filter(item => !isTemporary(item)).map(item => row(item, 'Folders'))]
    if (shownMode !== 'hidden' && temporary.length) {
      // eslint-disable-next-line react-hooks/refs -- These stored click and undo handlers read the latest-view ref only after a user action.
      rows.push({ value: '#temporary', label: 'Temporary', noSearch: true, count: temporary.reduce((sum, item) => sum + item.count, 0), lead: collapsed ? <ChevronRight size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />,
        reveal: collapsed, run: () => setMode(collapsed ? 'open' : 'collapsed'), trailing: { label: 'Hide all temporary chats', icon: <EyeOff size={14} aria-hidden="true" />, run: () => { if (temporary.some(item => item.dir === value)) onChange(''); const before = shownMode; onRemoved?.('Temporary folders hidden', () => { setLocalMode(before); return post({ kind: 'temporary', mode: before }) }); setMode('hidden') } } })
      rows.push(...temporary.map(item => row(item, 'Temporary', collapsed)))
    }
    if (hiddenCount > 0) rows.push({ value: '#hidden', label: 'Hidden', noSearch: true, count: hiddenCount, lead: <EyeOff size={14} aria-hidden="true" />, run: openHidden })
    return rows
    // The handlers only use the values listed here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, hidden, folders, value, shownMode, hiddenCount])
  const current = value ? folderParts(value).name : 'All folders'
  return <SelectMenu value={value} options={options} onChange={onChange} label="Folder" className="cp-folder" minWidth={340} searchLabel="Search folders" emptyText={view === 'hidden' ? 'Nothing else is hidden.' : 'No folders match.'}
    triggerLabel={current} triggerTitle={value || undefined} onClosed={() => setView('folders')} icon={<Folder size={13} aria-hidden="true" />} />
}
