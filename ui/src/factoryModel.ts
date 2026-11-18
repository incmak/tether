// The Factory view's model: every agent tab and backlog item as a task that moves through phases.
// Pure functions only, so the phase rules can be tested without a browser (ui/test_factory_model.cjs).
//
// Phase rules, first match wins. Each task carries `why`: the evidence the phase was guessed from.
//   Done    a backlog item checked off (or named on a DEPLOYED.md line) in the last 24 h,
//           or a tab with no item whose last turn ended in the last 24 h.
//   Inbox   an open backlog item that no open tab works on.
//   Deploy  an open item that a DEPLOYED.md line of the last 24 h names, or a tab whose latest steps run a deploy.
//   Review  a reviewer tab of the task is working, or the worker's latest step waits on a review (codex-lane, wait).
//   Verify  the worker's latest steps run tests, type checks, lint or screenshots.
//   Spec    the worker's recent steps only read and search: no edits yet.
//   Build   anything else that has a tab: edits, or no step evidence at all ("guessed").
import type { Pane } from './api'
import type { Item, Row } from './backlogItems'
import { needsYou } from './paneState'

export const PHASES = ['inbox', 'spec', 'build', 'review', 'verify', 'deploy', 'done'] as const
export type Phase = typeof PHASES[number]
export const PHASE_NAMES: Record<Phase, string> = { inbox: 'Inbox', spec: 'Spec', build: 'Build', review: 'Review', verify: 'Verify', deploy: 'Deploy', done: 'Done' }

export interface FactoryTool { name: string; title: string; at?: string | number | null; error?: boolean; done?: boolean }
export interface Deploy { at: number; timed: boolean; commit: string | null; text: string; refs: string[] }
export interface Commit { hash: string; at: number; subject: string; refs: string[]; repo: string }
/** One finding of the 30-minute worktree sweep (server/sweep.py): the tab's branch is behind main, its checks fail, or the tab is idle. */
export type SweepKind = 'behind' | 'checks' | 'idle'
export interface SweepLine { pane: string; name: string; branch: string; kind: SweepKind; text: string }
export interface Sweep { at: number; every: number; lines: SweepLine[] }
export const SWEEP_NAMES: Record<SweepKind, string> = { behind: 'Behind main', checks: 'Checks fail', idle: 'Idle worktree' }
export interface FactoryData { now: number; tools: Record<string, FactoryTool[]>; deployed: Deploy[]; commits: Commit[]; sweep?: Sweep }

export type Role = 'main' | 'worker' | 'reviewer' | 'advisor'
/** working: the agent runs a turn. prompt: a permission, question or plan on screen waits for Mak. waits: the turn ended with a question for Mak. idle: nothing. */
export type AgentState = 'working' | 'prompt' | 'waits' | 'idle'
export interface Agent { pane: Pane; name: string; role: Role; state: AgentState; step: string; tools: FactoryTool[]; last: number; sweep: SweepLine[] }
export interface Task {
  id: string
  ref?: string
  title: string
  group: string
  phase: Phase
  why: string
  agents: Agent[]
  item?: Item
  waits: boolean
  stuck: boolean
  last: number           // ms of the last activity we know of, 0 when unknown
  deploys: Deploy[]
  commits: Commit[]
  sweep: SweepLine[]     // the sweep findings of the task's tabs
}
export interface Floor { tasks: Task[]; parked: number }

export const DAY = 24 * 3600 * 1000
export const STUCK_MS = 30 * 60 * 1000       // no tab of a task in progress has moved for 30 min
const RECENT = 4                            // the latest steps that decide Verify, Review and Deploy
const SPEC_WINDOW = 12                      // Spec: none of the latest 12 steps edits a file

const EDITS = /^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|edit|write)$/i
const READS = /^(Read|Grep|Glob|LS|WebSearch|WebFetch|ToolSearch|TodoWrite|TaskList|Skill|AskUserQuestion|read_file|list_dir|grep|view_image|web_search|update_plan)$/i
const READ_COMMANDS = /^\s*(cd [^&;]+(&&|;)\s*)*(cat|head|tail|sed -n|grep|rg|ls|find|wc|git (log|show|diff|status|blame)|jq|tree|stat|file)\b/
const TESTS = /(unittest|pytest|run_ui_tests|test_[\w-]+\.(cjs|py|js|ts)|\bnpm (run )?test|vitest|jest|playwright|tsc -b|\beslint\b|screenshot|SHOTS=|\bcheck\b.*--)/i
const REVIEWS = /(codex-lane|codex exec|agent_tab_cli\.py (wait|start --reviews-me|send)|agent_advisor_cli)/
const DEPLOYS = /(deploy-ui\.py|\bdeploy\.sh\b|systemctl --user restart|restart-backend|\bfly deploy\b|gcloud run deploy)/
const REF = /\bB(\d{1,5})\b/

export const toMs = (at: string | number | null | undefined) => {
  if (at == null || at === '') return 0
  if (typeof at === 'number') return at < 1e12 ? at * 1000 : at
  const parsed = Date.parse(at)
  return Number.isNaN(parsed) ? 0 : parsed
}
export const refOf = (text: string | undefined) => text?.match(REF)?.[0]
export const paneName = (pane: Pane) => pane.pane_name || pane.brief?.name || pane.agent_name || `${pane.agent} ${pane.id}`

export function agentState(pane: Pane): AgentState {
  if (needsYou(pane.state)) return 'prompt'
  if (pane.state === 'Working') return 'working'
  if (pane.ask && !(pane.ask.snoozed_until && pane.ask.snoozed_until * 1000 > Date.now())) return 'waits'
  return 'idle'
}
export const STATE_NAMES: Record<AgentState, string> = { working: 'Working', prompt: 'Waits for you', waits: 'Waits for you', idle: 'Idle' }

const toolText = (tool: FactoryTool) => `${tool.name} ${tool.title}`
/** The step a tab is on now, in words: its latest tool call. */
export function stepOf(pane: Pane, tools: FactoryTool[]) {
  if (needsYou(pane.state)) return pane.needs?.text || (pane.state === 'Approval' ? 'Asks for permission' : 'Asks a question')
  const last = tools[tools.length - 1]
  if (!last) return pane.state === 'Working' ? 'Working (no step read)' : 'No step read'
  return last.title ? `${last.name}: ${last.title}` : last.name
}

/** The phase that one tab's latest steps show, or null when the steps show nothing. why: the evidence. */
export function phaseOfTools(tools: FactoryTool[]): { phase: Phase; why: string } | null {
  if (!tools.length) return null
  const latest = tools.slice(-RECENT)
  const find = (pattern: RegExp) => [...latest].reverse().find(tool => pattern.test(toolText(tool)))
  const deploy = find(DEPLOYS)
  if (deploy) return { phase: 'deploy', why: `latest steps deploy: ${short(deploy)}` }
  const review = latest.length && REVIEWS.test(toolText(latest[latest.length - 1])) ? latest[latest.length - 1] : undefined
  if (review) return { phase: 'review', why: `latest step waits on a review: ${short(review)}` }
  const test = find(TESTS)
  if (test) return { phase: 'verify', why: `latest steps check the work: ${short(test)}` }
  const window = tools.slice(-SPEC_WINDOW)
  const edits = window.some(tool => EDITS.test(tool.name))
  const reads = window.every(tool => READS.test(tool.name) || (/^(Bash|exec_command|shell)$/i.test(tool.name) && READ_COMMANDS.test(tool.title)))
  if (!edits && reads) return { phase: 'spec', why: `the latest ${window.length} steps only read and search, no edits yet` }
  const edit = [...window].reverse().find(tool => EDITS.test(tool.name))
  return { phase: 'build', why: edit ? `edits files: ${short(edit)}` : `latest step: ${short(tools[tools.length - 1])}` }
}
const short = (tool: FactoryTool) => { const text = tool.title ? `${tool.name} ${tool.title}` : tool.name; return text.length > 70 ? `${text.slice(0, 69)}…` : text }

/** When a backlog item was checked off: the date on its "Done:" line, in ms (local time), or 0. */
export function doneAt(item: Item) {
  const found = item.status?.kind === 'done' ? item.status.text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?/) : null
  if (!found) return 0
  return new Date(+found[1], +found[2] - 1, +found[3], +(found[4] ?? 0), +(found[5] ?? 0)).getTime()
}

interface Input {
  panes: Pane[]
  rows: Row[]                       // the whole backlog (parseBacklog)
  data: FactoryData | null
  groupName?: (pane: Pane) => string | undefined   // the tab's stack name
  now: number                       // ms
}

/** Every task on the floor. Tabs with no item that ended over 24 h ago and wait for nothing are "parked": counted, not shown. */
export function buildFloor({ panes, rows, data, groupName, now }: Input): Floor {
  const agents = panes.filter(pane => pane.agent !== 'shell')
  const byId = new Map(agents.map(pane => [pane.id, pane]))
  const toolsOf = (pane: Pane) => data?.tools[pane.id] ?? []
  const lastOf = (pane: Pane) => Math.max(toolMs(toolsOf(pane)), toMs(pane.finished ?? 0), pane.state === 'Working' ? now : 0)
  const reviewerOf = new Map<string, string>()   // reviewer pane -> the pane it reviews
  for (const pane of agents) for (const reviewer of [...(pane.reviewers ?? []), ...(pane.reviewers_paused ?? [])]) if (!reviewerOf.has(reviewer)) reviewerOf.set(reviewer, pane.id)
  const agent = (pane: Pane, role: Role): Agent => ({ pane, name: paneName(pane), role, state: agentState(pane), step: stepOf(pane, toolsOf(pane)), tools: toolsOf(pane), last: lastOf(pane), sweep: (data?.sweep?.lines ?? []).filter(line => line.pane === pane.id) })

  // Backlog items, with the project heading each sits under.
  const items: { item: Item; project: string }[] = []
  let project = ''
  for (const row of rows) {
    if (row.kind === 'note' && row.heading) project = row.text
    else if (row.kind === 'item' && row.ref) items.push({ item: row, project })
  }
  const deploysOf = (ref?: string) => ref ? (data?.deployed ?? []).filter(line => line.refs.includes(ref)) : []
  const commitsOf = (ref?: string) => ref ? (data?.commits ?? []).filter(commit => commit.refs.includes(ref)) : []

  // Which tabs work on which item: the item's "Doing:" tab, and tabs whose name carries the item's number or that the
  // item's notes name ("Worker tab: Factory View").
  const claimed = new Set<string>()
  const tasks: Task[] = []
  for (const { item, project } of items) {
    const ref = item.ref!
    const recentDone = item.done ? Math.max(doneAt(item), ...deploysOf(ref).map(line => line.at * 1000)) : 0
    if (item.done && now - recentDone > DAY) continue
    const linked: Agent[] = []
    const reasons: string[] = []
    const add = (pane: Pane | undefined, role: Role, reason: string) => {
      if (!pane || linked.some(entry => entry.pane.id === pane.id)) return
      linked.push(agent(pane, role)); claimed.add(pane.id); reasons.push(reason)
    }
    const named = agents.filter(pane => pane.id !== item.status?.source?.pane && pane.role !== 'advisor'
      && (refOf(pane.pane_name) === ref || refOf(pane.brief?.name) === ref
        || item.details.some(detail => !!pane.pane_name && detail.text.toLowerCase().includes(`tab: ${pane.pane_name.toLowerCase()}`))))
    const doing = item.status?.kind === 'doing' ? byId.get(item.status.source?.pane ?? '') : undefined
    if (!item.done) {
      add(doing, named.length ? 'main' : 'worker', `the item's Doing line names ${doing ? paneName(doing) : ''}`)
      for (const pane of named) add(pane, reviewerOf.has(pane.id) ? 'reviewer' : 'worker', refOf(pane.pane_name) === ref || refOf(pane.brief?.name) === ref ? `tab name ${paneName(pane)} carries ${ref}` : `the item's notes name tab ${paneName(pane)}`)
      for (const entry of [...linked]) for (const [reviewer, of] of reviewerOf) {
        const pane = byId.get(reviewer)
        if (of === entry.pane.id && pane) add(pane, 'reviewer', `${paneName(pane)} reviews ${entry.name}`)
      }
    }
    const deploys = deploysOf(ref)
    let phase: Phase
    let why: string
    if (item.done) { phase = 'done'; why = doneAt(item) ? `checked off ${clock(doneAt(item))}` : `checked off; DEPLOYED.md names ${ref}` }
    else if (!linked.length) {
      phase = 'inbox'
      why = item.status?.kind === 'doing' ? `open item; its Doing tab ${item.status.source?.pane ?? ''} is closed` : 'open item, no tab works on it'
    } else if (deploys.some(line => now - line.at * 1000 <= DAY)) {
      phase = 'deploy'; why = `DEPLOYED.md line ${clock(deploys[0].at * 1000)} names ${ref}; the item is still open`
    } else ({ phase, why } = phaseOfLinked(linked))
    const last = Math.max(0, ...linked.map(entry => entry.last), recentDone)
    tasks.push(finish({ id: ref, ref, title: item.text, group: project || 'Inbox', phase, why: reasons.length && phase !== 'inbox' ? `${why} · linked: ${reasons.join('; ')}` : why, agents: linked, item, last, deploys, commits: commitsOf(ref) }, now))
  }

  // Tabs with no item are tasks of their own. Reviewers and advisors join the task of the tab they serve.
  let parked = 0
  for (const pane of agents) {
    if (claimed.has(pane.id) || reviewerOf.has(pane.id)) continue
    const own = agent(pane, pane.role === 'advisor' ? 'advisor' : 'worker')
    const helpers = agents.filter(other => !claimed.has(other.id) && reviewerOf.get(other.id) === pane.id).map(other => agent(other, 'reviewer'))
    const linked = [own, ...helpers]
    const ref = refOf(pane.pane_name) ?? refOf(pane.brief?.name)
    const busy = linked.some(entry => entry.state !== 'idle')
    let phase: Phase
    let why: string
    if (!busy) {
      if (now - own.last > DAY || !own.last) { parked += 1; continue }
      phase = 'done'; why = `tab idle; its last turn ended ${clock(own.last)}; no backlog item`
    } else ({ phase, why } = phaseOfLinked(linked))
    tasks.push(finish({ id: pane.id, ref, title: own.name, group: groupName?.(pane) || folderName(pane.dir), phase, why, agents: linked, last: Math.max(...linked.map(entry => entry.last)), deploys: deploysOf(ref), commits: commitsOf(ref) }, now))
  }
  return { tasks, parked }
}

function phaseOfLinked(linked: Agent[]): { phase: Phase; why: string } {
  const reviewer = linked.find(entry => entry.role === 'reviewer' && entry.state === 'working')
  if (reviewer) return { phase: 'review', why: `reviewer ${reviewer.name} is working` }
  const doers = linked.filter(entry => entry.role === 'worker').concat(linked.filter(entry => entry.role === 'main'))
  // The newest steps of the doer that moved last decide.
  const lead = [...doers].sort((a, b) => toolMs(b.tools) - toolMs(a.tools) || b.last - a.last)[0] ?? linked[0]
  const found = phaseOfTools(lead.tools)
  if (found) return { phase: found.phase, why: `${lead.name}: ${found.why}` }
  return { phase: 'build', why: `guessed: ${lead.name} is open, no step read${lead.pane.agent === 'antigravity' ? ' (Tether reads no Antigravity steps)' : ''}` }
}

function finish(task: Omit<Task, 'waits' | 'stuck' | 'sweep'>, now: number): Task {
  const waits = task.agents.some(entry => entry.state === 'prompt' || entry.state === 'waits')
  const moving = task.agents.some(entry => entry.state === 'working')
  const open = task.phase !== 'inbox' && task.phase !== 'done'
  const stuck = open && !moving && !waits && !!task.last && now - task.last > STUCK_MS
  return { ...task, waits, stuck, sweep: task.agents.flatMap(entry => entry.sweep) }
}

const toolMs = (tools: FactoryTool[]) => Math.max(0, ...tools.map(tool => toMs(tool.at)))
const folderName = (dir: string) => (dir || '~').split('/').filter(Boolean).pop() || '~'
export const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })

/** Tasks that need Mak first, then the stuck ones, then the newest. */
export function attentionOrder(a: Task, b: Task) {
  return Number(b.waits) - Number(a.waits) || Number(b.stuck) - Number(a.stuck) || b.last - a.last
}

/** Tasks of one phase, in groups (backlog project or tab stack), biggest need first. */
export function groupsOf(tasks: Task[]) {
  const groups = new Map<string, Task[]>()
  for (const task of [...tasks].sort(attentionOrder)) groups.set(task.group, [...(groups.get(task.group) ?? []), task])
  return [...groups].map(([name, list]) => ({ name, tasks: list })).sort((a, b) => attentionOrder(a.tasks[0], b.tasks[0]) || b.tasks.length - a.tasks.length)
}

// Phase history, kept by this screen: the first time it saw each task in each phase. Nothing on the server records
// phase changes, so the history starts when the screen first loads and says so.
export type History = Record<string, { phase: Phase; at: number }[]>
// Tasks that leave the floor keep their history for a week, so an empty or failed poll does not erase it.
const HISTORY_DAYS = 7
export function recordHistory(history: History, tasks: Task[], now: number, keep = 40): { history: History; moved: Task[] } {
  const next: History = {}
  const moved: Task[] = []
  for (const [id, seen] of Object.entries(history)) if (seen.length && now - seen[seen.length - 1].at < HISTORY_DAYS * DAY) next[id] = seen
  for (const task of tasks) {
    const seen = history[task.id] ?? []
    const last = seen[seen.length - 1]
    if (!last) next[task.id] = [{ phase: task.phase, at: now }]
    else if (last.phase !== task.phase) { next[task.id] = [...seen, { phase: task.phase, at: now }].slice(-keep); moved.push(task) }
    else next[task.id] = seen
  }
  return { history: next, moved }
}
/** How long the task has been in its phase, as far as this screen saw. */
export const phaseSince = (history: History, task: Task) => history[task.id]?.[history[task.id].length - 1]?.at ?? 0

// Spatial focus for arrow keys and TV remotes: the nearest target in the pressed direction. Rows count first, so
// Down from a station goes to the item under it, not to a far one beside it.
export interface Box { x: number; y: number; width: number; height: number }
export type Direction = 'up' | 'down' | 'left' | 'right'
export function nearest(from: Box, boxes: Box[], direction: Direction): number {
  const cx = from.x + from.width / 2, cy = from.y + from.height / 2
  let best = -1, score = Infinity
  boxes.forEach((box, index) => {
    const bx = box.x + box.width / 2, by = box.y + box.height / 2
    const dx = bx - cx, dy = by - cy
    const ahead = direction === 'right' ? dx > 1 : direction === 'left' ? dx < -1 : direction === 'down' ? dy > 1 : dy < -1
    if (!ahead) return
    const along = direction === 'left' || direction === 'right' ? Math.abs(dx) : Math.abs(dy)
    const across = direction === 'left' || direction === 'right' ? Math.abs(dy) : Math.abs(dx)
    const value = along + across * 2
    if (value < score) { score = value; best = index }
  })
  return best
}
