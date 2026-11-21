// B331: which entries of Claude's input queue this message box did not send itself.
export interface QueueEntry { id?: number; text: string; time: string | null }

const squash = (text: string) => text.replace(/\s+/g, ' ').trim()
// Claude stores "!ls" as "! ls". Compare with the "!" marks joined, as Chat.tsx does.
const normal = (text: string) => squash(text).replace(/^(?:!\s*)+/, '!')

/** Each local bubble accounts for one queue entry: an entry with the same text first, else the first entry that starts
 *  with the bubble's 80-character key (the server tidies some texts, such as pasted blocks). The rest came from
 *  other senders: Friday, another device or the terminal. */
export function foreignQueued<T extends QueueEntry>(entries: T[], local: { text: string; key: string }[]): T[] {
  const texts = entries.map(entry => normal(entry.text))
  const used = new Set<number>()
  const take = (test: (text: string) => boolean) => {
    const at = texts.findIndex((text, index) => !used.has(index) && test(text))
    if (at >= 0) used.add(at)
    return at >= 0
  }
  const unmatched = local.filter(item => !take(text => text === normal(item.text)))
  for (const item of unmatched) {
    const key = normal(item.key)
    if (key) take(text => text.startsWith(key))
  }
  return entries.filter((_, index) => !used.has(index))
}
