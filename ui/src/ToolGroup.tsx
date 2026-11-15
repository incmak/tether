import { RetryButton } from './Retry'
import { Loading } from './Skeleton'
import { useContext, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { AppWindow, Bot, ChevronRight, CircleAlert, Code2, ExternalLink, Eye, Globe, ListTodo, LoaderCircle, Search, SquarePen, Terminal, Wrench } from 'lucide-react'
import { errorText } from './api'
import type { Api, ToolSummary, ToolDetail } from './api'
import { ImageViewer } from './ImageViewer'
import { PreviewOpener } from './preview'
import { Elapsed, formatElapsed } from './Notices'
import { groupSeconds, liveTail, slowestStep, toolHeadline, toolKind, toolSeconds } from './toolSummary'

// Tool icons and the live-edge detail scroller follow T3 Code's TimelineToolActivityIcon and WorkLogDetails
// (github.com/pingdotgg/t3code, MIT License, Copyright (c) 2026 T3 Tools Inc.).
const KIND_ICONS = { command: Terminal, edit: SquarePen, read: Eye, search: Search, web: Globe, agent: Bot, plan: ListTodo, other: Wrench }
function ToolIcon({ name, failed, running }: { name: string; failed: boolean; running?: boolean }) {
  if (running) return <LoaderCircle className="tool-icon spin" size={15} aria-hidden="true" />
  const Icon = failed ? CircleAlert : KIND_ICONS[toolKind(name)]
  return <Icon className={failed ? 'tool-icon failed' : 'tool-icon'} size={15} aria-hidden="true" />
}

// A step's time: counting up while it runs, fixed once it ends. Under a second says nothing.
function StepTime({ tool, live }: { tool: ToolSummary; live: boolean }) {
  const seconds = toolSeconds(tool)
  const started = tool.started ? Date.parse(tool.started) : NaN
  if (tool.status === 'pending') return live && !Number.isNaN(started) ? <small className="tool-time"><Elapsed since={started} /></small> : null
  return seconds !== null && seconds >= 1 ? <small className="tool-time">{formatElapsed(seconds)}</small> : null
}

// Long output keeps the newest line in view while you stay at the bottom; scroll up to read and it stays put.
function LiveDetail({ children, label }: { children: React.ReactNode; label: string }) {
  const ref = useRef<HTMLPreElement>(null)
  const atEdge = useRef(true)
  useLayoutEffect(() => { const box = ref.current; if (box && atEdge.current) box.scrollTop = box.scrollHeight })
  return <pre ref={ref} tabIndex={0} aria-label={label} onScroll={event => { const box = event.currentTarget; atEdge.current = box.scrollHeight - box.scrollTop - box.clientHeight <= 4 }}>{children}</pre>
}

// `path` reads the detail from another endpoint (a subagent's transcript); the default is the pane's own transcript.
const FILE_TOOLS = new Set(['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit'])

// `compact` is the agent map's one-line row: the tool name, then what it did (a file tool shows the file name).
export function ToolRow({ tool, api, session, live, onOpen, path, compact = false }: { tool: ToolSummary; api: Api; session: string; live: boolean; onOpen: () => void; path?: string; compact?: boolean }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<ToolDetail | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const id = useId()
  const source = path ?? `tool?session=${encodeURIComponent(session)}&id=${encodeURIComponent(tool.id)}`
  const request = [open, api, source, tool.id, tool.is_error, tool.status]
  const [previousRequest, setPreviousRequest] = useState(request)
  if (request.some((value, at) => !Object.is(value, previousRequest[at]))) { setPreviousRequest(request); if (open) setError('') }
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    void api<ToolDetail>(source, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setDetail(value); setError(''); setBusy(false) } }).catch(reason => { if (!controller.signal.aborted) { setError(errorText(reason)); setBusy(false) } })
    return () => controller.abort()
  }, [open, api, source, tool.id, tool.is_error, tool.status, attempt])
  return <li className="tool-item">
    <button type="button" className="tool-toggle" aria-expanded={open} aria-controls={id} onClick={() => { setOpen(!open); onOpen() }}>
      {compact
        ? <span className="tool-inline"><strong>{tool.name}</strong>{FILE_TOOLS.has(tool.name) && tool.title ? <code title={tool.title}>{tool.title.split('/').pop()}</code> : <span>{tool.description || tool.title}</span>}</span>
        : <><ToolIcon name={tool.name} failed={!!(tool.is_error || detail?.is_error)} running={live && tool.status === 'pending'} /><span className="tool-text"><strong>{tool.description || tool.name}</strong>{tool.title && tool.title !== tool.description && <span className="tool-title">{tool.title}</span>}</span></>}
      {!compact && <StepTime tool={tool} live={live} />}{(tool.is_error || detail?.is_error) && <span className="tool-error">Error</span>}<ChevronRight className="chevron" size={15} aria-hidden="true" />
    </button>
    {open && <div id={id}>
      {error ? <p role="alert">{error} <RetryButton label="Retry" className="" busy={busy} onRetry={() => { setBusy(true); setAttempt(attempt + 1) }} /></p> : !detail ? <Loading label="Loading tool detail…" /> : <>
        <LiveDetail label={`${tool.name} detail`}><code>{detail.detail.split('\n').map((line, index) => <span key={index} className={detail.format === 'diff' ? line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : '' : ''}>{line}{'\n'}</span>)}</code></LiveDetail>
        {detail.status === 'pending' && <p role="status">Result pending or unavailable.</p>}
        {detail.truncated && <p className="tool-cut">Detail truncated.</p>}
      </>}
    </div>}
  </li>
}

// Images the agent looked at stay visible under the folded tool group. They come from the transcript, not from a file path.
function ToolImages({ tools, session }: { tools: ToolSummary[]; session: string }) {
  const [viewing, setViewing] = useState<{ name: string; url: string } | null>(null)
  const shots = tools.flatMap(tool => Array.from({ length: tool.images ?? 0 }, (_, n) => ({
    name: tool.title.split('/').pop() || 'Image', url: `/mobile/api/tool-image?session=${encodeURIComponent(session)}&id=${encodeURIComponent(tool.id)}&n=${n}` })))
  if (!shots.length) return null
  return <>
    <ul className="tool-images" aria-label="Images">{shots.map(shot => <li key={shot.url}>
      <button type="button" aria-label={`View ${shot.name}`} onClick={() => setViewing(shot)}><img src={shot.url} alt="" loading="lazy" /><span>{shot.name}</span></button>
    </li>)}</ul>
    <ImageViewer image={viewing} close={() => setViewing(null)} items={shots} onSelect={setViewing} />
  </>
}

// Pages Claude published as artifacts in this group. They stay in view when the group folds: they are what the work made.
function ToolArtifacts({ tools }: { tools: ToolSummary[] }) {
  const preview = useContext(PreviewOpener)
  const pages = [...new Map(tools.flatMap(tool => tool.artifact ? [[tool.artifact.url, tool.artifact] as const] : [])).values()]
  if (!pages.length) return null
  return <ul className="tool-artifacts" aria-label="Artifacts">{pages.map(page => <li key={page.url}>
    {preview ? <button type="button" className="artifact-main" onClick={() => preview.open({ path: page.path, title: page.title, remote: page.url })}>
      <AppWindow size={18} aria-hidden="true" /><span><bdi>{page.title}</bdi><small>Claude artifact · Preview</small></span>
    </button> : <span className="artifact-main"><AppWindow size={18} aria-hidden="true" /><span><bdi>{page.title}</bdi><small>Claude artifact</small></span></span>}
    <a className="icon-button" href={page.url} target="_blank" rel="noreferrer" aria-label={`Open ${page.title} on claude.ai`} title="Open on claude.ai"><ExternalLink size={16} /></a>
  </li>)}</ul>
}

const LIVE_STEPS = 4

// A folded group says what the agent did and how long it took. The newest group of a running turn stays open with its
// last steps, so the chat shows the work as it happens, and folds when the turn moves on. A click on the group decides for good.
export function ToolGroup({ id, text, count, tools, api, session, live = false, runningShown = false }: { id: string; text: string; count?: unknown; tools?: ToolSummary[]; api: Api; session: string; live?: boolean; runningShown?: boolean }) {
  const [choice, setChoice] = useState<boolean | null>(null)
  const panel = useId()
  const all = tools ?? []
  const total = all.length || Number(count) || text.split('\n').filter(Boolean).length || 1
  const following = live && choice === null
  const open = choice ?? live
  const { steps, offset, earlier } = following ? liveTail(all, runningShown, LIVE_STEPS) : { steps: all, offset: 0, earlier: 0 }
  // The working line under the chat already names the one running step.
  if (following && all.length === 1 && !steps.length) return null
  const seconds = groupSeconds(all)
  const slowest = slowestStep(all)
  const failed = all.filter(tool => tool.is_error).length
  const headline = all.length ? toolHeadline(all) : `Used ${total} ${total === 1 ? 'tool' : 'tools'}`
  return <div className="tool-row tool-group" data-msg-id={id}>
    <button type="button" className="tool-toggle" aria-expanded={open} aria-controls={panel} onClick={() => setChoice(!open)}>
      <Code2 size={15} aria-hidden="true" />
      <span className="tool-text"><span className="tool-headline"><span className="tool-headline-text">{headline}</span>{seconds !== null && seconds >= 1 && <small className="tool-time">{formatElapsed(seconds)}</small>}{failed > 0 && <span className="tool-error">{failed} failed</span>}</span>
        {slowest && <span className="tool-slowest">Longest: {slowest.text} · {formatElapsed(slowest.seconds)}</span>}</span>
      <ChevronRight className="chevron" size={15} aria-hidden="true" />
    </button>
    {open && <div id={panel}>{all.length ? <>
      {earlier > 0 && <button type="button" className="tool-earlier" onClick={() => setChoice(true)}>{earlier} earlier {earlier === 1 ? 'step' : 'steps'}</button>}
      {steps.length > 0 && <ul className="tool-list">{steps.map((tool, index) => <ToolRow key={`${tool.id}:${index + offset}`} tool={tool} api={api} session={session} live={live} onOpen={() => setChoice(true)} />)}</ul>}
    </> : <pre tabIndex={0}>{text}</pre>}</div>}
    {tools && <ToolImages tools={tools} session={session} />}
    {tools && <ToolArtifacts tools={tools} />}
  </div>
}
