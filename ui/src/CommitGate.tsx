import { NoteBone, SettingSkeleton, StepperBone, SwitchBone } from './SettingBones'
import { useCallback, useId, useRef, useState } from 'react'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'

import { gateSummary } from './commitGateText'
import type { CommitGateState, GateMode, GateModeInfo, GateStats } from './commitGateText'

function useCommitGate(api: Api, project?: string) {
  const path = project ? `commit-gate?project=${encodeURIComponent(project)}` : 'commit-gate'
  const requests = useRef(0)
  const fetcher = useCallback(async (signal: AbortSignal) => {
    const number = ++requests.current
    return { state: await api<CommitGateState>(path, undefined, signal), number }
  }, [api, path])
  const { data, error: loadError, refresh } = usePolling(fetcher, 15000)
  const [pending, setPending] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState<{ state: CommitGateState; after: number }>()
  async function save(body: Record<string, unknown>) {
    setPending(body); setError('')
    try {
      const state = await api<CommitGateState>('commit-gate', body)
      setSaved({ state, after: requests.current })
    } catch (failure) {
      setError(errorText(failure))
      return
    }
    finally { setPending(null) }
    void refresh().catch(() => {})
  }
  return { data: saved && (data?.number ?? 0) <= saved.after ? saved.state : data?.state, error: error || loadError, pending, save }
}

/** While a save runs (`busy`), the group stays enabled so the keyboard focus stays on the chosen mode; it only ignores input. */
function ModeChoice({ name, modes, value, disabled, busy, onChange }: { name: string; modes: GateModeInfo[]; value: GateMode; disabled?: boolean; busy: boolean; onChange: (mode: GateMode) => void }) {
  const current = modes.find(item => item.mode === value)
  return <fieldset className="choice codex-level commit-gate-mode" disabled={disabled} aria-busy={busy}>
    <legend>Mode</legend>
    <div className="segmented">{modes.map(item => <label key={item.mode}>
      <input type="radio" name={name} value={item.mode} checked={value === item.mode} onChange={() => { if (!busy) onChange(item.mode) }} /><span>{item.name}</span>
    </label>)}</div>
    {current && <p className="setting-note codex-level-note"><strong>{current.name}.</strong> {current.summary}</p>}
  </fieldset>
}

function Numbers({ stats, mode, here }: { stats: GateStats | null; mode: GateMode; here?: boolean }) {
  const { seen, rate } = stats ? gateSummary(stats, mode) : { seen: 'Commit gate stats are temporarily unavailable.', rate: '' }
  return <div className="switch-row codex-runs-summary" role="status">
    <span>{stats && <strong>Last {stats.days} days{here ? ', this project' : ''}</strong>}<small>{seen}{rate && <><br />{rate}</>}</small></span>
  </div>
}

const pendingMode = (pending: Record<string, unknown> | null) => typeof pending?.mode === 'string' ? pending.mode as GateMode : undefined

/** Settings › Agents: the global mode and the numbers from the log. */
export function GlobalCommitGate({ api }: { api: Api }) {
  const { data, error, pending, save } = useCommitGate(api)
  const value = pendingMode(pending) ?? data?.global
  return <section className="settings-group" aria-labelledby="settings-commit-gate"><h3 id="settings-commit-gate">Commit gate</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading the commit gate"><StepperBone count={3} legend="Mode" width="7ch" /><NoteBone lines={2} /><SwitchBone control="none" lines={2} from={1} /></SettingSkeleton> : <>
      <ModeChoice name="commit-gate-global" modes={data.modes} value={value ?? 'observe'} busy={!!pending} onChange={mode => void save({ mode })} />
      <p className="setting-note">For Claude sessions in every project. The gate checks that each commit message has a “Verified:” line, which says how the work was checked. A project can use its own mode. Use Enforce after a week in Observe with a false-block rate below 5%.</p>
      <Numbers stats={data.stats} mode={data.mode} />
    </>}
  </section>
}

/** The Project sheet: an override that beats the global mode while it is on. */
export function ProjectCommitGate({ api, project }: { api: Api; project: string }) {
  const { data, error, pending, save } = useCommitGate(api, project)
  const overrideId = useId()
  const override = typeof pending?.override === 'boolean' ? pending.override : !!data?.project?.override
  const mode = pendingMode(pending) ?? data?.project?.mode ?? data?.global ?? 'observe'
  const name = (value: GateMode) => data?.modes.find(item => item.mode === value)?.name ?? value
  return <section className="settings-group" aria-labelledby="project-commit-gate"><h3 id="project-commit-gate">Commit gate</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading the commit gate"><SwitchBone /><StepperBone count={3} legend="Mode" width="7ch" /><SwitchBone control="none" lines={2} from={1} /></SettingSkeleton> : <>
      <label className="switch-row"><span><strong id={overrideId}>Use a project mode</strong><small id={`${overrideId}-description`}>{override ? `Claude here uses ${name(mode)}, not the global mode ${name(data.global)}.` : `Claude here uses the global mode ${name(data.global)}.`}</small></span>
        <input type="checkbox" role="switch" aria-labelledby={overrideId} aria-describedby={`${overrideId}-description`} checked={override} disabled={!!pending} onChange={event => void save({ project, override: event.target.checked, mode })} /></label>
      <ModeChoice name="commit-gate-project" modes={data.modes} value={mode} disabled={!override} busy={!!pending} onChange={next => void save({ project, override: true, mode: next })} />
      <Numbers stats={data.stats} mode={data.mode} here />
    </>}
  </section>
}
