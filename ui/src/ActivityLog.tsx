import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Calendar, FolderOpen, Monitor, RotateCw, Search, Terminal, Undo2, X } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import { SelectMenu } from './SelectMenu'
import { SkeletonRows } from './Skeleton'
import './ActivityLog.css'

// Settings › Activity (server: audit.py, GET /mobile/api/audit). What each device and agent changed: settings with the old
// and the new value, tabs, reviews, backlog, lessons, advisor asks and approvals. Newest first; the server keeps 90 days.
export type AuditSource = 'ui' | 'agent-cli' | 'cli' | 'hook' | 'server' | 'unknown'
type Scalar = string | number | boolean | null
export interface AuditEntry {
  id: string; at: number; kind: string; summary: string
  actor: { source: AuditSource; device?: string; tab?: string; pane?: string; agent?: string }
  project?: string; old?: Scalar; new?: Scalar; undo?: boolean; detail?: Record<string, Scalar>
}
interface Facets { kinds: { id: string; group: string; count: number }[]; projects: string[]; actors: string[] }
interface AuditPage { entries: AuditEntry[]; next: string | null; facets?: Facets; retention_days?: number }
/** One answer to the first-page request: the page, or why it failed (`failed`). */
interface Answer { path: string; page?: AuditPage; failed: string }
/** A Load more request: the query and the cursor it continues, and why it failed ('' while it runs). */
interface More { path: string; cursor: string; failed: string }

const AUDIT_GROUPS: [string, string][] = [['settings', 'Settings'], ['tabs', 'Tabs'], ['reviews', 'Reviews'], ['backlog', 'Backlog'], ['lessons', 'Lessons'], ['advisor', 'Advisor'], ['approvals', 'Approvals']]
const GROUP_OF: Record<string, string> = { setting: 'settings', passkey: 'settings', tab: 'tabs', review: 'reviews', backlog: 'backlog', lesson: 'lessons', advisor: 'advisor', approval: 'approvals' }
const groupLabel = (kind: string) => AUDIT_GROUPS.find(([id]) => id === GROUP_OF[kind.split('.')[0]])?.[1] ?? 'Other'
const ANY = ''
const PAGE = 50
const AGENT_LABELS = new Map([['claude', 'Claude tab'], ['codex', 'Codex tab'], ['antigravity', 'Antigravity tab']])

/** Who did it, in words: the tab for an agent, the device for the app. Never only a colour or an icon. */
function actorText(actor: AuditEntry['actor']) {
  const { source, device, tab, pane, agent } = actor
  if (source === 'agent-cli' || source === 'hook' || pane) {
    const label = tab || device || (agent && AGENT_LABELS.get(agent)) || 'An agent tab'
    return `${label}${source === 'hook' ? ' · hook' : ''}`
  }
  if (source === 'ui') return device || 'Tether app'
  if (source === 'cli') return 'Command line'
  if (source === 'server') return tab || 'Tether'  // a review verdict names the reviewer tab
  return `Unknown client${device ? ` · ${device}` : ''}`
}
const ACTOR_ICON = { ui: Monitor, 'agent-cli': Bot, hook: Bot, cli: Terminal, server: Monitor, unknown: Monitor }

/** A date input value (local day) → epoch seconds at the start of that day; `end` gives the start of the next day. */
function dayStart(value: string, end = false) {
  const [year, month, day] = value.split('-').map(Number)
  if (!year || !month || !day) return undefined
  return Math.floor(new Date(year, month - 1, day + (end ? 1 : 0)).getTime() / 1000)
}
const dayKey = (seconds: number) => new Date(seconds * 1000).toDateString()
function dayHeading(seconds: number) {
  const date = new Date(seconds * 1000)
  const today = new Date(); const yesterday = new Date(); yesterday.setDate(today.getDate() - 1)
  if (date.toDateString() === today.toDateString()) return 'Today'
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric' })
}

interface Filters { text: string; group: string; project: string; actor: string; from: string; to: string }
const NONE: Filters = { text: '', group: ANY, project: ANY, actor: ANY, from: '', to: '' }
function queryOf(filters: Filters, before?: string) {
  const params = new URLSearchParams({ limit: String(PAGE) })
  if (filters.text.trim()) params.set('q', filters.text.trim())
  if (filters.group) params.set('kind', filters.group)
  if (filters.project) params.set('project', filters.project)
  if (filters.actor) params.set('actor', filters.actor)
  const since = filters.from ? dayStart(filters.from) : undefined
  const until = filters.to ? dayStart(filters.to, true) : undefined
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  if (before) params.set('before', before)
  else params.set('facets', '1')
  return `audit?${params}`
}

function Row({ entry }: { entry: AuditEntry }) {
  const date = new Date(entry.at * 1000)
  const Icon = ACTOR_ICON[entry.actor.source] ?? Monitor
  // Settings rows show old → new. Skip the line when the summary already says it ("Codex level 3 → 1").
  const old = String(entry.old ?? '—'), now = String(entry.new ?? '—')
  const change = (entry.old !== undefined || entry.new !== undefined) && !entry.summary.includes(`${old} → ${now}`)
  return <li className="audit-row" data-kind={entry.kind}>
    <time className="audit-time" dateTime={date.toISOString()} title={date.toLocaleString()}>{date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>
    <span className="audit-text">
      <strong className="audit-summary">{entry.undo && <span className="audit-undo"><Undo2 size={12} aria-hidden="true" />Undo</span>}{entry.summary}</strong>
      {change && <span className="audit-change"><span className="visually-hidden">Changed from </span><span className="audit-old">{old}</span><span aria-hidden="true"> → </span><span className="visually-hidden"> to </span><b>{now}</b></span>}
      <small className="audit-meta">
        <span className="audit-actor"><Icon size={13} aria-hidden="true" />{actorText(entry.actor)}</span>
        <span>{groupLabel(entry.kind)}</span>
        {entry.project && <span className="audit-project"><FolderOpen size={12} aria-hidden="true" />{entry.project}</span>}
      </small>
    </span>
  </li>
}

/** Newest first, each id once. Ids start with the time in ms, so they sort by time. */
function merged(...lists: AuditEntry[][]) {
  const seen = new Set<string>()
  return lists.flat().filter(entry => !seen.has(entry.id) && !!seen.add(entry.id)).sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0))
}

/** Settings › Activity. The first page reloads every 20 s while it is open. After Load more, the sheet keeps one list:
 * each reload and each older page merge into it, so no row that was on screen goes away. */
export function ActivityLog({ api }: { api: Api }) {
  const [filters, setFilters] = useState<Filters>(NONE)
  // The list after Load more, for one query. A filter change drops it, so the list starts again at the newest entry.
  const [loaded, setLoaded] = useState<{ path: string; entries: AuditEntry[]; next: string | null } | null>(null)
  const [text, setText] = useState('')
  // The Load more request that runs or failed. It belongs to one list (query) and one cursor.
  const [more, setMore] = useState<More | null>(null)
  // The one Load more request whose answer may still change the list. Two requests can ask for the same query and
  // cursor (a filter change and back), so a reply checks that it is still this request, not only the query.
  const request = useRef<More | null>(null)
  const drop = () => { request.current = null; setLoaded(null); setMore(null) }
  // Typing waits 250 ms before it asks the server, so each key does not send a request.
  useEffect(() => { const timer = setTimeout(() => { if (text !== filters.text) { request.current = null; setLoaded(null); setMore(null); setFilters(current => ({ ...current, text })) } }, 250); return () => clearTimeout(timer) }, [text, filters.text])
  const path = queryOf(filters)
  // Each answer carries the query that asked for it, and a failure is an answer too: a page or an error for the last
  // filter must not show under this one.
  const fetcher = useCallback((signal: AbortSignal) => api<AuditPage>(path, undefined, signal).then(
    (page): Answer => ({ path, page, failed: '' }),
    (failure: unknown): Answer => { if (signal.aborted) throw failure; return { path, failed: errorText(failure) || 'The server did not answer.' } }), [api, path])
  const { data, refresh } = usePolling(fetcher, 20000)
  // The last page that loaded. A failed reload keeps it on screen, under the error.
  const [kept, setKept] = useState<{ path: string; page: AuditPage } | null>(null)
  if (data?.page && data.page !== kept?.page) setKept({ path: data.path, page: data.page })
  const live = kept?.path === path ? kept.page : undefined
  const error = data?.path === path ? data.failed : ''
  // The filter lists come from the first page; keep the last ones while a new filter loads.
  const [facets, setFacets] = useState<Facets | null>(null)
  if (live?.facets && live.facets !== facets) setFacets(live.facets)
  const list = loaded?.path === path ? loaded : null
  // A reload after Load more: merge it in. If more than a page came in since the last reload, the new page does not
  // reach the list (a gap), so start again at the first page and offer Load more again.
  const [merges, setMerges] = useState<AuditPage | undefined>(undefined)
  if (live !== merges) {
    setMerges(live)
    if (live && list) {
      const known = new Set(list.entries.map(entry => entry.id))
      setLoaded(live.next && !live.entries.some(entry => known.has(entry.id)) ? null : { ...list, entries: merged(live.entries, list.entries) })
    }
  }
  const entries = useMemo(() => list ? list.entries : live?.entries ?? [], [list, live])
  const next = list ? list.next : live?.next ?? null
  // Show only the request for the list and the cursor on screen: after a filter change or a reset it is old.
  const asking = more && more.path === path && more.cursor === next ? more : null
  async function loadMore() {
    if (!next || !live || (asking && !asking.failed)) return
    const asked: More = { path, cursor: next, failed: '' }
    // From now on, reloads merge into this list, so its cursor stays the one this request continues.
    if (!list) setLoaded({ path, entries: live.entries, next })
    request.current = asked
    setMore(asked)
    try {
      const page = await api<AuditPage>(queryOf(filters, next))
      // Add the page only to the list it continues. A filter change or a reset dropped that list: then drop the page.
      if (request.current !== asked) return
      request.current = null
      setLoaded(before => before && before.path === asked.path && before.next === asked.cursor
        ? { path: asked.path, entries: merged(before.entries, page.entries), next: page.next } : before)
      setMore(null)
    } catch (failure) {
      if (request.current !== asked) return
      request.current = null
      const failed = errorText(failure) || 'The server did not answer.'
      setMore({ ...asked, failed })
    }
  }
  const filtered = filters.text.trim() !== '' || filters.group !== ANY || filters.project !== ANY || filters.actor !== ANY || !!filters.from || !!filters.to
  const set = (change: Partial<Filters>) => { drop(); setFilters(current => ({ ...current, ...change })) }
  const clear = () => { drop(); setText(''); setFilters(NONE) }
  const projects = facets?.projects ?? []
  const actors = facets?.actors ?? []
  const days = live?.retention_days ?? kept?.page.retention_days ?? 90
  const today = new Date(); const max = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`

  let body
  if (!live && error) body = <div className="audit-state" role="alert"><p className="error">Could not load the activity log. {error}</p><button type="button" className="secondary" onClick={() => void refresh()}><RotateCw size={15} aria-hidden="true" />Retry</button></div>
  else if (!live) body = <SkeletonRows count={5} label="Loading the activity log" className="audit-skeleton" icon={false} />
  else if (!entries.length && !filtered) body = <div className="audit-state audit-empty">
    <h4>Nothing recorded yet</h4>
    <p>Tether records who changes what: autonomy and Codex levels, reviewers, the advisor, learning and passkeys (with the old and the new value), tabs that start or close, reviews and their verdicts, backlog items, lessons, advisor questions and approvals. It never records messages, typed text or secrets.</p>
  </div>
  else if (!entries.length) body = <div className="audit-state audit-empty" role="status">
    <h4>No matching activity</h4>
    <p>Nothing in the last {days} days matches these filters.</p>
    <button type="button" className="secondary" onClick={clear}>Clear filters</button>
  </div>
  else body = <>
    {error && <p className="error audit-stale" role="alert">Could not refresh. {error} <button type="button" className="link-button" onClick={() => void refresh()}>Retry</button></p>}
    <ol className="audit-days">{entries.reduce<AuditEntry[][]>((groups, entry) => {
      const last = groups[groups.length - 1]
      if (last && dayKey(last[0].at) === dayKey(entry.at)) last.push(entry); else groups.push([entry])
      return groups
    }, []).map(group => <li key={group[0].id} className="audit-day">
      <h4>{dayHeading(group[0].at)}</h4>
      <ul className="audit-list">{group.map(entry => <Row key={entry.id} entry={entry} />)}</ul>
    </li>)}</ol>
    {asking?.failed && <p className="error" role="alert">Could not load more. {asking.failed}</p>}
    {next ? <button type="button" className="secondary audit-more" disabled={!!asking && !asking.failed} onClick={() => void loadMore()}>{asking ? asking.failed ? 'Try again' : 'Loading…' : 'Load more'}</button>
      : <p className="setting-note audit-end">{filtered ? 'End of the matching activity.' : `That is all. Tether keeps ${days} days.`}</p>}
  </>

  return <section className="settings-group activity-log" aria-labelledby="settings-activity-log"><h3 id="settings-activity-log">Activity log</h3>
    <p className="setting-note">What each device and agent changed, newest first. Tether keeps {days} days on this server. Agents read it with <code>agent_audit_cli.py list</code>.</p>
    <div className="audit-tools">
      <label className="audit-search"><Search size={15} aria-hidden="true" /><span className="visually-hidden">Search the activity log</span>
        <input type="search" placeholder="Search activity…" value={text} autoComplete="off" onChange={event => setText(event.target.value)} />
      </label>
      <div className="audit-filters">
        <SelectMenu label="Type" value={filters.group} className="sm-field audit-filter" minWidth={180} triggerLabel={filters.group ? undefined : 'All types'}
          onChange={group => set({ group })} options={[{ value: ANY, label: 'All types' }, ...AUDIT_GROUPS.map(([value, label]) => ({ value, label }))]} />
        <SelectMenu label="Project" value={filters.project} icon={<FolderOpen size={14} aria-hidden="true" />} className="sm-field audit-filter" minWidth={200} searchLabel="Find a project" triggerLabel={filters.project || 'All projects'}
          onChange={project => set({ project })} options={[{ value: ANY, label: 'All projects' }, ...(filters.project && !projects.includes(filters.project) ? [filters.project] : []).concat(projects).map(value => ({ value, label: value }))]} />
        <SelectMenu label="Agent or device" value={filters.actor} icon={<Bot size={14} aria-hidden="true" />} className="sm-field audit-filter" minWidth={220} searchLabel="Find a tab or device" triggerLabel={filters.actor || 'Anyone'}
          onChange={actor => set({ actor })} options={[{ value: ANY, label: 'Anyone' }, ...(filters.actor && !actors.includes(filters.actor) ? [filters.actor] : []).concat(actors).map(value => ({ value, label: value }))]} />
      </div>
      <fieldset className="audit-dates">
        <legend className="visually-hidden">Date range</legend>
        <Calendar size={15} aria-hidden="true" />
        <label><span>From</span><input type="date" value={filters.from} max={filters.to || max} onChange={event => set({ from: event.target.value })} /></label>
        <label><span>To</span><input type="date" value={filters.to} min={filters.from || undefined} max={max} onChange={event => set({ to: event.target.value })} /></label>
      </fieldset>
      {(filtered || text) && <button type="button" className="link-button audit-clear" onClick={clear}><X size={14} aria-hidden="true" />Clear filters</button>}
    </div>
    <p className="visually-hidden" role="status">{live ? `${entries.length}${next ? '+' : ''} ${entries.length === 1 ? 'entry' : 'entries'}` : ''}</p>
    {body}
  </section>
}
