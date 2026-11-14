import { Bone, Loading, Skeleton } from './Skeleton'
import { useWords, count } from './words'
import { useCallback, useEffect, useRef, useState } from 'react'
import { FolderInput, Eye, Archive, ArchiveRestore, ArrowDown, BookMarked, ArrowUp, Bell, Check, Columns2, GitFork, LayoutDashboard, Merge, ArrowRightLeft, Plus, PanelBottom, PanelRight, Pencil, Pin, PinOff, Shapes, Sparkles, RotateCw, Rows2, Square, Trash2, X } from 'lucide-react'
import { ColorRow, GroupPicker } from './Organize'
import type { SessionGroup, TagColor } from './Organize'
import { ReviewLinkPicker } from './ReviewLinks'
import { SelectMenu } from './SelectMenu'
import { SESSION_NAME, SESSION_NAME_HELP, canFork, creditsLabel, errorText, usageLabel } from './api'
import type { Api, Catalog, Footer, ModelOptions, Pane, PastChat } from './api'
import { loadCatalog } from './Suggest'
import { chatSlug, resumeChat } from './ChatPicker'
import { useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, PANE_ICONS, agentNames, paneTitle, shownIndex } from './Sidebar'
import type { Session } from './Sidebar'
import { KeepChoice, keepNeeded } from './Project'
import type { Keep, ProjectMarks } from './Project'
import { GlobalCodexLevel } from './CodexLevel'
import { CopyNames } from './CopyNames'
import { NewGroup, TabGroupPicker } from './Groups'
import { placeName } from './groups'

type PaneLevel = 'all' | 'important' | 'urgent' | 'off'
type Delivery = 'sound' | 'silent' | 'off'
type Priority = 'urgent' | 'important' | 'low'
interface PushConfig { supported: boolean; publicKey: string; muted?: string[]; levels?: Record<string, PaneLevel>; delivery?: Record<Priority, Delivery> }
const PANE_LEVELS: [PaneLevel, string][] = [['all', 'All'], ['important', 'Important and urgent'], ['urgent', 'Urgent only'], ['off', 'Off']]

// Pane notification submenu. Hidden when this browser has no push subscription.
export function MuteItem({ api, pane, disabled = false }: { api: Api; pane: string; disabled?: boolean }) {
  const [level, setLevel] = useState<PaneLevel | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return
    let live = true
    Promise.all([navigator.serviceWorker.ready.then(registration => registration.pushManager.getSubscription()), api<PushConfig>('push/config')])
      .then(([subscription, config]) => { if (live) setLevel(subscription ? config.levels?.[pane] ?? ((config.muted ?? []).includes(pane) ? 'off' : 'all') : null) }, () => {})
    return () => { live = false }
  }, [api, pane])
  if (level === null) return null
  async function choose(next: PaneLevel) {
    setBusy(true); setError('')
    try { const result = await api<{ levels: Record<string, PaneLevel> }>('push/level', { session: pane, level: next }); setLevel(result.levels[pane] ?? 'all') }
    catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  return <>
    <button type="button" aria-expanded={open} disabled={disabled || busy} onClick={() => setOpen(!open)}><Bell size={16} />Notifications</button>
    {open && <div className="notification-levels" role="group" aria-label="Pane notifications">
      {PANE_LEVELS.map(([value, label]) => <button type="button" key={value} aria-pressed={level === value} disabled={disabled || busy} onClick={() => void choose(value)}><span>{label}</span>{level === value && <Check size={16} />}</button>)}
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </>
}


export function Usage({ footer, onClose }: { footer: Footer; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const context = footer.context
  const credits = creditsLabel(footer.credits)
  return <dialog {...modal} className="sheet" aria-labelledby="usage-title">
    <div className="sheet-heading"><h2 id="usage-title">Usage</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    {footer.model && <p className="sheet-sub">{footer.model}{footer.effort && ` · ${footer.effort}`}</p>}
    <div className="usage-detail">
      {context && <div className="usage-row"><div><strong>Context</strong><span>{context.percent.toFixed(1)}% used · {(100 - context.percent).toFixed(1)}% left</span></div>
        {context.tokens != null && context.limit != null && <small>{context.tokens.toLocaleString()} / {context.limit.toLocaleString()} tokens</small>}</div>}
      {footer.windows?.map(window => <div className="usage-row" key={window.label}><div><strong>{usageLabel(window.label)}</strong><span>{window.percent}% used</span></div>
        <div className="meter-bar" role="meter" aria-label={`${usageLabel(window.label)} used`} aria-valuenow={window.percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${Math.max(0, Math.min(100, window.percent))}%` }} /></div>
        {window.resets_at != null && <small>Resets {new Date(window.resets_at * 1000).toLocaleString()}</small>}</div>)}
      {credits && <dl className="info-list"><div><dt>Credits</dt><dd>{credits}</dd></div></dl>}
    </div>
  </dialog>
}

/** Desktop workspace moves for a pane or session: each one is there only when it fits the current layout. */
export interface Place { here?: () => void; right?: () => void; below?: () => void; all?: () => void }

function PlaceItems({ place, pending, onClose }: { place: Place | undefined; pending: boolean; onClose: () => void }) {
  if (!place || !(place.here || place.right || place.below || place.all)) return null
  const item = (run: (() => void) | undefined, icon: React.ReactNode, label: string) => run && <button disabled={pending} onClick={() => { run(); onClose() }}>{icon}{label}</button>
  return <>
    <p className="action-label">Show in this window</p>
    {item(place.all, <LayoutDashboard size={18} />, 'Show all panes')}
    {item(place.here, <Square size={18} />, 'Show in the focused slot')}
    {item(place.right, <PanelRight size={18} />, 'Open to the right')}
    {item(place.below, <PanelBottom size={18} />, 'Open below')}
    <hr />
    <p className="action-label">tmux</p>
  </>
}

/** The keyboard path for drag to merge: a list of the other sessions to choose from. */
function SessionPicker({ label, targets, pending, extra, onPick, onCancel }: { label: string; targets: Session[]; pending: boolean; extra?: React.ReactNode; onPick: (target: Session) => void; onCancel: () => void }) {
  return <div className="action-list" role="group" aria-label={label}>
    <p className="action-label">{label}</p>
    {targets.map((target, at) => <button key={target.id} autoFocus={at === 0} disabled={pending} onClick={() => onPick(target)}>{target.name}<small>{target.panes.length === 1 ? '1 pane' : `${target.panes.length} panes`}</small></button>)}
    {!targets.length && !extra && <p className="hint">There is no other session.</p>}
    {extra}
    <hr />
    <button disabled={pending} onClick={onCancel}>Cancel</button>
  </div>
}

/** A sheet on the phone; a popover at `at` on the desktop. The popover closes on Esc or a click outside it. */
export function useActionsDialog(at: Anchor | undefined, close: () => void) {
  const modal = useModal(true, close)
  const popover = usePopover(modal.ref, at, close)
  return { ...modal, ...popover, className: at ? 'sheet popover' : 'sheet' }
}

export function SessionActions({ api, session, tabCount, pane, pinned, archived, canMoveUp, canMoveDown, startDelete = false, at, place, targets, marks, onMarks, onPin, onArchive, onMove, onProject, onDone, onClose, color, onColor, groups = [], group, onGroup }: {
  color?: string; onColor?: (color: TagColor | null) => void; groups?: SessionGroup[]; group?: string; onGroup?: (group: SessionGroup | null, created?: SessionGroup) => void
  api: Api; session: Session; tabCount: number; pane: string; pinned: boolean; archived: boolean; canMoveUp: boolean; canMoveDown: boolean; startDelete?: boolean; at?: Anchor; place?: Place; targets: Session[]; marks?: ProjectMarks; onMarks?: () => void
  onPin: () => void; onArchive: () => void; onMove: (step: -1 | 1) => void; onProject: () => void; onDone: (newPane?: string, direction?: 'right' | 'down') => void; onClose: () => void
}) {
  const words = useWords()
  const [renaming, setRenaming] = useState(false)
  const [confirming, setConfirming] = useState(startDelete)
  // "Merge into…": choose a session, then confirm, as a drop of the whole session asks first.
  const [merging, setMerging] = useState<Session | 'pick' | null>(null)
  const [grouping, setGrouping] = useState(false)
  const [keep, setKeep] = useState<Keep | null>(null)  // which project a merge keeps, when both sessions have one
  const [name, setName] = useState(session.name)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useActionsDialog(at, () => { if (!pending) onClose() })
  async function run(action: () => Promise<string | undefined>, direction?: 'right' | 'down') {
    setPending(true); setError('')
    try { onDone(await action(), direction) } catch (failure) { setError(errorText(failure)); onMarks?.() } finally { setPending(false) }
  }
  const rename = (event: React.FormEvent) => {
    event.preventDefault()
    const clean = name.trim()
    if (!SESSION_NAME.test(clean)) { setError(SESSION_NAME_HELP); return }
    // A busy agent keeps its own name: say so, and keep the sheet open.
    void (async () => {
      setPending(true); setError('')
      try { const saved = await api<{ note?: string }>('rename', { pane, name: clean }); if (saved.note) setError(`Saved. ${saved.note}`); else onDone(undefined) }
      catch (failure) { setError(errorText(failure)); onMarks?.() } finally { setPending(false) }
    })()
  }
  const split = (direction: 'right' | 'down') => run(async () => (await api<{ pane: string }>('split', { session: pane, direction })).pane, direction)
  return <dialog {...dialog} aria-labelledby="actions-title">
    <div className="sheet-heading"><h2 id="actions-title">{session.name}</h2>{!at && <button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button>}</div>
    <p className="sheet-sub">{count(words, tabCount)} · {session.dir}</p>
    {grouping && onGroup
      ? <GroupPicker groups={groups} value={group} onPick={(picked, created) => { onGroup(picked, created); onClose() }} onBack={() => setGrouping(false)} />
      : merging === 'pick'
      ? <SessionPicker label={`Merge ${session.name} into`} targets={targets.filter(target => target.id !== session.id)} pending={pending} onPick={target => { setKeep(null); setMerging(target) }} onCancel={() => setMerging(null)} />
      : merging
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="merge-session-title">
          <p id="merge-session-title"><strong>Merge {session.name} into {merging.name}?</strong> {tabCount === 1 ? `Its ${words.pane} moves` : `Its ${count(words, tabCount)} move`} there, and {session.name} closes.</p>
          {keepNeeded(marks, session.id, merging.id) && <KeepChoice source={session} target={merging} value={keep} disabled={pending} onChange={setKeep} />}
          <div className="sheet-buttons"><button type="button" className="secondary" autoFocus disabled={pending} onClick={() => setMerging('pick')}>Cancel</button><button type="button" className="primary" disabled={pending || (keepNeeded(marks, session.id, merging.id) && !keep)} onClick={() => void run(async () => { await api('merge', { kind: 'session', source: session.id, target: merging.id, ...(keep && { keep }) }); return undefined })}>{pending ? 'Merging…' : 'Merge'}</button></div>
        </div>
      : confirming
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="delete-session-title">
          <p id="delete-session-title"><strong>Delete {session.name}?</strong> This ends {tabCount === 1 ? `its ${words.pane}` : `all ${count(words, tabCount)}`} and stops anything running in them. You cannot undo this.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setConfirming(false)}>Cancel</button><button type="button" className="danger" disabled={pending} onClick={() => void run(async () => { await api('tmux-sessions', { session: session.id }, undefined, 'DELETE'); return undefined })}>{pending ? 'Deleting…' : 'Delete session'}</button></div>
        </div>
      : renaming
      ? <form className="rename-form" onSubmit={rename}>
          <label htmlFor="rename-input">New name</label>
          <input id="rename-input" autoFocus value={name} maxLength={64} disabled={pending} autoComplete="off" onChange={event => setName(event.target.value)} />
          <p className="hint">This renames the tmux session too.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setRenaming(false)}>Cancel</button><button className="primary" disabled={pending}>{pending ? 'Renaming…' : 'Rename'}</button></div>
        </form>
      : <div className="action-list">
          {onColor && <ColorRow value={color} disabled={pending} onChange={onColor} />}
          <button disabled={pending} onClick={() => { onPin(); onClose() }}>{pinned ? <PinOff size={18} /> : <Pin size={18} />}{pinned ? 'Unpin' : 'Pin to top'}</button>
          <button disabled={pending} onClick={() => { onArchive(); onClose() }}>{archived ? <ArchiveRestore size={18} /> : <Archive size={18} />}{archived ? 'Restore from archive' : 'Archive'}</button>
          <button disabled={pending} onClick={() => setRenaming(true)}><Pencil size={18} />Rename</button>
          <CopyNames pane={session.panes.find(item => item.id === pane) ?? session.panes[0]} disabled={pending} onCopied={onClose} />
          <button disabled={pending} onClick={onProject}><BookMarked size={18} />Project…</button>
          {onGroup && <button disabled={pending} onClick={() => setGrouping(true)}><FolderInput size={18} />{group && groups.some(item => item.id === group) ? `Group: ${groups.find(item => item.id === group)!.name}` : 'Add to group…'}</button>}
          <button disabled={pending || !canMoveUp} onClick={() => onMove(-1)}><ArrowUp size={18} />Move up</button>
          <button disabled={pending || !canMoveDown} onClick={() => onMove(1)}><ArrowDown size={18} />Move down</button>
          <button disabled={pending} onClick={() => setMerging('pick')}><Merge size={18} />Merge into…</button>
          <hr />
          <PlaceItems place={place} pending={pending} onClose={onClose} />
          <button disabled={pending} onClick={() => void split('right')}><Columns2 size={18} />{words.newRight}{words.tmux && <kbd>Ctrl-b %</kbd>}</button>
          <button disabled={pending} onClick={() => void split('down')}><Rows2 size={18} />{words.newBelow}{words.tmux && <kbd>Ctrl-b "</kbd>}</button>
          <hr />
          <button className="danger-item" disabled={pending} onClick={() => setConfirming(true)}><Trash2 size={18} />Delete session</button>
        </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>
}

export function PaneActions({ api, session, tabCount, archived, pane, panes, at, place, targets, onArchive, onFork, onDone, onClose, color, onColor }: {
  color?: string; onColor?: (color: TagColor | null) => void
  api: Api; session: Session | undefined; tabCount: number; archived: boolean; pane: Pane; panes: Pane[]; at?: Anchor; place?: Place; targets: Session[]; onArchive: () => void; onFork?: () => void; onDone: (newPane?: string, direction?: 'right' | 'down') => void; onClose: () => void
}) {
  const words = useWords()
  const [mode, setMode] = useState<'menu' | 'rename' | 'icon' | 'close' | 'move' | 'review' | 'group' | 'new-group'>('menu')
  const [name, setName] = useState(pane.pane_name ?? '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useActionsDialog(at, () => { if (!pending) onClose() })
  const last = tabCount <= 1
  async function run(action: () => Promise<string | undefined>, direction?: 'right' | 'down') {
    setPending(true); setError('')
    try { onDone(await action(), direction) } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  // A busy agent keeps its own name: say so, and keep the sheet open.
  async function rename() {
    setPending(true); setError('')
    try { const saved = await api<{ note?: string }>('pane-name', { pane: pane.id, name }); if (saved.note) setError(`Saved in Tether. ${saved.note}`); else onDone(undefined) }
    catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  const split = (direction: 'right' | 'down') => run(async () => (await api<{ pane: string }>('split', { session: pane.id, direction })).pane, direction)
  const title = [paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' · ')
  return <dialog {...dialog} aria-labelledby="pane-title">
    <div className="sheet-heading"><h2 id="pane-title">{title}</h2>{!at && <button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button>}</div>
    <p className="sheet-sub">{words.Pane} in {session?.name ?? pane.label} · {pane.dir}</p>
    {mode === 'move'
      ? <SessionPicker label={`Move this ${words.pane} to`} targets={targets.filter(target => target.id !== pane.tmux_session)} pending={pending} onCancel={() => setMode('menu')}
          onPick={target => void run(async () => { await api('merge', { kind: 'pane', source: pane.id, target: target.id }); return undefined })}
          extra={!last && <button disabled={pending} onClick={() => void run(async () => { await api('merge', { kind: 'break', source: pane.id }); return undefined })}><Plus size={18} />New session</button>} />
      : mode === 'rename'
      ? <form className="rename-form" onSubmit={event => { event.preventDefault(); void rename() }}>
          <label htmlFor="pane-name-input">{words.Pane} name</label>
          <input id="pane-name-input" autoFocus value={name} maxLength={40} disabled={pending} autoComplete="off" placeholder={paneTitle({ ...pane, pane_name: '' })} onChange={event => setName(event.target.value)} />
          <p className="hint">{pane.agent === 'claude' || pane.agent === 'codex' ? `${agentNames[pane.agent]} gets the same name. ` : ''}{last ? 'The session takes it too. ' : ''}Leave it empty to show the agent name again.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setMode('menu')}>Cancel</button><button className="primary" disabled={pending}>{pending ? 'Saving…' : 'Save'}</button></div>
        </form>
      : mode === 'group' && session
      ? <TabGroupPicker api={api} session={session} pane={pane} pending={pending} run={run} onNew={() => setMode('new-group')} onBack={() => setMode('menu')} />
      : mode === 'new-group' && session
      ? <NewGroup api={api} session={session} pane={pane} onClose={() => setMode('group')} onDone={() => onDone(undefined)} />
      : mode === 'review'
      ? <ReviewLinkPicker pane={pane} panes={panes} pending={pending} onBack={() => setMode('menu')} onPick={(worker, reviewer) => void run(async () => { await api('review-link', { worker, reviewer }); return undefined })} />
      : mode === 'icon'
      ? <IconPicker api={api} pane={pane} pending={pending} run={run} onBack={() => setMode('menu')} />
      : mode === 'close'
      ? <div className="confirm-box" role="alertdialog" aria-labelledby="close-pane-title">
          <p id="close-pane-title"><strong>Close this {words.pane}?</strong> {last ? `It is the last ${words.pane}, so this also ends the session ${session?.name ?? ''}.` : 'This stops anything running in it.'} You cannot undo this.</p>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setMode('menu')}>Cancel</button><button type="button" className="danger" disabled={pending} onClick={() => void run(async () => { await api('session', { pane: pane.id }, undefined, 'DELETE'); return undefined })}>{pending ? 'Closing…' : `Close ${words.pane}`}</button></div>
        </div>
      : <div className="action-list">
          {onColor && <ColorRow value={color} disabled={pending} onChange={onColor} />}
          <button disabled={pending} onClick={() => setMode('rename')}><Pencil size={18} />Rename {words.pane}</button>
          <CopyNames pane={pane} disabled={pending} onCopied={onClose} />
          <button disabled={pending} onClick={() => setMode('icon')}><Shapes size={18} />Change icon</button>
          <button disabled={pending} onClick={() => setMode('move')}><ArrowRightLeft size={18} />Move to session…</button>
          {session && <button disabled={pending} onClick={() => setMode('group')}><FolderInput size={18} />Move to stack…{!!session.groups?.length && <small>{placeName(pane.group ?? '', session.groups)}</small>}</button>}
          {(pane.agent === 'claude' || pane.agent === 'codex') && <button disabled={pending} onClick={() => setMode('review')}><Eye size={18} />Link a review…</button>}
          <hr />
          <PlaceItems place={place} pending={pending} onClose={onClose} />
          <button disabled={pending} onClick={() => void split('right')}><Columns2 size={18} />{words.newRight}{words.tmux && <kbd>Ctrl-b %</kbd>}</button>
          <button disabled={pending} onClick={() => void split('down')}><Rows2 size={18} />{words.newBelow}{words.tmux && <kbd>Ctrl-b "</kbd>}</button>
          {onFork && canFork(pane) && <button disabled={pending} onClick={onFork}><GitFork size={18} />New chat from this</button>}
          <hr />
          <button disabled={pending} onClick={() => { onArchive(); onClose() }}>{archived ? <ArchiveRestore size={18} /> : <Archive size={18} />}{archived ? 'Restore' : 'Archive'} {words.pane}</button>
          <button className="danger-item" disabled={pending} onClick={() => setMode('close')}><Trash2 size={18} />Close {words.pane}{words.tmux && <kbd>Ctrl-b x</kbd>}</button>
        </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>
}

// Pick a pane icon: suggested ones, a search over all lucide icons, or one Claude draws (server/pane_icons.py, about 10 s).
function IconPicker({ api, pane, pending, run, onBack }: { api: Api; pane: Pane; pending: boolean; run: (action: () => Promise<string | undefined>) => Promise<void>; onBack: () => void }) {
  const [query, setQuery] = useState('')
  const [names, setNames] = useState<string[]>([])
  const [hint, setHint] = useState('')
  const [drawing, setDrawing] = useState(false)
  useEffect(() => { if (query && !names.length) void import('lucide-react/dynamic').then(module => setNames(module.iconNames)) }, [query, names.length])
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const found = words.length ? names.filter(name => words.every(word => name.includes(word))).slice(0, 96) : []
  const choices: [string, string][] = words.length ? found.map(name => [name, name.replace(/-/g, ' ')])
    : [['', 'Agent icon'], ...(pane.pane_icon?.startsWith('svg-') ? [[pane.pane_icon, 'Drawn by Claude'] as [string, string]] : []), ...Object.keys(PANE_ICONS).map(key => [key, key] as [string, string])]
  const pick = (icon: string) => void run(async () => { await api('pane-icon', { pane: pane.id, icon }); return undefined })
  async function draw() {
    setDrawing(true)
    try { await run(async () => { await api('pane-icon/draw', { pane: pane.id, label: paneTitle(pane), hint }); return undefined }) } finally { setDrawing(false) }
  }
  const busy = pending || drawing
  return <div className="icon-picker">
    <input type="search" aria-label="Search icons" placeholder="Search 1,600 icons" value={query} disabled={busy} onChange={event => setQuery(event.target.value)} />
    <div className={words.length ? 'icon-grid scroll' : 'icon-grid'} role="group" aria-label="Pane icon">
      {choices.map(([key, label]) =>
        <button key={key || 'agent'} type="button" title={label} aria-label={label} aria-pressed={(pane.pane_icon ?? '') === key} disabled={busy} onClick={() => pick(key)}>
          <AgentIcon agent={pane.agent} icon={key} size={20} />
        </button>)}
    </div>
    {words.length > 0 && !found.length && <p className="hint">{names.length ? 'No icon has that name. Ask Claude to draw one.' : 'Loading icons…'}</p>}
    <form className="icon-draw" onSubmit={event => { event.preventDefault(); void draw() }}>
      <input aria-label="Describe the icon for Claude" placeholder={`Describe it, or leave empty for “${paneTitle(pane)}”`} maxLength={200} value={hint} disabled={busy} onChange={event => setHint(event.target.value)} />
      <button className="primary" disabled={busy}><Sparkles size={16} />{drawing ? 'Drawing…' : 'Draw'}</button>
    </form>
    <p className="hint">Claude Haiku draws it in about 10 s.</p>
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={busy} onClick={onBack}>Back</button></div>
  </div>
}

export function ResumeChat({ api, chat, sessions, onClose, onOpened }: { api: Api; chat: PastChat; sessions: { id: string; name: string; panes: Pane[] }[]; onClose: () => void; onOpened: (pane: string) => void }) {
  const [name, setName] = useState(() => chatSlug(chat.title))
  const [place, setPlace] = useState('')  // '' is a new tmux session; otherwise the session that gets the new tab
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!pending) onClose() })
  const target = sessions.find(session => session.id === place)
  async function resume(event: React.FormEvent) {
    event.preventDefault()
    const clean = name.trim()
    if (!target && !SESSION_NAME.test(clean)) { setError(SESSION_NAME_HELP); return }
    setPending(true); setError('')
    try {
      const created = await resumeChat(api, chat, target ? { after: target.panes[target.panes.length - 1].id } : { name: clean })
      onOpened(created.id)
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  return <dialog {...modal} className="sheet" aria-labelledby="resume-title">
    <form onSubmit={resume}>
      <div className="sheet-heading"><h2 id="resume-title">Resume chat</h2><button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button></div>
      <p className="sheet-sub"><AgentIcon agent={chat.agent} size={14} />{chat.title || 'Untitled chat'}</p>
      <p className="hint">Runs <code>{chat.agent === 'codex' ? 'codex resume' : 'claude --resume'}</code> in {chat.dir ?? 'your home folder'}.</p>
      <label htmlFor="resume-place">Open in</label>
      <SelectMenu id="resume-place" className="sm-field resume-place" value={place} disabled={pending} onChange={setPlace} options={[{ value: '', label: 'New session' }, ...sessions.map(session => ({ value: session.id, label: `New tab in ${session.name}` }))]} />
      {!target && <><label htmlFor="resume-name">Session name</label>
      <input id="resume-name" autoFocus value={name} maxLength={64} disabled={pending} autoComplete="off" onChange={event => setName(event.target.value)} /></>}
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary create-button" disabled={pending}>{pending ? 'Starting…' : 'Resume'}</button>
    </form>
  </dialog>
}

export { Settings } from './Settings'

// Everything the agent in this pane can use: tap a command or skill to put it in the message box.
export function Tools({ api, pane, onPick, onClose }: { api: Api; pane: Pane; onPick: (text: string) => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { loadCatalog(api, pane.id).then(setCatalog, failure => setError(errorText(failure))) }, [api, pane.id])
  const groups = catalog ? [
    { title: 'Skills', items: catalog.commands.filter(item => item.kind === 'skill') },
    { title: 'Commands', items: catalog.commands.filter(item => item.kind !== 'skill') },
  ] : []
  const insert = (name: string) => name.startsWith('$') ? name + ' ' : '/' + name + ' '
  return <dialog {...modal} className="sheet tools-sheet" aria-labelledby="tools-title">
    <div className="sheet-heading"><h2 id="tools-title">Skills, plugins and MCP</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="sheet-sub">{paneTitle(pane)} in {pane.dir}</p>
    <p className="hint">In the message box, type <code>/</code> for commands and skills{pane.agent === 'codex' ? <>, <code>$</code> for skills</> : null}, or <code>@</code> for files.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {!catalog && !error && <Skeleton label="Loading tools"><div aria-hidden="true">{['MCP servers', 'Plugins', 'Skills', 'Commands'].map(name => <div key={name} className="tools-group"><div className="sk-summary"><Bone w="7em" /></div></div>)}</div></Skeleton>}
    {catalog && <>
      <details className="tools-group"><summary>MCP servers <span className="group-count">{catalog.mcp.length}</span></summary>
        <ul>{catalog.mcp.map(server => <li key={server.name + server.source}><strong>{server.name}</strong><small>{server.source}</small></li>)}</ul>
        {catalog.note && <p className="hint">{catalog.note}</p>}
      </details>
      {catalog.plugins.length > 0 && <details className="tools-group"><summary>Plugins <span className="group-count">{catalog.plugins.filter(plugin => plugin.enabled).length} on</span></summary>
        <ul>{catalog.plugins.map(plugin => <li key={plugin.name}><strong>{plugin.name}</strong><small>{plugin.enabled ? 'On' : 'Off'} · {plugin.source}</small></li>)}</ul>
      </details>}
      {groups.map(group => <details key={group.title} className="tools-group"><summary>{group.title} <span className="group-count">{group.items.length}</span></summary>
        <ul>{group.items.map(item => <li key={item.name}><button type="button" onClick={() => onPick(insert(item.name))}><strong>{item.name.startsWith('$') ? item.name : '/' + item.name}</strong>{item.description && <small>{item.description}</small>}</button></li>)}</ul>
      </details>)}
    </>}
  </dialog>
}

// Model and effort, read from the agent's own /model picker (it opens, is read and closes again: a few seconds).
// The model list costs about 5 seconds (the terminal opens /model), so keep the last one per agent and refresh on request.
const modelCacheKey = (agent: string) => `tether.models.${agent}`
function cachedModels(agent: string): ModelOptions | null {
  try { return JSON.parse(localStorage.getItem(modelCacheKey(agent)) ?? 'null') as ModelOptions | null } catch { return null }
}
// Mark the model the status line shows now; the cached "current" flag may be old.
function markCurrent(options: ModelOptions, label?: string, effort?: string): ModelOptions {
  const name = label?.toLowerCase()
  const match = name ? options.models.find(item => item.label.toLowerCase() === name) ?? options.models.find(item => item.label.toLowerCase().includes(name)) : undefined
  return { ...options, models: match ? options.models.map(item => ({ ...item, current: item.id === match.id })) : options.models, current_effort: effort ?? options.current_effort }
}
export function ModelSheet({ api, pane, thread, currentLabel, currentEffort, onClose, onDone, onContinue }: { onContinue?: () => void; api: Api; pane: Pane; thread: string | null; currentLabel?: string; currentEffort?: string; onClose: () => void; onDone: (message: string) => void }) {
  const initial = cachedModels(pane.agent)
  const [data, setData] = useState<ModelOptions | null>(initial && markCurrent(initial, currentLabel, currentEffort))
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(!initial)
  const [model, setModel] = useState(() => data ? (data.models.find(item => item.current) ?? data.models[0])?.id ?? '' : '')
  const [effort, setEffort] = useState(data?.current_effort ?? '')
  const [busy, setBusy] = useState(false)
  const modal = useModal(true, () => { if (!busy) onClose() })
  const [view, setView] = useState<'model' | 'codex'>('model')
  const live = useRef(true)
  useEffect(() => () => { live.current = false }, [])
  const fetchList = useCallback(() => {
    api<ModelOptions>('model-options', { session: pane.id, thread }).then(result => {
      if (!live.current) return
      try { localStorage.setItem(modelCacheKey(pane.agent), JSON.stringify(result)) } catch { /* private mode */ }
      setData(result); setLoading(false)
      setModel((result.models.find(item => item.current) ?? result.models[0])?.id ?? '')
      setEffort(result.current_effort ?? '')
    }, failure => { if (live.current) { setNote(errorText(failure)); setLoading(false) } })
  }, [api, pane.id, pane.agent, thread])
  useEffect(() => { if (!initial) fetchList() }, [])  // eslint-disable-line react-hooks/exhaustive-deps -- first open only
  const chosen = data?.models.find(item => item.id === model)
  const current = data?.models.find(item => item.current)
  const changed = !!data && (model !== current?.id || (effort || null) !== (data.current_effort ?? null))
  async function apply() {
    if (!data) return
    setBusy(true); setNote('Applying… the agent is being switched.')
    try {
      const result = await api<{ message?: string }>('model', { session: pane.id, thread, model, effort: effort && effort !== data.current_effort && chosen?.effort !== false ? effort : null })
      onDone(result.message || 'Model changed.')
    } catch (failure) { setNote(errorText(failure)); setBusy(false) }
  }
  return <dialog {...modal} className="sheet model-sheet" aria-labelledby="model-title">
    <div className="sheet-heading"><h2 id="model-title">Model</h2><button type="button" className="icon-button" aria-label="Close" disabled={busy} onClick={onClose}><X /></button></div>
    {onContinue && (pane.agent === 'claude' || pane.agent === 'codex') && <button type="button" className="secondary create-button" disabled={busy || loading} onClick={onContinue}><ArrowRightLeft size={16} />Continue with…</button>}
    {pane.agent === 'claude' && <div className="settings-tabs model-tabs" role="tablist" aria-label="Model or Codex">
      {(['model', 'codex'] as const).map(item => <button type="button" role="tab" key={item} id={`model-tab-${item}`} aria-selected={view === item} aria-controls="model-panel" tabIndex={view === item ? 0 : -1} disabled={busy}
        onClick={() => setView(item)} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); const next = item === 'model' ? 'codex' : 'model'; setView(next); document.getElementById(`model-tab-${next}`)?.focus() } }}>
        {item === 'model' ? 'This session' : 'Codex helper'}</button>)}
    </div>}
    {view === 'codex' ? <div role="tabpanel" id="model-panel" aria-labelledby="model-tab-codex"><GlobalCodexLevel api={api} /></div> : <>
    <p className="sheet-sub">{pane.agent === 'antigravity' ? 'Applies as soon as you choose.' : 'Applies to this session only.'}</p>
    {data && <div className="model-list" role="radiogroup" aria-label="Model">{data.models.map(item => <button key={item.id} type="button" role="radio" aria-checked={item.id === model} disabled={busy} onClick={() => setModel(item.id)}>
      <span>{item.label}</span>{item.current && <small>current</small>}{item.id === model && <Check size={18} />}</button>)}</div>}
    {data && data.efforts.length > 0 && chosen?.effort !== false && <fieldset className="choice"><legend>Effort</legend><div className="segmented effort-levels">
      {data.efforts.map(level => <label key={level}><input type="radio" name="effort" value={level} checked={effort === level} disabled={busy} onChange={() => setEffort(level)} /><span>{level[0].toUpperCase() + level.slice(1)}</span></label>)}
    </div></fieldset>}
    {loading && <Loading className="hint" label="Reading the model list from the terminal… about 5 seconds." />}
    {note && <p className="hint" role="status">{note}</p>}
    {!loading && <button type="button" className="link-button refresh-models" disabled={busy} onClick={() => { setLoading(true); setNote(''); fetchList() }}><RotateCw size={14} />{data ? 'Refresh model list' : 'Try again'}</button>}
    {data && <button type="button" className="primary create-button" disabled={busy || !changed} onClick={() => void apply()}>{busy ? 'Applying…' : 'Apply'}</button>}
    </>}
  </dialog>
}
