import { useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'

// One search field for the pickers and lists of tabs, sessions, stacks and panes (B628).
// A list of SEARCH_FROM items or more gets the field; a shorter list is quick to read and the field is only clutter.
// The match is plain: every word of the query is a substring of the item's text, in any order. A filter must be
// predictable, so the list keeps its own order and nothing is ranked or matched loosely.
export const SEARCH_FROM = 8

/** The lower-case words of a query. */
const wordsOf = (query: string) => query.toLowerCase().split(/\s+/).filter(Boolean)

/** True when every word of the query is in one of the parts. An empty query matches everything. */
export function matchesQuery(query: string, ...parts: (string | null | undefined)[]) {
  const words = wordsOf(query)
  if (!words.length) return true
  const text = parts.filter(Boolean).join(' ').toLowerCase()
  return words.every(word => text.includes(word))
}

// The rows that the arrow keys walk and that Enter picks: the enabled buttons of the list, not separators or hints.
// A row can mark itself out with data-search-skip (for example Cancel or Back at the end of a list).
const ROWS = 'button:not(:disabled):not([aria-disabled="true"]):not([data-search-skip])'
export const rowsOf = (list: HTMLElement | null) => [...(list?.querySelectorAll<HTMLButtonElement>(ROWS) ?? [])]

export interface ListSearchState<T> {
  query: string
  setQuery: (query: string) => void
  /** The items to show: all of them when the list is short or the query is empty. */
  shown: T[]
  /** The list is long enough for a search field. */
  searchable: boolean
  /** The field and the list, as elements: pass setInput and setList as their ref. */
  input: HTMLInputElement | null
  setInput: (element: HTMLInputElement | null) => void
  list: HTMLElement | null
  setList: (element: HTMLElement | null) => void
  /** Spread on the list container: Up and Down walk the rows, Up on the first row goes back to the field. */
  listKeys: (event: KeyboardEvent<HTMLElement>) => void
}

/** The filter state of one list. textOf gives the words an item is found by: its name, place, folder, agent. */
export function useListSearch<T>(items: T[], textOf: (item: T) => (string | null | undefined)[]): ListSearchState<T> {
  const [query, setQuery] = useState('')
  const [input, setInput] = useState<HTMLInputElement | null>(null)
  const [list, setList] = useState<HTMLElement | null>(null)
  const searchable = items.length >= SEARCH_FROM
  const shown = useMemo(() => searchable && query.trim() ? items.filter(item => matchesQuery(query, ...textOf(item))) : items,
    // textOf is a new function on each render; the items and the query decide the result.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, query, searchable])
  const listKeys = (event: KeyboardEvent<HTMLElement>) => {
    // A short list has no field: the dialog's own arrow walk (usePopover) still reaches every button, Cancel too.
    if (!searchable) return
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Home' && event.key !== 'End') return
    const rows = rowsOf(list)
    const at = rows.indexOf(document.activeElement as HTMLButtonElement)
    if (at < 0) return
    // The popover also walks its rows on the arrow keys (usePopover): this list moves focus once, and stops at its ends.
    event.preventDefault()
    event.stopPropagation()
    if (event.key === 'ArrowUp' && at === 0) { input?.focus(); return }
    const next = { ArrowDown: Math.min(at + 1, rows.length - 1), ArrowUp: Math.max(at - 1, 0), Home: 0, End: rows.length - 1 }[event.key]
    rows[next]?.focus()
  }
  return { query, setQuery, shown, searchable, input, setInput, list, setList, listKeys }
}

