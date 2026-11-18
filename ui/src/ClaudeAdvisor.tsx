import { useCallback, useState } from 'react'
import { NoteBone, SettingSkeleton, SwitchBone } from './SettingBones'
import { errorText } from './api'
import type { Api, Footer } from './api'
import { usePolling } from './hooks'

// Claude Code's own advisor tool (server: claude_advisor.py). Tether adds advisorModel to the --settings JSON of each
// Claude tab it starts. The choice applies to new tabs; a running tab keeps the advisor it started with.
export type AdvisorModel = 'off' | 'opus' | 'fable'
export interface AdvisorProblem { code: string; text: string }
export interface ClaudeAdvisorState {
  models: { model: AdvisorModel; name: string; summary: string }[]
  global: AdvisorModel; model: AdvisorModel; source: 'global' | 'project'
  project: { override: boolean; model: AdvisorModel } | null
  user_setting: string | null
  problems: Partial<Record<AdvisorModel, AdvisorProblem[]>>
}

function useClaudeAdvisor(api: Api, project?: string) {
  const path = project ? `claude-advisor?project=${encodeURIComponent(project)}` : 'claude-advisor'
  const fetcher = useCallback((signal: AbortSignal) => api<ClaudeAdvisorState>(path, undefined, signal), [api, path])
  const { data, error: loadError, refresh } = usePolling(fetcher, 10000)
  const [pending, setPending] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  async function save(body: Record<string, unknown>) {
    setPending(body); setError('')
    try { await api<ClaudeAdvisorState>('claude-advisor', body); await refresh() }
    catch (failure) { setError(errorText(failure)) }
    finally { setPending(null) }
  }
  return { data, error: error || loadError, pending, save }
}

function ModelChoice({ name, data, value, disabled, onChange }: { name: string; data: ClaudeAdvisorState; value: AdvisorModel; disabled?: boolean; onChange: (model: AdvisorModel) => void }) {
  const current = data.models.find(item => item.model === value)
  const problems = data.problems[value] ?? []
  return <fieldset className="choice" disabled={disabled}>
    <legend>Advisor</legend>
    <div className="segmented">{data.models.map(item => <label key={item.model}>
      <input type="radio" name={name} value={item.model} checked={value === item.model} aria-describedby={`${name}-note`} onChange={() => onChange(item.model)} /><span>{item.name}</span>
    </label>)}</div>
    {current && <p id={`${name}-note`} className="setting-note">{current.summary}</p>}
    {problems.length > 0 && <div className="provider-warning" role="status"><strong>The advisor will not attach in new tabs:</strong>
      <ul className="advisor-problems">{problems.map(item => <li key={item.code}>{item.text}</li>)}</ul></div>}
  </fieldset>
}

const userNote = (data: ClaudeAdvisorState) => data.user_setting
  ? `Your Claude Code settings already set advisorModel to “${data.user_setting}”. With Off, Tether adds nothing, so tabs keep that advisor.`
  : ''

/** Settings › Agents: the choice for every Claude tab that Tether starts. */
export function GlobalClaudeAdvisor({ api }: { api: Api }) {
  const { data, error, pending, save } = useClaudeAdvisor(api)
  const value = (pending?.model as AdvisorModel | undefined) ?? data?.global ?? 'off'
  return <section className="settings-group" aria-labelledby="settings-claude-advisor"><h3 id="settings-claude-advisor">Claude Code advisor</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading Claude Code advisor"><NoteBone lines={1} /><NoteBone lines={2} /></SettingSkeleton> : <>
      <ModelChoice name="claude-advisor-global" data={data} value={value} disabled={!!pending} onChange={model => void save({ model })} />
      <p className="setting-note">A stronger model that Claude consults before big steps. Applies to new Claude tabs; open tabs keep their advisor. A project can use its own choice. {userNote(data)}</p>
    </>}
  </section>
}

/** The Project sheet: an override that beats the global choice while it is on. */
export function ProjectClaudeAdvisor({ api, project }: { api: Api; project: string }) {
  const { data, error, pending, save } = useClaudeAdvisor(api, project)
  const override = typeof pending?.override === 'boolean' ? pending.override : !!data?.project?.override
  const model = (pending?.model as AdvisorModel | undefined) ?? data?.project?.model ?? data?.global ?? 'off'
  const globalName = data?.models.find(item => item.model === data.global)?.name ?? 'Off'
  const name = data?.models.find(item => item.model === model)?.name ?? 'Off'
  return <section className="settings-group" aria-labelledby="project-claude-advisor"><h3 id="project-claude-advisor">Claude Code advisor</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading Claude Code advisor"><SwitchBone /><NoteBone lines={1} /></SettingSkeleton> : <>
      <label className="switch-row"><span><strong>Use a project choice</strong><small>{override ? `New Claude tabs here use ${name}, not the global choice (${globalName}).` : `New Claude tabs here use the global choice: ${globalName}.`}</small></span>
        <input type="checkbox" role="switch" checked={override} disabled={!!pending} onChange={event => void save({ project, override: event.target.checked, model })} /></label>
      <ModelChoice name="claude-advisor-project" data={data} value={model} disabled={!!pending || !override} onChange={next => void save({ project, override: true, model: next })} />
    </>}
  </section>
}

/** The tab's advisor in the Model sheet: what it started with, and why it may not be attached. */
export function TabAdvisor({ advisor }: { advisor: Footer['advisor'] }) {
  if (!advisor) return <p className="hint">Advisor: off in this tab.</p>
  if (advisor.source === 'claude') return <p className="hint">Advisor: {advisor.name}, from your Claude Code settings. Tether did not set one for this tab.</p>
  // No known problem is not proof that Claude Code attached it: only Claude Code knows whether a plan needs the Fable consent.
  if (!advisor.problems.length) return <p className="hint">Advisor: {advisor.name}, set for this tab. {advisor.model === 'fable' ? 'If your plan still needs a usage-credits consent, Claude Code shows a notice in this tab and works without the advisor.' : 'Claude consults it before big steps.'}</p>
  return <div className="provider-warning" role="status"><strong>Advisor: {advisor.name}, not attached</strong>
    <ul className="advisor-problems">{advisor.problems.map(item => <li key={item.code}>{item.text}</li>)}</ul></div>
}
