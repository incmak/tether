import type { Api, Pane } from './api'
import { groupOf } from './groups'
import { needsMak } from './helperTabs'
import type { GroupInfo } from './api'

export function taskOf(pane: Pane, panes: Pane[]) {
  return pane.task ? panes.find(item => item.id === pane.task && !item.task) ?? pane : pane
}

export function taskChildren(task: Pane, panes: Pane[]) {
  return panes.filter(item => item.task === task.id)
}

export function taskState(pane: Pane, children: Pane[] = []) {
  if (needsMak(pane) || children.some(needsMak)) return 'needs'
  if (pane.done) return 'done'
  if (pane.state === 'Working') return 'working'
  if (pane.state === 'Stopped') return 'stopped'
  return 'idle'
}

export const taskStateLabel = { needs: 'needs you', working: 'working', done: 'done', idle: 'idle', stopped: 'stopped' }
export const taskRole = (pane: Pane) => pane.task_role ?? 'Helper'
export const isTaskAdvisor = (pane: Pane) => pane.role === 'advisor' || pane.task_role === 'Advisor' || pane.task_role === 'Second advisor'
export const taskIdentity = (pane: Pane) => JSON.stringify([pane.id, pane.thread])

export function canMoveInto(pane: Pane, target: Pane, panes: Pane[], groups?: GroupInfo[]) {
  return pane.id !== target.id && pane.tmux_session === target.tmux_session && pane.agent !== 'shell' &&
    !target.task && target.agent !== 'shell' && pane.task !== target.id && !panes.some(item => item.task === pane.id) &&
    groupOf(taskOf(pane, panes), groups) === groupOf(target, groups)
}

export function moveIntoTask(api: Api, pane: Pane, target: Pane | null) {
  return api('task-tree/move', { pane: pane.id, thread: pane.thread, into: target?.id ?? null, into_thread: target?.thread ?? null })
}
