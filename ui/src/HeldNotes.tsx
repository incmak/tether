// The "Held for …" chip in Live: topic notes Friday keeps until they are sent or cleared. Tap to read or clear.
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { StickyNote, X } from 'lucide-react'
import { errorText, type Api } from './api'
import { chipLabel, NOTES_PATH as PATH, type HeldTopic } from './fridayNotes'
import { HeldDraft } from './HeldDraft'
type HeldNote = HeldTopic['notes'][number]

// A note is armed by what it says, not by its place: a refresh can shift places under the finger.
const noteKey = (topic: HeldTopic, note: HeldNote) => `${topic.id}\n${note.at}\n${note.text}`
const clearKey = (topic: HeldTopic) => `${topic.id}\nclear`
const FOCUSABLE = 'button:not(:disabled), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'

// `show` asks to put one draft on screen: Friday sends it with a send read-back and when a draft is ready.
type Shown = { id: string; rev: number; at: number }
type Props = { api: Api; topics: HeldTopic[]; refresh: () => Promise<void>; onEmpty?: () => void; show?: Shown | null }

export function HeldNotes({ api, topics, refresh, onEmpty, show }: Props) {
  const [open, setOpen] = useState(false)
  const [arming, setArming] = useState({ key: '', shape: '' })
  const [problem, setProblem] = useState('')
  const [busy, setBusy] = useState(false)
  const chip = useRef<HTMLButtonElement>(null)
  const root = useRef<HTMLDivElement>(null)
  const sheet = useRef<HTMLDivElement>(null)
  // Where focus goes after a remove: the first of these keys still on screen, else the sheet.
  const focusNext = useRef<string[] | null>(null)
  // True while focus is on the chip or in the sheet; both unmount with the last note (no blur event then).
  const focusInside = useRef(false)
  const sheetId = useId()
  const shape = JSON.stringify(topics.map(topic => [topic.id, topic.notes.map(note => [note.at, note.text])]))
  // The draft Friday last put on screen. It opens the sheet once per request, when the list holds that revision.
  const [shown, setShown] = useState<Shown | null>(null)
  const pending = useRef<Shown | null>(null)
  const scrollTo = useRef('')
  const lastRev = (topic: HeldTopic) => topic.draft?.revisions[topic.draft.revisions.length - 1]?.rev ?? 0
  const drafts = topics.map(topic => `${topic.id}:${lastRev(topic)}`).join()

  const close = useCallback((focus = true) => {
    setOpen(false); setArming({ key: '', shape: '' }); setProblem('')
    if (focus) chip.current?.focus()
  }, [])

  // Any change to the list disarms: a second tap must confirm what the user saw armed.
  const armed = arming.shape === shape ? arming.key : ''
  // An empty list closes the sheet, so a later note shows the chip, not an open sheet.
  if (open && !topics.length) { setOpen(false); setProblem('') }

  useLayoutEffect(() => {
    if (busy) return
    if (!topics.length) {
      if (focusNext.current || focusInside.current) onEmpty?.()
      focusNext.current = null; focusInside.current = false
      return
    }
    const keys = focusNext.current
    if (!keys) return
    focusNext.current = null
    const buttons = [...(sheet.current?.querySelectorAll<HTMLElement>('[data-key]') ?? [])]
    const target = keys.map(key => buttons.find(button => button.dataset.key === key)).find(Boolean)
    ;(target ?? sheet.current)?.focus()
  }, [shape, busy, topics.length, onEmpty])

  // A new request waits for a list that has its revision; a refresh is asked for once.
  useEffect(() => {
    if (!show) return
    pending.current = show
    void refresh()
  }, [show, refresh])
  useEffect(() => {
    const want = pending.current
    if (!want) return
    const topic = topics.find(item => item.id === want.id)
    if (!topic || lastRev(topic) < want.rev) return
    pending.current = null
    scrollTo.current = want.id
    setShown(want)
    setOpen(true)
  }, [drafts, topics])
  useLayoutEffect(() => {
    if (!open || !scrollTo.current) return
    const id = scrollTo.current
    scrollTo.current = ''
    const block = [...(sheet.current?.querySelectorAll<HTMLElement>('[data-draft]') ?? [])].find(item => item.dataset.draft === id)
    block?.scrollIntoView({ block: 'nearest' })
  })

  useEffect(() => {
    if (!open) return
    sheet.current?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.stopPropagation(); close(); return }
      // The sheet covers the call controls, so Tab stays inside it until it closes.
      if (event.key !== 'Tab' || !sheet.current) return
      const items = [...sheet.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
      if (!items.length) return
      const first = items[0], last = items[items.length - 1], at = document.activeElement
      const inside = at instanceof Node && sheet.current.contains(at)
      if (event.shiftKey && (!inside || at === first || at === sheet.current)) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (!inside || at === last)) { event.preventDefault(); first.focus() }
    }
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

  // Focus order after a remove: the next note, the one before, this topic's clear button, then the next topics.
  function afterRemove(topicIndex: number, noteIndex?: number) {
    const topic = topics[topicIndex]
    const keys: string[] = []
    if (noteIndex !== undefined) {
      const rest = topic.notes.filter((_, i) => i !== noteIndex)
      const next = rest[noteIndex] ?? rest[noteIndex - 1]
      if (next) keys.push(noteKey(topic, next), clearKey(topic))
    }
    for (const other of [...topics.slice(topicIndex + 1), ...topics.slice(0, topicIndex).reverse()]) {
      if (other.notes[0]) keys.push(noteKey(other, other.notes[0]))
      keys.push(clearKey(other))
    }
    return keys
  }

  async function remove(topicIndex: number, noteIndex?: number) {
    const topic = topics[topicIndex]
    const key = noteIndex === undefined ? clearKey(topic) : noteKey(topic, topic.notes[noteIndex])
    if (armed !== key) { setArming({ key, shape }); setProblem(''); return }
    setBusy(true)
    const next = afterRemove(topicIndex, noteIndex)
    try {
      await api(`${PATH}/drop`, noteIndex === undefined
        ? { id: topic.id, count: topic.notes.length, texts: topic.notes.map(item => item.text) }
        : { id: topic.id, note: noteIndex + 1, text: topic.notes[noteIndex].text })
      setArming({ key: '', shape: '' }); setProblem('')
      focusNext.current = next
    } catch (failure) {
      // A failed delete disarms too: the next delete starts again from one tap that only arms.
      setArming({ key: '', shape: '' }); setProblem(errorText(failure))
      // The tapped button was disabled while busy; bring focus back to it, or its neighbour if it is gone.
      focusNext.current = [key, ...next]
    } finally {
      await refresh()
      setBusy(false)
    }
  }

  return <div ref={root} className="live-held" data-no-drag onFocus={() => { focusInside.current = true }}
    onBlur={event => { focusInside.current = !!root.current?.contains(event.relatedTarget as Node | null) }}>
    <button ref={chip} type="button" className="live-held-chip" aria-expanded={open} aria-controls={open ? sheetId : undefined}
      aria-label={`${lead}: ${count} ${count === '1' ? 'note' : 'notes'}. Show held notes.`} onClick={() => open ? close() : setOpen(true)}>
      <StickyNote size={14} aria-hidden="true" /><span className="live-held-lead">{lead}</span><b>{count}</b>
    </button>
    {open && <div ref={sheet} id={sheetId} className="live-held-sheet" role="dialog" aria-modal="true" aria-label="Held notes" tabIndex={-1}>
      {/* The title, close button and any error stay above the scrolling notes, never over them. */}
      <div className="live-held-top">
        <header><h3>Held notes</h3><button type="button" className="live-held-x" aria-label="Close held notes" onClick={() => close()}><X size={16} aria-hidden="true" /></button></header>
        {problem && <p className="live-held-problem" role="alert">{problem}</p>}
      </div>
      <div className="live-held-body">
      <p className="live-held-hint">Friday keeps these across calls until you send or clear them.</p>
      {topics.map((topic, topicIndex) => <section key={topic.id} aria-label={topic.title}>
        <h4><span>{topic.title}</span>{topic.chat && <small>for {topic.chat}</small>}</h4>
        <ol>
          {topic.notes.map((note, index) => {
            const key = noteKey(topic, note), on = armed === key
            return <li key={`${note.at}-${index}`}>
              <span>{note.text}</span>
              <button type="button" className="live-held-remove" data-key={key} data-armed={on} disabled={busy}
                aria-label={on ? `Confirm: remove note ${index + 1}` : `Remove note ${index + 1}`}
                onClick={() => void remove(topicIndex, index)}>{on ? 'Remove' : <X size={14} aria-hidden="true" />}</button>
            </li>
          })}
        </ol>
        {topic.draft && topic.draft.state !== 'closed' && <HeldDraft topicId={topic.id} draft={topic.draft}
          shown={shown?.id === topic.id && shown.rev === lastRev(topic)} />}
        <button type="button" className="live-held-clear" data-key={clearKey(topic)} data-armed={armed === clearKey(topic)} disabled={busy}
          onClick={() => void remove(topicIndex)}>
          {armed === clearKey(topic) ? `Tap again to clear ${topic.notes.length} ${topic.notes.length === 1 ? 'note' : 'notes'}` : 'Clear topic'}
        </button>
      </section>)}
      </div>
    </div>}
  </div>
}
