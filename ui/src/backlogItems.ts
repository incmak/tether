// Reads the backlog's Markdown into checklist rows, and changes it one line at a time. The file stays the only copy, so
// whatever else an agent or person wrote stays as it was. A change finds its line again by its text: an agent may have
// moved it since the person clicked.
export interface Source { agent: 'claude' | 'codex'; tmux?: string; pane?: string; chat?: string }
export interface Detail { line: number; text: string; done?: boolean; raw: string }
/** Who works on an item now ("Doing"), or what came of it ("Done"). Agents write these lines (server: backlog.py). */
export interface Status { kind: 'doing' | 'done'; text: string; source?: Source; line: number; raw: string }
/** ref: the item's ID, like B12, that agents and people use to name it. */
export interface Item { kind: 'item'; id: string; line: number; raw: string; done: boolean; text: string; ref?: string; source?: Source; status?: Status; details: Detail[] }
export interface Note { kind: 'note'; id: string; line: number; raw: string; text: string; heading: boolean }
export type Row = Item | Note
/** A change to the text. null: the line it needs is gone. */
export type Change = (text: string) => string | null

const CHECK = /^(\s*)[-*] \[([ xX])\] (.*)$/
const SOURCE = /\s*\((claude|codex)((?: · [^()·]+)*)\)\s*$/
const HEADING = /^#{1,6}\s+/
const ID = /\s+#B([0-9]+)\s*$/
const TOP = /^[-*] \[[ xX]\] /
const STATUS = /^\s+- (Doing|Done): (.*)$/
const indented = (line: string | undefined) => !!line && line.trim() !== '' && /^\s/.test(line)

/** "codex · tmux $3 · pane %12 · chat 019a" without its brackets. */
function sourceOf(agent: string, rest: string): Source {
  const source: Source = { agent: agent as Source['agent'] }
  for (const part of rest.split(' · ').slice(1)) {
    const [key, value] = part.trim().split(/\s+/, 2)
    if (value && (key === 'tmux' || key === 'pane' || key === 'chat')) source[key] = value
  }
  return source
}

function parseSource(text: string): { text: string; source?: Source } {
  const found = SOURCE.exec(text)
  if (!found || found.index === 0) return { text }
  return { text: text.slice(0, found.index), source: sourceOf(found[1], found[2]) }
}

/** The ID goes last, after who added the item. */
function parseItem(text: string): { text: string; ref?: string; source?: Source } {
  const id = ID.exec(text)
  const rest = id && id.index > 0 ? text.slice(0, id.index) : text
  return { ...parseSource(rest), ...(id && id.index > 0 ? { ref: `B${id[1]}` } : {}) }
}

function parseStatus(raw: string, line: number): Status | null {
  const found = STATUS.exec(raw)
  if (!found) return null
  const kind = found[1] === 'Doing' ? 'doing' : 'done'
  const plain = /^(claude|codex)((?: · [^()·]+)*)$/.exec(found[2].trim())
  if (kind === 'doing' && plain) return { kind, text: '', source: sourceOf(plain[1], plain[2]), line, raw }
  const { text, source } = parseSource(found[2])
  return { kind, text: text.trim(), source, line, raw }
}

/** Give each item line without an ID the next free one, in file order. The server does the same (backlog.py with_ids),
 * so a change shows the IDs it will be saved with, and a later change still finds its line. */
export function withIds(text: string): string {
  const lines = text.split('\n')
  let next = 1
  for (const line of lines) { const id = TOP.test(line) ? ID.exec(line) : null; if (id) next = Math.max(next, Number(id[1]) + 1) }
  return lines.map(line => TOP.test(line) && !ID.test(line) ? `${line.trimEnd()} #B${next++}` : line).join('\n')
}

export function parseBacklog(text: string): Row[] {
  const rows: Row[] = []
  const seen = new Map<string, number>()
  let item: Item | null = null
  text.split('\n').forEach((raw, line) => {
    if (!raw.trim()) return
    const check = CHECK.exec(raw)
    if (item && indented(raw)) {
      const status = parseStatus(raw, line)
      if (status) { item.status = status; return }
      item.details.push(check ? { line, raw, text: check[3], done: check[2] !== ' ' } : { line, raw, text: raw.trim().replace(/^[-*] /, '') })
      return
    }
    const count = seen.get(raw) ?? 0
    seen.set(raw, count + 1)
    const id = `${raw}\n${count}`
    if (check && !check[1]) {
      item = { kind: 'item', id, line, raw, done: check[2] !== ' ', ...parseItem(check[3]), details: [] }
      rows.push(item)
    } else {
      item = null
      rows.push({ kind: 'note', id, line, raw, text: raw.trim().replace(HEADING, ''), heading: HEADING.test(raw) })
    }
  })
  return rows
}

/** The nearest line with this exact text, or -1. */
function find(lines: string[], raw: string, near: number) {
  let best = -1
  lines.forEach((line, at) => { if (line === raw && (best < 0 || Math.abs(at - near) < Math.abs(best - near))) best = at })
  return best
}
const blockEnd = (lines: string[], at: number) => { let end = at + 1; while (indented(lines[end])) end += 1; return end }

/** A new item goes to the end of the inbox: before the first heading. */
export function addItem(item: string): Change {
  const line = `- [ ] ${item.trim().replace(/\s*\n\s*/g, ' ')}`
  return text => {
    const lines = text.split('\n')
    const heading = lines.findIndex(row => HEADING.test(row))
    let at = heading < 0 ? lines.length : heading
    while (at > 0 && !lines[at - 1].trim()) at -= 1
    lines.splice(at, 0, line)
    if (heading >= 0 && lines[at + 1]?.trim()) lines.splice(at + 1, 0, '')
    if (heading < 0 && lines[lines.length - 1] !== '') lines.push('')
    return lines.join('\n')
  }
}

export function setDone(raw: string, near: number, done: boolean): Change {
  return text => {
    const lines = text.split('\n'), at = find(lines, raw, near)
    if (at < 0) return null
    lines[at] = raw.replace(/^(\s*[-*] \[)[ xX](\] )/, `$1${done ? 'x' : ' '}$2`)
    return lines.join('\n')
  }
}

/** New wording for an item. Its checkbox and the record of who added it stay. */
export function rewordItem(item: Item, wording: string): Change {
  const check = CHECK.exec(item.raw)
  const tail = item.raw.slice(check ? check[0].length - check[3].length + item.text.length : 0)
  const start = check ? item.raw.slice(0, check[0].length - check[3].length) : ''
  const line = start + wording.trim().replace(/\s*\n\s*/g, ' ') + tail
  return text => {
    const lines = text.split('\n'), at = find(lines, item.raw, item.line)
    if (at < 0) return null
    lines[at] = line
    return lines.join('\n')
  }
}

/** Mark an item as sent to a chat. A second send replaces the first. */
export function startItem(raw: string, near: number, who: string): Change {
  return text => {
    const lines = text.split('\n'), at = find(lines, raw, near)
    if (at < 0) return null
    const end = blockEnd(lines, at)
    const kept = lines.slice(at + 1, end).filter(line => !STATUS.test(line))
    lines.splice(at + 1, end - at - 1, `  - Doing: ${who}`, ...kept)
    return lines.join('\n')
  }
}

export interface Removed { lines: string[]; before: string | null; line: number }

/** The item's lines, and what undoing its removal needs. */
export function blockOf(text: string, raw: string, near: number): Removed | null {
  const lines = text.split('\n'), at = find(lines, raw, near)
  if (at < 0) return null
  return { lines: lines.slice(at, blockEnd(lines, at)), before: at ? lines[at - 1] : null, line: at }
}

export function removeItem(raw: string, near: number): Change {
  return text => {
    const lines = text.split('\n'), at = find(lines, raw, near)
    if (at < 0) return null
    lines.splice(at, blockEnd(lines, at) - at)
    return lines.join('\n')
  }
}

/** Put a removed item back after the line it followed, or where it was. */
export function restoreItem(removed: Removed): Change {
  return text => {
    const lines = text.split('\n')
    const after = removed.before === null ? -1 : find(lines, removed.before, removed.line - 1)
    const at = removed.before === null ? 0 : after >= 0 ? after + 1 : Math.min(removed.line, lines.length)
    lines.splice(at, 0, ...removed.lines)
    return lines.join('\n')
  }
}

/** Move an item and its details before or after another item. */
export function moveItem(raw: string, near: number, targetRaw: string, targetNear: number, after: boolean): Change {
  return text => {
    const lines = text.split('\n'), from = find(lines, raw, near)
    if (from < 0) return null
    const end = blockEnd(lines, from)
    const block = lines.slice(from, end)
    const target = find(lines, targetRaw, targetNear)
    if (target < 0) return null
    if (target >= from && target < end) return text
    const rest = [...lines.slice(0, from), ...lines.slice(end)]
    const place = find(rest, targetRaw, target > from ? target - block.length : target)
    rest.splice(after ? blockEnd(rest, place) : place, 0, ...block)
    return rest.join('\n')
  }
}

/** Rows as the checklist shows them: open items by group, without a heading that has nothing open under it. */
export function openGroups(rows: Row[]): { heading: Note | null; rows: Row[] }[] {
  const groups: { heading: Note | null; rows: Row[] }[] = [{ heading: null, rows: [] }]
  for (const row of rows) {
    if (row.kind === 'note' && row.heading) groups.push({ heading: row, rows: [] })
    else if (row.kind === 'note' || !row.done) groups[groups.length - 1].rows.push(row)
  }
  return groups.filter(group => group.rows.length)
}

const refNumber = (item: Item) => Number(item.ref?.slice(1) ?? 0)
export const highestRef = (items: Item[]) => items.reduce((most, item) => Math.max(most, refNumber(item)), 0)

/** Open items an agent added after the person last looked. seen is the highest ID they saw; null is a first visit,
 * where nothing is new. The sidebar's "N new" and the sheet's New filter both count these. */
export const freshItems = (items: Item[], seen: number | null) => seen === null ? [] : items.filter(item => !item.done && !!item.source && refNumber(item) > seen)

/** The checklist's filters. They only choose which rows show; the file stays as it is. */
export const FILTERS = ['all', 'mine', 'agents', 'new'] as const
export type Filter = typeof FILTERS[number]
export const asFilter = (value: unknown): Filter => FILTERS.find(filter => filter === value) ?? 'all'

/** The items a filter shows. Mine: nobody is named as the source. From agents: a source is named. */
export function filterItems(items: Item[], filter: Filter, seen: number | null): Item[] {
  if (filter === 'mine') return items.filter(item => !item.source)
  if (filter === 'agents') return items.filter(item => !!item.source)
  return filter === 'new' ? freshItems(items, seen) : items
}

/** How many open items each filter shows. */
export function filterCounts(items: Item[], seen: number | null): Record<Filter, number> {
  const open = items.filter(item => !item.done)
  return { all: open.length, mine: filterItems(open, 'mine', seen).length, agents: filterItems(open, 'agents', seen).length, new: freshItems(open, seen).length }
}
