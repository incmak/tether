// What the backlog shows: search, status, project, who added it, and the order. The file order stays the saved order;
// a sort only changes what is shown, so drag-to-reorder works only in the file order with nothing filtered out.
import type { Item, Note, Row } from './backlogItems'

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
const words = (query: string) => query.toLowerCase().split(/\s+/).filter(Boolean)

/** All the text a search reads: title, ID, details, status and who added it. */
export const searchText = (item: Item, project = '') => [item.text, item.ref, project, item.source?.agent, item.status?.text, item.status?.kind === 'doing' ? 'doing in progress' : '', ...item.details.map(detail => detail.text)]
  .filter(Boolean).join(' \n ').toLowerCase()

/** Every word of the query is in the item. An ID like "b12" matches that item only. */
export function matchesQuery(item: Item, query: string, project = '') {
  const parts = words(query)
  if (!parts.length) return true
  if (parts.length === 1 && /^b\d+$/.test(parts[0])) return item.ref?.toLowerCase() === parts[0]
  const text = searchText(item, project)
  return parts.every(part => text.includes(part))
}

/** A search score: a match in the title counts more than one in the details. null: no match. */
export function queryScore(item: Item, query: string, project = '') {
  if (!matchesQuery(item, query, project)) return null
  const title = item.text.toLowerCase()
  return words(query).reduce((score, part) => score + (title.startsWith(part) ? 4 : title.includes(part) ? 3 : 1), 0)
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
  return projectGroups(rows)
    .filter(group => view.project === null || group.name === view.project)
    .map(group => {
      const items = group.items.filter(item => matchesStatus(item, status) && matchesWho(item, view.who) && matchesQuery(item, view.query, group.name))
      return { ...group, items: view.sort === 'manual' ? items : [...items].sort(compare[view.sort]) }
    })
    .filter(group => group.items.length)
}

/** Drag-to-reorder changes the saved order, so it works only while the list shows the open items in the saved order. */
export const canReorder = (view: View) => view.sort === 'manual' && !view.query.trim() && view.who === 'any' && view.status === 'open'

/** True when anything but the order narrows the list. */
export const isFiltered = (view: View) => !!view.query.trim() || view.status !== 'open' || view.project !== null || view.who !== 'any'

/** The parts of a title that match the query, for highlighting. Returns [text, matched] pieces. */
export function highlight(text: string, query: string): [string, boolean][] {
  const parts = words(query).filter(part => !/^b\d+$/.test(part)).map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  if (!parts.length) return [[text, false]]
  // A split on a capturing pattern puts the matches at the odd places.
  return text.split(new RegExp(`(${parts.join('|')})`, 'i')).map((piece, at) => [piece, at % 2 === 1] as [string, boolean]).filter(([piece]) => piece)
}
