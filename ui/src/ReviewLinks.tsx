import { useRef, useState } from 'react'
import { Eye, X } from 'lucide-react'
import { errorText } from './api'
import type { Api, Pane } from './api'
import { useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, agentNames, needsYou, paneTitle } from './Sidebar'
import { ListSearch } from './ListSearch'
import { useListSearch } from './listSearch'
import './NewTask.css'
import './ReviewLinks.css'

const POP_WIDTH = 300
const plural = (count: number) => `${count} ${count === 1 ? 'tab' : 'tabs'}`

interface Link { worker: string; reviewer: string; other: Pane | undefined; otherId: string; paused: boolean; rounds?: number }

export function ReviewGrantButton({ api, pane, onChanged, onToast, onGranted }: {
  api: Api; pane: Pane; onChanged: () => void; onToast: (text: string) => void; onGranted?: () => void
}) {
  const [pending, setPending] = useState<string | null>(null)
  const grant = pane.review_grant
  if (!grant) return null
  async function allow(grant: NonNullable<Pane['review_grant']>) {
    setPending(grant.token)
    try {
      const { limit } = await api<{ limit: number }>('review-grant', { worker: pane.id, thread: grant.thread, token: grant.token })
      onToast(`Allowed 2 more rounds. Limit: ${limit}.`)
      onGranted?.()
    } catch (failure) {
      onToast(`Rounds not allowed: ${errorText(failure)}`)
      setPending(null)
    } finally {
      onChanged()
    }
  }
  return <button type="button" className="secondary review-grant" disabled={pending === grant.token}
    aria-label={`Allow 2 more rounds for ${paneTitle(pane)}`} onClick={() => void allow(grant)}>Allow 2 more rounds</button>
}

// The eye in the chat header shows linked tabs and paused reviews.
// Each row opens that tab. Its X stops that link.
export function ReviewLinks({ api, pane, panes, onOpen, onChanged, onToast }: { api: Api; pane: Pane; panes: Pane[]; onOpen: (pane: Pane) => void; onChanged: () => void; onToast: (text: string) => void }) {
  const [at, setAt] = useState<Anchor | null>(null)
  const button = useRef<HTMLButtonElement>(null)
  const byId = new Map(panes.map(item => [item.id, item]))
  const reviewing: Link[] = panes.filter(item => item.reviewers?.includes(pane.id)).map(item => ({ worker: item.id, reviewer: pane.id, other: item, otherId: item.id, paused: !!item.reviewers_paused?.includes(pane.id), rounds: item.review_grant?.rounds }))
  const reviewers: Link[] = (pane.reviewers ?? []).map(id => ({ worker: pane.id, reviewer: id, other: byId.get(id), otherId: id, paused: !!pane.reviewers_paused?.includes(id) && !pane.review_grant }))
  const total = reviewing.length + reviewers.length
  if (!total && !pane.review_grant) return null
  const label = [pane.review_grant && 'Paused review', reviewing.length && `Reviewing ${plural(reviewing.length)}`, reviewers.length && `Reviewed by ${plural(reviewers.length)}`].filter(Boolean).join(' · ')
  return <>
    <button ref={button} type="button" className="icon-button review-toggle" aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={!!at}
      onClick={() => { const box = button.current!.getBoundingClientRect(); setAt({ x: box.right - POP_WIDTH, y: box.bottom + 4 }) }}>
      <Eye size={20} />{total > 1 && <span className="review-count" aria-hidden="true">{total}</span>}
    </button>
    {at && <ReviewPop api={api} pane={pane} at={at} reviewing={reviewing} reviewers={reviewers} onClose={() => { setAt(null); requestAnimationFrame(() => button.current?.focus()) }}
      onOpen={target => { setAt(null); onOpen(target) }} onChanged={onChanged} onToast={onToast} />}
  </>
}

function ReviewPop({ api, pane, at, reviewing, reviewers, onClose, onOpen, onChanged, onToast }: { api: Api; pane: Pane; at: Anchor; reviewing: Link[]; reviewers: Link[]; onClose: () => void; onOpen: (pane: Pane) => void; onChanged: () => void; onToast: (text: string) => void }) {
  const modal = useModal(true, onClose)
  const popover = usePopover(modal.ref, at, onClose)
  const [stopping, setStopping] = useState('')
  async function stop(link: Link) {
    setStopping(link.otherId)
    try { await api('review-stop', { worker: link.worker, reviewer: link.reviewer }); onToast('Review stopped.'); onChanged() } catch (failure) { onToast(`Review not stopped: ${errorText(failure)}`) }
    setStopping('')
  }
  const row = (link: Link) => {
    const name = link.other ? paneTitle(link.other) : link.otherId
    const state = link.other?.state
    return <li key={link.otherId} className="review-row">
      <button type="button" className="review-go" disabled={!link.other} onClick={() => link.other && onOpen(link.other)}>
        {link.other && <AgentIcon agent={link.other.agent} icon={link.other.pane_icon} size={14} />}
        <span className="review-text"><span className="review-name" title={name}>{name}</span>{link.paused && <small>{link.rounds === undefined ? 'Review paused.' : `Paused after ${link.rounds} review rounds with open findings.`} Your next prompt in the worker tab restarts it.</small>}</span>
        {state === 'Working' || needsYou(state ?? '') ? <span className={`state-dot ${state === 'Working' ? 'working' : 'approval'}`} aria-label={state === 'Working' ? 'Working' : 'Needs you'} /> : null}
      </button>
      <button type="button" className="review-stop" disabled={stopping === link.otherId} aria-label={`Stop review link with ${name}`} title="Stop this review" onClick={() => void stop(link)}><X size={14} /></button>
    </li>
  }
  return <dialog {...modal} {...popover} className="sheet popover review-pop" aria-label="Review links">
    {pane.review_grant && <section className="review-paused">
      <h2>Paused review</h2>
      <p>Paused after {pane.review_grant.rounds} review rounds with open findings.</p>
      <ReviewGrantButton api={api} pane={pane} onChanged={onChanged} onToast={onToast} onGranted={onClose} />
    </section>}
    {reviewing.length > 0 && <section><h2>Reviewing</h2><ul>{reviewing.map(row)}</ul></section>}
    {reviewers.length > 0 && <section><h2>Reviewed by</h2><ul>{reviewers.map(row)}</ul></section>}
  </dialog>
}

type Role = 'reviews' | 'reviewed'
const ROLES: [Role, string, string][] = [
  ['reviews', 'This tab reviews', 'Pick the tab to check. A finished turn gets its review now.'],
  ['reviewed', 'Reviewed by', 'Pick the tab that checks this one. A finished turn here gets its review now.'],
]

// The tab menu's review option: link this tab with a tab that is already open.
export function ReviewLinkPicker({ pane, panes, pending, onPick, onBack }: { pane: Pane; panes: Pane[]; pending: boolean; onPick: (worker: string, reviewer: string) => void; onBack: () => void }) {
  const [role, setRole] = useState<Role>('reviews')
  // A link in either direction hides the tab: the server refuses a repeat and refuses two tabs that review each other.
  // This session's tabs come first, then the others; in each, the newest tab first (tmux numbers new panes upwards).
  const number = (item: Pane) => Number(item.id.slice(1)) || 0
  const open = panes.filter(item => item.id !== pane.id && (item.agent === 'claude' || item.agent === 'codex')
    && !item.reviewers?.includes(pane.id) && !pane.reviewers?.includes(item.id))
    .sort((a, b) => Number(b.tmux_session === pane.tmux_session) - Number(a.tmux_session === pane.tmux_session) || number(b) - number(a))
  const search = useListSearch(open, item => [paneTitle(item), item.label, item.tmux_session, item.dir, agentNames[item.agent]])
  const { shown, query, setQuery, input, setList, listKeys } = search
  const choose = (item: Pane) => onPick(role === 'reviews' ? item.id : pane.id, role === 'reviews' ? pane.id : item.id)
  return <div className="review-picker">
    <div className="task-agents" role="radiogroup" aria-label="Review direction">
      {ROLES.map(([choice, label]) => <button type="button" key={choice} role="radio" aria-checked={role === choice} disabled={pending} className={role === choice ? 'on' : ''} onClick={() => setRole(choice)}>{label}</button>)}
    </div>
    <p className="hint">{ROLES.find(item => item[0] === role)![2]}</p>
    <ListSearch search={search} label="Find a tab" noun="tabs" pick />
    {!open.length
      ? <p className="hint">No other open tab can link with this one.</p>
      : !shown.length
      ? <p className="hint list-search-none">No tab matches “{query.trim()}”. <button type="button" className="link-button" onClick={() => { setQuery(''); input?.focus() }}>Clear search</button></p>
      : <div ref={setList} className="action-list review-picker-list" onKeyDown={listKeys}>{shown.map(item => <button key={item.id} disabled={pending} onClick={() => choose(item)}>
          <AgentIcon agent={item.agent} icon={item.pane_icon} size={18} /><span className="review-name" title={paneTitle(item)}>{paneTitle(item)}</span><small>{item.label}</small>
        </button>)}</div>}
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={onBack}>Back</button></div>
  </div>
}
