import { useState } from 'react'
import { Check, FolderPlus, Sparkles, X } from 'lucide-react'
import { Spinner } from './Skeleton'
import type { Api } from './api'
import { errorText } from './api'
import type { Anchor } from './hooks'
import type { Prefs } from './prefs'
import { Choice } from './SettingsControls'
import { useActionsDialog } from './Sheets'
import type { Session } from './Sidebar'

// Colors a session or a tab can wear in the sidebar: an accent bar plus a light tint of the row.
export const TAG_COLORS = ['red', 'orange', 'yellow', 'green', 'blue', 'purple'] as const
export type TagColor = typeof TAG_COLORS[number]
export interface SessionGroup { id: string; name: string }
export type GroupBy = 'custom' | 'folder' | 'agent' | 'none'
export type SortBy = 'smart' | 'manual' | 'name' | 'recent'

/** A copy of `colors` with `id` set to `color`, or removed for null. */
export function withColor(colors: Record<string, string> | undefined, id: string, color: TagColor | null) {
  const next = { ...colors }
  if (color) next[id] = color; else delete next[id]
  return next
}
/** A copy of `groupOf` with session `id` in `group`, or in no group. */
export function withGroup(groupOf: Record<string, string> | undefined, id: string, group: string | undefined) {
  const next = { ...groupOf }
  if (group) next[id] = group; else delete next[id]
  return next
}
/** The row style for a color, or nothing. */
export const tagStyle = (color: string | undefined) => color && (TAG_COLORS as readonly string[]).includes(color) ? { ['--tag' as string]: `var(--tag-${color})` } : undefined

/** The small dot after a row's name that shows its color, or nothing. A dot, not a tinted row: a tint reads as the open tab. */
export const tagDot = (color: string | undefined) => {
  const style = tagStyle(color)
  return style ? <span className="tag-dot" style={style} role="img" aria-label={`${color} color`} /> : null
}

/** A group id from its name: lower case, a-z 0-9 and "-", unique among `taken`. */
export function groupId(name: string, taken: string[]) {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'group'
  let id = base
  for (let n = 2; taken.includes(id); n++) id = `${base}-${n}`
  return id
}

export function ColorRow({ value, disabled, onChange }: { value?: string; disabled?: boolean; onChange: (color: TagColor | null) => void }) {
  return <div className="color-row" role="radiogroup" aria-label="Color">
    <button type="button" role="radio" aria-checked={!value} aria-label="No color" title="No color" disabled={disabled} className="color-swatch none" onClick={() => onChange(null)}>{!value && <Check size={13} />}</button>
    {TAG_COLORS.map(color => <button key={color} type="button" role="radio" aria-checked={value === color} aria-label={color} title={color[0].toUpperCase() + color.slice(1)} disabled={disabled} className="color-swatch" style={tagStyle(color)} onClick={() => onChange(color)}>{value === color && <Check size={13} />}</button>)}
  </div>
}

/** Choose the group of one session, or make a new one. */
export function GroupPicker({ groups, value, onPick, onBack }: { groups: SessionGroup[]; value?: string; onPick: (group: SessionGroup | null, created?: SessionGroup) => void; onBack: () => void }) {
  const [name, setName] = useState('')
  const add = (event: React.FormEvent) => {
    event.preventDefault()
    const clean = name.trim().slice(0, 40)
    if (!clean) return
    const same = groups.find(group => group.name.toLowerCase() === clean.toLowerCase())
    if (same) { onPick(same); return }
    const created = { id: groupId(clean, groups.map(group => group.id)), name: clean }
    onPick(created, created)
  }
  return <div className="group-picker">
    <div className="action-list" role="radiogroup" aria-label="Group">
      {groups.map(group => <button key={group.id} type="button" role="radio" aria-checked={value === group.id} onClick={() => onPick(group)}>{value === group.id ? <Check size={18} /> : <span className="picker-space" />}{group.name}</button>)}
      <button type="button" role="radio" aria-checked={!value} onClick={() => onPick(null)}>{!value ? <Check size={18} /> : <span className="picker-space" />}No group</button>
    </div>
    {groups.length < 20 && <form className="group-new" onSubmit={add}>
      <input aria-label="New group name" placeholder="New group" maxLength={40} value={name} autoComplete="off" onChange={event => setName(event.target.value)} />
      <button type="submit" className="secondary" disabled={!name.trim()}><FolderPlus size={16} />Add</button>
    </form>}
    <div className="sheet-buttons"><button type="button" className="secondary" onClick={onBack}>Back</button></div>
  </div>
}

interface Suggestion { groups: { name: string; sessions: string[] }[]; source: 'claude' | 'codex' | 'llm' | 'folder' }

/** Group by, sort, and groups suggested by the local model. Applying a suggestion replaces the custom groups. */
export function OrganizeMenu({ api, sessions, prefs, update, at, onClose }: { api: Api; sessions: Session[]; prefs: Prefs; update: (change: (current: Prefs) => Partial<Prefs>) => void; at?: Anchor; onClose: () => void }) {
  const dialog = useActionsDialog(at, onClose)
  const [asking, setAsking] = useState(false)
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null)
  const [error, setError] = useState('')
  const names = new Map(sessions.map(session => [session.id, session.name]))
  async function suggest() {
    setAsking(true); setError(''); setSuggestion(null)
    try {
      const body = { sessions: sessions.slice(0, 200).map(session => ({ id: session.id, name: session.name, dir: session.dir, chats: session.panes.map(pane => pane.pane_name || pane.agent_name || '').filter(Boolean).slice(0, 12) })), ...(prefs.mainModel !== 'none' ? { agent: prefs.mainModel } : {}) }
      // The server tries the main AI model (up to 45 s), then the local model (up to 90 s), then folders.
      setSuggestion(await api<Suggestion>('suggest-groups', body, undefined, undefined, 150000))
    } catch (failure) { setError(errorText(failure)) } finally { setAsking(false) }
  }
  function apply(found: Suggestion) {
    const groups: SessionGroup[] = []
    const groupOf: Record<string, string> = {}
    for (const item of found.groups.slice(0, 20)) {
      const group = { id: groupId(item.name, groups.map(other => other.id)), name: item.name.slice(0, 40) }
      groups.push(group)
      for (const id of item.sessions) groupOf[id] = group.id
    }
    update(() => ({ groups, groupOf, groupBy: 'custom' }))
    onClose()
  }
  return <dialog {...dialog} aria-labelledby="organize-title">
    <div className="sheet-heading"><h2 id="organize-title">Organize sessions</h2>{!at && <button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button>}</div>
    <div className="organize-body">
      <Choice legend="Group by" name="group-by" value={prefs.groupBy} options={[['agent', 'Agent'], ['folder', 'Folder'], ['custom', 'My groups'], ['none', 'None']]} onChange={groupBy => update(() => ({ groupBy }))} />
      <Choice legend="Sort" name="sort" value={prefs.sort} options={[['smart', 'Smart'], ['recent', 'Recent'], ['name', 'Name'], ['manual', 'Manual']]} onChange={sort => update(() => ({ sort }))} />
      <p className="hint organize-hint">{prefs.sort === 'smart' ? 'Done first, then working, then the latest activity.' : prefs.sort === 'recent' ? 'The latest agent activity first.' : prefs.sort === 'name' ? 'A to Z.' : 'Your own order. Use Reorder in a group.'} Sessions that need you always show at the top.</p>
      <section className="suggest-box" aria-live="polite">
        {!suggestion && <button type="button" className="secondary suggest-button" disabled={asking} onClick={() => void suggest()}>{asking ? <Spinner size={16} /> : <Sparkles size={16} />}{asking ? 'Suggesting groups… about 15 seconds' : 'Suggest groups'}</button>}
        {suggestion && <>
          <p className="suggest-note">{suggestion.source === 'claude' ? 'Suggested by Claude.' : suggestion.source === 'codex' ? 'Suggested by Codex.' : suggestion.source === 'llm' ? 'Suggested by the local model.' : 'The model gave no useful groups. These groups follow the folders.'} Apply replaces your groups.</p>
          <ul className="suggest-list">{suggestion.groups.map(group => <li key={group.name}><strong>{group.name}</strong><span>{group.sessions.map(id => names.get(id) ?? id).join(', ')}</span></li>)}</ul>
          <div className="sheet-buttons"><button type="button" className="secondary" onClick={() => setSuggestion(null)}>Discard</button><button type="button" className="primary" disabled={!suggestion.groups.length} onClick={() => apply(suggestion)}>Apply groups</button></div>
        </>}
        {error && <p className="error" role="alert">{error}</p>}
      </section>
    </div>
  </dialog>
}
