import { CornerUpLeft } from 'lucide-react'
import type { Pane } from './api'
import { paneTitle } from './Sidebar'
import './HelperTabs.css'

/** "Opened by <tab>" under the chat header of a tab that another tab started: a link to that tab while it is open. */
export function OpenedBy({ pane, panes, onOpen }: { pane: Pane; panes: Pane[]; onOpen: (pane: Pane) => void }) {
  const by = pane.opened_by
  if (!by) return null
  const opener = by.live ? panes.find(item => item.id === by.pane) : undefined
  const name = opener ? paneTitle(opener) : by.name
  return <p className="opened-by">
    <CornerUpLeft size={13} aria-hidden="true" />
    {opener
      ? <>Opened by <button type="button" className="opened-by-link" onClick={() => onOpen(opener)} title={`Open ${name}`}>{name}</button></>
      : <span>Opened by {name ? <>{name} <span className="opened-by-gone">(closed)</span></> : 'a tab that is closed'}</span>}
  </p>
}
