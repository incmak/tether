import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowLeft, Check, ChevronRight, Square, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, ToolSummary } from './api'
import { useModal, usePolling } from './hooks'
import { formatElapsed } from './Notices'
import { Bone, CodeBones, Skeleton } from './Skeleton'
import { RetryButton } from './Retry'
import { ToolRow } from './ToolGroup'
import './AgentMap.css'

// Claude Code subagents of one pane's session: a pill in the composer bar opens a map (the main agent and what it
// spawned); a tap on an agent shows its prompt, its tool calls, its report and its transcript. Data comes from the
// subagent transcripts beside the session transcript, read by the server.
export type AgentState = 'running' | 'finished' | 'failed' | 'stopped'
export interface SubAgent { id: string; description: string; type: string; model: string | null; effort: string | null; background: boolean; state: AgentState; started: string | null; ended: string | null; seconds: number | null; tokens: number | null; tools: number; depth: number; parent: string | null; summary: string | null }
export interface BgTask { id: string; kind: 'shell' | 'monitor'; description: string | null; command: string | null; state: AgentState; started: string | null; ended: string | null; seconds: number | null; parent: string | null; summary: string | null }
export interface SubAgents { main: { model: string | null; context: number | null; title: string | null }; agents: SubAgent[]; tasks?: BgTask[] }
interface TaskDetail extends BgTask { output: string; output_cut?: boolean }
type Step = { kind: 'text'; text: string } | ({ kind: 'tool' } & ToolSummary)
interface AgentDetail extends SubAgent { prompt: string; steps: Step[]; steps_cut?: boolean; report: string | null }

const STATE_LABEL: Record<AgentState, string> = { running: 'Running', finished: 'Done', failed: 'Failed', stopped: 'Stopped' }
const STATE_ORDER: AgentState[] = ['running', 'failed', 'stopped', 'finished']

// "claude-opus-5-5" → "Opus 5.5"; short names ("sonnet") get a capital letter.
export function modelName(model: string | null | undefined) {
  if (!model) return ''
  const found = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?/.exec(model)
  if (found) return `${found[1][0].toUpperCase()}${found[1].slice(1)} ${found[2]}${found[3] ? `.${found[3]}` : ''}`
  return model[0].toUpperCase() + model.slice(1)
}
export const tokenText = (tokens: number | null) => tokens == null ? '' : tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k tokens` : `${tokens} tokens`
const facts = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(' · ')

// Running agents poll every 4 s; an idle list every 15 s. The pill hides while a session has no subagents.
export function useSubAgents(api: Api, session: string, enabled: boolean) {
  const [fast, setFast] = useState(false)
  const fetcher = useCallback((signal: AbortSignal) => enabled
    ? api<SubAgents>(`subagents?session=${encodeURIComponent(session)}`, undefined, signal).then(value => ({ ...value, session }))
    : Promise.resolve({ main: { model: null, context: null, title: null }, agents: [], tasks: [], session }), [api, session, enabled])
  const { data, error, refresh } = usePolling<SubAgents & { session: string }>(fetcher, fast ? 4000 : 15000)
  // A pane switch keeps the old list until the new one loads; never show another pane's agents.
  const list = enabled && data?.session === session ? data : undefined
  const running = (list?.agents.filter(agent => agent.state === 'running').length ?? 0) + (list?.tasks?.filter(task => task.state === 'running').length ?? 0)
  // The poll interval follows the newest list (state adjusted during render, not in an effect).
  if (fast !== running > 0) setFast(running > 0)
  return { data: list, error, refresh, running }
}

const count = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
// A task's name: its description, else the first line of its command.
const taskName = (task: BgTask) => task.description || task.command?.split('\n')[0] || task.id

// The pill counts agents (running ones first); a session with only background tasks counts those.
export function AgentPill({ list, onOpen }: { list: SubAgents | undefined; onOpen: () => void }) {
  const agents = list?.agents ?? [], tasks = list?.tasks ?? []
  if (!agents.length && !tasks.length) return null
  const busyAgents = agents.filter(agent => agent.state === 'running').length
  const busyTasks = tasks.filter(task => task.state === 'running').length
  const running = busyAgents + busyTasks
  const label = agents.length ? count(busyAgents || agents.length, 'agent') : count(busyTasks || tasks.length, 'task')
  const labelRunning = agents.length ? busyAgents > 0 : busyTasks > 0
  return <button type="button" className={`pill agent-pill ${running ? 'running' : ''}`} aria-haspopup="dialog" aria-label={`${label}${labelRunning ? ' running' : ''}. Open the agent map`} onClick={onOpen}>
    <span className={`agent-dot ${running ? 'running' : 'finished'}`} aria-hidden="true" />{label.split(' ')[0]}<span className="agent-pill-word">{label.split(' ')[1]}</span>
  </button>
}

// A state is never color alone: an icon in a tinted disc, and the word beside it.
function StateIcon({ state }: { state: AgentState }) {
  return <span className={`agent-state ${state}`} aria-hidden="true">
    {state === 'running' ? <i /> : state === 'finished' ? <Check size={13} strokeWidth={3} /> : state === 'failed' ? <X size={13} strokeWidth={3} /> : <Square size={9} strokeWidth={0} fill="currentColor" />}
  </span>
}
function StateWord({ state }: { state: AgentState }) {
  return <b className={`agent-state-word ${state}`}>{STATE_LABEL[state]}</b>
}
// "1 running · 2 done": one count per state present, busiest first.
const stateCounts = (items: { state: AgentState }[]) => STATE_ORDER
  .map(state => [state, items.filter(item => item.state === state).length] as const)
  .filter(([, n]) => n > 0).map(([state, n]) => `${n} ${STATE_LABEL[state].toLowerCase()}`)
const runningFirst = <T extends { state: AgentState }>(items: T[]) => [...items].sort((a, b) => Number(b.state === 'running') - Number(a.state === 'running'))
// The second line of a row: the state word, then plain facts.
function RowFacts({ state, children }: { state: AgentState; children: string }) {
  return <small><StateWord state={state} />{children && <> · {children}</>}</small>
}

export function AgentMap({ api, session, list, onClose }: { api: Api; session: string; list: SubAgents | undefined; onClose: () => void }) {
  const [open, setOpen] = useState<string | null>(null)
  const modal = useModal(true, onClose)
  const agents = list?.agents ?? []
  const tasks = list?.tasks ?? []
  const chosen = agents.find(agent => agent.id === open)
  const chosenTask = chosen ? undefined : tasks.find(task => task.id === open)
  const owner = (id: string | null | undefined) => id ? agents.find(agent => agent.id === id) : undefined
  return <dialog {...modal} className={`sheet agent-map ${chosen || chosenTask ? 'agent-detail' : ''}`} aria-labelledby="agent-map-title">
    {chosen ? <AgentView key={chosen.id} api={api} session={session} agent={chosen} parent={owner(chosen.parent)} onBack={() => setOpen(null)} onClose={onClose} />
      : chosenTask ? <TaskView key={chosenTask.id} api={api} session={session} task={chosenTask} parent={owner(chosenTask.parent)} onBack={() => setOpen(null)} onClose={onClose} /> : <>
      <div className="sheet-heading"><h2 id="agent-map-title">Agent map</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
      {agents.length > 0 && <>
        <p className="sheet-sub">{facts(count(agents.length, 'agent'), ...stateCounts(agents))}</p>
        <div className="agent-tree">
          <div className="agent-branch">
            <div className="agent-node main"><span className="agent-state main" aria-hidden="true"><i /></span><span className="agent-node-text"><strong>{list?.main.title || 'Main agent'}</strong><small>{facts(modelName(list?.main.model), list?.main.context != null && `${tokenText(list.main.context)} in context`)}</small></span></div>
            <Children agents={agents} parent={null} onOpen={setOpen} />
          </div>
        </div>
      </>}
      {tasks.length > 0 && <section className="agent-tasks" aria-labelledby="agent-tasks-title">
        <p className="sheet-sub" id="agent-tasks-title">{facts(count(tasks.length, 'background task'), ...stateCounts(tasks))}</p>
        <ul>{runningFirst(tasks).map(task => <li key={task.id}>
          <button type="button" className="agent-node" data-state={task.state} onClick={() => setOpen(task.id)}>
            <StateIcon state={task.state} />
            <span className="agent-node-text"><strong className={task.description ? '' : 'mono'}>{taskName(task)}</strong><RowFacts state={task.state}>{facts(task.kind, task.seconds != null && formatElapsed(task.seconds))}</RowFacts></span>
          </button>
        </li>)}</ul>
      </section>}
    </>}
  </dialog>
}

// Each agent sits beside (wide screens) or under (phones) the agent that spawned it; an agent whose parent is unknown
// sits with the main agent.
function Children({ agents, parent, onOpen }: { agents: SubAgent[]; parent: string | null; onOpen: (id: string) => void }) {
  const known = new Set(agents.map(agent => agent.id))
  const here = runningFirst(agents.filter(agent => parent === null ? !agent.parent || !known.has(agent.parent) : agent.parent === parent))
  if (!here.length) return null
  return <ul className="agent-children" aria-label={parent === null ? 'Spawned agents' : undefined}>
    {here.map(agent => <li key={agent.id}><div className="agent-branch">
      <button type="button" className="agent-node" data-state={agent.state} onClick={() => onOpen(agent.id)}>
        <StateIcon state={agent.state} />
        <span className="agent-node-text"><strong>{agent.description || agent.type}</strong><RowFacts state={agent.state}>{facts(agent.seconds != null && formatElapsed(agent.seconds), tokenText(agent.tokens))}</RowFacts></span>
      </button>
      <Children agents={agents} parent={agent.id} onOpen={onOpen} />
    </div></li>)}
  </ul>
}

function AgentView({ api, session, agent, parent, onBack, onClose }: { api: Api; session: string; agent: SubAgent; parent: SubAgent | undefined; onBack: () => void; onClose: () => void }) {
  const [detail, setDetail] = useState<AgentDetail | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [transcript, setTranscript] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  // Stop drives Claude's /tasks dialog in the pane; the server checks the screen after every key and closes it again.
  const stop = async () => {
    setStopping(true); setStopError('')
    try { await api('subagent-stop', { session, id: agent.id }) } catch (reason) { setStopError(errorText(reason)) }
    setStopping(false)
  }
  // Reload when the list says the agent moved on (more tools, a new state).
  useEffect(() => {
    const controller = new AbortController()
    void api<AgentDetail>(`subagent?session=${encodeURIComponent(session)}&id=${encodeURIComponent(agent.id)}`, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setDetail(value); setError(''); setBusy(false) } })
      .catch(reason => { if (!controller.signal.aborted) { setError(errorText(reason)); setBusy(false) } })
    return () => controller.abort()
  }, [api, session, agent.id, agent.state, agent.tools, attempt])
  const shown = detail ?? agent
  const title = shown.description || shown.type
  const tools = detail?.steps.filter((step): step is Step & { kind: 'tool' } => step.kind === 'tool') ?? []
  const path = (call: string) => `subagent-tool?session=${encodeURIComponent(session)}&agent=${encodeURIComponent(agent.id)}&id=${encodeURIComponent(call)}`
  const live = shown.state === 'running'
  const heading = (back: () => void, label: string, text: ReactNode) => <div className="sheet-heading">
    <button type="button" className="icon-button" aria-label={label} onClick={back}><ArrowLeft /></button>
    <h2 id="agent-map-title" className="agent-title">{text}</h2>
    <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
  </div>
  if (transcript && detail) return <>
    {heading(() => setTranscript(false), 'Back to the agent', 'Transcript')}
    <p className="agent-meta">{title}</p>
    <ol className="agent-steps">
      {detail.steps_cut && <li className="hint">Earlier steps are not shown.</li>}
      {detail.steps.map((step, index) => step.kind === 'text'
        ? <li key={index} className="agent-step-text">{step.text}</li>
        : <li key={index} className={`agent-step-tool ${step.is_error ? 'failed' : ''}`}><strong>{step.name}</strong> <span>{step.description || step.title}</span></li>)}
      {detail.report && <li className="agent-step-text">{detail.report}</li>}
    </ol>
  </>
  return <>
    {heading(onBack, 'Back to the agent map', <><StateIcon state={shown.state} />{title}</>)}
    <div className="agent-meta">
      <p className="agent-facts"><StateWord state={shown.state} /> · {facts(shown.type, modelName(shown.model), shown.effort, shown.background && 'background')}</p>
      <p>Spawned by {parent ? parent.description || parent.type : 'the main agent'}</p>
      <p className="agent-numbers">{facts(shown.seconds != null && formatElapsed(shown.seconds), tokenText(shown.tokens))}</p>
    </div>
    {error ? <p role="alert">{error} <RetryButton label="Retry" className="" busy={busy} onRetry={() => { setBusy(true); setAttempt(attempt + 1) }} /></p>
      : !detail ? <Skeleton label="Loading agent…" className="agent-body"><div className="sk-summary" aria-hidden="true"><Bone w="5em" /></div><div className="sk-summary" aria-hidden="true"><Bone w="8em" /></div></Skeleton> : <div className="agent-body">
        {(shown.state === 'failed' || shown.state === 'stopped') && shown.summary && <p className="agent-summary">{shown.summary}</p>}
        {detail.report && <div className="agent-report" aria-label="Report">{detail.report}</div>}
        <details className="agent-section"><summary><ChevronRight size={16} aria-hidden="true" />Prompt</summary><pre className="agent-text" tabIndex={0}>{detail.prompt}</pre></details>
        <details className="agent-section" open={!detail.report}><summary><ChevronRight size={16} aria-hidden="true" />Tool calls ({tools.length})</summary>
          {tools.length ? <div className="tool-row tool-group agent-tools"><ul className="tool-list">{tools.map((tool, index) => <ToolRow key={`${tool.id}:${index}`} tool={tool} api={api} session={session} live={live} onOpen={() => {}} path={path(tool.id)} compact />)}</ul></div> : <p className="hint">No tool calls yet.</p>}
        </details>
      </div>}
    <div className="agent-actions">
      <button type="button" className="secondary" disabled={!detail} onClick={() => setTranscript(true)}>Open transcript</button>
      {live && <button type="button" className="secondary agent-stop" disabled={stopping} onClick={() => void stop()}>{stopping ? 'Stopping…' : 'Stop agent'}</button>}
      {stopError && <p role="alert">{stopError}</p>}
    </div>
  </>
}

// One background shell or monitor: its command, its output (live while it runs), and Stop task.
function TaskView({ api, session, task, parent, onBack, onClose }: { api: Api; session: string; task: BgTask; parent: SubAgent | undefined; onBack: () => void; onClose: () => void }) {
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  const [tick, setTick] = useState(0)
  const live = task.state === 'running'
  // The output grows while the task runs: reload every 3 s, and once more when the state changes.
  useEffect(() => {
    if (!live) return
    const timer = window.setInterval(() => setTick(value => value + 1), 3000)
    return () => window.clearInterval(timer)
  }, [live])
  useEffect(() => {
    const controller = new AbortController()
    void api<TaskDetail>(`subagent-task?session=${encodeURIComponent(session)}&id=${encodeURIComponent(task.id)}`, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setDetail(value); setError(''); setBusy(false) } })
      .catch(reason => { if (!controller.signal.aborted) { setError(errorText(reason)); setBusy(false) } })
    return () => controller.abort()
  }, [api, session, task.id, task.state, tick, attempt])
  const stop = async () => {
    setStopping(true); setStopError('')
    try { await api('subagent-stop', { session, id: task.id }) } catch (reason) { setStopError(errorText(reason)) }
    setStopping(false)
  }
  const shown = detail ?? task
  // Keep the newest output in view, the way a terminal does, unless the reader scrolled up.
  const output = useRef<HTMLPreElement>(null)
  const pinned = useRef(true)
  useLayoutEffect(() => { if (output.current && pinned.current) output.current.scrollTop = output.current.scrollHeight }, [detail?.output])
  const onScroll = () => { const node = output.current; if (node) pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40 }
  return <>
    <div className="sheet-heading">
      <button type="button" className="icon-button" aria-label="Back to the agent map" onClick={onBack}><ArrowLeft /></button>
      <h2 id="agent-map-title" className={`agent-title ${shown.description ? '' : 'mono'}`}><StateIcon state={shown.state} />{taskName(shown)}</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
    </div>
    <div className="agent-meta">
      <p className="agent-facts"><StateWord state={shown.state} /> · {facts(shown.kind, 'background')}</p>
      <p>Started by {parent ? parent.description || parent.type : 'the main agent'}</p>
      <p className="agent-numbers">{shown.seconds != null && formatElapsed(shown.seconds)}</p>
    </div>
    <div className="agent-body">
      {(shown.state === 'failed' || shown.state === 'stopped') && shown.summary && <p className="agent-summary">{shown.summary}</p>}
      {shown.command && <details className="agent-section" open={Boolean(shown.description) || undefined}><summary><ChevronRight size={16} aria-hidden="true" />Command</summary><pre className="agent-text" tabIndex={0}>{shown.command}</pre></details>}
      <details className="agent-section" open><summary><ChevronRight size={16} aria-hidden="true" />Output</summary>
        {error ? <p role="alert">{error} <RetryButton label="Retry" className="" busy={busy} onRetry={() => { setBusy(true); setAttempt(attempt + 1) }} /></p>
          : !detail ? <CodeBones lines={5} label="Loading output…" className="agent-text agent-output" />
          : detail.output ? <pre ref={output} onScroll={onScroll} className="agent-text agent-output" tabIndex={0} aria-label="Output">{detail.output_cut ? '…\n' : ''}{detail.output}</pre>
          : <p className="hint">No output yet.</p>}
      </details>
    </div>
    {live && <div className="agent-actions">
      <button type="button" className="secondary agent-stop" disabled={stopping} onClick={() => void stop()}>{stopping ? 'Stopping…' : 'Stop task'}</button>
      {stopError && <p role="alert">{stopError}</p>}
    </div>}
  </>
}
