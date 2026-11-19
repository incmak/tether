import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { BellRing, ChevronRight, ClipboardList, Copy, ExternalLink, ListPlus, RefreshCw, Star } from 'lucide-react'
import type { Api } from './api'
import { errorText } from './api'
import { Bone } from './Skeleton'
import { agoLabel, dueLabel, groupTasks, settings, syncNow, toBacklog } from './tasks'
import type { ExternalTask, TaskSource, useTasks } from './tasks'
import { announce } from './reminders'
import './Todos.css'

type Polled = ReturnType<typeof useTasks>
const LABEL: Record<string, string> = { todo: 'To Do', planner: 'Planner' }

function TaskRow({ task, now, overdue, api, onOpenItem, onChanged, onError }: {
  task: ExternalTask; now: number; overdue: boolean; api: Api; onOpenItem?: (ref: string) => void; onChanged: () => Promise<void>; onError: (text: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const askRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const itemRef = useRef<HTMLElement>(null)
  // Move focus only after React has drawn the control that takes it.
  const focusNext = (ref: React.RefObject<HTMLElement | null>) => requestAnimationFrame(() => ref.current?.focus())
  const ask = () => { setConfirming(true); focusNext(confirmRef) }
  const cancel = () => { setConfirming(false); focusNext(askRef) }
  const add = async () => {
    if (busy) return
    setBusy(true)
    try {
      await toBacklog(api, task.key)
      await onChanged()
      setConfirming(false)
      focusNext(itemRef)
    } catch (error) { onError(errorText(error)) } finally { setBusy(false) }
  }
  const app = LABEL[task.source] ?? 'the app'
  return <li className="reminder-row todo-row">
    <div className="reminder-main">
      <p className="reminder-text">{task.importance === 'high' && <Star className="todo-star" size={13} aria-label="Important" />}{task.title}</p>
      <p className="reminder-meta">
        {task.due && <span className={overdue ? 'todo-overdue' : ''}>{dueLabel(task.due, now)}{task.timeUncertain && task.due.at !== null ? ' (time zone unclear)' : ''}</span>}
        <span>{LABEL[task.source] ?? task.source}{task.list ? ` · ${task.list}` : ''}</span>
        {task.reminder && <span className="todo-reminder" title={`Tether reminds you: ${task.reminder}`}><BellRing size={12} aria-hidden="true" />{task.reminder}</span>}
      </p>
    </div>
    <div className="reminder-actions">
      {task.backlog
        ? (onOpenItem
          ? <button type="button" ref={itemRef as React.RefObject<HTMLButtonElement>} className="link-button todo-backlog" title="In the backlog" onClick={() => onOpenItem(task.backlog!)}><ClipboardList size={13} aria-hidden="true" />{task.backlog}</button>
          : <span ref={itemRef as React.RefObject<HTMLSpanElement>} tabIndex={-1} className="todo-backlog" title="In the backlog"><ClipboardList size={13} aria-hidden="true" />{task.backlog}</span>)
        : <button type="button" ref={askRef} className="icon-button" aria-label={`Add to backlog: ${task.title}`} aria-expanded={confirming} title="Add to backlog Inbox" onClick={() => confirming ? cancel() : ask()}><ListPlus size={16} /></button>}
      {task.link && <a className="icon-button" href={task.link} target="_blank" rel="noopener noreferrer" aria-label={`Open in ${app}: ${task.title}`} title={`Open in ${app}`}><ExternalLink size={15} /></a>}
    </div>
    {confirming && !task.backlog && <div className="todo-confirm" role="group" aria-label={`Add to backlog: ${task.title}`}>
      <p>Add to the backlog Inbox? Every agent sees Inbox items.</p>
      <button type="button" ref={confirmRef} className="primary" aria-disabled={busy} onClick={() => void add()}>{busy ? 'Adding…' : 'Add'}</button>
      <button type="button" className="secondary" onClick={cancel}>Cancel</button>
    </div>}
  </li>
}

function Group({ title, tasks, render, folded = false }: { title: string; tasks: ExternalTask[]; render: (task: ExternalTask) => React.ReactNode; folded?: boolean }) {
  const id = useId()
  const [open, setOpen] = useState(!folded)
  if (!tasks.length) return null
  return <section className="reminder-section" aria-labelledby={id}>
    <h3 id={id}>{folded
      ? <button type="button" className="reminder-fold" aria-expanded={open} onClick={() => setOpen(!open)}><ChevronRight className="reminder-fold-icon" size={14} aria-hidden="true" />{title} <span className="reminder-count">{tasks.length}</span></button>
      : <>{title} <span className="reminder-count">{tasks.length}</span></>}</h3>
    {open && <ul>{tasks.map(render)}</ul>}
  </section>
}

function SourceRow({ source, now, api, onChanged, onError }: { source: TaskSource; now: number; api: Api; onChanged: () => Promise<void>; onError: (text: string) => void }) {
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const change = async (body: { enabled?: boolean; alerts?: boolean }) => {
    if (busy) return
    setBusy(true)
    try { await settings(api, source.id, body); await onChanged(); if ('alerts' in body) announce() } catch (error) { onError(errorText(error)) } finally { setBusy(false) }
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(source.command!); setCopied(true); window.setTimeout(() => setCopied(false), 2000) } catch { onError('Cannot copy. Select the command and copy it.') }
  }
  const status = !source.enabled ? 'Hidden'
    : source.state === 'off' ? 'Off'
    : source.state === 'syncing' && !source.syncedAt ? 'Reading tasks…'
    : `${source.count} open${source.syncedAt ? ` · synced ${agoLabel(source.syncedAt, now)}` : ''}${source.complete ? '' : ' · some tasks not read'}`
  return <li className="todo-source">
    <div className="todo-source-head">
      <strong>{source.label}</strong>
      <span className={`todo-source-state ${source.state}`}>{status}</span>
    </div>
    {source.why && source.enabled && <p className={source.state === 'error' ? 'todo-source-why error' : 'todo-source-why'}>{source.why}</p>}
    {source.command && source.state === 'off' && <div className="todo-command">
      <code>{source.command}</code>
      <button type="button" className="icon-button" aria-label="Copy the command" title={copied ? 'Copied' : 'Copy'} onClick={() => void copy()}><Copy size={14} /></button>
      <span className="visually-hidden" role="status">{copied ? 'Copied' : ''}</span>
    </div>}
    <label className="switch-row"><span><strong>Show tasks</strong></span>
      <input type="checkbox" role="switch" checked={source.enabled} aria-disabled={busy} onChange={() => void change({ enabled: !source.enabled })} /></label>
    {source.id === 'todo' && <label className="switch-row"><span><strong>Alerts</strong><small>A task’s own reminder time becomes a Tether reminder. Past times never alert.</small></span>
      <input type="checkbox" role="switch" checked={source.alerts} disabled={!source.enabled} aria-disabled={busy} onChange={() => void change({ alerts: !source.alerts })} /></label>}
  </li>
}

/** Todos: open tasks from Mak's task apps, read only, grouped like a Planned view. */
export function TodosPanel({ api, polled, onOpenItem }: { api: Api; polled: Polled; onOpenItem?: (ref: string) => void }) {
  const { data, error, loading, refresh } = polled
  const [problem, setProblem] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 30000); return () => clearInterval(timer) }, [])
  const shown = useMemo(() => {
    if (!Array.isArray(data?.sources) || !Array.isArray(data?.tasks)) return []
    const on = new Set(data.sources.filter(source => source.enabled).map(source => source.id))
    return data.tasks.filter(task => on.has(task.source))
  }, [data])
  const groups = useMemo(() => groupTasks(shown, now), [shown, now])
  const sources = Array.isArray(data?.sources) ? data.sources : []
  const live = sources.some(source => source.enabled && source.state !== 'off')
  // "No open tasks" is true only after a source has read its tasks at least once.
  const read = sources.some(source => source.enabled && source.state !== 'off' && source.syncedAt !== null)
  const reading = sources.some(source => source.enabled && source.state === 'syncing')
  const changed = () => refresh()
  const sync = async () => {
    setSyncing(true); setProblem('')
    try { await syncNow(api); await refresh() } catch (error) { setProblem(errorText(error)) } finally { setSyncing(false) }
  }
  const row = (overdue: boolean) => (task: ExternalTask) => <TaskRow key={task.key} task={task} now={now} overdue={overdue} api={api} onOpenItem={onOpenItem} onChanged={changed} onError={setProblem} />
  return <div className="todos-panel">
    <div className="todos-bar">
      <p className="sheet-note">Read only. Change tasks in their app.</p>
      {live && <button type="button" className="secondary todos-sync" disabled={syncing} onClick={() => void sync()}><RefreshCw size={14} aria-hidden="true" className={syncing ? 'spin' : ''} />{syncing ? 'Syncing…' : 'Sync now'}</button>}
    </div>
    {problem && <p className="error reminder-error" role="alert">{problem}</p>}
    <div className="reminder-lists" aria-busy={loading && !data}>
      {loading && !data && <ul className="reminder-skeleton" aria-label="Loading todos">{[0, 1, 2].map(key => <li key={key}><Bone w="70%" h={14} /><Bone w="35%" h={12} /></li>)}</ul>}
      {error && !data && <div className="reminder-load-error" role="alert"><p>Cannot load todos: {error}</p><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
      {error && data && <p className="reminder-stale" role="status">Not up to date: {error}</p>}
      {data && live && !shown.length && (read
        ? <p className="reminder-empty">No open tasks. New tasks in To Do and Planner show here within 5 minutes.</p>
        : reading
          ? <p className="reminder-empty" role="status">Reading your tasks…</p>
          : <p className="reminder-empty">Tether has not read your tasks yet. See Task apps below.</p>)}
      {data && !live && <p className="reminder-empty">Connect a task app below to see your To Do and Planner tasks here.</p>}
      <Group title="Overdue" tasks={groups.overdue} render={row(true)} />
      <Group title="Today" tasks={groups.today} render={row(false)} />
      <Group title="Upcoming" tasks={groups.upcoming} render={row(false)} />
      <Group title="No due date" tasks={groups.someday} render={row(false)} folded={groups.overdue.length + groups.today.length + groups.upcoming.length > 0} />
      {data && <section className="reminder-section todo-sources" aria-label="Task apps">
        <h3>Task apps</h3>
        <ul>{sources.map(source => <SourceRow key={source.id} source={source} now={now} api={api} onChanged={changed} onError={setProblem} />)}</ul>
        <p className="reminder-deleted-note">Planner shows tasks assigned to you. Private tasks show through To Do. Todoist and Google Tasks come later.</p>
      </section>}
    </div>
  </div>
}
