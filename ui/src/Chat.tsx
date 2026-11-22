import { TaskChips } from './TaskChips'
import { TaskNeeds } from './TaskNeeds'
import { taskChildren, taskOf } from './taskTree'
import { RetryButton } from './Retry'
import type { TabPickControls } from './tabSelection'
import { takeBootChat } from './boot'
import { noticeAction } from './noticeActions'
import { terminalText } from './terminalText'
import { ShellOutput } from './ShellOutput'
import { ContinueWith } from './ContinueWith'
import { shellCommand } from './shellCommand'
import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useWords } from './words'
import { ArrowDown, Ban, CalendarClock, Paperclip, ArrowLeftRight, Blocks, GitFork, Check, ChevronDown, ChevronRight, Clock3, Columns2, CornerDownRight, Gauge, Info, Layers, Maximize2, Menu, MessageSquare, Minimize2, MoreHorizontal, PanelTop, Pencil, Pin, PinOff, Redo2, RefreshCw, RotateCcw, Rows2, Search, Server, Settings2, Shield, ShieldAlert, SquareTerminal, Target, Terminal, TriangleAlert, Undo2, WifiOff, X, Zap } from 'lucide-react'
import { requestUndo, useUndoNext } from './undoState'
import { MineBar, MINE_BAR_SPACE, startedLabel } from './MineBar'
import { useTetherZone } from './zone'
import { AssistantRuntimeProvider, ThreadPrimitive, useExternalStoreRuntime } from '@assistant-ui/react'
import { ApiError, canFork, chatMessages, cleanChat, creditsLabel, cronsLabel, cronSummary, errorText, goalStatusLabel } from './api'
import type { AdvisorCall, Api, Approval, AskBridge, Attention, ChatData, ChatMessage, Choice, DialogAction, Footer, Pane, PromptAction } from './api'
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
import { SentContent, sentContent, imagesOnly, uploadImageParts } from './SentImages'
import type { Media } from './ImageViewer'
import { FilePane, MediaOpener, TabOpener, saveFile } from './MediaLink'
import type { Kind } from './MediaLink'
import { TextViewer } from './TextViewer'
import { keepAudioUnlocked, speak, stopSpeaking } from './speech'
import type { TextFile } from './TextViewer'
import { OfficeViewer } from './OfficeViewer'
import type { OfficeFile } from './OfficeViewer'
import type { ToolSummary } from './api'
import { StatusStack } from './StatusStack'
import { PaneTabs, movePill } from './PaneTabs'
import { HEADER, PARENTS, SKIP, STRIPS, boxOf, createStripWheel, scrollsItself } from './stripScroll'
import { scrollSideways } from './wheelSideways'
import { WorktreeMark } from './Worktree'
import { Elapsed, NoticeStack, formatElapsed } from './Notices'
import type { NoticeItem, NoticeVariant } from './Notices'
import { State } from './NewSession'
import { Linked, Message, convertMessage, SelectionCopy } from './Messages'
import { INTERRUPTED } from './injectedPrompt'
import type { WatchRun } from './Messages'
import { foldWatchCycles } from './watchCycles'
import { Composer } from './Composer'
import { ReplyFeedbackProvider, useReplyFeedback } from './ReplyFeedback'
import { dropRefusal, useFileDrop } from './chatDrop'
import { Bone, Skeleton, Spinner } from './Skeleton'
import { expandPlaceholders } from './pasteText'
import { autoBlock } from './noticeText'
import { ChoiceDock, HeadsUp, taskLabel, LiveSheet, SearchSheet } from './Dock'
import { SessionDetails } from './SessionDetails'
import { AskBar } from './ChoiceExtras'
import { pickHeading } from './pickKeys'
import type { PickKey } from './ChoiceExtras'
import { LiveTerminal } from './Terminal'
import { WORKSPACE_QUERY } from './workspace'
import { useHost } from './host'
import { OpenedBy } from './HelperFold'
import { ReviewLinks } from './ReviewLinks'
import { agentBrowserItems, useAgentBrowser } from './AgentBrowser'
import { PreviewDrawer } from './PreviewDrawer'
import { PreviewOpener, artifactsOf, devServersTarget } from './preview'
import type { PreviewTarget } from './preview'
import { codexRunItems, useCodexRuns } from './CodexRuns'
import { CodexTrustCard } from './CodexTrust'
import { useCodexTrust } from './codexTrustState'
import { HOLD_MS } from './gesture'
import { foreignQueued } from './queueMatch'


interface ChatProps { liveReplyPreview: boolean; /** Desktop: a trackpad swipe over the stack row or tab strip scrolls it, not Back/Forward (Settings). */ headerSwipe?: boolean; pane: Pane; session: Session | undefined; api: Api; onMenu: () => void; onUpdate: () => void; draft: string; setDraft: (text: string) => void; onOpenPane: (pane: Pane) => void; onSessionActions: (at?: Anchor) => void; onPaneActions: (pane: string, at?: Anchor) => void; onSplit: (direction: 'right' | 'down') => void; onFork: () => void; onContinued: (id: string) => void; onReorder: (ids: string[]) => void; micOk: boolean; chime: boolean; readAloud: boolean; handsFree: boolean; slot?: SlotControls; running?: number; sidePin?: SidePinControls; allPanes: Pane[]; headerExtra?: ReactNode; askBar?: ReactNode; groupHooks?: GroupHooks; onDropGroup?: (pane: string, group: string) => void; onBacklogAdd?: (text: string, pane: Pane) => void; /** The × close and multi-select on the tab strip (tabSelection.ts). */ tabPick?: TabPickControls }
/** Desktop workspace controls for a Chat shown in a slot next to other slots. */
export interface SlotControls { index: number; zoomed: boolean; onZoom: () => void; onClose?: () => void; onPick?: () => void; onDragStart?: (event: React.DragEvent) => void; onSwapTo?: (to: number) => void }
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
    // An advisor call is not the agent's reply: a reply before it still ends the turn.
    if (item.kind === 'notice' || item.kind === 'terminal' || item.kind === 'advisor') continue
    if (!open && item.role === 'assistant' && !item.kind) ends.add(item.id)
    open = true
  }
  return ends
}
// A tool group or advisor call is live while it is the newest work in a running turn. Skip notices.
function liveWorkOf(messages: ChatMessage[], working: boolean) {
  if (!working) return null
  for (let at = messages.length - 1; at >= 0; at--) {
    const { kind, id } = messages[at]
    if (kind === 'notice') continue
    return kind === 'actions' || kind === 'advisor' ? id : null
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
  const messages = useMemo(() => chatMessages(data), [data])
  const ends = useMemo(() => turnEndsOf(messages, data?.state === 'Working'), [messages, data?.state])
  const liveWork = useMemo(() => liveWorkOf(messages, data?.state === 'Working'), [messages, data?.state])
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
    <AssistantRuntimeProvider runtime={runtime}><ThreadPrimitive.Messages>{({ message }) => <Message api={api} session={id} actions={ends.has(message.id)} tools={message.metadata.custom.tools as ToolSummary[] | undefined} advisor={message.metadata.custom.advisor as AdvisorCall | undefined} live={message.metadata.custom.kind === 'advisor' && message.id === liveWork} id={message.id} role={message.role} kind={message.metadata.custom.kind} title={message.metadata.custom.title ? String(message.metadata.custom.title) : undefined} count={message.metadata.custom.count} text={String(message.metadata.custom.text ?? '')} source={message.metadata.custom.source as ChatMessage['source']} legacy={message.metadata.custom.legacy === true ? true : undefined} tabs={message.metadata.custom.tabs as ChatMessage['tabs']} time={String(message.metadata.custom.time ?? '')} images={message.metadata.custom.images as number | undefined} imageKey={message.metadata.custom.imageKey as string | undefined} watch={message.metadata.custom.watch as WatchRun | undefined} />}</ThreadPrimitive.Messages></AssistantRuntimeProvider>
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
interface Pending { id: number; text: string; key: string; seen: number; imagesSeen?: number; at: number; thread?: string | null; failed?: string; offline?: boolean; recoveredImageText?: boolean; terminal?: { lastId: string | null } }
// Persist queued sends until delivery or 30 minutes, and keep recovered text until the user handles it.
const PENDING_KEY = 'tether.pending'
const PENDING_TTL = 1800000
const SLASH_CHECK = 3000
const BTW_TIMEOUT = 10000
const keepPending = (item: Pending) => (!item.terminal && item.recoveredImageText) || Date.now() - item.at < PENDING_TTL
function keepPendingOnLoad(item: Pending) {
  if (keepPending(item)) return true
  if (item.terminal || typeof item.text !== 'string') return false
  const { paths, rest } = uploadImageParts(item.text)
  return paths.length > 0 && !!rest
}
const pendingByPane = new Map<string, Pending[]>((() => {
  try {
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}') as Record<string, Pending[]>
    return Object.entries(saved).map(([pane, list]) => [pane, list.filter(keepPendingOnLoad)] as [string, Pending[]]).filter(([, list]) => list.length)
  } catch { return [] }
})())
function savePending() {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(Object.fromEntries([...pendingByPane].filter(([, list]) => list.length)))) } catch { /* storage full or blocked: keep them in memory */ }
}
const squash = (text: string) => text.replace(/\s+/g, ' ').trim()
const slashCommand = (text: string) => /^\/[^\s/]+(?:\s|$)/.test(text.trim())
const commandOf = (text: string) => text.trim().split(/\s+/, 1)[0]
// Claude stores a shell command "!ls" as "! ls", and "!! ls" as "! ! ls". Compare with the "!" marks joined, so a run command leaves the queue.
const sameCommand = (text: string) => text.replace(/^(?:!\s*)+/, '!')
const matches = (messages: ChatMessage[], key: string, images = false) => messages.filter(item => {
  if (item.role !== 'user') return false
  if (images && (item.images ?? 0) > 0) return true
  const shell = shellCommand(item.text)
  return sameCommand(squash(shell ? `!${shell.command}` : item.text)).includes(sameCommand(key))
}).length

function codexSendEvidence(data: ChatData | undefined, text: string) {
  if (data?.agent !== 'codex') return
  const { paths, rest } = uploadImageParts(text)
  if (!paths.length) return
  const files = JSON.stringify(paths.sort()), words = squash(rest)
  let complete = 0, imageOnly = 0
  for (const message of chatMessages(data)) {
    if (message.role !== 'user' || message.kind) continue
    const received = uploadImageParts(message.text)
    if (JSON.stringify(received.paths.sort()) !== files) continue
    if (squash(received.rest) === words) complete++
    else if (!received.rest) imageOnly++
  }
  return { complete, imageOnly, rest }
}

const changedThread = (item: Pending, data: ChatData) => item.thread != null && item.thread !== data.thread

function afterTranscript(item: Pending, data: ChatData): Pending | null {
  const images = codexSendEvidence(data, item.text)
  if ((images || item.recoveredImageText) && changedThread(item, data)) return item
  // /clear and a pane's first prompt begin a new transcript, so its old count cannot apply there.
  const seen = item.thread !== undefined && item.thread !== (data.thread ?? null) ? 0 : item.seen
  if ((images?.complete ?? matches(data.messages, item.key, imagesOnly(item.text))) > seen) return null
  if (!images || images.imageOnly <= (item.imagesSeen ?? 0) || (data.state === 'Working' && keepPending(item))) return item
  // Older Codex versions could take an image from a combined paste and drop the prose.
  const key = squash(images.rest).slice(0, 80)
  return { ...item, text: images.rest, key, seen: matches(data.messages, key), thread: data.thread,
    failed: 'Image delivered, but the message text was not sent.', offline: false, recoveredImageText: true }
}
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

export function Chat({ liveReplyPreview, headerSwipe = false, pane, session, api, onMenu, onUpdate, draft, setDraft, onOpenPane, onSessionActions, onPaneActions, onSplit, onFork, onContinued, onReorder, micOk, chime, readAloud, handsFree, slot, running = 0, sidePin, allPanes, headerExtra, askBar, groupHooks, onDropGroup, onBacklogAdd, tabPick }: ChatProps) {
  useTetherZone()
  const agentBrowser = useAgentBrowser(api)
  const words = useWords()
  const tabPanelId = useId()
  const browserAsk = agentBrowser?.asks.find(item => item.pane === pane.id) ?? null
  const host = useHost()
  const panelRef = useRef<HTMLElement>(null)
  const headerTitle = useRef<HTMLButtonElement>(null)
  const headerTouch = useRef<{ x: number; y: number; offset: number; width: number; mode: 'wait' | 'drag'; lastX: number; lastAt: number; speed: number } | null>(null)
  const headerLeaving = useRef<ReturnType<typeof setTimeout> | null>(null)
  const headerDragged = useRef(false)
  const headerFromTouch = useRef(false)
  const [limit, setLimit] = useState(() => scrollByPane.get(pane.id)?.limit ?? 40)
  const chatQuery = `session=${encodeURIComponent(pane.id)}&limit=${limit}&preview=${liveReplyPreview ? 1 : 0}${pane.agent === 'shell' ? '&mode=shell' : ''}`
  const fetchChat = useCallback(async (signal: AbortSignal) => {
    const early = await takeBootChat(chatQuery)  // index.html asked for this chat while the app downloaded (B24)
    if (early) return early.data as ChatData
    return api<ChatData>(`chat?${chatQuery}${document.visibilityState === 'visible' ? '&watch=1' : ''}`, undefined, signal)
  }, [api, chatQuery])
  const streamPath = useCallback((shown?: ChatData) => `stream?${chatQuery}&delta=1${shown?.version ? `&have=${encodeURIComponent(shown.version)}` : ''}`, [chatQuery])
  const { data: received, error, loading, refresh } = usePolling(fetchChat, 1500, streamPath, chatCache.get(pane.id))
  useEffect(() => { if (received) cacheChat(pane.id, received) }, [pane.id, received])
  // Everything below reads a clean transcript. `transcript` says whether the server sent a list at all.
  const data = useMemo(() => cleanChat(received), [received])
  const transcript = Array.isArray(received?.messages)
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
  const undoNext = useUndoNext()
  const [tools, setTools] = useState(false)
  const [liveSheet, setLiveSheet] = useState<'goal' | 'tasks' | 'crons' | null>(null)
  const [searching, setSearching] = useState(false)
  const target = useRef<string | null>(null)
  const [acting, setActing] = useState(false)
  const [term, setTermState] = useState(() => terminalPanes.has(pane.id))
  // The chat box unmounts while the terminal shows. On the way back, go to the saved place (or the bottom) again.
  const setTerm = (on: boolean) => { if (on) { terminalPanes.add(pane.id); restore.current = scrollByPane.get(pane.id) ?? null } else terminalPanes.delete(pane.id); setTermState(on) }
  // A menu item that closes the menu gives the focus back to the … button, not to the page.
  const focusKeys = () => keysRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
  const openTerminal = () => {
    setTerm(true)
    requestAnimationFrame(() => panelRef.current?.querySelector<HTMLButtonElement>('.terminal-toggle')?.focus())
  }
  const [jump, setJump] = useState(false)
  const viewport = useRef<HTMLDivElement>(null)
  const page = useRef<HTMLDivElement>(null)  // everything under the tabs: it slides as one card on a side swipe
  const hold = useRef<{ top: number; until: number } | null>(null)
  useLayoutEffect(() => { const element = page.current, id = pane.id; return () => saveDock(id, element) }, [pane.id])
  // A side swipe on the chat: 'wait' until the finger shows a direction, then 'drag' moves the chat with the finger.
  const swipe = useRef<{ x: number; y: number; offset: number; mode: 'wait' | 'drag'; lastX: number; lastAt: number; speed: number } | null>(null)
  const leaving = useRef<ReturnType<typeof setTimeout> | null>(null)
  // For a moment after a pane opens, the messages already there do not play the "new message" settle-in.
  const [opening, setOpening] = useState(true)
  useEffect(() => { const timer = setTimeout(() => setOpening(false), 400); return () => clearTimeout(timer) }, [])
  useEffect(() => () => {
    if (leaving.current) clearTimeout(leaving.current)
    if (headerLeaving.current) clearTimeout(headerLeaving.current)
  }, [])
  // In a session with groups, the strip, the swipe and the prefetch use the open tab's group only.
  const view = useMemo(() => session?.groups?.length ? { ...session, panes: groupPanes(session.panes, session.groups, pane) } : session, [session, pane])
  const grouped = !!session?.groups?.length
  const task = taskOf(pane, session?.panes ?? allPanes)
  const children = taskChildren(task, session?.panes ?? allPanes)
  const groupName = placeName(groupOf(task, session?.groups), session?.groups)
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
    const currentX = page.current ? new DOMMatrix(getComputedStyle(page.current).transform).m41 : 0
    if (leaving.current) { clearTimeout(leaving.current); leaving.current = null }
    // Freeze all three pages and the pill where the finger caught them.
    if (currentX) {
      moveChat(currentX)
      const width = page.current?.clientWidth || window.innerWidth
      const target = swipeTarget(currentX)
      const offset = !target || target === 'menu' ? currentX * 4 : Math.abs(currentX) > width ? Math.sign(currentX) * (width + (Math.abs(currentX) - width) * 4) : currentX
      swipe.current = { x: touch.clientX, y: touch.clientY, offset, mode: 'drag', lastX: touch.clientX, lastAt: event.timeStamp, speed: 0 }
      pull.current = null
      return
    }
    const offset = pullNote.current ? new DOMMatrix(getComputedStyle(pullNote.current).transform).m42 + 40 : 0
    if (pullNote.current) { pullNote.current.style.transition = 'none'; pullNote.current.style.transform = `translate3d(0, ${offset - 40}px, 0)` }
    pull.current = viewport.current && viewport.current.scrollTop <= 0 ? touch.clientY - Math.max(0, offset) : null
    pullSpeed.current = { y: touch.clientY, at: event.timeStamp, speed: 0 }
    if (touch.clientX < 28 || touch.clientX > window.innerWidth - 28) return
    for (let node = event.target as HTMLElement | null; node && node !== event.currentTarget; node = node.parentElement) {
      if (node.scrollWidth > node.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(node).overflowX)) return
    }
    if (window.getSelection()?.toString()) return
    swipe.current = { x: touch.clientX, y: touch.clientY, offset: 0, mode: 'wait', lastX: touch.clientX, lastAt: event.timeStamp, speed: 0 }
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
    const transition = ms ? `transform ${ms}ms cubic-bezier(.32,.72,0,1)` : 'none'
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
    const dx = touch.clientX - start.x + start.offset, dy = touch.clientY - start.y
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
    const now = event.timeStamp
    if (now > start.lastAt) start.speed = start.speed * 0.4 + ((touch.clientX - start.lastX) / (now - start.lastAt)) * 0.6
    start.lastX = touch.clientX; start.lastAt = now
    const target = swipeTarget(dx)
    // With a pane on that side the chat follows the finger; at the end of the row it resists.
    const width = page.current?.clientWidth || window.innerWidth
    moveChat(target && target !== 'menu' ? Math.abs(dx) > width ? Math.sign(dx) * (width + (Math.abs(dx) - width) * 0.25) : dx : dx * 0.25)
  }
  const swipeEnd = (event: React.TouchEvent) => {
    pullEnd()
    const start = swipe.current
    swipe.current = null
    if (!start || start.mode !== 'drag') return
    const dx = event.changedTouches[0].clientX - start.x + start.offset
    const width = page.current?.clientWidth || window.innerWidth
    const fling = !reduceMotion() && event.timeStamp - start.lastAt < 100 && Math.abs(start.speed) > 0.35 && Math.sign(start.speed) === Math.sign(dx)
    const target = Math.abs(dx) > width * 0.3 || fling ? swipeTarget(dx) : null
    if (!target || target === 'menu') {
      moveChat(0, reduceMotion() ? 0 : 220)
      if (target === 'menu') onMenu()
      return
    }
    if (reduceMotion()) { onOpenPane(target); return }
    // Finish at the finger's speed: a fast fling ends sooner, a slow drag eases in.
    const x = page.current ? new DOMMatrix(getComputedStyle(page.current).transform).m41 : dx
    const time = Math.round(Math.min(320, Math.max(140, Math.abs(width - Math.abs(x)) / Math.max(Math.abs(start.speed), 1.2))))
    moveChat(Math.sign(dx) * width, time)
    leaving.current = setTimeout(() => { leaving.current = null; onOpenPane(target) }, time)
  }
  // Desktop: a two-finger swipe over the stack row or the tab strip, or BAND px above them, scrolls that strip; over the
  // rest of the header it does nothing (stripScroll.ts). The listeners sit on the header parts only: a non-passive wheel
  // listener on the chat would make Chromium wait for the main thread before each chat scroll starts, also while a
  // reply streams. So the band below the tab strip, which lies over the chat, is left out.
  // A mouse wheel (vertical) over a strip scrolls it sideways, with the setting on or off.
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const desk = matchMedia(WORKSPACE_QUERY)
    const decide = createStripWheel(headerSwipe)
    const wheel = (event: WheelEvent) => {
      const target = event.target as Element | null
      if (!desk.matches || target?.closest(SKIP)) return
      const strips = [...panel.querySelectorAll<HTMLElement>(STRIPS)].filter(strip => strip.offsetHeight > 0)
      const plan = decide(event, dx => ({ strips: strips.map(boxOf), native: scrollsItself(target, panel, dx) }))
      if (plan.do === 'pass' || !event.cancelable) return
      event.preventDefault()
      const strip = plan.do === 'scroll' ? strips[plan.strip] : undefined
      if (strip && plan.do === 'scroll') scrollSideways(strip, plan.dx, event.timeStamp)
    }
    let zone: Element[] = []
    const attach = () => {
      for (const part of zone) part.removeEventListener('wheel', wheel as EventListener)
      zone = [...panel.querySelectorAll(HEADER)]
      for (const part of zone) part.addEventListener('wheel', wheel as EventListener, { passive: false })
      for (const parent of panel.querySelectorAll(PARENTS)) observer.observe(parent, { childList: true })
    }
    const observer = new MutationObserver(attach)  // the stack row and the strip come and go with the tabs
    observer.observe(panel, { childList: true })
    attach()
    if (headerSwipe) panel.dataset.stripScroll = ''
    return () => { observer.disconnect(); for (const part of zone) part.removeEventListener('wheel', wheel as EventListener); delete panel.dataset.stripScroll }
  }, [headerSwipe])
  const swipeCancel = () => {
    if (swipe.current?.mode === 'drag') moveChat(0, reduceMotion() ? 0 : 220)
    swipe.current = null; pull.current = null; setPulled(0)
  }
  const moveHeader = (x: number, width: number, ms = 0) => {
    const title = headerTitle.current
    if (!title) return
    title.style.transition = ms ? `transform ${ms}ms cubic-bezier(.32,.72,0,1), opacity ${ms}ms cubic-bezier(.32,.72,0,1)` : 'none'
    title.style.transform = `translateX(${x}px)`
    title.style.opacity = `${Math.max(0, 1 - Math.abs(x) / width * 0.5)}`
  }
  const headerRef = useRef<HTMLElement>(null)
  const headerGhost = useRef<HTMLDivElement | null>(null)
  const headerSwap = useRef(slot?.onSwapTo)
  // Polls replace slot callbacks. Keep the current one without cancelling a hold.
  useLayoutEffect(() => { headerSwap.current = slot?.onSwapTo })
  const canCarryHeader = !!slot?.onSwapTo
  useEffect(() => {
    const header = headerRef.current
    if (!header || !canCarryHeader) return
    let held: { x: number; y: number; lastX: number; lastY: number; active: boolean; moved: boolean; timer: ReturnType<typeof setTimeout>; source: HTMLElement } | null = null
    let over: HTMLElement | null = null
    const end = () => {
      if (held) { clearTimeout(held.timer); if (held.active) headerDragged.current = true; held.source.removeAttribute('data-carried') }
      held = null
      over?.removeAttribute('data-touch-drop'); over = null
      headerGhost.current?.remove(); headerGhost.current = null
    }
    const targetAt = (x: number, y: number) => {
      const target = document.elementFromPoint(x, y)?.closest<HTMLElement>('.slot[data-slot]')
      return target && target !== held?.source ? target : null
    }
    const start = (event: TouchEvent) => {
      end()
      if (event.touches.length !== 1) return
      const target = event.target as Element
      if (target.closest('button:not(.chat-title), .icon-button, [role="button"], a, input, select, textarea, [role="menu"]')) return
      const source = header.closest<HTMLElement>('.slot[data-slot]')
      if (!source) return
      const touch = event.touches[0]
      held = { x: touch.clientX, y: touch.clientY, lastX: touch.clientX, lastY: touch.clientY, active: false, moved: false, source, timer: setTimeout(() => {
        if (!held) return
        held.active = true; headerDragged.current = true; headerTouch.current = null
        navigator.vibrate?.(12)
        const ghost = document.createElement('div'), label = document.createElement('span')
        ghost.className = 'touch-ghost'; ghost.setAttribute('aria-hidden', 'true')
        ghost.style.transform = `translate3d(${held.lastX}px, ${held.lastY}px, 0)`
        label.textContent = headerTitle.current?.querySelector('h1')?.textContent ?? ''
        ghost.append(label); document.body.append(ghost); headerGhost.current = ghost
        source.setAttribute('data-carried', '')
      }, HOLD_MS) }
    }
    const move = (event: TouchEvent) => {
      if (!held) return
      if (event.touches.length !== 1) { end(); return }
      const touch = event.touches[0], distance = Math.hypot(touch.clientX - held.x, touch.clientY - held.y)
      if (held.active) held.moved ||= touch.clientX !== held.lastX || touch.clientY !== held.lastY
      held.lastX = touch.clientX; held.lastY = touch.clientY
      if (!held.active) { if (distance > 10) end(); return }
      event.preventDefault()
      if (headerGhost.current) headerGhost.current.style.transform = `translate3d(${touch.clientX}px, ${touch.clientY}px, 0)`
      const next = targetAt(touch.clientX, touch.clientY)
      if (over !== next) { over?.removeAttribute('data-touch-drop'); over = next; over?.setAttribute('data-touch-drop', '') }
    }
    const up = (event: TouchEvent) => {
      if (!held) return
      if (!held.active) { end(); return }
      event.preventDefault()
      const touch = event.changedTouches[0], target = touch && held.moved ? targetAt(touch.clientX, touch.clientY) : null
      const to = target ? Number(target.dataset.slot) : NaN
      end()
      if (Number.isInteger(to)) headerSwap.current?.(to)
    }
    const extraFinger = (event: TouchEvent) => { if (event.touches.length > 1) end() }
    const context = (event: Event) => { if (held?.active) event.preventDefault() }
    header.addEventListener('touchstart', start, { passive: true })
    header.addEventListener('touchmove', move, { passive: false })
    header.addEventListener('touchend', up, { passive: false })
    header.addEventListener('touchcancel', end)
    header.addEventListener('contextmenu', context)
    window.addEventListener('touchstart', extraFinger, { capture: true, passive: true })
    return () => {
      end()
      header.removeEventListener('touchstart', start); header.removeEventListener('touchmove', move)
      header.removeEventListener('touchend', up); header.removeEventListener('touchcancel', end)
      header.removeEventListener('contextmenu', context)
      window.removeEventListener('touchstart', extraFinger, true)
    }
  }, [canCarryHeader, pane.id, slot?.index])
  const headerStart = (event: React.TouchEvent) => {
    headerTouch.current = null
    if (!sidePin?.pinned || sidePin.total <= 1 || event.touches.length !== 1 || !headerTitle.current) return
    const touch = event.touches[0], width = event.currentTarget.clientWidth || window.innerWidth
    const x = new DOMMatrix(getComputedStyle(headerTitle.current).transform).m41
    if (headerLeaving.current) { clearTimeout(headerLeaving.current); headerLeaving.current = null }
    moveHeader(x, width)
    const offset = Math.abs(x) > width ? Math.sign(x) * (width + (Math.abs(x) - width) * 4) : x
    headerDragged.current = !!x
    headerTouch.current = { x: touch.clientX, y: touch.clientY, offset, width, mode: x ? 'drag' : 'wait', lastX: touch.clientX, lastAt: event.timeStamp, speed: 0 }
  }
  const headerMove = (event: React.TouchEvent) => {
    const start = headerTouch.current
    if (!start || event.touches.length !== 1) return
    const touch = event.touches[0], dx = touch.clientX - start.x + start.offset, dy = touch.clientY - start.y
    if (start.mode === 'wait') {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return
      if (Math.abs(dx) <= Math.abs(dy) * 1.5 || window.getSelection()?.toString()) { headerTouch.current = null; return }
      start.mode = 'drag'; headerDragged.current = true
    }
    const now = event.timeStamp
    if (now > start.lastAt) start.speed = start.speed * 0.4 + (touch.clientX - start.lastX) / (now - start.lastAt) * 0.6
    start.lastX = touch.clientX; start.lastAt = now
    const x = Math.abs(dx) > start.width ? Math.sign(dx) * (start.width + (Math.abs(dx) - start.width) * 0.25) : dx
    moveHeader(x, start.width)
  }
  const headerEnd = (event: React.TouchEvent, cancelled = false) => {
    const start = headerTouch.current
    headerTouch.current = null
    if (!start || start.mode !== 'drag') return
    const dx = (event.changedTouches[0]?.clientX ?? start.lastX) - start.x + start.offset
    const reduced = reduceMotion()
    const fling = !reduced && event.timeStamp - start.lastAt < 100 && Math.abs(start.speed) > 0.35 && Math.sign(start.speed) === Math.sign(dx)
    if (cancelled || !sidePin?.pinned || sidePin.total <= 1 || !(Math.abs(dx) > start.width * 0.3 || fling)) {
      moveHeader(0, start.width, reduced ? 0 : 200)
      return
    }
    const step = () => { headerLeaving.current = null; moveHeader(0, start.width); sidePin.onStep(dx < 0 ? 1 : -1) }
    if (reduced) { step(); return }
    const x = headerTitle.current ? new DOMMatrix(getComputedStyle(headerTitle.current).transform).m41 : dx
    const time = Math.round(Math.max(140, Math.min(260, Math.abs(start.width - Math.abs(x)) / Math.max(Math.abs(start.speed), 1.2))))
    moveHeader(Math.sign(dx) * start.width, start.width, time)
    headerLeaving.current = setTimeout(step, time)
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
  // Files dragged in from the desktop attach to this chat's composer (chatDrop.ts).
  const { dropping, fileDrop } = useFileDrop(activePane.id, dropRefusal({ agent: activePane.agent, writable: activePane.writable && writable }, draft, !!data), setToast)
  // The name the tab shows: the pane's own name, else the session's (a bare number is not a name).
  const claudeName = pane.pane_name || (session && !/^\d+$/.test(session.name) ? session.name : '')
  const messages = useMemo(() => chatMessages(data), [data])
  // Up-arrow recall offers only what the person typed: not the status, review and rules prompts Tether typed in.
  const sentPrompts = useMemo(() => messages.filter(message => message.role === 'user' && !message.kind && message.source !== 'tether' && !INTERRUPTED.has(message.text.trim())).map(message => message.text), [messages])
  // Read a new reply aloud when its turn ends. Replies already there when the chat opens are not read.
  useEffect(() => readAloud ? keepAudioUnlocked() : undefined, [readAloud])
  // Hands-free: when the reading ends, ask the composer to start the mic.
  const [listenRequest, setListenRequest] = useState(0)
  const lastReply = useMemo(() => [...messages].reverse().find(message => message.role === 'assistant' && !message.kind), [messages])
  const readUpTo = useRef<string | null | undefined>(undefined)
  const cachedAtOpen = useRef(chatCache.get(pane.id))
  useEffect(() => {
    if (!data || received === cachedAtOpen.current) return  // saved data is not "there when the chat opens"; wait for fresh data
    if (readUpTo.current === undefined || !readAloud) { readUpTo.current = lastReply?.id ?? null; return }
    if (state === 'Working' || !lastReply || lastReply.id === readUpTo.current) return
    readUpTo.current = lastReply.id
    if (document.visibilityState !== 'visible') return
    speak(lastReply.text, lastReply.text, handsFree && micOk && writable ? () => setListenRequest(count => count + 1) : undefined)
  }, [data, received, state, lastReply, readAloud, handsFree, micOk, writable])
  useEffect(() => () => stopSpeaking(), [])
  const [pending, setPendingState] = useState<Pending[]>(() => pendingByPane.get(pane.id) ?? [])
  // A running `!` command shows in the thread with its live output, so its queued bubble is hidden.
  const bang = activePane.agent === 'claude' ? data?.bang : undefined
  const bangKey = bang ? squash(bang.command).slice(0, 40) : ''
  const bangItem = bang ? pending.find(item => !item.failed && item.text.trim().startsWith('!') && squash(item.text.trim().slice(1)).slice(0, 40) === bangKey) : undefined
  const notes = pending.filter(item => item.terminal)
  const newestNote = notes[notes.length - 1]
  const queued = pending.filter(item => !item.terminal && item !== bangItem)
  const notSent = queued.filter(item => item.failed).length
  const waiting = queued.length - notSent
  // Prompts in Claude's own input queue (the server sends them only mid-turn). One this message box sent already shows as its own bubble, so show only the rest:
  // sends from Friday, another device or the terminal.
  const otherQueued = foreignQueued(data?.queued ?? [], queued.filter(item => !item.failed))
  const waitingAll = waiting + otherQueued.length
  // Claude's "edit queued" pulls back its whole queue. With more than one entry, the cancel button says so, without a
  // count: the server shows at most 50 entries.
  const cancelLabel = activePane.agent !== 'claude' ? 'Hide this queued message' : waitingAll > 1 ? 'Cancel all queued messages' : 'Cancel this queued message'
  const queueLabel = [waitingAll ? `${waitingAll} queued` : '', notSent ? `${notSent} not sent` : ''].filter(Boolean).join(' · ')
  const setPending = useCallback((update: (list: Pending[]) => Pending[]) => setPendingState(list => { const next = update(list); pendingByPane.set(pane.id, next); savePending(); return next }), [pane.id])
  const dataReceivedAt = useRef(0)
  useEffect(() => { dataReceivedAt.current = received && received !== cachedAtOpen.current ? Date.now() : 0 }, [received])
  const refreshedFor = useRef(new WeakSet<Pending>())
  const [recheck, setRecheck] = useState(0)
  useEffect(() => {
    const now = Date.now()
    let left: Pending[]
    let needsRefresh = false
    // Reconcile only against a real transcript. A partial body (an error, an old server) keeps the queue as it is.
    if (data && (!data.session || data.session === pane.id) && transcript) {
      const cached = received === cachedAtOpen.current
      const card = data.attention || data.approval || data.plan
      const lastId = () => data.messages[data.messages.length - 1]?.id ?? null
      left = pending.flatMap(item => {
        if (cached && !keepPending(item)) return keepPendingOnLoad(item) ? [item] : []
        const next = afterTranscript(item, data)
        if (!next || !keepPending(next)) return []
        // A pane's first prompt starts a thread where there was none; that is a chat effect too.
        const threadChanged = changedThread(item, data) || (item.thread === null && data.thread != null)
        if (item.terminal) return card || threadChanged || lastId() !== item.terminal.lastId ? [] : [item]
        if (next.failed || !slashCommand(next.text) || now - next.at <= SLASH_CHECK) return [next]
        // These cards open at once, even while the agent works: /btw, /mcp, /config, /permissions. The command ran.
        if (['side', 'dialog', 'permissions', 'panel'].includes(data.attention?.reason ?? '')) return []
        // /btw can finish even while the agent works, either with a saved side answer or after its timeout.
        if (/^\/btw\s/.test(next.text.trim())) {
          const question = squash(next.text.trim().slice(4))
          const answered = question && data.messages.some(message => message.kind === 'side' && squash(message.title ?? '') === question)
          return data.state !== 'Working' || answered || now - next.at > BTW_TIMEOUT ? [] : [next]
        }
        if (data.state === 'Working' || next !== item) return [next]
        // Only a confirmed Idle pane says the command ran with no output. Other states (Unknown, Unavailable, Approval) leave quietly, as before.
        // A card or a new thread shows its effect in the chat, so leave quietly too.
        if (data.state !== 'Idle' || card || threadChanged) return []
        if (dataReceivedAt.current >= item.at + SLASH_CHECK) return [{ ...item, terminal: { lastId: lastId() } }]
        // An idle stream can report the same version without delivering another data object. Ask for one fresh read.
        if (!refreshedFor.current.has(item)) { refreshedFor.current.add(item); needsRefresh = true }
        return [item]
      })
    } else left = pending.filter(item => !item.terminal || keepPending(item))
    const unchanged = left.length === pending.length && left.every((item, index) => item === pending[index])
    setPending(list => list !== pending || unchanged ? list : left)
    if (needsRefresh) void refresh()
    // Recheck unresolved commands at their deadlines. Notes only need a timer for expiry.
    const deadline = Math.min(...left.flatMap(item => {
      if (item.terminal) return [item.at + PENDING_TTL]
      if (item.failed || !slashCommand(item.text)) return []
      const deadlines = [item.at + SLASH_CHECK + 1]
      if (/^\/btw\s/.test(item.text.trim())) deadlines.push(item.at + BTW_TIMEOUT + 1)
      return deadlines.filter(at => at > now)
    }))
    if (!Number.isFinite(deadline)) return
    const timer = window.setTimeout(() => setRecheck(n => n + 1), Math.max(0, deadline - Date.now()))
    return () => window.clearTimeout(timer)
  }, [data, received, transcript, pane.id, pending, recheck, refresh, setPending])
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
  // A workspace slot clips what overflows it, and a short grid row can be under 120px tall. There the … menu opens in
  // the top layer (popover), under the … button and inside the window, and it moves when the window or slot resizes (B378).
  useLayoutEffect(() => {
    const keys = keysRef.current, menu = keys?.querySelector<HTMLElement>('.keys-menu'), panel = panelRef.current
    if (!keysOpen || !keys || !menu || !panel?.closest('.workspace') || typeof menu.showPopover !== 'function') return
    menu.popover = 'manual'
    menu.showPopover()
    const place = () => {
      const button = keys.getBoundingClientRect(), gap = 4, edge = 8, scrolled = menu.scrollTop
      Object.assign(menu.style, { inset: 'auto', margin: '0', maxHeight: 'none', overflowY: 'auto', overscrollBehavior: 'contain' })
      const width = menu.offsetWidth, below = window.innerHeight - button.bottom - gap - edge, above = button.top - gap - edge
      const up = below < Math.min(menu.scrollHeight, 240) && above > below
      menu.style.maxHeight = `${Math.max(0, Math.floor(up ? above : below))}px`
      menu.style.left = `${Math.max(edge, Math.min(button.right - width, window.innerWidth - width - edge))}px`
      if (up) menu.style.bottom = `${window.innerHeight - button.top + gap}px`
      else menu.style.top = `${button.bottom + gap}px`
      // Keep the reader's place, and keep the focused item in view when the menu gets shorter.
      menu.scrollTop = scrolled
      if (menu.contains(document.activeElement)) (document.activeElement as HTMLElement).scrollIntoView({ block: 'nearest' })
    }
    place()
    const observer = new ResizeObserver(place)
    observer.observe(panel)
    window.addEventListener('resize', place)
    return () => { observer.disconnect(); window.removeEventListener('resize', place); if (menu.matches(':popover-open')) menu.hidePopover() }
  }, [keysOpen])
  // When a header or menu action hides the focused control (a menu item closes the menu, Back to chat folds the terminal
  // button, a pin folds), focus moves to the … button, not to the page (WCAG 2.4.3). Only while focus was in this header.
  // A resize can hide it too: a menu item or header button folds or unfolds at a container breakpoint.
  // It acts only when the header control that last had focus is gone or hidden: focus the user moved away stays put.
  const headerFocus = useRef<HTMLElement | null>(null)
  const rescueFocus = useRef(() => {})
  useLayoutEffect(() => {
    rescueFocus.current = () => {
      const last = headerFocus.current, active = document.activeElement
      if (!last || (last.isConnected && last.getClientRects().length)) return
      if (!active || active === document.body || active === last) focusKeys()
    }
  })
  useLayoutEffect(() => rescueFocus.current(), [keysOpen, term, sidePin?.pinned])
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const observer = new ResizeObserver(() => rescueFocus.current())
    observer.observe(panel)
    // A press outside the header moves the user away, even where it leaves focus on the page (empty chat space).
    const away = (event: PointerEvent) => { if (!headerRef.current?.contains(event.target as Node)) headerFocus.current = null }
    document.addEventListener('pointerdown', away, true)
    return () => { observer.disconnect(); document.removeEventListener('pointerdown', away, true) }
  }, [])
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
      // A hit inside a fold (a scheduled run, a watch cycle) must show: open each closed fold around it.
      for (let fold = element.parentElement?.closest('details'); fold; fold = fold.parentElement?.closest('details')) fold.open = true
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
    if (viewport.current) observer.observe(viewport.current)
    return () => observer.disconnect()
  }, [followBottom, term])
  const lastFailure = useRef<unknown>(null)
  const cardError = useRef(false)
  async function write(path: string, body: object) {
    setSendError(''); setDraftBlocked(false); lastFailure.current = null; cardError.current = false
    try { await api(path, body); await refresh(); onUpdate(); return true }
    catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await refresh(); setToast(path === 'pick' ? errorText(error) : 'The thread changed. Review the chat, then try again.')
      } else { lastFailure.current = error; setSendError(errorText(error)); setDraftBlocked(error instanceof ApiError && error.draft) }
      return false
    }
  }
  // Claude: Up pulls every queued message back into its input; Tether clears that input and returns the text to the message box.
  // Codex has no queue to edit, so the × only hides the bubble there.
  // id null: an entry another sender queued. Claude returns the whole queue, so it lands in the message box the same way.
  async function unqueue(id: number | null) {
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
  const menuBlocksComposer = !data?.approval && (data?.attention?.menu_verified === false || data?.attention?.ask_current?.multi === true)
  const menuRevision = data?.attention?.menu_verified || undefined
  const ownAnswer = !menuBlocksComposer && !data?.approval && data?.attention?.reason === 'menu' ? data.attention.options?.find(option => option.free) : undefined
  async function send(text: string, retryOf?: Pending) {
    text = terminalText(text)
    setPending(list => list.some(item => item.terminal) ? list.filter(item => !item.terminal) : list)
    // Claude shows these as a terminal panel that a phone sees only a slice of. Show Tether's own sheet instead, as T3 Code does.
    if (!retryOf && activePane.agent === 'claude' && /^\/(usage|cost|stats|status)$/.test(text.trim())) { setPlanSheet(true); return true }
    if (ownAnswer && !retryOf) return write('menu', { session: pane.id, thread: data?.thread ?? null, choice: ownAnswer.id, answer: text, revision: menuRevision })
    const key = squash(text).slice(0, 80)
    // The send must go out even when the delivery check cannot read the chat.
    let images: ReturnType<typeof codexSendEvidence>
    try { images = codexSendEvidence(data, text) } catch { images = undefined }
    const seen = retryOf?.seen ?? images?.complete ?? matches(messages, key, imagesOnly(text))
    const thread = retryOf?.thread ?? data?.thread ?? null
    const accepted = await write('message', { session: pane.id, thread, message: text, ...(activePane.agent === 'shell' ? { mode: 'shell' } : {}) })
    const failure = lastFailure.current
    // No answer from the server (offline, timeout, server down): keep the message as a "Not sent" bubble with Retry, and clear the box.
    const lost = !accepted && !!failure && (!(failure instanceof ApiError) || (failure.status >= 500 && !failure.draft))
    if (lost) setSendError('')
    if ((accepted || lost) && activePane.agent !== 'shell' && key) {
      // eslint-disable-next-line react-hooks/purity -- The runtime invokes this send handler on message submission; these timestamps follow the awaited write, never render.
      const at = retryOf?.at ?? Date.now()
      const item = { ...retryOf, id: retryOf?.id ?? at, text, key, seen, imagesSeen: retryOf?.imagesSeen ?? images?.imageOnly,
        thread, at, failed: lost ? errorText(failure) : undefined, offline: lost && !(failure instanceof ApiError) }
      setPending(list => {
        const queued = list.filter(other => !other.terminal)
        return retryOf ? queued.map(other => other.id === retryOf.id ? item : other) : [...queued, item]
      })
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
      let fresh = await api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=${limit}`).catch(() => null)
      if (!fresh || !Array.isArray(fresh.messages)) {
        setToast('Could not check delivery. Reconnect and try again.'); return
      }
      fresh = cleanChat(fresh)!
      if (fresh.session !== pane.id || changedThread(item, fresh)) {
        setToast('The thread changed. Use Edit to recover this message.'); void refresh(); return
      }
      const next = afterTranscript(item, fresh)
      if (next !== item) {
        setPending(list => list.flatMap(other => other.id !== item.id ? [other] : next ? [next] : [])); void refresh(); return
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
  const agentThread = data?.thread ?? activePane.thread ?? null
  const [agentMapThread, setAgentMapThread] = useState<string | null>()
  if (agentMapThread !== undefined && agentMapThread !== agentThread) setAgentMapThread(undefined)
  const subAgents = useSubAgents(api, activePane.id, agentThread, activePane.agent === 'claude' || activePane.agent === 'codex')
  const [planSheet, setPlanSheet] = useState(false)
  const [modelSheet, setModelSheet] = useState(false)
  const [continueSheet, setContinueSheet] = useState(false)
  const [modeMenu, setModeMenu] = useState(false)
  const modeButton = useRef<HTMLButtonElement>(null)
  useBackStep(modeMenu, () => setModeMenu(false))
  const [modeBusy, setModeBusy] = useState('')
  const [info, setInfo] = useState(false)
  // A media path the agent named in a reply. Ask the server first, so a refused or missing file shows its reason, not a broken image.
  const [opened, setOpened] = useState<Media | null>(null)
  const [textFile, setTextFile] = useState<TextFile | null>(null)
  const [officeFile, setOfficeFile] = useState<OfficeFile | null>(null)
  const [previewPage, setPreviewPage] = useState<PreviewTarget | null>(null)
  const tabOpener = useMemo(() => ({ panes: allPanes, current: pane, open: onOpenPane }), [allPanes, pane, onOpenPane])
  const openMedia = useCallback(async (path: string, kind: Kind, line?: number) => {
    const url = `/mobile/api/file?session=${encodeURIComponent(activePane.id)}&path=${encodeURIComponent(path)}`
    const name = path.split('/').pop() || path
    if (kind === 'text') { setTextFile({ path, url, line }); return }
    if (kind === 'html') { setPreviewPage({ path, title: name }); return }
    try {
      const check = await fetch(url, { headers: { Range: 'bytes=0-0' } })
      if (!check.ok) { const body = await check.json().catch(() => null) as { error?: string } | null; setOpened({ name, url, error: body?.error ?? 'Tether cannot open this file.' }); return }
      void check.body?.cancel()
    } catch { setOpened({ name, url, error: `Cannot reach ${host}.` }); return }
    if (kind === 'office') setOfficeFile({ path, url })
    else if (kind === 'file') saveFile(url, name)
    else if (kind === 'pdf') window.open(url, '_blank', 'noopener')
    else setOpened({ name: path, url, kind })
  }, [activePane.id, host, setTextFile, setOfficeFile, setOpened, setPreviewPage])
  // Pages agents made: a Claude artifact opens its local copy in the drawer.
  const artifacts = useMemo(() => artifactsOf(messages), [messages])
  const previewActions = useMemo(() => ({
    open: setPreviewPage,
    artifactFor: (url: string) => artifacts.get(url),
  }), [artifacts, setPreviewPage])
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
    let timer: ReturnType<typeof setTimeout> | undefined
    const read = () => {
      if (!live || document.visibilityState !== 'visible') return Promise.resolve()
      const sequence = ++footerReads.current.requested
      return api<Footer>(`footer?session=${encodeURIComponent(pane.id)}`).then(result => {
        if (!live || sequence < footerReads.current.applied) return
        footerReads.current.applied = sequence
        // No input box on screen (a dialog, a redraw): keep the chips, but not a card whose keys could reach the dialog.
        const next = result.observed === false ? { ...(footerCache.get(footerId) ?? {}), heads_up: null, suggestion: '' } : result
        cacheFooter(footerId, next)
        setFooter({ key: footerId, value: next })
      }, () => {})
    }
    // The next read starts a set time after the last one ends, so on a slow link the reads never pile up (B64).
    // While Claude works, its verb, tokens and tool change every second.
    const delay = state === 'Working' ? 1000 : 5000
    const poll = () => { void read().finally(() => { if (live) timer = setTimeout(poll, delay) }) }
    readFooter.current = () => { void read() }
    poll()
    return () => {
      live = false; clearTimeout(timer)
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
  const advisorAlert = activePane.agent === 'claude' && !!footer.advisor?.problems.length
  const tier = footer.tier === 'fast' || footer.tier === 'ultrafast' ? footer.tier : ''
  const tierLabel = tier ? `. ${tier === 'fast' ? 'Fast' : 'Ultrafast'} mode on` : ''
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
  const codexTrust = useCodexTrust(api, activePane.agent === 'codex' ? { mode: 'running', session: activePane.id } : null, activePane.thread ?? '')
  // A card key that Claude did not take leaves its error under the message box. Clear it when the card changes or closes.
  const card = footer.heads_up ? `${footer.heads_up.state}|${footer.heads_up.text}` : ''
  useEffect(() => { if (cardError.current) { cardError.current = false; setSendError(''); setDraftBlocked(false) } }, [card, pane.id])
  // A list action names a row or submits the numbered question identified by its revision.
  async function pickRow(row: number | undefined, labels: string[] | undefined, then: PickKey, revision?: string, ticked?: boolean) {
    if (acting) return false
    setActing(true)
    try { return await write('pick', { session: pane.id, thread: data?.thread ?? null, row, labels, then, heading: data?.attention ? pickHeading(data.attention) : '', revision, ticked }) } finally { setActing(false) }
  }
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
  // One action of a Claude dialog card. The server reads the dialog again and sends only keys that the dialog offers.
  async function dialogAction(attention: Attention, action: DialogAction) {
    if (acting) return null
    setActing(true)
    try { await api('dialog', { session: pane.id, thread: data?.thread ?? null, title: attention.dialog?.title, mode: attention.dialog?.mode, ...action }); return null }
    catch (failure) { return errorText(failure) }
    finally { await refresh(); setActing(false) }
  }
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
  // Both routes verify the cursor before sending Enter.
  async function choose(kind: 'approval' | 'menu', option: Choice, approval: Approval | null, feedback?: string) {
    if (acting) return
    setActing(true)
    try {
      if (kind === 'approval' && approval?.id) await write('approval', { session: pane.id, thread: data?.thread ?? null, approval: approval.id, choice: option.id, feedback })
      // Arrows plus Enter, checked on the screen: a digit key types text when the cursor sits on the own-answer row.
      else await write('menu', { session: pane.id, thread: data?.thread ?? null, choice: option.id, label: option.text, revision: menuRevision })
    } finally { setActing(false) }
  }
  async function start(agent: 'claude' | 'codex') {
    if (acting) return
    setActing(true)
    try { await write('start', { session: pane.id, thread: data?.thread ?? null, agent }) } finally { setActing(false) }
  }
  // Copy, read aloud and share belong to a finished reply: only the last text of each turn gets them, and not while the agent still works.
  const turnEnds = useMemo(() => turnEndsOf(messages, state === 'Working'), [messages, state])
  const liveWork = useMemo(() => liveWorkOf(messages, state === 'Working'), [messages, state])
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
  // Thumbs down can send its note to the agent. Not while the agent asks a question: the message box would answer that question.
  // It sends only to the conversation that was rated: after a switch, the thread differs and nothing is sent.
  const tellRef = useRef<(text: string, thread: string) => Promise<boolean>>(async () => false)
  useLayoutEffect(() => { tellRef.current = (text, thread) => thread !== data?.thread || !writable || ownAnswer || data?.approval ? Promise.resolve(false) : send(text) })
  const tell = useCallback((text: string, thread: string) => tellRef.current(text, thread), [])
  const feedback = useReplyFeedback(api, activePane.agent === 'shell' ? null : data?.thread, writable ? tell : undefined)
  return <AssistantRuntimeProvider runtime={runtime}><MediaOpener.Provider value={openMedia}><TabOpener.Provider value={tabOpener}><FilePane.Provider value={activePane.id}><PreviewOpener.Provider value={previewActions}><ReplyFeedbackProvider value={feedback}><section ref={panelRef} className={activePane.agent === 'shell' && !term ? 'chat-panel shell-page' : 'chat-panel'} aria-label={`Chat with ${pane.label}`} data-pane={activePane.id} data-task-tree={children.length ? '' : undefined} {...fileDrop}>
    <div className="visually-hidden" role="status">{newestNote ? `${commandOf(newestNote.text)} ran in the terminal. It left no chat output.` : ''}</div>
    {dropping !== null && <div className={dropping ? 'chat-drop refused' : 'chat-drop'} aria-hidden="true">{dropping ? <><Ban size={22} />{dropping}</> : <><Paperclip size={22} />Drop to attach to {chatTitle(pane, session)}</>}</div>}
    <header ref={headerRef} className="chat-header" onFocus={event => { headerFocus.current = event.target }} onBlur={event => { if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) headerFocus.current = null }} draggable={!!slot?.onDragStart} onPointerDown={event => { headerFromTouch.current = event.pointerType === 'touch'; headerDragged.current = false }} onDragStart={event => { if (headerFromTouch.current || headerTouch.current) event.preventDefault(); else slot?.onDragStart?.(event) }} onTouchStart={headerStart} onTouchMove={headerMove} onTouchEnd={headerEnd} onTouchCancel={event => headerEnd(event, true)} onClickCapture={event => { if (headerDragged.current && event.detail !== 0) { event.preventDefault(); event.stopPropagation() } }}><button className="icon-button mobile-menu" onClick={onMenu} aria-label={running ? `Open sessions. ${running} agents running or waiting` : 'Open sessions'}><Menu />{running > 0 && <span className="menu-count" aria-hidden="true">{running}</span>}</button>{slot && <kbd className="slot-badge" title={`Focus this slot: Alt+${slot.index + 1}`}>⌥{slot.index + 1}</kbd>}<button ref={headerTitle} type="button" className="chat-title" aria-label="Session details" onClick={() => setInfo(true)}><span className="task-phone-stack">{groupName}</span><h1><span className="task-desktop-title">{chatTitle(pane, session)}</span><span className="task-phone-title">{chatTitle(task, session)}</span></h1><div>{grouped && session && <><bdi className="group-crumb" title={`${session.name} › ${groupName}`}>{session.name} › {groupName}</bdi><span className="header-separator">·</span></>}<AgentIcon agent={activePane.agent} icon={activePane.pane_icon} /><span className="agent-word">{agentNames[activePane.agent]}</span>{activePane.agent === 'codex' && pane.codex_profile && <span className="provider-badge" title={`Runs on codex -p ${pane.codex_profile}`}>{profileName(pane.codex_profile)}</span>}<span className="header-separator agent-word">·</span><State state={state} />{context && <><span className="header-separator">·</span><span className={Number(context) >= 80 ? 'context-high' : ''}>{context}%<span className="context-word"> context</span></span></>}{activePane.worktree && <><span className="header-separator">·</span><WorktreeMark branch={activePane.worktree} /></>}{activePane.dir && !activePane.worktree && <><span className="header-separator">·</span><bdi className="header-folder" dir="ltr" title={activePane.dir} aria-label={`Directory: ${activePane.dir}`}>{activePane.dir.split('/').filter(Boolean).pop() || '/'}</bdi></>}{started && <span className="header-started" title={`Chat started ${started}`} aria-label={`Started ${started}`}><Clock3 size={12} aria-hidden="true" />{started}</span>}{sidePin?.pinned && <PinMark pin={sidePin} />}</div></button>
      {headerExtra}
      <ReviewLinks api={api} pane={pane} panes={allPanes} onOpen={onOpenPane} onChanged={onUpdate} onToast={setToast} />
      {sidePin && activePane.agent !== 'shell' && <button type="button" className={data ? 'icon-button side-pin-toggle foldable' : 'icon-button side-pin-toggle'} aria-pressed={sidePin.pinned} aria-label={sidePin.pinned ? 'Unpin from side tab' : 'Pin to side tab'} title={sidePin.pinned ? 'Unpin from side tab' : 'Pin to side tab'} onClick={sidePin.onToggle}>{sidePin.pinned ? <PinOff size={20} /> : <Pin size={20} />}</button>}
      <button type="button" className={data ? 'icon-button search-toggle foldable' : 'icon-button search-toggle'} aria-label="Search this chat" title="Search this chat" disabled={!data} onClick={() => setSearching(true)}><Search size={20} /></button>
      <button type="button" className={`icon-button terminal-toggle${term ? ' active' : ''}${data ? ' foldable' : ''}`} aria-label={term ? 'Back to chat' : 'Open terminal'} aria-pressed={term} title={term ? 'Back to chat' : 'Open terminal'} onClick={() => setTerm(!term)}>{term ? <MessageSquare size={20} /> : <SquareTerminal size={20} />}</button><div className="keys-container" ref={keysRef} onKeyDown={event => { if (event.key === 'Escape') { setKeysOpen(false); keysRef.current?.querySelector('button')?.focus() } }} onBlur={event => { if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setKeysOpen(false) }}><button className="icon-button" aria-label={`Session, ${words.pane} and keys`} aria-expanded={keysOpen} disabled={!data} onClick={() => setKeysOpen(!keysOpen)}><MoreHorizontal /></button>{keysOpen && <div className="keys-menu" aria-label="Session and keys">{/* A narrow workspace pane hides these header buttons (App.css, B378); the menu keeps them in reach. */}<button className="keys-fold" onClick={() => { setKeysOpen(false); focusKeys(); setSearching(true) }}><Search size={16} />Search this chat</button><button className="keys-fold" onClick={() => { setKeysOpen(false); if (term) { focusKeys(); setTerm(false) } else openTerminal() }}>{term ? <MessageSquare size={16} /> : <SquareTerminal size={16} />}{term ? 'Back to chat' : 'Open terminal'}</button>{slot?.onPick && <button className="keys-fold-slot" onClick={() => { setKeysOpen(false); focusKeys(); slot.onPick?.() }}><ArrowLeftRight size={16} />Show another pane here</button>}{slot && <button className="keys-fold-slot" onClick={() => { setKeysOpen(false); focusKeys(); slot.onZoom() }}>{slot.zoomed ? <Minimize2 size={16} /> : <Maximize2 size={16} />}{slot.zoomed ? 'Show all slots' : 'Zoom this slot'}<kbd>Alt+Z</kbd></button>}<button onClick={() => { setKeysOpen(false); onSessionActions(menuAnchor()) }}><Settings2 size={16} />Session options</button><button onClick={() => { setKeysOpen(false); onPaneActions(activePane.id, menuAnchor()) }}><PanelTop size={16} />{words.Pane} options</button><MuteItem api={api} pane={activePane.id} />{undoNext.undo && <button className="keys-step" onClick={() => { setKeysOpen(false); requestUndo('undo') }}><Undo2 size={16} aria-hidden="true" /><span>Undo: {undoNext.undo}</span></button>}{undoNext.redo && <button className="keys-step" onClick={() => { setKeysOpen(false); requestUndo('redo') }}><Redo2 size={16} aria-hidden="true" /><span>Redo: {undoNext.redo}</span></button>}{sidePin && activePane.agent !== 'shell' && <button onClick={() => { setKeysOpen(false); sidePin.onToggle() }}>{sidePin.pinned ? <PinOff size={16} /> : <Pin size={16} />}{sidePin.pinned ? 'Unpin from side tab' : 'Pin to side tab'}</button>}{activePane.agent === 'claude' && data && data.claude_name === null && claudeName && <button disabled={!writable || acting} onClick={() => { setKeysOpen(false); void send(`/rename ${claudeName}`) }}><Pencil size={16} />Name Claude “{claudeName}”</button>}{activePane.agent !== 'shell' && <button onClick={() => { setKeysOpen(false); setTools(true) }}><Blocks size={16} />Skills, plugins, MCP</button>}<button onClick={() => { setKeysOpen(false); keysRef.current?.querySelector('button')?.focus(); setPreviewPage(devServersTarget()) }}><Server size={16} />Dev servers</button><button onClick={() => { setKeysOpen(false); onSplit('right') }}><Columns2 size={16} />{words.newRight}{words.tmux && <kbd>%</kbd>}</button><button onClick={() => { setKeysOpen(false); onSplit('down') }}><Rows2 size={16} />{words.newBelow}{words.tmux && <kbd>"</kbd>}</button>{canFork(activePane) && <button onClick={() => { setKeysOpen(false); onFork() }}><GitFork size={16} />New chat from this</button>}<span>Send a key</span>{['Escape', 'C-c', 'Enter', 'Up', 'Down', 'Tab', 'y', 'n'].map(key => <button key={key} disabled={!writable || acting} onClick={() => void sendKey(key)}>{key === 'Escape' ? 'Esc' : key === 'C-c' ? 'Ctrl+C' : key}<kbd>{key === 'Up' ? '↑' : key === 'Down' ? '↓' : key === 'Enter' ? '↵' : key === 'Tab' ? '⇥' : key === 'C-c' ? '^C' : key === 'Escape' ? 'esc' : key}</kbd></button>)}</div>}</div>
      {slot?.onPick && <button type="button" className="icon-button slot-button slot-pick" aria-label={`Change what slot ${slot.index + 1} shows`} title="Show another pane or session here" onClick={slot.onPick}><ArrowLeftRight size={16} /></button>}
      {slot && <button type="button" className="icon-button slot-button slot-zoom" aria-label={slot.zoomed ? 'Show all slots (Alt+Z)' : 'Zoom this slot (Alt+Z)'} title={slot.zoomed ? 'Show all slots (Alt+Z)' : 'Zoom this slot (Alt+Z)'} onClick={slot.onZoom}>{slot.zoomed ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>}
      {slot?.onClose && <button type="button" className="icon-button slot-button" aria-label="Close this slot (Alt+X)" title="Close this slot (Alt+X)" onClick={slot.onClose}><X size={16} /></button>}
    </header>
    {session && view && (grouped && groupHooks
      ? <GroupBar api={api} session={session} pane={pane} hooks={groupHooks} onOpenPane={onOpenPane} onUpdate={onUpdate}>
          <PaneTabs ref={tabs} panelId={term ? undefined : tabPanelId} session={view} pane={task} onOpenPane={onOpenPane} onPaneActions={onPaneActions} onSplit={onSplit} onReorder={onReorder} onDropGroup={onDropGroup} pick={tabPick} api={api} onUpdate={onUpdate} micOk={micOk} chime={chime} onNotice={setToast} />
        </GroupBar>
      : <PaneTabs ref={tabs} panelId={term ? undefined : tabPanelId} session={session} pane={task} onOpenPane={onOpenPane} onPaneActions={onPaneActions} onSplit={onSplit} onReorder={onReorder} onDropGroup={onDropGroup} pick={tabPick} api={api} onUpdate={onUpdate} micOk={micOk} chime={chime} onNotice={setToast} />)}
    {children.length > 0 && <TaskChips panelId={term ? undefined : tabPanelId} task={task} children={children} pane={pane} onOpen={onOpenPane} onActions={onPaneActions} />}
    {!pane.task && <OpenedBy pane={pane} panes={allPanes} onOpen={onOpenPane} />}
    {term && data?.attention?.askBridge && <AskBar agent={agentNames[activePane.agent]} disabled={!writable || acting} onOpen={() => setTerm(false)} onTerminal={() => askAnswer(data.attention!.askBridge!, null)} />}
    {term ? <LiveTerminal paneId={activePane.id} onChat={() => setTerm(false)} /> : <>
    {(offline || (error && data)) && <div className="reconnect-banner" role="status"><WifiOff size={15} /><span>{offline ? 'You are offline. Tether reconnects when the network is back.' : `Cannot reach ${host}. Reconnecting…`}</span><RetryButton label="Retry now" className="" onRetry={refresh} /></div>}
    {data && activePane.agent === 'shell' && <div className="start-options"><button disabled={!writable || acting} onClick={() => void start('claude')}>Start Claude<ChevronRight size={16} /></button><button disabled={!writable || acting} onClick={() => void start('codex')}><span>Start Codex<FoundryTag api={api} /></span><ChevronRight size={16} /></button></div>}
    <div className="chat-pager" id={tabPanelId} role={session ? 'tabpanel' : undefined} aria-label={session ? chatTitle(pane, session) : undefined}><div className="chat-page" ref={page}>
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

      {activePane.agent === 'shell' ? <ShellOutput key={activePane.id} folder={activePane.dir} text={messages.map(message => message.text).join('\n')} /> : <ThreadPrimitive.Messages>{({ message }) => <Message api={api} session={activePane.id} live={message.id === liveWork} runningShown={!!footer.running} actions={turnEnds.has(message.id)} tools={message.metadata.custom.tools as ToolSummary[] | undefined} advisor={message.metadata.custom.advisor as AdvisorCall | undefined} id={message.id} role={message.role} kind={message.metadata.custom.kind} title={message.metadata.custom.title ? String(message.metadata.custom.title) : undefined} count={message.metadata.custom.count} text={String(message.metadata.custom.text ?? '')} source={message.metadata.custom.source as ChatMessage['source']} legacy={message.metadata.custom.legacy === true ? true : undefined} tabs={message.metadata.custom.tabs as ChatMessage['tabs']} time={String(message.metadata.custom.time ?? '')} images={message.metadata.custom.images as number | undefined} imageKey={message.metadata.custom.imageKey as string | undefined} watch={message.metadata.custom.watch as WatchRun | undefined} />}</ThreadPrimitive.Messages>}
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
    {liveSheet && <LiveSheet api={api} pane={activePane} show={liveSheet} taskDetails={footer.task_details} crons={footer.crons} scheduled={data?.scheduled} onClose={() => setLiveSheet(null)} onTerminal={() => setTerm(true)} />}
    {tools && <Tools api={api} pane={activePane} onClose={() => setTools(false)} onPick={text => { setDraft(text); setTools(false) }} />}
    {data?.login_result && <p className="login-result" role={data.login_result.phase === 'failed' ? 'alert' : 'status'}>{data.login_result.message}</p>}
    {state !== 'Working' && doneIn?.pane === pane.id && <div className="working-line done-line" role="status"><span className="done-check"><Check size={12} strokeWidth={3} /></span><span><strong>Done in {formatElapsed(doneIn.seconds)}</strong></span></div>}
    {state === 'Stopped' && <div className="working-line" role="status"><span className="state-dot" aria-hidden="true" /><span><strong>Stopped by a restart</strong><small>Send a message to continue.</small></span></div>}
    {state === 'Working' && <div className="working-line" role="status"><WorkGlyph /><span><span className="working-head"><strong className="work-verb">{footer.spinner?.verb ?? 'Working…'}<span className="work-glint" aria-hidden="true"><span>{footer.spinner?.verb ?? 'Working…'}</span></span></strong>{footer.spinner?.detail ? <small>{footer.spinner.detail}</small> : workingSince && <Elapsed since={workingSince.at} />}</span>{footer.running && <span className="running-tool"><Terminal size={13} /><span><span className="running-title">{footer.running.title}{footer.running.elapsed && <small> · {footer.running.elapsed}</small>}</span>{footer.running.command && footer.running.command !== footer.running.title && <code>{footer.running.command}</code>}</span></span>}{footer.denied && <button type="button" className="running-tool denied-tool" disabled={!writable || acting} title="Open /permissions, tab Recently blocked" onClick={() => void openPermissions('Recently blocked')}><ShieldAlert size={13} /><span>{footer.denied.text}{footer.denied.reason && <small>{footer.denied.reason}</small>}</span><span className="denied-review">Review</span></button>}</span></div>}
    <StatusStack priorityKey={data?.approval ? `approval:${(data.approval as Approval).id}` : data?.attention ? `attention:${data.attention.id}` : data?.plan ? `plan:${data.plan.id}` : footer.heads_up ? 'heads' : browserAsk ? `browser-ask:${browserAsk.id}` : ''} chips={(footer.goal || !!footer.tasks?.length || !!footer.crons?.length || (footer.suggestion && !draft.trim() && state !== 'Working')) ? <>{(footer.goal || !!footer.tasks?.length || !!footer.crons?.length) ? <div className="live-chips">{footer.goal && <button type="button" className="live-chip" aria-haspopup="dialog" onClick={() => setLiveSheet('goal')}><Target size={14} />{footer.goal_status ? `Goal · ${goalStatusLabel(footer.goal_status)}` : `Goal active${footer.goal !== 'active' ? ` · ${footer.goal}` : ''}`}<ChevronRight size={13} /></button>}{footer.tasks?.map(task => <button type="button" key={task} className="live-chip" aria-haspopup="dialog" onClick={() => setLiveSheet('tasks')}><Layers size={14} /><span className="live-chip-text">{taskLabel(task, footer.task_details)}</span><ChevronRight size={13} /></button>)}{!!footer.crons?.length && <button type="button" className="live-chip" aria-haspopup="dialog" title={cronSummary(footer.crons)} onClick={() => setLiveSheet('crons')}><CalendarClock size={14} /><span className="live-chip-text">{cronsLabel(footer.crons)}</span><ChevronRight size={13} /></button>}</div> : null}{(footer.suggestion && !draft.trim() && state !== 'Working' ? <button type="button" className="prompt-suggestion" title="Use this suggested prompt" onClick={() => setDraft(footer.suggestion ?? '')}><CornerDownRight size={15} /><span>{footer.suggestion}</span></button> : null)}</> : null} items={[
      { id: 'action', label: data?.plan ? 'Review plan' : data?.approval ? `${agentNames[activePane.agent]} needs permission` : data?.attention?.reason === 'permissions' ? 'Permissions' : data?.attention?.reason === 'side' ? 'Side question' : data?.attention?.reason === 'dialog' ? data.attention.dialog?.title ?? 'Dialog' : data?.attention?.reason === 'panel' || data?.attention?.reason === 'info' ? `${agentNames[activePane.agent]} panel open` : `${agentNames[activePane.agent]} needs attention`, active: !!(data?.approval || data?.attention || data?.plan), content: <>{data && (data.approval != null || data.attention || data.plan) && <ChoiceDock key={(data.approval as Approval | null)?.id ?? data.attention?.id ?? 'choice'} approval={data.approval as Approval | null} attention={data.attention ?? null} plan={data.plan} agent={agentNames[activePane.agent]} disabled={!writable || acting} onChoose={choose} onKey={key => sendKey(key)} onOpenTerminal={openTerminal} permissionsTab={permissionsTab} onAction={action => promptAction(data.attention!, action)} onDialog={action => dialogAction(data.attention!, action)} onPick={pickRow} onAsk={askAnswer} onAskEngage={askEngage} onType={(text, loginUrl) => write('type', { session: activePane.id, thread: data?.thread ?? null, text, enter: true, ...loginUrl ? { login_url: loginUrl } : {} })} />}</> },
      { id: 'heads', label: '✦ Heads up', active: !!footer.heads_up, content: <>{footer.heads_up && <HeadsUp card={footer.heads_up} disabled={!writable || acting} onKey={headsUpKey} onChat={note => { setDraft(note); requestAnimationFrame(() => { const box = panelRef.current?.querySelector<HTMLTextAreaElement>('.composer textarea'); box?.focus(); box?.setSelectionRange(box.value.length, box.value.length) }) }} />}</> },
      { id: 'agents', label: `${background.length} agents running`, active: background.length > 0, content: <>{background.length > 0 && <div className="agents-strip"><ul>{background.map((agent, index) => <li key={index}><strong>{agent.name}</strong><span>{agent.task}</span><small>{agent.detail}</small></li>)}</ul></div>}</> },
      { id: 'queue', label: queueLabel, active: queued.length + otherQueued.length > 0, content: <>      {queued.map(item => item.failed ? <div key={item.id} className="user-message pending-message failed-message"><span>{item.text}</span><small role="alert">{item.offline ? <><WifiOff size={12} />Not sent</> : <span className="failed-reason">Not sent: {item.failed}</span>}
        <button type="button" className="failed-action" disabled={retrying !== null || !writable} onClick={() => void retry(item)}><RotateCcw size={13} />{retrying === item.id ? 'Sending…' : 'Retry'}</button>
        <button type="button" className="failed-action" onClick={() => editFailed(item)}><Pencil size={13} />Edit</button>
        <button type="button" aria-label="Delete this unsent message" onClick={() => setPending(list => list.filter(other => other.id !== item.id))}><X size={12} /></button></small></div>
      : <div key={item.id} className="user-message pending-message"><SentContent {...sentContent(item.text, activePane.id)} /><small><Clock3 size={12} />{/^\/btw\s/.test(item.text.trim()) ? 'Asking…' : state === 'Working' ? 'Queued' : 'Sending…'}<button type="button" aria-label={cancelLabel} title={cancelLabel} onClick={() => void unqueue(item.id)}><X size={12} /></button></small></div>)}
        {otherQueued.map((entry, index) => <div key={`queued-${entry.id ?? index}`} className="user-message pending-message"><SentContent {...sentContent(entry.text, activePane.id)} /><small><Clock3 size={12} />Queued<button type="button" aria-label={cancelLabel} title={cancelLabel} onClick={() => void unqueue(null)}><X size={12} /></button></small></div>)}</> },
      { id: 'terminal-run', label: notes.length === 1 ? `${commandOf(notes[0].text)} ran in the terminal` : `${notes.length} commands ran in the terminal`, active: notes.length > 0, content: notes.map(item => {
        const command = commandOf(item.text)
        const dismiss = () => setPending(list => list.filter(other => other.id !== item.id))
        return <div key={item.id} className="user-message pending-message terminal-note">
          <SentContent {...sentContent(item.text, activePane.id)} />
          <small>
            <span className="terminal-note-text"><SquareTerminal size={12} aria-hidden="true" />Ran in the terminal. No chat output.</span>
            <button type="button" className="failed-action" aria-label={`Open terminal to see ${command}`} onClick={() => {
              dismiss()
              openTerminal()
            }}><SquareTerminal size={13} aria-hidden="true" />Open terminal</button>
            <button type="button" aria-label={`Dismiss note: ${command} ran in the terminal`} onClick={dismiss}><X size={12} aria-hidden="true" /></button>
          </small>
        </div>
      }) },
      ...codexRunItems(codexRuns),
      { id: 'codex-trust', label: codexTrust.status?.state === 'untrusted' ? 'Codex read-only' : 'Folder trusted', active: codexTrust.status?.state === 'untrusted' || codexTrust.status?.state === 'restart' || codexTrust.justTrusted, content: <CodexTrustCard trust={codexTrust} running /> },
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
    {info && <SessionDetails key={`${activePane.id} ${data?.thread ?? activePane.thread ?? ''}`} api={api} pane={activePane} session={session} stack={groupName} footer={footer} mode={MODES[currentMode]} model={model ?? ''} context={context} state={state} thread={data?.thread ?? activePane.thread} onClose={() => setInfo(false)}
      onFiles={folder => { setInfo(false); window.dispatchEvent(new CustomEvent('tether:open-files', { detail: { path: folder } })) }}
      onModel={writable && state !== 'Working' ? () => { setInfo(false); setModelSheet(true) } : undefined}
      onUsage={activePane.agent === 'claude' ? () => { setInfo(false); setPlanSheet(true) } : activePane.agent === 'codex' ? () => { setInfo(false); setUsageSheet(true) } : undefined}
      onLive={show => { setInfo(false); setLiveSheet(show) }} onTools={activePane.agent !== 'shell' ? () => { setInfo(false); setTools(true) } : undefined}
      onBacklog={query => { setInfo(false); window.dispatchEvent(new CustomEvent('tether:open-backlog', { detail: { query } })) }} onRenamed={onUpdate} />}
    {usageSheet && <Usage footer={footer} onClose={() => setUsageSheet(false)} />}
    {agentMapThread === agentThread && <AgentMap api={api} session={activePane.id} list={subAgents.data} onClose={() => setAgentMapThread(undefined)} />}
    {planSheet && <PlanUsage api={api} onClose={() => setPlanSheet(false)} />}
    {continueSheet && <ContinueWith api={api} pane={activePane} thread={data?.thread ?? null} idle={state === 'Idle' || state === 'Stopped'} onClose={() => setContinueSheet(false)} onCreated={id => { setContinueSheet(false); onContinued(id) }} />}
    {modelSheet && <ModelSheet onContinue={() => { setModelSheet(false); setContinueSheet(true) }} api={api} pane={activePane} thread={data?.thread ?? null} currentLabel={model} currentEffort={footer.effort} advisor={footer.advisor} onClose={() => setModelSheet(false)} onDone={message => { setModelSheet(false); setToast(message); readFooter.current() }} />}
    {pane.id === task.id && <TaskNeeds api={api} task={task} children={children} onOpen={onOpenPane} onUpdate={onUpdate} />}
    <Composer onTerminalEscape={() => void sendKey('Escape')} options={activePane.agent === 'shell' ? undefined : <span className="composer-pills">
      <button type="button" className="pill" aria-label={`Model and effort${advisorAlert ? `. Advisor ${footer.advisor?.name} is not attached` : ''}${tierLabel}`} disabled={!writable || state === 'Working'} onClick={() => setModelSheet(true)}>
        {advisorAlert && <TriangleAlert size={13} className="pill-warning" aria-hidden="true" />}
        <span className="pill-text">{model || 'Model'}</span>
        {footer.effort && <span className="pill-effort">· {footer.effort}</span>}
        {tier && <span className="pill-tier"><Zap size={12} aria-hidden="true" /><span className="pill-tier-word">{tier}</span></span>}
        <ChevronDown size={13} />
      </button>
      <AgentPill list={subAgents.data} onOpen={() => setAgentMapThread(agentThread)} />
      {activePane.agent === 'codex' && (footer.context || !!footer.windows?.length || creditsLabel(footer.credits)) && <button type="button" className="pill" aria-label="Usage and credits" aria-haspopup="dialog" onClick={() => setUsageSheet(true)}><Gauge size={13} />Usage</button>}
      {activePane.agent === 'claude' && (footer.observed || footer.status || footer.mode) && <span className="mode-container" onKeyDown={event => { if (modeMenu && event.key === 'Escape') { event.stopPropagation(); setModeMenu(false); modeButton.current?.focus() } }}><button ref={modeButton} type="button" className="pill" aria-label={`Mode: ${MODES[currentMode]}. Change mode`} aria-expanded={modeMenu} disabled={!writable || !!modeBusy} onClick={() => setModeMenu(!modeMenu)}><Gauge size={13} /><span className="pill-text">{modeBusy ? `${MODES[modeBusy]}…` : MODES[currentMode]}</span></button>
        {modeMenu && <div className="attach-menu mode-menu" role="menu">{Object.entries(MODES).map(([key, label]) => <button key={key} role="menuitemradio" aria-checked={key === currentMode} type="button" onClick={() => void chooseMode(key)}>{label}{key === currentMode && <Check size={16} />}</button>)}</div>}</span>}
    </span>} api={api} pane={activePane} thread={data?.thread ?? null} disabled={!data || !writable || menuBlocksComposer} onSend={send} error={sendError} errorAction={draftBlocked ? <button type="button" disabled={acting} onClick={() => void clearAgentInput()}>Clear {agentNames[activePane.agent]}’s input</button> : null} draft={draft} setDraft={setDraft} working={state === 'Working'} onStop={() => void stop()} micOk={micOk} chime={chime} handsFree={handsFree && readAloud} listenRequest={listenRequest} sent={sentPrompts} onTools={() => setTools(true)} hint={ownAnswer ? `Answer ${agentNames[activePane.agent]}’s question` : undefined} />
    </div>{nextPane && <ChatPeek id={nextPane.id} data={chatCache.get(nextPane.id)} api={api} side={1} peekRef={peekNext} />}{previousPane && <ChatPeek id={previousPane.id} data={chatCache.get(previousPane.id)} api={api} side={-1} peekRef={peekPrevious} />}</div>
    </>}
    <ImageViewer image={opened} close={() => setOpened(null)} onShow={showInFiles(opened && !opened.error ? opened.name : null, () => setOpened(null))} />
    <TextViewer file={textFile} close={() => setTextFile(null)} onShow={showInFiles(textFile?.path ?? null, () => setTextFile(null))} />
    <OfficeViewer file={officeFile} close={() => setOfficeFile(null)} onShow={showInFiles(officeFile?.path ?? null, () => setOfficeFile(null))} />
    <PreviewDrawer api={api} session={activePane.id} target={previewPage} close={() => setPreviewPage(null)} onSource={(path, url) => setTextFile({ path, url, readOnly: true })} />
  </section></ReplyFeedbackProvider></PreviewOpener.Provider></FilePane.Provider></TabOpener.Provider></MediaOpener.Provider></AssistantRuntimeProvider>
}
