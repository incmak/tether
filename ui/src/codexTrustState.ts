// Trust state for a folder Codex runs in (server: codex_trust.py, backlog B82). The card is in CodexTrust.tsx.
import { useEffect, useState } from 'react'
import { errorText } from './api'
import type { Api } from './api'
import { withApproval } from './approval'

export type TrustState = 'trusted' | 'untrusted' | 'restart' | 'other'
/** target_id: the identity of the folder `target` names; the approval binds it, so a swapped folder is refused. */
export interface TrustStatus { state: TrustState; folder: string | null; target: string | null; target_id?: string | null }
/** mode 'new': a new Codex tab, in the folder of tab `session` or the default session folder; worktree: a new worktree there.
 *  mode 'running': the Codex tab `session`. */
export interface TrustSubject { mode: 'new' | 'running'; session?: string; worktree?: boolean }

/** Reads the trust state for the subject; null while it loads, when it fails (the check only advises) and when off. */
export function useCodexTrust(api: Api, subject: TrustSubject | null, refresh = '') {
  const key = subject ? JSON.stringify(subject) : ''
  const [result, setResult] = useState<{ key: string; status: TrustStatus | null }>({ key: '', status: null })
  const [trusted, setTrusted] = useState<{ key: string; status: TrustStatus } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ key: string; text: string } | null>(null)
  useEffect(() => {
    if (!key) return
    const controller = new AbortController()
    api<TrustStatus>('codex-trust', { op: 'status', ...JSON.parse(key) }, controller.signal)
      .then(status => setResult({ key, status }), () => { if (!controller.signal.aborted) setResult({ key, status: null }) })
    return () => controller.abort()
  }, [api, key, refresh])
  const status = trusted?.key === key ? trusted.status : result.key === key ? result.status : null
  async function trust() {
    if (!subject || !status?.target || busy) return
    setBusy(true); setError(null)
    try {
      const next = await withApproval<TrustStatus>(api, 'codex-trust', 'codex-trust', { op: 'trust', ...subject, target: status.target, target_id: status.target_id })
      setTrusted({ key, status: next })
    } catch (failure) {
      setError({ key, text: errorText(failure) })
    } finally { setBusy(false) }
  }
  return { status, busy, error: error?.key === key ? error.text : '', trust, justTrusted: trusted?.key === key }
}
