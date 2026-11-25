import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Blocks, CalendarClock, Check, ChevronRight, Copy, Folder, Gauge, Layers, ListTodo, Pencil, RefreshCw, Sparkles, Target, X } from 'lucide-react'
import { errorText, goalStatusLabel } from './api'
import type { Api, Footer, Pane, TabBrief, TabBriefLink } from './api'
import { useModal, usePolling } from './hooks'
import { agentNames, chatTitle } from './Sidebar'
import type { Session } from './Sidebar'
import { State } from './NewSession'
import { taskLabel } from './Dock'
import { copyText } from './clipboard'
import { profileName } from './provider'
import './SessionDetails.css'
import { fmtDateTime, useTetherZone } from './zone'

// Same limits as server/tab_brief.py and the pane-name endpoint.
const DESCRIPTION_LIMIT = 300
const NAME_LIMIT = 40

export interface SessionDetailsActions {
  onFiles: (folder: string) => void
  onModel?: () => void  // undefined while the agent works or the tab is read-only: the model picker would refuse
  onUsage?: () => void
  onLive: (show: 'goal' | 'tasks' | 'crons') => void
  onTools?: () => void
  onBacklog: (query: string) => void
  onRenamed: () => void
}
interface Props extends SessionDetailsActions {
  api: Api; pane: Pane; session?: Session; stack: string; footer: Footer; mode: string; model: string; context?: string; state: string; thread: string | null; onClose: () => void
}
interface BriefReply { brief: TabBrief | null; enabled?: boolean; backlog?: TabBriefLink[] }

const ago = (seconds?: number | null) => {
  if (!seconds) return ''
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60))
  return minutes < 1 ? 'Just now' : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min ago` : `${Math.floor(minutes / 1440)} d ago`
}
const BRIEF_MODEL: Partial<Record<Pane['agent'], string>> = { claude: 'Claude Haiku', codex: 'the fast model of your ChatGPT plan' }

// One line of the drawer: a label over a value. With onClick the whole line is one button; with copy, a copy button sits at the end.
function Row({ label, children, onClick, copy, icon, hint }: { label: string; children: ReactNode; onClick?: () => void; copy?: string; icon?: ReactNode; hint?: string }) {
  const body = <>{icon && <span className="sd-row-icon" aria-hidden="true">{icon}</span>}<span className="sd-row-text"><small>{label}</small><span className="sd-row-value">{children}</span></span></>
  if (onClick) return <li><button type="button" className="sd-row sd-action" onClick={onClick} title={hint}>{body}<ChevronRight size={16} className="sd-chevron" aria-hidden="true" /></button></li>
  return <li className="sd-row">{body}{copy && <CopyValue text={copy} label={`Copy ${label.toLowerCase()}`} />}</li>
}
function CopyValue({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState<'' | 'ok' | 'fail'>('')
  useEffect(() => { if (!done) return; const timer = setTimeout(() => setDone(''), 1500); return () => clearTimeout(timer) }, [done])
  return <button type="button" className="sd-copy" aria-label={done === 'ok' ? 'Copied' : done === 'fail' ? 'Could not copy' : label} title={label}
    onClick={() => copyText(text).then(() => setDone('ok'), () => setDone('fail'))}>{done === 'ok' ? <Check size={16} /> : <Copy size={16} />}</button>
}
function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = `sd-${title.toLowerCase()}`
  return <section className="sd-section" aria-labelledby={id}><h3 id={id}>{title}</h3>{children}</section>
}

// The tab's details, in the Settings style: what the tab is for, where it runs, the agent, its work and its tools.
export function SessionDetails({ api, pane, session, stack, footer, mode, model, context, state, thread, onClose, onFiles, onModel, onUsage, onLive, onTools, onBacklog, onRenamed }: Props) {
  useTetherZone()
  const modal = useModal(true, onClose)
  const [busy, setBusy] = useState<'' | 'write' | 'save' | 'name'>('')
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [editing, setEditing] = useState<'' | 'name' | 'description'>('')
  const [draft, setDraft] = useState('')
  // After an edit closes (saved or cancelled), focus returns to the button that opened it.
  const lastEdit = useRef<'' | 'name' | 'description'>('')
  useEffect(() => {
    if (!editing && lastEdit.current) document.querySelector<HTMLElement>(`.session-details [data-sd-edit="${lastEdit.current}"]`)?.focus()
    lastEdit.current = editing
  }, [editing])
  const briefed = pane.agent === 'claude' || pane.agent === 'codex'
  // Shells have no brief. A brief that is being written shows up on the next look, 4 seconds later at most.
  const fetcher = useCallback((signal: AbortSignal) => briefed
    ? api<BriefReply>(`tab-brief?pane=${encodeURIComponent(pane.id)}${thread ? `&thread=${encodeURIComponent(thread)}` : ''}`, undefined, signal)
    : Promise.resolve<BriefReply>({ brief: null }), [api, briefed, pane.id, thread])
  const { data: reply, error: loadError, refresh } = usePolling(fetcher, 4000)
  const brief = reply ? reply.brief : pane.brief ?? null
  const running = brief?.state === 'running'

  const title = chatTitle(pane, session)
  const enabled = reply?.enabled !== false && brief?.state !== 'off'
  const foundry = pane.agent === 'codex' && pane.codex_profile === 'foundry'
  const canWrite = briefed && !!thread && enabled && !foundry && !running && !busy
  const description = brief?.description ?? ''
  const nameByAi = !!brief?.name && pane.pane_name === brief.name

  async function write() {
    setBusy('write'); setError(''); setStatus('Writing the name and description…')
    try {
      const data = await api<{ brief: TabBrief | null }>('tab-brief', { pane: pane.id, thread, action: 'write' }, undefined, undefined, 90000)
      await refresh()
      if (data.brief?.state === 'failed' || data.brief?.state === 'off') { setStatus(''); setError(data.brief.error || 'Could not write a description.') }
      else { setStatus('Description written.'); onRenamed() }
    } catch (failure) { setStatus(''); setError(errorText(failure)) } finally { setBusy('') }
  }
  async function saveDescription(text: string) {
    setBusy('save'); setError(''); setStatus('')
    try {
      await api('tab-brief', { pane: pane.id, thread, action: 'save', description: text }); await refresh(); setEditing(''); setStatus(text.trim() ? 'Description saved.' : 'Description cleared.')
    } catch (failure) { setError(errorText(failure)) } finally { setBusy('') }
  }
  async function saveName() {
    setBusy('name'); setError(''); setStatus('')
    try {
      const saved = await api<{ note?: string }>('pane-name', { pane: pane.id, name: draft.trim() })
      setEditing(''); onRenamed()
      if (saved.note) setError(`Saved in Tether. ${saved.note}`); else setStatus(draft.trim() ? 'Name saved.' : 'Name cleared.')
    } catch (failure) { setError(errorText(failure)) } finally { setBusy('') }
  }
  const edit = (what: 'name' | 'description') => { setEditing(what); setError(''); setStatus(''); setDraft(what === 'name' ? pane.pane_name ?? '' : description) }

  const crumbs = [session?.name, stack, title].filter((part, index, list) => part && list.indexOf(part) === index)
  const tasks = footer.tasks ?? []
  const background = (footer.agents ?? []).filter(agent => !agent.main)
  const links = reply?.backlog ?? []
  const provider = pane.agent === 'codex' ? pane.codex_profile ? profileName(pane.codex_profile) : 'ChatGPT plan' : ''
  const statusParts = (footer.status ?? '').split('|').map(part => part.trim()).filter(Boolean)
  const usageLabel = pane.agent === 'claude' ? 'Plan usage' : 'Usage and credits'
  const disclosure = foundry ? 'Tether does not write descriptions for Foundry tabs.'
    : !enabled ? (brief?.error || 'AI descriptions are off: Settings › Models & helpers › Main AI model is Local only.')
    : `Once per chat, Tether sends the first two prompts and replies, shortened, to ${BRIEF_MODEL[pane.agent] ?? 'a small model'} to write the name and description.`

  return <dialog {...modal} className="sheet session-details" aria-label={`Session details: ${title}`} aria-describedby={description ? 'sd-description' : undefined}>
    <header className="sd-header">
      <div className="sd-heading">
        <span className="sd-eyebrow">Session details</span>
        {editing === 'name'
          ? <form className="sd-edit" onSubmit={event => { event.preventDefault(); void saveName() }}>
              <label htmlFor="sd-name-input" className="sd-sr">Tab name</label>
              <input id="sd-name-input" autoFocus value={draft} maxLength={NAME_LIMIT} autoComplete="off" disabled={busy === 'name'} placeholder={chatTitle({ ...pane, pane_name: '' }, session)} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setEditing('') } }} />
              <div className="sd-edit-buttons"><button type="button" className="secondary" disabled={busy === 'name'} onClick={() => setEditing('')}>Cancel</button><button type="submit" disabled={busy === 'name'}>{busy === 'name' ? 'Saving…' : 'Save name'}</button></div>
            </form>
          : <div className="sd-title-line"><h2 id="sd-title"><bdi>{title}</bdi></h2><button type="button" className="icon-button sd-small-button" data-sd-edit="name" aria-label="Rename this tab" title="Rename this tab" onClick={() => edit('name')}><Pencil size={16} /></button></div>}
        <p className="sd-sub"><State state={state} />{nameByAi && <span className="sd-tag"><Sparkles size={12} aria-hidden="true" />Named by AI</span>}</p>
      </div>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
    </header>

    <div className="sd-body">
      <p className="sd-sr" role="status">{status}</p>
      {error && <p className="sd-error" role="alert">{error}</p>}

      {briefed && <Section title="Overview">
        <div className="sd-brief">
          {editing === 'description'
            ? <form className="sd-edit" onSubmit={event => { event.preventDefault(); void saveDescription(draft) }}>
                <label htmlFor="sd-description-input">Description</label>
                <textarea id="sd-description-input" autoFocus rows={3} value={draft} maxLength={DESCRIPTION_LIMIT} disabled={busy === 'save'} aria-describedby="sd-description-count" onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setEditing('') } }} />
                <small id="sd-description-count" className="sd-count">{draft.length} / {DESCRIPTION_LIMIT}</small>
                <div className="sd-edit-buttons"><button type="button" className="secondary" disabled={busy === 'save'} onClick={() => setEditing('')}>Cancel</button><button type="submit" disabled={busy === 'save'}>{busy === 'save' ? 'Saving…' : 'Save'}</button></div>
              </form>
            : <>
                {description ? <p id="sd-description" className="sd-description">{description}</p>
                  : running || busy === 'write' ? <p className="sd-placeholder" aria-busy="true"><RefreshCw size={14} className="spin" aria-hidden="true" />Writing a description…</p>
                  : !reply && !loadError ? <p className="sd-placeholder" aria-busy="true">Loading the description…</p>
                  : <p className="sd-placeholder">No description yet.</p>}
                {description && <p className="sd-meta">{brief?.description_by === 'user' ? 'Written by you' : 'Written by AI'}{brief?.at ? ` · ${ago(brief.at).toLowerCase()}` : ''}</p>}
                {brief?.state === 'failed' && !busy && <p className="sd-error" role="alert">Could not write a description: {brief.error || 'unknown error'}</p>}
                {loadError && <p className="sd-error" role="alert">Could not load the description: {loadError} <button type="button" className="sd-link" onClick={() => void refresh()}>Try again</button></p>}
                <div className="sd-brief-actions">
                  <button type="button" className="secondary" data-sd-edit="description" disabled={!!busy || running} onClick={() => edit('description')}><Pencil size={15} />{description ? 'Edit' : 'Write it yourself'}</button>
                  {description && <button type="button" className="secondary" disabled={!!busy || running} onClick={() => void saveDescription('')}><X size={15} />Clear</button>}
                  {thread && !foundry && enabled && <button type="button" className="secondary" disabled={!canWrite} aria-busy={busy === 'write' || running} onClick={() => void write()}><Sparkles size={15} />{busy === 'write' || running ? 'Writing…' : description ? 'Write again with AI' : 'Write with AI'}</button>}
                </div>
                <p className="sd-note">{disclosure}</p>
              </>}
        </div>
        {footer.goal && <ul className="sd-rows"><Row label="Goal" icon={<Target size={16} />} onClick={() => onLive('goal')}>{footer.goal_status ? goalStatusLabel(footer.goal_status) : `Active${footer.goal !== 'active' ? ` · ${footer.goal}` : ''}`}</Row></ul>}
      </Section>}

      <Section title="Where">
        <ul className="sd-rows">
          {crumbs.length > 1 && <Row label={stack ? 'Session › stack › tab' : 'Session › tab'}><bdi>{crumbs.join(' › ')}</bdi></Row>}
          {pane.dir && <Row label="Folder" icon={<Folder size={16} />} onClick={() => onFiles(pane.dir)} hint="Open in Files"><bdi dir="ltr" className="sd-mono">{pane.dir}</bdi></Row>}
          {pane.worktree && <Row label="Worktree branch" copy={pane.worktree}><bdi dir="ltr" className="sd-mono">{pane.worktree}</bdi></Row>}
          <Row label="Pane" copy={pane.id}><span className="sd-mono">{pane.id}</span>{pane.label && pane.label !== pane.id && <span className="sd-muted"> · {pane.label}</span>}</Row>
        </ul>
      </Section>

      {pane.agent !== 'shell' && <Section title="Agent">
        <ul className="sd-rows">
          <Row label="Agent">{agentNames[pane.agent]}{provider && <span className="sd-muted"> · {provider}</span>}</Row>
          {model && <Row label="Model and effort" onClick={onModel} hint={onModel ? 'Change the model' : undefined}>
            {model}{footer.effort && <span className="sd-muted"> · {footer.effort}</span>}
            {footer.tier && <span className="sd-muted"> · {footer.tier}</span>}
          </Row>}
          {!model && footer.effort && <Row label="Effort">{footer.effort}</Row>}
          {pane.agent === 'claude' && (footer.mode || footer.observed) && <Row label="Mode">{mode}</Row>}
          {context && <Row label="Context used"><span className={Number(context) >= 80 ? 'sd-warn' : ''}>{context}%</span></Row>}
          {onUsage && <Row label={usageLabel} icon={<Gauge size={16} />} onClick={onUsage}>See limits and resets</Row>}
          {pane.agent === 'claude' && statusParts.length > 1 && <Row label="Status line">{statusParts.join(' · ')}</Row>}
        </ul>
      </Section>}

      {(tasks.length > 0 || !!footer.crons?.length || background.length > 0 || !!pane.finished || links.length > 0) && <Section title="Activity">
        <ul className="sd-rows">
          {tasks.length > 0 && <Row label="Background tasks" icon={<Layers size={16} />} onClick={() => onLive('tasks')}>{tasks.map(task => taskLabel(task, footer.task_details)).join(', ')}</Row>}
          {!!footer.crons?.length && <Row label="Scheduled jobs" icon={<CalendarClock size={16} />} onClick={() => onLive('crons')}>{footer.crons.map(job => `${job.label} · ${job.every}`).join(', ')}</Row>}
          {background.map((agent, index) => <Row key={index} label="Background agent">{[agent.name, agent.task, agent.detail].filter(Boolean).join(' · ')}</Row>)}
          {!!pane.finished && <Row label="Last reply"><time dateTime={new Date(pane.finished * 1000).toISOString()} title={fmtDateTime(pane.finished * 1000)}>{ago(pane.finished)}</time></Row>}
          {links.map(item => <Row key={item.id} label={`Backlog ${item.id} · ${item.state === 'doing' ? 'In progress' : item.state === 'done' ? 'Done' : 'Added here'}`} icon={<ListTodo size={16} />} onClick={() => onBacklog(item.id)} hint="Open in the backlog">{item.title}</Row>)}
        </ul>
      </Section>}

      {(footer.ide || onTools || pane.agent_name) && <Section title="Tools">
        <ul className="sd-rows">
          {footer.ide && <Row label="IDE context">{footer.ide}</Row>}
          {onTools && <Row label="Skills, plugins, MCP" icon={<Blocks size={16} />} onClick={onTools}>See what this agent can use</Row>}
          <Row label="Tab name" copy={title}><bdi>{title}</bdi></Row>
          {pane.agent_name && pane.agent_name !== title && <Row label={`${agentNames[pane.agent]} session name`} copy={pane.agent_name}><bdi>{pane.agent_name}</bdi></Row>}
        </ul>
      </Section>}
    </div>
  </dialog>
}
