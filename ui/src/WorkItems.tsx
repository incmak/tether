import { useState } from 'react'
import type { FormEvent } from 'react'
import { ClipboardList, Send } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api, Pane } from './api'
import { RetryButton } from './Retry'
import { useNow } from './attention'
import { age } from './needs'
import { answerItem, decisions, projectHue } from './work'
import type { WorkBoard, WorkItem } from './work'
import './Work.css'

// "Decisions on items": work items whose agent asked Mak something. Each card answers in place; the answer goes to the
// tab that asked. Home shows it first, and the desktop Needs you panel shows it above the tabs that wait for input.

function DecisionCard({ scope, item, prefix, panes, busy, onAnswer, onOpen }: {
  scope: string; item: WorkItem; prefix: string; panes: Pane[]; busy: boolean
  onAnswer: (item: WorkItem, reply: { choice: string } | { text: string }) => Promise<boolean>; onOpen: (pane: Pane) => void
}) {
  const now = useNow()
  const [replying, setReplying] = useState(false)
  const [text, setText] = useState('')
  const ask = item.ask!
  const titleId = `${scope}-work-${item.id}-title`
  const send = async (event: FormEvent) => {
    event.preventDefault()
    if (!text.trim()) return
    if (await onAnswer(item, { text: text.trim() })) { setText(''); setReplying(false) }
  }
  const linked = item.tabs.map(tab => ({ tab, pane: panes.find(pane => pane.id === tab.pane) }))
  return <li className="home-card work-card" data-item={item.id} aria-labelledby={titleId}>
    <div className="work-card-body">
      <span className="home-card-top">
        <span className={`work-swatch hue-${projectHue(prefix)}`} aria-hidden="true" />
        <span className="work-id">{item.id}</span>
        <span className="home-source">{item.project}</span>
        <span className="work-time" title="Asked">{age(ask.at, now)}</span>
      </span>
      <h3 id={titleId} className="work-title">{item.title}</h3>
      <p className="work-q">{ask.q}</p>
    </div>
    <div className="inbox-actions" role="group" aria-label={`Answer ${item.id}`}>
      {ask.options.map((option, index) => <button key={option} type="button" className={index === 0 ? 'inbox-yes' : undefined} disabled={busy}
        onClick={() => void onAnswer(item, { choice: option })}><span>{option}</span></button>)}
      <button type="button" aria-expanded={replying} disabled={busy} onClick={() => setReplying(open => !open)}>Reply</button>
    </div>
    {replying && <form className="work-reply" onSubmit={event => void send(event)}>
      <label className="visually-hidden" htmlFor={`${scope}-work-${item.id}-reply`}>Reply to {item.id}</label>
      <input id={`${scope}-work-${item.id}-reply`} value={text} onChange={event => setText(event.target.value)} placeholder="Or reply in words: it goes to the tab" autoFocus maxLength={4000} />
      <button type="submit" className="icon-button" aria-label="Send reply" title="Send reply" disabled={busy || !text.trim()}><Send size={16} /></button>
    </form>}
    {linked.length > 0 && <div className="work-tabs" role="group" aria-label="Linked tabs">
      {linked.map(({ tab, pane }) => <button key={tab.pane} type="button" className="work-tab" disabled={!pane} title={pane ? `Open ${tab.name || tab.pane}` : 'This tab is closed'}
        onClick={() => pane && onOpen(pane)}>
        <span className={`work-tab-dot${pane ? '' : ' closed'}`} aria-hidden="true" />
        <span>{tab.name || tab.session || tab.pane}</span>{!pane && <span className="visually-hidden">, closed</span>}
      </button>)}
    </div>}
  </li>
}

export type Result = { id: string; text: string; ok: boolean }

/** Answer state for every copy of the group (Home, the blank Home page, the Needs you panel). It lives in App, so a
 * result stays when the group moves: Home turns into the blank page once the last question is answered.
 * Each item answers on its own: answers to two items can run at once, and each keeps its own result line. */
export function useDecisionAnswers(api: Api, onRefresh: () => unknown) {
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set())
  const [results, setResults] = useState<Result[]>([])
  // An item whose answer came back 409 stays locked until the board shows a newer version of it.
  const [conflicts, setConflicts] = useState<Record<string, number>>({})
  const mark = (id: string, on: boolean) => setBusy(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next })
  const show = (result: Result) => setResults(current => [result, ...current.filter(item => item.id !== result.id)].slice(0, 3))
  const answer = async (item: WorkItem, reply: { choice: string } | { text: string }) => {
    mark(item.id, true)
    setResults(current => current.filter(entry => entry.id !== item.id))
    try {
      const done = await answerItem(api, item, reply)
      show(done.sent ? { id: item.id, text: `${item.id}: answer sent to the tab.`, ok: true }
        : { id: item.id, text: `${item.id}: answer saved. Not sent: ${done.why}`, ok: false })
      return true
    } catch (failure) {
      const changed = failure instanceof ApiError && failure.status === 409
      if (changed) setConflicts(current => ({ ...current, [item.id]: item.version }))
      show({ id: item.id, ok: false, text: changed ? `${item.id} changed before your answer. Read it again, then answer.` : `${item.id}: ${errorText(failure)}` })
      return false
    } finally {
      mark(item.id, false)
      void onRefresh()
    }
  }
  return { answer, results, locked: (item: WorkItem) => busy.has(item.id) || conflicts[item.id] === item.version }
}
export type DecisionAnswers = ReturnType<typeof useDecisionAnswers>

/** The group. Hidden while nothing waits and no result shows; a failed load shows a retry, so a broken board does
 * not hide questions. */
export function DecisionsGroup({ board, error, panes, answers, onRefresh, onOpen, compact }: {
  board?: WorkBoard; error: string; panes: Pane[]; answers: DecisionAnswers; onRefresh: () => unknown; onOpen: (pane: Pane) => void; compact?: boolean
}) {
  const items = decisions(board)
  const prefixes = new Map(board?.projects.map(project => [project.name, project.prefix]))
  const scope = compact ? 'inbox' : 'home'
  const { results } = answers
  if (!items.length && !error && !results.length) return null
  return <section id={compact ? undefined : 'home-group-decisions'} className={`home-group decisions${compact ? ' compact' : ''}`} aria-labelledby={`${scope}-work-decisions-title`}>
    <div className="home-group-head">
      <h2 id={`${scope}-work-decisions-title`}><ClipboardList size={14} aria-hidden="true" />Decisions on items</h2>
      {items.length > 0 && <span className="home-count">{items.length}</span>}
    </div>
    <div role="status">{results.map(result => <p key={result.id} className={`work-result${result.ok ? '' : ' failed'}`}>{result.text}</p>)}</div>
    {error && <p className="work-error" role="alert"><span>{board ? 'Could not refresh work items. These questions can be out of date.' : 'Could not load work items.'} {error}</span><RetryButton onRetry={onRefresh} className="ghost sm" /></p>}
    {items.length > 0 && <ul>{items.map(item => <DecisionCard key={`${item.id}:${item.ask!.id}`} scope={scope} item={item} prefix={prefixes.get(item.project) ?? item.id.split('-')[0]}
      panes={panes} busy={answers.locked(item)} onAnswer={answers.answer} onOpen={onOpen} />)}</ul>}
  </section>
}
