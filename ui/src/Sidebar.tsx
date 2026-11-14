import { BacklogButton } from './Backlog'
import { WorkButton, WorkMark } from './WorkBoard'
import type { WorkLinks } from './WorkBoard'
import { Bone, SkeletonRows, Spinner } from './Skeleton'
import { RetryButton } from './Retry'
import { rememberCount, rememberedCount } from './skeletonCounts'
import { needsYou, isDone } from './paneState'
import { Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { Archive, ArchiveRestore, Asterisk, BarChart3, BookMarked, BookOpen, Bug, Cpu, Database, FileText, FlaskConical, GitBranch, Globe, LayoutDashboard, MessageSquare, MessagesSquare, Mic, Palette, Rocket, Server, Shield, Sparkles, Wrench, Zap, ChevronRight, Code2, GripVertical, Layers, Pin, PinOff, Plus, RefreshCw, RotateCw, Search, Settings, SlidersHorizontal, Terminal, Trash2, X } from 'lucide-react'
import { KeepChoice, keepNeeded } from './Project'
import { FolderMenu } from './SelectMenu'
import type { Keep, ProjectMark, ProjectMarks } from './Project'
import type { Agent, Api, ChatAgent, GroupInfo, Pane, PastChat, UsageData, UsageReport, UsageWindow } from './api'
import { SESSION_NAME, SESSION_NAME_HELP, errorText } from './api'
import { anchorOf, clipTitle, useModal, usePolling } from './hooks'
import type { Anchor } from './hooks'
import { AccountLogin } from './AccountLogin'
import { CodexProviderSwitch } from './CodexProvider'
import { profileSuffix } from './provider'
import type { Prefs } from './prefs'
import { DRAG_CHAT, DRAG_PANE, DRAG_SESSION } from './workspace'
import { resumeChat, resumeInNewSession, useChatHistory, useRemovedChat } from './ChatPicker'
import type { ResumeTarget } from './ChatPicker'
import { shareUsage } from './usage'
import { useHost } from './host'
import { HostMenu } from './HostMenu'
import { UpdateBanner } from './UpdateBanner'
import { Elapsed } from './Notices'
import { ShellList } from './ShellList'
import { OrganizeMenu, tagDot } from './Organize'
import { useWords } from './words'
import { WaitingBox } from './Attention'
import type { ReviewEntry } from './Attention'
import { waitingAsks } from './attention'
import { HOLD_MS } from './gesture'
import { matchSessions, searchText, searchWords } from './sessionSearch'
import type { SearchEntry } from './sessionSearch'

// The avatars layout variant (layouts.css) shows a round avatar per session and tab: up to two initials, a colour from the name.
const initials = (name: string) => (name.match(/[\p{L}\p{N}]+/gu) ?? ['?']).slice(0, 2).map(word => [...word][0]).join('').toUpperCase()
const avatar = (name: string) => <span className="session-avatar" aria-hidden="true" style={{ '--avatar': `${[...name].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360}` } as CSSProperties}>{initials(name)}</span>

export const agentNames: Record<Agent, string> = { claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', shell: 'Shell' }
// Icons a pane can wear instead of its agent's icon (tmux option @chat_icon). The person picks one in the pane menu,
// or the agent sets one itself (server/projects.py). These load at once; any other lucide name loads on demand,
// and svg-<name> is an icon Claude drew (server/pane_icons.py).
export const PANE_ICONS = { bug: Bug, flask: FlaskConical, palette: Palette, rocket: Rocket, book: BookOpen, database: Database, globe: Globe,
  server: Server, shield: Shield, wrench: Wrench, mic: Mic, branch: GitBranch, file: FileText, search: Search, sparkles: Sparkles, zap: Zap,
  cpu: Cpu, layout: LayoutDashboard, chat: MessageSquare, chart: BarChart3 } as const
type PaneIcon = keyof typeof PANE_ICONS
const LucideIcon = lazy(() => import('lucide-react/dynamic').then(module => ({ default: module.DynamicIcon })))
// Claude gets an asterisk, like the ✳ Claude Code shows in its own title. icon: the pane's own icon, when it has one.
export function AgentIcon({ agent, icon, size = 16 }: { agent: Agent; icon?: string; size?: number }) {
  const Own = agent === 'shell' ? Terminal : agent === 'codex' ? Code2 : Asterisk
  if (icon?.startsWith('svg-')) return <span className="drawn-icon" aria-hidden="true" style={{ width: size, height: size, ['--icon' as string]: `url("/mobile/api/icon/${encodeURIComponent(icon.slice(4))}.svg")` }} />
  if (icon && icon in PANE_ICONS) { const Icon = PANE_ICONS[icon as PaneIcon]; return <Icon size={size} aria-hidden="true" /> }
  if (icon && /^[a-z0-9-]+$/.test(icon)) return <Suspense fallback={<Own size={size} aria-hidden="true" />}><LucideIcon name={icon as never} size={size} aria-hidden="true" fallback={() => <Own size={size} aria-hidden="true" />} /></Suspense>
  return <Own size={size} aria-hidden="true" />
}

// The name a chat shows everywhere (tab, header, message box): the pane's own name, the agent's /rename name,
// then the tmux session's name. The same order as paneTitle, but a session name beats the generic "Claude".
export const chatTitle = (pane: Pane, session?: { name: string }) => pane.pane_name || pane.agent_name || session?.name || pane.label

// One tmux session with all its panes. "ESEF:0.1" is session ESEF, window 0, pane 1.
export interface Session { id: string; name: string; panes: Pane[]; agent: Agent; state: string; dir: string; groups?: GroupInfo[] }

export { needsYou, isDone } from './paneState'
type Status = { kind: 'approval' | 'working' | 'done' | 'background' | 'unknown' | 'idle' | 'unavailable'; label: string; at: number }
export function status(panes: Pane[], seen: Record<string, number>, selected?: string): Status {
  // Only the agent's own activity counts. Opening a session is not activity, so it does not move the session up.
  const at = Math.max(0, ...panes.map(pane => Math.max(pane.since ?? 0, pane.finished ?? 0)))
  if (panes.some(pane => needsYou(pane.state))) return { kind: 'approval', label: 'Needs you', at }
  const done = panes.filter(pane => isDone(pane, seen, selected))
  if (done.length) return { kind: 'done', label: `Done · ${agoSeconds(Math.max(...done.map(pane => pane.finished!)))}`, at }
  if (panes.some(pane => pane.state === 'Working')) return { kind: 'working', label: 'Working', at }
  const tasks = panes.flatMap(pane => pane.tasks ?? [])
  if (tasks.length) return { kind: 'background', label: tasks.join(' · '), at }
  if (panes.some(pane => pane.state === 'Unknown')) return { kind: 'unknown', label: 'Unknown', at }  // the server cannot read this pane: never show it as idle
  if (panes.some(pane => pane.state === 'Stopped')) return { kind: 'idle', label: 'Stopped', at }
  if (panes.every(pane => pane.state === 'Unavailable')) return { kind: 'unavailable', label: 'Unavailable', at }
  return { kind: 'idle', label: 'Idle', at }
}
const RANK = { approval: 0, done: 1, working: 2, background: 3, unknown: 4, idle: 5, unavailable: 6 }
// Smart order: what needs you first, then the most recent agent activity (a state change or a finished turn).
export function sortSmart(sessions: Session[], seen: Record<string, number>, selected?: string) {
  const keyed = sessions.map(session => ({ session, status: status(session.panes, seen, selected) }))
  return keyed.sort((a, b) => RANK[a.status.kind] - RANK[b.status.kind] || b.status.at - a.status.at || a.session.name.localeCompare(b.session.name)).map(item => item.session)
}
export function agoSeconds(seconds: number) {
  const minutes = Math.floor((Date.now() / 1000 - seconds) / 60)
  return minutes < 1 ? 'now' : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`
}
// A name set in Tether wins; then the agent's own session name (/rename), which follows later renames; then the agent.
export const paneTitle = (pane: Pane) => pane.pane_name || pane.agent_name || agentNames[pane.agent]
export const paneIndex = (pane: Pane) => pane.label.includes(':') ? pane.label.slice(pane.label.lastIndexOf(':') + 1) : '0.0'
/** The pane index for display: empty when the pane has a name of its own. */
export const shownIndex = (pane: Pane) => pane.pane_name || pane.agent_name ? '' : paneIndex(pane)
/** The tmux session name: the label without the window.pane suffix it gets when the session has more than one pane. */
export const sessionName = (pane: Pane) => pane.label.includes(':') ? pane.label.slice(0, pane.label.lastIndexOf(':')) : pane.label

export function groupSessions(panes: Pane[], hide?: (pane: Pane) => boolean): Session[] {
  const byId = new Map<string, Pane[]>()
  for (const pane of panes) byId.set(pane.tmux_session, [...(byId.get(pane.tmux_session) ?? []), pane])
  return [...byId.entries()].flatMap(([id, list]) => {
    const sorted = [...list].sort((a, b) => paneIndex(a).localeCompare(paneIndex(b), undefined, { numeric: true }))
    const shown = hide ? sorted.filter(pane => !hide(pane)) : sorted
    if (!shown.length) return []
    const states = shown.map(pane => pane.state)
    return [{
      id, name: sessionName(sorted[0]), panes: shown, dir: sorted[0].dir,
      agent: shown.find(pane => pane.agent !== 'shell')?.agent ?? 'shell',
      state: states.find(needsYou) ?? (states.includes('Working') ? 'Working' : states[0]),
    }]
  })
}

// Archived: the id still names the session it had when you archived it. tmux reuses ids after a restart; a new session stays out.
export const isArchived = (session: Session, archived: Record<string, string>) => archived[session.id] === session.name
export const toggleArchive = (session: Session) => (current: Prefs) => {
  const archived = { ...current.archived }
  if (isArchived(session, current.archived)) delete archived[session.id]; else archived[session.id] = session.name
  return { archived }
}

export function sortByPrefs(sessions: Session[], order: string[]) {
  const rank = (session: Session) => { const at = order.indexOf(session.id); return at < 0 ? Number.MAX_SAFE_INTEGER : at }
  return [...sessions].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name, undefined, { numeric: true }))
}

/** The sidebar order for the chosen sort. Smart and recent use agent activity; name is A to Z. */
export function sortSessions(sessions: Session[], prefs: Pick<Prefs, 'sort' | 'order' | 'seen'>, selected?: string) {
  if (prefs.sort === 'smart') return sortSmart(sessions, prefs.seen, selected)
  if (prefs.sort === 'name') return [...sessions].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))
  if (prefs.sort === 'recent') return sessions.map(session => ({ session, at: status(session.panes, prefs.seen, selected).at })).sort((a, b) => b.at - a.at || a.session.name.localeCompare(b.session.name)).map(item => item.session)
  return sortByPrefs(sessions, prefs.order)
}
const lastFolder = (dir: string) => dir.split('/').filter(Boolean).pop() || '/'
/** The body groups under Needs you and Pinned. Each session shows in exactly one group. */
function bodyGroups(rest: Session[], prefs: Prefs): { key: Section; title: string; items: Session[] }[] {
  if (prefs.groupBy === 'none') return [{ key: 'all', title: 'Sessions', items: rest }]
  if (prefs.groupBy === 'folder') {
    const byFolder = new Map<string, Session[]>()
    for (const session of rest) byFolder.set(lastFolder(session.dir), [...(byFolder.get(lastFolder(session.dir)) ?? []), session])
    const shared = [...byFolder.entries()].filter(([, items]) => items.length > 1).sort(([a], [b]) => a.localeCompare(b))
    const single = rest.filter(session => byFolder.get(lastFolder(session.dir))!.length === 1)
    return [...shared.map(([folder, items]) => ({ key: `f:${folder}` as const, title: folder, items })), ...(single.length ? [{ key: 'other' as const, title: 'Other', items: single }] : [])]
  }
  if (prefs.groupBy === 'custom') {
    const known = new Set(prefs.groups.map(group => group.id))
    const own = prefs.groups.map(group => ({ key: `g:${group.id}` as const, title: group.name, items: rest.filter(session => prefs.groupOf[session.id] === group.id) })).filter(group => group.items.length)
    const loose = rest.filter(session => !known.has(prefs.groupOf[session.id]))
    return [...own, ...(loose.length || !own.length ? [{ key: 'other' as const, title: own.length ? 'Ungrouped' : 'Sessions', items: loose }] : [])]
  }
  return [{ key: 'agents', title: 'Claude & Codex', items: rest.filter(session => session.agent !== 'shell') },
    ...(rest.some(session => session.agent === 'shell') ? [{ key: 'terminals' as const, title: 'Terminals', items: rest.filter(session => session.agent === 'shell') }] : [])]
}

export function ago(time: string) {
  const minutes = Math.round((Date.now() - Date.parse(time)) / 60000)
  if (!Number.isFinite(minutes)) return ''
  if (minutes < 60) return `${Math.max(1, minutes)}m ago`
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h ago`
  return `${Math.round(minutes / 60 / 24)}d ago`
}
// Every row starts with the same status column, so the marks line up from row to row. Idle is a quiet ring.
export const dot = (state: Status) => <span className="state-slot">{state.kind === 'background'
  ? <Layers size={14} className="state-background" role="img" aria-label={`Running: ${state.label}`} />
  : <span className={`state-dot ${state.kind}`} role="img" aria-label={state.label} title={state.label} />}</span>

// Agents that work now or wait for you, across every session, so one look answers "what is running?" (and "can I restart?").
export const runningPanes = (sessions: Session[]) => sessions.flatMap(session => session.panes
  .filter(pane => pane.agent !== 'shell' && (pane.state === 'Working' || needsYou(pane.state)))
  .map(pane => ({ pane, session })))
  .sort((a, b) => Number(needsYou(b.pane.state)) - Number(needsYou(a.pane.state)) || (a.pane.since ?? 0) - (b.pane.since ?? 0))

// Agent turns that ended after you last looked: the "Unread" list. Newest first.
export const unreadPanes = (sessions: Session[], seen: Record<string, number>, selected?: string) => sessions.flatMap(session => session.panes
  .filter(pane => pane.agent !== 'shell' && isDone(pane, seen, selected))
  .map(pane => ({ pane, session })))
  .sort((a, b) => (b.pane.finished ?? 0) - (a.pane.finished ?? 0))

// "2 need you · 1 working", or "All idle". One wording for the sidebar bar and the bubble.
export function runningSummary(running: ReturnType<typeof runningPanes>) {
  const working = running.filter(item => item.pane.state === 'Working').length
  const waiting = running.length - working
  return running.length ? [waiting && `${waiting} need you`, working && `${working} working`].filter(Boolean).join(' · ') : 'All idle'
}

export function RunningList({ running, selected, seen = {}, onOpen }: { running: ReturnType<typeof runningPanes>; selected?: string; seen?: Record<string, number>; onOpen: (pane: Pane) => void }) {
  return <ul className="running-list">{running.map(({ pane, session }) => <li key={pane.id}><button type="button" className={pane.id === selected ? 'selected' : ''} aria-current={pane.id === selected ? 'page' : undefined} onClick={() => onOpen(pane)}>
    {dot(status([pane], seen, undefined))}
    <span className="running-name"><strong>{paneTitle(pane)}</strong><small>{session.name} · {agentNames[pane.agent]}</small></span>
    {pane.state === 'Working' && pane.since ? <small className="running-time"><Elapsed since={pane.since * 1000} /></small>
      : needsYou(pane.state) ? <small className="running-time needs">{pane.state === 'Approval' ? 'Approval' : 'Question'}</small>
      : <small className="running-time">{pane.finished ? (ago => ago === 'now' ? 'Just now' : `${ago} ago`)(agoSeconds(pane.finished)) : 'Done'}</small>}
  </button></li>)}</ul>
}

// Collapsed bar at the foot of the sidebar. Open, it shows the list, which scrolls on its own.
function RunningNow({ sessions, selected, onOpen }: { sessions: Session[]; selected?: string; onOpen: (pane: Pane) => void }) {
  const [open, setOpen] = useState(false)
  const running = runningPanes(sessions)
  if (!sessions.some(session => session.panes.some(pane => pane.agent !== 'shell'))) return null
  const waiting = running.some(item => item.pane.state !== 'Working')
  return <section className="running-now">
    <button type="button" className="running-toggle" aria-expanded={open} aria-controls="running-now-list" onClick={() => setOpen(!open)}>
      <span className={`running-mark ${waiting ? 'attention' : running.length ? 'working' : ''}`} aria-hidden="true" />
      <span>Running now</span><span className="group-count">{runningSummary(running)}</span><ChevronRight size={14} className="chevron" />
    </button>
    {open && <div id="running-now-list" className="running-scroll">{running.length ? <RunningList running={running} selected={selected} onOpen={onOpen} /> : <p className="group-empty">No agent is working. All are idle.</p>}</div>}
  </section>
}

// Fixed sections, plus g:<id> (your groups) and f:<folder> (folder groups).
type Section = 'needs' | 'pinned' | 'agents' | 'terminals' | 'shells' | 'archived' | 'past' | 'other' | 'all' | `g:${string}` | `f:${string}`
// What a sidebar drag carries: a whole session, or one pane of a session with several panes.
type Carry = { session: string; pane?: string; chat?: PastChat }
// The server's answer to a merge: the session that now holds the moved panes, for the rename prompt.
type Moved = { session: string; name: string; pane: string }
interface Props {
  onDeck?: () => void
  review?: ReviewEntry
  api: Api
  sessions: Session[]
  selected: string | undefined
  prefs: Prefs
  update: (change: (current: Prefs) => Partial<Prefs>) => void
  loading: boolean
  /** The server has answered the session list at least once. Until then counts are unknown, not 0. */
  loaded: boolean
  error: string
  onRetry: () => unknown
  onReload: () => void
  onOpen: (pane: Pane) => void
  onNew: () => void
  onActions: (session: Session, remove?: boolean, at?: Anchor) => void
  onPaneActions: (pane: Pane, at?: Anchor) => void
  archivedTabs: Pane[]
  stacks?: Record<string, GroupInfo[]>  // each tmux session's stacks, also for a session whose tabs are all archived
  onRestoreTab: (pane: Pane) => void
  onPastChat: (chat: PastChat) => void
  onSettings: () => void
  onBacklog: () => void
  /** The work board: its button, and each linked tab's item id and stage. Absent until the board loads. */
  work?: WorkLinks
  onClose?: () => void
  // Panes shown in the desktop workspace, in slot order: their rows show the slot key (⌥1 to ⌥4).
  slots?: string[]
  /** Desktop workspace: rows can be dragged into a slot. */
  drag?: boolean
  onDropPane?: (id: string, index: number, mode: null) => void
  onDropSession?: (id: string) => void
  onDropChat?: (data: string, index: number, mode: null) => void
  /** Desktop workspace: ⌘K opens the command menu, so the key hint opens it too. */
  onCommandMenu?: () => void
  /** Opens the "Message sessions" box. Absent when no agent pane takes input. */
  onMessageAll?: () => void
  /** Sessions whose project gives agents context, and panes that started with an older version. */
  marks?: ProjectMarks
  onMarks?: () => void
  /** Desktop workspace: a Shells row opens that shell in a Terminal panel. */
  onOpenShell?: (pane: string) => void
  /** Shells that ended from the Shells list: their panels close. */
  onShellsEnded?: (panes: string[]) => void
  /** The + on a session row: a new tab (tmux pane) in that session. */
  onAddPane?: (session: Session, at: Anchor) => void
}

// The drawer unmounts on close; the wide sidebar keeps its own scroll.
let drawerScrollTop = 0

export function Sidebar({ onDeck, review, api, sessions, selected, prefs, update, loaded, error, onRetry, onReload, onOpen, onNew, onActions, onPaneActions, onPastChat, onSettings, onBacklog, work, onClose, slots, drag, onDropPane, onDropSession, onDropChat, onCommandMenu, onMessageAll, marks, onMarks, onOpenShell, onShellsEnded, onAddPane, archivedTabs, onRestoreTab, stacks }: Props) {
  const host = useHost()
  const [query, setQuery] = useState('')
  const [reorder, setReorder] = useState<Section | null>(null)
  // Drag a session or a pane onto another session to merge them. Drag one pane to "New session" to give it its own.
  const [carry, setCarry] = useState<Carry | null>(null)
  const [overNew, setOverNew] = useState(false)
  // The rename box waits for the list that holds the moved panes (`before` is the list at the merge).
  const [renaming, setRenaming] = useState<{ moved: Moved; before: Session[] } | null>(null)
  const [mergeError, setMergeError] = useState('')
  // A whole-session merge closes the dragged session, so it asks first. A pane move does not.
  const [confirming, setConfirming] = useState<{ source: Session; target: Session } | null>(null)
  const merge = (kind: 'session' | 'pane' | 'break', source: string, target?: string, keep?: Keep) => {
    setCarry(null); setOverNew(false); setMergeError(''); setConfirming(null)
    // Moved panes keep their old session's name (server), so only a new group asks for a name: a single-pane target,
    // or a split-out the server had to call split-HHMMSS. Merging into a group that already has a name does not ask.
    const single = sessions.find(session => session.id === target)?.panes.length === 1
    api<Moved>('merge', { kind, source, target, ...(keep && { keep }) }).then(moved => {
      if (kind === 'break' ? /^split-\d{6}/.test(moved.name) : single) setRenaming({ moved, before: sessions })
      onRetry()
    }, failure => { setMergeError(errorText(failure)); onMarks?.() })
  }
  // A closed past chat opens as a new tab of the session it lands on, or as a new session.
  const [openingChat, setOpeningChat] = useState('')
  const openChat = (chat: PastChat, target: ResumeTarget | null) => {
    setCarry(null); setOverNew(false); setMergeError(''); setOpeningChat(chat.title || 'Untitled chat')
    const opened = target ? resumeChat(api, chat, target) : resumeInNewSession(api, chat)
    opened.then(pane => { onRetry(); onOpen(pane) }, failure => setMergeError(errorText(failure))).finally(() => setOpeningChat(''))
  }
  const dropOn = (target: Session) => {
    if (carry?.chat) { openChat(carry.chat, { after: target.panes[target.panes.length - 1].id }); return }
    const source = carry && !carry.pane ? sessions.find(session => session.id === carry.session) : undefined
    if (carry?.pane) merge('pane', carry.pane, target.id)
    else if (source) { setCarry(null); setConfirming({ source, target }) }
  }
  const startCarry = (next: Carry | null) => { if (next) setRenaming(null); setCarry(next) }
  // Touch lifts a row at every width. A hold released on the spot still opens the menu.
  const root = useRef<HTMLDivElement>(null)
  const hold = useRef<{ timer: number; x: number; y: number; lastX: number; lastY: number; lastAt: number; vx: number; vy: number; carried: Carry; label: string; menu: () => void; active: boolean; moved: boolean } | null>(null)
  const ghostRef = useRef<HTMLDivElement>(null)
  const ghostTimer = useRef(0)
  const [ghost, setGhost] = useState<{ x: number; y: number; label: string } | null>(null)
  const [touchOver, setTouchOver] = useState<string | null>(null)
  const latest = useRef({ dropOn, merge, openChat, sessions, onDropPane, onDropSession, onDropChat })
  useEffect(() => { latest.current = { dropOn, merge, openChat, sessions, onDropPane, onDropSession, onDropChat } })
  const ghostX = (x: number) => {
    const edge = x < 118 ? 118 : x > window.innerWidth - 118 ? window.innerWidth - 118 : x
    return edge + (x - edge) * .25
  }
  const holdStart = (event: React.TouchEvent, carried: Carry, label: string, menu: () => void) => {
    if (reorder || event.touches.length !== 1) return
    const { clientX: x, clientY: y } = event.touches[0]
    if (hold.current) window.clearTimeout(hold.current.timer)
    // A ghost still settling from the last drop belongs to that drop: clear it, and let this touch start its own hold.
    if (ghostTimer.current) { window.clearTimeout(ghostTimer.current); ghostTimer.current = 0; setGhost(null) }
    hold.current = { x, y, lastX: x, lastY: y, lastAt: performance.now(), vx: 0, vy: 0, carried, label, menu, active: false, moved: false, timer: window.setTimeout(() => {
      const held = hold.current
      if (!held) return
      held.active = true
      held.lastAt = performance.now()
      navigator.vibrate?.(12)
      startCarry(held.carried)
      setGhost({ x: ghostX(held.x), y: held.y, label: held.label })
    }, HOLD_MS) }
  }
  useEffect(() => {
    const el = root.current
    if (!el) return
    let overNew = false, overSession: string | null = null
    let overSlot: HTMLElement | null = null
    const spotAt = (x: number, y: number) => document.elementFromPoint(x, y)?.closest<HTMLElement>('.slot[data-slot], .new-session-drop, .session-list > li[data-id]') ?? null
    const finishGhost = (x: number, y: number, committed: boolean, speed: number) => {
      const node = ghostRef.current
      if (!node) { setGhost(null); return }
      const position = new DOMMatrix(getComputedStyle(node).transform)
      const distance = Math.hypot(x - position.m41, y - position.m42)
      const duration = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : committed ? Math.round(Math.max(140, Math.min(320, distance / Math.max(speed, .8)))) : 220
      node.style.transition = duration ? `transform ${duration}ms cubic-bezier(.32,.72,0,1)` : 'none'
      node.style.transform = `translate3d(${x}px, ${y}px, 0)`
      window.clearTimeout(ghostTimer.current)
      if (duration) ghostTimer.current = window.setTimeout(() => { ghostTimer.current = 0; setGhost(null) }, duration)
      else { ghostTimer.current = 0; setGhost(null) }
    }
    const end = () => { if (hold.current) window.clearTimeout(hold.current.timer); hold.current = null; setTouchOver(null); setOverNew(false); overNew = false; overSession = null; overSlot?.removeAttribute('data-touch-drop'); overSlot = null }
    const move = (event: TouchEvent) => {
      const held = hold.current, touch = event.touches[0]
      if (!held || !touch) return
      if (event.touches.length !== 1) { cancel(); return }
      const far = Math.hypot(touch.clientX - held.x, touch.clientY - held.y) > 10
      if (!held.active) { if (far) end(); return }  // a scroll or a swipe, not a hold
      event.preventDefault()
      held.moved ||= far
      const now = performance.now()
      if (now > held.lastAt) { held.vx = held.vx * .4 + (touch.clientX - held.lastX) / (now - held.lastAt) * .6; held.vy = held.vy * .4 + (touch.clientY - held.lastY) / (now - held.lastAt) * .6 }
      held.lastX = touch.clientX; held.lastY = touch.clientY; held.lastAt = now
      if (ghostRef.current) { ghostRef.current.style.transition = 'none'; ghostRef.current.style.transform = `translate3d(${ghostX(touch.clientX)}px, ${touch.clientY}px, 0)` }
      const spot = spotAt(touch.clientX, touch.clientY)
      const nextSlot = latest.current.onDropPane && spot?.matches('.slot[data-slot]') ? spot : null
      if (overSlot !== nextSlot) { overSlot?.removeAttribute('data-touch-drop'); overSlot = nextSlot; overSlot?.setAttribute('data-touch-drop', '') }
      const nextNew = !!(held.carried.pane || held.carried.chat) && !!spot?.classList.contains('new-session-drop')
      if (overNew !== nextNew) { overNew = nextNew; setOverNew(nextNew) }
      const id = spot?.dataset.id
      const nextSession = id && id !== held.carried.session ? id : null
      if (overSession !== nextSession) { overSession = nextSession; setTouchOver(nextSession) }
      // Near the top or bottom edge of the list, the list scrolls so far rows can be reached.
      const nav = el.querySelector<HTMLElement>('.session-groups')
      if (nav && !nextSlot) { const box = nav.getBoundingClientRect(); if (touch.clientY < box.top + 48) nav.scrollTop -= 14; else if (touch.clientY > box.bottom - 48) nav.scrollTop += 14 }
    }
    const up = (event: TouchEvent) => {
      const held = hold.current
      if (!held) return
      if (!held.active) { end(); return }
      event.preventDefault()  // no click after a hold
      const touch = event.changedTouches[0]
      const spot = touch ? spotAt(touch.clientX, touch.clientY) : null
      const { dropOn, merge, openChat, sessions, onDropPane, onDropSession, onDropChat } = latest.current
      const speed = matchMedia('(prefers-reduced-motion: reduce)').matches || performance.now() - held.lastAt > 100 ? 0 : Math.hypot(held.vx, held.vy)
      held.moved ||= speed > .4
      const source = sessions.find(session => session.id === held.carried.session)
      const slot = spot?.matches('.slot[data-slot]') ? Number(spot.dataset.slot) : NaN
      const target = sessions.find(session => session.id === spot?.dataset.id)
      const committed = held.moved && !!(Number.isInteger(slot) && onDropPane || spot?.classList.contains('new-session-drop') && (held.carried.pane || held.carried.chat) || target && target.id !== held.carried.session)
      const box = committed ? spot?.getBoundingClientRect() : null
      finishGhost(box ? ghostX(box.left + box.width / 2) : ghostX(held.x), box ? box.top + box.height / 2 : held.y, committed, speed)
      end()
      if (!held.moved) { setCarry(null); held.menu(); return }
      if (Number.isInteger(slot) && onDropPane) {
        setCarry(null)
        if (held.carried.chat) onDropChat?.(JSON.stringify({ id: held.carried.chat.id, agent: held.carried.chat.agent }), slot, null)
        else if (held.carried.pane) onDropPane(held.carried.pane, slot, null)
        else if (source?.panes.length === 1) onDropPane(source.panes[0].id, slot, null)
        else if (source) onDropSession?.(source.id)
        return
      }
      if (spot?.classList.contains('new-session-drop') && held.carried.chat) { openChat(held.carried.chat, null); return }
      if (spot?.classList.contains('new-session-drop') && held.carried.pane) { merge('break', held.carried.pane); return }
      if (target && target.id !== held.carried.session) dropOn(target)
      else setCarry(null)
    }
    const cancel = () => { const held = hold.current; if (held?.active) { finishGhost(ghostX(held.x), held.y, false, 0); setCarry(null) } end() }
    // The phone's own long-press menu would open the row's menu at once; the hold decides on release instead.
    const menu = (event: Event) => { if (hold.current) { event.preventDefault(); event.stopPropagation() } }
    const nativeDrag = (event: Event) => { if (hold.current) { event.preventDefault(); event.stopPropagation() } }
    el.addEventListener('dragstart', nativeDrag, true)
    el.addEventListener('touchmove', move, { passive: false })
    el.addEventListener('touchend', up)
    el.addEventListener('touchcancel', cancel)
    document.addEventListener('contextmenu', menu, true)
    return () => { end(); window.clearTimeout(ghostTimer.current); el.removeEventListener('dragstart', nativeDrag, true); el.removeEventListener('touchmove', move); el.removeEventListener('touchend', up); el.removeEventListener('touchcancel', cancel); document.removeEventListener('contextmenu', menu, true) }
  }, [])
  const [organizing, setOrganizing] = useState<{ at?: Anchor } | null>(null)
  const renamed = () => { setRenaming(null); onRetry() }
  const search = query.trim().toLowerCase()
  const drawer = !!onClose
  useLayoutEffect(() => {
    const nav = root.current?.querySelector<HTMLElement>('.session-groups')
    if (!drawer || !nav) return
    if (search) { nav.scrollTop = 0; return }
    let scroll = drawerScrollTop
    const save = () => { if (nav.clientHeight) scroll = nav.scrollTop }
    const restore = () => {
      if (!nav.clientHeight) return
      nav.scrollTop = scroll
      scroll = nav.scrollTop
      observer.disconnect()
    }
    // The dialog opens after mount. Restore before paint once its list has a height.
    const observer = new ResizeObserver(restore)
    observer.observe(nav)
    restore()
    nav.addEventListener('scroll', save)
    return () => { drawerScrollTop = scroll; observer.disconnect(); nav.removeEventListener('scroll', save) }
  }, [drawer, search])
  const manual = prefs.sort === 'manual'
  // The search text is built once per data change; each keystroke only scans it.
  const groupNames = useMemo(() => new Map(prefs.groups.map(group => [group.id, group.name])), [prefs.groups])
  const [index, archivedIndex] = useMemo(() => {
    const stacksOf = new Map(Object.entries(stacks ?? {}).map(([id, list]) => [id, new Map(list.map(stack => [stack.id, stack.name]))]))
    const tabText = (pane: Pane) => searchText(paneTitle(pane), pane.tmux_pane_name, pane.agent_name, agentNames[pane.agent], stacksOf.get(pane.tmux_session)?.get(pane.group ?? ''), pane.worktree)
    const sessionText = (id: string, name: string, dir: string) => searchText(name, dir, groupNames.get(prefs.groupOf[id] ?? ''))
    const live: SearchEntry[] = sessions.map(session => ({ id: session.id, text: sessionText(session.id, session.name, session.dir), tabs: session.panes.map(pane => ({ id: pane.id, text: tabText(pane) })) }))
    // An archived tab is its own entry: its session's text and its own.
    const hidden: SearchEntry[] = archivedTabs.map(pane => ({ id: pane.id, text: sessionText(pane.tmux_session, sessionName(pane), pane.dir), tabs: [{ id: pane.id, text: tabText(pane) }] }))
    return [live, hidden]
  }, [sessions, archivedTabs, stacks, groupNames, prefs.groupOf])
  const matches = useMemo(() => search ? matchSessions(index, searchWords(search)) : null, [index, search])
  const archivedMatches = useMemo(() => search ? matchSessions(archivedIndex, searchWords(search)) : null, [archivedIndex, search])
  const visible = sortSessions(sessions, prefs, selected).filter(session => !matches || matches.has(session.id))
  const archived = visible.filter(session => isArchived(session, prefs.archived))
  const hiddenTabs = archivedTabs.filter(pane => !archivedMatches || archivedMatches.has(pane.id))
  const live = visible.filter(session => !isArchived(session, prefs.archived))
  // A session that needs you moves to the top group, out of its usual group, so it shows once.
  const needs = live.filter(session => status(session.panes, prefs.seen, selected).kind === 'approval')
  const calm = live.filter(session => !needs.includes(session))
  const pinned = calm.filter(session => prefs.pinned.includes(session.id))
  const rest = calm.filter(session => !prefs.pinned.includes(session.id))
  const groups: { key: Section; title: string; items: Session[] }[] = [
    ...(needs.length ? [{ key: 'needs' as const, title: 'Needs you', items: needs }] : []),
    ...(pinned.length ? [{ key: 'pinned' as const, title: 'Pinned', items: pinned }] : []),
    ...bodyGroups(rest, prefs),
    ...(archived.length || hiddenTabs.length ? [{ key: 'archived' as const, title: 'Archived', items: archived }] : []),
  ]
  // Keep each group's size for the next first paint of the skeleton.
  const groupSizes = groups.map(group => `${group.key}:${group.items.length}`).join(',')
  useEffect(() => {
    if (!loaded || search) return
    groupSizes.split(',').forEach(item => { const [key, count] = item.split(':'); rememberCount(`sidebar.${key}`, Number(count)) })
  }, [loaded, search, groupSizes])
  const closedAtFirst = (key: Section) => key === 'past' || key === 'archived'
  const toggle = (key: Section) => update(current => ({ collapsed: { ...current.collapsed, [key]: !(current.collapsed[key] ?? closedAtFirst(key)) } }))
  const isOpen = (key: Section) => !!search || !(prefs.collapsed[key] ?? closedAtFirst(key))
  // After the refresh, a target the list does not show (search, closed group) gets no rename box later.
  const shown = groups.filter(group => isOpen(group.key)).flatMap(group => group.items.map(session => session.id))
  const renameRow = renaming && (sessions === renaming.before || shown.includes(renaming.moved.session)) ? renaming.moved : null
  if (renaming && !renameRow) setRenaming(null)
  // A drag reorders one section; the other sessions keep their places in the saved order.
  const moveWithin = (items: Session[], ids: string[]) => update(current => {
    const others = sortByPrefs(sessions, current.order).map(session => session.id).filter(id => !ids.includes(id))
    return items === pinned ? { pinned: ids, order: [...ids, ...others] } : { order: [...ids, ...others] }
  })
  // Enter opens the first match; Down moves into the list; Escape clears the search.
  const searchKeys = (event: React.KeyboardEvent<HTMLInputElement>) => {
    const first = event.currentTarget.closest('.sidebar-inner')?.querySelector<HTMLButtonElement>('.session-groups :is(.session-main:not([aria-expanded]), .pane-children button, .archived-tab-open)')
    if (event.key === 'Enter' && query.trim() && first) { event.preventDefault(); first.click(); setQuery(''); event.currentTarget.blur() }
    else if (event.key === 'ArrowDown' && first) { event.preventDefault(); first.focus() }
    else if (event.key === 'Escape' && query) { event.preventDefault(); event.stopPropagation(); setQuery('') }
  }
  return <div className={`sidebar-inner ${ghost ? 'touch-carrying' : ''}`} ref={root}>
    <div className="sidebar-top">
      <div className="sidebar-brand"><img src="/chat/icon.svg" alt="" width="22" height="22" />Tether</div>
      <span className="sidebar-top-actions">{onMessageAll && <button className="icon-button" aria-label="Message all sessions" title="Message all sessions" onClick={onMessageAll}><MessagesSquare size={19} /></button>}<button className="icon-button" aria-label="New session" title="New session" onClick={onNew}><Plus size={20} /></button>
      {onClose && <button className="icon-button" aria-label="Close sessions" onClick={onClose}><X size={20} /></button>}</span>
    </div>
    <div className="sidebar-search-row"><label className="sidebar-search"><Search size={16} aria-hidden="true" /><input type="search" placeholder="Search" aria-label="Search sessions" aria-keyshortcuts={onCommandMenu ? undefined : 'Control+K Meta+K'} value={query} onChange={event => setQuery(event.target.value)} onKeyDown={searchKeys} />{onCommandMenu
      ? <button type="button" className="search-key" aria-label="Command menu" aria-keyshortcuts="Control+K Meta+K" title="Go to a pane or run a command" onClick={onCommandMenu}>{shortcutLabel}</button>
      : <kbd className="search-key" aria-hidden="true">{shortcutLabel}</kbd>}</label><button type="button" className="icon-button organize-button" aria-label="Organize sessions" title="Group and sort" aria-haspopup="dialog" onClick={event => setOrganizing({ at: matchMedia('(hover: hover)').matches ? anchorOf(event) : undefined })}><SlidersHorizontal size={17} /></button></div>
    <BacklogButton api={api} onOpen={onBacklog} />
    {work && <WorkButton links={work} />}
    {organizing && <OrganizeMenu api={api} sessions={sessions.filter(session => !isArchived(session, prefs.archived))} prefs={prefs} update={update} at={organizing.at} onClose={() => setOrganizing(null)} />}
    <nav className="session-groups" aria-label="Sessions">
      {/* eslint-disable-next-line react-hooks/purity -- Sample snooze expiry on each existing render without adding a clock or delaying it. */}
      {prefs.attention && !search && <WaitingBox variant="list" sort={{ order: prefs.waitingOrder ?? 'oldest', onOrder: waitingOrder => update(() => ({ waitingOrder })) }} items={waitingAsks(sessions.flatMap(item => item.panes), Date.now() / 1000)} sessions={sessions} selected={selected} review={review} onOpen={onOpen} onDeck={onDeck} />}
      {error && <div className="sidebar-error" role="alert"><span>{loaded ? 'Could not refresh sessions.' : 'Could not load sessions.'} {error}</span><RetryButton onRetry={onRetry} /></div>}
      {openingChat && <div className="past-opening" role="status">Opening {openingChat}…</div>}
      {mergeError && <div className="sidebar-error" role="alert">{mergeError}<button onClick={() => setMergeError('')}>Dismiss</button></div>}
      {groups.map(group => <section key={group.key} className={group.key === 'needs' ? 'group needs-group' : 'group'}>
        <div className="group-head">
          <button className="group-toggle" aria-expanded={isOpen(group.key)} onClick={() => toggle(group.key)}>{group.title}{loaded ? <span className="group-count">· {group.items.length + (group.key === 'archived' ? hiddenTabs.length : 0)}</span> : !error && <span className="group-count" aria-hidden="true"><Bone w="1.4em" /></span>}{group.key === 'archived' && !isOpen('archived') && [...group.items.map(session => session.panes), ...hiddenTabs.map(pane => [pane])].some(list => status(list, prefs.seen, selected).kind === 'approval') && <span className="state-dot approval" role="img" aria-label="An archived session needs you" />}<ChevronRight size={14} className="chevron" /></button>
          {isOpen(group.key) && group.items.length > 1 && !search && manual && group.key !== 'archived' && group.key !== 'needs' && <button className="group-edit" onClick={() => setReorder(reorder === group.key ? null : group.key)}>{reorder === group.key ? 'Done' : 'Reorder'}</button>}
        </div>
        {isOpen(group.key) && group.key === 'archived' && !group.items.length ? null : isOpen(group.key) && (group.items.length
          ? <SessionList workMarks={work?.marks} items={group.items} reorder={reorder === group.key} selected={selected} prefs={prefs} update={update} onOpen={onOpen} onActions={onActions} onPaneActions={onPaneActions} onMove={ids => moveWithin(group.items, ids)} pinned={group.key === 'pinned'} slots={slots ?? []} canDrag={!!drag} swipe={!drawer} api={api} carry={carry} onCarry={startCarry} onDrop={dropOn} onHold={holdStart} touchOver={touchOver} renaming={renameRow} onRenamed={renamed} marks={marks} onAddPane={onAddPane} matches={matches} />
          : !loaded ? !error && <SkeletonRows count={rememberedCount(`sidebar.${group.key}`, 2, 6)} label="Finding sessions…" className="sidebar-skeleton" meta={false} />
          : <p className="group-empty">{search ? 'No sessions or tabs match.' : group.key === 'terminals' ? 'No plain terminals. Create one with New session.' : 'Nothing here yet.'}</p>)}
        {isOpen(group.key) && group.key === 'archived' && hiddenTabs.length > 0 && <ul className="archived-tabs">{hiddenTabs.map(pane => <li key={pane.id}>
          <button type="button" className="archived-tab-open" onMouseEnter={clipTitle} onClick={() => onOpen(pane)} onContextMenu={event => { event.preventDefault(); onPaneActions(pane, anchorOf(event)) }}>{dot(status([pane], prefs.seen, selected))}<span className="pane-child-name">{[paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')} · {sessionName(pane)}</span></button>
          <button type="button" className="icon-button archived-tab-restore" aria-label={`Restore ${paneTitle(pane)}`} title="Restore" onClick={() => onRestoreTab(pane)}><ArchiveRestore size={16} /></button>
        </li>)}</ul>}
      </section>)}
      <ShellList api={api} open={isOpen('shells')} onToggle={() => toggle('shells')} onOpenShell={onOpenShell} onEnded={panes => onShellsEnded?.(panes)} />
      {(carry?.pane || carry?.chat) && <div className={`new-session-drop ${overNew ? 'over' : ''}`} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setOverNew(true) }} onDragLeave={() => setOverNew(false)} onDrop={event => { event.preventDefault(); if (carry.chat) openChat(carry.chat, null); else merge('break', carry.pane!) }}><Plus size={14} aria-hidden="true" />New session</div>}
      <section className="group">
        <div className="group-head"><button className="group-toggle" aria-expanded={isOpen('past')} onClick={() => toggle('past')}>Past chats<ChevronRight size={14} className="chevron" /></button></div>
        {isOpen('past') && <PastChats api={api} search={search} showPaths={prefs.showPaths} onPick={onPastChat} sessions={sessions} drag={!!drag && !reorder} onCarry={startCarry} onHold={holdStart} />}
      </section>
    </nav>
    {ghost && <div ref={ghostRef} className="touch-ghost" aria-hidden="true" style={{ transform: `translate3d(${ghost.x}px, ${ghost.y}px, 0)` }}><span>{ghost.label}</span></div>}
    {confirming && <MergeConfirm {...confirming} marks={marks} onMerge={keep => merge('session', confirming.source.id, confirming.target.id, keep)} onCancel={() => setConfirming(null)} />}
    <footer className="sidebar-footer">
      <RunningNow sessions={sessions} selected={selected} onOpen={onOpen} />
      <UpdateBanner />
      <Usage api={api} />
      <div className="footer-row"><HostMenu host={host} hosts={prefs.hosts ?? []} onChange={hosts => update(() => ({ hosts }))} /><ReloadButton onReload={onReload} /><button className="icon-button" aria-label="Settings" title="Settings" onClick={onSettings}><Settings size={18} /></button></div>
    </footer>
  </div>
}

// Reload Tether, always one tap away in the drawer footer. The spinner paints before the page goes.
function ReloadButton({ onReload }: { onReload: () => void }) {
  const [busy, setBusy] = useState(false)
  return <button type="button" className="icon-button reload-button" aria-label={busy ? 'Reloading Tether' : 'Reload Tether'} title="Reload Tether · unsent drafts stay" aria-busy={busy} disabled={busy}
    onClick={() => { setBusy(true); requestAnimationFrame(() => requestAnimationFrame(onReload)) }}>{busy ? <Spinner size={18} /> : <RotateCw size={18} />}</button>
}

// A book on sessions whose project gives agents context. Amber when an agent there started with an older version.
function projectMark(mark: ProjectMark | undefined) {
  if (!mark || (!mark.context && !mark.stale.length)) return null
  const stale = mark.stale.length > 0
  return <BookMarked size={11} className={`project-mark ${stale ? 'stale' : ''}`} aria-label={stale ? 'Project changed. Restart its agents to apply.' : 'Has project context'} />
}

function SessionList({ matches, workMarks, items, reorder, selected, prefs, update, onOpen, onActions, onPaneActions, onMove, pinned, slots, canDrag, swipe, api, carry, onCarry, onDrop, onHold, touchOver, renaming, onRenamed, marks, onAddPane }: { workMarks?: WorkLinks['marks']; onAddPane?: Props['onAddPane']; marks?: ProjectMarks; onHold: (event: React.TouchEvent, carried: Carry, label: string, menu: () => void) => void; touchOver: string | null; items: Session[]; reorder: boolean; selected: string | undefined; prefs: Prefs; update: Props['update']; onOpen: Props['onOpen']; onActions: Props['onActions']; onPaneActions: Props['onPaneActions']; onMove: (ids: string[]) => void; pinned: boolean; slots: string[]; canDrag: boolean; swipe: boolean; api: Api; carry: Carry | null; onCarry: (carry: Carry | null) => void; onDrop: (target: Session) => void; renaming: Moved | null; onRenamed: () => void; matches?: Map<string, 'all' | Set<string>> | null }) {
  const list = useRef<HTMLUListElement>(null)
  const dragging = useRef<string | null>(null)
  const [preview, setPreview] = useState<string[] | null>(null)
  const order = useRef<string[] | null>(null)
  const ids = preview ?? items.map(session => session.id)
  const drag = useCallback((event: React.PointerEvent) => {
    const id = dragging.current
    if (!id || !list.current) return
    const rows = [...list.current.querySelectorAll<HTMLElement>(':scope > li')]
    const target = rows.filter(row => row.dataset.id !== id).findIndex(row => { const box = row.getBoundingClientRect(); return event.clientY < box.top + box.height / 2 })
    const without = ids.filter(other => other !== id)
    const at = target < 0 ? without.length : Math.min(target, without.length)
    const next = [...without.slice(0, at), id, ...without.slice(at)]
    if (next.join() !== ids.join()) { order.current = next; setPreview(next) }
  }, [ids])
  // Swipe a row left to show Pin, Archive and Delete (Delete still asks first); swipe right or tap the row to hide them.
  const [swiped, setSwiped] = useState<string | null>(null)
  const touch = useRef<{ x: number; y: number; id: string; row: HTMLElement; base: number; lastX: number; lastAt: number; speed: number; locked: boolean; remove?: () => void } | null>(null)
  const swipeMotion = useRef<{ row: HTMLElement; timer: ReturnType<typeof setTimeout>; done: () => void } | null>(null)
  const suppressClick = useRef(false)
  const swipeX = (x: number) => x > 0 ? x * .25 : x < -228 ? -228 + (x + 228) * .25 : x
  const settleRow = (open: boolean, speed = 0) => {
    const start = touch.current
    touch.current = null; start?.remove?.()
    if (!start) return
    const { row, id } = start, node = row.querySelector<HTMLElement>('.session-row')!
    const x = new DOMMatrix(getComputedStyle(node).transform).m41
    const duration = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0
      : open === (swiped === id) ? 220 : Math.round(Math.min(280, Math.max(200, Math.abs((open ? -228 : 0) - x) / Math.max(Math.abs(speed), .8))))
    node.style.transition = duration ? `transform ${duration}ms cubic-bezier(.32,.72,0,1)` : 'none'
    node.style.transform = `translateX(${open ? -228 : 0}px)`
    const done = () => {
      swipeMotion.current = null
      setSwiped(current => open ? id : current === id ? null : current)
      node.style.removeProperty('transform'); node.style.removeProperty('transition')
      row.querySelector<HTMLElement>('.swipe-actions')!.style.removeProperty('visibility')
    }
    if (duration) swipeMotion.current = { row, done, timer: setTimeout(done, duration) }
    else done()
  }
  useEffect(() => () => {
    touch.current?.remove?.()
    if (swipeMotion.current) clearTimeout(swipeMotion.current.timer)
  }, [])
  const togglePin = (id: string) => update(current => ({ pinned: current.pinned.includes(id) ? current.pinned.filter(other => other !== id) : [...current.pinned, id] }))
  // In the desktop workspace, a pane on screen shows its slot key.
  // The same drag drops into a workspace slot (the data) or onto another session row (the carry).
  const dragAs = (type: string, value: string, carried: Carry) => canDrag && !reorder ? { draggable: true, onDragStart: (event: React.DragEvent) => { event.dataTransfer.setData(type, value); event.dataTransfer.effectAllowed = 'copyMove'; onCarry(carried) }, onDragEnd: () => onCarry(null) } : {}
  const [over, setOver] = useState<string | null>(null)
  const dropTarget = (session: Session) => carry && carry.session !== session.id && !reorder ? {
    onDragOver: (event: React.DragEvent) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setOver(session.id) },
    onDragLeave: (event: React.DragEvent) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver(null) },
    onDrop: (event: React.DragEvent) => { event.preventDefault(); setOver(null); onDrop(session) },
  } : {}
  const words = useWords()
  const slotKey = (id: string) => { const at = slots.indexOf(id); return at < 0 ? null : <kbd className="slot-key" aria-label={`In slot ${at + 1}`}>⌥{at + 1}</kbd> }
  return <ul className={`session-list ${reorder ? 'reordering' : ''}`} ref={list}>
    {/* eslint-disable-next-line react-hooks/refs -- Sample the drag marker on reorder renders; state would add visible updates on pointer down and up. */}
    {(preview ? [...items].sort((a, b) => preview.indexOf(a.id) - preview.indexOf(b.id)) : items).map(session => {
      const multi = session.panes.length > 1
      const active = session.panes.some(pane => pane.id === selected)
      const state = status(session.panes, prefs.seen, selected)
      // During a search each session shows its matching tabs, open, and the session row opens the first match (not the last-used tab).
      const only = matches?.get(session.id)
      const tabs = only instanceof Set ? session.panes.filter(pane => only.has(pane.id)) : session.panes
      const target = (only ? undefined : session.panes.find(pane => pane.id === prefs.lastPane[session.id])) ?? tabs[0]
      const archived = isArchived(session, prefs.archived)
      // A session with several panes is open by default; its heading folds the pane rows away.
      const filtered = !!only
      const open = !multi || filtered || prefs.expanded[session.id] !== false
      const toggleOpen = () => update(current => ({ expanded: { ...current.expanded, [session.id]: current.expanded[session.id] === false } }))
      const dropping = (over === session.id || touchOver === session.id) && !!carry
      const tag = tagDot(prefs.colors?.[session.id] ?? (session.panes.length === 1 ? prefs.colors?.[session.panes[0].id] : undefined))
      return <li key={session.id} data-id={session.id} className={`${dragging.current === session.id ? 'dragging' : ''} ${dropping ? 'drop-over' : ''} ${carry && !carry.pane && carry.session === session.id ? 'carried' : ''}`} {...dropTarget(session)}>
        <div className={`swipe-row ${swiped === session.id ? 'swiped' : ''}`}
          onPointerDownCapture={() => { suppressClick.current = false }}
          onClickCapture={event => { if (suppressClick.current && event.detail) { suppressClick.current = false; event.preventDefault(); event.stopPropagation() } }}
          onTouchStart={event => {
            suppressClick.current = false
            if (!swipe || carry || reorder || event.touches.length !== 1) return
            if ((event.target as HTMLElement).closest('.swipe-actions')) {
              const pending = swipeMotion.current
              if (pending) { clearTimeout(pending.timer); pending.done() }
              return
            }
            const row = event.currentTarget, node = row.querySelector<HTMLElement>('.session-row')!
            const x = new DOMMatrix(getComputedStyle(node).transform).m41
            const pending = swipeMotion.current
            if (pending) { clearTimeout(pending.timer); if (pending.row !== row) pending.done(); swipeMotion.current = null }
            node.style.transition = 'none'; node.style.transform = `translateX(${x}px)`
            touch.current?.remove?.()
            const finger = event.touches[0]
            touch.current = { x: finger.clientX, y: finger.clientY, id: session.id, row, base: x > 0 ? x * 4 : x < -228 ? -228 + (x + 228) * 4 : x, lastX: finger.clientX, lastAt: performance.now(), speed: 0, locked: false }
          }}
          onTouchMove={event => {
            const start = touch.current
            if (!start || start.id !== session.id) return
            if (carry || reorder || event.touches.length !== 1) { settleRow(swiped === session.id); return }
            const finger = event.touches[0], dx = finger.clientX - start.x, dy = finger.clientY - start.y
            if (Math.abs(dx) <= 10 && Math.abs(dy) <= 10) return
            if (Math.abs(dx) <= 10 || Math.abs(dx) <= Math.abs(dy) * 1.5) { settleRow(swiped === session.id); return }
            start.locked = true; suppressClick.current = true
            const node = start.row.querySelector<HTMLElement>('.session-row')!
            start.row.querySelector<HTMLElement>('.swipe-actions')!.style.visibility = 'visible'
            const move = (event: TouchEvent) => {
              if (event.touches.length !== 1) { settleRow(swiped === session.id); return }
              const finger = event.touches[0], now = performance.now()
              if (now > start.lastAt) start.speed = start.speed * .4 + (finger.clientX - start.lastX) / (now - start.lastAt) * .6
              start.lastX = finger.clientX; start.lastAt = now
              node.style.transform = `translateX(${swipeX(start.base + finger.clientX - start.x)}px)`
              if (event.cancelable) event.preventDefault()
              event.stopPropagation()
            }
            // The first move reaches the hold listener before React cancels propagation.
            node.style.transform = `translateX(${swipeX(start.base + dx)}px)`
            const now = performance.now()
            if (now > start.lastAt) start.speed = dx / (now - start.lastAt) * .6
            start.lastX = finger.clientX; start.lastAt = now
            start.row.addEventListener('touchmove', move, { passive: false })
            start.remove = () => start.row.removeEventListener('touchmove', move)
            event.stopPropagation()
          }}
          onTouchCancel={() => settleRow(swiped === session.id)}
          onTouchEnd={event => {
            const start = touch.current
            if (!start || start.id !== session.id) return
            if (!start.locked) {
              touch.current = null
              const node = start.row.querySelector<HTMLElement>('.session-row')!
              node.style.removeProperty('transform'); node.style.removeProperty('transition')
              start.row.querySelector<HTMLElement>('.swipe-actions')!.style.removeProperty('visibility')
              return
            }
            if (carry || reorder) { settleRow(swiped === session.id); return }
            event.stopPropagation()
            const x = swipeX(start.base + event.changedTouches[0].clientX - start.x)
            const speed = matchMedia('(prefers-reduced-motion: reduce)').matches || performance.now() - start.lastAt > 100 ? 0 : start.speed
            settleRow(speed > .4 ? false : speed < -.4 || x < -114, speed)
          }}>
        <span className="swipe-actions" aria-hidden={swiped !== session.id}>
          <button type="button" tabIndex={swiped === session.id ? 0 : -1} onClick={() => { togglePin(session.id); setSwiped(null) }}>{prefs.pinned.includes(session.id) ? <PinOff size={18} /> : <Pin size={18} />}{prefs.pinned.includes(session.id) ? 'Unpin' : 'Pin'}</button>
          <button type="button" tabIndex={swiped === session.id ? 0 : -1} onClick={() => { update(toggleArchive(session)); setSwiped(null) }}>{archived ? <ArchiveRestore size={18} /> : <Archive size={18} />}{archived ? 'Restore' : 'Archive'}</button>
          <button type="button" className="swipe-delete" tabIndex={swiped === session.id ? 0 : -1} onClick={() => { setSwiped(null); onActions(session, true) }}><Trash2 size={18} />Delete</button>
        </span>
        <div className={`session-row ${active && !multi ? 'selected' : ''} ${multi ? 'session-head' : ''}`}>
          {renaming?.session === session.id ? <RenameRow key={renaming.pane} api={api} moved={renaming} onDone={onRenamed} /> : <button className="session-main" {...dragAs(multi ? DRAG_SESSION : DRAG_PANE, multi ? session.id : session.panes[0].id, { session: session.id })} aria-current={active && !multi ? 'page' : undefined} aria-expanded={multi && !filtered ? open : undefined} onMouseEnter={clipTitle} onTouchStart={event => onHold(event, { session: session.id }, session.name, () => onActions(session))} onClick={() => { if (swiped) { setSwiped(null); return } if (multi && !filtered) toggleOpen(); else onOpen(target) }} onContextMenu={event => { event.preventDefault(); onActions(session, false, anchorOf(event)) }}>
            {multi && !filtered && <ChevronRight size={14} className="chevron" aria-hidden="true" />}
            {!multi && dot(state)}
            {!multi && avatar(session.name)}
            <span className="session-text"><span className="session-title">{session.name}{tag}{pinned ? null : prefs.pinned.includes(session.id) && <Pin size={11} aria-label="Pinned" />}{projectMark(marks?.[session.id])}{!multi && <WorkMark mark={workMarks?.[session.panes[0].id]} />}</span>{prefs.showPaths && <span className="session-folder"><bdi dir="ltr">{session.dir}</bdi></span>}</span>
            {!multi && slotKey(session.panes[0].id)}
            {multi && !open && state.kind !== 'idle' && dot(state)}
          </button>}
          {onAddPane && !reorder && !dropping && renaming?.session !== session.id && <button type="button" className="session-add" aria-label={`${words.newRight} in ${session.name}`} title={words.newRight} aria-haspopup="dialog" onClick={event => { const box = event.currentTarget.getBoundingClientRect(); onAddPane(session, { x: box.left, y: box.bottom + 6 }) }}><Plus size={15} aria-hidden="true" /></button>}
          {dropping && <span className="merge-hint" aria-hidden="true">{carry.pane ? 'Move here' : carry.chat ? 'Open here' : 'Merge here'}</span>}
          {reorder && <span className="grip" aria-label={`Drag to move ${session.name}`} onPointerDown={event => { dragging.current = session.id; order.current = null; event.currentTarget.setPointerCapture(event.pointerId) }} onPointerMove={drag} onPointerUp={() => { if (order.current) onMove(order.current); dragging.current = null; order.current = null; setPreview(null) }} onPointerCancel={() => { dragging.current = null; order.current = null; setPreview(null) }}><GripVertical size={18} /></span>}
        </div>
        </div>
        {multi && open && !reorder && <ul className="pane-children">{tabs.map(pane => { const paneState = status([pane], prefs.seen, selected), paneTag = tagDot(prefs.colors?.[pane.id]); return <li key={pane.id}><button {...dragAs(DRAG_PANE, pane.id, { session: session.id, pane: pane.id })} className={pane.id === selected ? 'selected' : ''} aria-current={pane.id === selected ? 'page' : undefined} onMouseEnter={clipTitle} onTouchStart={event => onHold(event, { session: session.id, pane: pane.id }, paneTitle(pane) || session.name, () => onPaneActions(pane))} onClick={() => onOpen(pane)} onContextMenu={event => { event.preventDefault(); onPaneActions(pane, anchorOf(event)) }}>{dot(paneState)}{avatar(paneTitle(pane) || session.name)}<span className="pane-child-name">{[paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')} · {agentNames[pane.agent]}{profileSuffix(pane)}</span><WorkMark mark={workMarks?.[pane.id]} />{paneTag}{slotKey(pane.id)}</button></li> })}</ul>}
      </li>
    })}
  </ul>
}

// After a merge: name the session that now holds the moved panes. Enter saves; Esc or leaving the box keeps the name.
function RenameRow({ api, moved, onDone }: { api: Api; moved: Moved; onDone: () => void }) {
  const [name, setName] = useState(moved.name)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const save = (event: React.FormEvent) => {
    event.preventDefault()
    const clean = name.trim()
    if (clean === moved.name) { onDone(); return }
    if (!SESSION_NAME.test(clean)) { setError(SESSION_NAME_HELP); return }
    setSaving(true)
    api<{ note?: string }>('rename', { pane: moved.pane, name: clean }).then(saved => { if (saved.note) { setSaving(false); setError(`Saved. ${saved.note}`) } else onDone() }, failure => { setSaving(false); setError(errorText(failure)) })
  }
  return <form className="rename-row" onSubmit={save}>
    <input aria-label="Session name" value={name} readOnly={saving} autoFocus onFocus={event => event.currentTarget.select()} onChange={event => { setName(event.target.value); setError('') }}
      onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onDone() } }} onBlur={() => { if (!saving) onDone() }} />
    {error ? <span className="rename-hint inline-error" role="alert">{error}</span> : <span className="rename-hint">Enter saves · Esc keeps the name</span>}
  </form>
}

const PAST_SOURCES = [['all', 'All'], ['claude', 'Claude'], ['codex', 'Codex']] as const
// Claude and Codex chats, newest first. A row drags into a workspace slot or onto a session; a chat that is open now moves as its pane.
function PastChats({ api, search, showPaths, onPick, sessions, drag, onCarry, onHold }: { api: Api; search: string; showPaths: boolean; onPick: (chat: PastChat) => void; sessions: Session[]; drag: boolean; onCarry: (carried: Carry | null) => void; onHold: (event: React.TouchEvent, carried: Carry, label: string, menu: () => void) => void }) {
  const [text, setText] = useState('')
  const [agent, setAgent] = useState<'all' | ChatAgent>('all')
  const [dir, setDir] = useState('')
  const [settled, setSettled] = useState('')
  const typed = text.trim()
  // The sidebar search and this box both ask the server, so wait for a pause in typing.
  const wanted = [search, typed].filter(Boolean).join(' ')
  useEffect(() => { const timer = setTimeout(() => setSettled(wanted), 180); return () => clearTimeout(timer) }, [wanted])
  const { chats, data, error, loading, refresh, next, loadMore, loadingMore, setChatHidden, foldersChanged } = useChatHistory(api, { agent, q: settled, dir })
  const [failure, setFailure] = useState('')
  const removed = useRemovedChat(setChatHidden, setFailure)
  const carryFor = (chat: PastChat): Carry => {
    const owner = chat.pane ? sessions.find(session => session.panes.some(pane => pane.id === chat.pane)) : undefined
    return owner && chat.pane ? { session: owner.id, pane: chat.pane } : { session: '', chat }
  }
  const dragProps = (chat: PastChat) => drag ? { draggable: true, onDragStart: (event: React.DragEvent) => {
    const carried = carryFor(chat)
    if (carried.pane) event.dataTransfer.setData(DRAG_PANE, carried.pane)
    else event.dataTransfer.setData(DRAG_CHAT, JSON.stringify({ id: chat.id, agent: chat.agent }))
    event.dataTransfer.effectAllowed = 'copyMove'
    onCarry(carried)
  }, onDragEnd: () => onCarry(null) } : {}
  const counts = data?.counts
  const folders = data?.dirs ?? []
  const filtered = !!(typed || search || dir || agent !== 'all')
  return <>
    <div className="past-filters">
      <label className="past-search"><Search size={13} aria-hidden="true" /><input type="search" value={text} placeholder="Search past chats" aria-label="Search past chats" autoComplete="off" onChange={event => setText(event.target.value)} /></label>
      <div className="cp-filters">
        <div className="cp-sources" role="radiogroup" aria-label="Agent">
          {PAST_SOURCES.map(([id, label]) => <button type="button" key={id} role="radio" aria-checked={agent === id} className={agent === id ? 'on' : ''} title={counts ? `${counts[id]} chats` : undefined} onClick={() => setAgent(id)}>{label}</button>)}
        </div>
        <FolderMenu api={api} value={dir} folders={folders} mode={data?.temporary} hiddenCount={data?.hiddenCount} onChange={setDir} onChanged={foldersChanged} onError={setFailure} onRemoved={removed.noteFolder} />
      </div>
    </div>
    {failure && <p className="error cp-error" role="alert">{failure}</p>}
    {removed.bar}
    {error ? <div className="sidebar-error" role="alert"><span>Could not load past chats. {error}</span><RetryButton onRetry={refresh} /></div>
      : loading && !chats.length ? <SkeletonRows count={5} label="Loading past chats…" className="past-skeleton" />
      : !chats.length ? <p className="group-empty">{filtered ? 'No past chats match.' : 'No past chats found.'}{filtered && <> <button type="button" className="link-button" onClick={() => { setText(''); setAgent('all'); setDir('') }}>Clear filters</button></>}</p>
      : <ul className="session-list past-list">{chats.map(chat => <li key={chat.agent + chat.id}><div className="session-row"><button className="session-main" {...dragProps(chat)} onClick={() => onPick(chat)} onTouchStart={event => onHold(event, carryFor(chat), chat.title || 'Untitled chat', () => onPick(chat))}>
        <span className="session-icon" title={agentNames[chat.agent]}><AgentIcon agent={chat.agent} size={16} /><span className="visually-hidden">{agentNames[chat.agent]}: </span></span>
        <span className="session-text"><span className="session-title">{chat.title || 'Untitled chat'}</span><span className="past-meta"><span>{chat.live ? 'Open now · ' : ''}{ago(chat.time)}</span>{showPaths && chat.dir && <span className="session-folder"><bdi dir="ltr">{chat.dir}</bdi></span>}</span></span>
      </button>{!chat.live && <button type="button" className="row-remove" aria-label={`Remove ${chat.title || 'this chat'} from the list`} title="Remove from the list" onClick={() => removed.remove(chat)}><X size={14} aria-hidden="true" /></button>}</div></li>)}</ul>}
    {next !== null && !error && <button type="button" className="past-more" disabled={loadingMore} onClick={() => void loadMore().catch(() => undefined)}>{loadingMore ? <><Spinner /> Loading…</> : 'Show older chats'}</button>}
  </>
}

// Hours or days until a window resets: "3h", "2d".
export function resetsIn(resets: number | null) {
  if (!resets) return ''
  const hours = (resets * 1000 - Date.now()) / 3600000
  if (hours <= 0) return ''
  return hours < 1 ? `${Math.max(1, Math.round(hours * 60))}m` : hours < 48 ? `${Math.round(hours)}h` : `${Math.round(hours / 24)}d`
}

function Meter({ name, window }: { name: string; window: UsageWindow }) {
  const percent = Math.max(0, Math.min(100, Math.round(window.percent)))
  const reset = resetsIn(window.resets_at)
  const level = percent >= 90 ? 'high' : percent >= 70 ? 'warn' : ''
  return <li className="meter" aria-label={`${name} ${window.label} limit: ${percent}% used${reset ? `, resets in ${reset}` : ''}`}>
    <span className="meter-name">{name}</span><span className="meter-window">{window.label}</span>
    <span className={`meter-bar ${level}`} aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>
    <span className="meter-value">{percent}%</span><span className="meter-reset">{reset}</span>
  </li>
}

// Plan limits for Claude and Codex. Both are account-wide, so one report covers every session.
function Usage({ api }: { api: Api }) {
  const fetcher = useCallback((signal: AbortSignal) => api<UsageData>('usage', undefined, signal), [api])
  const { data, refresh } = usePolling(fetcher, 60000)
  const [details, setDetails] = useState(false)
  useEffect(() => shareUsage(data), [data])
  if (!data) return null
  const rows = [['Claude', data.claude], ['Codex', data.codex]] as const
  return <>
    <button type="button" className="usage-button" aria-label="Plan usage. Show details" onClick={() => { void refresh(); setDetails(true) }}>
      <ul className="usage">
        {rows.map(([name, report]) => report?.windows.length
          ? report.windows.map(window => <Meter key={name + window.label} name={name} window={window} />)
          : <li key={name} className="meter empty"><span className="meter-name">{name}</span><span className="meter-none">No usage report yet</span></li>)}
      </ul>
    </button>
    {details && <UsageSheet data={data} api={api} onDone={refresh} onClose={() => setDetails(false)} />}
  </>
}

// The Plan usage sheet on its own, for /usage, /cost, /stats and /status in a Claude chat. It starts a live check at once.
export function PlanUsage({ api, onClose }: { api: Api; onClose: () => void }) {
  const [data, setData] = useState<UsageData | null>(null)
  const load = useCallback((live: boolean) => api<UsageData>(live ? 'usage?live=1' : 'usage').then(setData, () => {}), [api])
  useEffect(() => { void load(false).then(() => load(true)) }, [load])
  if (!data) return null
  return <UsageSheet data={data} api={api} onDone={() => load(false)} onClose={onClose} />
}

const WINDOW_NAMES: Record<string, string> = { '5h': '5-hour limit', '7d': 'Weekly limit' }
const when = (seconds: number) => new Date(seconds * 1000).toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' })
function MergeConfirm({ source, target, marks, onMerge, onCancel }: { source: Session; target: Session; marks?: ProjectMarks; onMerge: (keep?: Keep) => void; onCancel: () => void }) {
  const modal = useModal(true, onCancel)
  const [keep, setKeep] = useState<Keep | null>(null)
  const ask = keepNeeded(marks, source.id, target.id)
  const count = source.panes.length
  return <dialog {...modal} className="sheet merge-confirm" aria-labelledby="merge-title" aria-describedby="merge-text">
    <div className="sheet-heading"><h2 id="merge-title">Merge sessions?</h2><button type="button" className="icon-button" aria-label="Close" onClick={onCancel}><X /></button></div>
    <p id="merge-text">Merge {source.name} into {target.name}? {count === 1 ? 'Its pane moves' : `Its ${count} panes move`} there, and {source.name} closes.</p>
    {ask && <KeepChoice source={source} target={target} value={keep} onChange={setKeep} />}
    <div className="sheet-buttons"><button type="button" className="secondary" autoFocus onClick={onCancel}>Cancel</button><button type="button" className="primary" disabled={ask && !keep} onClick={() => onMerge(keep ?? undefined)}>Merge</button></div>
  </dialog>
}

function reportedAgo(value: string | number | null) {
  if (value === null) return ''
  const time = typeof value === 'number' ? value * 1000 : Date.parse(value)
  if (Number.isFinite(time) && Date.now() - time < 60000) return 'just now'
  return Number.isFinite(time) ? ago(new Date(time).toISOString()) : ''
}

function UsageSheet({ data, api, onDone, onClose }: { data: UsageData; api: Api; onDone: () => Promise<void> | void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [loading, setLoading] = useState(false)
  // Refresh asks the server for a live Codex check (at most 30 s old), then redraws the meters from it.
  // Claude's live check asks Claude Code in SDK mode (no prompt, about 15 s).
  const reload = () => { setLoading(true); void api('usage?live=1').catch(() => {}).then(() => onDone()).finally(() => setLoading(false)) }
  const section = (name: string, report: UsageReport | null, missing: React.ReactNode, agent: 'claude' | 'codex') => <section className="usage-detail">
    <h3>{name}{report?.reported_at != null && <small>{report.source === 'live' ? 'Live' : 'Saved report'} · {reportedAgo(report.reported_at)}</small>}</h3>
    {data.live_errors?.[agent] && <p className="hint">{data.live_errors[agent]}</p>}
    {/* A sign-in or account switch changes whose limits these are: check live at once. */}
    <AccountLogin agent={agent} api={api} account={data.accounts?.[agent]} onDone={reload} />
    {agent === 'codex' && <CodexProviderSwitch api={api} />}
    {report?.windows.length ? [...report.windows.map(window => {
      const percent = Math.max(0, Math.min(100, Math.round(window.percent)))
      return <div key={window.label} className="usage-row">
        <div><strong>{WINDOW_NAMES[window.label] ?? window.label}</strong><span>{percent}% used</span></div>
        <span className={`meter-bar ${percent >= 90 ? 'high' : percent >= 70 ? 'warn' : ''}`} aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>
        <small>{window.resets_at ? `Resets ${when(window.resets_at)} (in ${resetsIn(window.resets_at)})` : 'Reset time not reported'}</small>
      </div>
    }), !report.windows.some(window => window.label === '5h') && <p key="no-5h" className="hint">No 5-hour limit reported. {name} sends only the weekly limit for this plan.</p>] : <p className="hint">{missing}</p>}
  </section>
  return <dialog {...modal} className="sheet" aria-labelledby="usage-title">
    <div className="sheet-heading"><h2 id="usage-title">Plan usage</h2><button type="button" className="icon-button usage-refresh" aria-label="Refresh" aria-busy={loading} disabled={loading} onClick={reload}><RefreshCw className={loading ? 'spin' : undefined} /></button><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="sheet-sub">Limits are for your whole account, not one session.</p>
    {section('Claude', data.claude, <>No report yet. In a Claude chat, send <code>! bash ~/apps/tmux-chat/tools/enable-claude-usage.sh</code> once. The meter fills in after the next Claude reply.</>, 'claude')}
    {section('Codex', data.codex, 'No report yet. The meter fills in after the next Codex reply.', 'codex')}
    <p className="hint">For Codex's full panel, send <code>/status</code> in a Codex chat. It opens in the terminal.</p>
  </dialog>
}

const shortcutLabel = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K'
