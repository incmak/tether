import { marks } from './search'

/** A text with the parts the search matched in <mark> (search.ts marks()). words: searchWords of the query. */
export function Marked({ text, words, fuzzy = true }: { text: string; words: string[]; fuzzy?: boolean }) {
  if (!words.length) return <>{text}</>
  return <>{marks(text, words, fuzzy).map(([piece, hit], at) => hit ? <mark key={at}>{piece}</mark> : piece)}</>
}
