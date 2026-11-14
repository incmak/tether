import { useEffect, useId, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { saveProject, newItem } from './work'
import type { FixResult, WorkBoard, WorkItem, WorkProject } from './work'

// Project settings (Add project, Settings) and New item: small sheets over the work page. The server checks every
// field (work_items._clean_project); these forms show its message and keep what Mak typed.

const FEEDS: { value: WorkProject['feed']; label: string; target: string; hint: string; placeholder: string; optional?: boolean }[] = [
  { value: 'manual', label: 'None: add items by hand', target: '', hint: '', placeholder: '' },
  { value: 'teams', label: 'Teams channel', target: 'Team/Channel', hint: 'One channel, like Brisk/QA.', placeholder: 'Team/Channel' },
  { value: 'github', label: 'GitHub issues', target: 'Repository (owner/name)', hint: '', placeholder: 'owner/name' },
  { value: 'email', label: 'Email (Microsoft 365)', target: 'Mail folder (optional)', hint: 'Empty reads the Inbox.', placeholder: 'Inbox', optional: true },
]
const BLANK: WorkProject = { name: '', prefix: '', folder: '', triage: '', work: '', feed: 'manual', feedTarget: '', feedBy: 'tether', jira: '', qaStatus: '', jiraUser: '' }

/** What the feed does now: read by the project's agent, the last read, or what blocks it. */
function feedNote(board: WorkBoard, form: WorkProject, saved?: WorkProject) {
  if (form.feed === 'manual') return 'Add items with New item.'
  if (form.feedBy === 'agent') return "Tether does not read this feed. The project's agent adds its items."
  const graph = board.graph
  if (form.feed !== 'github' && !graph?.connected) return `${graph?.why || 'Microsoft 365 is not connected.'} Until then, Tether does not read this feed.`
  if (form.feed === 'teams' && !graph?.teams) return 'The Microsoft 365 sign-in has no Teams permission. Run tether graph connect --feeds mail,teams.'
  if (form.feed === 'email' && !graph?.mail) return 'The Microsoft 365 sign-in has no mail permission. Run tether graph connect --feeds mail,teams.'
  const state = saved && saved.feed === form.feed && saved.feedTarget === form.feedTarget && saved.feedBy === form.feedBy ? board.feeds?.[saved.name] : undefined
  const start = 'Tether reads it every 2 minutes and adds new threads to Triage. It starts with messages that come after you save.'
  if (!state || state.status === 'waiting' || !state.at) return start
  const at = new Date(state.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (state.status === 'ok') return `Last read at ${at}. ${state.items} ${state.items === 1 ? 'item' : 'items'} added so far.`
  return `${state.status === 'off' ? 'Stopped' : 'The last read failed'} at ${at}: ${state.error}`
}

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return <div className="work-field">
    <label htmlFor={id}>{label}</label>
    {children}
    {hint && <p id={`${id}-hint`} className="work-hint">{hint}</p>}
  </div>
}

function FormSheet({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const modal = useModal(true, onClose)
  const id = useId()
  // The tool that opened the form gets focus back when it closes: React removes the dialog without close().
  const [opener] = useState(() => document.activeElement instanceof HTMLElement ? document.activeElement : null)
  useEffect(() => {
    // Focus the first field to fill. The dialog opens in useModal's effect, which runs first; autoFocus runs too early.
    modal.ref.current?.querySelector<HTMLElement>('[data-first]')?.focus()
    return () => { if (opener?.isConnected) opener.focus() }
  }, [modal.ref, opener])
  return <dialog {...modal} className="sheet work-form-sheet" aria-labelledby={`${id}-title`}>
    <div className="sheet-heading"><h2 id={`${id}-title`}>{title}</h2><button type="button" className="icon-button" aria-label={`Close ${title.toLowerCase()}`} onClick={onClose}><X /></button></div>
    {children}
  </dialog>
}

/** Add a project (no project given) or change one. Name and prefix are fixed once the project exists. */
export function ProjectForm({ api, board, project, onClose, onSaved }: {
  api: Api; board: WorkBoard; project?: WorkProject; onClose: () => void; onSaved: (name: string) => void
}) {
  const id = useId()
  const editing = !!project
  const [form, setForm] = useState<WorkProject>(() => ({ ...BLANK, ...project }))
  // The registry revision this form started from. After a 409 it follows the board, so a second Save replaces.
  const [base, setBase] = useState<number | null>(board.revision)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (key: keyof WorkProject) => (event: { target: { value: string } }) => setForm(current => ({ ...current, [key]: event.target.value }))
  const feed = FEEDS.find(entry => entry.value === form.feed) ?? FEEDS[0]
  const name = form.name.trim()
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const saved = { ...form, prefix: form.prefix.trim().toUpperCase(), jira: form.jira.trim().toUpperCase(), name }
      await saveProject(api, saved, base ?? board.revision, !editing)
      onSaved(name)
    } catch (failure) {
      const stale = failure instanceof ApiError && failure.status === 409 && /changed in another tab/.test(failure.message)
      if (stale) setBase(null)
      setError(stale ? 'The projects changed in another tab. Your entries are kept. Save again to replace the other change.' : errorText(failure))
    } finally { setBusy(false) }
  }
  return <FormSheet title={editing ? `${project.name} settings` : 'Add project'} onClose={onClose}>
    <form className="work-form" onSubmit={event => void submit(event)}>
      <div className="work-form-row">
        <Field id={`${id}-name`} label="Name" hint={editing ? 'Fixed: item ids and folders use it.' : 'Lower-case letters, digits and dashes.'}>
          <input id={`${id}-name`} value={form.name} onChange={set('name')} readOnly={editing} required maxLength={32} pattern="[a-z0-9][a-z0-9\-]*" autoComplete="off" autoCapitalize="none" spellCheck={false} aria-describedby={`${id}-name-hint`} data-first={editing ? undefined : ''} />
        </Field>
        <Field id={`${id}-prefix`} label="Item prefix" hint={editing ? 'Fixed.' : '1 to 3 capital letters, like T.'}>
          <input id={`${id}-prefix`} value={form.prefix} onChange={event => setForm(current => ({ ...current, prefix: event.target.value.toUpperCase() }))} readOnly={editing} required maxLength={3} pattern="[A-Za-z]{1,3}" autoComplete="off" autoCapitalize="characters" spellCheck={false} aria-describedby={`${id}-prefix-hint`} />
        </Field>
      </div>
      <Field id={`${id}-folder`} label="Folder" hint="The project's code folder. Fix and triage tabs open here.">
        <input id={`${id}-folder`} value={form.folder} onChange={set('folder')} required maxLength={1024} autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="~/code/project" aria-describedby={`${id}-folder-hint`} data-first={editing ? '' : undefined} />
      </Field>
      <div className="work-form-row">
        <Field id={`${id}-triage`} label="Triage session">
          <input id={`${id}-triage`} value={form.triage} onChange={set('triage')} maxLength={64} autoComplete="off" spellCheck={false} placeholder={`${name || 'name'}-triage`} />
        </Field>
        <Field id={`${id}-work`} label="Work session">
          <input id={`${id}-work`} value={form.work} onChange={set('work')} maxLength={64} autoComplete="off" spellCheck={false} placeholder={name || 'name'} />
        </Field>
      </div>
      <fieldset className="work-fieldset">
        <legend>Feed</legend>
        <Field id={`${id}-feed`} label="Source">
          <select id={`${id}-feed`} value={form.feed} onChange={set('feed')}>{FEEDS.map(entry => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select>
        </Field>
        {feed.value !== 'manual' && <Field id={`${id}-target`} label={feed.target} hint={feed.hint || undefined}>
          <input id={`${id}-target`} value={form.feedTarget} onChange={set('feedTarget')} required={!feed.optional} maxLength={300} autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder={feed.placeholder} aria-describedby={feed.hint ? `${id}-target-hint` : undefined} />
        </Field>}
        {feed.value !== 'manual' && <label className="work-check"><input type="checkbox" checked={form.feedBy === 'agent'} onChange={event => { const agent = event.target.checked; setForm(current => ({ ...current, feedBy: agent ? 'agent' : 'tether' })) }} />The project's own agent adds items from this feed</label>}
        <p className="work-hint">{feedNote(board, form, project)}</p>
      </fieldset>
      <fieldset className="work-fieldset">
        <legend>Jira <span>(optional)</span></legend>
        <div className="work-form-row">
          <Field id={`${id}-jira`} label="Project key">
            <input id={`${id}-jira`} value={form.jira} onChange={event => setForm(current => ({ ...current, jira: event.target.value.toUpperCase() }))} maxLength={20} pattern="[A-Za-z][A-Za-z0-9]*" autoComplete="off" autoCapitalize="characters" spellCheck={false} placeholder="BS" />
          </Field>
          <Field id={`${id}-qa`} label="Status when QA must test">
            <input id={`${id}-qa`} value={form.qaStatus} onChange={set('qaStatus')} maxLength={64} autoComplete="off" placeholder="QA Testing" />
          </Field>
        </div>
        <p className="work-hint">On Done, Tether moves the item's tickets that are assigned to you. {board.jira?.connected
          ? `Connected to ${board.jira.site} as ${board.jira.account}.${board.jira.guard ? '' : ` ${board.jira.why}`}`
          : 'Jira is not connected: run tether jira connect on the Tether host.'}</p>
      </fieldset>
      {error && <p className="work-form-error" role="alert">{error}</p>}
      <div className="work-form-actions">
        <button type="submit" className="primary" disabled={busy}>{editing ? 'Save' : 'Add project'}</button>
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
      </div>
    </form>
  </FormSheet>
}

/** Add an item by hand. It starts in Triage; a triage tab opens for it in the project's triage session. */
export function NewItemForm({ api, board, project, onClose, onAdded }: {
  api: Api; board: WorkBoard; project?: string; onClose: () => void; onAdded: (item: WorkItem, tab?: FixResult) => void
}) {
  const id = useId()
  const [chosen, setChosen] = useState(project ?? board.projects[0]?.name ?? '')
  const [title, setTitle] = useState('')
  const [links, setLinks] = useState('')
  const [tab, setTab] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!title.trim()) return
    setBusy(true); setError('')
    const refs = links.split(/[\s,]+/).map(ref => ref.trim()).filter(Boolean)
    try {
      const done = await newItem(api, chosen, title.trim(), refs, tab)
      onAdded(done.item, done.tab)
    } catch (failure) { setError(errorText(failure)) } finally { setBusy(false) }
  }
  return <FormSheet title="New item" onClose={onClose}>
    <form className="work-form" onSubmit={event => void submit(event)}>
      {board.projects.length > 1 && <Field id={`${id}-project`} label="Project">
        <select id={`${id}-project`} value={chosen} onChange={event => setChosen(event.target.value)}>{board.projects.map(entry => <option key={entry.name} value={entry.name}>{entry.name}</option>)}</select>
      </Field>}
      <Field id={`${id}-title`} label="Title">
        <input id={`${id}-title`} value={title} onChange={event => setTitle(event.target.value)} required maxLength={200} autoComplete="off" placeholder="What is wrong, or what to do" data-first="" />
      </Field>
      <Field id={`${id}-links`} label="Links (optional)" hint="Tickets or URLs, separated by spaces or commas.">
        <input id={`${id}-links`} value={links} onChange={event => setLinks(event.target.value)} maxLength={4000} autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="BS-1340" aria-describedby={`${id}-links-hint`} />
      </Field>
      <label className="work-check"><input type="checkbox" checked={tab} onChange={event => setTab(event.target.checked)} />Start a triage tab for it</label>
      {error && <p className="work-form-error" role="alert">{error}</p>}
      <div className="work-form-actions">
        <button type="submit" className="primary" disabled={busy || !title.trim() || !chosen}>Add to Triage</button>
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
      </div>
    </form>
  </FormSheet>
}
