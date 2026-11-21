// B331: which entries of Claude's input queue this message box did not send itself.
export interface QueueEntry { id?: number; text: string; time: string | null }

const squash = (text: string) => text.replace(/\s+/g, ' ').trim()
// Claude stores "!ls" as "! ls". Compare with the "!" marks joined, as Chat.tsx does.
const normal = (text: string) => squash(text).replace(/^(?:!\s*)+/, '!')

/** Each local bubble accounts for one queue entry with the same text (whitespace aside). Only an exact match counts:
 *  two prompts can share a long start, and hiding another sender's prompt is worse than showing one twice. The server's
 *  display text equals the sent text in all 2124 queued prompts in local transcripts (B331 check, 21 Nov 2026).
 *  The entries left came from other senders: Friday, another device or the terminal. */
export function foreignQueued<T extends QueueEntry>(entries: T[], local: { text: string }[]): T[] {
  const texts = entries.map(entry => normal(entry.text))
  const used = new Set<number>()
  for (const item of local) {
    const text = normal(item.text)
    const at = texts.findIndex((other, index) => !used.has(index) && other === text)
    if (at >= 0) used.add(at)
  }
  return entries.filter((_, index) => !used.has(index))
}
