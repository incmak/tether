import type { Approval, Choice, Needs, Pane } from './api'
import { agentNames } from './Sidebar'
import { isDone, needsYou } from './paneState'

// The rows of the desktop "Needs you" list (Inbox.tsx).

export type Kind = Needs['kind'] | 'done' | 'browser' | 'working' | 'stopped'
/** browser: an agent asked for the person in the shared browser (AgentBrowser.tsx). ask is its id, tab the agent's first tab.
 *  always: the "Yes, and don't ask again" option. Only the action deck sets it (deck.ts). */
export interface InboxItem { pane: Pane; kind: Kind; text: string; at: number; yes?: Choice; always?: Choice; no?: Choice; menu?: Choice[]; ask?: string; tab?: string }
export interface BrowserAskRow { id: string; pane: string | null; text: string; at: number; tab?: string }

export const KIND_LABEL: Record<Kind, string> = { working: 'Working', approval: 'Needs permission', plan: 'Plan to review', question: 'Question', done: 'Done', browser: 'Needs you in the browser', stopped: 'Stopped' }
const YES = /^Yes\b/i, ALWAYS = /always|don.t ask|remember/i
const NO = /^(?:No\b|Reject\b|Cancel\b|Deny\b)/i
const PLAN = /\bkeep planning\b|tell Claude what to change/i

/** The servers before `needs` existed: a line from the approval or the question. */
function fallback(pane: Pane): Needs {
  const approval = pane.approval as Approval | null
  const plan = [...(approval?.options ?? []), ...(pane.attention?.options ?? [])].some(option => PLAN.test(option.text))
  const text = approval ? approval.text.split('\n').map(line => line.trim()).find(Boolean) ?? '' : pane.attention?.question ?? ''
  return { kind: plan ? 'plan' : pane.state === 'Approval' ? 'approval' : 'question', text }
}

export function inboxItems(panes: Pane[], seen: Record<string, number>, selected?: string, asks: BrowserAskRow[] = []): InboxItem[] {
  const items: InboxItem[] = []
  for (const ask of asks) {
    const pane = panes.find(item => item.id === ask.pane)
    if (pane) items.push({ pane, kind: 'browser', text: ask.text, at: ask.at, ask: ask.id, tab: ask.tab })
  }
  for (const pane of panes) {
    if (needsYou(pane.state)) {
      const needs = pane.needs ?? fallback(pane)
      const item: InboxItem = { pane, kind: needs.kind, text: needs.text, at: pane.since ?? 0 }
      const approval = pane.approval as Approval | null
      // Yes and No only for a request the server can check (it has an id and a cursor). A plan needs reading first.
      if (needs.kind === 'approval' && approval?.id && approval.actionable && approval.options?.length) {
        item.yes = approval.options.find(option => YES.test(option.text) && !ALWAYS.test(option.text))
        item.no = approval.options.find(option => NO.test(option.text))
      }
      if (needs.kind === 'question' && !approval && pane.attention?.reason === 'menu') {
        const menu = (pane.attention.options ?? []).filter(option => option.id >= 1 && option.id <= 3 && !option.free)
        if (menu.length) item.menu = menu
      }
      items.push(item)
    } else if (isDone(pane, seen, selected)) {
      items.push({ pane, kind: 'done', text: `${agentNames[pane.agent]} finished its turn`, at: pane.finished ?? 0 })
    }
  }
  return items.sort((a, b) => b.at - a.at)
}

export function age(at: number, now: number) {
  const seconds = Math.max(0, now - at)
  if (!at || seconds < 60) return 'now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

/** The key label for shortcuts: ⌘ on a Mac, Ctrl elsewhere. */
export const MOD = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl+'
