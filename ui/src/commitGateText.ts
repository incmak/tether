// The commit gate (server: commit_gate.py). A Claude Code hook checks each `git commit` for a "Verified:" line.
// The person sets the mode; nothing else changes it.
export type GateMode = 'off' | 'observe' | 'enforce'
export interface GateModeInfo { mode: GateMode; name: string; summary: string }
export interface GateStats {
  days: number; commits: number; passed: number; would_block: number; blocked: number; let_through: number; unknown: number
  stamped: number; unstamped: number; false_blocks: number; rate: number | null
}
export interface CommitGateState {
  modes: GateModeInfo[]; global: GateMode; mode: GateMode; source: 'global' | 'project'
  project: { override: boolean; mode: GateMode } | null; stats: GateStats; max_blocks: number
}

const count = (value: number, one: string, many: string) => `${value.toLocaleString('en-US')} ${value === 1 ? one : many}`

/** The two status lines under the mode: what the gate saw, and how often it was wrong. */
export function gateSummary(stats: GateStats, mode: GateMode): { seen: string; rate: string } {
  if (!stats.commits) return { seen: mode === 'off' ? 'The gate is off. No commit is checked.' : 'No commits checked yet.', rate: '' }
  const noLine = stats.would_block + stats.blocked + stats.let_through
  const parts = [count(stats.commits, 'commit checked', 'commits checked'), `${noLine.toLocaleString('en-US')} with no valid line`]
  if (stats.blocked || stats.let_through) parts.push(`${stats.blocked.toLocaleString('en-US')} blocked, ${stats.let_through.toLocaleString('en-US')} let through`)
  if (stats.unknown) parts.push(`${stats.unknown.toLocaleString('en-US')} not readable`)
  const rate = stats.rate === null
    ? 'No false-block rate yet. It needs a checked commit that has a valid line.'
    : `False-block rate: ${(stats.rate * 100).toLocaleString('en-US', { maximumFractionDigits: 1 })}% (${stats.false_blocks.toLocaleString('en-US')} of ${count(stats.stamped, 'commit', 'commits')} with a valid line).`
  return { seen: parts.join(' · '), rate }
}

