import type { KeyboardEvent, MouseEvent } from 'react'
import type { Pane } from './api'

/** Manual activation keeps arrow navigation from reloading the chat as focus moves. */
export function moveTabFocus(event: KeyboardEvent<HTMLButtonElement>) {
  const { key, currentTarget } = event
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  const tabs = [...(currentTarget.closest('[role="tablist"]')?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])]
  const index = tabs.indexOf(currentTarget)
  const next = key === 'Home' ? 0 : key === 'End' ? tabs.length - 1 : (index + (key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  event.preventDefault()
  tabs[next]?.focus()
}

let focusOnOpen: { source: HTMLButtonElement; pane: string; stripClass: string; expires: number } | undefined

/** The pane switch replaces the chat DOM. The new strip restores focus when it mounts. */
export function openFromTab(event: MouseEvent<HTMLButtonElement>, pane: Pane, onOpen: (pane: Pane) => void) {
  const source = event.currentTarget
  focusOnOpen = event.detail === 0 ? {
    source, pane: pane.id, stripClass: source.closest('.task-chips') ? 'task-chips' : 'pane-tabs', expires: performance.now() + 1000,
  } : undefined
  onOpen(pane)
}

export function restoreTabFocus(strip: HTMLElement, pane: string) {
  if (!focusOnOpen) return
  if (focusOnOpen.expires < performance.now()) { focusOnOpen = undefined; return }
  if (focusOnOpen.pane !== pane || !strip.classList.contains(focusOnOpen.stripClass) || focusOnOpen.source.isConnected) return
  focusOnOpen = undefined
  strip.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus({ preventScroll: true })
}
