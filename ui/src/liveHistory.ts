/** The Live window's words: the newest exchange in front, earlier lines kept above it. */
export type Spoken = { who: string; text: string }
export type Hero = { said: string; reply: Spoken }
export type Line = Spoken & { id: number; you: boolean }

/** Earlier lines kept on screen. Older ones drop off the top. */
export const PAST_MAX = 200

/** The lines of the current exchange worth keeping, person first. */
function exchange(hero: Hero) {
  return [{ who: 'You', text: hero.said, you: true }, { ...hero.reply, you: false }].filter(line => line.text.trim())
}

/** A new phrase from the person: the last exchange moves up into the history. */
export function heard(hero: Hero, text: string) {
  return { hero: { said: text, reply: { ...hero.reply, text: '' } }, keep: exchange(hero) }
}

/** A reply grows while it streams. A different reply moves the last exchange up first. */
export function answered(hero: Hero, next: Spoken) {
  const old = hero.reply
  const grows = !old.text || (next.who === old.who && next.text.startsWith(old.text))
  return grows ? { hero: { ...hero, reply: next }, keep: [] }
    : { hero: { said: '', reply: next }, keep: exchange(hero) }
}

/** Add kept lines with fresh ids, and keep at most PAST_MAX. */
export function remember(past: Line[], keep: Omit<Line, 'id'>[], nextId: () => number) {
  return keep.length ? [...past, ...keep.map(line => ({ ...line, id: nextId() }))].slice(-PAST_MAX) : past
}
