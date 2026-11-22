import { useEffect, useRef } from 'react'
import type { Api } from './api'

// Activity time (server/activity_time.py): the page reports that the person used a tab.
const EVERY = 60_000  // one ping a minute at most; the server counts whole minutes
const INPUT = ['pointerdown', 'keydown', 'wheel', 'touchstart'] as const
// A chat panel (Chat.tsx) or a tab in the tab strip (PaneTabs.tsx). Home, Settings and other screens name no tab.
const TAB = '.chat-panel[data-pane], [role="tab"][data-pane]'

/** The tab an input event belongs to: the chat panel or tab button under it, read when the event happens. */
export function eventPane(target: EventTarget | null): string {
  return target instanceof Element ? target.closest<HTMLElement>(TAB)?.dataset.pane ?? '' : ''
}

/** Tell the server the person used a tab: a key, a click, a tap or a scroll inside that tab while the page is visible. */
export function useActivityPing(api: Api, on: boolean) {
  const last = useRef<Record<string, number>>({})
  useEffect(() => {
    if (!on) return
    const ping = (event: Event) => {
      const pane = eventPane(event.target)
      const now = Date.now()
      if (!pane || document.visibilityState !== 'visible' || now - (last.current[pane] ?? 0) < EVERY) return
      last.current[pane] = now
      api('activity', { pane }).catch(() => { /* activity time is best effort */ })
    }
    for (const name of INPUT) window.addEventListener(name, ping, { capture: true, passive: true })
    return () => { for (const name of INPUT) window.removeEventListener(name, ping, { capture: true }) }
  }, [api, on])
}
