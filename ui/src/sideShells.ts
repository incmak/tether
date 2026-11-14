import type { Api } from './api'
import type { DesktopLayout, PanelLayout } from './prefs'
import { panelCols, removePanel } from './workspace'

/** One shell window in Tether's own tmux session "tether-shells" (server: side_shells.list_shells).
 * idle: only the shell runs (nothing would stop). monitor: a btop window for Host resources; folder is ''. */
export interface SideShell { pane: string; folder: string; command: string; idle: boolean; monitor: boolean }

export const listShells = (api: Api, priority?: RequestPriority) => api<{ shells: SideShell[] }>('side-shells', undefined, undefined, undefined, undefined, priority).then(data => Array.isArray(data.shells) ? data.shells : [])
export const endShell = (api: Api, pane: string) => api('side-shell/end', { pane })
/** idle: only shells with nothing running. all: every shell. Returns the panes that ended. */
export const endShells = (api: Api, which: 'idle' | 'all') => api<{ panes?: string[] }>('side-shell/end', { which }).then(data => Array.isArray(data.panes) ? data.panes : [])

/** The folder's last part, for a row title. */
export const shellName = (shell: SideShell) => shell.monitor ? 'Host resources' : shell.folder.replace(/\/+$/, '').split('/').pop() || shell.folder || 'Shell'

/** Take out every terminal panel that shows one of these ended shells. */
export function withoutShells(desk: DesktopLayout, panes: string[]): Partial<PanelLayout> {
  const cols = panelCols(desk)
  const gone = cols.flatMap(col => col.items).filter(item => item.kind === 'terminal' && item.pane && panes.includes(item.pane))
  return { cols: gone.reduce((left, item) => removePanel(left, item.id), cols) }
}

// Other views (the panel X, a new panel) tell the sidebar list to look again now, not at its next poll.
export const SHELLS_CHANGED = 'tether-shells-changed'
export const shellsChanged = () => window.dispatchEvent(new Event(SHELLS_CHANGED))
