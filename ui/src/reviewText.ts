// The review messages that Tether types into a tab (server/review_links.py), parsed for the review card in chat.
export type Review = {
  kind: 'request' | 'feedback' | 'approved' | 'stopped' | 'summary' | 'notice' | 'correction'
  feedback: boolean; tab?: string; agent?: string; name?: string; terms?: TermPart[]; commit?: string
  reply: string; transcript?: string; last?: boolean; uncommitted?: boolean; rounds?: number
}
/** Plain text, or the server's own REVIEW.md command (shown as code). Other text keeps its backticks: it holds paths and checklist rows. */
export type TermPart = { text: string; code?: boolean }

const INTRO = 'Review its work: check the changes and the claims, then list what is wrong or missing. '
  + 'Start your reply with APPROVED if nothing is. Tether sends your reply to that tab.'
const literal = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
// The request (server/review_links.py, "[Tether review] The"). The name, the terms, the reply and the transcript path are
// free text; each ends at the fixed text that the server puts after it. No field before the reply holds
// "Its last reply:" (the server escapes it), and the reply runs to the last "\n\nTranscript: ".
const NAME = '(?:(?!Its last reply:\\n\\n)[\\s\\S])*'
const request = (name: string, after: string) => new RegExp(`^\\[Tether review\\] The (Claude|Codex|claude|codex) tab “(${name})” \\((%\\d+)\\) `
  + `finished a turn\\. ${after}Its last reply:\\n\\n([\\s\\S]*)\\n\\nTranscript: ([^\\n]*?)`
  + '( This is the last round: your reply stays here\\.)?\\s*$')
// Historical requests lack a review id. The shortest historical form also omits the full intro.
const CURRENT = `${literal(INTRO)} Review id: [0-9a-f]{8}\\. ([\\s\\S]*?)`
const OLDER = `(?:${literal(INTRO)} (?!Review id:)[\\s\\S]*?|Review its work: )()`
// A tab name can hold the text that follows it. The shortest and the longest name must then agree, or the card refuses.
const REQUESTS = [CURRENT, OLDER].map(after => [request(`${NAME}?`, after), request(NAME, after)])
const REVIEW_FEEDBACK = /^\[Tether review\] Feedback from the reviewer tab \((%\d+)\):\s*([\s\S]*?)\s*Fix what it raises, then stop and wait\.\s*$/
// The bound header, as the server builds it: the commit, its folder (shlex.quote, so one exact token), the file status,
// the optional rules command for that same folder, then the reply format for that same commit.
const FOLDER = `'[^']*'(?:"'"'[^']*')*|[\\w@%+=:,./-]+`
const HEADER = new RegExp(`^(Commit under review: ([0-9a-f]{40}|[0-9a-f]{64}) in (${FOLDER})`
  + '(?: plus uncommitted changes| \\(Tether could not read the status of its files\\))?\\. )'
  + '(?:Read the review rules first: `(git -C \\3 show origin/main:REVIEW\\.md)`\\. )?'
  + 'An approval counts only for this commit: start it with `APPROVED \\2` \\(the full hash\\)\\.(?: |$)')

const FULL_HASH = '[0-9a-f]{40}|[0-9a-f]{64}'
const HASH = `[0-9a-f]{12}|${FULL_HASH}`
const APPROVED = new RegExp(String.raw`^\[Tether review\] Approved by the reviewer tab \((%[0-9]+)\)`
  + String.raw`(?: for commit (${HASH})( and its uncommitted changes)?)?:\n\n`)
// review_links approves a reply when raw.strip().upper().startswith('APPROVED').
const APPROVED_REPLY = /^APPROVED/i
const LEGACY_NAMED_COMMIT = /^APPROVED[\s:,.(`*\u2013\u2014-]*(?:for\s+)?(?:commit\s+)?[`*]*([0-9a-f]{7,64})\b/i
// Match Python's strip and NAMED in review_links.py, including Unicode whitespace and word boundaries.
const SPACE = String.raw`\p{White_Space}\x1c-\x1f`
const NAMED_COMMIT = new RegExp(String.raw`^[${SPACE}]*APPROVED[${SPACE}:,.(\x60*\u2013\u2014-]*`
  + String.raw`(?:for[${SPACE}]+)?(?:comm[iİı]t[${SPACE}]+)?[\x60*]*([0-9a-f]{7,64})(?![\p{L}\p{N}_])`, 'iu')
const APPROVED_MARK = '\n\nThe review is complete. '
const APPROVED_END = 'the next step your instructions give for an approved review, then stop.'
const APPROVED_DIRTY = 'It covered uncommitted changes, so no commit is approved yet: commit the work '
  + 'in this turn, and Tether asks the reviewer to confirm that commit. '
const APPROVED_CLEAN = new RegExp(`^The approval is for commit (${FULL_HASH}) only\\. Before you push, deploy or merge, `
  + `check that HEAD (?:${literal("in this tab's worktree (`git rev-parse HEAD`)")}`
  + `|in (${FOLDER}) \\(\`git -C \\2 rev-parse HEAD\`\\)) is \\1\\. `
  + literal('If HEAD is different or you cannot read it, do not act on this approval. '
    + `Stop and wait for the next review. If HEAD is that hash, do ${APPROVED_END}`) + '$')

const FINDINGS = '\n\nOpen findings:\n\n'
const STOPPED = new RegExp(String.raw`^\[Tether review\] Stopped after ([1-9][0-9]*) review rounds: the reviewer tab \((%[0-9]+)\)`
  + literal(` still finds blocking problems. Do not fix them in this turn. Mak decides the next step.${FINDINGS}`)
  + String.raw`([\s\S]*)\n\n`
  + literal('Reply with a short list of the open findings and what you tried, then end with "Needs you: yes".') + '$')
// The parent summary and notice always use review_links.MAX_ROUNDS. The worker's STOPPED count varies.
const PARENT_ROUNDS = 3
const SUMMARY = new RegExp(String.raw`^\[Tether review\] ${PARENT_ROUNDS} review rounds still found blocking problems\. `
  + literal(`Your parent tab decides the next step. Do not fix them in this turn.${FINDINGS}`)
  + String.raw`([\s\S]*)\n\n`
  + literal('Reply with the open findings and what you tried. End with "Needs you: no" unless '
    + 'a finding needs Mak (taste, new authority, or you disagree with the reviewer).') + '$')
const noticePattern = (name: string) => new RegExp(String.raw`^\[Tether review\] “(${name})” \((%[0-9]+)\) `
  + String.raw`still has blocking review findings after ([1-9][0-9]*) rounds\. Open findings: “([^\r\n]*)”\n\n`
  + String.raw`To allow 2 more rounds, send it the next step: python3 ([^\r\n]*?/agent_tab_cli\.py) send --pane \2 "<what to fix>"\. `
  + String.raw`To hand it to Mak: python3 \5 escalate --pane \2\.$`)
const NOTICE = noticePattern(String.raw`[^\r\n]*?`)
const NOTICE_LONG_NAME = noticePattern(String.raw`[^\r\n]*`)
const CORRECTION = new RegExp(`^\\[Tether review\\] Correction: the approval above is for commit (${FULL_HASH}) only\\. `
  + `(?:This tab is now at (${FULL_HASH})\\. `
  + `|${literal("Tether could not read this tab's commit after the approval, so the approval cannot be checked. ")})`
  + literal('Do not push, deploy or merge on that approval. ')
  + `(?:${['The review is paused: Mak decides.', 'Tether asked the reviewer to review the current commit.',
    'Wait for the next review.'].map(literal).join('|')})$`)

export function parseReview(text: string): Review | null {
  for (const [shortest, longest] of REQUESTS) {
    const request = shortest.exec(text)
    if (!request) continue
    if (longest.exec(text)?.[2] !== request[2]) return null
    const { commit, parts } = termParts(request[4].trim())
    return {
      kind: 'request', feedback: false, agent: request[1], name: request[2], tab: request[3], terms: parts, commit,
      reply: request[5].trim(), transcript: request[6], last: !!request[7],
    }
  }
  const feedback = REVIEW_FEEDBACK.exec(text)
  if (feedback) return { kind: 'feedback', feedback: true, tab: feedback[1], reply: feedback[2] }

  const message = text.trimEnd()
  const approved = APPROVED.exec(message)
  if (approved) {
    const [, tab, commit, dirty] = approved
    const end = message.lastIndexOf(APPROVED_MARK)
    if (end < approved[0].length) return null
    const footer = message.slice(end + APPROVED_MARK.length)
    const rawReply = message.slice(approved[0].length, end)
    const reply = rawReply.trim()
    // Older relays used a 12-digit hash with the same footer as an unbound approval.
    const legacy = commit?.length === 12
    if (commit && !legacy) {
      if (NAMED_COMMIT.exec(rawReply)?.[1].toLowerCase() !== commit) return null
    } else {
      if (!APPROVED_REPLY.test(reply) || legacy && dirty) return null
      const named = LEGACY_NAMED_COMMIT.exec(reply)?.[1].toLowerCase()
      if (legacy && named && !named.startsWith(commit)) return null
    }
    const matches = commit && !legacy && !dirty ? APPROVED_CLEAN.exec(footer)?.[1] === commit
      : footer === `${dirty ? APPROVED_DIRTY : ''}Do ${APPROVED_END}`
    if (!matches) return null
    return { kind: 'approved', feedback: true, tab, commit, ...(dirty ? { uncommitted: true } : {}), reply }
  }
  const stopped = STOPPED.exec(message)
  if (stopped && Number.isSafeInteger(Number(stopped[1]))) {
    return { kind: 'stopped', feedback: true, rounds: Number(stopped[1]), tab: stopped[2], reply: stopped[3] }
  }
  const summary = SUMMARY.exec(message)
  if (summary) {
    return { kind: 'summary', feedback: true, rounds: PARENT_ROUNDS, reply: summary[1] }
  }
  const notice = NOTICE.exec(message)
  if (notice && notice[3] === String(PARENT_ROUNDS)) {
    // Both the name and the findings preview can contain the quoted separator.
    if (NOTICE_LONG_NAME.exec(message)?.[1] !== notice[1]) return null
    return { kind: 'notice', feedback: true, name: notice[1], tab: notice[2], rounds: PARENT_ROUNDS, reply: notice[4] }
  }
  const correction = CORRECTION.exec(message)
  if (correction && correction[1] !== correction[2]) {
    return { kind: 'correction', feedback: true, commit: correction[1], reply: message.slice('[Tether review] '.length) }
  }
  return null
}

/** The commit and the terms to show. The card shows the reply format on its own line, so the terms leave out that
 * sentence. Terms with no bound header show as sent. */
function termParts(terms: string): { commit?: string; parts?: TermPart[] } {
  if (!terms) return {}
  const header = HEADER.exec(terms)
  if (!header) return { parts: [{ text: terms }] }
  const rest = terms.slice(header[0].length)
  return {
    commit: header[2],
    parts: header[4]
      ? [{ text: `${header[1]}Read the review rules first: ` }, { text: header[4], code: true }, { text: `.${rest ? ' ' : ''}${rest}` }]
      : [{ text: `${header[1]}${rest}`.trim() }],
  }
}
