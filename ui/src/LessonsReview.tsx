// The lessons review sheet. Lists every lesson of every project, the ones that need a look first: questioned by an
// agent, did not stick, seen once long ago, new. The person keeps, edits or removes each one; untouched lessons stay
// as they are. "Finish review" starts the next 14 days; "Remind me in 3 days" snoozes the card. The advisor check asks
// the advisor model for a second look; it only suggests, and nothing changes until the person acts.
import { useCallback, useEffect, useState } from 'react'
import { Check, Pencil, Sparkles, Trash2, X } from 'lucide-react'
import type { Api } from './api'
import { errorText } from './api'
import { useModal, usePolling } from './hooks'
import { Bone, Skeleton } from './Skeleton'
import { BoneLines } from './SettingBones'
import { LessonText } from './Learning'
import { reviewReason } from './lessonsReview'
import './Settings.css'
import './lessonsReview.css'
import type { CheckAdvice, CheckVerdict, LessonsCheck, ReviewGroupKey, ReviewLesson, ReviewState } from './lessonsReview'

const GROUP: Record<ReviewGroupKey, { title: string; note?: string }> = {
  disputed: { title: 'Questioned by an agent', note: 'An agent found evidence against these. Read its reason, then decide.' },
  stuck: { title: 'Did not stick', note: 'You corrected these 3 times or more. A rule in AGENTS.md or CLAUDE.md is stronger.' },
  stale: { title: 'Seen once, not repeated in 30 days', note: 'A lesson that works also does not repeat. Remove only the ones that no longer hold.' },
  new: { title: 'New since your last review' },
  other: { title: 'Other lessons' },
}

const VERDICT: Record<CheckVerdict, string> = { keep: 'Keep', edit: 'Fix', remove: 'Remove', unsure: 'Not sure' }
const clock = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
const lessonsWord = (count: number) => `${count} ${count === 1 ? 'lesson' : 'lessons'}`

/** The advisor check: one button, then running, done or failed. */
function CheckPanel({ check, busy, error, onStart }: { check: LessonsCheck; busy: boolean; error: string; onStart: () => void }) {
  const running = check.state === 'running'
  const who = check.label ? `The advisor (${check.label})` : 'The advisor you chose in Settings › Agents'
  return <section className="review-check" aria-labelledby="review-check-title">
    <h3 id="review-check-title">Advisor check</h3>
    {running ? <p className="review-check-status" role="status"><span className="review-check-pulse" aria-hidden="true" />The advisor is checking {check.count ? lessonsWord(check.count) : 'the lessons'}. It takes a few minutes. You can close this sheet.</p>
      : check.state === 'done' ? <p className="review-check-status" role="status">Advisor checked {lessonsWord(check.count ?? 0)}{check.finished ? ` at ${clock(check.finished)}` : ''}{check.label ? ` · ${check.label}` : ''}.{check.skipped ? ` It checks up to 60 at a time, so ${lessonsWord(check.skipped)} were not checked.` : ''} You still decide each one.</p>
      : <p className="setting-note">{who} reads your project notes and saved answers, then suggests what to keep, fix or remove. You still decide. It takes a few minutes.</p>}
    {check.state === 'failed' && check.error && <p className="error" role="alert">{check.error}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {!running && <button type="button" className="secondary" disabled={busy} onClick={onStart}><Sparkles size={15} aria-hidden="true" />
      {check.state === 'done' ? 'Check again' : check.state === 'failed' || error ? 'Try again' : 'Ask the advisor to check these'}</button>}
  </section>
}

function AdviceLine({ advice, onUse }: { advice: CheckAdvice; onUse?: () => void }) {
  return <span className={`review-advice review-advice-${advice.verdict}`}>
    <small><strong>Advisor suggests: {VERDICT[advice.verdict]}</strong> · {advice.confidence} confidence. {advice.reason}</small>
    {advice.verdict === 'edit' && advice.text && <small className="review-advice-text">“{advice.text}”</small>}
    {advice.verdict === 'edit' && advice.text && onUse && <button type="button" className="link-button" onClick={onUse}>Use this wording</button>}
  </span>
}

export function LessonsReview({ api, onClose, onChange }: { api: Api; onClose: () => void; onChange?: () => void }) {
  const modal = useModal(true, onClose)
  const fetcher = useCallback((signal: AbortSignal) => api<ReviewState>('autonomy/review', undefined, signal), [api])
  const { data, error: loadError, refresh } = usePolling(fetcher, 60000)
  const [failure, setError] = useState('')
  const error = failure || loadError
  const [busy, setBusy] = useState('')
  const [kept, setKept] = useState<string[]>([])
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)
  const [now] = useState(() => Date.now() / 1000)
  // A start error belongs to the check that was shown when it failed. Once a check runs or a newer one exists (for
  // example another device started it), the error no longer applies.
  const [startError, setStartError] = useState<{ text: string; started?: number } | null>(null)
  const [starting, setStarting] = useState(false)
  const check: LessonsCheck = data?.check ?? { state: 'idle' }
  const checkError = startError && check.state !== 'running' && startError.started === check.started ? startError.text : ''
  const advice = check.advice ?? {}

  // While the advisor works, look every 5 seconds; the normal poll is once a minute.
  const running = check.state === 'running'
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => void refresh(), 5000)
    return () => clearInterval(timer)
  }, [running, refresh])

  async function startCheck() {
    const shown = check.started
    setStarting(true); setStartError(null)
    try { await api('autonomy/review', { action: 'check' }); await refresh() }
    catch (caught) { setStartError({ text: errorText(caught), started: shown }); void refresh() }
    finally { setStarting(false) }
  }

  async function act(key: string, work: () => Promise<unknown>, after?: () => void) {
    setBusy(key); setError('')
    try { await work(); after?.(); await refresh(); onChange?.() }
    catch (caught) { setError(errorText(caught)) }
    finally { setBusy('') }
  }
  const lessonBody = (lesson: ReviewLesson, action: Record<string, unknown>) =>
    lesson.project ? { project: lesson.project, ...action } : action
  const keep = (lesson: ReviewLesson) => act(lesson.id, () => api('autonomy/lessons', lesson.project ? { project: lesson.project, keep: lesson.id } : { keep_global: lesson.id }), () => setKept(list => [...list, lesson.id]))
  const remove = (lesson: ReviewLesson) => act(lesson.id, () => api('autonomy/lessons', lesson.project ? { project: lesson.project, remove: lesson.id } : { remove_global: lesson.id }))
  const save = (lesson: ReviewLesson, text: string) => act(lesson.id, () => api('autonomy/lessons', lessonBody(lesson, { edit: lesson.id, text })), () => { setEditing(null); setKept(list => [...list, lesson.id]) })
  const finish = (action: 'done' | 'snooze') => act(action, () => api('autonomy/review', { action }), onClose)

  const all = data?.groups.flatMap(group => group.lessons) ?? []
  const textOf = (id: string) => all.find(lesson => lesson.id === id)?.text
  const reason = data?.due ? reviewReason(data) : ''

  return <dialog {...modal} className="sheet lessons-review" aria-labelledby="review-title">
    <div className="sheet-heading">
      <h2 id="review-title">Review lessons</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} aria-hidden="true" /></button>
    </div>
    <div className="review-body">
      {reason && <p className="review-reason">{reason}.</p>}
      <p className="setting-note">You can be wrong too. Keep what still holds, fix what is close and remove the rest. Lessons you do not touch stay as they are.</p>
      {data && !!all.length && <CheckPanel check={check} busy={starting} error={checkError} onStart={() => void startCheck()} />}
      {error && <p className="error" role="alert">{error} <button type="button" className="link-button" onClick={() => { setError(''); void refresh() }}>Try again</button></p>}
      {!data ? !error && <Skeleton label="Loading lessons" className="review-group"><ul className="lesson-list" aria-hidden="true">{[0, 1, 2].map(index => <li key={index} className="lesson"><span className="lesson-text"><strong><BoneLines count={index === 1 ? 1 : 2} from={index} /></strong><small><Bone w="40%" /></small></span></li>)}</ul></Skeleton> : !all.length ? <p className="hint">No lessons yet. Agents save one when you correct them or confirm a choice.</p> : data.groups.map(group => <section key={group.key} className="review-group" aria-labelledby={`review-${group.key}`}>
        <h3 id={`review-${group.key}`}>{GROUP[group.key].title} <span className="review-group-count">{group.lessons.length}</span></h3>
        {GROUP[group.key].note && <p className="hint">{GROUP[group.key].note}</p>}
        <ul className="lesson-list">{group.lessons.map(lesson => <li key={lesson.id} className="lesson review-lesson">
          {editing?.id === lesson.id ? <form className="lesson-edit" onSubmit={event => { event.preventDefault(); void save(lesson, editing.text) }}>
            <label htmlFor={`review-edit-${lesson.id}`} className="visually-hidden">Lesson text</label>
            <textarea id={`review-edit-${lesson.id}`} value={editing.text} maxLength={200} rows={3} autoFocus onChange={event => setEditing({ id: lesson.id, text: event.target.value })} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setEditing(null) } }} />
            <small className="hint">Start with “When”. One line, up to 200 characters.</small>
            <div className="lesson-edit-actions"><button type="button" onClick={() => setEditing(null)}>Cancel</button><button type="submit" className="primary" disabled={!!busy || !editing.text.trim()}>Save</button></div>
          </form> : <>
            <span className="review-main">
              <LessonText lesson={lesson} now={now} where={lesson.project ? lesson.project_name ?? lesson.project : 'All projects'} />
              {lesson.disputes?.map(item => <small key={item.pane + item.at} className="review-dispute">Agent ({item.pane}): {item.reason}</small>)}
              {lesson.flags?.map(flag => textOf(flag.with) && <small key={flag.type + flag.with} className="review-flag">{flag.type === 'conflict' ? 'May conflict with' : 'May repeat'}: {textOf(flag.with)}</small>)}
              {advice[lesson.id] && <AdviceLine advice={advice[lesson.id]} onUse={lesson.project ? () => setEditing({ id: lesson.id, text: advice[lesson.id].text ?? lesson.text }) : undefined} />}
            </span>
            <div className="review-actions">
              {kept.includes(lesson.id) && !lesson.disputed
                ? <span className="review-kept" role="status"><Check size={15} aria-hidden="true" />Kept</span>
                : <button type="button" className="secondary" disabled={!!busy} onClick={() => void keep(lesson)}><Check size={15} aria-hidden="true" />Keep</button>}
              {lesson.project && <button type="button" className="icon-button" aria-label={`Edit the lesson: ${lesson.text}`} disabled={!!busy} onClick={() => setEditing({ id: lesson.id, text: lesson.text })}><Pencil size={16} aria-hidden="true" /></button>}
              <button type="button" className="icon-button" aria-label={`Remove the lesson: ${lesson.text}`} disabled={!!busy} onClick={() => void remove(lesson)}><Trash2 size={16} aria-hidden="true" /></button>
            </div>
          </>}
        </li>)}</ul>
      </section>)}
      {!!data?.skipped?.length && <p className="hint">Tether could not read the lessons of {data.skipped.length} {data.skipped.length === 1 ? 'project' : 'projects'}.</p>}
    </div>
    <div className="review-footer">
      {data?.due && <button type="button" className="secondary" disabled={!!busy} onClick={() => void finish('snooze')}>Remind me in 3 days</button>}
      <button type="button" className="primary" disabled={!!busy || !data} onClick={() => void finish('done')}>Finish review</button>
    </div>
  </dialog>
}
