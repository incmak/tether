// Attention mode: the tabs whose ask waits on you (server/attention.py), oldest first.
import { useEffect, useState } from 'react'
import type { Api, Ask, Pane } from './api'
import { needsYou } from './paneState'

// state: the tab waits by its state only (an approval or a question with no ask); its ask is made up from the pane.
export interface Waiting { pane: Pane; ask: Ask; state?: boolean }
export type AskAction = 'handled' | 'snooze' | 'opened'
export interface AskResult { ask: Ask | null; waiting: number }
export interface Digest { enabled: boolean; hour: number }

/** Waited this long (seconds): the age shows in bold. */
export const LATE = 12 * 3600

/** The asks that show now: not snoozed, oldest first. */
export function waitingAsks(panes: Pane[], now: number): Waiting[] {
  return panes.flatMap(pane => pane.ask && pane.agent !== 'shell' && !(pane.ask.snoozed_until && pane.ask.snoozed_until > now) ? [{ pane, ask: pane.ask }] : [])
    .sort((a, b) => a.ask.since - b.ask.since)
}

/** The pane's ask when it shows now, else null. */
/** The deck's cards: the asks, then each tab that waits by its state only, as a card that opens the tab. */
export function deckItems(panes: Pane[], now: number): Waiting[] {
  const stateOnly = panes.filter(pane => !pane.ask && pane.agent !== 'shell' && needsYou(pane.state)).map(pane => ({
    pane, state: true,
    ask: { pane: pane.id, thread: pane.thread ?? null, since: pane.since ?? 0, next: '', kind: pane.state === 'Approval' ? 'approval' : 'question', opened: false, snoozed_until: null } as Ask,
  }))
  return [...waitingAsks(panes, now), ...stateOnly].sort((a, b) => a.ask.since - b.ask.since)
}

export const visibleAsk = (pane: Pane, now: number) => waitingAsks([pane], now)[0]?.ask ?? null

export const askAction = (api: Api, pane: Pane, action: AskAction) =>
  api<AskResult>(`attention/${action}`, { session: pane.id, thread: pane.thread ?? null })

export const stillWait = (count: number) => count ? `${count} still wait${count === 1 ? 's' : ''}.` : 'Nothing else waits on you.'

/** Unix seconds, updated every 30 s, so the ages move on while nothing else changes. */
export function useNow() {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 30000); return () => clearInterval(timer) }, [])
  return now
}
