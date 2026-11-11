// The action deck's rules (ActionDeck.tsx): card order, the answers a card offers, its keys and the ask text.
import type { Api, Approval, Choice, Pane } from './api'
import type { Waiting } from './attention'
import { inboxItems } from './needs'
import type { InboxItem } from './needs'

/** Urgent asks first, then oldest first. */
export const deckOrder = (items: Waiting[]) => [...items].sort((a, b) => Number(!!b.ask.urgent) - Number(!!a.ask.urgent) || a.ask.since - b.ask.since)

const YES = /^Yes\b/i
const KEEP_PLANNING = /^No,?\s+keep planning\b/i
const numbered = (options: Choice[] = []) => options.filter(option => option.id >= 1 && option.id <= 9 && !option.free)

/** The answers for a pane that waits on a terminal prompt: the Home card's item (needs.ts), plus what the deck adds
 *  because it shows the whole prompt: the second "Yes" of a permission request (always, don't ask again) and
 *  menu options up to 9. A plan gets no item answers and no keys: see planOptions.
 *  Undefined when the pane no longer waits on a prompt. */
export function deckItem(pane: Pane): InboxItem | undefined {
  const item = inboxItems([pane], {}).find(found => found.kind === 'approval' || found.kind === 'plan' || found.kind === 'question')
  if (!item) return undefined
  const approval = pane.approval as Approval | null
  const attention = pane.attention
  if (item.kind === 'approval' && item.yes) {
    // inboxItems set Yes only for a request the server can check, so these options are checked too.
    const more = (approval?.options ?? []).filter(option => YES.test(option.text) && option !== item.yes)
    if (more.length === 1) item.always = more[0]
  }
  if (item.kind === 'question') {
    // A list with tick boxes needs more than one tap, and a held question is answered as data: both stay in the tab.
    const single = !attention?.askBridge && attention?.choice?.selection !== 'multi' && !attention?.ask_current?.multi
    const menu = !approval && attention?.reason === 'menu' && single ? numbered(attention.options) : []
    // The Home item already holds options 1 to 3. Replace them, also with nothing when the question needs the tab.
    item.menu = menu.length ? menu : undefined
  }
  return item
}

/** A plan needs reading first, so it has no keys: its options are buttons with their full text, to click.
 *  Only for a plan chooser the server can check (it has an id and one cursor). "Keep planning" needs your
 *  feedback text, so it stays in the tab. */
export function planOptions(pane: Pane, item?: InboxItem): Choice[] {
  const approval = pane.approval as Approval | null
  if (item?.kind !== 'plan' || !approval?.id || !approval.actionable) return []
  return numbered(approval.options).filter(option => !KEEP_PLANNING.test(option.text))
}

/** Sends a plan option through the checked approval route, the one the chat's plan card uses. Home never calls this. */
export async function sendPlanChoice(api: Api, pane: Pane, option: Choice) {
  const approval = pane.approval as Approval | null
  if (!approval?.id || !approval.actionable) throw new Error('This plan changed. Open the tab to check.')
  await api('approval', { session: pane.id, thread: pane.thread, approval: approval.id, choice: option.id })
}

export const hasAnswers = (item?: InboxItem) => !!item && !!(item.yes || item.always || item.no || item.menu?.length)

/** What a key does on a card: an answer, 'next' (J), or nothing. Letters and digits are read from event.code. */
export function deckKey(code: string, item?: InboxItem): Choice | 'next' | undefined {
  if (code === 'KeyJ') return 'next'
  if (!item) return undefined
  if (code === 'KeyY') return item.yes
  if (code === 'KeyA') return item.always
  if (code === 'KeyN') return item.no
  const digit = /^(?:Digit|Numpad)([1-9])$/.exec(code)
  return digit ? item.menu?.find(choice => choice.id === Number(digit[1])) : undefined
}

/** True when the key belongs to a text field, so it must not answer a card. */
export function isTyping(target: { tagName?: string; isContentEditable?: boolean } | null) {
  return !!target && (/^(?:INPUT|TEXTAREA|SELECT)$/.test(target.tagName ?? '') || !!target.isContentEditable)
}

/** The short name of an answer for the status line: "Yes sent to Docs pass." */
export function answerName(item: InboxItem, option: Choice) {
  if (option === item.yes) return 'Yes'
  if (option === item.no) return 'No'
  return `"${option.text.length > 40 ? `${option.text.slice(0, 39).trimEnd()}…` : option.text}"`
}

/** The prompt as the agent shows it: the command, diff or plan of an approval, or the question above its options. */
export function askText(pane: Pane): string {
  const approval = pane.approval as Approval | null
  if (approval?.text?.trim()) return approval.text.trim()
  const attention = pane.attention
  if (!attention) return ''
  const lines = (attention.body ?? attention.text ?? '').split('\n')
  // The options show as buttons, so the text ends where the numbered list starts.
  const first = attention.options?.length ? lines.findIndex(line => /^\s*[>❯›]?\s*1[.)]\s/.test(line)) : -1
  return (first >= 0 ? lines.slice(0, first) : lines).join('\n').trim() || attention.question?.trim() || ''
}

export interface AskLine { text: string; mark: '' | 'add' | 'remove' }

const NUMBERED_LINE = /^\s*\d+(?:\s|$)/
const EDIT_LINE = /^\s*\d+\s*([+-])/

/** One entry per line. A permission request can show an edit as numbered lines ("12 -old", "12 +new", "13  same").
 *  Only inside such a block (two or more numbered lines in a row) do added and removed lines get a mark, so a
 *  Bash command line that starts with - or + stays plain. */
export function askLines(text: string, kind: string): AskLine[] {
  const lines = text.split('\n')
  const numbered = lines.map(line => kind === 'approval' && NUMBERED_LINE.test(line))
  return lines.map((line, at) => {
    const sign = numbered[at] && (numbered[at - 1] || numbered[at + 1]) ? EDIT_LINE.exec(line)?.[1] : undefined
    return { text: line, mark: sign === '+' ? 'add' : sign === '-' ? 'remove' : '' }
  })
}
