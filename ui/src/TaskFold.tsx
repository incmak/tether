import { useId, useState } from 'react'
import type { ReactNode } from 'react'
import { Check, ChevronRight } from 'lucide-react'
import type { Pane } from './api'
import { paneTitle } from './Sidebar'
import { helperLabel, TaskDot } from './TaskChips'
import { needsMak } from './helperTabs'
import { taskIdentity, taskState } from './taskTree'

export function TaskBranch({ task, children, selected, search, row }: {
  task: Pane; children: Pane[]; selected?: string; search: boolean
  row: (pane: Pane, children?: Pane[], controls?: ReactNode, describedBy?: string) => ReactNode
}) {
  const id = useId()
  const triggers = JSON.stringify(children.filter(child => needsMak(child) || child.id === selected).map(taskIdentity))
  const [fold, setFold] = useState(() => ({ triggers, open: triggers !== '[]' }))
  if (fold.triggers !== triggers) {
    const previous = new Set(JSON.parse(fold.triggers) as string[])
    setFold({ triggers, open: fold.open || (JSON.parse(triggers) as string[]).some(key => !previous.has(key)) })
  }
  const [focused, setFocused] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  if (!children.length) return row(task)
  const open = search || fold.open
  const label = `${open ? 'Fold' : 'Show'} helpers of ${paneTitle(task)}`
  const tooltip = `${id}-helpers`
  const description = open ? undefined : tooltip
  const visible = !open && !dismissed && (focused || hovered)
  return <li className="task-branch">
    <ul className="task-list" onFocus={event => {
      const keyboard = event.target.matches('.task-row-main:focus-visible, .task-fold-toggle:focus-visible')
      setFocused(keyboard)
      if (keyboard) setDismissed(false)
    }} onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false)
    }} onKeyDown={event => {
      if (event.key === 'Escape' && visible) {
        event.preventDefault()
        event.stopPropagation()
        setDismissed(true)
        setHovered(false)
      }
    }}>{row(task, children, <>
      {!open && <span className="task-helper-dots" role="group" aria-label="Helpers">{children.map(child =>
        <span key={child.id} role="img" aria-label={helperLabel(child)} title={helperLabel(child)}><TaskDot state={taskState(child)} /></span>
      )}</span>}
      <button type="button" className="task-row-control task-fold-toggle" aria-label={label} title={label} aria-expanded={open}
        aria-controls={id} aria-describedby={description} onClick={() => setFold({ triggers, open: !open })} disabled={search}>
        <ChevronRight size={14} className="chevron" aria-hidden="true" />
      </button>
      {!open && <div id={tooltip} role="tooltip" className="task-helper-tooltip" hidden={!visible}
        onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}>
        <ul>{children.map(child => <li key={child.id}>{helperLabel(child)}</li>)}</ul>
      </div>}
    </>, description)}</ul>
    <div id={id} className={`task-fold${open ? ' open' : ''}`} inert={!open}>
      <div><ul className="task-list task-kids">{children.map(child => row(child))}</ul></div>
    </div>
  </li>
}

export function DoneTasks({ count, children, openForSelection }: { count: number; children: ReactNode; openForSelection: boolean }) {
  const id = useId()
  const [open, setOpen] = useState(openForSelection)
  return <li className="task-done">
    <button type="button" className="task-done-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
      <ChevronRight size={14} className="chevron" aria-hidden="true" /><Check size={14} className="task-check" aria-hidden="true" />
      Done <span className="group-count">{count}</span>
    </button>
    <div id={id} className={`task-fold${open ? ' open' : ''}`} inert={!open}><div><ul className="task-list">{children}</ul></div></div>
  </li>
}
