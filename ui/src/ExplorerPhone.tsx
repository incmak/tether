import { RetryButton } from './Retry'
import { Bone, Loading, SkeletonRows } from './Skeleton'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowUpDown, BookMarked, Camera, ChevronLeft, ClipboardCopy, CopyPlus, Download, File, Folder, FolderInput, FolderOutput, FolderPlus, Image as ImageIcon, Lock, MessageSquarePlus, MoreHorizontal, Package, Pencil, Plus, Search, SlidersHorizontal, Trash2, Upload, X } from 'lucide-react'
import { ApiError, errorText, reach } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { baseName, crumbs, filesUrl, formatModified, formatSize, gitText, joinPath, kindLabel, parentOf, readSecrets, shapeOf } from './files'
import type { FileEntry, FileList, FilePreview, FileSearch, SecretsState, Shape, SortKey } from './files'
import { clearUploads, setUploadDone, startUploads, stopUploads, useUploads } from './fileWrites'
import './ExplorerPhone.css'
import { useHost } from './host'
import { OfficeViewer, isOffice } from './OfficeViewer'
import type { OfficeFile } from './OfficeViewer'
import { TextViewer } from './TextViewer'
import type { TextFile } from './TextViewer'
import { wheelSideways } from './wheelSideways'
import { useTetherZone } from './zone'

// The phone Files screen (slice 4). Layout from ExplorerPhone.dc.html: path chips, a sort cycle, search, Select mode with
// an action bar, a + sheet, a ⋯ sheet per row and a full-screen preview. Writes use POST /mobile/api/files/* (API.md).
// Secret paths (the read-only window) never offer writes, downloads or "Add to chat"; the server refuses them too.

/** Sort cycle: Name (A to Z), Newest first, Largest first. */
const SORTS: { key: SortKey; asc: boolean; label: string }[] = [
  { key: 'name', asc: true, label: 'Name' },
  { key: 'modified', asc: false, label: 'Newest' },
  { key: 'size', asc: false, label: 'Largest' },
]
const STORE = 'tether-files-phone'
interface Stored { path: string; hidden: boolean; sort: number }
function loadStored(): Stored {
  const base: Stored = { path: '~', hidden: false, sort: 0 }
  try {
    const raw = JSON.parse(localStorage.getItem(STORE) ?? 'null') as Partial<Stored> | null
    if (!raw) return base
    return { path: typeof raw.path === 'string' && raw.path ? raw.path : '~', hidden: raw.hidden === true, sort: [0, 1, 2].includes(Number(raw.sort)) ? Number(raw.sort) : 0 }
  } catch { return base }
}

// Same PRIVATE list as the server (media_files.py). Only used to hide write buttons inside a secret folder.
const PRIVATE = new Set(['.ssh', '.gnupg', '.config', '.aws', '.azure', '.claude', '.codex', '.local', '.docker', '.kube', '.password-store', '.pki', '.mozilla', '.git', '.npm', '.cargo', '.gemini', '.vscode-server'])
const inSecret = (path: string) => path.split('/').some(part => PRIVATE.has(part))

interface Row extends FileEntry { full: string; where?: string }
type Sheet = { kind: 'add' } | { kind: 'view' } | { kind: 'row'; row: Row }
type Dialog = { kind: 'rename'; row: Row } | { kind: 'mkdir' } | { kind: 'delete'; rows: Row[] } | { kind: 'zip'; paths: string[] } | { kind: 'pick'; mode: 'copy' | 'move'; paths: string[] }
interface Result { done: { from: string; path: string }[]; failed: { path: string; error: string }[]; skipped?: { secret?: number } }

const ICONS: Record<Shape, typeof File> = { dir: Folder, image: ImageIcon, archive: Package, doc: File }
const items = (count: number) => `${count} ${count === 1 ? 'item' : 'items'}`
const leftOut = (count: number | undefined) => count ? ` ${count} secret ${count === 1 ? 'item' : 'items'} left out.` : ''

/** A JSON POST for writes. Copy, move and trash of a big folder can take longer than the usual 10 s. */
async function write<T>(path: string, body: unknown): Promise<T> {
  const response = await reach(`/mobile/api/${path}`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
    body: JSON.stringify(body), signal: AbortSignal.timeout(300000),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(response.status, data.error || `Request failed (${response.status}). Please try again.`)
  return data as T
}

const zipUrl = (paths: string[]) => `/mobile/api/files/zip?${paths.map(path => `path=${encodeURIComponent(path)}`).join('&')}`
const sizeUrl = (paths: string[]) => `files/size?${paths.map(path => `path=${encodeURIComponent(path)}`).join('&')}`
function startDownload(url: string, name = '') {
  const link = document.createElement('a')
  link.href = url; link.download = name; link.rel = 'noopener'
  document.body.appendChild(link); link.click(); link.remove()
}
async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text); return true } catch { /* plain http on the tailnet */ }
  const box = document.createElement('textarea')
  box.value = text; box.setAttribute('readonly', ''); box.style.position = 'fixed'; box.style.opacity = '0'
  document.body.appendChild(box); box.select()
  let done: boolean
  try { done = document.execCommand('copy') } catch { done = false }
  box.remove()
  return done
}

function EntryIcon({ entry }: { entry: FileEntry }) {
  const shape = shapeOf(entry)
  const Icon = ICONS[shape]
  return <Icon size={24} aria-hidden="true" strokeWidth={1.7} className={`xp-icon xp-icon-${shape}`} />
}

interface Props {
  api: Api
  /** Open this folder ("Open in Files"), then call onTarget. */
  target: string | null
  onTarget: () => void
  /** The open chat's name, for "Add to …". Without a chat there is no "Add to chat". */
  chatName?: string
  onAddToChat?: (paths: string[]) => void
  /** "Pin to project" for one file: the open chat's session project. */
  onPin?: { label: string; run: (path: string) => void }
}

export function ExplorerPhone({ api, target, onTarget, chatName, onAddToChat, onPin }: Props) {
  useTetherZone()
  const [stored, setStored] = useState(loadStored)
  const { path: cwd, hidden } = stored
  const sort = SORTS[stored.sort]
  useEffect(() => { try { localStorage.setItem(STORE, JSON.stringify(stored)) } catch { /* private window */ } }, [stored])
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  useEffect(() => { const timer = setTimeout(() => setSearch(query.trim()), 300); return () => clearTimeout(timer) }, [query])
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [sheet, setSheet] = useState<Sheet | null>(null)
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [preview, setPreview] = useState<Row | null>(null)
  const [toast, setToast] = useState('')
  const [tick, setTick] = useState(0)
  const reload = () => setTick(n => n + 1)
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(''), 4000); return () => clearTimeout(timer) }, [toast])

  const go = useCallback((path: string) => {
    setStored(current => ({ ...current, path }))
    setSelected([]); setQuery(''); setSearch(''); setSearchOpen(false); setPreview(null)
  }, [])
  // eslint-disable-next-line react-hooks/set-state-in-effect -- Consume the external Files target and acknowledge it after the parent commits.
  useEffect(() => { if (target) { onTarget(); go(target) } }, [target, onTarget, go])

  // The "Show secret folders" read window. the server runs the clock; this shows the minutes left and turns off on time.
  const [secrets, setSecrets] = useState<SecretsState>({ on: false, expires: 0 })
  const [now, setNow] = useState(() => Date.now())
  const takeSecrets = useCallback((wire: FileList['secrets']) => {
    if (!wire) return
    const next = readSecrets(wire)
    setSecrets(current => current.on === next.on && current.expires === next.expires ? current : next)
  }, [])
  const switchSecrets = async (on: boolean) => {
    try {
      const next = readSecrets(await api<FileList['secrets']>(filesUrl.secrets(), { on }))
      setSecrets(next)
      setToast(next.on ? `Secret folders show for ${Math.round((next.expires - Date.now()) / 60000)} minutes. Read only.` : 'Secret folders are hidden again.')
    } catch (failure) { setToast(errorText(failure)) }
  }
  useEffect(() => {
    if (!secrets.on) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Refresh the wall clock when the server's secret window changes, before the first interval tick.
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 15000)
    const end = setTimeout(() => { setSecrets({ on: false, expires: 0 }); setToast('Secret folders are hidden again.') }, Math.max(0, secrets.expires - Date.now()) + 250)
    return () => { clearInterval(timer); clearTimeout(end) }
  }, [secrets])
  useEffect(() => {
    const again = () => { if (document.visibilityState === 'visible') api<{ secrets?: FileList['secrets'] }>(filesUrl.places()).then(data => takeSecrets(data.secrets), () => {}) }
    again()
    document.addEventListener('visibilitychange', again)
    return () => document.removeEventListener('visibilitychange', again)
  }, [api, takeSecrets])
  const minutesLeft = Math.max(1, Math.ceil((secrets.expires - now) / 60000))

  // The folder, or search hits under it. Sort runs on the server.
  const [listing, setListing] = useState<{ key: string; attempt: number; data?: FileList | FileSearch; error?: string } | null>(null)
  const listKey = `${cwd}|${hidden}|${sort.key}|${sort.asc}|${search}|${secrets.on}`
  useEffect(() => {
    const controller = new AbortController()
    const url = search ? filesUrl.search(cwd, search, hidden) : filesUrl.list(cwd, hidden, sort.key, sort.asc)
    api<FileList | FileSearch>(url, undefined, controller.signal).then(
      data => { if (controller.signal.aborted) return; setListing({ key: listKey, attempt: tick, data }); if ('secrets' in data) takeSecrets(data.secrets) },
      failure => { if (!controller.signal.aborted) setListing({ key: listKey, attempt: tick, error: errorText(failure) }) })
    return () => controller.abort()
  }, [api, cwd, hidden, sort.key, sort.asc, search, listKey, tick, takeSecrets])
  // Keep a failed load mounted during retries; other reloads keep their loading state.
  const current = listing?.key === listKey && (listing.attempt === tick || listing.error) ? listing : null
  const rows: Row[] = useMemo(() => {
    const data = current?.data
    const entries = data ? 'hits' in data ? data.hits : data.entries : []
    return entries.map(entry => {
      const full = entry.path ?? joinPath(cwd, entry.name)
      const where = search && entry.path ? parentOf(entry.path) : null
      return { ...entry, full, where: where && where !== cwd ? where.slice(cwd.length + 1) || undefined : undefined }
    })
  }, [current, cwd, search])
  const [lastRows, setLastRows] = useState<Row[]>([])
  if (current?.data && lastRows !== rows) setLastRows(rows)
  const shown = current ? rows : lastRows
  const picked = shown.filter(row => selected.includes(row.full))
  const secretHere = inSecret(cwd)
  const folderName = baseName(cwd)
  const canAdd = !!onAddToChat
  const addLabel = chatName ? `Add to ${chatName}` : 'Add to chat'

  const endSelect = () => { setSelecting(false); setSelected([]) }
  const toggle = (row: Row) => setSelected(list => list.includes(row.full) ? list.filter(path => path !== row.full) : [...list, row.full])
  const open = (row: Row) => { if (row.dir) go(row.full); else setPreview(row) }
  const tap = (row: Row) => { if (selecting) { if (!row.secret) toggle(row) } else open(row) }
  const addToChat = (paths: string[]) => { if (onAddToChat && paths.length) { endSelect(); setPreview(null); onAddToChat(paths) } }
  const download = (list: Row[]) => {
    if (list.length === 1 && !list[0].dir) { startDownload(filesUrl.raw(list[0].full, true), list[0].name); setToast(`Downloading ${list[0].name}.`) }
    else setDialog({ kind: 'zip', paths: list.map(row => row.full) })
  }
  const copyPath = async (path: string) => setToast(await copyText(path) ? `Copied ${path}` : 'Could not copy. Select the path and copy it.')
  const duplicate = async (row: Row) => {
    try {
      const result = await write<{ name: string; skipped?: { secret?: number } }>('files/duplicate', { path: row.full })
      setToast(`Made ${result.name}.${leftOut(result.skipped?.secret)}`); reload()
    } catch (failure) { setToast(errorText(failure)) }
  }
  const finish = (message: string) => { setDialog(null); endSelect(); setToast(message); reload() }

  // Uploads use the shared list in fileWrites, so they keep going (with their progress) when the person switches to Chats.
  const uploads = useUploads()
  const going = uploads.filter(item => item.state === 'waiting' || item.state === 'sending')
  const sending = going.find(item => item.state === 'sending') ?? going[0]
  const upload = sending && { name: sending.name, index: uploads.length - going.length, count: uploads.length, loaded: sending.sent, total: sending.size }
  const fileInput = useRef<HTMLInputElement>(null)
  const photoInput = useRef<HTMLInputElement>(null)
  const cwdRef = useRef(cwd)
  useEffect(() => { cwdRef.current = cwd }, [cwd])
  useEffect(() => {
    setUploadDone(item => { if (item.folder === cwdRef.current) setTick(n => n + 1) })
    return () => setUploadDone(null)
  }, [])
  // When the batch ends (also when the person comes back from Chats), say what happened once, then clear the list.
  const finished = !going.length && uploads.length > 0
  useEffect(() => {
    if (!finished) return
    const done = uploads.filter(item => item.state === 'done')
    const failed = uploads.filter(item => item.state === 'failed')
    const stopped = failed.filter(item => item.error === 'Stopped.').length
    const folders = [...new Set(uploads.map(item => item.folder))]
    const where = folders.length === 1 ? baseName(folders[0]) : `${folders.length} folders`
    const notes = failed.filter(item => item.error !== 'Stopped.').slice(0, 2).map(item => `${item.name}: ${item.error}`)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Acknowledge a completed external upload batch before clearing its shared store, including batches completed off-screen.
    setToast(!failed.length ? `Uploaded ${done.length === 1 ? done[0].name : `${done.length} files`} to ${where}.`
      : [stopped ? `Upload stopped. ${done.length} of ${uploads.length} uploaded.` : `Uploaded ${done.length} of ${uploads.length} to ${where}.`, ...notes].join(' '))
    clearUploads()
    setTick(n => n + 1)
  }, [finished, uploads])
  const sendFiles = (list: FileList_ | null) => {
    const files = [...(list ?? [])]
    if (files.length && !upload) startUploads(cwd, files)
  }

  const header = preview ? null : <header className="xp-head">
    <button type="button" className="icon-button" aria-label="Parent folder" disabled={!parentOf(cwd) || cwd === '/'} onClick={() => { const up = parentOf(cwd); if (up) go(up) }}><ChevronLeft size={24} /></button>
    <h1 className="xp-title">{folderName}</h1>
    {selecting ? <button type="button" className="xp-text-button" onClick={endSelect}>Done</button> : <>
      <button type="button" className="icon-button" aria-label="Search" aria-expanded={searchOpen} onClick={() => { setSearchOpen(!searchOpen); setQuery(''); setSearch('') }}><Search size={21} /></button>
      <button type="button" className="icon-button" aria-label="View options" aria-haspopup="dialog" onClick={() => setSheet({ kind: 'view' })}><SlidersHorizontal size={20} /></button>
      <button type="button" className="xp-text-button" onClick={() => { setSelecting(true); setSelected([]) }}>Select</button>
    </>}
  </header>

  return <section className="xp" aria-label="Files">
    {header}
    {!preview && <>
      {searchOpen && <label className="xp-search">
        <Search size={16} aria-hidden="true" />
        <span className="xp-sr">Search files</span>
        <input type="search" autoFocus value={query} placeholder={`Search in ${folderName}`} enterKeyHint="search" onChange={event => setQuery(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter') { setSearch(query.trim()); event.currentTarget.blur() } }} />
      </label>}
      <nav ref={wheelSideways} className="xp-crumbs" aria-label="Path">
        {crumbs(cwd).map((crumb, at, all) => at === all.length - 1
          ? <span key={crumb.path} className="xp-chip" aria-current="page">{crumb.label}</span>
          : <button key={crumb.path} type="button" className="xp-chip" onClick={() => go(crumb.path)}>{crumb.label}</button>)}
      </nav>
      {secrets.on && <div className="xp-secret-bar" role="status">
        <Lock size={14} aria-hidden="true" /><span>Secret folders show, read only. {minutesLeft} min left.</span>
        <button type="button" onClick={() => void switchSecrets(false)}>Hide</button>
      </div>}
      <div className="xp-bar">
        <span className="xp-count" role="status">{selecting ? `${picked.length} selected` : !current ? (search ? 'Searching…' : <Bone w="4.5em" />)
          : search ? `${shown.length}${current.data?.truncated ? '+' : ''} ${shown.length === 1 ? 'match' : 'matches'}` : `${items(shown.length)}${current.data?.truncated ? ' (first 5000)' : ''}`}</span>
        {selecting ? <button type="button" className="xp-sort" onClick={() => setSelected(picked.length ? [] : shown.filter(row => !row.secret).map(row => row.full))}>{picked.length ? 'Clear' : 'Select all'}</button>
          : !search && <button type="button" className="xp-sort" aria-label={`Sort: ${sort.label}. Tap to change.`} onClick={() => setStored(old => ({ ...old, sort: (old.sort + 1) % SORTS.length }))}><ArrowUpDown size={15} aria-hidden="true" />{sort.label}</button>}
      </div>
      <div className={`xp-list${selecting ? ' selecting' : ''}`} aria-busy={!current || current.attempt !== tick || undefined}>
        {current?.error ? <div className="xp-empty" role="alert">
            <p>{current.error}</p>
            <div className="xp-empty-actions">
              <RetryButton className="secondary" busy={!current || current.attempt !== tick} onRetry={reload} />
              {cwd !== '~' && <button type="button" className="secondary" onClick={() => go('~')}>Go to Home</button>}
            </div>
          </div>
          : <ul aria-label={search ? `Search results in ${folderName}` : `Contents of ${folderName}`}>
            {shown.map(row => {
              const on = selected.includes(row.full)
              return <li key={row.full} className={`xp-row${on ? ' on' : ''}${row.hidden || row.secret ? ' dim' : ''}`}>
                {selecting && <input type="checkbox" className="xp-check" aria-label={`Select ${row.name}`} checked={on} disabled={row.secret} onChange={() => toggle(row)} />}
                <button type="button" className="xp-row-main" onClick={() => tap(row)}>
                  <EntryIcon entry={row} />
                  <span className="xp-row-text">
                    <span className="xp-name"><span>{row.name}</span>
                      {row.secret && <Lock size={12} className="xp-lock" aria-label="Secret, read only" />}
                      {row.git && <span className="xp-git" title={gitText(row.git)} aria-label={gitText(row.git)}>{row.git}</span>}</span>
                    <span className="xp-meta">{[row.where, row.dir ? null : formatSize(row.size), formatModified(row.modified)].filter(Boolean).join(' · ')}</span>
                  </span>
                </button>
                {!selecting && <button type="button" className="icon-button xp-more" aria-label={`Actions for ${row.name}`} aria-haspopup="dialog" onClick={() => setSheet({ kind: 'row', row })}><MoreHorizontal size={20} /></button>}
              </li>
            })}
          </ul>}
        {!current && !search && <SkeletonRows count={8} label="Loading files" className="xp-skeleton" icon={24} />}
        {current && !current.error && !shown.length && <p className="xp-empty">{search ? `Nothing in ${folderName} matches “${search}”.` : secretHere ? 'This folder is empty.' : 'This folder is empty. Tap + to upload.'}</p>}
      </div>
      {!selecting && !secretHere && !upload && <button type="button" className="xp-fab" aria-label={`Add files or a folder to ${folderName}`} aria-haspopup="dialog" onClick={() => setSheet({ kind: 'add' })}><Plus size={26} /></button>}
      {selecting && <div className="xp-actionbar" role="toolbar" aria-label={`Actions for ${picked.length} selected`}>
        {canAdd && <button type="button" className="xp-act-accent" disabled={!picked.length} onClick={() => addToChat(picked.map(row => row.full))}><MessageSquarePlus size={20} aria-hidden="true" />Add to chat</button>}
        <button type="button" disabled={!picked.length} onClick={() => download(picked)}><Download size={20} aria-hidden="true" />Download</button>
        <button type="button" disabled={!picked.length} onClick={() => setDialog({ kind: 'pick', mode: 'copy', paths: picked.map(row => row.full) })}><FolderOutput size={20} aria-hidden="true" />Copy to</button>
        <button type="button" disabled={!picked.length} onClick={() => setDialog({ kind: 'pick', mode: 'move', paths: picked.map(row => row.full) })}><FolderInput size={20} aria-hidden="true" />Move to</button>
        <button type="button" className="xp-act-danger" disabled={!picked.length} onClick={() => setDialog({ kind: 'delete', rows: picked })}><Trash2 size={20} aria-hidden="true" />Delete</button>
      </div>}
    </>}

    {preview && <Preview api={api} row={preview} repo={current?.data && 'repo' in current.data ? !!current.data.repo : undefined} addLabel={canAdd ? addLabel : null} onBack={() => setPreview(null)} onMore={() => setSheet({ kind: 'row', row: preview })}
      onAdd={() => addToChat([preview.full])} onDownload={() => download([preview])} />}

    {upload && <div className="xp-upload" role="status" aria-live="polite">
      <div className="xp-upload-text"><strong>Uploading {upload.count > 1 ? `${upload.index + 1} of ${upload.count}` : ''}</strong><span>{upload.name}</span></div>
      <div className="meter-bar" role="progressbar" aria-label={`Upload of ${upload.name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={upload.total ? Math.round(upload.loaded / upload.total * 100) : 0}>
        <span style={{ width: `${upload.total ? Math.min(100, upload.loaded / upload.total * 100).toFixed(1) : 0}%` }} /></div>
      <div className="xp-upload-foot"><span>{formatSize(upload.loaded)} of {formatSize(upload.total)}</span><button type="button" onClick={stopUploads}>Stop</button></div>
    </div>}
    <input ref={fileInput} type="file" multiple hidden onChange={event => { sendFiles(event.target.files); event.target.value = '' }} />
    <input ref={photoInput} type="file" accept="image/*" capture="environment" hidden onChange={event => { sendFiles(event.target.files); event.target.value = '' }} />

    {sheet?.kind === 'add' && <ActionSheet title={`Add to ${folderName}`} onClose={() => setSheet(null)}>
      <button type="button" onClick={() => { setSheet(null); fileInput.current?.click() }}><Upload size={20} />Upload from phone</button>
      <button type="button" onClick={() => { setSheet(null); photoInput.current?.click() }}><Camera size={20} />Take photo</button>
      <button type="button" onClick={() => { setSheet(null); setDialog({ kind: 'mkdir' }) }}><FolderPlus size={20} />New folder</button>
      <p className="hint xp-sheet-note">Uploads up to 2 GB each. A name in use gets a number, like “photo (2).jpg”.</p>
    </ActionSheet>}
    {sheet?.kind === 'view' && <ActionSheet title="View" onClose={() => setSheet(null)}>
      <label className="switch-row"><span><strong>Hidden files</strong><small>Show names that start with a dot.</small></span>
        <input type="checkbox" role="switch" checked={hidden} onChange={event => setStored(old => ({ ...old, hidden: event.target.checked }))} /></label>
      <label className="switch-row"><span><strong>Secret folders</strong><small>{secrets.on ? `Read only. Turns off in ${minutesLeft} min.` : 'Folders such as .ssh and .config, and files such as .env. Read only, for 10 minutes.'}</small></span>
        <input type="checkbox" role="switch" checked={secrets.on} onChange={event => void switchSecrets(event.target.checked)} /></label>
    </ActionSheet>}
    {sheet?.kind === 'row' && (() => {
      const row = sheet.row
      const close = () => setSheet(null)
      const run = (action: () => void) => () => { close(); action() }
      const locked = !!row.secret || inSecret(row.full)
      return <ActionSheet title={row.name} sub={row.full} onClose={close}>
        {!(preview && preview.full === row.full) && <button type="button" onClick={run(() => open(row))}>{row.dir ? <Folder size={20} /> : <File size={20} />}{row.dir ? 'Open' : 'Preview'}</button>}
        {!locked && canAdd && <button type="button" title={addLabel} onClick={run(() => addToChat([row.full]))}><MessageSquarePlus size={20} /><span className="xp-action-label">{addLabel}</span></button>}
        {!locked && <>
          <button type="button" onClick={run(() => setDialog({ kind: 'rename', row }))}><Pencil size={20} />Rename</button>
          <button type="button" onClick={run(() => void duplicate(row))}><CopyPlus size={20} />Duplicate</button>
          <button type="button" onClick={run(() => setDialog({ kind: 'pick', mode: 'move', paths: [row.full] }))}><FolderInput size={20} />Move to…</button>
        </>}
        <button type="button" onClick={run(() => void copyPath(row.full))}><ClipboardCopy size={20} />Copy path</button>
        {!locked && !row.dir && onPin && <button type="button" title={onPin.label} onClick={run(() => onPin.run(row.full))}><BookMarked size={20} /><span className="xp-action-label">{onPin.label}</span></button>}
        {!locked && <>
          <button type="button" onClick={run(() => download([row]))}><Download size={20} />{row.dir ? 'Download as zip' : 'Download'}</button>
          <hr />
          <button type="button" className="danger-item" onClick={run(() => setDialog({ kind: 'delete', rows: [row] }))}><Trash2 size={20} />Delete</button>
        </>}
        {locked && <p className="hint xp-sheet-note">Secret items are read only. Download, writes and “Add to chat” stay off.</p>}
      </ActionSheet>
    })()}

    {(dialog?.kind === 'rename' || dialog?.kind === 'mkdir') && <NameDialog title={dialog.kind === 'mkdir' ? 'New folder' : `Rename ${dialog.row.name}`} start={dialog.kind === 'mkdir' ? 'New folder' : dialog.row.name}
      action={dialog.kind === 'mkdir' ? 'Create' : 'Save'} onClose={() => setDialog(null)}
      onSave={async name => {
        if (dialog.kind === 'mkdir') { const result = await write<{ name: string }>('files/mkdir', { path: cwd, name }); finish(`Made the folder ${result.name}.`) }
        else {
          const result = await write<{ path: string; name: string }>('files/rename', { path: dialog.row.full, name })
          if (preview?.full === dialog.row.full) setPreview({ ...preview, name: result.name, full: result.path })
          finish(`Renamed to ${result.name}.`)
        }
      }} />}
    {dialog?.kind === 'delete' && <DeleteDialog rows={dialog.rows} onClose={() => setDialog(null)} onDone={(result, count) => {
      if (preview && result.done.some(item => item.from === preview.full)) setPreview(null)
      finish(result.failed.length ? `Moved ${result.done.length} of ${count} to the trash. ${result.failed[0].error}` : `Moved ${count === 1 ? dialog.rows[0].name : items(count)} to the trash.`)
    }} />}
    {dialog?.kind === 'zip' && <ZipDialog api={api} paths={dialog.paths} onClose={() => setDialog(null)} onStart={name => { setDialog(null); endSelect(); setToast(`Downloading ${name}.`) }} />}
    {dialog?.kind === 'pick' && <FolderPicker api={api} mode={dialog.mode} paths={dialog.paths} start={cwd} hidden={hidden} onClose={() => setDialog(null)}
      onDone={(result, to) => {
        const verb = dialog.mode === 'copy' ? 'Copied' : 'Moved'
        if (dialog.mode === 'move' && preview && result.done.some(item => item.from === preview.full)) setPreview(null)
        finish(result.failed.length ? `${verb} ${result.done.length} of ${dialog.paths.length} to ${baseName(to)}. ${result.failed[0].error}`
          : `${verb} ${dialog.paths.length === 1 ? baseName(dialog.paths[0]) : items(dialog.paths.length)} to ${baseName(to)}.${leftOut(result.skipped?.secret)}`)
      }} />}
    {toast && <div className="toast app-toast xp-toast" role="status">{toast}</div>}
  </section>
}
type FileList_ = globalThis.FileList

function ActionSheet({ title, sub, onClose, children }: { title: string; sub?: string; onClose: () => void; children: ReactNode }) {
  const modal = useModal(true, onClose)
  return <dialog {...modal} className="sheet xp-sheet" aria-labelledby="xp-sheet-title">
    <div className="sheet-heading"><h2 id="xp-sheet-title">{title}</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    {sub && <p className="sheet-sub xp-mono">{sub}</p>}
    <div className="action-list">{children}</div>
  </dialog>
}

function NameDialog({ title, start, action, onClose, onSave }: { title: string; start: string; action: string; onClose: () => void; onSave: (name: string) => Promise<void> }) {
  const [name, setName] = useState(start)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!busy) onClose() })
  const input = useRef<HTMLInputElement>(null)
  // Select the name without its extension, as file managers do.
  useEffect(() => { const box = input.current; if (!box) return; const dot = start.lastIndexOf('.'); box.focus(); box.setSelectionRange(0, dot > 0 ? dot : start.length) }, [start])
  return <dialog {...modal} className="sheet xp-sheet" aria-labelledby="xp-name-title">
    <div className="sheet-heading"><h2 id="xp-name-title">{title}</h2><button type="button" className="icon-button" aria-label="Close" disabled={busy} onClick={onClose}><X /></button></div>
    <form className="rename-form" onSubmit={event => {
      event.preventDefault()
      const clean = name.trim()
      if (!clean || clean === '.' || clean === '..' || clean.includes('/')) { setError('Use a name without “/”.'); return }
      setBusy(true); setError('')
      onSave(clean).catch(failure => { setError(errorText(failure)); setBusy(false) })
    }}>
      <label htmlFor="xp-name-input">Name</label>
      <input id="xp-name-input" ref={input} value={name} maxLength={255} disabled={busy} autoComplete="off" autoCapitalize="off" spellCheck={false} onChange={event => setName(event.target.value)} />
      {error && <p className="error" role="alert">{error}</p>}
      <div className="sheet-buttons"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="primary" disabled={busy || !name.trim()}>{busy ? 'Saving…' : action}</button></div>
    </form>
  </dialog>
}

function DeleteDialog({ rows, onClose, onDone }: { rows: Row[]; onClose: () => void; onDone: (result: { done: { from: string }[]; failed: { path: string; error: string }[] }, count: number) => void }) {
  const host = useHost()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!busy) onClose() })
  const what = rows.length === 1 ? rows[0].name : items(rows.length)
  const run = async () => {
    setBusy(true); setError('')
    try { onDone(await write('files/delete', { paths: rows.map(row => row.full) }), rows.length) } catch (failure) { setError(errorText(failure)); setBusy(false) }
  }
  return <dialog {...modal} className="sheet xp-sheet" aria-labelledby="xp-delete-title">
    <div className="confirm-box" role="alertdialog" aria-labelledby="xp-delete-title" aria-describedby="xp-delete-text">
      <h2 id="xp-delete-title">Delete {what}?</h2>
      <p id="xp-delete-text">{rows.length === 1 ? 'It moves' : 'They move'} to the trash on {host}. You can restore {rows.length === 1 ? 'it' : 'them'} from a file manager’s trash.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="sheet-buttons"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button type="button" className="danger" disabled={busy} onClick={() => void run()}>{busy ? 'Moving…' : 'Move to trash'}</button></div>
    </div>
  </dialog>
}

interface SizeAnswer { files: number; folders: number; bytes: number; skipped?: { secret?: number }; partial?: boolean; note?: string | null }
/** The size step before a zip: the total and the count of secret items left out show first. */
function ZipDialog({ api, paths, onClose, onStart }: { api: Api; paths: string[]; onClose: () => void; onStart: (name: string) => void }) {
  const [size, setSize] = useState<SizeAnswer | null>(null)
  const [error, setError] = useState('')
  const modal = useModal(true, onClose)
  useEffect(() => {
    const controller = new AbortController()
    api<SizeAnswer>(sizeUrl(paths), undefined, AbortSignal.any([controller.signal, AbortSignal.timeout(20000)])).then(setSize, failure => { if (!controller.signal.aborted) setError(errorText(failure)) })
    return () => controller.abort()
  }, [api, paths])
  const name = paths.length === 1 ? `${baseName(paths[0])}.zip` : 'files.zip'
  const secret = size?.skipped?.secret ?? 0
  return <dialog {...modal} className="sheet xp-sheet" aria-labelledby="xp-zip-title">
    <div className="sheet-heading"><h2 id="xp-zip-title">Download {name}</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <div className="xp-zip" role="status">
      {error ? <p className="error" role="alert">{error}</p>
        : !size ? <p className="hint">Counting the files…</p>
        : <>
          {secret > 0 && <p className="xp-zip-secret"><Lock size={15} aria-hidden="true" />{secret} secret {secret === 1 ? 'item' : 'items'} left out.</p>}
          <p className="xp-zip-size">{size.partial ? 'At least ' : ''}{formatSize(size.bytes)}</p>
          <p className="hint">{size.files} {size.files === 1 ? 'file' : 'files'}{size.folders ? `, ${size.folders} ${size.folders === 1 ? 'folder' : 'folders'}` : ''}, before compression.{size.partial ? ' Counting took too long, so the total may be higher.' : ''}</p>
        </>}
    </div>
    <div className="sheet-buttons"><button type="button" className="secondary" onClick={onClose}>Cancel</button>
      <button type="button" className="primary" disabled={!size} onClick={() => { startDownload(zipUrl(paths), name); onStart(name) }}><Download size={17} aria-hidden="true" />Download zip</button></div>
  </dialog>
}

/** Copy to / Move to: walk the folders, then copy or move here. */
function FolderPicker({ api, mode, paths, start, hidden, onClose, onDone }: { api: Api; mode: 'copy' | 'move'; paths: string[]; start: string; hidden: boolean; onClose: () => void; onDone: (result: Result, to: string) => void }) {
  const [folder, setFolder] = useState(start)
  const [list, setList] = useState<{ path: string; rows?: Row[]; error?: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!busy) onClose() })
  useEffect(() => {
    const controller = new AbortController()
    api<FileList>(filesUrl.list(folder, hidden, 'name', true), undefined, controller.signal).then(
      data => setList({ path: folder, rows: data.entries.filter(entry => entry.dir && !entry.secret).map(entry => ({ ...entry, full: joinPath(folder, entry.name) })) }),
      failure => { if (!controller.signal.aborted) setList({ path: folder, error: errorText(failure) }) })
    return () => controller.abort()
  }, [api, folder, hidden])
  const current = list?.path === folder ? list : null
  const inside = paths.some(path => folder === path || folder.startsWith(`${path}/`))
  const same = mode === 'move' && paths.every(path => parentOf(path) === folder)
  const blocked = inside ? `${mode === 'copy' ? 'Copy' : 'Move'} cannot go into a folder it is ${mode === 'copy' ? 'copying' : 'moving'}.` : same ? 'The items are already in this folder.' : inSecret(folder) ? 'Secret folders are read only.' : ''
  const verb = mode === 'copy' ? 'Copy' : 'Move'
  const run = async () => {
    setBusy(true); setError('')
    try { onDone(await write<Result>(`files/${mode}`, { paths, to: folder }), folder) } catch (failure) { setError(errorText(failure)); setBusy(false) }
  }
  return <dialog {...modal} className="xp-picker" aria-labelledby="xp-pick-title">
    <header className="xp-head">
      <button type="button" className="icon-button" aria-label="Parent folder" disabled={!parentOf(folder) || busy} onClick={() => { const up = parentOf(folder); if (up) setFolder(up) }}><ChevronLeft size={24} /></button>
      <div className="xp-pick-title"><h2 id="xp-pick-title">{verb} {paths.length === 1 ? baseName(paths[0]) : items(paths.length)}</h2><span>Choose a folder</span></div>
      <button type="button" className="icon-button" aria-label="Close" disabled={busy} onClick={onClose}><X /></button>
    </header>
    <nav ref={wheelSideways} className="xp-crumbs" aria-label="Folder path">
      {crumbs(folder).map((crumb, at, all) => at === all.length - 1 ? <span key={crumb.path} className="xp-chip" aria-current="page">{crumb.label}</span>
        : <button key={crumb.path} type="button" className="xp-chip" disabled={busy} onClick={() => setFolder(crumb.path)}>{crumb.label}</button>)}
    </nav>
    <div className="xp-list">
      {current?.error ? <p className="xp-empty" role="alert">{current.error}</p>
        : !current ? <SkeletonRows count={8} label="Loading files" className="xp-skeleton" icon={24} />
        : !current.rows?.length ? <p className="xp-empty">No folders in here.</p>
        : <ul aria-label={`Folders in ${baseName(folder)}`}>{current.rows.map(row => <li key={row.full} className={`xp-row${row.hidden ? ' dim' : ''}`}>
            <button type="button" className="xp-row-main" disabled={busy || paths.includes(row.full)} onClick={() => setFolder(row.full)}>
              <EntryIcon entry={row} /><span className="xp-row-text"><span className="xp-name"><span>{row.name}</span></span><span className="xp-meta">{formatModified(row.modified)}</span></span>
            </button>
          </li>)}</ul>}
    </div>
    <footer className="xp-pick-foot">
      {(error || blocked) && <p className={error ? 'error' : 'hint'} role={error ? 'alert' : undefined}>{error || blocked}</p>}
      <button type="button" className="primary" disabled={busy || !!blocked} onClick={() => void run()}>{busy ? `${verb === 'Copy' ? 'Copying' : 'Moving'}…` : `${verb} to ${baseName(folder)}`}</button>
    </footer>
  </dialog>
}

const NO_PREVIEW: Record<string, string> = {
  'too-large': 'Too large to preview on a phone. Download it, or add it to a chat so the agent reads it.',
  binary: 'No preview for this kind of file. Download it to open it.',
  secret: 'Secret file. Only text shows here. Download stays off.',
  unreadable: 'Tether cannot read this file. Check its permissions.',
}

/** The full-screen preview: text with line numbers, an image or player, or a note. "Add to chat" sits at the bottom. */
function Preview({ api, row, repo, addLabel, onBack, onMore, onAdd, onDownload }: { api: Api; row: Row; repo?: boolean; addLabel: string | null; onBack: () => void; onMore: () => void; onAdd: () => void; onDownload: () => void }) {
  const [state, setState] = useState<{ path: string; data?: FilePreview; error?: string } | null>(null)
  const [office, setOffice] = useState<OfficeFile | null>(null)
  const [editing, setEditing] = useState<TextFile | null>(null)
  // Goes up when the editor closes, so the preview shows saved edits.
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    api<FilePreview>(filesUrl.preview(row.full), undefined, controller.signal).then(data => setState({ path: row.full, data }), failure => { if (!controller.signal.aborted) setState({ path: row.full, error: errorText(failure) }) })
    return () => controller.abort()
  }, [api, row.full, version])
  const body = useRef<HTMLDivElement>(null)
  useEffect(() => { body.current?.focus({ preventScroll: true }) }, [row.full])
  const current = state?.path === row.full ? state : null
  const data = current?.data
  const locked = !!row.secret || inSecret(row.full)
  return <><div className="xp-preview" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); onBack() } }}>
    <header className="xp-head xp-preview-head">
      <button type="button" className="icon-button" aria-label="Back to folder" onClick={onBack}><ChevronLeft size={24} /></button>
      <div className="xp-pick-title"><h1>{row.name}</h1><span>{[kindLabel(row), formatSize(row.size), formatModified(row.modified)].join(' · ')}</span></div>
      {!locked && data?.kind === 'text' && <button type="button" className="icon-button" aria-label={`Edit ${row.name}`} onClick={() => setEditing({ path: row.full, url: filesUrl.raw(row.full), repo, edit: true })}><Pencil size={20} /></button>}
      <button type="button" className="icon-button" aria-label={`Actions for ${row.name}`} aria-haspopup="dialog" onClick={onMore}><MoreHorizontal size={20} /></button>
    </header>
    <div className="xp-preview-body" ref={body} tabIndex={-1} aria-label={`Preview of ${row.name}`} aria-busy={!current || undefined}>
      {current?.error ? <p className="xp-note" role="alert">{current.error}</p>
        : !data ? <Loading className="xp-note" label="Loading preview…" />
        : !locked && !row.dir && isOffice(row.name) ? <p className="xp-note"><button type="button" className="secondary" onClick={() => setOffice({ path: row.full, url: filesUrl.raw(row.full) })}>Open in viewer</button></p>
        : data.kind === 'text' ? <div className="xp-code">
            {data.lines.length ? data.lines.map((line, at) => <div key={at} className="xp-line"><span className="xp-ln" aria-hidden="true">{at + 1}</span><span className="xp-lt">{line || ' '}</span></div>)
              : <p className="xp-note">This file is empty.</p>}
            {data.truncated && <p className="xp-note">The preview shows the start of the file.</p>}
          </div>
        : data.kind === 'image' ? <div className="xp-media"><img src={data.url} alt={row.name} /></div>
        : data.kind === 'video' ? <div className="xp-media"><video src={data.url} controls playsInline preload="metadata" /></div>
        : data.kind === 'audio' ? <div className="xp-media"><audio src={data.url} controls preload="metadata" /></div>
        : data.kind === 'pdf' ? <p className="xp-note">PDF file. <a href={data.url} target="_blank" rel="noopener">Open the PDF</a></p>
        : 'reason' in data ? <p className="xp-note">{NO_PREVIEW[data.reason] ?? data.reason}</p> : null}
    </div>
    {!locked ? <div className="xp-preview-foot">
      {addLabel && <button type="button" className="primary" onClick={onAdd}><MessageSquarePlus size={18} aria-hidden="true" /><span>{addLabel}</span></button>}
      <button type="button" className="secondary xp-square" aria-label={`Download ${row.name}`} onClick={onDownload}><Download size={20} /></button>
    </div> : <p className="xp-preview-foot hint">Secret file: read only. Download and “Add to chat” stay off.</p>}
  </div>
    <OfficeViewer file={!locked && office?.path === row.full ? office : null} close={() => setOffice(null)} />
    <TextViewer file={!locked && editing?.path === row.full ? editing : null} close={() => { setEditing(null); setVersion(value => value + 1) }} />
  </>
}
