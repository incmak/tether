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
}

export interface ClosePlan {
  plan: string
  task: { pane: string; name: string }
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
  const close_also = plan.working.filter(child => checked.includes(child.pane)).map(child => child.pane)
  return api<{ closed: string[]; kept: string[] }>('task-tree/close', {
    pane: pane.id, thread: pane.thread, plan: plan.plan, close_idle: plan.idle.map(child => child.pane), close_also,
  })
}

export function TaskCloseChoices({ plan, pending, checked, setChecked }: {
  plan: ClosePlan; pending: boolean; checked: string[]; setChecked: (ids: string[]) => void
}) {
  const idleLabel = useId()
  return <>
    {plan.working.length > 0 && <p>Check a helper to close it too.</p>}
    {plan.idle.length > 0 && <div>
      <p id={idleLabel}><strong>Close with the task</strong></p>
      <ul className="task-close-list task-close-idle" role="list" aria-labelledby={idleLabel}>
        {plan.idle.map(child => <li key={child.pane}>{child.name} <small>· {child.role}, idle</small></li>)}
      </ul>
    </div>}
    {plan.working.length > 0 && <fieldset className="task-close-working">
      <legend>Still working (stay open as separate tasks)</legend>
      <ul className="task-close-list" role="list">{plan.working.map(child => <li key={child.pane}><label>
        <input type="checkbox" disabled={pending} checked={checked.includes(child.pane)}
          onChange={event => setChecked(event.target.checked ? [...checked, child.pane] : checked.filter(id => id !== child.pane))} />
        <span>{child.name} <small>· {child.role}, {child.state === 'needs' ? 'needs you' : 'working'}</small></span>
      </label></li>)}</ul>
    </fieldset>}
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
      {plan.working.length === 0 && <p>{plan.idle.length ? 'The task and its idle helpers will close.' : 'This task will close.'}</p>}
      <TaskCloseChoices plan={plan} pending={pending} checked={checked} setChecked={setChecked} />
    </>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" ref={cancel} autoFocus disabled={pending} onClick={onCancel}>Cancel</button>
      <button type="button" className="danger" disabled={pending || !plan} onClick={() => {
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
