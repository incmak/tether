import { useEffect, useRef, useState } from 'react'
import { CheckSquare, RotateCw, Square, SquareX } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { useModal } from './hooks'
import { RetryButton } from './Retry'
import { Spinner } from './Skeleton'

/** A dev server on this machine, as GET /mobile/api/dev-preview lists it (server/dev_previews.py: only dev servers,
 * never Tether, its services or an agent CLI). */
export type DevServerRow = { port: number; id?: string; name: string; folder: string }  // id: the program (its listening sockets); a row without one cannot stop
/** What POST /mobile/api/dev-preview/stop answers for each port. changed: another program listens on the port than the
 * one the list showed (the `expect` id), and nothing was signalled. */
type StopState = 'stopped' | 'still-running' | 'refused' | 'not-found' | 'changed'
type StopResult = { port: number; state: StopState; error?: string; reason?: string }
/** A row's state in the list. stopping: the request runs (it waits up to about 3 s for the socket to close). id: the
 * program (DevServerRow.id) that the state describes; a list that shows another program on the port drops it.
 * changed has no id: it describes the program that the next list shows. */
type RowState = { state: 'stopping' | 'still-running' | 'refused' | 'failed' | 'changed'; text?: string; id?: string }
const CHANGED = 'A new program uses this port now. Nothing was stopped.'
/** A stop the person is asked to confirm. rows: the programs the box shows, with their ids, taken when it opens; the
 * stop names exactly these (the server requires an id for each port). */
type Ask = { rows: (DevServerRow & { id: string })[]; force: boolean }

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
  const named = ask.rows
  const one = named.length === 1
  // The list was read again while the box was open: a program it names left its port, or another one took it.
  const moved = named.filter(row => rows.find(item => item.port === row.port)?.id !== row.id)
  const title = ask.force ? `Force stop ${one ? `localhost:${named[0].port}` : count(named.length)}?` : `Stop ${one ? `localhost:${named[0].port}` : count(named.length)}?`
  // The drawer is a <dialog> too: Escape here must close only this box, not the drawer under it.
  return <dialog {...modal} onCancel={event => { event.stopPropagation(); modal.onCancel(event) }} className="sheet dev-stop-confirm" role="alertdialog" aria-labelledby="dev-stop-title" aria-describedby="dev-stop-effect">
    <div className="sheet-heading"><h2 id="dev-stop-title">{title}</h2></div>
    <p id="dev-stop-effect" className="dev-stop-effect">{ask.force
      ? 'It did not stop when asked. A force stop ends it at once: it gets no chance to save, and unsaved work in it is lost.'
      : `${one ? 'The server ends' : 'These servers end'} now. Unsaved work in ${one ? 'it' : 'them'} is lost, and ${one ? 'its previews close' : 'their previews close'}.`}</p>
    {!one && <ul className="dev-stop-list">{named.map(row => <li key={row.port}><span>localhost:{row.port}</span>{label(row) && <small>{label(row)}</small>}</li>)}</ul>}
    {one && label(named[0]) && <p className="dev-stop-one">{label(named[0])}</p>}
    {moved.length > 0 && <p className="dev-stop-moved" role="alert">{one ? `localhost:${moved[0].port} has changed` : `${moved.map(row => `localhost:${row.port}`).join(', ')} ${moved.length === 1 ? 'has' : 'have'} changed`}: the program stopped, or a new one uses the port. Nothing will stop. Close this box and check the list.</p>}
    <div className="sheet-buttons">
      <button type="button" className="secondary" autoFocus onClick={onClose}>{moved.length ? 'Close' : 'Cancel'}</button>
      <button type="button" className="danger" disabled={moved.length > 0} onClick={onYes}>{ask.force ? 'Force stop' : one ? 'Stop' : `Stop ${named.length}`}</button>
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
  const all = chosen.length === shown.filter(row => row.id).length
  // The list generation: the person's Refresh or Try again starts a new one. A stop answer from an older generation
  // describes programs that may have left: it only ends the Stopping state and reads the list again.
  const generation = useRef(refreshed)
  useEffect(() => { generation.current = refreshed }, [refreshed])
  // The list now shown, for stop answers that come back later.
  const current = useRef(rows)
  useEffect(() => { current.current = rows }, [rows])
  const holds = (port: number, id: string) => current.current.find(row => row.port === port)?.id === id
  // Only a row with an id can stop: the server stops a port only for the program that the list named.
  const stoppable = (ports: number[]) => ports.flatMap(port => { const row = shown.find(item => item.port === port); return row?.id ? [{ ...row, id: row.id }] : [] })
  const askStop = (ports: number[], force: boolean) => { const named = stoppable(ports); if (named.length) setAsk({ rows: named, force }) }
  const stop = ({ rows: named, force }: Ask) => {
    setAsk(null)
    // The programs the person saw and confirmed: the server stops a port only while that program still holds it.
    const ports = named.map(row => row.port)
    const ids: Record<number, string> = Object.fromEntries(named.map(row => [row.port, row.id]))
    setStates(old => ({ ...old, ...Object.fromEntries(ports.map(port => [port, { state: 'stopping', id: ids[port] }])) }))
    const started = generation.current
    const chunks: number[][] = []
    for (let at = 0; at < ports.length; at += MAX_STOP) chunks.push(ports.slice(at, at + MAX_STOP))
    // One request after another: the server runs one stop at a time and answers 429 (busy) to a second one.
    void (async () => {
      const next: Record<number, RowState> = {}, ended: number[] = [], words: string[] = []
      for (const chunk of chunks) {
        if (generation.current !== started) break  // the person refreshed: the rest is not sent
        const expect = Object.fromEntries(chunk.map(port => [port, ids[port]]))
        let results: StopResult[]
        try {
          results = (await api<{ results: StopResult[] }>('dev-preview/stop', { ports: chunk, force, expect })).results
        } catch (failure) {
          const text = errorText(failure)
          for (const port of chunk) next[port] = { state: 'failed', text, id: ids[port] }
          words.push(`Not stopped: ${text}`)
          continue
        }
        for (const result of results) {
          if (result.state === 'stopped' || result.state === 'not-found') { ended.push(result.port); continue }
          if (result.state === 'still-running') { next[result.port] = { state: 'still-running', id: ids[result.port] }; words.push(`localhost:${result.port} is still running.`) }
          else if (result.state === 'changed') { next[result.port] = { state: 'changed' }; words.push(`localhost:${result.port}: ${CHANGED}`) }
          else { next[result.port] = { state: 'refused', id: ids[result.port], text: result.error || result.reason || 'Tether does not stop this program: it is not a dev server.' }; words.push(`localhost:${result.port} was not stopped.`) }
        }
      }
      const changed = Object.values(next).some(row => row.state === 'changed')
      if (generation.current !== started) {
        // Answers and unsent parts from before a Refresh: the rows go back to normal.
        setStates(old => Object.fromEntries(Object.entries(old).filter(([port]) => !ports.includes(Number(port)))))
        if (ended.length || changed) onChanged()
        return
      }
      if (ended.length) words.unshift(ended.length === 1 ? `Stopped localhost:${ended[0]}.` : `Stopped ${count(ended.length)}.`)
      // The list may have been read again while the stop ran: an answer describes the program it named, so it marks a
      // row only while that program still holds the port. Else the row goes back to normal.
      setStates(old => {
        const kept = { ...old }
        for (const port of ports) {
          const answer = next[port]
          if (answer && (answer.state === 'changed' || holds(port, ids[port]))) kept[port] = answer
          else delete kept[port]
        }
        return kept
      })
      setGone(old => [...old, ...ended.filter(port => holds(port, ids[port]))])
      setSelected(old => old.filter(port => !ended.includes(port)))
      // The selection cannot change while a stop runs (busy), so it is the one that was stopped.
      if (ended.length && !Object.keys(next).length) setSelecting(false)
      setSaid(words.join(' '))
      if (ended.length || changed) onChanged()
      // The focused Stop button may leave with its row: keep focus in the list.
      requestAnimationFrame(() => { if (!document.activeElement || document.activeElement === document.body) document.getElementById(titleId)?.focus() })
    })()
  }
  // A new list from the server is the truth: a port that stopped and runs again shows again.
  const [listed, setListed] = useState(rows)
  // A state describes one program: when the list shows another program on that port, the state goes. A stop that still
  // runs keeps its state until its answer comes.
  if (listed !== rows) {
    setListed(rows); setGone([])
    setStates(old => {
      const kept: Record<number, RowState> = {}
      for (const [key, row] of Object.entries(old)) {
        const now = rows.find(item => item.port === Number(key))
        if (row.state === 'stopping') kept[Number(key)] = row
        else if (!now) continue
        else if (row.state === 'changed' && row.id === undefined) kept[Number(key)] = { ...row, id: now.id }
        else if (row.id === now.id) kept[Number(key)] = row
      }
      return kept
    })
  }
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
            {shown.length > 1 && <button type="button" className="secondary danger-text" disabled={busy} onClick={() => askStop(shown.map(row => row.port), false)}><SquareX size={16} aria-hidden="true" />Stop all</button>}
          </>}
      </div>}
    </div>
    <ul ref={listRef} aria-labelledby={titleId}>{shown.map(row => {
      const kept = states[row.port]
      const state = kept && (kept.state === 'stopping' || kept.id === undefined || kept.id === row.id) ? kept : undefined
      const stopping = state?.state === 'stopping', name = `localhost:${row.port}`
      const status = stopping ? 'Stopping…' : state?.state === 'still-running' ? 'Still running' : state?.state === 'refused' ? 'Not stopped' : state?.state === 'failed' ? 'Not stopped' : state?.state === 'changed' ? CHANGED : ''
      const statusId = `${titleId}-${row.port}-status`
      return <li key={row.port} className={state ? `dev-row ${state.state}` : 'dev-row'} aria-busy={stopping || undefined}>
        {selecting
          ? <label className="dev-row-main">
            <input type="checkbox" checked={selected.includes(row.port)} disabled={busy || !row.id} onChange={() => toggle(row.port)} aria-describedby={status ? statusId : undefined} />
            <span className="dev-row-text"><span className="dev-row-name">{name}</span>{label(row) && <small>{label(row)}</small>}</span>
          </label>
          : <button type="button" className="dev-row-main" disabled={stopping} aria-describedby={status ? statusId : undefined} onClick={() => onPick(row.port)}>
            <span className="dev-row-text"><span className="dev-row-name">{name}</span>{label(row) && <small>{label(row)}</small>}</span>
          </button>}
        {status && <p id={statusId} className="dev-row-status">{stopping && <Spinner />}<span>{status}{state?.text && state.state !== 'stopping' ? `: ${state.text}` : ''}</span></p>}
        {!selecting && <div className="dev-row-actions">
          {state?.state === 'still-running' && <button type="button" className="danger dev-force" aria-disabled={busy || undefined} onClick={() => { if (!busy) askStop([row.port], true) }}>Force stop</button>}
          {state?.state !== 'still-running' && row.id && <button type="button" className="icon-button dev-stop" aria-disabled={busy || undefined} aria-label={`Stop ${name}`} title={`Stop ${name}`} onClick={() => { if (!busy) askStop([row.port], false) }}><Square size={16} aria-hidden="true" fill="currentColor" /></button>}
        </div>}
      </li>
    })}</ul>
    {selecting && <div className="dev-select-bar" role="group" aria-label="Selected dev servers">
      <span aria-live="polite">{chosen.length} selected</span>
      <button type="button" className="secondary" disabled={busy} onClick={() => setSelected(all ? [] : shown.filter(row => row.id).map(row => row.port))}>{all ? 'Clear' : 'Select all'}</button>
      <button type="button" className="danger" disabled={!chosen.length || busy} onClick={() => askStop(chosen, false)}>Stop {chosen.length || ''}</button>
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
