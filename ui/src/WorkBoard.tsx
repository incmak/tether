import { useEffect, useId, useState } from 'react'
import type { FormEvent } from 'react'
import { Plus, Send, Settings2, SquareKanban, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, Pane } from './api'
import { RetryButton } from './Retry'
import { Bone, Skeleton } from './Skeleton'
import { useNow } from './attention'
import { useModal } from './hooks'
import { age } from './needs'
import { STAGE_LABEL, decisions, moveItem, moveJira, onBoard, projectHue, setItemQa, startFix } from './work'
import type { JiraLink, Stage, WorkBoard, WorkItem, WorkProject } from './work'
import { DecisionCard, DecisionsGroup } from './WorkItems'
import { NewItemForm, ProjectForm } from './WorkForms'
import type { DecisionAnswers } from './WorkItems'
import './Work.css'

// The work page: a Needs you list and the board. The board has one column per stage; a card opens the item's
// drawer (stage track, question, tabs, Jira flag, links, history). Answers share App's answer state, so a result
// shows the same here, on Home and in the Needs you panel.

const COLUMNS: { stage: Stage; hint: string }[] = [
  { stage: 'triage', hint: 'agent checks it' },
  { stage: 'needs', hint: 'your call' },
  { stage: 'ready', hint: 'approved, not started' },
  { stage: 'building', hint: 'fix tab works' },
  { stage: 'review', hint: 'reviewer + checks' },
  { stage: 'done', hint: 'merged and proven' },
]
const HOLD = { stage: 'hold' as Stage, hint: 'parked by you' }
const TRACK: Stage[] = ['triage', 'needs', 'ready', 'building', 'review', 'done']

export type WorkView = 'needs' | 'board'
export type WorkLinks = { open: number; asking: number; marks: Record<string, { id: string; stage: Stage }>; onOpen: () => void }

/** The sidebar's way in: under Backlog, with the count of open items (not Done, not on hold). */
export function WorkButton({ links }: { links: WorkLinks }) {
  const label = ['Work board', links.open && `${links.open} open`, links.asking && `${links.asking} need you`].filter(Boolean).join(', ')
  return <button type="button" className="sidebar-backlog" aria-label={label} aria-haspopup="dialog" onClick={links.onOpen}>
    <SquareKanban size={16} aria-hidden="true" /><span>Work board</span>
    {links.asking > 0 && <span className="sidebar-backlog-new">{links.asking} need you</span>}
    {links.open > 0 && <span className="sidebar-backlog-count">{links.open}</span>}
  </button>
}

/** A tab's item in the sidebar: "T-21 · Building". */
export function WorkMark({ mark }: { mark?: { id: string; stage: Stage } }) {
  if (!mark) return null
  return <span className={`work-mark ${mark.stage}`} title={`Works on ${mark.id}: ${STAGE_LABEL[mark.stage]}`}>
    <span className="visually-hidden">, item </span>{mark.id} · {STAGE_LABEL[mark.stage]}
  </span>
}
type Props = {
  api: Api; board?: WorkBoard; error: string; panes: Pane[]; answers: DecisionAnswers
  onRefresh: () => unknown; onOpen: (pane: Pane) => void; onClose: () => void; start?: WorkView
}

const prefixOf = (board: WorkBoard | undefined, item: WorkItem) => board?.projects.find(project => project.name === item.project)?.prefix ?? item.id.split('-')[0]
// Newest activity first; in Needs you, the oldest question first (it has waited longest).
const order = (stage: Stage) => (a: WorkItem, b: WorkItem) => stage === 'needs' ? (a.ask?.at ?? a.updated) - (b.ask?.at ?? b.updated) : b.updated - a.updated
const tabPath = (tab: WorkItem['tabs'][number]) => [tab.session, tab.stack, tab.name || tab.pane].filter(Boolean).join(' › ')

export function WorkSheet({ api, board, error, panes, answers, onRefresh, onOpen, onClose, start }: Props) {
  const modal = useModal(true, onClose, undefined, { view: start ? `work:${start}` : 'work' })
  const now = useNow()
  const [view, setView] = useState<WorkView>(() => start ?? (decisions(board).length ? 'needs' : 'board'))
  const [chosen, setProject] = useState('all')
  const [showHold, setShowHold] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  // A form over the sheet: add a project, a project's settings, or a new item. added: the last result line.
  const [form, setForm] = useState<{ kind: 'project'; name?: string } | { kind: 'item' } | null>(null)
  const [added, setAdded] = useState<{ text: string; ok: boolean } | null>(null)
  // A reply closes only the form that sent it: Mak can cancel and open another form while a save runs.
  const sent = form
  const close = (mine: typeof form) => setForm(current => current === mine ? null : current)
  const projects = board?.projects ?? []
  const project = projects.some(entry => entry.name === chosen) ? chosen : 'all'
  // Settings: the chosen project, or the only one.
  const settingsFor = project !== 'all' ? project : projects.length === 1 ? projects[0].name : ''
  const editing = form?.kind === 'project' && form.name ? projects.find(entry => entry.name === form.name) : undefined
  const items = (board?.items ?? []).filter(item => onBoard(item, now) && (project === 'all' || item.project === project))
  const shown = board && { ...board, items }
  const asking = decisions(shown).length
  const holds = items.filter(item => item.stage === 'hold').length
  const opened = board?.items.find(item => item.id === openId)
  const open = (pane: Pane) => { onClose(); onOpen(pane) }
  const columns = showHold ? [...COLUMNS, HOLD] : COLUMNS
  return <dialog {...modal} className="sheet work-sheet" aria-labelledby="work-title">
    <div className="sheet-heading work-head">
      <h2 id="work-title">Work</h2>
      <div className="tab-bar work-views" role="group" aria-label="View">
        <button type="button" aria-pressed={view === 'needs'} onClick={() => setView('needs')}>Needs you{asking > 0 && <span className="badge accent">{asking}</span>}</button>
        <button type="button" aria-pressed={view === 'board'} onClick={() => setView('board')}>Board</button>
      </div>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>
    </div>
    {board && <div className="work-filters">
      <div className="tab-bar work-projects" role="group" aria-label="Project">
        {projects.length > 1 && <>
        <button type="button" aria-pressed={project === 'all'} onClick={() => setProject('all')}>All projects</button>
        {projects.map(entry => {
          const count = (board?.items ?? []).filter(item => item.project === entry.name && item.stage === 'needs').length
          return <button key={entry.name} type="button" aria-pressed={project === entry.name} onClick={() => setProject(entry.name)}>
            <span className={`work-swatch hue-${projectHue(entry.prefix)}`} aria-hidden="true" />{entry.name}
            {count > 0 && <span className="work-chip-count"> · {count}<span className="visually-hidden"> need you</span></span>}
          </button>
        })}
        </>}
        <button type="button" className="work-add-project" aria-haspopup="dialog" onClick={() => setForm({ kind: 'project' })}><Plus size={14} aria-hidden="true" />Add project</button>
      </div>
      <div className="work-tools">
        {settingsFor && <button type="button" className="ghost sm" aria-haspopup="dialog" onClick={() => setForm({ kind: 'project', name: settingsFor })}>
          <Settings2 size={14} aria-hidden="true" />Settings<span className="visually-hidden"> of {settingsFor}</span></button>}
        {view === 'board' && projects.length > 0 && <button type="button" className="ghost sm work-hold" onClick={() => setShowHold(on => !on)}>{showHold ? 'Hide' : 'Show'} on hold ({holds})</button>}
        {projects.length > 0 && <button type="button" className="secondary sm" aria-haspopup="dialog" onClick={() => setForm({ kind: 'item' })}><Plus size={14} aria-hidden="true" />New item</button>}
      </div>
    </div>}
    <div className="work-body">
      {!board && !error && <BoardBones />}
      {!board && error && <p className="work-error" role="alert"><span>Could not load work items. {error}</span><RetryButton onRetry={onRefresh} className="ghost sm" /></p>}
      {board && <div role="status" className="work-added">{added && <p className={`work-result${added.ok ? '' : ' failed'}`}>{added.text}</p>}</div>}
      {board && !projects.length && <div className="work-empty work-start">
        <p>No projects yet. Add a project: its items, sessions and Jira key live here.</p>
        <button type="button" className="primary" aria-haspopup="dialog" onClick={() => setForm({ kind: 'project' })}>Add project</button>
      </div>}
      {board && view === 'needs' && projects.length > 0 && <>
        <DecisionsGroup board={shown} error={error} panes={panes} answers={answers} onRefresh={onRefresh} onOpen={open} compact scope="sheet" onDetails={item => setOpenId(item.id)} />
        {!asking && <p className="work-empty">Nothing waits on you. An item shows here when its agent asks you something.</p>}
      </>}
      {board && view === 'board' && projects.length > 0 && <>
        <div role="status">{answers.results.map(result => <p key={result.id} className={`work-result${result.ok ? '' : ' failed'}`}>{result.text}</p>)}</div>
        {error && <p className="work-error" role="alert"><span>Could not refresh work items. The board can be out of date. {error}</span><RetryButton onRetry={onRefresh} className="ghost sm" /></p>}
        {projects.length > 0 && !board.items.length && <p className="work-empty">No work items yet. Use New item, or an agent adds one with <code>tether item new</code>.</p>}
        <div className="work-columns">
          {columns.map(column => {
            const list = items.filter(item => item.stage === column.stage).sort(order(column.stage))
            return <section key={column.stage} className={`work-col ${column.stage}`} aria-labelledby={`work-col-${column.stage}`}>
              <h3 id={`work-col-${column.stage}`} className="work-col-head"><span>{STAGE_LABEL[column.stage]}</span><span className="badge">{list.length}</span><span className="work-col-hint">{column.hint}</span></h3>
              {list.length ? <ul className="work-col-list">{list.map(item => item.stage === 'needs' && item.ask
                ? <DecisionCard key={`${item.id}:${item.ask.id}`} scope="board" item={item} prefix={prefixOf(board, item)} panes={panes} busy={answers.locked(item)} onAnswer={answers.answer} onOpen={open} onDetails={entry => setOpenId(entry.id)} />
                : <li key={item.id}><BoardCard item={item} prefix={prefixOf(board, item)} panes={panes} now={now} onDetails={() => setOpenId(item.id)} /></li>)}</ul>
                : <p className="work-col-empty">Nothing here.</p>}
            </section>
          })}
        </div>
      </>}
    </div>
    {opened && <ItemDrawer key={opened.id} api={api} item={opened} project={projects.find(entry => entry.name === opened.project)} prefix={prefixOf(board, opened)} panes={panes} link={board?.jira}
      answers={answers} error={error} onRefresh={onRefresh} onOpen={open} onClose={() => setOpenId(null)} />}
    {board && form?.kind === 'project' && (!form.name || editing) && <ProjectForm api={api} board={board} project={editing} onClose={() => setForm(null)}
      onSaved={name => { close(sent); setProject(name); setAdded({ ok: true, text: editing ? `${name} settings saved.` : `Project ${name} added.` }); void onRefresh() }} />}
    {board && form?.kind === 'item' && <NewItemForm api={api} board={board} project={project === 'all' ? undefined : project} onClose={() => setForm(null)}
      onAdded={(item, tab) => {
        close(sent); setView('board')
        if (project !== 'all') setProject(item.project)
        const started = !tab ? '' : tab.started ? ' Triage tab started.' : ` Triage tab not started: ${tab.why}`
        setAdded({ ok: !tab || tab.started, text: `${item.id} added to Triage.${started}` })
        void onRefresh()
      }} />}
  </dialog>
}

function BoardBones() {
  return <Skeleton label="Loading work items" className="work-columns">
    {COLUMNS.slice(0, 3).map(column => <div key={column.stage} className="work-col" aria-hidden="true">
      <div className="work-col-head"><Bone w="40%" /></div>
      {[0, 1].map(index => <div key={index} className="work-bcard sk"><Bone w="50%" /><Bone w="86%" /><Bone w="64%" /></div>)}
    </div>)}
  </Skeleton>
}

function BoardCard({ item, prefix, panes, now, onDetails }: { item: WorkItem; prefix: string; panes: Pane[]; now: number; onDetails: () => void }) {
  const live = new Set(panes.map(pane => pane.id))
  const links = item.tabs.length > 0 || item.refs.length > 0 || (item.qa && item.stage !== 'done') || !!item.jira
  return <button type="button" className="work-bcard" data-item={item.id} aria-haspopup="dialog" onClick={onDetails}>
    <span className="home-card-top">
      <span className={`work-swatch hue-${projectHue(prefix)}`} aria-hidden="true" />
      <span className="work-id">{item.id}</span>
      <span className="home-source">{item.project}</span>
      <span className="work-time" title="Last change">{age(item.updated, now)}</span>
    </span>
    <span className="work-title">{item.title}</span>
    {links && <span className="work-links">
      {item.tabs.map(tab => <span key={tab.pane} className="work-link-tab"><span className={`work-tab-dot${live.has(tab.pane) ? '' : ' closed'}`} aria-hidden="true" />{tabPath(tab)}{!live.has(tab.pane) && <span className="visually-hidden"> (closed)</span>}</span>)}
      {item.refs.length > 0 && <span>{item.refs.join(', ')}</span>}
      {item.qa && item.stage !== 'done' && <span>needs QA</span>}
      {item.jira?.status && <span className={item.jira.status === 'failed' || item.jira.status === 'off' ? 'work-link-error' : undefined}>{JIRA_CARD[item.jira.status] ?? 'Jira'}</span>}
    </span>}
  </button>
}

// The card's one-word Jira result. The drawer gives the details and Move again.
const JIRA_CARD: Record<string, string> = { moved: 'Jira moved', owners: 'Jira: owner moves it', failed: 'Jira move failed', off: 'Jira not connected' }

/** What Done does, or did, to the item's Jira tickets. Only tickets assigned to the connected account move. */
function JiraState({ item, project, link }: { item: WorkItem; project?: WorkProject; link?: JiraLink }) {
  if (!project?.jira) return <p className="work-note">This project has no Jira project key.</p>
  const jira = item.jira
  const target = item.qa ? project.qaStatus || 'its QA status' : 'Done'
  if (item.stage !== 'done' || !jira?.status) {
    return <>
      <p className="work-note">On Done, Tether moves this item's {project.jira} tickets that are assigned to you to {target}.</p>
      {link?.why && <p className="work-note failed">{link.why}</p>}
    </>
  }
  return <>
    {!!jira.tickets?.length && <p className="work-note">Moved to {jira.target || target}: {jira.tickets.join(', ')}.</p>}
    {jira.owners?.map(owner => <p key={owner} className="work-note">{owner}</p>)}
    {jira.error && <p className="work-note failed">{jira.status === 'off' ? jira.error : `Jira move failed: ${jira.error}`}</p>}
  </>
}

const when = (at: number) => new Date(at * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const isUrl = (ref: string) => /^https?:\/\//.test(ref)

function ItemDrawer({ api, item, project, prefix, panes, link, answers, error, onRefresh, onOpen, onClose }: {
  api: Api; item: WorkItem; project?: WorkProject; prefix: string; panes: Pane[]; link?: JiraLink; answers: DecisionAnswers; error: string
  onRefresh: () => unknown; onOpen: (pane: Pane) => void; onClose: () => void
}) {
  const modal = useModal(true, onClose)
  const id = useId()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null)
  // After a 409 the drawer stays locked until the board shows a newer version of the item.
  const [stale, setStale] = useState<number | null>(null)
  const ask = item.stage === 'needs' ? item.ask : null
  // A typed reply belongs to the question it answers. A new question starts with an empty box.
  const [draft, setDraft] = useState({ ask: '', text: '' })
  const reply = ask && draft.ask === ask.id ? draft.text : ''
  const locked = answers.locked(item) || busy || stale === item.version
  const result = answers.results.find(entry => entry.id === item.id)
  const at = TRACK.indexOf(item.stage)
  useEffect(() => {
    // Focus the first main action, else the close button. The dialog opens in useModal's effect, which runs first.
    const dialog = modal.ref.current
    ;(dialog?.querySelector<HTMLElement>('[data-first]') ?? dialog?.querySelector<HTMLElement>('.work-drawer-close'))?.focus()
  }, [modal.ref])
  const write = async (done: string, run: () => Promise<unknown>) => {
    setBusy(true); setNote(null)
    try { await run(); setNote({ text: done, ok: true }) }
    catch (failure) {
      const changed = failure instanceof ApiError && failure.status === 409
      if (changed) setStale(item.version)
      setNote({ ok: false, text: changed ? `${item.id} changed. Read it again, then try again.` : errorText(failure) })
    }
    finally { setBusy(false); void onRefresh() }
  }
  const move = (stage: Stage, done: string) => void write(done, () => moveItem(api, item, stage))
  const fix = () => void write(`${item.id}: fix tab started.`, async () => {
    const done = await startFix(api, item)
    if (!done.fix.started) throw new Error(`Fix tab not started: ${done.fix.why}`)
  })
  const send = async (event: FormEvent) => {
    event.preventDefault()
    if (!ask || !reply.trim()) return
    const sent = ask.id
    if (await answers.answer(item, { text: reply.trim() })) setDraft(current => current.ask === sent ? { ask: '', text: '' } : current)
  }
  const again = () => void write(`${item.id}: Jira move started. The result shows here in a few seconds.`, async () => {
    const done = await moveJira(api, item)
    if (!done.queued) throw new Error(done.why)
  })
  const live = (pane: string) => panes.find(entry => entry.id === pane)
  const firstMove = !ask && item.stage === 'review'
  // A Ready item can start a fix tab unless one still runs an agent (the Build answer tries first). Matches the server.
  const canFix = item.stage === 'ready' && !item.tabs.some(tab => tab.role === 'fix' && live(tab.pane) && live(tab.pane)!.agent !== 'shell')
  return <dialog {...modal} className="work-drawer" aria-labelledby={`${id}-title`}>
    <div className="work-drawer-head">
      <div>
        <span className="home-card-top">
          <span className={`work-swatch hue-${projectHue(prefix)}`} aria-hidden="true" />
          <span className="work-id">{item.id}</span>
          <span className="work-drawer-meta">{item.project} · {STAGE_LABEL[item.stage]}</span>
        </span>
        <h3 id={`${id}-title`}>{item.title}</h3>
      </div>
      <button type="button" className="icon-button work-drawer-close" aria-label="Close item" onClick={onClose}><X /></button>
    </div>
    <div className="work-drawer-body">
      <ol className="work-track" aria-label="Stage">
        {TRACK.map((stage, index) => <li key={stage} className={stage === item.stage ? 'on' : index < at ? 'past' : undefined} aria-current={stage === item.stage ? 'step' : undefined}>{STAGE_LABEL[stage]}</li>)}
        {item.stage === 'hold' && <li className="on" aria-current="step">On hold</li>}
      </ol>
      <div role="status" className="work-drawer-status">
        {note && <p className={`work-result${note.ok ? '' : ' failed'}`}>{note.text}</p>}
        {result && <p className={`work-result${result.ok ? '' : ' failed'}`}>{result.text}</p>}
      </div>
      {error && <p className="work-error" role="alert"><span>Could not refresh work items. This item can be out of date. {error}</span><RetryButton onRetry={onRefresh} className="ghost sm" /></p>}
      {ask && <section className="work-block" aria-labelledby={`${id}-call`}>
        <h4 id={`${id}-call`}>Your call</h4>
        <p className="work-q">{ask.q}</p>
        <div className="work-actions" role="group" aria-label={`Answer ${item.id}`}>
          {ask.options.map((option, index) => <button key={option} type="button" className={index === 0 ? 'primary' : 'secondary'} data-first={index === 0 ? '' : undefined} disabled={locked}
            onClick={() => void answers.answer(item, { choice: option })}>{option}</button>)}
        </div>
        <form className="work-reply" onSubmit={event => void send(event)}>
          <label className="visually-hidden" htmlFor={`${id}-reply`}>Reply to {item.id}</label>
          <input id={`${id}-reply`} value={reply} onChange={event => setDraft({ ask: ask.id, text: event.target.value })} placeholder="Or reply in words: it goes to the tab" maxLength={4000} />
          <button type="submit" className="icon-button" aria-label="Send reply" title="Send reply" disabled={locked || !reply.trim()}><Send size={16} /></button>
        </form>
      </section>}
      {item.stage !== 'done' && item.stage !== 'needs' && <section className="work-block" aria-labelledby={`${id}-move`}>
        <h4 id={`${id}-move`}>Move</h4>
        <div className="work-actions">
          {item.stage === 'review' && <button type="button" className="primary" data-first="" disabled={locked} onClick={() => move('done', `${item.id} is Done.`)}>Mark done</button>}
          {canFix && <button type="button" className="primary" data-first="" disabled={locked} onClick={fix}>Start fix tab</button>}
          {item.stage === 'hold'
            ? <>
              <button type="button" className="secondary" disabled={locked} onClick={() => move('triage', `${item.id} is back in Triage.`)}>Back to Triage</button>
              <button type="button" className="secondary" disabled={locked} onClick={() => move('ready', `${item.id} moved to Ready.`)}>Move to Ready</button>
            </>
            : <button type="button" className={firstMove || canFix ? 'ghost' : 'secondary'} disabled={locked} onClick={() => move('hold', `${item.id} is on hold.`)}>Put on hold</button>}
        </div>
      </section>}
      <section className="work-block" aria-labelledby={`${id}-tabs`}>
        <h4 id={`${id}-tabs`}>Tabs</h4>
        {item.tabs.length
          ? <ul className="work-tablist">{item.tabs.map(tab => {
            const pane = live(tab.pane)
            return <li key={tab.pane}><button type="button" className="work-tab" disabled={!pane} title={pane ? `Open ${tab.name || tab.pane}` : 'This tab is closed'} onClick={() => pane && onOpen(pane)}>
              <span className={`work-tab-dot${pane ? '' : ' closed'}`} aria-hidden="true" /><span>{tabPath(tab)}</span>
              <small>{tab.role}{pane ? '' : ', closed'}</small>
            </button></li>
          })}</ul>
          : <p className="work-note">No tab yet. One starts when the item moves to Building.</p>}
      </section>
      <section className="work-block" aria-labelledby={`${id}-jira`}>
        <h4 id={`${id}-jira`}>Jira</h4>
        <label className="work-check">
          <input type="checkbox" checked={item.qa} disabled={locked || item.stage === 'done'}
            onChange={event => { const on = event.target.checked; void write(on ? `${item.id} needs QA testing.` : `${item.id} needs no QA testing.`, () => setItemQa(api, item, on)) }} />
          Needs QA testing
        </label>
        <JiraState item={item} project={project} link={link} />
        {item.stage === 'done' && project?.jira && (item.jira?.status === 'failed' || item.jira?.status === 'off') &&
          <div className="work-actions"><button type="button" className="secondary" disabled={locked} onClick={again}>Move again</button></div>}
      </section>
      <dl className="work-kv">
        <dt>Project</dt><dd>{item.project}</dd>
        <dt>Source</dt><dd>{item.source || '–'}</dd>
        <dt>Links</dt><dd>{item.refs.length ? item.refs.map((ref, index) => <span key={ref}>{index > 0 && ', '}{isUrl(ref) ? <a href={ref} target="_blank" rel="noreferrer">{ref}</a> : ref}</span>) : '–'}</dd>
      </dl>
      <section className="work-block" aria-labelledby={`${id}-history`}>
        <h4 id={`${id}-history`}>History</h4>
        {item.history.length
          ? <ol className="work-history">{[...item.history].reverse().map((entry, index) => <li key={`${entry.at}-${index}`}><time dateTime={new Date(entry.at * 1000).toISOString()}>{when(entry.at)}</time><span>{entry.text}</span></li>)}</ol>
          : <p className="work-note">No history yet.</p>}
      </section>
    </div>
  </dialog>
}
