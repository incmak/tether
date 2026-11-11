// The ask card in the chat (Dock.tsx ChoiceDock): its kind (the chip above the card, Chat.tsx), its one-line title and the detail box.
// Pure text work on what the server already sends; nothing here sends a key.

export type AskKind = 'permission' | 'plan' | 'question'
export const KIND_LABEL: Record<AskKind, string> = { permission: 'Needs permission', plan: 'Plan to review', question: 'Question' }

/** The kind chip. Panels, pickers, sign-in and /permissions keep their own cards and get none. */
export function askKind(plan: boolean, approval: boolean, attention?: { reason: string; options?: unknown[]; ask?: unknown; askBridge?: unknown; login?: unknown } | null): AskKind | null {
  if (plan) return 'plan'
  if (approval) return 'permission'
  if (!attention || attention.login || ['panel', 'picker', 'info', 'permissions'].includes(attention.reason)) return null
  return attention.options?.length || attention.ask || attention.askBridge ? 'question' : null
}

/** "Waiting 12m", or "Just asked" in the first minute. Both times are unix seconds. */
export function waited(since: number, now: number) {
  const seconds = Math.max(0, now - since)
  if (seconds < 60) return 'Just asked'
  return `Waiting ${seconds < 3600 ? `${Math.floor(seconds / 60)}m` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h` : `${Math.floor(seconds / 86400)}d`}`
}

export interface DetailLine { text: string; sign: '+' | '-' | '' }
/** title: the one-line request. lines: the full detail. question: the agent's own closing question, when it is not the title. */
export interface ApprovalParts { title: string; format: 'command' | 'diff' | 'text'; lines: DetailLine[]; question: string }

const BORDER = /^[\s│┃╭╮╰╯┌┐└┘─━═╌╍┄┅]+$/
const QUESTION = /^(?:Do you want|Would you like|Allow|Run this command|Do you trust)\b.*\?$/i
// The server drops this line when it fits one row; a wrapped one is dropped here. The card's options say it already.
const GENERIC = /^Do you want to proceed\?$/i
const FILE_HEADER = /\b(?:file|edit|update|write|create|patch)\b/i
// Claude numbers its diff rows ("13 -  old"); a plain "+new" or "-old" row counts only under a file header.
// A new file is all new: its numbered rows may start with "- " (a list), so they carry no sign.
const NEW_FILE = /^(?:create|write)\b/i
const NUMBERED_SIGN = /^\s*\d+\s*([+-])(?![+-])/
const PLAIN_SIGN = /^([+-])(?![+-])/

/** Split an approval's screen text into a title and the detail lines. Every line of the request stays visible in one of the parts. */
export function splitApproval(text: string): ApprovalParts {
  // Drop the box that some agents draw around the dialog, keep the indentation inside it.
  let rows = text.split('\n').filter(row => !row.trim() || !BORDER.test(row)).map(row => row.replace(/^\s*[│┃] ?/, '').replace(/\s*[│┃]\s*$/, '').trimEnd())
  const trim = () => { while (rows.length && !rows[0].trim()) rows.shift(); while (rows.length && !rows[rows.length - 1].trim()) rows.pop() }
  trim()
  let question = ''
  // A narrow pane wraps the closing question over two or three rows: read them as one line.
  for (let take = 1; take <= 3 && take < rows.length; take++) {
    const tail = rows.slice(-take)
    if (tail.some(row => !row.trim())) break
    const joined = tail.map(row => row.trim()).join(' ')
    if (QUESTION.test(joined)) { rows.splice(-take); question = GENERIC.test(joined) ? '' : joined; trim(); break }
  }
  let title = '', header = ''
  const first = rows[0]?.trim() ?? ''
  if (rows.length === 1 && first.length <= 160) { title = first; rows = [] }
  else if (rows.length > 1 && first.length <= 60 && !first.startsWith('$ ')) {
    title = header = first; rows.shift(); trim()
    // "Edit file" then the path on its own line: the title names the target.
    const target = rows[0]?.trim() ?? ''
    if (/\bfile$/i.test(header) && target && !/\s/.test(target) && rows.length > 1 && !NUMBERED_SIGN.test(rows[0])) { title = `${header} · ${target}`; rows.shift(); trim() }
  }
  if (!title && question) { title = question; question = '' }
  const indent = Math.min(...rows.filter(row => row.trim()).map(row => row.length - row.trimStart().length))
  rows = rows.map(row => row.slice(Number.isFinite(indent) ? indent : 0))
  const command = /\bcommand\b|^bash\b/i.test(header) || rows.some(row => row.startsWith('$ '))
  const fileHeader = FILE_HEADER.test(header)
  const lines = rows.map(row => {
    const sign = command || NEW_FILE.test(header) ? null : NUMBERED_SIGN.exec(row) ?? (fileHeader ? PLAIN_SIGN.exec(row) : null)
    return { text: row, sign: (sign?.[1] ?? '') as DetailLine['sign'] }
  })
  return { title, format: command ? 'command' : lines.some(line => line.sign) ? 'diff' : 'text', lines, question }
}

/** A plan's first heading is the card title; the rest is the plan to read. No heading at the top: no title. */
export function planParts(text: string): { title: string; body: string } {
  const match = /^\s*#{1,6}\s+(.+?)\s*#*\s*(?:\n|$)/.exec(text)
  return match ? { title: match[1], body: text.slice(match[0].length).replace(/^\n+/, '') } : { title: '', body: text }
}

/** A menu's question as the title: one short line. A longer text stays in the body as before. */
export const questionTitle = (text: string) => { const line = text.trim(); return line && line.length <= 200 && !line.includes('\n') ? line : '' }
