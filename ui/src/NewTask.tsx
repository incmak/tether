import { Spinner } from './Skeleton'
import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowUp, ChevronDown, Mic, Paperclip, Square, X } from 'lucide-react'
import { SESSION_NAME, SESSION_NAME_HELP, errorText } from './api'
import type { Api, Pane, SessionsData } from './api'
import { useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { uploadFile } from './Composer'
import { ChatPicker, resumeChat, resumeInNewSession } from './ChatPicker'
import { AgentIcon, agentNames } from './Sidebar'
import type { Session } from './Sidebar'
import { keyLabel } from './keys'
import { useDictation } from './useDictation'
import { WORKTREE_TIMEOUT, WorktreeField } from './Worktree'
import type { MadeWorktree } from './Worktree'
import { CodexTrustCard } from './CodexTrust'
import { useCodexTrust } from './codexTrustState'
import { FolderField, NameField } from './CreateFields'
import type { MainModel } from './prefs'
import { groupNameError } from './groups'
import './NewTask.css'

type Worker = 'claude' | 'codex'
type Starter = Worker | 'shell'
type Review = 'off' | 'source' | 'new'
/** tab: a new tab beside `pane`. stack: a new stack in `session`, with a first tab or empty. session: a new tmux session. */
export type CreateKind = 'tab' | 'stack' | 'session'
interface TaskFile { path: string; name: string; url?: string }
const WORKERS: Worker[] = ['claude', 'codex']
const STARTERS: Starter[] = ['claude', 'codex', 'shell']
const REVIEWS: [Review, string][] = [['off', 'Off'], ['source', 'This tab reviews'], ['new', 'New tab reviews']]
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
interface TaskModel { id: string; label: string }
type TaskModels = Record<Worker, TaskModel[]>
// The model list comes from the server (claude aliases, the Codex model cache). Keep the last list for an instant first paint.
const cachedTaskModels = (): TaskModels | null => { try { return JSON.parse(localStorage.getItem('tether.newTask.models') ?? 'null') as TaskModels | null } catch { return null } }
const lastModel = (agent: Worker) => { try { return localStorage.getItem(`tether.newTask.model.${agent}`) ?? '' } catch { return '' } }
const lastWorker = () => { try { return localStorage.getItem('tether.newTask.agent') === 'codex' ? 'codex' : 'claude' } catch { return 'claude' } }
const finePointer = () => typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches

/** The default name of a new session: chat-MMDD-HHMM. */
export function defaultSessionName() {
  const now = new Date()
  const two = (n: number) => String(n).padStart(2, '0')
  return `chat-${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}`
}

// The first pane of a new session shows up in the session list a moment after tmux makes it.
async function sessionPane(api: Api, session: string, signal: AbortSignal) {
  const deadline = Date.now() + 5000
  while (Date.now() < deadline && !signal.aborted) {
    const list = await api<SessionsData>('sessions', undefined, AbortSignal.any([signal, AbortSignal.timeout(Math.max(1, deadline - Date.now()))]))
    const found = list.sessions.find(pane => pane.tmux_session === session)
    if (found) return found
    await new Promise(resolve => setTimeout(resolve, 300))
  }
  return undefined
}

export interface NewTaskProps {
  api: Api; kind?: CreateKind
  /** tab and stack: the tab the new one starts beside (its folder, its chat as context, its reviewer). */
  pane?: Pane
  /** stack: the session the stack goes in. */
  session?: Session
  /** Desktop: the popover opens here. Without it, the box is a sheet with a close button. */
  at?: Anchor
  micOk: boolean; chime: boolean; mainModel?: MainModel
  /** session: names in use, so suggestions skip them. */
  taken?: string[]
  onClose: () => void
  /** tab: Empty tab. */
  onEmpty?: () => void
  /** tab and stack: a new tab started with a task. */
  onCreated: (pane: string, worktree?: MadeWorktree) => void
  /** An existing chat or tab opened. */
  onOpen: (pane: Pane) => void
  /** tab: a tab of another session moved here. */
  onMoved?: (pane: Pane) => void
  /** stack: an empty stack was made. */
  onStack?: () => void
  /** session: the session is ready. `warning` says what did not start. */
  onSession?: (pane: Pane, worktree?: MadeWorktree, warning?: string) => void
}

// One box for a new tab (the + on the tab strip), a new stack (the + on the stack row) and a new session (sidebar, ⌥N).
// Each starts an agent with a task (server/delegate.py, POST tmux-sessions "task"), or starts empty.
// Stack and session add a name with suggestions and a folder (CreateFields.tsx).
export function NewTask({ api, kind = 'tab', pane, session, at, micOk, chime, mainModel, taken, onClose, onEmpty, onCreated, onOpen, onMoved, onStack, onSession }: NewTaskProps) {
  const modal = useModal(true, () => { if (!pending) onClose() })
  const [opening, setOpening] = useState(false)
  // Open existing is 100 px wider, and the box keeps its right edge under the +.
  const place = useMemo(() => at && opening ? { ...at, x: at.x - 100 } : at, [at, opening])
  const popover = usePopover(modal.ref, place, onClose)
  const [text, setText] = useState('')
  const [agent, setAgent] = useState<Starter>(lastWorker)
  const worker: Worker = agent === 'shell' ? 'claude' : agent
  const shell = agent === 'shell'
  const [review, setReview] = useState<Review>('off')
  const [models, setModels] = useState<TaskModels | null>(cachedTaskModels)
  // '' = the agent's own default. A remembered model that left the list falls back to the default.
  const [picked, setPicked] = useState<Record<Worker, string>>(() => ({ claude: lastModel('claude'), codex: lastModel('codex') }))
  // /start takes no model: a session without a task starts the agent's default model, so the list shows with a task only.
  const choices = shell || (kind === 'session' && !text.trim()) ? [] : models?.[worker] ?? []
  const model = choices.some(item => item.id === picked[worker]) ? picked[worker] : ''
  const [name, setName] = useState(() => kind === 'session' ? defaultSessionName() : '')
  const [tried, setTried] = useState(false)
  // null = the default folder: this tab's folder for a stack, the saved default folder for a session.
  const [folder, setFolder] = useState<string | null>(null)
  const [savedFolder, setSavedFolder] = useState('')
  const [context, setContext] = useState(false)
  const [worktree, setWorktree] = useState<string | null>(null)
  // A reviewer stays in this tab's folder: on a clean branch it could not see the work under review.
  const branch = review === 'new' ? null : worktree
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [files, setFiles] = useState<TaskFile[]>([])
  const [uploading, setUploading] = useState(0)
  const urls = useRef(new Set<string>())
  const field = useRef<HTMLTextAreaElement>(null)
  const controller = useRef<AbortController | null>(null)
  const creating = useRef(false)
  const dictation = useDictation({ text, setText, chime, onFinished: () => { if (finePointer()) field.current?.focus() } })
  const { listening, transcribing, seconds, canRecord } = dictation
  useEffect(() => () => controller.current?.abort(), [])
  useEffect(() => {
    const abort = new AbortController()
    api<TaskModels>('task-models', {}, abort.signal).then(result => {
      setModels(result)
      try { localStorage.setItem('tether.newTask.models', JSON.stringify(result)) } catch { /* private mode */ }
    }).catch(() => { /* keep the cached list; Default always works */ })
    return () => abort.abort()
  }, [api])
  // A new session starts in the default folder from Settings, or in the home folder.
  useEffect(() => {
    if (kind !== 'session') return
    const abort = new AbortController()
    api<{ prefs: { sessionFolder?: string } }>('prefs', undefined, abort.signal).then(data => setSavedFolder(data.prefs.sessionFolder || ''), () => { /* home folder */ })
    return () => abort.abort()
  }, [api, kind])
  useEffect(() => { const made = urls.current; return () => made.forEach(url => URL.revokeObjectURL(url)) }, [])
  // A phone keyboard would cover the popover: focus a field only with a mouse or trackpad. A stack starts at its name.
  useEffect(() => { if (kind !== 'stack' && finePointer()) field.current?.focus() }, [kind])
  const agentPane = !!pane && (pane.agent === 'claude' || pane.agent === 'codex')
  const startFolder = kind === 'session' ? savedFolder || '~' : pane?.dir || '~'
  const trust = useCodexTrust(api, agent === 'codex' ? kind === 'session' || folder !== null
    ? { mode: 'new', ...(folder !== null ? { cwd: folder } : {}), worktree: branch !== null }
    : { mode: 'new', session: pane?.id, worktree: branch !== null } : null)
  const canContext = kind !== 'session' && agentPane && !!pane?.thread && UUID.test(pane.thread)
  const canReview = kind !== 'session' && agentPane && !!pane?.writable
  const canReviewNew = canReview && canContext
  const canAttach = kind !== 'session' && !!pane
  const problem = kind === 'stack' ? groupNameError(name, session?.groups) : kind === 'session' && !SESSION_NAME.test(name.trim()) ? SESSION_NAME_HELP : ''
  const nameContext = () => ({ task: text, folder: folder ?? startFolder,
    tabs: kind === 'stack' ? session?.panes.map(item => item.pane_name || item.agent_name || '').filter(Boolean).slice(0, 12) : undefined,
    taken: kind === 'stack' ? ['Unstacked', 'Main', ...(session?.groups ?? []).map(group => group.name)] : taken?.slice(0, 200) })
  // Pasted files use the composer's upload. Their saved paths go in the task, as they do in a chat message.
  async function attach(list: File[]) {
    if (!pane) return
    setError('')
    setUploading(count => count + list.length)
    for (const file of list) {
      try {
        const saved = await uploadFile(file, pane.id, pane.thread)
        const url = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined
        if (url) urls.current.add(url)
        setFiles(current => [...current, { ...saved, url }])
      } catch (failure) { setError(errorText(failure)) }
      setUploading(count => count - 1)
    }
  }
  function remove(file: TaskFile) {
    if (file.url) { URL.revokeObjectURL(file.url); urls.current.delete(file.url) }
    setFiles(current => current.filter(item => item !== file))
  }
  const remember = () => { try { if (!shell) { localStorage.setItem('tether.newTask.agent', worker); localStorage.setItem(`tether.newTask.model.${worker}`, model) } } catch { /* private mode */ } }

  /** A stack with no tab: the + and Empty stack. */
  async function emptyStack() {
    setTried(true)
    if (!session || problem || creating.current) return
    creating.current = true
    setPending(true); setError('')
    try { await api('groups/create', { session: session.id, name: name.trim() }); onStack?.() }
    catch (failure) { setError(errorText(failure)); setPending(false) }
    finally { creating.current = false }
  }

  async function makeSession(task: string, abort: AbortController) {
    const clean = name.trim()
    const first = !shell && task ? { agent: worker, message: task, ...(model ? { model } : {}) } : null
    let made: MadeWorktree | undefined
    let created: Pane | undefined
    try {
      const result = await api<{ session: string; name: string; pane?: string; task_error?: string; worktree?: MadeWorktree }>('tmux-sessions',
        { name: clean, ...(branch !== null ? { worktree: branch } : folder !== null ? { cwd: folder } : {}), ...(first ? { task: first } : {}) },
        abort.signal, undefined, branch !== null ? WORKTREE_TIMEOUT : undefined)
      made = result.worktree
      remember()
      created = await sessionPane(api, result.session, abort.signal)
      if (abort.signal.aborted) return
      if (!created) throw new Error(`Session “${result.name}” was made, but its tab is not in the list yet. Close this box and select it in the sidebar.`)
      let warning = result.task_error ? `Session made. The task did not start: ${result.task_error}` : undefined
      if (!first && !shell) {
        try { await api('start', { session: created.id, thread: null, agent: worker }, abort.signal) }
        catch (failure) { if (abort.signal.aborted) return; warning = `Session made. ${agentNames[worker]} did not start: ${errorText(failure)}` }
      }
      onSession?.(created, made, warning)
    } catch (failure) {
      if (abort.signal.aborted) return
      // The branch exists now: a second Create with the same name would be refused.
      if (made) setWorktree(null)
      throw new Error(errorText(failure) + (made ? ` The worktree is at ${made.path}.` : ''), { cause: failure })
    }
  }

  async function create(event?: React.FormEvent) {
    event?.preventDefault()
    if (creating.current || pending || uploading) return
    setTried(true)
    if (problem) { setError(''); return }
    if (branch === '') { setError('Enter a branch name for the new worktree.'); return }
    creating.current = true
    setPending(true); setError('')
    const abort = new AbortController()
    controller.current = abort
    try {
      const waiting = dictation.finalText()
      dictation.stop()
      const task = (waiting ? await waiting : text).trim()
      if (abort.signal.aborted) return
      if (kind === 'session') { await makeSession(task, abort); return }
      // The text goes first: the server names the tab from the first line.
      const message = [task, files.map(file => file.path).join(' ')].filter(Boolean).join('\n\n')
      if (!message && review !== 'new') {
        if (kind === 'stack') { creating.current = false; await emptyStack(); return }
        setError('Type a task first.'); setPending(false); field.current?.focus(); return
      }
      if (!pane) return
      const where = branch !== null ? { worktree: branch } : folder !== null ? { cwd: folder } : {}
      const result = await api<{ pane: string; name: string; worktree?: MadeWorktree }>('delegate', {
        session: pane.id, thread: pane.thread, agent: worker, message, context: context && canContext, review: review === 'off' ? false : review,
        ...(model ? { model } : {}), ...(!task && files.length ? { name: files[0].name } : {}), ...where, ...(kind === 'stack' ? { stack: name.trim() } : {}),
      }, abort.signal, undefined, branch !== null ? WORKTREE_TIMEOUT : undefined)
      remember()
      onCreated(result.pane, result.worktree)
    } catch (failure) {
      if (!abort.signal.aborted) { setError(errorText(failure)); setPending(false) }
    } finally { creating.current = false; if (abort.signal.aborted) setPending(false) }
  }

  const title = kind === 'stack' ? 'New stack' : kind === 'session' ? 'New session' : 'New tab'
  const closeButton = !at && <button type="button" className="icon-button" aria-label={`Close ${title.toLowerCase()}`} disabled={pending} onClick={onClose}><X size={18} /></button>
  const modes = kind !== 'stack' && <div className="pick-modes" role="tablist" aria-label={title}>
    <button type="button" role="tab" aria-selected={!opening} disabled={pending} onClick={() => setOpening(false)}>{kind === 'session' ? 'New' : 'New task'}</button>
    <button type="button" role="tab" aria-selected={opening} disabled={pending} onClick={() => { dictation.stop(); setOpening(true) }}>Open existing</button>
  </div>
  const empty = kind === 'tab' ? onEmpty && <button type="button" className="task-empty" disabled={pending} onClick={onEmpty}>Empty tab</button>
    : kind === 'stack' ? <button type="button" className="task-empty" disabled={pending} onClick={() => void emptyStack()}>Empty stack</button> : null
  const head = <div className="task-head">
    {kind === 'stack' ? <h2 id="task-title">{title}</h2> : modes}
    <span className="task-head-end">{empty}{closeButton}</span>
  </div>
  const placeholder = shell ? 'A shell starts without a task.' : review === 'new' ? 'What should it check? Optional.' : kind === 'tab' ? 'What should it work on?' : 'First task. Optional.'
  const createLabel = pending
    ? kind === 'session' ? 'Making session…' : `Starting ${choices.find(item => item.id === model)?.label ?? agentNames[worker]}…`
    : kind === 'stack' ? text.trim() || files.length || review === 'new' ? 'Make stack and start' : 'Make empty stack'
    : kind === 'session' ? 'Create session' : 'Create'
  return <dialog {...modal} {...popover} className={['sheet task-pop', at && 'popover', !at && 'task-sheet', kind !== 'tab' && 'task-wide', pending && 'working', opening && 'opening'].filter(Boolean).join(' ')}
    {...(kind === 'stack' ? { 'aria-labelledby': 'task-title' } : { 'aria-label': title })}
    onCancel={event => { event.stopPropagation(); if (!pending) modal.onCancel(event); else event.preventDefault() }}>
    {opening && <div className="task-open">
      {head}
      {kind === 'session'
        ? <ChatPicker api={api} onChat={async chat => onOpen(await resumeInNewSession(api, chat))} onTab={onOpen} />
        : pane && <ChatPicker api={api} exclude={pane.id} here={pane.tmux_session}
          onChat={async chat => onOpen(await resumeChat(api, chat, { after: pane.id }))} onTab={onOpen}
          onMove={async other => { await api('merge', { kind: 'pane', source: other.id, target: pane.tmux_session }); onMoved?.(other) }} />}
    </div>}
    {!opening && <form onSubmit={create} noValidate onKeyDown={event => {
      if (micOk && canRecord && !pending && !shell && event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.code === 'KeyV') {
        event.preventDefault()
        if (transcribing) dictation.cancelConverting(); else void dictation.toggle()
      }
    }}>
      {head}
      {kind !== 'tab' && <NameField api={api} kind={kind} label={kind === 'stack' ? 'Stack name' : 'Session name'} value={name} disabled={pending} autoFocus={kind === 'stack' && finePointer()}
        onChange={value => { setName(value); setError('') }} problem={tried ? problem : undefined} context={nameContext} mainModel={mainModel} />}
      <div className="task-field">
        <textarea ref={field} value={shell ? '' : text} rows={3} maxLength={20000} readOnly={listening || transcribing || pending} disabled={shell} aria-label={kind === 'tab' ? 'Task' : 'First task'} placeholder={placeholder}
          onChange={event => { setText(event.target.value); setError(''); dictation.setError('') }}
          onPaste={event => { if (event.clipboardData.files.length) { event.preventDefault(); if (!canAttach) setError('Attach files after the session starts.'); else if (!pending) void attach(Array.from(event.clipboardData.files)) } }}
          onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void create() } }} />
        {files.length + uploading > 0 && <ul className="attachments task-files" aria-label="Attached files">
          {files.map(file => <li key={file.path} className="chip">
            {file.url ? <img className="task-thumb" src={file.url} alt="" /> : <Paperclip size={13} />}<span>{file.name}</span>
            <button type="button" aria-label={`Remove ${file.name}`} disabled={pending} onClick={() => remove(file)}><X size={14} /></button>
          </li>)}
          {uploading > 0 && <li className="chip task-uploading"><Spinner size={13} /><span>Uploading…</span></li>}
        </ul>}
        <div className="task-foot">
          <div className="task-agents" role="radiogroup" aria-label="Agent">
            {(kind === 'session' ? STARTERS : WORKERS).map(choice => <button type="button" key={choice} role="radio" aria-checked={agent === choice} disabled={pending} className={agent === choice ? 'on' : ''} onClick={() => { if (choice === 'shell') dictation.stop(); setAgent(choice) }}>
              <AgentIcon agent={choice} size={14} />{agentNames[choice]}
            </button>)}
          </div>
          {choices.length > 0 && <label className="task-model" title="Model for the new tab">
            <select aria-label={`${agentNames[worker]} model`} value={model} disabled={pending} onChange={event => { const value = event.target.value; setPicked(current => ({ ...current, [worker]: value })) }}>
              <option value="">Default</option>
              {choices.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
            <ChevronDown size={13} aria-hidden="true" />
          </label>}
          <span className="composer-status" aria-live="polite">{listening ? <><span className="task-rec-word">Recording </span>{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, '0')}</> : transcribing ? 'Converting…' : ''}</span>
          {micOk && canRecord && !shell && (transcribing
            ? <button type="button" className="icon-button mic active" aria-label="Stop converting and keep the live text" disabled={pending} onClick={() => dictation.cancelConverting()}><X size={19} /></button>
            : <button type="button" className={`icon-button mic ${listening ? 'active' : ''}`} aria-label={listening ? 'Stop recording' : 'Start voice input'} aria-pressed={listening} aria-keyshortcuts="Alt+V" title={`${listening ? 'Stop recording' : 'Voice input'} (${keyLabel('⌥V')})`} disabled={pending} onClick={() => void dictation.toggle()}>{listening ? <Square size={18} /> : <Mic size={20} />}</button>)}
        </div>
      </div>
      {kind !== 'tab' && branch === null && <FolderField api={api} value={folder} start={startFolder} note={kind === 'session' ? savedFolder ? 'Default' : 'Home' : 'This tab’s'} disabled={pending} onChange={value => { setFolder(value); setError('') }} />}
      {review !== 'new' && (kind !== 'tab' ? folder === null : true) && <WorktreeField api={api} session={kind === 'session' ? undefined : pane?.id} value={worktree} onChange={value => { setWorktree(value); if (error) setError('') }} disabled={pending} />}
      <CodexTrustCard trust={trust} />
      {kind !== 'session' && agentPane && <div className="task-options">
        <div className="task-review">
          <strong id="task-review-label">Review</strong>
          <div className="task-agents" role="radiogroup" aria-labelledby="task-review-label">
            {REVIEWS.map(([choice, label]) => { const usable = choice === 'off' || (choice === 'new' ? canReviewNew : canReview); return <button type="button" key={choice} role="radio" aria-checked={review === choice} disabled={pending || !usable} className={review === choice ? 'on' : ''} title={usable ? undefined : 'Send a first message here first.'} onClick={() => setReview(choice)}>{label}</button> })}
          </div>
          {review === 'source' && <small>This tab checks each finished turn of the new tab.</small>}
          {review === 'new' && <small>The new tab checks each finished turn here and sends feedback back.</small>}
        </div>
        {review !== 'new' && <label className={canContext ? 'task-switch' : 'task-switch off'}>
          <span><strong>Use this chat as context</strong><small>{canContext ? 'A short brief of recent turns, not the whole chat.' : 'Send a first message here first.'}</small></span>
          <input type="checkbox" role="switch" checked={context && canContext} disabled={!canContext || pending} onChange={event => setContext(event.target.checked)} />
        </label>}
      </div>}
      {(error || dictation.error) && <p className="error task-error" role="alert">{error || dictation.error}</p>}
      <button className="primary task-create" disabled={pending || uploading > 0}>
        <span>{createLabel}</span>{pending ? <Spinner size={16} /> : <ArrowUp size={16} />}
      </button>
    </form>}
  </dialog>
}
