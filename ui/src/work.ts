import { useCallback } from 'react'
import type { Api } from './api'
import { usePolling } from './hooks'

// The work board's data (server: work_items.py, work_api.py). Items move by stage; an agent's question moves an item
// to Needs you, and Mak's answer goes back to the asking tab.

export type Stage = 'triage' | 'needs' | 'ready' | 'building' | 'review' | 'done' | 'hold'
export const STAGE_LABEL: Record<Stage, string> = { triage: 'Triage', needs: 'Needs you', ready: 'Ready', building: 'Building', review: 'Review', done: 'Done', hold: 'On hold' }

export interface WorkProject { name: string; prefix: string; folder: string; triage: string; work: string; feed: string; feedTarget: string; jira: string; qaStatus: string; jiraUser: string }
export interface WorkTab { pane: string; role: 'triage' | 'fix' | 'review' | 'other'; chat: string; name: string; session: string; stack: string }
export interface WorkAsk { id: string; q: string; options: string[]; pane: string; chat: string; at: number }
export interface WorkHistory { at: number; by: string; text: string; ref?: string }
export interface WorkItem {
  id: string; project: string; title: string; stage: Stage; qa: boolean; ask: WorkAsk | null; askFrom: Stage | null
  refs: string[]; tabs: WorkTab[]; history: WorkHistory[]; source: string; created: number; updated: number
  doneAt: number | null; jira: WorkJira | null; version: number
}
/** The last Jira move of a Done item. moved: every ticket is in target. owners: some belong to someone else (owners
 * holds one note each). failed: error says why. off: Jira is not connected. tickets: the ones now in target. */
export interface WorkJira { status?: 'moved' | 'owners' | 'failed' | 'off'; error?: string; tickets?: string[]; owners?: string[]; target?: string }
/** The Jira connection on the Tether host (`tether jira connect`). Never holds the token. guard: Mak confirmed the
 * Only Assignee condition; without it nothing moves. why: what to do when not connected or not guarded. */
export interface JiraLink { connected: boolean; site?: string; account?: string; guard?: boolean; why?: string }
export interface WorkBoard { revision: number; projects: WorkProject[]; items: WorkItem[]; stages: Stage[]; jira?: JiraLink }
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
