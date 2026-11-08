// PROTOTYPE, throwaway (branch prototype/home-variants). Three structurally different Home pages, switched by ?variant=.
// Desk: one decision at a time. Timeline: every agent over the last hour. Briefing: the page as a few sentences.
// It shows only in a build with VITE_HOME_PROTOTYPE=1. Do not merge this file into main.
import { useEffect, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { Check, ChevronLeft, ChevronRight, Menu, MessageSquare, Plus } from 'lucide-react'
import type { Api, Pane } from './api'
import { AnswerActions } from './answers'
import { shortFolder } from './paneState'
import { cardTime } from './homeGroups'
import type { HomeCard, HomeGroups } from './homeGroups'
import { KIND_LABEL } from './needs'
import { AgentIcon, agentNames } from './Sidebar'
import { Timer, cardNames } from './Home'
import './HomePrototype.css'

export const PROTOTYPE = import.meta.env.VITE_HOME_PROTOTYPE === '1'
export const VARIANTS = [
  { key: 'desk', name: 'Desk', note: 'One decision at a time' },
  { key: 'timeline', name: 'Timeline', note: 'Every agent over time. History is simulated: the server keeps none yet' },
  { key: 'briefing', name: 'Briefing', note: 'The page as a few sentences' },
  { key: 'board', name: 'Shipped board', note: 'What is live now, for comparison' },
] as const
export type VariantKey = typeof VARIANTS[number]['key']
export const readVariant = (): VariantKey => {
  const value = new URLSearchParams(location.search).get('variant')
  return VARIANTS.find(variant => variant.key === value)?.key ?? 'desk'
}

export interface VariantProps {
  api: Api; groups: HomeGroups; busy: string | null; errors: Record<string, string>
  answer: Parameters<typeof AnswerActions>[0]['onAnswer']; fail: Parameters<typeof AnswerActions>[0]['onFail']
  onAnswerKey: (event: ReactKeyboardEvent<HTMLElement>, card: HomeCard) => void
  onOpen: (pane: Pane) => void; onMarkRead: (panes: string[]) => void; onNew: () => void; onMenu: () => void
}

const WORDS = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten']
const word = (count: number) => WORDS[count] ?? String(count)
const source = (card: HomeCard) => [agentNames[card.pane.agent], cardNames(card).session || card.pane.tmux_session].filter(Boolean).join(' · ')
const lowerFirst = (text: string) => text ? text[0].toLowerCase() + text.slice(1) : text

function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), ms); return () => clearInterval(timer) }, [ms])
  return now
}

function Head({ title, children, onMenu, onNew }: { title: ReactNode; children?: ReactNode; onMenu: () => void; onNew: () => void }) {
  return <header className="pv-head">
    <button type="button" className="icon-button mobile-menu" aria-label="Open sessions" onClick={onMenu}><Menu /></button>
    <div className="pv-head-text"><h1 tabIndex={-1}>{title}</h1>{children}</div>
    <button type="button" className="secondary pv-new" onClick={onNew} aria-label="New session"><Plus size={16} /><span>New session</span></button>
  </header>
}

/* ───────────────────────── Desk ───────────────────────── */

function DeskAsk({ card }: { card: HomeCard }) {
  if (card.kind === 'approval') return <p className="desk-ask desk-ask-command">{card.text}</p>
  if (card.kind === 'plan') return <blockquote className="desk-ask desk-ask-plan">{card.text}</blockquote>
  return <p className="desk-ask">{card.text || KIND_LABEL[card.kind]}</p>
}

export function Desk(props: VariantProps) {
  const { api, groups, busy, errors, answer, fail, onAnswerKey, onOpen, onMarkRead, onNew, onMenu } = props
  const [frontId, setFront] = useState<string | null>(null)
  const queue = [...groups.needs]
  const at = queue.findIndex(card => card.pane.id === frontId)
  if (at > 0) queue.unshift(...queue.splice(at, 1))
  const front = queue[0]
  const total = groups.needs.length
  const running = groups.working.length
  return <div className="pv desk">
    <Head title={total ? `${word(total)} ${total === 1 ? 'agent needs' : 'agents need'} you` : 'Nothing waits on you'} onMenu={onMenu} onNew={onNew}>
      <p className="pv-sub">{running ? `${running} working` : 'No agent is working'}{groups.done.length ? ` · ${groups.done.length} finished since you looked` : ''}</p>
    </Head>
    <div className="desk-body">
      <section className="desk-queue" aria-label="Waiting on you">
        {front ? <>
          <div className="desk-stack">
            {queue.slice(0, 3).map((card, depth) => {
              const { title } = cardNames(card)
              return <article key={card.pane.id} className="desk-card" data-depth={depth} aria-hidden={depth > 0 || undefined} inert={depth > 0}
                onKeyDown={event => onAnswerKey(event, card)} data-pane={card.pane.id}>
                <div className="desk-card-in">
                  <div className="desk-meta">
                    <AgentIcon agent={card.pane.agent} icon={card.pane.pane_icon} size={15} />
                    <span>{source(card)}</span>
                    <span className="desk-folder">{shortFolder(card.pane.dir)}</span>
                    <span className="desk-wait">Waiting <Timer kind={card.kind} at={card.at} /></span>
                  </div>
                  <h2>{title}</h2>
                  <p className="desk-kind">{KIND_LABEL[card.kind]}</p>
                  <DeskAsk card={card} />
                  <div className="desk-actions">
                    <AnswerActions api={api} item={card} label={title} waiting={busy === card.pane.id} onAnswer={answer} onFail={fail} />
                    <button type="button" className="ghost desk-open" onClick={() => onOpen(card.pane)}><MessageSquare size={15} />Open chat</button>
                  </div>
                  {errors[card.pane.id] && <p className="inbox-error" role="alert">{errors[card.pane.id]}</p>}
                </div>
              </article>
            })}
          </div>
          {queue.length > 1 && <div className="desk-next">
            <h3>Up next</h3>
            <ol>{queue.slice(1).map(card => <li key={card.pane.id}>
              <button type="button" onClick={() => setFront(card.pane.id)}>
                <span className="desk-next-title">{cardNames(card).title}</span>
                <span className="desk-next-kind">{KIND_LABEL[card.kind]}</span>
                <Timer kind={card.kind} at={card.at} />
              </button>
            </li>)}</ol>
          </div>}
        </> : <div className="desk-calm">
          <p className="desk-calm-big">Nothing waits on you.</p>
          <p>{running ? `${word(running)} ${running === 1 ? 'agent is' : 'agents are'} working. A question shows here the moment one asks.` : 'Start a session, and its questions show here.'}</p>
        </div>}
      </section>
      <aside className="desk-side">
        <section>
          <h3>Working <span>{groups.working.length}</span></h3>
          {groups.working.length ? <ul>{groups.working.map(card => <li key={card.pane.id}>
            <button type="button" className="desk-row" onClick={() => onOpen(card.pane)}>
              <span className="state-dot working" aria-hidden="true" />
              <span className="desk-row-title">{cardNames(card).title}</span>
              <Timer kind={card.kind} at={card.at} />
              <span className="desk-row-sub">{[card.pane.tmux_session, card.text].filter(Boolean).join(' · ')}</span>
            </button>
          </li>)}</ul> : <p className="desk-none">No agent is working.</p>}
        </section>
        <section>
          <h3>Finished <span>{groups.done.length}</span>{groups.done.length > 1 && <button type="button" className="ghost sm" onClick={() => onMarkRead(groups.done.map(card => card.pane.id))}>Mark all read</button>}</h3>
          {groups.done.length ? <ul>{groups.done.map(card => <li key={card.pane.id} className="desk-done">
            <button type="button" className="desk-row" onClick={() => onOpen(card.pane)}>
              <Check size={13} className="desk-check" aria-hidden="true" />
              <span className="desk-row-title">{cardNames(card).title}</span>
              <Timer kind={card.kind} at={card.at} />
              <span className="desk-row-sub">{card.pane.tmux_session}</span>
            </button>
            <button type="button" className="icon-button desk-read" aria-label={`Mark ${cardNames(card).title} read`} onClick={() => onMarkRead([card.pane.id])}><Check size={15} /></button>
          </li>)}</ul> : <p className="desk-none">You saw every finished turn.</p>}
        </section>
      </aside>
    </div>
  </div>
}

/* ───────────────────────── Timeline ───────────────────────── */

type Segment = { kind: 'work' | 'wait'; from: number; to: number; now?: boolean }
const seed = (id: string, salt: number) => { let h = salt * 977; for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 100003; return h / 100003 }

/** PROTOTYPE: only the current stretch is real (`since`, `finished`). The earlier stretches are made up from the pane id. */
function pastOf(card: HomeCard, now: number): { segments: Segment[]; finished?: number } {
  const segments: Segment[] = []
  let end = now
  if (card.kind === 'done') { end = card.at || now; }
  else segments.push({ kind: card.kind === 'working' ? 'work' : 'wait', from: card.at || now, to: now, now: true })
  if (card.kind !== 'working') {
    const start = card.kind === 'done' ? end : (card.at || now)
    segments.push({ kind: 'work', from: start - 240 - seed(card.pane.id, 1) * 1500, to: start })
  }
  for (let i = 2; i < 6; i++) {
    const last = segments[segments.length - 1]
    const gap = 60 + seed(card.pane.id, i) * 400
    const wait = { kind: 'wait' as const, from: last.from - gap, to: last.from }
    const work = { kind: 'work' as const, from: wait.from - 300 - seed(card.pane.id, i + 9) * 1200, to: wait.from }
    segments.push(wait, work)
  }
  return { segments, finished: card.kind === 'done' ? end : undefined }
}

const SPANS = [{ label: '15m', seconds: 900 }, { label: '1h', seconds: 3600 }, { label: '4h', seconds: 14400 }]

export function Timeline(props: VariantProps) {
  const { api, groups, busy, errors, answer, fail, onAnswerKey, onOpen, onNew, onMenu } = props
  const now = useNow(2000)
  const [span, setSpan] = useState(3600)
  const cards = [...groups.needs, ...groups.working, ...groups.done]
  const sessions = [...new Set(cards.map(card => card.pane.tmux_session))]
    .sort((a, b) => Number(groups.needs.some(c => c.pane.tmux_session === b)) - Number(groups.needs.some(c => c.pane.tmux_session === a)))
  const x = (t: number) => Math.max(0, Math.min(100, 100 - (now - t) / span * 100))
  const waited = groups.needs.reduce((sum, card) => sum + Math.max(0, now - card.at), 0)
  const ticks = [1, .75, .5, .25, 0]
  const tickLabel = (f: number) => f === 0 ? 'Now' : cardTime({ kind: 'done', at: now - f * span }, now).replace(' ago', '')
  return <div className="pv tl">
    <Head title={groups.needs.length ? `${word(groups.needs.length)} waiting on you` : 'Nothing waits on you'} onMenu={onMenu} onNew={onNew}>
      <p className="pv-sub">{groups.needs.length ? `Your agents waited ${cardTime({ kind: 'working', at: now - waited }, now)} in total so far.` : `${groups.working.length} working, ${groups.done.length} finished.`}</p>
    </Head>
    <div className="tl-bar">
      <div className="tl-legend" aria-hidden="true"><span className="tl-key work" />Working<span className="tl-key wait" />Waiting on you<span className="tl-key fin" />Finished</div>
      <div className="tl-spans" role="group" aria-label="Time shown">
        {SPANS.map(option => <button key={option.label} type="button" aria-pressed={span === option.seconds} onClick={() => setSpan(option.seconds)}>{option.label}</button>)}
      </div>
    </div>
    <div className="tl-grid">
      <div className="tl-axis" aria-hidden="true"><span />
        <div className="tl-axis-track">{ticks.map(f => <span key={f} style={{ left: `${100 - f * 100}%` }}>{tickLabel(f)}</span>)}</div>
      </div>
      {sessions.map(session => <section key={session} className="tl-session" aria-label={session}>
        <h2>{session}</h2>
        {cards.filter(card => card.pane.tmux_session === session).map(card => {
          const { title } = cardNames(card)
          const { segments, finished } = pastOf(card, now)
          const needs = groups.needs.includes(card)
          return <div key={card.pane.id} className={`tl-row ${needs ? 'needs' : card.kind}`} data-pane={card.pane.id} onKeyDown={event => needs && onAnswerKey(event, card)}>
            <button type="button" className="tl-label" onClick={() => onOpen(card.pane)}>
              <AgentIcon agent={card.pane.agent} icon={card.pane.pane_icon} size={14} />
              <span className="tl-title">{title}</span>
              <span className="tl-state">{needs ? <>waiting <Timer kind={card.kind} at={card.at} /></> : card.kind === 'done' ? <>finished <Timer kind="done" at={card.at} /></> : <>working <Timer kind={card.kind} at={card.at} /></>}</span>
            </button>
            <div className="tl-track">
              {ticks.slice(1, -1).map(f => <i key={f} className="tl-grid-line" style={{ left: `${100 - f * 100}%` }} />)}
              {segments.filter(s => s.to > now - span).map((s, i) => <span key={i} className={`tl-seg ${s.kind}${s.now ? ' now' : ''}`} style={{ left: `${x(s.from)}%`, width: `${x(s.to) - x(s.from)}%` }} />)}
              {finished && finished > now - span && <span className="tl-fin" style={{ left: `${x(finished)}%` }} />}
            </div>
            {needs && <div className="tl-ask">
              <p><strong>{KIND_LABEL[card.kind]}:</strong> {card.text}</p>
              <AnswerActions api={api} item={card} label={title} waiting={busy === card.pane.id} onAnswer={answer} onFail={fail} />
              {errors[card.pane.id] && <p className="inbox-error" role="alert">{errors[card.pane.id]}</p>}
            </div>}
          </div>
        })}
      </section>)}
    </div>
  </div>
}

/* ───────────────────────── Briefing ───────────────────────── */

function askPhrase(card: HomeCard) {
  if (card.kind === 'approval') return <> asks to {lowerFirst(card.text)}.</>
  if (card.kind === 'plan') return <> has a plan for you to review: <q>{card.text}</q></>
  if (card.kind === 'question') return <> asks: <q>{card.text}</q></>
  if (card.kind === 'stopped') return <> stopped when the machine restarted, and waits for a message.</>
  return <> needs you in the browser{card.text ? `: ${card.text}` : '.'}</>
}

function List({ items }: { items: ReactNode[] }) {
  return <>{items.map((item, i) => <span key={i}>{i > 0 && (i === items.length - 1 ? ' and ' : ', ')}{item}</span>)}</>
}

export function Briefing(props: VariantProps) {
  const { api, groups, busy, errors, answer, fail, onAnswerKey, onOpen, onMarkRead, onMenu } = props
  const now = useNow(30000)
  const oldest = groups.needs[0]
  const date = new Date(now * 1000).toLocaleString([], { weekday: 'long', hour: '2-digit', minute: '2-digit' })
  const name = (card: HomeCard) => <button type="button" className="brief-name" onClick={() => onOpen(card.pane)}>{cardNames(card).title}</button>
  return <div className="pv brief">
    <button type="button" className="icon-button mobile-menu brief-menu" aria-label="Open sessions" onClick={onMenu}><Menu /></button>
    <article className="brief-page">
      <header>
        <p className="brief-date">{date}</p>
        <h1 tabIndex={-1}>{groups.needs.length ? `${word(groups.needs.length)} ${groups.needs.length === 1 ? 'agent needs' : 'agents need'} you.` : 'Nothing needs you.'}</h1>
        {oldest && <p className="brief-lead">The oldest has waited <Timer kind={oldest.kind} at={oldest.at} />. {groups.working.length ? `${word(groups.working.length)} more ${groups.working.length === 1 ? 'is' : 'are'} still working.` : ''}</p>}
      </header>
      {groups.needs.length > 0 && <ol className="brief-asks">{groups.needs.map(card => <li key={card.pane.id} data-pane={card.pane.id} onKeyDown={event => onAnswerKey(event, card)}>
        <p className="brief-line">{name(card)}{askPhrase(card)}</p>
        <p className="brief-meta"><AgentIcon agent={card.pane.agent} icon={card.pane.pane_icon} size={13} />{source(card)} · {shortFolder(card.pane.dir)} · <Timer kind={card.kind} at={card.at} /></p>
        <AnswerActions api={api} item={card} label={cardNames(card).title} waiting={busy === card.pane.id} onAnswer={answer} onFail={fail} />
        {errors[card.pane.id] && <p className="inbox-error" role="alert">{errors[card.pane.id]}</p>}
      </li>)}</ol>}
      {groups.working.length > 0 && <section>
        <h2>Still working</h2>
        <p className="brief-prose"><List items={groups.working.map(card => <>{name(card)} for <Timer kind={card.kind} at={card.at} />{card.text ? <span className="brief-aside"> ({card.text})</span> : null}</>)} />.</p>
      </section>}
      {groups.done.length > 0 && <section>
        <h2>Finished <button type="button" className="brief-read" onClick={() => onMarkRead(groups.done.map(card => card.pane.id))}>Mark all read</button></h2>
        <p className="brief-prose"><List items={groups.done.map(card => <>{name(card)} <Timer kind="done" at={card.at} /></>)} />.</p>
      </section>}
    </article>
  </div>
}

/* ───────────────────────── Switcher ───────────────────────── */

export function PrototypeSwitcher({ current, onChange }: { current: VariantKey; onChange: (key: VariantKey) => void }) {
  const at = VARIANTS.findIndex(variant => variant.key === current)
  const go = (step: number) => {
    const next = VARIANTS[(at + step + VARIANTS.length) % VARIANTS.length].key
    const url = new URL(location.href); url.searchParams.set('variant', next); window.history.replaceState(window.history.state, '', url)
    onChange(next)
  }
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (target.closest('input, textarea, [contenteditable="true"]') || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key === 'ArrowLeft') go(-1)
      if (event.key === 'ArrowRight') go(1)
    }
    addEventListener('keydown', onKey)
    return () => removeEventListener('keydown', onKey)
  })
  const variant = VARIANTS[at]
  return <div className="pv-switch" role="group" aria-label="Prototype variant">
    <button type="button" aria-label="Previous variant" onClick={() => go(-1)}><ChevronLeft size={16} /></button>
    <span><b>{at + 1}/{VARIANTS.length} {variant.name}</b><small>{variant.note}</small></span>
    <button type="button" aria-label="Next variant" onClick={() => go(1)}><ChevronRight size={16} /></button>
  </div>
}
