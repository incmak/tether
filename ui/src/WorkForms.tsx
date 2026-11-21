import { useEffect, useId, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { Plus, Trash2, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { saveProject, newItem } from './work'
import type { FeedKind, FeedState, FixResult, WorkBoard, WorkFeed, WorkItem, WorkProject } from './work'
import { fmtTime, useTetherZone } from './zone'

// Project settings (Add project, Settings) and New item: small sheets over the work page. The server checks every
// field (work_items._clean_project); these forms show its message and keep what Mak typed.

const FEEDS: { value: FeedKind; label: string; target: string; hint: string; placeholder: string; optional?: boolean }[] = [
  { value: 'github', label: 'GitHub issues', target: 'Repository (owner/name)', hint: '', placeholder: 'owner/name' },
  { value: 'email', label: 'Email (Microsoft 365)', target: 'Mail folder (optional)', hint: 'Empty reads the Inbox.', placeholder: 'Inbox', optional: true },
  { value: 'teams', label: 'Teams channel', target: 'Team/Channel', hint: 'One channel, like Brisk/QA.', placeholder: 'Team/Channel' },
]
const FEEDS_MAX = 8  // work_items.FEEDS_MAX
const BLANK: WorkProject = { name: '', prefix: '', folder: '', triage: '', work: '', feeds: [], jira: '', qaStatus: '', jiraUser: '' }
/** A feed row in the form. key: the saved id, or a local one for a row not saved yet (the server gives it an id). */
interface FeedRow extends WorkFeed { key: string }
let rowCount = 0
const newKey = () => `new-${++rowCount}`
const toRows = (feeds: WorkFeed[]): FeedRow[] => feeds.map(feed => ({ ...feed, key: feed.id ?? newKey() }))

/** What one feed does now: read by the project's agent, the last read, or what blocks it. failed: show it as an error. */
function feedNote(board: WorkBoard, project: string | undefined, row: FeedRow, saved?: WorkFeed): { text: string; failed?: boolean } {
  if (row.by === 'agent') return { text: "Tether does not read this feed. The project's agent adds its items." }
  const graph = board.graph
  if (row.kind !== 'github' && !graph?.connected) return { text: `${graph?.why || 'Microsoft 365 is not connected.'} Until then, Tether does not read this feed.`, failed: true }
  if (row.kind === 'teams' && !graph?.teams) return { text: 'Off: Teams channels need admin consent for ChannelMessage.Read.All. Ask your Microsoft 365 admin to grant it, then run tether graph connect --feeds mail,teams.', failed: true }
  if (row.kind === 'email' && !graph?.mail) return { text: 'Off: the Microsoft 365 sign-in has no mail permission. Run tether graph connect --feeds mail.', failed: true }
  const same = saved && saved.kind === row.kind && saved.target === row.target.trim() && saved.by === row.by
  const state: FeedState | undefined = same && project ? board.feeds?.[project]?.find(entry => entry.id === saved.id) : undefined
  const start = saved && !same ? 'Changed: Tether starts again with messages that come after you save.'
    : 'Tether reads it every 2 minutes and adds new threads to Triage. It starts with messages that come after you save.'
  if (!state || state.status === 'waiting' || !state.at) return { text: saved && same ? 'Saved. Waiting for the first read.' : start }
  const at = fmtTime(state.at * 1000, { hour: '2-digit', minute: '2-digit' })
  if (state.status === 'ok') return { text: `Last read at ${at}. ${state.items} ${state.items === 1 ? 'item' : 'items'} added so far.` }
  return { text: `${state.status === 'off' ? 'Off since' : 'The last read failed at'} ${at}: ${state.error}`, failed: true }
}

/** One feed: where it reads, who reads it, and its last read. */
function FeedFields({ id, number, row, note, onChange, onRemove, selectRef }: {
  id: string; number: number; row: FeedRow; note: { text: string; failed?: boolean }
  onChange: (change: Partial<WorkFeed>) => void; onRemove: () => void; selectRef: (node: HTMLSelectElement | null) => void
}) {
  const feed = FEEDS.find(entry => entry.value === row.kind) ?? FEEDS[0]
  const hint = [feed.hint, `${id}-note`].filter(Boolean)
  return <li className="work-feed">
    <fieldset>
      <legend>Feed {number}</legend>
      <div className="work-feed-fields">
        <Field id={`${id}-kind`} label="Source">
          <select id={`${id}-kind`} ref={selectRef} value={row.kind} onChange={event => onChange({ kind: event.target.value as FeedKind })}>
            {FEEDS.map(entry => <option key={entry.value} value={entry.value}>{entry.label}</option>)}
          </select>
        </Field>
        <Field id={`${id}-target`} label={feed.target} hint={feed.hint || undefined}>
          <input id={`${id}-target`} value={row.target} onChange={event => onChange({ target: event.target.value })} required={!feed.optional} maxLength={300} autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder={feed.placeholder} aria-describedby={hint.length > 1 ? `${id}-target-hint ${id}-note` : `${id}-note`} />
        </Field>
      </div>
      <div className="work-feed-foot">
        <label className="work-check"><input type="checkbox" checked={row.by === 'agent'} onChange={event => onChange({ by: event.target.checked ? 'agent' : 'tether' })} />The project's own agent adds items from this feed</label>
        <button type="button" className="secondary work-feed-remove" onClick={onRemove} aria-label={`Remove feed ${number}`}><Trash2 aria-hidden="true" /><span>Remove</span></button>
      </div>
      <p id={`${id}-note`} className={note.failed ? 'work-hint work-feed-note failed' : 'work-hint work-feed-note'}>{note.text}</p>
    </fieldset>
  </li>
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
  useTetherZone()
  const id = useId()
  const editing = !!project
  const [form, setForm] = useState<WorkProject>(() => ({ ...BLANK, ...project }))
  const [rows, setRows] = useState<FeedRow[]>(() => toRows(project?.feeds ?? []))
  // After Add feed, its source gets focus; after Remove, Add feed does (the removed row's button is gone).
  const focusRow = useRef<string | null>(null)
  const selects = useRef(new Map<string, HTMLSelectElement>())
  const addButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (focusRow.current === null) return
    const target = focusRow.current ? selects.current.get(focusRow.current) : addButton.current
    focusRow.current = null
    target?.focus()
  }, [rows])
  const changeRow = (key: string, change: Partial<WorkFeed>) => setRows(current => current.map(row => row.key === key ? { ...row, ...change } : row))
  const addRow = () => {
    const used = new Set(rows.map(row => row.kind))
    const row: FeedRow = { key: newKey(), kind: FEEDS.find(entry => !used.has(entry.value))?.value ?? 'github', target: '', by: 'tether' }
    focusRow.current = row.key
    setRows(current => [...current, row])
  }
  const removeRow = (key: string) => { focusRow.current = ''; setRows(current => current.filter(row => row.key !== key)) }
  // The registry revision this form started from. After a 409 it follows the board, so a second Save replaces.
  const [base, setBase] = useState<number | null>(board.revision)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (key: keyof WorkProject) => (event: { target: { value: string } }) => setForm(current => ({ ...current, [key]: event.target.value }))
  const name = form.name.trim()
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const feeds = rows.map(row => ({ ...(row.id ? { id: row.id } : {}), kind: row.kind, target: row.target.trim(), by: row.by }))
      const saved = { ...form, feeds, prefix: form.prefix.trim().toUpperCase(), jira: form.jira.trim().toUpperCase(), name }
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
        <legend>Feeds</legend>
        <p className="work-hint">Each feed adds new threads to Triage: GitHub issues, email and Teams messages. A project can read several at once.</p>
        {rows.length > 0
          ? <ol className="work-feeds">{rows.map((row, index) => <FeedFields key={row.key} id={`${id}-feed-${row.key}`} number={index + 1} row={row}
              note={feedNote(board, project?.name, row, project?.feeds.find(feed => feed.id === row.id))}
              onChange={change => changeRow(row.key, change)} onRemove={() => removeRow(row.key)}
              selectRef={node => { if (node) selects.current.set(row.key, node); else selects.current.delete(row.key) }} />)}</ol>
          : <p className="work-feeds-empty">No feeds. Add items by hand with New item, or add a feed.</p>}
        <div className="work-feeds-add">
          <button type="button" ref={addButton} className="secondary" onClick={addRow} disabled={rows.length >= FEEDS_MAX} aria-describedby={rows.length >= FEEDS_MAX ? `${id}-feeds-max` : undefined}><Plus aria-hidden="true" />Add feed</button>
          {rows.length >= FEEDS_MAX && <p id={`${id}-feeds-max`} className="work-hint">A project reads at most {FEEDS_MAX} feeds.</p>}
        </div>
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
