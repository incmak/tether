// index.html starts the first session list and chat requests before the app script downloads, so on a slow
// network the server works on them while the 500 KB script is still on the wire (B24).
interface BootAnswer { data: unknown; tag: string | null }
interface Boot { sessions?: Promise<BootAnswer | null>; chat?: { query: string; answer: Promise<BootAnswer | null> } }
declare global { interface Window { __tetherBoot?: Boot } }

/** The first caller always gets the early answer. A caller that comes while it is still on the wire shares it: an
 *  early refresh() aborts the first poll, and its replacement must not start a second request. After it arrives,
 *  later callers get null and fetch a new one. */
function shared(answer: Promise<BootAnswer | null> | undefined) {
  if (!answer) return () => null
  let taken = false, settled = false
  void answer.finally(() => { settled = true })
  return () => {
    if (taken && settled) return null
    taken = true
    return answer
  }
}

const boot = typeof window === 'undefined' ? undefined : window.__tetherBoot
const sessions = shared(boot?.sessions)
const chat = shared(boot?.chat?.answer)

/** The early session list, or null when index.html did not start it or it already arrived and was used. */
export function takeBootSessions(): Promise<BootAnswer | null> | null {
  return sessions()
}

/** The early chat answer for exactly this query (pane, limit, preview, mode), else null. */
export function takeBootChat(query: string): Promise<BootAnswer | null> | null {
  return boot?.chat?.query === query ? chat() : null
}
