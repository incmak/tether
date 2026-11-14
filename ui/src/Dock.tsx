import { Loading } from './Skeleton'
import { VoiceField } from './VoiceField'
import { PermissionsPanel } from './Permissions'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Search, Clock3, Check, Terminal, X, RefreshCw, Gauge, SquareTerminal, Layers } from 'lucide-react'
import { TextMessagePartProvider } from '@assistant-ui/react'
import { errorText, goalStatusLabel } from './api'
import type { Api, Approval, AskBridge, Attention, ChatData, Choice, Footer, FooterTask, LiveState, Pane, PromptAction } from './api'
import { useModal, usePolling } from './hooks'
import { agentNames } from './Sidebar'
import { Linked, PanelText, PanelPre, CopyButton, Markdown } from './Messages'
import { Elapsed } from './Notices'
import { LoginLink } from './LoginLink'
import { AskForm, Box, ChoiceList, ChoiceMeta } from './ChoiceExtras'
import './InfoPanel.css'








// A read-only panel such as Codex analytics. Its buttons are the keys its own footer offers, checked by the server before each one.
// The text stays exactly as the terminal shows it (tables and bars keep their columns); the card scrolls it, the buttons scroll the agent.
function InfoPanel({ info, disabled, onAction }: { info: Attention; disabled: boolean; onAction?: (action: PromptAction) => Promise<string | null> }) {
  const [busy, setBusy] = useState('')
  const [failed, setFailed] = useState('')
  const [revision, setRevision] = useState(info.revision)
  if (info.revision !== revision) { setRevision(info.revision); setFailed('') }
  const actions = info.actions ?? []
  const press = async (action: PromptAction) => {
    if (!onAction || busy) return
    setBusy(action.id); setFailed('')
    try { setFailed(await onAction(action) ?? '') } finally { setBusy('') }
  }
  const blocked = actions.filter(action => !action.enabled && action.disabledReason)
  const reasons = [...new Set(blocked.map(action => action.disabledReason!))]
  return <div className="info-card">
    <pre className="panel-pre info-body" tabIndex={0} aria-label="Panel text">{(info.body ?? info.text).replace(/\s+$/, '')}</pre>
    {!!actions.length && <div className="info-actions" role="group" aria-label="Panel keys">
      {actions.map(action => <button key={action.id} type="button" disabled={disabled || !action.enabled || !onAction || !!busy} aria-busy={busy === action.id || undefined}
        title={action.enabled ? undefined : action.disabledReason} onClick={() => void press(action)}>
        <span>{busy === action.id ? 'Sending…' : action.label}</span><kbd>{action.key}</kbd></button>)}
    </div>}
    {reasons.map(reason => <p key={reason} className="info-note">{reason}</p>)}
    {!actions.some(action => action.enabled) && <p className="info-note">This panel has no keys that Tether can send safely. Use the terminal to move in it.</p>}
    {failed && <p className="info-error" role="alert">{failed}</p>}
  </div>
}

export function ChoiceDock({ approval, attention, plan, agent, disabled, onChoose, onKey, onAction, onAsk, onAskEngage, onType, onFocusReply, permissionsTab }: { plan?: ChatData['plan']; approval: Approval | null; attention: Attention | null; agent: string; disabled: boolean; permissionsTab?: string | null
  onChoose: (kind: 'approval' | 'menu', option: Choice, approval: Approval | null, feedback?: string) => Promise<void>; onKey: (key: string) => unknown; onAction?: (action: PromptAction) => Promise<string | null>; onAsk?: (ask: AskBridge, answers: Record<string, string | string[]> | null) => Promise<string | null>; onAskEngage?: (ask: AskBridge) => Promise<number | null>; onType?: (text: string, loginUrl?: string) => Promise<boolean>; onFocusReply?: () => void }) {
  const [feedback, setFeedback] = useState('')
  const [feedbackVoiceBusy, setFeedbackVoiceBusy] = useState(false)
  const feedbackId = useId()
  const [code, setCode] = useState('')
  const [sending, setSending] = useState(false)
  const [codeSent, setCodeSent] = useState(false)
  const [picked, setPicked] = useState<number | null>(null)
  const kind = approval ? 'approval' : 'menu'
  // Claude's question held by the ask bridge: the phone answers it as data. The terminal shows nothing until it goes back.
  const bridge = !approval && onAsk ? attention?.askBridge : undefined
  // What the server proved about the list on screen: ticked boxes, tabs, filter, hidden rows, footer hints.
  const choice = !approval && !bridge ? attention?.choice : undefined
  // Claude's native question, read from the screen: which question is active and which boxes are ticked.
  const current = !approval && !bridge ? attention?.ask_current : undefined
  const checkedById = new Map([...(choice?.items ?? []).filter(item => item.checked !== undefined).map(item => [item.id, item.checked!] as const),
    ...(current?.options ?? []).filter(item => item.checked !== undefined).map(item => [item.id, item.checked!] as const)])
  const multiBox = current ? current.multi : choice?.selection === 'multi'
  const spaceKey = choice?.selection === 'multi' && choice.hints.some(hint => /\bspace\b/i.test(hint))
  const options = (approval ? (approval.actionable ? approval.options : undefined) : attention?.options?.filter(option => option.id <= 9)) ?? []
  const keepPlanning = plan && approval?.actionable ? options.find(option => /^No,?\s+keep planning\b/i.test(option.text)) : undefined
  const panel = !approval && attention?.reason === 'panel'
  // Claude's /btw: a question and its answer that stay out of the main conversation.
  const side = !approval && !plan && attention?.reason === 'side' ? attention : undefined
  // Claude's /permissions dialog: tabs, the list, and the keys that act on it.
  const perms = !approval && attention?.reason === 'permissions' ? attention.permissions : undefined
  const info = !approval && attention?.reason === 'info' ? attention : undefined
  // An unnumbered list such as Rewind: the arrow keys move the mark, Enter picks, Esc closes it.
  const picker = !approval && attention?.reason === 'picker' && attention.items?.length ? attention : undefined
  // A question taller than the pane: the screen shows only its last rows, so read it from the transcript. The keys answer it.
  const ask = !approval && !options.length && !panel && !perms && !info && !picker ? attention?.ask?.questions : undefined
  // A sign-in screen: its URL wraps over several lines, so the terminal can't open or copy it in one piece.
  const login = !approval && !options.length && !panel && !perms && !info && !picker ? attention?.login : undefined
  const keys = ask ? [...Array.from({ length: Math.min(9, Math.max(...ask.map(item => item.options.length)) + 2) }, (_, at) => String(at + 1)), ...(ask.length > 1 ? ['Left', 'Tab'] : [])] : ['1', '2', '3']
  const title = plan ? 'Review plan' : bridge ? `${agent} asks` : info ? `${agent} · ${info.title || 'panel'}` : login ? `${agent} sign-in` : approval ? `${agent} needs your permission` : perms ? `${agent} · Permissions` : panel ? `${agent} panel` : picker ? `${agent} · ${picker.title || 'Choose an item'}` : options.length || ask ? `${agent} asks` : `${agent} is waiting for you`
  const text = approval ? approval.text : attention?.question || attention?.text || ''
  const root = useRef<HTMLElement>(null)
  // The message box of this pane: the desktop workspace can show several.
  const focusBox = () => { if (onFocusReply) onFocusReply(); else root.current?.closest('.chat-panel')?.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus() }
  // "Chat about this" closes the question so the user can talk it through: put them in the message box after it.
  const chat = (option: Choice) => !approval && option.text === 'Chat about this'
  const pick = async (option: Choice) => {
    if (feedbackVoiceBusy) return
    if (option.free) { focusBox(); return }
    setPicked(option.id); try { await onChoose(kind, option, approval, option.id === keepPlanning?.id ? feedback.trim() || undefined : undefined) } finally { setPicked(null) }
    if (chat(option)) focusBox() }
  if (side) return <section ref={root} className="choice-dock side-dock open" aria-label={`${agent} side question`}>
    <div className="choice-head"><span className="choice-mark" />{agent} · Side question</div>
    <p className="side-question">{side.question}</p>
    {side.pending ? <Loading label="Answering…" className="side-wait" />
      : side.markdown ? <div className="side-answer assistant-message" tabIndex={0} aria-label="Answer"><TextMessagePartProvider text={side.text}><Markdown /></TextMessagePartProvider></div>
      : <PanelPre text={side.text} />}
    {side.markdown && <p className="info-note side-note">Claude keeps this out of the main conversation. Tether keeps a copy in this chat.</p>}
    <div className="choice-keys">
      <button type="button" disabled={disabled} onClick={() => onKey('Escape')}>Close</button>
      {side.fork && !side.pending && <button type="button" disabled={disabled} onClick={() => onKey('f')} title="Claude starts a subagent that continues from this answer">Continue in a subagent</button>}
      {!side.pending && side.text && <CopyButton text={side.text} label="Copy answer" className="copy-chip" />}
    </div>
  </section>
  return <section ref={root} className={`choice-dock ${plan ? 'plan-review' : ''} open`} aria-label={title}>
    <div className="choice-head"><span className="choice-mark" />{title}</div>
    <>
      {bridge ? <AskForm ask={bridge} disabled={disabled} onAnswer={answers => onAsk!(bridge, answers)} onEngage={onAskEngage && (() => onAskEngage(bridge))} /> : plan ? <div className="plan-text assistant-message" tabIndex={0} aria-label="Plan"><TextMessagePartProvider text={plan.text}><Markdown /></TextMessagePartProvider></div> : info ? <InfoPanel info={info} disabled={disabled} onAction={onAction} /> : perms ? <PermissionsPanel dialog={perms} disabled={disabled} want={permissionsTab} onKey={async key => { await onKey(key) }} /> : panel ? <PanelPre text={text} /> : picker ? <div className="picker-card">
        {picker.subtitle && <p className="picker-subtitle">{picker.subtitle}</p>}
        {choice && <ChoiceMeta choice={choice} />}
        {choice ? <ChoiceList choice={choice} label={picker.title || 'Items'} /> : <ul className="picker-list" aria-label={picker.title || 'Items'}>
          {!!picker.more_above && <li className="picker-more">↑ {picker.more_above} more</li>}
          {picker.items!.map((item, at) => <li key={at} className={item.selected ? 'current' : ''} aria-current={item.selected || undefined}>{item.text}</li>)}
          {!!picker.more_below && <li className="picker-more">↓ {picker.more_below} more</li>}
        </ul>}
        <p className="ask-hint">Use ↑ and ↓ to move the mark.{spaceKey ? ' Space ticks or clears the marked item.' : ''} Enter chooses the marked item. Esc closes the list.</p></div> : login ? <div className="login-card">
        <p>{login.code ? 'Open the sign-in page and sign in. Then paste the code it shows here.' : 'Open the sign-in page and enter the device code there.'}</p>
        <LoginLink url={login.url} />
        {login.user_code && <div className="login-device"><code>{login.user_code}</code><CopyButton text={login.user_code} label="Copy code" className="copy-chip" wide /></div>}
        {login.message && <p role="alert">{login.message}</p>}
        {codeSent && <p role="status">Code sent. Waiting for the agent to finish sign-in.</p>}
        {login.code && onType && <form className="login-card-code" onSubmit={event => { event.preventDefault(); const value = code.trim(); if (!value || sending || disabled) return; setSending(true); setCodeSent(false); void onType(value, login.url).then(ok => { if (ok) { setCode(''); setCodeSent(true) } }).finally(() => setSending(false)) }}>
          <label htmlFor={`login-code-${attention?.id}`}>Sign-in code</label>
          <div><input id={`login-code-${attention?.id}`} type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={500} value={code} disabled={disabled || sending} onChange={event => setCode(event.target.value)} />
          <button type="submit" className="primary" disabled={disabled || sending || !code.trim()}>{sending ? 'Sending…' : 'Send code'}</button></div></form>}
      </div> : ask ? <div className="ask-card">{ask.map((item, at) => <div key={at}>
        {(item.header || ask.length > 1) && <p className="ask-header">{ask.length > 1 ? `Question ${at + 1} of ${ask.length}` : ''}{ask.length > 1 && item.header ? ' · ' : ''}{item.header}</p>}
        <p className="ask-question"><Linked text={item.question} /></p>
        <ol className="ask-options">{item.options.map(option => <li key={option.label}><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</li>)}
          <li><strong>Type something</strong><small>Write your own answer in the terminal.</small></li><li><strong>Chat about this</strong></li></ol></div>)}
        {ask.length === 1 ? <p className="ask-hint">Press a number to choose. The pane is too short to show the whole question.</p> : <p className="ask-hint">The pane is too short to show the question, so this card cannot follow your answers. The terminal shows one question at a time: press a number to answer it, then the next one opens.{ask.some(item => item.multi) ? ' In a “choose one or more” question, a number ticks a box; press Question → when done.' : ''} Open the terminal view to see where you are.</p>}</div> : text && <div className={`choice-text ${approval || !options.length ? 'mono' : ''}`}>{approval || !options.length ? <PanelText text={text} /> : <Linked text={text} />}</div>}
      {keepPlanning && <div className="plan-feedback"><label htmlFor={feedbackId}>Keep planning — optional feedback</label>
        <VoiceField multiline label="plan feedback" value={feedback} setValue={setFeedback} onBusyChange={setFeedbackVoiceBusy}
          field={{ id: feedbackId, maxLength: 10000, rows: 2, disabled: disabled || picked !== null, placeholder: 'Tell Claude what to change' }} />
      </div>}
      {current && current.total > 1 && options.length > 0 && <p className="ask-progress">Question {current.index + 1} of {current.total} · {current.header}{current.multi ? ' · choose one or more' : ''}</p>}
      {!picker && choice && options.length > 0 && <ChoiceMeta choice={choice} />}
      {options.length > 0 && <div className="choice-options" role="list">{options.map(option => <button key={option.id} type="button" role="listitem" className={[option.selected ? 'current' : '', approval ? /^yes\b/i.test(option.text) ? 'accept' : /^no\b/i.test(option.text) ? 'decline' : '' : ''].filter(Boolean).join(' ')} disabled={disabled || picked !== null || feedbackVoiceBusy} onClick={() => void pick(option)}>
        <span className="choice-number">{picked === option.id ? '…' : option.id}</span>{checkedById.has(option.id) && <Box checked={checkedById.get(option.id)!} multi={multiBox} />}{option.free
          ? <span><strong>Type your own answer</strong><small>{option.text === 'Type something.' ? 'Write it in the message box below, then send.' : `In the terminal now: “${option.text}”. Your answer from the message box replaces it.`}</small></span>
          : <span><strong>{checkedById.has(option.id) ? option.text.replace(/^\[[ ✔xX]\]\s+/, '') : option.text}</strong>{chat(option) ? <small>Close the question and talk it through in the message box.</small> : option.detail && <small>{option.detail}</small>}</span>}</button>)}</div>}
      {!picker && choice && options.length > 0 && (choice.more_above !== 0 || choice.more_below !== 0) && <p className="choice-hints">{[choice.more_above !== 0 && (choice.more_above === -1 ? 'More options above.' : `${choice.more_above} more above.`), choice.more_below !== 0 && (choice.more_below === -1 ? 'More options below.' : `${choice.more_below} more below.`)].filter(Boolean).join(' ')} Use ↑ and ↓ to reach them.</p>}
      {bridge || perms ? null : info ? <div className="choice-keys">{(info.body ?? text) && <CopyButton text={info.body ?? text} label="Copy panel text" className="copy-chip" />}</div> : <div className="choice-keys">
        {!options.length && !panel && !login && [...(picker ? [] : keys), 'Up', 'Down', ...(spaceKey ? ['Space'] : []), 'Enter'].map(key => <button key={key} type="button" disabled={disabled} onClick={() => onKey(key)} aria-label={ask?.length === 1 && /^\d$/.test(key) ? `Choose ${key}: ${ask[0].options[Number(key) - 1]?.label ?? (Number(key) === ask[0].options.length + 1 ? 'Type something' : 'Chat about this')}` : undefined}>{key === 'Up' ? '↑' : key === 'Down' ? '↓' : key === 'Left' ? '← Question' : key === 'Tab' ? 'Question →' : key}</button>)}
        {!!options.length && current && current.total > 1 && ['Left', 'Tab'].map(key => <button key={key} type="button" disabled={disabled} onClick={() => onKey(key)}>{key === 'Left' ? '← Previous question' : 'Next question →'}</button>)}
        {!!options.length && spaceKey && ['Up', 'Down', 'Space', 'Enter'].map(key => <button key={key} type="button" disabled={disabled} onClick={() => onKey(key)}>{key === 'Up' ? '↑' : key === 'Down' ? '↓' : key}</button>)}
        <button type="button" disabled={disabled} onClick={() => onKey('Escape')}>{options.length || picker ? 'Cancel (Esc)' : 'Close (Esc)'}</button>
        {!options.length && !login && !picker && text && <CopyButton text={text} label="Copy panel text" className="copy-chip" />}
      </div>}
    </>
  </section>
}

// Claude Code's ✦ card in each state: an offer, "Making your page…", a ready page link, a written explanation, or a failure.
// "Chat in main session" puts the note in this composer (Claude would put it in the terminal's input box), then closes the card.
const plainMarkdown = (line: string) => line.replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1')
export function HeadsUp({ card, disabled, onKey, onChat }: { card: NonNullable<Footer['heads_up']>; disabled: boolean; onKey: (key: string) => Promise<void>; onChat: (note: string) => void }) {
  const [sent, setSent] = useState('')
  // The card always shows in full. The status panel scrolls, and its grip sets the height.
  const [previousCard, setPreviousCard] = useState({ text: card.text, state: card.state })
  if (card.text !== previousCard.text || card.state !== previousCard.state) {
    setPreviousCard({ text: card.text, state: card.state }); setSent('')
  }
  useEffect(() => { if (!sent) return; const timer = setTimeout(() => setSent(''), 4000); return () => clearTimeout(timer) }, [sent])  // the card did not change: let the person try again
  const linked = (text: string) => card.url && text.includes(card.url)
    ? <>{text.slice(0, text.indexOf(card.url))}<a href={card.url} target="_blank" rel="noreferrer">{card.url}</a>{text.slice(text.indexOf(card.url) + card.url.length)}</>
    : text
  const text = card.text.replace(/^Heads up · /, '')
  const lines = card.full ? card.full.split('\n').map(line => line.trim()) : card.detail ?? []
  const press = (key: string, label: string) => {
    setSent(label)
    if (card.note && label === 'Chat in main session') { onChat(card.note); key = '1' }  // "Understood" closes the card without filling the terminal's input
    void onKey(key).catch(() => setSent(''))
  }
  return <div className={`heads-up ${card.state ?? 'offer'}`} role="status">
    <p className="heads-up-text">{card.state === 'making' && <RefreshCw size={14} className="spin" />}{linked(text)}</p>
    {!!lines.length && <div className="heads-up-detail">{lines.map((line, index) => line ? <p key={index} className={/^[-*] /.test(line) ? 'bullet' : /^\*\*.+\*\*$/.test(line) ? 'title' : ''}>{linked(plainMarkdown(line.replace(/^[-*] /, '')))}</p> : null)}{!card.full && !!card.more && <p className="heads-up-more">{card.more} more lines in the terminal</p>}</div>}
    {card.state === 'ready' && card.url && !text.includes(card.url) && <p><a href={card.url} target="_blank" rel="noreferrer">Open the page</a></p>}
    {card.state === 'making' && <p className="heads-up-note">Claude keeps working. The page link shows here when it is ready.</p>}
    {!!card.options.length && <div className="heads-up-keys">{card.options.map(option => <button key={option.key} type="button" className={option.key === '0' ? 'secondary' : ''} disabled={disabled || !!sent} aria-busy={sent === option.label} onClick={() => press(option.key, option.label)}>{sent === option.label ? 'Sent…' : option.label}</button>)}</div>}
  </div>
}

// Claude's mode line says "2 shells" or "1 monitor". With hook details the chip names the work instead.
export const taskLabel = (task: string, details?: FooterTask[]) => {
  const kind = /^\d+ (\w+?)s?$/.exec(task)?.[1]
  const named = (details ?? []).filter(item => item.type === kind && item.description)
  if (!named.length) return task
  return named.length > 1 ? `${named[0].description} +${named.length - 1}` : String(named[0].description)
}

export function SessionInfo({ pane, footer, mode, onClose }: { pane: Pane; footer: Footer; mode: string; onClose: () => void }) {
  const modal = useModal(true, onClose)
  const parts = (footer.status ?? '').split('|').map(part => part.trim()).filter(Boolean)
  const rows: [string, string][] = [['Pane', `${pane.label}`], ['Folder', pane.dir], ...(pane.worktree ? [['Worktree branch', pane.worktree] as [string, string]] : []), ['Agent', agentNames[pane.agent]], ['State', pane.state],
    ...(pane.agent === 'claude' ? [['Mode', mode], ['Effort', footer.effort ?? 'Not shown'], ['Status line', parts.join(' · ') || 'Not shown'], ['Goal', footer.goal ? `Active${footer.goal !== 'active' ? ` · ${footer.goal}` : ''}` : 'None'], ['Background tasks', footer.tasks?.map(task => taskLabel(task)).join(', ') || 'None'], ['IDE context', footer.ide || 'None']] as [string, string][] : []),
    ...((footer.agents ?? []).filter(agent => !agent.main).map(agent => ['Background agent', [agent.name, agent.task, agent.detail].filter(Boolean).join(' · ')]) as [string, string][])]
  return <dialog {...modal} className="sheet" aria-labelledby="info-title">
    <div className="sheet-heading"><h2 id="info-title">Session details</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <dl className="info-list">{rows.map(([name, value], index) => <div key={index}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
  </dialog>
}
const since = (time?: number | null) => {
  if (!time) return ''
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - time) / 60))
  return minutes < 1 ? 'just now' : minutes < 60 ? `${minutes} min ago` : minutes < 1440 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min ago` : `${Math.floor(minutes / 1440)} d ago`
}

// The goal's last check, or the conversation's background tasks and their latest output.
export function LiveSheet({ api, pane, show, taskDetails, onClose, onTerminal }: { api: Api; pane: Pane; show: 'goal' | 'tasks'; taskDetails?: FooterTask[]; onClose: () => void; onTerminal: () => void }) {
  const modal = useModal(true, onClose)
  const [stopping, setStopping] = useState<string | null>(null)
  const [stopError, setStopError] = useState('')
  const fetcher = useCallback((signal: AbortSignal) => api<LiveState>(`live?session=${encodeURIComponent(pane.id)}`, undefined, signal), [api, pane.id])
  const { data: state, error, refresh } = usePolling(fetcher, 5000)
  const stop = async (task: string) => {
    if (!window.confirm('Stop this shell?')) return
    setStopping(task); setStopError('')
    try { await api('stop-task', { session: pane.id, task }); await refresh() }
    catch (error) { setStopError(error instanceof Error ? error.message : 'Could not stop this shell.') }
    finally { setStopping(null) }
  }
  const goal = state?.goal
  const hookTasks = show === 'tasks' && taskDetails !== undefined
  return <dialog {...modal} className="sheet live-sheet" aria-labelledby="live-title">
    <div className="sheet-heading"><h2 id="live-title">{show === 'goal' ? 'Goal' : 'Background tasks'}</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    {!hookTasks && error && <p className="live-empty" role="alert">Could not load: {error}</p>}
    {stopError && <p className="live-empty" role="alert">{stopError}</p>}
    {!hookTasks && !state && !error && <Loading className="live-empty" />}
    {state && show === 'goal' && (goal ? <div className="live-goal">
      <p className="live-label">Condition</p>
      <p className="live-condition">{goal.condition}</p>
      <p className={goal.met ? 'live-status met' : 'live-status'}>{goal.met ? <Check size={15} /> : <Clock3 size={15} />}{goal.status ? goalStatusLabel(goal.status) : goal.met ? 'Met' : 'Not met yet'}{goal.checked_at ? ` · checked ${since(goal.checked_at)}` : ''}</p>
      {goal.reason && <><p className="live-label">Last check</p><p className="live-reason">{goal.reason}</p></>}
      {goal.tokens_used != null && <p className="live-meta">Tokens: {goal.tokens_used.toLocaleString()}{goal.token_budget != null ? ` / ${goal.token_budget.toLocaleString()}` : ' · no token budget'}</p>}
      {goal.time_used_seconds != null && <p className="live-meta">Time used: {Math.floor(goal.time_used_seconds / 60)} min {goal.time_used_seconds % 60} sec</p>}
      {goal.set_at && <p className="live-meta">Set {since(goal.set_at)}</p>}
    </div> : <p className="live-empty">No goal found in this conversation.</p>)}
    {hookTasks && (taskDetails.length ? <ul className="live-tasks hook-tasks">{taskDetails.map(task => <li key={task.id}>
      <p className="live-task-title">{task.type === 'monitor' ? <Gauge size={15} /> : task.type === 'shell' ? <Terminal size={15} /> : <Layers size={15} />}<strong>{task.description || task.type || 'Task'}</strong></p>
      <p className="live-meta">{task.type || 'Task'}{task.status ? ` · ${task.status}` : ''}{task.started != null && <> · <Elapsed since={task.started * 1000} /></>}</p>
      {task.command && <code className="live-command">{task.command}</code>}
    </li>)}</ul> : <p className="live-empty">No background tasks in this conversation.</p>)}
    {!hookTasks && state && show === 'tasks' && (state.tasks.length ? <ul className="live-tasks">{[...state.tasks.filter(task => task.state === 'running').reverse(), ...state.tasks.filter(task => task.state !== 'running').reverse().slice(0, 5)].map(task => <li key={task.id}>
      <p className="live-task-title">{task.kind === 'monitor' ? <Gauge size={15} /> : <Terminal size={15} />}<strong>{task.title || task.command}</strong></p>
      <p className="live-meta">{task.kind === 'monitor' ? 'Monitor' : 'Shell'} · {task.state === 'running' ? 'Running' : 'Ended'} · {task.id}{task.started ? ` · started ${new Date(task.started * 1000).toLocaleString()}` : ''}</p>
      {task.title && <code className="live-command">{task.command}</code>}
      {task.output ? <pre className="live-output">{task.output}</pre> : <p className="live-meta">No output yet.</p>}
      {task.kind === 'shell' && task.state === 'running' && <div className="live-actions"><button type="button" disabled={stopping !== null} onClick={() => void stop(task.id)}>{stopping === task.id ? 'Stopping…' : 'Stop'}</button></div>}
    </li>)}</ul> : <p className="live-empty">No background tasks in this conversation.</p>)}
    <div className="live-actions"><button type="button" className="secondary" onClick={() => { onClose(); onTerminal() }}><SquareTerminal size={16} />Open terminal</button></div>
  </dialog>
}
interface SearchMatch { id: string; role: string; kind?: string; time?: string; from_end: number; snippet: string }
// Finds words in the whole conversation (not only the loaded part). Tapping a result loads enough of the chat and scrolls to it.
export function SearchSheet({ api, pane, shell, onClose, onPick }: { api: Api; pane: Pane; shell: boolean; onClose: () => void; onPick: (match: SearchMatch, maxLimit: number) => void }) {
  const modal = useModal(true, onClose)
  const [query, setQuery] = useState(() => lastSearch.get(pane.id) ?? '')
  const [result, setResult] = useState<{ matches: SearchMatch[]; total: number; max_limit: number; query: string } | null>(null)
  const [error, setError] = useState('')
  const [previousQuery, setPreviousQuery] = useState(query)
  if (query !== previousQuery) {
    setPreviousQuery(query)
    if (!query.trim()) { setResult(null); setError('') }
  }
  useEffect(() => {
    lastSearch.set(pane.id, query)
    if (!query.trim()) return
    const abort = new AbortController()
    const timer = setTimeout(() => {
      api<{ matches: SearchMatch[]; total: number; max_limit: number }>(`search?session=${encodeURIComponent(pane.id)}&q=${encodeURIComponent(query.trim())}${shell ? '&mode=shell' : ''}`, undefined, abort.signal)
        .then(data => { setResult({ ...data, query: query.trim() }); setError('') }, failure => { if (!abort.signal.aborted) setError(errorText(failure)) })
    }, 250)
    return () => { clearTimeout(timer); abort.abort() }
  }, [api, pane.id, shell, query])
  const words = (result?.query ?? '').toLowerCase().split(/\s+/).filter(Boolean)
  const marked = (text: string) => {
    if (!words.length) return text
    const parts = text.split(new RegExp(`(${words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'ig'))
    return parts.map((part, index) => index % 2 ? <mark key={index}>{part}</mark> : part)
  }
  return <dialog {...modal} className="sheet search-sheet" aria-labelledby="search-title">
    <div className="sheet-heading"><h2 id="search-title">Search this chat</h2><button type="button" className="icon-button" aria-label="Close" onClick={onClose}><X /></button></div>
    <label className="search-field"><Search size={17} /><input type="search" autoFocus enterKeyHint="search" placeholder="Words to find" aria-label="Words to find" value={query} onChange={event => setQuery(event.target.value)} /></label>
    {error && <p className="live-empty" role="alert">Could not search: {error}</p>}
    {result && <p className="search-count" role="status">{result.total === 0 ? 'No messages found.' : `${result.total} ${result.total === 1 ? 'message' : 'messages'}${result.total > result.matches.length ? `, newest ${result.matches.length} shown` : ''}`}</p>}
    {result && !!result.matches.length && <ul className="search-results">{result.matches.map(match => <li key={match.id}>
      <button type="button" disabled={match.from_end > result.max_limit} onClick={() => onPick(match, result.max_limit)}>
        <small>{match.kind === 'actions' ? 'Tools' : match.role === 'user' ? 'You' : 'Agent'}{match.time ? ` · ${since(Date.parse(match.time) / 1000)}` : ''}{match.from_end > result.max_limit ? ' · too far back to open' : ''}</small>
        <span>{marked(match.snippet)}</span>
      </button></li>)}</ul>}
  </dialog>
}
const lastSearch = new Map<string, string>()
