import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { LayoutDashboard, SquareSplitHorizontal, SquareSplitVertical, Terminal } from 'lucide-react'
import { ApiError, canFork, errorText, request } from './api'
import type { Api, Pane, PastChat, SessionsData } from './api'
import { AutoResumeNotice } from './AutoResumeNotice'
import { useModal, usePolling } from './hooks'
import { startTrial } from './lookTrial'
import { TrialBar } from './TrialBar'
import type { Anchor } from './hooks'
import { setPlace, setPlaceHandler } from './backNav'
import { usePrefs } from './prefs'
import { resumeBackground } from './backgrounds'
import { applyPhoto } from './photoLook'
import { applyScheme } from './schemes'
import { DEFAULT_LOOK, PHOTO_URL, type Look, type PanelName, applyLayoutTheme, gridStyle, hasPanel as layoutHas, lookKey, lookLayout, themeMode, variantAttrs } from './layouts'
import { StatusColumn, WaitingPanel } from './LayoutPanels'
import './layouts.css'
import './glass.css'
import { PlanUsage, Sidebar, groupSessions, isArchived, isDone, needsYou, paneTitle, runningPanes, shownIndex, sortByPrefs, toggleArchive } from './Sidebar'
import type { Session } from './Sidebar'
import { withColor, withGroup } from './Organize'
import { PaneActions, ResumeChat, SessionActions, Settings } from './Sheets'
import type { Place } from './Sheets'
import { ProjectSheet, useProjectMarks } from './Project'
import { BacklogSheet } from './Backlog'
import { BacklogAddSheet } from './BacklogAdd'
import { orderPanes, reorderPanes } from './PaneTabs'
import { worktreeNotice } from './Worktree'
import { NewTask } from './NewTask'
import { isTabArchived, tabNeighbor, withTabArchived, withoutTab } from './tabArchive'
import { selectPane } from './paneSelection'
import type { SelectionSnapshot } from './paneSelection'
import { NewSession } from './NewSession'
import { resumeChat } from './ChatPicker'
import type { ChatAgent } from './api'
import { Chat } from './Chat'
import type { SlotControls } from './Chat'
import { SlotPicker, Workspace } from './Workspace'
import { Inbox, InboxButton } from './Inbox'
import { inboxItems } from './needs'
import { AskChips, OldestButton, WaitingBox } from './Attention'
import { ActionDeck, useUrgentDeck } from './ActionDeck'
import { useNow, visibleAsk, waitingAsks } from './attention'
import { Home } from './Home'
import { hasAgents, homeGroups } from './homeGroups'
import { DRAG_SLOT, WORKSPACE_QUERY, addPanel, addSlot, deskFor, deskKeyFor, hasPanel, newPanelId, panelCols, placePane, removePanel, withDesk, withPanels, resolveSlots, splitSlot, storeSlots, swapSlots } from './workspace'
import { PanelColumn, PanelTools } from './PanelColumn'
import { PANELS, TOOLBAR_PANELS, extraPanels, togglePanelKind } from './panels'
import type { PanelKind, SetPanels } from './panels'
import type { Side } from './workspace'
import { withoutShells } from './sideShells'
import type { DesktopLayout, SidePin, WaitOrder } from './prefs'
import { CommandMenu } from './CommandMenu'
import type { Command } from './CommandMenu'
import { Shortcuts } from './Shortcuts'
import { StatusBar } from './StatusBar'
import { toggleTerminal, toggleVoice } from './keys'
import { copyPaneName, nameKindOf } from './CopyNames'
import { Rail } from './Rail'
import type { Screen } from './Rail'
import { Explorer } from './Explorer'
import { mentionText } from './fileWrites'
import type { FilesTarget } from './Explorer'
import { ExplorerPhone } from './ExplorerPhone'
import { Share } from './Share'
import { PhoneTabs } from './PhoneTabs'
import { ASSISTANT, Live } from './Live'
import { BrowserFloat } from './BrowserWindow'
import { BrowserSheet, WATCH_EVENT, browserAsks, setPaneLabels, useAgentBrowser, watchTab } from './AgentBrowser'
import { AgentBubble } from './AgentBubble'
import { SidePins } from './SidePins'
import { AgentsPage } from './AgentsView'
import { choosePhoneScreen, toggleButton } from './phoneScreen'
import { WordsContext, wordsFor } from './words'
import type { GroupHooks } from './Groups'
import { groupOf, placesOf, stepPlace, tabFor } from './groups'
import './App.css'
import './TitleBar.css'
import { useWindowControls } from './titlebar'
import { PanelHandles } from './PanelHandles'
import { sizeVars } from './panelEdges'

// One tap turns notifications for this pane off or on. Hidden until this browser has notifications on.

function App() {
  const [unauthorized, setUnauthorized] = useState(false)
  useWindowControls(!unauthorized)
  const api: Api = useCallback(async (path, body, signal, method, timeout) => {
    try { return await request(path, body, signal, method, timeout) } catch (error) {
      if (error instanceof ApiError && error.status === 401) setUnauthorized(true)
      throw error
    }
  }, [])
  const fetchSessions = useCallback((signal: AbortSignal) => api<SessionsData>('sessions', undefined, signal), [api])
  const { data, error, loading, refresh } = usePolling(fetchSessions, 4000)
  const [prefs, update] = usePrefs()
  useEffect(() => {
    const pending = resumeBackground(api)
    if (pending) void pending.promise.then(item => applyPhoto(item, update)).catch(() => { /* A resumed job can expire or disappear after a server restart. */ })
  }, [api, update])
  const [drawer, setDrawer] = useState(false)
  const [newSession, setNewSession] = useState(false)
  const [actions, setActions] = useState<string | null>(null)
  const [actionsDelete, setActionsDelete] = useState(false)
  const [paneActions, setPaneActions] = useState<string | null>(null)
  // Where the actions menu opens on the desktop. Without it (the phone) the actions show as a sheet.
  const [menuAt, setMenuAt] = useState<Anchor>()
  // The sidebar row's +: the same New task popover as the tab strip's +, for that session's last pane.
  const [tabTask, setTabTask] = useState<{ pane: Pane; at: Anchor } | null>(null)
  const [resume, setResume] = useState<PastChat | null>(null)
  const [notice, setNotice] = useState('')
  const [settings, setSettings] = useState<boolean | 'layout-maker'>(false)
  const [backlog, setBacklog] = useState(false)
  const [backlogAdd, setBacklogAdd] = useState<{ text: string; pane: Pane } | null>(null)
  // The Project sheet for a tmux session, with a file from Files to pin when it came from there.
  const [project, setProject] = useState<{ session: string; pin?: string; group?: string } | null>(null)
  const [planSheet, setPlanSheet] = useState(false)
  const [liveMode, setLive] = useState(false)
  const [liveReveal, setLiveReveal] = useState(0)  // a new ask while Friday runs as a bubble opens its window again
  const [liveFrom, setLiveFrom] = useState<DOMRect | null>(null)  // the button that opened Friday: the window grows out of it
  const openLive = (event?: unknown) => {
    const button = event && typeof event === 'object' && 'currentTarget' in event && event.currentTarget instanceof Element ? event.currentTarget : null
    if (!liveMode) setLiveFrom(button?.getBoundingClientRect() ?? null)
    setLive(true); setLiveReveal(n => n + 1)
  }
  // Phone (or no workspace): "Watch" on an agent's browser card opens the browser full screen.
  const [browserSheet, setBrowserSheet] = useState(false)
  // Desktop workspace: the ⌘K command menu or the ? shortcuts sheet.
  const [menu, setMenu] = useState<'commands' | 'shortcuts' | null>(null)
  // Home-screen shortcuts open /chat/?go=attention|new|usage (manifest.webmanifest).
  const [go, setGo] = useState(() => new URL(location.href).searchParams.get('go'))
  // The Agents page (Active and Unread tabs). Tether's summary notification opens it with /chat/?go=agents.
  // 'message' opens it on the "Message sessions" box (sidebar button, command menu).
  const [agentsPage, setAgentsPage] = useState<false | 'list' | 'message'>(false)
  const markRead = useCallback((ids: string[]) => {
    const now = Date.now() / 1000
    update(current => ({ seen: { ...current.seen, ...Object.fromEntries(ids.map(id => [id, now])) } }))
  }, [update])
  // A layout with its own colours also sets light or dark to match them (layouts.ts).
  const [wide, setWide] = useState(() => typeof matchMedia === 'function' && matchMedia(WORKSPACE_QUERY).matches)
  const look = prefs.look ?? DEFAULT_LOOK
  const lookShown = lookLayout(look)
  const lookKeep = !wide && lookShown.phone === 'keep'
  const deviceLook = lookKeep ? (prefs.myLook ?? DEFAULT_LOOK) : look
  const lookTheme = JSON.stringify(lookKeep ? null : lookShown.theme)
  const themeShown = !lookKeep && lookShown.theme ? themeMode(lookShown.theme) : prefs.theme
  useEffect(() => {
    const root = document.documentElement
    if (themeShown === 'system') delete root.dataset.theme; else root.dataset.theme = themeShown
    root.dataset.textSize = prefs.textSize
  }, [themeShown, prefs.textSize])
  useLayoutEffect(() => { applyScheme(prefs.scheme); applyLayoutTheme(JSON.parse(lookTheme)) }, [prefs.scheme, lookTheme])
  // Unsent drafts are part of the synced prefs: a reload, a pane switch or another device keeps them.
  const reload = () => location.reload()
  const setDraft = (id: string, text: string) => update(current => (current.drafts[id]?.text ?? '') === text ? {} : { drafts: { ...current.drafts, [id]: { text, at: Date.now() / 1000 } } })
  // An installed app on Android resumes instead of reloading. When it comes back to the front, check for a newer build.
  const [updated, setUpdated] = useState(false)
  useEffect(() => {
    const current = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/"]')?.getAttribute('src')
    if (!current) return
    const check = () => {
      if (document.visibilityState !== 'visible') return
      fetch('/chat/', { cache: 'no-store' }).then(response => response.text()).then(html => {
        const latest = /src="([^"]*\/assets\/index-[^"]+\.js)"/.exec(html)?.[1]
        if (latest && latest !== current) setUpdated(true)
      }, () => {})
    }
    document.addEventListener('visibilitychange', check)
    const timer = setInterval(check, 300000)
    return () => { document.removeEventListener('visibilitychange', check); clearInterval(timer) }
  }, [])
  const panes = useMemo(() => data?.sessions ?? [], [data])
  const sessions = useMemo(() => groupSessions(panes, item => isTabArchived(item, prefs.archivedTabs) && item.id !== prefs.selected).map(item => ({ ...item, panes: orderPanes(item.panes, prefs.paneOrder[item.id]), groups: data?.groups?.[item.id] })), [panes, data?.groups, prefs.archivedTabs, prefs.selected, prefs.paneOrder])
  const archivedTabs = useMemo(() => groupSessions(panes).flatMap(session => session.panes.filter(item => isTabArchived(item, prefs.archivedTabs) && item.id !== prefs.selected)), [panes, prefs.archivedTabs, prefs.selected])
  const livePins = useMemo(() => prefs.sidePins.flatMap(pin => { const pane = panes.find(item => item.id === pin.pane && item.agent !== 'shell'); return pane ? [{ pin, pane }] : [] }), [prefs.sidePins, panes])
  const pinKeys = useRef<(dir: 1 | -1) => boolean>(() => false)
  const groupKeys = useRef<(dir: 1 | -1) => boolean>(() => false)
  const previousSelection = useRef<SelectionSnapshot>({ sessions: [] })
  const orderedSessions = useMemo(() => sortByPrefs(sessions, prefs.order), [sessions, prefs.order])
  const pane = selectPane(orderedSessions, prefs.selected, previousSelection.current, data?.default_thread, prefs.lastPane)
  useLayoutEffect(() => {
    if (!data) return
    const previous = previousSelection.current
    previousSelection.current = { sessions: orderedSessions, selected: pane?.id }
    // Persist a close fallback once. A pending new-tab selection must wait for its refresh.
    const wanted = prefs.selected ?? previous.selected
    if (previous.selected !== wanted || !previous.sessions.some(session => session.panes.some(item => item.id === wanted)) || panes.some(item => item.id === wanted)) return
    update(current => current.selected !== prefs.selected ? {} : {
      selected: pane?.id ?? null,
      ...(pane && { lastPane: { ...current.lastPane, [pane.tmux_session]: pane.id } }),
    })
  }, [data, orderedSessions, pane, panes, prefs.selected, update])
  // Desktop workspace: on only at desktop width with a mouse or trackpad. The phone keeps one pane.
  useEffect(() => {
    const query = matchMedia(WORKSPACE_QUERY)
    const change = () => setWide(query.matches)
    query.addEventListener('change', change)
    return () => query.removeEventListener('change', change)
  }, [])
  const [zoomed, setZoomed] = useState(false)
  // Desktop rail: Chats and Terminals are the workspace (chat or terminal view of the focused slot); Files is the explorer.
  // A device with no chosen pane starts on Home, unless a link names a pane or a place.
  const [screen, setScreen] = useState<Screen>(() => prefs.selected || new URL(location.href).searchParams.has('session') || new URL(location.href).searchParams.get('go') === 'attention' ? 'chats' : 'home')
  // Home takes over when you pick it, and when there is no pane to show. Nothing is on screen behind it, so no pane counts as open.
  const home = screen === 'home' || (!pane && screen !== 'files' && screen !== 'share')
  const openId = home ? undefined : pane?.id
  const followSlot = useRef(false)
  const leaveHome = useCallback(() => { followSlot.current = true; setScreen(current => current === 'home' ? 'chats' : current) }, [])
  const [filesTarget, setFilesTarget] = useState<FilesTarget | null>(null)
  const files = wide && screen === 'files'
  const phoneFiles = !wide && screen === 'files'
  const share = screen === 'share'
  const chooseScreen = useCallback((next: Screen) => {
    setScreen(next)
    if (next === 'home' || next === 'files' || next === 'share') return
    setTimeout(() => {
      const toggle = document.querySelector<HTMLButtonElement>('.slot.focused .terminal-toggle') ?? toggleButton()
      if (toggle && (toggle.getAttribute('aria-pressed') === 'true') !== (next === 'terminals')) toggle.click()
    }, 0)
  }, [])
  // A slot keeps its terminal view behind Home, so the rail takes its screen from the slot that just opened.
  useLayoutEffect(() => {
    if (!followSlot.current) return
    followSlot.current = false
    if (home) return
    const toggle = document.querySelector<HTMLButtonElement>('.slot.focused .terminal-toggle') ?? toggleButton()
    if (!toggle) return
    const shown: Screen = toggle.getAttribute('aria-pressed') === 'true' ? 'terminals' : 'chats'
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Read the committed slot DOM before paint so the rail matches its terminal view.
    setScreen(current => current === 'chats' || current === 'terminals' ? shown : current)
  }, [home, pane?.id])
  const screenKeys = useRef<{ files: boolean; home: boolean; choose: (next: Screen) => void; toggleSidebar: () => void } | null>(null)
  useEffect(() => { screenKeys.current = wide ? { files: files || share, home, choose: chooseScreen, toggleSidebar: () => setDesk({ sidebar: !desk.sidebar }) } : null })
  useEffect(() => {
    const open = (event: Event) => { const { path, select } = (event as CustomEvent<Partial<FilesTarget>>).detail ?? {}; if (!path) return; setFilesTarget({ path, select }); setScreen('files') }
    window.addEventListener('tether:open-files', open)
    return () => window.removeEventListener('tether:open-files', open)
  }, [])
  const live = useMemo(() => new Set(sessions.flatMap(session => session.panes.map(item => item.id))), [sessions])
  const owner = pane ? sessions.find(item => item.id === pane.tmux_session) : undefined
  const [marks, loadMarks] = useProjectMarks(api, panes.length > 0)
  // Files → "Pin to project": the open chat's session gets the file.
  const pinTarget = owner ? { label: `Pin to ${owner.name} project`, run: (path: string) => setProject({ session: owner.id, pin: path }) } : undefined
  // Each tmux session keeps its own layout. Selecting a pane that the current layout shows only moves focus;
  // selecting any other pane opens its own session's layout, so other sessions never land in this split.
  const deskKey = deskKeyFor(prefs.layout, pane?.id, owner?.id, sessions.map(item => item.id))
  const desk = deskFor(prefs.layout, deskKey)
  // Slots show only the layout session's panes, plus panes of other sessions the user placed (desk.guests).
  const deskSession = sessions.find(item => item.id === deskKey) ?? owner
  const sameSession = deskSession ? deskSession.panes.map(item => item.id).filter(id => id !== pane?.id) : []
  const view = wide && data && pane ? resolveSlots(desk, pane.id, live, sameSession) : null
  const slotPanes = view ? view.slots.map(id => panes.find(item => item.id === id)).filter((item): item is Pane => !!item) : []
  const multi = slotPanes.length > 1
  const panesRef = useRef(panes)
  panesRef.current = panes
  useEffect(() => {
    setPlaceHandler(place => {
      if (place.pane && panesRef.current.some(item => item.id === place.pane)) update(() => ({ selected: place.pane }))
      chooseScreen(place.screen)
    })
    return () => setPlaceHandler(null)
  }, [update, chooseScreen])
  const previousPlace = useRef<{ pane?: string; screen: Screen; slots: string[] }>({ screen, slots: [] })
  useEffect(() => {
    const before = previousPlace.current
    const focusOnly = before.screen === screen && before.pane !== pane?.id && !!before.pane && !!pane?.id && before.slots.includes(before.pane) && before.slots.includes(pane.id)
    setPlace({ pane: pane?.id, screen }, { push: !focusOnly })
  }, [pane?.id, screen])
  useEffect(() => { previousPlace.current = { pane: pane?.id, screen, slots: view?.slots ?? [] } })
  // Desktop "Needs you" list (Inbox.tsx). Its show/hide switch is the shared layout.desktop.inbox pref.
  const desktop = !!view
  const agentBrowser = useAgentBrowser(api)
  const asks = useMemo(() => browserAsks(agentBrowser), [agentBrowser])
  const inbox = useMemo(() => desktop ? inboxItems(panes, prefs.seen, openId, asks) : [], [desktop, panes, prefs.seen, openId, asks])
  const groups = useMemo(() => homeGroups(panes, sessions, prefs.seen, openId, asks), [panes, sessions, prefs.seen, openId, asks])
  // Attention mode: the tabs whose ask waits on you (oldest first). The Home badge counts them with the open prompts.
  const now = useNow()
  const waitSort = { order: prefs.waitingOrder ?? 'oldest', onOrder: (waitingOrder: WaitOrder) => update(() => ({ waitingOrder })) }
  const waiting = useMemo(() => prefs.attention ? waitingAsks(panes, now) : [], [prefs.attention, panes, now])
  const [deck, setDeck] = useUrgentDeck(waiting, prefs.attention)
  const waitingCount = new Set([...groups.needs.map(item => item.pane.id), ...waiting.map(item => item.pane.id)]).size
  const stacked = !!desk.stack && slotPanes.length === 2
  // Keep the stored slots in step with what shows, so focus moves and pane picks survive a reload.
  const slotKey = view ? `${view.slots.join(',')}|${view.focus}` : ''
  useEffect(() => {
    if (!slotKey) return
    const [ids, at] = slotKey.split('|')
    const visible = ids.split(','), focus = Number(at)
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      const list = storeSlots(stored.panes, visible)
      // A guest that left the screen is no longer placed here.
      const guests = stored.guests?.filter(id => visible.includes(id))
      if (list.join() === stored.panes.join() && stored.focus === focus && (guests?.length ?? 0) === (stored.guests?.length ?? 0)) return {}
      return { layout: withDesk(current.layout, deskKey, { ...stored, panes: list, focus, guests }) }
    })
  }, [slotKey, deskKey, update])
  const setDesk = (patch: Partial<DesktopLayout>) => update(current => ({ layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), ...patch }) }))
  // A layout that starts with the sidebar closed (Zed) closes it once, when you choose it. ⌘B shows it again.
  const lookId = lookKey(lookShown)
  const lookClosesSidebar = !!lookShown.grid.sessionsClosed
  const lastLook = useRef(lookId)
  useEffect(() => {
    if (lastLook.current === lookId) return
    lastLook.current = lookId
    if (lookClosesSidebar) update(current => ({ layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), sidebar: false }) }))
  }, [lookId, lookClosesSidebar, deskKey, update])
  // The panel column beside the slots (PanelColumn.tsx), saved with this session's layout.
  const setPanels: SetPanels = change => update(current => { const stored = deskFor(current.layout, deskKey); return { layout: withDesk(current.layout, deskKey, { ...stored, ...withPanels(stored, change(stored)) }) } })
  // "Watch" in a chat's browser card (AgentBrowser.tsx): show the Browser panel, or the full-screen sheet without a workspace.
  const watchRef = useRef({ panel: false, open: false, setPanels })
  watchRef.current = { panel: !!view, open: hasPanel(panelCols(desk), 'browser'), setPanels }
  useEffect(() => {
    const onWatch = () => {
      const { panel, open, setPanels: change } = watchRef.current
      if (!panel) setBrowserSheet(true)
      else if (!open) togglePanelKind(change, 'browser')
    }
    window.addEventListener(WATCH_EVENT, onWatch)
    return () => window.removeEventListener(WATCH_EVENT, onWatch)
  }, [])
  useEffect(() => setPaneLabels(panes.map(item => ({ id: item.id, label: [paneTitle(item), shownIndex(item)].filter(Boolean).join(' ') }))), [panes])
  const focusSlot = (index: number) => {
    const target = slotPanes[index]
    if (!view || !target) return
    setZoomed(false)
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      return { selected: target.id, lastPane: { ...current.lastPane, [target.tmux_session]: target.id }, layout: withDesk(current.layout, deskKey, { ...stored, panes: storeSlots(stored.panes, view.slots), focus: index }) }
    })
  }
  const setCount = (count: DesktopLayout['count'], stack = false) => {
    if (!view || !pane) return
    setZoomed(false)
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      const list = storeSlots([...sameSession, ...stored.panes], view.slots)
      const at = Math.max(0, list.indexOf(pane.id))
      if (at >= count) [list[at], list[count - 1]] = [list[count - 1], list[at]]
      return { layout: withDesk(current.layout, deskKey, { ...stored, count, stack, panes: list, focus: Math.min(at, count - 1) }) }
    })
  }
  const closeSlot = (index: number) => {
    if (!view || !pane || view.slots.length < 2) return
    setZoomed(false)
    const out = view.slots[index], rest = view.slots.filter(id => id !== out)
    const next = index === view.focus ? rest[Math.max(0, index - 1)] : pane.id
    update(current => {
      const stored = deskFor(current.layout, deskKey)
      // The closed pane is remembered right after the visible ones, so the next split brings it back.
      const list = [...rest, out, ...stored.panes.filter(id => !view.slots.includes(id))].slice(0, 4)
      return { selected: next, layout: withDesk(current.layout, deskKey, { ...stored, count: rest.length as DesktopLayout['count'], panes: list, focus: rest.indexOf(next) }) }
    })
  }
  const toggleZoom = () => { if (multi || zoomed) setZoomed(!zoomed) }
  /** The layout's guests after the user places pane `id` here: a pane of another session is remembered as placed on purpose. */
  const guestsWith = (stored: DesktopLayout, id: string) => {
    const from = panes.find(item => item.id === id)?.tmux_session
    return from && from !== deskKey && !stored.guests?.includes(id) ? [...(stored.guests ?? []), id] : stored.guests
  }
  // Manual picks: one pane into one slot, or every pane of a session across the slots.
  const showInSlot = (id: string, index: number) => {
    if (!view) return
    setZoomed(false)
    const list = placePane(view.slots, id, index)
    update(current => { const stored = deskFor(current.layout, deskKey); return { selected: id, layout: withDesk(current.layout, deskKey, { ...stored, panes: storeSlots(list, list), focus: index, guests: guestsWith(stored, id) }) } })
  }
  const showSession = (target: Session) => {
    if (!view) return
    setZoomed(false)
    const ids = target.panes.slice(0, 4).map(item => item.id)
    const focus = Math.max(0, ids.indexOf(pane?.id ?? ''))
    update(current => ({ selected: ids[focus], layout: withDesk(current.layout, target.id, { ...deskFor(current.layout, target.id), count: ids.length as DesktopLayout['count'], stack: false, panes: ids, focus }) }))
  }
  const [picking, setPicking] = useState<number | null>(null)
  // Drag and drop: a sidebar pane into a slot (or beside it), a session across the slots, or one slot onto another.
  // A side that does not fit the layout shows the pane in the slot instead.
  const dropPane = (id: string, index: number, side: Side | null) => {
    if (!view || !live.has(id)) return
    const shape = side && splitSlot(view.slots, stacked, index, side, id)
    if (!shape) return showInSlot(id, index)
    setZoomed(false)
    update(current => { const stored = deskFor(current.layout, deskKey); return { selected: id, layout: withDesk(current.layout, deskKey, { ...stored, ...shape, focus: shape.panes.indexOf(id), guests: guestsWith(stored, id) }) } })
  }
  // A past chat dropped on a slot: it opens as a tab of the slot's session, then shows there once the list has it.
  const pendingDrop = useRef<{ id: string; index: number; side: Side | null } | null>(null)
  useEffect(() => {
    const waiting = pendingDrop.current
    if (waiting && live.has(waiting.id)) { pendingDrop.current = null; dropPane(waiting.id, waiting.index, waiting.side) }
  })
  const dropChat = (data: string, index: number, side: Side | null) => {
    let chat: { id: string; agent: ChatAgent }
    try { chat = JSON.parse(data) } catch { return }
    const beside = view?.slots[index] ?? pane?.id
    if (!beside) return
    setNotice('Opening the chat…')
    resumeChat(api, chat, { after: beside }).then(opened => { pendingDrop.current = { id: opened.id, index, side }; setNotice(''); void refresh() }, failure => setNotice(errorText(failure)))
  }
  // The "Show in this window" items of the actions menu: each one only when it fits the layout.
  const placeFor = (id: string, all?: Session): Place | undefined => {
    if (!view) return undefined
    const fits = (side: Side) => splitSlot(view.slots, stacked, view.focus, side, id) ? () => dropPane(id, view.focus, side) : undefined
    if (all && all.panes.length > 1) return { all: () => showSession(all) }
    return { here: view.slots[view.focus] === id ? undefined : () => showInSlot(id, view.focus), right: fits('right'), below: fits('bottom') }
  }
  const dropSession = (id: string) => { const target = sessions.find(item => item.id === id); if (target) showSession(target) }
  const swap = (from: number, to: number) => {
    if (!view || !pane) return
    const list = swapSlots(view.slots, from, to)
    update(current => ({ layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), panes: storeSlots(list, list), focus: list.indexOf(pane.id) }) }))
  }
  // The global key handler reads the workspace through this ref, so it does not re-subscribe on each render.
  const workspaceKeys = useRef<{ count: number; focus: number; focusSlot: (index: number) => void; setCount: (count: DesktopLayout['count']) => void; closeSlot: (index: number) => void; toggleZoom: () => void; toggleSidebar: () => void; togglePanel: (kind: PanelKind) => void; sideChat: () => void; pane?: Pane } | null>(null)
  useEffect(() => {
    workspaceKeys.current = view ? { count: slotPanes.length, focus: view.focus, focusSlot, setCount, closeSlot, toggleZoom, toggleSidebar: () => setDesk({ sidebar: !desk.sidebar }), togglePanel: kind => togglePanelKind(setPanels, kind), sideChat: () => void sideChat(), pane } : null
  })
  useEffect(() => {
    const open = (id: unknown) => {
      if (typeof id !== 'string' || !id) return
      update(() => ({ selected: id }))
      leaveHome()
      setDrawer(false)
      void refresh()
      const url = new URL(location.href)
      url.searchParams.delete('session')
      history.replaceState(history.state, '', url)
    }
    open(new URL(location.href).searchParams.get('session'))
    const message = (event: MessageEvent) => { if (event.data?.go) setGo(String(event.data.go)); else open(event.data?.session) }
    if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', message)
    return () => { if ('serviceWorker' in navigator) navigator.serviceWorker.removeEventListener('message', message) }
  }, [update, refresh, leaveHome])
  useEffect(() => {
    const close = () => {
      if (!openId || document.visibilityState !== 'visible' || !('serviceWorker' in navigator)) return
      void navigator.serviceWorker.getRegistration('/chat/').then(async registration => {
        const notifications = await registration?.getNotifications({ tag: 'session-' + openId })
        notifications?.forEach(notification => notification.close())
      }).catch(() => {})
    }
    close()
    document.addEventListener('visibilitychange', close)
    return () => document.removeEventListener('visibilitychange', close)
  }, [openId])
  useEffect(() => {
    if (!go || (go === 'attention' && !data)) return
    const url = new URL(location.href)
    url.searchParams.delete('go')
    history.replaceState(history.state, '', url)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Consume the external navigation request after session data arrives and update history once.
    setGo(null)
    if (go === 'new') setNewSession(true)
    else if (go === 'usage') setPlanSheet(true)
    else if (go === 'agents') {
      setDrawer(false); setAgentsPage('list')
      void navigator.serviceWorker?.getRegistration('/chat/').then(async registration => (await registration?.getNotifications({ tag: 'tether-summary' }))?.forEach(item => item.close())).catch(() => {})
    }
    else if (go === 'attention') {
      const target = waiting[0]?.pane ?? panes.find(item => needsYou(item.state)) ?? panes.find(item => isDone(item, prefs.seen, prefs.selected ?? undefined))
      if (target) { update(() => ({ selected: target.id })); leaveHome() }
      else setNotice('Nothing needs you right now.')
    }
  }, [go, data, panes, waiting, prefs.selected, prefs.seen, update, leaveHome])
  // Mark the panes on screen as seen, again whenever a turn ends while you watch, so only turns you missed show Done.
  // Home shows no chat, so a turn that ends behind it stays unread.
  const watched = (home ? [] : view && !zoomed ? slotPanes : pane ? [pane] : []).map(item => `${item.id} ${item.finished ?? ''}`).join('|')
  useEffect(() => {
    if (!watched) return
    const now = Date.now() / 1000
    update(current => ({ seen: { ...current.seen, ...Object.fromEntries(watched.split('|').map(item => [item.split(' ')[0], now])) } }))
  }, [watched, update])
  // Restore default: Classic, flat, at once, with Undo for 10 seconds (lookTrial.ts).
  const restoreClassic = () => {
    if (lookKey(lookShown) === 'classic' && look.surface !== 'glass') return setNotice('Classic is already on.')
    startTrial(prefs.look ?? null, 'undo', 'Classic')
    update(() => ({ look: DEFAULT_LOOK }))
  }
  const revertLook = useCallback((previous: Look | null) => update(() => ({ look: previous ?? DEFAULT_LOOK })), [update])
  const lookKeys = useRef<{ maker: () => void; classic: () => void } | null>(null)
  useEffect(() => { lookKeys.current = { maker: () => { setDrawer(false); setSettings('layout-maker') }, classic: restoreClassic } })
  // Desktop keys: Ctrl/Cmd+K searches sessions, Alt+Up/Down opens the session above or below,
  // and typing with nothing focused goes to the message box.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = (event.target as Element | null)?.closest('input, textarea, select, [contenteditable="true"], dialog')
      const rail = screenKeys.current
      // The workspace behind Home takes no keys. ⌘K then searches the sidebar.
      const slots = rail?.home ? null : workspaceKeys.current
      // ⌘1–4 (Ctrl off a Mac) pick Chats, Files, Terminals or Share. A browser tab may keep these keys; an installed app window gets them.
      const screenKey = /^Digit([1-4])$/.exec(event.code)
      if (rail && screenKey && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && !document.querySelector('dialog[open]')) {
        event.preventDefault()
        rail.choose((['chats', 'files', 'terminals', 'share'] as const)[Number(screenKey[1]) - 1])
        return
      }
      if (rail && event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.code === 'KeyH' && !document.querySelector('dialog[open]')) {
        event.preventDefault()
        rail.choose('home')
        return
      }
      // ⌥⇧] and ⌥⇧[ go to the next or previous group of the focused tab's session. Read event.code: on a Mac the keys type ” and “,
      // so a text field or an open dialog keeps them and gets the character.
      if (event.altKey && event.shiftKey && !event.ctrlKey && !event.metaKey && (event.code === 'BracketRight' || event.code === 'BracketLeft')) {
        const target = event.target as HTMLElement | null
        const typing = !!target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
        if (!typing && !document.querySelector('dialog[open]') && groupKeys.current(event.code === 'BracketRight' ? 1 : -1)) event.preventDefault()
        return
      }
      if (event.altKey && !event.ctrlKey && !event.metaKey && (event.code === 'BracketRight' || event.code === 'BracketLeft')) {
        if (pinKeys.current(event.code === 'BracketRight' ? 1 : -1)) { event.preventDefault(); return }
      }
      // ⌥V turns voice input on or off, also while typing. Read event.code: Alt+V types √ on a Mac.
      // ⌥L makes a layout (Settings › Appearance); ⌥⇧L goes back to Classic from anywhere, even from a broken layout.
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.code === 'KeyL' && !document.querySelector('dialog[open]')) {
        event.preventDefault()
        if (event.shiftKey) lookKeys.current?.classic()
        else lookKeys.current?.maker()
        return
      }
      if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.code === 'KeyV' && !document.querySelector('dialog[open]')) {
        if (toggleVoice()) event.preventDefault()
        return
      }
      // The Files and Share screens handle its own keys; the hidden workspace must not take them.
      if (rail?.files) return
      if (rail && !document.querySelector('dialog[open]')) {
        const mod = event.ctrlKey || event.metaKey
        const act = (run: () => void) => { event.preventDefault(); run() }
        if (mod && !event.altKey && !event.shiftKey && event.code === 'KeyB') return act(rail.toggleSidebar)
        if (event.altKey && !mod && !event.shiftKey && event.code === 'KeyN') return act(() => setNewSession(true))
        if (event.key === '?' && !typing && !mod && !event.altKey) return act(() => setMenu('shortcuts'))
      }
      if (slots && !document.querySelector('dialog[open]')) {
        // Browsers keep Cmd+1-9, Cmd+W and Cmd+N, so slot keys use Alt. Read event.code: Alt+letter types a symbol on a Mac.
        const mod = event.ctrlKey || event.metaKey
        const act = (run: () => void) => { event.preventDefault(); run() }
        if (mod && !event.altKey && event.code === 'Backslash') return act(() => slots.setCount(event.shiftKey ? 4 : Math.min(3, Math.max(2, slots.count + 1)) as DesktopLayout['count']))
        if (mod && !event.altKey && !event.shiftKey && event.code === 'Period') return act(() => slots.togglePanel('terminal'))
        if (mod && !event.altKey && !event.shiftKey && event.code === 'Semicolon') return act(slots.sideChat)
        if (event.altKey && !mod && !event.shiftKey) {
          const digit = /^Digit([1-4])$/.exec(event.code)
          // Leave the old slot's message box, so typing goes to the newly focused slot.
          if (digit && Number(digit[1]) <= slots.count) return act(() => { (document.activeElement as HTMLElement | null)?.blur(); slots.focusSlot(Number(digit[1]) - 1) })
          if (event.code === 'KeyZ') return act(slots.toggleZoom)
          if (event.code === 'KeyX' && slots.count > 1) return act(() => slots.closeSlot(slots.focus))
          if (event.code === 'KeyT') return act(toggleTerminal)
        }
        // ⌥⇧S, C, P and T copy a name of the focused pane: the pane menu's Copy name keys (CopyNames.tsx).
        const copyKind = event.altKey && event.shiftKey && !mod ? nameKindOf(event.code) : undefined
        if (copyKind && slots.pane) { const target = slots.pane; return act(() => void copyPaneName(target, copyKind).then(setNotice)) }
      }
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        // The workspace opens the command menu; ⌘K again closes it. The phone keeps the sidebar search.
        if (slots) { setMenu(current => current === 'commands' ? null : document.querySelector('dialog[open]') ? current : 'commands'); return }
        const input = document.querySelector<HTMLInputElement>('.sidebar .sidebar-search input')
        if (input?.offsetParent) input.select()
        else { setDrawer(true); setTimeout(() => document.querySelector<HTMLInputElement>('.drawer .sidebar-search input')?.focus(), 50) }
        return
      }
      if (event.altKey && !event.ctrlKey && !event.metaKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown') && !document.querySelector('dialog[open]')) {
        // Every row that opens a pane: one-pane sessions and the pane rows under a session with several panes.
        const rows = [...document.querySelectorAll<HTMLButtonElement>('.sidebar .session-groups .session-row:not(.session-head) .session-main, .sidebar .session-groups .pane-children button')].filter(row => row.offsetParent)
        const at = rows.findIndex(row => row.getAttribute('aria-current') === 'page')
        const next = rows[at < 0 ? 0 : at + (event.key === 'ArrowDown' ? 1 : -1)]
        if (next) { event.preventDefault(); next.click(); next.scrollIntoView({ block: 'nearest' }) }
        return
      }
      if (!typing && event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey && !document.querySelector('dialog[open]')) {
        const box = document.querySelector<HTMLTextAreaElement>('.slot.focused form.composer textarea') ?? document.querySelector<HTMLTextAreaElement>('form.composer textarea')
        if (box && !box.disabled && !box.readOnly) box.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const drawerModal = useModal(drawer, () => setDrawer(false), { onInterrupt: () => setDrawer(true) })
  const [drawerPresent, setDrawerPresent] = useState(false)
  if (drawer && !drawerPresent) setDrawerPresent(true)
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 5000); return () => clearTimeout(timer) }, [notice])
  // Files explorer → chat: "Add to message" puts @paths in the active chat's draft; "Terminal here" opens a side shell at a folder.
  const addToMessage = (paths: string[]) => {
    if (!pane || !paths.length) return setNotice('Open a chat first. Then add files to its message.')
    const old = prefs.drafts[pane.id]?.text ?? ''
    setDraft(pane.id, old + (old && !/\s$/.test(old) ? ' ' : '') + mentionText(paths))
    chooseScreen('chats')
    setNotice(`Added ${paths.length === 1 ? paths[0] : `${paths.length} items`} to the message for ${paneTitle(pane)}.`)
    setTimeout(() => {
      const box = document.querySelector<HTMLTextAreaElement>('.slot.focused form.composer textarea') ?? document.querySelector<HTMLTextAreaElement>('form.composer textarea')
      if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length) }
    }, 50)
  }
  const terminalHere = (folder: string) => {
    if (!view) return setNotice('Open a chat first. The terminal opens in the panel beside it.')
    setPanels(current => ({ cols: addPanel(panelCols(current), { id: newPanelId(), kind: 'terminal', folder, fresh: true }) }))
    chooseScreen('chats')
  }
  // The sidebar Shells list: show a shell in a Terminal panel (once), and close the panels of shells that ended.
  const openShell = (shell: string) => {
    setPanels(current => panelCols(current).some(col => col.items.some(item => item.kind === 'terminal' && item.pane === shell)) ? {}
      : { cols: addPanel(panelCols(current), { id: newPanelId(), kind: 'terminal', pane: shell }) })
    chooseScreen('chats')
  }
  const shellsEnded = (panes: string[]) => setPanels(current => withoutShells(current, panes))
  const openPane = (target: Pane) => {
    update(current => ({ selected: target.id, lastPane: { ...current.lastPane, [target.tmux_session]: target.id } }))
    setDrawer(false)
    leaveHome()
  }
  const archiveTab = (target: Pane) => {
    const owner = sessions.find(session => session.panes.some(item => item.id === target.id))
    if (!owner) return
    if (owner.panes.length <= 1) { update(toggleArchive(owner)); return }
    const neighbor = tabNeighbor(owner, target.id)!
    update(current => ({
      archivedTabs: withTabArchived(current.archivedTabs, target, new Set(panes.map(item => item.id))),
      ...(pane?.id === target.id && { selected: neighbor.id }),
      ...((pane?.id === target.id || current.lastPane[owner.id] === target.id) && { lastPane: { ...current.lastPane, [owner.id]: neighbor.id } }),
    }))
  }
  const restoreTab = (target: Pane) => {
    update(current => {
      const archivedTabs = withoutTab(current.archivedTabs, target.id)
      const owner = groupSessions(panes, item => isTabArchived(item, archivedTabs) && item.id !== current.selected).find(session => session.id === target.tmux_session)
      return { archivedTabs, ...(owner && owner.panes.length === 1 && isArchived(owner, current.archived) ? toggleArchive(owner)(current) : {}) }
    })
  }
  const pinPane = (id: string) => {
    if (prefs.sidePins.some(pin => pin.pane === id)) return
    // At the cap, pins of closed panes make room; they cannot be unpinned from the tab.
    const room = (pins: SidePin[]) => pins.length < 40 ? pins : pins.filter(pin => panes.some(item => item.id === pin.pane))
    if (room(prefs.sidePins).length >= 40) { setNotice('Up to 40 chats can be pinned.'); return }
    update(current => { const kept = room(current.sidePins); return kept.some(pin => pin.pane === id) || kept.length >= 40 ? {} : { sidePins: [{ pane: id }, ...kept] } })
    setNotice('Pinned to the side tab')
  }
  const unpinPane = (id: string) => {
    update(current => ({ sidePins: current.sidePins.filter(pin => pin.pane !== id) }))
    setNotice('Unpinned from the side tab')
  }
  const notePin = (id: string, text: string) => update(current => ({ sidePins: current.sidePins.map(pin => pin.pane === id ? { pane: id, ...(text ? { note: text.slice(0, 60) } : {}) } : pin) }))
  const movePin = (id: string, to: string) => update(current => {
    const sidePins = [...current.sidePins], from = sidePins.findIndex(pin => pin.pane === id), at = sidePins.findIndex(pin => pin.pane === to)
    if (from < 0 || at < 0 || from === at) return {}
    sidePins.splice(at, 0, sidePins.splice(from, 1)[0])
    return { sidePins }
  })
  const stepPin = (dir: 1 | -1, from = pane?.id) => {
    if (!livePins.length) return false
    const at = livePins.findIndex(item => item.pane.id === from)
    openPane(livePins[at < 0 ? 0 : (at + dir + livePins.length) % livePins.length].pane)
    setAgentsPage(false)
    chooseScreen('chats')
    return true
  }
  useEffect(() => { pinKeys.current = stepPin })
  // The next group (with a tab) of the focused tab's session; it opens the tab last open there on this device.
  const stepGroup = (dir: 1 | -1) => {
    const owner = pane && sessions.find(item => item.id === pane.tmux_session)
    if (!pane || !owner?.groups?.length) return false
    const next = stepPlace(placesOf(owner.panes, owner.groups).filter(place => place.panes.length), groupOf(pane, owner.groups), dir)
    const target = next && tabFor(owner.id, next)
    if (!target || target.id === pane.id) return false
    openPane(target)
    return true
  }
  useEffect(() => { groupKeys.current = stepGroup })
  const openById = (id: string) => { update(() => ({ selected: id })); setDrawer(false); leaveHome(); void refresh() }
  // A new tmux pane. On the desktop workspace it opens in a new slot beside the focused one, on the same side as the tmux split.
  // The list is fetched first so the slot keeps it.
  const openNewPane = async (id: string, direction: 'right' | 'down' = 'right') => {
    setDrawer(false)
    setScreen(current => current === 'home' ? 'chats' : current)
    if (!view) return openById(id)
    await refresh()
    const shape = splitSlot(view.slots, stacked, view.focus, direction === 'down' ? 'bottom' : 'right', id)
    const next = shape ? { ...shape, focus: shape.panes.indexOf(id) } : { ...addSlot(view.slots, view.focus, id), stack: false }
    setZoomed(false)
    update(current => ({ selected: id, layout: withDesk(current.layout, deskKey, { ...deskFor(current.layout, deskKey), ...next }) }))
  }
  // `group` puts the new tab in that group ('' = Unstacked); without it the tab joins the group of `target`.
  async function split(direction: 'right' | 'down', target = pane, group?: string) {
    if (!target) return
    try { await openNewPane((await api<{ pane: string }>('split', { session: target.id, direction, ...(group !== undefined ? { group } : {}) })).pane, direction) } catch (error) { setNotice(errorText(error)) }
  }
  // A copy of the chat with its full history, in a new window of the same tmux session (claude --fork-session, codex fork).
  async function fork(target: Pane) {
    try {
      const result = await api<{ pane: string; name: string }>('fork', { session: target.id })
      await openNewPane(result.pane, 'right')
      setNotice(`Opened “${result.name}” with this chat’s history.`)
    } catch (error) { setNotice(errorText(error)) }
  }
  // ⌘; and ⋯ › Side chat: a fork of the focused chat, shown in the panel column instead of a slot. Again closes the panel; the fork keeps running.
  async function sideChat() {
    if (!view || !pane) return
    if (hasPanel(panelCols(desk), 'chat')) return setPanels(current => ({ cols: panelCols(current).flatMap(col => col.items).filter(item => item.kind === 'chat').reduce((out, item) => removePanel(out, item.id), panelCols(current)) }))
    if (!canFork(pane)) return setNotice('A side chat needs a Claude or Codex chat with a first message.')
    try {
      const result = await api<{ pane: string; name: string }>('fork', { session: pane.id })
      await refresh()
      setPanels(current => ({ cols: addPanel(panelCols(current), { id: newPanelId(), kind: 'chat', pane: result.pane }) }))
      setNotice(`Opened “${result.name}” beside this chat, with its history.`)
    } catch (error) { setNotice(errorText(error)) }
  }
  const actionSession = sessions.find(item => item.id === actions)
  const ordered = sortByPrefs(sessions, prefs.order).map(item => item.id)
  // Sessions a pane or a session can move into from the ⋯ menus (the keyboard path for drag to merge).
  const moveTargets = sortByPrefs(sessions, prefs.order).filter(item => !isArchived(item, prefs.archived))
  const move = (id: string, step: -1 | 1) => update(current => {
    const order = sortByPrefs(sessions, current.order).map(item => item.id)
    const at = order.indexOf(id), to = at + step
    if (at < 0 || to < 0 || to >= order.length) return {}
    ;[order[at], order[to]] = [order[to], order[at]]
    return { order }
  })
  const pickPastChat = (chat: PastChat) => {
    const open = chat.live ? panes.find(item => item.id === chat.pane) ?? panes.find(item => item.thread === chat.id) : undefined
    if (open) openPane(open)
    else { setDrawer(false); setResume(chat) }
  }
  const canMessage = sessions.some(item => item.panes.some(p => p.agent !== 'shell' && p.writable))
  const words = useMemo(() => wordsFor(prefs.tmuxWords), [prefs.tmuxWords])
  const sidebarProps = { onDeck: () => setDeck(true), archivedTabs, onRestoreTab: restoreTab, api, sessions, marks, onMarks: loadMarks, selected: openId, slots: multi && !home ? view?.slots : undefined, drag: !!view && !home, onDropPane: view && !home ? dropPane : undefined, onDropSession: view && !home ? dropSession : undefined, onDropChat: view && !home ? dropChat : undefined, prefs, update, loading, loaded: !!data, error, onRetry: () => refresh(), onReload: reload, onOpen: openPane, onNew: () => { setDrawer(false); setNewSession(true) }, onActions: (item: Session, remove?: boolean, at?: Anchor) => { if (remove) setDrawer(false); setMenuAt(view ? at : undefined); setActionsDelete(!!remove); setActions(item.id) }, onPaneActions: (item: Pane, at?: Anchor) => { setDrawer(false); setMenuAt(view ? at : undefined); setPaneActions(item.id) }, onPastChat: pickPastChat, onSettings: () => { setDrawer(false); setSettings(true) }, onBacklog: () => { setDrawer(false); setBacklog(true) }, onCommandMenu: view && !home ? () => setMenu('commands') : undefined, onMessageAll: canMessage ? () => { setDrawer(false); setAgentsPage('message') } : undefined, onOpenShell: view ? openShell : undefined, onShellsEnded: shellsEnded, onAddPane: (item: Session, at: Anchor) => { setDrawer(false); setTabTask({ pane: item.panes.find(entry => entry.id === prefs.lastPane[item.id]) ?? item.panes[0], at }) } }
  // The command menu runs the same actions as the keys. A command shows only when it would do something now.
  const commands: Command[] = view ? [
    { label: 'Home', key: '⌥H', run: () => chooseScreen('home') },
    ...(slotPanes.length < 3 ? [{ label: 'Split right', key: '⌘\\', run: () => setCount(Math.max(2, slotPanes.length + 1) as DesktopLayout['count']) }] : []),
    ...(slotPanes.length < 4 ? [{ label: 'Grid of four', key: '⌘⇧\\', run: () => setCount(4) }] : []),
    ...(multi || zoomed ? [{ label: zoomed ? 'Restore the split' : 'Zoom the focused slot', key: '⌥Z', run: toggleZoom }] : []),
    ...(multi ? [{ label: 'Close the focused slot', key: '⌥X', run: () => closeSlot(view.focus) }] : []),
    { label: 'Terminal view of the focused pane', key: '⌥T', run: toggleTerminal },
    ...(pane ? [...TOOLBAR_PANELS.map(({ kind, key }) => ({ label: `${hasPanel(panelCols(desk), kind) ? 'Hide' : 'Show'} the ${PANELS[kind].title.toLowerCase()} panel`, key, run: () => togglePanelKind(setPanels, kind) })),
      ...extraPanels(desk, pane).map(({ kind, key, open }) => ({ label: open ? `Hide the ${PANELS[kind].title.toLowerCase()} panel` : kind === 'chat' ? 'Side chat: fork this chat beside it' : `Show ${PANELS[kind].title.toLowerCase()}`, key, run: () => kind === 'chat' ? void sideChat() : togglePanelKind(setPanels, kind) }))] : []),
    ...(pane && placesOf(sessions.find(item => item.id === pane.tmux_session)?.panes ?? [], sessions.find(item => item.id === pane.tmux_session)?.groups).filter(place => place.panes.length).length > 1 ? [{ label: 'Next stack', key: '⌥⇧]', run: () => stepGroup(1) }, { label: 'Previous stack', key: '⌥⇧[', run: () => stepGroup(-1) }] : []),
    ...(livePins.length > 1 ? [{ label: 'Next pinned chat', key: '⌥]', run: () => stepPin(1) }, { label: 'Previous pinned chat', key: '⌥[', run: () => stepPin(-1) }] : []),
    { label: 'New session', key: '⌥N', run: () => setNewSession(true) },
    { label: desk.sidebar ? 'Hide the sidebar' : 'Show the sidebar', key: '⌘B', run: () => setDesk({ sidebar: !desk.sidebar }) },
    { label: 'Keyboard shortcuts', key: '?', run: () => setMenu('shortcuts') },
    { label: 'Make a layout', key: '⌥L', run: () => setSettings('layout-maker') },
    { label: 'Restore the default layout', key: '⌥⇧L', run: restoreClassic },
    { label: 'Plan usage', run: () => setPlanSheet(true) },
    { label: 'Backlog', run: () => setBacklog(true) },
    { label: 'Agents page', run: () => setAgentsPage('list') },
    ...(canMessage ? [{ label: 'Message all sessions', run: () => setAgentsPage('message') }] : []),
    { label: `${ASSISTANT} Live mode: talk hands-free`, run: openLive },
  ] : []
  const running = runningPanes(sessions).length
  const chatFor = (target: Pane, slot?: SlotControls) => {
    const owner = sessions.find(item => item.id === target.tmux_session)
    const at = livePins.findIndex(item => item.pane.id === target.id)
    const sidePin = target.agent !== 'shell' ? { pinned: at >= 0, index: at, total: livePins.length, note: at >= 0 ? livePins[at].pin.note ?? '' : '', onToggle: () => at >= 0 ? unpinPane(target.id) : pinPane(target.id), onNote: (text: string) => notePin(target.id, text), onStep: (dir: 1 | -1) => stepPin(dir, target.id) } : undefined
    const ask = prefs.attention ? visibleAsk(target, now) : null
    const groupHooks: GroupHooks | undefined = owner?.groups?.length ? {
      seen: prefs.seen, selected: openId, hidden: archivedTabs.filter(item => item.tmux_session === owner.id),
      onStart: (group, from) => void split('right', from, group),
      onContext: group => setProject({ session: owner.id, group: group.id }),
    } : undefined
    const onDropGroup = owner ? (id: string, group: string) => { api('groups/move', { session: owner.id, pane: id, group: group || null }).then(() => refresh(), error => setNotice(errorText(error))) } : undefined
    return <Chat onBacklogAdd={(text, from) => setBacklogAdd({ text, pane: from })} headerExtra={view ? undefined : <OldestButton compact sort={waitSort} onDeck={() => setDeck(true)} items={waiting} sessions={sessions} selected={openId} onOpen={openPane} />}
      groupHooks={groupHooks} onDropGroup={onDropGroup} askBar={ask && target.writable ? <AskChips api={api} pane={target} ask={ask} onDone={setNotice} onUpdate={() => void refresh()} /> : undefined} allPanes={panes} sidePin={sidePin} liveReplyPreview={prefs.liveReplyPreview} key={target.id} pane={target} session={owner} api={api} onMenu={() => setDrawer(true)} onUpdate={() => void refresh()} draft={prefs.drafts[target.id]?.text ?? ''} setDraft={text => setDraft(target.id, text)} onOpenPane={openPane} onSessionActions={at => { if (owner) { setMenuAt(view ? at : undefined); setActionsDelete(false); setActions(owner.id) } }} onPaneActions={(id, at) => { setMenuAt(view ? at : undefined); setPaneActions(id) }} onSplit={direction => void split(direction, target)} onFork={() => void fork(target)} onContinued={id => void openNewPane(id).catch(error => setNotice(errorText(error)))} onReorder={ids => { if (owner) update(current => ({ paneOrder: { ...current.paneOrder, [owner.id]: reorderPanes(groupSessions(panes).find(item => item.id === owner.id)!.panes, current.paneOrder[owner.id], ids) } })) }} micOk={!prefs.voiceOff} chime={prefs.voiceSound} readAloud={prefs.readAloud} handsFree={prefs.handsFree} slot={slot} running={running} />
  }
  // Layouts (layouts.ts, layouts.css): the grid shows on the wide workspace screen only. Home, Files, Share and the
  // phone keep today's arrangement and take only the colours and the Style. Classic adds no attribute and no style.
  const lookGrid = !lookKeep && lookShown.id !== 'classic' && !!view && !home && !files && !(wide && share)
  // Panel sizes and closed panels (PanelHandles.tsx), per layout on this device. Only the wide workspace uses them.
  const panelsOn = wide && !!view && !home && !files && !share
  const panelSizes = panelsOn ? sizeVars(prefs.panelSizes[lookId]) : {}
  const panelsClosed = lookGrid ? (prefs.panelsClosed[lookId] ?? []).filter(name => layoutHas(lookShown, name as PanelName)) : []
  const showPanel = (name: PanelName) => lookGrid && layoutHas(lookShown, name) && !panelsClosed.includes(name)
  const lookStyle = lookKeep || lookShown.id === 'classic' ? (Object.keys(panelSizes).length ? panelSizes as CSSProperties : undefined) : {
    '--l-radius': `${lookShown.style.radius}px`, '--l-gap': `${lookShown.style.gap}px`, ...lookShown.grid.chatWidth ? { '--l-chat': `${lookShown.grid.chatWidth}px` } : {}, ...panelSizes,
    ...lookGrid ? gridStyle(lookShown, { rail: wide, sessions: desk.sidebar && layoutHas(lookShown, 'sessions'), inbox: desk.inbox, closed: panelsClosed }) : {},
  } as CSSProperties
  // Glass options (Settings › Style): only safe values reach CSS. The photo URL must be one of Tether's own backgrounds.
  const glass = deviceLook.surface === 'glass'
  const tint = glass && deviceLook.tint && /^#[0-9a-f]{6}$/i.test(deviceLook.tint) ? deviceLook.tint : undefined
  const photo = glass && deviceLook.backdrop === 'photo' && deviceLook.photo && PHOTO_URL.test(deviceLook.photo.url) ? deviceLook.photo.url : undefined
  const fill = glass && typeof deviceLook.fill === 'number' ? Math.min(0.9, Math.max(0.3, deviceLook.fill)) : undefined
  const blur = glass && typeof deviceLook.blur === 'number' ? Math.min(40, Math.max(0, Math.round(deviceLook.blur))) : undefined
  const glassVars = { ...tint ? { '--g-tint': tint } : {}, ...photo ? { '--g-photo': `url("${photo}")` } : {}, ...fill !== undefined ? { '--g-alpha': `${Math.round(fill * 100)}%` } : {}, ...blur !== undefined ? { '--g-blur': `${blur}px` } : {} }
  const shellStyle = Object.keys(glassVars).length ? { ...lookStyle, ...glassVars } as CSSProperties : lookStyle
  const backdrop = deviceLook.backdrop === 'photo' ? (photo ? 'photo' : 'ember') : ['graphite', 'dusk', 'colour'].includes(deviceLook.backdrop) ? deviceLook.backdrop : 'ember'
  const lookAttrs = {
    ...variantAttrs(lookKeep ? { ...lookShown, variants: undefined } : lookShown),
    'data-layout': lookShown.id === 'classic' || !wide ? undefined : lookId.startsWith('custom:') ? 'custom' : lookShown.id,
    'data-grid': lookGrid ? '' : undefined, 'data-gap': lookGrid && lookShown.style.gap > 0 ? '' : undefined,
    'data-tabs': lookGrid && !layoutHas(lookShown, 'agents') ? 'off' : undefined, 'data-centre': lookGrid && lookShown.grid.chatWidth ? '' : undefined,
    'data-activity': lookGrid && layoutHas(lookShown, 'activity') ? '' : undefined,
    'data-sessions': lookGrid && !layoutHas(lookShown, 'sessions') ? 'off' : undefined,
    'data-waiting': lookGrid && layoutHas(lookShown, 'waiting') ? '' : undefined,
    'data-surface': deviceLook.surface === 'glass' ? 'glass' : undefined, 'data-backdrop': glass ? backdrop : undefined,
    'data-tint': tint ? '' : undefined, 'data-fill': fill !== undefined ? '' : undefined,
  }
  if (unauthorized) return <main className="signin"><Terminal size={32} strokeWidth={1.4} /><h1>Sign in</h1><p>Sign in to access your terminal sessions.</p><a className="primary" href="/mobile/login?next=/chat/">Sign in</a></main>
  return <WordsContext.Provider value={words}><main className={['app-shell', (view || (wide && home)) && !desk.sidebar && 'no-sidebar', wide && 'with-rail', files && 'screen-files', wide && share && 'screen-share', home && wide && 'screen-home', home && !wide && 'phone-home', !wide && 'phone-tabs', phoneFiles && 'phone-files', !wide && share && 'phone-share'].filter(Boolean).join(' ')} style={shellStyle} {...lookAttrs}>{wide && <Rail screen={home ? 'home' : screen} waiting={waitingCount} onScreen={chooseScreen} onLive={openLive} />}<aside className="sidebar"><Sidebar {...sidebarProps} /></aside><dialog {...drawerModal} className="drawer" aria-label="Sessions" onClick={event => { const box = event.currentTarget.getBoundingClientRect(); if (event.target === event.currentTarget && (event.clientX > box.right || event.clientX < box.left || event.clientY < box.top || event.clientY > box.bottom)) setDrawer(false) }} onClose={() => setDrawerPresent(false)}>{(drawer || drawerPresent) && <Sidebar {...sidebarProps} onClose={() => setDrawer(false)} />}</dialog>
    {pane ? (view ? <Workspace layout={desk} slots={slotPanes} focus={view.focus} zoomed={zoomed && multi} onCount={setCount} onFocus={focusSlot} onLayout={setDesk}
      renderSlot={(target, index, shown) => chatFor(target, shown > 1 || zoomed ? { index, zoomed, onZoom: toggleZoom, onClose: zoomed ? undefined : () => closeSlot(index), onPick: () => setPicking(index), onDragStart: event => { event.dataTransfer.setData(DRAG_SLOT, String(index)); event.dataTransfer.effectAllowed = 'move' } } : undefined)}
      onDropPane={dropPane} onDropSession={dropSession} onDropChat={dropChat} onSwap={swap}
      tools={<>
        {owner && owner.panes.length > 1 && <button type="button" className="icon-button" aria-label={`Show all panes of ${owner.name}`} title={`Show all panes of ${owner.name}`} onClick={() => showSession(owner)}><LayoutDashboard size={17} /></button>}
        <button type="button" className="icon-button" aria-label={words.newRight} title={words.tmux ? 'New pane right (tmux split, opens in a new slot)' : 'New tab, opens in a new slot'} onClick={() => void split('right')}><SquareSplitHorizontal size={17} /></button>
        <button type="button" className="icon-button" aria-label={words.newBelow} title={words.tmux ? 'New pane below (tmux split, opens in a new slot)' : 'New tab below, opens in a new slot'} onClick={() => void split('down')}><SquareSplitVertical size={17} /></button>
        <OldestButton items={waiting} sessions={sessions} selected={openId} onOpen={openPane} onDeck={() => setDeck(true)} />
        <InboxButton count={inbox.length} open={desk.inbox} onToggle={() => setDesk({ inbox: !desk.inbox })} />
      </>}
      panelTools={<PanelTools desk={desk} pane={pane} setPanels={setPanels} onSideChat={() => void sideChat()} />}
      panels={<PanelColumn api={api} desk={desk} pane={pane} setPanels={setPanels} renderChat={id => { const target = panes.find(item => item.id === id); return target ? chatFor(target) : null }} />} /> : chatFor(pane)) : null}
    {pane && view && wide && <BrowserFloat api={api} pane={pane} onDock={() => { if (!hasPanel(panelCols(desk), 'browser')) togglePanelKind(setPanels, 'browser') }} onClose={() => { if (hasPanel(panelCols(desk), 'browser')) togglePanelKind(setPanels, 'browser') }} />}
    {showPanel('waiting') && <WaitingPanel api={api} cards={groups.needs} asks={prefs.attention && waiting.length ? <WaitingBox items={waiting} sessions={sessions} selected={openId} limit={0} sort={waitSort} onOpen={openPane} onDeck={() => setDeck(true)} /> : undefined} edge={lookShown.grid.columns.some(column => (column.top === 'waiting' || column.bottom === 'waiting') && (column.main !== 'sessions' || desk.sidebar))} onOpen={openPane} onUpdate={() => void refresh()} />}
    {showPanel('status') && <StatusColumn groups={groups} panes={panes} sessions={sessions} selected={openId} onOpen={openPane} />}
    {home && <Home api={api} groups={groups} asks={waiting} sessions={sessions} sort={waitSort} hasAgents={hasAgents(panes)} loaded={!!data} loading={loading} error={error} onRetry={() => void refresh()} onOpen={openPane} onMarkRead={markRead} onUpdate={() => void refresh()} onNew={() => setNewSession(true)} onMenu={() => setDrawer(true)} onBacklog={() => setBacklog(true)} />}
    {files && <Explorer api={api} panes={panes} pane={pane} target={filesTarget} onTarget={() => setFilesTarget(null)} onAddToMessage={addToMessage} onTerminalHere={terminalHere} onPin={pinTarget} />}
    {share && <Share wide={wide} onAddToMessage={wide ? addToMessage : pane ? paths => {
      const before = prefs.drafts[pane.id]?.text ?? ''
      setDraft(pane.id, before + (before && !/\s$/.test(before) ? ' ' : '') + mentionText(paths))
      choosePhoneScreen('chats', setScreen)
    } : undefined} />}
    {phoneFiles && <ExplorerPhone api={api} target={filesTarget?.path ?? null} onTarget={() => setFilesTarget(null)} onPin={pinTarget} chatName={pane ? (pane.pane_name || owner?.name || pane.label) : undefined}
      onAddToChat={pane ? paths => {
        // "Add to chat": append @~/path to the open chat's unsent draft, then go back to the chat.
        const before = prefs.drafts[pane.id]?.text ?? ''
        setDraft(pane.id, before + (before && !/\s$/.test(before) ? ' ' : '') + mentionText(paths))
        choosePhoneScreen('chats', setScreen)
        setNotice(paths.length === 1 ? `Added ${paths[0]} to the message.` : `Added ${paths.length} paths to the message.`)
      } : undefined} />}
    {!home && prefs.agentBubble && sessions.some(item => item.panes.some(p => p.agent !== 'shell')) && <AgentBubble sessions={sessions} seen={prefs.seen} selected={openId} onOpen={openPane} onMarkRead={markRead} onPage={() => setAgentsPage('list')} onHide={() => { update(() => ({ agentBubble: false })); setNotice('Agents bubble hidden. Turn it on in Settings › Chat.') }} />}
    {livePins.length > 0 && <SidePins api={api} pins={prefs.sidePins} sessions={sessions} seen={prefs.seen} selected={openId} onOpen={openPane} onPin={pinPane} onUnpin={unpinPane} onNote={notePin} onMove={movePin} onUpdate={() => void refresh()} />}
    {!wide && <PhoneTabs screen={home ? 'home' : screen} waiting={waitingCount} onScreen={next => choosePhoneScreen(next, setScreen)} setScreen={setScreen} terminals={!!pane} onLive={openLive}
      browser={pane ? {
        // An open ask goes straight to the agent's page. Otherwise the browser opens on the last tab.
        onOpen: () => { const ask = asks.find(item => item.tab); if (ask?.tab && ask.pane) watchTab(ask.tab, ask.pane); else setBrowserSheet(true) },
        busy: !!agentBrowser?.agents.some(agent => agent.tabs.length), asking: asks.length > 0,
      } : undefined} />}
    {browserSheet && pane && <BrowserSheet api={api} pane={pane} onClose={() => setBrowserSheet(false)} />}
    {deck && <ActionDeck api={api} items={waiting} sessions={sessions} onOpen={openPane} onClose={() => setDeck(false)} onUpdate={() => void refresh()} />}
    {liveMode && <Live api={api} panes={panes} selected={pane} commands={commands.filter(item => !item.label.includes('Live mode'))} onClose={() => setLive(false)} onBacklog={() => setBacklog(true)} reveal={liveReveal} from={liveFrom} />}
    {view && <Inbox api={api} items={inbox} sessions={sessions} selected={openId} open={desk.inbox} onToggle={() => { if (!home) setDesk({ inbox: !desk.inbox }) }} onOpen={openPane} onUpdate={() => void refresh()} />}
    {panelsOn && <PanelHandles grid={lookShown.grid} closed={panelsClosed}
      onSize={(key, size) => update(current => { const sizes = { ...current.panelSizes[lookId] }; if (size === null) delete sizes[key]; else sizes[key] = size; return { panelSizes: { ...current.panelSizes, [lookId]: sizes } } })}
      onClose={name => { if (name === 'sessions') setDesk({ sidebar: false }); else if (name === 'inbox') setDesk({ inbox: false }); else update(current => ({ panelsClosed: { ...current.panelsClosed, [lookId]: [...new Set([...(current.panelsClosed[lookId] ?? []), name])] } })) }}
      onOpen={name => update(current => ({ panelsClosed: { ...current.panelsClosed, [lookId]: (current.panelsClosed[lookId] ?? []).filter(item => item !== name) } }))} />}
    {view && picking !== null && picking < view.slots.length && <SlotPicker index={picking} sessions={sortByPrefs(sessions, prefs.order).filter(item => !isArchived(item, prefs.archived))} slots={view.slots} onPane={id => { showInSlot(id, picking); setPicking(null) }} onSession={target => { showSession(target); setPicking(null) }} onClose={() => setPicking(null)} />}
    {project && sessions.some(item => item.id === project.session && (!project.group || item.groups?.some(group => group.id === project.group))) && <ProjectSheet key={`${project.session}/${project.group ?? ''}`} api={api} session={sessions.find(item => item.id === project.session)!} group={project.group ? sessions.find(item => item.id === project.session)!.groups?.find(item => item.id === project.group) : undefined} mark={marks[project.session]} pin={project.pin} onChanged={loadMarks} onClose={() => setProject(null)} />}
    {backlog && <BacklogSheet api={api} panes={panes} onOpenPane={openPane} onClose={() => setBacklog(false)} />}
    {backlogAdd && <BacklogAddSheet key={backlogAdd.text} api={api} text={backlogAdd.text} pane={backlogAdd.pane} onClose={() => setBacklogAdd(null)} onAdded={setNotice} />}
    {settings && <Settings api={api} prefs={prefs} update={update} onReload={reload} onClose={() => setSettings(false)} start={settings === 'layout-maker' ? 'layout-maker' : undefined} />}
    {!settings && <TrialBar onRevert={revertLook} />}
    {tabTask && <NewTask api={api} pane={tabTask.pane} at={tabTask.at} micOk={!prefs.voiceOff} chime={prefs.voiceSound} onClose={() => setTabTask(null)}
      onEmpty={() => { const target = tabTask.pane; setTabTask(null); void split('right', target) }}
      onCreated={(_id, made) => { setTabTask(null); void refresh(); if (made) setNotice(worktreeNotice(made)) }}
      onOpen={item => { setTabTask(null); void refresh(); openPane(item) }} onMoved={item => { setTabTask(null); void refresh(); openPane(item) }} />}
    {newSession && <NewSession api={api} onClose={() => setNewSession(false)} onCreated={(created, made) => { openPane(created); setNewSession(false); void refresh(); if (made) setNotice(worktreeNotice(made)) }} />}
    {actionSession && pane && <SessionActions key={`${actionSession.id}-${actionsDelete}`} tabCount={panes.filter(item => item.tmux_session === actionSession.id).length} startDelete={actionsDelete} at={actionsDelete ? undefined : menuAt} place={placeFor(actionSession.panes[0].id, actionSession)} api={api} session={actionSession} pane={actionSession.panes.find(item => item.id === pane.id)?.id ?? actionSession.panes[0].id} pinned={prefs.pinned.includes(actionSession.id)} archived={isArchived(actionSession, prefs.archived)} canMoveUp={ordered.indexOf(actionSession.id) > 0} canMoveDown={ordered.indexOf(actionSession.id) < ordered.length - 1} targets={moveTargets} marks={marks} onMarks={loadMarks}
      onPin={() => update(current => ({ pinned: current.pinned.includes(actionSession.id) ? current.pinned.filter(id => id !== actionSession.id) : [...current.pinned, actionSession.id] }))}
      onArchive={() => update(toggleArchive(actionSession))}
      color={prefs.colors?.[actionSession.id]} onColor={color => update(current => ({ colors: withColor(current.colors, actionSession.id, color) }))}
      groups={prefs.groups} group={prefs.groupOf?.[actionSession.id]}
      onGroup={(group, created) => update(current => ({ groups: created ? [...current.groups, created] : current.groups, groupOf: withGroup(current.groupOf, actionSession.id, group?.id), ...current.groupBy === 'custom' || !group ? {} : { groupBy: 'custom' as const } }))}
      onMove={step => move(actionSession.id, step)} onProject={() => { setActions(null); setProject({ session: actionSession.id }) }} onClose={() => setActions(null)} onDone={(newPane, direction) => { setActions(null); if (newPane) void openNewPane(newPane, direction); else void refresh() }} />}
    {paneActions && panes.find(item => item.id === paneActions) && (() => { const target = panes.find(item => item.id === paneActions)!; const shown = sessions.find(item => item.id === target.tmux_session); const archived = isTabArchived(target, prefs.archivedTabs) || (!!shown && shown.panes.length <= 1 && isArchived(shown, prefs.archived)); return <PaneActions api={api} panes={panes} tabCount={panes.filter(item => item.tmux_session === target.tmux_session).length} archived={archived} onArchive={() => archived ? restoreTab(target) : archiveTab(target)} pane={target} color={prefs.colors?.[target.id]} onColor={color => update(current => ({ colors: withColor(current.colors, target.id, color) }))} at={menuAt} place={placeFor(target.id)} session={sessions.find(item => item.id === target.tmux_session)} targets={moveTargets} onClose={() => setPaneActions(null)} onFork={() => { setPaneActions(null); void fork(target) }} onDone={(newPane, direction) => { setPaneActions(null); if (newPane) void openNewPane(newPane, direction); else void refresh() }} /> })()}
    {resume && <ResumeChat api={api} chat={resume} sessions={sessions} onClose={() => setResume(null)} onOpened={id => { setResume(null); openById(id) }} />}
    {notice && <div className="toast app-toast" role="status">{notice}</div>}
    {planSheet && <PlanUsage api={api} onClose={() => setPlanSheet(false)} />}
    {view && menu === 'commands' && <CommandMenu sessions={sortByPrefs(sessions, prefs.order).filter(item => !isArchived(item, prefs.archived))} seen={prefs.seen} selected={openId} slots={multi ? view.slots : []} commands={commands} onPane={openPane} onSession={showSession} onClose={() => setMenu(null)} />}
    {(view || (wide && home)) && menu === 'shortcuts' && <Shortcuts onClose={() => setMenu(null)} />}
    {agentsPage && <AgentsPage api={api} sessions={sessions} seen={prefs.seen} selected={openId} onOpen={openPane} onMarkRead={markRead} startMessaging={agentsPage === 'message'} onClose={() => setAgentsPage(false)} />}
    {view && !home && !files && !(wide && share) && <StatusBar panes={panes} loading={loading} error={error} />}
    {updated && <div className="update-banner" role="status"><span>A new version of Tether is ready.</span><button type="button" onClick={reload}>Reload</button></div>}
    {!updated && !!data?.auto_resume?.failed && <AutoResumeNotice api={api} data={data.auto_resume} onChange={refresh} />}
  </main></WordsContext.Provider>
}
export default App
