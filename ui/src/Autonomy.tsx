import { useCallback, useState } from 'react'
import { Loading } from './Skeleton'
import { SelectMenu } from './SelectMenu'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import { withApproval } from './approval'
import { SavedAnswers } from './AutonomySetup'
import type { SavedAnswer } from './AutonomySetup'

// Autonomy level and reviewer choice (server: autonomy.py). A Claude Code UserPromptSubmit hook gives each prompt the
// active level's rules and the reviewer rules. The person sets both; agents never change them.
export interface AutonomyLevelInfo { level: number; name: string; summary: string }
export type Tier = 'critical' | 'normal' | 'light'
export interface Reviewer { agent: 'auto' | 'claude' | 'codex' | 'none'; model: string; effort: string }
export interface Advisor { agent: 'claude' | 'codex' | 'none'; model: string; effort: string; second: boolean }
export interface AutonomyState {
  levels: AutonomyLevelInfo[]; global: number; level: number; source: 'global' | 'project'
  project: { override: boolean; level: number } | null
  reviewers: Record<Tier, Reviewer>; tiers: Tier[]
  models: { claude: (string | { slug: string; name?: string })[]; codex: { slug: string; name: string; efforts: string[] }[] }
  efforts: string[]
  decisions?: SavedAnswer[]
  advisor?: Advisor
  latest?: { claude: string; codex: string | null }
}

const TIERS: Record<Tier, { title: string; note: string }> = {
  critical: { title: 'Critical tasks', note: 'Security, auth, payments, data, migrations, deploys, releases, big changes.' },
  normal: { title: 'Normal tasks', note: 'Features and bug fixes.' },
  light: { title: 'Light tasks', note: 'Docs, copy, styles and small fixes.' },
}
const DEFAULTS: Record<Tier, string> = {
  critical: 'Default: the other agent, strongest model, high effort.',
  normal: 'Default: the other agent, default model, medium effort.',
  light: 'Default: no reviewer tab. The agent reviews its own change.',
}

function useAutonomy(api: Api, project?: string) {
  const path = project ? `autonomy?project=${encodeURIComponent(project)}` : 'autonomy'
  const fetcher = useCallback((signal: AbortSignal) => api<AutonomyState>(path, undefined, signal), [api, path])
  const { data, error: loadError, refresh } = usePolling(fetcher, 10000)
  const [pending, setPending] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  async function save(body: Record<string, unknown>) {
    setPending(body); setError('')
    // Raising a level gives agents more power, so the server asks for a passkey approval; lowering it does not.
    try { await withApproval<AutonomyState>(api, 'autonomy', 'level-raise', body); await refresh() }
    catch (failure) { setError(errorText(failure)) }
    finally { setPending(null) }
  }
  return { data, error: error || loadError, pending, save, refresh }
}

/** One named row per level: the number, the name and what the agent does. */
function LevelList({ name, levels, value, disabled, onChange }: { name: string; levels: AutonomyLevelInfo[]; value: number; disabled?: boolean; onChange: (level: number) => void }) {
  return <fieldset className="autonomy-levels" disabled={disabled}>
    <legend className="visually-hidden">Autonomy level</legend>
    {levels.map(item => <label key={item.level} className="autonomy-level">
      <input type="radio" name={name} value={item.level} checked={value === item.level} onChange={() => onChange(item.level)} />
      <span className="autonomy-level-number" aria-hidden="true">{item.level}</span>
      <span className="autonomy-level-text"><strong>{item.name}</strong><small>{item.summary}</small></span>
    </label>)}
  </fieldset>
}

/** "Latest" follows provider releases: Claude Code's Fable alias, or the first model in Codex's own list. */
function latestOption(data: AutonomyState, agent: Reviewer['agent'] | Advisor['agent']) {
  if (agent === 'claude') return [{ value: 'latest', label: 'Latest Fable' }]
  if (agent === 'codex') {
    const name = data.latest?.codex ?? data.models.codex[0]?.name
    return [{ value: 'latest', label: name ? `Latest: ${name}` : 'Latest' }]
  }
  return []
}

function modelOptions(data: AutonomyState, agent: Reviewer['agent'] | Advisor['agent']) {
  if (agent === 'claude') return data.models.claude.map(item => typeof item === 'string' ? { value: item, label: item } : { value: item.slug, label: item.name || item.slug })
  if (agent === 'codex') return data.models.codex.map(item => ({ value: item.slug, label: item.name }))
  return []
}

function effortOptions(data: AutonomyState, agent: Reviewer['agent'] | Advisor['agent'], model: string) {
  if (agent !== 'codex') return data.efforts
  const row = model === 'latest' || model === 'auto' ? data.models.codex[0] : data.models.codex.find(item => item.slug === model)
  return row?.efforts ?? data.efforts
}

function ReviewerRow({ tier, value, data, disabled, onChange }: { tier: Tier; value: Reviewer; data: AutonomyState; disabled: boolean; onChange: (next: Reviewer) => void }) {
  const id = `reviewer-${tier}`
  const models = [...latestOption(data, value.agent), ...modelOptions(data, value.agent)]
  const efforts = effortOptions(data, value.agent, value.model)
  const fixed = value.agent === 'auto' || value.agent === 'none'
  function pick(next: Partial<Reviewer>) {
    const merged = { ...value, ...next }
    if (merged.agent !== value.agent) merged.model = 'auto'
    if (merged.agent === 'none') merged.effort = 'auto'
    if (!effortOptions(data, merged.agent, merged.model).includes(merged.effort)) merged.effort = 'auto'
    onChange(merged)
  }
  const isDefault = value.agent === 'auto' && value.model === 'auto' && value.effort === 'auto'
  return <fieldset className="choice reviewer-tier" disabled={disabled}>
    <legend>{TIERS[tier].title}</legend>
    <p className="setting-note">{TIERS[tier].note} {isDefault && DEFAULTS[tier]}</p>
    <div className="project-size-row">
      <label htmlFor={`${id}-agent`}>Reviewer</label>
      <SelectMenu id={`${id}-agent`} value={value.agent} onChange={agent => pick({ agent: agent as Reviewer['agent'] })} options={[{ value: 'auto', label: 'Auto: the agent chooses' }, { value: 'claude', label: 'Claude' }, { value: 'codex', label: 'Codex' }, { value: 'none', label: 'No reviewer' }]} />
    </div>
    <div className="project-size-row">
      <label htmlFor={`${id}-model`}>Model</label>
      <SelectMenu id={`${id}-model`} disabled={fixed} value={value.model} onChange={model => pick({ model })} options={[{ value: 'auto', label: fixed ? 'Auto' : 'Auto: the agent chooses' }, ...models]} />
    </div>
    <div className="project-size-row">
      <label htmlFor={`${id}-effort`}>Effort</label>
      <SelectMenu id={`${id}-effort`} disabled={value.agent === 'none'} value={value.effort} onChange={effort => pick({ effort })} options={[{ value: 'auto', label: 'Auto: the agent chooses' }, ...efforts.map(effort => ({ value: effort, label: effort }))]} />
    </div>
  </fieldset>
}

/** Settings › Agents: the global autonomy level. */
export function GlobalAutonomy({ api }: { api: Api }) {
  const { data, error, pending, save } = useAutonomy(api)
  const value = typeof pending?.level === 'number' ? pending.level : data?.global
  return <section className="settings-group" aria-labelledby="settings-autonomy"><h3 id="settings-autonomy">Autonomy level</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <Loading className="setting-note" /> : <>
      <p className="setting-note">How often agents stop to ask you. Agents get the rules with each prompt. At every level they ask before they delete data, force-push, spend money or release.</p>
      <LevelList name="autonomy-global" levels={data.levels} value={value ?? 2} disabled={!!pending} onChange={level => void save({ level })} />
    </>}
  </section>
}

/** Settings › Agents: who reviews the work, per task risk. Auto keeps the built-in rules. */
export function ReviewerSettings({ api }: { api: Api }) {
  const { data, error, pending, save } = useAutonomy(api)
  const reviewers = (pending?.reviewers as Record<Tier, Reviewer> | undefined) ?? data?.reviewers
  return <section className="settings-group" aria-labelledby="settings-reviewers"><h3 id="settings-reviewers">Reviewers</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data || !reviewers ? error ? <p className="setting-note" role="alert">{error}</p> : <Loading className="setting-note" /> : <>
      <p className="setting-note">Agents rate each task as critical, normal or light, then choose a reviewer. Set a tier here to override their choice.</p>
      {data.tiers.map(tier => <ReviewerRow key={tier} tier={tier} value={reviewers[tier]} data={data} disabled={!!pending} onChange={next => void save({ reviewers: { ...reviewers, [tier]: next } })} />)}
    </>}
  </section>
}

/** Settings › Agents: the stronger model that agents at levels 3 to 5 ask before they ask you. */
export function AdvisorSettings({ api }: { api: Api }) {
  const { data, error, pending, save } = useAutonomy(api)
  const advisor = (pending?.advisor as Advisor | undefined) ?? data?.advisor
  function pick(next: Partial<Advisor>) {
    if (!data || !advisor) return
    const merged = { ...advisor, ...next }
    if (merged.agent !== advisor.agent) merged.model = 'latest'
    if (merged.agent === 'none') Object.assign(merged, { model: 'latest', effort: 'auto', second: false })
    else if (advisor.agent === 'none') Object.assign(merged, { effort: 'high', second: true })
    if (!['auto', ...effortOptions(data, merged.agent, merged.model)].includes(merged.effort)) merged.effort = 'auto'
    void save({ advisor: merged })
  }
  const off = advisor?.agent === 'none'
  const other = advisor?.agent === 'codex' ? 'Claude' : 'Codex'
  return <section className="settings-group" aria-labelledby="settings-advisor"><h3 id="settings-advisor">Advisor</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data || !advisor ? error ? <p className="setting-note" role="alert">{error}</p> : <Loading className="setting-note" /> : <>
      <p className="setting-note">At levels 3 to 5, agents check the code, notes and docs first. When that does not settle a decision, they ask the advisor in an Advisor tab. They ask you only when the advisor is unsure or the question needs you. A Latest model moves to each new model when the provider releases it.</p>
      <fieldset className="choice reviewer-tier" disabled={!!pending} aria-labelledby="settings-advisor">
        <div className="project-size-row">
          <label htmlFor="advisor-agent">Advisor</label>
          <SelectMenu id="advisor-agent" value={advisor.agent} onChange={agent => pick({ agent: agent as Advisor['agent'] })} options={[{ value: 'claude', label: 'Claude' }, { value: 'codex', label: 'Codex' }, { value: 'none', label: 'Off: agents ask you' }]} />
        </div>
        <div className="project-size-row">
          <label htmlFor="advisor-model">Model</label>
          <SelectMenu id="advisor-model" disabled={off} value={advisor.model} onChange={model => pick({ model })} options={off ? [{ value: 'latest', label: 'None' }] : [...latestOption(data, advisor.agent), ...modelOptions(data, advisor.agent)]} />
        </div>
        <div className="project-size-row">
          <label htmlFor="advisor-effort">Effort</label>
          <SelectMenu id="advisor-effort" disabled={off} value={advisor.effort} onChange={effort => pick({ effort })} options={[{ value: 'auto', label: off ? 'None' : 'Auto: the model default' }, ...effortOptions(data, advisor.agent, advisor.model).map(effort => ({ value: effort, label: effort }))]} />
        </div>
        <label className="switch-row"><span><strong>Second opinion on critical decisions</strong><small>{off ? 'Turn on an advisor first.' : `${other}, latest model, also answers security, data, money, release and architecture questions. Agents act only when both agree.`}</small></span>
          <input type="checkbox" role="switch" checked={!off && advisor.second} disabled={off || !!pending} onChange={event => pick({ second: event.target.checked })} /></label>
      </fieldset>
    </>}
  </section>
}

/** The Project sheet: an override that beats the global level while it is on. */
export function ProjectAutonomy({ api, project }: { api: Api; project: string }) {
  const { data, error, pending, save, refresh } = useAutonomy(api, project)
  const override = typeof pending?.override === 'boolean' ? pending.override : !!data?.project?.override
  const level = typeof pending?.level === 'number' ? pending.level : data?.project?.level ?? data?.global ?? 2
  const named = (n: number) => data?.levels[n] ? `${n} (${data.levels[n].name})` : `${n}`
  return <><section className="settings-group" aria-labelledby="project-autonomy"><h3 id="project-autonomy">Autonomy level</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <Loading className="setting-note" /> : <>
      <label className="switch-row"><span><strong>Use a project level</strong><small>{override ? `Agents here use level ${named(level)}, not the global level ${named(data.global)}.` : `Agents here use the global level ${named(data.global)}.`}</small></span>
        <input type="checkbox" role="switch" checked={override} disabled={!!pending} onChange={event => void save({ project, override: event.target.checked, level })} /></label>
      <LevelList name="autonomy-project" levels={data.levels} value={level} disabled={!!pending || !override} onChange={next => void save({ project, override: true, level: next })} />
    </>}
  </section>
  {data && <SavedAnswers api={api} project={project} answers={data.decisions ?? []} onChange={() => void refresh()} />}</>
}
