// Claude Code's /permissions dialog, read from the screen by server/permissions_dialog.py.
// The tabs and keys drive the real dialog; Tether keeps no permission state of its own.
import { useCallback, useEffect, useRef } from 'react'
import { wheelSideways } from './wheelSideways'
import type { PermissionsDialog } from './api'

const BLOCKED = 'Recently blocked'

export function PermissionsPanel({ dialog, disabled, want, onKey }: { dialog: PermissionsDialog; disabled: boolean; want?: string | null; onKey: (key: string) => Promise<unknown> }) {
  const active = dialog.tab ? dialog.tabs.indexOf(dialog.tab) : -1
  const moving = useRef(false)
  const go = async (index: number) => {
    if (active < 0 || index === active || moving.current) return
    moving.current = true
    try { for (let step = 0; step < Math.abs(index - active); step++) await onKey(index > active ? 'Right' : 'Left') } finally { moving.current = false }
  }
  // Opened from a blocked notice: go to Recently blocked once.
  const wanted = useRef(want)
  useEffect(() => {
    if (!wanted.current || active < 0) return
    const index = dialog.tabs.indexOf(wanted.current)
    wanted.current = null
    if (index >= 0) void go(index)
  })
  const blocked = dialog.tab === BLOCKED
  const row = dialog.selected != null ? dialog.lines[dialog.selected] ?? '' : ''
  const locked = /can['’]t be approved here/i.test(row)
  const canRetry = blocked && !dialog.empty && dialog.selected != null && !locked
  const press = (key: string) => () => void onKey(key)
  const autoTab = dialog.tabs.indexOf('Auto mode')
  // Narrow screens scroll the tab row; keep the active tab in view.
  const tabRow = useRef<HTMLDivElement>(null)
  const tabRowRef = useCallback((node: HTMLDivElement | null) => { tabRow.current = node; const stop = wheelSideways(node); return () => { tabRow.current = null; stop?.() } }, [])
  useEffect(() => { tabRow.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }, [dialog.tab])
  // Drop the drawn search box (Tether cannot type into it; its border wraps on a phone) and the leading blank lines.
  // Each row keeps its index in dialog.lines, so the selected row stays right.
  const rows = dialog.lines.map((line, index) => ({ line, index })).filter(({ line }) => !/^\s*[╭╰│]/.test(line))
  const first = rows.findIndex(({ line }) => line.trim())
  const body = first > 0 ? rows.slice(first) : rows
  return <div className="perm-panel">
    <div ref={tabRowRef} className="perm-tabs" role="tablist" aria-label="Permissions tabs">
      {dialog.tabs.map((tab, index) => <button key={tab} type="button" role="tab" aria-selected={tab === dialog.tab} disabled={disabled || active < 0} onClick={() => void go(index)}>{tab}</button>)}
    </div>
    {blocked && !dialog.empty && <p className="perm-note">Retry once lets Claude try the action one more time. Claude’s safety check reviews it again and can block it again. It saves no rule.</p>}
    <ul className="perm-body" aria-label={dialog.tab ?? 'Permissions'}>
      {body.map(({ line, index }) => <li key={index} className={index === dialog.selected ? 'current' : ''} aria-current={index === dialog.selected || undefined}>{line || ' '}</li>)}
    </ul>
    {locked && <p className="perm-note">This action can’t be approved here. If you meant it, tell Claude in your next message.</p>}
    <div className="choice-keys">
      {!dialog.empty && <>
        <button type="button" disabled={disabled} aria-label="Move up" onClick={press('Up')}>↑</button>
        <button type="button" disabled={disabled} aria-label="Move down" onClick={press('Down')}>↓</button>
      </>}
      {canRetry && <button type="button" className="primary" disabled={disabled} onClick={press('r')}>Retry once</button>}
      {blocked ? !dialog.empty && <button type="button" disabled={disabled} title="Approve it, and Claude may retry after your next message. Press again to take it back." onClick={press('Enter')}>Approve for later</button>
        : <button type="button" disabled={disabled} onClick={press('Enter')}>Select (Enter)</button>}
      <button type="button" disabled={disabled} onClick={press('Escape')}>{blocked ? 'Close and apply' : 'Close (Esc)'}</button>
    </div>
    {blocked && !dialog.empty && autoTab >= 0 && <p className="perm-note">To allow this kind of action every time, add an allow rule or a trusted place in the <button type="button" className="link-button" disabled={disabled} onClick={() => void go(autoTab)}>Auto mode tab</button>.</p>}
  </div>
}
