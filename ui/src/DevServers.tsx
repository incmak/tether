import { useEffect, useRef, useState } from 'react'
import { CheckSquare, RotateCw, Square, SquareX } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { RetryButton } from './Retry'
import { Spinner } from './Skeleton'

/** A dev server on this machine, as GET /mobile/api/dev-preview lists it (server/dev_previews.py: only dev servers,
 * never Tether, its services or an agent CLI). */
export type DevServerRow = { port: number; name: string; folder: string }
/** What POST /mobile/api/dev-preview/stop answers for each port. */
type StopState = 'stopped' | 'still-running' | 'refused' | 'not-found'
type StopResult = { port: number; state: StopState; error?: string; reason?: string }
/** A row's state in the list. stopping: the request runs (it waits up to about 3 s for the socket to close). */
type RowState = { state: 'stopping' | 'still-running' | 'refused' | 'failed'; text?: string }
type Ask = { ports: number[]; force: boolean }

const MAX_STOP = 64  // ports per stop request (server/dev_previews.py MAX_STOP)
const label = (row: DevServerRow) => [row.name, row.folder].filter(Boolean).join(' · ')
const count = (n: number) => `${n} dev server${n === 1 ? '' : 's'}`

/** Give focus back to the control that had it before the confirm box opened (it unmounts without dialog.close()). */
function useReturnFocus(fallback: string) {
  const [opener] = useState(() => document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null)
  useEffect(() => () => { requestAnimationFrame(() => {
    if (document.activeElement && document.activeElement !== document.body && document.activeElement.isConnected) return
    if (opener?.isConnected && !opener.closest('[inert]')) opener.focus()
    else document.getElementById(fallback)?.focus()  // the row stopped and left the list
  }) }, [opener, fallback])
}

function ConfirmStop({ ask, rows, fallback, onYes, onClose }: { ask: Ask; rows: DevServerRow[]; fallback: string; onYes: () => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  useReturnFocus(fallback)
  const named = ask.ports.map(port => rows.find(row => row.port === port) ?? { port, name: '', folder: '' })
  const one = named.length === 1
  const title = ask.force ? `Force stop ${one ? `localhost:${named[0].port}` : count(named.length)}?` : `Stop ${one ? `localhost:${named[0].port}` : count(named.length)}?`
  // The drawer is a <dialog> too: Escape here must close only this box, not the drawer under it.
  return <dialog {...modal} onCancel={event => { event.stopPropagation(); modal.onCancel(event) }} className="sheet dev-stop-confirm" role="alertdialog" aria-labelledby="dev-stop-title" aria-describedby="dev-stop-effect">
    <div className="sheet-heading"><h2 id="dev-stop-title">{title}</h2></div>
    <p id="dev-stop-effect" className="dev-stop-effect">{ask.force
      ? 'It did not stop when asked. A force stop ends it at once: it gets no chance to save, and unsaved work in it is lost.'
      : `${one ? 'The server ends' : 'These servers end'} now. Unsaved work in ${one ? 'it' : 'them'} is lost, and ${one ? 'its previews close' : 'their previews close'}.`}</p>
    {!one && <ul className="dev-stop-list">{named.map(row => <li key={row.port}><span>localhost:{row.port}</span>{label(row) && <small>{label(row)}</small>}</li>)}</ul>}
    {one && label(named[0]) && <p className="dev-stop-one">{label(named[0])}</p>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" autoFocus onClick={onClose}>Cancel</button>
      <button type="button" className="danger" onClick={onYes}>{ask.force ? 'Force stop' : one ? 'Stop' : `Stop ${named.length}`}</button>
    </div>
  </dialog>
}

/** The dev servers that run now, each with Stop. manage: the "Dev servers" view, with Select (several) and Stop all.
 * onPick: preview a server. onChanged: some servers stopped; read the list again. refreshed: changes each time the person
 * asks for a fresh list. */
export function DevServerList({ api, rows, manage, refreshed = 0, onPick, onChanged, titleId, title }: { api: Api; rows: DevServerRow[]; manage?: boolean; refreshed?: number; onPick: (port: number) => void; onChanged: () => void; titleId: string; title: string }) {
  const [states, setStates] = useState<Record<number, RowState>>({})
  const [gone, setGone] = useState<number[]>([])
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState<number[]>([])
  const [ask, setAsk] = useState<Ask | null>(null)
  const [said, setSaid] = useState('')
  const listRef = useRef<HTMLUListElement>(null)
  const shown = rows.filter(row => !gone.includes(row.port))
  const busy = Object.values(states).some(row => row.state === 'stopping')
  const chosen = selected.filter(port => shown.some(row => row.port === port))
  const stop = ({ ports, force }: Ask) => {
    setAsk(null)
    setStates(old => ({ ...old, ...Object.fromEntries(ports.map(port => [port, { state: 'stopping' }])) }))
    const chunks: number[][] = []
    for (let at = 0; at < ports.length; at += MAX_STOP) chunks.push(ports.slice(at, at + MAX_STOP))
    void Promise.allSettled(chunks.map(chunk => api<{ results: StopResult[] }>('dev-preview/stop', { ports: chunk, force }))).then(answers => {
      const next: Record<number, RowState> = {}, ended: number[] = [], words: string[] = []
      answers.forEach((answer, index) => {
        if (answer.status === 'rejected') {
          const text = errorText(answer.reason)
          for (const port of chunks[index]) next[port] = { state: 'failed', text }
          words.push(`Not stopped: ${text}`)
          return
        }
        for (const result of answer.value.results) {
        if (result.state === 'stopped' || result.state === 'not-found') { ended.push(result.port); continue }
        if (result.state === 'still-running') { next[result.port] = { state: 'still-running' }; words.push(`localhost:${result.port} is still running.`) }
        else { next[result.port] = { state: 'refused', text: result.error || result.reason || 'Tether does not stop this program: it is not a dev server.' }; words.push(`localhost:${result.port} was not stopped.`) }
        }
      })
      if (ended.length) words.unshift(ended.length === 1 ? `Stopped localhost:${ended[0]}.` : `Stopped ${count(ended.length)}.`)
      setStates(old => { const kept = { ...old, ...next }; for (const port of ended) delete kept[port]; return kept })
      setGone(old => [...old, ...ended])
      setSelected(old => old.filter(port => !ended.includes(port)))
      // The selection cannot change while a stop runs (busy), so it is the one that was stopped.
      if (ended.length && !Object.keys(next).length) setSelecting(false)
      setSaid(words.join(' '))
      if (ended.length) onChanged()
      // The focused Stop button may leave with its row: keep focus in the list.
      requestAnimationFrame(() => { if (!document.activeElement || document.activeElement === document.body) document.getElementById(titleId)?.focus() })
    })
  }
  // A new list from the server is the truth: a port that stopped and runs again shows again.
  const [listed, setListed] = useState(rows)
  if (listed !== rows) { setListed(rows); setGone([]) }
  // The person refreshed: a program on a port may be a new one now. Still running, Not stopped and Force stop describe
  // the old one, so only a stop that still runs keeps its state. The list read again after a stop keeps them.
  const [seen, setSeen] = useState(refreshed)
  if (seen !== refreshed) {
    setSeen(refreshed)
    setStates(old => Object.fromEntries(Object.entries(old).filter(([, row]) => row.state === 'stopping')))
  }
  const toggle = (port: number) => setSelected(old => old.includes(port) ? old.filter(item => item !== port) : [...old, port])
  const startSelect = () => { setSelected([]); setSelecting(true) }
  const endSelect = () => { setSelecting(false); setSelected([]); requestAnimationFrame(() => document.getElementById(`${titleId}-select`)?.focus()) }
  return <div className={selecting ? 'dev-servers selecting' : 'dev-servers'}>
    <div className="dev-servers-head">
      <h3 id={titleId} tabIndex={-1}>{title}{manage && shown.length > 0 && <span className="dev-servers-count"> ({shown.length})</span>}</h3>
      {manage && shown.length > 0 && <div className="dev-servers-tools">
        {selecting
          ? <button type="button" id={`${titleId}-select`} className="secondary" onClick={endSelect}>Done</button>
          : <>
            <button type="button" id={`${titleId}-select`} className="secondary" disabled={busy} onClick={startSelect}><CheckSquare size={16} aria-hidden="true" />Select</button>
            {shown.length > 1 && <button type="button" className="secondary danger-text" disabled={busy} onClick={() => setAsk({ ports: shown.map(row => row.port), force: false })}><SquareX size={16} aria-hidden="true" />Stop all</button>}
          </>}
      </div>}
    </div>
    <ul ref={listRef} aria-labelledby={titleId}>{shown.map(row => {
      const state = states[row.port], stopping = state?.state === 'stopping', name = `localhost:${row.port}`
      const status = stopping ? 'Stopping…' : state?.state === 'still-running' ? 'Still running' : state?.state === 'refused' ? 'Not stopped' : state?.state === 'failed' ? 'Not stopped' : ''
      const statusId = `${titleId}-${row.port}-status`
      return <li key={row.port} className={state ? `dev-row ${state.state}` : 'dev-row'} aria-busy={stopping || undefined}>
        {selecting
          ? <label className="dev-row-main">
            <input type="checkbox" checked={selected.includes(row.port)} disabled={busy} onChange={() => toggle(row.port)} aria-describedby={status ? statusId : undefined} />
            <span className="dev-row-text"><span className="dev-row-name">{name}</span>{label(row) && <small>{label(row)}</small>}</span>
          </label>
          : <button type="button" className="dev-row-main" disabled={stopping} aria-describedby={status ? statusId : undefined} onClick={() => onPick(row.port)}>
            <span className="dev-row-text"><span className="dev-row-name">{name}</span>{label(row) && <small>{label(row)}</small>}</span>
          </button>}
        {status && <p id={statusId} className="dev-row-status">{stopping && <Spinner />}<span>{status}{state?.text && state.state !== 'stopping' ? `: ${state.text}` : ''}</span></p>}
        {!selecting && <div className="dev-row-actions">
          {state?.state === 'still-running' && <button type="button" className="danger dev-force" onClick={() => setAsk({ ports: [row.port], force: true })}>Force stop</button>}
          {state?.state !== 'still-running' && <button type="button" className="icon-button dev-stop" aria-disabled={stopping || undefined} aria-label={`Stop ${name}`} title={`Stop ${name}`} onClick={() => { if (!stopping) setAsk({ ports: [row.port], force: false }) }}><Square size={16} aria-hidden="true" fill="currentColor" /></button>}
        </div>}
      </li>
    })}</ul>
    {selecting && <div className="dev-select-bar" role="group" aria-label="Selected dev servers">
      <span aria-live="polite">{chosen.length} selected</span>
      <button type="button" className="secondary" disabled={busy} onClick={() => setSelected(chosen.length === shown.length ? [] : shown.map(row => row.port))}>{chosen.length === shown.length ? 'Clear' : 'Select all'}</button>
      <button type="button" className="danger" disabled={!chosen.length || busy} onClick={() => setAsk({ ports: chosen, force: false })}>Stop {chosen.length || ''}</button>
    </div>}
    <p className="visually-hidden" role="status">{said}</p>
    {ask && <ConfirmStop ask={ask} rows={rows} fallback={titleId} onYes={() => stop(ask)} onClose={() => setAsk(null)} />}
  </div>
}

type DevState = { servers: DevServerRow[]; assigned: { port: number; dev_port: number }[]; free: number }

/** The "Dev servers" view of the preview drawer: what runs now, Stop for one, several or all, and the preview ports
 * that dev servers hold. Opened from the chat's ... menu. */
export function DevServerManager({ api, onPick }: { api: Api; onPick: (port: number) => void }) {
  const [data, setData] = useState<DevState | null>(null)
  const [error, setError] = useState('')
  const [round, setRound] = useState(0)
  const [done, setDone] = useState(-1)  // the round whose answer came back
  const loading = done !== round
  const [releasing, setReleasing] = useState(0)
  const [refreshed, setRefreshed] = useState(0)  // the person's Refresh and Try again
  useEffect(() => {
    const abort = new AbortController()
    api<DevState>('dev-preview', undefined, abort.signal).then(found => {
      setData(found); setError(''); setDone(round)
      // The last server stopped and the list left with the focus: give it to the empty state.
      if (!found.servers.length) requestAnimationFrame(() => { if (document.activeElement === document.body) document.getElementById('dev-manager-empty')?.focus() })
    },
      failure => { if (!abort.signal.aborted) { setError(errorText(failure)); setDone(round) } })
    return () => abort.abort()
  }, [api, round])
  const reload = () => setRound(value => value + 1)
  const release = (port: number) => {
    setReleasing(port)
    api('dev-preview/release', { port }).then(reload, failure => setError(errorText(failure))).finally(() => setReleasing(0))
  }
  const servers = data?.servers ?? []
  const inUse = data?.assigned ?? []
  return <div className="dev-manager">
    <div className="dev-manager-bar">
      <p>Programs on this machine that serve a page on a localhost port. Tap one to preview it.</p>
      <button type="button" className="icon-button" aria-label="Refresh the list" title="Refresh" disabled={loading} onClick={() => { setRefreshed(value => value + 1); reload() }}><RotateCw size={18} aria-hidden="true" /></button>
    </div>
    {error && <div className="dev-manager-error" role="alert"><p>{error}</p><RetryButton label="Try again" onRetry={() => { setError(''); setRefreshed(value => value + 1); reload() }} /></div>}
    {!data && loading && !error && <div className="dev-manager-loading" role="status"><Spinner />Looking for dev servers…</div>}
    {data && (servers.length
      ? <DevServerList api={api} rows={servers} manage refreshed={refreshed} onPick={onPick} onChanged={reload} titleId="dev-manager-title" title="Running now" />
      : <div className="dev-manager-empty"><h3 id="dev-manager-empty" tabIndex={-1}>No dev servers run now</h3><p>Ask an agent to start one, or run <code>npm run dev</code> in a tab.</p></div>)}
    {!!inUse.length && <div className="preview-servers dev-in-use">
      <h3 id="dev-in-use-title">Preview ports in use</h3>
      <p className="preview-servers-note">Each dev server you preview holds a preview port. Release one after you close every tab and preview of that dev server: a page that stays open could reach the next app on its port.</p>
      <ul aria-labelledby="dev-in-use-title">{inUse.map(row => <li key={row.dev_port}>
        <button type="button" className="secondary" disabled={!!releasing} aria-busy={releasing === row.dev_port} onClick={() => release(row.dev_port)}>
          <span>Release localhost:{row.dev_port}</span>
          <small>{servers.some(server => server.port === row.dev_port) ? 'running' : 'not running'}</small>
        </button>
      </li>)}</ul>
    </div>}
  </div>
}
