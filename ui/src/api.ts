export type Agent = 'claude' | 'codex' | 'antigravity' | 'shell'
/** Omit browser for the existing test browser. Approved signed tabs use mode: 'agent'. */
export type BrowserId = 'test' | 'signed' | 'chrome'
export type SignedBrowserSource = 'chrome' | 'server'
export interface BrowserSelection { browser?: BrowserId }
export interface BrowserAvailability { id: BrowserId; label: string; running: boolean; available: boolean; reason: string }
export interface BrowserTab {
  id: string; url: string; title: string; preset: string; mode: 'private' | 'saved' | 'agent'
  width: number; height: number; touch: boolean
  owner?: { pane: string | null }; controller?: 'user' | 'agent'
  grant?: { id: string; expires: number }
}
export interface SignedBrowserRequest {
  browser: SignedBrowserSource
  id: string; pane: string; agent: string; reason: string; minutes: number; url: string | null; created: number
}
export interface SignedBrowserGrant {
  browser: SignedBrowserSource
  id: string; pane: string; agent: string; tab: string; expires: number; reason: string
}
export interface SignedBrowserEnded {
  id: string; pane: string; agent: string; browser: SignedBrowserSource; reason: string; message: string; at: number
}
/** GET signed/access and POST signed/allow, signed/deny, signed/end, signed/dismiss. No CDP token is exposed to the panel. */
export interface SignedBrowserAccess {
  browser: 'signed'; requests: SignedBrowserRequest[]; grants: SignedBrowserGrant[]; ended: SignedBrowserEnded[]
}
/** POST signed/dismiss {id} removes an ended row and returns SignedBrowserAccess. */
export interface SignedBrowserDismiss { id: string }
export interface SignedBrowserDecision { id: string; minutes?: number }
export type SignedBrowserAskResult = { ok: true; browser: SignedBrowserSource; cdp: string; tab: string; expires: number }
  | { ok: false; reason: 'denied' | 'timeout' | 'request failed' }
/** POST extension/pair {}, authenticated with the normal Tether cookie, Origin and write header. */
export interface BrowserExtensionPair { code: string; expires: number }
/** POST extension/exchange, from the extension Origin. Consumes the code once. */
export interface BrowserExtensionExchange { code: string; name: string }
export interface BrowserExtensionCredential { id: string; token: string }
/** GET extension/devices returns BrowserExtensionDevice[]. No credential hashes or tokens. */
export interface BrowserExtensionDevice {
  id: string; name: string; connected: boolean; lastSeen: number; chrome: string
}
/** POST extension/revoke returns the remaining devices, after disconnect and grant revocation. */
export interface BrowserExtensionRevoke { id: string }
export interface BrowserExtensionDevices { devices: BrowserExtensionDevice[] }
export interface SignedBrowserAsk {
  text: string; pane?: string; minutes?: number; url?: string; browser?: 'auto' | SignedBrowserSource
}
/** WSS extension/connect. Authenticate in the first frame; credentials never enter the URL.
 * Panel timestamps use Unix seconds. The extension's grant expires value uses Unix milliseconds. */
export type BrowserExtensionMessage =
  | { type: 'hello'; token: string; chrome: string }
  | { type: 'ready' | 'ping' | 'pong'; epoch: string }
  | { type: 'grant'; epoch: string; id: string; reason: string; minutes: number; url: string | null }
  | { type: 'granted'; epoch: string; id: string; tab: number; target: string; expires: number }
  | { type: 'command'; epoch: string; id: string; grant: string; tab: number; method: string; params: Record<string, unknown> }
  | { type: 'result'; epoch: string; id: string; result: Record<string, unknown> }
  | { type: 'error'; epoch: string; id: string | null; code: string; message: string }
  | { type: 'end'; epoch: string; id: string }
  | { type: 'ended'; epoch: string; id: string; tab?: number; reason: string }
  | { type: 'event'; epoch: string; grant: string; tab: number; method: string; params: Record<string, unknown> }
  | { type: 'tab'; epoch: string; grant: string; tab: number; url: string; title: string }
export interface BrowserTabs extends BrowserSelection {
  running: boolean; version: number; tabs: BrowserTab[]; browsers: BrowserAvailability[]
  presets: { id: string; label: string; width: number; height: number; touch: boolean }[]
  nav?: { back: boolean; forward: boolean }
  requests?: SignedBrowserRequest[]; grants?: SignedBrowserGrant[]; ended?: SignedBrowserEnded[]
}
/** SSE tabs events carry the selected browser's state, without the browser switch list. */
export type BrowserStreamTabs = Omit<BrowserTabs, 'browsers'>
export interface BrowserFrame extends BrowserSelection { seq: number; width: number; height: number; top: number; jpeg: string }
export interface Pane {
  id: string
  tmux_session: string
  label: string
  agent: Agent
  dir: string
  state: string
  writable: boolean
  thread: string | null
  approval: unknown
  pane_name?: string
  tmux_pane_name?: string  // the raw tmux name (@chat_name); pane_name is empty when a newer /rename wins
  group?: string  // the group id (tmux option @group, server/groups.py); empty or unknown = Unstacked
  worktree?: string  // the branch, when Tether started this tab in a new worktree (tmux option @worktree)
  role?: 'advisor'  // a helper tab that answers another tab's questions (server delegate.ROLES)
  pane_icon?: string  // tmux option @chat_icon: a lucide icon name, or svg-<name> for an icon Claude drew (Sidebar.tsx AgentIcon)
  agent_name?: string  // the Claude or Codex session name (/rename); shown when the pane has no Tether name
  codex_profile?: string | null  // a Codex pane started with `codex -p foundry`: 'foundry'. null = the ChatGPT plan
  since?: number | null
  finished?: number | null
  tasks?: string[]  // background work from Claude's footer: "1 shell", "1 monitor"
  crons?: PaneCron[]  // Claude's session cron jobs (CronCreate), from its last Stop hook (server/claude_hooks.crons)
  attention?: Attention | null
  needs?: Needs | null  // a one-line summary while the pane waits on you (the desktop "Needs you" list)
  ask?: Ask | null  // Attention mode: the ask that waits on you until you answer it (server/attention.py)
  reviewers?: string[]
  reviewers_paused?: string[]  // the panes that review each finished turn of this one (server/review_links.py)
  parent?: string  // the tab that started this one, from @tether_helper (server/helper_tabs.py)
  opened_by?: { pane: string; name: string | null; live: boolean }  // the tab that ran `start` for this one (B400)
  owner?: string  // the live tab that owns this tab's work; the tab nests under it (B400)
  helper?: boolean  // an agent opened it for its own work; "Hide agent helper tabs" may hide it (helperTabs.ts)
  task?: string
  task_role?: string
  task_depth?: 1 | 2
  answer_digest?: string
  from?: { pane: string; name: string; live: boolean }
  done?: true
  done_mark?: true
  moved_by?: 'user'
  escalated?: boolean  // its owner sent a paused review to you (agent_tab_cli.py escalate)
  brief?: TabBrief | null  // the AI name and description of this conversation (server/tab_brief.py)
}
/** A session cron job. every: the schedule in words; next: the next run in Unix seconds, approximate (Claude Code adds jitter). */
export interface PaneCron { id: string; label: string; every: string; next?: number }
export interface FooterCron extends PaneCron { schedule: string; recurring: boolean }
// A clock time for a Unix second: "14:05" today, "Wed 09:00" within a week, else the date.
export const cronTime = (seconds: number, now = Date.now()) => {
  const date = new Date(seconds * 1000), today = new Date(now)
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (date.toDateString() === today.toDateString()) return time
  if (seconds * 1000 - now < 6 * 86400000) return `${date.toLocaleDateString([], { weekday: 'short' })} ${time}`
  return `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${time}`
}
export const cronsLabel = (crons: { label: string }[]) => crons.length === 1 ? crons[0].label : `${crons.length} scheduled jobs`
// One line per job for a tooltip or a screen reader: name, schedule, next run.
export const cronSummary = (crons: PaneCron[]) => crons.map(job => [job.label, job.every, job.next != null ? `next about ${cronTime(job.next)}` : ''].filter(Boolean).join(' · ')).join('; ')
/** The last run of each cron job in this conversation: when it fired, the notice id, and the agent's last words in that turn. */
export interface ScheduledRun { at: string; message: string; result: string }
/** state: running while the model writes; off when no model may be used; skipped when the chat was too long when first seen. at: Unix seconds. */
export interface TabBrief { name?: string; description?: string; description_by?: 'ai' | 'user'; state?: 'running' | 'done' | 'failed' | 'off' | 'skipped'; error?: string; at?: number }
/** A backlog item that names this tab or chat (server/backlog.py linked). */
export interface TabBriefLink { id: string; title: string; state: 'doing' | 'added' | 'done'; open: boolean }
export interface Needs { kind: 'approval' | 'plan' | 'question'; text: string }
// since and snoozed_until are Unix seconds. next is the reply's "Next:" line or the prompt's question.
export interface Ask { pane: string; thread: string | null; since: number; next: string; kind: 'reply' | 'approval' | 'plan' | 'question'; opened: boolean; snoozed_until: number | null; urgent?: boolean }
// images: how many images the tool result holds (Claude's Read of a screenshot). GET tool-image serves them.
// artifact: a page Claude published with its Artifact tool. url: the claude.ai link; path: the local HTML file it came from.
export interface ToolArtifact { url: string; path: string; title: string }
export interface ToolSummary { id: string; name: string; title: string; is_error: boolean; status: 'pending' | 'complete'; images?: number; description?: string; started?: string; ended?: string; artifact?: ToolArtifact }
export interface ToolDetail extends ToolSummary { detail: string; format: 'text' | 'diff'; truncated: boolean }
export interface AdvisorCall { id: string; model?: string; status: 'pending' | 'answered' | 'error' | 'unknown'; error?: string; result_type?: string; started?: string; ended?: string }
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  kind?: 'actions' | 'advisor' | 'terminal' | 'notice' | 'peer' | 'watch' | 'side'
  quiet?: string
  watch?: string
  members?: ChatMessage[]
  watches?: string[]
  cycles?: number
  rearmed?: boolean
  // A folded run of session cron turns (watchCycles.ts); reported: the last turn went on to show more after the fold.
  schedule?: boolean
  reported?: boolean
  result?: string
  // The session cron job that started this turn ('Scheduled run' notice).
  cron?: string
  title?: string
  tools?: ToolSummary[]
  advisor?: AdvisorCall
  count?: string
  time: string
  images?: number  // Claude: images inside this user message. GET message-image serves them by image_key.
  image_key?: string
}
// Chat data comes from the server and is not trusted. A message needs a role and text to render or to match a send.
// Its optional fields must have their declared types, or the readers that walk them (tools, members) would throw.
const MESSAGE_FIELDS: Record<string, 'string' | 'number' | 'boolean'> = {
  id: 'string', kind: 'string', quiet: 'string', watch: 'string', result: 'string', cron: 'string', title: 'string',
  count: 'string', time: 'string', image_key: 'string', cycles: 'number', images: 'number', schedule: 'boolean',
  rearmed: 'boolean', reported: 'boolean',
}
const TOOL_FIELDS: Record<string, 'string' | 'number' | 'boolean'> = {
  is_error: 'boolean', status: 'string', images: 'number', description: 'string', started: 'string', ended: 'string',
}
const isObject = (item: unknown): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item)
const hasStrings = (item: Record<string, unknown>, keys: string[]) => keys.every(key => typeof item[key] === 'string')
const isArtifact = (item: unknown) => isObject(item) && hasStrings(item, ['url', 'path', 'title'])
/** Each entry cleaned; entries that clean to null drop out. The same array when every entry came back as itself.
 *  A rejected entry (null in, null out) counts as a change, and so does a hole in a sparse array. */
function cleanEach<T>(list: unknown[], clean: (item: unknown) => T | null): T[] {
  const out: T[] = []
  let same = true
  for (let index = 0; index < list.length; index++) {
    const entry = index in list ? clean(list[index]) : null
    if (entry === null || entry !== list[index]) same = false
    if (entry !== null) out.push(entry)
  }
  return same ? list as T[] : out
}
/** The tool itself when valid, a copy without its wrong-type fields, or null when it has no id, name and title. */
const cleanTool = (item: unknown): ToolSummary | null => {
  if (!isObject(item) || !hasStrings(item, ['id', 'name', 'title'])) return null
  const bad = Object.entries(TOOL_FIELDS).filter(([key, type]) => item[key] !== undefined && typeof item[key] !== type).map(([key]) => key)
  if (item.artifact !== undefined && !isArtifact(item.artifact)) bad.push('artifact')
  if (!bad.length) return item as unknown as ToolSummary
  const rest = { ...item }
  for (const key of bad) delete rest[key]
  return rest as unknown as ToolSummary
}
const cleanAdvisor = (item: unknown) => {
  if (!isObject(item) || typeof item.id !== 'string' || !item.id
    || typeof item.status !== 'string' || !['pending', 'answered', 'error', 'unknown'].includes(item.status)) return undefined
  const bad = ['model', 'error', 'result_type', 'started', 'ended'].filter(key => item[key] !== undefined && typeof item[key] !== 'string')
  if (!bad.length) return item
  const rest = { ...item }
  for (const key of bad) delete rest[key]
  return rest
}
const isStringList = (item: unknown) => Array.isArray(item) && item.every(entry => typeof entry === 'string')
/** The message itself when every field it has is the right type; a copy without the wrong fields when it has a role and
 *  text; else null. Bad tools drop out of the tool list, and bad members out of the member list. */
const cleanMessage = (item: unknown): ChatMessage | null => {
  if (!isObject(item) || typeof item.role !== 'string' || typeof item.text !== 'string') return null
  let copy: Record<string, unknown> | null = null
  const set = (key: string, value: unknown) => { copy ??= { ...item }; if (value === undefined) delete copy[key]; else copy[key] = value }
  for (const [key, type] of Object.entries(MESSAGE_FIELDS)) if (item[key] !== undefined && typeof item[key] !== type) set(key, undefined)
  const advisor = cleanAdvisor(item.advisor)
  if (advisor !== item.advisor) set('advisor', advisor)
  if (item.tools !== undefined) {
    if (!Array.isArray(item.tools)) set('tools', undefined)
    else {
      const tools = cleanEach(item.tools, cleanTool)
      if (tools !== item.tools) set('tools', tools)
    }
  }
  if (item.members !== undefined) {
    const members = Array.isArray(item.members) ? cleanList(item.members) : undefined
    if (members !== item.members) set('members', members)
  }
  if (item.watches !== undefined && !isStringList(item.watches)) set('watches', undefined)
  return (copy ?? item) as unknown as ChatMessage
}
/** A list of clean messages. A list that is already clean comes back as the same array. */
const cleanList = (list: unknown[]): ChatMessage[] => cleanEach(list, cleanMessage)
/** The transcript of a chat answer, without entries that are not messages. An error body or an old server can send no
 *  list or another type; read that as empty. A valid list comes back as the same array, so memos keep it. */
export const chatMessages = (data: { messages?: unknown } | null | undefined): ChatMessage[] => {
  const list: unknown = data?.messages
  return Array.isArray(list) ? cleanList(list) : []
}
/** The same chat data with a clean transcript. Valid data comes back as the same object. */
export const cleanChat = <T extends { messages?: unknown }>(data: T | undefined): T | undefined => {
  if (!data) return data
  const messages = chatMessages(data)
  return messages === data.messages ? data : { ...data, messages }
}
export interface ChatData {
  /** A hash of this payload. The chat stream takes it as `have` and skips sending the same payload again (B63). */
  version?: string
  login_result?: { phase: 'done' | 'failed' | 'unknown'; message: string }
  preview?: { text: string }
  // A `!` shell command that is still running in Claude Code, read from the screen (the transcript gets it only at the end).
  bang?: { command: string; output: string; seconds: number | null }
  messages: ChatMessage[]
  busy: boolean
  thread: string | null
  agent: Agent
  writable: boolean
  session: string
  earlier: number
  limit: number
  state: string
  approval: unknown
  plan?: { id: string; text: string } | null
  claude_name?: string | null  // Claude's own session name (/rename); null when it has none
  started?: string | null  // ISO time of the thread's first record; null when unknown
  attention?: Attention | null
  footer?: Footer
  scheduled?: Record<string, ScheduledRun>
}
// free: Claude's own-answer row ("Type something."). The answer goes in the message box, not a tap.
export interface Choice { id: number; text: string; detail?: string; selected?: boolean; free?: boolean }
export interface Approval { id: string | null; text: string; actionable: boolean; options?: Choice[] }
/** Claude's native question as the screen shows it now. Present only when the whole question is visible. checked = answer mark, selected = cursor. */
export interface AskCurrent { index: number; total: number; header: string; question: string; multi: boolean; options: { id: number; label: string; text: string; description: string; selected: boolean; checked?: boolean; kind: 'option' | 'free' | 'chat' }[]; tabs: { label: string; active: boolean; answered: boolean }[]; keys: string[] }
export interface AskQuestion { question: string; header: string; multi: boolean; options: { label: string; description: string }[] }
/** Original hook questions use Claude's multiSelect field, unlike transcript AskQuestion. */
export interface AskBridgeQuestion { question: string; header: string; multiSelect?: boolean; options: { label: string; description?: string; preview?: string }[] }
// expires: unix seconds, the current deadline. Each ask-engage call moves it to now + idle (capped by the server).
export interface AskBridge { id: string; nonce: string; questions: AskBridgeQuestion[]; expires: number; idle?: number }
/** null hands the question back to the native terminal. A retry must reuse the exact request. */
export interface AskAnswerRequest { session: string; id: string; nonce: string; answers: Record<string, string | string[]> | null; requestId: string }
export interface AskAnswerResult { status: 'submitted' | 'handoff' }
/** ask: Claude's open question from the transcript, when the pane is too short to show all of it. */
export interface PickerItem { text: string; selected: boolean }
/** A key that a read-only panel's own footer offers. The server keeps the real key; the browser sends only the id. */
export interface PromptAction { id: string; label: string; key: string; enabled: boolean; disabledReason?: string }
/** Visible read state only. IDs are frame-local; focus indexes items. -1 means an unknown overflow count. */
export interface ChoiceState {
  selection: 'single' | 'multi'
  items: { id: number; text: string; detail?: string; selected: boolean; checked?: boolean; disabled?: boolean }[]
  focus: number | null
  filter: string | null
  tabs: { label: string; active: boolean }[] | null
  more_above: number
  more_below: number
  hints: string[]
}
// Claude's /permissions dialog as server/permissions_dialog.py reads it.
export interface PermissionsDialog { tabs: string[]; tab: string | null; lines: string[]; selected: number | null; hint: string; empty: boolean }
/** A Claude slash-command dialog read from the screen (server/claude_dialog.py). `hints` are the keys its footer offers. */
export interface DialogItem { text: string; value: string; mark: string; detail: string; selected: boolean }
export type DialogBlock = { type: 'text'; lines: string[]; dim: boolean; mono: boolean } | { type: 'heading'; text: string }
  | { type: 'facts'; rows: { label: string; value: string }[] } | { type: 'meter'; percent: number; text: string }
  | { type: 'tabs'; tabs: string[]; tab: string } | { type: 'scale'; options: string[]; value: string; ends: string[] }
  | { type: 'search'; placeholder: string; value: string; focused: boolean }
  | { type: 'heatmap'; months: [number, string][]; rows: { label: string; cells: string }[] } | { type: 'options'; options: string[]; value: string }
  | { type: 'shortcuts'; items: { keys: string; effect: string }[] } | { type: 'entries'; items: { title: string; lines: string[] }[] }
  | { type: 'list'; items: (DialogItem | { heading: string })[]; more_above: number; more_below: number }
export interface DialogHint { keys: { key: string; label: string }[]; effect: string; text: string; go?: boolean }
export interface ClaudeDialog { title: string; note?: string; tabs?: string[]; tab?: string; tab_focus?: boolean; blocks: DialogBlock[]; hints: DialogHint[]; focus: 'list' | 'search' | 'none'; mode: string; text: string }
export type DialogAction = { action: 'key'; key: string } | { action: 'row'; row: number; label: string; then?: 'Up' | 'Down' } | { action: 'tab'; tab: string } | { action: 'scale'; value: string } | { action: 'filter'; text: string } | { action: 'subtab'; tab: string } | { action: 'option'; value: string }
export interface Attention { id: string; text: string; reason: 'menu' | 'prompt' | 'no-input-box' | 'panel' | 'picker' | 'info' | 'permissions' | 'side' | 'dialog'; permissions?: PermissionsDialog; dialog?: ClaudeDialog;
  menu_verified?: false | string;
  /** reason 'side' (Claude's /btw): pending = no answer yet, markdown = text is the raw Markdown answer, fork = the fork key is offered. */
  pending?: boolean; markdown?: boolean; fork?: boolean;
  promptId?: string; revision?: string; requiresResponse?: boolean; body?: string; hints?: string[]; actions?: PromptAction[]; title?: string; subtitle?: string; items?: PickerItem[]; more_above?: number; more_below?: number; hint?: string; question?: string; options?: Choice[]; choice?: ChoiceState; ask?: { id: string; questions: AskQuestion[] }; askBridge?: AskBridge; ask_current?: AskCurrent; login?: { url: string; code: boolean; user_code?: string; message?: string } }
export type ChatAgent = 'claude' | 'codex'
export interface PastChat { id: string; agent: ChatAgent; title: string; dir: string | null; time: string; live: boolean; pane: string | null }
export type TemporaryMode = 'collapsed' | 'open' | 'hidden'
export interface HistoryDir { dir: string; count: number; temporary?: boolean }
export interface HiddenData { temporary: TemporaryMode; temporaryCount: number; dirs: { dir: string; count: number }[]; chats: { id: string; agent: ChatAgent; title: string; dir: string | null; time: string }[] }
export interface HistoryData { chats: PastChat[]; total: number; next: number | null; counts: { all: number; claude: number; codex: number }; dirs: HistoryDir[]; temporary?: TemporaryMode; hiddenCount?: number }
export interface UsageWindow { label: string; percent: number; resets_at: number | null }
export interface UsageReport { windows: UsageWindow[]; reported_at: string | number | null; source?: 'live' }
export interface SavedAccount { number: number; email: string | null; org?: string | null; plan?: string | null; active: boolean; disabled?: boolean; windows: { label: string; percent: number; resets_at: string | null }[] }
export interface SavedAccounts { available: boolean; claude: SavedAccount[]; codex: SavedAccount[] }
// The provider for new Codex panes. ready: Foundry's profile and key command look set up on the server; hint says what is missing.
type SpendTokens = Record<'input' | 'cached' | 'cache_write' | 'output' | 'requests', number>
type SpendWindow = { short: SpendTokens; long: SpendTokens }
export interface FoundrySpend {
  month: string; budget: number
  estimate: { tokens: SpendWindow; today_tokens: SpendWindow; sessions: number; usd: number | null; today_usd: number | null }
  bill: { state: 'ok'; usd: number; currency: string; checked: number } | { state: 'not-set-up' } | { state: 'error'; hint: string }
}
export interface CodexProvider { provider: 'plan' | 'foundry'; profile: string; model: string | null; ready: boolean; hint: string | null; everywhere?: boolean }
export interface UsageAccount { email: string | null; plan: string | null }
export interface UsageData { claude: UsageReport | null; codex: UsageReport | null; accounts?: { claude: UsageAccount | null; codex: UsageAccount | null }; live_errors?: { claude?: string; codex?: string } }
export interface AutoResumeData { failed: number; tabs: { thread: string; agent: string; name: string; folder: string; error: string; state: string }[] }
/** A group in a tmux session (session option @groups). `project` is its context bundle id, empty until made. */
export interface GroupInfo { id: string; name: string; project: string }
export interface SessionsData { sessions: Pane[]; default_thread: string | null; auto_resume?: AutoResumeData; groups?: Record<string, GroupInfo[]> }
export class ApiError extends Error {
  status: number
  draft: boolean  // the agent's own input box has text that blocks this action
  approval: boolean  // the action needs a passkey approval (approval.ts)
  constructor(status: number, message: string, draft = false, approval = false) { super(message); this.status = status; this.draft = draft; this.approval = approval }
}
/** timeout: milliseconds before the request gives up (10 s). A broadcast types into many panes one after another, so it waits longer.
 *  priority: 'low' for background polls the current view does not wait on, so they do not compete with chat on a slow link (B67). */
export type Api = <T>(path: string, body?: unknown, signal?: AbortSignal, method?: 'DELETE', timeout?: number, priority?: RequestPriority) => Promise<T>
export async function request<T>(path: string, body?: unknown, signal?: AbortSignal, method?: 'DELETE', timeout = 10000, priority?: RequestPriority): Promise<T> {
  const raw = typeof Blob !== 'undefined' && body instanceof Blob  // background uploads send the image bytes as is
  const init: RequestInit = {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': raw ? body.type || 'application/octet-stream' : 'application/json', 'X-Codex-Mobile': '1', 'X-Tether-Client': 'web' },
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout),
  }
  if (priority) init.priority = priority  // browsers without Fetch priority ignore it
  const response = await reach(`/mobile/api/${path}`, init)
  const data = await response.json().catch(error => {
    // Failed body reads must reject so callers do not treat them as empty data.
    if (response.ok && error?.name !== 'SyntaxError') throw error
    if (response.ok && path.split('?')[0] === 'footer') throw error
    return {}
  })
  if (init.method !== 'GET') announceUndo(response)
  if (!response.ok) throw new ApiError(response.status, data.error || `Request failed (${response.status}). Please try again.`, data.draft === true, data.approval === 'required')
  return data as T
}

/** A step the server recorded in the undo history (undo_api.headers): the page offers Undo in a toast (Undo.tsx). */
export interface UndoStep { id: string; label: string }
export const UNDO_EVENT = 'tether-undo-step'
function announceUndo(response: Response) {
  const id = response.headers.get('X-Tether-Undo')
  if (!id || typeof window === 'undefined') return
  let label = ''
  try { label = decodeURIComponent(response.headers.get('X-Tether-Undo-Label') ?? '') } catch { /* a bad label: use the plain one */ }
  window.dispatchEvent(new CustomEvent<UndoStep>(UNDO_EVENT, { detail: { id, label: label || 'Last step' } }))
}
/** A GET that the server can answer with 304 Not Modified. It sends the ETag of the last answer and returns the last
 *  data again when nothing changed, so a poll of an unchanged list moves no body bytes. `last` holds the state per caller. */
export async function requestIfChanged<T>(path: string, last: { tag?: string; data?: T }, signal?: AbortSignal, timeout = 10000): Promise<T> {
  const response = await reach(`/mobile/api/${path}`, {
    credentials: 'same-origin',
    cache: 'no-store',  // the browser's own cache must not answer; the tag below is the only cache
    headers: last.tag && last.data !== undefined ? { 'If-None-Match': last.tag } : undefined,
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout),
  })
  if (response.status === 304 && last.data !== undefined) return last.data
  const data = await response.json().catch(error => {
    if (response.ok) throw error
    return {}
  })
  if (!response.ok) throw new ApiError(response.status, data.error || `Request failed (${response.status}). Please try again.`)
  last.tag = response.headers.get('ETag') ?? undefined
  last.data = data as T
  return data as T
}
/** tmux session names: spaces are fine; tmux turns . and : into _, so they are refused. Same rule as the server. */
export const SESSION_NAME = /^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,62}[A-Za-z0-9_-])?$/
export const SESSION_NAME_HELP = 'Use 1–64 letters, numbers, spaces, hyphens, or underscores. Start with a letter or number.'

/** A chat the agent can copy with its history: Claude or Codex, after its first message. */
export const canFork = (pane: Pane) => (pane.agent === 'claude' || pane.agent === 'codex') && !!pane.thread

/** fetch() rejected: the browser could not reach Tether (offline, server down, connection dropped). */
export class NetworkError extends Error {
  readonly cause: unknown
  constructor(cause: unknown) {
    super('Tether cannot reach the server. Check the connection.')
    this.name = 'NetworkError'
    this.cause = cause
  }
}

/** fetch(), but a rejection from fetch() itself (no answer at all) becomes a NetworkError.
 *  Timeouts and aborts keep their own names. Use it wherever a failure can reach errorText. */
export async function reach(input: RequestInfo | URL, init?: RequestInit) {
  try {
    return await fetch(input, init)
  } catch (error) {
    if (error instanceof TypeError) throw new NetworkError(error)
    throw error
  }
}

/** A failure in words a person can act on. The browser's own texts ("signal timed out", "Failed to fetch") never show.
 *  Only a real network failure (NetworkError, or the browser is offline) says "cannot reach".
 *  A built-in code error (TypeError, ReferenceError, …) is a bug in Tether: it says so and goes to the console.
 *  An Error that Tether throws on purpose keeps its own message. */
export function errorText(error: unknown) {
  const name = error instanceof Error || (typeof DOMException !== 'undefined' && error instanceof DOMException) ? error.name : ''
  if (name === 'TimeoutError') return 'Tether took too long to answer.'
  if (name === 'AbortError') return 'The request was stopped.'
  if (error instanceof NetworkError || (typeof navigator !== 'undefined' && navigator.onLine === false)) return 'Tether cannot reach the server. Check the connection.'
  const codeError = [TypeError, ReferenceError, RangeError, SyntaxError, EvalError, URIError].some(kind => error instanceof kind)
  if (!codeError && error instanceof Error && error.message) return error.message
  console.error('Tether app error:', error)
  return `Something went wrong in Tether.${error instanceof Error && error.message ? ` (${error.name}: ${error.message})` : ''}`
}
export interface CatalogItem { name: string; description: string; kind: 'builtin' | 'skill' | 'command'; aliases?: string[]; hint?: string }
export interface Catalog { commands: CatalogItem[]; plugins: { name: string; source: string; enabled: boolean }[]; mcp: { name: string; source: string }[]; note: string }

export interface FooterAgent { name: string; task: string; detail: string; main: boolean }
export interface FooterTask { id: string; type?: string; status?: string; description?: string; command?: string; started?: number }
export const usageLabel = (label: string) => ({ '5h': '5-hour', '168h': 'Weekly', '7d': 'Weekly' })[label] ?? label
export const creditsLabel = (credits?: Footer['credits']) => credits?.unlimited ? 'Unlimited' : credits?.balance != null ? String(credits.balance) : ''
// The strip under Claude's input box: its suggested next prompt, notices, mode, status line and background agents.
export interface Footer { observed?: boolean; suggestion?: string; notices?: string[]; effort?: string; tier?: string; mode?: string; status?: string; agents?: FooterAgent[]; update?: string
  model?: string; advisor?: { model: string; name: string; source: 'tether' | 'claude'; problems: { code: string; text: string }[] } | null; context?: { percent: number; tokens?: number | null; limit?: number | null }; windows?: UsageWindow[]
  credits?: { balance?: string | number | null; unlimited?: boolean; has_credits?: boolean }
  goal?: string; goal_status?: string; tasks?: string[]; task_details?: FooterTask[]; ide?: string; crons?: FooterCron[]
  spinner?: { verb: string; detail: string } | null
  running?: { title: string; elapsed: string; command: string } | null
  denied?: { text: string; reason: string } | null
  // The last auto mode block of this turn, from the PermissionDenied hook.
  blocked?: { tool: string; description: string; command: string; reason: string; at: number } | null
  heads_up?: { text: string; state?: 'offer' | 'explained' | 'making' | 'ready' | 'failed'; detail?: string[]; url?: string | null; more?: number; full?: string; note?: string; options: { key: string; label: string }[] } | null }
// Goals for Claude and Codex, plus Claude's background shells and monitors.
export const goalStatusLabel = (status: string) => ({ active: 'Active', paused: 'Paused', blocked: 'Blocked', usage_limited: 'Usage limit reached', budget_limited: 'Budget limit reached', complete: 'Complete' })[status] ?? status
export interface LiveGoal { condition: string; met: boolean; status?: string; token_budget?: number | null; tokens_used?: number; time_used_seconds?: number; reason?: string; set_at?: number | null; checked_at?: number | null }
export interface LiveTask { id: string; kind: 'shell' | 'monitor'; title: string; command: string; started: number | null; output: string | null; state: 'running' | 'ended' }
export interface LiveState { goal: LiveGoal | null; tasks: LiveTask[] }
export interface ModelOptions { agent: string; models: { id: string; label: string; current: boolean; effort?: boolean }[]; efforts: string[]; current_effort: string | null }

/** One pane's outcome of a broadcast (server/broadcast.py). sent_busy: the agent was working and took the typed text;
 * Tether keeps no queue. handed_off: in the Claude master's target list. */
export interface BroadcastRow { pane: string; name: string; agent: Agent | null; result: 'sent' | 'sent_busy' | 'skipped' | 'failed' | 'handed_off'; reason?: string }
export interface BroadcastResult { via: 'direct' | 'claude'; results: BroadcastRow[]; master?: BroadcastRow }
