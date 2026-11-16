import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { ArrowUp, Check, ChevronDown, Folder, FolderOpen, FolderPlus, Sparkles } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { baseName, crumbs, filesUrl, joinPath, parentOf } from './files'
import type { FileList, PlacesData } from './files'
import type { MainModel } from './prefs'
import { Spinner } from './Skeleton'
import { wheelSideways } from './wheelSideways'

// Fields shared by the New tab, New stack and New session popovers (NewTask.tsx): a name with suggestions, and a folder.

type NameKind = 'session' | 'stack' | 'tab'
export interface NameContext { task?: string; folder?: string; tabs?: string[]; taken?: string[] }

/** The name of a new stack or session. The sparkle asks the server for names (server/suggest_names.py). */
export function NameField({ api, kind, label, value, onChange, problem, context, mainModel, disabled, autoFocus }: {
  api: Api; kind: NameKind; label: string; value: string; onChange: (value: string) => void; problem?: string
  context: () => NameContext; mainModel?: MainModel; disabled?: boolean; autoFocus?: boolean
}) {
  const id = useId()
  const [asking, setAsking] = useState(false)
  const [names, setNames] = useState<string[]>([])
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  async function suggest() {
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    setAsking(true); setError('')
    try {
      const body = { kind, ...context(), ...(mainModel && mainModel !== 'none' ? { agent: mainModel } : {}) }
      // The server tries the main AI model (up to 30 s), then the local model (up to 30 s), then the task words.
      const result = await api<{ names: string[] }>('suggest-name', body, abort.signal, undefined, 70000)
      if (abort.signal.aborted) return
      setNames(result.names)
      if (result.names[0]) onChange(result.names[0])
    } catch (failure) { if (!abort.signal.aborted) setError(errorText(failure)) } finally { if (!abort.signal.aborted) setAsking(false) }
  }
  const hint = problem ? `${id}-problem` : error ? `${id}-error` : undefined
  return <div className="create-name">
    <label htmlFor={id}>{label}</label>
    <div className="create-name-box">
      <input id={id} value={value} maxLength={kind === 'session' ? 64 : 40} disabled={disabled} autoComplete="off" spellCheck={false} autoFocus={autoFocus}
        aria-invalid={!!problem || undefined} aria-describedby={hint} onChange={event => { onChange(event.target.value); setError('') }} />
      <button type="button" className="create-suggest" disabled={disabled || asking} aria-label={asking ? 'Suggesting names…' : 'Suggest names'} title="Suggest names" onClick={() => void suggest()}>
        {asking ? <Spinner size={15} /> : <Sparkles size={15} />}
      </button>
    </div>
    {names.length > 1 && <div className="create-names" role="group" aria-label="Suggested names">
      {names.map(name => <button type="button" key={name} disabled={disabled} aria-pressed={value === name} onClick={() => onChange(name)}>{name}</button>)}
    </div>}
    {problem && <p id={`${id}-problem`} className="create-hint">{problem}</p>}
    {error && <p id={`${id}-error`} className="error create-hint" role="alert">{error}</p>}
  </div>
}

/** One folder row that opens the folder browser in place. `value` null = `start`, the default folder. */
export function FolderField({ api, value, start, note, onChange, disabled }: {
  api: Api; value: string | null; start: string; note?: string; onChange: (value: string | null) => void; disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const shown = value ?? start
  return <div className="create-folder">
    <button type="button" className="create-folder-row" aria-expanded={open} aria-controls={id} disabled={disabled} onClick={() => setOpen(!open)}>
      <Folder size={15} aria-hidden="true" />
      <span className="create-folder-label">Folder</span>
      <span className="create-folder-path" title={shown}>{shown}</span>
      {value === null && note && <small>{note}</small>}
      <ChevronDown size={14} aria-hidden="true" className="create-folder-chevron" />
    </button>
    {open && <FolderBrowser id={id} api={api} start={shown} onPick={path => { onChange(path === start ? null : path); setOpen(false) }} />}
  </div>
}

// The folders people work in: live tabs' folders first (most tabs first), then pinned folders. Home is always there.
function recentFolders(places: PlacesData | null) {
  if (!places) return []
  const counts = new Map<string, number>()
  for (const row of places.sessions) if (row.cwd && row.cwd !== '~') counts.set(row.cwd, (counts.get(row.cwd) ?? 0) + 1)
  const live = [...counts].sort((a, b) => b[1] - a[1]).map(([path]) => path)
  const pinned = places.pinned.map(item => typeof item === 'string' ? item : item.exists === false ? '' : item.path).filter(path => path && path !== '~')
  return [...new Set([...live, ...pinned])].slice(0, 8)
}

/** Recent folders, a filter that also takes a typed path, the path bar, sub folders and New folder. Hidden and secret folders do not show. */
function FolderBrowser({ id, api, start, onPick }: { id: string; api: Api; start: string; onPick: (path: string) => void }) {
  const [folder, setFolder] = useState(start || '~')
  const [filter, setFilter] = useState('')
  const [listing, setListing] = useState<{ path: string; folders?: string[]; error?: string } | null>(null)
  const [places, setPlaces] = useState<PlacesData | null>(null)
  const [making, setMaking] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const abort = new AbortController()
    api<PlacesData>(filesUrl.places(), undefined, abort.signal).then(setPlaces, () => { /* Recent stays empty; browsing still works */ })
    return () => abort.abort()
  }, [api])
  useEffect(() => {
    const abort = new AbortController()
    api<FileList>(filesUrl.list(folder, false, 'name', true), undefined, abort.signal).then(
      data => setListing({ path: folder, folders: data.entries.filter(entry => entry.dir && !entry.secret && !entry.hidden).map(entry => entry.name) }),
      failure => { if (!abort.signal.aborted) setListing({ path: folder, error: errorText(failure) }) })
    return () => abort.abort()
  }, [api, folder])
  useEffect(() => { if (typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches) field.current?.focus() }, [])
  const shown = listing?.path === folder ? listing : null
  const typedPath = /^(~|\/)/.test(filter.trim())
  const query = typedPath ? '' : filter.trim().toLowerCase()
  const matches = useMemo(() => shown?.folders?.filter(name => name.toLowerCase().includes(query)) ?? [], [shown, query])
  const recent = recentFolders(places).filter(path => path !== folder)
  const enter = (path: string) => { setFolder(path); setFilter(''); setError(''); setMaking(null) }
  // A div, not a form: this field sits inside the New tab / session form, and Enter here must not submit that form.
  async function make() {
    const name = making?.trim()
    if (!name) return
    setPending(true); setError('')
    try {
      const made = await api<{ name: string }>('files/mkdir', { path: folder, name })
      enter(joinPath(folder, made.name))
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  return <div id={id} className="create-browser" role="group" aria-label="Choose a folder">
    <input ref={field} className="create-browser-filter" value={filter} placeholder="Filter, or type a path like ~/apps" aria-label="Filter folders or type a path" autoComplete="off" spellCheck={false}
      onChange={event => setFilter(event.target.value)}
      onKeyDown={event => {
        if (event.key !== 'Enter') return
        event.preventDefault()
        if (typedPath) enter(filter.trim().replace(/\/+$/, '') || '/')
        else if (matches.length === 1) enter(joinPath(folder, matches[0]))
      }} />
    {!filter && recent.length > 0 && <div ref={wheelSideways} className="create-recent" role="group" aria-label="Recent folders">
      {recent.map(path => <button type="button" key={path} title={path} onClick={() => onPick(path)}>{baseName(path)}</button>)}
    </div>}
    <div className="create-crumbs">
      <button type="button" className="icon-button" aria-label="Parent folder" disabled={!parentOf(folder)} onClick={() => { const up = parentOf(folder); if (up) enter(up) }}><ArrowUp size={15} /></button>
      <nav ref={wheelSideways} aria-label="Folder path">{crumbs(folder).map((crumb, at, all) => <span key={crumb.path}>
        {at > 0 && <span className="create-sep" aria-hidden="true">/</span>}
        {at === all.length - 1 ? <span aria-current="location">{crumb.label}</span> : <button type="button" onClick={() => enter(crumb.path)}>{crumb.label}</button>}
      </span>)}</nav>
    </div>
    <ul className="create-folders" aria-label={`Folders in ${baseName(folder)}`} aria-busy={!shown || undefined}>
      {shown?.error ? <li className="error" role="alert">{shown.error}</li>
        : !shown ? <li className="create-note"><Spinner size={13} />Loading folders…</li>
        : typedPath ? <li className="create-note">Press Enter to open {filter.trim()}.</li>
        : !matches.length ? <li className="create-note">{query ? `No folder here matches “${filter.trim()}”.` : 'No folders here.'}</li>
        : matches.map(name => <li key={name}><button type="button" onClick={() => enter(joinPath(folder, name))}><FolderOpen size={15} aria-hidden="true" />{name}</button></li>)}
    </ul>
    {making !== null && <div className="create-mkdir" role="group" aria-label="New folder">
      <input autoFocus value={making} maxLength={255} placeholder="Folder name" aria-label="New folder name" disabled={pending} onChange={event => setMaking(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setMaking(null) }
          else if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); if (!pending) void make() }
        }} />
      <button type="button" className="secondary" disabled={pending || !making.trim()} onClick={() => void make()}>{pending ? 'Making…' : 'Make'}</button>
    </div>}
    {error && <p className="error create-hint" role="alert">{error}</p>}
    <div className="create-browser-foot">
      {making === null && <button type="button" className="ghost" disabled={!shown || !!shown.error} onClick={() => setMaking('')}><FolderPlus size={15} />New folder</button>}
      <button type="button" className="primary" disabled={!shown || !!shown.error} onClick={() => onPick(folder)}><Check size={15} />Use {baseName(folder)}</button>
    </div>
  </div>
}
