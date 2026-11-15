// What the backlog shows: search, status, project, who added it, and the order. The file order stays the saved order;
// a sort only changes what is shown, so drag-to-reorder works only in the file order with nothing filtered out.
import type { Item, Note, Row } from './backlogItems'
import { exactField, field, marks, scoreWords, searchWords, wordScore } from './search'
import type { Field } from './search'

export type StatusFilter = 'open' | 'doing' | 'done' | 'all'
export type SortKey = 'manual' | 'newest' | 'oldest' | 'az'
export type Who = 'any' | 'me' | 'claude' | 'codex'
/** project: null shows every project; '' is the inbox (the items above the first heading). */
export interface View { query: string; status: StatusFilter; project: string | null; who: Who; sort: SortKey }
export const DEFAULT_VIEW: View = { query: '', status: 'open', project: null, who: 'any', sort: 'manual' }

export const STATUS_LABELS: Record<StatusFilter, string> = { open: 'Open', doing: 'In progress', done: 'Done', all: 'All' }
export const SORT_LABELS: Record<SortKey, string> = { manual: 'Manual order', newest: 'Newest first', oldest: 'Oldest first', az: 'A to Z' }
export const WHO_LABELS: Record<Who, string> = { any: 'Anyone', me: 'Me', claude: 'Claude', codex: 'Codex' }

const number = (item: Item) => Number(item.ref?.slice(1) ?? 0)

// The search fields of an item, made once per item and project: each keystroke only scores them.
// Title and ID count double. Project, who added it and status match fuzzy too. Details match whole words only.
const fieldCache = new WeakMap<Item, { project: string; fields: Field[] }>()
function itemFields(item: Item, project: string) {
  const cached = fieldCache.get(item)
  if (cached && cached.project === project) return cached.fields
  const fields = [
    field(2, item.text, item.ref),
    field(1, project, item.source?.agent, item.status?.text, item.status?.kind === 'doing' ? 'doing in progress' : ''),
    exactField(1, ...item.details.map(detail => detail.text)),
  ]
  fieldCache.set(item, { project, fields })
  return fields
}
const isRef = (word: string) => /^b\d+$/.test(word)

/**
 * The search score of an item: the same matcher as the sidebar and ⌘K (search.ts). null: no match.
 * Every word must match. An ID alone like "b12" matches that item only.
 */
export const queryScore = (item: Item, query: string, project = '') => wordsScore(item, searchWords(query), project)

function wordsScore(item: Item, words: string[], project: string) {
  if (!words.length) return 0
  if (words.length === 1 && isRef(words[0])) return item.ref?.toLowerCase() === words[0] ? 10 : null
  return scoreWords(itemFields(item, project), words)
}

export const matchesQuery = (item: Item, query: string, project = '') => queryScore(item, query, project) !== null

/** True when a detail is why the item matches: a word that the title, ID, project and status do not match. Show the details then. */
export function detailMatched(item: Item, query: string, project = '') {
  // An ID alone matches the ID only. In a longer query an ID is a plain word and can match a note ("Blocked by B1234").
  const words = searchWords(query)
  if (!words.length || (words.length === 1 && isRef(words[0])) || !matchesQuery(item, query, project)) return false
  const [title, meta, details] = itemFields(item, project)
  return words.some(word => wordScore(title, word) === null && wordScore(meta, word) === null && wordScore(details, word) !== null)
}

export function matchesStatus(item: Item, status: StatusFilter) {
  if (status === 'all') return true
  if (status === 'done') return item.done
  if (status === 'doing') return !item.done && item.status?.kind === 'doing'
  return !item.done
}

export function matchesWho(item: Item, who: Who) {
  if (who === 'any') return true
  if (who === 'me') return !item.source
  return item.source?.agent === who
}

export interface Group { key: string; heading: Note | null; name: string; items: Item[] }

/** Items under each heading, in file order. The inbox is the part above the first heading. */
export function projectGroups(rows: Row[]): Group[] {
  const groups: Group[] = [{ key: 'inbox', heading: null, name: '', items: [] }]
  for (const row of rows) {
    if (row.kind === 'note' && row.heading) groups.push({ key: row.id, heading: row, name: row.text, items: [] })
    else if (row.kind === 'item') groups[groups.length - 1].items.push(row)
  }
  return groups
}

export interface ProjectCount { name: string; open: number; doing: number; done: number }
/** Each project with its counts, for the side list. Two headings with one name count as one project. */
export function projectCounts(rows: Row[]): ProjectCount[] {
  const counts = new Map<string, ProjectCount>()
  for (const group of projectGroups(rows)) {
    const entry = counts.get(group.name) ?? { name: group.name, open: 0, doing: 0, done: 0 }
    for (const item of group.items) {
      if (item.done) entry.done += 1
      else { entry.open += 1; if (item.status?.kind === 'doing') entry.doing += 1 }
    }
    counts.set(group.name, entry)
  }
  return [...counts.values()].filter(entry => !entry.name || entry.open + entry.done > 0)
}

const compare: Record<Exclude<SortKey, 'manual'>, (a: Item, b: Item) => number> = {
  newest: (a, b) => number(b) - number(a),
  oldest: (a, b) => number(a) - number(b),
  az: (a, b) => a.text.localeCompare(b.text, undefined, { sensitivity: 'base', numeric: true }),
}

/** The groups the list shows: filtered, then sorted inside each group. A group with nothing left is hidden. */
export function viewGroups(rows: Row[], view: View, statusOverride?: StatusFilter): Group[] {
  const status = statusOverride ?? view.status
  const words = searchWords(view.query)
  return projectGroups(rows)
    .filter(group => view.project === null || group.name === view.project)
    .map(group => {
      const scores = new Map<Item, number>()
      for (const item of group.items) {
        if (!matchesStatus(item, status) || !matchesWho(item, view.who)) continue
        const score = wordsScore(item, words, group.name)
        if (score !== null) scores.set(item, score)
      }
      const items = [...scores.keys()]
      // A search in the saved order shows the best match first; a chosen sort keeps its order.
      if (view.sort !== 'manual') items.sort(compare[view.sort])
      else if (view.query.trim()) items.sort((a, b) => scores.get(b)! - scores.get(a)!)
      return { ...group, items }
    })
    .filter(group => group.items.length)
}

/** Drag-to-reorder changes the saved order, so it works only while the list shows the open items in the saved order. */
export const canReorder = (view: View) => view.sort === 'manual' && !view.query.trim() && view.who === 'any' && view.status === 'open'

/** True when anything but the order narrows the list. */
export const isFiltered = (view: View) => !!view.query.trim() || view.status !== 'open' || view.project !== null || view.who !== 'any'

/** The parts of a text that match the query, for highlighting: [text, matched] pieces (search.ts marks()).
 * fuzzy false for the details, which match whole substrings only. An ID word finds its item, so it marks nothing. */
export const highlight = (text: string, query: string, fuzzy = true) => marks(text, searchWords(query).filter(part => !isRef(part)), fuzzy)
