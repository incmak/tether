import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { Inbox as InboxIcon, X } from 'lucide-react'
import type { Api, Pane } from './api'
import { AgentIcon, agentNames, paneTitle, shownIndex } from './Sidebar'
import type { Session } from './Sidebar'
import { KIND_LABEL, MOD, age } from './needs'
import { AnswerActions } from './answers'
import { useAnswers } from './useAnswers'
import type { InboxItem } from './needs'

// Desktop "Needs you" list: every pane, in every session, that waits on you. Newest first.
// Permission requests and menus can be answered here, so the slots do not move.

const rowTitle = (pane: Pane, sessions: Session[]) => {
  const session = sessions.find(item => item.id === pane.tmux_session)
  // The same pane name as the sidebar: an unnamed pane gets its number, "Shell 0.2".
  const name = [paneTitle(pane), shownIndex(pane)].filter(Boolean).join(' ')
  if (!session) return name
  return session.panes.length > 1 ? `${session.name} · ${name}` : session.name
}

interface InboxProps {
  api: Api
  items: InboxItem[]
  sessions: Session[]
  selected?: string
  open: boolean
  onToggle: () => void
  onOpen: (pane: Pane) => void
  onUpdate: () => void
  /** Decisions on work items, shown above the tabs that wait for input. */
  decisions?: ReactNode
  decisionCount?: number
}

/** The button in the workspace bar that shows and hides the list. */
export function InboxButton({ count, open, onToggle }: { count: number; open: boolean; onToggle: () => void }) {
  const label = `Needs you${count ? `, ${count}` : ''} (${MOD}I)`
  return <button type="button" className="icon-button inbox-toggle" aria-label={label} title={label} aria-pressed={open} onClick={onToggle}>
    <InboxIcon size={17} />{count > 0 && <span className="inbox-badge" aria-hidden="true">{count > 99 ? '99+' : count}</span>}
  </button>
}

const focusRow = (id: string) => document.querySelector<HTMLButtonElement>(`.inbox [data-pane="${CSS.escape(id)}"] .inbox-open`)?.focus()

export function Inbox({ api, items, sessions, selected, open, onToggle, onOpen, onUpdate, decisions, decisionCount = 0 }: InboxProps) {
  const { busy, errors, answer, fail, onAnswerKey } = useAnswers({ api, items, active: open, onUpdate, focusRow })
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 30000); return () => clearInterval(timer) }, [])
  // ⌘I (Ctrl+I) shows or hides the list. ⌘J (Ctrl+J) opens the next pane that needs you. Letters are read from event.code.
  const keys = useRef({ items, selected, open, onToggle, onOpen })
  useEffect(() => { keys.current = { items, selected, open, onToggle, onOpen } })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || document.querySelector('dialog[open]')) return
      const current = keys.current
      if (event.code === 'KeyI') { event.preventDefault(); current.onToggle() }
      else if (event.code === 'KeyJ') {
        event.preventDefault()
        const list = current.items
        if (!list.length) return
        const at = list.findIndex(item => item.pane.id === current.selected)
        const next = list[(at + 1) % list.length]
        current.onOpen(next.pane)
        if (current.open) requestAnimationFrame(() => focusRow(next.pane.id))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Y, N and 1-3 answer the row that has focus. Arrow keys move between rows.
  const onRowKey = (event: ReactKeyboardEvent<HTMLLIElement>, item: InboxItem) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return
    const rows = [...event.currentTarget.parentElement!.querySelectorAll<HTMLButtonElement>('.inbox-open')]
    const at = rows.indexOf(event.currentTarget.querySelector('.inbox-open')!)
    if (event.code === 'ArrowDown' || event.code === 'ArrowUp') {
      event.preventDefault(); event.stopPropagation()
      rows[Math.min(rows.length - 1, Math.max(0, at + (event.code === 'ArrowDown' ? 1 : -1)))]?.focus()
      return
    }
    onAnswerKey(event, item)
  }

  if (!open) return null
  return <aside className="inbox" aria-labelledby="inbox-title">
    <div className="inbox-head">
      <h2 id="inbox-title">Needs you</h2>
      <span className="inbox-count">{items.length + decisionCount || ''}</span>
      <span className="workspace-spacer" />
      <button type="button" className="inbox-next" disabled={!items.length} title={`Open the next pane that needs you (${MOD}J)`}
        onClick={() => { const at = items.findIndex(item => item.pane.id === selected); const next = items[(at + 1) % items.length]; if (next) onOpen(next.pane) }}>Next <kbd>{MOD}J</kbd></button>
      <button type="button" className="icon-button" aria-label={`Hide Needs you (${MOD}I)`} title={`Hide (${MOD}I)`} onClick={onToggle}><X size={16} /></button>
    </div>
    <div className="inbox-scroll">
    {decisions}
    {decisionCount > 0 && items.length > 0 && <h3 className="inbox-subhead">Tabs waiting for input · {items.length}</h3>}
    {items.length ? <ul className="inbox-list" aria-label="Panes that need you">
      {items.map(item => {
        const { pane } = item
        const title = rowTitle(pane, sessions)
        const waiting = busy === pane.id
        return <li key={`${pane.id}:${item.kind}`} data-pane={pane.id} className={`inbox-row ${item.kind}`} aria-current={pane.id === selected ? 'true' : undefined} onKeyDown={event => onRowKey(event, item)}>
          <button type="button" className="inbox-open" onClick={() => onOpen(pane)}
            aria-label={`${KIND_LABEL[item.kind]}: ${title}, ${agentNames[pane.agent]}. ${item.text}. ${age(item.at, now)}.`}>
            <span className="inbox-kind"><span className="inbox-dot" aria-hidden="true" />{KIND_LABEL[item.kind]}<span className="inbox-age">{age(item.at, now)}</span></span>
            <span className="inbox-title">{title}</span>
            <span className="inbox-meta"><AgentIcon agent={pane.agent} icon={pane.pane_icon} size={13} />{agentNames[pane.agent]}</span>
            {item.text && <span className="inbox-text" title={item.text}>{item.text}</span>}
          </button>
          <AnswerActions api={api} item={item} label={title} waiting={waiting} onAnswer={answer} onFail={fail} />
          {errors[pane.id] && <p className="inbox-error" role="alert">{errors[pane.id]}</p>}
        </li>
      })}
    </ul> : !decisionCount && <p className="inbox-empty">Nothing needs you.</p>}
    </div>
  </aside>
}
