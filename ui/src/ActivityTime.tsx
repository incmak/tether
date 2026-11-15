import { useCallback, useState } from 'react'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import type { Prefs } from './prefs'

// Activity time, kept on this server (server/activity_time.py). Off until the person turns it on in Settings.
interface Publish { folder: string; file: string; busy: boolean; pending: boolean; published: number | null; last: { at: number; ok: boolean; error: string } | null }
interface ActivityStatus {
  enabled: boolean; agents: boolean; zone: string; since: number | null; store: string
  days: { day: string; you: number; agents: number }[]
  projects: { name: string; you: number; agents: number }[]
  publish: Publish
}

function duration(seconds: number) {
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} m`
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} m`
}

function dayLabel(day: string, today: string) {
  if (day === today) return 'Today'
  const date = new Date(`${day}T12:00:00Z`)
  return date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
}

function when(seconds: number | null | undefined) {
  return seconds ? new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : ''
}

function Toggle({ title, note, checked, disabled = false, onChange }: { title: string; note: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return <label className="switch-row"><span><strong>{title}</strong><small>{note}</small><span className="preferences-scope">All devices</span></span><input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} /></label>
}

function Bar({ value, max, kind }: { value: number; max: number; kind: 'you' | 'agents' }) {
  return <span className={`activity-bar activity-bar-${kind}`} aria-hidden="true"><span style={{ width: `${max ? Math.round(value / max * 100) : 0}%` }} /></span>
}

function Days({ status, agents }: { status: ActivityStatus; agents: boolean }) {
  const days = [...status.days].reverse()
  const today = status.days[status.days.length - 1]?.day ?? ''
  const max = Math.max(1, ...days.map(row => Math.max(row.you, agents ? row.agents : 0)))
  const hasAgents = agents || days.some(row => row.agents)
  return <table className="activity-table">
    <caption>Last {days.length === 1 ? 'day' : `${days.length} days`}</caption>
    <thead><tr><th scope="col">Day</th><th scope="col">You</th>{hasAgents && <th scope="col">Agents</th>}</tr></thead>
    <tbody>{days.map(row => <tr key={row.day}>
      <th scope="row">{dayLabel(row.day, today)}</th>
      <td><span className="activity-time">{duration(row.you)}</span><Bar value={row.you} max={max} kind="you" /></td>
      {hasAgents && <td><span className="activity-time">{duration(row.agents)}</span><Bar value={row.agents} max={max} kind="agents" /></td>}
    </tr>)}</tbody>
  </table>
}

function Projects({ status, agents }: { status: ActivityStatus; agents: boolean }) {
  if (!status.projects.length) return null
  const hasAgents = agents || status.projects.some(row => row.agents)
  return <table className="activity-table activity-projects">
    <caption>Top projects, last {status.days.length === 1 ? 'day' : `${status.days.length} days`}</caption>
    <thead><tr><th scope="col">Project</th><th scope="col">You</th>{hasAgents && <th scope="col">Agents</th>}</tr></thead>
    <tbody>{status.projects.map(row => <tr key={row.name}><th scope="row" title={row.name}>{row.name}</th><td>{duration(row.you)}</td>{hasAgents && <td>{duration(row.agents)}</td>}</tr>)}</tbody>
  </table>
}

function PublishForm({ api, status, onStatus }: { api: Api; status: ActivityStatus; onStatus: (status: ActivityStatus) => void }) {
  const saved = status.publish.folder
  const [folder, setFolder] = useState(saved)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  async function send(body: { folder?: string; now?: boolean }, done: string) {
    if (pending) return
    setPending(true); setError(''); setNotice('')
    try {
      const next = await api<ActivityStatus & { started: boolean }>('activity/publish', body)
      onStatus(next)
      if (body.folder !== undefined) setFolder(next.publish.folder)
      setNotice(body.now ? (next.started ? '' : 'A publish is already running.') : done)
    } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  const last = status.publish.last
  const busy = pending || status.publish.busy
  return <form className="session-folder-setting activity-publish" onSubmit={event => { event.preventDefault(); void send({ folder: folder.trim() }, folder.trim() ? 'Saved. Tether publishes every 6 hours when the time changed.' : 'Publishing is off.') }}>
    <label htmlFor="activity-publish-folder">Publish folder</label>
    <p id="activity-publish-help" className="setting-note">Optional. The top folder of a git checkout on this server, on a branch with an upstream. Tether writes {status.publish.file} there, commits it and pushes. Leave blank to keep the time only here.</p>
    <input id="activity-publish-folder" value={folder} maxLength={1024} placeholder="~/stats-activity" autoCapitalize="none" autoCorrect="off" spellCheck={false} autoComplete="off" aria-describedby="activity-publish-help" disabled={pending} onChange={event => { setFolder(event.target.value); setNotice(''); setError('') }} />
    {error && <p className="error" role="alert">{error}</p>}
    {!error && last && !last.ok && <p className="error" role="alert">Last publish failed: {last.error}</p>}
    {notice && <p className="setting-note" role="status">{notice}</p>}
    {saved && !notice && !(last && !last.ok) && <p className="setting-note" role="status">{status.publish.busy ? 'Publishing…' : status.publish.published ? `Published ${when(status.publish.published)}.${status.publish.pending ? ' New time waits for the next publish.' : ''}` : 'Not published yet.'}</p>}
    <div className="activity-actions">
      <button className="secondary" disabled={pending || folder.trim() === saved}>{pending ? 'Please wait…' : 'Save folder'}</button>
      {saved && <button type="button" className="secondary" disabled={busy || folder.trim() !== saved} onClick={() => void send({ now: true }, '')}>Publish now</button>}
    </div>
  </form>
}

export function ActivitySettings({ api, prefs, update }: { api: Api; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void }) {
  const fetchStatus = useCallback((signal: AbortSignal) => api<ActivityStatus>('activity', undefined, signal), [api])
  const { data: polled, error } = usePolling(fetchStatus, 15000)
  // A publish answer shows until the next poll replaces it.
  const [posted, setPosted] = useState<{ status: ActivityStatus; after: ActivityStatus | null | undefined } | null>(null)
  const status = posted && posted.after === polled ? posted.status : polled
  const today = status?.days[status.days.length - 1]
  return <section className="settings-group" aria-labelledby="settings-activity"><h3 id="settings-activity">Activity time</h3>
    <Toggle title="Keep activity time" note="Tether counts each minute you use a tab, for that tab’s project. It keeps the minutes on this server. It records no messages and no code." checked={prefs.activity} onChange={activity => update(() => ({ activity }))} />
    <Toggle title="Count agent work" note="Also count each minute an agent tab works, as a separate total. Agents can work all day, so this total can be many hours." checked={prefs.activity && prefs.activityAgents} disabled={!prefs.activity} onChange={activityAgents => update(() => ({ activityAgents }))} />
    {error && <p className="error" role="alert">{error}</p>}
    {status && !status.days.length && <p className="setting-note" role="status">{prefs.activity ? 'No time yet. Use a tab, and the first minute shows here.' : 'Off. Nothing is counted.'}</p>}
    {status && today && <p className="activity-today" role="status"><strong>Today: {duration(today.you)}</strong>{(prefs.activityAgents || today.agents > 0) && <span> · agents {duration(today.agents)}</span>}</p>}
    {status && status.days.length > 0 && <><Days status={status} agents={prefs.activityAgents} /><Projects status={status} agents={prefs.activityAgents} /></>}
    {status && status.days.length > 0 && <p className="setting-note">Days use {status.zone || 'this server’s time zone'}. A gap of up to 15 minutes between two counted minutes counts as work. Full history: <a className="activity-link" href="/mobile/api/activity/export" target="_blank" rel="noreferrer">JSON</a>.</p>}
    {status && <PublishForm api={api} status={status} onStatus={next => setPosted({ status: next, after: polled })} />}
  </section>
}
