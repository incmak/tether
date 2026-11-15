// A Claude Code slash-command dialog (/mcp, /config, /resume, /status ...), read from the screen by server/claude_dialog.py.
// The card drives the real dialog: every tap is one action the server checks against the dialog's own key hints.
import { useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import type { ClaudeDialog, DialogAction, DialogBlock, DialogHint, DialogItem } from './api'
import { Linked } from './Messages'

const MARKS: Record<string, [string, string]> = { '✔': ['ok', 'On'], '✓': ['ok', 'On'], '⚠': ['warn', 'Needs attention'], '✘': ['bad', 'Failed'], '✗': ['bad', 'Failed'], '×': ['bad', 'Failed'],
  '◯': ['off', 'Not selected'], '○': ['off', 'Not selected'], '☐': ['off', 'Not selected'], '◉': ['ok', 'Selected'], '●': ['ok', 'Selected'], '☑': ['ok', 'Selected'] }
const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)
const ARROWS = ['Up', 'Down', 'Left', 'Right']

export function DialogCard({ dialog, disabled, onAction }: { dialog: ClaudeDialog; disabled: boolean; onAction: (action: DialogAction) => Promise<string | null> }) {
  const [busy, setBusy] = useState('')
  const [failed, setFailed] = useState('')
  const box = dialog.blocks.find(block => block.type === 'search')
  const [query, setQuery] = useState(box?.type === 'search' ? box.value : '')
  // The terminal's box is the truth: when its text changes there (a key in the terminal, a cleared filter), show that.
  const [boxValue, setBoxValue] = useState(box?.type === 'search' ? box.value : '')
  if (box?.type === 'search' && box.value !== boxValue) { setBoxValue(box.value); setQuery(box.value) }
  const act = async (id: string, action: DialogAction) => {
    if (busy || disabled) return
    setBusy(id); setFailed('')
    try { setFailed(await onAction(action) ?? '') } finally { setBusy('') }
  }
  const hasList = dialog.blocks.some(block => block.type === 'list')
  const hasScale = dialog.blocks.some(block => block.type === 'scale')
  const canType = dialog.hints.some(hint => hint.keys.some(key => key.label === 'Type' || key.key === '/'))
  const tabRow = useRef<HTMLDivElement>(null)
  useEffect(() => { tabRow.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }, [dialog.tab])
  const list = useRef<HTMLUListElement>(null)
  const current = dialog.blocks.flatMap(block => block.type === 'list' ? block.items : []).find(item => 'selected' in item && item.selected)
  const currentText = current && 'text' in current ? current.text : ''
  useEffect(() => { list.current?.querySelector('[aria-current="true"]')?.scrollIntoView({ block: 'nearest' }) }, [currentText])

  // One button per key the dialog offers. Arrows that a tab row, a slider or the list rows already cover get no button.
  const buttons: { id: string; key: string; label: string; kbd: string; primary: boolean; close: boolean }[] = []
  const add = (hint: DialogHint) => {
    const keys = hint.keys.filter(key => key.key && key.key !== '/' || key.key === '/' && !box)
    const arrows = keys.filter(key => ARROWS.includes(key.key))
    if (arrows.length && arrows.length === keys.filter(key => key.key !== 'Tab').length) {
      if (/tab|switch/i.test(hint.effect) && dialog.tabs || hasScale && arrows.some(key => key.key === 'Left')) return
      for (const key of arrows) if (!buttons.some(button => button.key === key.key)) buttons.push({ id: key.key, key: key.key, label: key.label, kbd: '', primary: false, close: false })
      return
    }
    const key = keys.find(key => !ARROWS.includes(key.key)) ?? keys[0]
    if (!key || buttons.some(button => button.key === key.key)) return
    buttons.push({ id: key.key, key: key.key, label: capital(hint.effect), kbd: key.label, primary: key.key === 'Enter', close: key.key === 'Escape' })
  }
  dialog.hints.forEach(add)
  if (hasList) for (const [key, label] of [['Up', '↑'], ['Down', '↓']]) if (!buttons.some(button => button.key === key)) buttons.unshift({ id: key, key, label, kbd: '', primary: false, close: false })
  if (!buttons.some(button => button.close)) buttons.push({ id: 'Escape', key: 'Escape', label: 'Close', kbd: 'Esc', primary: false, close: true })
  buttons.sort((a, b) => Number(a.close) - Number(b.close))

  const row = (item: DialogItem, at: number) => {
    const mark = item.mark ? MARKS[item.mark] : undefined
    return <li key={at}>
      <button type="button" className={item.selected ? 'current' : ''} aria-current={item.selected || undefined} disabled={disabled || !!busy}
        onClick={() => void act(`row-${at}`, { action: 'row', row: at, label: item.text })}>
        {item.mark && <span className={`dlg-mark ${mark?.[0] ?? ''}`} role="img" aria-label={mark?.[1] ?? item.mark}>{item.mark}</span>}
        <span className="dlg-row-text"><strong>{item.text}</strong>{item.detail && <small>{item.detail}</small>}</span>
        {item.value && <span className="dlg-row-value">{item.value}</span>}
      </button>
    </li>
  }
  const render = (block: DialogBlock, at: number) => {
    if (block.type === 'heading') return <h3 key={at} className="dlg-heading">{block.text}</h3>
    if (block.type === 'text') return block.mono
      ? <pre key={at} className="panel-pre dlg-pre" tabIndex={0} aria-label="Text as the terminal shows it">{block.lines.join('\n')}</pre>
      : <p key={at} className={block.dim ? 'dlg-text dim' : 'dlg-text'}><Linked text={block.lines.join(' ')} /></p>
    if (block.type === 'facts') return <dl key={at} className="dlg-facts">{block.rows.map((fact, index) => <div key={index}><dt>{fact.label}</dt><dd><Linked text={fact.value} /></dd></div>)}</dl>
    if (block.type === 'meter') return <div key={at} className="dlg-meter"><progress max={100} value={block.percent} aria-label={block.text} /><span>{block.text}</span></div>
    if (block.type === 'tabs') return <p key={at} className="dlg-subtabs" aria-label="Views">{block.tabs.map(tab => <span key={tab} className={tab === block.tab ? 'current' : ''} aria-current={tab === block.tab || undefined}>{tab}</span>)}</p>
    if (block.type === 'scale') return <div key={at} className="dlg-scale">
      <div role="radiogroup" aria-label={dialog.title}>
        {block.options.map(option => <button key={option} type="button" role="radio" aria-checked={option === block.value} disabled={disabled || !!busy}
          onClick={() => void act(`scale-${option}`, { action: 'scale', value: option })}>{busy === `scale-${option}` ? '…' : option}</button>)}
      </div>
      {block.ends.length === 2 && <p className="dlg-scale-ends" aria-hidden="true"><span>{block.ends[0]}</span><span>{block.ends[1]}</span></p>}
    </div>
    if (block.type === 'search') {
      // No native form submit: the status panel on a phone cancels a click's default action.
      const search = () => { if (canType && query.trim() !== block.value) void act('filter', { action: 'filter', text: query.trim() }) }
      return <div key={at} className="dlg-search" role="search">
        <label><Search size={16} aria-hidden="true" />
          <input type="search" aria-label={(block.placeholder || 'Search').replace(/…$/, '')} enterKeyHint="search" maxLength={80} autoCapitalize="none" spellCheck={false} placeholder={block.placeholder || 'Search…'} value={query} disabled={disabled || !canType}
            onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); search() } }} /></label>
        <button type="button" disabled={disabled || !canType || !!busy || query.trim() === block.value} onClick={search}>{busy === 'filter' ? 'Searching…' : 'Search'}</button>
      </div>
    }
    let index = dialog.blocks.slice(0, at).reduce((count, earlier) => count + (earlier.type === 'list' ? earlier.items.filter(item => 'text' in item).length : 0), 0)
    return <ul key={at} ref={list} className="dlg-list" aria-label={dialog.title}>
      {block.more_above !== 0 && <li className="dlg-more">{block.more_above > 0 ? `↑ ${block.more_above} more above` : '↑ More above'}</li>}
      {block.items.map((item, position) => 'heading' in item ? <li key={`h${position}`} className="dlg-group">{item.heading}</li> : row(item, index++))}
      {block.more_below !== 0 && <li className="dlg-more">{block.more_below > 0 ? `↓ ${block.more_below} more below` : '↓ More below'}</li>}
    </ul>
  }
  // On a phone the bottom bar hides while a text box has the focus. A press on a button would move the focus, the bar
  // would return, and the button would move away under the finger. So a press on a button leaves the focus in the box.
  return <div className="dlg-card" onMouseDown={event => { if (document.activeElement instanceof HTMLInputElement && (event.target as Element).closest('button')) event.preventDefault() }}>
    {dialog.tabs && <div ref={tabRow} className="perm-tabs" role="tablist" aria-label={`${dialog.title} tabs`}>
      {dialog.tabs.map(tab => <button key={tab} type="button" role="tab" aria-selected={tab === dialog.tab} aria-busy={busy === `tab-${tab}` || undefined} disabled={disabled || !!busy}
        onClick={() => { if (tab !== dialog.tab) void act(`tab-${tab}`, { action: 'tab', tab }) }}>{tab}</button>)}
    </div>}
    {dialog.note && <p className="dlg-text dim">{dialog.note}</p>}
    {dialog.blocks.map(render)}
    {!dialog.blocks.length && <p className="dlg-text dim">Loading…</p>}
    {hasList && <p className="dlg-text dim dlg-help">Tap a row to move the mark to it. The buttons act on the marked row.</p>}
    <div className="info-actions dlg-actions" role="group" aria-label="Dialog keys">
      {buttons.map(button => <button key={button.id} type="button" className={button.primary ? 'primary' : ''} disabled={disabled || !!busy} aria-busy={busy === button.id || undefined}
        aria-label={button.kbd ? undefined : ({ Up: 'Move up', Down: 'Move down', Left: 'Left', Right: 'Right' } as Record<string, string>)[button.key]}
        onClick={() => void act(button.id, { action: 'key', key: button.key })}><span>{button.label}</span>{button.kbd && <kbd>{button.kbd}</kbd>}</button>)}
    </div>
    {failed && <p className="info-error dlg-error" role="alert">{failed}</p>}
  </div>
}
