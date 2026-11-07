import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react'
import { Check, ChevronRight, ClipboardList, FileText, GripVertical, ListChecks, Pencil, Trash2, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api } from './api'
import { addItem, blockOf, moveItem, openGroups, parseBacklog, removeItem, restoreItem, rewordItem, setDone } from './backlogItems'
import type { Change, Item, Note, Source } from './backlogItems'
import { copyText } from './clipboard'
import { useModal, usePolling } from './hooks'
import { usePanelPoll } from './panelProps'
import './Backlog.css'

// The app's one backlog (server: backlog.py). People use the checklist or the text; agents use a command. Every save
// carries the version the person last saw. A checklist change applies itself again to a newer file, so only rewriting
// the text ever asks which version to keep.
interface Saved { text: string; version: string; limit: number }
const POLL_MS = 3000, SAVE_MS = 800, UNDO_MS = 8000, RETRIES = 5
const PATH = 'projects/backlog'
/** A refused save says why. Anything else means the server could not be reached. */
const saveProblem = (failure: unknown) => failure instanceof ApiError ? errorText(failure) : failure instanceof Error && failure.message.startsWith('The backlog') ? failure.message : 'Cannot reach Tether.'

/** Who added an item, short enough to scan. It copies the chat ID; the full IDs are in the title. */
function SourceTag({ source }: { source: Source }) {
  const [copied, setCopied] = useState(false)
  const short = source.chat?.slice(0, 8)
  const full = [source.tmux && `tmux ${source.tmux}`, source.pane && `pane ${source.pane}`, source.chat && `chat ${source.chat}`].filter(Boolean).join(' · ')
  const label = `${source.agent}${short ? ` · ${short}` : ''}`
  if (!source.chat) return <span className="backlog-tag" title={full}>{label}</span>
  return <button type="button" className="backlog-tag" title={`${full}. Click to copy the chat ID.`} aria-label={`Added by ${source.agent}. Copy chat ID ${short}`}
    onClick={() => { void copyText(source.chat!).then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1500) }, () => {}) }}>{copied ? 'Copied' : label}</button>
}

interface RowActions {
  onDone: (raw: string, line: number, done: boolean) => void
  onDelete: (item: Item) => void
  onReword: (item: Item, wording: string) => void
  onDragStart: (event: ReactPointerEvent, item: Item) => void
  onDragKey: (event: KeyboardEvent, item: Item) => void
}

function ItemRow({ item, actions, locked, editing, setEditing, typed, drop, dragged }: { item: Item; actions: RowActions; locked: boolean; editing: boolean; setEditing: (id: string | null) => void; typed: RefObject<string>; drop?: 'before' | 'after'; dragged: boolean }) {
  const [wording, setWording] = useState(item.text)
  const ended = useRef(false)
  const finish = (keep: boolean) => {
    if (ended.current) return
    ended.current = true
    typed.current = ''
    setEditing(null)
    if (keep && wording.trim() && wording.trim() !== item.text) actions.onReword(item, wording)
  }
  return <li data-backlog-row={item.done ? undefined : item.id} className={['backlog-row', item.done && 'done', drop && `drop-${drop}`, dragged && 'dragged'].filter(Boolean).join(' ')}>
    {!item.done && <button type="button" className="backlog-grip" aria-label={`Move ${item.text}. Use the up and down arrow keys.`} title="Drag to reorder" disabled={locked}
      onPointerDown={event => actions.onDragStart(event, item)} onKeyDown={event => actions.onDragKey(event, item)}
      onTouchStart={event => event.stopPropagation()} onTouchEnd={event => event.stopPropagation()}><GripVertical size={15} aria-hidden="true" /></button>}
    <input type="checkbox" className="backlog-box" aria-label={item.text} checked={item.done} disabled={locked} onChange={event => actions.onDone(item.raw, item.line, event.target.checked)} />
    <div className="backlog-body">
      {editing
        ? <input aria-label={`Reword ${item.text}`} className="backlog-input backlog-reword" autoFocus value={wording} onFocus={event => { ended.current = false; event.target.select() }}
            onChange={event => { setWording(event.target.value); typed.current = event.target.value }} onBlur={() => finish(true)}
            onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); finish(true) } else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(false) } }} />
        : <span className="backlog-text-line" onDoubleClick={() => { if (!locked && !item.done) { setWording(item.text); setEditing(item.id) } }}>{item.text}</span>}
      {item.source && <SourceTag source={item.source} />}
      {item.details.length > 0 && <ul className="backlog-details">{item.details.map(detail => <li key={detail.line}>
        {detail.done === undefined ? <span>{detail.text}</span>
          : <label className={`backlog-sub${detail.done ? ' done' : ''}`}><input type="checkbox" checked={detail.done} disabled={locked} onChange={event => actions.onDone(detail.raw, detail.line, event.target.checked)} /><span>{detail.text}</span></label>}
      </li>)}</ul>}
    </div>
    {!editing && <span className="backlog-actions">
      {!item.done && <button type="button" className="icon-button" aria-label={`Reword ${item.text}`} title="Reword" disabled={locked} onClick={() => { setWording(item.text); setEditing(item.id) }}><Pencil size={15} /></button>}
      <button type="button" className="icon-button" aria-label={`Delete ${item.text}`} title="Delete" disabled={locked} onClick={() => actions.onDelete(item)}><Trash2 size={15} /></button>
    </span>}
  </li>
}

function NoteRow({ note }: { note: Note }) {
  return <li className="backlog-free">{note.text}</li>
}

interface Undo { label: string; change: Change }
/** What a close has to ask about: text that would otherwise be lost. */
export type BacklogGuard = RefObject<() => boolean>

export function Backlog({ api, guard, active = true }: { api: Api; guard?: BacklogGuard; active?: boolean }) {
  const [saved, setSaved] = useState<Saved | null>(null)
  const [changes, setChanges] = useState<Change[]>([])  // made in the checklist, not saved yet
  const [draft, setDraft] = useState<string | null>(null)  // the text while the person edits it as text
  const [item, setItem] = useState('')
  const [error, setError] = useState('')
  const [failed, setFailed] = useState('')  // saving failed for a reason that a retry can fix
  const [saving, setSaving] = useState(false)
  const [editingRow, setEditingRow] = useState<string | null>(null)
  const [showDone, setShowDone] = useState(false)
  const [conflict, setConflict] = useState<Saved | null>(null)  // the newer file, while the person chooses
  const [undo, setUndo] = useState<Undo | null>(null)
  const [drag, setDrag] = useState<{ item: Item; target: string | null; after: boolean } | null>(null)
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
  const change = useCallback((next: Change, offer?: Undo) => {
    if (!current.current) return false
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

  useEffect(() => {
    if (!undo) return
    const timer = window.setTimeout(() => setUndo(null), UNDO_MS)
    return () => window.clearTimeout(timer)
  }, [undo])

  const fresh = useRef({ item, conflict, failed, tooLong, dirty, changes: changes.length })
  useEffect(() => { fresh.current = { item, conflict, failed, tooLong, dirty, changes: changes.length } })
  useEffect(() => {
    if (!guard) return
    const check = guard as { current: () => boolean }
    check.current = () => { const now = fresh.current; return !!now.item.trim() || !!typedRow.current.trim() || !!now.conflict || !!now.tooLong || (!!now.failed && (now.changes > 0 || now.dirty)) }
  }, [guard])

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
    if (item.trim() && change(addItem(item))) { reveal.current = `- [ ] ${item.trim().replace(/\s*\n\s*/g, ' ')}\n`; setItem('') }
  }
  const rows = parseBacklog(shown)
  const done = rows.filter((row): row is Item => row.kind === 'item' && row.done)
  const groups = openGroups(rows)
  const hasHeadings = groups.some(group => group.heading)
  const openItems = groups.flatMap(group => group.rows.filter((row): row is Item => row.kind === 'item'))
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
    onDragStart: (event, target) => {
      if (event.button !== 0) return
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
      const moved = (at: PointerEvent) => {
        const box = list.current
        if (!box) return
        const edges = box.getBoundingClientRect()
        if (at.clientY < edges.top + 36) box.scrollTop -= 8
        else if (at.clientY > edges.bottom - 36) box.scrollTop += 8
        const places = [...box.querySelectorAll<HTMLElement>('[data-backlog-row]')]
        let found: { target: string; after: boolean } | null = null
        for (const place of places) {
          const area = place.getBoundingClientRect()
          if (at.clientY < area.top + area.height / 2) { found = { target: place.dataset.backlogRow!, after: false }; break }
        }
        if (!found && places.length) found = { target: places[places.length - 1].dataset.backlogRow!, after: true }
        setDrag({ item: target, target: found?.target ?? null, after: found?.after ?? false })
      }
      const finish = (ended: PointerEvent) => {
        window.removeEventListener('pointermove', moved)
        window.removeEventListener('pointerup', finish)
        window.removeEventListener('pointercancel', finish)
        setDrag(now => {
          if (now?.target && ended.type === 'pointerup' && now.target !== target.id) {
            const place = openItems.find(row => row.id === now.target)
            if (place) queueMicrotask(() => change(moveItem(target.raw, target.line, place.raw, place.line, now.after)))
          }
          return null
        })
      }
      setDrag({ item: target, target: null, after: false })
      window.addEventListener('pointermove', moved)
      window.addEventListener('pointerup', finish)
      window.addEventListener('pointercancel', finish)
    },
    onDragKey: (event, target) => {
      if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
      event.preventDefault()
      const at = openItems.findIndex(row => row.id === target.id)
      const up = event.key === 'ArrowUp'
      const next = openItems[at + (up ? -1 : 1)]
      if (!next) return
      refocus.current = target.id
      change(moveItem(target.raw, target.line, next.raw, next.line, !up))
    },
  }
  const showRow = (row: Item | Note) => row.kind === 'note'
    ? <NoteRow key={row.id} note={row} />
    : <ItemRow key={row.id} item={row} actions={actions} locked={locked} editing={editingRow === row.id} setEditing={setEditingRow} typed={typedRow} dragged={drag?.item.id === row.id}
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
    : `${openItems.length} open · Saved`

  return <div className="backlog">
    <div className="backlog-top">
      {inText
        ? <p className="backlog-mode">Editing as text. Items are Markdown lines like <code>- [ ] Item</code>.</p>
        : <form className="backlog-add" onSubmit={add}>
            <input ref={addBox} aria-label="New backlog item" className="backlog-input" type="text" value={item} disabled={locked} placeholder="Add an item" enterKeyHint="done" onChange={event => setItem(event.target.value)} />
            <button type="submit" className="secondary" disabled={locked || !item.trim()}>Add</button>
          </form>}
      <button type="button" className="icon-button" aria-pressed={inText} disabled={!saved || (!inText && changes.length > 0)} aria-label={inText ? 'Show checklist' : 'Edit as text'} title={inText ? 'Show checklist' : 'Edit as text'}
        onClick={() => { if (inText) void leaveText(); else { setUndo(null); setDraft(shown) } }}>{inText ? <ListChecks size={17} /> : <FileText size={16} />}</button>
    </div>
    {inText
      ? <textarea aria-label="Backlog text" className="backlog-text" value={draft} readOnly={!!conflict} spellCheck onChange={event => setDraft(event.target.value)} />
      : <div className="backlog-list" ref={list}>
          {saved && !rows.length && <div className="backlog-empty"><ClipboardList size={26} aria-hidden="true" /><p><strong>Nothing here yet</strong></p><p>Add what you want to remember. Agents add follow-ups they find, and fill in items you point them to.</p></div>}
          {saved && !!rows.length && !openItems.length && <p className="backlog-clear"><Check size={16} aria-hidden="true" />All done.</p>}
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
export function BacklogSheet({ api, onClose }: { api: Api; onClose: () => void }) {
  const guard = useRef<() => boolean>(() => false)
  const [asking, setAsking] = useState(false)
  const close = () => { if (guard.current()) setAsking(true); else onClose() }
  const modal = useModal(true, close)
  return <dialog {...modal} className="sheet settings-sheet backlog-sheet" aria-labelledby="backlog-title">
    <div className="sheet-heading">
      <h2 id="backlog-title">Backlog</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={close}><X /></button>
    </div>
    <p className="sheet-sub">One list for all your sessions</p>
    <Backlog api={api} guard={guard} />
    {asking && <div className="confirm-box backlog-leave" role="alertdialog" aria-labelledby="backlog-leave">
      <p id="backlog-leave"><strong>Some text is not saved.</strong> Close and lose it?</p>
      <div className="sheet-buttons">
        <button type="button" className="secondary" autoFocus onClick={() => setAsking(false)}>Keep editing</button>
        <button type="button" className="secondary danger-text" onClick={onClose}>Close anyway</button>
      </div>
    </div>}
  </dialog>
}

interface Count { text: string }
/** The sidebar's way in, with how many items are open. */
export function BacklogButton({ api, onOpen }: { api: Api; onOpen: () => void }) {
  const fetcher = useCallback((signal: AbortSignal) => api<Count>(PATH, undefined, signal), [api])
  const { data } = usePolling(fetcher, 30000)
  const open = data ? parseBacklog(data.text).filter(row => row.kind === 'item' && !row.done).length : 0
  return <button type="button" className="sidebar-backlog" aria-label={open ? `Backlog, ${open} open` : 'Backlog'} onClick={onOpen}>
    <ClipboardList size={16} aria-hidden="true" /><span>Backlog</span>{open > 0 && <span className="sidebar-backlog-count">{open}</span>}
  </button>
}
