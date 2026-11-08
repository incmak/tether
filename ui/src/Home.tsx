import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FocusEvent as ReactFocusEvent, KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { Check, Menu, Plus, Terminal } from 'lucide-react'
import type { Api, Pane } from './api'
import { AnswerActions } from './answers'
import { useAnswers } from './useAnswers'
import { shortFolder } from './paneState'
import { cardTime } from './homeGroups'
import type { HomeCard, HomeGroups } from './homeGroups'
import { KIND_LABEL } from './needs'
import { WorktreeMark } from './Worktree'
import { AgentIcon, agentNames, chatTitle, sessionName, shownIndex } from './Sidebar'
import './Home.css'
import { Briefing, Desk, PROTOTYPE, PrototypeSwitcher, Timeline, readVariant } from './HomePrototype'

// Home answers one question: what do my agents need, across every session? Needs you, Working and Done.
// Needs you cards carry the answer buttons. The other two groups stay quiet rows, so the eye goes to what waits.

type GroupKey = keyof HomeGroups

const GROUPS: { key: GroupKey; label: string; dot: string; empty: string }[] = [
  { key: 'needs', label: 'Needs you', dot: 'approval', empty: 'Nothing waits on you.' },
  { key: 'working', label: 'Working', dot: 'working', empty: 'No agent is working.' },
  { key: 'done', label: 'Done', dot: 'done', empty: 'You saw every finished turn.' },
]

const headline = (count: number) => count ? `${count} agent${count === 1 ? ' needs' : 's need'} you` : 'Nothing needs you'
const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`
const groupId = (key: GroupKey) => `home-group-${key}`
const focusRow = (id: string) => document.querySelector<HTMLButtonElement>(`.home [data-pane="${CSS.escape(id)}"] .home-open`)?.focus()

/** The time on a card. The first value comes from the layout effect, and Home's one interval keeps it current. */
export function Timer({ kind, at }: { kind: string; at: number }) {
  const ref = useRef<HTMLSpanElement>(null)
  useLayoutEffect(() => { if (ref.current) ref.current.textContent = cardTime({ kind, at }, Date.now() / 1000) }, [kind, at])
  return at ? <span ref={ref} className="home-time" data-at={at} data-kind={kind} /> : null
}

export function cardNames(card: HomeCard) {
  const { pane } = card
  const session = card.session?.name ?? sessionName(pane)
  const name = chatTitle(pane, { name: session })
  const title = [name, (card.session?.panes.length ?? 1) > 1 ? shownIndex(pane) : ''].filter(Boolean).join(' ')
  return { title, session: session === name ? '' : session }
}

function Card({ api, card, group, waiting, error, onOpen, onAnswer, onFail, onKey, onMarkRead }: {
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

function Blank({ title, text, children, onMenu }: { title: string; text: string; children?: ReactNode; onMenu: () => void }) {
  return <section className="home home-blank">
    <button type="button" className="icon-button mobile-menu" aria-label="Open sessions" onClick={onMenu}><Menu /></button>
    <div><Terminal size={32} strokeWidth={1.4} /><h1>{title}</h1><p>{text}</p>{children}</div>
  </section>
}

export function Home({ api, groups, hasAgents, loaded, loading, error, onRetry, onOpen, onMarkRead, onUpdate, onNew, onMenu }: {
  api: Api; groups: HomeGroups; hasAgents: boolean; loaded: boolean; loading: boolean; error: string
  onRetry: () => void; onOpen: (pane: Pane) => void; onMarkRead: (panes: string[]) => void; onUpdate: () => void; onNew: () => void; onMenu: () => void
}) {
  const root = useRef<HTMLElement>(null)
  const cards = [...groups.needs, ...groups.working, ...groups.done]
  const sessionCount = new Set(cards.map(card => card.session?.id ?? card.pane.id)).size
  const rows = () => [...(root.current?.querySelectorAll<HTMLButtonElement>('.home-open') ?? [])]
  const { busy, errors, answer, fail, onAnswerKey } = useAnswers({ api, items: groups.needs, active: true, onUpdate, focusRow })
  const [variant, setVariant] = useState(() => PROTOTYPE ? readVariant() : 'board')

  // One clock for every card: it writes only the text that changed, with no React render and no server poll.
  useEffect(() => {
    const tick = () => {
      const now = Date.now() / 1000
      root.current?.querySelectorAll<HTMLElement>('.home-time').forEach(item => {
        const text = cardTime({ kind: item.dataset.kind ?? '', at: Number(item.dataset.at) }, now)
        if (item.textContent !== text) item.textContent = text
      })
    }
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [])

  // A card can leave under the focus: read, answered or closed. Focus then moves to the card now in that place.
  const focused = useRef<{ element: HTMLElement; at: number } | null>(null)
  const cardKeys = cards.map(card => `${card.pane.id}:${card.kind}`).join(',')
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
  if (!loaded && !loading) return <Blank title="Tether cannot reach the server" text={error || 'The session list did not load.'} onMenu={onMenu}><button type="button" className="secondary home-blank-action" onClick={onRetry}>Try again</button></Blank>
  if (loaded && !hasAgents) return <Blank title="Space for your next task." text="No agent is running. Open a session, or start something new." onMenu={onMenu}><button type="button" className="primary home-blank-action" onClick={onNew}><Plus size={18} />New session</button></Blank>

  if (PROTOTYPE && variant !== 'board' && loaded) {
    const props = { api, groups, busy, errors, answer, fail, onAnswerKey, onOpen, onMarkRead, onNew, onMenu }
    return <section className="home" ref={root} aria-label="Home">
      {variant === 'desk' ? <Desk {...props} /> : variant === 'timeline' ? <Timeline {...props} /> : <Briefing {...props} />}
      <PrototypeSwitcher current={variant} onChange={setVariant} />
    </section>
  }

  return <section className="home" ref={root} aria-label="Home" onFocus={onFocus}>
    {PROTOTYPE && <PrototypeSwitcher current={variant} onChange={setVariant} />}
    <div className="home-inner">
      <header className="home-head">
        <button type="button" className="icon-button mobile-menu" aria-label="Open sessions" onClick={onMenu}><Menu /></button>
        <h1 tabIndex={-1} aria-live="polite">{loaded ? headline(groups.needs.length) : 'Finding your agents…'}</h1>
        {loaded && <button type="button" className="secondary home-new" onClick={onNew} aria-label="New session" title="New session"><Plus size={16} /><span>New session</span></button>}
        {loaded && cards.length > 0 && <p className="home-scope">{plural(cards.length, 'active agent')} across {plural(sessionCount, 'session')}</p>}
        {loaded && <div className="home-summary" role="group" aria-label="Jump to a group">
          {GROUPS.map(group => <button key={group.key} type="button" onClick={() => document.getElementById(groupId(group.key))?.scrollIntoView({ block: 'start' })}>
            <span className={`state-dot ${group.dot}`} aria-hidden="true" /><b>{groups[group.key].length}</b> {group.label}
          </button>)}
        </div>}
      </header>
      {error && loaded && <p className="home-stale" role="alert"><span>Tether cannot refresh this list. {error}</span><button type="button" className="ghost sm" onClick={onRetry}>Try again</button></p>}
      <div className={`home-board${loaded ? '' : ' loading'}`} aria-busy={!loaded}>
        {GROUPS.map(group => {
          const items = groups[group.key]
          return <section key={group.key} id={groupId(group.key)} className={`home-group ${group.key}`} aria-labelledby={`${groupId(group.key)}-title`}>
            <div className="home-group-head">
              <h2 id={`${groupId(group.key)}-title`}><span className={`state-dot ${group.dot}`} aria-hidden="true" />{group.label}</h2>
              {loaded && <span className="home-count">{items.length}</span>}
              {group.key === 'done' && items.length > 1 && <button type="button" className="ghost sm home-mark-all" onClick={() => onMarkRead(items.map(card => card.pane.id))}>Mark all read</button>}
            </div>
            {!loaded ? <div className="home-placeholder" aria-hidden="true"><span /><span /></div>
              : items.length ? <ul>{items.map(card => <Card key={`${card.pane.id}:${card.kind}${card.ask ?? ''}`} api={api} card={card} group={group.key} waiting={busy === card.pane.id} error={errors[card.pane.id]}
                onOpen={onOpen} onAnswer={answer} onFail={fail} onKey={event => onKey(event, card)} onMarkRead={() => onMarkRead([card.pane.id])} />)}</ul>
              : <p className="home-empty">{group.empty}</p>}
          </section>
        })}
      </div>
    </div>
  </section>
}
