import { createContext, useCallback, useContext, useSyncExternalStore } from 'react'
import { fileChecks } from './fileChecks'
import { useIsMarkdownCodeBlock } from '@assistant-ui/react-markdown'
import type { Pane } from './api'
import { tabTarget } from './tabLinks'

// Paths an agent names in a reply (`shots/row-menu.png`, [clip](out/demo.mp4), src/App.tsx:42) open on the phone through
// GET /mobile/api/file. the server decides what may open: home folder only, no secrets. Media, text and Office files open in Tether;
// any other file ('file': an archive or an unsupported document) downloads.
export type Kind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'office' | 'file'
const KINDS: Record<string, Kind> = {
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image',
  mp4: 'video', webm: 'video', mov: 'video', m4v: 'video', mp3: 'audio', m4a: 'audio', wav: 'audio', ogg: 'audio', pdf: 'pdf',
  xlsx: 'office', xlsm: 'office', xls: 'office', ods: 'office', docx: 'office', pptx: 'office' }
// Keep in step with TEXT_SUFFIXES and TEXT_NAMES in media_files.py.
for (const suffix of ('txt md markdown log json jsonl csv tsv yml yaml toml ini cfg conf py js jsx mjs cjs ts tsx css scss html htm xml svg sh zsh bash '
  + 'sql go rs java kt swift c h cc cpp hpp rb php lua diff patch tf graphql proto vue svelte ipynb lock gitignore dockerignore editorconfig').split(' ')) KINDS[suffix] = 'text'
const TEXT_NAMES = new Set(['Makefile', 'Dockerfile', 'README', 'LICENSE', 'Procfile', 'Gemfile', 'Justfile', 'CODEOWNERS'])
// A bare document or other file name needs one of these extensions, so "Node.js" or "e.g." is not a file; a path with a folder may have any.
const DOCUMENTS = new Set(('xlsx xls xlsm ods docx doc odt rtf pptx ppt odp key numbers pages epub zip tar gz tgz bz2 xz 7z rar '
  + 'dmg pkg apk ipa heic avif tiff psd sketch fig').split(' '))

// "src/a.png:12" and "file:///x.png" name the same file; the line number is kept for text files. URLs to other sites are not paths.
export function mediaPath(text: string) {
  const raw = text.trim().replace(/^file:\/\//, '')
  const line = Number(/:(\d+)(?::\d+)?$/.exec(raw)?.[1]) || undefined
  const path = raw.replace(/:\d+(?::\d+)?$/, '')
  if (!path || /\s/.test(path) && !path.includes('/') || /^[a-z][\w+.-]*:\/\//i.test(path) || path.length > 1024) return null
  const name = path.split('/').pop() ?? ''
  const suffix = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
  const other = /^[a-z0-9]{1,10}$/.test(suffix) && /[a-z]/.test(suffix) && (DOCUMENTS.has(suffix) || path.includes('/'))
  const kind: Kind | undefined = TEXT_NAMES.has(name) ? 'text' : KINDS[suffix] ?? (other ? 'file' : undefined)
  return kind ? { path, kind, line } : null
}

// A path in plain reply text. It needs a known extension; a bare word like "Node.js" (no folder, no lowercase file name) is a name, not a file.
const TOKEN = /(^|[\s([{"'<])((?:~|\.{1,2})?\/?[\w@+-][\w.@+-]*(?:\/[\w.@+-]+)*(?::\d+(?::\d+)?)?)(?=$|[\s)\]}"'>,;!?]|[.:](?:\s|$))/g
function plainPaths(text: string) {
  const found: { start: number; end: number; raw: string }[] = []
  for (const match of text.matchAll(TOKEN)) {
    const raw = match[2].replace(/\.+$/, '')
    if (!raw.includes('/') && /^([A-Z]\w*|d3|three|chart|express)\.js$/.test(raw)) continue
    if (!mediaPath(raw)) continue
    const start = match.index! + match[1].length
    found.push({ start, end: start + raw.length, raw })
  }
  return found
}

type Node = { type: string; value?: string; url?: string; children?: Node[]; data?: { hProperties?: Record<string, string> } }
// Remark plugin: turn file paths in plain text into links that Link (below) opens in Tether. Code, links and their text stay as they are.
export function remarkPaths() {
  const walk = (node: Node) => {
    if (!node.children || node.type === 'link' || node.type === 'linkReference') return
    node.children = node.children.flatMap(child => {
      if (child.type !== 'text' || !child.value) { walk(child); return [child] }
      const found = plainPaths(child.value)
      if (!found.length) return [child]
      const parts: Node[] = []
      let at = 0
      for (const item of found) {
        if (item.start > at) parts.push({ type: 'text', value: child.value.slice(at, item.start) })
        parts.push({ type: 'link', url: '#', data: { hProperties: { 'data-path': item.raw } }, children: [{ type: 'text', value: item.raw }] })
        at = item.end
      }
      if (at < child.value.length) parts.push({ type: 'text', value: child.value.slice(at) })
      return parts
    })
  }
  return (tree: Node) => walk(tree)
}

export const MediaOpener = createContext<((path: string, kind: Kind, line?: number) => void) | null>(null)

export const FilePane = createContext<string | null>(null)

/** The live tabs and how to open one, so a tab name in a reply opens that tab (tabLinks.ts). */
export const TabOpener = createContext<{ panes: Pane[]; current: Pane; open: (pane: Pane) => void } | null>(null)

function useTab(text: string) {
  const tabs = useContext(TabOpener)
  const pane = tabs && text ? tabTarget(text, tabs.panes, tabs.current) : null
  return pane && tabs ? { pane, open: () => tabs.open(pane) } : null
}

function useFileState(path: string | undefined) {
  const pane = useContext(FilePane)
  const subscribe = useCallback((listener: () => void) => pane && path
    ? fileChecks.subscribe(pane, path, listener) : () => {}, [pane, path])
  const snapshot = useCallback(() => pane && path ? fileChecks.stateOf(pane, path) : 'closed', [pane, path])
  return useSyncExternalStore(subscribe, snapshot, snapshot)
}

function useOpen(text: string) {
  const open = useContext(MediaOpener)
  const media = mediaPath(text)
  const state = useFileState(media?.path)
  return { media, open: open && media && (state === 'open' || state === 'unknown')
    ? () => open(media.path, media.kind, media.line) : null }
}

export function InlineCode(props: React.ComponentProps<'code'>) {
  const block = useIsMarkdownCodeBlock()
  const text = typeof props.children === 'string' ? props.children : ''
  const { media, open } = useOpen(block ? '' : text)
  const tab = useTab(block || media ? '' : text)
  if (tab) return <FileLink open={tab.open} label={`Open the tab ${text}`}><code {...props} /></FileLink>
  if (!open) return <code {...props} className={media ? [props.className, 'file-chip'].filter(Boolean).join(' ') : props.className} />
  return <FileLink open={open} label={`${media?.kind === 'file' ? 'Download' : 'Open'} ${text}`}><code {...props} /></FileLink>
}

export function Link({ href = '', children, ...props }: React.ComponentProps<'a'>) {
  const found = (props as Record<string, unknown>)['data-path'] as string | undefined
  const named = (props as Record<string, unknown>)['data-tab'] as string | undefined
  const tab = useTab(named ?? '')
  const { media, open } = useOpen(named !== undefined ? '' : found ?? (/^(https?|mailto):/i.test(href) ? '' : safeDecode(href)))
  if (named !== undefined) return tab ? <FileLink open={tab.open} label={`Open the tab ${named}`}>{children}</FileLink> : <>{children}</>
  if (open) return <FileLink open={open}>{children}</FileLink>
  if (found || media) return <span className="file-chip">{children}</span>
  return <a {...props} href={href} target="_blank" rel="noreferrer">{children}</a>
}

// A link, not a button: it flows and wraps with the text around it. Enter and Space both open it.
function FileLink({ open, label, children }: { open: () => void; label?: string; children: React.ReactNode }) {
  return <a href="#" role="button" className="file-link" aria-label={label}
    onClick={event => { event.preventDefault(); open() }}
    onKeyDown={event => { if (event.key === ' ') { event.preventDefault(); open() } }}>{children}</a>
}

/** Save a file the server sends as an attachment. */
export function saveFile(url: string, name: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function safeDecode(text: string) { try { return decodeURI(text) } catch { return text } }
