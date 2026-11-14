import { useState } from 'react'
import { Globe, Pencil, Trash2 } from 'lucide-react'
import { Loading } from './Skeleton'
import { errorText } from './api'
import type { Api } from './api'
import { withApproval } from './approval'
import { useAutonomy } from './Autonomy'
import type { Learning, Lesson } from './Autonomy'

// Learning from feedback (server: lessons.py, agent_lessons_cli.py). When the user corrects an agent, the agent writes
// an "avoid" lesson; when the user confirms a choice, a "keep" lesson. The autonomy hook gives the best lessons to
// Claude and Codex with each prompt. Agents write project lessons only; a lesson for all projects needs a passkey.

const DAY = 24 * 60 * 60
const SOURCE: Record<Lesson['source'], string> = { user: 'From your feedback', reviewer: 'From a review', agent: 'From the agent' }
const AGENT: Record<string, string> = { claude: 'Claude', codex: 'Codex' }

function when(seconds: number) {
  return new Date(seconds * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

function meta(lesson: Lesson) {
  const parts = [SOURCE[lesson.source] ?? 'From the agent']
  if (AGENT[lesson.agent]) parts.push(`written by ${AGENT[lesson.agent]}`)
  parts.push(lesson.hits > 1 ? `${lesson.hits} times, last ${when(lesson.last_hit)}` : when(lesson.created))
  return parts.join(' · ')
}

/** Settings › Agents: the switch and the lessons for all projects. */
export function LearningSettings({ api }: { api: Api }) {
  const { data, error, refresh } = useAutonomy(api)
  const [busy, setBusy] = useState('')
  const [failure, setFailure] = useState('')
  const learning = data?.learning
  const [now] = useState(() => Date.now() / 1000)
  async function send(key: string, body: Record<string, unknown>) {
    setBusy(key); setFailure('')
    try { await api('autonomy/lessons', body); await refresh() }
    catch (caught) { setFailure(errorText(caught)) }
    finally { setBusy('') }
  }
  return <section className="settings-group" aria-labelledby="settings-learning"><h3 id="settings-learning">Learning from feedback</h3>
    {(failure || (data && error)) && <p className="error" role="alert">{failure || error}</p>}
    {!learning ? error && !data ? <p className="setting-note" role="alert">{error}</p> : <Loading className="setting-note" /> : <>
      <label className="switch-row"><span><strong>Learn from your feedback</strong>
        <small>When you correct an agent, it saves a lesson to avoid the mistake. When you confirm a choice, it saves a lesson to keep doing it. Claude and Codex get the lessons of the project with each prompt.</small></span>
        <input type="checkbox" role="switch" checked={learning.enabled} disabled={!!busy} onChange={event => void send('enabled', { enabled: event.target.checked })} /></label>
      <p className="setting-note">Lessons are hints. They never give agents more power, and your request and the safety rules always win. Each project lists its lessons in Projects, where you can edit or remove them.</p>
      <h4 className="helper-group-title" id="lessons-global">Lessons for all projects</h4>
      {learning.global.length ? <ul className="lesson-list" aria-labelledby="lessons-global">{learning.global.map(lesson => <li key={lesson.id} className="lesson">
        <LessonText lesson={lesson} now={now} />
        <div className="lesson-actions"><button type="button" className="icon-button" aria-label={`Remove the lesson: ${lesson.text}`} disabled={!!busy} onClick={() => void send(lesson.id, { remove_global: lesson.id })}><Trash2 size={16} aria-hidden="true" /></button></div>
      </li>)}</ul> : <p className="hint">None yet. In a project, choose “Use in all projects” on a lesson to add it here.</p>}
    </>}
  </section>
}

function LessonText({ lesson, now }: { lesson: Lesson; now: number }) {
  const fresh = now - lesson.created < DAY
  return <span className="lesson-text">
    <span className="lesson-tags"><span className={`lesson-kind lesson-${lesson.kind}`}>{lesson.kind === 'avoid' ? 'Avoid' : 'Keep doing'}</span>
      {fresh && <span className="lesson-flag">New</span>}
      {lesson.stuck && <span className="lesson-flag lesson-stuck">Did not stick</span>}</span>
    <strong>{lesson.text}</strong><small>{meta(lesson)}</small>
    {lesson.stuck && <small className="lesson-hint">You corrected this {lesson.hits} times. A rule in the project's AGENTS.md or CLAUDE.md is stronger than a lesson.</small>}
  </span>
}

/** The Project sheet: the lessons agents saved in this project. */
export function ProjectLessons({ api, project, learning, onChange }: { api: Api; project: string; learning: Learning; onChange: () => void }) {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)
  const lessons = learning.lessons ?? []
  const [now] = useState(() => Date.now() / 1000)
  async function send(id: string, body: Record<string, unknown>, approval = false) {
    setBusy(id); setError('')
    try {
      if (approval) await withApproval(api, 'autonomy/lessons', 'lesson-promote', body)
      else await api('autonomy/lessons', body)
      setEditing(null); onChange()
    } catch (failure) { setError(errorText(failure)) }
    finally { setBusy('') }
  }
  return <section className="settings-group" aria-labelledby="project-lessons"><h3 id="project-lessons">Lessons</h3>
    <p className="setting-note">{learning.enabled ? 'Agents saved these from your feedback in this project. They get the most important ones with each prompt.' : 'Learning is off in Settings › Agents, so agents do not get these lessons now.'}</p>
    {error && <p className="error" role="alert">{error}</p>}
    {lessons.length ? <ul className="lesson-list">{lessons.map(lesson => <li key={lesson.id} className="lesson">
      {editing?.id === lesson.id ? <form className="lesson-edit" onSubmit={event => { event.preventDefault(); void send(lesson.id, { project, edit: lesson.id, text: editing.text }) }}>
        <label htmlFor={`lesson-edit-${lesson.id}`} className="visually-hidden">Lesson text</label>
        <textarea id={`lesson-edit-${lesson.id}`} value={editing.text} maxLength={200} rows={3} autoFocus onChange={event => setEditing({ id: lesson.id, text: event.target.value })} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setEditing(null) } }} />
        <small className="hint">Start with “When”. One line, up to 200 characters.</small>
        <div className="lesson-edit-actions"><button type="button" onClick={() => setEditing(null)}>Cancel</button><button type="submit" className="primary" disabled={!!busy || !editing.text.trim()}>Save</button></div>
      </form> : <>
        <LessonText lesson={lesson} now={now} />
        <div className="lesson-actions">
          <button type="button" className="icon-button" aria-label={`Edit the lesson: ${lesson.text}`} disabled={!!busy} onClick={() => setEditing({ id: lesson.id, text: lesson.text })}><Pencil size={16} aria-hidden="true" /></button>
          <button type="button" className="icon-button" aria-label={`Use in all projects: ${lesson.text}`} title="Use in all projects" disabled={!!busy} onClick={() => void send(lesson.id, { project, promote: lesson.id }, true)}><Globe size={16} aria-hidden="true" /></button>
          <button type="button" className="icon-button" aria-label={`Remove the lesson: ${lesson.text}`} disabled={!!busy} onClick={() => void send(lesson.id, { project, remove: lesson.id })}><Trash2 size={16} aria-hidden="true" /></button>
        </div>
      </>}
    </li>)}</ul> : <p className="hint">No lessons yet. When you correct an agent here, or confirm a choice it made, it saves a lesson.</p>}
  </section>
}
