import { useCallback } from 'react'
import type { Api } from './api'
import { usePolling } from './hooks'
import type { Prefs } from './prefs'

// Activity time for WakaTime (server/wakatime.py). Off until the person turns it on in Settings.
interface ActivityStatus { enabled: boolean; agents: boolean; cli: boolean; key: boolean; config: string; today: string; last: { at: number; ok: boolean; error: string } | null }

function Toggle({ title, note, checked, disabled = false, onChange }: { title: string; note: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return <label className="switch-row"><span><strong>{title}</strong><small>{note}</small><span className="preferences-scope">All devices</span></span><input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} /></label>
}

export function ActivitySettings({ api, prefs, update }: { api: Api; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void }) {
  const fetchStatus = useCallback((signal: AbortSignal) => api<ActivityStatus>('activity', undefined, signal), [api])
  const { data: status, error } = usePolling(fetchStatus, 15000)
  const config = status?.config ?? '~/.wakatime.cfg'
  const problem = !status ? '' : !status.cli ? 'wakatime-cli is not on this server. Install a WakaTime editor plugin or wakatime-cli, then come back.'
    : !status.key ? `${config} has no API key. Add “api_key = …” under [settings].` : prefs.wakatime && status.last && !status.last.ok ? status.last.error : ''
  return <section className="settings-group" aria-labelledby="settings-activity"><h3 id="settings-activity">Activity time</h3>
    <Toggle title="Send activity time to WakaTime" note="When you use a tab, Tether sends WakaTime this server’s name and the tab’s folder, project and branch, at most once every 2 minutes. It sends no messages and no code. WakaTime adds this up as coding time." checked={prefs.wakatime} onChange={wakatime => update(() => ({ wakatime }))} />
    <Toggle title="Count agent work" note="Every 10 minutes, wakatime-cli also reads the Claude and Codex transcripts on this server and sends the agents’ time as AI coding. Agents can work all day, so your total can grow to many hours." checked={prefs.wakatime && prefs.wakatimeAgents} disabled={!prefs.wakatime} onChange={wakatimeAgents => update(() => ({ wakatimeAgents }))} />
    {error && <p className="error" role="alert">{error}</p>}
    {problem && <p className="error" role="alert">{problem}</p>}
    {status && !problem && <p className="setting-note" role="status">{prefs.wakatime ? (status.today ? `WakaTime today: ${status.today}.` : 'WakaTime is ready.') : `Ready: wakatime-cli and the API key in ${config}.`} <a href="https://wakatime.com/dashboard" target="_blank" rel="noreferrer">Open WakaTime</a></p>}
  </section>
}
