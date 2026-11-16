import { Loading } from './Skeleton'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { Download, FolderOpen, Pencil, X } from 'lucide-react'
import type { WorkBook } from 'xlsx'
import { errorText, NetworkError, reach, request } from './api'
import { useModal } from './hooks'
import { useHost, useOfficeEditing } from './host'
import { wheelSideways } from './wheelSideways'

export type OfficeFile = { path: string; url: string }
/** Office files Tether shows in its own viewer: spreadsheets, Word documents and slide decks. */
export const OFFICE_SUFFIXES = ['xlsx', 'xlsm', 'xls', 'ods', 'docx', 'pptx']
const suffixOf = (path: string) => path.split('.').pop()?.toLowerCase() ?? ''
export const isOffice = (path: string) => OFFICE_SUFFIXES.includes(suffixOf(path))
/** Files the Office editor add-on can edit. */
const EDITABLE = ['docx', 'xlsx', 'pptx']

// The file is read in the browser: the libraries load only when a file of their type opens, and nothing leaves Tether.
// External images and links in a file stay off, so opening a file never calls another site.
const MAX_BYTES = 50 * 1024 * 1024
const MAX_ROWS = 2000
const MAX_COLUMNS = 200
const UNREADABLE = 'Tether cannot read this file. Download it to open it in another app.'

type Loaded = { url: string; error?: string; unreachable?: boolean; sheet?: WorkBook; data?: ArrayBuffer }

/** Read-only view of a spreadsheet, a Word document or a slide deck. onShow: a "Show in Files" button. */
export function OfficeViewer({ file, close, onShow }: { file: OfficeFile | null; close: () => void; onShow?: () => void }) {
  const host = useHost()
  const modal = useModal(!!file, close)
  const editing = useOfficeEditing()
  const [state, setState] = useState<Loaded | null>(null)
  // The file the editor has open. Leaving the editor loads the file again, because a save can change it.
  const [edited, setEdited] = useState<string | null>(null)
  const [round, setRound] = useState(0)
  const editingNow = !!file && edited === file.url
  // When the file changed on disk during an edit, the server saves to a "(Tether edit)" copy. Ask for it while the editor
  // is open and for 30 seconds after Done, because the last save comes about 10 seconds after the editor closes.
  const [docKey, setDocKey] = useState<{ url: string; key: string } | null>(null)
  const [watchUntil, setWatchUntil] = useState(0)
  const [copy, setCopy] = useState<{ url: string; path: string } | null>(null)
  const key = file && docKey?.url === file.url ? docKey.key : ''
  const copyPath = file && copy?.url === file.url ? copy.path : ''
  useEffect(() => {
    if (!file || !key || copyPath || (!editingNow && !watchUntil)) return
    let live = true
    const check = () => request<{ copy: string | null }>(`office/copy?key=${encodeURIComponent(key)}`)
      .then(result => { if (live && result.copy) setCopy({ url: file.url, path: result.copy }) }, () => {})
    const timer = window.setInterval(() => {
      if (!editingNow && Date.now() > watchUntil) { window.clearInterval(timer); return }
      void check()
    }, 4000)
    return () => { live = false; window.clearInterval(timer) }
  }, [file, key, copyPath, editingNow, watchUntil])
  useEffect(() => {
    if (!file || editingNow) return
    const abort = new AbortController()
    const kind = suffixOf(file.path)
    const load = async () => {
      const response = await reach(file.url, { signal: abort.signal })
      if (!response.ok) throw new Error(((await response.json().catch(() => null)) as { error?: string } | null)?.error ?? 'Tether cannot open this file.')
      if (Number(response.headers.get('Content-Length')) > MAX_BYTES) throw new Error('This file is too large to show (50 MB at most). Download it instead.')
      const data = await response.arrayBuffer()
      if (data.byteLength > MAX_BYTES) throw new Error('This file is too large to show (50 MB at most). Download it instead.')
      if (kind !== 'docx' && kind !== 'pptx') {
        const { read } = await import('xlsx')
        try { return { sheet: read(data, { type: 'array', dense: true, sheetRows: MAX_ROWS + 1 }) } } catch { throw new Error(UNREADABLE) }
      }
      return { data }
    }
    load().then(result => setState({ url: file.url, ...result }),
      failure => { if (!abort.signal.aborted) setState({ url: file.url, ...(failure instanceof NetworkError ? { unreachable: true } : { error: String(failure.message) }) }) })
    return () => abort.abort()
  }, [file, editingNow, round])
  const current = file && state?.url === file.url && !editingNow ? state : null
  const name = file?.path.split('/').pop() || file?.path
  const kind = file ? suffixOf(file.path) : ''
  return <dialog {...modal} className="text-viewer office-viewer" aria-labelledby="office-viewer-title">{file && <>
    <div className="text-viewer-head">
      <div className="text-viewer-title"><h2 id="office-viewer-title">{name}</h2>
        {editingNow ? <p className="office-save-note">Saves when you press Save, or about 10 seconds after you close the editor.</p>
          : <p title={file.path}><bdi>{file.path}</bdi></p>}</div>
      {onShow && !editingNow && <button type="button" className="viewer-files" onClick={onShow}><FolderOpen size={15} aria-hidden="true" />Show in Files</button>}
      {editing && EDITABLE.includes(kind) && (editingNow
        ? <button type="button" className="viewer-files" onClick={() => { setEdited(null); setWatchUntil(Date.now() + 30000); setRound(value => value + 1); window.setTimeout(() => setRound(value => value + 1), 12000) }}>Done</button>
        : <button type="button" className="viewer-files" onClick={() => setEdited(file.url)}><Pencil size={15} aria-hidden="true" />Edit</button>)}
      <a className="icon-button" href={file.url} download={name} aria-label={`Download ${name}`} title="Download"><Download size={19} /></a>
      <button type="button" className="icon-button" aria-label="Close" onClick={close}><X /></button>
    </div>
    {copyPath && <p className="office-copy-note" role="status">The file changed on disk while you edited it. Tether saved your edits to a copy: <bdi title={copyPath}>{copyPath.split('/').pop()}</bdi></p>}
    {editingNow ? <OfficeEditor key={file.url} file={file} onKey={value => setDocKey({ url: file.url, key: value })} />
      : current?.error || current?.unreachable ? <div className="text-viewer-body"><p className="text-viewer-note" role="alert">{current.unreachable ? `Cannot reach ${host}.` : current.error}</p></div>
      : !current ? <div className="text-viewer-body"><Loading className="text-viewer-note" /></div>
      : current.sheet ? <Sheets key={current.url} book={current.sheet} name={name ?? ''} />
      : current.data && kind === 'docx' ? <WordDocument key={current.url} data={current.data} name={name ?? ''} />
      : current.data ? <Slides key={current.url} data={current.data} name={name ?? ''} /> : null}
  </>}</dialog>
}

type DocsEditor = { destroyEditor(): void }
type DocsApi = { DocEditor: new (id: string, config: unknown) => DocsEditor }
let apiScript: Promise<DocsApi> | null = null

// ONLYOFFICE's api.js comes from the editor's own port on this host, so the editor never runs with Tether's rights.
// It loads once per page.
function loadDocsApi(src: string) {
  const ready = () => (window as unknown as { DocsAPI?: DocsApi }).DocsAPI
  const loaded = ready()
  if (loaded) return Promise.resolve(loaded)
  apiScript ??= new Promise<DocsApi>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = src
    script.async = true
    script.onload = () => { const api = ready(); if (api) resolve(api); else { apiScript = null; reject(new Error('The Office editor did not start.')) } }
    script.onerror = () => { apiScript = null; script.remove(); reject(new Error('Tether cannot reach the Office editor. Check that its container runs.')) }
    document.head.append(script)
  })
  return apiScript
}

const darkPage = () => getComputedStyle(document.documentElement).colorScheme.includes('dark')
  || matchMedia('(prefers-color-scheme: dark)').matches && document.documentElement.dataset.theme !== 'light'

/** The ONLYOFFICE editor for one file. The server signs the editor settings and saves the file when the editor sends it back. */
function OfficeEditor({ file, onKey }: { file: OfficeFile; onKey: (key: string) => void }) {
  const holder = useRef<HTMLDivElement>(null)
  const mountId = `office-editor-${useId().replace(/[^a-zA-Z0-9]/g, '')}`
  const [error, setError] = useState('')
  const [ready, setReady] = useState(false)
  useEffect(() => {
    const target = holder.current
    if (!target) return
    let live = true
    let editor: DocsEditor | null = null
    const session = new URL(file.url, location.href).searchParams.get('session') ?? undefined
    // Phones get the desktop editor too: ONLYOFFICE Community opens its mobile editor for viewing only.
    const body = { path: file.path, session, mobile: false, lang: navigator.language.split('-')[0], theme: darkPage() ? 'dark' : 'light' }
    request<{ editorPort: number; apiPath: string; config: Record<string, unknown> }>('office/open', body, undefined, undefined, 20000)
      .then(async ({ editorPort, apiPath, config }) => {
        const src = new URL(apiPath, location.origin)
        src.port = String(editorPort)
        const docs = await loadDocsApi(src.href)
        if (!live) return
        const documentKey = (config.document as { key?: unknown } | undefined)?.key
        if (typeof documentKey === 'string') onKey(documentKey)
        const mount = document.createElement('div')
        mount.id = mountId
        target.replaceChildren(mount)
        editor = new docs.DocEditor(mountId, {
          ...config,
          events: {
            onAppReady: () => { if (live) setReady(true) },
            onError: (event: { data?: { errorDescription?: string } }) => { if (live) setError(event.data?.errorDescription || 'The Office editor stopped with an error.') },
          },
        })
      })
      .catch(failure => { if (live) setError(errorText(failure)) })
    return () => { live = false; editor?.destroyEditor(); target.replaceChildren() }
  }, [file.path, file.url, mountId])
  return <div className="text-viewer-body office-editor">
    {error ? <p className="text-viewer-note" role="alert">{error}</p> : !ready && <p className="text-viewer-note office-editor-wait">Starting the editor…</p>}
    <div ref={holder} className="office-editor-frame" hidden={!!error} />
  </div>
}

function Sheets({ book, name }: { book: WorkBook; name: string }) {
  const [chosen, setChosen] = useState(0)
  const [table, setTable] = useState<{ index: number; rows: string[][]; firstRow: number; columns: string[]; more: boolean } | null>(null)
  const index = Math.min(chosen, book.SheetNames.length - 1)
  useEffect(() => {
    let live = true
    import('xlsx').then(({ utils }) => {
      const sheet = book.Sheets[book.SheetNames[index]]
      const ref = sheet?.['!ref']
      if (!live) return
      if (!sheet || !ref) { setTable({ index, rows: [], firstRow: 1, columns: [], more: false }); return }
      const range = utils.decode_range(ref)
      const last = Math.min(range.e.c, range.s.c + MAX_COLUMNS - 1)
      const rows = (utils.sheet_to_json<unknown[]>(sheet, { header: 1, raw: false, defval: '', blankrows: true, range: { s: range.s, e: { r: range.e.r, c: last } } }))
        .slice(0, MAX_ROWS).map(row => row.map(cell => cell == null ? '' : String(cell)))
      const columns = Array.from({ length: last - range.s.c + 1 }, (_, at) => utils.encode_col(range.s.c + at))
      setTable({ index, rows, firstRow: range.s.r + 1, columns, more: range.e.r - range.s.r + 1 > MAX_ROWS || range.e.c > last })
    }, () => live && setTable({ index, rows: [], firstRow: 1, columns: [], more: false }))
    return () => { live = false }
  }, [book, index])
  const shown = table?.index === index ? table : null
  return <>
    {book.SheetNames.length > 1 && <div ref={wheelSideways} className="office-tabs" role="tablist" aria-label="Sheets">
      {book.SheetNames.map((sheet, at) => <button key={at} type="button" role="tab" aria-selected={at === index} onClick={() => setChosen(at)}>{sheet}</button>)}
    </div>}
    <div className="text-viewer-body office-sheet" tabIndex={0} role={book.SheetNames.length > 1 ? 'tabpanel' : undefined} aria-label={`${book.SheetNames[index]} in ${name}`}>
      {!shown ? <Loading className="text-viewer-note" />
        : !shown.rows.length ? <p className="text-viewer-note">This sheet is empty.</p>
        : <>
          <table>
            <thead><tr><th aria-hidden="true" />{shown.columns.map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
            <tbody>{shown.rows.map((row, at) => <tr key={at}><th scope="row">{shown.firstRow + at}</th>
              {shown.columns.map((_, column) => <td key={column}>{row[column] ?? ''}</td>)}</tr>)}</tbody>
          </table>
          {shown.more && <p className="text-viewer-note">Tether shows the first {MAX_ROWS} rows and {MAX_COLUMNS} columns. Download the file to see the rest.</p>}
        </>}
    </div>
  </>
}

// Links in a document open in a new tab when they go to a web page or an email address. Other links show as text.
function safeLinks(root: HTMLElement) {
  for (const link of root.querySelectorAll('a')) {
    const href = link.getAttribute('href') ?? ''
    if (/^(https?|mailto):/i.test(href)) { link.target = '_blank'; link.rel = 'noopener noreferrer' }
    else if (!href.startsWith('#')) link.removeAttribute('href')
  }
}

function WordDocument({ data, name }: { data: ArrayBuffer; name: string }) {
  const body = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  // A phone is narrower than the page: let the text wrap to the screen instead of scrolling sideways.
  const narrow = useMemo(() => typeof matchMedia === 'function' && matchMedia('(max-width: 820px)').matches, [])
  useEffect(() => {
    const target = body.current
    if (!target) return
    let live = true
    import('docx-preview')
      .then(({ renderAsync }) => renderAsync(data, target, target, { className: 'docx', inWrapper: true, ignoreWidth: narrow, ignoreHeight: narrow, breakPages: !narrow, renderComments: false, renderChanges: false, experimental: false }))
      .then(() => { if (live) safeLinks(target) }, () => { if (live) setError(UNREADABLE) })
    return () => { live = false; target.replaceChildren() }
  }, [data, narrow])
  return <div className={narrow ? 'text-viewer-body office-doc narrow' : 'text-viewer-body office-doc'} tabIndex={0} aria-label={`Contents of ${name}`}>
    {error && <p className="text-viewer-note" role="alert">{error}</p>}
    <div ref={body} />
  </div>
}

function Slides({ data, name }: { data: ArrayBuffer; name: string }) {
  const scroller = useRef<HTMLDivElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<{ count: number; error?: string } | null>(null)
  useEffect(() => {
    const target = stage.current
    if (!target || !scroller.current) return
    const abort = new AbortController()
    let viewer: { destroy(): void } | null = null
    import('@aiden0z/pptx-renderer')
      .then(({ PptxViewer, RECOMMENDED_ZIP_LIMITS }) => PptxViewer.open(data, target, {
        signal: abort.signal, zipLimits: RECOMMENDED_ZIP_LIMITS, externalMedia: 'block', pdfjs: false, fitMode: 'contain',
        scrollContainer: scroller.current ?? undefined, lazyMedia: true, lazySlides: true,
        renderMode: 'list', listOptions: { windowed: true, showSlideLabels: true },
      }))
      .then(opened => {
        if (abort.signal.aborted) { opened.destroy(); return }
        viewer = opened
        setState({ count: opened.slideCount })
      }, () => { if (!abort.signal.aborted) setState({ count: 0, error: UNREADABLE }) })
    return () => { abort.abort(); viewer?.destroy(); target.replaceChildren() }
  }, [data])
  return <div ref={scroller} className="text-viewer-body office-slides" tabIndex={0} aria-label={state && !state.error ? `${state.count} slides in ${name}` : `Slides in ${name}`}>
    {!state ? <Loading className="text-viewer-note" /> : state.error ? <p className="text-viewer-note" role="alert">{state.error}</p>
      : !state.count ? <p className="text-viewer-note">This deck has no slides.</p> : null}
    <div ref={stage} className="office-slides-stage" />
  </div>
}
