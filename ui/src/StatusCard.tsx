import { useContext, useId, useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown, CircleAlert, CircleCheck, Hand, MessageCircleQuestion, Radio } from 'lucide-react'
import type { Pane } from './api'
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

// tmux reuses pane ids: the id opens a tab only while that tab still has the same name. Else the name alone, when one tab has it.
function useTabOf(update: TabUpdate) {
  const tabs = useContext(TabOpener)
  if (!tabs) return null
  const others = tabs.panes.filter(pane => pane.id !== tabs.current.id)
  const named = (pane: Pane) => [pane.pane_name, pane.agent_name, pane.label].some(name => !!name && name.trim() === update.name.trim())
  const byId = others.find(pane => pane.id === update.pane)
  const byName = others.filter(named)
  const pane = byId && named(byId) ? byId : byName.length === 1 ? byName[0] : null
  return pane ? () => tabs.open(pane) : null
}

function StatusRow({ update }: { update: TabUpdate }) {
  const { word, Icon } = STATE[update.state]
  const open = useTabOf(update)
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

/** Tether's "[Tether status]" prompt in the coordinator tab: one row per tab, what needs the person first. */
export function StatusCard({ id, status, time }: { id: string; status: StatusUpdate; time?: string }) {
  const rows = status.tabs.map((update, at) => ({ update, at })).sort((a, b) => STATE[a.update.state].rank - STATE[b.update.state].rank || a.at - b.at)
  const waiting = status.tabs.filter(update => update.state === 'needs').length
  const when = stamp(time)
  const count = `${status.tabs.length} ${status.tabs.length === 1 ? 'tab' : 'tabs'}`
  return <section className="status-card" data-msg-id={id} aria-label={`Tether status: ${count}${waiting ? `, ${waiting} waiting on you` : ''}`}>
    <p className="status-title"><Radio size={13} aria-hidden="true" /><span>Tether status · {count}</span>{when && <time dateTime={time}>{when}</time>}</p>
    <ul>{rows.map(({ update, at }) => <StatusRow key={at} update={update} />)}</ul>
  </section>
}
