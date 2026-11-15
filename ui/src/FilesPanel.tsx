import { SkeletonRows } from './Skeleton'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronRight, File, Folder, FolderOpen, FolderSearch } from 'lucide-react'
import { errorText } from './api'
import { folderQuery, useFileOpener, usePanelPoll } from './panelProps'
import type { PanelProps } from './panelProps'

// GET repo/tree: one folder level. Folders first; .git, private folders and secret-looking names are left out by the server.
interface Entry { name: string; dir: boolean; size?: number | null }
interface Listing { folder: string; path: string; entries: Entry[]; truncated: boolean }
interface Row { path: string; name: string; dir: boolean; level: number; parent: string | null }
const POLL_MS = 15000

export function FilesPanel({ api, pane, folder, active }: PanelProps) {
  const query = folderQuery(pane, folder)
  const [listings, setListings] = useState<Record<string, Listing>>({})
  const [failed, setFailed] = useState<Record<string, string>>({})
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [focus, setFocus] = useState('')
  const [open, viewers] = useFileOpener(pane, api)
  const tree = useRef<HTMLUListElement>(null)
  const wanted = useRef(expanded)
  useEffect(() => { wanted.current = expanded }, [expanded])

  const fetchDir = useCallback(async (path: string, signal?: AbortSignal) => {
    try {
      const result = await api<Listing>(`repo/tree?${query}&path=${encodeURIComponent(path)}`, undefined, signal)
      setListings(current => ({ ...current, [path]: result }))
      setFailed(current => { if (!(path in current)) return current; const next = { ...current }; delete next[path]; return next })
    } catch (failure) {
      if (!signal?.aborted) setFailed(current => ({ ...current, [path]: errorText(failure) }))
    }
  }, [api, query])
  useEffect(() => { setListings({}); setFailed({}); setExpanded(new Set()); setFocus('') }, [query])
  // Refresh the root and every open folder while the panel shows.
  const reload = useCallback(async (signal: AbortSignal) => {
    await Promise.all(['', ...wanted.current].map(path => fetchDir(path, signal)))
  }, [fetchDir])
  usePanelPoll(reload, POLL_MS, active)

  const rows = useMemo(() => {
    const out: Row[] = []
    const walk = (path: string, level: number) => {
      for (const entry of listings[path]?.entries ?? []) {
        const child = path ? `${path}/${entry.name}` : entry.name
        out.push({ path: child, name: entry.name, dir: entry.dir, level, parent: path || null })
        if (entry.dir && expanded.has(child)) walk(child, level + 1)
      }
    }
    walk('', 1)
    return out
  }, [listings, expanded])
  const current = rows.some(row => row.path === focus) ? focus : rows[0]?.path ?? ''

  const toggle = (row: Row, to?: boolean) => {
    const next = new Set(expanded)
    const show = to ?? !next.has(row.path)
    if (show) { next.add(row.path); if (!listings[row.path]) void fetchDir(row.path) } else next.delete(row.path)
    setExpanded(next)
  }
  const root = listings['']?.folder ?? folder ?? pane.dir
  const activate = (row: Row) => { if (row.dir) toggle(row); else void open(`${root}/${row.path}`) }
  const move = (path: string | undefined) => {
    if (path === undefined) return
    setFocus(path)
    tree.current?.querySelector<HTMLElement>(`[data-path="${CSS.escape(path)}"]`)?.focus()
  }
  const onKeyDown = (event: React.KeyboardEvent) => {
    const at = rows.findIndex(row => row.path === current)
    const row = rows[at]
    if (!row) return
    const keys: Record<string, () => void> = {
      ArrowDown: () => move(rows[at + 1]?.path),
      ArrowUp: () => move(rows[at - 1]?.path),
      Home: () => move(rows[0]?.path),
      End: () => move(rows[rows.length - 1]?.path),
      ArrowRight: () => { if (row.dir && !expanded.has(row.path)) toggle(row, true); else if (row.dir) move(rows[at + 1]?.parent === row.path ? rows[at + 1].path : undefined) },
      ArrowLeft: () => { if (row.dir && expanded.has(row.path)) toggle(row, false); else move(row.parent ?? undefined) },
      Enter: () => activate(row),
      ' ': () => activate(row),
    }
    const action = keys[event.key]
    if (!action) return
    event.preventDefault()
    action()
  }

  const top = listings['']
  return <div className="repo-panel files-panel">
    {/* The desktop Files screen (Explorer.tsx) opens at this folder. App.tsx listens for the event. */}
    {top && <button type="button" className="files-panel-open" onClick={() => window.dispatchEvent(new CustomEvent('tether:open-files', { detail: { path: top.folder } }))}>
      <FolderSearch size={14} aria-hidden="true" />Open in Files
    </button>}
    {!top ? (failed[''] ? <p className="repo-note" role="alert">{failed['']}</p> : <SkeletonRows count={8} label="Loading files" className="files-skeleton" icon={14} meta={false} />)
      : !top.entries.length ? <div className="repo-empty calm"><p><strong>This folder is empty</strong>Files the agent makes here show up on their own.</p></div>
      : <ul ref={tree} role="tree" aria-label={`Files in ${top.folder}`} className="file-tree" onKeyDown={onKeyDown}>
        {rows.map(row => {
          const isOpen = row.dir && expanded.has(row.path)
          const loading = isOpen && !listings[row.path] && !failed[row.path]
          const Icon = row.dir ? isOpen ? FolderOpen : Folder : File
          return <li key={row.path} role="treeitem" data-path={row.path} aria-level={row.level} aria-expanded={row.dir ? isOpen : undefined}
            aria-busy={loading || undefined} tabIndex={row.path === current ? 0 : -1} className="file-row"
            style={{ '--level': row.level - 1 } as React.CSSProperties}
            onClick={event => { event.stopPropagation(); setFocus(row.path); activate(row) }} onFocus={() => setFocus(row.path)}>
            <span className="file-row-inner">
              {row.dir ? <ChevronRight className="chevron" size={13} aria-hidden="true" /> : <span className="file-gap" aria-hidden="true" />}
              <Icon size={14} aria-hidden="true" className="file-icon" /><span className="file-name">{row.name}</span>
            </span>
            {failed[row.path] && <span className="repo-note file-fail">{failed[row.path]}</span>}
            {listings[row.path]?.truncated && isOpen && <span className="repo-note file-fail">Showing the first 1000 items.</span>}
          </li>
        })}
      </ul>}
    {top?.truncated && <p className="repo-note">Showing the first 1000 items.</p>}
    {viewers}
  </div>
}
