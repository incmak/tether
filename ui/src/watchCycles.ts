import type { ChatMessage } from './api'

// Two kinds of runs fold into one line: a Monitor watch that expired and was re-armed ('watch'), and a turn a session
// cron job started ('schedule'). A watch run can only re-arm, so its fold may say "no events". A scheduled run can act
// (post, write, deploy) with any tool, so its fold never claims it was quiet: the line shows the run's own last words,
// and runs merge only when those words are the same.
type QuietKind = 'watch' | 'schedule'
const quietKind = (message: ChatMessage): QuietKind | null => message.kind === 'notice' && (message.quiet === 'watch' || message.quiet === 'schedule') ? message.quiet : null
const toolsDone = (message: ChatMessage, only?: string) => message.kind === 'actions'
  && !!message.tools?.length
  && message.tools.every(tool => (only === undefined || tool.name === only) && tool.status === 'complete' && !tool.is_error)
const isReply = (message: ChatMessage) => message.role === 'assistant'
  && !message.kind
  && message.text.trim().length <= 400
  && !/needs you:\s*yes/i.test(message.text)
  && !message.text.includes('?')
// A run's outcome as one line: its last reply without the "Needs you: no" footer.
const outcome = (members: ChatMessage[]) => {
  const last = [...members].reverse().find(message => !message.kind && message.role === 'assistant')
  return (last?.text ?? '').split('\n').map(line => line.trim()).filter(line => line && !/^needs you:/i.test(line)).join(' ')
}

// One scheduled run from its notice at `start`: the notice, then finished tools and short replies.
function scheduledRun(messages: ChatMessage[], start: number) {
  let end = start + 1
  while (end < messages.length && (toolsDone(messages[end]) || isReply(messages[end]))) end++
  const members = messages.slice(start, end)
  return { members, end, result: outcome(members) }
}

export function foldWatchCycles(messages: ChatMessage[]): ChatMessage[] {
  const firstQuiet = messages.findIndex(message => quietKind(message) !== null)
  if (firstQuiet === -1) return messages

  const result = messages.slice(0, firstQuiet)
  let index = firstQuiet
  while (index < messages.length) {
    const first = messages[index]
    const kind = quietKind(first)
    if (kind === null) {
      result.push(first)
      index++
      continue
    }

    if (kind === 'schedule') {
      index = foldSchedule(messages, index, result)
      continue
    }

    const members: ChatMessage[] = []
    const watches = new Set<string>()
    let cycles = 0
    let rearmed = false
    let afterReply = false
    while (index < messages.length) {
      const message = messages[index]
      if (quietKind(message) === kind) {
        cycles++
        if (message.watch !== undefined) watches.add(message.watch)
        afterReply = false
      } else if (kind === 'watch' && !afterReply && toolsDone(message, 'Monitor')) {
        rearmed = true
      } else if (kind === 'watch' && !afterReply && isReply(message)) {
        afterReply = true
      } else {
        break
      }
      members.push(message)
      index++
    }

    result.push({
      id: 'watch:' + first.id,
      role: 'assistant',
      kind: 'watch',
      text: '',
      time: members[members.length - 1].time,
      members,
      watches: [...watches],
      cycles,
      rearmed,
    })
  }
  return result
}

// A job is its cron id: two jobs can share a label (label() shortens the prompt).
const jobOf = (message: ChatMessage) => message.cron ?? message.watch

// Fold scheduled runs of one job that ended with the same words; return the index after them.
function foldSchedule(messages: ChatMessage[], start: number, result: ChatMessage[]) {
  const first = messages[start]
  let run = scheduledRun(messages, start)
  const members = [...run.members]
  const result0 = run.result
  let cycles = 1
  let index = run.end
  while (index < messages.length && quietKind(messages[index]) === 'schedule' && jobOf(messages[index]) === jobOf(first)) {
    run = scheduledRun(messages, index)
    if (run.result !== result0) break
    members.push(...run.members)
    cycles++
    index = run.end
  }
  // The run went on with something the reader must see (a long report, a question): it shows below the line.
  const next = messages[index]
  const reported = next !== undefined && next.role === 'assistant' && quietKind(next) === null
  if (members.length === 1) {
    result.push(first)
    return index
  }
  result.push({
    id: 'watch:' + first.id,
    role: 'assistant',
    kind: 'watch',
    text: '',
    time: members[members.length - 1].time,
    members,
    watches: first.watch === undefined ? [] : [first.watch],
    cycles,
    rearmed: false,
    schedule: true,
    reported,
    result: result0,
  })
  return index
}
