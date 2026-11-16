import type { Api, Pane } from './api'
import { errorText } from './api'

/**
 * Several tabs chosen at once, in the tab strip or the sidebar (like a browser tab strip):
 * Ctrl/Cmd+click adds or removes a tab, Shift+click picks the range from the anchor, Esc clears.
 * The first Ctrl/Cmd+click also keeps the open tab, so the open tab plus the clicked tab are chosen.
 */
export interface TabSelection {
  ids: string[]
  anchor: string | null
  /** The chat each chosen tab held when it was chosen. A tab that now holds another chat drops out (keepLive). */
  threads?: Record<string, string | null>
}
export const NO_SELECTION: TabSelection = { ids: [], anchor: null }

/** Record the chat of each newly chosen tab. now: each live tab's chat. */
export function stamp(selection: TabSelection, now: Map<string, string | null>): TabSelection {
  const threads: Record<string, string | null> = {}
  for (const id of selection.ids) threads[id] = selection.threads && id in selection.threads ? selection.threads[id] : now.get(id) ?? null
  return { ...selection, threads }
}

export type PickMode = 'toggle' | 'range'

/**
 * A pick as the page makes it: drop tabs that changed chat first, then pick, then record the chats of new tabs.
 * Tabs that stay chosen keep the chat recorded when they were first chosen. now: each live tab's chat.
 */
export function choose(selection: TabSelection, now: Map<string, string | null>, id: string, mode: PickMode, order: string[], open?: string): TabSelection {
  const fresh = keepLive(selection, now)
  return stamp({ ...pickTab(fresh, id, mode, order, open), threads: fresh.threads }, now)
}

/** The selection after a pick. order: the tabs in the list the click came from, top to bottom or left to right. */
export function pickTab(selection: TabSelection, id: string, mode: PickMode, order: string[], open?: string): TabSelection {
  if (mode === 'range') {
    const anchor = selection.anchor ?? (open && order.includes(open) ? open : id)
    const from = order.indexOf(anchor), to = order.indexOf(id)
    if (from < 0 || to < 0) return { ids: [id], anchor: id }
    const [low, high] = from < to ? [from, to] : [to, from]
    return { ids: order.slice(low, high + 1), anchor }
  }
  const start = !selection.ids.length && open && open !== id ? [open] : selection.ids
  const ids = start.includes(id) ? start.filter(item => item !== id) : [...start, id]
  return { ids, anchor: id }
}

/**
 * Drop the tabs that closed or now hold another chat than when they were chosen, so a bulk action never reaches
 * a chat the user did not choose. live: each live tab's chat. Keeps the same object when nothing changed.
 */
export function keepLive(selection: TabSelection, live: Map<string, string | null>): TabSelection {
  const same = (id: string) => live.has(id) && (!selection.threads || !(id in selection.threads) || selection.threads[id] === (live.get(id) ?? null))
  if (selection.ids.every(same) && (!selection.anchor || live.has(selection.anchor))) return selection
  const ids = selection.ids.filter(same)
  const threads = selection.threads && Object.fromEntries(ids.filter(id => id in selection.threads!).map(id => [id, selection.threads![id]]))
  return { ids, anchor: selection.anchor && live.has(selection.anchor) ? selection.anchor : null, ...(threads ? { threads } : {}) }
}

/** The pick a click asks for: Shift = range, Ctrl or Cmd = toggle, a plain click while choosing (touch) = toggle. */
export function pickMode(event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }, choosing: boolean): PickMode | null {
  if (event.shiftKey) return 'range'
  if (event.ctrlKey || event.metaKey || choosing) return 'toggle'
  return null
}

/** The tab ids of the list that holds `from`, in screen order (the strip or the sidebar). */
export function orderAround(from: Element): string[] {
  const scope = from.closest('[data-select-scope]')
  return scope ? [...scope.querySelectorAll<HTMLElement>('[data-select-pane]')].map(item => item.dataset.selectPane!) : []
}

/**
 * Close one tab with the server's close route. thread: the chat the page saw in it; the server refuses the close
 * when the tab now holds another chat (server.py do_DELETE), so a stale list cannot close the wrong agent.
 */
export function closeTab(api: Api, pane: Pick<Pane, 'id' | 'thread'>) {
  return api('session', { pane: pane.id, thread: pane.thread ?? null }, undefined, 'DELETE')
}

/**
 * Close tabs one at a time with the single close route. A tab that fails stays open; the others still close.
 * threads: the chat each tab held when the user chose it; the close names that chat, not the newest one.
 */
export async function closeTabs(api: Api, panes: Pane[], threads?: Record<string, string | null>) {
  const closed: string[] = [], failed: { pane: Pane; error: string }[] = []
  for (const pane of panes) {
    const thread = threads && pane.id in threads ? threads[pane.id] : pane.thread
    try { await closeTab(api, { id: pane.id, thread }); closed.push(pane.id) } catch (failure) { failed.push({ pane, error: errorText(failure) }) }
  }
  return { closed, failed }
}

/** What the tab strip and the sidebar need: the chosen tabs, the touch "choosing" mode, a pick, and the × close. */
export interface TabPickControls {
  picked: Set<string>
  choosing: boolean
  onPick: (id: string, mode: PickMode, order: string[], open?: string) => void
  /** The × on a tab: the tab menu's close question (PaneActions startClose), not a second close path. */
  onCloseTab: (id: string, at?: { x: number; y: number }) => void
}
