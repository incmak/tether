import { Loading } from './Skeleton'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { FolderOpen, Pencil, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useModal } from './hooks'
import { useHost } from './host'
import { NetworkError, reach } from './api'
import { saveText, SaveError, sha256Hex } from './textEdit'
import type { EditorHandle } from './textEdit'
import './TextEditor.css'

const SourceEditor = lazy(() => import('./SourceEditor'))
const RichEditor = lazy(() => import('./RichEditor'))

/** repo: false when the file is known to be outside a Git repository. Markdown there opens in the formatted editor.
 *  edit: open straight in the editor; Done then closes the viewer. */
/** readOnly: show the text without the edit controls (the source of a previewed page). */
export type TextFile = { path: string; url: string; line?: number; repo?: boolean; edit?: boolean; readOnly?: boolean }
const MAX_LINES = 20000
/** Larger files open read-only. The server takes up to 3 MB of JSON. */
const MAX_EDIT_BYTES = 2 * 1024 * 1024
const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path)

// Preview of a Markdown file. Raw HTML in the file is dropped. Web links open in a new tab; other links and images show as text,
// because a relative path would resolve against Tether's own URL.
const DOC: Components = {
  a: ({ href = '', children }) => /^(https?|mailto):/i.test(href)
    ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
    : <span className="doc-link" title={href}>{children}</span>,
  img: ({ alt, src }) => <span className="doc-image" title={typeof src === 'string' ? src : undefined}>{alt || 'Image'}</span>,
  table: props => <div className="doc-table"><table {...props} /></div>,
}

type Loaded = { url: string; text?: string; lines?: string[]; bytes?: Uint8Array; editable?: boolean; error?: string; unreachable?: boolean }
type EditView = 'rich' | 'source'
/** seed: the text the editor mounts with. mount changes to start the editor again from seed. carried: unsaved edits came from the other view. */
type Editing = { url: string; view: EditView; seed: string; base: string; mount: number; carried: boolean }
type SaveState = { status: 'idle' | 'saving' | 'saved' } | { status: 'error'; message: string } | { status: 'conflict' }

async function load(url: string, signal?: AbortSignal): Promise<{ text: string; bytes: Uint8Array; editable: boolean }> {
  const response = await reach(url, { signal, cache: 'no-store' })
  if (!response.ok) throw new Error(((await response.json().catch(() => null)) as { error?: string } | null)?.error ?? 'Tether cannot open this file.')
  const bytes = new Uint8Array(await response.arrayBuffer())
  try { return { text: new TextDecoder('utf-8', { fatal: true }).decode(bytes), bytes, editable: true } }
  catch { return { text: new TextDecoder().decode(bytes), bytes, editable: false } }
}

const sessionOf = (url: string) => new URL(url, location.href).searchParams.get('session')
const split = (text: string) => text.replace(/\n$/, '').split('\n')

// View, and edit, a source, log or Markdown file. It opens at the named line and marks it.
/** onShow: a "Show in Files" button (a chat on desktop). */
export function TextViewer({ file, close, onShow }: { file: TextFile | null; close: () => void; onShow?: () => void }) {
  const host = useHost()
  const [state, setState] = useState<Loaded | null>(null)
  const [editing, setEditing] = useState<Editing | null>(null)
  const [dirty, setDirty] = useState(false)
  const [save, setSave] = useState<SaveState>({ status: 'idle' })
  const handle = useRef<EditorHandle | null>(null)
  const edit = file && editing?.url === file.url ? editing : null
  const unsaved = !!edit && (dirty || edit.carried)
  const leave = (then: () => void) => {
    if (unsaved && !confirm('Discard unsaved changes?')) return
    setEditing(null); setDirty(false); setSave({ status: 'idle' }); then()
  }
  const guardedClose = () => leave(close)
  const modal = useModal(!!file, guardedClose)
  // The view each file opens in: Preview for Markdown, Source when a line is named or the file is not Markdown.
  const [chosen, setChosen] = useState<{ url: string; preview: boolean } | null>(null)
  const body = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!file) return
    const abort = new AbortController()
    load(file.url, abort.signal)
      .then(({ text, bytes, editable }) => setState({ url: file.url, text, lines: split(text), bytes, editable }),
        failure => { if (!abort.signal.aborted) setState({ url: file.url, ...(failure instanceof NetworkError ? { unreachable: true } : { error: String(failure.message) }) }) })
    return () => abort.abort()
  }, [file])
  // Warn before the tab closes with unsaved edits.
  useEffect(() => {
    if (!unsaved) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault() }
    addEventListener('beforeunload', warn)
    return () => removeEventListener('beforeunload', warn)
  }, [unsaved])
  const current = file && state?.url === file.url ? state : null
  const lines = current?.lines ?? []
  const target = file?.line && file.line <= lines.length ? file.line : undefined
  // A huge file shows a window of lines around the target.
  const first = lines.length > MAX_LINES ? Math.max(0, Math.min((target ?? 1) - 1 - MAX_LINES / 2, lines.length - MAX_LINES)) : 0
  const shown = lines.slice(first, first + MAX_LINES)
  const markdown = !!file && isMarkdown(file.path)
  const preview = markdown && (file && chosen?.url === file.url ? chosen.preview : !file?.line)
  useEffect(() => {
    if (edit || !current?.lines || preview) { body.current?.scrollTo(0, 0); return }
    const row = target ? body.current?.querySelector(`[data-line="${target}"]`) : null
    if (row) row.scrollIntoView({ block: 'center' })
    else body.current?.scrollTo(0, 0)
  }, [current, target, preview, edit])
  const canEdit = !file?.readOnly && !!current?.editable && !!current.bytes && current.bytes.length <= MAX_EDIT_BYTES && lines.length <= MAX_LINES

  const startEdit = async () => {
    if (!file || !current?.bytes || current.text === undefined) return
    const base = await sha256Hex(current.bytes)
    // Rich editing writes the Markdown again in its own style. In a repository that makes noisy diffs, so Source comes first there.
    const view: EditView = markdown && file.repo === false ? 'rich' : 'source'
    setDirty(false); setSave({ status: 'idle' })
    setEditing({ url: file.url, view, seed: current.text, base, mount: 0, carried: false })
  }
  // A file opened to edit starts the editor once it loads.
  const started = useRef<string | null>(null)
  useEffect(() => {
    if (!file?.edit || !canEdit || started.current === file.url) return
    started.current = file.url
    void startEdit()
  })
  useEffect(() => { if (!file) started.current = null }, [file])
  const switchView = (view: EditView) => {
    if (!edit || edit.view === view) return
    // Unchanged text keeps its seed, so a look at Rich and back does not restyle the file.
    const text = dirty ? handle.current?.text() ?? edit.seed : edit.seed
    setEditing({ ...edit, view, seed: text, mount: edit.mount + 1, carried: edit.carried || dirty })
    setDirty(false)
  }
  const store = async (base: string) => {
    if (!file || !edit || !handle.current || save.status === 'saving') return
    const text = handle.current.text()
    setSave({ status: 'saving' })
    try {
      const next = await saveText(file.path, sessionOf(file.url), text, base)
      handle.current?.saved()
      const bytes = new TextEncoder().encode(text)
      setState({ url: file.url, text, lines: split(text), bytes, editable: true })
      setEditing(value => value && { ...value, base: next, seed: text, carried: false })
      setDirty(false)
      setSave({ status: 'saved' })
    } catch (failure) {
      if (failure instanceof SaveError && failure.code === 'changed') setSave({ status: 'conflict' })
      else setSave({ status: 'error', message: failure instanceof Error ? failure.message : 'Save failed. Try again.' })
    }
  }
  // Conflict: Keep mine saves over the disk version; Load disk version drops the edits.
  const keepMine = async () => {
    if (!file) return
    let base: string
    try { base = await sha256Hex((await load(file.url)).bytes) }
    catch { setSave({ status: 'error', message: `Cannot reach ${host}. Your edits are still here. Try again.` }); return }
    await store(base)
  }
  const loadDisk = async () => {
    if (!file || !edit) return
    try {
      const disk = await load(file.url)
      setState({ url: file.url, text: disk.text, lines: split(disk.text), bytes: disk.bytes, editable: disk.editable })
      if (!disk.editable) { setEditing(null); setDirty(false); setSave({ status: 'idle' }); return }
      setEditing({ ...edit, seed: disk.text, base: await sha256Hex(disk.bytes), mount: edit.mount + 1, carried: false })
      setDirty(false); setSave({ status: 'idle' })
    } catch { setSave({ status: 'error', message: `Cannot reach ${host}. Try again.` }) }
  }
  const onDirty = (value: boolean) => { setDirty(value); if (value) setSave(old => old.status === 'saved' ? { status: 'idle' } : old) }
  const onSave = () => { if (edit) void store(edit.base) }

  const name = file?.path.split('/').pop() || file?.path
  const saveLabel = save.status === 'saving' ? 'Saving…' : save.status === 'saved' && !unsaved ? 'Saved' : 'Save'
  return <dialog {...modal} className={`text-viewer${edit ? ' editing' : ''}`} aria-labelledby="text-viewer-title">{file && <>
    <div className="text-viewer-head">
      <div className="text-viewer-title"><h2 id="text-viewer-title">{name}{target && !edit ? <span>:{target}</span> : null}</h2><p title={file.path}><bdi>{file.path}</bdi></p></div>
      {edit ? <>
        <button type="button" className="primary sm" disabled={!unsaved || save.status === 'saving'} onClick={onSave}>{saveLabel}</button>
        <button type="button" className="secondary sm" onClick={() => leave(file.edit ? close : () => {})}>Done</button>
      </> : <>
        {markdown && <div className="view-switch" role="group" aria-label="View">
          {(['Preview', 'Source'] as const).map(view => <button key={view} type="button" aria-pressed={preview === (view === 'Preview')}
            onClick={() => setChosen({ url: file.url, preview: view === 'Preview' })}>{view}</button>)}
        </div>}
        {canEdit && <button type="button" className="viewer-files" onClick={() => void startEdit()}><Pencil size={15} aria-hidden="true" />Edit</button>}
        {onShow && <button type="button" className="viewer-files" onClick={onShow}><FolderOpen size={15} aria-hidden="true" />Show in Files</button>}
      </>}
      <button type="button" className="icon-button" aria-label="Close" onClick={guardedClose}><X /></button>
    </div>
    {edit && (markdown || save.status === 'error' || save.status === 'conflict') && <div className="text-edit-bar">
      {markdown && <div className="view-switch" role="group" aria-label="Editor">
        {([['rich', 'Rich'], ['source', 'Source']] as const).map(([view, label]) => <button key={view} type="button" aria-pressed={edit.view === view}
          onClick={() => switchView(view)}>{label}</button>)}
      </div>}
      {markdown && edit.view === 'rich' && file.repo !== false && save.status !== 'error' && save.status !== 'conflict' && <p className="text-edit-hint">Rich editing can restyle unchanged Markdown.</p>}
      {save.status === 'error' && <p className="text-edit-note" role="alert">{save.message}</p>}
      {save.status === 'conflict' && <div className="text-edit-note" role="alert">
        <p>The file changed on disk after you opened it.</p>
        <button type="button" className="secondary sm" onClick={() => void keepMine()}>Keep mine</button>
        <button type="button" className="ghost sm" onClick={() => void loadDisk()}>Load disk version</button>
      </div>}
    </div>}
    <div className="text-viewer-body" ref={body} tabIndex={edit ? undefined : 0} aria-label={edit ? undefined : `Contents of ${name}`}>
      {current?.error || current?.unreachable ? <p className="text-viewer-note" role="alert">{current.unreachable ? `Cannot reach ${host}.` : current.error}</p>
        : !current ? <Loading className="text-viewer-note" />
        : edit ? <Suspense fallback={<Loading className="text-viewer-note" label="Loading editor…" />}>
          {edit.view === 'rich'
            ? <RichEditor key={`r${edit.mount}`} initial={edit.seed} path={file.path} handleRef={handle} onDirty={onDirty} onSave={onSave} />
            : <SourceEditor key={`s${edit.mount}`} initial={edit.seed} path={file.path} line={edit.mount === 0 ? target : undefined} handleRef={handle} onDirty={onDirty} onSave={onSave} />}
        </Suspense>
        : preview ? <article className="text-viewer-doc"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={DOC}>{current.text}</ReactMarkdown></article>
        : <pre className="text-viewer-source" style={{ '--gutter': `${String(first + shown.length).length + 1}ch` } as React.CSSProperties}>{first > 0 && <span className="text-viewer-skip">{first} lines above not shown</span>}
          {shown.map((text, at) => <span key={at} data-line={first + at + 1} className={first + at + 1 === target ? 'marked' : undefined}>{text || ' '}</span>)}
          {first + shown.length < lines.length && <span className="text-viewer-skip">{lines.length - first - shown.length} lines below not shown</span>}</pre>}
    </div>
  </>}</dialog>
}
