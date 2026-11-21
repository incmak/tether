import { useContext, useId, useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown, CircleAlert, CircleCheck, Hand, MessageCircleQuestion, Radio } from 'lucide-react'
import type { StatusTab } from './api'
import { FileLink, TabOpener } from './MediaLink'
import { stamp } from './messageTime'
import type { StatusUpdate, TabUpdate, TabUpdateState } from './statusText'

// The words the card shows for each state, and the order of the rows: what needs the person comes first.
const STATE: Record<TabUpdateState, { word: string; rank: number; Icon: typeof Hand }> = {
  needs: { word: 'Waits on you', rank: 0, Icon: Hand },
  asks: { word: 'Asks you', rank: 1, Icon: MessageCircleQuestion },
  failed: { word: 'Failed', rank: 2, Icon: CircleAlert },
  done: { word: 'Finished', rank: 3, Icon: CircleCheck },
}

// A pane id can be reused. A link follows the reported chat within its original session.
function useTabOf(tab?: StatusTab) {
  const tabs = useContext(TabOpener)
  if (!tabs || !tab || tab.thread === null) return null
  const matches = tabs.panes.filter(pane => pane.id !== tabs.current.id && pane.tmux_session === tab.session && pane.thread === tab.thread)
  return matches.length === 1 ? () => tabs.open(matches[0]) : null
}

function StatusRow({ update, tab }: { update: TabUpdate; tab?: StatusTab }) {
  const { word, Icon } = STATE[update.state]
  const open = useTabOf(tab)
  const textId = useId()
  const textRef = useRef<HTMLSpanElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [clipped, setClipped] = useState(false)
  // The expand button shows only when the one-line summary is cut off at this width.
  useLayoutEffect(() => {
    const element = textRef.current
    if (!element || expanded) return
    const measure = () => setClipped(element.scrollWidth > element.clientWidth + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [expanded, update.text])
  const name = <span className="status-name">{update.name}</span>
  return <li className={`status-row ${update.state}`}>
    <Icon className="status-icon" size={15} aria-hidden="true" />
    <span className="status-head">
      {open ? <FileLink open={open} label={`Open the tab ${update.name}`}>{name}</FileLink> : name}
      <span className="status-state">{word}</span>
    </span>
    {update.text && <span ref={textRef} id={textId} className={`status-text ${expanded ? 'expanded' : ''}`}>{update.text}</span>}
    {update.text && (clipped || expanded) && <button type="button" className="status-more" aria-expanded={expanded} aria-controls={textId}
      aria-label={`${expanded ? 'Show less' : 'Show all'} of the ${update.name} summary`} onClick={() => setExpanded(value => !value)}>
      <ChevronDown size={15} aria-hidden="true" />
    </button>}
  </li>
}

// The folded line counts what needs the person; "finished" rows only add to the total.
const COUNTS: [TabUpdateState, string][] = [['needs', 'waits on you'], ['asks', 'asks you'], ['failed', 'failed']]

/** Tether's "[Tether status]" prompt in the coordinator tab. It starts folded to one line; open it for one row per tab,
 *  what needs the person first. */
export function StatusCard({ id, status, time, tabs }: { id: string; status: StatusUpdate; time?: string; tabs?: StatusTab[] }) {
  const [open, setOpen] = useState(false)
  const listId = useId()
  const rows = status.tabs.map((update, at) => ({ update, at })).sort((a, b) => STATE[a.update.state].rank - STATE[b.update.state].rank || a.at - b.at)
  const targets = tabs?.length === rows.length ? tabs : undefined
  const counts = COUNTS.map(([state, words]) => ({ state, n: status.tabs.filter(update => update.state === state).length, words })).filter(item => item.n)
  const waiting = counts.find(item => item.state === 'needs')?.n ?? 0
  const when = stamp(time)
  const total = `${status.tabs.length} tab ${status.tabs.length === 1 ? 'update' : 'updates'}`
  return <section className={`status-card ${waiting ? 'has-needs' : ''}`} data-msg-id={id}
    aria-label={`Tether status: ${total}${counts.map(item => `, ${item.n} ${item.words}`).join('')}`}>
    <button type="button" className="status-toggle" aria-expanded={open} aria-controls={listId} onClick={() => setOpen(value => !value)}>
      <Radio className="status-toggle-icon" size={13} aria-hidden="true" />
      <span className="status-summary">
        <span>Tether status · {total}</span>
        {counts.map(item => <span key={item.state} className={`status-count ${item.state}`}>
          {item.state === 'needs' && <Hand size={13} aria-hidden="true" />}{item.n} {item.words}
        </span>)}
      </span>
      {when && <time dateTime={time}>{when}</time>}
      <ChevronDown className="status-chevron" size={15} aria-hidden="true" />
    </button>
    <ul id={listId} hidden={!open}>{rows.map(({ update, at }) => <StatusRow key={at} update={update} tab={targets?.[at]} />)}</ul>
  </section>
}
