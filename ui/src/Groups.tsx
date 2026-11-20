import { taskOf } from './taskTree'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, ChevronDown, FolderInput, FolderOutput, Pencil, Plus, ScrollText, Sparkles, Trash2, X } from 'lucide-react'
import { Spinner } from './Skeleton'
import type { Api, GroupInfo, Pane } from './api'
import { errorText } from './api'
import { anchorOf, useModal } from './hooks'
import { NameField } from './CreateFields'
import type { MainModel } from './prefs'
import type { Anchor } from './hooks'
import { useActionsDialog } from './Sheets'
import type { Session } from './Sidebar'
import { chatTitle, needsYou } from './Sidebar'
import { groupNameError, groupOf, placesOf, rememberTab, revealLeft, tabFor, topState, waitingElsewhere } from './groups'
import type { Place } from './groups'

// The stack row remounts with each tab, like the tab strip: keep its scroll place for each session.
const rowScroll = new Map<string, number>()

/** What the group row needs from the app, beyond what a chat already has. */
export interface GroupHooks {
  seen: Record<string, number>
  selected?: string
  /** Archived tabs of the session. The strip leaves them out, but they are still in their group. */
  hidden: Pane[]
  /** Start a new tab in a group ('' = Unstacked), next to `from`. */
  onStart: (group: string, from: Pane) => void
  /** Open the group's context in the project editor. */
  onContext: (group: GroupInfo) => void
  /** New stack: a name, a folder and an optional first task, beside `from` (NewTask.tsx). `at` = the popover anchor. */
  onNewStack: (from: Pane, at?: Anchor) => void
  /** The main AI model from Settings, for "Suggest stacks". */
  mainModel?: MainModel
}

const STATE_DOT = { needs: 'approval', done: 'done', working: 'working', idle: 'idle' } as const
const STATE_WORD = { needs: 'needs you', done: 'done', working: 'working', idle: '' } as const

/**
 * The group row above the tab strip (wide screens) and the group switcher left of it (phones).
 * It shows only when the session has two or more places (Unstacked while it holds a tab, and each group).
 * `children` is the tab strip; it shows as before when the row does not.
 */
export function GroupBar({ api, session, pane, hooks, onOpenPane, onUpdate, children }: {
  api: Api; session: Session; pane: Pane; hooks: GroupHooks; onOpenPane: (pane: Pane) => void; onUpdate: () => void; children?: React.ReactNode
}) {
  const groups = session.groups
  const current = groupOf(taskOf(pane, session.panes), groups)
  // Each group remembers its last tab on this device, so a switch back opens it again.
  useEffect(() => { if (groups?.length) rememberTab(session.id, current, pane.id) }, [groups?.length, session.id, current, pane.id])
  const [menu, setMenu] = useState<{ place: Place; at?: Anchor } | null>(null)
  const [picking, setPicking] = useState(false)
  // Drag a stack to reorder it, like a tab (a mouse drags at once; the stack menu has Move left and Move right too).
  // `order` is the stack order shown while a drag runs and until the saved order comes back.
  const [dragged, setDragged] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ ids: string[]; base: string } | null>(null)
  const [error, setError] = useState('')
  const saved = (groups ?? []).map(group => group.id).join()
  const order = preview?.base === saved ? preview.ids : null  // a saved or outside change of the order ends the preview
  const setOrder = (ids: string[] | null) => setPreview(ids && { ids, base: saved })
  const all = placesOf(session.panes, groups)
  // Before paint: restore the row's scroll, then show the open stack fully. A click on a stack in view keeps the row still.
  const row = useRef<HTMLElement>(null)
  const shown = all.length > 1
  useLayoutEffect(() => {
    const box = row.current
    if (!box) return
    box.scrollLeft = rowScroll.get(session.id) ?? 0
    const reveal = () => {
      const active = box.querySelector<HTMLElement>('[aria-current="true"]')
      if (!active) return
      const start = active.getBoundingClientRect().left - box.getBoundingClientRect().left + box.scrollLeft
      box.scrollLeft = revealLeft(box.scrollLeft, box.clientWidth, box.scrollWidth, start, active.offsetWidth, 24)
    }
    reveal()
    rowScroll.set(session.id, box.scrollLeft)
    const save = () => rowScroll.set(session.id, box.scrollLeft)
    box.addEventListener('scroll', save, { passive: true })
    // A narrower window or panel must not push the open stack out of view.
    let width = box.clientWidth
    const observer = new ResizeObserver(() => { if (box.clientWidth !== width) { width = box.clientWidth; reveal() } })
    observer.observe(box)
    return () => { box.removeEventListener('scroll', save); observer.disconnect() }
  }, [session.id, current, shown])
  if (!shown) return <>{children}</>
  const places = order ? [...all.filter(place => !place.id), ...order.flatMap(id => all.filter(place => place.id === id))] : all
  const over = (event: React.DragEvent<HTMLButtonElement>, target: Place) => {
    if (!dragged || !target.id) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
    if (target.id === dragged) return
    const box = event.currentTarget.getBoundingClientRect()
    const ids = places.filter(place => place.id && place.id !== dragged).map(place => place.id)
    const at = ids.indexOf(target.id) + (event.clientX > box.left + box.width / 2 ? 1 : 0)
    const next = [...ids.slice(0, at), dragged, ...ids.slice(at)]
    if (next.join() !== places.filter(place => place.id).map(place => place.id).join()) setOrder(next)
  }
  const drop = () => {
    const ids = order
    setDragged(null)
    if (!ids || ids.join() === saved) { setOrder(null); return }
    setError('')
    api('groups/reorder', { session: session.id, groups: ids }).then(onUpdate, failure => { setOrder(null); setError(errorText(failure)) })
  }

  const here = places.find(place => place.id === current) ?? places[0]
  const go = (place: Place, at?: Anchor) => {
    const target = tabFor(session.id, place)
    if (target) { if (target.id !== pane.id) onOpenPane(target) }
    else setMenu({ place, at })  // an empty group: its menu offers "Start a tab here"
  }
  const waiting = waitingElsewhere(places, current)
  const movable = places.filter(place => place.id).length > 1  // two named stacks can swap, with or without Unstacked
  return <>
    <nav ref={row} className="group-row" aria-label={`Stacks of ${session.name}`}>
      {places.map(place => {
        const state = topState(place.panes, hooks.seen, hooks.selected)
        const word = STATE_WORD[state]
        return <button key={place.id || 'main'} type="button" data-group-drop={place.id} aria-current={place.id === here.id ? 'true' : undefined}
          className={['group-tab', place.id === here.id ? 'active' : '', dragged === place.id ? 'dragging' : ''].filter(Boolean).join(' ')}
          draggable={!!place.id && movable}
          onDragStart={event => { if (!place.id) return; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', place.name); setDragged(place.id) }}
          onDragOver={event => over(event, place)} onDrop={event => { if (dragged) { event.preventDefault(); drop() } }}
          onDragEnd={() => { if (dragged) { setDragged(null); setOrder(null) } }}
          aria-label={`${place.name}, ${place.panes.length} ${place.panes.length === 1 ? 'tab' : 'tabs'}${word ? `, ${word}` : ''}`}
          title={place.id ? `${place.name}. Drag to reorder. Right-click for stack actions.` : 'Tabs that are in no stack'}
          onClick={event => go(place, anchorOf(event))}
          onContextMenu={event => { if (!place.id) return; event.preventDefault(); setMenu({ place, at: anchorOf(event) }) }}>
          <span className={`state-dot ${STATE_DOT[state]}`} aria-hidden="true" />
          <span className="group-name">{place.name}</span>
          <span className="group-count" aria-hidden="true">{place.panes.length}</span>
        </button>
      })}
      {error && <p className="error group-row-error" role="alert">{error}</p>}
      <button type="button" className="group-add" aria-label="New stack" title="New stack" aria-haspopup="dialog" onClick={event => { const box = event.currentTarget.getBoundingClientRect(); hooks.onNewStack(pane, { x: box.right - 392, y: box.bottom + 6 }) }}><Plus size={16} /></button>
    </nav>
    <div className="group-strip">
      <button type="button" className="group-switch" aria-haspopup="dialog" onClick={() => setPicking(true)}
        aria-label={`Stack: ${here.name}. Change stack${waiting ? `. ${waiting} ${waiting === 1 ? 'tab needs' : 'tabs need'} you in other stacks` : ''}`}>
        <span className="group-name">{here.name}</span>
        {waiting > 0 && <span className="group-badge" aria-hidden="true">{waiting}</span>}
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {children}
    </div>
    {picking && <GroupSwitcher api={api} onUpdate={onUpdate} session={session} places={places} current={here.id} hooks={hooks}
      onPick={place => { setPicking(false); go(place) }} onMenu={place => { setPicking(false); setMenu({ place }) }}
      onNew={() => { setPicking(false); hooks.onNewStack(pane) }} onClose={() => setPicking(false)} />}
    {menu && <GroupMenu api={api} session={session} place={menu.place} order={places.filter(place => place.id).map(place => place.id)} at={menu.at} from={pane} hooks={hooks} onClose={() => setMenu(null)} onDone={() => { setMenu(null); onUpdate() }} />}
  </>
}

/** The phone sheet: every place with its state, then New stack. */
function GroupSwitcher({ api, onUpdate, session, places, current, hooks, onPick, onMenu, onNew, onClose }: {
  api: Api; onUpdate: () => void; session: Session; places: Place[]; current: string; hooks: GroupHooks; onPick: (place: Place) => void; onMenu: (place: Place) => void; onNew: () => void; onClose: () => void
}) {
  const modal = useModal(true, onClose)
  const [suggesting, setSuggesting] = useState(false)
  return <dialog {...modal} className="sheet" aria-labelledby="group-switch-title">
    <div className="sheet-heading"><h2 id="group-switch-title">Stacks</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="sheet-sub">{session.name}</p>
    {suggesting
      ? <SuggestStacks api={api} session={session} mainModel={hooks.mainModel} onBack={() => setSuggesting(false)} onDone={() => { onClose(); onUpdate() }} />
      : <div className="action-list group-list">
      {places.map(place => {
        const state = topState(place.panes, hooks.seen, hooks.selected)
        const waiting = place.panes.filter(item => needsYou(item.state)).length
        return <div key={place.id || 'main'} className="group-list-row">
          <button type="button" aria-current={place.id === current ? 'true' : undefined} onClick={() => onPick(place)}>
            <span className={`state-dot ${STATE_DOT[state]}`} aria-hidden="true" />
            <span className="group-name">{place.name}</span>
            <small>{waiting ? `${waiting} ${waiting === 1 ? 'needs' : 'need'} you` : `${place.panes.length} ${place.panes.length === 1 ? 'tab' : 'tabs'}`}</small>
          </button>
          {place.id && <button type="button" className="icon-button" aria-label={`Actions for ${place.name}`} onClick={() => onMenu(place)}><Pencil size={16} /></button>}
        </div>
      })}
      <hr />
      <button type="button" onClick={onNew}><Plus size={18} />New stack</button>
      {session.panes.length > 1 && <button type="button" onClick={() => setSuggesting(true)}><Sparkles size={18} />Suggest stacks…</button>}
    </div>}
  </dialog>
}

/** The menu of one group: start a tab, rename, context, ungroup, close all tabs, remove. */
function GroupMenu({ api, session, place, order, at, from, hooks, onClose, onDone }: {
  api: Api; session: Session; place: Place; order: string[]; at?: Anchor; from: Pane; hooks: GroupHooks; onClose: () => void; onDone: () => void
}) {
  const group = session.groups?.find(item => item.id === place.id)
  const [mode, setMode] = useState<'menu' | 'rename' | 'close'>('menu')
  const [name, setName] = useState(place.name)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useActionsDialog(at, () => { if (!pending) onClose() })
  if (!group) return null
  // Archived tabs are hidden from the strip but still in the group: close all and remove must count them.
  const archived = hooks.hidden.filter(item => item.group === group.id)
  const all = [...place.panes, ...archived]
  const count = all.length
  const tabs = `${count} ${count === 1 ? 'tab' : 'tabs'}`
  async function run(action: () => Promise<unknown>) {
    setPending(true); setError('')
    try { await action(); onDone() } catch (failure) { setError(errorText(failure)); setPending(false) }
  }
  const nameError = mode === 'rename' ? groupNameError(name, session.groups, group.id) : ''
  // Move left/right (up/down in the phone sheet): the one-tap way to reorder, beside dragging (WCAG 2.5.7).
  // `order` is the order the stack row shows, which can run ahead of the saved order just after a drag.
  const ids = order
  const index = ids.indexOf(group.id)
  const shift = (by: -1 | 1) => {
    const next = [...ids]
    next.splice(index, 1)
    next.splice(index + by, 0, group.id)
    void run(() => api('groups/reorder', { session: session.id, groups: next }))
  }
  // Agents that a close would stop in the middle of a turn, or that wait on you.
  const busy = all.filter(item => item.state === 'Working' || needsYou(item.state))
  const endsSession = count > 0 && count === session.panes.length + hooks.hidden.length
  async function closeAll() {
    // Closing the whole stack already confirms all its tabs. Close helpers before their tasks.
    const closing = [...all].sort((a, b) => (b.task_depth ?? 0) - (a.task_depth ?? 0))
    for (const item of closing) await api('session', { pane: item.id, thread: item.thread }, undefined, 'DELETE')
    if (!endsSession) await api('groups/remove', { session: session.id, group: group!.id })
  }
  return <dialog {...dialog} aria-labelledby="group-menu-title">
    <div className="sheet-heading"><h2 id="group-menu-title">{group.name}</h2>{!at && <button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button>}</div>
    <p className="sheet-sub">Stack in {session.name} · {count ? tabs : 'no tabs'}</p>
    {mode === 'rename'
      ? <form className="rename-form" onSubmit={event => { event.preventDefault(); if (!nameError) void run(() => api('groups/rename', { session: session.id, group: group.id, name: name.trim() })) }}>
          <label htmlFor="group-name-input">Stack name</label>
          <input id="group-name-input" autoFocus value={name} maxLength={40} disabled={pending} autoComplete="off" aria-invalid={!!nameError && name !== place.name} onChange={event => setName(event.target.value)} />
          {nameError && name !== place.name && <p className="hint">{nameError}</p>}
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setMode('menu')}>Cancel</button><button className="primary" disabled={pending || !!nameError}>{pending ? 'Saving…' : 'Save'}</button></div>
        </form>
      : mode === 'close'
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="group-close-title">
          <p id="group-close-title"><strong>{count === 1 ? 'Close the tab' : `Close all ${tabs}`} in {group.name}?</strong> {archived.length ? `${archived.length === 1 ? '1 of them is' : `${archived.length} of them are`} archived. ` : ''}{busy.length ? `This stops ${busy.map(item => `${chatTitle(item)} (${needsYou(item.state) ? 'needs you' : 'working'})`).join(', ')}. ` : ''}{endsSession ? `${count === 1 ? 'It is the last tab' : 'They are the last tabs'}, so this also ends the session ${session.name}. ` : 'Then the empty stack is removed. '}You cannot undo this.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setMode('menu')}>Cancel</button><button type="button" className="danger" disabled={pending} onClick={() => void run(closeAll)}>{pending ? 'Closing…' : `Close ${tabs}`}</button></div>
        </div>
      : <div className="action-list">
          <button disabled={pending} onClick={() => { onClose(); hooks.onStart(group.id, from) }}><Plus size={18} />New tab here</button>
          <button disabled={pending} onClick={() => setMode('rename')}><Pencil size={18} />Rename stack</button>
          {ids.length > 1 && <>
            <button disabled={pending || index <= 0} onClick={() => shift(-1)}>{at ? <ArrowLeft size={18} /> : <ArrowUp size={18} />}{at ? 'Move left' : 'Move up'}</button>
            <button disabled={pending || index >= ids.length - 1} onClick={() => shift(1)}>{at ? <ArrowRight size={18} /> : <ArrowDown size={18} />}{at ? 'Move right' : 'Move down'}</button>
          </>}
          <button disabled={pending} onClick={() => { onClose(); hooks.onContext(group) }}><ScrollText size={18} />Stack context</button>
          <hr />
          {count > 0 && <button disabled={pending} title={`Move ${tabs} to Unstacked. No agent stops.`} onClick={() => void run(() => api('groups/ungroup', { session: session.id, group: group.id }))}><FolderOutput size={18} />Unstack<small>{tabs} to Unstacked</small></button>}
          {count > 0
            ? <button className="danger-item" disabled={pending} onClick={() => setMode('close')}><Trash2 size={18} />Close all tabs…</button>
            : <button className="danger-item" disabled={pending} onClick={() => void run(() => api('groups/remove', { session: session.id, group: group.id }))}><Trash2 size={18} />Remove stack</button>}
        </div>}
    {mode === 'menu' && <p className="hint">{count ? 'Unstack moves the tabs to Unstacked and stops no agent. ' : ''}The stack context is kept in the project archive.</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>
}

/** Name a new stack that `pane` moves into (the tab menu's "New stack with this tab"). */
/** "New stack with this tab" (or with the selected tabs): make the stack with the first tab, then move the others in. */
export function NewGroup({ api, session, panes, mainModel, onClose, onDone }: { api: Api; session: Session; panes: Pane[]; mainModel?: MainModel; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [tried, setTried] = useState(false)
  // After a failed move, the stack exists and some tabs moved: a retry moves only the rest.
  const [made, setMade] = useState<{ group: string; moved: string[] } | null>(null)
  const problem = made ? undefined : groupNameError(name, session.groups)
  async function create(event: React.FormEvent) {
    event.preventDefault(); setTried(true)
    if (problem) return
    setPending(true); setError('')
    let state = made
    try {
      if (!state) {
        const created = await api<{ group: GroupInfo }>('groups/create', { session: session.id, name: name.trim(), pane: panes[0].id })
        state = { group: created.group.id, moved: [panes[0].id] }
      }
      for (const item of panes) {
        if (state.moved.includes(item.id)) continue
        await api('groups/move', { session: session.id, pane: item.id, group: state.group })
        state = { ...state, moved: [...state.moved, item.id] }
      }
      onDone()
    } catch (failure) { setMade(state); setError(errorText(failure)); setPending(false) }
  }
  return <form className="rename-form new-group" onSubmit={create} noValidate>
    <NameField api={api} kind="stack" label="Stack name" value={name} disabled={pending || !!made} autoFocus mainModel={mainModel} problem={tried ? problem : undefined}
      onChange={value => { setName(value); setError('') }}
      context={() => ({ tabs: panes.map(item => chatTitle(item, session)), folder: panes[0].dir, taken: ['Unstacked', 'Main', ...(session.groups ?? []).map(group => group.name)] })} />
    <p className="hint">{panes.length === 1 ? `${chatTitle(panes[0], session)} moves` : `${panes.length} tabs move`} into it. A stack has its own tabs and its own agent context.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={onClose}>Back</button><button className="primary" disabled={pending}>{pending ? 'Making…' : made ? 'Move the rest' : 'Make stack'}</button></div>
  </form>
}

interface StackSuggestion { groups: { name: string; sessions: string[] }[]; unstacked?: string[]; source: 'claude' | 'codex' | 'llm' | 'folder' }

/**
 * "Suggest stacks": ask the main AI model (then the local model, then folders) to sort the session's tabs into stacks.
 * Nothing moves until Apply. Apply reuses a stack with the same name, and tabs left out stay where they are.
 */
export function SuggestStacks({ api, session, mainModel, onBack, onDone }: { api: Api; session: Session; mainModel?: MainModel; onBack: () => void; onDone: (count: number) => void }) {
  const [asking, setAsking] = useState(false)
  const [applying, setApplying] = useState(false)
  const [suggestion, setSuggestion] = useState<StackSuggestion | null>(null)
  const [error, setError] = useState('')
  const titles = new Map(session.panes.map(item => [item.id, chatTitle(item, session)]))
  // Tabs the suggestion leaves in no stack. Only the ones in a stack now move.
  const loose = (suggestion?.unstacked ?? []).filter(id => { const item = session.panes.find(other => other.id === id); return item && groupOf(item, session.groups) })
  const changes = !!suggestion && (suggestion.groups.length > 0 || loose.length > 0)
  async function suggest() {
    setAsking(true); setError(''); setSuggestion(null)
    try {
      const tabs = session.panes.slice(0, 200).map(item => ({ id: item.id, name: chatTitle(item, session), dir: item.dir ?? '', chats: item.agent ? [item.agent] : [] }))
      const body = { kind: 'tabs', tabs, ...(mainModel && mainModel !== 'none' ? { agent: mainModel } : {}) }
      // The server tries the main AI model (up to 45 s), then the local model (up to 90 s), then folders.
      setSuggestion(await api<StackSuggestion>('suggest-groups', body, undefined, undefined, 150000))
    } catch (failure) { setError(errorText(failure)) } finally { setAsking(false) }
  }
  async function apply(found: StackSuggestion) {
    setApplying(true); setError('')
    try {
      const made = await api<{ groups: GroupInfo[] }>('groups/arrange', { session: session.id, stacks: found.groups.map(group => ({ name: group.name, panes: group.sessions })), unstack: loose })
      onDone(made.groups.length)
    } catch (failure) { setError(errorText(failure)); setApplying(false) }
  }
  const source = suggestion?.source === 'claude' ? 'Suggested by Claude.' : suggestion?.source === 'codex' ? 'Suggested by Codex.' : suggestion?.source === 'llm' ? 'Suggested by the local model.' : 'The model gave no useful stacks. These stacks follow the folders.'
  return <section className="suggest-box suggest-stacks" aria-live="polite" aria-busy={asking || undefined}>
    {!suggestion && <>
      <p className="suggest-note">Sort the {session.panes.length} tabs of {session.name} into stacks by task. Nothing moves until you apply.</p>
      <button type="button" className="secondary suggest-button" disabled={asking} onClick={() => void suggest()}>{asking ? <Spinner size={16} /> : <Sparkles size={16} />}{asking ? 'Suggesting stacks… about 15 seconds' : 'Suggest stacks'}</button>
    </>}
    {suggestion && (changes
      ? <>
          <p className="suggest-note">{source} Apply moves these tabs, with their reviewers and advisors. A stack with the same name is reused. A stack left with no tabs is removed.</p>
          <ul className="suggest-list">
            {suggestion.groups.map(group => <li key={group.name}><strong>{group.name}</strong><span>{group.sessions.map(id => titles.get(id) ?? id).join(', ')}</span></li>)}
            {loose.length > 0 && <li><strong>Unstacked</strong><span>{loose.map(id => titles.get(id) ?? id).join(', ')}</span></li>}
          </ul>
        </>
      : <p className="suggest-note">No stacks to suggest. A stack needs 2 or more tabs that share one goal.</p>)}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" disabled={applying} onClick={suggestion ? () => setSuggestion(null) : onBack}>{suggestion ? 'Discard' : 'Back'}</button>
      {suggestion && changes && <button type="button" className="primary" disabled={applying} onClick={() => void apply(suggestion)}>{applying ? 'Applying…' : 'Apply stacks'}</button>}
    </div>
  </section>
}

/** The tab menu's "Move to stack…" (or Group for the selected tabs): Unstacked, each group, or a new group with these tabs. */
export function TabGroupPicker({ api, session, panes, pending, run, onNew, onBack }: {
  api: Api; session: Session; panes: Pane[]; pending: boolean; run: (action: () => Promise<string | undefined>) => Promise<void>; onNew: () => void; onBack: () => void
}) {
  const places: { id: string; name: string }[] = [{ id: '', name: 'Unstacked' }, ...(session.groups ?? [])]
  const shared = new Set(panes.map(item => groupOf(item, session.groups)))
  const current = shared.size === 1 ? [...shared][0] : undefined  // the place when all the tabs are in one
  // One move per tab with the single-tab route, so each move can be undone; a tab already there stays.
  const move = (group: string) => void run(async () => {
    for (const item of panes) if (groupOf(item, session.groups) !== group) await api('groups/move', { session: session.id, pane: item.id, group: group || null })
    return undefined
  })
  return <div className="action-list">
    {places.map(place => <button key={place.id || 'main'} type="button" disabled={pending || place.id === current} aria-current={place.id === current ? 'true' : undefined} onClick={() => move(place.id)}>
      <FolderInput size={18} />{place.name}{place.id === current && <small>Here now</small>}
    </button>)}
    <hr />
    <button type="button" disabled={pending} onClick={onNew}><Plus size={18} />{panes.length === 1 ? 'New stack with this tab' : `New stack with these ${panes.length} tabs`}</button>
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={onBack}>Back</button></div>
  </div>
}
