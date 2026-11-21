// The prompt Friday drafts from a topic's held notes, shown in the "Held notes" sheet. Sending stays a voice step:
// Friday reads it back and needs a yes, so this block has no send button.
import { FileText } from 'lucide-react'

export interface HeldDraftData {
  state: 'drafting' | 'ready' | 'failed' | 'closed'
  error: string | null
  revisions: { rev: number; text: string; skill: string | null; summary: string; at: number }[]
}

export function HeldDraft({ topicId, draft, shown = false }: { topicId: string; draft: HeldDraftData; shown?: boolean }) {
  const latest = draft.revisions[draft.revisions.length - 1]
  const status = draft.state === 'drafting'
    ? (latest ? `Friday is changing version ${latest.rev}. It takes about a minute.` : 'Friday is writing the prompt. It takes about a minute.')
    : draft.state === 'failed' ? (draft.error || 'The prompt could not be made.') : ''
  return <div className="live-held-draft" data-draft={topicId} data-state={draft.state} data-shown={shown}>
    <div className="live-held-draft-head">
      <FileText size={14} aria-hidden="true" /><span>Prompt</span>{latest && <small>version {latest.rev}</small>}
    </div>
    {/* Always in the page, so a change of state is announced. */}
    <p className="live-held-draft-status" role="status" data-failed={draft.state === 'failed'} hidden={!status}>{status}</p>
    {latest && <>
      <p className="live-held-draft-meta">
        {latest.skill && <code>{latest.skill}</code>}<span>{latest.text.length.toLocaleString()} characters</span>
      </p>
      <div className="live-held-draft-text" tabIndex={0} role="region" aria-label={`Prompt text, version ${latest.rev}`}>{latest.text}</div>
      {draft.state === 'ready' && <p className="live-held-draft-hint">To send it, tell Friday which chat. Friday reads it back first.</p>}
    </>}
  </div>
}
