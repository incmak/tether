// The search menu's "Add to the backlog" and "Start a new tab" rows run here, and one toast reports each result.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { MapPin, X } from 'lucide-react'
import type { Api, Pane } from './api'
import type { Create } from './CommandMenu'
import type { Session } from './Sidebar'
import { SelectMenu } from './SelectMenu'
import { addToBacklog } from './quickBacklog'
import { DEFAULT_SESSION, DEFAULT_STACK, ROUTE_WAIT_MS, moveTab, placeName, placeValue, places, requestId, route, samePlace, startTab } from './quickTab'
import type { Choice, Place, Routed, Started } from './quickTab'
import './QuickToast.css'

/** busy: work runs, no timer. done: closes after DONE_MS unless the pointer or focus is on it. error: stays. */
interface Toast {
  id: number
  kind: 'busy' | 'done' | 'error'
  text: string
  note?: string
  undo?: () => Promise<void>
  retry?: () => void
  tab?: Started
  /** A place Tether chose after the tab started somewhere else. */
  suggest?: Place
}

export const DONE_MS = 10000
const message = (failure: unknown) => failure instanceof Error && failure.message ? failure.message : 'Something went wrong.'
const wait = (ms: number) => new Promise<'late'>(resolve => setTimeout(() => resolve('late'), ms))

interface Options {
  api: Api
  /** The main AI model from Settings: auto, claude, codex or none. */
  agent: string
  sessions: Session[]
  onOpen: (pane: Pane) => void
}

/** create goes to CommandMenu; toast goes anywhere in the page. */
export function useQuickCreate({ api, agent, sessions, onOpen }: Options): { create: Create; toast: ReactNode } {
  const [toast, setToast] = useState<Toast | null>(null)
  const [held, setHeld] = useState(false)
  const [running, setRunning] = useState(0)
  const next = useRef(0)
  const live = useRef(true)
  useEffect(() => () => { live.current = false }, [])
  // Each action owns the toast until a newer action starts. Late answers for an old action change nothing.
  const show = useCallback((id: number, value: Omit<Toast, 'id'> | ((old: Toast) => Toast)) => {
    if (!live.current) return
    setToast(old => {
      if (typeof value === 'function') return old?.id === id ? value(old) : old
      // Only the newest action may set a new toast: a late result must not bring back a dismissed or newer toast.
      return id === next.current ? { id, ...value } : old
    })
  }, [])

  useEffect(() => {
    if (!toast || toast.kind !== 'done' || held) return
    const timer = setTimeout(() => setToast(old => old?.id === toast.id ? null : old), DONE_MS)
    return () => clearTimeout(timer)
  }, [toast, held])

  const create = useMemo<Create>(() => {
    const backlog = (query: string) => {
      const id = ++next.current
      show(id, { kind: 'busy', text: `Adding “${query}” to the backlog…` })
      addToBacklog(api, query).then(({ ref, undo }) => {
        show(id, {
          kind: 'done', text: ref ? `Added ${ref} to the backlog.` : 'Added to the backlog.',
          undo: undo && (async () => {
            show(id, old => ({ ...old, kind: 'busy', text: `Removing ${ref}…`, undo: undefined }))
            try {
              await undo()
              show(id, old => ({ ...old, kind: 'done', text: `Removed ${ref} from the backlog.` }))
            } catch (failure) {
              show(id, old => ({ ...old, kind: 'error', text: `${ref} was not removed. ${message(failure)}` }))
            }
          }),
        })
      }, failure => show(id, { kind: 'error', text: `“${query}” was not added. ${message(failure)}`, retry: () => backlog(query) }))
    }

    const startOne = async (query: string, choice: Choice) => {
      const id = ++next.current
      const request = requestId()
      let late: Promise<Routed | null> | null = null
      let note: string | undefined
      if (choice.kind === 'auto') {
        show(id, { kind: 'busy', text: `Choosing a place for “${query}”…` })
        const routed = route(api, query, agent).catch(() => null)
        const first = await Promise.race([routed, wait(ROUTE_WAIT_MS)])
        if (first === 'late') {
          late = routed
          note = 'Tether took too long to choose, so the tab started in the default place.'
          choice = { kind: 'default' }
        } else {
          choice = first?.place ? { kind: 'place', place: first.place } : { kind: 'default' }
          if (!first?.place) note = first?.why ?? 'Tether could not choose a place.'
        }
      }
      const target = choice.kind === 'place' ? placeName(choice.place) : `${DEFAULT_SESSION} · ${DEFAULT_STACK}`
      const run = async () => {
        show(id, { kind: 'busy', text: `Starting a tab in ${target}…` })
        try {
          const started = await startTab(api, query, choice, request)
          show(id, { kind: 'done', text: `Started “${started.name}” in ${placeName(started.place)}.`, note, tab: started })
          const answer = await late
          if (answer?.place && !samePlace(answer.place, started.place)) show(id, old => ({ ...old, suggest: answer.place! }))
        } catch (failure) {
          // The same request id: a retry after a lost answer starts no second tab.
          show(id, { kind: 'error', text: `The tab did not start. ${message(failure)}`, retry: () => void run() })
        }
      }
      await run()
    }
    // running counts tab actions until their last show. It is state, so its commit includes every earlier toast update.
    const tab = async (query: string, choice: Choice) => {
      setRunning(count => count + 1)
      try { await startOne(query, choice) } finally { if (live.current) setRunning(count => count - 1) }
    }

    return { onBacklog: backlog, onTab: (query, choice) => void tab(query, choice) }
  }, [api, agent, show])

  const move = async (started: Started, place: Place) => {
    const id = toast!.id
    show(id, old => ({ ...old, kind: 'busy', text: `Moving “${started.name}” to ${placeName(place)}…`, suggest: undefined }))
    try {
      const moved = await moveTab(api, started.pane, place)
      show(id, old => ({ ...old, kind: 'done', text: `Moved “${started.name}” to ${placeName(moved.place)}.`, note: 'The agent keeps its folder.', tab: { ...started, place: moved.place } }))
    } catch (failure) {
      show(id, old => ({ ...old, kind: 'error', text: `The tab was not moved. ${message(failure)}` }))
    }
  }

  const pane = toast?.tab && sessions.flatMap(session => session.panes).find(item => item.id === toast.tab!.pane)
  const targets = toast?.tab ? places(sessions).filter(place => !samePlace(place, toast.tab!.place)) : []
  const byValue = new Map(targets.map(place => [placeValue(place), place]))
  const close = () => { setToast(null); setHeld(false) }

  const element = toast && <div className={`toast app-toast quick-toast ${toast.kind}`}
    onPointerEnter={() => setHeld(true)} onPointerLeave={() => setHeld(false)}
    onFocus={() => setHeld(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setHeld(false) }}>
    <div className="quick-toast-text" role={toast.kind === 'error' ? 'alert' : 'status'}>
      {toast.kind === 'busy' && <span className="state-dot working" aria-hidden="true" />}
      <span>{toast.text}{toast.note && <small>{toast.note}</small>}
        {toast.suggest && <small>Tether suggests {placeName(toast.suggest)}.</small>}</span>
    </div>
    {toast.kind !== 'busy' && <div className="quick-toast-actions">
      {toast.undo && <button type="button" onClick={() => void toast.undo!()}>Undo</button>}
      {toast.retry && <button type="button" onClick={toast.retry}>Try again</button>}
      {toast.tab && toast.kind === 'done' && pane && <button type="button" onClick={() => { onOpen(pane); close() }}>Open</button>}
      {toast.tab && toast.kind === 'done' && toast.suggest && <button type="button" onClick={() => void move(toast.tab!, toast.suggest!)}>Move to {placeName(toast.suggest)}</button>}
      {toast.tab && toast.kind === 'done' && targets.length > 0 && <SelectMenu value="" options={targets.map(place => ({ value: placeValue(place), label: place.groupName || 'Tabs in no stack', detail: place.groupName ? undefined : place.sessionName, group: place.sessionName, search: placeName(place) }))}
        onChange={value => { const place = byValue.get(value); if (place) void move(toast.tab!, place) }} label="Move the tab to" className="sm-field quick-toast-move" minWidth={260}
        searchLabel="Search sessions and stacks" emptyText="No session or stack matches." triggerLabel="Move…" icon={<MapPin size={13} aria-hidden="true" />} />}
      <button type="button" className="quick-toast-close" aria-label="Dismiss" onClick={close}><X size={16} aria-hidden="true" /></button>
    </div>}
  </div>
  // Tests wait for data-running="0" before they check that a late result changed nothing.
  return { create, toast: <>{element}<span className="quick-toast-running" hidden data-running={running} /></> }
}
