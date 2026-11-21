// Attention mode: the tabs whose ask waits on you (server/attention.py), oldest first.
import { useEffect, useState } from 'react'
import type { Api, Ask, Pane } from './api'
import { needsYou } from './paneState'

// state: the tab waits by its state only (an approval or a question with no ask); its ask is made up from the pane.
export interface Waiting { pane: Pane; ask: Ask; state?: boolean }
export type AskAction = 'handled' | 'snooze' | 'opened' | 'later' | 'back'
export interface AskResult { ask: Ask | null; waiting: number }
export interface Digest { enabled: boolean; hour: number }

/** Waited this long (seconds): the age shows in bold. */
export const LATE = 12 * 3600

/** The asks that show now: not snoozed and not in Later, oldest first. */
export function waitingAsks(panes: Pane[], now: number): Waiting[] {
  return panes.flatMap(pane => pane.ask && pane.agent !== 'shell' && !pane.ask.later && !(pane.ask.snoozed_until && pane.ask.snoozed_until > now) ? [{ pane, ask: pane.ask }] : [])
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

/** ask: the occurrence id the page saw. The server refuses the write (409) when the ask changed since. */
export const askAction = (api: Api, pane: Pane, action: AskAction, ask?: Ask | null) =>
  api<AskResult>(`attention/${action}`, {
    session: pane.id, thread: pane.thread ?? null,
    ...(ask?.id ? { ask: ask.id } : {}),
    ...(action === 'back' && ask?.later?.entry ? { entry: ask.later.entry } : {}),
  })

/** The tabs whose ask is in Later, newest move first. */
export const laterAsks = (panes: Pane[]): Waiting[] =>
  panes.flatMap(pane => pane.ask?.later && pane.agent !== 'shell' ? [{ pane, ask: pane.ask }] : [])
    .sort((a, b) => (b.ask.later?.at ?? 0) - (a.ask.later?.at ?? 0))

/** One line of the automation log (server/attention.py GET attention/log, audit kinds auto.*). */
export interface AutoEntry {
  id: string; kind: string; summary: string; at: number
  detail?: Record<string, string | number | boolean | null>
  undone?: boolean
  failed?: boolean
  undo?: { session: string; thread: string | null; ask: string; entry: string } | null
}
export interface AutoLog { entries: AutoEntry[]; handled_today: number; today_start: number }
export interface AttentionState { later: Ask[]; handled_today: number | null; log_error?: boolean; rules: { stale: boolean }; digest: Digest }

export function confirmStaleRule(current: { confirmed: boolean | null; shown: boolean | null }, value: boolean | null, show: boolean) {
  const confirmed = value ?? current.confirmed
  return { confirmed, shown: show ? confirmed ?? current.shown : current.shown }
}

export interface StaleRuleResult { confirmed: boolean | null; last: boolean; error: string }

// The server applies stale rule writes in the order they arrive, so only one write runs at a time. The queue lives
// for the page, not the dialog: Settings can close and open again while a write runs.
export class StaleRuleQueue {
  private running = false
  private next: boolean | null = null
  wanted: boolean | null = null  // the newest choice while writes run
  generation = 0                 // changes on each choice, so an older read cannot overwrite it
  listener: ((result: StaleRuleResult) => void) | null = null

  get busy() { return this.running || this.next !== null }

  async set(value: boolean, write: (value: boolean) => Promise<boolean>, read: () => Promise<boolean>,
    describe: (reason: unknown) => string) {
    this.generation++
    this.next = value
    this.wanted = value
    if (this.running) return
    this.running = true
    try {
      while (this.next !== null) {
        const sent = this.next
        this.next = null
        let result: Omit<StaleRuleResult, 'last'>
        try {
          result = { confirmed: await write(sent), error: '' }
        } catch (reason) {
          let confirmed: boolean | null = null
          try { confirmed = await read() } catch { /* Keep the last confirmation. */ }
          result = { confirmed, error: describe(reason) }
        }
        // Only the newest write decides what the switch shows and whether an error shows.
        const last = this.next === null
        if (last) this.wanted = null
        this.listener?.({ ...result, last })
      }
    } finally {
      this.running = false
    }
  }
}

export const staleRuleQueue = new StaleRuleQueue()

export function splitToday(entries: AutoEntry[], todayStart: number) {
  const today: AutoEntry[] = [], older: AutoEntry[] = []
  for (const entry of entries) {
    if (entry.at >= todayStart) today.push(entry)
    else older.push(entry)
  }
  return { today, older }
}

export const stillWait = (count: number) => count ? `${count} still wait${count === 1 ? 's' : ''}.` : 'Nothing else waits on you.'

/** Unix seconds, updated every 30 s, so the ages move on while nothing else changes. */
export function useNow() {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 30000); return () => clearInterval(timer) }, [])
  return now
}
