import { useCallback, useEffect, useState } from 'react'
import { SelectMenu } from './SelectMenu'
import { ArrowLeft, Eye, EyeOff, FileText, Link2, Trash2, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { ProjectCodexLevel } from './CodexLevel'
import type { Session } from './Sidebar'

// The session's project (server: projects.py). Agents that start in the session get its text: Claude through a
// SessionStart hook, Codex through developer_instructions. An empty project gives them only the short Notes part.
interface Pinned { name: string; kind: 'copy' | 'link'; path: string; size: number | null; missing: boolean }
interface Note { name: string; title: string; size: number; mtime: number; new: boolean }
export interface ProjectState {
  id: string; name: string; revision: number; budget: number; budget_min: number; budget_max: number
  instructions: string; pins: Pinned[]; notes: Note[]; preview: string; chars: number; folder: string; session_name?: string
}
/** One session in GET projects/marks: does it give agents context, and which panes started with an older version. */
export interface ProjectMark { project: string; revision: number; context: boolean; stale: string[] }
export type ProjectMarks = Record<string, ProjectMark>

const SIZES = [1000, 2000, 4000, 6000, 8000, 10000]

/** Which project a session merge keeps (server: tmux_merge.py). */
export type Keep = 'source' | 'target'
/** A merge asks only when both sessions give agents text. Otherwise the server keeps the one with text. */
export function keepNeeded(marks: ProjectMarks | undefined, source: string, target: string) {
  const ours = marks?.[source], theirs = marks?.[target]
  return !!(ours?.context && theirs?.context && ours.project !== theirs.project)
}

export function KeepChoice({ source, target, value, disabled, onChange }: { source: Session; target: Session; value: Keep | null; disabled?: boolean; onChange: (keep: Keep) => void }) {
  return <fieldset className="choice keep-choice" disabled={disabled}>
    <legend>Both sessions have a project. Keep which one?</legend>
    <div className="segmented">{([['source', source], ['target', target]] as const).map(([key, item]) => <label key={key}>
      <input type="radio" name="merge-keep" value={key} checked={value === key} onChange={() => onChange(key)} /><span title={item.name}>{item.name}</span>
    </label>)}</div>
    <p className="hint">The other project goes to the archive folder. Restart agents that used it.</p>
  </fieldset>
}

/** Sidebar marks, read every 15 s. Read only: it never gives a session a project. */
export function useProjectMarks(api: Api, ready: boolean) {
  const [marks, setMarks] = useState<ProjectMarks>({})
  const load = useCallback(() => { api<ProjectMarks>('projects/marks').then(setMarks, () => {}) }, [api])
  useEffect(() => {
    if (!ready) return
    load()
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
  }, [load, ready])
  return [marks, load] as const
}

export function ProjectSheet({ api, session, mark, pin, onChanged, onClose }: { api: Api; session: Session; mark?: ProjectMark; pin?: string; onChanged: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [project, setProject] = useState<ProjectState | null>(null)
  const [text, setText] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)  // another tab saved first (409)
  const [pinPath, setPinPath] = useState(pin)
  const [note, setNote] = useState<{ name: string; text: string; saved: string } | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [preview, setPreview] = useState(false)

  const show = (next: ProjectState, keepText = false) => { setProject(next); if (!keepText) setText(next.instructions.replace(/\n$/, '')) }
  const load = useCallback(async () => {
    setError(''); setStale(false)
    try { show(await api<ProjectState>(`projects/project?session=${encodeURIComponent(session.id)}`)) }
    catch (failure) { setError(errorText(failure)) }
  }, [api, session.id])
  useEffect(() => { void load() }, [load])

  async function post(path: string, body: Record<string, unknown>, keepText = true) {
    if (!project) return null
    setPending(true); setError('')
    try {
      const next = await api<ProjectState>(`projects/${path}`, { project: project.id, revision: project.revision, session_name: session.name, ...body })
      show(next, keepText)
      onChanged()
      return next
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) setStale(true)
      setError(errorText(failure))
      return null
    } finally { setPending(false) }
  }

  async function openNote(name: string) {
    if (!project) return
    setPending(true); setError(''); setConfirmDelete(false)
    try { const found = await api<{ name: string; text: string }>(`projects/note?project=${project.id}&name=${encodeURIComponent(name)}`); setNote({ ...found, saved: found.text }) }
    catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }

  const dirty = !!project && text.trim() !== project.instructions.trim()
  const fresh = project?.notes.filter(item => item.new).length ?? 0
  const level = project ? project.chars / project.budget : 0
  const staleCount = mark && project && mark.project === project.id ? mark.stale.length : 0
  const pinName = pinPath?.split('/').pop()

  return <dialog {...modal} className="sheet settings-sheet project-sheet" aria-labelledby="project-title">
    <div className="sheet-heading">
      <h2 id="project-title">{note ? note.name : 'Project'}</h2>
      <button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onClose}><X /></button>
    </div>
    <p className="sheet-sub">{session.name}{project && ` · ${project.chars.toLocaleString()} / ${project.budget.toLocaleString()} characters`}</p>
    {!project && !error && <p className="hint">Loading…</p>}
    {error && <p className="error" role="alert">{error}{stale && <> <button type="button" className="link-button" onClick={() => void load()}>Reload</button></>}</p>}

    {project && note ? <section className="settings-group project-note" aria-label={`Note ${note.name}`}>
      <button type="button" className="link-button project-back" disabled={pending} onClick={() => { setNote(null); setConfirmDelete(false) }}><ArrowLeft size={16} />All notes</button>
      <label htmlFor="project-note-text" className="visually-hidden">Note text</label>
      <textarea id="project-note-text" className="project-text" value={note.text} disabled={pending} rows={12} spellCheck onChange={event => setNote({ ...note, text: event.target.value })} />
      {confirmDelete
        ? <div className="confirm-box" role="alertdialog" aria-labelledby="project-note-delete"><p id="project-note-delete"><strong>Delete {note.name}?</strong> Agents stop getting it. A copy stays in the notes trash folder.</p>
            <div className="sheet-buttons"><button type="button" className="secondary" autoFocus disabled={pending} onClick={() => setConfirmDelete(false)}>Cancel</button>
              <button type="button" className="danger" disabled={pending} onClick={async () => { if (await post('note-delete', { name: note.name })) setNote(null) }}>Delete note</button></div></div>
        : <div className="sheet-buttons project-row-end">
            <button type="button" className="secondary danger-text" disabled={pending} onClick={() => setConfirmDelete(true)}><Trash2 size={16} />Delete</button>
            <button type="button" className="primary" disabled={pending || note.text === note.saved} onClick={async () => { if (await post('note', { name: note.name, text: note.text })) setNote({ ...note, saved: note.text }) }}>{pending ? 'Saving…' : 'Save note'}</button>
          </div>}
    </section>

    : project && <>
      {staleCount > 0 && <p className="project-stale" role="status">{staleCount === 1 ? '1 agent' : `${staleCount} agents`} in this session started with an older version. Restart them to apply the change. In Claude, <code>/clear</code> also applies it.</p>}
      {pinPath && <div className="confirm-box project-pin" role="alertdialog" aria-labelledby="project-pin-title">
        <p id="project-pin-title"><strong>Pin {pinName}?</strong> A link always shows the current file. A copy keeps this version (1 MB at most).</p>
        <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setPinPath(undefined)}>Cancel</button>
          <button type="button" className="secondary" disabled={pending} onClick={async () => { if (await post('pin', { path: pinPath, mode: 'copy' })) setPinPath(undefined) }}>Pin a copy</button>
          <button type="button" className="primary" autoFocus disabled={pending} onClick={async () => { if (await post('pin', { path: pinPath, mode: 'link' })) setPinPath(undefined) }}>Pin a link</button></div>
      </div>}
      {!project.instructions.trim() && !project.pins.length && !project.notes.length && !pinPath && <p className="hint">Agents here get only Tether's standard guide. Add instructions or pin a file to give them more.</p>}

      <section className="settings-group" aria-labelledby="project-instructions"><h3 id="project-instructions">Instructions</h3>
        <label htmlFor="project-text" className="visually-hidden">Instructions for every agent in this session</label>
        <textarea id="project-text" className="project-text" value={text} rows={6} disabled={pending} placeholder="For example: Use pnpm. Run the tests before you say done." onChange={event => setText(event.target.value)} />
        <div className="sheet-buttons project-row-end">
          {dirty && <button type="button" className="secondary" disabled={pending} onClick={() => setText(project.instructions.replace(/\n$/, ''))}>Undo</button>}
          <button type="button" className="primary" disabled={pending || !dirty} onClick={() => void post('instructions', { text }, false)}>{pending && dirty ? 'Saving…' : 'Save'}</button>
        </div>
      </section>

      <section className="settings-group" aria-labelledby="project-files"><h3 id="project-files">Pinned files</h3>
        {project.pins.length ? <ul className="project-list">{project.pins.map(item => <li key={item.name}>
          {item.kind === 'link' ? <Link2 size={16} aria-label="Link" /> : <FileText size={16} aria-label="Copy" />}
          <span className="project-item"><strong>{item.name}</strong><small>{item.missing ? 'The file is gone' : item.kind === 'link' ? item.path : 'Copy in the project folder'}</small></span>
          <button type="button" className="secondary" disabled={pending} aria-label={`Unpin ${item.name}`} onClick={() => void post('unpin', { name: item.name })}>Unpin</button>
        </li>)}</ul> : <p className="hint">No files. To pin one, open Files, open the file’s menu, then choose “Pin to project”.</p>}
      </section>

      <section className="settings-group" aria-labelledby="project-notes"><h3 id="project-notes">Notes from agents{fresh > 0 && <span className="project-new">{fresh} new</span>}</h3>
        {project.notes.length ? <ul className="project-list">{project.notes.map(item => <li key={item.name}>
          <button type="button" className="project-open" disabled={pending} onClick={() => void openNote(item.name)}>
            <span className="project-item"><strong>{item.title}{item.new && <span className="project-dot" aria-label="New" />}</strong><small>{item.name} · {new Date(item.mtime).toLocaleString()}</small></span>
          </button>
        </li>)}</ul> : <p className="hint">No notes yet. Agents save project facts here, and you review them.</p>}
        {fresh > 0 && <div className="sheet-buttons project-row-end"><button type="button" className="secondary" disabled={pending} onClick={() => void post('seen', {})}>Mark all as read</button></div>}
      </section>

      <ProjectCodexLevel api={api} project={project.id} panes={session.panes.map(item => item.id)} />

      <section className="settings-group" aria-labelledby="project-size"><h3 id="project-size">Size</h3>
        <div className={`meter-bar ${level > .95 ? 'high' : level > .8 ? 'warn' : ''}`} role="meter" aria-label="Project text size" aria-valuenow={project.chars} aria-valuemin={0} aria-valuemax={project.budget}><span style={{ width: `${Math.min(100, level * 100)}%` }} /></div>
        <div className="project-size-row">
          <label htmlFor="project-budget">Limit</label>
          <SelectMenu id="project-budget" value={String(project.budget)} disabled={pending} onChange={size => void post('budget', { chars: Number(size) })} options={[...new Set([...SIZES, project.budget])].sort((a, b) => a - b).map(size => ({ value: String(size), label: `${size.toLocaleString()} characters` }))} />
          <button type="button" className="secondary" disabled={!project.preview} aria-expanded={preview} onClick={() => setPreview(!preview)}>{preview ? <EyeOff size={16} /> : <Eye size={16} />}{preview ? 'Hide' : 'Preview'}</button>
        </div>
        <p className="setting-note">Agents get this text when they start, resume, clear or compact. A smaller limit keeps more room for the chat.</p>
        {preview && project.preview && <pre className="project-preview" tabIndex={0} aria-label="What agents get">{project.preview}</pre>}
      </section>
    </>}
  </dialog>
}
