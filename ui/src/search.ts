// One search for the sidebar, the ⌘K / ⌥Space menu and the backlog: the same words find the same things everywhere.
// Every word of the query must match a field, in any order. A word matches, best first:
//   at the start of the text (5), at the start of a word (4), inside a word (3),
//   as initials ("vml" → Voice mode latency) (3), as close letters from a word start ("bklg" → backlog) (2),
//   or with one typo when it has 4 letters or more ("vioce" → voice) (1).
// A field has a weight: a name counts double, so a title match ranks above a folder match.
// Build the fields once per data change (field()); each keystroke only scores them.

/** fuzzy false: whole substrings only, for long free text (backlog details) where loose letters would match noise. */
export interface Field { text: string; tokens: string[]; initials: string; weight: number; letters: number; fuzzy: boolean }
export interface SearchTab { id: string; fields: Field[] }
/** A session and its tabs. A word can match the session and the next word a tab: "tether voice". */
export interface SearchEntry { id: string; fields: Field[]; tabs: SearchTab[] }
export interface SessionMatch {
  score: number
  /** 'all': the session's own fields match, or every tab matches. A set: only these tabs match. */
  tabs: 'all' | Set<string>
  /** The score of each matching tab, for ranking tab rows. */
  tabScores: Map<string, number>
  /** The score of the session's own fields, or null when only its tabs match. */
  own: number | null
}

const SPLIT = /[^\p{L}\p{N}]+/u
const BOUNDARY = /[^\p{L}\p{N}]/u

// One bit for each letter a to z in the text. A fuzzy match needs the word's letters, less one for a typo.
function letterBits(text: string) {
  let bits = 0
  for (let i = 0; i < text.length; i++) { const code = text.charCodeAt(i) - 97; if (code >= 0 && code < 26) bits |= 1 << code }
  return bits
}
function bitCount(bits: number) { let count = 0; for (; bits; bits &= bits - 1) count++; return count }

/** The lower-case words of a query. */
export const searchWords = (query: string) => query.toLowerCase().split(/\s+/).filter(Boolean)

/** A searchable field: the parts joined, in lower case. weight 2 for names and titles, 1 for the rest. */
export function field(weight: number, ...parts: (string | null | undefined)[]): Field {
  const text = parts.filter(Boolean).join(' ').toLowerCase()
  const tokens = text.split(SPLIT).filter(Boolean)
  return { text, tokens, initials: tokens.map(token => token[0]).join(''), weight, letters: letterBits(text), fuzzy: true }
}

/** A field for long free text: it matches whole substrings only, never initials, close letters or typos. */
export function exactField(weight: number, ...parts: (string | null | undefined)[]): Field {
  const text = parts.filter(Boolean).join(' \n ').toLowerCase()
  return { text, tokens: [], initials: '', weight, letters: 0, fuzzy: false }
}

// The letters of the word in order, starting at a word start, within a short span: no letters scattered over a long path.
function closeLetters(text: string, word: string) {
  const span = word.length * 2 + 2
  for (let start = text.indexOf(word[0]); start >= 0; start = text.indexOf(word[0], start + 1)) {
    if (start > 0 && !BOUNDARY.test(text[start - 1])) continue
    let next = 1
    for (let at = start + 1; at < text.length && at - start < span && next < word.length; at++) if (text[at] === word[next]) next++
    if (next === word.length) return true
  }
  return false
}

// One edit (a wrong, missing, extra or swapped letter) between the word and the start of the token.
// Optimal string alignment distance over three rows kept between calls: no allocation per token.
let rows = [new Int32Array(0), new Int32Array(0), new Int32Array(0)]
function oneTypo(word: string, token: string) {
  // One edit leaves one of the first two letters in place, maybe shifted by one: a cheap test before the full check.
  if (token.length < word.length - 1 || (token[0] !== word[0] && token[1] !== word[1] && token[0] !== word[1] && token[1] !== word[0])) return false
  const n = Math.min(token.length, word.length + 1)
  if (rows[0].length <= n) rows = rows.map(() => new Int32Array(n + 8))
  let [prev, before, row] = rows
  for (let j = 0; j <= n; j++) before[j] = j
  for (let i = 1; i <= word.length; i++) {
    row[0] = i
    let low = i
    for (let j = 1; j <= n; j++) {
      let cost = Math.min(before[j] + 1, row[j - 1] + 1, before[j - 1] + (word[i - 1] === token[j - 1] ? 0 : 1))
      if (i > 1 && j > 1 && word[i - 1] === token[j - 2] && word[i - 2] === token[j - 1]) cost = Math.min(cost, prev[j - 2] + 1)
      row[j] = cost
      if (cost < low) low = cost
    }
    if (low > 1) return false
    ;[prev, before, row] = [before, row, prev]
  }
  // The token may run on after the word, so its tail is free: the word may end one letter short or long.
  for (let j = Math.max(0, word.length - 1); j <= n; j++) if (before[j] <= 1) return true
  return false
}

/** How well one word matches one field, before its weight. null: no match. */
export function wordScore(f: Field, word: string): number | null {
  const at = f.text.indexOf(word)
  if (at === 0) return 5
  if (at > 0) {
    // A later match at a word start beats the first match inside a word: "voice" in "revoice voice".
    for (let next = at; next > 0; next = f.text.indexOf(word, next + 1)) if (BOUNDARY.test(f.text[next - 1])) return 4
    return 3
  }
  if (!f.fuzzy || word.length < 2) return null
  const missing = bitCount(letterBits(word) & ~f.letters)
  if (missing > 1 || (missing === 1 && word.length < 4)) return null
  if (f.initials.startsWith(word) || (word.length >= 3 && f.initials.includes(word))) return 3
  if (closeLetters(f.text, word)) return 2
  if (word.length >= 4 && f.tokens.some(token => oneTypo(word, token))) return 1
  return null
}

// Where a word matches the field's text, by the rule that wordScore used: [start, end) ranges in the text.
function wordRanges(f: Field, word: string): [number, number][] {
  const text = f.text
  let at = text.indexOf(word)
  if (at >= 0) {
    for (let next = at; next > 0; next = text.indexOf(word, next + 1)) if (BOUNDARY.test(text[next - 1])) { at = next; break }
    return [[at, at + word.length]]
  }
  if (wordScore(f, word) === null) return []
  const tokens = [...text.matchAll(/[\p{L}\p{N}]+/gu)].map(match => ({ start: match.index, text: match[0] }))
  const first = f.initials.startsWith(word) ? 0 : word.length >= 3 ? f.initials.indexOf(word) : -1
  if (first >= 0) return tokens.slice(first, first + word.length).map(token => [token.start, token.start + 1])
  const span = word.length * 2 + 2
  for (let start = text.indexOf(word[0]); start >= 0; start = text.indexOf(word[0], start + 1)) {
    if (start > 0 && !BOUNDARY.test(text[start - 1])) continue
    const found: [number, number][] = [[start, start + 1]]
    for (let at = start + 1; at < text.length && at - start < span && found.length < word.length; at++) if (text[at] === word[found.length]) found.push([at, at + 1])
    if (found.length === word.length) return found
  }
  const token = tokens.find(entry => oneTypo(word, entry.text))
  return token ? [[token.start, token.start + token.text.length]] : []
}

/** The parts of a text that the query words match, for <mark>: [piece, matched] pairs in order. It marks the same
 * letters the search matched: a substring, the initials ("vml"), the close letters ("bklg") or the word with a typo.
 * fuzzy false: substrings only, as for an exactField. */
export function marks(text: string, words: string[], fuzzy = true): [string, boolean][] {
  const f = fuzzy ? field(1, text) : exactField(1, text)
  // A few letters change length in lower case (İ): their offsets would not fit the shown text.
  if (!words.length || f.text.length !== text.length) return text ? [[text, false]] : []
  const hit = new Uint8Array(text.length)
  for (const word of words) for (const [start, end] of wordRanges(f, word)) hit.fill(1, start, end)
  const pieces: [string, boolean][] = []
  for (let start = 0; start < text.length;) {
    let end = start + 1
    while (end < text.length && hit[end] === hit[start]) end++
    pieces.push([text.slice(start, end), hit[start] === 1])
    start = end
  }
  return pieces
}

function best(fields: Field[], word: string) {
  let top: number | null = null
  for (const f of fields) { const score = wordScore(f, word); if (score !== null && (top === null || score * f.weight > top)) top = score * f.weight }
  return top
}

/** The score of the query against these fields: the sum of each word's best match. null: a word matches nothing. */
export function scoreWords(fields: Field[], words: string[]): number | null {
  let total = 0
  for (const word of words) { const score = best(fields, word); if (score === null) return null; total += score }
  return total
}

/** The sessions the query finds, with their matching tabs and scores. A session not in the map does not match. */
export function matchSessions(entries: SearchEntry[], words: string[]): Map<string, SessionMatch> {
  const found = new Map<string, SessionMatch>()
  for (const entry of entries) {
    // Each word scores once against the session; a tab adds its own fields.
    const sessionWord = words.map(word => best(entry.fields, word))
    const own = sessionWord.every(score => score !== null) ? sessionWord.reduce<number>((sum, score) => sum + score!, 0) : null
    const tabScores = new Map<string, number>()
    for (const tab of entry.tabs) {
      let total = 0
      for (let i = 0; i < words.length && total >= 0; i++) {
        const top = Math.max(best(tab.fields, words[i]) ?? -1, sessionWord[i] ?? -1)
        total = top < 0 ? -1 : total + top
      }
      if (total >= 0) tabScores.set(tab.id, total)
    }
    if (own === null && !tabScores.size) continue
    const score = Math.max(own ?? 0, ...tabScores.values())
    const tabs = own !== null || tabScores.size === entry.tabs.length ? 'all' as const : new Set(tabScores.keys())
    found.set(entry.id, { score, tabs, tabScores, own })
  }
  return found
}
