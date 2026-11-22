// Upkeep in Settings › Learning & upkeep (server: prune.py, ops_agent.py, upkeep_api.py).
// - Clean-up (B211): a weekly check flags lessons that never repeated and notes nobody touched for 30 days. The
//   person removes each one with its own button; nothing goes without that tap. Undo works for 7 days.
// - Daily ops report (B212): once a day Tether writes yesterday's facts to a postmortem note and a cheap tab says
//   what to learn. It proposes lessons; the person adds or dismisses each one. Off until the person turns it on.
import { useCallback, useState } from 'react'
import { Check, FileText, ListChecks, Play, Plus, Trash2, Undo2, X } from 'lucide-react'
import type { Api } from './api'
import { errorText } from './api'
import { useModal, usePolling } from './hooks'
import { Bone, Skeleton } from './Skeleton'
import { BoneLines, NoteBone, SettingSkeleton, SwitchBone } from './SettingBones'
import { LessonText } from './Learning'
import type { Lesson } from './Autonomy'
import './Settings.css'
import './lessonsReview.css'
import './Upkeep.css'
import { fmtDate, useTetherZone } from './zone'

interface StaleLesson extends Lesson { key: string; mark: string; project: string | null; project_name: string | null }
interface StaleNote { key: string; mark: string; project: string; project_name: string; name: string; title: string; size: number; mtime: number }
interface Removed { token: string; kind: 'lesson' | 'note'; at: number; until: number; project_name: string; text: string; name: string }
interface PruneState {
  last_run: number; next_run: number; stale_days: number; note_days: number; undo_days: number
  lessons: StaleLesson[]; notes: StaleNote[]; removed: Removed[]; skipped: string[]
}
interface Candidate { id: string; text: string; kind: 'avoid' | 'keep'; quote: string; tab: string; at: number; project: string; project_name: string }
interface OpsRun { day: string; project: string; project_name: string; status: 'started' | 'quiet' | 'failed' | ''; why: string; pane: string; at: number; note: boolean; problems: number }
interface OpsState { enabled: boolean; hour: number; model: string; tabs: number; busy: boolean; runs: OpsRun[]; candidates: Candidate[]; skipped: string[] }

const day = (seconds: number) => fmtDate(seconds * 1000, { day: 'numeric', month: 'short', year: 'numeric' })
const count = (number: number, word: string) => `${number} ${number === 1 ? word : `${word}s`}`
const daysLeft = (until: number, now: number) => Math.max(1, Math.ceil((until - now) / 86400))

function useAct(refresh: () => Promise<void>) {
  const [busy, setBusy] = useState('')
  const [failure, setFailure] = useState('')
  async function act(key: string, work: () => Promise<unknown>) {
    setBusy(key); setFailure('')
    try { await work(); await refresh() }
    catch (caught) { setFailure(errorText(caught)); void refresh() }
    finally { setBusy('') }
  }
  return { busy, failure, setFailure, act }
}

/** The clean-up sheet: every flagged lesson and note with its own Keep and Remove, then what can be undone. */
export function CleanUp({ api, onClose }: { api: Api; onClose: () => void }) {
  useTetherZone()
  const modal = useModal(true, onClose, undefined, { view: 'cleanup' })
  const fetcher = useCallback((signal: AbortSignal) => api<PruneState>('upkeep/prune', undefined, signal), [api])
  const { data, error: loadError, refresh } = usePolling(fetcher, 60000)
  const { busy, failure, setFailure, act } = useAct(refresh)
  const [now] = useState(() => Date.now() / 1000)
  const error = failure || loadError
  const send = (key: string, body: Record<string, unknown>) => act(key, () => api('upkeep/prune', body))
  const empty = !!data && !data.lessons.length && !data.notes.length

  return <dialog {...modal} className="sheet lessons-review upkeep-sheet" aria-labelledby="cleanup-title">
    <div className="sheet-heading">
      <h2 id="cleanup-title">Clean up lessons and notes</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} aria-hidden="true" /></button>
    </div>
    <div className="review-body">
      <p className="setting-note">Tether checks once a week. It removes nothing itself: each Remove button removes one item, and you can undo it for {data?.undo_days ?? 7} days.</p>
      {error && <p className="error" role="alert">{error} <button type="button" className="link-button upkeep-retry" onClick={() => { setFailure(''); void refresh() }}>Try again</button></p>}
      {!data ? !error && <Skeleton label="Loading the clean-up list" className="review-group"><ul className="lesson-list" aria-hidden="true">{[0, 1, 2].map(index => <li key={index} className="lesson"><span className="lesson-text"><strong><BoneLines count={index === 1 ? 1 : 2} from={index} /></strong><small><Bone w="40%" /></small></span></li>)}</ul></Skeleton> : <>
        {empty && <p className="hint upkeep-empty" role="status">{data.last_run ? `Nothing is stale. Last check: ${day(data.last_run)}.` : 'No check ran yet. Tether runs the first one within a minute.'}</p>}
        {!!data.lessons.length && <section className="review-group" aria-labelledby="cleanup-lessons">
          <h3 id="cleanup-lessons">Lessons that never repeated <span className="review-group-count">{data.lessons.length}</span></h3>
          <p className="hint">Saved once, over {data.stale_days} days ago. A lesson that works also does not repeat, so remove only the ones that no longer hold.</p>
          <ul className="lesson-list">{data.lessons.map(lesson => <li key={lesson.key} className="lesson review-lesson">
            <span className="review-main"><LessonText lesson={lesson} now={now} where={lesson.project ? lesson.project_name ?? lesson.project : 'All projects'} /></span>
            <div className="review-actions">
              <button type="button" className="secondary" disabled={!!busy} aria-label={`Keep the lesson: ${lesson.text}`} onClick={() => void send(lesson.key, { action: 'keep', kind: 'lesson', project: lesson.project, id: lesson.id })}><Check size={15} aria-hidden="true" />Keep</button>
              <button type="button" className="secondary upkeep-remove" disabled={!!busy} aria-label={`Remove the lesson: ${lesson.text}`} onClick={() => void send(lesson.key, { action: 'remove', kind: 'lesson', project: lesson.project, id: lesson.id, mark: lesson.mark })}><Trash2 size={15} aria-hidden="true" />Remove</button>
            </div>
          </li>)}</ul>
        </section>}
        {!!data.notes.length && <section className="review-group" aria-labelledby="cleanup-notes">
          <h3 id="cleanup-notes">Notes not changed for {data.note_days} days <span className="review-group-count">{data.notes.length}</span></h3>
          <p className="hint">Agents get the note list with each prompt. A removed note moves to the project’s notes/.trash folder.</p>
          <ul className="lesson-list">{data.notes.map(note => <li key={note.key} className="lesson review-lesson">
            <span className="review-main"><span className="lesson-text"><strong>{note.title}</strong><small>{note.project_name} · {note.name} · last changed {day(note.mtime / 1000)}</small></span></span>
            <div className="review-actions">
              <button type="button" className="secondary" disabled={!!busy} aria-label={`Keep the note: ${note.title}`} onClick={() => void send(note.key, { action: 'keep', kind: 'note', project: note.project, id: note.name })}><Check size={15} aria-hidden="true" />Keep</button>
              <button type="button" className="secondary upkeep-remove" disabled={!!busy} aria-label={`Remove the note: ${note.title}`} onClick={() => void send(note.key, { action: 'remove', kind: 'note', project: note.project, id: note.name, mark: note.mark })}><Trash2 size={15} aria-hidden="true" />Remove</button>
            </div>
          </li>)}</ul>
        </section>}
        {!!data.removed.length && <section className="review-group" aria-labelledby="cleanup-removed">
          <h3 id="cleanup-removed">Removed <span className="review-group-count">{data.removed.length}</span></h3>
          <ul className="lesson-list">{data.removed.map(item => <li key={item.token} className="lesson review-lesson">
            <span className="review-main"><span className="lesson-text"><strong>{item.text}</strong><small>{item.kind === 'lesson' ? 'Lesson' : `Note ${item.name}`} · {item.project_name} · removed {day(item.at)} · undo for {count(daysLeft(item.until, now), 'more day')}</small></span></span>
            <div className="review-actions"><button type="button" className="secondary" disabled={!!busy} aria-label={`Undo the removal of: ${item.text}`} onClick={() => void send(item.token, { action: 'restore', token: item.token })}><Undo2 size={15} aria-hidden="true" />Undo</button></div>
          </li>)}</ul>
        </section>}
        {!!data.skipped.length && <p className="hint">Tether could not read {count(data.skipped.length, 'project')}.</p>}
      </>}
    </div>
    <div className="review-footer">
      <button type="button" className="secondary" disabled={!!busy || !data} onClick={() => void send('check', { action: 'check' })}>Check now</button>
      <button type="button" className="primary" onClick={onClose}>Done</button>
    </div>
  </dialog>
}

/** Settings › Learning & upkeep: the entry to the clean-up sheet, with the counts. */
export function CleanUpSettings({ api }: { api: Api }) {
  const fetcher = useCallback((signal: AbortSignal) => api<PruneState>('upkeep/prune', undefined, signal), [api])
  const { data, error, refresh } = usePolling(fetcher, 120000)
  const [open, setOpen] = useState(false)
  const stale = data ? data.lessons.length + data.notes.length : 0
  return <section className="settings-group" aria-labelledby="settings-cleanup"><h3 id="settings-cleanup">Clean up lessons and notes</h3>
    {!data ? error ? <p className="setting-note" role="alert">{error} <button type="button" className="link-button upkeep-retry" onClick={() => void refresh()}>Try again</button></p>
      : <SettingSkeleton label="Loading the clean-up list"><div className="review-entry"><span><strong><Bone w="9em" /></strong><small><BoneLines count={2} phone={2} /></small></span><Bone w={92} h={36} /></div></SettingSkeleton>
      : <div className="review-entry"><span><strong>{stale ? `${count(data.lessons.length, 'lesson')} and ${count(data.notes.length, 'note')} look stale` : 'Nothing is stale'}</strong>
        <small>Once a week Tether flags lessons that never repeated in {data.stale_days} days and notes not changed for {data.note_days} days. You remove each one yourself, and you can undo it for {data.undo_days} days.{data.removed.length ? ` ${count(data.removed.length, 'removal')} can be undone.` : ''}</small></span>
        <button type="button" className={stale ? 'primary' : 'secondary'} onClick={() => setOpen(true)}><ListChecks size={15} aria-hidden="true" />Review</button></div>}
    {open && <CleanUp api={api} onClose={() => { setOpen(false); void refresh() }} />}
  </section>
}

const RUN_TEXT: Record<string, string> = { started: 'An ops tab started', quiet: 'No tab was needed', failed: 'Not started' }

/** One postmortem note, read-only. */
function Postmortem({ api, run, onClose }: { api: Api; run: OpsRun; onClose: () => void }) {
  const modal = useModal(true, onClose, undefined, { view: 'postmortem' })
  const fetcher = useCallback((signal: AbortSignal) => api<{ text: string; path: string }>(`upkeep/ops/note?project=${encodeURIComponent(run.project)}&day=${encodeURIComponent(run.day)}`, undefined, signal), [api, run.project, run.day])
  const { data, error, refresh } = usePolling(fetcher, 30000)
  return <dialog {...modal} className="sheet lessons-review upkeep-sheet" aria-labelledby="postmortem-title">
    <div className="sheet-heading">
      <h2 id="postmortem-title">Postmortem {run.day}</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X size={18} aria-hidden="true" /></button>
    </div>
    <div className="review-body">
      {error && <p className="error" role="alert">{error} <button type="button" className="link-button upkeep-retry" onClick={() => void refresh()}>Try again</button></p>}
      {!data ? !error && <Skeleton label="Loading the postmortem"><p aria-hidden="true"><BoneLines count={6} phone={8} /></p></Skeleton>
        : <><pre className="upkeep-note" tabIndex={0} aria-label={`Postmortem of ${run.day}`}>{data.text}</pre><p className="hint">{data.path}</p></>}
    </div>
  </dialog>
}

/** Settings › Learning & upkeep: the daily ops report's switch, its last runs and the lessons it proposes. */
export function OpsSettings({ api }: { api: Api }) {
  useTetherZone()
  const fetcher = useCallback((signal: AbortSignal) => api<OpsState>('upkeep/ops', undefined, signal), [api])
  const { data, error, refresh } = usePolling(fetcher, 15000)
  const { busy, failure, act } = useAct(refresh)
  const [note, setNote] = useState<OpsRun | null>(null)
  const send = (key: string, body: Record<string, unknown>) => act(key, () => api('upkeep/ops', body))
  return <section className="settings-group" aria-labelledby="settings-ops"><h3 id="settings-ops">Daily ops report</h3>
    {(failure || (data && error)) && <p className="error" role="alert">{failure || error}</p>}
    {!data ? error ? <p className="setting-note" role="alert">{error} <button type="button" className="link-button upkeep-retry" onClick={() => void refresh()}>Try again</button></p>
      : <SettingSkeleton label="Loading the daily ops report"><SwitchBone lines={3} phone={4} /><NoteBone lines={2} phone={3} /></SettingSkeleton> : <>
      <label className="switch-row"><span><strong>Write a postmortem each day</strong>
        <small>After {String(data.hour).padStart(2, '0')}:00, Tether lists yesterday’s commits and deploys with no verification, paused reviews and reverts in a note. When it finds one, a tab ({data.model}) reads the evidence, writes what to learn and proposes lessons. This uses tokens each day: at most {count(data.tabs, 'tab')}, and none on a day with no problem.</small></span>
        <input type="checkbox" role="switch" checked={data.enabled} disabled={!!busy} onChange={event => void send('enabled', { enabled: event.target.checked })} /></label>
      <div className="review-entry"><span><strong>Run it now</strong><small>Reports on yesterday, also when the switch is off. The notes go to each project’s notes/postmortems folder.</small></span>
        <button type="button" className="secondary" disabled={!!busy || data.busy} onClick={() => void send('run', { action: 'run' })}><Play size={15} aria-hidden="true" />{data.busy ? 'Running…' : 'Run now'}</button></div>
      <h4 className="helper-group-title" id="ops-candidates">Proposed lessons</h4>
      {data.candidates.length ? <ul className="lesson-list" aria-labelledby="ops-candidates">{data.candidates.map(item => <li key={item.project + item.id} className="lesson review-lesson">
        <span className="review-main"><span className="lesson-text">
          <span className="lesson-tags"><span className={`lesson-kind lesson-${item.kind}`}>{item.kind === 'avoid' ? 'Avoid' : 'Keep doing'}</span><span className="lesson-flag upkeep-flag">Proposed</span></span>
          <strong>{item.text}</strong>
          {item.quote && <small className="lesson-quote">Evidence: “{item.quote}”</small>}
          <small>{item.project_name} · {item.tab || 'ops tab'} · {day(item.at)}</small></span></span>
        <div className="review-actions">
          <button type="button" className="secondary" disabled={!!busy} aria-label={`Add the lesson: ${item.text}`} onClick={() => void send(item.id, { action: 'accept', project: item.project, id: item.id })}><Plus size={15} aria-hidden="true" />Add</button>
          <button type="button" className="icon-button" disabled={!!busy} aria-label={`Dismiss the proposed lesson: ${item.text}`} onClick={() => void send(item.id, { action: 'dismiss', project: item.project, id: item.id })}><X size={16} aria-hidden="true" /></button>
        </div>
      </li>)}</ul> : <p className="hint">None. Agents do not get a proposed lesson until you add it.</p>}
      <h4 className="helper-group-title" id="ops-runs">Last runs</h4>
      {data.runs.length ? <ul className="lesson-list" aria-labelledby="ops-runs">{data.runs.map(run => <li key={run.at + run.project} className="lesson review-lesson">
        <span className="review-main"><span className="lesson-text">
          <strong>{run.day}{run.project_name ? ` · ${run.project_name}` : ''}</strong>
          <small>{RUN_TEXT[run.status] ?? run.status}{run.status === 'started' && run.problems ? ` for ${count(run.problems, 'problem')}` : ''}{run.why ? `. ${run.why}` : '.'}</small></span></span>
        {run.note && <div className="review-actions"><button type="button" className="secondary" onClick={() => setNote(run)} aria-label={`Read the postmortem of ${run.day}, ${run.project_name}`}><FileText size={15} aria-hidden="true" />Read</button></div>}
      </li>)}</ul> : <p className="hint">No run yet.</p>}
      {!!data.skipped.length && <p className="hint">Tether could not read {count(data.skipped.length, 'project')}.</p>}
      {note && <Postmortem api={api} run={note} onClose={() => setNote(null)} />}
    </>}
  </section>
}
