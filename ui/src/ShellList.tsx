import { useCallback, useEffect, useState } from 'react'
import { ChevronRight, X } from 'lucide-react'
import { errorText } from './api'
import type { Api } from './api'
import { SHELLS_CHANGED, endShell, endShells, listShells, shellName, shellsChanged } from './sideShells'
import type { SideShell } from './sideShells'

const POLL_MS = 5000

/** The sidebar "Shells" group: every shell behind a Terminal panel, open or closed (tmux session "tether-shells").
 * A row opens its shell in a panel (desktop). X ends one shell. "End idle" ends shells with nothing running; "End all" asks first.
 * A shell with a program running asks before it ends, because its work stops. */
export function ShellList({ api, open, onToggle, onOpenShell, onEnded }: { api: Api; open: boolean; onToggle: () => void; onOpenShell?: (pane: string) => void; onEnded: (panes: string[]) => void }) {
  const [shells, setShells] = useState<SideShell[] | null>(null)
  const [confirm, setConfirm] = useState<'all' | string | null>(null)  // 'all', or the pane of a busy row
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const load = useCallback((priority?: RequestPriority) => { void listShells(api, priority).then(setShells, () => setShells(current => current ?? [])) }, [api])
  useEffect(() => {
    load()
    if (!open) return
    const tick = () => { if (document.visibilityState === 'visible') load('low') }  // refresh of a list already shown
    const timer = setInterval(tick, POLL_MS)
    const changed = () => load()
    window.addEventListener(SHELLS_CHANGED, changed)
    document.addEventListener('visibilitychange', tick)
    return () => { clearInterval(timer); window.removeEventListener(SHELLS_CHANGED, changed); document.removeEventListener('visibilitychange', tick) }
  }, [open, load])
  if (!shells?.length) return null
  const busy = shells.filter(shell => !shell.idle)
  const idle = shells.length - busy.length
  const run = async (work: () => Promise<string[]>) => {
    setPending(true); setError('')
    try { const ended = await work(); setConfirm(null); if (ended.length) onEnded(ended) }
    catch (failure) { setError(errorText(failure)) }
    finally { setPending(false); shellsChanged() }
  }
  const endOne = (shell: SideShell) => {
    if (!shell.idle && confirm !== shell.pane) { setConfirm(shell.pane); setError(''); return }
    void run(() => endShell(api, shell.pane).then(() => [shell.pane]))
  }
  return <section className="group shell-group">
    <div className="group-head">
      <button className="group-toggle" aria-expanded={open} onClick={onToggle}>Shells<span className="group-count">· {shells.length}</span><ChevronRight size={14} className="chevron" /></button>
      {open && <span className="shell-group-actions">
        {idle > 0 && <button type="button" className="group-edit" disabled={pending} onClick={() => void run(() => endShells(api, 'idle'))} title="End every shell with nothing running">End idle</button>}
        <button type="button" className="group-edit danger-text" disabled={pending} aria-expanded={confirm === 'all'} onClick={() => { setConfirm(confirm === 'all' ? null : 'all'); setError('') }}>End all</button>
      </span>}
    </div>
    {open && confirm === 'all' && <div className="shell-confirm" role="alertdialog" aria-labelledby="end-all-shells">
      <p id="end-all-shells"><strong>End all {shells.length} shells?</strong>{busy.length > 0 && <> This stops {busy.map(shell => `${shell.command} in ${shellName(shell)}`).join(', ')}.</>}</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="shell-confirm-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setConfirm(null)}>Cancel</button><button type="button" className="danger" autoFocus disabled={pending} onClick={() => void run(() => endShells(api, 'all'))}>{pending ? 'Ending…' : 'End all'}</button></div>
    </div>}
    {open && <ul className="session-list shell-list">
      {shells.map(shell => {
        const name = shellName(shell)
        const state = shell.idle ? 'Idle' : `${shell.command} running`
        const body = <><span className="state-slot"><span className={`state-dot ${shell.idle ? 'idle' : 'shell-busy'}`} aria-hidden="true" /></span>
          <span className="session-text"><span className="session-title"><bdi>{name}</bdi></span><span className="shell-state">{state}{shell.folder && <> · <bdi className="shell-path">{shell.folder}</bdi></>}</span></span></>
        return <li key={shell.pane}>
          <div className="session-row">
            {onOpenShell ? <button type="button" className="session-main" title={`Open this shell in a Terminal panel. ${shell.folder}`} onClick={() => onOpenShell(shell.pane)}>{body}</button>
              : <div className="session-main shell-static" title={shell.folder}>{body}</div>}
            <button type="button" className="icon-button shell-end" aria-label={`End the shell in ${name}`} title={shell.idle ? 'End this shell' : `End this shell. It stops ${shell.command}.`} disabled={pending} onClick={() => endOne(shell)}><X size={15} /></button>
          </div>
          {confirm === shell.pane && <div className="shell-confirm" role="alertdialog" aria-labelledby={`end-${shell.pane}`}>
            <p id={`end-${shell.pane}`}><strong>{shell.command} is running.</strong> Ending this shell stops it.</p>
            {error && <p className="error" role="alert">{error}</p>}
            <div className="shell-confirm-buttons"><button type="button" className="secondary" disabled={pending} onClick={() => setConfirm(null)}>Cancel</button><button type="button" className="danger" autoFocus disabled={pending} onClick={() => endOne(shell)}>{pending ? 'Ending…' : 'End shell'}</button></div>
          </div>}
        </li>
      })}
    </ul>}
    {open && error && confirm === null && <p className="error shell-error" role="alert">{error}</p>}
  </section>
}
