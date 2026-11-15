import { useEffect, useRef, useState } from 'react'
import { Plus, X } from 'lucide-react'
import { SESSION_NAME, SESSION_NAME_HELP, errorText } from './api'
import type { Api, Pane, SessionsData } from './api'
import { useModal } from './hooks'
import { AgentIcon, agentNames, needsYou } from './Sidebar'
import { FoundryTag } from './CodexProvider'
import { ChatPicker, resumeInNewSession } from './ChatPicker'
import { WORKTREE_TIMEOUT, WorktreeField } from './Worktree'
import type { MadeWorktree } from './Worktree'
import { CodexTrustCard } from './CodexTrust'
import { useCodexTrust } from './codexTrustState'



























export function State({ state }: { state: string }) {
  return <span className="pane-state"><span className={`state-dot ${needsYou(state) ? 'approval' : state.toLowerCase()}`} />{state}</span>
}
function defaultName() {
  const now = new Date()
  const two = (n: number) => String(n).padStart(2, '0')
  return `chat-${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}`
}

export function NewSession({ api, onClose, onCreated }: { api: Api; onClose: () => void; onCreated: (pane: Pane, worktree?: MadeWorktree) => void }) {
  const [opening, setOpening] = useState(false)
  const [name, setName] = useState(defaultName)
  const [agent, setAgent] = useState<'claude' | 'codex' | 'shell'>('claude')
  const [worktree, setWorktree] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  const modal = useModal(true, () => { if (!pending) onClose() })
  const trust = useCodexTrust(api, agent === 'codex' ? { mode: 'new', worktree: worktree !== null } : null)
  useEffect(() => () => controller.current?.abort(), [])
  async function create(event: React.FormEvent) {
    event.preventDefault()
    if (pending) return
    const clean = name.trim()
    if (!SESSION_NAME.test(clean)) { setError(SESSION_NAME_HELP); return }
    if (worktree !== null && !worktree) { setError('Enter a branch name for the new worktree.'); return }
    setPending(true); setError('')
    const abort = new AbortController()
    controller.current = abort
    let created: Pane | undefined
    let sessionCreated = false
    let made: MadeWorktree | undefined
    try {
      const result = await api<{ session: string; name: string; worktree?: MadeWorktree }>('tmux-sessions', { name: clean, ...(worktree !== null ? { worktree } : {}) }, abort.signal, undefined, worktree !== null ? WORKTREE_TIMEOUT : undefined)
      sessionCreated = true
      made = result.worktree
      const deadline = Date.now() + 5000
      while (Date.now() < deadline && !abort.signal.aborted) {
        const remaining = Math.max(1, deadline - Date.now())
        const resultList = await api<SessionsData>('sessions', undefined, AbortSignal.any([abort.signal, AbortSignal.timeout(remaining)]))
        created = resultList.sessions.find(pane => pane.label === result.name)
        if (created) break
        await new Promise(resolve => setTimeout(resolve, Math.min(300, Math.max(0, deadline - Date.now()))))
      }
      if (!created) throw new Error(`Session “${result.name}” was created but its pane is not visible yet. Close this sheet and select it from the list.`)
      if (agent !== 'shell') await api('start', { session: created.id, thread: null, agent }, abort.signal)
      onCreated(created, made)
    } catch (error) {
      if (!abort.signal.aborted) {
        const kept = made ? ` The worktree is at ${made.path}.` : ''
        setError((sessionCreated && created ? `Session created. Could not start ${agentNames[agent]}: ${errorText(error)} Close this sheet and open the session to try again.` : errorText(error)) + kept)
        if (sessionCreated) setName(defaultName())
        // The branch now exists: a second Create with the same name would be refused.
        if (made) setWorktree(null)
      }
    } finally { if (!abort.signal.aborted) setPending(false) }
  }
  const heading = <>
    <div className="sheet-heading"><h2 id="new-title">{opening ? 'Open a chat' : 'New session'}</h2><button type="button" className="icon-button" aria-label="Close new session" disabled={pending} onClick={onClose}><X /></button></div>
    <div className="pick-modes" role="tablist" aria-label="New session">
      <button type="button" role="tab" aria-selected={!opening} disabled={pending} onClick={() => setOpening(false)}>New</button>
      <button type="button" role="tab" aria-selected={opening} disabled={pending} onClick={() => setOpening(true)}>Open existing</button>
    </div>
  </>
  if (opening) return <dialog {...modal} className="sheet session-pick" aria-labelledby="new-title">
    {heading}
    <ChatPicker api={api} onChat={async chat => onCreated(await resumeInNewSession(api, chat))} onTab={onCreated} />
  </dialog>
  return <dialog {...modal} className="sheet session-pick" aria-labelledby="new-title">
    <form onSubmit={create}>
      {heading}
      <p>A fresh terminal, ready for your next task.</p>
      <label htmlFor="session-name">Session name</label>
      <input autoFocus id="session-name" value={name} maxLength={64} onChange={event => setName(event.target.value)} disabled={pending} autoComplete="off" />
      <fieldset disabled={pending}><legend>Start with</legend><div className="agent-options">{(['claude', 'codex', 'shell'] as const).map(choice => <label key={choice} className={choice === agent ? 'selected' : ''}><input type="radio" name="agent" value={choice} checked={agent === choice} onChange={() => setAgent(choice)} /><AgentIcon agent={choice} /><span className="agent-choice-name">{agentNames[choice]}{choice === 'codex' && <FoundryTag api={api} below />}</span></label>)}</div></fieldset>
      <WorktreeField api={api} value={worktree} onChange={value => { setWorktree(value); if (error) setError('') }} disabled={pending} />
      <CodexTrustCard trust={trust} />
      {error && <p className="error" role="alert">{error}</p>}
      <button className="primary create-button" disabled={pending}>{pending ? 'Creating session…' : 'Create session'}<Plus size={18} /></button>
    </form>
  </dialog>
}

// Plain terminal text with each web address as a link. Trailing punctuation stays outside the link.
