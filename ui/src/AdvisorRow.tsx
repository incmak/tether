import { ChevronRight, Lightbulb } from 'lucide-react'
import type { AdvisorCall } from './api'
import { stamp } from './messageTime'
import { formatElapsed } from './paneState'
import { toolSeconds } from './toolSummary'

function modelLabel(model = '') {
  const match = /^claude-(fable|opus|sonnet|haiku)-(\d+)-(\d+)(?:-\d{8})?$/.exec(model)
  if (!match) return model
  const [, family, major, minor] = match
  return `${family[0].toUpperCase()}${family.slice(1)} ${major}.${minor}`
}

export function AdvisorRow({ id, advisor, time, live }: { id: string; advisor: AdvisorCall; time?: string; live?: boolean }) {
  const model = modelLabel(advisor.model)
  const when = stamp(time)
  let state
  let body
  switch (advisor.status) {
    case 'answered': {
      const seconds = toolSeconds(advisor)
      const duration = seconds === null ? '' : formatElapsed(seconds)
      state = duration
      body = <>
        <p>Claude sent its conversation to the advisor{model ? ` (${model})` : ''}.{duration && ` The advice came back after ${duration}.`}</p>
        <p>Claude Code keeps the advice text encrypted in the transcript, so Tether cannot show it. Claude&apos;s next steps show how it used the advice.</p>
      </>
      break
    }
    case 'error':
      state = ''
      body = <p>The advisor did not answer: {(advisor.error || 'unknown').replace(/_/g, ' ')}. Claude went on without advice.</p>
      break
    case 'pending':
      state = live ? 'Waiting for advice…' : 'No reply recorded'
      body = <p>{live ? 'Claude is waiting for the advisor to answer.' : 'The transcript has no answer for this call. Claude may have stopped before the advisor replied.'}</p>
      break
    case 'unknown':
      state = 'Reply received'
      body = <p>Claude Code recorded the advisor&apos;s reply in a form Tether does not read ({advisor.result_type || 'unknown'}).</p>
      break
  }
  // The state first: at 320 px the end of the line is cut. A failed call says so in the error color, inside the same line.
  const failed = advisor.status === 'error'
  const meta = [failed ? 'No answer' : state, model].filter(Boolean).join(' · ')
  return <details className="tool-row compact-row advisor-call" data-msg-id={id}>
    <summary>
      <Lightbulb size={15} aria-hidden="true" />
      <span className="advisor-call-what">Asked the advisor</span>
      <span className="advisor-call-meta" title={meta || undefined}>{failed ? <><span className="tool-error">No answer</span>{model && ` · ${model}`}</> : meta}</span>
      {when && <time dateTime={time}>{when}</time>}
      <ChevronRight className="chevron" size={15} aria-hidden="true" />
    </summary>
    <div className="advisor-call-body">{body}</div>
  </details>
}
