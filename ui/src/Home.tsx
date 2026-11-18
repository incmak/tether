import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { RetryButton } from './Retry'
import { groupOf, placeName } from './groups'
import type { FocusEvent as ReactFocusEvent, KeyboardEvent as ReactKeyboardEvent, ReactNode, Ref } from 'react'
import { BellRing, Check, ClipboardList, Hand, Menu, MessageCircle, Plus, Terminal } from 'lucide-react'
import type { Api, Pane } from './api'
import { AnswerActions } from './answers'
import { useAnswers } from './useAnswers'
import { shortFolder } from './paneState'
import { cardTime } from './homeGroups'
import type { HomeCard, HomeGroups } from './homeGroups'
import { KIND_LABEL } from './needs'
import { WorktreeMark } from './Worktree'
import { LATE, useNow } from './attention'
import { OldestButton, SortButton, ordered } from './Attention'
import type { WaitSort } from './Attention'
import type { Waiting } from './attention'
import type { DueReminders } from './Attention'
import { act, fullLabel, whenLabel } from './reminders'
import type { Reminder } from './reminders'
import { errorText } from './api'
import { age } from './needs'
import type { Session } from './Sidebar'
import { AgentIcon, agentNames, chatTitle, sessionName, shownIndex } from './Sidebar'
import { BacklogNews } from './Backlog'
import { Bone, Loading } from './Skeleton'
import { rememberCount, rememberedCount } from './skeletonCounts'
import './Home.css'

// Home answers one question: what do my agents need, across every session? Needs you, Working and Done.
// Needs you cards carry the answer buttons. The other two groups stay quiet rows, so the eye goes to what waits.

type GroupKey = keyof HomeGroups

const GROUPS: { key: GroupKey; label: string; dot: string; empty: string }[] = [
  { key: 'needs', label: 'Needs you', dot: 'approval', empty: 'Nothing waits on you.' },
  { key: 'working', label: 'Working', dot: 'working', empty: 'No agent is working.' },
  { key: 'done', label: 'Done', dot: 'done', empty: 'You saw every finished turn.' },
]

const headline = (count: number, asks: number, due: number) => count ? `${count} agent${count === 1 ? ' needs' : 's need'} you` : asks ? `${asks} tab${asks === 1 ? ' waits' : 's wait'} on you` : due ? `${due} reminder${due === 1 ? '' : 's'} due` : 'Nothing needs you'
const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`
const groupId = (key: GroupKey) => `home-group-${key}`
const focusRow = (id: string) => document.querySelector<HTMLButtonElement>(`.home [data-pane="${CSS.escape(id)}"] .home-open`)?.focus()

// Before the first list lands, each group shows as many placeholder cards as it had last time, so the board keeps its shape.
const FIRST_COUNT: Record<GroupKey, number> = { needs: 1, working: 2, done: 2 }
const BONE_WIDTHS = ['68%', '54%', '76%', '60%']

/** A placeholder card with the real card's lines, in the real classes, so heights match and nothing jumps when data lands. */
function CardBone({ group, index }: { group: GroupKey; index: number }) {
  return <li className={`home-card ${group} home-card-bone`}><div className="home-open">
    <span className="home-card-top"><span className="home-icon"><Bone w={14} h={14} round /></span><span className="home-source"><Bone w="40%" /></span><span className="home-time"><Bone w={26} /></span></span>
    <span className="home-title"><Bone w={BONE_WIDTHS[index % BONE_WIDTHS.length]} /></span>
    <span className="home-meta"><Bone w="32%" /></span>
    {group !== 'done' && <span className="home-status"><Bone w={group === 'needs' ? '86%' : '48%'} /></span>}
  </div></li>
}

function GroupBones({ group }: { group: GroupKey }) {
  const count = rememberedCount(`home.${group}`, FIRST_COUNT[group])
  if (!count) return <p className="home-empty" aria-hidden="true"><Bone w="38%" /></p>
  return <ul aria-hidden="true">{Array.from({ length: count }, (_, index) => <CardBone key={index} group={group} index={index} />)}</ul>
}

/** The time on a card. The first value comes from the layout effect, and Home's one interval keeps it current. */
function Timer({ kind, at }: { kind: string; at: number }) {
  const ref = useRef<HTMLSpanElement>(null)
  useLayoutEffect(() => { if (ref.current) ref.current.textContent = cardTime({ kind, at }, Date.now() / 1000) }, [kind, at])
  return at ? <span ref={ref} className="home-time" data-at={at} data-kind={kind} /> : null
}

function cardNames(card: HomeCard) {
  const { pane } = card
  const session = card.session?.name ?? sessionName(pane)
  const name = chatTitle(pane, { name: session })
  const title = [name, (card.session?.panes.length ?? 1) > 1 ? shownIndex(pane) : ''].filter(Boolean).join(' ')
  // In a session with groups, the card names the tab's group too: "Session › Group".
  const groups = card.session?.groups
  const where = groups?.length ? `${session} › ${placeName(groupOf(pane, groups), groups)}` : session === name ? '' : session
  return { title, session: where }
}

export function Card({ api, card, group, waiting, error, onOpen, onAnswer, onFail, onKey, onMarkRead }: {
  api: Api; card: HomeCard; group: GroupKey; waiting: boolean; error?: string
  onOpen: (pane: Pane) => void
  onAnswer: Parameters<typeof AnswerActions>[0]['onAnswer']
  onFail: Parameters<typeof AnswerActions>[0]['onFail']
  onKey: (event: ReactKeyboardEvent<HTMLLIElement>) => void
  onMarkRead: () => void
}) {
  const { pane } = card
  const { title, session } = cardNames(card)
  const source = [agentNames[pane.agent], session].filter(Boolean).join(' · ')
  const folder = shortFolder(pane.dir)
  return <li className={`home-card ${group}`} data-pane={pane.id} onKeyDown={onKey}>
    <button type="button" className="home-open" onClick={() => onOpen(pane)}>
      <span className="home-card-top">
        <span className="home-icon"><AgentIcon agent={pane.agent} icon={pane.pane_icon} size={14} /></span>
        <span className="home-source">{source}</span>
        <Timer kind={card.kind} at={card.at} />
      </span>
      <span className="home-title" title={title}>{title}</span>
      {(folder || pane.worktree) && <span className="home-meta" title={pane.dir}>{pane.worktree && <><WorktreeMark branch={pane.worktree} />{folder && ' · '}</>}{folder}</span>}
      {group !== 'done' && (group === 'needs' || card.text) && <span className="home-status" title={card.text}>
        {group === 'needs' && <strong>{KIND_LABEL[card.kind]}{card.text ? ': ' : ''}</strong>}{card.text}
      </span>}
    </button>
    <AnswerActions api={api} item={card} label={title} waiting={waiting} onAnswer={onAnswer} onFail={onFail} />
    {group === 'done' && <button type="button" className="icon-button home-mark" aria-label={`Mark ${title} read`} title="Mark read" onClick={onMarkRead}><Check size={16} /></button>}
    {error && <p className="inbox-error" role="alert">{error}</p>}
  </li>
}

/** Attention mode: the tabs whose last reply asks you something, oldest first. Each card opens the tab. */
function AsksGroup({ api, items, reminders, sessions, sort, onOpen }: { api: Api; items: Waiting[]; reminders?: DueReminders; sessions: Session[]; sort?: WaitSort; onOpen: (pane: Pane) => void }) {
  const now = useNow()
  const due = reminders?.items ?? []
  return <section id="home-group-asks" className="home-group asks" aria-labelledby="home-group-asks-title">
    <div className="home-group-head">
      <h2 id="home-group-asks-title"><Hand size={14} aria-hidden="true" />Waiting on you</h2>
      <span className="home-count">{items.length + due.length}</span>
      {sort && items.length > 1 && <SortButton {...sort} className="home-sort" />}
    </div>
    <ul>{ordered(items, sort?.order).map(({ pane, ask }) => {
      const session = sessions.find(item => item.id === pane.tmux_session)
      const title = chatTitle(pane, session)
      const source = [agentNames[pane.agent], session && session.name !== title ? session.name : ''].filter(Boolean).join(' · ')
      const seen = ask.opened ? 'Opened, not answered' : 'Not opened'
      return <li key={`${pane.id}:${ask.since}`} className="home-card asks" data-pane={pane.id}>
        <button type="button" className="home-open" onClick={() => onOpen(pane)} aria-label={`${title}, waited ${age(ask.since, now)}, ${seen.toLowerCase()}`}>
          <span className="home-card-top">
            <span className="home-icon"><AgentIcon agent={pane.agent} icon={pane.pane_icon} size={14} /></span>
            <span className="home-source">{source}</span>
            <span className={now - ask.since >= LATE ? 'home-time late' : 'home-time'}>{age(ask.since, now)}</span>
          </span>
          <span className="home-title" title={title}>{title}</span>
          {ask.next && <span className="home-status" title={ask.next}>{ask.next}</span>}
          <span className="home-meta">{seen}</span>
        </button>
      </li>
    })}
    {reminders && due.map(item => <ReminderCard key={`${item.id}:${item.occ ?? 0}`} api={api} item={item} now={now} onOpen={reminders.onOpen} />)}</ul>
  </section>
}

/** A due reminder on Home: the text opens the Reminders sheet; Done and Snooze 1 h act here. */
function ReminderCard({ api, item, now, onOpen }: { api: Api; item: Reminder; now: number; onOpen: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const when = `${item.late ? 'Late, was due' : 'Due'} ${whenLabel(item.due, now)}`
  const run = async (action: 'done' | 'snooze', body: Record<string, unknown>) => {
    setBusy(true); setError('')
    try { await act(api, action, { id: item.id, occ: item.occ, ...body }) } catch (failure) { setError(errorText(failure)); setBusy(false) }
  }
  return <li className="home-card asks home-reminder">
    <button type="button" className="home-open" onClick={onOpen} aria-label={`Reminder: ${item.text}. ${when}. Open Reminders`}>
      <span className="home-card-top">
        <span className="home-icon"><BellRing size={14} aria-hidden="true" /></span>
        <span className="home-source">Reminder</span>
        <time className={item.late ? 'home-time late' : 'home-time'} dateTime={new Date(item.due * 1000).toISOString()} title={fullLabel(item.due)}>{whenLabel(item.due, now)}</time>
      </span>
      <span className="home-status" title={item.text}>{item.text}</span>
    </button>
    <div className="inbox-actions" role="group" aria-label={`Reminder: ${item.text}`}>
      <button type="button" className="inbox-yes" disabled={busy} onClick={() => void run('done', {})}>Done</button>
      <button type="button" disabled={busy} onClick={() => void run('snooze', { for: 3600 })}>Snooze 1 h</button>
    </div>
    {error && <p className="inbox-error" role="alert">{error}</p>}
  </li>
}

function Blank({ title, text, children, onMenu, rootRef }: { title: string; text: string; children?: ReactNode; onMenu: () => void; rootRef?: Ref<HTMLElement> }) {
  // h1 takes focus when the last card leaves under it (Home's focus recovery reaches it through rootRef).
  return <section className="home home-blank" ref={rootRef}>
    <button type="button" className="icon-button mobile-menu" aria-label="Open sessions" onClick={onMenu}><Menu /></button>
    <div><Terminal size={32} strokeWidth={1.4} /><h1 tabIndex={-1}>{title}</h1>{text && <p>{text}</p>}{children}</div>
  </section>
}

/** Phone, Terminals with no chat open: one line that says why it is empty, and the way to a chat. */
export function NoTerminal({ loaded, loading, error, onRetry, onMenu }: { loaded: boolean; loading: boolean; error: string; onRetry: () => unknown; onMenu: () => void }) {
  if (!loaded && !loading) return <Blank title="Could not load sessions" text={error || 'The session list did not load.'} onMenu={onMenu}><span className="home-blank-action"><RetryButton onRetry={onRetry} className="secondary" /></span></Blank>
  if (!loaded) return <Blank title="Terminals" text="" onMenu={onMenu}><Loading label="Finding your sessions…" className="home-blank-action" /></Blank>
  return <Blank title="No chat is open" text="Terminals shows the terminal of the open chat." onMenu={onMenu}>
    <button type="button" className="primary home-blank-action" onClick={onMenu}><MessageCircle size={18} aria-hidden="true" />Choose a chat</button>
    {error && <p className="home-stale" role="alert"><span>Could not refresh the session list. {error}</span><RetryButton onRetry={onRetry} className="ghost sm" /></p>}
  </Blank>
}

export function Home({ api, groups: all, asks = [], reminders, sessions = [], sort, decisions, decisionCount = 0, hasAgents, loaded, loading, error, onRetry, onOpen, onMarkRead, onUpdate, onNew, onMenu, onBacklog }: {
  api: Api; groups: HomeGroups; asks?: Waiting[]; reminders?: DueReminders; sessions?: Session[]; sort?: WaitSort; decisions?: ReactNode; decisionCount?: number; hasAgents: boolean; loaded: boolean; loading: boolean; error: string
  onRetry: () => unknown; onOpen: (pane: Pane) => void; onMarkRead: (panes: string[]) => void; onUpdate: () => void; onNew: () => void; onMenu: () => void; onBacklog?: () => void
}) {
  const root = useRef<HTMLElement>(null)
  // A tab that waits on you shows in Waiting on you only, not again in Done.
  const asking = new Set(asks.map(item => item.pane.id))
  const groups = asking.size ? { ...all, done: all.done.filter(card => !asking.has(card.pane.id)) } : all
  const cards = [...groups.needs, ...groups.working, ...groups.done]
  const sessionCount = new Set(cards.map(card => card.session?.id ?? card.pane.id)).size
  const rows = () => [...(root.current?.querySelectorAll<HTMLButtonElement>('.home-open') ?? [])]
  const { busy, errors, answer, fail, onAnswerKey } = useAnswers({ api, items: groups.needs, active: true, onUpdate, focusRow })

  // One clock for every card: it writes only the text that changed, with no React render and no server poll.
  useEffect(() => {
    const tick = () => {
      const now = Date.now() / 1000
      root.current?.querySelectorAll<HTMLElement>('.home-time[data-at]').forEach(item => {
        const text = cardTime({ kind: item.dataset.kind ?? '', at: Number(item.dataset.at) }, now)
        if (item.textContent !== text) item.textContent = text
      })
    }
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [])

  // Keep each group's size for the next first paint.
  const counts = GROUPS.map(group => groups[group.key].length).join(',')
  useEffect(() => {
    if (!loaded) return
    counts.split(',').forEach((count, index) => rememberCount(`home.${GROUPS[index].key}`, Number(count)))
  }, [loaded, counts])

  // A card can leave under the focus: read, answered or closed. Focus then moves to the card now in that place.
  const focused = useRef<{ element: HTMLElement; at: number } | null>(null)
  // Every card that can leave counts: agent cards, asks, and due reminders (Done or Snooze removes one).
  const cardKeys = [...cards.map(card => `${card.pane.id}:${card.kind}`), ...asks.map(item => `ask:${item.pane.id}`),
    ...(reminders?.items ?? []).map(item => `reminder:${item.id}:${item.occ ?? 0}`)].join(',')
  useLayoutEffect(() => {
    const last = focused.current
    if (!last || last.element.isConnected || (document.activeElement && document.activeElement !== document.body)) return
    focused.current = null
    const list = rows()
    if (list.length) list[Math.min(last.at, list.length - 1)].focus()
    else root.current?.querySelector<HTMLElement>('h1')?.focus()
  }, [cardKeys])
  const onFocus = (event: ReactFocusEvent) => {
    const open = (event.target as Element).closest('.home-card')?.querySelector<HTMLElement>('.home-open')
    if (open) focused.current = { element: event.target as HTMLElement, at: rows().indexOf(open as HTMLButtonElement) }
  }

  const onKey = (event: ReactKeyboardEvent<HTMLLIElement>, card: HomeCard) => {
    if (event.code === 'ArrowDown' || event.code === 'ArrowUp') {
      if (event.ctrlKey || event.metaKey || event.altKey) return
      event.preventDefault(); event.stopPropagation()
      const list = rows()
      const at = list.indexOf(event.currentTarget.querySelector<HTMLButtonElement>('.home-open')!)
      list[Math.min(list.length - 1, Math.max(0, at + (event.code === 'ArrowDown' ? 1 : -1)))]?.focus()
      return
    }
    if (card.kind !== 'working' && card.kind !== 'done') onAnswerKey(event, card)
  }

  // The first paint, before any list has come back, and a server that never answered.
  if (!loaded && !loading) return <Blank title="Could not load sessions" text={error || 'The session list did not load.'} onMenu={onMenu}><span className="home-blank-action"><RetryButton onRetry={onRetry} className="secondary" /></span></Blank>
  // With no agent and no open question, the blank page still shows the last answer's result (sent or not).
  if (loaded && !hasAgents && !decisionCount && !reminders) return <Blank rootRef={root} title="Space for your next task." text="No agent is running. Open a session, or start something new." onMenu={onMenu}><button type="button" className="primary home-blank-action" onClick={onNew}><Plus size={18} />New session</button>{decisions}</Blank>

  return <section className="home" ref={root} aria-label="Home" onFocus={onFocus}>
    <div className="home-inner">
      <header className="home-head">
        <button type="button" className="icon-button mobile-menu" aria-label="Open sessions" onClick={onMenu}><Menu /></button>
        <h1 tabIndex={-1} aria-live="polite">{loaded ? headline(groups.needs.length, asks.length, reminders?.items.length ?? 0) : <><Bone w="9.5em" className="home-head-bone" /><span className="visually-hidden">Finding your agents…</span></>}</h1>
        {loaded && asks.length > 0 && <OldestButton items={asks} sessions={sessions} onOpen={onOpen} />}
        <button type="button" className="secondary home-new" onClick={onNew} aria-label="New session" title="New session"><Plus size={16} /><span>New session</span></button>
        {loaded && cards.length > 0 && <p className="home-scope">{plural(cards.length, 'active agent')} across {plural(sessionCount, 'session')}</p>}
        {!loaded && <div className="home-summary home-summary-bone" aria-hidden="true">{GROUPS.map(group => <span key={group.key}><Bone w={group.key === 'needs' ? 74 : 62} /></span>)}</div>}
        {loaded && <div className="home-summary" role="group" aria-label="Jump to a group">
          {decisionCount > 0 && <button type="button" onClick={() => document.getElementById('home-group-decisions')?.scrollIntoView({ block: 'start' })}><ClipboardList size={13} aria-hidden="true" className="home-decisions-mark" /><b>{decisionCount}</b> Decisions</button>}
          {(asks.length > 0 || reminders) && <button type="button" onClick={() => document.getElementById('home-group-asks')?.scrollIntoView({ block: 'start' })}><Hand size={13} aria-hidden="true" className="home-asks-mark" /><b>{asks.length + (reminders?.items.length ?? 0)}</b> Waiting on you</button>}
          {GROUPS.map(group => <button key={group.key} type="button" onClick={() => document.getElementById(groupId(group.key))?.scrollIntoView({ block: 'start' })}>
            <span className={`state-dot ${group.dot}`} aria-hidden="true" /><b>{groups[group.key].length}</b> {group.label}
          </button>)}
        </div>}
      </header>
      {loaded && onBacklog && <BacklogNews api={api} onOpen={onBacklog} />}
      {error && loaded && <p className="home-stale" role="alert"><span>Could not refresh this list. {error}</span><RetryButton onRetry={onRetry} className="ghost sm" /></p>}
      <div className={`home-board${loaded ? '' : ' loading skeleton'}`} aria-busy={!loaded}>
        {loaded && decisions}
        {loaded && (asks.length > 0 || reminders) && <AsksGroup api={api} items={asks} reminders={reminders} sessions={sessions} sort={sort} onOpen={onOpen} />}
        {GROUPS.map(group => {
          const items = groups[group.key]
          return <section key={group.key} id={groupId(group.key)} className={`home-group ${group.key}`} aria-labelledby={`${groupId(group.key)}-title`}>
            <div className="home-group-head">
              <h2 id={`${groupId(group.key)}-title`}><span className={`state-dot ${group.dot}`} aria-hidden="true" />{group.label}</h2>
              {loaded && <span className="home-count">{items.length}</span>}
              {group.key === 'done' && items.length > 1 && <button type="button" className="ghost sm home-mark-all" onClick={() => onMarkRead(items.map(card => card.pane.id))}>Mark all read</button>}
            </div>
            {!loaded ? <GroupBones group={group.key} />
              : items.length ? <ul>{items.map(card => <Card key={`${card.pane.id}:${card.kind}${card.ask ?? ''}`} api={api} card={card} group={group.key} waiting={busy === card.pane.id} error={errors[card.pane.id]}
                onOpen={onOpen} onAnswer={answer} onFail={fail} onKey={event => onKey(event, card)} onMarkRead={() => onMarkRead([card.pane.id])} />)}</ul>
              : <p className="home-empty">{group.empty}</p>}
          </section>
        })}
      </div>
    </div>
  </section>
}
