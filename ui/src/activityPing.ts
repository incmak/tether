import { useEffect, useRef } from 'react'
import type { Api } from './api'

// Activity time for WakaTime (server/wakatime.py): the page reports that the person used a tab.
const EVERY = 60_000  // one ping a minute at most; the server sends one heartbeat per folder every 2 minutes
const INPUT = ['pointerdown', 'keydown', 'wheel', 'touchstart'] as const

/** Tell the server the person used this tab: a key, a click, a tap or a scroll while the page is visible. */
export function useActivityPing(api: Api, pane: string | undefined, on: boolean) {
  const last = useRef(0)
  useEffect(() => {
    if (!on || !pane) return
    const ping = () => {
      const now = Date.now()
      if (document.visibilityState !== 'visible' || now - last.current < EVERY) return
      last.current = now
      api('activity', { pane }).catch(() => { /* activity time is best effort */ })
    }
    for (const name of INPUT) window.addEventListener(name, ping, { capture: true, passive: true })
    return () => { for (const name of INPUT) window.removeEventListener(name, ping, { capture: true }) }
  }, [api, pane, on])
}
