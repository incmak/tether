import { RetryButton } from './Retry'
import { Loading, SkeletonRows } from './Skeleton'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, AtSign, BookMarked, ChevronLeft, ChevronRight, Copy, CopyPlus, Download, File, Folder, FolderInput, FolderOpen, FolderPlus, Image as ImageIcon, LayoutGrid, List, Lock, MessageCircle, MoreHorizontal, Package, Pencil, Plus, Search, SquareTerminal, Trash2, Upload, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, Pane } from './api'
import { needsYou, paneTitle } from './Sidebar'
import { anchorOf } from './hooks'
import type { Anchor } from './hooks'
import { FolderPicker, ItemMenu, TrashConfirm, UploadPanel, ZipConfirm } from './ExplorerWrites'
import type { MenuItem } from './ExplorerWrites'
import { setUploadDone, startUploads, summary, writes } from './fileWrites'
import { ImageViewer } from './ImageViewer'
import type { Media } from './ImageViewer'
import { TextViewer } from './TextViewer'
import type { TextFile } from './TextViewer'
import { OfficeViewer, isOffice } from './OfficeViewer'
import type { OfficeFile } from './OfficeViewer'
import { baseName, crumbs, filesUrl, formatModified, formatSize, fullDate, gitText, joinPath, kindLabel, parentOf, readSecrets, shapeOf } from './files'
import type { FileEntry, FileList, FilePreview, FileSearch, FileStat, PlacesData, SecretsState, Shape, SortKey, ViewMode } from './files'
import './Explorer.css'
import { wheelSideways } from './wheelSideways'

const MENU_ICONS = { open: FolderOpen, add: AtSign, terminal: SquareTerminal, rename: Pencil, duplicate: CopyPlus, copyTo: Copy, moveTo: FolderInput, copyPath: Copy, pin: BookMarked, download: Download, trash: Trash2 }

// The desktop Files screen (slices 1 to 3: browse, hand-offs, writes). Layout from Explorer.dc.html: Places, folder tabs, a toolbar, the list or grid, and a details pane.

interface Tab { id: number; path: string; back: string[]; fwd: string[] }
/** session: the chat whose folder the explorer last opened at. A different active chat moves the explorer to its folder. */
interface Stored { tabs: Tab[]; active: number; view: ViewMode; hidden: boolean; sort: SortKey; asc: boolean; session?: string }
const STORE = 'tether-files'
const HOME_TAB = (id = 1): Tab => ({ id, path: '~', back: [], fwd: [] })
function loadStored(): Stored {
  const base: Stored = { tabs: [HOME_TAB()], active: 0, view: 'list', hidden: false, sort: 'name', asc: true }
  try {
    const raw = JSON.parse(localStorage.getItem(STORE) ?? 'null') as Partial<Stored> | null
    if (!raw) return base
    const tabs = Array.isArray(raw.tabs) ? raw.tabs.filter(tab => typeof tab?.path === 'string').slice(0, 12).map((tab, at) => ({ id: at + 1, path: tab.path, back: [], fwd: [] })) : []
    return {
      tabs: tabs.length ? tabs : base.tabs,
      active: Math.min(Math.max(0, Number(raw.active) || 0), Math.max(0, tabs.length - 1)),
      view: raw.view === 'grid' ? 'grid' : 'list', hidden: raw.hidden === true,
      sort: (['name', 'modified', 'size', 'kind'] as const).includes(raw.sort as SortKey) ? raw.sort as SortKey : 'name', asc: raw.asc !== false,
      session: typeof raw.session === 'string' ? raw.session : undefined,
    }
  } catch { return base }
}

/** A row on screen: a folder entry, or a search hit with its full path. */
interface Row extends FileEntry { full: string; where?: string }

const ICONS: Record<Shape, typeof File> = { dir: Folder, image: ImageIcon, archive: Package, doc: File }
function EntryIcon({ entry, size }: { entry: FileEntry; size: number }) {
  const shape = shapeOf(entry)
  const Icon = ICONS[shape]
  return <Icon size={size} aria-hidden="true" className={`fx-icon fx-icon-${shape}`} strokeWidth={size > 24 ? 1.3 : 1.8} />
}

/** Copy text. the server page may be plain http on the tailnet, where navigator.clipboard is missing. */
async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text); return true } catch { /* fall back */ }
  const box = document.createElement('textarea')
  box.value = text; box.setAttribute('readonly', ''); box.style.position = 'fixed'; box.style.opacity = '0'
  document.body.appendChild(box); box.select()
  let done: boolean
  try { done = document.execCommand('copy') } catch { done = false }
  box.remove()
  return done
}

/** Open this folder; with select, select that item in it ("Show in Files" from a chat). */
export interface FilesTarget { path: string; select?: string }
interface Props {
  api: Api
  panes: Pane[]
  /** The active chat. The explorer starts at its folder. */
  pane?: Pane
  /** Open this folder (the panel column's "Open in Files", a chat's "Show in Files"), then call onTarget. */
  target: FilesTarget | null
  onTarget: () => void
  /** Put @paths in the active chat's message draft and go to the chat. */
  onAddToMessage: (paths: string[]) => void
  /** Open a side shell at this folder in the chat's panel column. */
  onTerminalHere: (folder: string) => void
  /** "Pin to project" for one file: the active chat's session project. */
  onPin?: { label: string; run: (path: string) => void }
}

export function Explorer({ api, panes, pane, target, onTarget, onAddToMessage, onTerminalHere, onPin }: Props) {
  const [stored, setStored] = useState(loadStored)
  const [places, setPlaces] = useState<PlacesData | null>(null)
  const sessionCwd = places?.sessions.find(item => item.session === pane?.id)?.cwd
  const { tabs, view, hidden, sort, asc } = stored
  const active = Math.min(stored.active, tabs.length - 1)
  const tab = tabs[active]
  const cwd = tab.path
  useEffect(() => { try { localStorage.setItem(STORE, JSON.stringify({ ...stored, tabs: stored.tabs.map(item => ({ path: item.path })) })) } catch { /* private window */ } }, [stored])
  const patch = useCallback((change: Partial<Stored> | ((current: Stored) => Partial<Stored>)) => setStored(current => ({ ...current, ...(typeof change === 'function' ? change(current) : change) })), [])
  const setTab = useCallback((change: (tab: Tab) => Tab) => patch(current => ({ tabs: current.tabs.map((item, at) => at === Math.min(current.active, current.tabs.length - 1) ? change(item) : item) })), [patch])

  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  useEffect(() => { const timer = setTimeout(() => setSearch(query.trim()), 300); return () => clearTimeout(timer) }, [query])
  const [selected, setSelected] = useState<string[]>([])
  const [focusPath, setFocusPath] = useState('')
  const [toast, setToast] = useState('')
  const [reloadTick, setReloadTick] = useState(0)
  const [full, setFull] = useState<{ media?: Media; text?: TextFile; office?: OfficeFile } | null>(null)
  const focusFirst = useRef(false)
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(''), 2600); return () => clearTimeout(timer) }, [toast])

  // Navigation. Each tab keeps its own back and forward lists.
  const reset = () => { setSelected([]); setFocusPath(''); setQuery(''); setSearch('') }
  const go = useCallback((path: string) => {
    reset()
    setTab(item => item.path === path ? item : { ...item, path, back: [...item.back, item.path].slice(-50), fwd: [] })
  }, [setTab])
  const back = () => { if (!tab.back.length) return; reset(); setTab(item => ({ ...item, path: item.back[item.back.length - 1], back: item.back.slice(0, -1), fwd: [item.path, ...item.fwd] })) }
  const fwd = () => { if (!tab.fwd.length) return; reset(); setTab(item => ({ ...item, path: item.fwd[0], fwd: item.fwd.slice(1), back: [...item.back, item.path] })) }
  const up = () => { const parent = parentOf(cwd); if (parent) go(parent) }
  const pickTab = (at: number) => { if (at === active) return; reset(); patch({ active: at }) }
  const newTab = (path = sessionCwd ?? '~') => { reset(); patch(current => ({ tabs: [...current.tabs, { ...HOME_TAB(Math.max(...current.tabs.map(item => item.id)) + 1), path }], active: current.tabs.length })) }
  const closeTab = (at: number) => {
    if (tabs.length < 2) return
    if (at === active) reset()
    patch(current => { const next = current.tabs.filter((_, n) => n !== at); return { tabs: next, active: Math.min(current.active > at || current.active === next.length ? current.active - 1 : current.active, next.length - 1) } })
  }
  // A folder in a tab: switch to the tab that shows it, or open a new tab. select: then select that item.
  const revealPathRef = useRef('')
  const showIn = (folder: string, select?: string) => {
    const at = tabs.findIndex(item => item.path === folder)
    if (at === active) go(folder); else if (at >= 0) pickTab(at); else newTab(folder)
    if (select) { setSelected([select]); setFocusPath(select); revealPathRef.current = select }
  }
  // "Open in Files" from the panel column, "Show in Files" from a chat. A chat path may be absolute: stat gives its ~ path and folder.
  useEffect(() => {
    if (!target) return
    onTarget()
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Consume the external Files target in the same effect as navigation and focus.
    if (pane && stored.session !== pane.id) patch({ session: pane.id })
    if (!target.select) { showIn(target.path); return }
    api<FileStat & { parent?: string | null }>(filesUrl.stat(target.select)).then(
      stat => { if (stat.secret) setToast('That file is in a secret folder. Turn on “Show secret folders” to see it.'); showIn(stat.parent ?? parentOf(stat.path) ?? '~', stat.secret ? undefined : stat.path) },
      failure => setToast(errorText(failure)))
  })

  // The "Show secret folders" read window. the server runs the clock; this only shows it and turns the switch off on time.
  // The state comes with every list and places answer; the switch POSTs files/secrets.
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
  const secretsLeft = secrets.on ? Math.max(0, (secrets.expires - now) / 1000) : 0
  useEffect(() => {
    if (!secrets.on) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Refresh the wall clock when the server's secret window changes, before the first interval tick.
    setNow(Date.now())
    const tick = setInterval(() => setNow(Date.now()), 15000)
    // the server ends the window at expires_at. Turn the switch off on time; the listing reloads without secret items.
    const end = setTimeout(() => { setSecrets({ on: false, expires: 0 }); setToast('Secret folders are hidden again.') }, Math.max(0, secrets.expires - Date.now()) + 250)
    return () => { clearInterval(tick); clearTimeout(end) }
  }, [secrets])

  // The folder listing, or the search results under this folder. Sort and filters run on the server.
  const [listing, setListing] = useState<{ key: string; attempt: number; data?: FileList | FileSearch; error?: string; status?: number } | null>(null)
  const listKey = `${cwd}|${hidden}|${sort}|${asc}|${search}|${secrets.on}`
  useEffect(() => {
    const controller = new AbortController()
    const url = search ? filesUrl.search(cwd, search, hidden) : filesUrl.list(cwd, hidden, sort, asc)
    api<FileList | FileSearch>(url, undefined, controller.signal).then(
      data => { if (controller.signal.aborted) return; setListing({ key: listKey, attempt: reloadTick, data }); if ('secrets' in data) takeSecrets(data.secrets) },
      failure => { if (!controller.signal.aborted) setListing({ key: listKey, attempt: reloadTick, error: errorText(failure), status: failure instanceof ApiError ? failure.status : 0 }) })
    return () => controller.abort()
  }, [api, cwd, hidden, sort, asc, search, listKey, reloadTick, takeSecrets])
  // Keep a failed load mounted during retries; other reloads keep their loading state.
  const current = listing?.key === listKey && (listing.attempt === reloadTick || listing.error) ? listing : null
  const loadingList = !current || current.attempt !== reloadTick
  const entries = useMemo(() => { const data = current?.data; return data ? 'hits' in data ? data.hits : data.entries : [] }, [current])
  const rows: Row[] = useMemo(() => entries.map(entry => {
    const full = entry.path ?? joinPath(cwd, entry.name)
    const where = search && entry.path ? parentOf(entry.path) ?? undefined : undefined
    return { ...entry, full, where: where && where !== cwd ? where.slice(cwd.length + 1) || undefined : undefined }
  }), [entries, cwd, search])
  const [lastRows, setLastRows] = useState<Row[]>([])
  if (current && lastRows !== rows) setLastRows(rows)
  // Keep the old rows while the next folder loads, so the list does not flash empty.
  const shownRows = current ? rows : lastRows
  const byPath = useMemo(() => new Map(shownRows.map(row => [row.full, row])), [shownRows])
  const picked = selected.map(path => byPath.get(path)).filter((row): row is Row => !!row)
  const one = picked.length === 1 ? picked[0] : null
  const focused = shownRows.some(row => row.full === focusPath) ? focusPath : shownRows[0]?.full ?? ''

  // "Show in Files": scroll the selected item into view once its folder has loaded.
  useEffect(() => {
    const path = revealPathRef.current
    if (!path || !current) return
    revealPathRef.current = ''
    const node = listRef.current?.querySelector<HTMLElement>(`[data-path="${CSS.escape(path)}"]`)
    // A new folder shows its name field: focus that, not the row, or the field loses focus and keeps the name.
    if (node) { (node.querySelector<HTMLElement>('.fx-rename') ?? node).focus(); node.scrollIntoView({ block: 'center' }) }
  }, [current])
  useEffect(() => {
    if (!focusFirst.current || !current) return
    focusFirst.current = false
    listRef.current?.querySelector<HTMLElement>('[data-path]')?.focus()
  }, [current])

  // Places: pinned folders, a folder for each session at its cwd, and the disk.
  useEffect(() => {
    const controller = new AbortController()
    const load = () => api<PlacesData>(filesUrl.places(), undefined, controller.signal).then(data => { setPlaces(data); takeSecrets(data.secrets) }, () => {})
    void load()
    const timer = setInterval(load, 60000)
    // Back from another tab or app: the secrets window may have ended or opened elsewhere.
    const again = () => { if (document.visibilityState === 'visible') void load() }
    document.addEventListener('visibilitychange', again)
    return () => { clearInterval(timer); controller.abort(); document.removeEventListener('visibilitychange', again) }
  }, [api, takeSecrets])

  // Details: stat and preview of the one selected item.
  const [detail, setDetail] = useState<{ path: string; stat?: FileStat; preview?: FilePreview; error?: string } | null>(null)
  const detailPath = one?.full ?? ''
  const detailDir = !!one?.dir
  const [detailFor, setDetailFor] = useState({ api, path: detailPath, dir: detailDir })
  if (detailFor.api !== api || detailFor.path !== detailPath || detailFor.dir !== detailDir) {
    setDetailFor({ api, path: detailPath, dir: detailDir })
    if (detailPath) setDetail({ path: detailPath })
  }
  useEffect(() => {
    if (!detailPath) return
    const controller = new AbortController()
    const signal = controller.signal
    api<FileStat>(filesUrl.stat(detailPath), undefined, signal).then(stat => setDetail(old => old?.path === detailPath ? { ...old, stat } : old),
      failure => { if (!signal.aborted) setDetail(old => old?.path === detailPath ? { ...old, error: errorText(failure) } : old) })
    if (!detailDir) api<FilePreview>(filesUrl.preview(detailPath), undefined, signal).then(preview => setDetail(old => old?.path === detailPath ? { ...old, preview } : old),
      failure => { if (!signal.aborted) setDetail(old => old?.path === detailPath ? { ...old, preview: { kind: 'none', reason: errorText(failure) } } : old) })
    return () => controller.abort()
  }, [api, detailPath, detailDir])
  const shownDetail = detail?.path === detailPath ? detail : null

  // Is this folder itself secret? Only possible while the window is open; then stat tells. Until it answers, treat it as secret.
  const [cwdStat, setCwdStat] = useState<{ path: string; secret: boolean } | null>(null)
  useEffect(() => {
    if (!secrets.on) return
    const controller = new AbortController()
    api<FileStat>(filesUrl.stat(cwd), undefined, controller.signal).then(stat => setCwdStat({ path: cwd, secret: !!stat.secret }), () => {})
    return () => controller.abort()
  }, [api, cwd, secrets.on])
  const inSecret = secrets.on && (cwdStat?.path !== cwd || cwdStat.secret)
  // Secret items never go into a message, a download or a write.
  const plain = (items: Row[]) => !inSecret && items.length > 0 && !items.some(row => row.secret)
  const addLabel = pane ? `Add to ${paneTitle(pane)} message` : 'Add to message'
  const addToMessage = (items: Row[]) => { if (plain(items)) onAddToMessage(items.map(row => row.full)) }
  const terminalHere = (folder: string) => { if (!inSecret) onTerminalHere(folder) }

  // Actions.
  const copyPath = async (path: string) => setToast(await copyText(path) ? `Copied ${path}` : 'Could not copy. Select the path and copy it.')
  const openFull = (row: Row) => {
    if (row.dir) { go(row.full); return }
    const shape = shapeOf(row)
    const preview = shownDetail?.path === row.full ? shownDetail.preview : undefined
    if (row.secret || shownDetail?.path === row.full && shownDetail.stat?.secret) return
    if (isOffice(row.name)) { setFull({ office: { path: row.full, url: filesUrl.raw(row.full) } }); return }
    if (preview?.kind === 'none') return
    if (shape === 'image' || row.kind === 'video' || row.kind === 'audio') setFull({ media: { name: row.name, url: filesUrl.raw(row.full), kind: row.kind === 'video' || row.kind === 'audio' ? row.kind : 'image' } })
    else if (row.kind === 'pdf') window.open(filesUrl.raw(row.full), '_blank', 'noopener')
    else if (preview?.kind === 'text' || row.kind === 'code' || row.kind === 'text') setFull({ text: { path: row.full, url: filesUrl.raw(row.full), ...(current?.data && 'repo' in current.data ? { repo: !!current.data.repo } : {}) } })
  }
  const activate = (row: Row, keyboard = false) => {
    if (row.dir) { if (keyboard) focusFirst.current = true; go(row.full) }
    else { setSelected([row.full]); setFocusPath(row.full) }
  }
  const toggleSelect = (row: Row) => setSelected(current => current.includes(row.full) ? current.filter(path => path !== row.full) : [...current, row.full])
  const sortBy = (key: SortKey) => patch(current => ({ sort: key, asc: current.sort === key ? !current.asc : true }))

  // Writes (slice 3). Every write reloads the folder and selects what it made. Secret items never get a write.
  const [menu, setMenu] = useState<{ at: Anchor; row: Row } | null>(null)
  const [renaming, setRenaming] = useState('')
  const [trash, setTrash] = useState<Row[] | null>(null)
  const [zip, setZip] = useState<string[] | null>(null)
  const [picker, setPicker] = useState<{ move: boolean; rows: Row[] } | null>(null)
  const [dropping, setDropping] = useState(false)
  const uploadInput = useRef<HTMLInputElement>(null)
  const reload = (select?: string[]) => {
    setReloadTick(n => n + 1)
    if (select) { setSelected(select); setFocusPath(select[0] ?? ''); revealPathRef.current = select[0] ?? '' }
  }
  const writable = (items: Row[]) => !inSecret && plain(items)
  const canUpload = !inSecret && !search && !current?.error
  // Reload this folder when an upload into it ends. Uploads keep going on other screens.
  const cwdRef = useRef(cwd)
  useEffect(() => { cwdRef.current = cwd }, [cwd])
  useEffect(() => {
    setUploadDone(upload => { if (upload.folder === cwdRef.current) setReloadTick(n => n + 1) })
    return () => setUploadDone(null)
  }, [])
  const upload = (list: File[]) => { if (canUpload && list.length) startUploads(cwd, list) }
  const newFolder = async () => {
    try { const made = await writes.mkdir(api, cwd, 'New folder'); reload([made.path]); setRenaming(made.path) }
    catch (failure) { setToast(errorText(failure)) }
  }
  const rename = async (row: Row, name: string) => {
    const wanted = name.trim()
    if (!wanted || wanted === row.name) { setRenaming(''); return }
    try { const done = await writes.rename(api, row.full, wanted); setRenaming(''); reload([done.path]) }
    catch (failure) { setRenaming(''); setToast(errorText(failure)) }
  }
  const duplicate = async (row: Row) => {
    try {
      const made = await writes.duplicate(api, row.full)
      setToast(`Made ${made.name}.${made.skipped?.secret ? ` ${made.skipped.secret} secret items were left out.` : ''}`)
      reload([made.path])
    } catch (failure) { setToast(errorText(failure)) }
  }
  /** Returns an error for the dialog, or null when it can close. */
  const transfer = async (move: boolean, rows: Row[], folder: string) => {
    try {
      const result = await (move ? writes.move : writes.copy)(api, rows.map(row => row.full), folder)
      if (!result.done.length && result.failed.length) return summary('', result)
      setPicker(null)
      setToast(summary(`${move ? 'Moved' : 'Copied'} to ${baseName(folder)}:`, result))
      reload(folder === cwd ? result.done.map(item => item.path) : move ? [] : undefined)
      return null
    } catch (failure) { return errorText(failure) }
  }
  const toTrash = async (rows: Row[]) => {
    try {
      const result = await writes.trash(api, rows.map(row => row.full))
      if (!result.done.length && result.failed.length) return summary('', result)
      setTrash(null)
      setToast(summary('Moved to the trash:', result))
      reload([])
      return null
    } catch (failure) { return errorText(failure) }
  }
  const download = (rows: Row[]) => {
    if (!plain(rows)) return
    if (rows.length === 1 && !rows[0].dir) {
      const link = document.createElement('a')
      link.href = filesUrl.raw(rows[0].full, true); link.download = rows[0].name; link.click()
      return
    }
    setZip(rows.map(row => row.full))
  }
  const askTrash = (rows: Row[]) => { if (writable(rows)) setTrash(rows) }
  const startRename = (row: Row) => { if (writable([row])) { setSelected([row.full]); setFocusPath(row.full); setRenaming(row.full) } }
  const menuItems = (row: Row): MenuItem[] => {
    const rows = selected.includes(row.full) && picked.length > 1 ? picked : [row]
    const many = rows.length > 1
    const canChange = writable(rows)
    const items: MenuItem[] = []
    if (!many) items.push({ label: row.dir ? 'Open folder' : 'Open', icon: MENU_ICONS.open, run: () => row.dir ? go(row.full) : openFull(row) })
    if (plain(rows)) items.push({ label: addLabel, icon: MENU_ICONS.add, run: () => addToMessage(rows) })
    if (!many && !inSecret && !row.secret) items.push({ label: 'Terminal here', icon: MENU_ICONS.terminal, run: () => terminalHere(row.dir ? row.full : parentOf(row.full) ?? cwd) })
    if (canChange && !many) items.push({ label: 'Rename', icon: MENU_ICONS.rename, key: 'F2', gap: true, run: () => startRename(row) })
    if (canChange && !many) items.push({ label: 'Duplicate', icon: MENU_ICONS.duplicate, run: () => void duplicate(row) })
    if (canChange) items.push({ label: 'Copy to…', icon: MENU_ICONS.copyTo, gap: many, run: () => setPicker({ move: false, rows }) })
    if (canChange) items.push({ label: 'Move to…', icon: MENU_ICONS.moveTo, run: () => setPicker({ move: true, rows }) })
    if (!many) items.push({ label: 'Copy path', icon: MENU_ICONS.copyPath, gap: !canChange, run: () => void copyPath(row.full) })
    if (!many && !row.dir && !row.secret && !inSecret && onPin) items.push({ label: onPin.label, icon: MENU_ICONS.pin, run: () => onPin.run(row.full) })
    if (plain(rows)) items.push({ label: many || row.dir ? 'Download as .zip' : 'Download', icon: MENU_ICONS.download, run: () => download(rows) })
    if (canChange) items.push({ label: 'Move to trash', icon: MENU_ICONS.trash, key: 'Del', danger: true, gap: true, run: () => askTrash(rows) })
    return items
  }
  const menuAtFocus = (row: Row) => {
    const box = listRef.current?.querySelector<HTMLElement>(`[data-path="${CSS.escape(row.full)}"]`)?.getBoundingClientRect()
    if (box) setMenu({ at: { x: box.left + 40, y: box.bottom + 2 }, row })
  }
  const openMenu = (event: React.MouseEvent, row: Row) => {
    event.preventDefault(); event.stopPropagation()
    if (!selected.includes(row.full)) setSelected([row.full])
    setFocusPath(row.full)
    setMenu({ at: anchorOf(event), row })
  }

  // Keys in the list or grid: arrows move, Enter opens, Backspace goes up, Space checks, ⌘A selects all, Esc clears.
  const moveFocus = (path: string | undefined, select = true) => {
    if (!path) return
    setFocusPath(path)
    if (select) setSelected([path])
    const node = listRef.current?.querySelector<HTMLElement>(`[data-path="${CSS.escape(path)}"]`)
    node?.focus(); node?.scrollIntoView({ block: 'nearest' })
  }
  const columns = () => {
    if (view === 'list') return 1
    const grid = listRef.current?.querySelector<HTMLElement>('.fx-grid')
    return grid ? Math.max(1, getComputedStyle(grid).gridTemplateColumns.split(' ').length) : 1
  }
  const onListKey = (event: React.KeyboardEvent) => {
    if ((event.target as Element).closest('input, button[data-cell]')) return
    const at = shownRows.findIndex(row => row.full === focused)
    const row = shownRows[at]
    const step = columns()
    const mod = event.metaKey || event.ctrlKey
    const keys: Record<string, () => void> = {
      ArrowDown: () => moveFocus(shownRows[Math.min(shownRows.length - 1, at + step)]?.full),
      ArrowUp: () => moveFocus(shownRows[Math.max(0, at - step)]?.full),
      ArrowRight: () => { if (view === 'grid') moveFocus(shownRows[at + 1]?.full) },
      ArrowLeft: () => { if (view === 'grid') moveFocus(shownRows[at - 1]?.full) },
      Home: () => moveFocus(shownRows[0]?.full),
      End: () => moveFocus(shownRows[shownRows.length - 1]?.full),
      Enter: () => { if (row) { if (row.dir) activate(row, true); else openFull(row) } },
      Backspace: () => { if (parentOf(cwd)) { focusFirst.current = true; up() } },
      ' ': () => { if (row) toggleSelect(row) },
      Escape: () => setSelected([]),
      F2: () => { if (row) startRename(row) },
      Delete: () => askTrash(picked.length ? picked : row ? [row] : []),
      ContextMenu: () => { if (row) menuAtFocus(row) },
    }
    if (mod && event.key.toLowerCase() === 'a') { event.preventDefault(); setSelected(shownRows.map(item => item.full)); return }
    if (mod && event.key === 'Backspace') { event.preventDefault(); askTrash(picked.length ? picked : row ? [row] : []); return }
    if (event.shiftKey && event.key === 'F10') { event.preventDefault(); if (row) menuAtFocus(row); return }
    if (mod || event.altKey) return
    const action = keys[event.key]
    if (!action) return
    event.preventDefault()
    action()
  }
  // Alt+←/→ go back and forward, as in a browser. Only while this screen shows and no dialog is open.
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (!event.altKey || event.ctrlKey || event.metaKey || document.querySelector('dialog[open]')) return
      if ((event.target as Element | null)?.closest('input, textarea')) return
      if (event.key === 'ArrowLeft') { event.preventDefault(); document.querySelector<HTMLButtonElement>('.explorer [data-nav="back"]')?.click() }
      if (event.key === 'ArrowRight') { event.preventDefault(); document.querySelector<HTMLButtonElement>('.explorer [data-nav="fwd"]')?.click() }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [])

  // Start at the active chat's folder: on the first visit, and when the active chat changed since the last visit.
  if (places && pane && stored.session !== pane.id) {
    if (sessionCwd && !target) { const at = tabs.findIndex(item => item.path === sessionCwd); if (at >= 0) pickTab(at); else if (cwd !== sessionCwd) go(sessionCwd) }
    patch({ session: pane.id })
  }

  const pinned = (places?.pinned ?? []).map(item => typeof item === 'string' ? { path: item, label: baseName(item) } : { path: item.path, label: item.name || baseName(item.path) })
  const sessionDot = (id: string) => {
    const own = panes.filter(item => item.id === id)
    if (own.some(item => needsYou(item.state))) return { kind: 'approval', label: 'Needs you' }
    if (own.some(item => item.state === 'Working')) return { kind: 'working', label: 'Working' }
    if (own.some(item => item.state === 'Unknown')) return { kind: 'unknown', label: 'Unknown' }
    return { kind: 'idle', label: own.length ? 'Idle' : 'Ended' }
  }
  const place = (path: string, label: string, icon: ReactNode, extra?: ReactNode) => <li key={`${label}|${path}`}>
    <button type="button" className="fx-place" aria-current={cwd === path ? 'page' : undefined} title={path} onClick={() => go(path)}>{icon}<span className="fx-place-name">{label}</span>{extra}</button>
  </li>
  const disk = places?.disk
  const diskFree = disk ? disk.free ?? Math.max(0, disk.total - disk.used) : 0

  const folderName = baseName(cwd)
  const count = current ? entries.length : shownRows.length
  const totalSize = picked.reduce((sum, row) => sum + (row.dir ? 0 : row.size ?? 0), 0)
  const truncated = !!current?.data?.truncated
  const arrow = (key: SortKey) => sort === key ? asc ? <ArrowUp size={12} aria-hidden="true" /> : <ArrowDown size={12} aria-hidden="true" /> : null
  const ariaSort = (key: SortKey) => search ? undefined : sort === key ? asc ? 'ascending' as const : 'descending' as const : 'none' as const
  const head = (key: SortKey, label: string, className: string) => <span role="columnheader" aria-sort={ariaSort(key)} className={className}>
    <button type="button" data-cell disabled={!!search} onClick={() => sortBy(key)}>{label}{!search && arrow(key)}</button>
  </span>
  const failed = current?.error
  const emptyText = search ? `Nothing in ${folderName} matches “${search}”.` : 'This folder is empty.'

  return <section className="explorer" aria-label="Files">
    <aside className="fx-places" aria-label="Places">
      <h1 className="fx-title">Files</h1>
      <div className="fx-places-scroll">
        <h2 className="fx-group">Pinned</h2>
        <ul className="fx-place-list">
          {(pinned.length ? pinned : [{ path: '~', label: 'Home' }]).map(item => place(item.path, item.label, <Folder size={15} aria-hidden="true" className="fx-icon-dir-line" />))}
        </ul>
        {!!places?.sessions.length && <>
          <h2 className="fx-group">Session folders</h2>
          <ul className="fx-place-list">
            {places.sessions.map(item => { const dot = sessionDot(item.session); return place(item.cwd, item.name || baseName(item.cwd), <MessageCircle size={15} aria-hidden="true" className="fx-muted-icon" />,
              <span className="state-slot"><span className={`state-dot ${dot.kind}`} role="img" aria-label={dot.label} title={dot.label} /></span>) })}
          </ul>
        </>}
      </div>
      {disk && disk.total > 0 && <div className="fx-disk">
        <div className="fx-disk-head"><span>Disk</span><span>{formatSize(diskFree)} free</span></div>
        <div className="fx-meter" role="meter" aria-label="Disk used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(disk.used / disk.total * 100)}><span style={{ width: `${Math.min(100, disk.used / disk.total * 100).toFixed(1)}%` }} /></div>
        <div className="fx-disk-note">{formatSize(disk.used)} used of {formatSize(disk.total)}</div>
      </div>}
    </aside>

    <div className="fx-main" onDragOver={event => {
        if (!canUpload || !event.dataTransfer.types.includes('Files')) return
        event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; setDropping(true)
      }} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropping(false) }}
      onDrop={event => { if (!canUpload || !event.dataTransfer.files.length) return; event.preventDefault(); setDropping(false); upload([...event.dataTransfer.files]) }}>
      {dropping && <div className="fx-drop" aria-hidden="true"><Upload size={22} />Drop to upload to {folderName}</div>}
      <input ref={uploadInput} type="file" multiple hidden onChange={event => { upload([...event.target.files ?? []]); event.target.value = '' }} />
      <div ref={wheelSideways} className="fx-tabs" role="tablist" aria-label="Folder tabs">
        {tabs.map((item, at) => <div key={item.id} className="fx-tab" data-on={at === active || undefined}>
          <button type="button" role="tab" aria-selected={at === active} className="fx-tab-label" title={item.path} onClick={() => pickTab(at)}>{baseName(item.path)}</button>
          {tabs.length > 1 && <button type="button" className="fx-ib fx-tab-close" aria-label={`Close tab ${baseName(item.path)}`} onClick={() => closeTab(at)}><X size={12} strokeWidth={2.4} /></button>}
        </div>)}
        <button type="button" className="fx-ib fx-tab-new" aria-label="New tab" title="New tab" onClick={() => newTab()}><Plus size={15} /></button>
      </div>

      <div className="fx-navbar">
        <button type="button" className="fx-ib" data-nav="back" aria-label="Back" title="Back (Alt+←)" disabled={!tab.back.length} onClick={back}><ChevronLeft size={16} /></button>
        <button type="button" className="fx-ib" data-nav="fwd" aria-label="Forward" title="Forward (Alt+→)" disabled={!tab.fwd.length} onClick={fwd}><ChevronRight size={16} /></button>
        <button type="button" className="fx-ib" aria-label="Parent folder" title="Parent folder (Backspace)" disabled={!parentOf(cwd)} onClick={up}><ArrowUp size={16} /></button>
        <nav className="fx-path" aria-label="Path">
          <ol>{crumbs(cwd).map((crumb, at, all) => <li key={crumb.path}>
            {at === all.length - 1 ? <span className="fx-crumb" aria-current="page">{crumb.label}</span>
              : <button type="button" className="fx-crumb" onClick={() => go(crumb.path)}>{crumb.label}</button>}
          </li>)}</ol>
          <button type="button" className="fx-ib fx-copy" aria-label="Copy folder path" title="Copy folder path" onClick={() => void copyPath(cwd)}><Copy size={14} /></button>
        </nav>
        <label className="fx-search">
          <Search size={14} aria-hidden="true" />
          <span className="fx-sr">Search files</span>
          <input type="search" value={query} placeholder={`Search in ${folderName}`} onChange={event => setQuery(event.target.value)}
            onKeyDown={event => { if (event.key === 'Escape' && query) { event.preventDefault(); event.stopPropagation(); setQuery(''); setSearch('') } if (event.key === 'Enter') setSearch(query.trim()) }} />
        </label>
      </div>

      <div className="fx-toolbar">
        <div className="fx-tb-actions">
        {picked.length > 0 ? <>
          <span className="fx-selcount">{picked.length} selected</span>
          {plain(picked) && <button type="button" className="fx-tb fx-tb-primary" title={addLabel} onClick={() => addToMessage(picked)}><AtSign size={14} aria-hidden="true" /><span className="fx-tb-text">{addLabel}</span></button>}
          {plain(picked) && <button type="button" className="fx-tb" onClick={() => download(picked)}><Download size={14} aria-hidden="true" /><span className="fx-tb-text">Download</span></button>}
          {writable(picked) && <>
            <button type="button" className="fx-tb" onClick={() => setPicker({ move: false, rows: picked })}><Copy size={14} aria-hidden="true" /><span className="fx-tb-text">Copy to…</span></button>
            <button type="button" className="fx-tb" onClick={() => setPicker({ move: true, rows: picked })}><FolderInput size={14} aria-hidden="true" /><span className="fx-tb-text">Move to…</span></button>
            <button type="button" className="fx-tb fx-tb-danger" title="Move to trash (Delete)" onClick={() => askTrash(picked)}><Trash2 size={14} aria-hidden="true" /><span className="fx-tb-text">Delete</span></button>
          </>}
          <button type="button" className="fx-tb fx-tb-quiet" onClick={() => setSelected([])}>Clear</button>
        </> : !search ? <>
          {canUpload && <>
            <button type="button" className="fx-tb" onClick={() => void newFolder()}><FolderPlus size={14} aria-hidden="true" />New folder</button>
            <button type="button" className="fx-tb" title="Upload files to this folder. You can also drop files here." onClick={() => uploadInput.current?.click()}><Upload size={14} aria-hidden="true" />Upload</button>
          </>}
          {!inSecret && <button type="button" className="fx-tb" title="Open a terminal in this folder, beside the chat" onClick={() => terminalHere(cwd)}><SquareTerminal size={14} aria-hidden="true" />Terminal here</button>}
        </> : search ? <span className="fx-toolbar-note" role="status">{loadingList ? 'Searching…' : `${count}${truncated ? '+' : ''} ${count === 1 ? 'match' : 'matches'} in ${folderName} and its folders`}</span> : null}
        </div>
        <div className="fx-tb-fixed">
        <button type="button" className="fx-tb" aria-pressed={hidden} onClick={() => patch({ hidden: !hidden })}>Hidden files</button>
        <button type="button" className="fx-tb" aria-pressed={secrets.on} title="Show folders that hold keys and tokens, read only, for 10 minutes" onClick={() => void switchSecrets(!secrets.on)}>
          <Lock size={13} aria-hidden="true" />{secrets.on ? <>Secret folders <span className="fx-tb-time">{Math.max(1, Math.ceil(secretsLeft / 60))} min left</span></> : 'Show secret folders'}
        </button>
        <div className="fx-viewswitch" role="group" aria-label="View">
          <button type="button" className="fx-ib" aria-label="List view" aria-pressed={view === 'list'} onClick={() => patch({ view: 'list' })}><List size={15} /></button>
          <button type="button" className="fx-ib" aria-label="Grid view" aria-pressed={view === 'grid'} onClick={() => patch({ view: 'grid' })}><LayoutGrid size={15} /></button>
        </div>
        </div>
      </div>

      <div className="fx-body">
        <div className="fx-content" ref={listRef} onKeyDown={onListKey} aria-busy={loadingList || undefined}>
          {failed ? <div className="fx-empty" role="alert">
              <p>{failed}</p>
              <div className="fx-empty-actions">
                <RetryButton className="fx-tb" busy={loadingList} onRetry={() => setReloadTick(n => n + 1)} />
                {cwd !== '~' && <button type="button" className="fx-tb" onClick={() => go('~')}>Go to Home</button>}
              </div>
            </div>
          : view === 'list' ? <div role="grid" aria-label={search ? `Search results in ${folderName}` : `Contents of ${folderName}`} aria-multiselectable="true" aria-rowcount={shownRows.length + 1} className="fx-list">
              <div role="row" className="fx-row fx-head">
                <span role="columnheader" className="fx-c-check"><span className="fx-sr">Select</span></span>
                {head('name', 'Name', 'fx-c-name')}
                {head('modified', 'Modified', 'fx-c-mod')}
                {head('size', 'Size', 'fx-c-size')}
                {head('kind', 'Kind', 'fx-c-kind')}
                <span role="columnheader" className="fx-c-more"><span className="fx-sr">Options</span></span>
              </div>
              {shownRows.map(row => {
                const on = selected.includes(row.full)
                return <div key={row.full} role="row" data-path={row.full} aria-selected={on} tabIndex={row.full === focused ? 0 : -1}
                  className={`fx-row${on ? ' selected' : ''}${row.hidden || row.secret ? ' dim' : ''}`}
                  onFocus={event => { if (event.target === event.currentTarget) setFocusPath(row.full) }}
                  onClick={() => { setFocusPath(row.full); activate(row) }} onDoubleClick={() => { if (!row.dir) openFull(row) }} onContextMenu={event => openMenu(event, row)}>
                  <span role="gridcell" className="fx-c-check"><input type="checkbox" tabIndex={-1} aria-label={`Select ${row.name}`} checked={on} onClick={event => event.stopPropagation()} onChange={() => toggleSelect(row)} /></span>
                  <span role="gridcell" className="fx-c-name">
                    <EntryIcon entry={row} size={18} />
                    {renaming === row.full ? <RenameBox name={row.name} onDone={name => { if (name === null) { setRenaming(''); moveFocus(row.full, false) } else void rename(row, name) }} />
                      : <span className="fx-name-text"><span className="fx-name">{row.name}</span>{row.where && <span className="fx-where">{row.where}</span>}</span>}
                    {row.secret && <Lock size={12} className="fx-secret" aria-label="Secret folder, read only" />}
                    {row.git && <span className="fx-git" title={gitText(row.git)} aria-label={gitText(row.git)}>{row.git}</span>}
                  </span>
                  <span role="gridcell" className="fx-c-mod">{formatModified(row.modified)}</span>
                  <span role="gridcell" className="fx-c-size">{row.dir ? '—' : formatSize(row.size)}</span>
                  <span role="gridcell" className="fx-c-kind">{kindLabel(row)}</span>
                  <span role="gridcell" className="fx-c-more"><button type="button" data-cell tabIndex={-1} className="fx-ib" aria-label={`Options for ${row.name}`} title="Options" onClick={event => openMenu(event, row)}><MoreHorizontal size={15} /></button></span>
                </div>
              })}
              {loadingList && !shownRows.length && <SkeletonRows count={8} label="Loading files" className="fx-skeleton" icon={18} meta={false} />}
              {!loadingList && !shownRows.length && <div className="fx-empty">{emptyText}</div>}
            </div>
          : <div className="fx-grid" role="listbox" aria-label={search ? `Search results in ${folderName}` : `Contents of ${folderName}`} aria-multiselectable="true">
              {shownRows.map(row => {
                const on = selected.includes(row.full)
                const thumb = shapeOf(row) === 'image' && (row.size ?? 0) < 8 * 1024 * 1024 && !row.secret
                return <div key={row.full} role="option" data-path={row.full} aria-selected={on} tabIndex={row.full === focused ? 0 : -1} title={row.where ? `${row.where}/${row.name}` : row.name}
                  className={`fx-tile${on ? ' selected' : ''}${row.hidden || row.secret ? ' dim' : ''}`}
                  onFocus={event => { if (event.target === event.currentTarget) setFocusPath(row.full) }}
                  onClick={() => { setFocusPath(row.full); activate(row) }} onDoubleClick={() => { if (!row.dir) openFull(row) }} onContextMenu={event => openMenu(event, row)}>
                  {thumb ? <img className="fx-thumb" src={filesUrl.raw(row.full)} alt="" loading="lazy" /> : <EntryIcon entry={row} size={40} />}
                  {renaming === row.full ? <RenameBox name={row.name} onDone={name => { if (name === null) { setRenaming(''); moveFocus(row.full, false) } else void rename(row, name) }} />
                    : <span className="fx-tile-name">{row.git && <span className="fx-git" aria-label={gitText(row.git)}>{row.git}</span>}<span>{row.name}</span></span>}
                </div>
              })}
              {loadingList && !shownRows.length && <SkeletonRows count={8} label="Loading files" className="fx-skeleton" icon={18} meta={false} />}
              {!loadingList && !shownRows.length && <div className="fx-empty">{emptyText}</div>}
            </div>}
        </div>

        <aside className="fx-details" aria-label="Details">
          {one ? <Details row={one} detail={shownDetail} onOpen={() => openFull(one)} onCopy={() => void copyPath(one.full)} add={plain([one]) ? { label: addLabel, run: () => addToMessage([one]) } : undefined} />
            : picked.length > 1 ? <div className="fx-details-head">
                <h2>{picked.length} items selected</h2>
                <p className="fx-sub">{formatSize(totalSize)} in total{picked.some(row => row.dir) ? ', not counting folders' : ''}</p>
                <ul className="fx-selnames">{picked.slice(0, 12).map(row => <li key={row.full}>{row.name}</li>)}{picked.length > 12 && <li className="fx-sub">and {picked.length - 12} more</li>}</ul>
                {plain(picked) && <div className="fx-details-actions"><button type="button" className="fx-tb fx-tb-wide" onClick={() => download(picked)}><Download size={14} aria-hidden="true" />Download as .zip</button></div>}
              </div>
            : <div className="fx-details-head">
                <h2>{folderName}</h2>
                <p className="fx-sub">{loadingList ? 'Loading…' : `${count}${truncated ? '+' : ''} ${count === 1 ? 'item' : 'items'}`}</p>
                <p className="fx-sub fx-hint">Select a file to see a preview here.</p>
              </div>}
        </aside>
      </div>

      <footer className="fx-status">
        <span>{loadingList ? 'Loading…' : `${count}${truncated ? '+' : ''} ${count === 1 ? 'item' : 'items'}`}</span>
        {picked.length > 0 && <span>{picked.length} selected · {formatSize(totalSize)}</span>}
        {truncated && <span>{search ? 'Showing the first 500 matches.' : 'Showing the first items only.'}</span>}
        <span className="fx-spacer" />
        {inSecret && <span>Read only</span>}
      </footer>
    </div>
    {toast && <div className="toast app-toast fx-toast" role="status">{toast}</div>}
    <ImageViewer image={full?.media ?? null} close={() => setFull(null)} />
    <TextViewer file={full?.text ?? null} close={() => setFull(null)} />
    <OfficeViewer file={full?.office ?? null} close={() => setFull(null)} />
    <UploadPanel />
    {/* eslint-disable-next-line react-hooks/refs -- menuItems only stores event handlers; their focus and reveal refs are accessed after a user action. */}
    {menu && <ItemMenu at={menu.at} title={menu.row.name} items={menuItems(menu.row)} onClose={() => setMenu(null)} />}
    {trash && <TrashConfirm names={trash.map(row => row.name)} onCancel={() => setTrash(null)} onConfirm={() => toTrash(trash)} />}
    {zip && <ZipConfirm api={api} paths={zip} onClose={() => setZip(null)} />}
    {picker && <FolderPicker api={api} move={picker.move} names={picker.rows.map(row => row.name)} start={cwd} sources={picker.rows.map(row => row.full)} onCancel={() => setPicker(null)} onPick={folder => transfer(picker.move, picker.rows, folder)} />}
  </section>
}

/** The name field for an inline rename. Enter or leaving the field keeps the name; Esc cancels. The stem is selected first. */
function RenameBox({ name, onDone }: { name: string; onDone: (name: string | null) => void }) {
  const [value, setValue] = useState(name)
  const done = useRef(false)
  const finish = (next: string | null) => { if (!done.current) { done.current = true; onDone(next) } }
  return <input className="fx-rename" aria-label={`New name for ${name}`} value={value} autoFocus spellCheck={false}
    onFocus={event => { const dot = name.lastIndexOf('.'); event.target.setSelectionRange(0, dot > 0 ? dot : name.length) }}
    onClick={event => event.stopPropagation()} onDoubleClick={event => event.stopPropagation()}
    onChange={event => setValue(event.target.value)}
    onKeyDown={event => { event.stopPropagation(); if (event.key === 'Enter') { event.preventDefault(); finish(value) } else if (event.key === 'Escape') { event.preventDefault(); finish(null) } }}
    onBlur={() => finish(value)} />
}

const NO_PREVIEW: Record<string, string> = {
  'too-large': 'Too large to preview. Download it or open it in a terminal.',
  binary: 'No preview for this kind of file. Download it to open it.',
  secret: 'Secret file. Only text shows here, and download stays off.',
  unreadable: 'Tether cannot read this file. Check its permissions.',
  folder: 'This is a folder.',
}

function Details({ row, detail, onOpen, onCopy, add }: { row: Row; detail: { stat?: FileStat; preview?: FilePreview; error?: string } | null; onOpen: () => void; onCopy: () => void; add?: { label: string; run: () => void } }) {
  const stat = detail?.stat
  const preview = detail?.preview
  const size = row.dir ? stat?.items != null ? `${stat.items} ${stat.items === 1 ? 'item' : 'items'}` : 'Folder' : formatSize(stat?.size ?? row.size)
  const image = preview?.kind === 'image'
  const media = preview && (preview.kind === 'video' || preview.kind === 'audio' || preview.kind === 'pdf') ? preview : null
  const secret = row.secret || stat?.secret
  const office = !row.dir && !secret && isOffice(row.name)
  const canFull = !row.dir && !secret && (office || image || !!media || preview?.kind === 'text')
  const gitLine = stat ? stat.git ? gitText(stat.git.state) : 'Not in a repository' : row.git ? gitText(row.git) : '…'
  return <>
    <div className="fx-details-head">
      <h2 className="fx-details-name">{row.name}</h2>
      <p className="fx-sub">{kindLabel(row)} · {size}</p>
    </div>
    {!row.dir && (!detail || !preview ? detail?.error ? null : <Loading className="fx-preview fx-preview-note" label="Loading preview…" />
      : preview.kind === 'text' ? <div className="fx-preview fx-code" tabIndex={0} aria-label={`Preview of ${row.name}`}>
          {preview.lines.length ? preview.lines.map((line, at) => <div key={at} className="fx-line"><span className="fx-ln" aria-hidden="true">{at + 1}</span><span className="fx-lt">{line || ' '}</span></div>)
            : <p className="fx-preview-note">This file is empty.</p>}
          {preview.truncated && <p className="fx-preview-note">The preview shows the start of the file.</p>}
        </div>
      : image ? <div className="fx-preview fx-image"><img src={preview.url} alt={`Preview of ${row.name}`} /></div>
      : media?.kind === 'video' ? <div className="fx-preview fx-image"><video src={media.url} controls preload="metadata" aria-label={`Preview of ${row.name}`} /></div>
      : media?.kind === 'audio' ? <div className="fx-preview fx-audio"><audio src={media.url} controls preload="metadata" aria-label={`Preview of ${row.name}`} /></div>
      : media ? <div className="fx-preview fx-preview-note">PDF file. Open it in full view to read it.</div>
      : office ? <div className="fx-preview fx-preview-note">Open in full view to read it.</div>
      : preview.kind === 'none' && <div className="fx-preview fx-preview-note">{NO_PREVIEW[preview.reason] ?? preview.reason}</div>)}
    {detail?.error && <p className="fx-preview-note error" role="alert">{detail.error}</p>}
    <dl className="fx-facts">
      <dt>Path</dt><dd className="fx-mono">{stat?.path ?? row.full}</dd>
      <dt>Modified</dt><dd>{fullDate(stat?.modified ?? row.modified)}</dd>
      <dt>Owner</dt><dd className="fx-mono">{stat ? `${stat.owner ?? '?'} · ${stat.mode ?? '?'}` : '…'}</dd>
      <dt>Git</dt><dd>{gitLine}{stat?.git?.repo && <span className="fx-sub fx-repo">{stat.git.repo}{stat.git.branch ? ` on ${stat.git.branch}` : ''}</span>}</dd>
    </dl>
    <div className="fx-details-actions">
      {add && !secret && <button type="button" className="fx-tb fx-tb-wide fx-tb-primary" onClick={add.run}><AtSign size={14} aria-hidden="true" /><span className="fx-tb-text">{add.label}</span></button>}
      {row.dir ? <button type="button" className="fx-tb fx-tb-wide" onClick={onOpen}><FolderOpen size={14} aria-hidden="true" />Open folder</button>
        : canFull && <button type="button" className="fx-tb fx-tb-wide" onClick={onOpen}>Open in full view</button>}
      {!row.dir && !secret && <a className="fx-tb fx-tb-wide" href={filesUrl.raw(row.full, true)} download={row.name}><Download size={14} aria-hidden="true" />Download</a>}
      <button type="button" className="fx-tb fx-tb-wide" onClick={onCopy}><Copy size={14} aria-hidden="true" />Copy path</button>
      {secret && <p className="fx-sub">Secret folders are read only. Download and writes stay off.</p>}
    </div>
  </>
}
