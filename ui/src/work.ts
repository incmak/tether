import { useCallback } from 'react'
import type { Api } from './api'
import { usePolling } from './hooks'
import { exactField, field, scoreWords } from './search'
import type { Field } from './search'

// The work board's data (server: work_items.py, work_api.py). Items move by stage; an agent's question moves an item
// to Needs you, and Mak's answer goes back to the asking tab.

export type Stage = 'triage' | 'needs' | 'ready' | 'building' | 'review' | 'done' | 'hold'
export const STAGE_LABEL: Record<Stage, string> = { triage: 'Triage', needs: 'Needs you', ready: 'Ready', building: 'Building', review: 'Review', done: 'Done', hold: 'On hold' }

export type FeedKind = 'teams' | 'github' | 'email'
/** One feed of a project. id: kept across saves (its state file and cursor go with it); a new feed has none until
 * saved. by agent: the project's own agent reads it and Tether does not. since: when Tether starts reading it. */
export interface WorkFeed { id?: string; kind: FeedKind; target: string; by: 'tether' | 'agent'; since?: number }
export const FEED_KIND_LABEL: Record<FeedKind, string> = { teams: 'Teams', github: 'GitHub', email: 'Email' }
/** A feed in a sentence: "GitHub o/r", "Email Inbox". */
export const feedLabel = (kind: FeedKind, target: string) => `${FEED_KIND_LABEL[kind] ?? kind} ${target || (kind === 'email' ? 'Inbox' : '')}`.trim()
/** A project with no feeds gets its items by hand. */
export interface WorkProject { name: string; prefix: string; folder: string; triage: string; work: string; feeds: WorkFeed[]; jira: string; qaStatus: string; jiraUser: string }
export interface WorkTab { pane: string; role: 'triage' | 'fix' | 'review' | 'other'; chat: string; name: string; session: string; stack: string }
export interface WorkAsk { id: string; q: string; options: string[]; pane: string; chat: string; at: number }
export interface WorkHistory { at: number; by: string; text: string; ref?: string }
export interface WorkItem {
  id: string; project: string; title: string; stage: Stage; qa: boolean; ask: WorkAsk | null; askFrom: Stage | null
  refs: string[]; tabs: WorkTab[]; history: WorkHistory[]; source: string; created: number; updated: number
  doneAt: number | null; jira: WorkJira | null; version: number
  /** The feed key (gh:…, teams:…, mail:…) when a feed added the item. */
  feed?: string
}
/** The last Jira move of a Done item. moved: every ticket is in target. owners: some belong to someone else (owners
 * holds one note each). failed: error says why. off: Jira is not connected. tickets: the ones now in target. */
export interface WorkJira { status?: 'moved' | 'owners' | 'failed' | 'off'; error?: string; tickets?: string[]; owners?: string[]; target?: string }
/** The Jira connection on the Tether host (`tether jira connect`). Never holds the token. guard: Mak confirmed the
 * Only Assignee condition; without it nothing moves. why: what to do when not connected or not guarded. */
export interface JiraLink { connected: boolean; site?: string; account?: string; guard?: boolean; why?: string }
/** The Microsoft 365 connection for Teams and email feeds (`tether graph connect`). Never holds a token. mail, teams:
 * the granted permissions allow that feed. why: what to do when not connected. */
export interface GraphLink { connected: boolean; account?: string; mail?: boolean; teams?: boolean; why?: string }
/** One feed as Tether reads it, by the feed's id. by agent: the project's own agent reads it and Tether does not.
 * waiting: not read yet. off: the feed cannot run until Mak acts (error says what to do). errors: failed reads in a row. */
export interface FeedState { id: string; kind: FeedKind; target: string; by: 'tether' | 'agent'; status: 'ok' | 'off' | 'error' | 'waiting'; at: number | null; error: string; errors: number; items: number }
export interface WorkBoard { revision: number; projects: WorkProject[]; items: WorkItem[]; stages: Stage[]; jira?: JiraLink; graph?: GraphLink; feeds?: Record<string, FeedState[]>; backlog?: BacklogCards }
/** A BACKLOG.md item on the board (server: work_backlog.py). The file stays the record. area: its section, or Inbox.
 * rank: its place in the file. doing, done: the text of its Doing and Done lines. doneAt: from a dated Done line;
 * ticked items without one stay off the board. version: of the item's own lines. */
export interface BacklogCard {
  id: string; project: string; area: string; title: string; rank: number; stage: BacklogStage; source: string
  doing: string | null; done: string | null; doneAt: number | null; details: string[]; version: number
}
export type BacklogStage = 'ready' | 'building' | 'done'
/** error: the backlog could not be read; the rest of the board still shows. */
export interface BacklogCards { items: BacklogCard[]; error: string }
/** A Build answer, or Start fix tab, opens a fix tab. When it does not start, the item stays Ready and why says so. */
export interface FixResult { started: boolean; why: string }
export interface AnswerResult { item: WorkItem; sent: boolean; why: string; fix?: FixResult }

export const BOARD_PATH = 'work/board'
const POLL_MS = 5000

/** The board, polled while the page is open. One call in App feeds Home, the Needs you list and the board. */
export function useWork(api: Api) {
  const fetcher = useCallback((signal: AbortSignal) => api<WorkBoard>(BOARD_PATH, undefined, signal), [api])
  return usePolling(fetcher, POLL_MS)
}

/** Items that wait on Mak, oldest question first. */
export const decisions = (board?: WorkBoard) =>
  (board?.items ?? []).filter(item => item.stage === 'needs' && item.ask).sort((a, b) => (a.ask!.at ?? 0) - (b.ask!.at ?? 0))

export const answerItem = (api: Api, item: WorkItem, reply: { choice: string } | { text: string }) =>
  api<AnswerResult>('work/answer', { item: item.id, ask: item.ask!.id, version: item.version, ...reply })

/** A project's swatch: one of six hues, picked by its prefix, so a project keeps its color on every device. */
export function projectHue(prefix: string) {
  let sum = 0
  for (const char of prefix) sum = (sum * 31 + char.charCodeAt(0)) % 997
  return sum % 6
}

export const moveItem = (api: Api, item: WorkItem, stage: Stage) =>
  api<{ item: WorkItem }>('work/stage', { item: item.id, stage, version: item.version })

export const startFix = (api: Api, item: WorkItem) =>
  api<{ item: WorkItem; fix: FixResult }>('work/fix', { item: item.id, version: item.version })

/** Start triage tab: for a Triage item with no live triage tab (a feed item that found no free tab, or a closed tab). */
export const startTriage = (api: Api, item: WorkItem) =>
  api<{ item: WorkItem; tab: FixResult }>('work/triage', { item: item.id, version: item.version })

/** Add or change a project. revision: the registry revision the form started from (409 when it changed). */
export const saveProject = (api: Api, project: WorkProject, revision: number, create: boolean) =>
  api<{ revision: number; projects: WorkProject[] }>('work/project', { project, revision, create })

/** Add an item by hand. tab: start a triage tab for it; tab in the answer says whether it started. */
export const newItem = (api: Api, project: string, title: string, refs: string[], tab: boolean) =>
  api<{ item: WorkItem; tab?: FixResult }>('work/item', { project, title, refs, tab })

/** Move again: queue the Jira move of a Done item. The result shows on the item after the next poll. */
export const moveJira = (api: Api, item: WorkItem) =>
  api<{ item: WorkItem; queued: boolean; why: string }>('work/jira', { item: item.id, version: item.version })

export const setItemQa = (api: Api, item: WorkItem, qa: boolean) =>
  api<{ item: WorkItem }>('work/qa', { item: item.id, qa, version: item.version })

const DAY = 86400
/** Done items leave the board after a week; their history stays in the store. */
export const onBoard = (item: WorkItem, now: number) => item.stage !== 'done' || !item.doneAt || now - item.doneAt < 7 * DAY

/** The item each open tab works on, for the sidebar: the newest link wins when a tab is linked to more than one. */
export function itemsByPane(board?: WorkBoard) {
  const now = Date.now() / 1000
  const marks: Record<string, { id: string; stage: Stage }> = {}
  for (const item of [...(board?.items ?? [])].filter(entry => onBoard(entry, now)).sort((a, b) => a.updated - b.updated))
    for (const tab of item.tabs) marks[tab.pane] = { id: item.id, stage: item.stage }
  return marks
}

/** Move a backlog item: Ready (reopen, or stop Doing), Building (a Doing line) or Done (ticked, with a dated Done line). */
export const moveBacklog = (api: Api, card: BacklogCard, stage: BacklogStage) =>
  api<{ item: BacklogCard }>('work/stage', { item: card.id, stage, version: card.version })

/** Backlog cards the board shows: Done ones for a week, like work items. */
export const backlogOnBoard = (board: WorkBoard | undefined, now: number) =>
  (board?.backlog?.items ?? []).filter(card => card.stage !== 'done' || !card.doneAt || now - card.doneAt < 7 * DAY)

/** Backlog cards a column shows before "Show more". Ready can hold dozens. */
export const BACKLOG_CAP = 5

/** Ready and Building in file order (the person's own order); Done newest first. */
export const backlogOrder = (a: BacklogCard, b: BacklogCard) =>
  a.stage === 'done' && b.stage === 'done' ? (b.doneAt ?? 0) - (a.doneAt ?? 0) : a.rank - b.rank

// The board's search: the same matcher as the sidebar, ⌘K and the backlog (search.ts).
// Title and ID count double. Project, stage, links, tabs and source match fuzzy too.
// The question, history and backlog notes match whole words only, so long text adds no noise.
// IDs and links sit in their own fields: a word shaped like an ID ("b1", "t-21") matches them exactly, not as a prefix.
interface SearchFields { text: Field[]; ids: Field[]; idList: string[]; links: string[] }
const searchFields = new WeakMap<WorkItem | BacklogCard, SearchFields>()
function fieldsOf(entry: WorkItem | BacklogCard) {
  let fields = searchFields.get(entry)
  if (fields) return fields
  fields = 'area' in entry
    ? { text: [field(2, entry.title), field(1, 'backlog', entry.area, STAGE_LABEL[entry.stage], entry.source), exactField(1, ...entry.details, entry.doing, entry.done)],
      ids: [field(2, entry.id)], idList: [entry.id], links: [] }
    : { text: [field(2, entry.title),
        field(1, entry.project, STAGE_LABEL[entry.stage], ...entry.tabs.flatMap(tab => [tab.name, tab.stack, tab.session]), entry.source),
        exactField(1, entry.ask?.q, ...(entry.ask?.options ?? []), ...entry.history.map(line => line.text))],
      ids: [field(2, entry.id, ...(entry.jira?.tickets ?? [])), field(1, ...entry.refs)], idList: [entry.id], links: [...entry.refs, ...(entry.jira?.tickets ?? [])] }
  searchFields.set(entry, fields)
  return fields
}

// A word shaped like an ID: T-21, B12, B-12, SHOP-77. It also matches ordinary text, like covid-19 in a title.
const ID = /^(?:[a-z]{1,5}-\d+|b\d+)$/
const sameId = (a: string, b: string) => { const norm = (id: string) => id.toLowerCase().replace(/^b-(?=\d)/, 'b'); return norm(a) === norm(b) }

/** What a card marks for these query words. title: the words that match by text. id: an ID word names the entry
 * itself. An ID word that found the entry by its ID or a link marks no title letters. */
export function markWords(entry: WorkItem | BacklogCard, words: string[]) {
  const fields = fieldsOf(entry)
  const title: string[] = []
  let id = false
  for (const word of words) {
    if (ID.test(word) && fields.idList.some(entryId => sameId(entryId, word))) id = true
    else if (!ID.test(word) || !fields.links.some(link => sameId(link, word))) title.push(word)
  }
  return { title, id }
}

/** The search score of an item or backlog card for these query words (searchWords). 0: no query. null: no match.
 * An ID alone, like T-21 or B12, finds that item and the items that link to it, not T-210 or B120. */
export function workScore(entry: WorkItem | BacklogCard, words: string[]): number | null {
  if (!words.length) return 0
  const fields = fieldsOf(entry)
  let total = 0
  for (const word of words) {
    let score: number | null
    if (ID.test(word)) {
      // The item itself ranks first, then the items that link to it (a ref, or a Jira ticket), then text matches.
      score = fields.idList.some(id => sameId(id, word)) ? 10 : fields.links.some(link => sameId(link, word)) ? 8 : scoreWords(fields.text, [word])
    } else score = scoreWords([...fields.text, ...fields.ids], [word])
    if (score === null) return null
    total += score
  }
  return total
}
