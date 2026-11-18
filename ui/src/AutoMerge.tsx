import { useCallback, useState } from 'react'
import { NoteBone, SettingSkeleton, SwitchBone } from './SettingBones'
import { agoSeconds } from './Sidebar'
import { errorText } from './api'
import type { Api } from './api'
import { usePolling } from './hooks'

// Auto-merge by risk and the trust ladder (server: auto_merge.py). Observe is the default: Tether records what it
// would merge and merges nothing. Only the user moves it to Enforce, here.
export type AutoMergeMode = 'off' | 'observe' | 'enforce'
type Outcome = 'would_merge' | 'wait' | 'merged' | 'failed'
interface Check { key: string; ok: boolean; text: string }
interface Decision {
  at: number; mode: AutoMergeMode; outcome: Outcome; repo: string | null; root: string | null; branch: string | null
  head: string | null; target: string | null; name: string | null; subject: string | null; checks: Check[] | null
  merge: string | null; error: string | null; note: string | null
}
interface TrustArea { name: string; label: string; rung: number; limit: number; streak: number; need: number; top: boolean; merges: number; reverts: number }
interface Trust { root: string; repo: string; target?: string; areas: TrustArea[]; error: string }
export interface AutoMergeState {
  mode: AutoMergeMode; since: number
  modes: { mode: AutoMergeMode; name: string; summary: string }[]
  decisions: Decision[]; trust: Trust[]; rungs: number[]; rules_file: string
}

const OUTCOME: Record<Outcome, string> = { would_merge: 'Would merge', wait: 'Waits for you', merged: 'Merged', failed: 'Merge failed' }

function trustText(area: TrustArea, rungs: number[]) {
  if (area.rung === 0) return 'Trust is at zero after a revert. You merge this area until you restore it.'
  const limit = `Up to ${area.limit} changed lines in one merge.`
  if (area.top) return `${limit} This is the highest step.`
  return `${limit} ${area.streak} of ${area.need} clean merges to the next step (${rungs[area.rung + 1]} lines).`
}

function DecisionRow({ item }: { item: Decision }) {
  const when = agoSeconds(item.at)
  const checks = item.checks ?? []
  const failed = checks.filter(check => !check.ok)
  const title = item.name || item.branch || 'A branch'
  return <li className="auto-merge-decision"><details>
    <summary>
      <span className={`auto-merge-outcome auto-merge-${item.outcome}`}>{OUTCOME[item.outcome] ?? item.outcome}</span>
      <span className="auto-merge-what"><strong>{title}</strong>
        <small>{[item.repo, item.branch, item.head?.slice(0, 12), when === 'now' ? 'now' : `${when} ago`].filter(Boolean).join(' · ')}</small>
        {item.outcome === 'wait' && failed[0] && <small className="auto-merge-reason">{failed[0].text}</small>}
        {item.outcome === 'failed' && item.error && <small className="auto-merge-reason">{item.error}</small>}
        {item.outcome === 'merged' && item.merge && <small className="auto-merge-reason">Merge commit {item.merge.slice(0, 12)} on {item.target}. Not deployed.</small>}
      </span>
    </summary>
    {item.subject && <p className="setting-note">{item.subject}</p>}
    {item.note && <p className="setting-note">{item.note}</p>}
    <ul className="auto-merge-checks">{checks.map(check => <li key={check.key}>
      <span className={check.ok ? 'auto-merge-pass' : 'auto-merge-fail'}>{check.ok ? 'Pass' : 'Wait'}</span><span>{check.text}</span>
    </li>)}</ul>
    {item.outcome === 'merged' && item.merge && <p className="setting-note">Undo: <code>git revert -m 1 {item.merge.slice(0, 12)}</code></p>}
  </details></li>
}

/** Settings › Agents: the mode, the trust of each area and the latest decisions. */
export function AutoMergeSettings({ api }: { api: Api }) {
  const fetcher = useCallback((signal: AbortSignal) => api<AutoMergeState>('auto-merge', undefined, signal), [api])
  const { data, error: loadError, refresh } = usePolling(fetcher, 10000)
  const [pending, setPending] = useState<AutoMergeMode | 'restore' | null>(null)
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState('')
  async function save(body: Record<string, unknown>, kind: AutoMergeMode | 'restore') {
    setPending(kind); setError(''); setAsking(false)
    try { await api<AutoMergeState>('auto-merge', body); await refresh() }
    catch (failure) { setError(errorText(failure)) }
    finally { setPending(null) }
  }
  const choose = (mode: AutoMergeMode) => { if (mode === 'enforce') setAsking(true); else void save({ mode }, mode) }
  const value = pending && pending !== 'restore' ? pending : asking ? 'enforce' : data?.mode ?? 'observe'
  const current = data?.modes.find(item => item.mode === (asking ? data.mode : value))
  return <section className="settings-group auto-merge" aria-labelledby="settings-auto-merge"><h3 id="settings-auto-merge">Auto-merge</h3>
    {data && (error || loadError) && <p className="error" role="alert">{error || loadError}</p>}
    {!data ? loadError ? <p className="setting-note" role="alert">{loadError}</p> : <SettingSkeleton label="Loading auto-merge"><NoteBone lines={1} /><SwitchBone /><NoteBone lines={2} /></SettingSkeleton> : <>
      <fieldset className="choice" disabled={!!pending}>
        <legend>Mode</legend>
        <div className="segmented">{data.modes.map(item => <label key={item.mode}>
          <input type="radio" name="auto-merge-mode" value={item.mode} checked={value === item.mode} aria-describedby="auto-merge-note" onChange={() => choose(item.mode)} /><span>{item.name}</span>
        </label>)}</div>
        {current && <p id="auto-merge-note" className="setting-note">{current.summary}</p>}
      </fieldset>
      {asking && <div className="provider-warning auto-merge-confirm" role="alertdialog" aria-labelledby="auto-merge-confirm-title" aria-describedby="auto-merge-confirm-text">
        <strong id="auto-merge-confirm-title">Turn on Enforce?</strong>
        <p id="auto-merge-confirm-text">Tether then merges a worker branch into the main branch on origin without you, when every check passes. It pushes one merge commit, never a forced push. <code>git revert -m 1</code> undoes a merge. A merge is not a deploy. Approvals from before now do not merge.</p>
        <div className="auto-merge-actions">
          <button type="button" className="secondary" onClick={() => setAsking(false)}>Cancel</button>
          <button type="button" className="primary" autoFocus onClick={() => void save({ mode: 'enforce' }, 'enforce')}>Turn on Enforce</button>
        </div>
      </div>}
      <p className="setting-note">A branch merges without you only when all of this holds: the reviewer approved the exact commit, its stamp names tests, every changed file is in a trusted area and within the area’s limit, no reliability checklist row is hit, and the commit merges with no conflict. If one check fails, the branch waits for you.</p>

      <h4 className="auto-merge-heading">Trust by area</h4>
      {data.trust.length === 0 ? <p className="hint">No open repository has a <code>{data.rules_file}</code> on its main branch, so nothing can auto-merge yet.</p>
        : data.trust.map(repo => <div key={repo.root} className="auto-merge-repo">
          <p className="auto-merge-repo-name"><strong>{repo.repo}</strong>{repo.target && <small> · {repo.target}</small>}</p>
          {repo.error ? <p className="setting-note">{repo.error}</p> : <ul className="auto-merge-areas">{repo.areas.map(area => <li key={area.name}>
            <span><strong>{area.label}</strong><small>{trustText(area, data.rungs)}</small>
              <small>{area.merges} {area.merges === 1 ? 'merge' : 'merges'} by Tether · {area.reverts} {area.reverts === 1 ? 'revert' : 'reverts'}</small></span>
            {area.rung === 0 && <button type="button" className="secondary" disabled={!!pending} onClick={() => void save({ restore: { root: repo.root, area: area.name } }, 'restore')}>Restore trust<span className="visually-hidden"> for {area.label} in {repo.repo}</span></button>}
          </li>)}</ul>}
        </div>)}
      <p className="setting-note">Each area starts at {data.rungs[1]} lines. A limit widens only after clean merges that Tether made. A revert on the main branch narrows it. Files outside these areas always wait for you.</p>

      <h4 className="auto-merge-heading">Latest decisions</h4>
      {data.decisions.length === 0 ? <p className="hint">No decisions yet. One appears here when a reviewer approves a commit of a worker branch.</p>
        : <ul className="auto-merge-decisions">{data.decisions.map(item => <DecisionRow key={`${item.root}-${item.head}-${item.at}-${item.outcome}`} item={item} />)}</ul>}
    </>}
  </section>
}
