// Reads the backlog's Markdown into checklist rows, and changes it one line at a time. The file stays the only copy, so
// whatever else an agent or person wrote stays as it was. A change finds its line again by its text: an agent may have
// moved it since the person clicked.
export interface Source { agent: 'claude' | 'codex'; tmux?: string; pane?: string; chat?: string }
export interface Detail { line: number; text: string; done?: boolean; raw: string }
export interface Item { kind: 'item'; id: string; line: number; raw: string; done: boolean; text: string; source?: Source; details: Detail[] }
export interface Note { kind: 'note'; id: string; line: number; raw: string; text: string; heading: boolean }
export type Row = Item | Note
/** A change to the text. null: the line it needs is gone. */
export type Change = (text: string) => string | null

const CHECK = /^(\s*)[-*] \[([ xX])\] (.*)$/
const SOURCE = /\s*\((claude|codex)((?: · [^()·]+)*)\)\s*$/
const HEADING = /^#{1,6}\s+/
const indented = (line: string | undefined) => !!line && line.trim() !== '' && /^\s/.test(line)

function parseSource(text: string): { text: string; source?: Source } {
  const found = SOURCE.exec(text)
  if (!found || found.index === 0) return { text }
  const source: Source = { agent: found[1] as Source['agent'] }
  for (const part of found[2].split(' · ').slice(1)) {
    const [key, value] = part.trim().split(/\s+/, 2)
    if (value && (key === 'tmux' || key === 'pane' || key === 'chat')) source[key] = value
  }
  return { text: text.slice(0, found.index), source }
}

export function parseBacklog(text: string): Row[] {
  const rows: Row[] = []
  const seen = new Map<string, number>()
  let item: Item | null = null
  text.split('\n').forEach((raw, line) => {
    if (!raw.trim()) return
    const check = CHECK.exec(raw)
    if (item && indented(raw)) {
      item.details.push(check ? { line, raw, text: check[3], done: check[2] !== ' ' } : { line, raw, text: raw.trim().replace(/^[-*] /, '') })
      return
    }
    const count = seen.get(raw) ?? 0
    seen.set(raw, count + 1)
    const id = `${raw}\n${count}`
    if (check && !check[1]) {
      item = { kind: 'item', id, line, raw, done: check[2] !== ' ', ...parseSource(check[3]), details: [] }
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
