import { useLayoutEffect, useRef, useState } from 'react'
import { moveTabFocus, openFromTab, restoreTabFocus } from './tabsKeyboard'
import { Check, Eye, Lightbulb, MessageSquare, Wrench } from 'lucide-react'
import type { Pane } from './api'
import type { Anchor } from './hooks'
import { anchorOf } from './hooks'
import { paneTitle } from './Sidebar'
import { revealLeft } from './groups'
import { fromLabel, needsMak } from './helperTabs'
import { isTaskAdvisor, taskIdentity, taskRole, taskState, taskStateLabel } from './taskTree'
import './HelperTabs.css'

export function TaskDot({ state, label }: { state: ReturnType<typeof taskState>; label?: string }) {
  return state === 'done' ? <Check className="task-check" size={13} aria-hidden="true" />
    : <span className={`task-dot ${state}`} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} />
}

export function TaskFrom({ pane }: { pane: Pane }) {
  return pane.from && <span className="task-from" title={`Started by ${pane.from.name}`}>from {fromLabel(pane.from.name)}</span>
}

export function helperLabel(pane: Pane) {
  const state = taskState(pane)
  const status = state === 'working' && isTaskAdvisor(pane) ? 'Advising' : taskStateLabel[state]
  return `${paneTitle(pane)}, ${taskRole(pane)}, ${status}`
}

export function TaskChips({ task, children, pane, panelId, onOpen, onActions }: {
  task: Pane; children: Pane[]; pane: Pane; panelId?: string; onOpen: (pane: Pane) => void; onActions: (id: string, at?: Anchor) => void
}) {
  const row = useRef<HTMLDivElement>(null)
  const [focus, setFocus] = useState(pane.id)
  const needsKey = JSON.stringify(children.filter(needsMak)
    .sort((a, b) => (b.ask?.since ?? b.since ?? 0) - (a.ask?.since ?? a.since ?? 0))
    .map(child => [taskIdentity(child), child.id]))
  const previous = useRef<Set<string> | null>(null)
  useLayoutEffect(() => {
    const strip = row.current
    if (!strip) return
    restoreTabFocus(strip, pane.id)
    const needs = JSON.parse(needsKey) as [string, string][]
    const was = previous.current
    const phone = matchMedia('(max-width: 600px)').matches
    const newNeed = phone ? needs.find(([identity]) => !was?.has(identity)) : undefined
    const targetId = (!was && pane.id !== task.id ? pane.id : newNeed?.[1]) ?? pane.id
    previous.current = new Set(needs.map(([identity]) => identity))
    const target = [...strip.querySelectorAll<HTMLButtonElement>('button')].find(button => button.dataset.chip === targetId)
    if (!target) return
    const left = target.getBoundingClientRect().left - strip.getBoundingClientRect().left + strip.scrollLeft
    strip.scrollTo({ left: revealLeft(strip.scrollLeft, strip.clientWidth, strip.scrollWidth, left, target.offsetWidth),
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  }, [needsKey, pane.id, task.id])
  const items = [task, ...children]
  return <div className="task-chips" role="tablist" aria-label={`Tabs in ${paneTitle(task)}`} ref={row}>
    {items.map(item => {
      const main = item.id === task.id
      const role = taskRole(item)
      const advisor = !main && isTaskAdvisor(item)
      const Icon = main ? MessageSquare : advisor ? Lightbulb : /review/i.test(role) ? Eye : Wrench
      const state = taskState(item)
      const advising = advisor && state === 'working'
      const dotLabel = state === 'needs' ? 'Needs you' : state === 'working' ? advising ? 'Advising' : 'Working' : undefined
      const label = main ? `${paneTitle(task)}, Main, ${taskStateLabel[state]}` : helperLabel(item)
      return <button key={item.id} type="button" role="tab" aria-controls={panelId} data-chip={item.id} aria-label={label} title={label} className={advising ? 'advising' : undefined}
        aria-selected={item.id === pane.id} tabIndex={item.id === (items.some(tab => tab.id === focus) ? focus : pane.id) ? 0 : -1}
        onFocus={() => setFocus(item.id)} onClick={event => openFromTab(event, item, onOpen)}
        onContextMenu={event => { event.preventDefault(); onActions(item.id, anchorOf(event)) }}
        onKeyDown={event => { moveTabFocus(event); if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) { event.preventDefault(); onActions(item.id, anchorOf(event)) } }}>
        <Icon size={14} className={advisor ? 'tab-advisor' : undefined} aria-hidden="true" /><span>{main ? 'Main' : paneTitle(item)}</span>
        {/* Concept D: the task tab already shows Main's working state; Main keeps only the needs-you dot. */}
        {dotLabel && !(main && state === 'working') && <TaskDot state={state} label={dotLabel} />}
      </button>
    })}
  </div>
}
