import { VoiceField } from './VoiceField'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, FormEvent, KeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode, RefObject } from 'react'
import { Check, ChevronRight, ClipboardList, FileText, GripVertical, ListChecks, Pencil, Plus, Send, Trash2, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, Pane } from './api'
import { FILTERS, addItem, asFilter, blockOf, filterCounts, filterItems, highestRef, moveItem, openGroups, parseBacklog, removeItem, restoreItem, rewordItem, setDone, startItem, withIds } from './backlogItems'
import type { Change, Filter, Item, Note, Source, Status } from './backlogItems'
import { copyText } from './clipboard'
import { useModal } from './hooks'
import { usePanelPoll } from './panelProps'
import { BACKLOG_PATH, markSeen, readSeen, useBacklogNews } from './backlogNews'
import { agentTabs, itemPrompt, sendToTab, startTab } from './backlogSend'
import './Backlog.css'

// The app's one backlog (server: backlog.py). People use the checklist or the text; agents use a command. Every save
// carries the version the person last saw. A checklist change applies itself again to a newer file, so only rewriting
// the text ever asks which version to keep.
interface Saved { text: string; version: string; limit: number; command?: string }
const POLL_MS = 3000, SAVE_MS = 800, UNDO_MS = 8000, RETRIES = 5
const PATH = BACKLOG_PATH
/** A refused save says why. Anything else means the server could not be reached. */
const saveProblem = (failure: unknown) => failure instanceof ApiError ? errorText(failure) : failure instanceof Error && failure.message.startsWith('The backlog') ? failure.message : 'Cannot reach Tether.'
// The chosen filter, kept per browser.
const FILTER_KEY = 'tether.backlog.filter'
const FILTER_NAMES: Record<Filter, string> = { all: 'All', mine: 'Mine', agents: 'From agents', new: 'New' }
function readFilter(): Filter {
  try { return asFilter(localStorage.getItem(FILTER_KEY)) } catch { return 'all' }
}

/** How the sidebar names a tab. */
const tabName = (pane: Pane) => pane.pane_name || pane.agent_name || pane.label || pane.id
/** The open tab a source names: the same pane, still on the same chat when both are known. */
const tabOf = (source: Source | undefined, panes: Pane[]) => source?.pane ? panes.find(pane => pane.id === source.pane && (!source.chat || !pane.thread || pane.thread === source.chat)) : undefined

interface Links { panes: Pane[]; onOpenPane?: (pane: Pane) => void }

/** Who added an item, or works on it, short enough to scan. An open tab opens; otherwise it copies the chat ID. The full IDs are in the title. */
function SourceTag({ source, links, verb = 'Added by' }: { source: Source; links: Links; verb?: string }) {
  const [copied, setCopied] = useState(false)
  const short = source.chat?.slice(0, 8)
  const full = [source.tmux && `tmux ${source.tmux}`, source.pane && `pane ${source.pane}`, source.chat && `chat ${source.chat}`].filter(Boolean).join(' · ')
  const tab = tabOf(source, links.panes)
  if (tab && links.onOpenPane) {
    const open = links.onOpenPane
    return <button type="button" className="backlog-tag backlog-tag-open" title={`${full}. Open this tab.`} aria-label={`${verb} ${source.agent}: open ${tabName(tab)}`} onClick={() => open(tab)}>{source.agent} · {tabName(tab)}</button>
  }
  const label = `${source.agent}${short ? ` · ${short}` : ''}`
  if (!source.chat) return <span className="backlog-tag" title={full}>{label}</span>
  return <button type="button" className="backlog-tag" title={`${full}. Click to copy the chat ID.`} aria-label={`${verb} ${source.agent}. Copy chat ID ${short}`}
    onClick={() => { void copyText(source.chat!).then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1500) }, () => {}) }}>{copied ? 'Copied' : label}</button>
}

/** Who works on the item now, or what came of it. */
function StatusLine({ status, links }: { status: Status; links: Links }) {
  return <div className={`backlog-state ${status.kind}`}>
    <span className="backlog-pill">{status.kind === 'doing' ? 'Doing' : 'Done'}</span>
    {status.text && <span className="backlog-result">{status.text}</span>}
    {status.source && <SourceTag source={status.source} links={links} verb={status.kind === 'doing' ? 'In progress in' : 'Done by'} />}
  </div>
}

const folderName = (dir: string) => dir.replace(/\/+$/, '').split('/').pop() || dir

/** Pick a chat for an item: send it there, or start a new tab beside it. */
function SendPanel({ item, api, links, command, onSent, onClose }: { item: Item; api: Api; links: Links; command?: string; onSent: (who: string, pane: Pane | null, name: string) => void; onClose: () => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const tabs = agentTabs(links.panes)
  const run = async (key: string, work: () => Promise<void>) => {
    setBusy(key); setError('')
    try { await work() } catch (failure) { setError(errorText(failure)); setBusy('') }
  }
  const message = itemPrompt(item, command)
  return <div className="backlog-send" role="group" aria-label={`Send ${item.ref} to a chat`} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}>
    <div className="backlog-send-head"><span>Send {item.ref} to</span><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={14} /></button></div>
    {!tabs.length ? <p className="backlog-send-empty">No Claude or Codex tab is open.</p>
      : <ul>{tabs.map(pane => <li key={pane.id}>
          <span className="backlog-send-name"><strong>{tabName(pane)}</strong><span>{pane.agent} · {folderName(pane.dir)}</span></span>
          <button type="button" className="secondary" disabled={!!busy} autoFocus={pane === tabs[0]}
            onClick={() => void run(`send ${pane.id}`, async () => { const thread = await sendToTab(api, pane, message); onSent(`${pane.agent} · pane ${pane.id}${thread ? ` · chat ${thread}` : ''}`, pane, tabName(pane)) })}>
            <Send size={14} aria-hidden="true" />{busy === `send ${pane.id}` ? 'Sending…' : 'Send'}</button>
          <button type="button" className="secondary" disabled={!!busy || !pane.thread} title={pane.thread ? `A new ${pane.agent} tab in ${folderName(pane.dir)}` : 'This tab has no chat yet'}
            onClick={() => void run(`new ${pane.id}`, async () => { const made = await startTab(api, pane, message); onSent(`${pane.agent} · pane ${made.pane}`, null, made.name) })}>
            <Plus size={14} aria-hidden="true" />{busy === `new ${pane.id}` ? 'Starting…' : 'New tab'}</button>
        </li>)}</ul>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>
}

/** Where a caret goes after the server adds IDs at the ends of lines: the same place in the same line. */
function moveCaret(before: string, after: string, at: number) {
  const old = before.split('\n'), now = after.split('\n')
  if (old.length !== now.length) return Math.min(at, after.length)
  let offset = 0
  for (let line = 0; line < old.length; line += 1) {
    if (at <= old[line].length) return offset + at
    at -= old[line].length + 1
    offset += now[line].length + 1
  }
  return after.length
}

interface RowActions {
  onDone: (raw: string, line: number, done: boolean) => void
  onDelete: (item: Item) => void
  onReword: (item: Item, wording: string) => void
  onDragStart: (event: ReactPointerEvent, item: Item) => void
  onDragKey: (event: KeyboardEvent, item: Item) => void
  onSend?: (item: Item) => void
}

function ItemRow({ item, actions, links, sending, locked, fixed, editing, setEditing, typed: typedRef, drop, dragged }: { item: Item; actions: RowActions; links: Links; sending?: ReactNode; locked: boolean; fixed: boolean; editing: boolean; setEditing: (id: string | null) => void; typed: RefObject<string>; drop?: 'before' | 'after'; dragged: boolean }) {
  const [wording, setWording] = useState(item.text)
  // Sub-steps stay in view. Notes (the why, the files, selected chat text) fold under the title until opened.
  const [showNotes, setShowNotes] = useState(false)
  const notes = item.details.filter(detail => detail.done === undefined)
  const shown = showNotes ? item.details : item.details.filter(detail => detail.done !== undefined)
  const ended = useRef(false)
  const finish = (keep: boolean) => {
    if (ended.current) return
    ended.current = true
    typedRef.current = ''
    setEditing(null)
    if (keep && wording.trim() && wording.trim() !== item.text) actions.onReword(item, wording)
  }
  return <li data-backlog-row={item.done ? undefined : item.id} className={['backlog-row', item.done && 'done', drop && `drop-${drop}`, dragged && 'dragged'].filter(Boolean).join(' ')}
    style={dragged ? { opacity: 1, zIndex: 1, background: 'var(--bg)', boxShadow: '0 3px 10px rgb(0 0 0 / .16)' } : undefined}>
    {!item.done && <button type="button" className="backlog-grip" aria-label={fixed ? `Move ${item.text}. Show All to reorder.` : `Move ${item.text}. Use the up and down arrow keys.`} title={fixed ? 'Show All to reorder' : 'Drag to reorder'} disabled={locked || fixed}
      onPointerDown={event => actions.onDragStart(event, item)} onKeyDown={event => actions.onDragKey(event, item)}
      onTouchStart={event => event.stopPropagation()} onTouchEnd={event => event.stopPropagation()}><GripVertical size={15} aria-hidden="true" /></button>}
    <input type="checkbox" className="backlog-box" aria-label={item.text} checked={item.done} disabled={locked} onChange={event => actions.onDone(item.raw, item.line, event.target.checked)} />
    <div className="backlog-body">
      {editing
        ? <VoiceField label="backlog title" value={wording} setValue={value => { setWording(value); typedRef.current = value }} onLeave={() => finish(true)} field={{ 'aria-label': `Reword ${item.text}`, className: 'backlog-input backlog-reword', autoFocus: true,
            onFocus: event => { ended.current = false; event.target.select() },
            onKeyDown: event => { if (event.key === 'Enter') { event.preventDefault(); finish(true) } else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(false) } } }} />
        : <span className="backlog-text-line" onDoubleClick={() => { if (!locked && !item.done) { setWording(item.text); setEditing(item.id) } }}>{item.text}</span>}
      {(item.ref || item.source) && <span className="backlog-meta">
        {item.ref && <span className="backlog-ref" title="The ID agents use for this item">{item.ref}</span>}
        {item.source && <SourceTag source={item.source} links={links} />}
      </span>}
      {item.status && (item.status.kind === 'done' ? item.done : !item.done) && <StatusLine status={item.status} links={links} />}
      {notes.length > 0 && <button type="button" className="backlog-done-toggle backlog-notes-toggle" aria-expanded={showNotes} onClick={() => setShowNotes(!showNotes)}>
        <ChevronRight size={14} className={showNotes ? 'open' : ''} aria-hidden="true" />Details · {notes.length}</button>}
      {shown.length > 0 && <ul className="backlog-details">{shown.map(detail => <li key={detail.line}>
        {detail.done === undefined ? <span>{detail.text}</span>
          : <label className={`backlog-sub${detail.done ? ' done' : ''}`}><input type="checkbox" checked={detail.done} disabled={locked} onChange={event => actions.onDone(detail.raw, detail.line, event.target.checked)} /><span>{detail.text}</span></label>}
      </li>)}</ul>}
    </div>
    {!editing && <span className="backlog-actions">
      {!item.done && actions.onSend && <button type="button" className="icon-button" aria-label={`Send ${item.text} to an agent`} title="Send to an agent" disabled={locked || !item.ref} onClick={() => actions.onSend!(item)}><Send size={15} /></button>}
      {!item.done && <button type="button" className="icon-button" aria-label={`Reword ${item.text}`} title="Reword" disabled={locked} onClick={() => { setWording(item.text); setEditing(item.id) }}><Pencil size={15} /></button>}
      <button type="button" className="icon-button" aria-label={`Delete ${item.text}`} title="Delete" disabled={locked} onClick={() => actions.onDelete(item)}><Trash2 size={15} /></button>
    </span>}
    {sending}
  </li>
}

function NoteRow({ note }: { note: Note }) {
  return <li className="backlog-free">{note.text}</li>
}

interface Undo { label: string; change: Change }
/** What a close has to ask about: text that would otherwise be lost. */
export type BacklogGuard = RefObject<() => boolean>

export function Backlog({ api, guard: guardRef, active = true, panes = [], onOpenPane }: { api: Api; guard?: BacklogGuard; active?: boolean; panes?: Pane[]; onOpenPane?: (pane: Pane) => void }) {
  const [saved, setSaved] = useState<Saved | null>(null)
  const [changes, setChanges] = useState<Change[]>([])  // made in the checklist, not saved yet
  const [draft, setDraft] = useState<string | null>(null)  // the text while the person edits it as text
  const [item, setItem] = useState('')
  const [itemVoice, setItemVoice] = useState(false)
  const [textVoice, setTextVoice] = useState(false)
  const [error, setError] = useState('')
  const [failed, setFailed] = useState('')  // saving failed for a reason that a retry can fix
  const [saving, setSaving] = useState(false)
  const [editingRow, setEditingRow] = useState<string | null>(null)
  const [showDone, setShowDone] = useState(false)
  const [conflict, setConflict] = useState<Saved | null>(null)  // the newer file, while the person chooses
  const [undo, setUndo] = useState<Undo | null>(null)
  const [drag, setDrag] = useState<{ item: Item; target: string | null; after: boolean } | null>(null)
  const [sendFor, setSendFor] = useState<string | null>(null)  // the row whose Send panel is open
  const [sent, setSent] = useState<{ label: string; pane: Pane | null } | null>(null)
  const [filter, setFilter] = useState(readFilter)
  // What was seen before this sheet opened. Opening marks every item seen, so New needs the older mark.
  const [seenBefore] = useState(readSeen)
  const textBox = useRef<HTMLTextAreaElement>(null)
  const caret = useRef<number | null>(null)
  const typedRow = useRef('')
  const addBox = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const refocus = useRef<string | null>(null)
  const reveal = useRef<string | null>(null)
  const queue = useRef<Change[]>([])
  const working = useRef(false)
  const saves = useRef(0)  // a load that started before a save has old text
  const current = useRef<Saved | null>(null)
  const latest = useRef({ draft, conflict, failed })
  useEffect(() => { latest.current = { draft, conflict, failed } })

  const shown = saved ? changes.reduce((text, change) => change(text) ?? text, saved.text) : ''
  const dirty = !!saved && draft !== null && draft !== saved.text
  const tooLong = !!saved && draft !== null && draft.length > saved.limit
  const unsaved = changes.length > 0 || dirty

  const remember = useCallback((next: Saved) => { current.current = next; setSaved(next) }, [])
  const fetchLatest = useCallback((signal?: AbortSignal) => api<Saved>(PATH, undefined, signal), [api])

  const work = useCallback(async () => {
    if (working.current) return
    working.current = true
    setSaving(true)
    try {
      while (queue.current.length && current.current) {
        const change = queue.current[0]
        let base: Saved = current.current
        let settled = false
        for (let attempt = 0; attempt < RETRIES && !settled; attempt += 1) {
          const value = change(base.text)
          if (value === null) { setError('That item was changed elsewhere, so your change was not applied.'); settled = true; break }
          if (value === base.text) { settled = true; break }
          try {
            remember(await api<Saved>(PATH, { text: value, version: base.version }))
            saves.current += 1
            settled = true
          } catch (failure) {
            if (failure instanceof ApiError && failure.status === 409) {
              base = await fetchLatest()
              remember(base)
            } else if (failure instanceof ApiError && failure.status < 500 && failure.status !== 401 && failure.status !== 403) {
              setError(errorText(failure))
              settled = true
            } else throw failure
          }
        }
        if (!settled) throw new Error('The backlog keeps changing. Your change is not saved yet.')
        queue.current.shift()
        setChanges([...queue.current])
      }
      setFailed('')
    } catch (failure) {
      setFailed(saveProblem(failure))
    } finally {
      working.current = false
      setSaving(false)
    }
  }, [api, fetchLatest, remember])

  const load = useCallback(async (signal: AbortSignal) => {
    const started = saves.current
    try {
      const next = await fetchLatest(signal)
      const now = latest.current
      if (signal.aborted || started !== saves.current || working.current || now.conflict) return
      if (now.failed) { void work(); return }
      if (!current.current) { setError(''); remember(next); return }
      if (next.version === current.current.version) return
      if (now.draft === null) remember(next)
      else if (now.draft === current.current.text) { remember(next); setDraft(next.text) }
    } catch (failure) {
      if (!signal.aborted && !current.current) setError(errorText(failure))
    }
  }, [fetchLatest, remember, work])
  usePanelPoll(load, POLL_MS, active)

  /** Show a checklist change now and save it in order. false: it cannot be made, and nothing changed. */
  const change = useCallback((made: Change, offer?: Undo) => {
    if (!current.current) return false
    // The server numbers new items. Numbering here too shows the saved lines, so a later change finds them.
    const next: Change = text => { const value = made(text); return value === null ? null : withIds(value) }
    const text = queue.current.reduce((value, queued) => queued(value) ?? value, current.current.text)
    const value = next(text)
    if (value === null) { setError('That item was changed elsewhere. Check the list and try again.'); return false }
    if (value.length > current.current.limit) { setError(`The backlog is full (${current.current.limit.toLocaleString()} characters). Delete some items first.`); return false }
    setError('')
    queue.current.push(next)
    setChanges([...queue.current])
    setUndo(offer ?? null)
    void work()
    return true
  }, [work])

  const saveDraft = useCallback(async (over: Saved, value: string) => {
    setSaving(true); setFailed('')
    saves.current += 1
    try {
      const next = await api<Saved>(PATH, { text: value, version: over.version })
      saves.current += 1
      remember(next); setConflict(null)
      // The server numbered new items. Show the numbers, and keep the cursor on its line.
      if (latest.current.draft === value && next.text !== value) {
        const box = textBox.current
        if (box && document.activeElement === box) caret.current = moveCaret(value, next.text, box.selectionStart)
        setDraft(next.text)
      }
      return true
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        try { setConflict(await fetchLatest()) } catch (again) { setFailed(saveProblem(again)) }
      } else setFailed(saveProblem(failure))
      return false
    } finally { setSaving(false) }
  }, [api, fetchLatest, remember])

  useEffect(() => {
    if (!saved || !dirty || tooLong || saving || conflict || failed) return
    const timer = window.setTimeout(() => void saveDraft(saved, draft!), SAVE_MS)
    return () => window.clearTimeout(timer)
  }, [saved, dirty, tooLong, saving, conflict, failed, draft, saveDraft])

  useLayoutEffect(() => {
    if (caret.current === null || !textBox.current) return
    textBox.current.setSelectionRange(caret.current, caret.current)
    caret.current = null
  }, [draft])

  useEffect(() => {
    if (!undo) return
    const timer = window.setTimeout(() => setUndo(null), UNDO_MS)
    return () => window.clearTimeout(timer)
  }, [undo])

  const fresh = useRef({ item, conflict, failed, tooLong, dirty, changes: changes.length })
  useEffect(() => { fresh.current = { item, conflict, failed, tooLong, dirty, changes: changes.length } })
  useEffect(() => {
    if (!guardRef) return
    guardRef.current = () => { const now = fresh.current; return !!now.item.trim() || !!typedRow.current.trim() || !!now.conflict || !!now.tooLong || (!!now.failed && (now.changes > 0 || now.dirty)) }
  }, [guardRef])

  // Closing the sheet keeps what was typed. The version check still protects an agent's edit.
  useEffect(() => () => {
    const last = current.current, value = latest.current.draft
    if (last && value !== null && value !== last.text && value.length <= last.limit && !latest.current.conflict) void api(PATH, { text: value, version: last.version }).catch(() => {})
  }, [api])

  // A page close can stop a save that has not reached the server.
  useEffect(() => {
    if (!unsaved) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [unsaved])

  // The add box is disabled until the list loads, so the cursor goes there then. A phone keeps its keyboard closed.
  const loaded = !!saved
  useEffect(() => {
    if (loaded && window.matchMedia('(pointer: fine)').matches) addBox.current?.focus()
  }, [loaded])

  useEffect(() => {
    if ((!refocus.current && !reveal.current) || !list.current) return
    const rows = [...list.current.querySelectorAll<HTMLElement>('[data-backlog-row]')]
    rows.find(row => row.dataset.backlogRow === refocus.current)?.querySelector<HTMLElement>('.backlog-grip')?.focus()
    rows.filter(row => reveal.current && row.dataset.backlogRow?.startsWith(reveal.current)).pop()?.scrollIntoView({ block: 'nearest' })
    refocus.current = reveal.current = null
  })

  const add = (event: FormEvent) => {
    event.preventDefault()
    if (!itemVoice && item.trim() && change(addItem(item))) {
      reveal.current = `- [ ] ${item.trim().replace(/\s*\n\s*/g, ' ')}\n`; setItem('')
      // The person's own item is hidden under these two filters.
      if (filter === 'agents' || filter === 'new') pick('all')
    }
  }
  const pick = (next: Filter) => { setFilter(next); try { localStorage.setItem(FILTER_KEY, next) } catch { /* private window: the filter lasts until reload */ } }
  const rows = parseBacklog(shown)
  const items = rows.filter((row): row is Item => row.kind === 'item')
  const counts = filterCounts(items, seenBefore)
  const filtered = filter !== 'all'
  const visible = new Set(filterItems(items, filter, seenBefore))
  const allGroups = openGroups(rows)
  // A filter shows items only: a free line belongs to nobody.
  const groups = !filtered ? allGroups : allGroups.map(group => ({ ...group, rows: group.rows.filter(row => row.kind === 'item' && visible.has(row)) })).filter(group => group.rows.length)
  const done = items.filter(row => row.done && visible.has(row))
  const highest = highestRef(items)
  useEffect(() => { if (active && highest) markSeen(highest) }, [active, highest])
  const hasHeadings = allGroups.some(group => group.heading)
  const openItems = items.filter(row => !row.done)
  const shownOpen = groups.flatMap(group => group.rows.filter((row): row is Item => row.kind === 'item'))
  const locked = !saved || !!conflict

  const short = (text: string) => text.length > 40 ? `${text.slice(0, 40)}…` : text
  const actions: RowActions = {
    onDone: (raw, line, value) => {
      const after = raw.replace(/^(\s*[-*] \[)[ xX](\] )/, `$1${value ? 'x' : ' '}$2`)
      const row = rows.find((one): one is Item => one.kind === 'item' && one.raw === raw)
      change(setDone(raw, line, value), { label: value ? `Done: ${short(row?.text ?? 'item')}` : `Reopened: ${short(row?.text ?? 'item')}`, change: setDone(after, line, !value) })
    },
    onDelete: target => {
      const removed = blockOf(shown, target.raw, target.line)
      if (removed) change(removeItem(target.raw, target.line), { label: `Deleted: ${short(target.text)}`, change: restoreItem(removed) })
    },
    onReword: (target, wording) => { change(rewordItem(target, wording)) },
    onSend: target => { setSent(null); setSendFor(now => now === target.id ? null : target.id) },
    onDragStart: (event, target) => {
      if (filtered || event.button !== 0 || !(event.currentTarget instanceof HTMLElement)) return
      const box = list.current
      const row = box?.querySelector<HTMLElement>(`[data-backlog-row="${CSS.escape(target.id)}"]`)
      if (!box || !row) return
      event.preventDefault()
      const grip = event.currentTarget, pointer = event.pointerId
      grip.setPointerCapture(pointer)
      const startY = event.clientY, startScroll = box.scrollTop
      const area = row.getBoundingClientRect()
      const places = [...box.querySelectorAll<HTMLElement>('[data-backlog-row]')]
      const min = places[0].getBoundingClientRect().top - area.top
      const max = places[places.length - 1].getBoundingClientRect().bottom - area.bottom
      // eslint-disable-next-line react-hooks/purity -- Runs in the row's pointerdown handler, not during render.
      let offset = 0, lastY = startY, lastAt = performance.now(), speed = 0
      let destination: { target: string | null; after: boolean } = { target: null, after: false }
      row.style.transition = 'none'
      const locate = (y: number) => {
        let found: { target: string | null; after: boolean } = { target: null, after: false }
        for (const place of places) {
          const rect = place.getBoundingClientRect()
          // The lifted row keeps its original slot for hit testing.
          const center = rect.top + rect.height / 2 - (place === row ? offset : 0)
          if (y < center) { found = { target: place.dataset.backlogRow!, after: false }; break }
        }
        if (!found.target && places.length) found = { target: places[places.length - 1].dataset.backlogRow!, after: true }
        if (found.target !== destination.target || found.after !== destination.after) {
          destination = found
          setDrag({ item: target, ...found })
        }
      }
      const moved = (at: PointerEvent) => {
        if (at.pointerId !== pointer) return
        const now = performance.now()
        if (now > lastAt) speed = speed * .4 + (at.clientY - lastY) / (now - lastAt) * .6
        lastY = at.clientY; lastAt = now
        const edges = box.getBoundingClientRect()
        if (at.clientY < edges.top + 36) box.scrollTop -= 8
        else if (at.clientY > edges.bottom - 36) box.scrollTop += 8
        const y = at.clientY - startY + box.scrollTop - startScroll
        const bound = Math.max(min, Math.min(max, y))
        offset = bound + (y - bound) * .25
        row.style.transform = `translateY(${offset}px)`
        locate(at.clientY)
      }
      const finish = (ended: PointerEvent) => {
        if (ended.pointerId !== pointer) return
        grip.removeEventListener('pointermove', moved)
        grip.removeEventListener('pointerup', finish)
        grip.removeEventListener('pointercancel', finish)
        grip.removeEventListener('lostpointercapture', finish)
        if (ended.type === 'pointerup' && !matchMedia('(prefers-reduced-motion: reduce)').matches && performance.now() - lastAt < 80 && Math.abs(speed) > .4 && Math.sign(speed) === Math.sign(lastY - startY)) {
          const direction = Math.sign(speed), neighbor = places[places.indexOf(row) + direction]?.getBoundingClientRect()
          const edge = neighbor ? neighbor.top + neighbor.height / 2 + direction : lastY
          locate(direction > 0 ? Math.max(edge, lastY + speed * 100) : Math.min(edge, lastY + speed * 100))
        }
        const place = openItems.find(item => item.id === destination.target)
        if (place && ended.type === 'pointerup' && place.id !== target.id) change(moveItem(target.raw, target.line, place.raw, place.line, destination.after))
        row.style.transform = ''; row.style.transition = ''
        setDrag(null)
      }
      setDrag({ item: target, target: null, after: false })
      grip.addEventListener('pointermove', moved)
      grip.addEventListener('pointerup', finish)
      grip.addEventListener('pointercancel', finish)
      grip.addEventListener('lostpointercapture', finish)
    },
    onDragKey: (event, target) => {
      if (filtered || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return
      event.preventDefault()
      const at = openItems.findIndex(row => row.id === target.id)
      const up = event.key === 'ArrowUp'
      const next = openItems[at + (up ? -1 : 1)]
      if (!next) return
      refocus.current = target.id
      change(moveItem(target.raw, target.line, next.raw, next.line, !up))
    },
  }
  const links: Links = { panes, onOpenPane }
  const showRow = (row: Item | Note) => row.kind === 'note'
    ? <NoteRow key={row.id} note={row} />
    : <ItemRow key={row.id} item={row} actions={actions} links={links} locked={locked} fixed={filtered}
        sending={sendFor === row.id && !row.done ? <SendPanel item={row} api={api} links={links} command={saved?.command} onClose={() => setSendFor(null)}
          onSent={(who, pane, name) => { setSendFor(null); change(startItem(row.raw, row.line, who)); setSent({ label: `Sent ${row.ref} to ${name}`, pane }) }} /> : undefined} editing={editingRow === row.id} setEditing={setEditingRow} typed={typedRow} dragged={drag?.item.id === row.id}
        drop={drag?.target === row.id && drag.item.id !== row.id ? (drag.after ? 'after' : 'before') : undefined} />
  const clearDone = () => {
    const blocks = done.map(row => blockOf(shown, row.raw, row.line)).filter(block => block !== null).sort((a, b) => a.line - b.line)
    const remove: Change = text => done.reduce<string | null>((value, row) => { if (value === null) return null; return removeItem(row.raw, row.line)(value) ?? value }, text)
    const restore: Change = text => blocks.reduce<string | null>((value, block) => value === null ? null : restoreItem(block)(value), text)
    change(remove, { label: `Cleared ${done.length} done`, change: restore })
    setShowDone(false)
  }
  const leaveText = async () => {
    if (draft === null || !saved) return setDraft(null)
    if (draft === saved.text) return setDraft(null)
    if (tooLong || conflict) return
    if (await saveDraft(saved, draft)) setDraft(null)
  }
  const retry = () => {
    setFailed('')
    if (draft !== null && saved && dirty) void saveDraft(saved, draft)
    else void work()
  }
  const inText = draft !== null

  const status = !saved ? 'Loading…'
    : tooLong ? `Keep it under ${saved.limit.toLocaleString()} characters.`
    : unsaved || saving ? 'Saving…'
    : !rows.length ? ''
    : filtered ? `${shownOpen.length} of ${openItems.length} open shown · Saved`
    : `${openItems.length} open · Saved`

  return <div className="backlog">
    <div className="backlog-top">
      {inText
        ? <p className="backlog-mode">Editing as text. Items are Markdown lines like <code>- [ ] Item</code>.</p>
        : <form className="backlog-add" onSubmit={add}>
            <VoiceField label="new backlog item" onBusyChange={setItemVoice} value={item} setValue={setItem} fieldRef={addBox} field={{ 'aria-label': 'New backlog item', className: 'backlog-input', type: 'text', disabled: locked, placeholder: 'Add an item', enterKeyHint: 'done' }} />
            <button type="submit" className="secondary" disabled={locked || itemVoice || !item.trim()}>Add</button>
          </form>}
      <button type="button" className="icon-button" aria-pressed={inText} disabled={!saved || itemVoice || textVoice || (!inText && changes.length > 0)} aria-label={inText ? 'Show checklist' : 'Edit as text'} title={inText ? 'Show checklist' : 'Edit as text'}
        onClick={() => { if (inText) void leaveText(); else { setUndo(null); setDraft(shown) } }}>{inText ? <ListChecks size={17} /> : <FileText size={16} />}</button>
    </div>
    {!inText && saved && !!rows.length && <div className="backlog-filters">
      <div role="group" aria-label="Filter the backlog">
        {FILTERS.map(name => <button key={name} type="button" className="backlog-chip" aria-pressed={filter === name} onClick={() => pick(name)}>{FILTER_NAMES[name]}<span>{counts[name]}</span></button>)}
      </div>
      {filtered && <p className="backlog-filter-hint">Show All to reorder.</p>}
    </div>}
    {inText
      ? <VoiceField label="backlog text" onBusyChange={setTextVoice} value={draft} setValue={setDraft} multiline fieldRef={textBox} field={{ 'aria-label': 'Backlog text', className: 'backlog-text', readOnly: !!conflict, disabled: !!conflict, spellCheck: true }} />
      : <div className="backlog-list" ref={list}>
          {saved && !rows.length && <div className="backlog-empty"><ClipboardList size={26} aria-hidden="true" /><p><strong>Nothing here yet</strong></p><p>Add what you want to remember. Agents add follow-ups they find, and fill in items you point them to.</p></div>}
          {saved && !!rows.length && !openItems.length && <p className="backlog-clear"><Check size={16} aria-hidden="true" />All done.</p>}
          {filtered && !!openItems.length && !shownOpen.length && <p className="backlog-clear">{filter === 'new' ? 'Nothing new from agents.' : filter === 'mine' ? 'No open items of yours.' : 'No open items from agents.'} <button type="button" className="link-button" onClick={() => pick('all')}>Show all</button></p>}
          {groups.map(group => <section key={group.heading?.id ?? 'inbox'} className="backlog-group">
            {(group.heading || hasHeadings) && <h3 className="backlog-heading"><span>{group.heading?.text ?? 'Inbox'}</span><span className="backlog-count">{group.rows.filter(row => row.kind === 'item').length}</span></h3>}
            <ul>{group.rows.map(row => showRow(row as Item | Note))}</ul>
          </section>)}
          {done.length > 0 && <div className="backlog-done">
            <div className="backlog-done-bar">
              <button type="button" className="backlog-done-toggle" aria-expanded={showDone} onClick={() => setShowDone(!showDone)}><ChevronRight size={15} className={showDone ? 'open' : ''} aria-hidden="true" />Done · {done.length}</button>
              {showDone && <button type="button" className="link-button backlog-clear-done" disabled={locked} onClick={clearDone}>Clear done</button>}
            </div>
            {showDone && <ul>{done.map(showRow)}</ul>}
          </div>}
        </div>}
    {sent && !undo && !inText && <div className="backlog-undo" role="status"><span>{sent.label}</span>{sent.pane && onOpenPane && <button type="button" className="link-button" onClick={() => onOpenPane(sent.pane!)}>Open</button>}<button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setSent(null)}><X size={14} /></button></div>}
    {undo && !inText && <div className="backlog-undo" role="status"><span>{undo.label}</span><button type="button" className="link-button" onClick={() => { const back = undo.change; setUndo(null); change(back) }}>Undo</button><button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setUndo(null)}><X size={14} /></button></div>}
    {conflict ? <div className="confirm-box" role="alertdialog" aria-labelledby="backlog-conflict">
      <p id="backlog-conflict"><strong>The backlog changed while you were editing.</strong> An agent or another tab saved a newer version.</p>
      <div className="sheet-buttons">
        <button type="button" className="secondary" disabled={saving} onClick={() => { remember(conflict); setDraft(conflict.text); setConflict(null) }}>Use the newer version</button>
        <button type="button" className="secondary danger-text" disabled={saving} onClick={() => { if (draft !== null) void saveDraft(conflict, draft) }}>Keep mine</button>
      </div>
    </div>
    : failed && unsaved ? <p className="error" role="alert"><strong>Not saved.</strong> {failed} Tether will keep trying. <button type="button" className="link-button" onClick={retry}>Try now</button></p>
    : error ? <p className="error" role="alert">{error} <button type="button" className="link-button" onClick={() => setError('')}>Dismiss</button></p>
    : <p className={`backlog-status${tooLong ? ' error' : ''}`} role="status">{status}</p>}
  </div>
}

/** The sheet the sidebar opens. Closing asks first only when text would be lost. */
// The sheet's size, kept per browser. A phone sheet sits on the bottom edge, so only its height changes. A wider screen
// centres the sheet, so a drag of the corner grows it on both sides: twice the pointer's move keeps the corner under it.
const SIZE_KEY = 'tether.backlogSize'
const WIDE = '(min-width: 600px)'
interface SheetSize { width?: number; height?: number; phoneHeight?: number }
const clamp = (value: number, low: number, high: number) => Math.round(Math.max(low, Math.min(value, Math.max(low, high))))
function readSize(): SheetSize {
  try {
    const saved = JSON.parse(localStorage.getItem(SIZE_KEY) || '{}') as Record<string, unknown>
    const size: SheetSize = {}
    for (const key of ['width', 'height', 'phoneHeight'] as const) if (typeof saved[key] === 'number' && saved[key] > 0) size[key] = saved[key]
    return size
  } catch { return {} }
}
function useSheetSize() {
  const [size, setSize] = useState(readSize)
  const [wide, setWide] = useState(() => window.matchMedia(WIDE).matches)
  useEffect(() => {
    const query = window.matchMedia(WIDE), changed = () => setWide(query.matches)
    query.addEventListener('change', changed)
    return () => query.removeEventListener('change', changed)
  }, [])
  const save = (next: SheetSize) => { setSize(next); try { localStorage.setItem(SIZE_KEY, JSON.stringify(next)) } catch { /* private window: the size lasts until reload */ } }
  const px = (value?: number) => value ? `${value}px` : undefined
  const style = (wide ? { '--backlog-w': px(size.width), '--backlog-h': px(size.height) } : { '--backlog-h': px(size.phoneHeight) }) as unknown as CSSProperties
  return { size, wide, style, save }
}

/** A grabber on the phone's top edge, or a grip in the wide sheet's corner. Arrow keys resize too; a double click resets. */
function SheetResize({ sheet, sizing, resized }: { sheet: RefObject<HTMLDialogElement | null>; sizing: ReturnType<typeof useSheetSize>; resized: RefObject<number> }) {
  const { size, wide, save } = sizing
  const limits = () => ({ width: [360, window.innerWidth - 32], height: [wide ? 320 : 240, window.innerHeight - (wide ? 32 : 24)] }) as const
  const fit = (width: number, height: number): SheetSize => {
    const { width: w, height: h } = limits()
    return wide ? { ...size, width: clamp(width, w[0], w[1]), height: clamp(height, h[0], h[1]) } : { ...size, phoneHeight: clamp(height, h[0], h[1]) }
  }
  const begin = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const box = sheet.current?.getBoundingClientRect()
    if (!box || event.button !== 0) return
    event.preventDefault()
    const handle = event.currentTarget, from = { x: event.clientX, y: event.clientY, width: box.width, height: box.height }
    handle.setPointerCapture(event.pointerId)
    let last = size, frame = 0
    const paint = () => {
      frame = 0
      if (!sheet.current) return
      if (wide) sheet.current.style.maxWidth = `${last.width ?? box.width}px`
      sheet.current.style.height = `${(wide ? last.height : last.phoneHeight) ?? box.height}px`
    }
    const move = (moved: PointerEvent) => {
      const dx = moved.clientX - from.x, dy = moved.clientY - from.y
      last = wide ? fit(from.width + 2 * dx, from.height + 2 * dy) : fit(from.width, from.height - dy)
      if (!frame) frame = requestAnimationFrame(paint)
    }
    const end = () => {
      cancelAnimationFrame(frame)
      save(last)
      sheet.current?.style.removeProperty('max-width')
      sheet.current?.style.removeProperty('height')
      resized.current = Date.now()
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', end); handle.removeEventListener('pointercancel', end)
    }
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', end); handle.addEventListener('pointercancel', end)
  }
  const key = (event: KeyboardEvent<HTMLButtonElement>) => {
    const box = sheet.current?.getBoundingClientRect()
    const step = { ArrowUp: [0, wide ? -24 : 24], ArrowDown: [0, wide ? 24 : -24], ArrowLeft: [-24, 0], ArrowRight: [24, 0] }[event.key]
    if (!box || !step || (!wide && step[0])) return
    event.preventDefault()
    save(fit(box.width + step[0], box.height + step[1]))
  }
  const reset = () => save(wide ? { phoneHeight: size.phoneHeight } : { width: size.width, height: size.height })
  // A drag down from the phone's grabber must not reach the sheet's swipe-to-close.
  const stop = (event: React.TouchEvent) => event.stopPropagation()
  return <button type="button" className={wide ? 'backlog-resize corner' : 'backlog-resize top'} aria-label="Resize the backlog"
    title={wide ? 'Drag to resize. Double-click to reset.' : 'Drag to resize'} onPointerDown={begin} onKeyDown={key} onDoubleClick={reset} onTouchStart={stop} onTouchEnd={stop}>
    {wide && <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M11 4 4 11M11 8 8 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>}
  </button>
}

export function BacklogSheet({ api, onClose, panes, onOpenPane }: { api: Api; onClose: () => void; panes?: Pane[]; onOpenPane?: (pane: Pane) => void }) {
  const guard = useRef<() => boolean>(() => false)
  const [asking, setAsking] = useState(false)
  const close = () => { if (guard.current()) setAsking(true); else onClose() }
  const modal = useModal(true, close)
  const sizing = useSheetSize()
  const resized = useRef(0)
  // A resize that ends outside the sheet sends its click to the backdrop. That click must not close the sheet.
  const onClick = (event: React.MouseEvent<HTMLDialogElement>) => { if (Date.now() - resized.current > 300) modal.onClick(event) }
  return <dialog {...modal} onClick={onClick} style={sizing.style} className="sheet settings-sheet backlog-sheet" aria-labelledby="backlog-title">
    <SheetResize sheet={modal.ref} sizing={sizing} resized={resized} />
    <div className="sheet-heading">
      <h2 id="backlog-title">Backlog</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={close}><X /></button>
    </div>
    <p className="sheet-sub">One list for all your sessions</p>
    <Backlog api={api} guard={guard} panes={panes} onOpenPane={onOpenPane && (pane => { if (!guard.current()) onClose(); onOpenPane(pane) })} />
    {asking && <div className="confirm-box backlog-leave" role="alertdialog" aria-labelledby="backlog-leave">
      <p id="backlog-leave"><strong>Some text is not saved.</strong> Close and lose it?</p>
      <div className="sheet-buttons">
        <button type="button" className="secondary" autoFocus onClick={() => setAsking(false)}>Keep editing</button>
        <button type="button" className="secondary danger-text" onClick={onClose}>Close anyway</button>
      </div>
    </div>}
  </dialog>
}

/** The sidebar's way in: how many items are open, and how many agents added since you last looked. */
export function BacklogButton({ api, onOpen }: { api: Api; onOpen: () => void }) {
  const { open, fresh } = useBacklogNews(api)
  const label = ['Backlog', open && `${open} open`, fresh.length && `${fresh.length} new from agents`].filter(Boolean).join(', ')
  return <button type="button" className="sidebar-backlog" aria-label={label} onClick={onOpen}>
    <ClipboardList size={16} aria-hidden="true" /><span>Backlog</span>
    {fresh.length > 0 && <span className="sidebar-backlog-new" title={`${fresh.length} new from agents`}>{fresh.length} new</span>}
    {open > 0 && <span className="sidebar-backlog-count">{open}</span>}
  </button>
}

/** Home's line about items agents added since you last looked. Nothing new: no line. */
export function BacklogNews({ api, onOpen }: { api: Api; onOpen: () => void }) {
  const { fresh } = useBacklogNews(api)
  if (!fresh.length) return null
  const shown = fresh.slice(-3).reverse()
  return <section className="home-backlog" aria-label="New backlog items">
    <ClipboardList size={15} aria-hidden="true" />
    <span className="home-backlog-title">{fresh.length} new in backlog</span>
    <ul>{shown.map(item => <li key={item.id}><span className="backlog-ref">{item.ref}</span><span className="home-backlog-text" title={item.text}>{item.text}</span></li>)}</ul>
    <button type="button" className="ghost sm" onClick={onOpen}>Open backlog</button>
  </section>
}
