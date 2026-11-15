// Catch up: the sidebar view for "what needs me, and what changed?". Tabs that wait on you, grouped by goal (a stack,
// or the session), then turns that finished since you looked, then the working tabs behind one button.
import { useId, useState } from 'react'
import { Check, CheckCheck, ChevronRight, GalleryVerticalEnd, ListChecks } from 'lucide-react'
import type { Pane } from './api'
import type { Prefs } from './prefs'
import { SortButton } from './Attention'
import type { ReviewEntry } from './Attention'
import { LATE, waitingAsks } from './attention'
import { age } from './needs'
import { SkeletonRows } from './Skeleton'
import { catchUpGoals, finishedPanes, waitedSince } from './catchUp'
import type { Goal } from './catchUp'
import { RunningList, chatTitle, runningPanes } from './Sidebar'
import './CatchUp.css'
import type { Session } from './Sidebar'

const FINISHED_SHOWN = 5

interface Props {
  sessions: Session[]
  prefs: Prefs
  update: (change: (current: Prefs) => Partial<Prefs>) => void
  selected?: string
  loaded: boolean
  error: string
  now: number
  review?: ReviewEntry
  onOpen: (pane: Pane) => void
  onDeck?: () => void
}

export function CatchUp({ sessions, prefs, update, selected, loaded, error, now, review, onOpen, onDeck }: Props) {
  const ids = { needs: useId(), finished: useId(), working: useId() }
  const [allFinished, setAllFinished] = useState(false)
  const [showWorking, setShowWorking] = useState(false)
  // A finished row you open stays in its place while it is open, so the keyboard and focus keep their place.
  const [kept, setKept] = useState<string>()
  if (!loaded) return error ? null : <SkeletonRows count={4} label="Finding what needs you…" className="sidebar-skeleton" meta={false} />
  const nameOf = (pane: Pane) => chatTitle(pane, sessions.find(session => session.id === pane.tmux_session))
  const newest = prefs.waitingOrder === 'newest'
  const goals = catchUpGoals(sessions, now, prefs.attention)
  const shownGoals = newest ? [...goals].reverse().map(goal => ({ ...goal, panes: [...goal.panes].reverse() })) : goals
  const waiting = goals.reduce((sum, goal) => sum + goal.panes.length, 0)
  const unread = finishedPanes(sessions, prefs.seen, selected, now, prefs.attention)
  const keptPane = kept && kept === selected && !unread.some(pane => pane.id === kept) ? sessions.flatMap(session => session.panes).find(pane => pane.id === kept && pane.finished) : undefined
  const finished = keptPane ? [...unread, keptPane].sort((a, b) => (b.finished ?? 0) - (a.finished ?? 0)) : unread
  // The deck answers asks. A tab that waits only by its state (an approval with no ask) opens from its row.
  const asks = prefs.attention ? waitingAsks(goals.flatMap(goal => goal.panes), now).length : 0
  const working = runningPanes(sessions).filter(item => item.pane.state === 'Working')
  const markRead = () => update(current => ({ seen: { ...current.seen, ...Object.fromEntries(unread.map(pane => [pane.id, pane.finished ?? now])) } }))
  const shownFinished = allFinished ? finished : finished.slice(0, FINISHED_SHOWN)
  return <div className="catchup">
    {waiting > 0 || review ? <section className="catchup-part" aria-labelledby={ids.needs}>
      <h2 className="catchup-head" id={ids.needs}><span className="catchup-mark need" aria-hidden="true" />Needs you<span className="catchup-count">· {waiting}</span>
        {waiting > 1 && <SortButton order={prefs.waitingOrder ?? 'oldest'} onOrder={waitingOrder => update(() => ({ waitingOrder }))} />}
        {onDeck && asks > 0 && <button type="button" className="icon-button waiting-sort" aria-label="Answer one by one" title="Answer one by one" onClick={onDeck}><GalleryVerticalEnd size={15} aria-hidden="true" /></button>}
      </h2>
      {shownGoals.map(goal => <GoalBlock key={goal.key} goal={goal} now={now} selected={selected} nameOf={nameOf} onOpen={onOpen} />)}
      {review && <button type="button" className="catchup-row catchup-review" aria-label={`Review lessons: ${review.label}`} title={review.label} onClick={review.onOpen}>
        <ListChecks size={14} className="catchup-icon" aria-hidden="true" /><span className="catchup-name">Review lessons</span></button>}
    </section> : null}
    {finished.length > 0 && <section className="catchup-part" aria-labelledby={ids.finished}>
      <h2 className="catchup-head" id={ids.finished}><span className="catchup-mark done" aria-hidden="true" />Finished since you looked<span className="catchup-count">· {finished.length}</span>
        {unread.length > 0 && <button type="button" className="icon-button waiting-sort" aria-label="Mark all finished as read" title="Mark all as read" onClick={markRead}><CheckCheck size={15} aria-hidden="true" /></button>}
      </h2>
      <ul className="catchup-rows">{shownFinished.map(pane => <li key={pane.id}><button type="button" className="catchup-row" aria-current={pane.id === selected ? 'page' : undefined} onClick={() => { setKept(pane.id); onOpen(pane) }}>
        <span className="catchup-dot done" aria-hidden="true" />
        <span className="catchup-name">{nameOf(pane)}</span>
        <span className="catchup-age"><span className="visually-hidden">finished </span>{age(pane.finished ?? now, now)}</span>
        <span className="catchup-next">{sessions.find(session => session.id === pane.tmux_session)?.name}</span>
      </button></li>)}</ul>
      {finished.length > FINISHED_SHOWN && <button type="button" className="catchup-more" aria-expanded={allFinished} onClick={() => setAllFinished(!allFinished)}>{allFinished ? 'Show fewer' : `+${finished.length - FINISHED_SHOWN} more`}</button>}
    </section>}
    {!waiting && !review && !finished.length && <p className="catchup-empty"><Check size={16} aria-hidden="true" />You are caught up.</p>}
    {working.length > 0 && <section className="catchup-part">
      <button type="button" className="catchup-toggle" aria-expanded={showWorking} aria-controls={ids.working} onClick={() => setShowWorking(!showWorking)}>
        <span className="catchup-mark working" aria-hidden="true" />{showWorking ? 'Hide' : 'Show'} {working.length} working<ChevronRight size={14} className="chevron" aria-hidden="true" />
      </button>
      {showWorking && <div id={ids.working}><RunningList running={working} selected={selected} seen={prefs.seen} onOpen={onOpen} /></div>}
    </section>}
  </div>
}

// One goal: its name (and its session, for a stack), then each tab that waits, with the ask and its age.
function GoalBlock({ goal, now, selected, nameOf, onOpen }: { goal: Goal; now: number; selected?: string; nameOf: (pane: Pane) => string; onOpen: (pane: Pane) => void }) {
  const id = useId()
  return <section className="catchup-goal" aria-labelledby={id}>
    <h3 className="catchup-goal-head" id={id}><span className="catchup-goal-name">{goal.name}</span>{goal.stack && <span className="catchup-goal-session"><span className="visually-hidden">, stack in </span>{goal.session}</span>}<span className="catchup-goal-count">{goal.panes.length}<span className="visually-hidden"> waiting</span></span></h3>
    <ul className="catchup-rows">{goal.panes.map(pane => {
      const since = waitedSince(pane), waited = age(since, now), name = nameOf(pane), urgent = !!pane.ask?.urgent
      const next = pane.ask?.next || (pane.state === 'Approval' ? 'Approval' : 'Question')
      return <li key={pane.id}><button type="button" className="catchup-row" aria-current={pane.id === selected ? 'page' : undefined}
        aria-label={`${urgent ? 'Urgent: ' : ''}${name}, waited ${waited}. ${next}`} onClick={() => onOpen(pane)}>
        <span className={`catchup-dot need${pane.ask?.opened ? ' opened' : ''}${urgent ? ' urgent' : ''}`} aria-hidden="true" />
        <span className="catchup-name">{name}</span>
        <span className={since && now - since >= LATE ? 'catchup-age late' : 'catchup-age'}>{waited}</span>
        <span className="catchup-next" title={next}>{next}</span>
      </button></li>
    })}</ul>
  </section>
}
