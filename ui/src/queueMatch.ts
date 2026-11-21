// B331: which entries of Claude's input queue this message box did not send itself.
export interface QueueEntry { id?: number; text: string; time: string | null }

// Chat.tsx keys each local bubble with its first 80 squashed characters.
const KEY_LENGTH = 80
const squash = (text: string) => text.replace(/\s+/g, ' ').trim()
// Claude stores "!ls" as "! ls". Compare with the "!" marks joined, as Chat.tsx does.
const normal = (text: string) => squash(text).replace(/^(?:!\s*)+/, '!')

/** Each local bubble accounts for one queue entry with the same text. A long bubble whose text the server may tidy
 *  (a pasted block, for example) can also take the first entry that starts with its full 80-character key. A shorter
 *  bubble never takes a longer entry: "run the tests" must not hide "run the tests after Friday".
 *  The entries left came from other senders: Friday, another device or the terminal. */
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
    if (key.length >= KEY_LENGTH) take(text => text.startsWith(key))
  }
  return entries.filter((_, index) => !used.has(index))
}
