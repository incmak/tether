import { useContext } from 'react'
import { ChevronRight, CornerUpLeft } from 'lucide-react'
import type { Pane } from './api'
import { HelperFoldToggle, helperCount } from './helperTabs'
import type { HelperFold } from './helperTabs'
import { paneTitle } from './Sidebar'
import './HelperTabs.css'

/**
 * "3 helpers" after the tab that owns them: shows or folds the agent helper tabs that "Hide agent helper tabs" hides.
 * `variant` picks the tab strip or the sidebar look. A helper that needs you shows by itself, so the count has no badge.
 */
export function HelperFoldButton({ fold, owner, variant }: { fold: HelperFold; owner?: Pane; variant: 'strip' | 'list' }) {
  const toggle = useContext(HelperFoldToggle)
  const count = helperCount(fold.panes.length)
  const working = fold.panes.some(pane => pane.state === 'Working')
  const of = owner ? ` of ${paneTitle(owner)}` : ''
  const label = `${fold.open ? 'Fold' : 'Show'} ${fold.panes.length === 1 ? 'the helper tab' : `${fold.panes.length} helper tabs`}${of}`
  return <button type="button" className={`helper-fold ${variant}${fold.open ? ' open' : ''}`} data-helper-fold={fold.key} aria-expanded={fold.open}
    aria-label={`${label}${working ? ', working' : ''}`} title={label} onClick={() => toggle(fold.key)}>
    <ChevronRight size={12} className="chevron" aria-hidden="true" />
    <span>{count}</span>
    {working && <span className="state-dot working" aria-hidden="true" />}
  </button>
}

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
