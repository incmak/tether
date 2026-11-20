import { useId, useRef, useState } from 'react'
import { errorText } from './api'
import type { Api, Approval, Pane } from './api'
import { paneTitle } from './Sidebar'
import { needsMak } from './helperTabs'
import { taskIdentity } from './taskTree'

export function TaskNeeds({ api, task, children, onOpen, onUpdate }: {
  api: Api; task: Pane; children: Pane[]; onOpen: (pane: Pane) => void; onUpdate: () => void
}) {
  const waiting = children.filter(needsMak).sort((a, b) => (b.ask?.since ?? b.since ?? 0) - (a.ask?.since ?? a.since ?? 0))
  if (!waiting.length) return null
  return <div className="task-answers">{waiting.map(child =>
    <TaskAnswer key={taskIdentity(child)} api={api} task={task} child={child} onOpen={() => onOpen(child)} onUpdate={onUpdate} />
  )}</div>
}

function TaskAnswer({ api, task, child, onOpen, onUpdate }: {
  api: Api; task: Pane; child: Pane; onOpen: () => void; onUpdate: () => void
}) {
  const id = useId()
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<string | null>(null)
  const [error, setError] = useState('')
  const writing = useRef(false)
  const nativeRequest = useRef<{ key: string; id: string } | null>(null)
  const name = paneTitle(child)
  const approval = child.approval as Approval | null
  const menu = child.attention?.reason === 'menu' && child.answer_digest ? child.attention : null
  const bridge = child.attention?.askBridge
  const native = bridge?.questions.length === 1 && !bridge.questions[0].multiSelect ? bridge.questions[0] : undefined
  const choicePrompt = child.state === 'Approval' && approval?.actionable && approval.id ? approval : menu
  const options = (choicePrompt?.options ?? []).filter(option => !option.free && option.text !== 'Chat about this')
  const question = approval?.text || native?.question || menu?.question || child.ask?.next || child.needs?.text || child.attention?.question || 'Open the helper to review its request.'
  const prompt = approval?.id || (native && bridge?.nonce) || child.answer_digest
  const answered = !!prompt && sent === prompt
  const canType = !!native || (!!child.answer_digest && (!menu || !!menu.options?.some(option => option.free)))
  const disabled = !child.writable || !child.thread || busy || answered
  const answerLabel = `Answer ${name}`
  async function send(answer: { text: string } | { choice: number } | { nativeChoice: string }) {
    if (disabled || writing.current || !prompt) return
    writing.current = true
    setBusy(true)
    setError('')
    try {
      if (bridge && native && ('text' in answer || 'nativeChoice' in answer)) {
        const value = 'text' in answer ? answer.text : answer.nativeChoice
        const key = JSON.stringify([bridge.id, bridge.nonce, native.question, value])
        if (nativeRequest.current?.key !== key) {
          nativeRequest.current = { key, id: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}` }
        }
        await api('ask-answer', { session: child.id, id: bridge.id, nonce: bridge.nonce,
          answers: { [native.question]: value }, requestId: nativeRequest.current.id })
      } else if ('choice' in answer && approval && choicePrompt === approval) {
        await api('approval', { session: child.id, thread: child.thread, approval: approval.id, choice: answer.choice })
      } else {
        await api('task-tree/answer', { pane: child.id, thread: child.thread, digest: child.answer_digest, ...answer })
      }
      setSent(prompt)
      setDraft('')
      onUpdate()
    } catch (failure) {
      setError(errorText(failure))
      onUpdate()
    } finally {
      writing.current = false
      setBusy(false)
    }
  }
  return <section className="task-answer" aria-label={`${name} needs you`}>
    <p className="task-question"><strong>{name}</strong> needs you: {question}</p>
    <form className="task-answer-field" onSubmit={event => { event.preventDefault(); if (canType && draft.trim()) void send({ text: draft.trim() }) }}>
      <input value={draft} onChange={event => setDraft(event.target.value)} aria-label={answerLabel} aria-describedby={`${id}-destination`}
        placeholder={`${answerLabel}…`} maxLength={native ? 2000 : 20000} disabled={disabled || !canType} />
      <button type="submit" className="secondary" disabled={disabled || !canType || !draft.trim()} aria-label={`Send to ${name}`}>{busy ? 'Sending…' : 'Send'}</button>
    </form>
    <div className="task-answer-actions">
      {native?.options.map((option, index) => <button type="button" key={option.label} className={index ? 'secondary' : 'primary'} disabled={disabled}
        aria-describedby={`${id}-destination`} onClick={() => void send({ nativeChoice: option.label })}>{option.label}</button>)}
      {options.map((option, index) => <button type="button" key={option.id} className={index ? 'secondary' : 'primary'} disabled={disabled}
        aria-describedby={`${id}-destination`} onClick={() => void send({ choice: option.id })}>{option.text}</button>)}
      <button type="button" className="secondary" aria-label={`Open ${name}`} onClick={onOpen}>Open</button>
      <span id={`${id}-destination`} className="task-destination">Goes to the helper {name}, not to {paneTitle(task)}.</span>
    </div>
    {answered && <p role="status">Sent to {name}.</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
