import { useEffect, useState } from 'react'
import { errorText } from './api'
import type { Api, CodexProvider, FoundrySpend } from './api'
import { useHost } from './host'
import { fmtTime, useTetherZone } from './zone'

// Which provider new Codex runs use. Tether panes start with `codex -p foundry` for Azure Foundry; the server also sets the default
// provider in ~/.codex/config.toml, so Claude Code lanes and scripts that run plain `codex` follow the same choice.
function useCodexProvider(api: Api) {
  const [data, setData] = useState<CodexProvider | null>(null)
  useEffect(() => { void api<CodexProvider>('codex-provider').then(setData, () => {}) }, [api])
  return [data, setData] as const
}

const LABELS = { plan: 'ChatGPT plan', foundry: 'Azure Foundry' } as const

// The switch in Plan usage. Foundry stays off until the server has its setup, but you can always go back to the plan.
export function CodexProviderSwitch({ api }: { api: Api }) {
  const host = useHost()
  const [data, setData] = useCodexProvider(api)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  if (!data) return null
  // The fieldset stays enabled while saving, so keyboard focus stays on the radio that was chosen.
  async function choose(provider: CodexProvider['provider']) {
    if (pending) return
    setPending(true); setError('')
    try { setData(await api<CodexProvider>('codex-provider', { provider })) } catch (failure) { setError(errorText(failure)) } finally { setPending(false) }
  }
  const foundry = data.provider === 'foundry'
  return <fieldset className="choice codex-provider" aria-busy={pending}>
    <legend>Provider for new Codex runs</legend>
    <div className="segmented">{(['plan', 'foundry'] as const).map(provider => <label key={provider}>
      <input type="radio" name="codex-provider" value={provider} checked={data.provider === provider}
        disabled={provider === 'foundry' && !data.ready && !foundry} onChange={() => void choose(provider)} />
      <span>{LABELS[provider]}</span>
    </label>)}</div>
    {foundry
      ? <p className="provider-warning" role="status">New Codex runs on {host} use Azure Foundry{data.model ? ` (${data.model})` : ''} and spend Azure credit. This includes Tether panes, Claude Code lanes and scripts. Running ones keep their provider.</p>
      : <p className="hint">New Codex runs on {host} use your ChatGPT plan, including Claude Code lanes. Running ones keep their provider.</p>}
    {data.everywhere === false && <p className="hint">Only Tether panes follow this choice right now. Claude Code lanes and scripts still use the other provider. <button type="button" className="link-button" disabled={pending} onClick={() => void choose(data.provider)}>Apply to all Codex runs</button></p>}
    {!data.ready && data.hint && <p className="hint">{data.hint}</p>}
    {data.ready && <SpendRow api={api} show={foundry} />}
    {error && <p className="error" role="alert">{error}</p>}
  </fieldset>
}

// " · Foundry" after "Codex" on a start button while new Codex panes use Azure Foundry. Nothing for the plan.
export function FoundryTag({ api }: { api: Api }) {
  const [data] = useCodexProvider(api)
  return data?.provider === 'foundry' ? <span className="provider-tag"> · Foundry</span> : null
}

const money = (usd: number, currency = 'USD') => new Intl.NumberFormat(undefined, { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(usd)
const count = (window: FoundrySpend['estimate']['tokens'], key: 'requests' | 'tokens') => (['short', 'long'] as const)
  .reduce((sum, tier) => sum + (key === 'requests' ? window[tier].requests : window[tier].input + window[tier].cached + window[tier].cache_write + window[tier].output), 0)

// Foundry spend this month: a live estimate from the server Codex logs at list prices, and the real Azure bill when it is connected.
// It shows while Foundry is chosen, and on the plan too if Foundry was used this month.
function SpendRow({ api, show }: { api: Api; show: boolean }) {
  useTetherZone()
  const [spend, setSpend] = useState<FoundrySpend | null>(null)
  // Refresh every minute while open: the estimate grows as Codex runs write their logs.
  useEffect(() => {
    const load = () => { void api<FoundrySpend>('foundry-spend').then(setSpend, () => {}) }
    load()
    const timer = window.setInterval(load, 60_000)
    return () => window.clearInterval(timer)
  }, [api, show])
  if (!spend || (!show && !spend.estimate.usd)) return null
  const { estimate, bill, budget } = spend
  const used = bill.state === 'ok' ? bill.usd : estimate.usd
  const percent = used === null ? 0 : Math.min(100, Math.round(used / budget * 100))
  const today = count(estimate.today_tokens, 'requests')
  return <div className="foundry-spend">
    <div className="usage-row">
      <div><strong>Azure Foundry · {spend.month}</strong><span>{used === null ? 'Price unknown' : `${money(used)} of ${money(budget).replace(/\.00$/, '')}`}</span></div>
      <span className={`meter-bar ${percent >= 90 ? 'high' : percent >= 70 ? 'warn' : ''}`} aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>
      <small>Today (UTC): {estimate.today_usd === null ? '' : `${money(estimate.today_usd)} · `}{today} {today === 1 ? 'request' : 'requests'} · {count(estimate.today_tokens, 'tokens').toLocaleString()} tokens</small>
    </div>
    <dl className="spend-sources">
      <div><dt>Estimate</dt><dd>{estimate.usd === null ? 'Prices not available' : money(estimate.usd)} · {count(estimate.tokens, 'tokens').toLocaleString()} tokens in {estimate.sessions} {estimate.sessions === 1 ? 'chat' : 'chats'}. Runs on this server only, at Global Standard list prices.</dd></div>
      <div><dt>Azure bill</dt><dd>{bill.state === 'ok' ? `${money(bill.usd, bill.currency)} so far · checked ${fmtTime(bill.checked * 1000, { hour: 'numeric', minute: '2-digit' })}. Azure updates it a few times a day.`
        : bill.state === 'error' ? bill.hint : 'Not connected. The bar uses the estimate.'}</dd></div>
    </dl>
  </div>
}
