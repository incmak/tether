// The change a Codex or autonomy level POST reports (B56): the server's old value and the revision an Undo must send.
export interface LevelChange { old: number | ProjectValue; new: number | ProjectValue; revision: string }
export interface ProjectValue { level: number; override: boolean }
/** Undo restores the value the server replaced, not the one this tab showed; the revision makes the server refuse it
 * if anything wrote the level after this change. */
export function undoBody(change: LevelChange | undefined, project?: string): Record<string, unknown> | null {
  if (!change || JSON.stringify(change.old) === JSON.stringify(change.new)) return null
  return typeof change.old === 'number' ? { level: change.old, undo: true, revision: change.revision }
    : { project, override: change.old.override, level: change.old.level, undo: true, revision: change.revision }
}
export const levelText = (value: number | ProjectValue) => typeof value === 'number' ? `${value}` : value.override ? `${value.level}` : 'global'
