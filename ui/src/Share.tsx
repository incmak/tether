import { RetryButton } from './Retry'
import { Bone, Loading, Skeleton } from './Skeleton'
// Share: your own file store in Tether. Library grid by day, a preview pane, an upload queue and a "From agents" strip.
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Bot, Check, ChevronsRight, Download, ExternalLink, File, FileText, Film, Image as ImageIcon, MessageSquarePlus, Music, Pencil, Plus, RotateCcw, Search, Trash2, TriangleAlert, Upload, X } from 'lucide-react'
import { errorText } from './api'
import { useNow } from './attention'
import { ImageViewer, type Media } from './ImageViewer'
import { backfillThumb, cancelUpload, CHUNK, inspect, listShare, listUploads, putThumb, rawUrl, renameShare, restoreShare, sameFile, shareToChat, textPreview, thumbUrl, trashShare, uploadResumable, uploadShare, type ShareFile, type ShareFilter, type ShareKind, type ShareUpload, type ShareUsage, type Wait } from './shareApi'
import './Share.css'
import { wheelSideways } from './wheelSideways'
import { fmtDate, useTetherZone, zonedTime, zoneParts } from './zone'

const PdfView = lazy(() => import('./SharePdfView').then(m => ({ default: m.PdfView })))

const DAY = 864e5
const time = (file: ShareFile) => Date.parse(file.created)
const size = (n: number) => n < 1e3 ? `${n} B` : n < 1e6 ? `${(n / 1e3).toFixed(0)} KB` : n < 1e9 ? `${(n / 1e6).toFixed(1)} MB` : `${(n / 1e9).toFixed(n < 1e10 ? 2 : 1)} GB`
const clock = (s?: number) => s == null ? '' : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
const ago = (t: number) => {
  const m = Math.round((Date.now() - t) / 60e3)
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : fmtDate(t, { weekday: 'short', day: 'numeric', month: 'short' })
}
const dayLabel = (t: number, today: number, yesterday: number, week: number) => t >= today ? 'Today' : t >= yesterday ? 'Yesterday' : t >= week ? 'This week' : 'Earlier'
const ext = (name: string) => name.includes('.') ? (name.split('.').pop() ?? '').toUpperCase().slice(0, 4) : ''
const agentName = (file: ShareFile) => file.source.startsWith('agent:') ? file.source.slice(6) : ''
const KIND_ICON: Record<ShareKind, typeof File> = { image: ImageIcon, video: Film, audio: Music, pdf: FileText, text: FileText, other: File }
const KIND_LABEL: Record<ShareKind, string> = { image: 'Photo', video: 'Video', audio: 'Audio', pdf: 'PDF', text: 'Text', other: 'File' }
type View = ShareFilter | 'trash'
const VIEWS: { id: View; label: string }[] = [
  { id: 'all', label: 'All' }, { id: 'image', label: 'Photos' }, { id: 'video', label: 'Videos' },
  { id: 'doc', label: 'Docs' }, { id: 'audio', label: 'Audio' }, { id: 'other', label: 'Other' }, { id: 'trash', label: 'Trash' },
]
const PARALLEL = 2

type Upload = { key: string; file: globalThis.File; sent: number; state: 'waiting' | 'sending' | 'done' | 'failed'; error?: string; wait?: Wait; resume?: ShareUpload }
type Toast = { text: string; undo?: () => void }

/** The first lines of a text file, read with a Range request so a tile costs about 2 KB. */
const pages = new Map<string, Promise<string>>()
function firstPage(file: ShareFile) {
  const key = `${file.id}:${file.modified}`
  if (!pages.has(key)) pages.set(key, fetch(rawUrl(file.id), { credentials: 'same-origin', headers: { Range: 'bytes=0-2047' } })
    .then(response => response.ok ? response.arrayBuffer() : new ArrayBuffer(0))
    .then(bytes => new TextDecoder('utf-8', { fatal: false }).decode(bytes).replace(/�+$/, ''))
    .catch(() => ''))
  return pages.get(key)!
}
function usePage(file: ShareFile, on: boolean) {
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    if (!on) return
    let live = true
    firstPage(file).then(value => { if (live) setText(value) })
    return () => { live = false }
  }, [file, on])
  return on ? text : null
}

/** Makes a thumbnail in the browser when the tile comes near view and the file has none (files an agent added). */
function useBackfill(file: ShareFile, update?: (file: ShareFile) => void) {
  const ref = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    const target = ref.current
    if (!update || file.has_thumb || !target) return
    let live = true
    const observer = new IntersectionObserver(entries => {
      if (!entries[0].isIntersecting) return
      observer.disconnect()
      void backfillThumb(file).then(changed => { if (live && changed) update(changed) })
    }, { rootMargin: '200px' })
    observer.observe(target)
    return () => { live = false; observer.disconnect() }
  }, [file, update])
  return ref
}

function Thumb({ file, update }: { file: ShareFile; update?: (file: ShareFile) => void }) {
  const Icon = KIND_ICON[file.kind]
  const page = usePage(file, file.kind === 'text')
  const [broken, setBroken] = useState(false)
  const ref = useBackfill(file, update)
  const src = !broken && (file.has_thumb ? thumbUrl(file) : file.kind === 'image' ? rawUrl(file.id) : '')
  if (src) return <span ref={ref} className="sh-thumb"><img src={src} alt="" loading="lazy" decoding="async" onError={() => setBroken(true)} />
    {file.kind === 'video' && file.duration != null && <span className="sh-length">{clock(file.duration)}</span>}</span>
  if (page) return <span ref={ref} className="sh-thumb sh-page"><span className="sh-page-text">{page}</span>{ext(file.name) && <b className={`sh-ext k-${file.kind}`}>{ext(file.name)}</b>}</span>
  return <span ref={ref} className={`sh-thumb sh-doc k-${file.kind}`}><Icon size={22} strokeWidth={1.5} aria-hidden="true" />{ext(file.name) && <b>{ext(file.name)}</b>}</span>
}

function Ring({ value, size: px = 22 }: { value: number; size?: number }) {
  const r = px / 2 - 2.5, c = 2 * Math.PI * r
  return <svg className="sh-ring" width={px} height={px} viewBox={`0 0 ${px} ${px}`} aria-hidden="true">
    <circle cx={px / 2} cy={px / 2} r={r} /><circle cx={px / 2} cy={px / 2} r={r} strokeDasharray={c} strokeDashoffset={c * (1 - Math.min(1, value))} />
  </svg>
}

const WAIT_TEXT: Record<NonNullable<Wait>, string> = { offline: 'Waiting for a connection', retrying: 'Connection lost. Trying again…' }

function Uploads({ list, close }: { list: Upload[]; close: () => void }) {
  const left = list.filter(u => u.state === 'waiting' || u.state === 'sending')
  const wait = left.find(u => u.wait)?.wait
  const failed = list.filter(u => u.state === 'failed')
  const total = list.reduce((sum, u) => sum + u.file.size, 0), sent = list.reduce((sum, u) => sum + (u.state === 'done' ? u.file.size : u.sent), 0)
  const n = (count: number) => `${count} ${count === 1 ? 'file' : 'files'}`
  const shown = left.length ? list.filter(u => u.state !== 'done') : failed
  return <aside className="sh-uploads" aria-label="Uploads">
    <header aria-live="polite">
      {left.length ? <Ring value={total ? sent / total : 0} /> : failed.length ? <TriangleAlert size={18} className="sh-warn" aria-hidden="true" /> : <Check size={18} className="sh-ok" aria-hidden="true" />}
      <b>{left.length ? `Adding ${n(list.length)}` : failed.length ? `${n(failed.length)} not added` : `Added ${n(list.length)}`}</b>
      {left.length > 0 ? <span className={wait ? 'sh-up-wait' : undefined}>{wait ? WAIT_TEXT[wait] : `${size(sent)} of ${size(total)}`}</span>
        : failed.length > 0 && <button type="button" className="icon-button sm" onClick={close} aria-label="Close uploads"><X size={15} /></button>}
    </header>
    {shown.length > 0 && <ul>{shown.slice(0, 3).map(u => <li key={u.key}>
      <span className="sh-up-name">{u.file.name}</span>
      <span className={u.state === 'failed' ? 'sh-up-meta error' : 'sh-up-meta'}>{u.state === 'failed' ? u.error : u.state === 'waiting' ? 'Waiting' : u.wait ? `Paused at ${size(u.sent)} of ${size(u.file.size)}` : `${size(u.sent)} of ${size(u.file.size)}`}</span>
      {u.state === 'failed' ? <TriangleAlert size={15} className="sh-warn" aria-hidden="true" /> : <Ring value={u.file.size ? u.sent / u.file.size : 0} size={18} />}
    </li>)}{shown.length > 3 && <li className="sh-up-more">and {shown.length - 3} more</li>}</ul>}
  </aside>
}

function Preview({ file, view }: { file: ShareFile; view: (file: ShareFile) => void }) {
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    if (file.kind !== 'text') return
    const stop = new AbortController()
    textPreview(file.id, stop.signal).then(setText, () => setText(''))
    return () => stop.abort()
  }, [file.id, file.kind])
  if (file.kind === 'image') return <button type="button" className="sh-view-image" onClick={() => view(file)} aria-label={`Open ${file.name} full screen`}>
    <img src={rawUrl(file.id)} alt={file.name} /></button>
  if (file.kind === 'video') return <video className="sh-view-video" src={rawUrl(file.id)} poster={file.has_thumb ? thumbUrl(file) : undefined} controls playsInline preload="metadata" />
  if (file.kind === 'audio') return <div className="sh-view-none"><Music size={40} strokeWidth={1.4} aria-hidden="true" /><audio controls preload="metadata" src={rawUrl(file.id)} /></div>
  if (file.kind === 'text') return text == null ? <Loading className="sh-view-none" as="div" />
    : <pre className="sh-view-text" tabIndex={0}>{text || 'This file is empty.'}</pre>
  if (file.kind === 'pdf') return <Suspense fallback={<Loading className="sh-view-none" as="div" />}><PdfView url={rawUrl(file.id)} name={file.name} /></Suspense>
  return <div className="sh-view-none"><File size={40} strokeWidth={1.4} aria-hidden="true" />{ext(file.name) && <b>{ext(file.name)}</b>}<span>No preview. Download it to open.</span></div>
}

function Pane({ file, wide, trash, close, send, remove, restore, rename, view }: {
  file: ShareFile; wide: boolean; trash: boolean; close: () => void
  send?: (files: ShareFile[]) => void; remove: (files: ShareFile[]) => void; restore: (files: ShareFile[]) => void
  rename: (file: ShareFile, name: string) => Promise<boolean>; view: (file: ShareFile) => void
}) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(file.name)
  const agent = agentName(file)
  const save = async () => { if (name.trim() && name !== file.name && !await rename(file, name.trim())) return; setEditing(false) }
  return <aside className={wide ? 'sh-pane' : 'sh-pane sh-sheet'} aria-label={`Preview of ${file.name}`} onKeyDown={e => { if (e.key === 'Escape' && !editing) close() }}>
    <header>
      <button type="button" className="icon-button" onClick={close} aria-label="Close preview" autoFocus>{wide ? <ChevronsRight size={18} /> : <ArrowLeft size={20} />}</button>
      {editing ? <form className="text-field sh-rename" onSubmit={e => { e.preventDefault(); void save() }}>
        <input value={name} onChange={e => setName(e.target.value)} aria-label="File name" autoFocus
          onFocus={e => e.target.setSelectionRange(0, name.lastIndexOf('.') > 0 ? name.lastIndexOf('.') : name.length)}
          onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setName(file.name); setEditing(false) } }} onBlur={() => void save()} />
      </form> : <div className="sh-pane-title"><h2 title={file.name}>{file.name}</h2>
        <p>{KIND_LABEL[file.kind]} · {size(file.size)}{file.width && file.height ? ` · ${file.width}×${file.height}` : ''}{file.duration ? ` · ${clock(file.duration)}` : ''}</p></div>}
    </header>
    <div className="sh-pane-actions">
      {trash ? <button type="button" className="primary" onClick={() => { restore([file]); close() }}><RotateCcw size={16} aria-hidden="true" />Restore</button> : <>
        {send && <button type="button" className="primary" onClick={() => send([file])}><MessageSquarePlus size={16} aria-hidden="true" />Send to chat</button>}
        <a className="secondary" href={rawUrl(file.id, true)} download={file.name}><Download size={16} aria-hidden="true" />Download</a>
        {file.kind === 'pdf' && <a className="icon-button" href={rawUrl(file.id)} target="_blank" rel="noopener" aria-label="Open in a new tab" title="Open in a new tab"><ExternalLink size={17} /></a>}
        <button type="button" className="icon-button" onClick={() => { setName(file.name); setEditing(true) }} aria-label="Rename" title="Rename"><Pencil size={17} /></button>
        <button type="button" className="icon-button danger-text" onClick={() => { remove([file]); close() }} aria-label="Delete" title="Delete"><Trash2 size={17} /></button>
      </>}
    </div>
    <div className="sh-pane-body">
      <div className={`sh-view k-${file.kind}`}><Preview key={file.id} file={file} view={view} /></div>
      <p className="sh-pane-meta">{agent ? <><Bot size={13} aria-hidden="true" />Sent by {agent}</> : <><Upload size={13} aria-hidden="true" />Added by you</>} · {ago(time(file))}</p>
    </div>
  </aside>
}

/** Agent files from the last 3 days, grouped into drops: one agent, files added within 10 minutes of each other. */
function drops(files: ShareFile[]) {
  const out: ShareFile[][] = []
  for (const file of files) {
    if (Date.now() - time(file) > 3 * DAY) continue
    const last = out[out.length - 1]
    if (last && agentName(last[0]) === agentName(file) && time(last[last.length - 1]) - time(file) < 10 * 60e3) last.push(file)
    else out.push([file])
  }
  return out.slice(0, 2)
}

export function Share({ wide, onAddToMessage }: { wide: boolean; onAddToMessage?: (paths: string[]) => void }) {
  const zone = useTetherZone()
  const [view, setView] = useState<View>('all')
  const [query, setQuery] = useState('')
  const [q, setQ] = useState('')
  const [files, setFiles] = useState<ShareFile[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [usage, setUsage] = useState<ShareUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [agentFiles, setAgentFiles] = useState<ShareFile[]>([])
  const [hideAgents, setHideAgents] = useState(() => { try { return localStorage.getItem('share.hideAgents') === '1' } catch { return false } })
  const [picked, setPicked] = useState<string[]>([])
  const [selectMode, setSelectMode] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  const [opened, setOpened] = useState<ShareFile | null>(null)
  const [uploads, setUploads] = useState<Upload[]>([])
  const [viewing, setViewing] = useState<Media | null>(null)
  const [toast, setToast] = useState<Toast | null>(null)
  const [dragging, setDragging] = useState(false)
  const [unfinished, setUnfinished] = useState<ShareUpload[]>([])
  const input = useRef<HTMLInputElement>(null)
  const more = useRef<HTMLDivElement>(null)
  const loadingMore = useRef(false)
  const trash = view === 'trash'
  const selecting = selectMode || picked.length > 0

  useEffect(() => { const t = setTimeout(() => setQ(query.trim()), 250); return () => clearTimeout(t) }, [query])
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(null), toast.undo ? 6000 : 3000); return () => clearTimeout(t) }, [toast])

  const options = useMemo(() => ({ kind: trash ? 'all' as const : view as ShareFilter, q, trash }), [view, q, trash])
  const load = useCallback((signal?: AbortSignal) =>
    listShare(options, signal).then(data => {
      setFiles(data.files); setNext(data.next); setUsage(data.usage); setError('')
    }).catch(e => { if (!signal?.aborted) setError(errorText(e)) })
      .finally(() => { if (!signal?.aborted) setLoading(false) }),
  [options])
  const loadAgents = useCallback((signal?: AbortSignal) => listShare({ source: 'agent', limit: 24 }, signal).then(data => setAgentFiles(data.files), () => {}), [])
  const [previousOptions, setPreviousOptions] = useState(options)
  if (options !== previousOptions) { setPreviousOptions(options); setLoading(true); setPicked([]) }
  useEffect(() => {
    const stop = new AbortController()
    void load(stop.signal)
    return () => stop.abort()
  }, [load])
  useEffect(() => { const stop = new AbortController(); void loadAgents(stop.signal); return () => stop.abort() }, [loadAgents])
  const refresh = () => { void loadAgents(); return load() }
  useEffect(() => { const stop = new AbortController(); listUploads(stop.signal).then(setUnfinished, () => {}); return () => stop.abort() }, [])
  const update = useCallback((changed: ShareFile) => {
    setFiles(list => list.map(f => f.id === changed.id ? changed : f))
    setAgentFiles(list => list.map(f => f.id === changed.id ? changed : f))
  }, [])

  // Load the next page when the end of the grid scrolls into view.
  useEffect(() => {
    const target = more.current
    if (!target || !next) return
    const observer = new IntersectionObserver(async entries => {
      if (!entries[0].isIntersecting || loadingMore.current) return
      loadingMore.current = true
      try {
        const data = await listShare({ ...options, cursor: next })
        setFiles(list => [...list, ...data.files.filter(file => !list.some(f => f.id === file.id))]); setNext(data.next)
      } catch (e) { setError(errorText(e)) }
      finally { loadingMore.current = false }
    }, { rootMargin: '400px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [next, options])

  // Uploads: at most PARALLEL at once. Each one sends its thumbnail after the file.
  const started = useRef(new Set<string>())
  useEffect(() => {
    const active = uploads.filter(u => u.state === 'sending').length
    const waiting = uploads.filter(u => u.state === 'waiting' && !started.current.has(u.key)).slice(0, PARALLEL - active)
    for (const u of waiting) {
      started.current.add(u.key)
      const set = (patch: Partial<Upload>) => setUploads(list => list.map(x => x.key === u.key ? { ...x, ...patch } : x))
      set({ state: 'sending' })
      void (async () => {
        try {
          const { meta, thumb } = await inspect(u.file)
          const chunked = u.resume || u.file.size > CHUNK ? await uploadResumable(u.file, meta, sent => set({ sent }), wait => set({ wait }), undefined, u.resume) : null
          let file = chunked ?? await uploadShare(u.file, meta, sent => set({ sent }))
          if (thumb) file = await putThumb(file.id, thumb).catch(() => file)
          setFiles(list => [file, ...list.filter(f => f.id !== file.id)])
          setUsage(value => value && { ...value, used: value.used + file.size, count: value.count + 1 })
          set({ state: 'done', sent: u.file.size, wait: null })
        } catch (e) { set({ state: 'failed', error: errorText(e), wait: null }) }
      })()
    }
  }, [uploads])
  const busy = uploads.some(u => u.state === 'waiting' || u.state === 'sending')
  const failed = uploads.some(u => u.state === 'failed')
  useEffect(() => {
    if (busy || failed || !uploads.length) return
    const t = setTimeout(() => { setUploads([]); started.current.clear() }, 2600)
    return () => clearTimeout(t)
  }, [busy, failed, uploads.length])
  const add = async (list: FileList | globalThis.File[] | null) => {
    if (!list?.length) return
    if (trash) setView('all')
    const stamp = Date.now(), files = [...list], open = [...unfinished], taken = new Set<string>()
    const next: Upload[] = []
    for (const [i, file] of files.entries()) {
      let resume: ShareUpload | undefined
      for (const s of open) if (!taken.has(s.id) && await sameFile(s, file).catch(() => false)) { resume = s; taken.add(s.id); break }
      next.push({ key: `${stamp}-${i}`, file, sent: resume?.offset ?? 0, state: 'waiting' as const, resume })
    }
    setUnfinished(current => current.filter(s => !taken.has(s.id)))
    setUploads(current => [...current.filter(u => u.state !== 'done'), ...next])
  }
  const discard = () => { const list = unfinished; setUnfinished([]); for (const s of list) void cancelUpload(s.id).catch(() => {}) }

  const byId = (ids: string[]) => ids.map(id => files.find(f => f.id === id)).filter((f): f is ShareFile => !!f)
  const forget = (ids: string[]) => {
    setFiles(list => list.filter(f => !ids.includes(f.id)))
    setAgentFiles(list => list.filter(f => !ids.includes(f.id)))
    setPicked([]); setSelectMode(false)
    if (openId && ids.includes(openId)) setOpenId(null)
  }
  const remove = async (list: ShareFile[]) => {
    const ids = list.map(f => f.id)
    try {
      await trashShare(ids); forget(ids)
      setUsage(value => value && { ...value, used: value.used - list.reduce((s, f) => s + f.size, 0), count: value.count - list.length })
      setToast({ text: list.length === 1 ? `Moved ${list[0].name} to Trash.` : `Moved ${list.length} files to Trash.`, undo: () => { void restoreShare(ids).then(refresh, e => setToast({ text: errorText(e) })) } })
    } catch (e) { setToast({ text: errorText(e) }) }
  }
  const restore = async (list: ShareFile[]) => {
    const ids = list.map(f => f.id)
    try { await restoreShare(ids); forget(ids); void loadAgents(); setToast({ text: list.length === 1 ? `Restored ${list[0].name}.` : `Restored ${list.length} files.` }) }
    catch (e) { setToast({ text: errorText(e) }) }
  }
  const rename = async (file: ShareFile, name: string) => {
    try {
      const { files: [changed] } = await renameShare(file.id, name)
      setFiles(list => list.map(f => f.id === changed.id ? changed : f))
      setAgentFiles(list => list.map(f => f.id === changed.id ? changed : f))
      return true
    } catch (e) { setToast({ text: errorText(e) }); return false }
  }
  const send = onAddToMessage && (async (list: ShareFile[]) => {
    try {
      const paths = await Promise.all(list.map(f => shareToChat(f.id).then(r => r.path)))
      onAddToMessage(paths)
    } catch (e) { setToast({ text: errorText(e) }) }
  })
  const download = (list: ShareFile[]) => {
    for (const file of list) { const a = document.createElement('a'); a.href = rawUrl(file.id, true); a.download = file.name; a.click() }
  }
  const mediaFor = (file: ShareFile): Media => ({ name: file.name, url: rawUrl(file.id), downloadUrl: rawUrl(file.id, true), thumbnailUrl: file.has_thumb ? thumbUrl(file) : undefined, kind: file.kind === 'video' || file.kind === 'audio' ? file.kind : 'image' })
  const openFull = (file: ShareFile) => setViewing(mediaFor(file))

  const now = useNow()
  const { year, month, date } = zoneParts(now * 1000, zone)
  const [today, yesterday, week] = useMemo(() =>
    [0, 1, 6].map(daysAgo => zonedTime(year, month, date - daysAgo, 0, 0, zone)),
  [year, month, date, zone])
  const groups = useMemo(() => {
    const map = new Map<string, ShareFile[]>()
    for (const file of files) { const label = dayLabel(time(file), today, yesterday, week); map.set(label, [...(map.get(label) ?? []), file]) }
    return [...map]
  }, [files, today, yesterday, week])
  const agentDrops = useMemo(() => drops(agentFiles), [agentFiles])
  // Keep the pane open when the file is in the agents strip but not in the loaded grid page.
  const current = files.find(f => f.id === openId) ?? agentFiles.find(f => f.id === openId) ?? (opened?.id === openId ? opened : null)
  const open = (file: ShareFile) => {
    if (!wide && !trash && file.kind === 'image') { openFull(file); return }
    setOpenId(file.id); setOpened(file)
  }
  const toggle = (id: string) => setPicked(list => list.includes(id) ? list.filter(x => x !== id) : [...list, id])
  const progress = uploads.filter(u => u.state === 'sending' || u.state === 'waiting')

  const drop = {
    onDragOver: (e: React.DragEvent) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } },
    onDragLeave: (e: React.DragEvent) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false) },
    onDrop: (e: React.DragEvent) => { if (!e.dataTransfer.files.length) return; e.preventDefault(); setDragging(false); void add(e.dataTransfer.files) },
    onPaste: (e: React.ClipboardEvent) => { if (e.clipboardData.files.length && !(e.target instanceof HTMLInputElement)) { e.preventDefault(); void add(e.clipboardData.files) } },
  }
  const quota = usage ? `${size(usage.used)} of ${size(usage.quota)}` : ''
  const empty = trash ? 'Trash is empty.' : q ? `No files match “${q}”.` : view === 'all' ? 'Nothing here yet. Drop files anywhere on this screen, or press Add.' : 'No files of this kind.'

  return <section className={['share', wide ? 'wide' : 'narrow', selecting && 'selecting', current && wide && 'with-pane'].filter(Boolean).join(' ')} aria-label="Share" {...drop}>
    <div className="sh-main">
      <header className="sh-head">
        <h1>Share</h1>
        {quota && <span className="sh-usage" title="Storage used">{quota}</span>}
        <label className="text-field sh-search"><Search aria-hidden="true" /><input type="search" placeholder="Search Share" aria-label="Search Share" value={query} onChange={e => setQuery(e.target.value)} /></label>
        <button type="button" className="ghost" aria-pressed={selectMode} onClick={() => { setSelectMode(!selectMode); setPicked([]) }}>{selectMode ? 'Done' : 'Select'}</button>
        <button type="button" className="primary" onClick={() => input.current?.click()}><Plus aria-hidden="true" />Add</button>
        <input ref={input} type="file" multiple hidden onChange={e => { void add(e.target.files); e.target.value = '' }} />
      </header>
      <nav ref={wheelSideways} className="tab-bar sh-views" aria-label="Show">{VIEWS.map(v => <button key={v.id} type="button" aria-pressed={view === v.id} onClick={() => { setView(v.id); setOpenId(null) }}>{v.label}</button>)}</nav>
      <div className="sh-scroll">
        {!hideAgents && view === 'all' && !q && agentDrops.length > 0 && <section className="sh-agents" aria-label="From agents">
          <header><h2 className="sh-day">From agents</h2>
            <button type="button" className="icon-button sm" onClick={() => { setHideAgents(true); try { localStorage.setItem('share.hideAgents', '1') } catch { /* private mode */ } }} aria-label="Hide From agents" title="Hide"><X size={15} /></button></header>
          {agentDrops.map(list => <button key={list[0].id} type="button" className="sh-agent-row" onClick={() => open(list[0])}>
            <span className="sh-agent-icon"><Bot size={16} aria-hidden="true" /></span>
            <span className="sh-agent-text"><span><b>{agentName(list[0])}</b> sent {list[0].name}{list.length > 1 ? ` and ${list.length - 1} more` : ''}</span>
              <small>{ago(time(list[0]))}</small></span>
            <span className="sh-agent-thumbs">{list.slice(0, 3).map(f => <Thumb key={f.id} file={f} update={update} />)}</span>
          </button>)}
        </section>}
        {!trash && unfinished.length > 0 && <div className="sh-resume" role="status">
          <Upload size={16} aria-hidden="true" />
          <span><b>{unfinished.length === 1 ? `${unfinished[0].name} did not finish` : `${unfinished.length} uploads did not finish`}</b>
            <small>{unfinished.length === 1 ? `${size(unfinished[0].offset)} of ${size(unfinished[0].size)} sent. Add the same file to go on.` : 'Add the same files to go on from where they stopped.'}</small></span>
          <button type="button" className="secondary sm" onClick={() => input.current?.click()}>Resume</button>
          <button type="button" className="ghost sm" onClick={discard}>Discard</button>
        </div>}
        {trash && files.length > 0 && <p className="sh-note">Files in Trash are deleted for good after 30 days.</p>}
        {groups.map(([label, list]) => <section key={label}>
          <h2 className="sh-day">{label}</h2>
          <ul className="sh-grid">{list.map(file => <li key={file.id} className={[picked.includes(file.id) && 'picked', file.id === openId && 'open'].filter(Boolean).join(' ') || undefined}>
            <button type="button" className="sh-tile" onClick={() => selecting ? toggle(file.id) : open(file)} aria-label={`${file.name}, ${KIND_LABEL[file.kind]}, ${size(file.size)}`}>
              <Thumb file={file} update={update} />{!(file.has_thumb || file.kind === 'image') && <span className="sh-tile-name">{file.name}</span>}
            </button>
            <button type="button" className="sh-check" aria-label={`Select ${file.name}`} aria-pressed={picked.includes(file.id)} onClick={() => toggle(file.id)}><span><Check size={14} strokeWidth={3} /></span></button>
          </li>)}</ul>
        </section>)}
        {loading && !files.length && <Skeleton label="Loading files…" className="sh-skeleton"><span className="sh-day"><Bone w="5em" /></span>
          <ul className="sh-grid" aria-hidden="true">{Array.from({ length: 6 }, (_, index) => <li key={index}><Bone className="sh-tile" /></li>)}</ul></Skeleton>}
        {error && <p className="sh-empty error" role="alert">{error} <RetryButton onRetry={refresh} /></p>}
        {!loading && !error && !files.length && <p className="sh-empty">{empty}</p>}
        {next && <div ref={more} className="sh-more" aria-hidden="true" />}
      </div>
      {uploads.length > 0 && !picked.length && <Uploads list={uploads} close={() => { setUploads([]); started.current.clear() }} />}
      {progress.length > 0 && <span className="sh-sr" role="status">{`Adding ${progress.length} files`}</span>}
      {picked.length > 0 && <div className="sh-actionbar" role="toolbar" aria-label="Selected files">
        <button type="button" onClick={() => { setPicked([]); setSelectMode(false) }} aria-label="Clear selection"><X size={18} /></button>
        <span>{picked.length} selected</span>
        {trash ? <button type="button" onClick={() => void restore(byId(picked))}><RotateCcw size={17} aria-hidden="true" /><span>Restore</span></button> : <>
          {send && <button type="button" onClick={() => { void send(byId(picked)); setPicked([]); setSelectMode(false) }}><MessageSquarePlus size={17} aria-hidden="true" /><span>Send to chat</span></button>}
          <button type="button" onClick={() => download(byId(picked))}><Download size={17} aria-hidden="true" /><span>Download</span></button>
          <button type="button" className="sh-danger" onClick={() => void remove(byId(picked))}><Trash2 size={17} aria-hidden="true" /><span>Delete</span></button>
        </>}
      </div>}
      {toast && <p className="sh-toast" role="status">{toast.text}{toast.undo && <button type="button" className="link-button" onClick={() => { toast.undo?.(); setToast(null) }}>Undo</button>}</p>}
    </div>
    {current && <Pane key={current.id} file={current} wide={wide} trash={trash} close={() => setOpenId(null)} send={send} remove={list => void remove(list)} restore={list => void restore(list)} rename={rename} view={openFull} />}
    {dragging && <div className="sh-dropzone" aria-hidden="true"><Upload size={28} />Drop to add to Share</div>}
    <ImageViewer image={viewing} close={() => setViewing(null)} items={(files.some(file => rawUrl(file.id) === viewing?.url) ? files : agentFiles).filter(file => file.kind === 'image').map(mediaFor)} onSelect={setViewing}
      onDetails={viewing ? () => {
        const file = [...files, ...agentFiles].find(item => rawUrl(item.id) === viewing.url)
        if (file) { setOpenId(file.id); setOpened(file); setViewing(null) }
      } : undefined} />
  </section>
}
