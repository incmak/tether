import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ArrowLeft, ExternalLink, GitBranch, Hand, Hourglass, Tv, X } from 'lucide-react'
import type { Api, Footer, GroupInfo, Pane } from './api'
import { parseBacklog } from './backlogItems'
import type { Row } from './backlogItems'
import { BACKLOG_PATH } from './backlogNews'
import { useBackStep } from './backNav'
import { formatElapsed } from './paneState'
import { PHASES, PHASE_NAMES, STATE_NAMES, SWEEP_NAMES, attentionOrder, buildFloor, clock, groupsOf, historyMoves, nearest, phaseSince, toMs } from './factoryModel'
import type { Agent, Direction, FactoryData, History, Phase, Sweep, SweepKind, SweepLine, Task } from './factoryModel'
import { fmtDate, fmtDay, sameDay, useTetherZone } from './zone'
import './Factory.css'

/* The Factory view: every agent tab and backlog item as work moving through phases, on one canvas with four zoom
   levels: the floor (all phases), one phase, one task, one agent. Made for a big screen across the room, and it
   still works from 320 px wide, where each phase is a list.
   Read-only. The only action is "Open tab", which leaves the view for that tab.
   Input: click, ctrl+wheel or a two-finger pinch to zoom in or out, arrows move focus to the nearest item in that direction,
   Enter zooms in, Esc or Backspace zooms out (a TV remote's Back key too). Phone Back zooms out one level. */

const FACTORY_MS = 5000
const BACKLOG_MS = 20000
const SCREEN_MS = 3000
const STALE_MS = 20000
const IDLE_MS = 60000          // ambient tour starts after a minute with no input
const TOUR_MS = 12000          // and shows each task this long
const ANNOUNCE_MS = 15000      // at most one live-region message this often
const FLOOR_TOKENS = 10        // tokens a station shows before "+N more"
const PINCH_SPREAD_RATIO = 1.35
const PINCH_SQUEEZE_RATIO = 0.75
const PINCH_MIN_CHANGE = 40   // CSS pixels
const HISTORY_KEY = 'tether-factory-history'
const HISTORY_BATCH = 256 // factory_history.MAX_MOVES
const TOUR_KEY = 'tether-factory-tour'
const BACK_KEYS = new Set(['Escape', 'Backspace', 'BrowserBack', 'GoBack'])
const ARROWS: Record<string, Direction> = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' }

type Level = { phase?: Phase; task?: string; agent?: string }
type Props = { api: Api; panes?: Pane[]; groups?: Record<string, GroupInfo[]>; onOpen: (pane: Pane) => void; onClose: () => void }

const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
const load = <T,>(key: string, fallback: T): T => { try { const raw = localStorage.getItem(key); return raw ? JSON.parse(raw) as T : fallback } catch { return fallback } }
const save = (key: string, value: unknown) => { try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode: preference lasts this visit */ } }
const ago = (ms: number, now: number) => ms ? formatElapsed((now - ms) / 1000) : 'unknown'
/** A time, with the day when it is not today. */
const when = (ms: number) => sameDay(ms, Date.now()) ? clock(ms) : `${fmtDate(ms, { day: 'numeric', month: 'short' })} ${clock(ms)}`
/** When it last moved: a working agent is moving now, not "0s ago". */
const lastMove = (busy: boolean, ms: number, now: number) => busy ? 'active now' : ms ? `moved ${ago(ms, now)} ago` : ''
const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden'

/** Poll a read-only endpoint while the page is visible. */
function usePoll<T>(api: Api, path: string | null, every: number) {
  const [value, setValue] = useState<{ data: T | null; at: number; error: string; path: string | null }>({ data: null, at: 0, error: '', path })
  useEffect(() => {
    if (!path) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const control = new AbortController()
    const run = () => {
      if (!visible()) { timer = setTimeout(run, every); return }
      api<T>(path, undefined, control.signal, undefined, 10000, 'low').then(
        data => { if (live) setValue({ data, at: Date.now(), error: '', path }) },
        (error: Error) => { if (live && error.name !== 'AbortError') setValue(old => ({ ...old, error: error.message || 'Server unreachable', path })) },
      ).finally(() => { if (live) timer = setTimeout(run, every) })
    }
    run()
    return () => { live = false; clearTimeout(timer); control.abort() }
  }, [api, path, every])
  return value.path === path ? value : { data: null, at: 0, error: '', path }
}

export function Factory({ api, panes, groups, onOpen, onClose }: Props) {
  const zone = useTetherZone()
  const dialog = useRef<HTMLDialogElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const factory = usePoll<FactoryData>(api, 'factory', FACTORY_MS)
  const backlog = usePoll<{ text: string }>(api, BACKLOG_PATH, BACKLOG_MS)
  const rows: Row[] = useMemo(() => parseBacklog(backlog.data?.text ?? ''), [backlog.data])
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(timer) }, [])
  const stackName = useCallback((pane: Pane) => Object.values(groups ?? {}).flat().find(group => group.id === pane.group)?.name, [groups])
  const ready = !!factory.data && !!backlog.data
  const floor = useMemo(() => ready ? buildFloor({ panes: panes ?? [], rows, data: factory.data, groupName: stackName, now, zone }) : null, [ready, panes, rows, factory.data, stackName, now, zone])

  const history = factory.data?.history ?? {}
  const report = useRef<{ data: FactoryData; inFlight: boolean } | null>(null)
  const pending = useRef(new Map<string, Phase>())
  const seen = useRef(new Set<string>())
  useEffect(() => {
    try { localStorage.removeItem(HISTORY_KEY) } catch { /* storage can be disabled */ }
  }, [])

  // Announce transitions observed on this screen once, even while a history write is pending.
  const [announce, setAnnounce] = useState('')
  const queue = useRef<string[]>([])
  const phases = useRef<Map<string, Phase> | null>(null)
  const waited = useRef<Set<string> | null>(null)
  const [moved, setMoved] = useState<Set<string>>(new Set())
  useEffect(() => {
    // The open backlog alone can look like every task moved back to Inbox while sessions load.
    if (!floor || !panes) return
    const data = factory.data
    const previous = phases.current
    const decision = data?.history
      ? historyMoves(data.history, data.now * 1000, previous, seen.current, floor.tasks, pending.current)
      : null
    if (decision) pending.current = decision.pending
    for (const task of floor.tasks) seen.current.add(task.id)

    const changes = floor.tasks.filter(task => previous?.has(task.id) && previous.get(task.id) !== task.phase)
    // The clock rebuilds the floor every tick: keep the marks until the tasks or their phases change.
    const same = !!previous && previous.size === floor.tasks.length && floor.tasks.every(task => previous.get(task.id) === task.phase)
    phases.current = new Map(floor.tasks.map(task => [task.id, task.phase]))
    if (!same) setMoved(new Set(changes.map(task => task.id)))
    for (const task of changes) queue.current.push(`${label(task)} moved to ${PHASE_NAMES[task.phase]}.`)
    const waiting = new Set(floor.tasks.filter(task => task.waits).map(task => task.id))
    if (waited.current) for (const task of floor.tasks) if (task.waits && !waited.current.has(task.id)) queue.current.push(`${label(task)} waits for you.`)
    waited.current = waiting

    // Record evidence on every complete floor. Send at most one attempt per Factory response.
    if (!data || !decision || report.current?.data === data || report.current?.inFlight) return
    const { moves } = decision
    const attempt = { data, inFlight: moves.length > 0 }
    report.current = attempt
    if (!attempt.inFlight) return
    void (async () => {
      try {
        for (let start = 0; start < moves.length; start += HISTORY_BATCH) {
          const batch = moves.slice(start, start + HISTORY_BATCH).filter(move => pending.current.get(move.task) === move.phase)
          if (batch.length) await api('factory/history', { moves: batch })
        }
      } catch { /* keep pending moves for the next Factory poll */ }
      finally { attempt.inFlight = false }
    })()
  }, [api, factory.data, floor, panes])
  useEffect(() => {
    const timer = setInterval(() => {
      if (!queue.current.length) return
      const items = queue.current.splice(0)
      setAnnounce(items.length > 3 ? `${items.slice(0, 3).join(' ')} And ${items.length - 3} more changes.` : items.join(' '))
    }, ANNOUNCE_MS)
    return () => clearInterval(timer)
  }, [])

  // Where we are. A task or agent that leaves the floor falls back to the level above.
  const [level, setLevelState] = useState<Level>({})
  const task = floor?.tasks.find(entry => entry.id === level.task)
  const agent = task?.agents.find(entry => entry.pane.id === level.agent)
  const shown: Level = { phase: level.phase, task: task ? task.id : undefined, agent: task && agent ? agent.pane.id : undefined }
  const depth = shown.agent ? 3 : shown.task ? 2 : shown.phase ? 1 : 0
  const returnTo = useRef<string | null>(null)
  const loaded = !!floor
  const setLevel = useCallback((next: Level, from?: string) => {
    returnTo.current = from ?? null
    const apply = () => setLevelState(next)
    const start = (document as Document & { startViewTransition?: (update: () => void) => unknown }).startViewTransition
    if (start && !reduced()) start.call(document, apply)
    else apply()
  }, [])
  const out = useCallback(() => {
    if (shown.agent) setLevel({ phase: shown.phase, task: shown.task }, `[data-key="agent:${shown.agent}"]`)
    else if (shown.task) setLevel({ phase: shown.phase }, `[data-key="task:${shown.task}"]`)
    else if (shown.phase) setLevel({}, `[data-key="phase:${shown.phase}"]`)
    else onClose()
  }, [shown.agent, shown.task, shown.phase, setLevel, onClose])
  // Phone Back: one history step per zoom level, so Back zooms out one level at a time.
  useBackStep(true, onClose, { view: 'factory' })
  useBackStep(depth >= 1, () => setLevelState({}))
  useBackStep(depth >= 2, () => setLevelState(old => ({ phase: old.phase })))
  useBackStep(depth >= 3, () => setLevelState(old => ({ phase: old.phase, task: old.task })))

  // Focus: after a zoom in, the first item of the new level; after a zoom out, the item we came from.
  useLayoutEffect(() => {
    const root = body.current
    if (!root) return
    const back = returnTo.current ? root.querySelector<HTMLElement>(returnTo.current) : null
    const target = back ?? root.querySelector<HTMLElement>('[data-first]') ?? root.querySelector<HTMLElement>('button, a[href]')
    target?.focus()
    returnTo.current = null
  }, [depth, shown.phase, shown.task, shown.agent, loaded])

  useEffect(() => { const box = dialog.current; if (box && !box.open) box.showModal() }, [])

  // Ambient tour: after a minute with no input, show the tasks that need Mak, one after another. Any input stops it.
  const [tour, setTourState] = useState<boolean>(() => load(TOUR_KEY, false))
  const [touring, setTouring] = useState(false)
  const lastInput = useRef(0)
  useEffect(() => { lastInput.current = Date.now() }, [])
  const setTour = (on: boolean) => { setTourState(on); save(TOUR_KEY, on); lastInput.current = Date.now() }
  const tasksRef = useRef<Task[]>([])
  useEffect(() => { tasksRef.current = floor?.tasks ?? [] }, [floor])
  useEffect(() => {
    if (!tour) return
    let step = 0
    const timer = setInterval(() => {
      if (Date.now() - lastInput.current < IDLE_MS) return
      const list = tasksRef.current.filter(entry => entry.waits || entry.stuck).sort(attentionOrder)
      const pool = list.length ? list : tasksRef.current.filter(entry => entry.agents.some(one => one.state === 'working'))
      if (!pool.length) return
      setTouring(true)
      if (step % Math.round(TOUR_MS / 1000) === 0) {
        const next = pool[Math.floor(step / Math.round(TOUR_MS / 1000)) % pool.length]
        setLevelState({ phase: next.phase, task: next.id })
      }
      step += 1
    }, 1000)
    return () => clearInterval(timer)
  }, [tour])
  useEffect(() => {
    const stop = () => { lastInput.current = Date.now(); setTouring(false) }
    const events = ['keydown', 'pointerdown', 'wheel', 'touchstart'] as const
    events.forEach(name => window.addEventListener(name, stop, { capture: true, passive: true }))
    return () => events.forEach(name => window.removeEventListener(name, stop, { capture: true }))
  }, [])

  // Keys: arrows move focus spatially, Esc/Backspace/Back zoom out. Enter is the focused button's own click.
  const keyDown = (event: React.KeyboardEvent) => {
    const target = event.target as HTMLElement
    if (target.closest('input, textarea, select, [contenteditable="true"]')) return
    if (BACK_KEYS.has(event.key)) { event.preventDefault(); out(); return }
    const direction = ARROWS[event.key]
    if ((direction === 'up' || direction === 'down') && target.classList.contains('factory-screen')) return  // the screen scrolls
    if (!direction || event.altKey || event.metaKey || event.ctrlKey) return
    const items = [...(dialog.current?.querySelectorAll<HTMLElement>('.factory-bar button, .factory-body button, .factory-body a[href], .factory-screen') ?? [])].filter(item => item.getClientRects().length)
    if (!items.length) return
    event.preventDefault()
    const from = items.indexOf(document.activeElement as HTMLElement)
    if (from < 0) { items[0].focus(); return }
    const boxes = items.map(item => item.getBoundingClientRect())
    const found = nearest(boxes[from], boxes, direction)
    if (found >= 0) { items[found].focus(); items[found].scrollIntoView({ block: 'nearest', inline: 'nearest' }) }
  }
  // Wheel: ctrl+wheel (a trackpad pinch sends this) zooms in on the item under the pointer, or out.
  const wheelAt = useRef(0)
  useEffect(() => {
    const box = dialog.current
    if (!box) return
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return
      event.preventDefault()
      if (Date.now() - wheelAt.current < 450 || Math.abs(event.deltaY) < 4) return
      wheelAt.current = Date.now()
      if (event.deltaY > 0) { out(); return }
      const item = (event.target as HTMLElement).closest<HTMLElement>('[data-zoom]')
      item?.click()
    }
    box.addEventListener('wheel', wheel, { passive: false })
    return () => box.removeEventListener('wheel', wheel)
  }, [out])

  // Touch pinch: use the starting midpoint and take one step until a finger lifts.
  const pinch = useRef({
    pointers: new Map<number, { x: number; y: number }>(),
    start: null as { first: number; second: number; distance: number; item: HTMLElement | null } | null,
  })
  useEffect(() => {
    const box = dialog.current
    if (!box) return
    const state = pinch.current
    const { pointers } = state
    const down = (event: PointerEvent) => {
      if (event.pointerType !== 'touch' || pointers.has(event.pointerId)) return
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
      if (pointers.size !== 2) return
      const [[first, a], [second, b]] = pointers
      const item = document.elementFromPoint((a.x + b.x) / 2, (a.y + b.y) / 2)?.closest<HTMLElement>('[data-zoom]')
      state.start = {
        first, second, distance: Math.hypot(a.x - b.x, a.y - b.y),
        item: item && box.contains(item) ? item : null,
      }
      // Keep receiving touch events when zoom replaces the items under the fingers.
      if (event.isTrusted) {
        box.setPointerCapture(first)
        box.setPointerCapture(second)
      }
    }
    const move = (event: PointerEvent) => {
      if (event.pointerType !== 'touch' || !pointers.has(event.pointerId)) return
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
      const start = state.start
      if (!start || pointers.size !== 2) return
      const a = pointers.get(start.first), b = pointers.get(start.second)
      if (!a || !b) return
      const distance = Math.hypot(a.x - b.x, a.y - b.y)
      if (Math.abs(distance - start.distance) < PINCH_MIN_CHANGE) return
      if (distance >= start.distance * PINCH_SPREAD_RATIO) {
        state.start = null
        if (start.item && box.contains(start.item)) start.item.click()
      } else if (distance <= start.distance * PINCH_SQUEEZE_RATIO) {
        state.start = null
        // A squeeze at the floor leaves Factory open.
        if (depth > 0) out()
      }
    }
    const end = (event: PointerEvent) => {
      if (event.pointerType !== 'touch') return
      pointers.delete(event.pointerId)
      if (pointers.size < 2) state.start = null
    }
    const touchmove = (event: TouchEvent) => {
      if (event.touches.length >= 2 && event.cancelable) event.preventDefault()
    }
    box.addEventListener('pointerdown', down, { passive: true })
    box.addEventListener('pointermove', move, { passive: true })
    // A finger can end outside the dialog (its item was removed, or it left the screen): listen on the window.
    window.addEventListener('pointerup', end, { capture: true, passive: true })
    window.addEventListener('pointercancel', end, { capture: true, passive: true })
    box.addEventListener('touchmove', touchmove, { passive: false })
    return () => {
      box.removeEventListener('pointerdown', down)
      box.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', end, { capture: true })
      window.removeEventListener('pointercancel', end, { capture: true })
      box.removeEventListener('touchmove', touchmove)
    }
  }, [depth, out])

  // Tokens that change station travel there (FLIP). Reduced motion: they just appear in the new place.
  const places = useRef(new Map<string, { rect: DOMRect; phase: string }>())
  useLayoutEffect(() => {
    const root = body.current
    if (!root || depth !== 0) return
    const next = new Map<string, { rect: DOMRect; phase: string }>()
    root.querySelectorAll<HTMLElement>('[data-token]').forEach(token => {
      const id = token.dataset.token!
      const rect = token.getBoundingClientRect()
      const phase = token.dataset.phase ?? ''
      next.set(id, { rect, phase })
      const old = places.current.get(id)?.rect
      if (old && places.current.get(id)?.phase !== phase && !reduced() && typeof token.animate === 'function') {
        token.animate([{ transform: `translate(${old.left - rect.left}px, ${old.top - rect.top}px)` }, { transform: 'none' }], { duration: 900, easing: 'cubic-bezier(0.2, 0, 0, 1)' })
        token.animate([{ boxShadow: '0 0 0 4px var(--accent)' }, { boxShadow: '0 0 0 0 transparent' }], { duration: 2400, delay: 900 })
      }
    })
    places.current = next
  }, [floor, depth])

  const age = factory.at ? now - factory.at : 0
  const crumbs: { key: string; text: string; level: Level }[] = [{ key: 'floor', text: 'Factory', level: {} }]
  if (shown.phase) crumbs.push({ key: 'phase', text: PHASE_NAMES[shown.phase], level: { phase: shown.phase } })
  if (task) crumbs.push({ key: 'task', text: task.ref ?? task.title, level: { phase: shown.phase, task: task.id } })
  if (agent) crumbs.push({ key: 'agent', text: agent.name, level: shown })
  const counts = floor ? { working: floor.tasks.filter(entry => entry.agents.some(one => one.state === 'working')).length, waits: floor.tasks.filter(entry => entry.waits).length, stuck: floor.tasks.filter(entry => entry.stuck).length } : null

  let content: ReactNode
  if (!floor && (factory.error || backlog.error)) content = <Problem title="The server does not answer" text={`${factory.error || backlog.error}. The view tries again every few seconds.`} />
  else if (!floor) content = <p className="factory-loading" role="status">Loading the factory…</p>
  else if (!floor.tasks.length) content = <Problem title="No work on the floor" text={`No agent tab is open and no backlog item is open.${floor.parked ? ` ${floor.parked} idle tabs ended over a day ago.` : ''} Start a tab with New session.`} />
  else if (agent && task) content = <AgentLevel api={api} agent={agent} task={task} now={now} onOpen={pane => { onOpen(pane); onClose() }} />
  else if (task) content = <TaskLevel task={task} history={history[task.id] ?? []} now={now} zoom={next => setLevel({ phase: shown.phase, task: task.id, agent: next.pane.id })} />
  else if (shown.phase) content = <PhaseLevel phase={shown.phase} tasks={floor.tasks.filter(entry => entry.phase === shown.phase)} history={history} now={now} zoom={next => setLevel({ phase: shown.phase, task: next.id })} />
  else content = <FloorLevel floor={floor.tasks} parked={floor.parked} now={now} moved={moved} zoomPhase={phase => setLevel({ phase })} zoomTask={next => setLevel({ phase: next.phase, task: next.id })} />

  return createPortal(<dialog ref={dialog} className="factory" aria-label="Factory view" onCancel={event => event.preventDefault()} onKeyDown={keyDown}>
    <header className="factory-bar">
      <button type="button" className="factory-back" onClick={out} aria-label={depth ? 'Zoom out' : 'Close the Factory view'}>{depth ? <ArrowLeft aria-hidden="true" /> : <X aria-hidden="true" />}</button>
      <nav className="factory-crumbs" aria-label="Zoom level">
        <ol>{crumbs.map((crumb, index) => <li key={crumb.key}>{index === crumbs.length - 1
          ? <span aria-current="location">{crumb.text}</span>
          : <button type="button" onClick={() => setLevel(crumb.level)}>{crumb.text}</button>}</li>)}</ol>
      </nav>
      {counts && <p className="factory-counts">
        <span><b>{counts.working}</b> working</span>
        <span className={counts.waits ? 'is-waits' : ''}><Hand aria-hidden="true" /><b>{counts.waits}</b> wait for you</span>
        <span className={counts.stuck ? 'is-stuck' : ''}><Hourglass aria-hidden="true" /><b>{counts.stuck}</b> stuck</span>
      </p>}
      <button type="button" className={`factory-tour${tour ? ' is-on' : ''}`} aria-pressed={tour} onClick={() => setTour(!tour)} title="After a minute with no input, show the tasks that need you one by one">
        <Tv aria-hidden="true" /><span>Tour{touring ? ': on' : ''}</span>
      </button>
    </header>
    {(factory.error || age > STALE_MS) && floor && <p className="factory-stale" role="status">
      {factory.error ? 'The server does not answer.' : 'The data is late.'} Shown data is from {when(factory.at)}{age >= 60000 ? `, ${formatElapsed(age / 1000)} old` : ''}.
    </p>}
    {backlog.error && backlog.data && <p className="factory-stale" role="status">The backlog does not load. Items are from {when(backlog.at)}.</p>}
    {!depth && floor && factory.data?.sweep && <SweepStrip sweep={factory.data.sweep} />}
    {touring && <p className="factory-touring" role="status">Tour: tasks that need you, one by one. Press any key to stop.</p>}
    <div ref={body} className={`factory-body level-${depth}`}>{content}</div>
    <p className="visually-hidden" aria-live="polite">{announce}</p>
  </dialog>, document.body)
}

const label = (task: Task) => task.ref ? `${task.ref} ${task.title}` : task.title
const Problem = ({ title, text }: { title: string; text: string }) => <div className="factory-problem" role="status"><h2>{title}</h2><p>{text}</p></div>

function Flags({ task, now }: { task: Task; now: number }) {
  return <>
    {task.waits && <span className="factory-flag is-waits"><Hand aria-hidden="true" />Waits for you</span>}
    {task.stuck && <span className="factory-flag is-stuck"><Hourglass aria-hidden="true" />Stuck {ago(task.last, now)}</span>}
    {([...new Set(task.sweep.map(line => line.kind))] as SweepKind[]).map(kind => <span key={kind} className="factory-flag is-sweep"><GitBranch aria-hidden="true" />{SWEEP_NAMES[kind]}</span>)}
  </>
}

/** The findings of the worktree sweep, one line each. Read-only: Tether never rebases a branch. */
const SweepRows = ({ lines, names }: { lines: SweepLine[]; names?: boolean }) => <ul className="factory-list">{lines.map(line => <li key={line.pane + line.kind}>
  <b>{names ? `${line.name}: ` : ''}{SWEEP_NAMES[line.kind]}</b> {line.text} {line.branch && <span>{line.branch}</span>}
</li>)}</ul>

/** The floor's sweep strip: when the sweep last ran and every finding. Closed by default, so the floor stays calm. */
function SweepStrip({ sweep }: { sweep: Sweep }) {
  const count = sweep.lines.length
  if (!sweep.at) return null
  return <details className="factory-sweep">
    <summary><GitBranch aria-hidden="true" /><b>Worktree sweep</b> {count ? `${count} ${count === 1 ? 'finding' : 'findings'}` : 'no findings'} <span>checked {when(sweep.at * 1000)}, every {Math.round(sweep.every / 60)} min</span></summary>
    {count ? <SweepRows lines={sweep.lines} names /> : <p className="factory-none">No worktree tab is behind main, has failing checks or is idle for over 2 hours.</p>}
    <p className="factory-note">Tether only reports. It does not rebase or change a branch.</p>
  </details>
}

/** A mark per agent: filled = working, ring = idle, square = waits for you. Shape, not only colour. */
const Dot = ({ agent }: { agent: Agent }) => <span className={`factory-dot is-${agent.state}`} title={`${agent.name}: ${STATE_NAMES[agent.state]}`} />

function FloorLevel({ floor, parked, now, moved, zoomPhase, zoomTask }: { floor: Task[]; parked: number; now: number; moved: Set<string>; zoomPhase: (phase: Phase) => void; zoomTask: (task: Task) => void }) {
  return <ol className="factory-floor" aria-label="Phases, left to right">
    {PHASES.map((phase, index) => {
      const tasks = floor.filter(task => task.phase === phase).sort(attentionOrder)
      const agents = tasks.reduce((sum, task) => sum + task.agents.length, 0)
      const waits = tasks.filter(task => task.waits).length
      const stuck = tasks.filter(task => task.stuck).length
      return <li key={phase} className={`factory-station phase-${phase}`}>
        <button type="button" className="factory-station-head" data-zoom data-key={`phase:${phase}`} data-first={index === 0 ? '' : undefined} onClick={() => zoomPhase(phase)}
          aria-label={`${PHASE_NAMES[phase]}: ${tasks.length} tasks, ${agents} agents${waits ? `, ${waits} wait for you` : ''}${stuck ? `, ${stuck} stuck` : ''}. Zoom in.`}>
          <span className="factory-station-name">{PHASE_NAMES[phase]}</span>
          <span className="factory-station-count">{tasks.length}</span>
          <span className="factory-station-meta">{agents} {agents === 1 ? 'agent' : 'agents'}{phase === 'done' ? ' · last 24 h' : ''}</span>
        </button>
        <ul className="factory-tokens">
          {tasks.slice(0, FLOOR_TOKENS).map(task => <li key={task.id}>
            <button type="button" className={`factory-token${task.waits ? ' is-waits' : ''}${task.stuck ? ' is-stuck' : ''}${moved.has(task.id) ? ' is-moved' : ''}`} data-zoom data-token={task.id} data-phase={task.phase} data-key={`task:${task.id}`} onClick={() => zoomTask(task)}>
              <span className="factory-token-title">{task.ref && <b>{task.ref}</b>} {task.title}</span>
              {(task.agents.length > 0 || task.waits || task.stuck) && <span className="factory-token-foot">
                {task.agents.map(agent => <Dot key={agent.pane.id} agent={agent} />)}
                <Flags task={task} now={now} />
              </span>}
            </button>
          </li>)}
          {tasks.length > FLOOR_TOKENS && <li><button type="button" className="factory-more" onClick={() => zoomPhase(phase)}>{tasks.length - FLOOR_TOKENS} more in {PHASE_NAMES[phase]}</button></li>}
          {!tasks.length && <li className="factory-none">Nothing here</li>}
        </ul>
        {phase === 'done' && parked > 0 && <p className="factory-parked">{parked} idle tabs from before today are not shown.</p>}
      </li>
    })}
  </ol>
}

function PhaseLevel({ phase, tasks, history, now, zoom }: { phase: Phase; tasks: Task[]; history: History; now: number; zoom: (task: Task) => void }) {
  const groups = groupsOf(tasks)
  if (!tasks.length) return <Problem title={`Nothing in ${PHASE_NAMES[phase]}`} text="Tasks show here when their tabs reach this phase." />
  const firstId = groups[0]?.tasks[0]?.id
  return <div className="factory-phase">
    {groups.map(group => <section key={group.name} className="factory-group" aria-label={`${group.name}, ${group.tasks.length} tasks`}>
      <h2>{group.name} <span>{group.tasks.length}</span></h2>
      <ul>{group.tasks.map(task => {
        const since = phaseSince(history, task)
        const mark = task.id === firstId
        return <li key={task.id}><button type="button" className={`factory-card${task.waits ? ' is-waits' : ''}${task.stuck ? ' is-stuck' : ''}`} data-zoom data-key={`task:${task.id}`} data-first={mark ? '' : undefined} onClick={() => zoom(task)}>
          <span className="factory-card-title">{task.ref && <b>{task.ref}</b>} {task.title}</span>
          <span className="factory-card-meta">{[since ? `${formatElapsed((now - since) / 1000)} in ${PHASE_NAMES[phase]}` : '', lastMove(task.agents.some(one => one.state === 'working'), task.last, now)].filter(Boolean).join(' · ')}</span>
          {task.agents.length > 0 && <span className="factory-card-agents">{task.agents.map(agent => <span key={agent.pane.id}><Dot agent={agent} />{agent.name} · {agent.role}</span>)}</span>}
          <span className="factory-token-foot"><Flags task={task} now={now} /></span>
        </button></li>
      })}</ul>
    </section>)}
  </div>
}

function TaskLevel({ task, history, now, zoom }: { task: Task; history: History[string]; now: number; zoom: (agent: Agent) => void }) {
  const proof = task.agents.flatMap(agent => agent.tools.filter(tool => /(test|unittest|tsc -b|eslint|screenshot|SHOTS=|playwright)/i.test(`${tool.name} ${tool.title}`)).map(tool => ({ agent: agent.name, tool })))
  return <div className="factory-task">
    <div className="factory-task-head">
      <p className="factory-kicker">{PHASE_NAMES[task.phase]} · {task.group}</p>
      <h2>{task.ref && <b>{task.ref}</b>} {task.title}</h2>
      <p className="factory-flags"><Flags task={task} now={now} /></p>
      <p className="factory-why"><b>Phase guessed from:</b> {task.why}</p>
    </div>
    <section aria-labelledby="factory-agents"><h3 id="factory-agents">Agents</h3>
      {task.agents.length ? <ul className="factory-agents">{task.agents.map((agent, index) => <li key={agent.pane.id}>
        <button type="button" className="factory-card" data-zoom data-key={`agent:${agent.pane.id}`} data-first={index === 0 ? '' : undefined} onClick={() => zoom(agent)}>
          <span className="factory-card-title"><Dot agent={agent} />{agent.name}</span>
          <span className="factory-card-meta">{agent.role} · {agent.pane.agent} · {STATE_NAMES[agent.state]}{lastMove(agent.state === 'working', agent.last, now) ? ` · ${lastMove(agent.state === 'working', agent.last, now)}` : ''}</span>
          <span className="factory-step">{agent.step}</span>
        </button>
      </li>)}</ul> : <p className="factory-none">No tab works on this item.</p>}
    </section>
    {task.sweep.length > 0 && <section aria-labelledby="factory-sweep"><h3 id="factory-sweep">Worktree sweep</h3>
      <SweepRows lines={task.sweep} names={task.agents.length > 1} />
      <p className="factory-note">Tether only reports. It does not rebase or change a branch.</p>
    </section>}
    <section aria-labelledby="factory-history"><h3 id="factory-history">Phase history</h3>
      {history.length ? <ol className="factory-timeline">{history.map((entry, index) => <li key={index}><b>{PHASE_NAMES[entry.phase]}</b> from {when(entry.at)}</li>)}</ol>
        : <p className="factory-none">No phases recorded yet.</p>}
      <p className="factory-note">Tether shares this history across screens. It starts when any screen first sees the task.</p>
    </section>
    <section aria-labelledby="factory-commits"><h3 id="factory-commits">Commits</h3>
      {task.commits.length ? <ul className="factory-list">{task.commits.map(commit => <li key={commit.hash + commit.repo}><code>{commit.hash}</code> {commit.subject} <span>{commit.repo} · {clock(commit.at * 1000)}</span></li>)}</ul>
        : <p className="factory-none">{task.ref ? `No commit of the last 48 h names ${task.ref}.` : 'Tabs with no backlog item have no commits linked.'}</p>}
    </section>
    <section aria-labelledby="factory-proof"><h3 id="factory-proof">Proof</h3>
      <ul className="factory-list">
        {task.deploys.map(line => <li key={line.at + line.text}><b>DEPLOYED.md</b> {line.timed ? clock(line.at * 1000) : fmtDay(line.day)}: {line.text}</li>)}
        {proof.map(({ agent, tool }, index) => <li key={index}><b>{agent}</b> {tool.error ? 'failed: ' : ''}{tool.name}: {tool.title} {tool.at ? <span>{clock(toMs(tool.at))}</span> : null}</li>)}
        {task.item?.status?.kind === 'done' && <li><b>Backlog</b> Done: {task.item.status.text}</li>}
        {task.item?.details.filter(detail => /screenshot|shots|test|proof|verified/i.test(detail.text)).map(detail => <li key={detail.line}><b>Backlog note</b> {detail.text}</li>)}
      </ul>
      {!task.deploys.length && !proof.length && task.item?.status?.kind !== 'done' && <p className="factory-none">No tests, screenshots or deploy lines seen yet.</p>}
    </section>
  </div>
}

function AgentLevel({ api, agent, task, now, onOpen }: { api: Api; agent: Agent; task: Task; now: number; onOpen: (pane: Pane) => void }) {
  const id = encodeURIComponent(agent.pane.id)
  const footer = usePoll<Footer>(api, agent.pane.agent === 'shell' ? null : `footer?session=${id}`, SCREEN_MS * 3)
  const screen = usePoll<{ text: string }>(api, `screen?session=${id}&lines=60`, SCREEN_MS)
  const context = footer.data?.context?.percent
  return <div className="factory-agent">
    <div className="factory-task-head">
      <p className="factory-kicker">{agent.role} on {task.ref ?? task.title} · {agent.pane.agent}{footer.data?.model ? ` · ${footer.data.model}` : ''}</p>
      <h2><Dot agent={agent} />{agent.name}</h2>
      <p className="factory-step is-big">{agent.step}</p>
      <dl className="factory-facts">
        <div><dt>State</dt><dd>{STATE_NAMES[agent.state]}</dd></div>
        <div><dt>Context</dt><dd>{context != null ? `${Math.round(context)}% used` : footer.error ? 'not read' : '…'}</dd></div>
        <div><dt>Last move</dt><dd>{agent.state === 'working' ? 'now' : agent.last ? `${ago(agent.last, now)} ago` : 'unknown'}</dd></div>
        <div><dt>Folder</dt><dd>{agent.pane.dir || '~'}</dd></div>
      </dl>
      <button type="button" className="factory-open" data-first="" onClick={() => onOpen(agent.pane)}><ExternalLink aria-hidden="true" />Open tab</button>
    </div>
    <div className="factory-agent-grid">
      <section aria-labelledby="factory-tools"><h3 id="factory-tools">Recent steps</h3>
        {agent.tools.length ? <ol className="factory-list" reversed>{[...agent.tools].reverse().map((tool, index) => <li key={index} className={tool.error ? 'is-error' : ''}>
          <b>{tool.name}</b> {tool.title} {tool.at ? <span>{clock(toMs(tool.at))}</span> : null}{tool.error ? <em> failed</em> : null}
        </li>)}</ol> : <p className="factory-none">{agent.pane.agent === 'antigravity' ? 'Tether reads no steps from Antigravity transcripts.' : 'No steps read yet.'}</p>}
      </section>
      <section aria-labelledby="factory-screen" className="factory-screen-box"><h3 id="factory-screen">Live screen <span>read-only, every {SCREEN_MS / 1000} s</span></h3>
        {screen.data && screen.error && <p className="factory-stale" role="status">The screen does not refresh: {screen.error}. Shown from {when(screen.at)}.</p>}
        {screen.data ? <pre className="factory-screen" tabIndex={0} aria-label={`Screen of ${agent.name}`}>{screen.data.text}</pre>
          : <p className="factory-none">{screen.error ? `The screen does not load: ${screen.error}` : 'Loading the screen…'}</p>}
      </section>
    </div>
  </div>
}
