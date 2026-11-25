import { Spinner } from './Skeleton'
import { useSyncExternalStore } from 'react'
import { X } from 'lucide-react'
import { errorText, request } from './api'
import { reloadPage } from './backNav'

interface Run { state: 'running' | 'failed' | 'done'; target: string; error?: string }
interface Update { current?: string; latest: string | null; available: boolean; url: string; mode: 'notify' | 'auto' | 'off'; checking: boolean; can_update?: boolean; run?: Run | null }
interface Stage { state: 'running' | 'failed' | 'done'; target: string; error?: string }
interface View { update: Update | null; stage: Stage | null }

const POLL_MS = 3000
const POLL_LIMIT_MS = 900000 // The server marks a silent run as failed after the same time.
let update: Update | null = null
let stage: Stage | null = null
let view: View | null = null
let started = false
let watching = false
let dismissed: string | null = null
const listeners = new Set<() => void>()
const bare = (value: string) => value.replace(/^v/, '')
const label = (value: string) => 'v' + bare(value)

function compute(): View | null {
  if (stage?.state === 'running' || stage?.state === 'done') return { update, stage }
  if (update?.available && update.mode !== 'off' && update.latest !== dismissed) return { update, stage }
  return null
}

function notify() {
  view = compute()
  listeners.forEach(listener => listener())
}

function follow(data: Update) {
  const run = data.run
  if (run?.state === 'running' && !watching) {
    stage = { state: 'running', target: run.target }
    watch(run.target)
  } else if (run?.state === 'failed' && data.latest && bare(run.target) === bare(data.latest) && data.available && !watching) {
    stage = { state: 'failed', target: run.target, error: run.error }
  }
}

function load(retries = 0) {
  void request<Update>('update').then(data => {
    update = data
    try { dismissed = localStorage.getItem('tether-dismissed-update') } catch { /* Storage can be disabled. */ }
    follow(data)
    notify()
    if (data.checking && data.mode !== 'off' && retries < 3) {
      setTimeout(() => load(retries + 1), 15000)
    }
  }, () => { /* Retry only when the server reports an active check. */ })
}

/** The server restarts during an update, so a failed request here is normal. Only the run file or a new version ends the wait. */
function watch(target: string) {
  watching = true
  const began = Date.now()
  const finish = (next: Stage) => {
    watching = false
    stage = next
    notify()
  }
  const next = () => {
    if (Date.now() - began > POLL_LIMIT_MS) finish({ state: 'failed', target, error: 'Tether did not come back. Check the host.' })
    else setTimeout(tick, POLL_MS)
  }
  const tick = () => {
    void request<Update>('update').then(data => {
      update = data
      if (data.current && bare(data.current) === bare(target)) finish({ state: 'done', target })
      else if (data.run?.state === 'failed') finish({ state: 'failed', target, error: data.run.error })
      else next()
    }, next)
  }
  setTimeout(tick, POLL_MS)
}

function start(target: string) {
  stage = { state: 'running', target }
  notify()
  void request('update', { version: bare(target) }).then(() => watch(target), error => {
    stage = { state: 'failed', target, error: errorText(error) }
    notify()
    load()
  })
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (!started) {
    started = true
    load()
  }
  return () => { listeners.delete(listener) }
}

export function UpdateBanner() {
  const shown = useSyncExternalStore(subscribe, () => view, () => null)
  if (!shown) return null
  const active = shown.stage
  if (active?.state === 'done') {
    return <div className="release-update-banner" role="status">
      <span>Updated to {label(active.target)}</span>
      <button type="button" className="primary sm" onClick={reloadPage}>Reload</button>
    </div>
  }
  if (active?.state === 'running') {
    return <div className="release-update-banner" role="status">
      <Spinner size={14} />
      <span>Updating to {label(active.target)}. Chats reconnect after the restart.</span>
    </div>
  }
  const available = shown.update
  if (!available?.latest || !available.url.startsWith('https://')) return null
  const latest = available.latest
  const version = label(latest)
  return <div className="release-update-banner">
    <a href={available.url} target="_blank" rel="noopener noreferrer">Update available: {version}</a>
    <button type="button" className="icon-button" aria-label={`Dismiss update ${version}`} onClick={() => {
      dismissed = latest
      stage = null
      try { localStorage.setItem('tether-dismissed-update', dismissed ?? '') } catch { /* Keep dismissal for this page. */ }
      notify()
    }}><X size={16} aria-hidden="true" /></button>
    {active?.state === 'failed' && <p className="release-update-error" role="alert">Update failed. {active.error}</p>}
    {available.can_update && <button type="button" className="primary sm release-update-action" onClick={() => start(latest)}>{active?.state === 'failed' ? 'Try again' : 'Update now'}</button>}
  </div>
}
