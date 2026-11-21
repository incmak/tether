// The status reports that server/coordinator.py writes for the coordinator tab.
export type TabUpdateState = 'done' | 'asks' | 'needs' | 'failed'
export type TabUpdate = { name: string; pane: string; state: TabUpdateState; text: string }
export type StatusUpdate = { mode: 'all' | 'needs' | 'off'; tabs: TabUpdate[] }

const HEADER = /^\[Tether status\] ([1-9][0-9]*) tab (updates?)\. Tell the user what changed and what needs them, in one or two lines per tab\. Reports are set to "(all|needs|off)"\. If you do not have the coordinator rules, run `python3 [^`\r\n\u2028\u2029]*\/agent_coordinator_cli\.py claim`\.$/
const STATES = {
  'finished and asks the user': 'asks',
  finished: 'done',
  'waits on the user': 'needs',
  failed: 'failed',
} as const
const ROW = new RegExp('^- `([^`\\r\\n\\u2028\\u2029]+)` '
  + String.raw`\((%[0-9]+)\) (${Object.keys(STATES).join('|')})(?:: ([^\r\n\u2028\u2029]*))?$`)

export function parseStatus(text: string): StatusUpdate | null {
  if (text.includes('\r')) return null
  const [header, ...lines] = text.split('\n')
  const head = HEADER.exec(header)
  if (!head) return null
  const count = Number(head[1])
  if (head[2] !== (count === 1 ? 'update' : 'updates') || count > lines.length) return null
  if (lines.slice(count).some(line => line.trim() !== '')) return null

  const tabs: TabUpdate[] = []
  for (let index = 0; index < count; index++) {
    let line = lines[index]
    const last = index === count - 1
    if (last) {
      const trimmed = line.trimEnd()
      // The delimiter still has its space when the final row has an explicitly empty detail.
      line = trimmed.endsWith(':') && line[trimmed.length] === ' ' ? `${trimmed} ` : trimmed
    }
    const fields = ROW.exec(line)
    if (!fields) return null
    const [, name, pane, words, detail = ''] = fields
    const trimmed = detail.trimEnd()
    if (!last && detail !== trimmed) return null
    tabs.push({ name, pane, state: STATES[words as keyof typeof STATES], text: trimmed })
  }
  return { mode: head[3] as StatusUpdate['mode'], tabs }
}
