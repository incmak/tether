import { useState } from 'react'
import { ArrowRightLeft, X } from 'lucide-react'
import type { Api, Pane } from './api'
import { errorText } from './api'
import { useModal } from './hooks'

type Target = 'claude' | 'codex-plan' | 'codex-foundry'
interface Preview {
  request: string; version: string; target: Target; native: boolean; brief: string
  characters: number; estimated_tokens: number; message_count: number
  choices: { id: Target; label: string; current: boolean; ready: boolean }[]
}
const choices = [
  { id: 'claude', label: 'Claude' }, { id: 'codex-plan', label: 'Codex · ChatGPT plan' }, { id: 'codex-foundry', label: 'Codex · Foundry' },
] as const

export function ContinueWith({ api, pane, thread, idle, onClose, onCreated }: {
  api: Api; pane: Pane; thread: string | null; idle: boolean; onClose(): void; onCreated(id: string): void
}) {
  const [target, setTarget] = useState<Target>(pane.agent === 'claude' ? 'codex-plan' : 'claude')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [available, setAvailable] = useState<Preview['choices']>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const modal = useModal(true, () => { if (!busy) onClose() })
  async function inspect() {
    setBusy(true); setError('')
    try { const data = await api<Preview>('continue-preview', { session: pane.id, thread, target }); setPreview(data); setAvailable(data.choices) }
    catch (error) { setError(errorText(error)) }
    finally { setBusy(false) }
  }
  async function apply() {
    if (!preview || busy || !idle) return
    setBusy(true); setError('')
    try {
      const result = await api<{ pane: string }>('continue', { session: pane.id, thread, target: preview.target, version: preview.version, request: preview.request })
      onCreated(result.pane)
    } catch (error) { setError(errorText(error)); setBusy(false) }
  }
  return <dialog {...modal} className="sheet model-sheet" aria-labelledby="continue-title"
    onCancel={event => { if (busy) event.preventDefault(); else modal.onCancel(event) }}>
    <div className="sheet-heading"><h2 id="continue-title">Continue with…</h2><button type="button" className="icon-button" aria-label="Close" disabled={busy} onClick={onClose}><X /></button></div>
    <p className="sheet-sub sheet-note">Keep this conversation visible with another agent or provider. The original session stays available.</p>
    <div className="model-list" role="radiogroup" aria-label="Continue with">
      {choices.map(choice => {
        const status = available.find(item => item.id === choice.id)
        const current = status?.current ?? (choice.id === 'claude' && pane.agent === 'claude')
        return <button type="button" role="radio" aria-checked={target === choice.id} key={choice.id} disabled={busy || current || status?.ready === false}
          onClick={() => { setTarget(choice.id); setPreview(null); setError('') }}><span>{choice.label}</span>{current && <small>current</small>}{status?.ready === false && <small>not set up</small>}</button>
      })}
    </div>
    {preview && <div className="continuation-preview" role="status">
      <p>{preview.native ? 'Codex keeps its native conversation history in a new thread.' : `A context brief of ${preview.characters.toLocaleString()} characters goes with your next message. About ${preview.estimated_tokens.toLocaleString()} tokens; estimate only.`}</p>
      <p>Tether sends no prompt during the switch. Running jobs stay in the original session. Check the goal and budget in the new session. Attachments are not resent in a context brief.</p>
      {!preview.native && <details><summary>Review the context brief</summary><pre tabIndex={0}>{preview.brief}</pre></details>}
    </div>}
    {!idle && <p className="hint" role="status">Wait until this chat is idle.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {preview ? <><button type="button" className="primary create-button" disabled={busy || !idle} onClick={() => void apply()}><ArrowRightLeft size={16} />{busy ? 'Opening…' : 'Continue'}</button><button type="button" className="link-button" disabled={busy || !idle} onClick={() => void inspect()}>Refresh preview</button></>
      : <button type="button" className="primary create-button" disabled={busy || !idle} onClick={() => void inspect()}>{busy ? 'Reading context…' : 'Preview handoff'}</button>}
  </dialog>
}
