import { Loading } from './Skeleton'
import { useEffect, useState } from 'react'
import { ArrowUp, Check, Copy, Download, FolderOpen, X } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import type { Anchor } from './hooks'
import { PopMenu } from './PanelColumn'
import { baseName, crumbs, filesUrl, formatSize, parentOf } from './files'
import type { FileList } from './files'
import { clearUploads, useUploads, writes } from './fileWrites'
import type { SizeInfo } from './fileWrites'
import { sentence, useHost } from './host'

// The Files explorer's write actions (slice 3): the item menu, the trash and zip confirms, the folder picker and the upload panel.

export interface MenuItem { label: string; icon: typeof Copy; run: () => void; key?: string; danger?: boolean; gap?: boolean }
/** The ⋯ menu of a row, or the right-click menu. */
export function ItemMenu({ at, title, items, onClose }: { at: Anchor; title: string; items: MenuItem[]; onClose: () => void }) {
  return <PopMenu at={at} label={`${title} options`} onClose={onClose}>
    <div className="sheet-heading"><h2>{title}</h2></div>
    <div className="action-list" role="menu">
      {items.map(item => <div key={item.label} className="fx-menu-row">
        {item.gap && <hr />}
        <button type="button" role="menuitem" className={item.danger ? 'danger-item' : undefined} onClick={() => { onClose(); item.run() }}>
          <item.icon size={16} aria-hidden="true" />{item.label}{item.key && <kbd>{item.key}</kbd>}
        </button>
      </div>)}
    </div>
  </PopMenu>
}

const itemsText = (names: string[]) => names.length === 1 ? names[0] : `${names.length} items`

/** Delete moves items to the server trash. There is no hard delete. */
export function TrashConfirm({ names, onCancel, onConfirm }: { names: string[]; onCancel: () => void; onConfirm: () => Promise<string | null> }) {
  const host = useHost()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!pending) onCancel() })
  const go = async () => {
    setPending(true); setError('')
    const failure = await onConfirm()
    setPending(false)
    if (failure) setError(failure)
  }
  return <dialog {...modal} className="sheet fx-dialog" aria-labelledby="fx-trash-title">
    <div className="confirm-box" role="alertdialog" aria-labelledby="fx-trash-title" aria-describedby="fx-trash-text">
      <h2 id="fx-trash-title">Move {names.length === 1 ? `“${names[0]}”` : `${names.length} items`} to the trash?</h2>
      <p id="fx-trash-text">{sentence(host)} keeps {names.length === 1 ? 'it' : 'them'} in its trash. You can restore {names.length === 1 ? 'it' : 'them'} from the Trash in a file manager on {host}.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="sheet-buttons">
        <button type="button" className="secondary" disabled={pending} onClick={onCancel}>Cancel</button>
        <button type="button" className="danger" disabled={pending} onClick={() => void go()}>{pending ? 'Moving…' : 'Move to trash'}</button>
      </div>
    </div>
  </dialog>
}

/** A folder or several items download as one streamed zip. Show the total first (Mak's decision 4: no cap). */
export function ZipConfirm({ api, paths, onClose }: { api: Api; paths: string[]; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const [size, setSize] = useState<SizeInfo | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    writes.size(api, paths, controller.signal).then(setSize, failure => { if (!controller.signal.aborted) setError(errorText(failure)) })
    return () => controller.abort()
  }, [api, paths])
  const name = paths.length === 1 ? `${baseName(paths[0])}.zip` : 'files.zip'
  return <dialog {...modal} className="sheet fx-dialog" aria-labelledby="fx-zip-title">
    <div className="confirm-box">
      <h2 id="fx-zip-title">Download {name}</h2>
      {error ? <p className="error" role="alert">{error}</p>
        : !size ? <p role="status">Counting the files…</p>
        : <p role="status">{formatSize(size.bytes)}{size.partial ? ' or more' : ''} in {size.files} {size.files === 1 ? 'file' : 'files'}{size.folders ? ` and ${size.folders} ${size.folders === 1 ? 'folder' : 'folders'}` : ''}, before compression.{size.note ? ` ${size.note}` : ''}</p>}
      <div className="sheet-buttons">
        <button type="button" className="secondary" onClick={onClose}>Cancel</button>
        {size && !error && <a className="primary" href={writes.zipUrl(paths)} download={name} autoFocus onClick={() => setTimeout(onClose)}><Download size={16} aria-hidden="true" />Download</a>}
      </div>
    </div>
  </dialog>
}

/** "Copy to…" and "Move to…": pick a folder. Secret folders do not show. */
export function FolderPicker({ api, move, names, start, sources, onCancel, onPick }: { api: Api; move: boolean; names: string[]; start: string; sources: string[]; onCancel: () => void; onPick: (folder: string) => Promise<string | null> }) {
  const [pending, setPending] = useState(false)
  const modal = useModal(true, () => { if (!pending) onCancel() })
  const [folder, setFolder] = useState(start)
  const [listing, setListing] = useState<{ path: string; folders?: string[]; error?: string } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api<FileList>(filesUrl.list(folder, false, 'name', true), undefined, controller.signal).then(
      data => setListing({ path: folder, folders: data.entries.filter(entry => entry.dir && !entry.secret).map(entry => entry.name) }),
      failure => { if (!controller.signal.aborted) setListing({ path: folder, error: errorText(failure) }) })
    return () => controller.abort()
  }, [api, folder])
  const shown = listing?.path === folder ? listing : null
  // A folder cannot go into itself. Moving to the folder the items are in changes nothing.
  const blocked = sources.some(path => folder === path || folder.startsWith(`${path}/`))
  const same = move && sources.every(path => parentOf(path) === folder)
  const verb = move ? 'Move' : 'Copy'
  const pick = async () => {
    setPending(true); setError('')
    const failure = await onPick(folder)
    setPending(false)
    if (failure) setError(failure)
  }
  return <dialog {...modal} className="sheet fx-dialog fx-picker" aria-labelledby="fx-pick-title">
    <div className="sheet-heading"><h2 id="fx-pick-title">{verb} {itemsText(names)} to…</h2><button type="button" className="icon-button" aria-label="Close" disabled={pending} onClick={onCancel}><X /></button></div>
    <div className="fx-pick-bar">
      <button type="button" className="fx-ib" aria-label="Parent folder" disabled={!parentOf(folder)} onClick={() => { const up = parentOf(folder); if (up) setFolder(up) }}><ArrowUp size={16} /></button>
      <nav className="fx-pick-path" aria-label="Folder">{crumbs(folder).map((crumb, at, all) => <span key={crumb.path}>
        {at > 0 && <span className="fx-pick-sep" aria-hidden="true">/</span>}
        {at === all.length - 1 ? <span aria-current="location">{crumb.label}</span> : <button type="button" onClick={() => setFolder(crumb.path)}>{crumb.label}</button>}
      </span>)}</nav>
    </div>
    <ul className="fx-pick-list" aria-label={`Folders in ${baseName(folder)}`} aria-busy={!shown || undefined}>
      {shown?.error ? <li className="error" role="alert">{shown.error}</li>
        : !shown ? <Loading as="li" className="fx-pick-note" />
        : !shown.folders?.length ? <li className="fx-pick-note">No folders here.</li>
        : shown.folders.map(name => <li key={name}><button type="button" onClick={() => setFolder(folder === '/' ? `/${name}` : `${folder}/${name}`)}><FolderOpen size={15} aria-hidden="true" />{name}</button></li>)}
    </ul>
    {error && <p className="error" role="alert">{error}</p>}
    {blocked && <p className="fx-pick-note" role="status">A folder cannot go into itself. Choose another folder.</p>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" disabled={pending} onClick={onCancel}>Cancel</button>
      <button type="button" className="primary" disabled={pending || blocked || same} onClick={() => void pick()}><Check size={16} aria-hidden="true" />{pending ? `${move ? 'Moving' : 'Copying'}…` : `${verb} to ${baseName(folder)}`}</button>
    </div>
  </dialog>
}

/** Uploads in progress, bottom right. They keep going on other screens; this panel shows on the Files screen. */
export function UploadPanel() {
  const uploads = useUploads()
  if (!uploads.length) return null
  const going = uploads.filter(item => item.state === 'waiting' || item.state === 'sending')
  const failed = uploads.filter(item => item.state === 'failed').length
  // Failed files first, so a failure never hides under "and N more"; then the file in progress, the queue, and the finished files.
  const shown = (['failed', 'sending', 'waiting', 'done'] as const).flatMap(state => uploads.filter(item => item.state === state)).slice(0, 6)
  const folders = [...new Set(uploads.map(item => item.folder))]
  const where = folders.length === 1 ? baseName(folders[0]) : `${folders.length} folders`
  const title = going.length ? `Uploading ${going.length} ${going.length === 1 ? 'file' : 'files'} to ${where}`
    : failed ? (failed === uploads.length ? `${failed} ${failed === 1 ? 'upload' : 'uploads'} failed` : `${failed} of ${uploads.length} uploads failed`) : `Uploaded ${uploads.length} ${uploads.length === 1 ? 'file' : 'files'} to ${where}`
  return <section className="fx-uploads" aria-label="Uploads">
    <div className="fx-uploads-head">
      <h2 role="status">{title}</h2>
      {!going.length && <button type="button" className="fx-ib" aria-label="Hide uploads" onClick={clearUploads}><X size={14} /></button>}
    </div>
    <ul>{shown.map(item => {
      const percent = item.size ? Math.floor(item.sent / item.size * 100) : item.state === 'done' ? 100 : 0
      const state = item.state === 'waiting' ? 'Waiting' : item.state === 'sending' ? `${percent}%` : item.state === 'done' ? 'Done' : 'Failed'
      return <li key={item.id} data-state={item.state}>
        <div className="fx-upload-line"><span className="fx-mono fx-upload-name">{item.name}</span><span className="fx-upload-state">{state}</span></div>
        {item.state === 'sending' && <div className="fx-meter" role="progressbar" aria-label={`Upload of ${item.name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><span style={{ width: `${percent}%` }} /></div>}
        {item.error && <p className="fx-upload-error">{item.error}</p>}
      </li>
    })}</ul>
    {uploads.length > shown.length && <p className="fx-sub">and {uploads.length - shown.length} more</p>}
    {going.length > 0 && <p className="fx-sub">Uploads keep going if you switch to a chat.</p>}
  </section>
}
