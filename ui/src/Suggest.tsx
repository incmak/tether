import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { AtSign, Blocks, CircleHelp, File, Folder, Send, Slash, Sparkles } from 'lucide-react'
import { errorText } from './api'
import { agentName, bare, closest, named, needsMore, rank, unknownCommand } from './commandMatch'
import type { Api, Catalog, CatalogItem, Pane } from './api'

// One catalog per pane for a minute: skills and plugins rarely change while you type.
const catalogs = new Map<string, { at: number; data: Promise<Catalog> }>()
export function loadCatalog(api: Api, pane: string) {
  const cached = catalogs.get(pane)
  if (cached && Date.now() - cached.at < 60000) return cached.data
  const data = api<Catalog>(`catalog?session=${encodeURIComponent(pane)}`)
  data.catch(() => catalogs.delete(pane))
  catalogs.set(pane, { at: Date.now(), data })
  return data
}

// What the end of the draft asks for: "/com" at the start, "$sk" at the start for Codex, or "@src/ap" anywhere.
function trigger(draft: string, agent: Pane['agent']) {
  // A command or skill at the start, or after a space (to mention a skill mid-message).
  const command = /(^|\s)([/$])([^\s/]*)$/.exec(draft)
  if (command && (command[2] === '/' || agent === 'codex')) return { kind: 'command' as const, query: command[2] + command[3], start: command.index + command[1].length }
  const file = /(^|\s)@([^\s]*)$/.exec(draft)
  if (file) return { kind: 'file' as const, query: file[2], start: file.index + file[1].length }
  return null
}

// check(text) is true when the text starts with a command the agent does not have; the list then offers fixes.
// The agent would run its closest command for a partial name, or reply "Unknown command".
export interface SuggestHandle { key: (event: React.KeyboardEvent) => boolean; check: (text: string) => boolean }
interface Props { api: Api; pane: Pane; draft: string; setDraft: (text: string) => void; focus: () => void; run: (text: string) => void; sendAnyway: () => void }

export const Suggestions = forwardRef<SuggestHandle, Props>(function Suggestions({ api, pane, draft, setDraft, focus, run, sendAnyway }, ref) {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [files, setFiles] = useState<{ query: string; list: string[] } | null>(null)
  const [dismissed, setDismissed] = useState('')
  const [active, setActive] = useState(0)
  const [unknown, setUnknown] = useState('')
  const list = useRef<HTMLDivElement>(null)
  const found = pane.agent === 'shell' ? null : trigger(draft, pane.agent)
  const wantsCommands = found?.kind === 'command'
  useEffect(() => { setCatalog(null); setCatalogError('') }, [pane.id])
  // Load when the pane opens, and again on the next "/" if that failed.
  useEffect(() => {
    if (pane.agent === 'shell' || catalog) return
    let live = true
    loadCatalog(api, pane.id).then(data => { if (live) { setCatalog(data); setCatalogError('') } }, failure => { if (live) setCatalogError(errorText(failure)) })
    return () => { live = false }
  }, [api, pane.id, pane.agent, catalog, wantsCommands])
  const fileQuery = found?.kind === 'file' ? found.query : null
  useEffect(() => {
    if (fileQuery === null) return
    const abort = new AbortController()
    const timer = setTimeout(() => {
      api<{ files: string[] }>(`files?session=${encodeURIComponent(pane.id)}&q=${encodeURIComponent(fileQuery)}`, undefined, abort.signal)
        .then(result => setFiles({ query: fileQuery, list: result.files }), () => {})
    }, 150)
    return () => { clearTimeout(timer); abort.abort() }
  }, [api, pane.id, fileQuery])
  useEffect(() => setActive(0), [found?.query, found?.kind])
  useEffect(() => setUnknown(''), [pane.id])
  let items: { label: string; hint?: string; detail: string; icon: React.ReactNode; insert: string; item?: CatalogItem; action?: () => void }[] = []
  const typo = unknown && draft.trim() === unknown && catalog ? /^\/([^\s/]+)(.*)$/s.exec(unknown) : null
  if (typo && catalog) {
    items = closest(typo[1], catalog.commands)
      .map(item => ({ label: '/' + item.name, hint: item.hint, detail: item.description, icon: <Slash size={16} />, insert: '/' + item.name + (typo[2] || ' ') }))
    items.push({ label: 'Send as typed', detail: `${agentName[pane.agent] ?? 'The agent'} may run its closest command instead.`, icon: <Send size={16} />, insert: '', action: () => { setUnknown(''); sendAnyway() } })
  } else if (found?.kind === 'command' && catalog) {
    const query = found.query.slice(found.query[0] === '/' ? 1 : 0).toLowerCase()
    items = catalog.commands
      .filter(item => (found.query[0] === '$') === item.name.startsWith('$'))
      .map(item => ({ item, at: rank(item, query) }))
      .filter(entry => entry.at >= 0)
      .sort((a, b) => a.at - b.at || (a.at >= 3 ? a.item.name.length - b.item.name.length : 0))
      .slice(0, 40)
      .map(({ item, at }) => ({ item, hint: item.hint, label: item.name.startsWith('$') ? item.name : '/' + item.name,
        detail: at === 2 ? `Also /${item.aliases!.find(alias => bare(alias).startsWith(bare(query)))}. ${item.description}` : item.description,
        icon: item.kind === 'skill' ? <Sparkles size={16} /> : item.name.includes(':') ? <Blocks size={16} /> : <Slash size={16} />,
        insert: (item.name.startsWith('$') ? item.name : '/' + item.name) + ' ' }))
  } else if (found?.kind === 'file' && files) {
    items = files.list.map(path => ({ label: path, detail: '', icon: path.endsWith('/') ? <Folder size={16} /> : <File size={16} />, insert: '@' + path + (path.endsWith('/') ? '' : ' ') }))
  }
  const key = typo ? `typo:${draft}` : found ? `${found.kind}:${draft}` : ''
  const status = !typo && found?.kind === 'command' && !catalog ? (catalogError ? `Could not load commands: ${catalogError}` : 'Loading commands…') : ''
  const open = (!!typo || !!found) && (items.length > 0 || !!status) && dismissed !== key
  // Enter on the exact name runs it, as in the agent's own menu, unless it needs an argument. Tab only fills the box.
  const pick = (index: number, enter = false) => {
    const choice = items[index]
    if (!choice) return
    if (choice.action) { choice.action(); return }
    if (typo) { setUnknown(''); setDraft(choice.insert); focus(); return }
    if (!found) return
    const typed = found.query.slice(1).toLowerCase()
    if (enter && found.start === 0 && found.query[0] === '/' && choice.item && named(choice.item, typed) && !needsMore(choice.item)) { run('/' + choice.item.name); return }
    setDraft(draft.slice(0, found.start) + choice.insert)
    focus()
  }
  useImperativeHandle(ref, () => ({
    key(event) {
      if (!open || !items.length) return false
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { setActive(current => (current + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length); return true }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) { pick(active, event.key === 'Enter'); return true }
      if (event.key === 'Escape') { setDismissed(key); return true }
      return false
    },
    check(text) {
      if (!unknownCommand(text, pane.agent, catalog?.commands ?? [])) return false
      setUnknown(text.trim()); setActive(0)
      return true
    },
  }))
  // Arrow keys move the highlight; keep it in view, below the sticky heading.
  useEffect(() => { list.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [active, open])
  if (!open) return null
  const kind = typo ? 'typo' : found?.kind
  return <div ref={list} className="suggest" role="listbox" aria-label={kind === 'file' ? 'Files' : kind === 'typo' ? 'Unknown command' : 'Commands and skills'}>
    <div className={kind === 'typo' ? 'suggest-head warn' : 'suggest-head'}>{kind === 'file' ? <><AtSign size={13} />Files in {pane.dir}</> : kind === 'typo' ? <><CircleHelp size={13} />/{typo![1]} is not a {agentName[pane.agent]} command. Did you mean:</> : <><Slash size={13} />Commands and skills</>}</div>
    {status && <p className="suggest-status">{status}</p>}
    {items.map((item, index) => <button key={item.label} type="button" role="option" aria-selected={index === active} className={index === active ? 'active' : ''}
      onPointerDown={event => event.preventDefault()} onClick={() => pick(index)}>
      {item.icon}<span><strong>{item.label}{item.hint && <code className="suggest-hint">{item.hint}</code>}</strong>{item.detail && <small>{item.detail}</small>}</span>
    </button>)}
  </div>
})
