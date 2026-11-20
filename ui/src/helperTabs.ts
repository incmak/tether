import { createContext } from 'react'
import type { Pane } from './api'
import type { ArchivedTab } from './prefs'

// Helper tabs (B400): reviewer, advisor and worker tabs that an agent opened for its own work (server row `helper`).
// With "Hide agent helper tabs" on, they leave the strip, the sidebar and the deck, and fold into a count next to the
// tab that owns them (server row `owner`). A helper shows again while it needs you, while it is the open tab, or when
// you chose "Always show this tab". Tabs you place or open yourself never get `helper` from the server.

/** The prefs this module reads. `shownHelpers` binds a tab like an archived tab: pane id, conversation and folder. */
export interface HelperPrefs { hideHelpers: boolean; shownHelpers: Record<string, ArchivedTab> }

/** The tab waits on you: a prompt on screen, an ask ("Needs you: yes", Urgent), or an escalated review. */
export function needsMak(pane: Pane): boolean {
  return pane.state === 'Approval' || pane.state === 'Input needed' || !!pane.ask || !!pane.escalated
}

/** You chose "Always show this tab". tmux reuses pane ids after a restart, so the conversation and folder must match too. */
export function alwaysShown(pane: Pane, map: Record<string, ArchivedTab>): boolean {
  const entry = map[pane.id]
  return !!entry && entry.dir === pane.dir && (entry.thread === pane.thread || entry.thread === null || pane.thread === null)
}

export function withShown(map: Record<string, ArchivedTab>, pane: Pane, show: boolean, liveIds: ReadonlySet<string>): Record<string, ArchivedTab> {
  const next = Object.fromEntries(Object.entries(map).filter(([id]) => liveIds.has(id) && id !== pane.id))
  return show ? { ...next, [pane.id]: { thread: pane.thread, dir: pane.dir } } : next
}

/** The filter hides this tab now. */
export function hidesHelper(pane: Pane, prefs: HelperPrefs, selected: string | null | undefined): boolean {
  return prefs.hideHelpers && !!pane.helper && pane.id !== selected && !needsMak(pane) && !alwaysShown(pane, prefs.shownHelpers)
}

/**
 * The shown tab that a hidden helper folds under: its owner, or the owner's owner when the owner is hidden too, up to
 * the first shown tab. Undefined when no tab in the chain is shown (the helper is then reached from the stack's count).
 */
export function foldTarget(pane: Pane, byId: ReadonlyMap<string, Pane>, shown: ReadonlySet<string>): string | undefined {
  const seen = new Set([pane.id])
  let owner = pane.owner
  while (owner && !seen.has(owner)) {
    if (shown.has(owner)) return owner
    seen.add(owner)
    owner = byId.get(owner)?.owner
  }
  return undefined
}

/** Hidden helpers grouped by the shown tab they fold under. A helper with no shown owner goes under `orphan(helper)`. */
export function foldHelpers(hidden: Pane[], all: Pane[], shown: Pane[], orphan: (pane: Pane) => string = () => ''): Map<string, Pane[]> {
  const byId = new Map(all.map(pane => [pane.id, pane]))
  const ids = new Set(shown.map(pane => pane.id))
  const out = new Map<string, Pane[]>()
  for (const pane of hidden) {
    const key = foldTarget(pane, byId, ids) ?? orphan(pane)
    out.set(key, [...out.get(key) ?? [], pane])
  }
  return out
}

/** How deep a tab sits under owners in this list: 0 for a tab whose owner is not in the list. */
export function nestDepth(pane: Pane, byId: ReadonlyMap<string, Pane>): number {
  let depth = 0
  const seen = new Set([pane.id])
  let owner = pane.owner
  while (owner && byId.has(owner) && !seen.has(owner)) {
    depth++
    seen.add(owner)
    owner = byId.get(owner)?.owner
  }
  return depth
}

/**
 * Each owner's helpers right after it (depth first), the rest in their order. The helpers keep their own order among
 * themselves. A loop in the owner links ends at the first tab seen twice.
 */
export function nestHelpers(panes: Pane[]): Pane[] {
  const ids = new Set(panes.map(pane => pane.id))
  const children = new Map<string, Pane[]>()
  const roots: Pane[] = []
  for (const pane of panes) {
    if (pane.owner && pane.owner !== pane.id && ids.has(pane.owner)) children.set(pane.owner, [...children.get(pane.owner) ?? [], pane])
    else roots.push(pane)
  }
  const out: Pane[] = []
  const placed = new Set<string>()
  const visit = (pane: Pane) => {
    if (placed.has(pane.id)) return
    placed.add(pane.id)
    out.push(pane)
    for (const child of children.get(pane.id) ?? []) visit(child)
  }
  roots.forEach(visit)
  for (const pane of panes) visit(pane)  // tabs in an owner loop: keep them, in their order
  return out
}

/** "3 helpers" for a count. */
export const helperCount = (count: number) => `${count} ${count === 1 ? 'helper' : 'helpers'}`

/** The fold key of a session's helpers that have no shown owner. */
export const orphanKey = (pane: Pane) => `@${pane.tmux_session}`

/** One "N helpers" fold: the hidden helpers of the tab `key` (or of a session, `@id`), and whether you opened it. */
export interface HelperFold { key: string; panes: Pane[]; open: boolean }

/** Opens or closes a fold (App.tsx holds the state in prefs.expanded, key `helpers:<fold key>`). */
export const HelperFoldToggle = createContext<(key: string) => void>(() => {})

/**
 * Which tabs the helper filter hides, and the folds that list them. A fold you opened shows its helpers again. Archived
 * tabs stay with the archive. A session never loses all its tabs to the filter: then its helpers show.
 */
export function helperView(panes: Pane[], prefs: HelperPrefs, selected: string | null | undefined, archived: (pane: Pane) => boolean, opened: (key: string) => boolean) {
  const candidates = panes.filter(pane => !archived(pane) && hidesHelper(pane, prefs, selected))
  const candidateIds = new Set(candidates.map(pane => pane.id))
  const shown = panes.filter(pane => !archived(pane) && !candidateIds.has(pane.id))
  const sessionsShown = new Set(shown.map(pane => pane.tmux_session))
  const kept = candidates.filter(pane => sessionsShown.has(pane.tmux_session))
  const folds = foldHelpers(kept, panes, shown, orphanKey)
  const hidden = new Set<string>()
  const bySession = new Map<string, HelperFold[]>()
  for (const [key, list] of folds) {
    const open = opened(key)
    if (!open) for (const pane of list) hidden.add(pane.id)
    const session = list[0].tmux_session
    bySession.set(session, [...bySession.get(session) ?? [], { key, panes: list, open }])
  }
  return { hidden, bySession }
}
