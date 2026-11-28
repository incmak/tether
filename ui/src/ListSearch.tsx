import { useEffect, useState } from 'react'
import { Search } from 'lucide-react'
import { rowsOf } from './listSearch'
import type { ListSearchState } from './listSearch'

// The search field of a long list; the filter and the keys are in listSearch.ts (B628).
/**
 * The search field above a list. Down moves to the first row. Enter picks the first row when pick is set and a query
 * is typed (a picker); without pick, Enter does nothing (a field in a form must not send it). Escape clears the text
 * first, from anywhere in the dialog; a second Escape closes it. The status line tells a screen reader how many rows match.
 */
export function ListSearch<T>({ search, label, noun, pick = false, autoFocus }: {
  search: ListSearchState<T>; label: string
  /** The plural name of the items, for the count: "tabs", "sessions". */
  noun: string
  pick?: boolean
  /** Focus the field when it appears. Default: only with a mouse or trackpad, so a phone keyboard does not cover the list. */
  autoFocus?: boolean
}) {
  const { query, setQuery, shown, input, setInput, list, searchable } = search
  const [settled, setSettled] = useState('')
  const focus = autoFocus ?? (typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches)
  // After the dialog's own focus step, which takes the first button.
  useEffect(() => { if (!focus || !input) return; const frame = requestAnimationFrame(() => input.focus()); return () => cancelAnimationFrame(frame) }, [focus, input])
  // Escape anywhere in the picker (the field, a result, Clear search) clears a query first; with no query it closes the
  // dialog as before. Capture on the dialog runs before any row's own keys, and preventDefault stops its cancel event.
  useEffect(() => {
    const scope = input?.closest('dialog') ?? input?.closest('.list-search-box')
    if (!input || !scope || !query) return
    const clear = (event: Event) => {
      const key = event as globalThis.KeyboardEvent
      if (key.key !== 'Escape' || key.isComposing || key.keyCode === 229) return
      // A dialog can hold another (Broadcast holds Send via): only the innermost dialog of the key clears its own search.
      if ((key.target as Element | null)?.closest?.('dialog') !== input.closest('dialog')) return
      key.preventDefault(); key.stopPropagation(); setQuery(''); input.focus()
    }
    scope.addEventListener('keydown', clear, true)
    return () => scope.removeEventListener('keydown', clear, true)
  }, [input, query, setQuery])
  // Announce the count a moment after typing stops, so a screen reader does not read a number for every letter.
  useEffect(() => { const timer = setTimeout(() => setSettled(query), 400); return () => clearTimeout(timer) }, [query])
  const count = !query.trim() || settled !== query ? ''
    : shown.length ? `${shown.length} ${shown.length === 1 ? noun.replace(/e?s$/, '') : noun} found` : `No ${noun} found`
  if (!searchable) return null
  return <div className="list-search-box">
    <label className="list-search"><Search size={15} aria-hidden="true" />
      <input ref={setInput} type="search" value={query} aria-label={label} placeholder={label} autoComplete="off" enterKeyHint={pick ? 'go' : 'search'}
        onChange={event => setQuery(event.target.value)}
        onKeyDown={event => {
          // Keys that finish an IME composition (Chinese, Japanese, Korean input) belong to the IME, not the picker.
          // Stop them here, or the dialog's arrow walk (usePopover) moves the focus away; keep their default for the IME.
          if (event.nativeEvent.isComposing || event.keyCode === 229) { event.stopPropagation(); return }
          if (event.key === 'Enter') {
            event.preventDefault()
            if (pick && query.trim()) rowsOf(list)[0]?.click()
            return
          }
          // With no result, Down stays in the field: the commands below the list (New, Cancel) are reached with Tab.
          if (event.key === 'ArrowDown') { event.preventDefault(); event.stopPropagation(); rowsOf(list)[0]?.focus() }
          if (event.key === 'ArrowUp') { event.preventDefault(); event.stopPropagation() }
        }} />
    </label>
    <p className="visually-hidden" role="status">{count}</p>
  </div>
}
