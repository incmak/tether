import type { ToolSummary } from './api'

// Turns a run of tool calls into words: what the agent did, how long it took, and which step took the time.
export type ToolKind = 'command' | 'edit' | 'read' | 'search' | 'web' | 'agent' | 'plan' | 'other'

export function toolKind(name: string): ToolKind {
  const key = name.toLowerCase()
  if (/todo|plan/.test(key)) return 'plan'
  if (/bash|shell|exec|command|run/.test(key)) return 'command'
  if (/edit|write|patch|notebook/.test(key)) return 'edit'
  if (/^read|view|open/.test(key)) return 'read'
  if (/web|fetch|browser|url/.test(key)) return 'web'
  if (/grep|glob|search|find|ls\b/.test(key)) return 'search'
  if (/task|agent/.test(key)) return 'agent'
  return 'other'
}

const fileName = (path: string) => path.split('/').pop() || path
const distinct = (items: string[]) => [...new Set(items.filter(Boolean))]
const SLOW_STEP_SECONDS = 30

function clause(kind: ToolKind, tools: ToolSummary[]) {
  const count = tools.length
  const files = distinct(tools.flatMap(tool => tool.title.split(', ')).map(fileName))
  if (kind === 'command') return count === 1 ? 'ran a command' : `ran ${count} commands`
  if (kind === 'edit') return files.length === 1 ? `edited ${files[0]}` : `edited ${files.length || count} files`
  if (kind === 'read') return files.length === 1 ? `read ${files[0]}` : `read ${files.length || count} files`
  if (kind === 'search') return count === 1 ? 'searched' : `searched ${count} times`
  if (kind === 'web') return count === 1 ? 'used the web' : `made ${count} web calls`
  if (kind === 'agent') return count === 1 ? 'started an agent' : `started ${count} agents`
  if (kind === 'plan') return 'updated the plan'
  return count === 1 ? `used ${tools[0].name.split('__').pop()}` : `used ${count} other tools`
}

const ORDER: ToolKind[] = ['edit', 'command', 'agent', 'web', 'read', 'search', 'other', 'plan']
const MAX_CLAUSES = 3

export function toolHeadline(tools: ToolSummary[]) {
  if (tools.length === 1) {
    const [tool] = tools
    if (tool.description) return tool.description
    if (toolKind(tool.name) === 'command' && tool.title) return `Ran ${tool.title}`
  }
  const clauses = ORDER.flatMap(kind => {
    const group = tools.filter(tool => toolKind(tool.name) === kind)
    return group.length ? [clause(kind, group)] : []
  })
  const shown = clauses.slice(0, MAX_CLAUSES)
  const rest = clauses.length - shown.length
  const text = shown.join(', ') + (rest ? ` and ${rest} more` : '')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

const stamp = (value?: string | null) => { const time = value ? Date.parse(value) : NaN; return Number.isNaN(time) ? null : time }

export function toolSeconds(tool: Pick<ToolSummary, 'started' | 'ended'>) {
  const started = stamp(tool.started), ended = stamp(tool.ended)
  return started === null || ended === null ? null : Math.max(0, (ended - started) / 1000)
}

// From the first start to the last end, so parallel calls do not count twice.
export function groupSeconds(tools: ToolSummary[]) {
  const starts = tools.flatMap(tool => stamp(tool.started) ?? [])
  const ends = tools.flatMap(tool => stamp(tool.ended) ?? [])
  return starts.length && ends.length ? Math.max(0, (Math.max(...ends) - Math.min(...starts)) / 1000) : null
}

// The step that explains a long group. Only worth a line when it took real time and the group has other steps.
export function slowestStep(tools: ToolSummary[]) {
  if (tools.length < 2) return null
  const timed = tools.flatMap(tool => { const seconds = toolSeconds(tool); return seconds === null ? [] : [{ tool, seconds }] })
  const slowest = timed.reduce<typeof timed[number] | null>((best, item) => !best || item.seconds > best.seconds ? item : best, null)
  if (!slowest || slowest.seconds < SLOW_STEP_SECONDS) return null
  return { text: slowest.tool.description || slowest.tool.title || slowest.tool.name, seconds: slowest.seconds }
}

// The newest steps of a group that is still running. The working line under the chat already names a single running
// step, so that step stays out of this list. Keeps its place in the full list so a row keeps its identity as the list slides.
export function liveTail(tools: ToolSummary[], runningShown: boolean, size: number) {
  const pending = tools.filter(tool => tool.status === 'pending')
  const end = runningShown && pending.length === 1 && tools[tools.length - 1] === pending[0] ? tools.length - 1 : tools.length
  const start = Math.max(0, end - size)
  return { steps: tools.slice(start, end), offset: start, earlier: start }
}
