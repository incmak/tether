import { VoiceField } from './VoiceField'
import { useRef, useState } from 'react'
import { Check, ChevronDown, Send } from 'lucide-react'
import { errorText } from './api'
import type { Api, BroadcastResult, BroadcastRow } from './api'
import { anchorOf, useModal, usePopover } from './hooks'
import type { Anchor } from './hooks'
import { AgentIcon, agentNames, paneTitle } from './Sidebar'
import type { Session } from './Sidebar'
import { ListSearch } from './ListSearch'
import { useListSearch } from './listSearch'

// "Message sessions" on the Agents page: one text to many panes, through the server's guarded broker
// (server/broadcast.py). Idle panes start selected. A working agent takes the text while it works (Tether keeps no queue).
// A pane that waits on an approval or a question is skipped by the server and never typed into.
type Via = { kind: 'direct' } | { kind: 'claude'; master: string }
const STATE_LABEL: Record<string, string> = { Idle: 'Idle', Working: 'Working', Approval: 'Approval', 'Input needed': 'Question', Unavailable: 'Unavailable', Unknown: 'Unknown', Stopped: 'Stopped' }
const RESULT_LABEL: Record<BroadcastRow['result'], string> = { sent: 'Sent', sent_busy: 'Sent while busy', skipped: 'Skipped', failed: 'Failed', handed_off: 'Handed to the master' }
const BROADCAST_TIMEOUT = 180000  // the server types into each pane in turn

const broadcastPanes = (sessions: Session[]) => sessions.flatMap(session => session.panes
  .filter(pane => pane.agent !== 'shell' && pane.writable).map(pane => ({ pane, session })))
type Item = ReturnType<typeof broadcastPanes>[number]

const paneSearchText = ({ pane, session }: Item) => [paneTitle(pane), session.name, agentNames[pane.agent], pane.dir]

function ViaMenu({ at, via, claudes, onPick, onClose }: { at: Anchor; via: Via; claudes: Item[]; onPick: (via: Via) => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const popover = usePopover(modal.ref, at, onClose)
  const pick = (next: Via) => { onPick(next); onClose() }
  const search = useListSearch(claudes, paneSearchText)
  const { shown, query, setQuery, input, setList, listKeys } = search
  // React bubbles a nested dialog's cancel (Escape) to the Agents sheet; keep it here so only the menu closes.
  return <dialog {...modal} {...popover} onCancel={event => { event.stopPropagation(); modal.onCancel(event) }} className="sheet popover via-menu" aria-label="Send via">
    <div className="action-list">
      <p className="action-label">Send via</p>
      <button type="button" data-search-skip onClick={() => pick({ kind: 'direct' })}><span className="via-check">{via.kind === 'direct' && <Check />}</span>Direct<small>Tether types it</small></button>
      <hr />
      <p className="action-label">Claude master: it delivers with peer messaging</p>
    </div>
    <ListSearch search={search} label="Find a Claude pane" noun="panes" pick />
    {!claudes.length && <p className="hint via-none">No Claude pane is open.</p>}
    {!!claudes.length && !shown.length && <p className="hint via-none list-search-none">No Claude pane matches “{query.trim()}”. <button type="button" className="link-button" onClick={() => { setQuery(''); input?.focus() }}>Clear search</button></p>}
    <div ref={setList} className="action-list" onKeyDown={listKeys}>
      {shown.map(({ pane, session }) => <button type="button" className="via-pane" key={pane.id} onClick={() => pick({ kind: 'claude', master: pane.id })}>
        <span className="via-check">{via.kind === 'claude' && via.master === pane.id && <Check />}</span>
        <span className="broadcast-name"><strong>{paneTitle(pane)}</strong><small>{session.name}</small></span>
      </button>)}
    </div>
  </dialog>
}

function Results({ result, onAgain, onDone }: { result: BroadcastResult; onAgain: () => void; onDone: () => void }) {
  const rows = [...(result.master ? [{ ...result.master, name: `${result.master.name} (master)` }] : []), ...result.results]
  const sent = result.results.filter(row => row.result === 'sent' || row.result === 'sent_busy' || row.result === 'handed_off').length
  return <div className="broadcast-results">
    <p className="hint" role="status">{result.via === 'claude'
      ? result.master && result.master.result.startsWith('sent') ? `The master got the list of ${sent} ${sent === 1 ? 'pane' : 'panes'}. It reports back in its own chat.` : 'The master did not get the message.'
      : `Sent to ${sent} of ${result.results.length}.`}</p>
    <ul className="broadcast-list">{rows.map(row => <li key={`${row.pane}-${row.name}`} className={`broadcast-row result-${row.result}`}>
      {row.agent && <AgentIcon agent={row.agent} size={16} />}
      <span className="broadcast-name"><strong>{row.name}</strong>{row.reason && <small>{row.reason}</small>}</span>
      <small className="broadcast-state">{RESULT_LABEL[row.result]}</small>
    </li>)}</ul>
    <div className="sheet-buttons"><button type="button" className="secondary" onClick={onAgain}>New message</button><button type="button" className="primary" onClick={onDone}>Done</button></div>
  </div>
}

export function Broadcast({ api, sessions, onDone }: { api: Api; sessions: Session[]; onDone: () => void }) {
  const panes = broadcastPanes(sessions)
  const [picked, setPicked] = useState<Set<string>>(() => new Set(panes.filter(item => item.pane.state === 'Idle').map(item => item.pane.id)))
  const [text, setText] = useState('')
  const [voiceBusy, setVoiceBusy] = useState(false)
  const [via, setVia] = useState<Via>({ kind: 'direct' })
  const [menuAt, setMenuAt] = useState<Anchor | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<BroadcastResult | null>(null)
  const search = useListSearch(panes, paneSearchText)
  const { shown, query, setQuery, input, setList, listKeys } = search
  const field = useRef<HTMLTextAreaElement>(null)
  const claudes = panes.filter(item => item.pane.agent === 'claude')
  const master = via.kind === 'claude' ? panes.find(item => item.pane.id === via.master)?.pane : undefined
  const targets = panes.filter(item => picked.has(item.pane.id) && item.pane.id !== master?.id)
  const toggle = (id: string) => setPicked(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next })
  const setMany = (ids: string[], on: boolean) => setPicked(current => { const next = new Set(current); for (const id of ids) if (on) next.add(id); else next.delete(id); return next })
  const shownIds = shown.filter(item => item.pane.id !== master?.id).map(item => item.pane.id)
  // Group by session in the sidebar's order, so 50 panes read as a few named blocks.
  const groups: { session: Session; items: Item[] }[] = []
  for (const item of shown) {
    const last = groups[groups.length - 1]
    if (last?.session.id === item.session.id) last.items.push(item); else groups.push({ session: item.session, items: [item] })
  }
  const grouped = groups.length > 1
  const blocked = !text.trim() ? '' : !targets.length ? 'Choose at least one pane.' : via.kind === 'claude' && !master ? 'The master pane has ended. Choose another.' : ''
  async function send(event: React.SyntheticEvent) {
    event.preventDefault()
    if (pending || voiceBusy || blocked || !text.trim()) return
    setPending(true); setError('')
    try {
      const body = { message: text, panes: targets.map(item => ({ pane: item.pane.id, thread: item.pane.thread })), via: via.kind, ...(master && { master: master.id }) }
      setResult(await api<BroadcastResult>('broadcast', body, undefined, undefined, BROADCAST_TIMEOUT))
      setText('')
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  if (result) return <Results result={result} onAgain={() => { setResult(null); setTimeout(() => field.current?.focus()) }} onDone={onDone} />
  return <form className="broadcast" onSubmit={event => void send(event)}>
    <ListSearch search={search} label="Filter panes" noun="panes" autoFocus={false} />
    <div className="broadcast-bar">
      <span className="broadcast-count">{targets.length} of {panes.length - (master ? 1 : 0)} selected{query && ` · ${shown.length} shown`}</span>
      <button type="button" className="link-button" disabled={pending || !shownIds.length} onClick={() => setMany(shownIds, true)}>{query ? 'Select shown' : 'Select all'}</button>
      <button type="button" className="link-button" disabled={pending || !shownIds.length} onClick={() => setMany(shownIds, false)}>{query ? 'Clear shown' : 'None'}</button>
    </div>
    {!panes.length ? <p className="group-empty">No agent pane is open.</p>
      : !shown.length ? <p className="group-empty list-search-none">No pane matches “{query.trim()}”. <button type="button" className="link-button" onClick={() => { setQuery(''); input?.focus() }}>Clear search</button></p>
      : <ul ref={setList} className="broadcast-list" aria-label="Panes to message" onKeyDown={listKeys}>{groups.map(({ session, items }) => {
          const ids = items.filter(item => item.pane.id !== master?.id).map(item => item.pane.id)
          const on = ids.filter(id => picked.has(id)).length
          return <li key={session.id} className="broadcast-group">
            {grouped && <label className="broadcast-group-head">
              <input type="checkbox" checked={!!ids.length && on === ids.length} disabled={pending || !ids.length}
                ref={box => { if (box) box.indeterminate = on > 0 && on < ids.length }}
                aria-label={`All panes in ${session.name}`} onChange={() => setMany(ids, on < ids.length)} />
              <span>{session.name}</span><small>{on}/{ids.length}</small>
            </label>}
            <ul>{items.map(({ pane }) => {
              const isMaster = pane.id === master?.id
              return <li key={pane.id}><label className={`broadcast-row ${isMaster ? 'is-master' : ''}`}>
                <input type="checkbox" checked={!isMaster && picked.has(pane.id)} disabled={pending || isMaster} onChange={() => toggle(pane.id)} />
                <AgentIcon agent={pane.agent} icon={pane.pane_icon} size={16} />
                <span className="broadcast-name"><strong>{paneTitle(pane)}</strong><small>{grouped ? agentNames[pane.agent] : `${session.name} · ${agentNames[pane.agent]}`}</small></span>
                <small className={`broadcast-state state-${pane.state === 'Approval' || pane.state === 'Input needed' ? 'waits' : pane.state.toLowerCase()}`}>{isMaster ? 'Master' : STATE_LABEL[pane.state] ?? pane.state}</small>
              </label></li>
            })}</ul>
          </li>
        })}</ul>}
    <p className="hint broadcast-rules">Working agents get the text while they work. Panes that wait on an approval or a question are skipped.</p>
    <label className="visually-hidden" htmlFor="broadcast-text">Message</label>
    <VoiceField multiline label="broadcast message" value={text} setValue={setText} fieldRef={field} onBusyChange={setVoiceBusy}
      field={{ id: 'broadcast-text', rows: 3, maxLength: 20000, disabled: pending, placeholder: 'Message to the selected panes',
        onKeyDown: event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void send(event) } }} />
    {(error || blocked) && <p className="error" role="alert">{error || blocked}</p>}
    <div className="broadcast-send">
      <button type="button" className="secondary via-button" aria-haspopup="dialog" disabled={pending} onClick={event => setMenuAt(anchorOf(event))}>
        {master ? `Via ${paneTitle(master)}` : 'Direct'}<ChevronDown size={16} />
      </button>
      <button className="primary" disabled={pending || voiceBusy || !text.trim() || !!blocked}><Send size={16} />{pending ? 'Sending…' : `Send to ${targets.length}`}</button>
    </div>
    {menuAt && <ViaMenu at={menuAt} via={via} claudes={claudes} onPick={setVia} onClose={() => setMenuAt(null)} />}
  </form>
}
