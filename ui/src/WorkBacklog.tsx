import { useEffect, useId, useState } from 'react'
import { ListTodo, X } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api } from './api'
import { RetryButton } from './Retry'
import { useModal } from './hooks'
import { age } from './needs'
import { STAGE_LABEL, markWords, moveBacklog } from './work'
import { Marked } from './Marked'
import type { BacklogCard, BacklogStage } from './work'

// Backlog items on the work board (server: work_backlog.py). BACKLOG.md stays the record: a card shows one item, and
// its drawer moves it between Ready, Building and Done. Everything else happens in the Backlog.

export function BacklogBoardCard({ card, now, onDetails, words = [] }: { card: BacklogCard; now: number; onDetails: () => void; words?: string[] }) {
  const hit = markWords(card, words)
  return <button type="button" className="work-bcard work-bcard-backlog" data-item={card.id} aria-haspopup="dialog" onClick={onDetails}>
    <span className="home-card-top">
      <ListTodo size={12} className="work-backlog-icon" aria-hidden="true" />
      <span className="work-id">{hit.id ? <mark>{card.id}</mark> : card.id}</span>
      <span className="home-source">Backlog · {card.area}</span>
      {card.stage === 'done' && card.doneAt && <span className="work-time" title="Done">{age(card.doneAt, now)}</span>}
    </span>
    <span className="work-title"><Marked text={card.title} words={hit.title} /></span>
    {card.stage === 'building' && card.doing && <span className="work-links"><span>Doing: {card.doing}</span></span>}
  </button>
}

const MOVED: Record<BacklogStage, string> = { ready: 'is in Ready', building: 'is in Building', done: 'is Done' }

export function BacklogDrawer({ api, card, error, onRefresh, onBacklog, onClose }: {
  api: Api; card: BacklogCard; error: string; onRefresh: () => unknown; onBacklog: (query: string) => void; onClose: () => void
}) {
  const modal = useModal(true, onClose)
  const id = useId()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null)
  // After a 409 the drawer stays locked until the board shows a newer version of the item.
  const [stale, setStale] = useState<number | null>(null)
  const locked = busy || stale === card.version
  useEffect(() => {
    const dialog = modal.ref.current
    ;(dialog?.querySelector<HTMLElement>('[data-first]') ?? dialog?.querySelector<HTMLElement>('.work-drawer-close'))?.focus()
  }, [modal.ref])
  const move = async (stage: BacklogStage) => {
    setBusy(true); setNote(null)
    try { await moveBacklog(api, card, stage); setNote({ ok: true, text: `${card.id} ${MOVED[stage]}.` }) }
    catch (failure) {
      const changed = failure instanceof ApiError && failure.status === 409
      if (changed) setStale(card.version)
      setNote({ ok: false, text: changed ? `${card.id} changed in the backlog. Read it again, then try again.` : errorText(failure) })
    }
    finally { setBusy(false); void onRefresh() }
  }
  const button = (stage: BacklogStage, label: string, primary: boolean) =>
    <button type="button" className={primary ? 'primary' : 'secondary'} data-first={primary ? '' : undefined} disabled={locked} onClick={() => void move(stage)}>{label}</button>
  const track: BacklogStage[] = ['ready', 'building', 'done']
  const at = track.indexOf(card.stage)
  return <dialog {...modal} className="work-drawer" aria-labelledby={`${id}-title`}>
    <div className="work-drawer-head">
      <div>
        <span className="home-card-top">
          <ListTodo size={12} className="work-backlog-icon" aria-hidden="true" />
          <span className="work-id">{card.id}</span>
          <span className="work-drawer-meta">Backlog · {card.area} · {STAGE_LABEL[card.stage]}</span>
        </span>
        <h3 id={`${id}-title`}>{card.title}</h3>
      </div>
      <button type="button" className="icon-button work-drawer-close" aria-label="Close item" onClick={onClose}><X /></button>
    </div>
    <div className="work-drawer-body">
      <ol className="work-track" aria-label="Stage">
        {track.map((stage, index) => <li key={stage} className={stage === card.stage ? 'on' : index < at ? 'past' : undefined} aria-current={stage === card.stage ? 'step' : undefined}>{STAGE_LABEL[stage]}</li>)}
      </ol>
      <div role="status" className="work-drawer-status">{note && <p className={`work-result${note.ok ? '' : ' failed'}`}>{note.text}</p>}</div>
      {error && <p className="work-error" role="alert"><span>Could not refresh the board. This item can be out of date. {error}</span><RetryButton onRetry={onRefresh} className="ghost sm" /></p>}
      <section className="work-block" aria-labelledby={`${id}-move`}>
        <h4 id={`${id}-move`}>Move</h4>
        <div className="work-actions">
          {card.stage !== 'done' && button('done', 'Mark done', true)}
          {card.stage === 'ready' && button('building', 'Move to Building', false)}
          {card.stage === 'building' && button('ready', 'Back to Ready', false)}
          {card.stage === 'done' && button('ready', 'Reopen', true)}
          <button type="button" className="ghost" onClick={() => onBacklog(card.id)}>Open in Backlog</button>
        </div>
        <p className="work-note">{card.stage === 'done' ? 'Reopen unticks the item and removes its Done line.'
          : card.stage === 'building' ? 'Back to Ready removes the Doing line.'
          : 'Move to Building adds a line that says you work on it.'} The change goes to the backlog file.</p>
      </section>
      {card.details.length > 0 && <section className="work-block" aria-labelledby={`${id}-details`}>
        <h4 id={`${id}-details`}>Details</h4>
        <ul className="work-backlog-details">{card.details.map((line, index) => <li key={index}>{line}</li>)}</ul>
      </section>}
      <dl className="work-kv">
        <dt>Section</dt><dd>{card.area}</dd>
        <dt>Added by</dt><dd>{card.source || '–'}</dd>
        {card.doing && <><dt>Doing</dt><dd>{card.doing}</dd></>}
        {card.done !== null && <><dt>Done</dt><dd>{card.done || 'finished'}</dd></>}
      </dl>
    </div>
  </dialog>
}
