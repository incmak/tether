import { useSyncExternalStore } from 'react'

/** What ⌘Z and ⌘⇧Z would act on now, for the Undo and Redo buttons (Rail, the chat header's ⋯ menu) and the command menu. Undo.tsx keeps it current and runs
 *  the step when a button sends UNDO_RUN_EVENT. */
export interface UndoNext { undo: string | null; redo: string | null }
export const UNDO_RUN_EVENT = 'tether-undo-run'
let next: UndoNext = { undo: null, redo: null }
const listeners = new Set<() => void>()

export function setUndoNext(value: UndoNext) {
  if (value.undo === next.undo && value.redo === next.redo) return
  next = value
  for (const listener of listeners) listener()
}
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
export const useUndoNext = () => useSyncExternalStore(subscribe, () => next)
export const requestUndo = (direction: 'undo' | 'redo') => window.dispatchEvent(new CustomEvent(UNDO_RUN_EVENT, { detail: direction }))
