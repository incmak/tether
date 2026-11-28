// The Factory view's model: every agent tab and backlog item as a task that moves through phases.
// Pure functions only, so the phase rules can be tested without a browser (ui/test_factory_model.cjs).
//
// Direct backlog links come first. Parent links group the remaining tabs under their root unless their backlog
// refs differ. Reviewers follow the tab they review even when a parent link points elsewhere.
//
// Phase rules, first match wins. Each task carries `why`: the evidence the phase was guessed from.
//   Done    a backlog item checked off (or named on a DEPLOYED.md line) in the last 24 h,
//           or an idle task with no item whose latest activity was in the last 24 h.
//   Inbox   an open backlog item that no open tab works on.
//   Deploy  an open item that a DEPLOYED.md line of the last 24 h names, or a tab whose latest steps run a deploy.
//   Review  a reviewer tab of the task is working, or the worker's latest step waits on a review (codex-lane, wait).
//   Verify  the worker's latest steps run tests, type checks, lint or screenshots.
//   Spec    the worker's recent steps only read and search: no edits yet.
//   Build   anything else that has a tab: edits, or no step evidence at all ("guessed").
import type { Pane } from './api'
import type { Item, Row } from './backlogItems'
import { needsYou } from './paneState'
import { dayParts, fmtDate, fmtDay, fmtTime, zonedTime, zonedTimeLatest } from './zone'

export const PHASES = ['inbox', 'spec', 'build', 'review', 'verify', 'deploy', 'done'] as const
export type Phase = typeof PHASES[number]
export const PHASE_NAMES: Record<Phase, string> = { inbox: 'Inbox', spec: 'Spec', build: 'Build', review: 'Review', verify: 'Verify', deploy: 'Deploy', done: 'Done' }

export interface FactoryTool { name: string; title: string; at?: string | number | null; error?: boolean; done?: boolean }
export interface Deploy { at: number; day?: string; timed: boolean; commit: string | null; text: string; refs: string[] }
export interface Commit { hash: string; at: number; subject: string; refs: string[]; repo: string }
/** One finding of the 30-minute worktree sweep (server/sweep.py): the tab's branch is behind main, its checks fail, or the tab is idle. */
export type SweepKind = 'behind' | 'checks' | 'idle'
export interface SweepLine { pane: string; name: string; branch: string; kind: SweepKind; text: string }
export interface Sweep { at: number; every: number; lines: SweepLine[] }
export const SWEEP_NAMES: Record<SweepKind, string> = { behind: 'Behind main', checks: 'Checks fail', idle: 'Idle worktree' }
export interface FactoryData { now: number; tools: Record<string, FactoryTool[]>; deployed: Deploy[]; commits: Commit[]; sweep?: Sweep; history: History }

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

const EDITS = /^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|edit|write|edit_file|write_file|notebook_edit)$/i
const READS = /^(Read|Grep|Glob|LS|WebSearch|WebFetch|ToolSearch|TodoWrite|TaskList|Skill|AskUserQuestion|read_file|list_dir|grep|view_image|web_search|update_plan|update_steps|view_file|grep_search|find_by_name|search_in_file|view_content_chunk|code_search|search_web|command_status|read_terminal|ask_question)$/i
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
  if (pane.ask && !pane.ask.later && !(pane.ask.snoozed_until && pane.ask.snoozed_until * 1000 > Date.now())) return 'waits'
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
  const reads = window.every(tool => READS.test(tool.name) || (/^(Bash|exec_command|shell|run_command)$/i.test(tool.name) && READ_COMMANDS.test(tool.title)))
  if (!edits && reads) return { phase: 'spec', why: `the latest ${window.length} steps only read and search, no edits yet` }
  const edit = [...window].reverse().find(tool => EDITS.test(tool.name))
  return { phase: 'build', why: edit ? `edits files: ${short(edit)}` : `latest step: ${short(tools[tools.length - 1])}` }
}
const short = (tool: FactoryTool) => { const text = tool.title ? `${tool.name} ${tool.title}` : tool.name; return text.length > 70 ? `${text.slice(0, 69)}…` : text }

/** When an item was checked off, in ms, or 0. A bare stamp uses the later instant in the Tether zone. */
export function doneAt(item: Item, zone?: string) {
  const stamp = item.status?.stamp
  if (!stamp) return 0
  const time = stamp.time ?? '00:00'
  if (stamp.offset) {
    const offset = stamp.offset.replace(/^([+-]\d{2})(\d{2})$/, '$1:$2')
    return Date.parse(`${stamp.day}T${time.padStart(5, '0')}${offset}`) || 0
  }
  const [year, month, day] = stamp.day.split('-').map(Number)
  const [hour, minute] = time.split(':').map(Number)
  return zonedTimeLatest(year, month - 1, day, hour, minute, zone)
}

function deployAt(line: Deploy, zone?: string) {
  const day = line.timed ? undefined : dayParts(line.day)
  if (!day) return line.at * 1000
  return zonedTime(day.year, day.month, day.date + 1, 0, 0, zone) - 1
}

export function deployLabel(line: Deploy) {
  if (line.timed) return clock(line.at * 1000)
  return line.day ? fmtDay(line.day) : fmtDate(line.at * 1000)
}

interface Input {
  panes: Pane[]
  rows: Row[]                       // the whole backlog (parseBacklog)
  data: FactoryData | null
  groupName?: (pane: Pane) => string | undefined   // the tab's stack name
  now: number                       // ms
  zone?: string
}

interface Links { ref?: string; agents: Agent[]; reasons: string[] }
const withLinks = (why: string, reasons: string[]) => reasons.length ? `${why} · linked: ${reasons.join('; ')}` : why

/** Tasks with no item and no activity in the past 24 h count as parked while all members are idle. */
export function buildFloor({ panes, rows, data, groupName, now, zone }: Input): Floor {
  const byId = new Map(panes.filter(pane => pane.agent !== 'shell').map(pane => [pane.id, pane]))
  const agents = [...byId.values()]
  const toolsOf = (pane: Pane) => data?.tools[pane.id] ?? []
  const lastOf = (pane: Pane) => Math.max(toolMs(toolsOf(pane)), toMs(pane.finished ?? 0), pane.state === 'Working' ? now : 0)
  const reviewerOf = new Map<Pane, Pane>()   // reviewer pane -> the pane it reviews
  for (const pane of agents) {
    for (const id of [...(pane.reviewers ?? []), ...(pane.reviewers_paused ?? [])]) {
      const reviewer = byId.get(id)
      if (reviewer && reviewer !== pane && !reviewerOf.has(reviewer)) reviewerOf.set(reviewer, pane)
    }
  }
  const agent = (pane: Pane, role: Role): Agent => ({ pane, name: paneName(pane), role, state: agentState(pane), step: stepOf(pane, toolsOf(pane)), tools: toolsOf(pane), last: lastOf(pane), sweep: (data?.sweep?.lines ?? []).filter(line => line.pane === pane.id) })

  // Resolve reviews before parent links. In --review-it, the new tab's parent is its reviewer.
  const reviewRoots = new Map<Pane, Pane>()
  for (const pane of agents) {
    const visited = new Set<Pane>()
    let root = pane
    while (!reviewRoots.has(root) && !visited.has(root)) {
      visited.add(root)
      const reviewed = reviewerOf.get(root)
      if (!reviewed) break
      root = reviewed
    }
    root = reviewRoots.get(root) ?? root
    for (const member of visited) reviewRoots.set(member, root)
  }
  const reviewRoot = (pane: Pane) => reviewRoots.get(pane)!
  const roleOf = (pane: Pane, role: Role = 'worker'): Role =>
    pane.role === 'advisor' ? 'advisor' : reviewRoot(pane) === pane ? role : 'reviewer'

  // Backlog items, with the project heading each sits under.
  const items: { item: Item; project: string; links: Links }[] = []
  let project = ''
  for (const row of rows) {
    if (row.kind === 'note' && row.heading) project = row.text
    else if (row.kind === 'item' && row.ref) items.push({ item: row, project, links: { ref: row.ref, agents: [], reasons: [] } })
  }
  const deploysOf = (ref?: string) => ref ? (data?.deployed ?? []).filter(line => line.refs.includes(ref)) : []
  const commitsOf = (ref?: string) => ref ? (data?.commits ?? []).filter(commit => commit.refs.includes(ref)) : []

  // Which tabs work on which item: the item's "Doing:" tab, and tabs whose name carries the item's number or that the
  // item's notes name ("Worker tab: Factory View").
  const direct = new Map<Pane, { links: Links; role: Role; reason: string }>()
  for (const { item, links } of items) {
    if (item.done) continue
    const ref = links.ref
    const add = (pane: Pane | undefined, role: Role, reason: string) => {
      if (pane && !direct.has(pane)) direct.set(pane, { links, role: roleOf(pane, role), reason })
    }
    const named = agents.filter(pane => pane.id !== item.status?.source?.pane && pane.role !== 'advisor'
      && (refOf(pane.pane_name) === ref || refOf(pane.brief?.name) === ref
        || item.details.some(detail => !!pane.pane_name && detail.text.toLowerCase().includes(`tab: ${pane.pane_name.toLowerCase()}`))))
    const doing = item.status?.kind === 'doing' ? byId.get(item.status.source?.pane ?? '') : undefined
    if (doing) add(doing, named.length ? 'main' : 'worker', `the item's Doing line names ${paneName(doing)}`)
    for (const pane of named) add(pane, 'worker', refOf(pane.pane_name) === ref || refOf(pane.brief?.name) === ref ? `tab name ${paneName(pane)} carries ${ref}` : `the item's notes name tab ${paneName(pane)}`)
  }

  // A review group follows the reviewed tab's direct link, or inherits a reviewer's link if it has none.
  const assigned = new Map<Pane, Links>()
  for (const [pane, claim] of direct) {
    const root = reviewRoot(pane)
    if (root === pane || !assigned.has(root)) assigned.set(root, claim.links)
  }
  const standalone = new Map<Pane, Links>()
  const startTask = (pane: Pane) => {
    const links = { ref: refOf(pane.pane_name) ?? refOf(pane.brief?.name), agents: [agent(pane, roleOf(pane))], reasons: [] }
    assigned.set(pane, links)
    standalone.set(pane, links)
  }
  const startedBy = new Map<Pane, Pane>()
  for (const pane of agents) {
    const chain = new Map<Pane, Pane>()
    let root = reviewRoot(pane)
    while (!assigned.has(root) && !chain.has(root)) {
      const parent = byId.get(root.parent ?? '')
      if (!parent) break
      chain.set(root, parent)
      root = reviewRoot(parent)
    }
    if (!assigned.has(root)) startTask(root)
    // Walk back toward the child so a ref conflict becomes a new root for its descendants.
    for (const [child, parent] of [...chain].reverse()) {
      if (assigned.has(child)) continue
      const links = assigned.get(reviewRoot(parent))!
      const refs = [refOf(child.pane_name), refOf(child.brief?.name)]
      if (refs.some(ref => ref && ref !== links.ref)) startTask(child)
      else {
        assigned.set(child, links)
        startedBy.set(child, parent)
      }
    }
  }

  // Keep the direct members first, then append the remaining members to their one assigned task.
  const added = new Set<Pane>(standalone.keys())
  for (const [pane, claim] of direct) {
    if (assigned.get(reviewRoot(pane)) !== claim.links) continue
    claim.links.agents.push(agent(pane, claim.role))
    claim.links.reasons.push(claim.reason)
    added.add(pane)
  }
  for (const pane of agents) {
    if (added.has(pane)) continue
    const links = assigned.get(reviewRoot(pane))!
    links.agents.push(agent(pane, roleOf(pane)))
    const reviewed = reviewerOf.get(pane)
    const parent = startedBy.get(pane)
    if (reviewed && reviewRoot(pane) !== pane) links.reasons.push(`${paneName(pane)} reviews ${paneName(reviewed)}`)
    else if (parent) links.reasons.push(`${paneName(pane)} was started by ${paneName(parent)}`)
  }

  // The lead tab of a task is "main" when it has started a worker there.
  const promote = (linked: Agent[]) => {
    const head = linked[0]
    if (head?.role === 'worker' && linked.some(entry => entry !== head && entry.role === 'worker' && startedBy.has(entry.pane))) head.role = 'main'
  }
  const tasks: Task[] = []
  for (const { item, project, links } of items) {
    const ref = item.ref!
    const deploys = deploysOf(ref)
    const checkedOff = doneAt(item, zone)
    const recentDone = item.done ? Math.max(checkedOff, ...deploys.map(line => deployAt(line, zone))) : 0
    if (item.done && now - recentDone > DAY) continue
    const { agents: linked, reasons } = links
    promote(linked)
    let phase: Phase
    let why: string
    if (item.done) { phase = 'done'; why = checkedOff ? `checked off ${clock(checkedOff)}` : `checked off; DEPLOYED.md names ${ref}` }
    else if (!linked.length) {
      phase = 'inbox'
      why = item.status?.kind === 'doing' ? `open item; its Doing tab ${item.status.source?.pane ?? ''} is closed` : 'open item, no tab works on it'
    } else {
      const recent = deploys.find(line => now - deployAt(line, zone) <= DAY)
      if (recent) {
        phase = 'deploy'; why = `DEPLOYED.md line ${deployLabel(recent)} names ${ref}; the item is still open`
      } else ({ phase, why } = phaseOfLinked(linked))
    }
    const last = Math.max(0, ...linked.map(entry => entry.last), recentDone)
    tasks.push(finish({ id: ref, ref, title: item.text, group: project || 'Inbox', phase, why: withLinks(why, reasons), agents: linked, item, last, deploys, commits: commitsOf(ref) }, now))
  }

  // Each unclaimed root and its members make one task. A quiet task with old activity is parked.
  let parked = 0
  for (const pane of agents) {
    const links = standalone.get(pane)
    if (!links) continue
    const { agents: linked, ref, reasons } = links
    const own = linked[0]
    promote(linked)
    const busy = linked.some(entry => entry.state !== 'idle')
    const last = Math.max(...linked.map(entry => entry.last))
    let phase: Phase
    let why: string
    if (!busy) {
      if (now - last > DAY || !last) { parked += 1; continue }
      phase = 'done'
      why = linked.length === 1 ? `tab idle; its last turn ended ${clock(last)}; no backlog item` : `tabs idle; last activity ${clock(last)}; no backlog item`
    } else ({ phase, why } = phaseOfLinked(linked))
    tasks.push(finish({ id: pane.id, ref, title: own.name, group: groupName?.(pane) || folderName(pane.dir), phase, why: withLinks(why, reasons), agents: linked, last, deploys: deploysOf(ref), commits: commitsOf(ref) }, now))
  }
  return { tasks, parked }
}

function phaseOfLinked(linked: Agent[]): { phase: Phase; why: string } {
  const reviewer = linked.find(entry => entry.role === 'reviewer' && entry.state === 'working')
  if (reviewer) return { phase: 'review', why: `reviewer ${reviewer.name} is working` }
  const doers = linked.filter(entry => entry.role === 'worker').concat(linked.filter(entry => entry.role === 'main'))
  // The newest steps of the doer that moved last decide.
  // A task with no worker (an advisor whose parent closed) reads its own steps.
  const lead = [...doers].sort((a, b) => toolMs(b.tools) - toolMs(a.tools) || b.last - a.last)[0] ?? linked[0]
  const found = phaseOfTools(lead.tools)
  if (found) return { phase: found.phase, why: `${lead.name}: ${found.why}` }
  return { phase: 'build', why: `guessed: ${lead.name} is open, no step read` }
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
export const clock = (ms: number) => fmtTime(ms, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })

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

export type History = Record<string, { phase: Phase; at: number }[]>
const CATCH_UP_MS = 60_000
const lastPhase = (history: History, id: string) => {
  const entries = history[id]
  return entries?.[entries.length - 1]
}

/** A different server phase needs a local move, a first observation, or a one-time catch-up. */
export function historyMoves(
  history: History,
  serverNowMs: number,
  previous: ReadonlyMap<string, Phase> | null,
  seen: ReadonlySet<string>,
  tasks: readonly Pick<Task, 'id' | 'phase'>[],
  pending: ReadonlyMap<string, Phase>,
) {
  const next = new Map<string, Phase>()
  for (const { id, phase } of tasks) {
    const last = lastPhase(history, id)
    if (last?.phase === phase) continue
    const before = previous?.get(id)
    const changed = before !== undefined && before !== phase
    const catchUp = last && !seen.has(id) && serverNowMs - last.at > CATCH_UP_MS
    if (!last || changed || catchUp || pending.get(id) === phase) next.set(id, phase)
  }
  return { moves: Array.from(next, ([task, phase]) => ({ task, phase })), pending: next }
}
/** When the server first recorded the current phase. */
export function phaseSince(history: History, task: Task) {
  const last = lastPhase(history, task.id)
  return last?.phase === task.phase ? last.at : 0
}

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
