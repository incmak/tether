// Trust a folder for Codex (server: codex_trust.py, backlog B82). Codex runs read-only in a folder that config.toml does
// not trust. The write needs a passkey approval: agents hold Tether's token, so a click alone is no proof of the person.
import { useId } from 'react'
import { Check, ShieldAlert } from 'lucide-react'
import type { useCodexTrust } from './codexTrustState'
import { Spinner } from './Skeleton'
import './CodexTrust.css'

/** The card: the reason, the folder that gets trusted, and the action. Nothing for a folder Codex can already edit. */
export function CodexTrustCard({ trust, running = false }: { trust: ReturnType<typeof useCodexTrust>; running?: boolean }) {
  const { status, busy, error, justTrusted } = trust
  const title = useId()
  if (!status) return null
  if (status.state === 'restart') return <div className="codex-trust" role="status">
    <Check size={16} aria-hidden="true" />
    <span><strong>Folder trusted. This Codex tab still runs read-only.</strong>
      <small>To let Codex edit here, type <code>/quit</code> in this tab. Then open this chat again from New session › Open existing.</small></span>
  </div>
  if (status.state === 'trusted' && justTrusted) return <div className="codex-trust" role="status">
    <Check size={16} aria-hidden="true" />
    <span><strong>Folder trusted.</strong><small>{running ? 'Codex can edit here.' : 'The new Codex tab can edit files here.'}</small></span>
  </div>
  if (status.state !== 'untrusted' || !status.target) return null
  const wider = status.folder !== status.target
  return <div className="codex-trust" role="group" aria-labelledby={title}>
    <ShieldAlert size={16} aria-hidden="true" />
    <span>
      <strong id={title}>Codex can only read files in this folder.</strong>
      <small>Trust it to let Codex edit here. You approve with your passkey.</small>
      <code className="codex-trust-path">{status.target}</code>
      {wider && <small>This trusts the whole repository, its worktrees included.</small>}
      {error && <small className="codex-trust-error" role="alert">{error}</small>}
    </span>
    <button type="button" className="secondary" disabled={busy} aria-busy={busy} onClick={() => void trust.trust()}>
      {busy ? <><Spinner size={14} />Trusting…</> : 'Trust folder'}
    </button>
  </div>
}
