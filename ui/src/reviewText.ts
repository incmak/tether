// The review messages that Tether types into a tab (server/review_links.py), parsed for the review card in chat.
export type Review = {
  feedback: boolean; tab: string; agent?: string; name?: string; terms?: TermPart[]; commit?: string
  reply: string; transcript?: string; last?: boolean
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
const request = (name: string, after: string) => new RegExp(`^\\[Tether review\\] The (\\S+) tab “(${name})” \\((%\\d+)\\) `
  + `finished a turn\\. ${after}Its last reply:\\n\\n([\\s\\S]*)\\n\\nTranscript: ([^\\n]*?)`
  + '( This is the last round: your reply stays here\\.)?\\s*$')
// The current request has the full intro and a review id (secrets.token_hex(4)) before the terms. An older one has neither.
const CURRENT = `${literal(INTRO)} Review id: [0-9a-f]{8}\\. ([\\s\\S]*?)`
const OLDER = 'Review its work: [\\s\\S]*?()'
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

export function parseReview(text: string): Review | null {
  for (const [shortest, longest] of REQUESTS) {
    const request = shortest.exec(text)
    if (!request) continue
    if (longest.exec(text)?.[2] !== request[2]) return null
    const { commit, parts } = termParts(request[4].trim())
    return {
      feedback: false, agent: request[1], name: request[2], tab: request[3], terms: parts, commit,
      reply: request[5].trim(), transcript: request[6], last: !!request[7],
    }
  }
  const feedback = REVIEW_FEEDBACK.exec(text)
  return feedback ? { feedback: true, tab: feedback[1], reply: feedback[2] } : null
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
