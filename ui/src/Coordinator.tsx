// The coordinator tab of a tmux session (server/coordinator.py, B115/B296): it takes the user's tasks, gives them to the
// other tabs, and gets a short [Tether status] message when a tab finishes, fails or needs the user.
import { Check } from 'lucide-react'
import { useEffect, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import type { Pane } from './api'
import { paneTitle } from './Sidebar'

export type CoordinatorMode = 'all' | 'needs' | 'off'

// The value null is "not the coordinator".
const CHOICES: [CoordinatorMode | null, string, string][] = [
  ['all', 'All updates', 'A tab finished, failed or needs you.'],
  ['needs', 'Only when it matters', 'A tab failed or needs you.'],
  ['off', 'No updates', 'It routes tasks and reports only when you ask.'],
  [null, 'Not the coordinator', ''],
]

export const coordinatorLabel = (mode?: CoordinatorMode | null) => CHOICES.find(item => item[0] === (mode ?? null))?.[1] ?? ''

/** May this tab be the coordinator? Claude and Codex tabs that are not reviewer, advisor or helper tabs (server: coordinator.browser_role). */
export const canCoordinate = (pane: Pane) => (pane.agent === 'claude' || pane.agent === 'codex') && !pane.role && !pane.helper && pane.writable

/**
 * One choice writes at once, so this is a menu of radio items (WAI-ARIA menu pattern): the arrow keys move focus,
 * Enter or Space picks. A radio group would pick on each arrow press. `thread` is the chat the picker opened for:
 * a write never goes to a newer chat of the same tab. While a write runs, items are aria-disabled, not disabled,
 * so focus stays on the item.
 */
export function CoordinatorPicker({ pane, panes, thread, pending, error, onPick, onBack }: {
  pane: Pane; panes: Pane[]; thread: string | null; pending: boolean; error: string
  onPick: (mode: CoordinatorMode | null, take: boolean) => void; onBack: () => void
}) {
  const current = pane.coordinator ?? null
  const stale = pane.thread !== thread
  const other = panes.find(item => item.id !== pane.id && item.tmux_session === pane.tmux_session && item.coordinator)
  const list = useRef<HTMLDivElement>(null)
  // With no role yet, focus starts on the first choice; the checked "Not the coordinator" row shows the state only.
  const start = current === null ? 0 : CHOICES.findIndex(([mode]) => mode === current)
  useEffect(() => { list.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]')[start]?.focus() }, [start])
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = [...(list.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]') ?? [])]
    const at = items.indexOf(document.activeElement as HTMLElement)
    const next = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[event.key]
    if (next === undefined || !items.length) return
    event.preventDefault()
    event.stopPropagation()  // the popover moves focus on the arrow keys too (usePopover)
    items[(next + items.length) % items.length].focus()
  }
  return <div className="coordinator-picker">
    <p className="hint">The coordinator takes your tasks, gives them to the other tabs of this session, and tells you about them.</p>
    <div className="action-list" role="menu" aria-label="Coordinator updates" ref={list} onKeyDown={move}>
      {CHOICES.map(([mode, label, detail], index) => <button type="button" key={mode ?? 'none'} role="menuitemradio" aria-checked={current === mode}
          tabIndex={index === start ? 0 : -1} aria-disabled={pending || stale || mode === current || undefined} aria-busy={pending || undefined}
          onClick={() => { if (!pending && !stale && mode !== current) onPick(mode, !current && !!other) }}>
        <span className="coordinator-choice"><span>{label}</span>{detail && <small>{detail}</small>}</span>
        {current === mode && <Check size={16} aria-hidden="true" />}
      </button>)}
    </div>
    {/* Under the choices, so a phone shows it next to the item that failed. */}
    {error && <p className="error" role="alert">{error}</p>}
    {stale
      ? <p className="hint" role="status">This tab started a new chat. Go back and open Coordinator again.</p>
      : !current && <p className="hint">{other
        ? <><strong className="coordinator-name">{paneTitle(other)}</strong> is the coordinator now. A choice here moves the role to this tab.</>
        : null} Tether sends this tab the coordinator rules.</p>}
    <div className="sheet-buttons"><button type="button" className="secondary" aria-disabled={pending || undefined} onClick={() => { if (!pending) onBack() }}>Back</button></div>
  </div>
}
