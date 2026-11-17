import { useCallback, useEffect, useRef, useState } from 'react'
import { Check } from 'lucide-react'
import type { AskBridge, ChoiceState } from './api'
import { rovingRows, useRovingStop } from './rovingRows'
import { pickRowKey } from './pickKeys'
import './ChoiceExtras.css'

// The tabs and the filter text the agent shows above a list. Read only: the keys below change them.
export function ChoiceMeta({ choice }: { choice: ChoiceState }) {
  if (!choice.tabs?.length && choice.filter === null) return null
  return <div className="choice-meta">
    {!!choice.tabs?.length && <ul className="choice-tabs" aria-label="Tabs">{choice.tabs.map(tab => <li key={tab.label} className={tab.active ? 'active' : ''} aria-current={tab.active || undefined}>{tab.label}</li>)}</ul>}
    {choice.filter !== null && <p className="choice-filter"><span>Filter</span>{choice.filter ? <code>{choice.filter}</code> : <em>empty</em>}</p>}
  </div>
}

const more = (count: number, arrow: string) => count === -1 ? `${arrow} more` : `${arrow} ${count} more`

export type PickKey = 'Enter' | 'Up' | 'Down' | 'Space'
export interface PickRow { text: string; detail?: string; selected?: boolean; checked?: boolean; disabled?: boolean }

// A list the agent draws with a cursor (a picker, or a numbered menu). With `onPick`, every row is a button: a click
// or Enter chooses it (multi: ticks it), and "more" rows show the rows past the screen. The server moves the
// cursor and checks the row before it presses a key. Without `onPick` the list is read only.
// `labels`: the key of each row (pickRowKey) as the server's own parser reads it. The server refuses when the list on screen
// differs, so a row index never lands on a look-alike row.
export function PickList({ rows, labels, label, more_above, more_below, multi, disabled, onPick }: { rows: PickRow[]; labels?: string[]; label: string; more_above: number; more_below: number; multi?: boolean; disabled?: boolean; onPick?: (row: number, labels: string[], then: PickKey) => Promise<unknown> }) {
  const [busy, setBusy] = useState<string>('')
  const names = labels ?? rows.map(pickRowKey)
  const roving = useRovingStop()
  const live = !!onPick && names.length === rows.length && rows.length > 0
  const off = disabled || !!busy
  const pick = async (id: string, row: number, then: PickKey) => {
    if (!live || off) return
    setBusy(id)
    try { await onPick!(row, names, then) } finally { setBusy('') }
  }
  const marked = Math.max(0, rows.findIndex(row => row.selected))
  const keys = [...more_above !== 0 ? ['more-↑'] : [], ...rows.map((_, at) => `row-${at}`), ...more_below !== 0 ? ['more-↓'] : []]
  const tab = (key: string) => roving.tabFor(key, keys, `row-${marked}`)
  const moreRow = (count: number, arrow: '↑' | '↓') => {
    const text = count === -1 ? `Show more ${arrow === '↑' ? 'above' : 'below'}` : `Show ${count} more ${arrow === '↑' ? 'above' : 'below'}`
    const id = `more-${arrow}`
    return <li className="picker-more">{live
      ? <button type="button" data-row={id} tabIndex={tab(id)} aria-disabled={off || undefined} aria-busy={busy === id || undefined}
          onClick={() => void pick(id, arrow === '↑' ? 0 : rows.length - 1, arrow === '↑' ? 'Up' : 'Down')}>{busy === id ? 'Loading…' : `${arrow} ${text}`}</button>
      : more(count, arrow)}</li>
  }
  return <ul className={live ? 'picker-list pick-list' : 'picker-list'} aria-label={label} onKeyDown={live ? rovingRows : undefined} onFocus={live ? roving.onFocus : undefined}>
    {more_above !== 0 && moreRow(more_above, '↑')}
    {rows.map((row, at) => {
      const body = <>{row.checked !== undefined && <Box checked={row.checked} multi={!!multi} />}
        <span className="choice-item-text">{row.text}{row.detail && <small>{row.detail}</small>}</span>
        {busy === `row-${at}` && <span className="pick-busy" aria-hidden="true">…</span>}</>
      const cls = [row.selected ? 'current' : '', row.disabled ? 'disabled' : ''].filter(Boolean).join(' ')
      return <li key={at} className={live ? undefined : cls} aria-current={!live && row.selected || undefined}>{live
        ? <button type="button" data-row={`row-${at}`} tabIndex={tab(`row-${at}`)} className={cls || undefined} aria-current={row.selected || undefined}
            aria-disabled={off || row.disabled || undefined} aria-busy={busy === `row-${at}` || undefined}
            onClick={() => { if (!row.disabled) void pick(`row-${at}`, at, multi ? 'Space' : 'Enter') }}>{body}</button>
        : body}</li>
    })}
    {more_below !== 0 && moreRow(more_below, '↓')}
  </ul>
}

// The server's choice record for a picker: the mark, the ticked boxes and the hidden rows.
export function ChoiceList({ choice, label, labels, disabled, onPick }: { choice: ChoiceState; label: string; labels?: string[]; disabled?: boolean; onPick?: (row: number, labels: string[], then: PickKey) => Promise<unknown> }) {
  return <PickList rows={choice.items} labels={labels} label={label} more_above={choice.more_above} more_below={choice.more_below} multi={choice.selection === 'multi'} disabled={disabled} onPick={onPick} />
}

// decorative: the form's real input already tells assistive tech its state.
export function Box({ checked, multi, decorative }: { checked: boolean; multi: boolean; decorative?: boolean }) {
  return <span className={`choice-box ${multi ? 'multi' : 'single'} ${checked ? 'on' : ''}`} {...decorative ? { 'aria-hidden': true } : { role: 'img', 'aria-label': checked ? 'Selected' : 'Not selected' }}>{checked && <Check size={12} strokeWidth={3} />}</span>
}

type Answers = Record<string, string | string[]>

function secondsLeft(expires: number) { return Math.max(0, Math.round(expires - Date.now() / 1000)) }

// Claude's question, sent to the phone by the ask bridge before the terminal draws it. Answers go back as data, not keys.
// "Answer in the terminal" gives the question back at once. The question stays here while the person uses the form:
// each tap or keystroke renews the server's deadline. Showing the form does not: only use proves a person is answering.
export function AskForm({ ask, disabled, onAnswer, onEngage }: { ask: AskBridge; disabled: boolean; onAnswer: (answers: Answers | null) => Promise<string | null>; onEngage?: () => Promise<number | null> }) {
  const [picked, setPicked] = useState<Record<number, string[]>>({})
  const [other, setOther] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState<'send' | 'back' | ''>('')
  const [failed, setFailed] = useState('')
  // The newest deadline: from the attention record, or sooner from an engage reply. Never moves back.
  const [renewed, setRenewed] = useState(0)
  const expires = Math.max(ask.expires, renewed)
  const [left, setLeft] = useState(() => secondsLeft(expires))
  const lastEngage = useRef(0)
  const [identity, setIdentity] = useState({ id: ask.id, nonce: ask.nonce })
  if (identity.id !== ask.id || identity.nonce !== ask.nonce) {
    setIdentity({ id: ask.id, nonce: ask.nonce }); setPicked({}); setOther({}); setFailed(''); setRenewed(0)
  }
  useEffect(() => { lastEngage.current = 0 }, [ask.id, ask.nonce])
  // eslint-disable-next-line react-hooks/set-state-in-effect -- Resync the clock immediately when the deadline changes instead of waiting up to one second for the interval.
  useEffect(() => { const timer = setInterval(() => setLeft(secondsLeft(expires)), 1000); setLeft(secondsLeft(expires)); return () => clearInterval(timer) }, [expires])
  // At most one call per 5 s. A hidden page never renews: a phone left on a desk must not hold the question.
  const engage = useCallback(() => {  // at most one call per 5 s
    if (!onEngage || document.visibilityState !== 'visible' || Date.now() - lastEngage.current < 5000) return
    lastEngage.current = Date.now()
    void onEngage().then(next => { if (next) setRenewed(old => Math.max(old, next)) })
  }, [onEngage])
  const answerOf = (at: number) => {
    const typed = other[at]?.trim()
    const labels = picked[at] ?? []
    if (ask.questions[at].multiSelect) return typed ? [...labels, typed] : labels
    return typed || labels[0] || ''
  }
  const missing = ask.questions.findIndex((_, at) => { const value = answerOf(at); return Array.isArray(value) ? !value.length : !value })
  const toggle = (at: number, label: string, multi: boolean) => {
    setPicked(old => { const now = old[at] ?? []; return { ...old, [at]: multi ? now.includes(label) ? now.filter(item => item !== label) : [...now, label] : [label] } })
    if (!multi) setOther(old => ({ ...old, [at]: '' }))
  }
  const send = async (answers: Answers | null) => {
    if (busy) return
    setBusy(answers ? 'send' : 'back'); setFailed('')
    try { setFailed(await onAnswer(answers) ?? '') } finally { setBusy('') }
  }
  const submit = () => { if (missing === -1) void send(Object.fromEntries(ask.questions.map((item, at) => [item.question, answerOf(at)]))) }
  const off = disabled || !!busy || left === 0
  return <form className="ask-form" onSubmit={event => { event.preventDefault(); submit() }} onPointerDown={engage} onKeyDown={engage} onInput={engage}>
    {ask.questions.map((item, at) => {
      const multi = !!item.multiSelect
      const name = `ask-${ask.id}-${at}`
      return <fieldset key={at} className="ask-form-question">
        <legend>{ask.questions.length > 1 && <span className="ask-header">Question {at + 1} of {ask.questions.length}{item.header ? ` · ${item.header}` : ''}</span>}
          {ask.questions.length === 1 && item.header && <span className="ask-header">{item.header}</span>}
          <span className="ask-question">{item.question}</span>
          {multi && <span className="ask-form-hint">Choose one or more.</span>}</legend>
        {item.options.map(option => {
          const on = (picked[at] ?? []).includes(option.label)
          return <label key={option.label} className={`ask-form-option ${on ? 'on' : ''}`}>
            <input type={multi ? 'checkbox' : 'radio'} name={name} checked={on} disabled={off} onChange={() => toggle(at, option.label, multi)} />
            <Box checked={on} multi={multi} decorative />
            <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span></label>
        })}
        <label className="ask-form-other"><span>{multi ? 'Add your own answer' : 'Or write your own answer'}</span>
          <input type="text" value={other[at] ?? ''} disabled={off} maxLength={2000} onChange={event => { const value = event.target.value; setOther(old => ({ ...old, [at]: value })); if (!multi && value.trim()) setPicked(old => ({ ...old, [at]: [] })) }} /></label>
      </fieldset>
    })}
    <p className="ask-form-time" role="timer" aria-live="off">{left === 0 ? 'The question is back in the terminal.' : onEngage ? renewed ? `Stays here while you answer. Back to the terminal after ${ask.idle ?? 60} s without a tap (${left} s left).` : `Back to the terminal in ${left} s unless you tap here. Each tap keeps it here ${ask.idle ?? 60} s more.` : `The question goes back to the terminal in ${left} s.`}</p>
    {failed && <p className="info-error" role="alert">{failed}</p>}
    <div className="ask-form-actions">
      <button type="submit" className="primary" disabled={off || missing !== -1}>{busy === 'send' ? 'Sending…' : ask.questions.length > 1 ? 'Send answers' : 'Send answer'}</button>
      <button type="button" disabled={off} onClick={() => void send(null)}>{busy === 'back' ? 'Sending…' : 'Answer in the terminal'}</button>
    </div>
    {missing !== -1 && ask.questions.length > 1 && <p className="ask-form-hint">Answer question {missing + 1} to send.</p>}
  </form>
}

// Tether's terminal view hides the form, but the terminal itself shows only a spinner while the bridge waits.
// This bar says where the question is and gives it back to the terminal in one tap.
export function AskBar({ agent, disabled, onOpen, onTerminal }: { agent: string; disabled: boolean; onOpen: () => void; onTerminal: () => Promise<string | null> }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState('')
  return <div className="ask-bar" role="status">
    <p>{agent} asks a question. It is waiting in the chat view, not in the terminal.</p>
    <div className="ask-form-actions">
      <button type="button" className="primary" onClick={onOpen}>Answer in chat</button>
      <button type="button" disabled={disabled || busy} onClick={() => { setBusy(true); setFailed(''); void onTerminal().then(error => setFailed(error ?? '')).finally(() => setBusy(false)) }}>{busy ? 'Sending…' : 'Show in terminal'}</button>
    </div>
    {failed && <p className="info-error" role="alert">{failed}</p>}
  </div>
}
