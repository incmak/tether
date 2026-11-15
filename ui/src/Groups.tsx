import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown, FolderInput, FolderOutput, Pencil, Plus, ScrollText, Trash2, X } from 'lucide-react'
import type { Api, GroupInfo, Pane } from './api'
import { errorText } from './api'
import { anchorOf, useModal } from './hooks'
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
  const current = groupOf(pane, groups)
  // Each group remembers its last tab on this device, so a switch back opens it again.
  useEffect(() => { if (groups?.length) rememberTab(session.id, current, pane.id) }, [groups?.length, session.id, current, pane.id])
  const [menu, setMenu] = useState<{ place: Place; at?: Anchor } | null>(null)
  const [creating, setCreating] = useState(false)
  const [picking, setPicking] = useState(false)
  const places = placesOf(session.panes, groups)
  // Before paint: restore the row's scroll, then show the open stack fully. A click on a stack in view keeps the row still.
  const row = useRef<HTMLElement>(null)
  const shown = places.length > 1
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
  if (!shown) return <>{children}{creating && <NewGroup api={api} session={session} onClose={() => setCreating(false)} onDone={() => { setCreating(false); onUpdate() }} />}</>

  const here = places.find(place => place.id === current) ?? places[0]
  const go = (place: Place, at?: Anchor) => {
    const target = tabFor(session.id, place)
    if (target) { if (target.id !== pane.id) onOpenPane(target) }
    else setMenu({ place, at })  // an empty group: its menu offers "Start a tab here"
  }
  const waiting = waitingElsewhere(places, current)
  return <>
    <nav ref={row} className="group-row" aria-label={`Stacks of ${session.name}`}>
      {places.map(place => {
        const state = topState(place.panes, hooks.seen, hooks.selected)
        const word = STATE_WORD[state]
        return <button key={place.id || 'main'} type="button" data-group-drop={place.id} aria-current={place.id === here.id ? 'true' : undefined}
          className={place.id === here.id ? 'group-tab active' : 'group-tab'}
          aria-label={`${place.name}, ${place.panes.length} ${place.panes.length === 1 ? 'tab' : 'tabs'}${word ? `, ${word}` : ''}`}
          title={place.id ? `${place.name}. Right-click for stack actions.` : 'Tabs that are in no stack'}
          onClick={event => go(place, anchorOf(event))}
          onContextMenu={event => { if (!place.id) return; event.preventDefault(); setMenu({ place, at: anchorOf(event) }) }}>
          <span className={`state-dot ${STATE_DOT[state]}`} aria-hidden="true" />
          <span className="group-name">{place.name}</span>
          <span className="group-count" aria-hidden="true">{place.panes.length}</span>
        </button>
      })}
      <button type="button" className="group-add" aria-label="New stack" title="New stack" onClick={() => setCreating(true)}><Plus size={16} /></button>
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
    {picking && <GroupSwitcher session={session} places={places} current={here.id} hooks={hooks}
      onPick={place => { setPicking(false); go(place) }} onMenu={place => { setPicking(false); setMenu({ place }) }}
      onNew={() => { setPicking(false); setCreating(true) }} onClose={() => setPicking(false)} />}
    {menu && <GroupMenu api={api} session={session} place={menu.place} at={menu.at} from={pane} hooks={hooks} onClose={() => setMenu(null)} onDone={() => { setMenu(null); onUpdate() }} />}
    {creating && <NewGroup api={api} session={session} onClose={() => setCreating(false)} onDone={() => { setCreating(false); onUpdate() }} />}
  </>
}

/** The phone sheet: every place with its state, then New stack. */
function GroupSwitcher({ session, places, current, hooks, onPick, onMenu, onNew, onClose }: {
  session: Session; places: Place[]; current: string; hooks: GroupHooks; onPick: (place: Place) => void; onMenu: (place: Place) => void; onNew: () => void; onClose: () => void
}) {
  const modal = useModal(true, onClose)
  return <dialog {...modal} className="sheet" aria-labelledby="group-switch-title">
    <div className="sheet-heading"><h2 id="group-switch-title">Stacks</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="sheet-sub">{session.name}</p>
    <div className="action-list group-list">
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
    </div>
  </dialog>
}

/** The menu of one group: start a tab, rename, context, ungroup, close all tabs, remove. */
function GroupMenu({ api, session, place, at, from, hooks, onClose, onDone }: {
  api: Api; session: Session; place: Place; at?: Anchor; from: Pane; hooks: GroupHooks; onClose: () => void; onDone: () => void
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
  // Agents that a close would stop in the middle of a turn, or that wait on you.
  const busy = all.filter(item => item.state === 'Working' || needsYou(item.state))
  const endsSession = count > 0 && count === session.panes.length + hooks.hidden.length
  async function closeAll() {
    // One close at a time with the existing close route, then remove the empty group (the session ends with its last tab).
    for (const item of all) await api('session', { pane: item.id }, undefined, 'DELETE')
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
          {!count && <button disabled={pending} onClick={() => { onClose(); hooks.onStart(group.id, from) }}><Plus size={18} />Start a tab here</button>}
          <button disabled={pending} onClick={() => setMode('rename')}><Pencil size={18} />Rename stack</button>
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

/** Name a new group; with `pane`, that tab moves into it. */
export function NewGroup({ api, session, pane, onClose, onDone }: { api: Api; session: Session; pane?: Pane; onClose: () => void; onDone: () => void }) {
  const modal = useModal(true, () => { if (!pending) onClose() })
  const [name, setName] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [tried, setTried] = useState(false)
  const problem = groupNameError(name, session.groups)
  async function create(event: React.FormEvent) {
    event.preventDefault(); setTried(true)
    if (problem) return
    setPending(true); setError('')
    try { await api('groups/create', { session: session.id, name: name.trim(), ...(pane ? { pane: pane.id } : {}) }); onDone() }
    catch (failure) { setError(errorText(failure)); setPending(false) }
  }
  return <dialog {...modal} className="sheet" aria-labelledby="new-group-title">
    <form onSubmit={create}>
      <div className="sheet-heading"><h2 id="new-group-title">New stack</h2><button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button></div>
      <p className="sheet-sub">{pane ? `${chatTitle(pane, session)} moves into it` : `In ${session.name}`}</p>
      <label htmlFor="new-group-name">Stack name</label>
      <input id="new-group-name" autoFocus value={name} maxLength={40} disabled={pending} autoComplete="off" aria-invalid={tried && !!problem} onChange={event => setName(event.target.value)} />
      <p className="hint">{tried && problem ? problem : 'A stack has its own tabs and its own agent context.'}</p>
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary create-button" disabled={pending}>{pending ? 'Making…' : 'Make stack'}</button>
    </form>
  </dialog>
}

/** The tab menu's "Move to stack…": Unstacked, each group, or a new group with this tab. */
export function TabGroupPicker({ api, session, pane, pending, run, onNew, onBack }: {
  api: Api; session: Session; pane: Pane; pending: boolean; run: (action: () => Promise<string | undefined>) => Promise<void>; onNew: () => void; onBack: () => void
}) {
  const current = groupOf(pane, session.groups)
  const places: { id: string; name: string }[] = [{ id: '', name: 'Unstacked' }, ...(session.groups ?? [])]
  const move = (group: string) => void run(async () => { await api('groups/move', { session: session.id, pane: pane.id, group: group || null }); return undefined })
  return <div className="action-list">
    {places.map(place => <button key={place.id || 'main'} type="button" disabled={pending || place.id === current} aria-current={place.id === current ? 'true' : undefined} onClick={() => move(place.id)}>
      <FolderInput size={18} />{place.name}{place.id === current && <small>Here now</small>}
    </button>)}
    <hr />
    <button type="button" disabled={pending} onClick={onNew}><Plus size={18} />New stack with this tab</button>
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={onBack}>Back</button></div>
  </div>
}
