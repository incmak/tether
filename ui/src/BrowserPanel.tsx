import { RetryButton } from './Retry'
import { Spinner } from './Skeleton'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import { AppWindow, ArrowLeft, ArrowRight, Bot, Check, ChevronDown, Crosshair, FlaskConical, HatGlasses, Keyboard, KeyRound, Monitor, PenLine, PictureInPicture2, Plus, RotateCw, Send, Smartphone, Square, Tablet, Trash2, Type, UserRound, X } from 'lucide-react'
import { errorText } from './api'
import { useBackStep } from './backNav'
import type { Api, BrowserAvailability, BrowserId, Pane } from './api'
import { usePaneLabel, WATCH_EVENT } from './AgentBrowser'
import { useHost } from './host'
import { MacPairing } from './MacPairing'
import { openLive } from './liveStream'
import type { LiveStream } from './liveStream'
import './BrowserPanel.css'

// Same shape as PanelProps in src/panels.ts (frame lane); kept here until the lanes merge.
// onPopOut: show a button that moves the panel into its own window (BrowserWindow.tsx).
export interface BrowserPanelProps { api: Api; pane: Pane; folder: string | null; active: boolean; onPopOut?: () => void }

type Mode = 'private' | 'saved'
// 'agent': a tab an agent opened over CDP (server/browser_cdp.py). The agent owns its size until a person takes over
// (controller 'user'); Stop gives the agent its own viewport back.
interface BrowserTab { id: string; url: string; title: string; preset: string; mode: Mode | 'agent'; width: number; height: number; touch: boolean; owner?: { pane: string | null } | null; controller?: 'user' | 'agent' }
interface Preset { id: string; label: string; width: number; height: number; touch: boolean }
interface BrowserState { tabs: BrowserTab[]; presets: Preset[]; nav?: { back: boolean; forward: boolean }; browsers?: BrowserAvailability[]; live?: string; live_port?: number | null; live_run?: string | null }
interface FrameMessage { seq: number; width: number; height: number; jpeg: string; scrollOffsetX?: number; scrollOffsetY?: number }
type Gesture = Record<string, string | number | string[]>
type Tool = 'point' | 'pen' | 'rect' | 'text'
type Shape = { kind: 'point'; x: number; y: number } | { kind: 'pen'; points: number[] } | { kind: 'rect'; x: number; y: number; w: number; h: number } | { kind: 'text'; x: number; y: number; text: string }

// The composer of the pane with this id takes the files and uploads them (see Composer.tsx). It cancels the event when it does.
const ATTACH_EVENT = 'tether-attach'
const MARK = '#e5372b'
const KEYS = new Set(['Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'])
const COMMANDS: Record<string, string> = { a: 'selectAll', c: 'copy', x: 'cut', z: 'undo', y: 'redo' }
const BUTTONS = ['left', 'middle', 'right']
const SENTINEL = '_' // the hidden text box keeps one character, so a phone keyboard's Backspace has something to delete
const TOOLS: { id: Tool; label: string; icon: typeof PenLine }[] = [
  { id: 'point', label: 'Point', icon: Crosshair }, { id: 'pen', label: 'Pen', icon: PenLine },
  { id: 'rect', label: 'Rectangle', icon: Square }, { id: 'text', label: 'Text', icon: Type },
]

function modifiers(event: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }) {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0)
}
function shown(url: string) { return url === 'about:blank' ? '' : url }
function tabName(tab: BrowserTab) {
  if (tab.title && tab.title !== 'about:blank') return tab.title
  try { return tab.url === 'about:blank' ? 'New tab' : new URL(tab.url).host || tab.url } catch { return tab.url || 'New tab' }
}
function DeviceIcon({ preset }: { preset: string }) {
  return preset === 'desktop' ? <Monitor size={17} /> : preset === 'tablet' ? <Tablet size={17} /> : <Smartphone size={17} />
}

// Close a menu on Escape or on a click outside it.
function useMenu() {
  const [open, setOpen] = useState(false)
  useBackStep(open, () => setOpen(false))
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  const onKeyDown = (event: ReactKeyboardEvent) => { if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false); ref.current?.querySelector<HTMLButtonElement>('button')?.focus() } }
  return { open, setOpen, ref, onKeyDown }
}

function usePageVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible')
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return visible
}

/** A tab of the server's headless Chromium: tabs, address bar, device presets, the streamed page, and notes on a still frame. */
// A phone: tabs it opens or takes over start at phone width.
const onPhone = () => typeof matchMedia === 'function' && matchMedia('(pointer: coarse) and (max-width: 820px)').matches
const savedNote = (host: string) => `Saved logins · readable by agents on ${host}`
const signedNote = (host: string) => `Your sign-ins · agents cannot drive this browser, but can read its files on ${host}`
const WHICH_KEY = 'tether-browser-which'
const tabKey = (which: BrowserId) => which === 'test' ? 'tether-browser-tab' : `tether-browser-tab-${which}`
// 'mac' is the old id for 'chrome', still saved in some browsers' localStorage.
const asBrowser = (value: unknown): BrowserId => value === 'mac' || value === 'chrome' ? 'chrome' : value === 'signed' ? 'signed' : 'test'
const BROWSERS: { id: BrowserId; label: string; note: string; icon: typeof UserRound }[] = [
  { id: 'test', label: 'Test', note: 'Agents can use it. No sign-ins.', icon: FlaskConical },
  { id: 'signed', label: 'Signed in', note: 'Google Chrome with your sign-ins. Agents cannot use it.', icon: UserRound },
  { id: 'chrome', label: 'Your Chrome', note: 'Your own Chrome, through the Tether extension. Agents use a tab only after you allow it.', icon: AppWindow },
]

/** Two server browsers: 'test' (headless, agents can drive it) and 'signed' (headed Google Chrome, people only).
 * Each choice mounts its own view, so a switch closes the old stream and keeps tab choices apart. */
export function BrowserPanel(props: BrowserPanelProps) {
  const [which, setWhich] = useState<BrowserId>(() => { try { return asBrowser(localStorage.getItem(WHICH_KEY)) } catch { return 'test' } })
  const change = (id: BrowserId) => { setWhich(id); try { localStorage.setItem(WHICH_KEY, id) } catch { /* private mode */ } }
  // "Watch" opens an agent tab: in the test browser, or in the signed-in browser when Mak allowed it (detail.browser).
  useEffect(() => {
    const onWatch = (event: Event) => {
      const detail = (event as CustomEvent<{ tab: string; browser?: BrowserId }>).detail
      const target = asBrowser(detail.browser)
      if (which === target) return
      try { localStorage.setItem(tabKey(target), detail.tab) } catch { /* private mode */ }
      change(target)
    }
    window.addEventListener(WATCH_EVENT, onWatch)
    return () => window.removeEventListener(WATCH_EVENT, onWatch)
  }, [which])
  return <BrowserView key={which} {...props} which={which} onSwitch={change} />
}

function BrowserView({ api: baseApi, pane, active, onPopOut, which, onSwitch }: BrowserPanelProps & { which: BrowserId; onSwitch: (id: BrowserId) => void }) {
  const host = useHost()
  // Every request names the signed-in browser; headed Chrome can take longer than 10 s to start.
  const api: Api = which === 'test' ? baseApi : (path, body, signal, method, timeout, priority) => baseApi(
    body === undefined ? `${path}${path.includes('?') ? '&' : '?'}browser=${which}` : path,
    body === undefined ? undefined : { ...(body as object), browser: which }, signal, method, timeout ?? 30000, priority)
  const [browsers, setBrowsers] = useState<BrowserAvailability[]>([])
  const switchMenu = useMenu()
  const [tabs, setTabs] = useState<BrowserTab[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [nav, setNav] = useState({ back: false, forward: false })
  const [current, setCurrent] = useState<string | null>(() => { try { return localStorage.getItem(tabKey(which)) } catch { return null } })
  const [loaded, setLoaded] = useState(false)
  const [failure, setFailure] = useState('')
  const [status, setStatus] = useState('')
  const [live, setLive] = useState(true)
  // The browser runs in a Selkies container: a Desktop tab shows its real window as video (state.live is the path).
  // The video page has its own origin (state.live_port), so a page from the container cannot reach Tether.
  const [liveBase, setLiveBase] = useState<{ path: string; port: number; run: string } | null>(null)
  const [liveReady, setLiveReady] = useState(false)
  // 'down': the live port did not answer, so the panel uses the picture stream and says why.
  const [liveReach, setLiveReach] = useState<'checking' | 'ok' | 'down'>('checking')
  const [typing, setTyping] = useState(false)
  // BrowserView is keyed by browser id, so switching browsers resets this choice.
  const [windowView, setWindowView] = useState(false)
  const windowViewRef = useRef(windowView)
  useLayoutEffect(() => { windowViewRef.current = windowView }, [windowView])
  const [draft, setDraft] = useState<string | null>(null)
  const [stage, setStage] = useState({ width: 0, height: 0 })
  const [frame, setFrame] = useState<{ width: number; height: number } | null>(null)
  const [noting, setNoting] = useState(false)
  const [tool, setTool] = useState<Tool>('pen')
  const [shapes, setShapes] = useState<Shape[]>([])
  const [label, setLabel] = useState<{ x: number; y: number; left: number; top: number; text: string } | null>(null)
  const visible = usePageVisible()
  const tabMenu = useMenu()
  const deviceMenu = useMenu()
  const tab = tabs.find(item => item.id === current) ?? tabs[0] ?? null
  const tabId = tab?.id ?? null
  // Agent tabs are watch-only until the person takes over: no clicks, keys or navigation reach the page.
  // The server holds who controls the viewport, so every phone and desktop shows the same state.
  const watching = tab?.mode === 'agent' && tab.controller !== 'user'
  const [handing, setHanding] = useState(false)
  const watchingRef = useRef(watching)
  useLayoutEffect(() => { watchingRef.current = watching }, [watching])
  // While an agent controls the tab, the live view takes no keys either: inert keeps Tab out, and focus already in it leaves.
  const liveFrameRef = useRef<HTMLIFrameElement>(null)
  useLayoutEffect(() => { if (watching && document.activeElement === liveFrameRef.current) liveFrameRef.current?.blur() }, [watching])
  const owner = usePaneLabel(tab?.owner?.pane)
  // Phone and tablet tabs keep the picture stream: they need Chrome's device emulation at the panel's scale.
  const liveWanted = !!liveBase && tab?.preset === 'desktop' && !windowView
  const liveMode = liveWanted && liveReach !== 'down'
  const liveView = liveMode && !noting
  const liveSrc = liveBase ? `${location.protocol}//${location.hostname}:${liveBase.port}${liveBase.path}` : ''

  const stageRef = useRef<HTMLDivElement | null>(null)
  // The stage mounts late in Your Chrome (after the pairing card), so observe the element itself, not the load.
  const [stageBox, setStageBox] = useState<HTMLDivElement | null>(null)
  const stageSlot = useCallback((box: HTMLDivElement | null) => { stageRef.current = box; setStageBox(box) }, [])
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  // The canvas mounts late in Your Chrome too, so the wheel listener follows the element.
  const [canvasBox, setCanvasBox] = useState<HTMLCanvasElement | null>(null)
  const canvasSlot = useCallback((box: HTMLCanvasElement | null) => { canvasRef.current = box; setCanvasBox(box) }, [])
  const marksRef = useRef<HTMLCanvasElement>(null)
  const keysRef = useRef<HTMLTextAreaElement>(null)
  const urlRef = useRef<HTMLInputElement>(null)
  const tabRef = useRef(tab)
  useLayoutEffect(() => { tabRef.current = tab }, [tab])
  const frameRef = useRef(frame)
  useLayoutEffect(() => { frameRef.current = frame }, [frame])
  const notingRef = useRef(noting)
  useLayoutEffect(() => { notingRef.current = noting }, [noting])

  function apply(state: BrowserState) {
    setTabs(state.tabs)
    if (state.presets) setPresets(state.presets)
    if (state.nav) setNav(state.nav)
    if (state.browsers) setBrowsers(state.browsers)
    const path = state.live ?? null, port = state.live_port ?? 0, run = state.live_run ?? ''
    setLiveBase(old => !path || !port ? null : old && old.path === path && old.port === port && old.run === run ? old : { path, port, run })
  }
  function choose(id: string) {
    setCurrent(id)
    setFrame(null)
    setNav({ back: false, forward: false })
    try { localStorage.setItem(tabKey(which), id) } catch { /* private mode */ }
  }
  async function load() {
    try { apply(await api<BrowserState>('browser/tabs')); setFailure('') } catch (error) { setFailure(errorText(error)) } finally { setLoaded(true) }
  }
  // An empty Chrome view has no stream, so check for a newly allowed tab every few seconds.
  const macEmpty = which === 'chrome' && loaded && !tabs.length
  useEffect(() => {
    if (!active || !macEmpty) return
    const timer = window.setInterval(() => void load(), 4000)
    return () => clearInterval(timer)
  }, [active, macEmpty]) // eslint-disable-line react-hooks/exhaustive-deps
  // Start the browser only when the panel is first shown.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- Loading starts an external request; state changes occur after await or when the request fails.
  useEffect(() => { if (active && !loaded) void load() }, [active]) // eslint-disable-line react-hooks/exhaustive-deps
  // eslint-disable-next-line react-hooks/set-state-in-effect -- Keep fallback tab selection, frame reset, navigation reset, and saved tab identity in one transition.
  useEffect(() => { if (tab && tab.id !== current) choose(tab.id) }, [tab?.id]) // eslint-disable-line react-hooks/exhaustive-deps
  // "Watch" in a chat card (AgentBrowser.tsx): show that tab. Reload the list: the tab can be newer than the stream's last list.
  useEffect(() => {
    const onWatch = (event: Event) => {
      const detail = (event as CustomEvent<{ tab: string; browser?: BrowserId }>).detail
      if (which !== (detail.browser === 'signed' ? 'signed' : 'test')) return
      choose(detail.tab); void load()
    }
    window.addEventListener(WATCH_EVENT, onWatch)
    return () => window.removeEventListener(WATCH_EVENT, onWatch)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Stage size: the page is scaled to fit it, and a Desktop tab takes its size.
  useEffect(() => {
    const box = stageBox
    if (!box) return
    const observer = new ResizeObserver(([entry]) => setStage({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) }))
    observer.observe(box)
    return () => observer.disconnect()
  }, [stageBox])
  // The screen in use owns the size; never react to another viewer's size.
  function claimSize() {
    const tab = tabRef.current
    if (!active || !tab || watching || windowView || tab.preset !== 'desktop' || stage.width < 50 || stage.height < 50 || noting) return
    const width = Math.max(320, stage.width), height = Math.max(240, stage.height)
    if (Math.abs(width - tab.width) < 3 && Math.abs(height - tab.height) < 3) return
    void api<BrowserTab>('browser/device', { tab: tab.id, preset: 'desktop', width, height }).then(row => setTabs(list => list.map(item => item.id === row.id ? row : item))).catch(() => undefined)
  }
  useEffect(() => {
    const timer = setTimeout(claimSize, 400)
    const onActive = () => { if (document.visibilityState === 'visible') claimSize() }
    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)
    return () => {
      clearTimeout(timer)
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [active, tab?.id, tab?.preset, stage.width, stage.height, noting, watching, windowView]) // eslint-disable-line react-hooks/exhaustive-deps

  // Frames: decode the newest one and skip any that arrive while one decodes.
  const pending = useRef<FrameMessage | null>(null)
  const decoding = useRef(false)
  // Each stream gets a number. A frame that finishes decoding after its stream closed is dropped.
  const streamGen = useRef(0)
  // Instant scroll. A wheel turn moves the last picture at once, by the CSS pixels the page will scroll; the next
  // frame shows how far the page really moved and keeps only the part not yet done. A frame reaches the panel a
  // round trip after the input, so without this the page seemed to follow the wheel late and in jumps.
  // It runs only on a tab where a wheel turn has moved the page's own scroll position: a page that scrolls an
  // inner box (mail and chat apps) reports no change, and moving the whole picture there would be wrong.
  const painted = useRef<{ bitmap: ImageBitmap; x: number; y: number; width: number } | null>(null)
  const ahead = useRef({ x: 0, y: 0, at: 0, confirmed: false })
  const scrolls = useRef<{ tab: string | null; works: boolean }>({ tab: null, works: false })
  const settle = useRef<ReturnType<typeof setTimeout> | null>(null)
  function draw() {
    const canvas = canvasRef.current, last = painted.current
    const context = canvas?.getContext('2d')
    if (!canvas || !last || !context) return
    const scale = canvas.width / Math.max(1, last.width)
    const dx = Math.round(-ahead.current.x * scale), dy = Math.round(-ahead.current.y * scale)
    const { width, height } = canvas
    context.drawImage(last.bitmap, dx, dy)
    // Fill the strip the page has not sent yet with its edge row or column, stretched.
    if (dy < 0) context.drawImage(last.bitmap, 0, height - 1, width, 1, dx, height + dy, width, -dy)
    if (dy > 0) context.drawImage(last.bitmap, 0, 0, width, 1, dx, 0, width, dy)
    if (dx < 0) context.drawImage(last.bitmap, width - 1, 0, 1, height, width + dx, 0, -dx, height)
    if (dx > 0) context.drawImage(last.bitmap, 0, 0, 1, height, 0, 0, dx, height)
  }
  function reset() {
    if (settle.current) clearTimeout(settle.current)
    settle.current = null
    const moved = ahead.current.x || ahead.current.y
    if (moved && !ahead.current.confirmed) scrolls.current.works = false  // the page did not scroll itself
    ahead.current = { x: 0, y: 0, at: 0, confirmed: false }
    if (moved) draw()
  }
  function predict(dx: number, dy: number) {
    if (windowViewRef.current) return
    const tab = tabRef.current, last = painted.current, size = frameRef.current
    if (!tab || tab.touch || scrolls.current.tab !== tab.id || !scrolls.current.works || !last || !size) {
      ahead.current.at = performance.now()  // still mark the turn so the next frame can learn that scrolling works
      return
    }
    const limit = (value: number, most: number) => Math.max(-most, Math.min(most, value))
    ahead.current = { ...ahead.current, x: limit(ahead.current.x + dx, size.width), y: limit(ahead.current.y + dy, size.height), at: performance.now() }
    draw()
    if (settle.current) clearTimeout(settle.current)
    settle.current = setTimeout(reset, 350)  // no frame took the rest: the page reached its end
  }
  // Draw at most once per display frame: pictures that arrive together show only the newest.
  const drawQueued = useRef(false)
  function drawSoon() {
    if (drawQueued.current) return
    drawQueued.current = true
    requestAnimationFrame(() => { drawQueued.current = false; draw() })
  }
  // A decoded picture from either stream. It replaces the last one and keeps the scroll prediction in step.
  function show(bitmap: ImageBitmap, message: { width: number; height: number; scrollOffsetX?: number; scrollOffsetY?: number }, gen: number) {
    const canvas = canvasRef.current
    if (!canvas || notingRef.current || gen !== streamGen.current) { bitmap.close(); return }
    if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) { canvas.width = bitmap.width; canvas.height = bitmap.height }
    const id = tabRef.current?.id ?? null
    if (scrolls.current.tab !== id) { scrolls.current = { tab: id, works: false }; ahead.current = { x: 0, y: 0, at: 0, confirmed: false } }
    const before = painted.current
    const x = message.scrollOffsetX, y = message.scrollOffsetY
    if (typeof x === 'number' && typeof y === 'number' && before && before.width === message.width) {
      const movedX = x - before.x, movedY = y - before.y
      if ((movedX || movedY) && performance.now() - ahead.current.at < 1000) scrolls.current.works = true
      // Keep only the predicted scroll the page has not done yet; never overshoot past zero.
      const rest = (left: number, moved: number) => left > 0 ? Math.max(0, left - moved) : left < 0 ? Math.min(0, left - moved) : 0
      if (movedX || movedY) ahead.current = { ...ahead.current, x: rest(ahead.current.x, movedX), y: rest(ahead.current.y, movedY), confirmed: true }
    } else ahead.current = { x: 0, y: 0, at: ahead.current.at, confirmed: false }
    const resized = !before || before.bitmap.width !== bitmap.width || before.bitmap.height !== bitmap.height
    before?.bitmap.close()
    painted.current = typeof x === 'number' && typeof y === 'number' ? { bitmap, x, y, width: message.width } : { bitmap, x: 0, y: 0, width: message.width }
    if (typeof x !== 'number' || typeof y !== 'number') scrolls.current.works = false
    // A resize clears the canvas, so draw now; otherwise wait for the display frame.
    if (resized) draw(); else drawSoon()
    setFrame(old => old && old.width === message.width && old.height === message.height ? old : { width: message.width, height: message.height })
  }
  async function paint() {
    decoding.current = true
    while (pending.current) {
      const message = pending.current
      const gen = streamGen.current
      pending.current = null
      try {
        const binary = atob(message.jpeg)
        const bytes = new Uint8Array(binary.length)
        for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index)
        show(await createImageBitmap(new Blob([bytes], { type: 'image/jpeg' })), message, gen)
      } catch { /* skip a frame that does not decode */ }
    }
    decoding.current = false
  }

  // The stream runs only while the panel is shown, the page is visible, and no note is open on a still frame.
  const streamWidth = Math.min(2560, Math.max(160, Math.ceil(stage.width * devicePixelRatio / 64) * 64))
  const streamHeight = Math.min(2560, Math.max(160, Math.ceil(stage.height * devicePixelRatio / 64) * 64))
  const [retry, setRetry] = useState(0)
  // 'live': one WebSocket for pictures (H.264 when the browser can decode it), input and acks (liveStream.ts).
  // 'sse': the older EventSource stream with POSTed input, used when the live socket cannot connect.
  const [transport, setTransport] = useState<'live' | 'sse'>(() => typeof WebSocket === 'function' ? 'live' : 'sse')
  const socketRef = useRef<{ stream: LiveStream; tab: string } | null>(null)
  useEffect(() => {
    if (!active || !visible || (!tabId && !windowView) || noting || liveMode || !stage.width) return
    const gen = ++streamGen.current
    let again = 0
    const query = `tab=${encodeURIComponent(tabId)}&w=${streamWidth}&h=${streamHeight}&cssw=${Math.max(1, stage.width)}&cssh=${Math.max(1, stage.height)}${which === 'test' ? '' : `&browser=${which}`}`
    if (!windowView && transport === 'live') {
      const stream = openLive(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/mobile/api/browser/live?${query}`, {
        onPicture: (bitmap, meta) => { setLive(true); show(bitmap, meta, gen) },
        onTabs: state => apply(state as BrowserState),
        onClosed: () => void load(),
        onOpen: () => setLive(true),
        onError: text => setStatus(text),
        // A socket that never connected: this server has no live route or blocks it, so use the older stream.
        onEnd: opened => {
          setLive(false)
          if (socketRef.current?.stream === stream) socketRef.current = null
          if (!opened) { setTransport('sse'); return }
          again = window.setTimeout(() => { again = 0; void load(); setRetry(count => count + 1) }, 1000)
        },
      })
      socketRef.current = { stream, tab: tabId }
      return () => { stream.close(); clearTimeout(again); streamGen.current++; if (socketRef.current?.stream === stream) socketRef.current = null }
    }
    // The server sends the next frame only after this panel confirms the previous ones, so frames never queue
    // in the network and the picture stays close to the input.
    const sid = Array.from(crypto.getRandomValues(new Uint8Array(12)), byte => byte.toString(16).padStart(2, '0')).join('')
    const source = new EventSource(windowView
      ? `/mobile/api/browser/window?browser=signed&w=${streamWidth}&h=${streamHeight}&sid=${sid}`
      : `/mobile/api/browser/stream?${query}&sid=${sid}`)
    source.addEventListener('frame', event => {
      if (gen !== streamGen.current) return
      const message = JSON.parse((event as MessageEvent).data)
      if (typeof message.seq === 'number') api('browser/ack', { sid, seq: message.seq }, undefined, undefined, 5000).catch(() => {})
      pending.current = message; setLive(true); if (!decoding.current) void paint()
    })
    source.addEventListener('tabs', event => apply(JSON.parse((event as MessageEvent).data)))
    source.addEventListener('closed', () => void load())
    source.addEventListener('error', event => {
      if (event instanceof MessageEvent) setStatus(JSON.parse(event.data).error)
    })
    source.onopen = () => setLive(true)
    // EventSource connects again by itself after a network drop, but not after an error reply
    // (for example a tab that a server restart replaced). Then reload the tabs and open a new stream.
    source.onerror = () => {
      setLive(false)
      if (again) return
      again = window.setTimeout(() => {
        again = 0
        void load()
        if (source.readyState === EventSource.CLOSED) setRetry(count => count + 1)
      }, 2000)
    }
    return () => { source.close(); clearTimeout(again); streamGen.current++; pending.current = null } // eslint-disable-line react-hooks/exhaustive-deps
  }, [active, visible, tabId, noting, liveMode, streamWidth, streamHeight, stage.width > 0, retry, transport, windowView]) // eslint-disable-line react-hooks/exhaustive-deps

  // The live view has no picture stream to bring tab titles and history, so ask for them while it shows.
  // Each poll also keeps the browser from stopping as idle. A new tab, or this screen coming back, brings it to the front.
  useEffect(() => {
    if (!active || !visible || !liveMode || !tabId) return
    let stopped = false
    void api('browser/show', { tab: tabId }).catch(error => { if (!stopped) setStatus(errorText(error)) })
    const poll = () => api<BrowserState>(`browser/tabs?tab=${encodeURIComponent(tabId)}`).then(state => { if (!stopped) { apply(state); setFailure('') } }, () => undefined)
    const timer = window.setInterval(() => void poll(), 2000)
    void poll()
    return () => { stopped = true; clearInterval(timer) }
  }, [active, visible, liveMode, tabId]) // eslint-disable-line react-hooks/exhaustive-deps
  // eslint-disable-next-line react-hooks/set-state-in-effect -- A new container run loads a new page.
  useEffect(() => setLiveReady(false), [liveBase?.run])
  // Check that the live port answers before the frame shows it: a frame cannot tell the panel that its page failed.
  // A port that does not answer is a setup gap (tailscale serve), so say so and keep the picture stream.
  useEffect(() => {
    if (!active || !visible || !liveWanted || !liveSrc) return
    const control = new AbortController()
    let stopped = false
    const timer = window.setTimeout(() => control.abort(), 8000)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- A new check starts; a working view stays up meanwhile.
    setLiveReach(old => old === 'ok' ? old : 'checking')
    fetch(liveSrc, { mode: 'no-cors', credentials: 'include', cache: 'no-store', signal: control.signal })
      .then(() => { if (!stopped) setLiveReach('ok') }, () => { if (!stopped) setLiveReach('down') })
      .finally(() => clearTimeout(timer))
    return () => { stopped = true; clearTimeout(timer); control.abort() }
  }, [active, visible, liveWanted, liveSrc, tabId, liveBase?.run])

  // Input: queue events, merge moves and wheel turns, and send one batch per animation frame.
  const queue = useRef<Gesture[]>([])
  const queueTab = useRef<string | null>(null)
  const queueWindow = useRef(false)
  const sending = useRef(false)
  const scheduled = useRef(false)
  function push(...events: Gesture[]) {
    // An agent tab blocks input to that tab only; the whole window is not one tab, and its dialogs must stay usable.
    if (watchingRef.current && !windowViewRef.current) return
    // The queue belongs to one tab. Input for a new tab never goes to the old one.
    const wholeWindow = windowViewRef.current
    const id = wholeWindow ? 'window' : tabRef.current?.id ?? null
    if (queueTab.current !== id || queueWindow.current !== wholeWindow) {
      queue.current = []; queueTab.current = id; queueWindow.current = wholeWindow
    }
    for (const event of events) {
      const last = queue.current[queue.current.length - 1]
      if (last && last.type === event.type && event.type === 'move') queue.current[queue.current.length - 1] = event
      else if (last && last.type === 'wheel' && event.type === 'wheel') { last.dx = Number(last.dx) + Number(event.dx); last.dy = Number(last.dy) + Number(event.dy) }
      else queue.current.push(event)
    }
    if (!scheduled.current) { scheduled.current = true; requestAnimationFrame(() => { scheduled.current = false; void flush() }) }
  }
  async function flush() {
    const target = queueTab.current
    const wholeWindow = queueWindow.current
    if (sending.current || !queue.current.length || !target) return
    // The live socket keeps input in order with no request per batch.
    const socket = socketRef.current
    if (!wholeWindow && socket?.tab === target && socket.stream.ready()) {
      while (queue.current.length) socket.stream.input(queue.current.splice(0, 200))
      return
    }
    sending.current = true
    const events = queue.current.splice(0, 200)
    try {
      if (wholeWindow) await api('browser/window/input', { browser: 'signed', events })
      else await api('browser/input', { tab: target, events })
    } catch (error) { setStatus(errorText(error)) } finally {
      sending.current = false
      if (queue.current.length) void flush()
    }
  }
  // Canvas point to page CSS pixels.
  function pagePoint(event: { clientX: number; clientY: number }) {
    const canvas = canvasRef.current, size = frameRef.current ?? (windowViewRef.current
      ? { width: 2560, height: 1600 } : tabRef.current ? { width: tabRef.current.width, height: tabRef.current.height } : null)
    if (!canvas || !size) return { x: 0, y: 0 }
    const box = canvas.getBoundingClientRect()
    return { x: Math.round((event.clientX - box.left) / box.width * size.width), y: Math.round((event.clientY - box.top) / box.height * size.height) }
  }
  function cssScale() {
    const canvas = canvasRef.current, size = frameRef.current
    return canvas && size ? size.width / canvas.getBoundingClientRect().width : 1
  }

  const lastDown = useRef({ time: 0, x: 0, y: 0, count: 0 })
  // A press the page saw. It must always get its release (pointer cancel, tab change, unmount).
  const pressed = useRef<{ button: string; x: number; y: number } | null>(null)
  const drag = useRef<{ id: number; x: number; y: number; start: { x: number; y: number }; moved: boolean } | null>(null)
  function onPointerDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!tab && !windowView) return
    if (event.pointerType === 'mouse') keysRef.current?.focus({ preventScroll: true })
    event.currentTarget.setPointerCapture(event.pointerId)
    const point = pagePoint(event)
    // A finger on a Desktop tab scrolls; a tap clicks.
    if (event.pointerType === 'touch' && (windowView || !tab?.touch)) { drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, start: point, moved: false }; return }
    if (event.button > 2) return
    const now = performance.now(), last = lastDown.current
    const count = now - last.time < 500 && Math.abs(point.x - last.x) < 5 && Math.abs(point.y - last.y) < 5 ? Math.min(3, last.count + 1) : 1
    lastDown.current = { time: now, ...point, count }
    pressed.current = { button: BUTTONS[event.button] ?? 'left', ...point }
    push({ type: 'down', ...point, button: BUTTONS[event.button] ?? 'left', buttons: event.buttons, clicks: count, modifiers: modifiers(event) })
  }
  function onPointerMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!tab && !windowView) return
    const touch = drag.current
    if (touch && touch.id === event.pointerId) {
      const dx = event.clientX - touch.x, dy = event.clientY - touch.y
      if (!touch.moved && Math.hypot(dx, dy) < 8) return
      touch.moved = true
      touch.x = event.clientX; touch.y = event.clientY
      const scale = cssScale()
      push({ type: 'wheel', ...touch.start, dx: -dx * scale, dy: -dy * scale })
      predict(-dx * scale, -dy * scale)
      return
    }
    // A phone or tablet tab gets only pressed moves (the server sends them as a finger drag); hover means nothing there.
    if (!windowView && tab?.touch ? !event.buttons : event.pointerType === 'touch') return
    if (pressed.current) Object.assign(pressed.current, pagePoint(event))
    push({ type: 'move', ...pagePoint(event), button: event.buttons & 1 ? 'left' : event.buttons & 2 ? 'right' : 'none', buttons: event.buttons, modifiers: modifiers(event) })
  }
  function onPointerUp(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!tab && !windowView) return
    const touch = drag.current
    if (touch && touch.id === event.pointerId) {
      drag.current = null
      if (!touch.moved) push({ type: 'down', ...touch.start, button: 'left', clicks: 1 }, { type: 'up', ...touch.start, button: 'left', clicks: 1 })
      return
    }
    if (event.button > 2) return
    pressed.current = null
    push({ type: 'up', ...pagePoint(event), button: BUTTONS[event.button] ?? 'left', buttons: event.buttons, clicks: lastDown.current.count, modifiers: modifiers(event) })
  }
  // React adds wheel listeners as passive, so the page could not stop the panel from scrolling. Add it here.
  useEffect(() => {
    const canvas = canvasBox
    if (!canvas) return
    const wheel = (event: WheelEvent) => {
      if (notingRef.current || (!tabRef.current && !windowViewRef.current)) return
      event.preventDefault()
      const step = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1
      push({ type: 'wheel', ...pagePoint(event), dx: event.deltaX * step, dy: event.deltaY * step, modifiers: modifiers(event) })
      if (!event.ctrlKey) predict(event.deltaX * step, event.deltaY * step)
    }
    canvas.addEventListener('wheel', wheel, { passive: false })
    return () => canvas.removeEventListener('wheel', wheel)
  }, [canvasBox]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keyboard: a hidden text box takes the keys. Keys with a meaning go as key events; phone keyboards send text.
  const lastEscape = useRef(0)
  const held = useRef(new Map<string, { key: string; code: string }>())
  // Releases for every held key and the held button, to send before focus or the tab moves away.
  function releases(): Gesture[] {
    const events: Gesture[] = [...held.current.values()].map(({ key, code }) => ({ type: 'keyup', key, code, modifiers: 0 }))
    held.current.clear()
    const press = pressed.current
    pressed.current = null
    if (press) events.push({ type: 'up', x: press.x, y: press.y, button: press.button, buttons: 0, clicks: 1, modifiers: 0 })
    return events
  }
  // Leaving a tab: release keys and buttons there, and drop its queued input.
  useEffect(() => {
    if (!tabId && !windowView) return
    return () => {
      // Blur can queue a release just before this cleanup. Send it to the old destination before dropping input.
      const queued = queueWindow.current === windowView && queueTab.current === (windowView ? 'window' : tabId)
        ? queue.current.filter(event => event.type === 'up' || event.type === 'keyup') : []
      const events = [...queued, ...releases()]
      queue.current = []
      drag.current = null
      if (events.length) void api(windowView ? 'browser/window/input' : 'browser/input',
        windowView ? { browser: 'signed', events } : { tab: tabId, events }).catch(() => undefined)
    }
  }, [tabId, windowView]) // eslint-disable-line react-hooks/exhaustive-deps
  function onKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.key === 'Unidentified' || event.key === 'Process' || ['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) return
    if (event.key === 'Escape') {
      const now = performance.now()
      if (now - lastEscape.current < 600) { event.preventDefault(); stageRef.current?.focus(); return }
      lastEscape.current = now
    }
    const command = event.ctrlKey || event.metaKey
    if (command && event.key.toLowerCase() === 'v') return // the paste event brings the text
    if (event.key.length !== 1 && !KEYS.has(event.key) && !(windowView && /^F([1-9]|1[0-2])$/.test(event.key))) return
    event.preventDefault()
    event.stopPropagation() // the page owns this key; Tether's own shortcuts must not run
    const name = command ? COMMANDS[event.key.toLowerCase()] : undefined
    held.current.set(event.code + event.key, { key: event.key, code: event.code })
    push({ type: 'keydown', key: event.key, code: event.code, modifiers: modifiers(event), ...(name ? { commands: [event.shiftKey && name === 'undo' ? 'redo' : name] } : {}) })
  }
  function onKeyUp(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (!held.current.delete(event.code + event.key)) return
    event.preventDefault()
    push({ type: 'keyup', key: event.key, code: event.code, modifiers: modifiers(event) })
  }
  function syncText() {
    const box = keysRef.current
    if (!box) return
    const value = box.value
    if (value.length < SENTINEL.length) push({ type: 'keydown', key: 'Backspace', code: 'Backspace' }, { type: 'keyup', key: 'Backspace', code: 'Backspace' })
    else if (value.length > SENTINEL.length) push({ type: 'text', text: value.startsWith(SENTINEL) ? value.slice(SENTINEL.length) : value })
    box.value = SENTINEL
    box.setSelectionRange(SENTINEL.length, SENTINEL.length)
  }

  // Tabs and navigation.
  async function act<T>(path: string, body: unknown): Promise<T | null> {
    setStatus('')
    try { return await api<T>(path, body) } catch (error) { setStatus(errorText(error)); return null }
  }
  async function newTab(mode: Mode) {
    tabMenu.setOpen(false)
    const row = await act<BrowserTab>('browser/new', { mode })
    if (!row) return
    const fitted = onPhone() ? await act<BrowserTab>('browser/device', { tab: row.id, preset: 'phone' }) : null
    setTabs(list => [...list.filter(item => item.id !== row.id), fitted ?? row])
    choose(row.id)
    requestAnimationFrame(() => urlRef.current?.focus())
  }
  async function closeTab(id: string) {
    const index = tabs.findIndex(item => item.id === id)
    const state = await act<BrowserState>('browser/close', { tab: id })
    if (!state) return
    apply(state)
    if (id === tab?.id) { const next = state.tabs[Math.min(index, state.tabs.length - 1)]; if (next) choose(next.id) }
  }
  async function navigate(text: string) {
    if (!tab || !text.trim()) return
    const row = await act<BrowserTab & { error?: string }>('browser/navigate', { tab: tab.id, url: text.trim() })
    if (!row) return
    setTabs(list => list.map(item => item.id === row.id ? { ...item, url: row.url } : item))
    if (row.error) setStatus(`Could not open ${row.url}: ${row.error.replace(/^net::/, '')}`)
    setDraft(null)
    urlRef.current?.blur()
  }
  async function sendDevice(body: Record<string, unknown>) {
    const row = await act<BrowserTab>('browser/device', body)
    if (row) { setTabs(list => list.map(item => item.id === row.id ? row : item)); setFrame(null) }
    return row
  }
  async function setDevice(preset: string) {
    deviceMenu.setOpen(false)
    if (!tab) return
    await sendDevice(preset === 'desktop' ? { tab: tab.id, preset, width: Math.max(320, stage.width), height: Math.max(240, stage.height) } : { tab: tab.id, preset })
  }
  // Take over: a device request takes the viewport from the agent. Stop: the server puts back the agent's own viewport.
  async function handOver(toUser: boolean) {
    if (!tab || handing) return
    setHanding(true)
    try { await sendDevice(!toUser ? { tab: tab.id, preset: 'agent' } : onPhone() ? { tab: tab.id, preset: 'phone' } : { tab: tab.id, preset: 'desktop', width: Math.max(320, stage.width), height: Math.max(240, stage.height) }) } finally { setHanding(false) }
  }

  // Notes: draw on a still copy of the frame, then hand the PNG to this pane's composer.
  const [grabbing, setGrabbing] = useState(false)
  async function startNotes() {
    if (liveMode) {
      // The live view is video in a frame, so take one picture of the page from Chrome first.
      setGrabbing(true); setStatus('Taking a picture of the page…')
      const ok = await grabFrame()
      setGrabbing(false)
      if (!ok) { setStatus('Could not take a picture of the page. Try again.'); return }
    } else if (!canvasRef.current?.width) return
    setShapes([]); setLabel(null); setTool('pen'); setStatus(''); setNoting(true)
  }
  function grabFrame() {
    const target = tabRef.current?.id
    if (!target) return Promise.resolve(false)
    const gen = ++streamGen.current
    const sid = Array.from(crypto.getRandomValues(new Uint8Array(12)), byte => byte.toString(16).padStart(2, '0')).join('')
    const source = new EventSource(`/mobile/api/browser/stream?tab=${encodeURIComponent(target)}&w=${streamWidth}&h=${streamHeight}&cssw=${Math.max(1, stage.width)}&cssh=${Math.max(1, stage.height)}${which === 'test' ? '' : `&browser=${which}`}&sid=${sid}`)
    return new Promise<boolean>(resolve => {
      const finish = (ok: boolean) => { source.close(); clearTimeout(timer); resolve(ok) }
      const timer = window.setTimeout(() => finish(false), 10000)
      source.addEventListener('frame', event => {
        source.close()
        const message = JSON.parse((event as MessageEvent).data)
        const binary = atob(message.jpeg)
        const bytes = new Uint8Array(binary.length)
        for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index)
        createImageBitmap(new Blob([bytes], { type: 'image/jpeg' })).then(bitmap => {
          if (gen !== streamGen.current || tabRef.current?.id !== target) { bitmap.close(); finish(false); return }
          show(bitmap, message, gen); draw(); finish(true)
        }, () => finish(false))
      })
      source.addEventListener('error', () => { if (source.readyState === EventSource.CLOSED) finish(false) })
    })
  }
  function stopNotes() { setNoting(false); setShapes([]); setLabel(null) }
  const drawing = useRef<Shape | null>(null)
  function markPoint(event: { clientX: number; clientY: number }) {
    const marks = marksRef.current!
    const box = marks.getBoundingClientRect()
    return { x: (event.clientX - box.left) * marks.width / box.width, y: (event.clientY - box.top) * marks.height / box.height, left: event.clientX - box.left, top: event.clientY - box.top }
  }
  function render(list: Shape[]) {
    const marks = marksRef.current
    const context = marks?.getContext('2d')
    if (!marks || !context) return
    const unit = marks.width / Math.max(1, marks.getBoundingClientRect().width)
    context.clearRect(0, 0, marks.width, marks.height)
    context.lineCap = 'round'; context.lineJoin = 'round'
    for (const shape of list) {
      context.strokeStyle = MARK; context.fillStyle = MARK; context.lineWidth = 3 * unit
      if (shape.kind === 'pen') {
        context.beginPath()
        for (let index = 0; index < shape.points.length; index += 2) context[index ? 'lineTo' : 'moveTo'](shape.points[index], shape.points[index + 1])
        context.stroke()
      } else if (shape.kind === 'rect') {
        context.strokeRect(shape.x, shape.y, shape.w, shape.h)
      } else if (shape.kind === 'point') {
        context.beginPath(); context.arc(shape.x, shape.y, 14 * unit, 0, Math.PI * 2); context.stroke()
        context.beginPath(); context.arc(shape.x, shape.y, 4 * unit, 0, Math.PI * 2); context.fill()
      } else {
        context.font = `600 ${16 * unit}px system-ui, sans-serif`; context.textBaseline = 'middle'
        context.lineWidth = 4 * unit; context.strokeStyle = '#fff'; context.strokeText(shape.text, shape.x, shape.y)
        context.fillText(shape.text, shape.x, shape.y)
      }
    }
  }
  useEffect(() => {
    const canvas = canvasRef.current, marks = marksRef.current
    if (!noting || !canvas || !marks) return
    if (marks.width !== canvas.width || marks.height !== canvas.height) { marks.width = canvas.width; marks.height = canvas.height }
    render(drawing.current ? [...shapes, drawing.current] : shapes)
  }, [noting, shapes])
  function onMarkDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    const point = markPoint(event)
    if (label) commitLabel()
    if (tool === 'point') { setShapes(list => [...list, { kind: 'point', x: point.x, y: point.y }]); return }
    if (tool === 'text') { setLabel({ ...point, text: '' }); return }
    event.currentTarget.setPointerCapture(event.pointerId)
    drawing.current = tool === 'pen' ? { kind: 'pen', points: [point.x, point.y] } : { kind: 'rect', x: point.x, y: point.y, w: 0, h: 0 }
  }
  function onMarkMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    const shape = drawing.current
    if (!shape) return
    const point = markPoint(event)
    if (shape.kind === 'pen') shape.points.push(point.x, point.y)
    else if (shape.kind === 'rect') { shape.w = point.x - shape.x; shape.h = point.y - shape.y }
    render([...shapes, shape])
  }
  function onMarkUp() {
    const shape = drawing.current
    drawing.current = null
    if (shape && (shape.kind !== 'rect' || (Math.abs(shape.w) > 2 && Math.abs(shape.h) > 2))) setShapes(list => [...list, shape])
  }
  function commitLabel() {
    if (label?.text.trim()) setShapes(list => [...list, { kind: 'text', x: label.x, y: label.y, text: label.text.trim() }])
    setLabel(null)
  }
  async function sendToChat() {
    const canvas = canvasRef.current, marks = marksRef.current
    if (!canvas || !marks) return
    const list = label?.text.trim() ? [...shapes, { kind: 'text' as const, x: label.x, y: label.y, text: label.text.trim() }] : shapes
    render(list)
    const out = document.createElement('canvas')
    out.width = canvas.width; out.height = canvas.height
    const context = out.getContext('2d')!
    context.drawImage(canvas, 0, 0); context.drawImage(marks, 0, 0)
    const blob = await new Promise<Blob | null>(resolve => out.toBlob(resolve, 'image/png'))
    if (!blob) { setStatus('Could not make the image.'); return }
    let host = 'page'
    try { host = new URL(tab?.url ?? '').host.replace(/[^\w.-]+/g, '-') || 'page' } catch { /* about:blank */ }
    const file = new File([blob], `browser-${host}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.png`, { type: 'image/png' })
    const taken = !window.dispatchEvent(new CustomEvent(ATTACH_EVENT, { cancelable: true, detail: { pane: pane.id, files: [file] } }))
    if (!taken) { setStatus('This pane does not take files. Choose a Claude or Codex pane, then send again.'); return }
    stopNotes()
    setStatus('Image added to the message box.')
  }

  // Fit the frame (or the tab's viewport before the first frame) inside the stage.
  const size = windowView ? { width: 2560, height: 1600 } : frame ?? (tab ? { width: tab.width, height: tab.height } : null)
  const scale = size && stage.width && stage.height ? Math.min(stage.width / size.width, stage.height / size.height) : 0
  const view = size && scale ? { width: Math.floor(size.width * scale), height: Math.floor(size.height * scale) } : { width: 0, height: 0 }
  const presetLabel = presets.find(item => item.id === tab?.preset)?.label ?? 'Desktop'
  function changeWindowView(value: boolean) {
    setWindowView(value); setFrame(null); setStatus(''); deviceMenu.setOpen(false)
    painted.current?.bitmap.close()
    painted.current = null
    reset()
    canvasRef.current?.getContext('2d')?.clearRect(0, 0, canvasRef.current.width, canvasRef.current.height)
  }

  const chosen = BROWSERS.find(item => item.id === which)!
  const switcher = <div className="browser-menu-anchor" ref={switchMenu.ref} onKeyDown={switchMenu.onKeyDown}>
    <button type="button" className="browser-switch" aria-label={`Browser: ${chosen.label}`} aria-haspopup="menu" aria-expanded={switchMenu.open} onClick={() => switchMenu.setOpen(!switchMenu.open)}>
      <chosen.icon size={15} aria-hidden="true" /><span>{chosen.label}</span><ChevronDown size={14} aria-hidden="true" />
    </button>
    {switchMenu.open && <div className="browser-menu" role="menu">
      {BROWSERS.map(item => {
        const row = browsers.find(entry => entry.id === item.id)
        const off = row && item.id !== 'chrome' ? !row.available : false
        return <button key={item.id} type="button" role="menuitemradio" aria-checked={item.id === which} autoFocus={item.id === which} disabled={off}
          onClick={() => { switchMenu.setOpen(false); if (item.id !== which) onSwitch(item.id) }}>
          <item.icon size={16} /><span>{item.label} browser<small>{off || (row && !row.available) ? row?.reason : item.note}</small></span>{item.id === which && <Check size={16} />}
        </button>
      })}
    </div>}
  </div>

  if (!loaded) return <div className="browser-panel"><p className="browser-empty">{active ? (which === 'signed' ? 'Starting Google Chrome…' : which === 'chrome' ? 'Connecting to your Chrome…' : 'Starting the browser…') : ''}</p></div>
  if (failure && !tabs.length) return <div className="browser-panel"><div className="browser-empty"><p>{failure}</p><RetryButton className="browser-text-button" onRetry={load} />
    {which !== 'test' && <button type="button" className="browser-text-button" onClick={() => onSwitch('test')}>Use the test browser</button>}</div></div>

  // Your Chrome opens tabs only through Allow, so an empty Chrome view is the pairing card.
  if (which === 'chrome' && !tabs.length) return <div className="browser-panel">
    <div className="browser-tabs" role="toolbar" aria-label="Browser">{switcher}</div>
    <MacPairing api={baseApi} connected={!!browsers.find(row => row.id === 'chrome')?.available} />
  </div>

  return <div className={`browser-panel${which === 'signed' ? ' browser-signed' : ''}`} onKeyDown={event => { if (noting && event.key === 'Escape' && !event.defaultPrevented) stopNotes() }}>
    <div className="browser-tabs" role="tablist" aria-label="Browser tabs">
      {switcher}
      {tabs.map(item => {
        const name = tabName(item)
        return <div key={item.id} className={item.id === tab?.id ? 'browser-tab active' : 'browser-tab'} title={item.mode === 'saved' ? `${name} · ${which === 'signed' ? signedNote(host) : savedNote(host)}` : item.mode === 'agent' ? `${name} · Agent` : `${name} · Private`}>
          <button type="button" role="tab" aria-selected={item.id === tab?.id} className="browser-tab-main" onClick={() => choose(item.id)}>
            {item.mode === 'saved' ? <KeyRound size={14} aria-label="Saved" /> : item.mode === 'agent' ? <Bot size={14} aria-label="Agent" /> : <HatGlasses size={14} aria-label="Private" />}
            <span>{name}</span>
          </button>
          <button type="button" className="browser-tab-close" aria-label={`Close ${name}`} onClick={() => void closeTab(item.id)}><X size={14} /></button>
        </div>
      })}
      {which !== 'chrome' && <div className="browser-menu-anchor" ref={tabMenu.ref} onKeyDown={tabMenu.onKeyDown}>
        <button type="button" className="browser-icon" aria-label="New tab" aria-haspopup={which === 'signed' ? undefined : 'menu'} aria-expanded={which === 'signed' ? undefined : tabMenu.open} onClick={() => which === 'signed' ? void newTab('saved') : tabMenu.setOpen(!tabMenu.open)}><Plus size={17} /></button>
        {tabMenu.open && <div className="browser-menu" role="menu">
          <button type="button" role="menuitem" autoFocus onClick={() => void newTab('private')}><HatGlasses size={16} /><span>New private tab<small>Logins go away when the tab closes</small></span></button>
          <button type="button" role="menuitem" onClick={() => void newTab('saved')}><KeyRound size={16} /><span>New saved tab<small>{savedNote(host)}</small></span></button>
        </div>}
      </div>}
      {onPopOut && <button type="button" className="browser-icon browser-popout" aria-label="Open the browser in its own window" title="Open in a window" onClick={onPopOut}><PictureInPicture2 size={17} /></button>}
    </div>

    {noting ? <div className="browser-bar browser-notes" role="toolbar" aria-label="Notes">
      {TOOLS.map(item => <button key={item.id} type="button" className="browser-icon" aria-label={item.label} title={item.label} aria-pressed={tool === item.id} onClick={() => { commitLabel(); setTool(item.id) }}><item.icon size={17} /></button>)}
      <span className="browser-divider" />
      <button type="button" className="browser-icon" aria-label="Clear notes" title="Clear" disabled={!shapes.length && !label} onClick={() => { setShapes([]); setLabel(null) }}><Trash2 size={17} /></button>
      <span className="browser-grow" />
      <button type="button" className="browser-icon" aria-label="Close notes" title="Close" onClick={stopNotes}><X size={17} /></button>
      <button type="button" className="browser-send" onClick={() => void sendToChat()}><Send size={16} /><span>Send to chat</span></button>
    </div> : <form className="browser-bar" onSubmit={event => { event.preventDefault(); if (!watching && !windowView) void navigate(draft ?? '') }}>
      <button type="button" className="browser-icon" aria-label="Back" disabled={!nav.back || watching || windowView} onClick={() => tab && void act('browser/history', { tab: tab.id, action: 'back' })}><ArrowLeft size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Forward" disabled={!nav.forward || watching || windowView} onClick={() => tab && void act('browser/history', { tab: tab.id, action: 'forward' })}><ArrowRight size={17} /></button>
      <button type="button" className="browser-icon" aria-label="Reload" disabled={!tab || watching || windowView} onClick={() => tab && void act('browser/history', { tab: tab.id, action: 'reload' })}><RotateCw size={16} /></button>
      <input ref={urlRef} className="browser-url" aria-label="Address" readOnly={watching} disabled={windowView} placeholder="Address or port, like 8765" spellCheck={false} autoCapitalize="off" autoCorrect="off" enterKeyHint="go" inputMode="url"
        value={draft ?? shown(tab?.url ?? '')} onFocus={event => { setDraft(shown(tab?.url ?? '')); event.currentTarget.select() }} onBlur={() => setDraft(null)} onChange={event => setDraft(event.target.value)}
        onKeyDown={event => { if (event.key === 'Escape') { setDraft(null); event.currentTarget.blur() } }} />
      <div className="browser-menu-anchor" ref={deviceMenu.ref} onKeyDown={deviceMenu.onKeyDown}>
        <button type="button" className="browser-icon" aria-label={`Device: ${presetLabel}`} title={`Device: ${presetLabel}`} aria-haspopup="menu" aria-expanded={deviceMenu.open} disabled={!tab || watching || windowView} onClick={() => deviceMenu.setOpen(!deviceMenu.open)}><DeviceIcon preset={tab?.preset ?? 'desktop'} /></button>
        {deviceMenu.open && <div className="browser-menu end" role="menu">
          {presets.map(item => <button key={item.id} type="button" role="menuitemradio" aria-checked={item.id === tab?.preset} autoFocus={item.id === tab?.preset} onClick={() => void setDevice(item.id)}>
            <DeviceIcon preset={item.id} /><span>{item.label}<small>{item.id === 'desktop' ? 'Fits the panel' : `${item.width} × ${item.height}`}</small></span>{item.id === tab?.preset && <Check size={16} />}
          </button>)}
        </div>}
      </div>
      <button type="button" className="browser-icon browser-touch-only" aria-label="Type into the page" disabled={watching && !windowView} onClick={() => keysRef.current?.focus()}><Keyboard size={17} /></button>
      {which === 'signed' && <button type="button" className="browser-icon" aria-label="Show the whole Chrome window" title="Whole window: Chrome's dialogs, toolbar and extensions" aria-pressed={windowView} onClick={() => changeWindowView(!windowView)}><AppWindow size={17} /></button>}
      <button type="button" className="browser-icon" aria-label="Add notes to this frame" title="Notes" disabled={(!frame && !liveMode) || windowView || grabbing} onClick={() => void startNotes()}><PenLine size={17} /></button>
    </form>}

    {(windowView || tab?.mode === 'saved') && <p className="browser-saved">{which === 'signed' ? <><UserRound size={13} aria-hidden="true" />
      <span>{windowView ? "Whole Chrome window · use Chrome's own toolbar and dialogs" : signedNote(host)}</span>
      {!windowView && <button type="button" className="browser-window-link" onClick={() => changeWindowView(true)}>Dialog not showing? Show the whole window</button>}
    </> : <><KeyRound size={13} />{savedNote(host)}</>}</p>}
    {tab?.mode === 'agent' && <div className="browser-agent" role="note">
      <Bot size={14} aria-hidden="true" />
      <span>{watching ? `${owner ?? 'An agent'} uses this tab. You watch.` : `You control this tab. ${owner ?? 'The agent'} can still act.`}</span>
      <button type="button" className="browser-text-button" aria-pressed={!watching} disabled={handing} onClick={() => void handOver(watching)}>{handing ? '…' : watching ? 'Take over' : 'Give back'}</button>
    </div>}

    <div className={typing ? 'browser-stage typing' : 'browser-stage'} ref={stageSlot} tabIndex={-1} aria-label="Page">
      <div className="browser-screen" style={view} hidden={liveView}>
        <canvas ref={canvasSlot} className="browser-canvas" aria-hidden="true" style={view}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => { drag.current = null; const press = pressed.current; pressed.current = null; if (press) push({ type: 'up', x: press.x, y: press.y, button: press.button, buttons: 0, clicks: 1, modifiers: 0 }) }} onContextMenu={event => event.preventDefault()}
          onMouseDown={event => event.preventDefault() /* keep focus in the keys box, not the stage */} />
        {noting && <canvas ref={marksRef} className={`browser-marks tool-${tool}`} aria-label="Draw notes on the frame" style={view} onPointerDown={onMarkDown} onPointerMove={onMarkMove} onPointerUp={onMarkUp} onPointerCancel={onMarkUp} />}
        {noting && label && <input className="browser-label" style={{ left: label.left, top: label.top }} aria-label="Note text" autoFocus value={label.text}
          onChange={event => setLabel({ ...label, text: event.target.value })} onBlur={commitLabel}
          onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); commitLabel() } else if (event.key === 'Escape') { event.stopPropagation(); setLabel(null) } }} />}
      </div>
      {liveMode && liveReach === 'ok' && <iframe key={liveBase?.run} ref={liveFrameRef} className="browser-live" src={liveSrc} hidden={noting} inert={watching}
        title={`Page: ${tab?.title || tab?.url || 'New tab'}`} sandbox="allow-scripts allow-same-origin" allow="clipboard-read; clipboard-write"
        referrerPolicy="no-referrer" onLoad={() => setLiveReady(true)} />}
      {liveView && liveReach === 'ok' && watching && <div className="browser-live-block" aria-hidden="true" />}
      {(liveView ? !liveReady : !frame && (tab || windowView)) && <p className="browser-wait"><span><Spinner /> {live || liveView ? windowView ? 'Loading the Chrome window…' : 'Loading the page…' : 'Connecting…'}</span></p>}
      <textarea ref={keysRef} className="browser-keys" aria-label="Keys for the page. Press Escape twice to leave." defaultValue={SENTINEL} autoCapitalize="off" autoCorrect="off" spellCheck={false}
        onFocus={() => setTyping(true)} onBlur={() => { setTyping(false); const events = releases(); if (events.length) push(...events) }} onKeyDown={onKeyDown} onKeyUp={onKeyUp}
        onInput={event => { if (!(event.nativeEvent as InputEvent).isComposing) syncText() }} onCompositionEnd={syncText}
        onPaste={event => { event.preventDefault(); const text = event.clipboardData.getData('text/plain'); if (text) push({ type: 'text', text: text.slice(0, 20000) }) }} />
    </div>
    <p className="browser-status" role="status">{status || (typing ? 'Keys go to the page. Press Esc twice to leave.' : !live && frame && !liveMode ? 'Connecting again…'
      : liveWanted && liveReach === 'down' ? `Live video is off: port ${liveBase?.port} did not answer. Showing pictures instead.` : '')}</p>
  </div>
}
