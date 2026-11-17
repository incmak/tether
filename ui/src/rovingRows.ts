import { useState } from 'react'
import type { FocusEvent, KeyboardEvent } from 'react'

// A list of row buttons takes one Tab stop. The arrow keys, Home and End move the focus between its rows. Enter or
// Space on a row does what a click does. Rows carry `data-row` with a key unique in the list.
// The Tab stop is React state (useRovingStop), never set on the DOM here: a DOM tabIndex and a React tabIndex on the
// same row disagree after the next refresh, and the list then has two Tab stops.
export function rovingRows(event: KeyboardEvent<HTMLElement>) {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  const rows = [...event.currentTarget.querySelectorAll<HTMLElement>('[data-row]')]
  const at = rows.indexOf(document.activeElement as HTMLElement)
  if (at < 0) return
  event.preventDefault()
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)))
  rows[next].focus()
}

// The row that holds the Tab stop of a list: the last row that had the focus while it is still in that list, else
// `fallback` (the marked row). Give each list `onFocus` and each row `tabIndex={tabFor(key, keysOfItsList, fallback)}`.
// Tab out and back returns to the same row, and a new mark from the agent does not add a second Tab stop.
export function useRovingStop() {
  const [stop, setStop] = useState<string | null>(null)
  const onFocus = (event: FocusEvent<HTMLElement>) => {
    const key = (event.target as HTMLElement).closest('[data-row]')?.getAttribute('data-row')
    if (key && event.currentTarget.contains(event.target as Node)) setStop(key)
  }
  const tabFor = (key: string, keys: string[], fallback: string) => key === (stop !== null && keys.includes(stop) ? stop : fallback) ? 0 : -1
  return { tabFor, onFocus }
}
