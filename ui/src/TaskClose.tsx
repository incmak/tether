import { useEffect, useId, useRef, useState } from 'react'
import { ApiError, errorText } from './api'
import type { Api, Pane } from './api'
import { paneTitle } from './Sidebar'

interface CloseChild {
  pane: string
  thread: string | null
  name: string
  role: string
  state: 'working' | 'needs' | 'idle'
  undo: boolean
}

export interface ClosePlan {
  plan: string
  task: { pane: string; name: string; undo: boolean }
  idle: CloseChild[]
  working: CloseChild[]
}

export function closePlanChanged(error: unknown) {
  return error instanceof ApiError && error.status === 409 && error.message === 'The helpers changed. Review the list again.'
}

export function loadClosePlan(api: Api, pane: string, signal: AbortSignal) {
  return api<ClosePlan>(`task-tree/close-plan?pane=${encodeURIComponent(pane)}`, undefined, signal)
}

export function closeTask(api: Api, pane: Pick<Pane, 'id' | 'thread'>, plan: ClosePlan, checked: string[]) {
  const close_idle = plan.idle.filter(child => child.undo).map(child => child.pane)
  const close_also = [...plan.working, ...plan.idle.filter(child => !child.undo)]
    .filter(child => checked.includes(child.pane)).map(child => child.pane)
  return api<{ closed: string[]; kept: string[]; closed_without_undo?: string[] }>('task-tree/close', {
    pane: pane.id, thread: pane.thread, plan: plan.plan, close_idle, close_also,
  })
}

export function TaskCloseChoices({ plan, pending, checked, setChecked, warningId }: {
  plan: ClosePlan; pending: boolean; checked: string[]; setChecked: (ids: string[]) => void; warningId?: string
}) {
  const idleLabel = useId()
  const idle = plan.idle.filter(child => child.undo)
  const working = plan.working.filter(child => child.undo)
  const withoutUndo = [...plan.idle, ...plan.working].filter(child => !child.undo)
  const choose = (label: string, children: CloseChild[]) => children.length > 0 && <fieldset className="task-close-choices">
    <legend>{label}</legend>
    <ul className="task-close-list" role="list">{children.map(child => <li key={child.pane}><label>
      <input type="checkbox" disabled={pending} checked={checked.includes(child.pane)}
        onChange={event => setChecked(event.target.checked ? [...checked, child.pane] : checked.filter(id => id !== child.pane))} />
      <span>{child.name} <small>· {child.role}, {child.state === 'needs' ? 'needs you' : child.state}</small></span>
    </label></li>)}</ul>
  </fieldset>
  return <>
    {(working.length > 0 || withoutUndo.length > 0) && <p>Check a helper to close it too.</p>}
    {idle.length > 0 && <div>
      <p id={idleLabel}><strong>Close with the task</strong></p>
      <ul className="task-close-list task-close-idle" role="list" aria-labelledby={idleLabel}>
        {idle.map(child => <li key={child.pane}>{child.name} <small>· {child.role}, idle</small></li>)}
      </ul>
    </div>}
    {choose('Still working (stay open as separate tasks)', working)}
    {choose('Cannot be undone', withoutUndo)}
    {!plan.task.undo && <p id={warningId}><strong>You cannot undo closing {plan.task.name}.</strong></p>}
  </>
}

export function TaskClose({ api, pane, pending, run, onCancel }: {
  api: Api; pane: Pane; pending: boolean
  run: (action: () => Promise<undefined>) => Promise<void>; onCancel: () => void
}) {
  const [plan, setPlan] = useState<ClosePlan>()
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [checked, setChecked] = useState<string[]>([])
  const [changed, setChanged] = useState('')
  const cancel = useRef<HTMLButtonElement>(null)
  const warningId = useId()
  useEffect(() => { if (changed && !pending) cancel.current?.focus() }, [changed, pending])
  useEffect(() => {
    const controller = new AbortController()
    loadClosePlan(api, pane.id, controller.signal).then(
      result => { if (!controller.signal.aborted) setPlan(result) },
      failure => { if (!controller.signal.aborted) setError(errorText(failure)) },
    )
    return () => controller.abort()
  }, [api, pane.id, attempt])
  return <div className="confirm-box task-close-confirm">
    <div className="sheet-heading"><h2 id="task-close-title">Close {paneTitle(pane)}?</h2></div>
    {!plan && !error && <p role="status">Loading helpers…</p>}
    {error && <><p className="error" role="alert">{error}</p><button type="button" className="secondary" onClick={() => { setError(''); setAttempt(attempt + 1) }}>Try again</button></>}
    {plan && <>
      {!plan.working.length && !plan.idle.length && plan.task.undo && <p>This task will close.</p>}
      <TaskCloseChoices plan={plan} pending={pending} checked={checked} setChecked={setChecked} warningId={warningId} />
    </>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" ref={cancel} autoFocus disabled={pending} onClick={onCancel}>Cancel</button>
      <button type="button" className="danger" disabled={pending || !plan} aria-describedby={plan && !plan.task.undo ? warningId : undefined} onClick={() => {
        if (!plan) return
        setChanged('')
        void run(async () => {
          try {
            await closeTask(api, pane, plan, checked)
          } catch (failure) {
            if (closePlanChanged(failure)) {
              setChanged(errorText(failure))
              setPlan(undefined)
              setChecked([])
              setAttempt(attempt + 1)
            }
            throw failure
          }
          return undefined
        })
      }}>{pending ? 'Closing…' : 'Close'}</button>
    </div>
  </div>
}
