import { useEffect, useRef, useState } from 'react'
import { FolderInput, Trash2, X } from 'lucide-react'
import type { Api, Pane } from './api'
import { errorText } from './api'
import { useActionsDialog } from './Sheets'
import type { MainModel } from './prefs'
import { NewGroup, TabGroupPicker } from './Groups'
import { needsYou, paneTitle, shownIndex } from './Sidebar'
import type { Session } from './Sidebar'
import { closeTabs } from './tabSelection'
import { count, useWords } from './words'
import './TabSelectionBar.css'

const nameOf = (pane: Pane) => [paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')

/**
 * The bar for several chosen tabs (strip or sidebar): Group and Close, each with its own question first.
 * Close asks once for all the tabs, then closes them one by one with the single close route, so each close keeps
 * the stale-chat check. A tab that fails stays chosen, with its error.
 */
export function TabSelectionBar({ api, panes, sessions, ids, threads, choosing, mainModel, onKeep, onClear, onDone }: {
  api: Api
  /** Every live tab, also archived ones: a session ends when all its tabs close. */
  panes: Pane[]
  sessions: Session[]
  ids: string[]
  /** The chat each tab held when it was chosen: the close names it, so a tab that changed chat is refused. */
  threads?: Record<string, string | null>
  choosing: boolean
  mainModel?: MainModel
  /** Keep only these tabs chosen (the ones that failed to close). */
  onKeep: (ids: string[]) => void
  onClear: () => void
  onDone: () => void
}) {
  const words = useWords()
  const [ask, setAsk] = useState<'close' | 'group' | null>(null)
  const [notice, setNotice] = useState('')
  const bar = useRef<HTMLDivElement>(null)
  const chosen = ids.flatMap(id => panes.find(pane => pane.id === id) ?? [])
  const homes = new Set(chosen.map(pane => pane.tmux_session))
  const home = homes.size === 1 ? sessions.find(session => session.id === [...homes][0]) : undefined
  // Esc clears the choice, unless a dialog is open or the key is for a text field.
  useEffect(() => {
    const keys = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('dialog[open]')) return
      const target = event.target as HTMLElement | null
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return
      event.preventDefault(); onClear()
    }
    document.addEventListener('keydown', keys)
    return () => document.removeEventListener('keydown', keys)
  }, [onClear])
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 6000); return () => clearTimeout(timer) }, [notice])
  // A toolbar: Left and Right move between its buttons (WAI-ARIA toolbar pattern); Tab still reaches each one.
  const arrows = (event: React.KeyboardEvent) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'Home' && event.key !== 'End') return
    const buttons = [...bar.current?.querySelectorAll<HTMLButtonElement>('button') ?? []]
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement)
    if (at < 0) return
    event.preventDefault()
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (at + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length
    buttons[next].focus()
  }
  const status = chosen.length ? `${count(words, chosen.length)} selected` : `Tap ${words.panes} to select them`
  const groupHint = `Stacks hold the ${words.panes} of one session. Choose ${words.panes} from one session.`
  return <>
    <div className="tab-select-bar" role="toolbar" aria-label={`Selected ${words.panes}`} ref={bar} onKeyDown={arrows}>
      <span className="tab-select-count" role="status">{status}</span>
      <button type="button" className="secondary" aria-disabled={chosen.length < 1 || !home} title={home ? `Move to a stack` : groupHint}
        onClick={() => { if (!chosen.length) return; if (!home) setNotice(groupHint); else setAsk('group') }}><FolderInput size={16} aria-hidden="true" />Group…</button>
      <button type="button" className="secondary danger-text" aria-disabled={chosen.length < 1} onClick={() => { if (chosen.length) setAsk('close') }}><Trash2 size={16} aria-hidden="true" />Close…</button>
      <button type="button" className="icon-button tab-select-done" aria-label={choosing ? 'Done selecting' : 'Clear selection'} title={choosing ? 'Done (Esc)' : 'Clear (Esc)'} onClick={onClear}><X size={18} aria-hidden="true" /></button>
      {notice && <p className="tab-select-notice" role="alert">{notice}</p>}
    </div>
    {ask === 'close' && <CloseTabs api={api} chosen={chosen} threads={threads} panes={panes} sessions={sessions} onClose={() => setAsk(null)}
      onDone={failed => { setAsk(null); onDone(); if (failed.length) onKeep(failed); else onClear() }} />}
    {ask === 'group' && home && <GroupTabs api={api} session={home} chosen={chosen} mainModel={mainModel} onClose={() => setAsk(null)} onDone={() => { setAsk(null); onDone(); onClear() }} />}
  </>
}

function CloseTabs({ api, chosen: now, threads, panes, sessions, onClose, onDone }: { api: Api; chosen: Pane[]; threads?: Record<string, string | null>; panes: Pane[]; sessions: Session[]; onClose: () => void; onDone: (failed: string[]) => void }) {
  const words = useWords()
  // The tabs the question names. A refresh while it is open does not change what Close closes.
  const [chosen] = useState(now)
  const [pending, setPending] = useState(false)
  const [failed, setFailed] = useState<{ pane: Pane; error: string }[]>([])
  const dialog = useActionsDialog(undefined, () => { if (!pending) { if (failed.length) onDone(failed.map(item => item.pane.id)); else onClose() } })
  const busy = chosen.filter(pane => pane.state === 'Working' || needsYou(pane.state))
  const chosenIds = new Set(chosen.map(pane => pane.id))
  // A session whose every tab is chosen ends with them, as the last tab's close ends its session.
  const ending = [...new Set(chosen.map(pane => pane.tmux_session))].filter(id => panes.every(pane => pane.tmux_session !== id || chosenIds.has(pane.id)))
    .map(id => sessions.find(session => session.id === id)?.name ?? id)
  async function close() {
    setPending(true)
    const result = await closeTabs(api, chosen, threads)
    setPending(false)
    if (result.failed.length) setFailed(result.failed)
    else onDone([])
  }
  const n = count(words, chosen.length)
  return <dialog {...dialog} aria-labelledby="close-tabs-title">
    <div className="sheet-heading"><h2 id="close-tabs-title">Close {n}?</h2><button type="button" className="icon-button" aria-label="Cancel" disabled={pending} onClick={failed.length ? () => onDone(failed.map(item => item.pane.id)) : onClose}><X /></button></div>
    {failed.length
      ? <div className="confirm-box" role="alert">
          <p><strong>{count(words, failed.length)} did not close.</strong> {chosen.length - failed.length > 0 && `The other ${count(words, chosen.length - failed.length)} closed. `}They stay selected.</p>
          <ul className="tab-select-list">{failed.map(item => <li key={item.pane.id}><strong>{nameOf(item.pane)}</strong>: {item.error}</li>)}</ul>
          <div className="sheet-buttons"><button type="button" className="primary" onClick={() => onDone(failed.map(item => item.pane.id))}>OK</button></div>
        </div>
      : <div className="confirm-box" role="alertdialog" aria-labelledby="close-tabs-title" aria-describedby="close-tabs-what">
          <div id="close-tabs-what">
            <ul className="tab-select-list">{chosen.map(pane => <li key={pane.id}>{nameOf(pane)}{(pane.state === 'Working' || needsYou(pane.state)) && <small> · {pane.state === 'Working' ? 'working' : 'needs you'}</small>}</li>)}</ul>
            <p>{busy.length > 0 && `${count(words, busy.length)} still ${busy.length === 1 ? 'runs' : 'run'}; closing stops ${busy.length === 1 ? 'it' : 'them'}. `}
              {ending.length > 0 && `This also ends ${ending.length === 1 ? 'the session' : 'the sessions'} ${ending.join(', ')}. `}You cannot undo this.</p>
          </div>
          <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={onClose}>Cancel</button><button type="button" className="danger" disabled={pending} onClick={() => void close()}>{pending ? 'Closing…' : `Close ${n}`}</button></div>
        </div>}
  </dialog>
}

function GroupTabs({ api, session, chosen, mainModel, onClose, onDone }: { api: Api; session: Session; chosen: Pane[]; mainModel?: MainModel; onClose: () => void; onDone: () => void }) {
  const words = useWords()
  const [making, setMaking] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const dialog = useActionsDialog(undefined, () => { if (!pending) onClose() })
  async function run(action: () => Promise<string | undefined>) {
    setPending(true); setError('')
    try { await action(); onDone() } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  return <dialog {...dialog} aria-labelledby="group-tabs-title">
    <div className="sheet-heading"><h2 id="group-tabs-title">Move {count(words, chosen.length)} to a stack</h2><button type="button" className="icon-button" aria-label="Cancel" disabled={pending} onClick={onClose}><X /></button></div>
    <p className="sheet-sub">{session.name}</p>
    {making
      ? <NewGroup api={api} session={session} panes={chosen} mainModel={mainModel} onClose={() => setMaking(false)} onDone={onDone} />
      : <TabGroupPicker api={api} session={session} panes={chosen} pending={pending} run={run} onNew={() => setMaking(true)} onBack={onClose} />}
    {error && <p className="error" role="alert">{error}</p>}
  </dialog>
}
