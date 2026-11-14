// The sidebar search over live sessions and their tabs (Sidebar.tsx).
// A session has its own text (name, folder, custom group). Each tab adds its own (title, agent, stack, branch).
// Every word of the query must appear, in any order, in the session text or the tab text.

/** The lower-case words of a query. */
export const searchWords = (query: string) => query.toLowerCase().split(/\s+/).filter(Boolean)

const hasAll = (text: string, words: string[]) => words.every(word => text.includes(word))

export interface SearchTab { id: string; text: string }
export interface SearchEntry { id: string; text: string; tabs: SearchTab[] }

/** The search text of one session or tab: the fields joined and in lower case. Build it once per data change, not per keystroke. */
export const searchText = (...fields: (string | null | undefined)[]) => fields.filter(Boolean).join(' ').toLowerCase()

/** Which tabs of each session the query shows. A session that is not in the map does not match.
 *  'all': the session's own text matches, or every tab matches. A list: only these tabs match. */
export function matchSessions(entries: SearchEntry[], words: string[]): Map<string, 'all' | Set<string>> {
  const found = new Map<string, 'all' | Set<string>>()
  for (const entry of entries) {
    if (hasAll(entry.text, words)) { found.set(entry.id, 'all'); continue }
    // A word can match the session and the next word the tab: "tether voice" finds the Voice tab in the Tether session.
    const tabs = entry.tabs.filter(tab => words.every(word => tab.text.includes(word) || entry.text.includes(word)))
    if (!tabs.length) continue
    found.set(entry.id, tabs.length === entry.tabs.length ? 'all' : new Set(tabs.map(tab => tab.id)))
  }
  return found
}
