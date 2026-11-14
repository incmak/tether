import { useEffect, useMemo, useState } from 'react'
import { CircleCheck, ClipboardList, CornerDownLeft, Search, SquareTerminal, Zap } from 'lucide-react'
import type { Api, Pane } from './api'
import { useModal } from './hooks'
import { fuzzyScore, keyLabel } from './keys'
import { agentNames, paneTitle, shownIndex, status } from './Sidebar'
import type { Session } from './Sidebar'
import { BACKLOG_PATH } from './backlogNews'
import { parseBacklog } from './backlogItems'
import { projectGroups, queryScore } from './backlogView'

/** One command of the menu. Leave a command out when its action does not apply now. */
export interface Command { label: string; key?: string; run: () => void }

type Group = 'Panes' | 'Commands' | 'Backlog'
interface Row { id: string; group: Group; label: string; hint?: string; key?: string; dot?: string; done?: boolean; run: () => void }

interface Props {
  sessions: Session[]
  seen: Record<string, number>
  selected?: string
  /** Pane ids in slot order: their rows show the slot key. */
  slots: string[]
  commands: Command[]
  onPane: (pane: Pane) => void
  onSession: (session: Session) => void
  onClose: () => void
  /** Search the backlog too. `open` shows the backlog page with this search. */
  backlog?: { api: Api; open: (query: string) => void }
}

interface BacklogHit { ref: string; text: string; project: string; done: boolean; score: number }
const BACKLOG_ROWS = 6

/** The backlog, read once when the menu opens. text null: still loading; failed: why it did not load. */
function useBacklogText(api: Api | undefined) {
  const [text, setText] = useState<string | null>(null)
  const [failed, setFailed] = useState('')
  useEffect(() => {
    if (!api) return
    const stop = new AbortController()
    api<{ text: string }>(BACKLOG_PATH, undefined, stop.signal)
      .then(data => setText(data.text))
      .catch(() => { if (!stop.signal.aborted) setFailed('The backlog did not load.') })
    return () => stop.abort()
  }, [api])
  return { text, failed }
}

const TYPE: Record<Group, string> = { Panes: 'Pane', Commands: 'Command', Backlog: 'Backlog' }

/** ⌘K or ⌥Space: go to a pane or session, run a command, or find a backlog item. Each command row shows its key, so the menu teaches the keys. */
export function CommandMenu({ sessions, seen, selected, slots, commands, onPane, onSession, onClose, backlog }: Props) {
  // Opening the menu keeps the Forward steps, so its Forward command can use them.
  const modal = useModal(true, onClose, undefined, { keepForward: true })
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const { text: backlogText, failed: backlogFailed } = useBacklogText(backlog?.api)
  // Each item with its project, so a search for a project name finds its items.
  const backlogItems = useMemo(() => backlogText === null ? [] : projectGroups(parseBacklog(backlogText)).flatMap(group => group.items.map(item => ({ item, project: group.name }))), [backlogText])
  const rows = useMemo(() => {
    const panes: Row[] = sessions.flatMap(session => {
      const one = session.panes.length === 1
      const paneRows = session.panes.map(pane => {
        const state = status([pane], seen, selected)
        const at = slots.indexOf(pane.id)
        return { id: pane.id, group: 'Panes' as const, label: one ? session.name : `${session.name} · ${[paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')}`, hint: `${one ? agentNames[pane.agent] + ' · ' : ''}${state.label}`, key: at < 0 ? undefined : `⌥${at + 1}`, dot: state.kind, run: () => onPane(pane) }
      })
      return one ? paneRows : [{ id: `session:${session.id}`, group: 'Panes' as const, label: session.name, hint: `Show all ${Math.min(4, session.panes.length)} panes`, run: () => onSession(session) }, ...paneRows]
    })
    const commandRows: Row[] = commands.map(command => ({ id: `command:${command.label}`, group: 'Commands', ...command }))
    const words = query.trim()
    if (!words) return [...commandRows, ...panes]
    // A pane also matches on its folder and agent, the same fields the sidebar search reads. A match in the name ranks first.
    const scoreOf = (row: Row, extra: string) => { const named = fuzzyScore(row.label, words); return named !== null ? named * 2 : fuzzyScore(`${row.label} ${extra}`, words) }
    const ranked = (list: Row[], extra: (row: Row) => string) => list
      .map(row => ({ row, score: scoreOf(row, extra(row)) }))
      .filter((item): item is { row: Row; score: number } => item.score !== null)
      .sort((a, b) => b.score - a.score)
      .map(item => item.row)
    const paneText = (row: Row) => { const pane = sessions.flatMap(session => session.panes).find(item => item.id === row.id); return pane ? `${pane.dir} ${agentNames[pane.agent]}` : '' }
    // Backlog items match on every word, like the backlog's own search. Open items rank above done ones.
    const hits: BacklogHit[] = !backlog ? [] : backlogItems.flatMap(({ item, project }) => {
      const score = item.ref ? queryScore(item, words, project) : null
      return score === null ? [] : [{ ref: item.ref!, text: item.text, project, done: item.done, score: score - (item.done ? 100 : 0) }]
    }).sort((a, b) => b.score - a.score)
    const backlogRows: Row[] = hits.slice(0, BACKLOG_ROWS).map(hit => ({ id: `backlog:${hit.ref}`, group: 'Backlog', label: hit.text, hint: [hit.ref, hit.project || 'Inbox', hit.done && 'Done'].filter(Boolean).join(' · '), done: hit.done, run: () => backlog!.open(hit.ref) }))
    if (backlog && backlogText !== null) backlogRows.push({ id: 'backlog:search', group: 'Backlog', label: hits.length > BACKLOG_ROWS ? `See all ${hits.length} in the backlog` : `Search the backlog for “${words}”`, run: () => backlog.open(words) })
    return [...ranked(panes, paneText), ...ranked(commandRows, () => ''), ...backlogRows]
  }, [sessions, seen, selected, slots, commands, onPane, onSession, query, backlog, backlogItems, backlogText])
  const at = Math.min(active, Math.max(0, rows.length - 1))
  const pick = (row: Row | undefined) => { if (!row) return; onClose(); row.run() }
  const move = (to: number) => {
    const next = (to + rows.length) % rows.length
    setActive(next)
    document.getElementById(`command-${next}`)?.scrollIntoView({ block: 'nearest' })
  }
  const icon = (row: Row) => row.group === 'Panes' ? (row.dot ? <span className={`state-dot ${row.dot}`} /> : <SquareTerminal size={16} />)
    : row.group === 'Backlog' ? (row.id === 'backlog:search' ? <Search size={16} /> : row.done ? <CircleCheck size={16} /> : <ClipboardList size={16} />)
    : <Zap size={16} />
  const searching = !!query.trim()
  const placeholder = backlog ? 'Search panes, commands and the backlog' : 'Go to a pane or run a command'
  return <dialog {...modal} className="sheet command-menu" aria-label="Search">
    <label className="command-search"><Search size={20} aria-hidden="true" />
      <input autoFocus role="combobox" aria-expanded="true" aria-controls="command-list" aria-autocomplete="list" aria-activedescendant={rows.length ? `command-${at}` : undefined}
        aria-label={placeholder} placeholder={placeholder} value={query} autoComplete="off" spellCheck={false}
        onChange={event => { setQuery(event.target.value); setActive(0) }}
        onKeyDown={event => {
          if (!rows.length) return
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); move(at + (event.key === 'ArrowDown' ? 1 : -1)) }
          else if (event.key === 'Home' && event.ctrlKey) { event.preventDefault(); move(0) }
          else if (event.key === 'End' && event.ctrlKey) { event.preventDefault(); move(rows.length - 1) }
          else if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); pick(rows[at]) }
        }} />
    </label>
    <div id="command-list" role="listbox" tabIndex={-1} aria-label="Results" className="command-list">
      {rows.map((row, index) => <div key={row.id} role="presentation">
        {(index === 0 || rows[index - 1].group !== row.group) && <div className="command-group" role="presentation">{row.group}</div>}
        <div id={`command-${index}`} role="option" aria-selected={index === at} className={`command-row${index === at ? ' active' : ''}${row.done ? ' done' : ''}`}
          onMouseMove={() => { if (index !== at) setActive(index) }} onMouseDown={event => event.preventDefault()} onClick={() => pick(row)}>
          <span className="command-icon" aria-hidden="true">{icon(row)}</span>
          <span className="command-label">{row.label}</span>
          {row.hint && <span className="command-hint">{row.hint}</span>}
          {row.key ? <kbd>{keyLabel(row.key)}</kbd> : <span className="command-type" aria-hidden="true">{TYPE[row.group]}</span>}
        </div>
      </div>)}
      {!rows.length && <p className="command-empty">Nothing matches “{query.trim()}”.</p>}
      {searching && backlog && backlogText === null && <p className="command-note" role="status">{backlogFailed || 'Searching the backlog…'}</p>}
    </div>
    <div className="command-foot" aria-hidden="true">
      <span><kbd>↑</kbd><kbd>↓</kbd> Move</span>
      <span><kbd><CornerDownLeft size={11} /></kbd> Open</span>
      <span><kbd>Esc</kbd> Close</span>
    </div>
  </dialog>
}
