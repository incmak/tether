import type { Pane } from './api'

/** The tab waits on you: a prompt on screen, an ask ("Needs you: yes", with or without Urgent), or a review paused for you. */
export function needsMak(pane: Pane): boolean {
  return pane.state === 'Approval' || pane.state === 'Input needed' || !!pane.ask || !!pane.escalated
}

/** "3 helpers" for a count. */
export const helperCount = (count: number) => `${count} ${count === 1 ? 'helper' : 'helpers'}`

/** The short 'from' label: the backlog id when the name starts with one (B12), else the name. */
export const fromLabel = (name: string) => /^\s*(B\d+)(?![\w])/.exec(name)?.[1] ?? name.trim()
