import { useState } from 'react'
import { Activity, Maximize2, X } from 'lucide-react'
import type { Pane } from './api'
import { runningSummary } from './Sidebar'
import type { Session } from './Sidebar'
import { AgentTabs, agentLists, firstTab } from './AgentsView'
import type { AgentTab } from './AgentsView'
import { useDockedBubble } from './dockedBubble'

// Answers "which agents run now?" from any screen. At rest it is a small D-shaped tab docked flat against a side edge.
// It shows "active | unread": agents that work or wait on you, then finished turns you have not seen.
// Tap: it grows into a bubble and a popover shows the two lists as tabs; on close it docks again.
// Drag: the bubble follows the finger; on release it docks on the nearer edge. The place is kept on this device.
export function AgentBubble({ sessions, seen, selected, onOpen, onHide, onMarkRead, onPage }: { sessions: Session[]; seen: Record<string, number>; selected?: string; onOpen: (pane: Pane) => void; onHide: () => void; onMarkRead: (panes: string[]) => void; onPage: () => void }) {
  const { place, docked, dragging, open, closing, button, pop, shape, popStyle, close, down, move, up, click } = useDockedBubble('tether.agent-bubble.v1', { side: 'right', y: 0.45 })
  const [tab, setTab] = useState<AgentTab>('active')

  const lists = agentLists(sessions, seen, selected)
  const running = lists.active, unread = lists.unread.length
  const waiting = lists.waiting
  const working = running.length - waiting
  const tone = waiting ? 'attention' : working ? 'working' : unread ? 'unread' : 'idle'

  const summary = runningSummary(running)
  const label = `Agents: ${summary}${unread ? `, ${unread} unread` : ''}`

  return <>
    <button ref={button} type="button" className={`agent-bubble ${tone} ${place.side} ${docked ? 'docked' : ''} ${dragging ? 'dragging' : ''} ${open ? 'open' : ''}`} style={shape}
      aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={open}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onClick={event => click(event, () => setTab(firstTab(lists)))}>
      <span className="agent-bubble-ring" aria-hidden="true" />
      {running.length || unread
        ? <span key={docked ? 'tab' : 'bubble'} className="agent-bubble-counts" aria-hidden="true"><span className="agent-bubble-count">{running.length}</span>{unread > 0 && <><span className="agent-bubble-sep" /><span className="agent-bubble-count unread">{unread}</span></>}</span>
        : <Activity key={docked ? 'tab' : 'bubble'} size={docked ? 14 : 20} aria-hidden="true" />}
    </button>
    {open && <div ref={pop} className={`agent-pop ${closing ? 'closing' : ''}`} style={popStyle} role="dialog" aria-label="Agents" tabIndex={-1}>
      <header className="agent-pop-head">
        <span><strong>Agents</strong><small>{summary}</small></span>
        <button type="button" className="icon-button" aria-label="Open the Agents page" title="Open the Agents page" onClick={() => { close(false); onPage() }}><Maximize2 size={16} /></button>
        <button type="button" className="icon-button" aria-label="Close agents" onClick={() => close()}><X size={18} /></button>
      </header>
      <div className="agent-pop-list">
        <AgentTabs sessions={sessions} seen={seen} selected={selected} tab={tab} onTab={setTab} onOpen={pane => { close(false); onOpen(pane) }} onMarkRead={onMarkRead} />
      </div>
      <footer className="agent-pop-foot">
        <button type="button" className="agent-pop-hide" title="Turn it back on in Settings › Appearance" onClick={() => { close(false); onHide() }}>Hide bubble</button>
      </footer>
    </div>}
  </>
}
