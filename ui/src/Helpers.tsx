import { useState } from 'react'
import { Check, ChevronRight, X } from 'lucide-react'
import { Bone } from './Skeleton'
import { NoteBone, RowBones, SettingSkeleton } from './SettingBones'
import type { Api } from './api'
import { useModal } from './hooks'
import { useAutonomy } from './Autonomy'
import type { Advisor, AutonomyState, CatalogModel, HelperChoice, ProviderCatalog, Reviewer, Tier } from './Autonomy'

// Settings › Agents › "Who helps your agents": the advisor, the second opinion and the three reviewer tiers
// (server: advisor.py, autonomy.py, model_catalog.py). Each row shows what runs now; a sheet changes it.
type Role = 'advisor' | 'second' | Tier
type Agent = Reviewer['agent']

const ROLES: Record<Role, { title: string; purpose: string; agents: Agent[] }> = {
  advisor: { title: 'Advisor', agents: ['claude', 'codex', 'none'],
    purpose: 'At levels 3 to 5, agents ask the advisor when the code, notes and docs do not settle a decision. They ask you only when the advisor is unsure or the question needs you.' },
  second: { title: 'Second opinion', agents: ['claude', 'codex', 'none'],
    purpose: 'Also answers security, data, money, release and architecture questions. Agents act only when both advisors agree.' },
  critical: { title: 'Critical tasks', agents: ['auto', 'claude', 'codex', 'none'],
    purpose: 'Reviews security, auth, payments, data, migrations, deploys, releases and big changes.' },
  normal: { title: 'Normal tasks', agents: ['auto', 'claude', 'codex', 'none'], purpose: 'Reviews features and bug fixes.' },
  light: { title: 'Light tasks', agents: ['auto', 'claude', 'codex', 'none'], purpose: 'Reviews docs, copy, styles and small fixes.' },
}
const AUTO: Record<Tier, string> = {
  critical: 'The other agent, its strongest model, high effort.',
  normal: 'The other agent, its default model, medium effort.',
  light: 'No reviewer tab. The agent checks its own change.',
}
const AUTO_SUMMARY: Record<Tier, string> = {
  critical: 'Auto: the other agent, strongest model, high effort',
  normal: 'Auto: the other agent, default model, medium effort',
  light: 'Auto: no reviewer. The agent checks its own change',
}
const AGENT_NAME: Record<Agent, string> = { auto: 'Auto', claude: 'Claude', codex: 'Codex', none: 'Off' }
const PROVIDER: Record<'claude' | 'codex', string> = { claude: 'Anthropic', codex: 'OpenAI' }
const ACCESS: Record<NonNullable<CatalogModel['access']>, string> = {
  ok: 'Stays on this version.',
  'no-access': 'Your account has no access. Anthropic gives it to verified organizations.',
  checking: 'Tether is checking your access.',
  error: 'Tether could not check your access. It tries again later.',
}

/** The provider catalog, or one built from the older `models` field when the server has no catalog. */
function catalogFor(data: AutonomyState, agent: 'claude' | 'codex'): ProviderCatalog {
  if (data.catalog) return data.catalog[agent]
  const models: CatalogModel[] = agent === 'codex' ? data.models.codex
    : data.models.claude.map(item => typeof item === 'string' ? { slug: item, name: item, efforts: data.efforts } : { slug: item.slug, name: item.name || item.slug, efforts: data.efforts })
  return { latest: models[0] ? { launch: models[0].slug, slug: models[0].slug, name: models[0].name } : null, models }
}

function efforts(data: AutonomyState, agent: Agent, model: string): string[] {
  if (agent !== 'claude' && agent !== 'codex') return []
  const catalog = catalogFor(data, agent)
  const row = model === 'latest' || model === 'auto' ? catalog.models.find(item => item.slug === catalog.latest?.slug) ?? catalog.models[0]
    : catalog.models.find(item => item.slug === model)
  return row?.efforts ?? data.efforts
}

function listNames(names: string[]) {
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

export function effortLabel(effort: string) {
  return effort === 'xhigh' ? 'Extra high' : effort === 'auto' ? 'Auto' : effort[0].toUpperCase() + effort.slice(1)
}

function modelName(data: AutonomyState, agent: Agent, model: string) {
  if (agent !== 'claude' && agent !== 'codex') return ''
  const catalog = catalogFor(data, agent)
  if (model === 'latest') return catalog.latest ? `Latest (${catalog.latest.name})` : 'Latest'
  if (model === 'auto') return 'Auto model'
  return catalog.models.find(item => item.slug === model)?.name ?? model
}

function summary(data: AutonomyState, role: Role, value: HelperChoice | Reviewer, disabled = false) {
  if (disabled) return 'Off while the advisor is off'
  if (value.agent === 'none') return role === 'advisor' ? 'Off. Agents ask you' : role === 'second' ? 'Off' : 'No reviewer'
  if (value.agent === 'auto') return role in AUTO_SUMMARY ? AUTO_SUMMARY[role as Tier] : 'Auto'
  const effort = value.effort === 'auto' ? 'auto effort' : `${effortLabel(value.effort).toLowerCase()} effort`
  return `${AGENT_NAME[value.agent]} · ${modelName(data, value.agent, value.model)} · ${effort}`
}

/** Settings › Agents: who helps the agents before they ask you, and who checks their work after. */
export function HelperSettings({ api }: { api: Api }) {
  const { data, error, pending, save } = useAutonomy(api)
  const [open, setOpen] = useState<Role | null>(null)
  const advisor = (pending?.advisor as Advisor | undefined) ?? data?.advisor
  const reviewers = (pending?.reviewers as Record<Tier, Reviewer> | undefined) ?? data?.reviewers
  function value(role: Role): HelperChoice | Reviewer {
    return role === 'advisor' ? advisor!.main : role === 'second' ? advisor!.second : reviewers![role]
  }
  function change(role: Role, next: HelperChoice | Reviewer) {
    if (!advisor || !reviewers) return
    if (role === 'advisor') {
      const main = next as HelperChoice
      // No advisor means no second opinion; turning the advisor back on restores the default second opinion.
      const second = main.agent === 'none' ? { agent: 'none' as const, model: 'latest', effort: 'auto' }
        : advisor.main.agent === 'none' && advisor.second.agent === 'none' ? { agent: main.agent === 'claude' ? 'codex' as const : 'claude' as const, model: 'latest', effort: 'high' } : advisor.second
      void save({ advisor: { main, second } })
    } else if (role === 'second') void save({ advisor: { ...advisor, second: next } })
    else void save({ reviewers: { ...reviewers, [role]: next } })
  }
  const changed = data && (['claude', 'codex'] as const).map(agent => ({ agent, catalog: catalogFor(data, agent) }))
  const blocked = data ? catalogFor(data, 'claude').models.filter(item => item.available === false) : []
  const row = (role: Role) => {
    const disabled = role === 'second' && advisor?.main.agent === 'none'
    return <li key={role}><button type="button" id={`helper-row-${role}`} className="helper-row" disabled={disabled} aria-haspopup="dialog" onClick={() => setOpen(role)}>
      <span><strong>{ROLES[role].title}</strong><small>{summary(data!, role, value(role), disabled)}</small></span><ChevronRight size={18} aria-hidden="true" /></button></li>
  }
  return <section className="settings-group helper-settings" aria-labelledby="settings-helpers"><h3 id="settings-helpers">Who helps your agents</h3>
    {data && error && <p className="error" role="alert">{error}</p>}
    {!data || !advisor || !reviewers ? error ? <p className="setting-note" role="alert">{error}</p> : <SettingSkeleton label="Loading helpers"><NoteBone lines={1} /><h4 className="helper-group-title"><Bone w="9em" /></h4><RowBones count={2} className="helper-rows" /><h4 className="helper-group-title"><Bone w="8em" /></h4><RowBones count={3} className="helper-rows" /><NoteBone lines={2} className="helper-latest" /></SettingSkeleton> : <>
      <p className="setting-note">Agents ask an advisor before they ask you. A reviewer checks their work when they finish.</p>
      <h4 className="helper-group-title" id="helpers-before">Before they ask you</h4>
      <ul className="helper-rows" aria-labelledby="helpers-before">{(['advisor', 'second'] as const).map(row)}</ul>
      <h4 className="helper-group-title" id="helpers-after">After they finish</h4>
      <ul className="helper-rows" aria-labelledby="helpers-after">{data.tiers.map(row)}</ul>
      <p className="setting-note helper-latest">Latest uses the newest model of each provider: now {changed!.map(({ agent, catalog }) => `${catalog.latest?.name ?? 'none found'} (${AGENT_NAME[agent]})`).join(' and ')}. It moves when {PROVIDER.claude} or {PROVIDER.codex} releases a new model.</p>
      {changed!.filter(({ catalog }) => catalog.changed).map(({ agent, catalog }) => <p key={agent} className="setting-note helper-news"><strong>New model.</strong> {catalog.changed!.to} replaced {catalog.changed!.from} for {AGENT_NAME[agent]}. Latest uses it now.</p>)}
      {changed!.filter(({ catalog }) => catalog.new_families?.length).map(({ agent, catalog }) => <p key={`${agent}-family`} className="setting-note helper-news">{agent === 'claude' ? 'Claude Code' : 'Codex'} lists a new model family: {catalog.new_families!.map(name => name[0].toUpperCase() + name.slice(1)).join(', ')}. Tether checks your access. Latest moves to it when your account can use it and it ranks higher.</p>)}
      {blocked.length > 0 && <div className="helper-access"><p className="setting-note">{listNames(blocked.map(item => item.name))}: your account has no access. Anthropic gives {blocked.length > 1 ? 'these models' : 'this model'} to verified organizations only. Latest switches when access starts.</p>
        <button type="button" disabled={!!pending} onClick={() => void save({ catalog: 'recheck' })}>Check access again</button></div>}
      {open && <HelperSheet role={open} data={data} value={value(open)} busy={!!pending} onChange={next => change(open, next)} onClose={() => { const role = open; setOpen(null); requestAnimationFrame(() => document.getElementById(`helper-row-${role}`)?.focus()) }} />}
    </>}
  </section>
}

function HelperSheet({ role, data, value, busy, onChange, onClose }: { role: Role; data: AutonomyState; value: HelperChoice | Reviewer; busy: boolean; onChange: (next: HelperChoice | Reviewer) => void; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const meta = ROLES[role]
  const reviewer = role !== 'advisor' && role !== 'second'
  const provider = value.agent === 'claude' || value.agent === 'codex' ? value.agent : null
  const catalog = provider && catalogFor(data, provider)
  const options = efforts(data, value.agent, value.model)
  function pick(next: Partial<Reviewer>) {
    const merged = { ...value, ...next } as Reviewer
    if (merged.agent !== value.agent) {
      merged.model = merged.agent === 'auto' || merged.agent === 'none' ? (reviewer ? 'auto' : 'latest') : 'latest'
      merged.effort = merged.agent === 'none' || merged.agent === 'auto' ? 'auto' : value.agent === 'none' || value.agent === 'auto' ? (role === 'normal' ? 'medium' : 'high') : value.effort
    }
    if (merged.effort !== 'auto' && !efforts(data, merged.agent, merged.model).includes(merged.effort)) merged.effort = 'auto'
    onChange(merged)
  }
  const id = `helper-${role}`
  const pinned = catalog ? catalog.models : []
  const unknown = provider && value.model !== 'latest' && value.model !== 'auto' && !pinned.some(item => item.slug === value.model)
  const note = value.agent === 'none' ? (role === 'advisor' ? 'Agents ask you when the evidence does not settle a decision.' : role === 'second' ? 'Agents act on one advisor answer.' : 'No reviewer tab. The agent checks its own change.')
    : value.agent === 'auto' && reviewer ? `Agents choose. ${AUTO[role as Tier]}` : ''
  return <dialog {...modal} className="sheet helper-sheet" aria-labelledby={`${id}-title`}>
    <div className="sheet-heading"><h2 id={`${id}-title`}>{meta.title}</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <p className="helper-purpose">{meta.purpose}</p>
    <fieldset className="choice helper-field" disabled={busy}><legend>{reviewer ? 'Reviewer' : 'Agent'}</legend>
      <div className="segmented">{meta.agents.map(agent => <label key={agent}><input type="radio" name={`${id}-agent`} value={agent} checked={value.agent === agent} onChange={() => pick({ agent })} /><span>{AGENT_NAME[agent]}</span></label>)}</div>
      {note && <p className="setting-note">{note}</p>}
    </fieldset>
    {catalog && <fieldset className="choice helper-field" disabled={busy}><legend>Model</legend>
      <div className="helper-options">
        {reviewer && <ModelOption name={`${id}-model`} value="auto" checked={value.model === 'auto'} title="Auto" detail="The agent chooses for each task." onPick={model => pick({ model })} />}
        <ModelOption name={`${id}-model`} value="latest" checked={value.model === 'latest'} title={catalog.latest ? `Latest: ${catalog.latest.name}` : 'Latest'} detail={`Moves to each new ${PROVIDER[provider!]} model.`} badge="Recommended" onPick={model => pick({ model })} />
      </div>
      <p className="helper-subhead">Or keep one version</p>
      <div className="helper-options">
        {pinned.map(item => <ModelOption key={item.slug} name={`${id}-model`} value={item.slug} checked={value.model === item.slug} title={item.name} detail={ACCESS[item.access ?? 'ok']} disabled={(item.available === false || item.access === 'checking') && value.model !== item.slug} onPick={model => pick({ model })} />)}
        {unknown && <ModelOption name={`${id}-model`} value={value.model} checked title={value.model} detail="Saved earlier. Not in the current list." onPick={model => pick({ model })} />}
      </div>
    </fieldset>}
    {provider && options.length > 0 && <fieldset className="choice helper-field" disabled={busy}><legend>Effort</legend>
      <div className="segmented effort-levels">{['auto', ...options].map(effort => <label key={effort}><input type="radio" name={`${id}-effort`} value={effort} checked={value.effort === effort} onChange={() => pick({ effort })} /><span>{effortLabel(effort)}</span></label>)}</div>
      <p className="setting-note">Higher effort answers better and takes longer. Auto uses the model default.</p>
    </fieldset>}
    <p className="visually-hidden" role="status">{busy ? 'Saving' : 'Saved'}</p>
    <button type="button" className="primary create-button" onClick={onClose}>Done</button>
  </dialog>
}

function ModelOption({ name, value, checked, title, detail, badge, disabled, onPick }: { name: string; value: string; checked: boolean; title: string; detail: string; badge?: string; disabled?: boolean; onPick: (model: string) => void }) {
  return <label className="helper-option"><input type="radio" name={name} value={value} checked={checked} disabled={disabled} onChange={() => onPick(value)} />
    <span><strong>{title}{badge && <em>{badge}</em>}</strong><small>{detail}</small></span>{checked && <Check size={18} aria-hidden="true" />}</label>
}
