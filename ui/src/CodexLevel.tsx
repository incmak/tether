import { NoteBone, RunOptionsBone, SettingSkeleton, StepperBone, SwitchBone } from './SettingBones'
import { useCallback, useState } from 'react'
import { SelectMenu } from './SelectMenu'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'

// The Codex level (server: codex_level.py). A Claude Code UserPromptSubmit hook gives Claude the active level's rules
// on each prompt. The person sets the level; nothing raises it.
export interface CodexLevelInfo { level: number; name: string; summary: string }
export interface CodexRuns { today: number; running: number; panes: { pane: string; label: string; running: number }[] }
export interface CodexRun { model: string; effort: string; fast: boolean }
export interface CodexModel { slug: string; name: string; efforts: string[]; fast: boolean }
export interface CodexLevelState {
  levels: CodexLevelInfo[]; global: number; level: number; source: 'global' | 'project'
  project: { override: boolean; level: number } | null; runs: CodexRuns
  run: CodexRun; models: CodexModel[]
}
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']

function useCodexLevel(api: Api, project?: string) {
  const path = project ? `codex-level?project=${encodeURIComponent(project)}` : 'codex-level'
  const fetcher = useCallback((signal: AbortSignal) => api<CodexLevelState>(path, undefined, signal), [api, path])
  const { data, error: loadError, refresh } = usePolling(fetcher, 5000)
  const [pending, setPending] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  async function save(body: Record<string, unknown>) {
    setPending(body); setError('')
    try { await api<CodexLevelState>('codex-level', body); await refresh() }
    catch (failure) { setError(errorText(failure)) }
    finally { setPending(null) }
  }
  return { data, error: error || loadError, pending, save }
}

function Stepper({ name, legend, levels, value, disabled, onChange }: { name: string; legend: string; levels: CodexLevelInfo[]; value: number; disabled?: boolean; onChange: (level: number) => void }) {
  const current = levels[value]
  return <fieldset className="choice codex-level" disabled={disabled}>
    <legend>{legend}</legend>
    <div className="segmented">{levels.map(item => <label key={item.level} title={item.name}>
      <input type="radio" name={name} value={item.level} checked={value === item.level} onChange={() => onChange(item.level)} /><span>{item.level}</span>
    </label>)}</div>
    {current && <p className="setting-note codex-level-note"><strong>{current.name}.</strong> {current.summary}</p>}
  </fieldset>
}

function Runs({ runs, panes }: { runs: CodexRuns; panes?: string[] }) {
  const shown = panes ? runs.panes.filter(item => panes.includes(item.pane)) : runs.panes
  const here = shown.reduce((sum, item) => sum + item.running, 0)
  return <div className="switch-row codex-runs-summary" role="status">
    <span><strong>Codex runs</strong><small>
      {panes ? `${here} running in this session · ${runs.running} running in all · ${runs.today} today` : `${runs.running} running now · ${runs.today} today`}
      {!panes && shown.length > 0 && <> · {shown.map(item => `${item.label}: ${item.running}`).join(', ')}</>}
    </small></span>
  </div>
}

/** Model, effort and fast mode for each Codex run. "auto" lets Claude choose for each task. */
function RunOptions({ run, models, disabled, onChange }: { run: CodexRun; models: CodexModel[]; disabled: boolean; onChange: (run: CodexRun) => void }) {
  const model = models.find(item => item.slug === run.model)
  const efforts = model ? model.efforts : EFFORTS
  const slugs = models.some(item => item.slug === run.model) || run.model === 'auto' ? models : [...models, { slug: run.model, name: run.model, efforts: EFFORTS, fast: true }]
  function pick(next: Partial<CodexRun>) {
    const merged = { ...run, ...next }
    const allowed = models.find(item => item.slug === merged.model)?.efforts
    if (allowed && merged.effort !== 'auto' && !allowed.includes(merged.effort)) merged.effort = 'auto'
    onChange(merged)
  }
  return <fieldset className="choice codex-run-options" disabled={disabled}>
    <legend>How Claude runs Codex</legend>
    <div className="project-size-row">
      <label htmlFor="codex-run-model">Model</label>
      <SelectMenu id="codex-run-model" value={run.model} onChange={model => pick({ model })} options={[{ value: 'auto', label: 'Auto: Claude chooses' }, ...slugs.map(item => ({ value: item.slug, label: item.name }))]} />
    </div>
    <div className="project-size-row">
      <label htmlFor="codex-run-effort">Effort</label>
      <SelectMenu id="codex-run-effort" value={run.effort} onChange={effort => pick({ effort })} options={[{ value: 'auto', label: 'Auto: Claude chooses' }, ...efforts.map(effort => ({ value: effort, label: effort }))]} />
    </div>
    <label className="switch-row"><span><strong>Fast mode</strong><small>{run.fast ? 'Faster Codex runs. They use more of your Codex limit.' : 'Codex runs at normal speed.'}</small></span>
      <input type="checkbox" role="switch" checked={run.fast} disabled={model ? !model.fast : false} onChange={event => pick({ fast: event.target.checked })} /></label>
  </fieldset>
}

/** Settings › System: the global level. */
export function GlobalCodexLevel({ api }: { api: Api }) {
  const { data, error, pending, save } = useCodexLevel(api)
  const value = typeof pending?.level === 'number' ? pending.level : data?.global
  return <section className="settings-group" aria-labelledby="settings-codex"><h3 id="settings-codex">Codex helper</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading Codex helper"><StepperBone /><NoteBone lines={2} /><RunOptionsBone /><SwitchBone control="none" from={1} /></SettingSkeleton> : <>
      <Stepper name="codex-level-global" legend="Level" levels={data.levels} value={value ?? 0} disabled={!!pending} onChange={level => void save({ level })} />
      <p className="setting-note">For every Claude session. Claude gets the rules with each prompt. A project can use its own level. Below level 5, UI design and prototyping stay with Claude.</p>
      <RunOptions run={(pending?.run as CodexRun | undefined) ?? data.run} models={data.models} disabled={!!pending} onChange={run => void save({ run })} />
      <Runs runs={data.runs} />
    </>}
  </section>
}

/** The Project sheet: an override that beats the global level while it is on. */
export function ProjectCodexLevel({ api, project, panes }: { api: Api; project: string; panes: string[] }) {
  const { data, error, pending, save } = useCodexLevel(api, project)
  const override = typeof pending?.override === 'boolean' ? pending.override : !!data?.project?.override
  const level = typeof pending?.level === 'number' ? pending.level : data?.project?.level ?? data?.global ?? 0
  return <section className="settings-group" aria-labelledby="project-codex"><h3 id="project-codex">Codex level</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading Codex level"><SwitchBone /><StepperBone /><SwitchBone control="none" from={1} /></SettingSkeleton> : <>
      <label className="switch-row"><span><strong>Use a project level</strong><small>{override ? `Claude here uses level ${level}, not the global level ${data.global}.` : `Claude here uses the global level ${data.global}.`}</small></span>
        <input type="checkbox" role="switch" checked={override} disabled={!!pending} onChange={event => void save({ project, override: event.target.checked, level })} /></label>
      <Stepper name="codex-level-project" legend="Level" levels={data.levels} value={level} disabled={!!pending || !override} onChange={next => void save({ project, override: true, level: next })} />
      <Runs runs={data.runs} panes={panes} />
    </>}
  </section>
}
