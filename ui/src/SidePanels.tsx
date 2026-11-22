import { Loading } from './Skeleton'
import { useCallback, useState } from 'react'
import { Gauge, Terminal } from 'lucide-react'
import { TextMessagePartProvider } from '@assistant-ui/react'
import { errorText } from './api'
import type { ChatData, LiveState } from './api'
import { usePolling } from './hooks'
import { Markdown } from './Messages'
import type { PanelBodyProps } from './panels'
import { agentNames, paneTitle } from './Sidebar'
import { fmtTime, useTetherZone } from './zone'

/** The focused pane's background shells and monitors (Claude only), with Stop for a running shell. */
export function TasksPanel({ api, pane }: PanelBodyProps) {
  useTetherZone()
  const claude = pane.agent === 'claude'
  const fetcher = useCallback((signal: AbortSignal) => claude ? api<LiveState>(`live?session=${encodeURIComponent(pane.id)}`, undefined, signal) : Promise.resolve({ goal: null, tasks: [] }), [api, pane.id, claude])
  const { data, error, refresh } = usePolling(fetcher, 5000)
  const [stopping, setStopping] = useState<string | null>(null)
  const [stopError, setStopError] = useState('')
  const stop = async (task: string) => {
    if (!window.confirm('Stop this shell?')) return
    setStopping(task); setStopError('')
    try { await api('stop-task', { session: pane.id, task }); await refresh() }
    catch (failure) { setStopError(errorText(failure)) }
    finally { setStopping(null) }
  }
  const tasks = data ? [...data.tasks.filter(task => task.state === 'running').reverse(), ...data.tasks.filter(task => task.state !== 'running').reverse().slice(0, 5)] : []
  return <div className="panel-scroll">
    <p className="panel-for">For {paneTitle(pane)}</p>
    {!claude ? <p className="live-empty">Background tasks show for Claude chats. This pane runs {agentNames[pane.agent]}.</p>
      : error ? <p className="live-empty" role="alert">Could not load: {error}</p>
      : !data ? <Loading className="live-empty" />
      : !tasks.length ? <p className="live-empty">No background tasks in this conversation.</p>
      : <ul className="live-tasks">{tasks.map(task => <li key={task.id}>
          <p className="live-task-title">{task.kind === 'monitor' ? <Gauge size={15} /> : <Terminal size={15} />}<strong>{task.title || task.command}</strong></p>
          <p className="live-meta">{task.kind === 'monitor' ? 'Monitor' : 'Shell'} · {task.state === 'running' ? 'Running' : 'Ended'}{task.started ? ` · started ${fmtTime(task.started * 1000)}` : ''}</p>
          {task.title && <code className="live-command">{task.command}</code>}
          {task.output ? <pre className="live-output">{task.output}</pre> : <p className="live-meta">No output yet.</p>}
          {task.kind === 'shell' && task.state === 'running' && <div className="live-actions"><button type="button" disabled={stopping !== null} onClick={() => void stop(task.id)}>{stopping === task.id ? 'Stopping…' : 'Stop'}</button></div>}
        </li>)}</ul>}
    {stopError && <p className="live-empty" role="alert">{stopError}</p>}
  </div>
}

/** The plan the focused pane's agent asks you to review, read from its chat. */
export function PlanPanel({ api, pane }: PanelBodyProps) {
  const fetcher = useCallback((signal: AbortSignal) => api<ChatData>(`chat?session=${encodeURIComponent(pane.id)}&limit=1`, undefined, signal), [api, pane.id])
  const { data, error } = usePolling(fetcher, 5000)
  const plan = data?.plan
  return <div className="panel-scroll">
    <p className="panel-for">For {paneTitle(pane)}</p>
    {plan ? <div className="plan-text assistant-message" tabIndex={0} aria-label="Plan"><TextMessagePartProvider text={plan.text}><Markdown /></TextMessagePartProvider></div>
      : null}
    {plan ? <p className="live-meta">Approve it or ask for changes in the chat.</p>
      : error ? <p className="live-empty" role="alert">Could not load: {error}</p>
      : !data ? <Loading className="live-empty" />
      : <p className="live-empty">No plan to review in this chat right now.</p>}
  </div>
}
