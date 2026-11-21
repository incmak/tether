import { parseReview } from './reviewText'
import { parseStatus } from './statusText'

// Prompts that Tether or Claude Code type into a tab. Messages.tsx shows them as cards or notice lines, not as your bubble.
export const COORDINATOR_RULES = '[Tether] The user made this tab the coordinator of this session in Tether.\n'
// A Map, not an object: a message that is only "constructor" must not find Object.prototype.constructor.
export const INTERRUPTED = new Map([
  ['[Request interrupted by user]', 'You stopped the turn'],
  ['[Request interrupted by user for tool use]', 'You stopped the turn during a tool call'],
])
/** Text Tether or Claude Code typed into the tab, not the person: these render as cards, not as your bubble. */
export function injectedPrompt(text: string) {
  return !!(parseReview(text) || parseStatus(text) || text.startsWith(COORDINATOR_RULES) || INTERRUPTED.has(text.trim()))
}
