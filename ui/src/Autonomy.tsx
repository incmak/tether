import { useCallback, useState } from 'react'
import { LevelBones, NoteBone, SettingSkeleton, SwitchBone } from './SettingBones'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'
import { withApproval } from './approval'
import { SavedAnswers } from './AutonomySetup'
import { ProjectLessons } from './Learning'
import type { SavedAnswer } from './AutonomySetup'

// Autonomy level and reviewer choice (server: autonomy.py). A Claude Code UserPromptSubmit hook gives each prompt the
// active level's rules and the reviewer rules. The person sets both; agents never change them.
export interface AutonomyLevelInfo { level: number; name: string; summary: string }
export type Tier = 'critical' | 'normal' | 'light'
export interface Reviewer { agent: 'auto' | 'claude' | 'codex' | 'none'; model: string; effort: string }
export interface HelperChoice { agent: 'claude' | 'codex' | 'none'; model: string; effort: string }
export interface Advisor { main: HelperChoice; second: HelperChoice }
export interface CatalogModel {
  slug: string; name: string; efforts: string[]
  // Claude rows only. available: false = this account cannot use it; null = Tether still checks.
  family?: string; available?: boolean | null; access?: 'ok' | 'no-access' | 'checking' | 'error'
}
export interface Lesson {
  id: string; text: string; kind: 'avoid' | 'keep'; source: 'user' | 'reviewer' | 'agent'; agent: string
  hits: number; created: number; last_hit: number; proposed_global?: boolean; stuck?: boolean
  // trial: seen once, weighs less until it repeats in another tab or a day later, or the user keeps it.
  status?: 'trial' | 'confirmed'; quote?: string; tab?: string
  disputes?: { pane: string; reason: string; at: number }[]; disputed?: boolean
}
export interface ReviewSummary { due: boolean; reason: 'disputed' | 'new' | 'time' | null; new: number; disputed: number; total: number }
export interface Learning { enabled: boolean; global: Lesson[]; lessons?: Lesson[]; path?: string; review?: ReviewSummary | null }
export interface ProviderCatalog {
  latest: { launch: string; slug: string; name: string } | null
  models: CatalogModel[]
  new_families?: string[]
  changed?: { from: string; to: string; at: number } | null
}
export interface AutonomyState {
  levels: AutonomyLevelInfo[]; global: number; level: number; source: 'global' | 'project'
  project: { override: boolean; level: number } | null
  reviewers: Record<Tier, Reviewer>; tiers: Tier[]
  models: { claude: (string | { slug: string; name?: string })[]; codex: { slug: string; name: string; efforts: string[] }[] }
  efforts: string[]
  decisions?: SavedAnswer[]
  advisor?: Advisor
  catalog?: { claude: ProviderCatalog; codex: ProviderCatalog }
  learning?: Learning
}

export function useAutonomy(api: Api, project?: string) {
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

/** Settings › Agents: the global autonomy level. */
export function GlobalAutonomy({ api }: { api: Api }) {
  const { data, error, pending, save } = useAutonomy(api)
  const value = typeof pending?.level === 'number' ? pending.level : data?.global
  return <section className="settings-group" aria-labelledby="settings-autonomy"><h3 id="settings-autonomy">Autonomy level</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading autonomy level"><NoteBone lines={3} /><LevelBones /></SettingSkeleton> : <>
      <p className="setting-note">How often agents stop to ask you. Agents get the rules with each prompt. At every level they ask before they delete data, force-push, spend money or release.</p>
      <LevelList name="autonomy-global" levels={data.levels} value={value ?? 2} disabled={!!pending} onChange={level => void save({ level })} />
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
    {!data ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading autonomy level"><SwitchBone /><LevelBones /></SettingSkeleton> : <>
      <label className="switch-row"><span><strong>Use a project level</strong><small>{override ? `Agents here use level ${named(level)}, not the global level ${named(data.global)}.` : `Agents here use the global level ${named(data.global)}.`}</small></span>
        <input type="checkbox" role="switch" checked={override} disabled={!!pending} onChange={event => void save({ project, override: event.target.checked, level })} /></label>
      <LevelList name="autonomy-project" levels={data.levels} value={level} disabled={!!pending || !override} onChange={next => void save({ project, override: true, level: next })} />
    </>}
  </section>
  {data && <SavedAnswers api={api} project={project} answers={data.decisions ?? []} onChange={() => void refresh()} />}
  {data?.learning && <ProjectLessons api={api} project={project} learning={data.learning} onChange={() => void refresh()} />}</>
}
