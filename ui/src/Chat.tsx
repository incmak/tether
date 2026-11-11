import { RetryButton } from './Retry'
import { noticeAction } from './noticeActions'
import { terminalText } from './terminalText'
import { ShellOutput } from './ShellOutput'
import { ContinueWith } from './ContinueWith'
import { shellCommand } from './shellCommand'
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useWords } from './words'
import { ArrowDown, Plus, ArrowLeftRight, Blocks, GitFork, Check, ChevronDown, ChevronRight, Clock3, Columns2, CornerDownRight, Gauge, Info, Layers, Maximize2, Menu, MessageSquare, Minimize2, MoreHorizontal, PanelTop, Pencil, Pin, PinOff, RefreshCw, RotateCcw, Rows2, Search, Settings2, Shield, ShieldAlert, SquareTerminal, Target, Terminal, WifiOff, X } from 'lucide-react'
import { MineBar, MINE_BAR_SPACE, startedLabel } from './MineBar'
import { AssistantRuntimeProvider, ThreadPrimitive, useExternalStoreRuntime } from '@assistant-ui/react'
import { ApiError, canFork, creditsLabel, errorText, goalStatusLabel } from './api'
import type { Api, Approval, AskBridge, Attention, ChatData, ChatMessage, Choice, Footer, Pane, PromptAction } from './api'
import { usePolling } from './hooks'
import { useBackStep } from './backNav'
import type { Anchor } from './hooks'
import { AgentIcon, PlanUsage, agentNames, chatTitle } from './Sidebar'
import { GroupBar } from './Groups'
import type { GroupHooks } from './Groups'
import { groupOf, groupPanes, placeName } from './groups'
import { profileName } from './provider'
import { FoundryTag } from './CodexProvider'
import type { Session } from './Sidebar'
import { ModelSheet, MuteItem, Tools, Usage } from './Sheets'
import { AgentMap, AgentPill, useSubAgents } from './AgentMap'
import { ImageViewer } from './ImageViewer'
import { SentContent, sentContent, imagesOnly } from './SentImages'
import type { Media } from './ImageViewer'
import { FilePane, MediaOpener, saveFile } from './MediaLink'
import type { Kind } from './MediaLink'
import { TextViewer } from './TextViewer'
import { speak, stopSpeaking } from './speech'
import type { TextFile } from './TextViewer'
import { OfficeViewer } from './OfficeViewer'
import type { OfficeFile } from './OfficeViewer'
import type { ToolSummary } from './api'
import { StatusStack } from './StatusStack'
import { PaneTabs, movePill } from './PaneTabs'
import { WorktreeMark } from './Worktree'
import { Elapsed, NoticeStack, formatElapsed } from './Notices'
import type { NoticeItem, NoticeVariant } from './Notices'
import { State } from './NewSession'
import { Linked, Message, convertMessage, SelectionCopy } from './Messages'
import type { WatchRun } from './Messages'
import { foldWatchCycles } from './watchCycles'
import { Composer } from './Composer'
import { Bone, Skeleton, Spinner } from './Skeleton'
import { expandPlaceholders } from './pasteText'
import { autoBlock } from './noticeText'
import { ChoiceDock, HeadsUp, taskLabel, SessionInfo, LiveSheet, SearchSheet } from './Dock'
import { askKind, KIND_LABEL } from './askCard'
import { AskBar } from './ChoiceExtras'
import { LiveTerminal } from './Terminal'
import { WORKSPACE_QUERY } from './workspace'
import { useHost } from './host'
import { ReviewLinks } from './ReviewLinks'
import { agentBrowserItems, useAgentBrowser } from './AgentBrowser'
import { codexRunItems, useCodexRuns } from './CodexRuns'


interface ChatProps { liveReplyPreview: boolean; pane: Pane; session: Session | undefined; api: Api; onMenu: () => void; onUpdate: () => void; draft: string; setDraft: (text: string) => void; onOpenPane: (pane: Pane) => void; onSessionActions: (at?: Anchor) => void; onPaneActions: (pane: string, at?: Anchor) => void; onSplit: (direction: 'right' | 'down') => void; onFork: () => void; onContinued: (id: string) => void; onReorder: (ids: string[]) => void; micOk: boolean; chime: boolean; readAloud: boolean; handsFree: boolean; slot?: SlotControls; running?: number; sidePin?: SidePinControls; allPanes: Pane[]; headerExtra?: ReactNode; askBar?: ReactNode; askSince?: number; groupHooks?: GroupHooks; onDropGroup?: (pane: string, group: string) => void; onBacklogAdd?: (text: string, pane: Pane) => void }
/** Desktop workspace controls for a Chat shown in a slot next to other slots. */
export interface SlotControls { index: number; zoomed: boolean; onZoom: () => void; onClose?: () => void; onPick?: () => void; onDragStart?: (event: React.DragEvent) => void }
export interface SidePinControls { pinned: boolean; index: number; total: number; note: string; onToggle(): void; onNote(text: string): void; onStep(dir: 1 | -1): void }

// A pinned chat says so in the header line: its place in the set and its note. A side swipe on the header steps (phone).
function PinMark({ pin }: { pin: SidePinControls }) {
  const place = pin.total > 1 ? `${pin.index + 1}/${pin.total}` : 'Pinned'
  return <><span className="header-separator">·</span><span className="pin-mark" title={`Pinned chat ${pin.index + 1} of ${pin.total}${pin.note ? `: ${pin.note}` : ''}`}><Pin size={12} aria-hidden="true" /><span className="pin-mark-place">{place}</span>{pin.note && <span className="pin-mark-note">{pin.note}</span>}</span></>
}

// The last chat data of each pane, so a pane switch shows the conversation at once and refreshes it after.
// The panes next to the open one load in the background, so the first swipe to them is instant too.
const chatCache = new Map<string, ChatData>()
const prefetching = new Set<string>()
// Claude's bottom strip belongs to one conversation in one pane.
const footerCache = new Map<string, Footer>()
const footerKey = (id: string, thread: string | null) => JSON.stringify([id, thread])
const cacheFooter = (id: string, footer: Footer) => {
  footerCache.delete(id); footerCache.set(id, footer)
  if (footerCache.size > 24) footerCache.delete(footerCache.keys().next().value as string)
}
const cacheChat = (id: string, data: ChatData) => {
  chatCache.delete(id); chatCache.set(id, data)
  if (chatCache.size > 24) chatCache.delete(chatCache.keys().next().value as string)
}
// The replies that end a turn get the copy and read-aloud actions.
function turnEndsOf(messages: ChatMessage[], working: boolean) {
  const ends = new Set<string>()
  let open = working
  for (let at = messages.length - 1; at >= 0; at--) {
    const item = messages[at]
    if (item.role === 'user' && !item.kind) { open = false; continue }
    if (item.kind === 'notice' || item.kind === 'terminal') continue
    if (!open && item.role === 'assistant' && !item.kind) ends.add(item.id)
    open = true
  }
  return ends
}
// The tool group the agent is still adding to: the newest one in a running turn. A notice after it does not end it.
function liveGroupOf(messages: ChatMessage[], working: boolean) {
  if (!working) return null
  for (let at = messages.length - 1; at >= 0; at--) {
    if (messages[at].kind === 'notice') continue
    return messages[at].kind === 'actions' ? messages[at].id : null
  }
  return null
}
// A still copy of each pane's footer (status cards, work line, message box), taken when the pane closes.
// A swipe shows it under the next-door chat, so the footer slides in with its own height and the layout does not jump.
const dockSnapshot = new Map<string, string>()
function dockHtml(page: HTMLElement | null, composerOnly = false) {
  const thread = page?.querySelector(':scope > .thread')
  let html = ''
  for (let node = thread?.nextElementSibling; node; node = node.nextElementSibling) {
    if (node.tagName === 'DIALOG' || (composerOnly && !node.classList.contains('composer-area'))) continue
    const copy = node.cloneNode(true) as HTMLElement
    copy.querySelectorAll('dialog, [role="menu"]').forEach(item => item.remove())
    copy.querySelectorAll('[id]').forEach(item => item.removeAttribute('id'))
    const boxes = node.querySelectorAll('textarea'), copies = copy.querySelectorAll('textarea')
    boxes.forEach((box, at) => { if (copies[at]) copies[at].textContent = box.value })
    html += copy.outerHTML
  }
  return html
}
function saveDock(id: string, page: HTMLElement | null) {
  if (!page) return
  const html = dockHtml(page)
  dockSnapshot.delete(id); dockSnapshot.set(id, html)
  if (dockSnapshot.size > 24) dockSnapshot.delete(dockSnapshot.keys().next().value!)
}
// A still copy of a next-door pane's chat, from the cache. It waits just off screen on its side (side 1 = right,
// -1 = left), at the place the real chat opens, so a side swipe drags both chats together like photos in a gallery.
const ChatPeek = memo(function ChatPeek({ id, data, api, side, peekRef }: { id: string; data: ChatData | undefined; api: Api; side: 1 | -1; peekRef: React.RefObject<HTMLDivElement | null> }) {
  const messages = useMemo(() => data?.messages ?? [], [data?.messages])
  const ends = useMemo(() => turnEndsOf(messages, data?.state === 'Working'), [messages, data?.state])
  const folded = useMemo(() => foldWatchCycles(messages), [messages])
  const runtime = useExternalStoreRuntime({ messages: folded, convertMessage, isRunning: data?.state === 'Working', onNew: async () => {} })
  // Messages render a moment after the data; keep the place the real chat will open at while the content grows.
  const scroller = useRef<HTMLDivElement>(null)
  const dock = useRef<HTMLDivElement>(null)
  // The footer copy is plain HTML, set outside React (Chat fills in a stand-in when the pane has none yet).
  useLayoutEffect(() => { if (dock.current) dock.current.innerHTML = dockSnapshot.get(id) ?? '' }, [id, data])
  useLayoutEffect(() => {
    const view = scroller.current
    if (!view) return
    const place = () => { const saved = scrollByPane.get(id); view.scrollTop = saved && !saved.bottom ? saved.top : view.scrollHeight }
    place()
    const observer = new ResizeObserver(place)
    if (view.firstElementChild) observer.observe(view.firstElementChild)
    return () => observer.disconnect()
  }, [id, messages])
  return <div className="chat-peek" ref={peekRef} aria-hidden="true" inert style={{ transform: `translateX(${side * 100}%)` }}><div className="thread"><div className="chat-scroll" ref={scroller}><div className="messages">
    <AssistantRuntimeProvider runtime={runtime}><ThreadPrimitive.Messages>{({ message }) => <Message api={api} session={id} actions={ends.has(message.id)} tools={message.metadata.custom.tools as ToolSummary[] | undefined} id={message.id} role={message.role} kind={message.metadata.custom.kind} title={message.metadata.custom.title ? String(message.metadata.custom.title) : undefined} count={message.metadata.custom.count} text={String(message.metadata.custom.text ?? '')} time={String(message.metadata.custom.time ?? '')} images={message.metadata.custom.images as number | undefined} imageKey={message.metadata.custom.imageKey as string | undefined} watch={message.metadata.custom.watch as WatchRun | undefined} />}</ThreadPrimitive.Messages></AssistantRuntimeProvider>
  </div></div></div><div className="chat-peek-dock" ref={dock} /></div>
})
// Where each pane was scrolled, so switching panes and coming back lands at the same place.
const scrollByPane = new Map<string, { top: number; limit: number; bottom: boolean }>()
// Messages sent while the agent works wait in its input queue and reach the transcript only when it takes them.
// Keep them here (per pane, so a pane switch keeps them) and show them as "Queued" until the transcript has them.
// "⏵⏵ auto mode on · 2 shells" → "Auto"; "⏸ plan mode on" → "Plan"; "accept edits on" → "Accept edits".
// Claude's permission modes. Same keys as footer.mode_key on the server.
const MODES: Record<string, string> = { default: 'Manual', accept: 'Accept edits', plan: 'Plan', auto: 'Auto' }
const modeKey = (mode?: string | null) => (['accept', 'plan', 'auto', 'bypass'].find(key => (mode ?? '').toLowerCase().includes(key)) ?? 'default')
// What the terminal is waiting for, as a drawer above the message box: the question, then each real option.
interface Pending { id: number; text: string; key: string; seen: number; at: number; thread?: string | null; failed?: string; offline?: boolean }  // failed: why the send failed; offline: no answer from the server at all
// Queued bubbles survive a reload: Claude already holds the message, so the bubble should stay until it shows in the chat (30 minutes at most).
const PENDING_KEY = 'tether.pending'
const pendingByPane = new Map<string, Pending[]>((() => {
  try {
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}') as Record<string, Pending[]>
    return Object.entries(saved).map(([pane, list]) => [pane, list.filter(item => Date.now() - item.at < 1800000)] as [string, Pending[]]).filter(([, list]) => list.length)
  } catch { return [] }
})())
function savePending() {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(Object.fromEntries([...pendingByPane].filter(([, list]) => list.length)))) } catch { /* storage full or blocked: keep them in memory */ }
}
const squash = (text: string) => text.replace(/\s+/g, ' ').trim()
const slashCommand = (text: string) => /^\/[^\s/]+(?:\s|$)/.test(text.trim())
// Claude stores a shell command "!ls" as "! ls", and "!! ls" as "! ! ls". Compare with the "!" marks joined, so a run command leaves the queue.
const sameCommand = (text: string) => text.replace(/^(?:!\s*)+/, '!')
const matches = (messages: ChatMessage[], key: string, images = false) => messages.filter(item => {
  if (item.role !== 'user') return false
  if (images && (item.images ?? 0) > 0) return true
  const shell = shellCommand(item.text)
  return sameCommand(squash(shell ? `!${shell.command}` : item.text)).includes(sameCommand(key))
}).length
// Panes shown as a live terminal; kept for the visit so switching panes and back keeps the mode.
const terminalPanes = new Set<string>()

// ttyd's page exposes its xterm.js terminal as window.term. Only the parts used here.
const PREVIEW_HOLD_MS = 1600  // longer than one 1.5 s poll, so one stray sample in polling mode stays hidden too
// Claude Code's own spinner: a star that grows and shrinks. A text glyph, so it takes the accent color like the terminal.
const GLYPHS = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢']
function WorkGlyph() {
  const ref = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    let at = 0
    const timer = window.setInterval(() => { at = (at + 1) % GLYPHS.length; if (ref.current) ref.current.textContent = GLYPHS[at] }, 120)
    return () => window.clearInterval(timer)
  }, [])
  return <span ref={ref} className="work-glyph" aria-hidden="true">✻</span>
}

function useSteadyPreview(text: string | undefined) {
  const present = !!text
  const [preview, setPreview] = useState({ present, ready: false })
  if (preview.present !== present) setPreview({ present, ready: false })
  useLayoutEffect(() => {
    if (!present) return
    const timer = window.setTimeout(() => setPreview({ present: true, ready: true }), PREVIEW_HOLD_MS)
    return () => window.clearTimeout(timer)
  }, [present])
  return text && preview.ready ? text : undefined
}

export function Chat({ liveReplyPreview, pane, session, api, onMenu, onUpdate, draft, setDraft, onOpenPane, onSessionActions, onPaneActions, onSplit, onFork, onContinued, onReorder, micOk, chime, readAloud, handsFree, slot, running = 0, sidePin, allPanes, headerExtra, askBar, askSince, groupHooks, onDropGroup, onBacklogAdd }: ChatProps) {
  const agentBrowser = useAgentBrowser(api)
  const words = useWords()
  const browserAsk = agentBrowser?.asks.find(item => item.pane === pane.id) ?? null
  const host = useHost()
  const panelRef = useRef<HTMLElement>(null)
  const headerTouch = useRef<{ x: number; y: number } | null>(null)
  const [limit, setLimit] = useState(() => scrollByPane.get(pane.id)?.limit ?? 40)
  const chatQuery = `session=${encodeURIComponent(pane.id)}&limit=${limit}&preview=${liveReplyPreview ? 1 : 0}${pane.agent === 'shell' ? '&mode=shell' : ''}`
  const fetchChat = useCallback((signal: AbortSignal) => api<ChatData>(`chat?${chatQuery}${document.visibilityState === 'visible' ? '&watch=1' : ''}`, undefined, signal), [api, chatQuery])
  const { data, error, loading, refresh } = usePolling(fetchChat, 1500, `stream?${chatQuery}`, chatCache.get(pane.id))
  useEffect(() => { if (data) cacheChat(pane.id, data) }, [pane.id, data])
  const preview = useSteadyPreview(liveReplyPreview ? data?.preview?.text : undefined)
  // Connection state for the banner: the browser's offline flag, plus failed chat reads while a chat is already on screen.
  const [offline, setOffline] = useState(() => !navigator.onLine)
  const wasDown = useRef(false)
  useEffect(() => {
    const up = () => { setOffline(false); void refresh() }, down = () => setOffline(true)
    window.addEventListener('online', up); window.addEventListener('offline', down)
    return () => { window.removeEventListener('online', up); window.removeEventListener('offline', down) }
  }, [refresh])
  const [sendError, setSendError] = useState('')
  const [draftBlocked, setDraftBlocked] = useState(false)
  const [toast, setToast] = useState('')
  // The /permissions tab to open once the dialog shows, such as Recently blocked from a blocked notice.
  const [permissionsTab, setPermissionsTab] = useState<string | null>(null)
  useEffect(() => {
    const down = offline || (!!error && !!data)
    if (down) wasDown.current = true
    else if (wasDown.current && data) { wasDown.current = false; setToast('Reconnected.') }
  }, [offline, error, data])
  const [keysOpen, setKeysOpen] = useState(false)
  const [tools, setTools] = useState(false)
  const [liveSheet, setLiveSheet] = useState<'goal' | 'tasks' | null>(null)
  const [searching, setSearching] = useState(false)
  const target = useRef<string | null>(null)
  const [acting, setActing] = useState(false)
  const [term, setTermState] = useState(() => terminalPanes.has(pane.id))
  // The chat box unmounts while the terminal shows. On the way back, go to the saved place (or the bottom) again.
  const setTerm = (on: boolean) => { if (on) { terminalPanes.add(pane.id); restore.current = scrollByPane.get(pane.id) ?? null } else terminalPanes.delete(pane.id); setTermState(on) }
  const [jump, setJump] = useState(false)
  const viewport = useRef<HTMLDivElement>(null)
  const page = useRef<HTMLDivElement>(null)  // everything under the tabs: it slides as one card on a side swipe
  const hold = useRef<{ top: number; until: number } | null>(null)
  useLayoutEffect(() => { const element = page.current, id = pane.id; return () => saveDock(id, element) }, [pane.id])
  // A side swipe on the chat: 'wait' until the finger shows a direction, then 'drag' moves the chat with the finger.
  const swipe = useRef<{ x: number; y: number; mode: 'wait' | 'drag'; lastX: number; lastAt: number; speed: number } | null>(null)
  const leaving = useRef<ReturnType<typeof setTimeout> | null>(null)
  // For a moment after a pane opens, the messages already there do not play the "new message" settle-in.
  const [opening, setOpening] = useState(true)
  useEffect(() => { const timer = setTimeout(() => setOpening(false), 400); return () => clearTimeout(timer) }, [])
  useEffect(() => () => { if (leaving.current) clearTimeout(leaving.current) }, [])
  // In a session with groups, the strip, the swipe and the prefetch use the open tab's group only.
  const view = useMemo(() => session?.groups?.length ? { ...session, panes: groupPanes(session.panes, session.groups, pane) } : session, [session, pane])
  const grouped = !!session?.groups?.length
  const groupName = grouped ? placeName(groupOf(pane, session?.groups), session?.groups) : ''
  const panes = view?.panes ?? []
  const ready = data !== undefined
  const [, setPrefetched] = useState(0)
  const peekNext = useRef<HTMLDivElement>(null), peekPrevious = useRef<HTMLDivElement>(null)
  const paneIndex = panes.findIndex(item => item.id === pane.id)
  const nextPane = paneIndex >= 0 ? panes[paneIndex + 1] : undefined, previousPane = paneIndex > 0 ? panes[paneIndex - 1] : undefined
  useEffect(() => {
    const list = view?.panes ?? []
    if (!ready) return
    const index = list.findIndex(item => item.id === pane.id)
    for (const near of [list[index + 1], list[index - 1]]) {
      if (!near) continue
      const key = footerKey(near.id, near.thread)
      if (near.agent === 'claude' && !footerCache.has(key) && !prefetching.has(`footer:${key}`)) {
        prefetching.add(`footer:${key}`)
        api<Footer>(`footer?session=${encodeURIComponent(near.id)}`).then(result => { if (result.observed !== false && !footerCache.has(key)) cacheFooter(key, result) }, () => {}).finally(() => prefetching.delete(`footer:${key}`))
      }
      if (chatCache.has(near.id) || prefetching.has(near.id)) continue
      prefetching.add(near.id)
      const query = `session=${encodeURIComponent(near.id)}&limit=${scrollByPane.get(near.id)?.limit ?? 40}&preview=${liveReplyPreview ? 1 : 0}${near.agent === 'shell' ? '&mode=shell' : ''}`
      api<ChatData>(`chat?${query}`).then(result => { if (!chatCache.has(near.id)) { cacheChat(near.id, result); setPrefetched(count => count + 1) } }).catch(() => {}).finally(() => prefetching.delete(near.id))
    }
  }, [ready, pane.id, view?.panes, api, liveReplyPreview])
  // Swipe left or right on the chat to go to the next or previous pane. A swipe that starts on something that scrolls
  // sideways (a wide table or code block) scrolls it instead, and a swipe from the screen edge stays Android's back gesture.
  const swipeStart = (event: React.TouchEvent) => {
    swipe.current = null
    hold.current = null
    if (event.touches.length !== 1) return
    const touch = event.touches[0]
    const offset = pullNote.current ? new DOMMatrix(getComputedStyle(pullNote.current).transform).m42 + 40 : 0
    if (pullNote.current) { pullNote.current.style.transition = 'none'; pullNote.current.style.transform = `translate3d(0, ${offset - 40}px, 0)` }
    pull.current = viewport.current && viewport.current.scrollTop <= 0 ? touch.clientY - Math.max(0, offset) : null
    pullSpeed.current = { y: touch.clientY, at: performance.now(), speed: 0 }
    if (touch.clientX < 28 || touch.clientX > window.innerWidth - 28) return
    for (let node = event.target as HTMLElement | null; node && node !== event.currentTarget; node = node.parentElement) {
      if (node.scrollWidth > node.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(node).overflowX)) return
    }
    if (window.getSelection()?.toString()) return
    swipe.current = { x: touch.clientX, y: touch.clientY, mode: 'wait', lastX: touch.clientX, lastAt: performance.now(), speed: 0 }
  }
  // Pull down at the top of the chat to load earlier messages.
  const pull = useRef<number | null>(null)
  const pullNote = useRef<HTMLParagraphElement>(null)
  const pullDistance = useRef(0)
  const pullLabel = useRef(0)
  const pullSpeed = useRef({ y: 0, at: 0, speed: 0 })
  const [pulled, setPullLabel] = useState(0)
  const setPulled = (distance: number) => {
    pullDistance.current = distance
    const label = distance >= 70 ? 70 : distance > 0 ? 1 : 0
    if (pullLabel.current !== label) { pullLabel.current = label; setPullLabel(label) }
    if (pullNote.current) {
      pullNote.current.style.transform = `translate3d(0, ${distance - 40}px, 0)`
      pullNote.current.style.opacity = distance > 0 ? '1' : '0'
    }
  }
  const pullMove = (event: React.TouchEvent) => {
    if (pull.current === null || !data?.earlier) return
    const y = event.touches[0].clientY, now = performance.now(), last = pullSpeed.current
    last.speed = last.speed * 0.4 + (y - last.y) / Math.max(1, now - last.at) * 0.6
    last.y = y; last.at = now
    const distance = Math.max(0, y - pull.current)
    if (viewport.current && viewport.current.scrollTop > 0) { pull.current = null; setPulled(0); return }
    setPulled(distance > 110 ? 110 + (distance - 110) * 0.25 : distance)
  }
  const pullEnd = () => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    // eslint-disable-next-line react-hooks/purity -- Runs on touchend, not during render.
    const commit = pull.current !== null && (pullDistance.current >= 70 || (!reduced && pullDistance.current > 10 && performance.now() - pullSpeed.current.at < 100 && pullSpeed.current.speed > 0.45))
    const duration = reduced ? 0 : commit ? Math.max(140, Math.min(320, pullDistance.current / Math.max(0.5, Math.abs(pullSpeed.current.speed)))) : 200
    if (commit) loadEarlier()
    if (pullNote.current) pullNote.current.style.transition = `transform ${duration}ms cubic-bezier(.32,.72,0,1), opacity ${duration}ms`
    pull.current = null; setPulled(0)
  }
  const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
  // Where the chat goes next: the pane on that side, the sidebar (right swipe on the first pane), or nothing.
  const swipeTarget = (dx: number) => {
    const index = panes.findIndex(item => item.id === pane.id)
    const next = panes[index + (dx < 0 ? 1 : -1)]
    return next ? next : dx > 0 && index <= 0 ? 'menu' as const : null
  }
  const tabs = useRef<HTMLDivElement>(null)
  // `ms` animates the move; 0 follows the finger. The tab pill slides the same share of the way.
  const moveChat = (x: number, ms = 0) => {
    const element = page.current
    if (!element) return
    const transition = ms ? `transform ${ms}ms cubic-bezier(.2,.8,.2,1)` : ''
    const target = x ? swipeTarget(x) : null
    movePill(tabs.current, target && target !== 'menu' ? target.id : null, Math.abs(x) / (element.clientWidth || window.innerWidth), ms)
    element.style.transition = transition
    element.style.transform = x ? `translateX(${x}px)` : ''
    for (const [peek, side] of [[peekNext.current, 100], [peekPrevious.current, -100]] as const) {
      if (!peek) continue
      peek.style.transition = transition
      peek.style.transform = `translateX(calc(${side}% + ${x}px))`
    }
  }
  const swipeMove = (event: React.TouchEvent) => {
    const start = swipe.current
    if (!start || event.touches.length !== 1) return
    const touch = event.touches[0]
    const dx = touch.clientX - start.x, dy = touch.clientY - start.y
    if (start.mode === 'wait') {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return
      if (Math.abs(dx) < Math.abs(dy) * 1.5) { swipe.current = null; return }  // a scroll, not a swipe
      if (window.getSelection()?.toString()) { swipe.current = null; return }  // a long press, then a drag: it extends the selection
      start.mode = 'drag'; pull.current = null; setPulled(0)
      // A next-door pane not seen yet has no footer copy: show this pane's message box as a stand-in.
      for (const peek of [peekNext.current, peekPrevious.current]) {
        const dock = peek?.querySelector<HTMLElement>('.chat-peek-dock')
        if (dock && !dock.childElementCount) dock.innerHTML = dockHtml(page.current, true)
      }
    }
    const now = performance.now()
    if (now > start.lastAt) start.speed = start.speed * 0.4 + ((touch.clientX - start.lastX) / (now - start.lastAt)) * 0.6
    start.lastX = touch.clientX; start.lastAt = now
    const target = swipeTarget(dx)
    // With a pane on that side the chat follows the finger; at the end of the row it resists.
    moveChat(target && target !== 'menu' ? dx : dx * 0.25)
  }
  const swipeEnd = (event: React.TouchEvent) => {
    pullEnd()
    const start = swipe.current
    swipe.current = null
    if (!start || start.mode !== 'drag') return
    const dx = event.changedTouches[0].clientX - start.x
    const width = viewport.current?.clientWidth || window.innerWidth
    const fling = Math.abs(start.speed) > 0.35 && Math.sign(start.speed) === Math.sign(dx) && Math.abs(dx) > 40
    const target = Math.abs(dx) > width * 0.3 || fling ? swipeTarget(dx) : null
    if (!target || target === 'menu') {
      moveChat(0, reduceMotion() ? 0 : 220)
      if (target === 'menu') onMenu()
      return
    }
    if (reduceMotion()) { onOpenPane(target); return }
    // Finish at the finger's speed: a fast fling ends sooner, a slow drag eases in.
    const time = Math.round(Math.min(280, Math.max(140, (width - Math.abs(dx)) / Math.max(Math.abs(start.speed), 1.2))))
    moveChat(Math.sign(dx) * width, time)
    leaving.current = setTimeout(() => onOpenPane(target), time)
  }
  const swipeCancel = () => {
    if (swipe.current?.mode === 'drag') moveChat(0, reduceMotion() ? 0 : 220)
    swipe.current = null; pull.current = null; setPulled(0)
  }
  useEffect(() => { tabs.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ inline: 'nearest', block: 'nearest' }) }, [pane.id])
  const content = useRef<HTMLDivElement>(null)
  const restore = useRef(scrollByPane.get(pane.id) ?? null)
  const sticky = useRef(scrollByPane.get(pane.id)?.bottom ?? true)
  const anchor = useRef<{ height: number; top: number } | null>(null)
  // The chat stands still while text in it is selected: a scroll under the finger would move the selection.
  const frozen = useRef(false)
  const followBottom = useCallback(() => {
    if (!sticky.current || anchor.current) return false
    const selection = window.getSelection()
    frozen.current = !!selection && !selection.isCollapsed && !!viewport.current?.contains(selection.anchorNode)
    return !frozen.current
  }, [])
  const keysRef = useRef<HTMLDivElement>(null)
  // Menus opened from ⋯ hang below it, right-aligned (a popover is 288px wide).
  const menuAnchor = (): Anchor | undefined => { const box = keysRef.current?.getBoundingClientRect(); return box && { x: box.right - 288, y: box.bottom + 4 } }
  const state = data?.state ?? pane.state
  const writable = data?.writable ?? false
  const activePane = { ...pane, agent: data?.agent ?? pane.agent }
  // The name the tab shows: the pane's own name, else the session's (a bare number is not a name).
  const claudeName = pane.pane_name || (session && !/^\d+$/.test(session.name) ? session.name : '')
  const messages = useMemo(() => data?.messages ?? [], [data?.messages])
  const sentPrompts = useMemo(() => messages.filter(message => message.role === 'user' && !message.kind).map(message => message.text), [messages])
  // Read a new reply aloud when its turn ends. Replies already there when the chat opens are not read.
  // Hands-free: when the reading ends, ask the composer to start the mic.
  const [listenRequest, setListenRequest] = useState(0)
  const lastReply = useMemo(() => [...messages].reverse().find(message => message.role === 'assistant' && !message.kind), [messages])
  const readUpTo = useRef<string | null | undefined>(undefined)
  const cachedAtOpen = useRef(chatCache.get(pane.id))
  useEffect(() => {
    if (!data || data === cachedAtOpen.current) return  // saved data is not "there when the chat opens"; wait for fresh data
    if (readUpTo.current === undefined || !readAloud) { readUpTo.current = lastReply?.id ?? null; return }
    if (state === 'Working' || !lastReply || lastReply.id === readUpTo.current) return
    readUpTo.current = lastReply.id
    if (document.visibilityState !== 'visible') return
    speak(lastReply.text, lastReply.text, handsFree && micOk && writable ? () => setListenRequest(count => count + 1) : undefined)
  }, [data, state, lastReply, readAloud, handsFree, micOk, writable])
  useEffect(() => () => stopSpeaking(), [])
  const [pending, setPendingState] = useState<Pending[]>(() => pendingByPane.get(pane.id) ?? [])
  // A running `!` command shows in the thread with its live output, so its queued bubble is hidden.
  const bang = activePane.agent === 'claude' ? data?.bang : undefined
  // The chip above the ask card names its kind: "Needs permission", "Plan to review" or "Question". Other panels keep their own label.
  const askCardKind = askKind(!!data?.plan, data?.approval != null, data?.attention)
  const askLabel = askCardKind ? KIND_LABEL[askCardKind] : undefined
  const bangKey = bang ? squash(bang.command).slice(0, 40) : ''
  const bangItem = bang ? pending.find(item => !item.failed && item.text.trim().startsWith('!') && squash(item.text.trim().slice(1)).slice(0, 40) === bangKey) : undefined
  const queued = bangItem ? pending.filter(item => item !== bangItem) : pending
  const setPending = (update: (list: Pending[]) => Pending[]) => setPendingState(list => { const next = update(list); pendingByPane.set(pane.id, next); savePending(); return next })
  const [recheck, setRecheck] = useState(0)
  useEffect(() => {
    if (!data) return
    // Claude writes no chat record for some slash commands (/usage opens a panel). Once the pane is not working, it ran them.
    const ran = (item: Pending) => slashCommand(item.text) && !item.failed && data.state !== 'Working' && Date.now() - item.at > 3000
    // /clear (and a new chat) starts a new transcript: the count taken in the old one does not apply there.
    const seen = (item: Pending) => item.thread !== undefined && item.thread !== (data.thread ?? null) ? 0 : item.seen
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Reconcile persisted queued sends with the received transcript before scheduling the slash-command recheck.
    setPending(list => { const left = list.filter(item => matches(data.messages, item.key, imagesOnly(item.text)) <= seen(item) && Date.now() - item.at < 1800000 && !ran(item)); return left.length === list.length ? list : left })
    // An idle pane sends no new data, so check a slash command again once its 3 s have passed.
    const slash = pending.filter(item => slashCommand(item.text) && !item.failed).map(item => item.at + 3100 - Date.now())
    if (!slash.length) return
    const timer = window.setTimeout(() => setRecheck(n => n + 1), Math.max(100, Math.min(...slash)))
    return () => window.clearTimeout(timer)
  }, [data, recheck, pending.length]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(''), 6000)
    return () => clearTimeout(timer)
  }, [toast])
  useEffect(() => {
    if (!keysOpen) return
    const dismiss = (event: MouseEvent) => { if (!keysRef.current?.contains(event.target as Node)) setKeysOpen(false) }
    document.addEventListener('click', dismiss)
    return () => document.removeEventListener('click', dismiss)
  }, [keysOpen])
  useLayoutEffect(() => {
    const view = viewport.current
    if (!view) return
    if (restore.current && data) {
      // Messages render a moment after the data arrives; keep the saved place while the content grows.
      if (!restore.current.bottom) { const top = restore.current.top; hold.current = { top, until: Date.now() + 2000 }; view.scrollTop = top; setJump(true) }
      else view.scrollTop = view.scrollHeight
      restore.current = null
    } else if (anchor.current && data?.limit === limit) {
      view.scrollTop = anchor.current.top + view.scrollHeight - anchor.current.height
      anchor.current = null
    } else if (followBottom()) view.scrollTop = view.scrollHeight
  }, [data, followBottom, limit, term])
  // After a search pick: once the message is in the page, center it and flash it.
  useEffect(() => {
    const id = target.current, view = viewport.current
    if (!id || !view) return
    const timer = setTimeout(() => {
      const element = view.querySelector<HTMLElement>(`[data-msg-id="${CSS.escape(id)}"]`)
      if (!element) return
      target.current = null; sticky.current = false; setJump(true)
      element.scrollIntoView({ block: element.offsetHeight > view.clientHeight * .6 ? 'start' : 'center' })  // a tall message starts at the top
      if (element.offsetHeight > view.clientHeight * .6) view.scrollTop -= 16
      hold.current = { top: view.scrollTop, until: Date.now() + 1500 }
      element.classList.add('search-hit')
      setTimeout(() => element.classList.remove('search-hit'), 2400)
    }, 80)
    return () => clearTimeout(timer)
  }, [data])
  // After a selection clears, the chat catches up with what arrived while it stood still.
  useEffect(() => {
    const caughtUp = () => { if (frozen.current && viewport.current && followBottom()) viewport.current.scrollTop = viewport.current.scrollHeight }
    document.addEventListener('selectionchange', caughtUp)
    return () => document.removeEventListener('selectionchange', caughtUp)
  }, [followBottom])
  useEffect(() => {
    const observer = new ResizeObserver(() => {
      if (hold.current && viewport.current && Date.now() < hold.current.until) viewport.current.scrollTop = hold.current.top
      else if (viewport.current && followBottom()) viewport.current.scrollTop = viewport.current.scrollHeight
    })
    if (content.current) observer.observe(content.current)
    return () => observer.disconnect()
  }, [followBottom])
  const lastFailure = useRef<unknown>(null)
  const cardError = useRef(false)
  async function write(path: string, body: object) {
    setSendError(''); setDraftBlocked(false); lastFailure.current = null; cardError.current = false
    try { await api(path, body); await refresh(); onUpdate(); return true }
    catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await refresh(); setToast('The thread changed. Review the chat, then try again.')
      } else { lastFailure.current = error; setSendError(errorText(error)); setDraftBlocked(error instanceof ApiError && error.draft) }
      return false
    }
  }
  // Claude: Up pulls every queued message back into its input; Tether clears that input and returns the text to the message box.
  // Codex has no queue to edit, so the × only hides the bubble there.
  async function unqueue(id: number) {
    if (activePane.agent !== 'claude') { setPending(list => list.filter(other => other.id !== id)); return }
    try {
      const { text } = await api<{ text: string }>('unqueue', { session: pane.id, thread: data?.thread ?? null })
      setPending(list => list.filter(other => other.failed))
      setDraft(draft.trim() ? `${draft.trimEnd()}\n${text}` : text)
      setToast('Cancelled. The queued text is back in your message box.')
    } catch (error) {
      setPending(list => list.filter(other => other.id !== id))
      setToast(`Not cancelled: ${errorText(error)}`)
    }
  }
  // Stop before Claude replies: Claude drops the turn and puts the message (and any queued ones) back in its input box.
  // The server clears that box and returns the text, so it lands in this message box instead of blocking the next send.
  async function stop() {
    if (activePane.agent !== 'claude') { void sendKey(activePane.agent === 'shell' ? 'C-c' : 'Escape'); return }
    if (acting) return
    setActing(true); setSendError(''); setDraftBlocked(false)
    try {
      const { restored } = await api<{ restored?: string }>('key', { session: pane.id, thread: data?.thread ?? null, key: 'Escape', stop: true })
      if (restored) {
        const text = expandPlaceholders(restored, [...sentPrompts, ...pending.map(item => item.text)])
        setPending(list => list.filter(other => other.failed))
        setDraft(draft.trim() ? `${text}\n\n${draft}` : text)
        setToast('Stopped. Your message is back in the box.')
      }
      await refresh(); onUpdate()
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { await refresh(); setToast('The thread changed. Review the chat, then try again.') }
      else { setSendError(errorText(error)); setDraftBlocked(error instanceof ApiError && error.draft) }
    } finally { setActing(false) }
  }
  // While Claude asks a question, the message box answers it through its own-answer row (checked on the server).
  const ownAnswer = !data?.approval && data?.attention?.reason === 'menu' ? data.attention.options?.find(option => option.free) : undefined
  async function send(text: string, retryOf?: Pending) {
    text = terminalText(text)
    // Claude shows these as a terminal panel that a phone sees only a slice of. Show Tether's own sheet instead, as T3 Code does.
    if (!retryOf && activePane.agent === 'claude' && /^\/(usage|cost|stats|status)$/.test(text.trim())) { setPlanSheet(true); return true }
    if (ownAnswer && !retryOf) return write('menu', { session: pane.id, thread: data?.thread ?? null, choice: ownAnswer.id, answer: text })
    const key = squash(text).slice(0, 80)
    const seen = retryOf?.seen ?? matches(messages, key, imagesOnly(text))
    const accepted = await write('message', { session: pane.id, thread: data?.thread ?? null, message: text, ...(activePane.agent === 'shell' ? { mode: 'shell' } : {}) })
    const failure = lastFailure.current
    // No answer from the server (offline, timeout, server down): keep the message as a "Not sent" bubble with Retry, and clear the box.
    const lost = !accepted && !!failure && (!(failure instanceof ApiError) || (failure.status >= 500 && !failure.draft))
    if (lost) setSendError('')
    if ((accepted || lost) && activePane.agent !== 'shell' && key) {
      // eslint-disable-next-line react-hooks/purity -- The runtime invokes this send handler on message submission; these timestamps follow the awaited write, never render.
      const item = { id: retryOf?.id ?? Date.now(), text, key, seen, thread: retryOf?.thread ?? data?.thread ?? null, at: retryOf?.at ?? Date.now(), ...(lost ? { failed: errorText(failure), offline: !(failure instanceof ApiError) } : {}) }
      setPending(list => retryOf ? list.map(other => other.id === retryOf.id ? item : other) : [...list, item])
    }
    if (accepted || lost) { sticky.current = true; setJump(false); hold.current = null; requestAnimationFrame(() => requestAnimationFrame(() => viewport.current?.scrollTo({ top: viewport.current.scrollHeight }))) }  // after the bubble renders
    return accepted || (lost && activePane.agent !== 'shell')
  }
  // Retry a "Not sent" bubble. First reload the chat: if the message did arrive after all, the bubble just becomes a normal one.
  const [retrying, setRetrying] = useState<number | null>(null)
  async function retry(item: Pending) {
    if (retrying !== null) return
    setRetrying(item.id)
    try {
      const fresh = await api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=${limit}`).catch(() => null)
      if (fresh && Array.isArray(fresh.messages) && matches(fresh.messages as ChatMessage[], item.key) > item.seen) {
        setPending(list => list.filter(other => other.id !== item.id)); void refresh(); return
      }
      await send(item.text, item)
    } finally { setRetrying(null) }
  }
  function editFailed(item: Pending) {
    setPending(list => list.filter(other => other.id !== item.id))
    setDraft(draft.trim() ? `${draft.trimEnd()}\n${item.text}` : item.text)
  }
  // A tap on the bar with your messages: put that message just under the bar and flash it.
  function revealMine(id: string) {
    const view = viewport.current, element = view?.querySelector<HTMLElement>(`[data-msg-id="${CSS.escape(id)}"]`)
    if (!view || !element) return
    sticky.current = false; setJump(true)
    view.scrollTop += element.getBoundingClientRect().top - view.getBoundingClientRect().top - MINE_BAR_SPACE - 4
    hold.current = { top: view.scrollTop, until: Date.now() + 1500 }
    element.classList.add('search-hit')
    setTimeout(() => element.classList.remove('search-hit'), 2400)
  }
  const started = startedLabel(data?.started)
  function loadEarlier() {
    if (!data?.earlier || data.limit !== limit) return
    const view = viewport.current
    if (view) anchor.current = { height: view.scrollHeight, top: view.scrollTop }
    setLimit(value => value + 40)
  }
  // Claude's bottom strip, read every 5 seconds while this page is visible.
  const footerId = footerKey(pane.id, data ? data.thread : pane.thread)
  const [claudeFooter, setFooter] = useState<{ key: string; value: Footer }>(() => ({ key: footerId, value: footerCache.get(footerId) ?? {} }))
  const footer = activePane.agent === 'codex' ? data?.footer ?? {} : claudeFooter.key === footerId ? claudeFooter.value : footerCache.get(footerId) ?? {}
  const [usageSheet, setUsageSheet] = useState(false)
  const [agentMap, setAgentMap] = useState(false)
  const subAgents = useSubAgents(api, activePane.id, activePane.agent === 'claude')
  const [planSheet, setPlanSheet] = useState(false)
  const [modelSheet, setModelSheet] = useState(false)
  const [continueSheet, setContinueSheet] = useState(false)
  const [modeMenu, setModeMenu] = useState(false)
  useBackStep(modeMenu, () => setModeMenu(false))
  const [modeBusy, setModeBusy] = useState('')
  const [info, setInfo] = useState(false)
  // A media path the agent named in a reply. Ask the server first, so a refused or missing file shows its reason, not a broken image.
  const [opened, setOpened] = useState<Media | null>(null)
  const [textFile, setTextFile] = useState<TextFile | null>(null)
  const [officeFile, setOfficeFile] = useState<OfficeFile | null>(null)
  const openMedia = useCallback(async (path: string, kind: Kind, line?: number) => {
    const url = `/mobile/api/file?session=${encodeURIComponent(activePane.id)}&path=${encodeURIComponent(path)}`
    const name = path.split('/').pop() || path
    if (kind === 'text') { setTextFile({ path, url, line }); return }
    try {
      const check = await fetch(url, { headers: { Range: 'bytes=0-0' } })
      if (!check.ok) { const body = await check.json().catch(() => null) as { error?: string } | null; setOpened({ name, url, error: body?.error ?? 'Tether cannot open this file.' }); return }
      void check.body?.cancel()
    } catch { setOpened({ name, url, error: `Cannot reach ${host}.` }); return }
    if (kind === 'office') setOfficeFile({ path, url })
    else if (kind === 'file') saveFile(url, name)
    else if (kind === 'pdf') window.open(url, '_blank', 'noopener')
    else setOpened({ name: path, url, kind })
  }, [activePane.id, host, setTextFile, setOfficeFile, setOpened])
  // Desktop: "Show in Files" opens the explorer at the file's folder with the file selected. A relative path is in the pane's folder.
  const showInFiles = (path: string | null, close: () => void) => {
    if (!path || typeof matchMedia !== 'function' || !matchMedia(WORKSPACE_QUERY).matches) return undefined
    const full = /^[~/]/.test(path) ? path : `${(activePane.dir || '~').replace(/\/$/, '')}/${path.replace(/^\.\//, '')}`
    return () => { close(); window.dispatchEvent(new CustomEvent('tether:open-files', { detail: { path: full.slice(0, full.lastIndexOf('/')) || '/', select: full } })) }
  }
  const readFooter = useRef<() => void>(() => {})
  const footerReads = useRef({ requested: 0, applied: 0 })
  const footerReread = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (activePane.agent !== 'claude') return
    let live = true
    const read = () => {
      if (!live || document.visibilityState !== 'visible') return
      const sequence = ++footerReads.current.requested
      api<Footer>(`footer?session=${encodeURIComponent(pane.id)}`).then(result => {
        if (!live || sequence < footerReads.current.applied) return
        footerReads.current.applied = sequence
        // No input box on screen (a dialog, a redraw): keep the chips, but not a card whose keys could reach the dialog.
        const next = result.observed === false ? { ...(footerCache.get(footerId) ?? {}), heads_up: null, suggestion: '' } : result
        cacheFooter(footerId, next)
        setFooter({ key: footerId, value: next })
      }, () => {})
    }
    readFooter.current = read
    read()
    const timer = setInterval(read, state === 'Working' ? 1000 : 5000)  // while Claude works, its verb, tokens and tool change every second
    return () => {
      live = false; clearInterval(timer)
      readFooter.current = () => {}
      if (footerReread.current !== null) clearTimeout(footerReread.current)
      footerReread.current = null
    }
  }, [api, pane.id, activePane.agent, state, footerId])
  async function headsUpKey(key: string) {
    const read = readFooter.current
    const ok = await sendKey(key, true)
    if (read === readFooter.current) {
      read()
      if (footerReread.current !== null) clearTimeout(footerReread.current)
      footerReread.current = setTimeout(read, 700)
    }
    if (!ok) { cardError.current = true; throw new Error('not taken') }
  }
  // Claude: the model from its status-line JSON; a custom status line's first part when Tether's status line is not set.
  const model = footer.model || (activePane.agent === 'codex' ? '' : footer.status?.split('|')[0].trim())
  const context = footer.context ? String(Math.round(footer.context.percent)) : /ctx\s+(\d+)%/.exec(footer.status ?? '')?.[1]
  const currentMode = modeBusy || modeKey(footer.mode)
  async function chooseMode(target: string) {
    setModeMenu(false)
    if (target === modeKey(footer.mode)) return
    setModeBusy(target)
    try { await api('mode', { session: pane.id, thread: data?.thread ?? null, mode: target }) } catch (failure) { setToast(`Mode not changed: ${errorText(failure)}`) }
    readFooter.current(); setModeBusy('')
  }
  const background = (footer.agents ?? []).filter(agent => !agent.main)
  const codexRuns = useCodexRuns(api, activePane.id, activePane.agent !== 'shell')
  // A card key that Claude did not take leaves its error under the message box. Clear it when the card changes or closes.
  const card = footer.heads_up ? `${footer.heads_up.state}|${footer.heads_up.text}` : ''
  useEffect(() => { if (cardError.current) { cardError.current = false; setSendError(''); setDraftBlocked(false) } }, [card, pane.id])
  async function sendKey(key: string, card = false) {
    if (acting) return false
    setActing(true); setKeysOpen(false)
    try { return await write('key', { session: pane.id, thread: data?.thread ?? null, key, ...(card ? { card: true } : {}) }) } finally { setActing(false) }
  }
  // Open Claude's /permissions dialog. No queued bubble: the dialog opens over the chat, even while Claude works.
  function openPermissions(tab: string | null) {
    setPermissionsTab(tab)
    return write('message', { session: pane.id, thread: data?.thread ?? null, message: '/permissions' })
  }
  // A key from a read-only panel's footer: the server resolves the action id, checks the screen again, then sends one key.
  // A refusal (the panel changed) returns its sentence to show beside the panel; the next poll shows the new panel.
  async function promptAction(attention: Attention, action: PromptAction) {
    if (acting) return null
    setActing(true)
    try {
      await api('prompt-action', { session: pane.id, thread: data?.thread ?? null, promptId: attention.promptId, expectedRevision: attention.revision, actionId: action.id, requestId: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}` })
      return null
    } catch (failure) { return errorText(failure) }
    finally { await refresh(); setActing(false) }
  }
  // A bridged Claude question: the answers go to the waiting hook as data. null gives the question back to the terminal.
  // The phone person is using the form: keep the question here (the server extends the deadline). Returns the new deadline.
  async function askEngage(ask: AskBridge) {
    try { return (await api<{ expires: number }>('ask-engage', { session: pane.id, id: ask.id, nonce: ask.nonce })).expires } catch { return null }
  }
  async function askAnswer(ask: AskBridge, answers: Record<string, string | string[]> | null) {
    if (acting) return null
    setActing(true)
    try {
      await api('ask-answer', { session: pane.id, id: ask.id, nonce: ask.nonce, answers, requestId: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}` })
      return null
    } catch (failure) { return errorText(failure) }
    finally { await refresh(); setActing(false) }
  }
  // Clears the text in the agent's own input box (for example digits that a card did not take).
  async function clearAgentInput() {
    if (acting) return
    setActing(true)
    try { if (await write('clear-draft', { session: pane.id, thread: data?.thread ?? null })) setToast('Cleared the input box in the terminal.') } finally { setActing(false) }
  }
  // An approval goes through the checked route (it confirms the cursor before Enter); a plain menu takes the option's number key.
  async function choose(kind: 'approval' | 'menu', option: Choice, approval: Approval | null, feedback?: string) {
    if (acting) return
    setActing(true)
    try {
      if (kind === 'approval' && approval?.id) await write('approval', { session: pane.id, thread: data?.thread ?? null, approval: approval.id, choice: option.id, feedback })
      // Arrows plus Enter, checked on the screen: a digit key types text when the cursor sits on the own-answer row.
      else await write('menu', { session: pane.id, thread: data?.thread ?? null, choice: option.id, label: option.text })
    } finally { setActing(false) }
  }
  async function start(agent: 'claude' | 'codex') {
    if (acting) return
    setActing(true)
    try { await write('start', { session: pane.id, thread: data?.thread ?? null, agent }) } finally { setActing(false) }
  }
  // Copy, read aloud and share belong to a finished reply: only the last text of each turn gets them, and not while the agent still works.
  const turnEnds = useMemo(() => turnEndsOf(messages, state === 'Working'), [messages, state])
  const liveGroup = useMemo(() => liveGroupOf(messages, state === 'Working'), [messages, state])
  const folded = useMemo(() => foldWatchCycles(messages), [messages])
  const runtime = useExternalStoreRuntime({ messages: folded, convertMessage, isRunning: state === 'Working', onNew: async message => { await send(message.content.filter(part => part.type === 'text').map(part => part.text).join('\n')) } })
  // When this phone first saw the pane working. Claude prints its own timer in the spinner detail.
  const [workingSince, setWorkingSince] = useState<{ pane: string; at: number } | null>(null)
  // eslint-disable-next-line react-hooks/set-state-in-effect -- Capture the committed working transition's timestamp; deriving it during render would read the clock there.
  useEffect(() => { setWorkingSince(current => state !== 'Working' ? null : current?.pane === pane.id ? current : { pane: pane.id, at: Date.now() }) }, [state, pane.id])
  // A turn this page watched from its start ends: "Done in 2m 14s" with a check, for a few seconds. Short turns (under 10 s) say nothing.
  const [doneIn, setDoneIn] = useState<{ pane: string; seconds: number } | null>(null)
  const lastRun = useRef<{ pane: string; at: number; ended?: number } | null>(null)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Clear the completion notice in the same effect that records the run and its six-second expiry.
    if (workingSince) { lastRun.current = { ...workingSince }; setDoneIn(null); return }
    const run = lastRun.current
    if (!run || run.pane !== pane.id) { setDoneIn(null); return }
    run.ended ??= Date.now()
    const left = 6000 - (Date.now() - run.ended)
    if (left <= 0 || run.ended - run.at < 10000) return
    setDoneIn({ pane: pane.id, seconds: (run.ended - run.at) / 1000 })
    const timer = window.setTimeout(() => setDoneIn(null), left)
    return () => window.clearTimeout(timer)
  }, [workingSince, pane.id])
  function startWith(text: string) {
    setDraft(text)
    requestAnimationFrame(() => { const box = panelRef.current?.querySelector<HTMLTextAreaElement>('.composer textarea'); if (box) { box.focus(); box.setSelectionRange(text.length, text.length) } })
  }
  // A hidden notice stays hidden while the same text shows in this pane; a new notice shows again.
  const [noticeConfirm, setNoticeConfirm] = useState<{ key: string; until: number } | null>(null)
  useEffect(() => {
    if (!noticeConfirm) return
    const timer = window.setTimeout(() => setNoticeConfirm(null), Math.max(0, noticeConfirm.until - Date.now()))
    return () => window.clearTimeout(timer)
  }, [noticeConfirm])
  const [hiddenNotices, setHiddenNotices] = useState<string[]>([])
  const visibleNotices = (footer.notices ?? []).filter(note => !hiddenNotices.includes(note))
  const hideNotice = (note: string) => setHiddenNotices(current => [...current.filter(item => (footer.notices ?? []).includes(item)), note])
  return <AssistantRuntimeProvider runtime={runtime}><MediaOpener.Provider value={openMedia}><FilePane.Provider value={activePane.id}><section ref={panelRef} className={activePane.agent === 'shell' && !term ? 'chat-panel shell-page' : 'chat-panel'} aria-label={`Chat with ${pane.label}`}>
    <header className="chat-header" draggable={!!slot?.onDragStart} onDragStart={slot?.onDragStart} onTouchStart={event => {
      headerTouch.current = sidePin?.pinned && sidePin.total > 1 && event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null
    }} onTouchEnd={event => {
      const start = headerTouch.current, end = event.changedTouches[0]
      headerTouch.current = null
      if (!start || !end || !sidePin) return
      const dx = end.clientX - start.x, dy = end.clientY - start.y
      if (Math.abs(dx) >= 50 && Math.abs(dy) < 0.6 * Math.abs(dx)) sidePin.onStep(dx < 0 ? 1 : -1)
    }} onTouchCancel={() => { headerTouch.current = null }}><button className="icon-button mobile-menu" onClick={onMenu} aria-label={running ? `Open sessions. ${running} agents running or waiting` : 'Open sessions'}><Menu />{running > 0 && <span className="menu-count" aria-hidden="true">{running}</span>}</button>{slot && <kbd className="slot-badge" title={`Focus this slot: Alt+${slot.index + 1}`}>⌥{slot.index + 1}</kbd>}<button type="button" className="chat-title" aria-label="Session details" onClick={() => setInfo(true)}><h1>{chatTitle(pane, session)}</h1><div>{grouped && session && <><bdi className="group-crumb" title={`${session.name} › ${groupName}`}>{session.name} › {groupName}</bdi><span className="header-separator">·</span></>}<AgentIcon agent={activePane.agent} icon={activePane.pane_icon} /><span className="agent-word">{agentNames[activePane.agent]}</span>{activePane.agent === 'codex' && pane.codex_profile && <span className="provider-badge" title={`Runs on codex -p ${pane.codex_profile}`}>{profileName(pane.codex_profile)}</span>}<span className="header-separator agent-word">·</span><State state={state} />{context && <><span className="header-separator">·</span><span className={Number(context) >= 80 ? 'context-high' : ''}>{context}%<span className="context-word"> context</span></span></>}{activePane.worktree && <><span className="header-separator">·</span><WorktreeMark branch={activePane.worktree} /></>}{activePane.dir && !activePane.worktree && <><span className="header-separator">·</span><bdi className="header-folder" dir="ltr" title={activePane.dir} aria-label={`Directory: ${activePane.dir}`}>{activePane.dir.split('/').filter(Boolean).pop() || '/'}</bdi></>}{started && <span className="header-started" title={`Chat started ${started}`} aria-label={`Started ${started}`}><Clock3 size={12} aria-hidden="true" />{started}</span>}{sidePin?.pinned && <PinMark pin={sidePin} />}</div></button>
      {headerExtra}
      {grouped && panes.length <= 1 && <button type="button" className="icon-button group-new-tab" aria-label={`New tab in ${groupName}`} title={`New tab in ${groupName}`} onClick={() => onSplit('right')}><Plus size={20} /></button>}
      <ReviewLinks api={api} pane={pane} panes={allPanes} onOpen={onOpenPane} onChanged={onUpdate} onToast={setToast} />
      {sidePin && activePane.agent !== 'shell' && <button type="button" className="icon-button side-pin-toggle" aria-pressed={sidePin.pinned} aria-label={sidePin.pinned ? 'Unpin from side tab' : 'Pin to side tab'} title={sidePin.pinned ? 'Unpin from side tab' : 'Pin to side tab'} onClick={sidePin.onToggle}>{sidePin.pinned ? <PinOff size={20} /> : <Pin size={20} />}</button>}
      <button type="button" className="icon-button search-toggle" aria-label="Search this chat" title="Search this chat" disabled={!data} onClick={() => setSearching(true)}><Search size={20} /></button>
      <button type="button" className={term ? 'icon-button terminal-toggle active' : 'icon-button terminal-toggle'} aria-label={term ? 'Back to chat' : 'Open terminal'} aria-pressed={term} title={term ? 'Back to chat' : 'Open terminal'} onClick={() => setTerm(!term)}>{term ? <MessageSquare size={20} /> : <SquareTerminal size={20} />}</button><div className="keys-container" ref={keysRef} onKeyDown={event => { if (event.key === 'Escape') { setKeysOpen(false); keysRef.current?.querySelector('button')?.focus() } }}><button className="icon-button" aria-label={`Session, ${words.pane} and keys`} aria-expanded={keysOpen} disabled={!data} onClick={() => setKeysOpen(!keysOpen)}><MoreHorizontal /></button>{keysOpen && <div className="keys-menu" aria-label="Session and keys"><button onClick={() => { setKeysOpen(false); onSessionActions(menuAnchor()) }}><Settings2 size={16} />Session options</button><button onClick={() => { setKeysOpen(false); onPaneActions(activePane.id, menuAnchor()) }}><PanelTop size={16} />{words.Pane} options</button><MuteItem api={api} pane={activePane.id} />{sidePin && activePane.agent !== 'shell' && <button onClick={() => { setKeysOpen(false); sidePin.onToggle() }}>{sidePin.pinned ? <PinOff size={16} /> : <Pin size={16} />}{sidePin.pinned ? 'Unpin from side tab' : 'Pin to side tab'}</button>}{activePane.agent === 'claude' && data && data.claude_name === null && claudeName && <button disabled={!writable || acting} onClick={() => { setKeysOpen(false); void send(`/rename ${claudeName}`) }}><Pencil size={16} />Name Claude “{claudeName}”</button>}{activePane.agent !== 'shell' && <button onClick={() => { setKeysOpen(false); setTools(true) }}><Blocks size={16} />Skills, plugins, MCP</button>}<button onClick={() => { setKeysOpen(false); onSplit('right') }}><Columns2 size={16} />{words.newRight}{words.tmux && <kbd>%</kbd>}</button><button onClick={() => { setKeysOpen(false); onSplit('down') }}><Rows2 size={16} />{words.newBelow}{words.tmux && <kbd>"</kbd>}</button>{canFork(activePane) && <button onClick={() => { setKeysOpen(false); onFork() }}><GitFork size={16} />New chat from this</button>}<span>Send a key</span>{['Escape', 'C-c', 'Enter', 'Up', 'Down', 'Tab', 'y', 'n'].map(key => <button key={key} disabled={!writable || acting} onClick={() => void sendKey(key)}>{key === 'Escape' ? 'Esc' : key === 'C-c' ? 'Ctrl+C' : key}<kbd>{key === 'Up' ? '↑' : key === 'Down' ? '↓' : key === 'Enter' ? '↵' : key === 'Tab' ? '⇥' : key === 'C-c' ? '^C' : key === 'Escape' ? 'esc' : key}</kbd></button>)}</div>}</div>
      {slot?.onPick && <button type="button" className="icon-button slot-button" aria-label={`Change what slot ${slot.index + 1} shows`} title="Show another pane or session here" onClick={slot.onPick}><ArrowLeftRight size={16} /></button>}
      {slot && <button type="button" className="icon-button slot-button" aria-label={slot.zoomed ? 'Show all slots (Alt+Z)' : 'Zoom this slot (Alt+Z)'} title={slot.zoomed ? 'Show all slots (Alt+Z)' : 'Zoom this slot (Alt+Z)'} onClick={slot.onZoom}>{slot.zoomed ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>}
      {slot?.onClose && <button type="button" className="icon-button slot-button" aria-label="Close this slot (Alt+X)" title="Close this slot (Alt+X)" onClick={slot.onClose}><X size={16} /></button>}
    </header>
    {session && view && (grouped && groupHooks
      ? <GroupBar api={api} session={session} pane={pane} hooks={groupHooks} onOpenPane={onOpenPane} onUpdate={onUpdate}>
          {panes.length > 1 && <PaneTabs ref={tabs} session={view} pane={pane} onOpenPane={onOpenPane} onPaneActions={onPaneActions} onSplit={onSplit} onReorder={onReorder} onDropGroup={onDropGroup} api={api} onUpdate={onUpdate} micOk={micOk} chime={chime} onNotice={setToast} />}
        </GroupBar>
      : <PaneTabs ref={tabs} session={session} pane={pane} onOpenPane={onOpenPane} onPaneActions={onPaneActions} onSplit={onSplit} onReorder={onReorder} onDropGroup={onDropGroup} api={api} onUpdate={onUpdate} micOk={micOk} chime={chime} onNotice={setToast} />)}
    {term && data?.attention?.askBridge && <AskBar agent={agentNames[activePane.agent]} disabled={!writable || acting} onOpen={() => setTerm(false)} onTerminal={() => askAnswer(data.attention!.askBridge!, null)} />}
    {term ? <LiveTerminal paneId={activePane.id} onChat={() => setTerm(false)} /> : <>
    {(offline || (error && data)) && <div className="reconnect-banner" role="status"><WifiOff size={15} /><span>{offline ? 'You are offline. Tether reconnects when the network is back.' : `Cannot reach ${host}. Reconnecting…`}</span><RetryButton label="Retry now" className="" onRetry={refresh} /></div>}
    {data && activePane.agent === 'shell' && <div className="start-options"><button disabled={!writable || acting} onClick={() => void start('claude')}>Start Claude<ChevronRight size={16} /></button><button disabled={!writable || acting} onClick={() => void start('codex')}><span>Start Codex<FoundryTag api={api} /></span><ChevronRight size={16} /></button></div>}
    <div className="chat-pager"><div className="chat-page" ref={page}>
    <ThreadPrimitive.Root className={opening ? 'thread opening' : 'thread'}><div className="chat-scroll" ref={viewport} onTouchStart={swipeStart} onTouchMove={event => { pullMove(event); swipeMove(event) }} onTouchEnd={swipeEnd} onTouchCancel={swipeCancel} onScroll={() => {
      const view = viewport.current
      if (view && hold.current && Date.now() < hold.current.until && Math.abs(view.scrollTop - hold.current.top) > 2) return
      if (view) { hold.current = null; sticky.current = view.scrollHeight - view.scrollTop - view.clientHeight < 90; setJump(!sticky.current); scrollByPane.set(pane.id, { top: view.scrollTop, limit, bottom: sticky.current }) }
    }}><div className="messages" ref={content}>
      {error && !data && <div className="inline-error" role="alert">{error}<RetryButton label="Retry" className="" onRetry={refresh} /></div>}
      {loading && <Skeleton label="Loading conversation…" className="sk-chat">
        <Bone className="sk-ask" /><div className="sk-answer"><span><Bone w="94%" /></span><span><Bone w="88%" /></span><span><Bone w="61%" /></span></div>
        <Bone className="sk-ask" w="32%" /><div className="sk-answer"><span><Bone w="90%" /></span><span><Bone w="47%" /></span></div>
      </Skeleton>}
      <p ref={pullNote} className="pull-note" role="status" aria-hidden={pulled === 0}>{pulled >= 70 ? 'Release to load earlier' : 'Pull to load earlier'}</p>
      {started && !data?.earlier && activePane.agent !== 'shell' && <p className="chat-started">Chat started {started}</p>}
      {!!data?.earlier && <button className="load-earlier" disabled={data.limit !== limit} onClick={loadEarlier}>{data.limit !== limit ? <><Spinner /> Loading…</> : `Load earlier · ${data.earlier}`}</button>}
      {data && activePane.agent !== 'shell' && !messages.length && <div className="chat-empty"><Terminal size={27} strokeWidth={1.4} /><h2>No messages yet</h2><p>{`Send a message to ${pane.label} to get started.`}</p>
        {writable && <div className="starter-actions" aria-label="Ways to start">
          <button type="button" onClick={() => startWith('Explain what this project does and how its files are laid out.')}><MessageSquare size={16} />Explain this project</button>
          <button type="button" onClick={() => startWith('!git status --short --branch')}><Terminal size={16} />Check git status</button>
          <button type="button" onClick={() => startWith('/')}><Blocks size={16} />Commands & skills</button>
        </div>}</div>}

      {activePane.agent === 'shell' ? <ShellOutput key={activePane.id} folder={activePane.dir} text={messages.map(message => message.text).join('\n')} /> : <ThreadPrimitive.Messages>{({ message }) => <Message api={api} session={activePane.id} live={message.id === liveGroup} runningShown={!!footer.running} actions={turnEnds.has(message.id)} tools={message.metadata.custom.tools as ToolSummary[] | undefined} id={message.id} role={message.role} kind={message.metadata.custom.kind} title={message.metadata.custom.title ? String(message.metadata.custom.title) : undefined} count={message.metadata.custom.count} text={String(message.metadata.custom.text ?? '')} time={String(message.metadata.custom.time ?? '')} images={message.metadata.custom.images as number | undefined} imageKey={message.metadata.custom.imageKey as string | undefined} watch={message.metadata.custom.watch as WatchRun | undefined} />}</ThreadPrimitive.Messages>}
      {bang && <><div className="user-message"><span>{bangItem?.text ?? `!${bang.command}`}</span></div>
        <div className="assistant-message bang-run" role="status"><small><Terminal size={12} />Running{bang.seconds != null ? ` · ${bang.seconds}s` : ''}<button type="button" className="bang-stop" aria-label="Stop this command" disabled={!writable || acting} onClick={() => void sendKey('C-c')}><X size={13} />Stop</button></small><pre>{bang.output ? <Linked text={bang.output} /> : 'No output yet'}</pre></div></>}
      {state === 'Working' && preview && <div className="assistant-message reply-preview"><small>typing</small><div>{preview}</div></div>}
      {askBar}
    </div></div>{jump && <button className="jump-button icon-button" aria-label="Jump to latest message" onClick={() => { sticky.current = true; setJump(false); viewport.current?.scrollTo({ top: viewport.current.scrollHeight }) }}><ArrowDown size={20} /></button>}<SelectionCopy messages={messages} onBacklog={onBacklogAdd && (text => onBacklogAdd(text, pane))} />{data && activePane.agent !== 'shell' && <MineBar view={viewport} messages={messages} earlier={data.limit === limit ? data.earlier : 0} onReveal={revealMine} onEarlier={loadEarlier} />}</ThreadPrimitive.Root>
    {searching && <SearchSheet api={api} pane={activePane} shell={activePane.agent === 'shell'} onClose={() => setSearching(false)} onPick={(match, maxLimit) => {
      setSearching(false); setTerm(false); target.current = match.id; sticky.current = false; anchor.current = null
      const need = Math.min(maxLimit, Math.max(limit, match.from_end + 10))
      if (need !== limit) setLimit(need); else void refresh()
    }} />}
    {liveSheet && <LiveSheet api={api} pane={activePane} show={liveSheet} taskDetails={footer.task_details} onClose={() => setLiveSheet(null)} onTerminal={() => setTerm(true)} />}
    {tools && <Tools api={api} pane={activePane} onClose={() => setTools(false)} onPick={text => { setDraft(text); setTools(false) }} />}
    {data?.login_result && <p className="login-result" role={data.login_result.phase === 'failed' ? 'alert' : 'status'}>{data.login_result.message}</p>}
    {state !== 'Working' && doneIn?.pane === pane.id && <div className="working-line done-line" role="status"><span className="done-check"><Check size={12} strokeWidth={3} /></span><span><strong>Done in {formatElapsed(doneIn.seconds)}</strong></span></div>}
    {state === 'Stopped' && <div className="working-line" role="status"><span className="state-dot" aria-hidden="true" /><span><strong>Stopped by a restart</strong><small>Send a message to continue.</small></span></div>}
    {state === 'Working' && <div className="working-line" role="status"><WorkGlyph /><span><span className="working-head"><strong className="work-verb">{footer.spinner?.verb ?? 'Working…'}<span className="work-glint" aria-hidden="true"><span>{footer.spinner?.verb ?? 'Working…'}</span></span></strong>{footer.spinner?.detail ? <small>{footer.spinner.detail}</small> : workingSince && <Elapsed since={workingSince.at} />}</span>{footer.running && <span className="running-tool"><Terminal size={13} /><span><span className="running-title">{footer.running.title}{footer.running.elapsed && <small> · {footer.running.elapsed}</small>}</span>{footer.running.command && footer.running.command !== footer.running.title && <code>{footer.running.command}</code>}</span></span>}{footer.denied && <button type="button" className="running-tool denied-tool" disabled={!writable || acting} title="Open /permissions, tab Recently blocked" onClick={() => void openPermissions('Recently blocked')}><ShieldAlert size={13} /><span>{footer.denied.text}{footer.denied.reason && <small>{footer.denied.reason}</small>}</span><span className="denied-review">Review</span></button>}</span></div>}
    <StatusStack priorityKey={data?.approval ? `approval:${(data.approval as Approval).id}` : data?.attention ? `attention:${data.attention.id}` : data?.plan ? `plan:${data.plan.id}` : footer.heads_up ? 'heads' : browserAsk ? `browser-ask:${browserAsk.id}` : ''} chips={(footer.goal || !!footer.tasks?.length || (footer.suggestion && !draft.trim() && state !== 'Working')) ? <>{(footer.goal || !!footer.tasks?.length) ? <div className="live-chips">{footer.goal && <button type="button" className="live-chip" aria-haspopup="dialog" onClick={() => setLiveSheet('goal')}><Target size={14} />{footer.goal_status ? `Goal · ${goalStatusLabel(footer.goal_status)}` : `Goal active${footer.goal !== 'active' ? ` · ${footer.goal}` : ''}`}<ChevronRight size={13} /></button>}{footer.tasks?.map(task => <button type="button" key={task} className="live-chip" aria-haspopup="dialog" onClick={() => setLiveSheet('tasks')}><Layers size={14} /><span className="live-chip-text">{taskLabel(task, footer.task_details)}</span><ChevronRight size={13} /></button>)}</div> : null}{(footer.suggestion && !draft.trim() && state !== 'Working' ? <button type="button" className="prompt-suggestion" title="Use this suggested prompt" onClick={() => setDraft(footer.suggestion ?? '')}><CornerDownRight size={15} /><span>{footer.suggestion}</span></button> : null)}</> : null} items={[
      { id: 'action', label: askLabel ?? (data?.plan ? 'Review plan' : data?.approval ? `${agentNames[activePane.agent]} needs permission` : data?.attention?.reason === 'permissions' ? 'Permissions' : data?.attention?.reason === 'panel' || data?.attention?.reason === 'info' ? `${agentNames[activePane.agent]} panel open` : `${agentNames[activePane.agent]} needs attention`), active: !!(data?.approval || data?.attention || data?.plan), content: <>{data && (data.approval != null || data.attention || data.plan) && <ChoiceDock key={(data.approval as Approval | null)?.id ?? data.attention?.id ?? 'choice'} approval={data.approval as Approval | null} attention={data.attention ?? null} plan={data.plan} agent={agentNames[activePane.agent]} disabled={!writable || acting} since={askSince} onChoose={choose} onKey={key => sendKey(key)} permissionsTab={permissionsTab} onAction={action => promptAction(data.attention!, action)} onAsk={askAnswer} onAskEngage={askEngage} onType={(text, loginUrl) => write('type', { session: activePane.id, thread: data?.thread ?? null, text, enter: true, ...loginUrl ? { login_url: loginUrl } : {} })} />}</> },
      { id: 'heads', label: '✦ Heads up', active: !!footer.heads_up, content: <>{footer.heads_up && <HeadsUp card={footer.heads_up} disabled={!writable || acting} onKey={headsUpKey} onChat={note => { setDraft(note); requestAnimationFrame(() => { const box = panelRef.current?.querySelector<HTMLTextAreaElement>('.composer textarea'); box?.focus(); box?.setSelectionRange(box.value.length, box.value.length) }) }} />}</> },
      { id: 'agents', label: `${background.length} agents running`, active: background.length > 0, content: <>{background.length > 0 && <div className="agents-strip"><ul>{background.map((agent, index) => <li key={index}><strong>{agent.name}</strong><span>{agent.task}</span><small>{agent.detail}</small></li>)}</ul></div>}</> },
      { id: 'queue', label: `${queued.length} queued${queued.some(item => item.failed) ? ' · Not sent' : ''}`, active: queued.length > 0, content: <>      {queued.map(item => item.failed ? <div key={item.id} className="user-message pending-message failed-message"><span>{item.text}</span><small role="alert">{item.offline ? <><WifiOff size={12} />Not sent</> : <span className="failed-reason">Not sent: {item.failed}</span>}
        <button type="button" className="failed-action" disabled={retrying !== null || !writable} onClick={() => void retry(item)}><RotateCcw size={13} />{retrying === item.id ? 'Sending…' : 'Retry'}</button>
        <button type="button" className="failed-action" onClick={() => editFailed(item)}><Pencil size={13} />Edit</button>
        <button type="button" aria-label="Delete this unsent message" onClick={() => setPending(list => list.filter(other => other.id !== item.id))}><X size={12} /></button></small></div>
      : <div key={item.id} className="user-message pending-message"><SentContent {...sentContent(item.text, activePane.id)} /><small><Clock3 size={12} />{state === 'Working' ? 'Queued' : 'Sending…'}<button type="button" aria-label={activePane.agent === 'claude' ? 'Cancel this queued message' : 'Hide this queued message'} onClick={() => void unqueue(item.id)}><X size={12} /></button></small></div>)}</> },
      ...codexRunItems(codexRuns),
      ...agentBrowserItems(api, agentBrowser, activePane.id),
      { id: 'update', label: 'Update ready', active: !!footer.update, content: <>{footer.update && <div className="cli-update" role="status"><RefreshCw size={16} /><span><strong>Claude Code update ready</strong><small>{footer.update.replace(/^[^\w/]+\s*/, '')}</small></span><button type="button" className="secondary" disabled={!writable || state === 'Working'} onClick={() => void send('/restart')}>Restart</button></div>}</> },
    ]} />
    {/* Notice cards and the work line, after T3 Code's banner stack and status line (see Notices.tsx). */}
    <NoticeStack items={[
      ...(data && !writable ? [{ id: 'read-only', group: 'environment' as const, variant: 'info' as const, icon: <Info size={15} />, title: 'This pane is read-only.' }] : []),
      ...(toast ? [{ id: 'toast', icon: <Info size={15} />, title: toast, live: true }] : []),
      ...visibleNotices.map((note): NoticeItem => {
        const base = { id: `notice:${note}`, dismissLabel: `Hide notice: ${note}`, onDismiss: () => hideNotice(note) }
        // Auto mode's safety check stopped a tool call. Say what Claude tried in words; the command waits behind the toggle.
        const block = autoBlock(note, footer.blocked)
        if (block) return { ...base, icon: <Shield size={15} />, title: block.title,
          detail: block.reason ? `Rule: ${block.reason}` : undefined,
          more: block.command ? <code className="notice-command">{block.command}</code> : undefined,
          action: { label: 'Review', hint: 'Open /permissions, tab Recently blocked. You can let Claude retry it once there.', disabled: !writable || acting, onClick: () => void openPermissions('Recently blocked') } }
        const found = noticeAction(note, activePane.agent)
        const confirmKey = `${activePane.id}:${data?.thread ?? ''}:${note}`
        const confirmed = noticeConfirm?.key === confirmKey  // the timer above clears it after 4 s
        const action = found ? {
          label: found.confirm && confirmed ? found.confirm : found.label,
          hint: found.hint, disabled: !writable || acting || state === 'Working',
          onClick: (): void => {
            if (!writable || acting || state === 'Working') return
            if (found.confirm && !confirmed) { setNoticeConfirm({ key: confirmKey, until: Date.now() + 4000 }); return }
            setNoticeConfirm(null)
            void send(found.command)
          },
        } : undefined
        return { ...base, action, variant: (/\b(error|failed|denied|limit)\b/i.test(note) ? 'warning' : 'default') as NoticeVariant, icon: /\b(error|failed|denied|limit)\b/i.test(note) ? <ShieldAlert size={15} /> : <Info size={15} />, title: <Linked text={note} /> }
      }),
    ]} />
    {info && <SessionInfo pane={activePane} footer={footer} mode={MODES[currentMode]} onClose={() => setInfo(false)} />}
    {usageSheet && <Usage footer={footer} onClose={() => setUsageSheet(false)} />}
    {agentMap && <AgentMap api={api} session={activePane.id} list={subAgents.data} onClose={() => setAgentMap(false)} />}
    {planSheet && <PlanUsage api={api} onClose={() => setPlanSheet(false)} />}
    {continueSheet && <ContinueWith api={api} pane={activePane} thread={data?.thread ?? null} idle={state === 'Idle' || state === 'Stopped'} onClose={() => setContinueSheet(false)} onCreated={id => { setContinueSheet(false); onContinued(id) }} />}
    {modelSheet && <ModelSheet onContinue={() => { setModelSheet(false); setContinueSheet(true) }} api={api} pane={activePane} thread={data?.thread ?? null} currentLabel={model} currentEffort={footer.effort} onClose={() => setModelSheet(false)} onDone={message => { setModelSheet(false); setToast(message); readFooter.current() }} />}
    <Composer onTerminalEscape={() => void sendKey('Escape')} options={activePane.agent === 'shell' ? undefined : <span className="composer-pills">
      <button type="button" className="pill" aria-label="Model and effort" disabled={!writable || state === 'Working'} onClick={() => setModelSheet(true)}><span className="pill-text">{model || 'Model'}</span>{footer.effort && <span className="pill-effort">· {footer.effort}</span>}<ChevronDown size={13} /></button>
      <AgentPill list={subAgents.data} onOpen={() => setAgentMap(true)} />
      {activePane.agent === 'codex' && (footer.context || !!footer.windows?.length || creditsLabel(footer.credits)) && <button type="button" className="pill" aria-label="Usage and credits" aria-haspopup="dialog" onClick={() => setUsageSheet(true)}><Gauge size={13} />Usage</button>}
      {activePane.agent === 'claude' && (footer.observed || footer.status || footer.mode) && <span className="mode-container"><button type="button" className="pill" aria-label={`Mode: ${MODES[currentMode]}. Change mode`} aria-expanded={modeMenu} disabled={!writable || !!modeBusy} onClick={() => setModeMenu(!modeMenu)}><Gauge size={13} />{modeBusy ? `${MODES[modeBusy]}…` : MODES[currentMode]}</button>
        {modeMenu && <div className="attach-menu mode-menu" role="menu">{Object.entries(MODES).map(([key, label]) => <button key={key} role="menuitemradio" aria-checked={key === currentMode} type="button" onClick={() => void chooseMode(key)}>{label}{key === currentMode && <Check size={16} />}</button>)}</div>}</span>}
    </span>} api={api} pane={activePane} thread={data?.thread ?? null} disabled={!data || !writable} onSend={send} error={sendError} errorAction={draftBlocked ? <button type="button" disabled={acting} onClick={() => void clearAgentInput()}>Clear {agentNames[activePane.agent]}’s input</button> : null} draft={draft} setDraft={setDraft} working={state === 'Working'} onStop={() => void stop()} micOk={micOk} chime={chime} handsFree={handsFree && readAloud} listenRequest={listenRequest} sent={sentPrompts} onTools={() => setTools(true)} hint={ownAnswer ? `Answer ${agentNames[activePane.agent]}’s question` : undefined} />
    </div>{nextPane && <ChatPeek id={nextPane.id} data={chatCache.get(nextPane.id)} api={api} side={1} peekRef={peekNext} />}{previousPane && <ChatPeek id={previousPane.id} data={chatCache.get(previousPane.id)} api={api} side={-1} peekRef={peekPrevious} />}</div>
    </>}
    <ImageViewer image={opened} close={() => setOpened(null)} onShow={showInFiles(opened && !opened.error ? opened.name : null, () => setOpened(null))} />
    <TextViewer file={textFile} close={() => setTextFile(null)} onShow={showInFiles(textFile?.path ?? null, () => setTextFile(null))} />
    <OfficeViewer file={officeFile} close={() => setOfficeFile(null)} onShow={showInFiles(officeFile?.path ?? null, () => setOfficeFile(null))} />
  </section></FilePane.Provider></MediaOpener.Provider></AssistantRuntimeProvider>
}
