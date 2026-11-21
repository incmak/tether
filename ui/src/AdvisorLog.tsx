import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, Check, Clock, MessageCircleQuestion, PowerOff, RotateCw, X } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import { SkeletonRows } from './Skeleton'
import { BoneLines } from './SettingBones'
import { fmtDateTime, useTetherZone } from './zone'

// The advisor log of a project (server: advisor_log.py). Agents at autonomy level 3 and above ask the advisor when the
// evidence does not settle a decision; each ask is one entry. The list shows a cut question and answer; an entry
// opens in full. Entries from before Tether kept the full text exist only as cut lines (full: false).
export type AdvisorStatus = 'decided' | 'ask_user' | 'timeout' | 'error' | 'off'
export interface AdvisorRow { id: string; time: number; status: AdvisorStatus; question: string; answer: string; agent: string; model: string; full: boolean }
interface AdvisorReply { role: string; agent: string; model: string; answer: string; confidence: string }
interface AdvisorEntry { id: string; time: number; status: AdvisorStatus; question: string; answer: string; agent: string; model: string
  confidence: string; evidence: string; error: string; advisors: AdvisorReply[]; cut: string[]; limit: number }
interface AdvisorLogData { entries: AdvisorRow[]; keep: number }

type Filter = 'all' | 'ask_user' | 'decided' | 'errors'
const FILTERS: [Filter, string][] = [['all', 'All'], ['ask_user', 'Asked you'], ['decided', 'Decided'], ['errors', 'Errors']]
const STATUS: Record<AdvisorStatus, string> = { decided: 'Decided', ask_user: 'Asked you', timeout: 'Timed out', error: 'Error', off: 'Advisor off' }
const ICON = { decided: Check, ask_user: MessageCircleQuestion, timeout: Clock, error: X, off: PowerOff }
const matches = (filter: Filter, status: AdvisorStatus) =>
  filter === 'all' || (filter === 'errors' ? status === 'error' || status === 'timeout' : status === filter)
const EMPTY: Record<Filter, string> = { all: '', ask_user: 'No asks went to you.', decided: 'The advisor decided nothing yet.', errors: 'No errors or timeouts.' }

function when(seconds: number) {
  const date = new Date(seconds * 1000)
  return <time dateTime={date.toISOString()}>{fmtDateTime(date, { dateStyle: 'medium', timeStyle: 'short' })}</time>
}

// A status is never color alone: an icon in a tinted disc, and the word beside it (like the agent map).
function StatusIcon({ status }: { status: AdvisorStatus }) {
  const Icon = ICON[status]
  return <span className={`advisor-status ${status}`} aria-hidden="true"><Icon size={13} strokeWidth={2.5} /></span>
}
const StatusWord = ({ status }: { status: AdvisorStatus }) => <b className={`advisor-word ${status}`}>{STATUS[status]}</b>

/** The list, read every 15 s while the Project sheet is open. The sheet keeps it, so the filter survives an open entry. */
export function useAdvisorLog(api: Api, project: string | undefined) {
  const fetcher = useCallback((signal: AbortSignal) => project
    ? api<AdvisorLogData>(`projects/advisor-log?project=${encodeURIComponent(project)}`, undefined, signal)
    : new Promise<AdvisorLogData>(() => {}), [api, project])  // no project yet: stay in the loading state
  const polled = usePolling(fetcher, 15000)
  const [filter, setFilter] = useState<Filter>('all')
  return { ...polled, filter, setFilter }
}

export function AdvisorLog({ log, disabled, focus, onOpen }: { log: ReturnType<typeof useAdvisorLog>; disabled?: boolean; focus?: string; onOpen: (row: AdvisorRow) => void }) {
  useTetherZone()
  const { data, error, refresh, filter, setFilter } = log
  const list = useRef<HTMLUListElement>(null)
  // Back from an entry: focus its row again, so a keyboard user keeps their place.
  useEffect(() => {
    if (focus === undefined) return
    list.current?.querySelector<HTMLButtonElement>(`button[data-key="${CSS.escape(focus)}"]`)?.focus()
  }, [focus])
  // An older line has no id: its place in the whole list names it, and stays the same under every filter.
  const rows = data?.entries.map((row, index) => ({ ...row, id: row.id || `line-${index}-${row.time}` })).filter(row => matches(filter, row.status)) ?? []
  const count = (key: Filter) => data?.entries.filter(row => matches(key, row.status)).length ?? 0
  return <section className="settings-group advisor-log" aria-labelledby="project-advisor"><h3 id="project-advisor">Advisor decisions</h3>
    <p className="setting-note">Agents at autonomy level 3 and above ask the advisor when the evidence does not settle a decision. Tether keeps the last {data?.keep ?? 200} asks.</p>
    {data && error && <p className="error" role="alert">{error} <button type="button" className="link-button" onClick={() => void refresh()}>Retry</button></p>}
    {!data ? error
      ? <p className="error" role="alert">Could not load the advisor log. {error} <button type="button" className="link-button" onClick={() => void refresh()}><RotateCw size={14} aria-hidden="true" />Retry</button></p>
      : <SkeletonRows count={3} label="Loading advisor decisions" className="advisor-skeleton" icon={22} />
    : !data.entries.length ? <p className="hint">No advisor decisions yet. When an agent here asks the advisor, the question and the answer show here.</p>
    : <>
      <fieldset className="choice advisor-filter" disabled={disabled}>
        <legend className="visually-hidden">Show</legend>
        <div className="segmented">{FILTERS.map(([key, label]) => <label key={key}>
          <input type="radio" name="advisor-filter" value={key} aria-label={`${label}, ${count(key)}`} checked={filter === key} onChange={() => setFilter(key)} />
          <span>{label}</span>
        </label>)}</div>
      </fieldset>
      <p className="visually-hidden" role="status">{rows.length === 1 ? '1 entry' : `${rows.length} entries`}</p>
      {rows.length ? <ul className="project-list advisor-list" ref={list}>{rows.map(row => <li key={row.id}>
        <button type="button" className="advisor-row" data-key={row.id} data-status={row.status} disabled={disabled} onClick={() => onOpen(row)}>
          <StatusIcon status={row.status} />
          <span className="advisor-row-text">
            <small><StatusWord status={row.status} /> · {when(row.time)}</small>
            <strong className="advisor-question">{row.question}</strong>
            {row.answer ? <span className="advisor-answer">{row.answer}</span> : <span className="advisor-answer empty">No answer</span>}
          </span>
        </button></li>)}</ul> : <p className="hint">{EMPTY[filter]}</p>}
    </>}
  </section>
}

// Storage policy: the server keeps each text up to advisor_log.FULL characters, cuts longer text and names it in cut.
const CUT_NAMES: Record<string, string> = { question: 'the question', answer: 'the answer', evidence: 'the evidence', error: 'the error', advisors: 'the advisor answers' }
function cutNote(entry: AdvisorEntry) {
  const names = entry.cut.map(field => CUT_NAMES[field])
  if (!names.length) return ''
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  return Number.isFinite(entry.limit) && entry.limit > 0
    ? `Tether kept the first ${entry.limit.toLocaleString('en-US')} characters of ${list}.` : `Tether cut ${list}.`
}

const text = (value: unknown) => typeof value === 'string' ? value : ''
/** The entry in the shape the detail reads. A null, missing or wrong-type field (an older or damaged answer) becomes
 *  empty or falls back to the list row, so it never crashes the sheet. */
function normal(found: Partial<Record<keyof AdvisorEntry, unknown>> | null, row: AdvisorRow): AdvisorEntry {
  const raw = found && typeof found === 'object' ? found : {}
  const advisors = Array.isArray(raw.advisors) ? raw.advisors : []
  return { ...row, id: row.id, time: typeof raw.time === 'number' ? raw.time : row.time,
    status: typeof raw.status === 'string' && raw.status in STATUS ? raw.status as AdvisorStatus : row.status,
    question: text(raw.question) || row.question, answer: text(raw.answer), agent: text(raw.agent) || row.agent, model: text(raw.model) || row.model,
    confidence: text(raw.confidence), evidence: text(raw.evidence), error: text(raw.error),
    advisors: advisors.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
      .map(item => ({ role: text(item.role), agent: text(item.agent), model: text(item.model), answer: text(item.answer), confidence: text(item.confidence) })),
    cut: Array.isArray(raw.cut) ? raw.cut.filter((name): name is string => typeof name === 'string' && name in CUT_NAMES) : [],
    limit: typeof raw.limit === 'number' ? raw.limit : NaN }
}

function Text({ label, children }: { label: string; children: string }) {
  return <section className="advisor-part"><h4>{label}</h4><p className="advisor-text">{children}</p></section>
}

/** One ask in full. An older line has no full text: it shows the cut text from the list and says so. */
export function AdvisorDecision({ api, project, row, onBack }: { api: Api; project: string; row: AdvisorRow; onBack: () => void }) {
  useTetherZone()
  const [entry, setEntry] = useState<AdvisorEntry | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!row.full) return
    const controller = new AbortController()
    api<Partial<Record<keyof AdvisorEntry, unknown>> | null>(`projects/advisor-entry?project=${encodeURIComponent(project)}&id=${encodeURIComponent(row.id)}`, undefined, controller.signal)
      .then(found => { setEntry(normal(found, row)); setError('') }, failure => { if (!controller.signal.aborted) setError(errorText(failure)) })
    return () => controller.abort()
  }, [api, project, row, attempt])
  const back = useRef<HTMLButtonElement>(null)
  useEffect(() => { back.current?.focus() }, [])
  const shown = entry ?? row
  const replies = entry && entry.advisors.length ? entry.advisors : null
  const who = (agent: string, model: string) => [agent, model].filter(Boolean).join(' ')
  return <section className="settings-group advisor-detail" aria-labelledby="advisor-detail-title">
    <button type="button" ref={back} className="link-button project-back" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All decisions</button>
    <h3 id="advisor-detail-title" className="visually-hidden">Advisor decision</h3>
    <p className="advisor-facts"><StatusIcon status={shown.status} /><span><StatusWord status={shown.status} /> · {when(shown.time)}{entry?.confidence && entry.status !== 'off' ? ` · ${entry.confidence} confidence` : ''}</span></p>
    {error ? <p className="error" role="alert">{error} <button type="button" className="link-button" onClick={() => { setError(''); setAttempt(attempt + 1) }}><RotateCw size={14} aria-hidden="true" />Retry</button></p>
    : row.full && !entry ? <div className="skeleton" aria-busy="true"><span className="sk-label" role="status">Loading the decision</span><p className="advisor-text" aria-hidden="true"><BoneLines count={4} /></p></div>
    : <>
      {entry && cutNote(entry) && <p className="setting-note advisor-cut">{cutNote(entry)}</p>}
      {!row.full && <p className="setting-note">Older entry. Tether kept only the first 200 characters of the question and the first 300 of the answer.</p>}
      <Text label="Question">{shown.question}</Text>
      {entry?.error && <section className="advisor-part"><h4>Error</h4><p className="advisor-text error">{entry.error}</p></section>}
      {replies && replies.length > 1
        ? replies.map((reply, index) => <Text key={`${index}-${reply.role}`} label={`${reply.role === 'main' ? 'Advisor' : 'Second advisor'} (${who(reply.agent, reply.model)})${reply.confidence ? ` · ${reply.confidence} confidence` : ''}`}>{reply.answer || 'No answer.'}</Text>)
        : <Text label={shown.status === 'off' ? 'Answer' : `Answer (${who(shown.agent, shown.model)})`}>{shown.answer || (shown.status === 'off' ? 'The advisor was off. The agent decided or asked you itself.' : 'No answer.')}</Text>}
      {entry?.evidence && <Text label="Evidence">{entry.evidence}</Text>}
    </>}
  </section>
}
