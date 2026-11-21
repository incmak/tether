// The Files explorer's view of GET /mobile/api/files/* (backend files.py, contract in .work-notes/2026-11-02/explorer/API.md). Paths are "~/..." strings.
import { fmtDate, fmtDateTime, fmtTime, sameDay, zonedTime, zoneParts } from './zone'

export type GitMark = 'M' | 'A' | '?' | 'D' | 'R' | null
export interface FileEntry {
  name: string
  dir: boolean
  size: number | null
  modified: number | null
  /** folder | image | video | audio | pdf | archive | code | text | other */
  kind: string
  git?: GitMark
  hidden?: boolean
  link?: boolean
  /** A secret folder or file, shown only while the "Show secret folders" window is open. */
  secret?: boolean
  /** Search hits carry their full path; folder listings do not. */
  path?: string
}
export interface SecretsWire { on: boolean; expires_at: number | null }
export interface FileList { path: string; parent?: string | null; repo?: string | null; entries: FileEntry[]; truncated: boolean; secrets?: SecretsWire }
export interface FileSearch { path: string; q?: string; hits: FileEntry[]; truncated: boolean; timed_out?: boolean }
export interface FileStat {
  path: string
  name?: string
  dir?: boolean
  size: number | null
  modified: number | null
  owner: string | null
  mode: string | null
  /** state is null for an unchanged file. */
  git: { repo: string; branch?: string | null; state: string | null } | null
  mime: string | null
  kind?: string
  items?: number | null
  secret?: boolean
}
export type FilePreview =
  | { kind: 'text'; lines: string[]; truncated: boolean }
  | { kind: 'image' | 'video' | 'audio' | 'pdf'; url: string; mime?: string }
  | { kind: 'none'; reason: 'too-large' | 'binary' | 'folder' | 'secret' | 'unreadable' | string }
export interface PlacesData {
  pinned: (string | { path: string; name?: string; exists?: boolean })[]
  /** One row per live pane; session is the pane id. */
  sessions: { session: string; name?: string; agent?: string; cwd: string }[]
  disk: { used: number; total: number; free?: number } | null
  home?: string
  secrets?: SecretsWire
}
/** The "Show secret folders" read window. the server runs the clock; expires is in ms. */
export interface SecretsState { on: boolean; expires: number }

export type SortKey = 'name' | 'modified' | 'size' | 'kind'
export type ViewMode = 'list' | 'grid'

const q = encodeURIComponent
export const filesUrl = {
  list: (path: string, hidden: boolean, sort: SortKey, asc: boolean) => `files/list?path=${q(path)}&hidden=${hidden ? 1 : 0}&sort=${sort}&order=${asc ? 'asc' : 'desc'}`,
  search: (path: string, query: string, hidden: boolean) => `files/search?path=${q(path)}&q=${q(query)}&hidden=${hidden ? 1 : 0}`,
  stat: (path: string) => `files/stat?path=${q(path)}`,
  preview: (path: string) => `files/preview?path=${q(path)}`,
  places: () => 'files/places',
  /** POST {on}. The state also comes with every list and places answer. */
  secrets: () => 'files/secrets',
  /** Browser URLs (not through api()): image previews, the full view and downloads. */
  raw: (path: string, download = false) => `/mobile/api/files/raw?path=${q(path)}${download ? '&download=1' : ''}`,
}

/** The secrets window as the server sends it: {on, expires_at} in epoch seconds. */
export function readSecrets(data: Partial<SecretsWire> | null | undefined): SecretsState {
  const expires = typeof data?.expires_at === 'number' ? data.expires_at * 1000 : 0
  const on = data?.on === true && expires > Date.now()
  return { on, expires: on ? expires : 0 }
}

export const joinPath = (folder: string, name: string) => folder === '/' ? `/${name}` : `${folder.replace(/\/$/, '')}/${name}`
export const parentOf = (path: string) => {
  if (path === '~' || path === '/') return null
  const at = path.lastIndexOf('/')
  if (at < 0) return null
  return at === 0 ? '/' : path.slice(0, at)
}
export const baseName = (path: string) => path === '~' ? 'Home' : path.split('/').filter(Boolean).pop() ?? path
/** The path bar: one crumb per folder, from Home (or /) down to this folder. */
export function crumbs(path: string): { label: string; path: string }[] {
  const parts = path.split('/')
  const out: { label: string; path: string }[] = []
  parts.forEach((part, at) => {
    if (at === 0 && part === '') { out.push({ label: '/', path: '/' }); return }
    if (!part) return
    const full = at === 0 ? part : parts.slice(0, at + 1).join('/') || '/'
    out.push({ label: part === '~' && at === 0 ? 'Home' : part, path: full })
  })
  return out
}

const seconds = (value: number) => value > 1e12 ? value / 1000 : value
export function formatSize(bytes: number | null | undefined) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024, unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++ }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}
/** "Today, 14:02", "Yesterday, 09:10", "3 Oct", "3 Oct 2024". */
export function formatModified(value: number | null | undefined, now = Date.now()) {
  if (value == null) return '—'
  const at = seconds(value) * 1000
  if (!Number.isFinite(at)) return 'Invalid Date'
  const time = fmtTime(at, { hour: '2-digit', minute: '2-digit' })
  if (sameDay(at, now)) return `Today, ${time}`
  const { year, month, date } = zoneParts(now)
  if (sameDay(at, zonedTime(year, month, date - 1, 0, 0))) return `Yesterday, ${time}`
  return fmtDate(at, { day: 'numeric', month: 'short', year: zoneParts(at).year === year ? undefined : 'numeric' })
}
export const fullDate = (value: number | null | undefined) => value == null ? '—' : fmtDateTime(seconds(value) * 1000, { dateStyle: 'medium', timeStyle: 'short' })

const KINDS: Record<string, string> = {
  md: 'Markdown', ts: 'TypeScript', tsx: 'TypeScript', js: 'JavaScript', jsx: 'JavaScript', mjs: 'JavaScript', cjs: 'JavaScript', json: 'JSON', py: 'Python',
  sh: 'Script', zsh: 'Script', bash: 'Script', css: 'CSS', html: 'HTML', txt: 'Text', log: 'Log', csv: 'CSV', yml: 'YAML', yaml: 'YAML', toml: 'TOML', lock: 'Lock file',
  png: 'PNG image', jpg: 'JPEG image', jpeg: 'JPEG image', gif: 'GIF image', webp: 'WebP image', svg: 'SVG image', pdf: 'PDF', zip: 'ZIP archive', gz: 'Archive', tgz: 'Archive', tar: 'Archive',
  mp4: 'Video', mov: 'Video', webm: 'Video', mp3: 'Audio', wav: 'Audio', m4a: 'Audio',
}
const BROAD: Record<string, string> = { image: 'Image', video: 'Video', audio: 'Audio', pdf: 'PDF', archive: 'Archive', code: 'Code', text: 'Text' }
/** A readable kind: the extension first, then the server's broad kind. */
export function kindLabel(entry: Pick<FileEntry, 'name' | 'dir' | 'kind'>) {
  if (entry.dir) return 'Folder'
  const ext = entry.name.includes('.') && !/^\.[^.]+$/.test(entry.name) ? entry.name.split('.').pop()!.toLowerCase() : ''
  return KINDS[ext] ?? BROAD[entry.kind] ?? (ext ? `${ext.toUpperCase()} file` : 'File')
}
export type Shape = 'dir' | 'image' | 'archive' | 'doc'
export function shapeOf(entry: Pick<FileEntry, 'name' | 'dir' | 'kind'>): Shape {
  if (entry.dir) return 'dir'
  const label = kindLabel(entry)
  if (entry.kind === 'image' || /image/i.test(label)) return 'image'
  if (entry.kind === 'archive' || /archive/i.test(label)) return 'archive'
  return 'doc'
}
export const gitText = (state: string | null | undefined) => ({
  M: 'Changed, not committed', modified: 'Changed, not committed', A: 'Added, not committed', added: 'Added, not committed',
  '?': 'Not tracked', '': 'No changes', untracked: 'Not tracked', D: 'Deleted', R: 'Renamed', clean: 'Clean', ignored: 'Ignored by git',
} as Record<string, string>)[state ?? ''] ?? state ?? 'No changes'
