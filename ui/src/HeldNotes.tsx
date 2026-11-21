// The "Held for …" chip in Live: topic notes Friday keeps until they are sent or cleared. Tap to read or clear.
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { StickyNote, X } from 'lucide-react'
import { errorText, type Api } from './api'
import { chipLabel, NOTES_PATH as PATH, type HeldTopic } from './fridayNotes'

type Pending = { topic: string; note?: number } | null

export function HeldNotes({ api, topics, refresh }: { api: Api; topics: HeldTopic[]; refresh: () => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [armed, setArmed] = useState<Pending>(null)
  const [problem, setProblem] = useState('')
  const [busy, setBusy] = useState(false)
  const chip = useRef<HTMLButtonElement>(null)
  const sheet = useRef<HTMLDivElement>(null)
  const sheetId = useId()

  const close = useCallback((focus = true) => {
    setOpen(false); setArmed(null); setProblem('')
    if (focus) chip.current?.focus()
  }, [])

  useEffect(() => {
    if (!open) return
    sheet.current?.focus()
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); close() } }
    const away = (event: PointerEvent) => {
      const target = event.target as Node
      if (!sheet.current?.contains(target) && !chip.current?.contains(target)) close(false)
    }
    window.addEventListener('keydown', key, true)
    document.addEventListener('pointerdown', away)
    return () => { window.removeEventListener('keydown', key, true); document.removeEventListener('pointerdown', away) }
  }, [open, close])

  if (!topics.length) return null
  const { lead, count } = chipLabel(topics)
  const isArmed = (topic: string, note?: number) => armed?.topic === topic && armed.note === note

  async function remove(topic: HeldTopic, note?: number) {
    if (!isArmed(topic.id, note)) { setArmed({ topic: topic.id, note }); setProblem(''); return }
    setBusy(true)
    try {
      await api(`${PATH}/drop`, note === undefined ? { id: topic.id } : { id: topic.id, note: note + 1, text: topic.notes[note].text })
      setArmed(null); setProblem('')
      const last = topics.length === 1 && (note === undefined || topic.notes.length === 1)
      if (last) close(false)
    } catch (failure) {
      setProblem(errorText(failure))
    } finally {
      setBusy(false)
      await refresh()
    }
  }

  return <div className="live-held" data-no-drag>
    <button ref={chip} type="button" className="live-held-chip" aria-expanded={open} aria-controls={open ? sheetId : undefined}
      aria-label={`${lead}: ${count} ${count === '1' ? 'note' : 'notes'}. Show held notes.`} onClick={() => open ? close() : setOpen(true)}>
      <StickyNote size={14} aria-hidden="true" /><span className="live-held-lead">{lead}</span><b>{count}</b>
    </button>
    {open && <div ref={sheet} id={sheetId} className="live-held-sheet" role="dialog" aria-label="Held notes" tabIndex={-1}>
      <header><h3>Held notes</h3><button type="button" className="live-held-x" aria-label="Close held notes" onClick={() => close()}><X size={16} aria-hidden="true" /></button></header>
      <p className="live-held-hint">Friday keeps these across calls until you send or clear them.</p>
      {topics.map(topic => <section key={topic.id} aria-label={topic.title}>
        <h4><span>{topic.title}</span>{topic.chat && <small>for {topic.chat}</small>}</h4>
        <ol>
          {topic.notes.map((note, index) => <li key={`${note.at}-${index}`}>
            <span>{note.text}</span>
            <button type="button" className="live-held-remove" data-armed={isArmed(topic.id, index)} disabled={busy}
              aria-label={isArmed(topic.id, index) ? `Confirm: remove note ${index + 1}` : `Remove note ${index + 1}`}
              onClick={() => void remove(topic, index)}>{isArmed(topic.id, index) ? 'Remove' : <X size={14} aria-hidden="true" />}</button>
          </li>)}
        </ol>
        <button type="button" className="live-held-clear" data-armed={isArmed(topic.id)} disabled={busy} onClick={() => void remove(topic)}>
          {isArmed(topic.id) ? `Tap again to clear ${topic.notes.length} ${topic.notes.length === 1 ? 'note' : 'notes'}` : 'Clear topic'}
        </button>
      </section>)}
      {problem && <p className="live-held-problem" role="alert">{problem}</p>}
    </div>}
  </div>
}
