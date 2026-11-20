// The coordinator tab of a tmux session (server/coordinator.py, B115/B296): it takes the user's tasks, gives them to the
// other tabs, and gets a short [Tether status] message when a tab finishes, fails or needs the user.
import { Check } from 'lucide-react'
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

/** May this tab be the coordinator? Claude and Codex tabs that are not helper tabs. */
export const canCoordinate = (pane: Pane) => (pane.agent === 'claude' || pane.agent === 'codex') && !pane.role && pane.writable

export function CoordinatorPicker({ pane, panes, pending, onPick, onBack }: {
  pane: Pane; panes: Pane[]; pending: boolean
  onPick: (mode: CoordinatorMode | null, take: boolean) => void; onBack: () => void
}) {
  const current = pane.coordinator ?? null
  const other = panes.find(item => item.id !== pane.id && item.tmux_session === pane.tmux_session && item.coordinator)
  return <div className="coordinator-picker">
    <p className="hint">The coordinator takes your tasks, gives them to the other tabs of this session, and tells you about them.</p>
    <div className="action-list" role="radiogroup" aria-label="Coordinator updates">
      {CHOICES.map(([mode, label, detail]) => <button type="button" key={mode ?? 'none'} role="radio" aria-checked={current === mode}
          disabled={pending || (mode === null && current === null)} onClick={() => { if (mode !== current) onPick(mode, !current && !!other) }}>
        <span className="coordinator-choice"><span>{label}</span>{detail && <small>{detail}</small>}</span>
        {current === mode && <Check size={16} aria-hidden="true" />}
      </button>)}
    </div>
    {!current && <p className="hint">{other
      ? <><strong>{paneTitle(other)}</strong> is the coordinator now. A choice here moves the role to this tab.</>
      : null} Tether sends this tab the coordinator rules.</p>}
    <div className="sheet-buttons"><button type="button" className="secondary" disabled={pending} onClick={onBack}>Back</button></div>
  </div>
}
