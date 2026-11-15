import { useEffect, useMemo, useRef, useState } from 'react'
import { CircleCheck, ClipboardList, CornerDownLeft, ListPlus, MapPin, Search, SquareKanban, SquarePlus, SquareTerminal, Zap } from 'lucide-react'
import type { Api, Pane } from './api'
import { useModal } from './hooks'
import { keyLabel } from './keys'
import { agentNames, paneTitle, searchIndex, shownIndex, status } from './Sidebar'
import type { Session } from './Sidebar'
import type { Prefs } from './prefs'
import type { GroupInfo } from './api'
import { field, matchSessions, scoreWords, searchWords } from './search'
import { STAGE_LABEL, markWords, onBoard, workScore } from './work'
import type { WorkBoard } from './work'
import { Marked } from './Marked'
import { useNow } from './attention'
import { BACKLOG_PATH } from './backlogNews'
import { parseBacklog } from './backlogItems'
import { projectGroups, queryScore } from './backlogView'
import { SelectMenu } from './SelectMenu'
import type { MenuOption } from './SelectMenu'
import { DEFAULT_SESSION, DEFAULT_STACK, placeName, placeValue, places } from './quickTab'
import type { Choice, Place } from './quickTab'
import './CommandMenu.css'

/** One command of the menu. Leave a command out when its action does not apply now. */
export interface Command { label: string; key?: string; run: () => void }

type Group = 'Panes' | 'Commands' | 'Work' | 'Backlog' | 'Create'
/** keep: the row changes the menu and does not close it. marked: the label's words to mark; none: the query's words. */
interface Row { id: string; group: Group; label: string; hint?: string; key?: string; dot?: string; done?: boolean; keep?: boolean; marked?: string[]; run: () => void }

/** The last rows of a search: keep the query as a backlog item, or start an agent tab with it. */
export interface Create {
  onBacklog: (query: string) => void
  onTab: (query: string, choice: Choice) => void
}

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
  /** The custom groups and each session's stacks: the menu finds the same tabs as the sidebar search. */
  groups?: Pick<Prefs, 'groups' | 'groupOf'>
  stacks?: Record<string, GroupInfo[]>
  /** Search the backlog too. `open` shows the backlog page with this search. */
  backlog?: { api: Api; open: (query: string) => void }
  /** Search the work board's items too. `open` shows the board on this search, with this item open. */
  work?: { board?: WorkBoard; error: string; open: (query: string, item?: string) => void }
  create?: Create
}

interface BacklogHit { ref: string; text: string; project: string; done: boolean; score: number }
const BACKLOG_ROWS = 6
const WORK_ROWS = 6

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

const TYPE: Record<Group, string> = { Panes: 'Pane', Commands: 'Command', Work: 'Work item', Backlog: 'Backlog', Create: 'New' }
const defaultName = `${DEFAULT_SESSION} · ${DEFAULT_STACK}`

/** The picker's rows: Tether's choice, the default, then each session with its stacks. */
function placeOptions(sessions: Session[]): { options: MenuOption[]; byValue: Map<string, Place> } {
  const byValue = new Map<string, Place>()
  const options: MenuOption[] = [
    { value: 'auto', label: 'Let Tether choose', detail: 'Picks the session and stack for the task', group: 'Tether' },
    { value: 'default', label: defaultName, detail: sessions.some(session => session.name === DEFAULT_SESSION) ? 'The default place' : 'The default place. Tether makes the General session.', group: 'Tether' },
  ]
  for (const place of places(sessions)) {
    const value = placeValue(place)
    byValue.set(value, place)
    options.push({ value, label: place.groupName || 'Tabs in no stack', detail: place.groupName ? undefined : place.sessionName, group: place.sessionName, search: placeName(place) })
  }
  return { options, byValue }
}

/** ⌘K or ⌥Space: go to a pane or session, run a command, or find a backlog item. Each command row shows its key, so the menu teaches the keys. */
export function CommandMenu({ sessions, seen, selected, slots, commands, onPane, onSession, onClose, groups, stacks, backlog, work, create }: Props) {
  // Opening the menu keeps the Forward steps, so its Forward command can use them.
  const modal = useModal(true, onClose, undefined, { keepForward: true })
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [picking, setPicking] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const { text: backlogText, failed: backlogFailed } = useBacklogText(backlog?.api)
  const now = useNow()
  // Each item with its project, so a search for a project name finds its items.
  const backlogItems = useMemo(() => backlogText === null ? [] : projectGroups(parseBacklog(backlogText)).flatMap(group => group.items.map(item => ({ item, project: group.name }))), [backlogText])
  // The same index and scores as the sidebar search, built once while the menu is open.
  const index = useMemo(() => searchIndex(sessions, [], groups ?? { groups: [], groupOf: {} }, stacks)[0], [sessions, groups, stacks])
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
    // Panes rank by the sidebar's scores: a session row by its own fields, a pane row by its tab and session.
    const wordList = searchWords(words)
    const found = matchSessions(index, wordList)
    const tabScores = new Map([...found.values()].flatMap(match => [...match.tabScores]))
    const paneScore = (row: Row) => row.id.startsWith('session:') ? found.get(row.id.slice('session:'.length))?.own ?? null : tabScores.get(row.id) ?? null
    const ranked = (list: Row[], score: (row: Row) => number | null) => list
      .map(row => ({ row, score: score(row) }))
      .filter((item): item is { row: Row; score: number } => item.score !== null)
      .sort((a, b) => b.score - a.score)
      .map(item => item.row)
    // Backlog items match on every word, like the backlog's own search. Open items rank above done ones.
    const hits: BacklogHit[] = !backlog ? [] : backlogItems.flatMap(({ item, project }) => {
      const score = item.ref ? queryScore(item, words, project) : null
      return score === null ? [] : [{ ref: item.ref!, text: item.text, project, done: item.done, score: score - (item.done ? 100 : 0) }]
    }).sort((a, b) => b.score - a.score)
    const backlogRows: Row[] = hits.slice(0, BACKLOG_ROWS).map(hit => ({ id: `backlog:${hit.ref}`, group: 'Backlog', label: hit.text, marked: wordList.filter(word => !/^b\d+$/.test(word)), hint: [hit.ref, hit.project || 'Inbox', hit.done && 'Done'].filter(Boolean).join(' · '), done: hit.done, run: () => backlog!.open(hit.ref) }))
    if (backlog && backlogText !== null) backlogRows.push({ id: 'backlog:search', group: 'Backlog', label: hits.length > BACKLOG_ROWS ? `See all ${hits.length} in the backlog` : `Search the backlog for “${words}”`, run: () => backlog.open(words) })
    // Work items on the board match like the board's own search. Done items rank below open ones.
    const workHits = (work?.board?.items ?? []).filter(item => onBoard(item, now)).flatMap(item => {
      const score = workScore(item, wordList)
      return score === null ? [] : [{ item, score: score - (item.stage === 'done' ? 100 : 0) }]
    }).sort((a, b) => b.score - a.score)
    const workRows: Row[] = workHits.slice(0, WORK_ROWS).map(({ item }) => ({ id: `work:${item.id}`, group: 'Work', label: item.title, marked: markWords(item, wordList).title,
      hint: [item.id, item.project, STAGE_LABEL[item.stage]].join(' · '), done: item.stage === 'done', run: () => work!.open(words, item.id) }))
    // No match: no "Search the work board" row. It would find nothing, and Enter would pick it over the backlog hits.
    if (work && workHits.length) workRows.push({ id: 'work:search', group: 'Work', label: workHits.length > WORK_ROWS ? `See all ${workHits.length} on the work board` : `Search the work board for “${words}”`, run: () => work.open(words) })
    const createRows: Row[] = !create ? [] : [
      { id: 'create:backlog', group: 'Create', label: `Add “${words}” to the backlog`, run: () => create.onBacklog(words) },
      { id: 'create:tab', group: 'Create', label: `Start a new tab for “${words}”`, hint: 'Tether picks the place', run: () => create.onTab(words, { kind: 'auto' }) },
      { id: 'create:place', group: 'Create', label: `Start a new tab for “${words}” in…`, hint: 'Choose the session and stack', keep: true, run: () => setPicking(true) },
    ]
    return [...ranked(panes, paneScore), ...ranked(commandRows, row => scoreWords([field(2, row.label)], wordList)), ...workRows, ...backlogRows, ...createRows]
  }, [sessions, seen, selected, slots, commands, onPane, onSession, query, backlog, backlogItems, backlogText, index, work, now, create])
  const at = Math.min(active, Math.max(0, rows.length - 1))
  const words = query.trim()
  const pick = (row: Row | undefined) => { if (!row) return; if (!row.keep) onClose(); row.run() }
  const { options: placeRows, byValue } = useMemo(() => placeOptions(sessions), [sessions])
  const choose = (value: string) => {
    const place = byValue.get(value)
    const choice: Choice = value === 'default' ? { kind: 'default' } : place ? { kind: 'place', place } : { kind: 'auto' }
    onClose()
    create?.onTab(words, choice)
  }
  const move = (to: number) => {
    const next = (to + rows.length) % rows.length
    setActive(next)
    document.getElementById(`command-${next}`)?.scrollIntoView({ block: 'nearest' })
  }
  const icon = (row: Row) => row.group === 'Panes' ? (row.dot ? <span className={`state-dot ${row.dot}`} /> : <SquareTerminal size={16} />)
    : row.group === 'Work' ? (row.id === 'work:search' ? <Search size={16} /> : row.done ? <CircleCheck size={16} /> : <SquareKanban size={16} />)
    : row.group === 'Backlog' ? (row.id === 'backlog:search' ? <Search size={16} /> : row.done ? <CircleCheck size={16} /> : <ClipboardList size={16} />)
    : row.group === 'Create' ? (row.id === 'create:backlog' ? <ListPlus size={16} /> : row.id === 'create:place' ? <MapPin size={16} /> : <SquarePlus size={16} />)
    : <Zap size={16} />
  const searching = !!words
  const matched = rows.some(row => row.group !== 'Create' && row.id !== 'backlog:search' && row.id !== 'work:search')
  const placeholder = backlog && work ? 'Search panes, commands, work and the backlog' : backlog ? 'Search panes, commands and the backlog' : 'Go to a pane or run a command'
  const wordList = searchWords(words)
  return <dialog {...modal} className="sheet command-menu" aria-label="Search">
    <label className="command-search"><Search size={20} aria-hidden="true" />
      <input ref={input} autoFocus role="combobox" aria-expanded="true" aria-controls="command-list" aria-autocomplete="list" aria-activedescendant={rows.length ? `command-${at}` : undefined}
        aria-label={placeholder} placeholder={placeholder} value={query} autoComplete="off" spellCheck={false}
        onChange={event => { setQuery(event.target.value); setActive(0) }}
        onKeyDown={event => {
          if (!rows.length) return
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); move(at + (event.key === 'ArrowDown' ? 1 : -1)) }
          else if (event.key === 'Home' && event.ctrlKey) { event.preventDefault(); move(0) }
          else if (event.key === 'End' && event.ctrlKey) { event.preventDefault(); move(rows.length - 1) }
          else if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); pick(rows[at]) }
          // → at the end of the text, or Tab, on a "Start a new tab" row opens the place picker.
          else if (create && rows[at]?.id.startsWith('create:') && rows[at].id !== 'create:backlog' && !event.shiftKey && (event.key === 'Tab' || (event.key === 'ArrowRight' && event.currentTarget.selectionStart === event.currentTarget.value.length))) { event.preventDefault(); setPicking(true) }
        }} />
    </label>
    {picking && create && words && <div className="command-place">
      <span className="command-place-label" id="command-place-label">Start “{words}” in</span>
      <SelectMenu openOnMount value="" options={placeRows} onChange={choose} label="Place for the new tab" className="sm-field command-place-field" minWidth={300}
        searchLabel="Search sessions and stacks" emptyText="No session or stack matches." triggerLabel="Choose a place" icon={<MapPin size={13} aria-hidden="true" />}
        onClosed={() => { setPicking(false); requestAnimationFrame(() => input.current?.focus()) }} />
    </div>}
    <div id="command-list" role="listbox" tabIndex={-1} aria-label="Results" className="command-list">
      {searching && !matched && rows.length > 0 && <p className="command-empty command-none">Nothing matches “{words}”.</p>}
      {rows.map((row, index) => <div key={row.id} role="presentation">
        {(index === 0 || rows[index - 1].group !== row.group) && <div className="command-group" role="presentation">{row.group}</div>}
        <div id={`command-${index}`} role="option" aria-selected={index === at} className={`command-row${index === at ? ' active' : ''}${row.done ? ' done' : ''}`}
          onMouseMove={() => { if (index !== at) setActive(index) }} onMouseDown={event => event.preventDefault()} onClick={() => pick(row)}>
          <span className="command-icon" aria-hidden="true">{icon(row)}</span>
          <span className="command-label">{row.group === 'Create' || row.id.endsWith(':search') ? row.label : <Marked text={row.label} words={row.marked ?? wordList} />}</span>
          {row.hint && <span className="command-hint">{row.hint}</span>}
          {row.key ? <kbd>{keyLabel(row.key)}</kbd> : <span className="command-type" aria-hidden="true">{TYPE[row.group]}</span>}
        </div>
      </div>)}
      {!rows.length && <p className="command-empty">Nothing matches “{query.trim()}”.</p>}
      {searching && work && !work.board && <p className="command-note" role="status">{work.error ? 'Work items did not load.' : 'Searching work items…'}</p>}
      {searching && backlog && backlogText === null && <p className="command-note" role="status">{backlogFailed || 'Searching the backlog…'}</p>}
    </div>
    <div className="command-foot" aria-hidden="true">
      <span><kbd>↑</kbd><kbd>↓</kbd> Move</span>
      <span><kbd><CornerDownLeft size={11} /></kbd> {rows[at]?.group === 'Create' ? 'Run' : 'Open'}</span>
      {rows[at]?.id.startsWith('create:') && rows[at].id !== 'create:backlog' && <span><kbd>→</kbd> Choose place</span>}
      <span><kbd>Esc</kbd> Close</span>
    </div>
  </dialog>
}
